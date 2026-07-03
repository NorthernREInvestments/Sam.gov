"""Notify GovSpend when a gs_watchlist target is matched on SAM.gov (read-only on gs_watchlist)."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx

from gs_watchlist_service import WatchlistTarget, govspend_watchlist_meta

logger = logging.getLogger("govtracker.govspend")

GOVSPEND_FOUND_STATUS = "Found on SAM"
NOTIFY_TIMEOUT_SECONDS = float(os.getenv("GOVSPEND_API_TIMEOUT", "30"))


def govspend_api_url() -> str | None:
    url = (os.getenv("GOVSPEND_API_URL") or "").strip()
    return url or None


def govspend_api_configured() -> bool:
    return bool(govspend_api_url())


def _auth_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    api_key = (os.getenv("GOVSPEND_API_KEY") or "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def build_watchlist_hit_payload(
    contract: Any,
    target: WatchlistTarget,
    *,
    match_fields: list[str],
    match_count: int,
    match_score: int | None = None,
    match_confidence: str | None = None,
    match_signals: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    due = getattr(contract, "due_date", None)
    analysis = contract.analysis if isinstance(getattr(contract, "analysis", None), dict) else {}
    meta = analysis.get("govspend_watchlist") if isinstance(analysis.get("govspend_watchlist"), dict) else {}
    return {
        "event": "watchlist_sam_match",
        "suggested_status": GOVSPEND_FOUND_STATUS,
        "watchlist_id": target.id,
        "matched_watchlist_id": target.id,
        "match_count": match_count,
        "match_fields": match_fields,
        "match_score": match_score if match_score is not None else meta.get("match_score"),
        "match_confidence": match_confidence or meta.get("match_confidence"),
        "match_signals": match_signals if match_signals is not None else meta.get("match_signals"),
        "match_confirmed": bool(meta.get("match_confirmed")),
        "matched_at": datetime.now(timezone.utc).isoformat(),
        "watchlist_target": {
            "id": target.id,
            "award_id": target.award_id,
            "contract_name": target.contract_name,
            "contracting_office": target.contracting_office or target.agency,
            "agency": target.agency,
            "location_city": target.location_city,
            "location_state": target.location_state,
            "location_zip": target.location_zip,
            "naics_code": target.naics_code,
            "incumbent_name": target.incumbent_name,
            "award_amount": target.award_amount,
            "estimated_annual_value": target.estimated_annual_value or target.award_amount,
            "title_keywords": list(target.title_keywords),
            "priority": target.priority,
            "status": target.status,
        },
        "sam_contract": {
            "notice_id": getattr(contract, "notice_id", None),
            "title": getattr(contract, "title", None),
            "agency": getattr(contract, "agency", None),
            "location": getattr(contract, "location", None),
            "naics_code": getattr(contract, "naics_code", None),
            "due_date": due.isoformat() if due else None,
            "link": getattr(contract, "link", None),
            "estimated_value": getattr(contract, "estimated_value", None),
        },
    }


def notify_govspend_watchlist_hit(
    contract: Any,
    target: WatchlistTarget,
    *,
    match_fields: list[str],
    match_count: int,
    match_score: int | None = None,
    match_confidence: str | None = None,
    match_signals: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    POST match details to GovSpend so it can update gs_watchlist status.
    Updates contract.analysis.govspend_watchlist notification fields.
    """
    url = govspend_api_url()
    meta = govspend_watchlist_meta(contract) or {}
    if meta.get("govspend_notified_at"):
        return {"skipped": True, "reason": "already_notified"}

    if not url:
        return {"skipped": True, "reason": "GOVSPEND_API_URL not configured"}

    payload = build_watchlist_hit_payload(
        contract,
        target,
        match_fields=match_fields,
        match_count=match_count,
        match_score=match_score,
        match_confidence=match_confidence,
        match_signals=match_signals,
    )

    try:
        with httpx.Client(timeout=NOTIFY_TIMEOUT_SECONDS) as client:
            response = client.post(url, json=payload, headers=_auth_headers())
            response.raise_for_status()
            body: Any = None
            if response.content:
                try:
                    body = response.json()
                except Exception:
                    body = response.text[:500]
    except Exception as exc:
        logger.warning(
            "GovSpend notify failed for watchlist_id=%s notice_id=%s: %s",
            target.id,
            getattr(contract, "notice_id", None),
            exc,
        )
        _store_notify_result(
            contract,
            ok=False,
            error=str(exc),
        )
        return {"ok": False, "error": str(exc)}

    _store_notify_result(contract, ok=True, response=body)
    logger.info(
        "GovSpend notified watchlist_id=%s notice_id=%s status=%s",
        target.id,
        getattr(contract, "notice_id", None),
        GOVSPEND_FOUND_STATUS,
    )
    return {"ok": True, "response": body}


def _store_notify_result(
    contract: Any,
    *,
    ok: bool,
    response: Any = None,
    error: str | None = None,
) -> None:
    analysis = dict(contract.analysis) if isinstance(contract.analysis, dict) else {}
    meta = dict(analysis.get("govspend_watchlist") or {})
    if ok:
        meta["govspend_notified_at"] = datetime.now(timezone.utc).isoformat()
        meta["govspend_notify_ok"] = True
        meta.pop("govspend_notify_error", None)
        if response is not None:
            meta["govspend_notify_response"] = response
    else:
        meta["govspend_notify_ok"] = False
        meta["govspend_notify_error"] = error
    analysis["govspend_watchlist"] = meta
    contract.analysis = analysis


def retry_pending_govspend_notifications(session) -> dict[str, int]:
    """Retry POST for hits that were never successfully acknowledged by GovSpend."""
    from gs_watchlist_service import is_govspend_watchlist_hit
    from models import Contract

    if not govspend_api_configured():
        return {"attempted": 0, "sent": 0, "failed": 0, "skipped": 0}

    stats = {"attempted": 0, "sent": 0, "failed": 0, "skipped": 0}
    rows = session.query(Contract).all()
    for row in rows:
        if not is_govspend_watchlist_hit(row):
            continue
        meta = govspend_watchlist_meta(row) or {}
        if meta.get("govspend_notified_at"):
            stats["skipped"] += 1
            continue
        from gs_watchlist_service import should_notify_for_contract

        if not should_notify_for_contract(row):
            stats["skipped"] += 1
            continue
        watchlist_id = meta.get("watchlist_id")
        if not watchlist_id:
            continue
        target = WatchlistTarget(
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
            title_keywords=tuple(meta.get("title_keywords") or []),
            priority=meta.get("priority"),
            status=meta.get("status_on_watchlist"),
        )
        stats["attempted"] += 1
        result = notify_govspend_watchlist_hit(
            row,
            target,
            match_fields=list(meta.get("match_fields") or []),
            match_count=int(meta.get("match_count") or 0),
            match_score=int(meta.get("match_score") or 0) if meta.get("match_score") is not None else None,
            match_confidence=meta.get("match_confidence"),
            match_signals=list(meta.get("match_signals") or []),
        )
        if result.get("ok"):
            stats["sent"] += 1
        elif result.get("skipped"):
            stats["skipped"] += 1
        else:
            stats["failed"] += 1
    return stats
