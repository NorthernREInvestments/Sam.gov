"""Per-item exact product URL discovery pipeline."""

from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import quote_plus, urlparse

from exact_product_url_discovery.classify import classify_candidate_url, is_search_shell
from exact_product_url_discovery.models import (
    BUILD,
    DEAD_PRIMARY_IDENTITY_ONLY,
    EXACT_PRODUCT_UNVERIFIED,
    EXACT_PRODUCT_VERIFIED,
    ITEM_DEADLINE_S,
    MAX_CANDIDATES_PER_ITEM,
    METHOD_AUTH_DIST,
    METHOD_BROWSER,
    METHOD_CURATED,
    METHOD_INTERNAL_SEARCH,
    METHOD_KNOWN_SELLER,
    METHOD_MANUFACTURER,
    METHOD_PATTERN,
    METHOD_SERP_HREF,
    PROVEN_OPEN,
    READY_FOR_PRICE_EXTRACTION,
    REJECTED,
)
from exact_product_url_discovery.normalize import is_short_mpn, mpn_in_text, mpn_variants
from exact_product_url_discovery.search_convert import convert_internal_search
from exact_product_url_discovery.sitemap import discover_sitemap_urls
from exact_product_url_discovery.store import note_domain_attempt, note_domain_success, upsert_product_url
from exact_product_url_discovery.validate_identity import validate_product_page
from manufacturer_distributor_graph.exact_urls import curated_exact_urls
from manufacturer_distributor_graph.manufacturer_seed import authorized_distributors, resolve_manufacturer
from price_adapters.validate import seller_of
from public_price_search.search import search_web

_SERP_OK: bool | None = None


def _serp_alive() -> bool:
    global _SERP_OK
    return _SERP_OK is not False


def _note_serp(res: dict[str, Any]) -> None:
    global _SERP_OK
    if res.get("results"):
        _SERP_OK = True
    elif _SERP_OK is None:
        # first empty → mark degraded so we stop burning 30s/query
        _SERP_OK = False


def _add_candidate(
    bag: list[dict[str, Any]],
    seen: set[str],
    *,
    url: str,
    method: str,
    confidence: float = 0.5,
    extra: dict[str, Any] | None = None,
    mpn: str = "",
) -> None:
    if not url or not url.startswith("http"):
        return
    url = "".join(ch for ch in url if 32 <= ord(ch) < 127)
    if url in seen or is_search_shell(url):
        return
    cls = classify_candidate_url(url, mpn=mpn)
    if cls in REJECTED:
        return
    seen.add(url)
    row = {
        "url": url,
        "domain": seller_of(url),
        "url_class": cls,
        "discovery_method": method,
        "confidence": confidence,
    }
    if extra:
        row.update(extra)
    bag.append(row)


