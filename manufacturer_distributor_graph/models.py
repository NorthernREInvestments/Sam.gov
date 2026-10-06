"""Manufacturer / authorized-distributor graph build constants.

Build: 20261004-m3-manufacturer-distributor-graph-v1
"""

from __future__ import annotations

BUILD = "20261004-m3-manufacturer-distributor-graph-v1"

# Domain roles after demotion
PRICE_SOURCE = "PRICE_SOURCE"
IDENTITY_SOURCE = "IDENTITY_SOURCE"
LOW_PRIORITY = "LOW_PRIORITY"

# Edge types
OEM_SELLS_DIRECT = "OEM_SELLS_DIRECT"
OEM_AUTHORIZED_DISTRIBUTOR = "OEM_AUTHORIZED_DISTRIBUTOR"
OEM_DEALER = "OEM_DEALER"
DISTRIBUTOR_CARRIES_MPN = "DISTRIBUTOR_CARRIES_MPN"
RESELLER_CARRIES_MPN = "RESELLER_CARRIES_MPN"
PUBLIC_CATALOG_LISTING = "PUBLIC_CATALOG_LISTING"
EXACT_PRODUCT_PAGE = "EXACT_PRODUCT_PAGE"
QUOTE_ONLY_CHANNEL = "QUOTE_ONLY_CHANNEL"
PRIOR_WINNER_RESELLER_PATH = "PRIOR_WINNER_RESELLER_PATH"

# Auth status
AUTHORIZED_CONFIRMED = "AUTHORIZED_CONFIRMED"
AUTHORIZED_UNKNOWN = "AUTHORIZED_UNKNOWN"
NOT_AUTHORIZED = "NOT_AUTHORIZED"
AUTHORIZATION_NOT_REQUIRED = "AUTHORIZATION_NOT_REQUIRED"

# Persist paths
CK = "m3_mfr_dist_graph_v1_checkpoint.json"
JOB = "m3_mfr_dist_graph_v1_job.json"
REPORT = "m3_mfr_dist_graph_v1_last_report.json"
GRAPH_STORE = "m3_mfr_dist_graph_v1_store.json"
URL_MEMORY = "m3_mfr_dist_graph_v1_url_memory.json"
DOMAIN_ROLES = "m3_mfr_dist_graph_v1_domain_roles.json"

# Checkpoint item statuses
NOT_STARTED = "NOT_STARTED"
IN_PROGRESS = "IN_PROGRESS"
PRICE_FOUND = "PRICE_FOUND"
NO_PRICE_EXHAUSTIVE = "NO_PRICE_EXHAUSTIVE"
BLOCKED_RETRYABLE = "BLOCKED_RETRYABLE"
ERROR = "ERROR"
TERMINAL = {PRICE_FOUND, NO_PRICE_EXHAUSTIVE}

LOW_YIELD_PRIMARY = {
    "grainger.com": LOW_PRIORITY,
    "supplyhouse.com": LOW_PRIORITY,
    "finditparts.com": LOW_PRIORITY,
    "zoro.com": LOW_PRIORITY,
    "globalindustrial.com": IDENTITY_SOURCE,  # mixed; demote primary spend
}

REGRESSION_CASES = [
    {"id": "easy-makita-b-45580", "manufacturer": "Makita", "mpn": "B-45580"},
    {"id": "easy-watts-lf777m2", "manufacturer": "Watts", "mpn": "LF777M2-QT"},
    {"id": "easy-leviton-5320", "manufacturer": "Leviton", "mpn": "5320-S"},
    {"id": "easy-brady-121943", "manufacturer": "Brady", "mpn": "121943"},
    {"id": "easy-fleetguard-ff63009", "manufacturer": "Fleetguard", "mpn": "FF63009"},
    {"id": "easy-sharkbite-uc248lfa", "manufacturer": "SharkBite", "mpn": "UC248LFA"},
    {"id": "easy-honeywell-th8320u1008", "manufacturer": "Honeywell", "mpn": "TH8320U1008"},
    {"id": "easy-global-dwt-6", "manufacturer": "Global Industrial", "mpn": "DWT-6"},
    {"id": "quote-cummins-5579409px", "manufacturer": "Cummins", "mpn": "5579409PX"},
]
