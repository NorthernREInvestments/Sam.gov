# BidNet Data Map

Status: **honest inventory of M3’s BidNet integration**.  
The forensic 82-opportunity cohort contained **zero BidNet opportunities**. Prior NextBid investigation does **not** prove BidNet acquisition.

## Current configuration (preserve)

| Setting | Value | Location |
| --- | --- | --- |
| `bidnet_enabled` | `False` (default) | `m3/config.py` |
| Production acquisition | Disabled | Feature kill with SAM/Euna |
| Cohort coverage | 0 / 82 BidNet | `TEMP/forensic_q1_audit/SUMMARY.json` |

## Verified vs unverified

### Verified in M3 codebase

- Config flag `bidnet_enabled` exists and defaults off.
- Host heuristic: `package.py` maps host containing `bidnet` → source label `"bidnet"`.
- No dedicated BidNet authenticated client module exists under `m3/research/` (no `bidnet_*.py` client).
- Discovery of BidNet-hosted listings, if any, historically arrives via **NextBid aggregation** (`source` / `source_url`), not a separate BidNet deep-read pipeline.

### Unverified (do not claim)

- Authenticated BidNet Direct search request shapes.
- Pagination behavior on BidNet Direct.
- Detail page HTML selectors / structured API fields.
- Document inventory structure on BidNet Direct.
- Document download / amendment / Q&A mechanisms on BidNet Direct.
- Whether a given BidNet listing contains uploaded attachments vs external agency links only.
- Live BidNet session reuse, CAPTCHA handling, or subscription limits.

## Discovery vs detail (conceptual — BidNet Direct)

An authenticated search hit may expose only title, agency, deadline, category, short description, and a detail URL. That is **discovery**, not a complete solicitation. Detail pages may include notices, dates, documents, addenda, Q&A, and external official links — **structure not verified in this build**.

## Source coverage honesty

| Situation | Correct classification |
| --- | --- |
| BidNet lists an opportunity | Discovery only until detail + docs inspected |
| BidNet hosts uploaded PDFs | Possible; unverified for M3 path |
| BidNet only links to agency portal | Package may be external; do not claim BidNet has the package |
| Zero BidNet in cohort | Cannot claim BidNet deep-read works |

## Storage / mapping

No BidNet-specific paid response store or document inventory path is active while `bidnet_enabled=false`.

## Operating rule

Do **not** enable BidNet production acquisition or recurring jobs in this corrective build. Separate owner authorization + live verification required before any BidNet claim of document completeness.
