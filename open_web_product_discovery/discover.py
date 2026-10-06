"""Per-item open-web product discovery pipeline."""

from __future__ import annotations

import time
from typing import Any

from exact_product_url_discovery.classify import is_search_shell
from exact_product_url_discovery.search_convert import convert_internal_search
from exact_product_url_discovery.sitemap import discover_sitemap_urls
from exact_product_url_discovery.validate_identity import validate_product_page
from manufacturer_distributor_graph.exact_urls import curated_exact_urls
from manufacturer_distributor_graph.manufacturer_seed import authorized_distributors, resolve_manufacturer
from open_web_product_discovery.adapters_platt import platt_suggest_urls
from open_web_product_discovery.adapters_slug import diesel_slug_candidates, northernsafety_candidates
from open_web_product_discovery.adapters_open import (
    activeplumbing_candidates,
    gsistore_candidates,
    homelectrical_candidates,
)
from open_web_product_discovery.models import (
    BUILD,
    EXACT_PRODUCT_VERIFIED,
    ITEM_DEADLINE_S,
    MAX_CANDIDATES_VALIDATE,
    READY_FOR_PRICE_EXTRACTION,
    URL_DISCOVERY_PENDING,
)
from open_web_product_discovery.result_filter import classify_search_hit, is_allowed_candidate
from open_web_product_discovery.routing import build_query_set, preferred_domains, resolve_category
from open_web_product_discovery.store import learn_domain_pattern, upsert_product_url
from open_web_product_discovery.transport import note_product_yield, search_open_web
from price_adapters.validate import seller_of


def _add(bag: list[dict[str, Any]], seen: set[str], row: dict[str, Any]) -> None:
    url = row.get("url") or ""
    if not url.startswith("http") or url in seen or is_search_shell(url):
        return
    seen.add(url)
    bag.append(row)


