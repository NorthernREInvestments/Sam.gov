# SAM Query Planner

`build_daily_query_plan()` chooses up to 9 production calls before the reserve.

Prefer:

- max page size (1000)
- broad active solicitations (`ptype=o,k,i`)
- incremental date windows from `last_successful_sam_refresh`
- local product filtering (not one-call-per-NAICS)

Artifact: `artifacts/sam/sam_daily_query_plan.json`
