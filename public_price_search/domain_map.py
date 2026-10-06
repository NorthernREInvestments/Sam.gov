"""Reusable distributor/reseller domain map by product category.

Build: 20261004-m3-price-search-reliability-v1
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote_plus

# Category → public seller search templates (exact MPN substituted)
CATEGORY_SELLERS: dict[str, list[str]] = {
    "automotive_heavy": [
        "https://www.fleetpride.com/search?q={q}",
        "https://www.dieselpartsdirect.com/search?q={q}",
        "https://www.thedieselstore.com/search?type=product&q={q}",
        "https://www.alliantpower.com/search?q={q}",
        "https://www.finditparts.com/search?q={q}",
        "https://advancedtruckparts.com/search?q={q}",
        "https://www.bigrigworld.com/search?q={q}",
    ],
    "mro": [
        "https://www.grainger.com/search?searchQuery={q}",
        "https://www.zoro.com/search?q={q}",
        "https://www.mscdirect.com/browse/tn/?searchterm={q}",
        "https://www.fastenal.com/product/search?term={q}",
        "https://www.motionindustries.com/products/search?q={q}",
        "https://www.globalindustrial.com/search?q={q}",
    ],
    "plumbing": [
        "https://www.supplyhouse.com/search?q={q}",
        "https://www.ferguson.com/search?q={q}",
        "https://www.grainger.com/search?searchQuery={q}",
        "https://www.zoro.com/search?q={q}",
    ],
    "electrical": [
        "https://www.grainger.com/search?searchQuery={q}",
        "https://www.zoro.com/search?q={q}",
        "https://www.platt.com/search?q={q}",
        "https://www.rexelusa.com/search?text={q}",
    ],
    "tools": [
        "https://www.grainger.com/search?searchQuery={q}",
        "https://www.zoro.com/search?q={q}",
        "https://www.mscdirect.com/browse/tn/?searchterm={q}",
        "https://www.globalindustrial.com/search?q={q}",
    ],
    "office": [
        "https://www.staples.com/search?query={q}",
        "https://www.officedepot.com/catalog/search.do?Ntt={q}",
        "https://www.quill.com/search?keywords={q}",
    ],
    "ppe": [
        "https://www.grainger.com/search?searchQuery={q}",
        "https://www.zoro.com/search?q={q}",
        "https://www.globalindustrial.com/search?q={q}",
    ],
    "industrial": [
        "https://www.grainger.com/search?searchQuery={q}",
        "https://www.zoro.com/search?q={q}",
        "https://www.mscdirect.com/browse/tn/?searchterm={q}",
        "https://www.globalindustrial.com/search?q={q}",
        "https://www.fastenal.com/product/search?term={q}",
    ],
    "hvac": [
        "https://www.supplyhouse.com/search?q={q}",
        "https://www.grainger.com/search?searchQuery={q}",
        "https://www.zoro.com/search?q={q}",
    ],
    "lighting": [
        "https://www.grainger.com/search?searchQuery={q}",
        "https://www.zoro.com/search?q={q}",
        "https://www.1000bulbs.com/search?q={q}",
    ],
    "furniture": [
        "https://www.globalindustrial.com/search?q={q}",
        "https://www.staples.com/search?query={q}",
        "https://www.officedepot.com/catalog/search.do?Ntt={q}",
    ],
}

_BRAND_CATEGORY: dict[str, str] = {
    "CUMMINS": "automotive_heavy",
    "FLEETGUARD": "automotive_heavy",
    "CATERPILLAR": "automotive_heavy",
    "CAT": "automotive_heavy",
    "FORD": "automotive_heavy",
    "JOHN DEERE": "automotive_heavy",
    "ALLIANT": "automotive_heavy",
    "SMITH-BLAIR": "plumbing",
    "SMITH BLAIR": "plumbing",
    "DYNAREX": "ppe",
    "3M": "ppe",
    "HONEYWELL": "ppe",
    "GRAINGER": "mro",
    "MILWAUKEE": "tools",
    "DEWALT": "tools",
    "MAKITA": "tools",
}

_DESC_HINTS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(?:injector|diesel|turbo|truck|engine|filter|fuel)\b", re.I), "automotive_heavy"),
    (re.compile(r"\b(?:pipe|valve|coupling|fitting|plumbing|gasket)\b", re.I), "plumbing"),
    (re.compile(r"\b(?:wire|breaker|conduit|electrical|panel)\b", re.I), "electrical"),
    (re.compile(r"\b(?:glove|mask|respirator|ppe|safety)\b", re.I), "ppe"),
    (re.compile(r"\b(?:hvac|thermostat|duct|furnace)\b", re.I), "hvac"),
    (re.compile(r"\b(?:lamp|led|ballast|fixture|lighting)\b", re.I), "lighting"),
    (re.compile(r"\b(?:desk|chair|cabinet|furniture)\b", re.I), "furniture"),
    (re.compile(r"\b(?:paper|toner|pen|office)\b", re.I), "office"),
    (re.compile(r"\b(?:drill|wrench|socket|tool)\b", re.I), "tools"),
]


def infer_category(identity: dict[str, Any]) -> str:
    mfr = str(identity.get("manufacturer") or identity.get("brand") or "").upper()
    for k, cat in _BRAND_CATEGORY.items():
        if k in mfr:
            return cat
    blob = " ".join(
        str(identity.get(k) or "")
        for k in ("raw_description", "description", "part_number", "model")
    )
    for rx, cat in _DESC_HINTS:
        if rx.search(blob):
            return cat
    return "industrial"


def category_search_urls(identity: dict[str, Any], *, limit: int = 8) -> list[str]:
    pn = str(
        identity.get("part_number")
        or identity.get("catalog_number")
        or identity.get("sku")
        or identity.get("model")
        or ""
    ).strip()
    mfr = str(identity.get("manufacturer") or identity.get("brand") or "").strip()
    seed = f"{mfr} {pn}".strip() or pn
    if not seed:
        return []
    q = quote_plus(seed)
    cat = infer_category(identity)
    templates = list(CATEGORY_SELLERS.get(cat) or []) + list(CATEGORY_SELLERS["mro"])
    out: list[str] = []
    seen: set[str] = set()
    for t in templates:
        u = t.format(q=q)
        if u not in seen:
            seen.add(u)
            out.append(u)
        if len(out) >= limit:
            break
    return out
