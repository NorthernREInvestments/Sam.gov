"""Retry Stage A failures with soft identity + price sanity; invalidate Resideo outlier."""
from __future__ import annotations

import json
from m3_data_root import data_path
from price_14_expand_patterns.sweep import ready_14_records, build_final_report, format_report
from price_14_expand_patterns.price_url import price_exact_url
from price_14_expand_patterns.patterns import learn_success
from application_clock import now_utc

CK = "m3_price_14_expand_patterns_v1_checkpoint.json"
ck = json.loads(data_path(CK).read_text(encoding="utf-8"))
stats = ck.setdefault("stats", {})
stage_a = ck.setdefault("stage_a", {})

# Invalidate Resideo outlier if present
res = stage_a.get("hvac-resideo-th6220u2000") or {}
if res.get("status") == "PASS" and float(res.get("price") or 0) > 1000:
    print("invalidate resideo", res.get("price"))
    stage_a["hvac-resideo-th6220u2000"] = {
        **res,
        "status": "FAIL",
        "price": None,
        "rejection": "price_outlier_product_class",
        "usable": False,
    }

ready = {r["benchmark_id"]: r for r in ready_14_records()}
for bid, row in list(stage_a.items()):
    if row.get("status") == "PASS" and row.get("price"):
        continue
    item = ready.get(bid)
    if not item or not item.get("url"):
        continue
    print("retry", bid)
    item_ready = {**item, "_ready_verified": True}
    result = price_exact_url(item["url"], item_ready, allow_browser=True, stats=stats, revalidate_identity=True)
    result["discovery_method"] = item.get("discovery_method")
    stage_a[bid] = {**result, "updated_at": now_utc().isoformat()}
    if result.get("status") == "PASS":
        learn_success(
            manufacturer=str(item.get("manufacturer") or ""),
            category=str(item.get("category") or ""),
            domain=str(result.get("domain") or ""),
            discovery_route=str(item.get("discovery_method") or "READY_URL"),
            price_route=str(result.get("extraction_route") or ""),
            url=item["url"],
            mpn=str(item.get("mpn") or ""),
        )
        print("  PASS", result.get("price"), result.get("extraction_route"))
    else:
        print("  FAIL", result.get("rejection"))

data_path(CK).write_text(json.dumps(ck, indent=2, default=str), encoding="utf-8")
report = build_final_report(ck)
print(format_report(report))
print(json.dumps(report.get("PRICE_THE_14"), indent=2))
print(json.dumps(report.get("FULL100"), indent=2))
print(json.dumps(report.get("EASY_25"), indent=2))
print(json.dumps(report.get("FINAL_ANSWERS"), indent=2))
