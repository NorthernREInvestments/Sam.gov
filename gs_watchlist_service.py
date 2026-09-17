"""Read GovSpend gs_watchlist targets (read-only) and match SAM.gov contracts."""

from __future__ import annotations
from application_clock import now_utc

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

from sqlalchemy import text

from db_tables import GS_WATCHLIST_TABLE
from shared_db import SharedReadSession, resolve_watchlist_table_name
from watchlist_fingerprint import (
    FingerprintMatchResult,
    PostingFingerprint,
    best_fingerprint_match,
    extract_title_keywords,
    posting_fingerprint_from_contract,
    posting_fingerprint_from_opportunity,
    score_fingerprint_match,
    should_notify_govspend,
    should_surface_match,
    should_trigger_pipeline,
)


@dataclass(frozen=True)
class WatchlistTarget:
    id: int
    award_id: str | None
    contract_name: str | None
    agency: str | None
    contracting_office: str | None
    location_city: str | None
    location_state: str | None
    location_zip: str | None
    naics_code: str | None
    incumbent_name: str | None
    award_amount: float | None
    estimated_annual_value: float | None
    title_keywords: tuple[str, ...]
    priority: str | None
    status: str | None


def watchlist_status_watching() -> str:
    """Deprecated — GovTracker loads all gs_watchlist rows; GovSpend UI filters are ignored."""
    return os.getenv("GS_WATCHLIST_STATUS", "Watching").strip() or "Watching"


def watchlist_priority_values() -> tuple[str, ...]:
    """Deprecated — GovTracker loads all gs_watchlist rows; GovSpend UI filters are ignored."""
    raw = os.getenv("GS_WATCHLIST_PRIORITIES", "High,Medium")
    values = tuple(v.strip() for v in raw.split(",") if v.strip())
    return values or ("High", "Medium")


def _watchlist_order_by(columns: frozenset[str]) -> str:
    parts: list[str] = []
    if "priority" in columns:
        parts.append(
            """CASE LOWER(TRIM(priority))
                    WHEN 'high' THEN 0
                    WHEN 'medium' THEN 1
                    ELSE 2
                  END"""
        )
    if "expected_repost_start" in columns:
        parts.append("expected_repost_start NULLS LAST")
    if "expiration_date" in columns:
        parts.append("expiration_date NULLS LAST")
    parts.append("id ASC")
    return ", ".join(parts)


@lru_cache(maxsize=1)
def _resolved_table_cached() -> str | None:
    table = resolve_watchlist_table_name()
    return table or GS_WATCHLIST_TABLE


