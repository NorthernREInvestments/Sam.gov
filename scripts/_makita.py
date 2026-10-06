from price_adapters.browser import bing_discover_urls, bounded_browser_fetch
from price_adapters.validate import extract_jsonld_exact, extract_near_mpn_price
from public_price_search.catalog_intel import extract_structured_prices
from price_adapters.orchestrate import _try_page

links = bing_discover_urls("B-45580 Makita blade", limit=10)
print([(l.get("title"), (l.get("url") or "")[:90]) for l in links])
idn = {
    "part_number": "B-45580",
    "mpn": "B-45580",
    "manufacturer": "Makita",
    "raw_description": "Makita circular saw blade",
    "expected_condition": "NEW",
    "expected_uom": "EA",
    "expected_pack": 1,
}
for l in links:
    url = l.get("url") or ""
    if any(x in url.lower() for x in ("ebay", "amazon.com/s", "wikipedia")):
        continue
    br = bounded_browser_fetch(url)
    html = br.get("html") or ""
    exact = extract_jsonld_exact(html, mpn="B-45580", manufacturer="Makita")
    print(url[:90], "exact", exact[:1], "struct", extract_structured_prices(html)[:1], "near", extract_near_mpn_price(html, mpn="B-45580", manufacturer="Makita"))
    cand = _try_page(url, idn, allow_browser=True)
    if cand:
        print("HIT", cand)
        break
