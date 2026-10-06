"""Owner channel tests — real supplier-facing loop (no auto-send).

Build: 20261005-m3-owner-channel-tests-v1
"""

from __future__ import annotations

BUILD = "20261005-m3-owner-channel-tests-v1"

CK = "m3_owner_channel_tests_v1_checkpoint.json"
REPORT = "m3_owner_channel_tests_v1_last_report.json"
CORPUS = "OWNER_CHANNEL_TEST_CORPUS_V1.json"
SENT_STORE = "m3_owner_channel_sent_state_v1.json"
RESPONSE_STORE = "m3_owner_channel_responses_v1.json"
QUOTE_STORE = "m3_owner_channel_real_quotes_v1.json"
METRICS = "m3_owner_channel_metrics_v1.json"
SUPPLIER_MEMORY = "m3_supplier_intelligence_memory_v1.json"
OUTREACH = "m3_owner_channel_outreach_texts_v1.json"
EXPORTS_DIR = "owner_channel_exports"
P1_AUDIT = "m3_owner_channel_p1_audit_v1.json"
BID_READY_SPEC = "m3_bid_ready_requirements_v1.json"
PACKAGE_PROVENANCE_AUDIT = "m3_package_provenance_audit_v1.json"
LARGE_TEST_GATE = "m3_large_test_entry_gate_v1.json"

PRIOR_PACKETS = "m3_owner_channel_test_packets_v1.json"

GO_METRO_OID = "opengov:go-metro:298984"
DEKALB_OID = "opengov:dekalbcountyga:286698"

# Contact route classes
EMAIL_READY = "EMAIL_READY"
WEB_FORM_READY = "WEB_FORM_READY"
PHONE_READY = "PHONE_READY"
ACCOUNT_REQUIRED = "ACCOUNT_REQUIRED"
SALES_REP_REQUIRED = "SALES_REP_REQUIRED"
UNKNOWN = "UNKNOWN"

# Packet / sent states
READY_TO_SEND = "READY_TO_SEND"
SENT = "SENT"
WAITING = "WAITING"
PARTIAL_RESPONSE = "PARTIAL_RESPONSE"
QUOTE_RECEIVED = "QUOTE_RECEIVED"
CHANNEL_CONFIRMED = "CHANNEL_CONFIRMED"
CHANNEL_FAILED = "CHANNEL_FAILED"
REFERRED = "REFERRED"
ACCOUNT_SETUP_REQUIRED = "ACCOUNT_SETUP_REQUIRED"
QUOTE_REQUEST_SENT = "QUOTE_REQUEST_SENT"
WAITING_FOR_SUPPLIER_QUOTE = "WAITING_FOR_SUPPLIER_QUOTE"

# Response outcomes
OUTCOMES = [
    "QUOTE_RECEIVED",
    "PARTIAL_QUOTE_RECEIVED",
    "DECLINED_TO_QUOTE",
    "NO_RESPONSE",
    "ACCOUNT_REQUIRED",
    "MANUFACTURER_AUTH_REQUIRED",
    "BACKORDER",
    "DISCONTINUED",
    "MINIMUM_ORDER",
    "WRONG_CONTACT",
    "REFERRED_TO_DISTRIBUTOR",
    "OTHER",
]

# Match classes
EXACT_MATCH = "EXACT_MATCH"
PACK_CONVERSION_VALID = "PACK_CONVERSION_VALID"
ALTERNATE_PRODUCT_OFFERED = "ALTERNATE_PRODUCT_OFFERED"
UNMATCHED = "UNMATCHED"
AMBIGUOUS = "AMBIGUOUS"

REAL_SUPPLIER_QUOTE = "REAL_SUPPLIER_QUOTE"
TEST_FIXTURE_ONLY = "TEST_FIXTURE_ONLY"
SUPPLIER_CHANNEL_PROOF_ONLY = "SUPPLIER_CHANNEL_PROOF_ONLY"
