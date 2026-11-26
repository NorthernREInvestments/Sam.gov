# BUILD 002Q — Final Pipeline Defect Audit & Opportunity Recovery

**Generated:** 2026-11-23T01:01:48.185991+00:00
**Runtime:** 65.51s

## Status

- Acquisition locked: **YES**
- Metered NextBid delta: **0**
- OpenAI spend: **$0**
- Free official docs recovered: **0**
- Source limitation note: Free official URL recovery: PlanetBids/OpenGov often HTTP 403; Cal eProcure is JS SPA (thin HTML ~30 chars). Restricted portals recorded as restricted — not automatic DQ. Of 1912 full-text files, ~1718 are listing summaries; only ~66 genuine package/spec bodies.

## Confirmed software defects corrected

- **D1** `classify._infer_status / collect_population normalize / commercial_criteria status map` — lifecycle_status=open never became solicitation_status for bid gates → Map lifecycle_status→solicitation_status; re-infer when status unknown
- **D2** `commercial_criteria.evaluate_bid_eligibility sufficient_documents` — document_set_status=COMPLETE ignored; 500-char floor blocked Stage C text → Honor document_set_status; Stage C+qty passes at doc_len≥200
- **D3** `commercial_criteria._SUBMISSION_RE` — IFB/RFB/RFP instrument language did not satisfy submission gate → Accept invitation-for-bids / IFB/RFB/RFP/RFQ as submission evidence
- **D4** `commercial_criteria quantity_defined + extract_fields ESTIMATED markers` — Distant 'up to' boilerplate demoted firm ORDERED quantities → Narrow estimated markers; firm ORDERED/LINE_ITEM not demoted by qty_kind=estimated
- **D5** `extract_fields WORD_PAREN / BARE_EA / N_EACH_OF` — Only after-context checked; BARE_EA/N_EACH_OF dead code → Before+after product context; wire bare EA / N each of extractors
- **D6** `pipeline_repair full_text attach + document_quality.title_body_overlap` — Wrong portal pages preferred as full text over matching listings → Reject low title/body overlap; never overwrite thicker text with thinner
- **D7** `nextbid_free_doc_recovery (official public URLs)` — Existing records lacked free official document recovery path → Bounded public URL recovery for product/ambiguous open records

## Population reconciliation (all unique records)

| Metric | Count |
|---|---|
| Unique population | 6585 |
| Open | 1517 |
| Amended | 0 |
| Closed/expired/cancelled | 53 |
| Unknown status | 5015 |
| Confirmed tangible product | 2044 |
| Mixed/install | 22 |
| Potential product | 4089 |
| Confirmed non-product | 430 |
| Duplicate sol# pairs | 94 |
| Verified quantities | 69 |
| Ready to submit (B) | 2 |
| Ordinary registration required (C) | 22 |
| Ready for supplier pricing (A; qty+product, not bid-ready) | 45 |
| Info recovery (D) | 6071 |

### Primary dispositions

`{"confirmed_disqualification": 489, "verified_product_information_needed": 2003, "potential_product_classification_needed": 4068, "easy_vendor_registration": 22, "closed_expired_non_solicitation": 1, "verified_bid_opportunities": 2}`

## Full-text quality validation

- Files: 1912
- Usable non-junk (approx): 1852
- Genuine solicitation docs (C/D approx): 66
- Listing summaries only (A): 1718
- Buckets: `{"A_short_listing_summaries": 1718, "E_amendments": 29, "F_junk_html_login_portal": 51, "B_detailed_descriptions": 81, "D_complete_packages": 39, "C_specs_and_bid_schedules": 14, "F_title_body_mismatch": 9}`

## Free official document recovery

`{"recovered": 0, "restricted": 88, "failed": 92}`

## ACTUAL ready-to-submit opportunities (B)

