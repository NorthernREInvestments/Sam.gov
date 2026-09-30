"""Recompute NE/WY attribution on saved live opportunity test artifacts."""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.run_m3_live_opportunity_test import _state_of, format_human_table

REPORT = ROOT / "artifacts" / "m3_live_opportunity_test_report.json"
NEW_PRODUCT_CATS = {
    "A_NEW_PRODUCT_ACTIONABLE",
    "B_NEW_PRODUCT_NEEDS_EVIDENCE",
    "C_NEW_PRODUCT_BLOCKED",
    "D_NEW_AMBIGUOUS_CLASSIFICATION",
}
PRODUCT_WORDS = (
    "supply",
    "supplies",
    "equipment",
    "parts",
    "hardware",
    "tool",
    "material",
    "consumable",
    "glove",
    "cement",
    "furniture",
    "computer",
    "laptop",
    "pump",
    "motor",
    "filter",
    "safety",
    "nsn",
    "footwear",
    "clothing",
    "washer",
    "chair",
    "fencing",
)


def main() -> None:
    r = json.loads(REPORT.read_text(encoding="utf-8"))
    cards = r.get("new_opportunity_cards") or []
    for c in cards:
        c["state"] = _state_of(
            {
                "state_code": None,
                "source_id": c.get("source"),
                "agency": c.get("agency"),
                "title": c.get("title"),
                "detail_url": c.get("source_url"),
            }
        )

    ne = [c for c in cards if c.get("state") == "NE"]
    wy = [c for c in cards if c.get("state") == "WY"]
    a = [c for c in cards if c.get("category") == "A_NEW_PRODUCT_ACTIONABLE"]

    def slim(rows):
        return [
            {
                k: c.get(k)
                for k in (
                    "opportunity_id",
                    "title",
                    "agency",
                    "state",
                    "source",
                    "source_url",
                    "deadline",
                    "classification",
                    "category",
                )
            }
            for c in rows
        ]

    r["nebraska"] = {
        "new_product_opportunities_found": len(ne),
        "by_category": dict(Counter(c["category"] for c in ne)),
        "actionable": slim([c for c in ne if c.get("category") == "A_NEW_PRODUCT_ACTIONABLE"]),
        "note": "Corrected state attribution; NEW product cards only",
    }
    r["wyoming"] = {
        "new_product_opportunities_found": len(wy),
        "by_category": dict(Counter(c["category"] for c in wy)),
        "actionable": slim([c for c in wy if c.get("category") == "A_NEW_PRODUCT_ACTIONABLE"]),
        "note": "Corrected state attribution; NEW product cards only",
    }
    r["new_opportunity_cards"] = cards
    r["state_attribution_corrected"] = True

    productish_d = [
        c
        for c in cards
        if c.get("category") == "D_NEW_AMBIGUOUS_CLASSIFICATION"
        and any(w in (c.get("title") or "").lower() for w in PRODUCT_WORDS)
    ]
    ordered = []
    seen = set()
    for c in a + productish_d:
        key = c.get("opportunity_id") or c.get("title")
        if key in seen:
            continue
        seen.add(key)
        ordered.append(c)

    (ROOT / "artifacts" / "m3_live_opportunity_test_new_opportunities.md").write_text(
        format_human_table(ordered[:80]), encoding="utf-8"
    )
    (ROOT / "artifacts" / "m3_live_opportunity_test_actionable.md").write_text(
        format_human_table(a), encoding="utf-8"
    )
    REPORT.write_text(json.dumps(r, indent=2, default=str), encoding="utf-8")

    print("NE", r["nebraska"]["new_product_opportunities_found"], r["nebraska"]["by_category"])
    print("WY", r["wyoming"]["new_product_opportunities_found"], r["wyoming"]["by_category"])
    print("A_total", len(a), "by_state", dict(Counter(c.get("state") for c in a)))
    print("prioritized_human", min(80, len(ordered)))
    for c in a[:25]:
        title = (c.get("title") or "")[:72]
        print(f"A|{c.get('state')}|{title}|{c.get('source')}|{c.get('deadline')}")


if __name__ == "__main__":
    main()
