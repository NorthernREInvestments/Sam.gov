"""Plain-English display helpers for dashboard cards."""

from __future__ import annotations

import ast
import json
import re
from typing import Any

from usaspending_client import extract_work_location, normalize_state


def format_agency_display(agency: str | None) -> str:
    if not agency:
        return "Federal agency"
    text = str(agency).strip()
    if not text or text.startswith("{") or text.startswith("["):
        return "Federal agency"

    upper = text.upper()
    known = (
        ("FISH AND WILDLIFE", "US Fish and Wildlife"),
        ("FOREST SERVICE", "USDA Forest Service"),
        ("DEPT OF THE ARMY", "US Army"),
        ("DEPARTMENT OF THE ARMY", "US Army"),
        ("DEPT OF THE NAVY", "US Navy"),
        ("DEPT OF THE AIR FORCE", "US Air Force"),
        ("CORPS OF ENGINEERS", "US Army Corps of Engineers"),
        ("GENERAL SERVICES ADMINISTRATION", "GSA"),
        ("VETERANS AFFAIRS", "VA"),
        ("HOMELAND SECURITY", "DHS"),
        ("NATIONAL PARK SERVICE", "National Park Service"),
        ("BUREAU OF LAND MANAGEMENT", "BLM"),
    )
    for needle, label in known:
        if needle in upper:
            return label

    segments = [part.strip() for part in text.split(".") if part.strip()]
    for needle, label in known:
        for segment in segments:
            if needle in segment.upper():
                return label

    for segment in segments:
        seg_upper = segment.upper()
        if _is_parent_department_segment(seg_upper):
            continue
        if len(segment) > 3 and not seg_upper.startswith("USDA-"):
            return _title_agency(segment)

    if segments:
        return _title_agency(segments[-1])

    first = text.split(",")[0].strip()
    if first.upper() in ("AGRICULTURE", "AGRICULTURE, DEPARTMENT OF"):
        return "USDA"
    return _title_agency(first) or "Federal agency"


def _is_parent_department_segment(seg_upper: str) -> bool:
    if "DEPARTMENT OF" in seg_upper:
        return True
    if seg_upper in ("AGRICULTURE", "DEFENSE", "INTERIOR", "COMMERCE", "JUSTICE", "TREASURY"):
        return True
    if seg_upper.startswith("AGRICULTURE,"):
        return True
    return False


def format_department_display(agency: str | None) -> str:
    """Parent department for dashboard cards — e.g. Dept of Ag, US Army."""
    if not agency:
        return "Federal agency"
    upper = str(agency).strip().upper()
    if not upper or upper.startswith("{"):
        return "Federal agency"

    rules: tuple[tuple[str, ...], str] = (
        (("AGRICULTURE", "FOREST SERVICE", "USDA"), "Dept of Ag"),
        (("DEPT OF THE ARMY", "DEPARTMENT OF THE ARMY"), "US Army"),
        (("DEPT OF THE NAVY", "DEPARTMENT OF THE NAVY"), "US Navy"),
        (("DEPT OF THE AIR FORCE", "DEPARTMENT OF THE AIR FORCE"), "US Air Force"),
        (("CORPS OF ENGINEERS",), "US Army"),
        (("DEPT OF DEFENSE", "DEPARTMENT OF DEFENSE"), "Dept of Defense"),
        (("INTERIOR", "FISH AND WILDLIFE", "NATIONAL PARK SERVICE", "BUREAU OF LAND MANAGEMENT"), "Dept of Interior"),
        (("VETERANS AFFAIRS",), "VA"),
        (("HOMELAND SECURITY",), "DHS"),
        (("GENERAL SERVICES",), "GSA"),
    )
    for needles, label in rules:
        if any(needle in upper for needle in needles):
            return label

    segments = [part.strip() for part in str(agency).split(".") if part.strip()]
    if segments:
        first = segments[0].split(",")[0].strip()
        if first.upper() == "AGRICULTURE":
            return "Dept of Ag"
        return _title_agency(first) or "Federal agency"
    return "Federal agency"


