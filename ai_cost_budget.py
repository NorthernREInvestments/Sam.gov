"""Dollar-based AI budget: monthly hard cap, daily soft limit, stage reserves."""

from __future__ import annotations
from application_clock import now_utc, today_local

import json
import os
from datetime import date, datetime, timezone
from typing import Any

from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).resolve().parent / ".env")


class AIDollarBudgetExceeded(Exception):
    def __init__(self, message: str, *, reason: str = "monthly"):
        self.reason = reason
        super().__init__(message)


class AIStageBudgetExceeded(Exception):
    def __init__(self, message: str, *, stage: int):
        self.stage = stage
        super().__init__(message)


class AIInputTooLarge(Exception):
    def __init__(self, message: str, *, stage: int, chars: int, limit: int):
        self.stage = stage
        self.chars = chars
        self.limit = limit
        super().__init__(message)


def _float_env(key: str, default: float) -> float:
    raw = os.getenv(key)
    if raw is None or not str(raw).strip():
        return default
    try:
        return max(0.0, float(str(raw).strip()))
    except ValueError:
        return default


def monthly_dollar_budget() -> float:
    return _float_env("AI_MONTHLY_DOLLAR_BUDGET", 40.0)


def daily_dollar_soft_limit() -> float:
    return _float_env("AI_DAILY_DOLLAR_SOFT_LIMIT", 1.0)


def stage_budget_percent(stage: int) -> float:
    defaults = {1: 20.0, 2: 20.0, 3: 25.0, 4: 20.0, 5: 15.0}
    key = f"AI_BUDGET_STAGE{stage}_PERCENT"
    return _float_env(key, defaults.get(stage, 0.0))


def stage_monthly_ceiling_usd(stage: int) -> float:
    return round(monthly_dollar_budget() * stage_budget_percent(stage) / 100.0, 4)


def _month_key(d: date | None = None) -> str:
    d = d or today_local()
    return f"{d.year:04d}-{d.month:02d}"


def _cost_ledger_key(month: str | None = None) -> str:
    return f"ai_cost_ledger_{month or _month_key()}"


def _load_ledger(session, month: str | None = None) -> dict[str, Any]:
    from models import AppSetting

    key = _cost_ledger_key(month)
    row = session.query(AppSetting).filter_by(key=key).first()
    if not row or not row.value:
        return {"month": month or _month_key(), "entries": [], "totals": {}}
    try:
        data = json.loads(row.value)
    except json.JSONDecodeError:
        return {"month": month or _month_key(), "entries": [], "totals": {}}
    if not isinstance(data, dict):
        return {"month": month or _month_key(), "entries": [], "totals": {}}
    data.setdefault("entries", [])
    data.setdefault("totals", {})
    return data


def _save_ledger(session, ledger: dict[str, Any], month: str | None = None) -> None:
    from models import AppSetting

    key = _cost_ledger_key(month)
    # Cap entries to keep AppSetting size reasonable.
    entries = list(ledger.get("entries") or [])[-2000:]
    ledger = {**ledger, "entries": entries}
    serialized = json.dumps(ledger)
    row = session.query(AppSetting).filter_by(key=key).first()
    if row:
        row.value = serialized
    else:
        session.add(AppSetting(key=key, value=serialized))


def _recompute_totals(entries: list[dict[str, Any]]) -> dict[str, Any]:
    today = today_local().isoformat()
    by_stage: dict[str, float] = {}
    by_model: dict[str, float] = {}
    by_task: dict[str, float] = {}
    by_notice: dict[str, float] = {}
    today_cost = 0.0
    month_cost = 0.0
    web_search_cost = 0.0
    for e in entries:
        if not e.get("success"):
            continue
        cost = float(e.get("estimated_cost_usd") or 0.0)
        month_cost += cost
        day = str(e.get("ts") or "")[:10]
        if day == today:
            today_cost += cost
        stage = str(e.get("funnel_stage") if e.get("funnel_stage") is not None else "unknown")
        by_stage[stage] = by_stage.get(stage, 0.0) + cost
        model = str(e.get("model") or "unknown")
        by_model[model] = by_model.get(model, 0.0) + cost
        task = str(e.get("task") or "unknown")
        by_task[task] = by_task.get(task, 0.0) + cost
        notice = e.get("notice_id")
        if notice:
            by_notice[str(notice)] = by_notice.get(str(notice), 0.0) + cost
        if e.get("web_search"):
            web_search_cost += cost
    return {
        "month_to_date_usd": round(month_cost, 6),
        "today_usd": round(today_cost, 6),
        "by_stage_usd": {k: round(v, 6) for k, v in by_stage.items()},
        "by_model_usd": {k: round(v, 6) for k, v in by_model.items()},
        "by_task_usd": {k: round(v, 6) for k, v in by_task.items()},
        "by_opportunity_usd": {k: round(v, 6) for k, v in by_notice.items()},
        "web_search_usd": round(web_search_cost, 6),
    }


