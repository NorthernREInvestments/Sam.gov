# Phase H — Preflight

**Date:** 2026-09-24  
**Mode:** Supervised deep research only — no outreach, bids, financing applications, or WAWF.  
**Start from:** Phase G LIVE_SOURCE artifacts (do not re-scrape 800 unless necessary).

---

## Phase G Starting Point

| Metric | Value |
|--------|------:|
| LIVE_SOURCE retrieved | 800 |
| Product-classified | 136 |
| Enriched through owner gate | 100 |
| READY | 0 |
| False READY / REJECT | 0 / 0 |
| Full suite | 1756 / 0 |

Phase G ran listing-only `enrich_deal_for_operator(..., include_full_profile=False)`.  
It did **not** run document deep-pull, USAspending history, commercial pricing, full identity resolution, or max-supplier-cost economics.

---

## Artifact Inputs

| Path | Use |
|------|-----|
| `artifacts/phase_g/live_run_latest.json` | 800 rows; filter `is_product` / `enriched` |
| `artifacts/phase_g/live_scorecard_latest.json` | Funnel totals |
| `validation_harness/cases/phase_g_live/` | Frozen promotions (not LIVE proof) |

Enriched product rows have: `canonical_id`, `title`, `ui_link` / `listing_url`, `has_nsn`, `is_dla`, blockers, provenance=`LIVE_SOURCE`.  
**Often missing:** deadline, description body, attachments (must deep-pull in Phase H).

---

## Existing Deep-Research Path (reuse, do not redesign)

| Step | Module |
|------|--------|
| Document page + links | `deep_deal_documents.retrieve_solicitation_page`, `discover_document_links`, `extract_solicitation_facts` |
| Evidence ladder | `m3_evidence_acquisition.acquire_evidence` |
| Product identity | `m3_product_identity_resolution.resolve_opportunity_product_identities` |
| Gov history | `usaspending_client`, `m3_government_revenue_benchmark.analyze_opportunity_revenue` |
| Commercial / public price | `m3_public_pricing_evidence`, `m3_supplier_intelligence` |
| Economics + max supplier cost | `deep_deal_economics.build_deal_economics`, `maximum_allowable_supplier_cost` |
| Funding | `funding_path_intelligence`, `m3_capital_requirement_gate_read` |
| Orchestrator | `deep_deal_research.research_one_deal` (+ Phase H wrapper to wire history) |
| Owner gate (bid-decision layer) | `execution_requirements.enrichment.enrich_deal_for_operator` |

**Phase H readiness layers (new classification, not gate rewrite):**
- `READY_FOR_QUOTE_OUTREACH` — evidence enough to safely request quotes (no contact yet)
- `READY_FOR_BID_DECISION` — only if cost+funding+compliance supported (must remain rare / honest)

---

## Ranking Fields for Cohort (deterministic)

From Phase G enriched products, score and take top 25:

1. `has_nsn` (exact NSN in title/structure)
2. `is_dla` (federal product RFQ style)
3. P/N or model token in title
4. `PARTS KIT` / equipment noun clarity
5. Prefer non-empty `ui_link`
6. Prefer fewer “service-ish” title tokens
7. Stable tie-break: `canonical_id` ascending

Exclude: already hard-rejected Phase G services/construction; empty title.

Deadline runway: Phase G deadlines often null → hydrate via live listing page during deep research; expire drops recorded then.

---

## Cost / Stop-Loss

| Control | Phase H plan |
|---------|----------------|
| Public HTTP | Shared `ResearchBudget` hard max (reuse deep_deal constants; target ≤80 for cohort) |
| USAspending | Cap ≤25 keyword/NSN lookups for cohort |
| SAM API | Prefer ui_link page fetch; no new 800-opp bootstrap |
| OpenAI | Offline by default (`force_openai_offline=True`) |
| Outreach | 0 |

---

## Known Limits (carry-forward)

| Item | Handling |
|------|----------|
| DIBBS bot-blocked | No bypass; SAM listing + public secondary only |
| TDP behind auth | Block readiness; name missing doc |
| History may be NONE | Not automatic reject |
| Phase G zeros on READY | Expected until deep path succeeds |

---

## Success Aim (truth over grade)

- ≥3 `READY_FOR_QUOTE_OUTREACH` if evidence supports  
- ≥1 `READY_FOR_BID_DECISION` only if evidence supports  
- If zero: document exact blockers — do not fabricate