def format_service_type_display(naics_code: str | None, naics_label: str | None = None) -> str:
    """Short service label for cards — e.g. Janitorial, Landscaping."""
    from naics_labels import naics_label as lookup_label

    label = (naics_label or lookup_label(naics_code or "") or "").strip()
    if not label:
        return "Contract"
    short = re.sub(r"\s+Services?$", "", label, flags=re.IGNORECASE).strip()
    return short or label


def format_work_location_short(
    location: str | None,
    sam_raw: dict[str, Any] | None = None,
    work: dict[str, Any] | None = None,
) -> str:
    work = work or extract_work_location(location, sam_raw)
    city = work.get("city")
    state = work.get("state_code")
    if city and state:
        return f"{city}, {state}"
    if state:
        return state
    label = work.get("label")
    if label:
        return _label_to_city_state(label) or label

    parsed = _parse_location_blob(location)
    if parsed:
        city = parsed.get("city")
        state = parsed.get("state_code")
        if city and state:
            return f"{city}, {state}"
        if state:
            return state
    if location and not str(location).strip().startswith("{"):
        cleaned = str(location).strip()
        if len(cleaned) <= 80:
            return cleaned
    return "Location pending"


def format_work_address_display(contract: Any) -> str | None:
    """Street address line for dashboard cards — from SAM place of performance or text."""
    from location_matching import extract_site_profile

    site = extract_site_profile(contract)
    street = (site.get("street_address") or "").strip()
    if not street:
        return None
    city = site.get("city")
    state = site.get("state_code")
    zip_code = site.get("zip")
    locality = ", ".join(part for part in (city, state) if part)
    if locality and zip_code:
        locality = f"{locality} {zip_code}"
    elif zip_code:
        locality = zip_code
    if locality:
        return f"{street} · {locality}"
    return street


def prior_hints_from_contract(contract: Any) -> dict[str, Any]:
    """Prior-contract dollars and names from PDF extraction / solicitation meta."""
    from prior_contract_extract import _clean_contract_number, _clean_incumbent_name

    analysis = contract.analysis if isinstance(getattr(contract, "analysis", None), dict) else {}
    sol = analysis.get("solicitation_meta") if isinstance(analysis.get("solicitation_meta"), dict) else {}
    hints: dict[str, Any] = {
        "previous_contract_number": _clean_contract_number(
            str(
                sol.get("manual_previous_contract_number")
                or sol.get("previous_contract_number")
                or analysis.get("previous_contract_number")
                or ""
            )
        ),
        "incumbent_contractor": _clean_incumbent_name(
            str(sol.get("incumbent_contractor") or analysis.get("incumbent_contractor") or "")
        ),
        "tcv": sol.get("prior_contract_tcv"),
        "annual": sol.get("prior_contract_annual"),
        "amount_source": sol.get("prior_contract_amount_source"),
    }
    if hints["tcv"] or hints["annual"]:
        if not hints["annual"] and hints["tcv"]:
            from prior_contract_extract import _estimate_contract_years

            years = _estimate_contract_years(
                f"{getattr(contract, 'estimated_value', '') or ''} {analysis.get('estimated_value') or ''}"
            )
            if not years:
                years = _estimate_contract_years(getattr(contract, "attachment_text", None) or "")
            if years:
                hints["annual"] = round(float(hints["tcv"]) / years, 2)
        return hints

    from prior_contract_extract import parse_prior_amount_from_estimated_value

    estimated = parse_prior_amount_from_estimated_value(
        getattr(contract, "estimated_value", None) or analysis.get("estimated_value")
    )
    if estimated.get("prior_contract_tcv"):
        hints["tcv"] = estimated["prior_contract_tcv"]
        hints["annual"] = estimated.get("prior_contract_annual")
        hints["amount_source"] = estimated.get("prior_contract_amount_source")
    return hints


