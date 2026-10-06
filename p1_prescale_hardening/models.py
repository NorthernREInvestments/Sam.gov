"""P1 pre-scale hardening — package provenance + BID_READY 17/17 + quote observability.

Build: 20261005-m3-p1-prescale-hardening-v1
"""

from __future__ import annotations

BUILD = "20261005-m3-p1-prescale-hardening-v1"

CK = "m3_p1_prescale_hardening_v1_checkpoint.json"
REPORT = "m3_p1_prescale_hardening_v1_last_report.json"
PACKAGE_INDEX = "m3_package_provenance_index_v1.json"
PACKAGE_AUDIT = "m3_package_provenance_audit_v1.json"
BID_READY_SPEC = "BID_READY_REQUIREMENTS_V1.json"
BID_READY_STORE = "m3_bid_ready_state_v1.json"
QUOTE_LEDGER = "QUOTE_EVENT_LEDGER.json"
QUOTE_SNAPSHOTS = "m3_quote_before_after_snapshots_v1.json"
P1_REGISTER = "m3_p1_pre_large_test_register_v1.json"
REGRESSION = "m3_p1_prescale_hardening_regression_v1.json"
CONSERVATION = "m3_p1_prescale_conservation_v1.json"
LARGE_TEST_GATE = "m3_large_test_entry_gate_v1.json"
LARGE_TEST_DESIGN = "m3_large_test_design_v1.json"
INSTRUMENTATION = "m3_large_test_instrumentation_v1.json"

PRIOR_MLR_CK = "m3_material_line_identity_price_recovery_v1_checkpoint.json"
GOLDEN = "m3_golden_path_corpus_v1.json"

PACKAGE_PROVENANCE_COMPLETE = "PACKAGE_PROVENANCE_COMPLETE"
PACKAGE_PROVENANCE_PARTIAL = "PACKAGE_PROVENANCE_PARTIAL"
PACKAGE_PROVENANCE_MISSING = "PACKAGE_PROVENANCE_MISSING"
SOURCE_URL_UNAVAILABLE = "SOURCE_URL_UNAVAILABLE"

PASS = "PASS"
FAIL = "FAIL"
ACTION_REQUIRED = "ACTION_REQUIRED"
NOT_APPLICABLE = "NOT_APPLICABLE"
UNKNOWN = "UNKNOWN"

BID_READY_REQUIREMENTS = [
    "ALL_SOLICITATION_DOCS_PROCESSED",
    "ALL_AMENDMENTS_PROCESSED_ACKNOWLEDGED",
    "SUBMISSION_METHOD_PORTAL_CONFIRMED",
    "DEADLINE_TIMEZONE_CONFIRMED",
    "REQUIRED_FORMS_IDENTIFIED",
    "REQUIRED_SIGNATURES_IDENTIFIED",
    "PRICING_SCHEDULE_COMPLETE",
    "CERTIFICATIONS_REPS_CLEARED",
    "ELIGIBILITY_CLEARED",
    "DELIVERY_REQUIREMENTS_CLEARED",
    "INSURANCE_BONDING_CLEARED",
    "OEM_AUTHORIZATION_CLEARED",
    "COUNTRY_OF_ORIGIN_CLEARED",
    "CYBERSECURITY_REQUIREMENTS_CLEARED",
    "WARRANTY_INSPECTION_CLEARED",
    "PAST_PERFORMANCE_SAMPLES_CATALOGS_CLEARED",
    "ECONOMICS_FINANCING_EXECUTION_CLEARED",
]

HASH_ALG = "SHA-256"
