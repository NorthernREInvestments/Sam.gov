"""Targeted recovery for hard Easy-25 misses."""
from __future__ import annotations

import time

from price_adapters.browser import bing_discover_urls, bounded_browser_fetch
from price_adapters.orchestrate import _try_page
from price_adapters.validate import extract_jsonld_exact, extract_near_mpn_price
from public_price_search.catalog_intel import extract_structured_prices
from public_price_search.search import fetch_page


CASES = [
    ("Leviton", "5320-S", "Leviton outlet receptacle", ["site:1000bulbs.com Leviton 5320-S", "Leviton 5320-S duplex buy -ebay"]),
    ("Brother", "TN760", "Brother toner cartridge", ["Brother TN760 toner buy site:quill.com", "Brother TN760 buy -ebay -amazon"]),
    ("CRC", "05089", "CRC electronic cleaner", ["CRC 05089 QD Electronic Cleaner buy -ebay", "\"05089\" CRC cleaner price"]),
    ("Brady", "121943", "Brady lockout tag", ["Brady 121943 lockout tag buy", "\"121943\" Brady tag price"]),
    ("Watts", "LF777M2-QT", "Watts pressure reducing valve", ["Watts LF777M2-QT PRV buy -car -ebay", "\"LF777M2-QT\" plumbing valve price"]),
    ("Gates", "38507", "Gates belt", ["Gates 38507 belt buy -ebay", "\"38507\" Gates V-belt price"]),
    ("Timken", "SET6", "Timken bearing set", ["Timken SET6 bearing buy -ebay", "\"SET6\" Timken wheel bearing"]),
    ("Milwaukee", "48-22-1902", "Milwaukee measuring tool", ["\"48-22-1902\" Milwaukee buy -ebay", "Milwaukee 48-22-1902 price"]),
    ("Makita", "B-45580", "Makita saw blade", ["\"B-45580\" Makita buy -ebay", "Makita B-45580 blade price"]),
    ("Fleetguard", "FF63009", "Fleetguard fuel filter", ["\"FF63009\" Fleetguard buy -ebay", "Fleetguard FF63009 filter price"]),
]


def main() -> None:
    for mfr, pn, desc, queries in CASES:
        print("\n===", mfr, pn)
        identity = {
            "part_number": pn,
            "mpn": pn,
            "manufacturer": mfr,
            "raw_description": desc,
            "expected_condition": "NEW",
            "expected_uom": "EA",
            "expected_pack": 1,
            "known_public_seller": "",
        }
        seen = set()
        for q in queries:
            links = bing_discover_urls(q, limit=5)
            for row in links:
                url = row.get("url") or ""
                if not url or url in seen:
                    continue
                low = url.lower()
                if any(x in low for x in ("ebay.com", "amazon.com", "wikipedia", "facebook", "reddit", "youtube")):
                    continue
                seen.add(url)
                cand = _try_page(url, identity, allow_browser=False)
                if not cand:
                    cand = _try_page(url, identity, allow_browser=True)
                print(" ", "HIT" if cand else "miss", (cand or {}).get("unit_price"), (cand or {}).get("seller"), url[:100])
                if cand:
                    break
            if any(True for _ in [1] if False):
                pass
            # break outer if we found — check last print awkwardly; use flag
        # also try quill/officedepot/1000bulbs direct
        for url in [
            f"https://www.quill.com/search?keywords={mfr}+{pn}",
            f"https://www.1000bulbs.com/search?q={pn}",
            f"https://www.officedepot.com/catalog/search.do?Ntt={pn}",
        ]:
            cand = _try_page(url, identity, allow_browser=True)
            if cand:
                print(" DIRECT", cand.get("unit_price"), cand.get("seller"), url)
                break


if __name__ == "__main__":
    main()
