"""Commercial / wholesale / historical-winner intelligence — no false negatives."""

from __future__ import annotations

from typing import Any

from m3_evidence_constants import (
    COMPETITION_HISTORY_UNKNOWN,
    OEM_DOMINATED_HISTORY,
    OPEN_RESELLER_COMPETITION,
    PUBLIC_PRICE_UNWORKABLE,
    PUBLIC_RESEARCH_INCOMPLETE,
    RESELLER_HISTORY_PRESENT,
    SPECIALIZED_DISTRIBUTION_LIKELY,
    VERIFIED_ACQUISITION_UNWORKABLE,
    VERIFIED_ACQUISITION_WORKABLE,
    WHOLESALE_ACCESS_MAY_BE_REQUIRED,
    WHOLESALE_ACCESS_UNKNOWN,
    WHOLESALE_PRICE_VERIFICATION_REQUIRED,
)


def assess_commercial_economics(
    *,
    public_unit_price: float | None = None,
    government_historical_unit: float | None = None,
    target_acquisition_unit: float | None = None,
    verified_wholesale_unit: float | None = None,
    target_profit: float = 10000.0,
    quantity: float | None = 1.0,
) -> dict[str, Any]:
    """
    Distinguish PUBLIC_PRICE_UNWORKABLE from WHOLESALE_PRICE_VERIFICATION_REQUIRED.

    Public price above target ≠ automatic economic death.
    """
    qty = float(quantity or 1.0) or 1.0
    out: dict[str, Any] = {
        "kind": "M3CommercialEconomicsAssessment",
        "public_unit_price": public_unit_price,
        "government_historical_unit": government_historical_unit,
        "target_acquisition_unit": target_acquisition_unit,
        "verified_wholesale_unit": verified_wholesale_unit,
        "wholesale_access_status": WHOLESALE_ACCESS_UNKNOWN,
        "state": PUBLIC_RESEARCH_INCOMPLETE,
        "notes": [],
    }

    if target_acquisition_unit is None and government_historical_unit is not None:
        # Rough target: leave room for profit / freight / financing on historical revenue
        # Operator/financing still required — this is a research target only.
        rev = government_historical_unit * qty
        # If historical unit known, acquisition target aims to leave >= target_profit after rough 15% burden
        burden = 0.15
        affordable_total = max(0.0, rev * (1.0 - burden) - target_profit)
        target_acquisition_unit = round(affordable_total / qty, 4) if qty else None
        out["target_acquisition_unit"] = target_acquisition_unit
        out["notes"].append("target_derived_from_historical_government_unit")

    if verified_wholesale_unit is not None and target_acquisition_unit is not None:
        if verified_wholesale_unit <= target_acquisition_unit:
            out["state"] = VERIFIED_ACQUISITION_WORKABLE
            out["wholesale_access_status"] = "VERIFIED"
        else:
            out["state"] = VERIFIED_ACQUISITION_UNWORKABLE
            out["wholesale_access_status"] = "VERIFIED"
        return out

    if public_unit_price is None:
        out["state"] = PUBLIC_RESEARCH_INCOMPLETE
        out["notes"].append("public_price_unknown")
        return out

    if target_acquisition_unit is None:
        out["state"] = PUBLIC_RESEARCH_INCOMPLETE
        out["notes"].append("acquisition_target_unknown")
        return out

    if public_unit_price <= target_acquisition_unit:
        out["state"] = "PUBLIC_PRICE_WORKABLE_PRELIMINARY"
        out["notes"].append("public_price_meets_target")
        return out

    # Public price too high — NOT automatic reject
    out["state"] = WHOLESALE_PRICE_VERIFICATION_REQUIRED
    out["public_gap"] = round(public_unit_price - target_acquisition_unit, 4)
    out["notes"].append("public_price_above_target_wholesale_channel_may_exist")
    out["notes"].append("UNKNOWN_wholesale_access_is_not_unavailable")
    if government_historical_unit is not None and public_unit_price > 0:
        ratio = government_historical_unit / public_unit_price
        if ratio < 0.85:
            out["notes"].append("historical_government_paid_below_current_public_suggests_lower_channel")
            out["wholesale_access_status"] = WHOLESALE_ACCESS_MAY_BE_REQUIRED
    # Mark public unworkable as a sub-signal without killing the deal
    out["public_price_signal"] = PUBLIC_PRICE_UNWORKABLE
    out["future_action_if_pursued"] = "COMMERCIAL_WHOLESALE_VERIFICATION"
    return out


