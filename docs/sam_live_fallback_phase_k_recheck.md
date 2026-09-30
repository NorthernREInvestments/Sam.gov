# SAM Live Fallback — Phase K Recheck

**Constraint:** `DEVELOPMENT_NO_OUTREACH`  
**Mode:** Retrieval validation only (not a new deep-research phase)  
**API state:** `SAM_API_AUTH_FAILED` on all four (personal key still invalid)  
**Fallback:** Public SAM detail + attachments used successfully

Business readiness from Phase K is **preserved** unless new live evidence contradicted it.

---

## Pipefitter toolkit — NSN 5180-00-596-1509 (FIRST)

| Field | Result | Evidence |
|-------|--------|----------|
| API | `SAM_API_AUTH_FAILED` | Opportunities API 401 |
| Fallback path | Public detail → attachments | `LIVE_ATTACHMENT_CONFIRMED` |
| Live/open | **OPEN** | Public status `published`, not archived |
| Deadline | **2026-10-09T15:30:00-04:00** | Public solicitation deadlines |
| Qty | **40 EA** | Base PDF schedule `Toolkit (PSTK)\n40\n180 Days` |
| Option qty | **40** | Same schedule / 100% option CLIN 0003 |
| UOM | **EA** | Toolkit schedule (kit = each) |
| Amendment | W912CH26BA015-0001.pdf retrieved | Deadline extension confirmed |
| Set-aside | **SBA** (Total Small Business) | Public detail `setAside=SBA` + description TDP Dist A |
| TDP | Distribution code **A** | Description / IFB text |
| Prior Phase K data | **Confirmed** | Matches Oct 9 / 40+40 / SB / TDP-A |
| Business state | **`VERIFY_SUPPLIER_CHANNEL`** | Unchanged (IPI/TDP kit-build; SB eligibility unknown) |

**Pipefitter data confirmed by live public-source fallback without personal API key.**

---

## Diesel engine — NSN 2815-01-536-9262

| Field | Result |
|-------|--------|
| API | `SAM_API_AUTH_FAILED` |
| Fallback | `LIVE_ATTACHMENT_CONFIRMED` (AMD 0001 + PD PDF retrieved) |
| Live/open | OPEN |
| Deadline | **2026-09-28T17:00:00-04:00** |
| Qty/UOM | Still **unconfirmed firm** (requirements estimates; not a single CLIN buy) |
| Business state | **`VERIFY_SOURCE_APPROVAL`** (HATZ / auth path — unchanged) |

---

## Transmission kit — NSN 2520-01-682-2226

| Field | Result |
|-------|--------|
| API | `SAM_API_AUTH_FAILED` |
| Fallback | `LIVE_PUBLIC_WEB_CONFIRMED` |
| Live/open | OPEN |
| Deadline | **2026-10-09T13:00:00-04:00** |
| Qty/UOM | **225 EA** from public description (`225 EA` snippet) — estimated annual basis |
| Note | Phase K previously cited Amd 0001 guaranteed min **180**; description still shows **225** estimate. No silent overwrite of business demotion. |
| Set-aside | SBA |
| Business state | **`NOT_EXECUTABLE`** (unchanged — TDP-D / capital) |

---

## Turbine support — NSN 2840-00-411-8852

| Field | Result |
|-------|--------|
| API | `SAM_API_AUTH_FAILED` |
| Fallback | `LIVE_ATTACHMENT_CONFIRMED` (amendment PDF listed/retrieved) |
| Public open flag | Still `published` / not archived on SPA detail |
| Deadline | **2026-09-14T15:00:00-05:00** (past as of recheck) |
| Qty | Not confirmed from attachment text extract |
| Soft conflict | Listing still “OPEN/published” while response deadline has passed |
| Business state | **`EXPIRED_DURING_VERIFICATION`** (unchanged) |

---

## Source conflicts

| Deal | Conflict notes |
|------|----------------|
| Pipefitter / Turbine / Diesel | `amendment` field candidates differ by string form (version metadata vs attachment filename) — flagged, not material to qty/deadline |
| Turbine | Published flag vs past deadline — prefer deadline → expired |
| Transmission | 225 EA description estimate vs prior 180 guaranteed-min research — prefer amendment when downloaded for economics; business state not raised |

---

## Summary

| Deal | API | Fallback confirmation | Live open | Deadline | Qty/UOM | Final state |
|------|-----|----------------------|-----------|----------|---------|-------------|
| Pipefitter | AUTH_FAILED | ATTACHMENT | OPEN | 2026-10-09 | **40 EA + 40 opt** | `VERIFY_SUPPLIER_CHANNEL` |
| Diesel | AUTH_FAILED | ATTACHMENT | OPEN | 2026-09-28 | unconfirmed firm | `VERIFY_SOURCE_APPROVAL` |
| Transmission | AUTH_FAILED | PUBLIC_WEB | OPEN | 2026-10-09 | 225 EA (est.) | `NOT_EXECUTABLE` |
| Turbine | AUTH_FAILED | ATTACHMENT | published* | 2026-09-14 past | unconfirmed | `EXPIRED_DURING_VERIFICATION` |

\*Published on public detail but past response deadline → expired for execution.

## Artifact

`artifacts/phase_k/live_verification.json` refreshed by `scripts/run_phase_k_live_verification.py`.
