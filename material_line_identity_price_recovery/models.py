"""Material line identity recovery + production price evidence.

Build: 20261005-m3-material-line-identity-price-recovery-v1
"""

from __future__ import annotations

BUILD = "20261005-m3-material-line-identity-price-recovery-v1"

CK = "m3_material_line_identity_price_recovery_v1_checkpoint.json"
JOB = "m3_material_line_identity_price_recovery_v1_job.json"
REPORT = "m3_material_line_identity_price_recovery_v1_last_report.json"
CORPUS = "m3_material_line_recovery_corpus_v1.json"
CLUSTERS = "m3_material_identity_clusters_v1.json"
REVENUE_REGRESSION = "m3_revenue_context_regression_v1.json"
DEEP_CANDIDATES = "m3_deep_completion_candidates_v1.json"
CONSERVATION = "m3_material_line_recovery_conservation_v1.json"

PRIOR_STRICT_CK = "m3_line_basket_completion_strict_economics_v1_checkpoint.json"
PRIOR_CORPUS_V2 = "m3_basket_completion_corpus_v2.json"
PRIOR_REV = "m3_revenue_evidence_v1_store.json"

PACKAGE_ROOT = "opengov_public_docs/documents"
COLLIER_OID = "opengov:collier-county-fl:295143"

# Identity confidence A–G (Phase 4)
A_EXACT_MPN = "A_EXACT_MPN"
B_EXACT_MODEL = "B_EXACT_MODEL"
C_NSN_TO_EXACT = "C_NSN_TO_EXACT_COMMERCIAL"
D_PERMITTED_EQUAL = "D_PERMITTED_EQUAL_WITH_SALIENT_SPECS"
E_STRONG_GENERIC = "E_STRONG_GENERIC_SPEC"
F_FAMILY_ONLY = "F_COMMERCIAL_FAMILY_ONLY"
G_AMBIGUOUS = "G_IDENTITY_AMBIGUOUS"

USABLE_IDENTITY = {
    A_EXACT_MPN,
    B_EXACT_MODEL,
    C_NSN_TO_EXACT,
    D_PERMITTED_EQUAL,
    E_STRONG_GENERIC,
}

# Price origins
PUBLIC_CURRENT = "PUBLIC_CURRENT"
MANUFACTURER_CURRENT = "MANUFACTURER_CURRENT"
AUTHORIZED_DISTRIBUTOR_CURRENT = "AUTHORIZED_DISTRIBUTOR_CURRENT"
SUPPLIER_QUOTE = "SUPPLIER_QUOTE"
ALLOWED_PRICE_ORIGINS = {
    PUBLIC_CURRENT,
    MANUFACTURER_CURRENT,
    AUTHORIZED_DISTRIBUTOR_CURRENT,
    SUPPLIER_QUOTE,
}

# Quote subtypes
OEM_QUOTE_REQUIRED = "OEM_QUOTE_REQUIRED"
DISTRIBUTOR_QUOTE_REQUIRED = "DISTRIBUTOR_QUOTE_REQUIRED"
SUPPLIER_QUOTE_REQUIRED = "SUPPLIER_QUOTE_REQUIRED"

# Revenue semantic roles
CURRENT_CONTRACT_VALUE = "CURRENT_CONTRACT_VALUE"
BUDGET = "BUDGET"
CEILING = "CEILING"
ESTIMATE = "ESTIMATE"
HISTORICAL_REFERENCE = "HISTORICAL_REFERENCE"
GRANT_TOTAL = "GRANT_TOTAL"
PROGRAM_FUNDING = "PROGRAM_FUNDING"
BOND_THRESHOLD = "BOND_THRESHOLD"
INSURANCE_THRESHOLD = "INSURANCE_THRESHOLD"
OTHER_NON_REVENUE_AMOUNT = "OTHER_NON_REVENUE_AMOUNT"

# Allowed as *program/context* revenue classes — NOT automatically product-scope
REVENUE_ALLOWED = {
    CURRENT_CONTRACT_VALUE,
    BUDGET,
    CEILING,
    ESTIMATE,
    HISTORICAL_REFERENCE,
}

# Grant/program funding is contextual only — not usable as product contract revenue
REVENUE_CONTEXT_ONLY = {
    GRANT_TOTAL,
    PROGRAM_FUNDING,
}

MIN_EXECUTABLE_PRICE = 1.51
PROGRESS_EVERY = 1

# Research budgets (materiality-first)
P0_MAX_LIVE = 14
P1_MAX_LIVE = 12
ITEM_DEADLINE_S = 35.0
OPP_DEADLINE_S = 240.0