def discover_candidates(item: dict[str, Any], *, deadline: float | None = None) -> dict[str, Any]:
    started = time.time()
    if deadline is None:
        deadline = started + ITEM_DEADLINE_S
    mpn = str(item.get("mpn") or item.get("part_number") or "")
    mfr = str(item.get("manufacturer") or "")
    bid = item.get("benchmark_id")
    cat = resolve_category(item)
    mfr_res = resolve_manufacturer(item)

    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    rejected: list[dict[str, Any]] = []
    methods: list[str] = []
    source_counts = {
        "search": 0,
        "manufacturer": 0,
        "distributor": 0,
        "seller_internal": 0,
        "sitemap": 0,
        "feed": 0,
        "pattern": 0,
        "curated": 0,
        "platt_graphql": 0,
        "other": 0,
    }

    # 1 Curated
    methods.append("CURATED")
    for u in curated_exact_urls(mpn) or []:
        if is_search_shell(u):
            rejected.append({"url": u, "reason": "SEARCH_PAGE", "source": "curated"})
            continue
        _add(
            candidates,
            seen,
            {
                "url": u,
                "domain": seller_of(u),
                "discovery_method": "CURATED_EXACT",
                "confidence": 0.85,
                "source": "curated",
            },
        )
        source_counts["curated"] += 1

    # 2 Platt GraphQL (electrical/tools priority, but try broadly — cheap & exact)
    if time.time() < deadline:
        methods.append("PLATT_GRAPHQL")
        for row in platt_suggest_urls(mpn, manufacturer=mfr, limit=4):
            row["source"] = "platt_graphql"
            _add(candidates, seen, row)
            source_counts["platt_graphql"] += 1

    # 3 Open-domain pattern adapters (homelectrical / activeplumbing / gsistore)
    if time.time() < deadline:
        methods.append("OPEN_DOMAIN_ADAPTERS")
        if cat in {"mro", "ppe", "lighting", "electrical", "default"}:
            for row in homelectrical_candidates(mpn, mfr):
                if "search" in (row.get("url") or "").lower():
                    continue  # search shells handled via internal conversion
                row["source"] = "pattern"
                _add(candidates, seen, row)
                source_counts["pattern"] += 1
        if cat in {"plumbing", "hvac", "default"}:
            for row in activeplumbing_candidates(mpn):
                row["source"] = "pattern"
                _add(candidates, seen, row)
                source_counts["pattern"] += 1
        if cat in {"hvac", "default"}:
            for row in gsistore_candidates(mpn, mfr):
                row["source"] = "pattern"
                _add(candidates, seen, row)
                source_counts["pattern"] += 1

    # 4 Category slug patterns
    if time.time() < deadline and cat in {"automotive_heavy", "mro", "default"}:
        methods.append("DIESEL_SLUG")
        for row in diesel_slug_candidates(mpn):
            row["source"] = "pattern"
            _add(candidates, seen, row)
            source_counts["pattern"] += 1
    if time.time() < deadline and cat in {"ppe", "mro"}:
        for row in northernsafety_candidates(mpn, mfr):
            row["source"] = "pattern"
            _add(candidates, seen, row)
            source_counts["pattern"] += 1

    # 4 Internal search conversion on preferred domains
    if time.time() < deadline and len(candidates) < 6:
        methods.append("INTERNAL_SEARCH")
        for dom in preferred_domains(item)[:3]:
            if time.time() > deadline:
                break
            for row in convert_internal_search(dom, mpn=mpn, manufacturer=mfr, limit=2):
                _add(
                    candidates,
                    seen,
                    {
                        "url": row["url"],
                        "domain": row.get("domain") or dom,
                        "discovery_method": "INTERNAL_SEARCH_CONVERSION",
                        "confidence": row.get("confidence", 0.55),
                        "source": "seller_internal",
                        "from_search_url": row.get("from_search_url"),
                    },
                )
                source_counts["seller_internal"] += 1

    # 5 Manufacturer / open-web search (transport recovery)
    if time.time() < deadline and len(candidates) < 8:
        methods.append("OPEN_WEB_SEARCH")
        for qrow in build_query_set(item)[:6]:
            if time.time() > deadline or len(candidates) >= 14:
                break
            prefer_pw = False  # Playwright SERP disabled for reliability; HTTP/phase_l only
            res = search_open_web(qrow["query"], limit=5, use_budget=True, prefer_playwright=prefer_pw)
            provider = res.get("provider") or "none"
            for hit in res.get("results") or []:
                cls = classify_search_hit(
                    url=hit.get("url") or "",
                    title=hit.get("title") or "",
                    snippet=hit.get("snippet") or "",
                    mpn=mpn,
                    manufacturer=mfr,
                )
                if not is_allowed_candidate(cls):
                    rejected.append(
                        {
                            "url": cls.get("url"),
                            "reason": cls.get("result_type"),
                            "source": "search",
                            "provider": provider,
                        }
                    )
                    continue
                method = "MANUFACTURER_PRODUCT_PAGE" if qrow["type"] == "manufacturer_mpn" else "SEARCH_RESULT_HREF"
                host = cls.get("domain") or ""
                mfr_domain = (mfr_res.get("manufacturer_domain") or "").lower()
                if mfr_domain and mfr_domain in host:
                    method = "MANUFACTURER_PRODUCT_PAGE"
                    source_counts["manufacturer"] += 1
                    source = "manufacturer"
                else:
                    source_counts["search"] += 1
                    source = "search"
                _add(
                    candidates,
                    seen,
                    {
                        "url": cls["url"],
                        "domain": host,
                        "discovery_method": method,
                        "confidence": 0.55,
                        "source": source,
                        "title": cls.get("title"),
                        "provider": provider,
                        "query_type": qrow.get("type"),
                    },
                )
                note_product_yield(provider, 1)

    # 6 Authorized distributors (light)
    if time.time() < deadline and len(candidates) < 8:
        methods.append("AUTH_DIST")
        for dist in authorized_distributors(mfr_res.get("manufacturer_key") or mfr)[:2]:
            dom = dist.get("distributor_domain") or ""
            if not dom:
                continue
            for row in convert_internal_search(dom, mpn=mpn, manufacturer=mfr, limit=2):
                _add(
                    candidates,
                    seen,
                    {
                        "url": row["url"],
                        "domain": dom,
                        "discovery_method": "AUTHORIZED_DISTRIBUTOR",
                        "confidence": 0.6,
                        "source": "distributor",
                    },
                )
                source_counts["distributor"] += 1
            for row in discover_sitemap_urls(dom, mpn=mpn, manufacturer=mfr, limit=2):
                _add(
                    candidates,
                    seen,
                    {
                        "url": row["url"],
                        "domain": dom,
                        "discovery_method": row.get("discovery_method") or "XML_SITEMAP",
                        "confidence": 0.5,
                        "source": "sitemap",
                    },
                )
                source_counts["sitemap"] += 1

    # Prefer high-confidence / platt / curated
    pref = {"platt.com": 0, "dieselpartsdirect.com": 1, "leviton.com": 2, "northernsafety.com": 3}
    candidates.sort(
        key=lambda c: (
            0 if c.get("discovery_method") in {"PLATT_GRAPHQL_SUGGEST", "CURATED_EXACT"} else 1,
            pref.get(c.get("domain") or "", 40),
            -float(c.get("confidence") or 0),
        )
    )
    return {
        "build": BUILD,
        "benchmark_id": bid,
        "mpn": mpn,
        "manufacturer": mfr,
        "category": cat,
        "candidates": candidates[:16],
        "rejected": rejected[:40],
        "methods_tried": methods,
        "source_counts": source_counts,
        "elapsed_s": round(time.time() - started, 3),
    }


