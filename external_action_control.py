"""Operator authorization + dry-run execution — DEVELOPMENT_NO_OUTREACH hard block."""

from __future__ import annotations

import hashlib
from typing import Any
from uuid import uuid4

from application_clock import now_utc
from commercial_verification_constants import (
    ACT_AGENCY_Q,
    ACT_FINANCING_IND,
    ACT_REGISTER,
    ACT_SIGN,
    ACT_SUBMIT_BID,
    ACT_SUPPLIER_QUOTE,
    ACT_VERIFY_AUTH,
    ACT_VERIFY_STOCK,
    AUD_AUTHORIZED,
    AUD_BLOCKED,
    AUD_EXECUTED,
    AUD_PROPOSED,
    AUD_REJECTED,
    AUD_RESULT,
    AUD_REVOKED,
    AUTHZ_AUTHORIZED,
    AUTHZ_EXPIRED,
    AUTHZ_NOT,
    AUTHZ_REJECTED,
    AUTHZ_REVIEW,
    AUTHZ_REVOKED,
    EXEC_BLOCKED_AUTH,
    EXEC_BLOCKED_FORBIDDEN,
    EXEC_BLOCKED_GOV,
    EXEC_BLOCKED_MODE,
    EXEC_CONTROLLED_AUTHORIZED,
    EXEC_DRY_RUN,
    EXEC_NOT_STARTED,
    FUTURE_ACTION,
)
from cost_governor import get_cost_governor, research_value_decision
from operating_mode import is_controlled_verification, is_development_no_outreach, mode_snapshot

# Allowed only under CONTROLLED_REAL_WORLD_VERIFICATION + per-action operator auth
CONTROLLED_ALLOWED_ACTIONS = {
    ACT_SUPPLIER_QUOTE,
    ACT_VERIFY_STOCK,
    ACT_VERIFY_AUTH,
    ACT_FINANCING_IND,
    ACT_AGENCY_Q,
}

# Never auto-allowed — even in controlled mode
CONTROLLED_FORBIDDEN_ACTIONS = {
    ACT_SUBMIT_BID,
    ACT_REGISTER,
    ACT_SIGN,
}


def _fp(*parts: Any) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:20]


class ExternalActionStore:
    def __init__(self) -> None:
        self._actions: dict[str, dict[str, Any]] = {}
        self._audit: list[dict[str, Any]] = []

    def _audit_event(self, action_id: str, event: str, **extra: Any) -> None:
        self._audit.append(
            {
                "action_id": action_id,
                "event": event,
                "at": now_utc().isoformat(),
                **extra,
            }
        )

    def propose(
        self,
        *,
        opportunity_id: str,
        action_type: str,
        purpose: str,
        target: str | None = None,
        information_to_send: str | None = None,
        information_requested: list[str] | None = None,
        expected_cost: float = 0.0,
        risk: str = "MEDIUM",
        deadline: str | None = None,
        prerequisite: str | None = None,
        proposed_by: str = "m3",
    ) -> dict[str, Any]:
        fingerprint = _fp(opportunity_id, action_type, target, purpose)
        for a in self._actions.values():
            if a.get("fingerprint") == fingerprint and a.get("authorization_state") not in {
                AUTHZ_REJECTED,
                AUTHZ_REVOKED,
                AUTHZ_EXPIRED,
            }:
                return a  # idempotent
        action_id = f"PEA-{uuid4().hex[:10]}"
        action = {
            "kind": "ProposedExternalAction",
            "action_id": action_id,
            "opportunity_id": opportunity_id,
            "action_type": action_type,
            "purpose": purpose,
            "target": target,
            "information_to_send": information_to_send,
            "information_requested": information_requested or [],
            "expected_cost": expected_cost,
            "risk": risk,
            "deadline": deadline,
            "prerequisite": prerequisite,
            "proposed_by": proposed_by,
            "created_at": now_utc().isoformat(),
            "authorization_state": AUTHZ_REVIEW,
            "authorized_by": None,
            "authorized_at": None,
            "execution_state": EXEC_NOT_STARTED,
            "fingerprint": fingerprint,
            "proposed_action_label": FUTURE_ACTION,
            "network_transmitted": False,
        }
        self._actions[action_id] = action
        self._audit_event(action_id, AUD_PROPOSED, action_type=action_type)
        return action

    def authorize(self, action_id: str, *, operator_id: str = "operator") -> dict[str, Any]:
        a = self._actions.get(action_id)
        if not a:
            return {"error": "not_found"}
        a["authorization_state"] = AUTHZ_AUTHORIZED
        a["authorized_by"] = operator_id
        a["authorized_at"] = now_utc().isoformat()
        self._audit_event(action_id, AUD_AUTHORIZED, operator_id=operator_id)
        return a

    def reject(self, action_id: str, *, operator_id: str = "operator", reason: str | None = None) -> dict[str, Any]:
        a = self._actions.get(action_id)
        if not a:
            return {"error": "not_found"}
        a["authorization_state"] = AUTHZ_REJECTED
        a["reject_reason"] = reason
        self._audit_event(action_id, AUD_REJECTED, operator_id=operator_id, reason=reason)
        return a

    def revoke(self, action_id: str, *, operator_id: str = "operator") -> dict[str, Any]:
        a = self._actions.get(action_id)
        if not a:
            return {"error": "not_found"}
        a["authorization_state"] = AUTHZ_REVOKED
        self._audit_event(action_id, AUD_REVOKED, operator_id=operator_id)
        return a

    def get(self, action_id: str) -> dict[str, Any] | None:
        return self._actions.get(action_id)

    def list_for(self, opportunity_id: str) -> list[dict[str, Any]]:
        return [a for a in self._actions.values() if a["opportunity_id"] == opportunity_id]

    def audit(self) -> list[dict[str, Any]]:
        return list(self._audit)


