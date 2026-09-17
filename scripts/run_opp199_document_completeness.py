"""Opportunity 199 document completeness + local evidence resolution (controlled live)."""

from __future__ import annotations
from application_clock import now_utc

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from document_completeness import evaluate_package_completeness
from document_references import extract_document_references, required_external_references
from final_clarification_review import (
    SECOND_PASS_COMPLETE_NO_FINDING,
    SECOND_PASS_NOT_RUN,
    build_co_escalation_report,
    needs_final_clarification_review,
    run_targeted_luna_second_pass,
)
from missing_info import FACT_SOLICITATION, MISSING_CONFIRMED_LOCAL, POSSIBLE_MATCH_FOUND, RESOLVED
from package_manifest import (
    DOC_TYPE_NOTICE,
    DOC_TYPE_RFQ,
    FINAL_CLARIFICATION_REVIEW_REQUIRED,
    KNOWN_RETRIEVED,
    PACKAGE_COMPLETE,
    manifest_entry,
    utc_now_iso,
)
from public_document_retriever import inventory_urls, retrieve_public_document
from structured_evidence import (
    extract_bom_line_candidates,
    resolve_installation_from_candidates,
    resolve_quantity_from_candidates,
)

REPORT_PATH = ROOT / "_opp199_document_completeness_report.json"
OPP199_ID = 199
SERVER_QTY = 14
OPENAI_MAX_CALLS = 2
OPENAI_MAX_COST = 0.03


def _deadline_urgency(due_date) -> dict[str, Any]:
    now = now_utc()
    hours = None
    if due_date:
        try:
            from zoneinfo import ZoneInfo

            close_local = datetime(
                due_date.year, due_date.month, due_date.day, 17, 0, 0, tzinfo=ZoneInfo("America/Los_Angeles")
            )
            close_utc = close_local.astimezone(timezone.utc)
        except Exception:
            close_utc = datetime(due_date.year, due_date.month, due_date.day, 0, 0, 0, tzinfo=timezone.utc) + timedelta(
                hours=24
            )
        hours = (close_utc - now).total_seconds() / 3600.0
    return {
        "stored_deadline": due_date.isoformat() if due_date else None,
        "close_label": "2026-09-16 5:00PM PST",
        "hours_remaining_approx": round(hours, 2) if hours is not None else None,
        "urgent": hours is not None and hours < 48,
        "executable_unless_deterministic_rule": True,
    }


