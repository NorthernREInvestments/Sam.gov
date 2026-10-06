from __future__ import annotations

import json

from m3_data_root import data_path
from seller_rediscovery.domain_yield import load_yield, save_yield, yield_snapshot, suppressed_domains
from seller_rediscovery.models import FROZEN, PRICE_FOUND, PRODUCT_PUBLIC_PRICE_EXHAUSTED
from seller_rediscovery.sweep import build_final_report, format_completion_report

ckp = data_path("m3_seller_rediscovery_v1_checkpoint.json")
ck = json.loads(ckp.read_text(encoding="utf-8"))

# Purge wrong-pack oatey gallon
row = ck["items"].get("plumb-oatey-31016") or {}
url = str((row.get("found") or {}).get("source_url") or "").lower()
price = float((row.get("found") or {}).get("unit_price") or 0)
if (row.get("found") or {}).get("usable") and ("gallon" in url or price >= 50):
    print("purging oatey-31016", price, url)
    ck["items"]["plumb-oatey-31016"] = {
        "benchmark_id": "plumb-oatey-31016",
        "status": PRODUCT_PUBLIC_PRICE_EXHAUSTED,
        "found": {"usable": False, "miss_notes": ["PURGED_WRONG_PACK_GALLON"]},
        "accuracy": {"claimed": False, "correct": False, "class": "NO_PRICE_CLAIMED"},
        "purged": True,
        "prior_found": row.get("found"),
    }

# Remove test domains from yield
payload = load_yield()
for d in list((payload.get("domains") or {}).keys()):
    if d.startswith("test-"):
        payload["domains"].pop(d, None)
save_yield(payload)

ck["domain_yield"] = yield_snapshot(40)
ck["suppressed_domains"] = suppressed_domains()
ckp.write_text(json.dumps(ck, indent=2, default=str), encoding="utf-8")

report = build_final_report(ck)
print(format_completion_report(report))
print(json.dumps(report["FINAL_ANSWERS"], indent=2, default=str))
print("CONTROLS", report.get("CONTROLS"))
print("KNOWN_MISS", json.dumps(report.get("KNOWN_MISS_CORPUS"), indent=2, default=str))
print("TOP DOMAINS", json.dumps(report.get("DOMAIN_YIELD")[:10], indent=2, default=str))
print("ROUTES", report.get("RECOVERY_ROUTES"))
print("EFFICIENCY", report.get("EFFICIENCY"))
print("GAP_N", len(report.get("COVERAGE_GAP") or []))
