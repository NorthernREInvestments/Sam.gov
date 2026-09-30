"""R4 ResponsePlan — derived from R1 deliverables/requirements. No freeform package design."""

from __future__ import annotations

import re
from typing import Any

from response_engine.models import new_id
from response_engine.r4_constants import (
    ATTACHMENT_COPY,
    BUYER_CSV_TEMPLATE,
    BUYER_DOCX_FORM,
    BUYER_PDF_FORM,
    BUYER_XLSX_TEMPLATE,
    EMAIL_RESPONSE_DATA,
    GENERATED_CERTIFICATION,
    GENERATED_COMPLIANCE_MATRIX,
    GENERATED_COVER_LETTER,
    GENERATED_PRICING_SCHEDULE,
    GENERATED_PRODUCT_SCHEDULE,
    GENERATED_QUOTE_LETTER,
    GENERATED_TECHNICAL_RESPONSE,
    OTHER_REQUIRED_ATTACHMENT,
    PHYSICAL_PACKAGE_INSTRUCTION,
    PORTAL_RESPONSE_DATA,
    BUILD,
)


def build_response_plan(project: dict[str, Any]) -> dict[str, Any]:
    """Inventory what must be submitted from R1 deliverables + docs + submission system."""
    deliverables_out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(dtype: str, *, label: str, required: bool = True, source: str | None = None, filename: str | None = None, signature: bool = False):
        key = f"{dtype}:{label}:{filename or ''}"
        if key in seen:
            return
        seen.add(key)
        deliverables_out.append(
            {
                "deliverable_type": dtype,
                "label": label,
                "required": required,
                "source": source,
                "filename": filename,
                "signature_required": signature,
                "status": "PLANNED",
            }
        )

    # From R1 deliverables inventory
    for d in project.get("deliverables") or []:
        dtype = d.get("type") or "OTHER"
        mapped = _map_deliverable_type(dtype, d)
        add(
            mapped,
            label=d.get("label") or dtype,
            required=bool(d.get("required", True)),
            source=d.get("source_requirement_id") or d.get("deliverable_id"),
            signature=bool(d.get("signature_requirement")),
        )

    # Buyer template documents
    for doc in project.get("documents") or []:
        if doc.get("superseded") or doc.get("controlling_status") == "SUPERSEDED":
            continue
        name = (doc.get("filename") or doc.get("title") or "").lower()
        dtype = (doc.get("document_type") or "").upper()
        is_tmpl = doc.get("is_buyer_template") or dtype in {
            "BUYER_TEMPLATE",
            "PRICING_SHEET",
            "COST_SHEET",
            "SF1449",
            "SF33",
        }
        if not is_tmpl and not re.search(r"\.(xlsx?|xlsm|docx?|pdf|csv)$", name):
            continue
        if name.endswith(".xlsm"):
            add(BUYER_XLSX_TEMPLATE, label=doc.get("filename") or doc.get("title") or "XLSM template", source=doc.get("document_id"), filename=doc.get("filename"))
        elif name.endswith((".xlsx", ".xls")) or dtype in {"PRICING_SHEET", "COST_SHEET"} or doc.get("workbook"):
            add(BUYER_XLSX_TEMPLATE, label=doc.get("filename") or "Buyer pricing workbook", source=doc.get("document_id"), filename=doc.get("filename"))
        elif name.endswith(".docx") or name.endswith(".doc"):
            add(BUYER_DOCX_FORM, label=doc.get("filename") or "Buyer DOCX form", source=doc.get("document_id"), filename=doc.get("filename"))
        elif name.endswith(".pdf") or "sf1449" in name or "sf33" in name or dtype in {"SF1449", "SF33"}:
            add(BUYER_PDF_FORM, label=doc.get("filename") or "Buyer PDF form", source=doc.get("document_id"), filename=doc.get("filename"))
        elif name.endswith(".csv"):
            add(BUYER_CSV_TEMPLATE, label=doc.get("filename") or "Buyer CSV", source=doc.get("document_id"), filename=doc.get("filename"))

    # Generated artifacts based on response type / requirements
    blob = _req_blob(project)
    response_type = (project.get("response_type") or "").upper()
    eval_methods = [str(x).upper() for x in (project.get("evaluation_method") or [])]

    if response_type in {"RFQ", "IFB", "QUOTE"} or "LPTA" in eval_methods or not response_type:
        add(GENERATED_QUOTE_LETTER, label="Quote / offer letter", required=True)
    if re.search(r"brand[\s\-]?or[\s\-]?equal|performance\s+spec|technical\s+response|volume\s+i", blob, re.I) or "BEST_VALUE" in eval_methods:
        add(GENERATED_TECHNICAL_RESPONSE, label="Technical response / compliance narrative", required=True)
        add(GENERATED_COMPLIANCE_MATRIX, label="Technical compliance matrix", required=True)
    if project.get("line_items"):
        add(GENERATED_PRODUCT_SCHEDULE, label="Product / CLIN schedule", required=True)
        if not any(d["deliverable_type"] == BUYER_XLSX_TEMPLATE for d in deliverables_out):
            add(GENERATED_PRICING_SCHEDULE, label="Pricing schedule", required=True)
    if (project.get("owner_attestations") or project.get("r3_matrix") or project.get("r3_analysis")):
        add(GENERATED_CERTIFICATION, label="Certification / representation summary", required=True)

    # Always prepare portal/email/physical datasets when submission system known
    sub = (project.get("submission_system") or project.get("portal") or "").upper()
    if sub in {"DIBBS", "PIEE", "SAM", "OPENGOV", "BONFIRE", "PLANETBIDS"} or "PORTAL" in sub:
        add(PORTAL_RESPONSE_DATA, label=f"Portal response dataset ({sub or 'portal'})", required=True)
    if response_type == "EMAIL" or "EMAIL" in sub or re.search(r"submit\s+by\s+e-?mail", blob, re.I):
        add(EMAIL_RESPONSE_DATA, label="Email response dataset", required=True)
    if re.search(r"hand[\s\-]?deliver|sealed\s+bid|mail\s+to|physical\s+submission", blob, re.I):
        add(PHYSICAL_PACKAGE_INSTRUCTION, label="Physical submission instructions", required=True)

    # Cover letter only if useful/required
    if re.search(r"cover\s+letter|transmittal\s+letter", blob, re.I):
        add(GENERATED_COVER_LETTER, label="Cover / transmittal letter", required=True)

    # Format constraints from R1
    format_reqs = _extract_format_requirements(project)

    # Filenames from FILE_NAME requirements
    filenames = []
    for req in project.get("requirements") or []:
        if req.get("superseded"):
            continue
        if req.get("requirement_category") == "FILE_NAME":
            filenames.append(req.get("normalized_requirement") or req.get("requirement_text"))

    signatures = [d for d in deliverables_out if d.get("signature_required")]
    blockers = []
    if not (project.get("documents") or project.get("requirements")):
        blockers.append("No solicitation documents/requirements — cannot plan response")

    return {
        "kind": "ResponsePlan",
        "plan_id": new_id("PLAN"),
        "build": BUILD,
        "response_project_id": project.get("response_project_id"),
        "response_type": project.get("response_type"),
        "submission_system": project.get("submission_system") or project.get("portal"),
        "evaluation_method": project.get("evaluation_method") or [],
        "deliverables": deliverables_out,
        "format_requirements": format_reqs,
        "required_filenames": filenames,
        "volume_structure": _volume_structure(blob, format_reqs),
        "price_in_technical_prohibited": bool(
            re.search(r"no\s+pricing\s+in\s+technical|price\s+volume\s+separate|do\s+not\s+include\s+price", blob, re.I)
        ),
        "signatures_required": len(signatures),
        "blockers": blockers,
        "status": "BLOCKED" if blockers else "READY",
        "notes": "Derived from R1 deliverables/requirements — not freeform package design",
    }


