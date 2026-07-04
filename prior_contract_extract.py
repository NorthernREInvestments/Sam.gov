"""Extract incumbent and prior contract # from solicitation PDF text — always when present."""

from __future__ import annotations

import re
from typing import Any

from usaspending_client import (
    extract_contract_numbers,
    extract_pricing_hints_from_text,
    is_plausible_contract_number,
    normalize_contract_number,
)

_PRIOR_META_KEYS = ("incumbent_contractor", "previous_contract_number")
_PRIOR_PRICING_META_KEYS = (
    "prior_contract_tcv",
    "prior_contract_annual",
    "prior_contract_amount_source",
)

_TCV_RE = re.compile(
    r"(?:Total\s+Contract\s+Value(?:\s*\(TCV\))?|TCV)\s*[:\s]*\$?\s*([\d,]+(?:\.\d{2})?)",
    re.IGNORECASE,
)
_ESTIMATED_PRIOR_VALUE_RE = re.compile(
    r"\$([\d,]+(?:\.\d{2})?).{0,80}?\bprior\s+contract\b",
    re.IGNORECASE,
)
_OPTION_YEARS_RE = re.compile(r"(\d+)\s*(?:\(\d+\)\s*)?option\s*years?", re.IGNORECASE)
_MONTH_EXTENSION_RE = re.compile(
    r"(\d+)\s*(?:\(\d+\)\s*)?months?\s+(?:option|extension)",
    re.IGNORECASE,
)

# Labeled fields common in federal solicitation background sections.
_INCUMBENT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?:incumbent|current|existing|present|previous|prior)\s+"
        r"(?:contractor|vendor|awardee|contractor/vendor)\s*(?:is|:)?\s*"
        r"([A-Z][A-Za-z0-9&.,'\-\s]{2,90}?)(?:\.|;|\n|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:awarded\s+to|contract(?:or)?\s+is|performed\s+by)\s+"
        r"([A-Z][A-Za-z0-9&.,'\-\s]{2,90}?)(?:\.|;|\n|$)",
        re.IGNORECASE,
    ),
)

_PREVIOUS_NUMBER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?:previous|prior|predecessor|expiring|current|existing|incumbent|old)\s+"
        r"(?:contract|award|piid|order)\s*(?:number|no\.?|#)\s*(?:is|:)?\s*"
        r"([A-Z0-9][A-Z0-9\-\s/]{5,45})",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:contract|award|piid|task\s+order)\s*(?:number|no\.?|#)\s*(?:is|:)?\s*"
        r"([A-Z0-9][A-Z0-9\-\s/]{5,45})",
        re.IGNORECASE,
    ),
)


def prior_contract_hints_complete(analysis: dict[str, Any] | None) -> bool:
    sol = _sol(analysis)
    return bool(
        str(sol.get("previous_contract_number") or "").strip()
        or str(sol.get("incumbent_contractor") or "").strip()
    )


