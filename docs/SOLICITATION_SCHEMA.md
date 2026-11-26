# Solicitation Understanding Schema

Permanent information model for M3. Every critical field uses an explicit state:  
`VERIFIED` | `UNKNOWN` | `CONFLICTING` | `NOT_APPLICABLE` | `ESTIMATED`.

AI may propose values; **deterministic verification** (`ai_output_verify.py`) and readiness gates decide promotion.

## Identification

| Field | Purpose | Typical source | Storage / UI |
| --- | --- | --- | --- |
| `official_solicitation_number` | Agency number | get_opportunity / body | inventory + detail |
| `provider_id` (`nb_id`) | NextBid id | provider | PK association |
| `title` | Listing title | provider | inventory |
| `issuing_agency` | Buyer | provider / docs | inventory |
| `issuing_department` | Sub-org | provider | optional |
| `procurement_level` | federal/state/local/other | inferred carefully | research |
| `agency_location` | Agency seat (≠ delivery) | docs | research |
| `procurement_type` | IFB/RFQ/RFP/… | notice | research |
| `original_url` / `additional_urls` | Official links | `source_url` | inventory |
| `posted_at` / `updated_at` / `lifecycle_status` | Timing/status | provider | inventory |

## Dates

Posting, submission deadline (+ time + **source timezone if stated**), questions, site visit, pre-bid, substitution, award estimate, delivery, performance period, amendment dates.  
Never invent timezone/time. Ambiguous → preserve source language + `UNKNOWN` normalized time.

## Products / line items

CLIN/line #, name, description, manufacturer/brand/model/MPN/NSN, quantity, UoM, package/case qty, dimensions, material, capacity, voltage, standards, certifications, condition, accessories, options, substitutions (BNE / brand-only), warranties, delivery notes.

**Quantity rules:** firm vs estimated vs IDIQ / as-needed; never treat ceiling as guaranteed revenue; never silent unit conversion without evidence.

## Award structure

Line-item / lot / region / all-or-none / multiple / partial / alternate. Partial readiness ≠ full solicitation ready.

## Delivery & fulfillment

Addresses, dates/lead time, Incoterms/freight, packaging/pallet/label, appointments, inside/liftgate, inspection/acceptance, installation/assembly flags.

## Eligibility

Set-asides, registrations, licenses, manufacturer auth, domestic sourcing, insurance/bonding, experience, vehicles, mandatory meetings.

## Additional obligations

Installation, construction, maintenance, removal, training, testing, commissioning, ongoing support. Distinguish ordinary manufacturer warranty from mandatory service.

## Submission

Method/portal/email, forms, signatures, certifications, validity, amendment acknowledgment, bid security.

## Document coverage (separate from fields)

Per document: discovery → download → extract → analyze → verify; package labels from `document_inventory.package_completeness` — **never text-length thresholds**.

## Evidence binding

Critical findings must cite document id/version, page/sheet/row, or structured provider field that actually exists in stored evidence.
