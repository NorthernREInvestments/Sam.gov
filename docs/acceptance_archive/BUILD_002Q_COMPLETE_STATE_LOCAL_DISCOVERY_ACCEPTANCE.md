# BUILD 002Q — Complete State/Local Discovery

Generated: `2026-11-23T04:11:07.701440+00:00`
Runtime: **102.0s**
Mode: **offline_only**
Metered delta: **0**
Acquisition locked: `True`

## Allowance gate

- Local date: `2026-11-22` (day_used=99/100)
- Daily left: **1** · Job limit: 50
- Max callable now: **1** · Authorized listing cap: 123
- Insufficient daily allowance: `True`
- Resume: After local calendar day rolls past 2026-11-22 (Central), day_used resets to 0 and up to 100 listing calls/day can resume from controlled coverage checkpoints. Do not auto-schedule.

## Coverage

- Provider active state/local: **None**
- M3 active state/local (Category A): **3608**
- Coverage: **None%**
- Remaining unacquired: **None**
- By source: `null`

## Listing acquisition

- Metered search calls: **0**
- Unique new: 0
- Cache hits: 0
- Stopped: offline_only_insufficient_daily_allowance

## Resume position (preserved)

```json
{
  "coverage_file": "C:\\Users\\M_gra\\OneDrive\\Desktop\\Sam.Gov App\\govtracker\\data\\nextbid_checkpoints\\build002q_controlled_coverage.json",
  "pages_remaining": 123,
  "by_source": {
    "opengov": 0,
    "bonfire": 0,
    "planetbids": 0,
    "calprocure": 0,
    "publicpurchase": 0,
    "instantmarkets": 0,
    "opengov_open": 30,
    "bonfire_open": 19,
    "planetbids_open": 1,
    "calprocure_open": 0,
    "publicpurchase_open": 0,
    "instantmarkets_open": 73,
    "priority_commodities_combined": 0
  },
  "searches": {
    "opengov_open|tradelines=None|open=true": {
      "source": "opengov_open",
      "last_successful_page": 30,
      "provider_total_pages": 60,
      "completion_status": "partial",
      "unique_records": 279
    },
    "bonfire_open|tradelines=None|open=true": {
      "source": "bonfire_open",
      "last_successful_page": 30,
      "provider_total_pages": 49,
      "completion_status": "partial",
      "unique_records": 613
    },
    "planetbids_open|tradelines=None|open=true": {
      "source": "planetbids_open",
      "last_successful_page": 30,
      "provider_total_pages": 31,
      "completion_status": "partial",
      "unique_records": 325
    },
    "instantmarkets_open|tradelines=None|open=true": {
      "source": "instantmarkets_open",
      "last_successful_page": 28,
      "provider_total_pages": 101,
      "completion_status": "partial",
      "unique_records": 286
    }
  },
  "next_authorized_listing_budget": 123,
  "do_not_auto_schedule": true
}
```

## Qualification (active state/local)

| Metric | Count |
|---|---:|
| Active SL reprocessed | 3608 |
| Confirmed product | 923 |
| Potential product | 1925 |
| Mixed product/service | 99 |
| Incompatible | 760 |
| Verified quantities | 87 |
| Procurement-eligible | 3 |
| Ready to submit (B) | 3 |
| Registration (C) | 0 |
| Supplier pricing (A) | 2845 |
| Information recovery (D) | 2845 |

## Actionable contracts

### B — Ready to submit

| nb_id | sol# | qty | deadline | agency | title | URL |
|---|---|---|---|---|---|---|
| NB00320329 | 0000040733 | None | 2026-11-23T23:00:00.000Z | CA State Lottery Commission | IFB #R006316 Narrow-Aisle Reach Truck, Sit-Down Rider Truck, | https://caleprocure.ca.gov/event/0850/0000040733 |
| NB00362710 | 0000040903 | None | 2026-12-09T22:00:00.000Z | California Military Department | IFB No. 132845 Fire Suppression Systems Maintenance at Multi | https://caleprocure.ca.gov/event/8940/0000040903 |
| NB00375282 | 10A2969 | None | 2026-12-08T01:00:00.000Z | Department of Transportation | IFPQ Replace Chiller Coils - San Joaquin County | https://caleprocure.ca.gov/event/2660/10A2969 |

### C — Registration required

_None._

### A — Supplier-pricing candidates (sample)

