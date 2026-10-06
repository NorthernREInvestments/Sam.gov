"""Phases 22–25 — P1 audit, package provenance, bid-ready, large-test entry gate."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from application_clock import now_utc
from m3_data_root import data_path
from owner_channel_tests.models import (
    BID_READY_SPEC,
    BUILD,
    LARGE_TEST_GATE,
    P1_AUDIT,
    PACKAGE_PROVENANCE_AUDIT,
)


BID_READY_REQUIREMENTS = [
    {"id": "solicitation_docs", "mandatory": True, "wired": False},
    {"id": "amendments_processed", "mandatory": True, "wired": False},
    {"id": "submission_portal_method", "mandatory": True, "wired": False},
    {"id": "deadline_timezone", "mandatory": True, "wired": True},
    {"id": "required_forms", "mandatory": True, "wired": False},
    {"id": "required_signatures", "mandatory": True, "wired": False},
    {"id": "pricing_schedule", "mandatory": True, "wired": False},
    {"id": "certifications", "mandatory": True, "wired": False},
    {"id": "reps_certs", "mandatory": True, "wired": False},
    {"id": "eligibility", "mandatory": True, "wired": True},
    {"id": "delivery", "mandatory": True, "wired": True},
    {"id": "insurance_bonding_if_applicable", "mandatory": True, "wired": False},
    {"id": "oem_authorization", "mandatory": False, "wired": False},
    {"id": "country_of_origin", "mandatory": False, "wired": False},
    {"id": "cybersecurity", "mandatory": False, "wired": False},
    {"id": "warranty", "mandatory": False, "wired": False},
    {"id": "inspection", "mandatory": False, "wired": False},
    {"id": "past_performance", "mandatory": False, "wired": False},
    {"id": "samples_catalogs", "mandatory": False, "wired": False},
    {"id": "questions_deadline", "mandatory": True, "wired": False},
    {"id": "bid_validity", "mandatory": True, "wired": False},
    {"id": "execution_risks", "mandatory": True, "wired": True},
    {"id": "economics", "mandatory": True, "wired": True},
    {"id": "financing", "mandatory": True, "wired": True},
]


def audit_package_provenance() -> dict[str, Any]:
    """Sample package docs under opengov_public_docs for provenance completeness."""
    root = data_path("opengov_public_docs/documents")
    complete = partial = missing = 0
    deficiencies: list[str] = []
    samples: list[dict[str, Any]] = []

    # Prefer channel-test package folders
    targets = [
        root / "go-metro" / "298984",
        root / "dekalbcountyga" / "286698",
    ]
    for folder in targets:
        if not folder.exists():
            missing += 1
            deficiencies.append(f"missing_package_dir:{folder}")
            continue
        for f in folder.iterdir():
            if not f.is_file():
                continue
            meta = {
                "source_system": "opengov_public_docs",
                "source_url": None,  # often not persisted on disk copy
                "retrieved_timestamp": None,
                "document_id": f.stem,
                "filename": f.name,
                "document_type": f.suffix.lstrip(".").lower(),
                "amendment_status": "UNKNOWN",
                "hash": None,
                "opportunity_link": str(folder.relative_to(root)).replace("\\", "/"),
            }
            # Check sidecar meta if any
            side = f.with_suffix(f.suffix + ".meta.json")
            has_side = side.exists()
            if has_side:
                try:
                    meta.update(json.loads(side.read_text(encoding="utf-8")))
                except Exception:
                    pass
            fields_present = sum(
                1
                for k in (
                    "source_system",
                    "filename",
                    "document_type",
                    "opportunity_link",
                )
                if meta.get(k)
            )
            # Complete requires URL + retrieved + hash ideally
            if meta.get("source_url") and meta.get("retrieved_timestamp") and meta.get("hash"):
                status = "PACKAGE_PROVENANCE_COMPLETE"
                complete += 1
            elif fields_present >= 3:
                status = "PACKAGE_PROVENANCE_PARTIAL"
                partial += 1
                if not meta.get("source_url"):
                    deficiencies.append(f"no_source_url:{f.name}")
                if not meta.get("hash"):
                    deficiencies.append(f"no_hash:{f.name}")
            else:
                status = "PACKAGE_PROVENANCE_MISSING"
                missing += 1
            samples.append({"file": f.name, "status": status, "meta": meta})

    # Dedupe deficiency messages
    deficiencies = sorted(set(deficiencies))[:40]
    out = {
        "Complete": complete,
        "Partial": partial,
        "Missing": missing,
        "Main_deficiencies": deficiencies[:15],
        "samples": samples[:20],
        "plan_to_fix_before_large_test": [
            "Persist source_url + retrieved_timestamp on every package download",
            "Store content hash (sha256) for each document",
            "Record amendment status from portal listing",
            "Link document_id → opportunity canonical id in package index",
        ],
        "status": "PARTIAL" if partial or missing else "COMPLETE",
    }
    data_path(PACKAGE_PROVENANCE_AUDIT).write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    return out


def define_bid_ready() -> dict[str, Any]:
    reqs = list(BID_READY_REQUIREMENTS)
    mandatory = [r for r in reqs if r["mandatory"]]
    wired_m = [r for r in mandatory if r["wired"]]
    gaps = [r["id"] for r in mandatory if not r["wired"]]
    out = {
        "build": BUILD,
        "definition": "BID_READY requires all mandatory pre-bid items cleared",
        "requirements": reqs,
        "Requirements_defined": True,
        "Requirements_wired_mandatory": f"{len(wired_m)}/{len(mandatory)}",
        "Remaining_gaps": gaps,
        "Status": "DEFINED_NOT_FULLY_WIRED",
        "enforcement_rule": "No BID_READY unless all mandatory items cleared",
    }
    data_path(BID_READY_SPEC).write_text(json.dumps(out, indent=2), encoding="utf-8")
    return out


def run_p1_audit(*, package_prov: dict[str, Any], bid_ready: dict[str, Any], new_issues: dict[str, Any]) -> dict[str, Any]:
    # Known P1 from prior register
    known = [
        {
            "id": "package_provenance_uneven",
            "status": "OPEN" if package_prov.get("status") != "COMPLETE" else "FIXED",
            "before_large_test": True,
            "changes_needed": package_prov.get("plan_to_fix_before_large_test"),
        },
        {
            "id": "bid_ready_package",
            "status": "OPEN" if bid_ready.get("Status") != "ENFORCED" else "FIXED",
            "before_large_test": True,
            "changes_needed": [
                "Wire mandatory BID_READY checklist into opportunity state",
                "Block BID_READY until remaining_gaps empty",
                f"Remaining: {bid_ready.get('Remaining_gaps')}",
            ],
        },
    ]
    out = {
        "build": BUILD,
        "known_p1": known,
        "Open": sum(1 for k in known if k["status"] == "OPEN"),
        "Fixed": sum(1 for k in known if k["status"] == "FIXED"),
        "Remaining": [k for k in known if k["status"] == "OPEN"],
        "new_issues": new_issues,
        "updated_at": now_utc().isoformat(),
    }
    data_path(P1_AUDIT).write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    return out


def large_test_entry_gate(
    *,
    p0_open: int,
    real_interaction_proven: bool,
    real_quote_ingestion_proven: bool,
    line_matching_proven: bool,
    auto_wire_proven: bool,
    package_provenance_sufficient: bool,
    bid_ready_framework_sufficient: bool,
    conservation_pass: bool,
    contamination_zero: bool,
) -> dict[str, Any]:
    criteria = {
        "P0_open": p0_open,
        "Real_supplier_interaction_proven": real_interaction_proven,
        "Real_quote_ingestion_proven": real_quote_ingestion_proven,
        "Line_matching_proven": line_matching_proven,
        "Auto_wire_proven": auto_wire_proven,
        "Package_provenance_sufficient": package_provenance_sufficient,
        "Bid_ready_framework_sufficient": bid_ready_framework_sufficient,
        "Conservation_pass": conservation_pass,
        "Contamination_zero": contamination_zero,
    }
    # Real interaction not yet done by owner — gate must fail honestly
    ready = (
        p0_open == 0
        and real_interaction_proven
        and real_quote_ingestion_proven
        and line_matching_proven
        and auto_wire_proven
        and package_provenance_sufficient
        and bid_ready_framework_sufficient
        and conservation_pass
        and contamination_zero
    )

    if ready:
        next_run = "LARGE_TEST"
    elif p0_open > 0:
        next_run = "NO"
    elif not real_interaction_proven:
        next_run = "OWNER_CHANNEL_TESTS_CONTINUE"
    elif not (package_provenance_sufficient and bid_ready_framework_sufficient):
        next_run = "FIX_P1_THEN_LARGE_TEST"
    else:
        next_run = "OWNER_CHANNEL_TESTS_CONTINUE"

    out = {
        "LARGE_TEST_ENTRY_READY": ready,
        **criteria,
        "NEXT_RUN_ALLOWED": next_run,
        "do_not_run_large_test_in_this_build": True,
        "updated_at": now_utc().isoformat(),
    }
    data_path(LARGE_TEST_GATE).write_text(json.dumps(out, indent=2), encoding="utf-8")
    return out
