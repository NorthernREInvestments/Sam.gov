"""Budget configuration + operator audit — AI cannot self-increase caps."""

from __future__ import annotations

import json
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

from application_clock import now_utc, today_local
from cost_governor_constants import (
    DEFAULT_ABSOLUTE_CAP,
    DEFAULT_DAILY_CAP,
    DEFAULT_MONTHLY_CAP,
    DEFAULT_PER_OPPORTUNITY_CAP,
    DEFAULT_PER_RUN_CAP,
    DEFAULT_SAFETY_BUFFER,
)

DEFAULT_PATH = Path(__file__).resolve().parent / "artifacts" / "cost_governor_budget_config.json"
_lock = threading.RLock()


def _utc() -> str:
    return now_utc().isoformat()


def default_budget_config() -> dict[str, Any]:
    return {
        "ABSOLUTE_AUTONOMOUS_SPEND_CAP": DEFAULT_ABSOLUTE_CAP,
        "MONTHLY_CAP": DEFAULT_MONTHLY_CAP,
        "DAILY_CAP": DEFAULT_DAILY_CAP,
        "PER_RUN_CAP": DEFAULT_PER_RUN_CAP,
        "PER_OPPORTUNITY_CAP": DEFAULT_PER_OPPORTUNITY_CAP,
        "safety_buffer_usd": DEFAULT_SAFETY_BUFFER,
        "provider_caps": {},
        "model_caps": {},
        "stage_caps": {},
        "paid_discovery_paused": False,
        "paid_research_paused": False,
        "updated_at": _utc(),
        "updated_by": "system_default",
    }


class BudgetConfigStore:
    """Operator-authoritative budget limits with audit history."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or DEFAULT_PATH
        self._config = default_budget_config()
        self._audit: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            cfg = data.get("config") or {}
            self._config = {**default_budget_config(), **cfg}
            self._audit = list(data.get("audit") or [])

    def save(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"kind": "CostGovernorBudgetConfig", "config": self._config, "audit": self._audit[-500:]}
        self.path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        return self.path

    def get(self) -> dict[str, Any]:
        with _lock:
            return deepcopy(self._config)

    def audit(self) -> list[dict[str, Any]]:
        with _lock:
            return deepcopy(self._audit)

    def update_limits(
        self,
        updates: dict[str, Any],
        *,
        operator_id: str,
        reason: str | None = None,
        allow_autonomous: bool = False,
    ) -> dict[str, Any]:
        """Only explicit operator action may raise caps. Autonomous code cannot."""
        if not allow_autonomous and operator_id in {"ai", "autonomous", "system", "m3"}:
            raise PermissionError("AI/autonomous code cannot increase or change budget caps")
        with _lock:
            old = deepcopy(self._config)
            for k, v in updates.items():
                if k.startswith("_"):
                    continue
                if isinstance(v, (int, float)) and v < 0:
                    raise ValueError(f"budget limit {k} cannot be negative")
                self._config[k] = v
            self._config["updated_at"] = _utc()
            self._config["updated_by"] = operator_id
            entry = {
                "at": _utc(),
                "operator_id": operator_id,
                "reason": reason,
                "old": {k: old.get(k) for k in updates},
                "new": {k: self._config.get(k) for k in updates},
            }
            self._audit.append(entry)
            self.save()
            return deepcopy(self._config)

    def period_keys(self) -> dict[str, str]:
        d = today_local()
        return {
            "day": d.isoformat(),
            "month": f"{d.year:04d}-{d.month:02d}",
        }
