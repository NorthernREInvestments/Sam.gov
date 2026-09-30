# Phase L.11 — Platform / Coverage Notes

## Weak platforms (classification)

| Platform | Classification |
|----------|----------------|
| BidNet | `anti_bot` (HTTP 202 empty on open-bids) |
| OpenGov | enumeration_incomplete + auth |
| Bonfire | auth_issue |
| IonWave | discovery_logic_incomplete |
| PlanetBids | buyer_registry_incomplete |
| DemandStar | auth_issue |
| Jaggaer | auth_issue |
| Public Purchase | enumeration_incomplete |
| DLA/DIBBS | not_in_accessible_commercial_path |
| cooperatives | not_retained_in_accessible_set |

## Buyer registry

`data/phase_l11_buyer_registry.json` — buyer, state, type, platform, auth, yield, last_checked.

## Coverage reports

Do **not** claim 50-state coverage from BidNet network membership alone.

See:

- `artifacts/phase_l/l11_state_coverage.json`
- `artifacts/phase_l/l11_buyer_type_coverage.json`
- `artifacts/phase_l/l11_category_coverage.json`
