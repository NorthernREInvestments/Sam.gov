"""M3 Owner UI service — assembles operator queues from canonical store + L.22 artifacts.

Backend remains source of truth for priority/status/economics.
Frontend only displays decisions from these endpoints.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from application_clock import now_utc
from phase_l.owner_ui_status import (
    ACTION_CALL_SUPPLIER,
    BID_PREP,
    BLOCKED,
    CALL_SUPPLIER,
    FOLLOW_UP,
    QUOTE_RECEIVED,
    REGISTER_FIRST,
    WAITING_FOR_QUOTE,
    WATCH,
    deadline_urgency,
    map_call_priority_label,
    map_funnel_to_owner_status,
    operator_safe_error,
)
from phase_l.quote_economics import load_json, save_json
from phase_l.registration_tracker import load_tracker, save_tracker

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "phase_l"
BUILD = "20261002-m3-owner-ui-opportunity-visibility-unlock-repair"


def _data_dir() -> Path:
    try:
        from m3_data_root import get_data_root

        return get_data_root()
    except Exception:
        return ROOT / "data"


DATA = ROOT / "data"  # legacy; prefer _data_dir()

# Operator session notes (does not alter canonical funnel without explicit complete)
def _ui_notes_path() -> Path:
    return _data_dir() / "owner_ui_operator_notes.json"


def _ui_quotes_path() -> Path:
    return _data_dir() / "owner_ui_quote_reviews.json"


UI_NOTES_PATH = DATA / "owner_ui_operator_notes.json"
UI_QUOTES_PATH = DATA / "owner_ui_quote_reviews.json"

DEAD_FUNNEL_STATES = {
    "FAST_REJECT",
    "REJECTED",
    "HARD_REJECT",
    "EXPIRED",
    "CANCELED",
    "CANCELLED",
}
DATA_SOURCE_MISSING = "DATA_SOURCE_MISSING"
OPPORTUNITY_STORE_MISSING = "OPPORTUNITY_STORE_MISSING"


def _utc() -> str:
    return now_utc().isoformat()


def _load_store() -> dict[str, Any]:
    from phase_l.l23_full_population_funnel import load_store

    return load_store()


def _canonical_store_meta() -> dict[str, Any]:
    try:
        from m3_data_root import canonical_opportunity_store_path

        path = canonical_opportunity_store_path()
    except Exception:
        path = _data_dir() / "l23_canonical_population_store.json"
    exists = path.exists()
    updated = None
    if exists:
        try:
            raw = load_json(path)
            updated = raw.get("updated_at") if isinstance(raw, dict) else None
        except Exception:
            updated = None
    return {"path": str(path), "exists": exists, "updated_at": updated}


def _is_available_rec(rec: dict[str, Any]) -> bool:
    """Exclude only expired/canceled/hard-rejected — keep UNKNOWN economics/financing visible."""
    st = str(rec.get("current_funnel_state") or "")
    if st in DEAD_FUNNEL_STATES:
        return False
    if str(rec.get("freshness") or "").upper() in {"EXPIRED", "CANCELED", "CANCELLED"}:
        return False
    if str(rec.get("source_status") or "").upper() in {"CANCELED", "CANCELLED", "CLOSED_EXPIRED"}:
        return False
    return True


def _jurisdiction_bucket(rec: dict[str, Any]) -> str:
    if rec.get("is_federal") or str(rec.get("jurisdiction") or "").upper() == "FEDERAL":
        return "FEDERAL"
    j = str(rec.get("jurisdiction") or "").upper()
    if "COOP" in j:
        return "COOPERATIVE"
    if j in {"STATE", "LOCAL", "CITY", "COUNTY", "MULTI_AGENCY_NETWORK"}:
        return "STATE_LOCAL" if j != "LOCAL" and j != "CITY" and j != "COUNTY" else "LOCAL"
    if j:
        return j
    return "UNKNOWN"


def _econ_fields(rec: dict[str, Any]) -> dict[str, Any]:
    rr = rec.get("row_ref") if isinstance(rec.get("row_ref"), dict) else {}
    econ = rr.get("economics") if isinstance(rr.get("economics"), dict) else {}
    return {
        "estimated_value": econ.get("expected_revenue") or rec.get("estimated_value") or "UNKNOWN",
        "historical_government_price": rr.get("historical_award_price")
        or econ.get("historical_unit_price")
        or "UNKNOWN",
        "acquisition_cost": econ.get("acquisition_cost")
        or econ.get("public_retail_total")
        or "UNKNOWN",
        "potential_profit": econ.get("expected_net_profit") or "UNKNOWN",
        "supplier_status": "UNKNOWN",
        "financing_status": "UNKNOWN",
        "eligibility_access_status": rec.get("access_status") or "UNKNOWN",
        "registration_needed": str(rec.get("source_status") or "").upper().find("REGISTRATION") >= 0
        or str(rec.get("registration_status") or "").upper() in {"REQUIRED", "NOT_REGISTERED"},
    }


def _available_deal_card(cid: str, rec: dict[str, Any]) -> dict[str, Any]:
    status = map_funnel_to_owner_status(rec)
    econ = _econ_fields(rec)
    return _deal_card(
        deal_id=f"c:{cid}",
        buyer=rec.get("buyer"),
        solicitation=rec.get("solicitation_event_id"),
        product=_product_from_rec(rec),
        deadline=rec.get("deadline"),
        delivery=None,
        estimated_value=econ.get("estimated_value") if econ.get("estimated_value") != "UNKNOWN" else None,
        status_packet=status,
        extras={
            "canonical_id": cid,
            "title": rec.get("title"),
            "jurisdiction_bucket": _jurisdiction_bucket(rec),
            "jurisdiction": rec.get("jurisdiction"),
            "product_service_classification": rec.get("product_service_classification") or "UNKNOWN",
            "m3_status": rec.get("current_funnel_state"),
            "estimated_value": econ.get("estimated_value"),
            "historical_government_price": econ.get("historical_government_price"),
            "acquisition_cost": econ.get("acquisition_cost"),
            "potential_profit": econ.get("potential_profit"),
            "supplier_status": econ.get("supplier_status"),
            "financing_status": econ.get("financing_status"),
            "eligibility_access_status": econ.get("eligibility_access_status"),
            "registration_needed": bool(econ.get("registration_needed")),
            "next_owner_action": status.get("ui_next_action_label") or status.get("ui_next_action"),
        },
    )


def _load_today_calls() -> dict[str, Any]:
    path = OUT / "l22_today_calls.json"
    if not path.exists():
        return {"entries": [], "count": 0}
    return load_json(path)


def _load_workspaces() -> dict[str, Any]:
    path = OUT / "l22_workspaces.json"
    if not path.exists():
        return {"opportunities": []}
    return load_json(path)


def _load_dynamic_questions() -> dict[str, Any]:
    path = OUT / "l22_dynamic_questions.json"
    if path.exists():
        return load_json(path)
    return {}


def _norm(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "").lower()).strip()


def _product_from_rec(rec: dict[str, Any]) -> str:
    dr = rec.get("deep_research") if isinstance(rec.get("deep_research"), dict) else {}
    commercial = dr.get("commercial") if isinstance(dr.get("commercial"), dict) else {}
    bits = [
        commercial.get("manufacturer"),
        commercial.get("model"),
        commercial.get("mpn"),
        rec.get("product_service_classification"),
        rec.get("title"),
    ]
    for b in bits:
        if b and str(b).strip() and str(b).upper() not in {"UNKNOWN", "NONE", "N/A"}:
            return str(b).strip()[:120]
    return (rec.get("title") or "Product TBD")[:120]


def _quantity_from_rec(rec: dict[str, Any]) -> Any:
    dr = rec.get("deep_research") if isinstance(rec.get("deep_research"), dict) else {}
    return dr.get("quantity") or rec.get("quantity")


def _suppliers_from_rec(rec: dict[str, Any]) -> list[dict[str, Any]]:
    dr = rec.get("deep_research") if isinstance(rec.get("deep_research"), dict) else {}
    raw = dr.get("suppliers") if isinstance(dr.get("suppliers"), list) else []
    out = []
    for i, s in enumerate(raw):
        if not isinstance(s, dict):
            continue
        band = "CALL_FIRST" if i == 0 else ("CALL_SECOND" if i == 1 else "BACKUP")
        out.append(
            {
                "supplier_id": s.get("supplier_domain") or s.get("name"),
                "company": s.get("name") or s.get("supplier_domain"),
                "phone": (s.get("contact") or {}).get("phone") if isinstance(s.get("contact"), dict) else s.get("phone"),
                "contact": (s.get("contact") or {}).get("person") if isinstance(s.get("contact"), dict) else None,
                "rfq_url": s.get("locator_url") or s.get("rfq_url"),
                "why": f"Grade {str(s.get('supplier_grade') or '?').replace('SUPPLIER_', '')}; fit={s.get('product_fit') or 'n/a'}",
                "call_priority": map_call_priority_label(band),
                "call_priority_band": band,
                "grade": s.get("supplier_grade"),
            }
        )
    return out


def _index_store_by_title(store: dict[str, Any]) -> dict[str, str]:
    idx: dict[str, str] = {}
    for cid, rec in store.items():
        title = _norm(rec.get("title"))
        if title:
            idx.setdefault(title, cid)
        sol = _norm(rec.get("solicitation_event_id"))
        if sol:
            idx.setdefault(f"sol:{sol}", cid)
    return idx


def _deal_card(
    *,
    deal_id: str,
    buyer: Any,
    solicitation: Any,
    product: Any,
    deadline: Any,
    delivery: Any,
    estimated_value: Any,
    status_packet: dict[str, Any],
    extras: dict[str, Any] | None = None,
) -> dict[str, Any]:
    urg = deadline_urgency(deadline)
    card = {
        "deal_id": deal_id,
        "buyer": buyer or "Buyer TBD",
        "solicitation": solicitation,
        "product": product or "Product TBD",
        "deadline": deadline,
        "deadline_urgency": urg,
        "required_delivery": delivery,
        "estimated_opportunity_value": estimated_value,
        "ui_status": status_packet["ui_status"],
        "ui_status_color": status_packet["ui_status_color"],
        "ui_priority": status_packet["ui_priority"],
        "ui_next_action": status_packet["ui_next_action"],
        "ui_next_action_label": status_packet["ui_next_action_label"],
        "ui_next_action_reason": status_packet["ui_next_action_reason"],
        "why": status_packet["ui_next_action_reason"],
        "backend_funnel_state": status_packet.get("backend_funnel_state"),
        "blocked": status_packet.get("blocked"),
    }
    if extras:
        card.update(extras)
    return card


def _notes_store() -> dict[str, Any]:
    path = _ui_notes_path()
    if path.exists():
        try:
            return load_json(path)
        except Exception:
            pass
    return {"kind": "OwnerUiOperatorNotes", "by_deal": {}, "updated_at": None}


def _save_notes(store: dict[str, Any]) -> None:
    store["updated_at"] = _utc()
    store["kind"] = "OwnerUiOperatorNotes"
    save_json(_ui_notes_path(), store)


def _quotes_store() -> dict[str, Any]:
    path = _ui_quotes_path()
    if path.exists():
        try:
            return load_json(path)
        except Exception:
            pass
    return {"kind": "OwnerUiQuoteReviews", "quotes": [], "updated_at": None}


def build_home() -> dict[str, Any]:
    today = build_today()
    counts = today["counts"]
    active = (
        counts["call_today"]
        + counts["follow_up"]
        + counts["quotes"]
        + counts["registrations"]
        + counts.get("owner_actions", 0)
        + counts.get("responses", 0)
        + counts["bid_prep"]
        + counts.get("submissions", 0)
    )
    health = opportunity_data_health()
    try:
        from m3_canonical_discovery_bridge import discovery_health_payload

        discovery_health = discovery_health_payload()
    except Exception as exc:
        discovery_health = {
            "kind": "DiscoveryHealth",
            "run_status": "NEVER_RUN",
            "error": str(exc)[:200],
        }
    return {
        "kind": "OwnerUiHome",
        "build": BUILD,
        "generated_at": _utc(),
        "headline": "WHAT NEEDS MY ATTENTION TODAY?",
        "cards": [
            {"id": "call_today", "label": "CALL TODAY", "count": counts["call_today"], "color": "green", "href": "#/today/call_today"},
            {"id": "available", "label": "AVAILABLE DEALS", "count": health.get("currently_available") or 0, "color": "green", "href": "#/deals?filter=available"},
            {"id": "follow_up", "label": "FOLLOW UP", "count": counts["follow_up"], "color": "yellow", "href": "#/today/follow_up"},
            {"id": "quotes", "label": "QUOTES RECEIVED", "count": counts["quotes"], "color": "yellow", "href": "#/quotes"},
            {"id": "registrations", "label": "REGISTER", "count": counts["registrations"], "color": "yellow", "href": "#/registrations"},
            {"id": "owner_actions", "label": "OWNER ACTIONS", "count": counts.get("owner_actions", 0), "color": "yellow", "href": "#/today/owner_actions"},
            {"id": "responses", "label": "RESPONSES", "count": counts.get("responses", 0), "color": "green", "href": "#/today/responses"},
            {"id": "bid_prep", "label": "BID PREP", "count": counts["bid_prep"], "color": "green", "href": "#/bid-prep"},
            {"id": "submissions", "label": "SUBMISSIONS", "count": counts.get("submissions", 0), "color": "green", "href": "#/today/submissions"},
            {"id": "blocked", "label": "BLOCKED", "count": counts["blocked"], "color": "red", "href": "#/blocked"},
        ],
        "summary": {
            "active_deals": active,
            "calls_due": counts["call_today"],
            "quotes_pending": counts["quotes"] + counts["follow_up"],
            "bid_ready": counts["bid_prep"],
            "potential_profit": None,  # only when defensible — intentionally null
            "note": "Potential profit shown only when quote economics are defensible.",
        },
        "data_health": health,
        "discovery_health": discovery_health,
        "caught_up": active == 0 and (health.get("status") != DATA_SOURCE_MISSING),
        "caught_up_message": "You're caught up." if active == 0 else None,
        "next_useful": (
            "Opportunity data store is missing — check Settings / Railway volume."
            if health.get("status") == DATA_SOURCE_MISSING
            else ("Open Available Deals to browse the current population." if active == 0 else "Open Today and work the top CALL TODAY item.")
        ),
    }


def build_today() -> dict[str, Any]:
    store = _load_store()
    today_calls = _load_today_calls()
    title_idx = _index_store_by_title(store)
    notes = _notes_store().get("by_deal") or {}

    call_today: list[dict[str, Any]] = []
    follow_up: list[dict[str, Any]] = []
    quotes: list[dict[str, Any]] = []
    registrations: list[dict[str, Any]] = []
    bid_prep: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    watch: list[dict[str, Any]] = []

    seen_call_keys: set[str] = set()

    # 1) L.22 today call queue (highest-fidelity supplier contact)
    for entry in today_calls.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        oid = str(entry.get("opportunity_id") or "")
        title_key = _norm(entry.get("opportunity") or entry.get("product"))
        cid = title_idx.get(title_key) or title_idx.get(f"sol:{_norm(entry.get('solicitation'))}")
        rec = store.get(cid) if cid else {}
        deal_id = f"c:{cid}" if cid else f"l22:{oid}"
        call_status = str(entry.get("call_status") or "NOT_CALLED").upper()
        overlay = {
            "call_status": call_status,
            "priority": entry.get("priority"),
            "priority_score": entry.get("priority_score"),
            "why_call": entry.get("why_call"),
            "call_ready": True,
            "promised_quote_date": (notes.get(deal_id) or {}).get("promised_quote_date"),
        }
        if not rec:
            rec = {
                "current_funnel_state": "READY_TO_CALL",
                "buyer": entry.get("buyer"),
                "title": entry.get("opportunity"),
                "deadline": entry.get("deadline"),
                "priority_score": entry.get("priority_score"),
            }
        status = map_funnel_to_owner_status(rec, overlay=overlay)
        card = _deal_card(
            deal_id=deal_id,
            buyer=entry.get("buyer") or rec.get("buyer"),
            solicitation=entry.get("solicitation") or rec.get("solicitation_event_id"),
            product=entry.get("product") or entry.get("opportunity") or _product_from_rec(rec),
            deadline=entry.get("deadline") or rec.get("deadline"),
            delivery=entry.get("required_delivery") or rec.get("required_delivery"),
            estimated_value=None,
            status_packet=status,
            extras={
                "supplier": entry.get("supplier"),
                "phone": entry.get("phone"),
                "rfq_url": entry.get("rfq_url"),
                "call_priority": map_call_priority_label(entry.get("priority")),
                "opportunity_id": oid,
                "canonical_id": cid,
                "source": "l22_today_calls",
            },
        )
        key = deal_id
        if status["ui_status"] in {FOLLOW_UP, WAITING_FOR_QUOTE}:
            follow_up.append(card)
        else:
            call_today.append(card)
            seen_call_keys.add(key)
            if title_key:
                seen_call_keys.add(title_key)

    # 2) Canonical READY_TO_CALL not already listed
    for cid, rec in store.items():
        if not isinstance(rec, dict):
            continue
        state = rec.get("current_funnel_state")
        deal_id = f"c:{cid}"
        title_key = _norm(rec.get("title"))
        product_key = _norm(_product_from_rec(rec))
        if deal_id in seen_call_keys or title_key in seen_call_keys or product_key in seen_call_keys:
            continue
        # Soft dedupe against L.22 opportunity text already shown
        if any(title_key and title_key in _norm(c.get("product")) for c in call_today):
            continue

        overlay: dict[str, Any] = {}
        nd = notes.get(deal_id) or {}
        if nd.get("promised_quote_date"):
            overlay["promised_quote_date"] = nd["promised_quote_date"]
            overlay["call_status"] = "QUOTE_PROMISED"
        status = map_funnel_to_owner_status(rec, overlay=overlay)
        card = _deal_card(
            deal_id=deal_id,
            buyer=rec.get("buyer"),
            solicitation=rec.get("solicitation_event_id"),
            product=_product_from_rec(rec),
            deadline=rec.get("deadline"),
            delivery=None,
            estimated_value=None,
            status_packet=status,
            extras={
                "suppliers": _suppliers_from_rec(rec)[:3],
                "canonical_id": cid,
                "source": "canonical",
                "quantity": _quantity_from_rec(rec),
            },
        )

        ui = status["ui_status"]
        if ui == CALL_SUPPLIER and state == "READY_TO_CALL":
            call_today.append(card)
            seen_call_keys.add(deal_id)
            if title_key:
                seen_call_keys.add(title_key)
        elif ui in {FOLLOW_UP, WAITING_FOR_QUOTE}:
            follow_up.append(card)
        elif ui == QUOTE_RECEIVED:
            quotes.append(card)
        elif ui == BID_PREP:
            bid_prep.append(card)
        elif ui == BLOCKED:
            blocked.append(card)
        elif ui == REGISTER_FIRST:
            registrations.append(card)
        elif ui == WATCH and state in {"WATCH", "WATCH_OTHER", "DEEP_RESEARCH_COMPLETE"}:
            # Cap watch on Today — full list on Watch screen
            if len(watch) < 25 and state != "DEEP_RESEARCH_COMPLETE":
                watch.append(card)

    # 3) Blocked federal sample for Today (counts from full store)
    blocked_count = sum(1 for r in store.values() if r.get("current_funnel_state") == "WATCH_FEDERAL_ACCESS")
    if not blocked:
        for cid, rec in store.items():
            if rec.get("current_funnel_state") != "WATCH_FEDERAL_ACCESS":
                continue
            status = map_funnel_to_owner_status(rec)
            blocked.append(
                _deal_card(
                    deal_id=f"c:{cid}",
                    buyer=rec.get("buyer"),
                    solicitation=rec.get("solicitation_event_id"),
                    product=_product_from_rec(rec),
                    deadline=rec.get("deadline"),
                    delivery=None,
                    estimated_value=None,
                    status_packet=status,
                    extras={"canonical_id": cid, "source": "canonical"},
                )
            )
            if len(blocked) >= 20:
                break

    # 4) Registrations from tracker
    reg_cards = build_registrations().get("items") or []
    if not registrations:
        registrations = [
            {
                "deal_id": f"reg:{r['portal_id']}",
                "buyer": r.get("portal"),
                "solicitation": None,
                "product": f"Unlocks {r.get('buyers_unlocked', 0)} buyers",
                "deadline": None,
                "deadline_urgency": {"label": None},
                "required_delivery": None,
                "estimated_opportunity_value": None,
                "ui_status": REGISTER_FIRST,
                "ui_status_color": "yellow",
                "ui_priority": r.get("priority") or "NORMAL",
                "ui_next_action": "REGISTER",
                "ui_next_action_label": "REGISTER NOW",
                "ui_next_action_reason": r.get("why"),
                "why": r.get("why"),
                "registration": r,
            }
            for r in reg_cards[:30]
        ]

    # Quote reviews from UI store + funnel
    qstore = _quotes_store()
    for q in qstore.get("quotes") or []:
        if not isinstance(q, dict):
            continue
        quotes.append(
            {
                "deal_id": q.get("deal_id"),
                "buyer": q.get("buyer"),
                "product": q.get("product"),
                "ui_status": QUOTE_RECEIVED,
                "ui_status_color": "yellow",
                "ui_next_action": "REVIEW_QUOTE",
                "ui_next_action_label": "REVIEW QUOTE",
                "ui_next_action_reason": "Quote on file",
                "why": "Quote on file",
                "quote_bucket": q.get("bucket") or "NEW QUOTES",
                "quote": q,
            }
        )

    def _sort_calls(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        order = {"CALL FIRST": 0, "CALL SECOND": 1, "BACKUP": 2}
        return sorted(
            items,
            key=lambda c: (
                0 if (c.get("deadline_urgency") or {}).get("level") == "critical" else 1,
                order.get(c.get("call_priority") or "", 3),
                0 if c.get("ui_priority") == "HIGH" else 1,
                str(c.get("buyer") or ""),
            ),
        )

    call_today = _sort_calls(call_today)

    # 5) Response-engine owner / submission queues (plain language — no R1–R5 labels)
    owner_actions: list[dict[str, Any]] = []
    responses: list[dict[str, Any]] = []
    submissions: list[dict[str, Any]] = []
    awaiting_result: list[dict[str, Any]] = []
    try:
        from response_engine.operator_state_service import build_operator_state
        from response_engine.store import list_projects as list_response_projects
        from response_engine.store import load_project

        for meta in list_response_projects(limit=80):
            rid = meta.get("response_project_id")
            if not rid:
                continue
            project = load_project(rid)
            if not project:
                continue
            state = build_operator_state(project)
            nba = state.get("next_action") or {}
            card = {
                "deal_id": f"rp:{rid}",
                "buyer": project.get("buyer") or meta.get("buyer"),
                "solicitation": project.get("solicitation_number") or meta.get("solicitation_number"),
                "product": project.get("title") or "Response package",
                "deadline": project.get("submission_deadline"),
                "deadline_urgency": {},
                "ui_status": state.get("plain_status"),
                "ui_status_color": "yellow",
                "ui_next_action": nba.get("action_type"),
                "ui_next_action_label": nba.get("action_label"),
                "ui_next_action_reason": nba.get("reason"),
                "assigned_role": nba.get("assigned_role"),
                "response_project_id": rid,
                "canonical_id": project.get("canonical_opportunity_id"),
                "why": nba.get("reason"),
                "source": "response_engine",
            }
            plain = state.get("plain_status") or ""
            if plain == "AWAITING RESULT":
                awaiting_result.append(card)
            elif nba.get("assigned_role") == "owner":
                owner_actions.append(card)
            elif nba.get("action_type") in {"SUBMIT", "VERIFY_RECEIPT"} or plain == "READY FOR GUIDED SUBMISSION":
                submissions.append(card)
            elif nba.get("action_type") in {"BUILD_RESPONSE", "PREFLIGHT", "FIX_PREFLIGHT"} or plain in {
                "BUILD RESPONSE",
                "NEEDS ACTION",
                "READY FOR OWNER APPROVAL",
            }:
                responses.append(card)
    except Exception:
        pass

    sections = {
        "call_today": {
            "title": "CALL TODAY",
            "empty": "No supplier calls need attention right now.",
            "items": call_today,
        },
        "follow_up": {
            "title": "FOLLOW UP",
            "empty": "No follow-ups due right now.",
            "items": follow_up,
        },
        "quotes": {
            "title": "QUOTES TO REVIEW",
            "empty": "No quotes waiting for review.",
            "items": quotes,
        },
        "registrations": {
            "title": "REGISTRATIONS",
            "empty": "No high-priority registrations right now.",
            "items": registrations,
        },
        "owner_actions": {
            "title": "OWNER ACTIONS",
            "empty": "Nothing waiting on the owner right now.",
            "items": owner_actions[:15],
        },
        "responses": {
            "title": "RESPONSES",
            "empty": "No response packages need work right now.",
            "items": responses[:15],
        },
        "bid_prep": {
            "title": "BID PREP",
            "empty": "No deals ready for bid prep.",
            "items": bid_prep,
        },
        "submissions": {
            "title": "SUBMISSIONS",
            "empty": "No guided submissions ready.",
            "items": submissions[:15],
        },
        "awaiting_result": {
            "title": "AWAITING RESULT",
            "empty": "No bids awaiting award decision.",
            "items": awaiting_result[:15],
        },
        "blocked": {
            "title": "BLOCKED",
            "empty": "Nothing blocked that needs your attention.",
            "items": blocked,
            "total_available": blocked_count,
        },
    }

    counts = {
        "call_today": len(call_today),
        "follow_up": len(follow_up),
        "quotes": len(quotes),
        "registrations": len(registrations),
        "owner_actions": len(owner_actions),
        "responses": len(responses),
        "bid_prep": len(bid_prep),
        "submissions": len(submissions),
        "awaiting_result": len(awaiting_result),
        "blocked": blocked_count,
        "watch": sum(1 for r in store.values() if r.get("current_funnel_state") in {"WATCH", "WATCH_OTHER"}),
    }

    return {
        "kind": "OwnerUiToday",
        "build": BUILD,
        "generated_at": _utc(),
        "title": "Today's Work",
        "counts": counts,
        "sections": sections,
        "caught_up": (
            counts["call_today"]
            + counts["follow_up"]
            + counts["quotes"]
            + counts["registrations"]
            + counts["owner_actions"]
            + counts["responses"]
            + counts["bid_prep"]
            + counts["submissions"]
            == 0
        ),
    }


def list_deals(
    *,
    status: str | None = None,
    q: str | None = None,
    buyer: str | None = None,
    state: str | None = None,
    filter: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    meta = _canonical_store_meta()
    if not meta["exists"]:
        return {
            "kind": "OwnerUiDealList",
            "build": BUILD,
            "page": 1,
            "page_size": page_size,
            "total": 0,
            "has_more": False,
            "items": [],
            "data_status": DATA_SOURCE_MISSING,
            "error_code": OPPORTUNITY_STORE_MISSING,
            "message": "Opportunity store is missing — not a legitimate empty population.",
            "store_path": meta["path"],
            "filter": filter or "available",
        }

    store = _load_store()
    page = max(1, int(page or 1))
    page_size = min(100, max(1, int(page_size or 50)))
    qn = _norm(q)
    buyer_n = _norm(buyer)
    status_n = (status or "").strip().upper()
    filt = (filter or "available").strip().lower()

    cards: list[dict[str, Any]] = []
    actionable_first = (
        "READY_TO_CALL",
        "CALLS_IN_PROGRESS",
        "QUOTE_PENDING",
        "QUOTES_RECEIVED",
        "READY_TO_BID",
        "DEEP_RESEARCH_COMPLETE",
        "WATCH_FEDERAL_ACCESS",
        "WATCH",
        "WATCH_OTHER",
    )
    ordered: list[tuple[str, dict[str, Any]]] = []
    for prefer in actionable_first:
        for cid, rec in store.items():
            if rec.get("current_funnel_state") == prefer:
                ordered.append((cid, rec))
    seen = {c for c, _ in ordered}
    for cid, rec in store.items():
        if cid not in seen:
            ordered.append((cid, rec))

    for cid, rec in ordered:
        if not isinstance(rec, dict):
            continue
        # Default available filter excludes dead/rejected only
        if filt in {"", "available", "all_available", "all"}:
            if filt != "all" and not _is_available_rec(rec):
                continue
        elif filt == "product_resale":
            if not _is_available_rec(rec):
                continue
            cls = str(rec.get("product_service_classification") or "").upper()
            if "SERVICE" in cls and "PRODUCT" not in cls:
                continue
        elif filt == "common_commercial":
            if not _is_available_rec(rec):
                continue
            cls = str(rec.get("product_service_classification") or "").upper()
            if cls and cls not in {"COMMON_COMMERCIAL", "COMMERCIAL", "CORE_PRODUCT", "PRODUCT", "UNKNOWN", ""}:
                if "PRODUCT" not in cls and "COMMERCIAL" not in cls:
                    continue
        elif filt == "ready_to_research":
            if rec.get("current_funnel_state") not in {"DEEP_RESEARCH_COMPLETE", "FAST_RESEARCH_COMPLETE", "ACCESSIBLE_PRODUCT", "WATCH", "WATCH_OTHER"}:
                continue
        elif filt == "ready_to_call":
            if rec.get("current_funnel_state") != "READY_TO_CALL":
                continue
        elif filt == "ready_to_quote":
            if rec.get("current_funnel_state") not in {"QUOTE_PENDING", "QUOTES_RECEIVED", "CALLS_IN_PROGRESS"}:
                continue
        elif filt == "registration_blocked":
            if not _is_available_rec(rec):
                continue
            if not (
                "REGISTRATION" in str(rec.get("source_status") or "").upper()
                or rec.get("current_funnel_state") == "WATCH_FEDERAL_ACCESS"
                or str(rec.get("registration_status") or "").upper() in {"REQUIRED", "NOT_REGISTERED"}
            ):
                continue
        elif filt == "financing_blocked":
            if not _is_available_rec(rec):
                continue
            # Soft: show opportunities with financing/access uncertainty — never invent blocked
            if str(rec.get("financing_status") or "").upper() not in {"BLOCKED", "FINANCING_GAP", "EXECUTION_FAIL"}:
                # Keep visible via filter only when explicit; otherwise skip
                fin = ((rec.get("row_ref") or {}) if isinstance(rec.get("row_ref"), dict) else {}).get("economics") or {}
                if not (isinstance(fin, dict) and fin.get("blocker")):
                    continue
        elif filt == "federal":
            if not _is_available_rec(rec) or _jurisdiction_bucket(rec) != "FEDERAL":
                continue
        elif filt in {"state_local", "state", "local"}:
            if not _is_available_rec(rec):
                continue
            bucket = _jurisdiction_bucket(rec)
            if filt == "federal":
                continue
            if bucket == "FEDERAL":
                continue

        status_packet = map_funnel_to_owner_status(rec)
        if status_n and status_packet["ui_status"] != status_n and status_packet["ui_status"].replace(" ", "_") != status_n.replace(" ", "_"):
            if status_packet.get("ui_queue") != status_n.lower() and status_packet["ui_status"] != status_n:
                continue
        if buyer_n and buyer_n not in _norm(rec.get("buyer")):
            continue
        if state and state.upper() not in _norm(rec.get("jurisdiction") or rec.get("buyer") or ""):
            if state.upper() not in str(rec.get("jurisdiction") or "").upper():
                continue
        hay = " ".join(
            str(x or "")
            for x in (rec.get("buyer"), rec.get("title"), rec.get("solicitation_event_id"), _product_from_rec(rec))
        )
        if qn and qn not in _norm(hay):
            continue
        cards.append(_available_deal_card(cid, rec))

    total = len(cards)
    start = (page - 1) * page_size
    slice_ = cards[start : start + page_size]

    # Quote outreach reserve — count only; collapsed by default on Available Deals
    quote_reserve_count = 0
    quote_reserve_surfaced = False
    try:
        from evidence_exhaustion.owner_surface import should_surface_quote_reserve
        from m3_data_root import data_path
        import json as _json

        ee = data_path("m3_evidence_exhaustion_v1_store.json")
        if ee.exists():
            raw = _json.loads(ee.read_text(encoding="utf-8"))
            by = raw.get("by_opportunity") or {}
            quote_reserve_count = sum(
                1
                for o in by.values()
                if isinstance(o, dict) and o.get("terminal_status") == "QUOTE_OUTREACH_RESERVE"
            )
            strong = sum(
                1
                for o in by.values()
                if isinstance(o, dict)
                and (
                    o.get("readiness") in {"LENDER_READY", "NEAR_READY_24H"}
                    or o.get("profit_status") in {"PROVEN_PROFITABLE", "LIKELY_PROFITABLE"}
                )
            )
            quote_reserve_surfaced = should_surface_quote_reserve(strong_lead_count=strong)
    except Exception:
        pass

    return {
        "kind": "OwnerUiDealList",
        "build": BUILD,
        "page": page,
        "page_size": page_size,
        "total": total,
        "has_more": start + page_size < total,
        "items": slice_,
        "filter": filt,
        "data_status": "LOADED",
        "canonical_count": len(store),
        "store_path": meta["path"],
        "quote_outreach_reserve_count": quote_reserve_count,
        "quote_outreach_reserve_collapsed": True,
        "quote_outreach_reserve_surfaced": quote_reserve_surfaced,
        "quote_outreach_reserve_label": f"Quote Outreach Reserve: {quote_reserve_count}",
    }


def get_deal(deal_id: str) -> dict[str, Any]:
    store = _load_store()
    today_calls = _load_today_calls()
    notes = (_notes_store().get("by_deal") or {}).get(deal_id) or {}

    cid = None
    l22_oid = None
    if deal_id.startswith("c:"):
        cid = deal_id[2:]
    elif deal_id.startswith("l22:"):
        l22_oid = deal_id[4:]
    elif deal_id.startswith("reg:"):
        regs = build_registrations()
        for r in regs.get("items") or []:
            if r.get("portal_id") == deal_id[4:]:
                return {"kind": "OwnerUiRegistrationDetail", "registration": r, "deal_id": deal_id}
        raise KeyError(deal_id)
    else:
        cid = deal_id

    rec = store.get(cid) if cid else None
    entry = None
    if l22_oid:
        entry = next((e for e in (today_calls.get("entries") or []) if str(e.get("opportunity_id")) == l22_oid), None)
    if not entry and (cid or rec):
        # Re-attach L.22 contact path when deal was promoted to canonical id
        title_key = _norm((rec or {}).get("title") or "")
        for e in today_calls.get("entries") or []:
            if not isinstance(e, dict):
                continue
            if title_key and (
                title_key == _norm(e.get("opportunity"))
                or title_key in _norm(e.get("opportunity"))
                or _norm(e.get("opportunity")) in title_key
                or _norm(e.get("product")) in title_key
            ):
                entry = e
                break
            if rec and _norm(e.get("solicitation")) and _norm(e.get("solicitation")) == _norm(rec.get("solicitation_event_id")):
                entry = e
                break
    if not rec and entry:
        # fuzzy match
        title_idx = _index_store_by_title(store)
        cid = title_idx.get(_norm(entry.get("opportunity")))
        rec = store.get(cid) if cid else None
        if cid:
            deal_id = f"c:{cid}"

    if not rec and not entry:
        raise KeyError(deal_id)

    rec = rec or {
        "current_funnel_state": "READY_TO_CALL",
        "buyer": (entry or {}).get("buyer"),
        "title": (entry or {}).get("opportunity"),
        "deadline": (entry or {}).get("deadline"),
    }
    overlay = {
        "why_call": (entry or {}).get("why_call"),
        "priority": (entry or {}).get("priority"),
        "call_status": (entry or {}).get("call_status") or notes.get("call_status"),
        "promised_quote_date": notes.get("promised_quote_date"),
        "call_ready": bool(entry) or rec.get("current_funnel_state") == "READY_TO_CALL",
    }
    status = map_funnel_to_owner_status(rec, overlay=overlay)
    suppliers = []
    if entry:
        suppliers.append(
            {
                "supplier_id": entry.get("supplier_id") or entry.get("supplier"),
                "company": entry.get("supplier"),
                "phone": entry.get("phone"),
                "rfq_url": entry.get("rfq_url"),
                "why": entry.get("why_call"),
                "call_priority": map_call_priority_label(entry.get("priority")),
                "call_priority_band": entry.get("priority"),
            }
        )
    suppliers.extend(_suppliers_from_rec(rec))
    # dedupe by supplier_id
    seen_s: set[str] = set()
    uniq = []
    for s in suppliers:
        sid = str(s.get("supplier_id") or s.get("company") or "")
        if sid in seen_s:
            continue
        seen_s.add(sid)
        uniq.append(s)

    dr = rec.get("deep_research") if isinstance(rec.get("deep_research"), dict) else {}
    payload = {
        "kind": "OwnerUiDealDetail",
        "build": BUILD,
        "deal_id": deal_id if deal_id.startswith(("c:", "l22:")) else f"c:{cid or deal_id}",
        "what": {
            "buyer": rec.get("buyer") or (entry or {}).get("buyer"),
            "solicitation": rec.get("solicitation_event_id") or (entry or {}).get("solicitation"),
            "product": (entry or {}).get("product") or _product_from_rec(rec),
            "quantity": (entry or {}).get("quantity") or _quantity_from_rec(rec),
            "deadline": rec.get("deadline") or (entry or {}).get("deadline"),
            "deadline_urgency": deadline_urgency(rec.get("deadline") or (entry or {}).get("deadline")),
            "delivery_requirement": (entry or {}).get("required_delivery"),
            "title": rec.get("title") or (entry or {}).get("opportunity"),
        },
        "why_we_care": {
            "expected_upside": None,
            "evidence_quality": "Defensible only after supplier quote" if not notes.get("unit_price") else "Quote on file",
            "recurring_buyer": None,
            "strong_supplier_path": bool(uniq),
            "deadline_runway": deadline_urgency(rec.get("deadline") or (entry or {}).get("deadline")),
            "summary": status["ui_next_action_reason"],
        },
        "next": {
            "action": status["ui_next_action"],
            "label": status["ui_next_action_label"],
            "reason": status["ui_next_action_reason"],
        },
        "ui_status": status["ui_status"],
        "ui_status_color": status["ui_status_color"],
        "ui_priority": status["ui_priority"],
        "suppliers": uniq,
        "blocked": status.get("blocked"),
        "operator_notes": notes.get("general_notes") or "",
        "has_advanced": True,
        "opportunity_id": (entry or {}).get("opportunity_id") or l22_oid,
        "canonical_id": cid,
        "line_item_economics": _line_item_economics_for_deal(cid or deal_id, rec),
        "profit_first": _profit_first_for_deal(cid or deal_id, rec),
        "package_and_product_data": _package_and_product_data_for_deal(cid, rec),
        "ui_path": "CANONICAL_OPERATOR",
        "legacy_suppressed": True,
    }
    try:
        from p0_prescale_hardening.ui_canonical import attach_canonical_deal_panel
        from p0_prescale_hardening.next_action import opportunity_owner_snapshot
        from m3_data_root import data_path
        import json as _json

        snap = None
        ch_path = data_path("m3_owner_channel_outreach_queue_v1.json")
        if ch_path.exists():
            q = _json.loads(ch_path.read_text(encoding="utf-8"))
            oid_guess = str((entry or {}).get("opportunity_id") or cid or "")
            for row in q.get("queue") or []:
                if oid_guess and oid_guess in str(row.get("Opportunity") or ""):
                    snap = opportunity_owner_snapshot(
                        opportunity_id=str(row.get("Opportunity")),
                        packet={
                            "status": row.get("Packet_status"),
                            "line_count": row.get("Lines"),
                            "packet_audit_status": row.get("Source_trace"),
                            "DIFF": row.get("Qty_reconciliation"),
                            "delivery_destination": row.get("Delivery"),
                            "deadline": row.get("Deadline"),
                            "supplier": {"supplier_name": row.get("Supplier")},
                            "fail_reasons": [],
                        },
                        revenue_usable=False,
                    )
                    break
        payload = attach_canonical_deal_panel(payload, snapshot=snap)
    except Exception:
        payload["canonical_funnel"] = {
            "stages": [
                "OPPORTUNITY",
                "PACKAGE",
                "ELIGIBILITY",
                "PRODUCTS",
                "REVENUE",
                "ACQUISITION",
                "QUOTES",
                "BASKET",
                "FREIGHT",
                "FINANCING",
                "ECONOMICS",
                "EXECUTION",
                "LENDER READY",
                "BID READY",
            ],
            "current_stage": "OPPORTUNITY",
            "do_not_send_automatically": True,
        }
    return payload


def _package_and_product_data_for_deal(cid: str | None, rec: dict[str, Any] | None) -> dict[str, Any]:
    """Plain-language PACKAGE & PRODUCT DATA for Deal page (no engine jargon as primary text)."""
    rec = rec or {}
    pm = rec.get("package_materialization") if isinstance(rec.get("package_materialization"), dict) else {}
    # Fall back to latest package recovery / schedule canary rows
    if not pm and cid:
        try:
            from m3_data_root import data_path
            import json as _json

            for fname in (
                "m3_same20_full_pipeline_v1_rows.json",
                "m3_schedule_recovery_v1_rows.json",
                "m3_package_materialization_v1_rows.json",
                "m3_schedule_backed_canary_v1_rows.json",
                "m3_channel_fit_live_scores_v1.json",
            ):
                path = data_path(fname)
                if not path.exists():
                    continue
                doc = _json.loads(path.read_text(encoding="utf-8"))
                for row in doc.get("rows") or []:
                    if not isinstance(row, dict):
                        continue
                    keys = {
                        str(row.get("canonical_opportunity_id") or ""),
                        str(row.get("stable_key") or ""),
                    }
                    if cid in keys or str(rec.get("stable_key") or "") in keys:
                        pm = row.get("package_materialization") if isinstance(row.get("package_materialization"), dict) else {}
                        if pm:
                            return _format_product_data_card(pm, row)
        except Exception:
            pass
    return _format_product_data_card(pm, rec)


def _format_product_data_card(pm: dict[str, Any], row: dict[str, Any] | None = None) -> dict[str, Any]:
    row = row or {}
    completeness = str(pm.get("PACKAGE_COMPLETENESS") or "UNKNOWN")
    pkg_label = {
        "COMPLETE": "Complete",
        "LIKELY_COMPLETE": "Complete",
        "PARTIAL": "Partial",
        "DETAIL_ONLY": "Missing",
        "NONE": "Missing",
    }.get(completeness, "Unknown")
    discovered = int(pm.get("PACKAGE_DOCUMENT_COUNT_DISCOVERED") or 0)
    valid = int(pm.get("PACKAGE_DOCUMENT_COUNT_MATERIALIZED") or 0)
    auth = bool(pm.get("AUTHORITATIVE_PRODUCT_DOC_FOUND"))
    auth_doc = pm.get("AUTHORITATIVE_PRODUCT_DOC") if isinstance(pm.get("AUTHORITATIVE_PRODUCT_DOC"), dict) else {}
    lines = int(
        pm.get("EXTRACTED_PRODUCT_LINES")
        or row.get("raw_lines")
        or row.get("material_lines")
        or 0
    )
    expected = int(pm.get("EXPECTED_PRODUCT_LINES") or 0)
    coverage = pm.get("LINE_EXTRACTION_COVERAGE")
    pages = auth_doc.get("product_signal_pages") or []
    pages_s = ""
    if pages:
        pages_s = f"page {pages[0]}" if len(pages) == 1 else f"pages {pages[0]}–{pages[-1]}"
    msg = (
        pm.get("operator_product_status")
        or row.get("package_operator_message")
        or pm.get("operator_message")
        or "Package status not yet assessed."
    )
    if auth and lines and not pm.get("operator_product_status"):
        name = auth_doc.get("filename") or "product schedule"
        msg = f"{lines} product lines found in {name}" + (f", {pages_s}." if pages_s else ".")
        if expected and lines < expected:
            msg = f"Product table detected but extraction incomplete: {lines} of {expected} rows."
    schedule = "Found" if auth else (
        "Locked" if "REGISTRATION" in str(pm.get("package_state_truthful") or "") else "Missing"
    )
    identity_ready = int(row.get("usable_ae") or 0)
    public_priced = int(row.get("public_prices") or 0)
    material = int(row.get("material_lines") or lines or 0)
    unresolved = max(0, material - identity_ready)
    try:
        cov_pct = float(row.get("public_price_coverage_pct") or 0)
    except (TypeError, ValueError):
        cov_pct = round(100.0 * public_priced / max(material, 1), 1) if material else 0.0
    rev_ready = row.get("revenue_state") == "ECONOMIC_REVENUE_USABLE"
    channel = str(row.get("channel_class") or "UNKNOWN")
    headroom = row.get("visible_headroom")
    headroom_pct = row.get("visible_headroom_percent")
    decision = str(row.get("decision") or "")
    if not decision:
        if headroom is not None and float(headroom or 0) > 0 and cov_pct >= 50 and identity_ready >= 3:
            decision = "CALL_TODAY"
        elif rev_ready and public_priced > 0:
            decision = "QUOTE_IF_CAPACITY"
        elif auth and lines > 0:
            decision = "WATCH"
        elif str(pm.get("product_classification") or "") == "PRODUCT_SCHEDULE_INACCESSIBLE":
            decision = "INSUFFICIENT_EVIDENCE"
        else:
            decision = "WATCH"
    plain = str(row.get("blocker_plain") or msg)
    # Prefer operator plain language over engine codes
    if plain and "_" in plain and plain == plain.upper():
        plain = msg
    pipe = row.get("operator_pipeline_status") if isinstance(row.get("operator_pipeline_status"), dict) else {}
    return {
        "package_label": pkg_label,
        "documents_acquired": valid,
        "documents_discovered": discovered,
        "product_schedule": schedule,
        "product_data_status": msg,
        "lines_extracted": lines,
        "expected_lines": expected or None,
        "extraction_coverage": coverage,
        "source_pages": pages_s or None,
        "identity_ready_lines": identity_ready or None,
        "identity_unresolved": unresolved,
        "public_priced_lines": public_priced or None,
        "public_price_coverage_pct": cov_pct,
        "public_basket_value": row.get("public_basket_value")
        or (pipe.get("public_pricing") or {}).get("basket_value"),
        "government_value": "Ready" if rev_ready else "Missing",
        "channel_class": channel,
        "visible_headroom": headroom,
        "visible_headroom_percent": headroom_pct,
        "decision": decision,
        "blocker_plain": plain,
        "authoritative_doc": auth_doc.get("filename"),
        "completeness": completeness,
        "ready_for_line_extraction": bool(pm.get("PACKAGE_READY_FOR_LINE_EXTRACTION")),
        "product_classification": pm.get("product_classification"),
        "product_pipeline_status": pipe or {
            "package": pkg_label,
            "product_document": schedule,
            "lines": {"expected": expected, "extracted": lines, "coverage": coverage},
            "identity": {"ae_count": identity_ready, "unresolved": unresolved},
            "government_value": "Ready" if rev_ready else "Missing",
            "public_pricing": {
                "priced_lines": public_priced,
                "coverage_pct": cov_pct,
                "basket_value": row.get("public_basket_value"),
            },
            "channel": {"class": channel},
            "visible_headroom": {"amount": headroom, "percent": headroom_pct},
            "decision": decision,
            "blocker_plain": plain,
        },
    }


def _profit_first_for_deal(opportunity_id: str | None, rec: dict[str, Any] | None) -> dict[str, Any] | None:
    if not opportunity_id and not rec:
        return None
    try:
        from line_item_economics.engine import load_analysis
        from profit_first.router import evaluate_opportunity_profit

        oid = str(opportunity_id or "")
        if oid.startswith("c:"):
            oid = oid[2:]
        lie = None
        try:
            lie = load_analysis(oid)
        except Exception:
            lie = None
        # Prefer attached evaluation on record
        attached = (rec or {}).get("profit_first") if isinstance((rec or {}).get("profit_first"), dict) else None
        if attached and attached.get("owner_card") and not lie:
            return attached
        ev = evaluate_opportunity_profit(
            opportunity_id=oid or "unknown",
            rec=rec or {},
            title=(rec or {}).get("title"),
            buyer=(rec or {}).get("buyer"),
            line_item_analysis=lie,
            ranking_signals={
                "exact_identity": bool((rec or {}).get("solicitation_event_id")),
                "public_retail_available": bool(lie),
                "multiline_priceable": bool(lie),
            },
        )
        return {
            "profit_status": (ev.get("economics") or {}).get("profit_status"),
            "expected_profit": (ev.get("economics") or {}).get("expected_profit"),
            "post_financing_profit": (ev.get("economics") or {}).get("post_financing_profit"),
            "route": ev.get("route"),
            "owner_card": ev.get("owner_card"),
            "ranking_score": (ev.get("ranking") or {}).get("profit_probability_score"),
            "research_priority": (ev.get("research") or {}).get("priority"),
            "economics": ev.get("economics"),
            "evaluated_at": ev.get("evaluated_at"),
        }
    except Exception:
        return None


def _line_item_economics_for_deal(opportunity_id: str | None, rec: dict[str, Any] | None) -> dict[str, Any] | None:
    if not opportunity_id:
        return None
    try:
        from line_item_economics.engine import load_analysis, owner_summary

        oid = str(opportunity_id)
        # Also try bare canonical without prefix
        for key in (oid, oid[2:] if oid.startswith("c:") else oid):
            analysis = load_analysis(key)
            if analysis:
                return {
                    "owner_summary": owner_summary(analysis),
                    "rollup": analysis.get("rollup"),
                    "line_table": analysis.get("line_table"),
                    "freight": analysis.get("freight"),
                    "supplier_coverage": analysis.get("supplier_coverage"),
                    "simple_resale": analysis.get("simple_resale"),
                    "next_action": analysis.get("next_action"),
                    "analyzed_at": analysis.get("analyzed_at"),
                }
        # Surface embedded schedule if present on record without full analysis
        rr = (rec or {}).get("row_ref") if isinstance((rec or {}).get("row_ref"), dict) else {}
        if rr.get("line_items") or (rec or {}).get("line_items"):
            return {
                "owner_summary": None,
                "pending": True,
                "message": "Line items present — run line-item economics analysis.",
            }
    except Exception:
        return None
    return None


def get_advanced(deal_id: str) -> dict[str, Any]:
    """Lazy-loaded advanced/owner details — not for normal operator flow."""
    detail = get_deal(deal_id)
    cid = detail.get("canonical_id")
    store = _load_store()
    rec = store.get(cid) if cid else {}
    dr = rec.get("deep_research") if isinstance(rec.get("deep_research"), dict) else {}
    return {
        "kind": "OwnerUiAdvanced",
        "deal_id": deal_id,
        "evidence_grades": {
            "gov_clue": dr.get("gov_evidence_clue"),
            "supplier_grade_best": dr.get("supplier_grade_best"),
            "product_identity": dr.get("product_identity"),
        },
        "source_links": {
            "authoritative_url": rec.get("authoritative_url"),
            "platform": rec.get("platform"),
            "source_status": rec.get("source_status"),
        },
        "internal_scoring": {
            "priority_score": rec.get("priority_score"),
            "deal_priority_score": rec.get("deal_priority_score"),
            "fast_research_score": rec.get("fast_research_score"),
        },
        "funnel_state": rec.get("current_funnel_state"),
        "call_gate": rec.get("call_gate"),
        "source_provenance": rec.get("source_provenance"),
        "technical_notes": rec.get("owner_reason"),
        "raw_evidence_refs": rec.get("evidence_references"),
    }


def build_call_workspace(deal_id: str, supplier_id: str | None = None) -> dict[str, Any]:
    detail = get_deal(deal_id)
    suppliers = detail.get("suppliers") or []
    chosen = None
    if supplier_id:
        chosen = next((s for s in suppliers if str(s.get("supplier_id")) == str(supplier_id)), None)
    if not chosen and suppliers:
        # CALL FIRST preferred
        chosen = next((s for s in suppliers if s.get("call_priority") == "CALL FIRST"), suppliers[0])

    from phase_l.l22_supplier_call_desk import (
        MUST_ASK,
        ASK_IF_RELEVANT,
        build_dynamic_questions,
        new_call_session,
        load_call_session,
        save_call_session,
    )

    # Resume existing open session if notes reference one
    notes = (_notes_store().get("by_deal") or {}).get(deal_id) or {}
    session = None
    if notes.get("session_id"):
        session = load_call_session(notes["session_id"])

    req = {
        "manufacturer": None,
        "model": detail["what"].get("product"),
        "quantity": detail["what"].get("quantity") or 1,
        "uom": "EA",
        "delivery_destination": detail["what"].get("delivery_requirement"),
        "required_delivery_date": detail["what"].get("delivery_requirement"),
        "product_specification": detail["what"].get("product"),
    }
    supplier_dict = {
        "supplier_domain": (chosen or {}).get("supplier_id"),
        "name": (chosen or {}).get("company"),
        "supplier_grade": (chosen or {}).get("grade"),
        "product_fit": "PARTIAL",
        "authorization_state": "UNKNOWN",
        "contact": {"phone": (chosen or {}).get("phone")},
    }
    questions = build_dynamic_questions(req, supplier_dict)
    if not session:
        sheet = {
            "opportunity_id": detail.get("opportunity_id") or detail.get("canonical_id") or deal_id,
            "supplier_id": (chosen or {}).get("supplier_id") or "unknown",
            "supplier_name": (chosen or {}).get("company"),
            "public_contact_person": (chosen or {}).get("contact"),
            "phone_number": (chosen or {}).get("phone"),
            "rfq_url": (chosen or {}).get("rfq_url"),
            "product": detail["what"].get("product"),
            "quantity": detail["what"].get("quantity"),
            "buyer": detail["what"].get("buyer"),
            "solicitation": detail["what"].get("solicitation"),
            "deadline": detail["what"].get("deadline"),
            "required_delivery_date": detail["what"].get("delivery_requirement"),
            "why_selected": (chosen or {}).get("why") or detail["next"].get("reason"),
            "opening_script": None,
            "supplier_grade": (chosen or {}).get("grade"),
            "authorization_status": None,
            "supplier_call_priority": {"band": (chosen or {}).get("call_priority_band")},
        }
        session = new_call_session(sheet, questions)
        # Persist immediately so answer saves / resume work without a prior Save click
        save_call_session(session)
        nstore = _notes_store()
        by = nstore.setdefault("by_deal", {})
        by[deal_id] = {
            **(by.get(deal_id) or {}),
            "session_id": session["session_id"],
            "updated_at": _utc(),
        }
        _save_notes(nstore)

    must = [q for q in session.get("questions") or [] if q.get("priority_band") == MUST_ASK]
    relevant = [q for q in session.get("questions") or [] if q.get("priority_band") == ASK_IF_RELEVANT]

    return {
        "kind": "OwnerUiCallWorkspace",
        "build": BUILD,
        "deal_id": deal_id,
        "session_id": session.get("session_id"),
        "saved_at": session.get("saved_at"),
        "supplier": {
            "company": (chosen or {}).get("company") or session.get("supplier_name"),
            "phone": (chosen or {}).get("phone") or session.get("phone_number"),
            "contact": (chosen or {}).get("contact") or session.get("contact_person"),
            "rfq_url": (chosen or {}).get("rfq_url"),
            "why": (chosen or {}).get("why") or detail["next"].get("reason"),
            "call_priority": (chosen or {}).get("call_priority") or "CALL FIRST",
        },
        "product": {
            "exact_item": detail["what"].get("product"),
            "quantity": detail["what"].get("quantity"),
            "required_delivery": detail["what"].get("delivery_requirement"),
            "buyer": detail["what"].get("buyer"),
            "solicitation": detail["what"].get("solicitation"),
            "deadline": detail["what"].get("deadline"),
        },
        "what_to_ask": {"MUST_ASK": must, "ASK_IF_RELEVANT": relevant},
        "answers": session.get("answers") or [],
        "questions": session.get("questions") or [],
        "call_notes": session.get("call_notes") or notes.get("general_notes") or "",
        "follow_up": session.get("follow_up") or {},
        "still_needed": [],
        "training_hints": [
            "Start here.",
            "Call this supplier.",
            "Enter the price they give you.",
            "Save when finished.",
        ],
        "session": session,
        "other_suppliers": [
            {"supplier_id": s.get("supplier_id"), "company": s.get("company"), "call_priority": s.get("call_priority")}
            for s in suppliers
            if s is not chosen
        ],
    }


def save_call_answers(
    deal_id: str,
    *,
    session_id: str,
    answers: list[dict[str, Any]] | None = None,
    call_notes: str | None = None,
    promised_quote_date: str | None = None,
    complete: bool = False,
    outcome: str | None = None,
) -> dict[str, Any]:
    from phase_l.l22_supplier_call_desk import (
        complete_call,
        load_call_session,
        missing_critical_answers,
        record_answer,
        save_call_session,
        QUOTE_PROMISED,
    )

    session = load_call_session(session_id)
    if not session:
        return {"ok": False, **operator_safe_error("session_not_found"), "preserved": True}

    try:
        for a in answers or []:
            if not isinstance(a, dict) or not a.get("question_id"):
                continue
            record_answer(
                session,
                a["question_id"],
                a.get("answer_value"),
                owner_note=a.get("owner_note"),
                source=a.get("source") or "PHONE",
            )
        if call_notes is not None:
            session["call_notes"] = call_notes
        if promised_quote_date:
            session["follow_up"] = {
                **(session.get("follow_up") or {}),
                "follow_up_required": True,
                "promised_quote_date": promised_quote_date,
                "follow_up_date": promised_quote_date,
                "reason": "Supplier promised quote",
            }
            if not outcome:
                outcome = QUOTE_PROMISED

        still = missing_critical_answers(session)
        if complete:
            result = complete_call(
                session,
                outcome=outcome or "SPOKE_TO_SUPPLIER",
                override_reason="operator_mark_complete" if still else None,
                follow_up=session.get("follow_up"),
            )
            if not result.get("ok"):
                return {
                    "ok": False,
                    "still_needed": result.get("still_needed") or still,
                    "message": "Still needed before complete — or use Mark Complete with notes.",
                    "saved_at": None,
                    "preserved": True,
                }
            saved_at = (result.get("session") or {}).get("saved_at")
        else:
            saved = save_call_session(session)
            saved_at = saved.get("saved_at")

        # Operator notes index for Today overlays
        nstore = _notes_store()
        by = nstore.setdefault("by_deal", {})
        by[deal_id] = {
            **(by.get(deal_id) or {}),
            "session_id": session_id,
            "general_notes": session.get("call_notes"),
            "promised_quote_date": (session.get("follow_up") or {}).get("promised_quote_date"),
            "call_status": "QUOTE_PROMISED" if promised_quote_date else ("COMPLETE" if complete else "IN_PROGRESS"),
            "updated_at": _utc(),
        }
        _save_notes(nstore)

        return {
            "ok": True,
            "saved": True,
            "saved_at": saved_at or _utc(),
            "still_needed": still,
            "session_id": session_id,
            "message": "Saved",
            "follow_up_queued": bool(promised_quote_date),
        }
    except Exception as exc:
        return {"ok": False, **operator_safe_error(exc), "preserved": True}


def build_quotes() -> dict[str, Any]:
    qstore = _quotes_store()
    buckets = {
        "NEW QUOTES": [],
        "NEEDS REVIEW": [],
        "GOOD PRICE": [],
        "MARGINAL": [],
        "TOO HIGH": [],
        "DELIVERY FAIL": [],
    }
    for q in qstore.get("quotes") or []:
        if not isinstance(q, dict):
            continue
        bucket = q.get("bucket") or "NEW QUOTES"
        if bucket not in buckets:
            bucket = "NEEDS REVIEW"
        buckets[bucket].append(q)

    # Seed empty sections still present for UI structure
    today = build_today()
    for item in (today.get("sections") or {}).get("quotes", {}).get("items") or []:
        if item.get("quote"):
            continue
        # synthetic needs-review placeholder from funnel
        buckets["NEEDS REVIEW"].append(
            {
                "deal_id": item.get("deal_id"),
                "buyer": item.get("buyer"),
                "product": item.get("product"),
                "bucket": "NEEDS REVIEW",
                "unit_price": None,
                "recommendation": None,
            }
        )

    return {
        "kind": "OwnerUiQuotes",
        "build": BUILD,
        "generated_at": _utc(),
        "sections": [{"title": k, "items": v, "empty": f"No items in {k.lower()}."} for k, v in buckets.items()],
        "count": sum(len(v) for v in buckets.values()),
    }


def get_quote_review(deal_id: str) -> dict[str, Any]:
    qstore = _quotes_store()
    quote = next((q for q in (qstore.get("quotes") or []) if q.get("deal_id") == deal_id), None)
    detail = get_deal(deal_id)
    notes = (_notes_store().get("by_deal") or {}).get(deal_id) or {}
    session_answers = {}
    if notes.get("session_id"):
        from phase_l.l22_supplier_call_desk import load_call_session

        sess = load_call_session(notes["session_id"]) or {}
        for a in sess.get("answers") or []:
            session_answers[a.get("question_id")] = a.get("answer_value")

    unit = (quote or {}).get("unit_price") or session_answers.get("unit_price")
    freight = (quote or {}).get("freight") or session_answers.get("freight_amount") or 0
    qty = detail["what"].get("quantity") or 1
    try:
        landed = float(unit or 0) * float(qty) + float(freight or 0)
    except (TypeError, ValueError):
        landed = None

    recommendation = (quote or {}).get("recommendation")
    if not recommendation and unit is not None:
        recommendation = "GET ANOTHER QUOTE"

    return {
        "kind": "OwnerUiQuoteReview",
        "deal_id": deal_id,
        "supplier_quote": {
            "unit_price": unit,
            "freight": freight,
            "total_landed": landed,
            "lead_time": (quote or {}).get("lead_time") or session_answers.get("lead_time"),
            "terms": (quote or {}).get("terms") or session_answers.get("payment_terms"),
            "quote_expiration": (quote or {}).get("quote_expiration") or session_answers.get("quote_expiration"),
        },
        "deal_economics": {
            "owner_only": True,
            "expected_revenue": (quote or {}).get("expected_revenue"),
            "landed_cost": landed,
            "financing": (quote or {}).get("financing"),
            "expected_profit": (quote or {}).get("expected_profit"),
            "margin": (quote or {}).get("margin"),
            "max_buy": (quote or {}).get("max_buy"),
            "note": "Economics shown only when evidence is defensible.",
        },
        "recommendation": recommendation or "GET ANOTHER QUOTE",
        "recommendation_options": ["BID", "GET ANOTHER QUOTE", "NEGOTIATE", "SKIP"],
        "deal": detail,
        "comparison": {
            "columns": ["supplier", "landed_cost", "delivery", "terms", "expected_profit", "recommendation"],
            "rows": [
                {
                    "supplier": (quote or {}).get("supplier") or (detail.get("suppliers") or [{}])[0].get("company"),
                    "landed_cost": landed,
                    "delivery": session_answers.get("delivery_feasible"),
                    "terms": session_answers.get("payment_terms"),
                    "expected_profit": (quote or {}).get("expected_profit"),
                    "recommendation": recommendation or "GET ANOTHER QUOTE",
                }
            ],
        },
    }


def upsert_quote_review(deal_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    qstore = _quotes_store()
    quotes = qstore.setdefault("quotes", [])
    existing = next((q for q in quotes if q.get("deal_id") == deal_id), None)
    row = {
        **(existing or {}),
        **payload,
        "deal_id": deal_id,
        "updated_at": _utc(),
        "bucket": payload.get("bucket") or (existing or {}).get("bucket") or "NEEDS REVIEW",
    }
    if existing:
        quotes[quotes.index(existing)] = row
    else:
        quotes.append(row)
    qstore["updated_at"] = _utc()
    save_json(_ui_quotes_path(), qstore)
    return {"ok": True, "saved": True, "saved_at": row["updated_at"], "message": "Saved", "quote": row}


def build_registrations() -> dict[str, Any]:
    from phase_l.unlock_opportunity_links import summarize_unlock_links

    tracker = load_tracker()
    meta = _canonical_store_meta()
    store = _load_store() if meta["exists"] else {}
    items = []
    all_blocked_ids: set[str] = set()
    for sid, row in (tracker.get("portals") or {}).items():
        if not isinstance(row, dict):
            continue
        status = str(row.get("registration_status") or "")
        if status == "REGISTERED":
            continue
        action = str(row.get("recommended_action") or "")
        if action in {"NONE", ""} and not row.get("currently_live_relevant_opportunities"):
            # still show recurring buyers with sightings
            if int(row.get("relevant_opportunities_seen") or 0) < 2:
                continue

        links = summarize_unlock_links(portal_id=sid, portal_row=row, store=store)
        opp_ids = list(links.get("opportunity_ids") or [])
        opp_count = int(links.get("opportunity_count") or 0)
        # Never fabricate fallback capped counts when store is missing
        if not meta["exists"]:
            opp_count = 0
            opp_ids = []
        buyers_unlocked = max(1, int(links.get("buyer_count") or 1))
        free = str(row.get("registration_type") or "").startswith("EASY")
        priority = "HIGH" if action.endswith("RECURRING_BUYER") or opp_count >= 3 else ("NORMAL" if opp_count >= 1 or int(row.get("relevant_opportunities_seen") or 0) >= 2 else "LOW")
        all_blocked_ids.update(opp_ids)
        items.append(
            {
                "portal_id": sid,
                "portal": row.get("buyer_name") or row.get("portal") or sid,
                "state_jurisdiction": row.get("jurisdiction"),
                "free_or_paid": "Free" if free else "May require documents",
                "estimated_time": "~10 minutes" if free else "~20–40 minutes",
                "buyers_unlocked": buyers_unlocked,
                "buyer_ids": links.get("buyer_ids") or [],
                "opportunities_unlocked": opp_count,
                "opportunity_ids": opp_ids,
                "opportunity_count": opp_count,
                "priority": priority,
                "why": (
                    f"Unlocks {buyers_unlocked} buyers · {opp_count} relevant opportunities"
                    if meta["exists"]
                    else "Opportunity store missing — count unavailable (not fabricated)."
                ),
                "why_registration_matters": row.get("recommended_action") or "Register to unlock buyers and current opportunities.",
                "registration_url": row.get("registration_url"),
                "walkthrough": {
                    "steps": [
                        {"n": 1, "title": "Where to go", "body": row.get("registration_url") or "Open the buyer portal registration page."},
                        {"n": 2, "title": "What you need", "body": "Business name, email, phone, and basic company profile."},
                        {"n": 3, "title": "What to enter", "body": "Create vendor account; complete profile; confirm email if asked."},
                        {"n": 4, "title": "Confirm", "body": "After you finish, mark registered below."},
                    ]
                },
                "recommended_action": action or "REGISTER_NOW",
                "data_status": "LOADED" if meta["exists"] else DATA_SOURCE_MISSING,
            }
        )
    items.sort(key=lambda x: (0 if x["priority"] == "HIGH" else 1 if x["priority"] == "NORMAL" else 2, -x["opportunities_unlocked"]))
    return {
        "kind": "OwnerUiRegistrations",
        "build": BUILD,
        "generated_at": _utc(),
        "items": items,
        "count": len(items),
        "unique_blocked_opportunities": len(all_blocked_ids),
        "data_status": "LOADED" if meta["exists"] else DATA_SOURCE_MISSING,
        "empty": "No registrations need attention right now.",
    }


def registration_opportunity_drilldown(portal_id: str) -> dict[str, Any]:
    """Exact unique opportunity IDs behind an unlock card count."""
    from phase_l.unlock_opportunity_links import summarize_unlock_links

    tracker = load_tracker()
    row = (tracker.get("portals") or {}).get(portal_id)
    if not isinstance(row, dict):
        return {"ok": False, "error": "portal_not_found", "portal_id": portal_id}
    meta = _canonical_store_meta()
    if not meta["exists"]:
        return {
            "ok": False,
            "error": OPPORTUNITY_STORE_MISSING,
            "data_status": DATA_SOURCE_MISSING,
            "portal_id": portal_id,
            "opportunity_ids": [],
            "opportunity_count": 0,
            "message": "Cannot derive unlock opportunities — store missing.",
        }
    store = _load_store()
    links = summarize_unlock_links(portal_id=portal_id, portal_row=row, store=store)
    cards = []
    for cid in links.get("opportunity_ids") or []:
        rec = store.get(cid)
        if isinstance(rec, dict):
            cards.append(_available_deal_card(cid, rec))
    return {
        "ok": True,
        "kind": "OwnerUiRegistrationDrilldown",
        "portal_id": portal_id,
        "portal": row.get("buyer_name") or row.get("portal") or portal_id,
        "opportunity_ids": links.get("opportunity_ids") or [],
        "opportunity_count": links.get("opportunity_count") or 0,
        "buyer_ids": links.get("buyer_ids") or [],
        "items": cards,
        "count_matches_ids": len(links.get("opportunity_ids") or []) == int(links.get("opportunity_count") or 0),
    }


def opportunity_data_health() -> dict[str, Any]:
    meta = _canonical_store_meta()
    if not meta["exists"]:
        return {
            "kind": "OpportunityDataHealth",
            "status": DATA_SOURCE_MISSING,
            "error_code": OPPORTUNITY_STORE_MISSING,
            "canonical_count": None,
            "currently_available": None,
            "last_updated": None,
            "store_path": meta["path"],
            "message": "Opportunity store missing — do not treat as zero legitimate opportunities.",
            "registration_unlocks": None,
            "unique_blocked_opportunities": None,
        }
    store = _load_store()
    available = sum(1 for r in store.values() if isinstance(r, dict) and _is_available_rec(r))
    regs = build_registrations()
    return {
        "kind": "OpportunityDataHealth",
        "status": "LOADED",
        "canonical_count": len(store),
        "currently_available": available,
        "last_updated": meta.get("updated_at"),
        "store_path": meta["path"],
        "registration_unlocks": regs.get("count"),
        "unique_blocked_opportunities": regs.get("unique_blocked_opportunities"),
        "message": None,
    }


def opportunity_visibility_diagnostics() -> dict[str, Any]:
    """Production diagnostic for opportunity visibility + unlock integrity."""
    from collections import Counter

    from m3_data_root import get_data_root

    meta = _canonical_store_meta()
    root = str(get_data_root())
    if not meta["exists"]:
        return {
            "kind": "OpportunityVisibilityDiagnostics",
            "build": BUILD,
            "data_root": root,
            "store_path": meta["path"],
            "file_exists": False,
            "data_status": DATA_SOURCE_MISSING,
            "error_code": OPPORTUNITY_STORE_MISSING,
            "record_count": 0,
            "canonical_count": 0,
            "note": "Missing store is not a legitimate empty population.",
        }
    store = _load_store()
    by_status = Counter(str(r.get("current_funnel_state") or "UNKNOWN") for r in store.values() if isinstance(r, dict))
    by_class = Counter(str(r.get("product_service_classification") or "UNKNOWN") for r in store.values() if isinstance(r, dict))
    by_jur = Counter(_jurisdiction_bucket(r) for r in store.values() if isinstance(r, dict))
    available = sum(1 for r in store.values() if isinstance(r, dict) and _is_available_rec(r))
    today = build_today()
    deals = list_deals(filter="available", page=1, page_size=1)
    watch = build_watch(page=1, page_size=1)
    regs = build_registrations()
    unlock_examples = [
        {
            "portal_id": i.get("portal_id"),
            "portal": i.get("portal"),
            "opportunity_count": i.get("opportunity_count"),
            "sample_ids": (i.get("opportunity_ids") or [])[:5],
        }
        for i in (regs.get("items") or [])[:8]
    ]
    return {
        "kind": "OpportunityVisibilityDiagnostics",
        "build": BUILD,
        "data_root": root,
        "store_path": meta["path"],
        "file_exists": True,
        "data_status": "LOADED",
        "record_count": len(store),
        "canonical_count": len(store),
        "currently_available": available,
        "counts_by_status": dict(by_status),
        "counts_by_product_service_classification": dict(by_class),
        "counts_by_jurisdiction": dict(by_jur),
        "visible_today_call_today": (today.get("counts") or {}).get("call_today"),
        "visible_today_blocked": (today.get("counts") or {}).get("blocked"),
        "visible_deals_available": deals.get("total"),
        "visible_watch": watch.get("total"),
        "last_updated": meta.get("updated_at"),
        "registration_unlocks": regs.get("count"),
        "unique_blocked_opportunities": regs.get("unique_blocked_opportunities"),
        "unlock_examples": unlock_examples,
    }


def mark_registered(portal_id: str, *, confirmation: str | None = None) -> dict[str, Any]:
    tracker = load_tracker()
    portals = tracker.setdefault("portals", {})
    row = portals.get(portal_id)
    if not isinstance(row, dict):
        return {"ok": False, "message": "Registration task not found.", "preserved": True}
    row["registration_status"] = "REGISTERED"
    row["registered_at"] = _utc()
    row["confirmation_note"] = confirmation
    row["recommended_action"] = "NONE"
    save_tracker(tracker)
    return {"ok": True, "saved": True, "saved_at": row["registered_at"], "message": "Saved"}


def build_blocked(*, page: int = 1, page_size: int = 40) -> dict[str, Any]:
    store = _load_store()
    page = max(1, int(page or 1))
    page_size = min(100, max(1, int(page_size or 40)))
    items = []
    for cid, rec in store.items():
        if rec.get("current_funnel_state") != "WATCH_FEDERAL_ACCESS":
            continue
        status = map_funnel_to_owner_status(rec)
        items.append(
            _deal_card(
                deal_id=f"c:{cid}",
                buyer=rec.get("buyer"),
                solicitation=rec.get("solicitation_event_id"),
                product=_product_from_rec(rec),
                deadline=rec.get("deadline"),
                delivery=None,
                estimated_value=None,
                status_packet=status,
                extras={
                    "blocker": (status.get("blocked") or {}).get("blocker"),
                    "plain": (status.get("blocked") or {}).get("plain"),
                    "resolves_with": (status.get("blocked") or {}).get("resolves_with"),
                    "owner_action_required": (status.get("blocked") or {}).get("owner_action_required"),
                    "operator_note": "Waiting on CAGE. No action required inside M3."
                    if "CAGE" in str((status.get("blocked") or {}).get("blocker") or "")
                    else (status.get("blocked") or {}).get("plain"),
                },
            )
        )
    total = len(items)
    start = (page - 1) * page_size
    return {
        "kind": "OwnerUiBlocked",
        "build": BUILD,
        "page": page,
        "page_size": page_size,
        "total": total,
        "has_more": start + page_size < total,
        "items": items[start : start + page_size],
        "empty": "Nothing blocked that needs your attention.",
    }


def build_watch(*, page: int = 1, page_size: int = 40) -> dict[str, Any]:
    store = _load_store()
    page = max(1, int(page or 1))
    page_size = min(100, max(1, int(page_size or 40)))
    items = []
    for cid, rec in store.items():
        if rec.get("current_funnel_state") not in {"WATCH", "WATCH_OTHER", "DEEP_RESEARCH_COMPLETE", "LOW_PRIORITY_RESEARCH"}:
            continue
        if rec.get("current_funnel_state") == "DEEP_RESEARCH_COMPLETE" and (rec.get("call_gate") or {}).get("ready"):
            continue
        status = map_funnel_to_owner_status(rec)
        if status["ui_status"] != WATCH:
            continue
        items.append(
            _deal_card(
                deal_id=f"c:{cid}",
                buyer=rec.get("buyer"),
                solicitation=rec.get("solicitation_event_id"),
                product=_product_from_rec(rec),
                deadline=rec.get("deadline"),
                delivery=None,
                estimated_value=None,
                status_packet=status,
                extras={
                    "why_watching": status["ui_next_action_reason"],
                    "moves_forward_when": rec.get("recheck_trigger") or "Better evidence, closer deadline, or stronger supplier path.",
                },
            )
        )
    total = len(items)
    start = (page - 1) * page_size
    return {
        "kind": "OwnerUiWatch",
        "build": BUILD,
        "page": page,
        "page_size": page_size,
        "total": total,
        "has_more": start + page_size < total,
        "items": items[start : start + page_size],
        "empty": "Watch list is empty.",
    }


def build_bid_prep() -> dict[str, Any]:
    store = _load_store()
    items = []
    bid_states = {"READY_TO_BID", "READY_FOR_FINAL_ECONOMICS", "QUOTES_RECEIVED"}
    ready_call = []
    for cid, rec in store.items():
        st = rec.get("current_funnel_state")
        if st not in bid_states and st != "READY_TO_CALL":
            continue
        status = map_funnel_to_owner_status(rec)
        base = {
            **_deal_card(
                deal_id=f"c:{cid}",
                buyer=rec.get("buyer"),
                solicitation=rec.get("solicitation_event_id"),
                product=_product_from_rec(rec),
                deadline=rec.get("deadline"),
                delivery=None,
                estimated_value=None,
                status_packet=status,
            ),
            "checklist": [
                {"id": "quote", "label": "Supplier quote selected", "done": st in {"READY_TO_BID", "QUOTES_RECEIVED"}},
                {"id": "delivery", "label": "Delivery commitment confirmed", "done": False},
                {"id": "terms", "label": "Payment terms acceptable", "done": False},
                {"id": "registration", "label": "Portal registration complete", "done": rec.get("registration_status") == "REGISTERED"},
                {"id": "package", "label": "Bid package drafted", "done": False},
            ],
            "missing_items": ["Bid package not started"],
            "quote_selected": None,
            "expected_profit": None,
            "submission_method": rec.get("submission_path") or "Portal upload / email per solicitation",
            "primary_action": "START BID PREP",
            "canonical_id": cid,
            "_funnel": st,
        }
        try:
            from response_engine.service import bid_prep_card_for_opportunity

            base = bid_prep_card_for_opportunity(cid, base)
        except Exception:
            pass
        if st in bid_states:
            items.append(base)
        else:
            ready_call.append(base)

    # Prefer true bid-ready; fill with READY_TO_CALL for foundation work
    items.extend(ready_call)

    def _rank(it: dict[str, Any]) -> tuple:
        st = (it.get("r1") or {}).get("status") or ""
        funnel_boost = 0 if it.get("_funnel") in bid_states else 1
        return (0 if it.get("response_project_id") else 1, funnel_boost, st)

    items.sort(key=_rank)
    for it in items:
        it.pop("_funnel", None)
    items = items[:40]
    return {
        "kind": "OwnerUiBidPrep",
        "build": BUILD,
        "generated_at": _utc(),
        "items": items,
        "count": len(items),
        "empty": "No deals ready for bid prep.",
        "note": "R1 foundation: documents → requirements → compliance matrix. No submission yet.",
    }


def global_search(q: str, *, limit: int = 30) -> dict[str, Any]:
    qn = _norm(q)
    if not qn or len(qn) < 2:
        return {"kind": "OwnerUiSearch", "query": q, "items": []}
    store = _load_store()
    today = _load_today_calls()
    items = []
    for entry in today.get("entries") or []:
        hay = _norm(" ".join(str(entry.get(k) or "") for k in ("buyer", "solicitation", "opportunity", "product", "supplier")))
        if qn in hay:
            items.append(
                {
                    "deal_id": f"l22:{entry.get('opportunity_id')}",
                    "type": "call",
                    "label": f"{entry.get('buyer')} — {entry.get('product') or entry.get('opportunity')}",
                    "sub": entry.get("supplier"),
                }
            )
    for cid, rec in store.items():
        hay = _norm(
            " ".join(
                str(x or "")
                for x in (rec.get("buyer"), rec.get("title"), rec.get("solicitation_event_id"), _product_from_rec(rec))
            )
        )
        # suppliers
        for s in _suppliers_from_rec(rec):
            hay += " " + _norm(s.get("company"))
        if qn in hay:
            status = map_funnel_to_owner_status(rec)
            items.append(
                {
                    "deal_id": f"c:{cid}",
                    "type": "deal",
                    "label": f"{rec.get('buyer')} — {_product_from_rec(rec)}",
                    "sub": status["ui_status"],
                }
            )
        if len(items) >= limit:
            break
    return {"kind": "OwnerUiSearch", "query": q, "items": items[:limit]}


def settings_payload() -> dict[str, Any]:
    return {
        "kind": "OwnerUiSettings",
        "build": BUILD,
        "operator": {
            "training_mode_default": False,
            "show_hints": True,
        },
        "advanced_note": "Infrastructure and research controls live under System / Advanced — not required for daily work.",
        "links": {
            "legacy_research_ui": "/index.html?legacy=1",
            "call_desk_artifact": "/supplier_call_desk.html",
            "quickstart": "/docs/M3_OPERATOR_QUICKSTART.md",
        },
    }


def preservation_snapshot() -> dict[str, Any]:
    """Confirm current production state is still readable for UI."""
    store = _load_store()
    ready = sum(1 for r in store.values() if r.get("current_funnel_state") == "READY_TO_CALL")
    federal = sum(1 for r in store.values() if r.get("current_funnel_state") == "WATCH_FEDERAL_ACCESS")
    today = _load_today_calls()
    return {
        "canonical_rows": len(store),
        "ready_to_call": ready,
        "watch_federal_access": federal,
        "l22_today_calls": int(today.get("count") or len(today.get("entries") or [])),
        "registration_portals": len((load_tracker().get("portals") or {})),
        "preserved": ready >= 48 and len(store) >= 1600,
    }
