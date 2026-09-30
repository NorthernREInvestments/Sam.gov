"""BUILD 27 — Pursuit Readiness Engine (evidence-based opportunity assessment).

Decision support for: which opportunities deserve limited operator time, and why?

NOT: win probability, ranking, black-box score, autonomous bid decisions.
IS: transparent known / unknown / supports pursuit / blocks / next action.

Reuses Opportunity Operating, Supply Intelligence, Action Orchestration, Human OS.
Does not duplicate opportunity/supplier/product/task/evidence stores.
"""

from __future__ import annotations

import logging
from typing import Any

from application_clock import now_utc

log = logging.getLogger("govtracker.m3_pursuit_readiness")

BUILD_TAG = "20260919-m3-pursuit-readiness-1"

ST_KNOWN = "KNOWN"
ST_PARTIAL = "PARTIAL"
ST_UNKNOWN = "UNKNOWN"

STATUS_GREEN = "GREEN"
STATUS_YELLOW = "YELLOW"
STATUS_RED = "RED"

DIMENSIONS = (
    "product_fit",
    "supply_confidence",
    "economics_visibility",
    "competition_context",
    "compliance_execution_fit",
)


def _utc() -> str:
    return now_utc().isoformat()


def _as_dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _known(v: Any) -> bool:
    return v not in (None, "", "UNKNOWN")


def evidence_envelope(
    *,
    claim: str,
    evidence: Any = "UNKNOWN",
    source: Any = "UNKNOWN",
    date: Any = None,
    context: Any = "UNKNOWN",
) -> dict[str, Any]:
    ev_ok = _known(evidence) or isinstance(evidence, (list, dict))
    return {
        "claim": claim if _known(claim) else "UNKNOWN",
        "evidence": evidence if ev_ok else "UNKNOWN",
        "source": source if _known(source) else "UNKNOWN",
        "date": date or "UNKNOWN",
        "context": context if _known(context) else "UNKNOWN",
        "unsupported_conclusion": not ev_ok,
    }


def _status_from_state(state: str) -> str:
    if state == ST_KNOWN:
        return STATUS_GREEN
    if state == ST_PARTIAL:
        return STATUS_YELLOW
    return STATUS_RED


def _dimension(
    *,
    key: str,
    label: str,
    state: str,
    why: str,
    evidence: list[dict[str, Any]],
    unknowns: list[str],
    next_action: dict[str, Any],
    questions_answered: dict[str, Any] | None = None,
) -> dict[str, Any]:
    st = state if state in {ST_KNOWN, ST_PARTIAL, ST_UNKNOWN} else ST_UNKNOWN
    return {
        "key": key,
        "label": label,
        "state": st,
        "status": _status_from_state(st),
        "why": why,
        "evidence": evidence or [evidence_envelope(claim="No evidence recorded")],
        "unknowns": unknowns or [],
        "next_action": next_action,
        "questions": questions_answered or {},
        "not_a_prediction": True,
        "not_a_score": True,
    }


