"""API/UI helpers for national discovery tracking & change alerts."""

from __future__ import annotations

from typing import Any

from tracked_solicitation import TrackedSolicitationStore

# Process-local store for development/validation; production may swap persistence later.
_STORE = TrackedSolicitationStore()


def get_tracked_store() -> TrackedSolicitationStore:
    return _STORE


def set_tracked_store(store: TrackedSolicitationStore) -> None:
    global _STORE
    _STORE = store


def changes_requiring_review_payload() -> dict[str, Any]:
    store = get_tracked_store()
    data = store.changes_requiring_review()
    enriched = []
    for c in data.get("changes") or []:
        ui = store.ui_severity_payload(c)
        enriched.append({**c, "ui": ui})
    return {
        "count": data["count"],
        "label": data["label"],
        "changes": enriched,
        "nav_indicator": {
            "text": data["label"],
            "visible": data["count"] > 0,
            "css_class": "nav-changes-review",
            "icon": "bell-alert",
        },
    }


def contract_change_alert_fields(notice_id: str) -> dict[str, Any]:
    """Fields to merge into contract card payloads."""
    store = get_tracked_store()
    unreviewed = store.unreviewed_for(notice_id)
    if not unreviewed:
        tracked = store.get(notice_id)
        if tracked:
            return {
                "tracked_solicitation": True,
                "monitoring_tier": tracked.get("monitoring_tier"),
                "tracked_change_alert": None,
            }
        return {}
    top = unreviewed[0]
    ui = store.ui_severity_payload(top)
    return {
        "tracked_solicitation": True,
        "tracked_change_alert": {
            "change_id": top["change_id"],
            "severity": top["severity"],
            "change_type": top["change_type"],
            "badge_text": ui["badge_text"],
            "icon": ui["icon"],
            "css_class": ui["css_class"],
            "border_class": ui["border_class"],
            "aria_label": ui["aria_label"],
            "color_token": ui["color_token"],
            "text_label_required": True,
            "operator_reviewed": False,
        },
        "unreviewed_change_count": len(unreviewed),
    }
