# BUILD 002P — Complete Procurement Backlog

**Status:** AWAITING OWNER ACCEPTANCE (before Railway deploy)  
**Build:** 002P  
**Started:** 2026-11-22T05:34:46.451188+00:00  
**Finished:** 2026-11-22T05:54:59.587875+00:00  
**Elapsed sec:** 1213.2  
**Metered NextBid:** 0 (LOCKED)  
**Web search used:** False  
**Build 003:** not started  

## OpenAI one-time authorization

- Max additional authorized: **$1.00**
- Actual additional spent: **$0.0669**
- Successful requests: `125`
- Cache hits: `17`
- Batched records: `466`
- Applied firm / estimated: `57` / `6`
- Tokens in/out: `233985` / `53580`
- Budget denied stops: `0`
- Ceiling restored: `{"restored_at": "2026-11-22T05:55:00.236183+00:00", "daily_budget_usd": 1.0, "per_job_budget_usd": 0.5, "per_user_daily_budget_usd": 1.0}`

## Results

| Metric | Count |
|--------|------:|
| Deferred investigated | 717 |
| OpenAI-eligible targeted | 559 |
| Quantities recovered | 63 |
| Newly verified quantities | 63 |
| New Stage C fits | 56 |
| Unique verified Stage C | 73 |
| Remaining unresolved | 661 |
| Complete packages | 9 |
| Partial packages | 14 |
| Restricted packages | 442 |

## Queues (`data/procurement_queues/`)

| Queue | Count |
|-------|------:|
| Verified fits | 73 |
| Promising awaiting verification | 200 |
| Missing document | 50 |
| Restricted document | 405 |
| Estimated quantity | 6 |
| Confirmed DQ | 0 |
| Indefinite | 0 |
| Archived/closed | 0 |
| Budget denied unresolved | 0 |

Unresolved opportunities are **not** listed as verified fits.

## Document accessibility

- Packages: 6780
- Documents: 4350
- With extracted excerpt: 1632

## UI/API verification

```
{
  "queue_index_exists": true,
  "app_import_ok": true,
  "routes_sample": [
    "/healthz",
    "/api/m3/procurement/queues",
    "/opportunities",
    "/opportunities/find",
    "/opportunities/save",
    "/opportunities/searches",
    "/opportunities/discover/{run_id}/{external_key}",
    "/opportunities/discover/{run_id}/{external_key}/research",
    "/opportunities/{opportunity_id}",
    "/opportunities/{opportunity_id}/research-awards",
    "/opportunities/{opportunity_id}/package/retry",
    "/opportunities/{opportunity_id}/package/refresh",
    "/opportunities/{opportunity_id}/package/acquire",
    "/opportunities/{opportunity_id}/package/upload",
    "/opportunities/discover/{run_id}/{external_key}/package/retry",
    "/opportunities/discover/{run_id}/{external_key}/package/refresh",
    "/opportunities/discover/{run_id}/{external_key}/package/acquire",
    "/opportunities/discover/{run_id}/{external_key}/package/upload",
    "/api/m3/nextbid/status",
    "/api/m3/nextbid/run"
  ]
}
```

## New Stage C (this build)

