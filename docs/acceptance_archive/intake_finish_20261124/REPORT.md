# M3 — Finish Procurement Intake (2026-11-24)

Branch `repair/procurement-engine-independent`. Not merged/deployed. Schedule remains disabled. Paid APIs locked. External calls this run: **0**.

## Defects fixed
- Title-stated quantities (e.g. “Purchase of one …”) now extract without a body page.
- NSN in title → EXACT identity; complete retained page/doc line schedules → SPECIFIED.
- Known-blocked hosts / prior restricted|robots outcomes → `access_required` (no re-probing OpenGov/Georgia).
- Owner buckets: `pricing_now`, `access_required`, `needs_investigation`, `submission_verified` (+ default `actionable_opportunities.jsonl`).
- Service-only schedule lines (e.g. HAZMAT screening services) excluded from pricing research.

## Inventory totals (offline rebuild)
- Loaded 92,166 → canonical 88,428 → evaluated open solicitations 29,257.
- Queues: pricing/`supplier_research_ready` 8 · `commercial_research_ready` 49 · `access_required` 40 · `document_verification` 13,574 · `status_verification` 1,926 · blocked 10 · archived 76,559.
- READY_TO_BID: **0** (correct — never asserted from solicitation info alone).
- SUBMISSION_VERIFIED: **0**.

## Incremental refresh
- `acquire --incremental`, `bidnet-enumerate --incremental`, and `discovery-schedule-plan` are implemented.
- Daily pipeline stages documented; `discovery_schedule_enabled=false`; cleanup remains dry-run / owner-gated.
- Healthz 200; NextBid acquisition_locked.

## API spend / runtime
- Paid API expenditure: **$0**. Metered NextBid: **0**.
- Final rebuild ~66–101s offline.
