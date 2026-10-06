"""Full-100 throughput build constants + item statuses.

Build: 20261004-m3-full100-throughput-v1
"""

from __future__ import annotations

BUILD = "20261004-m3-full100-throughput-v1"

NOT_STARTED = "NOT_STARTED"
IN_PROGRESS = "IN_PROGRESS"
PRICE_FOUND = "PRICE_FOUND"
BLOCKED_RETRYABLE = "BLOCKED_RETRYABLE"
NO_PRICE_EXHAUSTIVE = "NO_PRICE_EXHAUSTIVE"
ERROR = "ERROR"
ROUTE_TIMEOUT = "ROUTE_TIMEOUT"

# Explicit finished statuses only. ERROR / BLOCKED_RETRYABLE remain unfinished until resolved.
TERMINAL = {PRICE_FOUND, NO_PRICE_EXHAUSTIVE}

KNOWN_MISSES = [
    {"id": "easy-makita-b-45580", "manufacturer": "Makita", "mpn": "B-45580", "domain": "grainger.com"},
    {"id": "easy-watts-lf777m2", "manufacturer": "Watts", "mpn": "LF777M2-QT", "domain": "supplyhouse.com"},
    {"id": "easy-leviton-5320", "manufacturer": "Leviton", "mpn": "5320-S", "domain": "grainger.com"},
    {"id": "easy-brady-121943", "manufacturer": "Brady", "mpn": "121943", "domain": "grainger.com"},
    {"id": "easy-fleetguard-ff63009", "manufacturer": "Fleetguard", "mpn": "FF63009", "domain": "finditparts.com"},
]

LOW_YIELD_PRIMARY = {"grainger.com", "supplyhouse.com", "zoro.com", "finditparts.com"}

CK = "m3_full100_throughput_v1_checkpoint.json"
JOB = "m3_full100_throughput_v1_job.json"
REPORT = "m3_full100_throughput_v1_last_report.json"
DOMAIN_INTEL = "m3_full100_throughput_domain_intel.json"
