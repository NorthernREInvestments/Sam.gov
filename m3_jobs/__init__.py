"""LONG_RUNNING_JOB_EXECUTION_STANDARD — bounded, checkpointed, resumable batch jobs."""

from m3_jobs.standard import LONG_RUNNING_JOB_EXECUTION_STANDARD
from m3_jobs.store import JobStore, JobLeaseError

__all__ = [
    "LONG_RUNNING_JOB_EXECUTION_STANDARD",
    "JobStore",
    "JobLeaseError",
]
