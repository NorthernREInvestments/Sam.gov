# Repository cleanup — critical safety checkpoint

**Status:** `DESTRUCTIVE_CLEANUP_BLOCKED`  
**Branch:** `cleanup/m3-r5-production-consolidation`  
**Artifact:** `artifacts/repository_cleanup/safety_checkpoint.json`

## Starting commit (branch base)

| Field | Value |
|-------|--------|
| Hash | `0d5c08c717298888b013af1badba702999241419` |
| Subject | feat: add µLab product-transaction integrity and source routing. |
| Prior branch | `main` (tracking `origin/main`) |

## Working tree understanding

The tree is **not clean** (~678 porcelain lines). Material R5 / operator / phase_l work is **uncommitted**, including:

- `response_engine/` (R1–R5)
- `phase_l/`, `static/operator.*`
- R5 docs / tests / `scripts/run_r5_corpus_validation.py`
- `artifacts/response_engine/r5_*.json` (and many other untracked artifacts/docs)

`starting_commit` alone is **not** a complete known-good R5 snapshot.

## Rules locked for this cleanup

1. No delete/overwrite of real business data (`data/` and live stores).  
2. No delete of modules with live runtime consumers.  
3. Unique legacy behavior → migrate + regression + verify → then delete.  
4. Uncertainty → **KEEP** + list in `remaining_compatibility.json`.  
5. Boot / route / regression / lost invariant failure → `M3_PRODUCTION_REPOSITORY_CONSOLIDATED_FAILED`.

## Destructive cleanup allowed?

**No.** Not until a known-good R5 commit exists on this branch and is recorded as `r5_known_good_commit`.

## Next required action

Approve a commit of the known-good R5 state onto `cleanup/m3-r5-production-consolidation` (exclude secrets / live `data/` as appropriate). After that commit, record its hash and only then begin **proven-redundancy inventory** (audit first; deletes only when proven safe).
