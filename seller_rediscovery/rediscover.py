"""Exact-MPN seller rediscovery via public search (snippets → candidate URLs only)."""

from __future__ import annotations

import json
import re
import time
from typing import Any
from urllib.parse import urlparse

from exact_page_extraction.models import (
    CATEGORY_PAGE,
    EXACT_PRODUCT_UNVERIFIED,
    EXACT_PRODUCT_VERIFIED,
    PRODUCT_FAMILY_PAGE,
    SEARCH_RESULT_SHELL,
    WRONG_PRODUCT,
)
from exact_page_extraction.url_classify import classify_url
from m3_data_root import data_path
from price_adapters.validate import seller_of
from seller_rediscovery.domain_yield import is_suppressed, rank_urls, tier_rank
from seller_rediscovery.models import (
    BUILD,
    DISCOVERY_CACHE,
    MAX_CANDIDATE_URLS,
    PREFERRED_OPEN_DISTRIBUTORS,
    REJECT_HOST_FRAGMENTS,
    SEED_TIER_A,
)


def _load_cache() -> dict[str, Any]:
    p = data_path(DISCOVERY_CACHE)
    if not p.exists():
        return {"build": BUILD, "queries": {}, "by_product": {}}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"build": BUILD, "queries": {}, "by_product": {}}


def _save_cache(payload: dict[str, Any]) -> None:
    p = data_path(DISCOVERY_CACHE)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload["build"] = BUILD
    payload["updated_at"] = time.time()
    p.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def build_queries(mpn: str, manufacturer: str | None = None) -> list[str]:
    mpn = (mpn or "").strip()
    mfr = (manufacturer or "").strip()
    if not mpn:
        return []
    qs = [
        f'"{mpn}"',
        f'{mfr} "{mpn}"'.strip(),
        f'"{mpn}" price',
        f'"{mpn}" buy',
        f'"{mpn}" distributor',
        f'"{mpn}" supplier',
        f'{mfr} "{mpn}" buy price'.strip(),
    ]
    # Prefer known open distributors via site: queries (Tier A first, then preferred list)
    for site in list(SEED_TIER_A) + list(PREFERRED_OPEN_DISTRIBUTORS):
        qs.append(f'site:{site} "{mpn}"')
    # Dedup preserve order
    seen: set[str] = set()
    out: list[str] = []
    for q in qs:
        q = re.sub(r"\s+", " ", q).strip()
        if q and q not in seen:
            seen.add(q)
            out.append(q)
    return out


def _reject_host(host: str) -> bool:
    h = (host or "").lower()
    return any(x in h for x in REJECT_HOST_FRAGMENTS)


def _identity_confidence(url: str, mpn: str) -> str:
    return classify_url(url, mpn=mpn)


def _search(query: str, *, limit: int = 8) -> list[dict[str, Any]]:
    cache = _load_cache()
    qcache = cache.setdefault("queries", {})
    if query in qcache and isinstance(qcache[query], list):
        return list(qcache[query])[:limit]
    results: list[dict[str, Any]] = []
    try:
        from public_price_search.search import search_web

        payload = search_web(query, limit=limit, use_budget=True)
        for r in payload.get("results") or []:
            url = r.get("url") or ""
            if not url.startswith("http"):
                continue
            results.append(
                {
                    "url": url,
                    "title": r.get("title") or "",
                    "snippet": r.get("snippet") or "",
                    "provider": r.get("provider"),
                }
            )
    except Exception:
        results = []
    # Fallback: playwright bing only if HTTP search empty (cached)
    if not results:
        try:
            from price_adapters.browser import bing_discover_urls

            for r in bing_discover_urls(query, limit=limit):
                results.append(
                    {
                        "url": r.get("url") or "",
                        "title": r.get("title") or "",
                        "snippet": "",
                        "provider": "bing_playwright",
                    }
                )
        except Exception:
            pass
    qcache[query] = results
    _save_cache(cache)
    return results[:limit]