def _product_fit(row: dict[str, Any], deal: dict[str, Any] | None = None) -> dict[str, Any]:
    deal = deal or {}
    supply = _as_dict(_as_dict(deal.get("supply_intelligence")).get("opportunity_view"))
    derived = _as_dict(supply.get("derived_product"))
    req = _as_dict(deal.get("requirements"))
    nsn = _as_dict(_as_dict(_as_dict(row.get("dla_product_structure")).get("fields")).get("nsn")).get(
        "value"
    )
    product_name = derived.get("name") or row.get("title") or "UNKNOWN"
    manufacturer = derived.get("manufacturer") or row.get("manufacturer") or "UNKNOWN"
    pc = str(row.get("product_classification") or "").upper()
    has_bom = bool(row.get("bom") or row.get("line_items") or req.get("bom_lines"))
    has_nsn = _known(nsn)
    has_class = bool(pc and pc not in {"UNKNOWN", "NONE", ""})
    has_mfr = _known(manufacturer)
    has_spec = has_bom or has_nsn or _known(row.get("description"))

    evidence: list[dict[str, Any]] = []
    unknowns: list[str] = []
    if has_nsn:
        evidence.append(
            evidence_envelope(
                claim="NSN/product structure present",
                evidence=nsn,
                source="dla_product_structure",
                context=str(row.get("canonical_id") or ""),
            )
        )
    if has_class:
        evidence.append(
            evidence_envelope(
                claim="Product classification recorded",
                evidence=pc,
                source="opportunity_row",
                context=str(row.get("canonical_id") or ""),
            )
        )
    if has_bom:
        evidence.append(
            evidence_envelope(
                claim="BOM / line items present",
                evidence="bom_or_line_items",
                source="requirements",
                context=str(row.get("canonical_id") or ""),
            )
        )
    if has_mfr:
        evidence.append(
            evidence_envelope(
                claim="Manufacturer identified",
                evidence=manufacturer,
                source="supply_intelligence_or_row",
                context=str(row.get("canonical_id") or ""),
            )
        )

    translated = has_nsn or has_class or has_bom
    if not translated:
        unknowns.append("Government requirement not translated into commercial product")
    if not has_mfr:
        unknowns.append("Manufacturer not identified")
    if not has_spec:
        unknowns.append("Specifications not understood")

    if translated and has_mfr and has_spec:
        state = ST_KNOWN
        why = "Product identity, manufacturer, and specs have supporting evidence"
        next_what = "Confirm product match against solicitation"
    elif translated or has_mfr or has_spec:
        state = ST_PARTIAL
        why = "Some product-fit evidence exists; gaps remain"
        next_what = "Identify commercial product" if not translated else "Confirm manufacturer / specs"
    else:
        state = ST_UNKNOWN
        why = "Insufficient evidence to claim product fit"
        next_what = "Identify commercial product"

    return _dimension(
        key="product_fit",
        label="Product Fit",
        state=state,
        why=why,
        evidence=evidence,
        unknowns=unknowns,
        next_action={
            "what": next_what,
            "why": "Cannot pursue supply without product identity",
            "evidence_required": "NSN/part/spec/manufacturer evidence",
        },
        questions_answered={
            "requirement_translated_to_commercial_product": ST_KNOWN if translated else ST_UNKNOWN,
            "manufacturer_identified": ST_KNOWN if has_mfr else ST_UNKNOWN,
            "specifications_understood": ST_KNOWN if has_spec else ST_UNKNOWN,
        },
    )


def _supply_confidence(row: dict[str, Any], deal: dict[str, Any] | None = None) -> dict[str, Any]:
    deal = deal or {}
    supply_view = _as_dict(_as_dict(deal.get("supply_intelligence")).get("opportunity_view"))
    st = _as_dict(supply_view.get("supply_status"))
    try:
        if not st:
            from m3_supply_intelligence_read import derive_supply_from_row

            st = _as_dict(derive_supply_from_row(row).get("supply_status"))
            supply_view = derive_supply_from_row(row)
    except Exception:
        pass

    supplier_ok = bool(st.get("supplier_identified")) or bool(
        _as_dict(row.get("supplier_product_graph")).get("edges")
    )
    commercial_ok = bool(st.get("commercial_evidence_available"))
    paths = supply_view.get("stored_paths") or []
    suppliers = supply_view.get("derived_suppliers") or supply_view.get("stored_suppliers") or []
    unknowns = [u for u in (st.get("unknowns_remaining") or []) if "None listed" not in str(u)]

    evidence: list[dict[str, Any]] = []
    if supplier_ok:
        evidence.append(
            evidence_envelope(
                claim="Supplier / acquisition path evidenced",
                evidence=suppliers[:3] if suppliers else "supplier_identified",
                source="supply_intelligence",
                context=str(row.get("canonical_id") or ""),
            )
        )
    if paths:
        evidence.append(
            evidence_envelope(
                claim="Supply path records present",
                evidence=len(paths),
                source="supply_paths",
                context=str(row.get("canonical_id") or ""),
            )
        )
    if commercial_ok:
        evidence.append(
            evidence_envelope(
                claim="Commercial / quote evidence available",
                evidence="commercial_evidence",
                source="supply_intelligence",
                context=str(row.get("canonical_id") or ""),
            )
        )

    if not supplier_ok:
        unknowns = list(dict.fromkeys([*unknowns, "Supplier not identified", "Acquisition path unknown"]))
    if not commercial_ok:
        unknowns = list(dict.fromkeys([*unknowns, "Quotes / commercial terms not available"]))

    if supplier_ok and commercial_ok:
        state = ST_KNOWN
        why = "Acquisition path and commercial evidence are present"
        next_what = "Validate supplier capability against requirement"
    elif supplier_ok or commercial_ok or paths:
        state = ST_PARTIAL
        why = "Partial supply path; critical commercial or supplier gaps remain"
        next_what = "Request supplier pricing" if not commercial_ok else "Confirm supplier capability"
    else:
        state = ST_UNKNOWN
        why = "No realistic acquisition path evidenced"
        next_what = "Find supplier pathway"

    return _dimension(
        key="supply_confidence",
        label="Supply Confidence",
        state=state,
        why=why,
        evidence=evidence,
        unknowns=unknowns,
        next_action={
            "what": next_what,
            "why": "Supply uncertainty blocks pursuit commitment",
            "evidence_required": "Supplier capability confirmation and/or quote",
        },
        questions_answered={
            "realistic_acquisition_path": ST_KNOWN if supplier_ok else ST_UNKNOWN,
            "suppliers_identified": ST_KNOWN if supplier_ok else ST_UNKNOWN,
            "supplier_capability_evidenced": ST_PARTIAL if supplier_ok else ST_UNKNOWN,
            "quotes_or_commercial_terms": ST_KNOWN if commercial_ok else ST_UNKNOWN,
        },
    )


