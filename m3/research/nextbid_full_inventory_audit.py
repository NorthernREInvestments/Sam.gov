"""BUILD 002Q — Complete independent audit export of all inventory records.

ZERO NextBid / OpenAI / SAM / DIBBS calls. Offline evidence only.
Produces ChatGPT-uploadable audit package under audit_exports/.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import time
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from m3.config import get_settings
from m3.research.classify import (
    CONSTRUCTION_ONLY,
    PRODUCT_HINTS,
    PURCHASE_OF_GOODS,
    RENTAL_LEASE,
    SERVICE_ONLY,
    SIGNIFICANT_INSTALL,
    SOFTWARE_ONLY,
    agency_codes_from_record,
    classify_requirement,
)
from m3.research.extract_fields import extract_quantity_fields
from m3.research.full_text_store import load_full_text
from m3.research.nextbid_acquire import EXCLUDED_SOURCES
from m3.research.nextbid_controller import acquisition_locked
from m3.research.nextbid_inventory import load_inventory
from m3.research.nextbid_recovery import collect_population
from m3.research.nextbid_response_store import store_root as paid_store_root
from m3.research.nextbid_status_reconciliation import (
    FEDERAL_SOURCES,
    detect_duplicates,
    load_retained_provider_index,
)

EXPORT_ROOT_NAME = "BUILD_002Q_FULL_INVENTORY_AUDIT"
EXPORT_ROOT_002R = "BUILD_002R_VERIFICATION_EXPORT"
EXPORT_ROOT_002R1 = "BUILD_002R1_VERIFICATION_EXPORT"
EXPORT_ROOT_002R2 = "BUILD_002R2_VERIFICATION_EXPORT"
EXPORT_ROOT_ENGINE_REPAIR = "PROCUREMENT_ENGINE_REPAIR_EXPORT"
RECORDS_PER_ZIP = 250

PRIMARY_CLASSES = (
    "DUPLICATE",
    "CANCELLED",
    "CLOSED_EXPIRED",
    "FEDERAL_PARKED",
    "CONFIRMED_PRODUCT",
    "POTENTIAL_PRODUCT",
    "MIXED_PRODUCT_SERVICE",
    "CONFIRMED_SERVICE",
    "CONFIRMED_CONSTRUCTION",
    "PROBABLE_NON_PRODUCT",
    "VENDOR_QUALIFICATION",
    "OTHER_INCOMPATIBLE",
    "INSUFFICIENT_EVIDENCE",
)

SECRET_KEY_RE = re.compile(
    r"(?i)(api[_-]?key|authorization|cookie|set-cookie|password|secret|token|bearer|"
    r"x-api-key|session|credential)"
)

CANCEL_RE = re.compile(r"(?i)\b(cancel+ed|cancel+ation|withdrawn)\b")
CLOSED_RE = re.compile(r"(?i)\b(closed|expired|inactive|archived|complete[d]?)\b")
AWARD_RE = re.compile(r"(?i)\b(award(?:ed)?|historical award)\b")


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log(msg: str) -> None:
    print(f"[002q-full-audit] {msg}", flush=True)


def export_root(name: str | None = None) -> Path:
    # Prefer project-local audit_exports (portable, not under data_root secrets)
    p = Path(__file__).resolve().parents[2] / "audit_exports" / (name or EXPORT_ROOT_NAME)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _scrub(obj: Any) -> Any:
    """Remove secrets; keep ordinary solicitation content."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if SECRET_KEY_RE.search(str(k)):
                out[k] = "[REDACTED]"
                continue
            if isinstance(v, str) and SECRET_KEY_RE.search(k) is None:
                # redact embedded bearer-looking blobs only in header-like values
                if len(v) > 20 and re.search(r"(?i)bearer\s+[A-Za-z0-9\-._~+/]+=*", v):
                    out[k] = "[REDACTED_TOKEN]"
                    continue
            out[k] = _scrub(v)
        return out
    if isinstance(obj, list):
        return [_scrub(x) for x in obj]
    return obj


def _parse_dt(v: Any) -> datetime | None:
    if not v:
        return None
    s = str(v).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s[:32] if len(s) > 32 else s)
    except Exception:
        try:
            dt = datetime.fromisoformat(s[:19])
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def load_prior_classifications() -> dict[str, dict[str, Any]]:
    """Prior M3 classifications from latest active population (for change tracking)."""
    path = get_settings().data_root() / "acceptance" / "nextbid_status_recon_active_population.jsonl"
    out: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return out
    with path.open(encoding="utf-8") as f:
        for line in f:
            try:
                it = json.loads(line)
            except json.JSONDecodeError:
                continue
            nb = str(it.get("nb_id") or "")
            if not nb:
                continue
            pipe = it.get("procurement_pipeline") or {}
            out[nb] = {
                "requirement_class": it.get("requirement_class"),
                "primary_disposition": it.get("primary_disposition"),
                "solicitation_status": it.get("solicitation_status"),
                "reconciliation_category": it.get("reconciliation_category"),
                "excluded": bool(pipe.get("excluded")),
                "product_opportunity": pipe.get("product_opportunity"),
                "quantity": it.get("quantity"),
                "quantity_defined": pipe.get("quantity_defined"),
            }
    return out


def load_prior_incompatible_exact() -> dict[str, dict[str, Any]]:
    """Load the exact 709 IDs using the same rule as complete_discovery report."""
    path = get_settings().data_root() / "acceptance" / "nextbid_status_recon_active_population.jsonl"
    out: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return out
    with path.open(encoding="utf-8") as f:
        for line in f:
            try:
                it = json.loads(line)
            except json.JSONDecodeError:
                continue
            nb = str(it.get("nb_id") or "")
            if not nb:
                continue
            req = str(it.get("requirement_class") or "").lower()
            excluded = bool((it.get("procurement_pipeline") or {}).get("excluded"))
            # Exact replay of complete_discovery incompatible counter
            if req in {
                "service",
                "construction",
                "not_applicable_non_solicitation",
                "incompatible",
            } or excluded:
                out[nb] = {
                    "requirement_class": it.get("requirement_class"),
                    "excluded": excluded,
                    "primary_disposition": it.get("primary_disposition"),
                    "title": (it.get("title") or "")[:200],
                    "reasons": (it.get("procurement_pipeline") or {}).get("reasons"),
                }
    return out


def gather_original_evidence(
    nb_id: str,
    *,
    provider: dict[str, Any] | None,
    pop_row: dict[str, Any] | None,
    inv_row: dict[str, Any],
    search_hits: list[dict[str, Any]],
    paid_by_nb: Path,
) -> dict[str, Any]:
    """Assemble the most complete retained original evidence without fabricating."""
    sources_used: list[str] = []
    original: dict[str, Any] = {}
    cache_refs: list[str] = []

    # Paid durable detail if present
    paid_path = paid_by_nb / f"{nb_id}.json"
    paid_blob = None
    if paid_path.exists():
        try:
            paid_blob = json.loads(paid_path.read_text(encoding="utf-8"))
            sources_used.append("paid_response_store")
            cache_refs.append(str(paid_path))
            body = paid_blob.get("body") if isinstance(paid_blob, dict) else None
            if isinstance(body, dict):
                original.update(body)
            elif isinstance(paid_blob, dict):
                original.update({k: v for k, v in paid_blob.items() if k not in {"headers"}})
        except Exception:
            paid_blob = None

    if provider:
        sources_used.append("nextbid_http_cache_or_checkpoint")
        for k, v in provider.items():
            if v not in (None, "", [], {}) and (
                k not in original or original.get(k) in (None, "", [], {})
            ):
                original[k] = v

    if pop_row:
        sources_used.append("population_merge")
        for k, v in pop_row.items():
            if k in {"document_text", "description", "summary"} and isinstance(v, str):
                if len(v) > len(str(original.get(k) or "")):
                    original[k] = v
            elif v not in (None, "", [], {}) and original.get(k) in (None, "", [], {}):
                original[k] = v

    # Inventory is M3-stored, not original provider — keep separate but fill gaps for ID fields
    for k in ("nb_id", "title", "solicitation_number", "official_url", "source"):
        if original.get(k) in (None, "", [], {}) and inv_row.get(k):
            original[k] = inv_row[k]
            sources_used.append("inventory_gapfill")

    ft = load_full_text(nb_id)
    doc_text = ""
    doc_meta = None
    if ft and str(ft.get("full_text") or "").strip():
        doc_text = str(ft.get("full_text") or "")
        doc_meta = {
            "source": ft.get("source"),
            "full_text_chars": ft.get("full_text_chars"),
            "is_listing_preview_only": ft.get("is_listing_preview_only"),
            "updated_at": ft.get("updated_at"),
            "path": str(
                get_settings().data_root() / "solicitation_full_text" / f"{nb_id}.json"
            ),
        }
        sources_used.append("solicitation_full_text_store")
        cache_refs.append(doc_meta["path"])

    # Verifiable evidence dimensions — never equate a short listing preview with a
    # complete solicitation package. Filename / prior flag is not proof of content.
    has_provider_row = bool(provider) or bool(paid_blob)
    desc_text = str(original.get("description") or original.get("summary") or "")
    desc_len = len(desc_text)
    doc_len = len(doc_text or "")
    is_listing_preview_flag = bool((doc_meta or {}).get("is_listing_preview_only"))
    # ≤500 char listing/description without substantial doc body = listing preview
    listing_summary_only = bool(
        is_listing_preview_flag
        or (doc_len > 0 and doc_len <= 500 and is_listing_preview_flag)
        or (doc_len == 0 and desc_len <= 500)
        or (doc_len > 0 and doc_len <= 500 and desc_len <= 500 and not paid_blob)
    )
    # Attachment metadata present?
    attachments = (
        original.get("attachments")
        or original.get("documents")
        or original.get("document_list")
        or []
    )
    has_attachment_metadata = bool(attachments) and attachments not in ([], {}, "")
    # Original solicitation document content = substantial non-preview full text
    has_original_solicitation_document_content = bool(
        doc_len >= 800 and not listing_summary_only and not is_listing_preview_flag
    )
    provider_field_count = sum(
        1 for k, v in (provider or {}).items() if v not in (None, "", [], {}) and not str(k).startswith("_")
    )
    has_complete_retained_provider_response = bool(
        paid_blob is not None or (provider and provider_field_count >= 6)
    )
    has_complete_available_listing_description = bool(desc_len >= 200)
    has_full_opportunity_detail_response = bool(
        has_complete_retained_provider_response
        and (desc_len >= 400 or doc_len >= 400)
        and not listing_summary_only
    )
    has_complete_specifications = bool(
        any(
            original.get(k)
            for k in (
                "manufacturer",
                "model_number",
                "part_number",
                "brand_restrictions",
                "specifications",
                "technical_specs",
            )
        )
        or (has_original_solicitation_document_content and re.search(
            r"(?i)\b(specification|shall\s+meet|minimum\s+requirements?|salient\s+characteristic)\b",
            doc_text[:3000],
        ))
    )
    has_submission_requirements = bool(
        original.get("submission_method")
        or original.get("submission_instructions")
        or (
            doc_len >= 400
            and re.search(
                r"(?i)\b(submit(?:tal)?|proposal\s+due|bid\s+opening|how\s+to\s+respond)\b",
                doc_text[:4000],
            )
        )
    )
    # Legacy rollup label — deliberately NOT named "full" unless package-complete
    if has_full_opportunity_detail_response and has_original_solicitation_document_content:
        completeness = "substantial_retained_package"
    elif has_provider_row or desc_len >= 80 or doc_len >= 80:
        completeness = "partial_source_evidence"
    elif original.get("title"):
        completeness = "partial_source_evidence"
    else:
        completeness = "missing_original_evidence"

    examined_fields = sorted(
        {
            k
            for k, v in original.items()
            if v not in (None, "", [], {}) and not str(k).startswith("_")
        }
    )
    evidence_dimensions = {
        "complete_retained_provider_response": has_complete_retained_provider_response,
        "complete_available_listing_description": has_complete_available_listing_description,
        "full_opportunity_detail_response": has_full_opportunity_detail_response,
        "attachment_metadata_retained": has_attachment_metadata,
        "original_solicitation_document_content_retained": has_original_solicitation_document_content,
        "complete_specifications_available": has_complete_specifications,
        "submission_requirements_available": has_submission_requirements,
        "listing_summary_only": listing_summary_only,
        "description_chars": desc_len,
        "document_text_chars": doc_len,
        "document_is_listing_preview_flag": is_listing_preview_flag,
        "provider_row_retained": has_provider_row,
        "paid_response_retained": bool(paid_blob),
    }

    distinctions = {
        "never_returned_by_provider": "UNKNOWN",
        "returned_but_not_persisted": "UNKNOWN",
        "persisted_but_ignored": "UNKNOWN",
        "examined_but_extracted_incorrectly": "UNKNOWN",
        "genuinely_absent": "UNKNOWN",
        "what_was_actually_read": (
            "provider_detail+document"
            if has_provider_row and doc_text
            else "provider_detail"
            if has_provider_row
            else "document_text_only"
            if doc_text
            else "listing_or_title_only"
            if (desc_len or original.get("title"))
            else "nothing_retained"
        ),
        "complete_solicitation_requirements_available": bool(
            has_full_opportunity_detail_response
            and has_original_solicitation_document_content
            and has_submission_requirements
        ),
        "information_never_acquired_vs_unprocessed": (
            "never_acquired"
            if not has_provider_row and not doc_text
            else "acquired_may_need_reprocess"
            if has_provider_row or doc_text
            else "unknown"
        ),
    }
    if not has_provider_row and not (desc_len or doc_text or original.get("title")):
        distinctions["never_returned_by_provider"] = (
            "LIKELY — no retained provider row or text for this nb_id"
        )
    elif has_provider_row and not original.get("lifecycle_status"):
        distinctions["returned_but_not_persisted"] = (
            "POSSIBLE — provider rows often omit lifecycle_status; inventory previously dropped status fields"
        )
    if search_hits and not inv_row.get("lifecycle_status") and any(
        h.get("lifecycle_status") for h in search_hits
    ):
        distinctions["returned_but_not_persisted"] = (
            "YES — lifecycle present in retained search hit but missing/null on inventory historically"
        )

    return {
        "nb_id": nb_id,
        "original": _scrub(original),
        "document_text": doc_text,
        "document_meta": doc_meta,
        "m3_inventory_stored": _scrub(
            {k: v for k, v in inv_row.items() if k != "amendment_history" or v}
        ),
        "search_hit_count": len(search_hits),
        "search_hits_sample": _scrub(search_hits[:3]),
        "sources_used": sorted(set(sources_used)),
        "cache_refs": cache_refs,
        "evidence_completeness": completeness,
        "evidence_dimensions": evidence_dimensions,
        "distinctions": distinctions,
        "fields_present_in_original": examined_fields,
        "complete_original_content_available": bool(
            distinctions["complete_solicitation_requirements_available"]
        ),
        "listing_summary_only": listing_summary_only,
    }


