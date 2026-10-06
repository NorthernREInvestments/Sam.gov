"""Category → high-yield seller templates (learned + seeded).

Build: 20261004-m3-full100-throughput-v1
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote_plus

from full100_throughput.domain_intel import rank_domains

# Template uses {q} = manufacturer+mpn, {pn} = mpn lower, {PN} = raw mpn
_CATEGORY_SELLERS: dict[str, list[tuple[str, str]]] = {
    "mro": [
        ("quill.com", "https://www.quill.com/search?keywords={q}"),
        ("motion.com", "https://www.motion.com/products/search?q={q}"),
        ("mscdirect.com", "https://www.mscdirect.com/browse/tn/?searchterm={q}"),
        ("globalindustrial.com", "https://www.globalindustrial.com/search?q={q}"),
    ],
    "industrial": [
        ("quill.com", "https://www.quill.com/search?keywords={q}"),
        ("motion.com", "https://www.motion.com/products/search?q={q}"),
        ("mscdirect.com", "https://www.mscdirect.com/browse/tn/?searchterm={q}"),
        ("globalindustrial.com", "https://www.globalindustrial.com/search?q={q}"),
    ],
    "tools": [
        ("motion.com", "https://www.motion.com/products/search?q={q}"),
        ("quill.com", "https://www.quill.com/search?keywords={q}"),
        ("homedepot.com", "https://www.homedepot.com/s/{q}"),
    ],
    "plumbing": [
        ("mccoys.com", "https://www.mccoys.com/search?q={pn}"),
        ("parts-hvac.com", "https://parts-hvac.com/search?q={q}"),
        ("homedepot.com", "https://www.homedepot.com/s/{q}"),
        ("lowes.com", "https://www.lowes.com/search?searchTerm={q}"),
    ],
    "hvac": [
        ("parts-hvac.com", "https://parts-hvac.com/search?q={q}"),
        ("mccoys.com", "https://www.mccoys.com/search?q={pn}"),
        ("supplyhouse.com", "https://www.supplyhouse.com/search?q={pn}"),
    ],
    "automotive_heavy": [
        ("dieselpartsdirect.com", "https://www.dieselpartsdirect.com/{pn}"),
        ("dieselpartsdirect.com", "https://www.dieselpartsdirect.com/search?q={q}"),
        ("thedieselstore.com", "https://www.thedieselstore.com/search?type=product&q={q}"),
        ("maxtran.com", "https://maxtran.com/?s={q}"),
        ("autobuffy.com", "https://autobuffy.com/?s={q}"),
    ],
    "office": [
        ("quill.com", "https://www.quill.com/search?keywords={q}"),
        ("staples.com", "https://www.staples.com/search?query={q}"),
        ("officedepot.com", "https://www.officedepot.com/catalog/search.do?Ntt={q}"),
        ("brother-usa.com", "https://www.brother-usa.com/search?q={pn}"),
    ],
    "lighting": [
        ("1000bulbs.com", "https://www.1000bulbs.com/search?q={q}"),
        ("quill.com", "https://www.quill.com/search?keywords={q}"),
    ],
    "electrical": [
        ("rspsupply.com", "https://rspsupply.com/search.aspx?SearchTerm={q}"),
        ("1000bulbs.com", "https://www.1000bulbs.com/search?q={q}"),
        ("platt.com", "https://www.platt.com/search?q={q}"),
        ("quill.com", "https://www.quill.com/search?keywords={q}"),
    ],
    "ppe": [
        ("quill.com", "https://www.quill.com/search?keywords={q}"),
        ("mscdirect.com", "https://www.mscdirect.com/browse/tn/?searchterm={q}"),
    ],
    "furniture": [
        ("globalindustrial.com", "https://www.globalindustrial.com/search?q={q}"),
        ("quill.com", "https://www.quill.com/search?keywords={q}"),
    ],
}

_ALWAYS = [
    ("quill.com", "https://www.quill.com/search?keywords={q}"),
    ("dieselpartsdirect.com", "https://www.dieselpartsdirect.com/{pn}"),
    ("motion.com", "https://www.motion.com/products/search?q={q}"),
]


def seller_candidates_for_item(item: dict[str, Any], *, limit: int = 5) -> list[tuple[str, str]]:
    cat = (item.get("category") or "mro").lower()
    pn = str(item.get("mpn") or item.get("part_number") or "")
    mfr = str(item.get("manufacturer") or "")
    q = quote_plus(f"{mfr} {pn}".strip())
    pn_l = quote_plus(pn.lower())
    rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    for domain, tmpl in list(_CATEGORY_SELLERS.get(cat) or []) + list(_ALWAYS):
        if domain in seen:
            continue
        seen.add(domain)
        url = tmpl.replace("{q}", q).replace("{pn}", pn.lower()).replace("{PN}", pn)
        rows.append((domain, url))
    # Rank by historical yield
    ranked_domains = rank_domains([d for d, _ in rows])
    order = {d: i for i, d in enumerate(ranked_domains)}
    rows.sort(key=lambda t: order.get(t[0], 999))
    return rows[:limit]
