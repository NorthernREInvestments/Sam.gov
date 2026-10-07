"""BidNet downstream census. Discovery thresholds and harvest code are not modified."""

from __future__ import annotations

BUILD = "20261006-m3-bidnet-downstream-processing-v1"
VALID_OPEN_TARGET = 21976
# Harvest list rows: 7,313 new + 7,751 existing + 6,905 same-solicitation repeats
# + 7 gap-closure recoveries = 21,976 list identities and 15,071 canonical records.
HARVEST_CANONICAL_DISTINCT = 15071

CORPUS = "m3_bidnet_downstream_v1_corpus.json"
CHECKPOINT = "m3_bidnet_downstream_v1_checkpoint.json"
REPORT_JSON = "m3_bidnet_downstream_v1_last_report.json"
REPORT_TXT = "m3_bidnet_downstream_v1_last_report.txt"
PROGRESS = "m3_bidnet_downstream_v1_progress.json"

CLASSES = (
    "PRODUCT",
    "MIXED_PRODUCT_MATERIAL",
    "SERVICE",
    "CONSTRUCTION",
    "UNKNOWN",
)
PRODUCT_CLASSES = {"PRODUCT", "MIXED_PRODUCT_MATERIAL"}

DETAIL_STATES = (
    "DETAIL_COMPLETE",
    "DETAIL_PARTIAL",
    "DETAIL_LOCKED",
    "DETAIL_EXTERNAL_SOURCE",
    "DETAIL_RETRYABLE",
    "DETAIL_TERMINAL",
)
PACKAGE_STATES = (
    "PACKAGE_ACQUIRED_BIDNET",
    "PACKAGE_ACQUIRED_OFFICIAL_SOURCE",
    "PACKAGE_ACCESSIBLE_PENDING_DOWNLOAD",
    "PACKAGE_LOCKED_MEMBERSHIP",
    "PACKAGE_EXTERNAL_PORTAL_REQUIRED",
    "PACKAGE_REGISTRATION_REQUIRED",
    "PACKAGE_NOT_POSTED",
    "PACKAGE_RETRYABLE",
    "PACKAGE_TERMINAL",
)
ELIGIBILITY_STATES = (
    "ELIGIBILITY_CLEAR",
    "ELIGIBILITY_CONDITIONAL",
    "ELIGIBILITY_UNKNOWN",
    "ELIGIBILITY_BLOCKED",
)
