"""Fresh hard-miss pass: clear discovery caches, rebuild corpus, recover."""

from __future__ import annotations

import json
from pathlib import Path

from hard_miss_recovery.corpus import build_hard_miss_corpus
from hard_miss_recovery.sweep import run_hard_miss_recovery_v1
from m3_data_root import data_path


def main() -> None:
    # Clear rediscovery discovery/page caches so site probes refetch
    for name in (
        "m3_seller_rediscovery_discovery_cache_v1.json",
        "m3_hard_miss_page_cache_v1.json",
        "m3_seller_rediscovery_page_cache_v1.json",
    ):
        p = data_path(name)
        if p.exists():
            p.unlink()
            print("cleared", name)
    # Also clear bing browser cache section? leave global browser cache

    corpus = build_hard_miss_corpus(force=True)
    print("corpus misses", corpus.get("n_misses"), "prior_priced", corpus.get("prior_priced"))

    # Ensure checkpoint previously_priced matches honest kept
    ck_path = data_path("m3_hard_miss_recovery_v1_checkpoint.json")
    ck = json.loads(ck_path.read_text(encoding="utf-8"))
    # Drop exhausted hard_miss rows so they retry; keep usable prices
    drop = 0
    for bid in list(ck.get("items") or {}):
        row = ck["items"][bid]
        if (row.get("found") or {}).get("usable"):
            continue
        if row.get("hard_miss") or row.get("purged") or row.get("status") not in {"PRICE_FOUND", "FROZEN_VALIDATED"}:
            del ck["items"][bid]
            drop += 1
    ck["previously_priced"] = sum(1 for r in ck["items"].values() if (r.get("found") or {}).get("usable"))
    ck["report_ready"] = False
    ck_path.write_text(json.dumps(ck, indent=2, default=str), encoding="utf-8")
    print("cleared exhausted rows", drop, "kept priced", ck["previously_priced"])

    report = run_hard_miss_recovery_v1(fresh=False, skip_audit=True, audit_force=False)
    print(json.dumps(report.get("FULL100"), indent=2))
    print(json.dumps(report.get("EASY25"), indent=2))
    print(json.dumps(report.get("FINAL_ANSWERS"), indent=2, default=str))


if __name__ == "__main__":
    main()
