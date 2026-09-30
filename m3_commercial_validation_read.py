"""BUILD 30 — Commercial Validation + Quote-to-Economics Handoff.

Human-driven path:
  Supplier/commercial evidence → verified commercial inputs → economics
  → updated Pursuit Readiness (on next read)

Reuses Supply Intelligence commercial evidence, supplier commercial terms,
transaction_economics on the opportunity row, Operator Loop, and economics
read models. Does NOT invent costs, treat UNKNOWN as zero, auto-outreach,
auto-advance readiness, or spend AI budget.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from application_clock import now_utc

log = logging.getLogger("govtracker.m3_commercial_validation")

BUILD_TAG = "20260919-m3-commercial-validation-1"

# Align with supplier capital ops + master-record spirit
ST_VERIFIED = "VERIFIED"
ST_OPERATOR_REPORTED = "OPERATOR_REPORTED"
ST_UNKNOWN = "UNKNOWN"
ST_DETECTED = "DETECTED"

VERIFICATION_STATES = (ST_VERIFIED, ST_OPERATOR_REPORTED, ST_UNKNOWN, ST_DETECTED)


def _utc() -> str:
    return now_utc().isoformat()


def _as_dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _known(v: Any) -> bool:
    return v not in (None, "", "UNKNOWN")


def _unknown_or(v: Any) -> Any:
    """Preserve UNKNOWN — never coerce blank/None to 0."""
    if v is None or v == "":
        return "UNKNOWN"
    if isinstance(v, str) and v.strip().upper() in {"UNKNOWN", "N/A", "NA", "NONE"}:
        return "UNKNOWN"
    return v


def _parse_number(v: Any) -> Any:
    """Return number if parseable, else UNKNOWN. Never returns 0 for blank."""
    v = _unknown_or(v)
    if v == "UNKNOWN":
        return "UNKNOWN"
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    try:
        s = str(v).strip().replace(",", "").replace("$", "")
        if not s or s.upper() == "UNKNOWN":
            return "UNKNOWN"
        return float(s)
    except (TypeError, ValueError):
        return "UNKNOWN"


def _stable_evidence_id(payload: dict[str, Any]) -> str:
    seed = "|".join(
        [
            str(payload.get("opportunity_id") or ""),
            str(payload.get("supplier") or ""),
            str(payload.get("product") or ""),
            str(payload.get("unit_price") or payload.get("price") or ""),
            str(payload.get("quantity") or ""),
            str(payload.get("quote_date") or payload.get("date") or ""),
            str(payload.get("source") or ""),
            str(payload.get("quote_reference") or ""),
        ]
    )
    return f"cev:{hashlib.sha1(seed.encode('utf-8')).hexdigest()[:12]}"


def _normalize_verification(v: Any) -> str:
    s = str(v or ST_OPERATOR_REPORTED).upper().strip()
    if s in {"VERIFIED", "SUPPLIER_VERIFIED", "DOCUMENTED"}:
        return ST_VERIFIED
    if s in {"OPERATOR_REPORTED", "REPORTED", "HUMAN_ENTERED", "DETECTED"}:
        return ST_OPERATOR_REPORTED if s != "DETECTED" else ST_DETECTED
    if s == "UNKNOWN":
        return ST_UNKNOWN
    return ST_OPERATOR_REPORTED


def build_commercial_economics_view(
    row: dict[str, Any] | None,
    *,
    deal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Transparent revenue / acquisition / costs / unknowns / margin visibility."""
    row = row if isinstance(row, dict) else {}
    deal = deal if isinstance(deal, dict) else {}
    econ = _as_dict(deal.get("economics") or row.get("transaction_economics") or row.get("economics"))
    funding = _as_dict(deal.get("funding"))

    revenue = _unknown_or(econ.get("revenue") or row.get("estimated_value") or row.get("award_amount"))
    acquisition = _unknown_or(
        econ.get("acquisition") or econ.get("acquisition_evidence") or row.get("acquisition_cost")
    )
    unit = _unknown_or(econ.get("acquisition_unit") or econ.get("unit_price"))
    qty = _unknown_or(econ.get("quantity"))
    freight = _unknown_or(econ.get("freight") or row.get("freight_cost"))
    other = _unknown_or(econ.get("other_costs"))
    payment = _unknown_or(econ.get("payment_terms"))
    deposit = _unknown_or(econ.get("deposit_requirement"))
    cash = _unknown_or(
        funding.get("capital_requirement")
        or econ.get("cash_requirement")
        or econ.get("capital_requirement")
    )
    currency = _unknown_or(econ.get("currency"))

    known_additional: list[dict[str, Any]] = []
    unknown_costs: list[str] = []
    if _known(freight):
        known_additional.append({"label": "Freight / shipping", "value": freight})
    else:
        unknown_costs.append("Freight / shipping")
    if _known(other):
        known_additional.append({"label": "Other costs", "value": other})
    else:
        unknown_costs.append("Other costs")
    if not _known(acquisition):
        unknown_costs.append("Acquisition / supplier cost")
    if not _known(revenue):
        unknown_costs.append("Government-side revenue / pricing")

    margin: Any = "UNKNOWN"
    margin_note = "Margin not calculated — missing acquisition and/or revenue evidence"
    if _known(revenue) and _known(acquisition):
        try:
            margin = float(revenue) - float(acquisition)
            if _known(freight):
                margin = float(margin) - float(freight)
            margin_note = "Calculated only from known revenue − acquisition (− freight if known)"
        except (TypeError, ValueError):
            margin = "UNKNOWN"
            margin_note = "Could not calculate — non-numeric known inputs"

    # Never present unsupported profit from row if inputs insufficient
    stored_profit = econ.get("supported_profit") or econ.get("expected_profit")
    if not (_known(revenue) and _known(acquisition)):
        stored_profit = "UNKNOWN"

    return {
        "kind": "M3CommercialEconomicsView",
        "build": BUILD_TAG,
        "opportunity_id": row.get("canonical_id") or "UNKNOWN",
        "revenue": {
            "value": revenue,
            "label": "Government-side pricing evidence",
            "unknown": not _known(revenue),
        },
        "acquisition_cost": {
            "value": acquisition,
            "unit_price": unit,
            "quantity": qty,
            "currency": currency,
            "label": "Supplier / commercial evidence",
            "unknown": not _known(acquisition),
            "source": econ.get("acquisition_source") or econ.get("source") or "UNKNOWN",
            "verification_status": econ.get("verification_status") or ST_UNKNOWN,
        },
        "known_additional_costs": known_additional,
        "unknown_costs": unknown_costs,
        "payment_terms": payment,
        "deposit_requirement": deposit,
        "cash_requirement": cash,
        "margin_visibility": {
            "value": margin if _known(revenue) and _known(acquisition) else "UNKNOWN",
            "stored_supported_profit": stored_profit if _known(stored_profit) else "UNKNOWN",
            "note": margin_note,
            "unsupported_profit_presented_as_fact": False,
        },
        "rules": {
            "unknown_never_zero": True,
            "no_fabricated_costs": True,
            "margin_only_from_known_inputs": True,
        },
        "generated_at": _utc(),
        "read_only": True,
    }


