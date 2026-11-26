"""Authoritative OpenAI research budget manager.

Application-estimated costs are tracked for enforcement; they are not invoice totals.
"""

from __future__ import annotations

import threading
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from m3.config import get_settings
from m3.models import ResearchSpendEvent, ResearchBudgetPolicy

# Conservative published rates (USD) — estimates only
WEB_SEARCH_CALL_USD = Decimal("0.01")  # $10 / 1k calls
# gpt-4o-mini fixed search-content block when using non-preview web_search
MINI_SEARCH_CONTENT_TOKENS = 8000
MODEL_RATES: dict[str, tuple[Decimal, Decimal]] = {
    # (input_per_1m, output_per_1m)
    "gpt-4o-mini": (Decimal("0.15"), Decimal("0.60")),
    "gpt-4.1-mini": (Decimal("0.40"), Decimal("1.60")),
    "gpt-5-mini": (Decimal("0.25"), Decimal("2.00")),
    "gpt-5-nano": (Decimal("0.05"), Decimal("0.40")),
    "gpt-4o": (Decimal("2.50"), Decimal("10.00")),
    "gpt-5": (Decimal("1.25"), Decimal("10.00")),
    "gpt-6.2-astra": (Decimal("8.00"), Decimal("40.00")),
}

_lock = threading.Lock()


def _d(v: Any) -> Decimal:
    try:
        return Decimal(str(v))
    except Exception:
        return Decimal("0")


def default_policy_dict() -> dict[str, Any]:
    s = get_settings()
    return {
        "paid_research_enabled": bool(s.paid_research_enabled and s.openai_enabled),
        "daily_budget_usd": float(s.research_daily_budget_usd),
        "monthly_budget_usd": float(s.research_monthly_budget_usd),
        "per_job_budget_usd": float(s.research_per_job_budget_usd),
        "per_user_daily_budget_usd": float(s.research_per_user_daily_budget_usd),
        "max_research_depth": int(s.research_max_depth),
        "default_model": s.openai_default_model,
        "capable_model": s.openai_capable_model,
        "search_context_size": s.openai_search_context_size,
    }


