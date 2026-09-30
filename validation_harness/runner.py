"""Run golden cases through M3 projection surfaces and compare.

Coverage depths (Phase E.1):
- execution_path — solicitation text → execution requirements → owner gate → operator workflow
- broader_pipeline — frozen fixture through classification + identity + qty + supplier +
  economics + financing + execution + owner gate + workflow (still no live internet)
- adversarial_readiness — false-ready / false-reject probes
- positive_ready — curated executable deals expected READY=true

Do not label any of these as complete live M3 end-to-end coverage.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from typing import Any

from application_clock import now_utc
from validation_harness.case_loader import load_case, load_cases
from validation_harness.comparator import compare_case, extract_actuals
from validation_harness.models import SOURCE_TYPE_SYNTHETIC


def _build_row(case: dict[str, Any]) -> dict[str, Any]:
    row = {
        "canonical_id": case.get("case_id") or "CASE_UNKNOWN",
        "title": case.get("case_name") or "Validation Case",
        "description": case.get("source_fixture") or case.get("description") or "",
        "solicitation_text": case.get("source_fixture") or "",
        "label": case.get("source_type") or SOURCE_TYPE_SYNTHETIC,
        "synthetic_fixture": True,
        "source_type_label": case.get("source_type") or SOURCE_TYPE_SYNTHETIC,
        "validation_depth": case.get("validation_depth") or "execution_path",
    }
    overrides = case.get("row_overrides") or {}
    if isinstance(overrides, dict):
        row.update(overrides)
    if case.get("amendment_text"):
        row["amendment_text"] = case.get("amendment_text")
    return row


def run_m3_surfaces(case: dict[str, Any]) -> dict[str, Any]:
    """
    Execute M3 surfaces under test.
    Does not invent expected truth — only produces actuals.

    Broader-pipeline mode additionally materializes supplier_execution_state and
    uses the canonical enrich_deal_for_operator path so dashboard/deal-room agree.
    """
    row = _build_row(case)
    text = case.get("source_fixture") or row.get("description") or ""
    amendment_text = case.get("amendment_text")
    depth = str(case.get("validation_depth") or "execution_path")

    profile: dict[str, Any] = {}
    workflow: dict[str, Any] = {}
    enriched: dict[str, Any] = {}

    if depth in {"broader_pipeline", "positive_ready", "reality"}:
        try:
            from execution_requirements.enrichment import enrich_deal_for_operator

            enriched = enrich_deal_for_operator(
                row,
                text=text,
                amendment_text=amendment_text,
                include_summary=True,
                include_full_profile=True,
            )
            profile = enriched.get("execution_compliance") or {}
            workflow = {
                "operator_workflow_state": enriched.get("operator_workflow_state"),
                "operator_blockers": enriched.get("operator_blockers") or [],
                "operator_next_action": enriched.get("operator_next_action"),
            }
            row = enriched
        except Exception as e:
            profile = {
                "kind": "ExecutionComplianceProfile",
                "error": str(e),
                "requirements": [],
                "owner_approval_gate": {"ready_for_owner_approval": False, "blockers": ["PROFILE_ERROR"]},
            }
    else:
        try:
            from execution_requirements.profile import build_execution_compliance_profile

            profile = build_execution_compliance_profile(row, text=text, amendment_text=amendment_text)
        except Exception as e:
            profile = {
                "kind": "ExecutionComplianceProfile",
                "error": str(e),
                "requirements": [],
                "owner_approval_gate": {"ready_for_owner_approval": False, "blockers": ["PROFILE_ERROR"]},
            }

        row_for_wf = deepcopy(row)
        row_for_wf["execution_critical_blockers"] = list(profile.get("execution_critical_blockers") or [])
        try:
            from operator_workflow.resolver import project_operator_workflow

            workflow = project_operator_workflow(row_for_wf)
        except Exception as e:
            workflow = {"operator_workflow_state": "UNKNOWN", "error": str(e), "operator_blockers": []}
        row = row_for_wf

    actuals = extract_actuals(profile, workflow, row)
    return {
        "row": row,
        "profile": profile,
        "workflow": workflow,
        "enriched": enriched,
        "actuals": actuals,
        "validation_depth": depth,
    }


def run_case(case_or_id: str | dict[str, Any], *, run_id: str | None = None) -> dict[str, Any]:
    case = case_or_id if isinstance(case_or_id, dict) else load_case(case_or_id)
    surfaces = run_m3_surfaces(case)
    comparison = compare_case(case, surfaces["actuals"], run_id=run_id)
    return {
        "case_id": case.get("case_id"),
        "case_name": case.get("case_name"),
        "source_type": case.get("source_type"),
        "tags": case.get("tags") or [],
        "adversarial": bool(case.get("adversarial")),
        "validation_depth": surfaces.get("validation_depth") or case.get("validation_depth") or "execution_path",
        "actuals": surfaces["actuals"],
        "workflow_state": (surfaces["workflow"] or {}).get("operator_workflow_state"),
        "ready_for_owner_approval": (surfaces["actuals"].get("owner_readiness") or {}).get("ready_for_owner_approval"),
        "comparison": comparison,
        "status": comparison.get("status"),
        "gaps": comparison.get("gaps") or [],
    }


def _depth_breakdown(results: list[dict[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for r in results:
        depth = str(r.get("validation_depth") or "execution_path")
        if r.get("adversarial") and depth == "execution_path":
            depth = "adversarial_readiness"
        counts[depth] += 1
    return dict(counts)


def run_corpus(
    *,
    tags: list[str] | None = None,
    case_ids: list[str] | None = None,
    adversarial_only: bool = False,
    validation_depth: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    rid = run_id or now_utc().strftime("%Y%m%dT%H%M%SZ")
    cases = load_cases(tags=tags, case_ids=case_ids, adversarial_only=adversarial_only)
    if validation_depth:
        cases = [c for c in cases if str(c.get("validation_depth") or "execution_path") == validation_depth]
    results = [run_case(c, run_id=rid) for c in cases]
    passed = sum(1 for r in results if r.get("status") == "PASSED")
    partial = sum(1 for r in results if r.get("status") == "PARTIAL")
    failed = sum(1 for r in results if r.get("status") == "FAILED")
    all_gaps = []
    for r in results:
        all_gaps.extend(r.get("gaps") or [])
    depth = _depth_breakdown(results)
    return {
        "kind": "ValidationHarnessRun",
        "build": "20260922-m3-validation-harness-1",
        "run_id": rid,
        "started_note": (
            "Golden expected truth is curated — never generated by M3. "
            "This harness is execution-path / broader-pipeline / adversarial coverage — "
            "not complete live M3 end-to-end."
        ),
        "case_count": len(results),
        "passed": passed,
        "partial": partial,
        "failed": failed,
        "gap_count": len(all_gaps),
        "critical_gaps": sum(1 for g in all_gaps if g.get("severity") == "CRITICAL"),
        "depth_breakdown": {
            "execution_path_golden_cases": depth.get("execution_path", 0),
            "broader_pipeline_golden_cases": depth.get("broader_pipeline", 0),
            "adversarial_readiness_cases": depth.get("adversarial_readiness", 0),
            "positive_ready_cases": depth.get("positive_ready", 0),
            "reality_cases": depth.get("reality", 0),
        },
        "results": results,
        "gaps": all_gaps,
    }
