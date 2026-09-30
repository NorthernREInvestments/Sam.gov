# Operator Workflow State Mapping (Phase A)

**Build:** `20260922-m3-operator-workflow-projection-1`  
**Module:** `operator_workflow/`  
**API:** `project_operator_workflow(record) -> dict`

This is a **read-only projection**. It does **not** replace `m3_lifecycle`, portfolio `Deal_state`, CRM `deal_lifecycle`, `award_lifecycle`, inventory `INV_*`, operator `OP_*`, or µLab `research_state`.

---

## Operator ladder

| Rank | Operator state | Meaning |
|------|----------------|---------|
| 0 | DISCOVERED | In the system; not yet screened as product-resale fit |
| 1 | QUALIFIED | Passed cheap/product screen; worth deeper work |
| 2 | DEEP_RESEARCH | Identity, quantity, history, package research |
| 3 | SUPPLIER_VALIDATION | Supplier/cost/availability validation |
| 4 | FINANCE_REVIEW | Financing path / cash requirement confirmation |
| 5 | BID_PREPARATION | Ready for human bid prep (not yet submitted) |
| 6 | SUBMITTED | Bid/offer submitted |
| 7 | AWARDED | Award received |
| 8 | ORDERING | Supplier order / fulfillment in progress |
| 9 | DELIVERED | Delivered / invoiced pending payment |
| 10 | PAID | Government paid / financier settled |
| 11 | HISTORY | Closed, lost, rejected, archived, learned |

---

## Projection output fields

| Field | Purpose |
|-------|---------|
| `operator_workflow_state` | Single ladder state |
| `operator_state_reason` | Why (includes source→map traces) |
| `operator_next_action` | Human-readable next step (no internal enums as primary text) |
| `operator_blockers` | e.g. `ECONOMICS_UNKNOWN`, `SUPPLIER_UNKNOWN`, `FUNDING_UNRESOLVED` |
| `confidence_level` | HIGH / MEDIUM / LOW / UNKNOWN (projection confidence) |
| `missing_information` | Gaps + `UNMAPPED:source:raw` when unknown enums appear |
| `risk_flags` | Warnings (e.g. µLab COMPLETE ≠ submitted) |
| `state_conflict_detected` | True when subsystems disagree |
| `conflicting_states` | Raw subsystem labels involved |
| `source_signals` | Audit trail of each mapped input |

---

## Resolution logic

1. **Collect** signals from known fields on the opportunity/deal dict.
2. **Map** each raw state through the tables below.
3. **Post-award authority:** if `award_lifecycle` is SUBMITTED+, use that track (do not let stale `RESEARCH_QUEUED` hide PERFORMING).
4. **Conservative pick:** among pre-award signals, choose the **least advanced** non-HISTORY state.
5. **Evidence gates** (pre-award only):
   - Unknown economics (`LEVEL_4_UNKNOWN`, missing unit economics) → cannot exceed **DEEP_RESEARCH** toward bid.
   - Unknown supplier → cannot reach **BID_PREPARATION** (cap at **SUPPLIER_VALIDATION**).
   - Unresolved funding → cannot reach **BID_PREPARATION** (cap at **FINANCE_REVIEW**).
6. **Conflicts:** if Ready-like and Verify-like signals coexist, set `state_conflict_detected=true` and keep the verify-equivalent (or deeper research) state. Do not silently pick the optimistic label.

---

## Source maps (summary)

### m3_lifecycle → operator

| Existing | Operator |
|----------|----------|
| DISCOVERED, NORMALIZED | DISCOVERED |
| CHEAP_SCREENED | QUALIFIED |
| RESEARCH_*, PACKAGE_*, BOM_*, ECONOMICS_IN_PROGRESS/PRELIMINARY | DEEP_RESEARCH |
| ECONOMICS_ATTRACTIVE | SUPPLIER_VALIDATION |
| COMMERCIAL_VERIFICATION_REQUIRED, PRICING_IN_PROGRESS | SUPPLIER_VALIDATION |
| FUNDING_VERIFICATION_REQUIRED | FINANCE_REVIEW |
| DRAFT_BID_READY, READY_FOR_OPERATOR_ACTION, COMPLIANCE_* | BID_PREPARATION |
| AWARDED | AWARDED |
| REJECTED_*, UNATTRACTIVE, CLOSED, CANCELLED, LOST, ARCHIVED | HISTORY |

### Portfolio Deal_state → operator

| Existing | Operator |
|----------|----------|
| DISCOVERED | DISCOVERED |
| CHEAP_SCREEN_PASSED | QUALIFIED |
| *_RESEARCH, PARTIAL_ECONOMICS | DEEP_RESEARCH |
| COMMERCIAL_VERIFICATION_WORTHY, ECONOMICS_ESTABLISHED, FIRST_TRANSACTION_CANDIDATE | SUPPLIER_VALIDATION |
| FUNDING_VERIFICATION_REQUIRED | FINANCE_REVIEW |
| OWNER_REVIEW | BID_PREPARATION |
| DEFERRED, VERIFIED_BLOCKED | HISTORY |

**Note:** `COMMERCIAL_VERIFICATION_WORTHY` is **not** bid-ready.

### Operator OP_* → operator workflow

