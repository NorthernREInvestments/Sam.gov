"""Evidence fact models — reconstructed specs never overwrite authoritative requirements."""

from __future__ import annotations
from application_clock import now_utc

from datetime import datetime, timezone
from typing import Any

from public_evidence_constants import EV_INFERRED, EV_UNKNOWN


def _utc() -> str:
    return now_utc().isoformat()


def evidence_fact(
    value: Any,
    *,
    evidence_class: str = EV_UNKNOWN,
    source_url: str | None = None,
    document: str | None = None,
    page_section: str | None = None,
    confidence: str = "UNKNOWN",
    relationship_to_current: str = "unrelated_or_unknown",
    authoritative_for_bidding: bool = False,
    notes: str | None = None,
) -> dict[str, Any]:
    """Single recovered fact with mandatory provenance."""
    if authoritative_for_bidding and evidence_class not in {
        "AUTHORITATIVE_CURRENT_SOLICITATION",
    }:
        # Hard guard: only current solicitation evidence may be authoritative for bidding
        authoritative_for_bidding = False
        notes = (notes or "") + "|authoritative_for_bidding_forced_false"
    return {
        "value": value,
        "evidence_class": evidence_class,
        "source_url": source_url,
        "document": document,
        "page_section": page_section,
        "retrieval_timestamp": _utc(),
        "confidence": confidence if value not in (None, "", []) else "UNKNOWN",
        "relationship_to_current": relationship_to_current,
        "authoritative_for_bidding": authoritative_for_bidding,
        "notes": notes,
    }


def empty_reconstructed_specification() -> dict[str, Any]:
    keys = [
        "blade_dimensions",
        "lengths",
        "width",
        "thickness",
        "hole_pattern",
        "mounting_pattern",
        "carbide_configuration",
        "rubber_encasement",
        "steel_material_requirements",
        "hardness",
        "carbide_grade_configuration",
        "cover_strap_requirements",
        "back_support_requirements",
        "acceptable_manufacturers",
        "historical_manufacturers",
        "equivalent_products",
        "certifications",
        "mill_certifications",
        "samples_testing",
        "packaging",
        "warranty",
        "delivery_conventions",
    ]
    return {
        "kind": "ReconstructedSpecification",
        "authoritative_for_current_bid": False,
        "fields": {k: evidence_fact(None, evidence_class=EV_UNKNOWN) for k in keys},
        "sources": [],
        "disclaimer": (
            "Reconstructed from public/historical/commercial evidence only. "
            "Does NOT replace TransactionalRequirement. "
            "authoritative_for_current_bid=false for all reconstructed fields."
        ),
    }


def set_reconstructed_field(
    spec: dict[str, Any],
    key: str,
    value: Any,
    *,
    evidence_class: str,
    source_url: str | None = None,
    document: str | None = None,
    page_section: str | None = None,
    confidence: str = "LOW",
    historical_or_current: str = "historical_or_commercial",
    notes: str | None = None,
) -> None:
    """Set a reconstructed field — never authoritative for current bid."""
    if key not in spec["fields"]:
        spec["fields"][key] = evidence_fact(None)
    fact = evidence_fact(
        value,
        evidence_class=evidence_class,
        source_url=source_url,
        document=document,
        page_section=page_section,
        confidence=confidence,
        relationship_to_current=historical_or_current,
        authoritative_for_bidding=False,
        notes=notes,
    )
    fact["historical_current_indicator"] = historical_or_current
    fact["authoritative_for_current_bid"] = False
    spec["fields"][key] = fact
    if source_url and source_url not in spec["sources"]:
        spec["sources"].append(source_url)


def agency_item_intelligence(
    *,
    agency_item_code: str,
    historical_description: str | None = None,
    known_dimensions: str | None = None,
    historical_supplier: str | None = None,
    manufacturer: str | None = None,
    part_number: str | None = None,
    historical_quantity: float | None = None,
    historical_unit_price: float | None = None,
    award_date: str | None = None,
    solicitation_contract_reference: str | None = None,
    source_url: str | None = None,
    evidence_class: str = EV_UNKNOWN,
    confidence: str = "UNKNOWN",
    authoritative_for_current_bid: bool = False,
) -> dict[str, Any]:
    if authoritative_for_current_bid and evidence_class != "AUTHORITATIVE_CURRENT_SOLICITATION":
        authoritative_for_current_bid = False
    return {
        "kind": "AgencyItemIntelligence",
        "agency_item_code": agency_item_code,
        "historical_description": historical_description,
        "known_dimensions": known_dimensions,
        "historical_supplier": historical_supplier,
        "manufacturer": manufacturer,
        "part_number": part_number,
        "historical_quantity": historical_quantity,
        "historical_unit_price": historical_unit_price,
        "price_date": award_date,
        "award_date": award_date,
        "solicitation_contract_reference": solicitation_contract_reference,
        "source": source_url,
        "evidence_class": evidence_class,
        "confidence": confidence,
        "authoritative_for_current_bid": authoritative_for_current_bid,
        "retrieval_timestamp": _utc(),
    }


def assert_reconstructed_not_merged_into_requirement(
    requirement: dict[str, Any],
    reconstructed: dict[str, Any],
) -> bool:
    """
    Guard: reconstructed field values must not appear as authoritative requirement facts.
    Returns True if separation holds.
    """
    terms = requirement.get("terms") or {}
    for key, fact in (reconstructed.get("fields") or {}).items():
        if not isinstance(fact, dict):
            continue
        if fact.get("authoritative_for_bidding") or fact.get("authoritative_for_current_bid"):
            return False
        # If requirement has same key as VERIFIED from reconstructed-only source, fail
        req_fact = terms.get(key)
        if isinstance(req_fact, dict) and req_fact.get("value") == fact.get("value"):
            if req_fact.get("confidence") == "VERIFIED_DOCUMENT" and fact.get("evidence_class") in {
                EV_INFERRED,
                "THIRD_PARTY_BID_MIRROR",
                "SEARCH_DISCOVERY_ONLY",
                "MANUFACTURER_EVIDENCE",
                "COMMERCIAL_DISTRIBUTOR_EVIDENCE",
                "HISTORICAL_AWARD",
            }:
                # Same value coincidence OK if requirement has its own provenance from solicitation
                src = (req_fact.get("source_document") or "").lower()
                if "reconstruct" in src or "historical" in src:
                    return False
    return True
