# BUILD 002Q — Acquisition Quality Repair

Finished: 2026-11-22T21:47:28.759717+00:00 · Runtime: 420.5s · OpenAI: $0 · NextBid: 0

## Source quality

- Sources total: **250** · Active-enabled after repair: **62**
- Disabled non-active roles this pass: **69**
- Duplicate connectors disabled: **0**
- Purpose counts: `{"individual_solicitation_candidate": 73, "supplier_directory": 70, "historical_award": 17, "quarantined": 83, "forecast": 5, "purchasing_plan": 1, "procurement_listing": 1}`
- Connector smoke: tested=15 ok=4 fail=11

## Active solicitations by source (top 15)

- `discovered_citydata_mesaaz_gov_dfcn-ivuc`: 242
- `nj_purchase`: 235
- `la_ramp_open_bids`: 231
- `discovered_data_lacity_org_hf3r-utnq`: 231
- `sf_open_bid_opportunities`: 80
- `discovered_data_winnipeg_ca_rijt-92n4`: 59
- `winnipeg_bid_opportunities`: 59
- `delaware_open_bids`: 42
- `georgia_gpr_open_goods`: 31
- `chicago_procurement`: 6
- `al_purchasing`: 2
- `boston_procurement`: 2
- `sacramento_procurement`: 2
- `detroit_purchasing`: 1
- `nc_purchase`: 1

## Document validation

- Packages with downloads scanned: **1505**
- Genuine non-junk package excerpts (downloaded set): **286**
- Junk packages demoted this pass: **282**
- Freeze-linked packages: **14075** · Genuine usable text: **334** · Junk/thin linked: **13741**
- Document acquisition success rate (usable/linked): **0.0237**
- Targeted recovery success rate: **0.702**

## Product recovery (targeted free re-acquire)

- Missing-qty targets: **370** · Stage C gaps: **20**
- Attempted: **151** · Packages COMPLETE/PARTIAL: **106**
- Quantities recovered: **16**
- Skipped blocked hosts: **187** · junk portals: **52**

## After full qualify reprocess

| Metric | Count |
|---|---:|
| Product opportunities | 390 |
| Missing quantity | 370 |
| Stage C | 20 |
| Bid-eligible | 2 |

## Remaining acquisition blockers

- Missing quantity on product opportunities: **370** (of 390)
- LA RAMP / auth-walled detail pages skipped: **187**
- Mesa/Phoenix junk portal hosts skipped on recovery: **52**
- Freeze still contains portal-linked packages: **13,741** junk/thin of **14,075** linked (success rate 2.37%)
- Many HTML portal connectors reach listing pages but yield few solicitation-specific document links without auth

Awaiting owner acceptance.

## Controlled source reset (follow-on)

Superseding operating mode as of 2026-11-22T23:35:55.964670+00:00: owner allowlist only; NextBid offline baseline unique=2255, products=714, stage_c=12, bid_eligible=0. See BUILD_002Q_CONTROLLED_SOURCE_RESET.md.