- NB00040055 qty=None elig=False — FARRAGUT ELEMENTARY SCHOOL ELECTRICAL REPAIR PROJECT CITY PROJECT NO. MY2303
- NB00051314 qty=None elig=False — Food Service Providers RFSQ
- NB00063402 qty=None elig=False — Heavy Duty Tow Rotation Program Q24211313004
- NB00076766 qty=None elig=False — Janitorial and Cleaning Supplies
- NB00080682 qty=None elig=False — Qualified Vendor List (QVL) for the Purchase of New Vehicles
- NB00080760 qty=None elig=False — Early Intervention Service Provider Enrollment
- NB00080844 qty=None elig=False — Plumbing Supplies and Fixtures
- NB00080845 qty=None elig=False — On-Call Electrical Services  Fixed Price Bid
- NB00086116 qty=None elig=False — HSR26-20 Cal CLEAN Partnership Agreement
- NB00091657 qty=None elig=False — Financing District Advisory Services — Enhanced Infrastructure Financing District (EIFD) F
- NB00092427 qty=None elig=False — Psychiatry Services at Sacramento County's Youth Detention Facility Open Enrollment
- NB00100619 qty=None elig=False — CDPH Centralized Application Branch (CAB) Online - RFP 26-10053
- NB00100623 qty=None elig=False — CDPH SaPHIRE M&O - IT Services
- NB00213476 qty=None elig=False — EMERGENCY MEDICAL SUPPLIES FOR FIRE RESCUE (ANNUAL CONTRACT WITH 4 OPTIONS TO RENEW)
- NB00214106 qty=None elig=False — Recovered Materials Processing Services
- NB00214380 qty=None elig=False — Silverbell Road Roadway Improvements - El Camino del Cerro to Goret Road
- NB00215298 qty=None elig=False — CONTRACT NUMBER: 26-330888; CHPERS I: REPLACEMENT OF TOWERS AND VAULTS ¿ LEVIATHAN PEAK CA
- NB00216756 qty=None elig=False — Corrections Booking and Release Center, Building Automation System, Smoke and Fire Alarm A
- NB00217355 qty=None elig=False — IFB - Hazardous Waste Disposal Services in San Diego and Imperial Counties.
- NB00217896 qty=None elig=False — 2026 Annual Invitation for CUPCCAA Informal Construction Bid List 2026CUPCCAA
- NB00217984 qty=None elig=False — ("Continuous") Establish a Qualified Pool of Bank Credit and Loan Facility Services 100897
- NB00218086 qty=None elig=False — Inland Feeder/San Bernardino Valley Municipal Water District (SBVMWD) Foothill Pump Statio
- NB00218094 qty=None elig=False — NOTICE ONLY: Fiscal Year 2027 Project Opportunities with the Department of Public Utilitie
- NB00218498 qty=None elig=False — Wood Pole Inspection, Treatment, Repair, and UAV Inspection Services
- NB00218576 qty=None elig=False — Emergency Sheltering Voucher (ESV) Program for Families P27231031002
- NB00218613 qty=None elig=False — ("Continuous") Establish a Qualified Pool of Municipal Advisors 10089754-21-W
- NB00218980 qty=None elig=False — ("Continuous") Establish a Qualified Pool of Underwriters and Commercial Paper Dealers 100
- NB00219050 qty=None elig=False — Mobile Substation Project
- NB00219394 qty=None elig=False — CONTRACT : 26-333577; PHASE V COMMUNICATION FACILITIES REPLACEMENT: GROUP A ;CALIFORNIA DE
- NB00219946 qty=None elig=False — Climate Controlled Booth Installation - IFB 26C626001

### D — Information recovery (sample)

