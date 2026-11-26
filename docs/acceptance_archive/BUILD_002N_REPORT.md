# BUILD 002N — Quantity Extraction & Solicitation Document Recovery

**Status:** AWAITING OWNER ACCEPTANCE  
**Build:** 002N  
**Started:** 2026-11-22T04:38:07.510040+00:00  
**Finished:** 2026-11-22T04:43:52.723887+00:00  
**Metered NextBid calls:** 0 (LOCKED)

## NextBid lock

- Locked at start: `True`
- Locked at end: `True`
- Killswitch engaged: `True`
- Owner authorization: `None`

## OpenAI integration verification (live evidence)

- API credentials configured: `True` (value never logged)
- Paid research enabled: `True`
- Policy default model: `gpt-4o-mini`
- Live request OK: `True`
- Model used: `gpt-4o-mini`
- Response id: `resp_00457bb42e9229ae006b0271b2b2e487d18c382dd6a04f2df6`
- Tokens in/out: `276` / `149`
- Text chars sent (smoke): `194`
- Parsed smoke quantity/semantic: `15` / `ORDERED_QUANTITY`
- Estimated cost (smoke): `0.0001`
- Silent regex-only fallback: `False`

### OpenAI quantity extraction execution

- Successful requests: **17**
- Failed requests: **1**
- Budget denied: `1` (daily ≤$1 ceiling; remaining ~403 long-text targets deferred — **not** silently regex-fallbacked)
- Applied firm quantities at call time: `2` (both later rejected in correction as fund/services — not Stage C)
- Applied estimated (not Stage C): `0`
- Job spend estimate USD: `0.0095`
- Silent regex fallback: `False`
- Correction pass: deterministic extractor fixes (spelled hundreds, singular false positives, service-title rejects) with **zero** additional OpenAI spend

## Inventory results

| Metric | Count |
|--------|------:|
| Product opportunities investigated | 732 |
| Audit-710 IDs covered | 710 |
| Quantities successfully recovered (any semantic) | 18 |
| New Stage C fits (firm ordered only) | 14 |
| Firm ordered quantity verified | 14 |
| Estimated quantity | 4 |
| Indefinite purchase commitment | 0 |
| Quantity absent from complete documents | 316 |
| Missing document | 196 |
| Extraction failure | 0 |
| Source access restricted | 202 |
| Confirmed procurement incompatibility | 0 |
| Long text (≥400 chars) after recovery | 430 |
| Complete packages | 5 |
| Partial packages | 10 |
| Restricted packages | 210 |
| Promising working queue | 718 |

## Free public document recovery

- Processed: `710`
- Metered NextBid: `0`
- OK: `True`
- Stats: `{"COMPLETE": 3, "PARTIAL": 10, "ACCESS_RESTRICTED": 198, "VERIFICATION_REQUIRED": 129, "FAILED": 370, "downloaded_docs": 61, "identified_docs": 73, "errors": 365}`

## Existing Stage C verification

- Examined: `26`
- Kept Stage C: `20`
- Demoted (no longer firm-eligible): `6`

## Standards preserved

- Estimated / ambiguous quantities are **not** represented as firm Stage C.
- Missing information is **not** classified as confirmed disqualification.
- No quantities invented.
- Model years, packaging, dimensions, capacities, weights rejected as ordered qty.
- Every previously acquired opportunity / document / source id preserved.
- Individual accounting: `data/acceptance/build002n_individual_accounting.json` (732 rows)
- Stage C evidence: `data/acceptance/build002n_stage_c_evidence.json`
- Promising queue: `data/acceptance/build002n_promising_queue.json`

## New Stage C evidence (summary)

- `NB00348901` qty=12 sem=ORDERED_QUANTITY — C26RI103317777R - REBID - Invitation For Bids (Non- Negotiable) - Furnish and De
- `NB00321554` qty=1 sem=ORDERED_QUANTITY — PURCHASE OF ONE (1) 6” HIGH PRESSURE DIESEL PUMP  WITH A TRAILER
- `NB00345848` qty=2 sem=ORDERED_QUANTITY — Two (2) Animal Transport Vehicles
- `NB00370595` qty=1 sem=ORDERED_QUANTITY — Purchase of one Rebuilt Caterpillar 836G landfill compactor utilizing a trade-in
- `NB00339475` qty=2 sem=ORDERED_QUANTITY — Two (2) New/Unused 2026 or Newer Hybrid Full Size Light Duty four (4) door pick 
- `NB00365477` qty=2 sem=ORDERED_QUANTITY — Police Department Off Highway Vehicles (OHV)
- `NB00338603` qty=160 sem=ORDERED_QUANTITY — Furnish one hundred sixty (160) Chemical Flow Meters for the Metropolitan Water 
- `NB00260314` qty=1 sem=ORDERED_QUANTITY — Towable Air Start Unit
- `NB00261760` qty=1 sem=ORDERED_QUANTITY — BL081-24 Purchase of a Swift Water Rescue Boat
- `PE-74600-NONST-2027-000000031` qty=5 sem=ORDERED_QUANTITY — 2026-011 Five (5) New Custom Class A Pumper Apparatus
- `PE-61700-NONST-2027-000000082` qty=1 sem=ORDERED_QUANTITY — Pumper Fire Truck
- `NB00375434` qty=7 sem=ORDERED_QUANTITY — East Penn Manufacturing Company Station Batteries (7 units) RFB-LB-481905
- `NB00372789` qty=1 sem=ORDERED_QUANTITY — Purchase and Delivery of Intelligent Auto-Flusher
- `NB00362602` qty=8 sem=ORDERED_QUANTITY — F125 Wheel Sets & BTR Wheel Assemblies IFB No. PO1071-27

---
Awaiting owner acceptance. Build 003 not started. NextBid remains locked.
