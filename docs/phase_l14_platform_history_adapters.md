# Phase L.14 — Platform History Adapters

**Build:** `20260928-m3-phase-l14-nonbidnet-source-expansion`

## Module

`phase_l.platform_history_adapters`

## Adapters (history only)

| Adapter | Platform |
|---------|----------|
| `OpenGovHistoryAdapter` | OpenGov |
| `IonWaveHistoryAdapter` | IonWave |
| `PlanetBidsHistoryAdapter` | PlanetBids |
| `BonfireHistoryAdapter` | Bonfire |
| `DemandStarHistoryAdapter` | DemandStar |
| `JaggaerHistoryAdapter` | Jaggaer/SciQuest |
| `PublicPurchaseHistoryAdapter` | Public Purchase |
| `DlaDibbsHistoryAdapter` | DLA/DIBBS |

Entry: `run_platform_history(row, …)` → public artifact recovery → buyer pivot → auth-walled recovery (never BidNet login).

## BidNet

If parked: return `BIDNET_AUTH_HISTORY_PARKED` immediately. No history scrape.

## Normalization

Awards/tabs normalize via `normalize_award_tabulation` / `grade_recovered_award`:

solicitation, line, description, model, manufacturer, quantity, UOM, bidder, vendor, unit price, line total, award total, date.

## Bonfire / DemandStar pivot

When platform results are not public: buyer procurement page, board agendas, award notices, contract register, open data (`run_buyer_pivot`).
