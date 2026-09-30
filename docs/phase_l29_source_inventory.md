# Phase L.2.9 — Source Inventory

| Source | Roles | Access | Format | Auth | Bot behavior | Categories |
|--------|-------|--------|--------|------|--------------|------------|
| SAM.gov | LIVE_OPPORTUNITY, PRODUCT_IDENTITY | public_api_html | html/api | none | rate_limited | ALL |
| USAspending | HISTORICAL_AWARD, HISTORICAL_GOV_PRICE | public_api | json | none | stable | ALL |
| DLA/DIBBS | LIVE_OPPORTUNITY, HISTORICAL_GOV_PRICE, PRODUCT_IDENTITY, MPN_CROSS_REFERENCE | public_html | html/pdf | none_or_cac | variable | MRO, AERO |
| GSA Advantage / eLibrary | CURRENT_GOV_CONTRACT_PRICE, PRODUCT_IDENTITY | public_html | html | none_for_catalog | variable | IT, MRO, OFFICE |
| NASA SEWP | CURRENT_GOV_CONTRACT_PRICE, HISTORICAL_GOV_PRICE | public_docs | html/pdf | none_for_public | stable | IT |
| OMNIA Partners | CURRENT_GOV_CONTRACT_PRICE, HISTORICAL_GOV_PRICE | public_html_docs | html/pdf/xlsx | none_for_public_docs | shell_possible | EQUIPMENT, VEHICLE, IT, MRO |
| Sourcewell | CURRENT_GOV_CONTRACT_PRICE, HISTORICAL_GOV_PRICE | public_html_docs | html/pdf/xlsx | none_for_public | timeout_prone | EQUIPMENT, VEHICLE, FLEET |
| NASPO ValuePoint | CURRENT_GOV_CONTRACT_PRICE, AUTHORIZED_DISTRIBUTOR | public_html_docs | html/pdf | none_for_public | variable | IT, VEHICLE, TOOLS |
| State term contracts | CURRENT_GOV_CONTRACT_PRICE, HISTORICAL_GOV_PRICE | public_html_docs | html/pdf/xlsx/csv | none_or_login_for_some | portal_specific | ALL |
| Local bid tabs | HISTORICAL_GOV_PRICE, LIVE_OPPORTUNITY, COMPETITION | public_html | html/pdf | often_auth_wall | platform_specific | ALL |
| Government board records | HISTORICAL_GOV_PRICE, PRODUCT_IDENTITY, RECURRING_BUY | public_pdf_html | pdf/html | none | stable | VEHICLE, EQUIPMENT, IT |
| Open-data portals | HISTORICAL_GOV_PRICE, RECURRING_BUY | public_api | json/csv | none | stable | ALL |
| OEM | CURRENT_COMMERCIAL_PRICE, CURRENT_ACQUISITION_PRICE | public_html | html | none | frequently_bot_blocked | IT, EQUIPMENT, VEHICLE, TOOLS |
| Dealers | CURRENT_ACQUISITION_PRICE, AUTHORIZED_DISTRIBUTOR | public_html | html/pdf | none | variable | EQUIPMENT, VEHICLE |
| Distributors | CURRENT_ACQUISITION_PRICE | public_html | html | none | frequently_bot_blocked | MRO, IT, LAB, ELECTRONICS |
| Static PDFs | CURRENT_ACQUISITION_PRICE, CURRENT_GOV_CONTRACT_PRICE, HISTORICAL_GOV_PRICE, PRICE_LEAD_ONLY | public_http | pdf | none | usually_ok | ALL |
| XLSX/CSV | CURRENT_ACQUISITION_PRICE, CURRENT_GOV_CONTRACT_PRICE, HISTORICAL_GOV_PRICE | public_http | xlsx/csv | none | usually_ok | ALL |
| Secondary award aggregators | HISTORICAL_AWARD, PRICE_LEAD_ONLY | public_html | html | often_paywall | variable | ALL |

**Note:** GSA/SEWP/gov-channel prices are intelligence (`GOVERNMENT_CHANNEL_PRICE`), not automatic acquisition cost.

Runtime health: `data/phase_l29_source_health.json` + `source_inventory.health_dashboard()`.
