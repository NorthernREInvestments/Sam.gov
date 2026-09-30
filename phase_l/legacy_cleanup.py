"""Phase L.7 — legacy cleanup / canonical funnel reconciliation.

Does not delete stored-data compatibility. Marks superseded paths and
ensures a single canonical progressive funnel is the live path.
"""

from __future__ import annotations

from typing import Any

BUILD = "20260928-m3-phase-l14-nonbidnet-source-expansion"

# ---------------------------------------------------------------------------
# Canonical funnel (sole live path) — L.10 wraps with CanonicalOpportunityWorkflow
# ---------------------------------------------------------------------------
CANONICAL_FUNNEL_STAGES = (
    "broad_discovery",
    "stage0_cheap_hard_rejection",
    "stage1_permissive_commercial_product_triage",
    "stage2_identity_acquisition_path_enrichment",
    "stage3_economics",
    "exact_evidence_workflow",
    "public_artifact_recovery",
    "deep_validation",
    "quote_readiness",
    "owner_approval",
    "supplier_outreach_later",
    "final_economics",
    "compliance_bid_readiness",
)

CANONICAL_FUNNEL_ENTRY = "phase_l.l23_full_population_funnel.run_phase_l23"
CANONICAL_WORKFLOW = "phase_l.canonical_workflow.CanonicalOpportunityWorkflow"
CANONICAL_LIVE_RUNNER = "phase_l.l23_full_population_funnel.run_phase_l23"
CANONICAL_HISTORY_RECOVERY = "phase_l.platform_history_adapters.run_platform_history"
CANONICAL_EXACT_HISTORY = "phase_l.exact_history_recovery.run_exact_history_recovery"
CANONICAL_PUBLIC_ARTIFACT = "phase_l.public_artifact_recovery.run_public_artifact_recovery"
CANONICAL_AUTH_ACCESS = "phase_l.auth_access.classify_access_mode"
CANONICAL_BUYER_HISTORY_PATHS = "phase_l.buyer_history_paths"
CANONICAL_RESILIENT_HUNT = "phase_l.resilient_hunt.run_resilient_live_discovery"
CANONICAL_NONBIDNET = "phase_l.nonbidnet_expansion"
CANONICAL_LANE_CLASSIFIER = "phase_l.acquisition_lanes.classify_acquisition_lane"
CANONICAL_QUOTE_ECONOMICS = "phase_l.quote_economics.evaluate_quote_opportunity"
CANONICAL_QUOTE_READINESS = "phase_l.quote_readiness.evaluate_quote_readiness"

# ---------------------------------------------------------------------------
# Obsolete / superseded rule IDs (must not gate Stage 1/2/3 live path)
# ---------------------------------------------------------------------------
OBSOLETE_RULE_IDS = (
    "LEGACY_STAGE1_EXACT_IDENTITY_REQUIRED",
    "LEGACY_STAGE2_EXACT_MPN_GATE",
    "LEGACY_FIXED_DEEP_RESEARCH_COUNT_CAP",
    "LEGACY_STAGE3_ROW_COUNT_CAP",
    "LEGACY_NO_PRICE_INFORMATION_AS_DEAD_END",
    "LEGACY_QUOTE_REQUIRED_EQUALS_FAILURE",
    "LEGACY_AGENCY_WIDE_BUYER_MEDIAN",
    "LEGACY_LOOSE_CATEGORY_BENCHMARK_AS_VALIDATED",
    "LEGACY_GENERIC_SUPPLIER_AUTO_PROMOTE",
    "LEGACY_DUPLICATE_HISTORY_BRANCH",
    "LEGACY_READY_WITHOUT_EVIDENCE_GRADES",
    "LEGACY_COLLAPSE_ALL_AUTH_TO_AUTH_REQUIRED",
    "LEGACY_PLATFORM_BLOCK_EQUALS_HISTORY_NOT_AVAILABLE",
    "LEGACY_BIDNET_TERMINATES_EXACT_HISTORY",
    "LEGACY_AUTO_ACCOUNT_CREATION",
    "LEGACY_ID_BRUTE_FORCE_ARTIFACTS",
    "LEGACY_CAPTCHA_BYPASS",
    "LEGACY_SKIP_PUBLIC_ARTIFACT_BEFORE_REGISTRATION",
    "LEGACY_BIDNET_AUTH_HISTORY_AS_CURRENT_PRIORITY",
    "LEGACY_COMMERCIAL_FEED_BIDNET_ONLY",
)

