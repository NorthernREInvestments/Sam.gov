"""USAspending pricing lookups for gt_csv_opportunities (no SAM.gov, no attachments)."""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from csv_opportunity_service import _location_display, _matches_filters
from models import CsvOpportunity
from usaspending_client import CSV_PRICING_LOOKBACK_YEARS, extract_work_location

logger = logging.getLogger("govtracker.csv_pricing")

SAME_LOCATION_METHODS = frozenset(
    {
        "contract_number",
        "contract_number_keyword",
        "manual_contract_number",
        "same_site_match",
        "facility_keyword",
        "agency_facility_match",
        "same_city_match",
        "recipient_search",
        "incumbent_name_match",
    }
)


class _CsvPricingTarget:
    """Minimal contract-like object for existing pricing helpers."""

    def __init__(self, row: CsvOpportunity) -> None:
        self.notice_id = row.notice_id
        self.title = row.title
        self.agency = row.agency
        self.location = _location_display(row)
        self.naics_code = row.naics_code
        self.description = row.description
        self.sam_raw = row.sam_raw if isinstance(row.sam_raw, dict) else {}
        self.analysis: dict[str, Any] = {}
        self.pricing_intel = None
        self.estimated_value = None
        self.attachment_text = None


def _short_company_name(name: str | None, max_len: int = 28) -> str:
    if not name:
        return ""
    cleaned = str(name).strip()
    if len(cleaned) <= max_len:
        return cleaned
    return f"{cleaned[: max_len - 1]}…"


def _short_money(value: Any) -> str:
    amount = float(value)
    if amount >= 1_000_000:
        return f"${amount / 1_000_000:.1f}M".replace(".0M", "M")
    if amount >= 1000:
        return f"${round(amount / 1000)}k"
    return f"${amount:,.0f}"


def csv_pricing_card_display(intel: dict[str, Any] | None) -> dict[str, Any]:
    """Dashboard pricing line for a CSV opportunity card."""
    if not intel or not isinstance(intel, dict):
        return {
            "kind": "pending",
            "line": "Pricing not run yet",
            "main_line": "Pricing not run yet",
            "source_label": None,
            "unique_bidders": None,
            "lookup_method": None,
        }

    intel = dict(intel)
    if intel.get("error"):
        error = str(intel.get("error") or "Pricing lookup failed")
        return {
            "kind": "error",
            "line": error,
            "main_line": error,
            "source_label": None,
            "unique_bidders": None,
            "lookup_method": None,
        }

    predecessor = intel.get("predecessor_award") if isinstance(intel.get("predecessor_award"), dict) else {}
    unique_bidders = intel.get("unique_bidders")
    if unique_bidders is None:
        unique_bidders = intel.get("awards_count")

    if predecessor and predecessor.get("is_prior_contract"):
        annual = (
            predecessor.get("recent_annual_amount")
            or predecessor.get("annual_amount")
            or predecessor.get("base_year_amount")
            or predecessor.get("total_value")
        )
        recipient = _short_company_name(predecessor.get("recipient_name"))
        method = str(predecessor.get("lookup_method") or "")
        source_label = "Same location" if method in SAME_LOCATION_METHODS else "Regional estimate"
        if annual and recipient:
            main_line = f"Prior: {_short_money(annual)}/yr · {recipient}"
        elif annual:
            main_line = f"Prior: {_short_money(annual)}/yr"
        else:
            main_line = "Prior contract found — amount unavailable"
        return {
            "kind": "prior",
            "line": main_line,
            "main_line": main_line,
            "source_label": source_label,
            "unique_bidders": unique_bidders,
            "lookup_method": method or None,
        }

    avg = intel.get("average_annual_award")
    if avg:
        main_line = f"Regional avg: {_short_money(avg)}/yr"
        return {
            "kind": "regional",
            "line": main_line,
            "main_line": main_line,
            "source_label": "Regional estimate",
            "unique_bidders": unique_bidders,
            "lookup_method": None,
        }

    return {
        "kind": "none",
        "line": "No prior pricing on file",
        "main_line": "No prior pricing on file",
        "source_label": None,
        "unique_bidders": unique_bidders,
        "lookup_method": None,
    }