@lru_cache(maxsize=1)
def _watchlist_table_columns() -> frozenset[str]:
    table = _resolved_table_cached()
    if not table:
        return frozenset()
    session = SharedReadSession()
    try:
        rows = session.execute(
            text(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = :table
                """
            ),
            {"table": table},
        ).scalars().all()
        return frozenset(str(row).lower() for row in rows)
    except Exception:
        return frozenset()
    finally:
        session.close()


def clear_watchlist_cache() -> None:
    _resolved_table_cached.cache_clear()
    _watchlist_table_columns.cache_clear()
    _load_watching_targets_cached.cache_clear()


def load_watching_targets() -> tuple[WatchlistTarget, ...]:
    """All GovSpend gs_watchlist rows — no status, priority, or option-year filtering."""
    table = _resolved_table_cached() or ""
    return _load_watching_targets_cached(table)


def _select_columns() -> list[str]:
    columns = _watchlist_table_columns()
    base = [
        "id",
        "award_id",
        "contract_name",
        "agency",
        "location_city",
        "location_state",
        "naics_code",
        "incumbent_name",
        "award_amount",
        "priority",
        "status",
    ]
    optional = ["contracting_office", "location_zip", "estimated_annual_value"]
    selected = list(base)
    for col in optional:
        if col in columns:
            selected.append(col)
    return selected


@lru_cache(maxsize=4)
def _load_watching_targets_cached(cache_key: str) -> tuple[WatchlistTarget, ...]:
    table = _resolved_table_cached()
    if not table:
        return ()

    columns = _watchlist_table_columns()
    selected = _select_columns()
    order_by = _watchlist_order_by(columns)
    session = SharedReadSession()
    try:
        rows = session.execute(
            text(
                f"""
                SELECT {", ".join(selected)}
                FROM {table}
                ORDER BY {order_by}
                """
            ),
        ).mappings().all()
    except Exception:
        return ()
    finally:
        session.close()

    targets: list[WatchlistTarget] = []
    for row in rows:
        amount = row.get("award_amount")
        annual = row.get("estimated_annual_value")
        award_amount = float(amount) if amount is not None else None
        estimated_annual = float(annual) if annual is not None else award_amount
        contract_name = _clean(row.get("contract_name"))
        keywords = tuple(
            extract_title_keywords(contract_name, min_count=3, max_count=5)
        )
        contracting_office = _clean(row.get("contracting_office")) or _clean(row.get("agency"))
        targets.append(
            WatchlistTarget(
                id=int(row["id"]),
                award_id=_clean(row.get("award_id")),
                contract_name=contract_name,
                agency=_clean(row.get("agency")),
                contracting_office=contracting_office,
                location_city=_clean(row.get("location_city")),
                location_state=_clean(row.get("location_state")),
                location_zip=_clean(row.get("location_zip")),
                naics_code=_clean(row.get("naics_code")),
                incumbent_name=_clean(row.get("incumbent_name")),
                award_amount=award_amount,
                estimated_annual_value=estimated_annual,
                title_keywords=keywords,
                priority=_clean(row.get("priority")),
                status=_clean(row.get("status")),
            )
        )
    return tuple(targets)


def _clean(value: Any) -> str | None:
    text_val = str(value or "").strip()
    return text_val or None


def target_by_id(target_id: int) -> WatchlistTarget | None:
    for target in load_watching_targets():
        if target.id == target_id:
            return target
    return None


def target_from_meta(meta: dict[str, Any]) -> WatchlistTarget | None:
    watchlist_id = meta.get("matched_watchlist_id") or meta.get("watchlist_id")
    if watchlist_id is None:
        return None
    live = target_by_id(int(watchlist_id))
    if live:
        return live
    keywords = meta.get("title_keywords") or []
    return WatchlistTarget(
        id=int(watchlist_id),
        award_id=meta.get("award_id"),
        contract_name=meta.get("contract_name"),
        agency=meta.get("agency"),
        contracting_office=meta.get("contracting_office") or meta.get("agency"),
        location_city=meta.get("location_city"),
        location_state=meta.get("location_state"),
        location_zip=meta.get("location_zip"),
        naics_code=meta.get("naics_code"),
        incumbent_name=meta.get("incumbent_name"),
        award_amount=meta.get("award_amount"),
        estimated_annual_value=meta.get("estimated_annual_value") or meta.get("award_amount"),
        title_keywords=tuple(keywords),
        priority=meta.get("priority"),
        status=meta.get("status_on_watchlist") or meta.get("status"),
    )


def fingerprint_meta(contract: Any) -> dict[str, Any] | None:
    analysis = contract.analysis if isinstance(getattr(contract, "analysis", None), dict) else {}
    meta = analysis.get("govspend_watchlist")
    return meta if isinstance(meta, dict) else None


def govspend_watchlist_meta(contract: Any) -> dict[str, Any] | None:
    meta = fingerprint_meta(contract)
    if not meta:
        return None
    if meta.get("match_rejected"):
        return None
    confidence = meta.get("match_confidence")
    if confidence == "Weak":
        return None
    if confidence in ("High", "Possible"):
        return meta
    if meta.get("sam_found"):
        return meta
    return None


def is_watchlist_rejected(contract: Any) -> bool:
    meta = fingerprint_meta(contract) or {}
    return bool(meta.get("match_rejected"))


def is_govspend_watchlist_hit(contract: Any) -> bool:
    meta = fingerprint_meta(contract)
    if not meta or meta.get("match_rejected"):
        return False
    if meta.get("match_confirmed"):
        return True
    confidence = meta.get("match_confidence")
    if confidence == "High":
        return True
    if meta.get("sam_found") and confidence not in ("Possible", "Weak", "None"):
        return True
    return False


def is_possible_watchlist_match(contract: Any) -> bool:
    meta = fingerprint_meta(contract)
    if not meta or meta.get("match_rejected") or meta.get("match_confirmed"):
        return False
    return meta.get("match_confidence") == "Possible"


def is_weak_watchlist_match(contract: Any) -> bool:
    meta = fingerprint_meta(contract)
    if not meta or meta.get("match_rejected"):
        return False
    return meta.get("match_confidence") == "Weak"


def score_contract_against_target(contract: Any, target: WatchlistTarget) -> FingerprintMatchResult:
    posting = posting_fingerprint_from_contract(contract)
    return score_fingerprint_match(posting, target)


def score_opportunity_against_target(opp: dict[str, Any], target: WatchlistTarget) -> FingerprintMatchResult:
    posting = posting_fingerprint_from_opportunity(opp)
    return score_fingerprint_match(posting, target)


def best_contract_fingerprint(
    contract: Any,
    targets: list[WatchlistTarget] | tuple[WatchlistTarget, ...],
) -> FingerprintMatchResult | None:
    posting = posting_fingerprint_from_contract(contract)
    return best_fingerprint_match(posting, targets)


def best_opportunity_fingerprint(
    opp: dict[str, Any],
    targets: list[WatchlistTarget] | tuple[WatchlistTarget, ...],
) -> tuple[WatchlistTarget | None, FingerprintMatchResult | None]:
    posting = posting_fingerprint_from_opportunity(opp)
    best_target: WatchlistTarget | None = None
    best_result: FingerprintMatchResult | None = None
    for target in targets:
        result = score_fingerprint_match(posting, target)
        if result.confidence == "None":
            continue
        if best_result is None or result.score > best_result.score:
            best_result = result
            best_target = target
    return best_target, best_result


def _target_display(target: WatchlistTarget) -> dict[str, Any]:
    return {
        "watchlist_id": target.id,
        "award_id": target.award_id,
        "contract_name": target.contract_name,
        "contracting_office": target.contracting_office or target.agency,
        "agency": target.agency,
        "location_city": target.location_city,
        "location_state": target.location_state,
        "location_zip": target.location_zip,
        "location": ", ".join(
            filter(None, [target.location_city, target.location_state, target.location_zip])
        ),
        "naics_code": target.naics_code,
        "incumbent_name": target.incumbent_name,
        "estimated_annual_value": target.estimated_annual_value or target.award_amount,
        "award_amount": target.award_amount,
        "title_keywords": list(target.title_keywords),
        "priority": target.priority,
        "status": target.status,
    }


def _posting_display(contract: Any, posting: PostingFingerprint) -> dict[str, Any]:
    due = getattr(contract, "due_date", None)
    return {
        "notice_id": getattr(contract, "notice_id", None),
        "title": getattr(contract, "title", None),
        "contracting_office": posting.contracting_office,
        "agency": getattr(contract, "agency", None),
        "location": posting.location,
        "location_zip": posting.location_zip,
        "naics_code": getattr(contract, "naics_code", None),
        "estimated_annual_value": posting.estimated_annual_value,
        "estimated_value": getattr(contract, "estimated_value", None),
        "due_date": due.isoformat() if due else None,
        "link": getattr(contract, "link", None),
    }


def stamp_fingerprint_match(
    contract: Any,
    target: WatchlistTarget,
    result: FingerprintMatchResult,
    *,
    posting: PostingFingerprint | None = None,
) -> dict[str, Any]:
    """Persist fingerprint match on contract — GovTracker never writes gs_watchlist."""
    analysis = dict(contract.analysis) if isinstance(contract.analysis, dict) else {}
    prev = dict(analysis.get("govspend_watchlist") or {})
    if posting is None:
        posting = posting_fingerprint_from_contract(contract)

    confidence = result.confidence
    sam_found = confidence == "High" or bool(prev.get("match_confirmed"))
    meta = {
        "watchlist_id": target.id,
        "matched_watchlist_id": target.id,
        "award_id": target.award_id,
        "contract_name": target.contract_name,
        "contracting_office": target.contracting_office or target.agency,
        "priority": target.priority,
        "status_on_watchlist": target.status,
        "match_score": result.score,
        "match_confidence": confidence,
        "match_signals": result.to_match_signals_json(),
        "match_fields": result.matched_signals,
        "match_count": len(result.matched_signals),
        "title_keywords": list(result.title_keywords),
        "award_amount": target.award_amount,
        "estimated_annual_value": target.estimated_annual_value or target.award_amount,
        "incumbent_name": target.incumbent_name,
        "agency": target.agency,
        "location_city": target.location_city,
        "location_state": target.location_state,
        "location_zip": target.location_zip,
        "matched_at": now_utc().isoformat(),
        "sam_found": sam_found,
        "match_confirmed": bool(prev.get("match_confirmed")),
        "match_rejected": bool(prev.get("match_rejected")),
        "needs_review": confidence == "Possible" and not prev.get("match_confirmed"),
        "surfaced": should_surface_match(confidence, confirmed=bool(prev.get("match_confirmed"))),
        "watchlist_target_display": _target_display(target),
        "sam_posting_display": _posting_display(contract, posting),
        "pipeline_status": prev.get("pipeline_status", "queued"),
        "govspend_notified_at": prev.get("govspend_notified_at"),
        "govspend_notify_ok": prev.get("govspend_notify_ok"),
        "govspend_notify_error": prev.get("govspend_notify_error"),
    }
    analysis["govspend_watchlist"] = meta
    contract.analysis = analysis
    return meta


def confirm_fingerprint_match(contract: Any) -> dict[str, Any] | None:
    meta = fingerprint_meta(contract)
    if not meta or meta.get("match_confidence") != "Possible":
        return None
    analysis = dict(contract.analysis) if isinstance(contract.analysis, dict) else {}
    updated = dict(meta)
    updated["match_confirmed"] = True
    updated["match_rejected"] = False
    updated["needs_review"] = False
    updated["sam_found"] = True
    updated["surfaced"] = True
    updated["confirmed_at"] = now_utc().isoformat()
    analysis["govspend_watchlist"] = updated
    contract.analysis = analysis
    return updated


def reject_fingerprint_match(contract: Any) -> dict[str, Any] | None:
    meta = fingerprint_meta(contract)
    if not meta:
        return None
    analysis = dict(contract.analysis) if isinstance(contract.analysis, dict) else {}
    updated = dict(meta)
    updated["match_confirmed"] = False
    updated["match_rejected"] = True
    updated["needs_review"] = False
    updated["sam_found"] = False
    updated["surfaced"] = False
    updated["rejected_at"] = now_utc().isoformat()
    analysis["govspend_watchlist"] = updated
    contract.analysis = analysis
    return updated


def should_run_pipeline_for_contract(contract: Any) -> bool:
    meta = fingerprint_meta(contract) or {}
    return should_trigger_pipeline(
        str(meta.get("match_confidence") or ""),
        confirmed=bool(meta.get("match_confirmed")),
    )


def should_notify_for_contract(contract: Any) -> bool:
    meta = fingerprint_meta(contract) or {}
    return should_notify_govspend(
        str(meta.get("match_confidence") or ""),
        confirmed=bool(meta.get("match_confirmed")),
    )


# Backward-compatible alias
def stamp_govspend_watchlist_hit(
    contract: Any,
    target: WatchlistTarget,
    *,
    match_fields: list[str] | None = None,
    match_count: int | None = None,
    result: FingerprintMatchResult | None = None,
) -> dict[str, Any]:
    if result is None:
        result = score_contract_against_target(contract, target)
    return stamp_fingerprint_match(contract, target, result)


def watchlist_status() -> dict[str, Any]:
    table = _resolved_table_cached()
    targets = load_watching_targets()
    return {
        "table": table,
        "monitors_all_rows": True,
        "note": "GovTracker reads every gs_watchlist row. GovSpend UI filters are display-only.",
        "fingerprint_max_score": 11,
        "confidence_tiers": {
            "high": "8+ points — auto pipeline + GovSpend notify",
            "possible": "5-7 points — manual review",
            "weak": "3-4 points — log only",
        },
        "watchlist_target_count": len(targets),
        "targets": [
            {
                "id": t.id,
                "award_id": t.award_id,
                "contract_name": t.contract_name,
                "contracting_office": t.contracting_office or t.agency,
                "agency": t.agency,
                "naics_code": t.naics_code,
                "location": ", ".join(filter(None, [t.location_city, t.location_state, t.location_zip])),
                "incumbent_name": t.incumbent_name,
                "estimated_annual_value": t.estimated_annual_value or t.award_amount,
                "title_keywords": list(t.title_keywords),
                "priority": t.priority,
                "status": t.status,
            }
            for t in targets[:100]
        ],
    }


def load_priority_targets() -> tuple[WatchlistTarget, ...]:
    return load_watching_targets()


def match_contract_to_targets(
    contract: Any,
    targets: list[WatchlistTarget] | tuple[WatchlistTarget, ...],
) -> tuple[bool, str | None, str | None]:
    if is_govspend_watchlist_hit(contract):
        meta = govspend_watchlist_meta(contract) or {}
        return True, meta.get("priority"), ",".join(meta.get("match_fields") or [])

    if is_possible_watchlist_match(contract):
        meta = govspend_watchlist_meta(contract) or {}
        return True, meta.get("priority"), ",".join(meta.get("match_fields") or [])

    result = best_contract_fingerprint(contract, targets)
    if not result:
        return False, None, None
    for target in targets:
        if target.id == result.watchlist_id:
            if should_surface_match(result.confidence):
                return True, target.priority, ",".join(result.matched_signals)
            break
    return False, None, None


def is_watchlist_field_match(count: int) -> bool:
    """Deprecated — kept for callers passing legacy field counts."""
    return count >= 3