def _economics_visibility(row: dict[str, Any], deal: dict[str, Any] | None = None) -> dict[str, Any]:
    deal = deal or {}
    econ = _as_dict(deal.get("economics") or row.get("transaction_economics") or row.get("economics"))
    funding = _as_dict(deal.get("funding"))
    ci = _as_dict(deal.get("commercial_intelligence") or row.get("commercial_intelligence"))

    gov_price = econ.get("revenue") or ci.get("Government_Value") or row.get("estimated_value")
    acq = econ.get("acquisition") or econ.get("acquisition_evidence") or _as_dict(
        ci.get("Estimated_Acquisition")
    ).get("low")
    margin = econ.get("supported_profit") or econ.get("expected_profit") or ci.get("Margin_Potential")
    cash = (
        funding.get("capital_requirement")
        or econ.get("cash_requirement")
        or econ.get("capital_requirement")
        or econ.get("financing")
    )

    evidence: list[dict[str, Any]] = []
    unknowns: list[str] = []

    if _known(gov_price):
        evidence.append(
            evidence_envelope(
                claim="Government pricing / history visible",
                evidence=gov_price,
                source="economics_or_commercial_intelligence",
                context=str(row.get("canonical_id") or ""),
            )
        )
    else:
        unknowns.append("Government pricing/history unknown")

    if _known(acq):
        evidence.append(
            evidence_envelope(
                claim="Acquisition cost evidenced",
                evidence=acq,
                source="economics",
                context=str(row.get("canonical_id") or ""),
            )
        )
    else:
        unknowns.append("Acquisition cost unknown")

    # Only treat margin as known when acquisition AND revenue-like figures exist —
    # never invent profitability.
    margin_ok = _known(margin) and _known(gov_price) and _known(acq)
    if margin_ok:
        evidence.append(
            evidence_envelope(
                claim="Margin visibility supported by acquisition + government figures",
                evidence=margin,
                source="economics",
                context=str(row.get("canonical_id") or ""),
            )
        )
    else:
        unknowns.append("Margin visibility insufficient (unsupported profitability not calculated)")

    if _known(cash):
        evidence.append(
            evidence_envelope(
                claim="Cash requirements recorded",
                evidence=cash,
                source="funding|transaction_economics",
                context=str(row.get("canonical_id") or ""),
            )
        )
    else:
        unknowns.append("Cash requirements unknown")

    known_n = sum([_known(gov_price), _known(acq), margin_ok, _known(cash)])
    if known_n >= 3:
        state = ST_KNOWN
        why = "Pricing, cost, and cash context have supporting evidence"
        next_what = "Review economics with sources before decision"
    elif known_n >= 1:
        state = ST_PARTIAL
        why = "Some economic evidence; unknowns remain — no unsupported profit claimed"
        next_what = "Gather acquisition cost / quote evidence"
    else:
        state = ST_UNKNOWN
        why = "Economics not visible from evidence"
        next_what = "Collect government price history and acquisition evidence"

    dim = _dimension(
        key="economics_visibility",
        label="Economics Visibility",
        state=state,
        why=why,
        evidence=evidence,
        unknowns=unknowns,
        next_action={
            "what": next_what,
            "why": "Cannot allocate capital without evidenced economics",
            "evidence_required": "Quote + government price/history + cash need",
        },
        questions_answered={
            "government_pricing_history_known": ST_KNOWN if _known(gov_price) else ST_UNKNOWN,
            "acquisition_cost_known": ST_KNOWN if _known(acq) else ST_UNKNOWN,
            "margin_visibility_sufficient": ST_KNOWN if margin_ok else ST_UNKNOWN,
            "cash_requirements_understood": ST_KNOWN if _known(cash) else ST_UNKNOWN,
        },
    )

    # Capital Requirement Gate — refine visibility; never force GREEN/RED from theoretical paths
    try:
        from m3_capital_requirement_gate_read import (
            build_capital_requirement_assessment,
            enrich_economics_visibility_with_capital,
        )

        cap = build_capital_requirement_assessment(row, deal=deal, ensure_actions=False)
        dim = enrich_economics_visibility_with_capital(dim, cap)
    except Exception as e:
        log.debug("capital gate enrich skipped: %s", e)
    return dim


