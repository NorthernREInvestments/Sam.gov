# Phase L.14 — Discovery Gaps

**Build:** `20260928-m3-phase-l14-nonbidnet-source-expansion`

Do **not** claim complete coverage. After each L.14 run, `artifacts/phase_l/l14_discovery_gaps.json` lists gaps.

## Measured gaps (this run)

| Gap | Notes |
|-----|-------|
| BidNet exact awards | Parked (`BIDNET_AUTH_HISTORY_PARKED`); 82 blocked; free registration later |
| OpenGov CDN | `procurement.opengov.com` Cloudflare → `PARKED_ACCESS_DEPENDENCY`; use agency alternates |
| Exact history upgrades | 0 Gov A/B/C upgrades this run → next bottleneck: **history recovery** |
| IonWave / PlanetBids / Bonfire hubs | Public hubs empty or JS-shell in this crawl |
| DemandStar / Public Purchase | Often registration for useful docs |
| DIBBS | EMPTY / bot in this crawl; keep specialty + SAM reconcile |
| NASPO / OMNIA live pages | Empty this crawl; Sourcewell + HGAC produced live rows |
| Platform detect undercount | Fetch yielded 22 unique; inventory retains fewer tagged non-SAM portals after merge |

## Next phase guidance

Use yield telemetry — not the default priority list alone. Highest immediate ROI: deepen cooperative + agency-alternate OpenGov history, then PlanetBids/Bonfire buyer pivots.
