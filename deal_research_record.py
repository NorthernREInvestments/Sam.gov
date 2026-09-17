"""DealResearchRecord — durable results with references, not bulk archives."""

from __future__ import annotations

from typing import Any

from application_clock import now_utc
from persistence_policy import KIND_DEAL_RECORD, persistence_decision


def deal_research_record(
    *,
    opportunity_id: str | None = None,
    solicitation_number: str | None = None,
    agency: str | None = None,
    buyer: str | None = None,
    source: str | None = None,
    source_url: str | None = None,
    deadline: str | None = None,
    document_manifest_summary: list[dict[str, Any]] | None = None,
    controlling_document_refs: list[dict[str, Any]] | None = None,
    requirement_summary: dict[str, Any] | None = None,
    bom: Any = None,
    supplier_candidates: list[dict[str, Any]] | None = None,
    supplier_quote_status: str | None = None,
    economics: dict[str, Any] | None = None,
    funding_requirement: dict[str, Any] | None = None,
    funding_status: str | None = None,
    compliance_blockers: list[str] | None = None,
    portal_blockers: list[str] | None = None,
    operator_next_action: str | None = None,
    notes: str | None = None,
    provenance: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "kind": "DealResearchRecord",
        "opportunity_id": opportunity_id,
        "solicitation_number": solicitation_number,
        "agency": agency,
        "buyer": buyer,
        "source": source,
        "source_url_reference": source_url,
        "retrieved_at": now_utc().isoformat(),
        "deadline": deadline,
        "document_manifest_summary": list(document_manifest_summary or []),
        "controlling_document_references": list(controlling_document_refs or []),
        "requirement_summary": requirement_summary or {},
        "bom": bom,
        "supplier_candidates": list(supplier_candidates or []),
        "supplier_quote_status": supplier_quote_status,
        "economics": economics or {},
        "funding_requirement": funding_requirement or {},
        "funding_status": funding_status,
        "compliance_blockers": list(compliance_blockers or []),
        "portal_blockers": list(portal_blockers or []),
        "operator_next_action": operator_next_action,
        "notes": notes,
        "provenance": list(provenance or []),
        "last_refreshed": now_utc().isoformat(),
        "persistence": persistence_decision(KIND_DEAL_RECORD),
        "bulk_documents_embedded": False,
    }
