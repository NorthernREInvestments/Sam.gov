# Phase L.4 — Live Source Report

**Verdict:** `PHASE_L4_PARTIAL_COMMERCIAL_FEED_EXPANSION`

## Fresh discovery

| Metric | Count |
|--------|------:|
| Live runner unique | 12,087 |
| Live runner raw | 12,655 |
| Sources attempted / successful | 90 / 45 |
| Accessible (post-screen) | 1,949 |
| Stage 1 | 1,617 |
| Stage 2 / Stage 3 | 102 / 102 |
| Stage 3 processed | **102/102** |
| Deep queue | 27 |

## Source-family (accessible, deduped view)

| Family | Raw | Commercial-tagged |
|--------|----:|------------------:|
| STATE (incl. BidNet networks in mix) | 1,625 | 162 |
| FEDERAL | 292 | 0 |
| LOCAL | 32 | 7 |

Hunt source_mix before access screen: FEDERAL 500, STATE 11,807, LOCAL 279.

## Buyer types (accessible)

| Buyer type | Raw | Commercial |
|------------|----:|-----------:|
| MULTI_AGENCY_NETWORK (BidNet) | 1,621 | 161 |
| FEDERAL | 292 | — |
| CITY | 32 | 7 |
| STATE_AGENCY | 4 | 1 |

## Platform notes

45/90 sources succeeded. BidNet Direct statewide networks drove the majority of new commercial-looking inventory. OpenGov/Bonfire/PlanetBids/IonWave/DemandStar adapters exercised; some AUTH/bot/parse failures expected.
