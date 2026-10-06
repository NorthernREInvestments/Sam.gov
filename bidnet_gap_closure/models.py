"""BidNet missing-inventory reconciliation. Does not retune discovery coverage thresholds."""

from __future__ import annotations

BUILD = "20261006-m3-bidnet-gap-closure-v1"

REPORT_JSON = "m3_bidnet_gap_closure_v1_last_report.json"
REPORT_TXT = "m3_bidnet_gap_closure_v1_last_report.txt"
CHECKPOINT = "bidnet_auth/gap_closure_checkpoint.json"

BASELINE_REPORTED = 24372
BASELINE_HARVESTED = 21969
BASELINE_MISSING = 2403

CLASSES = (
    "STALE_OR_CLOSED",
    "DUPLICATE_OR_MERGED",
    "PAGINATION_MISS",
    "STATE_SWEEP_GAP",
    "FILTER_EXCLUSION",
    "AUTH_SESSION_VARIANCE",
    "MALFORMED_RECORD",
    "TEMPORARY_FETCH_FAILURE",
    "ACCESSIBLE_BUT_MISSED",
    "UNKNOWN",
)

RETRY_CLASSES = {
    "PAGINATION_MISS",
    "STATE_SWEEP_GAP",
    "TEMPORARY_FETCH_FAILURE",
    "ACCESSIBLE_BUT_MISSED",
    "AUTH_SESSION_VARIANCE",
}

TERMINAL_CLASSES = {
    "STALE_OR_CLOSED",
    "DUPLICATE_OR_MERGED",
    "FILTER_EXCLUSION",
    "MALFORMED_RECORD",
    "UNKNOWN",
}