def _sol(analysis: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(analysis, dict):
        return {}
    sol = analysis.get("solicitation_meta")
    return dict(sol) if isinstance(sol, dict) else {}


def _clean_incumbent_name(value: str) -> str | None:
    cleaned = re.sub(r"\s+", " ", value).strip(" .,;:-")
    if len(cleaned) < 3:
        return None
    lower = cleaned.lower()
    if lower in ("the", "none", "n/a", "unknown", "tbd"):
        return None
    if re.match(r"^(contract|award|number|piid)\b", lower):
        return None
    reject_phrases = (
        "rated",
        "required to",
        "unable to",
        "government personnel",
        "must ",
        "shall ",
        "contractor shall",
        "a rated",
    )
    if any(phrase in lower for phrase in reject_phrases):
        return None
    if len(cleaned) < 12 and cleaned.isupper() and "LLC" not in cleaned and "INC" not in cleaned:
        return None
    return cleaned[:120]


def _looks_like_fake_contract_number(value: str) -> bool:
    return not is_plausible_contract_number(value)


def _parse_money_amount(value: str | None) -> float | None:
    if not value:
        return None
    cleaned = re.sub(r"[^\d.]", "", str(value))
    if not cleaned:
        return None
    try:
        amount = float(cleaned)
    except ValueError:
        return None
    return amount if amount > 0 else None


def _prior_contract_pricing_section(text: str) -> str:
    match = re.search(
        r"(?i)(?:prior|previous|expiring|incumbent)\s+contract(?:\s+information)?",
        text,
    )
    if match:
        return text[match.start() : match.start() + 3000]
    return text[:12000]


def _estimate_contract_years(text: str) -> float | None:
    section = _prior_contract_pricing_section(text)
    years = 0.0
    if re.search(r"\bbase\s*\+\s*", section, re.IGNORECASE) or re.search(
        r"\bbase\s*\+\s*", text, re.IGNORECASE
    ):
        years += 1.0
    elif re.search(r"\bbase\s+year\b", section, re.IGNORECASE) or re.search(
        r"\bbase\s+year\b", text, re.IGNORECASE
    ):
        years += 1.0
    option_years = [int(match) for match in _OPTION_YEARS_RE.findall(section)]
    if not option_years:
        option_years = [int(match) for match in _OPTION_YEARS_RE.findall(text)]
    if option_years:
        years += max(option_years)
    months = sum(int(match) for match in _MONTH_EXTENSION_RE.findall(section))
    if not months:
        months = sum(int(match) for match in _MONTH_EXTENSION_RE.findall(text))
    years += months / 12.0
    if years >= 0.5:
        return years
    period_match = re.search(
        r"(\d+(?:\.\d+)?)\s*years?\s+(?:contract|period|performance)",
        section,
        re.IGNORECASE,
    )
    if period_match:
        return float(period_match.group(1))
    return None


def extract_prior_pricing_from_text(text: str | None) -> dict[str, Any]:
    """Pull predecessor TCV / estimated annual pay from solicitation PDF text."""
    result: dict[str, Any] = {
        "prior_contract_tcv": None,
        "prior_contract_annual": None,
        "prior_contract_amount_source": None,
    }
    if not text or not str(text).strip():
        return result

    section = _prior_contract_pricing_section(str(text))
    tcv_match = _TCV_RE.search(section) or _TCV_RE.search(str(text))
    if not tcv_match:
        return result

    tcv = _parse_money_amount(tcv_match.group(1))
    if not tcv:
        return result

    result["prior_contract_tcv"] = round(tcv, 2)
    result["prior_contract_amount_source"] = "pdf"
    years = _estimate_contract_years(section) or _estimate_contract_years(str(text))
    if years and years > 0:
        result["prior_contract_annual"] = round(tcv / years, 2)
    return result


def parse_prior_amount_from_estimated_value(value: str | None) -> dict[str, Any]:
    """Use Claude screening estimated_value when it cites prior-contract dollars."""
    if not value or not str(value).strip():
        return {}
    text = str(value).strip()
    if "prior contract" not in text.lower():
        return {}

    match = _ESTIMATED_PRIOR_VALUE_RE.search(text)
    if not match:
        return {}

    tcv = _parse_money_amount(match.group(1))
    if not tcv:
        return {}

    out: dict[str, Any] = {
        "prior_contract_tcv": round(tcv, 2),
        "prior_contract_amount_source": "estimated_value",
    }
    years = _estimate_contract_years(text)
    if years and years > 0:
        out["prior_contract_annual"] = round(tcv / years, 2)
    return out


def _clean_contract_number(value: str) -> str | None:
    raw = str(value).strip()
    normalized = normalize_contract_number(raw)
    if normalized and not _looks_like_fake_contract_number(normalized):
        return normalized
    compact = re.sub(r"[^A-Z0-9]", "", raw.upper())
    if len(compact) >= 10 and not _looks_like_fake_contract_number(compact):
        return compact
    return None


def extract_prior_contract_from_text(text: str | None) -> dict[str, Any]:
    """Deterministic extraction from full attachment text — no API calls."""
    result: dict[str, Any] = {
        "incumbent_contractor": None,
        "previous_contract_number": None,
        "extra_contract_numbers": [],
        "extraction_source": None,
        "prior_contract_tcv": None,
        "prior_contract_annual": None,
        "prior_contract_amount_source": None,
    }
    if not text or not str(text).strip():
        return result

    body = str(text)
    hints = extract_pricing_hints_from_text(body)
    if hints.get("incumbent_contractor"):
        result["incumbent_contractor"] = hints["incumbent_contractor"]
    if hints.get("previous_contract_number"):
        result["previous_contract_number"] = hints["previous_contract_number"]
    result["extra_contract_numbers"] = [
        n for n in (hints.get("extra_contract_numbers") or []) if is_plausible_contract_number(n)
    ]

    if not result["incumbent_contractor"]:
        for pattern in _INCUMBENT_PATTERNS:
            match = pattern.search(body)
            if match:
                name = _clean_incumbent_name(match.group(1))
                if name:
                    result["incumbent_contractor"] = name
                    break

    numbers: list[str] = list(result["extra_contract_numbers"])
    seen = set(numbers)
    if not result["previous_contract_number"]:
        for pattern in _PREVIOUS_NUMBER_PATTERNS:
            for match in pattern.findall(body):
                candidate = match if isinstance(match, str) else match[0]
                normalized = _clean_contract_number(candidate)
                if normalized and normalized not in seen:
                    result["previous_contract_number"] = normalized
                    seen.add(normalized)
                    numbers.append(normalized)
                    break
            if result["previous_contract_number"]:
                break

    for number in extract_contract_numbers(body):
        if number not in seen:
            seen.add(number)
            numbers.append(number)
    result["extra_contract_numbers"] = numbers
    if not result["previous_contract_number"] and numbers:
        result["previous_contract_number"] = numbers[0]

    pricing = extract_prior_pricing_from_text(body)
    for key in _PRIOR_PRICING_META_KEYS:
        if pricing.get(key) and not result.get(key):
            result[key] = pricing[key]

    if result["previous_contract_number"] or result["incumbent_contractor"]:
        result["extraction_source"] = "attachment_text"
    return result


def _normalize_screen_meta(analysis: dict[str, Any]) -> dict[str, Any]:
    """Pull prior-contract fields from full screening output into solicitation_meta."""
    sol = _sol(analysis)
    changed = False
    for key in _PRIOR_META_KEYS:
        top = str(analysis.get(key) or "").strip()
        nested = str(sol.get(key) or "").strip()
        if top and not nested:
            sol[key] = top
            changed = True
        elif nested and not top:
            analysis[key] = nested
            changed = True
    if changed:
        analysis["solicitation_meta"] = sol
    return analysis


def _sanitize_stored_hints(sol: dict[str, Any], *, notice_id: str | None = None) -> dict[str, Any]:
    """Drop garbage values saved by older extraction passes."""
    cleaned = dict(sol)
    prev = _clean_contract_number(str(cleaned.get("previous_contract_number") or ""))
    if prev and notice_id:
        nid = re.sub(r"[^A-Z0-9]", "", str(notice_id).upper())
        if nid and prev.startswith(nid[:12]):
            prev = None
    cleaned["previous_contract_number"] = prev
    inc = _clean_incumbent_name(str(cleaned.get("incumbent_contractor") or ""))
    cleaned["incumbent_contractor"] = inc
    extra = []
    for number in cleaned.get("extra_contract_numbers") or []:
        normalized = _clean_contract_number(str(number))
        if normalized and normalized not in extra:
            extra.append(normalized)
    cleaned["extra_contract_numbers"] = extra
    return cleaned


def merge_prior_contract_hints(contract: Any) -> bool:
    """
    Merge prior-contract hints from every PDF text source into solicitation_meta.
    Returns True if new data was written.
    """
    analysis = dict(contract.analysis) if isinstance(contract.analysis, dict) else {}
    analysis = _normalize_screen_meta(analysis)
    if not isinstance(analysis.get("solicitation_meta"), dict):
        analysis["solicitation_meta"] = {}
    contract.analysis = analysis
    sol = _sanitize_stored_hints(dict(analysis["solicitation_meta"]), notice_id=getattr(contract, "notice_id", None))
    changed = sol != analysis.get("solicitation_meta")

    blobs: list[str] = []
    stored = getattr(contract, "attachment_text", None)
    if stored and str(stored).strip():
        blobs.append(str(stored))
    if getattr(contract, "description", None):
        blobs.append(str(contract.description))
    if analysis.get("plain_english_summary"):
        blobs.append(str(analysis["plain_english_summary"]))

    merged_numbers: list[str] = list(sol.get("extra_contract_numbers") or [])
    seen_numbers = set(merged_numbers)

    for blob in blobs:
        extracted = extract_prior_contract_from_text(blob)
        extracted_prev = _clean_contract_number(str(extracted.get("previous_contract_number") or ""))
        stored_prev = _clean_contract_number(str(sol.get("previous_contract_number") or ""))
        if extracted_prev and extracted_prev != stored_prev:
            sol["previous_contract_number"] = extracted_prev
            changed = True
        if not sol.get("incumbent_contractor") and extracted.get("incumbent_contractor"):
            sol["incumbent_contractor"] = extracted["incumbent_contractor"]
            changed = True
        for key in _PRIOR_PRICING_META_KEYS:
            if extracted.get(key) and not sol.get(key):
                sol[key] = extracted[key]
                changed = True
        for number in extracted.get("extra_contract_numbers") or []:
            cleaned = _clean_contract_number(str(number))
            if cleaned and cleaned not in seen_numbers:
                seen_numbers.add(cleaned)
                merged_numbers.append(cleaned)

    merged_numbers = [_clean_contract_number(str(n)) for n in merged_numbers]
    merged_numbers = [n for n in merged_numbers if n]

    if merged_numbers:
        sol["extra_contract_numbers"] = merged_numbers
        if not sol.get("previous_contract_number"):
            sol["previous_contract_number"] = merged_numbers[0]
            changed = True

    if not sol.get("prior_contract_tcv"):
        estimated = parse_prior_amount_from_estimated_value(
            getattr(contract, "estimated_value", None) or analysis.get("estimated_value")
        )
        if estimated:
            for key, value in estimated.items():
                if value and not sol.get(key):
                    sol[key] = value
                    changed = True

    if sol.get("prior_contract_tcv") and not sol.get("prior_contract_annual"):
        years = _estimate_contract_years(
            f"{getattr(contract, 'estimated_value', '') or ''} {analysis.get('estimated_value') or ''}"
        )
        if not years and getattr(contract, "attachment_text", None):
            years = _estimate_contract_years(str(contract.attachment_text))
        if years:
            sol["prior_contract_annual"] = round(float(sol["prior_contract_tcv"]) / years, 2)
            changed = True

    if changed:
        analysis["solicitation_meta"] = sol
        for key in _PRIOR_META_KEYS:
            if sol.get(key):
                analysis[key] = sol[key]
        contract.analysis = analysis
    return changed


def ensure_prior_contract_from_pdfs(contract: Any, session=None, *, force: bool = False) -> dict[str, Any]:
    """
    Guarantee prior-contract fields are extracted from PDFs when present.
    1) Regex on stored attachment text (free)
    2) Claude solicitation meta if still missing (uses PDFs, not SAM.gov)
    """
    analysis = dict(contract.analysis) if isinstance(contract.analysis, dict) else {}
    merge_prior_contract_hints(contract)
    analysis = dict(contract.analysis) if isinstance(contract.analysis, dict) else analysis
    sol = _sol(analysis)

    if prior_contract_hints_complete(analysis) and not force:
        return sol

    from api_budget import can_screen

    if not can_screen():
        return sol

    from proposal_service import ensure_solicitation_meta

    if session is not None:
        ensure_solicitation_meta(session, contract, force=True)
    else:
        from database import SessionLocal

        s = SessionLocal()
        try:
            ensure_solicitation_meta(s, contract, force=True)
            s.commit()
        finally:
            s.close()

    merge_prior_contract_hints(contract)
    return _sol(contract.analysis if isinstance(contract.analysis, dict) else {})


def reextract_attachment_text_from_db(session, contract: Any, *, max_pdfs: int = 12) -> bool:
    """Rebuild attachment_text from PDF bytes already stored in PostgreSQL — no SAM.gov calls."""
    from attachment_storage import stored_pdf_items
    from pdf_text import extract_pdf_text

    if not contract.id:
        return False
    items = stored_pdf_items(session, contract.id)
    if not items:
        return False

    parts: list[str] = []
    for name, data in items[:max_pdfs]:
        try:
            text = extract_pdf_text(data)
        except Exception:
            text = ""
        if text and str(text).strip():
            label = name or "document.pdf"
            parts.append(f"--- {label} ---\n{text.strip()}")

    if not parts:
        return False

    from datetime import datetime, timezone

    merged = "\n\n".join(parts)
    contract.attachment_text = merged
    contract.attachment_extraction_method = "text"
    contract.attachment_text_extracted_at = datetime.now(timezone.utc)
    return True


def backfill_prior_contract_and_pricing(session, contract: Any) -> dict[str, Any]:
    """
    Backfill prior-contract hints + USAspending pricing using only:
    - stored attachment_text
    - PDF bytes already in PostgreSQL
  Never calls SAM.gov.
    """
    from watchlist_pricing import apply_watchlist_pricing, pricing_from_watchlist, should_skip_usaspending_lookup

    if should_skip_usaspending_lookup(contract):
        if apply_watchlist_pricing(contract):
            pred = contract.pricing_intel.get("predecessor_award") if isinstance(contract.pricing_intel, dict) else {}
            pred = pred if isinstance(pred, dict) else {}
            return {
                "skipped_usaspending": True,
                "watchlist_sourced": True,
                "is_prior_contract": bool(pred.get("is_prior_contract")),
                "lookup_method": pred.get("lookup_method"),
                "annual_amount": pred.get("recent_annual_amount") or pred.get("annual_amount"),
                "incumbent_contractor": pred.get("recipient_name"),
                "awarding_office": pred.get("awarding_office"),
                "pricing_intel": contract.pricing_intel,
            }

    had_text = bool(str(getattr(contract, "attachment_text", None) or "").strip())
    if not had_text:
        reextract_attachment_text_from_db(session, contract)

    merge_prior_contract_hints(contract)
    analysis = contract.analysis if isinstance(contract.analysis, dict) else {}
    sol = analysis.get("solicitation_meta") if isinstance(analysis.get("solicitation_meta"), dict) else {}

    contract.pricing_intel = None
    intel = refresh_pricing_after_pdf_extract(contract)
    if intel is None:
        from pricing import get_regional_benchmark

        intel = get_regional_benchmark(contract, force_refresh=True)

    pred = intel.get("predecessor_award") if isinstance(intel.get("predecessor_award"), dict) else {}
    return {
        "had_attachment_text": had_text,
        "reextracted_from_db": not had_text and bool(contract.attachment_text),
        "incumbent_contractor": sol.get("incumbent_contractor"),
        "previous_contract_number": sol.get("previous_contract_number"),
        "is_prior_contract": bool(pred.get("is_prior_contract")),
        "lookup_method": pred.get("lookup_method"),
        "annual_amount": pred.get("recent_annual_amount") or pred.get("annual_amount") or intel.get("average_annual_award"),
        "pricing_intel": intel,
    }


def refresh_pricing_after_pdf_extract(contract: Any) -> dict[str, Any] | None:
    """Re-run USAspending prior-contract lookup after PDF hints are merged."""
    from watchlist_pricing import should_skip_usaspending_lookup, pricing_from_watchlist

    if should_skip_usaspending_lookup(contract):
        payload = pricing_from_watchlist(contract)
        if payload:
            contract.pricing_intel = payload
            from usaspending_savings import record_usaspending_skip

            record_usaspending_skip(contract)
            return payload
        return None

    from pricing import get_regional_benchmark

    contract.pricing_intel = None
    return get_regional_benchmark(contract, force_refresh=True)
