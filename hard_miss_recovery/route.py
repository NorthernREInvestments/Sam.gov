"""Hard-miss primary recovery strategy classification."""

from __future__ import annotations

import re
from typing import Any

from hard_miss_recovery.models import (
    DEAD_PRIMARY,
    SHORT_MPN_IDS,
    STRAT_AUTH_DIST,
    STRAT_EXACT_URL,
    STRAT_FAMILY,
    STRAT_OTHER,
    STRAT_PACK_UOM,
    STRAT_SHORT_MPN,
    STRAT_SPECIALIST,
)


def classify_strategy(item: dict[str, Any], attempted_domains: list[str] | None = None) -> str:
    mpn = str(item.get("mpn") or item.get("part_number") or "")
    tok = re.sub(r"[^A-Za-z0-9]", "", mpn).upper()
    cat = str(item.get("category") or "").lower()
    bid = str(item.get("benchmark_id") or "")
    known = str(item.get("known_public_seller") or "").lower().replace("www.", "")
    domains = [d.lower().replace("www.", "") for d in (attempted_domains or [])]

    if tok in SHORT_MPN_IDS or (len(tok) < 6 and sum(ch.isdigit() for ch in tok) >= 3):
        return STRAT_SHORT_MPN
    if "oatey" in bid or "gallon" in str(item.get("description") or "").lower():
        return STRAT_PACK_UOM
    if known in DEAD_PRIMARY or any(d in DEAD_PRIMARY for d in domains[:5]):
        # Prefer specialist / authorized over re-hitting dead primary
        if cat in {"plumbing", "electrical", "hvac", "auto", "ppe", "tools", "lighting"}:
            return STRAT_SPECIALIST
        return STRAT_AUTH_DIST
    if bid in {"easy-global-dwt-6", "ind-global-wb218294"}:
        return STRAT_FAMILY
    if known and known not in DEAD_PRIMARY:
        return STRAT_EXACT_URL
    if cat:
        return STRAT_SPECIALIST
    return STRAT_OTHER


def specialist_domains_for(item: dict[str, Any]) -> list[str]:
    from hard_miss_recovery.models import PROVEN_OPEN, SPECIALISTS

    cat = str(item.get("category") or "").lower()
    out: list[str] = []
    for d in SPECIALISTS.get(cat, ()):
        if d not in DEAD_PRIMARY and d not in out:
            out.append(d)
    for d in PROVEN_OPEN:
        if d not in DEAD_PRIMARY and d not in out:
            out.append(d)
    # Manufacturer seed distributors
    try:
        from manufacturer_distributor_graph.manufacturer_seed import authorized_distributors, resolve_manufacturer

        mfr = resolve_manufacturer(item)
        for row in authorized_distributors(mfr.get("manufacturer_key") or item.get("manufacturer")) or []:
            dom = str(row.get("distributor_domain") or "").lower().replace("www.", "")
            if dom and dom not in DEAD_PRIMARY and dom not in out:
                out.append(dom)
    except Exception:
        pass
    return out[:12]
