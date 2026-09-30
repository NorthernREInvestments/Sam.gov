# Phase L.2 — Regression

## Tests run

```
pytest tests/test_phase_l2_enrichment.py tests/test_phase_l_core.py tests/test_sam_live_fallback.py -q
```

Result: **44 passed** (L.2 + L core + SAM) on first full targeted run; **30 passed** on L.2+L core after market adapter tweaks.

## Cases covered (L.2)

| # | Case | Result |
|---|---|---|
| 1 | Exact MPN identity screen | Pass |
| 2 | Shared NSN cache key | Pass |
| 3 | History fields attach | Pass |
| 4 | BPA 2 offers → PRE_FILTERED | Pass |
| 5 | Open 2 offers → low open signal | Pass |
| 6–7 | Market selection prefers credible | Pass |
| 8 | Or-equal detected | Pass |
| 9 | Exact-only rejects substitutes | Pass |
| 10 | Unknown qty → no fabricated econ | Pass |
| 11 | Unknown history → not zero | Pass |
| 12 | Easy registration still enrichable | Pass |
| 13–14 | Owner queue ≥$10K vs hide fails | Pass |
| 15 | Reuses Phase J identity (not duplicate) | Pass |

## Non-regressions

- Phase L.1 access YES / easy registration invariants retained (`test_phase_l_core.py`)
- SAM live fallback suite unchanged

## Live validation

- `scripts/run_phase_l2_enrichment.py --refresh-hunt` then re-run without hunt
- Artifacts: `enrichment_latest.json`, `owner_profit_queue.json`
