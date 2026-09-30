"""Clean-room firewall — internal evidence must not become submission content."""

from __future__ import annotations

from typing import Any

from response_engine.constants import NS_GOVERNMENT_SUBMISSION, NS_INTERNAL_EVIDENCE
from response_engine.models import assert_not_internal_in_submission, store_internal_evidence


INTERNAL_ONLY_KEYS = frozenset({
    "max_buy", "margin", "target_profit", "expected_profit", "expected_net_profit",
    "financing", "financing_cost", "financing_assumptions", "subject_to_change",
    "cancellation_fee", "supplier_boilerplate", "internal_notes", "call_notes",
    "priority_score", "deal_priority_score", "gov_grade", "supplier_grade",
    "internal_max_buy", "supplier_quote_target", "thresholds",
    "historical_government_price", "gov_historical", "historical_paid",
    "financing_ceiling", "target_margin", "markup",
})


def ingest_supplier_quote_as_internal(project: dict[str, Any], quote: dict[str, Any]) -> dict[str, Any]:
    """Supplier quotes stay INTERNAL — including 'pricing subject to change'."""
    safe = {
        "supplier": quote.get("supplier"),
        "unit_price": quote.get("unit_price"),
        "freight": quote.get("freight"),
        "lead_time": quote.get("lead_time"),
        "terms": quote.get("terms"),
        "quote_expiration": quote.get("quote_expiration"),
        "raw_flags": [],
    }
    blob = str(quote).lower()
    if "subject to change" in blob:
        safe["raw_flags"].append("PRICING_SUBJECT_TO_CHANGE")
    if "cancellation" in blob:
        safe["raw_flags"].append("CANCELLATION_TERMS_PRESENT")
    row = store_internal_evidence(project, {"type": "supplier_quote", **safe, "original_keys": list(quote.keys())})
    # Explicitly refuse auto-promotion
    row["may_enter_submission"] = False
    row["promotion_requires"] = "explicit_owner_or_response_engine_decision"
    return row


def attempt_promote_supplier_document(
    project: dict[str, Any],
    evidence_id: str,
    *,
    owner_authorized: bool = False,
) -> dict[str, Any]:
    if not owner_authorized:
        return {
            "ok": False,
            "error": "Supplier/internal documents cannot become government submission content without explicit authorization.",
        }
    internal = (project.get("evidence_namespaces") or {}).get(NS_INTERNAL_EVIDENCE) or []
    row = next((r for r in internal if r.get("evidence_id") == evidence_id), None)
    if not row:
        return {"ok": False, "error": "Evidence not found in INTERNAL namespace."}
    # Still strip forbidden fields
    payload = {k: v for k, v in (row.get("payload") or {}).items() if k not in INTERNAL_ONLY_KEYS}
    project.setdefault("evidence_namespaces", {}).setdefault(NS_GOVERNMENT_SUBMISSION, []).append(
        {
            "evidence_id": evidence_id + "-SUB",
            "namespace": NS_GOVERNMENT_SUBMISSION,
            "payload": payload,
            "promoted_from": evidence_id,
            "owner_authorized": True,
        }
    )
    leaks = assert_not_internal_in_submission(project)
    return {"ok": len(leaks) == 0, "leaks": leaks}


def firewall_report(project: dict[str, Any]) -> dict[str, Any]:
    leaks = assert_not_internal_in_submission(project)
    internal = (project.get("evidence_namespaces") or {}).get(NS_INTERNAL_EVIDENCE) or []
    submission = (project.get("evidence_namespaces") or {}).get(NS_GOVERNMENT_SUBMISSION) or []
    return {
        "internal_evidence_count": len(internal),
        "submission_content_count": len(submission),
        "leaks": leaks,
        "clean": len(leaks) == 0,
        "supplier_quotes_blocked_from_auto_attach": True,
    }
