"""Tracked solicitations, change events, monitoring tiers, operator acknowledgment."""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from uuid import uuid4

from application_clock import now_utc
from national_discovery_constants import (
    CHG_AMENDMENT,
    CHG_CANCELLED,
    CHG_DEADLINE,
    CHG_PACKAGE,
    CHG_QA,
    INV_AWAITING,
    INV_AWARDED,
    INV_BID_PREP,
    INV_CANCELLED,
    INV_CLARIFICATION,
    INV_NOT_AWARDED,
    INV_POST_AMENDMENT,
    INV_PURSUIT_UNCERTAIN,
    INV_PURSUIT_WORTHY,
    INV_SUBMITTED,
    INV_UNKNOWN_RESULT,
    MON_ACTIVE,
    MON_BID_PREP,
    MON_DEADLINE_CRITICAL,
    MON_NORMAL,
    MON_POST_SUBMISSION,
    NOT_READY_UNREVIEWED_CHANGE,
    READY_FOR_SUBMISSION,
    REVERIFY_IF_PURSUED,
    SEV_CRITICAL,
    SEV_INFO,
    SEV_MATERIAL,
    SEV_NONMATERIAL,
    SEV_UNKNOWN,
)

# Dependency maps — targeted invalidation
DEPENDENCY_MAP = {
    "QUANTITY_CHANGED": [
        "supplier_quote",
        "freight",
        "cost_totals",
        "profit",
        "working_capital",
        "funding_requirement",
        "bid_pricing",
    ],
    "PRODUCT_SPEC_CHANGED": [
        "product_compliance",
        "selected_product",
        "supplier_fit",
        "supplier_quote",
        "cost",
        "economics",
        "delivery_lead_time",
    ],
    "DEADLINE_CHANGED": [
        "deadline_intelligence",
        "priority",
        "monitoring_intensity",
        "future_action_timing",
    ],
    "SUBMISSION_FORM_CHANGED": [
        "bid_form_readiness",
        "submission_package_readiness",
    ],
    "AMENDMENT_ADDED": [
        "amendment_analysis",
        "acknowledgment_requirement",
        "affected_dependency_evaluation",
    ],
    "PRICING_FORM_REPLACED": [
        "bid_pricing",
        "submission_package_readiness",
        "supplier_quote",
    ],
    "QA_ADDED": [
        "requirement_interpretation",
        "compliance",
    ],
    "CANCELLED": [
        "pursuit",
        "bid_prep",
        "submission",
    ],
}


def _utc() -> str:
    return now_utc().isoformat()


def monitoring_tier_for_stage(stage: str, *, days_to_deadline: float | None = None) -> str:
    if stage in {INV_SUBMITTED, INV_AWAITING, INV_POST_AMENDMENT, INV_CLARIFICATION}:
        return MON_POST_SUBMISSION
    if stage == INV_BID_PREP:
        return MON_BID_PREP
    if days_to_deadline is not None and days_to_deadline <= 3:
        return MON_DEADLINE_CRITICAL
    if stage in {INV_PURSUIT_WORTHY, INV_PURSUIT_UNCERTAIN}:
        return MON_ACTIVE
    return MON_NORMAL


def classify_change_severity(change_type: str, *, details: dict[str, Any] | None = None) -> str:
    details = details or {}
    if change_type in {CHG_CANCELLED, "MANDATORY_QUALIFICATION_ADDED", "SUBMISSION_METHOD_CHANGED"}:
        return SEV_CRITICAL
    if change_type == CHG_DEADLINE and details.get("moved_earlier"):
        return SEV_CRITICAL
    if change_type in {
        CHG_AMENDMENT,
        "QUANTITY_CHANGED",
        "PRODUCT_SPEC_CHANGED",
        "PRICING_FORM_REPLACED",
        CHG_QA,
        "DELIVERY_CHANGED",
    }:
        return SEV_MATERIAL
    if change_type in {"METADATA_ONLY", "FORMATTING"}:
        return SEV_NONMATERIAL
    if change_type in {CHG_PACKAGE, "STATUS_CHANGED"}:
        return SEV_MATERIAL
    if change_type == "UNKNOWN_CHANGE":
        return SEV_UNKNOWN
    return SEV_INFO


