"""BidNet incremental + parallel downstream engine. Discovery is frozen."""

from __future__ import annotations

BUILD = "20261007-m3-money-path-recovery-v1"
PREV_DOWNSTREAM_BUILD = "20261006-m3-bidnet-downstream-processing-v1"
VALID_OPEN = 21976
PRODUCT_MIXED_TOTAL = 13270
PREVIOUSLY_COMPLETE = 120

STATE = "m3_bidnet_engine_v1_state.json"
QUEUE = "m3_bidnet_engine_v1_queue.json"
TIMING = "m3_bidnet_engine_v1_timing.json"
REPORT_JSON = "m3_bidnet_engine_v1_last_report.json"
REPORT_TXT = "m3_bidnet_engine_v1_last_report.txt"
PROGRESS = "m3_bidnet_engine_v1_progress.json"
FINGERPRINTS = "m3_bidnet_engine_v1_fingerprints.json"
DOC_HASHES = "m3_bidnet_engine_v1_doc_hashes.json"
CACHE_METRICS = "m3_bidnet_engine_v1_cache_metrics.json"

# Reuse the downstream checkpoint that already holds the completed 120.
DOWNSTREAM_CHECKPOINT = "m3_bidnet_downstream_v1_checkpoint.json"

STAGES = (
    "DETAIL",
    "PACKAGE",
    "ELIGIBILITY",
    "LINES",
    "IDENTITY",
    "REVENUE",
    "ACQUISITION",
    "QUOTE",
    "BASKET",
    "ECONOMICS",
)

CHANGE_TYPES = (
    "NO_CHANGE",
    "NEW_OPPORTUNITY",
    "STATUS_CHANGED",
    "DEADLINE_ONLY",
    "METADATA_CHANGED",
    "DETAIL_CHANGED",
    "DOCUMENT_LIST_CHANGED",
    "NEW_AMENDMENT",
    "PACKAGE_CHANGED",
    "LINE_RELEVANT_CHANGE",
    "CLOSED",
    "REOPENED",
)

PRIORITY_CLASSES = ("P0_IMMEDIATE", "P1_HIGH", "P2_NORMAL", "P3_LOW")

RETRY_CLASSES = (
    "RETRYABLE_NETWORK",
    "RETRYABLE_SESSION",
    "RETRYABLE_TIMEOUT",
    "RETRYABLE_SOURCE",
    "TERMINAL",
    "OWNER_ACTION",
    "REGISTRATION_REQUIRED",
)

BACKLOG_STATES = ("QUEUED", "RUNNING", "COMPLETE", "RETRYABLE", "OWNER_ACTION", "TERMINAL")

DEFAULT_WORKERS = 3
WORKER_CANDIDATES = (1, 3, 5, 10)
THROUGHPUT_MIN_TARGET = 10.0
THROUGHPUT_PREFERRED = 25.0
