"""Live exact priced PDP acquisition.

Build: 20261005-m3-live-exact-priced-pdp-v1
"""

from __future__ import annotations

BUILD = "20261005-m3-live-exact-priced-pdp-v1"

CK = "m3_live_exact_priced_pdp_v1_checkpoint.json"
JOB = "m3_live_exact_priced_pdp_v1_job.json"
REPORT = "m3_live_exact_priced_pdp_v1_last_report.json"
BASELINE = "m3_full100_live_pdp_baseline_v1.json"
URL_EVIDENCE = "m3_live_pdp_url_evidence_v1.json"
SELLER_SCORE = "m3_live_priceability_score_v1.json"
DEMOTIONS = "m3_live_seller_demotions_v1.json"
CONTRADICTIONS = "m3_benchmark_public_price_contradictions_v1.json"

PRIOR_OSE_CK = "m3_open_seller_expansion_v1_checkpoint.json"
PRIOR_KPE_CK = "m3_known_pdp_price_extraction_v2_checkpoint.json"
PRIOR_KPE_CORPUS = "m3_known_pdp_price_corpus_v1.json"
PRIOR_HM_CK = "m3_hard_miss_recovery_v1_checkpoint.json"
PRIOR_P14_CK = "m3_price_14_expand_patterns_v1_checkpoint.json"

ORIGINAL_DENOM = 82
HONEST_BASELINE = 47
TARGET_PRICED = 66
TARGET_NEW = 19
TARGET_LIVE_PDPS = 25
TARGET_COVERAGE = 0.80
TARGET_ACCURACY = 0.95
EASY25_COVERAGE_FLOOR = 0.80

PROGRESS_EVERY = 5
ITEM_DEADLINE_S = 110.0
EXTRACT_RESERVE_S = 30.0
MAX_DISCOVERY_QUERIES = 5
MAX_CANDIDATES_VALIDATE = 8
STOP_DISCOVERY_AT_EXTRACTABLE = 2

# PDP state model
LIVE_EXACT_PRICED_PDP = "LIVE_EXACT_PRICED_PDP"
LIVE_EXACT_PDP_PRICE_HIDDEN = "LIVE_EXACT_PDP_PRICE_HIDDEN"
LIVE_EXACT_PDP_NO_PUBLIC_PRICE = "LIVE_EXACT_PDP_NO_PUBLIC_PRICE"
LIVE_EXACT_PDP_QUOTE_ONLY = "LIVE_EXACT_PDP_QUOTE_ONLY"
LIVE_EXACT_PDP_LOGIN_REQUIRED = "LIVE_EXACT_PDP_LOGIN_REQUIRED"
LIVE_EXACT_PDP_ACCESS_BLOCKED = "LIVE_EXACT_PDP_ACCESS_BLOCKED"
LIVE_EXACT_PDP_WRONG_PACK = "LIVE_EXACT_PDP_WRONG_PACK"
LIVE_EXACT_PDP_WRONG_VARIANT = "LIVE_EXACT_PDP_WRONG_VARIANT"
DEAD_404 = "DEAD_404"
SEARCH_SHELL = "SEARCH_SHELL"
CATEGORY_PAGE = "CATEGORY_PAGE"
FAMILY_PAGE = "FAMILY_PAGE"
MARKETING_PAGE = "MARKETING_PAGE"
WRONG_PRODUCT = "WRONG_PRODUCT"
WRONG_MANUFACTURER = "WRONG_MANUFACTURER"
WRONG_MPN = "WRONG_MPN"
SOFT_URL_UNVERIFIED = "SOFT_URL_UNVERIFIED"

EXTRACTABLE_STATES = {LIVE_EXACT_PRICED_PDP, LIVE_EXACT_PDP_PRICE_HIDDEN}

# Offer presence
PUBLIC_PRICE_VISIBLE = "PUBLIC_PRICE_VISIBLE"
PUBLIC_PRICE_STRUCTURED = "PUBLIC_PRICE_STRUCTURED"
PUBLIC_PRICE_JS_HIDDEN = "PUBLIC_PRICE_JS_HIDDEN"
QUOTE_ONLY = "QUOTE_ONLY"
LOGIN_REQUIRED = "LOGIN_REQUIRED"
NO_PUBLIC_OFFER = "NO_PUBLIC_OFFER"

# Human visibility
HUMAN_PUBLIC_PRICE_VISIBLE = "HUMAN_PUBLIC_PRICE_VISIBLE"
HUMAN_PUBLIC_PRICE_HIDDEN_BUT_OFFER_EXISTS = "HUMAN_PUBLIC_PRICE_HIDDEN_BUT_OFFER_EXISTS"
HUMAN_NO_PUBLIC_PRICE = "HUMAN_NO_PUBLIC_PRICE"
HUMAN_ACCESS_BLOCKED = "HUMAN_ACCESS_BLOCKED"
HUMAN_UNKNOWN = "UNKNOWN"

# Demotion classes
IDENTITY_ONLY = "IDENTITY_ONLY"
QUOTE_ONLY_DOMAIN = "QUOTE_ONLY"
NON_PRICEABLE = "NON_PRICEABLE"
ACCESS_BLOCKED_DOMAIN = "ACCESS_BLOCKED"

HARD_FOCUS = [
    "easy-watts-lf777m2",
    "easy-brady-121943",
    "easy-makita-b-45580",
    "easy-wd40-490040",
    "plumb-oatey-30241",
    "plumb-oatey-31016",
    "tool-dewalt-dwht56027",
    "easy-philips-led",
    "light-ge-93129788",
    "light-lithonia-2gtl4",
    "mro-crc-05005",
    "mro-permatex-80078",
    "mro-gorilla-50036",
    "mro-jb-weld-8265",
    "ppe-ansi-z89",
    "ppe-ansell-37-175",
    "furn-hon-h5701",
    "furn-safco-1201bl",
]

# Sellers known to expose public prices historically
PREFERRED_LIVE_SELLERS = [
    "quill.com",
    "platt.com",
    "lightbulbs.com",
    "rshughes.com",
    "homelectrical.com",
    "activeplumbing.com",
    "pksafety.com",
    "shop.aprilaire.com",
    "gsistore.com",
]
