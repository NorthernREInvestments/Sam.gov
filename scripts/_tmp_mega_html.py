from public_price_search.search import fetch_page
from urllib.parse import quote_plus
import re

q = "Oatey 30241"
url = f"https://megadepot.com/?s={quote_plus(q)}"
fr = fetch_page(url)
html = fr.get("text") or ""
print("ok", fr.get("ok"), "len", len(html), "final", fr.get("url"))
hrefs = re.findall(r'href=["\']([^"\']+)["\']', html, re.I)
prod = [h for h in hrefs if "/product/" in h.lower()]
print("product hrefs", len(prod))
print(prod[:30])
print("30241 mentions", html.lower().count("30241"))
m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
print("title", (m.group(1) if m else "")[:160])
# also try direct known product
fr2 = fetch_page("https://megadepot.com/product/oatey-30241-tub-n-tile-10oz-caulk-cartridge")
print("direct", fr2.get("ok"), len(fr2.get("text") or ""))
