"""Multi-signal fingerprint matching for GovSpend gs_watchlist vs SAM.gov postings."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("govtracker.watchlist.fingerprint")

SIGNAL_WEIGHTS: dict[str, int] = {
    "contracting_office": 2,
    "location": 2,
    "naics_code": 1,
    "incumbent_name": 3,
    "estimated_annual_value": 2,
    "title_keywords": 1,
}
MAX_FINGERPRINT_SCORE = sum(SIGNAL_WEIGHTS.values())

VALUE_MATCH_TOLERANCE = 0.30
TITLE_KEYWORD_MIN = 2
TITLE_KEYWORD_COUNT = (3, 5)

_STOP_WORDS = frozenset(
    """
    a an and are as at be by for from has have in into is it its of on or per the to with
    services service contract support maintenance cleaning janitorial facility facilities
    federal government department agency office base
    """.split()
)


@dataclass(frozen=True)
class MatchSignal:
    signal: str
    matched: bool
    points: int
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "signal": self.signal,
            "matched": self.matched,
            "points": self.points if self.matched else 0,
        }
        if self.detail:
            payload["detail"] = self.detail
        return payload


@dataclass
class FingerprintMatchResult:
    watchlist_id: int
    score: int
    confidence: str
    signals: list[MatchSignal] = field(default_factory=list)
    title_keywords: list[str] = field(default_factory=list)

    @property
    def matched_signals(self) -> list[str]:
        return [s.signal for s in self.signals if s.matched]

    def to_match_signals_json(self) -> list[dict[str, Any]]:
        return [s.to_dict() for s in self.signals]


def confidence_from_score(score: int) -> str:
    if score >= 8:
        return "High"
    if score >= 5:
        return "Possible"
    if score >= 3:
        return "Weak"
    return "None"


def should_surface_match(confidence: str, *, confirmed: bool = False) -> bool:
    if confidence == "High" or confirmed:
        return True
    if confidence == "Possible":
        return True
    return False


def should_trigger_pipeline(confidence: str, *, confirmed: bool = False) -> bool:
    return confidence == "High" or (confidence == "Possible" and confirmed)


def should_notify_govspend(confidence: str, *, confirmed: bool = False) -> bool:
    return should_trigger_pipeline(confidence, confirmed=confirmed)


def _norm(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def extract_title_keywords(title: str | None, *, min_count: int = 3, max_count: int = 5) -> list[str]:
    text = _norm(title)
    if not text:
        return []
    tokens: list[str] = []
    for token in re.split(r"[^\w]+", text):
        token = token.strip()
        if len(token) < 4 or token in _STOP_WORDS or token.isdigit():
            continue
        if token not in tokens:
            tokens.append(token)
    tokens.sort(key=len, reverse=True)
    if len(tokens) < min_count:
        return tokens
    return tokens[:max_count]


def parse_dollar_amount(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        amount = float(value)
        return amount if amount > 0 else None
    text = str(value).strip()
    if not text:
        return None
    text = text.replace(",", "").replace("$", "").strip()
    mult = 1.0
    if text[-1:].lower() == "k":
        mult = 1_000.0
        text = text[:-1]
    elif text[-1:].lower() == "m":
        mult = 1_000_000.0
        text = text[:-1]
    match = re.search(r"(\d+(?:\.\d+)?)", text)
    if not match:
        return None
    try:
        amount = float(match.group(1)) * mult
    except ValueError:
        return None
    return amount if amount > 0 else None


def values_within_tolerance(
    watchlist_value: float | None,
    posting_value: float | None,
    *,
    tolerance: float = VALUE_MATCH_TOLERANCE,
) -> bool:
    if watchlist_value is None or posting_value is None or watchlist_value <= 0:
        return False
    delta = abs(posting_value - watchlist_value) / watchlist_value
    return delta <= tolerance


def contracting_office_matches(posting_office: str | None, target_office: str | None) -> bool:
    a = _norm(posting_office)
    b = _norm(target_office)
    if not a or not b or len(b) < 4:
        return False
    return b in a or a in b


def naics_matches(posting_naics: str | None, target_naics: str | None) -> bool:
    a = (posting_naics or "").strip()
    b = (target_naics or "").strip()
    if not a or not b:
        return False
    return a == b or a.startswith(b) or b.startswith(a)


def _parse_location_parts(location: str | None) -> tuple[str | None, str | None, str | None]:
    loc = (location or "").strip()
    if not loc:
        return None, None, None
    zip_match = re.search(r"\b(\d{5})(?:-\d{4})?\b", loc)
    zip_code = zip_match.group(1) if zip_match else None
    state_match = re.search(r",\s*([A-Za-z]{2})\b", loc)
    state = state_match.group(1).upper() if state_match else None
    city = None
    if state_match:
        before_state = loc[: state_match.start()].strip()
        if "," in before_state:
            city = before_state.split(",")[-1].strip()
        else:
            city = before_state
    elif "," in loc:
        parts = [p.strip() for p in loc.split(",") if p.strip()]
        if parts:
            city = parts[0]
        if len(parts) >= 2 and len(parts[-1]) == 2:
            state = parts[-1].upper()
    return city or None, state, zip_code


def location_matches(
    posting_location: str | None,
    *,
    posting_zip: str | None,
    target_city: str | None,
    target_state: str | None,
    target_zip: str | None,
) -> bool:
    if not target_state:
        return False
    loc = _norm(posting_location)
    state = target_state.strip().upper()[:2]
    if not re.search(rf"\b{re.escape(state.lower())}\b", loc) and f", {state.lower()}" not in loc:
        if not loc.endswith(state.lower()):
            return False
    if target_city:
        city = _norm(target_city)
        if len(city) >= 3 and city not in loc:
            parsed_city, _, _ = _parse_location_parts(posting_location)
            if not parsed_city or city not in _norm(parsed_city):
                return False
    if target_zip:
        target_zip5 = str(target_zip).strip()[:5]
        if len(target_zip5) == 5:
            posting_zip5 = (posting_zip or "").strip()[:5]
            if not posting_zip5:
                _, _, parsed_zip = _parse_location_parts(posting_location)
                posting_zip5 = (parsed_zip or "")[:5]
            if posting_zip5 and posting_zip5 != target_zip5:
                return False
            if not posting_zip5:
                return False
    return True


def incumbent_matches(
    *,
    incumbent: str | None,
    title: str | None,
    description: str | None,
) -> bool:
    name = _norm(incumbent)
    if not name or len(name) < 4:
        return False
    haystack = f"{_norm(title)} {_norm(description)}"
    tokens = [t for t in re.split(r"[^\w]+", name) if len(t) >= 4]
    if not tokens:
        return name in haystack
    return sum(1 for token in tokens if token in haystack) >= min(2, len(tokens))


def title_keywords_match(
    keywords: list[str],
    *,
    title: str | None,
    description: str | None,
    min_matches: int = TITLE_KEYWORD_MIN,
) -> tuple[bool, list[str]]:
    if len(keywords) < min_matches:
        min_matches = max(1, len(keywords))
    haystack = f"{_norm(title)} {_norm(description)}"
    hits = [kw for kw in keywords if kw in haystack]
    return len(hits) >= min_matches, hits


def extract_posting_contracting_office(
    *,
    agency: str | None,
    sam_raw: dict[str, Any] | None,
    analysis: dict[str, Any] | None = None,
) -> str | None:
    parts: list[str] = []
    if agency:
        parts.append(str(agency))
    raw = sam_raw if isinstance(sam_raw, dict) else {}
    for key in ("fullParentPathName", "department", "subtierAgency", "organizationName"):
        val = raw.get(key)
        if val:
            parts.append(str(val))
    office = raw.get("officeAddress")
    if isinstance(office, dict):
        for key in ("city", "state", "countryCode", "zipcode", "zip"):
            val = office.get(key)
            if val:
                parts.append(str(val))
    elif office:
        parts.append(str(office))
    sol = (analysis or {}).get("solicitation") if isinstance(analysis, dict) else {}
    if isinstance(sol, dict):
        for key in ("contracting_officer_name", "contracting_office"):
            val = sol.get(key)
            if val:
                parts.append(str(val))
    merged = " ".join(parts).strip()
    return merged or None


def extract_posting_zip(*, location: str | None, sam_raw: dict[str, Any] | None) -> str | None:
    _, _, parsed = _parse_location_parts(location)
    if parsed:
        return parsed
    raw = sam_raw if isinstance(sam_raw, dict) else {}
    place = raw.get("placeOfPerformance") or raw.get("placeOfPerformanceLocation") or raw.get("officeAddress")
    if isinstance(place, dict):
        zip_val = place.get("zip") or place.get("zipcode") or place.get("zipCode")
        if zip_val:
            return str(zip_val).strip()[:5]
    return None


def extract_posting_annual_value(
    *,
    estimated_value: str | None,
    sam_raw: dict[str, Any] | None,
    pricing_intel: dict[str, Any] | None = None,
) -> float | None:
    for candidate in (estimated_value,):
        amount = parse_dollar_amount(candidate)
        if amount:
            return amount
    raw = sam_raw if isinstance(sam_raw, dict) else {}
    for key in ("award", "awardAmount", "estimatedValue", "totalEstimatedValue"):
        amount = parse_dollar_amount(raw.get(key))
        if amount:
            return amount
    intel = pricing_intel if isinstance(pricing_intel, dict) else {}
    pred = intel.get("predecessor_award") if isinstance(intel.get("predecessor_award"), dict) else {}
    amount = parse_dollar_amount(pred.get("annual_amount") or pred.get("recent_annual_amount"))
    if amount:
        return amount
    return None


@dataclass(frozen=True)
class PostingFingerprint:
    contracting_office: str | None
    location: str | None
    location_zip: str | None
    naics_code: str | None
    title: str | None
    description: str | None
    estimated_annual_value: float | None


def posting_fingerprint_from_contract(contract: Any) -> PostingFingerprint:
    analysis = contract.analysis if isinstance(getattr(contract, "analysis", None), dict) else {}
    sam_raw = contract.sam_raw if isinstance(getattr(contract, "sam_raw", None), dict) else {}
    location = getattr(contract, "location", None)
    return PostingFingerprint(
        contracting_office=extract_posting_contracting_office(
            agency=getattr(contract, "agency", None),
            sam_raw=sam_raw,
            analysis=analysis,
        ),
        location=location,
        location_zip=extract_posting_zip(location=location, sam_raw=sam_raw),
        naics_code=getattr(contract, "naics_code", None),
        title=getattr(contract, "title", None),
        description=getattr(contract, "description", None) or analysis.get("plain_english_summary"),
        estimated_annual_value=extract_posting_annual_value(
            estimated_value=getattr(contract, "estimated_value", None),
            sam_raw=sam_raw,
            pricing_intel=getattr(contract, "pricing_intel", None),
        ),
    )


def posting_fingerprint_from_opportunity(opp: dict[str, Any]) -> PostingFingerprint:
    raw = opp.get("sam_raw") if isinstance(opp.get("sam_raw"), dict) else {}
    description = opp.get("description") or raw.get("descriptionText") or raw.get("description")
    location = opp.get("location")
    return PostingFingerprint(
        contracting_office=extract_posting_contracting_office(
            agency=opp.get("agency"),
            sam_raw=raw,
        ),
        location=location,
        location_zip=extract_posting_zip(location=location, sam_raw=raw),
        naics_code=opp.get("naics_code"),
        title=opp.get("title"),
        description=str(description) if description else None,
        estimated_annual_value=extract_posting_annual_value(
            estimated_value=opp.get("estimated_value"),
            sam_raw=raw,
        ),
    )


def score_fingerprint_match(
    posting: PostingFingerprint,
    target: Any,
    *,
    title_keywords: list[str] | None = None,
) -> FingerprintMatchResult:
    keywords = list(title_keywords or getattr(target, "title_keywords", ()) or [])
    if not keywords:
        keywords = extract_title_keywords(
            getattr(target, "contract_name", None),
            min_count=TITLE_KEYWORD_COUNT[0],
            max_count=TITLE_KEYWORD_COUNT[1],
        )

    target_office = getattr(target, "contracting_office", None) or getattr(target, "agency", None)
    watchlist_value = getattr(target, "estimated_annual_value", None)
    if watchlist_value is None:
        watchlist_value = getattr(target, "award_amount", None)

    signals: list[MatchSignal] = []

    office_hit = contracting_office_matches(posting.contracting_office, target_office)
    signals.append(
        MatchSignal(
            "contracting_office",
            office_hit,
            SIGNAL_WEIGHTS["contracting_office"],
            detail=target_office,
        )
    )

    loc_hit = location_matches(
        posting.location,
        posting_zip=posting.location_zip,
        target_city=getattr(target, "location_city", None),
        target_state=getattr(target, "location_state", None),
        target_zip=getattr(target, "location_zip", None),
    )
    loc_detail = ", ".join(
        filter(
            None,
            [
                getattr(target, "location_city", None),
                getattr(target, "location_state", None),
                getattr(target, "location_zip", None),
            ],
        )
    )
    signals.append(MatchSignal("location", loc_hit, SIGNAL_WEIGHTS["location"], detail=loc_detail or None))

    naics_hit = naics_matches(posting.naics_code, getattr(target, "naics_code", None))
    signals.append(
        MatchSignal(
            "naics_code",
            naics_hit,
            SIGNAL_WEIGHTS["naics_code"],
            detail=getattr(target, "naics_code", None),
        )
    )

    inc_hit = incumbent_matches(
        incumbent=getattr(target, "incumbent_name", None),
        title=posting.title,
        description=posting.description,
    )
    signals.append(
        MatchSignal(
            "incumbent_name",
            inc_hit,
            SIGNAL_WEIGHTS["incumbent_name"],
            detail=getattr(target, "incumbent_name", None),
        )
    )

    value_hit = values_within_tolerance(watchlist_value, posting.estimated_annual_value)
    value_detail = None
    if watchlist_value is not None:
        value_detail = f"watchlist={watchlist_value:.0f}"
        if posting.estimated_annual_value is not None:
            value_detail += f", posting={posting.estimated_annual_value:.0f}"
    signals.append(
        MatchSignal(
            "estimated_annual_value",
            value_hit,
            SIGNAL_WEIGHTS["estimated_annual_value"],
            detail=value_detail,
        )
    )

    kw_hit, kw_matched = title_keywords_match(
        keywords,
        title=posting.title,
        description=posting.description,
    )
    signals.append(
        MatchSignal(
            "title_keywords",
            kw_hit,
            SIGNAL_WEIGHTS["title_keywords"],
            detail=", ".join(kw_matched) if kw_matched else ", ".join(keywords[:5]) or None,
        )
    )

    score = sum(s.points for s in signals if s.matched)
    confidence = confidence_from_score(score)
    return FingerprintMatchResult(
        watchlist_id=int(getattr(target, "id")),
        score=score,
        confidence=confidence,
        signals=signals,
        title_keywords=keywords,
    )


def best_fingerprint_match(
    posting: PostingFingerprint,
    targets: list[Any] | tuple[Any, ...],
) -> FingerprintMatchResult | None:
    best: FingerprintMatchResult | None = None
    for target in targets:
        result = score_fingerprint_match(posting, target)
        if result.confidence == "None":
            continue
        if best is None or result.score > best.score:
            best = result
    return best


def log_weak_match(notice_id: str | None, result: FingerprintMatchResult) -> None:
    logger.info(
        "Weak watchlist fingerprint notice_id=%s watchlist_id=%s score=%s signals=%s",
        notice_id,
        result.watchlist_id,
        result.score,
        result.matched_signals,
    )
