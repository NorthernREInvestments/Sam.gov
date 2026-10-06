"""Known PDP price extraction + seller-specific adapters.

Build: 20261005-m3-known-pdp-price-extraction-v2
"""

from __future__ import annotations

BUILD = "20261005-m3-known-pdp-price-extraction-v2"

CK = "m3_known_pdp_price_extraction_v2_checkpoint.json"
JOB = "m3_known_pdp_price_extraction_v2_job.json"
REPORT = "m3_known_pdp_price_extraction_v2_last_report.json"
CORPUS = "m3_known_pdp_price_corpus_v1.json"
DOMAIN_MEMORY = "m3_known_pdp_domain_extraction_memory_v2.json"
ENDPOINT_DB = "m3_known_pdp_price_endpoints_v2.json"

PRIOR_OSE_CK = "m3_open_seller_expansion_v1_checkpoint.json"
PRIOR_OSE_REPORT = "m3_open_seller_expansion_v1_last_report.json"
PRIOR_HM_CK = "m3_hard_miss_recovery_v1_checkpoint.json"
PRIOR_P14_CK = "m3_price_14_expand_patterns_v1_checkpoint.json"

ORIGINAL_DENOM = 82
HONEST_BASELINE = 47
TARGET_PRICED = 66
TARGET_NEW = 19
TARGET_COVERAGE = 0.80
TARGET_ACCURACY = 0.95
EASY25_COVERAGE_FLOOR = 0.80
EASY25_ACCURACY_FLOOR = 0.95

PROGRESS_EVERY = 5
ITEM_DEADLINE_S = 75.0

# Domain-first priority (highest unresolved PDP density / adapter reuse)
PRIORITY_DOMAINS = [
    "northernsafety.com",
    "acmetools.com",
    "plumbingsupply.com",
    "pksafety.com",
    "seton.com",
    "rshughes.com",
    "crcautocare.com",
    "toolbarn.com",
    "1000bulbs.com",
    "lightbulbs.com",
    "zoro.com",
    "gorillatough.com",
    "jbweld.com",
    "autozone.com",
    "activeplumbing.com",
    "homelectrical.com",
    "platt.com",
]

HARD_FOCUS_IDS = [
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
