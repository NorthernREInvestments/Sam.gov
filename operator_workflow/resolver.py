"""Resolve existing M3 subsystem states into one conservative operator workflow projection.

Does not mutate input records. Does not change engines, scoring, or persistence.
"""

from __future__ import annotations

from typing import Any

from operator_workflow.constants import (
    BUILD_TAG,
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    CONFIDENCE_UNKNOWN,
    OW_AWARDED,
    OW_BID_PREPARATION,
    OW_DEEP_RESEARCH,
    OW_DELIVERED,
    OW_DISCOVERED,
    OW_FINANCE_REVIEW,
    OW_HISTORY,
    OW_ORDERING,
    OW_PAID,
    OW_QUALIFIED,
    OW_SUBMITTED,
    OW_SUPPLIER_VALIDATION,
    RANK_BID_PREP,
    RANK_DEEP,
    RANK_FINANCE,
    RANK_SUBMITTED,
    RANK_SUPPLIER,
    STATE_RANK,
)
from operator_workflow.mapping import (
    AWARD_LIFECYCLE_MAP,
    DEAL_LIFECYCLE_MAP,
    INVENTORY_STATE_MAP,
    M3_LIFECYCLE_MAP,
    MICRO_LAB_ECON_MAP,
    MICRO_LAB_STATE_MAP,
    OPERATOR_LIFECYCLE_MAP,
    PORTFOLIO_STATE_MAP,
    READINESS_LABEL_MAP,
    SRC_AWARD,
    SRC_DEAL_CRM,
    SRC_EVIDENCE_GATE,
    SRC_INVENTORY,
    SRC_M3_LIFECYCLE,
    SRC_MICRO_ECON,
    SRC_MICRO_LAB,
    SRC_OPERATOR,
    SRC_PORTFOLIO,
    SRC_READINESS,
    map_state,
)
from operator_workflow.models import empty_projection

_UNKNOWN_TOKENS = frozenset({"", "UNKNOWN", "NONE", "NULL", "N/A", "LEVEL_4_UNKNOWN", "VERIFY"})


def _u(v: Any) -> str:
    return str(v or "").strip().upper()


def _known(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, (list, dict)) and not v:
        return False
    return _u(v) not in _UNKNOWN_TOKENS


def _rank(state: str) -> int:
    return int(STATE_RANK.get(state, 0))


def _signal(source: str, raw: Any, mapped: str) -> dict[str, Any]:
    return {
        "source_system": source,
        "raw_state": raw,
        "mapped_operator_state": mapped,
        "rank": _rank(mapped),
    }


