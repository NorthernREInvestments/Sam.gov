"""Persist PWS scope fields from Claude screening onto contract records."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from pricing_constants import BUILDING_TYPES, STATE_TO_MACRO_REGION
from usaspending_client import extract_work_location


def _parse_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().replace(",", "")
    match = re.search(r"(\d{3,})", text)
    return int(match.group(1)) if match else None


def _parse_frequency(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value))
    text = str(value).lower()
    if "daily" in text and "week" not in text:
        return Decimal("7")
    if "monday through friday" in text or "mon thru fri" in text or "m-f" in text:
        return Decimal("5")
    if "five days" in text or "5 days" in text:
        return Decimal("5")
    if "three times" in text or "3 times" in text:
        return Decimal("3")
    if "twice" in text or "2 times" in text:
        return Decimal("2")
    if "weekly" in text and "per week" not in text:
        return Decimal("1")
    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:days?\s+per\s+week|x\s+per\s+week|times?\s+per\s+week)", text)
    if match:
        return Decimal(match.group(1))
    match = re.search(r"(\d+(?:\.\d+)?)", text)
    return Decimal(match.group(1)) if match else None


def _normalize_building_type(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).strip().lower()
    for bt in BUILDING_TYPES:
        if bt in text:
            return bt
    return "other" if text else None


def _parse_special_requirements(value: Any) -> list[str] | None:
    if not value:
        return None
    if isinstance(value, list):
        items = [str(v).strip() for v in value if str(v).strip()]
        return items or None
    if isinstance(value, str):
        parts = [p.strip() for p in re.split(r"[,;]", value) if p.strip()]
        return parts or None
    return None


def annual_visits(frequency_per_week: Decimal | float | None) -> Decimal | None:
    if frequency_per_week is None:
        return None
    return Decimal(str(frequency_per_week)) * Decimal("52")


def recalculate_pricing_derivatives(contract: Any) -> None:
    """Update price_per_sqft_per_year and price_per_sqft_per_visit from awarded_amount."""
    sqft = contract.square_footage
    awarded = contract.awarded_amount
    visits = annual_visits(contract.cleaning_frequency_per_week)

    if awarded is not None and sqft and sqft > 0:
        contract.price_per_sqft_per_year = Decimal(str(awarded)) / Decimal(str(sqft))
    else:
        contract.price_per_sqft_per_year = None

    if contract.price_per_sqft_per_year is not None and visits and visits > 0:
        contract.price_per_sqft_per_visit = contract.price_per_sqft_per_year / visits
    else:
        contract.price_per_sqft_per_visit = None


def contract_pws_missing(contract: Any) -> bool:
    """True when key PWS scope fields are not yet persisted on the contract row."""
    naics = str(getattr(contract, "naics_code", None) or "").strip()
    cleaning_naics = {"561720", "561790", "561740", "561210"}
    if naics in cleaning_naics:
        return (
            contract.square_footage is None
            or contract.cleaning_frequency_per_week is None
        )
    return (
        contract.square_footage is None
        and contract.cleaning_frequency_per_week is None
        and contract.wage_determination_number is None
    )


def supplement_pws_from_pdf_text(analysis: dict[str, Any], text: str) -> None:
    """Fill missing PWS fields from regex patterns in extracted PDF text."""
    if not text.strip():
        return
    pws = analysis.setdefault("pws_extraction", {})
    if not isinstance(pws, dict):
        pws = {}
        analysis["pws_extraction"] = pws

    if not pws.get("square_footage"):
        patterns = [
            r"(?:total\s+)?cleanable\s+square\s+footage[^\d]*([\d,]+)",
            r"(?:gross|net|total)\s+square\s+footage[^\d]*([\d,]+)",
            r"(?:building|facility|structure)\s+(?:area|size)[^\d]*([\d,]+)\s*(?:sq\.?\s*ft|sf|square\s+feet)",
            r"([\d,]{3,})\s*(?:sq\.?\s*ft|sf|square\s+feet)\b",
            r"([\d,]{4,})\s+SF\b",
            r"area[^\d]{0,40}([\d,]{3,})\s*(?:sq|sf)",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                pws["square_footage"] = int(match.group(1).replace(",", ""))
                break

    if not pws.get("wage_determination_number"):
        wd = re.search(r"WD[\s-]*(\d{4}[-\s]?\d{4})", text, flags=re.IGNORECASE)
        if wd:
            pws["wage_determination_number"] = f"WD {wd.group(1).replace(' ', '-')}"

    if re.search(r"monday\s+through\s+friday|mon\s*[-–]\s*fri|5\s+days?\s+per\s+week", text, re.I):
        pws["cleaning_frequency_per_week"] = 5


def apply_pws_extraction(contract: Any, analysis: dict[str, Any]) -> None:
    """Map Claude PWS extraction fields onto the contract row."""
    pws = analysis.get("pws_extraction")
    if not isinstance(pws, dict):
        pws = {}

    sqft = _parse_int(pws.get("square_footage") or analysis.get("square_footage"))
    if sqft:
        contract.square_footage = sqft

    freq = _parse_frequency(pws.get("cleaning_frequency_per_week"))
    if freq is not None:
        contract.cleaning_frequency_per_week = freq

    building = _normalize_building_type(pws.get("building_type"))
    if building:
        contract.building_type = building

    specials = _parse_special_requirements(pws.get("special_requirements"))
    if specials:
        contract.special_requirements = specials

    wd_num = pws.get("wage_determination_number")
    if wd_num:
        contract.wage_determination_number = str(wd_num).strip()[:32]

    wd_rate = pws.get("wage_determination_rate")
    if wd_rate not in (None, ""):
        try:
            contract.wage_determination_rate = Decimal(str(wd_rate))
        except Exception:
            pass

    work = extract_work_location(
        contract.location,
        contract.sam_raw if isinstance(getattr(contract, "sam_raw", None), dict) else None,
    )
    state = work.get("state_code")
    if state:
        contract.pricing_region = state
    elif contract.naics_code and not contract.pricing_region:
        pass

    recalculate_pricing_derivatives(contract)


def pws_read_status(contract: Any) -> dict[str, Any]:
    """Explain whether PWS PDFs were read or are still pending (e.g. PIEE download)."""
    raw = contract.sam_raw if isinstance(getattr(contract, "sam_raw", None), dict) else {}
    analysis = contract.analysis if isinstance(getattr(contract, "analysis", None), dict) else {}
    piee_names = raw.get("pieeAttachments") or []
    piee_count = len(piee_names) if isinstance(piee_names, list) else 0
    pdfs_sent = int(analysis.get("pdfs_sent_to_claude") or analysis.get("piee_pdfs_sent") or 0)
    skipped = analysis.get("pdfs_not_included") or []
    missing = contract_pws_missing(contract)

    if not missing:
        return {"status": "complete", "message": None}

    if piee_count and pdfs_sent == 0:
        piee_fail = any("PIEE" in str(s) for s in skipped) if isinstance(skipped, list) else False
        if piee_fail or piee_count:
            return {
                "status": "pending_piee",
                "message": (
                    f"{piee_count} solicitation PDF(s) are on PIEE — automatic download in progress."
                ),
                "piee_count": piee_count,
            }

    sam_files = [
        a for a in (raw.get("opportunityAttachments") or [])
        if isinstance(a, dict) and a.get("type") == "file"
    ]
    if sam_files and pdfs_sent == 0:
        return {
            "status": "pending_pdfs",
            "message": "SAM.gov PDF attachments are being downloaded and read automatically.",
        }

    return {"status": "incomplete", "message": "Scope fields not found in documents read so far."}


def pws_snapshot(contract: Any) -> dict[str, Any]:
    freq = contract.cleaning_frequency_per_week
    visits = annual_visits(freq)
    return {
        "square_footage": contract.square_footage,
        "building_type": contract.building_type,
        "cleaning_frequency_per_week": float(freq) if freq is not None else None,
        "annual_visits": float(visits) if visits is not None else None,
        "special_requirements": contract.special_requirements or [],
        "wage_determination_number": contract.wage_determination_number,
        "wage_determination_rate": float(contract.wage_determination_rate)
        if contract.wage_determination_rate is not None
        else None,
        "awarded_amount": float(contract.awarded_amount) if contract.awarded_amount is not None else None,
        "price_per_sqft_per_year": float(contract.price_per_sqft_per_year)
        if contract.price_per_sqft_per_year is not None
        else None,
        "price_per_sqft_per_visit": float(contract.price_per_sqft_per_visit)
        if contract.price_per_sqft_per_visit is not None
        else None,
        "pricing_region": contract.pricing_region,
        "naics_code": contract.naics_code,
        "option_years": (contract.analysis or {}).get("option_years") if isinstance(contract.analysis, dict) else None,
        **pws_read_status(contract),
    }