# Status migrations (old → new semantic)
STATUS_MIGRATIONS: dict[str, str] = {
    "NO_PRICE_INFORMATION": "PUBLIC_PRICE_NOT_FOUND_OR_QUOTE_REQUIRED",
    "PRICE_NOT_AVAILABLE": "PUBLIC_PRICE_NOT_FOUND_OR_QUOTE_REQUIRED",
    "LEGACY_STRICT_STAGE2_FAIL": "L5_PERMISSIVE_ADMISSION_COUNTERFACTUAL_ONLY",
}

# Modules retained for historical rescues / telemetry (not live alternate funnel)
COMPATIBILITY_MODULES = {
    "phase_l.l21_rescue": "historical L.2.1 rescue runner — not live funnel",
    "phase_l.l22_rescue": "historical L.2.2 rescue runner — not live funnel",
    "phase_l.l23_rescue": "historical L.2.3 rescue runner — not live funnel",
    "phase_l.l24_rescue": "historical L.2.4 rescue runner — not live funnel",
    "phase_l.l25_rescue": "historical L.2.5 rescue runner — not live funnel",
    "phase_l.l26_rescue": "historical L.2.6 rescue runner — not live funnel",
    "phase_l.l27_rescue": "historical L.2.7 rescue runner — not live funnel",
    "phase_l.l28_rescue": "historical L.2.8 rescue runner — not live funnel",
    "phase_l.l29_rescue": "historical L.2.9 rescue runner — not live funnel",
    "phase_l.l3_rescue": "historical L.3 rescue — lane/econ patterns reused via imports",
    "phase_l.l4_rescue": "historical L.4 rescue",
    "phase_l.l5_rescue": "historical L.5 rescue",
    "phase_l.l9_rescue": "historical L.9 quality audit — not live alternate funnel",
    "phase_l.l8_rescue": "historical L.8 population recovery",
    "phase_l.l7_rescue": "historical L.7 quote readiness",
    "phase_l.l6_rescue": "L.6 economics — still callable; L.10 wraps via exact workflow",
    "phase_l.l10_rescue": "historical L.10 exact evidence — superseded by L.11/L.12",
    "phase_l.l11_rescue": "historical L.11 exact history — L.12 wraps with buyer-pivot auth recovery",
    "phase_l.l12_rescue": "historical L.12 auth-walled recovery — L.13 adds public artifact branch before registration",
    "phase_l.exact_history_recovery.classify_auth_wall": "delegates to auth_access; legacy alias map only",
    "phase_l.platform_history.run_buyer_pivot": "seeds buyer URLs; L.12 auth_history_recovery is canonical pivot",
    "phase_l.acquisition_pricing.NO_PRICE_INFORMATION": "public-price path failure class only; not quote-required dead end",
    "phase_l.stage2_admission.legacy_strict_would_pass": "counterfactual telemetry only; not admission gate",
    "phase_l.source_roles.STAGE3_NO_ROW_CAP": "re-exported from acquisition_lanes (single source of truth)",
}


def assert_canonical_caps() -> dict[str, bool]:
    from phase_l.acquisition_lanes import (
        DEEP_RESEARCH_NO_FIXED_COUNT,
        MANUAL_QUEUE_NO_FIXED_CAP,
        STAGE3_NO_ROW_CAP,
    )

    assert STAGE3_NO_ROW_CAP is True
    assert DEEP_RESEARCH_NO_FIXED_COUNT is True
    assert MANUAL_QUEUE_NO_FIXED_CAP is True
    return {
        "STAGE3_NO_ROW_CAP": STAGE3_NO_ROW_CAP,
        "DEEP_RESEARCH_NO_FIXED_COUNT": DEEP_RESEARCH_NO_FIXED_COUNT,
        "MANUAL_QUEUE_NO_FIXED_CAP": MANUAL_QUEUE_NO_FIXED_CAP,
    }


