"""Resolve gov-value + acquisition cost for one identity via OpenGovBidPriceIndex."""

from __future__ import annotations

import re
from typing import Any

from evidence_breakthrough.models import (
    FOUND,
    GOV_CLOSE_PRODUCT_HISTORY,
    GOV_EXACT_LINE_HISTORY,
    GOV_EXACT_PRODUCT_HISTORY,
    GOV_NO_USABLE_HISTORY,
    INSUFFICIENT_IDENTITY,
    NOT_FOUND_AFTER_EXHAUSTIVE_SEARCH,
    PUBLIC_DISTRIBUTOR_EXACT,
)
from evidence_breakthrough.opengov_history import parse_opengov_opportunity_id
from scale_evidence_profit.bid_price_index import lookup_matches
from scale_evidence_profit.models import (
    BOTH_SIDES_READY,
    COST_ONLY,
    GOV_ONLY,
    INSUFFICIENT_IDENTITY as LINE_INSUFFICIENT,
    NO_HISTORY_AFTER_EXHAUSTIVE_SEARCH,
    NO_PUBLIC_PRICE_AFTER_EXHAUSTIVE_SEARCH,
    PUBLIC_VENDOR_BID_REFERENCE,
    UOM_BLOCKED,
)


def _norm(s: str | None) -> str:
    if not s:
        return ""
    return re.sub(r"[^A-Z0-9]", "", str(s).upper())


def _has_identity(ident: dict[str, Any]) -> bool:
    return bool(
        ident.get("part_number")
        or ident.get("catalog_number")
        or ident.get("model")
        or ident.get("sku")
        or (ident.get("manufacturer") and ident.get("model"))
    )


def _gov_type(grade: str, same_buyer: bool) -> tuple[str, str]:
    if grade in {"EXACT_PN", "EXACT_MODEL_AS_PN"}:
        return GOV_EXACT_LINE_HISTORY, ("A" if same_buyer else "B")
    if grade in {"EXACT_MODEL_TOKEN", "MFR_MODEL_IN_DESC"}:
        return GOV_EXACT_PRODUCT_HISTORY, ("A" if same_buyer else "B")
    if grade == "DESC_OVERLAP":
        return GOV_CLOSE_PRODUCT_HISTORY, "C"
    return GOV_CLOSE_PRODUCT_HISTORY, "C"


