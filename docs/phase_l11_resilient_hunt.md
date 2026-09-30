# Phase L.11 — Resilient Live Hunt

## Problem

L.10 `--refresh-hunt` hung inside monolithic `run_live_discovery` when a BidNet/source socket stalled.

## Solution

`phase_l.resilient_hunt`:

1. **Per-source process watchdog** — child process fetch; parent `terminate()` on timeout → `SOURCE_TIMEOUT`
2. **Checkpoint after each source** — `data/phase_l11_hunt_checkpoint.json` (resume-safe)
3. **Source health persistence** — `data/phase_l11_source_health.json`
4. **Circuit breaker** — skip sources with repeated failures/timeouts
5. **Productive-first ordering** — BidNet TX/OH/MA… before weak states
6. **Hunt statuses** — `COMPLETE` | `COMPLETE_WITH_SOURCE_FAILURES` | `PARTIAL` | `FAILED`
7. **Prior accessible merge** — if live unique ≪ prior corpus, merge rather than wipe

Wired into `hunt.discover_live_sources` for `commercial_feed` / `l4` / `commercial` / `resilient` profiles.

## Observed (this phase)

Fresh resilient hunt finished **`COMPLETE_WITH_SOURCE_FAILURES`** (no hang).

BidNet public open-bids currently returns **HTTP 202 empty body** (anti-bot) → 0 listings / `PARSER_SCHEMA_CHANGE` symptom. Classified `anti_bot` — not fabricated as healthy empty.

## Audit alignment ([Audit hunt crawl resilience](e57047e4-d25b-4971-b43e-b237f87f5685))

| Recommendation | Status |
|----------------|--------|
| Per-source watchdog around `fetch_listing` | Done — `resilient_hunt` for commercial_feed + `source_wall_clock_s` in `live_runner` |
| Checkpoint / resume | Done — `data/phase_l11_hunt_checkpoint.json` |
| Phase L commercial budget profile | Done — `PROFILE_PHASE_L_COMMERCIAL` (`phase_l_commercial`) |
| Do not rely on ThreadPool hunt timeout | Done — process isolation |
