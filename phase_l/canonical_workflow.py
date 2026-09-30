"""Phase L.10 — single canonical opportunity workflow (auditable state machine).

Live production path. Historical L.* rescues remain for replay only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from application_clock import now_utc
from phase_l.acquisition_lanes import (
    DEEP_RESEARCH_NO_FIXED_COUNT,
    MANUAL_QUEUE_NO_FIXED_CAP,
    STAGE3_NO_ROW_CAP,
)
from phase_l.legacy_cleanup import (
    CANONICAL_FUNNEL_ENTRY,
    CANONICAL_FUNNEL_STAGES,
    assert_canonical_caps,
)

BUILD = "20260928-m3-phase-l13-public-artifact-recovery"

assert STAGE3_NO_ROW_CAP and DEEP_RESEARCH_NO_FIXED_COUNT and MANUAL_QUEUE_NO_FIXED_CAP

# Workflow states (authoritative)
DISCOVERED = "DISCOVERED"
SOURCE_VERIFIED = "SOURCE_VERIFIED"
SOURCE_VERIFICATION_PENDING = "SOURCE_VERIFICATION_PENDING"
PRODUCT_CONFIRMED = "PRODUCT_CONFIRMED"
IDENTITY_RESOLVED = "IDENTITY_RESOLVED"
IDENTITY_EXACT = "IDENTITY_EXACT"
IDENTITY_STRONG = "IDENTITY_STRONG"
IDENTITY_SPEC_DRIVEN = "IDENTITY_SPEC_DRIVEN"
IDENTITY_BRAND_OR_EQUAL = "IDENTITY_BRAND_OR_EQUAL"
IDENTITY_PARTIAL = "IDENTITY_PARTIAL"
IDENTITY_UNRESOLVED = "IDENTITY_UNRESOLVED"
GOV_VALUE_RESEARCHED = "GOV_VALUE_RESEARCHED"
PUBLIC_ARTIFACT_RECOVERY = "PUBLIC_ARTIFACT_RECOVERY"
QUANTITY_RESEARCHED = "QUANTITY_RESEARCHED"
ACQUISITION_CHANNEL_RESOLVED = "ACQUISITION_CHANNEL_RESOLVED"
SUPPLIERS_RESEARCHED = "SUPPLIERS_RESEARCHED"
ACQUISITION_EVIDENCE_RESEARCHED = "ACQUISITION_EVIDENCE_RESEARCHED"
ECONOMICS_COMPUTED = "ECONOMICS_COMPUTED"
EVIDENCE_GRADED = "EVIDENCE_GRADED"
QUOTE_TARGET_CANDIDATE = "QUOTE_TARGET_CANDIDATE"
QUOTE_TARGET_VALIDATED = "QUOTE_TARGET_VALIDATED"
OWNER_REVIEW = "OWNER_REVIEW"
EVIDENCE_EXHAUSTED = "EVIDENCE_EXHAUSTED"
REGISTRATION_REQUIRED = "REGISTRATION_REQUIRED"

WORKFLOW_STATES = (
    DISCOVERED,
    SOURCE_VERIFIED,
    SOURCE_VERIFICATION_PENDING,
    PRODUCT_CONFIRMED,
    IDENTITY_RESOLVED,
    GOV_VALUE_RESEARCHED,
    PUBLIC_ARTIFACT_RECOVERY,
    QUANTITY_RESEARCHED,
    ACQUISITION_CHANNEL_RESOLVED,
    SUPPLIERS_RESEARCHED,
    ACQUISITION_EVIDENCE_RESEARCHED,
    ECONOMICS_COMPUTED,
    EVIDENCE_GRADED,
    QUOTE_TARGET_CANDIDATE,
    QUOTE_TARGET_VALIDATED,
    OWNER_REVIEW,
    EVIDENCE_EXHAUSTED,
    REGISTRATION_REQUIRED,
)

# Allowed forward transitions (simplified DAG)
_ALLOWED: dict[str, set[str]] = {
    DISCOVERED: {SOURCE_VERIFIED, SOURCE_VERIFICATION_PENDING},
    SOURCE_VERIFICATION_PENDING: {SOURCE_VERIFIED, EVIDENCE_EXHAUSTED},
    SOURCE_VERIFIED: {PRODUCT_CONFIRMED},
    PRODUCT_CONFIRMED: {IDENTITY_RESOLVED},
    IDENTITY_RESOLVED: {GOV_VALUE_RESEARCHED, PUBLIC_ARTIFACT_RECOVERY},
    GOV_VALUE_RESEARCHED: {QUANTITY_RESEARCHED, PUBLIC_ARTIFACT_RECOVERY},
    PUBLIC_ARTIFACT_RECOVERY: {
        GOV_VALUE_RESEARCHED,
        QUANTITY_RESEARCHED,
        EVIDENCE_EXHAUSTED,
        REGISTRATION_REQUIRED,
    },
    QUANTITY_RESEARCHED: {ACQUISITION_CHANNEL_RESOLVED},
    ACQUISITION_CHANNEL_RESOLVED: {SUPPLIERS_RESEARCHED},
    SUPPLIERS_RESEARCHED: {ACQUISITION_EVIDENCE_RESEARCHED},
    ACQUISITION_EVIDENCE_RESEARCHED: {ECONOMICS_COMPUTED},
    ECONOMICS_COMPUTED: {EVIDENCE_GRADED},
    EVIDENCE_GRADED: {QUOTE_TARGET_CANDIDATE, EVIDENCE_EXHAUSTED, OWNER_REVIEW},
    QUOTE_TARGET_CANDIDATE: {QUOTE_TARGET_VALIDATED, EVIDENCE_EXHAUSTED, OWNER_REVIEW},
    QUOTE_TARGET_VALIDATED: {OWNER_REVIEW},
    OWNER_REVIEW: set(),
    EVIDENCE_EXHAUSTED: {OWNER_REVIEW, PUBLIC_ARTIFACT_RECOVERY},
    REGISTRATION_REQUIRED: {OWNER_REVIEW, EVIDENCE_EXHAUSTED},
}


def _utc() -> str:
    return now_utc().isoformat()


@dataclass
class TransitionRecord:
    prior_state: str
    next_state: str
    rule_id: str
    evidence_used: list[str] = field(default_factory=list)
    evidence_source: str | None = None
    confidence: str | None = None
    timestamp: str = field(default_factory=_utc)
    missing_evidence: list[str] = field(default_factory=list)
    fallback_path_attempted: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "prior_state": self.prior_state,
            "next_state": self.next_state,
            "rule_id": self.rule_id,
            "evidence_used": self.evidence_used,
            "evidence_source": self.evidence_source,
            "confidence": self.confidence,
            "timestamp": self.timestamp,
            "missing_evidence": self.missing_evidence,
            "fallback_path_attempted": self.fallback_path_attempted,
        }


class CanonicalOpportunityWorkflow:
    """One live production orchestrator — progressive funnel + exact evidence chain."""

    entrypoint = CANONICAL_FUNNEL_ENTRY
    stages = CANONICAL_FUNNEL_STAGES
    build = BUILD

    def __init__(self, opportunity_id: str | None = None):
        assert_canonical_caps()
        self.opportunity_id = opportunity_id
        self.state = DISCOVERED
        self.audit_trail: list[dict[str, Any]] = []
        self.flags: dict[str, Any] = {}
        self.payload: dict[str, Any] = {}

    def transition(
        self,
        next_state: str,
        *,
        rule_id: str,
        evidence_used: list[str] | None = None,
        evidence_source: str | None = None,
        confidence: str | None = None,
        missing_evidence: list[str] | None = None,
        fallback_path_attempted: str | None = None,
        force: bool = False,
    ) -> TransitionRecord:
        prior = self.state
        allowed = _ALLOWED.get(prior, set())
        if next_state not in allowed and not force and next_state != prior:
            # Allow same-state no-op refresh
            if next_state != prior:
                raise ValueError(f"Illegal transition {prior} → {next_state} (rule={rule_id})")
        rec = TransitionRecord(
            prior_state=prior,
            next_state=next_state,
            rule_id=rule_id,
            evidence_used=list(evidence_used or []),
            evidence_source=evidence_source,
            confidence=confidence,
            missing_evidence=list(missing_evidence or []),
            fallback_path_attempted=fallback_path_attempted,
        )
        self.state = next_state
        self.audit_trail.append(rec.to_dict())
        return rec

    def snapshot(self) -> dict[str, Any]:
        return {
            "kind": "CanonicalOpportunityWorkflowSnapshot",
            "build": BUILD,
            "opportunity_id": self.opportunity_id,
            "state": self.state,
            "audit_trail": list(self.audit_trail),
            "flags": dict(self.flags),
            "live_path_only": True,
            "alternate_rescues_active": False,
        }


def classify_identity_state(commercial: dict[str, Any] | None, row: dict[str, Any] | None = None) -> str:
    commercial = commercial or {}
    row = row or {}
    blob = f"{row.get('title') or ''} {row.get('description') or ''}".lower()
    if commercial.get("mpn") or commercial.get("nsn") or row.get("nsn"):
        return IDENTITY_EXACT
    if commercial.get("model") and commercial.get("manufacturer"):
        if "or equal" in blob or "equivalent" in blob:
            return IDENTITY_BRAND_OR_EQUAL
        return IDENTITY_STRONG
    if commercial.get("model") or commercial.get("manufacturer"):
        return IDENTITY_PARTIAL
    if any(x in blob for x in ("shall", "must", "minimum", "specification", "iaw")):
        return IDENTITY_SPEC_DRIVEN
    return IDENTITY_UNRESOLVED


def advance_source_verified(wf: CanonicalOpportunityWorkflow, original: dict[str, Any]) -> None:
    if original.get("original_source_verified"):
        wf.transition(
            SOURCE_VERIFIED,
            rule_id="L10_SOURCE_AUTHORITATIVE_RESOLVED",
            evidence_used=["original_posting_url", "solicitation_number"],
            evidence_source=original.get("original_posting_url"),
            confidence="HIGH",
        )
    else:
        wf.transition(
            SOURCE_VERIFICATION_PENDING,
            rule_id="L10_SOURCE_PENDING",
            missing_evidence=["authoritative_solicitation_url"],
            confidence="LOW",
        )


def advance_through_research(
    wf: CanonicalOpportunityWorkflow,
    *,
    identity_state: str,
    gov_researched: bool,
    qty_researched: bool,
    channel: str | None,
    suppliers_n: int,
    economics_ok: bool,
    graded: bool,
) -> None:
    """Deterministic progression through research states (force for linear pipeline)."""
    if wf.state in {SOURCE_VERIFIED, SOURCE_VERIFICATION_PENDING}:
        if wf.state == SOURCE_VERIFICATION_PENDING:
            # Can still research but flag
            wf.flags["source_pending"] = True
        wf.transition(PRODUCT_CONFIRMED, rule_id="L10_PRODUCT_CLASSIFIED", force=True, confidence="MEDIUM")
    if wf.state == PRODUCT_CONFIRMED:
        wf.transition(
            IDENTITY_RESOLVED,
            rule_id="L10_IDENTITY_CLASSIFIED",
            evidence_used=[identity_state],
            confidence=identity_state,
            force=True,
        )
        wf.flags["identity_state"] = identity_state
    if wf.state == IDENTITY_RESOLVED and gov_researched:
        wf.transition(GOV_VALUE_RESEARCHED, rule_id="L10_GOV_VALUE_DONE", force=True)
    if wf.state == GOV_VALUE_RESEARCHED and qty_researched:
        wf.transition(QUANTITY_RESEARCHED, rule_id="L10_QTY_DONE", force=True)
    if wf.state == QUANTITY_RESEARCHED and channel:
        wf.transition(
            ACQUISITION_CHANNEL_RESOLVED,
            rule_id="L10_CHANNEL_RESOLVED",
            evidence_used=[channel],
            force=True,
        )
    if wf.state == ACQUISITION_CHANNEL_RESOLVED and suppliers_n >= 0:
        wf.transition(
            SUPPLIERS_RESEARCHED,
            rule_id="L10_SUPPLIERS_DONE",
            evidence_used=[f"n={suppliers_n}"],
            force=True,
        )
    if wf.state == SUPPLIERS_RESEARCHED:
        wf.transition(ACQUISITION_EVIDENCE_RESEARCHED, rule_id="L10_ACQ_EVIDENCE", force=True)
    if wf.state == ACQUISITION_EVIDENCE_RESEARCHED and economics_ok:
        wf.transition(ECONOMICS_COMPUTED, rule_id="L10_ECONOMICS", force=True)
    if wf.state == ECONOMICS_COMPUTED and graded:
        wf.transition(EVIDENCE_GRADED, rule_id="L10_GRADED", force=True)


def should_run_public_artifact_recovery(
    *,
    access_mode: str | None = None,
    platform_blocked: bool = False,
    gov_grade: str | None = None,
) -> bool:
    """Trigger PUBLIC_ARTIFACT_RECOVERY before REGISTRATION_REQUIRED / EVIDENCE_EXHAUSTED."""
    from phase_l.auth_access import (
        CAPTCHA_PRESENT,
        PLATFORM_HISTORY_BLOCKED,
        PUBLIC_ANTI_BOT_BLOCKED,
    )
    from phase_l.quality_audit import GOV_VALUE_D
    from phase_l.resilient_fetch import FETCH_JS_EMPTY

    triggers = {
        PUBLIC_ANTI_BOT_BLOCKED,
        CAPTCHA_PRESENT,
        FETCH_JS_EMPTY,
        PLATFORM_HISTORY_BLOCKED,
        "JS_EMPTY",
        "AUTH_REQUIRED",
        "HISTORY_AUTH_REQUIRED",
    }
    if platform_blocked:
        return True
    if access_mode and access_mode in triggers:
        return True
    if gov_grade == GOV_VALUE_D and platform_blocked:
        return True
    return False


def run_public_artifact_recovery_branch(
    wf: CanonicalOpportunityWorkflow,
    row: dict[str, Any],
    *,
    commercial: dict[str, Any] | None = None,
    authorize_live: bool = False,
    platform_blocked: bool = True,
) -> dict[str, Any]:
    """Canonical branch: call PublicArtifactRecovery before auth/registration classification."""
    from phase_l.public_artifact_recovery import run_public_artifact_recovery
    from phase_l.public_artifact_types import (
        FREE_REGISTRATION_STILL_REQUIRED,
        PUBLIC_ARTIFACT_RECOVERED,
        BUYER_ARTIFACT_RECOVERED,
    )

    if wf.state in {IDENTITY_RESOLVED, GOV_VALUE_RESEARCHED, EVIDENCE_EXHAUSTED}:
        wf.transition(
            PUBLIC_ARTIFACT_RECOVERY,
            rule_id="L13_PUBLIC_ARTIFACT_BRANCH",
            evidence_used=["platform_blocked_or_anti_bot"],
            force=True,
            confidence="MEDIUM",
        )
    elif wf.state != PUBLIC_ARTIFACT_RECOVERY:
        wf.transition(
            PUBLIC_ARTIFACT_RECOVERY,
            rule_id="L13_PUBLIC_ARTIFACT_BRANCH",
            force=True,
            confidence="MEDIUM",
        )

    result = run_public_artifact_recovery(
        row,
        commercial=commercial,
        authorize_live=authorize_live,
        platform_blocked=platform_blocked,
    )
    wf.payload["public_artifact_recovery"] = result
    wf.flags["public_artifact_outcome"] = result.get("outcome")

    if result.get("outcome") in {PUBLIC_ARTIFACT_RECOVERED, BUYER_ARTIFACT_RECOVERED}:
        wf.transition(
            GOV_VALUE_RESEARCHED,
            rule_id="L13_ARTIFACT_GOV_RECOVERED",
            evidence_used=[result.get("outcome") or ""],
            evidence_source=(result.get("artifacts") or [{}])[0].get("artifact_url"),
            confidence=str(result.get("grade_after") or ""),
            force=True,
        )
    elif result.get("outcome") == FREE_REGISTRATION_STILL_REQUIRED:
        wf.transition(
            REGISTRATION_REQUIRED,
            rule_id="L13_REGISTRATION_STILL_REQUIRED",
            evidence_used=["public_artifact_exhausted"],
            force=True,
        )
    else:
        wf.transition(
            EVIDENCE_EXHAUSTED,
            rule_id="L13_ARTIFACT_EXHAUSTED",
            evidence_used=[result.get("outcome") or ""],
            force=True,
        )
    return result