- NB00040055 missing=['requirement_class_unknown', 'potential_product_insufficient_classification', 'quantity_unknown'] — FARRAGUT ELEMENTARY SCHOOL ELECTRICAL REPAIR PROJECT CITY PROJECT NO. MY2303
- NB00051314 missing=['quantity_unknown'] — Food Service Providers RFSQ
- NB00063402 missing=['requirement_class_unknown', 'potential_product_insufficient_classification', 'quantity_unknown'] — Heavy Duty Tow Rotation Program Q24211313004
- NB00076766 missing=['quantity_unknown'] — Janitorial and Cleaning Supplies
- NB00080682 missing=['quantity_unknown'] — Qualified Vendor List (QVL) for the Purchase of New Vehicles
- NB00080760 missing=['requirement_class_unknown', 'potential_product_insufficient_classification', 'quantity_unknown'] — Early Intervention Service Provider Enrollment
- NB00080844 missing=['quantity_unknown'] — Plumbing Supplies and Fixtures
- NB00080845 missing=['quantity_unknown'] — On-Call Electrical Services  Fixed Price Bid
- NB00086116 missing=['requirement_class_unknown', 'potential_product_insufficient_classification', 'quantity_unknown'] — HSR26-20 Cal CLEAN Partnership Agreement
- NB00091657 missing=['requirement_class_unknown', 'potential_product_insufficient_classification', 'quantity_unknown'] — Financing District Advisory Services — Enhanced Infrastructure Financing Distric
- NB00092427 missing=['requirement_class_unknown', 'potential_product_insufficient_classification', 'quantity_unknown'] — Psychiatry Services at Sacramento County's Youth Detention Facility Open Enrollm
- NB00100619 missing=['requirement_class_unknown', 'potential_product_insufficient_classification', 'quantity_unknown'] — CDPH Centralized Application Branch (CAB) Online - RFP 26-10053
- NB00100623 missing=['requirement_class_unknown', 'potential_product_insufficient_classification', 'quantity_unknown'] — CDPH SaPHIRE M&O - IT Services
- NB00213476 missing=['quantity_unknown'] — EMERGENCY MEDICAL SUPPLIES FOR FIRE RESCUE (ANNUAL CONTRACT WITH 4 OPTIONS TO RE
- NB00214106 missing=['quantity_unknown'] — Recovered Materials Processing Services
- NB00214380 missing=['quantity_unknown'] — Silverbell Road Roadway Improvements - El Camino del Cerro to Goret Road
- NB00215298 missing=['requirement_class_unknown', 'potential_product_insufficient_classification', 'quantity_unknown'] — CONTRACT NUMBER: 26-330888; CHPERS I: REPLACEMENT OF TOWERS AND VAULTS ¿ LEVIATH
- NB00216756 missing=['requirement_class_unknown', 'potential_product_insufficient_classification', 'quantity_unknown'] — Corrections Booking and Release Center, Building Automation System, Smoke and Fi
- NB00217355 missing=['requirement_class_unknown', 'potential_product_insufficient_classification', 'quantity_unknown'] — IFB - Hazardous Waste Disposal Services in San Diego and Imperial Counties.
- NB00217896 missing=['requirement_class_unknown', 'potential_product_insufficient_classification', 'quantity_unknown'] — 2026 Annual Invitation for CUPCCAA Informal Construction Bid List 2026CUPCCAA
- NB00217984 missing=['requirement_class_unknown', 'potential_product_insufficient_classification'] — ("Continuous") Establish a Qualified Pool of Bank Credit and Loan Facility Servi
- NB00218086 missing=['quantity_unknown'] — Inland Feeder/San Bernardino Valley Municipal Water District (SBVMWD) Foothill P
- NB00218094 missing=['requirement_class_unknown', 'potential_product_insufficient_classification', 'quantity_unknown'] — NOTICE ONLY: Fiscal Year 2027 Project Opportunities with the Department of Publi
- NB00218498 missing=['requirement_class_unknown', 'potential_product_insufficient_classification', 'quantity_unknown'] — Wood Pole Inspection, Treatment, Repair, and UAV Inspection Services
- NB00218576 missing=['requirement_class_unknown', 'potential_product_insufficient_classification', 'quantity_unknown'] — Emergency Sheltering Voucher (ESV) Program for Families P27231031002
- NB00218613 missing=['requirement_class_unknown', 'potential_product_insufficient_classification', 'quantity_unknown'] — ("Continuous") Establish a Qualified Pool of Municipal Advisors 10089754-21-W
- NB00218980 missing=None — ("Continuous") Establish a Qualified Pool of Underwriters and Commercial Paper D
- NB00219050 missing=['requirement_class_unknown', 'potential_product_insufficient_classification', 'quantity_unknown'] — Mobile Substation Project
- NB00219394 missing=['requirement_class_unknown', 'potential_product_insufficient_classification', 'quantity_unknown'] — CONTRACT : 26-333577; PHASE V COMMUNICATION FACILITIES REPLACEMENT: GROUP A ;CAL
- NB00219946 missing=['quantity_unknown'] — Climate Controlled Booth Installation - IFB 26C626001

## Prioritized paid detail/document recovery (NOT executed)

- Candidates: 2848
- Estimated minimum credits (top 50): **99**
- Plan only — this job spent ZERO detail/document credits.

- NB00348901 calls=2 need=['submission_instructions', 'specification_or_document_text'] — C26RI103317777R - REBID - Invitation For Bids (Non- Negotiable) - Furn
- NB00321554 calls=2 need=['submission_instructions', 'specification_or_document_text'] — PURCHASE OF ONE (1) 6” HIGH PRESSURE DIESEL PUMP  WITH A TRAILER
- NB00345848 calls=2 need=['submission_instructions', 'specification_or_document_text'] — Two (2) Animal Transport Vehicles
- NB00347377 calls=2 need=['submission_instructions', 'specification_or_document_text'] — Mauldin Road WRRF Wet Weather Pump Station VFD Replacement Project RFB
- NB00362602 calls=2 need=['submission_instructions', 'specification_or_document_text'] — F125 Wheel Sets & BTR Wheel Assemblies IFB No. PO1071-27
- NB00380020 calls=2 need=['submission_instructions', 'specification_or_document_text'] — 26-0106 Frame Extension of Five (5) 2025 Freightliner 114SD Trucks
- NB00261760 calls=2 need=['submission_instructions', 'specification_or_document_text'] — BL081-24 Purchase of a Swift Water Rescue Boat
- NB00301738 calls=2 need=['verified_quantity', 'submission_instructions', 'specification_or_document_text'] — FORD AUTOMOTIVE AND TRUCK OEM PARTS, PURCHASE AND DELIVERY,
- NB00375272 calls=2 need=['verified_quantity', 'submission_instructions', 'specification_or_document_text'] — Traffic Engineering Division FY27 Uniforms - Field Workers & Managemen
- NB00317411 calls=2 need=['verified_quantity', 'submission_instructions', 'specification_or_document_text'] — Tool Storage Lockers for Rail
- NB00362564 calls=2 need=['verified_quantity', 'submission_instructions', 'specification_or_document_text'] — Automotive Oils, Lubricants, Greases, and Other Related Items
- NB00372835 calls=2 need=['verified_quantity', 'submission_instructions', 'specification_or_document_text'] — *222816 Purchase of - Police Equipment and Supplies
- NB00361066 calls=2 need=['verified_quantity', 'submission_instructions', 'specification_or_document_text'] — Badging Security Supplies for Milwaukee County General Mitchell Intern
- NB00345273 calls=2 need=['verified_quantity', 'submission_instructions', 'specification_or_document_text'] — ITB - Paint and Related Items for Harris County and the Harris County 
- NB00359027 calls=2 need=['verified_quantity', 'submission_instructions', 'specification_or_document_text'] — ITB - Irrigation Parts and Related Items for Harris County
- NB00359028 calls=2 need=['verified_quantity', 'submission_instructions', 'specification_or_document_text'] — ITB - Fertilizer, Grass Seed, Insecticide, Herbicide and Related Items
- NB00361152 calls=2 need=['verified_quantity', 'submission_instructions', 'specification_or_document_text'] — SLCo FAC135471 Salt Palace Upholstery - RFB
- NB00370592 calls=2 need=['verified_quantity', 'submission_instructions', 'specification_or_document_text'] — Radio Maintenance and Equipment
- NB00370760 calls=2 need=['verified_quantity', 'submission_instructions', 'specification_or_document_text'] — GSD Technology - Servers
- NB00361157 calls=2 need=['verified_quantity', 'submission_instructions', 'specification_or_document_text'] — EV4879 Water/Wastewater Treatment Chemicals

## Free document recovery

`{"recovered_count": 0, "restricted_count": 0, "stats": {"recovered": 0, "restricted": 0, "failed": 51, "skipped_checkpoint": 49}, "metered_nextbid": 0, "elapsed_sec": 17.72}`

## Provider usage

- Before: `{"ok": true, "remaining": 2547, "used": 3453, "allowance": 6000, "raw_ok": true}`
- After: `{"ok": false, "error": "NextBidControllerError", "used": 3453, "remaining": 2547, "allowance": 6000, "from_budget_state": true}`
- Remaining balance: **2547**

## STOP

Awaiting owner review. No Railway. No Build 003. No auto-scheduled acquisition.
