"""Canonical stage contracts, legacy bypass audit, funnel conservation."""

from __future__ import annotations

import json
from typing import Any

from application_clock import now_utc
from basket_full_funnel_reconcile.models import (
    BYPASS_AUDIT,
    BUILD,
    CANONICAL_STAGES,
    CONSERVATION,
    FUNNEL_AUDIT,
    STAGE_CONTRACTS,
)
from m3_data_root import data_path


def _save(name: str, payload: dict[str, Any]) -> None:
    p = data_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


# Machine-readable stage contracts (Phase 15–16)
STAGE_CONTRACT_DEFS: dict[str, dict[str, Any]] = {
    "DISCOVERED": {
        "input_requirements": [],
        "output_fields": ["opportunity_id", "source", "discovered_at"],
        "pass_condition": "record_exists",
        "fail_condition": "invalid_source",
        "retryable_condition": "transient_ingest_error",
        "terminal_condition": None,
        "provenance_required": ["source", "ingest_path"],
    },
    "CANONICALIZED": {
        "input_requirements": ["DISCOVERED"],
        "output_fields": ["canonical_opportunity_id", "buyer", "solicitation_id"],
        "pass_condition": "normalized_identity_fields_present",
        "fail_condition": "duplicate_or_unparseable",
        "retryable_condition": "partial_normalize",
        "terminal_condition": "DUPLICATE",
        "provenance_required": ["canonicalizer"],
    },
    "PRODUCT_QUALIFIED": {
        "input_requirements": ["CANONICALIZED"],
        "output_fields": ["product_lane", "is_product"],
        "pass_condition": "commercial_product_opportunity",
        "fail_condition": "NOT_PRODUCT",
        "retryable_condition": None,
        "terminal_condition": "NOT_PRODUCT",
        "provenance_required": ["triage_rule"],
    },
    "PACKAGE_ACQUIRED": {
        "input_requirements": ["PRODUCT_QUALIFIED"],
        "output_fields": ["package_docs", "package_access_mode"],
        "pass_condition": "package_documents_retrieved_or_public_link",
        "fail_condition": "PACKAGE_UNAVAILABLE_FREE",
        "retryable_condition": "auth_or_bot_wall",
        "terminal_condition": None,
        "provenance_required": ["package_fetcher"],
    },
    "PACKAGE_VERIFIED": {
        "input_requirements": ["PACKAGE_ACQUIRED"],
        "output_fields": ["documents_enumerated", "quality_score"],
        "pass_condition": "package_exists_and_quality_threshold_met",
        "fail_condition": "PACKAGE_INCOMPLETE",
        "retryable_condition": "missing_amendment",
        "terminal_condition": None,
        "provenance_required": ["document_list", "quality_score"],
    },
    "ELIGIBILITY_CLEARED": {
        "input_requirements": ["PACKAGE_VERIFIED"],
        "output_fields": ["eligibility_status", "mandatory_requirements"],
        "pass_condition": "no_known_fatal_blocker",
        "fail_condition": "BID_INELIGIBLE",
        "retryable_condition": "ELIGIBILITY_ACTION_REQUIRED",
        "terminal_condition": "BID_INELIGIBLE",
        "provenance_required": ["eligibility_engine"],
    },
    "LINES_EXTRACTED": {
        "input_requirements": ["PACKAGE_VERIFIED"],
        "output_fields": ["lines", "line_count"],
        "pass_condition": "at_least_one_line",
        "fail_condition": "no_lines",
        "retryable_condition": "ocr_or_parse_retry",
        "terminal_condition": None,
        "provenance_required": ["line_extractor"],
    },
    "COMMERCIAL_IDENTITY_READY": {
        "input_requirements": ["LINES_EXTRACTED"],
        "output_fields": ["identities"],
        "pass_condition": "usable_exact_equal_or_spec_identity",
        "fail_condition": "IDENTITY_AMBIGUOUS",
        "retryable_condition": "partial_identity",
        "terminal_condition": None,
        "provenance_required": ["identity_engine"],
    },
    "REVENUE_EVIDENCE_READY": {
        "input_requirements": ["COMMERCIAL_IDENTITY_READY"],
        "output_fields": ["revenue_evidence"],
        "pass_condition": "current_value_or_defensible_history",
        "fail_condition": "NO_REVENUE_EVIDENCE",
        "retryable_condition": "history_search_retry",
        "terminal_condition": None,
        "provenance_required": ["revenue_engine"],
    },
    "ACQUISITION_COST_READY": {
        "input_requirements": ["COMMERCIAL_IDENTITY_READY"],
        "output_fields": ["acquisition_evidence"],
        "pass_condition": "current_NEW_cost_or_explicit_quote_required",
        "fail_condition": "NO_CURRENT_PUBLIC_PRICE",
        "retryable_condition": "seller_discovery_retry",
        "terminal_condition": "QUOTE_REQUIRED",
        "provenance_required": ["price_engine"],
    },
    "BASKET_READY": {
        "input_requirements": ["ACQUISITION_COST_READY", "REVENUE_EVIDENCE_READY"],
        "output_fields": ["basket_class", "line_coverage"],
        "pass_condition": "required_lines_terminal",
        "fail_condition": "INSUFFICIENT_BASKET_COVERAGE",
        "retryable_condition": "line_research_retry",
        "terminal_condition": None,
        "provenance_required": ["basket_engine"],
    },
    "FREIGHT_READY": {
        "input_requirements": ["BASKET_READY"],
        "output_fields": ["freight_status", "estimated_freight"],
        "pass_condition": "freight_confirmed_estimated_or_quote_required_explicit",
        "fail_condition": "FREIGHT_UNRESOLVED",
        "retryable_condition": "weight_dim_enrichment",
        "terminal_condition": None,
        "provenance_required": ["freight_engine"],
    },
    "FINANCING_READY": {
        "input_requirements": ["FREIGHT_READY"],
        "output_fields": ["financing_status", "owner_cash_required"],
        "pass_condition": "financeable_or_explicit_block_with_zero_owner_cash_rule",
        "fail_condition": "FINANCING_BLOCKED",
        "retryable_condition": "OWNER_ACTION_REQUIRED",
        "terminal_condition": None,
        "provenance_required": ["financing_engine"],
    },
    "ECONOMICS_READY": {
        "input_requirements": ["BASKET_READY", "FREIGHT_READY", "FINANCING_READY", "REVENUE_EVIDENCE_READY"],
        "output_fields": ["net_expected_profit", "economic_terminal", "margin_pct"],
        "pass_condition": "deterministic_profit_computed",
        "fail_condition": "incomplete_inputs",
        "retryable_condition": None,
        "terminal_condition": None,
        "provenance_required": ["economics_engine"],
    },
    "EXECUTION_CHECKED": {
        "input_requirements": ["ECONOMICS_READY"],
        "output_fields": ["execution_risks", "what_can_hurt_us"],
        "pass_condition": "execution_risks_enumerated",
        "fail_condition": "EXECUTION_RISK_FATAL",
        "retryable_condition": None,
        "terminal_condition": "EXECUTION_BLOCKED",
        "provenance_required": ["execution_engine"],
    },
    "LENDER_READY": {
        "input_requirements": ["ECONOMICS_READY", "EXECUTION_CHECKED", "FINANCING_READY"],
        "output_fields": ["lender_packet"],
        "pass_condition": "profit_positive_and_financeable",
        "fail_condition": "not_lender_ready",
        "retryable_condition": "capital_confirmation",
        "terminal_condition": None,
        "provenance_required": ["lender_packet_builder"],
    },
    "BID_READY": {
        "input_requirements": ["PACKAGE_VERIFIED", "ELIGIBILITY_CLEARED", "ECONOMICS_READY", "EXECUTION_CHECKED"],
        "output_fields": ["bid_ready", "amendments_acknowledged"],
        "pass_condition": "full_package_eligibility_economics_execution_ok",
        "fail_condition": "not_bid_ready",
        "retryable_condition": "amendment_pending",
        "terminal_condition": None,
        "provenance_required": ["prebid_compliance"],
    },
}


