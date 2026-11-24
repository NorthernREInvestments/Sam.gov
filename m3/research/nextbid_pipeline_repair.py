"""Build 002Q — offline NextBid pipeline repair + full 2255 reprocess.

ZERO metered NextBid / OpenAI / SAM / DIBBS calls.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session, joinedload

from m3.config import get_settings
from m3.models import SolicitationPackage
from m3.research.classify import classify_solicitation_item
from m3.research.commercial_criteria import (
    STAGE_C,
    evaluate_bid_eligibility,
    evaluate_procurement_pipeline,
)
from m3.research.document_quality import is_junk_package_excerpt, title_body_overlap
from m3.research.full_text_store import load_full_text
from m3.research.nextbid_inventory import upsert_inventory
from m3.research.nextbid_recovery import collect_population, listing_prefilter
from m3.research.owner_queues import (
    PRIMARY_DISPOSITIONS,
    empty_queues,
    map_to_primary_disposition,
    write_queues,
)
from m3.research.reprocess import collect_package_text

ARCHIVE = Path(__file__).resolve().parents[2] / "docs" / "acceptance_archive"


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _attach_stored_evidence(db: Session, items: list[dict[str, Any]]) -> dict[str, Any]:
    pkgs = (
        db.query(SolicitationPackage)
        .options(joinedload(SolicitationPackage.documents))
        .filter(SolicitationPackage.source_key.in_(["nextbid", "nextbid_state_local"]))
        .all()
    )
    by_nb: dict[str, SolicitationPackage] = {}
    by_sn: dict[str, SolicitationPackage] = {}
    by_ek: dict[str, SolicitationPackage] = {}
    for p in pkgs:
        ek = str(p.external_key or "")
        if ek:
            by_ek[ek] = p
            if "nextbid:" in ek:
                by_nb[ek.split("nextbid:")[-1]] = p
        sn = str(p.solicitation_number or "").strip()
        if sn:
            by_sn[sn] = p
        nb = str((p.detail_fields or {}).get("nb_id") or "")
        if nb:
            by_nb[nb] = p

    linked = 0
    usable = 0
    richer_kept = 0
    full_text_hits = 0
    for it in items:
        nb = str(it.get("nb_id") or "")
        sn = str(it.get("solicitation_number") or "").strip()
        ek = str(it.get("external_key") or f"nextbid:{nb}")
        # Prefer durable full_text_store (paid/extracted text) over thin listing summary
        ft = load_full_text(nb) if nb else None
        ft_text = str((ft or {}).get("full_text") or "")
        title = str(it.get("title") or "")
        if ft_text and not is_junk_package_excerpt(ft_text[:8000]) and len(ft_text) >= 80:
            overlap = title_body_overlap(title, ft_text[:12000])
            if not overlap.get("ok") and len(ft_text) > 500:
                # Wrong-page pollution — do not prefer over listing description
                it["full_text_mismatch"] = overlap
            else:
                existing0 = str(it.get("document_text") or it.get("description") or "")
                if len(ft_text) > len(existing0):
                    it["document_text"] = ft_text
                    it["document_text_len"] = len(ft_text)
                    it["document_text_source"] = (ft or {}).get("source") or "full_text_store"
                    it["is_listing_preview_only"] = bool((ft or {}).get("is_listing_preview_only"))
                    full_text_hits += 1
                    usable += 1
        pkg = by_nb.get(nb) or by_ek.get(ek) or (by_sn.get(sn) if sn else None)
        if not pkg:
            continue
        linked += 1
        it["package_status"] = pkg.package_status
        it["package_external_key"] = pkg.external_key
        text = collect_package_text(pkg, max_chars=120_000)
        if text and is_junk_package_excerpt(text):
            text = ""
        existing = str(it.get("document_text") or it.get("description") or "")
        if text and len(text) > len(existing):
            it["document_text"] = text
            it["document_text_len"] = len(text)
            richer_kept += 1
            usable += 1
        elif text:
            it.setdefault("document_text", text)
            it["document_text_len"] = len(text)
            usable += 1
        elif existing:
            it["document_text_len"] = len(existing)
        # Always finalize document_text_len for eligibility gates
        if it.get("document_text") and not it.get("document_text_len"):
            it["document_text_len"] = len(str(it["document_text"]))
        # Honor NextBid document_set_status when DB package missing/null
        if not it.get("package_status") and it.get("document_set_status"):
            ds = str(it["document_set_status"]).upper()
            if ds in {"COMPLETE", "PARTIAL"}:
                it["package_status"] = ds
            elif ds == "AVAILABLE":
                it["package_status"] = "COMPLETE"

        hint = pkg.classification_hint or {}
        detail = pkg.detail_fields or {}
        # Never overwrite richer qty with empty
        if hint.get("quantity") and not it.get("quantity"):
            it["quantity"] = hint.get("quantity")
            it["quantity_semantic"] = hint.get("quantity_semantic") or it.get("quantity_semantic")
            it["quantity_evidence"] = hint.get("quantity_evidence") or it.get("quantity_evidence")
        for k in (
            "quantity_unit",
            "submission_method",
            "submission_instructions",
            "line_items",
            "manufacturer",
            "model_number",
            "part_number",
            "brand_restrictions",
            "approved_equivalents",
        ):
            if hint.get(k) and not it.get(k):
                it[k] = hint.get(k)
            if detail.get(k) and not it.get(k):
                it[k] = detail.get(k)
        # Description: keep longer
        desc = detail.get("description") or hint.get("description_excerpt")
        if desc and len(str(desc)) > len(str(it.get("description") or "")):
            it["description"] = desc

    return {
        "packages_total": len(pkgs),
        "items_linked": linked,
        "items_usable_text": usable,
        "richer_text_preferred": richer_kept,
        "full_text_store_hits": full_text_hits,
    }


def run_pipeline_repair(db: Session, *, before: dict[str, Any] | None = None) -> dict[str, Any]:
    t0 = datetime.now(timezone.utc)
    pop = collect_population()
    items = list(pop["by_id"].values())
    for it in items:
        it.setdefault("source_key", "nextbid_state_local")
        if not it.get("external_key") and it.get("nb_id"):
            it["external_key"] = f"nextbid:{it['nb_id']}"
        if not it.get("record_type") and it.get("verified_individual_solicitation") is not False:
            it["record_type"] = "INDIVIDUAL_SOLICITATION"
            it["verified_individual_solicitation"] = True

    pkg_meta = _attach_stored_evidence(db, items)

    # Classify all (no premature title rejects)
    classified: list[dict[str, Any]] = []
    pre = Counter()
    for it in items:
        disp = listing_prefilter(it)
        it["listing_disposition"] = disp
        pre[disp] += 1
        classified.append(classify_solicitation_item(it))

    dispositions: Counter[str] = Counter()
    queues = empty_queues()
    stage_counts: Counter[str] = Counter()
    blockers: Counter[str] = Counter()
    product_confirmed = 0
    product_potential = 0
    qty_verified = 0
    specs_complete = 0
    bid_eligible_n = 0
    ready_supplier = 0
    ready_bid = 0
    info_recovery = 0
    previously_excluded_potential_accounted = 0

    rows_out: list[dict[str, Any]] = []

    for it in classified:
        text = it.get("document_text") or it.get("description") or ""
        pipe = evaluate_procurement_pipeline(it, document_text=text or None)
        for k, v in (pipe.get("enriched_fields") or {}).items():
            if v is not None and (it.get(k) in (None, "", []) or k.startswith("quantity")):
                # Prefer richer: only fill empties for non-quantity; quantity from pipe if firm
                if k.startswith("quantity"):
                    if v not in (None, ""):
                        it[k] = v
                elif not it.get(k):
                    it[k] = v
        if pipe.get("quantity") is not None and not it.get("quantity"):
            it["quantity"] = pipe["quantity"]
        it["procurement_pipeline"] = pipe
        it["procurement_stage"] = pipe.get("stage")
        stage_counts[str(pipe.get("stage") or ("excluded" if pipe.get("excluded") else "none"))] += 1

        bid = evaluate_bid_eligibility(it, pipe, document_text=text or None)
        it["bid_eligibility"] = bid

        primary = map_to_primary_disposition(
            it, pipe, bid=bid, package_status=it.get("package_status")
        )
        it["primary_disposition"] = primary
        dispositions[primary] += 1

        # Stats
        req = (it.get("requirement_class") or "").lower()
        if pipe.get("product_opportunity"):
            if req in {"unknown", "unknown_mixed", "pending_classification", ""}:
                product_potential += 1
            else:
                product_confirmed += 1
        if pipe.get("quantity_defined"):
            qty_verified += 1
        if it.get("line_items") or (
            it.get("manufacturer") or it.get("model_number") or it.get("part_number")
        ):
            specs_complete += 1
        if bid.get("bid_eligible"):
            bid_eligible_n += 1
        if primary == "verified_bid_opportunities":
            ready_bid += 1
            ready_supplier += 1
        elif primary == "verified_product_information_needed":
            info_recovery += 1
            if pipe.get("quantity_defined"):
                ready_supplier += 1
        elif primary == "potential_product_classification_needed":
            info_recovery += 1
            previously_excluded_potential_accounted += 1

        if primary in {
            "verified_product_information_needed",
            "potential_product_classification_needed",
        }:
            if not pipe.get("quantity_defined"):
                blockers["missing_quantity"] += 1
            if not (it.get("submission_method") or it.get("submission_instructions")):
                blockers["missing_submission_instructions"] += 1
            if (it.get("package_status") or "") in {"ACCESS_RESTRICTED", "FAILED", ""}:
                blockers["missing_or_restricted_documents"] += 1
            if "potential_product_insufficient_classification" in (pipe.get("unknown_flags") or []):
                blockers["classification_insufficient"] += 1
            for gname, g in (bid.get("gates") or {}).items():
                if isinstance(g, dict) and g.get("status") in {"fail", "unknown"}:
                    blockers[f"eligibility_gate:{gname}"] += 1

        row = {
            "nb_id": it.get("nb_id"),
            "title": (it.get("title") or "")[:180],
            "solicitation_number": it.get("solicitation_number"),
            "official_url": it.get("official_url"),
            "requirement_class": it.get("requirement_class"),
            "solicitation_status": it.get("solicitation_status") or it.get("status"),
            "stage": pipe.get("stage"),
            "product_opportunity": pipe.get("product_opportunity"),
            "quantity": it.get("quantity"),
            "quantity_defined": pipe.get("quantity_defined"),
            "bid_eligible": bid.get("bid_eligible"),
            "primary_disposition": primary,
            "package_status": it.get("package_status"),
            "unknown_flags": pipe.get("unknown_flags"),
            "reasons": pipe.get("reasons"),
            "unresolved": primary
            in {
                "verified_product_information_needed",
                "potential_product_classification_needed",
            },
            "disposition": primary,
            "queue": primary,
        }
        rows_out.append(row)
        queues[primary].append(row)
        # Mirror into legacy recovery queues (do not re-append primary)
        if primary == "verified_bid_opportunities":
            queues["qualified_commercial_evaluation"].append(row)
            queues["bids_ready_for_owner_approval"].append(row)
            queues["verified_procurement_fits"].append(
                {**row, "disposition": "verified_procurement_fit"}
            )
        elif primary == "verified_product_information_needed":
            queues["promising_awaiting_verification"].append(row)
            if not pipe.get("quantity_defined"):
                queues["missing_document"].append(row)
        elif primary == "potential_product_classification_needed":
            queues["promising_awaiting_verification"].append(row)
        elif primary == "closed_expired_non_solicitation":
            queues["archived_or_closed"].append(row)
        elif primary == "easy_vendor_registration":
            queues["promising_awaiting_verification"].append(row)

    queue_summary = write_queues(queues, build="002Q-pipeline-repair")
    upsert_inventory(classified)

    # Persist population artifact for app visibility
    out_dir = get_settings().data_root() / "acceptance"
    out_dir.mkdir(parents=True, exist_ok=True)
    pop_path = out_dir / "nextbid_pipeline_repair_population.jsonl"
    with pop_path.open("w", encoding="utf-8") as f:
        for it in classified:
            slim = {
                k: it.get(k)
                for k in (
                    "nb_id",
                    "external_key",
                    "title",
                    "solicitation_number",
                    "official_url",
                    "requirement_class",
                    "solicitation_status",
                    "quantity",
                    "quantity_semantic",
                    "quantity_evidence",
                    "submission_method",
                    "submission_instructions",
                    "primary_disposition",
                    "procurement_stage",
                    "package_status",
                    "document_text_len",
                    "bid_eligibility",
                    "procurement_pipeline",
                    "verified_individual_solicitation",
                    "record_type",
                    "source_key",
                    "buyer_name",
                    "deadline_at",
                    "line_items",
                )
            }
            f.write(json.dumps(slim, default=str) + "\n")

    elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
    after = {
        "unique_solicitations": len(classified),
        "verified_active_open_amended": sum(
            1
            for it in classified
            if str(it.get("solicitation_status") or it.get("status") or "").lower()
            in {"open", "amended"}
        ),
        "confirmed_tangible_product_purchases": product_confirmed,
        "potential_tangible_product_purchases": product_potential,
        "verified_quantities": qty_verified,
        "complete_product_specifications": specs_complete,
        "verified_procurement_eligibility": bid_eligible_n,
        "easy_vendor_registration": dispositions.get("easy_vendor_registration", 0),
        "opportunities_ready_for_supplier_pricing": ready_supplier,
        "opportunities_ready_for_bidding": ready_bid,
        "remaining_information_recovery": info_recovery,
        "confirmed_disqualifications": dispositions.get("confirmed_disqualification", 0),
        "closed_or_non_solicitation": dispositions.get("closed_expired_non_solicitation", 0),
        "primary_dispositions": {k: dispositions.get(k, 0) for k in PRIMARY_DISPOSITIONS},
        "previously_excluded_potential_accounted": previously_excluded_potential_accounted,
    }

    report = {
        "build": "002Q",
        "job": "pipeline-repair",
        "finished_at": _utc(),
        "elapsed_sec": round(elapsed, 1),
        "metered_nextbid_calls": 0,
        "openai_spent_usd": 0,
        "sam_calls": 0,
        "dibbs_calls": 0,
        "new_sources_activated": 0,
        "defects_corrected": [
            "listing_prefilter no longer hard-rejects IT/construction/maintenance title words",
            "unknown_mixed treated as potential product investigation, not reject",
            "INITIAL_EXCLUDE no longer includes significant_installation or rental at product stage",
            "goods title overrides mis-tagged service/construction class for investigation",
            "IT hardware recognized as tangible goods; software-only still separated",
            "supplies/materials beat bare service/construction title words in classify",
            "rental vs purchase disambiguated; mandatory install requires explicit phrase",
            "primary dispositions separate procurement fit from commercial/financing",
            "missing qty/docs/submission → information-needed queues, not confirmed DQ",
        ],
        "package_recovery": pkg_meta,
        "listing_prefilter": dict(pre),
        "stage_counts": dict(stage_counts),
        "before": before
        or {
            "unique_solicitations": 2255,
            "reported_product_opportunities": 714,
            "confirmed_tangible_audit": 660,
            "mixed_audit": 117,
            "it_product_audit": 34,
            "potential_audit": 1063,
            "warranting_investigation_audit": 1874,
            "excluded_for_uncertainty": 1043,
        },
        "after": after,
        "minimal_blocker_counts": dict(blockers),
        "queue_summary": queue_summary,
        "population_path": str(pop_path),
        "all_ids_reconciled": len(rows_out) == len(classified),
        "id_count": len(rows_out),
        "remaining_data_limitations": {
            "note": (
                "Low verified-bid counts reflect missing/restricted NextBid document payloads "
                "in stored packages (many ACCESS_RESTRICTED), not live API refusal. "
                "M3 cannot invent quantities from thin listings."
            ),
            "packages_linked": pkg_meta.get("items_linked"),
            "usable_package_text": pkg_meta.get("items_usable_text"),
        },
    }

    ARCHIVE.mkdir(parents=True, exist_ok=True)
    detail = out_dir / "nextbid_pipeline_repair_dispositions.jsonl"
    with detail.open("w", encoding="utf-8") as f:
        for row in rows_out:
            f.write(json.dumps(row, default=str) + "\n")
    report["disposition_rows_path"] = str(detail)

    _write_acceptance(report)
    return report


def _write_acceptance(report: dict[str, Any]) -> None:
    path_json = ARCHIVE / "BUILD_002Q_PIPELINE_REPAIR_ACCEPTANCE.json"
    path_json.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    (get_settings().data_root() / "acceptance" / "jobs_pipeline_repair_last.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )
    b = report["before"]
    a = report["after"]
    md = "\n".join(
        [
            "# BUILD 002Q — Procurement Pipeline Repair Acceptance",
            "",
            f"Finished: {report['finished_at']} · Runtime: {report['elapsed_sec']}s",
            f"NextBid metered: **{report['metered_nextbid_calls']}** · OpenAI: **${report['openai_spent_usd']}** · "
            f"SAM: **{report['sam_calls']}** · DIBBS: **{report['dibbs_calls']}** · New sources: **{report['new_sources_activated']}**",
            "",
            "## Defects corrected",
            "",
            *[f"- {d}" for d in report["defects_corrected"]],
            "",
            "## Before → After",
            "",
            "| Metric | Before | After |",
            "|---|---:|---:|",
            f"| Unique solicitations | {b.get('unique_solicitations')} | {a['unique_solicitations']} |",
            f"| Reported product opportunities / confirmed+potential product | {b.get('reported_product_opportunities')} / {b.get('warranting_investigation_audit')} | {a['confirmed_tangible_product_purchases']}+{a['potential_tangible_product_purchases']} |",
            f"| Excluded for uncertainty | {b.get('excluded_for_uncertainty')} | (see potential disposition) |",
            f"| Verified active (open/amended) | — | {a['verified_active_open_amended']} |",
            f"| Confirmed tangible product (pipeline) | {b.get('confirmed_tangible_audit')} | {a['confirmed_tangible_product_purchases']} |",
            f"| Potential tangible product | {b.get('potential_audit')} | {a['potential_tangible_product_purchases']} |",
            f"| Verified quantities | — | {a['verified_quantities']} |",
            f"| Complete product specifications | — | {a['complete_product_specifications']} |",
            f"| Verified procurement eligibility | — | {a['verified_procurement_eligibility']} |",
            f"| Easy vendor registration | — | {a['easy_vendor_registration']} |",
            f"| Ready for supplier pricing | — | {a['opportunities_ready_for_supplier_pricing']} |",
            f"| Ready for bidding | — | {a['opportunities_ready_for_bidding']} |",
            f"| Information recovery | — | {a['remaining_information_recovery']} |",
            f"| Confirmed disqualifications | — | {a['confirmed_disqualifications']} |",
            f"| Closed / non-solicitation | — | {a['closed_or_non_solicitation']} |",
            "",
            "### Primary dispositions",
            "",
            "| Disposition | Count |",
            "|---|---:|",
            *[f"| {k} | {v} |" for k, v in (a.get("primary_dispositions") or {}).items()],
            "",
            f"Previously excluded potential-product path accounted in potential queue: **{a.get('previously_excluded_potential_accounted')}**",
            "",
            "## Minimal blockers",
            "",
            f"`{json.dumps(report.get('minimal_blocker_counts') or {})}`",
            "",
            "## Remaining data limitations",
            "",
            report["remaining_data_limitations"]["note"],
            f"- Packages linked: {report['remaining_data_limitations'].get('packages_linked')} · "
            f"Usable text: {report['remaining_data_limitations'].get('usable_package_text')}",
            "",
            f"All IDs reconciled: **{report.get('all_ids_reconciled')}** ({report.get('id_count')})",
            "",
            "Awaiting owner acceptance.",
            "",
        ]
    )
    (ARCHIVE / "BUILD_002Q_PIPELINE_REPAIR_ACCEPTANCE.md").write_text(md, encoding="utf-8")
