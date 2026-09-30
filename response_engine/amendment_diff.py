"""R1.2 amendment structural + light semantic change detection."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from application_clock import now_utc
from response_engine.constants import MATERIAL, POTENTIALLY_MATERIAL, UNKNOWN_MATERIALITY
from response_engine.models import new_id

BUILD_R12 = "20260929-m3-r12-production-corpus-ocr-amendment-hardening"

AMENDMENT_CHANGE_REVIEW_REQUIRED = "AMENDMENT_CHANGE_REVIEW_REQUIRED"

_CATEGORIES = (
    "deadline",
    "question_deadline",
    "quantity",
    "uom",
    "clin",
    "product_model",
    "specification",
    "delivery",
    "fob",
    "packaging",
    "inspection",
    "acceptance",
    "evaluation",
    "set_aside",
    "eligibility",
    "submission_method",
    "required_forms",
    "certifications",
    "pricing_sheet",
    "buyer_template",
    "attachments",
    "award_basis",
    "period_of_performance",
    "address",
    "point_of_contact",
    "other",
)

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("deadline", re.compile(r"(?:bid|offer|proposal|quote|response)\s+(?:due|deadline|closing)[:\s]+([^\n.]{5,80})", re.I)),
    ("question_deadline", re.compile(r"questions?\s+(?:due|deadline)[:\s]+([^\n.]{5,80})", re.I)),
    ("quantity", re.compile(r"(?:quantity|qty)\s*[:=]?\s*(\d[\d,]*)", re.I)),
    ("uom", re.compile(r"\b(each|ea|box|case|lot|set|kit|gallon|lb|pound|unit)\b", re.I)),
    ("clin", re.compile(r"\bCLIN\s*(\d[\w\-]*)", re.I)),
    ("product_model", re.compile(r"(?:model|part\s*(?:no\.?|number|#))\s*[:=]?\s*([A-Z0-9][\w\-/]{2,})", re.I)),
    ("delivery", re.compile(r"deliver(?:y|ed)?\s+(?:within|by|to)?\s*([^\n.]{3,80})", re.I)),
    ("submission_method", re.compile(r"\b(PIEE|DIBBS|email|portal|electronic\s+submission|sealed\s+bid)\b", re.I)),
    ("evaluation", re.compile(r"\b(LPTA|best\s+value|lowest\s+price|tradeoff|price\s+plus\s+technical)\b", re.I)),
    ("set_aside", re.compile(r"\b(small\s+business|set[\s\-]?aside|8\(a\)|SDVOSB|WOSB|HUBZone)\b", re.I)),
    ("required_forms", re.compile(r"\b(SF\s*1449|SF\s*33|Bid\s*Form|Offer\s*Form|Cost\s*Sheet|Pricing\s*Sheet)\b", re.I)),
    ("certifications", re.compile(r"\b(certif\w+|representation|acknowledgment)\b", re.I)),
    ("fob", re.compile(r"\bFOB\s+([A-Za-z ]{2,40})", re.I)),
    ("period_of_performance", re.compile(r"period\s+of\s+performance[:\s]+([^\n.]{5,80})", re.I)),
]


def _utc() -> str:
    return now_utc().isoformat()


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def extract_field_candidates(text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {c: [] for c in _CATEGORIES}
    for cat, pat in _PATTERNS:
        for m in pat.finditer(text or ""):
            val = m.group(1) if m.lastindex else m.group(0)
            val = val.strip()
            if val and val not in out[cat]:
                out[cat].append(val[:120])
    # attachment refs
    for m in re.finditer(r"\b(?:Attachment|Exhibit|Appendix|Schedule)\s+([A-Z0-9]+)\b", text or "", re.I):
        label = m.group(0).strip()
        if label not in out["attachments"]:
            out["attachments"].append(label)
    return out


def structural_text_diff(before: str, after: str) -> dict[str, Any]:
    """Paragraph-level added/removed + numeric token changes."""
    b_paras = [p.strip() for p in re.split(r"\n{2,}", before or "") if p.strip()]
    a_paras = [p.strip() for p in re.split(r"\n{2,}", after or "") if p.strip()]
    b_set = {_norm(p) for p in b_paras}
    a_set = {_norm(p) for p in a_paras}
    added = [p for p in a_paras if _norm(p) not in b_set][:40]
    removed = [p for p in b_paras if _norm(p) not in a_set][:40]

    def nums(t: str) -> set[str]:
        return set(re.findall(r"\b\d[\d,]*(?:\.\d+)?\b", t or ""))

    b_nums, a_nums = nums(before), nums(after)
    return {
        "added_paragraphs": added,
        "removed_paragraphs": removed,
        "added_numbers": sorted(a_nums - b_nums)[:50],
        "removed_numbers": sorted(b_nums - a_nums)[:50],
        "before_len": len(before or ""),
        "after_len": len(after or ""),
    }


def structural_workbook_diff(before_wb: dict | None, after_wb: dict | None) -> dict[str, Any]:
    before_wb = before_wb or {}
    after_wb = after_wb or {}
    b_sheets = set(before_wb.get("sheet_names") or [])
    a_sheets = set(after_wb.get("sheet_names") or [])
    b_cells = {
        f"{s.get('name')}:{c.get('addr')}": c.get("value")
        for s in (before_wb.get("sheets") or [])
        for c in (s.get("sample_cells") or [])
    }
    a_cells = {
        f"{s.get('name')}:{c.get('addr')}": c.get("value")
        for s in (after_wb.get("sheets") or [])
        for c in (s.get("sample_cells") or [])
    }
    changed = []
    for k, v in a_cells.items():
        if k in b_cells and b_cells[k] != v:
            changed.append({"cell": k, "old": b_cells[k], "new": v})
    return {
        "sheets_added": sorted(a_sheets - b_sheets),
        "sheets_removed": sorted(b_sheets - a_sheets),
        "cells_changed": changed[:100],
        "formulas_preserved_flag": after_wb.get("formulas_preserved"),
    }


def classify_change(
    category: str,
    old_value: str | None,
    new_value: str | None,
    *,
    confidence: str = "MEDIUM",
) -> dict[str, Any]:
    material_cats = {
        "deadline",
        "question_deadline",
        "quantity",
        "product_model",
        "specification",
        "delivery",
        "submission_method",
        "required_forms",
        "pricing_sheet",
        "buyer_template",
        "evaluation",
        "clin",
    }
    mat = MATERIAL if category in material_cats else POTENTIALLY_MATERIAL
    if old_value and new_value and _norm(old_value) == _norm(new_value):
        return {}
    if confidence == "LOW":
        return {
            "change_id": new_id("CHG"),
            "category": category,
            "old_value": old_value,
            "new_value": new_value,
            "semantic_summary": AMENDMENT_CHANGE_REVIEW_REQUIRED,
            "materiality": UNKNOWN_MATERIALITY,
            "confidence": "LOW",
            "requires_recompile": True,
            "owner_review_required": True,
        }
    summary = f"{category}: {old_value or '∅'} → {new_value or '∅'}"
    return {
        "change_id": new_id("CHG"),
        "category": category,
        "old_value": old_value,
        "new_value": new_value,
        "semantic_summary": summary,
        "materiality": mat,
        "confidence": confidence,
        "requires_recompile": category in material_cats,
        "owner_review_required": confidence != "HIGH" or category in {"specification", "evaluation"},
    }


def diff_documents(
    before_doc: dict[str, Any],
    after_doc: dict[str, Any],
    *,
    amendment_id: str | None = None,
) -> dict[str, Any]:
    before_text = before_doc.get("text") or ""
    after_text = after_doc.get("text") or ""
    struct = structural_text_diff(before_text, after_text)
    wb = structural_workbook_diff(before_doc.get("workbook"), after_doc.get("workbook"))
    b_fields = extract_field_candidates(before_text)
    a_fields = extract_field_candidates(after_text)

    changes: list[dict[str, Any]] = []
    for cat in _CATEGORIES:
        b_vals = b_fields.get(cat) or []
        a_vals = a_fields.get(cat) or []
        if not b_vals and not a_vals:
            continue
        if [_norm(x) for x in b_vals] != [_norm(x) for x in a_vals]:
            # pick first differing
            old_v = b_vals[0] if b_vals else None
            new_v = a_vals[0] if a_vals else None
            # numeric / deadline → high confidence when both present and differ
            conf = "HIGH" if old_v and new_v and cat in {"deadline", "quantity", "question_deadline"} else "MEDIUM"
            if not old_v or not new_v:
                conf = "MEDIUM"
            ch = classify_change(cat, old_v, new_v, confidence=conf)
            if ch:
                ch["amendment_id"] = amendment_id
                ch["source_before"] = before_doc.get("document_id")
                ch["source_after"] = after_doc.get("document_id")
                ch["affected_requirements"] = []
                changes.append(ch)

    if wb.get("cells_changed") or wb.get("sheets_added") or wb.get("sheets_removed"):
        ch = classify_change(
            "buyer_template" if after_doc.get("is_buyer_template") else "pricing_sheet",
            f"{len(wb.get('cells_changed') or [])} cells / sheets-{wb.get('sheets_removed')}",
            f"changed:{len(wb.get('cells_changed') or [])} added_sheets:{wb.get('sheets_added')}",
            confidence="HIGH" if wb.get("cells_changed") else "MEDIUM",
        )
        if ch:
            ch["amendment_id"] = amendment_id
            ch["source_before"] = before_doc.get("document_id")
            ch["source_after"] = after_doc.get("document_id")
            ch["workbook_diff"] = wb
            changes.append(ch)

    unresolved = not changes and (struct["added_paragraphs"] or struct["removed_paragraphs"] or struct["added_numbers"])
    if unresolved:
        changes.append(
            {
                "change_id": new_id("CHG"),
                "amendment_id": amendment_id,
                "category": "other",
                "old_value": None,
                "new_value": None,
                "semantic_summary": AMENDMENT_CHANGE_REVIEW_REQUIRED,
                "materiality": UNKNOWN_MATERIALITY,
                "confidence": "LOW",
                "requires_recompile": True,
                "owner_review_required": True,
                "source_before": before_doc.get("document_id"),
                "source_after": after_doc.get("document_id"),
                "structural": {
                    "added_count": len(struct["added_paragraphs"]),
                    "removed_count": len(struct["removed_paragraphs"]),
                    "added_numbers": struct["added_numbers"][:10],
                },
            }
        )

    return {
        "kind": "AmendmentDiff",
        "build": BUILD_R12,
        "amendment_id": amendment_id,
        "before_document_id": before_doc.get("document_id"),
        "after_document_id": after_doc.get("document_id"),
        "structural": struct,
        "workbook": wb,
        "changes": changes,
        "change_count": len(changes),
        "owner_review_required": any(c.get("owner_review_required") for c in changes),
        "computed_at": _utc(),
    }


def apply_amendment_diff_to_project(
    project: dict[str, Any],
    *,
    before_doc: dict[str, Any],
    after_doc: dict[str, Any],
    amendment_number: str | None = None,
) -> dict[str, Any]:
    """Attach diff, invalidate affected requirement categories, mark package stale when material."""
    from response_engine.document_graph import invalidate_requirements_for_amendment, register_amendment

    amd = None
    if amendment_number:
        amd = register_amendment(
            project,
            number=str(amendment_number),
            document_id=after_doc.get("document_id"),
            changes={"other": True},
            materiality=MATERIAL,
        )
    diff = diff_documents(before_doc, after_doc, amendment_id=(amd or {}).get("amendment_id"))
    project.setdefault("amendment_diffs", []).append(diff)
    project.setdefault("amendment_review_queue", [])
    if diff.get("owner_review_required"):
        project["amendment_review_queue"].append(
            {
                "amendment_id": diff.get("amendment_id"),
                "amendment_number": amendment_number,
                "change_count": diff.get("change_count"),
                "summary": [c.get("semantic_summary") for c in diff.get("changes") or []][:8],
            }
        )

    hints = set()
    for c in diff.get("changes") or []:
        cat = c.get("category")
        if cat in {"quantity", "uom"}:
            hints.add("QUANTITY")
        elif cat in {"deadline", "question_deadline"}:
            hints.add("SUBMISSION_DEADLINE")
        elif cat in {"product_model", "specification"}:
            hints.add("EXACT_BRAND")
            hints.add("SALIENT_CHARACTERISTIC")
        elif cat == "delivery":
            hints.add("DELIVERY")
        elif cat == "submission_method":
            hints.add("SUBMISSION_METHOD")
        elif cat in {"required_forms", "certifications", "pricing_sheet", "buyer_template"}:
            hints.add("FORM")
            hints.add("CERTIFICATION")
        if c.get("requires_recompile"):
            c["affected_requirements"] = invalidate_requirements_for_amendment(
                project, category_hints=hints or None, amendment_number=amendment_number
            )
    if any(c.get("materiality") == MATERIAL for c in diff.get("changes") or []):
        project["response_status"] = "PACKAGE_STALE_DUE_TO_AMENDMENT"
        project["package_stale"] = True
    return diff