| Existing | Operator |
|----------|----------|
| NEW | DISCOVERED |
| SCREENED | QUALIFIED |
| UNDERSTANDING, PRODUCT_RESEARCH | DEEP_RESEARCH |
| SUPPLY_RESEARCH, COMMERCIAL_VALIDATION | SUPPLIER_VALIDATION |
| DECISION_READY, EXECUTION | BID_PREPARATION (subject to gates) |
| COMPLETE, LEARNED | HISTORY (**OP_COMPLETE ≠ µLab COMPLETE**) |

### deal_lifecycle (CRM) → operator

Maps NEW…BID_READY into pre-award ladder; SUBMITTED→PAID→CLOSED into post-award / HISTORY.

### award_lifecycle → operator

| Existing | Operator |
|----------|----------|
| SUBMITTED | SUBMITTED |
| AWARDED | AWARDED |
| PERFORMING | ORDERING |
| INVOICED | DELIVERED |
| PAID | PAID |
| LOST, CLOSED | HISTORY |

### Inventory INV_* → operator

Pursuit-worthy → QUALIFIED / DEEP_RESEARCH; FUTURE_FUNDING → FINANCE_REVIEW; BID_PREP / SUBMITTED / AWARDED aligned; terminals → HISTORY.

### µLab research_state → operator

| Existing | Operator |
|----------|----------|
| RAW_UNRESOLVED | DISCOVERED |
| RESEARCH_*, INCOMPLETE_* (identity/history/value) | DEEP_RESEARCH |
| NO_MARKET_PRICE, COMPLETE | SUPPLIER_VALIDATION |
| REJECTED | HISTORY |

**µLab `COMPLETE` means research pack complete — not SUBMITTED.**

### µLab economics status

| Existing | Operator |
|----------|----------|
| SUPPLIER_QUOTE_NEEDED | SUPPLIER_VALIDATION |
| ECONOMIC_PASS | FINANCE_REVIEW |
| BID_CANDIDATE | BID_PREPARATION |
| ECONOMIC_FAIL, EXECUTION_FAIL | HISTORY |

---

## Duplicate / conflicting terminology (documented)

| Word | Meanings | Projection handling |
|------|----------|---------------------|
| COMPLETE | µLab funnel vs OP_COMPLETE (closed) vs offer blocker | µLab → SUPPLIER_VALIDATION; OP → HISTORY |
| READY / READY_FOR_* | Research-ready vs bid-ready | Map + evidence gates |
| VERIFICATION_WORTHY | Portfolio ranking | → SUPPLIER_VALIDATION, risk flag |
| TIER_2 | Multiple unrelated systems | Not used in projection |
| DEEP_RESEARCH | Often an action type | Operator **state** here is the phase name |

---

## Exceptions

1. **Post-award wins over stale pre-award lifecycle** when award_lifecycle ≥ SUBMITTED.
2. **HISTORY** signals are ignored when any active (non-HISTORY) signal exists, so a rejection flag on an otherwise live research row does not wipe the active path unless it is the only signal.
3. **Unmapped** raw values → DISCOVERED + `UNMAPPED:source:value` in `missing_information` (never invent BID_PREPARATION).
4. **LEVEL_4_UNKNOWN / economics_unknown** always caps below BID_PREPARATION at **DEEP_RESEARCH** (stricter than FINANCE_REVIEW alone). Funding-only gaps (economics known) land in **FINANCE_REVIEW**.
5. Evidence gates do **not** reopen HISTORY terminals (e.g. ARCHIVED stays HISTORY).

---

## Unresolved / residual risks

- Records that omit all status fields project to DISCOVERED with UNKNOWN confidence.
- `INVOICED` → DELIVERED is an approximation (invoice may precede physical delivery).
- Projection confidence ≠ AI model confidence; do not show as “AI score.”
- UI still shows old enums until a later phase consumes this API.
- `supplier_unknown` defaults false unless explicitly marked or commercial/quote-required — callers should pass `supplier_identified=False` when known missing.

---

## How to call

```python
from operator_workflow import project_operator_workflow

view = project_operator_workflow(opportunity_row)
# view["operator_workflow_state"], view["operator_next_action"], ...
```

Do not write `operator_workflow_state` back into durable stores in Phase A.

---

## Phase B — API integration (calculated fields)

Operator fields are attached at read time to UI-facing payloads. Still **not persisted**.

| Endpoint / read-model | Operator fields |
|-----------------------|-----------------|
| `GET /api/m3/mobile/dashboard` | Each `active_opportunities[]` card + top-level `operator_dashboard` (Active Work / Attention Needed) |
| `GET /api/m3/mobile/opportunities` | Portfolio `deals[]` / `top_cards[]` via `portfolio_operator_view` |
| `GET /api/m3/mobile/deal/{id}` | Deal room root + `operator_summary` + overview.operator_* |
| `GET /api/m3/mobile/actions` | Each action card |
| `GET /api/m3/portfolio/deals` | Same portfolio enrichment |
| `GET /api/m3/pipeline/status` | List items + single-opportunity detail |

Helpers: `operator_workflow.summary.attach_operator_workflow`, `build_operator_deal_summary`, `build_operator_dashboard_payload`.