def assess_competition_history(awards: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Historical winner intelligence — never auto-reject solely on OEM/distributor dominance."""
    awards = awards or []
    if not awards:
        return {
            "kind": "M3CompetitionHistory",
            "signal": COMPETITION_HISTORY_UNKNOWN,
            "winner_count": 0,
            "notes": ["no_public_award_history"],
            "auto_reject": False,
        }

    winners = []
    for a in awards:
        w = str(a.get("winner") or a.get("vendor") or a.get("awardee") or "").strip()
        if w:
            winners.append(w)
    distinct = sorted(set(w.lower() for w in winners))
    oem_hits = sum(1 for a in awards if str(a.get("vendor_type") or "").upper() in {"OEM", "MANUFACTURER"})
    reseller_hits = sum(
        1 for a in awards if str(a.get("vendor_type") or "").upper() in {"RESELLER", "DISTRIBUTOR", "DEALER"}
    )

    signal = COMPETITION_HISTORY_UNKNOWN
    notes = []
    if reseller_hits and oem_hits == 0:
        signal = OPEN_RESELLER_COMPETITION if len(distinct) > 1 else RESELLER_HISTORY_PRESENT
        notes.append("reseller_or_distributor_wins_observed")
    elif oem_hits and reseller_hits == 0 and len(distinct) <= 2:
        signal = OEM_DOMINATED_HISTORY
        notes.append("oem_dominated_history_is_intelligence_not_rejection")
        notes.append(WHOLESALE_ACCESS_MAY_BE_REQUIRED)
    elif oem_hits and reseller_hits:
        signal = RESELLER_HISTORY_PRESENT
        notes.append("mixed_oem_and_reseller_history")
    elif len(distinct) == 1:
        signal = SPECIALIZED_DISTRIBUTION_LIKELY
        notes.append("single_repeat_winner")
    else:
        signal = OPEN_RESELLER_COMPETITION if len(distinct) >= 3 else RESELLER_HISTORY_PRESENT

    return {
        "kind": "M3CompetitionHistory",
        "signal": signal,
        "winner_count": len(distinct),
        "winners_sample": winners[:8],
        "oem_awards": oem_hits,
        "reseller_awards": reseller_hits,
        "repeat_winner_concentration": round(1.0 / max(1, len(distinct)), 3),
        "notes": notes,
        "auto_reject": False,
        "future_action_if_pursued": "SUPPLIER_CHANNEL_VERIFICATION",
    }


def build_commercial_research_panel(row: dict[str, Any]) -> dict[str, Any]:
    pricing = row.get("commercial_pricing") or row.get("public_pricing") or {}
    awards = row.get("historical_awards") or row.get("award_history") or []
    econ = assess_commercial_economics(
        public_unit_price=_num(pricing.get("lowest_public_new_unit") or pricing.get("public_unit_price")),
        government_historical_unit=_num(
            pricing.get("government_historical_unit") or pricing.get("latest_government_unit")
        ),
        target_acquisition_unit=_num(pricing.get("target_acquisition_unit") or row.get("target_acquisition_price")),
        verified_wholesale_unit=_num(pricing.get("verified_wholesale_unit")),
        quantity=_num((row.get("line_items") or [{}])[0].get("quantity") if row.get("line_items") else 1) or 1,
    )
    hist = assess_competition_history(awards if isinstance(awards, list) else [])
    return {
        "kind": "M3CommercialResearchPanel",
        "msrp_list": pricing.get("msrp") or pricing.get("list_price") or "UNKNOWN",
        "lowest_public_new_price": pricing.get("lowest_public_new_unit") or "UNKNOWN",
        "government_historical_price": pricing.get("government_historical_unit") or "UNKNOWN",
        "acquisition_target": econ.get("target_acquisition_unit") or "UNKNOWN",
        "wholesale_verification_status": econ.get("state"),
        "wholesale_access_status": econ.get("wholesale_access_status"),
        "economics_assessment": econ,
        "historical_winners": hist,
        "potential_economics": row.get("transaction_economics") or row.get("economics") or "UNKNOWN",
    }


def _num(v: Any) -> float | None:
    if v is None or v == "" or v == "UNKNOWN":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
