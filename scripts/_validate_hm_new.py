from __future__ import annotations

import json
import re
from pathlib import Path

from price_adapters.validate import mpn_in_blob
from price_coverage_80.corpus import load_corpus
from public_price_search.search import fetch_page

ck = json.loads(Path("data/m3_hard_miss_recovery_v1_checkpoint.json").read_text(encoding="utf-8"))
by = {i["benchmark_id"]: i for i in load_corpus()["items"]}
for bid, row in sorted(ck["items"].items()):
    if not (row.get("hard_miss") and row.get("status") == "PRICE_FOUND"):
        continue
    f = row.get("found") or {}
    item = by[bid]
    url = f.get("source_url") or ""
    fr = fetch_page(url)
    html = fr.get("text") or ""
    title_m = re.search(r"<title[^>]*>(.*?)</title>", html[:12000], re.I | re.S)
    title = re.sub(r"<[^>]+>", "", title_m.group(1) if title_m else "")[:160]
    print("===", bid, f.get("unit_price"), f.get("seller"))
    print(" url", url[:100])
    print(" title", title)
    print(" mpn_in_title", mpn_in_blob(item.get("mpn") or "", title, url))
    print(" mfr_in_title", (item.get("manufacturer") or "").split()[0].lower() in title.lower())
    print(" gallon", "gallon" in (title + url).lower())
    print(" status", fr.get("status_code"), "ok", fr.get("ok"), "len", len(html))
