"""Operator supplier outreach queue — review/copy/mark-sent/response tracking.

Never auto-sends. Opening or copying an RFQ does NOT mark it sent.
"""

from __future__ import annotations

from typing import Any

from buyer_intelligence.commercial_intelligence_upgrade_v1 import _load, _save, _utc

BUILD = "20261012-m3-supplier-outreach-queue-v1"
OUT = "_og_commercial_research_pipeline_out"
QUEUE_REL = f"{OUT}/m3_supplier_outreach_queue_v1.json"

VALID_STATUSES = {
    "READY_FOR_REVIEW",
    "COPIED",  # not sent
    "CONTACT_OPENED",  # not sent
    "SENT",
    "RESPONSE_RECEIVED",
    "QUOTE_VERIFIED",
    "DECLINED",
    "NO_RESPONSE",
    "ALTERNATE_RESEARCH_REQUESTED",
}


def load_queue() -> dict[str, Any]:
    blob = _load(QUEUE_REL)
    if not isinstance(blob, dict):
        blob = {"kind": "supplier_outreach_queue_v1", "build": BUILD, "items": {}}
    blob.setdefault("items", {})
    return blob


def save_queue(blob: dict[str, Any]) -> None:
    blob["build"] = BUILD
    blob["updated_at"] = _utc()
    _save(QUEUE_REL, blob)


def sync_from_rfqs() -> dict[str, Any]:
    rfqs = _load(f"{OUT}/m3_cex_v1_rfqs.json") or {}
    q = load_queue()
    for pkt in rfqs.get("PACKETS") or []:
        oid = str(pkt.get("OPPORTUNITY_ID") or "")
        if not oid:
            continue
        existing = q["items"].get(oid) or {}
        if existing.get("operator_status") in {"SENT", "RESPONSE_RECEIVED", "QUOTE_VERIFIED", "DECLINED", "NO_RESPONSE"}:
            # Preserve operator progress; refresh packet body only
            existing["packet"] = pkt
            existing["updated_at"] = _utc()
            q["items"][oid] = existing
            continue
        q["items"][oid] = {
            "opportunity_id": oid,
            "operator_status": existing.get("operator_status") or "READY_FOR_REVIEW",
            "packet": pkt,
            "supplier": pkt.get("PRIMARY_SUPPLIER"),
            "contact_url": pkt.get("SUPPLIER_CONTACT_URL"),
            "sent_at": existing.get("sent_at"),
            "response": existing.get("response"),
            "verified_quote": existing.get("verified_quote"),
            "events": existing.get("events") or [],
            "updated_at": _utc(),
        }
    save_queue(q)
    return q


def operator_action(
    opportunity_id: str,
    action: str,
    *,
    note: str | None = None,
    response_text: str | None = None,
    verified_quote: dict[str, Any] | None = None,
) -> dict[str, Any]:
    q = load_queue()
    item = q["items"].get(opportunity_id)
    if not item:
        raise KeyError(f"No outreach item for {opportunity_id}")
    action_u = action.strip().upper()
    event = {"at": _utc(), "action": action_u, "note": note}
    item.setdefault("events", []).append(event)

    if action_u in {"REVIEW", "COPY_RFQ"}:
        # Explicitly NOT sent
        item["operator_status"] = "COPIED" if action_u == "COPY_RFQ" else item.get("operator_status") or "READY_FOR_REVIEW"
        item["NOTE"] = "Copy/review does not mark sent"
    elif action_u in {"OPEN_CONTACT"}:
        item["operator_status"] = "CONTACT_OPENED"
        item["NOTE"] = "Opening contact page does not mark sent"
    elif action_u in {"MARK_SENT", "SENT"}:
        item["operator_status"] = "SENT"
        item["sent_at"] = _utc()
    elif action_u in {"ENTER_RESPONSE", "RESPONSE"}:
        item["operator_status"] = "RESPONSE_RECEIVED"
        item["response"] = response_text or note
    elif action_u in {"ENTER_QUOTE", "QUOTE_VERIFIED"}:
        item["operator_status"] = "QUOTE_VERIFIED"
        item["verified_quote"] = verified_quote or {"note": note}
    elif action_u in {"DECLINED"}:
        item["operator_status"] = "DECLINED"
    elif action_u in {"NO_RESPONSE"}:
        item["operator_status"] = "NO_RESPONSE"
    elif action_u in {"REQUEST_ALTERNATE", "ALTERNATE_RESEARCH"}:
        item["operator_status"] = "ALTERNATE_RESEARCH_REQUESTED"
    else:
        raise ValueError(f"Unknown action: {action}")

    item["updated_at"] = _utc()
    q["items"][opportunity_id] = item
    save_queue(q)
    return item


def dashboard() -> dict[str, Any]:
    q = load_queue()
    items = list((q.get("items") or {}).values())
    by_status: dict[str, int] = {}
    for it in items:
        st = str(it.get("operator_status") or "UNKNOWN")
        by_status[st] = by_status.get(st, 0) + 1
    return {
        "kind": "supplier_outreach_dashboard",
        "build": BUILD,
        "count": len(items),
        "by_status": by_status,
        "auto_send": False,
        "items": items,
    }
