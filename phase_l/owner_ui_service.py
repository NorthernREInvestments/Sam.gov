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
DATA = ROOT / "data"
BUILD = "20260929-m3-owner-ui-15-minute-operator-training"

# Operator session notes (does not alter canonical funnel without explicit complete)
UI_NOTES_PATH = DATA / "owner_ui_operator_notes.json"
UI_QUOTES_PATH = DATA / "owner_ui_quote_reviews.json"


def _utc() -> str:
    return now_utc().isoformat()


def _load_store() -> dict[str, Any]:
    from phase_l.l23_full_population_funnel import load_store

    return load_store()


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
    if UI_NOTES_PATH.exists():
        try:
            return load_json(UI_NOTES_PATH)
        except Exception:
            pass
    return {"kind": "OwnerUiOperatorNotes", "by_deal": {}, "updated_at": None}


def _save_notes(store: dict[str, Any]) -> None:
    store["updated_at"] = _utc()
    store["kind"] = "OwnerUiOperatorNotes"
    save_json(UI_NOTES_PATH, store)


def _quotes_store() -> dict[str, Any]:
    if UI_QUOTES_PATH.exists():
        try:
            return load_json(UI_QUOTES_PATH)
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
    return {
        "kind": "OwnerUiHome",
        "build": BUILD,
        "generated_at": _utc(),
        "headline": "WHAT NEEDS MY ATTENTION TODAY?",
        "cards": [
            {"id": "call_today", "label": "CALL TODAY", "count": counts["call_today"], "color": "green", "href": "#/today/call_today"},
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
        "caught_up": active == 0,
        "caught_up_message": "You're caught up." if active == 0 else None,
        "next_useful": "Review Watch list for future opportunities." if active == 0 else "Open Today and work the top CALL TODAY item.",
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
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    store = _load_store()
    page = max(1, int(page or 1))
    page_size = min(100, max(1, int(page_size or 50)))
    qn = _norm(q)
    buyer_n = _norm(buyer)
    status_n = (status or "").strip().upper()

    cards: list[dict[str, Any]] = []
    # Prefer actionable states; paginate
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
        status_packet = map_funnel_to_owner_status(rec)
        if status_n and status_packet["ui_status"] != status_n and status_packet["ui_status"].replace(" ", "_") != status_n.replace(" ", "_"):
            # also allow queue ids
            if status_packet.get("ui_queue") != status_n.lower() and status_packet["ui_status"] != status_n:
                continue
        if buyer_n and buyer_n not in _norm(rec.get("buyer")):
            continue
        if state and state.upper() not in _norm(rec.get("jurisdiction") or rec.get("buyer") or ""):
            # soft filter on jurisdiction text
            if state.upper() not in str(rec.get("jurisdiction") or "").upper():
                continue
        hay = " ".join(
            str(x or "")
            for x in (rec.get("buyer"), rec.get("title"), rec.get("solicitation_event_id"), _product_from_rec(rec))
        )
        if qn and qn not in _norm(hay):
            continue
        cards.append(
            _deal_card(
                deal_id=f"c:{cid}",
                buyer=rec.get("buyer"),
                solicitation=rec.get("solicitation_event_id"),
                product=_product_from_rec(rec),
                deadline=rec.get("deadline"),
                delivery=None,
                estimated_value=None,
                status_packet=status_packet,
                extras={"canonical_id": cid},
            )
        )

    total = len(cards)
    start = (page - 1) * page_size
    slice_ = cards[start : start + page_size]
    return {
        "kind": "OwnerUiDealList",
        "build": BUILD,
        "page": page,
        "page_size": page_size,
        "total": total,
        "has_more": start + page_size < total,
        "items": slice_,
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
    return {
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
    }


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
    save_json(UI_QUOTES_PATH, qstore)
    return {"ok": True, "saved": True, "saved_at": row["updated_at"], "message": "Saved", "quote": row}


def build_registrations() -> dict[str, Any]:
    tracker = load_tracker()
    items = []
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
        buyers_unlocked = 1  # portal maps to recurring buyer entity
        live = int(row.get("currently_live_relevant_opportunities") or 0)
        seen = int(row.get("relevant_opportunities_seen") or 0)
        free = str(row.get("registration_type") or "").startswith("EASY")
        priority = "HIGH" if action.endswith("RECURRING_BUYER") or live >= 3 else ("NORMAL" if seen >= 2 else "LOW")
        items.append(
            {
                "portal_id": sid,
                "portal": row.get("buyer_name") or row.get("portal") or sid,
                "free_or_paid": "Free" if free else "May require documents",
                "estimated_time": "~10 minutes" if free else "~20–40 minutes",
                "buyers_unlocked": buyers_unlocked,
                "opportunities_unlocked": max(live, min(seen, 9)),
                "priority": priority,
                "why": f"Unlocks {buyers_unlocked} buyers · {max(live, min(seen, 9))} relevant opportunities",
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
            }
        )
    items.sort(key=lambda x: (0 if x["priority"] == "HIGH" else 1 if x["priority"] == "NORMAL" else 2, -x["opportunities_unlocked"]))
    return {
        "kind": "OwnerUiRegistrations",
        "build": BUILD,
        "generated_at": _utc(),
        "items": items,
        "count": len(items),
        "empty": "No registrations need attention right now.",
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