def _competition_context(row: dict[str, Any], deal: dict[str, Any] | None = None) -> dict[str, Any]:
    deal = deal or {}
    ci = _as_dict(deal.get("competitive_intelligence") or deal.get("commercial_intelligence") or {})
    if not ci:
        ci = _as_dict(row.get("commercial_intelligence") or row.get("competitive_intelligence"))
    winners = ci.get("Historical_Winners") or row.get("historical_winners") or []
    if isinstance(winners, str):
        winners = [winners] if _known(winners) else []
    prev = ci.get("Previous_Suppliers") or ci.get("Known_Manufacturer") or row.get("previous_suppliers")
    market = ci.get("Market_Context") or ci.get("Supply_Confidence") or row.get("market_context")

    evidence: list[dict[str, Any]] = []
    unknowns: list[str] = []
    if winners:
        evidence.append(
            evidence_envelope(
                claim="Historical awards / winners available",
                evidence=winners[:5],
                source="competitive_or_commercial_intelligence",
                context=str(row.get("canonical_id") or ""),
            )
        )
    else:
        unknowns.append("Historical awards not available")
    if _known(prev):
        evidence.append(
            evidence_envelope(
                claim="Previous suppliers / manufacturers visible",
                evidence=prev,
                source="competitive_or_commercial_intelligence",
                context=str(row.get("canonical_id") or ""),
            )
        )
    else:
        unknowns.append("Previous suppliers not visible")
    if _known(market):
        evidence.append(
            evidence_envelope(
                claim="Market context recorded",
                evidence=market,
                source="commercial_intelligence",
                context=str(row.get("canonical_id") or ""),
            )
        )
    else:
        unknowns.append("Market context unknown")

    if winners and _known(prev):
        state = ST_KNOWN
        why = "Award history and prior supplier visibility exist (not a win prediction)"
        next_what = "Review historical award evidence for context"
    elif winners or _known(prev) or _known(market):
        state = ST_PARTIAL
        why = "Partial competition context; do not treat as win likelihood"
        next_what = "Gather historical award / supplier evidence"
    else:
        state = ST_UNKNOWN
        why = "No competition context evidenced"
        next_what = "Search historical awards for this requirement"

    return _dimension(
        key="competition_context",
        label="Competition Context",
        state=state,
        why=why,
        evidence=evidence,
        unknowns=unknowns,
        next_action={
            "what": next_what,
            "why": "Context only — M3 does not predict winners",
            "evidence_required": "Historical award records",
        },
        questions_answered={
            "historical_awards_available": ST_KNOWN if winners else ST_UNKNOWN,
            "previous_suppliers_visible": ST_KNOWN if _known(prev) else ST_UNKNOWN,
            "market_context_known": ST_KNOWN if _known(market) else ST_UNKNOWN,
            "win_prediction": "NOT_PROVIDED",
        },
    )


