"""Phase L.10 — exact buyer-history search order (before category benchmarks)."""

from __future__ import annotations

from typing import Any

BUILD = "20260928-m3-phase-l10-exact-evidence-workflow"

# Deterministic search order — never skip ahead silently
BUYER_HISTORY_SEARCH_ORDER = (
    "same_buyer_exact_model",
    "same_buyer_exact_mpn_sku",
    "same_buyer_exact_product_family",
    "buyer_bid_tabs",
    "buyer_award_records",
    "buyer_board_council_approvals",
    "buyer_purchase_orders",
    "buyer_check_expenditure_registers",
    "buyer_prior_solicitations",
    "buyer_contract_renewals_extensions",
)

# After buyer-specific exhausted → broaden
PRODUCT_HISTORY_SEARCH_ORDER = (
    "exact_product_other_gov_buyer",
    "state_cooperative_price",
    "repeated_same_buyer_family",
    "strong_near_configuration",
    "strong_same_family_comparable",
    "category_benchmark_last_resort",
)

GOV_VALUE_TIER_ORDER = (
    "tier1_explicit_budget",
    "tier1_exact_current_contract",
    "tier1_exact_prior_award_same_buyer",
    "tier1_exact_bid_tab",
    "tier1_board_approved_purchase",
    "tier2_exact_model_other_gov",
    "tier2_state_coop_price",
    "tier2_repeated_buyer_family",
    "tier3_near_configuration",
    "tier3_same_family_comparable",
    "tier4_category_benchmark",
)


def _norm(s: Any) -> str:
    return str(s or "").strip().upper()


def buyer_memory_lookup(
    row: dict[str, Any],
    *,
    commercial: dict[str, Any] | None = None,
    buyer_memory: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Walk buyer-history order against persisted memory (exact paths first)."""
    commercial = commercial or {}
    buyer_memory = buyer_memory or {}
    buyer = _norm(row.get("agency") or row.get("buyer") or row.get("department"))
    model = _norm(commercial.get("model"))
    mpn = _norm(commercial.get("mpn") or commercial.get("sku"))
    family = _norm(commercial.get("product_family") or commercial.get("manufacturer"))
    attempts: list[dict[str, Any]] = []
    hit: dict[str, Any] | None = None

    entries = buyer_memory.get("entries") or buyer_memory.get("by_buyer") or buyer_memory
    if not isinstance(entries, dict):
        entries = {}

    def _scan(predicate, step: str) -> dict[str, Any] | None:
        nonlocal hit
        found = None
        for key, val in entries.items():
            if not isinstance(val, dict):
                continue
            b = _norm(val.get("buyer") or val.get("agency") or key.split("|")[0] if "|" in str(key) else key)
            if buyer and b and buyer[:20] not in b and b[:20] not in buyer:
                continue
            if predicate(val, key):
                found = {**val, "memory_key": key, "search_step": step}
                break
        attempts.append({"step": step, "hit": bool(found)})
        if found and hit is None:
            hit = found
        return found

    if model:
        _scan(lambda v, k: model in _norm(v.get("model") or k), "same_buyer_exact_model")
    if mpn and hit is None:
        _scan(lambda v, k: mpn in _norm(v.get("mpn") or v.get("sku") or k), "same_buyer_exact_mpn_sku")
    if family and hit is None:
        _scan(
            lambda v, k: family[:12] in _norm(v.get("product_family") or v.get("manufacturer") or k),
            "same_buyer_exact_product_family",
        )

    # Pattern slots (buyer retrieval memory)
    for step in BUYER_HISTORY_SEARCH_ORDER[3:]:
        attempts.append({"step": step, "hit": False, "note": "adapter_pending_or_no_memory"})

    return {
        "kind": "BuyerHistorySearch",
        "build": BUILD,
        "search_order": list(BUYER_HISTORY_SEARCH_ORDER),
        "attempts": attempts,
        "hit": hit,
        "exhausted": hit is None,
        "exhausted_reason": None if hit else "no_exact_buyer_history_found",
    }


def run_gov_value_upgrade_loop(
    row: dict[str, Any],
    *,
    commercial: dict[str, Any] | None = None,
    history: dict[str, Any] | None = None,
    buyer_memory: dict[str, Any] | None = None,
    stage3: dict[str, Any] | None = None,
    max_attempts: int = 12,
) -> dict[str, Any]:
    """Exact Gov D upgrade until A/B/C or evidence exhausted. No silent stop at benchmark."""
    from phase_l.evidence_recovery import recover_government_value
    from phase_l.quality_audit import GOV_VALUE_A, GOV_VALUE_B, GOV_VALUE_C, GOV_VALUE_D, grade_government_value

    commercial = commercial or {}
    attempts: list[dict[str, Any]] = []
    buyer = buyer_memory_lookup(row, commercial=commercial, buyer_memory=buyer_memory)
    attempts.append({"path": "buyer_history", "result": "hit" if buyer.get("hit") else "miss"})

    gov = recover_government_value(
        row, commercial=commercial, history=history, buyer_memory=buyer_memory, stage3=stage3
    )
    if buyer.get("hit"):
        h = buyer["hit"]
        uv = h.get("unit_value") or h.get("unit_price") or h.get("total_value")
        if uv:
            gov = {
                **gov,
                "state": "GOV_VALUE_EXACT",
                "tier": "A",
                "unit_value": float(uv),
                "source": "BUYER_PRICE_HISTORY_AVAILABLE",
                "match_rationale": f"same buyer {buyer['hit'].get('search_step')}",
                "recovered": True,
            }
            attempts.append({"path": "buyer_memory_apply", "result": "applied"})

    # Walk product-history order labels (deterministic audit)
    for step in PRODUCT_HISTORY_SEARCH_ORDER:
        if len(attempts) >= max_attempts:
            break
        graded = grade_government_value(gov, commercial=commercial, history=history)
        if graded["grade"] in {GOV_VALUE_A, GOV_VALUE_B, GOV_VALUE_C}:
            attempts.append({"path": step, "result": "skipped_already_upgraded", "grade": graded["grade"]})
            break
        if step == "category_benchmark_last_resort":
            attempts.append({"path": step, "result": "last_resort_applied_if_present"})
        else:
            attempts.append({"path": step, "result": "no_hit_continue"})

    graded = grade_government_value(gov, commercial=commercial, history=history)
    final = graded["grade"]
    exhausted = final == GOV_VALUE_D
    reason = None
    if exhausted:
        reason = "no_exact_history_found" if buyer.get("exhausted") else "category_benchmark_only"
    elif graded.get("upgraded_from_benchmark"):
        reason = "model_specific_band_promoted_to_C"

    return {
        "kind": "GovValueUpgradeLoop",
        "build": BUILD,
        "GOV_VALUE_UPGRADE_ATTEMPTS": attempts,
        "attempt_count": len(attempts),
        "buyer_history": buyer,
        "gov": gov,
        "grade_before": GOV_VALUE_D,
        "grade_after": final,
        "upgraded": final in {GOV_VALUE_A, GOV_VALUE_B, GOV_VALUE_C},
        "graded": graded,
        "EVIDENCE_EXHAUSTED": exhausted,
        "exhausted_reason": reason,
    }
