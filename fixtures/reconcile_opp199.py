"""Opportunity 199 regression reconcile — fixture-only Opp199-specific content."""

from __future__ import annotations
from application_clock import now_utc

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bid_package import empty_bid_package
from bom_gate import evaluate_bom_completeness
from company_profile import ensure_default_company_profile
from fixtures.opp199 import (
    OPP199_DELIVERY,
    OPP199_DESTINATION,
    OPP199_ID,
    OPP199_NOTICE,
    OPP199_SOLICITATION,
    OPP199_SUPPLIER_NAMES,
    seed_opp199_commercial_artifacts,
)
from quote_validation import default_negotiation_suggestions
from requirement_register import REQ_UNKNOWN, REQ_VERIFIED_REQUIREMENT, requirement_record

ROOT = Path(__file__).resolve().parent.parent
STAGE3_REPORT = ROOT / "_opp199_stage3_report.json"
COMPLETENESS_REPORT = ROOT / "_opp199_document_completeness_report.json"


def reconcile_opportunity_199_workspace(session: Any) -> dict[str, Any]:
    """Populate Opp 199 workspace from local Stage 3 / attachments only. Zero external calls."""
    from deal_workspace import build_workspace_snapshot
    from models import (
        BidPackage,
        Contract,
        ContractAttachment,
        DealState,
        FinancingProvider,
        FinancingPursuit,
        KnowledgeSupplier,
        MissingInfoItem,
        RequirementRegisterItem,
        SolicitationDocument,
        SupplierOffer,
        SupplierPursuitPlan,
    )
    from missing_info import CO_CLARIFICATION_CANDIDATE
    from missing_info_engine import run_missing_info_pass
    from sqlalchemy.orm.attributes import flag_modified

    ensure_default_company_profile(session)
    contract = session.query(Contract).filter_by(id=OPP199_ID).first()
    if not contract:
        contract = session.query(Contract).filter_by(notice_id=OPP199_NOTICE).first()
    if not contract:
        return {"ok": False, "error": "opportunity_199_not_found", "LIVE_API_REQUESTS": 0}

    report: dict[str, Any] = {}
    if STAGE3_REPORT.exists():
        try:
            report = json.loads(STAGE3_REPORT.read_text(encoding="utf-8"))
        except Exception:
            report = {}

    bom = report.get("bom") if isinstance(report.get("bom"), list) else []
    deal = session.query(DealState).filter_by(contract_id=contract.id).first()
    checkpoint = dict(deal.funnel_checkpoint_json or {}) if deal else {}
    existing_bom = checkpoint.get("bom") if isinstance(checkpoint.get("bom"), list) else []
    if existing_bom and evaluate_bom_completeness(existing_bom).get("supplier_quote_request_ready"):
        bom = existing_bom
    elif bom:
        checkpoint["bom"] = bom
    else:
        bom = existing_bom or bom
        if bom:
            checkpoint["bom"] = bom

    checkpoint["stage3"] = {
        "channel": report.get("channel"),
        "delivery": report.get("delivery"),
        "freight": report.get("freight"),
        "compliance": report.get("compliance"),
        "financing": report.get("financing"),
        "acquisition_cost": report.get("acquisition_cost"),
        "next_state": report.get("next_state"),
    }
    checkpoint["channel_status"] = (report.get("channel") or {}).get("status") or "CHANNEL_UNRESOLVED"
    checkpoint["delivery_status"] = (report.get("delivery") or {}).get("status") or "DELIVERY_UNRESOLVED"

    if deal is None:
        from knowledge_store import upsert_deal_state

        deal = upsert_deal_state(
            session,
            contract.id,
            {
                "core_fit": "CORE_PRODUCT",
                "pipeline_stage": "needs_research",
                "decision": "NEEDS_RESEARCH",
                "funnel_checkpoint": checkpoint,
                "economics": report.get("economics") or {},
                "financing_gate": report.get("financing_gate") or {},
                "reason_codes": ["STAGE3_NEEDS_QUOTES", "DEAL_WORKSPACE_SEEDED"],
            },
        )
    else:
        deal.funnel_checkpoint_json = checkpoint
        if report.get("economics"):
            deal.economics_json = report["economics"]
        deal.core_fit = deal.core_fit or "CORE_PRODUCT"

    if session.query(SolicitationDocument).filter_by(contract_id=contract.id).count() == 0:
        for a in session.query(ContractAttachment).filter_by(contract_id=contract.id).all():
            h = hashlib.sha256(a.file_bytes).hexdigest() if a.file_bytes else None
            dtype = "rfq_rfp_ifb" if a.filename and "RFQ" in a.filename.upper() else "attachment"
            session.add(
                SolicitationDocument(
                    contract_id=contract.id,
                    document_type=dtype,
                    source=a.source or "local_attachment",
                    filename=a.filename,
                    content_hash=h,
                    text_extraction_status="EXTRACTED" if a.extracted_text else "UNKNOWN",
                    superseded=False,
                    current=True,
                    local_attachment_id=a.id,
                    retrieved_at=a.downloaded_at or now_utc(),
                )
            )
        session.add(
            SolicitationDocument(
                contract_id=contract.id,
                document_type="notice",
                source="gt_contracts_local",
                url=contract.link,
                text_extraction_status="N/A",
                current=True,
                required_for_bid=True,
                retrieved_at=now_utc(),
            )
        )

    seeds = _opp199_requirement_seeds()
    for seed in seeds:
        row = (
            session.query(RequirementRegisterItem)
            .filter_by(contract_id=contract.id, requirement_key=seed["requirement_key"])
            .first()
        )
        if row is None:
            row = RequirementRegisterItem(
                contract_id=contract.id,
                requirement_key=seed["requirement_key"],
                description=seed["description"],
                requirement_type=seed["type"],
            )
            session.add(row)
        row.description = seed["description"]
        row.requirement_type = seed["type"]
        row.required = seed["required"]
        row.status = seed["status"]
        row.source_document = seed.get("source_document")
        row.evidence = seed.get("evidence")
        row.verified_status = seed.get("verified_status")
        row.notes = seed.get("notes")

    supplier_names = [
        s["supplier"]
        for s in (report.get("suppliers") or [])
        if isinstance(s, dict) and s.get("supplier")
    ] or list(OPP199_SUPPLIER_NAMES)
    bom_gate = evaluate_bom_completeness(bom)
    quote_ready = bool(bom_gate.get("supplier_quote_request_ready"))
    qty = 14

    for idx, name in enumerate(supplier_names):
        sup = session.query(KnowledgeSupplier).filter(KnowledgeSupplier.name == name).first()
        if sup is None:
            sup = (
                session.query(KnowledgeSupplier)
                .filter(KnowledgeSupplier.name.ilike(f"%{name.split()[0]}%"))
                .first()
            )
        if sup is None:
            continue
        plan = (
            session.query(SupplierPursuitPlan)
            .filter_by(contract_id=contract.id, supplier_id=sup.id)
            .first()
        )
        if plan is None:
            plan = SupplierPursuitPlan(contract_id=contract.id, supplier_id=sup.id)
            session.add(plan)
        plan.priority = idx + 1
        plan.contact_objective = "Obtain firm quote for exact RFQ BOM + OEM authorization letter"
        plan.required_bom_json = bom
        plan.quantity = qty
        plan.destination = OPP199_DESTINATION
        plan.delivery_deadline = OPP199_DELIVERY
        plan.authorization_requirement = "OEM/authorized reseller letter naming quoting company"
        plan.quote_request_ready = quote_ready
        plan.negotiation_suggestions_json = default_negotiation_suggestions()

    fin = report.get("financing") or {}
    for prov in fin.get("providers") or []:
        pname = prov.get("provider")
        if not pname:
            continue
        fp = session.query(FinancingProvider).filter(FinancingProvider.name == pname).first()
        if fp is None:
            continue
        existing = (
            session.query(FinancingPursuit)
            .filter_by(contract_id=contract.id, provider_id=fp.id)
            .first()
        )
        if existing is None:
            existing = FinancingPursuit(contract_id=contract.id, provider_id=fp.id)
            session.add(existing)
        existing.approval_status = "FINANCING_UNRESOLVED"
        existing.verification_status = "UNKNOWN"

    bp = session.query(BidPackage).filter_by(contract_id=contract.id).first()
    if bp is None:
        bp = BidPackage(contract_id=contract.id)
        session.add(bp)
    bp.package_json = empty_bid_package(
        solicitation_number=OPP199_SOLICITATION,
        notice_id=contract.notice_id,
        contract_id=contract.id,
    )
    bp.readiness_status = "BID_NOT_READY"

    pkg_status = (checkpoint.get("package_completeness") or {}).get("status")
    if pkg_status == "SOLICITATION_PACKAGE_COMPLETE":
        missing_result = {"items": [], "skipped": True}
        _restore_fob_from_completeness_report(session, contract.id)
    else:
        missing_result = run_missing_info_pass(session, contract.id, persist=True)

    fob = (
        session.query(MissingInfoItem)
        .filter_by(contract_id=contract.id, fact_key="freight_fob_responsibility")
        .first()
    )
    supplier_seed = _supplier_seed(session, contract.id)
    draft = None
    if fob and isinstance(fob.question_draft_json, dict):
        draft = fob.question_draft_json.get("draft")

    deal = session.query(DealState).filter_by(contract_id=contract.id).first()
    checkpoint = dict(deal.funnel_checkpoint_json or {})
    commercial_seed = seed_opp199_commercial_artifacts(
        bom=checkpoint.get("bom") or bom,
        suppliers=supplier_seed,
        fob_draft=draft,
        fob_safe_to_ask=bool(fob.safe_to_ask_co) if fob else True,
        due_date=contract.due_date,
    )
    checkpoint["commercial_artifacts"] = commercial_seed
    commercial_meta = dict(checkpoint.get("commercial") or {})
    commercial_meta.setdefault("quote_requested", False)
    checkpoint["commercial"] = commercial_meta
    deal.funnel_checkpoint_json = checkpoint
    flag_modified(deal, "funnel_checkpoint_json")

    session.flush()
    snap = build_workspace_snapshot(session, contract)
    return {
        "ok": True,
        "LIVE_API_REQUESTS": 0,
        "opportunity_id": contract.id,
        "next_action": snap.get("next_action"),
        "commercial_artifacts_seeded": bool(snap.get("supplier_rfq_packet")),
        "external_calls": snap["external_calls"],
    }


