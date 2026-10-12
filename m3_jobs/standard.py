"""Permanent policy for M3 long-running batch jobs."""

from __future__ import annotations

LONG_RUNNING_JOB_EXECUTION_STANDARD: dict[str, object] = {
    "ACTIVE": True,
    "BUILD": "20261009-m3-bidnet-recovery-bounded-checkpoint-resume-v1",
    "RULES": [
        "NO_OPAQUE_LONG_RUNNING_JOBS",
        "PER_ITEM_HARD_TIMEOUT",
        "PER_ITEM_CHECKPOINT_BEFORE_NEXT",
        "HEARTBEAT_LTE_30S",
        "RESUME_FROM_CHECKPOINT",
        "STALE_LOCK_RECOVERY",
        "FORCE_RESET_PRESERVES_COMPLETED",
        "NO_SYNCHRONOUS_LONG_HTTP",
        "SINGLE_WORKER_LEASE",
        "TIMEOUT_IS_VALID_RESULT",
    ],
    "STALE_RUNNING_SECONDS": 120,
    "HEARTBEAT_MAX_SECONDS": 30,
    "DEFAULT_ITEM_TIMEOUT_SECONDS": 30,
    "MAX_ITEM_TIMEOUT_SECONDS": 45,
}
