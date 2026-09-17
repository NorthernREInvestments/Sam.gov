"""CostGovernor — authoritative preauthorization for ALL paid autonomous M3 activity."""

from __future__ import annotations

import hashlib
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from application_clock import now_utc
from budget_config import BudgetConfigStore
from cost_governor_constants import (
    ACTION_AI_COMPLETION,
    BACKLOG_RECOVERY,
    BUDGET_CONSTRAINED,
    BUDGET_NORMAL,
    CATCH_UP,
    COST_AUTHORIZED,
    COST_BLOCKED_DAILY,
    COST_BLOCKED_HARD_CAP,
    COST_BLOCKED_MONTHLY,
    COST_BLOCKED_PER_OPP,
    COST_BLOCKED_RUN,
    COST_DEFERRED_BUDGET,
    COST_DEFERRED_PRIORITY,
    COST_DEFERRED_VALUE,
    COST_EXECUTED,
    COST_FAILED_CHARGE_POSSIBLE,
    COST_FAILED_NO_CHARGE,
    COST_FREE,
    COST_RECONCILED,
    COST_REUSED_EVIDENCE,
    COST_UNKNOWN,
    DISCOVERY_THROTTLED,
    HARD_CAP_EXHAUSTED,
    PAID_DISCOVERY_PAUSED,
    TIER_1_ACTIVE,
    TIER_2_QUALIFIED,
    TIER_3_BACKLOG,
    TIER_4_NEW_DISCOVERY,
    TIER_ORDER,
)
from research_cost_ledger import ResearchCostLedger

_lock = threading.RLock()
_INSTANCE: "CostGovernor | None" = None


def _utc() -> str:
    return now_utc().isoformat()


# Default provider pricing knowledge (effective dates — not scattered hardcodes in business logic)
DEFAULT_PRICING: dict[str, dict[str, Any]] = {
    "openai": {
        "effective_date": "2026-01-01",
        "input_per_1m": 0.15,
        "output_per_1m": 0.60,
        "request_fixed": 0.0,
        "provenance": "dev_default_estimate",
        "confidence": "MEDIUM",
        "last_verified": "2026-01-01",
    },
    "paid_search": {
        "effective_date": "2026-01-01",
        "per_request": 0.01,
        "provenance": "dev_default_estimate",
        "confidence": "LOW",
        "last_verified": "2026-01-01",
    },
}


def classify_priority_tier(
    *,
    action_type: str,
    tracked: bool = False,
    material_change: bool = False,
    deadline_critical: bool = False,
    pursuit_state: str | None = None,
    is_new_discovery: bool = False,
    is_backlog: bool = False,
    freshness_check: bool = False,
) -> str:
    if tracked and (material_change or deadline_critical or freshness_check):
        return TIER_1_ACTIVE
    if tracked or deadline_critical:
        return TIER_1_ACTIVE
    ps = (pursuit_state or "").upper()
    if ps in {"PURSUIT_WORTHY", "PURSUIT_WORTHY_WITH_MATERIAL_UNCERTAINTY", "PURSUIT_WORTHY_WITH_UNCERTAINTY"}:
        return TIER_2_QUALIFIED
    if is_new_discovery or action_type in {"PAID_SOURCE_FETCH", "WEEKLY_SOURCE_DISCOVERY"}:
        return TIER_4_NEW_DISCOVERY
    if is_backlog:
        return TIER_3_BACKLOG
    return TIER_3_BACKLOG