def _compliance_execution_fit(row: dict[str, Any], deal: dict[str, Any] | None = None) -> dict[str, Any]:
    deal = deal or {}
    compliance = _as_dict(deal.get("compliance") or deal.get("bid_compliance") or row.get("compliance"))
    req = _as_dict(deal.get("requirements"))
    docs = row.get("documents") or row.get("governing_documents") or req.get("package_access")
    restrictions = (
        compliance.get("restrictions")
        or row.get("unusual_restrictions")
        or row.get("set_aside")
        or compliance.get("set_aside")
    )
    delivery = row.get("delivery_issues") or compliance.get("delivery") or row.get("fob")
    missing = []
    if isinstance(compliance, dict):
        missing = list(compliance.get("missing") or compliance.get("missing_evidence") or [])
    missing.extend(list(req.get("missing_information") or []))

    understanding = bool(docs) or bool(row.get("requirements_parsed")) or (
        _known(row.get("description")) and len(str(row.get("description") or "")) > 40
    )
    evidence: list[dict[str, Any]] = []
    unknowns: list[str] = []

    if understanding:
        evidence.append(
            evidence_envelope(
                claim="Requirements / package understanding present",
                evidence="documents_or_description",
                source="opportunity_row",
                context=str(row.get("canonical_id") or ""),
            )
        )
    else:
        unknowns.append("Requirements not understood")

    if _known(delivery):
        evidence.append(
            evidence_envelope(
                claim="Delivery terms recorded",
                evidence=delivery,
                source="compliance_or_row",
                context=str(row.get("canonical_id") or ""),
            )
        )
    else:
        unknowns.append("Delivery issues / terms unknown")

    if _known(restrictions):
        evidence.append(
            evidence_envelope(
                claim="Restrictions / set-aside noted",
                evidence=restrictions,
                source="compliance_or_row",
                context=str(row.get("canonical_id") or ""),
            )
        )
    else:
        unknowns.append("Unusual restrictions unknown")

    if missing:
        unknowns.extend([str(m) for m in missing[:6]])

    # Small-operator realism: only claim when requirements understood and no hard missing evidence.
    can_execute = understanding and not missing
    if can_execute:
        evidence.append(
            evidence_envelope(
                claim="No unresolved compliance gaps listed — operator must still judge capacity",
                evidence="no_listed_missing_compliance",
                source="compliance",
                context=str(row.get("canonical_id") or ""),
            )
        )
    else:
        unknowns.append("Small-operator execution fit not evidenced")

    if understanding and can_execute and _known(delivery):
        state = ST_KNOWN
        why = "Requirements and delivery context evidenced; human still judges capacity"
        next_what = "Confirm operator can meet delivery/compliance"
    elif understanding or _known(restrictions) or _known(delivery):
        state = ST_PARTIAL
        why = "Partial compliance/execution evidence; gaps remain"
        next_what = "Review solicitation requirements"
    else:
        state = ST_UNKNOWN
        why = "Compliance / execution fit not evidenced"
        next_what = "Review solicitation requirements"

    return _dimension(
        key="compliance_execution_fit",
        label="Compliance / Execution Fit",
        state=state,
        why=why,
        evidence=evidence,
        unknowns=unknowns,
        next_action={
            "what": next_what,
            "why": "Cannot bid without understanding requirements and constraints",
            "evidence_required": "Solicitation package notes + restriction evidence",
        },
        questions_answered={
            "requirements_understood": ST_KNOWN if understanding else ST_UNKNOWN,
            "delivery_issues_known": ST_KNOWN if _known(delivery) else ST_UNKNOWN,
            "unusual_restrictions_known": ST_KNOWN if _known(restrictions) else ST_UNKNOWN,
            "small_operator_can_execute": ST_KNOWN if can_execute else ST_UNKNOWN,
        },
    )


def _overall(dims: dict[str, dict[str, Any]]) -> dict[str, Any]:
    reasons_continue: list[str] = []
    blockers: list[str] = []
    for key in DIMENSIONS:
        d = dims.get(key) or {}
        label = d.get("label") or key
        if d.get("state") == ST_KNOWN:
            reasons_continue.append(f"{label}: {d.get('why')}")
        elif d.get("state") == ST_PARTIAL:
            for u in (d.get("unknowns") or [])[:2]:
                blockers.append(f"{label}: {u}")
        else:
            blockers.append(f"{label}: {d.get('why') or 'UNKNOWN'}")
            for u in (d.get("unknowns") or [])[:1]:
                blockers.append(f"{label} unknown: {u}")

    # Next human action: first RED/UNKNOWN dimension, else first PARTIAL, else review decision.
    next_action = {
        "what": "Review pursuit decision with evidence",
        "why": "Enough context may exist — humans decide",
        "evidence_required": "Document decision with sources reviewed",
    }
    for prefer in (ST_UNKNOWN, ST_PARTIAL):
        for key in DIMENSIONS:
            d = dims.get(key) or {}
            if d.get("state") == prefer:
                next_action = dict(d.get("next_action") or next_action)
                break
        else:
            continue
        break

    return {
        "not_a_score": True,
        "not_a_ranking": True,
        "not_win_probability": True,
        "strongest_reasons_to_continue": reasons_continue[:8] or ["UNKNOWN — insufficient supporting evidence"],
        "biggest_blockers": blockers[:10] or [],
        "recommended_next_human_action": next_action,
        "summary": (
            f"{len(reasons_continue)} dimension(s) supported by evidence; "
            f"{len(blockers)} blocker(s)/gap(s) listed"
        ),
        "operator_question": "Where should I spend my next hour?",
    }