- `NB00350443` qty=2 sem=ORDERED_QUANTITY — NOTICE OF INTENT TO SINGLE SOURCE IXOM WATERCARE INC
- `NB00320237` qty=12 sem=ORDERED_QUANTITY — Chemical Controller Purchase
- `NB00375309` qty=5,000 sem=ORDERED_QUANTITY — Bulk Propane Fuel Supply
- `NB00321439` qty=5 sem=ORDERED_QUANTITY — Oliver Hill New Courtroom Furniture
- `NB00370639` qty=1 sem=ORDERED_QUANTITY — PS, Fire- Training Academy Passenger Van
- `NB00345318` qty=1 sem=ORDERED_QUANTITY — RFQ 902824 Dodge Vehicle Purchase
- `NB00350283` qty=25 sem=ORDERED_QUANTITY — Bus Manufacturing Inspection
- `NB00376134` qty=1 sem=ORDERED_QUANTITY — PS, Fire Supply Delivery Van
- `NB00376135` qty=1 sem=ORDERED_QUANTITY — PS, Fire,  2026/2027 Ford F-350 Supply Pickup Truck
- `NB00331651` qty=500 sem=ORDERED_QUANTITY — C26635024 - Helicopter transport services for Panther Gap in Humboldt Redwoods State Park
- `NB00263282` qty=1.0 sem=ORDERED_QUANTITY — T1954 - Professional Auctioneer Services for Vehicles and Heavy Equipment - DSS
- `NB00281812` qty=1.0 sem=ORDERED_QUANTITY — RFP:105F-26 Shuttle Bus Services
- `NB00269089` qty=3 sem=ORDERED_QUANTITY — 12-IND-7515-Flag Material
- `NB00279554` qty=1.0 sem=ORDERED_QUANTITY — Various sale of Property at online auction 2026 and 2027
- `NB00260636` qty=1.0 sem=ORDERED_QUANTITY — RFR 251633 Metabolic Food and Formulas
- `NB00260545` qty=1.0 sem=ORDERED_QUANTITY — Purchase of Publicly-Assisted Affordable Housing
- `NB00372651` qty=2 sem=ORDERED_QUANTITY — New AC Units for DAFT Building and Secondary Building at WRF
- `NB00326568` qty=12 sem=ORDERED_QUANTITY — Inmate Communications
- `NB00339509` qty=4 sem=ORDERED_QUANTITY — IFB 133896 Fire Alarm System Replacement
- `NB00348866` qty=3 sem=ORDERED_QUANTITY — IFB Monthly and On-call Pest Control Services, San Bernardino County
- `NB00263724` qty=1.0 sem=ORDERED_QUANTITY — SP22-TRAILER-Q66 Trailers, trailer parts and services
- `NB00326707` qty=1 sem=ORDERED_QUANTITY — One (1) Compact Tracked Articulating Boom Lift with Transport Trailer for Saratoga Region
- `NB00367536` qty=5 sem=ORDERED_QUANTITY — Phase 2 - Transit Electrification
- `NB00350445` qty=6 sem=ORDERED_QUANTITY — Consolidated Food Service Exhaust Fan Replacements
- `NB00376271` qty=71,000 sem=ORDERED_QUANTITY — IFB 32A0743 Painting Services in Yuba County
- `NB00362628` qty=2 sem=ORDERED_QUANTITY — 1PA5A008833.02 HUU Diesel Generator Set
- `NB00375344` qty=7 sem=ORDERED_QUANTITY — RFQ for Generator PM & On Call Services for COTA Facilities
- `NB00320183` qty=4 sem=ORDERED_QUANTITY — London Foggers Truck-Mounted Generators
- `NB00375282` qty=3 sem=ORDERED_QUANTITY — IFPQ Replace Chiller Coils - San Joaquin County
- `NB00362840` qty=2,000 sem=ORDERED_QUANTITY — 20017 - Stormwater Pump Station #6
- `NB00350374` qty=10 sem=ORDERED_QUANTITY — Robert A. Skinner Water Treatment Plant Fire Alarm Control Panels Replacement 2129A
- `NB00367851` qty=75 sem=ORDERED_QUANTITY — 2026 Cafeteria Tables
- `NB00361136` qty=1 sem=ORDERED_QUANTITY — Event Planning and Production Services for Hayward Amplified 2.0 Program
- `NB00370618` qty=5 sem=ORDERED_QUANTITY — Lester R. Dingle Filter Media Replacement
- `NB00352766` qty=2 sem=ORDERED_QUANTITY — Performing Arts Community Center (PACC) Industrial Roll Up Door Replacement PW 27-37
- `NB00320329` qty=2, 1, 1, 4 sem=ORDERED_QUANTITY — IFB #R006316 Narrow-Aisle Reach Truck, Sit-Down Rider Truck, and Rider Pallet Truck Forkli
- `NB00221043` qty=10 sem=ORDERED_QUANTITY — Zero-Emission Truck Purchasing Incentive Project
- `NB00361043` qty=8 sem=ORDERED_QUANTITY — SMALL BUSINESS OPTION - SA000006-26 - Relocation of Hewlett Packard Enterprise Company Equ
- `NB00259947` qty=1 sem=ORDERED_QUANTITY — EXT AIR | TERMINAL 3 EAST VIDEO GAME LOUNGE LEASE
- `NB00367495` qty=1250 sem=ORDERED_QUANTITY — Teams Phones
- `NB00331581` qty=1 sem=ORDERED_QUANTITY — ARMORED RESCUE VEHICLE PD-102326
- `NB00286264` qty=2 sem=ORDERED_QUANTITY — Modular Floating Platforms
- `NB00275531` qty=1.0 sem=ORDERED_QUANTITY — Textile Machinery, parts, Supplies and Service
- `NB00280391` qty=1.0 sem=ORDERED_QUANTITY — DCR 004 Portable Toilet Services
- `NB00280426` qty=1.0 sem=ORDERED_QUANTITY — Tipping Machinery Equipment and Service
- `NB00282078` qty=2 sem=ORDERED_QUANTITY — Hudson Fire Station 1 - GARAGE DOOR REPLACEMENT
- `NB00286337` qty=10 sem=ORDERED_QUANTITY — Port of Los Angeles Clean Ports Program – Zero-Emission Truck Purchase Incentive Project
- `NB00279235` qty=10 sem=ORDERED_QUANTITY — Zero-Emission Truck Purchasing Incentive Project
- `NB00315688` qty=1 sem=ORDERED_QUANTITY — ITB_EQ-2039A - BLOUNT ISLAND MARINE TERMINAL HANJUNG CRANE #8841 REFURBISHMENT
- `NB00328110` qty=1 sem=LINE_ITEM_QUANTITY — AMR, N Liberty S, Multi Cnty
- `NB00328155` qty=25 sem=ORDERED_QUANTITY — Cleaning Out and Plugging Twenty-Five (25) Orphan Gas Wells Wetmore Township, McKean Count
- `NB00380092` qty=1 sem=ORDERED_QUANTITY — Weg motor purchase
- `NB00376250` qty=70 sem=ORDERED_QUANTITY — P2699011 - WINDOW REPLACEMENT
- `NB00294331` qty=2 sem=ORDERED_QUANTITY — Gorge Powerhouse Crane Replacement
- `NB00302278` qty=1.0 sem=ORDERED_QUANTITY — 27-IND-7000-Wood
- `NB00350199` qty=19 sem=ORDERED_QUANTITY — Traffic Signal Preemption Equipment (ADOT/federal-funded)

## Unique Stage C total: 73

Deploy ready marker: `data/acceptance/build002p_deploy_ready.json` (deployed=false).  
Await owner acceptance before Railway deployment. NextBid remains locked. Normal $1 daily ceiling restored.
