# Phase L.14.1 — Profile Audit

**Build:** `20260928-m3-phase-l141-l14-repair-structured-source-audit`

| Setting | Value |
|---------|-------|
| Profile | `non_bidnet` |
| max_sources (L.14 run) | 36 |
| kind caps | STATE 32 / LOCAL 40 / NETWORK 4 / COOP 16 / FEDERAL 8 |
| per-source wall clock | 75s observed |
| federal public max | 500 |

Yield loss after interleave fix is **portal failure**, not unexpected inventory truncation.

Recommendation: Tier1/2/3 first; 90s wall for coop HTML; 45–60s for APIs.
