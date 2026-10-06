"""Separate price-search budget — exhaustion ≠ NO_PUBLIC_PRICE."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from application_clock import now_utc
from m3_data_root import data_path
from public_price_search.models import DEFAULT_QUERY_BUDGET


def _path() -> Path:
    return data_path("m3_public_price_search_budget.json")


def load_budget() -> dict[str, Any]:
    p = _path()
    today = now_utc().date().isoformat()
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if data.get("day") == today:
                return data
        except Exception:
            pass
    return {
        "day": today,
        "price_search_budget": DEFAULT_QUERY_BUDGET,
        "queries_used": 0,
        "pages_fetched": 0,
    }


def save_budget(data: dict[str, Any]) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = now_utc().isoformat()
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


def remaining(data: dict[str, Any] | None = None) -> int:
    b = data or load_budget()
    return max(0, int(b.get("price_search_budget") or 0) - int(b.get("queries_used") or 0))


def consume(n: int = 1, *, kind: str = "query") -> dict[str, Any]:
    b = load_budget()
    if kind == "query":
        b["queries_used"] = int(b.get("queries_used") or 0) + n
    else:
        b["pages_fetched"] = int(b.get("pages_fetched") or 0) + n
    save_budget(b)
    b["searches_remaining"] = remaining(b)
    return b


def snapshot() -> dict[str, Any]:
    b = load_budget()
    return {
        "price_search_budget": b.get("price_search_budget"),
        "queries_used": b.get("queries_used"),
        "pages_fetched": b.get("pages_fetched"),
        "searches_remaining": remaining(b),
        "day": b.get("day"),
    }


def ensure_budget(minimum_remaining: int = 100) -> dict[str, Any]:
    """Raise daily budget ceiling if remaining queries are too low for a staged run."""
    b = load_budget()
    rem = remaining(b)
    if rem < minimum_remaining:
        used = int(b.get("queries_used") or 0)
        b["price_search_budget"] = used + minimum_remaining
        save_budget(b)
    return snapshot()