def discover_and_validate(item: dict[str, Any], *, stats: dict[str, Any] | None = None) -> dict[str, Any]:
    stats = stats if stats is not None else {}
    for k in (
        "candidates_seen",
        "validated",
        "rejected_shells",
        "rejected_wrong_mpn",
        "rejected_wrong_mfr",
        "rejected_filter",
    ):
        stats.setdefault(k, 0)
    stats.setdefault("by_method", {})
    stats.setdefault("by_source", {})

    disc = discover_candidates(item, deadline=time.time() + ITEM_DEADLINE_S)
    mpn = disc["mpn"]
    mfr = disc["manufacturer"]
    bid = disc["benchmark_id"]
    validated: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    for rej in disc.get("rejected") or []:
        reason = str(rej.get("reason") or "")
        if "SEARCH" in reason or reason == "SEARCH_PAGE":
            stats["rejected_shells"] = int(stats["rejected_shells"]) + 1
        else:
            stats["rejected_filter"] = int(stats["rejected_filter"]) + 1

    budget = MAX_CANDIDATES_VALIDATE
    browser_left = 0  # HTTP-only validation for throughput/reliability
    for cand in disc.get("candidates") or []:
        if budget <= 0:
            break
        budget -= 1
        stats["candidates_seen"] = int(stats["candidates_seen"]) + 1
        url = cand["url"]
        method = cand.get("discovery_method") or "OTHER"
        source = cand.get("source") or "other"
        stats.setdefault("by_method", {}).setdefault(method, {"attempts": 0, "validated": 0})
        stats["by_method"][method]["attempts"] += 1
        stats.setdefault("by_source", {}).setdefault(source, {"attempts": 0, "validated": 0})
        stats["by_source"][source]["attempts"] += 1

        allow_browser = browser_left > 0 and method in {
            "CURATED_EXACT",
            "PLATT_GRAPHQL_SUGGEST",
            "MANUFACTURER_PRODUCT_PAGE",
            "SEARCH_RESULT_HREF",
            "AUTHORIZED_DISTRIBUTOR",
            "INTERNAL_SEARCH_CONVERSION",
            "HOMELECTRICAL_PATTERN",
            "ACTIVE_PLUMBING_PATTERN",
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
            learn_domain_pattern(cand.get("domain") or seller_of(url), url, mpn=mpn, success=False)
            if "mpn" in str(reason):
                stats["rejected_wrong_mpn"] = int(stats["rejected_wrong_mpn"]) + 1
            if "manufacturer" in str(reason):
                stats["rejected_wrong_mfr"] = int(stats["rejected_wrong_mfr"]) + 1
            continue

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
            extra={"title": v.get("title"), "source": source, "via": v.get("via")},
        )
        stats["validated"] = int(stats["validated"]) + 1
        stats["by_method"][method]["validated"] += 1
        stats["by_source"][source]["validated"] += 1
        validated.append({**rec, "title": v.get("title")})
        if len(validated) >= 1:
            break

    status = READY_FOR_PRICE_EXTRACTION if validated else URL_DISCOVERY_PENDING
    return {
        "build": BUILD,
        "benchmark_id": bid,
        "status": status,
        "category": disc.get("category"),
        "n_candidates": len(disc.get("candidates") or []),
        "n_validated": len(validated),
        "validated_urls": validated,
        "best_url": validated[0] if validated else None,
        "failures": failures[:15],
        "rejected": disc.get("rejected") or [],
        "methods_tried": disc.get("methods_tried"),
        "source_counts": disc.get("source_counts"),
        "discovery_elapsed_s": disc.get("elapsed_s"),
    }