def _recent_award_from_intel(intel: dict[str, Any]) -> tuple[float | None, str | None]:
    """Best available dollar amount from cached USAspending awards."""
    from pricing_constants import MIN_REGIONAL_AWARD_AMOUNT

    for award in intel.get("awards") or []:
        if not isinstance(award, dict):
            continue
        amount = award.get("recent_annual_amount") or award.get("annual_amount") or award.get("award_amount")
        try:
            value = float(amount) if amount is not None else None
        except (TypeError, ValueError):
            value = None
        if value and value >= MIN_REGIONAL_AWARD_AMOUNT:
            return value, _short_company_name(award.get("recipient_name"))
    avg = intel.get("average_annual_award")
    if avg:
        try:
            return float(avg), _short_company_name(intel.get("likely_incumbent"))
        except (TypeError, ValueError):
            pass
    return None, None


def pricing_card_display(
    pricing_intel: dict[str, Any] | None,
    *,
    has_work_state: bool = False,
    prior_hints: dict[str, Any] | None = None,
) -> dict[str, str | int | float | None]:
    """Structured price line for dashboard cards — separates prior contract from regional average."""
    intel = pricing_intel if isinstance(pricing_intel, dict) else {}
    predecessor = intel.get("predecessor_award") if isinstance(intel.get("predecessor_award"), dict) else None

    if predecessor and predecessor.get("is_prior_contract"):
        annual = (
            predecessor.get("recent_annual_amount")
            or predecessor.get("annual_amount")
            or predecessor.get("base_year_amount")
            or predecessor.get("total_value")
        )
        recipient = _short_company_name(predecessor.get("recipient_name"))
        method = predecessor.get("lookup_method") or ""
        if method in ("contract_number", "contract_number_keyword", "manual_contract_number"):
            label = "Prior contract"
        elif method == "same_site_match":
            label = "Prior (same address)"
        elif method == "facility_keyword":
            label = "Prior (facility)"
        elif method == "recipient_search":
            label = "Prior (incumbent)"
        elif method == "same_city_match":
            label = "Prior (same city)"
        else:
            label = "Prior (incumbent)"
        amount_suffix = "/yr"
        if predecessor.get("recent_annual_amount") and predecessor.get("award_amount_source") == "transaction_history":
            amount_suffix = "/yr recent"
        if annual and recipient:
            return {
                "kind": "prior_contract",
                "label": label,
                "amount": short_money(annual),
                "recipient": recipient,
                "line": f"{label}: {short_money(annual)}{amount_suffix} · {recipient}",
                "confidence": predecessor.get("confidence") or "high",
                "calc_note": predecessor.get("pricing_calc_note"),
            }
        if annual:
            return {
                "kind": "prior_contract",
                "label": label,
                "amount": short_money(annual),
                "recipient": None,
                "line": f"{label}: {short_money(annual)}{amount_suffix}",
                "confidence": predecessor.get("confidence") or "high",
                "calc_note": predecessor.get("pricing_calc_note"),
            }

    hints = prior_hints if isinstance(prior_hints, dict) else {}
    pdf_annual = hints.get("annual")
    pdf_tcv = hints.get("tcv")
    incumbent = _short_company_name(hints.get("incumbent_contractor"))
    prev_number = str(hints.get("previous_contract_number") or "").strip() or None

    if pdf_annual or pdf_tcv:
        label = "Prior contract"
        if pdf_annual and incumbent:
            line = f"{label}: {short_money(pdf_annual)}/yr · {incumbent}"
            amount = short_money(pdf_annual)
        elif pdf_annual:
            line = f"{label}: {short_money(pdf_annual)}/yr"
            amount = short_money(pdf_annual)
        elif pdf_tcv and incumbent:
            line = f"{label}: {short_money(pdf_tcv)} total · {incumbent}"
            amount = short_money(pdf_tcv)
        else:
            line = f"{label}: {short_money(pdf_tcv)} total"
            amount = short_money(pdf_tcv)
        return {
            "kind": "prior_contract",
            "label": label,
            "amount": amount,
            "recipient": incumbent,
            "line": line,
            "confidence": "medium" if hints.get("amount_source") == "pdf" else "low",
            "calc_note": "From solicitation PDF prior-contract section",
        }

    if prev_number or incumbent:
        annual, recipient = _recent_award_from_intel(intel)
        if annual:
            label = "Prior (same area)"
            line = f"{label}: {short_money(annual)}/yr recent"
            if recipient:
                line += f" · {recipient}"
            return {
                "kind": "prior_contract",
                "label": label,
                "amount": short_money(annual),
                "recipient": recipient,
                "line": line,
                "confidence": intel.get("confidence") or "medium",
                "calc_note": "Most recent comparable award in this area from USAspending.",
            }

    if intel.get("error"):
        return {
            "kind": "none",
            "label": None,
            "amount": None,
            "recipient": None,
            "line": "Prior contract amount not on file",
            "confidence": None,
        }

    if has_work_state:
        return {
            "kind": "first_at_location",
            "label": None,
            "amount": None,
            "recipient": None,
            "line": "No prior contract on file",
            "confidence": None,
        }
    return {
        "kind": "none",
        "label": None,
        "amount": None,
        "recipient": None,
        "line": "No prior contract on file",
        "confidence": None,
    }