def ensure_budget_policy(db: Session) -> ResearchBudgetPolicy:
    row = db.query(ResearchBudgetPolicy).order_by(ResearchBudgetPolicy.updated_at.desc()).first()
    d = default_policy_dict()
    if row:
        # Env kill-switch / enable can turn paid research on without wiping owner ceilings
        s = get_settings()
        if s.openai_enabled and s.paid_research_enabled and not row.paid_research_enabled:
            row.paid_research_enabled = True
            db.add(row)
            db.commit()
            db.refresh(row)
        return row
    row = ResearchBudgetPolicy(
        paid_research_enabled=d["paid_research_enabled"],
        daily_budget_usd=d["daily_budget_usd"],
        monthly_budget_usd=d["monthly_budget_usd"],
        per_job_budget_usd=d["per_job_budget_usd"],
        per_user_daily_budget_usd=d["per_user_daily_budget_usd"],
        max_research_depth=d["max_research_depth"],
        default_model=d["default_model"],
        capable_model=d["capable_model"],
        search_context_size=d["search_context_size"],
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def estimate_response_cost(
    *,
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    web_search_calls: int = 0,
    search_content_tokens: int | None = None,
) -> Decimal:
    """Application cost estimate from token/tool usage (not an OpenAI invoice).

    Build 002O: when the API already reports input_tokens that include tool/search
    content, do NOT add another assumed 8k search-content block (prior double-count).
    """
    rates = MODEL_RATES.get(model) or MODEL_RATES["gpt-4o-mini"]
    in_rate, out_rate = rates
    sc = search_content_tokens
    if sc is None and web_search_calls > 0 and model in {"gpt-4o-mini", "gpt-4.1-mini"}:
        # Assume fixed search-content block only when measured input is too small
        # to have already included it (pre-flight reserves still pass explicit sc).
        if int(input_tokens or 0) < MINI_SEARCH_CONTENT_TOKENS:
            sc = MINI_SEARCH_CONTENT_TOKENS * web_search_calls
        else:
            sc = 0
    sc = sc or 0
    token_cost = (Decimal(input_tokens + sc) / Decimal(1_000_000)) * in_rate
    token_cost += (Decimal(output_tokens) / Decimal(1_000_000)) * out_rate
    tool_cost = Decimal(web_search_calls) * WEB_SEARCH_CALL_USD
    return (token_cost + tool_cost).quantize(Decimal("0.0001"))


def estimate_reserve_for_call(*, model: str, depth: int = 1) -> Decimal:
    """Conservative pre-flight reservation before initiating a paid request."""
    calls = max(1, min(depth, 3))
    return estimate_response_cost(
        model=model,
        input_tokens=1200,
        output_tokens=800,
        web_search_calls=calls,
        search_content_tokens=MINI_SEARCH_CONTENT_TOKENS * calls if model.endswith("mini") else 4000 * calls,
    )


def _period_sums(db: Session, *, user_id: uuid.UUID | None = None) -> dict[str, Decimal]:
    today = date.today()
    month_start = today.replace(day=1)
    q = db.query(ResearchSpendEvent)
    rows = q.filter(ResearchSpendEvent.created_at >= datetime.combine(month_start, datetime.min.time()).replace(tzinfo=timezone.utc)).all()
    day_total = Decimal("0")
    month_total = Decimal("0")
    user_day = Decimal("0")
    reserved_open = Decimal("0")
    for r in rows:
        amt = _d(r.estimated_cost_usd)
        if r.status == "reserved":
            reserved_open += amt
        if r.status in {"reserved", "committed"}:
            month_total += amt
            if r.spend_date == today:
                day_total += amt
                if user_id and r.user_id == user_id:
                    user_day += amt
    return {
        "day": day_total,
        "month": month_total,
        "user_day": user_day,
        "reserved": reserved_open,
    }


def spending_snapshot(db: Session, *, user_id: uuid.UUID | None = None) -> dict[str, Any]:
    policy = ensure_budget_policy(db)
    sums = _period_sums(db, user_id=user_id)
    daily = _d(policy.daily_budget_usd)
    monthly = _d(policy.monthly_budget_usd)
    return {
        "paid_research_enabled": bool(policy.paid_research_enabled),
        "daily_budget_usd": float(daily),
        "monthly_budget_usd": float(monthly),
        "per_job_budget_usd": float(policy.per_job_budget_usd),
        "per_user_daily_budget_usd": float(policy.per_user_daily_budget_usd),
        "max_research_depth": int(policy.max_research_depth or 2),
        "default_model": policy.default_model,
        "capable_model": policy.capable_model,
        "search_context_size": policy.search_context_size,
        "today_spent_usd": float(sums["day"]),
        "month_spent_usd": float(sums["month"]),
        "today_remaining_usd": float(max(Decimal("0"), daily - sums["day"])),
        "month_remaining_usd": float(max(Decimal("0"), monthly - sums["month"])),
        "cost_basis": "application_estimate_not_invoice",
        "note": "Estimated costs enforce budgets; OpenAI invoices may differ.",
    }


class BudgetDenied(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def reserve_budget(
    db: Session,
    *,
    user_id: uuid.UUID | None,
    objective: str,
    model: str,
    estimated_usd: Decimal,
    research_run_id: uuid.UUID | None = None,
    job_spent_usd: Decimal = Decimal("0"),
    meta: dict | None = None,
) -> ResearchSpendEvent:
    """Atomically reserve estimated spend or raise BudgetDenied."""
    with _lock:
        policy = ensure_budget_policy(db)
        if not policy.paid_research_enabled:
            raise BudgetDenied("Paid OpenAI research is disabled by owner policy.")
        settings = get_settings()
        if not (settings.openai_enabled and settings.openai_api_key):
            raise BudgetDenied("OpenAI is not configured or enabled.")
        est = _d(estimated_usd)
        if est <= 0:
            est = estimate_reserve_for_call(model=model)
        sums = _period_sums(db, user_id=user_id)
        if sums["day"] + est > _d(policy.daily_budget_usd):
            raise BudgetDenied("Daily research budget exhausted.")
        if sums["month"] + est > _d(policy.monthly_budget_usd):
            raise BudgetDenied("Monthly research budget exhausted.")
        if user_id and sums["user_day"] + est > _d(policy.per_user_daily_budget_usd):
            raise BudgetDenied("Per-user daily research budget exhausted.")
        if job_spent_usd + est > _d(policy.per_job_budget_usd):
            raise BudgetDenied("Per-job research budget exhausted.")
        ev = ResearchSpendEvent(
            user_id=user_id,
            research_run_id=research_run_id,
            objective=objective,
            model=model,
            status="reserved",
            estimated_cost_usd=float(est),
            spend_date=date.today(),
            meta=meta or {},
        )
        db.add(ev)
        db.commit()
        db.refresh(ev)
        return ev


def commit_spend(
    db: Session,
    reservation: ResearchSpendEvent,
    *,
    actual_estimated_usd: Decimal,
    input_tokens: int = 0,
    output_tokens: int = 0,
    web_search_calls: int = 0,
    response_id: str | None = None,
    meta: dict | None = None,
) -> ResearchSpendEvent:
    with _lock:
        reservation.status = "committed"
        reservation.estimated_cost_usd = float(actual_estimated_usd)
        reservation.input_tokens = int(input_tokens or 0)
        reservation.output_tokens = int(output_tokens or 0)
        reservation.web_search_calls = int(web_search_calls or 0)
        reservation.response_id = response_id
        reservation.meta = {**(reservation.meta or {}), **(meta or {}), "cost_basis": "application_estimate_not_invoice"}
        db.add(reservation)
        db.commit()
        db.refresh(reservation)
        return reservation


def release_reservation(db: Session, reservation: ResearchSpendEvent, *, reason: str = "") -> None:
    with _lock:
        reservation.status = "released"
        reservation.meta = {**(reservation.meta or {}), "release_reason": reason}
        db.add(reservation)
        db.commit()


def update_owner_policy(
    db: Session,
    *,
    actor_id: uuid.UUID,
    **fields: Any,
) -> ResearchBudgetPolicy:
    policy = ensure_budget_policy(db)
    allowed = {
        "paid_research_enabled",
        "daily_budget_usd",
        "monthly_budget_usd",
        "per_job_budget_usd",
        "per_user_daily_budget_usd",
        "max_research_depth",
        "default_model",
        "capable_model",
        "search_context_size",
    }
    for k, v in fields.items():
        if k in allowed and v is not None:
            setattr(policy, k, v)
    policy.updated_by = actor_id
    policy.updated_at = datetime.now(timezone.utc)
    db.add(policy)
    db.commit()
    db.refresh(policy)
    return policy
