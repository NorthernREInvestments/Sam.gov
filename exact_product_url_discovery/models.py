"""Exact product URL discovery constants.

Build: 20261004-m3-exact-product-url-discovery-v1
"""

from __future__ import annotations

BUILD = "20261004-m3-exact-product-url-discovery-v1"

# Artifacts
URL_DB = "m3_exact_product_url_db_v1.json"
DOMAIN_RULES = "m3_domain_product_url_rules_v1.json"
CK = "m3_exact_product_url_discovery_v1_checkpoint.json"
JOB = "m3_exact_product_url_discovery_v1_job.json"
REPORT = "m3_exact_product_url_discovery_v1_last_report.json"
PAGE_CACHE = "m3_exact_product_url_page_cache_v1.json"

PRIOR_HM_CK = "m3_hard_miss_recovery_v1_checkpoint.json"

# URL types
EXACT_PRODUCT_VERIFIED = "EXACT_PRODUCT_VERIFIED"
EXACT_PRODUCT_UNVERIFIED = "EXACT_PRODUCT_UNVERIFIED"
SEARCH_RESULT = "SEARCH_RESULT"
SEARCH_RESULT_SHELL = "SEARCH_RESULT_SHELL"
CATEGORY_PAGE = "CATEGORY_PAGE"
COLLECTION_PAGE = "COLLECTION_PAGE"
PRODUCT_FAMILY_ONLY = "PRODUCT_FAMILY_ONLY"
BLOG_PAGE = "BLOG_PAGE"
PDF_WITHOUT_PRODUCT_ID = "PDF_WITHOUT_PRODUCT_ID"
SITE_SEARCH_SHELL = "SITE_SEARCH_SHELL"
WRONG_PRODUCT = "WRONG_PRODUCT"
REJECTED = {
    SEARCH_RESULT,
    SEARCH_RESULT_SHELL,
    CATEGORY_PAGE,
    COLLECTION_PAGE,
    PRODUCT_FAMILY_ONLY,
    BLOG_PAGE,
    PDF_WITHOUT_PRODUCT_ID,
    SITE_SEARCH_SHELL,
    WRONG_PRODUCT,
}

# Discovery methods
METHOD_KNOWN_SELLER = "KNOWN_SELLER_DOMAIN"
METHOD_INTERNAL_SEARCH = "INTERNAL_SEARCH_CONVERSION"
METHOD_SERP_HREF = "SEARCH_RESULT_HREF"
METHOD_SITEMAP = "XML_SITEMAP"
METHOD_PRODUCT_SITEMAP = "PRODUCT_SITEMAP"
METHOD_MANUFACTURER = "MANUFACTURER_PRODUCT_PAGE"
METHOD_AUTH_DIST = "AUTHORIZED_DISTRIBUTOR"
METHOD_CATALOG = "DISTRIBUTOR_CATALOG"
METHOD_EMBEDDED = "EMBEDDED_PRODUCT_REF"
METHOD_FEED = "PUBLIC_FEED_API"
METHOD_PATTERN = "URL_PATTERN_DISCOVERY"
METHOD_CURATED = "CURATED_EXACT"
METHOD_BROWSER = "BROWSER_ASSISTED"

READY_FOR_PRICE_EXTRACTION = "READY_FOR_PRICE_EXTRACTION"
URL_DISCOVERY_PENDING = "URL_DISCOVERY_PENDING"

NOT_STARTED = "NOT_STARTED"
URL_FOUND = "URL_FOUND"
NO_URL = "NO_URL"

TARGET_NEW_EXACT_URLS = 30
ORIGINAL_DENOM = 82
HONEST_PRICED = 38

KNOWN_FOCUS = [
    "easy-watts-lf777m2",
    "easy-leviton-5320",
    "easy-brady-121943",
    "easy-makita-b-45580",
    "easy-fleetguard-ff63009",
    "easy-filtete-mpr2200",
    "easy-global-dwt-6",
    "quote-cummins-5579409px",
]

PROVEN_OPEN = (
    "platt.com",
    "fluke.com",
    "mccoys.com",
    "parts-hvac.com",
    "dieselpartsdirect.com",
    "crossfilters.com",
    "standardelectricsupply.com",
    "leestools.com",
    "allprodiesel.net",
    "bearingsrus.com",
    "cripedistributing.com",
    "hopkinssales.com",
    "partsource.ca",
    "plexsupply.com",
    "lightingsupply.com",
    "honeywellstore.com",
    "lifesafetycom.com",
    "cooper-electric.com",
    "globalindustrial.com",
    "crcautocare.com",
    "brother-usa.com",
    "rspsupply.com",
    "quill.com",
    "1000bulbs.com",
    "toolbarn.com",
    "acmetools.com",
    "channellock.com",
    "kleintools.com",
    "makitatools.com",
    "filtersfast.com",
    "seton.com",
    "carid.com",
    "bulbs.com",
    "feit.com",
    "loctiteproducts.com",
    "northernsafety.com",
)

DEAD_PRIMARY_IDENTITY_ONLY = {
    "grainger.com",
    "supplyhouse.com",
    "homedepot.com",
    "lowes.com",
    "rockauto.com",
    "autozone.com",
    "bradyid.com",
    "pexuniverse.com",
    "digikey.com",
    "partzilla.com",
    "rshughes.com",
    "summitracing.com",
    "gordonelectricsupply.com",
    "ferguson.com",
}

SITEMAP_CANDIDATES = (
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/product-sitemap.xml",
    "/sitemap_products.xml",
    "/sitemap-products.xml",
    "/sitemaps/sitemap.xml",
    "/product_sitemap.xml",
)

SHORT_MPN_TOKENS = {"40771", "121943", "121944", "08884", "430", "201", "60926", "51515", "2097", "5P71"}

PROGRESS_EVERY = 5
ITEM_DEADLINE_S = 45.0
MAX_CANDIDATES_PER_ITEM = 16
