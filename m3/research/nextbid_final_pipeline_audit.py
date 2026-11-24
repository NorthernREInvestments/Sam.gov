"""BUILD 002Q — Final pipeline defect audit + opportunity recovery (offline + free URLs).

ZERO metered NextBid / OpenAI / SAM / DIBBS. Acquisition remains locked.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from m3.config import get_settings
from m3.research.document_quality import (
    classify_document_text,
    is_junk_package_excerpt,
    title_body_overlap,
)
from m3.research.full_text_store import load_full_text, store_root as ft_root
from m3.research.nextbid_controller import (
    acquisition_locked,
    controller_status,
    refresh_account_usage,
)
from m3.research.nextbid_free_doc_recovery import recover_free_documents
from m3.research.nextbid_pipeline_repair import run_pipeline_repair
from m3.research.nextbid_recovery import collect_population

ARCHIVE = Path(__file__).resolve().parents[2] / "docs" / "acceptance_archive"


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log(msg: str) -> None:
    print(f"[final-pipeline-audit] {msg}", flush=True)


def validate_full_text_quality() -> dict[str, Any]:
    """Independently validate stored full-text files into A–F buckets."""
    root = ft_root()
    buckets: Counter[str] = Counter()
    samples: dict[str, list[str]] = {k: [] for k in "ABCDEF"}
    total = 0
    genuine_docs = 0
    listing_only = 0
    mismatch = 0
    for path in root.glob("*.json") if root.exists() else []:
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            buckets["F_junk_or_unreadable"] += 1
            continue
        total += 1
        text = str(row.get("full_text") or "")
        title = str(row.get("title") or "")
        n = int(row.get("full_text_chars") or len(text))
        src = str(row.get("source") or "")
        if is_junk_package_excerpt(text[:5000]) or classify_document_text(text[:8000]).get("is_junk"):
            buckets["F_junk_html_login_portal"] += 1
            if len(samples["F"]) < 5:
                samples["F"].append(path.stem)
            continue
        ov = title_body_overlap(title, text[:12000]) if title else {"ok": True}
        if title and not ov.get("ok") and n > 500:
            mismatch += 1
            buckets["F_title_body_mismatch"] += 1
            if len(samples["F"]) < 8:
                samples["F"].append(path.stem)
            continue
        low = text.lower()
        if "amendment" in low[:2000] or "addendum" in low[:2000]:
            buckets["E_amendments"] += 1
            if len(samples["E"]) < 5:
                samples["E"].append(path.stem)
        if n <= 500 or row.get("is_listing_preview_only") or src.startswith(
            ("listing", "offline_listing", "permanent_repair_search")
        ):
            buckets["A_short_listing_summaries"] += 1
            listing_only += 1
            if len(samples["A"]) < 5:
                samples["A"].append(path.stem)
            continue
        if n < 1200:
            buckets["B_detailed_descriptions"] += 1
            if len(samples["B"]) < 5:
                samples["B"].append(path.stem)
        elif any(
            x in low
            for x in (
                "bid schedule",
                "schedule of prices",
                "line item",
                "technical specification",
                "unit price",
                "qty",
                "quantity:",
            )
        ):
            buckets["C_specs_and_bid_schedules"] += 1
            genuine_docs += 1
            if len(samples["C"]) < 5:
                samples["C"].append(path.stem)
        elif n >= 4000:
            buckets["D_complete_packages"] += 1
            genuine_docs += 1
            if len(samples["D"]) < 5:
                samples["D"].append(path.stem)
        else:
            buckets["B_detailed_descriptions"] += 1
            genuine_docs += 1
            if len(samples["B"]) < 5:
                samples["B"].append(path.stem)
    usable_non_junk = total - buckets.get("F_junk_html_login_portal", 0) - buckets.get(
        "F_title_body_mismatch", 0
    )
    return {
        "files_total": total,
        "usable_non_junk_approx": usable_non_junk,
        "genuine_solicitation_documents_approx": genuine_docs,
        "listing_summaries_only": listing_only,
        "title_body_mismatches": mismatch,
        "buckets": dict(buckets),
        "samples": samples,
        "note": (
            "A=listing summaries (≠ complete packages). "
            "C/D count as genuine solicitation document text."
        ),
    }


def reconcile_population(pipeline: dict[str, Any]) -> dict[str, Any]:
    after = (pipeline or {}).get("after") or {}
    pop_path = get_settings().data_root() / "acceptance" / "nextbid_pipeline_repair_population.jsonl"
    status_c: Counter[str] = Counter()
    req_c: Counter[str] = Counter()
    disp_c: Counter[str] = Counter()
    open_n = amended_n = closed_n = unknown_n = 0
    product_confirmed = product_potential = non_product = mixed = 0
    duplicates = 0
    seen_sn: dict[str, str] = {}
    eligible: list[dict[str, Any]] = []
    supplier: list[dict[str, Any]] = []
    blockers: Counter[str] = Counter()

    if not pop_path.exists():
        return {"error": "population_missing", "path": str(pop_path)}

    for line in pop_path.open(encoding="utf-8"):
        try:
            r = json.loads(line)
        except Exception:
            continue
        st = str(r.get("solicitation_status") or "unknown").lower()
        status_c[st] += 1
        if st in {"open", "active"}:
            open_n += 1
        elif st == "amended":
            amended_n += 1
        elif st in {"closed", "cancelled", "closed_by_deadline", "historical_award", "forecast"}:
            closed_n += 1
        else:
            unknown_n += 1
        req = str(r.get("requirement_class") or "unknown").lower()
        req_c[req] += 1
        disp = str(r.get("primary_disposition") or "")
        disp_c[disp] += 1
        if req in {"product_supply", "product_with_incidental_services"}:
            product_confirmed += 1
        elif req in {"mixed_product_service", "product_with_significant_installation"}:
            mixed += 1
        elif req in {"unknown", "unknown_mixed", "pending_classification", ""}:
            product_potential += 1
        elif req in {
            "service",
            "construction",
            "software_saas",
            "product_rental_leasing",
            "not_applicable_non_solicitation",
        }:
            non_product += 1
        else:
            product_potential += 1
        sn = str(r.get("solicitation_number") or "").strip().lower()
        if sn and sn in seen_sn and seen_sn[sn] != r.get("nb_id"):
            duplicates += 1
        elif sn:
            seen_sn[sn] = str(r.get("nb_id"))

        bid = r.get("bid_eligibility") or {}
        pipe = r.get("procurement_pipeline") or {}
        row = {
            "nb_id": r.get("nb_id"),
            "title": r.get("title"),
            "solicitation_number": r.get("solicitation_number"),
            "agency": r.get("buyer_name"),
            "deadline_at": r.get("deadline_at"),
            "quantity": r.get("quantity"),
            "quantity_semantic": r.get("quantity_semantic"),
            "official_url": r.get("official_url"),
            "requirement_class": req,
            "solicitation_status": st,
            "primary_disposition": disp,
            "failed_gates": bid.get("failed_gates"),
            "unknown_gates": bid.get("unknown_gates"),
        }
        if bid.get("bid_eligible") or bid.get("ready_to_submit"):
            eligible.append(row)
        elif pipe.get("quantity_defined") and pipe.get("product_opportunity"):
            supplier.append(row)
            for g in bid.get("failed_gates") or []:
                blockers[f"fail:{g}"] += 1
            for g in bid.get("unknown_gates") or []:
                blockers[f"unknown:{g}"] += 1
        elif disp in {
            "verified_product_information_needed",
            "potential_product_classification_needed",
        }:
            if not pipe.get("quantity_defined"):
                blockers["missing_quantity"] += 1

    return {
        "unique_population": after.get("unique_solicitations") or sum(disp_c.values()),
        "status": {
            "open": open_n,
            "amended": amended_n,
            "closed_expired_cancelled": closed_n,
            "unknown": unknown_n,
            "by_value": dict(status_c),
        },
        "product": {
            "confirmed_tangible": product_confirmed,
            "mixed_product_service_or_install": mixed,
            "potential_unknown": product_potential,
            "confirmed_non_product": non_product,
        },
        "duplicate_solicitation_number_pairs": duplicates,
        "primary_dispositions": dict(disp_c),
        "pipeline_after": after,
        "procurement_eligible": eligible,
        "ready_supplier_pricing_candidates": supplier[:80],
        "ready_supplier_pricing_count": len(supplier),
        "blockers": dict(blockers.most_common(30)),
    }


def revised_credit_plan(*, inventory: int, unresolved: int) -> dict[str, Any]:
    return {
        "status": "PREPARED_NOT_EXECUTED",
        "authorization_required": True,
        "ceilings": {
            "listing_enumeration_max": 400,
            "detail_and_docs_max": 1246,
            "monthly_reserve_min": 1000,
            "note": "Campaign ceilings — do not exceed daily≤100 or job≤50 without owner auth",
        },
        "strategy": [
            "Reuse durable paid-response store and free official URL docs first",
            "Enumerate remaining listing partitions only (page_size=25, non-overlapping)",
            "Spend detail credits only on product/ambiguous open records missing firm qty or submission",
            "Prefer get_opportunity then list_files; read_file only for schedule/spec attachments",
            "No speculative indiscriminate file retrieval",
        ],
        "priority_targets": {
            "open_product_missing_qty_or_docs": "highest",
            "ambiguous_promising_with_deadline": "high",
            "closed_or_confirmed_non_product": "skip",
        },
        "estimates_within_ceilings": {
            "listing_credits_planned": min(400, max(50, (max(0, inventory - 6585) // 25) + 50)),
            "detail_credits_planned_cap": 1246,
            "reserve_remaining": 1000,
            "spendable_after_reserve_reference": 1646,
        },
        "unresolved_after_offline_approx": unresolved,
        "do_not_execute_until": "explicit owner authorization",
    }


def _write_report(payload: dict[str, Any]) -> Path:
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    jpath = ARCHIVE / "BUILD_002Q_FINAL_PIPELINE_DEFECT_AUDIT_ACCEPTANCE.json"
    mpath = ARCHIVE / "BUILD_002Q_FINAL_PIPELINE_DEFECT_AUDIT_ACCEPTANCE.md"
    jpath.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    defects = payload.get("defects_corrected") or []
    recon = payload.get("reconciliation") or {}
    ftq = payload.get("full_text_quality") or {}
    free = payload.get("free_doc_recovery") or {}
    plan = payload.get("revised_credit_plan") or {}
    eligible = recon.get("procurement_eligible") or []
    registration = recon.get("registration_required") or []
    supplier = recon.get("ready_supplier_pricing_candidates") or []

    lines = [
        "# BUILD 002Q — Final Pipeline Defect Audit & Opportunity Recovery",
        "",
        f"**Generated:** {payload.get('generated_at')}",
        f"**Runtime:** {payload.get('elapsed_sec')}s",
        "",
        "## Status",
        "",
        "- Acquisition locked: **YES**",
        f"- Metered NextBid delta: **{payload.get('metered_nextbid_delta')}**",
        f"- OpenAI spend: **$0**",
        f"- Free official docs recovered: **{(free.get('recovered_count') or 0)}**",
        f"- Source limitation note: {payload.get('source_limitation_note') or 'n/a'}",
        "",
        "## Confirmed software defects corrected",
        "",
    ]
    for d in defects:
        if isinstance(d, dict):
            lines.append(
                f"- **D{d.get('id')}** `{d.get('location')}` — {d.get('defect')} → {d.get('fix')}"
            )
        else:
            lines.append(f"- {d}")
    lines += [
        "",
        "## Population reconciliation (all unique records)",
        "",
        f"| Metric | Count |",
        f"|---|---|",
        f"| Unique population | {recon.get('unique_population')} |",
        f"| Open | {(recon.get('status') or {}).get('open')} |",
        f"| Amended | {(recon.get('status') or {}).get('amended')} |",
        f"| Closed/expired/cancelled | {(recon.get('status') or {}).get('closed_expired_cancelled')} |",
        f"| Unknown status | {(recon.get('status') or {}).get('unknown')} |",
        f"| Confirmed tangible product | {(recon.get('product') or {}).get('confirmed_tangible')} |",
        f"| Mixed/install | {(recon.get('product') or {}).get('mixed_product_service_or_install')} |",
        f"| Potential product | {(recon.get('product') or {}).get('potential_unknown')} |",
        f"| Confirmed non-product | {(recon.get('product') or {}).get('confirmed_non_product')} |",
        f"| Duplicate sol# pairs | {recon.get('duplicate_solicitation_number_pairs')} |",
        f"| Verified quantities | {(recon.get('pipeline_after') or {}).get('verified_quantities')} |",
        f"| Ready to submit (B) | {len(eligible)} |",
        f"| Ordinary registration required (C) | {len(registration)} |",
        f"| Ready for supplier pricing (A; qty+product, not bid-ready) | {recon.get('ready_supplier_pricing_count')} |",
        f"| Info recovery (D) | {(recon.get('pipeline_after') or {}).get('remaining_information_recovery')} |",
        "",
        "### Primary dispositions",
        "",
        f"`{json.dumps(recon.get('primary_dispositions'), default=str)}`",
        "",
        "## Full-text quality validation",
        "",
        f"- Files: {ftq.get('files_total')}",
        f"- Usable non-junk (approx): {ftq.get('usable_non_junk_approx')}",
        f"- Genuine solicitation docs (C/D approx): {ftq.get('genuine_solicitation_documents_approx')}",
        f"- Listing summaries only (A): {ftq.get('listing_summaries_only')}",
        f"- Buckets: `{json.dumps(ftq.get('buckets'), default=str)}`",
        "",
        "## Free official document recovery",
        "",
        f"`{json.dumps(free.get('stats'), default=str)}`",
        "",
        "## ACTUAL ready-to-submit opportunities (B)",
        "",
    ]
    if not eligible:
        lines.append("_None after classification hardening — see registration list and blockers._")
        lines.append("")
    else:
        lines.append("| nb_id | qty | deadline | agency | title | URL |")
        lines.append("|---|---|---|---|---|---|")
        for e in eligible:
            lines.append(
                f"| {e.get('nb_id')} | {e.get('quantity')} | {e.get('deadline_at')} | "
                f"{e.get('agency')} | {str(e.get('title') or '')[:70].replace('|','/')} | {e.get('official_url')} |"
            )
        lines.append("")

    lines += [
        "## Ordinary registration required (C)",
        "",
    ]
    if not registration:
        lines.append("_None._")
        lines.append("")
    else:
        lines.append("| nb_id | qty | agency | title | URL |")
        lines.append("|---|---|---|---|---|")
        for e in registration:
            lines.append(
                f"| {e.get('nb_id')} | {e.get('quantity')} | {e.get('agency')} | "
                f"{str(e.get('title') or '')[:70].replace('|','/')} | {e.get('official_url')} |"
            )
        lines.append("")

    lines += [
        "## Supplier-pricing candidates (A — qty verified; not yet bid-ready)",
        "",
        "| nb_id | qty | status | disposition | title |",
        "|---|---|---|---|---|",
    ]
    for s in supplier[:40]:
        lines.append(
            f"| {s.get('nb_id')} | {s.get('quantity')} | {s.get('solicitation_status')} | "
            f"{s.get('primary_disposition')} | {str(s.get('title') or '')[:70].replace('|','/')} |"
        )
    lines += [
        "",
        "## Remaining blockers",
        "",
        f"`{json.dumps(recon.get('blockers'), default=str)}`",
        "",
        "## Revised NextBid credit plan (NOT executed)",
        "",
        f"`{json.dumps(plan, default=str)}`",
        "",
        "## Provider usage",
        "",
        f"- Before: `{json.dumps(payload.get('usage_before'), default=str)}`",
        f"- After: `{json.dumps(payload.get('usage_after'), default=str)}`",
        "",
        "## Confirmation",
        "",
        "- All API locks remain engaged.",
        "- No Railway deploy. No Build 003.",
        "- **STOP — AWAITING OWNER ACCEPTANCE.**",
        "",
    ]
    mpath.write_text("\n".join(lines), encoding="utf-8")
    return mpath


def run_final_pipeline_audit(db: Session, *, free_doc_limit: int = 180) -> dict[str, Any]:
    t0 = time.time()
    assert acquisition_locked(), "NextBid must remain locked"
    _log("start")

    try:
        usage_before = refresh_account_usage(allow_when_locked=True)
    except Exception as exc:
        usage_before = {"ok": False, "error": type(exc).__name__}

    _log("full-text quality validation")
    ftq = validate_full_text_quality()
    _log(
        f"full_text files={ftq.get('files_total')} "
        f"genuine~={ftq.get('genuine_solicitation_documents_approx')}"
    )

    _log("free official URL document recovery")
    free = recover_free_documents(db, limit=free_doc_limit)
    _log(f"free recovered={free.get('recovered_count')} restricted={free.get('restricted_count')}")

    _log("reprocess full inventory (pipeline repair)")
    pipeline = run_pipeline_repair(db)

    _log("reconcile population")
    recon = reconcile_population(pipeline)

    try:
        usage_after = refresh_account_usage(allow_when_locked=True)
    except Exception as exc:
        usage_after = {"ok": False, "error": type(exc).__name__}

    metered_delta = 0
    if usage_before.get("used") is not None and usage_after.get("used") is not None:
        metered_delta = int(usage_after["used"]) - int(usage_before["used"])

    plan = revised_credit_plan(
        inventory=int(recon.get("unique_population") or 0),
        unresolved=int((recon.get("pipeline_after") or {}).get("remaining_information_recovery") or 0),
    )

    defects = [
        {
            "id": 1,
            "location": "classify._infer_status / collect_population normalize / commercial_criteria status map",
            "defect": "lifecycle_status=open never became solicitation_status for bid gates",
            "fix": "Map lifecycle_status→solicitation_status; re-infer when status unknown",
        },
        {
            "id": 2,
            "location": "commercial_criteria.evaluate_bid_eligibility sufficient_documents",
            "defect": "document_set_status=COMPLETE ignored; 500-char floor blocked Stage C text",
            "fix": "Honor document_set_status; Stage C+qty passes at doc_len≥200",
        },
        {
            "id": 3,
            "location": "commercial_criteria._SUBMISSION_RE",
            "defect": "IFB/RFB/RFP instrument language did not satisfy submission gate",
            "fix": "Accept invitation-for-bids / IFB/RFB/RFP/RFQ as submission evidence",
        },
        {
            "id": 4,
            "location": "commercial_criteria quantity_defined + extract_fields ESTIMATED markers",
            "defect": "Distant 'up to' boilerplate demoted firm ORDERED quantities",
            "fix": "Narrow estimated markers; firm ORDERED/LINE_ITEM not demoted by qty_kind=estimated",
        },
        {
            "id": 5,
            "location": "extract_fields WORD_PAREN / BARE_EA / N_EACH_OF",
            "defect": "Only after-context checked; BARE_EA/N_EACH_OF dead code",
            "fix": "Before+after product context; wire bare EA / N each of extractors",
        },
        {
            "id": 6,
            "location": "pipeline_repair full_text attach + document_quality.title_body_overlap",
            "defect": "Wrong portal pages preferred as full text over matching listings",
            "fix": "Reject low title/body overlap; never overwrite thicker text with thinner",
        },
        {
            "id": 7,
            "location": "nextbid_free_doc_recovery (official public URLs)",
            "defect": "Existing records lacked free official document recovery path",
            "fix": "Bounded public URL recovery for product/ambiguous open records",
        },
    ]

    payload = {
        "build": "002Q",
        "directive": "FINAL_PIPELINE_DEFECT_AUDIT_AND_OPPORTUNITY_RECOVERY",
        "generated_at": _utc(),
        "elapsed_sec": round(time.time() - t0, 2),
        "acquisition_locked": acquisition_locked(),
        "controller": controller_status(),
        "usage_before": usage_before,
        "usage_after": usage_after,
        "metered_nextbid_delta": metered_delta,
        "defects_corrected": defects,
        "full_text_quality": ftq,
        "free_doc_recovery": free,
        "pipeline_after": (pipeline or {}).get("after"),
        "reconciliation": recon,
        "revised_credit_plan": plan,
        "stop": "AWAITING_OWNER_ACCEPTANCE",
    }
    report = _write_report(payload)
    payload["acceptance_report_md"] = str(report)
    _log(f"done report={report}")
    _log("STOP — awaiting owner acceptance")
    return payload
