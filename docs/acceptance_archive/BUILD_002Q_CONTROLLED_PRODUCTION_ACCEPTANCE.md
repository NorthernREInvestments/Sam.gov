# BUILD 002Q — Controlled NextBid Production Acquisition

**Generated:** 2026-11-23T01:17:51.824089+00:00
**Runtime:** 58.36s

## Usage

- Before: `{"ok": true, "remaining": 2569, "used": 3431, "allowance": 6000, "raw_ok": true}`
- After: `{"ok": true, "remaining": 2547, "used": 3453, "allowance": 6000, "raw_ok": true}`
- Metered delta: **22**
- By endpoint: `{"search": 70, "list_documents": 11, "list_files": 7, "read_file": 10, "get_opportunity": 1}`
- Cache/durable hits: 21
- Campaign listing used: 70
- Campaign detail used: 29
- Killswitch re-engaged: **True**

## Inventory results (state/local; federal parked)

| Metric | Count |
|---|---|
| Unique inventory | 8088 |
| Open | 2098 |
| Confirmed product | 2206 |
| Potential product | 5048 |
| Verified quantities | 78 |
| Ready to submit (B) | 1 |
| Registration required (C) | 0 |
| Supplier pricing (A) | 43 |

## Actual ready-to-submit (B)

| nb_id | qty | deadline | title | URL |
|---|---|---|---|---|
| NB00320329 | 2 | 2026-11-23T23:00:00.000Z | IFB #R006316 Narrow-Aisle Reach Truck, Sit-Down Rider Truck, and Rider | https://caleprocure.ca.gov/event/0850/0000040733 |

## Registration required (C)


## Supplier-pricing candidates (A) sample

- NB00348901 qty=12 — C26RI103317777R - REBID - Invitation For Bids (Non- Negotiable) - Furnish and De
- NB00321554 qty=1 — PURCHASE OF ONE (1) 6” HIGH PRESSURE DIESEL PUMP  WITH A TRAILER
- NB00345848 qty=2 — Two (2) Animal Transport Vehicles
- NB00291496 qty=4 — Fabrication and Installation for Four (4) Transportation Vans
- NB00320237 qty=12 — Chemical Controller Purchase
- NB00370595 qty=1 — Purchase of one Rebuilt Caterpillar 836G landfill compactor utilizing a trade-in
- NB00370639 qty=1 — PS, Fire- Training Academy Passenger Van
- NB00339475 qty=2 — Two (2) New/Unused 2026 or Newer Hybrid Full Size Light Duty four (4) door pick 
- NB00376134 qty=1 — PS, Fire Supply Delivery Van
- NB00376135 qty=1 — PS, Fire,  2026/2027 Ford F-350 Supply Pickup Truck
- NB00365477 qty=2 — Police Department Off Highway Vehicles (OHV)
- NB00338603 qty=160 — Furnish one hundred sixty (160) Chemical Flow Meters for the Metropolitan Water 
- NB00260203 qty=1 — Single Purchase of an Agilent 8890 GC System Custom with dual ECD detectors
- NB00260314 qty=1 — Towable Air Start Unit
- NB00261760 qty=1 — BL081-24 Purchase of a Swift Water Rescue Boat
- NB00350487 qty=6 — Gate Operator Replacement Services in Shasta County
- NB00326707 qty=1 — One (1) Compact Tracked Articulating Boom Lift with Transport Trailer for Sarato
- NB00320183 qty=4 — London Foggers Truck-Mounted Generators
- NB00375434 qty=7 — East Penn Manufacturing Company Station Batteries (7 units) RFB-LB-481905
- NB00372789 qty=1 — Purchase and Delivery of Intelligent Auto-Flusher
- NB00347377 qty=4 — Mauldin Road WRRF Wet Weather Pump Station VFD Replacement Project RFB 610
- NB00221043 qty=10 — Zero-Emission Truck Purchasing Incentive Project
- NB00268518 qty=12 — New Request for Window Mounted and Portable Air Conditioners (Target Market)
- NB00361043 qty=8 — SMALL BUSINESS OPTION - SA000006-26 - Relocation of Hewlett Packard Enterprise C
- NB00345249 qty=91 — CDBG-DR ECAT Bus Shelter Manufacturing Project HS018 - Re-Solicit

## Discovery / detail

- Listing: `{"note": "prior_session", "search_calls_metered": 70}`
- Detail: `{"ok": true, "detail_calls_approx": 19, "cache_hits": 21, "enriched": 21, "gov_docs": 0, "nextbid_doc_calls": 18, "by_endpoint": {"read_file": 8, "list_files": 5, "get_opportunity": 1}, "stopped_reason": null, "errors": ["NB00375309:PermissionError"], "elapsed_sec": 11.03}`
- Free docs: `{"recovered": 0, "restricted": 0, "failed": 46, "skipped_checkpoint": 34}`


## Campaign cumulative (authorized window)

| Item | Value |
|---|---|
| Provider used | 3354 -> 3453 (+99) |
| Provider remaining | 2547 |
| Listing credits | 70 / 400 |
| Detail credits | 29 / 1246 |
| Daily used | 99 / 100 |
| Unique inventory | 6585 -> 8088 (+1480 new from listing) |
| Cache hits (listing) | ~69 |

Daily allowance exhausted for today. Campaign continues tomorrow within remaining listing/detail ceilings. Killswitch engaged.

## Confirmation

- No Railway deploy. No Build 003. No OpenAI/SAM/DIBBS.
- **STOP — AWAITING OWNER REVIEW.**
