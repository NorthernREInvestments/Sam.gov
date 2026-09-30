"""Legacy bid-compliance cutover — R1 is canonical operator-facing readiness.

Legacy analyze_bid_compliance may still run for old API shapes, but operator
Bid Prep / readiness claims MUST come from ResponseProject.
"""

from __future__ import annotations

from typing import Any

from response_engine.compliance import operator_compliance_summary
from response_engine.store import find_by_opportunity, load_project


def r1_operator_readiness(canonical_opportunity_id: str) -> dict[str, Any] | None:
    project = find_by_opportunity(canonical_opportunity_id)
    if not project:
        # try raw id
        project = load_project(canonical_opportunity_id)
    if not project:
        return None
    summary = operator_compliance_summary(project)
    material_unresolved = int(summary.get("material_unresolved") or 0)
    hard = int(summary.get("hard_blockers") or 0)
    # Never claim bid-ready / ready-to-submit from R1.1
    if hard or material_unresolved:
        ladder = "BLOCKED" if hard else "REVIEW_REQUIRED"
    elif summary.get("status") == "READY_FOR_RESPONSE_BUILD":
        ladder = "SOLICITATION_COMPILED"
    elif summary.get("status") == "DOCUMENTS_INCOMPLETE":
        ladder = "DOCUMENTS_INCOMPLETE"
    else:
        ladder = summary.get("status") or "COMPLIANCE_REVIEW"

    return {
        "canonical": True,
        "source": "response_engine_r1",
        "response_project_id": project["response_project_id"],
        "ladder": ladder,
        "response_status": project.get("response_status"),
        "hard_blockers": hard,
        "material_unresolved": material_unresolved,
        "operator_summary": summary,
        "package_completeness": project.get("package_completeness"),
        "ready_to_submit": False,  # hard invariant for R1.1
        "bid_ready": False,
        "note": "R1 is the canonical Bid Prep readiness source. Legacy READY cannot override.",
    }


def wrap_legacy_bid_readiness(opportunity_id: str, legacy_analysis: dict[str, Any] | None = None) -> dict[str, Any]:
    """Compatibility shape for /api/opportunities/.../bid-readiness — R1 wins."""
    cid = opportunity_id[2:] if opportunity_id.startswith("c:") else opportunity_id
    r1 = r1_operator_readiness(cid)
    legacy = legacy_analysis or {}
    legacy_ladder = (legacy.get("bid_readiness") or {}).get("ladder") or legacy.get("ladder")

    if r1:
        conflict = False
        if legacy_ladder in {"READY", "BID_READY", "READY_TO_BID", "GO"} and r1["ladder"] in {
            "BLOCKED",
            "REVIEW_REQUIRED",
            "DOCUMENTS_INCOMPLETE",
        }:
            conflict = True
        return {
            "bid_readiness": {
                "ladder": r1["ladder"],
                "canonical_source": "response_engine_r1",
                "ready_to_submit": False,
                "bid_ready": False,
                "hard_blockers": r1["hard_blockers"],
                "material_unresolved": r1["material_unresolved"],
                "legacy_ladder_ignored": legacy_ladder if conflict else None,
                "conflict_with_legacy": conflict,
                "note": r1["note"],
            },
            "package_completeness": r1.get("package_completeness"),
            "commercial_verification": legacy.get("commercial_verification"),
            "response_project_id": r1["response_project_id"],
            "operator_summary": r1["operator_summary"],
        }

    # No R1 project yet — do not invent READY from legacy alone for operator claims
    return {
        "bid_readiness": {
            "ladder": "NOT_STARTED",
            "canonical_source": "response_engine_r1_absent",
            "ready_to_submit": False,
            "bid_ready": False,
            "legacy_analysis_present": bool(legacy),
            "legacy_ladder": legacy_ladder,
            "note": "Start Bid Prep to create ResponseProject. Legacy READY is not operator-facing.",
        },
        "package_completeness": legacy.get("package_completeness"),
        "commercial_verification": legacy.get("commercial_verification"),
    }


def wrap_legacy_compliance_matrix(opportunity_id: str, legacy_matrix: dict[str, Any] | None = None) -> dict[str, Any]:
    cid = opportunity_id[2:] if opportunity_id.startswith("c:") else opportunity_id
    r1 = r1_operator_readiness(cid)
    if r1:
        project = load_project(r1["response_project_id"])
        return {
            "canonical_source": "response_engine_r1",
            "matrix": (project or {}).get("compliance_matrix"),
            "operator_summary": r1["operator_summary"],
            "legacy_matrix_suppressed": True,
        }
    return {
        "canonical_source": "legacy_only_no_r1_project",
        "matrix": legacy_matrix or {},
        "note": "No ResponseProject — legacy matrix shown for compatibility only; not Bid Prep authority.",
        "legacy_matrix_suppressed": False,
    }