def _collect_signals(record: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Gather mapped signals from all known source fields on the record."""
    signals: list[dict[str, Any]] = []
    unmapped: list[str] = []

    def add(source: str, raw: Any, table: dict[str, str]) -> None:
        if raw is None or raw == "":
            return
        mapped = map_state(table, str(raw))
        if mapped is None:
            unmapped.append(f"UNMAPPED:{source}:{raw}")
            mapped = OW_DISCOVERED
        signals.append(_signal(source, raw, mapped))

    add(SRC_M3_LIFECYCLE, record.get("lifecycle") or record.get("m3_lifecycle"), M3_LIFECYCLE_MAP)
    add(
        SRC_PORTFOLIO,
        record.get("Deal_state")
        or record.get("portfolio_deal_state")
        or record.get("Status")
        or record.get("deal_state"),
        PORTFOLIO_STATE_MAP,
    )
    add(
        SRC_OPERATOR,
        record.get("operator_lifecycle") or record.get("operating_lifecycle"),
        OPERATOR_LIFECYCLE_MAP,
    )
    add(
        SRC_DEAL_CRM,
        record.get("deal_lifecycle") or record.get("crm_lifecycle") or record.get("pipeline_lifecycle"),
        DEAL_LIFECYCLE_MAP,
    )
    award_raw = record.get("award_lifecycle_status")
    if award_raw is None and isinstance(record.get("award_lifecycle"), dict):
        award_raw = record["award_lifecycle"].get("lifecycle_status")
    add(SRC_AWARD, award_raw, AWARD_LIFECYCLE_MAP)

    add(
        SRC_INVENTORY,
        record.get("inventory_stage") or record.get("inv_stage") or record.get("national_inventory_stage"),
        INVENTORY_STATE_MAP,
    )
    add(
        SRC_MICRO_LAB,
        record.get("research_state") or record.get("micro_lab_research_state"),
        MICRO_LAB_STATE_MAP,
    )
    micro_econ = record.get("micro_lab_economics_status")
    if micro_econ is None and isinstance(record.get("preliminary_economics"), dict):
        micro_econ = record["preliminary_economics"].get("status")
    add(SRC_MICRO_ECON, micro_econ, MICRO_LAB_ECON_MAP)

    # Readiness / pricing labels often shown as "Ready" in UI
    for key in (
        "Economics_readiness",
        "economics_readiness",
        "Research_readiness",
        "Commercial_readiness",
        "Funding",
        "funding_state",
        "pricing_confidence",
        "pricing_level",
    ):
        val = record.get(key)
        if isinstance(record.get("deal_economics"), dict) and key == "pricing_level":
            val = record["deal_economics"].get("pricing_level") or val
        if _known(val) or _u(val) in READINESS_LABEL_MAP:
            add(SRC_READINESS, val, READINESS_LABEL_MAP)

    # Nested portfolio snap
    snap = record.get("portfolio_snapshot") or record.get("economics_snapshot")
    if isinstance(snap, dict):
        add(SRC_PORTFOLIO, snap.get("Deal_state") or snap.get("Status"), PORTFOLIO_STATE_MAP)

    return signals, unmapped


def _economics_unknown(record: dict[str, Any]) -> bool:
    if record.get("economics_unknown") is True:
        return True
    pricing = None
    if isinstance(record.get("deal_economics"), dict):
        pricing = record["deal_economics"].get("pricing_level")
    pricing = (
        pricing
        or record.get("pricing_confidence")
        or record.get("pricing_level")
    )
    if isinstance(record.get("commercial_pricing"), dict) and not pricing:
        pricing = record["commercial_pricing"].get("confidence")
    if _u(pricing) in {"LEVEL_4_UNKNOWN", "UNKNOWN", "NONE"}:
        return True

    if record.get("economics_supported") and record.get("expected_actual_profit") is not None:
        return False
    if _known(record.get("Gross_spread")) or _known(record.get("gross_spread")):
        return False
    if _known(record.get("last_government_unit_price")) and _known(record.get("current_public_price")):
        return False

    econ = record.get("economics") or record.get("transaction_economics") or record.get("preliminary_economics")
    if isinstance(econ, dict):
        status = _u(econ.get("status") or econ.get("profit_status") or econ.get("Profit_Target_Status"))
        if status in {"UNKNOWN", "PARTIAL", "PARTIAL_ECONOMICS", "LEVEL_4_UNKNOWN"}:
            return True
        if econ.get("ok") is True and (
            _known(econ.get("estimated_revenue"))
            or _known(econ.get("gross_spread"))
            or _known(econ.get("expected_profit"))
        ):
            return False
        if not _known(econ.get("expected_profit") or econ.get("supported_profit") or econ.get("gross_spread")):
            return True

    # Claimed advanced econ/readiness without numbers
    if _u(record.get("Deal_state") or record.get("portfolio_deal_state") or "") in {
        "COMMERCIAL_VERIFICATION_WORTHY",
        "ECONOMICS_ESTABLISHED",
        "OWNER_REVIEW",
    }:
        return True
    if _u(record.get("lifecycle") or "") in {
        "ECONOMICS_ATTRACTIVE",
        "READY_FOR_OPERATOR_ACTION",
        "DRAFT_BID_READY",
    }:
        return True
    if record.get("Economics_readiness") or record.get("economics_readiness"):
        return True
    return False


def _supplier_unknown(record: dict[str, Any]) -> bool:
    if record.get("supplier_unknown") is True:
        return True
    if record.get("supplier_identified") is True:
        return False
    if record.get("supplier_identified") is False:
        return True
    if _known(record.get("current_market_seller")) or _known(record.get("supplier_name")):
        return False
    if _known(record.get("current_public_price")):
        return False
    quotes = record.get("supplier_quotes") or record.get("quotes")
    if isinstance(quotes, list) and quotes:
        return False
    commercial = record.get("commercial_pricing")
    if isinstance(commercial, dict) and _known(
        commercial.get("lowest_public_new_unit") or commercial.get("public_source")
    ):
        return False
    graph = record.get("supplier_product_graph")
    if isinstance(graph, dict) and graph.get("edges"):
        return False
    if _u(record.get("supply_status")) in {"UNKNOWN", "NO_SUPPLIER", "SUPPLIER_UNKNOWN"}:
        return True
    if _u(record.get("lifecycle")) in {"COMMERCIAL_VERIFICATION_REQUIRED", "PRICING_IN_PROGRESS"}:
        return True
    if _u(record.get("stop_reason")) == "QUOTE_REQUIRED":
        return True
    if _u(record.get("operator_readiness")) == "QUOTE_REQUIRED":
        return True
    # Default: not assumed missing (avoid false SUPPLIER_UNKNOWN on funding-only rows)
    return False


def _funding_unresolved(record: dict[str, Any]) -> bool:
    if record.get("funding_resolved") is True or record.get("funding_path_identified") is True:
        return False
    if record.get("funding_unresolved") is True:
        return True
    fund = _u(
        record.get("Funding")
        or record.get("funding_state")
        or record.get("operator_readiness")
        or ""
    )
    if "FUNDING" in fund and "REQUIRED" in fund:
        return True
    if fund in {"VERIFY", "UNKNOWN", "FUNDING_VERIFICATION_REQUIRED", "FUNDING_CALL_READY"}:
        return True
    if _u(record.get("lifecycle")) == "FUNDING_VERIFICATION_REQUIRED":
        return True
    if _u(record.get("Deal_state") or record.get("portfolio_deal_state")) == "FUNDING_VERIFICATION_REQUIRED":
        return True
    return False


def _apply_evidence_gates(
    candidate: str,
    record: dict[str, Any],
    *,
    post_award: bool,
    signals: list[dict[str, Any]] | None = None,
) -> tuple[str, list[str], list[str], list[dict[str, Any]]]:
    """Cap pre-award advancement when evidence is missing. Returns state, blockers, missing, gate signals."""
    blockers: list[str] = []
    missing: list[str] = []
    gate_signals: list[dict[str, Any]] = []
    if post_award:
        return candidate, blockers, missing, gate_signals

    # Terminal / history projections are not re-opened by evidence caps
    if candidate == OW_HISTORY:
        return candidate, blockers, missing, gate_signals

    state = candidate
    rank = _rank(state)
    sigs = signals or []
    claimed_advanced = any(
        s["rank"] >= RANK_BID_PREP and s["source_system"] != SRC_EVIDENCE_GATE for s in sigs
    )

    econ_unk = _economics_unknown(record)
    supplier_unk = _supplier_unknown(record)
    funding_unres = _funding_unresolved(record)

    if econ_unk and (rank >= RANK_BID_PREP or claimed_advanced or rank > RANK_DEEP):
        state = OW_DEEP_RESEARCH
        blockers.append("ECONOMICS_UNKNOWN")
        missing.append("usable_government_or_commercial_unit_economics")
        gate_signals.append(_signal(SRC_EVIDENCE_GATE, "ECONOMICS_UNKNOWN", OW_DEEP_RESEARCH))
        rank = _rank(state)
    elif econ_unk and claimed_advanced:
        blockers.append("ECONOMICS_UNKNOWN")
        missing.append("usable_government_or_commercial_unit_economics")

    if supplier_unk and (rank >= RANK_BID_PREP or claimed_advanced):
        state = OW_SUPPLIER_VALIDATION
        # Economics unknown is stricter — keep DEEP if already capped there
        if "ECONOMICS_UNKNOWN" in blockers:
            state = OW_DEEP_RESEARCH
        blockers.append("SUPPLIER_UNKNOWN")
        missing.append("supplier_identity_and_normalized_unit_cost")
        gate_signals.append(_signal(SRC_EVIDENCE_GATE, "SUPPLIER_UNKNOWN", state))
        rank = _rank(state)
    elif supplier_unk and rank > RANK_SUPPLIER and rank < RANK_SUBMITTED:
        if "ECONOMICS_UNKNOWN" not in blockers:
            state = OW_SUPPLIER_VALIDATION
            blockers.append("SUPPLIER_UNKNOWN")
            missing.append("supplier_identity_and_normalized_unit_cost")
            gate_signals.append(_signal(SRC_EVIDENCE_GATE, "SUPPLIER_UNKNOWN", OW_SUPPLIER_VALIDATION))
            rank = _rank(state)

    if funding_unres and rank >= RANK_BID_PREP and "ECONOMICS_UNKNOWN" not in blockers:
        state = OW_FINANCE_REVIEW
        blockers.append("FUNDING_UNRESOLVED")
        missing.append("financing_path_and_cash_requirement")
        gate_signals.append(_signal(SRC_EVIDENCE_GATE, "FUNDING_UNRESOLVED", OW_FINANCE_REVIEW))
        rank = _rank(state)
    elif funding_unres and RANK_FINANCE < rank < RANK_SUBMITTED and "ECONOMICS_UNKNOWN" not in blockers:
        state = OW_FINANCE_REVIEW
        blockers.append("FUNDING_UNRESOLVED")
        missing.append("financing_path_and_cash_requirement")
        gate_signals.append(_signal(SRC_EVIDENCE_GATE, "FUNDING_UNRESOLVED", OW_FINANCE_REVIEW))

    if econ_unk and _rank(state) > RANK_DEEP and _rank(state) < RANK_SUBMITTED:
        state = OW_DEEP_RESEARCH
        if "ECONOMICS_UNKNOWN" not in blockers:
            blockers.append("ECONOMICS_UNKNOWN")
        gate_signals.append(_signal(SRC_EVIDENCE_GATE, "ECONOMICS_UNKNOWN_CAP", OW_DEEP_RESEARCH))

    # Phase D — execution-critical unresolved requirements block READY FOR OWNER APPROVAL
    # (BID_PREPARATION+). Uses precomputed blockers only (deal-room / profile attach).
    # Does not run extraction inline — keeps projection cheap and scoring untouched.
    exec_blockers = record.get("execution_critical_blockers")
    if not isinstance(exec_blockers, list):
        exec_blockers = []
    if exec_blockers and rank >= RANK_BID_PREP and _rank(state) >= RANK_BID_PREP and _rank(state) < RANK_SUBMITTED:
        # Cap below BID_PREPARATION when execution-critical items remain
        if "SUPPLIER_UNKNOWN" in blockers:
            state = OW_SUPPLIER_VALIDATION
        elif "FUNDING_UNRESOLVED" in blockers:
            state = OW_FINANCE_REVIEW
        else:
            state = OW_DEEP_RESEARCH
        blockers.append("EXECUTION_CRITICAL_UNRESOLVED")
        missing.append("mandatory_execution_or_submission_requirements")
        gate_signals.append(_signal(SRC_EVIDENCE_GATE, "EXECUTION_CRITICAL_UNRESOLVED", state))
        rank = _rank(state)

    return state, list(dict.fromkeys(blockers)), list(dict.fromkeys(missing)), gate_signals


def _conservative_pick(signals: list[dict[str, Any]]) -> tuple[str, bool]:
    """Least-advanced non-empty mapping. Returns (state, used_only_history_terminal)."""
    if not signals:
        return OW_DISCOVERED, False
    # Prefer post-award signals if any at SUBMITTED+
    post = [s for s in signals if s["source_system"] == SRC_AWARD and s["rank"] >= RANK_SUBMITTED]
    if post:
        # Among award signals, use the award status (authoritative for post-submit)
        best = max(post, key=lambda s: s["rank"])
        return best["mapped_operator_state"], False

    ranks = [s["rank"] for s in signals]
    # HISTORY from rejection vs DISCOVERED: if ANY signal is active research, don't jump to HISTORY
    # unless ALL signals are HISTORY or only rejection
    non_history = [s for s in signals if s["mapped_operator_state"] != OW_HISTORY]
    if non_history:
        least = min(non_history, key=lambda s: s["rank"])
        return least["mapped_operator_state"], False
    least = min(signals, key=lambda s: s["rank"])
    return least["mapped_operator_state"], True


def _detect_conflicts(signals: list[dict[str, Any]], resolved: str) -> tuple[bool, list[str]]:
    if len(signals) < 2:
        return False, []
    states = sorted({s["mapped_operator_state"] for s in signals})
    raws = sorted({str(s["raw_state"]) for s in signals if s.get("raw_state") is not None})
    ranks = [_rank(s) for s in states]
    if max(ranks) - min(ranks) >= 1 and len(states) > 1:
        # Ready vs Verify style: readiness mapped to BID_PREP and funding to FINANCE
        ready_like = any(
            _u(s.get("raw_state")) in {"READY", "READY_FOR_OPERATOR_ACTION", "EXECUTION_READY", "DECISION_READY", "OWNER_REVIEW"}
            or s["mapped_operator_state"] == OW_BID_PREPARATION
            for s in signals
        )
        verify_like = any(
            _u(s.get("raw_state")) in {"VERIFY", "FUNDING_VERIFICATION_REQUIRED", "LEVEL_4_UNKNOWN"}
            or s["mapped_operator_state"] in {OW_FINANCE_REVIEW, OW_DEEP_RESEARCH, OW_SUPPLIER_VALIDATION}
            for s in signals
        )
        if ready_like and verify_like:
            return True, raws
        if max(ranks) - min(ranks) >= 2:
            return True, raws
        # Any disagreement with resolved
        if any(s["mapped_operator_state"] != resolved for s in signals):
            return True, raws
    return False, []


def _next_action(state: str, blockers: list[str], missing: list[str]) -> str:
    # Prefer specific execution-gate blockers (Phase F — VA-executable instructions)
    blocker_set = set(blockers or [])
    if "SUPPLIER_NOT_VALIDATED" in blocker_set or "QUOTE_NOT_EXECUTABLE" in blocker_set:
        return (
            "Obtain a formal written supplier quote for the exact part/NSN, quantity, and UOM; "
            "do not treat a public web price as an executable quote."
        )
    if "FINANCING_INCOMPATIBLE_OR_UNKNOWN" in blocker_set or "FUNDING_UNRESOLVED" in blocker_set:
        return (
            "Confirm a zero-owner-cash funding path with no personal guarantee before preparing a bid."
        )
    if "PACKAGING_UNRESOLVED" in blocker_set:
        return (
            "Confirm packaging method and per-unit packaging cost with supplier or packaging house "
            "before finalizing landed economics."
        )
    if "QUANTITY_UOM_UNCONFIRMED" in blocker_set or "PRODUCT_IDENTITY_UNCONFIRMED" in blocker_set:
        return (
            "Confirm exact product identity and normalize quantity/UOM (including HD=hundred if present) "
            "before requesting supplier pricing."
        )
    if "AMENDMENT_UNRESOLVED" in blocker_set:
        return (
            "Confirm the latest amendment supersedes quantity and deadline before requesting supplier quotes."
        )
    if "SUBMISSION_INCOMPLETE_OR_UNKNOWN" in blocker_set:
        return (
            "Complete mandatory submission items (acknowledgements, certifications, attachments) "
            "before owner approval."
        )
    if "ECONOMICS_INCOMPLETE_OR_UNKNOWN" in blocker_set or "ECONOMICS_UNKNOWN" in blockers:
        return (
            "Establish acquisition cost and expected profit from a formal quote before deciding whether to pursue."
        )
    if "SUPPLIER_UNKNOWN" in blockers:
        return (
            "Identify a contactable supplier for the exact product and pack size, then request a formal quote."
        )
    if "FUNDING_UNRESOLVED" in blockers:
        return "Confirm financing path and cash requirements before preparing a bid."
    if "EXECUTION_CRITICAL_UNRESOLVED" in blockers:
        return (
            "Resolve mandatory execution requirements (product, packaging, delivery, submission, "
            "supplier confirmations) before owner approval."
        )
    if state == OW_BID_PREPARATION:
        return "Prepare the bid package and confirm response method and deadline for owner review."
    return {
        OW_DISCOVERED: "Review whether this is a tangible product-resale opportunity worth screening.",
        OW_QUALIFIED: "Confirm product fit and open deeper research on identity, quantity, and history.",
        OW_DEEP_RESEARCH: "Complete product identity, quantity/UOM, and historical government pricing research.",
        OW_SUPPLIER_VALIDATION: "Validate supplier options, availability, and obtain a formal unit cost quote.",
        OW_FINANCE_REVIEW: "Confirm financing path and cash requirements with no personal-guarantee reliance.",
        OW_BID_PREPARATION: "Prepare the bid package and confirm response method and deadline.",
        OW_SUBMITTED: "Track award decision and respond to any clarifications.",
        OW_AWARDED: "Place supplier order after funding approval and confirm delivery plan.",
        OW_ORDERING: "Confirm supplier order, shipment, and government delivery.",
        OW_DELIVERED: "Confirm acceptance and invoice; monitor government payment.",
        OW_PAID: "Record final margin and close the deal into history.",
        OW_HISTORY: "Capture lessons learned for future sourcing and bidding.",
    }.get(state, "Review the opportunity and decide the next human step.")


def _reason(state: str, signals: list[dict[str, Any]], blockers: list[str], conflict: bool) -> str:
    parts = []
    if blockers:
        parts.append("Requires " + ", ".join(b.replace("_", " ").lower() for b in blockers))
    srcs = []
    for s in signals[:6]:
        srcs.append(f"{s['source_system']}={s['raw_state']}→{s['mapped_operator_state']}")
    if srcs:
        parts.append("Sources: " + "; ".join(srcs))
    if conflict:
        parts.append("Conflicting subsystem states resolved conservatively (least advanced)")
    if not parts:
        parts.append(f"Projected operator state {state}")
    return ". ".join(parts)


def _confidence(signals: list[dict[str, Any]], conflict: bool, blockers: list[str], unmapped: list[str]) -> str:
    if not signals:
        return CONFIDENCE_UNKNOWN
    if unmapped or conflict or len(blockers) >= 2:
        return CONFIDENCE_LOW
    if len(signals) == 1:
        return CONFIDENCE_MEDIUM
    # Agreement
    mapped = {s["mapped_operator_state"] for s in signals}
    if len(mapped) == 1 and not blockers:
        return CONFIDENCE_HIGH
    if len(mapped) <= 2 and not conflict:
        return CONFIDENCE_MEDIUM
    return CONFIDENCE_LOW


def _risk_flags(record: dict[str, Any], state: str, blockers: list[str]) -> list[str]:
    flags: list[str] = []
    for b in blockers:
        flags.append(b)
    if _u(record.get("product_resale_class") or record.get("product_classification")) in {
        "SERVICE",
        "CONSTRUCTION",
        "PERISHABLE",
        "LABOR_HEAVY",
    }:
        flags.append("NON_PRODUCT_RESALE_CLASS")
    if record.get("personal_guarantee_required") or record.get("personal_credit_required"):
        flags.append("PERSONAL_CREDIT_OR_PG_SIGNAL")
    if _u(record.get("Deal_state")) == "COMMERCIAL_VERIFICATION_WORTHY" and state != OW_BID_PREPARATION:
        flags.append("VERIFICATION_WORTHY_IS_NOT_BID_READY")
    if _u(record.get("research_state")) == "COMPLETE" and state != OW_BID_PREPARATION:
        flags.append("MICRO_LAB_COMPLETE_IS_NOT_BID_SUBMITTED")
    # dedupe
    return list(dict.fromkeys(flags))


def project_operator_workflow(record: dict[str, Any] | None) -> dict[str, Any]:
    """Project one opportunity/deal dict into the operator workflow view.

    Conservative: when systems disagree, choose the less advanced state.
    Evidence gates prevent BID_PREPARATION+ when economics/supplier/funding unknown
    (unless award lifecycle already shows SUBMITTED+).
    """
    out = empty_projection()
    out["build"] = BUILD_TAG
    record = record if isinstance(record, dict) else {}

    signals, unmapped = _collect_signals(record)
    out["missing_information"] = list(unmapped)

    # Award track?
    award_signals = [s for s in signals if s["source_system"] == SRC_AWARD and s["rank"] >= RANK_SUBMITTED]
    post_award = bool(award_signals)

    tentative, _ = _conservative_pick(signals)
    state, blockers, missing, gate_signals = _apply_evidence_gates(
        tentative, record, post_award=post_award, signals=signals
    )
    signals = signals + gate_signals
    out["missing_information"] = list(dict.fromkeys(out["missing_information"] + missing))

    # If gates produced a lower state than tentative, that is intentional conservatism
    if _rank(state) > _rank(tentative) and not post_award:
        # Should not advance beyond conservative pick
        state = tentative

    # Final conservatism: never exceed min(non-history signals) pre-award after gates
    if not post_award and signals:
        active = [s for s in signals if s["source_system"] != SRC_EVIDENCE_GATE]
        non_hist = [s for s in active if s["mapped_operator_state"] != OW_HISTORY]
        if non_hist:
            floor = min(non_hist, key=lambda s: s["rank"])["mapped_operator_state"]
            # Evidence gates may pull BELOW floor — that is OK
            # But never ABOVE the least advanced subsystem signal
            if _rank(state) > _rank(floor):
                state = floor

    conflict, conflicting = _detect_conflicts(
        [s for s in signals if s["source_system"] != SRC_EVIDENCE_GATE],
        state,
    )
    # Ready vs Verify explicit example
    raw_set = {_u(s.get("raw_state")) for s in signals}
    if ("READY" in raw_set or "READY_FOR_OPERATOR_ACTION" in raw_set or "DECISION_READY" in raw_set) and (
        "VERIFY" in raw_set or "FUNDING_VERIFICATION_REQUIRED" in raw_set or "LEVEL_4_UNKNOWN" in raw_set
    ):
        conflict = True
        conflicting = sorted(raw_set - {""})
        # Force verify-equivalent
        if not post_award and _rank(state) > RANK_FINANCE:
            if "LEVEL_4_UNKNOWN" in raw_set or "ECONOMICS_UNKNOWN" in blockers:
                state = OW_DEEP_RESEARCH
            else:
                state = OW_FINANCE_REVIEW
            if "FUNDING_UNRESOLVED" not in blockers and (
                "VERIFY" in raw_set or "FUNDING_VERIFICATION_REQUIRED" in raw_set
            ):
                blockers.append("FUNDING_UNRESOLVED")

    out["operator_workflow_state"] = state
    # Merge execution-critical blockers into operator blockers for VA-facing next actions
    exec_b = record.get("execution_critical_blockers") if isinstance(record.get("execution_critical_blockers"), list) else []
    merged_blockers = list(dict.fromkeys(list(blockers) + [str(x) for x in exec_b if x]))
    out["operator_blockers"] = merged_blockers
    out["operator_next_action"] = _next_action(state, merged_blockers, out["missing_information"])
    out["operator_state_reason"] = _reason(state, signals, out["operator_blockers"], conflict)
    out["state_conflict_detected"] = conflict
    out["conflicting_states"] = conflicting
    out["confidence_level"] = _confidence(signals, conflict, out["operator_blockers"], unmapped)
    out["risk_flags"] = _risk_flags(record, state, out["operator_blockers"])
    out["source_signals"] = signals
    return out


def map_single_source_state(source_system: str, raw_state: str) -> dict[str, Any]:
    """Test/helper: map one source state in isolation (no evidence gates)."""
    from operator_workflow.mapping import ALL_SOURCE_TABLES

    table = ALL_SOURCE_TABLES.get(source_system)
    if not table:
        return {"mapped": None, "error": "unknown_source_system"}
    mapped = map_state(table, raw_state)
    return {
        "source_system": source_system,
        "raw_state": raw_state,
        "mapped_operator_state": mapped or OW_DISCOVERED,
        "unmapped": mapped is None,
        "rank": _rank(mapped or OW_DISCOVERED),
    }
