"""Dump full BidNet table row + Boston views-row for parser fixtures."""
from __future__ import annotations

import re
from pathlib import Path

ART = Path(__file__).resolve().parents[1] / "artifacts"
t = (ART / "_probe_bidnet_il_open.html").read_text(encoding="utf-8", errors="ignore")

# Full first data row
m = re.search(r'<tr\s+data-index="0"[\s\S]*?</tr>', t, re.I)
if m:
    row = m.group(0)
    (ART / "fixtures_round2_bidnet_row.html").write_text(row, encoding="utf-8")
    print("ROW LEN", len(row))
    # Strip scripts for readability
    clean = re.sub(r"<script[\s\S]*?</script>", "", row, flags=re.I)
    print(clean[:3000])
    # Also get header
    th = re.search(r"<thead[\s\S]*?</thead>", t, re.I)
    if th:
        print("\nHEADER:\n", re.sub(r"<script[\s\S]*?</script>", "", th.group(0))[:2000])

# Grab 3 rows for fixture page
rows = re.findall(r'<tr\s+data-index="\d+"[\s\S]*?</tr>', t, re.I)[:5]
print("rows found", len(rows))
fixture = (
    "<html><body><table><thead><tr>"
    "<th>Title</th><th>Buyer</th><th>Closing</th><th>Status</th></tr></thead><tbody>"
    + "".join(re.sub(r"<script[\s\S]*?</script>", "", r, flags=re.I) for r in rows)
    + "</tbody></table>"
    '<a href="/illinois/solicitations/open-bids/page2">Next page</a>'
    "</body></html>"
)
(ART / "fixtures" / "round2_bidnet_open_bids.html").parent.mkdir(parents=True, exist_ok=True)
(ART / "fixtures" / "round2_bidnet_open_bids.html").write_text(fixture, encoding="utf-8")
print("wrote fixture", len(fixture))

# Boston views-row
b = (ART / "_probe_boston_bids.html").read_text(encoding="utf-8", errors="ignore")
vrows = re.findall(r'<div[^>]*class="[^"]*views-row[^"]*"[\s\S]*?(?=<div[^>]*class="[^"]*views-row|$)', b, re.I)
print("boston views-rows", len(vrows))
if vrows:
    clean_b = re.sub(r"<script[\s\S]*?</script>", "", vrows[0], flags=re.I)
    print(clean_b[:2500])
    (ART / "fixtures" / "round2_boston_bids.html").write_text(
        "<html><body>" + "".join(re.sub(r"<script[\s\S]*?</script>", "", r, flags=re.I) for r in vrows[:5]) + "</body></html>",
        encoding="utf-8",
    )
