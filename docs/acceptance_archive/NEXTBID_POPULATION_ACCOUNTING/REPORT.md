# M3 — Complete NextBid Inventory Bottleneck Accounting

- Ran: `2026-11-25T05:18:14.338602+00:00`
- Runtime: `31.24s`
- API expenditure: **ZERO**
- Killswitch locked: **True**
- Ledger: used 3454 / remaining 2546 / reserve 1000

## Population

- NextBid inventory records accounted: **8088**
- Prior offline-recovery NBs processed: **7951**
- Enrichment rows available: **12**
- Reconcile OK: **True**

## Primary classification (mutually exclusive)

- unknown: **4119**
- service: **1735**
- product: **1547**
- construction: **626**
- mixed: **61**

## Product evidence

- title_or_category_inference: **4704**
- not_product_or_unknown: **2922**
- confirmed_product_evidence: **462**

## Requirements completeness (mutually exclusive)

- unverified_no_retained_body: **6169**
- missing: **1723**
- partial: **196**

## Readiness & bottlenecks

- Confirmed product (body evidence): **462**
- Potential product (title/category inference): **4704**
- Verified quantities present: **142**
- Spec/model cues: **27**
- Supplier-quote-ready YES: **10**
- Supplier-quote PARTIAL (incl. title-only): **1537**
- Submission instructions in retained text: **82**
- Held back by missing source body only: **1237**
- Held back by extraction errors (post-fix brand-UOM): **0**
- Held back by eligibility/federal: **4330**
- Held back by retained closure/expiry: **56**

## Open status

_Offline evidence does not establish live open status._

- retained_open_claim_unverified_live: **7897**
- future_deadline_status_unverified: **80**
- expired_or_closed_in_retained_data: **56**
- status_unknown_unverified: **55**

## UOM defect fix

- Brand token captured as UOM (e.g. Unication)
- Fix: normalize_quantity_unit whitelist + brand reject in extract_fields
- Remaining brand-as-UOM in this pass: **0**

## Highest-value next acquisition

Do not broad-spend NextBid credits on the title-only majority. Highest value: (1) pursue supplier quotes for the independently verified YES items (Weg motor 27-P-00107; IL rock salt schedule) using retained requirements; (2) if one paid test is authorized later, prefer a single read_file of a category-C document0.md already indexed in enrichment (e.g. NB00375309), not a new search campaign. Offline evidence does not establish live open status.

**STOP FOR OWNER REVIEW.**