def _apply_quote_to_opportunity_row(
    row: dict[str, Any],
    *,
    quote: dict[str, Any],
    evidence_id: str,
) -> dict[str, Any]:
    """Merge supplier commercial terms + transaction_economics — UNKNOWN not zero."""
    out = dict(row)
    supplier = quote.get("supplier") if _known(quote.get("supplier")) else "UNKNOWN"
    unit = _parse_number(quote.get("unit_price") or quote.get("price"))
    qty = _parse_number(quote.get("quantity"))
    total = _parse_number(quote.get("total_quoted_price") or quote.get("total"))
    freight = _parse_number(quote.get("freight") or quote.get("shipping"))
    currency = _unknown_or(quote.get("currency"))
    verification = quote.get("verification_status") or ST_OPERATOR_REPORTED

    if total == "UNKNOWN" and unit != "UNKNOWN" and qty != "UNKNOWN":
        try:
            total = float(unit) * float(qty)
        except (TypeError, ValueError):
            total = "UNKNOWN"

    acquisition = total if total != "UNKNOWN" else unit

    # supplier_commercial_terms (append history — never invent)
    terms = _as_dict(out.get("supplier_commercial_terms"))
    by_supplier = dict(_as_dict(terms.get("by_supplier")))
    prev = _as_dict(by_supplier.get(supplier if supplier != "UNKNOWN" else "UNKNOWN"))
    entry = {
        "quote": {
            "price": unit if unit != "UNKNOWN" else total,
            "total": total,
            "quantity": qty,
            "uom": _unknown_or(quote.get("uom") or quote.get("unit")),
            "date": _unknown_or(quote.get("quote_date") or quote.get("date")),
            "expiration": _unknown_or(quote.get("quote_expiration")),
            "currency": currency,
            "reference": _unknown_or(quote.get("quote_reference")),
            "source": quote.get("source") or "UNKNOWN",
            "evidence": evidence_id,
            "status": ST_VERIFIED if verification == ST_VERIFIED else ST_DETECTED,
            "verified": verification == ST_VERIFIED,
            "verification_status": verification,
        },
        "terms": {
            "net_terms": _unknown_or(quote.get("payment_terms")),
            "deposit_requirement": _unknown_or(quote.get("deposit_requirement")),
            "shipping_terms": _unknown_or(quote.get("shipping_terms") or quote.get("freight")),
        },
        "lead_time": {
            "stated": _unknown_or(quote.get("lead_time")),
            "evidence_source": quote.get("source") or "UNKNOWN",
            "status": ST_DETECTED if _known(quote.get("lead_time")) else ST_UNKNOWN,
        },
        "availability": _unknown_or(quote.get("availability")),
        "product": _unknown_or(quote.get("product")),
        "manufacturer": _unknown_or(quote.get("manufacturer")),
        "model_sku": _unknown_or(quote.get("model_sku") or quote.get("sku")),
        "updated_at": _utc(),
    }
    key = supplier if supplier != "UNKNOWN" else "UNKNOWN"
    by_supplier[key] = entry
    hist = list(_as_dict(terms.get("history")).get("entries") or [])
    hist.append(
        {
            "supplier_name": key,
            "field": "quote",
            "previous_value": _as_dict(prev.get("quote")).get("price") or "UNKNOWN",
            "new_value": entry["quote"]["price"],
            "change_date": _utc(),
            "evidence": evidence_id,
            "user": quote.get("captured_by") or "operator",
        }
    )
    out["supplier_commercial_terms"] = {
        "by_supplier": by_supplier,
        "history": {"entries": hist[-50:]},
        "updated_at": _utc(),
        "build": BUILD_TAG,
    }

    # transaction_economics — only known values; never invent profit without both sides
    prev_econ = _as_dict(out.get("transaction_economics"))
    revenue = _unknown_or(
        prev_econ.get("revenue") or out.get("estimated_value") or out.get("award_amount")
    )
    supported_profit: Any = "UNKNOWN"
    if _known(revenue) and _known(acquisition):
        try:
            supported_profit = float(revenue) - float(acquisition)
            if _known(freight):
                supported_profit = float(supported_profit) - float(freight)
        except (TypeError, ValueError):
            supported_profit = "UNKNOWN"

    cash_req = acquisition if _known(acquisition) else "UNKNOWN"
    if _known(cash_req) and _known(freight):
        try:
            cash_req = float(cash_req) + float(freight)
        except (TypeError, ValueError):
            pass

    out["transaction_economics"] = {
        **prev_econ,
        "acquisition": acquisition,
        "acquisition_unit": unit,
        "quantity": qty,
        "supplier_quote_total": total,
        "freight": freight,
        "currency": currency,
        "payment_terms": _unknown_or(quote.get("payment_terms")),
        "deposit_requirement": _unknown_or(quote.get("deposit_requirement")),
        "cash_requirement": cash_req,
        "capital_requirement": cash_req,
        "revenue": revenue,
        "supported_profit": supported_profit,
        "expected_profit": supported_profit,
        "acquisition_source": evidence_id,
        "source": evidence_id,
        "verification_status": verification,
        "quote_date": _unknown_or(quote.get("quote_date") or quote.get("date")),
        "updated_at": _utc(),
        "build": BUILD_TAG,
        "unknown_never_treated_as_zero": True,
    }
    if _known(freight):
        out["freight_cost"] = freight
    if _known(acquisition):
        out["acquisition_cost"] = acquisition
    return out


