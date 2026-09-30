"""Phase H deep research — reuse existing engines; wire history Phase G skipped."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from application_clock import now_utc
from phase_g.provenance import PROVENANCE_LIVE_SOURCE
from phase_h.cohort import ARTIFACTS, select_cohort, write_cohort_docs

ROOT = Path(__file__).resolve().parents[1]
_NSN_RE = re.compile(r"\b(\d{4}-\d{2}-\d{3}-\d{4})\b")

STATE_QUOTE_OUTREACH = "READY_FOR_QUOTE_OUTREACH"
STATE_BID_DECISION = "READY_FOR_BID_DECISION"
STATE_BLOCKED = "BLOCKED"
STATE_REJECTED = "REJECTED"
STATE_EXPIRED = "EXPIRED"
STATE_RESEARCHED = "RESEARCHED_NOT_READY"
STATE_ELIGIBILITY_ACTION = "ELIGIBILITY_ACTION_REQUIRED"


def _utc() -> str:
    return now_utc().isoformat()


def _extract_nsn(title: str) -> str | None:
    m = _NSN_RE.search(title or "")
    return m.group(1) if m else None


def _history_class(awards: list[dict[str, Any]]) -> str:
    if not awards:
        return "NO_HISTORY_FOUND"
    priced = []
    recent_priced = []
    for a in awards:
        amt = None
        for k in ("unit_price", "Amount", "total_obligation_amount", "total_obligated_amount", "award_amount"):
            try:
                if a.get(k) is not None and float(a.get(k)) > 0:
                    amt = float(a.get(k))
                    break
            except (TypeError, ValueError):
                continue
        if amt is None:
            continue
        priced.append(a)
        days_ago = a.get("days_ago")
        try:
            days_ago = float(days_ago) if days_ago is not None else None
        except (TypeError, ValueError):
            days_ago = None
        # Prefer awards within ~5 years for actionable history
        if days_ago is None or days_ago <= 1825:
            recent_priced.append(a)
        else:
            rw = a.get("recency_weight")
            try:
                if rw is not None and float(rw) >= 0.05:
                    recent_priced.append(a)
            except (TypeError, ValueError):
                pass
    usable = recent_priced or []
    if len(usable) >= 3:
        return "STRONG_HISTORY"
    if len(usable) >= 1:
        return "MODERATE_HISTORY"
    # Ancient-only history is weak signal — not enough for quote-ready economics
    if len(priced) >= 1:
        return "WEAK_HISTORY"
    if len(awards) >= 1:
        return "WEAK_HISTORY"
    return "NO_HISTORY_FOUND"


def _sam_listing_url(row: dict[str, Any]) -> str | None:
    for k in ("ui_link", "listing_url", "source_url", "detail_url", "link"):
        v = row.get(k)
        if v and str(v).startswith("http"):
            return str(v)
    nid = row.get("source_opportunity_id") or row.get("notice_id") or row.get("canonical_id")
    if nid and re.fullmatch(r"[0-9a-fA-F]{32}", str(nid)):
        return f"https://sam.gov/opp/{nid}/view"
    return None


def _fetch_sam_description(notice_id: str | None, budget: Any) -> dict[str, Any]:
    """Public SAM noticedesc JSON — read-only, counts as HTTP."""
    if not notice_id or not budget.can_http():
        return {"ok": False, "body": None}
    try:
        import os

        import httpx

        key = (os.getenv("SAM_GOV_API_KEY") or "").strip()
        url = f"https://api.sam.gov/prod/opportunities/v1/noticedesc?noticeid={notice_id}"
        params = {"api_key": key} if key else {}
        with httpx.Client(timeout=20.0) as client:
            resp = client.get(url, params=params)
        budget.record_http(1)
        if resp.status_code != 200:
            return {"ok": False, "status": resp.status_code, "body": None, "LIVE_API_REQUESTS": 1}
        data = resp.json() if resp.content else {}
        desc = data.get("description") if isinstance(data, dict) else None
        if not desc and isinstance(data, dict):
            desc = data.get("Description") or data.get("body")
        text = str(desc or "")
        return {"ok": bool(text), "body": text, "LIVE_API_REQUESTS": 1, "source": "sam_noticedesc"}
    except Exception as e:
        budget.record_http(1)
        return {"ok": False, "error": str(e)[:200], "body": None, "LIVE_API_REQUESTS": 1}


def _identity_confidence(packet: dict[str, Any], row: dict[str, Any]) -> str:
    title = str(row.get("title") or "")
    desc = str(
        row.get("description")
        or (packet.get("opportunity") or {}).get("description")
        or (packet.get("document_review") or {}).get("body")
        or ""
    )
    nsn = _extract_nsn(title) or row.get("nsn") or _extract_nsn(desc)
    cert = str(packet.get("product_id_certainty") or "").upper()
    items = packet.get("line_items") or []
    facts = packet.get("extracted_facts") or {}
    if not nsn and isinstance(facts, dict):
        nsn = _extract_nsn(json.dumps(facts, default=str)[:4000])
    if nsn and (cert in {"PRODUCT_CONFIRMED", "EXACT", "HIGH", "PRODUCT_NSN_FROM_LISTING"} or items):
        return "EXACT_CONFIRMED" if cert in {"PRODUCT_CONFIRMED", "EXACT"} else "STRONG_MATCH"
    if nsn:
        return "STRONG_MATCH"
    if items and any((i.get("part_number") or i.get("model")) for i in items if isinstance(i, dict)):
        return "PARTIAL"
    if cert and cert not in {"PRODUCT_UNKNOWN", "", "NONE"}:
        return "PARTIAL"
    return "UNKNOWN"


def _economics_state(
    *,
    revenue: float | None,
    history_class: str,
    max_cost: dict[str, Any],
    public_price: float | None,
    identity: str,
    docs_ok: bool,
) -> str:
    if revenue is None and history_class == "NO_HISTORY_FOUND":
        return "ECONOMICS_BLOCKED_BY_UNKNOWN_REVENUE"
    if identity == "UNKNOWN" and not docs_ok:
        return "ECONOMICS_BLOCKED_BY_UNKNOWN_COST"
    max_v = max_cost.get("maximum_allowable_supplier_cost")
    if revenue is not None and max_v is not None and public_price is not None:
        if public_price <= float(max_v):
            return "ECONOMICS_SUPPORTED"
        # Public above max — quote might still work if discount
        return "ECONOMICS_PROMISING_QUOTE_REQUIRED"
    if revenue is not None and max_v is not None and identity in {"EXACT_CONFIRMED", "STRONG_MATCH", "PARTIAL"}:
        return "ECONOMICS_PROMISING_QUOTE_REQUIRED"
    if revenue is not None:
        return "ECONOMICS_BLOCKED_BY_UNKNOWN_COST"
    return "UNKNOWN"


def _funding_state(packet: dict[str, Any]) -> str:
    fund = packet.get("funding") or {}
    conf = str((fund.get("confidence") or fund.get("funding_confidence") or "")).upper()
    matches = fund.get("top_matches") or []
    if "ZERO" in conf or conf in {"SUPPORTED", "HIGH"}:
        return "ZERO_CASH_PATH_SUPPORTED"
    if matches:
        return "FINANCEABLE_IN_PRINCIPLE_BUT_UNPROVEN"
    req = fund.get("requirement") or {}
    if req.get("owner_cash_required") is True:
        return "OWNER_CASH_REQUIRED"
    return "UNKNOWN"


def _phase_h_readiness(
    *,
    identity: str,
    docs_reviewed: bool,
    history_class: str,
    economics_state: str,
    funding_state: str,
    deadline_blocked: bool,
    hard_blockers: list[Any],
    packet: dict[str, Any],
) -> str:
    if deadline_blocked:
        return STATE_EXPIRED
    if packet.get("decision") == "REJECT" or packet.get("bid_qualification_state") == "REJECTED":
        return STATE_REJECTED
    if hard_blockers:
        return STATE_BLOCKED

    # Eligibility outranks economics — never quote/bid actionable without gate pass
    from eligibility_gate import (
        ELIGIBLE_CONDITIONAL,
        ELIGIBILITY_UNKNOWN,
        NOT_CURRENTLY_ELIGIBLE,
        blocks_quote_outreach,
    )

    # Product-resale hunt: repair/overhaul titles are not quote-ready resale deals
    title_blob = str(
        (packet.get("phase_h_source_row") or {}).get("title")
        or (packet.get("opportunity") or {}).get("title")
        or packet.get("title")
        or ""
    )
    if re.search(r"\bRepair\s+of\b|\boverhaul\s+of\b", title_blob, re.I):
        return STATE_RESEARCHED

    gate = packet.get("eligibility_gate") or {}
    if gate and blocks_quote_outreach(gate):
        overall = gate.get("overall_status")
        if overall in {NOT_CURRENTLY_ELIGIBLE, ELIGIBLE_CONDITIONAL, ELIGIBILITY_UNKNOWN}:
            return STATE_ELIGIBILITY_ACTION
        return STATE_ELIGIBILITY_ACTION

    # Bid decision — require known supplier cost (Phase H will almost never hit this without quotes)
    if (
        packet.get("supplier_cost_known") is True
        and economics_state == "ECONOMICS_SUPPORTED"
        and funding_state in {"ZERO_CASH_PATH_SUPPORTED", "FINANCEABLE_IN_PRINCIPLE_BUT_UNPROVEN"}
        and identity in {"EXACT_CONFIRMED", "STRONG_MATCH"}
        and docs_reviewed
    ):
        return STATE_BID_DECISION

    # Quote outreach — evidence enough to ask for quotes safely (still no contact)
    # Require recent-enough history (MODERATE/STRONG); WEAK/ancient-only is not enough
    quote_econ = economics_state in {
        "ECONOMICS_PROMISING_QUOTE_REQUIRED",
        "ECONOMICS_SUPPORTED",
    }
    if (
        identity in {"EXACT_CONFIRMED", "STRONG_MATCH", "PARTIAL"}
        and docs_reviewed
        and quote_econ
        and not deadline_blocked
        and funding_state != "OWNER_CASH_REQUIRED"
        and history_class in {"STRONG_HISTORY", "MODERATE_HISTORY"}
    ):
        return STATE_QUOTE_OUTREACH

    # Alternate quote path: strong identity + NSN history revenue + max cost calculated
    max_cost = (packet.get("phase_h_max_supplier_cost") or {}).get("maximum_allowable_supplier_cost")
    if (
        identity in {"EXACT_CONFIRMED", "STRONG_MATCH"}
        and max_cost is not None
        and history_class in {"STRONG_HISTORY", "MODERATE_HISTORY"}
        and not deadline_blocked
        and funding_state != "OWNER_CASH_REQUIRED"
    ):
        return STATE_QUOTE_OUTREACH

    return STATE_RESEARCHED


def _eligibility_ui(gate: dict[str, Any]) -> dict[str, Any]:
    """Compact operator-facing eligibility chips — only relevant requirements."""
    if not gate:
        return {"overall_status": None, "label": "Not evaluated", "relevant": []}
    overall = gate.get("overall_status")
    label_map = {
        "ELIGIBLE_CONFIRMED": "Confirmed",
        "ELIGIBLE_CONDITIONAL": "Conditional",
        "NOT_CURRENTLY_ELIGIBLE": "Blocked",
        "ELIGIBILITY_UNKNOWN": "Unknown",
        "ELIGIBILITY_NOT_APPLICABLE": "Not applicable",
    }
    relevant: list[dict[str, str]] = []
    if gate.get("vehicle_required") and gate.get("vehicle_name"):
        relevant.append(
            {
                "name": gate.get("vehicle_name") or "Vehicle",
                "status": str(gate.get("vehicle_status") or "UNKNOWN"),
            }
        )
    elif gate.get("boa_required"):
        relevant.append({"name": "BOAST BOA", "status": str(gate.get("boa_status") or "UNKNOWN")})
    if gate.get("jcp_required"):
        relevant.append({"name": "JCP", "status": str(gate.get("jcp_status") or "UNKNOWN")})
    if gate.get("approved_source_required"):
        relevant.append(
            {"name": "Approved source", "status": str(gate.get("approved_source_status") or "UNKNOWN")}
        )
    if gate.get("set_aside_status") not in {None, "NOT_REQUIRED"}:
        relevant.append(
            {
                "name": f"Set-aside ({gate.get('set_aside_eligibility') or 'UNKNOWN'})",
                "status": str(gate.get("set_aside_status")),
            }
        )
    if gate.get("sam_status") and gate.get("sam_status") != "UNKNOWN":
        relevant.append({"name": "SAM", "status": str(gate.get("sam_status"))})
    if gate.get("cage_status") and gate.get("cage_required"):
        relevant.append({"name": "CAGE", "status": str(gate.get("cage_status"))})
    plain = gate.get("plain") or {}
    return {
        "overall_status": overall,
        "label": label_map.get(str(overall), str(overall or "Unknown")),
        "plain": plain,
        "relevant": relevant,
        "blocking_reason": gate.get("blocking_reason"),
        "next_action": gate.get("next_action") or plain.get("next"),
        "operator_state": gate.get("operator_state"),
    }


def _operator_packet(row: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    hist = result.get("phase_h_history") or {}
    awards = hist.get("awards") or []
    prior = awards[0] if awards else {}
    max_cost = result.get("phase_h_max_supplier_cost") or {}
    econ_state = result.get("phase_h_economics_state")
    identity = result.get("phase_h_identity_confidence")
    nsn = result.get("phase_h_nsn")
    max_v = max_cost.get("maximum_allowable_supplier_cost")
    next_action = result.get("primary_next_action") or result.get("phase_h_next_action")
    gate = result.get("eligibility_gate") or {}
    if result.get("phase_h_readiness") == STATE_ELIGIBILITY_ACTION:
        next_action = gate.get("next_action") or (gate.get("plain") or {}).get("next") or next_action
    elif result.get("phase_h_readiness") == STATE_QUOTE_OUTREACH and max_v is not None and nsn:
        next_action = (
            f"Request pricing from authorized distributors for NSN {nsn}. "
            f"To clear target economics, delivered supplier cost must be ≤ ${max_v:,.2f} "
            f"(history class {result.get('phase_h_history_class')}; no outreach sent)."
        )
    elif result.get("phase_h_readiness") == STATE_QUOTE_OUTREACH and max_v is not None:
        next_action = (
            f"Request supplier quotes for confirmed product. "
            f"Max allowable delivered supplier cost ≤ ${max_v:,.2f}. No outreach sent."
        )
    return {
        "kind": "PhaseHOperatorPacket",
        "opportunity": {
            "solicitation": row.get("solicitation_number") or row.get("canonical_id"),
            "agency": row.get("buyer"),
            "deadline": row.get("deadline") or (result.get("opportunity") or {}).get("due_date"),
            "product": row.get("title"),
            "nsn": nsn,
            "quantity": "UNKNOWN",
            "uom": "UNKNOWN",
            "provenance": PROVENANCE_LIVE_SOURCE,
        },
        "why_it_matters": (
            f"Live federal product candidate ({row.get('product_class')}). "
            f"Identity={identity}; economics={econ_state}."
        ),
        "government_history": {
            "class": result.get("phase_h_history_class"),
            "prior_award": prior.get("Award ID") or prior.get("award_id"),
            "prior_value": prior.get("Amount") or prior.get("total_obligation_amount"),
            "awardee": prior.get("Recipient Name") or prior.get("recipient_name"),
            "count": len(awards),
        },
        "market": {
            "public_price": result.get("phase_h_public_price"),
            "price_type": result.get("phase_h_public_price_type") or "UNKNOWN",
            "quote_required": True,
            "suppliers_plan": (result.get("suppliers") or {}).get("search_plan")
            or (result.get("suppliers") or {}).get("plan"),
        },
        "economics": {
            "state": econ_state,
            "revenue_basis": result.get("phase_h_revenue_basis"),
            "max_supplier_cost": max_v,
            "public_market_cost": result.get("phase_h_public_price"),
            "required_discount": result.get("phase_h_required_discount"),
        },
        "compliance": {
            "hard_blockers": result.get("hard_blockers") or [],
            "document_reviewed": bool((result.get("document_review") or {}).get("reviewed")),
            "documents": (result.get("document_review") or {}).get("documents") or [],
        },
        "funding": {
            "state": result.get("phase_h_funding_state"),
            "note": "No owner cash / PG / Net-30 assumed; financing unproven until quote+PO path exists.",
        },
        "eligibility": _eligibility_ui(result.get("eligibility_gate") or {}),
        "readiness": result.get("phase_h_readiness"),
        "next_action": next_action,
    }


def _lookup_history(
    nsn: str | None,
    title: str,
    budget_counter: dict[str, int],
    *,
    identity: dict[str, Any] | None = None,
    mpn: str | None = None,
) -> dict[str, Any]:
    """NSN-first history. Never bare-noun keywords (false-match risk)."""
    if budget_counter.get("usaspending", 0) >= budget_counter.get("usaspending_max", 25):
        return {"awards": [], "class": "NO_HISTORY_FOUND", "skipped": "budget"}

    keywords: list[str] = []
    if identity:
        from phase_j.history_reconciliation import history_search_keywords

        keywords = history_search_keywords(identity)
    if not keywords and nsn:
        keywords = [nsn, nsn.replace("-", "")]
    if not keywords and mpn:
        keywords = [mpn]
    # Phase J: do NOT fall back to WHEEL ASSEMBLY / TEST SET noun keywords
    if not keywords:
        return {
            "awards": [],
            "class": "NO_HISTORY_FOUND",
            "reason": "no_nsn_or_mpn_identity_key",
            "false_match_guard": True,
        }
    try:
        from usaspending_client import fetch_awards_by_keywords

        awards = fetch_awards_by_keywords(keywords[:2], limit=10)
        budget_counter["usaspending"] = budget_counter.get("usaspending", 0) + 1
        return {
            "awards": awards or [],
            "class": _history_class(awards or []),  # provisional; reconciliation overrides
            "keywords": keywords[:2],
            "LIVE_API_REQUESTS": 1,
        }
    except Exception as e:
        budget_counter["usaspending"] = budget_counter.get("usaspending", 0) + 1
        return {"awards": [], "class": "NO_HISTORY_FOUND", "error": str(e)[:200]}


def _revenue_from_history(awards: list[dict[str, Any]]) -> tuple[float | None, str]:
    vals: list[float] = []
    for a in awards:
        for k in ("Amount", "total_obligation_amount", "total_obligated_amount", "award_amount"):
            v = a.get(k)
            try:
                if v is not None and float(v) > 0:
                    vals.append(float(v))
                    break
            except (TypeError, ValueError):
                continue
    if not vals:
        return None, "no_numeric_award_amount"
    # Use median-ish: middle value
    vals.sort()
    mid = vals[len(vals) // 2]
    return mid, "usaspending_award_amount_medianish"


def research_one_phase_h(
    row: dict[str, Any],
    *,
    budget: Any,
    budget_counter: dict[str, int],
    authorize_live: bool = True,
) -> dict[str, Any]:
    from deep_deal_economics import maximum_allowable_supplier_cost
    from deep_deal_research import research_one_deal

    title = str(row.get("title") or "")
    nsn = _extract_nsn(title)
    url = _sam_listing_url(row)
    notice_id = row.get("source_opportunity_id") or row.get("canonical_id")
    opp = {
        "canonical_id": row.get("canonical_id"),
        "title": title,
        "agency": row.get("buyer"),
        "solicitation_number": row.get("solicitation_number") or row.get("source_opportunity_id"),
        "external_id": row.get("source_opportunity_id"),
        "source_id": row.get("source") or "fed_sam_contract_opportunities",
        "source_url": url,
        "detail_url": url,
        "deadline": row.get("deadline"),
        "response_deadline": row.get("deadline"),
        "product_classification": row.get("product_class"),
        "phase_g_provenance": PROVENANCE_LIVE_SOURCE,
        "notice_id": notice_id,
        "is_dla": row.get("is_dla"),
        "description": row.get("description") or "",
    }

    # Prefer official noticedesc body when listing page may be JS-heavy / hang-prone
    desc_pull = {"ok": False}
    if authorize_live and notice_id:
        print(f"  noticedesc {notice_id}...", flush=True)
        desc_pull = _fetch_sam_description(str(notice_id), budget)
        if desc_pull.get("ok") and desc_pull.get("body"):
            opp["description"] = desc_pull["body"]
            opp["solicitation_text"] = desc_pull["body"]
            # Promote NSN from noticedesc when title lacks it (common FSC--NOUN listings)
            if not nsn:
                nsn = _extract_nsn(str(desc_pull["body"]))
                if nsn:
                    row = {**row, "nsn": nsn, "has_nsn": True}

    # Skip sam.gov HTML page fetch when noticedesc succeeded (SPA hangs / low signal)
    fetch_docs = bool(authorize_live and not (desc_pull.get("ok") and desc_pull.get("body")))
    packet = research_one_deal(
        opp,
        budget=budget,
        authorize_live=authorize_live,
        force_openai_offline=True,
        fetch_documents=fetch_docs,
    )
    if desc_pull.get("ok") and desc_pull.get("body"):
        from deep_deal_documents import extract_solicitation_facts

        body = str(desc_pull["body"])
        packet.setdefault("document_review", {})
        packet["document_review"]["noticedesc_ok"] = True
        packet["document_review"]["reviewed"] = True
        packet["document_review"]["noticedesc_chars"] = len(body)
        packet["LIVE_API_REQUESTS"] = int(packet.get("LIVE_API_REQUESTS") or 0) + int(
            desc_pull.get("LIVE_API_REQUESTS") or 0
        )
        extracted = extract_solicitation_facts(body, source_url=f"noticedesc:{notice_id}", document_name="sam_noticedesc")
        packet["extracted_facts"] = extracted.get("facts") or {}
        packet["provenance"] = extracted.get("provenance") or {}
        if nsn and packet.get("product_id_certainty") in {None, "PRODUCT_UNKNOWN"}:
            packet["product_id_certainty"] = "PRODUCT_NSN_FROM_LISTING"

    # Phase J: canonical product identity + NSN-first / MPN history reconciliation
    from phase_j.history_reconciliation import reconcile_awards
    from phase_j.product_identity import build_product_identity

    desc_text = str(opp.get("description") or "")
    if desc_pull.get("ok") and desc_pull.get("body"):
        desc_text = str(desc_pull["body"])
    identity_obj = build_product_identity(
        {**row, "title": title, "description": desc_text, "nsn": nsn},
        text=desc_text,
        extracted_facts=packet.get("extracted_facts") if isinstance(packet.get("extracted_facts"), dict) else None,
    )
    nsn = identity_obj.get("nsn") or nsn
    mpn = identity_obj.get("mpn")

    hist = _lookup_history(nsn, title, budget_counter, identity=identity_obj, mpn=mpn)
    recon = reconcile_awards(identity_obj, hist.get("awards") or [])
    history_class = recon.get("history_class") or "NO_HISTORY_FOUND"
    revenue = recon.get("revenue")
    revenue_basis = recon.get("revenue_basis") or "no_comparable_history"
    # Fallback: do not use unreconciled raw award totals for economics
    if revenue is None and history_class in {"NO_HISTORY_FOUND", "WEAK_HISTORY"}:
        revenue, revenue_basis = None, revenue_basis
    max_cost = maximum_allowable_supplier_cost(expected_revenue=revenue)
    identity = identity_obj.get("identity_confidence") or _identity_confidence(packet, row)
    docs_ok = bool((packet.get("document_review") or {}).get("reviewed"))
    econ_state = _economics_state(
        revenue=revenue,
        history_class=history_class,
        max_cost=max_cost,
        public_price=None,
        identity=identity,
        docs_ok=docs_ok,
    )
    funding_state = _funding_state(packet)
    deadline_blocked = False
    due = (packet.get("opportunity") or {}).get("due_date") or row.get("deadline")
    if due and str(due).upper() not in {"UNKNOWN", "NONE", ""}:
        try:
            from datetime import date

            ds = str(due)[:10]
            if len(ds) >= 10 and ds[4] == "-" and date.fromisoformat(ds) < now_utc().date():
                deadline_blocked = True
        except Exception:
            pass

    packet["phase_h_nsn"] = nsn
    packet["phase_h_mpn"] = mpn
    packet["phase_j_product_identity"] = identity_obj
    packet["phase_j_history_reconciliation"] = recon
    packet["phase_h_history"] = {**hist, "reconciled": True, "usable_count": recon.get("usable_count")}
    packet["phase_h_history_class"] = history_class
    packet["phase_h_revenue"] = revenue
    packet["phase_h_revenue_basis"] = revenue_basis
    packet["phase_h_max_supplier_cost"] = max_cost
    packet["phase_h_identity_confidence"] = identity
    packet["phase_h_economics_state"] = econ_state
    packet["phase_h_funding_state"] = funding_state
    packet["phase_h_public_price"] = None
    packet["phase_h_public_price_type"] = "QUOTE_REQUIRED"
    packet["phase_h_required_discount"] = None
    packet["phase_h_listing_url"] = url

    # Eligibility / vehicle / access BEFORE quote/bid actionability
    from eligibility_gate import evaluate_eligibility_gate

    elig_text_parts = [title, str(opp.get("description") or ""), str(opp.get("solicitation_text") or "")]
    if desc_pull.get("ok") and desc_pull.get("body"):
        elig_text_parts.append(str(desc_pull["body"]))
    facts = packet.get("extracted_facts") or {}
    if facts:
        elig_text_parts.append(json.dumps(facts, default=str)[:8000])
    packet["eligibility_gate"] = evaluate_eligibility_gate(
        {
            **row,
            **opp,
            "title": title,
            "description": "\n".join(elig_text_parts),
            "extracted_facts": facts,
        },
        text="\n".join(elig_text_parts),
    )

    packet["phase_h_readiness"] = _phase_h_readiness(
        identity=identity,
        docs_reviewed=docs_ok,
        history_class=history_class,
        economics_state=econ_state,
        funding_state=funding_state,
        deadline_blocked=deadline_blocked,
        hard_blockers=list(packet.get("hard_blockers") or []),
        packet=packet,
    )
    if packet["phase_h_readiness"] == STATE_ELIGIBILITY_ACTION:
        packet["phase_h_next_action"] = (packet.get("eligibility_gate") or {}).get("next_action")
        packet["primary_next_action"] = packet["phase_h_next_action"]
    elif packet["phase_h_readiness"] == STATE_QUOTE_OUTREACH:
        max_v = (packet.get("phase_h_max_supplier_cost") or {}).get("maximum_allowable_supplier_cost")
        nsn = packet.get("phase_h_nsn")
        if max_v is not None and nsn:
            packet["phase_h_next_action"] = (
                f"Request pricing from authorized distributors for NSN {nsn}. "
                f"To clear target economics, delivered supplier cost must be <= ${max_v:,.2f} "
                f"(history class {packet.get('phase_h_history_class')}; no outreach sent)."
            )
        elif max_v is not None:
            packet["phase_h_next_action"] = (
                f"Request supplier quotes for confirmed product. "
                f"Max allowable delivered supplier cost <= ${max_v:,.2f}. No outreach sent."
            )
        packet["primary_next_action"] = packet.get("phase_h_next_action") or packet.get("primary_next_action")
    else:
        packet["phase_h_next_action"] = packet.get("primary_next_action")
    packet["phase_h_operator_packet"] = _operator_packet(row, packet)
    packet["phase_h_source_row"] = {
        "canonical_id": row.get("canonical_id"),
        "title": title,
        "listing_url": url,
        "provenance": PROVENANCE_LIVE_SOURCE,
    }
    return packet


def run_phase_h_deep_research(*, size: int = 25, authorize_live: bool = True) -> dict[str, Any]:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:
        pass
    from deep_deal_research import ResearchBudget
    from operating_mode import MODE_DEVELOPMENT_NO_OUTREACH, set_operating_mode

    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)
    cohort = select_cohort(size=size)
    write_cohort_docs(cohort)
    budget = ResearchBudget(public_http_hard_max=100, public_http_target=80, usaspending_hard_max=25, sam_hard_max=0)
    counter = {"usaspending": 0, "usaspending_max": 25}
    results: list[dict[str, Any]] = []

    for i, row in enumerate(cohort, 1):
        print(f"[phase_h] {i}/{len(cohort)} {(row.get('title') or '')[:60]}", flush=True)
        try:
            packet = research_one_phase_h(row, budget=budget, budget_counter=counter, authorize_live=authorize_live)
            print(
                f"  -> {packet.get('phase_h_readiness')} hist={packet.get('phase_h_history_class')} "
                f"id={packet.get('phase_h_identity_confidence')} docs={bool((packet.get('document_review') or {}).get('reviewed'))}",
                flush=True,
            )
        except Exception as e:
            print(f"  -> ERROR {e}", flush=True)
            packet = {
                "phase_h_readiness": STATE_BLOCKED,
                "error": str(e)[:300],
                "phase_h_source_row": {"canonical_id": row.get("canonical_id"), "title": row.get("title")},
                "phase_h_history_class": "NO_HISTORY_FOUND",
                "phase_h_identity_confidence": "UNKNOWN",
                "phase_h_economics_state": "UNKNOWN",
                "phase_h_funding_state": "UNKNOWN",
                "phase_h_operator_packet": {"next_action": f"Research failed: {e}"},
            }
        results.append(packet)

    readiness = Counter(r.get("phase_h_readiness") for r in results)
    identity = Counter(r.get("phase_h_identity_confidence") for r in results)
    history = Counter(r.get("phase_h_history_class") for r in results)
    economics = Counter(r.get("phase_h_economics_state") for r in results)
    funding = Counter(r.get("phase_h_funding_state") for r in results)

    quote_ready = [r for r in results if r.get("phase_h_readiness") == STATE_QUOTE_OUTREACH]
    bid_ready = [r for r in results if r.get("phase_h_readiness") == STATE_BID_DECISION]

    scorecard = {
        "kind": "PhaseHScorecard",
        "selected": len(cohort),
        "deep_researched": len(results),
        "expired": readiness.get(STATE_EXPIRED, 0),
        "rejected": readiness.get(STATE_REJECTED, 0),
        "blocked": readiness.get(STATE_BLOCKED, 0),
        "researched_not_ready": readiness.get(STATE_RESEARCHED, 0),
        "READY_FOR_QUOTE_OUTREACH": len(quote_ready),
        "READY_FOR_BID_DECISION": len(bid_ready),
        "ELIGIBILITY_ACTION_REQUIRED": readiness.get(STATE_ELIGIBILITY_ACTION, 0),
        "eligibility_status_counts": dict(
            Counter((r.get("eligibility_gate") or {}).get("overall_status") for r in results)
        ),
        "identity_counts": dict(identity),
        "history_counts": dict(history),
        "economics_counts": dict(economics),
        "funding_counts": dict(funding),
        "readiness_counts": dict(readiness),
        "docs_reviewed": sum(1 for r in results if (r.get("document_review") or {}).get("reviewed")),
        "history_found": sum(1 for r in results if r.get("phase_h_history_class") not in {None, "NO_HISTORY_FOUND"}),
        "prior_unit_price_found": sum(
            1
            for r in results
            for a in ((r.get("phase_h_history") or {}).get("awards") or [])
            if a.get("unit_price")
        ),
        "false_READY": 0,  # filled by manual audit
        "false_reject": 0,
        "budgets": budget.to_dict(),
        "usaspending_calls": counter.get("usaspending", 0),
        "external_actions": 0,
        "DEVELOPMENT_NO_OUTREACH": True,
        "generated_at": _utc(),
    }

    report = {
        "kind": "PhaseHDeepResearchRun",
        "scorecard": scorecard,
        "quote_ready": [
            {
                "title": (r.get("phase_h_source_row") or {}).get("title"),
                "canonical_id": (r.get("phase_h_source_row") or {}).get("canonical_id"),
                "nsn": r.get("phase_h_nsn"),
                "max_supplier_cost": (r.get("phase_h_max_supplier_cost") or {}).get("maximum_allowable_supplier_cost"),
                "history_class": r.get("phase_h_history_class"),
                "next_action": (r.get("phase_h_operator_packet") or {}).get("next_action"),
                "packet": r.get("phase_h_operator_packet"),
            }
            for r in quote_ready
        ],
        "bid_ready": bid_ready,
        "results": results,
        "cohort_ids": [c.get("canonical_id") for c in cohort],
    }
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    (ARTIFACTS / "deep_research_latest.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    (ARTIFACTS / "scorecard_latest.json").write_text(json.dumps(scorecard, indent=2, default=str), encoding="utf-8")
    (ARTIFACTS / "quote_ready_latest.json").write_text(
        json.dumps({"count": len(quote_ready), "cases": report["quote_ready"]}, indent=2, default=str),
        encoding="utf-8",
    )
    return report


if __name__ == "__main__":
    out = run_phase_h_deep_research(size=25, authorize_live=True)
    print(json.dumps(out["scorecard"], indent=2, default=str))
