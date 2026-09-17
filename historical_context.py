"""Canonical historical award/pricing access — HISTORICAL never becomes current revenue."""

from __future__ import annotations

from typing import Any

from data_integrity import STATUS_ASSESSMENT, STATUS_VERIFIED
from economic_integrity import historical_award_fact


def extract_historical_from_pricing_intel(pricing_intel: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Normalize pricing_intel awards into historical context envelopes."""
    if not isinstance(pricing_intel, dict):
        return []
    out: list[dict[str, Any]] = []
    awards = pricing_intel.get("awards") or []
    if not isinstance(awards, list):
        return out
    for aw in awards:
        if not isinstance(aw, dict):
            continue
        amount = aw.get("award_amount") or aw.get("amount") or aw.get("total_obligation")
        fact = historical_award_fact(
            amount,
            amount_basis="total",
            source_field="pricing_intel.awards",
        )
        if aw.get("award_id") or aw.get("generated_unique_award_id"):
            fact["source_reference"] = str(aw.get("award_id") or aw.get("generated_unique_award_id"))
        fact["source_type"] = str(pricing_intel.get("source") or "pricing_intel")
        offers = aw.get("number_of_offers_received")
        if offers is None:
            offers = aw.get("numberOfOffersReceived")
        unit_price = aw.get("unit_price")  # only if actually present
        out.append(
            {
                "temporal": "HISTORICAL",
                "knowledge_type": "HISTORICAL_AWARD",
                "award_identity": aw.get("award_id") or aw.get("generated_unique_award_id"),
                "agency": aw.get("agency") or aw.get("awarding_agency_name"),
                "awardee": aw.get("recipient_name") or aw.get("awardee") or aw.get("incumbent"),
                "amount_fact": fact,
                "award_date": aw.get("action_date") or aw.get("date") or aw.get("award_date"),
                "number_of_offers": offers,  # may be None — never invent
                "unit_price": unit_price,  # None unless established
                "unit_price_status": STATUS_VERIFIED if unit_price is not None else "UNKNOWN",
                "source": pricing_intel.get("source") or "pricing_intel",
                "retrieved_at": pricing_intel.get("fetched_at") or pricing_intel.get("cached_at"),
                "notes": "Historical government award ≠ current expected revenue or supplier cost",
                "relevance": "UNSCORED",
            }
        )
    return out


def extract_historical_from_gs_contract(row: Any) -> dict[str, Any]:
    """Map a gs_contracts ORM-like row into historical context."""
    amount = getattr(row, "award_amount", None)
    fact = historical_award_fact(
        float(amount) if amount is not None else None,
        amount_basis="total",
        source_field="gs_contracts.award_amount",
    )
    fact["source_type"] = "gs_contracts"
    fact["source_reference"] = str(getattr(row, "award_id", "") or "")
    offers = getattr(row, "number_of_offers_received", None)
    return {
        "temporal": "HISTORICAL",
        "knowledge_type": "HISTORICAL_AWARD",
        "award_identity": getattr(row, "award_id", None),
        "agency": getattr(row, "agency", None),
        "awardee": getattr(row, "incumbent_name", None),
        "amount_fact": fact,
        "award_date": str(getattr(row, "start_date", None) or ""),
        "number_of_offers": offers,
        "unit_price": None,
        "unit_price_status": "UNKNOWN",
        "source": "gs_contracts",
        "retrieved_at": str(getattr(row, "last_synced_at", None) or getattr(row, "updated_at", None) or ""),
        "notes": "Historical government award ≠ current expected revenue",
        "relevance": "UNSCORED",
    }


def get_historical_context_for_opportunity(
    opportunity: Any,
    *,
    session: Any = None,
    include_gs_lookup: bool = False,
) -> dict[str, Any]:
    """
    Canonical historical access. Does NOT call USAspending live.
    Reads opportunity.pricing_intel; optionally existing gs_contracts by NAICS (read-only).
    """
    pi = getattr(opportunity, "pricing_intel", None)
    if isinstance(opportunity, dict):
        pi = opportunity.get("pricing_intel")
    items = extract_historical_from_pricing_intel(pi if isinstance(pi, dict) else None)

    gs_items: list[dict[str, Any]] = []
    if include_gs_lookup and session is not None:
        naics = getattr(opportunity, "naics_code", None)
        if isinstance(opportunity, dict):
            naics = opportunity.get("naics_code")
        if naics:
            try:
                from sqlalchemy import text

                rows = session.execute(
                    text(
                        "SELECT award_id, agency, incumbent_name, award_amount, start_date, "
                        "number_of_offers_received, last_synced_at, updated_at "
                        "FROM gs_contracts WHERE naics_code = :n LIMIT 20"
                    ),
                    {"n": str(naics)},
                ).mappings().all()
                for r in rows:
                    gs_items.append(
                        extract_historical_from_gs_contract(type("R", (), dict(r))())
                    )
            except Exception:
                pass

    return {
        "temporal": "HISTORICAL",
        "from_pricing_intel": items,
        "from_gs_contracts": gs_items,
        "live_usaspending_called": False,
        "notes": [
            "Historical total award value is NOT unit price",
            "Historical award amount is NOT expected current revenue",
            "No invented competition count",
        ],
    }