def csv_row_needs_pricing(row: CsvOpportunity, *, force: bool = False) -> bool:
    """True when this row should be included in a pricing batch (resume skips completed lookups)."""
    if force:
        return True
    intel = row.pricing_intel if isinstance(row.pricing_intel, dict) else None
    if not intel:
        return True
    if intel.get("cached_at"):
        return False
    if intel.get("error") in ("Pricing lookup timed out",):
        return True
    return not bool(intel.get("tier"))


def lookup_csv_opportunity_pricing_with_timeout(
    row: CsvOpportunity,
    *,
    timeout_seconds: float = 120.0,
) -> dict[str, Any]:
    """Run lookup with a per-row timeout so one slow USAspending response cannot stall the batch."""
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(lookup_csv_opportunity_pricing, row)
        try:
            return future.result(timeout=timeout_seconds)
        except FuturesTimeoutError:
            logger.warning("CSV pricing timed out for %s after %ss", row.notice_id, timeout_seconds)
            payload = {
                "error": "Pricing lookup timed out",
                "tier": "csv_usaspending",
                "naics_code": row.naics_code,
                "state_code": row.location_state,
            }
            return {
                "pricing_intel": payload,
                "pricing_display": csv_pricing_card_display(payload),
            }


def lookup_csv_opportunity_pricing(row: CsvOpportunity) -> dict[str, Any]:
    """Query USAspending for one CSV row using NAICS + city/state + agency (+ CSV description hints)."""
    from location_matching import extract_site_profile, extract_site_profiles
    from prior_contract_extract import merge_prior_contract_hints
    from pricing import _merge_predecessor, _solicitation_pricing_hints
    from usaspending_client import fetch_predecessor_pricing, fetch_regional_benchmarks

    target = _CsvPricingTarget(row)
    merge_prior_contract_hints(target)

    naics_code = (row.naics_code or "").strip() or None
    work = extract_work_location(
        target.location,
        target.sam_raw,
        title=row.title,
        description=row.description,
    )
    state_code = (work.get("state_code") or (row.location_state or "").strip().upper() or None)
    city = work.get("city") or row.location_city

    if not naics_code:
        payload = {"error": "NAICS code missing", "tier": "csv_usaspending"}
        return {"pricing_intel": payload, "pricing_display": csv_pricing_card_display(payload)}

    if not state_code:
        payload = {"error": "Work state missing", "tier": "csv_usaspending", "naics_code": naics_code}
        return {"pricing_intel": payload, "pricing_display": csv_pricing_card_display(payload)}

    lookback = CSV_PRICING_LOOKBACK_YEARS
    hints = _solicitation_pricing_hints(target)
    site_profiles = extract_site_profiles(target)
    origin_profile = dict(site_profiles[0] if site_profiles else extract_site_profile(target))
    if site_profiles:
        origin_profile["_all_profiles"] = site_profiles
    origin_profile["_exclude_contract_numbers"] = hints.get("exclude_contract_numbers") or []

    try:
        intel = fetch_regional_benchmarks(
            naics_code,
            state_code,
            origin_profile=origin_profile,
            agency=row.agency,
            city=city,
            lookback_years=lookback,
        )
        predecessor = fetch_predecessor_pricing(
            previous_contract_number=hints.get("previous_contract_number"),
            incumbent_contractor=hints.get("incumbent_contractor"),
            naics_code=naics_code,
            state_code=state_code,
            city=city,
            agency=row.agency,
            extra_contract_numbers=hints.get("extra_contract_numbers"),
            origin_profile=origin_profile,
            facility_terms=hints.get("facility_terms"),
            title=row.title,
            lookback_years=lookback,
        )
        intel = _merge_predecessor(intel, predecessor)
    except Exception as exc:
        logger.exception("CSV pricing lookup failed for %s", row.notice_id)
        payload = {
            "error": str(exc),
            "tier": "csv_usaspending",
            "naics_code": naics_code,
            "state_code": state_code,
        }
        return {"pricing_intel": payload, "pricing_display": csv_pricing_card_display(payload)}

    intel["cached_at"] = datetime.now(timezone.utc).isoformat()
    intel["tier"] = "csv_usaspending"
    intel["lookback_years"] = lookback
    intel["source"] = "csv_batch_usaspending"
    display = csv_pricing_card_display(intel)
    return {"pricing_intel": intel, "pricing_display": display}


