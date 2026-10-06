"""Revenue evidence tiers and statuses.

Build: 20261004-m3-revenue-evidence-v1

Historical gov data = revenue/reference/channel ONLY — never acquisition cost.
"""

from __future__ import annotations

BUILD = "20261004-m3-revenue-evidence-v1"

# Tiers
TIER_R1 = "R1"
TIER_R2 = "R2"
TIER_R3 = "R3"
TIER_R4 = "R4"
TIER_R5 = "R5"

CURRENT_VALUE_EXPLICIT = "CURRENT_VALUE_EXPLICIT"
EXACT_PRIOR_LINE_VALUE = "EXACT_PRIOR_LINE_VALUE"
COMPARABLE_PRIOR_BASKET_VALUE = "COMPARABLE_PRIOR_BASKET_VALUE"
BUYER_CATEGORY_REFERENCE = "BUYER_CATEGORY_REFERENCE"
CHANNEL_ONLY_REFERENCE = "CHANNEL_ONLY_REFERENCE"

STRONG_TIERS = {TIER_R1, TIER_R2, TIER_R3}
WEAK_TIERS = {TIER_R4, TIER_R5}

# Comparability
EXACT_MATCH = "EXACT_MATCH"
STRONG_COMPARABLE = "STRONG_COMPARABLE"
WEAK_COMPARABLE = "WEAK_COMPARABLE"
NOT_COMPARABLE = "NOT_COMPARABLE"

# Terminals
NO_HISTORY_AFTER_EXHAUSTIVE_SEARCH = "NO_HISTORY_AFTER_EXHAUSTIVE_SEARCH"
REVENUE_RESEARCH_RETRYABLE = "REVENUE_RESEARCH_RETRYABLE"
REVENUE_RESEARCH_BUDGET_DEFERRED = "REVENUE_RESEARCH_BUDGET_DEFERRED"
REVENUE_CONFIRMATION_REQUIRED = "REVENUE_CONFIRMATION_REQUIRED"

# Channel (reuse names)
OPEN_RESELLER_CHANNEL_CONFIRMED = "OPEN_RESELLER_CHANNEL_CONFIRMED"
INCUMBENT_CHANNEL_ADVANTAGE = "INCUMBENT_CHANNEL_ADVANTAGE"
OEM_DIRECT_POSSIBLE = "OEM_DIRECT_POSSIBLE"
AUTHORIZED_DISTRIBUTOR_PATH = "AUTHORIZED_DISTRIBUTOR_PATH"

# Required routes before NO_HISTORY
REQUIRED_REVENUE_ROUTES = (
    "current_package_value_mined",
    "exact_solicitation_search",
    "buyer_archive_search",
    "award_bid_tab_search",
    "recurring_title_search",
    "opengov_history_search",
    "buyer_product_search",
    "buyer_description_search",
)

OPTIONAL_REVENUE_ROUTES = (
    "board_council_search",
    "po_check_register_search",
    "federal_award_search",
    "canonical_duplicate_search",
)
