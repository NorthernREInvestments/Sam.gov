# Phase L.10 — Platform History Adapters

Discovery adapters ≠ history adapters.

| Platform | History capabilities | Status |
|----------|---------------------|--------|
| SAM | historical sol + USAspending awards | active |
| DLA/DIBBS | awards, NSN history | active |
| BidNet | awards, bid tabs | partial (auth) |
| OpenGov | awards, tabs, contracts | partial |
| Bonfire | awards, tabs | stub |
| IonWave | awards, tabs | stub |
| PlanetBids | awards, tabs | partial |
| Jaggaer/SciQuest | awards, contracts | stub |
| Public Purchase | awards, tabs | partial |
| DemandStar | awards | stub |
| state_portals | awards, term contracts | partial |

Normalization: buyer, solicitation, vendor, manufacturer/model, qty, UOM, unit/total, award date → product history graph.

Module: `phase_l.platform_history` + `phase_l.history_graphs`
