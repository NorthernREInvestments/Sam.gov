# Current M3 Architecture

Build context: `20260929-m3-repository-fat-trim-runtime-consolidation`

## Canonical workflow

1. **Discovery / source coverage** — platform adapters (OpenGov, Bonfire, PlanetBids, IonWave, Socrata, SimpleHTML, state-owned, public BidNet portions). SAM API parked/credit-constrained; DIBBS CAGE-gated; BidNet auth history parked.
2. **Ingestion** — hunt + enrichment materialize `artifacts/phase_l/accessible_latest.json`.
3. **Canonical population** — single persistent store: `data/l23_canonical_population_store.json` (L.23 / L.23.1).
4. **Dedupe** — strong identity via solicitation_id / notice URL / buyer+solicitation; no silent fuzzy title merges.
5. **Fast funnel** — stage0 hard reject → stage1 product triage → accessible product / fast research.
6. **Deep research** — priority tiers (HIGH/MEDIUM/LOW) order work; score does not permanently strand viable rows.
7. **Buyer history** — L.19 recovery into buyer/product memory.
8. **Supplier acquisition** — L.20 channels + memory.
9. **Quote prep** — L.21 packets / owner approval.
10. **Call desk** — L.22 supplier call sheets, sessions, notes, answer capture, quote ingestion.
11. **Owner worklist** — CALL TODAY / FOLLOW UP / RESEARCH NEXT / REGISTER / BID PREP.

## Key states

- `READY_TO_CALL`
- `DEEP_RESEARCH_COMPLETE` (includes supplier-path-needed)
- `WATCH_FEDERAL_ACCESS` / `WATCH_OTHER`
- Quote / call session states

## Persistent intelligence (do not delete)

- `data/l23_canonical_population_store.json`
- `data/jurisdiction_procurement_registry.json`
- buyer/product/supplier history graphs under `data/`
- `artifacts/transactional_procurement_evidence/`
- L.18–L.23.1 CURRENT operational artifacts under `artifacts/phase_l/`

## One funnel rule

L.23 / L.23.1 is the canonical lifecycle. Older phase runners are historical only.

## SAM API (budgeted)

- Canonical client: `discovery.sam_budgeted_client`
- Daily budget: 10 (`SAM_DAILY_CALL_BUDGET`)
- Ledger + cache under `artifacts/sam/`
- Federal rows → `WATCH_FEDERAL_ACCESS` until entity registration; research retained

Last conversion verdict: `SAM_BUDGETED_REFRESH_AND_SUPPLIER_CONVERSION_WORKING`


## National source expansion

- Platform adapters: `discovery.platform_adapters` (OpenGov, Bonfire, PlanetBids, IonWave, SimpleHTML)
- Product density: `discovery.product_density`
- SAM value planner: `discovery.sam_call_value_planner`
- Expansion queue: `data/source_expansion_queue.json`
- Runner: `phase_l.national_source_expansion`

Last verdict: `NATIONAL_SOURCE_EXPANSION_WORKING`

## Owner / operator UI (M3)

- Primary console: `/ops` — `static/operator.html`
- Status mapping: `phase_l/owner_ui_status.py` (single label layer)
- Queue assembly: `phase_l/owner_ui_service.py`
- HTTP: `/api/ui/*` (today, deals, calls, quotes, registrations, blocked, bid-prep, watch)
- Training docs: `docs/M3_OPERATOR_QUICKSTART.md`, `M3_OPERATOR_CHEATSHEET.md`, `M3_OPERATOR_TRAINING_TEST.md`
- UI architecture: `docs/CURRENT_M3_UI_ARCHITECTURE.md`

Operators work Home → Today → next action → save. Research phase names are not primary navigation.

## Response engine (R1–R5)

- Package: `response_engine/` — solicitation intake through R5 submission workflow
- **R2 canonical economics:** `r2_service.py`
- **R3 canonical company/compliance:** `r3_service.py`
- **R4 canonical response generation:** `r4_service.py` (never submit)
- **R5 canonical preflight / approval / guided submission:** `r5_service.py` + `operator_state_service.py`
- Bid Prep UI: package + Review & submit (preflight / owner approve / guided steps)
- Docs: `docs/R5_*.md` + prior R* + operator/owner quickstarts
- Artifacts: `artifacts/response_engine/r5_*.json` …
- SAM calls for R1–R5 tests: **0** · External side effects in R5 tests: **0**

R1.3: `PHASE_R13_RESPONSE_COMPILER_VALIDATED_READY`  
R2: `PHASE_R2_CLIN_PRICING_TECHNICAL_COMPLIANCE_READY`  
R3: `PHASE_R3_COMPANY_COMPLIANCE_READY`  
R4: `PHASE_R4_RESPONSE_DOCUMENT_GENERATION_READY`  
R5: `PHASE_R5_SUBMISSION_WORKFLOW_READY`
