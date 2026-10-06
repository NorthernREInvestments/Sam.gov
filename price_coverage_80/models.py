"""Price accuracy + 80% public NEW coverage.

Build: 20261004-m3-price-coverage-80-v1

Accuracy and coverage are DIFFERENT metrics.
Fail closed — wrong product is worse than no price.
"""

from __future__ import annotations

BUILD = "20261004-m3-price-coverage-80-v1"

# Benchmark truth classes
PUBLIC_NEW_PRICE_CONFIRMED = "PUBLIC_NEW_PRICE_CONFIRMED"
PUBLIC_PRICE_EXISTS_BUT_ROUTE_DIFFICULT = "PUBLIC_PRICE_EXISTS_BUT_ROUTE_DIFFICULT"
QUOTE_ONLY_CONFIRMED = "QUOTE_ONLY_CONFIRMED"
NO_PUBLIC_PRICE_CONFIRMED = "NO_PUBLIC_PRICE_CONFIRMED"
AMBIGUOUS = "AMBIGUOUS"

# Accuracy classes
CORRECT_EXACT_MATCH = "CORRECT_EXACT_MATCH"
CORRECT_COMPLIANT_EQUAL = "CORRECT_COMPLIANT_EQUAL"
WRONG_MODEL = "WRONG_MODEL"
WRONG_MPN = "WRONG_MPN"
WRONG_CONDITION = "WRONG_CONDITION"
WRONG_UOM = "WRONG_UOM"
WRONG_PACK = "WRONG_PACK"
WRONG_VARIANT = "WRONG_VARIANT"
WRONG_QUANTITY_BASIS = "WRONG_QUANTITY_BASIS"
WRONG_PRICE_EXTRACTION = "WRONG_PRICE_EXTRACTION"
STALE_OR_NONEXECUTABLE = "STALE_OR_NONEXECUTABLE"
ACCURACY_AMBIGUOUS = "AMBIGUOUS"
NO_PRICE_CLAIMED = "NO_PRICE_CLAIMED"

# Block causes
BLOCK_403 = "403"
BLOCK_429 = "429"
BLOCK_BOT_PAGE = "bot_page"
BLOCK_EMPTY_HTML = "empty_html"
BLOCK_JS_HIDDEN = "js_hidden"
BLOCK_LOGIN = "login_required"
BLOCK_REGION = "region_issue"
BLOCK_SEARCH_PROVIDER = "search_provider_blocked"
BLOCK_DOMAIN_UNAVAILABLE = "domain_unavailable"
BLOCK_PRODUCT_REMOVED = "product_removed"
BLOCK_UNKNOWN = "unknown"

ACCURACY_TARGET = 0.95
COVERAGE_TARGET = 0.80
EASY25_COVERAGE_TARGET = 0.90

# Non-credible seller patterns (fail closed)
NONCREDIBLE_SELLER_PATTERNS = (
    "advancedtruckparts.com",  # repeated $1 placeholders
)
MIN_CREDIBLE_PRICE = 1.51

# Trusted sellers accepted for fail-closed credibility
TRUSTED_SELLER_SUFFIXES = (
    "grainger.com",
    "zoro.com",
    "supplyhouse.com",
    "1000bulbs.com",
    "globalindustrial.com",
    "finditparts.com",
    "mscdirect.com",
    "fastenal.com",
    "staples.com",
    "fleetpride.com",
    "quill.com",
    "officedepot.com",
    "homedepot.com",
    "lowes.com",
    "acehardware.com",
    "bradyid.com",
    "dieselpartsdirect.com",
    "thedieselstore.com",
    "cummins.com",
)
