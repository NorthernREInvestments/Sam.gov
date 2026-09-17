"""Procurement Document Intelligence Extraction Engine — research only.

Convert recovered procurement documents into structured commercial purchase
intelligence. Never invent products, quantities, accessories, prices, or BOMs.
Missing evidence → UNKNOWN + missing-evidence list.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections import defaultdict
from copy import deepcopy
from typing import Any

from application_clock import now_utc
from m3_procurement_package import (
    BOM_CONFIRMED,
    BOM_LIKELY,
    BOM_UNKNOWN,
    MATCH_HIGH,
    MATCH_LOW,
    MATCH_MEDIUM,
    MATCH_UNKNOWN,
    READY_FOR_ECONOMICS,
    READY_FOR_PRICING,
    VAL_ESTIMATED,
    VAL_UNKNOWN,
    VAL_VERIFIED,
    build_commercial_requirement_profile,
    build_package_product_identity,
    build_procurement_bom,
    build_product_configuration_profile,
    validate_identifier,
)
from portal_document_resolver import classify_portal_family, extract_jaggaer_product_line_items

log = logging.getLogger("govtracker.m3_document_intelligence")

INTEL_INDEX_KEY = "m3_document_intelligence_v1"
LEARNING_INDEX_KEY = "m3_document_intelligence_learning_v1"

# Processing / extraction statuses
NOT_PROCESSED = "NOT_PROCESSED"
PROCESSING = "PROCESSING"
PROCESSED = "PROCESSED"
FAILED = "FAILED"
REQUIRES_REVIEW = "REQUIRES_REVIEW"

# Economics readiness
PARTIAL = "PARTIAL"
BLOCKED = "BLOCKED"

# Priority portal families (Phase 12)
PRIORITY_FAMILIES = frozenset({"IOWA", "MONTANA", "PHOENIX", "SOURCEWELL", "JAGGAER"})

VA_ALLOWED_ACTIONS = frozenset(
    {
        "REVIEW_EXTRACTION",
        "CORRECT_EXTRACTION",
        "FLAG_MISSING_INFORMATION",
        "ATTACH_NOTES",
        "NOTE",
        "UPDATE_STATUS",
        "MARK_REQUIRES_REVIEW",
    }
)
VA_FORBIDDEN_ACTIONS = frozenset(
    {
        "APPROVE_DEAL",
        "CHANGE_SCORING",
        "SUBMIT_BID",
        "CONTACT_SUPPLIER",
        "SPEND_MONEY",
        "INVENT_LINE_ITEMS",
    }
)

SIGNAL_PATTERNS: list[tuple[str, re.Pattern[str], int]] = [
    ("pricing", re.compile(r"\b(price\s*schedule|unit\s*price|extended\s*price|bid\s*schedule)\b", re.I), 100),
    ("schedule", re.compile(r"\b(delivery\s*schedule|performance\s*schedule|milestone)\b", re.I), 80),
    ("bom", re.compile(r"\b(bill\s+of\s+materials|product\s+line\s+items|line\s+item|bom)\b", re.I), 95),
    ("technical", re.compile(r"\b(technical\s+spec|specification|salient\s+characteristic)\b", re.I), 70),
    ("quantity", re.compile(r"\b(quantity|qty|estimated\s+quantity)\b", re.I), 85),
    ("manufacturer", re.compile(r"\b(manufacturer|oem|brand|make)\b", re.I), 75),
    ("part_number", re.compile(r"\b(part\s*#|part\s*number|p/?n|model\s*number|nsn|cage)\b", re.I), 90),
    ("delivery", re.compile(r"\b(delivery|fob|place\s+of\s+performance|ship\s+to)\b", re.I), 65),
    ("options", re.compile(r"\b(option\s+year|optional|alternate)\b", re.I), 55),
    ("warranty", re.compile(r"\b(warranty|guarantee)\b", re.I), 50),
    ("support", re.compile(r"\b(support|maintenance|sla|service\s+level)\b", re.I), 50),
    ("product_description", re.compile(r"\b(description|item\s+name|commodity)\b", re.I), 60),
]

QTY_LINE_RE = re.compile(
    r"(?P<desc>.{8,120}?)\s+(?P<qty>\d+(?:\.\d+)?)\s*(?P<unit>EA|EACH|LS|LB|GAL|FT|KIT|SET|BOX|CS)\b",
    re.I,
)
MFR_RE = re.compile(
    r"\b(Cisco|Dell|HP|HPE|Lenovo|Apple|Microsoft|Motorola|Ford|Chevrolet|Caterpillar|"
    r"John\s+Deere|Polaris|Honda|Toyota|3M|Honeywell|Motorola|Axis|Bosch)\b",
    re.I,
)
VALUE_RE = re.compile(
    r"(?:estimated?\s+(?:value|cost|amount)|award\s+(?:amount|value)|contract\s+(?:value|amount)|"
    r"ceiling|not\s+to\s+exceed|NTE|total\s+(?:price|amount)|budget)[:\s]*\$?\s*([\d,]+(?:\.\d{2})?)",
    re.I,
)
DELIVERY_RE = re.compile(
    r"(?:delivery\s+(?:date|required|within)|required\s+delivery|fob\s+\w+|place\s+of\s+performance)"
    r"[:\s]+([^\n]{5,80})",
    re.I,
)


def _utc() -> str:
    return now_utc().isoformat()


def _known(v: Any) -> bool:
    return v not in {None, "", "UNKNOWN", "unknown"}


def _num(v: Any) -> float | None:
    if v is None or v == "" or v == "UNKNOWN":
        return None
    try:
        return float(str(v).replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return None


def _doc_id(doc: dict[str, Any], index: int) -> str:
    raw = str(doc.get("content_fingerprint") or doc.get("url") or doc.get("download_url") or doc.get("title") or index)
    return hashlib.sha256(f"{raw}:{index}".encode("utf-8", errors="ignore")).hexdigest()[:16]


def load_intelligence_index() -> dict[str, Any]:
    try:
        from models import AppSetting, session_scope

        with session_scope() as session:
            row = session.get(AppSetting, INTEL_INDEX_KEY)
            if row and isinstance(row.value, dict):
                return deepcopy(row.value)
    except Exception:
        log.debug("document intelligence index load failed", exc_info=True)
    return {"kind": "M3DocumentIntelligenceIndex", "by_id": {}, "updated_at": None}


def save_intelligence_index(index: dict[str, Any]) -> None:
    index = deepcopy(index)
    index["kind"] = "M3DocumentIntelligenceIndex"
    index["updated_at"] = _utc()
    try:
        from models import AppSetting, session_scope

        with session_scope() as session:
            row = session.get(AppSetting, INTEL_INDEX_KEY)
            if row is None:
                session.add(AppSetting(key=INTEL_INDEX_KEY, value=index))
            else:
                row.value = index
    except Exception:
        log.debug("document intelligence index save failed", exc_info=True)


def load_learning_index() -> dict[str, Any]:
    try:
        from models import AppSetting, session_scope

        with session_scope() as session:
            row = session.get(AppSetting, LEARNING_INDEX_KEY)
            if row and isinstance(row.value, dict):
                return deepcopy(row.value)
    except Exception:
        log.debug("document intelligence learning load failed", exc_info=True)
    return {
        "kind": "M3DocumentIntelligenceLearning",
        "by_portal_family": {},
        "by_document_type": {},
        "updated_at": None,
    }


def save_learning_index(index: dict[str, Any]) -> None:
    index = deepcopy(index)
    index["kind"] = "M3DocumentIntelligenceLearning"
    index["updated_at"] = _utc()
    try:
        from models import AppSetting, session_scope

        with session_scope() as session:
            row = session.get(AppSetting, LEARNING_INDEX_KEY)
            if row is None:
                session.add(AppSetting(key=LEARNING_INDEX_KEY, value=index))
            else:
                row.value = index
    except Exception:
        log.debug("document intelligence learning save failed", exc_info=True)


def detect_file_type(doc: dict[str, Any]) -> str:
    fmt = str(doc.get("format") or "").upper()
    if fmt in {"PDF", "DOCX", "XLSX", "XLS", "CSV", "HTML", "JSON", "TXT", "ZIP"}:
        return fmt
    name = str(doc.get("name") or doc.get("title") or doc.get("filename") or "").lower()
    url = str(doc.get("url") or doc.get("download_url") or "").lower()
    hay = f"{name} {url}"
    for ext, label in (
        (".pdf", "PDF"),
        (".docx", "DOCX"),
        (".doc", "DOC"),
        (".xlsx", "XLSX"),
        (".xls", "XLS"),
        (".csv", "CSV"),
        (".html", "HTML"),
        (".htm", "HTML"),
        (".json", "JSON"),
        (".zip", "ZIP"),
    ):
        if hay.endswith(ext) or ext in hay:
            return label
    if doc.get("extracted_text") or doc.get("text_preview"):
        return "TXT"
    return "UNKNOWN"


def detect_procurement_signals(text: str) -> dict[str, Any]:
    """Phase 3 — prioritize pricing / schedule / BOM / technical sections."""
    blob = text or ""
    found: list[dict[str, Any]] = []
    for name, pat, priority in SIGNAL_PATTERNS:
        matches = list(pat.finditer(blob))
        if matches:
            found.append(
                {
                    "signal": name,
                    "priority": priority,
                    "count": len(matches),
                    "sample": matches[0].group(0)[:80],
                }
            )
    found.sort(key=lambda x: -int(x["priority"]))
    return {
        "kind": "PROCUREMENT_SIGNAL_DETECTION",
        "signals": found,
        "has_pricing_section": any(s["signal"] == "pricing" for s in found),
        "has_bom_section": any(s["signal"] == "bom" for s in found),
        "has_quantity_section": any(s["signal"] == "quantity" for s in found),
        "has_technical_section": any(s["signal"] == "technical" for s in found),
        "priority_order": [s["signal"] for s in found],
    }


def _normalize_line_item(raw: dict[str, Any], *, source_document: str, index: int) -> dict[str, Any] | None:
    """Phase 4 — PROCUREMENT_LINE_ITEM; evidence required."""
    desc = str(raw.get("description") or raw.get("Item") or raw.get("name") or "").strip()
    qty = _num(raw.get("quantity") if raw.get("quantity") is not None else raw.get("Quantity"))
    unit = str(raw.get("unit") or raw.get("uom") or raw.get("Unit") or "").strip() or "UNKNOWN"
    unit_price = _num(raw.get("unit_price") or raw.get("Unit_price"))
    ext_price = _num(raw.get("extended_price") or raw.get("Extended_price"))
    mfr = str(raw.get("manufacturer") or raw.get("Manufacturer") or "").strip() or "UNKNOWN"
    model = str(raw.get("model") or raw.get("Model") or "").strip() or "UNKNOWN"
    pn_raw = raw.get("part_number") or raw.get("Part_number")
    nsn_raw = raw.get("nsn") or raw.get("NSN")

    # Must have description OR validated identifier — never empty fake lines
    pn = validate_identifier(pn_raw, source=source_document, kind="part_number")
    nsn = validate_identifier(nsn_raw, source=source_document, kind="nsn")
    if not desc and not pn.get("accepted") and not nsn.get("accepted"):
        return None
    if not desc and (pn.get("accepted") or nsn.get("accepted")):
        desc = f"Part {pn.get('Identifier') or nsn.get('Identifier')}"

    conf = str(raw.get("confidence") or raw.get("Confidence") or "").upper()
    if conf not in {BOM_CONFIRMED, BOM_LIKELY, "HIGH", "MEDIUM", "LOW"}:
        if qty is not None and desc:
            conf = "HIGH"
        elif desc:
            conf = "MEDIUM"
        else:
            conf = "LOW"
    if conf == BOM_CONFIRMED:
        conf = "HIGH"
    elif conf == BOM_LIKELY:
        conf = "MEDIUM"

    return {
        "kind": "PROCUREMENT_LINE_ITEM",
        "Line_number": raw.get("line_id") or raw.get("Line_number") or index,
        "Description": desc[:300],
        "Quantity": qty if qty is not None else "UNKNOWN",
        "Unit": unit.upper()[:12] if unit != "UNKNOWN" else "UNKNOWN",
        "Unit_price": unit_price if unit_price is not None else "UNKNOWN",
        "Extended_price": ext_price if ext_price is not None else "UNKNOWN",
        "Manufacturer": mfr if _known(mfr) else "UNKNOWN",
        "Model": model if _known(model) else "UNKNOWN",
        "Part_number": pn["Identifier"] if pn.get("accepted") else "UNKNOWN",
        "NSN": nsn["Identifier"] if nsn.get("accepted") else "UNKNOWN",
        "Required_date": raw.get("required_date") or "UNKNOWN",
        "Delivery_requirement": raw.get("delivery_requirement") or "UNKNOWN",
        "Source_document": source_document,
        "Confidence": conf,
    }


def extract_line_items_from_text(text: str, *, source_document: str) -> list[dict[str, Any]]:
    """Extract evidenced line items from document text (Jaggaer + heuristics)."""
    items: list[dict[str, Any]] = []
    if not text:
        return items

    for raw in extract_jaggaer_product_line_items(text):
        norm = _normalize_line_item(raw, source_document=source_document, index=len(items) + 1)
        if norm:
            items.append(norm)

    if items:
        return items

    # Heuristic table rows — only with qty evidence
    seen: set[str] = set()
    for m in QTY_LINE_RE.finditer(text):
        desc = re.sub(r"\s+", " ", m.group("desc")).strip(" -:|")
        if len(desc) < 8:
            continue
        key = desc.lower()[:80]
        if key in seen:
            continue
        seen.add(key)
        # Skip header-like rows
        if re.search(r"\b(description|quantity|unit|price|item\s*#)\b", desc, re.I):
            continue
        mfr_m = MFR_RE.search(desc)
        norm = _normalize_line_item(
            {
                "description": desc[:200],
                "quantity": float(m.group("qty")),
                "unit": m.group("unit"),
                "manufacturer": mfr_m.group(0) if mfr_m else None,
                "confidence": "MEDIUM",
            },
            source_document=source_document,
            index=len(items) + 1,
        )
        if norm:
            items.append(norm)
        if len(items) >= 40:
            break
    return items


def extract_document_content(doc: dict[str, Any]) -> dict[str, Any]:
    """Phase 2 — extract text/tables from available document surfaces."""
    text = str(doc.get("extracted_text") or doc.get("text_preview") or doc.get("text") or "")
    tables: list[Any] = []
    line_items_raw: list[dict[str, Any]] = []
    confidence = "LOW"
    method = "stored_text"

    # Prefer in-memory bytes when present (rare on pipeline rows)
    raw_bytes = doc.get("content_bytes") or doc.get("raw_bytes")
    if isinstance(raw_bytes, (bytes, bytearray)) and raw_bytes:
        try:
            from table_extractors import extract_from_bytes

            result = extract_from_bytes(
                bytes(raw_bytes),
                filename=str(doc.get("name") or doc.get("title") or "document"),
                content_type=str(doc.get("content_type") or doc.get("mime_type") or ""),
            )
            if result.get("ok") or result.get("line_items") or result.get("rows"):
                method = result.get("extraction_method") or "bytes_dispatch"
                line_items_raw = list(result.get("line_items") or [])
                tables = list(result.get("rows") or [])[:50]
                if not text and result.get("text"):
                    text = str(result.get("text") or "")
                confidence = "HIGH" if line_items_raw else "MEDIUM"
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": str(exc)[:200],
                "text": text[:200000],
                "tables": [],
                "line_items_raw": [],
                "confidence": "LOW",
                "method": method,
            }

    if text and not line_items_raw:
        # PDF heuristic tables from text
        try:
            from table_extractors import extract_pdf_tables_heuristic

            heur = extract_pdf_tables_heuristic(text, source_name=str(doc.get("title") or "doc"))
            line_items_raw = list(heur.get("line_items") or [])
            tables = list(heur.get("rows") or [])[:50]
            if line_items_raw:
                method = "text_table_heuristic"
                confidence = "MEDIUM"
        except Exception:
            pass

    if text and len(text) > 200:
        confidence = "HIGH" if confidence == "MEDIUM" and line_items_raw else (
            "MEDIUM" if confidence == "LOW" else confidence
        )

    return {
        "ok": bool(text or line_items_raw),
        "text": text[:200000],
        "tables": tables,
        "line_items_raw": line_items_raw,
        "confidence": confidence if (text or line_items_raw) else "LOW",
        "method": method,
        "char_count": len(text),
    }


def build_document_intelligence_profile(
    doc: dict[str, Any],
    row: dict[str, Any],
    *,
    index: int = 0,
) -> dict[str, Any]:
    """Phase 1 — DOCUMENT_INTELLIGENCE_PROFILE for one document."""
    doc_id = _doc_id(doc, index)
    dtype = str(doc.get("document_type") or doc.get("type") or "unknown")
    file_type = detect_file_type(doc)
    source = str(doc.get("source") or row.get("source_id") or "UNKNOWN")

    profile: dict[str, Any] = {
        "kind": "DOCUMENT_INTELLIGENCE_PROFILE",
        "Document_ID": doc_id,
        "Opportunity_ID": row.get("canonical_id") or "UNKNOWN",
        "Document_type": dtype,
        "Source": source,
        "File_type": file_type,
        "Processing_status": PROCESSING,
        "Extraction_status": NOT_PROCESSED,
        "Confidence": "UNKNOWN",
        "Name": doc.get("name") or doc.get("title") or "UNKNOWN",
        "URL": doc.get("url") or doc.get("download_url") or "UNKNOWN",
        "bytes_recovered": bool(doc.get("bytes_recovered")),
        "Timestamp": _utc(),
    }

    # Need text, bytes, or already-linked content
    has_surface = bool(
        doc.get("extracted_text")
        or doc.get("text_preview")
        or doc.get("content_bytes")
        or doc.get("raw_bytes")
        or doc.get("bytes_recovered")
    )
    if not has_surface and not (doc.get("url") or doc.get("download_url")):
        profile["Processing_status"] = FAILED
        profile["Extraction_status"] = FAILED
        profile["Confidence"] = "LOW"
        profile["failure"] = "no_document_surface"
        return profile

    if not (doc.get("extracted_text") or doc.get("text_preview") or doc.get("content_bytes") or doc.get("raw_bytes")):
        # Linked but no extractable content yet
        profile["Processing_status"] = REQUIRES_REVIEW
        profile["Extraction_status"] = REQUIRES_REVIEW
        profile["Confidence"] = "LOW"
        profile["failure"] = "linked_unfetched_no_text"
        profile["missing_evidence"] = ["document_text_or_bytes"]
        return profile

    try:
        content = extract_document_content(doc)
        signals = detect_procurement_signals(content.get("text") or "")
        lines = extract_line_items_from_text(content.get("text") or "", source_document=str(profile["Name"]))
        # Merge table extractor raw lines only when text extractors found little
        if len(lines) < 2:
            for i, raw in enumerate(content.get("line_items_raw") or [], start=1):
                if not isinstance(raw, dict):
                    continue
                norm = _normalize_line_item(raw, source_document=str(profile["Name"]), index=len(lines) + i)
                if norm:
                    lines.append(norm)
                if len(lines) >= 40:
                    break
        else:
            # Cap any supplemental table rows — avoid heuristic flood on Jaggaer PDFs
            for i, raw in enumerate((content.get("line_items_raw") or [])[:5], start=1):
                if not isinstance(raw, dict):
                    continue
                norm = _normalize_line_item(raw, source_document=str(profile["Name"]), index=len(lines) + i)
                if not norm:
                    continue
                key = str(norm.get("Description") or "").lower()[:60]
                if any(str(x.get("Description") or "").lower()[:60] == key for x in lines):
                    continue
                lines.append(norm)

        profile["Processing_status"] = PROCESSED
        profile["Extraction_status"] = PROCESSED if (content.get("text") or lines) else REQUIRES_REVIEW
        profile["Confidence"] = content.get("confidence") or "LOW"
        profile["extraction_method"] = content.get("method")
        profile["char_count"] = content.get("char_count") or 0
        profile["PROCUREMENT_SIGNALS"] = signals
        profile["PROCUREMENT_LINE_ITEMS"] = lines
        profile["table_row_count"] = len(content.get("tables") or [])
        profile["text_excerpt"] = (content.get("text") or "")[:2000]
        if not lines and not signals.get("signals"):
            profile["Extraction_status"] = REQUIRES_REVIEW
            profile["missing_evidence"] = ["line_items", "pricing_section"]
        elif not lines:
            profile["Extraction_status"] = REQUIRES_REVIEW
            profile["missing_evidence"] = ["line_items"]
    except Exception as exc:  # noqa: BLE001
        log.exception("document intelligence extraction failed")
        profile["Processing_status"] = FAILED
        profile["Extraction_status"] = FAILED
        profile["Confidence"] = "LOW"
        profile["failure"] = str(exc)[:300]

    return profile


def build_configuration_requirement_profile(
    row: dict[str, Any],
    identity: dict[str, Any],
) -> dict[str, Any]:
    """Phase 6 — CONFIGURATION_REQUIREMENT_PROFILE (evidence only)."""
    cfg = build_product_configuration_profile(row, identity)
    items: list[dict[str, Any]] = []
    by_type = cfg.get("items_by_type") or {}
    for role, entries in by_type.items():
        for e in entries or []:
            if not isinstance(e, dict):
                continue
            items.append(
                {
                    "Description": e.get("Item") or "UNKNOWN",
                    "Manufacturer": e.get("Manufacturer") or "UNKNOWN",
                    "Part_number": e.get("Part_number") or "UNKNOWN",
                    "Quantity": e.get("Quantity") if e.get("Quantity") is not None else "UNKNOWN",
                    "Required_or_optional": e.get("Required_or_optional") or "UNKNOWN",
                    "Evidence_source": e.get("Evidence_source") or "UNKNOWN",
                    "Confidence": e.get("Confidence") or BOM_UNKNOWN,
                    "config_type": role,
                }
            )
    return {
        "kind": "CONFIGURATION_REQUIREMENT_PROFILE",
        "items": items,
        "PRIMARY_EQUIPMENT": cfg.get("PRIMARY_EQUIPMENT") or [],
        "REQUIRED_ACCESSORIES": cfg.get("REQUIRED_ACCESSORIES") or [],
        "items_by_type": by_type,
        "configuration_complete": bool(cfg.get("configuration_complete")),
        "notes": cfg.get("notes") or ["never_assume_accessories"],
        "underlying": "PRODUCT_CONFIGURATION_PROFILE",
    }


def build_economics_readiness_score(
    *,
    identity: dict[str, Any],
    configuration: dict[str, Any],
    bom: dict[str, Any],
    commercial: dict[str, Any],
    line_items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Phase 9 — ECONOMICS_READINESS_SCORE."""
    product_ok = identity.get("Identity_confidence") in {MATCH_HIGH, MATCH_MEDIUM}
    qty_ok = _known(identity.get("Quantity")) or any(
        _known(li.get("Quantity")) for li in line_items if isinstance(li, dict)
    )
    config_ok = bool(configuration.get("configuration_complete"))
    value_conf = (commercial.get("Contract_value_model") or {}).get("confidence") or commercial.get("Confidence")
    value_ok = value_conf in {VAL_VERIFIED, VAL_ESTIMATED}
    pricing_inputs = product_ok and qty_ok
    supplier_possible = product_ok and (
        _known(identity.get("Manufacturer_part_number"))
        or _known(identity.get("NSN"))
        or _known(identity.get("Model_number"))
    )
    cost_eligible = len(bom.get("cost_eligible_lines") or []) > 0

    flags = {
        "Product_identified": product_ok,
        "Quantity_known": qty_ok,
        "Configuration_known": config_ok,
        "Value_known": value_ok,
        "Pricing_inputs_available": pricing_inputs,
        "Supplier_research_possible": supplier_possible,
        "BOM_cost_eligible": cost_eligible,
    }
    score = sum(10 for v in flags.values() if v)
    # Bonuses
    if identity.get("Identity_confidence") == MATCH_HIGH:
        score += 15
    if value_conf == VAL_VERIFIED:
        score += 10
    if cost_eligible and qty_ok:
        score += 10

    missing = []
    if not product_ok:
        missing.append("exact product identity")
    if not qty_ok:
        missing.append("quantity")
    if not config_ok:
        missing.append("complete configuration")
    if not value_ok:
        missing.append("contract value")
    if not cost_eligible:
        missing.append("confirmed BOM lines for cost")

    if product_ok and qty_ok and cost_eligible and (config_ok or not (configuration.get("REQUIRED_ACCESSORIES") or [])):
        if value_ok:
            status = READY_FOR_ECONOMICS
        else:
            status = READY_FOR_PRICING
    elif product_ok or qty_ok or cost_eligible:
        status = PARTIAL
    else:
        status = BLOCKED

    return {
        "kind": "ECONOMICS_READINESS_SCORE",
        "score": min(100, score),
        "status": status,
        "flags": flags,
        "missing": missing,
        "Identity_confidence": identity.get("Identity_confidence") or MATCH_UNKNOWN,
        "Value_confidence": value_conf or VAL_UNKNOWN,
        "BOM_completeness_pct": bom.get("BOM_completeness_pct") or 0,
    }