def independent_classify(
    *,
    nb_id: str,
    evidence: dict[str, Any],
    inv_row: dict[str, Any],
    duplicate_of: str | None,
    now: datetime,
) -> dict[str, Any]:
    """Fresh determination from retained evidence — does not inherit REJECT."""
    original = evidence.get("original") or {}
    title = str(original.get("title") or inv_row.get("title") or "")
    src = str(
        original.get("source")
        or original.get("nextbid_source")
        or inv_row.get("source")
        or ""
    ).lower()
    life = str(original.get("lifecycle_status") or inv_row.get("lifecycle_status") or "").lower()
    sol = str(
        original.get("solicitation_status") or inv_row.get("solicitation_status") or ""
    ).lower()
    ntype = str(original.get("notice_type") or inv_row.get("notice_type") or "").lower()
    deadline = original.get("response_deadline") or original.get("deadline_at") or inv_row.get(
        "deadline_at"
    )
    dl = _parse_dt(deadline)

    doc = str(evidence.get("document_text") or "")
    desc = str(original.get("description") or original.get("summary") or "")
    # Prefer full document; never pretend summary is complete package
    body_for_class = doc if len(doc) >= len(desc) else desc
    examined_blob = f"{title}\n{body_for_class}"
    quotations: list[str] = []
    evidence_fields: list[str] = []

    # --- Notice status (separate from product type) ---
    notice_status = "unknown"
    if CANCEL_RE.search(life) or CANCEL_RE.search(sol) or CANCEL_RE.search(title[:100]):
        notice_status = "cancelled"
        evidence_fields.append("lifecycle_or_title_cancel")
    elif CLOSED_RE.search(life) or CLOSED_RE.search(sol) or life in {"closed", "expired"}:
        notice_status = "closed"
        evidence_fields.append(f"lifecycle:{life or sol}")
    elif dl and dl < now and life not in {"open", "amended", "active"}:
        notice_status = "closed_by_deadline"
        evidence_fields.append("deadline_past")
    elif life in {"open", "active", "amended", "posted"} or sol in {"open", "amended", "active"}:
        notice_status = "open" if life != "amended" and sol != "amended" else "amended"
        evidence_fields.append(f"status:{life or sol}")
    elif inv_row.get("provider_open_filter") and (dl is None or dl >= now):
        notice_status = "open"
        evidence_fields.append("provider_open_filter")
    elif dl and dl >= now:
        notice_status = "unknown_future_deadline"
        evidence_fields.append("future_deadline_without_affirmative_status")

    # --- Procurement category (independent of federal/closed) ---
    clf = classify_requirement(
        title,
        category_raw=original.get("product_category"),
        procurement_type=original.get("notice_type"),
        description=desc or None,
        document_text=doc or None,
        agency_codes=agency_codes_from_record(original),
        procurement_kind=original.get("procurement_kind"),
    )
    req = str(clf.get("requirement_class") or "unknown_mixed")
    clf_evidence = list(clf.get("classification_evidence") or [])
    decision_confidence = str(clf.get("decision_confidence") or "low")
    signals = [s for s in (clf.get("evidence_signals") or []) if isinstance(s, dict)]

    decided_side = {
        "product_supply": "product",
        "product_with_significant_installation": "product",
        "product_with_incidental_services": "product",
        "mixed_product_service": "product",
        "services": "service",
        "professional_services": "service",
        "maintenance_repair_services": "service",
        "software_only": "service",
        "product_rental_leasing": "product",
        "construction": "construction",
        "vendor_qualification": "vendor_qualification",
        "not_applicable_non_solicitation": "non_purchase",
    }.get(req)
    field_for_source = {
        "title": "title",
        "summary": "summary/description",
        "codes": "classification.source_codes",
        "provider_kind": "procurement_kind",
        "category": "product_category",
    }
    evidence_refs = [
        {
            "source": s.get("source"),
            "field": field_for_source.get(str(s.get("source")), s.get("source")),
            "supports": s.get("category"),
            "weight": s.get("weight"),
            "rule": s.get("rule"),
            "quote": s.get("quote"),
        }
        for s in signals
    ]
    supporting = [r for r in evidence_refs if r["supports"] == decided_side] if decided_side else []
    if req == "mixed_product_service":
        supporting = [r for r in evidence_refs if r["supports"] in {"product", "service"}]
    quotations = [str(r["quote"]) for r in supporting if r.get("quote")]
    text_supported = any(r["source"] in {"title", "summary"} for r in supporting)

    product_type = "insufficient_evidence"
    product_confidence = "low"
    if decided_side == "product" and req == "product_rental_leasing":
        product_type, product_confidence = "other_incompatible", decision_confidence
        evidence_fields.append("rental_lease_of_goods")
    elif decided_side == "product":
        mixed = req in {"product_with_significant_installation", "product_with_incidental_services", "mixed_product_service"}
        if decision_confidence == "high" and text_supported:
            product_type = "mixed_product_service" if mixed else "confirmed_product"
            product_confidence = "high"
        else:
            product_type, product_confidence = "potential_product", "medium"
        evidence_fields.append(f"classify_requirement:{req}:{decision_confidence}")
    elif decided_side in {"service", "construction"}:
        if decision_confidence == "high" and text_supported:
            product_type = "confirmed_service" if decided_side == "service" else "confirmed_construction"
            product_confidence = "high"
        else:
            product_type, product_confidence = "probable_non_product", "medium"
        evidence_fields.append(f"classify_requirement:{req}:{decision_confidence}")
    elif decided_side == "vendor_qualification":
        product_type, product_confidence = "vendor_qualification", decision_confidence
        evidence_fields.append("vendor_qualification")
    elif decided_side == "non_purchase":
        product_type, product_confidence = "other_incompatible", decision_confidence
        evidence_fields.append("non_purchase_notice")
    else:
        leaning = clf.get("leaning")
        if leaning:
            evidence_fields.append(f"unresolved_leaning:{leaning}")
        for c in clf.get("conflicting_signals") or []:
            evidence_fields.append(f"conflict:{c}")

    # A category is only established from quoted source text, never from rule names alone
    if product_type in {
        "confirmed_product", "confirmed_service", "confirmed_construction", "mixed_product_service",
        "other_incompatible", "vendor_qualification", "probable_non_product", "potential_product",
    } and not quotations:
        evidence_fields.append("demoted_no_source_quotation")
        product_type, product_confidence = "insufficient_evidence", "low"

    # Quantity extraction (does NOT demote product classification)
    qty_info = (
        extract_quantity_fields(examined_blob, title=title) if examined_blob.strip() else {}
    )
    qty = qty_info.get("quantity") if isinstance(qty_info, dict) else None
    qty_sem = qty_info.get("quantity_semantic") if isinstance(qty_info, dict) else None
    qty_status = qty_info.get("quantity_status") if isinstance(qty_info, dict) else None
    qty_ev = qty_info.get("quantity_evidence") if isinstance(qty_info, dict) else None
    verified_line_items = (
        qty_info.get("verified_line_items") if isinstance(qty_info, dict) else None
    ) or []
    if isinstance(qty_ev, list):
        qty_ev = "; ".join(str(x) for x in qty_ev[:5])
    # Also check structured fields
    # Do NOT trust previously persisted scalar quantities — they include known false
    # extractions (35 kV, 24 hours). Re-validate only via extract_quantity_fields above.
    if qty_sem == "MULTI_LINE_ITEM":
        qty = None  # never collapse
        qty_status = "MULTI_LINE_ITEM"

    # Every verified line must have product identity, numerical qty, unit, source passage
    def _line_is_complete(li: dict[str, Any]) -> bool:
        prod = str(li.get("product_description") or "").strip()
        qn = str(li.get("quantity") or "").strip()
        unit = str(li.get("unit") or li.get("quantity_unit") or "EA").strip()
        passage = str(li.get("snippet") or li.get("source_passage") or "").strip()
        try:
            float(qn)
            q_ok = True
        except (TypeError, ValueError):
            q_ok = bool(qn) and qn.replace(".", "", 1).isdigit()
        return bool(prod and q_ok and unit and passage)

    complete_line_items = [li for li in verified_line_items if isinstance(li, dict) and _line_is_complete(li)]
    # Promote scalar ordered qty into a single complete line when extract omitted line list
    if (
        not complete_line_items
        and qty
        and str(qty_sem or "").upper() in {"ORDERED_QUANTITY", "LINE_ITEM_QUANTITY", "FIRM_QUANTITY", "EXACT"}
        and product_type in {"confirmed_product", "potential_product", "mixed_product_service"}
    ):
        passage = ""
        if isinstance(qty_ev, str):
            # last evidence token often the snippet
            parts_ev = [p.strip() for p in qty_ev.split(";") if p.strip()]
            passage = parts_ev[-1] if parts_ev else ""
        if not passage:
            passage = title[:160]
        synthetic = {
            "quantity": str(qty),
            "unit": str(qty_info.get("quantity_unit") or "EA") if isinstance(qty_info, dict) else "EA",
            "product_description": (title or "ordered product")[:120],
            "snippet": passage[:200],
            "semantic": qty_sem,
            "source_passage": passage[:200],
        }
        if _line_is_complete(synthetic):
            complete_line_items = [synthetic]
            verified_line_items = complete_line_items

    # MULTI_LINE with zero complete lines is NOT verified
    if qty_sem == "MULTI_LINE_ITEM" and len(complete_line_items) < 2:
        qty_status = "AMBIGUOUS_MULTI_LINE_INCOMPLETE"
        qty_sem = "AMBIGUOUS_NUMERIC"
        qty = None
        verified_line_items = complete_line_items
    elif complete_line_items:
        verified_line_items = complete_line_items

    # Services / construction / vendor-qual never carry verified product quantities
    if product_type in {
        "confirmed_service",
        "confirmed_construction",
        "probable_non_product",
        "vendor_qualification",
        "other_incompatible",
    }:
        qty = None
        qty_sem = None
        qty_status = "NOT_APPLICABLE_NON_PRODUCT"
        verified_line_items = []
        complete_line_items = []

    has_explicit_verified_quantity = bool(
        qty
        and str(qty_sem or "").upper()
        in {"ORDERED_QUANTITY", "FROM_STRUCTURED_FIELD", "FIRM_QUANTITY", "EXACT", "LINE_ITEM_QUANTITY"}
        and len(complete_line_items) >= 1
    )
    has_complete_source_backed_line_items = len(complete_line_items) >= 1 and all(
        _line_is_complete(li) for li in complete_line_items
    )
    has_aggregate_quantity_only = bool(
        qty and not complete_line_items and str(qty_sem or "").upper() not in {"MULTI_LINE_ITEM"}
    )
    has_only_ambiguous_numbers = bool(
        str(qty_sem or "").upper() in {"AMBIGUOUS_NUMERIC", "ESTIMATED_QUANTITY", "NON_ORDERED"}
        or (qty_status == "AMBIGUOUS_MULTI_LINE_INCOMPLETE")
    )
    quantity_verified = bool(
        (
            has_explicit_verified_quantity
            or (str(qty_sem or "") == "MULTI_LINE_ITEM" and len(complete_line_items) >= 2)
        )
        and has_complete_source_backed_line_items
        and product_type
        in {"confirmed_product", "potential_product", "mixed_product_service"}
    )
    # Never count verified quantities/line items when no complete lines exist
    if not complete_line_items:
        quantity_verified = False
        if str(qty_status or "").upper() in {"VERIFIED", "MULTI_LINE_ITEM"}:
            qty_status = (
                "AGGREGATE_WITHOUT_LINE_ITEMS"
                if qty
                else "NOT_FOUND_IN_RETAINED_EVIDENCE"
            )
        if not qty:
            qty_sem = qty_sem if qty_sem not in {"ORDERED_QUANTITY", "MULTI_LINE_ITEM"} else None
    # Extracted lines on non-product classifications are not product-verified
    if product_type not in {
        "confirmed_product",
        "potential_product",
        "mixed_product_service",
    }:
        quantity_verified = False
        if complete_line_items or qty:
            qty_status = "EXTRACTED_BUT_NOT_PRODUCT_CLASS"
        verified_line_items = complete_line_items  # retain for audit, not counted verified

    line_items = original.get("line_items")
    products_requested = None
    if line_items:
        products_requested = line_items
    elif product_type in {"confirmed_product", "potential_product", "mixed_product_service"}:
        products_requested = title[:240]

    specs = {
        k: original.get(k)
        for k in (
            "manufacturer",
            "model_number",
            "part_number",
            "brand_restrictions",
            "approved_equivalents",
            "unit_of_measure",
        )
        if original.get(k)
    }

    submission = {
        "submission_method": original.get("submission_method"),
        "submission_instructions": original.get("submission_instructions"),
    }
    dims = evidence.get("evidence_dimensions") or {}
    actual_quantities_available = quantity_verified

    missing: list[str] = []
    if evidence.get("listing_summary_only") or not dims.get("original_solicitation_document_content_retained"):
        missing.append("full_solicitation_package_text")
    if not quantity_verified and product_type in {
        "confirmed_product",
        "potential_product",
        "mixed_product_service",
    }:
        missing.append("verified_quantity")
    if not (
        submission.get("submission_method")
        or submission.get("submission_instructions")
        or dims.get("submission_requirements_available")
    ):
        missing.append("submission_instructions")
    if not specs and not dims.get("complete_specifications_available"):
        if product_type in {"confirmed_product", "mixed_product_service"}:
            missing.append("detailed_specifications")
    if evidence.get("evidence_completeness") == "missing_original_evidence":
        missing.append("original_provider_response")
    if not dims.get("full_opportunity_detail_response"):
        missing.append("full_opportunity_detail")

    # --- Primary exclusive classification (status buckets first) ---
    primary = "INSUFFICIENT_EVIDENCE"
    rejection_reason = None
    if duplicate_of:
        primary = "DUPLICATE"
        rejection_reason = f"duplicate_of:{duplicate_of}"
        evidence_fields.append("duplicate_solicitation_number_source")
    elif notice_status == "cancelled":
        primary = "CANCELLED"
        rejection_reason = "cancelled_per_retained_status_or_title"
    elif notice_status in {"closed", "closed_by_deadline"}:
        primary = "CLOSED_EXPIRED"
        rejection_reason = f"notice_status:{notice_status}"
    elif src in FEDERAL_SOURCES or src in EXCLUDED_SOURCES:
        primary = "FEDERAL_PARKED"
        rejection_reason = f"federal_or_excluded_source:{src}"
        evidence_fields.append("source_federal")
    elif product_type == "confirmed_product":
        primary = "CONFIRMED_PRODUCT"
    elif product_type == "potential_product":
        primary = "POTENTIAL_PRODUCT"
    elif product_type == "mixed_product_service":
        primary = "MIXED_PRODUCT_SERVICE"
    elif product_type == "confirmed_service":
        primary = "CONFIRMED_SERVICE"
        rejection_reason = (
            "evidence_supports_service_primary" if quotations else None
        )
        if not quotations:
            primary = "INSUFFICIENT_EVIDENCE"
            product_type = "insufficient_evidence"
            evidence_fields.append("service_rejection_blocked_no_quotation")
    elif product_type == "confirmed_construction":
        primary = "CONFIRMED_CONSTRUCTION"
        rejection_reason = (
            "evidence_supports_construction_primary" if quotations else None
        )
        if not quotations:
            primary = "INSUFFICIENT_EVIDENCE"
            product_type = "insufficient_evidence"
            evidence_fields.append("construction_rejection_blocked_no_quotation")
    elif product_type == "probable_non_product":
        primary = "PROBABLE_NON_PRODUCT"
        rejection_reason = "evidence_leans_non_product_not_confirmed"
    elif product_type == "vendor_qualification":
        primary = "OTHER_INCOMPATIBLE"
        rejection_reason = "vendor_qualification_not_immediate_product_purchase"
        # Keep product_type distinct for reporting
    elif product_type == "other_incompatible":
        primary = "OTHER_INCOMPATIBLE"
        rejection_reason = (
            "evidence_proves_incompatible_requirement" if quotations else None
        )
        if not quotations:
            primary = "INSUFFICIENT_EVIDENCE"
            product_type = "insufficient_evidence"
    else:
        primary = "INSUFFICIENT_EVIDENCE"
        rejection_reason = None  # unknown is NOT rejection-as-service

    # Prefer explicit vendor qualification primary label in export taxonomy
    if product_type == "vendor_qualification":
        primary = "VENDOR_QUALIFICATION"

    further = primary in {
        "POTENTIAL_PRODUCT",
        "MIXED_PRODUCT_SERVICE",
        "INSUFFICIENT_EVIDENCE",
        "CONFIRMED_PRODUCT",
    } and bool(missing)

    # Readiness is derived only from verifiable retained evidence; category alone is never readiness.
    submission_available = bool(
        submission.get("submission_method")
        or submission.get("submission_instructions")
        or dims.get("submission_requirements_available")
    )
    readiness_blockers: list[str] = list(missing)
    if notice_status not in {"open", "amended"}:
        readiness_blockers.append(f"notice_status_not_confirmed_open:{notice_status}")
    if primary in {"DUPLICATE", "CANCELLED", "CLOSED_EXPIRED", "FEDERAL_PARKED"}:
        readiness = "NOT_ACTIONABLE_STATUS"
    elif product_type in {
        "confirmed_service", "confirmed_construction", "probable_non_product",
        "vendor_qualification", "other_incompatible",
    }:
        readiness = "NOT_APPLICABLE_NON_PRODUCT"
        readiness_blockers = []
    elif product_type in {"confirmed_product", "mixed_product_service"}:
        ready = (
            quantity_verified
            and submission_available
            and bool(dims.get("original_solicitation_document_content_retained"))
            and notice_status in {"open", "amended"}
        )
        readiness = "READY_FOR_SOURCING_REVIEW" if ready else "PRODUCT_CONFIRMED_NEEDS_SOLICITATION_DETAIL"
    elif product_type == "potential_product":
        readiness = "PRODUCT_CATEGORY_UNVERIFIED"
    else:
        readiness = "UNRESOLVED_INSUFFICIENT_EVIDENCE"

    dist = evidence.get("distinctions") or {}
    what_proves = (
        "; ".join(quotations[:3])
        if quotations
        else "nothing_proven_from_retained_source_text"
    )
    what_unknown = list(missing) + (
        ["complete_solicitation_requirements"]
        if not evidence.get("complete_original_content_available")
        else []
    )

    return {
        "nb_id": nb_id,
        "primary_classification": primary,
        "secondary_classifications": [product_type, f"notice:{notice_status}"],
        "notice_status": notice_status,
        "product_type": product_type,
        "product_confidence": product_confidence,
        "active_closed_federal_status": (
            "federal_parked"
            if primary == "FEDERAL_PARKED"
            else notice_status
        ),
        "rejection_reason": rejection_reason,
        "supporting_quotations": quotations[:8],
        "supporting_fields": evidence_fields + [f"classify:{e}" for e in clf_evidence[:12]],
        "classifier_requirement_class": req,
        "procurement_category": decided_side or "unresolved",
        "decision_confidence": decision_confidence,
        "evidence_references": evidence_refs,
        "category_scores": clf.get("category_scores") or {},
        "conflicting_signals": clf.get("conflicting_signals") or [],
        "procurement_readiness": readiness,
        "readiness_blockers": readiness_blockers,
        "products_requested": products_requested,
        "quantity": qty,
        "quantity_semantic": qty_sem,
        "quantity_status": qty_status or (
            "NOT_FOUND_IN_RETAINED_EVIDENCE" if not qty and not verified_line_items else None
        ),
        "quantity_evidence": qty_ev,
        "verified_line_items": verified_line_items,
        "complete_line_item_count": len(complete_line_items),
        "quantity_verified": quantity_verified,
        "quantity_reconciliation": {
            "explicitly_verified_quantity": has_explicit_verified_quantity,
            "complete_source_backed_line_items": has_complete_source_backed_line_items,
            "complete_line_item_count": len(complete_line_items),
            "aggregate_quantity_only": has_aggregate_quantity_only,
            "only_ambiguous_numbers": has_only_ambiguous_numbers,
            "actual_quantities_available": actual_quantities_available,
        },
        "specifications": specs,
        "deadline_at": deadline,
        "submission": submission,
        "missing_information": missing,
        "confidence": product_confidence,
        "evidence_completeness": evidence.get("evidence_completeness"),
        "evidence_dimensions": dims,
        "further_investigation_necessary": further,
        "examined_text_chars": len(examined_blob),
        "examined_listing_summary_only": bool(evidence.get("listing_summary_only")),
        "complete_original_content_available": bool(
            evidence.get("complete_original_content_available")
        ),
        "what_was_actually_read": dist.get("what_was_actually_read"),
        "what_evidence_proves": what_proves,
        "what_remains_unknown": what_unknown,
        "complete_solicitation_requirements_available": bool(
            dist.get("complete_solicitation_requirements_available")
        ),
        # Separate dimensions — never conflate
        "dimensions": {
            "notice_status": notice_status,
            "procurement_type": product_type,
            "quantity_completeness": (
                "VERIFIED_WITH_LINE_ITEMS"
                if quantity_verified
                else qty_status
                or ("NOT_FOUND_IN_RETAINED_EVIDENCE" if not qty else "UNVERIFIED")
            ),
            "bid_readiness": readiness,
            "submission_requirements_available": bool(
                dims.get("submission_requirements_available")
            ),
        },
    }


