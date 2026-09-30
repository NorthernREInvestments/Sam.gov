# R5 Legacy Cutover

**Build:** `20260929-m3-r5-preflight-submission-ui-sync`

## Canonical path

`response_engine.r5_service` (+ `operator_state_service` for UI)

## Reused

- R4 `generated_package` / `SubmissionHandoff`
- Operator console `/ops`
- Bid Prep card enrichment via `bid_prep_card_for_opportunity`
- L.22 call queue / registrations tracker

## Deprecated as sole truth

- Legacy “bid ready to submit” flags without R5 preflight
- Competing readiness engines for operator next-action

## Compatibility retained

- `phase_l.owner_ui_service` Home/Today/Deals
- Legacy index behind `?legacy=1`

## Operator entry

`/ops` — one workflow, not five backend phases.
