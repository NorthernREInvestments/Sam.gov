"""Compact discovery source health — avoid infinite retry on broken sources."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from application_clock import now_utc
from pursuit_qualification_constants import (
    SRC_ANTI,
    SRC_AUTH,
    SRC_BAD_RECIPE,
    SRC_CHANGED,
    SRC_NETWORK,
    SRC_NO_OPEN,
    SRC_NOT_DISCOVERY,
    SRC_PARSER,
    SRC_UNKNOWN,
)

DEFAULT_PATH = Path(__file__).resolve().parent / "artifacts" / "source_health.json"


def _utc() -> str:
    return now_utc().isoformat()


def classify_source_failure(per_source_row: dict[str, Any]) -> str:
    if per_source_row.get("ok") is True and (per_source_row.get("unique") or per_source_row.get("raw") or 0) > 0:
        return "OK"
    err = str(per_source_row.get("error") or "").lower()
    stop = str(per_source_row.get("source_stop_reason") or "").upper()
    validation = per_source_row.get("validation")
    fail = ""
    if isinstance(validation, dict):
        fail = str(validation.get("failure_type") or "").upper()
        warnings = " ".join(validation.get("warnings") or []).lower()
    else:
        warnings = ""
        fail = str(validation or "").upper()

    if "getaddrinfo" in err or "dns" in err or "timed out" in err or "timeout" in err:
        return SRC_NETWORK
    if fail in {"AUTH_REQUIRED"} or "403" in err or stop == "AUTH_REQUIRED":
        return SRC_AUTH
    if "captcha" in err or fail in {"CAPTCHA", "BLOCKED"}:
        return SRC_ANTI
    if fail == "PARSER_FAILURE" or stop == "PARSER_FAILURE" or "parser" in warnings:
        return SRC_PARSER
    if "404" in err or fail.startswith("HTTP_404") or stop in {"HTTP_404", "SOURCE_CHANGED"}:
        return SRC_CHANGED
    if stop in {"COMPLETED"} and (per_source_row.get("unique") or 0) == 0:
        return SRC_NO_OPEN
    if "marketing" in warnings or "not_open" in warnings:
        return SRC_NOT_DISCOVERY
    if "recipe" in err:
        return SRC_BAD_RECIPE
    if stop == "SOURCE_EXCEPTION" and ("getaddrinfo" in err or "dns" in err):
        return SRC_NETWORK
    if per_source_row.get("ok") is False:
        return SRC_UNKNOWN
    return SRC_NO_OPEN


def recommend_retry(failure_type: str) -> str:
    if failure_type == "OK":
        return "CONTINUE"
    if failure_type == SRC_NETWORK:
        return "RETRY_LATER_LIMITED"
    if failure_type == SRC_PARSER:
        return "NEEDS_PARSER_REPAIR"
    if failure_type == SRC_CHANGED:
        return "NEEDS_URL_OR_ADAPTER_UPDATE"
    if failure_type == SRC_AUTH:
        return "SKIP_UNTIL_PUBLIC_FEED"
    if failure_type == SRC_ANTI:
        return "SKIP"
    if failure_type == SRC_NOT_DISCOVERY:
        return "DEMOTE_FROM_DISCOVERY"
    if failure_type == SRC_NO_OPEN:
        return "RETRY_INFREQUENT"
    return "UNKNOWN"


def build_source_health_report(discovery_metrics: dict[str, Any]) -> dict[str, Any]:
    per = discovery_metrics.get("per_source") or {}
    rows = []
    for sid, row in per.items():
        if not isinstance(row, dict):
            continue
        ftype = classify_source_failure(row)
        rows.append(
            {
                "source": sid,
                "last_attempt": _utc(),
                "last_successful_discovery": _utc() if ftype == "OK" else None,
                "status": "HEALTHY" if ftype == "OK" else "UNHEALTHY",
                "failure_type": None if ftype == "OK" else ftype,
                "candidate_count": row.get("unique") or row.get("raw") or 0,
                "transactional_candidate_count": row.get("product_candidates") or 0,
                "confidence": "HIGH" if ftype in {"OK", SRC_PARSER, SRC_NETWORK, SRC_AUTH} else "MEDIUM",
                "recommended_retry_behavior": recommend_retry(ftype),
                "url": row.get("url"),
                "stop_reason": row.get("source_stop_reason"),
            }
        )
    return {
        "kind": "SourceHealthReport",
        "generated_at": _utc(),
        "sources": rows,
        "ok_count": sum(1 for r in rows if r["status"] == "HEALTHY"),
        "fail_count": sum(1 for r in rows if r["status"] != "HEALTHY"),
    }


def persist_source_health(report: dict[str, Any], path: Path | None = None) -> Path:
    path = path or DEFAULT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return path


def analyze_source_failures(report: dict[str, Any]) -> dict[str, Any]:
    by_type: dict[str, list[str]] = {}
    for r in report.get("sources") or []:
        ft = r.get("failure_type") or "OK"
        by_type.setdefault(ft, []).append(r["source"])
    fixable = [
        s
        for s in (report.get("sources") or [])
        if s.get("failure_type") in {SRC_PARSER, SRC_CHANGED, SRC_BAD_RECIPE}
    ]
    return {
        "kind": "SourceFailureAnalysis",
        "by_type": {k: {"count": len(v), "sources": v} for k, v in by_type.items()},
        "highest_value_fix_candidates": fixable[:8],
        "note": "Do not bypass auth; prioritize parser/URL repairs that expand transactional discovery",
    }
