# Phase L.10 — Exact Evidence Workflow Design

**Build:** `20260928-m3-phase-l10-exact-evidence-workflow`

## Canonical orchestrator

Sole live path:

1. `phase_l.progressive_funnel.run_progressive_stages_cheap` — Stage 0–3 admission
2. `phase_l.canonical_workflow.CanonicalOpportunityWorkflow` — auditable state machine
3. `phase_l.l10_rescue.run_phase_l10_exact_workflow` — production runner

Historical `l6`–`l9_rescue` modules remain for replay only.

## State machine

`DISCOVERED` → `SOURCE_VERIFIED` | `SOURCE_VERIFICATION_PENDING` → `PRODUCT_CONFIRMED` → `IDENTITY_RESOLVED` → `GOV_VALUE_RESEARCHED` → `QUANTITY_RESEARCHED` → `ACQUISITION_CHANNEL_RESOLVED` → `SUPPLIERS_RESEARCHED` → `ACQUISITION_EVIDENCE_RESEARCHED` → `ECONOMICS_COMPUTED` → `EVIDENCE_GRADED` → `QUOTE_TARGET_CANDIDATE` → `QUOTE_TARGET_VALIDATED` → `OWNER_REVIEW`

Terminal: `EVIDENCE_EXHAUSTED`

Every transition stores: prior/next state, rule ID, evidence used/source, confidence, timestamp, missing evidence, fallback path.

## Evidence chain

`LIVE SOLICITATION` → exact product → buyer/product history → gov value → supplier channel → supplier → acquisition evidence → economics → quote target

No generic fallback may masquerade as exact evidence. Category benchmarks and generic supplier seeds are labeled `RECON_ONLY`.

## Identity states

`IDENTITY_EXACT` | `IDENTITY_STRONG` | `IDENTITY_SPEC_DRIVEN` | `IDENTITY_BRAND_OR_EQUAL` | `IDENTITY_PARTIAL` | `IDENTITY_UNRESOLVED`

## Economic confidence

Deterministic Gov×Supplier matrix → `HIGH` | `MEDIUM` | `LOW` | `RECON_ONLY`

## Stop rules

No Phase M, outreach, quotes, bids, purchase, or financing. Final bid gate unchanged.