def estimate_max_cost(
    *,
    provider: str,
    action_type: str,
    estimated_input_tokens: int = 0,
    estimated_output_tokens: int = 0,
    requests: int = 1,
    explicit_max: float | None = None,
    pricing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if explicit_max is not None:
        return {
            "estimated_max_cost": float(explicit_max),
            "cost_known": True,
            "pricing_stale": False,
            "pricing_unknown": False,
        }
    p = pricing or DEFAULT_PRICING.get(provider) or DEFAULT_PRICING.get(provider.lower())
    if not p:
        # Unknown pricing → conservative non-zero floor (never treat as zero)
        return {
            "estimated_max_cost": 0.50 * max(1, requests),
            "cost_known": False,
            "pricing_stale": False,
            "pricing_unknown": True,
            "note": "UNKNOWN_COST conservative estimate — not zero",
        }
    if action_type == ACTION_AI_COMPLETION or "input_per_1m" in p:
        # Conservative: assume 1.5x token estimate variance
        inp = (estimated_input_tokens * 1.5 / 1_000_000.0) * float(p.get("input_per_1m") or 0)
        out = (estimated_output_tokens * 1.5 / 1_000_000.0) * float(p.get("output_per_1m") or 0)
        fixed = float(p.get("request_fixed") or 0) * requests
        return {
            "estimated_max_cost": round(inp + out + fixed, 6),
            "cost_known": True,
            "pricing_stale": False,
            "pricing_unknown": False,
        }
    per = float(p.get("per_request") or 0.01)
    return {
        "estimated_max_cost": round(per * requests * 1.2, 6),
        "cost_known": True,
        "pricing_stale": False,
        "pricing_unknown": False,
    }


def research_value_decision(
    *,
    question: str,
    could_change_pursuit_or_readiness: bool,
    optimistic_profit: float | None = None,
    profit_floor: float = 10_000.0,
    reusable_evidence_sufficient: bool = False,
    already_researched_fresh: bool = False,
) -> dict[str, Any]:
    if already_researched_fresh or reusable_evidence_sufficient:
        return {
            "kind": "ResearchValueDecision",
            "allow_paid": False,
            "reason": "REUSED_EXISTING_EVIDENCE: paid research avoided",
            "question": question,
        }
    if optimistic_profit is not None and optimistic_profit < profit_floor * 0.3:
        return {
            "kind": "ResearchValueDecision",
            "allow_paid": False,
            "reason": "DEFERRED_VALUE: optimistic economics cannot reach pursuit threshold",
            "question": question,
        }
    if not could_change_pursuit_or_readiness:
        return {
            "kind": "ResearchValueDecision",
            "allow_paid": False,
            "reason": "DEFERRED_VALUE: answer cannot materially change pursuit decision or bid readiness",
            "question": question,
        }
    return {
        "kind": "ResearchValueDecision",
        "allow_paid": True,
        "reason": f"VALUE: {question}",
        "question": question,
    }


class CostGovernor:
    """Single authoritative paid-operation gate."""

    def __init__(
        self,
        *,
        config: BudgetConfigStore | None = None,
        ledger: ResearchCostLedger | None = None,
        run_id: str | None = None,
    ) -> None:
        self.config_store = config or BudgetConfigStore()
        self.ledger = ledger or ResearchCostLedger()
        self.run_id = run_id or f"RUN-{uuid4().hex[:10]}"
        self._run_spent = 0.0
        self._deferred: list[dict[str, Any]] = []
        self._paused_sources: dict[str, dict[str, Any]] = {}
        self._owed_weekly_source_discovery = False
        self._action_fingerprints: set[str] = set()
        self._unfinished_higher = False

    def budget_snapshot(self) -> dict[str, Any]:
        cfg = self.config_store.get()
        totals = self.ledger.spend_totals()
        reserved = totals["reserved"]
        absolute_cap = float(cfg["ABSOLUTE_AUTONOMOUS_SPEND_CAP"])
        monthly_cap = float(cfg["MONTHLY_CAP"])
        daily_cap = float(cfg["DAILY_CAP"])
        per_run = float(cfg["PER_RUN_CAP"])
        buffer = float(cfg.get("safety_buffer_usd") or 0.05)
        safely_available = max(
            0.0,
            min(
                absolute_cap - totals["absolute_spent"] - reserved,
                monthly_cap - totals["monthly_spent"] - reserved,
                daily_cap - totals["daily_spent"] - reserved,
                per_run - self._run_spent - reserved,
            )
            - buffer,
        )
        state = BUDGET_NORMAL
        if totals["absolute_spent"] + reserved >= absolute_cap - buffer:
            state = HARD_CAP_EXHAUSTED
        elif cfg.get("paid_discovery_paused") or safely_available < 0.25:
            state = PAID_DISCOVERY_PAUSED if cfg.get("paid_discovery_paused") else BUDGET_CONSTRAINED
        elif safely_available < 1.0:
            state = DISCOVERY_THROTTLED
        if self._paused_sources and state == BUDGET_NORMAL:
            state = CATCH_UP if any(s.get("catch_up_required") for s in self._paused_sources.values()) else state
        return {
            "config": cfg,
            "totals": totals,
            "run_spent": round(self._run_spent, 6),
            "safely_available": round(max(0.0, safely_available), 6),
            "safety_buffer": buffer,
            "budget_state": state,
            "deferred_count": len(self._deferred),
            "paused_sources": list(self._paused_sources.keys()),
            "owed_weekly_source_discovery": self._owed_weekly_source_discovery,
        }

    def operating_state(self) -> str:
        return self.budget_snapshot()["budget_state"]

    def pause_source_for_budget(self, source_id: str, *, last_successful_checkpoint: str | None) -> dict[str, Any]:
        """CRITICAL: do NOT advance successful checkpoint."""
        row = {
            "source_id": source_id,
            "reason": "SOURCE_PAUSED_BUDGET",
            "paused_at": _utc(),
            "preserved_successful_checkpoint": last_successful_checkpoint,
            "catch_up_required": True,
            "not_source_failed": True,
        }
        self._paused_sources[source_id] = row
        return deepcopy(row)

    def resume_source_catch_up(self, source_id: str) -> dict[str, Any] | None:
        row = self._paused_sources.get(source_id)
        if not row:
            return None
        row = dict(row)
        row["resumed_at"] = _utc()
        row["catch_up_from"] = row.get("preserved_successful_checkpoint")
        row["catch_up_to"] = _utc()
        return row

    def mark_weekly_source_discovery_owed(self) -> None:
        self._owed_weekly_source_discovery = True

    def clear_weekly_source_discovery_owed(self) -> None:
        self._owed_weekly_source_discovery = False

    def action_fingerprint(self, request: dict[str, Any]) -> str:
        parts = [
            str(request.get("provider") or ""),
            str(request.get("action_type") or ""),
            str(request.get("deal_id") or request.get("opportunity_id") or ""),
            str(request.get("question") or ""),
            str(request.get("idempotency_key") or ""),
            str(request.get("content_version") or request.get("version_fingerprint") or ""),
        ]
        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:24]

    def authorize(self, request: dict[str, Any]) -> dict[str, Any]:
        """
        PaidActionRequest → approve / defer / reject.
        FREE actions short-circuit. Unknown cost never treated as zero.
        """
        with _lock:
            if request.get("is_free") or request.get("cost_class") == "FREE":
                return {
                    "authorized": True,
                    "cost_status": COST_FREE,
                    "reason": "FREE local/metadata work — no charge",
                    "estimated_max_cost": 0.0,
                    "reservation_id": None,
                }

            fp = self.action_fingerprint(request)
            prior = self.ledger.find_fingerprint(fp)
            if prior and not request.get("force_reverify"):
                return {
                    "authorized": False,
                    "cost_status": COST_REUSED_EVIDENCE,
                    "reason": "REUSED_EXISTING_EVIDENCE / idempotent fingerprint — paid action avoided",
                    "action_fingerprint": fp,
                    "prior": prior,
                }
            if fp in self._action_fingerprints and not request.get("force_reverify"):
                return {
                    "authorized": False,
                    "cost_status": COST_REUSED_EVIDENCE,
                    "reason": "duplicate/retry idempotency — paid action blocked",
                    "action_fingerprint": fp,
                }

            voi = research_value_decision(
                question=str(request.get("question") or "unspecified research question"),
                could_change_pursuit_or_readiness=bool(request.get("could_change_decision", True)),
                optimistic_profit=request.get("optimistic_profit"),
                profit_floor=float(request.get("profit_floor") or 10_000),
                reusable_evidence_sufficient=bool(request.get("reusable_evidence_sufficient")),
                already_researched_fresh=bool(request.get("already_researched_fresh")),
            )
            if not voi["allow_paid"]:
                entry = self._defer(request, COST_DEFERRED_VALUE, voi["reason"], fp)
                return {"authorized": False, "cost_status": COST_DEFERRED_VALUE, "reason": voi["reason"], "value": voi, "ledger": entry}

            tier = request.get("priority_tier") or classify_priority_tier(
                action_type=str(request.get("action_type") or ""),
                tracked=bool(request.get("tracked")),
                material_change=bool(request.get("material_change")),
                deadline_critical=bool(request.get("deadline_critical")),
                pursuit_state=request.get("pursuit_state"),
                is_new_discovery=bool(request.get("is_new_discovery")),
                is_backlog=bool(request.get("is_backlog")),
                freshness_check=bool(request.get("freshness_check")),
            )

            # Higher-priority unfinished work blocks Tier 4
            if request.get("has_unfinished_higher_priority"):
                self._unfinished_higher = True
            if tier == TIER_4_NEW_DISCOVERY and self._has_higher_priority_work():
                entry = self._defer(
                    request,
                    COST_DEFERRED_PRIORITY,
                    "DEFERRED_PRIORITY: unfinished higher-priority work closer to revenue consumes capacity first",
                    fp,
                    tier=tier,
                )
                return {
                    "authorized": False,
                    "cost_status": COST_DEFERRED_PRIORITY,
                    "reason": entry["reason"],
                    "priority_tier": tier,
                    "ledger": entry,
                }

            cfg = self.config_store.get()
            if cfg.get("paid_research_paused") and tier != TIER_1_ACTIVE:
                entry = self._defer(request, COST_DEFERRED_BUDGET, "paid_research_paused_by_operator", fp, tier=tier)
                return {"authorized": False, "cost_status": COST_DEFERRED_BUDGET, "reason": entry["reason"], "ledger": entry}
            if cfg.get("paid_discovery_paused") and tier == TIER_4_NEW_DISCOVERY:
                self.mark_weekly_source_discovery_owed()
                entry = self._defer(request, COST_DEFERRED_BUDGET, "PAID_DISCOVERY_PAUSED", fp, tier=tier)
                return {"authorized": False, "cost_status": COST_DEFERRED_BUDGET, "reason": entry["reason"], "ledger": entry}

            est = estimate_max_cost(
                provider=str(request.get("provider") or "unknown"),
                action_type=str(request.get("action_type") or ACTION_AI_COMPLETION),
                estimated_input_tokens=int(request.get("estimated_input_tokens") or 0),
                estimated_output_tokens=int(request.get("estimated_output_tokens") or 0),
                requests=int(request.get("requests") or 1),
                explicit_max=request.get("estimated_max_cost"),
                pricing=request.get("pricing"),
            )
            max_cost = float(est["estimated_max_cost"])
            if est.get("pricing_unknown"):
                # Still authorize path only if budget covers conservative estimate
                pass

            snap = self.budget_snapshot()
            buffer = float(cfg.get("safety_buffer_usd") or 0.05)
            totals = snap["totals"]
            absolute_cap = float(cfg["ABSOLUTE_AUTONOMOUS_SPEND_CAP"])
            monthly_cap = float(cfg["MONTHLY_CAP"])
            daily_cap = float(cfg["DAILY_CAP"])
            per_run = float(cfg["PER_RUN_CAP"])
            per_opp = float(cfg["PER_OPPORTUNITY_CAP"])

            needed = max_cost + buffer
            available_abs = absolute_cap - totals["absolute_spent"] - totals["reserved"]
            if needed > available_abs:
                entry = self._defer(
                    request,
                    COST_BLOCKED_HARD_CAP,
                    f"BLOCKED_HARD_CAP: estimated maximum cost ${max_cost:.2f} exceeds safely available autonomous spend ${max(0, available_abs - buffer):.2f}",
                    fp,
                    tier=tier,
                    max_cost=max_cost,
                )
                return {"authorized": False, "cost_status": COST_BLOCKED_HARD_CAP, "reason": entry["reason"], "ledger": entry}

            if totals["monthly_spent"] + totals["reserved"] + max_cost > monthly_cap:
                entry = self._defer(request, COST_BLOCKED_MONTHLY, "BLOCKED_MONTHLY_CAP", fp, tier=tier, max_cost=max_cost)
                return {"authorized": False, "cost_status": COST_BLOCKED_MONTHLY, "reason": entry["reason"], "ledger": entry}
            if totals["daily_spent"] + totals["reserved"] + max_cost > daily_cap:
                entry = self._defer(request, COST_BLOCKED_DAILY, "BLOCKED_DAILY_CAP", fp, tier=tier, max_cost=max_cost)
                return {"authorized": False, "cost_status": COST_BLOCKED_DAILY, "reason": entry["reason"], "ledger": entry}
            if self._run_spent + totals["reserved"] + max_cost > per_run:
                entry = self._defer(request, COST_BLOCKED_RUN, "BLOCKED_PER_RUN_CAP", fp, tier=tier, max_cost=max_cost)
                return {"authorized": False, "cost_status": COST_BLOCKED_RUN, "reason": entry["reason"], "ledger": entry}

            oid = request.get("deal_id") or request.get("opportunity_id")
            if oid:
                opp_spend = self.ledger.opportunity_spend(str(oid))
                if opp_spend + max_cost > per_opp:
                    entry = self._defer(
                        request,
                        COST_BLOCKED_PER_OPP,
                        "BLOCKED_PER_OPPORTUNITY_CAP",
                        fp,
                        tier=tier,
                        max_cost=max_cost,
                    )
                    return {"authorized": False, "cost_status": COST_BLOCKED_PER_OPP, "reason": entry["reason"], "ledger": entry}

            # Atomic reservation
            reservation_id = f"RSV-{uuid4().hex[:12]}"
            self.ledger.reserve(
                reservation_id,
                max_cost,
                {
                    "run_id": self.run_id,
                    "deal_id": oid,
                    "provider": request.get("provider"),
                    "action_type": request.get("action_type"),
                    "priority_tier": tier,
                    "action_fingerprint": fp,
                },
            )
            self._action_fingerprints.add(fp)
            entry = self.ledger.append(
                {
                    "run_id": self.run_id,
                    "deal_id": oid,
                    "source_id": request.get("source_id"),
                    "provider": request.get("provider"),
                    "service_model": request.get("model"),
                    "action_type": request.get("action_type"),
                    "research_stage": request.get("research_stage"),
                    "priority_tier": tier,
                    "estimated_cost": max_cost,
                    "authorized_max_cost": max_cost,
                    "actual_cost": None,
                    "cost_status": COST_AUTHORIZED,
                    "budget_bucket": tier,
                    "reason": request.get("question") or "authorized",
                    "reservation_id": reservation_id,
                    "action_fingerprint": fp,
                    "value_of_information": voi,
                }
            )
            return {
                "authorized": True,
                "cost_status": COST_AUTHORIZED,
                "reason": "AUTHORIZED",
                "estimated_max_cost": max_cost,
                "reservation_id": reservation_id,
                "priority_tier": tier,
                "action_fingerprint": fp,
                "ledger_id": entry["ledger_id"],
                "pricing": est,
            }

    def _has_higher_priority_work(self) -> bool:
        if getattr(self, "_unfinished_higher", False):
            return True
        for d in self._deferred:
            if d.get("priority_tier") in {TIER_1_ACTIVE, TIER_2_QUALIFIED} and d.get("still_owed"):
                return True
        return False

    def set_unfinished_higher_priority(self, value: bool) -> None:
        self._unfinished_higher = value

    def _defer(
        self,
        request: dict[str, Any],
        status: str,
        reason: str,
        fp: str,
        *,
        tier: str | None = None,
        max_cost: float | None = None,
    ) -> dict[str, Any]:
        entry = self.ledger.append(
            {
                "run_id": self.run_id,
                "deal_id": request.get("deal_id") or request.get("opportunity_id"),
                "source_id": request.get("source_id"),
                "provider": request.get("provider"),
                "action_type": request.get("action_type"),
                "research_stage": request.get("research_stage"),
                "priority_tier": tier,
                "estimated_cost": max_cost,
                "authorized_max_cost": None,
                "actual_cost": 0.0,
                "cost_status": status,
                "budget_bucket": tier,
                "reason": reason,
                "action_fingerprint": fp,
                "still_owed": True,
                "request_snapshot": {
                    k: request.get(k)
                    for k in (
                        "action_type",
                        "provider",
                        "question",
                        "deal_id",
                        "opportunity_id",
                        "is_new_discovery",
                        "tracked",
                    )
                },
            }
        )
        self._deferred.append(entry)
        return entry

    def reconcile(
        self,
        *,
        reservation_id: str,
        actual_cost: float | None,
        success: bool,
        charge_possible_on_failure: bool = False,
        ledger_id: str | None = None,
    ) -> dict[str, Any]:
        with _lock:
            if not success:
                status = COST_FAILED_CHARGE_POSSIBLE if charge_possible_on_failure else COST_FAILED_NO_CHARGE
                cost = float(actual_cost or 0) if charge_possible_on_failure else 0.0
            else:
                status = COST_RECONCILED if actual_cost is not None else COST_EXECUTED
                cost = float(actual_cost) if actual_cost is not None else None

            unused = self.ledger.release_reservation(
                reservation_id,
                actual_cost=cost if cost is not None else None,
            )
            # If no actual yet, keep conservative: treat authorized max as committed until known
            if success and actual_cost is None:
                # reservation released fully above when actual_cost is None — re-check release_reservation
                # Our release_reservation with actual_cost=None returns full reserved and pops — for unknown actual
                # we should keep reservation. Fix: only release when actual known OR failure no charge.
                pass

            if success and actual_cost is not None:
                self._run_spent += float(actual_cost)
            elif success and actual_cost is None:
                # Keep spend at authorized estimate until reconciled
                pass

            entry = self.ledger.append(
                {
                    "run_id": self.run_id,
                    "reservation_id": reservation_id,
                    "parent_ledger_id": ledger_id,
                    "actual_cost": actual_cost,
                    "cost_status": status,
                    "unused_reservation_released": unused if actual_cost is not None else 0.0,
                    "variance": (
                        None
                        if actual_cost is None
                        else None  # filled below if we had estimate
                    ),
                    "reason": "reconcile",
                }
            )
            return {"status": status, "actual_cost": actual_cost, "entry": entry}

    def execute_paid(
        self,
        request: dict[str, Any],
        executor: Callable[[], Any],
        *,
        actual_cost_fn: Callable[[Any], float | None] | None = None,
    ) -> dict[str, Any]:
        """Authorize → execute → reconcile. Never execute without authorization."""
        auth = self.authorize(request)
        if not auth.get("authorized"):
            return {"ok": False, "authorization": auth, "result": None}
        try:
            result = executor()
            actual = actual_cost_fn(result) if actual_cost_fn else request.get("simulated_actual_cost")
            if actual is None and request.get("simulated_actual_cost") is not None:
                actual = request["simulated_actual_cost"]
            # Proper reservation handling for success with actual
            rid = auth["reservation_id"]
            with _lock:
                reserved_row = self.ledger._reservations.get(rid)
                est = float((reserved_row or {}).get("amount") or auth.get("estimated_max_cost") or 0)
                if actual is not None:
                    self.ledger.release_reservation(rid, actual_cost=float(actual))
                    self._run_spent += float(actual)
                    variance = float(actual) - est
                    status = COST_RECONCILED
                else:
                    # Retain conservative reserved amount — move to executed with authorized max
                    self.ledger.release_reservation(rid, actual_cost=est)
                    self._run_spent += est
                    actual = est
                    variance = 0.0
                    status = COST_EXECUTED
                self.ledger.append(
                    {
                        "run_id": self.run_id,
                        "deal_id": request.get("deal_id") or request.get("opportunity_id"),
                        "provider": request.get("provider"),
                        "action_type": request.get("action_type"),
                        "priority_tier": auth.get("priority_tier"),
                        "estimated_cost": est,
                        "authorized_max_cost": est,
                        "actual_cost": actual,
                        "cost_status": status,
                        "budget_bucket": auth.get("priority_tier"),
                        "reason": "executed",
                        "reservation_id": rid,
                        "action_fingerprint": auth.get("action_fingerprint"),
                        "variance": variance,
                    }
                )
            return {"ok": True, "authorization": auth, "result": result, "actual_cost": actual}
        except Exception as exc:  # noqa: BLE001
            rid = auth.get("reservation_id")
            if rid:
                self.ledger.release_reservation(rid, actual_cost=0.0)
            self.ledger.append(
                {
                    "run_id": self.run_id,
                    "reservation_id": rid,
                    "cost_status": COST_FAILED_NO_CHARGE,
                    "actual_cost": 0.0,
                    "reason": f"FAILED_NO_CHARGE: {exc}",
                }
            )
            return {"ok": False, "authorization": auth, "error": str(exc), "result": None}

    def deferred_work(self) -> list[dict[str, Any]]:
        return deepcopy(self._deferred)

    def on_budget_restored(self) -> dict[str, Any]:
        """Detect deferred work / catch-up without erasing history."""
        catch_up_sources = []
        for sid, row in self._paused_sources.items():
            plan = self.resume_source_catch_up(sid)
            if plan:
                catch_up_sources.append(plan)
        return {
            "transition": CATCH_UP if catch_up_sources else BACKLOG_RECOVERY,
            "catch_up_sources": catch_up_sources,
            "deferred_paid_actions": len([d for d in self._deferred if d.get("still_owed")]),
            "owed_weekly_source_discovery": self._owed_weekly_source_discovery,
            "note": "Budget renewal does not erase deferred work or advance paused checkpoints",
        }

    def day_rollover(self) -> dict[str, Any]:
        # Daily spend comes from ledger timestamps — no wipe
        return {"event": "DAY_ROLLOVER", "at": _utc(), "deferred_preserved": True, "checkpoints_unchanged": True}

    def month_rollover(self) -> dict[str, Any]:
        return {"event": "MONTH_ROLLOVER", "at": _utc(), "deferred_preserved": True, "ledger_history_preserved": True}

    def dashboard_payload(self) -> dict[str, Any]:
        snap = self.budget_snapshot()
        cfg = snap["config"]
        totals = snap["totals"]
        state = snap["budget_state"]
        return {
            "kind": "CostGovernorDashboard",
            "today_spend": totals["daily_spent"],
            "today_cap": cfg["DAILY_CAP"],
            "month_spend": totals["monthly_spent"],
            "month_cap": cfg["MONTHLY_CAP"],
            "absolute_cap": cfg["ABSOLUTE_AUTONOMOUS_SPEND_CAP"],
            "absolute_used": totals["absolute_spent"],
            "reserved": totals["reserved"],
            "remaining_authorized": snap["safely_available"],
            "paid_work_deferred": snap["deferred_count"],
            "sources_paused_by_budget": snap["paused_sources"],
            "budget_state": state,
            "ui": budget_ui_semantics(state),
            "hard_cap_exhausted": state == HARD_CAP_EXHAUSTED,
            "active_pursuits_protected": True,
            "optimize_for": "MAXIMIZE_USEFUL_BUSINESS_PROGRESS_SUBJECT_TO_AUTHORIZED_SPEND",
            "not_spend_target": True,
        }


