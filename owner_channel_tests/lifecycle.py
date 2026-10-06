"""Phases 8–15 — Sent state, response tracking, real quote ingest + auto-wire."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from application_clock import now_utc
from m3_data_root import data_path
from owner_channel_tests.models import (
    ACCOUNT_SETUP_REQUIRED,
    ALTERNATE_PRODUCT_OFFERED,
    AMBIGUOUS,
    CHANNEL_CONFIRMED,
    CHANNEL_FAILED,
    EXACT_MATCH,
    OUTCOMES,
    PACK_CONVERSION_VALID,
    PARTIAL_RESPONSE,
    QUOTE_RECEIVED,
    QUOTE_REQUEST_SENT,
    QUOTE_STORE,
    REAL_SUPPLIER_QUOTE,
    REFERRED,
    RESPONSE_STORE,
    SENT,
    SENT_STORE,
    SUPPLIER_CHANNEL_PROOF_ONLY,
    TEST_FIXTURE_ONLY,
    UNMATCHED,
    WAITING,
    WAITING_FOR_SUPPLIER_QUOTE,
)
from p0_prescale_hardening.quote_pipeline import (
    init_basket_from_packet,
    process_real_supplier_quote,
    production_gate_quote,
)


def _load(name: str) -> dict[str, Any]:
    p = data_path(name)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(name: str, obj: Any) -> None:
    data_path(name).write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")


def mark_quote_request_sent(
    *,
    packet_id: str,
    supplier: str,
    method: str,
    contact_used: str | None,
    requested_response_date: str | None = None,
    owner_notes: str | None = None,
) -> dict[str, Any]:
    store = _load(SENT_STORE)
    store.setdefault("by_packet", {})
    row = {
        "packet_id": packet_id,
        "supplier": supplier,
        "sent_timestamp": now_utc().isoformat(),
        "method": method,
        "contact_used": contact_used,
        "owner_notes": owner_notes,
        "requested_response_date": requested_response_date,
        "state": QUOTE_REQUEST_SENT,
        "next_state": WAITING_FOR_SUPPLIER_QUOTE,
        "channel_test_state": SENT,
        "do_not_send_automatically": True,
        "auto_sent": False,
    }
    store["by_packet"][packet_id] = row
    store["updated_at"] = now_utc().isoformat()
    _save(SENT_STORE, store)
    try:
        from p1_prescale_hardening.quote_observability import append_event

        append_event(
            event_type="REQUEST_MARKED_SENT",
            packet=packet_id,
            supplier=supplier,
            actor="owner",
            reason="owner marked quote request sent externally",
            new_state=QUOTE_REQUEST_SENT,
        )
    except Exception:
        pass
    return row


def record_response(
    *,
    packet_id: str,
    outcome: str,
    owner_notes: str | None = None,
) -> dict[str, Any]:
    if outcome not in OUTCOMES:
        outcome = "OTHER"
    store = _load(RESPONSE_STORE)
    store.setdefault("by_packet", {})
    row = {
        "packet_id": packet_id,
        "outcome": outcome,
        "recorded_at": now_utc().isoformat(),
        "owner_notes": owner_notes,
        "original_packet_preserved": True,
    }
    # Map to channel test state
    if outcome == "QUOTE_RECEIVED":
        row["channel_test_state"] = QUOTE_RECEIVED
    elif outcome == "PARTIAL_QUOTE_RECEIVED":
        row["channel_test_state"] = PARTIAL_RESPONSE
    elif outcome == "REFERRED_TO_DISTRIBUTOR":
        row["channel_test_state"] = REFERRED
    elif outcome == "ACCOUNT_REQUIRED":
        row["channel_test_state"] = ACCOUNT_SETUP_REQUIRED
    elif outcome in {"DECLINED_TO_QUOTE", "WRONG_CONTACT"}:
        row["channel_test_state"] = CHANNEL_FAILED
    else:
        row["channel_test_state"] = WAITING
    store["by_packet"][packet_id] = row
    store["updated_at"] = now_utc().isoformat()
    _save(RESPONSE_STORE, store)
    try:
        from p1_prescale_hardening.quote_observability import append_event

        append_event(
            event_type="SUPPLIER_RESPONSE_RECORDED",
            packet=packet_id,
            actor="owner",
            reason=outcome,
            new_state=row.get("channel_test_state"),
        )
    except Exception:
        pass
    return row


def match_quote_line(quote_line: dict[str, Any], packet_lines: list[dict[str, Any]]) -> dict[str, Any]:
    mpn = str(quote_line.get("mpn") or quote_line.get("exact_mpn_model") or "").strip().upper()
    mfr = str(quote_line.get("manufacturer") or "").strip().upper()
    candidates = []
    for pl in packet_lines:
        p_mpn = str(pl.get("mpn") or pl.get("model") or "").strip().upper()
        p_mfr = str(pl.get("manufacturer") or "").strip().upper()
        if mpn and p_mpn == mpn:
            candidates.append(pl)
        elif mpn and mpn in p_mpn or (p_mpn and p_mpn in mpn):
            candidates.append(pl)
    if len(candidates) == 1:
        pl = candidates[0]
        q_uom = str(quote_line.get("uom") or "").upper()
        p_uom = str(pl.get("uom") or "").upper()
        q_qty = float(quote_line.get("qty") or 0)
        p_qty = float(pl.get("qty") or 0)
        if quote_line.get("alternate") or quote_line.get("substitute"):
            return {
                "match_class": ALTERNATE_PRODUCT_OFFERED,
                "line_id": pl.get("line_id"),
                "alternate_status": "ALTERNATE_REVIEW_REQUIRED",
            }
        if q_uom and p_uom and q_uom != p_uom:
            if quote_line.get("conversion_rule"):
                return {
                    "match_class": PACK_CONVERSION_VALID,
                    "line_id": pl.get("line_id"),
                    "conversion_rule": quote_line.get("conversion_rule"),
                }
            return {"match_class": AMBIGUOUS, "line_id": pl.get("line_id"), "reason": "UOM_MISMATCH"}
        if abs(q_qty - p_qty) > 1e-6 and not quote_line.get("allow_partial_qty"):
            return {"match_class": EXACT_MATCH, "line_id": pl.get("line_id"), "partial_qty": True}
        return {"match_class": EXACT_MATCH, "line_id": pl.get("line_id")}
    if len(candidates) > 1:
        return {"match_class": AMBIGUOUS, "candidates": [c.get("line_id") for c in candidates]}
    return {"match_class": UNMATCHED}


def ingest_real_quote(
    *,
    packet: dict[str, Any],
    quote: dict[str, Any],
) -> dict[str, Any]:
    """Phase 10–15: real quote only; fixtures blocked; auto-wire via PROCESS_REAL_SUPPLIER_QUOTE."""
    origin = quote.get("origin") or quote.get("QUOTE_ORIGIN")
    if origin == TEST_FIXTURE_ONLY or str(quote.get("label") or "").startswith("TEST_FIXTURE"):
        return {
            "accepted": False,
            "blocked_reason": "TEST_FIXTURE_ONLY_NOT_PRODUCTION_EVIDENCE",
            "PASS_FAIL": "FAIL",
        }
    if origin != REAL_SUPPLIER_QUOTE:
        return {
            "accepted": False,
            "blocked_reason": "NOT_REAL_SUPPLIER_QUOTE",
            "PASS_FAIL": "FAIL",
        }

    # Required fields
    required = ["supplier", "quote_date", "source_artifact"]
    missing = [f for f in required if not quote.get(f) and not quote.get("supplier_identity")]
    if not quote.get("quote_number") and not quote.get("quote_number_or_reference"):
        missing.append("quote_number_or_reference")
    if missing:
        return {"accepted": False, "blocked_reason": f"missing:{missing}", "PASS_FAIL": "FAIL"}

    quote_lines = quote.get("lines") or []
    if not quote_lines and quote.get("exact_mpn_model"):
        quote_lines = [quote]

    packet_lines = packet.get("lines") or []
    matches = []
    accepted_cost_lines = []
    for ql in quote_lines:
        m = match_quote_line(ql, packet_lines)
        m["quote_line"] = {
            "mpn": ql.get("mpn") or ql.get("exact_mpn_model"),
            "qty": ql.get("qty"),
            "uom": ql.get("uom"),
            "unit_price": ql.get("unit_price"),
        }
        # Validity
        if str(ql.get("validity") or quote.get("validity") or "").lower() in {"expired", "invalid"}:
            m["validity"] = "QUOTE_EXPIRED"
            m["executable"] = False
        elif ql.get("availability") in {"NONE", "DISCONTINUED", "NO"}:
            m["validity"] = "NON_EXECUTABLE_QUOTE"
            m["executable"] = False
        else:
            m["validity"] = "VALID"
            m["executable"] = m["match_class"] in {EXACT_MATCH, PACK_CONVERSION_VALID}
        if m.get("match_class") == ALTERNATE_PRODUCT_OFFERED:
            m["executable"] = False
        matches.append(m)
        if m.get("executable") and ql.get("unit_price") is not None:
            accepted_cost_lines.append({**ql, "line_id": m.get("line_id")})

    # Auto-wire via existing PROCESS_REAL_SUPPLIER_QUOTE for each accepted line
    basket = init_basket_from_packet(packet)
    before_basket = deepcopy(basket)
    audits = []
    for ql in accepted_cost_lines:
        payload = {
            "origin": REAL_SUPPLIER_QUOTE,
            "supplier_identity": quote.get("supplier") or quote.get("supplier_identity"),
            "quote_date": quote.get("quote_date"),
            "quote_number_or_reference": quote.get("quote_number") or quote.get("quote_number_or_reference"),
            "exact_mpn_model": ql.get("mpn") or ql.get("exact_mpn_model"),
            "qty": ql.get("qty"),
            "uom": ql.get("uom"),
            "unit_price": ql.get("unit_price"),
            "extended_price": ql.get("extended_price")
            or float(ql.get("unit_price") or 0) * float(ql.get("qty") or 1),
            "freight_treatment": quote.get("freight_treatment") or "REQUESTED",
            "freight": quote.get("freight"),
            "validity": quote.get("validity") or "30 days",
            "source_artifact": quote.get("source_artifact"),
            "allow_partial_qty": True,
            "force_reprocess": True,
        }
        gate = production_gate_quote(payload)
        if not gate.get("accepted"):
            audits.append({"gate": gate, "PASS": False})
            continue
        audit = process_real_supplier_quote(payload, basket_state=basket, opportunity_state={})
        basket = audit.get("basket_state") or basket
        audits.append(audit)

    # Revenue safety: channel proof only — never create profit without usable revenue
    economics_state = basket.get("economics_state")
    profit_created = False
    revenue_mode = packet.get("revenue_note") or SUPPLIER_CHANNEL_PROOF_ONLY
    if "CHANNEL_PROOF" in str(revenue_mode).upper() or revenue_mode == SUPPLIER_CHANNEL_PROOF_ONLY:
        # Force economics presentation as channel-proof, not profit
        if economics_state == "ECONOMICS_READY":
            basket["economics_state"] = "ECONOMICS_CHANNEL_COST_ONLY"
            basket["profit_blocked_reason"] = "NO_USABLE_GOVERNMENT_REVENUE"
            basket["economics_display"] = {
                "acquisition_cost_known": True,
                "profit": None,
                "note": "Supplier channel / acquisition cost proven; government revenue not usable",
            }
        profit_created = False

    result = {
        "accepted": True,
        "QUOTE_ORIGIN": REAL_SUPPLIER_QUOTE,
        "packet_id": packet.get("packet_id"),
        "opportunity_id": packet.get("opportunity_id"),
        "matches": matches,
        "exact_matched": sum(1 for m in matches if m["match_class"] == EXACT_MATCH),
        "pack_converted": sum(1 for m in matches if m["match_class"] == PACK_CONVERSION_VALID),
        "alternates": sum(1 for m in matches if m["match_class"] == ALTERNATE_PRODUCT_OFFERED),
        "unmatched": sum(1 for m in matches if m["match_class"] == UNMATCHED),
        "ambiguous": sum(1 for m in matches if m["match_class"] == AMBIGUOUS),
        "rejected_non_executable": sum(1 for m in matches if not m.get("executable")),
        "basket_state": basket.get("basket_state"),
        "economics_state": basket.get("economics_state"),
        "profit_created": profit_created,
        "revenue_mode": revenue_mode,
        "auto_wire_audits": [
            {
                "PASS": a.get("PASS"),
                "next_action": a.get("next_action"),
                "steps": a.get("steps"),
            }
            for a in audits
        ],
        "auto_wire_pass": all(a.get("PASS") for a in audits) if audits else False,
        "channel_test_state": CHANNEL_CONFIRMED if audits and all(a.get("PASS") for a in audits) else PARTIAL_RESPONSE,
        "recorded_at": now_utc().isoformat(),
        "PASS_FAIL": "PASS" if audits and all(a.get("PASS") for a in audits) else ("PASS" if not accepted_cost_lines and matches else "FAIL"),
    }

    # P1 quote observability — ledger, before/after, idempotency, versioning
    try:
        from p1_prescale_hardening.quote_observability import (
            classify_quote_failure,
            line_match_audit_row,
            observe_ingest_pipeline,
        )

        line_audits = []
        for m in matches:
            ql = m.get("quote_line") or {}
            accepted = bool(m.get("executable"))
            fail = None if accepted else classify_quote_failure(
                supplier=quote.get("supplier"),
                packet=packet,
                quote={**quote, **ql},
                match=m,
            )
            line_audits.append(
                line_match_audit_row(
                    supplier_raw_line=ql,
                    matched_solicitation_line={"line_id": m.get("line_id")} if m.get("line_id") else None,
                    match_type=m.get("match_class") or "UNMATCHED",
                    confidence=1.0 if accepted else 0.0,
                    accepted=accepted,
                    reason=fail or m.get("reason"),
                )
            )
        obs = observe_ingest_pipeline(
            packet=packet,
            quote=quote,
            before_basket=before_basket,
            after_result={**result, "basket_state": basket},
            line_audits=line_audits,
        )
        result["observability"] = {
            "quote_id": obs.get("quote_id"),
            "idempotent": obs.get("idempotent"),
            "duplicate": obs.get("duplicate"),
            "supersedes": obs.get("supersedes"),
            "fingerprint": obs.get("fingerprint"),
        }
        if obs.get("duplicate"):
            result["idempotent_noop"] = True
            result["economics_corrupted"] = False
    except Exception as exc:  # noqa: BLE001 — observability must not break ingest
        result["observability_error"] = str(exc)

    # Invalidate BID_READY on expired quote
    if any(m.get("validity") == "QUOTE_EXPIRED" for m in matches):
        try:
            from p1_prescale_hardening.bid_ready import invalidate_bid_ready
            from m3_data_root import data_path as _dp
            import json as _json

            br_path = _dp("m3_bid_ready_state_v1.json")
            if br_path.exists():
                store_br = _json.loads(br_path.read_text(encoding="utf-8"))
                oid = packet.get("opportunity_id")
                cur = (store_br.get("by_opportunity") or {}).get(oid)
                if cur and cur.get("BID_READY"):
                    store_br["by_opportunity"][oid] = invalidate_bid_ready(
                        cur, reason="supplier quote expired", event="QUOTE_EXPIRED"
                    )
                    br_path.write_text(_json.dumps(store_br, indent=2, default=str), encoding="utf-8")
                    result["bid_ready_invalidated"] = True
        except Exception:
            pass

    store = _load(QUOTE_STORE)
    store.setdefault("quotes", []).append(
        {
            "packet_id": packet.get("packet_id"),
            "supplier": quote.get("supplier"),
            "quote_date": quote.get("quote_date"),
            "artifact": quote.get("source_artifact"),
            "result_summary": {
                k: result[k]
                for k in (
                    "exact_matched",
                    "pack_converted",
                    "alternates",
                    "unmatched",
                    "basket_state",
                    "economics_state",
                    "profit_created",
                    "channel_test_state",
                )
            },
            "observability": result.get("observability"),
            "at": result["recorded_at"],
        }
    )
    _save(QUOTE_STORE, store)
    return result


def channel_metrics(corpus: dict[str, Any]) -> dict[str, Any]:
    sent = _load(SENT_STORE).get("by_packet") or {}
    resp = _load(RESPONSE_STORE).get("by_packet") or {}
    quotes = _load(QUOTE_STORE).get("quotes") or []
    packets = corpus.get("packets") or []
    return {
        "packets_total": len(packets),
        "packets_sent": len(sent),
        "suppliers_contacted": len({v.get("supplier") for v in sent.values()}),
        "responses": len(resp),
        "quotes_received": sum(1 for r in resp.values() if r.get("outcome") == "QUOTE_RECEIVED")
        + len(quotes),
        "partial_quotes": sum(1 for r in resp.values() if r.get("outcome") == "PARTIAL_QUOTE_RECEIVED"),
        "declines": sum(1 for r in resp.values() if r.get("outcome") == "DECLINED_TO_QUOTE"),
        "referrals": sum(1 for r in resp.values() if r.get("outcome") == "REFERRED_TO_DISTRIBUTOR"),
        "account_required": sum(1 for r in resp.values() if r.get("outcome") == "ACCOUNT_REQUIRED"),
        "no_response": sum(1 for r in resp.values() if r.get("outcome") == "NO_RESPONSE"),
        "lines_in_corpus": sum(int(p.get("line_count") or 0) for p in packets),
        "real_quotes_ingested": len(quotes),
    }
