#!/usr/bin/env python3
"""Build 002N correction pass — re-extract + demote false Stage C. Zero NextBid. No new OpenAI spend."""
from __future__ import annotations

import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

for line in Path(".env").read_text(encoding="utf-8").splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from m3.research.commercial_criteria import STAGE_B, STAGE_C, evaluate_procurement_pipeline
from m3.research.extract_fields import enrich_item_from_text
from m3.research.nextbid_client import load_checkpoint
from m3.research.nextbid_controller import acquisition_locked
from m3.research.procurement_registry import ingest_population, load_records
from scripts.build002n_run import (
    assert_nextbid_locked,
    classify_disposition,
    collect_text_sources,
    index_packages,
    resolve_package_key,
    write_reports,
)


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> None:
    assert acquisition_locked()
    lock = assert_nextbid_locked()
    print("002N_correct lock", lock["locked"], flush=True)

    from m3.db import SessionLocal

    prev = json.loads(
        (ROOT / "data" / "acceptance" / "build002n_acceptance.json").read_text(encoding="utf-8")
    )
    audit = json.loads(
        (ROOT / "data" / "acceptance" / "_tmp_stage_b_c_audit.json").read_text(encoding="utf-8")
    )
    audit_ids = {str(r.get("nb_id")) for r in audit.get("all_stage_b") or []}
    ind_prev = {
        r["nb_id"]: r
        for r in json.loads(
            (ROOT / "data" / "acceptance" / "build002n_individual_accounting.json").read_text(
                encoding="utf-8"
            )
        ).get("rows")
        or []
    }

    recs = load_records()
    targets = []
    for r in recs.values():
        nb = str(r.get("nb_id") or r.get("provider_id") or "")
        if not nb:
            continue
        st = r.get("procurement_stage") or r.get("stage")
        if st in {STAGE_B, STAGE_C} or nb in audit_ids or nb in ind_prev:
            targets.append(dict(r))

    # Dedup by nb
    by_nb = {}
    for r in targets:
        nb = str(r.get("nb_id") or r.get("provider_id") or "")
        by_nb[nb] = r
    # Ensure audit 710
    for a in audit.get("all_stage_b") or []:
        nb = str(a.get("nb_id") or "")
        if nb and nb not in by_nb:
            by_nb[nb] = {"nb_id": nb, "title": a.get("title"), "requirement_class": a.get("requirement_class")}

    text_map = collect_text_sources()
    db = SessionLocal()
    try:
        pkg_idx = index_packages(db)
    finally:
        db.close()

    work = []
    for nb, row in by_nb.items():
        prev_row = ind_prev.get(nb) or {}
        src = text_map.get(nb) or {}
        url = row.get("official_url") or src.get("official_url") or prev_row.get("official_url")
        sol = row.get("solicitation_number") or src.get("solicitation_number") or prev_row.get(
            "solicitation_number"
        )
        pkg_key = resolve_package_key(pkg_idx, nb, url, sol)
        pkg_text = pkg_idx["text_by_ext"].get(pkg_key or "") or ""
        pkg_meta = pkg_idx["meta_by_ext"].get(pkg_key or "") or {}
        base = src.get("text") or ""
        # Keep prior recovered text length when available via package
        document_text = pkg_text if len(pkg_text) >= len(base) else base
        if pkg_text and base and pkg_text != base:
            document_text = (base + "\n" + pkg_text)[:50000]
        item = {
            **row,
            "nb_id": nb,
            "external_key": nb,
            "title": row.get("title") or src.get("title") or prev_row.get("title"),
            "official_url": url,
            "solicitation_number": sol,
            "buyer_name": row.get("agency") or row.get("buyer_name") or src.get("buyer_name"),
            "agency": row.get("agency") or src.get("buyer_name"),
            "requirement_class": row.get("requirement_class")
            or src.get("requirement_class")
            or "product_supply",
            "solicitation_status": row.get("solicitation_status")
            or src.get("solicitation_status")
            or "unknown",
            "verified_individual_solicitation": True,
            "record_type": "INDIVIDUAL_SOLICITATION",
            "deadline_at": row.get("deadline_at") or src.get("deadline_at"),
            "document_text": document_text,
            "document_text_len": len(document_text),
            "package_status": pkg_meta.get("package_status") or prev_row.get("package_status"),
            "package_downloaded_count": pkg_meta.get("downloaded_count")
            or prev_row.get("package_downloaded_count")
            or 0,
            "quantity": None,
            "quantity_semantic": None,
            "quantity_evidence": None,
            "quantity_kind": None,
        }
        work.append(item)

    print(f"002N_correct work={len(work)}", flush=True)
    dispositions = Counter()
    individual = []
    new_stage_c = []
    promising = []
    registry_payloads = []
    recovered = 0

    for it in work:
        enriched = enrich_item_from_text(
            it, it.get("document_text") or "", source="build002n_correction"
        )
        pipe = evaluate_procurement_pipeline(
            enriched, document_text=it.get("document_text") or ""
        )
        for k, v in (pipe.get("enriched_fields") or {}).items():
            if v is not None:
                it[k] = v
        if pipe.get("quantity") is not None:
            it["quantity"] = pipe["quantity"]
        if pipe.get("quantity_semantic"):
            it["quantity_semantic"] = pipe["quantity_semantic"]
        if pipe.get("quantity_evidence"):
            it["quantity_evidence"] = pipe["quantity_evidence"]
        it["quantity_defined"] = bool(pipe.get("quantity_defined"))
        it["procurement_pipeline"] = pipe
        it["procurement_stage"] = pipe.get("stage") or STAGE_B

        # Preserve prior OpenAI meta for accounting only (no new spend)
        prior = ind_prev.get(it["nb_id"]) or {}
        openai_meta = prior.get("openai") or {}

        disp = classify_disposition(
            pipe,
            text_len=int(it.get("document_text_len") or 0),
            package_status=it.get("package_status"),
            openai_meta=openai_meta if openai_meta.get("budget_denied") else None,
        )
        # Budget-skipped long text is not extraction_failure
        if (
            openai_meta.get("budget_denied")
            and disp == "extraction_failure"
            and int(it.get("document_text_len") or 0) >= 400
            and not pipe.get("quantity_defined")
        ):
            disp = "quantity_absent_from_complete_documents"

        dispositions[disp] += 1
        if it.get("quantity") not in (None, "", "unknown"):
            recovered += 1
        row = {
            "nb_id": it["nb_id"],
            "title": (it.get("title") or "")[:200],
            "solicitation_number": it.get("solicitation_number"),
            "official_url": it.get("official_url"),
            "disposition": disp,
            "stage": pipe.get("stage"),
            "quantity": it.get("quantity"),
            "quantity_semantic": it.get("quantity_semantic"),
            "quantity_kind": it.get("quantity_kind"),
            "quantity_defined": bool(pipe.get("quantity_defined")),
            "quantity_evidence": (it.get("quantity_evidence") or [])[:6],
            "unknown_flags": pipe.get("unknown_flags") or [],
            "document_text_len": it.get("document_text_len"),
            "package_status": it.get("package_status"),
            "package_downloaded_count": it.get("package_downloaded_count"),
            "openai": openai_meta,
            "in_audit_710": it["nb_id"] in audit_ids,
            "requirement_class": it.get("requirement_class"),
            "correction_pass": True,
        }
        individual.append(row)
        registry_payloads.append(
            {
                "provider": "nextbid",
                "provider_id": it["nb_id"],
                "nb_id": it["nb_id"],
                "title": it.get("title"),
                "solicitation_number": it.get("solicitation_number"),
                "official_url": it.get("official_url"),
                "agency": it.get("agency") or it.get("buyer_name"),
                "deadline_at": it.get("deadline_at"),
                "requirement_class": it.get("requirement_class"),
                "procurement_stage": pipe.get("stage") or STAGE_B,
                "quantity": it.get("quantity"),
                "quantity_unit": it.get("quantity_unit"),
                "quantity_semantic": it.get("quantity_semantic"),
                "quantity_evidence": it.get("quantity_evidence"),
                "package_status": it.get("package_status"),
                "document_text_len": it.get("document_text_len"),
                "build_002n_disposition": disp,
                "active_queue": pipe.get("stage") in {STAGE_B, STAGE_C},
            }
        )
        if pipe.get("stage") == STAGE_C and pipe.get("quantity_defined"):
            new_stage_c.append(
                {
                    **row,
                    "evidence": {
                        "quantity": row["quantity"],
                        "quantity_semantic": row["quantity_semantic"],
                        "quantity_evidence": row["quantity_evidence"],
                        "pipeline_reasons": pipe.get("reasons"),
                        "official_url": row["official_url"],
                        "document_text_len": row["document_text_len"],
                        "package_status": row["package_status"],
                        "build": "002N",
                        "correction_pass": True,
                    },
                }
            )
        elif disp in {
            "estimated_quantity",
            "missing_document",
            "quantity_absent_from_complete_documents",
            "extraction_failure",
            "source_access_restricted",
        }:
            promising.append(
                {
                    "nb_id": row["nb_id"],
                    "title": row["title"],
                    "disposition": disp,
                    "official_url": row["official_url"],
                    "document_text_len": row["document_text_len"],
                    "package_status": row["package_status"],
                    "next_action": (
                        "acquire_public_solicitation_documents"
                        if disp in {"missing_document", "source_access_restricted"}
                        else "confirm_firm_ordered_quantity"
                    ),
                }
            )

    print(f"002N_correct registry n={len(registry_payloads)}", flush=True)
    reg = ingest_population(registry_payloads, actor="build_002n_correction")

    # Re-verify prior Stage C set from previous acceptance
    prior_c = list((prev.get("stage_c_verification") or {}).get("rows") or [])
    # Also include any still-marked Stage C in registry
    stage_c_recs = [
        r
        for r in load_records().values()
        if (r.get("procurement_stage") or r.get("stage")) == STAGE_C
    ]
    kept = 0
    demoted = 0
    sc_rows = []
    for r in stage_c_recs:
        item = dict(r)
        item.setdefault("verified_individual_solicitation", True)
        item.setdefault("record_type", "INDIVIDUAL_SOLICITATION")
        txt = ""
        nb = r.get("nb_id") or r.get("provider_id")
        src = text_map.get(str(nb) or "") or {}
        txt = src.get("text") or ""
        pkg_key = resolve_package_key(
            pkg_idx, str(nb or ""), r.get("official_url"), r.get("solicitation_number")
        )
        pkg_text = pkg_idx["text_by_ext"].get(pkg_key or "") or ""
        if len(pkg_text) > len(txt):
            txt = pkg_text
        pipe = evaluate_procurement_pipeline(item, document_text=txt)
        still = pipe.get("stage") == STAGE_C and pipe.get("quantity_defined")
        if still:
            kept += 1
        else:
            demoted += 1
        sc_rows.append(
            {
                "nb_id": nb,
                "title": (r.get("title") or "")[:160],
                "still_stage_c": still,
                "stage": pipe.get("stage"),
                "quantity": pipe.get("quantity"),
                "quantity_semantic": pipe.get("quantity_semantic"),
                "quantity_defined": pipe.get("quantity_defined"),
                "unknown_flags": pipe.get("unknown_flags"),
            }
        )

    acceptance = {
        **prev,
        "correction_pass_at": _utc(),
        "nextbid_lock_final": assert_nextbid_locked(),
        "metered_nextbid_calls": 0,
        "totals": {
            "investigated": len(work),
            "audit_710_covered": sum(1 for r in individual if r.get("in_audit_710")),
            "quantities_recovered": recovered,
            "new_stage_c_fits": len(new_stage_c),
            "dispositions": dict(dispositions),
            "promising_queue": len(promising),
            "complete_packages": sum(1 for r in individual if r.get("package_status") == "COMPLETE"),
            "partial_packages": sum(1 for r in individual if r.get("package_status") == "PARTIAL"),
            "restricted_packages": sum(
                1 for r in individual if r.get("package_status") == "ACCESS_RESTRICTED"
            ),
            "long_text_ge_400": sum(
                1 for r in individual if int(r.get("document_text_len") or 0) >= 400
            ),
        },
        "new_stage_c": new_stage_c,
        "promising_queue": promising,
        "registry_persist": reg,
        "stage_c_verification": {
            "examined": len(sc_rows),
            "kept_stage_c": kept,
            "demoted": demoted,
            "rows": sc_rows,
        },
        "openai_execution": prev.get("openai_execution"),
        "openai_verification": prev.get("openai_verification"),
        "free_document_recovery": prev.get("free_document_recovery"),
        "finished_at": _utc(),
        "note": "Correction pass after singular/hundred/service false-positive fixes; no new OpenAI spend; NextBid locked",
    }

    out = ROOT / "data" / "acceptance"
    (out / "build002n_acceptance.json").write_text(
        json.dumps(acceptance, indent=2, default=str), encoding="utf-8"
    )
    (out / "build002n_individual_accounting.json").write_text(
        json.dumps({"build": "002N", "rows": individual}, indent=2, default=str), encoding="utf-8"
    )
    (out / "build002n_stage_c_evidence.json").write_text(
        json.dumps(
            {"build": "002N", "count": len(new_stage_c), "opportunities": new_stage_c},
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    (out / "build002n_promising_queue.json").write_text(
        json.dumps(
            {"build": "002N", "count": len(promising), "queue": promising}, indent=2, default=str
        ),
        encoding="utf-8",
    )
    write_reports(acceptance, individual)
    print(
        f"002N_correct DONE qty={recovered} stage_c={len(new_stage_c)} "
        f"disp={dict(dispositions)} kept_c={kept} demoted_c={demoted}",
        flush=True,
    )


if __name__ == "__main__":
    main()
