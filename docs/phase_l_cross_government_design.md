# Phase L — Cross-Government Design

**Observed gap:** Phase K validated SAM live fallback, but M3 still lacked a unified Federal+State+Local+Cooperative hunt that gates on **accessibility before pricing** and ranks by expected net profit (≥$10k).

**Root cause:** Discovery adapters existed; Phase L vocabulary (`our_bid_access`, `competition_access_type`, retail baseline, profit tiers, PRE_FILTERED competition) did not.

**Smallest fix:** New `phase_l/` package on top of existing `discovery/live_runner`, `eligibility_gate`, `phase_i` product screen, and `sam_live_fallback` — no SAM fallback rewrite.

---

## Architecture

```
discover (live_runner + public SAM SGS search)
  → cheap product screen (phase_i)
  → access gate (phase_l.access_gate)   ← HARD STOP if not YES
  → history / retail / economics        ← only if YES
  → normalize + unified rank
  → owner view chips
```

## Modules

| Path | Role |
|------|------|
| `phase_l/access_gate.py` | `competition_access_type`, `our_bid_access`, `access_blocker`, state/local fields |
| `phase_l/competition.py` | Open vs `PRE_FILTERED_COMPETITION`; offer ranking |
| `phase_l/economics.py` | Retail baseline, 5% financing default, profit tiers |
| `phase_l/normalize.py` | Common cross-gov row + actionable states |
| `phase_l/federal_public_search.py` | Public SAM SGS HAL search (no personal API key) |
| `phase_l/hunt.py` | Orchestrator + funnel + mix quotas |
| `phase_l/owner_view.py` | Owner Access/Competition/Economics chips |
| `scripts/run_phase_l_live_hunt.py` | Live entrypoint |
| `GET /api/m3/phase-l/latest` | Artifact summary API |
| Mobile deal room | Phase L chip section (no UI redesign) |

## Hard rules preserved

- `our_bid_access = YES` required for actionable / READY_FOR_OWNER_REVIEW
- CONDITIONAL / UNKNOWN stay in research only
- Low offer counts are not attractive when history is vehicle-prefiltered
- Unverified discounts cannot create a PASS
- SAM personal API failure → `DISCOVERY_DEGRADED`, not impossible
- No outreach / bids / financing apps / Phase M

## Company profile

`data/company_eligibility_profile.json` remains empty/UNKNOWN by design. Open-market deals therefore score **CONDITIONAL** until SAM/SB registration is confirmed by the owner — fail-closed, not invented YES.
