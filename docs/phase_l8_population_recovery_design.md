# Phase L.8 — Population Recovery Design

**Build:** `20260927-m3-phase-l8-population-evidence-recovery`  
**Primary KPI:** `ECONOMICALLY_EVALUABLE` population (not quote readiness alone)

## Objective

Recover missing evidence across **all** Stage 3 rows:

1. Government-side value  
2. Supplier / acquisition path  
3. Quantity / UOM  

Parallel branches: `GOV_VALUE_RECOVERY` · `SUPPLIER_RECOVERY` · `QUANTITY_RECOVERY` · `UOM_RECOVERY` · `CONFIGURATION_RECOVERY`

Immediate recompute on every recovered component → dynamic positive / READY expansion (no caps).

## Modules

| Module | Role |
|--------|------|
| `phase_l/evidence_recovery.py` | Recovery branches |
| `phase_l/economic_evaluability.py` | Evaluability states + recompute |
| `phase_l/l8_rescue.py` | Full Stage 3 runner |
| `scripts/run_phase_l8_population_recovery.py` | Live entry |

## Live KPI (this run)

Economically evaluable: **32 → 176** (+144)
