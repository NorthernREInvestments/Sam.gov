"""Re-run transactional procurement intelligence after authorized document ingestion.

Preserves prior evidence. Produces before/after change records.
Does not erase previous packets — writes updated packets + change history.
"""

from __future__ import annotations
from application_clock import now_utc

import csv
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bid_price_targets import compute_bid_price_targets
from document_ingestion import (
    append_change_history,
    list_operator_document_requests,
    load_ingested_texts,
    load_registry,
)
from document_ingestion_constants import ACQ_AUTHORIZED_OPERATOR_DOWNLOAD
from procurement_blockers import classify_procurement_blockers
from specification_extraction import extract_blade_or_product_specifications
from supplier_fit import evaluate_supplier_fit
from transactional_bom import (
    assess_document_completeness,
    build_transactional_requirement,
    extract_delivery_and_terms,
    extract_sciquest_product_line_items,
    identify_product,
)
from transactional_procurement import (
    ARTIFACTS_DIR,
    EVIDENCE_DIR,
    PACKETS_DIR,
    build_supplier_quote_packet,
    compute_procurement_economics,
    correct_deal_type_from_documents,
    discover_suppliers_for_product,
    evaluate_bid_ready_strict,
    maybe_run_funding,
    render_operator_packet_md,
)

ARTIFACTS = ARTIFACTS_DIR


def _utc() -> str:
    return now_utc().isoformat()


