# Phase G — Rejection Audit

**Run:** 2026-09-24  
**Sample:** all **11** rejected LIVE_SOURCE cases (fewer than 10 threshold → audit all)

## Method

Question asked for each reject:

> Did M3 reject a potentially viable reseller opportunity because of a parsing, document, UOM, history, supplier, funding, or classification error?

Safety rules were **not** weakened to inflate pass rate.

## Rejected cases

| # | Title | Class | Verdict |
|---|-------|-------|---------|
| 1 | AED Medical Oversight and Management Services | LIKELY_SERVICE | **Correct reject** — services |
| 2 | Award Notice SWIMMING POOL REHABILITATION WORK | Award / construction | **Correct reject** — award notice + rehab work |
| 3 | DI Instrument Manager Middleware - Intent to Award Sole Source | Intent to award / software | **Correct reject** — not open product RFQ |
| 4 | Vieques NWR Janitorial Services | LIKELY_SERVICE | **Correct reject** |
| 5 | Carolina Sandhills NWR Janitorial Services | LIKELY_SERVICE | **Correct reject** |
| 6–10 | Single Award Construction Contract (SACC) IDIQ (Batavia, Florence, Mega Hub DC, Miami, Ft Benning) | Construction IDIQ | **Correct reject** — construction, not product resale |
| 11 | USCG Sector San Diego CA Janitorial Services | LIKELY_SERVICE | **Correct reject** |

## False-rejection count

**0** in the audited reject set.

## Notes on non-reject held rows

Many non-product / UNKNOWN listings remain `HELD_FOR_ACTION` rather than hard-REJECT when the cheap screen preserves ambiguity (`plausible_tangible_or_unknown`). That is conservative (avoids false reject) but increases operator noise. Recorded as **P2** incompleteness in the live source gap register — not weakened into auto-reject without evidence.

## Conclusion

No material false-rejection pattern in hard rejects. Service/construction/award notices correctly excluded from the product-resale path.