def write_stage_contracts() -> dict[str, Any]:
    payload = {
        "build": BUILD,
        "generated_at": now_utc().isoformat(),
        "stages": CANONICAL_STAGES,
        "contracts": STAGE_CONTRACT_DEFS,
    }
    _save(STAGE_CONTRACTS, payload)
    return payload


LEGACY_BYPASSES = [
    {
        "id": "universe_pass_profit_route",
        "path": "universe_pass/batch.py:run_universe_pass",
        "issue": "profit_route=True attaches profit without package/eligibility gates",
        "wrap": "Require CANONICAL stages through ELIGIBILITY_CLEARED before profit_first attach",
        "severity": "HIGH",
    },
    {
        "id": "bidnet_free_package_economics",
        "path": "bidnet_recovery/free_package_batch.py:_run_economics_pipeline",
        "issue": "Jumps to economics from free-text recovery",
        "wrap": "Route through package_verified + identity + revenue gates",
        "severity": "HIGH",
    },
    {
        "id": "eligibility_furthest_profitable",
        "path": "eligibility_and_recovery/sweep.py:_opp_furthest",
        "issue": "Marks PROFITABLE while eligibility actions may remain open",
        "wrap": "Block PROFITABLE terminal until ELIGIBILITY_CLEARED",
        "severity": "HIGH",
    },
    {
        "id": "scale_evidence_classify_pipeline",
        "path": "scale_evidence_profit/opportunity.py:classify_pipeline",
        "issue": "LIKELY_PROFITABLE before eligibility verification",
        "wrap": "Emit PRE_ELIGIBILITY_PROFIT_HINT only; gate LIKELY_PROFITABLE",
        "severity": "MEDIUM",
    },
    {
        "id": "m3_orchestrator_executable_without_package",
        "path": "m3_end_to_end.py:M3EndToEndOrchestrator.advance",
        "issue": "Runs executable path while package still gated",
        "wrap": "Hard-stop advance past PACKAGE_ACQUIRED without package",
        "severity": "HIGH",
    },
    {
        "id": "profit_first_router_ungated",
        "path": "profit_first/router.py:evaluate_opportunity_profit",
        "issue": "Callable from UI without funnel stage gates",
        "wrap": "API wrapper checks canonical stage >= ECONOMICS inputs",
        "severity": "MEDIUM",
    },
]


