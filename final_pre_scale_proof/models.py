"""Final pre-scale proof — revenue hardening + owner-ready quote execution.

Build: 20261005-m3-final-pre-scale-proof-v1
"""

from __future__ import annotations

BUILD = "20261005-m3-final-pre-scale-proof-v1"

CK = "m3_final_pre_scale_proof_v1_checkpoint.json"
JOB = "m3_final_pre_scale_proof_v1_job.json"
REPORT = "m3_final_pre_scale_proof_v1_last_report.json"
GAP_REGISTER = "PRE_SCALE_GAP_REGISTER_V1.json"
QUOTE_PACKETS_AUDITED = "m3_final_pre_scale_quote_packets_audited_v1.json"
OUTREACH_QUEUE = "m3_final_pre_scale_outreach_queue_v1.json"
QUOTE_REQUESTS = "m3_final_pre_scale_quote_request_texts_v1.json"
INGESTION_FIXTURES = "m3_quote_ingestion_fixtures_v1.json"
INGESTION_GATE = "m3_quote_ingestion_production_gate_v1.json"
LARGE_TEST_DESIGN = "m3_large_test_design_v1.json"

PRIOR_DC_CK = "m3_deep_completion_3to5_v1_checkpoint.json"
PRIOR_DC_PACKETS = "m3_deep_completion_quote_packets_v1.json"
PRIOR_DC_REPORT = "m3_deep_completion_3to5_v1_last_report.json"
PRIOR_MLR_CK = "m3_material_line_identity_price_recovery_v1_checkpoint.json"
PRIOR_REV = "m3_revenue_evidence_v1_store.json"

DANIA_OID = "opengov:daniabeachfl:284328"
BRIDGEPORT_OID = "opengov:bridgeportct:299806"
GO_METRO_OID = "opengov:go-metro:298984"
DEKALB_OID = "opengov:dekalbcountyga:286698"
COLLIER_OID = "opengov:collier-county-fl:295143"

# Semantic roles (extended)
GRANT_TOTAL = "GRANT_TOTAL"
PROGRAM_FUNDING = "PROGRAM_FUNDING"
PROJECT_BUDGET = "PROJECT_BUDGET"
NOT_TO_EXCEED = "NOT_TO_EXCEED"
MULTI_YEAR_CEILING = "MULTI_YEAR_CEILING"
CATEGORY_FUNDING = "CATEGORY_FUNDING"
TOTAL_CONTRACT_VALUE = "TOTAL_CONTRACT_VALUE"
BUYER_CATEGORY_REFERENCE = "BUYER_CATEGORY_REFERENCE"
REVENUE_NOT_READY = "REVENUE_NOT_READY"
NOT_READY = "NOT_READY"

PRODUCT_SCOPE = "PRODUCT"
INSTALL_SCOPE = "INSTALL"
SERVICE_SCOPE = "SERVICE"
MIXED_SCOPE = "MIXED"
UNALLOCATED = "UNALLOCATED"

PACKET_PASS = "PACKET_PASS"
PACKET_FAIL = "PACKET_FAIL"
SUPPLIER_CONTACT_READY = "SUPPLIER_CONTACT_READY"

TEST_FIXTURE_ONLY = "TEST_FIXTURE_ONLY"
REAL_SUPPLIER_QUOTE = "REAL_SUPPLIER_QUOTE"
PRICE_ORIGIN_SUPPLIER_QUOTE = "SUPPLIER_QUOTE"

FREIGHT_RESERVE_PCT = 0.10
FINANCING_RESERVE_PCT = 0.03
INSTALL_RESERVE_PCT = 0.15  # when install included in program funding

FUNNEL_STAGES = [
    "DISCOVERY",
    "CANONICALIZATION",
    "PRODUCT QUALIFICATION",
    "PACKAGE",
    "ELIGIBILITY",
    "LINE EXTRACTION",
    "IDENTITY",
    "REVENUE",
    "ACQUISITION",
    "QUOTE RESERVE",
    "BASKET",
    "FREIGHT",
    "FINANCING",
    "ECONOMICS",
    "EXECUTION",
    "LENDER READY",
    "BID READY",
    "SUBMISSION",
    "AWARD",
    "FULFILLMENT",
    "INVOICE",
    "PAYMENT",
    "LEARNING",
]

PROVEN = "PROVEN"
PARTIAL = "PARTIAL"
NOT_BUILT = "NOT_BUILT"
BUILT_NOT_PROVEN = "BUILT_NOT_PROVEN"
BLOCKED = "BLOCKED"
