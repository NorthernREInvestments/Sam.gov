"""SAM_DAILY_CREDIT_MANAGER — wraps canonical budgeted client; never exceeds 10/day."""

from __future__ import annotations

import json
from typing import Any

from application_clock import now_utc
from discovery.sam_budgeted_client import (
    can_afford_live_calls,
    dashboard,
    load_cached_response,
    query_fingerprint,
    reserve_calls,
    sam_daily_call_budget,
    save_cached_response,
    search_opportunities,
    unified_calls_used_today,
)
from m3_data_root import data_path
from package_recovery_sam_budget.models import BUILD, SAM_DAILY_MAX, SAM_RESERVE

_MANAGER_STATE = "SAM_DAILY_CREDIT_MANAGER.json"


def _save(name: str, payload: Any) -> None:
    data_path(name).write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _load(name: str) -> dict[str, Any]:
    p = data_path(name)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


class SamDailyCreditManager:
    """Hard max 10/day; reserve ≥1; cache-first; fingerprint dedupe."""

    def __init__(self) -> None:
        self.state = _load(_MANAGER_STATE)
        dash = dashboard()
        day = dash.get("date")
        if self.state.get("date") != day:
            self.state = {
                "kind": "SAM_DAILY_CREDIT_MANAGER",
                "build": BUILD,
                "date": day,
                "calls_used": dash.get("calls_used", 0),
                "calls_remaining": dash.get("calls_remaining", SAM_DAILY_MAX),
                "cache_hits": 0,
                "duplicate_calls_prevented": 0,
                "calls_reserved": SAM_RESERVE,
                "calls_failed": 0,
                "calls_successful": 0,
                "fingerprints_seen": [],
                "updated_at": now_utc().isoformat(),
            }
        else:
            # Sync with live ledger
            self.state["calls_used"] = unified_calls_used_today()
            self.state["calls_remaining"] = max(0, sam_daily_call_budget() - self.state["calls_used"])
        self._persist()

    def _persist(self) -> None:
        self.state["updated_at"] = now_utc().isoformat()
        self.state["calls_used"] = unified_calls_used_today()
        self.state["calls_remaining"] = max(0, sam_daily_call_budget() - self.state["calls_used"])
        self.state["calls_reserved"] = reserve_calls()
        _save(_MANAGER_STATE, self.state)

    def snapshot(self) -> dict[str, Any]:
        self._persist()
        dash = dashboard()
        return {
            "date": self.state.get("date"),
            "daily_max": sam_daily_call_budget(),
            "calls_used": self.state.get("calls_used"),
            "calls_remaining": self.state.get("calls_remaining"),
            "calls_reserved": self.state.get("calls_reserved"),
            "cache_hits": self.state.get("cache_hits", 0),
            "duplicate_calls_prevented": self.state.get("duplicate_calls_prevented", 0),
            "calls_failed": self.state.get("calls_failed", 0),
            "calls_successful": self.state.get("calls_successful", 0),
            "production_budget": max(0, (self.state.get("calls_remaining") or 0) - (self.state.get("calls_reserved") or 0)),
            "display": dash.get("display"),
            "MUST_duplicate_waste": 0,
        }

    def can_spend_automated(self, credits: int = 1) -> bool:
        """Automated queue may spend remaining minus reserve."""
        rem = max(0, sam_daily_call_budget() - unified_calls_used_today())
        prod = rem - reserve_calls()
        return prod >= credits and can_afford_live_calls(credits)

    def search_cached_or_live(
        self,
        params: dict[str, Any],
        *,
        reason: str,
        allow_live: bool = True,
        use_reserve: bool = False,
    ) -> dict[str, Any]:
        endpoint = "https://api.sam.gov/opportunities/v2/search"
        fp = query_fingerprint(endpoint, params)
        seen = set(self.state.get("fingerprints_seen") or [])
        if fp in seen:
            self.state["duplicate_calls_prevented"] = int(self.state.get("duplicate_calls_prevented") or 0) + 1
            cached = load_cached_response(fp)
            self._persist()
            return {
                "ok": bool(cached),
                "cache_hit": True,
                "duplicate_prevented": True,
                "fingerprint": fp,
                "payload": cached,
                "credits_spent": 0,
            }

        cached = load_cached_response(fp)
        if cached:
            self.state["cache_hits"] = int(self.state.get("cache_hits") or 0) + 1
            seen.add(fp)
            self.state["fingerprints_seen"] = list(seen)[-500:]
            self._persist()
            return {
                "ok": True,
                "cache_hit": True,
                "duplicate_prevented": False,
                "fingerprint": fp,
                "payload": cached,
                "credits_spent": 0,
            }

        if not allow_live:
            return {
                "ok": False,
                "cache_hit": False,
                "reason": "PACKAGE_API_CREDIT_REQUIRED",
                "fingerprint": fp,
                "credits_spent": 0,
            }

        if use_reserve:
            afford = can_afford_live_calls(1)
        else:
            afford = self.can_spend_automated(1)
        if not afford:
            return {
                "ok": False,
                "cache_hit": False,
                "reason": "PACKAGE_API_CREDIT_EXHAUSTED",
                "fingerprint": fp,
                "credits_spent": 0,
            }

        try:
            result = search_opportunities(
                params,
                reason=reason,
                authorize_live=True,
                use_reserve=use_reserve,
            )
            seen.add(fp)
            self.state["fingerprints_seen"] = list(seen)[-500:]
            if result.get("ok") or result.get("opportunitiesData") is not None or result.get("data"):
                self.state["calls_successful"] = int(self.state.get("calls_successful") or 0) + 1
            else:
                self.state["calls_failed"] = int(self.state.get("calls_failed") or 0) + 1
            # Persist raw if client didn't
            if result and not load_cached_response(fp):
                try:
                    save_cached_response(fp, result)
                except Exception:
                    pass
            self._persist()
            return {
                "ok": True,
                "cache_hit": False,
                "fingerprint": fp,
                "payload": result,
                "credits_spent": 1,
            }
        except Exception as exc:
            self.state["calls_failed"] = int(self.state.get("calls_failed") or 0) + 1
            self._persist()
            return {
                "ok": False,
                "cache_hit": False,
                "reason": "PACKAGE_API_CALL_FAILED",
                "error": str(exc),
                "fingerprint": fp,
                "credits_spent": 0,
            }
