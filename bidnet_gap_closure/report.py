"""Text completion report for the 2,403-gap reconciliation."""

from __future__ import annotations

from typing import Any

from bidnet_gap_closure.models import CLASSES


def format_gap_report(report: dict[str, Any]) -> str:
    raw = report.get("raw") or {}
    counts = report.get("classification_counts") or {}
    rec = report.get("recovery") or {}
    cov = report.get("true_coverage") or {}
    cons = report.get("conservation") or {}
    gates = report.get("gates") or {}

    def yn(flag: bool) -> str:
        return "YES" if flag else "NO"

    lines = [
        f"BUILD: {report.get('build')}",
        "",
        "RAW COUNTS",
        f"Reported open: {raw.get('reported_open')}",
        f"Harvested: {raw.get('harvested')}",
        f"Missing: {raw.get('missing')}",
        "",
        "MISSING CLASSIFICATION",
    ]
    for name in CLASSES:
        lines.append(f"{name}: {int(counts.get(name) or 0)}")
    lines.extend(
        [
            "",
            "RECOVERY",
            f"Retry candidates: {rec.get('retry_candidates')}",
            f"Recovered: {rec.get('recovered')}",
            f"Still missing: {rec.get('still_missing')}",
            f"Terminal/non-open: {rec.get('terminal_non_open')}",
            "",
            "TRUE COVERAGE",
            f"Raw reported denominator: {cov.get('reported_open_raw')}",
            f"Corrected valid-open denominator: {cov.get('valid_open_denominator')}",
            f"Valid open retrieved: {cov.get('valid_open_retrieved')}",
            f"Raw coverage: {cov.get('raw_coverage_percent')}",
            f"True coverage: {cov.get('true_coverage_percent')}",
            "",
            "CONSERVATION",
            f"Expected: {cons.get('expected')}",
            f"Harvested: {cons.get('harvested')}",
            f"Classified missing: {cons.get('classified_missing')}",
            f"Recovered: {cons.get('recovered')}",
            f"Terminal: {cons.get('terminal')}",
            f"Still missing: {cons.get('still_missing')}",
            f"Diff: {cons.get('diff')}",
            "MUST = 0",
            "",
            "GATE",
            f"Every missing item classified: {yn(bool(gates.get('every_missing_item_classified')))}",
            f"UNKNOWN <=1%: {yn(bool(gates.get('unknown_le_1pct')))}",
            f"All accessible missed recovered: {yn(bool(gates.get('all_accessible_missed_recovered')))}",
            f"No silent drops: {yn(bool(gates.get('no_silent_drops')))}",
            f"No threshold changes: {yn(bool(gates.get('no_threshold_changes')))}",
            f"PASS/FAIL: {report.get('PASS_FAIL')}",
            "",
            "FINAL STATUS",
            f"BIDNET_DISCOVERY_TRULY_COMPLETE: {report.get('BIDNET_DISCOVERY_TRULY_COMPLETE')}",
            "",
            "NEXT_RUN_ALLOWED:",
            str(report.get("NEXT_RUN_ALLOWED") or ""),
        ]
    )
    return "\n".join(lines) + "\n"
