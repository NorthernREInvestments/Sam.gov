"""Recover public prices via seller rediscovery + JSON-LD-first extraction."""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any

from exact_page_extraction.models import EXACT_PRODUCT_VERIFIED
from exact_page_extraction.url_classify import classify_url
from m3_data_root import data_path
from price_adapters.orchestrate import _identity
from price_adapters.validate import seller_of
from price_coverage_80.scoring import score_accuracy
from public_price_search.search import fetch_page
from seller_rediscovery.adapters import jsonld_first_extract
from seller_rediscovery.domain_yield import is_suppressed, note_attempt, should_attempt, tier_rank
from seller_rediscovery.models import (
    BUILD,
    ITEM_DEADLINE_S,
    MAX_RETRIES,
    MIN_ALT_SELLERS,
    PAGE_CACHE,
    PRODUCT_PUBLIC_PRICE_EXHAUSTED,
    REQUEST_TIMEOUT_S,
    ROUTE_ALT_SELLER,
    ROUTE_BROWSER,
    SELLER_EXHAUSTED,
)
from seller_rediscovery.rediscover import rediscover_exact_sellers


def _page_cache_load() -> dict[str, Any]:
    p = data_path(PAGE_CACHE)
    if not p.exists():
        return {"pages": {}}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"pages": {}}


def _page_cache_get(url: str) -> dict[str, Any] | None:
    cache = _page_cache_load()
    key = hashlib.sha1(url.encode("utf-8")).hexdigest()
    return (cache.get("pages") or {}).get(key)


def _page_cache_put(url: str, payload: dict[str, Any]) -> None:
    cache = _page_cache_load()
    key = hashlib.sha1(url.encode("utf-8")).hexdigest()
    # Store truncated html
    html = payload.get("html") or ""
    cache.setdefault("pages", {})[key] = {
        "url": url,
        "ok": payload.get("ok"),
        "blocked": payload.get("blocked"),
        "status_code": payload.get("status_code"),
        "html": html[:400000] if html else "",
        "cached_at": time.time(),
    }
    p = data_path(PAGE_CACHE)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Avoid huge rewrites every call — only persist every put (pages are few per stage)
    try:
        p.write_text(json.dumps(cache, default=str), encoding="utf-8")
    except Exception:
        pass


def _call_for_price(html: str) -> bool:
    if not html:
        return False
    return bool(
        re.search(
            r"call\s*(for|/)\s*price|login\s*to\s*(see|view)\s*price|price\s*upon\s*request|"
            r"request\s*a\s*quote|sign\s*in\s*for\s*price",
            html[:20000],
            re.I,
        )
    )


def _fetch(url: str, stats: dict[str, Any]) -> dict[str, Any]:
    cached = _page_cache_get(url)
    if cached is not None:
        stats["cache_hits"] = int(stats.get("cache_hits") or 0) + 1
        return cached
    stats["http_requests"] = int(stats.get("http_requests") or 0) + 1
    last: dict[str, Any] = {"ok": False, "blocked": False, "html": "", "status_code": 0}
    for attempt in range(MAX_RETRIES + 1):
        try:
            # fetch_page owns its own short timeout; do not pass unsupported kwargs
            fr = fetch_page(url, use_budget=True)
            html = fr.get("text") or fr.get("html") or ""
            last = {
                "ok": bool(fr.get("ok")),
                "blocked": bool(fr.get("blocked")),
                "html": html,
                "status_code": fr.get("status_code") or fr.get("status") or 0,
                "error": fr.get("error"),
            }
            if last["ok"] or last["blocked"] or html:
                break
        except TypeError:
            # Compatibility fallback if signature differs
            try:
                fr = fetch_page(url)
                html = fr.get("text") or fr.get("html") or ""
                last = {
                    "ok": bool(fr.get("ok")),
                    "blocked": bool(fr.get("blocked")),
                    "html": html,
                    "status_code": fr.get("status_code") or 0,
                }
                break
            except Exception as exc:
                last = {"ok": False, "blocked": False, "html": "", "status_code": 0, "error": str(exc)}
        except Exception as exc:
            last = {"ok": False, "blocked": False, "html": "", "status_code": 0, "error": str(exc)}
        if attempt < MAX_RETRIES:
            time.sleep(0.3)
    _page_cache_put(url, last)
    return last


