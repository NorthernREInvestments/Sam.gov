"""Finalize hard-miss report with honest verified counts + search-shell finding."""

from __future__ import annotations

import json

from hard_miss_recovery.audit import load_denominator_audit
from hard_miss_recovery.corpus import load_hard_miss_corpus
from hard_miss_recovery.models import CURRENT_PUBLIC_NEW_PRICE_VERIFIED, CURRENT_PUBLIC_PRICE_VERIFIED_BUT_HARD_TO_EXTRACT
from hard_miss_recovery.sweep import build_final_report, format_completion_report
from m3_data_root import data_path


def main() -> None:
    ck = json.loads(data_path("m3_hard_miss_recovery_v1_checkpoint.json").read_text(encoding="utf-8"))
    audit = load_denominator_audit()
    honest_n = sum(1 for r in (ck.get("items") or {}).values() if (r.get("found") or {}).get("usable"))
    rejected = ck.get("honest_rejected_search_shells") or []

    # Annotate audit with honesty correction (denominator unchanged)
    audit["m3_executable_verified_honest"] = honest_n
    audit["prior_inflated_verified_count"] = 54
    audit["search_shell_false_positives_removed"] = len({r.get("benchmark_id") for r in rejected})
    audit["note"] = (
        "Denominator remains 82 (no item had positive evidence of no-longer-public). "
        f"Prior M3 '54 verified' included search-shell meta prices; honest executable exact-page count is {honest_n}."
    )
    # Reclassify counts for report clarity
    audit["current_public_new_verified"] = honest_n
    hard = int(audit.get("public_but_difficult") or 0) + (54 - honest_n)
    audit["public_but_difficult"] = hard
    data_path("m3_hard_miss_denominator_audit_v1.json").write_text(json.dumps(audit, indent=2, default=str), encoding="utf-8")

    report = build_final_report(ck, audit=audit, corpus=load_hard_miss_corpus())
    report["CRITICAL_FINDING"] = {
        "prior_reported_coverage": "54/82 = 65.9%",
        "honest_executable_coverage": f"{honest_n}/82 = {round(100*honest_n/82,1)}%",
        "cause": "quill.com/1000bulbs.com /search?keyword shells accepted via meta_price",
        "search_shells_removed": len({r.get("benchmark_id") for r in rejected}),
        "denominator_still_82": True,
        "fix": "Reject SEARCH_RESULT_SHELL before extract; require EXACT_PRODUCT_VERIFIED open URLs",
    }
    report["FINAL_ANSWERS"]["18_blocking_hard_miss_class"] = (
        "SEARCH_SHELL_INFLATION_PURGED + dead-primary bot-walls + insufficient open exact product URLs; "
        f"honest coverage {round(100*honest_n/82,1)}% vs prior inflated 65.9%"
    )
    report["FINAL_ANSWERS"]["prior_vs_honest"] = {
        "prior_claimed_priced": 54,
        "honest_priced": honest_n,
        "delta": 54 - honest_n,
    }
    data_path("m3_hard_miss_recovery_v1_last_report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    ck["report"] = report
    data_path("m3_hard_miss_recovery_v1_checkpoint.json").write_text(json.dumps(ck, indent=2, default=str), encoding="utf-8")
    print(format_completion_report(report))
    print(json.dumps(report["CRITICAL_FINDING"], indent=2))
    print(json.dumps(report["FINAL_ANSWERS"], indent=2, default=str))
    print(json.dumps(report["DENOMINATOR_AUDIT"], indent=2, default=str)[:1500])


if __name__ == "__main__":
    main()