def _opp199_requirement_seeds() -> list[dict[str, Any]]:
    return [
        requirement_record(
            requirement_key="product_dell_r670",
            description="Dell PowerEdge R670 / 210-BNZH brand-name only",
            requirement_type="PRODUCT",
            required=True,
            status=REQ_VERIFIED_REQUIREMENT,
            source_document="RFQ PDF",
            verified_status="VERIFIED",
        ),
        requirement_record(
            requirement_key="quantity_14",
            description="Quantity 14 servers",
            requirement_type="QUANTITY",
            required=True,
            status=REQ_VERIFIED_REQUIREMENT,
            verified_status="VERIFIED",
        ),
        requirement_record(
            requirement_key="delivery_30_days_alexandria",
            description="Deliver within 30 days ARO to Alexandria VA",
            requirement_type="DELIVERY",
            required=True,
            status=REQ_VERIFIED_REQUIREMENT,
            verified_status="VERIFIED",
        ),
        requirement_record(
            requirement_key="oem_authorization_letter",
            description="OEM/authorized dealer/distributor/reseller letter",
            requirement_type="OEM_LETTER",
            required=True,
            status=REQ_VERIFIED_REQUIREMENT,
            verified_status="VERIFIED",
        ),
        requirement_record(
            requirement_key="channel_authorization",
            description="Federal channel authorization for quoting company",
            requirement_type="CHANNEL_AUTHORIZATION",
            required=True,
            status=REQ_UNKNOWN,
        ),
    ]