def update_learning_stats(
    *,
    portal_family: str,
    document_type: str,
    profile: dict[str, Any],
    fields_found: list[str],
) -> None:
    """Phase 10 — track extraction success by portal family / document type."""
    idx = load_learning_index()
    by_fam = idx.setdefault("by_portal_family", {})
    by_dtype = idx.setdefault("by_document_type", {})
    fam = by_fam.setdefault(
        portal_family,
        {"attempts": 0, "processed": 0, "failed": 0, "line_items_total": 0, "fields": {}},
    )
    fam["attempts"] = int(fam.get("attempts") or 0) + 1
    status = profile.get("Processing_status")
    if status == PROCESSED:
        fam["processed"] = int(fam.get("processed") or 0) + 1
    elif status == FAILED:
        fam["failed"] = int(fam.get("failed") or 0) + 1
    fam["line_items_total"] = int(fam.get("line_items_total") or 0) + len(
        profile.get("PROCUREMENT_LINE_ITEMS") or []
    )
    fields = fam.setdefault("fields", {})
    if not isinstance(fields, dict):
        fields = {}
        fam["fields"] = fields
    for f in fields_found:
        fields[f] = int(fields.get(f) or 0) + 1
    fam["success_rate"] = round(
        100.0 * int(fam.get("processed") or 0) / max(1, int(fam.get("attempts") or 1)), 1
    )
    by_fam[portal_family] = fam

    dt = by_dtype.setdefault(document_type, {"attempts": 0, "processed": 0, "fields": {}})
    dt["attempts"] = int(dt.get("attempts") or 0) + 1
    if status == PROCESSED:
        dt["processed"] = int(dt.get("processed") or 0) + 1
    for f in fields_found:
        dt.setdefault("fields", {})[f] = int(dt.get("fields", {}).get(f) or 0) + 1
    by_dtype[document_type] = dt
    save_learning_index(idx)


