"""Purge search-shell/wrong-pack FPs and boost remaining hard misses."""

from __future__ import annotations

import json
import time

from exact_page_extraction.models import SEARCH_RESULT_SHELL
from exact_page_extraction.url_classify import classify_url
from hard_miss_recovery.corpus import load_hard_miss_corpus
from hard_miss_recovery.models import FROZEN, PRICE_FOUND, PRODUCT_PUBLIC_PRICE_EXHAUSTED
from hard_miss_recovery.recover import recover_hard_miss
from hard_miss_recovery.sweep import _found_from_recovery, _save, _score, build_final_report
from hard_miss_recovery.url_guards import reject_wrong_pack_blob
from m3_data_root import data_path
from price_adapters.models import FOUND_VALID_PRICE
from price_coverage_80.corpus import load_corpus
from price_coverage_80.scoring import score_accuracy
from public_price_search import budget as price_budget
from public_price_search.circuits import reset_all
from public_price_search.search import reset_serp_circuit


def _is_bad_found(item: dict, found: dict) -> str | None:
    url = str(found.get("source_url") or "")
    if not found.get("usable"):
        return None
    if classify_url(url, mpn=str(item.get("mpn") or "")) == SEARCH_RESULT_SHELL:
        return "SEARCH_SHELL"
    if "/search?" in url.lower() or "/search/" in url.lower():
        return "SEARCH_PATH"
    if reject_wrong_pack_blob(
        f"{found.get('selection_reason') or ''} {url}",
        pack=int(item.get("expected_pack") or 1),
        price=float(found.get("unit_price") or 0),
    ):
        return "WRONG_PACK"
    return None


def main() -> None:
    reset_all()
    reset_serp_circuit()
    price_budget.ensure_budget(minimum_remaining=500)

    ck = json.loads(data_path("m3_hard_miss_recovery_v1_checkpoint.json").read_text(encoding="utf-8"))
    by = {i["benchmark_id"]: i for i in load_corpus()["items"]}
    purged = 0
    for bid, row in list(ck.get("items", {}).items()):
        found = row.get("found") or {}
        item = by.get(bid) or {"mpn": "", "expected_pack": 1}
        reason = _is_bad_found(item, found)
        if reason:
            print("PURGE", bid, reason, found.get("unit_price"), found.get("source_url"))
            ck["items"][bid] = {
                "benchmark_id": bid,
                "status": PRODUCT_PUBLIC_PRICE_EXHAUSTED,
                "found": {"usable": False, "miss_notes": [f"PURGED_{reason}"]},
                "accuracy": {"claimed": False, "correct": False, "class": "NO_PRICE_CLAIMED"},
                "purged": True,
                "purge_reason": reason,
                "prior_found": found,
            }
            purged += 1
    print("purged", purged)

    corpus = load_hard_miss_corpus()
    stats = ck.setdefault("stats", {})
    new = 0
    remaining = []
    for m in corpus.get("misses") or []:
        bid = m["benchmark_id"]
        row = ck["items"].get(bid) or {}
        if row.get("status") in {PRICE_FOUND, FROZEN} and (row.get("found") or {}).get("usable"):
            continue
        remaining.append(m)

    print("boost remaining", len(remaining))
    for idx, miss in enumerate(remaining, 1):
        bid = miss["benchmark_id"]
        item = by[bid]
        rec = recover_hard_miss(item, miss_row=miss, stats=stats)
        found = _found_from_recovery(rec)
        if rec.get("best") and not found.get("usable"):
            found = _found_from_recovery({**rec, "status": FOUND_VALID_PRICE})
        # post guards
        bad = _is_bad_found(item, found) if found.get("usable") else None
        if bad:
            found = {"usable": False, "miss_notes": [f"REJECTED_{bad}"]}
        acc = score_accuracy(item, found)
        if acc.get("claimed") and not acc.get("correct"):
            found = {**found, "usable": False, "unit_price": None}
            acc = score_accuracy(item, found)
        status = PRICE_FOUND if found.get("usable") else PRODUCT_PUBLIC_PRICE_EXHAUSTED
        hm = rec.get("hard_miss") or {}
        disc = hm.get("discovery") or {}
        ck["items"][bid] = {
            "benchmark_id": bid,
            "status": status,
            "found": found,
            "accuracy": acc,
            "hard_miss": {
                "strategy": disc.get("strategy"),
                "n_discovered": disc.get("n_discovered"),
                "authorized_distributors": disc.get("authorized_distributors"),
                "specialist_domains": disc.get("specialist_domains"),
                "discovered": disc.get("discovered"),
                "catalog_pdfs": disc.get("catalog_pdfs"),
                "urls_tried": [
                    {
                        "url": u.get("url"),
                        "seller": u.get("seller"),
                        "visibility": u.get("price_visibility_status"),
                        "blocked": u.get("blocked"),
                    }
                    for u in (rec.get("urls_tried") or [])
                ],
                "elapsed_s": hm.get("elapsed_s"),
                "boost2": True,
            },
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        if found.get("usable"):
            new += 1
            print("RECOVERED", bid, found.get("unit_price"), found.get("seller"), found.get("source_url"))
        if idx % 5 == 0:
            print("progress", idx, "/", len(remaining), "new", new, "full", _score(ck)["priced"])
            _save("m3_hard_miss_recovery_v1_checkpoint.json", ck)

    _save("m3_hard_miss_recovery_v1_checkpoint.json", ck)
    from hard_miss_recovery.audit import load_denominator_audit

    report = build_final_report(ck, audit=load_denominator_audit(), corpus=corpus)
    print("boost2 new", new)
    print("FULL", report["FULL100"])
    print("EASY", report["EASY25"])
    print("SAFE", report["SCALE_DECISION"]["SAFE_TO_SCALE"])
    print("ANSWERS", json.dumps(report["FINAL_ANSWERS"], indent=2, default=str))


if __name__ == "__main__":
    main()