def _browser_allowed_for(domain: str) -> bool:
    """Only allow browser if domain memory proves browser yield > 0."""
    try:
        from exact_page_extraction.domain_memory import _load

        row = ((_load().get("domains") or {}).get(domain) or {})
        by = row.get("by_route") or {}
        return int((by.get("BROWSER") or {}).get("successes") or 0) > 0
    except Exception:
        return False


def _try_url(
    url: str,
    item: dict[str, Any],
    *,
    stats: dict[str, Any],
    seeded: bool,
    discovery_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    domain = seller_of(url)
    out: dict[str, Any] = {
        "url": url,
        "seller": domain,
        "domain": domain,
        "seeded": seeded,
        "identity_confidence": classify_url(url, mpn=str(_identity(item).get("part_number") or "")),
        "blocked": False,
        "call_for_price": False,
        "best": None,
        "route": None,
        "validation": None,
    }
    if is_suppressed(domain):
        out["skipped"] = "LOW_YIELD_BLOCKED"
        return out
    if not should_attempt(domain) and tier_rank(domain) >= 3 and not seeded:
        out["skipped"] = "TIER_D_DEPRIORITIZED"
        return out
    from exact_page_extraction.models import SEARCH_RESULT_SHELL

    if classify_url(url, mpn=str(_identity(item).get("part_number") or "")) == SEARCH_RESULT_SHELL:
        out["skipped"] = SEARCH_RESULT_SHELL
        out["price_visibility_status"] = SEARCH_RESULT_SHELL
        return out
    if "/search?" in url.lower() or url.lower().rstrip("/").endswith("/search"):
        out["skipped"] = "SEARCH_PATH"
        out["price_visibility_status"] = "SEARCH_PATH"
        return out

    fr = _fetch(url, stats)
    html = fr.get("html") or ""
    bot = bool(fr.get("blocked"))
    cfp = _call_for_price(html) if html and not bot else False
    out["blocked"] = bot
    out["call_for_price"] = cfp
    out["status_code"] = fr.get("status_code")
    out["cache_hit"] = fr.get("cached_at") is not None and not fr.get("ok") is None

    if bot:
        note_attempt(domain, validated=False, bot_wall=True, http_requests=1)
        out["price_visibility_status"] = "BOT_WALL"
        if discovery_meta is not None:
            discovery_meta["price_visibility_status"] = "BOT_WALL"
        return out
    if cfp:
        note_attempt(domain, validated=False, call_for_price=True, http_requests=1)
        out["price_visibility_status"] = "CALL_FOR_PRICE"
        return out
    if not fr.get("ok") or not html:
        note_attempt(domain, validated=False, http_requests=1)
        out["price_visibility_status"] = "NO_HTML"
        return out

    # Reject shell/homepage fetches that lost the product identity
    identity = _identity(item)
    mpn = str(identity.get("part_number") or "")
    if mpn:
        from price_adapters.validate import mpn_in_blob

        title_m = re.search(r"<title[^>]*>(.*?)</title>", html[:8000], re.I | re.S)
        title = re.sub(r"<[^>]+>", "", title_m.group(1) if title_m else "")
        if not mpn_in_blob(mpn, title, url, html[:12000]):
            note_attempt(domain, validated=False, http_requests=1)
            out["price_visibility_status"] = "MPN_NOT_ON_PAGE"
            return out

    best, route = jsonld_first_extract(html, url=url, item=item)

    # Browser only if prior evidence of domain browser yield > 0
    if not best and _browser_allowed_for(domain):
        try:
            from price_adapters.browser import bounded_browser_fetch

            stats["browser_renders"] = int(stats.get("browser_renders") or 0) + 1
            br = bounded_browser_fetch(url, timeout_ms=12000)
            bhtml = (br or {}).get("html") or ""
            if bhtml:
                best, route = jsonld_first_extract(bhtml, url=url, item=item)
                if best:
                    best["via"] = (best.get("via") or "") + "+browser"
                    route = ROUTE_BROWSER
        except Exception:
            pass

    if best:
        # Pack / absurd EA price guard (covers extract_from_html path too)
        try:
            p = float(best.get("unit_price") or 0)
        except Exception:
            p = 0.0
        exp_pack = int(item.get("expected_pack") or 1)
        cat = str(item.get("category") or item.get("benchmark_id") or "").lower()
        if (
            item.get("known_public_price") is None
            and exp_pack <= 1
            and p > 400
            and any(x in cat for x in ("mro", "ppe", "plumb", "light", "office", "auto", "tool", "ind-", "elec"))
        ):
            note_attempt(domain, validated=False, price_found=True, http_requests=1)
            out["price_visibility_status"] = "ABSURD_EA_PRICE"
            return out
        # Accuracy gate
        probe = {
            "usable": True,
            "unit_price": best.get("unit_price"),
            "condition": best.get("condition") or "NEW",
            "seller": best.get("seller") or domain,
            "source_url": url,
        }
        acc = score_accuracy(item, probe)
        out["validation"] = acc
        if acc.get("correct"):
            if not seeded:
                best = {**best, "via": (best.get("via") or "") + "+alt_seller"}
                route = route or ROUTE_ALT_SELLER
                stats.setdefault("route_counts", {})
                stats["route_counts"][ROUTE_ALT_SELLER] = int(
                    stats["route_counts"].get(ROUTE_ALT_SELLER) or 0
                ) + 1
            stats.setdefault("route_counts", {})
            rkey = route or best.get("route") or "UNKNOWN"
            stats["route_counts"][rkey] = int(stats["route_counts"].get(rkey) or 0) + 1
            out["best"] = best
            out["route"] = route
            out["price_visibility_status"] = "PRICE_VISIBLE"
            note_attempt(
                domain,
                validated=True,
                price_found=True,
                route=str(route or ""),
                http_requests=1,
            )
            return out
        out["price_visibility_status"] = "PRICE_FAILED_VALIDATION"
        note_attempt(domain, validated=False, price_found=True, route=str(route or ""), http_requests=1)
        return out

    out["price_visibility_status"] = "NO_PRICE"
    note_attempt(domain, validated=False, http_requests=1)
    return out


def recover_with_rediscovery(
    item: dict[str, Any],
    *,
    seeded_urls: list[str] | None = None,
    stats: dict[str, Any] | None = None,
    item_deadline: float | None = None,
    allow_rediscovery: bool = True,
) -> dict[str, Any]:
    stats = stats if stats is not None else {}
    stats.setdefault("http_requests", 0)
    stats.setdefault("browser_renders", 0)
    stats.setdefault("cache_hits", 0)
    stats.setdefault("route_counts", {})
    started = time.time()
    if item_deadline is None:
        item_deadline = started + ITEM_DEADLINE_S
    # Reserve time for rediscovery + alternate sellers
    rediscovery_deadline = started + max(ITEM_DEADLINE_S, 70.0)
    item_deadline = max(item_deadline, rediscovery_deadline)

    identity = _identity(item)
    mpn = str(identity.get("part_number") or "")
    bid = item.get("benchmark_id") or item.get("id")

    # Purge bad seeds (wrong SKU / marketplaces)
    from seller_rediscovery.models import REJECT_HOST_FRAGMENTS
    from seller_rediscovery.url_guards import reject_dwt6_wrong_page

    seeds = []
    for u in seeded_urls or []:
        if not u:
            continue
        if bid == "easy-global-dwt-6" and reject_dwt6_wrong_page(u):
            continue
        host = seller_of(u)
        if any(x in host for x in REJECT_HOST_FRAGMENTS):
            continue
        seeds.append(u)

    diag: dict[str, Any] = {
        "build": BUILD,
        "benchmark_id": bid,
        "identity": identity,
        "seeded_urls": seeds,
        "discovery": None,
        "urls_tried": [],
        "candidates": [],
        "best": None,
        "status": PRODUCT_PUBLIC_PRICE_EXHAUSTED,
        "seller_exhausted": False,
        "alternate_seller_recovery": False,
        "n_alt_attempted": 0,
    }

    # Phase 1: seeded sellers
    seller_hit = False
    for url in seeds[:4]:
        if time.time() > item_deadline:
            break
        row = _try_url(url, item, stats=stats, seeded=True)
        diag["urls_tried"].append(row)
        if row.get("best"):
            diag["candidates"].append(row["best"])
            seller_hit = True
            break
    if not seller_hit and seeds:
        diag["seller_exhausted"] = True
        diag["status"] = SELLER_EXHAUSTED

    # Phase 2: rediscovery
    discovered_rows: list[dict[str, Any]] = []
    if not diag["candidates"] and allow_rediscovery and time.time() < item_deadline:
        disc = rediscover_exact_sellers(item, existing_urls=seeds, max_queries=6)
        diag["discovery"] = {
            "n_new_exact": disc.get("n_new_exact"),
            "n_verified": disc.get("n_verified"),
            "queries": disc.get("queries"),
            "tier_mix": disc.get("tier_mix"),
            "discovered_exact_urls": disc.get("discovered_exact_urls"),
        }
        discovered_rows = list(disc.get("discovered_exact_urls") or [])

    alt_attempted = 0
    for drow in discovered_rows:
        if diag["candidates"] or time.time() > item_deadline:
            break
        url = drow.get("url") or ""
        if not url or url in seeds:
            continue
        # Prefer Tier A/B; allow Tier C; skip suppressed; soft-skip Tier D after MIN_ALT
        domain = drow.get("domain") or seller_of(url)
        if is_suppressed(domain):
            drow["validation_result"] = "SKIPPED_SUPPRESSED"
            continue
        if tier_rank(domain) >= 3 and alt_attempted >= MIN_ALT_SELLERS:
            continue
        alt_attempted += 1
        row = _try_url(url, item, stats=stats, seeded=False, discovery_meta=drow)
        drow["extraction_route_attempted"] = row.get("route")
        drow["price_visibility_status"] = row.get("price_visibility_status")
        drow["validation_result"] = (row.get("validation") or {}).get("class") or row.get(
            "price_visibility_status"
        )
        diag["urls_tried"].append(row)
        if row.get("best"):
            diag["candidates"].append(row["best"])
            diag["alternate_seller_recovery"] = True
            break

    diag["n_alt_attempted"] = alt_attempted

    if diag["candidates"]:
        def _rank(c: dict[str, Any]) -> tuple:
            probe = {
                "usable": True,
                "unit_price": c.get("unit_price"),
                "condition": c.get("condition") or "NEW",
                "seller": c.get("seller"),
                "source_url": c.get("url"),
            }
            acc = score_accuracy(item, probe)
            return (0 if acc.get("correct") else 1, float(c["unit_price"]))

        diag["candidates"].sort(key=_rank)
        best = diag["candidates"][0]
        best["selection_reason"] = (
            f"BEST_DEFENSIBLE_PUBLIC_PRICE; among {len(diag['candidates'])}; "
            f"seller={best.get('seller')}; via={best.get('via')}; "
            f"alt={diag['alternate_seller_recovery']}"
        )
        diag["best"] = best
        diag["status"] = "FOUND_VALID_PRICE"
    else:
        # Exhaustion policy
        n_viable = len(discovered_rows)
        if diag.get("seller_exhausted") and (n_viable == 0 or alt_attempted >= min(MIN_ALT_SELLERS, max(n_viable, 1))):
            diag["status"] = PRODUCT_PUBLIC_PRICE_EXHAUSTED
        elif diag.get("seller_exhausted"):
            diag["status"] = SELLER_EXHAUSTED
        else:
            diag["status"] = PRODUCT_PUBLIC_PRICE_EXHAUSTED

    diag["elapsed_s"] = round(time.time() - started, 3)
    diag["mpn_url_class_seed"] = [
        classify_url(u, mpn=mpn) for u in seeds[:4]
    ]
    return diag