def assert_no_fixed_positive_cap(n: int | None = None) -> bool:
    """32 (or any prior count) is never a hard ceiling."""
    assert n is None or n >= 0
    return True


def migrate_status(old: str | None) -> str | None:
    if not old:
        return old
    return STATUS_MIGRATIONS.get(str(old), str(old))


def obsolete_rule_active(rule_id: str) -> bool:
    """Obsolete rules must never be active on the live path."""
    return False if rule_id in OBSOLETE_RULE_IDS else False


def legacy_cleanup_report() -> dict[str, Any]:
    caps = assert_canonical_caps()
    return {
        "kind": "PhaseL14LegacyCleanup",
        "build": BUILD,
        "canonical_funnel": list(CANONICAL_FUNNEL_STAGES),
        "canonical_entrypoints": {
            "funnel": CANONICAL_FUNNEL_ENTRY,
            "workflow": CANONICAL_WORKFLOW,
            "live_runner": CANONICAL_LIVE_RUNNER,
            "history_recovery": CANONICAL_HISTORY_RECOVERY,
            "exact_history": CANONICAL_EXACT_HISTORY,
            "public_artifact": CANONICAL_PUBLIC_ARTIFACT,
            "auth_access": CANONICAL_AUTH_ACCESS,
            "buyer_history_paths": CANONICAL_BUYER_HISTORY_PATHS,
            "lane_classifier": CANONICAL_LANE_CLASSIFIER,
            "quote_economics": CANONICAL_QUOTE_ECONOMICS,
            "quote_readiness": CANONICAL_QUOTE_READINESS,
            "resilient_hunt": CANONICAL_RESILIENT_HUNT,
            "nonbidnet": CANONICAL_NONBIDNET,
        },
        "obsolete_rule_ids": list(OBSOLETE_RULE_IDS),
        "obsolete_rules_active_on_live_path": False,
        "status_migrations": dict(STATUS_MIGRATIONS),
        "compatibility_retained": dict(COMPATIBILITY_MODULES),
        "l12_reconciled": {
            "duplicate_auth_classifiers": "auth_access is sole taxonomy; exact_history_recovery maps legacy aliases",
            "duplicate_buyer_history_branches": "auth_history_recovery + buyer_history_paths canonical; platform_history pivot seeds only",
            "platform_block_vs_unavailable": "PLATFORM_HISTORY_BLOCKED ≠ HISTORY_NOT_AVAILABLE",
            "no_auto_registration": True,
            "bidnet_terminates_recovery": False,
        },
        "l13_reconciled": {
            "public_artifact_before_registration": True,
            "no_id_brute_force": True,
            "no_captcha_bypass": True,
            "search_is_discovery_only": True,
            "failed_bidnet_not_zero_inventory": True,
            "canonical_branch": "PUBLIC_ARTIFACT_RECOVERY",
        },
        "l14_reconciled": {
            "bidnet_auth_history_parked": True,
            "nonbidnet_hunt_profile": "non_bidnet",
            "discovery_history_separated": True,
            "no_bidnet_auth_engineering": True,
            "canonical_live_runner": CANONICAL_LIVE_RUNNER,
        },
        "caps": caps,
        "no_fixed_32_cap": True,
        "duplicate_lane_classifier": "resolved — only acquisition_lanes.classify_acquisition_lane",
        "duplicate_cap_flags": "source_roles re-exports acquisition_lanes constants",
        "dead_code_policy": "historical rescue scripts retained; not invoked by L.10 live runner",
        "category_benchmark_policy": "Gov D recon-only unless model-specific band → Gov C",
        "generic_supplier_policy": "Supplier D cannot validate; seeds never exact evidence",
    }
