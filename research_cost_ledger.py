"""ResearchCostLedger — compact persistent paid-action accounting."""

from __future__ import annotations

import json
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import uuid4

from application_clock import now_utc, today_local
from cost_governor_constants import COST_AUTHORIZED, COST_RECONCILED

DEFAULT_PATH = Path(__file__).resolve().parent / "artifacts" / "research_cost_ledger.json"
_lock = threading.RLock()


def _utc() -> str:
    return now_utc().isoformat()


class ResearchCostLedger:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or DEFAULT_PATH
        self._entries: list[dict[str, Any]] = []
        self._reservations: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._entries = list(data.get("entries") or [])
            self._reservations = dict(data.get("reservations") or {})

    def save(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "kind": "ResearchCostLedger",
            "updated_at": _utc(),
            "entries": self._entries[-5000:],
            "reservations": self._reservations,
        }
        self.path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        return self.path

    def append(self, entry: dict[str, Any]) -> dict[str, Any]:
        with _lock:
            row = {
                "ledger_id": entry.get("ledger_id") or f"CL-{uuid4().hex[:12]}",
                "timestamp": entry.get("timestamp") or _utc(),
                **entry,
            }
            self._entries.append(row)
            self.save()
            return deepcopy(row)

    def reserve(self, reservation_id: str, amount: float, meta: dict[str, Any]) -> dict[str, Any]:
        with _lock:
            row = {
                "reservation_id": reservation_id,
                "amount": float(amount),
                "created_at": _utc(),
                **meta,
                "status": COST_AUTHORIZED,
            }
            self._reservations[reservation_id] = row
            self.save()
            return deepcopy(row)

    def release_reservation(self, reservation_id: str, *, actual_cost: float | None = None) -> float:
        """Release reservation; return unused amount released."""
        with _lock:
            row = self._reservations.pop(reservation_id, None)
            if not row:
                return 0.0
            reserved = float(row.get("amount") or 0)
            used = reserved if actual_cost is None else min(reserved, float(actual_cost))
            unused = max(0.0, reserved - (0.0 if actual_cost is None else float(actual_cost)))
            self.save()
            return unused if actual_cost is not None else reserved

    def reserved_total(self) -> float:
        with _lock:
            return round(sum(float(r.get("amount") or 0) for r in self._reservations.values()), 6)

    def entries(self) -> list[dict[str, Any]]:
        with _lock:
            return deepcopy(self._entries)

    def spend_totals(self) -> dict[str, Any]:
        today = today_local().isoformat()
        month = today[:7]
        absolute = 0.0
        monthly = 0.0
        daily = 0.0
        by_tier: dict[str, float] = {}
        by_stage: dict[str, float] = {}
        by_provider: dict[str, float] = {}
        by_opp: dict[str, float] = {}
        with _lock:
            for e in self._entries:
                status = e.get("cost_status")
                if status not in {COST_RECONCILED, "EXECUTED", COST_AUTHORIZED}:
                    # Count reconciled + executed actuals; skip deferred/blocked
                    if status not in {"EXECUTED", "RECONCILED"}:
                        continue
                cost = float(e.get("actual_cost") if e.get("actual_cost") is not None else e.get("authorized_max_cost") or 0)
                if cost <= 0 and e.get("actual_cost") is None and status != "EXECUTED":
                    continue
                # Prefer actual when reconciled
                if e.get("actual_cost") is not None:
                    cost = float(e["actual_cost"])
                elif e.get("authorized_max_cost") is not None and status == "EXECUTED":
                    cost = float(e["authorized_max_cost"])
                else:
                    continue
                absolute += cost
                ts = str(e.get("timestamp") or "")
                if ts.startswith(month):
                    monthly += cost
                if ts[:10] == today:
                    daily += cost
                tier = str(e.get("priority_tier") or "UNKNOWN")
                by_tier[tier] = by_tier.get(tier, 0.0) + cost
                stage = str(e.get("research_stage") or "UNKNOWN")
                by_stage[stage] = by_stage.get(stage, 0.0) + cost
                prov = str(e.get("provider") or "UNKNOWN")
                by_provider[prov] = by_provider.get(prov, 0.0) + cost
                oid = e.get("deal_id") or e.get("opportunity_id")
                if oid:
                    by_opp[str(oid)] = by_opp.get(str(oid), 0.0) + cost
        return {
            "absolute_spent": round(absolute, 6),
            "monthly_spent": round(monthly, 6),
            "daily_spent": round(daily, 6),
            "reserved": self.reserved_total(),
            "by_tier": {k: round(v, 6) for k, v in by_tier.items()},
            "by_stage": {k: round(v, 6) for k, v in by_stage.items()},
            "by_provider": {k: round(v, 6) for k, v in by_provider.items()},
            "by_opportunity": {k: round(v, 6) for k, v in by_opp.items()},
        }

    def opportunity_spend(self, opportunity_id: str) -> float:
        return float(self.spend_totals()["by_opportunity"].get(str(opportunity_id), 0.0))

    def find_fingerprint(self, fingerprint: str) -> dict[str, Any] | None:
        with _lock:
            for e in reversed(self._entries):
                if e.get("action_fingerprint") == fingerprint and e.get("cost_status") in {
                    "EXECUTED",
                    "RECONCILED",
                    "AUTHORIZED",
                }:
                    return deepcopy(e)
            for r in self._reservations.values():
                if r.get("action_fingerprint") == fingerprint:
                    return deepcopy(r)
        return None
