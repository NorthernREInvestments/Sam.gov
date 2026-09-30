"""Build operator shortlist markdown from live opportunity report."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
r = json.loads((ROOT / "artifacts" / "m3_live_opportunity_test_report.json").read_text(encoding="utf-8"))
cards = [c for c in r["new_opportunity_cards"] if c.get("category") == "A_NEW_PRODUCT_ACTIONABLE"]
prefer, demote = [], []
bad = ("study", "repair", "maintenance", "services", "resurfacing", "painting", "rental", "implementation")
keep = ("supplies", "parts", "equipment", "hardware", "materials", "purchase of", "furnish", "kiln", "welding", "sign ")
for c in cards:
    t = (c.get("title") or "").lower()
    if any(b in t for b in bad) and not any(p in t for p in keep):
        demote.append(c)
    else:
        prefer.append(c)
out = prefer + demote
lines = ["# A. NEW + PRODUCT + ACTIONABLE (operator shortlist)", ""]
for i, c in enumerate(out, 1):
    lines.append(f"{i}. {c.get('title')}")
    lines.append(f"   Agency: {c.get('agency')} | Source: {c.get('source')}")
    lines.append(f"   Deadline: {c.get('deadline')} (runway={c.get('deadline_runway')})")
    lines.append(f"   URL: {c.get('source_url')}")
    lines.append(f"   Class: {c.get('classification')} | Pursuit: {c.get('pursuit_readiness')}")
    lines.append("")
(ROOT / "artifacts" / "m3_live_opportunity_test_actionable_shortlist.md").write_text("\n".join(lines), encoding="utf-8")
print("shortlist", len(out), "prefer", len(prefer))
for c in r["new_opportunity_cards"]:
    if c.get("state") in {"NE", "WY"}:
        print(c.get("state"), (c.get("title") or "")[:90], c.get("deadline"), c.get("source"))
