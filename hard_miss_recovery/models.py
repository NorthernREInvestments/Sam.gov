"""Hard-miss recovery constants.

Build: 20261004-m3-hard-miss-recovery-v1
"""

from __future__ import annotations

BUILD = "20261004-m3-hard-miss-recovery-v1"

HARD_MISS_CORPUS = "m3_hard_miss_corpus_v1.json"
DENOM_AUDIT = "m3_hard_miss_denominator_audit_v1.json"
CK = "m3_hard_miss_recovery_v1_checkpoint.json"
JOB = "m3_hard_miss_recovery_v1_job.json"
REPORT = "m3_hard_miss_recovery_v1_last_report.json"
PAGE_CACHE = "m3_hard_miss_page_cache_v1.json"

PRIOR_SR_CK = "m3_seller_rediscovery_v1_checkpoint.json"
PRIOR_FROZEN = "m3_seller_rediscovery_frozen_baseline_v1.json"

# Denominator audit classes
CURRENT_PUBLIC_NEW_PRICE_VERIFIED = "CURRENT_PUBLIC_NEW_PRICE_VERIFIED"
CURRENT_PUBLIC_PRICE_VERIFIED_BUT_HARD_TO_EXTRACT = "CURRENT_PUBLIC_PRICE_VERIFIED_BUT_HARD_TO_EXTRACT"
QUOTE_ONLY_NOW = "QUOTE_ONLY_NOW"
NO_LONGER_PUBLICLY_PRICED = "NO_LONGER_PUBLICLY_PRICED"
PRODUCT_DISCONTINUED = "PRODUCT_DISCONTINUED"
KNOWN_PAGE_DEAD = "KNOWN_PAGE_DEAD"
BENCHMARK_SOURCE_STALE = "BENCHMARK_SOURCE_STALE"
AMBIGUOUS = "AMBIGUOUS"

# Only these may remove from audited denominator
LEAVE_DENOMINATOR = {
    QUOTE_ONLY_NOW,
    NO_LONGER_PUBLICLY_PRICED,
    PRODUCT_DISCONTINUED,
    KNOWN_PAGE_DEAD,
    BENCHMARK_SOURCE_STALE,
}

# Recovery strategies
STRAT_AUTH_DIST = "AUTHORIZED_DISTRIBUTOR_DEEP_SEARCH"
STRAT_MFR_WTB = "MANUFACTURER_WHERE_TO_BUY"
STRAT_EXACT_URL = "EXACT_PRODUCT_URL_RECOVERY"
STRAT_CATALOG_PDF = "PUBLIC_CATALOG_PDF"
STRAT_SITEMAP = "SITEMAP_PRODUCT_RECOVERY"
STRAT_SPECIALIST = "CATEGORY_SPECIALIST_SEARCH"
STRAT_SHORT_MPN = "SHORT_MPN_DISAMBIGUATION"
STRAT_PACK_UOM = "PACK_UOM_DISAMBIGUATION"
STRAT_ALT_COUNTRY = "ALTERNATE_COUNTRY_SELLER"
STRAT_FAMILY = "PRODUCT_FAMILY_NAVIGATION"
STRAT_OTHER = "OTHER"

PRICE_FOUND = "PRICE_FOUND"
FROZEN = "FROZEN_VALIDATED"
PRODUCT_PUBLIC_PRICE_EXHAUSTED = "PRODUCT_PUBLIC_PRICE_EXHAUSTED"
NO_PRICE_EXHAUSTIVE = "NO_PRICE_EXHAUSTIVE"

# Domains: identity reference only (do not extract-budget)
DEAD_PRIMARY = {
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

# Proven productive open sellers (prior runs)
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
    "1000bulbs.com",
    "quill.com",
    "globalindustrial.com",
    "rspsupply.com",
    "crcautocare.com",
    "brother-usa.com",
    "proteccontrols.com",
    "nationaldistributorllc.com",
    "autobuffy.com",
    "maxtran.com",
    "motion.com",
    "plexsupply.com",
    "lightingsupply.com",
    "honeywellstore.com",
    "lifesafetycom.com",
    "cooper-electric.com",
)

# Category specialists (open-first)
SPECIALISTS: dict[str, tuple[str, ...]] = {
    "plumbing": (
        "plumbingsupply.com",
        "faucet.com",
        "plumbingstock.com",
        "mccoys.com",
        "plexsupply.com",
        "cripedistributing.com",
        "dkhardware.com",
        "voomisupply.com",
    ),
    "electrical": (
        "platt.com",
        "rspsupply.com",
        "standardelectricsupply.com",
        "cooper-electric.com",
        "elliott-electric.com",
        "rexelusa.com",
    ),
    "tools": (
        "leestools.com",
        "pnwtoolsupply.com",
        "toolbarn.com",
        "acmetools.com",
        "platt.com",
        "channellock.com",
        "kleintools.com",
        "makitatools.com",
        "dewalt.com",
        "irwin.com",
    ),
    "ppe": (
        "envirosafetyproducts.com",
        "quill.com",
        "northernsafety.com",
        "seton.com",
        "honeywellstore.com",
    ),
    "hvac": (
        "parts-hvac.com",
        "filtersfast.com",
        "lifesafetycom.com",
        "honeywellhome.com",
        "aprilaire.com",
    ),
    "lighting": (
        "1000bulbs.com",
        "lightingsupply.com",
        "bulbs.com",
        "feit.com",
        "acuitybrands.com",
    ),
    "auto": (
        "dieselpartsdirect.com",
        "allprodiesel.net",
        "crossfilters.com",
        "carid.com",
        "partsource.ca",
        "bearingsrus.com",
    ),
    "office": (
        "quill.com",
        "staples.com",
        "officedepot.com",
        "hopkinssales.com",
    ),
    "furniture": (
        "globalindustrial.com",
        "quill.com",
        "staples.com",
        "officedepot.com",
    ),
    "mro": (
        "quill.com",
        "envirosafetyproducts.com",
        "mscdirect.com",
        "loctiteproducts.com",
    ),
}

SHORT_MPN_IDS = {"40771", "121943", "121944", "08884", "430", "201", "60926", "51515"}

KNOWN_FOCUS = [
    "easy-watts-lf777m2",
    "easy-leviton-5320",
    "easy-brady-121943",
    "easy-makita-b-45580",
    "easy-fleetguard-ff63009",
    "easy-filtete-mpr2200",
    "easy-sharkbite-uc248lfa",
    "easy-honeywell-th8320u1008",
    "easy-global-dwt-6",
    "quote-cummins-5579409px",
]

CONTROL_IDS = [
    "easy-leviton-5320",
    "easy-filtete-mpr2200",
    "easy-sharkbite-uc248lfa",
    "easy-honeywell-th8320u1008",
]

ORIGINAL_DENOM = 82
TARGET_PRICED = 66
COVERAGE_TARGET = 80.0
ACCURACY_TARGET = 95.0
MIN_ALT_SELLERS = 5
ITEM_DEADLINE_S = 55.0
PROGRESS_EVERY = 5