def record_ai_dollar_spend(entry: dict[str, Any]) -> dict[str, Any]:
    """Persist a cost ledger entry and return updated totals."""
    from database import SessionLocal

    session = SessionLocal()
    try:
        ledger = _load_ledger(session)
        entries = list(ledger.get("entries") or [])
        entries.append(entry)
        totals = _recompute_totals(entries)
        ledger["entries"] = entries
        ledger["totals"] = totals
        ledger["updated_at"] = now_utc().isoformat()
        _save_ledger(session, ledger)
        session.commit()
        return totals
    finally:
        session.close()


def get_cost_snapshot() -> dict[str, Any]:
    from database import SessionLocal

    session = SessionLocal()
    try:
        ledger = _load_ledger(session)
        totals = ledger.get("totals") or _recompute_totals(list(ledger.get("entries") or []))
    finally:
        session.close()

    monthly = monthly_dollar_budget()
    daily_soft = daily_dollar_soft_limit()
    mtd = float(totals.get("month_to_date_usd") or 0.0)
    today = float(totals.get("today_usd") or 0.0)
    by_stage = totals.get("by_stage_usd") or {}
    stage_ceilings = {str(s): stage_monthly_ceiling_usd(s) for s in (1, 2, 3, 4, 5)}
    stage_remaining = {
        str(s): round(max(0.0, stage_ceilings[str(s)] - float(by_stage.get(str(s), 0.0))), 6)
        for s in (1, 2, 3, 4, 5)
    }
    return {
        "monthly_budget_usd": monthly,
        "daily_soft_limit_usd": daily_soft,
        "month_to_date_usd": mtd,
        "today_usd": today,
        "monthly_remaining_usd": round(max(0.0, monthly - mtd), 6),
        "daily_soft_remaining_usd": round(max(0.0, daily_soft - today), 6),
        "stage_ceilings_usd": stage_ceilings,
        "stage_spent_usd": {k: float(v) for k, v in by_stage.items()},
        "stage_remaining_usd": stage_remaining,
        "by_model_usd": totals.get("by_model_usd") or {},
        "by_task_usd": totals.get("by_task_usd") or {},
        "by_opportunity_usd": totals.get("by_opportunity_usd") or {},
        "web_search_usd": totals.get("web_search_usd") or 0.0,
    }


def can_afford_automatic_call(
    *,
    stage: int | None,
    estimated_cost_usd: float,
    respect_daily_soft: bool = True,
) -> tuple[bool, str | None]:
    """
    Gate automatic paid calls.
    Monthly budget is hard. Daily soft limit warns/blocks automation runaway.
    Stage ceilings reserve money for later stages (do not spend merely because remaining).
    """
    snap = get_cost_snapshot()
    est = max(0.0, float(estimated_cost_usd))
    if est > float(snap["monthly_remaining_usd"]) + 1e-9:
        return False, "monthly_budget_exhausted"
    if respect_daily_soft and est > float(snap["daily_soft_remaining_usd"]) + 1e-9:
        return False, "daily_soft_limit"
    if stage is not None and int(stage) >= 1:
        rem = float(snap["stage_remaining_usd"].get(str(int(stage)), 0.0))
        if est > rem + 1e-9:
            return False, f"stage_{int(stage)}_reserve_exhausted"
    return True, None


def require_automatic_budget(*, stage: int | None, estimated_cost_usd: float) -> None:
    ok, reason = can_afford_automatic_call(stage=stage, estimated_cost_usd=estimated_cost_usd)
    if ok:
        return
    if reason and reason.startswith("stage_"):
        raise AIStageBudgetExceeded(
            f"Stage {stage} budget reserve exhausted "
            f"(est ${estimated_cost_usd:.4f}; reason={reason}).",
            stage=int(stage or 0),
        )
    raise AIDollarBudgetExceeded(
        f"AI dollar budget blocks automatic call "
        f"(est ${estimated_cost_usd:.4f}; reason={reason}).",
        reason=reason or "blocked",
    )
