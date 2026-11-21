"""Human-readable activity descriptions for the operator dashboard."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

ACTION_LABELS: dict[str, str] = {
    "user.login": "Signed in",
    "user.create": "Created a user account",
    "user.edit": "Updated a team member",
    "user.toggle": "Changed account status",
    "user.password_reset": "Reset a password",
    "user.profile_update": "Updated profile",
    "user.invite_create": "Created an invitation",
    "user.invite_accept": "Accepted an invitation",
    "user.invite_revoke": "Revoked an invitation",
    "user.session_invalidate": "Signed out all sessions",
    "user.remove": "Removed a team member",
    "user.archive": "Archived a team member",
    "user.email_change_pending": "Started an email change",
    "user.email_change_complete": "Confirmed an email change",
    "opportunity.save": "Saved an opportunity",
    "opportunity.update": "Updated an opportunity",
    "opportunity.notes": "Updated opportunity notes",
    "opportunity.status": "Changed opportunity status",
    "opportunity.unsave": "Removed a saved opportunity",
    "opportunity.assign": "Assigned an opportunity",
    "supplier.create": "Added a supplier",
    "supplier.update": "Updated a supplier",
    "supplier.company_contact": "Updated supplier company contact",
    "supplier.contact_add": "Added a supplier contact",
    "supplier.note": "Added a supplier note",
    "task.create": "Created a task",
    "task.done": "Completed a task",
    "research.contracts": "Searched for contracts",
    "research.suppliers": "Searched for suppliers",
    "cleanup.opportunity_archive": "Archived test opportunity data",
    "cleanup.supplier_deactivate": "Deactivated test supplier data",
    "cleanup.user_deactivate": "Deactivated a test account",
    "catalog.ingest": "Uploaded a catalog",
}


def action_label(action: str | None) -> str:
    if not action:
        return "Activity"
    if action in ACTION_LABELS:
        return ACTION_LABELS[action]
    # research.* fallbacks
    if action.startswith("research."):
        return "Ran research"
    parts = str(action).replace(".", " ").replace("_", " ").strip()
    return parts[:1].upper() + parts[1:] if parts else "Activity"


def format_activity_time(at: Any) -> str:
    if not at:
        return "Unknown time"
    try:
        if isinstance(at, datetime):
            dt = at
        else:
            dt = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        # Present in local-looking wall clock without inventing a named zone
        local = dt.astimezone()
        return local.strftime("%b %d, %Y at %I:%M %p").replace(" 0", " ")
    except Exception:
        return str(at)


def enrich_activity_events(events: list[Any], users_by_id: dict[Any, Any] | None = None) -> list[dict[str, Any]]:
    users_by_id = users_by_id or {}
    out = []
    for e in events:
        actor_name = None
        uid = getattr(e, "actor_user_id", None)
        if uid and uid in users_by_id:
            actor_name = getattr(users_by_id[uid], "full_name", None)
        label = getattr(e, "actor_label", "") or ""
        if not actor_name:
            if label and not label.startswith("system:") and label.lower() not in {"user", "unknown"}:
                # Prefer email local-part only if it looks like a person label already
                actor_name = label
            elif label.startswith("system:"):
                actor_name = "System"
            else:
                actor_name = "Unknown user"
        out.append(
            {
                "action_label": action_label(getattr(e, "action", None)),
                "action_raw": getattr(e, "action", None),
                "actor_name": actor_name,
                "at_human": format_activity_time(getattr(e, "at", None)),
                "entity_type": getattr(e, "entity_type", None),
                "entity_id": getattr(e, "entity_id", None),
            }
        )
    return out
