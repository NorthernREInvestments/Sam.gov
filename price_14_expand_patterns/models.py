"""Price the 14 + product-family / domain pattern expansion.

Build: 20261005-m3-price-14-expand-patterns-v1
"""

from __future__ import annotations

BUILD = "20261005-m3-price-14-expand-patterns-v1"

CK = "m3_price_14_expand_patterns_v1_checkpoint.json"
JOB = "m3_price_14_expand_patterns_v1_job.json"
REPORT = "m3_price_14_expand_patterns_v1_last_report.json"
PATTERN_DB = "m3_domain_product_family_adapter_v1.json"
ROUTE_MEMORY = "m3_manufacturer_route_memory_v1.json"
PRICE_DB = "m3_price_14_expand_priced_v1.json"

PRIOR_OWP_REPORT = "m3_open_web_product_discovery_v1_last_report.json"
PRIOR_OWP_CK = "m3_open_web_product_discovery_v1_checkpoint.json"
PRIOR_HM_CK = "m3_hard_miss_recovery_v1_checkpoint.json"

ORIGINAL_DENOM = 82
HONEST_PRICED = 38
TARGET_PRICED = 66
TARGET_COVERAGE = 0.80
TARGET_ACCURACY = 0.95
EASY25_COVERAGE_FLOOR = 0.80
EASY25_ACCURACY_FLOOR = 0.95

READY_14_IDS = [
    "auto-wix-51515",
    "tool-channellock-430",
    "elec-klein-11055",
    "easy-3m-2097",
    "easy-hp-cf289a",
    "hvac-aprilaire-201",
    "hvac-honeywell-rth2300b",
    "hvac-resideo-th6220u2000",
    "mro-3m-5p71",
    "mro-3m-60926",
    "mro-loctite-262",
    "office-epson-t252120",
    "office-hp-cf410a",
    "plumb-sioux-886-GP",
]

# Seed domain → manufacturer/family preferences learned from open-web v1
SEED_FAMILY_ROUTES: list[dict] = [
    {
        "manufacturer_family": "HP toner",
        "manufacturers": ["HP"],
        "categories": ["office"],
        "domain": "quill.com",
        "discovery_route": "INTERNAL_SEARCH_CONVERSION",
        "price_route": "JSON_LD",
        "search_pattern": "https://www.quill.com/search?keywords={mfr}+{mpn}",
        "pdp_pattern": "/cbs/",
    },
    {
        "manufacturer_family": "Epson ink",
        "manufacturers": ["Epson"],
        "categories": ["office"],
        "domain": "quill.com",
        "discovery_route": "INTERNAL_SEARCH_CONVERSION",
        "price_route": "JSON_LD",
        "search_pattern": "https://www.quill.com/search?keywords={mfr}+{mpn}",
        "pdp_pattern": "/cbs/",
    },
    {
        "manufacturer_family": "3M MRO/electrical",
        "manufacturers": ["3M"],
        "categories": ["mro", "ppe", "electrical"],
        "domain": "platt.com",
        "discovery_route": "PLATT_GRAPHQL_SUGGEST",
        "price_route": "JSON_LD",
        "search_pattern": "graphql:ProductSuggest",
        "pdp_pattern": "/p/{routeId}/{slug}",
    },
    {
        "manufacturer_family": "Klein tools",
        "manufacturers": ["Klein"],
        "categories": ["tools", "electrical"],
        "domain": "platt.com",
        "discovery_route": "PLATT_GRAPHQL_SUGGEST",
        "price_route": "JSON_LD",
        "pdp_pattern": "/p/{routeId}/{slug}",
    },
    {
        "manufacturer_family": "Channellock tools",
        "manufacturers": ["Channellock"],
        "categories": ["tools"],
        "domain": "platt.com",
        "discovery_route": "CURATED_EXACT",
        "price_route": "JSON_LD",
        "pdp_pattern": "/p/{routeId}/{slug}",
    },
    {
        "manufacturer_family": "Aprilaire HVAC",
        "manufacturers": ["Aprilaire"],
        "categories": ["hvac"],
        "domain": "shop.aprilaire.com",
        "discovery_route": "CURATED_EXACT",
        "price_route": "JSON_LD",
        "pdp_pattern": "/products/",
    },
    {
        "manufacturer_family": "Honeywell/Resideo HVAC",
        "manufacturers": ["Honeywell", "Resideo"],
        "categories": ["hvac"],
        "domain": "gsistore.com",
        "discovery_route": "CURATED_EXACT",
        "price_route": "STATIC",
        "pdp_pattern": "/products/",
    },
    {
        "manufacturer_family": "Honeywell Home",
        "manufacturers": ["Honeywell"],
        "categories": ["hvac"],
        "domain": "honeywellhome.com",
        "discovery_route": "MANUFACTURER_PRODUCT_PAGE",
        "price_route": "STATIC",
        "pdp_pattern": "/products/",
    },
    {
        "manufacturer_family": "3M respiratory",
        "manufacturers": ["3M"],
        "categories": ["mro", "ppe"],
        "domain": "homelectrical.com",
        "discovery_route": "CURATED_EXACT",
        "price_route": "STATIC",
        "pdp_pattern": ".html",
    },
    {
        "manufacturer_family": "3M safety",
        "manufacturers": ["3M"],
        "categories": ["mro", "ppe"],
        "domain": "pksafety.com",
        "discovery_route": "INTERNAL_SEARCH_CONVERSION",
        "price_route": "STATIC",
        "pdp_pattern": "/products/",
    },
    {
        "manufacturer_family": "Loctite MRO",
        "manufacturers": ["Loctite"],
        "categories": ["mro"],
        "domain": "rshughes.com",
        "discovery_route": "MANUFACTURER_PRODUCT_PAGE",
        "price_route": "STATIC",
        "pdp_pattern": "/p/",
    },
    {
        "manufacturer_family": "Sioux Chief plumbing",
        "manufacturers": ["Sioux Chief"],
        "categories": ["plumbing"],
        "domain": "activeplumbing.com",
        "discovery_route": "CURATED_EXACT",
        "price_route": "STATIC",
        "pdp_pattern": "/buy/product/",
    },
    {
        "manufacturer_family": "WIX auto filters",
        "manufacturers": ["WIX"],
        "categories": ["automotive_heavy", "auto"],
        "domain": "rockauto.com",
        "discovery_route": "CURATED_EXACT",
        "price_route": "BROWSER",
        "pdp_pattern": "/en/parts/",
    },
]

PROGRESS_EVERY = 5
ITEM_DEADLINE_S = 45.0
MAX_BROWSER_PER_ITEM = 1
MAX_SIBLING_CANDIDATES = 6