def _restore_fob_from_completeness_report(session: Any, contract_id: int) -> None:
    from missing_info import CO_CLARIFICATION_CANDIDATE
    from models import MissingInfoItem

    if not COMPLETENESS_REPORT.exists():
        return
    try:
        crep = json.loads(COMPLETENESS_REPORT.read_text(encoding="utf-8"))
        clar = ((crep.get("fact_results") or {}).get("freight_fob") or {}).get("co_clarification") or {}
        if clar.get("SAFE_TO_ASK_CO") != "YES":
            return
        fob_row = (
            session.query(MissingInfoItem)
            .filter_by(contract_id=contract_id, fact_key="freight_fob_responsibility")
            .first()
        )
        if fob_row is None:
            return
        fob_row.status = CO_CLARIFICATION_CANDIDATE
        fob_row.safe_to_ask_co = True
        fob_row.confidence_absent = clar.get("CONFIDENCE_ABSENT") or "HIGH"
        if clar.get("DRAFT_QUESTION"):
            fob_row.question_draft_json = {"draft": clar.get("DRAFT_QUESTION")}
    except Exception:
        pass


def _supplier_seed(session: Any, contract_id: int) -> list[dict[str, Any]]:
    from models import KnowledgeSupplier, SupplierPursuitPlan

    ids = [
        p.supplier_id
        for p in session.query(SupplierPursuitPlan).filter_by(contract_id=contract_id).all()
        if p.supplier_id
    ]
    out = []
    if ids:
        for s in session.query(KnowledgeSupplier).filter(KnowledgeSupplier.id.in_(ids)).all():
            out.append({"id": s.id, "name": s.name, "website": s.website, "contact_verified": False})
    if not out:
        out = [{"id": i + 1, "name": n, "contact_verified": False} for i, n in enumerate(OPP199_SUPPLIER_NAMES)]
    return out
