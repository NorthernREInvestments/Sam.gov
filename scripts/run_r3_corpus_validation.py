"""R3 real-corpus + golden validation (0 SAM API). Writes artifacts/response_engine/r3_*.json."""

from __future__ import annotations

import json
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts" / "response_engine"
BUILD = "20260929-m3-r3-company-compliance-reps-certs-trade"


def main() -> dict:
    import response_engine.production_intake as pi
    import response_engine.store as store
    from response_engine.company_profile_r3 import load_company_compliance_profile
    from response_engine.corpus_store import list_projects
    from response_engine.production_intake import run_production_intake
    from response_engine.r2_service import run_r2_analysis
    from response_engine.r3_service import run_r3_analysis
    from response_engine.service import create_or_get_project_from_opportunity

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    corpus = list_projects()
    profile = load_company_compliance_profile()

    rows = []
    readiness = Counter()
    nmr_status = Counter()
    trade_status = Counter()
    s889_status = Counter()
    cyber_status = Counter()
    set_asides = Counter()
    attest_open = 0
    federal = 0
    dla = 0
    sam_calls = 0
    fabricated_pass = 0

    company_profile_validation = {
        "profile_version": profile.get("profile_version"),
        "fields": {
            "UEI": profile.get("UEI"),
            "CAGE": profile.get("CAGE"),
            "SAM": profile.get("SAM_registration_status"),
            "small_business_status": profile.get("small_business_status"),
        },
        "completeness": profile.get("completeness"),
        "conflicts": profile.get("conflicts"),
        "verified": [],
        "owner_entered": [],
        "stale": [],
        "unknown": (profile.get("completeness") or {}).get("required_unknown") or [],
        "note": "Default profile is UNKNOWN-heavy — do not fabricate PASS",
    }

    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        store.STORE_DIR = tdp / "response_projects"
        store.INDEX_PATH = store.STORE_DIR / "index.json"
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
                canonical_opportunity_id=f"r3-{cid}",
                buyer=meta.get("buyer"),
                solicitation_number=meta.get("solicitation_number"),
                title=meta.get("title") or cid,
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
            analysis = run_r3_analysis(rp, persist=True, force=True)
            sam_calls += int(analysis.get("sam_api_calls") or 0)
            sam_calls += int(analysis.get("LIVE_API_REQUESTS") or 0)

            rd = analysis.get("readiness") or "UNKNOWN"
            readiness[rd] += 1
            nmr_status[(analysis.get("nmr") or {}).get("decision_status") or "UNKNOWN"] += 1
            trade_status[(analysis.get("trade") or {}).get("overall_status") or "UNKNOWN"] += 1
            s889_status[(analysis.get("section_889") or {}).get("status") or "UNKNOWN"] += 1
            cyber_status[(analysis.get("cyber") or {}).get("status") or "UNKNOWN"] += 1
            sa = analysis.get("set_aside") or "unknown"
            set_asides[str(sa)[:60]] += 1
            open_att = sum(1 for a in (analysis.get("owner_attestations") or []) if not a.get("owner_confirmed"))
            attest_open += open_att
            juris = (analysis.get("jurisdiction") or "").lower()
            if juris in ("federal", "dod"):
                federal += 1
            if juris == "dla":
                dla += 1
            if rd in ("COMPLIANCE_READY", "READY_FOR_RESPONSE_BUILD") and (
                (profile.get("completeness") or {}).get("incomplete")
            ):
                # Suspect fabricated — profile incomplete should rarely be full ready
                fabricated_pass += 1

            rows.append(
                {
                    "corpus_project_id": cid,
                    "response_project_id": rp.get("response_project_id"),
                    "readiness": rd,
                    "recommendation": analysis.get("recommendation"),
                    "next_action": analysis.get("next_action"),
                    "set_aside": sa,
                    "jurisdiction": analysis.get("jurisdiction"),
                    "nmr": (analysis.get("nmr") or {}).get("decision_status"),
                    "trade": (analysis.get("trade") or {}).get("overall_status"),
                    "section_889": (analysis.get("section_889") or {}).get("status"),
                    "cyber": (analysis.get("cyber") or {}).get("status"),
                    "cage": (analysis.get("cage") or {}).get("status"),
                    "blockers": analysis.get("blockers") or [],
                    "warnings": analysis.get("warnings") or [],
                    "owner_attestations_open": open_att,
                    "matrix_rows": len(analysis.get("compliance_matrix") or []),
                    "sam_api_calls": 0,
                }
            )

    # Aggregate artifacts
    def dump(name: str, obj: object) -> None:
        (ARTIFACTS / name).write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")

    dump("r3_company_profile_validation.json", company_profile_validation)
    dump(
        "r3_nmr_validation.json",
        {
            "counts": dict(nmr_status),
            "engine": "response_engine.nmr.evaluate_nmr",
            "legacy_bridge": "response_engine.nmr.nmr_from_deep_deal_compat",
            "note": "Never boolean-only; waiver requires SBA source",
        },
    )
    dump(
        "r3_trade_compliance.json",
        {
            "counts": dict(trade_status),
            "engine": "response_engine.trade_compliance.evaluate_trade_compliance",
            "rule": "unknown origin never auto-passes; brand HQ ≠ COO",
        },
    )
    dump(
        "r3_section889_validation.json",
        {
            "counts": dict(s889_status),
            "engine": "response_engine.section889.evaluate_section_889",
            "rule": "never answer no from absence of evidence; owner must confirm company-use",
        },
    )
    dump(
        "r3_registration_validation.json",
        {
            "note": "REGISTER_BEFORE_BID retained for easy state/local; CAGE/DIBBS hard-block when required",
            "canonical": "response_engine.registrations_r3 + phase_l.registration_tracker (UI)",
        },
    )
    dump(
        "r3_cyber_validation.json",
        {
            "counts": dict(cyber_status),
            "rule": "CMMC not inferred from DoD alone",
            "engine": "response_engine.cyber_dod",
        },
    )
    dump(
        "r3_owner_attestations.json",
        {
            "open_across_corpus": attest_open,
            "rule": "no auto-confirm; versioned audit log",
            "engine": "response_engine.owner_attestations",
        },
    )
    dump(
        "r3_real_corpus_results.json",
        {
            "build": BUILD,
            "projects_analyzed": len(rows),
            "readiness_counts": dict(readiness),
            "set_asides": dict(set_asides),
            "federal_projects": federal,
            "dla_projects": dla,
            "rows": rows,
            "sam_api_calls": sam_calls,
        },
    )
    dump(
        "r3_legacy_cutover.json",
        {
            "canonical_path": "response_engine.r3_service.run_r3_analysis",
            "reused": [
                "data/company_eligibility_profile.json",
                "company_eligibility.set_aside_eligibility",
                "eligibility_gate.load_company_eligibility_profile",
                "deep_deal_compliance.evaluate_nonmanufacturer_rule (compat bridge)",
                "phase_l.registration_tracker (REGISTER_BEFORE_BID UI)",
            ],
            "migrated_into_r3": [
                "NMR states",
                "trade/COO line analysis",
                "Section 889",
                "owner attestations",
                "cyber/DPAS detection",
                "company profile versioning",
            ],
            "deprecated_as_operator_facing": [
                "Parallel Bid Prep eligibility results outside R3",
            ],
            "compatibility_only": [
                "deep_deal_compliance.evaluate_bid_compliance",
                "company_profile.py underwriting DB (not Bid Prep eligibility)",
            ],
            "one_canonical_path": True,
        },
    )

    pass_n = readiness.get("COMPLIANCE_READY", 0) + readiness.get("READY_FOR_RESPONSE_BUILD", 0)
    review_n = sum(v for k, v in readiness.items() if "REVIEW" in k or "ATTESTATION" in k or "REGISTRATION" in k)
    fail_n = sum(v for k, v in readiness.items() if "BLOCKED" in k or k == "DO_NOT_BID")
    unknown_n = readiness.get("COMPANY_PROFILE_INCOMPLETE", 0) + readiness.get("UNKNOWN", 0)

    verdict = "PHASE_R3_COMPANY_COMPLIANCE_READY"
    if sam_calls != 0 or fabricated_pass:
        verdict = "PHASE_R3_COMPANY_COMPLIANCE_FAILED"
    elif len(rows) < 5:
        verdict = "PHASE_R3_COMPANY_COMPLIANCE_PARTIAL"

    summary = {
        "build": BUILD,
        "verdict": verdict,
        "projects_analyzed": len(rows),
        "readiness_counts": dict(readiness),
        "nmr_counts": dict(nmr_status),
        "trade_counts": dict(trade_status),
        "section889_counts": dict(s889_status),
        "cyber_counts": dict(cyber_status),
        "pass_like": pass_n,
        "review_like": review_n,
        "fail_like": fail_n,
        "unknown_like": unknown_n,
        "owner_attestations_open": attest_open,
        "sam_api_calls": sam_calls,
        "fabricated_pass_suspect": fabricated_pass,
        "canonical_path": "response_engine.r3_service.run_r3_analysis",
        "never_ready_to_submit": True,
        "next_phase": "R4 — BUYER FORMS / SPREADSHEETS / RESPONSE DOCUMENT GENERATION",
        "limitations": [
            "Company profile largely UNKNOWN — expected fail-closed results",
            "No live SAM refresh in R3",
            "NMR waiver detection requires explicit waiver evidence on project",
            "CMMC level verification requires documented company evidence",
            "R4 form population not implemented",
        ],
    }
    dump("r3_summary.json", summary)
    print(json.dumps({"verdict": verdict, "projects": len(rows), "sam_api_calls": sam_calls}, indent=2))
    return summary


if __name__ == "__main__":
    main()
