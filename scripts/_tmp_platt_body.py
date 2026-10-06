from public_price_search.circuits import reset_all
from public_price_search.search import fetch_page
import re

reset_all()
fr = fetch_page("https://www.platt.com/search?q=11055")
html = fr.get("text") or ""
print("status", fr.get("status_code"), "len", len(html))
for pat in ["__NEXT_DATA__", "application/ld+json", "/p/", "kle11055", "11055", "window.__NUXT"]:
    print(pat, html.lower().count(pat.lower()))
i = html.lower().find("11055")
print("snippet", repr(html[max(0, i - 60) : i + 120]) if i >= 0 else "none")
print("abs p", len(re.findall(r"https://www\.platt\.com/p/[^\"\s<>]+", html)))
m = re.search(r"<title[^>]*>(.*?)</title>", html[:8000], re.I | re.S)
print("title", m.group(1)[:80] if m else None)
