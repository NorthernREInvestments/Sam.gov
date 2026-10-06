"""Alternate-seller discovery: queries, domain diversity, PDP candidates."""

from __future__ import annotations

import time
from typing import Any

from exact_product_url_discovery.classify import is_search_shell
from exact_product_url_discovery.search_convert import convert_internal_search
from manufacturer_distributor_graph.exact_urls import curated_exact_urls
from open_seller_expansion.fingerprint import build_fingerprint, try_upc_from_identity_url
from open_seller_expansion.models import (
    IDENTITY_REFERENCE_DOMAINS,
    ITEM_DEADLINE_S,
    JUNK_SERP_DOMAINS,
    MAX_QUERIES_PER_ITEM,
    MAX_SELLER_DOMAINS,
    MAX_VALIDATE_PER_ITEM,
    OPEN_ALT_PDPS,
)
from open_seller_expansion.pools import host_of, is_identity_reference, seller_pool_for_item
from open_web_product_discovery.result_filter import classify_search_hit, is_allowed_candidate
from open_web_product_discovery.transport import search_open_web, site_query
from price_adapters.validate import seller_of


def _is_junk_domain(domain: str) -> bool:
    d = host_of(domain)
    if d in JUNK_SERP_DOMAINS:
        return True
    # suffix match for nested junk (e.g. futurama.fandom.com already listed)
    return any(d.endswith("." + j) or d == j for j in JUNK_SERP_DOMAINS)


def _add(bag: list[dict[str, Any]], seen: set[str], row: dict[str, Any]) -> None:
    url = row.get("url") or ""
    if not url.startswith("http") or url in seen or is_search_shell(url):
        return
    if _is_junk_domain(seller_of(url)):
        return
    seen.add(url)
    bag.append(row)


def build_alternate_queries(item: dict[str, Any], fp: dict[str, Any]) -> list[dict[str, str]]:
    mpn = fp.get("mpn") or ""
    mfr = fp.get("manufacturer") or ""
    title = fp.get("canonical_title") or fp.get("exact_title") or ""
    upc = fp.get("upc") or fp.get("gtin") or ""
    rows: list[dict[str, str]] = []
    if mfr and mpn:
        rows.append({"type": "manufacturer_mpn", "query": f'{mfr} "{mpn}"'})
    if mpn:
        rows.append({"type": "quoted_mpn", "query": f'"{mpn}"'})
        rows.append({"type": "mpn_price", "query": f'{mfr} "{mpn}" price'.strip()})
        rows.append({"type": "mpn_buy", "query": f'{mfr} "{mpn}" buy'.strip()})
        rows.append({"type": "mpn_distributor", "query": f'{mfr} "{mpn}" distributor'.strip()})
        rows.append({"type": "mpn_supplier", "query": f'{mfr} "{mpn}" supplier'.strip()})
    if upc:
        rows.append({"type": "upc", "query": f'"{upc}"'})
        if mfr:
            rows.append({"type": "upc_mfr", "query": f'{mfr} {upc}'})
    if title and len(title) > 8:
        rows.append({"type": "exact_title", "query": f'"{title[:80]}"'})
        if mfr:
            rows.append({"type": "mfr_title", "query": f'{mfr} {title[:60]}'})
    # site-targeted alternate sellers
    for dom in seller_pool_for_item(item)[:MAX_SELLER_DOMAINS]:
        rows.append({"type": "site_alt", "query": site_query(dom, mpn, mfr or None), "domain": dom})
    seen: set[str] = set()
    out = []
    for r in rows:
        q = r["query"]
        if q in seen:
            continue
        seen.add(q)
        out.append(r)
        if len(out) >= MAX_QUERIES_PER_ITEM + 8:
            break
    return out