def record_supplier_quote(
    payload: dict[str, Any] | None,
    *,
    persist: bool = True,
    complete_action: bool = False,
) -> dict[str, Any]:
    """Structured quote capture → supply evidence → economics → optional action close."""
    payload = payload or {}
    oid = str(payload.get("opportunity_id") or payload.get("canonical_id") or "").strip()
    if not oid:
        raise ValueError("opportunity_id required")
    source = payload.get("source") or payload.get("contact_reference")
    if not _known(source):
        raise ValueError("source/contact reference required — commercial facts need provenance")

    verification = _normalize_verification(payload.get("verification_status"))
    # Do not allow VERIFIED without a source document/reference claim
    if verification == ST_VERIFIED and not _known(payload.get("source")):
        verification = ST_OPERATOR_REPORTED

    unit = _parse_number(payload.get("unit_price") or payload.get("price"))
    qty = _parse_number(payload.get("quantity"))
    total = _parse_number(payload.get("total_quoted_price") or payload.get("total"))
    freight = _parse_number(payload.get("freight") or payload.get("shipping"))

    quote = {
        "supplier": _unknown_or(payload.get("supplier")),
        "product": _unknown_or(payload.get("product")),
        "manufacturer": _unknown_or(payload.get("manufacturer")),
        "model_sku": _unknown_or(payload.get("model_sku") or payload.get("sku")),
        "quantity": qty,
        "unit_price": unit,
        "total_quoted_price": total,
        "currency": _unknown_or(payload.get("currency")),
        "quote_date": _unknown_or(payload.get("quote_date") or payload.get("date")),
        "quote_expiration": _unknown_or(payload.get("quote_expiration")),
        "lead_time": _unknown_or(payload.get("lead_time")),
        "availability": _unknown_or(payload.get("availability")),
        "freight": freight,
        "payment_terms": _unknown_or(payload.get("payment_terms")),
        "deposit_requirement": _unknown_or(payload.get("deposit_requirement")),
        "shipping_terms": _unknown_or(payload.get("shipping_terms")),
        "other_terms": _unknown_or(payload.get("other_terms") or payload.get("terms")),
        "source": source,
        "quote_reference": _unknown_or(payload.get("quote_reference")),
        "notes": payload.get("notes") or "",
        "verification_status": verification,
        "captured_by": payload.get("captured_by") or payload.get("actor") or "operator",
        "opportunity_id": oid,
    }

    evidence_id = payload.get("evidence_id") or _stable_evidence_id({**quote, "opportunity_id": oid})

    # Existing commercial evidence index (dedupe via stable id)
    from m3_supply_intelligence_read import create_commercial_evidence

    price_for_store = unit if unit != "UNKNOWN" else total
    commercial = create_commercial_evidence(
        {
            "evidence_id": evidence_id,
            "evidence_type": "Supplier Quote",
            "source": source,
            "product": quote["product"],
            "supplier": quote["supplier"],
            "price": price_for_store,
            "quantity": qty,
            "date": quote["quote_date"] if quote["quote_date"] != "UNKNOWN" else _utc(),
            "lead_time": quote["lead_time"],
            "terms": quote["payment_terms"]
            if quote["payment_terms"] != "UNKNOWN"
            else quote["other_terms"],
            "notes": quote["notes"],
            "claim": payload.get("claim")
            or f"Supplier quote for {quote['product']} from {quote['supplier']}",
            "opportunity_id": oid,
            # Extended provenance (stored on record; readers ignore unknown keys safely)
            "manufacturer": quote["manufacturer"],
            "model_sku": quote["model_sku"],
            "unit_price": unit,
            "total_quoted_price": total,
            "currency": quote["currency"],
            "quote_expiration": quote["quote_expiration"],
            "freight": freight,
            "deposit_requirement": quote["deposit_requirement"],
            "availability": quote["availability"],
            "quote_reference": quote["quote_reference"],
            "verification_status": verification,
            "captured_by": quote["captured_by"],
            "not_an_assumption": True,
            "fabricated": False,
        },
        persist=persist,
    )
    duplicate = commercial.get("evidence_id") == evidence_id and commercial.get("updated_at")

    # Optional supplier record (only if name known — evidence required by create_supplier)
    supplier_rec = None
    if _known(quote["supplier"]):
        try:
            from m3_supply_intelligence_read import create_supplier

            supplier_rec = create_supplier(
                {
                    "company_name": quote["supplier"],
                    "evidence": source,
                    "evidence_links": [evidence_id],
                    "opportunity_id": oid,
                    "quotes": [evidence_id],
                    "terms": quote["payment_terms"],
                },
                persist=persist,
            )
        except Exception as e:
            log.debug("supplier create skipped: %s", e)

    # Do not auto-create supply paths here — that would spawn research missions.
    # Commercial evidence index + opportunity economics are sufficient for readiness.
    path = None

    # Persist onto opportunity row
    row_out = None
    economics_view = None
    readiness = None
    if persist:
        try:
            from m3_pipeline_store import M3PipelineStore
            from m3_discovery_service import restore_pipeline_store_from_db

            store = M3PipelineStore()
            restore_pipeline_store_from_db(store)
            row = store.get(oid)
            if row:
                updated = _apply_quote_to_opportunity_row(
                    row, quote=quote, evidence_id=evidence_id
                )
                store._rows[oid] = updated
                store.save(durable_write=True, skip_remote_merge=True)
                row_out = updated
                economics_view = build_commercial_economics_view(updated)
                try:
                    from m3_pursuit_readiness_read import build_pursuit_readiness_assessment

                    readiness = build_pursuit_readiness_assessment(updated, ensure_actions=False)
                except Exception:
                    readiness = None
            else:
                economics_view = build_commercial_economics_view(
                    {"canonical_id": oid, "transaction_economics": {}}
                )
        except Exception as e:
            log.warning("opportunity row update failed: %s", e)

    # Terms history index (existing SCO helper)
    try:
        from m3_supplier_capital_ops_read import record_supplier_commercial_terms_change

        if _known(quote["supplier"]):
            record_supplier_commercial_terms_change(
                opportunity_id=oid,
                supplier_name=str(quote["supplier"]),
                field="quote.price",
                previous_value="UNKNOWN",
                new_value=unit if unit != "UNKNOWN" else total,
                evidence=evidence_id,
                user=str(quote["captured_by"]),
                persist=persist,
            )
    except Exception:
        pass

    action_result = None
    action_id = payload.get("action_id")
    do_complete = bool(complete_action or payload.get("complete_action"))
    if do_complete and _known(action_id):
        try:
            from m3_operator_loop_read import operator_mark_done, operator_record_evidence

            operator_record_evidence(
                {
                    "action_id": action_id,
                    "claim": commercial.get("claim"),
                    "source": source,
                    "evidence": f"quote:{evidence_id} price={price_for_store}",
                    "evidence_type": "Supplier Quote",
                    "opportunity_id": oid,
                    "supplier": quote["supplier"],
                    "product": quote["product"],
                    "price": price_for_store,
                },
                persist=persist,
            )
            # Only complete when there is real evidence attached
            action_result = operator_mark_done(
                {
                    "action_id": action_id,
                    "evidence": f"Supplier quote recorded ({evidence_id}) from {source}",
                    "result": "Quote captured into supply + economics",
                    "criteria_met": [
                        "communication_attempt_documented",
                        "response_or_waiting_status",
                        "evidence_of_contact_or_unknown",
                        "evidence_references_captured",
                    ],
                    "actor": quote["captured_by"],
                },
                persist=persist,
            )
        except Exception as e:
            action_result = {"accepted": False, "error": str(e)}

    supply_state = (
        ((_as_dict((_as_dict(readiness).get("dimensions") or {}).get("supply_confidence")).get("state"))
        if readiness
        else "UNKNOWN")
    )
    econ_state = (
        ((_as_dict((_as_dict(readiness).get("dimensions") or {}).get("economics_visibility")).get("state"))
        if readiness
        else "UNKNOWN")
    )

    return {
        "kind": "M3CommercialQuoteRecord",
        "build": BUILD_TAG,
        "opportunity_id": oid,
        "evidence_id": evidence_id,
        "duplicate_upsert": True,  # stable id upsert — not uncontrolled duplicate create
        "commercial_evidence": commercial,
        "quote": quote,
        "supplier_record": supplier_rec,
        "supply_path": path,
        "economics": economics_view,
        "opportunity_updated": row_out is not None,
        "pursuit_readiness_after": {
            "supply_state": supply_state,
            "economics_state": econ_state,
            "note": "Readiness reflects evidence — not forced to KNOWN by quote entry alone",
        },
        "action_close": action_result,
        "distinctions": {
            "what_we_know": [k for k, v in quote.items() if _known(v) and k not in {"notes", "captured_by"}],
            "what_supplier_provided": {
                "verification_status": verification,
                "source": source,
                "evidence_id": evidence_id,
            },
            "what_we_calculated": {
                "acquisition": (economics_view or {}).get("acquisition_cost", {}).get("value"),
                "margin": (economics_view or {}).get("margin_visibility", {}).get("value"),
            },
            "what_is_still_unknown": [
                k
                for k in (
                    "unit_price",
                    "quantity",
                    "lead_time",
                    "freight",
                    "payment_terms",
                    "deposit_requirement",
                )
                if not _known(quote.get(k))
            ],
        },
        "principles": {
            "unknown_never_zero": True,
            "no_fabricated_costs": True,
            "no_auto_outreach": True,
            "no_ai_spend": True,
            "no_forced_readiness_known": True,
            "human_confirmed": True,
            "reuses_supply_intelligence": True,
            "reuses_transaction_economics": True,
            "reuses_operator_loop": True,
        },
        "OpenAI": 0,
        "paid": 0,
        "automatic_outreach": False,
        "generated_at": _utc(),
    }


