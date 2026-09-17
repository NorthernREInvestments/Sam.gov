"""Extract BidNet solicitation card HTML snippets for fixture design."""
from __future__ import annotations

import re
from pathlib import Path

ART = Path(__file__).resolve().parents[1] / "artifacts"
t = (ART / "_probe_bidnet_il_open.html").read_text(encoding="utf-8", errors="ignore")

# Find first solicitation-link context
for m in re.finditer(r'class="solicitation-link[^"]*"[^>]*>', t):
    start = max(0, m.start() - 800)
    end = min(len(t), m.end() + 1200)
    snippet = t[start:end]
    out = ART / "fixtures_round2_bidnet_card.html"
    # Prefer a real opportunity not nav
    if "abstract" in snippet or "statewide" in snippet:
        out.write_text(snippet, encoding="utf-8")
        print("wrote card snippet", len(snippet))
        print(snippet[:1500])
        break

# Also grab table rows if present
tables = re.findall(r"<table[\s\S]{200,5000}?</table>", t, re.I)
print("tables", len(tables))
if tables:
    (ART / "fixtures_round2_bidnet_table.html").write_text(tables[0][:8000], encoding="utf-8")
    print(tables[0][:2000])

# Boston
b = (ART / "_probe_boston_bids.html").read_text(encoding="utf-8", errors="ignore")
# look for bid list items
for pat in (r'class="[^"]*bid[^"]*"', r"views-row", r"node-", r"field-title"):
    print("boston", pat, len(re.findall(pat, b, re.I)))
# sample around bid-list
idx = b.lower().find("bid-list")
if idx >= 0:
    sn = b[max(0, idx - 200) : idx + 2500]
    (ART / "fixtures_round2_boston_bids.html").write_text(sn, encoding="utf-8")
    print("boston snippet:\n", sn[:2000])
