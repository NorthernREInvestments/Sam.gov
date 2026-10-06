"""Seller rediscovery constants.

Build: 20261004-m3-seller-rediscovery-v1
"""

from __future__ import annotations

BUILD = "20261004-m3-seller-rediscovery-v1"

# Artifacts
FROZEN_BASELINE = "m3_seller_rediscovery_frozen_baseline_v1.json"
CK = "m3_seller_rediscovery_v1_checkpoint.json"
JOB = "m3_seller_rediscovery_v1_job.json"
REPORT = "m3_seller_rediscovery_v1_last_report.json"
DOMAIN_YIELD = "m3_seller_rediscovery_domain_yield_v1.json"
DISCOVERY_CACHE = "m3_seller_rediscovery_discovery_cache_v1.json"
PAGE_CACHE = "m3_seller_rediscovery_page_cache_v1.json"

# Prior exact-page checkpoint (source of frozen 40)
PRIOR_EPE_CK = "m3_exact_page_extract_v1_checkpoint.json"

# Status
NOT_STARTED = "NOT_STARTED"
IN_PROGRESS = "IN_PROGRESS"
PRICE_FOUND = "PRICE_FOUND"
SELLER_EXHAUSTED = "SELLER_EXHAUSTED"
PRODUCT_PUBLIC_PRICE_EXHAUSTED = "PRODUCT_PUBLIC_PRICE_EXHAUSTED"
BLOCKED_RETRYABLE = "BLOCKED_RETRYABLE"
FROZEN = "FROZEN_VALIDATED"
TERMINAL = {PRICE_FOUND, PRODUCT_PUBLIC_PRICE_EXHAUSTED, FROZEN}

# Domain suppression
LOW_YIELD_BLOCKED = "LOW_YIELD_BLOCKED"
BOT_WALL_STREAK_LIMIT = 5
ZERO_YIELD_ATTEMPT_LIMIT = 10

# Tiers
TIER_A = "TIER_A"  # >=60%
TIER_B = "TIER_B"  # 25-59%
TIER_C = "TIER_C"  # 1-24%
TIER_D = "TIER_D"  # 0% / blocked

# Extraction routes
ROUTE_JSONLD = "JSON_LD"
ROUTE_STRUCTURED = "STRUCTURED_EMBEDDED"
ROUTE_STATIC = "STATIC_MARKUP"
ROUTE_SELLER_ENDPOINT = "SELLER_SPECIFIC_ENDPOINT"
ROUTE_BROWSER = "BROWSER"
ROUTE_ALT_SELLER = "ALTERNATE_SELLER_DISCOVERY"
ROUTE_ADAPTER = "DOMAIN_ADAPTER"

# Known miss corpus (must re-run)
KNOWN_MISS_IDS = [
    "easy-watts-lf777m2",
    "easy-brady-121943",
    "easy-fleetguard-ff63009",
    "easy-makita-b-45580",
    "easy-global-dwt-6",
    "easy-milwaukee-48-22-1902",
    "quote-cummins-5579409px",  # focus item; may be outside Full-100
]

# Controls — must not regress
CONTROL_IDS = [
    "easy-leviton-5320",
    "easy-filtete-mpr2200",
    "easy-sharkbite-uc248lfa",
    "easy-honeywell-th8320u1008",
]

# Stage A seed (10 missing products — known misses + top Full-100 gaps)
STAGE_A_IDS = [
    "easy-watts-lf777m2",
    "easy-brady-121943",
    "easy-fleetguard-ff63009",
    "easy-makita-b-45580",
    "easy-global-dwt-6",
    "easy-milwaukee-48-22-1902",
    "mro-3m-60926",
    "tool-dewalt-dwht56027",
    "elec-hubbell-hbl5362",
    "plumb-sharkbite-u248lfa",
]

# Reject hosts that are marketplaces / aggregators without deterministic SKU pages
REJECT_HOST_FRAGMENTS = (
    "amazon.",
    "ebay.",
    "walmart.com",
    "alibaba.",
    "aliexpress.",
    "facebook.com",
    "youtube.com",
    "wikipedia.org",
    "pinterest.",
    "reddit.com",
    "google.com",
    "bing.com",
    "duckduckgo.com",
    "color-hex.com",
    "imagecolorpicker.com",
    "machinio.",
    "withlocals.com",
    "euroaquatours.com",
    "manualsfile.com",
    "cybo.com",
    "aritzia.com",  # fashion SKU collisions (Brady 121943)
    "cdn-documents.",
    "hubbellcdn.com",
)

# Extra open distributors to bias rediscovery toward
PREFERRED_OPEN_DISTRIBUTORS = (
    "platt.com",
    "fluke.com",
    "pexuniverse.com",
    "zoro.com",
    "webstaurantstore.com",
    "globalindustrial.com",
    "toolbarn.com",
    "acmetools.com",
    "mccoys.com",
    "parts-hvac.com",
    "dieselpartsdirect.com",
    "rshughes.com",
    "gordonelectricsupply.com",
    "standardelectricsupply.com",
    "envirosafetyproducts.com",
    "dkhardware.com",
    "voomisupply.com",
    "leestools.com",
    "pnwtoolsupply.com",
    "lowes.com",
)

# Domains with proven prior yield (starting signal, not permanent)
SEED_TIER_A = ("platt.com", "fluke.com")
SEED_TIER_D = (
    "homedepot.com",
    "grainger.com",
    "bradyid.com",
    "rockauto.com",
    "motion.com",
    "supplyhouse.com",
    "autozone.com",
)

# Gates
FULL100_TARGET_PRICED = 66
FULL100_DENOM = 82
EASY25_TARGET_PRICED = 20
COVERAGE_TARGET = 80.0
ACCURACY_TARGET = 95.0

# Efficiency
MAX_RETRIES = 2
REQUEST_TIMEOUT_S = 8.0
ITEM_DEADLINE_S = 45.0
MIN_ALT_SELLERS = 3
MAX_CANDIDATE_URLS = 10
PROGRESS_EVERY = 10
