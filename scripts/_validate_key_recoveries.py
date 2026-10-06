"""Quick validation of key recoveries before full run."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from exact_page_extraction.extract import extract_exact_page
from price_adapters.validate import mpn_in_blob

assert not mpn_in_blob("DWT-6", "DWT62")
assert mpn_in_blob("5320-S", "LEV5320S")
assert mpn_in_blob("117", "FLUFLUKE117")

cases = [
    ("5320-S", "Leviton", "https://www.platt.com/p/0034880/leviton/15a-residential-grade-duplex-receptacle-5-15r-brown/078477231951/5320-s"),
    ("1221-2", "Leviton", "https://www.platt.com/p/0034316/leviton/toggle-switch-1-pole-20-amp-120-277v-brown/078477239889/lev12212"),
    ("117", "Fluke", "https://www.platt.com/p/0680918/fluke/multimeter-with-non-contact-voltage-maximum-rating-600v/095969324205/flufluke117"),
    ("48-22-1902", "Milwaukee", "https://www.amazon.com/Milwaukee-Electric-48-22-1902-Fastback-Storage/dp/B00D0YR9A2"),
]
for mpn, mfr, url in cases:
    item = {"manufacturer": mfr, "mpn": mpn, "expected_condition": "NEW"}
    d = extract_exact_page(url, item, allow_browser=True, allow_api=True, allow_cart=False)
    print(mpn, d.get("status"), d.get("route"), (d.get("best") or {}).get("unit_price"), (d.get("best") or {}).get("seller"))
