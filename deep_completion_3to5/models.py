"""Deep completion 3–5: quote conversion + real economics.

Build: 20261005-m3-deep-completion-3to5-v1
"""

from __future__ import annotations

BUILD = "20261005-m3-deep-completion-3to5-v1"

CK = "m3_deep_completion_3to5_v1_checkpoint.json"
JOB = "m3_deep_completion_3to5_v1_job.json"
REPORT = "m3_deep_completion_3to5_v1_last_report.json"
CORPUS = "m3_deep_completion_corpus_v1.json"
QUOTE_PACKETS = "m3_deep_completion_quote_packets_v1.json"
OUTREACH_QUEUE = "m3_deep_completion_outreach_queue_v1.json"
QUOTE_RECEIPT_SCHEMA = "m3_quote_receipt_schema_v1.json"
CONSERVATION = "m3_deep_completion_conservation_v1.json"

PRIOR_MLR_CK = "m3_material_line_identity_price_recovery_v1_checkpoint.json"
PRIOR_REV = "m3_revenue_evidence_v1_store.json"

# Exact corpus order (research priority)
DEEP_OIDS = [
    "opengov:go-metro:298984",
    "opengov:dekalbcountyga:286698",
    "opengov:bridgeportct:299806",
    "opengov:daniabeachfl:284328",
    "opengov:collier-county-fl:295143",
]

COLLIER_OID = "opengov:collier-county-fl:295143"
GO_METRO_OID = "opengov:go-metro:298984"
DEKALB_OID = "opengov:dekalbcountyga:286698"

READY_FOR_OWNER_QUOTE_OUTREACH = "READY_FOR_OWNER_QUOTE_OUTREACH"
REVENUE_NOT_READY = "REVENUE_NOT_READY"
EXECUTION_BLOCKED = "EXECUTION_BLOCKED"
EXECUTION_COMPLEX = "EXECUTION_COMPLEX"
LOWER_PRIORITY = "LOWER_PRIORITY"

LINE_TARGET_COST_CONFIRMED = "LINE_TARGET_COST_CONFIRMED"
LINE_TARGET_COST_ALLOCATED = "LINE_TARGET_COST_ALLOCATED"
NO_LINE_TARGET_AVAILABLE = "NO_LINE_TARGET_AVAILABLE"

PUBLIC_CURRENT = "PUBLIC_CURRENT"
MANUFACTURER_CURRENT = "MANUFACTURER_CURRENT"
AUTHORIZED_DISTRIBUTOR_CURRENT = "AUTHORIZED_DISTRIBUTOR_CURRENT"
SUPPLIER_QUOTE = "SUPPLIER_QUOTE"

FREIGHT_RESERVE_PCT = 0.10
FINANCING_RESERVE_PCT = 0.03
MIN_EXECUTABLE_PRICE = 1.51

# Last public check budget per opportunity
PUBLIC_CHECK_MAX_LIVE = 6
PUBLIC_CHECK_ITEM_S = 25.0
OPP_DEADLINE_S = 120.0
