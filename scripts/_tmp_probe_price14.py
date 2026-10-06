"""Quick probe: can extract_exact_page price a few READY URLs?"""
from __future__ import annotations

import json
from m3_data_root import data_path
from exact_page_extraction.extract import extract_exact_page
from price_coverage_80.corpus import load_corpus

rep = json.loads(data_path("m3_open_web_product_discovery_v1_last_report.json").read_text(encoding="utf-8"))
recs = (rep.get("READY_FOR_PRICE_EXTRACTION") or {}).get("records") or []
by = {i["benchmark_id"]: i for i in (load_corpus().get("items") or [])}

for r in recs[:5]:
    bid = r["benchmark_id"]
    item = by.get(bid) or r
    item = {**item, "mpn": item.get("mpn") or r.get("mpn"), "manufacturer": item.get("manufacturer") or r.get("manufacturer")}
    url = r["url"]
    print("===", bid, url[:90])
    d = extract_exact_page(url, item, allow_browser=True, allow_api=True)
    best = d.get("best") or {}
    print(" status", d.get("status"), "route", d.get("route"), "price", best.get("unit_price"), "via", best.get("via"), "rej", d.get("rejection"))