class DryRunExecutionAdapter:
    """Records what WOULD happen — no network transmission."""

    name = "DryRunExecutionAdapter"

    def execute(self, action: dict[str, Any], store: ExternalActionStore) -> dict[str, Any]:
        action_type = str(action.get("action_type") or "")

        # Central hard block in DEVELOPMENT_NO_OUTREACH — even if AUTHORIZED
        if is_development_no_outreach():
            result = {
                "execution_state": EXEC_BLOCKED_MODE,
                "would_have_sent": {
                    "action_type": action.get("action_type"),
                    "target": action.get("target"),
                    "information_to_send": action.get("information_to_send"),
                    "information_requested": action.get("information_requested"),
                },
                "network_transmitted": False,
                "calls_made": 0,
                "emails_sent": 0,
                "quote_requests_made": 0,
                "financing_applications": 0,
                "registrations": 0,
                "bids_submitted": 0,
                "signatures": 0,
                "dry_run": True,
                "mode": mode_snapshot(),
            }
            action["execution_state"] = EXEC_BLOCKED_MODE
            action["dry_run_result"] = result
            store._audit_event(action["action_id"], AUD_BLOCKED, reason=EXEC_BLOCKED_MODE)
            return result

        # CONTROLLED_REAL_WORLD_VERIFICATION — operator-authorized verification only
        if is_controlled_verification():
            if action_type in CONTROLLED_FORBIDDEN_ACTIONS or action_type in {
                "SUBMIT_BID",
                "REGISTER_PORTAL",
                "SIGN_DOCUMENT",
            }:
                result = {
                    "execution_state": EXEC_BLOCKED_FORBIDDEN,
                    "reason": "forbidden_in_controlled_mode",
                    "action_type": action_type,
                    "network_transmitted": False,
                    "bids_submitted": 0,
                    "registrations": 0,
                    "signatures": 0,
                    "financing_applications": 0,
                    "mode": mode_snapshot(),
                }
                action["execution_state"] = EXEC_BLOCKED_FORBIDDEN
                action["dry_run_result"] = result
                store._audit_event(action["action_id"], AUD_BLOCKED, reason=EXEC_BLOCKED_FORBIDDEN)
                return result

            if action.get("authorization_state") != AUTHZ_AUTHORIZED:
                result = {
                    "execution_state": EXEC_BLOCKED_AUTH,
                    "network_transmitted": False,
                    "dry_run": True,
                    "mode": mode_snapshot(),
                }
                action["execution_state"] = EXEC_BLOCKED_AUTH
                store._audit_event(action["action_id"], AUD_BLOCKED, reason=EXEC_BLOCKED_AUTH)
                return result

            if action_type not in CONTROLLED_ALLOWED_ACTIONS:
                result = {
                    "execution_state": EXEC_BLOCKED_FORBIDDEN,
                    "reason": "action_not_in_controlled_allowlist",
                    "action_type": action_type,
                    "network_transmitted": False,
                    "mode": mode_snapshot(),
                }
                action["execution_state"] = EXEC_BLOCKED_FORBIDDEN
                action["dry_run_result"] = result
                store._audit_event(action["action_id"], AUD_BLOCKED, reason=EXEC_BLOCKED_FORBIDDEN)
                return result

            voi = research_value_decision(
                question=str(action.get("purpose") or "external action"),
                could_change_pursuit_or_readiness=True,
            )
            if not voi.get("allow_paid") and float(action.get("expected_cost") or 0) > 0:
                result = {
                    "execution_state": EXEC_BLOCKED_GOV,
                    "reason": voi.get("reason"),
                    "network_transmitted": False,
                }
                action["execution_state"] = EXEC_BLOCKED_GOV
                store._audit_event(action["action_id"], AUD_BLOCKED, reason=EXEC_BLOCKED_GOV)
                return result

            # Authorized controlled gate: operator may gather evidence offline; no auto network
            result = {
                "execution_state": EXEC_CONTROLLED_AUTHORIZED,
                "adapter": self.name,
                "controlled": True,
                "operator_authorized": True,
                "would_have_sent": {
                    "action_type": action.get("action_type"),
                    "target": action.get("target"),
                    "information_to_send": action.get("information_to_send"),
                },
                "network_transmitted": False,
                "automatic_outreach": False,
                "calls_made": 0,
                "emails_sent": 0,
                "quote_requests_made": 0,
                "financing_applications": 0,
                "registrations": 0,
                "bids_submitted": 0,
                "signatures": 0,
                "note": "Operator-authorized verification gate open — record evidence in learning store; no automatic send",
                "mode": mode_snapshot(),
            }
            action["execution_state"] = EXEC_CONTROLLED_AUTHORIZED
            action["dry_run_result"] = result
            store._audit_event(action["action_id"], AUD_EXECUTED, controlled=True, network=False)
            return result

        if action.get("authorization_state") != AUTHZ_AUTHORIZED:
            result = {
                "execution_state": EXEC_BLOCKED_AUTH,
                "network_transmitted": False,
                "dry_run": True,
            }
            action["execution_state"] = EXEC_BLOCKED_AUTH
            store._audit_event(action["action_id"], AUD_BLOCKED, reason=EXEC_BLOCKED_AUTH)
            return result

        # Cost governor check for paid verification
        voi = research_value_decision(
            question=str(action.get("purpose") or "external action"),
            could_change_pursuit_or_readiness=True,
        )
        if not voi.get("allow_paid") and float(action.get("expected_cost") or 0) > 0:
            result = {
                "execution_state": EXEC_BLOCKED_GOV,
                "reason": voi.get("reason"),
                "network_transmitted": False,
            }
            action["execution_state"] = EXEC_BLOCKED_GOV
            store._audit_event(action["action_id"], AUD_BLOCKED, reason=EXEC_BLOCKED_GOV)
            return result

        # In OPERATIONAL mode this would call real adapters — still dry-run foundation
        result = {
            "execution_state": EXEC_DRY_RUN,
            "adapter": self.name,
            "would_have_sent": {
                "action_type": action.get("action_type"),
                "target": action.get("target"),
                "information_to_send": action.get("information_to_send"),
            },
            "network_transmitted": False,
            "dry_run": True,
        }
        action["execution_state"] = EXEC_DRY_RUN
        action["dry_run_result"] = result
        store._audit_event(action["action_id"], AUD_EXECUTED, dry_run=True)
        return result


