# Phase L.10 — Regression

## Counterfactual

L.9 validated target (`SPRTA126Q0448`) must remain validated when replayed through L.10 exact workflow. Loose L.8 category-benchmark positives must not reappear as validated without evidence upgrades.

## Invariants

- Stage3 / deep-research / manual queue: no fixed caps  
- Gov D alone cannot validate  
- Supplier D alone cannot validate  
- Unit-only suppresses total profit tiers  
- No outreach / send_authorized always false in L.10 artifacts  
- Final bid / compliance gate unchanged  

## Suite

`tests/test_phase_l10_exact_workflow.py` plus retained L→L.9 suites.
