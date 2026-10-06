"""Audit suspicious Easy-25 priced rows for false positives."""
from __future__ import annotations

from price_adapters.orchestrate import recover_with_adapters
from public_price_search.search import fetch_page
from price_adapters.validate import extract_jsonld_exact, extract_near_mpn_price, mpn_in_blob
from public_price_search.catalog_intel import extract_structured_prices


SUSPECT = [
    ("Honeywell", "TH8320U1008", "Honeywell thermostat", "https://www.globalindustrial.com/search?q=TH8320U1008"),
    ("Ideal", "30-076", "Ideal wire nut connector", "https://www.globalindustrial.com/search?q=30-076"),
    ("CRC", "05089", "CRC cleaner", "https://www.globalindustrial.com/search?q=05089"),
    ("Gates", "38507", "Gates belt", "https://www.globalindustrial.com/search?q=38507"),
    ("Brady", "121943", "Brady lockout tag", "https://www.globalindustrial.com/search?q=121943"),
    ("SharkBite", "UC248LFA", "SharkBite PEX elbow", "https://www.mccoys.com/shop/p/9087685-052303-22/sharkbite-uc248lfa-pipe-elbow-12-in-barb-9"),
]


def main() -> None:
    for mfr, pn, desc, url in SUSPECT:
        html = fetch_page(url, use_budget=False).get("text") or ""
        print(
            "==",
            pn,
            "len",
            len(html),
            "mpn",
            mpn_in_blob(pn, html[:50000]),
            "exact",
            extract_jsonld_exact(html, mpn=pn, manufacturer=mfr)[:1],
            "near",
            extract_near_mpn_price(html, mpn=pn, manufacturer=mfr),
            "struct",
            extract_structured_prices(html)[:2],
        )
        rec = recover_with_adapters(
            {
                "manufacturer": mfr,
                "mpn": pn,
                "description": desc,
                "expected_condition": "NEW",
                "known_public_seller": "globalindustrial.com",
            },
            allow_browser=False,
            max_alts=4,
        )
        b = rec.get("best") or {}
        print(" recover", b.get("unit_price"), b.get("seller"), b.get("via"))


if __name__ == "__main__":
    main()