def extract_opportunity_intelligence(
    row: dict[str, Any],
    *,
    update_learning: bool = True,
) -> dict[str, Any]:
    """Full document → structured commercial package for one opportunity."""
    working = deepcopy(row)
    family = classify_portal_family(working)
    docs = list(working.get("documents") or []) if isinstance(working.get("documents"), list) else []

    # Also process governing / attachment text fields as synthetic docs
    for key, dtype in (
        ("governing_text", "solicitation"),
        ("attachment_text", "attachment"),
        ("solicitation_text", "solicitation"),
        ("evidence_text_excerpt", "attachment"),
    ):
        text = working.get(key)
        if text and isinstance(text, str) and len(text) > 80:
            docs.append(
                {
                    "title": key,
                    "name": key,
                    "document_type": dtype,
                    "extracted_text": text[:200000],
                    "bytes_recovered": True,
                    "source": "pipeline_text_field",
                }
            )

    profiles: list[dict[str, Any]] = []
    all_lines: list[dict[str, Any]] = []
    for i, doc in enumerate(docs):
        if not isinstance(doc, dict):
            continue
        prof = build_document_intelligence_profile(doc, working, index=i)
        profiles.append(prof)
        for li in prof.get("PROCUREMENT_LINE_ITEMS") or []:
            all_lines.append(li)

    # Preserve existing evidenced line items
    existing = working.get("line_items") or working.get("bom") or []
    if isinstance(existing, list):
        for i, li in enumerate(existing):
            if not isinstance(li, dict):
                continue
            norm = _normalize_line_item(li, source_document="existing_line_items", index=i + 1)
            if norm:
                # Prefer existing if already present
                key = str(norm.get("Description") or "").lower()[:80]
                if not any(str(x.get("Description") or "").lower()[:80] == key for x in all_lines):
                    all_lines.append(norm)

    # Deduplicate line items
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for li in all_lines:
        key = f"{li.get('Line_number')}|{str(li.get('Description') or '').lower()[:60]}|{li.get('Quantity')}"
        if key in seen:
            continue
        seen.add(key)
        deduped.append(li)

    # Patch working row for downstream package builders (evidence only)
    if deduped:
        working["line_items"] = [
            {
                "line_id": li.get("Line_number"),
                "description": li.get("Description"),
                "quantity": li.get("Quantity") if li.get("Quantity") != "UNKNOWN" else None,
                "unit": li.get("Unit") if li.get("Unit") != "UNKNOWN" else "EA",
                "part_number": li.get("Part_number") if li.get("Part_number") != "UNKNOWN" else None,
                "manufacturer": li.get("Manufacturer") if li.get("Manufacturer") != "UNKNOWN" else None,
                "nsn": li.get("NSN") if li.get("NSN") != "UNKNOWN" else None,
                "unit_price": li.get("Unit_price") if li.get("Unit_price") != "UNKNOWN" else None,
                "confidence": li.get("Confidence"),
                "source": li.get("Source_document"),
            }
            for li in deduped
        ]
        working["bom"] = working["line_items"]

    # Aggregate text for commercial / identity extractors
    text_parts = []
    for p in profiles:
        if p.get("text_excerpt"):
            text_parts.append(str(p["text_excerpt"]))
    if text_parts and not working.get("attachment_text"):
        working["attachment_text"] = "\n".join(text_parts)[:50000]

    identity = build_package_product_identity(working)
    configuration = build_configuration_requirement_profile(working, identity)
    # BOM builder expects PRODUCT_CONFIGURATION_PROFILE shape
    cfg_for_bom = {
        "items_by_type": configuration.get("items_by_type") or {},
        "PRIMARY_EQUIPMENT": configuration.get("PRIMARY_EQUIPMENT") or [],
        "REQUIRED_ACCESSORIES": configuration.get("REQUIRED_ACCESSORIES") or [],
        "configuration_complete": configuration.get("configuration_complete"),
    }
    bom = build_procurement_bom(working, identity, cfg_for_bom)
    commercial = build_commercial_requirement_profile(working)

    # Enrich commercial value from document text if still unknown
    blob = " ".join(text_parts)[:12000]
    values_found = []
    for m in VALUE_RE.finditer(blob):
        n = _num(m.group(1))
        if n is not None:
            values_found.append({"Value": n, "Source": "document_text", "Confidence": VAL_ESTIMATED})
    if values_found and not _known(commercial.get("Estimated_contract_value")):
        commercial = {
            **commercial,
            "Estimated_contract_value": values_found[0]["Value"],
            "Confidence": VAL_ESTIMATED,
            "value_extractions": values_found[:5],
        }
    delivery_hits = [m.group(0)[:100] for m in DELIVERY_RE.finditer(blob)][:5]
    pricing_sections = sum(
        1
        for p in profiles
        if (p.get("PROCUREMENT_SIGNALS") or {}).get("has_pricing_section")
    )

    readiness = build_economics_readiness_score(
        identity=identity,
        configuration=configuration,
        bom=bom,
        commercial=commercial,
        line_items=deduped,
    )

    processed = sum(1 for p in profiles if p.get("Processing_status") == PROCESSED)
    failed = sum(1 for p in profiles if p.get("Processing_status") == FAILED)
    review = sum(1 for p in profiles if p.get("Processing_status") == REQUIRES_REVIEW)

    if update_learning:
        for p in profiles:
            fields = []
            if p.get("PROCUREMENT_LINE_ITEMS"):
                fields.append("line_items")
            sig = p.get("PROCUREMENT_SIGNALS") or {}
            fields.extend(sig.get("priority_order") or [])
            update_learning_stats(
                portal_family=family,
                document_type=str(p.get("Document_type") or "unknown"),
                profile=p,
                fields_found=fields,
            )

    products = []
    if _known(identity.get("Manufacturer")) or _known(identity.get("Description")):
        products.append(
            {
                "Manufacturer": identity.get("Manufacturer"),
                "Model": identity.get("Model_number"),
                "Part_number": identity.get("Manufacturer_part_number"),
                "NSN": identity.get("NSN"),
                "Description": identity.get("Description"),
                "Confidence": identity.get("Identity_confidence"),
            }
        )

    missing = list(readiness.get("missing") or [])
    if not profiles:
        missing.append("no documents to process")

    result = {
        "kind": "M3DocumentIntelligence",
        "Opportunity_ID": working.get("canonical_id") or "UNKNOWN",
        "Portal_family": family,
        "DOCUMENT_INTELLIGENCE_PROFILES": profiles,
        "Documents_analyzed": len(profiles),
        "Processed": processed,
        "Failed": failed,
        "Requires_review": review,
        "PROCUREMENT_LINE_ITEMS": deduped,
        "PRODUCT_IDENTITY": identity,
        "PRODUCT_IDENTITY_CONFIDENCE": identity.get("Identity_confidence") or MATCH_UNKNOWN,
        "CONFIGURATION_REQUIREMENT_PROFILE": configuration,
        "PROCUREMENT_BOM": bom,
        "COMMERCIAL_REQUIREMENT": commercial,
        "ECONOMICS_READINESS_SCORE": readiness,
        "Products_identified": products,
        "Models_found": [
            identity.get("Model_number")
        ]
        if _known(identity.get("Model_number"))
        else [],
        "Part_numbers_found": [
            identity.get("Manufacturer_part_number")
        ]
        if _known(identity.get("Manufacturer_part_number"))
        else [],
        "Quantities_found": [
            li.get("Quantity") for li in deduped if _known(li.get("Quantity"))
        ],
        "Values_found": values_found,
        "Pricing_sections_found": pricing_sections,
        "Delivery_requirements_found": delivery_hits,
        "Missing_information": missing,
        "VA": {
            "allowed_actions": sorted(VA_ALLOWED_ACTIONS),
            "forbidden_actions": sorted(VA_FORBIDDEN_ACTIONS),
            "role": "DOCUMENT_INTELLIGENCE_OPERATOR",
            "may_approve_deals": False,
            "may_change_scoring": False,
            "may_submit_bids": False,
            "may_contact_suppliers": False,
        },
        "working_row_patch": {
            "line_items": working.get("line_items"),
            "bom": working.get("bom"),
            "attachment_text": working.get("attachment_text"),
            "document_intelligence": None,  # filled below
        },
        "Timestamp": _utc(),
        "DEVELOPMENT_NO_OUTREACH": True,
    }
    result["working_row_patch"]["document_intelligence"] = {
        k: result.get(k)
        for k in (
            "kind",
            "Opportunity_ID",
            "Portal_family",
            "Processed",
            "Failed",
            "Requires_review",
            "PRODUCT_IDENTITY_CONFIDENCE",
            "ECONOMICS_READINESS_SCORE",
            "Missing_information",
            "Timestamp",
        )
    }
    result["working_row_patch"]["document_intelligence"]["line_item_count"] = len(deduped)
    result["working_row_patch"]["document_intelligence"]["bom_line_count"] = bom.get("line_count")
    result["working_row_patch"]["procurement_package_intelligence"] = {
        "kind": "M3ProcurementPackageIntelligence",
        "COMMERCIAL_REQUIREMENT": commercial,
        "PRODUCT_IDENTITY": identity,
        "PRODUCT_CONFIGURATION": cfg_for_bom,
        "PROCUREMENT_BOM": bom,
        "ECONOMICS_HANDOFF": {
            "can_calculate": readiness.get("status") == READY_FOR_ECONOMICS,
            "missing": missing,
            "message": f"Economics readiness: {readiness.get('status')}",
        },
        "RESEARCH_READINESS": {"RESEARCH_QUEUE": readiness.get("status")},
        "source": "document_intelligence_extraction",
    }
    return result


