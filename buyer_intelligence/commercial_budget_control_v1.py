"""Central commercial budget control — OpenAI $1/day + $30/month, SAM 10/day.

Fail-closed, cross-process file-lock reservations on durable M3 data root.
No module may bypass these gates for paid commercial research.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterator

from application_clock import now_utc
from m3_data_root import data_path

BUILD = "20261012-m3-commercial-budget-control-v1"

# Hard policy defaults (env may only LOWER, never raise above these)
HARD_OPENAI_DAILY_USD = Decimal("1.00")
HARD_OPENAI_MONTHLY_USD = Decimal("30.00")
HARD_SAM_DAILY = 10
HARD_OTHER_PAID_USD = Decimal("0.00")

LEDGER_REL = "_og_commercial_research_pipeline_out/m3_commercial_budget_ledger_v1.json"
LOCK_REL = "_og_commercial_research_pipeline_out/.m3_commercial_budget_v1.lock"
PAUSE_REL = "_og_commercial_research_pipeline_out/m3_commercial_budget_pause_v1.json"

_THREAD = threading.RLock()


class BudgetDenied(Exception):
    def __init__(self, reason: str, *, detail: dict[str, Any] | None = None):
        self.reason = reason
        self.detail = detail or {}
        super().__init__(reason)


def budget_timezone() -> str:
    return (os.getenv("COMMERCIAL_BUDGET_TIMEZONE") or "America/Chicago").strip() or "America/Chicago"


def budget_day_key(at: datetime | None = None) -> str:
    """Operational day in America/Chicago (configurable)."""
    ts = at or now_utc()
    try:
        from zoneinfo import ZoneInfo

        return ts.astimezone(ZoneInfo(budget_timezone())).date().isoformat()
    except Exception:
        return ts.date().isoformat()


def budget_month_key(at: datetime | None = None) -> str:
    return budget_day_key(at)[:7]


def openai_daily_cap() -> Decimal:
    raw = os.getenv("OPENAI_DAILY_USD_CAP") or os.getenv("AI_DAILY_DOLLAR_HARD_LIMIT")
    if raw is not None and str(raw).strip() != "":
        try:
            return min(HARD_OPENAI_DAILY_USD, Decimal(str(raw).strip()))
        except Exception:
            pass
    return HARD_OPENAI_DAILY_USD


def openai_monthly_cap() -> Decimal:
    raw = os.getenv("OPENAI_MONTHLY_USD_CAP") or os.getenv("AI_MONTHLY_DOLLAR_BUDGET")
    if raw is not None and str(raw).strip() != "":
        try:
            return min(HARD_OPENAI_MONTHLY_USD, Decimal(str(raw).strip()))
        except Exception:
            pass
    return HARD_OPENAI_MONTHLY_USD


def sam_daily_cap() -> int:
    try:
        from discovery.sam_budgeted_client import sam_daily_call_budget

        return min(HARD_SAM_DAILY, int(sam_daily_call_budget()))
    except Exception:
        return HARD_SAM_DAILY


def _ledger_path() -> Path:
    return Path(data_path(LEDGER_REL))


def _lock_path() -> Path:
    return Path(data_path(LOCK_REL))


def _pause_path() -> Path:
    return Path(data_path(PAUSE_REL))


def _money(x: Decimal) -> str:
    return str(x.quantize(Decimal("0.0001")))


@contextmanager
def _file_lock(timeout_s: float = 20.0) -> Iterator[None]:
    path = _lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout_s
    fh = None
    while True:
        try:
            fh = open(path, "a+b")
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if fh:
                try:
                    fh.close()
                except Exception:
                    pass
                fh = None
            if time.time() >= deadline:
                raise BudgetDenied("BUDGET_LOCK_TIMEOUT", detail={"fail_closed": True})
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            fh.close()
        except Exception:
            pass


def _empty_ledger() -> dict[str, Any]:
    return {
        "kind": "commercial_budget_ledger_v1",
        "build": BUILD,
        "openai_commits": [],
        "openai_reservations": {},
        "sam_commits": [],
        "sam_reservations": {},
        "other_paid_commits": [],
        "updated_at": now_utc().isoformat(),
    }


def _load_ledger_unlocked() -> dict[str, Any]:
    p = _ledger_path()
    if not p.exists():
        return _empty_ledger()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return _empty_ledger()
        data.setdefault("openai_commits", [])
        data.setdefault("openai_reservations", {})
        data.setdefault("sam_commits", [])
        data.setdefault("sam_reservations", {})
        data.setdefault("other_paid_commits", [])
        return data
    except Exception:
        # Fail closed on corrupt ledger
        raise BudgetDenied("LEDGER_UNREADABLE", detail={"fail_closed": True})


def _save_ledger_unlocked(ledger: dict[str, Any]) -> None:
    p = _ledger_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    ledger["updated_at"] = now_utc().isoformat()
    ledger["build"] = BUILD
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(ledger, indent=2, default=str), encoding="utf-8")
    tmp.replace(p)


def shared_ledger_safe() -> dict[str, Any]:
    """Detect whether multi-instance accounting can be guaranteed.

    File locks only work across processes that share the same filesystem.
    If Railway runs multiple replicas without a shared volume, claim fail-closed
    when M3_BUDGET_REQUIRE_SHARED_LEDGER=1.
    """
    require = os.getenv("M3_BUDGET_REQUIRE_SHARED_LEDGER", "").strip().lower() in {"1", "true", "yes"}
    replicas = (os.getenv("RAILWAY_REPLICA_COUNT") or os.getenv("M3_REPLICA_COUNT") or "1").strip()
    try:
        n = int(replicas)
    except Exception:
        n = 1
    shared_mount = (os.getenv("M3_DATA_ROOT") or os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or "").strip()
    multi = n > 1
    safe = (not multi) or bool(shared_mount)
    return {
        "replica_count": n,
        "shared_mount_configured": bool(shared_mount),
        "cross_instance_safe": safe,
        "require_shared": require,
        "fail_closed_if_unsafe": require and not safe,
    }


def is_paid_research_paused() -> bool:
    # Env kill-switch (Railway): keep paid research paused until smoke clears
    if (os.getenv("M3_PAID_RESEARCH_PAUSED") or "").strip().lower() in {"1", "true", "yes", "on"}:
        return True
    p = _pause_path()
    if not p.exists():
        # Also honor cost_governor config
        try:
            from budget_config import BudgetConfigStore

            if BudgetConfigStore().get().get("paid_research_paused"):
                return True
        except Exception:
            pass
        return False
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return bool(data.get("paused"))
    except Exception:
        return True  # fail closed


def pause_paid_research(*, reason: str = "operator", by: str = "system") -> dict[str, Any]:
    payload = {
        "paused": True,
        "reason": reason,
        "by": by,
        "at": now_utc().isoformat(),
        "build": BUILD,
    }
    path = _pause_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    try:
        from budget_config import BudgetConfigStore

        BudgetConfigStore().update_limits(
            {"paid_research_paused": True},
            operator_id="policy_hard_limit_install",
            reason=reason,
        )
    except Exception:
        pass
    return payload


def resume_within_budget(*, by: str = "operator", reason: str = "resume") -> dict[str, Any]:
    """Clear pause switch — does NOT raise caps; next request still checks remaining."""
    payload = {
        "paused": False,
        "reason": reason,
        "by": by,
        "at": now_utc().isoformat(),
        "build": BUILD,
    }
    path = _pause_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    try:
        from budget_config import BudgetConfigStore

        BudgetConfigStore().update_limits(
            {"paid_research_paused": False},
            operator_id="policy_hard_limit_install",
            reason=reason,
        )
    except Exception:
        pass
    return payload


def apply_hard_policy_caps() -> dict[str, Any]:
    """Install $1/day and $30/month into BudgetConfigStore (may only lower existing)."""
    from budget_config import BudgetConfigStore

    store = BudgetConfigStore()
    cur = store.get()
    updates = {
        "DAILY_CAP": float(min(Decimal(str(cur.get("DAILY_CAP") or 999)), openai_daily_cap())),
        "MONTHLY_CAP": float(min(Decimal(str(cur.get("MONTHLY_CAP") or 999)), openai_monthly_cap())),
        "ABSOLUTE_AUTONOMOUS_SPEND_CAP": float(
            min(Decimal(str(cur.get("ABSOLUTE_AUTONOMOUS_SPEND_CAP") or 999)), openai_monthly_cap())
        ),
        "commercial_openai_daily_hard": float(openai_daily_cap()),
        "commercial_openai_monthly_hard": float(openai_monthly_cap()),
        "commercial_sam_daily_hard": sam_daily_cap(),
        "other_paid_research_usd": 0.0,
    }
    return store.update_limits(updates, operator_id="policy_hard_limit_install", reason="m3_hard_budget_policy_v1")


def _sum_openai(ledger: dict[str, Any], *, day: str | None = None, month: str | None = None) -> tuple[Decimal, Decimal]:
    day = day or budget_day_key()
    month = month or budget_month_key()
    daily = Decimal("0")
    monthly = Decimal("0")
    for e in ledger.get("openai_commits") or []:
        if e.get("status") not in {"COMMITTED", "UNRECONCILED_HELD"}:
            continue
        amt = Decimal(str(e.get("amount_usd") or 0))
        d = str(e.get("budget_day") or "")
        if d.startswith(month):
            monthly += amt
        if d == day:
            daily += amt
    reserved_d = Decimal("0")
    reserved_m = Decimal("0")
    for r in (ledger.get("openai_reservations") or {}).values():
        amt = Decimal(str(r.get("amount_usd") or 0))
        d = str(r.get("budget_day") or "")
        if d == day:
            reserved_d += amt
        if d.startswith(month):
            reserved_m += amt
    return daily + reserved_d, monthly + reserved_m


def _sam_used(ledger: dict[str, Any], *, day: str | None = None) -> tuple[int, int]:
    day = day or budget_day_key()
    used = 0
    for e in ledger.get("sam_commits") or []:
        if str(e.get("budget_day")) == day and e.get("status") in {"COMMITTED", "UNRECONCILED_HELD"}:
            used += int(e.get("calls") or 0)
    reserved = 0
    for r in (ledger.get("sam_reservations") or {}).values():
        if str(r.get("budget_day")) == day:
            reserved += int(r.get("calls") or 0)
    return used, reserved


def get_daily_usage() -> dict[str, Any]:
    with _file_lock():
        ledger = _load_ledger_unlocked()
        day = budget_day_key()
        d, _ = _sum_openai(ledger, day=day)
        # Split committed vs reserved for reporting
        committed = Decimal("0")
        for e in ledger.get("openai_commits") or []:
            if str(e.get("budget_day")) == day and e.get("status") in {"COMMITTED", "UNRECONCILED_HELD"}:
                committed += Decimal(str(e.get("amount_usd") or 0))
        reserved = Decimal("0")
        for r in (ledger.get("openai_reservations") or {}).values():
            if str(r.get("budget_day")) == day:
                reserved += Decimal(str(r.get("amount_usd") or 0))
        sam_used, sam_res = _sam_used(ledger, day=day)
        # Prefer unified SAM ledger if available
        try:
            from discovery.sam_budgeted_client import unified_calls_used_today

            sam_used = max(sam_used, int(unified_calls_used_today()))
        except Exception:
            pass
        cap = openai_daily_cap()
        return {
            "budget_day": day,
            "timezone": budget_timezone(),
            "openai_committed_usd": _money(committed),
            "openai_reserved_usd": _money(reserved),
            "openai_total_authorized_usd": _money(committed + reserved),
            "openai_daily_cap_usd": _money(cap),
            "openai_remaining_usd": _money(max(Decimal("0"), cap - committed - reserved)),
            "sam_calls_committed": sam_used,
            "sam_calls_reserved": sam_res,
            "sam_daily_cap": sam_daily_cap(),
            "sam_remaining": max(0, sam_daily_cap() - sam_used - sam_res),
            "paid_research_paused": is_paid_research_paused(),
            "other_paid_usd": "0.0000",
        }


def get_monthly_usage() -> dict[str, Any]:
    with _file_lock():
        ledger = _load_ledger_unlocked()
        month = budget_month_key()
        _, m = _sum_openai(ledger, month=month)
        committed = Decimal("0")
        for e in ledger.get("openai_commits") or []:
            if str(e.get("budget_day") or "").startswith(month) and e.get("status") in {
                "COMMITTED",
                "UNRECONCILED_HELD",
            }:
                committed += Decimal(str(e.get("amount_usd") or 0))
        reserved = Decimal("0")
        for r in (ledger.get("openai_reservations") or {}).values():
            if str(r.get("budget_day") or "").startswith(month):
                reserved += Decimal(str(r.get("amount_usd") or 0))
        cap = openai_monthly_cap()
        return {
            "budget_month": month,
            "openai_committed_usd": _money(committed),
            "openai_reserved_usd": _money(reserved),
            "openai_total_authorized_usd": _money(committed + reserved),
            "openai_monthly_cap_usd": _money(cap),
            "openai_remaining_usd": _money(max(Decimal("0"), cap - committed - reserved)),
        }


def get_remaining_budget() -> dict[str, Any]:
    daily = get_daily_usage()
    monthly = get_monthly_usage()
    rem = min(
        Decimal(daily["openai_remaining_usd"]),
        Decimal(monthly["openai_remaining_usd"]),
    )
    return {
        **daily,
        "monthly": monthly,
        "openai_remaining_usd": _money(rem),
        "sam_calls_remaining": daily["sam_remaining"],
        "fail_closed": True,
    }


def get_sam_calls_remaining() -> int:
    return int(get_remaining_budget()["sam_calls_remaining"])


def estimate_openai_worst_case(
    *,
    model: str = "gpt-4o-mini",
    max_input_tokens: int = 2000,
    max_output_tokens: int = 500,
) -> Decimal:
    """Conservative estimate from known mini pricing; never treat unknown as zero."""
    # gpt-4o-mini ballpark; apply 1.5x variance
    pricing = {
        "gpt-4o-mini": (Decimal("0.15"), Decimal("0.60")),
        "gpt-4o": (Decimal("2.50"), Decimal("10.00")),
    }
    inp_m, out_m = pricing.get(model, (Decimal("5.00"), Decimal("15.00")))
    cost = (Decimal(max_input_tokens) * Decimal("1.5") / Decimal(1_000_000)) * inp_m
    cost += (Decimal(max_output_tokens) * Decimal("1.5") / Decimal(1_000_000)) * out_m
    # Floor so tiny calls still reserve something measurable
    return max(cost.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP), Decimal("0.0001"))


def check_budget(
    *,
    provider: str,
    estimated_usd: Decimal | float | None = None,
    sam_calls: int = 0,
    model: str | None = None,
) -> dict[str, Any]:
    if is_paid_research_paused() and provider.lower() in {"openai", "anthropic", "paid_search", "other_paid"}:
        return {"allowed": False, "reason": "PAID_RESEARCH_PAUSED", "remaining": get_remaining_budget()}
    if provider.lower() in {"other_paid", "proxy", "captcha", "subscription"}:
        return {"allowed": False, "reason": "OTHER_PAID_NOT_AUTHORIZED", "remaining": get_remaining_budget()}
    rem = get_remaining_budget()
    if provider.lower() == "sam":
        need = max(0, int(sam_calls))
        ok = need <= int(rem["sam_calls_remaining"])
        return {"allowed": ok, "reason": None if ok else "SAM_DAILY_EXHAUSTED", "remaining": rem, "need_calls": need}
    if provider.lower() == "openai":
        est = Decimal(str(estimated_usd)) if estimated_usd is not None else estimate_openai_worst_case(model=model or "gpt-4o-mini")
        ok = est <= Decimal(rem["openai_remaining_usd"])
        return {"allowed": ok, "reason": None if ok else "OPENAI_BUDGET_EXHAUSTED", "remaining": rem, "estimated_usd": _money(est)}
    if provider.lower() in {"free", "cache", "http_public"}:
        return {"allowed": True, "reason": None, "remaining": rem}
    return {"allowed": False, "reason": "UNKNOWN_PROVIDER_FAIL_CLOSED", "remaining": rem}


def reserve_budget(
    *,
    provider: str,
    estimated_usd: Decimal | float | None = None,
    sam_calls: int = 0,
    model: str | None = None,
    purpose: str = "commercial_research",
    opportunity_id: str | None = None,
    reservation_id: str | None = None,
) -> dict[str, Any]:
    """Atomically reserve budget. Fail closed if insufficient or paused."""
    rid = reservation_id or f"CBR-{uuid.uuid4().hex[:16]}"
    safety = shared_ledger_safe()
    if safety.get("fail_closed_if_unsafe") and provider.lower() not in {"free", "cache", "http_public"}:
        raise BudgetDenied(
            "MULTI_INSTANCE_LEDGER_UNSAFE",
            detail=safety,
        )
    with _THREAD:
        with _file_lock():
            if is_paid_research_paused() and provider.lower() not in {"free", "cache", "http_public"}:
                raise BudgetDenied("PAID_RESEARCH_PAUSED")
            ledger = _load_ledger_unlocked()
            day = budget_day_key()
            if rid in (ledger.get("openai_reservations") or {}) or rid in (ledger.get("sam_reservations") or {}):
                raise BudgetDenied("DUPLICATE_RESERVATION_ID", detail={"reservation_id": rid})

            if provider.lower() == "sam":
                need = max(0, int(sam_calls))
                used, reserved = _sam_used(ledger, day=day)
                try:
                    from discovery.sam_budgeted_client import unified_calls_used_today

                    used = max(used, int(unified_calls_used_today()))
                except Exception:
                    pass
                if used + reserved + need > sam_daily_cap():
                    raise BudgetDenied("SAM_DAILY_EXHAUSTED", detail={"used": used, "reserved": reserved, "need": need})
                ledger.setdefault("sam_reservations", {})[rid] = {
                    "reservation_id": rid,
                    "calls": need,
                    "budget_day": day,
                    "purpose": purpose,
                    "opportunity_id": opportunity_id,
                    "created_at": now_utc().isoformat(),
                    "status": "RESERVED",
                }
                _save_ledger_unlocked(ledger)
                return {"reservation_id": rid, "provider": "sam", "calls": need, "budget_day": day}

            if provider.lower() == "openai":
                est = (
                    Decimal(str(estimated_usd))
                    if estimated_usd is not None
                    else estimate_openai_worst_case(model=model or "gpt-4o-mini")
                )
                if est <= 0:
                    raise BudgetDenied("INVALID_ESTIMATE_FAIL_CLOSED")
                daily_auth, monthly_auth = _sum_openai(ledger, day=day)
                if daily_auth + est > openai_daily_cap():
                    raise BudgetDenied(
                        "OPENAI_DAILY_EXHAUSTED",
                        detail={"authorized": _money(daily_auth), "need": _money(est), "cap": _money(openai_daily_cap())},
                    )
                if monthly_auth + est > openai_monthly_cap():
                    raise BudgetDenied(
                        "OPENAI_MONTHLY_EXHAUSTED",
                        detail={"authorized": _money(monthly_auth), "need": _money(est), "cap": _money(openai_monthly_cap())},
                    )
                ledger.setdefault("openai_reservations", {})[rid] = {
                    "reservation_id": rid,
                    "amount_usd": _money(est),
                    "budget_day": day,
                    "budget_month": budget_month_key(),
                    "model": model,
                    "purpose": purpose,
                    "opportunity_id": opportunity_id,
                    "created_at": now_utc().isoformat(),
                    "status": "RESERVED",
                }
                _save_ledger_unlocked(ledger)
                return {"reservation_id": rid, "provider": "openai", "amount_usd": _money(est), "budget_day": day}

            if provider.lower() in {"free", "cache", "http_public"}:
                return {"reservation_id": rid, "provider": provider, "amount_usd": "0", "free": True}

            raise BudgetDenied("PROVIDER_NOT_AUTHORIZED", detail={"provider": provider})


def commit_actual_usage(
    reservation_id: str,
    *,
    actual_usd: Decimal | float | None = None,
    actual_sam_calls: int | None = None,
    success: bool = True,
    charge_uncertain: bool = False,
    usage_tokens: dict[str, Any] | None = None,
) -> dict[str, Any]:
    with _THREAD:
        with _file_lock():
            ledger = _load_ledger_unlocked()
            # OpenAI path
            rsv = (ledger.get("openai_reservations") or {}).pop(reservation_id, None)
            if rsv:
                reserved = Decimal(str(rsv.get("amount_usd") or 0))
                if charge_uncertain or (not success and charge_uncertain):
                    # Hold full reservation as unreconciled
                    amt = reserved
                    status = "UNRECONCILED_HELD"
                elif not success and not charge_uncertain:
                    amt = Decimal("0")
                    status = "COMMITTED"
                else:
                    if actual_usd is None:
                        amt = reserved  # conservative — do not release optimistically
                        status = "UNRECONCILED_HELD"
                    else:
                        amt = min(reserved, Decimal(str(actual_usd)))
                        status = "COMMITTED"
                ledger.setdefault("openai_commits", []).append(
                    {
                        "reservation_id": reservation_id,
                        "amount_usd": _money(amt),
                        "reserved_usd": _money(reserved),
                        "budget_day": rsv.get("budget_day"),
                        "status": status,
                        "success": success,
                        "usage_tokens": usage_tokens,
                        "committed_at": now_utc().isoformat(),
                        "opportunity_id": rsv.get("opportunity_id"),
                        "purpose": rsv.get("purpose"),
                        "model": rsv.get("model"),
                    }
                )
                _save_ledger_unlocked(ledger)
                return {"provider": "openai", "committed_usd": _money(amt), "status": status, "released_usd": _money(max(Decimal("0"), reserved - amt))}

            rsv_s = (ledger.get("sam_reservations") or {}).pop(reservation_id, None)
            if rsv_s:
                calls = int(rsv_s.get("calls") or 0)
                if charge_uncertain or actual_sam_calls is None:
                    used = calls  # conservative count
                    status = "UNRECONCILED_HELD" if charge_uncertain else "COMMITTED"
                else:
                    used = max(0, int(actual_sam_calls))
                    status = "COMMITTED"
                if not success and not charge_uncertain and actual_sam_calls == 0:
                    used = 0
                ledger.setdefault("sam_commits", []).append(
                    {
                        "reservation_id": reservation_id,
                        "calls": used,
                        "budget_day": rsv_s.get("budget_day"),
                        "status": status,
                        "success": success,
                        "committed_at": now_utc().isoformat(),
                    }
                )
                _save_ledger_unlocked(ledger)
                return {"provider": "sam", "calls": used, "status": status}

            # Free reservation or unknown — idempotent no-op
            return {"provider": "unknown", "status": "NO_RESERVATION", "reservation_id": reservation_id}


def release_unused_reservation(reservation_id: str) -> dict[str, Any]:
    """Release a reservation with zero charge (only when caller is certain no bill)."""
    return commit_actual_usage(reservation_id, actual_usd=0, actual_sam_calls=0, success=False, charge_uncertain=False)


def dashboard_payload() -> dict[str, Any]:
    rem = get_remaining_budget()
    return {
        "kind": "commercial_budget_dashboard",
        "build": BUILD,
        "openai_spend_today_usd": rem["openai_committed_usd"],
        "openai_reserved_today_usd": rem["openai_reserved_usd"],
        "openai_limit_today_usd": rem["openai_daily_cap_usd"],
        "openai_remaining_today_usd": rem["openai_remaining_usd"],
        "openai_spend_month_usd": rem["monthly"]["openai_committed_usd"],
        "openai_limit_month_usd": rem["monthly"]["openai_monthly_cap_usd"],
        "openai_remaining_month_usd": rem["monthly"]["openai_remaining_usd"],
        "sam_calls_today": rem["sam_calls_committed"],
        "sam_calls_limit": rem["sam_daily_cap"],
        "sam_calls_remaining": rem["sam_calls_remaining"],
        "paid_research_paused": rem["paid_research_paused"],
        "other_paid_usd": "0.0000",
        "timezone": rem["timezone"],
        "budget_day": rem["budget_day"],
        "monthly_cap_definition": "calendar_month_America_Chicago",
        "shared_ledger": shared_ledger_safe(),
    }
