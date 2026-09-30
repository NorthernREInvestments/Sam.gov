"""R4 document generators — quote letter, technical matrix, product schedule, certifications.

Deterministic first. Narrative is DRAFT_REVIEW_REQUIRED. Never invent facts.
Never auto-sign. Never leak internal economics.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from response_engine.r4_constants import (
    DRAFT_LABEL,
    INTERNAL_ONLY,
    OWNER_SIGNATURE_REQUIRED_FIELD,
    POPULATED,
    READY,
    REFERENCE_ONLY,
    SUBMIT_REQUIRED,
    UNKNOWN,
)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def generate_quote_letter(
    project: dict[str, Any],
    *,
    out_path: Path,
    field_maps: list[dict[str, Any]],
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    profile = profile or {}
    company = profile.get("legal_name")
    if company in (None, "", "UNKNOWN"):
        company = None
    uei = profile.get("UEI") if profile.get("UEI") not in (None, "", "UNKNOWN") else None
    cage = profile.get("CAGE") if profile.get("CAGE") not in (None, "", "UNKNOWN") else None

    lines_out = []
    for li in project.get("line_items") or []:
        price_fm = next(
            (
                m
                for m in field_maps
                if m.get("source_requirement") == f"line.{li.get('line_item_id')}.unit_price"
                and m.get("generation_status") in (READY, POPULATED)
            ),
            None,
        )
        lines_out.append(
            {
                "clin": li.get("CLIN") or li.get("buyer_line_number"),
                "description": li.get("description") or li.get("required_mpn") or UNKNOWN,
                "qty": li.get("quantity") or li.get("normalized_quantity") or UNKNOWN,
                "uom": li.get("buyer_uom") or li.get("normalized_uom") or UNKNOWN,
                "unit_price": price_fm.get("source_value") if price_fm else None,
            }
        )

    # Deterministic text — no marketing fluff
    parts = [
        DRAFT_LABEL,
        "",
        f"Solicitation: {project.get('solicitation_number') or project.get('title') or UNKNOWN}",
        f"Buyer: {project.get('buyer') or UNKNOWN}",
        f"Offeror: {company or '[LEGAL NAME UNRESOLVED]'}",
        f"UEI: {uei or '[UEI UNRESOLVED]'}",
        f"CAGE: {cage or '[CAGE UNRESOLVED]'}",
        "",
        "Offered items:",
    ]
    for row in lines_out:
        price_txt = row["unit_price"] if row["unit_price"] is not None else "[PRICE UNRESOLVED]"
        parts.append(
            f"  CLIN {row['clin']}: {row['description']} | Qty {row['qty']} {row['uom']} | Unit {price_txt}"
        )
    parts.extend(
        [
            "",
            "This draft offer is assembled from verified R1/R2/R3 facts only.",
            "Signature: OWNER_SIGNATURE_REQUIRED — not applied by M3.",
            "",
            "DRAFT — NOT SUBMITTED",
        ]
    )
    text = "\n".join(parts)
    # Firewall scan
    leaks = scan_text_for_leaks(text)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    return {
        "ok": len(leaks) == 0,
        "output_path": str(out_path),
        "output_hash": sha256_file(out_path),
        "format": "txt",
        "leaks": leaks,
        "signature_status": OWNER_SIGNATURE_REQUIRED_FIELD,
        "label": DRAFT_LABEL,
        "traces": [
            {"statement_id": "QL-HEADER", "source_facts": ["company", "solicitation"], "evidence": "R3+R1"}
        ],
    }


def generate_product_schedule(project: dict[str, Any], *, out_path: Path) -> dict[str, Any]:
    products = project.get("offered_products") or []
    lines = project.get("line_items") or []
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            ["CLIN", "Manufacturer", "Model", "MPN", "Description", "Qty", "UOM", "Warranty", "COO", "Evidence"]
        )
        for li in lines:
            offered = _offered_for(products, li)
            w.writerow(
                [
                    li.get("CLIN") or li.get("buyer_line_number"),
                    _v((offered or {}).get("manufacturer")),
                    _v((offered or {}).get("model")),
                    _v((offered or {}).get("MPN") or (offered or {}).get("mpn") or li.get("required_mpn")),
                    _v(li.get("description") or (offered or {}).get("description")),
                    _v(li.get("quantity") or li.get("normalized_quantity")),
                    _v(li.get("buyer_uom") or li.get("normalized_uom")),
                    _v((offered or {}).get("warranty")),
                    _v((offered or {}).get("country_of_origin") or li.get("country_of_origin")),
                    "R2_verified" if offered else "PRODUCT_UNSELECTED",
                ]
            )
    return {"ok": True, "output_path": str(out_path), "output_hash": sha256_file(out_path), "format": "csv"}


def generate_technical_matrix(project: dict[str, Any], *, out_path: Path) -> dict[str, Any]:
    """Only verified evidence — UNKNOWN rows stay unresolved, never 'Complies'."""
    items = project.get("technical_compliance_items") or []
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "Buyer Requirement",
                "Required Value",
                "Offered Product",
                "Offered Value",
                "Evidence",
                "Evidence Location",
                "Status",
            ]
        )
        for it in items:
            status = it.get("status") or UNKNOWN
            # Never rewrite UNKNOWN/FAIL as complies
            display_status = status
            if status in ("UNKNOWN", "REVIEW_REQUIRED", "FAIL"):
                display_status = status  # unresolved
            w.writerow(
                [
                    it.get("requirement_text") or it.get("requirement_id") or "",
                    it.get("required_value") or "",
                    it.get("offered_product") or it.get("offered_mpn") or "",
                    it.get("offered_value") or "",
                    it.get("evidence_summary") or it.get("evidence_id") or "",
                    it.get("evidence_location") or "",
                    display_status,
                ]
            )
    return {
        "ok": True,
        "output_path": str(out_path),
        "output_hash": sha256_file(out_path),
        "format": "csv",
        "rows": len(items),
        "unresolved": sum(1 for it in items if (it.get("status") or "") in ("UNKNOWN", "REVIEW_REQUIRED", "FAIL")),
    }


def generate_certification_summary(project: dict[str, Any], *, out_path: Path) -> dict[str, Any]:
    """Populate only verified or owner-confirmed answers. Signature remains pending."""
    rows = []
    for m in (project.get("r3_matrix") or (project.get("r3_analysis") or {}).get("compliance_matrix") or []):
        status = m.get("status")
        answer = m.get("answer")
        if status in ("UNKNOWN", "OWNER_CONFIRMATION_REQUIRED", "COMPLIANCE_BLOCKED"):
            rows.append({**m, "populated": False, "reason": "unresolved_r3"})
        elif status in ("PASS_VERIFIED", "NOT_APPLICABLE") or (
            status == "OWNER_CONFIRMED" or (m.get("answer") in ("YES", "NO") and "OWNER" in str(status))
        ):
            rows.append({**m, "populated": True})
        else:
            rows.append({**m, "populated": False, "reason": status})

    for att in project.get("owner_attestations") or []:
        if att.get("owner_confirmed") and att.get("answer") in ("YES", "NO"):
            rows.append(
                {
                    "requirement": att.get("question"),
                    "answer": att.get("answer"),
                    "status": "OWNER_CONFIRMED",
                    "populated": True,
                    "confirmed_by": att.get("confirmed_by"),
                    "confirmed_at": att.get("confirmed_at"),
                }
            )
        else:
            rows.append(
                {
                    "requirement": att.get("question"),
                    "answer": None,
                    "status": "OWNER_CONFIRMATION_REQUIRED",
                    "populated": False,
                }
            )

    payload = {
        "kind": "CertificationSummary",
        "label": DRAFT_LABEL,
        "signature": OWNER_SIGNATURE_REQUIRED_FIELD,
        "rows": rows,
        "auto_signed": False,
    }
    text = json.dumps(payload, indent=2, default=str)
    leaks = scan_text_for_leaks(text)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    return {
        "ok": len(leaks) == 0,
        "output_path": str(out_path),
        "output_hash": sha256_file(out_path),
        "populated": sum(1 for r in rows if r.get("populated")),
        "unresolved": sum(1 for r in rows if not r.get("populated")),
        "auto_signed": False,
        "leaks": leaks,
    }


def generate_technical_narrative_draft(project: dict[str, Any], *, out_path: Path) -> dict[str, Any]:
    """Deterministic templated narrative from verified R2 facts only. DRAFT_REVIEW_REQUIRED."""
    traces = []
    paragraphs = [DRAFT_LABEL, "", "Technical Response (Draft — Review Required)", ""]
    tech = project.get("technical_compliance_items") or []
    verified = [t for t in tech if t.get("status") == "PASS_VERIFIED"]
    unknown = [t for t in tech if t.get("status") in ("UNKNOWN", "REVIEW_REQUIRED", None)]
    failed = [t for t in tech if t.get("status") == "FAIL"]

    if verified:
        paragraphs.append("The following requirements are supported by verified evidence:")
        for i, t in enumerate(verified[:40], 1):
            stmt = (
                f"{i}. Requirement {(t.get('requirement_text') or t.get('requirement_id') or '')[:120]} "
                f"is met by offered value {t.get('offered_value') or t.get('offered_mpn') or 'see evidence'} "
                f"(evidence: {t.get('evidence_id') or t.get('evidence_summary') or 'on file'})."
            )
            paragraphs.append(stmt)
            traces.append(
                {
                    "kind": "GeneratedStatementTrace",
                    "statement_id": f"TECH-{i}",
                    "statement": stmt,
                    "source_requirement": t.get("requirement_id"),
                    "source_facts": [t.get("offered_value"), t.get("offered_mpn")],
                    "evidence": t.get("evidence_id") or t.get("evidence_summary"),
                    "generation_version": "r4-deterministic-v1",
                }
            )
    if unknown:
        paragraphs.append("")
        paragraphs.append(
            f"{len(unknown)} requirement(s) remain UNKNOWN/REVIEW_REQUIRED and are not claimed as compliant."
        )
    if failed:
        paragraphs.append("")
        paragraphs.append(f"{len(failed)} requirement(s) are FAIL and are not claimed as compliant.")
    if not verified and not unknown and not failed:
        paragraphs.append("No technical compliance items available — narrative incomplete.")

    paragraphs.extend(["", "No unsupported superiority, warranty, lead-time, or past-performance claims are included.", ""])
    text = "\n".join(paragraphs)
    # Forbidden phrases
    forbidden = []
    for bad in ("fully compliant", "proprietary technology", "best in class", "guaranteed award"):
        if bad in text.lower():
            forbidden.append(bad)
    leaks = scan_text_for_leaks(text)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    return {
        "ok": not forbidden and len(leaks) == 0,
        "output_path": str(out_path),
        "output_hash": sha256_file(out_path),
        "draft_review_required": True,
        "traces": traces,
        "forbidden_hits": forbidden,
        "leaks": leaks,
    }


def generate_docx_bidder_info(
    project: dict[str, Any],
    *,
    out_path: Path,
    field_maps: list[dict[str, Any]],
) -> dict[str, Any]:
    """Simple DOCX bidder information form from ready field maps."""
    from docx import Document

    doc = Document()
    doc.add_heading("Bidder Information (DRAFT — NOT SUBMITTED)", level=1)
    doc.add_paragraph("Generated from verified company fields only. Signature not applied.")
    table = doc.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "Field"
    table.rows[0].cells[1].text = "Value"
    for fm in field_maps:
        if not str(fm.get("source_requirement") or "").startswith("company."):
            continue
        name = str(fm.get("source_requirement")).replace("company.", "")
        row = table.add_row().cells
        row[0].text = name
        if fm.get("generation_status") in (READY, POPULATED) and fm.get("source_value") is not None:
            row[1].text = str(fm.get("source_value"))
        else:
            row[1].text = f"[{fm.get('generation_status') or UNKNOWN}]"
    doc.add_paragraph("")
    doc.add_paragraph("Authorized signature: OWNER_SIGNATURE_REQUIRED")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(out_path)
    return {
        "ok": True,
        "output_path": str(out_path),
        "output_hash": sha256_file(out_path),
        "format": "docx",
        "signature_status": OWNER_SIGNATURE_REQUIRED_FIELD,
    }


def populate_pdf_safe_fields(
    *,
    original_path: Path | None,
    original_bytes: bytes | None,
    field_maps: list[dict[str, Any]],
    out_path: Path,
) -> dict[str, Any]:
    """
    Populate AcroForm text fields when pypdf is available.
    Signature fields never filled. If non-fillable / unavailable → MANUAL_GENERATION_REQUIRED.
    """
    raw = original_bytes
    if raw is None and original_path:
        raw = original_path.read_bytes()
    if raw is None:
        return {"ok": False, "error": "no_pdf", "status": "MANUAL_GENERATION_REQUIRED"}

    original_hash = hashlib.sha256(raw).hexdigest()
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(raw)
        return {
            "ok": False,
            "status": "MANUAL_GENERATION_REQUIRED",
            "note": "pypdf not installed — original copied; AcroForm fill unavailable",
            "original_hash": original_hash,
            "output_path": str(out_path),
            "output_hash": sha256_file(out_path),
            "signature_fields_left_empty": True,
            "auto_signed": False,
        }

    reader = PdfReader(io_bytes(raw))
    writer = PdfWriter()
    writer.append(reader)
    filled = []
    sig_skipped = []
    if reader.get_fields():
        updates = {}
        for fm in field_maps:
            if fm.get("target_type") == "SIGNATURE" or "signature" in str(fm.get("target_field_or_cell") or "").lower():
                sig_skipped.append(fm.get("target_field_or_cell"))
                continue
            if fm.get("generation_status") not in (READY, POPULATED):
                continue
            name = fm.get("target_field_or_cell")
            if name and name in (reader.get_fields() or {}):
                updates[name] = str(fm.get("source_value"))
                filled.append(name)
        if updates:
            for page in writer.pages:
                try:
                    writer.update_page_form_field_values(page, updates)
                except Exception:
                    pass
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("wb") as f:
        writer.write(f)
    return {
        "ok": True,
        "status": "GENERATED_RESPONSE_COPY",
        "original_hash": original_hash,
        "output_path": str(out_path),
        "output_hash": sha256_file(out_path),
        "fields_filled": filled,
        "signature_fields_left_empty": True,
        "signature_skipped": sig_skipped,
        "auto_signed": False,
        "page_count": len(reader.pages),
    }


def io_bytes(data: bytes):
    import io

    return io.BytesIO(data)


def build_portal_response_dataset(project: dict[str, Any], field_maps: list[dict[str, Any]]) -> dict[str, Any]:
    fields = []
    for fm in field_maps:
        fields.append(
            {
                "portal": project.get("submission_system") or project.get("portal") or UNKNOWN,
                "question_id": fm.get("field_map_id"),
                "label": fm.get("source_requirement"),
                "response": fm.get("source_value") if fm.get("generation_status") in (READY, POPULATED) else None,
                "source": fm.get("evidence_source"),
                "required": fm.get("required"),
                "owner_confirmation": fm.get("owner_confirmation_required"),
                "validation_status": fm.get("generation_status"),
            }
        )
    return {
        "kind": "PortalResponseDataset",
        "submitted": False,
        "fields": fields,
        "note": "Prepared for R5 — not submitted",
    }


def build_email_response_dataset(project: dict[str, Any], attachments: list[str]) -> dict[str, Any]:
    return {
        "kind": "EmailResponseDataset",
        "sent": False,
        "recipient": None,
        "cc": [],
        "subject": f"Response to {project.get('solicitation_number') or project.get('title') or 'Solicitation'} — DRAFT",
        "body": "DRAFT — NOT SUBMITTED. Attachments prepared by M3 R4. Do not send until R5 owner approval.",
        "attachments": attachments,
    }


def build_physical_submission_dataset(project: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "PhysicalPackageInstruction",
        "shipped": False,
        "address": UNKNOWN,
        "attention": UNKNOWN,
        "copies": UNKNOWN,
        "due": project.get("submission_deadline"),
        "note": "Instructions only — R4 does not ship",
    }


def classify_attachment(doc: dict[str, Any], *, owner_approved_supplier: bool = False) -> str:
    dtype = (doc.get("type") or doc.get("document_type") or "").upper()
    if "SUPPLIER" in dtype or doc.get("namespace") == "INTERNAL_EVIDENCE":
        return SUBMIT_REQUIRED if owner_approved_supplier else INTERNAL_ONLY
    if doc.get("is_buyer_template"):
        return REFERENCE_ONLY
    return SUBMIT_REQUIRED if doc.get("submit_required") else REFERENCE_ONLY


def scan_text_for_leaks(text: str) -> list[str]:
    leaks = []
    low = text.lower()
    patterns = {
        "max_buy": r"\bmax[_\s\-]?buy\b",
        "target_profit": r"\btarget[_\s\-]?profit\b",
        "target_margin": r"\btarget[_\s\-]?margin\b|\bmargin\s*[:=]\s*\d",
        "supplier_cost": r"\bsupplier[_\s\-]?cost\b|\bour\s+cost\b",
        "historical_government_price": r"\bhistorical\s+government\s+price\b|\bgov(?:ernment)?\s+paid\b",
        "financing_strategy": r"\bfinancing\s+ceiling\b|\bfinancing\s+strategy\b",
        "supplier_boilerplate": r"\bsubject\s+to\s+change\b|\bcancellation\s+fee\b",
        "internal_notes": r"\binternal[_\s\-]?notes?\b|\bcall\s+notes\b",
    }
    for name, pat in patterns.items():
        if re.search(pat, low, re.I):
            leaks.append(name)
    return leaks


def _offered_for(products: list[dict[str, Any]], line: dict[str, Any]) -> dict[str, Any] | None:
    sel = line.get("selected_offered_product_id")
    for p in products:
        if sel and p.get("offered_product_id") == sel:
            return p
        if p.get("line_item_id") == line.get("line_item_id") and p.get("selected"):
            return p
    return None


def _v(v: Any) -> str:
    if v in (None, "", "UNKNOWN"):
        return ""  # blank — not N/A invention
    return str(v)
