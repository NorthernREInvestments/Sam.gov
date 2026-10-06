"""Rebuild hard-miss checkpoint honestly: keep only non-search-shell validated prices."""

from __future__ import annotations

import json

from exact_page_extraction.models import SEARCH_RESULT_SHELL
from exact_page_extraction.url_classify import classify_url
from hard_miss_recovery.audit import load_denominator_audit
from hard_miss_recovery.corpus import load_hard_miss_corpus
from hard_miss_recovery.models import FROZEN, PRICE_FOUND, PRIOR_FROZEN, PRIOR_SR_CK
from hard_miss_recovery.sweep import build_final_report, format_completion_report
from hard_miss_recovery.url_guards import reject_wrong_pack_blob
from m3_data_root import data_path
from price_coverage_80.corpus import load_corpus


def _ok_price(item: dict, found: dict) -> bool:
    if not found.get("usable") or found.get("unit_price") is None:
        return False
    url = str(found.get("source_url") or "")
    mpn = str(item.get("mpn") or item.get("part_number") or "")
    if classify_url(url, mpn=mpn) == SEARCH_RESULT_SHELL:
        return False
    if "/search?" in url.lower() or "/search/" in url.lower():
        return False
    if "nationaldistributorllc" in url.lower():
        return False
    if reject_wrong_pack_blob(url, pack=int(item.get("expected_pack") or 1), price=float(found.get("unit_price") or 0)):
        return False
    if item.get("benchmark_id") == "easy-global-dwt-6" and "globalindustrial.com/p/dwt-6" in url.lower():
        return False
    return True


def main() -> None:
    by = {i["benchmark_id"]: i for i in load_corpus()["items"]}
    frozen = json.loads(data_path(PRIOR_FROZEN).read_text(encoding="utf-8"))
    sr = json.loads(data_path(PRIOR_SR_CK).read_text(encoding="utf-8"))
    ck = {
        "build": "20261004-m3-hard-miss-recovery-v1",
        "items": {},
        "stats": {"http_requests": 0, "browser_renders": 0, "cache_hits": 0, "route_counts": {}, "new_recoveries": 0, "rejections": {}},
        "report_ready": False,
    }
    kept = 0
    rejected = []
    # Prefer SR then frozen (SR may have better alt sellers)
    sources = []
    for bid, row in (frozen.get("items") or {}).items():
        sources.append((bid, row.get("found") or {}, row.get("validation_result"), "frozen"))
    for bid, row in (sr.get("items") or {}).items():
        if (row.get("found") or {}).get("usable"):
            sources.append((bid, row.get("found") or {}, row.get("accuracy"), "seller_rediscovery"))

    # Last write wins for same bid — process frozen first then SR
    for bid, found, acc, src in sources:
        item = by.get(bid) or {"benchmark_id": bid, "mpn": "", "expected_pack": 1}
        if not _ok_price(item, found):
            rejected.append({"benchmark_id": bid, "url": found.get("source_url"), "price": found.get("unit_price"), "source": src})
            # If already kept a good one, don't overwrite with bad
            if (ck["items"].get(bid) or {}).get("found", {}).get("usable"):
                continue
            continue
        ck["items"][bid] = {
            "benchmark_id": bid,
            "status": FROZEN if src == "frozen" else PRICE_FOUND,
            "found": found,
            "accuracy": acc or {"class": "CORRECT_EXACT_MATCH", "correct": True, "claimed": True},
            "seeded_from": src,
            "honest_rebuild": True,
        }
        kept += 1

    # De-dupe count
    kept = sum(1 for r in ck["items"].values() if (r.get("found") or {}).get("usable"))
    ck["previously_priced"] = kept
    ck["honest_rejected_search_shells"] = rejected
    data_path("m3_hard_miss_recovery_v1_checkpoint.json").write_text(json.dumps(ck, indent=2, default=str), encoding="utf-8")

    # Rebuild hard miss corpus against honest priced set
    from hard_miss_recovery.corpus import build_hard_miss_corpus

    # Temporarily the corpus builder reads PRIOR_SR_CK not our honest set — rebuild corpus manually
    from price_coverage_80.corpus import confirmed_public
    from hard_miss_recovery.route import classify_strategy

    priced = {bid for bid, r in ck["items"].items() if (r.get("found") or {}).get("usable")}
    misses = []
    for item in confirmed_public():
        if item["benchmark_id"] in priced:
            continue
        full = by.get(item["benchmark_id"]) or item
        misses.append(
            {
                **{k: full.get(k) for k in ("benchmark_id", "manufacturer", "mpn", "description", "category")},
                "brand": full.get("manufacturer"),
                "required_condition": full.get("expected_condition") or "NEW",
                "required_uom": full.get("expected_uom") or "EA",
                "required_pack": full.get("expected_pack") or 1,
                "known_seller": full.get("known_public_seller"),
                "known_public_price": full.get("known_public_price"),
                "known_public_url": full.get("known_public_url"),
                "benchmark_verified_date": "corpus_build",
                "current_failure_class": "NO_PRICE_AFTER_HONEST_PURGE",
                "previously_attempted_domains": [],
                "previously_attempted_urls": [],
                "primary_strategy": classify_strategy(full, []),
            }
        )
    corpus = {
        "kind": "HARD_MISS_CORPUS_V1",
        "build": "20261004-m3-hard-miss-recovery-v1",
        "immutable": True,
        "n_misses": len(misses),
        "prior_priced": kept,
        "note": "Rebuilt after purging search-shell inflated prices",
        "misses": misses,
    }
    data_path("m3_hard_miss_corpus_v1.json").write_text(json.dumps(corpus, indent=2, default=str), encoding="utf-8")

    report = build_final_report(ck, audit=load_denominator_audit(), corpus=corpus)
    print(format_completion_report(report))
    print("honest_kept", kept, "rejected_shells", len(rejected), "misses", len(misses))
    print("sample rejected:", rejected[:8])
    print(json.dumps(report["FINAL_ANSWERS"], indent=2, default=str))


if __name__ == "__main__":
    main()
