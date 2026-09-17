"""One-shot diagnostic snapshot of eligible discovery sources — no hammering loops."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from application_clock import now_utc
from discovery.selection import select_all_eligible_sources, _pool_map
from discovery.source_failure_taxonomy import classify_root_cause, root_cause_counts

DEFAULT_PATH = Path(__file__).resolve().parents[1] / "artifacts" / "source_access_baseline.json"


def _utc() -> str:
    return now_utc().isoformat()


def build_eligible_universe_snapshot(
    *,
    per_source_metrics: dict[str, Any] | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """
    Freeze eligible universe + optional last-run per_source metrics into a diagnostic snapshot.
    Does not perform network I/O.
    """
    bundle = select_all_eligible_sources(include_blocked_accounted=True)
    pool = _pool_map()
    per = per_source_metrics or {}
    rows: list[dict[str, Any]] = []

    for cand in bundle["eligible"]:
        sid = cand["source_id"]
        metrics = per.get(sid) if isinstance(per.get(sid), dict) else {}
        classification = classify_root_cause(metrics) if metrics else {
            "primary": "NOT_YET_ATTEMPTED",
            "access_outcome": "UNKNOWN",
            "secondary": [],
            "productive": False,
        }
        rows.append(
            {
                "source_id": sid,
                "source_name": cand.get("name"),
                "jurisdiction": cand.get("kind"),
                "entity": cand.get("name"),
                "state_code": cand.get("state_code"),
                "portal_family": cand.get("platform_family") or cand.get("adapter_family"),
                "adapter": cand.get("adapter_family"),
                "canonical_url": cand.get("list_url"),
                "adapter_status": cand.get("adapter_status"),
                "access_state": metrics.get("explicit_state") or metrics.get("source_stop_reason"),
                "last_http_status": (metrics.get("request_meta") or {}).get("http_status")
                if isinstance(metrics.get("request_meta"), dict)
                else None,
                "records": metrics.get("raw") or metrics.get("records_fetched") or 0,
                "pages": metrics.get("pages_fetched"),
                "pagination_complete": metrics.get("pagination_complete"),
                "pagination_stop_reason": metrics.get("pagination_stop_reason"),
                "parser_ok": metrics.get("ok"),
                "failure_reason": metrics.get("source_stop_reason") or metrics.get("error"),
                "classification": classification,
                "attempt": metrics.get("attempt", bool(metrics)),
            }
        )

    for row in bundle["accounted_non_attempt"]:
        sid = row["source_id"]
        rows.append(
            {
                "source_id": sid,
                "source_name": row.get("name"),
                "jurisdiction": row.get("kind"),
                "portal_family": row.get("platform_family") or row.get("adapter_family"),
                "adapter": row.get("adapter_family"),
                "canonical_url": row.get("list_url"),
                "access_state": row.get("selection_state") or row.get("accounted_reason"),
                "classification": {
                    "primary": row.get("selection_state") or "DISABLED",
                    "access_outcome": "ACCESS_FAILED",
                    "productive": False,
                    "external_access_block": True,
                },
                "attempt": False,
                "accounted_non_attempt": True,
            }
        )

    productive = [r for r in rows if (r.get("classification") or {}).get("productive")]
    failed = [
        r
        for r in rows
        if r.get("attempt")
        and not (r.get("classification") or {}).get("productive")
        and (r.get("classification") or {}).get("primary") not in {"VALID_HEALTHY_ZERO", "NOT_YET_ATTEMPTED"}
    ]
    by_family: dict[str, list[str]] = {}
    for r in failed:
        fam = str(r.get("portal_family") or "UNKNOWN")
        by_family.setdefault(fam, []).append(r["source_id"])

    access_parse = {"ACCESS_FAILED": 0, "ACCESS_SUCCEEDED_PARSE_FAILED": 0, "ACCESS_SUCCEEDED_HEALTHY_ZERO": 0}
    for r in rows:
        ao = (r.get("classification") or {}).get("access_outcome")
        if ao in access_parse:
            access_parse[ao] += 1

    snap = {
        "kind": "SourceAccessBaseline",
        "generated_at": _utc(),
        "run_id": run_id,
        "registered_in_pool": len(pool),
        "eligible_count": bundle["eligible_count"],
        "accounted_non_attempt_count": bundle["accounted_non_attempt_count"],
        "attempted_with_metrics": sum(1 for r in rows if r.get("attempt")),
        "productive_count": len(productive),
        "failed_nonproductive_count": len(failed),
        "root_cause_counts": root_cause_counts(failed),
        "access_vs_parse": access_parse,
        "failures_by_portal_family": {k: {"count": len(v), "sources": v} for k, v in sorted(by_family.items(), key=lambda x: -len(x[1]))},
        "sources": rows,
        "note": "Snapshot is diagnostic; productive≠market coverage",
    }
    return snap


def persist_baseline(snapshot: dict[str, Any], path: Path | None = None) -> Path:
    path = path or DEFAULT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot, indent=2, default=str), encoding="utf-8")
    return path


def cluster_fix_priority(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Prioritize portal families by yield potential (federal > state > local)."""
    rank = {
        "DIBBS": 0,
        "PIEE": 1,
        "FederalPublic": 2,
        "Jaggaer": 3,
        "BidNet": 4,
        "OpenGov": 5,
        "Bonfire": 6,
        "PlanetBids": 7,
        "PublicPurchase": 8,
        "JSON": 9,
        "StateOwned": 10,
        "SimpleHTML": 11,
    }
    fams = snapshot.get("failures_by_portal_family") or {}
    out = []
    for fam, info in fams.items():
        out.append(
            {
                "portal_family": fam,
                "failed_count": info.get("count") or 0,
                "sources": info.get("sources") or [],
                "priority_rank": rank.get(fam, 50),
            }
        )
    out.sort(key=lambda x: (x["priority_rank"], -int(x["failed_count"])))
    return out
