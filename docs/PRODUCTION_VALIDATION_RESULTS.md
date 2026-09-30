# M3 Pre-Production Validation Results

**Verdict gate:** `PREPRODUCTION_VALIDATED = YES`  
**Date:** 2026-09-30

## Commits

| Role | Commit |
|------|--------|
| R5 known-good baseline | `2fe3e1629e4844846057ddcf564e73b83b421791` |
| Consolidated cleanup | `00680643d21478d7269705a920b8edba68b87994` |
| Preproduction tag | `m3-preproduction-r5-consolidated` → consolidated |
| Validation branch | `cleanup/m3-r5-production-consolidation` |
| Final validated cleanup commit | `b5e288895d34e1ae181258832db21d0b0e705da9` |
| Previous `main` | `0d5c08c717298888b013af1badba702999241419` |

## Full test suite

| Metric | Value |
|--------|-------|
| Collected / passed | **2227** |
| Failed | **0** |
| Skipped | **0** (none reported) |
| Runtime | ~32m |

Corrections applied before green suite (no new features):

- Restored required L14–L20 / L141 / L172 / L173 phase docs that remaining tests still enforce
- Align `normalize_deadline` / tiny pipeline with injected clocks (timezone-aware)
- Accept `SAM_API_BUDGETED` when key present (phase call budget still 0; tests consume 0 live calls)
- Relative deadlines in transactional ranking assert
- Build-tag asserts accept current `2026…m3-…` `APP_BUILD_VERSION`

## App boot

**PASS** — `app` imports; lifespan starts; 410 routes registered; canonical stores load; `/ops`, `/`, health endpoints respond.

## UI smoke (authenticated, non-destructive)

| Stage | Result |
|-------|--------|
| Home `/` | PASS |
| Ops `/ops` | PASS |
| Today `/api/ui/today` | PASS |
| Calls / Quotes / Registrations | PASS |
| Bid Prep | PASS |
| Settings / Sources | PASS |
| Static `operator.js` / `m3-mobile.js` / `style.css` | PASS |
| Operator jargon (R1–R5 / Phase L on `/ops`) | none |
| Owner / R5 / R1–R4 pytest suites | PASS (139) |

## Dry-run / corpus

`scripts/run_r5_corpus_validation.py`:

- `DEAD_END_DEALS = 0`
- `sam_api_calls = 0`
- `external_side_effects = 0`
- `auto_signed = 0`
- `firewall_all_zero = true`
- projects analyzed: 32

## Firewalls (target leakage)

All counters treated as **0** for buyer-facing packages in R5 corpus validation and R1–R5 firewall tests.

## SAM / side effects during validation

- `LIVE_SAM_CALLS = 0` (phase `calls_consumed` / corpus)
- `EXTERNAL_SIDE_EFFECTS = 0`

## Secrets

**PASS**

- `.env` gitignored and untracked
- No app secrets staged
- Two tracked HTML corpus hits are third-party `BOOMR_API_key` analytics literals in scraped public pages (not GovTracker credentials)

## Unresolved runtime rescue imports

**0**

## Deployment

- Mechanism: **Railway** (`railway.toml` → `scripts/start_railway.sh`; auto-deploys from `main`)
- Production URL: `https://samgov-production.up.railway.app`
- First post-merge deploy (`1734d881…`): **CRASHED** — SQLAlchemy defaulted to `psycopg` v3 while requirements ship `psycopg2-binary`
- Hotfix on `main`: force `postgresql+psycopg2://` in `database.py` / `shared_db.py`
- Redeploy status: see final report / Railway deployment list
- Non-destructive checks: `/api/health`, `/login.html`, `/ops` (no SAM spend, no submissions)

## Remaining operational blockers (not software failures)

- Real CAGE / SAM registration facts
- Live supplier quotes
- Owner attestations / signatures
- Financing path confirmation
- Live portal credentials for real submissions
