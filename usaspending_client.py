"""USAspending.gov historical award search for pricing intelligence."""

from __future__ import annotations

import re
import statistics
import time
from collections import Counter
from datetime import date, timedelta
from typing import Any

import httpx

from geo import annotate_award_distances

BASE_URL = "https://api.usaspending.gov"
SEARCH_PATH = "/api/v2/search/spending_by_award/"

# Definitive contract award types (excludes grants, loans, IDVs)
CONTRACT_AWARD_TYPE_CODES = ["A", "B", "C", "D"]

AWARD_FIELDS = [
    "Award ID",
    "Recipient Name",
    "Award Amount",
    "Start Date",
    "End Date",
    "Awarding Agency",
    "Contract Award Type",
    "Description",
    "Place of Performance State Code",
    "Place of Performance City Name",
    "Place of Performance Zip5",
    "generated_internal_id",
    "NAICS",
]

# Pull extra candidates, then keep only scope-similar awards.
SEARCH_FETCH_LIMIT = 100
COMPARABLE_DISPLAY_LIMIT = 20

STATE_NAME_TO_CODE: dict[str, str] = {
    "ALABAMA": "AL",
    "ALASKA": "AK",
    "ARIZONA": "AZ",
    "ARKANSAS": "AR",
    "CALIFORNIA": "CA",
    "COLORADO": "CO",
    "CONNECTICUT": "CT",
    "DELAWARE": "DE",
    "DISTRICT OF COLUMBIA": "DC",
    "FLORIDA": "FL",
    "GEORGIA": "GA",
    "HAWAII": "HI",
    "IDAHO": "ID",
    "ILLINOIS": "IL",
    "INDIANA": "IN",
    "IOWA": "IA",
    "KANSAS": "KS",
    "KENTUCKY": "KY",
    "LOUISIANA": "LA",
    "MAINE": "ME",
    "MARYLAND": "MD",
    "MASSACHUSETTS": "MA",
    "MICHIGAN": "MI",
    "MINNESOTA": "MN",
    "MISSISSIPPI": "MS",
    "MISSOURI": "MO",
    "MONTANA": "MT",
    "NEBRASKA": "NE",
    "NEVADA": "NV",
    "NEW HAMPSHIRE": "NH",
    "NEW JERSEY": "NJ",
    "NEW MEXICO": "NM",
    "NEW YORK": "NY",
    "NORTH CAROLINA": "NC",
    "NORTH DAKOTA": "ND",
    "OHIO": "OH",
    "OKLAHOMA": "OK",
    "OREGON": "OR",
    "PENNSYLVANIA": "PA",
    "RHODE ISLAND": "RI",
    "SOUTH CAROLINA": "SC",
    "SOUTH DAKOTA": "SD",
    "TENNESSEE": "TN",
    "TEXAS": "TX",
    "UTAH": "UT",
    "VERMONT": "VT",
    "VIRGINIA": "VA",
    "WASHINGTON": "WA",
    "WEST VIRGINIA": "WV",
    "WISCONSIN": "WI",
    "WYOMING": "WY",
    **{code: code for code in [
        "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID",
        "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO",
        "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA",
        "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    ]},
}

STATE_CODE_TO_NAME: dict[str, str] = {
    code: name.title()
    for name, code in STATE_NAME_TO_CODE.items()
    if len(name) > 2
}

MIN_LOCAL_COMPARABLE_AWARDS = 3

# Land-adjacent states only — widest geographic search allowed.
BORDERING_STATES: dict[str, list[str]] = {
    "AL": ["MS", "TN", "GA", "FL"],
    "AK": [],
    "AZ": ["CA", "NV", "UT", "CO", "NM"],
    "AR": ["MO", "TN", "MS", "LA", "TX", "OK"],
    "CA": ["OR", "NV", "AZ"],
    "CO": ["WY", "NE", "KS", "OK", "NM", "AZ", "UT"],
    "CT": ["NY", "MA", "RI"],
    "DC": ["MD", "VA"],
    "DE": ["MD", "PA", "NJ"],
    "FL": ["GA", "AL"],
    "GA": ["FL", "AL", "TN", "NC", "SC"],
    "HI": [],
    "ID": ["MT", "WY", "UT", "NV", "OR", "WA"],
    "IL": ["WI", "IA", "MO", "KY", "IN"],
    "IN": ["MI", "OH", "KY", "IL"],
    "IA": ["MN", "WI", "IL", "MO", "NE", "SD"],
    "KS": ["NE", "MO", "OK", "CO"],
    "KY": ["IL", "IN", "OH", "WV", "VA", "TN", "MO"],
    "LA": ["TX", "AR", "MS"],
    "ME": ["NH"],
    "MD": ["PA", "DE", "WV", "VA", "DC"],
    "MA": ["NH", "RI", "CT", "NY", "VT"],
    "MI": ["OH", "IN", "WI"],
    "MN": ["WI", "IA", "SD", "ND"],
    "MS": ["LA", "AR", "TN", "AL"],
    "MO": ["IA", "IL", "KY", "TN", "AR", "OK", "KS", "NE"],
    "MT": ["ND", "SD", "WY", "ID"],
    "NE": ["SD", "IA", "MO", "KS", "CO", "WY"],
    "NV": ["OR", "ID", "UT", "AZ", "CA"],
    "NH": ["ME", "MA", "VT"],
    "NJ": ["NY", "PA", "DE"],
    "NM": ["AZ", "UT", "CO", "OK", "TX"],
    "NY": ["VT", "MA", "CT", "NJ", "PA"],
    "NC": ["VA", "TN", "GA", "SC"],
    "ND": ["MN", "SD", "MT"],
    "OH": ["PA", "WV", "KY", "IN", "MI"],
    "OK": ["KS", "MO", "AR", "TX", "NM", "CO"],
    "OR": ["WA", "ID", "NV", "CA"],
    "PA": ["NY", "NJ", "DE", "MD", "WV", "OH"],
    "RI": ["MA", "CT"],
    "SC": ["NC", "GA"],
    "SD": ["ND", "MN", "IA", "NE", "WY", "MT"],
    "TN": ["KY", "VA", "NC", "GA", "AL", "MS", "AR", "MO"],
    "TX": ["NM", "OK", "AR", "LA"],
    "UT": ["ID", "WY", "CO", "NM", "AZ", "NV"],
    "VT": ["NY", "NH", "MA"],
    "VA": ["MD", "WV", "KY", "TN", "NC", "DC"],
    "WA": ["ID", "OR"],
    "WV": ["PA", "MD", "VA", "KY", "OH"],
    "WI": ["MI", "MN", "IA", "IL"],
    "WY": ["MT", "SD", "NE", "CO", "UT", "ID"],
}


def _state_names(codes: list[str]) -> str:
    return ", ".join(STATE_CODE_TO_NAME.get(code, code) for code in codes)


def normalize_state(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = re.sub(r"\s+", " ", value.strip().upper())
    if not cleaned:
        return None
    if cleaned in STATE_NAME_TO_CODE:
        return STATE_NAME_TO_CODE[cleaned]
    match = re.fullmatch(r"[A-Z]{2}", cleaned)
    if match and cleaned in STATE_NAME_TO_CODE:
        return cleaned
    return None


def _parse_sam_location_block(block: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    city = block.get("city")
    if isinstance(city, dict):
        city = city.get("name") or city.get("code")
    state = block.get("state") or block.get("stateCode") or block.get("state_code")
    if isinstance(state, dict):
        state = state.get("code") or state.get("name")
    zip_code = block.get("zip") or block.get("zipcode") or block.get("zipCode")
    state_code = normalize_state(str(state) if state else "")
    city_name = str(city).strip() if city else None
    zip_text = str(zip_code).strip()[:5] if zip_code else None
    return city_name or None, state_code, zip_text


def _infer_city_from_title(title: str | None, state_code: str | None) -> str | None:
    """Pull a city name from titles like 'WA-RIDGEFIELD NWR' or 'Janitorial Services, Boise ID'."""
    if not title:
        return None
    text = str(title).strip()
    if state_code:
        match = re.search(
            rf"\b{re.escape(state_code)}[-\s]+([A-Za-z][A-Za-z\s'-]{{2,40}}?)(?:\s+NWR|\s+AFB|\s+REFUGE|\s+RANGER|\s+DISTRICT|\s+HQ|\s+HEADQUARTERS|\s+SERVICES|\s+JANITORIAL|\s+CUSTODIAL|\b)",
            text,
            flags=re.IGNORECASE,
        )
        if match:
            return match.group(1).strip(" -").title()
    match = re.search(r"\b([A-Za-z][A-Za-z\s'.-]+),\s*([A-Z]{2})\b", text)
    if match:
        return match.group(1).strip().title()
    return None


def _parse_city_state_zip_from_text(text: str | None) -> tuple[str | None, str | None, str | None]:
    if not text or str(text).strip().startswith("{"):
        return None, None, None
    body = str(text).strip()
    match = re.search(
        r"([A-Za-z][A-Za-z\s'.-]{1,40}),\s*([A-Z]{2})\s*,?\s*(\d{5})?",
        body,
    )
    if not match:
        return None, None, None
    city = match.group(1).strip().title()
    state_code = normalize_state(match.group(2))
    zip_code = match.group(3)[:5] if match.group(3) else None
    if city and re.search(r"\d", city) and " " in city:
        tokens = [t for t in re.split(r"\s+", city) if t]
        if tokens:
            city = tokens[-1]
    return city, state_code, zip_code


def extract_work_location(
    location: str | None,
    sam_raw: dict[str, Any] | None = None,
    *,
    title: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Where the work is performed — place of performance only, not contracting office."""
    city: str | None = None
    state_code: str | None = None
    zip_code: str | None = None

    if sam_raw:
        work_states = sam_raw.get("workStates")
        if isinstance(work_states, list) and work_states:
            for code in work_states:
                normalized = normalize_state(str(code))
                if normalized:
                    state_code = normalized
                    break

        for key in ("placeOfPerformance", "placeOfPerformanceLocation"):
            block = sam_raw.get(key)
            if isinstance(block, dict):
                pop_city, pop_state, pop_zip = _parse_sam_location_block(block)
                if pop_state:
                    state_code = pop_state
                if pop_city:
                    city = pop_city
                if pop_zip:
                    zip_code = pop_zip
                if pop_state or pop_city:
                    break

    if location and str(location).strip().startswith("{"):
        blob_text = str(location).strip()
        try:
            import ast
            import json

            try:
                block = json.loads(blob_text.replace("'", '"'))
            except (json.JSONDecodeError, TypeError):
                block = ast.literal_eval(blob_text)
            if isinstance(block, dict):
                pop_city, pop_state, pop_zip = _parse_sam_location_block(block)
                if pop_state:
                    state_code = pop_state
                if pop_city:
                    city = pop_city
                if pop_zip:
                    zip_code = pop_zip
        except (SyntaxError, ValueError, TypeError):
            pass

    if not state_code and location:
        parts = [part.strip() for part in str(location).split(",") if part.strip()]
        state_idx: int | None = None
        for idx in range(len(parts) - 1, -1, -1):
            part = parts[idx]
            if re.fullmatch(r"\d{5}(?:-\d{4})?", part):
                zip_code = part[:5]
                continue
            code = normalize_state(part)
            if code:
                state_code = code
                state_idx = idx
                break
        if state_code and state_idx is not None and state_idx > 0:
            candidate = parts[state_idx - 1]
            if candidate and not re.search(r"\d", candidate):
                city = candidate
            else:
                parsed_city, _, parsed_zip = _parse_city_state_zip_from_text(location)
                city = parsed_city or city
                zip_code = zip_code or parsed_zip

    if not state_code or not city:
        for blob in (location, description, title):
            parsed_city, parsed_state, parsed_zip = _parse_city_state_zip_from_text(
                blob if isinstance(blob, str) else None
            )
            if parsed_state and not state_code:
                state_code = parsed_state
            if parsed_city and not city:
                city = parsed_city
            if parsed_zip and not zip_code:
                zip_code = parsed_zip

    if not state_code and location:
        match = re.search(r"\b([A-Z]{2})\b", str(location).upper())
        if match:
            state_code = normalize_state(match.group(1))

    if not city and state_code:
        city = _infer_city_from_title(title, state_code) or _infer_city_from_title(description, state_code)

    label = format_location_scope(state_code, city)
    work_states = sam_raw.get("workStates") if isinstance(sam_raw, dict) else None
    if isinstance(work_states, list) and len(work_states) > 1:
        label = f"Multiple states ({', '.join(work_states)})"

    return {
        "state_code": state_code,
        "city": city,
        "zip": zip_code,
        "label": label,
        "work_states": work_states if isinstance(work_states, list) else [],
    }


def format_location_scope(state_code: str | None, city: str | None = None) -> str | None:
    if not state_code:
        return None
    state_name = STATE_CODE_TO_NAME.get(state_code, state_code)
    if city:
        return f"{city}, {state_name}"
    return state_name


def extract_state(location: str | None, sam_raw: dict[str, Any] | None = None) -> str | None:
    """Pull a two-letter state code from place of performance."""
    return extract_work_location(location, sam_raw).get("state_code")


def _parse_award_date(value: str | None) -> date | None:
    if not value:
        return None
    cleaned = str(value).strip()[:10]
    try:
        return date.fromisoformat(cleaned)
    except ValueError:
        return None


def _recency_weight(award_date: date, today: date | None = None) -> float:
    """Exponential decay — awards from ~1 year ago weigh 2x more than ~3 years ago."""
    today = today or date.today()
    days_ago = max(0, (today - award_date).days)
    return 0.5 ** (days_ago / 365.0)


def _weighted_percentile(pairs: list[tuple[float, float]], pct: float) -> float:
    """Percentile using recency weights. pairs = [(amount, weight), ...]"""
    if not pairs:
        raise ValueError("pairs required")
    if len(pairs) == 1:
        return pairs[0][0]
    ordered = sorted(pairs, key=lambda item: item[0])
    total_weight = sum(weight for _, weight in ordered)
    target = total_weight * (pct / 100.0)
    cumulative = 0.0
    for amount, weight in ordered:
        cumulative += weight
        if cumulative >= target:
            return amount
    return ordered[-1][0]


def _normalize_award(row: dict[str, Any], today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    amount = row.get("Award Amount")
    try:
        amount_value = float(amount) if amount is not None else None
    except (TypeError, ValueError):
        amount_value = None

    start_raw = row.get("Start Date")
    award_date = _parse_award_date(start_raw)
    days_ago = (today - award_date).days if award_date else None
    recency_weight = round(_recency_weight(award_date, today), 3) if award_date else None

    pop_state = normalize_state(str(row.get("Place of Performance State Code") or ""))
    pop_city = str(row.get("Place of Performance City Name") or "").strip() or None
    pop_zip = str(row.get("Place of Performance Zip5") or "").strip()[:5] or None
    location_parts = [p for p in (pop_city, pop_state, pop_zip) if p]
    performance_location = ", ".join(location_parts) if location_parts else None

    return {
        "award_id": row.get("Award ID"),
        "internal_id": row.get("internal_id"),
        "generated_internal_id": row.get("generated_internal_id"),
        "recipient_name": row.get("Recipient Name"),
        "award_amount": amount_value,
        "award_date": award_date.isoformat() if award_date else None,
        "start_date": start_raw,
        "end_date": row.get("End Date"),
        "days_ago": days_ago,
        "recency_weight": recency_weight,
        "awarding_agency": row.get("Awarding Agency"),
        "contract_award_type": row.get("Contract Award Type"),
        "naics_code": row.get("NAICS"),
        "description": str(row.get("Description") or "").strip() or None,
        "performance_state": pop_state,
        "performance_city": pop_city,
        "performance_zip": pop_zip,
        "performance_location": performance_location,
    }


DEFAULT_LOOKBACK_YEARS = 5
REGIONAL_LOOKBACK_YEARS = 3

PRIOR_CONTRACT_METHODS = frozenset({
    "contract_number",
    "contract_number_keyword",
    "manual_contract_number",
    "same_site_match",
    "facility_keyword",
    "agency_facility_match",
    "recipient_search",
    "incumbent_name_match",
})

_FACILITY_NAME_RE = re.compile(
    r"([\w][\w\s\-']{2,50}?\b(?:"
    r"Ranger District|National Park|Wildlife Refuge|Fish Hatchery|"
    r"Air Force Base|Army Base|Naval Station|Medical Center|"
    r"Veterans Affairs|Forest|Recreation Area|Historic Site|"
    r"Visitor Center|Headquarters|Annex|Complex"
    r"))\b",
    re.IGNORECASE,
)
_FACILITY_STOP_WORDS = frozenset({
    "janitorial", "cleaning", "services", "service", "maintenance", "landscaping",
    "landscape", "grounds", "custodial", "contract", "solicitation", "notice",
    "base year", "option", "pest", "control", "waste", "removal", "support",
    "facilities", "building", "annual", "recurring", "performance",
})

_INCUMBENT_TEXT_RE = re.compile(
    r"(?:incumbent|current|existing)\s+contractor\s*(?:is|:)?\s*([A-Z0-9][^\n;]{2,80}?)(?:\.|;|\n|$)",
    re.IGNORECASE,
)
_PREVIOUS_CONTRACT_TEXT_RE = re.compile(
    r"(?:previous|prior|predecessor|expiring|current)\s+contract(?:\s+number|\s+no\.?)?\s*(?:is|:)?\s*"
    r"([A-Z0-9][A-Z0-9\-/]{5,40})",
    re.IGNORECASE,
)

_CONTRACT_NUMBER_RE = re.compile(
    r"\b(?:FA|W|N|GS|VA|HQ|SPE|HSHQ|70Z|36C|"
    r"140[A-Z]{2}|ED|"
    r"[A-Z]{2,4})\s*[-]?\s*\d{2,4}\s*[-]?[A-Z]?\s*[-]?\s*[A-Z]?\s*[-]?\s*\d{3,6}\b",
    re.IGNORECASE,
)
_FWS_PIID_RE = re.compile(r"\b(140[A-Z]{2}\d{3}[A-Z]\d{4})\b", re.IGNORECASE)
_DOI_ED_PIID_RE = re.compile(r"\b(ED\d{10})\b", re.IGNORECASE)


def is_plausible_contract_number(value: str | None) -> bool:
    """Reject zip codes, time ranges, and other PDF false positives."""
    if not value:
        return False
    upper = re.sub(r"[^A-Z0-9]", "", str(value).strip().upper())
    if len(upper) < 8:
        return False
    if re.match(r"^[A-Z]{2}\d{5}$", upper):
        return False
    if re.match(r"^FROM\d+", upper):
        return False
    if re.match(r"^RATE\d+", upper):
        return False
    if re.match(r"^AREA\d+", upper):
        return False
    if re.match(r"^STE\d+", upper):
        return False
    if re.match(r"^OF\d+TO\d+", upper):
        return False
    if re.match(r"^IS\d{6}$", upper):
        return False
    if re.match(r"^CODE\d{6}$", upper):
        return False
    if re.match(r"^PAGE\d+$", upper):
        return False
    if re.match(r"^WD\d{4}\d+$", upper) and len(upper) <= 12:
        return False
    if not re.search(r"\d", upper):
        return False
    if upper.count("F") >= 6 and len(upper) > 20:
        return False
    if re.match(r"^ITEM\d+$", upper):
        return False
    if re.match(r"^JOB\d+$", upper):
        return False
    if re.match(r"^OF\d+$", upper):
        return False
    if re.match(r"^PERIOD", upper):
        return False
    if re.match(r"^FY\d{4}", upper):
        return False
    return True


def normalize_contract_number(value: str | None) -> str | None:
    """Normalize a federal PIID / contract number for USAspending lookup."""
    if not value:
        return None
    cleaned = re.sub(r"\s+", "", str(value).strip().upper())
    cleaned = re.sub(r"SECTION\d+$", "", cleaned, flags=re.IGNORECASE)
    if not cleaned or not is_plausible_contract_number(cleaned):
        return None
    return cleaned


def contract_number_search_variants(value: str | None) -> list[str]:
    """PIID variants to try against USAspending award_ids."""
    normalized = normalize_contract_number(value)
    if not normalized:
        return []
    variants: list[str] = []
    for candidate in (
        normalized,
        re.sub(r"[^A-Z0-9]", "", normalized),
        normalized.replace("-", ""),
    ):
        if candidate and candidate not in variants:
            variants.append(candidate)
    if normalized.startswith("ED") and len(normalized) > 10:
        tail = normalized[2:]
        if tail not in variants:
            variants.append(tail)
    return variants


def extract_contract_numbers(text: str | None) -> list[str]:
    """Pull plausible contract numbers from solicitation text."""
    if not text:
        return []
    found: list[str] = []
    seen: set[str] = set()
    body = str(text)

    for match in _PREVIOUS_CONTRACT_TEXT_RE.findall(body):
        normalized = normalize_contract_number(match)
        if normalized and normalized not in seen:
            seen.add(normalized)
            found.append(normalized)

    for pattern in (_FWS_PIID_RE, _DOI_ED_PIID_RE, _CONTRACT_NUMBER_RE):
        for match in pattern.findall(body):
            token = match if isinstance(match, str) else match[0]
            normalized = normalize_contract_number(token)
            if normalized and normalized not in seen:
                seen.add(normalized)
                found.append(normalized)
    return found


def extract_pricing_hints_from_text(text: str | None) -> dict[str, str | list[str]]:
    """Regex extraction for incumbent name and predecessor contract numbers in PDF text."""
    hints: dict[str, str | list[str]] = {
        "incumbent_contractor": None,
        "previous_contract_number": None,
        "extra_contract_numbers": [],
    }
    if not text:
        return hints

    body = str(text)
    incumbent_match = _INCUMBENT_TEXT_RE.search(body)
    if incumbent_match:
        name = re.sub(r"\s+", " ", incumbent_match.group(1)).strip(" .,;")
        if len(name) >= 3:
            hints["incumbent_contractor"] = name[:120]

    numbers = extract_contract_numbers(body)
    if numbers:
        hints["previous_contract_number"] = numbers[0]
        hints["extra_contract_numbers"] = numbers

    return hints


def extract_facility_search_terms(
    title: str | None,
    description: str | None = None,
    *,
    location: str | None = None,
    agency: str | None = None,
) -> list[str]:
    """Facility / site names from title, description, location, and agency for USAspending keyword search."""
    terms: list[str] = []
    seen: set[str] = set()
    for text in (title, description, location, agency):
        if not text:
            continue
        expanded = re.sub(
            r"\b(NWR|AFB|NPS|BRF)\b",
            lambda m: {
                "NWR": "National Wildlife Refuge",
                "AFB": "Air Force Base",
                "NPS": "National Park",
                "BRF": "National Wildlife Refuge",
            }.get(m.group(1).upper(), m.group(1)),
            str(text),
            flags=re.IGNORECASE,
        )
        for match in _FACILITY_NAME_RE.findall(expanded):
            cleaned = re.sub(r"\s+", " ", match).strip(" -–—,.")
            key = cleaned.lower()
            if len(cleaned) < 6 or key in seen:
                continue
            if any(stop in key for stop in _FACILITY_STOP_WORDS):
                continue
            seen.add(key)
            terms.append(cleaned)
        for segment in re.split(r"[-–—:]", expanded):
            segment = segment.strip()
            if len(segment) < 8 or len(segment) > 60:
                continue
            lower = segment.lower()
            if any(lower.startswith(w) for w in ("janitorial", "cleaning", "landscape", "maintenance", "pest")):
                continue
            if any(stop == lower for stop in _FACILITY_STOP_WORDS):
                continue
            if re.search(
                r"\b(district|park|refuge|base|center|station|forest|headquarters|nwr|afb)\b",
                lower,
            ):
                key = lower
                if key not in seen:
                    seen.add(key)
                    terms.append(segment)
    return terms[:4]


def _award_search_payload(
    *,
    filters: dict[str, Any],
    limit: int = 20,
    sort: str = "Start Date",
) -> dict[str, Any]:
    return {
        "filters": filters,
        "fields": AWARD_FIELDS,
        "sort": sort,
        "order": "desc",
        "page": 1,
        "limit": limit,
    }


def _post_award_search(payload: dict[str, Any], *, max_attempts: int = 4) -> list[dict[str, Any]]:
    """POST /api/v2/search/spending_by_award/ — retries on USAspending 502/503/429."""
    url = f"{BASE_URL}{SEARCH_PATH}"
    last_error: Exception | None = None
    for attempt in range(max_attempts):
        try:
            with httpx.Client(timeout=60.0) as client:
                response = client.post(url, json=payload)
                if response.status_code in (502, 503, 429) and attempt < max_attempts - 1:
                    time.sleep(min(8.0, 1.5 ** attempt))
                    continue
                response.raise_for_status()
                data = response.json()
            today = date.today()
            return [_normalize_award(row, today) for row in (data.get("results") or [])]
        except httpx.HTTPStatusError as exc:
            last_error = exc
            if exc.response.status_code in (502, 503, 429) and attempt < max_attempts - 1:
                time.sleep(min(8.0, 1.5 ** attempt))
                continue
            raise
        except httpx.RequestError as exc:
            last_error = exc
            if attempt < max_attempts - 1:
                time.sleep(min(8.0, 1.5 ** attempt))
                continue
            raise
    if last_error:
        raise last_error
    return []


def _contract_period_years(start_raw: str | None, end_raw: str | None) -> float | None:
    start = _parse_award_date(start_raw)
    end = _parse_award_date(end_raw)
    if not start or not end or end <= start:
        return None
    days = (end - start).days
    if days < 30:
        return None
    return max(1.0, round(days / 365.25, 2))


def estimate_annual_award_amount(award: dict[str, Any]) -> float | None:
    """Estimate annual payment from total award amount and contract period."""
    amount = award.get("award_amount")
    if not amount or amount <= 0:
        return None
    years = _contract_period_years(award.get("start_date"), award.get("end_date"))
    if years and years >= 1:
        return round(float(amount) / years, 2)
    return round(float(amount), 2)


def estimate_option_years(award: dict[str, Any]) -> int | None:
    """Rough option-year count from contract duration (base + options)."""
    years = _contract_period_years(award.get("start_date"), award.get("end_date"))
    if years is None:
        return None
    if years <= 1.25:
        return 0
    return max(0, int(round(years - 1)))


def _predecessor_summary(
    award: dict[str, Any],
    *,
    lookup_method: str,
    confidence: str = "high",
) -> dict[str, Any]:
    annual = estimate_annual_award_amount(award)
    options = estimate_option_years(award)
    return {
        "lookup_method": lookup_method,
        "confidence": confidence,
        "is_prior_contract": lookup_method in PRIOR_CONTRACT_METHODS,
        "contract_number": award.get("award_id"),
        "recipient_name": award.get("recipient_name"),
        "total_value": award.get("award_amount"),
        "annual_amount": annual,
        "option_years_exercised": options,
        "start_date": award.get("start_date") or award.get("award_date"),
        "end_date": award.get("end_date"),
        "awarding_agency": award.get("awarding_agency"),
        "performance_location": award.get("performance_location"),
        "performance_city": award.get("performance_city"),
        "performance_state": award.get("performance_state"),
        "description": award.get("description"),
    }


def agency_search_filters(agency: str | None) -> list[dict[str, str]] | None:
    """Map SAM.gov agency text to USAspending awarding-agency filters."""
    if not agency:
        return None
    upper = str(agency).upper()
    rules: tuple[tuple[str, ...], str, str] = (
        (("DEPT OF THE AIR FORCE", "DEPARTMENT OF THE AIR FORCE", "AIR FORCE"), "subtier", "Department of the Air Force"),
        (("DEPT OF THE ARMY", "DEPARTMENT OF THE ARMY", "CORPS OF ENGINEERS"), "subtier", "Department of the Army"),
        (("DEPT OF THE NAVY", "DEPARTMENT OF THE NAVY"), "subtier", "Department of the Navy"),
        (("FOREST SERVICE", "USDA FOREST"), "subtier", "Forest Service"),
        (("FISH AND WILDLIFE", "FISH & WILDLIFE"), "subtier", "Fish and Wildlife Service"),
        (("NATIONAL PARK SERVICE",), "subtier", "National Park Service"),
        (("BUREAU OF LAND MANAGEMENT",), "subtier", "Bureau of Land Management"),
        (("GENERAL SERVICES", "GSA"), "toptier", "General Services Administration"),
        (("VETERANS AFFAIRS",), "toptier", "Department of Veterans Affairs"),
        (("HOMELAND SECURITY",), "toptier", "Department of Homeland Security"),
        (("AGRICULTURE", "USDA"), "toptier", "Department of Agriculture"),
        (("INTERIOR",), "toptier", "Department of the Interior"),
        (("DEFENSE", "DEPT OF DEFENSE"), "toptier", "Department of Defense"),
    )
    for needles, tier, name in rules:
        if any(needle in upper for needle in needles):
            return [{"type": "awarding", "tier": tier, "name": name}]
    return None


def _agency_name_matches(agency: str | None, award_agency: str | None) -> bool:
    """True when a USAspending award's awarding agency matches the SAM.gov agency text."""
    if not agency:
        return True
    if not award_agency:
        return True
    mapped = agency_search_filters(agency)
    if mapped:
        expected = str(mapped[0].get("name") or "").upper()
        return expected in str(award_agency).upper()
    return str(agency).upper() in str(award_agency).upper()


def build_search_payload(
    naics_code: str,
    state_codes: str | list[str],
    *,
    city: str | None = None,
    limit: int = 20,
    lookback_years: int = REGIONAL_LOOKBACK_YEARS,
    agency: str | None = None,
) -> dict[str, Any]:
    end_date = date.today()
    start_date = end_date - timedelta(days=365 * lookback_years)
    if isinstance(state_codes, str):
        state_codes = [state_codes]
    if city and len(state_codes) == 1:
        locations: list[dict[str, str]] = [{"country": "USA", "state": state_codes[0], "city": city}]
    else:
        locations = [{"country": "USA", "state": code} for code in state_codes]
    filters: dict[str, Any] = {
        "naics_codes": {"require": [naics_code]},
        "place_of_performance_scope": "domestic",
        "place_of_performance_locations": locations,
        "award_type_codes": CONTRACT_AWARD_TYPE_CODES,
        "time_period": [
            {
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
            }
        ],
    }
    agency_filters = agency_search_filters(agency)
    if agency_filters:
        filters["agencies"] = agency_filters
    return _award_search_payload(filters=filters, limit=limit)


def fetch_awards_by_contract_number(contract_number: str, *, limit: int = 5) -> list[dict[str, Any]]:
    """Look up a specific predecessor contract by PIID / award ID."""
    variants = contract_number_search_variants(contract_number)
    if not variants:
        return []

    awards: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for variant_batch in (variants,):
        try:
            payload = _award_search_payload(
                filters={
                    "award_ids": variant_batch,
                    "award_type_codes": CONTRACT_AWARD_TYPE_CODES,
                },
                limit=limit,
            )
            for award in _post_award_search(payload):
                award_id = str(award.get("award_id") or "")
                if award_id and award_id not in seen_ids:
                    seen_ids.add(award_id)
                    awards.append(award)
        except Exception:
            pass

    if not awards:
        for variant in variants:
            for award in fetch_awards_by_keywords([variant], limit=limit):
                award_id = str(award.get("award_id") or "")
                needle = re.sub(r"[^A-Z0-9]", "", variant.upper())
                hay = re.sub(r"[^A-Z0-9]", "", award_id.upper())
                if needle and needle in hay and award_id not in seen_ids:
                    seen_ids.add(award_id)
                    awards.append(award)

    awards.sort(key=lambda a: a.get("award_date") or "", reverse=True)
    return awards


def fetch_filtered_awards(
    naics_code: str,
    state_code: str,
    *,
    city: str | None = None,
    agency: str | None = None,
    lookback_years: int = DEFAULT_LOOKBACK_YEARS,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """NAICS + location + optional agency search over a multi-year lookback."""
    payload = build_search_payload(
        naics_code,
        state_code,
        city=city,
        limit=limit,
        lookback_years=lookback_years,
        agency=agency,
    )
    awards = _post_award_search(payload)
    allowed = {state_code}
    awards = _filter_awards_by_states(awards, allowed)
    awards.sort(key=lambda a: a.get("award_date") or "", reverse=True)
    return awards



def fetch_award_detail(generated_internal_id: str | None) -> dict[str, Any] | None:
    """Full award record — total obligation, PoP, option values."""
    if not generated_internal_id:
        return None
    url = f"{BASE_URL}/api/v2/awards/{generated_internal_id}/"
    try:
        with httpx.Client(timeout=60.0) as client:
            response = client.get(url)
            response.raise_for_status()
            return response.json()
    except Exception:
        return None


def fetch_all_award_transactions(generated_internal_id: str | None, *, page_limit: int = 100) -> list[dict[str, Any]]:
    """Every modification on an award — source of truth for obligated dollars."""
    if not generated_internal_id:
        return []
    url = f"{BASE_URL}/api/v2/transactions/"
    transactions: list[dict[str, Any]] = []
    page = 1
    try:
        with httpx.Client(timeout=90.0) as client:
            while page <= page_limit:
                response = client.post(
                    url,
                    json={
                        "award_id": generated_internal_id,
                        "page": page,
                        "limit": 100,
                        "sort": "action_date",
                        "order": "asc",
                    },
                )
                response.raise_for_status()
                data = response.json()
                batch = data.get("results") or []
                transactions.extend(batch)
                meta = data.get("page_metadata") or {}
                if not meta.get("hasNext") or not batch:
                    break
                page += 1
    except Exception:
        return transactions
    return transactions


def summarize_award_transactions(transactions: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Derive bid-ready pricing from federal modification history:
    - total obligated (sum of all mods)
    - base year (first positive obligation, usually mod 0)
    - recent annual (most recent calendar year with net positive obligation)
    - option years exercised
    """
    if not transactions:
        return {}

    from collections import defaultdict

    yearly_net: dict[int, float] = defaultdict(float)
    base_year_amount: float | None = None
    mod_count = 0
    total = 0.0

    ordered = sorted(transactions, key=lambda row: str(row.get("action_date") or ""))
    for txn in ordered:
        try:
            obligation = float(txn.get("federal_action_obligation") or 0)
        except (TypeError, ValueError):
            obligation = 0.0
        total += obligation
        mod = str(txn.get("modification_number") or "").strip()
        if mod and mod not in ("0", "00"):
            mod_count += 1
        if base_year_amount is None and obligation > 0 and mod in ("0", "00", ""):
            base_year_amount = obligation
        action_date = _parse_award_date(txn.get("action_date"))
        if action_date:
            yearly_net[action_date.year] += obligation

    if base_year_amount is None:
        for txn in ordered:
            try:
                obligation = float(txn.get("federal_action_obligation") or 0)
            except (TypeError, ValueError):
                obligation = 0.0
            if obligation > 0:
                base_year_amount = obligation
                break

    positive_years = sorted(year for year, amount in yearly_net.items() if amount >= 10_000)
    recent_annual: float | None = None
    if positive_years:
        recent_annual = round(yearly_net[positive_years[-1]], 2)

    option_years_exercised = max(0, len(positive_years) - 1) if positive_years else None

    annual_amount = recent_annual or base_year_amount
    if annual_amount is None and positive_years:
        annual_amount = round(sum(yearly_net[y] for y in positive_years) / len(positive_years), 2)
    elif annual_amount is None and total > 0:
        annual_amount = round(total, 2)

    calc_note = "From USAspending modification history."
    if recent_annual and base_year_amount:
        calc_note = (
            f"Recent year obligation ${recent_annual:,.0f}; "
            f"base award ${base_year_amount:,.0f}; "
            f"{mod_count} modification(s)."
        )
    elif base_year_amount:
        calc_note = f"Base award obligation ${base_year_amount:,.0f}; {mod_count} modification(s)."

    return {
        "total_obligated": round(total, 2) if total else None,
        "base_year_amount": round(base_year_amount, 2) if base_year_amount else None,
        "recent_annual_amount": recent_annual,
        "annual_amount": round(annual_amount, 2) if annual_amount else None,
        "option_years_exercised": option_years_exercised,
        "modifications_count": mod_count,
        "obligation_years": positive_years,
        "yearly_obligations": {str(y): round(yearly_net[y], 2) for y in positive_years},
        "pricing_calc_note": calc_note,
        "award_amount_source": "transaction_history",
    }


def enrich_predecessor_with_award_detail(summary: dict[str, Any], award: dict[str, Any]) -> dict[str, Any]:
    """Enrich predecessor pricing with award detail + full transaction modification history."""
    summary = dict(summary)
    gid = award.get("generated_internal_id")
    detail = fetch_award_detail(gid)
    transactions = fetch_all_award_transactions(gid)
    txn_summary = summarize_award_transactions(transactions)

    if txn_summary.get("total_obligated"):
        summary["total_value"] = txn_summary["total_obligated"]
        summary["award_amount_source"] = txn_summary.get("award_amount_source", "transaction_history")
    elif detail:
        obligated = detail.get("total_obligation")
        try:
            obligated_value = float(obligated) if obligated is not None else None
        except (TypeError, ValueError):
            obligated_value = None
        if obligated_value and obligated_value > 0:
            summary["total_value"] = round(obligated_value, 2)
            summary["award_amount_source"] = "total_obligation"

    if txn_summary.get("annual_amount"):
        summary["annual_amount"] = txn_summary["annual_amount"]
    if txn_summary.get("base_year_amount"):
        summary["base_year_amount"] = txn_summary["base_year_amount"]
    if txn_summary.get("recent_annual_amount"):
        summary["recent_annual_amount"] = txn_summary["recent_annual_amount"]
    if txn_summary.get("option_years_exercised") is not None:
        summary["option_years_exercised"] = txn_summary["option_years_exercised"]
    if txn_summary.get("modifications_count") is not None:
        summary["modifications_count"] = txn_summary["modifications_count"]
    if txn_summary.get("pricing_calc_note"):
        summary["pricing_calc_note"] = txn_summary["pricing_calc_note"]
    if txn_summary.get("yearly_obligations"):
        summary["yearly_obligations"] = txn_summary["yearly_obligations"]

    if detail:
        pop_start = detail.get("period_of_performance_start_date") or detail.get("date_signed")
        pop_end = detail.get("period_of_performance_current_end_date") or detail.get("ordering_period_end_date")
        if pop_start:
            summary["start_date"] = pop_start
        if pop_end:
            summary["end_date"] = pop_end
        base_value = detail.get("base_exercised_options_val") or detail.get("base_and_all_options_value")
        if base_value:
            try:
                summary["base_and_options_value"] = float(base_value)
            except (TypeError, ValueError):
                pass

    if summary.get("annual_amount") is None and summary.get("total_value"):
        award_for_annual = dict(award)
        award_for_annual["award_amount"] = summary["total_value"]
        annual = estimate_annual_award_amount(award_for_annual)
        if annual:
            summary["annual_amount"] = annual
            summary.setdefault("pricing_calc_note", "Estimated annual from total ÷ contract period.")

    return summary


def fetch_awards_by_recipient(
    recipient_name: str,
    *,
    naics_code: str,
    state_code: str,
    city: str | None = None,
    agency: str | None = None,
    lookback_years: int = DEFAULT_LOOKBACK_YEARS,
    limit: int = 25,
) -> list[dict[str, Any]]:
    """Search awards by recipient name (UEI/DUNS/name) with NAICS + location filters."""
    end_date = date.today()
    start_date = end_date - timedelta(days=365 * lookback_years)
    locations: list[dict[str, str]] = [{"country": "USA", "state": state_code}]
    if city:
        locations = [{"country": "USA", "state": state_code, "city": city}]
    filters: dict[str, Any] = {
        "recipient_search_text": [recipient_name.strip()],
        "naics_codes": {"require": [naics_code]},
        "place_of_performance_scope": "domestic",
        "place_of_performance_locations": locations,
        "award_type_codes": CONTRACT_AWARD_TYPE_CODES,
        "time_period": [{"start_date": start_date.isoformat(), "end_date": end_date.isoformat()}],
    }
    agency_filters = agency_search_filters(agency)
    if agency_filters:
        filters["agencies"] = agency_filters
    awards = _post_award_search(_award_search_payload(filters=filters, limit=limit))
    awards = _filter_awards_by_states(awards, {state_code})
    awards.sort(key=lambda a: a.get("award_date") or "", reverse=True)
    return awards


def _finalize_predecessor(award: dict[str, Any], *, lookup_method: str, confidence: str) -> dict[str, Any]:
    summary = _predecessor_summary(award, lookup_method=lookup_method, confidence=confidence)
    return enrich_predecessor_with_award_detail(summary, award)


def _lookup_by_facility_keywords(
    facility_terms: list[str],
    *,
    naics_code: str,
    state_code: str,
    city: str | None = None,
    agency: str | None = None,
    origin_profile: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Match prior awards by facility name in title/description (ranger districts, bases, etc.)."""
    from location_matching import annotate_and_prioritize_location_awards
    from pricing_constants import MIN_REGIONAL_AWARD_AMOUNT

    if not facility_terms:
        return None

    best_award: dict[str, Any] | None = None
    best_score = -1

    for term in facility_terms[:3]:
        awards = fetch_awards_by_keywords(
            [term],
            naics_code=naics_code,
            state_code=state_code,
            limit=25,
        )
        if agency:
            awards = [a for a in awards if _agency_name_matches(agency, a.get("awarding_agency"))]
        if city:
            city_upper = city.upper()
            awards = [
                a
                for a in awards
                if city_upper in str(a.get("performance_city") or "").upper()
                or city_upper in str(a.get("performance_location") or "").upper()
                or not a.get("performance_city")
            ]

        if origin_profile:
            awards = annotate_and_prioritize_location_awards(awards, origin_profile)

        term_lower = term.lower()
        for award in awards:
            if not award.get("award_amount") or award["award_amount"] < MIN_REGIONAL_AWARD_AMOUNT:
                continue
            desc = " ".join(
                str(award.get(field) or "")
                for field in ("description", "performance_location", "award_id")
            ).lower()
            if term_lower not in desc and term_lower.split()[0] not in desc:
                continue
            score = 10
            if award.get("location_priority"):
                score += 30
            elif award.get("same_location"):
                score += 20
            if city and _city_matches(award.get("performance_city"), city):
                score += 15
            if score > best_score:
                best_score = score
                best_award = award

    if best_award:
        confidence = "high" if best_score >= 30 else "medium"
        return _finalize_predecessor(best_award, lookup_method="facility_keyword", confidence=confidence)
    return None


def _lookup_by_site(
    origin_profile: dict[str, Any],
    *,
    naics_code: str,
    state_code: str,
    city: str | None = None,
    agency: str | None = None,
) -> dict[str, Any] | None:
    """Prior award at the same street address + NAICS (best signal when PDF omits contract #)."""
    profiles = origin_profile.get("_all_profiles")
    if not isinstance(profiles, list):
        profiles = [origin_profile]
    for profile in profiles:
        if not profile.get("address_key"):
            continue
        found = _lookup_single_site_profile(
            profile,
            naics_code=naics_code,
            state_code=state_code,
            city=city,
            agency=agency,
        )
        if found:
            return found
    return None


def _lookup_single_site_profile(
    origin_profile: dict[str, Any],
    *,
    naics_code: str,
    state_code: str,
    city: str | None = None,
    agency: str | None = None,
) -> dict[str, Any] | None:
    """Prior award at the same street address + NAICS (best signal when PDF omits contract #)."""
    from location_matching import annotate_and_prioritize_location_awards
    from pricing_constants import MIN_REGIONAL_AWARD_AMOUNT

    if not origin_profile.get("address_key"):
        return None

    awards = fetch_filtered_awards(
        naics_code,
        state_code,
        city=city or origin_profile.get("city"),
        agency=agency,
        lookback_years=DEFAULT_LOOKBACK_YEARS,
        limit=75,
    )
    annotated = annotate_and_prioritize_location_awards(awards, origin_profile)
    for award in annotated:
        if not award.get("same_location"):
            continue
        if not award.get("award_amount") or award["award_amount"] < MIN_REGIONAL_AWARD_AMOUNT:
            continue
        confidence = "high" if award.get("location_priority") else "medium"
        return _finalize_predecessor(award, lookup_method="same_site_match", confidence=confidence)
    return None


def _lookup_by_recipient_search(
    incumbent_contractor: str,
    *,
    naics_code: str,
    state_code: str,
    city: str | None = None,
    agency: str | None = None,
) -> dict[str, Any] | None:
    from pricing_constants import MIN_REGIONAL_AWARD_AMOUNT

    for use_city in ([city] if city else [None]):
        awards = fetch_awards_by_recipient(
            incumbent_contractor,
            naics_code=naics_code,
            state_code=state_code,
            city=use_city,
            agency=agency,
            limit=25,
        )
        dated = [
            a
            for a in awards
            if a.get("award_date")
            and a.get("award_amount")
            and a["award_amount"] >= MIN_REGIONAL_AWARD_AMOUNT
            and _recipient_matches_incumbent(a.get("recipient_name"), incumbent_contractor)
        ]
        if dated:
            return _finalize_predecessor(dated[0], lookup_method="recipient_search", confidence="medium")
    return None


def _recipient_matches_incumbent(recipient: str | None, incumbent: str) -> bool:
    if not recipient or not incumbent:
        return False
    needle = incumbent.strip().upper()
    hay = recipient.strip().upper()
    if not needle or not hay:
        return False
    if needle in hay or hay in needle:
        return True
    needle_tokens = {t for t in re.split(r"[^A-Z0-9]+", needle) if len(t) > 2}
    hay_tokens = {t for t in re.split(r"[^A-Z0-9]+", hay) if len(t) > 2}
    if needle_tokens and needle_tokens.issubset(hay_tokens):
        return True
    return False


def _city_matches(award_city: str | None, target_city: str | None) -> bool:
    if not award_city or not target_city:
        return False
    return award_city.strip().upper() == target_city.strip().upper()


def fetch_awards_by_keywords(
    keywords: list[str],
    *,
    naics_code: str | None = None,
    state_code: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Keyword search — useful when award_ids lookup misses a PIID variant."""
    cleaned = [str(k).strip() for k in keywords if str(k).strip()]
    if not cleaned:
        return []
    filters: dict[str, Any] = {
        "keywords": cleaned,
        "award_type_codes": CONTRACT_AWARD_TYPE_CODES,
    }
    if naics_code:
        filters["naics_codes"] = {"require": [naics_code]}
    if state_code:
        filters["place_of_performance_scope"] = "domestic"
        filters["place_of_performance_locations"] = [{"country": "USA", "state": state_code}]
    payload = _award_search_payload(filters=filters, limit=limit)
    awards = _post_award_search(payload)
    awards.sort(key=lambda a: a.get("award_date") or "", reverse=True)
    return awards


def _prior_award_relevance_score(
    award: dict[str, Any],
    *,
    facility_terms: list[str] | None = None,
    title: str | None = None,
    naics_code: str | None = None,
) -> int:
    """Higher = better prior-contract candidate. Used to pick the right award at a site."""
    score = 0
    desc = " ".join(
        str(award.get(field) or "") for field in ("description", "performance_location", "award_id")
    ).lower()
    title_lower = (title or "").lower()

    if naics_code:
        naics_raw = award.get("naics_code")
        naics_val = naics_raw.get("code") if isinstance(naics_raw, dict) else str(naics_raw or "")
        if naics_val == naics_code:
            score += 15
        elif naics_code in str(naics_raw or ""):
            score += 10

    service_words = ("janitorial", "custodial", "cleaning", "housekeeping", "jantorial", "janitor")
    if naics_code == "561720" and any(word in desc for word in service_words):
        score += 25
    if any(word in title_lower for word in ("janitorial", "custodial", "cleaning", "jantorial")) and any(
        word in desc for word in service_words
    ):
        score += 20

    for term in facility_terms or []:
        term_lower = term.lower()
        if term_lower in desc:
            score += 20
        else:
            for piece in re.findall(r"[a-z]{5,}", term_lower):
                if piece in desc:
                    score += 12
                    break
            if term_lower.split()[0] in desc:
                score += 8

    for token in re.findall(r"[a-z]{5,}", title_lower):
        if token in desc and token not in service_words:
            score += 8

    noise = ("toilet", "vault", "pumping", "rodent", "pest", "abatement", "landscap", "septic")
    title_is_janitorial = any(w in title_lower for w in ("janitorial", "custodial", "cleaning", "jantorial"))
    if title_is_janitorial and any(word in desc for word in noise):
        score -= 40
    elif any(word in desc for word in noise) and not any(word in title_lower for word in noise):
        score -= 20

    return score


def _pick_best_prior_award(
    awards: list[dict[str, Any]],
    *,
    facility_terms: list[str] | None = None,
    title: str | None = None,
    naics_code: str | None = None,
    min_amount: float | None = None,
) -> dict[str, Any] | None:
    from pricing_constants import MIN_PRIOR_CONTRACT_AMOUNT

    floor = min_amount if min_amount is not None else MIN_PRIOR_CONTRACT_AMOUNT
    candidates = [
        a
        for a in awards
        if a.get("award_date")
        and a.get("award_amount")
        and a["award_amount"] >= floor
    ]
    if not candidates:
        return None
    candidates.sort(
        key=lambda a: (
            _prior_award_relevance_score(
                a,
                facility_terms=facility_terms,
                title=title,
                naics_code=naics_code,
            ),
            a.get("award_date") or "",
            a.get("award_amount") or 0,
        ),
        reverse=True,
    )
    best = candidates[0]
    relevance = _prior_award_relevance_score(
        best,
        facility_terms=facility_terms,
        title=title,
        naics_code=naics_code,
    )
    if relevance < 15:
        return None
    return best


def _lookup_by_agency_facility_site(
    *,
    naics_code: str,
    state_code: str,
    city: str | None = None,
    agency: str | None = None,
    facility_terms: list[str] | None = None,
    title: str | None = None,
) -> dict[str, Any] | None:
    """Same agency subtier + city + NAICS + facility name in award text."""
    if not facility_terms or not agency_search_filters(agency):
        return None

    awards = fetch_filtered_awards(naics_code, state_code, city=city, agency=agency, limit=40)
    if not awards:
        awards = fetch_filtered_awards(naics_code, state_code, city=city, agency=None, limit=40)

    filtered = [a for a in awards if _agency_name_matches(agency, a.get("awarding_agency"))]
    best_award = _pick_best_prior_award(
        filtered,
        facility_terms=facility_terms,
        title=title,
        naics_code=naics_code,
    )
    if best_award:
        return _finalize_predecessor(
            best_award,
            lookup_method="agency_facility_match",
            confidence="high",
        )
    return None


def _lookup_most_recent_city_award(
    *,
    naics_code: str,
    state_code: str,
    city: str | None = None,
    agency: str | None = None,
    facility_terms: list[str] | None = None,
    title: str | None = None,
) -> dict[str, Any] | None:
    """Best prior award at the work city — relevance-scored, not just highest dollars."""
    awards = fetch_filtered_awards(naics_code, state_code, city=city, agency=agency, limit=25)
    if not awards:
        awards = fetch_filtered_awards(naics_code, state_code, city=city, agency=None, limit=25)
    if not awards and city:
        awards = fetch_filtered_awards(naics_code, state_code, city=None, agency=None, limit=25)

    if city:
        city_matches = [a for a in awards if _city_matches(a.get("performance_city"), city)]
        if city_matches:
            awards = city_matches

    best_award = _pick_best_prior_award(
        awards,
        facility_terms=facility_terms,
        title=title,
        naics_code=naics_code,
    )
    if best_award:
        return _finalize_predecessor(
            best_award,
            lookup_method="agency_facility_match",
            confidence="high",
        )
    return None


def _lookup_by_contract_number(
    contract_number: str,
    *,
    naics_code: str | None = None,
    state_code: str | None = None,
) -> dict[str, Any] | None:
    from pricing_constants import MIN_PRIOR_CONTRACT_AMOUNT

    for award in fetch_awards_by_contract_number(contract_number):
        if award.get("award_amount") and award["award_amount"] >= MIN_PRIOR_CONTRACT_AMOUNT:
            return _finalize_predecessor(award, lookup_method="contract_number", confidence="high")

    for award in fetch_awards_by_keywords(
        [contract_number, normalize_contract_number(contract_number) or contract_number],
        naics_code=naics_code,
        state_code=state_code,
        limit=10,
    ):
        award_id = str(award.get("award_id") or "").upper()
        needle = normalize_contract_number(contract_number) or contract_number.upper()
        if needle in re.sub(r"[^A-Z0-9]", "", award_id) or contract_number.upper() in award_id:
            if award.get("award_amount") and award["award_amount"] >= MIN_PRIOR_CONTRACT_AMOUNT:
                return _finalize_predecessor(award, lookup_method="contract_number_keyword", confidence="high")
    return None


def _lookup_by_incumbent(
    incumbent_contractor: str,
    *,
    naics_code: str,
    state_code: str,
    city: str | None = None,
    agency: str | None = None,
) -> dict[str, Any] | None:
    from pricing_constants import MIN_REGIONAL_AWARD_AMOUNT

    city_awards = fetch_filtered_awards(
        naics_code,
        state_code,
        city=city,
        agency=agency,
        lookback_years=DEFAULT_LOOKBACK_YEARS,
        limit=50,
    ) if city else []

    state_awards = fetch_filtered_awards(
        naics_code,
        state_code,
        agency=agency,
        lookback_years=DEFAULT_LOOKBACK_YEARS,
        limit=50,
    )

    for pool, method, confidence in (
        (city_awards, "same_city_match", "medium"),
        (state_awards, "incumbent_name_match", "medium"),
    ):
        dated = [
            a
            for a in pool
            if a.get("award_date")
            and a.get("award_amount")
            and a["award_amount"] >= MIN_REGIONAL_AWARD_AMOUNT
            and _recipient_matches_incumbent(a.get("recipient_name"), incumbent_contractor)
        ]
        if method == "same_city_match":
            dated = [a for a in dated if _city_matches(a.get("performance_city"), city)]
        dated.sort(key=lambda a: a.get("award_date") or "", reverse=True)
        if dated:
            return _finalize_predecessor(dated[0], lookup_method=method, confidence=confidence)
    return None


def fetch_predecessor_pricing(
    *,
    previous_contract_number: str | None = None,
    incumbent_contractor: str | None = None,
    naics_code: str | None = None,
    state_code: str | None = None,
    city: str | None = None,
    agency: str | None = None,
    extra_contract_numbers: list[str] | None = None,
    origin_profile: dict[str, Any] | None = None,
    facility_terms: list[str] | None = None,
    manual_lookup: bool = False,
    title: str | None = None,
) -> dict[str, Any] | None:
    """
    Resolve the prior contract at this site. Lookup order (best evidence first):
    1) Contract number (PDF, attachment text, or manual entry)
    2) Same street address + NAICS
    3) Facility name keywords (ranger district, base, park, etc.)
    4) Incumbent via USAspending recipient search
    5) Incumbent matched in regional awards
    Each match is enriched with full modification/transaction history for real obligated dollars.
    """
    contract_numbers: list[str] = []
    exclude: set[str] = set()
    for value in origin_profile.get("_exclude_contract_numbers") or ():
        for variant in contract_number_search_variants(value):
            exclude.add(variant)
    for value in [previous_contract_number, *(extra_contract_numbers or [])]:
        for variant in contract_number_search_variants(value):
            if variant and variant not in exclude and variant not in contract_numbers:
                contract_numbers.append(variant)

    lookup_method_override = "manual_contract_number" if manual_lookup else None

    for contract_number in contract_numbers:
        found = _lookup_by_contract_number(
            contract_number,
            naics_code=naics_code,
            state_code=state_code,
        )
        if found:
            if lookup_method_override:
                found["lookup_method"] = lookup_method_override
            return found

    if naics_code and state_code and origin_profile:
        profile = dict(origin_profile)
        profiles = origin_profile.get("_all_profiles")
        if isinstance(profiles, list):
            profile["_all_profiles"] = profiles
        found = _lookup_by_site(
            profile,
            naics_code=naics_code,
            state_code=state_code,
            city=city,
            agency=agency,
        )
        if found:
            return found

    if naics_code and state_code and facility_terms:
        found = _lookup_by_agency_facility_site(
            naics_code=naics_code,
            state_code=state_code,
            city=city,
            agency=agency,
            facility_terms=facility_terms,
            title=title,
        )
        if found:
            return found

    if naics_code and state_code and facility_terms:
        found = _lookup_by_facility_keywords(
            facility_terms,
            naics_code=naics_code,
            state_code=state_code,
            city=city,
            agency=agency,
            origin_profile=origin_profile,
        )
        if found:
            return found

    if incumbent_contractor and naics_code and state_code:
        found = _lookup_by_recipient_search(
            incumbent_contractor,
            naics_code=naics_code,
            state_code=state_code,
            city=city,
            agency=agency,
        )
        if found:
            return found
        return _lookup_by_incumbent(
            incumbent_contractor,
            naics_code=naics_code,
            state_code=state_code,
            city=city,
            agency=agency,
        )

    if naics_code and state_code:
        return _lookup_most_recent_city_award(
            naics_code=naics_code,
            state_code=state_code,
            city=city,
            agency=agency,
            facility_terms=facility_terms,
            title=title,
        )

    return None


def _count_dated_awards(awards: list[dict[str, Any]]) -> int:
    return sum(
        1
        for award in awards
        if award.get("award_date") and award.get("award_amount") and award["award_amount"] > 0
    )


def _filter_awards_by_states(
    awards: list[dict[str, Any]],
    allowed_states: set[str],
) -> list[dict[str, Any]]:
    """Drop any award whose place of performance is outside the allowed states."""
    matched: list[dict[str, Any]] = []
    for award in awards:
        pop_state = award.get("performance_state")
        if pop_state and pop_state not in allowed_states:
            continue
        matched.append(award)
    return matched


def _filter_awards_by_state(awards: list[dict[str, Any]], state_code: str) -> list[dict[str, Any]]:
    return _filter_awards_by_states(awards, {state_code})


def _query_awards(
    naics_code: str,
    state_code: str,
    *,
    city: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    return _query_awards_in_states(naics_code, [state_code], city=city, limit=limit)


def _query_awards_in_states(
    naics_code: str,
    state_codes: list[str],
    *,
    city: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    payload = build_search_payload(naics_code, state_codes, city=city, limit=limit)
    url = f"{BASE_URL}{SEARCH_PATH}"
    with httpx.Client(timeout=60.0) as client:
        response = client.post(url, json=payload)
        response.raise_for_status()
        data = response.json()

    today = date.today()
    allowed = set(state_codes)
    awards = [_normalize_award(row, today) for row in (data.get("results") or [])[:limit]]
    awards = _filter_awards_by_states(awards, allowed)
    awards.sort(key=lambda a: a.get("award_date") or "", reverse=True)
    return awards


def summarize_awards(
    awards: list[dict[str, Any]],
    *,
    naics_code: str,
    state_code: str,
    location_scope: str | None = None,
    location_scope_type: str | None = None,
    location_scope_note: str | None = None,
    surrounding_states: list[str] | None = None,
    scope_profile: dict[str, Any] | None = None,
    unit_rate_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    today = date.today()
    dated_awards = [
        a
        for a in awards
        if a.get("award_date") and a.get("award_amount") and a["award_amount"] > 0
    ]
    dated_awards.sort(key=lambda a: a["award_date"], reverse=True)

    amounts = [a["award_amount"] for a in dated_awards]
    weighted_pairs = [
        (a["award_amount"], a["recency_weight"])
        for a in dated_awards
        if a.get("recency_weight")
    ]

    recipient_weights: Counter[str] = Counter()
    for award in dated_awards:
        name = str(award.get("recipient_name") or "").strip()
        if name and award.get("recency_weight"):
            recipient_weights[name] += award["recency_weight"]

    top_winner, top_winner_score = recipient_weights.most_common(1)[0] if recipient_weights else (None, 0)
    incumbent = dated_awards[0]["recipient_name"] if dated_awards else None

    awards_last_12_months = sum(
        1 for a in dated_awards if a.get("days_ago") is not None and a["days_ago"] <= 365
    )

    summary: dict[str, Any] = {
        "naics_code": naics_code,
        "state_code": state_code,
        "location_scope": location_scope or format_location_scope(state_code),
        "location_scope_type": location_scope_type or "state",
        "location_scope_note": location_scope_note,
        "surrounding_states": surrounding_states or [],
        "lookback_years": DEFAULT_LOOKBACK_YEARS,
        "awards_count": len(awards),
        "awards_with_dates": len(dated_awards),
        "awards_missing_dates": len(awards) - len([a for a in awards if a.get("award_date")]),
        "awards_last_12_months": awards_last_12_months,
        "awards_with_amounts": len(amounts),
        "unique_bidders": len(recipient_weights),
        "average_amount": None,
        "weighted_average_amount": None,
        "highest_amount": None,
        "lowest_amount": None,
        "most_frequent_winner": top_winner,
        "most_frequent_winner_count": round(top_winner_score, 1) if top_winner_score else 0,
        "likely_incumbent": incumbent,
        "recommended_bid_low": None,
        "recommended_bid_high": None,
        "recommended_bid_note": None,
        "newest_award_date": dated_awards[0]["award_date"] if dated_awards else None,
        "oldest_award_date": dated_awards[-1]["award_date"] if dated_awards else None,
        "awards": awards,
    }

    if not amounts:
        summary["recommended_bid_note"] = (
            "No dated award amounts returned for comparable contracts."
        )
        return summary

    total_weight = sum(weight for _, weight in weighted_pairs)
    weighted_avg = sum(amount * weight for amount, weight in weighted_pairs) / total_weight
    summary["average_amount"] = round(statistics.mean(amounts), 2)
    summary["weighted_average_amount"] = round(weighted_avg, 2)
    summary["highest_amount"] = round(max(amounts), 2)
    summary["lowest_amount"] = round(min(amounts), 2)

    if unit_rate_summary:
        summary["unit_rate_summary"] = unit_rate_summary
        if unit_rate_summary.get("recommended_annual_bid") is not None:
            summary["recommended_annual_bid"] = unit_rate_summary["recommended_annual_bid"]
            summary["recommended_bid_formula"] = unit_rate_summary.get("recommended_bid_formula")
            summary["recommended_bid_low"] = unit_rate_summary.get("recommended_bid_low")
            summary["recommended_bid_high"] = unit_rate_summary.get("recommended_bid_high")
            summary["recommended_bid_note"] = unit_rate_summary.get("recommended_bid_note")
        elif unit_rate_summary.get("recommended_bid_note"):
            summary["recommended_bid_note"] = unit_rate_summary["recommended_bid_note"]
    else:
        summary["recommended_bid_note"] = (
            "Comparable award totals shown in the table — recommended annual bid requires "
            "unit rates ($/sq ft per visit) from your contract scope."
        )

    return summary


def fetch_pricing_intelligence(
    naics_code: str | None,
    state_code: str | None,
    *,
    city: str | None = None,
    zip_code: str | None = None,
    origin_location: dict[str, Any] | None = None,
    scope_profile: dict[str, Any] | None = None,
    limit: int = COMPARABLE_DISPLAY_LIMIT,
) -> dict[str, Any]:
    """Query USAspending.gov for comparable contracts; normalize to $/sq ft per visit."""
    from comparable_scope import (
        filter_clearance_compatible_awards,
        pricing_allow_neighbor_states,
        pricing_max_distance_miles,
        summarize_unit_rates,
    )
    from geo import filter_local_awards

    if not naics_code:
        raise ValueError("NAICS code is required for pricing lookup.")
    if not state_code:
        raise ValueError("Could not determine the contract state for pricing lookup.")

    fetch_limit = SEARCH_FETCH_LIMIT
    max_miles = pricing_max_distance_miles()
    allow_neighbors = pricing_allow_neighbor_states()

    origin = origin_location or {
        "state_code": state_code,
        "city": city,
        "zip": zip_code,
        "label": format_location_scope(state_code, city),
    }
    state_name = STATE_CODE_TO_NAME.get(state_code, state_code)
    neighbors = BORDERING_STATES.get(state_code, [])
    awards: list[dict[str, Any]]
    location_scope: str
    location_scope_type = "state"
    location_scope_note: str | None = None
    surrounding_states: list[str] = []

    if city:
        local_awards = _query_awards(naics_code, state_code, city=city, limit=fetch_limit)
        local_dated = _count_dated_awards(local_awards)
        if local_dated >= MIN_LOCAL_COMPARABLE_AWARDS:
            awards = local_awards
            location_scope = format_location_scope(state_code, city) or f"{city}, {state_name}"
            location_scope_type = "city"
        else:
            state_awards = _query_awards(naics_code, state_code, limit=fetch_limit)
            state_dated = _count_dated_awards(state_awards)
            if state_dated >= MIN_LOCAL_COMPARABLE_AWARDS or not neighbors:
                awards = state_awards
                location_scope = state_name
                location_scope_note = (
                    f"Only {local_dated} recent comparable award(s) in {city}; "
                    f"expanded to all contracts performed in {state_name}."
                    if local_dated > 0
                    else f"No recent comparable awards in {city}; showing contracts performed statewide in {state_name}."
                )
            else:
                if allow_neighbors:
                    awards, location_scope, location_scope_note, surrounding_states = _regional_fallback(
                        naics_code,
                        state_code,
                        state_name,
                        neighbors,
                        limit=fetch_limit,
                        prior_note=(
                            f"Few comparables in {city} and {state_name}; "
                            f"expanded to neighboring states within {max_miles} mi."
                        ),
                    )
                    location_scope_type = "region"
                else:
                    awards = state_awards
                    location_scope = state_name
                    location_scope_note = (
                        f"Only {local_dated} local award(s) in {city}; "
                        f"showing same-state contracts within {max_miles} mi (neighboring states disabled)."
                        if local_dated > 0
                        else f"No local awards in {city}; showing same-state contracts within {max_miles} mi."
                    )
    else:
        state_awards = _query_awards(naics_code, state_code, limit=fetch_limit)
        state_dated = _count_dated_awards(state_awards)
        if state_dated >= MIN_LOCAL_COMPARABLE_AWARDS or not neighbors:
            awards = state_awards
            location_scope = state_name
        elif allow_neighbors:
            awards, location_scope, location_scope_note, surrounding_states = _regional_fallback(
                naics_code,
                state_code,
                state_name,
                neighbors,
                limit=fetch_limit,
                prior_note=(
                    f"Only {state_dated} recent comparable award(s) in {state_name}; "
                    f"expanded to neighboring states within {max_miles} mi."
                ),
            )
            location_scope_type = "region"
        else:
            awards = state_awards
            location_scope = state_name
            location_scope_note = (
                f"Only {state_dated} award(s) in {state_name}; "
                f"staying in-state within {max_miles} mi (neighboring states disabled)."
            )

    awards = annotate_award_distances(awards, origin, state_names=STATE_CODE_TO_NAME)

    awards, geo_meta = filter_local_awards(
        awards,
        origin_state=state_code,
        max_miles=max_miles,
        require_same_state=not allow_neighbors,
    )
    if geo_meta.get("dropped_out_of_range") or geo_meta.get("dropped_other_state"):
        geo_note = (
            f"Geography filter: kept {geo_meta['local_count']} award(s) within {max_miles} mi"
            f"{' and in ' + state_name if not allow_neighbors else ''}."
        )
        location_scope_note = f"{location_scope_note} {geo_note}".strip() if location_scope_note else geo_note

    profile = scope_profile or {}
    awards, scope_meta = filter_clearance_compatible_awards(awards, profile)
    from location_matching import annotate_and_prioritize_location_awards

    award_profile_base = {
        **(origin_location or {}),
        **(scope_profile or {}),
        "naics_code": naics_code,
    }
    awards = annotate_and_prioritize_location_awards(awards, award_profile_base)
    awards = awards[:limit]
    if scope_meta.get("scope_note"):
        location_scope_note = (
            f"{location_scope_note} {scope_meta['scope_note']}"
            if location_scope_note
            else scope_meta["scope_note"]
        )

    unit_rate_summary = summarize_unit_rates(awards, profile)

    summary = summarize_awards(
        awards,
        naics_code=naics_code,
        state_code=state_code,
        location_scope=location_scope,
        location_scope_type=location_scope_type,
        location_scope_note=location_scope_note,
        surrounding_states=surrounding_states,
        scope_profile=profile,
        unit_rate_summary=unit_rate_summary,
    )
    summary["origin_location"] = origin
    if awards:
        closest = awards[0]
        summary["closest_award_miles"] = closest.get("distance_miles")
        summary["closest_award_label"] = closest.get("distance_label")
        summary["closest_award_location"] = closest.get("performance_location")
    summary["source"] = "USAspending.gov"
    summary["fetched_at"] = date.today().isoformat()
    summary["scope_profile"] = profile
    summary["scope_matching"] = scope_meta
    summary["unit_rate_summary"] = unit_rate_summary
    summary["geo_filter"] = geo_meta
    return summary


def _regional_fallback(
    naics_code: str,
    state_code: str,
    state_name: str,
    neighbors: list[str],
    *,
    limit: int,
    prior_note: str,
) -> tuple[list[dict[str, Any]], str, str, list[str]]:
    """Widest allowed search: home state plus bordering states only."""
    regional_states = [state_code, *neighbors]
    awards = _query_awards_in_states(naics_code, regional_states, limit=limit)
    location_scope = f"{state_name} and neighboring states"
    location_scope_note = (
        f"{prior_note} Includes {_state_names(regional_states)}. "
        "National or distant-state awards are never included."
    )
    return awards, location_scope, location_scope_note, neighbors


def fetch_regional_benchmarks(
    naics_code: str,
    state_code: str,
    *,
    origin_profile: dict[str, Any] | None = None,
    origin_location: dict[str, Any] | None = None,
    limit: int = 50,
    agency: str | None = None,
    city: str | None = None,
) -> dict[str, Any]:
    """
    Tier 1 — state-level USAspending contract awards for regional annual benchmarks.
    Filters out awards under $10,000.
    """
    from pricing_constants import MIN_REGIONAL_AWARD_AMOUNT, regional_confidence

    state_name = STATE_CODE_TO_NAME.get(state_code, state_code)
    raw_awards = fetch_filtered_awards(
        naics_code,
        state_code,
        city=city,
        agency=agency,
        lookback_years=DEFAULT_LOOKBACK_YEARS,
        limit=limit,
    )
    if not raw_awards:
        raw_awards = fetch_filtered_awards(
            naics_code,
            state_code,
            city=city,
            agency=None,
            lookback_years=DEFAULT_LOOKBACK_YEARS,
            limit=limit,
        )

    dated_awards = [
        a
        for a in raw_awards
        if a.get("award_date")
        and a.get("award_amount")
        and a["award_amount"] >= MIN_REGIONAL_AWARD_AMOUNT
    ]
    dated_awards.sort(key=lambda a: a["award_date"], reverse=True)

    from location_matching import annotate_and_prioritize_location_awards

    profile = origin_profile or origin_location or {"state_code": state_code, "naics_code": naics_code}
    if origin_profile is None and origin_location is not None:
        profile = {**origin_location, "naics_code": naics_code}
    dated_awards = annotate_and_prioritize_location_awards(dated_awards, profile)
    same_site_expired = sum(1 for a in dated_awards if a.get("location_priority"))

    amounts = [a["award_amount"] for a in dated_awards]
    recipient_weights: Counter[str] = Counter()
    for award in dated_awards:
        name = str(award.get("recipient_name") or "").strip()
        if name:
            recipient_weights[name] += award.get("recency_weight") or 1.0

    top_winner, top_winner_score = recipient_weights.most_common(1)[0] if recipient_weights else (None, 0)
    incumbent = dated_awards[0]["recipient_name"] if dated_awards else None
    conf_key, conf_label = regional_confidence(len(dated_awards))

    summary: dict[str, Any] = {
        "tier": "regional_benchmark",
        "naics_code": naics_code,
        "state_code": state_code,
        "state_name": state_name,
        "lookback_years": DEFAULT_LOOKBACK_YEARS,
        "min_award_amount": MIN_REGIONAL_AWARD_AMOUNT,
        "awards_count": len(dated_awards),
        "awards_with_dates": len(dated_awards),
        "average_annual_award": round(statistics.mean(amounts), 2) if amounts else None,
        "highest_award": round(max(amounts), 2) if amounts else None,
        "lowest_award": round(min(amounts), 2) if amounts else None,
        "most_frequent_winner": top_winner,
        "most_frequent_winner_count": round(top_winner_score, 1) if top_winner_score else 0,
        "likely_incumbent": incumbent,
        "confidence": conf_key,
        "confidence_label": conf_label,
        "benchmark_note": (
            f"Based on {len(dated_awards)} similar contracts awarded in {state_name} "
            f"over the last {DEFAULT_LOOKBACK_YEARS} years. Award amounts vary by building size and cleaning frequency."
            + (
                f" {same_site_expired} prior award(s) at this same address & scope (expired) are listed first."
                if same_site_expired
                else ""
            )
            if dated_awards
            else f"No contracts over ${MIN_REGIONAL_AWARD_AMOUNT:,} found in {state_name} for this NAICS."
        ),
        "same_location_expired_count": same_site_expired,
        "awards": dated_awards[:20],
        "source": "USAspending.gov",
        "fetched_at": date.today().isoformat(),
    }
    return summary