def load_packet(solicitation_number: str) -> dict[str, Any] | None:
    path = PACKETS_DIR / f"{solicitation_number}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_packet(packet: dict[str, Any]) -> None:
    PACKETS_DIR.mkdir(parents=True, exist_ok=True)
    sol = packet["solicitation_number"]
    # Strip operator_markdown from JSON twin if present as separate md
    payload = {k: v for k, v in packet.items() if k != "operator_markdown"}
    (PACKETS_DIR / f"{sol}.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    md = packet.get("operator_markdown") or render_operator_packet_md(packet)
    (PACKETS_DIR / f"{sol}.md").write_text(md, encoding="utf-8")


def _snapshot_for_diff(packet: dict[str, Any]) -> dict[str, Any]:
    req = packet.get("requirement") or {}
    pid = req.get("product_identification") or {}
    terms = req.get("terms") or {}
    econ = packet.get("economics_block") or {}
    return {
        "document_completeness": (req.get("completeness") or {}).get("document_completeness")
        or req.get("document_completeness"),
        "product_id_state": pid.get("product_id_state"),
        "line_item_count": len(req.get("line_items") or []),
        "delivery_location": (terms.get("delivery_location") or {}).get("value"),
        "bid_deadline": (terms.get("bid_deadline") or {}).get("value"),
        "spec_known_fields": (packet.get("technical_specifications") or {}).get("known_fields"),
        "supplier_count": packet.get("supplier_count"),
        "supplier_cost": econ.get("supplier_cost"),
        "freight_status": econ.get("freight_status"),
        "expected_revenue": econ.get("expected_revenue"),
        "profit_state": econ.get("profit_state"),
        "working_capital_status": econ.get("working_capital_status"),
        "primary_blocker": (packet.get("blocker_model") or {}).get("primary_blocker"),
        "decision": packet.get("decision"),
    }


def diff_snapshots(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    changes = []
    keys = sorted(set(before) | set(after))
    for k in keys:
        b, a = before.get(k), after.get(k)
        if b != a:
            changes.append(
                {
                    "field": k,
                    "before": b if b is not None else "UNKNOWN",
                    "after": a if a is not None else "UNKNOWN",
                }
            )
    return changes


def reprocess_solicitation(
    solicitation_number: str,
    *,
    prior_packet: dict[str, Any] | None = None,
    write_artifacts: bool = True,
) -> dict[str, Any]:
    """
    Merge ingested operator documents into existing packet and re-run analysis.
    No live discovery HTTP by default.
    """
    prior = prior_packet or load_packet(solicitation_number) or {
        "solicitation_number": solicitation_number,
        "documents": [],
        "requirement": {},
        "suppliers": [],
        "economics_block": {},
        "funding": {"status": "NOT_YET_NEEDED"},
        "compliance": {},
        "government_value_evidence": {},
        "bidder_priced": True,
    }
    before = _snapshot_for_diff(prior)
    packet = deepcopy(prior)
    packet["bidder_priced"] = True  # Iowa RFB unit-price blanks → bidder-priced
    packet["reprocessed_at"] = _utc()
    packet["prior_evidence_preserved"] = True

    ingested = load_ingested_texts(solicitation_number)
    packet["ingested_documents"] = [{k: v for k, v in d.items() if k != "text"} for d in ingested]

    # Merge document inventory: mark auth-blocked specs resolved when ingested
    docs = list(packet.get("documents") or [])
    ingested_titles = {(d.get("document_title") or "").lower() for d in ingested}
    for d in docs:
        title = (d.get("document_title") or "").lower()
        if title in ingested_titles and d.get("access_status") in {
            "LOGIN_REQUIRED",
            "AUTH_REQUIRED",
            "PUBLIC_LISTED_NOT_FETCHED",
        }:
            d["access_status"] = "OPERATOR_PROVIDED"
            d["resolved_by_ingestion"] = True
            d["appears_authoritative"] = True
    # Append ingested docs not already listed
    existing_hashes = {d.get("hash") for d in docs if d.get("hash")}
    for d in ingested:
        if d.get("hash") and d["hash"] in existing_hashes:
            continue
        docs.append(
            {
                "solicitation_number": solicitation_number,
                "source_portal": d.get("source_system"),
                "document_title": d.get("document_title"),
                "document_url": d.get("operator_source_url"),
                "file_type": d.get("file_type"),
                "access_status": "OPERATOR_PROVIDED",
                "hash": d.get("hash"),
                "document_class": d.get("document_class"),
                "acquisition_method": d.get("acquisition_method"),
                "appears_authoritative": True,
                "superseded_or_amended": bool(d.get("superseded")),
                "local_path": d.get("local_path"),
                "provenance": d.get("provenance"),
            }
        )
    packet["documents"] = docs

    # Combine texts: prior event PDF preview not always stored — use ingested + any evidence txt
    combined_text_parts: list[str] = []
    evidence_dir = EVIDENCE_DIR / solicitation_number
    if evidence_dir.exists():
        for p in evidence_dir.glob("*.txt"):
            combined_text_parts.append(p.read_text(encoding="utf-8", errors="replace"))
        for p in evidence_dir.glob("*.pdf"):
            try:
                from pdf_text import extract_pdf_text

                combined_text_parts.append(extract_pdf_text(p.read_bytes()) or "")
            except Exception:
                pass
    for d in ingested:
        if d.get("text"):
            combined_text_parts.append(d["text"])

    combined = "\n\n".join(combined_text_parts)

    # Also pull text from existing packet previews if needed
    for d in prior.get("documents") or []:
        preview = d.get("text_preview")
        if preview:
            combined_text_parts.append(str(preview))
    if not combined.strip():
        combined = "\n\n".join(combined_text_parts)

    line_items = extract_sciquest_product_line_items(combined, source_document="combined_package")
    # Preserve prior line items if re-extract finds fewer (don't erase)
    prior_items = (prior.get("requirement") or {}).get("line_items") or []
    if prior_items and len(line_items) < len(prior_items):
        line_items = prior_items

    terms = extract_delivery_and_terms(combined, source_document="combined_package")
    # Merge terms: prefer newly verified over unknown
    prior_terms = (prior.get("requirement") or {}).get("terms") or {}
    merged_terms = dict(prior_terms)
    for k, v in terms.items():
        if isinstance(v, dict) and v.get("value") not in (None, "", []):
            merged_terms[k] = v
        elif k not in merged_terms:
            merged_terms[k] = v

    # Spec extraction from ingested specification-class docs preferentially
    spec_text = ""
    spec_source = None
    for d in ingested:
        if d.get("document_class") == "SPECIFICATION" or "spec" in (d.get("document_title") or "").lower():
            spec_text += "\n" + (d.get("text") or "")
            spec_source = d.get("document_title")
    if not spec_text.strip():
        spec_text = combined
        spec_source = "combined_package"
    technical = extract_blade_or_product_specifications(spec_text, source_document=spec_source)
    packet["technical_specifications"] = technical

    # Enrich line items with spec hints when present
    for li in line_items:
        if technical["specifications"].get("brand_or_equal_language", {}).get("value"):
            li["brand_name_or_equal"] = True
        if technical["specifications"].get("mill_certifications", {}).get("value"):
            li["required_certifications"] = ["mill_certification"]
        dims = technical["specifications"].get("dimensions", {}).get("value")
        if dims:
            li["dimensions"] = dims
        if (merged_terms.get("delivery_location") or {}).get("value"):
            li["delivery_location"] = merged_terms["delivery_location"]["value"]

    product_id = identify_product(
        line_items=line_items,
        terms=merged_terms,
        title=packet.get("title"),
    )
    # If specs add detail, prefer brand-or-equal / spec commodity over insufficient
    if product_id.get("product_id_state") == "INSUFFICIENT_INFORMATION" and technical.get("sufficient_for_quote_packet"):
        product_id["product_id_state"] = "SPEC_DEFINED_COMMODITY"
        product_id["rationale"] = "upgraded_after_spec_ingestion"

    completeness = assess_document_completeness(
        documents=docs,
        line_items=line_items,
        terms=merged_terms,
        product_id=product_id,
    )
    # If spec ingested, clear critical_spec_missing
    if any(d.get("document_class") == "SPECIFICATION" for d in ingested):
        completeness["critical_spec_missing"] = False
        if completeness.get("document_completeness") in {"PARTIAL", "CRITICAL_DOCUMENT_MISSING", "AUTH_BLOCKED"}:
            completeness["document_completeness"] = "COMPLETE_ENOUGH_FOR_PRODUCT_ID"
        missing = [m for m in (completeness.get("missing") or []) if not str(m).startswith("specification_attachment")]
        completeness["missing"] = missing

    deal_corr = correct_deal_type_from_documents(
        title=packet.get("title"),
        line_items=line_items,
        prior_deal_type=packet.get("deal_type"),
    )
    packet["deal_type"] = deal_corr["deal_type"]
    packet["deal_type_correction"] = deal_corr

    suppliers = discover_suppliers_for_product(
        product_id=product_id, title=packet.get("title"), max_suppliers=4
    )
    suppliers = evaluate_supplier_fit(
        suppliers=suppliers,
        specifications=technical,
        product_id=product_id,
    )
    packet["suppliers"] = suppliers
    packet["supplier_count"] = len(suppliers)

    quote_packets = []
    for s in suppliers[:4]:
        quote_packets.append(
            build_supplier_quote_packet(
                solicitation_number=solicitation_number,
                agency=packet.get("agency"),
                product_id=product_id,
                line_items=line_items,
                terms=merged_terms,
                supplier=s,
            )
        )
    # Enrich quote packets with technical specs summary
    spec_summary = {
        k: v.get("value")
        for k, v in (technical.get("specifications") or {}).items()
        if v.get("value") not in (None, "", [])
    }
    for qp in quote_packets:
        qp["technical_requirements"] = spec_summary
        qp["do_not_send_automatically"] = True
        qp["outreach_performed"] = False
    packet["quote_packets"] = quote_packets

    public_price = packet.get("public_price") or {
        "price_evidence": "QUOTE_REQUIRED",
        "unit_price": None,
        "notes": "suppliers_identified_quote_required",
    }
    gov_value = packet.get("government_value_evidence") or {}
    gov_value["bidder_priced"] = True
    gov_value["notes"] = gov_value.get("notes") or (
        "Bidder-priced RFB — no fixed government revenue assumed; use bid-price thresholds"
    )
    packet["government_value_evidence"] = gov_value

    economics_block = compute_procurement_economics(
        line_items=line_items,
        public_price_evidence=public_price,
        freight=None,
        freight_status="UNKNOWN",
        expected_revenue=gov_value.get("expected_revenue"),
        revenue_status=gov_value.get("revenue_confidence") or "UNKNOWN",
    )
    packet["economics_block"] = economics_block
    packet["funding"] = maybe_run_funding(economics_block)

    packet["bid_price_targets"] = compute_bid_price_targets(
        supplier_cost=economics_block.get("supplier_cost"),
        freight=economics_block.get("freight"),
        landed_cost=economics_block.get("landed_cost"),
    )

    requirement = build_transactional_requirement(
        solicitation_number=solicitation_number,
        agency=packet.get("agency"),
        buyer=(packet.get("contact") or {}).get("name") if isinstance(packet.get("contact"), dict) else None,
        source="sciquest_iowa",
        deal_type=deal_corr["deal_type"],
        line_items=line_items,
        terms=merged_terms,
        product_id=product_id,
        completeness=completeness,
    )
    packet["requirement"] = requirement

    blocker_model = classify_procurement_blockers(packet)
    packet["blocker_model"] = blocker_model

    # Decision from blockers
    codes = set(blocker_model.get("codes") or [])
    if "AUTHENTICATION_BLOCKED_DOCUMENT" in codes or "OPERATOR_DOCUMENT_REQUIRED" in codes:
        packet["decision"] = "GET MISSING DOCUMENT"
        packet["next_actions"] = [
            "OPERATOR_DOCUMENT_REQUIRED",
            "INGEST_VIA_scripts/ingest_solicitation_document.py",
            "REQUEST_SUPPLIER_QUOTE",
        ]
    elif "SUPPLIER_QUOTE_REQUIRED" in codes:
        packet["decision"] = "GET SUPPLIER QUOTE"
        packet["next_actions"] = [
            "REQUEST_SUPPLIER_QUOTE",
            "ESTIMATE_FREIGHT",
            "USE_BID_PRICE_THRESHOLDS_WHEN_COST_KNOWN",
        ]
    else:
        packet["decision"] = packet.get("decision") or "CONTINUE"
        packet["next_actions"] = packet.get("next_actions") or ["CONTINUE"]

    packet["bid_ready_evaluation"] = evaluate_bid_ready_strict(packet)
    packet["supplier_outreach"] = 0
    packet["lender_outreach"] = 0
    packet["bid_submissions"] = 0

    # Operator markdown with blockers
    base_md = render_operator_packet_md(packet)
    blocker_lines = [
        f"- {b['code']} ({b.get('severity')}): {b.get('detail')}"
        for b in blocker_model.get("blockers") or []
    ]
    packet["operator_markdown"] = (
        base_md
        + "\n\nBLOCKERS:\n"
        + ("\n".join(blocker_lines) if blocker_lines else "none")
        + f"\n\nPRIMARY BLOCKER: {blocker_model.get('primary_blocker')}"
        + f"\nFUNDING PREMATURE: {blocker_model.get('funding_premature')}"
        + "\n\nBID PRICE THRESHOLDS:\n"
        + json.dumps(packet.get("bid_price_targets"), indent=2, default=str)
    )

    after = _snapshot_for_diff(packet)
    changes = diff_snapshots(before, after)
    change_record = {
        "event": "PROCUREMENT_REPROCESS",
        "solicitation_number": solicitation_number,
        "timestamp": _utc(),
        "ingested_document_ids": [d.get("document_id") for d in ingested],
        "changes": changes,
        "before": before,
        "after": after,
    }
    packet["reprocess_change_record"] = change_record
    append_change_history(change_record)

    if write_artifacts:
        save_packet(packet)
        _write_ingestion_artifacts(solicitation_number, packet, change_record)

    return {
        "ok": True,
        "solicitation_number": solicitation_number,
        "packet": packet,
        "changes": changes,
        "change_record": change_record,
        "blocker_model": blocker_model,
        "supplier_outreach": 0,
        "lender_outreach": 0,
        "bid_submissions": 0,
    }


def _write_ingestion_artifacts(
    solicitation_number: str,
    packet: dict[str, Any],
    change_record: dict[str, Any],
) -> None:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    results = {
        "generated_at": _utc(),
        "solicitation_number": solicitation_number,
        "registry": load_registry(solicitation_number),
        "blocker_model": packet.get("blocker_model"),
        "changes": change_record.get("changes"),
        "decision": packet.get("decision"),
        "next_state": "AUTHORIZED_DOCUMENT_INGESTION_OPERATIONAL",
    }
    (ARTIFACTS / "document_ingestion_results.json").write_text(
        json.dumps(results, indent=2, default=str), encoding="utf-8"
    )

    # Queue CSV
    qpath = ARTIFACTS / "document_ingestion_queue.csv"
    with qpath.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "solicitation",
                "primary_blocker",
                "decision",
                "ingested_count",
                "product_id_state",
                "funding_premature",
                "next_action",
            ],
        )
        w.writeheader()
        w.writerow(
            {
                "solicitation": solicitation_number,
                "primary_blocker": (packet.get("blocker_model") or {}).get("primary_blocker"),
                "decision": packet.get("decision"),
                "ingested_count": len(packet.get("ingested_documents") or []),
                "product_id_state": ((packet.get("requirement") or {}).get("product_identification") or {}).get(
                    "product_id_state"
                ),
                "funding_premature": (packet.get("blocker_model") or {}).get("funding_premature"),
                "next_action": (packet.get("next_actions") or [""])[0],
            }
        )

    # Operator document requests
    reqs = list_operator_document_requests(packet)
    rpath = ARTIFACTS / "operator_document_requests.csv"
    with rpath.open("w", newline="", encoding="utf-8") as f:
        fields = [
            "solicitation",
            "document_title",
            "document_class",
            "access_status",
            "request",
            "acquisition_hint",
            "instructions",
        ]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in reqs:
            w.writerow(row)

    report = f"""# Document Ingestion Report

Solicitation: {solicitation_number}

## Primary blocker
{(packet.get('blocker_model') or {}).get('primary_blocker')}

Funding premature (not immediate): {(packet.get('blocker_model') or {}).get('funding_premature')}

## Changes this reprocess
{json.dumps(change_record.get('changes'), indent=2, default=str)}

## Decision
{packet.get('decision')}

## Acquisition hint
{ACQ_AUTHORIZED_OPERATOR_DOWNLOAD}

NEXT STATE:
AUTHORIZED_DOCUMENT_INGESTION_OPERATIONAL
"""
    (ARTIFACTS / "document_ingestion_report.md").write_text(report, encoding="utf-8")