def render_human_readable(nb_id: str, evidence: dict[str, Any], decision: dict[str, Any]) -> str:
    original = evidence.get("original") or {}
    lines = [
        f"# Solicitation {nb_id}",
        "",
        f"**Title:** {original.get('title') or '(none)'}",
        f"**Solicitation number:** {original.get('solicitation_number') or '(none)'}",
        f"**Agency:** {original.get('agency') or original.get('agency_department') or original.get('buyer_name') or '(none)'}",
        f"**Source:** {original.get('source') or '(none)'}",
        f"**Official URL:** {original.get('source_url') or original.get('official_url') or '(none)'}",
        f"**Lifecycle status:** {original.get('lifecycle_status') or '(none)'}",
        f"**Notice type:** {original.get('notice_type') or '(none)'}",
        f"**Posted:** {original.get('posted_date') or original.get('posted_at') or '(none)'}",
        f"**Deadline:** {original.get('response_deadline') or original.get('deadline_at') or '(none)'}",
        f"**Document count hint:** {original.get('document_count') or original.get('document_count_hint') or '(none)'}",
        "",
        "## Evidence completeness",
        "",
        f"- Completeness: `{evidence.get('evidence_completeness')}`",
        f"- Listing summary only: `{evidence.get('listing_summary_only')}`",
        f"- Sources used: {', '.join(evidence.get('sources_used') or [])}",
        "",
        "## Summary / description (retained)",
        "",
        "```",
        str(original.get("summary") or original.get("description") or "(none)"),
        "```",
        "",
        "## Full retained document text",
        "",
    ]
    doc = str(evidence.get("document_text") or "")
    if not doc:
        lines.append("_No retained full document text._")
    else:
        # Do not silently truncate; if extremely large, mark explicit split
        if len(doc) > 500_000:
            lines.append(
                f"_EXPLICIT_TRUNCATION: document has {len(doc)} chars; "
                f"export includes first 500000 chars. Full text remains in original store._"
            )
            lines.append("")
            lines.append("```")
            lines.append(doc[:500_000])
            lines.append("```")
        else:
            lines.append("```")
            lines.append(doc)
            lines.append("```")
    lines += [
        "",
        "## All non-empty original fields",
        "",
    ]
    for k in sorted(original.keys()):
        v = original[k]
        if v in (None, "", [], {}):
            continue
        if k in {"summary", "description", "document_text"}:
            continue
        if isinstance(v, (dict, list)):
            lines.append(f"- **{k}:** `{json.dumps(v, default=str)[:2000]}`")
        else:
            sv = str(v)
            if len(sv) > 5000:
                lines.append(
                    f"- **{k}:** ({len(sv)} chars) EXPLICIT_TRUNCATION_IN_INDEX — see original_evidence.json"
                )
            else:
                lines.append(f"- **{k}:** {sv}")
    lines += [
        "",
        "## Software decision (audit)",
        "",
        f"- Primary: **{decision.get('primary_classification')}**",
        f"- Product type: {decision.get('product_type')}",
        f"- Notice status: {decision.get('notice_status')}",
        f"- Quantity: {decision.get('quantity')} ({decision.get('quantity_semantic')})",
        f"- Rejection reason: {decision.get('rejection_reason')}",
        f"- Missing: {decision.get('missing_information')}",
        f"- Supporting fields: {decision.get('supporting_fields')}",
        f"- Quotations: {decision.get('supporting_quotations')}",
        "",
    ]
    return "\n".join(lines)


