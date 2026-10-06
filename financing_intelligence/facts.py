"""Human approval gate for extracted financing facts."""

from __future__ import annotations

from typing import Any

from financing_intelligence.constants import BUILD, FACT_APPROVED, FACT_PROPOSED, FACT_REJECTED, FACT_SUPERSEDED
from financing_intelligence.sources import get_source, upsert_source
from financing_intelligence.store import load_facts, save_facts, _utc


def list_facts(*, status: str | None = None, source_id: str | None = None) -> list[dict[str, Any]]:
    items = list(load_facts().get("items") or [])
    if status:
        items = [i for i in items if i.get("status") == status]
    if source_id:
        items = [i for i in items if i.get("source_id") == source_id]
    return items


def decide_fact(
    fact_id: str,
    *,
    decision: str,
    edited_value: Any | None = None,
    decided_by: str = "operator",
) -> dict[str, Any]:
    """Approve / edit / reject. Only APPROVED facts may affect opportunity scoring."""
    doc = load_facts()
    found = None
    for f in doc.get("items") or []:
        if f.get("fact_id") == fact_id:
            found = f
            break
    if not found:
        return {"ok": False, "error": "fact_not_found"}
    if found.get("status") not in {FACT_PROPOSED, FACT_APPROVED}:
        return {"ok": False, "error": "fact_not_decidable", "status": found.get("status")}

    decision_u = (decision or "").upper()
    if decision_u in {"APPROVE", "APPROVED", FACT_APPROVED}:
        if edited_value is not None:
            found["value"] = edited_value
        found["status"] = FACT_APPROVED
        found["decided_at"] = _utc()
        found["decided_by"] = decided_by
        # Push into source.terms only when approved
        sid = found.get("source_id")
        if sid and get_source(sid):
            upsert_source({"source_id": sid, "terms": {found["field"]: found["value"]}})
        elif sid:
            # source may not exist yet — still keep approved fact for later merge
            pass
    elif decision_u in {"REJECT", "REJECTED", FACT_REJECTED}:
        found["status"] = FACT_REJECTED
        found["decided_at"] = _utc()
        found["decided_by"] = decided_by
    elif decision_u in {"EDIT", "EDIT_APPROVE"}:
        if edited_value is None:
            return {"ok": False, "error": "edited_value_required"}
        # supersede prior approved same field
        for other in doc.get("items") or []:
            if (
                other.get("fact_id") != fact_id
                and other.get("source_id") == found.get("source_id")
                and other.get("field") == found.get("field")
                and other.get("status") == FACT_APPROVED
            ):
                other["status"] = FACT_SUPERSEDED
                other["superseded_at"] = _utc()
        found["value"] = edited_value
        found["status"] = FACT_APPROVED
        found["decided_at"] = _utc()
        found["decided_by"] = decided_by
        sid = found.get("source_id")
        if sid and get_source(sid):
            upsert_source({"source_id": sid, "terms": {found["field"]: found["value"]}})
    else:
        return {"ok": False, "error": "invalid_decision"}

    found["build"] = BUILD
    save_facts(doc)
    return {"ok": True, "fact": found, "affects_scoring": found.get("status") == FACT_APPROVED}
