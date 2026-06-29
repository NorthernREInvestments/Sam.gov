"""Match contracts and awards by place of performance — same address + scope of work."""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from geo import _normalize_city
from usaspending_client import _parse_award_date, extract_work_location

_STREET_PATTERN = re.compile(
    r"(\d{1,6}\s+[\w\s\.\#\-]{2,80}?\b(?:"
    r"road|rd|street|st|avenue|ave|boulevard|blvd|drive|dr|lane|ln|way|"
    r"highway|hwy|court|ct|parkway|pkwy|circle|cir|trail|trl|place|pl|"
    r"terrace|ter|loop|lp|path|pike|square|sq"
    r")\b\.?)",
    re.IGNORECASE,
)

_STREET_ABBREV = {
    " rd ": " road ",
    " st ": " street ",
    " ave ": " avenue ",
    " blvd ": " boulevard ",
    " dr ": " drive ",
    " ln ": " lane ",
    " hwy ": " highway ",
    " ct ": " court ",
    " pkwy ": " parkway ",
    " cir ": " circle ",
    " pl ": " place ",
    " sq ": " square ",
    " n ": " north ",
    " s ": " south ",
    " e ": " east ",
    " w ": " west ",
}


def _normalize_address_key(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.lower().strip()
    cleaned = re.sub(r"[^\w\s#]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    padded = f" {cleaned} "
    for old, new in _STREET_ABBREV.items():
        padded = padded.replace(old, new)
    cleaned = re.sub(r"\s+", " ", padded).strip()
    return cleaned or None


def parse_street_from_text(text: str | None) -> str | None:
    """Pull the first plausible street address from free text."""
    if not text:
        return None
    match = _STREET_PATTERN.search(text)
    if not match:
        return None
    return match.group(1).strip().rstrip(".,;")


def _street_from_sam_block(sam_raw: dict[str, Any] | None) -> str | None:
    if not isinstance(sam_raw, dict):
        return None
    for key in ("placeOfPerformance", "placeOfPerformanceLocation"):
        block = sam_raw.get(key)
        if not isinstance(block, dict):
            continue
        parts = [block.get("streetAddress"), block.get("streetAddress2")]
        street = ", ".join(str(p).strip() for p in parts if p)
        if street:
            return street
    return None


def extract_site_profile(contract: Any) -> dict[str, Any]:
    """Address + scope fields used to decide if two contracts are the same site & work."""
    sam = contract.sam_raw if isinstance(getattr(contract, "sam_raw", None), dict) else {}
    analysis = contract.analysis if isinstance(getattr(contract, "analysis", None), dict) else {}
    loc = extract_work_location(
        getattr(contract, "location", None),
        sam,
    )

    street = _street_from_sam_block(sam)
    location_raw = getattr(contract, "location", None) or ""
    if not street:
        street = parse_street_from_text(location_raw)
    if not street:
        street = parse_street_from_text(sam.get("descriptionText"))
    if not street:
        street = parse_street_from_text(getattr(contract, "description", None))
    if not street:
        drawing = analysis.get("drawing_sqft_extraction")
        if isinstance(drawing, dict):
            street = parse_street_from_text(drawing.get("calculation_notes"))

    zip_code = loc.get("zip")
    address_key = None
    if street:
        normalized_street = _normalize_address_key(street)
        if normalized_street:
            address_key = f"{normalized_street}|{zip_code[:5]}" if zip_code else normalized_street

    return {
        **loc,
        "street_address": street,
        "address_key": address_key,
        "location_raw": location_raw,
        "naics_code": str(getattr(contract, "naics_code", None) or "").strip() or None,
        "building_type": getattr(contract, "building_type", None),
        "sub_type_needed": analysis.get("sub_type_needed"),
    }


def build_address_key_from_parts(street: str | None, zip_code: str | None) -> str | None:
    normalized = _normalize_address_key(street)
    if not normalized:
        return None
    zip_clean = str(zip_code or "")[:5]
    if zip_clean.isdigit() and len(zip_clean) == 5:
        return f"{normalized}|{zip_clean}"
    return normalized


def addresses_match(origin: dict[str, Any] | None, candidate: dict[str, Any] | None) -> bool:
    """True only when both sides resolve to the same normalized street address."""
    if not origin or not candidate:
        return False
    key_a = origin.get("address_key")
    key_b = candidate.get("address_key")
    if key_a and key_b:
        return key_a == key_b
    # If either side lacks a parseable street address, do not guess from city/ZIP alone.
    return False


def scope_of_work_matches(origin: dict[str, Any] | None, candidate: dict[str, Any] | None) -> bool:
    """Same scope — NAICS must match when known; otherwise matching sub trade type."""
    if not origin or not candidate:
        return False

    naics_a = str(origin.get("naics_code") or "").strip()
    naics_b = str(candidate.get("naics_code") or "").strip()
    if naics_a and naics_b:
        return naics_a == naics_b

    trade_a = str(origin.get("sub_type_needed") or "").strip().lower()
    trade_b = str(candidate.get("sub_type_needed") or "").strip().lower()
    if trade_a and trade_b:
        return trade_a == trade_b

    return False


def same_site_and_scope(origin: dict[str, Any] | None, candidate: dict[str, Any] | None) -> bool:
    """Same physical address and same scope of work (recompete / prior award at this facility)."""
    return addresses_match(origin, candidate) and scope_of_work_matches(origin, candidate)


def extract_award_site_profile(award: dict[str, Any], *, origin_naics: str | None = None) -> dict[str, Any]:
    street = parse_street_from_text(award.get("description"))
    if not street:
        street = parse_street_from_text(award.get("performance_location"))
    zip_code = award.get("performance_zip")
    return {
        "street_address": street,
        "address_key": build_address_key_from_parts(street, zip_code),
        "city": award.get("performance_city"),
        "state_code": award.get("performance_state"),
        "zip": zip_code,
        "naics_code": origin_naics,
        "location_raw": award.get("performance_location"),
    }


def is_award_expired(award: dict[str, Any], *, today: date | None = None) -> bool:
    today = today or date.today()
    end = _parse_award_date(award.get("end_date"))
    if end:
        return end < today
    return False


def annotate_and_prioritize_location_awards(
    awards: list[dict[str, Any]],
    origin_profile: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Flag awards at the same address & scope; put matching expired contracts first."""
    if not origin_profile or not awards:
        return awards

    annotated: list[dict[str, Any]] = []
    for raw in awards:
        award = dict(raw)
        award_profile = extract_award_site_profile(
            award,
            origin_naics=origin_profile.get("naics_code"),
        )
        same = same_site_and_scope(origin_profile, award_profile)
        expired = is_award_expired(award)
        award["same_location"] = same
        award["contract_expired"] = expired
        award["location_priority"] = same and expired
        if same and expired:
            award["location_note"] = "Same address & scope — prior contract expired"
        elif same:
            award["location_note"] = "Same address & scope"
        annotated.append(award)

    def sort_key(award: dict[str, Any]) -> tuple[int, int]:
        if award.get("location_priority"):
            tier = 0
        elif award.get("same_location"):
            tier = 1
        else:
            tier = 2
        date_str = award.get("award_date") or "0000-01-01"
        try:
            ordinal = date.fromisoformat(date_str).toordinal()
        except ValueError:
            ordinal = 0
        return (tier, -ordinal)

    annotated.sort(key=sort_key)
    return annotated


def query_site_contract_history(session: Any, contract: Any) -> list[dict[str, Any]]:
    """Prior contracts at the same address & scope with expired solicitations."""
    from models import Contract

    origin = extract_site_profile(contract)
    if not origin.get("address_key"):
        return []

    today = date.today()
    rows = (
        session.query(Contract)
        .filter(Contract.id != contract.id)
        .filter(Contract.due_date.isnot(None))
        .filter(Contract.due_date < today)
        .order_by(Contract.due_date.desc())
        .all()
    )

    history: list[dict[str, Any]] = []
    for row in rows:
        row_profile = extract_site_profile(row)
        if not same_site_and_scope(origin, row_profile):
            continue
        street = row_profile.get("street_address") or row.location
        history.append(
            {
                "notice_id": row.notice_id,
                "title": row.title,
                "agency": row.agency,
                "location": row.location,
                "street_address": street,
                "naics_code": row.naics_code,
                "due_date": row.due_date.isoformat() if row.due_date else None,
                "status": row.status,
                "awarded_amount": float(row.awarded_amount) if row.awarded_amount else None,
                "square_footage": row.square_footage,
                "price_per_sqft_per_visit": float(row.price_per_sqft_per_visit)
                if row.price_per_sqft_per_visit is not None
                else None,
                "location_note": "Same address & scope — prior solicitation expired",
                "same_location": True,
                "expired": True,
            }
        )
    return history


def prioritize_matched_contracts(contract: Any, matches: list[Any]) -> list[Any]:
    """Sort internal pricing matches — same address & scope + expired due date first."""
    origin = extract_site_profile(contract)
    today = date.today()

    def sort_key(row: Any) -> tuple[int, int]:
        row_profile = extract_site_profile(row)
        same = same_site_and_scope(origin, row_profile)
        expired = bool(row.due_date and row.due_date < today)
        if same and expired:
            tier = 0
        elif same:
            tier = 1
        else:
            tier = 2
        due_ord = row.due_date.toordinal() if row.due_date else 0
        return (tier, -due_ord)

    return sorted(matches, key=sort_key)


# Backward-compatible alias — now strict (address + scope).
def locations_match(origin: dict[str, Any] | None, candidate: dict[str, Any] | None) -> bool:
    return same_site_and_scope(origin, candidate)
