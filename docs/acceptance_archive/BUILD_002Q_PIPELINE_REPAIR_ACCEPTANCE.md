# BUILD 002Q — Procurement Pipeline Repair Acceptance

Finished: 2026-11-23T01:17:50.834107+00:00 · Runtime: 27.4s
NextBid metered: **0** · OpenAI: **$0** · SAM: **0** · DIBBS: **0** · New sources: **0**

## Defects corrected

- listing_prefilter no longer hard-rejects IT/construction/maintenance title words
- unknown_mixed treated as potential product investigation, not reject
- INITIAL_EXCLUDE no longer includes significant_installation or rental at product stage
- goods title overrides mis-tagged service/construction class for investigation
- IT hardware recognized as tangible goods; software-only still separated
- supplies/materials beat bare service/construction title words in classify
- rental vs purchase disambiguated; mandatory install requires explicit phrase
- primary dispositions separate procurement fit from commercial/financing
- missing qty/docs/submission → information-needed queues, not confirmed DQ

## Before → After

| Metric | Before | After |
|---|---:|---:|
| Unique solicitations | 2255 | 8088 |
| Reported product opportunities / confirmed+potential product | 714 / 1874 | 2268+5041 |
| Excluded for uncertainty | 1043 | (see potential disposition) |
| Verified active (open/amended) | — | 2121 |
| Confirmed tangible product (pipeline) | 660 | 2268 |
| Potential tangible product | 1063 | 5041 |
| Verified quantities | — | 78 |
| Complete product specifications | — | 0 |
| Verified procurement eligibility | — | 1 |
| Easy vendor registration | — | 0 |
| Ready for supplier pricing | — | 78 |
| Ready for bidding | — | 1 |
| Information recovery | — | 7308 |
| Confirmed disqualifications | — | 777 |
| Closed / non-solicitation | — | 2 |

### Primary dispositions

| Disposition | Count |
|---|---:|
| verified_bid_opportunities | 1 |
| verified_product_information_needed | 2275 |
| potential_product_classification_needed | 5033 |
| easy_vendor_registration | 0 |
| confirmed_disqualification | 777 |
| closed_expired_non_solicitation | 2 |

Previously excluded potential-product path accounted in potential queue: **5033**

## Minimal blockers

`{"missing_quantity": 7231, "missing_submission_instructions": 7308, "eligibility_gate:tangible_products": 7242, "eligibility_gate:defined_quantities": 7242, "eligibility_gate:submission_instructions": 7106, "eligibility_gate:active_solicitation": 5677, "eligibility_gate:reseller_eligibility": 5100, "eligibility_gate:no_blocking_install_insurance_bond_license": 5128, "eligibility_gate:sufficient_documents": 5295, "classification_insufficient": 5052, "missing_or_restricted_documents": 4930}`

## Remaining data limitations

Low verified-bid counts reflect missing/restricted NextBid document payloads in stored packages (many ACCESS_RESTRICTED), not live API refusal. M3 cannot invent quantities from thin listings.
- Packages linked: 445 · Usable text: 341

All IDs reconciled: **True** (8088)

Awaiting owner acceptance.
