# Phase L.14 — Non-BidNet Commercial Source Expansion Design

**Build:** `20260928-m3-phase-l14-nonbidnet-source-expansion`

## Problem

Phase L.13 recovered BidNet public metadata for 82 blocked exact-history rows but **0 exact awards**. BidNet authenticated history is a future unlock, not current priority.

## Decision

Park BidNet auth history as `BIDNET_AUTH_HISTORY_PARKED`. Expand discovery + exact history on non-BidNet families.

## Canonical path only

All new sources feed `CanonicalOpportunityWorkflow`:

discovery → source verification → product classification → identity → history → quantity/UOM → supplier → economics → evidence grading → quote readiness

Live runner: `phase_l.l14_rescue.run_phase_l14_nonbidnet`

Hunt profile: `non_bidnet` (`phase_l.hunt` + `L14_KIND_CAPS`)

## Separated adapters

| Concern | Module |
|---------|--------|
| Discovery | `discovery.live_fetchers` (`live_opengov`, `live_ionwave`, …) |
| History | `phase_l.platform_history_adapters` (`OpenGovHistoryAdapter`, …) |
| Priority / yield | `phase_l.nonbidnet_expansion` |
| BidNet park | `phase_l.bidnet_parked` |

Discovery and history **must not** be mixed into one brittle adapter.

## Source priority (default; yield may reorder)

1. OpenGov 2. IonWave 3. PlanetBids 4. Bonfire 5. DemandStar  
6. Jaggaer/SciQuest 7. Public Purchase 8. DLA/DIBBS 9. cooperatives 10. state/local portals

## Success = commercially useful rows

Not “HTTP 200” or “adapter exists.” Track buyers, live/commercial Stage 3, attachments, exact identity/history, Gov A/B/C, validated/secondary quote targets.

## Hard stops

No BidNet login/CAPTCHA/registration. No accounts. No outreach/quotes/bids. No Phase M. Evidence grades unchanged.
