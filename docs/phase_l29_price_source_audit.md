# Phase L.2.9 — Price Source Audit

## Path expansion vs L.2.8

| Path | L.2.8 | L.2.9 |
|------|-------|-------|
| Direct distributor/OEM HTML | Yes | Yes |
| Product-detail resolution | Yes | Yes |
| Indexed snippet leads | Yes | Yes + corroboration |
| USAspending history | Yes (rescue) | Yes (HISTORY_BRANCH) |
| Procurement intel (coop/PDF) | L.2.5 only | Re-wired into Stage 3 |
| State term contracts | No | `StateContractPriceAdapter` |
| Board packets | Parser only | `PublicBoardPurchaseAdapter` + discovery |
| Open-data / Socrata | No | `OpenDataPurchaseAdapter` |
| Parallel hist∥acq | No | Yes |
| Lead corroboration | No | `CORROBORATED_STRONG_PRICE` |

## Evidence honesty

- Gov-channel / schedule prices → not auto acquisition  
- Indexed leads → `PRICE_LEAD_ONLY` / unverified until page verify  
- Corroborated leads → Stage 3 ranking eligible; final bid still needs verified acquisition when feasible  