# Action templates for missing critical items → Action Orchestration
_MISSING_ACTION_TEMPLATES: list[tuple[str, dict[str, Any]]] = [
    (
        "manufacturer",
        {
            "title": "Identify commercial product",
            "action_type": "RESEARCH",
            "why": "Manufacturer / commercial product match missing",
            "evidence_requirements": ["NSN/part/manufacturer evidence"],
        },
    ),
    (
        "commercial product",
        {
            "title": "Identify commercial product",
            "action_type": "RESEARCH",
            "why": "Government requirement not translated into commercial product",
            "evidence_requirements": ["Product match evidence"],
        },
    ),
    (
        "product",
        {
            "title": "Identify commercial product",
            "action_type": "RESEARCH",
            "why": "Product identity incomplete",
            "evidence_requirements": ["Product identity evidence"],
        },
    ),
    (
        "supplier",
        {
            "title": "Find supplier pathway",
            "action_type": "RESEARCH",
            "why": "Supplier / acquisition path missing",
            "evidence_requirements": ["Supplier capability confirmation"],
        },
    ),
    (
        "acquisition path",
        {
            "title": "Find supplier pathway",
            "action_type": "RESEARCH",
            "why": "Acquisition path unknown",
            "evidence_requirements": ["Supply path evidence"],
        },
    ),
    (
        "quote",
        {
            "title": "Request supplier pricing",
            "action_type": "COMMUNICATION",
            "why": "Supplier quote / commercial terms missing",
            "evidence_requirements": ["Supplier Quote"],
        },
    ),
    (
        "pricing",
        {
            "title": "Request supplier pricing",
            "action_type": "COMMUNICATION",
            "why": "Commercial pricing missing",
            "evidence_requirements": ["Supplier Quote"],
        },
    ),
    (
        "acquisition cost",
        {
            "title": "Request supplier pricing",
            "action_type": "RESEARCH",
            "why": "Acquisition cost unknown",
            "evidence_requirements": ["Quote or catalog price evidence"],
        },
    ),
    (
        "requirement",
        {
            "title": "Review solicitation requirements",
            "action_type": "DOCUMENTATION",
            "why": "Compliance / requirements information missing",
            "evidence_requirements": ["Solicitation package notes"],
        },
    ),
    (
        "compliance",
        {
            "title": "Review solicitation requirements",
            "action_type": "DOCUMENTATION",
            "why": "Compliance information missing",
            "evidence_requirements": ["Compliance evidence"],
        },
    ),
    (
        "restriction",
        {
            "title": "Review solicitation requirements",
            "action_type": "DOCUMENTATION",
            "why": "Restrictions not evidenced",
            "evidence_requirements": ["Set-aside / restriction clause evidence"],
        },
    ),
    (
        "historical award",
        {
            "title": "Gather historical award evidence",
            "action_type": "RESEARCH",
            "why": "Competition context incomplete",
            "evidence_requirements": ["Historical award records"],
        },
    ),
    (
        "cash",
        {
            "title": "Document cash requirements",
            "action_type": "RESEARCH",
            "why": "Cash requirements unknown",
            "evidence_requirements": ["Capital / cash need evidence"],
        },
    ),
    (
        "deposit",
        {
            "title": "Determine required deposit",
            "action_type": "RESEARCH",
            "why": "Deposit / prepayment unknown",
            "evidence_requirements": ["Supplier deposit terms evidence"],
        },
    ),
    (
        "payment terms",
        {
            "title": "Confirm supplier payment terms",
            "action_type": "RESEARCH",
            "why": "Supplier payment timing unknown",
            "evidence_requirements": ["Supplier payment terms evidence"],
        },
    ),
    (
        "payment timing",
        {
            "title": "Confirm supplier payment terms",
            "action_type": "RESEARCH",
            "why": "Supplier payment timing unknown",
            "evidence_requirements": ["Supplier payment terms evidence"],
        },
    ),
    (
        "funding",
        {
            "title": "Verify funding path conditions",
            "action_type": "RESEARCH",
            "why": "Funding path unresolved",
            "evidence_requirements": ["Funding path conditions evidence"],
        },
    ),
    (
        "capital timing",
        {
            "title": "Confirm supplier payment terms",
            "action_type": "RESEARCH",
            "why": "Capital timing unknown",
            "evidence_requirements": ["Supplier payment timing evidence"],
        },
    ),
]


def ensure_actions_for_pursuit_gaps(
    assessment: dict[str, Any],
    *,
    persist: bool = False,
) -> list[dict[str, Any]]:
    """Create Action Orchestration actions for critical unknowns — no second task system."""
    oid = assessment.get("opportunity_id")
    if not _known(oid):
        return []
    created: list[dict[str, Any]] = []
    seen_titles: set[str] = set()
    unknowns: list[str] = []
    for key in DIMENSIONS:
        d = _as_dict((assessment.get("dimensions") or {}).get(key))
        unknowns.extend([str(u) for u in (d.get("unknowns") or [])])

    try:
        from m3_action_orchestration_read import create_action, list_actions

        existing = {
            str(a.get("title") or "").lower()
            for a in list_actions(opportunity_id=str(oid), limit=50)
        }
    except Exception:
        create_action = None  # type: ignore
        existing = set()

    for text in unknowns:
        low = text.lower()
        tmpl = None
        for key, spec in _MISSING_ACTION_TEMPLATES:
            if key in low:
                tmpl = spec
                break
        if not tmpl:
            continue
        title = tmpl["title"]
        if title.lower() in seen_titles or title.lower() in existing:
            continue
        seen_titles.add(title.lower())
        if not create_action:
            created.append({"title": title, "suggested": True, "persist": False})
            continue
        try:
            action = create_action(
                {
                    "title": title,
                    "action_type": tmpl["action_type"],
                    "why": tmpl["why"],
                    "trigger_source": f"pursuit_readiness:{oid}:{title}",
                    "opportunity_id": oid,
                    "related_opportunity": oid,
                    "evidence_requirements": tmpl["evidence_requirements"],
                    "created_by": "pursuit_readiness",
                },
                persist=persist,
            )
            created.append(action)
        except Exception as e:
            log.debug("pursuit action create skipped: %s", e)
    return created


