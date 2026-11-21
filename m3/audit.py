"""Append-only audit trail. Never stores secrets."""

from __future__ import annotations

import re
import uuid
from typing import Any

from sqlalchemy.orm import Session

from m3.models import AuditEvent

_SECRET_KEYS = re.compile(r"(password|secret|token|api_key|authorization|credit_card)", re.I)


def _scrub(obj: Any) -> Any:
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if _SECRET_KEYS.search(str(k)):
                out[k] = "[redacted]"
            else:
                out[k] = _scrub(v)
        return out
    if isinstance(obj, list):
        return [_scrub(x) for x in obj]
    return obj


def record_audit(
    db: Session,
    *,
    action: str,
    entity_type: str,
    entity_id: str,
    actor_user_id: uuid.UUID | None = None,
    actor_label: str = "system",
    before: dict | None = None,
    after: dict | None = None,
    origin: str = "manual",
    research_run_id: uuid.UUID | None = None,
    meta: dict | None = None,
) -> AuditEvent:
    ev = AuditEvent(
        actor_user_id=actor_user_id,
        actor_label=actor_label[:128],
        action=action[:64],
        entity_type=entity_type[:64],
        entity_id=str(entity_id)[:128],
        before=_scrub(before) if before else None,
        after=_scrub(after) if after else None,
        origin=origin,
        research_run_id=research_run_id,
        meta=_scrub(meta or {}),
    )
    db.add(ev)
    db.flush()
    return ev
