"""Category specialist routing + query generation."""

from __future__ import annotations

import re
from typing import Any

from open_web_product_discovery.models import CATEGORY_DOMAINS, MAX_QUERIES_PER_ITEM
from open_web_product_discovery.transport import site_query
from public_price_search.domain_map import infer_category


def resolve_category(item: dict[str, Any]) -> str:
    cat = str(item.get("category") or "").strip().lower()
    mapping = {
        "electrical": "electrical",
        "tools": "tools",
        "plumbing": "plumbing",
        "hvac": "hvac",
        "lighting": "lighting",
        "office": "office",
        "ppe": "ppe",
        "mro": "mro",
        "industrial": "industrial",
        "furniture": "furniture",
        "auto": "automotive_heavy",
        "automotive": "automotive_heavy",
    }
    if cat in mapping:
        return mapping[cat]
    try:
        c2 = infer_category(item)
        if c2:
            return c2
    except Exception:
        pass
    bid = str(item.get("benchmark_id") or "")
    prefix = bid.split("-")[0] if bid else ""
    return {
        "elec": "electrical",
        "tool": "tools",
        "plumb": "plumbing",
        "hvac": "hvac",
        "light": "lighting",
        "office": "office",
        "ppe": "ppe",
        "mro": "mro",
        "ind": "industrial",
        "furn": "furniture",
        "auto": "automotive_heavy",
    }.get(prefix, "default")


def preferred_domains(item: dict[str, Any]) -> list[str]:
    cat = resolve_category(item)
    domains = list(CATEGORY_DOMAINS.get(cat) or CATEGORY_DOMAINS["default"])
    known = str(item.get("known_public_seller") or "").lower().replace("www.", "")
    if known and known not in domains:
        domains.insert(0, known)
    # dedupe
    seen: set[str] = set()
    out = []
    for d in domains:
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    return out


def build_query_set(item: dict[str, Any]) -> list[dict[str, str]]:
    """Generate ordered discovery queries with type labels."""
    mpn = str(item.get("mpn") or item.get("part_number") or "").strip()
    mfr = str(item.get("manufacturer") or "").strip()
    rows: list[dict[str, str]] = []
    if mfr and mpn:
        rows.append({"type": "manufacturer_mpn", "query": f'{mfr} "{mpn}"'})
    if mpn:
        rows.append({"type": "exact_mpn", "query": f'"{mpn}"'})
        rows.append({"type": "mpn_product", "query": f'"{mpn}" product'})
        rows.append({"type": "mpn_buy", "query": f'"{mpn}" buy'})
        rows.append({"type": "mpn_where_to_buy", "query": f'"{mpn}" "where to buy"'})
        rows.append({"type": "mpn_distributor", "query": f'"{mpn}" distributor'})
        rows.append({"type": "mpn_dealer", "query": f'"{mpn}" dealer'})
    for dom in preferred_domains(item)[:4]:
        rows.append(
            {
                "type": "site_targeted",
                "query": site_query(dom, mpn, mfr or None),
                "domain": dom,
            }
        )
    # de-dupe queries
    seen: set[str] = set()
    out = []
    for r in rows:
        q = r["query"]
        if q in seen:
            continue
        seen.add(q)
        out.append(r)
        if len(out) >= MAX_QUERIES_PER_ITEM + 4:
            break
    return out