| nb_id | qty | deadline | agency | title | URL |
|---|---|---|---|---|---|
| NB00320329 | 2 | 2026-11-23T23:00:00.000Z | CA State Lottery Commission | IFB #R006316 Narrow-Aisle Reach Truck, Sit-Down Rider Truck, and Rider | https://caleprocure.ca.gov/event/0850/0000040733 |
| NB00362607 | 2 | 2026-12-22T22:00:00.000Z | Dept of Food & Agriculture | IFB 26-0094 - Smith River BPS HVAC Split System (Del Norte County) - P | https://caleprocure.ca.gov/event/8570/0000040888 |

## Ordinary registration required (C)

| nb_id | qty | agency | title | URL |
|---|---|---|---|---|
| NB00338603 | 160 | The Metropolitan Water District of Southern California | Furnish one hundred sixty (160) Chemical Flow Meters for the Metropoli | https://vendors.planetbids.com/portal/16151/bo/bo-detail/147393 |
| NB00375434 | 7 | The Metropolitan Water District of Southern California | East Penn Manufacturing Company Station Batteries (7 units) RFB-LB-481 | https://vendors.planetbids.com/portal/16151/bo/bo-detail/148410 |
| NB00362874 | 3 | DEPT OF DEFENSE.DEFENSE LOGISTICS AGENCY.TROOP SUPPORT CONSTRUCTION & EQUIPMENT.DLA TROOP SUPPORT | 44--DEHYDRATOR UNIT,NONREA | https://sam.gov/workspace/contract/opp/b2695940babd4dd28227cc1fe6f7a47a/view |
| NB00362875 | 11 | DEPT OF DEFENSE.DEFENSE LOGISTICS AGENCY.DLA LAND.DLA LAND COLUMBUS.DLA WEAPONS SUPPORT | 29--THERMOSTAT ASSEMBLY | https://sam.gov/workspace/contract/opp/8266aad8bfc04f1eadc7de0eccdb4ad0/view |
| NB00362876 | 4 | DEPT OF DEFENSE.DEFENSE LOGISTICS AGENCY.TROOP SUPPORT CONSTRUCTION & EQUIPMENT.DLA TROOP SUPPORT | 41--COMPRESSOR UNIT,REFRIG | https://sam.gov/workspace/contract/opp/73e6f7f95bfa4d858a3e9e555e5d336d/view |
| NB00362877 | 20 | DEPT OF DEFENSE.DEFENSE LOGISTICS AGENCY.TROOP SUPPORT CONSTRUCTION & EQUIPMENT.DLA TROOP SUPPORT | 41--COOLING COIL,AIR,DUCT | https://sam.gov/workspace/contract/opp/5ff2d8e86ae04a4a8c58e2002136aeef/view |
| NB00362878 | 1 | DEPT OF DEFENSE.DEFENSE LOGISTICS AGENCY.TROOP SUPPORT CONSTRUCTION & EQUIPMENT.DLA TROOP SUPPORT | 44--DEHYDRATOR UNIT,NONREA | https://sam.gov/workspace/contract/opp/4c8f6060ff3c43c8b22964427e46cd9e/view |
| NB00362879 | 2 | DEPT OF DEFENSE.DEFENSE LOGISTICS AGENCY.TROOP SUPPORT CONSTRUCTION & EQUIPMENT.DLA TROOP SUPPORT | 41--CONTROLLER,PNEUMATIC | https://sam.gov/workspace/contract/opp/3ea92ccf22d54f6e8da5bb25788fffcc/view |
| NB00362881 | 3 | DEPT OF DEFENSE.DEFENSE LOGISTICS AGENCY.TROOP SUPPORT CONSTRUCTION & EQUIPMENT.DLA TROOP SUPPORT | 44--CONDENSATE DISCHARG | https://sam.gov/workspace/contract/opp/154da7b875fb467dbddc4ffbd06923ab/view |
| NB00362882 | 18 | DEPT OF DEFENSE.DEFENSE LOGISTICS AGENCY.TROOP SUPPORT CONSTRUCTION & EQUIPMENT.DLA TROOP SUPPORT | 44--CONDENSATE DISCHARG | https://sam.gov/workspace/contract/opp/14da2afb7ed04da3b8573a5500e5245f/view |
| NB00362883 | 1 | DEPT OF DEFENSE.DEFENSE LOGISTICS AGENCY.TROOP SUPPORT CONSTRUCTION & EQUIPMENT.DLA TROOP SUPPORT | 44--DEHYDRATOR UNIT,NONREA | https://sam.gov/workspace/contract/opp/0dc19cfd8cfd4f8ebe58d5e5ef6e03dd/view |
| NB00362884 | 4 | DEPT OF DEFENSE.DEFENSE LOGISTICS AGENCY.TROOP SUPPORT CONSTRUCTION & EQUIPMENT.DLA TROOP SUPPORT | 44--DEHYDRATOR UNIT,NONREA | https://sam.gov/workspace/contract/opp/04ec6e75a6f343ca9b390984f73382eb/view |
| NB00362929 | 5 | DEPT OF DEFENSE.DEFENSE LOGISTICS AGENCY.TROOP SUPPORT CONSTRUCTION & EQUIPMENT.DLA TROOP SUPPORT | 41--FAN,CENTRIFUGAL | https://sam.gov/workspace/contract/opp/e4873e3d10354031aae5f2e850f66dc3/view |
| NB00362930 | 2 | DEPT OF DEFENSE.DEFENSE LOGISTICS AGENCY.TROOP SUPPORT CONSTRUCTION & EQUIPMENT.DLA TROOP SUPPORT | 41--PANEL,AIR INTAKE | https://sam.gov/workspace/contract/opp/d26ca8e83c834391be688ba2525931f6/view |
| NB00362931 | 1 | DEPT OF DEFENSE.DEFENSE LOGISTICS AGENCY.TROOP SUPPORT CONSTRUCTION & EQUIPMENT.DLA TROOP SUPPORT | 41--FAN,VANEAXIAL | https://sam.gov/workspace/contract/opp/a50bfbf03b8542f58599a936d4498453/view |
| NB00362932 | 1 | DEPT OF DEFENSE.DEFENSE LOGISTICS AGENCY.TROOP SUPPORT CONSTRUCTION & EQUIPMENT.DLA TROOP SUPPORT | 41--FAN,VANEAXIAL | https://sam.gov/workspace/contract/opp/a23adfda6467461181360885d637c741/view |
| NB00362933 | 1 | DEPT OF DEFENSE.DEFENSE LOGISTICS AGENCY.TROOP SUPPORT CONSTRUCTION & EQUIPMENT.DLA TROOP SUPPORT | 41--IMPELLER,FAN,AXIAL | https://sam.gov/workspace/contract/opp/98c6769b5d944d59ae9a148cd98cf1af/view |
| NB00362934 | 1 | DEPT OF DEFENSE.DEFENSE LOGISTICS AGENCY.TROOP SUPPORT CONSTRUCTION & EQUIPMENT.DLA TROOP SUPPORT | 41--FAN,VANEAXIAL | https://sam.gov/workspace/contract/opp/844e56b2acec47aaaf3c1bc58b85ea6d/view |
| NB00362935 | 2 | DEPT OF DEFENSE.DEFENSE LOGISTICS AGENCY.TROOP SUPPORT CONSTRUCTION & EQUIPMENT.DLA TROOP SUPPORT | 41--FAN,CENTRIFUGAL | https://sam.gov/workspace/contract/opp/78efb5a825df44dab268c44c50f370e1/view |
| NB00362936 | 2 | DEPT OF DEFENSE.DEFENSE LOGISTICS AGENCY.TROOP SUPPORT CONSTRUCTION & EQUIPMENT.DLA TROOP SUPPORT | 41--FAN,VANEAXIAL | https://sam.gov/workspace/contract/opp/52d5dbfb3d8c4f5dbf50e97d9f542cf0/view |
| NB00362968 | 24 | DEPT OF DEFENSE.DEFENSE LOGISTICS AGENCY.TROOP SUPPORT CONSTRUCTION & EQUIPMENT.DLA TROOP SUPPORT | 45--HEATER,SPACE,ELECTRIC | https://sam.gov/workspace/contract/opp/bfb4014f30d24fc89bde40f7423a4177/view |
| NB00362969 | 131 | DEPT OF DEFENSE.DEFENSE LOGISTICS AGENCY.TROOP SUPPORT CONSTRUCTION & EQUIPMENT.DLA TROOP SUPPORT | 41--FAN,CENTRIFUGAL | https://sam.gov/workspace/contract/opp/9cd0ee438c414de6a6a80758e435b65d/view |

