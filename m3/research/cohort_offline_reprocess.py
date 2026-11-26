"""Offline reprocess of the original fresh 82-cohort — zero paid APIs / zero OpenAI.

Rebuilds document inventories from stored list_documents, revalidates cached AI
against corrected readiness gates, writes durable reconciliation reports.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from m3.config import get_settings
from m3.research.ai_output_verify import verify_analysis_against_sources
from m3.research.document_inventory import reprocess_stored_manifests
from m3.research.local_evidence import assemble_local_evidence_package
from m3.research.owner_queues import empty_queues, write_queues


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cohort_path() -> Path:
    # Prefer TEMP forensic cohort; fall back to inventory+archives
    p = Path(__file__).resolve().parents[2] / "TEMP" / "fresh_inventory_reset" / "fresh_cohort_full.json"
    return p


def load_original_82() -> list[dict[str, Any]]:
    path = _cohort_path()
    if path.exists():
        rows = json.loads(path.read_text(encoding="utf-8"))
        assert len(rows) == 82, len(rows)
        return rows
    raise FileNotFoundError(f"fresh cohort file missing: {path}")


def _disposition(nb_id: str, active: set[str], rejected: set[str], archived: set[str]) -> str:
    if nb_id in active:
        return "active"
    if nb_id in rejected:
        return "rejected_removed"
    if nb_id in archived:
        return "archived_after_close"
    return "UNACCOUNTED"


def run_offline_reprocess(*, write_queues_flag: bool = True) -> dict[str, Any]:
    root = get_settings().data_root()
    cohort = load_original_82()

    active = set()
    inv_path = root / "nextbid_inventory" / "opportunities.jsonl"
    inv_rows: dict[str, dict] = {}
    if inv_path.exists():
        for line in inv_path.open(encoding="utf-8"):
            if line.strip():
                row = json.loads(line)
                active.add(row["nb_id"])
                inv_rows[row["nb_id"]] = row

    rejected: set[str] = set()
    rp = root / "nextbid_inventory" / "rejected_removed.jsonl"
    if rp.exists():
        for line in rp.open(encoding="utf-8"):
            if line.strip():
                rejected.add(json.loads(line)["nb_id"])

    archived: set[str] = set()
    ap = root / "nextbid_inventory" / "archived_after_close.jsonl"
    if ap.exists():
        for line in ap.open(encoding="utf-8"):
            if line.strip():
                archived.add(json.loads(line)["nb_id"])

    per: list[dict[str, Any]] = []
    counts = Counter()
    queues = empty_queues()

    for c in cohort:
        nb = c["nb_id"]
        disp = _disposition(nb, active, rejected, archived)
        counts[f"disposition:{disp}"] += 1

        man = reprocess_stored_manifests(nb)
        package = assemble_local_evidence_package(
            nb, db=None, title=c.get("title"), include_http_cache=False, include_package_db=False
        )
        cov = package.get("coverage") or {}
        pkg_comp = package.get("package_completeness") or {}

        ai_path = root / "ai_doc_understanding" / "by_nb_id" / f"{nb}.json"
        ai = None
        revalidated = None
        if ai_path.exists():
            ai = json.loads(ai_path.read_text(encoding="utf-8"))
            parsed = ai.get("parsed") or {}
            # Re-verify against corrected gates using current local evidence
            revalidated = verify_analysis_against_sources(parsed, package, item=dict(inv_rows.get(nb) or c))
            # Persist revalidation sidecar (do not overwrite original AI parse)
            side = root / "ai_doc_understanding" / "revalidated" / f"{nb}.json"
            side.parent.mkdir(parents=True, exist_ok=True)
            side.write_text(
                json.dumps(
                    {
                        "nb_id": nb,
                        "revalidated_at": _utc(),
                        "original_promotion": (ai.get("verification") or {}).get("promotion"),
                        "corrected_promotion": revalidated.get("promotion"),
                        "unknown_fields": revalidated.get("unknown_fields"),
                        "verified_line_items": revalidated.get("verified_line_items"),
                        "package_completeness": pkg_comp,
                    },
                    indent=2,
                    default=str,
                ),
                encoding="utf-8",
            )

        promo = (revalidated or {}).get("promotion") or {}
        quote = bool(promo.get("supplier_quote_ready"))
        partial = bool(promo.get("partial_supplier_quote"))
        bid_ready = bool(promo.get("bid_submission_ready"))

        if disp == "rejected_removed":
            counts["rejected"] += 1
            status = "rejected"
        elif disp == "archived_after_close":
            counts["archived"] += 1
            status = "archived_expired"
        elif quote:
            counts["supplier_pricing_ready"] += 1
            status = "supplier_pricing_ready"
            queues["awaiting_supplier_quotations"].append(
                {
                    "nb_id": nb,
                    "title": c.get("title") or package.get("title"),
                    "owner_queue": "SUPPLIER_PRICING",
                    "promotion": promo,
                    "package_completeness": pkg_comp.get("package_completeness"),
                    "fresh_cohort": True,
                    "offline_revalidated": True,
                }
            )
        elif partial:
            counts["partial_supplier_pricing"] += 1
            status = "partial_supplier_pricing"
            queues["verified_product_information_needed"].append(
                {
                    "nb_id": nb,
                    "title": c.get("title") or package.get("title"),
                    "owner_queue": "NEEDS_DOCUMENTATION",
                    "reason": "partial_line_items_or_missing_qty",
                    "blockers": promo.get("readiness_blockers"),
                    "fresh_cohort": True,
                }
            )
        elif int((cov.get("document_inventory") or {}).get("download_pending") or 0) > 0 or (
            cov.get("opportunity_document_count") and int(cov.get("opportunity_document_count") or 0) > 0
            and int((cov.get("document_inventory") or {}).get("downloaded") or 0) == 0
        ):
            counts["awaiting_document_retrieval"] += 1
            status = "awaiting_document_retrieval"
            queues["missing_document"].append(
                {
                    "nb_id": nb,
                    "title": c.get("title"),
                    "opportunity_document_count": cov.get("opportunity_document_count"),
                    "inventoried": (cov.get("document_inventory") or {}).get("inventoried"),
                    "download_pending": (cov.get("document_inventory") or {}).get("download_pending"),
                    "next_action": "authorized_list_documents_or_get_document_download",
                }
            )
        elif not ai:
            if cov.get("has_substantive_document_content"):
                counts["awaiting_ai_analysis"] += 1
                status = "awaiting_ai_analysis"
            else:
                counts["incomplete_listing_or_body"] += 1
                status = "incomplete_listing_or_body"
        else:
            counts["incomplete_or_blocked"] += 1
            status = "incomplete_or_blocked"
            queues["verified_product_information_needed"].append(
                {
                    "nb_id": nb,
                    "title": c.get("title"),
                    "blockers": promo.get("readiness_blockers") or ["incomplete_requirements"],
                    "fresh_cohort": True,
                }
            )

        if bid_ready:
            counts["bid_ready"] += 1

        counts[f"package:{pkg_comp.get('package_completeness')}"] += 1

        per.append(
            {
                "nb_id": nb,
                "title": c.get("title"),
                "source_portal": c.get("source"),
                "official_url": c.get("official_url"),
                "solicitation_number": c.get("solicitation_number") or (inv_rows.get(nb) or {}).get("solicitation_number"),
                "disposition": disp,
                "status": status,
                "window_endpoints": c.get("endpoints"),
                "manifest_reprocess": man,
                "opportunity_document_count": cov.get("opportunity_document_count"),
                "package_completeness": pkg_comp.get("package_completeness"),
                "package_reasons": pkg_comp.get("reasons"),
                "document_inventory": cov.get("document_inventory"),
                "has_substantive_body": cov.get("has_substantive_document_content"),
                "assembled_chars": package.get("assembled_chars"),
                "ai_present": bool(ai),
                "original_ai_quote_ready": ((ai or {}).get("verification") or {}).get("promotion", {}).get(
                    "supplier_quote_ready"
                )
                if ai
                else None,
                "corrected_quote_ready": quote,
                "corrected_partial_quote": partial,
                "corrected_bid_ready": bid_ready,
                "readiness_blockers": promo.get("readiness_blockers"),
                "unknown_fields": (revalidated or {}).get("unknown_fields"),
                "next_authorized_action": (
                    "none_rejected_or_archived"
                    if disp != "active"
                    else (
                        "reuse_local_only"
                        if quote
                        else (
                            "authorized_document_retrieval"
                            if status == "awaiting_document_retrieval"
                            else "authorized_openai_or_local_research"
                        )
                    )
                ),
            }
        )

    if write_queues_flag:
        qsummary = write_queues(queues, build="corrective-offline-reprocess-82")
    else:
        qsummary = {k: len(v) for k, v in queues.items()}

    report = {
        "ok": True,
        "finished_at": _utc(),
        "paid_api_used": False,
        "openai_used": False,
        "cohort_size": 82,
        "unaccounted": sum(1 for p in per if p["disposition"] == "UNACCOUNTED"),
        "counts": dict(counts),
        "queue_summary": qsummary,
        "supplier_pricing_ready_nbs": [p["nb_id"] for p in per if p["corrected_quote_ready"]],
        "partial_quote_nbs": [p["nb_id"] for p in per if p["corrected_partial_quote"]],
        "bid_ready_nbs": [p["nb_id"] for p in per if p["corrected_bid_ready"]],
        "awaiting_document_retrieval_nbs": [
            p["nb_id"] for p in per if p["status"] == "awaiting_document_retrieval"
        ],
        "complete_relevant_packages": sum(
            1
            for p in per
            if p.get("package_completeness") in {"complete_relevant_package", "complete_package_reviewed"}
        ),
        "note": "complete_relevant requires downloaded inventoried docs; text length alone never counts",
    }

    out_dir = root / "acceptance" / "corrective_offline_reprocess_82"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "PER_SOLICITATION.json").write_text(json.dumps(per, indent=2, default=str), encoding="utf-8")
    (out_dir / "SUMMARY.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    # human table
    lines = [
        "nb_id|disposition|status|pkg_completeness|doc_count|inv|pending|ai|old_quote|new_quote|partial|blockers"
    ]
    for p in per:
        lines.append(
            "|".join(
                [
                    p["nb_id"],
                    p["disposition"],
                    p["status"],
                    str(p.get("package_completeness")),
                    str(p.get("opportunity_document_count")),
                    str((p.get("document_inventory") or {}).get("inventoried")),
                    str((p.get("document_inventory") or {}).get("download_pending")),
                    "Y" if p.get("ai_present") else "N",
                    str(p.get("original_ai_quote_ready")),
                    str(p.get("corrected_quote_ready")),
                    str(p.get("corrected_partial_quote")),
                    ";".join(p.get("readiness_blockers") or [])[:120],
                ]
            )
        )
    (out_dir / "RECONCILIATION.txt").write_text("\n".join(lines), encoding="utf-8")
    temp = Path(__file__).resolve().parents[2] / "TEMP" / "corrective_offline_reprocess"
    temp.mkdir(parents=True, exist_ok=True)
    (temp / "SUMMARY.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return report


if __name__ == "__main__":
    print(json.dumps(run_offline_reprocess(), indent=2, default=str))
