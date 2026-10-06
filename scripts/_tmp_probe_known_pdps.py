"""Probe known PDP HTML for price signals."""
from __future__ import annotations

import re
from public_price_search.search import fetch_page
from price_adapters.validate import seller_of

URLS = [
    "https://www.acmetools.com/crescent-ac216cvs-adjustable-wrench.html",
    "https://www.toolbarn.com/makita-b-45580/",
    "https://www.1000bulbs.com/product/218154/PHILIPS-9290018191.html",
    "https://crcautocare.com/product/crc-brakleen-brake-parts-cleaner-non-chlorinated-14-oz-05005/",
    "https://www.seton.com/brady-danger-do-not-operate-tag-b906.html",
    "https://www.lightbulbs.com/product/philips-9290018191",
    "https://www.zoro.com/crc-brakleen-brake-parts-cleaner-14-oz-05005/i/G1031581/",
]

for u in URLS:
    fr = fetch_page(u, use_budget=True)
    html = fr.get("text") or ""
    print("===", seller_of(u), "status", fr.get("status_code"), "len", len(html), "blocked", fr.get("blocked"))
    for needle in ("application/ld+json", "__NEXT_DATA__", "shopify", "productPrice", "currentPrice", "itemprop=\"price\""):
        if needle.lower() in html.lower():
            print("  has", needle)
    prices = re.findall(r'"price"\s*:\s*"?(\d+\.?\d*)"?', html[:300000], flags=re.I)[:10]
    print("  price fields", prices)
    m = re.search(r"<title[^>]*>([^<]{0,140})", html, re.I)
    print("  title", (m.group(1).strip() if m else "")[:110])
    # Magento/BigCommerce common
    for pat in (r'data-price-amount="([\d.]+)"', r'"product":\{[^}]{0,200}"price":([\d.]+)', r'itemprop="price"[^>]*content="([\d.]+)"'):
        mm = re.search(pat, html[:400000], re.I)
        if mm:
            print("  match", pat[:40], mm.group(1))