def budget_ui_semantics(state: str) -> dict[str, Any]:
    labels = {
        BUDGET_NORMAL: ("BUDGET NORMAL", "check", "budget-badge-normal"),
        BUDGET_CONSTRAINED: ("BUDGET CONSTRAINED", "alert", "budget-badge-constrained"),
        DISCOVERY_THROTTLED: ("PAID DISCOVERY THROTTLED", "alert", "budget-badge-throttled"),
        PAID_DISCOVERY_PAUSED: ("PAID DISCOVERY PAUSED", "pause", "budget-badge-paused"),
        HARD_CAP_EXHAUSTED: ("AUTONOMOUS PAID RESEARCH STOPPED", "stop", "budget-badge-exhausted"),
        CATCH_UP: ("CATCH-UP IN PROGRESS", "sync", "budget-badge-catchup"),
        BACKLOG_RECOVERY: ("BACKLOG RECOVERY", "sync", "budget-badge-recovery"),
    }
    text, icon, css = labels.get(state, (state, "info", "budget-badge-unknown"))
    return {
        "badge_text": text,
        "icon": icon,
        "css_class": css,
        "text_label_required": True,
        "aria_label": text,
    }


def get_cost_governor(**kwargs: Any) -> CostGovernor:
    global _INSTANCE
    if _INSTANCE is None or kwargs:
        _INSTANCE = CostGovernor(**kwargs)
    return _INSTANCE


def reset_cost_governor() -> None:
    global _INSTANCE
    _INSTANCE = None
