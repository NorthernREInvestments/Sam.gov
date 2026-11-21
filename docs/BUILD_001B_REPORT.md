# M3 2.0 Build 001B — Second Owner Acceptance Corrections

**Date:** 2026-11-21  
**Build ID:** `20261121-m3-2-build-001b`  
**Deploy:** https://samgov-production.up.railway.app  
**Scope:** Corrections only — no Build 002 work. Design/language preserved from 001A.

## Correction status

| # | Correction | Status |
|---|------------|--------|
| 1 | Production data integrity + owner cleanup preview | **VERIFIED** (code + local preview; apply on production via Settings → Data cleanup) |
| 2 | Home dashboard horizontal tabs | **VERIFIED** |
| 3 | Readable opportunity display titles | **VERIFIED** |
| 4 | Missing-field recovery + status reasons | **VERIFIED** (listing/title recovery; no fabricated values) |
| 5 | Estimated value evidence foundation | **VERIFIED** (published text only; method labeled) |
| 6 | Supplier company contact header + edit/audit | **VERIFIED** |
| 7 | Supplier form readable labels; no fake Net-30 | **VERIFIED** |
| 8 | Invitation-only signup | **VERIFIED** |
| 9 | Owner user management + session invalidation | **VERIFIED** |
| 10 | Opportunity save/unsave/archive/assign + active counts | **VERIFIED** |
| 11 | Design/mobile consistency | **VERIFIED** (tabs + forms preserved) |
| 12 | Deploy + evidence | **PENDING deploy confirmation** |

## Notes

### Data integrity
- Discovery still does **not** auto-save opportunities or CRM suppliers.
- Acceptance harness no longer bulk-saves into CRM.
- Settings → **Data cleanup** shows preview of confirmed/likely test pollution; owner must confirm.
- Cleanup archives polluted opportunities, deactivates seed CRM suppliers, deactivates test users and bumps `session_version`.

### Dashboard
- Horizontal tabs: Overview, Upcoming Deadlines, Assigned Tasks, Supplier Follow-ups, Applications, Recent Activity.
- Compact metrics link to tabs; empty sections no longer stretch in a four-column grid.
- Counts use active saved/pursuing product-resale records (excludes acceptance harness + clear services/construction).

### Titles & missing info
- Display title format: `{Purchasing entity} - {Product description}` when both verified.
- Official title preserved separately.
- Enrichment recovers solicitation #, manufacturer (`MFR.:`), closing text, UTC offsets → human dates.
- Detail view shows field status + unavailable reasons (not fabricated substitutes).

### Suppliers & users
- Company header: website, address (+ type), phone (`tel:`), email (`mailto:`).
- Individual contacts remain separate.
- Invitations: owner creates pending token; user signs up with token only; no public registration.
- Deactivate / password reset invalidates sessions via `session_version`.

## Missing-information rates
Measure on Filter results before/after enrichment (same discovery run):

| Field | Before (raw listing Unknown/empty) | After (enrichment from listing/title only) |
|-------|--------------------------------------|--------------------------------------------|
| Purchasing entity | High on HTML scrapes using source name only | Improved when buyer/source name present; still Unknown when neither published |
| Solicitation # | Often empty on HTML harvest | Recovered when RFB/IFB pattern present in title |
| Closing datetime | Often empty / timezone Unknown | ISO offsets → UTC/offset labels; title Closing: text recovered when present |
| Quantity | Often empty | Recovered only from explicit title patterns |
| Estimated value | Often Unknown | Filled only when published budget/NTE language present |
| Manufacturer | Rare | Recovered from `MFR.:` title pattern only |

Do **not** treat “Not published” / reason text as a lower Unknown rate — those are honest status labels.

## Await
Owner acceptance testing after Railway deploy of `001b`. Do not begin Build 002.
