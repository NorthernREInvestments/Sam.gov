# Phase L.1 — Completion

## Goal

Repair false CONDITIONAL access classifications by distinguishing **can_compete** from **submission_readiness**, linking existing eligibility/registration components, tracking easy recurring vendor registrations, and re-running Phase L economics on accessible opportunities.

## What was reused (no duplicate subsystems)

| Capability | Canonical reuse |
|---|---|
| Company eligibility profile | `data/company_eligibility_profile.json` + `eligibility_gate` load/save |
| Set-aside / SB certs | `company_eligibility.held_certifications` / `set_aside_eligibility` |
| Vehicle / sole / source gate | `eligibility_gate.evaluate_eligibility_gate` |
| Portal credentials override | `m3_source_credentials.get_credential` |
| Owner actions | `operator_action_queue` + existing `ACTION_REGISTER_PORTAL` |
| Economics / competition / hunt | Existing `phase_l/*` modules |
| SAM fallback | Untouched |

## What was added / extended

| Item | Change |
|---|---|
| `phase_l/access_gate.py` | Open/unrestricted → YES; easy reg keeps YES + `REGISTER_BEFORE_BID`; true blockers stay NO |
| `phase_l/registration_tracker.py` | Lightweight recurring buyer tracker + gate taxonomy |
| `data/buyer_portal_registration_tracker.json` | Persistent sightings |
| `operator_action_queue.enqueue_portal_registration_action` | Owner queue hook |
| `phase_l/hunt.py` | Sightings, economics on every YES, funnel L.1 metrics, enqueue actions |
| `phase_l/owner_view.py` | Registration action chips |
| `phase_l/normalize.py` | Owner decision recognizes unit-price history |
| Tests | Open-market YES; easy-reg invariant; SB large → NO |

## Acceptance checks

| Criterion | Result |
|---|---|
| Easy vendor signup does not force CONDITIONAL | **Pass** (CONDITIONAL/UNKNOWN = 0 on live re-run) |
| `our_bid_access = YES` for open competition | **Pass** (483 YES) |
| Economics allowed when YES | **Pass** (483 researched) |
| Registration actions produced | **Pass** (21 + 10 recurring) |
| True blockers remain NO | **Pass** (sole source / ASL / BOAST) |
| No Phase M / SAM rewrite / outreach | **Pass** |

## Live re-run headline

2232 live → 563 tangible → **483 YES** → 80 NO → 0 CONDITIONAL → 483 economics attempted → 0 profit passes (pending history/retail data).

## Docs

- `docs/phase_l1_capability_reconciliation.md`
- `docs/phase_l1_live_rerun_report.md`
- `docs/phase_l_accessibility_model.md` (updated)

## STOP

Phase L.1 complete. Do not start Phase M from this task.