def pricing_regional_display(pricing_intel: dict[str, Any] | None) -> dict[str, str | int | None] | None:
    """Regional benchmark line for contract detail — not used on dashboard cards."""
    intel = pricing_intel if isinstance(pricing_intel, dict) else {}
    avg = intel.get("average_annual_award")
    count = int(intel.get("awards_count") or 0)
    state = intel.get("state_code") or intel.get("state_name")
    if not avg or count <= 0:
        return None
    scope = f"{count} in {state}" if state else f"{count} contracts"
    return {
        "kind": "regional_average",
        "label": "Regional avg",
        "amount": short_money(avg),
        "recipient": None,
        "line": f"Regional avg: {short_money(avg)}/yr ({scope})",
        "confidence": intel.get("confidence"),
    }


def pricing_card_label(
    pricing_intel: dict[str, Any] | None,
    *,
    has_work_state: bool = False,
    prior_hints: dict[str, Any] | None = None,
) -> str:
    return str(
        pricing_card_display(
            pricing_intel,
            has_work_state=has_work_state,
            prior_hints=prior_hints,
        ).get("line")
        or "No prior contract on file"
    )


def _short_company_name(name: str | None, max_len: int = 22) -> str | None:
    if not name:
        return None
    cleaned = re.sub(r"\s+", " ", str(name).strip())
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[: max_len - 1].rstrip() + "…"


def short_money(value: float | int | None) -> str:
    if value is None:
        return "—"
    amount = float(value)
    if amount >= 1_000_000:
        return f"${amount / 1_000_000:.1f}M".replace(".0M", "M")
    if amount >= 1_000:
        return f"${round(amount / 1_000)}k"
    return f"${amount:,.0f}"


def _title_agency(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", value).strip(" .")
    if not cleaned:
        return ""
    lower = cleaned.lower()
    if lower.startswith("dept of "):
        cleaned = cleaned[8:]
    return cleaned.title()


def _label_to_city_state(label: str) -> str | None:
    parts = [part.strip() for part in label.split(",") if part.strip()]
    if len(parts) >= 2:
        city = parts[0]
        state = normalize_state(parts[-1])
        if city and state:
            return f"{city}, {state}"
    return None


def _parse_location_blob(location: Any) -> dict[str, Any] | None:
    if isinstance(location, dict):
        block = location
    elif isinstance(location, str):
        text = location.strip()
        if not text.startswith("{"):
            return None
        try:
            block = json.loads(text.replace("'", '"'))
        except (json.JSONDecodeError, TypeError):
            try:
                block = ast.literal_eval(text)
            except (SyntaxError, ValueError):
                return None
        if not isinstance(block, dict):
            return None
    else:
        return None

    from usaspending_client import _parse_sam_location_block

    city, state_code, _zip = _parse_sam_location_block(block)
    return {"city": city, "state_code": state_code}
