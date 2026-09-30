# Repository Cleanup Results

Build: `20260929-m3-repository-fat-trim-runtime-consolidation`

## Verdict

`M3_REPOSITORY_CLEANUP_COMPLETE`

## Size

| | Bytes | MB |
|--|------:|---:|
| Before | 206,580,446 | 197.0 |
| After | 116,191,565 | 116.2 |
| Removed | 90,388,881 | 90.4 |
| Reduction | | **43.8%** |

### Breakdown

| Area | Before MB | After MB |
|------|----------:|---------:|
| artifacts/ | 116.5 | ~51 |
| data/ | 33.9 | 33.9 (preserved) |
| Python caches | ~18.3 | ~0 |

## Files

- Before: 5,277
- After: ~4,200
- Removed: ~1,077+

## Removed (major categories)

| Category | Approx |
|----------|--------|
| REGENERABLE_CACHE (`__pycache__`, `.pytest_cache`) | ~18 MB |
| SUPERSEDED phase dumps (L.3–L.17, L.24–L.29, G/H/I/J/K) | large |
| TEMP (`_t`, `_temp_live_autonomous`, `live_inspect`) | ~7 MB |
| DUPLICATE / COMPACTED (`tiny_end_to_end` 30→0.04 MB; `l23_canonical_population` 8.5→0.001 MB) | ~38 MB |
| ARCHIVE_ONLY (obsolete phase docs, WIP backup) | ~1+ MB |
| Handoff survivors + obsolete reports | ~10 MB |
| Gazetteer zips | ~1+ MB |
| DEAD_CODE (`phase_l/l13_rescue.py`) | small |

## Preserved

- `data/l23_canonical_population_store.json` — **1,465** opportunities
  - READY_TO_CALL **18**
  - DEEP_RESEARCH_COMPLETE **376**
  - WATCH_FEDERAL_ACCESS **673**
- `artifacts/phase_l/accessible_latest.json`
- L.18–L.23.1 CURRENT operational artifacts
- `data/jurisdiction_procurement_registry.json`
- transactional evidence / packets / deal packets
- buyer/product/supplier intelligence under `data/`
- Call desk workspaces / sheets

## Consolidated

| Old | New |
|-----|-----|
| Full `l23_canonical_population.json` duplicate | `data/l23_canonical_population_store.json` + compact summary |
| 30 MB `tiny_end_to_end_results.json` | ~37 KB representative fixture |
| `_t` / `_temp_*` PDF clones | `transactional_procurement_evidence/` only |
| Hundreds of obsolete `phase_*.md` | `CURRENT_M3_ARCHITECTURE.md` + `PHASE_HISTORY_CHANGELOG.md` |

## Canonical runtime

- One funnel: L.23 / L.23.1
- One opportunity store: `data/l23_canonical_population_store.json`
- One call workflow: L.22
- Intelligence preserved: **yes**
- `save_store()` no longer writes full population copies into artifacts/

## Tests

- L.21 / L.22 / L.23 / L.23.1: **54 passed**
- SAM credits consumed: **0**

## Remaining large files (intentional)

1. `data/jurisdiction_procurement_registry.json` (~22 MB) — master data; future SQLite optional
2. `artifacts/phase_l/accessible_latest.json` (~20 MB) — current live feed
3. `data/l23_canonical_population_store.json` (~8.6 MB) — canonical store
4. Source registries / L.22 workspaces / evidence PDFs — CURRENT or PERMANENT

## Packaging

- `.gitignore` updated for caches, temps, probes, gaz zips
- `packaging_exclusions.txt` excludes `.git/`, caches, WIP, temp artifacts
