"""Presentation helpers — never invent commercial facts."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from m3.research.enrich import enrich_opportunity_fields, human_datetime
from m3.web.labels import label
from m3.web.titles import build_display_title


def unknown(val: Any, empty: str = "Unknown") -> str:
    if val is None:
        return empty
    s = str(val).strip()
    if not s or s.lower() in {"none", "null", "n/a", "unknown"}:
        return empty
    return s


def not_researched(val: Any) -> str:
    return unknown(val, "Not Yet Researched")


def field_label(code: str | None) -> str:
    return label(code)


def days_remaining(deadline: Any) -> str | int:
    if not deadline:
        return "Unknown"
    try:
        if isinstance(deadline, datetime):
            dt = deadline
        else:
            raw = str(deadline).replace("Z", "+00:00")
            try:
                dt = datetime.fromisoformat(raw)
            except ValueError:
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
        return {
            "date": "Unknown",
            "time": "Unknown",
            "timezone": unknown(tz_label),
            "human": "Not published",
            "status": "not_published",
        }
    try:
        if isinstance(deadline, datetime):
            dt = deadline
        else:
            raw = str(deadline).replace("Z", "+00:00")
            try:
                dt = datetime.fromisoformat(raw)
            except ValueError:
                dt = datetime.fromisoformat(raw[:19])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                    tz_label = tz_label or "UTC"
        return human_datetime(dt, tz_label)
    except Exception:
        return {
            "date": "Unknown",
            "time": "Unknown",
            "timezone": unknown(tz_label),
            "human": "Unable to parse timestamp",
            "status": "unable_to_retrieve",
        }


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
        return label(str(raw))
    if evidence:
        return "Published Estimate"
    return "Not Yet Researched"


def enrich_discovery_item(item: dict[str, Any], *, saved: bool = False, catalog_matches: int = 0) -> dict[str, Any]:
    enriched = enrich_opportunity_fields(item)
    out = dict(enriched)
    titles = build_display_title(
        official_title=out.get("title"),
        buyer_name=out.get("buyer_name"),
        solicitation_number=out.get("solicitation_number"),
        source_name=(out.get("evidence") or {}).get("source_name"),
    )
    dl = format_deadline(out.get("deadline_at"), out.get("deadline_timezone"))
    fs = out.get("field_status") or {}
    out["display"] = {
        "title": titles["display_title"],
        "official_title": titles["official_title"],
        "solicitation_number": unknown(out.get("solicitation_number")),
        "buyer": titles["buyer_display"],
        "city": unknown(out.get("geo_city")),
        "state": unknown(out.get("geo_state")),
        "closing_date": dl["date"],
        "closing_time": dl["time"],
        "timezone": dl["timezone"],
        "closing_human": dl.get("human") or f"{dl['date']} {dl['time']} {dl['timezone']}",
        "days_remaining": days_remaining(out.get("deadline_at")),
        "value": format_value(out),
        "value_class": value_class(out),
        "set_aside": label(out.get("set_aside")) if out.get("set_aside") else "Unknown",
        "set_aside_verified": bool(out.get("set_aside_verified")),
        "product_category": label(out.get("product_category")) if out.get("product_category") else "Unknown",
        "quantity": unknown(out.get("quantity")),
        "documents": label(out.get("documents_available")) if out.get("documents_available") else "Unknown",
        "requirement_class": label(out.get("requirement_class")),
        "manufacturer": unknown(out.get("manufacturer"), "Not published"),
        "saved": "Saved" if saved else "Not saved",
        "catalog_matches": catalog_matches if catalog_matches else "None found",
        "field_status": {k: label(v) for k, v in fs.items()},
        "unavailable_reasons": out.get("unavailable_reasons") or {},
    }
    out["display_title"] = titles["display_title"]
    return out