def build_change_event(
    *,
    deal_id: str,
    change_type: str,
    old_value: Any = None,
    new_value: Any = None,
    source: str = "authoritative_monitor",
    amendment_id: str | None = None,
    details: dict[str, Any] | None = None,
    test_only: bool = False,
) -> dict[str, Any]:
    severity = classify_change_severity(change_type, details=details)
    deps = DEPENDENCY_MAP.get(change_type, DEPENDENCY_MAP.get("AMENDMENT_ADDED" if change_type == CHG_AMENDMENT else "", []))
    if change_type == CHG_AMENDMENT:
        deps = DEPENDENCY_MAP["AMENDMENT_ADDED"]
    human_review = severity in {SEV_CRITICAL, SEV_MATERIAL, SEV_UNKNOWN}
    return {
        "kind": "SolicitationChangeEvent",
        "change_id": f"CHG-{uuid4().hex[:12]}",
        "deal_id": deal_id,
        "detected_at": _utc(),
        "effective_source_time": (details or {}).get("effective_source_time"),
        "change_type": change_type,
        "severity": severity,
        "old_value": old_value,
        "new_value": new_value,
        "source": source,
        "amendment_id": amendment_id,
        "materiality": severity,
        "affected_dependencies": list(deps),
        "automatic_actions_taken": [],
        "human_review_required": human_review,
        "m3_processed": False,
        "processed_at": None,
        "operator_reviewed": False,
        "acknowledged_at": None,
        "acknowledged_by": None,
        "notes": (details or {}).get("notes"),
        "test_only": test_only,
        "timeline": [{"at": _utc(), "event": "change_detected", "change_type": change_type}],
    }


def invalidate_dependencies(change: dict[str, Any], deal_state: dict[str, Any]) -> dict[str, Any]:
    """Targeted invalidation only — mark artifacts for reverify/recompute."""
    invalidated = []
    reverify = []
    state = deepcopy(deal_state)
    for dep in change.get("affected_dependencies") or []:
        invalidated.append(dep)
        if dep in {"supplier_quote", "funding_requirement", "product_compliance"}:
            state[dep] = {"status": REVERIFY_IF_PURSUED, "invalidated_by": change["change_id"]}
            reverify.append(dep)
        else:
            state[dep] = {"status": "INVALIDATED", "invalidated_by": change["change_id"]}
    change = dict(change)
    change["automatic_actions_taken"] = list(change.get("automatic_actions_taken") or []) + [
        {"action": "DEPENDENCY_INVALIDATION", "invalidated": invalidated, "reverify": reverify}
    ]
    change["timeline"] = list(change.get("timeline") or []) + [
        {"at": _utc(), "event": "dependencies_invalidated", "deps": invalidated}
    ]
    return {"change": change, "deal_state": state, "invalidated": invalidated, "reverify": reverify}


def automatic_reprocess(change: dict[str, Any], deal_state: dict[str, Any]) -> dict[str, Any]:
    """Rerun what M3 can do locally after material change — no outreach."""
    actions = []
    state = deepcopy(deal_state)
    ctype = change.get("change_type")
    if ctype in {"QUANTITY_CHANGED", CHG_AMENDMENT}:
        state["bom"] = {"status": "UPDATED", "note": "quantities_refreshed_from_change"}
        state["preliminary_cost"] = {"status": "RECALCULATED_IF_EVIDENCE", "note": "screening_only"}
        state["freight"] = {"status": "RANGE_REFRESHED"}
        state["economics"] = {"status": "RECALCULATED_PRELIMINARY"}
        state["working_capital"] = {"status": "REFRESHED_ESTIMATE"}
        state["pursuit_priority"] = {"status": "RERANKED"}
        actions.extend(["update_bom", "refresh_cost", "refresh_freight", "refresh_economics", "rerank"])
    if ctype == "PRODUCT_SPEC_CHANGED":
        state["product_compliance"] = {"status": REVERIFY_IF_PURSUED}
        state["supplier_fit"] = {"status": "REVIEW_REQUIRED"}
        actions.extend(["flag_compliance_reverify", "flag_supplier_review"])
    if ctype == CHG_DEADLINE:
        state["deadline_intelligence"] = {"status": "UPDATED", "deadline": change.get("new_value")}
        state["monitoring_tier"] = monitoring_tier_for_stage(
            state.get("stage") or INV_PURSUIT_WORTHY,
            days_to_deadline=(change.get("details") or {}).get("days_to_deadline"),
        )
        state["pursuit_priority"] = {"status": "RERANKED"}
        actions.extend(["update_deadline", "adjust_monitoring_tier", "rerank"])
    if ctype == CHG_CANCELLED:
        state["stage"] = INV_CANCELLED
        actions.append("mark_cancelled")
    if ctype == CHG_QA:
        state["qa"] = {"status": "APPENDED", "note": change.get("new_value")}
        actions.append("ingest_qa")
    if ctype == "PRICING_FORM_REPLACED":
        state["pricing_form"] = {"status": "REPLACED", "requires": REVERIFY_IF_PURSUED}
        actions.append("replace_pricing_form")

    change = dict(change)
    change["m3_processed"] = True
    change["processed_at"] = _utc()
    # CRITICAL: machine processed ≠ operator reviewed
    change["operator_reviewed"] = False
    change["automatic_actions_taken"] = list(change.get("automatic_actions_taken") or []) + [
        {"action": "AUTOMATIC_REPROCESS", "steps": actions}
    ]
    change["timeline"] = list(change.get("timeline") or []) + [
        {"at": _utc(), "event": "m3_reprocessed", "steps": actions}
    ]
    return {"change": change, "deal_state": state, "actions": actions}


