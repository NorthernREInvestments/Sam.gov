# SAM Live Fallback — Regression Report

**Date:** 2026-09-26  
**Suite run:** Targeted only (`tests/test_sam_live_fallback.py`)  
**Full suite:** **Not run** — changes limited to SAM retrieval adapter + Phase K wiring; targeted coverage is strong.

## Command

```bash
python -m pytest tests/test_sam_live_fallback.py -q
```

## Result

**14 passed / 0 failed**

## Cases covered

| # | Case | Result |
|---|------|--------|
| 1 | API success | PASS — `LIVE_API_CONFIRMED` |
| 2 | API 401 → public fallback succeeds | PASS — `LIVE_PUBLIC_WEB_CONFIRMED` |
| 3 | API timeout → public fallback succeeds | PASS |
| 4 | API failure → attachment confirms live data | PASS — `LIVE_ATTACHMENT_CONFIRMED` |
| 5 | API + public fail → agency source succeeds | PASS — `LIVE_AGENCY_SOURCE_CONFIRMED` |
| 6 | All live paths fail → `LIVE_SOURCE_UNAVAILABLE` | PASS |
| 7 | Stale cache cannot satisfy live status | PASS — `STALE_CACHE_ONLY`, `live_verified=False` |
| 8 | Source conflict prefers latest amendment | PASS — `SOURCE_CONFLICT` |
| 9 | API failure does not auto-block when fallback sufficient | PASS |
| 10 | Phase K pipefitter without API | PASS — open / deadline / 40 EA / SBA |
| + | Line-broken PDF schedule qty parse | PASS |
| + | Discovery degraded label | PASS |
| + | API 401 classifier | PASS |
| + | Toolkit UOM default EA | PASS |

## Code change note

**Observed failure:** SAM API 401 caused live verification degradation.  
**Root cause:** API treated as mandatory source.  
**Fix:** Multi-source live fallback with provenance (`sam_live_fallback.py`).  
**Regression test:** API failure + successful public/attachment retrieval.
