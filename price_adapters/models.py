"""Price source adapter statuses + base protocol.

Build: 20261004-m3-price-adapters-v1
"""

from __future__ import annotations

BUILD = "20261004-m3-price-adapters-v1"

FOUND_VALID_PRICE = "FOUND_VALID_PRICE"
FOUND_PRODUCT_NO_PRICE = "FOUND_PRODUCT_NO_PRICE"
JS_HIDDEN = "JS_HIDDEN"
BLOCKED_403 = "BLOCKED_403"
BLOCKED_429 = "BLOCKED_429"
DOMAIN_UNAVAILABLE = "DOMAIN_UNAVAILABLE"
PRODUCT_NOT_FOUND = "PRODUCT_NOT_FOUND"
WRONG_PRODUCT = "WRONG_PRODUCT"
CONDITION_MISMATCH = "CONDITION_MISMATCH"
UOM_MISMATCH = "UOM_MISMATCH"
RETRYABLE = "RETRYABLE"
EXHAUSTED = "EXHAUSTED"

REGRESSION_CASES = [
    {"id": "brady-121943", "manufacturer": "Brady", "mpn": "121943", "domain": "grainger.com", "description": "Brady lockout tag 121943"},
    {"id": "crc-05089", "manufacturer": "CRC", "mpn": "05089", "domain": "grainger.com", "description": "CRC QD Electronic Cleaner 05089"},
    {"id": "loctite-24221", "manufacturer": "Loctite", "mpn": "24221", "domain": "grainger.com", "description": "Loctite 242 Threadlocker 24221"},
    {"id": "wd40-490040", "manufacturer": "WD-40", "mpn": "490040", "domain": "grainger.com", "description": "WD-40 Multi-Use 490040"},
    {"id": "filtrete-2004dc-6", "manufacturer": "Filtrete", "mpn": "2004DC-6", "domain": "grainger.com", "description": "Filtrete 2004DC-6"},
    {"id": "sharkbite-uc248lfa", "manufacturer": "SharkBite", "mpn": "UC248LFA", "domain": "supplyhouse.com", "description": "SharkBite UC248LFA"},
    {"id": "watts-lf777m2-qt", "manufacturer": "Watts", "mpn": "LF777M2-QT", "domain": "supplyhouse.com", "description": "Watts LF777M2-QT"},
    {"id": "honeywell-th8320u1008", "manufacturer": "Honeywell", "mpn": "TH8320U1008", "domain": "supplyhouse.com", "description": "Honeywell TH8320U1008"},
    {"id": "fleetguard-ff63009", "manufacturer": "Fleetguard", "mpn": "FF63009", "domain": "finditparts.com", "description": "Fleetguard FF63009"},
    {"id": "global-dwt-6", "manufacturer": "Global Industrial", "mpn": "DWT-6", "domain": "globalindustrial.com", "description": "Global Industrial DWT-6"},
    {"id": "cummins-5579409px", "manufacturer": "Cummins", "mpn": "5579409PX", "domain": "shop.cummins.com", "description": "Cummins ReCon injector 5579409PX", "expected_condition": "RECONDITIONED"},
]
