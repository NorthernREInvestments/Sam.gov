"""Read GovSpend gs_watchlist targets (read-only) and match SAM.gov contracts."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

from sqlalchemy import text

from db_tables import GS_WATCHLIST_TABLE
from shared_db import SharedReadSession, resolve_watchlist_table_name

_MATCH_FIELDS = ("agency", "location", "naics_code", "incumbent_name")


@dataclass(frozen=True)
class WatchlistTarget:
    id: int
    award_id: str | None
    contract_name: str | None
    agency: str | None
    location_city: str | None
    location_state: str | None
    naics_code: str | None
    incumbent_name: str | None
    award_amount: float | None
    priority: str | None
    status: str | None


def watchlist_status_watching() -> str:
    return os.getenv("GS_WATCHLIST_STATUS", "Watching").strip() or "Watching"


def watchlist_priority_values() -> tuple[str, ...]:
    raw = os.getenv("GS_WATCHLIST_PRIORITIES", "High,Medium")
    values = tuple(v.strip() for v in raw.split(",") if v.strip())
    return values or ("High", "Medium")


def watchlist_match_minimum() -> int:
    try:
        return max(1, int(os.getenv("GS_WATCHLIST_MIN_FIELD_MATCHES", "3")))
    except ValueError:
        return 3


@lru_cache(maxsize=1)
def _resolved_table_cached() -> str | None:
    table = resolve_watchlist_table_name()
    return table or GS_WATCHLIST_TABLE


def clear_watchlist_cache() -> None:
    _resolved_table_cached.cache_clear()
    _load_watching_targets_cached.cache_clear()


def load_watching_targets() -> tuple[WatchlistTarget, ...]:
    """GovSpend rows: priority High/Medium and status Watching."""
    key = f"{watchlist_status_watching()}|{','.join(watchlist_priority_values())}"
    return _load_watching_targets_cached(key)


@lru_cache(maxsize=4)
def _load_watching_targets_cached(cache_key: str) -> tuple[WatchlistTarget, ...]:
    table = _resolved_table_cached()
    if not table:
        return ()

    priorities = watchlist_priority_values()
    status = watchlist_status_watching()
    session = SharedReadSession()
    try:
        rows = session.execute(
            text(
                f"""
                SELECT id, award_id, contract_name, agency, location_city, location_state,
                       naics_code, incumbent_name, award_amount, priority, status
                FROM {table}
                WHERE TRIM(COALESCE(status, '')) = :status
                  AND TRIM(COALESCE(priority, '')) = ANY(:priorities)
                ORDER BY
                  CASE LOWER(TRIM(priority))
                    WHEN 'high' THEN 0
                    WHEN 'medium' THEN 1
                    ELSE 2
                  END,
                  expected_repost_start NULLS LAST,
                  expiration_date NULLS LAST,
                  id ASC
                """
            ),
            {"status": status, "priorities": list(priorities)},
        ).mappings().all()
    except Exception:
        return ()
    finally:
        session.close()

    targets: list[WatchlistTarget] = []
    for row in rows:
        amount = row.get("award_amount")
        targets.append(
            WatchlistTarget(
                id=int(row["id"]),
                award_id=_clean(row.get("award_id")),
                contract_name=_clean(row.get("contract_name")),
                agency=_clean(row.get("agency")),
                location_city=_clean(row.get("location_city")),
                location_state=_clean(row.get("location_state")),
                naics_code=_clean(row.get("naics_code")),
                incumbent_name=_clean(row.get("incumbent_name")),
                award_amount=float(amount) if amount is not None else None,
                priority=_clean(row.get("priority")),
                status=_clean(row.get("status")),
            )
        )
    return tuple(targets)


def _clean(value: Any) -> str | None:
    text_val = str(value or "").strip()
    return text_val or None


def _norm(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def _agency_matches(contract_agency: str | None, target_agency: str | None) -> bool:
    a = _norm(contract_agency)
    b = _norm(target_agency)
    if not a or not b or len(b) < 4:
        return False
    return b in a or a in b


def _naics_matches(contract_naics: str | None, target_naics: str | None) -> bool:
    a = (contract_naics or "").strip()
    b = (target_naics or "").strip()
    if not a or not b:
        return False
    return a == b or a.startswith(b) or b.startswith(a)


def _location_matches(
    contract_location: str | None,
    *,
    target_city: str | None,
    target_state: str | None,
) -> bool:
    if not target_state:
        return False
    loc = _norm(contract_location)
    state = target_state.strip().upper()[:2]
    if not re.search(rf"\b{re.escape(state.lower())}\b", loc) and f", {state.lower()}" not in loc:
        if not loc.endswith(state.lower()):
            return False
    if target_city:
        city = _norm(target_city)
        if len(city) >= 3 and city not in loc:
            return False
    return True


def _incumbent_matches(
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


def score_watchlist_match(
    *,
    agency: str | None,
    location: str | None,
    naics_code: str | None,
    title: str | None,
    description: str | None,
    target: WatchlistTarget,
) -> tuple[int, list[str]]:
    matched: list[str] = []
    if _agency_matches(agency, target.agency):
        matched.append("agency")
    if _location_matches(location, target_city=target.location_city, target_state=target.location_state):
        matched.append("location")
    if _naics_matches(naics_code, target.naics_code):
        matched.append("naics_code")
    if _incumbent_matches(incumbent=target.incumbent_name, title=title, description=description):
        matched.append("incumbent_name")
    return len(matched), matched


def score_contract_against_target(contract: Any, target: WatchlistTarget) -> tuple[int, list[str]]:
    analysis = contract.analysis if isinstance(getattr(contract, "analysis", None), dict) else {}
    return score_watchlist_match(
        agency=getattr(contract, "agency", None),
        location=getattr(contract, "location", None),
        naics_code=getattr(contract, "naics_code", None),
        title=getattr(contract, "title", None),
        description=getattr(contract, "description", None) or analysis.get("plain_english_summary"),
        target=target,
    )


def score_opportunity_against_target(opp: dict[str, Any], target: WatchlistTarget) -> tuple[int, list[str]]:
    raw = opp.get("sam_raw") if isinstance(opp.get("sam_raw"), dict) else {}
    description = opp.get("description") or raw.get("descriptionText") or raw.get("description")
    return score_watchlist_match(
        agency=opp.get("agency"),
        location=opp.get("location"),
        naics_code=opp.get("naics_code"),
        title=opp.get("title"),
        description=str(description) if description else None,
        target=target,
    )


def is_watchlist_field_match(count: int) -> bool:
    return count >= watchlist_match_minimum()


def govspend_watchlist_meta(contract: Any) -> dict[str, Any] | None:
    analysis = contract.analysis if isinstance(getattr(contract, "analysis", None), dict) else {}
    meta = analysis.get("govspend_watchlist")
    return meta if isinstance(meta, dict) and meta.get("sam_found") else None


def is_govspend_watchlist_hit(contract: Any) -> bool:
    return govspend_watchlist_meta(contract) is not None


def stamp_govspend_watchlist_hit(
    contract: Any,
    target: WatchlistTarget,
    *,
    match_fields: list[str],
    match_count: int,
) -> dict[str, Any]:
    """Persist watchlist match locally — GovTracker never writes gs_watchlist."""
    analysis = dict(contract.analysis) if isinstance(contract.analysis, dict) else {}
    meta = {
        "watchlist_id": target.id,
        "award_id": target.award_id,
        "contract_name": target.contract_name,
        "priority": target.priority,
        "status_on_watchlist": target.status,
        "match_count": match_count,
        "match_fields": match_fields,
        "award_amount": target.award_amount,
        "incumbent_name": target.incumbent_name,
        "agency": target.agency,
        "location_city": target.location_city,
        "location_state": target.location_state,
        "naics_code": target.naics_code,
        "matched_at": datetime.now(timezone.utc).isoformat(),
        "sam_found": True,
        "govspend_status_note": (
            "Matched on SAM.gov — GovSpend can mark gs_watchlist status to 'Found on SAM'."
        ),
        "pipeline_status": analysis.get("govspend_watchlist", {}).get("pipeline_status", "queued"),
    }
    analysis["govspend_watchlist"] = meta
    contract.analysis = analysis
    return meta


def watchlist_status() -> dict[str, Any]:
    table = _resolved_table_cached()
    targets = load_watching_targets()
    return {
        "table": table,
        "status_filter": watchlist_status_watching(),
        "priorities": list(watchlist_priority_values()),
        "min_field_matches": watchlist_match_minimum(),
        "watchlist_target_count": len(targets),
        "targets": [
            {
                "id": t.id,
                "award_id": t.award_id,
                "contract_name": t.contract_name,
                "agency": t.agency,
                "naics_code": t.naics_code,
                "location": ", ".join(filter(None, [t.location_city, t.location_state])),
                "incumbent_name": t.incumbent_name,
                "award_amount": t.award_amount,
                "priority": t.priority,
                "status": t.status,
            }
            for t in targets[:100]
        ],
    }


# Backward-compatible alias used by dashboard sorting
def load_priority_targets() -> tuple[WatchlistTarget, ...]:
    return load_watching_targets()


def match_contract_to_targets(
    contract: Any,
    targets: list[WatchlistTarget] | tuple[WatchlistTarget, ...],
) -> tuple[bool, str | None, str | None]:
    if is_govspend_watchlist_hit(contract):
        meta = govspend_watchlist_meta(contract) or {}
        return True, meta.get("priority"), ",".join(meta.get("match_fields") or [])

    best_count = 0
    best_priority: str | None = None
    best_fields: list[str] = []
    for target in targets:
        count, fields = score_contract_against_target(contract, target)
        if count > best_count:
            best_count = count
            best_priority = target.priority
            best_fields = fields
    if is_watchlist_field_match(best_count):
        return True, best_priority, ",".join(best_fields)
    return False, None, None
