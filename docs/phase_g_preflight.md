# Phase G — Preflight

**Date:** 2026-09-24  
**Mode:** Supervised live validation only — no bids, outreach, purchases, financing, or WAWF.  
**Starting classification:** LIMITED LIVE TEST READY (Phase F)

---

## Phase F known Blind Spots (carry-forward)

| Blind spot | Implication for Phase G |
|------------|-------------------------|
| No live bulk scrape validation | Primary Phase G gap to close |
| TDP handling PARTIAL | Measure document/TDP blockers explicitly |
| WAWF / post-award PARTIAL | Out of Phase G scope (do not exercise) |
| Dashboard enrich capped at 80 | Live audits use per-deal enrich, not list-only |
| REAL_SOURCE cases are frozen fixtures | Must not count as LIVE_SOURCE proof |

---

## Live Source Paths To Use

| Source | Path | Status |
|--------|------|--------|
| **SAM.gov API** (primary) | `discovery/federal_sam_ingest.py` → `run_federal_sam_bootstrap` / `normalize_sam_opportunity`; client `sam_client.py`; wired in `discovery/live_runner.py` inside `run_live_discovery` | **Exercisable** with `SAM_GOV_API_KEY` + daily budget |
| **DLA via SAM cross-publish** | `discovery/dla_fallback.py`, `discovery/dla_product_extract.py` | **Exercisable** as SAM-reconciled DLA coverage |
| **DIBBS direct** | `discovery/live_fetchers.py` → `DibbsLiveFetcher` (`live_dibbs`) | **Not exercisable** — historically bot/403 (`DIBBS_BOT_BLOCKED_AUTOMATION`); no auth/bot bypass |
| **PIEE public** | `PieePublicLiveFetcher` | Often auth/chrome barriers — treat as degraded if attempted |
| **State/local live fetchers** | Bonfire, OpenGov, PlanetBids, BidNet, Jaggaer, etc. in `LIVE_FETCHERS` | Optional secondary; Phase G favors federal product resale |
| **GSA forecast / generic agency RFQ** | LEAD_ONLY / no list URL | **Cannot exercise** as open solicitations |

### Exact run entry points

```text
# Preferred supervised batch (Phase G runner — to be added under scripts/)
python -m scripts.run_phase_g_live_batch --target 100

# Existing production discovery (remote)
python scripts/run_production_discovery_once.py

# Existing local live opportunity test
python scripts/run_m3_live_opportunity_test.py

# API
POST /api/m3/discovery/run
GET  /api/m3/discovery/status
POST /api/m3/portfolio/analyze
```

Env required for SAM live: `SAM_GOV_API_KEY`. Optional: `SAM_API_CALL_LIMIT` / `SAM_DAILY_API_BUDGET`, `M3_DISCOVERY_PROFILE`, `SAM_FEDERAL_DAYS_BACK`.

---

## Current Pipeline Stages Used

| Stage | Module |
|-------|--------|
| Discovery | `m3_discovery_service._execute_run` → `discovery.live_runner.run_live_discovery` |
| Normalize / dedupe | `_execute_run` NORMALIZING / DEDUPLICATING |
| Cheap screen | `national_discovery_funnel.stage1_ultra_cheap` |
| Durable handoff | `m3_pipeline_handoff.run_durable_handoff` → ingest |
| Portfolio / research tiers | `m3_portfolio_deal_analysis.run_portfolio_cycle` |
| Canonical enrich | `execution_requirements.enrichment.enrich_deal_for_operator` |
| Owner gate (sole READY) | `execution_requirements.readiness.evaluate_owner_approval_gate` |
| Operator queue | `M3PipelineStore.operator_queue` / operator workflow projection |

Phase G measures each opportunity through this path with provenance = `LIVE_SOURCE`. No rewrite of Phase F gate logic.

---

## Request / Cost Limits

| Control | Default |
|---------|---------|
| SAM API daily | `SAM_API_CALL_LIMIT` / `SAM_DAILY_API_BUDGET` → **10**/day (raise carefully for Phase G batch) |
| AI daily screen | `AI_DAILY_SCREEN_BUDGET` → **25**/day |
| Enrich-on-sync | `ENRICH_ON_SYNC_LIMIT` → 5; intake-on-sync **false** |
| Cost Governor absolute autonomous | **$25** |
| Cost Governor monthly / daily / per-run / per-opp | $40 / $5 / $10 / $8 |
| Live profile TINY / BROAD / NATIONAL | 25 reqs·90s / 8000·5400s / 12000·10800s |

Phase G must respect Cost Governor and prefer cheap/public retrieval. Prefer TINY or bounded SAM bootstrap over NATIONAL unless volume requires it.

---

## Stop-Loss / Supervised Rules

| Rule | Enforcement |
|------|-------------|
| No automatic external actions | `automatic_external_actions_allowed()` always False |
| No outreach in development | `DEVELOPMENT_NO_OUTREACH` / `operating_mode.outreach_allowed()` |
| Bids / register / sign | Always forbidden in `external_action_control` |
| Portfolio SAFETY counters | Outreach/Quotes/Bids/Purchases forced to 0 |
| Live runner budget exhaust | Stops on `GLOBAL_BUDGET_EXHAUSTED` / `RUNTIME_BUDGET_EXHAUSTED` |
| Phase G hard stop | No supplier email, CO contact, bid, PO, financing, WAWF, purchase |

---

## Provenance Classes (Phase G)

| Class | Meaning | Counts as live proof? |
|-------|---------|----------------------|
| `LIVE_SOURCE` | Retrieved from live network this Phase G run | **Yes** |
| `FROZEN_REAL_SOURCE` | Frozen artifact from a prior real source (e.g. Phase F R_13/R_14) | No |
| `REALISTIC_FIXTURE` | Realistic frozen validation fixture | No |
| `SYNTHETIC_EDGE` | Synthetic edge case | No |

Pipeline rows today often carry `source_id` / SAM provenance but **not** harness-style Phase G provenance. Phase G runner must stamp `phase_g_provenance` / `source_provenance` explicitly.

---

## Live Sources That Cannot Be Exercised (and why)

1. **DIBBS direct scrape** — bot protection / 403; classified blocked; fallback is SAM DLA only.  
2. **PIEE** — frequent auth/chrome barriers.  
3. **GSA forecast / generic agency RFQ placeholders** — not open-solicit feeds.  
4. **Auth-required commercial portals** (IonWave, DemandStar, etc.) — `ADAPTER_AUTH_REQUIRED`.  
5. **Any action requiring account creation for write access** — forbidden under Phase G stop rules.

---

## Phase G Success Gate (preflight reminder)

- Prefer ≥50 LIVE_SOURCE opportunities (target 100); if fewer, classify **insufficient-volume** — do not pad with fixtures.  
- False-readiness audit on every READY.  
- No open P0 at completion.  
- Full suite runs **once** at the end.
