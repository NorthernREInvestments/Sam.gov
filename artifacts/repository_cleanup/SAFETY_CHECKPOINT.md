# Repository cleanup — critical safety checkpoint

**Status:** `DESTRUCTIVE_CLEANUP_ALLOWED` (cleanup **not** started)  
**Branch:** `cleanup/m3-r5-production-consolidation`

## Commits

| Role | Hash | Subject |
|------|------|---------|
| Branch base / starting | `0d5c08c717298888b013af1badba702999241419` | feat: add µLab product-transaction integrity and source routing. |
| **r5_known_good_commit** | `2fe3e1629e4844846057ddcf564e73b83b421791` | checkpoint: known-good M3 R5 production baseline before repository consolidation |

## Verification at baseline

- `.env` ignored and **not** in commit
- Secret path/content scan: **PASS** (0 hits)
- Imports + `app` module load: **OK**
- Targeted tests: R5 + owner UI → **38 passed**
- R5 code present: `response_engine/r5_*.py`, `operator_state_service.py`, operator UI, R5 docs/artifacts
- Canonical corpus present: `data/response_corpus/real/`
- Mutable live stores excluded: `data/response_projects/`, caches, memories, owner notes, attestation logs

## Rollback

```text
git checkout 2fe3e1629e4844846057ddcf564e73b83b421791
```

(or hard-reset this cleanup branch to that hash only when intentional)

## Destructive cleanup

`destructive_cleanup_allowed: true`  
`destructive_cleanup_started: false`

Do **not** begin deletes until an explicit cleanup pass is authorized. Prefer proven-redundancy inventory first.