# Adapter stubs for future live integrations (not implemented)
class SupplierQuoteAdapter:
    def execute(self, *_a: Any, **_k: Any) -> dict[str, Any]:
        raise RuntimeError("live SupplierQuoteAdapter not implemented — use DryRunExecutionAdapter")


class FinancingInquiryAdapter:
    def execute(self, *_a: Any, **_k: Any) -> dict[str, Any]:
        raise RuntimeError("live FinancingInquiryAdapter not implemented — use DryRunExecutionAdapter")


class AgencyQuestionAdapter:
    def execute(self, *_a: Any, **_k: Any) -> dict[str, Any]:
        raise RuntimeError("live AgencyQuestionAdapter not implemented")


class RegistrationAdapter:
    def execute(self, *_a: Any, **_k: Any) -> dict[str, Any]:
        raise RuntimeError("live RegistrationAdapter not implemented")


class BidSubmissionAdapter:
    def execute(self, *_a: Any, **_k: Any) -> dict[str, Any]:
        raise RuntimeError("live BidSubmissionAdapter not implemented")


_STORE: ExternalActionStore | None = None


def get_external_action_store() -> ExternalActionStore:
    global _STORE
    if _STORE is None:
        _STORE = ExternalActionStore()
    return _STORE


def reset_external_action_store() -> None:
    global _STORE
    _STORE = ExternalActionStore()


def execute_external_action(action_id: str, *, adapter: DryRunExecutionAdapter | None = None) -> dict[str, Any]:
    """Central execution entry — hard-blocks in DEVELOPMENT_NO_OUTREACH."""
    store = get_external_action_store()
    action = store.get(action_id)
    if not action:
        return {"error": "not_found"}
    # Idempotent: if already blocked/dry-run/controlled, return prior
    if action.get("execution_state") in {
        EXEC_BLOCKED_MODE,
        EXEC_DRY_RUN,
        EXEC_CONTROLLED_AUTHORIZED,
        EXEC_BLOCKED_FORBIDDEN,
    } and action.get("dry_run_result"):
        return action["dry_run_result"]
    ad = adapter or DryRunExecutionAdapter()
    return ad.execute(action, store)