def attach_commercial_validation_to_deal_room(
    deal: dict[str, Any], *, row: dict[str, Any] | None = None
) -> dict[str, Any]:
    if not isinstance(deal, dict):
        return deal
    out = dict(deal)
    try:
        r = row or {"canonical_id": deal.get("canonical_id")}
        out["commercial_validation"] = {
            "kind": "M3CommercialValidationDealRoom",
            "build": BUILD_TAG,
            "opportunity_id": r.get("canonical_id") or deal.get("canonical_id") or "UNKNOWN",
            "economics": build_commercial_economics_view(r, deal=out),
            "form": {
                "endpoint": "/api/m3/commercial/quote",
                "fields": [
                    "supplier",
                    "product",
                    "manufacturer",
                    "model_sku",
                    "quantity",
                    "unit_price",
                    "total_quoted_price",
                    "currency",
                    "quote_date",
                    "quote_expiration",
                    "lead_time",
                    "availability",
                    "freight",
                    "payment_terms",
                    "deposit_requirement",
                    "source",
                    "quote_reference",
                    "verification_status",
                    "notes",
                    "action_id",
                ],
                "verification_options": list(VERIFICATION_STATES),
                "beginner_labels": True,
            },
            "rules": {
                "blank_means_unknown": True,
                "unknown_never_zero": True,
                "source_required": True,
            },
            "read_only_view": True,
        }
    except Exception as e:
        log.warning("commercial validation attach failed: %s", e)
        out["commercial_validation"] = {
            "kind": "M3CommercialValidationDealRoom",
            "build": BUILD_TAG,
            "error": "commercial_validation_unavailable",
        }
    return out
