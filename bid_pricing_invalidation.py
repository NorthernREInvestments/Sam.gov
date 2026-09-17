"""Price freshness + targeted pricing invalidation on amendments."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from application_clock import now_utc
from bid_pricing_constants import (
    PRICE_AGING,
    PRICE_CURRENT,
    PRICE_EXPIRED,
    PRICE_REVERIFY,
    PRICE_STALE,
)


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def evaluate_price_freshness(
    *,
    priced_at: str | None,
    validity_days: float = 14.0,
    aging_days: float = 7.0,
    expired_explicit: bool = False,
) -> dict[str, Any]:
    if expired_explicit:
        return {"state": PRICE_EXPIRED, "blocks_final_pricing_readiness": True, "priced_at": priced_at}
    dt = _parse(priced_at)
    if dt is None:
        return {
            "state": PRICE_REVERIFY,
            "blocks_final_pricing_readiness": True,
            "priced_at": priced_at,
            "reason": "no_price_timestamp",
        }
    age = now_utc() - dt
    if age > timedelta(days=validity_days):
        return {
            "state": PRICE_STALE,
            "blocks_final_pricing_readiness": True,
            "age_days": age.total_seconds() / 86400.0,
            "priced_at": priced_at,
        }
    if age > timedelta(days=aging_days):
        return {
            "state": PRICE_AGING,
            "blocks_final_pricing_readiness": False,
            "age_days": age.total_seconds() / 86400.0,
            "priced_at": priced_at,
            "note": "Still usable for pursuit; reverify before finalization",
        }
    return {
        "state": PRICE_CURRENT,
        "blocks_final_pricing_readiness": False,
        "age_days": age.total_seconds() / 86400.0,
        "priced_at": priced_at,
    }


PRICING_DEPENDENCY_MAP = {
    "QUANTITY_CHANGE": [
        "extended_cost",
        "freight",
        "financing",
        "bid_price",
        "profit",
        "line_item_pricing",
    ],
    "SPEC_CHANGE": ["product_clin_pricing", "acquisition_cost", "compliance_linked_pricing"],
    "PRODUCT_CHANGE": ["product_clin_pricing", "acquisition_cost", "historical_benchmark"],
    "DELIVERY_CHANGE": ["freight", "lead_time", "financing_timeline"],
    "DEADLINE_CHANGE": ["financing_timeline"],
    "SUPPLIER_PRICE_CHANGE": ["acquisition_cost", "bid_price", "profit"],
    "FUNDING_ASSUMPTION_CHANGE": ["financing", "profit"],
}


def invalidate_pricing_state(
    pricing_state: dict[str, Any],
    *,
    change_type: str,
    affected_line_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Invalidate only affected pricing keys; preserve unrelated evidence."""
    state = dict(pricing_state or {})
    conclusions = dict(state.get("conclusions") or {})
    deps = PRICING_DEPENDENCY_MAP.get(change_type, ["bid_price", "profit"])
    invalidated = []
    for dep in deps:
        if dep in conclusions:
            conclusions[dep] = {
                "status": "INVALIDATED",
                "reason": f"pricing_change:{change_type}",
                "prior": conclusions[dep],
            }
        else:
            conclusions[dep] = {"status": "INVALIDATED", "reason": f"pricing_change:{change_type}"}
        invalidated.append(dep)
    # Line-specific
    if affected_line_ids and "lines" in state:
        new_lines = []
        for li in state["lines"]:
            if li.get("line_id") in affected_line_ids:
                new_lines.append({**li, "status": "INVALIDATED", "reason": change_type})
            else:
                new_lines.append(li)
        state["lines"] = new_lines
    preserved = [k for k in (pricing_state.get("conclusions") or {}) if k not in deps]
    state["conclusions"] = conclusions
    state["last_invalidation"] = {
        "change_type": change_type,
        "invalidated": invalidated,
        "preserved": preserved,
        "affected_line_ids": affected_line_ids or [],
    }
    state["repricing_required"] = True
    return state
