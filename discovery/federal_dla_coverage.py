"""Persist + report Federal/DLA coverage metrics for Home/API (minimal UI surface)."""

from __future__ import annotations

import json
from typing import Any

from application_clock import now_utc
from federal_dla_constants import FEDERAL_COVERAGE_KEY


def _utc() -> str:
    return now_utc().isoformat()


def save_federal_dla_coverage(payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    out["kind"] = "M3FederalDlaCoverage"
    out["updated_at"] = _utc()
    raw = json.dumps(out, default=str)
    try:
        from pathlib import Path

        path = Path(__file__).resolve().parent / "artifacts" / "m3_federal_dla_coverage.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(raw, encoding="utf-8")
    except Exception:
        pass
    try:
        from database import SessionLocal
        from models import AppSetting

        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == FEDERAL_COVERAGE_KEY).one_or_none()
            if row:
                row.value = raw
            else:
                db.add(AppSetting(key=FEDERAL_COVERAGE_KEY, value=raw))
            db.commit()
        finally:
            db.close()
    except Exception:
        pass
    return out


def load_federal_dla_coverage() -> dict[str, Any]:
    try:
        from database import SessionLocal
        from models import AppSetting

        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == FEDERAL_COVERAGE_KEY).one_or_none()
            if row and row.value:
                data = json.loads(row.value)
                if isinstance(data, dict):
                    return data
        finally:
            db.close()
    except Exception:
        pass
    try:
        from pathlib import Path

        path = Path(__file__).resolve().parent / "artifacts" / "m3_federal_dla_coverage.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"kind": "M3FederalDlaCoverage", "status": "EMPTY"}


def build_coverage_snapshot(
    *,
    federal_sam: dict[str, Any] | None = None,
    dla_from_sam: dict[str, Any] | None = None,
    dibbs_probe: dict[str, Any] | None = None,
    reconciliation: dict[str, Any] | None = None,
    coverage_matrix: list[dict[str, Any]] | None = None,
    gap_queue: list[dict[str, Any]] | None = None,
    sample: dict[str, Any] | None = None,
) -> dict[str, Any]:
    fs = federal_sam or {}
    dla = dla_from_sam or {}
    by_sem = fs.get("by_semantic") or {}
    by_agency = fs.get("by_agency") or {}
    return save_federal_dla_coverage(
        {
            "federal_current_notices": fs.get("unique_new") or fs.get("m3_count") or 0,
            "federal_bid_ready": by_sem.get("BID_OR_QUOTE_READY") or 0,
            "federal_upcoming": by_sem.get("UPCOMING_PROCUREMENT") or 0,
            "federal_sources_sought": by_sem.get("MARKET_RESEARCH") or 0,
            "federal_other": sum(
                int(v or 0)
                for k, v in by_sem.items()
                if k not in {"BID_OR_QUOTE_READY", "UPCOMING_PROCUREMENT", "MARKET_RESEARCH"}
            ),
            "federal_product_likely": dla.get("product_likely"),  # filled by caller preferred
            "federal_by_agency": by_agency,
            "federal_coverage_state": fs.get("coverage_state"),
            "federal_pagination_complete": fs.get("pagination_complete"),
            "federal_checkpoint": fs.get("checkpoint"),
            "federal_count_reconciliation": fs.get("count_reconciliation"),
            "sam_health": {
                "executed": fs.get("executed"),
                "LIVE_SAM_CALLS": fs.get("LIVE_SAM_CALLS"),
                "stop_reason": fs.get("stop_reason"),
                "coverage_state": fs.get("coverage_state"),
            },
            "dla_current": dla.get("current_unique") or 0,
            "dla_bid_ready": dla.get("bid_ready") or 0,
            "dla_product_likely": dla.get("product_likely") or 0,
            "dla_exact_nsn": dla.get("exact_nsn") or 0,
            "dla_exact_pn": dla.get("exact_pn") or 0,
            "dla_quantity": dla.get("quantity") or 0,
            "dla_approved_source": dla.get("approved_source") or 0,
            "dla_by_family": dla.get("by_family") or {},
            "dibbs_access_mode": (dibbs_probe or {}).get("access_state"),
            "dla_reconciliation": reconciliation,
            "dla_coverage_matrix": coverage_matrix or [],
            "gap_queue": gap_queue or [],
            "dla_coverage_sample": sample,
            "DEVELOPMENT_NO_OUTREACH": True,
            "anti_bot_bypass": 0,
        }
    )


def federal_dla_home_block() -> dict[str, Any]:
    """Compact block for Home / discovery status."""
    c = load_federal_dla_coverage()
    if c.get("status") == "EMPTY" and not c.get("federal_current_notices"):
        return {
            "available": False,
            "note": "No Federal/DLA coverage snapshot yet",
        }
    return {
        "available": True,
        "federal_current_notices": c.get("federal_current_notices"),
        "federal_bid_ready": c.get("federal_bid_ready"),
        "federal_product_likely": c.get("federal_product_likely"),
        "federal_unknown": c.get("federal_unknown"),
        "dla_current": c.get("dla_current"),
        "dla_bid_ready": c.get("dla_bid_ready"),
        "dla_product_likely": c.get("dla_product_likely"),
        "dla_exact_nsn": c.get("dla_exact_nsn"),
        "dla_exact_pn": c.get("dla_exact_pn"),
        "sam_health": c.get("sam_health"),
        "dibbs_access_mode": c.get("dibbs_access_mode"),
        "dla_reconciliation_status": (c.get("dla_reconciliation") or {}).get("kind"),
        "federal_coverage_state": c.get("federal_coverage_state"),
        "federal_last_checkpoint": (c.get("federal_checkpoint") or {}).get("seen_count"),
        "gap_queue_top": (c.get("gap_queue") or [])[:5],
        "updated_at": c.get("updated_at"),
    }