def _priority_score(row: dict[str, Any]) -> tuple[int, str]:
    family = classify_portal_family(row)
    docs = row.get("documents") if isinstance(row.get("documents"), list) else []
    with_bytes = sum(
        1
        for d in docs
        if isinstance(d, dict) and (d.get("bytes_recovered") or d.get("extracted_text") or d.get("text_preview"))
    )
    has_text = 1 if any(
        row.get(k) for k in ("governing_text", "attachment_text", "solicitation_text", "evidence_text_excerpt")
    ) else 0
    has_lines = 1 if (row.get("line_items") or row.get("bom")) else 0
    fam_boost = 50 if family in PRIORITY_FAMILIES else 0
    if family in {"IOWA", "MONTANA"}:
        fam_boost += 25
    elif family in {"PHOENIX", "SOURCEWELL"}:
        fam_boost += 10
    # Prefer recovered packages (bytes/text) — re-process even if line_items already exist
    bytes_boost = min(50, with_bytes * 12 + has_text * 8 + has_lines * 5)
    return (-(fam_boost + bytes_boost), str(row.get("canonical_id") or ""))


def analyze_document_intelligence_top(
    store: Any,
    *,
    limit: int = 25,
    persist: bool = True,
    priority_families_only: bool = False,
) -> dict[str, Any]:
    """Batch extraction prioritizing recovered Iowa/Montana/Phoenix/Sourcewell packages."""
    rows = list(store.all()) if hasattr(store, "all") else []
    rows = [r for r in rows if isinstance(r, dict)]
    if priority_families_only:
        rows = [r for r in rows if classify_portal_family(r) in PRIORITY_FAMILIES]
    ranked = sorted(rows, key=_priority_score)[: max(1, min(limit, 50))]

    items: list[dict[str, Any]] = []
    index = load_intelligence_index() if persist else {"by_id": {}}
    by_id = index.setdefault("by_id", {})

    docs_analyzed = processed = failed = review = 0
    line_items_n = products_n = models_n = parts_n = qtys_n = boms_n = 0
    values_n = pricing_n = delivery_n = 0
    ready_pricing = ready_econ = blocked = 0
    improved: list[dict[str, Any]] = []

    for row in ranked:
        intel = extract_opportunity_intelligence(row, update_learning=persist)
        items.append(intel)
        docs_analyzed += int(intel.get("Documents_analyzed") or 0)
        processed += int(intel.get("Processed") or 0)
        failed += int(intel.get("Failed") or 0)
        review += int(intel.get("Requires_review") or 0)
        line_items_n += len(intel.get("PROCUREMENT_LINE_ITEMS") or [])
        products_n += len(intel.get("Products_identified") or [])
        models_n += len(intel.get("Models_found") or [])
        parts_n += len(intel.get("Part_numbers_found") or [])
        qtys_n += len(intel.get("Quantities_found") or [])
        bom = intel.get("PROCUREMENT_BOM") or {}
        if (bom.get("confirmed_lines") or 0) + (bom.get("likely_lines") or 0) > 0:
            boms_n += 1
        values_n += len(intel.get("Values_found") or [])
        pricing_n += int(intel.get("Pricing_sections_found") or 0)
        delivery_n += len(intel.get("Delivery_requirements_found") or [])

        status = (intel.get("ECONOMICS_READINESS_SCORE") or {}).get("status")
        if status == READY_FOR_ECONOMICS:
            ready_econ += 1
        elif status == READY_FOR_PRICING:
            ready_pricing += 1
        elif status == BLOCKED:
            blocked += 1

        identity = intel.get("PRODUCT_IDENTITY") or {}
        improved.append(
            {
                "Opportunity": row.get("title"),
                "canonical_id": row.get("canonical_id"),
                "Portal_family": intel.get("Portal_family"),
                "Documents_processed": intel.get("Processed"),
                "Products": identity.get("Manufacturer") or identity.get("Description") or "UNKNOWN",
                "Quantity": identity.get("Quantity") or (
                    (intel.get("Quantities_found") or ["UNKNOWN"])[0]
                    if intel.get("Quantities_found")
                    else "UNKNOWN"
                ),
                "Configuration": "COMPLETE"
                if (intel.get("CONFIGURATION_REQUIREMENT_PROFILE") or {}).get("configuration_complete")
                else "INCOMPLETE",
                "BOM": bom.get("line_count") or 0,
                "Value": (intel.get("COMMERCIAL_REQUIREMENT") or {}).get("Estimated_contract_value")
                or (intel.get("COMMERCIAL_REQUIREMENT") or {}).get("Award_value")
                or "UNKNOWN",
                "Economics_readiness": status,
            }
        )

        cid = row.get("canonical_id")
        if cid and hasattr(store, "_rows"):
            existing = store._rows.get(cid) or dict(row)
            patch = intel.get("working_row_patch") or {}
            for k, v in patch.items():
                if v is not None:
                    existing[k] = v
            existing["document_intelligence_full"] = intel
            store._rows[cid] = existing

        if cid:
            by_id[str(cid)] = {
                "summary": {
                    "Portal_family": intel.get("Portal_family"),
                    "Processed": intel.get("Processed"),
                    "Failed": intel.get("Failed"),
                    "Requires_review": intel.get("Requires_review"),
                    "line_items": len(intel.get("PROCUREMENT_LINE_ITEMS") or []),
                    "PRODUCT_IDENTITY_CONFIDENCE": intel.get("PRODUCT_IDENTITY_CONFIDENCE"),
                    "ECONOMICS_READINESS": (intel.get("ECONOMICS_READINESS_SCORE") or {}).get("status"),
                    "Missing_information": intel.get("Missing_information"),
                },
                "updated_at": _utc(),
            }

    if persist:
        try:
            if hasattr(store, "save"):
                store.save()
        except Exception:
            pass
        save_intelligence_index(index)

    # Prefer improved with actual extraction progress
    improved.sort(
        key=lambda x: (
            0 if x.get("Economics_readiness") in {READY_FOR_ECONOMICS, READY_FOR_PRICING} else 1,
            -(x.get("BOM") or 0),
            -(x.get("Documents_processed") or 0),
        )
    )

    learning = load_learning_index() if persist else {}

    return {
        "kind": "M3DocumentIntelligenceRun",
        "analyzed": len(items),
        "DOCUMENT_PROCESSING": {
            "Documents_analyzed": docs_analyzed,
            "Processed": processed,
            "Failed": failed,
            "Requires_review": review,
        },
        "EXTRACTION": {
            "Line_items_extracted": line_items_n,
            "Products_identified": products_n,
            "Models_found": models_n,
            "Part_numbers_found": parts_n,
            "Quantities_found": qtys_n,
            "BOMs_created": boms_n,
        },
        "COMMERCIAL_DATA": {
            "Values_found": values_n,
            "Pricing_sections_found": pricing_n,
            "Delivery_requirements_found": delivery_n,
        },
        "ECONOMICS": {
            "Ready_for_pricing": ready_pricing,
            "Ready_for_economics": ready_econ,
            "Blocked": blocked,
            "Partial": len(items) - ready_pricing - ready_econ - blocked,
        },
        "TOP_IMPROVED_OPPORTUNITIES": improved[:10],
        "LEARNING": {
            "by_portal_family": (learning.get("by_portal_family") or {}) if isinstance(learning, dict) else {},
        },
        "LIMITATIONS": {
            "Missing": sorted(
                {
                    m
                    for it in items
                    for m in (it.get("Missing_information") or [])
                    if m
                }
            )[:12],
            "Unknown": sum(
                1
                for it in items
                if (it.get("PRODUCT_IDENTITY_CONFIDENCE") or MATCH_UNKNOWN) == MATCH_UNKNOWN
            ),
            "Extraction_failures": failed,
        },
        "COST": {"Paid_spend": 0},
        "SAFETY": {"Outreach_actions": 0},
        "items": items,
        "NEXT_STATE": "PROCUREMENT_DOCUMENT_INTELLIGENCE_OPERATIONAL",
        "DEVELOPMENT_NO_OUTREACH": True,
    }