def refresh_csv_opportunity_pricing(
    session: Session,
    row: CsvOpportunity,
    *,
    timeout_seconds: float = 120.0,
) -> dict[str, Any]:
    """Run lookup and persist on gt_csv_opportunities.pricing_intel."""
    result = lookup_csv_opportunity_pricing_with_timeout(row, timeout_seconds=timeout_seconds)
    row.pricing_intel = result.get("pricing_intel")
    session.flush()
    return {
        "notice_id": row.notice_id,
        **result,
    }


def csv_opportunity_ids_for_notice_ids(
    session: Session,
    notice_ids: list[str],
    *,
    force: bool = False,
) -> list[int]:
    """Resolve notice IDs to row IDs that still need pricing."""
    unique = list(dict.fromkeys(notice_id for notice_id in notice_ids if notice_id))
    if not unique:
        return []
    rows = session.query(CsvOpportunity).filter(CsvOpportunity.notice_id.in_(unique)).all()
    return [row.id for row in rows if csv_row_needs_pricing(row, force=force)]


def csv_opportunity_ids_for_filters(
    session: Session,
    *,
    state: str | None = None,
    days_bucket: str | None = None,
    naics_code: str | None = None,
    keyword: str | None = None,
    force: bool = False,
) -> list[int]:
    """Resolve filtered CSV row IDs for a pricing batch job."""
    from csv_opportunity_service import _days_bucket, csv_opportunity_to_card_dict

    today = date.today()
    rows = session.query(CsvOpportunity).order_by(CsvOpportunity.due_date.asc().nullslast()).all()
    keyword = (keyword or "").strip() or None
    state = (state or "").strip().upper() or None
    naics_code = (naics_code or "").strip() or None
    days_bucket = (days_bucket or "").strip().lower() or None
    if days_bucket == "all":
        days_bucket = None

    ids: list[int] = []
    for row in rows:
        card = csv_opportunity_to_card_dict(row, today=today)
        if _matches_filters(
            card,
            state=state,
            days_bucket=days_bucket,
            naics_code=naics_code,
            keyword=keyword,
        ):
            if csv_row_needs_pricing(row, force=force):
                ids.append(row.id)
    return ids


def run_csv_pricing_batch(
    session: Session,
    row_ids: list[int],
    *,
    progress_callback: Any | None = None,
    delay_seconds: float = 0.2,
) -> dict[str, Any]:
    """Process a list of CSV opportunity IDs with optional progress updates."""
    total = len(row_ids)
    processed = 0
    found_prior = 0
    found_regional = 0
    errors = 0

    for csv_id in row_ids:
        row = session.get(CsvOpportunity, csv_id)
        if not row:
            processed += 1
            continue
        try:
            result = refresh_csv_opportunity_pricing(session, row)
            session.commit()
            kind = (result.get("pricing_display") or {}).get("kind")
            if kind == "prior":
                found_prior += 1
            elif kind == "regional":
                found_regional += 1
        except Exception:
            session.rollback()
            errors += 1
            logger.exception("CSV pricing batch failed for id=%s", csv_id)
        processed += 1
        if progress_callback:
            progress_callback(
                processed=processed,
                total=total,
                notice_id=row.notice_id if row else None,
            )
        if delay_seconds > 0 and processed < total:
            time.sleep(delay_seconds)

    return {
        "processed": processed,
        "total": total,
        "found_prior": found_prior,
        "found_regional": found_regional,
        "errors": errors,
    }
