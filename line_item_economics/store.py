"""Persist line-item economics analyses under M3_DATA_ROOT."""

from __future__ import annotations

import json
from typing import Any

from application_clock import now_utc


def _store_path():
    from m3_data_root import data_path

    return data_path("m3_line_item_economics_store.json")


def load_all() -> dict[str, Any]:
    path = _store_path()
    if not path.exists():
        return {"kind": "LineItemEconomicsStore", "by_opportunity": {}, "updated_at": None}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"kind": "LineItemEconomicsStore", "by_opportunity": {}, "updated_at": None}


def save_analysis(opportunity_id: str, analysis: dict[str, Any]) -> dict[str, Any]:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = load_all()
    by = dict(payload.get("by_opportunity") or {})
    by[str(opportunity_id)] = analysis
    payload["by_opportunity"] = by
    payload["updated_at"] = now_utc().isoformat()
    payload["kind"] = "LineItemEconomicsStore"
    payload["count"] = len(by)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return analysis


def load_analysis(opportunity_id: str) -> dict[str, Any] | None:
    by = load_all().get("by_opportunity") or {}
    return by.get(str(opportunity_id))
