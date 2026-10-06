"""Recover remaining Easy-25 misses via known routes."""
from __future__ import annotations

import time

from price_adapters.browser import bing_discover_urls, bounded_browser_fetch
from price_adapters.orchestrate import recover_with_adapters, _try_page, _identity
from price_adapters.validate import extract_jsonld_exact, extract_near_mpn_price


def try_urls(mfr: str, pn: str, urls: list[str]) -> None:
    identity = {
        "part_number": pn,
        "mpn": pn,
        "manufacturer": mfr,
        "raw_description": f"{mfr} {pn}",
        "expected_condition": "NEW",
        "expected_uom": "EA",
        "expected_pack": 1,
    }
    for url in urls:
        for allow in (False, True):
            cand = _try_page(url, identity, allow_browser=allow)
            print(" ", "br" if allow else "st", url[:90], cand)


def main() -> None:
    # Dewalt motion known
    try_urls(
        "DEWALT",
        "DWHT11131",
        ["https://www.motion.com/products/sku/06668580"],
    )
    # Loctite via motion from prior bing cache
    links = bing_discover_urls('Loctite 24221 buy price -amazon -ebay', limit=6)
    print("loctite links", [(l.get("url") or "")[:100] for l in links])
    try_urls("Loctite", "24221", [l["url"] for l in links[:3] if l.get("url")])

    for mfr, pn, desc, seller in [
        ("DEWALT", "DWHT11131", "DEWALT folding utility knife", "zoro.com"),
        ("Loctite", "24221", "Loctite threadlocker", "grainger.com"),
        ("CRC", "05089", "CRC QD Electronic Cleaner", "grainger.com"),
        ("Brady", "121943", "Brady lockout tag", "grainger.com"),
        ("Brother", "TN760", "Brother toner", "staples.com"),
        ("Leviton", "5320-S", "Leviton outlet", "grainger.com"),
        ("Watts", "LF777M2-QT", "Watts pressure reducing valve", "supplyhouse.com"),
        ("Fleetguard", "FF63009", "Fleetguard fuel filter", "finditparts.com"),
        ("Gates", "38507", "Gates belt", "grainger.com"),
        ("Timken", "SET6", "Timken bearing", "grainger.com"),
        ("Milwaukee", "48-22-1902", "Milwaukee tool", "grainger.com"),
        ("Makita", "B-45580", "Makita blade", "grainger.com"),
    ]:
        r = recover_with_adapters(
            {
                "manufacturer": mfr,
                "mpn": pn,
                "description": desc,
                "known_public_seller": seller,
                "expected_condition": "NEW",
            },
            allow_browser=True,
            max_alts=5,
            item_deadline=time.time() + 40,
        )
        b = r.get("best") or {}
        print("REC", pn, b.get("unit_price"), b.get("seller"), b.get("via"), "cands", len(r.get("candidates") or []))


if __name__ == "__main__":
    main()