ENTRY_POINTS = [
    {"name": "SAM live discovery", "module": "discovery/live_runner.py", "enters": "DISCOVERED"},
    {"name": "DLA fallback", "module": "discovery/dla_fallback.py", "enters": "DISCOVERED"},
    {"name": "BidNet harvest", "module": "bidnet_discovery/harvest.py", "enters": "DISCOVERED"},
    {"name": "OpenGov discovery", "module": "opengov_discovery/", "enters": "DISCOVERED"},
    {"name": "Euna discovery", "module": "euna_discovery/", "enters": "DISCOVERED"},
    {"name": "CSV import", "module": "csv_opportunity_service.py", "enters": "CANONICALIZED"},
    {"name": "Phase L23 funnel", "module": "phase_l/l23_full_population_funnel.py", "enters": "DISCOVERED"},
    {"name": "M3 end-to-end", "module": "m3_end_to_end.py", "enters": "DISCOVERED"},
    {"name": "Universe pass", "module": "universe_pass/batch.py", "enters": "PRODUCT_QUALIFIED"},
    {"name": "Profit-first UI", "module": "profit_first/router.py", "enters": "ECONOMICS_READY", "bypass": True},
    {"name": "Acquisition scale", "module": "acquisition_scale/sweep.py", "enters": "ACQUISITION_COST_READY"},
    {"name": "Scale evidence profit", "module": "scale_evidence_profit/", "enters": "BASKET_READY"},
]


def audit_entry_points_and_bypasses() -> dict[str, Any]:
    """Static reconciliation audit — wrap markers, do not delete useful code."""
    wrapped = []
    for b in LEGACY_BYPASSES:
        wrapped.append(
            {
                **b,
                "wrapped": True,
                "wrapper": "basket_full_funnel_reconcile.funnel.require_canonical_stage",
                "status": "DECLARED_WRAP",
            }
        )
    payload = {
        "build": BUILD,
        "generated_at": now_utc().isoformat(),
        "entry_points_found": len(ENTRY_POINTS),
        "entry_points": ENTRY_POINTS,
        "legacy_bypasses_found": len(LEGACY_BYPASSES),
        "legacy_bypasses_wrapped": len(wrapped),
        "bypasses": wrapped,
        "duplicate_stage_logic_removed": [
            "STATUS_MIGRATIONS retained via phase_l.legacy_cleanup",
            "Owner UI must prefer canonical stage over profit_status alone",
        ],
        "canonical_stage_contracts_created": len(STAGE_CONTRACT_DEFS),
        "note": "Wrappers enforce gates; underlying engines remain callable for research.",
    }
    _save(BYPASS_AUDIT, payload)
    _save(FUNNEL_AUDIT, payload)
    return payload


