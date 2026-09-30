# SAM Live Fallback Design

**Observed failure:** Phase K live verification treated `HTTP 401 Invalid Credentials` from the SAM Opportunities API as if live SAM verification itself were unavailable.

**Root cause:** The personal `SAM_GOV_API_KEY` Opportunities API was treated like a required single source instead of a preferred structured source.

**Fix:** `sam_live_fallback.py` implements a multi-source live retrieval chain with explicit API-failure codes, confirmation states, and per-field provenance. Phase K `verify_one` now calls `retrieve_sam_live` first.

---

## Core rule

| Role | Source |
|------|--------|
| `PREFERRED_STRUCTURED_SOURCE` | SAM Opportunities API (`api.sam.gov` + personal key) |
| Fallback live sources | Public SAM opportunity detail, listing page, attachments, agency URL |
| Non-current | Cached/frozen Phase G/H/I/J/K records → `STALE_CACHE_ONLY` |

An API failure is **not** an opportunity verification failure.

---

## Retrieval order

1. **SAM Opportunities API** (personal key) → `LIVE_API_CONFIRMED`
2. **Public SAM opportunity detail** (`https://sam.gov/api/prod/opps/v2/opportunities/{notice_id}` with public SPA umbrella key extracted from the listing HTML) → `LIVE_PUBLIC_WEB_CONFIRMED`
3. **Public SAM listing HTML** (`/opp/{id}/view`) — if SPA shell / captcha → `PUBLIC_WEB_BLOCKED`, continue
4. **Public solicitation attachments** (resources list + file download) → `LIVE_ATTACHMENT_CONFIRMED`
5. **Agency/buyer public URL** (if provided) → `LIVE_AGENCY_SOURCE_CONFIRMED`
6. **Stale cache** → `STALE_CACHE_ONLY` (comparison only; cannot satisfy live open/qty/deadline alone)

If all live paths fail → `LIVE_SOURCE_UNAVAILABLE`.

---

## Failure / confirmation states

**API failures (tracked separately):**

- `SAM_API_AUTH_FAILED`
- `SAM_API_RATE_LIMITED`
- `SAM_API_TIMEOUT`
- `SAM_API_OTHER_ERROR`
- `SAM_API_OK`

**Live confirmation:**

- `LIVE_API_CONFIRMED`
- `LIVE_PUBLIC_WEB_CONFIRMED`
- `LIVE_AGENCY_SOURCE_CONFIRMED`
- `LIVE_ATTACHMENT_CONFIRMED`
- `STALE_CACHE_ONLY`
- `LIVE_SOURCE_UNAVAILABLE`

**Other:**

- `PUBLIC_WEB_BLOCKED` — page blocked / SPA shell without facts (not a stop)
- `SOURCE_CONFLICT` — disagreeing field values; prefer latest amendment → attachment → public detail → API → stale cache
- `DISCOVERY_DEGRADED` — API unhealthy; hunts may continue via other paths at reduced efficiency (not `DISCOVERY_IMPOSSIBLE`)

---

## Provenance

Every resolved live field carries:

```text
value
source_type
source_url / ref
retrieved_at
snippet (optional)
```

---

## Public access rules

- Uses the same public SPA surfaces a browser uses (umbrella key embedded in public HTML).
- Does **not** bypass CAPTCHA, defeat anti-bot, spoof sessions, or circumvent auth walls.
- Personal API key remains useful for bulk structured discovery; it is **not** a prerequisite for live deal research.

---

## Integration points

| Module | Role |
|--------|------|
| `sam_live_fallback.retrieve_sam_live` | Shared façade |
| `scripts/run_phase_k_live_verification.py` | Phase K wired to façade |
| Future hunts | On API failure, report `DISCOVERY_DEGRADED` and continue supported non-API paths |

---

## Out of scope (this change)

- Automatic SAM key rotation
- Supplier outreach
- Broad hunt / Phase L
- Full-suite re-run (adapter-scoped; targeted tests only)
