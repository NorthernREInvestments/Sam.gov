"""Live seller expansion + independent benchmark reverification.

Build: 20261005-m3-live-seller-benchmark-reverify-v1
"""

from __future__ import annotations

BUILD = "20261005-m3-live-seller-benchmark-reverify-v1"

CK = "m3_live_seller_benchmark_reverify_v1_checkpoint.json"
JOB = "m3_live_seller_benchmark_reverify_v1_job.json"
REPORT = "m3_live_seller_benchmark_reverify_v1_last_report.json"
BASELINE = "m3_full100_reverify_baseline_v1.json"
CONTRADICTION_CORPUS = "m3_benchmark_contradiction_corpus_v1.json"
OPEN_SELLER_CORPUS = "m3_open_priceable_seller_corpus_v1.json"
DOMAIN_CLASS = "m3_reverify_domain_class_v1.json"
REMOVALS = "m3_benchmark_denominator_removals_v1.json"
EASY25_REPORT = "m3_easy25_reverify_v1.json"

PRIOR_CONTRADICTIONS = "m3_benchmark_public_price_contradictions_v1.json"
PRIOR_LEP_CK = "m3_live_exact_priced_pdp_v1_checkpoint.json"
PRIOR_LEP_REPORT = "m3_live_exact_priced_pdp_v1_last_report.json"
PRIOR_OSE_CK = "m3_open_seller_expansion_v1_checkpoint.json"
PRIOR_P14_CK = "m3_price_14_expand_patterns_v1_checkpoint.json"
PRIOR_HM_CK = "m3_hard_miss_recovery_v1_checkpoint.json"
PRIOR_KPE_CORPUS = "m3_known_pdp_price_corpus_v1.json"
PRIOR_DEMOTIONS = "m3_live_seller_demotions_v1.json"

ORIGINAL_DENOM = 82
HONEST_BASELINE = 50
EASY25_BASELINE_PRICED = 19
EASY25_DENOM = 25
TARGET_REVERIFIED_COVERAGE = 0.80
TARGET_ACCURACY = 0.95
TARGET_EASY25_COVERAGE = 0.80

PROGRESS_EVERY = 5
ITEM_DEADLINE_S = 100.0
DISCOVERY_BUDGET_S = 55.0
EXTRACT_RESERVE_S = 35.0
MAX_SELLER_DOMAINS = 12
MAX_CANDIDATES_VALIDATE = 10
MAX_DISCOVERY_QUERIES = 6
STOP_AT_EXTRACTABLE = 2
HUMAN_CHECK_MIN = 20

# Contradiction outcomes (Phase 4)
PUBLIC_NEW_PRICE_REVERIFIED = "PUBLIC_NEW_PRICE_REVERIFIED"
PUBLIC_PRICE_EXISTS_BUT_ACCESS_DIFFICULT = "PUBLIC_PRICE_EXISTS_BUT_ACCESS_DIFFICULT"
QUOTE_ONLY_CONFIRMED = "QUOTE_ONLY_CONFIRMED"
NO_CURRENT_PUBLIC_PRICE_CONFIRMED = "NO_CURRENT_PUBLIC_PRICE_CONFIRMED"
PRODUCT_DISCONTINUED = "PRODUCT_DISCONTINUED"
BENCHMARK_SOURCE_STALE = "BENCHMARK_SOURCE_STALE"
BENCHMARK_IDENTITY_WRONG = "BENCHMARK_IDENTITY_WRONG"
AMBIGUOUS_REQUIRES_REVIEW = "AMBIGUOUS_REQUIRES_REVIEW"

RETAIN_IN_PUBLIC_DENOM = {
    PUBLIC_NEW_PRICE_REVERIFIED,
    PUBLIC_PRICE_EXISTS_BUT_ACCESS_DIFFICULT,
}

REMOVABLE_FROM_DENOM = {
    QUOTE_ONLY_CONFIRMED,
    NO_CURRENT_PUBLIC_PRICE_CONFIRMED,
    PRODUCT_DISCONTINUED,
    BENCHMARK_SOURCE_STALE,
    BENCHMARK_IDENTITY_WRONG,
}

# Domain classes (Phase 8)
PRICEABLE = "PRICEABLE"
IDENTITY_ONLY = "IDENTITY_ONLY"
QUOTE_ONLY_DOMAIN = "QUOTE_ONLY"
ACCESS_BLOCKED = "ACCESS_BLOCKED"
LOW_YIELD = "LOW_YIELD"

# Hard OEMs for authorized distributor expansion
HARD_OEMS = [
    "Brady",
    "Watts",
    "Oatey",
    "Makita",
    "DEWALT",
    "CRC",
    "Permatex",
    "WD-40",
    "Philips",
    "GE",
    "Lithonia",
    "Ansell",
    "MSA",
    "HON",
    "Safco",
    "3M",
    "Honeywell",
    "Rubbermaid",
    "Crescent",
    "IRWIN",
    "J-B Weld",
    "Gorilla",
]

# Prefer historically priceable open ecommerce
PREFERRED_PRICEABLE = [
    "quill.com",
    "platt.com",
    "lightbulbs.com",
    "1000bulbs.com",
    "rshughes.com",
    "homelectrical.com",
    "activeplumbing.com",
    "pksafety.com",
    "zoro.com",
    "toolbarn.com",
    "tooldiscounter.com",
    "northerntool.com",
    "gsistore.com",
    "shop.aprilaire.com",
    "bulbs.com",
    "crcautocare.com",
    "faucet.com",
    "freshwatersystems.com",
    "safetycompany.com",
    "officestore.com",
]

CATEGORY_EXPANDED_POOLS: dict[str, list[str]] = {
    "tools": [
        "quill.com",
        "platt.com",
        "toolbarn.com",
        "tooldiscounter.com",
        "northerntool.com",
        "acmetools.com",
        "zoro.com",
        "rshughes.com",
    ],
    "plumbing": [
        "activeplumbing.com",
        "faucet.com",
        "freshwatersystems.com",
        "plumbingsupply.com",
        "parts-hvac.com",
        "quill.com",
    ],
    "mro": [
        "quill.com",
        "rshughes.com",
        "zoro.com",
        "crcautocare.com",
        "homelectrical.com",
        "pksafety.com",
    ],
    "lighting": [
        "lightbulbs.com",
        "1000bulbs.com",
        "bulbs.com",
        "homelectrical.com",
        "quill.com",
    ],
    "ppe": [
        "quill.com",
        "pksafety.com",
        "northernsafety.com",
        "safetycompany.com",
        "seton.com",
    ],
    "hvac": ["gsistore.com", "parts-hvac.com", "quill.com", "shop.aprilaire.com"],
    "office": ["quill.com", "officestore.com"],
    "furniture": ["quill.com", "officestore.com"],
    "auto": ["crcautocare.com", "zoro.com", "quill.com"],
    "default": ["quill.com", "zoro.com", "rshughes.com", "platt.com"],
}