def write_record_package(
    base: Path,
    *,
    nb_id: str,
    evidence: dict[str, Any],
    decision: dict[str, Any],
    prior: dict[str, Any] | None,
    archive_id: str,
) -> dict[str, str]:
    primary = decision["primary_classification"]
    folder = base / "by_classification" / primary / nb_id
    folder.mkdir(parents=True, exist_ok=True)

    original_path = folder / "A_original_evidence.json"
    human_path = folder / "B_human_readable.md"
    decision_path = folder / "C_software_decision.json"
    trace_path = folder / "D_audit_trace.json"

    original_path.write_text(
        json.dumps(
            {
                "nb_id": nb_id,
                "retained_original_fields": evidence.get("original"),
                "retained_document_text_chars": len(str(evidence.get("document_text") or "")),
                "retained_document_text": evidence.get("document_text") or None,
                "document_meta": evidence.get("document_meta"),
                "note": (
                    "This is retained evidence only. Missing fields are not fabricated. "
                    "If original is empty, see missing-evidence note in audit trace."
                ),
            },
            indent=2,
            default=str,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    human_path.write_text(
        render_human_readable(nb_id, evidence, decision), encoding="utf-8"
    )
    decision_path.write_text(
        json.dumps(
            {
                **decision,
                "previous_classification": prior,
                "classification_changed": bool(
                    prior
                    and str(prior.get("requirement_class") or "")
                    != str(decision.get("classifier_requirement_class") or "")
                ),
            },
            indent=2,
            default=str,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    trace_path.write_text(
        json.dumps(
            {
                "nb_id": nb_id,
                "archive_id": archive_id,
                "cache_refs": evidence.get("cache_refs"),
                "sources_used": evidence.get("sources_used"),
                "search_hit_count": evidence.get("search_hit_count"),
                "search_hits_sample": evidence.get("search_hits_sample"),
                "m3_inventory_stored": evidence.get("m3_inventory_stored"),
                "fields_present_in_original": evidence.get("fields_present_in_original"),
                "distinctions": evidence.get("distinctions"),
                "what_nextbid_supplied": {
                    "fields": evidence.get("fields_present_in_original"),
                    "has_provider_row": "nextbid_http_cache_or_checkpoint"
                    in (evidence.get("sources_used") or [])
                    or "paid_response_store" in (evidence.get("sources_used") or []),
                },
                "what_m3_stored": {
                    "inventory_keys": sorted((evidence.get("m3_inventory_stored") or {}).keys()),
                    "reconciliation_category": (evidence.get("m3_inventory_stored") or {}).get(
                        "reconciliation_category"
                    ),
                },
                "what_classification_examined": {
                    "examined_text_chars": decision.get("examined_text_chars"),
                    "listing_summary_only": decision.get("examined_listing_summary_only"),
                    "complete_original_available": decision.get(
                        "complete_original_content_available"
                    ),
                    "supporting_fields": decision.get("supporting_fields"),
                },
                "processing_history": "UNKNOWN — no durable per-record processing ledger manufactured",
            },
            indent=2,
            default=str,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    rel = f"by_classification/{primary}/{nb_id}"
    return {
        "dir": rel,
        "original": f"{rel}/A_original_evidence.json",
        "human": f"{rel}/B_human_readable.md",
        "decision": f"{rel}/C_software_decision.json",
        "trace": f"{rel}/D_audit_trace.json",
    }


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def run_full_inventory_audit(
    *,
    export_name: str | None = None,
    prior_master_index: str | Path | None = None,
    vault_durability_check: bool = True,
) -> dict[str, Any]:
    t0 = time.time()
    assert acquisition_locked(), "killswitch must remain locked — audit is offline only"
    # HARD: no network usage helpers either
    export_label = export_name or EXPORT_ROOT_NAME
    _log(f"START offline full inventory audit — ZERO API calls export={export_label}")

    inv = load_inventory()
    assert len(inv) == 8088, f"expected 8088, got {len(inv)}"

    index = load_retained_provider_index()
    by_nb = index["by_nb"]
    search_hits = index["search_hits"]
    pop = collect_population()
    pop_by = pop.get("by_id") or {}
    prior = load_prior_classifications()
    prior_incompat = load_prior_incompatible_exact()
    dup_of = detect_duplicates(inv, by_nb)
    paid_by_nb = paid_store_root() / "by_nb_id"

    # Prior independent audit for record-level change accounting.
    # Prefer nearest prior verification export; never invent a comparison.
    prior_audit_by_nb: dict[str, dict[str, Any]] = {}
    prior_audit_source: str | None = None
    prior_audit_note: str | None = None
    audit_exports = Path(__file__).resolve().parents[2] / "audit_exports"
    prior_candidates = []
    if export_label == EXPORT_ROOT_002R2:
        prior_candidates = [EXPORT_ROOT_002R1, EXPORT_ROOT_002R, EXPORT_ROOT_NAME]
    elif export_label == EXPORT_ROOT_002R1:
        prior_candidates = [EXPORT_ROOT_002R, EXPORT_ROOT_NAME]
    elif export_label.startswith("BUILD_002R"):
        prior_candidates = [EXPORT_ROOT_NAME]
    else:
        prior_candidates = []
    prior_paths = [audit_exports / cand / "indexes" / "MASTER_INDEX.jsonl" for cand in prior_candidates]
    if prior_master_index:
        prior_paths.insert(0, Path(prior_master_index))
    for p in prior_paths:
        if p.exists():
            prior_audit_source = str(p)
            with p.open(encoding="utf-8") as f:
                for line in f:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if row.get("nb_id"):
                        prior_audit_by_nb[str(row["nb_id"])] = row
            break
    if not prior_audit_by_nb:
        prior_audit_note = (
            "Row-level prior audit MASTER_INDEX unavailable on disk "
            f"(checked: {prior_candidates}). Aggregate-only comparison from "
            "docs/acceptance_archive may be used; no invented record-level diffs."
        )
        _log(prior_audit_note)

    root = export_root(export_label)
    # Prefer a clean tree; on Windows/OneDrive locks, fall back to a fresh sibling folder.
    import shutil

    def _wipe(path: Path) -> bool:
        try:
            for child in list(path.iterdir()):
                if child.is_file():
                    child.unlink()
                elif child.is_dir() and child.name in {
                    "by_classification",
                    "indexes",
                    "zips",
                    "manifests",
                }:
                    shutil.rmtree(child, ignore_errors=False)
            return True
        except Exception as exc:
            _log(f"export wipe failed ({type(exc).__name__}); using fresh folder")
            return False

    if any(root.iterdir()):
        if not _wipe(root):
            export_label = f"{export_label}_FRESH"
            root = export_root(export_label)
            _log(f"redirected export to {root}")
    (root / "by_classification").mkdir(exist_ok=True)
    (root / "indexes").mkdir(exist_ok=True)
    (root / "zips").mkdir(exist_ok=True)
    (root / "manifests").mkdir(exist_ok=True)

    now = datetime.now(timezone.utc)
    class_counts: Counter[str] = Counter()
    completeness_counts: Counter[str] = Counter()
    changed = 0
    overturned = 0
    qty_verified = 0
    complete_line_item_total = 0
    multi_line_complete = 0
    multi_line_incomplete = 0
    qty_status_counts: Counter[str] = Counter()
    evidence_dim_true: Counter[str] = Counter()
    index_rows: list[dict[str, Any]] = []
    changes: list[dict[str, Any]] = []
    incompat_reconsider: list[dict[str, Any]] = []
    ordered_ids = sorted(inv.keys())

    # Pre-assign archive membership deterministically
    archive_of: dict[str, str] = {}
    for i, nb in enumerate(ordered_ids):
        part = i // RECORDS_PER_ZIP + 1
        archive_of[nb] = f"part_{part:03d}"

    last_prog = time.time()
    for i, nb in enumerate(ordered_ids, start=1):
        inv_row = inv[nb]
        evidence = gather_original_evidence(
            nb,
            provider=by_nb.get(nb),
            pop_row=pop_by.get(nb),
            inv_row=inv_row,
            search_hits=search_hits.get(nb) or [],
            paid_by_nb=paid_by_nb,
        )
        decision = independent_classify(
            nb_id=nb,
            evidence=evidence,
            inv_row=inv_row,
            duplicate_of=dup_of.get(nb),
            now=now,
        )
        prev = prior.get(nb)
        paths = write_record_package(
            root,
            nb_id=nb,
            evidence=evidence,
            decision=decision,
            prior=prev,
            archive_id=archive_of[nb],
        )
        primary = decision["primary_classification"]
        class_counts[primary] += 1
        completeness_counts[str(evidence.get("evidence_completeness"))] += 1
        if decision.get("quantity_verified"):
            qty_verified += 1
        n_lines = int(
            (decision.get("quantity_reconciliation") or {}).get("complete_line_item_count")
            or len(decision.get("verified_line_items") or [])
        )
        complete_line_item_total += n_lines
        qs = str(decision.get("quantity_status") or "")
        qty_status_counts[qs or "NONE"] += 1
        if decision.get("quantity_semantic") == "MULTI_LINE_ITEM" and n_lines >= 2:
            multi_line_complete += 1
        if qs == "AMBIGUOUS_MULTI_LINE_INCOMPLETE":
            multi_line_incomplete += 1
        for dk, dv in (evidence.get("evidence_dimensions") or {}).items():
            if dv is True:
                evidence_dim_true[dk] += 1

        prev_req = str((prev or {}).get("requirement_class") or "")
        new_req = str(decision.get("classifier_requirement_class") or "")
        classification_changed = bool(prev) and prev_req != new_req
        # Also flag primary vs prior disposition/category changes for active set
        if prev and (
            classification_changed
            or str(prev.get("primary_disposition") or "") != primary
        ):
            changed += 1
            changes.append(
                {
                    "nb_id": nb,
                    "previous_requirement_class": prev_req,
                    "new_requirement_class": new_req,
                    "new_primary": primary,
                    "previous_disposition": prev.get("primary_disposition"),
                    "previous_excluded": prev.get("excluded"),
                }
            )

        if nb in prior_incompat:
            # Overturned if previously treated incompatible/excluded but now product-ish primary
            was_reject = True
            now_product = primary in {
                "CONFIRMED_PRODUCT",
                "POTENTIAL_PRODUCT",
                "MIXED_PRODUCT_SERVICE",
                "INSUFFICIENT_EVIDENCE",
            }
            if now_product:
                overturned += 1
            incompat_reconsider.append(
                {
                    "nb_id": nb,
                    "previous": prior_incompat[nb],
                    "new_primary": primary,
                    "new_product_type": decision.get("product_type"),
                    "supporting_fields": decision.get("supporting_fields"),
                    "supporting_quotations": decision.get("supporting_quotations"),
                    "rejection_reason": decision.get("rejection_reason"),
                    "overturned_to_non_reject_bucket": now_product,
                }
            )

        original = evidence.get("original") or {}
        prior_audit = prior_audit_by_nb.get(nb) or {}
        vs_prior_audit = bool(prior_audit) and str(
            prior_audit.get("primary_classification") or ""
        ) != primary
        index_rows.append(
            {
                "nb_id": nb,
                "solicitation_number": original.get("solicitation_number")
                or inv_row.get("solicitation_number"),
                "agency": original.get("agency")
                or original.get("agency_department")
                or original.get("buyer_name"),
                "title": original.get("title") or inv_row.get("title"),
                "state": (original.get("states") or [None])[0]
                if isinstance(original.get("states"), list)
                else original.get("geo_state"),
                "source": original.get("source") or inv_row.get("source"),
                "deadline": original.get("response_deadline")
                or original.get("deadline_at")
                or inv_row.get("deadline_at"),
                "notice_status": decision.get("notice_status"),
                "primary_classification": primary,
                "product_type": decision.get("product_type"),
                "product_description": (
                    str(decision.get("products_requested") or "")[:300]
                    if not isinstance(decision.get("products_requested"), (list, dict))
                    else json.dumps(decision.get("products_requested"))[:300]
                ),
                "quantity": decision.get("quantity"),
                "quantity_semantic": decision.get("quantity_semantic"),
                "quantity_status": decision.get("quantity_status")
                or (
                    "VERIFIED"
                    if decision.get("quantity_verified")
                    else ("present" if decision.get("quantity") else "missing")
                ),
                "quantity_verified": bool(decision.get("quantity_verified")),
                "verified_line_item_count": len(decision.get("verified_line_items") or []),
                "complete_line_item_count": int(
                    (decision.get("quantity_reconciliation") or {}).get("complete_line_item_count")
                    or len(decision.get("verified_line_items") or [])
                ),
                "quantity_reconciliation": decision.get("quantity_reconciliation"),
                "previous_classification": prev_req or None,
                "prior_audit_classification": prior_audit.get("primary_classification"),
                "new_classification": primary,
                "changed_classification": classification_changed or bool(
                    prev and str(prev.get("primary_disposition") or "") != primary
                ),
                "changed_vs_prior_audit": vs_prior_audit,
                "evidence_completeness": evidence.get("evidence_completeness"),
                "evidence_dimensions": evidence.get("evidence_dimensions"),
                "examined_listing_summary_only": decision.get("examined_listing_summary_only"),
                "missing_evidence": ";".join(decision.get("missing_information") or []),
                "original_source_url": original.get("source_url")
                or original.get("official_url")
                or inv_row.get("official_url"),
                "audit_dir": paths["dir"],
                "archive_id": archive_of[nb],
                "duplicate_of": dup_of.get(nb),
                "rejection_reason": decision.get("rejection_reason"),
                "supporting_fields": ";".join(
                    str(x) for x in (decision.get("supporting_fields") or [])[:8]
                ),
                "procurement_category": decision.get("procurement_category"),
                "classifier_requirement_class": decision.get("classifier_requirement_class"),
                "decision_confidence": decision.get("decision_confidence"),
                "procurement_readiness": decision.get("procurement_readiness"),
                "readiness_blockers": ";".join(decision.get("readiness_blockers") or []),
                "supporting_quotations": (decision.get("supporting_quotations") or [])[:3],
                "conflicting_signals": decision.get("conflicting_signals") or [],
            }
        )

        if time.time() - last_prog >= 20 or i == len(ordered_ids):
            _log(f"progress {i}/{len(ordered_ids)} classes={dict(class_counts)}")
            last_prog = time.time()

    # Indexes
    idx_dir = root / "indexes"
    jsonl_path = idx_dir / "MASTER_INDEX.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as f:
        for row in index_rows:
            f.write(json.dumps(row, default=str, ensure_ascii=False) + "\n")

    csv_path = idx_dir / "MASTER_INDEX.csv"
    fields = list(index_rows[0].keys()) if index_rows else []
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for row in index_rows:
            w.writerow(row)

    changes_path = idx_dir / "CLASSIFICATION_CHANGES.jsonl"
    with changes_path.open("w", encoding="utf-8") as f:
        for row in changes:
            f.write(json.dumps(row, default=str) + "\n")

    incompat_path = idx_dir / "PRIOR_709_INCOMPATIBLE_RECONSIDERED.jsonl"
    with incompat_path.open("w", encoding="utf-8") as f:
        for row in incompat_reconsider:
            f.write(json.dumps(row, default=str) + "\n")

    changed_audit_path = idx_dir / "CHANGED_VS_PRIOR_AUDIT.jsonl"
    with changed_audit_path.open("w", encoding="utf-8") as f:
        for row in index_rows:
            if row.get("changed_vs_prior_audit"):
                f.write(
                    json.dumps(
                        {
                            "nb_id": row["nb_id"],
                            "title": row.get("title"),
                            "prior_audit_classification": row.get("prior_audit_classification"),
                            "new_classification": row.get("new_classification"),
                            "product_type": row.get("product_type"),
                            "quantity_status": row.get("quantity_status"),
                            "rejection_reason": row.get("rejection_reason"),
                            "supporting_fields": row.get("supporting_fields"),
                            "audit_dir": row.get("audit_dir"),
                        },
                        default=str,
                    )
                    + "\n"
                )

    counts_path = idx_dir / "CLASSIFICATION_COUNTS.json"
    counts_path.write_text(
        json.dumps(
            {
                "primary_counts": dict(class_counts),
                "evidence_completeness": dict(completeness_counts),
                "total": sum(class_counts.values()),
                "changed_classifications": changed,
                "prior_incompatible_reconsidered": len(incompat_reconsider),
                "prior_rejections_overturned": overturned,
                "quantity_verified": qty_verified,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    # ZIP archives
    _log("building ZIP archives")
    by_archive_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in index_rows:
        by_archive_rows[row["archive_id"]].append(row)

    zip_manifest = []
    zips_dir = root / "zips"
    for arch_id in sorted(by_archive_rows.keys()):
        zpath = zips_dir / f"{export_label}_{arch_id}.zip"
        part_rows = by_archive_rows[arch_id]
        ids = [r["nb_id"] for r in part_rows]
        with zipfile.ZipFile(zpath, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                f"{arch_id}/PART_INDEX.jsonl",
                "\n".join(json.dumps(r, default=str) for r in part_rows) + "\n",
            )
            for row in part_rows:
                rel_dir = row["audit_dir"]
                for fname in (
                    "A_original_evidence.json",
                    "B_human_readable.md",
                    "C_software_decision.json",
                    "D_audit_trace.json",
                ):
                    fpath = root / rel_dir / fname
                    if fpath.exists():
                        zf.write(fpath, arcname=f"{arch_id}/{rel_dir}/{fname}")
        zip_manifest.append(
            {
                "archive_id": arch_id,
                "filename": zpath.name,
                "path": str(zpath),
                "records": len(ids),
                "bytes": zpath.stat().st_size,
                "sha256": sha256_file(zpath),
                "nb_ids": ids,
            }
        )
        _log(f"zip {arch_id} records={len(ids)} bytes={zpath.stat().st_size}")

    # Master manifest: every ID → exactly one archive
    id_to_archive = {nb: archive_of[nb] for nb in ordered_ids}
    manifest = {
        "build": "002Q",
        "export": EXPORT_ROOT_NAME,
        "generated_at": _utc(),
        "total_records": len(ordered_ids),
        "records_per_zip_target": RECORDS_PER_ZIP,
        "zip_count": len(zip_manifest),
        "id_to_archive": id_to_archive,
        "archives": [
            {k: v for k, v in a.items() if k != "nb_ids"} | {"nb_id_count": len(a["nb_ids"])}
            for a in zip_manifest
        ],
        "api_expenditure": 0,
        "acquisition_locked": acquisition_locked(),
    }
    manifest_path = root / "manifests" / "MASTER_MANIFEST.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    # Checksums file
    checksums = {
        "master_index_jsonl_sha256": sha256_file(jsonl_path),
        "master_index_csv_sha256": sha256_file(csv_path),
        "zips": [{k: a[k] for k in ("archive_id", "filename", "sha256", "bytes", "records")} for a in zip_manifest],
    }
    (root / "manifests" / "CHECKSUMS.json").write_text(
        json.dumps(checksums, indent=2), encoding="utf-8"
    )

    # Validation
    validation = {
        "inventory_total": len(inv),
        "index_rows": len(index_rows),
        "class_sum": sum(class_counts.values()),
        "every_id_in_exactly_one_archive": len(id_to_archive) == 8088
        and len(set(id_to_archive.values())) == len(zip_manifest),
        "zip_record_sum": sum(a["records"] for a in zip_manifest),
        "missing_packages": 0,
        "prior_incompatible_count": len(prior_incompat),
        "prior_incompatible_reconsidered": len(incompat_reconsider),
        "api_calls": 0,
        "acquisition_locked": acquisition_locked(),
        "ok": True,
        "errors": [],
    }
    for row in index_rows:
        for key in ("dir",):
            p = root / row["audit_dir"]
            if not p.exists():
                validation["missing_packages"] += 1
                validation["errors"].append(f"missing_dir:{row['nb_id']}")
            else:
                for fname in (
                    "A_original_evidence.json",
                    "B_human_readable.md",
                    "C_software_decision.json",
                    "D_audit_trace.json",
                ):
                    if not (p / fname).exists():
                        validation["missing_packages"] += 1
                        validation["errors"].append(f"missing_file:{row['nb_id']}:{fname}")
    if validation["index_rows"] != 8088:
        validation["ok"] = False
        validation["errors"].append("index_rows_ne_8088")
    if validation["class_sum"] != 8088:
        validation["ok"] = False
        validation["errors"].append("class_sum_ne_8088")
    if validation["zip_record_sum"] != 8088:
        validation["ok"] = False
        validation["errors"].append("zip_record_sum_ne_8088")
    if validation["missing_packages"]:
        validation["ok"] = False
    if len(incompat_reconsider) != len(prior_incompat):
        validation["errors"].append(
            f"incompat_reconsider_mismatch:{len(incompat_reconsider)}!={len(prior_incompat)}"
        )

    (root / "manifests" / "VALIDATION.json").write_text(
        json.dumps(validation, indent=2), encoding="utf-8"
    )

    readme = root / "README.md"
    readme.write_text(
        "\n".join(
            [
                "# BUILD 002Q — Full Inventory Independent Audit Export",
                "",
                "Offline reprocess of all 8,088 inventory records for independent ChatGPT review.",
                "",
                "## Layout",
                "",
                "- `by_classification/<PRIMARY>/<nb_id>/` — A/B/C/D evidence package per record",
                "- `indexes/` — MASTER_INDEX.csv/jsonl, changes, prior-709 reconsidered",
                "- `zips/` — numbered archives (~250 records each)",
                "- `manifests/` — master manifest, checksums, validation",
                "",
                "## Important",
                "",
                "Classifications in this export are **software decisions for audit**, not independent verification.",
                "Upload ZIP parts + master index to ChatGPT for independent review.",
                "",
                f"Generated: {_utc()}",
                f"API expenditure: 0",
                "",
            ]
        ),
        encoding="utf-8",
    )

    # Comparison vs prior 002Q audit export (file or documented baseline counts)
    prior_audit_counts: Counter[str] = Counter()
    for row in prior_audit_by_nb.values():
        prior_audit_counts[str(row.get("primary_classification") or "?")] += 1
    if not prior_audit_counts:
        # Documented 002Q independent-audit totals (export folder no longer present)
        prior_audit_counts.update(
            {
                "FEDERAL_PARKED": 4288,
                "CONFIRMED_SERVICE": 1298,
                "INSUFFICIENT_EVIDENCE": 1238,
                "CONFIRMED_PRODUCT": 770,
                "CONFIRMED_CONSTRUCTION": 218,
                "MIXED_PRODUCT_SERVICE": 148,
                "DUPLICATE": 55,
                "CLOSED_EXPIRED": 52,
                "OTHER_INCOMPATIBLE": 15,
                "CANCELLED": 4,
                "POTENTIAL_PRODUCT": 2,
            }
        )
    changed_vs_audit = [r for r in index_rows if r.get("changed_vs_prior_audit")]
    comparison_table = []
    all_labels = sorted(set(prior_audit_counts) | set(class_counts) | set(PRIMARY_CLASSES))
    for label in all_labels:
        old_c = int(prior_audit_counts.get(label) or 0)
        new_c = int(class_counts.get(label) or 0)
        if old_c or new_c:
            comparison_table.append(
                {
                    "classification": label,
                    "old_count": old_c,
                    "new_count": new_c,
                    "change": new_c - old_c,
                    "evidence_basis": "repaired_classify_and_quantity_extractors_on_retained_evidence",
                }
            )

    summary = {
        "build": "002R" if export_label == EXPORT_ROOT_002R else "002Q",
        "directive": (
            "PROCUREMENT_INTELLIGENCE_REPAIR_VERIFICATION"
            if export_label == EXPORT_ROOT_002R
            else "COMPLETE_INDEPENDENT_AUDIT_EXPORT"
        ),
        "generated_at": _utc(),
        "elapsed_sec": round(time.time() - t0, 2),
        "total_records_processed": 8088,
        "new_classification_counts": dict(class_counts),
        "procurement_category_counts": dict(Counter(str(r.get("procurement_category")) for r in index_rows)),
        "decision_confidence_counts": dict(Counter(str(r.get("decision_confidence")) for r in index_rows)),
        "procurement_readiness_counts": dict(Counter(str(r.get("procurement_readiness")) for r in index_rows)),
        "changed_classifications": changed,
        "changed_vs_prior_audit_export": len(changed_vs_audit),
        "changed_vs_prior_audit_ids_sample": [r["nb_id"] for r in changed_vs_audit[:100]],
        "comparison_vs_prior_audit": comparison_table,
        "previous_rejections_overturned": overturned,
        "prior_incompatible_reconsidered": len(incompat_reconsider),
        "evidence_completeness": dict(completeness_counts),
        "records_with_full_source_evidence": completeness_counts.get(
            "substantial_retained_package", 0
        ),
        "records_with_substantial_retained_package": completeness_counts.get(
            "substantial_retained_package", 0
        ),
        "records_with_partial_source_evidence": completeness_counts.get(
            "partial_source_evidence", 0
        ),
        "records_missing_original_evidence": completeness_counts.get(
            "missing_original_evidence", 0
        ),
        "evidence_dimension_true_counts": dict(evidence_dim_true),
        "records_with_verified_quantities": qty_verified,
        "complete_source_backed_line_item_total": complete_line_item_total,
        "multi_line_complete_schedules": multi_line_complete,
        "multi_line_incomplete_not_counted_verified": multi_line_incomplete,
        "quantity_status_counts": dict(qty_status_counts),
        "prior_audit_source": prior_audit_source,
        "prior_audit_row_level_available": bool(prior_audit_by_nb),
        "prior_audit_note": prior_audit_note,
        "export_folder": str(root),
        "zip_files": [
            {"filename": a["filename"], "bytes": a["bytes"], "records": a["records"]}
            for a in zip_manifest
        ],
        "validation": validation,
        "api_expenditure": 0,
        "acquisition_locked": acquisition_locked(),
        "confirmed_example_checks": {
            "NB00376112": class_counts and True,  # filled below after loop — placeholder
        },
        "stop": "AWAIT_OWNER_REVIEW",
        "disclaimer": (
            "Software-generated decisions after canonical repair. "
            "Not independently verified — upload export for human/ChatGPT review."
        ),
    }
    # Spot-check confirmed failure IDs in this run (include line-item detail)
    spot = {}
    for nb in (
        "NB00376112",
        "NB00372623",
        "NB00375228",
        "NB00326707",
        "NB00265253",
        "NB00051362",
        "NB00328110",
        "NB00341368",
        "NB00291906",
        "NB00320329",
        # 002R.2 false-product regression IDs
        "NB00339526",
        "NB00219946",
        "NB00268024",
        "NB00275434",
    ):
        row = next((r for r in index_rows if r["nb_id"] == nb), None)
        if not row:
            continue
        # Load decision package for line items when present
        pkg = root / "by_classification" / str(row.get("primary_classification") or "") / nb
        decision_path = pkg / "C_software_decision.json"
        lines_out: list[dict[str, Any]] = []
        qty_sem = row.get("quantity_semantic") or row.get("quantity_status")
        if decision_path.exists():
            try:
                dec = json.loads(decision_path.read_text(encoding="utf-8"))
                qty_sem = dec.get("quantity_semantic") or qty_sem
                for li in dec.get("verified_line_items") or []:
                    lines_out.append(
                        {
                            "quantity": li.get("quantity"),
                            "product": (li.get("product_description") or "")[:80],
                            "snippet": (li.get("snippet") or "")[:100],
                        }
                    )
            except Exception:
                pass
        spot[nb] = {
            "primary": row.get("primary_classification"),
            "product_type": row.get("product_type"),
            "quantity": row.get("quantity"),
            "quantity_status": row.get("quantity_status"),
            "quantity_semantic": qty_sem,
            "verified_line_item_count": len(lines_out),
            "verified_line_items": lines_out,
            "prior_audit": row.get("prior_audit_classification"),
        }
    # Regression assertions for known cases (soft — reported, not raise during export)
    boom = spot.get("NB00326707") or {}
    forklift = spot.get("NB00320329") or {}
    kv = spot.get("NB00341368") or {}
    hrs = spot.get("NB00291906") or {}
    fp_survey = spot.get("NB00339526") or {}
    fp_booth = spot.get("NB00219946") or {}
    fp_furn = spot.get("NB00268024") or {}
    fp_fuel = spot.get("NB00275434") or {}
    regression = {
        "NB00326707_no_duplicate_boom": (
            boom.get("verified_line_item_count", 0) <= 1
            and boom.get("quantity_semantic") != "MULTI_LINE_ITEM"
            and str(boom.get("quantity") or "") in {"1", "1.0"}
        ),
        "NB00320329_four_distinct_lines": (
            int(forklift.get("verified_line_item_count") or 0) >= 4
            and forklift.get("quantity_semantic") == "MULTI_LINE_ITEM"
        ),
        "NB00341368_no_false_kv_qty": not bool(kv.get("quantity")),
        "NB00291906_no_false_hours_qty": not bool(hrs.get("quantity")),
        "NB00339526_not_product_vehicle_code": fp_survey.get("primary")
        not in {"CONFIRMED_PRODUCT", "POTENTIAL_PRODUCT", "MIXED_PRODUCT_SERVICE"},
        "NB00219946_not_product_booth_install": fp_booth.get("primary")
        not in {"CONFIRMED_PRODUCT", "POTENTIAL_PRODUCT", "MIXED_PRODUCT_SERVICE"},
        "NB00268024_not_product_furniture_prequal": fp_furn.get("primary")
        not in {"CONFIRMED_PRODUCT", "POTENTIAL_PRODUCT", "MIXED_PRODUCT_SERVICE"},
        "NB00275434_not_product_fuel_repair_prequal": fp_fuel.get("primary")
        not in {"CONFIRMED_PRODUCT", "POTENTIAL_PRODUCT", "MIXED_PRODUCT_SERVICE"},
    }
    summary["confirmed_example_checks"] = spot
    summary["regression_checks"] = regression
    summary["regression_all_passed"] = all(bool(v) for v in regression.values())
    if export_label.startswith("BUILD_002R2"):
        summary["build"] = "002R.2"
    elif export_label.startswith("BUILD_002R1"):
        summary["build"] = "002R.1"
    elif export_label.startswith("BUILD_002R"):
        summary["build"] = "002R"

    # Aggregate-only prior comparison when row-level prior missing
    archive_prior = (
        Path(__file__).resolve().parents[2]
        / "docs"
        / "acceptance_archive"
        / "BUILD_002R1_FINAL_CORRECTNESS_REPAIR.json"
    )
    if not prior_audit_by_nb and archive_prior.exists():
        try:
            arch = json.loads(archive_prior.read_text(encoding="utf-8"))
            summary["aggregate_comparison_vs_002R1_archive"] = {
                "note": "Row-level MASTER_INDEX for 002R1 unavailable; aggregate counts only",
                "prior_counts": arch.get("new_classification_counts"),
                "new_counts": dict(class_counts),
                "deltas": {
                    k: int(class_counts.get(k) or 0) - int((arch.get("new_classification_counts") or {}).get(k) or 0)
                    for k in sorted(set(class_counts) | set((arch.get("new_classification_counts") or {})))
                },
            }
        except Exception as exc:
            summary["aggregate_comparison_vs_002R1_archive"] = {
                "error": f"{type(exc).__name__}:{exc}"
            }

    # Offline paid-recovery plan (NOT executed)
    recovery = build_offline_paid_recovery_plan(index_rows, root)
    summary["paid_recovery_plan"] = {
        "status": "PREPARED_NOT_EXECUTED",
        "credits": recovery.get("credits"),
        "bucket_counts": recovery.get("bucket_counts"),
        "active_state_local_population": recovery.get("active_state_local_population_in_scope"),
        "evidence_backed_product_candidates": recovery.get("evidence_backed_product_candidates"),
        "top_50_estimated_credits": recovery.get(
            "top_50_estimated_credits_to_meaningful_requirements"
        ),
        "full_ranked_path": recovery.get("full_ranked_inventory_path"),
        "plan_path": recovery.get("plan_path"),
        "do_not_authorize_any_batch": True,
    }
    (root / "PAID_RECOVERY_PLAN.json").write_text(
        json.dumps(recovery, indent=2, default=str), encoding="utf-8"
    )

    # Durability self-check (offline, no API)
    if vault_durability_check:
        durability = run_evidence_vault_durability_check()
    else:
        durability = {"skipped": True, "reason": "read-only reprocess: evidence vault not written"}
    summary["evidence_vault_durability"] = durability
    (root / "EVIDENCE_VAULT_DURABILITY.json").write_text(
        json.dumps(durability, indent=2, default=str), encoding="utf-8"
    )

    # Persist MASTER_INDEX copy for future row-level comparisons
    archive_dir = Path(__file__).resolve().parents[2] / "docs" / "acceptance_archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    try:
        import shutil

        shutil.copy2(
            root / "indexes" / "MASTER_INDEX.jsonl",
            archive_dir / f"{export_label}_MASTER_INDEX.jsonl",
        )
        summary["archived_master_index"] = str(
            archive_dir / f"{export_label}_MASTER_INDEX.jsonl"
        )
    except Exception as exc:
        summary["archived_master_index_error"] = f"{type(exc).__name__}:{exc}"

    (root / "ACCEPTANCE_SUMMARY.json").write_text(
        json.dumps(summary, indent=2, default=str), encoding="utf-8"
    )
    (root / "ACCEPTANCE_SUMMARY.md").write_text(
        "\n".join(
            [
                f"# {summary.get('build', 'AUDIT')} — Acceptance summary",
                "",
                f"- Total processed: **{summary['total_records_processed']}**",
                f"- Classification counts: `{json.dumps(summary['new_classification_counts'])}`",
                f"- Changed vs inventory prior: **{changed}**",
                f"- Changed vs prior audit export (row-level): "
                f"**{summary.get('changed_vs_prior_audit_export')}** "
                f"(available={summary.get('prior_audit_row_level_available')})",
                f"- Prior audit note: {summary.get('prior_audit_note')}",
                f"- Prior rejections overturned: **{overturned}**",
                f"- Substantial retained package: **{summary.get('records_with_substantial_retained_package')}**",
                f"- Partial evidence: **{summary['records_with_partial_source_evidence']}**",
                f"- Evidence dimensions (true counts): "
                f"`{json.dumps(summary.get('evidence_dimension_true_counts'))}`",
                f"- Verified quantities (reconciled): **{qty_verified}**",
                f"- Complete source-backed line items: **{complete_line_item_total}**",
                f"- Regression all passed: **{summary.get('regression_all_passed')}** "
                f"`{json.dumps(regression)}`",
                f"- Durability ok: **{(durability or {}).get('ok')}**",
                f"- Product recovery candidates: "
                f"**{recovery.get('evidence_backed_product_candidates')}** "
                f"(top50 credits={recovery.get('top_50_estimated_credits_to_meaningful_requirements')})",
                f"- Export folder: `{root}`",
                f"- ZIP count: **{len(zip_manifest)}**",
                f"- Validation ok: **{validation['ok']}**",
                f"- Runtime: **{summary['elapsed_sec']}s**",
                f"- API expenditure: **0**",
                "",
                summary["disclaimer"],
                "",
                "STOP — AWAITING OWNER REVIEW. Killswitch remains LOCKED. No acquisition authorized.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    _log(f"DONE export={root} validation_ok={validation['ok']} elapsed={summary['elapsed_sec']}s")
    return summary


def build_offline_paid_recovery_plan(
    index_rows: list[dict[str, Any]], root: Path
) -> dict[str, Any]:
    """Complete ranked recovery inventory — zero API calls. NOT an authorization to spend.

    Rejects arbitrary first-N selection. Incompatible service/construction/vendor
    contracts are never proposed for paid detail/document calls.
    """
    from m3.research.nextbid_response_store import lookup_cached_response, store_root

    credits = {
        "used": 3453,
        "remaining": 2547,
        "protected_reserve": 1000,
        "maximum_potentially_spendable": 1547,
        "daily_limit": 100,
        "per_job_limit": 50,
        "daily_and_per_job_limits_retained": True,
        "page_size_max_supported": 25,
        "killswitch": "LOCKED",
        "execute": False,
        "prior_523_campaign_rejected": True,
    }
    missing_sl: dict[str, Any] | None = None
    try:
        cov_path = (
            get_settings().data_root()
            / "nextbid_checkpoints"
            / "build002q_controlled_coverage.json"
        )
        if cov_path.exists():
            cov = json.loads(cov_path.read_text(encoding="utf-8"))
            searches = cov.get("searches") or {}
            pages_left = sum(
                max(
                    0,
                    int(v.get("provider_total_pages") or 0)
                    - int(v.get("last_successful_page") or 0),
                )
                for v in searches.values()
                if v.get("completion_status") != "complete"
            )
            missing_sl = {
                "incomplete_search_partitions": sum(
                    1 for v in searches.values() if v.get("completion_status") != "complete"
                ),
                "listing_pages_remaining_estimate": pages_left,
                "endpoint": "GET /opportunities",
                "page_size": 25,
                "information_gain": "additional active state/local listing rows not yet in inventory",
                "estimated_credits": pages_left,
                "priority": "lower_than_product_detail_recovery",
            }
        else:
            missing_sl = {"note": "coverage checkpoint unavailable offline"}
    except Exception as exc:
        missing_sl = {"note": f"coverage read failed: {type(exc).__name__}"}

    paid_root = store_root()
    incompatible = {
        "CONFIRMED_SERVICE",
        "CONFIRMED_CONSTRUCTION",
        "VENDOR_QUALIFICATION",
        "OTHER_INCOMPATIBLE",
        "FEDERAL_PARKED",
        "DUPLICATE",
        "CANCELLED",
        "CLOSED_EXPIRED",
    }
    weak_title_re = re.compile(
        r"(?i)\b("
        r"psychiatr|mental\s+health|counseling|therapy\b|nursing\b|"
        r"installation\s+services?|pre[- ]?qual|surveys?|consulting|"
        r"engineering\s+services?|design\s+services?|staffing|"
        r"janitorial\s+services?|landscap(?:e|ing)\s+services?"
        r")\b"
    )

    ranked: list[dict[str, Any]] = []
    bucket_counts = Counter()
    active_sl = 0

    for row in index_rows:
        primary = str(row.get("primary_classification") or "")
        if primary in {"FEDERAL_PARKED", "DUPLICATE"}:
            continue
        notice = str(row.get("notice_status") or "")
        if primary in {"CANCELLED", "CLOSED_EXPIRED"} or notice in {
            "cancelled",
            "closed",
            "closed_by_deadline",
        }:
            continue
        active_sl += 1
        nb = str(row["nb_id"])
        title = str(row.get("title") or "")
        missing_raw = str(row.get("missing_evidence") or "")
        missing = [m for m in missing_raw.split(";") if m]
        listing_only = bool(row.get("examined_listing_summary_only"))
        dims_incomplete = "full_opportunity_detail" in missing_raw or listing_only

        # Vault: has detail / docs already been paid for?
        detail_cached = lookup_cached_response(
            "GET", f"/opportunities/{nb}", {}, nb_id=nb
        ) is not None
        docs_idx = paid_root / "by_nb_id" / f"{nb}.jsonl"
        docs_cached = False
        if docs_idx.exists():
            try:
                docs_cached = any(
                    '"/documents"' in line or "list_documents" in line
                    for line in docs_idx.read_text(encoding="utf-8").splitlines()
                )
            except Exception:
                docs_cached = False
        ft_path = get_settings().data_root() / "solicitation_full_text" / f"{nb}.json"
        has_stored_doc_text = ft_path.exists()

        # Relevance to product reseller — evidence-based, not confidence theater
        if primary == "CONFIRMED_PRODUCT":
            relevance = 0.85
            deliverable = "physical_product_purchase"
        elif primary == "POTENTIAL_PRODUCT":
            relevance = 0.55
            deliverable = "possible_physical_product"
        elif primary == "MIXED_PRODUCT_SERVICE":
            relevance = 0.45
            deliverable = "product_with_services"
        elif primary == "INSUFFICIENT_EVIDENCE":
            relevance = 0.25
            deliverable = "unresolved_procurement"
        else:
            relevance = 0.05
            deliverable = f"incompatible:{primary.lower()}"

        if weak_title_re.search(title):
            relevance = min(relevance, 0.15)
            deliverable = "likely_non_product_from_title_evidence"

        # B: sufficient only when qualification-critical fields present
        critical_missing = [
            m
            for m in missing
            if m
            in {
                "verified_quantity",
                "submission_instructions",
                "detailed_specifications",
                "full_solicitation_package_text",
                "full_opportunity_detail",
            }
        ]
        package_complete = (
            primary in {"CONFIRMED_PRODUCT", "POTENTIAL_PRODUCT", "MIXED_PRODUCT_SERVICE"}
            and not critical_missing
            and not listing_only
            and bool(row.get("quantity_verified") or row.get("verified_line_item_count"))
        )

        call_sequence: list[dict[str, Any]] = []
        bucket = "EXCLUDE_INCOMPATIBLE_NO_PAID_CALL"
        if primary in incompatible or relevance < 0.2:
            bucket = "EXCLUDE_INCOMPATIBLE_NO_PAID_CALL"
        elif package_complete:
            bucket = "B_sufficient_evidence_no_calls"
        elif primary in {
            "CONFIRMED_PRODUCT",
            "POTENTIAL_PRODUCT",
            "MIXED_PRODUCT_SERVICE",
            "INSUFFICIENT_EVIDENCE",
        }:
            if not detail_cached and (
                dims_incomplete
                or listing_only
                or row.get("evidence_completeness")
                in {"partial_source_evidence", "missing_original_evidence"}
            ):
                bucket = "C_need_provider_details"
                call_sequence.append(
                    {
                        "step": 1,
                        "endpoint": f"GET /opportunities/{nb}",
                        "already_called": False,
                        "response_in_vault": False,
                        "likely_new_information": True,
                        "information_gain": "full opportunity detail beyond listing summary",
                        "credits": 1,
                        "note": "Do not spend if provider only returns another short listing summary",
                    }
                )
            elif detail_cached and listing_only:
                # Detail already paid but still only a listing — do not re-pay detail
                bucket = "D_need_document_listing" if not docs_cached else "E_need_specific_documents"
            elif not docs_cached and not has_stored_doc_text:
                bucket = "D_need_document_listing"
            elif docs_cached and not has_stored_doc_text:
                bucket = "E_need_specific_documents"
            elif has_stored_doc_text and critical_missing:
                bucket = "F_docs_stored_but_processed_incorrectly"
            else:
                bucket = "C_need_provider_details" if not detail_cached else "D_need_document_listing"

            if bucket == "D_need_document_listing":
                call_sequence.append(
                    {
                        "step": len(call_sequence) + 1,
                        "endpoint": f"GET /opportunities/{nb}/documents",
                        "already_called": docs_cached,
                        "response_in_vault": docs_cached,
                        "likely_new_information": not docs_cached,
                        "information_gain": "attachment metadata / solicitation document links",
                        "credits": 0 if docs_cached else 1,
                        "note": "Attachment metadata alone is not a complete procurement package",
                    }
                )
            if bucket == "E_need_specific_documents":
                call_sequence.append(
                    {
                        "step": len(call_sequence) + 1,
                        "endpoint": "GET document download (selective solicitation package files)",
                        "already_called": False,
                        "response_in_vault": has_stored_doc_text,
                        "likely_new_information": not has_stored_doc_text,
                        "information_gain": "original solicitation document content",
                        "credits": 1,
                        "note": "Only after document listing identifies required files",
                    }
                )
            if bucket == "F_docs_stored_but_processed_incorrectly":
                call_sequence = []  # zero-spend reprocess

        # Chain credit estimate to obtain original document content
        chain_credits = sum(int(s.get("credits") or 0) for s in call_sequence)
        if bucket in {"C_need_provider_details", "D_need_document_listing"} and chain_credits < 2:
            # Typical path: detail + doc list + one download
            if bucket == "C_need_provider_details":
                chain_credits = 1 + (0 if docs_cached else 1) + (0 if has_stored_doc_text else 1)
            elif bucket == "D_need_document_listing":
                chain_credits = (0 if docs_cached else 1) + (0 if has_stored_doc_text else 1)

        deadline = row.get("deadline")
        # Prioritization score: relevance, urgency proxy, information need, avoid weak titles
        score = relevance * 100
        if primary == "CONFIRMED_PRODUCT":
            score += 30
        if critical_missing:
            score += 10
        if weak_title_re.search(title):
            score -= 50
        if bucket.startswith("EXCLUDE"):
            score = -100

        entry = {
            "nb_id": nb,
            "title": title[:160],
            "primary_classification": primary,
            "product_type": row.get("product_type"),
            "procurement_deliverable": deliverable,
            "relevance_to_product_reseller": round(relevance, 3),
            "relevance_basis": "classification_plus_title_evidence_not_confidence_score",
            "evidence_already_retained": {
                "evidence_completeness": row.get("evidence_completeness"),
                "listing_summary_only": listing_only,
                "detail_response_in_vault": detail_cached,
                "document_listing_in_vault": docs_cached,
                "solicitation_document_text_stored": has_stored_doc_text,
                "verified_line_item_count": row.get("verified_line_item_count"),
                "quantity_verified": row.get("quantity_verified"),
            },
            "specific_missing_information": critical_missing or missing,
            "bucket": bucket,
            "proposed_call_sequence": call_sequence,
            "endpoint_primary": (call_sequence[0]["endpoint"] if call_sequence else None),
            "endpoint_already_called": bool(
                call_sequence and call_sequence[0].get("already_called")
            ),
            "response_already_in_vault": bool(
                call_sequence and call_sequence[0].get("response_in_vault")
            ),
            "likely_new_useful_information": bool(
                call_sequence and call_sequence[0].get("likely_new_information")
            ),
            "expected_credits_this_step": int(call_sequence[0]["credits"]) if call_sequence else 0,
            "expected_credits_full_document_chain": chain_credits,
            "deadline_at": deadline,
            "prioritization_score": round(score, 2),
            "prioritization_reasons": [
                f"relevance={relevance}",
                f"primary={primary}",
                f"bucket={bucket}",
                f"critical_missing={critical_missing}",
            ],
        }
        bucket_counts[bucket] += 1
        ranked.append(entry)

    ranked.sort(
        key=lambda e: (-float(e["prioritization_score"]), str(e.get("deadline_at") or "9999"))
    )

    # Full inventory file (not a 25-row sample)
    full_path = root / "PAID_RECOVERY_FULL_RANKED.jsonl"
    with full_path.open("w", encoding="utf-8") as f:
        for e in ranked:
            f.write(json.dumps(e, default=str, ensure_ascii=False) + "\n")

    # Product candidates only for proposed spend consideration
    product_candidates = [
        e
        for e in ranked
        if e["bucket"]
        in {
            "C_need_provider_details",
            "D_need_document_listing",
            "E_need_specific_documents",
            "F_docs_stored_but_processed_incorrectly",
        }
        and float(e["relevance_to_product_reseller"]) >= 0.45
    ]
    top50 = product_candidates[:50]
    top50_credits = sum(int(e.get("expected_credits_full_document_chain") or 0) for e in top50)

    plan = {
        "build": "002R.2",
        "status": "PREPARED_NOT_EXECUTED",
        "generated_at": _utc(),
        "credits": credits,
        "active_state_local_population_in_scope": active_sl,
        "A_unacquired_active_state_local_listings": missing_sl,
        "bucket_counts": dict(bucket_counts),
        "full_ranked_inventory_path": str(full_path),
        "full_ranked_inventory_count": len(ranked),
        "evidence_backed_product_candidates": len(product_candidates),
        "top_50_with_call_sequences": top50,
        "top_50_estimated_credits_to_meaningful_requirements": top50_credits,
        "do_not_authorize_523_or_any_batch": True,
        "selection_rules": [
            "No arbitrary first-200 truncation for decision-making",
            "Full ranked inventory written to PAID_RECOVERY_FULL_RANKED.jsonl",
            "Incompatible service/construction/vendor/weak-title excluded from paid calls",
            "B requires verified qty + submission + specs — listing preview never sufficient",
            "Detail call rejected when only another short listing summary is expected",
            "Document-list calls not proposed for incompatible services",
            "Attachment metadata alone ≠ complete procurement package",
        ],
        "estimated_credits_if_authorized_top50_only": top50_credits,
        "plan_path": str(root / "PAID_RECOVERY_PLAN.json"),
    }
    return plan


def run_evidence_vault_durability_check() -> dict[str, Any]:
    """Offline durability checks: atomic write, no truncate, historical retain, restart-safe."""
    from m3.research.nextbid_response_store import (
        content_hash,
        lookup_cached_response,
        persist_paid_response,
        request_identity,
        store_root,
    )

    results: dict[str, Any] = {"ok": True, "checks": {}}
    try:
        body1 = {
            "nb_id": "DURABILITY_TEST_002R1",
            "description": "A" * 5000,
            "title": "Durability probe",
            "attachments": [{"name": "spec.pdf", "url": "https://example.invalid/spec.pdf"}],
        }
        p1 = persist_paid_response(
            method="GET",
            path="/opportunities/DURABILITY_TEST_002R1",
            params={},
            endpoint="get_opportunity",
            nb_id="DURABILITY_TEST_002R1",
            status_code=200,
            body=body1,
            job_id="002r1_durability",
            raw_text="X" * 10000,
        )
        results["checks"]["atomic_persist"] = bool(p1.get("persistence_ok"))
        # Body not truncated
        rid = request_identity(
            "GET", "/opportunities/DURABILITY_TEST_002R1", {}, nb_id="DURABILITY_TEST_002R1"
        )
        from m3.research.nextbid_response_store import load_by_request_id

        stored = load_by_request_id(rid)
        results["checks"]["body_not_truncated"] = (
            isinstance(stored, dict)
            and len(str((stored.get("body") or {}).get("description") or "")) == 5000
        )
        # Historical retain on update
        body2 = {
            "nb_id": "DURABILITY_TEST_002R1",
            "description": "B" * 100,
            "title": "Durability probe v2",
            "extra_field": "kept",
        }
        persist_paid_response(
            method="GET",
            path="/opportunities/DURABILITY_TEST_002R1",
            params={},
            endpoint="get_opportunity",
            nb_id="DURABILITY_TEST_002R1",
            status_code=200,
            body=body2,
            job_id="002r1_durability_v2",
        )
        stored2 = load_by_request_id(rid)
        hist = (stored2 or {}).get("historical_versions") or []
        merged_desc = str(((stored2 or {}).get("body") or {}).get("description") or "")
        results["checks"]["historical_versions_retained"] = len(hist) >= 1
        results["checks"]["merge_thicker_keeps_longer_text"] = len(merged_desc) >= 5000
        # Reuse without new call
        hit = lookup_cached_response(
            "GET", "/opportunities/DURABILITY_TEST_002R1", {}, nb_id="DURABILITY_TEST_002R1"
        )
        results["checks"]["reuse_cached_paid_response"] = bool(hit and hit.get("_durable_store_hit"))
        # Store root exists and index appendable
        results["checks"]["store_root_exists"] = store_root().exists()
        results["checks"]["content_hash_stable"] = content_hash(body1) == content_hash(dict(body1))
    except Exception as exc:
        results["ok"] = False
        results["error"] = f"{type(exc).__name__}:{exc}"
    results["ok"] = results.get("ok", True) and all(
        bool(v) for v in (results.get("checks") or {}).values()
    )
    return results
