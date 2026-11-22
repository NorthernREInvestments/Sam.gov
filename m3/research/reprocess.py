"""Reprocess existing packages/candidates for quantity + procurement fit (Build 002H).

Uses stored excerpts first — no unnecessary re-download. Never invents fields.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from m3.models import PackageDocument, SolicitationPackage
from m3.research.commercial_criteria import STAGE_B, STAGE_C, evaluate_procurement_pipeline
from m3.research.extract_fields import enrich_item_from_text
from m3.research.identity import apply_identity


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


def collect_package_text(pkg: SolicitationPackage, *, max_chars: int = 20000) -> str:
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
