"""Presentation helpers — never invent commercial facts."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def unknown(val: Any, empty: str = "Unknown") -> str:
    if val is None:
        return empty
    s = str(val).strip()
    if not s or s.lower() in {"none", "null", "n/a", "unknown"}:
        return empty
    return s


def not_researched(val: Any) -> str:
    return unknown(val, "Not Yet Researched")


def days_remaining(deadline: Any) -> str | int:
    if not deadline:
        return "Unknown"
    try:
        if isinstance(deadline, datetime):
            dt = deadline
        else:
            raw = str(deadline).replace("Z", "+00:00")
            dt = datetime.fromisoformat(raw[:19])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        days = (dt - datetime.now(timezone.utc)).days
        if days < 0:
            return "Past due"
        return days
    except Exception:
        return "Unknown"


def format_deadline(deadline: Any, tz_label: str | None = None) -> dict[str, str]:
    if not deadline:
        return {"date": "Unknown", "time": "Unknown", "timezone": unknown(tz_label)}
    try:
        if isinstance(deadline, datetime):
            dt = deadline
        else:
            raw = str(deadline).replace("Z", "+00:00")
            dt = datetime.fromisoformat(raw[:19])
        return {
            "date": dt.strftime("%Y-%m-%d"),
            "time": dt.strftime("%H:%M"),
            "timezone": unknown(tz_label or (str(dt.tzinfo) if dt.tzinfo else None), "Unknown"),
        }
    except Exception:
        return {"date": "Unknown", "time": "Unknown", "timezone": unknown(tz_label)}


def format_value(item: dict[str, Any] | Any) -> str:
    if isinstance(item, dict):
        if item.get("estimated_value_unknown") and item.get("estimated_value_min") is None:
            return "Unknown"
        lo = item.get("estimated_value_min")
        hi = item.get("estimated_value_max")
    else:
        lo = getattr(item, "estimated_value_min", None)
        hi = getattr(item, "estimated_value_max", None)
        if getattr(item, "estimated_value_unknown", True) and lo is None:
            return "Unknown"
    if lo is None and hi is None:
        return "Unknown"
    if lo is not None and hi is not None and float(lo) != float(hi):
        return f"${float(lo):,.0f} – ${float(hi):,.0f}"
    v = lo if lo is not None else hi
    return f"${float(v):,.0f}"


def value_class(item: dict[str, Any] | Any) -> str:
    if isinstance(item, dict):
        raw = item.get("value_evidence_class") or item.get("value_estimate_method")
        evidence = item.get("value_evidence")
    else:
        ev = getattr(item, "raw_evidence", None) or {}
        raw = ev.get("value_evidence_class") or getattr(item, "value_estimate_method", None)
        evidence = ev.get("value_evidence")
    if raw:
        return str(raw).replace("_", " ")
    if evidence:
        return "published estimate"
    return "Not Yet Researched"


def enrich_discovery_item(item: dict[str, Any], *, saved: bool = False, catalog_matches: int = 0) -> dict[str, Any]:
    out = dict(item)
    dl = format_deadline(item.get("deadline_at"), item.get("deadline_timezone"))
    out["display"] = {
        "title": unknown(item.get("title"), "Untitled"),
        "solicitation_number": unknown(item.get("solicitation_number")),
        "buyer": unknown(item.get("buyer_name"), "Unknown"),
        "city": unknown(item.get("geo_city")),
        "state": unknown(item.get("geo_state")),
        "closing_date": dl["date"],
        "closing_time": dl["time"],
        "timezone": dl["timezone"],
        "days_remaining": days_remaining(item.get("deadline_at")),
        "value": format_value(item),
        "value_class": value_class(item),
        "set_aside": unknown(item.get("set_aside")),
        "set_aside_verified": bool(item.get("set_aside_verified")),
        "product_category": unknown(item.get("product_category")),
        "quantity": unknown(item.get("quantity")),
        "documents": unknown(item.get("documents_available")),
        "requirement_class": unknown(item.get("requirement_class"), "Unknown"),
        "saved": "Saved" if saved else "Not saved",
        "catalog_matches": catalog_matches if catalog_matches else "None found",
    }
    return out
