# Build 002J — Forensic API Consumption Audit

**Build ID:** `20261122-m3-2-build-002j`  
**Rule:** ZERO metered NextBid calls during this audit.  
**Acquisition:** LOCKED fail-closed.  
**Provider (free `/v1/usage`):** used **3054** / remaining **2946** / allowance **6000**.

## Root cause

1. **No executable hard daily/job ceiling** — 002I soft buckets (600/2400/2000/1000) still allowed >2,000 calls in one session.
2. **Mass detail + document enrichment** before strong listing-level exclusion.
3. **Post-completion document pass** — ~646 metered calls after acceptance printed, **0** new Stage C fits.
4. **Production scheduler** could auto-start ~15s after deploy (now blocked by fail-closed lock).
5. **No persistent per-call ledger** in 002I — retries/duplicates hard to prove per opportunity.

## Local job accounting (evidence)

| Job | Remaining before → after | Used |
|-----|--------------------------|-----:|
| 002I acceptance run | 5999 → 4301 | **1698** |
| 002I doc pass (AFTER “completion”) | 4301 → 3655 | **646** |
| **Local sum** | | **2344** |

| Source | Used |
|--------|-----:|
| Owner / provider current used | **3054** |
| Unexplained gap (3054 − 2344) | **710** |

Gap status: **NOT fully attributed from available local logs**. Likely contributors: production scheduler post-deploy and/or retries. Free `/account/activity` sample returned **0 rows** in this environment (endpoint shape/empty) — cannot invent call history.

### First-job endpoint distribution (acceptance artifact)

| Endpoint | Calls |
|----------|------:|
| search | 61 |
| get_opportunity | 661 |
| list_documents | 270 |
| list_files | 237 |
| read_file | 404 |
| **Sum** | **1633** |

(Remaining ~65 of the 1698 delta may be retries/unlisted meter variance.)

## Why 2,344 (local) / 3,054 (provider) → only 6 claimed / 3 recovered fits

- Enrichment was treated as “acquire everything promising,” not “spend only after listing relevance.”
- Stage C requires defined ordered quantity; **304** Stage B products still lack it in accessible text.
- Document pass did not unlock quantities → wasted spend.
- Offline 002J reprocess with current gates recovers **3** strict fits from paid data (not 6). Prior free-source fits (3) are separate.

**Approx calls per claimed 002I fit:** 2344/6 ≈ **391**; vs provider 3054/6 ≈ **509**. Unacceptable.

## Avoidable calls (evidence-based)

- Nearly all document endpoint spend after listing (list_documents + list_files + read_file + doc pass).
- Entire post-completion doc pass (**646**, 0 fits).
- Large share of `get_opportunity` for rows rejectable from title/tradeline alone.

## Preservation

Checkpoint `data/nextbid_checkpoints/nextbid_acquire_v1.json` retained. No opportunity/document data deleted.

## Do not resume

Paid acquisition remains locked. Owner authorization required before any metered call. Hard ceiling after auth: **≤100 calls/day**, **≤50/job**, **≥1000 monthly reserve**.
