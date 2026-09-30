# Phase L.14.1 — L.14 Repair

**Build:** `20260928-m3-phase-l141-l14-repair-structured-source-audit`

## Artifact audit

All required L.14 artifacts and docs are **PRESENT** under `govtracker/artifacts/phase_l/` and `govtracker/docs/`.

The earlier `FileNotFoundError` for `artifacts\phase_l\l14_summary.json` was a **working-directory relative path** issue (python invoked outside `govtracker/`), not a missing file.

## 22-row collapse — exact causes

`22` = `live_runner.unique_records` only.

| Factor | Evidence |
|--------|----------|
| Only 4 productive adapters | Sourcewell 11 + HGAC 5 + Boston 4 + LAX 2 = **22** |
| Most portals ZERO_RESULTS / TIMEOUT | OpenGov CDN Cloudflare; Bonfire/IonWave empty; DIBBS empty |
| fed_sam in resilient runner | 0 rows (separate federal_public SAM search returned **500**) |
| Prior merge | accessible 864 → final **1062** |
| Not bad dedupe | Productive sums match unique; no mass collapse |

## Repair actions

- Clarified `l14_summary.json` / `l14_fresh_hunt.json` with live vs inventory split
- Always merge prior accessible when larger than live unique (hunt.py)
- Freshness labels: `LIVE_FRESH` / `LAST_KNOWN_RECENT`
- Structured source inventory + park fragile JS portals
- BidNet auth history remains parked

## Stop

No L.15. No Phase M. No accounts. No outreach.
