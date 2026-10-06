"""Sweep miss corpus URLs for current extractability (static+api, no browser)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from exact_page_extraction.extract import extract_exact_page
from m3_data_root import data_path

corpus = json.loads(data_path("m3_exact_page_miss_corpus_v1.json").read_text(encoding="utf-8"))
found = 0
attempted = 0
results = []
for miss in corpus.get("misses") or []:
    urls = (miss.get("exact_urls_verified") or []) + (miss.get("exact_urls_unverified") or [])
    if not urls:
        results.append({"bid": miss["benchmark_id"], "status": "NO_URL"})
        continue
    hit = None
    for url in urls[:4]:
        attempted += 1
        item = {
            "benchmark_id": miss["benchmark_id"],
            "manufacturer": miss.get("manufacturer"),
            "mpn": miss.get("mpn"),
            "expected_condition": miss.get("expected_condition") or "NEW",
            "expected_uom": miss.get("expected_uom") or "EA",
            "expected_pack": miss.get("expected_pack") or 1,
        }
        d = extract_exact_page(url, item, allow_browser=False, allow_api=True, allow_cart=False)
        if d.get("status") == "FOUND_VALID_PRICE":
            hit = {"url": url, "best": d.get("best"), "route": d.get("route")}
            found += 1
            break
        last = d.get("status")
    results.append({"bid": miss["benchmark_id"], "status": "HIT" if hit else last, "hit": hit, "n_urls": len(urls)})

print(json.dumps({"attempted_urls": attempted, "hits": found, "results": results}, indent=2, default=str))
