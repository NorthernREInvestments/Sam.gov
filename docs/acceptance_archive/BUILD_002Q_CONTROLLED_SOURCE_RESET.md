# BUILD 002Q — Controlled Procurement Source Reset

Finished: 2026-11-22T23:35:55.964670+00:00 · Runtime: 53.3s · OpenAI: $0 · NextBid metered: **0**

## API usage

- Before: day_used=0 · account_remaining=2646 · OpenAI today=$0.0
- After: day_used=0 · account_remaining=2646 · OpenAI job=$0
- Metered NextBid calls this job: **0** (lock engaged)

## Source access control

- Owner allowlist path: `C:\Users\M_gra\OneDrive\Desktop\Sam.Gov App\govtracker\data\config\owner_source_allowlist.json`
- Approved keys: `['nextbid_state_local']`
- Live acquisition keys: `[]` (empty = no free expansion)
- Sources disabled by allowlist: **127** (initial pass; 0 live remain)
- Enabled after reset: **0**
- Unapproved cannot acquire: **True**
- Discovery auto-activation: **False**

## NextBid inventory (offline)

| Metric | Count |
|---|---:|
| Unique solicitations | 2255 |
| Currently active (estimate) | 2085 |
| Tangible product opportunities | 714 |
| Complete packages | 7 |
| Partial packages | 195 |
| Missing quantities (products) | 702 |
| Missing submission instructions (products) | 714 |
| Stage C | 12 |
| Bid-eligible | 0 |

### Provider vs M3 gaps (product blockers)

- Provider data gaps: `{"missing_or_restricted_solicitation_documents": 467, "quantity_not_in_accessible_provider_payload": 405, "submission_instructions_not_in_accessible_payload": 414, "active_status_unverified_thin_payload": 180}`
- M3 extraction/classification gaps: `{"quantity_not_extracted_from_available_text": 293, "submission_instructions_not_extracted": 300, "active_status_not_confirmed_from_text": 81}`
- Primary blockers: `{"missing_or_restricted_solicitation_documents": 467, "quantity_not_extracted_from_available_text": 240, "submission_instructions_not_extracted": 7}`

## Data preservation

- Source registry rows: **250** · Packages: **7142** · Documents: **5021** · Opportunities: **59**
- Destructive reset: **false**

## Safety tests (mocked)

- Controller pytest ok: **True** (returncode=0)

## Permanent rules in force

- No automatic source activation
- No additional subscriptions / unauthorized API spend
- SAM.gov and DIBBS parked; NextBid locked
- No historical awards in active queues; portal pages ≠ bid documents
- No free-source expansion until owner-approved allowlist entries

Awaiting owner acceptance.
