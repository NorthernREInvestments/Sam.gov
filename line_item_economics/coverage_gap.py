"""Source coverage gap reporting — opportunities found outside M3."""

from __future__ import annotations

import json
from typing import Any

from application_clock import now_utc

GAP_REASONS = {
    "source_not_monitored",
    "auth_captcha",
    "parser_failure",
    "filtered_incorrectly",
    "merge_failure",
    "unknown",
}


def _path():
    from m3_data_root import data_path

    return data_path("m3_source_coverage_gaps.json")


def record_source_coverage_gap(
    *,
    source_portal: str,
    buyer: str | None,
    opportunity_id: str | None,
    category: str | None,
    why_missed: str,
    title: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    reason = why_missed if why_missed in GAP_REASONS else "unknown"
    entry = {
        "recorded_at": now_utc().isoformat(),
        "source_portal": source_portal,
        "buyer": buyer,
        "opportunity_id": opportunity_id,
        "title": title,
        "category": category,
        "why_m3_missed_it": reason,
        "notes": notes,
    }
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"kind": "SourceCoverageGapReport", "gaps": []}
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            payload = {"kind": "SourceCoverageGapReport", "gaps": []}
    gaps = list(payload.get("gaps") or [])
    gaps.insert(0, entry)
    payload["gaps"] = gaps[:500]
    payload["updated_at"] = now_utc().isoformat()
    payload["count"] = len(payload["gaps"])
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return entry


def source_coverage_gap_report(limit: int = 100) -> dict[str, Any]:
    path = _path()
    if not path.exists():
        return {"kind": "SourceCoverageGapReport", "count": 0, "gaps": [], "by_reason": {}}
    payload = json.loads(path.read_text(encoding="utf-8"))
    gaps = list(payload.get("gaps") or [])[:limit]
    by_reason: dict[str, int] = {}
    for g in gaps:
        r = str(g.get("why_m3_missed_it") or "unknown")
        by_reason[r] = by_reason.get(r, 0) + 1
    return {
        "kind": "SourceCoverageGapReport",
        "count": len(gaps),
        "gaps": gaps,
        "by_reason": by_reason,
        "path": str(path),
    }
