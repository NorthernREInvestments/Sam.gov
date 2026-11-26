# Build 002I Pre-Implementation / Root-Cause Audit

**Production at start:** `20261121-m3-2-build-002h` (rejected)  
**Independent validation:** live portal probes + code inspection (not prior-report-only)

## Why 18,023 raw → 752 individuals (confirmed)

| Layer | Finding |
|------:|---------|
| Raw discovery | Open-data / HTML harvest rows, including forecasts, awards, portal chrome, duplicate dataset mirrors, multi-source copies of the same LA RAMP feed |
| Identity (`identity.py`) | Correctly keeps only `INDIVIDUAL_SOLICITATION` |
| Dedup | Same solicitation retained once per identity key; raw count still rises when new seeds add non-individual rows |
| 002H fresh run | Individuals stayed **752** while discovery population rose to **18,023** — seed expansion without new Level-2/3 individual yield |

**Conclusion:** Raw-record growth is not market coverage. Further seed URL spam will not fix the engine.

## Why 752 → 2 purported M3 fits (002H) — and why those 2 fail (002I reaudit)

002H treated title parentheticals as ordered quantity:

| Solicitation | Expression | 002H claim | 002I semantic |
|---|---|---|---|
| LA RAMP 233012 Hydrant Pressure Recorder | `(300 UNIT)` | ordered qty 300 / Stage C | **UNKNOWN_NUMERIC_VALUE** — ambiguous; not affirmative ordered qty |
| LA RAMP 233123 Live Worms/Wax | `(250 PER CUP)` | ordered qty 250 / Stage C | **PACKAGE_QUANTITY** — per-cup packaging/capacity |

Both remain product-candidate listings; neither is a verified M3 procurement fit under quantity semantics.

## Software-controlled defects fixed in 002I

1. **Delaware detail URL misclassified as listing** — `classify_url_kind` matched `/bids/` listing marker before `/Bids/Details/{id}`. Inventory never verified; packages could not reach COMPLETE.
2. **Delaware attachments not discovered** — static detail HTML has no PDF hrefs; inventory is AJAX `GetBidDocumentList?id=`. Wired public fetch → `bidcondocs.delaware.gov`.
3. **Quantity semantics missing** — packaging / ambiguous parentheticals counted as ordered. Replaced with ORDERED vs PACKAGE vs UNKNOWN classes; Stage C requires affirmative ordered evidence.
4. **XLSX quantity lines truncated** — extracted `Quantity:` lines appended after large sheets then clipped by `_clean` limit. Prepended quantity lines; tightened IDIQ “as needed” false positives from vendor boilerplate.

## Independently observed working public paths

| Platform | Listing | Detail | Documents | Notes |
|---|---|---|---|---|
| Delaware MMP | Socrata `2hnj-zwix` | `/Bids/Details/{id}` (httpx OK) | `GetBidDocumentList` + bidcondocs PDFs/XLSX | Local acquire: Athletic Equipment **COMPLETE 5/5** |
| LA RAMP | Socrata `hf3r-utnq` | Salesforce shell | Restricted | Free vendor registration owner action |
| Georgia GPR | UI Goods OPEN (59) | `eventDetails` public | `downloadAttachment` public PDF | `eventSearch` POST returns **400** for non-browser clients |
| Montgomery MD | Socrata active | Metadata | TBD | Thin product yield in current active set |

## Ground truth

`docs/BUILD_002I_GROUND_TRUTH.json` — **132** independent active product-candidate references (LA/DE/MD/GA). Not imported into production.

## Funnel priority (do not reverse)

1. Level 2–4 adapters with real individuals + documents (Delaware, Georgia details)
2. Quantity/extraction correctness
3. Only then expand tenants / states

BidNet remains abandoned. SAM.gov / DLA parked.
