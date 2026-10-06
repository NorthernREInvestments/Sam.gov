"""Seller pools, memory, and identity-reference gating."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

from m3_data_root import data_path
from open_seller_expansion.models import (
    CATEGORY_SELLER_POOLS,
    IDENTITY_REFERENCE_DOMAINS,
    MANUFACTURER_SELLER_POOLS,
    SELLER_MEMORY,
)
from open_web_product_discovery.routing import resolve_category
from manufacturer_distributor_graph.manufacturer_seed import authorized_distributors, resolve_manufacturer


def host_of(url_or_host: str) -> str:
    s = (url_or_host or "").strip().lower()
    if "://" in s:
        s = urlparse(s).netloc
    return s.replace("www.", "").split(":")[0]


def is_identity_reference(domain: str) -> bool:
    return host_of(domain) in IDENTITY_REFERENCE_DOMAINS


def seller_pool_for_item(item: dict[str, Any]) -> list[str]:
    """Build ordered alternate-seller pool (manufacturer + category + authorized)."""
    mfr = str(item.get("manufacturer") or "").strip().upper()
    cat = resolve_category(item)
    out: list[str] = []
    seen: set[str] = set()

    def _add(dom: str) -> None:
        d = host_of(dom)
        if not d or d in seen or is_identity_reference(d):
            return
        seen.add(d)
        out.append(d)

    # Learned memory first
    for row in memory_domains_for(mfr, cat):
        _add(row)

    for key in (mfr, mfr.split()[0] if mfr else ""):
        for d in MANUFACTURER_SELLER_POOLS.get(key) or []:
            _add(d)

    for d in CATEGORY_SELLER_POOLS.get(cat) or CATEGORY_SELLER_POOLS["default"]:
        _add(d)

    try:
        mfr_res = resolve_manufacturer(item)
        for dist in authorized_distributors(mfr_res.get("manufacturer_key") or mfr)[:6]:
            if isinstance(dist, dict):
                _add(str(dist.get("distributor_domain") or dist.get("domain") or ""))
            elif isinstance(dist, tuple):
                _add(str(dist[0]))
            else:
                _add(str(dist))
    except Exception:
        pass

    return out


def _load_memory() -> dict[str, Any]:
    p = data_path(SELLER_MEMORY)
    if not p.exists():
        return {"sellers": {}, "build": "20261005-m3-open-seller-expansion-v1"}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"sellers": {}}


def _save_memory(db: dict[str, Any]) -> None:
    p = data_path(SELLER_MEMORY)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(db, indent=2, default=str), encoding="utf-8")


def memory_domains_for(manufacturer: str, category: str) -> list[str]:
    db = _load_memory()
    sellers = db.get("sellers") or {}
    scored: list[tuple[float, str]] = []
    for domain, row in sellers.items():
        if is_identity_reference(domain):
            continue
        mfrs = {str(x).upper() for x in (row.get("manufacturers") or [])}
        cats = {str(x).lower() for x in (row.get("categories") or [])}
        ok = False
        if manufacturer and manufacturer.upper() in mfrs:
            ok = True
        if category and category.lower() in cats:
            ok = True
        if not ok:
            continue
        attempts = max(1, int(row.get("attempts") or 1))
        prices = int(row.get("prices") or 0)
        scored.append((prices / attempts, domain))
    scored.sort(key=lambda x: -x[0])
    return [d for _, d in scored]


def note_seller_outcome(
    *,
    domain: str,
    manufacturer: str,
    category: str,
    mpn: str,
    exact_pdp: bool,
    priced: bool,
    url_pattern: str | None = None,
    price_route: str | None = None,
    blocked: bool = False,
) -> None:
    domain = host_of(domain)
    if not domain:
        return
    db = _load_memory()
    row = db.setdefault("sellers", {}).setdefault(
        domain,
        {
            "domain": domain,
            "manufacturers": [],
            "categories": [],
            "mpns": [],
            "attempts": 0,
            "exact_pdps": 0,
            "prices": 0,
            "block_count": 0,
            "url_patterns": [],
            "price_routes": [],
        },
    )
    row["attempts"] = int(row.get("attempts") or 0) + 1
    if exact_pdp:
        row["exact_pdps"] = int(row.get("exact_pdps") or 0) + 1
    if priced:
        row["prices"] = int(row.get("prices") or 0) + 1
    if blocked:
        row["block_count"] = int(row.get("block_count") or 0) + 1
    if manufacturer and manufacturer not in row["manufacturers"]:
        row["manufacturers"].append(manufacturer)
    if category and category not in row["categories"]:
        row["categories"].append(category)
    if mpn and mpn not in row["mpns"]:
        row["mpns"].append(mpn)
        row["mpns"] = row["mpns"][-40:]
    if url_pattern and url_pattern not in row["url_patterns"]:
        row["url_patterns"].append(url_pattern)
        row["url_patterns"] = row["url_patterns"][-10:]
    if price_route and price_route not in row["price_routes"]:
        row["price_routes"].append(price_route)
    attempts = max(1, int(row["attempts"]))
    row["success_rate"] = round(100.0 * int(row.get("prices") or 0) / attempts, 1)
    row["block_rate"] = round(100.0 * int(row.get("block_count") or 0) / attempts, 1)
    from application_clock import now_utc

    row["last_verified"] = now_utc().isoformat()
    _save_memory(db)


def seller_memory_snapshot(top_n: int = 30) -> list[dict[str, Any]]:
    db = _load_memory()
    rows = list((db.get("sellers") or {}).values())
    rows.sort(key=lambda r: (-int(r.get("prices") or 0), -int(r.get("exact_pdps") or 0)))
    return rows[:top_n]
