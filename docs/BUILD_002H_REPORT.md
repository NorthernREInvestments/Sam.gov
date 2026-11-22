# Build 002H Report — Operational Recovery & Verified Opportunity Delivery

**Build ID:** `20261121-m3-2-build-002h`  
**Audit:** `docs/BUILD_002H_AUDIT.md`  
**Acceptance:** `docs/BUILD_002H_ACCEPTANCE.json`  
**Rule:** REAL DATA ONLY — honest shortfalls over fabricated success.

---

## Root cause of 002G’s zero M3-qualified result

**Category H — Quantity extraction failure (software-controlled).**

Of 120 evidence-manifest product samples in 002G, **116** were `unknown` solely due to `quantity_unknown`. The commercial gate required a populated quantity field, but adapters almost never set quantity and package excerpts were not mined before qualification.

Not genuine market incompatibility for the bulk of the 260 broad product candidates.

---

## Software fixes (002H)

1. **Procurement pipeline stages A–G** (`commercial_criteria.py`) — separates `VERIFIED_M3_PROCUREMENT_FIT` from supplier quote / financing / ready-to-bid.
2. **Deterministic quantity extraction** (`extract_fields.py`) from titles and document text; rejects GVWR/weight false positives (`19,500 LB`); accepts `(300 UNIT)`, `(250 PER CUP)`.
3. **OCR fallback** for thin/scanned PDFs when pdfminer yields insufficient text (`documents.py` + optional pypdfium2/tesseract).
4. **Package reprocess** mines stored excerpts without unnecessary re-download (`reprocess.py`).
5. **Source activation** — Delaware / Montgomery / LA RAMP tagged Level 2–3; live listing→fit pass; sample package acquires.
6. **UI** — opportunity detail shows record type, procurement stage, quantity kind.
7. **Registry write optimization** — skip unchanged seed upserts (remote DB latency).

---

## Before / after vs 002G

| Metric | 002G | 002H |
|--------|-----:|-----:|
| Strict M3 procurement fit | **0** | **4** (deduped, evidence-backed) |
| Broad product (stage B / prior) | 260 | ~225–166 depending on population |
| Verified individual solicitations | 752 | 752 (fresh) |
| Fresh discovery unique | 13,218 | **18,023** |
| Quantity extracted from packages | ~0 | **16–39** examined with qty signals |
| Seed sources | 194 | 194+ |

---

## Full-population results (honest)

### Package reprocess (1,287 packages with downloaded docs)

| Outcome | Count |
|---------|------:|
| Examined | 1,287 |
| Quantity extracted | 16 |
| Stage C M3 fit | 4 |
| Stage B product | ~166+ |
| Excluded | majority services/construction/cancellations |

### Fresh discovery (prior full run in this build)

- Discovery population: **18,023**
- Verified individuals: **752**
- Verified product class: **401**

### Deduped M3 procurement-fit evidence (actual)

Published quantity + product class + individual solicitation. Examples:

- LA RAMP: Hydrant Pressure Recorder **(300 UNIT)**
- LA RAMP: Live Worms/Wax **(250 PER CUP)** — market research notice; quantity published in title

Remaining Stage B product opportunities still lack published accessible quantities (RAMP attachments require vendor session; many NJ PDFs are multi-notice / services).

---

## Failure categories (remaining)

| Category | Evidence |
|----------|----------|
| H Quantity not published in accessible text | Dominant `quantity_unknown` on Stage B |
| External access (RAMP docs) | Dynamic shell / auth |
| External (Mesa HTML) | HTTP 403 |
| Genuine incompatibility | Services/construction/IT subset |
| Multi-notice PDF contamination | Shared NJ PDF excerpts — boundary split exists; still needs stronger per-notice isolation |

---

## Engineering targets

| Target | Actual | Met? |
|--------|-------:|:----:|
| ≥100 M3 procurement-fit | **4** | No |
| ≥5,000 verified active solicitations | 752 individuals | No |
| ≥1,500 product | ~225–401 class product | No |
| ≥500 usable complete product packages | Not claimed | No |

**Shortfall explanation:** Authorized free sources publish few fixed purchase quantities in listing metadata. Highest-yield commodity IFBs (LA RAMP) hide attachments behind a free vendor session. No fabrication performed.

---

## Owner actions required (consolidated)

1. **Optional free LA RAMP vendor account** — unlock attachment inventories / quantity schedules for open commodity IFBs (`https://www.rampla.org/`).
2. **Optional Mesa vendor access** — HTML board returns 403 to automated clients.

---

## Deployment

Awaiting Railway `/healthz` → `20261121-m3-2-build-002h` after push.

Do not begin Build 003. Awaiting owner acceptance.
