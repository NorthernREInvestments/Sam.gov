"""Open-web product discovery constants.

Build: 20261004-m3-open-web-product-discovery-v1
"""

from __future__ import annotations

BUILD = "20261004-m3-open-web-product-discovery-v1"

# Artifacts
TRANSPORT_STATS = "m3_open_web_search_transport_v1.json"
URL_DB = "m3_open_web_product_url_db_v1.json"
DOMAIN_RULES = "m3_open_web_domain_rules_v1.json"
SELLER_GRAPH = "m3_open_web_seller_graph_v1.json"
CK = "m3_open_web_product_discovery_v1_checkpoint.json"
JOB = "m3_open_web_product_discovery_v1_job.json"
REPORT = "m3_open_web_product_discovery_v1_last_report.json"
PAGE_CACHE = "m3_open_web_page_cache_v1.json"

PRIOR_EPU_CK = "m3_exact_product_url_discovery_v1_checkpoint.json"
PRIOR_HM_CK = "m3_hard_miss_recovery_v1_checkpoint.json"

EXACT_PRODUCT_CANDIDATE = "EXACT_PRODUCT_CANDIDATE"
EXACT_PRODUCT_VERIFIED = "EXACT_PRODUCT_VERIFIED"
SEARCH_PAGE = "SEARCH_PAGE"
CATEGORY_PAGE = "CATEGORY_PAGE"
BLOG = "BLOG"
PDF_ONLY = "PDF_ONLY"
PARTIAL_MATCH = "PARTIAL_MATCH"
WRONG_PRODUCT = "WRONG_PRODUCT"
MARKETING_PAGE = "MARKETING_PAGE"
PLACEHOLDER = "PLACEHOLDER"

REJECTED_RESULT_TYPES = {
    SEARCH_PAGE,
    CATEGORY_PAGE,
    BLOG,
    PDF_ONLY,
    PARTIAL_MATCH,
    WRONG_PRODUCT,
    MARKETING_PAGE,
    PLACEHOLDER,
}

READY_FOR_PRICE_EXTRACTION = "READY_FOR_PRICE_EXTRACTION"
URL_DISCOVERY_PENDING = "URL_DISCOVERY_PENDING"

TARGET_NEW_EXACT_URLS = 30
ORIGINAL_DENOM = 82
HONEST_PRICED = 38
EASY25_COVERAGE_FLOOR = 0.80
EASY25_ACCURACY_FLOOR = 0.95

# Hard-miss priority order (same unresolved corpus; prioritize these)
HARD_FOCUS = [
    "easy-watts-lf777m2",
    "easy-brady-121943",
    "easy-makita-b-45580",
    "easy-global-dwt-6",
    "easy-filtete-mpr2200",
    "easy-leviton-5320",
    "easy-hp-cf289a",
    "easy-philips-led",
    "easy-3m-2097",
    "easy-wd40-490040",
]

PROGRESS_EVERY = 5
ITEM_DEADLINE_S = 35.0
MAX_CANDIDATES_VALIDATE = 8
MAX_QUERIES_PER_ITEM = 4

# Category → preferred public seller domains (open/fetchable preferred)
CATEGORY_DOMAINS: dict[str, list[str]] = {
    "electrical": ["platt.com", "homelectrical.com", "standardelectricsupply.com", "rspsupply.com", "leviton.com"],
    "tools": ["platt.com", "channellock.com", "kleintools.com", "makitatools.com", "acmetools.com"],
    "plumbing": ["activeplumbing.com", "pexuniverse.com", "supplyhouse.com", "parts-hvac.com"],
    "hvac": ["gsistore.com", "shop.aprilaire.com", "parts-hvac.com", "supplyhouse.com"],
    "automotive_heavy": ["dieselpartsdirect.com", "thedieselstore.com", "rockauto.com", "summitracing.com"],
    "lighting": ["1000bulbs.com", "homelectrical.com", "bulbs.com", "feit.com"],
    "office": ["officedepot.com", "quill.com", "staples.com"],
    "ppe": ["pksafety.com", "northernsafety.com", "seton.com"],
    "mro": ["homelectrical.com", "northernsafety.com", "crcautocare.com"],
    "industrial": ["globalindustrial.com", "northernsafety.com"],
    "furniture": ["globalindustrial.com"],
    "default": ["platt.com", "homelectrical.com", "activeplumbing.com", "dieselpartsdirect.com"],
}
