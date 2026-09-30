# M3 Operator UI Audit (Phase C prerequisite)

Inspected: `static/index.html`, `static/m3-mobile.js`, `static/micro-purchase-lab.js`, bottom nav, Phase B API fields.

**Date:** 2026-09-22  
**Rule:** Preserve engines + Phase A/B projection; UI-only transform.

---

## Current screens

| Screen | View id / route | Purpose today | Problems | Recommendation |
|--------|-----------------|---------------|----------|----------------|
| Home | `view-m3-home` · `#m3-home` | Ops console: Discovery/Research/Evidence bars + actions + active opps | Telemetry-first; Stage/discovery jargon; not “owner approvals” | **Modify** → Dashboard (Active Work / Approvals). Move discovery bars to Advanced |
| Opportunities | `view-m3-opportunities` · `#m3-opportunities` | Portfolio list; filters CVW / First tx / Partial | Enum filters; Status = Deal_state; not intake-focused | **Modify** → New Opportunities (plain cards, Why M3 likes it, next action) |
| Actions | `view-m3-actions` · `#m3-actions` | Operator action queue | Useful but duplicates Home “Actions first” | **Hide** from primary nav; fold into Dashboard Attention |
| Sources | `view-m3-sources` · `#m3-sources` | Source health / coverage / recovery | Engineer ops; encoding glitch in subcopy | **Hide** → Advanced |
| Controlled Verify | `view-m3-verify` · `#m3-verify` | Mode + first pursuits + learning | Nested under Settings; not daily VA work | **Hide** → Advanced |
| Settings | `view-m3-settings` · `#m3-settings` | Profile, credentials, link to Verify | OK as settings | **Keep**; add Advanced + role switch |
| µLab | `view-m3-micro-lab` · `#m3-micro-lab` | Economics lab / quote prep / funnel | Lab language; COMPLETE ambiguity | **Hide** → Advanced (keep capability) |
| Deal Room | `view-m3-deal-room` · `#m3-deal-room` | Per-opp deep stack (~18 sections) | Too technical; lifecycle vs Status conflict; no checklist | **Modify** → Deep Dive checklist workspace |
| Pipeline (missing) | — | — | No dedicated execution board | **Add** primary Pipeline view |
| Won/History (missing) | — | Outcomes only in Deal Room learning + Verify first-five | No institutional learning screen | **Add** primary Won / History view |

### Navigation today
Bottom nav (6): Home · Opps · Actions · µLab · Sources · Settings  
Hidden but active: Verify, Deal Room  

### Duplicated / conflicting UI concepts
| Concept | Where it appears | Conflict |
|---------|------------------|----------|
| COMPLETE | µLab badge, Deal Room evidence, OP complete | Different meanings |
| READY / READY_FOR_* | Deal Room pricing/economics readiness | Research-ready ≠ bid-ready |
| Status | Portfolio Deal_state on Opps cards | ≠ Deal Room `lifecycle` |
| Actions | Home + Actions tab | Duplicate queues |
| Legacy GOS / Product Pipeline | Hidden DOM in `index.html` | Parallel retired shells — keep hidden |

### Reusable Phase B fields (do not invent)
`operator_workflow_state`, `operator_next_action`, `operator_blockers`, `operator_state_reason`, `confidence_level`, `missing_information`, `risk_flags`, `state_conflict_detected`, `operator_summary`, `operator_dashboard` (active_work + attention_needed).

### Components to reuse
- `.m3-mobile-page`, `.m3-opp-card`, `.m3-action-card`, `.m3-info-card`, `.m3-card-stack`, `.m3-bottom-nav`
- `showM3View`, `fetchJson`/`apiFetch`, Deal Room open path

### Keep / hide summary
- **Keep (content):** Deal Room data, learning outcomes, µLab engines, Sources APIs  
- **Modify:** Home, Opps, Deal Room presentation + nav labels  
- **Hide from primary nav:** Actions, Sources, µLab, Verify (Advanced)  
- **Add:** Pipeline, Won/History  

---

## Implementation order (Phase C)
1. Nav shell + audit doc (this file) — **done**
2. Dashboard from `operator_dashboard` — **done**
3. New Opportunities cards — **done**
4. Deep Dive checklist (Deal Room reframe) — **done**
5. Pipeline + History lists — **done**
6. Role structure (Owner / VA) in Settings — **done** (UI emphasis only; no ACL)

**Build:** `20260922-m3-operator-ui-phase-c-1`  
**Primary nav:** Dashboard · New Opps · Deep Dive · Pipeline · History · Settings  
**Advanced (Settings):** System status, Actions, Sources, µLab, Controlled verify  
