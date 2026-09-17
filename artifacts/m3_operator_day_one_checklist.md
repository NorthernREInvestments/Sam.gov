# M3 Operator Day-One Checklist (no outreach)

Build to confirm: `20260917-m3-primary` (header badge + `/api/m3/health`)

## Before you start
- Mode must show **DEV_NO_OUTREACH**
- Do **not** enable Controlled Verify tonight unless intentional
- No supplier / financier / agency contact, no registration, no bids, no spend

## Laptop / desktop
1. Open production URL (Railway app root).
2. Confirm header build badge = `20260917-m3-primary`.
3. Confirm mode badge = `DEV_NO_OUTREACH`.
4. Confirm Home answers: what matters / what needs attention / what next.
5. Open Opportunities — review 5–10 cards.
6. Open one product-oriented Deal Room — check requirements, economics, funding, compliance, next action.
7. Navigate: Home → Opps → Deal Room → Back → Actions → Sources → Settings → Home.
8. Settings: confirm M3 procurement profile (not legacy NAICS list).
9. Optional: open Controlled Verify from Settings — leave **disabled**.

## iPhone / iPad
10. Same URL in Safari.
11. Confirm single-column cards, bottom nav usable, no sideways scroll on Home/Opps.
12. Repeat Home → Opps → Deal → Back → Actions → Sources → Settings.

## Record
- Confusing labels, broken taps, missing economics, wrong mode, stale build (old badge).

## Quick API check (optional)
`GET /api/m3/health` → `ok`, `build_version`, `DEVELOPMENT_NO_OUTREACH: true`