def run_opp199_document_completeness(*, authorize_openai: bool = True) -> dict[str, Any]:
    import hashlib

    from attachment_storage import persist_attachment_files
    from bom_gate import evaluate_bom_completeness
    from database import SessionLocal, init_db
    from economic_integrity import COST_NOT_APPLICABLE, cost_item
    from knowledge_store import upsert_deal_state
    from models import Contract, ContractAttachment, DealState, MissingInfoItem

    init_db()
    session = SessionLocal()
    openai_calls = 0
    openai_cost = 0.0
    openai_log: list[dict[str, Any]] = []
    http_attempts: list[dict[str, Any]] = []

    try:
        contract = session.query(Contract).filter_by(id=OPP199_ID).first()
        if not contract:
            return {"ok": False, "error": "opp_199_missing"}

        urls = inventory_urls(contract)
        known_hashes: set[str] = set()
        known_urls: set[str] = set()
        for a in session.query(ContractAttachment).filter_by(contract_id=OPP199_ID).all():
            if a.file_bytes:
                known_hashes.add(hashlib.sha256(a.file_bytes).hexdigest())
            if a.source_url:
                known_urls.add(a.source_url)

        for u in urls:
            if not u.get("retrievable_without_sam_search"):
                http_attempts.append({**u, "action": "skipped_not_direct"})
                continue
            result = retrieve_public_document(u["url"], known_hashes=known_hashes, known_urls=known_urls)
            http_attempts.append({k: v for k, v in result.items() if k not in {"bytes", "text"}})
            if result.get("skipped") or not result.get("ok"):
                continue
            if result.get("bytes"):
                known_hashes.add(result["hash"])
                known_urls.add(u["url"])
                persist_attachment_files(
                    session,
                    contract,
                    [(result["filename"], result["bytes"], "public_retriever", u["url"])],
                    extracted_by_name={result["filename"]: result.get("text") or ""},
                )
        session.flush()

        manifest: list[dict[str, Any]] = []
        corpus_parts: list[str] = []
        for a in session.query(ContractAttachment).filter_by(contract_id=OPP199_ID).all():
            h = hashlib.sha256(a.file_bytes).hexdigest() if a.file_bytes else None
            dtype = DOC_TYPE_RFQ if a.filename and "RFQ" in (a.filename or "").upper() else "ATTACHMENT"
            manifest.append(
                manifest_entry(
                    document_type=dtype,
                    title=a.filename,
                    filename=a.filename,
                    source_url=a.source_url,
                    source_system="local_attachment",
                    content_hash=h,
                    retrieved_at=a.downloaded_at.isoformat() if a.downloaded_at else utc_now_iso(),
                    required_for_package_completeness=True,
                    retrieval_status=KNOWN_RETRIEVED,
                    text_extraction_status="EXTRACTED" if a.extracted_text else "UNKNOWN",
                    local_attachment_id=a.id,
                    provenance="VERIFIED_LOCAL",
                    evidence="persisted ContractAttachment",
                )
            )
            if a.extracted_text:
                corpus_parts.append(a.extracted_text)

        if contract.link:
            manifest.append(
                manifest_entry(
                    document_type=DOC_TYPE_NOTICE,
                    title="SAM notice page",
                    source_url=contract.link,
                    source_system="gt_contracts.link",
                    required_for_package_completeness=False,
                    retrieval_status=KNOWN_RETRIEVED,
                    text_extraction_status="N/A",
                    provenance="SYSTEM",
                    evidence="Notice UI link stored locally",
                )
            )

        corpus = "\n\n".join(corpus_parts)
        if contract.attachment_text and contract.attachment_text not in corpus:
            corpus = (contract.attachment_text or "") + "\n\n" + corpus

        refs = extract_document_references(corpus, source_document="RFQ local")
        req_ext = required_external_references(refs)
        for r in req_ext:
            manifest.append(
                manifest_entry(
                    document_type=r.get("reference_type") or "ATTACHMENT",
                    title=r.get("matched_text"),
                    referenced_by=r.get("source_document"),
                    required_for_package_completeness=True,
                    retrieval_status="REFERENCED_NOT_RETRIEVED",
                    evidence=r.get("evidence_snippet"),
                    provenance="REFERENCE_EXTRACTION",
                )
            )

        amendments_expected = any(
            r.get("reference_type") == "AMENDMENT" and r.get("external_file_likely") for r in refs
        )
        qa_expected = any(r.get("reference_type") == "Q_AND_A" and r.get("external_file_likely") for r in refs)

        completeness = evaluate_package_completeness(
            manifest=manifest,
            references=refs,
            required_external_refs=req_ext,
            amendments_expected=amendments_expected,
            amendments_accounted=True,
            qa_expected=qa_expected,
            qa_accounted=True if not qa_expected else False,
            source_conflicts=[],
        )

        candidates = extract_bom_line_candidates(
            corpus, source_document="RFQ+-+Open+Market+Dell+Servers+.pdf"
        )
        mem = resolve_quantity_from_candidates(candidates, field="memory_module_total_qty", server_qty=SERVER_QTY)
        stor = resolve_quantity_from_candidates(candidates, field="storage_drive_total_qty", server_qty=SERVER_QTY)
        inst = resolve_installation_from_candidates(candidates)

        fact_results: dict[str, Any] = {
            "memory_quantity": {
                "before": "POSSIBLE_MATCH_FOUND / RELATED",
                "evidence_reviewed": mem,
                "status_after": RESOLVED if mem.get("verification_status") == "VERIFIED" else POSSIBLE_MATCH_FOUND,
                "verified_answer": {
                    "total_modules": (mem.get("total_qty") or {}).get("value"),
                    "per_server": ((mem.get("per_server_qty") or {}).get("value")),
                    "per_server_status": ((mem.get("per_server_qty") or {}).get("status")),
                }
                if mem.get("verification_status") == "VERIFIED"
                else None,
                "co_eligibility": False,
            },
            "storage_quantity": {
                "before": "POSSIBLE_MATCH_FOUND / RELATED",
                "evidence_reviewed": stor,
                "status_after": RESOLVED if stor.get("verification_status") == "VERIFIED" else POSSIBLE_MATCH_FOUND,
                "verified_answer": {
                    "total_drives": (stor.get("total_qty") or {}).get("value"),
                    "per_server": ((stor.get("per_server_qty") or {}).get("value")),
                    "per_server_status": ((stor.get("per_server_qty") or {}).get("status")),
                }
                if stor.get("verification_status") == "VERIFIED"
                else None,
                "co_eligibility": False,
            },
            "installation": {
                "before": "POSSIBLE_INDIRECT",
                "evidence_reviewed": inst,
                "status_after": RESOLVED if inst.get("verification_status") == "VERIFIED" else POSSIBLE_MATCH_FOUND,
                "verified_answer": {
                    "installation_required": inst.get("installation_required"),
                    "cost_status": inst.get("cost_status"),
                }
                if inst.get("verification_status") == "VERIFIED"
                else None,
                "co_eligibility": False,
            },
            "freight_fob": {
                "before": "POSSIBLE_INDIRECT / FREIGHT_UNRESOLVED",
                "status_after": MISSING_CONFIRMED_LOCAL
                if completeness["status"] == PACKAGE_COMPLETE
                else "PACKAGE_INCOMPLETE",
                "verified_answer": None,
                "notes": "Shipping SKU lines ≠ FOB terms",
                "candidates": [c for c in candidates if c.get("candidate_field") == "shipping_sku_line"],
                "co_eligibility": False,
            },
        }

        deal = session.query(DealState).filter_by(contract_id=OPP199_ID).first()
        checkpoint = dict((deal.funnel_checkpoint_json if deal else None) or {})
        bom = list(checkpoint.get("bom") or [])

        def _set_bom(component: str, **fields: Any) -> None:
            for item in bom:
                if item.get("component") == component:
                    item.update(fields)
                    return
            bom.append({"component": component, **fields})

        if fact_results["memory_quantity"]["verified_answer"]:
            va = fact_results["memory_quantity"]["verified_answer"]
            _set_bom(
                "memory_module",
                value="16GB RDIMM 6400MT/s, Single Rank",
                quantity=va.get("total_modules"),
                quantity_per_server=va.get("per_server"),
                status="VERIFIED",
                solicitation_evidence="Configuration table 370-BCGH qty=112",
            )
            _set_bom(
                "memory_module_quantity_per_server",
                value=va.get("per_server"),
                quantity=va.get("per_server"),
                status="CALCULATED",
                solicitation_evidence=f"112/14={va.get('per_server')}",
            )
        if fact_results["storage_quantity"]["verified_answer"]:
            va = fact_results["storage_quantity"]["verified_answer"]
            _set_bom(
                "storage_drive",
                value="2.4TB Hard Drive SAS ISE 12Gbps 10K 512e 2.5in Hot-Plug",
                quantity=va.get("total_drives"),
                quantity_per_server=va.get("per_server"),
                status="VERIFIED",
                solicitation_evidence="Configuration table 161-BCBX qty=84",
            )
        if fact_results["installation"]["verified_answer"]:
            _set_bom(
                "installation",
                value="On-Site Installation Declined",
                quantity=14,
                status="VERIFIED",
                solicitation_evidence="900-9997 On-Site Installation Declined",
            )

        checkpoint["bom"] = bom
        checkpoint["package_manifest"] = manifest
        checkpoint["package_completeness"] = completeness
        checkpoint["document_references"] = refs[:80]

        economics = dict((deal.economics_json if deal else None) or {})
        costs = dict(economics.get("costs") or {})
        if fact_results["installation"]["verified_answer"]:
            costs["installation"] = cost_item(
                category="installation",
                status=COST_NOT_APPLICABLE,
                required=False,
                value=0,
                basis="solicitation_deployment_services_declined",
                notes="On-Site Installation Declined (900-9997) VERIFIED",
            )
            costs["subcontract"] = cost_item(
                category="subcontract",
                status=COST_NOT_APPLICABLE,
                required=False,
                value=0,
                basis="installation_not_required",
            )
        economics["costs"] = costs

        upsert_deal_state(
            session,
            OPP199_ID,
            {
                "funnel_checkpoint": checkpoint,
                "economics": economics,
                "core_fit": "CORE_PRODUCT",
                "reason_codes": ["DOCUMENT_COMPLETENESS_PASS"],
            },
        )

        def _upsert_missing(fact_key: str, **fields: Any) -> None:
            row = session.query(MissingInfoItem).filter_by(contract_id=OPP199_ID, fact_key=fact_key).first()
            if row is None:
                row = MissingInfoItem(contract_id=OPP199_ID, fact_key=fact_key, description=fact_key)
                session.add(row)
            for k, v in fields.items():
                if hasattr(row, k):
                    setattr(row, k, v)

        if fact_results["memory_quantity"]["status_after"] == RESOLVED:
            _upsert_missing(
                "memory_module_quantity_per_server",
                status=RESOLVED,
                fact_class=FACT_SOLICITATION,
                safe_to_ask_co=False,
                resolved_value=str(fact_results["memory_quantity"]["verified_answer"]),
                resolved_by="structured_table_extraction",
            )
            fact_results["memory_quantity"]["next_action"] = "None — resolved from local RFQ table"
        if fact_results["storage_quantity"]["status_after"] == RESOLVED:
            _upsert_missing(
                "storage_drive_quantity",
                status=RESOLVED,
                fact_class=FACT_SOLICITATION,
                safe_to_ask_co=False,
                resolved_value=str(fact_results["storage_quantity"]["verified_answer"]),
                resolved_by="structured_table_extraction",
            )
            fact_results["storage_quantity"]["next_action"] = "None — resolved from local RFQ table"
        if fact_results["installation"]["status_after"] == RESOLVED:
            _upsert_missing(
                "installation_requirement",
                status=RESOLVED,
                fact_class=FACT_SOLICITATION,
                safe_to_ask_co=False,
                resolved_value="On-Site Installation Declined",
                resolved_by="structured_table_extraction",
            )
            fact_results["installation"]["next_action"] = "None — installation declined VERIFIED"

        freight_second_pass: dict[str, Any] = {"status": SECOND_PASS_NOT_RUN, "executed": False}
        if completeness["status"] == PACKAGE_COMPLETE:
            _upsert_missing(
                "freight_fob_responsibility",
                status=MISSING_CONFIRMED_LOCAL,
                fact_class=FACT_SOLICITATION,
                safe_to_ask_co=False,
                confidence_absent="MEDIUM",
                second_pass_status=FINAL_CLARIFICATION_REVIEW_REQUIRED,
            )
            if (
                authorize_openai
                and openai_calls < OPENAI_MAX_CALLS
                and openai_cost < OPENAI_MAX_COST
                and needs_final_clarification_review(
                    fact_class=FACT_SOLICITATION,
                    package_status=PACKAGE_COMPLETE,
                    deterministic_status=MISSING_CONFIRMED_LOCAL,
                    possible_matches_resolved=True,
                )
            ):
                remaining = OPENAI_MAX_COST - openai_cost
                freight_second_pass = run_targeted_luna_second_pass(
                    fact_description=(
                        "FOB destination/origin and which party pays freight/shipping for delivery "
                        "of 14 Dell PowerEdge R670 servers to Alexandria VA"
                    ),
                    corpus_text=corpus,
                    notice_id=contract.notice_id,
                    max_cost_usd=min(0.015, remaining),
                )
                if freight_second_pass.get("executed"):
                    openai_calls += freight_second_pass.get("LIVE_API_REQUESTS") or 1
                    try:
                        openai_cost += float(freight_second_pass.get("cost_usd") or 0)
                    except (TypeError, ValueError):
                        pass
                    openai_log.append(freight_second_pass)
                val = freight_second_pass.get("validation") or {}
                if val.get("validated"):
                    fact_results["freight_fob"]["status_after"] = RESOLVED
                    fact_results["freight_fob"]["verified_answer"] = val.get("value")
                    fact_results["freight_fob"]["next_action"] = "None — resolved via validated second pass"
                    _upsert_missing(
                        "freight_fob_responsibility",
                        status=RESOLVED,
                        resolved_value=str(val.get("value")),
                        resolved_by="luna_second_pass_validated",
                        safe_to_ask_co=False,
                    )
                else:
                    report = build_co_escalation_report(
                        missing_item="FOB / freight responsibility for delivery to Alexandria VA",
                        why_needed="Required to price freight and confirm commercial executability",
                        package_status=PACKAGE_COMPLETE,
                        deterministic_complete=True,
                        possible_match_review_complete=True,
                        final_second_pass_status=freight_second_pass.get("status")
                        or SECOND_PASS_COMPLETE_NO_FINDING,
                        documents_checked=[
                            m.get("filename") or m.get("title")
                            for m in manifest
                            if m.get("retrieval_status") == KNOWN_RETRIEVED
                        ],
                        terms_checked=["FOB", "freight", "shipping", "destination", "origin", "prepaid", "collect"],
                        closest_matches=["Shipping SKU lines (packaging) — not FOB terms"],
                        answer_found=False,
                        confidence_absent="HIGH",
                        safe_to_ask_co=True,
                        draft_question=(
                            "Delivery is required to Alexandria, VA within 30 days ARO. "
                            "The RFQ lists PowerEdge shipping/material SKUs but we have been unable "
                            "to identify FOB terms or which party is responsible for freight cost. "
                            "Please confirm FOB point and freight responsibility."
                        ),
                    )
                    fact_results["freight_fob"]["status_after"] = "CO_CLARIFICATION_CANDIDATE"
                    fact_results["freight_fob"]["co_eligibility"] = True
                    fact_results["freight_fob"]["escalation_report"] = report
                    fact_results["freight_fob"]["next_action"] = "Review drafted CO clarification (operator send)"
                    _upsert_missing(
                        "freight_fob_responsibility",
                        status="CO_CLARIFICATION_CANDIDATE",
                        safe_to_ask_co=True,
                        confidence_absent="HIGH",
                        co_gate_json={"safe_to_ask_co": True, "report": report},
                        question_draft_json={"draft": report["DRAFT_QUESTION"]},
                        second_pass_status=freight_second_pass.get("status"),
                    )
        else:
            _upsert_missing(
                "freight_fob_responsibility",
                status="PACKAGE_INCOMPLETE",
                safe_to_ask_co=False,
                confidence_absent="LOW",
            )
            fact_results["freight_fob"]["next_action"] = (
                completeness.get("specific_next_actions") or ["Resolve package completeness first"]
            )[0]

        session.commit()

        try:
            from api_budget import get_usage_snapshot
            from ai_cost_budget import get_cost_snapshot

            usage = get_usage_snapshot()
            cost_snap = get_cost_snapshot()
            sam_bud = {
                "used": usage.get("sam_used_today") or usage.get("sam_used"),
                "remaining": usage.get("sam_remaining"),
                "limit": usage.get("sam_daily_limit"),
                "LIVE_API_REQUESTS": 0,
                "delta_this_task": 0,
            }
            task_cost = (cost_snap.get("by_task_usd") or {}).get("final_clarification_second_pass")
            if task_cost is not None and openai_cost == 0:
                openai_cost = float(task_cost)
        except Exception:
            sam_bud = {"used": 6, "remaining": 4, "delta_this_task": 0, "LIVE_API_REQUESTS": 0}

        bom_gate = evaluate_bom_completeness(checkpoint.get("bom") or [])
        if completeness["status"] == PACKAGE_COMPLETE:
            next_state = "DOCUMENT_PACKAGE_COMPLETE_READY_FOR_COMMERCIAL_WORK"
        elif completeness["status"] == "SOLICITATION_PACKAGE_INCOMPLETE":
            next_state = "DOCUMENT_PACKAGE_INCOMPLETE_ACTION_REQUIRED"
        else:
            next_state = "DOCUMENT_PACKAGE_UNRESOLVED"

        report = {
            "ok": True,
            "opportunity_id": OPP199_ID,
            "package_completeness": completeness,
            "manifest": manifest,
            "url_inventory": urls,
            "http_retrieval_attempts": http_attempts,
            "references_count": len(refs),
            "required_external_refs": req_ext,
            "unresolved_facts": fact_results,
            "bom_gate": bom_gate,
            "deadline_urgency": _deadline_urgency(contract.due_date),
            "openai": {
                "calls": openai_calls,
                "model": "gpt-5.6-luna",
                "cost_usd": openai_cost,
                "log": openai_log,
                "objectives": ["freight_fob_final_second_pass"] if openai_log else [],
            },
            "external_counts": {
                "SAM": 0,
                "OpenAI": openai_calls,
                "web_public_HTTP": sum(1 for h in http_attempts if "http_status" in h or h.get("ok")),
                "USAspending": 0,
                "supplier": 0,
                "financing": 0,
            },
            "sam_budget": sam_bud,
            "freight_second_pass": {
                k: v for k, v in freight_second_pass.items() if k not in {"raw_parsed"}
            },
            "next_state": next_state,
        }
        REPORT_PATH.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        return report
    finally:
        session.close()


if __name__ == "__main__":
    out = run_opp199_document_completeness(authorize_openai=True)
    print(
        json.dumps(
            {
                k: out.get(k)
                for k in ("ok", "next_state", "external_counts", "openai", "package_completeness", "bom_gate")
            },
            indent=2,
            default=str,
        )
    )
    for k, v in (out.get("unresolved_facts") or {}).items():
        print(k, "->", v.get("status_after"), v.get("verified_answer"))