def deal_room_document_intelligence_section(row: dict[str, Any]) -> dict[str, Any]:
    """Phase 11 — Document Intelligence Panel."""
    intel = row.get("document_intelligence_full")
    if not isinstance(intel, dict) or intel.get("kind") != "M3DocumentIntelligence":
        # Lightweight build without learning writes
        try:
            intel = extract_opportunity_intelligence(row, update_learning=False)
        except Exception:
            intel = {}

    identity = intel.get("PRODUCT_IDENTITY") or {}
    bom = intel.get("PROCUREMENT_BOM") or {}
    cfg = intel.get("CONFIGURATION_REQUIREMENT_PROFILE") or {}
    readiness = intel.get("ECONOMICS_READINESS_SCORE") or {}
    commercial = intel.get("COMMERCIAL_REQUIREMENT") or {}

    return {
        "kind": "M3DealRoomDocumentIntelligence",
        "Documents_processed": intel.get("Processed") or 0,
        "Documents_failed": intel.get("Failed") or 0,
        "Documents_requires_review": intel.get("Requires_review") or 0,
        "Products_found": [
            p.get("Description") or p.get("Manufacturer") for p in (intel.get("Products_identified") or [])
        ][:8],
        "Line_items": (intel.get("PROCUREMENT_LINE_ITEMS") or [])[:20],
        "Quantity": identity.get("Quantity") or "UNKNOWN",
        "Models": intel.get("Models_found") or [],
        "Part_numbers": intel.get("Part_numbers_found") or [],
        "Configuration": {
            "complete": cfg.get("configuration_complete"),
            "primary": [i.get("Item") for i in (cfg.get("PRIMARY_EQUIPMENT") or [])][:5],
            "accessories": [i.get("Item") for i in (cfg.get("REQUIRED_ACCESSORIES") or [])][:8],
        },
        "BOM": (bom.get("lines") or [])[:20],
        "BOM_completeness_pct": bom.get("BOM_completeness_pct") or 0,
        "Value": commercial.get("Estimated_contract_value") or commercial.get("Award_value") or "UNKNOWN",
        "Missing_information": intel.get("Missing_information") or readiness.get("missing") or [],
        "Economics_readiness": readiness.get("status") or BLOCKED,
        "Economics_score": readiness.get("score") or 0,
        "Portal_family": intel.get("Portal_family") or classify_portal_family(row),
        "VA_allowed_actions": sorted(VA_ALLOWED_ACTIONS),
        "VA_forbidden_actions": sorted(VA_FORBIDDEN_ACTIONS),
        "Next_Action": (
            "Run market pricing on confirmed BOM"
            if readiness.get("status") in {READY_FOR_PRICING, READY_FOR_ECONOMICS}
            else "Recover or clarify: " + ", ".join((readiness.get("missing") or ["evidence"])[:3])
        ),
        "DEVELOPMENT_NO_OUTREACH": True,
    }