def discover_candidates(item: dict[str, Any], *, deadline: float | None = None) -> dict[str, Any]:
    """Run discovery pipeline; return candidates (not yet content-validated)."""
    started = time.time()
    if deadline is None:
        deadline = started + ITEM_DEADLINE_S
    mpn = str(item.get("mpn") or item.get("part_number") or "")
    mfr = str(item.get("manufacturer") or "")
    bid = item.get("benchmark_id")
    known = str(item.get("known_public_seller") or "").lower().replace("www.", "")
    mfr_res = resolve_manufacturer(item)

    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    rejected: list[dict[str, str]] = []
    methods_tried: list[str] = []

    # 1 Curated / known exact
    methods_tried.append(METHOD_CURATED)
    for u in curated_exact_urls(mpn) or []:
        if is_search_shell(u):
            rejected.append({"url": u, "reason": "SEARCH_SHELL"})
            continue
        _add_candidate(candidates, seen, url=u, method=METHOD_CURATED, confidence=0.8, mpn=mpn)

    # 1b Fast pattern guesses on open catalogs (before HTTP-heavy search)
    methods_tried.append(METHOD_PATTERN)
    toks = mpn_variants(mpn)
    tok0 = toks[0] if toks else mpn
    tok_l = (tok0 or "").lower()
    for guess in (
        f"https://www.dieselpartsdirect.com/{tok_l}",
        f"https://www.dieselpartsdirect.com/{tok0}",
        f"https://www.globalindustrial.com/p/{tok_l}",
        f"https://www.northernsafety.com/Product/{tok0}/{(mfr or 'Product').split()[0]}-{tok0}",
        f"https://parts-hvac.com/{tok_l}.html",
    ):
        if is_search_shell(guess):
            continue
        _add_candidate(candidates, seen, url=guess, method=METHOD_PATTERN, confidence=0.4, mpn=mpn)
    if mfr and "leviton" in mfr.lower():
        _add_candidate(
            candidates,
            seen,
            url=f"https://www.leviton.com/en/products/{tok_l}",
            method=METHOD_PATTERN,
            confidence=0.5,
            mpn=mpn,
        )

    # 2 Known seller internal search conversion (even if dead-primary — convert only)
    if known and time.time() < deadline and len(candidates) < 6:
        methods_tried.append(METHOD_KNOWN_SELLER)
        if known not in DEAD_PRIMARY_IDENTITY_ONLY:
            for row in convert_internal_search(known, mpn=mpn, manufacturer=mfr, limit=3):
                _add_candidate(
                    candidates,
                    seen,
                    url=row["url"],
                    method=row["discovery_method"],
                    confidence=row.get("confidence", 0.6),
                    extra={"from_search_url": row.get("from_search_url")},
                    mpn=mpn,
                )

    # 3 Proven open domains — only if still thin
    OPEN_FAST = (
        "platt.com",
        "dieselpartsdirect.com",
        "northernsafety.com",
        "globalindustrial.com",
    )
    if time.time() < deadline and len(candidates) < 4:
        methods_tried.append("PROVEN_OPEN_INTERNAL_SEARCH")
        for dom in OPEN_FAST:
            if time.time() > deadline or len(candidates) >= MAX_CANDIDATES_PER_ITEM:
                break
            if known and dom == known:
                continue
            note_domain_attempt(dom)
            for row in convert_internal_search(dom, mpn=mpn, manufacturer=mfr, limit=2):
                _add_candidate(
                    candidates,
                    seen,
                    url=row["url"],
                    method=row["discovery_method"],
                    confidence=row.get("confidence", 0.55),
                    mpn=mpn,
                )

    # 4 Authorized distributors (cap hard)
    if time.time() < deadline and len(candidates) < 5:
        methods_tried.append(METHOD_AUTH_DIST)
        for dist in authorized_distributors(mfr_res.get("manufacturer_key") or mfr)[:2]:
            if time.time() > deadline:
                break
            dom = dist.get("distributor_domain") or ""
            if not dom or dom in DEAD_PRIMARY_IDENTITY_ONLY:
                continue
            for row in convert_internal_search(dom, mpn=mpn, manufacturer=mfr, limit=2):
                _add_candidate(
                    candidates,
                    seen,
                    url=row["url"],
                    method=METHOD_AUTH_DIST,
                    confidence=0.65,
                    mpn=mpn,
                )

    # 5 Manufacturer / SERP — HTTP SERP, else Bing Playwright href extraction (SERP transport only)
    if time.time() < deadline and mfr and len(candidates) < 3:
        methods_tried.append(METHOD_MANUFACTURER)
        q = f'{mfr} "{mpn}" product -site:grainger.com -site:amazon.com'
        if is_short_mpn(mpn):
            q = f'{mfr} "{mpn}" {(item.get("description") or "")[:40]}'
        res_rows: list[dict[str, Any]] = []
        if _serp_alive():
            try:
                res = search_web(q, limit=5, use_budget=True)
            except Exception:
                res = {"results": []}
            _note_serp(res)
            res_rows = list(res.get("results") or [])
        if not res_rows and time.time() < deadline:
            try:
                from price_adapters.browser import bing_discover_urls

                for row in bing_discover_urls(q, limit=5):
                    res_rows.append({"url": row.get("url"), "title": row.get("title"), "snippet": ""})
            except Exception:
                pass
        mfr_domain = (mfr_res.get("manufacturer_domain") or "").lower()
        for r in res_rows:
            url = r.get("url") or ""
            if is_search_shell(url):
                rejected.append({"url": url, "reason": "SEARCH_SHELL"})
                continue
            host = seller_of(url)
            method = METHOD_MANUFACTURER if mfr_domain and mfr_domain in host else METHOD_SERP_HREF
            if not mpn_in_text(mpn, url, r.get("title") or "", r.get("snippet") or ""):
                if is_short_mpn(mpn):
                    continue
            _add_candidate(
                candidates,
                seen,
                url=url,
                method=method,
                confidence=0.55,
                extra={"title": (r.get("title") or "")[:120]},
                mpn=mpn,
            )

    OPEN_PREF = {
        "platt.com": 0,
        "dieselpartsdirect.com": 1,
        "leviton.com": 2,
        "northernsafety.com": 3,
        "globalindustrial.com": 4,
        "parts-hvac.com": 5,
    }
    # Prefer structural verified + open domains
    candidates.sort(
        key=lambda c: (
            0 if c.get("url_class") == EXACT_PRODUCT_VERIFIED else 1,
            OPEN_PREF.get(c.get("domain") or "", 50),
            -float(c.get("confidence") or 0),
            c.get("domain") or "",
        )
    )
    return {
        "build": BUILD,
        "benchmark_id": bid,
        "mpn": mpn,
        "manufacturer": mfr,
        "candidates": candidates[:MAX_CANDIDATES_PER_ITEM],
        "rejected": rejected[:30],
        "methods_tried": methods_tried,
        "elapsed_s": round(time.time() - started, 3),
    }


