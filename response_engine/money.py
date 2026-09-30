"""R2 canonical money — reuses µLab Decimal utilities (no float money math)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from micro_purchase_lab_economics import D, MONEY_Q, PCT_Q, RATIO_Q, money, pct, ratio

# Re-export for response_engine consumers
__all__ = ["D", "money", "pct", "ratio", "MONEY_Q", "PCT_Q", "RATIO_Q", "as_str", "reject_bad_money"]


def as_str(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")


def reject_bad_money(value: Any, *, allow_zero: bool = True) -> Decimal | None:
    """Parse money; reject NaN/inf/negative (unless zero allowed)."""
    d = D(value)
    if d is None:
        return None
    if d.is_nan() or d.is_infinite():
        raise ValueError("malformed currency: NaN/infinity")
    if d < 0:
        raise ValueError("negative money not allowed")
    if d == 0 and not allow_zero:
        raise ValueError("zero money not allowed in this context")
    return money(d)
