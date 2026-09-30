# Phase L.14 — Source Economic Yield

**Build:** `20260928-m3-phase-l14-nonbidnet-source-expansion`

## Measure: `SourceEconomicYield`

From `phase_l.nonbidnet_expansion.source_economic_yield`:

- commercial Stage 3 per 100 raw
- Gov A/B/C evidence per 100 Stage 3
- validated/secondary quote targets per 100 Stage 3
- cost proxy (raw / useful)

Ranked in `artifacts/phase_l/l14_summary.json` → `source_economic_yield`.

## Measured (L.14 fresh hunt)

| Source | Raw (inventory) | Commercial S3 | Quote targets | Notes |
|--------|-----------------|---------------|---------------|-------|
| SAM | 1055 | 46 | 10 | Highest volume; not the L.14 investment target |
| cooperatives | 3+ (Sourcewell 11 / HGAC 5 at fetch) | 1 | 0 | **Highest new non-BidNet live yield** |
| OpenGov | 2+ (Boston bid-listings 4 at fetch) | 0 | 0 | CDN Cloudflare parked; agency alternate works |
| IonWave / PlanetBids / Bonfire / DemandStar / Jaggaer / Public Purchase / DIBBS | ~0 live | 0 | 0 | Auth/bot/empty — continue via yield telemetry |

## Ranking rule

Prefer sources that produce **exact evidence and quote targets**, not raw volume. BidNet NETWORK is deprioritized this phase.
