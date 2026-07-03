"""Read high-priority targets from the shared gs_watchlist table (other app)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from sqlalchemy import text

from db_tables import GS_WATCHLIST_TABLE
from shared_db import SharedReadSession, resolve_watchlist_table_name


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
    priority: str | None
    status: str | None


def watchlist_priority_values() -> tuple[str, ...]:
    raw = os.getenv("GS_WATCHLIST_PRIORITIES", "high,critical,urgent")
    values = tuple(v.strip().lower() for v in raw.split(",") if v.strip())
    return values or ("high",)


def watchlist_excluded_statuses() -> tuple[str, ...]:
    raw = os.getenv("GS_WATCHLIST_EXCLUDE_STATUSES", "closed,archived,done,inactive")
    return tuple(v.strip().lower() for v in raw.split(",") if v.strip())


@lru_cache(maxsize=1)
def _resolved_table_cached() -> str | None:
    table = resolve_watchlist_table_name()
    return table or GS_WATCHLIST_TABLE


def clear_watchlist_cache() -> None:
    _resolved_table_cached.cache_clear()
    _load_priority_targets_cached.cache_clear()


def load_priority_targets() -> tuple[WatchlistTarget, ...]:
    return _load_priority_targets_cached(",".join(watchlist_priority_values()))


@lru_cache(maxsize=4)
def _load_priority_targets_cached(priority_key: str) -> tuple[WatchlistTarget, ...]:
    table = _resolved_table_cached()
    if not table:
        return ()

    priorities = tuple(priority_key.split(",")) if priority_key else watchlist_priority_values()
    excluded = watchlist_excluded_statuses()
    session = SharedReadSession()
    try:
        rows = session.execute(
            text(
                f"""
                SELECT id, award_id, contract_name, agency, location_city, location_state,
                       naics_code, incumbent_name, priority, status
                FROM {table}
                WHERE priority IS NOT NULL
                  AND LOWER(TRIM(priority)) = ANY(:priorities)
                  AND (
                    status IS NULL
                    OR TRIM(status) = ''
                    OR LOWER(TRIM(status)) <> ALL(:excluded_statuses)
                  )
                ORDER BY expected_repost_start NULLS LAST, expiration_date NULLS LAST, id ASC
                """
            ),
            {"priorities": list(priorities), "excluded_statuses": list(excluded)},
        ).mappings().all()
    except Exception:
        return ()
    finally:
        session.close()

    targets: list[WatchlistTarget] = []
    for row in rows:
        targets.append(
            WatchlistTarget(
                id=int(row["id"]),
                award_id=str(row.get("award_id") or "").strip() or None,
                contract_name=str(row.get("contract_name") or "").strip() or None,
                agency=str(row.get("agency") or "").strip() or None,
                location_city=str(row.get("location_city") or "").strip() or None,
                location_state=str(row.get("location_state") or "").strip() or None,
                naics_code=str(row.get("naics_code") or "").strip() or None,
                incumbent_name=str(row.get("incumbent_name") or "").strip() or None,
                priority=str(row.get("priority") or "").strip() or None,
                status=str(row.get("status") or "").strip() or None,
            )
        )
    return tuple(targets)


def _contract_state(contract: Any) -> str | None:
    location = str(getattr(contract, "location", None) or "")
    match = re.search(r",\s*([A-Z]{2})\b", location)
    if match:
        return match.group(1)
    return None


def match_contract_to_targets(
    contract: Any,
    targets: list[WatchlistTarget] | tuple[WatchlistTarget, ...],
) -> tuple[bool, str | None, str | None]:
    """Return (is_match, priority_label, matched_field) for a contract row."""
    if not targets:
        return False, None, None

    notice_id = str(getattr(contract, "notice_id", None) or "").strip()
    title = str(getattr(contract, "title", None) or "").lower()
    agency = str(getattr(contract, "agency", None) or "").lower()
    naics = str(getattr(contract, "naics_code", None) or "").strip()
    state = _contract_state(contract)
    city = ""
    loc = str(getattr(contract, "location", None) or "")
    city_match = re.match(r"([^,]+),", loc)
    if city_match:
        city = city_match.group(1).strip().lower()

    best_priority: str | None = None
    best_field: str | None = None
    priority_rank = {"critical": 3, "urgent": 2, "high": 1}

    for target in targets:
        matched_field: str | None = None
        if target.award_id and notice_id and target.award_id.lower() in notice_id.lower():
            matched_field = "award_id"
        elif target.naics_code and naics and target.naics_code == naics:
            matched_field = "naics_code"
        elif target.contract_name and target.contract_name.lower() in title:
            matched_field = "contract_name"
        elif target.agency and target.agency.lower() in agency:
            matched_field = "agency"
        elif target.location_state and state and target.location_state.upper() == state:
            if not target.location_city or not city or target.location_city.lower() in city:
                matched_field = "location"

        if not matched_field:
            continue

        label = (target.priority or "high").lower()
        rank = priority_rank.get(label, 1)
        best_rank = priority_rank.get((best_priority or "").lower(), 0)
        if best_priority is None or rank >= best_rank:
            best_priority = target.priority
            best_field = matched_field

    return best_priority is not None, best_priority, best_field


def watchlist_status() -> dict[str, Any]:
    table = _resolved_table_cached()
    targets = load_priority_targets()
    return {
        "table": table,
        "priorities": list(watchlist_priority_values()),
        "priority_target_count": len(targets),
        "targets": [
            {
                "id": t.id,
                "award_id": t.award_id,
                "contract_name": t.contract_name,
                "agency": t.agency,
                "naics_code": t.naics_code,
                "location": ", ".join(filter(None, [t.location_city, t.location_state])),
                "priority": t.priority,
                "status": t.status,
            }
            for t in targets[:100]
        ],
    }
