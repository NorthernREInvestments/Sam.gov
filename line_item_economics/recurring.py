"""Recurring buyer intelligence (category repeat purchases)."""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

_CATEGORIES = {
    "toilet_paper": re.compile(r"toilet\s*paper|tissue\s*paper|bath\s*tissue", re.I),
    "tools": re.compile(r"\btools?\b|wrench|socket|drill|hammer", re.I),
    "furniture": re.compile(r"furniture|desk|chair|cubicle|workstation", re.I),
    "ppe": re.compile(r"\bppe\b|glove|respirator|safety\s*glass|hard\s*hat", re.I),
    "light_bulbs": re.compile(r"light\s*bulb|led\s*lamp|fluorescent|luminaire", re.I),
    "plumbing": re.compile(r"plumbing|pipe\b|fitting|valve|faucet|nipple|gasket", re.I),
    "janitorial": re.compile(r"janitor|cleaner|disinfectant|paper\s*towel|mop\b", re.I),
}


def categorize_text(text: str) -> list[str]:
    return [name for name, rx in _CATEGORIES.items() if rx.search(text or "")]


def recurring_buyer_intelligence(
    *,
    buyer: str | None,
    current_categories: list[str],
    history_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """history_rows: prior awards with buyer, category/title, qty, unit_price, bidder_count, award_date."""
    buyer_n = (buyer or "").strip().lower()
    by_cat: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in history_rows or []:
        b = str(row.get("buyer") or "").strip().lower()
        if buyer_n and b and b != buyer_n:
            continue
        cats = row.get("categories") or categorize_text(str(row.get("title") or row.get("category") or ""))
        for c in cats:
            by_cat[c].append(row)

    focus = []
    for cat in current_categories or list(by_cat.keys()):
        rows = by_cat.get(cat) or []
        if not rows:
            focus.append({"category": cat, "frequency": 0, "status": "NO_HISTORY"})
            continue
        qtys = [float(r["quantity"]) for r in rows if r.get("quantity") is not None]
        prices = [float(r["unit_price"]) for r in rows if r.get("unit_price") is not None]
        bidders = [int(r["bidder_count"]) for r in rows if r.get("bidder_count") is not None]
        focus.append(
            {
                "category": cat,
                "frequency": len(rows),
                "prior_quantities": qtys[:20],
                "prior_prices": prices[:20],
                "avg_unit_price": round(sum(prices) / len(prices), 4) if prices else None,
                "average_bidder_count": round(sum(bidders) / len(bidders), 2) if bidders else None,
                "award_dates": [r.get("award_date") for r in rows if r.get("award_date")][:10],
                "typical_award_timing": rows[0].get("award_timing"),
                "status": "RECURRING" if len(rows) >= 2 else "SEEN_ONCE",
            }
        )
    return {
        "buyer": buyer,
        "categories": focus,
        "prioritize_recurring_simple": any(
            (c.get("frequency") or 0) >= 2 and c.get("category") in {"janitorial", "plumbing", "ppe", "toilet_paper", "light_bulbs"}
            for c in focus
        ),
    }