def rediscover_exact_sellers(
    item: dict[str, Any],
    *,
    existing_urls: list[str] | None = None,
    max_queries: int = 6,
    max_urls: int = MAX_CANDIDATE_URLS,
) -> dict[str, Any]:
    """Discover alternate exact product seller URLs for an unpriced item.

    Search snippets are discovery-only — never final price evidence.
    """
    from price_adapters.orchestrate import _identity

    identity = _identity(item)
    mpn = str(identity.get("part_number") or item.get("mpn") or "").strip()
    mfr = str(identity.get("manufacturer") or item.get("manufacturer") or "").strip()
    bid = item.get("benchmark_id") or item.get("id")

    diag: dict[str, Any] = {
        "build": BUILD,
        "benchmark_id": bid,
        "mpn": mpn,
        "manufacturer": mfr,
        "queries": [],
        "discovered_exact_urls": [],
        "rejected": [],
        "n_new_exact": 0,
    }
    if not mpn:
        diag["error"] = "NO_MPN"
        return diag

    existing = {u.strip() for u in (existing_urls or []) if u}
    # Also reject known wrong DWT-6 page
    if bid == "easy-global-dwt-6":
        existing.add("https://www.globalindustrial.com/p/dwt-6")

    candidates: list[dict[str, Any]] = []
    seen_urls: set[str] = set(existing)

    # Run core queries + a few preferred site: probes (bounded — keep Stage C fast)
    queries = build_queries(mpn, mfr)
    core = [q for q in queries if not q.startswith("site:")][: min(max_queries, 5)]
    # Prefer Tier A / open distributors only for site probes
    site_q = [q for q in queries if q.startswith("site:")][:4]
    for query in core + site_q:
        rows = _search(query, limit=8)
        diag["queries"].append({"query": query, "n_results": len(rows)})
        for r in rows:
            url = (r.get("url") or "").strip()
            url = "".join(ch for ch in url if 32 <= ord(ch) < 127)
            if not url.startswith("http") or url in seen_urls:
                continue
            host = seller_of(url)
            if _reject_host(host):
                diag["rejected"].append({"url": url, "reason": "MARKETPLACE_OR_NON_SELLER"})
                continue
            if is_suppressed(host):
                diag["rejected"].append({"url": url, "reason": "LOW_YIELD_BLOCKED", "domain": host})
                continue
            conf = _identity_confidence(url, mpn)
            if conf in {SEARCH_RESULT_SHELL, CATEGORY_PAGE, WRONG_PRODUCT, PRODUCT_FAMILY_PAGE}:
                diag["rejected"].append({"url": url, "reason": conf})
                continue
            path = (urlparse(url).path or "").lower()
            if path.endswith(".pdf") or "/specsheet" in path or "/documents/" in path:
                diag["rejected"].append({"url": url, "reason": "NON_PRODUCT_DOCUMENT"})
                continue
            tok = re.sub(r"[^a-z0-9]", "", mpn.lower())
            path_tok = re.sub(r"[^a-z0-9]", "", path)
            blob = f"{r.get('title') or ''} {r.get('snippet') or ''}"
            blob_tok = re.sub(r"[^a-z0-9]", "", blob.lower())
            # Require MPN in path OR title/snippet
            if tok and tok not in path_tok and tok not in blob_tok:
                diag["rejected"].append({"url": url, "reason": "NO_MPN_SIGNAL"})
                continue
            # Manufacturer soft check when available (avoid fashion/color collisions)
            if mfr and conf != EXACT_PRODUCT_VERIFIED:
                mfr_tok = re.sub(r"[^a-z0-9]", "", mfr.lower())
                if len(mfr_tok) >= 4 and mfr_tok not in blob_tok and mfr_tok not in path_tok:
                    # Still allow preferred open distributors
                    if host not in PREFERRED_OPEN_DISTRIBUTORS and not any(
                        host.endswith(d) for d in PREFERRED_OPEN_DISTRIBUTORS
                    ):
                        diag["rejected"].append({"url": url, "reason": "NO_MFR_SIGNAL"})
                        continue

            # DWT-6 special: reject DWT62 contamination
            if bid == "easy-global-dwt-6":
                if "dwt62" in path_tok or re.search(r"dwt-?62\b", path, re.I):
                    diag["rejected"].append({"url": url, "reason": "WRONG_VARIANT_DWT62"})
                    continue

            seen_urls.add(url)
            row = {
                "url": url,
                "seller": host,
                "domain": host,
                "identity_confidence": conf,
                "price_visibility_status": "UNKNOWN",
                "extraction_route_attempted": None,
                "validation_result": None,
                "discovery_query": query,
                "title": (r.get("title") or "")[:160],
                "from_seeded_seller": False,
            }
            candidates.append(row)

    ranked = rank_urls(candidates)[:max_urls]
    # Prefer verified
    verified = [c for c in ranked if c["identity_confidence"] == EXACT_PRODUCT_VERIFIED]
    unverified = [c for c in ranked if c["identity_confidence"] != EXACT_PRODUCT_VERIFIED]
    ordered = verified + unverified
    diag["discovered_exact_urls"] = ordered[:max_urls]
    diag["n_new_exact"] = len(diag["discovered_exact_urls"])
    diag["n_verified"] = len(verified)
    diag["tier_mix"] = {
        "A": sum(1 for c in ordered if tier_rank(c["domain"]) == 0),
        "B": sum(1 for c in ordered if tier_rank(c["domain"]) == 1),
        "C": sum(1 for c in ordered if tier_rank(c["domain"]) == 2),
        "D": sum(1 for c in ordered if tier_rank(c["domain"]) == 3),
    }

    # Persist per-product
    cache = _load_cache()
    if bid:
        cache.setdefault("by_product", {})[str(bid)] = {
            "mpn": mpn,
            "discovered": diag["discovered_exact_urls"],
            "updated_at": time.time(),
        }
        _save_cache(cache)
    return diag
