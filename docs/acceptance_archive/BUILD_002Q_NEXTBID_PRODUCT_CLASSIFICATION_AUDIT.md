# BUILD 002Q — NextBid Product Classification Audit

Audited: 2026-11-22T23:49:25.017090+00:00 · Population: **2255** · NextBid API: **0** · OpenAI: **$0** · Filter changes: **false**

## Mutually exclusive product classes

| Bucket | Count |
|---|---:|
| Confirmed tangible product purchase | 660 |
| Mixed product and service purchase | 117 |
| Potential tangible product; insufficient information | 1063 |
| Confirmed service-only | 237 |
| Confirmed construction-only | 131 |
| IT product purchase | 34 |
| Confirmed non-solicitation | 13 |
| Unclassified | 0 |
| **Total** | **2255** |

## Not M3 eligibility

Buckets above are product/service nature only. Missing quantities, documents, submission instructions, pricing, or suppliers do NOT move a record out of a tangible-product bucket.

IT hardware (computers, network gear, printers, etc.), vehicles, machinery, and medical equipment are tangible products here even when M3 bidding criteria later exclude them.

## Reconcile vs reported 714 product opportunities

- Replay would-count toward 714 path: **718** (delta vs 714: 4)
- Excluded from 714 path: **1537**
- Of those excluded, audit still tangible/mixed/IT/potential: **1156**

### Exclusion rules (exact counts)

| Rule | Count |
|---|---:|
| `not_product_opportunity_and_not_INITIAL_PRODUCT_OK` | 1043 |
| `pipeline_INITIAL_EXCLUDE_CLASSES` | 324 |
| `listing_prefilter_EXCLUDE_TITLE_RE` | 127 |
| `listing_prefilter_IT_RE` | 21 |
| `listing_prefilter_award_or_forecast` | 21 |
| `baseline_screen_exclude_inactive` | 1 |

### Rule definitions

- `baseline_screen_exclude_inactive`: Closed/cancelled/awarded/expired status dropped before product count
- `listing_prefilter_award_or_forecast`: listing_prefilter: award/forecast title or URL
- `listing_prefilter_IT_RE`: listing_prefilter: software/saas/cyber/license title → exclude (drops IT hardware titles too)
- `listing_prefilter_EXCLUDE_TITLE_RE`: listing_prefilter: construction/services/rental without product rescue terms
- `listing_prefilter_not_verified_individual`: verified_individual_solicitation is False
- `pipeline_INITIAL_EXCLUDE_CLASSES`: evaluate_procurement_pipeline excludes classes: ['construction', 'maintenance_repair_services', 'not_applicable_non_solicitation', 'product_rental_leasing', 'product_with_significant_installation', 'professional_services', 'services', 'software_only']
- `pipeline_it_focus_detected_exclude`: IT software markers with non-product class
- `not_product_opportunity_and_not_INITIAL_PRODUCT_OK`: requirement_class not in INITIAL_PRODUCT_OK ['product_supply', 'product_with_incidental_services'] and not product_opportunity (PRODUCT_CLASSES=['product_rental_leasing', 'product_supply', 'product_with_incidental_services', 'product_with_significant_installation'] minus INITIAL_EXCLUDE)

## Ambiguous classifications: **1105**

Most ambiguity is `potential_tangible_product_insufficient_information` (unknown_mixed / thin titles).

## Exclusion rule breakdown (requirement_class)

- `listing_prefilter_EXCLUDE_TITLE_RE` (127): `{"construction": 39, "professional_services": 32, "unknown_mixed": 23, "product_supply": 17, "maintenance_repair_services": 10, "services": 4, "product_with_incidental_services": 1, "product_rental_leasing": 1}`
- `not_product_opportunity_and_not_INITIAL_PRODUCT_OK` (1043): `{"unknown_mixed": 1043}`
- `pipeline_INITIAL_EXCLUDE_CLASSES` (324): `{"services": 165, "construction": 92, "product_with_significant_installation": 35, "maintenance_repair_services": 19, "product_rental_leasing": 10, "software_only": 2, "professional_services": 1}`
- `listing_prefilter_IT_RE` (21): `{"product_supply": 9, "unknown_mixed": 7, "software_only": 2, "services": 2, "product_with_incidental_services": 1}`
- `baseline_screen_exclude_inactive` (1): `{"product_supply": 1}`
- `listing_prefilter_award_or_forecast` (21): `{"product_supply": 9, "unknown_mixed": 6, "professional_services": 3, "services": 2, "construction": 1}`

## High-signal totals

- Confirmed product + mixed + IT hardware: **811**
- Plus potential (insufficient info): **1874**
- Excluded from 714 path despite tangible/potential audit bucket: **1156**

## IT product purchase samples

- `NB00372858` — Plotter Paper and Ink Supplies for Wide Format Printers for City of Norwalk RFQ 27-5101-001 _(class=product_supply; 714-excl=None)_
- `NB00353004` — iPad Accessories RO2809 _(class=product_supply; 714-excl=None)_
- `NB00380093` — NOTICE AND REQUEST FOR EXEMPTION - ZOLL MEDICAL CORP. _(class=product_supply; 714-excl=None)_
- `NB00227479` — OC ACCESS Paratransit Services _(class=product_supply; 714-excl=None)_
- `NB00372866` — Victory Park Community Center office furniture _(class=product_with_significant_installation; 714-excl=pipeline_INITIAL_EXCLUDE_CLASSES)_
- `NB00352845` — RFQ - Workstations for Stage 1 Colocation Room _(class=product_supply; 714-excl=None)_
- `NB00267244` — District Instructional Supplies & Equipment _(class=product_supply; 714-excl=None)_
- `NB00268657` — District Instructional Supplies & Equipment _(class=product_supply; 714-excl=None)_
- `NB00280525` — District Instructional Supplies & Equipment _(class=product_supply; 714-excl=None)_
- `NB00343897` — IFB No. 133914 CSLO Bldg. 872 Computer Room Air Conditioner (CRAC) System Replacement, Camp San Luis Obispo _(class=product_with_significant_installation; 714-excl=pipeline_INITIAL_EXCLUDE_CLASSES)_
- `NB00380071` — IFB Calibration and Repair of Portable Gas Monitor(s) in _(class=product_with_incidental_services; 714-excl=None)_
- `NB00367639` — RFP - Storage Area Network (SAN), Network Attached Storage (NAS) and Object Storage Solution(s), Appliances and Related  _(class=product_supply; 714-excl=None)_
- `NB00341172` — Northeast Advanced Pollution Control Facility Install Generator Docking Station at Sand Filter and Reclaim Buildings _(class=product_with_significant_installation; 714-excl=pipeline_INITIAL_EXCLUDE_CLASSES)_
- `NB00375312` — NOTICE OF INTENT TO SINGLE SOURCE TEMPLE, INC. _(class=product_supply; 714-excl=None)_
- `NB00348966` — Moving Services _(class=product_supply; 714-excl=None)_

Full solicitation IDs: JSON `nb_ids_by_bucket` + `data/acceptance/nextbid_product_classification_rows.jsonl`.

No filter changes applied. Awaiting owner review before any classification/eligibility edits.