def build_pursuit_readiness_assessment(
    row: dict[str, Any] | None,
    *,
    deal: dict[str, Any] | None = None,
    ensure_actions: bool = False,
    persist_actions: bool = False,
) -> dict[str, Any]:
    row = row if isinstance(row, dict) else {}
    deal = deal if isinstance(deal, dict) else {}
    oid = row.get("canonical_id") or deal.get("canonical_id") or "UNKNOWN"
    name = (
        _as_dict(deal.get("overview")).get("title")
        or row.get("title")
        or "UNKNOWN"
    )

    dims = {
        "product_fit": _product_fit(row, deal),
        "supply_confidence": _supply_confidence(row, deal),
        "economics_visibility": _economics_visibility(row, deal),
        "competition_context": _competition_context(row, deal),
        "compliance_execution_fit": _compliance_execution_fit(row, deal),
    }
    overall = _overall(dims)

    lifecycle = None
    try:
        from m3_opportunity_operating_read import derive_operator_lifecycle

        lifecycle = derive_operator_lifecycle(row)
    except Exception:
        pass

    deadline_ctx = None
    try:
        from deadline_runtime import deadline_context_for_operator

        deadline_ctx = deadline_context_for_operator(row)
    except Exception:
        deadline_ctx = {
            "deadline": row.get("deadline") or "UNKNOWN",
            "deadline_known": bool(row.get("deadline")),
            "deadline_viability": row.get("deadline_viability") or "UNKNOWN",
        }

    assessment = {
        "kind": "M3PursuitReadinessAssessment",
        "build": BUILD_TAG,
        "opportunity_id": oid,
        "opportunity_name": name,
        "deadline_context": deadline_ctx,
        "dimensions": dims,
        "overall": overall,
        "operator_lifecycle": (lifecycle or {}).get("current_state") if lifecycle else "UNKNOWN",
        "unresolved_blockers": overall.get("biggest_blockers") or [],
        "next_actions": [
            overall.get("recommended_next_human_action"),
            *[
                (dims[k].get("next_action"))
                for k in DIMENSIONS
                if dims[k].get("state") != ST_KNOWN
            ],
        ][:6],
        "evidence_discipline": "Claim → Evidence → Source → Date → Context",
        "principles": {
            "does_not_predict_winners": True,
            "reduces_uncertainty": True,
            "no_numeric_score": True,
            "no_ranking": True,
            "no_win_probability": True,
            "no_autonomous_bid_decisions": True,
            "unknown_preserved": True,
            "ai_confidence_not_evidence": True,
            "humans_decide": True,
        },
        "reuses_opportunity_operating": True,
        "reuses_supply_intelligence": True,
        "reuses_action_orchestration": True,
        "reuses_human_os": True,
        "reuses_deadline_runtime": True,
        "not_a_separate_workspace": True,
        "does_not_replace_pursuit_qualification_module": True,
        "generated_at": _utc(),
        "read_only": True,
    }

    suggested = ensure_actions_for_pursuit_gaps(
        assessment, persist=persist_actions if ensure_actions else False
    )
    assessment["suggested_actions"] = [
        {
            "action_id": a.get("action_id"),
            "title": a.get("title"),
            "why": a.get("why_exists") or a.get("why"),
            "status": a.get("status") or ("SUGGESTED" if a.get("suggested") else "UNKNOWN"),
        }
        for a in suggested[:8]
    ]
    if ensure_actions and persist_actions:
        assessment["actions_persisted"] = True
    return assessment


