"""Focused hunt for 2 more Easy-25 hits (80% gate)."""
from __future__ import annotations

from price_adapters.browser import bing_discover_urls
from price_adapters.orchestrate import _try_page

CASES = [
    ("Leviton", "5320-S", 'Leviton "5320-S" duplex receptacle buy price -clarity -crypto'),
    ("Fleetguard", "FF63009", 'Fleetguard FF63009 fuel filter buy price -ebay'),
    ("Brady", "121943", 'Brady 121943 lockout tag buy price -tom -nfl'),
    ("Makita", "B-45580", 'Makita "B-45580" circular saw blade buy price'),
    ("Milwaukee", "48-22-1902", '"48-22-1902" Milwaukee buy price -ebay'),
    ("Watts", "LF777M2-QT", '"LF777M2-QT" Watts pressure reducing valve buy'),
    ("None", "SL585101UL", '"SL585101UL" LED OR lumin OR light buy -gate -liftmaster'),
]


def main() -> None:
    for mfr, pn, q in CASES:
        print("\n===", pn)
        identity = {
            "part_number": pn,
            "mpn": pn,
            "manufacturer": None if mfr == "None" else mfr,
            "raw_description": f"{mfr} {pn}",
            "expected_condition": "NEW",
            "expected_uom": "EA",
            "expected_pack": 1,
        }
        for row in bing_discover_urls(q, limit=8):
            url = row.get("url") or ""
            low = url.lower()
            if any(
                x in low
                for x in (
                    "ebay",
                    "amazon.com/s",
                    "wikipedia",
                    "facebook",
                    "reddit",
                    "coindesk",
                    "crypto",
                    "clarity",
                    "tom-brady",
                    "nfl",
                    "youtube",
                )
            ):
                continue
            cand = _try_page(url, identity, allow_browser=False) or _try_page(url, identity, allow_browser=True)
            print(" ", "HIT" if cand else "miss", (cand or {}).get("unit_price"), (cand or {}).get("seller"), url[:110])
            if cand:
                break


if __name__ == "__main__":
    main()
