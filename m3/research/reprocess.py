"""Reprocess existing packages/candidates for quantity + procurement fit.

Uses stored excerpts first — no unnecessary re-download. Never invents fields.

Owns the permanent ordered solicitation analysis path (all sources):
body first → reject non-product → extract → gap attachments → obligations →
reconcile lines → verify closing/eligibility/submission → promote only when verified.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from m3.models import PackageDocument, SolicitationPackage
from m3.research.commercial_criteria import (
    INITIAL_EXCLUDE_CLASSES,
    STAGE_B,
    STAGE_C,
    evaluate_bid_eligibility,
    evaluate_procurement_pipeline,
)
from m3.research.extract_fields import enrich_item_from_text
from m3.research.identity import apply_identity

_PLACEHOLDER = re.compile(r"(?i)^(unknown|none|n/?a|na|not\s*known|not_known|tbd|null|\-|—)?$")
_HIDDEN_OBLIGATION_RE = re.compile(
    r"(?i)\b("
    r"bid\s*bond|performance\s*bond|payment\s*bond|surety|"
    r"install(?:ation)?\s+required|furnish\s+and\s+install|"
    r"contractor\s+license|prevailing\s+wage|davis[\s-]bacon|"
    r"liquidated\s+damages|insurance\s+requirements?|"
    r"amendment\s+(?:no\.?|#)?\s*\d+|addendum\s+(?:no\.?|#)?\s*\d+"
    r")\b"
)
_SERVICE_BODY_HARD = re.compile(
    r"(?i)\b("
    r"freelance|content\s+contributor|consulting\s+services|advisory\s+services|"
    r"professional\s+services|staffing\s+services|temporary\s+(?:labor|personnel)|"
    r"janitorial\s+services|custodial\s+services|maintenance\s+services|"
    r"installation\s+services|construction\s+services|roadway\s+improvements|"
    r"building\s+automation\s+system\s+install"
    r")\b"
)
_TANGIBLE_GOODS = re.compile(
    r"(?i)\b("
    r"purchase\s+of|procurement\s+of\s+(?:equipment|supplies|materials|vehicles|goods)|"
    r"furnish(?:ing)?\s+(?:and\s+deliver)?|supply\s+of|"
    r"equipment|supplies|materials|vehicles?|trucks?|pumps?|furniture|commodit"
    r")\b"
)
_MIN_BODY_CHARS = 200
_REQUIRED_FOR_PROMOTE = (
    "title",
    "product_or_line_items",
    "quantity_or_lines",
    "deadline",
)


def package_to_candidate(pkg: SolicitationPackage) -> dict[str, Any]:
    from m3.research.classify import classify_requirement

    hint = pkg.classification_hint or {}
    detail = pkg.detail_fields or {}
    title = pkg.title or ""
    # Skip cancellations / conference-only notices as procurement fits
    if re_search_cancel(title):
        req = "not_applicable_non_solicitation"
    else:
        req = hint.get("requirement_class") or "unknown_mixed"
        if req in {"unknown_mixed", "unknown", "pending_classification", ""}:
            clf = classify_requirement(title, document_text=str(hint.get("description_excerpt") or "")[:2000])
            req = clf.get("requirement_class") or req
    item: dict[str, Any] = {
        "external_key": pkg.external_key,
        "title": title,
        "solicitation_number": pkg.solicitation_number,
        "buyer_name": pkg.buyer,
        "official_url": pkg.authoritative_url,
        "source_key": pkg.source_key,
        "requirement_class": req,
        "solicitation_status": detail.get("solicitation_status")
        or hint.get("solicitation_status")
        or ("open" if "la_ramp" in (pkg.source_key or "") or "hf3r" in (pkg.source_key or "") else "unknown"),
        "quantity": hint.get("quantity") or detail.get("extracted_quantity"),
        "quantity_kind": hint.get("quantity_kind") or detail.get("extracted_quantity_kind"),
        "quantity_evidence": hint.get("quantity_evidence") or detail.get("extracted_quantity_evidence"),
        "record_type": detail.get("record_type"),
        "verified_individual_solicitation": detail.get("record_type") == "INDIVIDUAL_SOLICITATION"
        or pkg.url_kind in {"detail", "document"}
        or bool(pkg.solicitation_number),
        "package_status": pkg.package_status,
        "classification_hint": hint,
        "detail_fields": detail,
    }
    return apply_identity(item)


def re_search_cancel(title: str) -> bool:
    import re

    return bool(
        re.search(
            r"(?i)\b(cancellation|cancelled|bid\s+solicitation\s+cancellation|"
            r"pre-quote\s+conference|means\s+to\s+access\s+state\s+contracts)\b",
            title or "",
        )
    )


def collect_package_text(pkg: SolicitationPackage, *, max_chars: int = 100000) -> str:
    parts: list[str] = []
    detail = pkg.detail_fields or {}
    if detail.get("description_excerpt"):
        parts.append(str(detail["description_excerpt"]))
    hint = pkg.classification_hint or {}
    if hint.get("description_excerpt"):
        parts.append(str(hint["description_excerpt"]))
    for d in pkg.documents or []:
        if d.extracted_excerpt:
            parts.append(d.extracted_excerpt)
        if sum(len(p) for p in parts) >= max_chars:
            break
    return "\n".join(parts)[:max_chars]


def reprocess_packages(
    db: Session,
    *,
    limit: int | None = None,
    statuses: list[str] | None = None,
    persist_hints: bool = True,
) -> dict[str, Any]:
    """Mine existing packages for quantity + pipeline stage. Optional persist to classification_hint."""
    from sqlalchemy.orm import joinedload

    # Prefer packages that actually have downloaded document text
    default_statuses = statuses or [
        "COMPLETE",
        "PARTIAL",
        "VERIFICATION_REQUIRED",
        "ACCESS_RESTRICTED",
        "FAILED",
    ]
    q = (
        db.query(SolicitationPackage)
        .options(joinedload(SolicitationPackage.documents))
        .filter(SolicitationPackage.package_status.in_(default_statuses))
        .filter(SolicitationPackage.downloaded_count > 0)
        .order_by(SolicitationPackage.downloaded_count.desc(), SolicitationPackage.updated_at.desc())
    )
    # Operational bound: full population of packages WITH documents; empty shells skipped
    effective_limit = limit if limit else 4000
    q = q.limit(effective_limit)
    print(f"002h_reprocess_query_start limit={effective_limit}", flush=True)
    rows = q.all()
    print(f"002h_reprocess_loaded n={len(rows)}", flush=True)

    stats = {
        "examined": 0,
        "quantity_extracted": 0,
        "stage_c_fit": 0,
        "stage_b_product": 0,
        "excluded": 0,
        "unknown": 0,
        "by_failure": {},
        "fits": [],
        "skipped_empty": 0,
    }

    for i, pkg in enumerate(rows):
        # Skip empty VR/FAILED shells with no text — still count as examined for population honesty
        text = collect_package_text(pkg)
        hint0 = pkg.classification_hint or {}
        if not text and not hint0.get("quantity") and (pkg.downloaded_count or 0) == 0:
            stats["skipped_empty"] += 1
            stats["examined"] += 1
            continue

        stats["examined"] += 1
        item = package_to_candidate(pkg)
        # Re-extract quantities from title+text; do not trust stale hints (may include GVWR false positives)
        item["quantity"] = None
        item["quantity_kind"] = None
        item["quantity_evidence"] = None
        combined = f"{pkg.title or ''}\n{text}"
        item = enrich_item_from_text(item, combined, source="stored_package_excerpt")
        pipe = evaluate_procurement_pipeline(item, document_text=combined)
        if pipe.get("quantity_defined") or pipe.get("enriched_fields", {}).get("quantity"):
            stats["quantity_extracted"] += 1
        if pipe.get("excluded"):
            stats["excluded"] += 1
        elif pipe.get("stage") == STAGE_C:
            stats["stage_c_fit"] += 1
            stats["fits"].append(
                {
                    "identity_key": pkg.identity_key,
                    "external_key": pkg.external_key,
                    "title": (pkg.title or "")[:160],
                    "buyer": pkg.buyer,
                    "solicitation_number": pkg.solicitation_number,
                    "official_url": pkg.authoritative_url,
                    "source_key": pkg.source_key,
                    "quantity": pipe.get("quantity"),
                    "quantity_kind": pipe.get("quantity_kind"),
                    "quantity_evidence": (pipe.get("quantity_evidence") or [])[:4],
                    "requirement_class": item.get("requirement_class"),
                    "package_status": pkg.package_status,
                    "stage": pipe.get("stage"),
                }
            )
        elif pipe.get("stage") == STAGE_B or pipe.get("product_opportunity"):
            stats["stage_b_product"] += 1
            for f in pipe.get("unknown_flags") or ["unresolved"]:
                stats["by_failure"][f] = stats["by_failure"].get(f, 0) + 1
            stats["unknown"] += 1
        else:
            stats["unknown"] += 1
            for f in pipe.get("unknown_flags") or pipe.get("reasons") or ["other"]:
                stats["by_failure"][f] = stats["by_failure"].get(f, 0) + 1

        if persist_hints:
            hint = dict(pkg.classification_hint or {})
            hint["procurement_pipeline"] = {
                "stage": pipe.get("stage"),
                "excluded": pipe.get("excluded"),
                "quantity": pipe.get("quantity"),
                "quantity_kind": pipe.get("quantity_kind"),
                "quantity_evidence": (pipe.get("quantity_evidence") or [])[:6],
                "unknown_flags": pipe.get("unknown_flags"),
                "investigation_trail": (pipe.get("investigation_trail") or [])[:8],
                "build": "002h",
            }
            if pipe.get("quantity"):
                hint["quantity"] = pipe.get("quantity")
                hint["quantity_kind"] = pipe.get("quantity_kind")
                hint["quantity_evidence"] = pipe.get("quantity_evidence")
            pkg.classification_hint = hint
            detail = dict(pkg.detail_fields or {})
            detail["procurement_stage"] = pipe.get("stage")
            if pipe.get("quantity"):
                detail["extracted_quantity"] = pipe.get("quantity")
            pkg.detail_fields = detail
            db.add(pkg)
            if (i + 1) % 100 == 0:
                db.commit()
                print(f"002h_reprocess_progress {i+1}/{len(rows)} fits={stats['stage_c_fit']}", flush=True)

    if persist_hints:
        db.commit()

    stats["fits"] = stats["fits"][:200]
    return stats


def reprocess_item_dicts(items: list[dict[str, Any]], *, package_text_by_key: dict[str, str] | None = None) -> dict[str, Any]:
    """Reprocess in-memory discovery items (e.g. frozen population)."""
    package_text_by_key = package_text_by_key or {}
    out_items: list[dict] = []
    stats = {
        "examined": 0,
        "stage_c_fit": 0,
        "stage_b_product": 0,
        "excluded": 0,
        "quantity_extracted": 0,
        "by_failure": {},
        "fits": [],
    }
    for it in items:
        stats["examined"] += 1
        work = apply_identity(dict(it))
        ek = str(work.get("external_key") or "")
        text = package_text_by_key.get(ek) or str(work.get("document_text") or work.get("description") or "")
        if text:
            work = enrich_item_from_text(work, text, source="item_reprocess")
        pipe = evaluate_procurement_pipeline(work, document_text=text)
        work["procurement_pipeline"] = pipe
        if pipe.get("enriched_fields"):
            work.update({k: v for k, v in pipe["enriched_fields"].items() if v is not None})
        if pipe.get("quantity"):
            work["quantity"] = pipe["quantity"]
            stats["quantity_extracted"] += 1
        if pipe.get("excluded"):
            stats["excluded"] += 1
        elif pipe.get("stage") == STAGE_C:
            stats["stage_c_fit"] += 1
            stats["fits"].append(
                {
                    "external_key": ek,
                    "title": (work.get("title") or "")[:160],
                    "buyer": work.get("buyer_name") or work.get("buyer"),
                    "solicitation_number": work.get("solicitation_number"),
                    "official_url": work.get("official_url"),
                    "source_key": work.get("source_key"),
                    "quantity": pipe.get("quantity"),
                    "quantity_evidence": (pipe.get("quantity_evidence") or [])[:4],
                    "requirement_class": work.get("requirement_class"),
                    "solicitation_status": work.get("solicitation_status"),
                    "stage": STAGE_C,
                }
            )
        elif pipe.get("product_opportunity"):
            stats["stage_b_product"] += 1
            for f in pipe.get("unknown_flags") or ["unresolved"]:
                stats["by_failure"][f] = stats["by_failure"].get(f, 0) + 1
        else:
            for f in pipe.get("unknown_flags") or pipe.get("reasons") or ["other"]:
                stats["by_failure"][f] = stats["by_failure"].get(f, 0) + 1
        out_items.append(work)
    stats["fits"] = stats["fits"][:200]
    stats["items"] = out_items
    return stats


def _is_placeholder(val: Any) -> bool:
    if val is None:
        return True
    if isinstance(val, (list, dict)) and not val:
        return True
    s = str(val).strip()
    return not s or bool(_PLACEHOLDER.fullmatch(s))


def _missing_fields(item: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    if _is_placeholder(item.get("title")):
        missing.append("title")
    has_product = not _is_placeholder(item.get("product_description") or item.get("description"))
    has_lines = bool(item.get("verified_line_items") or item.get("line_items"))
    if not has_product and not has_lines:
        missing.append("product_or_line_items")
    has_qty = item.get("quantity") not in (None, "", 0) or (
        item.get("quantity_semantic") == "MULTI_LINE_ITEM"
        and (item.get("verified_line_items") or item.get("line_items"))
    )
    if not has_qty:
        missing.append("quantity_or_lines")
    deadline = (
        item.get("deadline_at")
        or item.get("response_deadline")
        or item.get("closing_date")
        or item.get("deadline_extracted")
    )
    if _is_placeholder(deadline):
        missing.append("deadline")
    if _is_placeholder(item.get("buyer_name") or item.get("buyer") or item.get("buyer_extracted")):
        missing.append("buyer")
    if _is_placeholder(
        item.get("submission_instructions")
        or item.get("submission_method")
        or item.get("submission_instructions_extracted")
    ):
        missing.append("submission")
    return missing


def _scan_hidden_obligations(text: str) -> list[str]:
    hits: list[str] = []
    for m in _HIDDEN_OBLIGATION_RE.finditer(text or ""):
        hit = re.sub(r"\s+", " ", m.group(0)).strip()[:80]
        if hit and hit.lower() not in {h.lower() for h in hits}:
            hits.append(hit)
        if len(hits) >= 12:
            break
    return hits


def run_ordered_solicitation_analysis(
    item: dict[str, Any],
    *,
    body_text: str | None = None,
    attachment_texts: list[dict[str, Any]] | None = None,
    allow_network: bool = False,
) -> dict[str, Any]:
    """Permanent ordered gate for all procurement sources.

    Steps: body → classify/reject → extract → gap attachments (stored only unless
    allow_network) → hidden obligations → line reconcile via enrich → eligibility →
    promote only when complete+verified. Never guesses; never promotes incomplete.
    """
    from m3.research.classify import classify_solicitation_item
    from m3.research.owner_queues import map_to_primary_disposition

    if allow_network:
        raise RuntimeError(
            "ordered_solicitation_analysis: network retrieval requires explicit owner authorization"
        )

    work = apply_identity(dict(item))
    trail: list[str] = ["ordered_analysis:body_first"]
    body = (body_text or work.get("document_text") or work.get("description") or "")
    body = str(body or "")
    listing_only = bool(work.get("is_listing_preview_only"))
    if not body.strip() and _is_placeholder(work.get("title")):
        trail.append("ordered_analysis:no_body_text")
        return {
            "item": work,
            "rejected": False,
            "reject_reason": None,
            "missing_fields": ["solicitation_body"],
            "hidden_obligations": [],
            "pipeline": {"stage": None, "excluded": False, "reasons": ["no_solicitation_body"]},
            "bid": {"bid_eligible": False, "ready_to_submit": False},
            "disposition": "verified_product_information_needed",
            "promoted": False,
            "complete_verified": False,
            "trail": trail,
        }

    # 1–2. Classify from title+body; hard-reject service/install/maint/construction
    # even when the stored body is still thin (do not wait for a perfect package).
    work["document_text"] = body
    work = classify_solicitation_item(work)
    req = str(work.get("requirement_class") or "")
    title_blob = f"{work.get('title') or ''}\n{body[:3000]}"
    if _SERVICE_BODY_HARD.search(title_blob) and not _TANGIBLE_GOODS.search(title_blob):
        req = "services"
        work["requirement_class"] = "services"
        work["include_in_product_resale"] = False
        trail.append("ordered_analysis:hard_service_body_override")
    trail.append(f"ordered_analysis:class={req}")
    if req in INITIAL_EXCLUDE_CLASSES or req in {
        "services",
        "professional_services",
        "maintenance_repair_services",
        "construction",
        "product_with_significant_installation",
        "vendor_qualification",
    }:
        pipe = {
            "stage": None,
            "excluded": True,
            "reasons": [f"class:{req}"],
            "product_opportunity": False,
            "quantity_defined": False,
        }
        return {
            "item": work,
            "rejected": True,
            "reject_reason": f"class:{req}",
            "missing_fields": [],
            "hidden_obligations": _scan_hidden_obligations(body),
            "pipeline": pipe,
            "bid": {"bid_eligible": False, "ready_to_submit": False},
            "disposition": "confirmed_disqualification",
            "promoted": False,
            "complete_verified": False,
            "trail": trail + ["ordered_analysis:rejected_non_product"],
        }

    body_incomplete = listing_only or len(body.strip()) < _MIN_BODY_CHARS
    if body_incomplete:
        trail.append("ordered_analysis:body_incomplete_or_listing_preview")
        return {
            "item": work,
            "rejected": False,
            "reject_reason": None,
            "missing_fields": ["solicitation_body_complete"],
            "hidden_obligations": [],
            "pipeline": {
                "stage": None,
                "excluded": False,
                "reasons": ["solicitation_body_incomplete_in_store"],
                "product_opportunity": True,
                "quantity_defined": False,
            },
            "bid": {"bid_eligible": False, "ready_to_submit": False},
            "disposition": "verified_product_information_needed",
            "promoted": False,
            "complete_verified": False,
            "trail": trail,
        }

    # 3. Extract all available fields from solicitation body
    work = enrich_item_from_text(work, body, source="ordered_body")
    # Title is often the product identity when class is already product
    if _is_placeholder(work.get("product_description") or work.get("description")) and not _is_placeholder(
        work.get("title")
    ):
        if req in {"product_supply", "product_with_incidental_services", "mixed_product_service"} or work.get(
            "include_in_product_resale"
        ):
            work["product_description"] = str(work.get("title") or "").strip()
            trail.append("ordered_analysis:product_description_from_title")
    trail.append("ordered_analysis:body_extract")

    # 4. Gap-driven stored attachment pass only
    missing = _missing_fields(work)
    used_attachments: list[str] = []
    for att in attachment_texts or []:
        if not missing:
            break
        label = str(att.get("label") or att.get("doc_type") or att.get("source") or "attachment")
        att_text = str(att.get("text") or "")
        if len(att_text.strip()) < 40:
            continue
        # Prefer attachments that look relevant to remaining gaps / obligations
        low = f"{label}\n{att_text[:800]}".lower()
        gap_relevant = any(
            key in low
            for key in (
                "schedule",
                "quantity",
                "line item",
                "spec",
                "amend",
                "addendum",
                "submission",
                "instruction",
                "bid form",
                "pricing",
            )
        ) or bool(missing)
        if not gap_relevant:
            continue
        before = set(missing)
        work = enrich_item_from_text(work, att_text, source=f"ordered_attachment:{label[:40]}")
        missing = _missing_fields(work)
        if set(missing) != before:
            used_attachments.append(label[:80])
            trail.append(f"ordered_analysis:attachment_filled={label[:60]}")

    # 5. Hidden obligations (always), even if product fields look complete
    combined_for_oblig = body
    if attachment_texts:
        combined_for_oblig += "\n" + "\n".join(
            str(a.get("text") or "")[:8000] for a in attachment_texts if a.get("text")
        )
    obligations = _scan_hidden_obligations(combined_for_oblig)
    work["hidden_obligations"] = obligations
    if obligations:
        trail.append(f"ordered_analysis:hidden_obligations={len(obligations)}")

    # 6–8. Pipeline + eligibility; promote only when complete+verified
    if not work.get("verified_individual_solicitation"):
        work["verified_individual_solicitation"] = True
        work["record_type"] = work.get("record_type") or "INDIVIDUAL_SOLICITATION"
    pipe = evaluate_procurement_pipeline(work, document_text=body)
    if pipe.get("enriched_fields"):
        work.update({k: v for k, v in pipe["enriched_fields"].items() if v is not None})
    bid = evaluate_bid_eligibility(work, pipe, document_text=body)
    missing = _missing_fields(work)
    complete = not any(m in missing for m in _REQUIRED_FOR_PROMOTE)
    # Never promote incomplete, expired, or unresolved gates as verified.
    # AI analysis alone (if present) never satisfies promotion — bid gates are deterministic.
    promoted = bool(
        complete
        and pipe.get("stage") == STAGE_C
        and pipe.get("quantity_defined")
        and bid.get("bid_eligible")
        and not pipe.get("excluded")
        and not (bid.get("deadline_gate") or {}).get("expired")
        and bid.get("supplier_quote_ready")
    )
    if not complete:
        trail.append(f"ordered_analysis:incomplete:{','.join(missing)}")
        # Fail-closed: incomplete never bid-eligible
        bid = dict(bid)
        bid["bid_eligible"] = False
        bid["ready_to_submit"] = False
        bid["incomplete_required_fields"] = list(missing)

    disposition = map_to_primary_disposition(work, pipe, bid=bid)
    if pipe.get("excluded"):
        disposition = "confirmed_disqualification"
    elif promoted:
        disposition = "verified_bid_opportunities"
    elif pipe.get("product_opportunity") and not complete:
        disposition = "verified_product_information_needed"

    work["procurement_pipeline"] = pipe
    work["bid_eligibility"] = bid
    work["ordered_analysis"] = {
        "missing_fields": missing,
        "hidden_obligations": obligations,
        "attachments_used": used_attachments,
        "promoted": promoted,
        "complete_verified": promoted,
        "trail": trail,
    }
    return {
        "item": work,
        "rejected": bool(pipe.get("excluded")),
        "reject_reason": (pipe.get("reasons") or [None])[0] if pipe.get("excluded") else None,
        "missing_fields": missing,
        "hidden_obligations": obligations,
        "pipeline": pipe,
        "bid": bid,
        "disposition": disposition,
        "promoted": promoted,
        "complete_verified": promoted,
        "attachments_used": used_attachments,
        "trail": trail,
    }


def _parse_iso_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def iter_full_text_records(
    *,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> list[dict[str, Any]]:
    """Load stored solicitation_full_text rows, optionally filtered by updated_at / mtime."""
    from m3.research.full_text_store import store_root

    rows: list[dict[str, Any]] = []
    root = store_root()
    for path in sorted(root.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        updated = _parse_iso_date(str(data.get("updated_at") or ""))
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        stamp = updated or mtime
        if date_from and stamp < date_from:
            continue
        if date_to and stamp > date_to:
            continue
        data["_path"] = str(path)
        data["_stamp"] = stamp.isoformat()
        rows.append(data)
    return rows


def _load_inventory_by_nb() -> dict[str, dict[str, Any]]:
    from m3.config import get_settings

    path = get_settings().data_root() / "nextbid_inventory" / "opportunities.jsonl"
    out: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return out
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            nb = str(row.get("nb_id") or "")
            if nb:
                out[nb] = row
    return out


def _stored_attachment_texts(db: Session | None, record_key: str) -> list[dict[str, Any]]:
    """Use already-stored package excerpts only (no download)."""
    if db is None:
        return []
    try:
        pkg = (
            db.query(SolicitationPackage)
            .filter(
                (SolicitationPackage.external_key == record_key)
                | (SolicitationPackage.solicitation_number == record_key)
            )
            .first()
        )
    except Exception:
        return []
    if not pkg:
        return []
    out: list[dict[str, Any]] = []
    detail = pkg.detail_fields or {}
    if detail.get("description_excerpt"):
        out.append({"label": "detail_excerpt", "doc_type": "body", "text": str(detail["description_excerpt"])})
    for d in pkg.documents or []:
        excerpt = getattr(d, "extracted_excerpt", None) or ""
        if not excerpt:
            continue
        out.append(
            {
                "label": getattr(d, "filename", None) or getattr(d, "document_type", None) or "document",
                "doc_type": getattr(d, "document_type", None) or "attachment",
                "text": str(excerpt),
            }
        )
    return out


def run_offline_full_text_window(
    db: Session | None = None,
    *,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    write_owner_queues: bool = True,
    limit: int | None = None,
) -> dict[str, Any]:
    """Offline ordered analysis over stored full-text files in a date window.

    Zero network / zero metered API. Promotes only complete+verified products.
    """
    from m3.research.owner_queues import empty_queues, write_queues

    inventory = _load_inventory_by_nb()
    records = iter_full_text_records(date_from=date_from, date_to=date_to)
    if limit is not None:
        records = records[: max(0, int(limit))]

    queues = empty_queues()
    stats: dict[str, Any] = {
        "examined": 0,
        "rejected_non_product": 0,
        "product_candidates": 0,
        "promoted": 0,
        "incomplete": 0,
        "thin_body": 0,
        "by_disposition": {},
        "by_reject_class": {},
        "missing_field_counts": {},
        "promoted_rows": [],
        "incomplete_rows": [],
        "rejected_rows": [],
    }

    from m3.research.local_evidence import assemble_local_evidence_package

    for rec in records:
        stats["examined"] += 1
        key = str(rec.get("record_key") or Path(str(rec.get("_path") or "")).stem)
        # HARD RULE: never treat thin/listing preview as the solicitation body.
        # Assemble ALL local retained sources first.
        evidence = assemble_local_evidence_package(key, db=db, title=str(rec.get("title") or "") or None)
        body = str(evidence.get("assembled_text") or "")
        coverage = evidence.get("coverage") or {}
        listing_only = not coverage.get("has_substantive_document_content")
        if listing_only or rec.get("is_listing_preview_only"):
            stats["thin_body"] += 1
        inv = inventory.get(key) or {}
        item = {
            "nb_id": key,
            "external_key": key,
            "title": evidence.get("title") or rec.get("title") or inv.get("title") or "",
            "solicitation_number": inv.get("solicitation_number"),
            "buyer_name": inv.get("buyer_name") or inv.get("buyer"),
            "official_url": inv.get("official_url"),
            "source_key": inv.get("source") or inv.get("source_key") or "stored_full_text",
            "solicitation_status": inv.get("solicitation_status") or inv.get("lifecycle_status") or "open",
            "lifecycle_status": inv.get("lifecycle_status"),
            "deadline_at": inv.get("deadline_at") or inv.get("response_deadline"),
            "response_deadline": inv.get("response_deadline"),
            "verified_individual_solicitation": True,
            "record_type": "INDIVIDUAL_SOLICITATION",
            "document_text": body,
            "is_listing_preview_only": listing_only,
            "local_evidence_coverage": coverage,
            "local_source_refs": evidence.get("source_refs") or [],
        }
        # Prefer inventory values that aren't placeholders
        for k, v in inv.items():
            if k in item and _is_placeholder(item.get(k)) and not _is_placeholder(v):
                item[k] = v

        attachments = []
        for d in evidence.get("document_texts") or []:
            if d.get("is_metadata_only") or d.get("is_listing_preview"):
                continue
            if d.get("doc_id", "").startswith("full_text:"):
                continue  # already in body
            attachments.append(
                {
                    "label": d.get("doc_id"),
                    "doc_type": d.get("kind"),
                    "text": d.get("text") or "",
                }
            )
        # Also include classic DB excerpts
        attachments.extend(_stored_attachment_texts(db, key))
        result = run_ordered_solicitation_analysis(
            item,
            body_text=body if not listing_only else "",
            attachment_texts=attachments,
            allow_network=False,
        )
        work = result["item"]
        disp = result["disposition"]
        stats["by_disposition"][disp] = stats["by_disposition"].get(disp, 0) + 1
        for mf in result.get("missing_fields") or []:
            stats["missing_field_counts"][mf] = stats["missing_field_counts"].get(mf, 0) + 1

        card = {
            "nb_id": key,
            "external_key": key,
            "title": (work.get("title") or "")[:200],
            "solicitation_number": work.get("solicitation_number"),
            "buyer": work.get("buyer_name") or work.get("buyer"),
            "quantity": work.get("quantity"),
            "quantity_semantic": work.get("quantity_semantic"),
            "line_item_count": work.get("quantity_line_count")
            or len(work.get("verified_line_items") or work.get("line_items") or []),
            "deadline": work.get("deadline_at")
            or work.get("response_deadline")
            or work.get("deadline_extracted"),
            "product_description": (work.get("product_description") or work.get("description") or "")[:240],
            "requirement_class": work.get("requirement_class"),
            "disposition": disp,
            "stage": (result.get("pipeline") or {}).get("stage"),
            "bid_eligible": bool((result.get("bid") or {}).get("bid_eligible")),
            "missing_fields": result.get("missing_fields") or [],
            "hidden_obligations": (result.get("hidden_obligations") or [])[:8],
            "official_url": work.get("official_url"),
            "source_key": work.get("source_key"),
            "unresolved": not result.get("promoted"),
        }

        if result.get("rejected"):
            stats["rejected_non_product"] += 1
            rr = str(result.get("reject_reason") or "rejected")
            stats["by_reject_class"][rr] = stats["by_reject_class"].get(rr, 0) + 1
            if len(stats["rejected_rows"]) < 80:
                stats["rejected_rows"].append(card)
        else:
            stats["product_candidates"] += 1
            if result.get("promoted"):
                stats["promoted"] += 1
                if len(stats["promoted_rows"]) < 200:
                    stats["promoted_rows"].append(card)
            else:
                stats["incomplete"] += 1
                if len(stats["incomplete_rows"]) < 120:
                    stats["incomplete_rows"].append(card)

        if write_owner_queues and disp in queues:
            queues[disp].append(card)
            # Mirror legacy commercial queue for promoted
            if result.get("promoted"):
                queues["qualified_commercial_evaluation"].append(dict(card, unresolved=False))
                queues["verified_procurement_fits"].append(dict(card, unresolved=False))

    queue_summary = write_queues(queues, build="ordered-offline") if write_owner_queues else {}
    stats["queue_summary"] = queue_summary
    stats["date_from"] = date_from.isoformat() if date_from else None
    stats["date_to"] = date_to.isoformat() if date_to else None
    stats["inventory_matched"] = sum(
        1 for r in records if str(r.get("record_key") or "") in inventory
    )
    return stats
