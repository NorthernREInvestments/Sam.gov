# Phase L.1 — Capability Reconciliation Map

Audit before coding. Prefer reuse; no parallel models.

| Required capability | Existing implementation | Action |
|---|---|---|
| Company eligibility profile | `data/company_eligibility_profile.json` + `eligibility_gate.load/save_company_eligibility_profile` | **Reuse / extend schema** (canonical for access) |
| Parallel company capabilities DB | `company_profile.py` | Keep separate; do not merge in L.1 |
| Owner UEI/CAGE settings UI | `settings_store` / Settings Proposal panel | Leave; optional future sync |
| Set-aside certs (SB default) | `company_eligibility.held_certifications` / `set_aside_eligibility` | **Wire into Phase L SB path** |
| Vehicle / sole / ASL / JCP gate | `eligibility_gate.evaluate_eligibility_gate` | **Reuse** |
| Phase L access vocabulary | `phase_l/access_gate.py` | **Extend** (core repair) |
| Vendor registration detect | `_VENDOR_REG_RE` in access_gate | **Extend** with easy-reg taxonomy |
| Free registration taxonomy | `procurement_source_coverage_discovery` ACCESS levels | Read optional `source_access_level` on row |
| Portal credentials | `m3_source_credentials` | Optional REGISTERED override |
| Owner action queue | `operator_action_queue` + `ACTION_REGISTER_PORTAL` | **Extend** enqueue helper |
| Mobile action queue | `m3_mobile_read_model.action_queue_mobile` | Consume existing actions |
| Recurring buyer registration tracker | — | **New lightweight** `data/buyer_portal_registration_tracker.json` |
| Economics / competition / hunt | `phase_l/economics`, `competition`, `hunt` | **Reuse**; unlock economics on YES |
| SAM live fallback | `sam_live_fallback.py` | **Do not rework** |

## Root cause of Phase L YES=0

Open IFB/RFQ → `eligibility_gate` returns `ELIGIBILITY_NOT_APPLICABLE` (quote-OK).  
Phase L mapped that to `UNKNOWN`, then forced `CONDITIONAL` via `open_competition_but_company_registration_unconfirmed`.  
Ordinary vendor registration and empty SAM fields were treated like legal ineligibility.

## L.1 rule

- **Can compete** (`our_bid_access`) ≠ **submission ready** (registration complete)
- Easy admin registration → YES + `REGISTER_BEFORE_BID` / `REGISTER_NOW`
- True vehicle/cert/source blockers → NO
- UNKNOWN only when that fact is solicitation-required

## Post-repair connections

```
company_eligibility_profile.json
        │
        ▼
eligibility_gate ──► phase_l.access_gate ──► phase_l.hunt
        ▲                      │                    │
company_eligibility.held_certs │                    ├─► economics (on YES)
                               │                    ├─► registration_tracker
                               ▼                    └─► operator_action_queue
                    owner_view registration chips         (ACTION_REGISTER_PORTAL)
```

See also: `docs/phase_l1_live_rerun_report.md`, `docs/phase_l1_completion.md`.
