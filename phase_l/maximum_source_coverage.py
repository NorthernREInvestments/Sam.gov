"""Phase L.2.9 — multi-source coverage engine: parallel history + acquisition branches."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from phase_l.acquisition_pricing import (
    UNVERIFIED_LEAD,
    _search_id_from,
    acquisition_range,
    infer_product_family,
    memory_key,
    recall_price,
    remember_price,
)
from phase_l.board_records import research_board_purchases, research_open_data_purchases
from phase_l.product_detail_resolution import (
    discover_indexed_price_leads,
    research_with_detail_resolution,
)
from phase_l.procurement_intel import (
    empty_source_telemetry,
    merge_history_from_procurement,
    research_procurement_sources,
)
from phase_l.resilient_fetch import DomainCircuitBreaker
from phase_l.source_inventory import record_source_health
from phase_l.source_roles import (
    CORROBORATED_STRONG_PRICE,
    CURRENT_GOV_CONTRACT_PRICE,
    FREIGHT_NOT_YET_RESEARCHED,
    HISTORICAL_GOV_PRICE,
    PRICE_LEAD_ONLY,
    PRICE_NOT_RECOVERED_AUTOMATICALLY,
    PRIMARY_SOURCE_BLOCKED,
    STAGE3_NO_ROW_CAP,
    DEEP_RESEARCH_NO_FIXED_COUNT,
    MANUAL_QUEUE_NO_FIXED_CAP,
)
from phase_l.state_contracts import research_state_contracts


def _f(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def corroborate_price_leads(leads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """If ≥2 independent sources agree on price within ~3%, mark CORROBORATED_STRONG_PRICE."""
    priced: list[tuple[float, dict[str, Any]]] = []
    for L in leads:
        p = _f(L.get("apparent_price") or L.get("price") or L.get("verified_price"))
        if p and p > 0:
            priced.append((p, L))
    out = []
    for L in leads:
        item = dict(L)
        p = _f(L.get("apparent_price") or L.get("price") or L.get("verified_price"))
        if p and p > 0:
            peers = []
            domains = set()
            for op, other in priced:
                # within 3% or $250
                if abs(op - p) <= max(250.0, p * 0.03):
                    peers.append(other)
                    domains.add(((other.get("seller") or other.get("source_url") or "")[:40]))
            if len(peers) >= 2 and len(domains) >= 2:
                item["evidence_state"] = CORROBORATED_STRONG_PRICE
                item["confidence"] = "HIGH"
                item["corroborated"] = True
                item["stage3_rank_eligible"] = True
                item["economics_eligible"] = False
        out.append(item)
    return out


def _history_branch(
    *,
    row: dict[str, Any],
    search_id: dict[str, Any],
    identity: dict[str, Any],
    breaker: DomainCircuitBreaker,
    health: dict[str, Any],
    hist_lookup,
    authorize_live: bool,
) -> dict[str, Any]:
    """Independent HISTORY_BRANCH — USAspending + procurement + state + board + open-data."""
    tele = empty_source_telemetry()
    family_hits: dict[str, dict[str, int]] = {}

    def _bump(fam: str, **kw: int) -> None:
        family_hits.setdefault(fam, {"attempts": 0, "exact_hits": 0, "price_leads": 0, "verified_prices": 0, "history_hits": 0, "blocks": 0})
        for k, v in kw.items():
            family_hits[fam][k] = family_hits[fam].get(k, 0) + int(v)

    history = hist_lookup() if hist_lookup else {"historical_award_unit_price": None}
    _bump("USAspending", attempts=1, history_hits=1 if history.get("historical_award_unit_price") else 0)
    record_source_health(
        health,
        family="USAspending",
        attempts=1,
        history_hits=1 if history.get("historical_award_unit_price") else 0,
        success=bool(history.get("historical_award_unit_price")),
    )

    # Procurement intel (coop/board/PDF via search)
    proc = {"records": [], "telemetry": {}}
    try:
        budget = {"procurement": 0, "procurement_max": 40, "procurement_fetch": 0, "procurement_fetch_max": 20}
        proc = research_procurement_sources(
            search_id=search_id,
            row=row,
            budget=budget,
            authorize_live=authorize_live,
            prefer_history=True,
            max_queries=4,
            max_urls=5,
            telemetry=tele,
        )
        n_hist = sum(
            1
            for r in (proc.get("records") or [])
            if (r.get("price") if isinstance(r, dict) else getattr(r, "price", None))
        )
        _bump("cooperative contracts", attempts=1, history_hits=n_hist, exact_hits=n_hist)
        history = merge_history_from_procurement(history, proc)
    except Exception as exc:
        proc = {"records": [], "error": str(exc)[:120]}

    # State term contracts
    state = research_state_contracts(search_id=search_id, breaker=breaker, max_portals=2, max_docs=2)
    st = state.get("telemetry") or {}
    _bump(
        "state term contracts",
        attempts=int(st.get("attempts") or 0),
        exact_hits=int(st.get("exact_hits") or 0),
        history_hits=int(st.get("history_hits") or 0),
        blocks=int(st.get("blocks") or 0),
    )
    record_source_health(
        health,
        family="state term contracts",
        attempts=int(st.get("attempts") or 0),
        exact_hits=int(st.get("exact_hits") or 0),
        history_hits=int(st.get("history_hits") or 0),
        blocks=int(st.get("blocks") or 0),
        auth_required=bool(st.get("auth_required")),
        restricted=bool(st.get("restricted")),
        success=bool(st.get("price_hits") or st.get("history_hits")),
    )

    # Board records
    board = research_board_purchases(search_id=search_id, row=row, breaker=breaker, max_fetches=3)
    bt = board.get("telemetry") or {}
    _bump(
        "government board records",
        attempts=int(bt.get("attempts") or 0),
        exact_hits=int(bt.get("exact_hits") or 0),
        history_hits=int(bt.get("history_hits") or 0),
        blocks=int(bt.get("blocks") or 0),
    )
    record_source_health(
        health,
        family="government board records",
        attempts=int(bt.get("attempts") or 0),
        history_hits=int(bt.get("history_hits") or 0),
        blocks=int(bt.get("blocks") or 0),
        success=bool(bt.get("history_hits")),
    )

    # Open data
    odata = research_open_data_purchases(search_id=search_id, breaker=breaker, max_datasets=2)
    ot = odata.get("telemetry") or {}
    _bump(
        "open-data datasets",
        attempts=int(ot.get("attempts") or 0),
        exact_hits=int(ot.get("exact_hits") or 0),
        history_hits=int(ot.get("history_hits") or 0),
        blocks=int(ot.get("blocks") or 0),
    )
    record_source_health(
        health,
        family="open-data datasets",
        attempts=int(ot.get("attempts") or 0),
        history_hits=int(ot.get("history_hits") or 0),
        success=bool(ot.get("history_hits")),
    )

    # Merge best history unit from branch records if USAspending miss
    hist_unit = _f(history.get("historical_award_unit_price"))
    hist_sources = []
    if hist_unit is not None:
        hist_sources.append("USAspending")
    for bundle in (state.get("records") or [], board.get("records") or [], odata.get("records") or []):
        for rec in bundle:
            roles = rec.get("roles") or []
            if HISTORICAL_GOV_PRICE in roles or ROLE_LABEL_HIST(rec):
                p = _f(rec.get("price"))
                if p and (hist_unit is None or p > 0):
                    if hist_unit is None:
                        hist_unit = p
                        history["historical_award_unit_price"] = p
                        history["history_source"] = rec.get("source_family")
                        history["history_confidence"] = rec.get("confidence") or "MEDIUM"
                    hist_sources.append(rec.get("source_family") or "expanded")

    return {
        "kind": "HISTORY_BRANCH",
        "history": history,
        "hist_unit": hist_unit,
        "hist_sources": list(dict.fromkeys(hist_sources)),
        "state_records": state.get("records") or [],
        "board_records": board.get("records") or [],
        "open_data_records": odata.get("records") or [],
        "procurement": {"n_records": len(proc.get("records") or []), "error": proc.get("error")},
        "family_hits": family_hits,
        "telemetry": tele,
    }


def ROLE_LABEL_HIST(rec: dict[str, Any]) -> bool:
    return rec.get("source_type") == HISTORICAL_GOV_PRICE or "HISTORICAL" in str(rec.get("source_type") or "")


def _acquisition_branch(
    *,
    row: dict[str, Any],
    commercial: dict[str, Any],
    identity: dict[str, Any],
    search_id: dict[str, Any],
    breaker: DomainCircuitBreaker,
    learning: dict[str, Any],
    memory: dict[str, Any],
    health: dict[str, Any],
    max_shell_fetches: int,
    max_detail_fetches: int,
) -> dict[str, Any]:
    """Independent ACQUISITION_BRANCH — detail resolution + state/coop current + exhaust on block."""
    family_hits: dict[str, dict[str, int]] = {}

    def _bump(fam: str, **kw: int) -> None:
        family_hits.setdefault(
            fam,
            {"attempts": 0, "exact_hits": 0, "price_leads": 0, "verified_prices": 0, "history_hits": 0, "blocks": 0},
        )
        for k, v in kw.items():
            family_hits[fam][k] = family_hits[fam].get(k, 0) + int(v)

    # Primary: L.2.8 product-detail + bot fallbacks
    acq = research_with_detail_resolution(
        row=row,
        commercial=commercial,
        identity=identity,
        breaker=breaker,
        learning=learning,
        memory=memory,
        max_shell_fetches=max_shell_fetches,
        max_detail_fetches=max_detail_fetches,
    )
    t = acq.get("telemetry") or {}
    blocked = bool(acq.get("blocked_domains") or t.get("blocked_domains"))
    _bump(
        "distributors",
        attempts=int(t.get("fetches") or 0),
        exact_hits=int(t.get("exact_product_pages") or 0),
        price_leads=int(t.get("leads") or 0),
        verified_prices=int(t.get("verified_prices") or 0),
        blocks=int(t.get("blocked_domains") or 0),
    )
    if blocked:
        acq["failure_class_l29"] = PRIMARY_SOURCE_BLOCKED
        # Exhaust alternatives beyond L.2.8 (state current + coop seeds + indexed)
        state = research_state_contracts(search_id=search_id, breaker=breaker, max_portals=2, max_docs=2)
        st = state.get("telemetry") or {}
        _bump(
            "state term contracts",
            attempts=int(st.get("attempts") or 0),
            exact_hits=int(st.get("exact_hits") or 0),
            price_leads=int(st.get("price_hits") or 0),
            blocks=int(st.get("blocks") or 0),
        )
        for rec in state.get("records") or []:
            if rec.get("price") and CURRENT_GOV_CONTRACT_PRICE in (rec.get("roles") or [CURRENT_GOV_CONTRACT_PRICE]):
                acq.setdefault("gov_channel_prices", []).append(rec)
                # Channel price as lead only unless marked accessible
                acq.setdefault("leads", []).append(
                    {
                        "apparent_price": rec["price"],
                        "source_url": rec.get("source_url"),
                        "source_type": "STATE_CONTRACT",
                        "confidence": "HIGH",
                        "verification_status": UNVERIFIED_LEAD,
                        "economics_eligible": False,
                        "roles": [CURRENT_GOV_CONTRACT_PRICE, PRICE_LEAD_ONLY],
                        "seller": rec.get("state"),
                        "evidence_text": (rec.get("evidence_text") or "")[:200],
                    }
                )

        # Extra indexed leads
        for lead in discover_indexed_price_leads(search_id, limit=3):
            acq.setdefault("leads", []).append(lead.to_dict() if hasattr(lead, "to_dict") else lead)

        if not acq.get("usable") and not acq.get("verified") and not acq.get("leads"):
            acq["failure_class"] = PRICE_NOT_RECOVERED_AUTOMATICALLY

    # Corroborate leads
    leads = corroborate_price_leads(list(acq.get("leads") or []))
    acq["leads"] = leads
    acq["corroborated_leads"] = [L for L in leads if L.get("evidence_state") == CORROBORATED_STRONG_PRICE]
    acq["family_hits"] = family_hits
    acq["freight_status"] = FREIGHT_NOT_YET_RESEARCHED
    return acq


def research_maximum_sources(
    *,
    row: dict[str, Any],
    commercial: dict[str, Any],
    identity: dict[str, Any],
    breaker: DomainCircuitBreaker,
    learning: dict[str, Any],
    memory: dict[str, Any],
    health: dict[str, Any],
    hist_lookup,
    authorize_live: bool = True,
    max_shell_fetches: int = 3,
    max_detail_fetches: int = 3,
    parallel: bool = True,
) -> dict[str, Any]:
    """Run HISTORY_BRANCH and ACQUISITION_BRANCH (optionally parallel), then join."""
    assert STAGE3_NO_ROW_CAP and DEEP_RESEARCH_NO_FIXED_COUNT and MANUAL_QUEUE_NO_FIXED_CAP
    search_id = _search_id_from(commercial, identity, row)
    family = infer_product_family(row, commercial)
    key = memory_key(search_id)

    cached = recall_price(memory, key)
    if cached and cached.get("verified_price"):
        return {
            "kind": "PhaseL29MaximumSourceResearch",
            "family": family,
            "cache_hit": True,
            "history": {"historical_award_unit_price": None},
            "acquisition": {
                "leads": [],
                "verified": [cached],
                "usable": [cached] if cached.get("economics_eligible") else [],
                "acquisition_range": acquisition_range([cached["verified_price"]]),
            },
            "joined": bool(cached.get("verified_price")),
            "source_family_table": {},
        }

    hist_result: dict[str, Any] = {}
    acq_result: dict[str, Any] = {}

    if parallel:
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_hist = pool.submit(
                _history_branch,
                row=row,
                search_id=search_id,
                identity=identity,
                breaker=breaker,
                health=health,
                hist_lookup=hist_lookup,
                authorize_live=authorize_live,
            )
            f_acq = pool.submit(
                _acquisition_branch,
                row=row,
                commercial=commercial,
                identity=identity,
                search_id=search_id,
                breaker=breaker,
                learning=learning,
                memory=memory,
                health=health,
                max_shell_fetches=max_shell_fetches,
                max_detail_fetches=max_detail_fetches,
            )
            hist_result = f_hist.result()
            acq_result = f_acq.result()
    else:
        hist_result = _history_branch(
            row=row,
            search_id=search_id,
            identity=identity,
            breaker=breaker,
            health=health,
            hist_lookup=hist_lookup,
            authorize_live=authorize_live,
        )
        acq_result = _acquisition_branch(
            row=row,
            commercial=commercial,
            identity=identity,
            search_id=search_id,
            breaker=breaker,
            learning=learning,
            memory=memory,
            health=health,
            max_shell_fetches=max_shell_fetches,
            max_detail_fetches=max_detail_fetches,
        )

    # Immediate join
    hist_unit = hist_result.get("hist_unit")
    usable = acq_result.get("usable") or []
    verified = acq_result.get("verified") or []
    leads = acq_result.get("leads") or []
    current_unit = usable[0]["verified_price"] if usable else None
    lead_price = None
    for L in leads:
        if L.get("apparent_price") and L.get("confidence") == "HIGH":
            lead_price = _f(L["apparent_price"])
            break
    both = hist_unit is not None and current_unit is not None
    joined_spread = None
    if both:
        joined_spread = round(float(hist_unit) - float(current_unit), 2)
    elif hist_unit is not None and lead_price is not None:
        joined_spread = round(float(hist_unit) - float(lead_price), 2)

    if usable:
        remember_price(
            memory,
            key,
            {
                "verified_price": usable[0]["verified_price"],
                "source_url": usable[0].get("source_url"),
                "economics_eligible": True,
                "price_type": usable[0].get("price_type"),
                "confidence": usable[0].get("confidence"),
            },
        )

    # Merge family hit tables
    fam_table: dict[str, dict[str, int]] = {}
    for src in (hist_result.get("family_hits") or {}, acq_result.get("family_hits") or {}):
        for fam, counts in src.items():
            dest = fam_table.setdefault(
                fam,
                {"attempts": 0, "exact_hits": 0, "price_leads": 0, "verified_prices": 0, "history_hits": 0, "blocks": 0},
            )
            for k, v in counts.items():
                dest[k] = dest.get(k, 0) + int(v)

    return {
        "kind": "PhaseL29MaximumSourceResearch",
        "family": family,
        "cache_hit": False,
        "history_branch": hist_result,
        "acquisition_branch": {
            "leads": leads,
            "verified": verified,
            "usable": usable,
            "acquisition_range": acq_result.get("acquisition_range"),
            "failure_class": acq_result.get("failure_class") or acq_result.get("failure_class_l29"),
            "telemetry": acq_result.get("telemetry"),
            "blocked_domains": acq_result.get("blocked_domains"),
            "corroborated_leads": acq_result.get("corroborated_leads") or [],
            "manual_fallback": acq_result.get("manual_fallback"),
            "promising_requires_verification": acq_result.get("promising_requires_verification"),
            "detail_resolutions": acq_result.get("detail_resolutions"),
            "gov_channel_prices": acq_result.get("gov_channel_prices") or [],
            "freight_status": acq_result.get("freight_status"),
        },
        "hist_unit": hist_unit,
        "current_unit": current_unit,
        "lead_price": lead_price,
        "both_found": both,
        "joined_unit_spread": joined_spread,
        "source_family_table": fam_table,
        "no_captcha_bypass": True,
    }