def apply_va_intelligence_update(
    store: Any,
    canonical_id: str,
    *,
    action: str,
    note: str | None = None,
    correction: dict[str, Any] | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """Phase 13 — VA review/correct/flag/notes. No deals/bids/outreach/scoring."""
    action_u = str(action or "").upper().strip()
    if action_u not in VA_ALLOWED_ACTIONS:
        return {
            "ok": False,
            "error": "action_not_allowed",
            "action": action_u,
            "VA_allowed_actions": sorted(VA_ALLOWED_ACTIONS),
            "VA_forbidden_actions": sorted(VA_FORBIDDEN_ACTIONS),
        }

    row = None
    if hasattr(store, "_rows") and canonical_id in store._rows:
        row = store._rows[canonical_id]
    elif hasattr(store, "get"):
        row = store.get(canonical_id)
    if not isinstance(row, dict):
        return {"ok": False, "error": "opportunity_not_found", "canonical_id": canonical_id}

    notes = list(row.get("document_intelligence_va_notes") or [])
    intel = row.get("document_intelligence_full") if isinstance(row.get("document_intelligence_full"), dict) else {}

    if action_u in {"ATTACH_NOTES", "NOTE"} and note:
        notes.append({"at": _utc(), "note": str(note)[:2000], "action": action_u})
    if action_u == "FLAG_MISSING_INFORMATION":
        missing = list(intel.get("Missing_information") or [])
        flag = str(note or "flagged_missing").strip()[:200]
        if flag and flag not in missing:
            missing.append(flag)
        intel["Missing_information"] = missing
        notes.append({"at": _utc(), "note": f"flagged:{flag}", "action": action_u})
    if action_u == "MARK_REQUIRES_REVIEW":
        for p in intel.get("DOCUMENT_INTELLIGENCE_PROFILES") or []:
            if isinstance(p, dict):
                p["Processing_status"] = REQUIRES_REVIEW
                p["Extraction_status"] = REQUIRES_REVIEW
        notes.append({"at": _utc(), "note": note or "requires_review", "action": action_u})
    if action_u == "REVIEW_EXTRACTION":
        notes.append({"at": _utc(), "note": note or "reviewed", "action": action_u})
    if action_u == "CORRECT_EXTRACTION" and isinstance(correction, dict):
        # VA may correct obvious extraction errors on line items — never invent prices
        lines = list(intel.get("PROCUREMENT_LINE_ITEMS") or [])
        idx = correction.get("line_index")
        if isinstance(idx, int) and 0 <= idx < len(lines):
            allowed_keys = {
                "Description",
                "Quantity",
                "Unit",
                "Manufacturer",
                "Model",
                "Part_number",
                "NSN",
            }
            patch = {k: correction[k] for k in allowed_keys if k in correction}
            # Re-validate part numbers
            if "Part_number" in patch:
                chk = validate_identifier(patch["Part_number"], source="va_correction", kind="part_number")
                patch["Part_number"] = chk["Identifier"] if chk.get("accepted") else "UNKNOWN"
            lines[idx] = {**lines[idx], **patch, "Confidence": "MEDIUM", "Source_document": "va_correction"}
            intel["PROCUREMENT_LINE_ITEMS"] = lines
            # Reflect onto row line_items
            row_lines = list(row.get("line_items") or [])
            if idx < len(row_lines) and isinstance(row_lines[idx], dict):
                row_lines[idx] = {
                    **row_lines[idx],
                    "description": lines[idx].get("Description"),
                    "quantity": lines[idx].get("Quantity")
                    if lines[idx].get("Quantity") != "UNKNOWN"
                    else row_lines[idx].get("quantity"),
                    "part_number": lines[idx].get("Part_number")
                    if lines[idx].get("Part_number") != "UNKNOWN"
                    else None,
                    "manufacturer": lines[idx].get("Manufacturer")
                    if lines[idx].get("Manufacturer") != "UNKNOWN"
                    else None,
                }
                row["line_items"] = row_lines
                row["bom"] = row_lines
        notes.append({"at": _utc(), "note": note or "correction_applied", "action": action_u})
    if action_u == "UPDATE_STATUS" and status:
        intel["VA_status"] = str(status).upper()
        notes.append({"at": _utc(), "note": f"status:{status}", "action": action_u})

    row["document_intelligence_va_notes"] = notes[-30:]
    if intel:
        row["document_intelligence_full"] = intel
    if hasattr(store, "_rows"):
        store._rows[canonical_id] = row
    if hasattr(store, "save"):
        try:
            store.save()
        except Exception:
            pass

    idx = load_intelligence_index()
    by_id = idx.setdefault("by_id", {})
    snap = by_id.get(canonical_id) if isinstance(by_id.get(canonical_id), dict) else {}
    snap["VA_notes"] = notes[-10:]
    snap["updated_at"] = _utc()
    by_id[canonical_id] = snap
    save_intelligence_index(idx)

    return {
        "ok": True,
        "canonical_id": canonical_id,
        "action": action_u,
        "VA_notes": notes[-10:],
        "DEVELOPMENT_NO_OUTREACH": True,
    }