def discover_and_validate(item: dict[str, Any], *, stats: dict[str, Any] | None = None) -> dict[str, Any]:
    """Full discovery + identity validation. Persist READY_FOR_PRICE_EXTRACTION records."""
    stats = stats if stats is not None else {}
    stats.setdefault("candidates_seen", 0)
    stats.setdefault("validated", 0)
    stats.setdefault("rejected_shells", 0)
    stats.setdefault("rejected_wrong_mpn", 0)
    stats.setdefault("rejected_wrong_mfr", 0)
    stats.setdefault("by_method", {})

    disc = discover_candidates(item, deadline=time.time() + ITEM_DEADLINE_S)
    mpn = disc["mpn"]
    mfr = disc["manufacturer"]
    bid = disc["benchmark_id"]
    validated: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    for rej in disc.get("rejected") or []:
        stats["rejected_shells"] = int(stats.get("rejected_shells") or 0) + 1

    browser_left = 2
    validated_budget = 8
    for cand in disc.get("candidates") or []:
        if validated_budget <= 0:
            break
        stats["candidates_seen"] = int(stats.get("candidates_seen") or 0) + 1
        url = cand["url"]
        method = cand.get("discovery_method") or "OTHER"
        stats.setdefault("by_method", {}).setdefault(method, {"attempts": 0, "validated": 0})
        stats["by_method"][method]["attempts"] += 1
        validated_budget -= 1

        allow_browser = browser_left > 0 and method in {
            METHOD_CURATED,
            METHOD_KNOWN_SELLER,
            METHOD_AUTH_DIST,
            METHOD_INTERNAL_SEARCH,
            METHOD_MANUFACTURER,
        }
        v = validate_product_page(
            url,
            mpn=mpn,
            manufacturer=mfr,
            description=item.get("description"),
            category=item.get("category"),
            allow_browser=allow_browser,
        )
        if allow_browser and v.get("via") == "BROWSER_ASSISTED":
            browser_left -= 1
        if not v.get("identity_match"):
            reason = v.get("reason") or "fail"
            failures.append({"url": url, "reason": reason, "method": method})
            if "mpn" in str(reason):
                stats["rejected_wrong_mpn"] = int(stats.get("rejected_wrong_mpn") or 0) + 1
            if "manufacturer" in str(reason):
                stats["rejected_wrong_mfr"] = int(stats.get("rejected_wrong_mfr") or 0) + 1
            continue

        via = v.get("via")
        if via == "BROWSER_ASSISTED":
            method_effective = METHOD_BROWSER
        else:
            method_effective = method
        if method_effective != method:
            stats["by_method"][method]["attempts"] = max(0, int(stats["by_method"][method]["attempts"]) - 1)
            stats.setdefault("by_method", {}).setdefault(method_effective, {"attempts": 0, "validated": 0})
            stats["by_method"][method_effective]["attempts"] += 1
            method = method_effective

        rec = upsert_product_url(
            manufacturer=mfr,
            mpn=mpn,
            url=url,
            url_type=EXACT_PRODUCT_VERIFIED,
            discovery_method=method,
            confidence=max(float(cand.get("confidence") or 0.5), 0.85),
            identity_match=True,
            price_extractability=v.get("price_extractability") or "UNKNOWN",
            benchmark_id=str(bid) if bid else None,
            seller_domain=cand.get("domain"),
            pack=v.get("pack_hint"),
            uom=v.get("uom_hint"),
            extra={"title": v.get("title"), "from_search_url": cand.get("from_search_url"), "via": via},
        )
        note_domain_success(cand.get("domain") or seller_of(url), url, mpn=mpn)
        stats["validated"] = int(stats.get("validated") or 0) + 1
        stats["by_method"][method]["validated"] += 1
        validated.append(
            {
                **rec,
                "title": v.get("title"),
                "price_extractability": v.get("price_extractability"),
                "via": via,
            }
        )
        if len(validated) >= 1:
            break

    status = READY_FOR_PRICE_EXTRACTION if validated else "URL_DISCOVERY_PENDING"
    return {
        "build": BUILD,
        "benchmark_id": bid,
        "status": status,
        "n_candidates": len(disc.get("candidates") or []),
        "n_validated": len(validated),
        "validated_urls": validated,
        "best_url": validated[0] if validated else None,
        "failures": failures[:15],
        "rejected_shells": disc.get("rejected") or [],
        "methods_tried": disc.get("methods_tried"),
        "discovery_elapsed_s": disc.get("elapsed_s"),
    }