def discover_alternate_sellers(item: dict[str, Any], *, deadline: float | None = None) -> dict[str, Any]:
    """Find diverse alternate seller PDP candidates (skip identity-reference price targets)."""
    started = time.time()
    if deadline is None:
        deadline = started + ITEM_DEADLINE_S

    fp = build_fingerprint(item)
    mpn = str(fp.get("mpn") or "")
    mfr = str(fp.get("manufacturer") or "")

    candidates: list[dict[str, Any]] = []
    identity_refs: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen: set[str] = set()
    domains_seen: set[str] = set()
    searches = 0
    upc_seller_hit = False

    # 0 High-confidence open alternate PDPs first
    for key in (mpn, mpn.upper()):
        for u in OPEN_ALT_PDPS.get(key) or []:
            dom = seller_of(u)
            if is_identity_reference(dom) or _is_junk_domain(dom):
                continue
            _add(
                candidates,
                seen,
                {
                    "url": u,
                    "domain": dom,
                    "discovery_method": "OPEN_ALT_PDP",
                    "confidence": 0.9,
                    "source": "open_alt",
                    "_curated_open": True,
                },
            )
            domains_seen.add(host_of(dom))

    # 1 Curated URLs — split identity-ref vs priceable
    for u in curated_exact_urls(mpn) or []:
        dom = seller_of(u)
        if is_identity_reference(dom) or host_of(dom) in IDENTITY_REFERENCE_DOMAINS:
            identity_refs.append({"url": u, "domain": dom, "role": "IDENTITY_REFERENCE"})
            continue
        if is_search_shell(u) or _is_junk_domain(dom):
            rejected.append({"url": u, "reason": "search_shell_or_junk", "source": "curated"})
            continue
        _add(
            candidates,
            seen,
            {
                "url": u,
                "domain": dom,
                "discovery_method": "CURATED_ALT",
                "confidence": 0.8,
                "source": "curated",
                "_curated_open": True,
            },
        )
        domains_seen.add(host_of(dom))

    # 2 Light UPC enrichment from first identity ref (no price extract)
    if identity_refs and time.time() < deadline and not fp.get("upc"):
        fp = try_upc_from_identity_url(identity_refs[0]["url"], fp)

    # 3 Internal search on alternate seller pool
    pool = seller_pool_for_item(item)
    for dom in pool[:6]:
        if time.time() > deadline or len(candidates) >= MAX_VALIDATE_PER_ITEM * 2:
            break
        for row in convert_internal_search(dom, mpn=mpn, manufacturer=mfr, limit=2):
            url = row.get("url") or ""
            if is_identity_reference(seller_of(url)):
                continue
            _add(
                candidates,
                seen,
                {
                    "url": url,
                    "domain": host_of(dom),
                    "discovery_method": "ALT_INTERNAL_SEARCH",
                    "confidence": row.get("confidence", 0.55),
                    "source": "seller_internal",
                },
            )
            domains_seen.add(host_of(dom))

    # 4 Open-web / Bing Playwright — only if we still need domain diversity / PDPs
    need_search = len([c for c in candidates if c.get("_curated_open")]) < 2
    if need_search or len(domains_seen) < 3:
        queries = build_alternate_queries(item, fp)
        # Prefer site-alt + manufacturer_mpn first
        queries = sorted(
            queries,
            key=lambda q: 0 if q.get("type") in {"site_alt", "manufacturer_mpn", "upc", "mpn_buy"} else 1,
        )
        for qrow in queries:
            if time.time() > deadline:
                break
            if len([c for c in candidates if c.get("_curated_open")]) >= 2 and len(domains_seen) >= 4:
                break
            if len(domains_seen) >= MAX_SELLER_DOMAINS and len(candidates) >= MAX_VALIDATE_PER_ITEM:
                break
            prefer_pw = qrow.get("type") in {"manufacturer_mpn", "quoted_mpn", "upc", "mpn_buy", "site_alt"}
            res = search_open_web(qrow["query"], limit=6, prefer_playwright=prefer_pw)
            searches += 1
            for hit in res.get("results") or []:
                url = hit.get("url") or ""
                dom = seller_of(url)
                if is_identity_reference(dom) or _is_junk_domain(dom):
                    if is_identity_reference(dom):
                        identity_refs.append(
                            {"url": url, "domain": dom, "role": "IDENTITY_REFERENCE", "query_type": qrow["type"]}
                        )
                        if not fp.get("upc") and time.time() < deadline:
                            fp = try_upc_from_identity_url(url, fp)
                    else:
                        rejected.append({"url": url, "reason": "junk_serp", "source": "search"})
                    continue
                cls = classify_search_hit(
                    url=url,
                    title=hit.get("title") or "",
                    snippet=hit.get("snippet") or "",
                    mpn=mpn,
                    manufacturer=mfr,
                )
                if fp.get("short_mpn") and mfr:
                    title_l = (hit.get("title") or "").lower()
                    if mfr.split()[0].lower() not in title_l and mfr.split()[0].lower() not in url.lower():
                        rejected.append({"url": url, "reason": "short_mpn_no_mfr", "source": "search"})
                        continue
                if not is_allowed_candidate(cls):
                    rejected.append(
                        {
                            "url": url,
                            "reason": cls.get("result_type"),
                            "source": "search",
                            "query_type": qrow["type"],
                        }
                    )
                    continue
                conf = 0.65 if host_of(dom) not in domains_seen else 0.45
                _add(
                    candidates,
                    seen,
                    {
                        "url": url,
                        "domain": host_of(dom),
                        "discovery_method": f"ALT_SEARCH:{qrow['type']}",
                        "confidence": conf,
                        "source": "search",
                        "title": hit.get("title") or "",
                        "provider": res.get("provider_used"),
                    },
                )
                domains_seen.add(host_of(dom))
                if qrow.get("type") in {"upc", "upc_mfr"}:
                    upc_seller_hit = True

    # Rank: prefer new domains + higher confidence + mpn in url
    def _rank(c: dict[str, Any]) -> tuple:
        url = (c.get("url") or "").lower()
        return (
            -float(c.get("confidence") or 0),
            0 if mpn.lower().replace("-", "") in url.replace("-", "") else 1,
            c.get("domain") or "",
        )

    candidates.sort(key=_rank)
    # Cap validate set with domain diversity
    selected: list[dict[str, Any]] = []
    sel_domains: set[str] = set()
    for c in candidates:
        d = host_of(c.get("domain") or "")
        if len(selected) >= MAX_VALIDATE_PER_ITEM:
            break
        n_same = sum(1 for x in selected if host_of(x.get("domain") or "") == d)
        # Prefer domain diversity: at most 2 URLs per domain once we have 3+ domains
        if n_same >= 2 and len(sel_domains) >= 3:
            continue
        selected.append(c)
        sel_domains.add(d)

    return {
        "fingerprint": fp,
        "candidates": selected,
        "all_candidates": candidates[:40],
        "identity_refs": identity_refs[:20],
        "rejected": rejected[:40],
        "searches": searches,
        "unique_domains": sorted(domains_seen),
        "n_unique_domains": len(domains_seen),
        "upc_recovered": bool(fp.get("upc") or fp.get("gtin")),
        "upc_seller_hit": upc_seller_hit,
        "seller_pool": pool,
        "elapsed_s": round(time.time() - started, 2),
    }


def MIN_DIVERSITY_SOFT() -> int:
    return 5
