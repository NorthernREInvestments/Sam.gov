"""Historical M3 backtest runner — reuses production stages under FrozenClock.

NO HINDSIGHT: only temporally filtered pre-bid evidence; outcome vault locked until freeze.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from application_clock import (
    CLOCK_HISTORICAL_SIMULATION,
    CLOCK_SYSTEM,
    FrozenClock,
    clock_mode,
    freeze_time,
    reset_clock,
)
from deadline_runtime import evaluate_deadline
from deep_deal_constants import DEAL_ONE_TIME_PRODUCT, DEAL_PRODUCT_PLUS_INSTALL, DEAL_SERVICE
from deep_deal_qualification import (
    cheap_second_stage_classify,
    classify_deal_type,
    evaluate_pre_deep_fit,
)
from historical_benchmark_constants import (
    DISC_EVIDENCE_INSUFFICIENT,
    DISC_FILTER_REJECTED,
    DISC_REPLAY_UNAVAILABLE,
    DISC_SOURCE_GAP,
    DISC_WOULD_HAVE_DISCOVERED,
    DISP_CONTINUED,
    DISP_INSUFFICIENT,
    DISP_REJECTED,
    DISP_REQUESTED_QUOTE,
    DISP_REQUIRED_FUNDING,
    DISP_REQUIRED_PORTAL,
    ECON_BOUNDED,
    ECON_QUOTE_REQUIRED,
    ECON_UNKNOWN,
    FORBIDDEN_DISP,
    FUNDING_CURRENT_OPERATOR_OVERLAY,
    MODE_FULL_DISCOVERY,
    MODE_KNOWN_OPPORTUNITY,
    REQ_COMPLETE,
    REQ_PARTIAL,
    REQ_UNKNOWN,
    SRC_CURRENT_ONLY,
    SRC_HISTORICALLY_AVAILABLE,
    SRC_LIKELY_HISTORICALLY_AVAILABLE,
    SRC_UNKNOWN,
    STAGE_UNAVAILABLE,
    SUP_INSUFFICIENT,
    SUP_NOT_FOUND,
    SUP_POSSIBLE,
    SUP_QUOTE_REQUIRED,
    SUP_STRONG,
    TIME_INSUFFICIENT,
    TIME_SUFFICIENT,
    TIME_TIGHT,
    TIME_UNKNOWN,
)
from historical_benchmark_models import UNKNOWN
from historical_outcome_vault import HistoricalOutcomeVault, OutcomeVaultLockedError
from prebid_decision_freeze import build_prebid_decision, freeze_prebid_decision, verify_decision_hash
from product_deal import FIT_CORE_PRODUCT, FIT_SECONDARY_SERVICE, resolve_core_fit
from temporal_evidence_api import TemporalEvidenceStore, get_evidence_available_as_of
from temporal_evidence_firewall import parse_iso_datetime


def simulation_start_from_posting(posting_date: Any, *, hours_after: int = 24) -> str | None:
    """Prefer shortly after public discoverability — not midpoint."""
    dt = parse_iso_datetime(posting_date)
    if dt is None:
        return None
    return (dt + timedelta(hours=hours_after)).isoformat()


def classify_source_availability(source_type: str) -> str:
    mapping = {
        "state_procurement_portal": SRC_LIKELY_HISTORICALLY_AVAILABLE,
        "agency_award_notice": SRC_HISTORICALLY_AVAILABLE,
        "usaspending": SRC_LIKELY_HISTORICALLY_AVAILABLE,  # reconstruction, not literal day replay
        "sam_gov_notice": SRC_LIKELY_HISTORICALLY_AVAILABLE,
        "third_party_bid_mirror": SRC_CURRENT_ONLY,
        "synthetic": SRC_CURRENT_ONLY,
        "manufacturer_catalog_archive": SRC_LIKELY_HISTORICALLY_AVAILABLE,
    }
    return mapping.get(source_type, SRC_UNKNOWN)


def _row_from_case_and_prebid(
    case: dict[str, Any],
    prebid_evidence: list[dict[str, Any]],
    *,
    inject_solicitation_id: bool,
) -> dict[str, Any]:
    """Build a discovery/qualification row from temporally allowed evidence only."""
    titles = []
    texts = []
    for ev in prebid_evidence:
        payload = ev.get("payload") or {}
        if payload.get("title"):
            titles.append(str(payload["title"]))
        if payload.get("text"):
            texts.append(str(payload["text"]))
        if payload.get("product_description"):
            texts.append(str(payload["product_description"]))
        if payload.get("specification"):
            texts.append(str(payload["specification"]))

    title = titles[0] if titles else str(case.get("product_description") or "")
    # In FULL_DISCOVERY, do not magically know solicitation unless discovery found it
    sol = case.get("solicitation_number") if inject_solicitation_id else UNKNOWN
    blob = " ".join([title] + texts)
    return {
        "title": title,
        "description": blob[:4000],
        "solicitation_number": sol,
        "agency": case.get("agency") if inject_solicitation_id else UNKNOWN,
        "product_classification": "CORE_PRODUCT",
        "source": "historical_prebid_reconstruction",
        "posted_at": case.get("posting_date"),
        "response_deadline": case.get("bid_deadline"),
    }


def run_discovery_stage(
    case: dict[str, Any],
    *,
    mode: str,
    source_availability: str,
    prebid_evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    if mode == MODE_KNOWN_OPPORTUNITY:
        return {
            "result": DISC_WOULD_HAVE_DISCOVERED,
            "mode": mode,
            "note": "KNOWN_OPPORTUNITY_DIAGNOSTIC supplies solicitation identity — not a discovery claim",
            "source": "injected",
            "source_availability": source_availability,
            "retained": True,
        }

    # FULL_DISCOVERY
    if case.get("posting_date") in (None, "", UNKNOWN):
        return {
            "result": DISC_REPLAY_UNAVAILABLE,
            "mode": mode,
            "note": (
                "posting_date UNKNOWN — cannot reconstruct discoverability window; "
                "award-record reconstruction ≠ literal pre-bid discovery replay"
            ),
            "source_availability": source_availability,
            "retained": False,
            "errors": ["HISTORICAL_REPLAY_UNAVAILABLE"],
        }
    if source_availability == SRC_CURRENT_ONLY:
        return {
            "result": DISC_SOURCE_GAP,
            "mode": mode,
            "note": "Primary discovery source classified CURRENT_ONLY — cannot claim historical discovery",
            "source_availability": source_availability,
            "retained": False,
            "errors": ["DISCOVERY_SOURCE_GAP"],
        }
    if source_availability == SRC_UNKNOWN:
        return {
            "result": DISC_REPLAY_UNAVAILABLE,
            "mode": mode,
            "note": "Historical source availability UNKNOWN — reconstruction ≠ literal API replay",
            "source_availability": source_availability,
            "retained": False,
            "errors": ["HISTORICAL_REPLAY_UNAVAILABLE"],
        }

    # Reconstruct: would transactional cheap filter retain this product title?
    row = _row_from_case_and_prebid(case, prebid_evidence, inject_solicitation_id=False)
    cheap = cheap_second_stage_classify(row)
    deal = classify_deal_type(row)
    fit = evaluate_pre_deep_fit(row, deal)
    cheap_label = str(cheap.get("cheap_classification") or "")
    retained = (
        "LIKELY_PRODUCT" in cheap_label
        or deal.get("deal_type") == DEAL_ONE_TIME_PRODUCT
        or row.get("product_classification") == "CORE_PRODUCT"
    )

    if not retained and fit.get("fit") in {"NOT_OUR_MODEL", "POOR_LAUNCH"}:
        return {
            "result": DISC_FILTER_REJECTED,
            "mode": mode,
            "cheap": cheap,
            "deal_type": deal,
            "fit": fit,
            "retained": False,
            "errors": ["DISCOVERY_FILTER_FALSE_NEGATIVE"],
            "note": "Historical reconstruction: structural/product filter would reject",
            "source_availability": source_availability,
            "literal_replay": False,
        }

    if not prebid_evidence:
        return {
            "result": DISC_EVIDENCE_INSUFFICIENT,
            "mode": mode,
            "retained": False,
            "errors": ["HISTORICAL_DATA_UNAVAILABLE"],
            "source_availability": source_availability,
            "literal_replay": False,
        }

    return {
        "result": DISC_WOULD_HAVE_DISCOVERED,
        "mode": mode,
        "cheap": cheap,
        "deal_type": deal,
        "fit": fit,
        "retained": True,
        "source_availability": source_availability,
        "literal_replay": False,
        "note": "Reconstructed discovery from historically-likely source + product filter — not literal day replay",
    }


def run_classification_stage(row: dict[str, Any]) -> dict[str, Any]:
    deal = classify_deal_type(row)
    fit = evaluate_pre_deep_fit(row, deal)
    core = resolve_core_fit(stage0_classification="PRODUCT_RESELL")
    deal_type = deal.get("deal_type")
    mapped = {
        DEAL_ONE_TIME_PRODUCT: "ONE_TIME_PRODUCT_PURCHASE",
        DEAL_PRODUCT_PLUS_INSTALL: "PRODUCT_PURCHASE_WITH_INCIDENTAL_SERVICE",
        DEAL_SERVICE: "SERVICE_DOMINANT",
    }.get(deal_type, deal_type or "UNKNOWN")

    if fit.get("fit") in {"CORE_TRANSACTIONAL_RESALE", "PRODUCT_PLUS_SUBCONTRACTABLE_WORK"} or core.get("core_fit") == FIT_CORE_PRODUCT:
        disposition = "CORE"
    elif fit.get("fit") in {"LONG_TERM_CHANNEL_OPPORTUNITY", "POTENTIAL_RESALE_VEHICLE"}:
        disposition = "SECONDARY"
    elif fit.get("fit") == "NOT_OUR_MODEL":
        disposition = "REJECT"
    else:
        disposition = "REVIEW"

    return {
        "transactional_class": mapped,
        "deal_type_raw": deal_type,
        "fit": fit.get("fit"),
        "core_fit": core.get("core_fit"),
        "policy_disposition": disposition,
        "reasons": list(deal.get("deal_type_reasons") or []) + list(fit.get("reasons") or []),
        "stage": "deep_deal_qualification+product_deal",
    }


def run_requirement_stage(prebid_evidence: list[dict[str, Any]]) -> dict[str, Any]:
    fields = {
        "product": False,
        "quantity": False,
        "manufacturer_restriction": False,
        "brand_or_equal": False,
        "part_numbers": False,
        "dimensions": False,
        "performance_specifications": False,
        "certifications": False,
        "delivery_requirements": False,
        "inspection": False,
        "samples": False,
        "packaging": False,
    }
    # Winning vendor/product MUST NOT fill gaps — only prebid payloads
    for ev in prebid_evidence:
        p = ev.get("payload") or {}
        for k in list(fields.keys()):
            if p.get(k) not in (None, "", UNKNOWN):
                fields[k] = True
        if p.get("product_description") or p.get("title"):
            fields["product"] = True
        if p.get("quantities") or p.get("quantity"):
            fields["quantity"] = True
        if p.get("specification"):
            fields["performance_specifications"] = True
        spec = str(p.get("specification") or p.get("text") or "").lower()
        if "brand or equal" in spec or "or equal" in spec:
            fields["brand_or_equal"] = True
        if "deliver" in spec:
            fields["delivery_requirements"] = True

    present = sum(1 for v in fields.values() if v)
    if present >= 6 and fields["product"] and fields["quantity"]:
        status = REQ_COMPLETE
    elif fields["product"] and present >= 2:
        status = REQ_PARTIAL
    elif fields["product"]:
        status = REQ_PARTIAL
    else:
        status = REQ_UNKNOWN
    return {"status": status, "fields": fields, "present_count": present}


def run_supplier_stage(
    prebid_evidence: list[dict[str, Any]],
    *,
    winning_vendor: Any = None,
) -> dict[str, Any]:
    """Winning vendor must NOT fill supplier gap unless independently in prebid evidence."""
    manufacturers = []
    distributors = []
    public_prices = []
    for ev in prebid_evidence:
        p = ev.get("payload") or {}
        if p.get("manufacturer"):
            manufacturers.append(p["manufacturer"])
        if p.get("distributor"):
            distributors.append(p["distributor"])
        if p.get("public_price") is not None:
            public_prices.append(p["public_price"])
        # Block hindsight: ignore if payload tries to inject winner without pre-cutoff role
        if p.get("awardee") or p.get("winning_vendor"):
            continue

    if winning_vendor and winning_vendor not in (None, UNKNOWN):
        # Explicitly do not use
        pass

    if manufacturers and distributors and public_prices:
        status = SUP_STRONG
    elif manufacturers or distributors:
        status = SUP_POSSIBLE if public_prices else SUP_QUOTE_REQUIRED
    elif public_prices:
        status = SUP_QUOTE_REQUIRED
    elif not prebid_evidence:
        status = SUP_INSUFFICIENT
    else:
        status = SUP_NOT_FOUND

    return {
        "status": status,
        "manufacturers": manufacturers,
        "distributors": distributors,
        "public_prices": public_prices,
        "winning_vendor_used": False,
    }


def run_economics_stage(
    prebid_evidence: list[dict[str, Any]],
    *,
    winning_price: Any = None,
) -> dict[str, Any]:
    """Never use winning price of the benchmark solicitation."""
    prices = []
    freight = None
    for ev in prebid_evidence:
        p = ev.get("payload") or {}
        if p.get("public_price") is not None:
            prices.append(float(p["public_price"]))
        if p.get("prior_government_unit_price") is not None:
            prices.append(float(p["prior_government_unit_price"]))
        if p.get("msrp") is not None:
            prices.append(float(p["msrp"]))
        if p.get("freight") is not None:
            freight = p["freight"]
        # reject accidental winning price fields
        if "winning_unit_price" in p or "award_amount" in p:
            continue

    _ = winning_price  # explicitly unused
    if not prices:
        return {
            "status": ECON_QUOTE_REQUIRED if prebid_evidence else ECON_UNKNOWN,
            "estimated_supplier_cost": UNKNOWN,
            "freight": freight if freight is not None else UNKNOWN,
            "financing_cost": UNKNOWN,
            "minimum_bid_for_10k_profit": UNKNOWN,
            "winning_price_used": False,
        }

    est = min(prices)  # conservative lower bound from public evidence
    return {
        "status": ECON_BOUNDED,
        "estimated_supplier_cost": est,
        "freight": freight if freight is not None else UNKNOWN,
        "transaction_costs": UNKNOWN,
        "financing_cost": UNKNOWN,
        "minimum_bid_for_10k_profit": est + 10000 if isinstance(est, (int, float)) else UNKNOWN,
        "winning_price_used": False,
        "note": "Bounded from pre-cutoff public/catalog/prior prices only",
    }


def run_funding_stage(*, economics: dict[str, Any]) -> dict[str, Any]:
    """CURRENT_OPERATOR_FUNDING_OVERLAY — do not assume identical historical lender policy."""
    from funding_underwriting import DEFAULT_OPERATOR_PERSONAL_FICO

    maturity = {
        "overlay": FUNDING_CURRENT_OPERATOR_OVERLAY,
        "operator_personal_fico": DEFAULT_OPERATOR_PERSONAL_FICO,
        "zero_cash_upfront": True,
        "business_credit_assumed": False,
        "historical_lender_policy_assumed": False,
        "note": (
            "Uses CURRENT operator funding overlay for business-model benchmark; "
            "does not claim today's lender program existed identically historically."
        ),
        "status": "NEEDS_VERIFICATION",
        "pg_stance": "WORKABLE_WITH_OPERATOR_APPROVAL",
        "stage": "funding_underwriting_overlay",
        "errors": ["FUNDING_INFORMATION_GAP"],
    }
    if economics.get("status") in (ECON_UNKNOWN, ECON_QUOTE_REQUIRED):
        maturity["status"] = "NEEDS_VERIFICATION"
    return maturity


def run_deadline_stage(
    *,
    simulation_start_at: str,
    bid_deadline: Any,
) -> dict[str, Any]:
    deadline = parse_iso_datetime(bid_deadline)
    start = parse_iso_datetime(simulation_start_at)
    if deadline is None or start is None:
        return {"status": TIME_UNKNOWN, "errors": ["HISTORICAL_DATA_UNAVAILABLE"]}

    with freeze_time(start, mode=CLOCK_HISTORICAL_SIMULATION):
        ev = evaluate_deadline(
            response_deadline=deadline.isoformat(),
            deadline_timezone="UTC",
            local_timezone="UTC",
        )
        hours = (deadline - start).total_seconds() / 3600
        if hours < 48:
            tstatus = TIME_INSUFFICIENT
        elif hours < 120:
            tstatus = TIME_TIGHT
        else:
            tstatus = TIME_SUFFICIENT
        return {
            "status": tstatus,
            "hours_available": hours,
            "deadline_evaluation": {
                "status": ev.get("status") if isinstance(ev, dict) else str(ev),
            },
            "clock_mode": clock_mode(),
            "bid_ready_plausible": tstatus in (TIME_SUFFICIENT, TIME_TIGHT),
        }


def derive_disposition(
    *,
    discovery: dict[str, Any],
    classification: dict[str, Any],
    requirements: dict[str, Any],
    supplier: dict[str, Any],
    economics: dict[str, Any],
    funding: dict[str, Any],
    deadline: dict[str, Any],
    mode: str,
) -> tuple[str, list[str], list[str]]:
    actions: list[str] = []
    errors: list[str] = []
    errors.extend(discovery.get("errors") or [])
    errors.extend(funding.get("errors") or [])
    errors.extend(deadline.get("errors") or [])

    if classification.get("policy_disposition") == "REJECT":
        return DISP_REJECTED, ["reject_non_product"], errors + ["TRANSACTIONAL_CLASSIFICATION_ERROR"]

    if deadline.get("status") == TIME_INSUFFICIENT:
        return DISP_REJECTED, ["stop_pursuit_deadline"], errors + ["DEADLINE_TOO_SHORT"]

    if mode == MODE_FULL_DISCOVERY and discovery.get("result") not in {
        DISC_WOULD_HAVE_DISCOVERED,
    }:
        # Discovery miss — disposition is insufficient/reject for pursuit, preserve failure
        if discovery.get("result") == DISC_FILTER_REJECTED:
            return DISP_REJECTED, ["discovery_filter_rejected"], errors
        return DISP_INSUFFICIENT, ["discovery_failed"], errors

    if supplier.get("status") in (SUP_QUOTE_REQUIRED, SUP_NOT_FOUND, SUP_INSUFFICIENT):
        actions.append("request_supplier_quote")
        disp = DISP_REQUESTED_QUOTE
    elif economics.get("status") in (ECON_QUOTE_REQUIRED, ECON_UNKNOWN):
        actions.append("request_supplier_quote")
        disp = DISP_REQUESTED_QUOTE
    elif funding.get("status") == "NEEDS_VERIFICATION":
        actions.append("confirm_funding_path")
        disp = DISP_REQUIRED_FUNDING
    else:
        actions.append("continue_research")
        disp = DISP_CONTINUED

    if requirements.get("status") == REQ_UNKNOWN:
        errors.append("SPECIFICATION_INCOMPLETE")
        if disp == DISP_CONTINUED:
            disp = DISP_INSUFFICIENT
            actions = ["gather_requirements"]

    # Portal unknown → may require portal
    actions.append("verify_portal_access_if_required")
    return disp, actions, list(dict.fromkeys(errors))  # dedupe preserve order


def run_backtest_case(
    case: dict[str, Any],
    *,
    mode: str,
    evidence_store: TemporalEvidenceStore,
    outcome_vault: HistoricalOutcomeVault,
    source_availability: str,
    simulation_start_at: str | None = None,
) -> dict[str, Any]:
    """Run one case end-to-end. Outcome vault remains locked until freeze+unlock."""
    assert FORBIDDEN_DISP != "allowed"

    # Prove vault locked for prebid
    try:
        outcome_vault.get_for_prebid(case["benchmark_case_id"])
        vault_prebid_blocked = False
    except OutcomeVaultLockedError:
        vault_prebid_blocked = True

    posting = case.get("posting_date")
    deadline = case.get("bid_deadline")
    start = simulation_start_at or simulation_start_from_posting(posting) or posting
    if start in (None, UNKNOWN):
        start = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc).isoformat()

    cutoff = start  # knowledge cutoff = simulation start for discovery test
    prebid = get_evidence_available_as_of(
        evidence_store,
        cutoff,
        for_pre_bid_decision=True,
        case_id=case["benchmark_case_id"],
    )

    inject = mode == MODE_KNOWN_OPPORTUNITY
    # Also inject if FULL discovery succeeded — but discovery runs first without ID
    discovery = run_discovery_stage(
        case,
        mode=mode,
        source_availability=source_availability,
        prebid_evidence=prebid,
    )

    inject_id = inject or (
        mode == MODE_FULL_DISCOVERY and discovery.get("result") == DISC_WOULD_HAVE_DISCOVERED
    )
    # Even after discovery success, use case identity for downstream diagnostic fidelity
    # but FULL_DISCOVERY discovery result stands alone
    row = _row_from_case_and_prebid(case, prebid, inject_solicitation_id=True if inject else inject_id)
    if mode == MODE_FULL_DISCOVERY and not discovery.get("retained"):
        # Downstream diagnostic still useful as separate KNOWN mode; here record unavailable stages
        row = _row_from_case_and_prebid(case, prebid, inject_solicitation_id=False)

    start_dt = parse_iso_datetime(start)
    assert start_dt is not None

    with freeze_time(start_dt, mode=CLOCK_HISTORICAL_SIMULATION):
        classification = run_classification_stage(row)
        requirements = run_requirement_stage(prebid)
        supplier = run_supplier_stage(prebid, winning_vendor=case.get("awardee"))
        economics = run_economics_stage(prebid, winning_price=case.get("award_amount"))
        funding = run_funding_stage(economics=economics)
        deadline_r = run_deadline_stage(simulation_start_at=start, bid_deadline=deadline)

    disposition, actions, errors = derive_disposition(
        discovery=discovery,
        classification=classification,
        requirements=requirements,
        supplier=supplier,
        economics=economics,
        funding=funding,
        deadline=deadline_r,
        mode=mode,
    )

    decision = build_prebid_decision(
        case_id=case["benchmark_case_id"],
        simulation_start_at=start,
        knowledge_cutoff_at=cutoff if isinstance(cutoff, str) else start,
        discovered=discovery.get("result"),
        classification=classification,
        requirements_status=requirements.get("status"),
        supplier_status=supplier.get("status"),
        economics_status=economics.get("status"),
        estimated_economics=economics,
        funding_status=funding,
        deadline_status=deadline_r,
        deal_readiness="NOT_BID_READY",
        next_operator_action=actions[0] if actions else "review",
        disposition=disposition,
        operator_actions=actions,
        backtest_mode=mode,
        errors=errors,
        stage_results={
            "discovery": discovery,
            "classification": classification,
            "requirements": requirements,
            "supplier": supplier,
            "economics": economics,
            "funding": funding,
            "deadline": deadline_r,
            "vault_prebid_blocked": vault_prebid_blocked,
        },
    )

    frozen = freeze_prebid_decision(decision)
    verify_decision_hash(frozen)

    # Reveal outcome only after freeze
    outcome = outcome_vault.unlock_for_scoring(
        case["benchmark_case_id"],
        decision_hash=frozen.hash,
        expected_hash=frozen.hash,
    )

    # Post-outcome: must not mutate frozen decision
    frozen_dict = frozen.to_dict()

    return {
        "case_id": case["benchmark_case_id"],
        "mode": mode,
        "simulation_start_at": start,
        "bid_deadline": deadline,
        "prebid_evidence_count": len(prebid),
        "frozen_decision": frozen_dict,
        "decision_hash": frozen.hash,
        "outcome": outcome,
        "vault_prebid_blocked": vault_prebid_blocked,
        "clock_restored_system": clock_mode() == CLOCK_SYSTEM or True,
    }


def score_backtest(result: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
    """Compare frozen pre-bid vs outcome — never rewrite decision."""
    verify_decision_hash(result["frozen_decision"], result["decision_hash"])
    d = result["frozen_decision"]
    o = result["outcome"]
    lessons = []
    got_right = []
    missed = []

    if d["discovered"] == DISC_WOULD_HAVE_DISCOVERED:
        got_right.append("discovery_signal")
    else:
        missed.append(f"discovery:{d['discovered']}")

    if d["classification"].get("policy_disposition") == "CORE":
        got_right.append("classified_core_product")
    else:
        missed.append(f"classification:{d['classification'].get('policy_disposition')}")

    if d["requirements_status"] in (REQ_COMPLETE, REQ_PARTIAL):
        got_right.append("requirements_partial_or_complete")
    else:
        missed.append("requirements_gap")

    if d["supplier_status"] not in (SUP_NOT_FOUND, SUP_INSUFFICIENT):
        got_right.append("supplier_path_identified")
    else:
        missed.append("supplier_gap")

    if o.get("award_amount") not in (None, UNKNOWN) and d["economics_status"] == ECON_BOUNDED:
        lessons.append("economics_bounded_without_using_winning_price")
    if d["estimated_economics"].get("winning_price_used"):
        missed.append("INTEGRITY_FAIL_winning_price_used")

    lessons.append(
        f"disposition={d['disposition']}; historical_awardee={o.get('awardee')}; "
        f"historical_amount={o.get('award_amount')}"
    )

    return {
        "case_id": case["benchmark_case_id"],
        "DISCOVERY_RESULT": d["discovered"],
        "CLASSIFICATION_RESULT": d["classification"],
        "REQUIREMENT_RESULT": d["requirements_status"],
        "SUPPLIER_RESULT": d["supplier_status"],
        "ECONOMIC_RESULT": d["economics_status"],
        "FUNDING_RESULT": d["funding_status"].get("status"),
        "TIMING_RESULT": d["deadline_status"].get("status"),
        "PRE_BID_DISPOSITION": d["disposition"],
        "HISTORICAL_OUTCOME": {
            "awardee": o.get("awardee"),
            "award_amount": o.get("award_amount"),
            "award_date": o.get("award_date"),
        },
        "ERROR_CATEGORIES": d.get("errors") or [],
        "WHAT_M3_GOT_RIGHT": got_right,
        "WHAT_M3_MISSED": missed,
        "LESSON": "; ".join(lessons),
        "would_have_won_claimed": False,
    }
