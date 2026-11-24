"""Offline NextBid inventory audit + product recovery (Build 002Q).

ZERO metered NextBid API calls. Uses checkpoint, inventory, packages, prior evidence.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session, joinedload

from m3.models import SolicitationPackage
from m3.research.classify import PRODUCT_CLASSES, classify_batch
from m3.research.commercial_criteria import (
    INITIAL_EXCLUDE_CLASSES,
    INITIAL_PRODUCT_OK,
    STAGE_C,
    evaluate_bid_eligibility,
    evaluate_procurement_pipeline,
)
from m3.research.document_quality import is_junk_package_excerpt
from m3.research.nextbid_inventory import load_inventory, upsert_inventory
from m3.research.nextbid_recovery import (
    _restore_002i_fit_evidence,
    collect_population,
    listing_prefilter,
)
from m3.research.reprocess import collect_package_text

ACTIVE_STATUS = {"open", "amended", "active", "posted"}
CLOSED_STATUS = {"closed", "cancelled", "canceled", "awarded", "expired", "historical_award"}


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm_status(item: dict[str, Any]) -> str:
    return str(item.get("solicitation_status") or item.get("status") or "unknown").lower().strip()


def _is_productish(item: dict[str, Any]) -> bool:
    req = (item.get("requirement_class") or "unknown").lower()
    if req in INITIAL_EXCLUDE_CLASSES:
        return False
    if req in INITIAL_PRODUCT_OK:
        return True
    if req in PRODUCT_CLASSES and req not in INITIAL_EXCLUDE_CLASSES:
        return True
    return False


def _blocker_bucket(item: dict[str, Any], pipe: dict[str, Any], bid: dict[str, Any]) -> dict[str, Any]:
    """Classify what prevents bid-eligibility — provider gap vs M3 failure."""
    gates = bid.get("gates") or {}
    blockers: list[str] = []
    provider_gaps: list[str] = []
    m3_gaps: list[str] = []

    for name, g in gates.items():
        st = (g or {}).get("status")
        if st in {"fail", "unknown"}:
            blockers.append(f"{name}:{st}:{(g or {}).get('evidence')}")

    pkg_status = str(item.get("package_status") or "")
    text_len = int(item.get("document_text_len") or 0)
    has_qty = bool(pipe.get("quantity_defined") or item.get("quantity") not in (None, "", "unknown"))

    if pkg_status in {"ACCESS_RESTRICTED", "FAILED"} or text_len < 80:
        provider_gaps.append("missing_or_restricted_solicitation_documents")
    if not has_qty and text_len < 80:
        provider_gaps.append("quantity_not_in_accessible_provider_payload")
    elif not has_qty and text_len >= 80:
        m3_gaps.append("quantity_not_extracted_from_available_text")

    sub_ok = bool(item.get("submission_method") or item.get("submission_instructions"))
    if not sub_ok and text_len < 80:
        provider_gaps.append("submission_instructions_not_in_accessible_payload")
    elif not sub_ok and text_len >= 80:
        m3_gaps.append("submission_instructions_not_extracted")

    status = _norm_status(item)
    if status in CLOSED_STATUS:
        provider_gaps.append(f"solicitation_inactive:{status}")
    elif status not in ACTIVE_STATUS:
        if text_len < 80:
            provider_gaps.append("active_status_unverified_thin_payload")
        else:
            m3_gaps.append("active_status_not_confirmed_from_text")

    if not blockers and not provider_gaps and not m3_gaps and not bid.get("bid_eligible"):
        m3_gaps.append("unclassified_gate_failure")

    return {
        "nb_id": item.get("nb_id"),
        "title": (item.get("title") or "")[:160],
        "solicitation_number": item.get("solicitation_number"),
        "stage": pipe.get("stage"),
        "bid_eligible": bool(bid.get("bid_eligible")),
        "package_status": pkg_status or None,
        "document_text_len": text_len,
        "quantity": item.get("quantity"),
        "blockers": blockers[:12],
        "provider_data_gaps": provider_gaps,
        "m3_extraction_or_classification_gaps": m3_gaps,
        "primary_blocker": (provider_gaps or m3_gaps or blockers or ["none"])[0],
    }


def _attach_packages(db: Session, items: list[dict[str, Any]]) -> dict[str, Any]:
    pkgs = (
        db.query(SolicitationPackage)
        .options(joinedload(SolicitationPackage.documents))
        .filter(SolicitationPackage.source_key.in_(["nextbid", "nextbid_state_local"]))
        .all()
    )
    by_nb: dict[str, SolicitationPackage] = {}
    by_ek: dict[str, SolicitationPackage] = {}
    by_sn: dict[str, SolicitationPackage] = {}
    for p in pkgs:
        ek = str(p.external_key or "")
        if ek:
            by_ek[ek] = p
            if "nextbid:" in ek:
                by_nb[ek.split("nextbid:")[-1]] = p
            by_nb.setdefault(ek, p)
        sn = str(p.solicitation_number or "").strip()
        if sn:
            by_sn[sn] = p
        # nb_id sometimes stored in detail_fields
        detail = p.detail_fields or {}
        nb = str(detail.get("nb_id") or "")
        if nb:
            by_nb[nb] = p

    status_counts = Counter(p.package_status or "unknown" for p in pkgs)
    complete = sum(1 for p in pkgs if (p.package_status or "") in {"COMPLETE"})
    partial = sum(1 for p in pkgs if (p.package_status or "") in {"PARTIAL", "VERIFICATION_REQUIRED"})
    linked = 0
    usable = 0
    for it in items:
        nb = str(it.get("nb_id") or "")
        sn = str(it.get("solicitation_number") or "").strip()
        ek = str(it.get("external_key") or "")
        pkg = by_nb.get(nb) or by_ek.get(ek) or (by_sn.get(sn) if sn else None)
        if not pkg:
            continue
        linked += 1
        it["package_status"] = pkg.package_status
        it["package_external_key"] = pkg.external_key
        text = collect_package_text(pkg, max_chars=100_000)
        if text and is_junk_package_excerpt(text):
            text = ""
        if text:
            it["document_text"] = text
            it["document_text_len"] = len(text)
            usable += 1
        hint = pkg.classification_hint or {}
        if hint.get("quantity") and not it.get("quantity"):
            it["quantity"] = hint.get("quantity")
            it["quantity_semantic"] = hint.get("quantity_semantic") or "ORDERED_QUANTITY"
            it["quantity_evidence"] = hint.get("quantity_evidence")
        for k in ("submission_method", "submission_instructions"):
            if hint.get(k) and not it.get(k):
                it[k] = hint.get(k)

    return {
        "packages_total": len(pkgs),
        "complete_packages": complete,
        "partial_packages": partial,
        "package_status_counts": dict(status_counts),
        "items_linked_to_package": linked,
        "items_with_usable_package_text": usable,
        "downloaded_count_gt0": sum(1 for p in pkgs if (p.downloaded_count or 0) > 0),
    }


def audit_and_recover_nextbid(db: Session, *, use_openai: bool = False) -> dict[str, Any]:
    """Isolate NextBid inventory, screen, reprocess products, report exact blockers."""
    pop = collect_population()
    restore_meta = _restore_002i_fit_evidence(pop["by_id"])
    items = list(pop["by_id"].values())
    for it in items:
        it.setdefault("source_key", "nextbid_state_local")
        it.setdefault("access_cost", "paid_nextbid")
        if not it.get("external_key") and it.get("nb_id"):
            it["external_key"] = f"nextbid:{it['nb_id']}"

    pkg_meta = _attach_packages(db, items)

    # Pre-document screen
    pre = Counter()
    screened: list[dict[str, Any]] = []
    for it in items:
        disp = listing_prefilter(it)
        it["listing_disposition"] = disp
        status = _norm_status(it)
        if status in CLOSED_STATUS:
            it["screen_result"] = "exclude_inactive"
            pre["exclude_inactive"] += 1
            continue
        if disp == "exclude":
            it["screen_result"] = "exclude_incompatible"
            pre["exclude_incompatible"] += 1
            continue
        it["screen_result"] = "retain"
        pre["retain"] += 1
        screened.append(it)

    # Classify retained set
    batch = classify_batch(screened)
    classified = batch.get("items") if isinstance(batch, dict) else screened

    products: list[dict[str, Any]] = []
    stage_c: list[dict[str, Any]] = []
    bid_eligible: list[dict[str, Any]] = []
    missing_qty: list[dict[str, Any]] = []
    missing_submission: list[dict[str, Any]] = []
    active_count = 0
    stage_counts: Counter[str] = Counter()
    blocker_rows: list[dict[str, Any]] = []
    provider_gap_counts: Counter[str] = Counter()
    m3_gap_counts: Counter[str] = Counter()

    openai_spent = 0.0
    openai_calls = 0

    for it in classified or []:
        status = _norm_status(it)
        if status in ACTIVE_STATUS:
            active_count += 1
        elif status == "unknown" and it.get("listing_disposition") != "exclude":
            # Unknown active ≠ confirmed closed — count separately
            pass

        text = it.get("document_text") or it.get("description") or ""
        # Selective OpenAI reserved for owner-authorized extraction helpers only.
        # Deterministic pipeline runs first; this build keeps OpenAI at $0 unless
        # a future approved extractor is wired (use_openai flag retained).
        _ = use_openai

        pipe = evaluate_procurement_pipeline(it, document_text=text or None)
        for k, v in (pipe.get("enriched_fields") or {}).items():
            if v is not None:
                it[k] = v
        if pipe.get("quantity") is not None:
            it["quantity"] = pipe["quantity"]
        it["procurement_pipeline"] = pipe
        it["procurement_stage"] = pipe.get("stage")
        stage_counts[str(pipe.get("stage") or "none")] += 1

        product_ok = bool(pipe.get("product_opportunity")) or _is_productish(it)
        if not product_ok:
            continue
        # Confirmed product opportunity (pipeline) or productish retained
        if pipe.get("product_opportunity"):
            products.append(it)
        else:
            # Title/class productish but pipeline not yet product — still track for recovery
            if it.get("requirement_class") in INITIAL_PRODUCT_OK:
                products.append(it)
            else:
                continue

        if not pipe.get("quantity_defined"):
            missing_qty.append(it)
        if not (it.get("submission_method") or it.get("submission_instructions")):
            missing_submission.append(it)

        bid = evaluate_bid_eligibility(it, pipe, document_text=text or None)
        it["bid_eligibility"] = bid
        if pipe.get("stage") == STAGE_C and pipe.get("quantity_defined"):
            stage_c.append(it)
        if bid.get("bid_eligible"):
            bid_eligible.append(
                {
                    "nb_id": it.get("nb_id"),
                    "title": it.get("title"),
                    "solicitation_number": it.get("solicitation_number"),
                    "quantity": it.get("quantity"),
                }
            )
        else:
            row = _blocker_bucket(it, pipe, bid)
            blocker_rows.append(row)
            for g in row["provider_data_gaps"]:
                provider_gap_counts[g] += 1
            for g in row["m3_extraction_or_classification_gaps"]:
                m3_gap_counts[g] += 1

    # Persist refreshed inventory markers (no API)
    upsert_inventory(classified or [])
    inv = load_inventory()

    # Active: open/amended OR unknown-not-closed with retain screen
    currently_active = [
        it
        for it in (classified or [])
        if _norm_status(it) in ACTIVE_STATUS
        or (
            _norm_status(it) not in CLOSED_STATUS
            and it.get("screen_result") == "retain"
            and it.get("listing_disposition") != "exclude"
        )
    ]

    return {
        "audited_at": _utc(),
        "metered_nextbid_calls": 0,
        "openai_calls": openai_calls,
        "openai_spent_usd": round(openai_spent, 4),
        "population": {
            "unique_solicitations": pop["unique_nextbid"],
            "enriched_checkpoint": pop["enriched_count"],
            "listings_checkpoint": pop["listings_count"],
            "seen_nb_ids": pop["seen_count"],
            "inventory_rows": len(inv),
            "restore_002i_fit_evidence": restore_meta,
        },
        "packages": pkg_meta,
        "screening": dict(pre),
        "currently_active_estimate": len(currently_active),
        "active_status_open_amended": active_count,
        "tangible_product_opportunities": len(products),
        "complete_solicitation_packages": pkg_meta["complete_packages"],
        "partial_solicitation_packages": pkg_meta["partial_packages"],
        "missing_quantities_among_products": len(missing_qty),
        "missing_submission_among_products": len(missing_submission),
        "stage_counts": dict(stage_counts),
        "stage_c": len(stage_c),
        "bid_eligible": len(bid_eligible),
        "bid_eligible_samples": bid_eligible[:20],
        "provider_gap_counts": dict(provider_gap_counts),
        "m3_gap_counts": dict(m3_gap_counts),
        "product_blockers_sample": blocker_rows[:40],
        "primary_blocker_counts": dict(
            Counter(r["primary_blocker"] for r in blocker_rows).most_common(20)
        ),
    }