def build_pursuit_readiness_profile(row: dict[str, Any] | None) -> dict[str, Any]:
    assessment = build_pursuit_readiness_assessment(row, ensure_actions=False)
    return {
        "kind": "M3PursuitReadinessProfile",
        "build": BUILD_TAG,
        "assessment": assessment,
        "view": {
            "opportunity": assessment.get("opportunity_name"),
            "pursuit_readiness": {
                d["label"]: {
                    "status": d.get("status"),
                    "state": d.get("state"),
                    "why": d.get("why"),
                    "evidence": d.get("evidence"),
                    "unknowns": d.get("unknowns"),
                    "next_action": d.get("next_action"),
                }
                for d in (assessment.get("dimensions") or {}).values()
            },
            "overall": assessment.get("overall"),
        },
        "generated_at": _utc(),
        "read_only": True,
    }


def attach_pursuit_readiness_to_deal_room(
    deal: dict[str, Any], *, row: dict[str, Any] | None = None
) -> dict[str, Any]:
    if not isinstance(deal, dict):
        return deal
    out = dict(deal)
    try:
        r = row or {"canonical_id": deal.get("canonical_id")}
        assessment = build_pursuit_readiness_assessment(r, deal=out, ensure_actions=False)
        out["pursuit_readiness"] = assessment
    except Exception as e:
        log.warning("pursuit readiness attach failed: %s", e)
        out["pursuit_readiness"] = {
            "kind": "M3PursuitReadinessAssessment",
            "build": BUILD_TAG,
            "error": "pursuit_readiness_unavailable",
            "read_only": True,
        }
    return out


def build_pursuit_operator_boards(rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Human OS: Today + Decisions slices from pursuit readiness (not rankings)."""
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    need_research: list[dict[str, Any]] = []
    need_supplier: list[dict[str, Any]] = []
    need_decision: list[dict[str, Any]] = []
    decision_blockers: list[dict[str, Any]] = []

    for r in rows[:40]:
        a = build_pursuit_readiness_assessment(r, ensure_actions=False)
        dims = a.get("dimensions") or {}
        item = {
            "opportunity_id": a.get("opportunity_id"),
            "title": (a.get("opportunity_name") or "")[:100],
            "next": (a.get("overall") or {}).get("recommended_next_human_action", {}).get("what"),
            "why": (a.get("overall") or {}).get("summary"),
            "blockers": (a.get("unresolved_blockers") or [])[:3],
        }
        pf = (dims.get("product_fit") or {}).get("state")
        sc = (dims.get("supply_confidence") or {}).get("state")
        ev = (dims.get("economics_visibility") or {}).get("state")
        ce = (dims.get("compliance_execution_fit") or {}).get("state")

        if pf in {ST_UNKNOWN, ST_PARTIAL} or ce in {ST_UNKNOWN, ST_PARTIAL}:
            need_research.append(item)
        if sc in {ST_UNKNOWN, ST_PARTIAL}:
            need_supplier.append(item)

        knownish = sum(
            1
            for d in dims.values()
            if isinstance(d, dict) and d.get("state") in {ST_KNOWN, ST_PARTIAL}
        )
        critical_unknown = any(
            (dims.get(k) or {}).get("state") == ST_UNKNOWN
            for k in ("product_fit", "supply_confidence", "economics_visibility")
        )
        if knownish >= 3 and not critical_unknown:
            need_decision.append(item)
        if knownish >= 3 and (a.get("unresolved_blockers") or []):
            decision_blockers.append(item)

    return {
        "kind": "M3PursuitReadinessBoards",
        "today": {
            "opportunities_needing_research": need_research[:12],
            "opportunities_needing_supplier_actions": need_supplier[:12],
            "opportunities_needing_decisions": need_decision[:12],
        },
        "decisions": {
            "enough_evidence_for_review": need_decision[:12],
            "unresolved_decision_blockers": decision_blockers[:12],
        },
        "not_a_ranking": True,
        "question": "Which opportunities deserve my limited time, and why?",
        "build": BUILD_TAG,
        "generated_at": _utc(),
    }


def enrich_command_center_pursuit_readiness(
    sections: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    period: str,
) -> dict[str, Any]:
    out = dict(sections or {})
    boards = build_pursuit_operator_boards(rows)
    if period == "morning":
        out["pursuit_need_research"] = boards["today"]["opportunities_needing_research"]
        out["pursuit_need_supplier"] = boards["today"]["opportunities_needing_supplier_actions"]
        out["pursuit_need_decision"] = boards["today"]["opportunities_needing_decisions"]
        out["pursuit_decision_blockers"] = boards["decisions"]["unresolved_decision_blockers"]
    else:
        out["pursuit_decision_reviews_ready"] = boards["decisions"]["enough_evidence_for_review"]
        out["pursuit_blockers_outstanding"] = boards["decisions"]["unresolved_decision_blockers"]
    out["pursuit_readiness_boards"] = boards
    return out