## Supplier-pricing candidates (A — qty verified; not yet bid-ready)

| nb_id | qty | status | disposition | title |
|---|---|---|---|---|
| NB00348901 | 12 | unknown | verified_product_information_needed | C26RI103317777R - REBID - Invitation For Bids (Non- Negotiable) - Furn |
| NB00321554 | 1 | open | verified_product_information_needed | PURCHASE OF ONE (1) 6” HIGH PRESSURE DIESEL PUMP  WITH A TRAILER |
| NB00345848 | 2 | unknown | verified_product_information_needed | Two (2) Animal Transport Vehicles |
| NB00291496 | 4 | open | verified_product_information_needed | Fabrication and Installation for Four (4) Transportation Vans |
| NB00320237 | 12 | open | verified_product_information_needed | Chemical Controller Purchase |
| NB00370595 | 1 | open | verified_product_information_needed | Purchase of one Rebuilt Caterpillar 836G landfill compactor utilizing  |
| NB00370639 | 1 | open | verified_product_information_needed | PS, Fire- Training Academy Passenger Van |
| NB00339475 | 2 | open | verified_product_information_needed | Two (2) New/Unused 2026 or Newer Hybrid Full Size Light Duty four (4)  |
| NB00376134 | 1 | open | verified_product_information_needed | PS, Fire Supply Delivery Van |
| NB00376135 | 1 | open | verified_product_information_needed | PS, Fire,  2026/2027 Ford F-350 Supply Pickup Truck |
| NB00365477 | 2 | open | verified_product_information_needed | Police Department Off Highway Vehicles (OHV) |
| NB00260203 | 1 | unknown | verified_product_information_needed | Single Purchase of an Agilent 8890 GC System Custom with dual ECD dete |
| NB00260314 | 1 | unknown | verified_product_information_needed | Towable Air Start Unit |
| NB00261760 | 1 | unknown | verified_product_information_needed | BL081-24 Purchase of a Swift Water Rescue Boat |
| NB00350487 | 6 | open | verified_product_information_needed | Gate Operator Replacement Services in Shasta County |
| NB00326707 | 1 | unknown | verified_product_information_needed | One (1) Compact Tracked Articulating Boom Lift with Transport Trailer  |
| NB00320183 | 4 | open | verified_product_information_needed | London Foggers Truck-Mounted Generators |
| NB00372789 | 1 | open | verified_product_information_needed | Purchase and Delivery of Intelligent Auto-Flusher |
| NB00347377 | 4 | open | verified_product_information_needed | Mauldin Road WRRF Wet Weather Pump Station VFD Replacement Project RFB |
| NB00221043 | 10 | open | verified_product_information_needed | Zero-Emission Truck Purchasing Incentive Project |
| NB00268518 | 12 | unknown | verified_product_information_needed | New Request for Window Mounted and Portable Air Conditioners (Target M |
| NB00361043 | 8 | open | verified_product_information_needed | SMALL BUSINESS OPTION - SA000006-26 - Relocation of Hewlett Packard En |
| NB00345249 | 91 | open | verified_product_information_needed | CDBG-DR ECAT Bus Shelter Manufacturing Project HS018 - Re-Solicit |
| NB00286337 | 10 | open | verified_product_information_needed | Port of Los Angeles Clean Ports Program – Zero-Emission Truck Purchase |
| NB00362719 | 1 | open | verified_product_information_needed | ABLE Replacement of Modular Toilet Building Bid No. 7333 |
| NB00279235 | 10 | unknown | verified_product_information_needed | Zero-Emission Truck Purchasing Incentive Project |
| NB00328110 | 1 | unknown | verified_product_information_needed | AMR, N Liberty S, Multi Cnty |
| NB00380092 | 1 | open | verified_product_information_needed | Weg motor purchase |
| NB00376174 | 1 | open | verified_product_information_needed | Trash Conveyor IFB 10205960 |
| NB00362602 | 8 | open | verified_product_information_needed | F125 Wheel Sets & BTR Wheel Assemblies IFB No. PO1071-27 |
| NB00270456 | 80 | unknown | verified_product_information_needed | Five (5) Year Lease of Eighty (80) 2020 Toyota Hybrid Camry Sedans |
| NB00332935 | 17 | unknown | verified_product_information_needed | Gear and Shaft Assembly |
| NB00333067 | 99 | unknown | verified_product_information_needed | Control, Generator |
| NB00368063 | 1 | unknown | verified_product_information_needed | 61--MOTOR,ALTERNATING CURR |
| NB00373018 | 41 | unknown | verified_product_information_needed | 58--DOCKING STATION |
| NB00380750 | 11 | unknown | verified_product_information_needed | 61--CABLE ASSEMBLY,SPECIAL |
| NB00380920 | 178 | unknown | verified_product_information_needed | 61--WEIGHT |
| NB00333303 | 8 | unknown | verified_product_information_needed | 31--SHELL, BEARING UP. |
| NB00333111 | 37 | unknown | verified_product_information_needed | VALVE ASSEMBLY |
| NB00333134 | 63 | unknown | verified_product_information_needed | VALVE, BUTTERFLY |

## Remaining blockers

`{"unknown:submission_instructions": 31, "unknown:active_solicitation": 21, "fail:tangible_products": 7, "fail:defined_quantities": 7, "unknown:sufficient_documents": 6, "unknown:reseller_eligibility": 3, "unknown:no_blocking_install_insurance_bond_license": 3, "fail:no_blocking_install_insurance_bond_license": 3}`

## Revised NextBid credit plan (NOT executed)

`{"status": "PREPARED_NOT_EXECUTED", "authorization_required": true, "ceilings": {"listing_enumeration_max": 400, "detail_and_docs_max": 1246, "monthly_reserve_min": 1000, "note": "Campaign ceilings \u2014 do not exceed daily\u2264100 or job\u226450 without owner auth"}, "strategy": ["Reuse durable paid-response store and free official URL docs first", "Enumerate remaining listing partitions only (page_size=25, non-overlapping)", "Spend detail credits only on product/ambiguous open records missing firm qty or submission", "Prefer get_opportunity then list_files; read_file only for schedule/spec attachments", "No speculative indiscriminate file retrieval"], "priority_targets": {"open_product_missing_qty_or_docs": "highest", "ambiguous_promising_with_deadline": "high", "closed_or_confirmed_non_product": "skip"}, "estimates_within_ceilings": {"listing_credits_planned": 50, "detail_credits_planned_cap": 1246, "reserve_remaining": 1000, "spendable_after_reserve_reference": 1646}, "unresolved_after_offline_approx": 6071, "do_not_execute_until": "explicit owner authorization"}`

## Provider usage

- Before: `{"ok": true, "remaining": 2646, "used": 3354, "allowance": 6000, "raw_ok": true}`
- After: `{"ok": true, "remaining": 2646, "used": 3354, "allowance": 6000, "raw_ok": true}`

## Confirmation

- All API locks remain engaged.
- No Railway deploy. No Build 003.
- **STOP — AWAITING OWNER ACCEPTANCE.**
