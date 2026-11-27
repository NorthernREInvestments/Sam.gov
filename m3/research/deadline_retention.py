"""48-hour post-close working-inventory retention (permanent SOP).

Actionable queue: remove immediately when deadline passes.
Working record: keep through deadline + 48h unless business activity or amendment.

Longer policy (close + 30 days, change-order watch, bid exception) lives in
`m3.research.change_watch` — soft-archive only; never deletes paid evidence.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from m3.config import get_settings
from m3.research.commercial_criteria import evaluate_deadline_gate

RETENTION_HOURS = 48


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse(ts: Any) -> datetime | None:
    if not ts:
        return None
    s = str(ts).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def retention_state(item: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Compute actionable vs working retention status from verified deadline."""
    now = now or datetime.now(timezone.utc)
    gate = evaluate_deadline_gate(item)
    deadline = _parse(gate.get("deadline_at") or item.get("deadline_at") or item.get("response_deadline"))
    tz = gate.get("timezone") or item.get("deadline_timezone") or "unknown"
    if not deadline:
        return {
            "deadline_known": False,
            "actionable": False,
            "open_status": "OPEN_STATUS_UNVERIFIED",
            "working_retain": True,
            "reason": "deadline_missing_or_unverified_do_not_auto_delete",
            "timezone": tz,
            "gate": gate,
        }
    expired = bool(gate.get("expired")) or deadline <= now
    retain_until = deadline + timedelta(hours=RETENTION_HOURS)
    in_retention = expired and now < retain_until
    past_retention = expired and now >= retain_until
    return {
        "deadline_known": True,
        "deadline_at": deadline.isoformat(),
        "timezone": tz,
        "expired": expired,
        "actionable": (not expired) and not gate.get("expired"),
        "working_retain": (not expired) or in_retention,
        "retain_until": retain_until.isoformat(),
        "past_48h_window": past_retention,
        "open_status": "CLOSED" if expired else ("VERIFIED_OPEN" if gate.get("source") else "OPEN_STATUS_UNVERIFIED"),
        "gate": gate,
        "amendment_can_reopen": True,
    }


def has_business_activity(nb_id: str) -> bool:
    """Preserve forever if quotes/bids/PO/etc exist — fail closed to retain if uncertain."""
    # Lightweight markers; expand when dedicated tables are queried
    root = get_settings().data_root()
    for rel in (
        f"supplier_quotes/{nb_id}.json",
        f"bids/{nb_id}.json",
        f"purchase_orders/{nb_id}.json",
    ):
        if (root / rel).exists():
            return True
    return False


def apply_retention_to_inventory(*, dry_run: bool = False) -> dict[str, Any]:
    """Move past-48h unpursued working rows out of active inventory (not evidence)."""
    inv = get_settings().data_root() / "nextbid_inventory" / "opportunities.jsonl"
    if not inv.exists():
        return {"ok": False, "error": "no_inventory"}
    kept: list[str] = []
    archived: list[dict[str, Any]] = []
    actionable_blocked: list[str] = []
    with inv.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            nb = str(row.get("nb_id") or "")
            st = retention_state(row)
            if not st.get("actionable"):
                actionable_blocked.append(nb)
            if st.get("past_48h_window") and not has_business_activity(nb):
                archived.append({"nb_id": nb, "retain_until": st.get("retain_until"), "title": row.get("title")})
                # keep compact identifier
                continue
            kept.append(json.dumps(row, default=str) + "\n")
    if not dry_run and archived:
        arch = get_settings().data_root() / "nextbid_inventory" / "archived_after_close.jsonl"
        with arch.open("a", encoding="utf-8") as fh:
            for a in archived:
                fh.write(json.dumps({**a, "archived_at": _utc()}, default=str) + "\n")
        # also write minimal id registry
        ids_path = get_settings().data_root() / "nextbid_inventory" / "closed_identity_index.jsonl"
        with ids_path.open("a", encoding="utf-8") as fh:
            for a in archived:
                fh.write(json.dumps({"nb_id": a["nb_id"], "at": _utc()}, default=str) + "\n")
        tmp = inv.with_suffix(".tmp")
        tmp.write_text("".join(kept), encoding="utf-8")
        tmp.replace(inv)
    return {
        "ok": True,
        "archived_after_48h": len(archived),
        "remaining_active": len(kept),
        "non_actionable_count": len(actionable_blocked),
        "dry_run": dry_run,
        "archived_sample": archived[:20],
    }
