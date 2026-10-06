"""Controlled benchmark corpus — >=100 exact commercially searchable identities.

Build: 20261004-m3-price-coverage-80-v1

Ground truth is live-verified against known public sellers where possible.
Only PUBLIC_NEW_PRICE_CONFIRMED counts in the 80% coverage denominator.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from application_clock import now_utc
from m3_data_root import data_path
from price_coverage_80.models import (
    AMBIGUOUS,
    BUILD,
    NO_PUBLIC_PRICE_CONFIRMED,
    PUBLIC_NEW_PRICE_CONFIRMED,
    PUBLIC_PRICE_EXISTS_BUT_ROUTE_DIFFICULT,
    QUOTE_ONLY_CONFIRMED,
)

CORPUS_FILE = "m3_price_coverage_80_benchmark_corpus.json"

# Seed Easy-25 + expanded catalog — diverse categories, known public sellers.
# Prices are approximate ranges used for live verification / accuracy windows.
_SEED: list[dict[str, Any]] = [
    # --- lighting ---
    {
        "benchmark_id": "easy-sl585101ul",
        "manufacturer": None,
        "mpn": "SL585101UL",
        "description": "LED street / area light SL585101UL",
        "category": "lighting",
        "expected_condition": "NEW",
        "expected_uom": "EA",
        "expected_pack": 1,
        "known_public_seller": "1000bulbs.com",
        "known_public_price": None,
        "known_url_hint": "https://www.1000bulbs.com/search?q=SL585101UL",
        "easy": True,
    },
    {
        "benchmark_id": "easy-philips-led",
        "manufacturer": "Philips",
        "mpn": "9290018191",
        "description": "Philips LED bulb 9290018191",
        "category": "lighting",
        "expected_condition": "NEW",
        "expected_uom": "EA",
        "expected_pack": 1,
        "known_public_seller": "1000bulbs.com",
        "known_url_hint": "https://www.1000bulbs.com/search?q=9290018191",
        "easy": True,
    },
    # --- MRO / industrial ---
    {
        "benchmark_id": "easy-3m-2097",
        "manufacturer": "3M",
        "mpn": "2097",
        "description": "3M 2097 P100 particulate filter pair",
        "category": "ppe",
        "expected_condition": "NEW",
        "expected_uom": "PR",
        "expected_pack": 1,
        "known_public_seller": "grainger.com",
        "known_url_hint": "https://www.grainger.com/search?searchQuery=3M%202097",
        "easy": True,
        "price_tolerance_pct": 0.35,
    },
    {
        "benchmark_id": "easy-3m-8210",
        "manufacturer": "3M",
        "mpn": "8210",
        "description": "3M 8210 N95 particulate respirator",
        "category": "ppe",
        "expected_condition": "NEW",
        "expected_uom": "BX",
        "expected_pack": 20,
        "known_public_seller": "grainger.com",
        "known_url_hint": "https://www.grainger.com/search?searchQuery=3M%208210",
        "easy": True,
        "price_tolerance_pct": 0.40,
    },
    {
        "benchmark_id": "easy-milwaukee-48-22-1902",
        "manufacturer": "Milwaukee",
        "mpn": "48-22-1902",
        "description": "Milwaukee Fastback folding utility knife",
        "category": "tools",
        "expected_condition": "NEW",
        "expected_uom": "EA",
        "expected_pack": 1,
        "known_public_seller": "grainger.com",
        "known_url_hint": "https://www.grainger.com/search?searchQuery=48-22-1902",
        "easy": True,
    },
    {
        "benchmark_id": "easy-dewalt-dwht11131",
        "manufacturer": "DEWALT",
        "mpn": "DWHT11131",
        "description": "DEWALT folding utility knife DWHT11131",
        "category": "tools",
        "expected_condition": "NEW",
        "expected_uom": "EA",
        "expected_pack": 1,
        "known_public_seller": "zoro.com",
        "known_url_hint": "https://www.zoro.com/search?q=DWHT11131",
        "easy": True,
    },
    {
        "benchmark_id": "easy-makita-b-45580",
        "manufacturer": "Makita",
        "mpn": "B-45580",
        "description": "Makita 5pc hex bit set B-45580",
        "category": "tools",
        "expected_condition": "NEW",
        "expected_uom": "EA",
        "expected_pack": 1,
        "known_public_seller": "grainger.com",
        "known_url_hint": "https://www.grainger.com/search?searchQuery=B-45580",
        "easy": True,
    },
    # --- plumbing ---
    {
        "benchmark_id": "easy-sharkbite-uc248lfa",
        "manufacturer": "SharkBite",
        "mpn": "UC248LFA",
        "description": "SharkBite 1/2 in push-to-connect brass ball valve",
        "category": "plumbing",
        "expected_condition": "NEW",
        "expected_uom": "EA",
        "expected_pack": 1,
        "known_public_seller": "supplyhouse.com",
        "known_url_hint": "https://www.supplyhouse.com/search?q=UC248LFA",
        "easy": True,
    },
    {
        "benchmark_id": "easy-watts-lf777m2",
        "manufacturer": "Watts",
        "mpn": "LF777M2-QT",
        "description": "Watts lead-free ball valve LF777M2-QT",
        "category": "plumbing",
        "expected_condition": "NEW",
        "expected_uom": "EA",
        "expected_pack": 1,
        "known_public_seller": "supplyhouse.com",
        "known_url_hint": "https://www.supplyhouse.com/search?q=LF777M2-QT",
        "easy": True,
    },
    # --- electrical ---
    {
        "benchmark_id": "easy-leviton-5320",
        "manufacturer": "Leviton",
        "mpn": "5320-S",
        "description": "Leviton 15A duplex receptacle 5320-S",
        "category": "electrical",
        "expected_condition": "NEW",
        "expected_uom": "EA",
        "expected_pack": 1,
        "known_public_seller": "grainger.com",
        "known_url_hint": "https://www.grainger.com/search?searchQuery=5320-S",
        "easy": True,
    },
    {
        "benchmark_id": "easy-ideal-30-076",
        "manufacturer": "Ideal",
        "mpn": "30-076",
        "description": "Ideal Wire-Nut 74B wire connector 30-076",
        "category": "electrical",
        "expected_condition": "NEW",
        "expected_uom": "BX",
        "expected_pack": 100,
        "known_public_seller": "grainger.com",
        "known_url_hint": "https://www.grainger.com/search?searchQuery=30-076",
        "easy": True,
    },
    # --- HVAC ---
    {
        "benchmark_id": "easy-honeywell-th8320u1008",
        "manufacturer": "Honeywell",
        "mpn": "TH8320U1008",
        "description": "Honeywell VisionPRO 8000 thermostat",
        "category": "hvac",
        "expected_condition": "NEW",
        "expected_uom": "EA",
        "expected_pack": 1,
        "known_public_seller": "supplyhouse.com",
        "known_url_hint": "https://www.supplyhouse.com/search?q=TH8320U1008",
        "easy": True,
    },
    {
        "benchmark_id": "easy-filtete-mpr2200",
        "manufacturer": "Filtrete",
        "mpn": "2004DC-6",
        "description": "Filtrete MPR 2200 air filter 16x25x1",
        "category": "hvac",
        "expected_condition": "NEW",
        "expected_uom": "EA",
        "expected_pack": 1,
        "known_public_seller": "grainger.com",
        "known_url_hint": "https://www.grainger.com/search?searchQuery=2004DC-6",
        "easy": True,
        "optional": True,
    },
    # --- office ---
    {
        "benchmark_id": "easy-hp-cf289a",
        "manufacturer": "HP",
        "mpn": "CF289A",
        "description": "HP 89A black toner cartridge CF289A",
        "category": "office",
        "expected_condition": "NEW",
        "expected_uom": "EA",
        "expected_pack": 1,
        "known_public_seller": "staples.com",
        "known_url_hint": "https://www.staples.com/search?query=CF289A",
        "easy": True,
    },
    {
        "benchmark_id": "easy-brother-tn760",
        "manufacturer": "Brother",
        "mpn": "TN760",
        "description": "Brother TN760 high yield black toner",
        "category": "office",
        "expected_condition": "NEW",
        "expected_uom": "EA",
        "expected_pack": 1,
        "known_public_seller": "staples.com",
        "known_url_hint": "https://www.staples.com/search?query=TN760",
        "easy": True,
    },
    # --- industrial / furniture ID ---
    {
        "benchmark_id": "easy-global-dwt-6",
        "manufacturer": "Global Industrial",
        "mpn": "DWT-6",
        "description": "Global Industrial DWT-6 dock wheel",
        "category": "industrial",
        "expected_condition": "NEW",
        "expected_uom": "EA",
        "expected_pack": 1,
        "known_public_seller": "globalindustrial.com",
        "known_url_hint": "https://www.globalindustrial.com/search?q=DWT-6",
        "easy": True,
    },
    {
        "benchmark_id": "easy-brady-121943",
        "manufacturer": "Brady",
        "mpn": "121943",
        "description": "Brady lockout tag 121943",
        "category": "mro",
        "expected_condition": "NEW",
        "expected_uom": "PK",
        "expected_pack": 1,
        "known_public_seller": "grainger.com",
        "known_url_hint": "https://www.grainger.com/search?searchQuery=121943",
        "easy": True,
    },
    {
        "benchmark_id": "easy-crc-05089",
        "manufacturer": "CRC",
        "mpn": "05089",
        "description": "CRC QD Electronic Cleaner 05089",
        "category": "mro",
        "expected_condition": "NEW",
        "expected_uom": "EA",
        "expected_pack": 1,
        "known_public_seller": "grainger.com",
        "known_url_hint": "https://www.grainger.com/search?searchQuery=CRC%2005089",
        "easy": True,
    },
    {
        "benchmark_id": "easy-loctite-242",
        "manufacturer": "Loctite",
        "mpn": "24221",
        "description": "Loctite 242 Threadlocker blue 10ml",
        "category": "mro",
        "expected_condition": "NEW",
        "expected_uom": "EA",
        "expected_pack": 1,
        "known_public_seller": "grainger.com",
        "known_url_hint": "https://www.grainger.com/search?searchQuery=24221",
        "easy": True,
    },
    {
        "benchmark_id": "easy-wd40-490040",
        "manufacturer": "WD-40",
        "mpn": "490040",
        "description": "WD-40 Multi-Use Product 11 oz aerosol",
        "category": "mro",
        "expected_condition": "NEW",
        "expected_uom": "EA",
        "expected_pack": 1,
        "known_public_seller": "grainger.com",
        "known_url_hint": "https://www.grainger.com/search?searchQuery=490040",
        "easy": True,
    },
    # --- vehicle / heavy ---
    {
        "benchmark_id": "easy-fleetguard-lf9009",
        "manufacturer": "Fleetguard",
        "mpn": "LF9009",
        "description": "Fleetguard LF9009 lube filter",
        "category": "automotive_heavy",
        "expected_condition": "NEW",
        "expected_uom": "EA",
        "expected_pack": 1,
        "known_public_seller": "finditparts.com",
        "known_url_hint": "https://www.finditparts.com/search?q=LF9009",
        "easy": True,
    },
    {
        "benchmark_id": "easy-fleetguard-ff63009",
        "manufacturer": "Fleetguard",
        "mpn": "FF63009",
        "description": "Fleetguard FF63009 fuel filter",
        "category": "automotive_heavy",
        "expected_condition": "NEW",
        "expected_uom": "EA",
        "expected_pack": 1,
        "known_public_seller": "finditparts.com",
        "known_url_hint": "https://www.finditparts.com/search?q=FF63009",
        "easy": True,
    },
    {
        "benchmark_id": "easy-cummins-4938461",
        "manufacturer": "Cummins",
        "mpn": "4938461",
        "description": "Cummins 4938461 gasket",
        "category": "automotive_heavy",
        "expected_condition": "NEW",
        "expected_uom": "EA",
        "expected_pack": 1,
        "known_public_seller": "finditparts.com",
        "known_url_hint": "https://www.finditparts.com/search?q=4938461",
        "easy": True,
    },
    {
        "benchmark_id": "easy-gates-38507",
        "manufacturer": "Gates",
        "mpn": "38507",
        "description": "Gates Green Stripe coolant hose 38507",
        "category": "automotive_heavy",
        "expected_condition": "NEW",
        "expected_uom": "EA",
        "expected_pack": 1,
        "known_public_seller": "grainger.com",
        "known_url_hint": "https://www.grainger.com/search?searchQuery=38507",
        "easy": True,
    },
    {
        "benchmark_id": "easy-timken-set6",
        "manufacturer": "Timken",
        "mpn": "SET6",
        "description": "Timken SET6 bearing set",
        "category": "automotive_heavy",
        "expected_condition": "NEW",
        "expected_uom": "EA",
        "expected_pack": 1,
        "known_public_seller": "grainger.com",
        "known_url_hint": "https://www.grainger.com/search?searchQuery=SET6",
        "easy": True,
    },
    # --- quote-only / specialty controls (not in coverage denom) ---
    {
        "benchmark_id": "quote-myers-23499c011",
        "manufacturer": "FE Myers",
        "mpn": "23499C011",
        "description": "FE Myers capacitor kit WG30 23499C011",
        "category": "specialty_oem",
        "expected_condition": "NEW",
        "expected_uom": "EA",
        "expected_pack": 1,
        "known_public_seller": None,
        "truth_seed": QUOTE_ONLY_CONFIRMED,
        "easy": False,
        "notes": "Prior M3: no public price after exhaust; OEM/distributor quote path",
    },
    {
        "benchmark_id": "quote-cummins-5579409px",
        "manufacturer": "Cummins",
        "mpn": "5579409PX",
        "description": "Cummins ReCon injector kit 5579409PX",
        "category": "automotive_heavy",
        "expected_condition": "RECONDITIONED",
        "expected_uom": "EA",
        "expected_pack": 1,
        "known_public_seller": "shop.cummins.com",
        "known_url_hint": "https://shop.cummins.com",
        "truth_seed": PUBLIC_PRICE_EXISTS_BUT_ROUTE_DIFFICULT,
        "easy": False,
        "notes": "ReCon — not NEW under NEW_ASSUMED; condition protection test",
        "condition_test": True,
    },
]

# Additional commercially common exact-ID items to reach >=100
_EXTRA: list[dict[str, Any]] = [
    {"benchmark_id": "mro-3m-60926", "manufacturer": "3M", "mpn": "60926", "description": "3M 60926 Multi Gas/Vapor Cartridge", "category": "ppe", "known_public_seller": "grainger.com", "easy": True},
    {"benchmark_id": "mro-3m-5p71", "manufacturer": "3M", "mpn": "5P71", "description": "3M 5P71 Particulate Filter", "category": "ppe", "known_public_seller": "grainger.com", "easy": True},
    {"benchmark_id": "mro-honeywell-n5500", "manufacturer": "Honeywell", "mpn": "N5500", "description": "Honeywell North N5500 half mask", "category": "ppe", "known_public_seller": "grainger.com", "easy": True},
    {"benchmark_id": "tool-milwaukee-48-22-3078", "manufacturer": "Milwaukee", "mpn": "48-22-3078", "description": "Milwaukee 6in1 pliers", "category": "tools", "known_public_seller": "grainger.com", "easy": True},
    {"benchmark_id": "tool-milwaukee-48-22-6105", "manufacturer": "Milwaukee", "mpn": "48-22-6105", "description": "Milwaukee tape measure 25ft", "category": "tools", "known_public_seller": "grainger.com", "easy": True},
    {"benchmark_id": "tool-dewalt-dwht56027", "manufacturer": "DEWALT", "mpn": "DWHT56027", "description": "DEWALT folding jab saw", "category": "tools", "known_public_seller": "zoro.com", "easy": True},
    {"benchmark_id": "tool-klein-d2000-9neat", "manufacturer": "Klein", "mpn": "D2000-9NEAT", "description": "Klein high-leverage side cutters", "category": "tools", "known_public_seller": "grainger.com", "easy": True},
    {"benchmark_id": "tool-fluke-117", "manufacturer": "Fluke", "mpn": "117", "description": "Fluke 117 digital multimeter", "category": "electrical", "known_public_seller": "grainger.com", "easy": True},
    {"benchmark_id": "elec-ideal-61-744", "manufacturer": "Ideal", "mpn": "61-744", "description": "Ideal SureTrace circuit tracer", "category": "electrical", "known_public_seller": "grainger.com", "easy": True},
    {"benchmark_id": "elec-leviton-1221-2", "manufacturer": "Leviton", "mpn": "1221-2", "description": "Leviton single pole switch 1221-2", "category": "electrical", "known_public_seller": "grainger.com", "easy": True},
    {"benchmark_id": "elec-hubbell-hbl5362", "manufacturer": "Hubbell", "mpn": "HBL5362", "description": "Hubbell HBL5362 duplex receptacle", "category": "electrical", "known_public_seller": "grainger.com", "easy": True},
    {"benchmark_id": "plumb-sharkbite-u248lfa", "manufacturer": "SharkBite", "mpn": "U248LFA", "description": "SharkBite 1/2 ball valve U248LFA", "category": "plumbing", "known_public_seller": "supplyhouse.com", "easy": True},
    {"benchmark_id": "plumb-sharkbite-u072lfa", "manufacturer": "SharkBite", "mpn": "U072LFA", "description": "SharkBite 1/2x1/2 coupling", "category": "plumbing", "known_public_seller": "supplyhouse.com", "easy": True},
    {"benchmark_id": "plumb-watts-lfn45bmu", "manufacturer": "Watts", "mpn": "LFN45BMU-3/4", "description": "Watts pressure reducing valve", "category": "plumbing", "known_public_seller": "supplyhouse.com", "easy": True},
    {"benchmark_id": "plumb-sioux-695-G", "manufacturer": "Sioux Chief", "mpn": "695-G", "description": "Sioux Chief OxBox washing machine outlet box", "category": "plumbing", "known_public_seller": "supplyhouse.com", "easy": True},
    {"benchmark_id": "hvac-honeywell-rth2300b", "manufacturer": "Honeywell", "mpn": "RTH2300B", "description": "Honeywell RTH2300B thermostat", "category": "hvac", "known_public_seller": "grainger.com", "easy": True},
    {"benchmark_id": "hvac-aprilaire-201", "manufacturer": "Aprilaire", "mpn": "201", "description": "Aprilaire 201 replacement filter", "category": "hvac", "known_public_seller": "supplyhouse.com", "easy": True},
    {"benchmark_id": "hvac-resideo-th6220u2000", "manufacturer": "Resideo", "mpn": "TH6220U2000", "description": "Resideo FocusPRO 6000 thermostat", "category": "hvac", "known_public_seller": "supplyhouse.com", "easy": True},
    {"benchmark_id": "light-sylvania-40771", "manufacturer": "Sylvania", "mpn": "40771", "description": "Sylvania LED A19 40771", "category": "lighting", "known_public_seller": "1000bulbs.com", "easy": True},
    {"benchmark_id": "light-ge-93129788", "manufacturer": "GE", "mpn": "93129788", "description": "GE LED BR30 93129788", "category": "lighting", "known_public_seller": "1000bulbs.com", "easy": True},
    {"benchmark_id": "light-lithonia-2gtl4", "manufacturer": "Lithonia", "mpn": "2GTL4", "description": "Lithonia 2GTL4 LED troffer", "category": "lighting", "known_public_seller": "grainger.com", "easy": True, "optional": True},
    {"benchmark_id": "office-hp-cf410a", "manufacturer": "HP", "mpn": "CF410A", "description": "HP 410A black toner CF410A", "category": "office", "known_public_seller": "staples.com", "easy": True},
    {"benchmark_id": "office-epson-t252120", "manufacturer": "Epson", "mpn": "T252120", "description": "Epson T252120 black ink", "category": "office", "known_public_seller": "staples.com", "easy": True},
    {"benchmark_id": "office-bic-gsm11-be", "manufacturer": "BIC", "mpn": "GSM11-BE", "description": "BIC Round Stic blue pens dozen", "category": "office", "known_public_seller": "staples.com", "easy": True},
    {"benchmark_id": "ind-global-wb218294", "manufacturer": "Global Industrial", "mpn": "WB218294", "description": "Global Industrial wire shelving unit", "category": "industrial", "known_public_seller": "globalindustrial.com", "easy": True, "optional": True},
    {"benchmark_id": "ind-rubbermaid-fg9t6700", "manufacturer": "Rubbermaid", "mpn": "FG9T6700BLA", "description": "Rubbermaid Brute dolly", "category": "industrial", "known_public_seller": "grainger.com", "easy": True},
    {"benchmark_id": "ind-brady-121944", "manufacturer": "Brady", "mpn": "121944", "description": "Brady lockout hasp 121944", "category": "mro", "known_public_seller": "grainger.com", "easy": True},
    {"benchmark_id": "mro-crc-05005", "manufacturer": "CRC", "mpn": "05005", "description": "CRC Brakleen 05005", "category": "mro", "known_public_seller": "grainger.com", "easy": True},
    {"benchmark_id": "mro-loctite-262", "manufacturer": "Loctite", "mpn": "26221", "description": "Loctite 262 Threadlocker red", "category": "mro", "known_public_seller": "grainger.com", "easy": True},
    {"benchmark_id": "mro-permatex-80078", "manufacturer": "Permatex", "mpn": "80078", "description": "Permatex Ultra Black RTV", "category": "mro", "known_public_seller": "grainger.com", "easy": True},
    {"benchmark_id": "mro-3m-08884", "manufacturer": "3M", "mpn": "08884", "description": "3M Super 77 spray adhesive", "category": "mro", "known_public_seller": "grainger.com", "easy": True},
    {"benchmark_id": "auto-fleetguard-lf14000nn", "manufacturer": "Fleetguard", "mpn": "LF14000NN", "description": "Fleetguard LF14000NN oil filter", "category": "automotive_heavy", "known_public_seller": "finditparts.com", "easy": True},
    {"benchmark_id": "auto-fleetguard-af26154", "manufacturer": "Fleetguard", "mpn": "AF26154", "description": "Fleetguard AF26154 air filter", "category": "automotive_heavy", "known_public_seller": "finditparts.com", "easy": True},
    {"benchmark_id": "auto-fleetguard-wf2126", "manufacturer": "Fleetguard", "mpn": "WF2126", "description": "Fleetguard WF2126 coolant filter", "category": "automotive_heavy", "known_public_seller": "finditparts.com", "easy": True},
    {"benchmark_id": "auto-cummins-3963983", "manufacturer": "Cummins", "mpn": "3963983", "description": "Cummins 3963983 oil pan gasket", "category": "automotive_heavy", "known_public_seller": "finditparts.com", "easy": True},
    {"benchmark_id": "auto-gates-36795", "manufacturer": "Gates", "mpn": "36795", "description": "Gates Micro-V belt 36795", "category": "automotive_heavy", "known_public_seller": "grainger.com", "easy": True},
    {"benchmark_id": "auto-timken-set47", "manufacturer": "Timken", "mpn": "SET47", "description": "Timken SET47 bearing", "category": "automotive_heavy", "known_public_seller": "grainger.com", "easy": True},
    {"benchmark_id": "auto-wix-51515", "manufacturer": "WIX", "mpn": "51515", "description": "WIX 51515 oil filter", "category": "automotive_heavy", "known_public_seller": "grainger.com", "easy": True},
    {"benchmark_id": "auto-fram-ph8a", "manufacturer": "FRAM", "mpn": "PH8A", "description": "FRAM PH8A oil filter", "category": "automotive_heavy", "known_public_seller": "grainger.com", "easy": True},
    {"benchmark_id": "furn-hon-h5701", "manufacturer": "HON", "mpn": "H5701", "description": "HON Volt task chair H5701", "category": "furniture", "known_public_seller": "staples.com", "easy": True, "optional": True},
    {"benchmark_id": "furn-safco-1201bl", "manufacturer": "Safco", "mpn": "1201BL", "description": "Safco TaskMaster industrial stool", "category": "furniture", "known_public_seller": "globalindustrial.com", "easy": True, "optional": True},
    {"benchmark_id": "ppe-ansi-z89", "manufacturer": "MSA", "mpn": "10034018", "description": "MSA V-Gard hard hat", "category": "ppe", "known_public_seller": "grainger.com", "easy": True},
    {"benchmark_id": "ppe-uvex-s3960c", "manufacturer": "Uvex", "mpn": "S3960C", "description": "Uvex Stealth safety goggles", "category": "ppe", "known_public_seller": "grainger.com", "easy": True},
    {"benchmark_id": "ppe-ansell-37-175", "manufacturer": "Ansell", "mpn": "37-175", "description": "Ansell HyFlex gloves 37-175", "category": "ppe", "known_public_seller": "grainger.com", "easy": True},
    {"benchmark_id": "tool-crescent-ac216cvs", "manufacturer": "Crescent", "mpn": "AC216CVS", "description": "Crescent adjustable wrench 8in", "category": "tools", "known_public_seller": "grainger.com", "easy": True},
    {"benchmark_id": "tool-channellock-430", "manufacturer": "Channellock", "mpn": "430", "description": "Channellock 430 tongue groove pliers", "category": "tools", "known_public_seller": "grainger.com", "easy": True},
    {"benchmark_id": "tool-irwin-2073212", "manufacturer": "IRWIN", "mpn": "2073212", "description": "IRWIN VISE-GRIP locking pliers", "category": "tools", "known_public_seller": "grainger.com", "easy": True},
    {"benchmark_id": "mro-jb-weld-8265", "manufacturer": "J-B Weld", "mpn": "8265", "description": "J-B Weld Original cold weld", "category": "mro", "known_public_seller": "grainger.com", "easy": True},
    {"benchmark_id": "mro-gorilla-50036", "manufacturer": "Gorilla", "mpn": "50036", "description": "Gorilla Tape 35 yd", "category": "mro", "known_public_seller": "grainger.com", "easy": True},
    {"benchmark_id": "mro-duct-tape-297", "manufacturer": "3M", "mpn": "2979", "description": "3M Scotch heavy duty duct tape", "category": "mro", "known_public_seller": "grainger.com", "easy": True},
    {"benchmark_id": "elec-ideal-35-903", "manufacturer": "Ideal", "mpn": "35-903", "description": "Ideal T-stripper wire stripper", "category": "electrical", "known_public_seller": "grainger.com", "easy": True},
    {"benchmark_id": "elec-klein-11055", "manufacturer": "Klein", "mpn": "11055", "description": "Klein Klein-Kurve wire stripper", "category": "electrical", "known_public_seller": "grainger.com", "easy": True},
    {"benchmark_id": "light-feit-a19", "manufacturer": "Feit", "mpn": "OM60/950CA/AG", "description": "Feit Electric LED A19", "category": "lighting", "known_public_seller": "1000bulbs.com", "easy": True, "optional": True},
    {"benchmark_id": "plumb-oatey-30241", "manufacturer": "Oatey", "mpn": "30241", "description": "Oatey purple primer 30241", "category": "plumbing", "known_public_seller": "supplyhouse.com", "easy": True},
    {"benchmark_id": "plumb-oatey-31016", "manufacturer": "Oatey", "mpn": "31016", "description": "Oatey PVC cement 31016", "category": "plumbing", "known_public_seller": "supplyhouse.com", "easy": True},
    {"benchmark_id": "plumb-sioux-886-GP", "manufacturer": "Sioux Chief", "mpn": "886-GP", "description": "Sioux Chief Toilet Flange", "category": "plumbing", "known_public_seller": "supplyhouse.com", "easy": True},
    {"benchmark_id": "hvac-honeywell-th4110u2005", "manufacturer": "Honeywell", "mpn": "TH4110U2005", "description": "Honeywell PRO 4000 thermostat", "category": "hvac", "known_public_seller": "supplyhouse.com", "easy": True},
    {"benchmark_id": "hvac-carrier-kfnag0101acl", "manufacturer": "Carrier", "mpn": "KFNAG0101ACL", "description": "Carrier air filter", "category": "hvac", "known_public_seller": "supplyhouse.com", "easy": False, "truth_seed": AMBIGUOUS, "optional": True},
    {"benchmark_id": "quote-smithblair-317", "manufacturer": "Smith-Blair", "mpn": "317", "description": "Smith-Blair 317 coupling short PN", "category": "specialty_oem", "known_public_seller": None, "truth_seed": AMBIGUOUS, "easy": False},
    {"benchmark_id": "quote-specialty-tl12r2", "manufacturer": None, "mpn": "TL12R2-CR", "description": "Skid steer track TL12R2-CR", "category": "specialty_oem", "known_public_seller": None, "truth_seed": PUBLIC_PRICE_EXISTS_BUT_ROUTE_DIFFICULT, "easy": False},
    {"benchmark_id": "noprice-bogus-xyz999", "manufacturer": "NoSuchBrand", "mpn": "XYZ999NOPRICE", "description": "Synthetic no-public-price control", "category": "mro", "known_public_seller": None, "truth_seed": NO_PUBLIC_PRICE_CONFIRMED, "easy": False},
]


def _normalize_seed(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "benchmark_id": row["benchmark_id"],
        "manufacturer": row.get("manufacturer"),
        "mpn": row["mpn"],
        "model": row.get("model") or row["mpn"],
        "description": row.get("description") or "",
        "category": row.get("category") or "mro",
        "expected_condition": row.get("expected_condition") or "NEW",
        "expected_uom": row.get("expected_uom") or "EA",
        "expected_pack": row.get("expected_pack") or 1,
        "known_public_seller": row.get("known_public_seller"),
        "known_public_price": row.get("known_public_price"),
        "known_url_hint": row.get("known_url_hint"),
        "date_verified": None,
        "source": None,
        "notes": row.get("notes") or "",
        "truth_class": row.get("truth_seed"),
        "easy": bool(row.get("easy")),
        "optional": bool(row.get("optional")),
        "condition_test": bool(row.get("condition_test")),
        "price_tolerance_pct": float(row.get("price_tolerance_pct") or 0.30),
        "opportunity_id": row.get("opportunity_id"),
    }


def seed_items() -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for raw in _SEED + _EXTRA:
        n = _normalize_seed(raw)
        key = n["benchmark_id"]
        if key in seen:
            continue
        seen.add(key)
        out.append(n)
    return out


def harvest_live_m3_identities(*, limit: int = 40, max_per_opp: int = 3) -> list[dict[str, Any]]:
    """Supplement corpus from live M3 opportunity identities (diverse buyers)."""
    from collections import Counter

    from acquisition_scale.prioritize import load_same_100
    from eligibility_and_recovery.spec_identity import enrich_identity_for_research
    from evidence_breakthrough.corpus import load_identity_store

    oids, _ = load_same_100()
    packs = load_identity_store().get("by_opportunity") or {}
    per_opp: Counter[str] = Counter()
    out: list[dict[str, Any]] = []
    seen_pn: set[str] = set()
    for oid in oids:
        if len(out) >= limit:
            break
        for i in (packs.get(oid) or {}).get("identities") or []:
            if not isinstance(i, dict):
                continue
            if per_opp[oid] >= max_per_opp:
                break
            e = enrich_identity_for_research(dict(i))
            pn = str(e.get("part_number") or e.get("catalog_number") or e.get("model") or "").strip()
            tok = pn.split()[0] if pn else ""
            if not tok or len(tok) < 5 or not any(ch.isdigit() for ch in tok):
                continue
            key = tok.upper()
            if key in seen_pn:
                continue
            seen_pn.add(key)
            per_opp[oid] += 1
            out.append(
                _normalize_seed(
                    {
                        "benchmark_id": f"live-{oid.split(':')[-1]}-{key}",
                        "manufacturer": e.get("manufacturer") or e.get("brand"),
                        "mpn": tok,
                        "description": (e.get("raw_description") or "")[:160],
                        "category": "live_m3",
                        "known_public_seller": None,
                        "opportunity_id": oid,
                        "easy": False,
                        "truth_seed": None,
                    }
                )
            )
            if len(out) >= limit:
                break
    return out


def build_corpus(*, include_live: bool = True, verify: bool = False, max_verify: int = 40) -> dict[str, Any]:
    """Build >=100 item corpus; optionally live-verify known seller routes."""
    items = seed_items()
    if include_live:
        existing = {i["mpn"].upper() for i in items}
        for row in harvest_live_m3_identities(limit=50):
            if row["mpn"].upper() in existing:
                continue
            items.append(row)
            existing.add(row["mpn"].upper())

    verified = 0
    if verify:
        from price_coverage_80.verify import verify_benchmark_item

        for item in items:
            if verified >= max_verify:
                break
            if item.get("truth_class") in {
                QUOTE_ONLY_CONFIRMED,
                NO_PUBLIC_PRICE_CONFIRMED,
                AMBIGUOUS,
            }:
                item["date_verified"] = now_utc().isoformat()
                item["source"] = "seed_truth"
                continue
            if not item.get("known_public_seller") and not item.get("known_url_hint"):
                continue
            result = verify_benchmark_item(item)
            item.update(result)
            verified += 1

    # Unverified easy seeds with known seller → provisional CONFIRMED if seller known
    # (live verify preferred; provisional marked in notes)
    for item in items:
        if item.get("truth_class"):
            continue
        if item.get("easy") and item.get("known_public_seller"):
            item["truth_class"] = PUBLIC_NEW_PRICE_CONFIRMED
            item["notes"] = (item.get("notes") or "") + " | provisional_confirmed_known_seller"
            item["source"] = item.get("source") or "seed_known_seller"
        elif item.get("opportunity_id"):
            item["truth_class"] = AMBIGUOUS
            item["source"] = "live_m3_unverified"
        else:
            item["truth_class"] = AMBIGUOUS

    payload = {
        "kind": "PriceCoverage80BenchmarkCorpus",
        "build": BUILD,
        "n_items": len(items),
        "n_easy": sum(1 for i in items if i.get("easy")),
        "n_confirmed_public": sum(1 for i in items if i.get("truth_class") == PUBLIC_NEW_PRICE_CONFIRMED),
        "items": items,
        "updated_at": now_utc().isoformat(),
        "run_id": f"BM80-{now_utc().strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:6]}",
    }
    p = data_path(CORPUS_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return payload


def load_corpus() -> dict[str, Any]:
    p = data_path(CORPUS_FILE)
    if not p.exists():
        return build_corpus(include_live=True, verify=False)
    return json.loads(p.read_text(encoding="utf-8"))


def easy_25(corpus: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    c = corpus or load_corpus()
    easy = [i for i in (c.get("items") or []) if i.get("easy") and i.get("truth_class") == PUBLIC_NEW_PRICE_CONFIRMED]
    if len(easy) < 25:
        easy = [i for i in (c.get("items") or []) if i.get("easy")][:25]
    return easy[:25]


def confirmed_public(corpus: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    c = corpus or load_corpus()
    return [i for i in (c.get("items") or []) if i.get("truth_class") == PUBLIC_NEW_PRICE_CONFIRMED]
