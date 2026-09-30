# Phase L.14 — Platform Capability Matrix

**Build:** `20260928-m3-phase-l14-nonbidnet-source-expansion`

Generated from `phase_l.platform_history_adapters.source_capability_matrix()` and refined by hunt telemetry in `artifacts/phase_l/l14_source_capabilities.json`.

| Platform | Discovery | Attachments | Awards | Bid Tabs | History | Auth | Commercial Yield |
|----------|-----------|-------------|--------|----------|---------|------|------------------|
| OpenGov | partial | partial | partial | partial | buyer_pivot | public_discovery_auth_history | high_potential |
| IonWave | partial | partial | partial | partial | buyer_pivot | varies | medium |
| PlanetBids | partial | partial | partial | partial | buyer_pivot | public_discovery_auth_docs | high_potential |
| Bonfire | partial | partial | buyer_pivot | buyer_pivot | buyer_pivot | public_discovery_auth_history | high_potential |
| DemandStar | stub | unknown | unknown | unknown | buyer_pivot | often_registration | medium |
| Jaggaer/SciQuest | partial | partial | partial | rare | buyer_pivot | often_auth | medium |
| Public Purchase | partial | partial | partial | partial | partial | often_public | high_potential |
| DLA/DIBBS | active | partial | partial | rare | nsn_history | public_or_cac | specialty_plus_milspec_open |
| cooperatives | partial | partial | schedules | rare | contract_pricing | public_docs | medium |
| state_portals | partial | partial | partial | partial | partial | varies | high_potential |
| BidNet | degraded_anti_bot | parked | parked | parked | PARKED (auth) | anti_bot + free_reg | parked_this_phase |

## Auth taxonomy

- fully public
- public discovery / auth documents
- public discovery / auth history
- free registration
- buyer-specific account
- private restricted
- anti-bot

When useful evidence requires registration/login for all paths → `PARKED_ACCESS_DEPENDENCY` (no endless engineering).
