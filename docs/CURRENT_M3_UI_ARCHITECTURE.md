# Current M3 UI Architecture

**Build:** `20260929-m3-r5-preflight-submission-ui-sync`  
**Primary entry:** `/ops` (`static/operator.html`)

## Role

The UI is an **operating console**, not a research dashboard.

Operator always answers:

1. What next?  
2. Which deal first?  
3. What exactly to do?  
4. What to collect?  
5. What am I waiting on?  
6. What is blocked?  
7. What can I ignore?

## Navigation

Primary: Home · Today · Deals · Calls · Quotes · Registrations · Bid Prep · Watch · Blocked · Settings  

Advanced / legacy: `Show Details`, `/index.html?legacy=1`, `/supplier_call_desk.html`

Internal phase names (R1–R5, L.21 / L.22 / L.23) are **not** primary nav.

## Status mapping

Funnel mapping: `phase_l/owner_ui_status.py`  
Response/submission next action: `response_engine.operator_state_service` (canonical)

| Operator label | Typical source |
|----------------|----------------|
| CALL SUPPLIER | L.22 today calls / READY_TO_CALL |
| FOLLOW UP | promised quote / callback |
| WAITING FOR QUOTE | QUOTE_PENDING |
| QUOTE RECEIVED | quotes store |
| REGISTER FIRST | registration tracker |
| OWNER ACTIONS | attestations / signatures / approval |
| RESPONSES | build / preflight / review |
| BID PREP | ready-to-bid funnel + response projects |
| SUBMISSIONS | guided portal/email after approval |
| AWAITING RESULT | confirmed / dry-run confirmed receipt |
| BLOCKED | access gates / hard fails |
| WATCH | weak / future |

Every deal exposes `ui_next_action` + `ui_next_action_reason` (+ `assigned_role` when from R5).

## API

`phase_l/owner_ui_service.py` + `/api/ui/*` in `app.py`:

- `/api/ui/home` `/today` `/deals` `/calls` `/quotes` `/registrations` `/blocked` `/bid-prep` `/watch`
- `/api/ui/calls/workspace` + `/api/ui/calls/save`
- R5: `/api/response-projects/{id}/preflight` · `owner-approval` · `signature-tasks` · `submission-plan` · `freeze-submission` · `submission-events` · `receipt` · `submission-audit` · `operator-state`

Frontend does **not** invent readiness — Bid Prep uses `bid_prep_card_for_opportunity`.

## Screens

| Screen | Purpose |
|--------|---------|
| Home | Attention cards (incl. Owner Actions / Responses / Submissions) |
| Today | Guided queues by action |
| Deal | What / Why / one primary action |
| Call | Supplier + Must Ask + answers + notes + save |
| Quotes | Bucketed queue + review |
| Registrations | Unlock value + walkthrough |
| Bid Prep | Response package + Review & submit (preflight / approve / guided) |
| Settings | Company compliance profile + toggles |
| Watch / Blocked | Separated |

## Legacy cleanup

- Old GOS/phase nav remains in `index.html` but **hidden** and redirected to `/ops` unless `?legacy=1`.
- Duplicate “ready to submit” claims without R5 preflight are not operator truth.
- R1–R5 codes stay in Advanced / docs only.

## Training

- `docs/M3_OPERATOR_QUICKSTART.md` (~15–30 min)
- `docs/M3_OWNER_APPROVAL_QUICKSTART.md`
