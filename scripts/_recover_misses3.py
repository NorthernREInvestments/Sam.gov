"""Hunt 4 more Easy-25 recoveries to reach 80%."""
from __future__ import annotations

from price_adapters.browser import bing_discover_urls, bounded_browser_fetch
from price_adapters.orchestrate import _try_page
from price_adapters.validate import extract_jsonld_exact


QUERIES = [
    ("Timken", "SET6", ['"Timken SET6" bearing buy -ebay', "Timken SET6 wheel bearing price rockauto OR amazon"]),
    ("CRC", "05089", ['"CRC 05089" OR "CRC QD" 05089 buy -ebay', "CRC 05089 electronic cleaner price"]),
    ("Brady", "121943", ['"Brady" "121943" lockout tag buy -tom', "Brady 121943 danger tag price"]),
    ("Leviton", "5320-S", ['"5320-S" Leviton duplex receptacle buy', "Leviton 5320-S price grainger OR supplyhouse OR 1000bulbs"]),
    ("Milwaukee", "48-22-1902", ['"48-22-1902" price -ebay', "Milwaukee 48-22-1902 buy"]),
    ("Makita", "B-45580", ['"B-45580" price -ebay', "Makita B-45580 circular blade"]),
    ("Watts", "LF777M2-QT", ['"LF777M2" Watts buy -ebay', "Watts LF777M2-QT pressure reducing"]),
    ("Fleetguard", "FF63009", ['"FF63009" price -ebay', "Fleetguard FF63009 fuel filter buy"]),
]


def main() -> None:
    for mfr, pn, qs in QUERIES:
        print("\n===", pn)
        identity = {
            "part_number": pn,
            "mpn": pn,
            "manufacturer": mfr,
            "raw_description": f"{mfr} {pn}",
            "expected_condition": "NEW",
            "expected_uom": "EA",
            "expected_pack": 1,
        }
        found = False
        for q in qs:
            for row in bing_discover_urls(q, limit=6):
                url = row.get("url") or ""
                low = url.lower()
                if any(x in low for x in ("ebay", "amazon.com/s?", "wikipedia", "facebook", "reddit", "youtube", "tom-brady", "whatsapp")):
                    continue
                cand = _try_page(url, identity, allow_browser=False) or _try_page(url, identity, allow_browser=True)
                print(" ", "HIT" if cand else "miss", (cand or {}).get("unit_price"), (cand or {}).get("seller"), url[:110])
                if cand:
                    found = True
                    break
            if found:
                break


if __name__ == "__main__":
    main()
