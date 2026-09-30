# Phase L.10 — Government Value Upgrades

## Grades

| Grade | Meaning |
|-------|---------|
| A | Exact / nearly exact buyer or NSN/MPN award |
| B | Strong close match / budget / ceiling |
| C | Comparable / model-specific award band (Tier 3) |
| D | Category benchmark recon only (Tier 4) |
| U | Unknown |

## Deterministic promotion

- Generic labels (`generic_vehicle`, `fleet`, …) → **always D**
- Named band (e.g. `light_truck_award_band`) **+ commercial model/MPN** → **C** (`model_specific_award_band_tier3`)
- Exact history / buyer memory → A/B via upgrade loop

## Upgrade loop

`GOV_VALUE_UPGRADE_ATTEMPTS` until A/B/C or `EVIDENCE_EXHAUSTED`.

Category benchmark never validates alone. Gov D → `RECON_ONLY_CATEGORY_BENCHMARK`.

Artifact: `artifacts/phase_l/l10_gov_d_upgrade.json`