def _map_deliverable_type(dtype: str, d: dict[str, Any]) -> str:
    u = (dtype or "").upper()
    label = (d.get("label") or "").lower()
    if "BUYER_TEMPLATE" in u or "pricing" in label or "cost sheet" in label:
        return BUYER_XLSX_TEMPLATE
    if "SIGNATURE" in u:
        return GENERATED_CERTIFICATION
    if "CLIN_PRICE" in u or u.endswith("PRICE") or "PRICE" == u:
        return GENERATED_PRICING_SCHEDULE
    if "FORM" in u:
        return BUYER_PDF_FORM
    if "EMAIL" in u:
        return EMAIL_RESPONSE_DATA
    if "PHYSICAL" in u:
        return PHYSICAL_PACKAGE_INSTRUCTION
    if "PORTAL" in u:
        return PORTAL_RESPONSE_DATA
    if "ATTACHMENT" in u or "UPLOAD" in u:
        return ATTACHMENT_COPY
    return OTHER_REQUIRED_ATTACHMENT


def _req_blob(project: dict[str, Any]) -> str:
    parts = []
    for r in project.get("requirements") or []:
        if r.get("superseded"):
            continue
        parts.append(r.get("requirement_text") or r.get("normalized_requirement") or "")
    for d in project.get("documents") or []:
        parts.append((d.get("text") or "")[:3000])
    return "\n".join(parts)


def _extract_format_requirements(project: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "page_limit": None,
        "font": None,
        "font_size": None,
        "margins_inches": None,
        "line_spacing": None,
        "file_types": [],
    }
    for req in project.get("requirements") or []:
        if req.get("superseded"):
            continue
        text = req.get("requirement_text") or req.get("normalized_requirement") or ""
        cat = req.get("requirement_category")
        if cat == "PAGE_LIMIT" or re.search(r"(\d+)\s*pages?\s*(?:maximum|limit|max)", text, re.I):
            m = re.search(r"(\d+)\s*pages?", text, re.I)
            if m:
                out["page_limit"] = int(m.group(1))
        if re.search(r"(\d+)\s*[-–]?\s*point|(\d+)\s*pt\b", text, re.I):
            m = re.search(r"(\d+)\s*(?:[-–]?point|pt)\b", text, re.I)
            if m:
                out["font_size"] = int(m.group(1))
        if re.search(r"1[\s\-]?inch\s+margin", text, re.I):
            out["margins_inches"] = 1.0
        if cat == "FILE_FORMAT":
            out["file_types"].append(text[:80])
    return out


def _volume_structure(blob: str, format_reqs: dict[str, Any]) -> list[dict[str, Any]]:
    vols = []
    if re.search(r"volume\s+i\b|volume\s+1\b", blob, re.I):
        vols.append({"volume": "I", "name": "Technical", "may_include_price": False})
    if re.search(r"volume\s+ii\b|volume\s+2\b|price\s+volume", blob, re.I):
        vols.append({"volume": "II", "name": "Price", "may_include_price": True})
    if re.search(r"volume\s+iii\b|past\s+performance", blob, re.I):
        vols.append({"volume": "III", "name": "Past Performance", "may_include_price": False})
    return vols
