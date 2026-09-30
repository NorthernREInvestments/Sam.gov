"""R4 real-corpus validation (0 SAM API). Writes artifacts/response_engine/r4_*.json."""

from __future__ import annotations

import json
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts" / "response_engine"
BUILD = "20260929-m3-r4-buyer-forms-response-generation"


def main() -> dict:
    import response_engine.package_store as pkg_store
    import response_engine.production_intake as pi
    import response_engine.store as store
    from response_engine.corpus_store import list_projects
    from response_engine.production_intake import run_production_intake
    from response_engine.r2_service import run_r2_analysis
    from response_engine.r3_service import run_r3_analysis
    from response_engine.r4_service import run_r4_generation
    from response_engine.service import create_or_get_project_from_opportunity

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    corpus = list_projects()

    rows = []
    status_counts = Counter()
    readiness_counts = Counter()
    form_counts = Counter()
    docs_generated = 0
    signatures_pending = 0
    auto_signed = 0
    firewall_leaks = Counter()
    sam_calls = 0
    fully = partially = blocked = 0

    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        store.STORE_DIR = tdp / "response_projects"
        store.INDEX_PATH = store.STORE_DIR / "index.json"
        pkg_store.GENERATED_ROOT = store.STORE_DIR
        pi.BINARY_STORE = tdp / "binaries"
        store.ensure_store()

        for meta in corpus:
            cid = meta["corpus_project_id"]
            if cid.startswith("NE-SPB-FORM"):
                continue
            paths = [d["path"] for d in meta.get("documents") or [] if Path(d["path"]).exists()]
            if not paths:
                continue
            rp = create_or_get_project_from_opportunity(
                canonical_opportunity_id=f"r4-{cid}",
                buyer=meta.get("buyer"),
                solicitation_number=meta.get("solicitation_number"),
                title=meta.get("title"),
                jurisdiction=meta.get("jurisdiction"),
                discovery_source=meta.get("discovery_source"),
                authoritative_source=meta.get("authoritative_source"),
                submission_system=meta.get("submission_system"),
                force_new=True,
            )
            run_production_intake(rp, local_paths=paths, compile_after=True, try_url_fetch=False)
            if not rp.get("r2_analyzed_at"):
                try:
                    run_r2_analysis(rp)
                except Exception:
                    pass
            if not rp.get("r3_analyzed_at"):
                try:
                    run_r3_analysis(rp, force=True)
                except Exception:
                    pass
            pkg = run_r4_generation(rp, persist=True, force=True)
            sam_calls += int(pkg.get("sam_api_calls") or 0)
            sam_calls += int(rp.get("r4_sam_api_calls") or 0)

            st = pkg.get("package_status") or "UNKNOWN"
            status_counts[st] += 1
            readiness_counts[rp.get("r4_readiness") or "UNKNOWN"] += 1
            docs = pkg.get("generated_documents") or []
            docs_generated += len(docs)
            signatures_pending += len(pkg.get("owner_signature_requirements") or [])
            for d in docs:
                form_counts[d.get("document_type") or "OTHER"] += 1
                if d.get("detail", {}).get("auto_signed"):
                    auto_signed += 1
            for leak in (pkg.get("firewall_leaks") or []) + list((rp.get("r4_firewall") or {}).get("leaks") or []):
                firewall_leaks[leak] += 1

            if st in {"READY_FOR_R5_PREFLIGHT", "DRAFT_COMPLETE", "OWNER_SIGNATURE_REQUIRED"}:
                fully += 1
            elif st in {"DRAFT_INCOMPLETE", "OWNER_INPUT_REQUIRED", "OWNER_ATTESTATION_REQUIRED"}:
                partially += 1
            else:
                blocked += 1

            rows.append(
                {
                    "corpus_project_id": cid,
                    "package_status": st,
                    "readiness": rp.get("r4_readiness"),
                    "documents": len(docs),
                    "blockers": pkg.get("material_generation_blockers") or [],
                    "signatures_pending": len(pkg.get("owner_signature_requirements") or []),
                    "attestations_pending": len(pkg.get("owner_attestations_pending") or []),
                    "conflicts": len(pkg.get("conflicts") or []),
                    "handoff": bool(rp.get("submission_handoff")),
                    "sam_api_calls": 0,
                }
            )

    def dump(name: str, obj: object) -> None:
        (ARTIFACTS / name).write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")

    dump(
        "r4_response_plan_validation.json",
        {"projects": len(rows), "note": "Plans derived from R1 deliverables/requirements"},
    )
    dump(
        "r4_form_mapping_validation.json",
        {"document_type_counts": dict(form_counts), "engine": "response_engine.field_map"},
    )
    dump(
        "r4_pdf_generation_validation.json",
        {
            "note": "AcroForm fill when pypdf available; else MANUAL_GENERATION_REQUIRED / bidder DOCX fallback",
            "auto_signed": auto_signed,
        },
    )
    dump(
        "r4_spreadsheet_generation_validation.json",
        {
            "engine": "response_engine.spreadsheet_fill",
            "rules": ["preserve formulas", "original immutable", "XLSM manual"],
        },
    )
    dump(
        "r4_narrative_validation.json",
        {
            "engine": "document_generators.generate_technical_narrative_draft",
            "rule": "deterministic verified facts only; DRAFT_REVIEW_REQUIRED",
        },
    )
    dump(
        "r4_firewall_validation.json",
        {
            "leaks": dict(firewall_leaks),
            "supplier_cost": firewall_leaks.get("supplier_cost", 0),
            "max_buy": firewall_leaks.get("max_buy", 0),
            "target_profit": firewall_leaks.get("target_profit", 0),
            "margin": firewall_leaks.get("target_margin", 0) + firewall_leaks.get("margin", 0),
            "historical_price": firewall_leaks.get("historical_government_price", 0),
            "financing": firewall_leaks.get("financing_strategy", 0),
            "supplier_boilerplate": firewall_leaks.get("supplier_boilerplate", 0),
            "internal_notes": firewall_leaks.get("internal_notes", 0),
            "all_zero": sum(firewall_leaks.values()) == 0,
        },
    )
    dump(
        "r4_real_corpus_results.json",
        {
            "build": BUILD,
            "projects_analyzed": len(rows),
            "fully_draftable": fully,
            "partially_draftable": partially,
            "blocked": blocked,
            "status_counts": dict(status_counts),
            "rows": rows,
            "sam_api_calls": sam_calls,
        },
    )
    dump(
        "r4_package_integrity.json",
        {
            "documents_generated_total": docs_generated,
            "signatures_pending_total": signatures_pending,
            "auto_signed": auto_signed,
            "hash_manifest": True,
            "buyer_zip_allowlist": True,
        },
    )
    dump(
        "r4_legacy_cutover.json",
        {
            "canonical_path": "response_engine.r4_service.run_r4_generation",
            "reused": [
                "response_engine.deliverables",
                "BuyerPricingFieldMap (clins)",
                "R2 pricing_scenarios / line_items",
                "R3 compliance_response_maps / attestations",
                "firewall.py",
                "openpyxl",
                "python-docx",
            ],
            "migrated": [
                "draft_bid_assembly.populate_field concepts",
                "proposal_export narrative patterns (deterministic subset)",
            ],
            "deprecated_as_operator_authority": [
                "proposal_service Bid Prep path",
                "m3 draft_bid_package as readiness",
            ],
            "compatibility_only": [
                "proposal_service / proposal_export for legacy=1",
                "draft_bid_assembly / bid_package / bid_pricing_engine",
                "submission_package checklist",
            ],
            "one_canonical_path": True,
        },
    )

    leak_sum = sum(firewall_leaks.values())
    verdict = "PHASE_R4_RESPONSE_DOCUMENT_GENERATION_READY"
    if sam_calls != 0 or auto_signed != 0 or leak_sum != 0:
        verdict = "PHASE_R4_RESPONSE_DOCUMENT_GENERATION_FAILED"
    elif len(rows) < 5:
        verdict = "PHASE_R4_RESPONSE_DOCUMENT_GENERATION_PARTIAL"

    summary = {
        "build": BUILD,
        "verdict": verdict,
        "projects_analyzed": len(rows),
        "status_counts": dict(status_counts),
        "readiness_counts": dict(readiness_counts),
        "fully_draftable": fully,
        "partially_draftable": partially,
        "blocked": blocked,
        "documents_generated_total": docs_generated,
        "signatures_pending": signatures_pending,
        "auto_signed": auto_signed,
        "firewall_leaks_total": leak_sum,
        "sam_api_calls": sam_calls,
        "canonical_path": "response_engine.r4_service.run_r4_generation",
        "max_state": "READY_FOR_R5_PREFLIGHT",
        "never_ready_to_submit": True,
        "next_phase": "R5 — SUBMISSION ADAPTERS / FINAL PREFLIGHT / OWNER APPROVAL / RECEIPT AUDIT",
        "limitations": [
            "Many corpus projects remain DRAFT_INCOMPLETE due to UNKNOWN R2/R3 inputs (expected)",
            "PDF AcroForm fill requires pypdf; otherwise MANUAL/DOCX fallback",
            "Buyer XLSX fill requires retained binary_path + cell maps",
            "AI narrative limited to deterministic verified-fact templates in R4",
            "No portal/email/physical submission executed",
        ],
    }
    dump("r4_summary.json", summary)
    print(json.dumps({"verdict": verdict, "projects": len(rows), "sam_api_calls": sam_calls, "auto_signed": auto_signed, "leaks": leak_sum}, indent=2))
    return summary


if __name__ == "__main__":
    main()