def acknowledge_change(change: dict[str, Any], *, operator_id: str = "operator") -> dict[str, Any]:
    c = dict(change)
    c["operator_reviewed"] = True
    c["acknowledged_at"] = _utc()
    c["acknowledged_by"] = operator_id
    c["timeline"] = list(c.get("timeline") or []) + [
        {"at": _utc(), "event": "operator_acknowledged", "by": operator_id}
    ]
    return c


def readiness_after_changes(
    *,
    base_readiness: str | None,
    unreviewed_changes: list[dict[str, Any]],
) -> dict[str, Any]:
    blocking = [
        c
        for c in unreviewed_changes
        if c.get("severity") in {SEV_CRITICAL, SEV_MATERIAL, SEV_UNKNOWN} and not c.get("operator_reviewed")
    ]
    if blocking and base_readiness == READY_FOR_SUBMISSION:
        return {
            "readiness": NOT_READY_UNREVIEWED_CHANGE,
            "previous": base_readiness,
            "blocking_changes": [c["change_id"] for c in blocking],
            "reason": "unreviewed_material_or_critical_change",
        }
    return {"readiness": base_readiness or "NOT_READY", "blocking_changes": []}


class TrackedSolicitationStore:
    def __init__(self) -> None:
        self._tracked: dict[str, dict[str, Any]] = {}
        self._changes: list[dict[str, Any]] = []

    def promote(
        self,
        deal_id: str,
        *,
        stage: str = INV_PURSUIT_WORTHY,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        row = {
            "kind": "TrackedSolicitation",
            "deal_id": deal_id,
            "tracked": True,
            "stage": stage,
            "monitoring_tier": monitoring_tier_for_stage(stage),
            "promoted_at": _utc(),
            "meta": meta or {},
            "deal_state": {},
            "readiness": "NOT_READY",
            "post_submission": None,
        }
        self._tracked[deal_id] = row
        return deepcopy(row)

    def get(self, deal_id: str) -> dict[str, Any] | None:
        return deepcopy(self._tracked.get(deal_id))

    def all_tracked(self) -> list[dict[str, Any]]:
        return [deepcopy(t) for t in self._tracked.values()]

    def apply_change(self, change: dict[str, Any]) -> dict[str, Any]:
        deal_id = change["deal_id"]
        tracked = self._tracked.get(deal_id) or self.promote(deal_id, stage=INV_PURSUIT_UNCERTAIN)
        inv = invalidate_dependencies(change, tracked.get("deal_state") or {})
        reproc = automatic_reprocess(inv["change"], inv["deal_state"])
        tracked["deal_state"] = reproc["deal_state"]
        if change.get("change_type") == CHG_CANCELLED:
            tracked["stage"] = INV_CANCELLED
        final_change = reproc["change"]
        self._changes.append(final_change)
        tracked["readiness"] = readiness_after_changes(
            base_readiness=tracked.get("readiness") if tracked.get("readiness") != READY_FOR_SUBMISSION else READY_FOR_SUBMISSION,
            unreviewed_changes=self.unreviewed_for(deal_id) + [final_change],
        )["readiness"]
        # If was ready, knock down
        if any(
            c.get("severity") in {SEV_CRITICAL, SEV_MATERIAL, SEV_UNKNOWN} and not c.get("operator_reviewed")
            for c in self.unreviewed_for(deal_id) + [final_change]
        ):
            if tracked.get("readiness") == READY_FOR_SUBMISSION or tracked.get("meta", {}).get("was_ready"):
                tracked["readiness"] = NOT_READY_UNREVIEWED_CHANGE
        self._tracked[deal_id] = tracked
        return {"tracked": deepcopy(tracked), "change": final_change}

    def acknowledge(self, change_id: str, *, operator_id: str = "operator") -> dict[str, Any]:
        for i, c in enumerate(self._changes):
            if c["change_id"] == change_id:
                self._changes[i] = acknowledge_change(c, operator_id=operator_id)
                deal_id = c["deal_id"]
                # Refresh readiness
                tracked = self._tracked.get(deal_id)
                if tracked:
                    still = self.unreviewed_for(deal_id)
                    if not still and tracked.get("readiness") == NOT_READY_UNREVIEWED_CHANGE:
                        tracked["readiness"] = "NOT_READY"  # still needs freshness gate etc.
                    self._tracked[deal_id] = tracked
                return deepcopy(self._changes[i])
        return {"error": "change_not_found"}

    def unreviewed_for(self, deal_id: str | None = None) -> list[dict[str, Any]]:
        rows = [
            c
            for c in self._changes
            if c.get("human_review_required")
            and not c.get("operator_reviewed")
            and c.get("severity") in {SEV_CRITICAL, SEV_MATERIAL, SEV_UNKNOWN}
        ]
        if deal_id:
            rows = [c for c in rows if c["deal_id"] == deal_id]
        return deepcopy(rows)

    def changes_requiring_review(self) -> dict[str, Any]:
        rows = self.unreviewed_for()
        # Rank: severity, deadline proximity (if known), change age
        sev_order = {SEV_CRITICAL: 0, SEV_MATERIAL: 1, SEV_UNKNOWN: 2}

        def sk(c: dict[str, Any]) -> tuple:
            return (sev_order.get(c.get("severity"), 9), c.get("detected_at") or "")

        rows.sort(key=sk)
        return {
            "count": len(rows),
            "changes": rows,
            "label": f"Changes requiring review: {len(rows)}",
        }

    def timeline_for(self, deal_id: str) -> list[dict[str, Any]]:
        events = []
        for c in self._changes:
            if c["deal_id"] != deal_id:
                continue
            events.extend(c.get("timeline") or [])
        events.sort(key=lambda e: e.get("at") or "")
        return events

    def mark_submitted(self, deal_id: str) -> dict[str, Any]:
        t = self._tracked[deal_id]
        t["stage"] = INV_SUBMITTED
        t["monitoring_tier"] = MON_POST_SUBMISSION
        t["post_submission"] = {
            "status": INV_AWAITING,
            "submitted_at": _utc(),
            "possible_states": [
                INV_SUBMITTED,
                INV_POST_AMENDMENT,
                INV_CLARIFICATION,
                INV_AWAITING,
                INV_AWARDED,
                INV_NOT_AWARDED,
                INV_CANCELLED,
                INV_UNKNOWN_RESULT,
            ],
        }
        return deepcopy(t)

    def ui_severity_payload(self, change: dict[str, Any]) -> dict[str, Any]:
        """Visual semantics — color alone is NOT sufficient."""
        sev = change.get("severity")
        if sev == SEV_CRITICAL:
            return {
                "badge_text": "CRITICAL UPDATE — UNREVIEWED" if not change.get("operator_reviewed") else "CRITICAL — REVIEWED",
                "icon": "alert-octagon",
                "css_class": "change-alert change-alert-critical",
                "border_class": "change-border-critical",
                "aria_label": "Critical solicitation update requires review",
                "color_token": "danger",
                "text_label_required": True,
            }
        if sev == SEV_MATERIAL:
            return {
                "badge_text": "MATERIAL UPDATE — UNREVIEWED" if not change.get("operator_reviewed") else "UPDATED — REVIEWED",
                "icon": "alert-triangle",
                "css_class": "change-alert change-alert-material",
                "border_class": "change-border-material",
                "aria_label": "Material solicitation update requires review",
                "color_token": "warning",
                "text_label_required": True,
            }
        return {
            "badge_text": "UPDATE" if change.get("operator_reviewed") else "INFORMATIONAL UPDATE",
            "icon": "info",
            "css_class": "change-alert change-alert-info",
            "border_class": "change-border-info",
            "aria_label": "Informational solicitation update",
            "color_token": "info",
            "text_label_required": True,
        }


class TrackedSolicitationMonitor:
    """Higher-priority authoritative monitoring for tracked deals."""

    def __init__(self, store: TrackedSolicitationStore) -> None:
        self.store = store

    def compare_versions(self, deal_id: str, previous: dict[str, Any], current: dict[str, Any], *, test_only: bool = False) -> list[dict[str, Any]]:
        events = []
        if previous.get("quantity") != current.get("quantity") and current.get("quantity") is not None:
            events.append(
                build_change_event(
                    deal_id=deal_id,
                    change_type="QUANTITY_CHANGED",
                    old_value=previous.get("quantity"),
                    new_value=current.get("quantity"),
                    test_only=test_only,
                )
            )
        if previous.get("deadline") != current.get("deadline") and current.get("deadline"):
            events.append(
                build_change_event(
                    deal_id=deal_id,
                    change_type=CHG_DEADLINE,
                    old_value=previous.get("deadline"),
                    new_value=current.get("deadline"),
                    details={"moved_earlier": str(current["deadline"]) < str(previous.get("deadline") or "9999")},
                    test_only=test_only,
                )
            )
        if previous.get("specification_hash") != current.get("specification_hash") and current.get("specification_hash"):
            events.append(
                build_change_event(
                    deal_id=deal_id,
                    change_type="PRODUCT_SPEC_CHANGED",
                    old_value=previous.get("specification_hash"),
                    new_value=current.get("specification_hash"),
                    test_only=test_only,
                )
            )
        if previous.get("pricing_form_id") != current.get("pricing_form_id") and current.get("pricing_form_id"):
            events.append(
                build_change_event(
                    deal_id=deal_id,
                    change_type="PRICING_FORM_REPLACED",
                    old_value=previous.get("pricing_form_id"),
                    new_value=current.get("pricing_form_id"),
                    test_only=test_only,
                )
            )
        if current.get("qa_id") and current.get("qa_id") != previous.get("qa_id"):
            events.append(
                build_change_event(
                    deal_id=deal_id,
                    change_type=CHG_QA,
                    old_value=previous.get("qa_id"),
                    new_value=current.get("qa_id"),
                    test_only=test_only,
                )
            )
        if current.get("amendment_id") and current.get("amendment_id") != previous.get("amendment_id"):
            events.append(
                build_change_event(
                    deal_id=deal_id,
                    change_type=CHG_AMENDMENT,
                    amendment_id=current.get("amendment_id"),
                    old_value=previous.get("amendment_id"),
                    new_value=current.get("amendment_id"),
                    test_only=test_only,
                )
            )
        if str(current.get("status") or "").upper() in {"CANCELLED", "CANCELED"}:
            events.append(
                build_change_event(
                    deal_id=deal_id,
                    change_type=CHG_CANCELLED,
                    old_value=previous.get("status"),
                    new_value=current.get("status"),
                    test_only=test_only,
                )
            )
        if current.get("unknown_delta") and not events:
            events.append(
                build_change_event(
                    deal_id=deal_id,
                    change_type="UNKNOWN_CHANGE",
                    details={"notes": "fingerprint_changed_fields_unclear"},
                    test_only=test_only,
                )
            )
        if current.get("metadata_only") and not events:
            events.append(
                build_change_event(
                    deal_id=deal_id,
                    change_type="METADATA_ONLY",
                    details={"notes": "non_impacting_administrative"},
                    test_only=test_only,
                )
            )
        applied = []
        for ev in events:
            applied.append(self.store.apply_change(ev)["change"])
        return applied
