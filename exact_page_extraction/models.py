"""Exact product page price extraction — build constants.

Build: 20261004-m3-exact-page-price-extraction-v1
"""

from __future__ import annotations

BUILD = "20261004-m3-exact-page-price-extraction-v1"

# URL confidence classes
EXACT_PRODUCT_VERIFIED = "EXACT_PRODUCT_VERIFIED"
EXACT_PRODUCT_UNVERIFIED = "EXACT_PRODUCT_UNVERIFIED"
SEARCH_RESULT_SHELL = "SEARCH_RESULT_SHELL"
CATEGORY_PAGE = "CATEGORY_PAGE"
PRODUCT_FAMILY_PAGE = "PRODUCT_FAMILY_PAGE"
WRONG_PRODUCT = "WRONG_PRODUCT"

# Extraction routes
ROUTE_STATIC = "STATIC_HTML"
ROUTE_JSONLD = "JSON_LD"
ROUTE_HYDRATION = "HYDRATION"
ROUTE_API = "API_XHR"
ROUTE_GRAPHQL = "GRAPHQL"
ROUTE_BROWSER = "BROWSER"
ROUTE_CART = "CART"
ROUTE_ALT_URL = "ALTERNATE_EXACT_URL"

# Persist
CK = "m3_exact_page_extract_v1_checkpoint.json"
JOB = "m3_exact_page_extract_v1_job.json"
REPORT = "m3_exact_page_extract_v1_last_report.json"
CORPUS = "m3_exact_page_miss_corpus_v1.json"
DOMAIN_MEMORY = "m3_exact_page_domain_memory.json"
BROWSER_CACHE = "m3_exact_page_browser_cache.json"

NOT_STARTED = "NOT_STARTED"
IN_PROGRESS = "IN_PROGRESS"
PRICE_FOUND = "PRICE_FOUND"
NO_PRICE_EXHAUSTIVE = "NO_PRICE_EXHAUSTIVE"
BLOCKED_RETRYABLE = "BLOCKED_RETRYABLE"
TERMINAL = {PRICE_FOUND, NO_PRICE_EXHAUSTIVE}

# Permanent placeholder regression
PLACEHOLDER_10_58 = 10.58

KNOWN_FOCUS = [
    {"id": "easy-watts-lf777m2", "manufacturer": "Watts", "mpn": "LF777M2-QT"},
    {"id": "easy-leviton-5320", "manufacturer": "Leviton", "mpn": "5320-S"},
    {"id": "easy-brady-121943", "manufacturer": "Brady", "mpn": "121943"},
    {"id": "easy-fleetguard-ff63009", "manufacturer": "Fleetguard", "mpn": "FF63009"},
    {"id": "easy-filtete-mpr2200", "manufacturer": "Filtrete", "mpn": "2004DC-6"},
    {"id": "easy-makita-b-45580", "manufacturer": "Makita", "mpn": "B-45580"},
    {"id": "easy-sharkbite-uc248lfa", "manufacturer": "SharkBite", "mpn": "UC248LFA"},
    {"id": "easy-honeywell-th8320u1008", "manufacturer": "Honeywell", "mpn": "TH8320U1008"},
    {"id": "easy-global-dwt-6", "manufacturer": "Global Industrial", "mpn": "DWT-6"},
    {"id": "quote-cummins-5579409px", "manufacturer": "Cummins", "mpn": "5579409PX"},
]
