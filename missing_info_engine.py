"""Orchestrate missing-info local search + CO gate + persistence (zero external calls)."""

from __future__ import annotations
from application_clock import now_utc

from datetime import datetime, timezone
from typing import Any

from co_clarification import draft_co_question, evaluate_co_clarification_gate
from local_package_search import collect_local_corpus, exhaustive_local_search
from missing_info import (
    EXTERNAL_REFERENCE_REQUIRED,
    FACT_COMMERCIAL,
    FACT_FINANCING,
    FACT_MANUFACTURER,
    FACT_SOLICITATION,
    MATCH_DIRECT,
    MATCH_NONE,
    MATCH_POSSIBLE_INDIRECT,
    MATCH_RELATED,
    MISSING_CONFIRMED_LOCAL,
    MISSING_UNCHECKED,
    POSSIBLE_MATCH_FOUND,
    RESOLVED,
    SEARCHING_LOCAL_PACKAGE,
    SECOND_PASS_PENDING,
    classify_fact_class,
    missing_info_record,
)
from solicitation_package import evaluate_solicitation_package


def default_critical_facts_for_product_resale(
    *,
    bom: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Generic product-resale critical facts — derived from BOM gaps when present."""
    facts: list[dict[str, Any]] = []
    bom = bom or []
    unknown_components = {
        str(i.get("component") or ""): i
        for i in bom
        if str(i.get("status") or "").upper() == "UNKNOWN"
    }
    if "memory_module_quantity_per_server" in unknown_components or "memory_module" in unknown_components:
        facts.append(
            missing_info_record(
                fact_key="memory_module_quantity_per_server",
                description="Required memory module quantity per unit/server",
                fact_class=FACT_SOLICITATION,
                necessary_for_bid=True,
                necessary_for_execution=True,
            )
        )
    if "storage_drive" in unknown_components or "storage_drive_quantity" in unknown_components:
        facts.append(
            missing_info_record(
                fact_key="storage_drive_quantity",
                description="Required storage drive quantity per unit/server",
                fact_class=FACT_SOLICITATION,
                necessary_for_bid=True,
                necessary_for_execution=True,
            )
        )
    facts.extend(
        [
            missing_info_record(
                fact_key="installation_requirement",
                description="Whether installation/setup services are required under the solicitation",
                fact_class=FACT_SOLICITATION,
                necessary_for_bid=True,
                necessary_for_execution=True,
            ),
            missing_info_record(
                fact_key="freight_fob_responsibility",
                description="FOB / freight / shipping responsibility and cost allocation",
                fact_class=FACT_SOLICITATION,
                necessary_for_bid=True,
                necessary_for_execution=True,
            ),
            missing_info_record(
                fact_key="supplier_acquisition_price",
                description="Current supplier acquisition price for exact BOM",
                fact_class=FACT_COMMERCIAL,
                necessary_for_bid=True,
                necessary_for_execution=True,
            ),
            missing_info_record(
                fact_key="financing_pg_requirement",
                description="Whether financing requires personal guarantee",
                fact_class=FACT_FINANCING,
                necessary_for_bid=False,
                necessary_for_execution=True,
            ),
        ]
    )
    return facts


def default_critical_facts_for_opp199() -> list[dict[str, Any]]:
    """Regression alias — delegates to fixture."""
    from fixtures.opp199 import opp199_critical_facts

    return opp199_critical_facts()


def _verified_contexts_from_deal(
    *,
    bom: list[dict[str, Any]] | None = None,
    checkpoint: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Operator/CO draft context from verified BOM lines — never invented product facts."""
    from deal_context import destination_from_checkpoint, primary_product_label, required_quantity_from_bom

    product = primary_product_label(bom) or "configured product"
    qty = required_quantity_from_bom(bom)
    dest = destination_from_checkpoint(checkpoint) or "solicitation delivery destination"
    qty_label = f"quantity {qty}" if qty else "stated quantity"
    return {
        "memory_module_quantity_per_server": f"Configuration identifies memory modules for {product}",
        "storage_drive_quantity": f"Configuration references storage for {product}",
        "installation_requirement": f"Product resale RFQ for {product} ({qty_label})",
        "freight_fob_responsibility": f"Delivery required to {dest}",
    }


def process_missing_fact(
    *,
    fact: dict[str, Any],
    corpus: list[dict[str, Any]],
    package_status: str,
    already_answered: bool = False,
    clarification_deadline: Any = None,
    verified_context: str | None = None,
) -> dict[str, Any]:
    """Run exhaustive local search + CO gate for one fact. No external calls."""
    fact = dict(fact)
    fact["status"] = SEARCHING_LOCAL_PACKAGE
    fact_class = fact.get("fact_class") or classify_fact_class(fact["fact_key"])

    # Non-solicitation facts: skip exhaustive CO path; still may search for operator context
    audit = exhaustive_local_search(
        fact_key=fact["fact_key"],
        description=fact.get("description") or fact["fact_key"],
        corpus=corpus,
        extra_terms=None,
    )
    fact["search_audit"] = audit

    overall = audit.get("overall_match_class")
    if overall in {MATCH_DIRECT, MATCH_RELATED, MATCH_POSSIBLE_INDIRECT}:
        fact["status"] = POSSIBLE_MATCH_FOUND
    elif overall == MATCH_NONE and audit.get("exhaustive_local"):
        fact["status"] = MISSING_CONFIRMED_LOCAL
        fact["second_pass_status"] = SECOND_PASS_PENDING
    else:
        fact["status"] = MISSING_CONFIRMED_LOCAL if corpus else MISSING_UNCHECKED

    gate = evaluate_co_clarification_gate(
        fact_class=fact_class,
        necessary_for_bid=bool(fact.get("necessary_for_bid")),
        necessary_for_execution=bool(fact.get("necessary_for_execution")),
        search_audit=audit,
        package_status=package_status,
        already_answered=already_answered or bool(fact.get("already_answered")),
        clarification_deadline=clarification_deadline,
    )
    # Align status with gate when gate is more specific
    if gate.get("status") == EXTERNAL_REFERENCE_REQUIRED:
        fact["status"] = EXTERNAL_REFERENCE_REQUIRED
    elif gate.get("status") == POSSIBLE_MATCH_FOUND:
        fact["status"] = POSSIBLE_MATCH_FOUND
    elif gate.get("safe_to_ask_co"):
        fact["status"] = gate["status"]

    fact["co_gate"] = gate
    fact["confidence_absent"] = gate.get("confidence_absent")
    fact["safe_to_ask_co"] = bool(gate.get("safe_to_ask_co"))

    # Draft only when candidate/safe — still operator must review
    checked_names = []
    for d in audit.get("documents_checked") or []:
        if d.get("checked"):
            label = d.get("filename") or d.get("document_type") or d.get("doc_id")
            if label:
                checked_names.append(str(label))
    fact["question_draft"] = draft_co_question(
        verified_context=verified_context or "",
        missing_item=fact.get("description") or fact["fact_key"],
        documents_actually_checked=checked_names,
    )
    return fact


def run_missing_info_pass(
    session: Any,
    contract_id: int,
    *,
    facts: list[dict[str, Any]] | None = None,
    persist: bool = True,
    package_status_override: str | None = None,
) -> dict[str, Any]:
    from models import MissingInfoItem, SolicitationDocument
    from solicitation_package import document_record

    corpus = collect_local_corpus(session, contract_id)
    docs = session.query(SolicitationDocument).filter_by(contract_id=contract_id).all()
    doc_dicts = [
        document_record(
            document_type=d.document_type,
            filename=d.filename,
            current=bool(d.current),
            superseded=bool(d.superseded),
        )
        for d in docs
    ]
    pkg = evaluate_solicitation_package(doc_dicts)
    package_status = package_status_override or pkg.get("status")

    from models import DealState

    deal = session.query(DealState).filter_by(contract_id=contract_id).first()
    checkpoint = (deal.funnel_checkpoint_json if deal else None) or {}
    bom = checkpoint.get("bom") if isinstance(checkpoint.get("bom"), list) else []
    items_in = facts or default_critical_facts_for_product_resale(bom=bom)
    results = []
    contexts = _verified_contexts_from_deal(bom=bom, checkpoint=checkpoint)
    for fact in items_in:
        processed = process_missing_fact(
            fact=fact,
            corpus=corpus,
            package_status=package_status,
            verified_context=contexts.get(fact.get("fact_key")),
        )
        results.append(processed)
        if persist:
            row = (
                session.query(MissingInfoItem)
                .filter_by(contract_id=contract_id, fact_key=processed["fact_key"])
                .first()
            )
            if row is None:
                row = MissingInfoItem(contract_id=contract_id, fact_key=processed["fact_key"])
                session.add(row)
            row.description = processed.get("description")
            row.fact_class = processed.get("fact_class")
            row.status = processed.get("status")
            row.search_audit_json = processed.get("search_audit")
            row.co_gate_json = processed.get("co_gate")
            row.question_draft_json = processed.get("question_draft")
            row.second_pass_status = processed.get("second_pass_status")
            row.safe_to_ask_co = bool(processed.get("safe_to_ask_co"))
            row.confidence_absent = processed.get("confidence_absent")
            row.necessary_for_bid = bool(processed.get("necessary_for_bid"))
            row.necessary_for_execution = bool(processed.get("necessary_for_execution"))
            row.updated_at = now_utc()
    session.flush()
    return {
        "LIVE_API_REQUESTS": 0,
        "contract_id": contract_id,
        "package_status": package_status,
        "corpus_document_count": len(corpus),
        "items": results,
        "external_calls": {
            "SAM": 0,
            "OpenAI": 0,
            "web": 0,
            "USAspending": 0,
            "supplier": 0,
            "financing": 0,
            "Quo": 0,
        },
    }
