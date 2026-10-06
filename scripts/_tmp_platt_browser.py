from price_adapters.browser import bing_discover_urls, bounded_browser_fetch
from public_price_search.circuits import reset_all
import re

reset_all()
rows = bing_discover_urls("site:platt.com Klein 11055", limit=5)
print("bing site", len(rows))
for r in rows:
    print((r.get("url") or "")[:110], (r.get("title") or "")[:50])

br = bounded_browser_fetch(
    "https://www.platt.com/search?q=11055",
    timeout_ms=20000,
    wait_ms=3500,
    capture_json=True,
)
html = br.get("html") or ""
print("br ok", br.get("ok"), "len", len(html), "json", br.get("json_urls"))
print("p path count", len(re.findall(r"/p/[0-9]+", html)))
links = re.findall(r"https://www\.platt\.com/p/[^\"\s<>]+", html)
print("abs links", links[:5])
