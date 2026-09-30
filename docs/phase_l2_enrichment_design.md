# Phase L.2 — Enrichment Design

## Architecture

```
accessible Phase L row (our_bid_access=YES)
        │
        ▼
 stage_a_identity  ──►  phase_j.build_product_identity
        │               + sam_live_fallback.parse_qty_uom_from_text
        │               + or-equal detection
        ▼
 priority_bucket   ──►  ENRICH_NOW / NEXT / IF_CAPACITY / IDENTITY_NEEDED
        │
        ▼
 history (budgeted) ──►  usaspending_client + phase_j.reconcile_awards
        │               (state/local: m3_government_revenue_benchmark when possible)
        ▼
 market (budgeted)  ──►  m3_public_pricing_evidence commercial retrieval
        │               + collect_existing_price_evidence reuse
        ▼
 competition        ──►  phase_l.annotate_competition (access-type aware)
        ▼
 economics          ──►  phase_l.build_phase_l_economics (5% default)
        ▼
 owner queue        ──►  expected_net ≥ $10K only
```

## Priority (cheap, pre-expensive)

Score from: exact NSN/MPN, quantity present, open competition, runway, source level, estimated value cues.

## Cache

`data/phase_l2_enrichment_cache.json` keyed by `nsn:` / `mpn:` / `sku:` — reuse history + market evidence across opportunities when identity matches and evidence is fresh (<14 days).

## States

Maps onto existing Phase L `actionable_state` vocabulary; adds enrichment-stage fields:

- `enrichment_priority`
- `identity_research_state`
- `history_research_state`
- `market_research_state`
- `history_confidence`
- `market_price_confidence`

## Hard rules

- UNKNOWN ≠ 0
- No fabricated qty / history / retail
- Easy registration does not block enrichment
- PRE_FILTERED competition never ranked as open low-offer
- No supplier/buyer outreach