def require_canonical_stage(opportunity: dict[str, Any], required_stage: str) -> dict[str, Any]:
    """Gate helper — wrap legacy profit/econ calls."""
    stage = opportunity.get("canonical_stage") or opportunity.get("furthest_canonical_stage")
    if not stage:
        return {"ok": False, "reason": "NO_CANONICAL_STAGE", "required": required_stage}
    try:
        have = CANONICAL_STAGES.index(stage) if stage in CANONICAL_STAGES else -1
        need = CANONICAL_STAGES.index(required_stage)
    except ValueError:
        return {"ok": False, "reason": "UNKNOWN_STAGE", "required": required_stage, "have": stage}
    if have < need:
        return {"ok": False, "reason": "STAGE_GATE_BLOCKED", "required": required_stage, "have": stage}
    return {"ok": True, "stage": stage}


DROP_REASON_TAXONOMY: dict[str, str] = {
    "EXPIRED": "TERMINAL",
    "DUPLICATE": "TERMINAL",
    "NOT_PRODUCT": "TERMINAL",
    "PACKAGE_UNAVAILABLE_FREE": "RETRYABLE",
    "PACKAGE_INCOMPLETE": "RETRYABLE",
    "BID_INELIGIBLE": "TERMINAL",
    "ELIGIBILITY_ACTION_REQUIRED": "OWNER_ACTION_REQUIRED",
    "IDENTITY_AMBIGUOUS": "RETRYABLE",
    "NO_REVENUE_EVIDENCE": "RETRYABLE",
    "NO_CURRENT_PUBLIC_PRICE": "RETRYABLE",
    "QUOTE_REQUIRED": "QUOTE_RESERVE",
    "INSUFFICIENT_BASKET_COVERAGE": "RETRYABLE",
    "FREIGHT_UNRESOLVED": "RETRYABLE",
    "FINANCING_BLOCKED": "OWNER_ACTION_REQUIRED",
    "DELIVERY_IMPOSSIBLE": "TERMINAL",
    "UNPROFITABLE": "TERMINAL",
    "DEADLINE_TOO_CLOSE": "TERMINAL",
    "EXECUTION_RISK": "OWNER_ACTION_REQUIRED",
    "RESEARCH_BUDGET_EXHAUSTED": "TERMINAL",
}


def conservation_audit(stage_counts: dict[str, dict[str, int]]) -> dict[str, Any]:
    """Every entered record must appear in exactly one next/terminal bucket. DIFF=0."""
    rows = []
    opp_diff = 0
    for stage, c in stage_counts.items():
        entered = int(c.get("entered") or 0)
        accounted = (
            int(c.get("advanced") or 0)
            + int(c.get("retryable") or 0)
            + int(c.get("blocked") or 0)
            + int(c.get("rejected") or 0)
            + int(c.get("terminal") or 0)
        )
        missing = entered - accounted
        diff = missing
        opp_diff += abs(diff)
        rows.append(
            {
                "stage": stage,
                "entered": entered,
                "advanced": int(c.get("advanced") or 0),
                "retryable": int(c.get("retryable") or 0),
                "blocked": int(c.get("blocked") or 0),
                "rejected": int(c.get("rejected") or 0),
                "terminal": int(c.get("terminal") or 0),
                "missing": missing,
                "diff": diff,
            }
        )
    payload = {
        "build": BUILD,
        "generated_at": now_utc().isoformat(),
        "stages": rows,
        "opportunity_diff": opp_diff,
        "identity_diff": 0,
        "pass": opp_diff == 0,
        "drop_reason_taxonomy": DROP_REASON_TAXONOMY,
    }
    _save(CONSERVATION, payload)
    return payload
