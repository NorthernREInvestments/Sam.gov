"""Financing cost from verified rules only — Decimal-safe, no invented rates."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from financing_intelligence.constants import UNKNOWN
from financing_intelligence.store import money, money_str


def _pct(rules: dict[str, Any], key: str) -> Decimal | None:
    raw = rules.get(key)
    if raw in (None, "", UNKNOWN):
        return None
    try:
        return Decimal(str(raw).replace("%", "").strip())
    except Exception:
        return None


def estimate_financing_cost(
    *,
    financed_amount: Decimal,
    rules: dict[str, Any],
    financed_days: int | None = None,
) -> dict[str, Any]:
    """Return fee breakdown. Time-based fees require known financed_days."""
    fee = Decimal("0")
    unknown_parts: list[str] = []
    components: list[dict[str, Any]] = []

    def add_fixed(key: str, label: str) -> None:
        nonlocal fee
        if rules.get(key) in (None, "", UNKNOWN):
            return
        amt = money(rules.get(key))
        fee += amt
        components.append({"type": label, "amount": money_str(amt)})

    add_fixed("origination_fee", "origination_fee")
    add_fixed("transaction_fee", "transaction_fee")
    add_fixed("wire_fee", "wire_fee")
    add_fixed("documentation_fee", "documentation_fee")
    add_fixed("extension_fee", "extension_fee")
    add_fixed("other_verified_fee", "other_verified_fee")

    pct = _pct(rules, "fee_pct_of_advance") or _pct(rules, "discount_rate") or _pct(rules, "factoring_rate")
    if pct is not None:
        amt = (financed_amount * pct / Decimal("100")).quantize(Decimal("0.01"))
        fee += amt
        components.append({"type": "percentage_fee", "rate_pct": str(pct), "amount": money_str(amt)})

    # Time-based: require known days — never invent duration
    daily = _pct(rules, "daily_rate")
    weekly = _pct(rules, "weekly_rate")
    monthly = _pct(rules, "monthly_rate")
    if daily is not None or weekly is not None or monthly is not None:
        if financed_days is None:
            unknown_parts.append("financed_days_required_for_time_based_fee")
        else:
            days = Decimal(int(financed_days))
            if daily is not None:
                # daily_rate as % of advance per day
                amt = (financed_amount * daily / Decimal("100") * days).quantize(Decimal("0.01"))
                fee += amt
                components.append({"type": "daily_rate", "days": int(days), "amount": money_str(amt)})
            elif weekly is not None:
                weeks = (days / Decimal("7")).quantize(Decimal("0.01"))
                amt = (financed_amount * weekly / Decimal("100") * weeks).quantize(Decimal("0.01"))
                fee += amt
                components.append({"type": "weekly_rate", "weeks": str(weeks), "amount": money_str(amt)})
            elif monthly is not None:
                months = (days / Decimal("30")).quantize(Decimal("0.01"))
                amt = (financed_amount * monthly / Decimal("100") * months).quantize(Decimal("0.01"))
                fee += amt
                components.append({"type": "monthly_rate", "months": str(months), "amount": money_str(amt)})

    min_fee = money(rules.get("minimum_fee") or 0) if rules.get("minimum_fee") not in (None, "", UNKNOWN) else Decimal("0")
    if min_fee > 0 and fee < min_fee:
        fee = min_fee
        components.append({"type": "minimum_fee_applied", "amount": money_str(min_fee)})

    has_any_fee_rule = any(
        rules.get(k) not in (None, "", UNKNOWN)
        for k in (
            "origination_fee",
            "transaction_fee",
            "minimum_fee",
            "fee_pct_of_advance",
            "discount_rate",
            "factoring_rate",
            "daily_rate",
            "weekly_rate",
            "monthly_rate",
            "wire_fee",
            "documentation_fee",
            "extension_fee",
            "other_verified_fee",
        )
    )
    if not has_any_fee_rule:
        unknown_parts.append("no_verified_fee_rules")

    return {
        "total": money_str(fee),
        "total_decimal": fee,
        "components": components,
        "partially_unknown": bool(unknown_parts),
        "unknown_parts": unknown_parts,
        "financed_days_used": financed_days,
    }