def resolve_line(
    identity: dict[str, Any],
    idx: dict[str, Any],
    *,
    allow_public_price: bool = True,
) -> dict[str, Any]:
    """Independent gov + cost resolution against global bid index."""
    oid = str(identity.get("opportunity_id") or "")
    gov_code, project_id = parse_opengov_opportunity_id(oid)
    pn = identity.get("part_number") or identity.get("catalog_number") or identity.get("sku")
    model = identity.get("model")
    mfr = identity.get("manufacturer") or identity.get("brand")
    desc = identity.get("raw_description") or identity.get("commercial_search_key")

    out: dict[str, Any] = {
        "opportunity_id": oid,
        "line_id": identity.get("line_id"),
        "identity": {
            "grade": identity.get("confidence_grade"),
            "manufacturer": mfr,
            "model": model,
            "part_number": pn,
            "description": desc,
            "quantity": identity.get("quantity"),
            "uom": identity.get("uom_normalized") or identity.get("uom") or "EA",
            "identity_type": identity.get("identity_type"),
        },
        "government_value": None,
        "public_cost": None,
        "line_status": None,
        "uom_ok": True,
    }

    if not _has_identity(identity):
        out["line_status"] = LINE_INSUFFICIENT
        out["government_value"] = {
            "status": INSUFFICIENT_IDENTITY,
            "match_type": GOV_NO_USABLE_HISTORY,
            "failure_reason": "INSUFFICIENT_IDENTITY",
        }
        out["public_cost"] = {
            "status": INSUFFICIENT_IDENTITY,
            "match_type": "NO_PUBLIC_PRICE",
            "failure_reason": "INSUFFICIENT_IDENTITY",
        }
        return out

    matches = lookup_matches(
        idx,
        part_number=str(pn) if pn else None,
        model=str(model) if model else None,
        manufacturer=str(mfr) if mfr else None,
        description=str(desc) if desc else None,
        government_code=gov_code,
        current_project_id=project_id,
        limit=30,
    )

    # Exact identity matches only — never promote DESC_OVERLAP to both-sides
    EXACT = {"EXACT_PN", "EXACT_MODEL_AS_PN", "EXACT_MODEL_TOKEN", "MFR_MODEL_IN_DESC"}
    exact_hits = [h for h in matches if h["match_grade"] in EXACT and h["score"] >= 85]

    # --- Government value: median of competitive (lowest-per-line) historical prices ---
    # Using the single lowest ever bid as revenue + another project's bid as cost
    # structurally understates expected clearing price. Median of line lows is defensible.
    def _line_low(rec: dict[str, Any]) -> tuple[float, str | None] | None:
        vendors = rec.get("all_priced_vendors") or [
            {"unit_price": rec.get("bid_unit_price"), "vendor": rec.get("vendor")}
        ]
        priced: list[tuple[float, str | None]] = []
        for vr in vendors:
            try:
                up = float(vr.get("unit_price"))
            except (TypeError, ValueError):
                continue
            if up > 0:
                priced.append((up, vr.get("vendor") or rec.get("vendor")))
        if not priced:
            return None
        priced.sort()
        return priced[0]

    same_buyer_hits = [h for h in exact_hits if h.get("same_buyer")]
    gov_pool = same_buyer_hits or exact_hits
    line_lows: list[tuple[float, str | None, dict, dict]] = []
    for h in gov_pool:
        low = _line_low(h["record"])
        if low:
            line_lows.append((low[0], low[1], h, h["record"]))

    gov_hit = None
    gov_unit = None
    gov_project = None
    if line_lows:
        line_lows_sorted = sorted(line_lows, key=lambda t: t[0])
        mid = line_lows_sorted[len(line_lows_sorted) // 2]
        unit, win_vendor, gov_hit, rec = mid
        # Prefer a same-buyer median member as provenance record
        mtype, conf = _gov_type(gov_hit["match_grade"], bool(gov_hit.get("same_buyer")))
        gov_unit = unit
        gov_project = rec.get("project_id")
        out["government_value"] = {
            "status": FOUND,
            "match_type": mtype,
            "confidence": conf,
            "source": "opengov_public_bid_tabulation",
            "evidence": {
                "historical_buyer": rec.get("buyer") or rec.get("government_code"),
                "government_code": rec.get("government_code"),
                "award_date": rec.get("award_date"),
                "exact_item": rec.get("line_description"),
                "part_number": rec.get("part_number"),
                "quantity": rec.get("quantity"),
                "uom": rec.get("uom"),
                "awarded_unit_price": unit,
                "unit_price": unit,
                "nominal_historical_price": unit,
                "awarded_extended_total": rec.get("extended"),
                "winning_vendor": win_vendor,
                "bidder_count": rec.get("bidder_count"),
                "project_id": rec.get("project_id"),
                "project_title": rec.get("project_title"),
                "financial_id": rec.get("solicitation_number"),
                "price_role": "MEDIAN_HISTORICAL_LINE_LOW",
                "history_samples": len(line_lows_sorted),
                "history_min": line_lows_sorted[0][0],
                "history_max": line_lows_sorted[-1][0],
                "same_buyer": gov_hit.get("same_buyer"),
                "match_grade_internal": gov_hit["match_grade"],
                "source_url": rec.get("source_url"),
            },
            "provenance": [{"route": "opengov_bid_index", "url": rec.get("source_url"), "same_buyer": gov_hit.get("same_buyer")}],
        }
    else:
        out["government_value"] = {
            "status": NOT_FOUND_AFTER_EXHAUSTIVE_SEARCH,
            "match_type": GOV_NO_USABLE_HISTORY,
            "failure_reason": "NO_MATCHING_HISTORY",
            "stop_reason": NO_HISTORY_AFTER_EXHAUSTIVE_SEARCH,
            "candidates_seen": len(matches),
            "index_records": len(idx.get("records") or []),
        }

    # --- Acquisition cost: DISTINCT public vendor price, prefer OTHER-project LOWEST ---
    # Revenue uses historical competitive/low bid; acquisition should be a defensible
    # buy-side proxy — prefer lowest other-project vendor bid (not a higher losing bid).
    cost_hit = None
    cost_vendor = None
    cost_price = None
    candidates: list[tuple[float, str | None, dict, dict]] = []
    for h in exact_hits:
        rec = h["record"]
        vendors = rec.get("all_priced_vendors") or [
            {"unit_price": rec.get("bid_unit_price"), "vendor": rec.get("vendor")}
        ]
        for vr in vendors:
            try:
                up = float(vr.get("unit_price"))
            except (TypeError, ValueError):
                continue
            if up <= 0:
                continue
            candidates.append((up, vr.get("vendor") or rec.get("vendor"), h, rec))

    # Acquisition cost MUST come from a different project (or later market route).
    # Same-project losing bids are not a defensible buy-side price.
    other_proj = [
        t
        for t in candidates
        if not t[2].get("same_project")
        and (gov_project is None or str(t[3].get("project_id")) != str(gov_project))
    ]
    other_proj.sort(key=lambda t: t[0])  # lowest other-project vendor bid
    for up, vendor, h, rec in other_proj:
        if gov_unit is not None and abs(up - float(gov_unit)) < 1e-9:
            continue
        # Cost must be strictly below median gov clearing proxy to count as buy-side
        # only when we have multi-sample history; otherwise allow any distinct other-project.
        cost_hit, cost_vendor, cost_price = h, vendor, up
        break
    # Fallback: multi-project history pool — buy at historical min line-low,
    # sell at median (already set as gov), when min comes from another project.
    if cost_hit is None and line_lows and len(line_lows) >= 2 and gov_unit is not None:
        lows_by_price = sorted(line_lows, key=lambda t: t[0])
        for up, vendor, h, rec in lows_by_price:
            if str(rec.get("project_id")) == str(gov_project):
                continue
            if abs(up - float(gov_unit)) < 1e-9:
                continue
            if up >= float(gov_unit) - 1e-9:
                continue  # buy-side must be below median clearing proxy
            cost_hit, cost_vendor, cost_price = h, vendor, up
            break

    # If only same-project prices exist, leave cost empty → GOV_ONLY (not BOTH_SIDES)
    if cost_hit is None and candidates and not other_proj:
        out["public_cost"] = {
            "status": NOT_FOUND_AFTER_EXHAUSTIVE_SEARCH,
            "match_type": "NO_PUBLIC_PRICE",
            "failure_reason": "NO_OTHER_PROJECT_VENDOR_PRICE",
            "stop_reason": NO_PUBLIC_PRICE_AFTER_EXHAUSTIVE_SEARCH,
            "routes_attempted": ["opengov_bid_index_other_project"],
            "note": "Same-project losing bids rejected as acquisition cost",
        }

    if cost_hit and cost_price:
        crec = cost_hit["record"]
        same_px = bool(gov_unit is not None and abs(float(cost_price) - float(gov_unit)) < 1e-9)
        out["public_cost"] = {
            "status": FOUND,
            "match_type": PUBLIC_VENDOR_BID_REFERENCE,
            "confidence": "B" if not cost_hit.get("same_project") and not same_px else "C",
            "source": cost_vendor or "opengov_vendor_bid",
            "evidence": {
                "unit_price": cost_price,
                "uom": identity.get("uom_normalized") or identity.get("uom") or "EA",
                "seller": cost_vendor,
                "source_url": crec.get("source_url"),
                "basis": PUBLIC_VENDOR_BID_REFERENCE,
                "exact_match": True,
                "retrieved_via": "opengov_bid_index",
                "award_date": crec.get("award_date"),
                "government_code": crec.get("government_code"),
                "project_id": crec.get("project_id"),
                "note": "Public unsealed vendor unit price on OpenGov bid tabulation (other project)",
                "alternate_basis_label": PUBLIC_DISTRIBUTOR_EXACT,
                "same_price_as_gov": same_px,
            },
            "provenance": [
                {
                    "route": "opengov_public_vendor_bid",
                    "url": crec.get("source_url"),
                    "seller": cost_vendor,
                    "same_project": cost_hit.get("same_project"),
                    "same_buyer": cost_hit.get("same_buyer"),
                }
            ],
        }
    elif not out.get("public_cost"):
        out["public_cost"] = {
            "status": NOT_FOUND_AFTER_EXHAUSTIVE_SEARCH,
            "match_type": "NO_PUBLIC_PRICE",
            "failure_reason": "NO_PUBLIC_PRICE",
            "stop_reason": NO_PUBLIC_PRICE_AFTER_EXHAUSTIVE_SEARCH,
            "routes_attempted": [
                "opengov_bid_index_exact_pn",
                "opengov_bid_index_model",
                "opengov_bid_index_other_project",
            ],
        }

    # Human-like public price fallback when OpenGov index has no buy-side price
    # (or only same-project bids). Prefer real distributor/retail over leaving GOV_ONLY.
    need_public = (out.get("public_cost") or {}).get("status") != FOUND
    if allow_public_price and need_public and (pn or model):
        try:
            from public_price_search.models import (
                PRICE_SEARCH_BUDGET_EXHAUSTED as _PPS_BUDGET,
                PUBLIC_PRICE_FOUND as _PPS_FOUND,
                PUBLIC_PRICE_PARTIAL as _PPS_PARTIAL,
            )
            from public_price_search.resolver import resolve_public_price as _pps_resolve

            pps = _pps_resolve(
                identity,
                opportunity_id=oid,
                use_budget=True,
                max_queries=2,
                max_pages=5,
            )
            if pps.get("status") in {_PPS_FOUND, _PPS_PARTIAL}:
                ev = pps.get("evidence") or {}
                cost_price = float(ev.get("unit_price") or 0) or None
                if cost_price and cost_price > 0:
                    out["public_cost"] = {
                        "status": FOUND,
                        "match_type": ev.get("basis") or PUBLIC_DISTRIBUTOR_EXACT,
                        "confidence": "A" if pps.get("status") == _PPS_FOUND else "B",
                        "source": ev.get("seller") or "public_price_search_v2",
                        "evidence": {
                            "unit_price": cost_price,
                            "displayed_price": ev.get("displayed_price") or cost_price,
                            "core_charge": ev.get("core_charge"),
                            "core_refundable": ev.get("core_refundable"),
                            "gross_cash_required": ev.get("gross_cash_required"),
                            "condition": ev.get("condition"),
                            "uom": identity.get("uom_normalized") or identity.get("uom") or "EA",
                            "seller": ev.get("seller"),
                            "source_url": ev.get("source_url"),
                            "basis": ev.get("basis") or PUBLIC_DISTRIBUTOR_EXACT,
                            "exact_match": True,
                            "retrieved_via": "public_price_search_v2",
                            "shipping": ev.get("shipping"),
                            "same_price_as_gov": bool(
                                gov_unit is not None and abs(float(cost_price) - float(gov_unit)) < 1e-9
                            ),
                        },
                        "provenance": pps.get("provenance")
                        or [{"route": "public_price_search_v2", "url": ev.get("source_url")}],
                    }
            elif pps.get("status") == _PPS_BUDGET:
                out["public_cost"] = {
                    "status": NOT_FOUND_AFTER_EXHAUSTIVE_SEARCH,
                    "match_type": "NO_PUBLIC_PRICE",
                    "failure_reason": _PPS_BUDGET,
                    "stop_reason": _PPS_BUDGET,
                    "routes_attempted": ["public_price_search_v2"],
                }
        except Exception:
            pass

    gov_ok = (out["government_value"] or {}).get("status") == FOUND
    cost_ok = (out["public_cost"] or {}).get("status") == FOUND
    # Refresh cost_price for ratio checks when PPS filled it
    if cost_ok and cost_price is None:
        try:
            cost_price = float(((out.get("public_cost") or {}).get("evidence") or {}).get("unit_price"))
        except (TypeError, ValueError):
            cost_price = None
    # UOM: fail closed only when both sides have conflicting non-numeric pack codes
    bu = str(identity.get("uom_normalized") or identity.get("uom") or "EA").upper()
    gu = None
    if gov_ok:
        gu = (out["government_value"]["evidence"] or {}).get("uom")
    if gu and str(gu).isdigit():
        gu = bu
    cu = bu
    if gov_ok and gu and cu and _norm(str(gu)) != _norm(str(cu)) and {str(gu).upper(), str(cu).upper()} not in [{"EA", "EACH"}]:
        # Only block if clearly different pack families
        packish = {"CASE", "BOX", "PACK", "KIT", "SET"}
        if str(gu).upper() in packish or str(cu).upper() in packish:
            out["uom_ok"] = False
            out["line_status"] = UOM_BLOCKED
            return out

    same_px = bool(
        ((out.get("public_cost") or {}).get("evidence") or {}).get("same_price_as_gov")
    )
    # Reject extreme unit-price collisions (short-PN / wrong-item matches)
    ratio_ok = True
    if gov_ok and cost_ok and gov_unit and cost_price:
        hi = max(float(gov_unit), float(cost_price))
        lo = min(float(gov_unit), float(cost_price))
        if lo > 0 and hi / lo > 15.0:
            ratio_ok = False
            out["match_rejected"] = "UNIT_PRICE_RATIO_SUSPECT"
    if gov_ok and cost_ok and not same_px and ratio_ok:
        out["line_status"] = BOTH_SIDES_READY
    elif gov_ok and cost_ok and not ratio_ok:
        out["line_status"] = GOV_ONLY
        out["public_cost"]["note"] = "REJECTED_PRICE_RATIO_SUSPECT_MATCH"
    elif gov_ok and cost_ok and same_px:
        # Single public bid price is not a defensible buy-vs-sell spread
        out["line_status"] = GOV_ONLY
        out["public_cost"]["note"] = "SAME_AS_GOV_REFERENCE_NO_SPREAD"
    elif gov_ok:
        out["line_status"] = GOV_ONLY
    elif cost_ok:
        out["line_status"] = COST_ONLY
    else:
        out["line_status"] = NO_HISTORY_AFTER_EXHAUSTIVE_SEARCH
    return out
