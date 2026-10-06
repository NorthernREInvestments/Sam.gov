"""Part B — BID_READY 17/17 fully wired with evidence + invalidation."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from application_clock import now_utc
from m3_data_root import data_path
from p1_prescale_hardening.models import (
    ACTION_REQUIRED,
    BID_READY_REQUIREMENTS,
    BID_READY_SPEC,
    BID_READY_STORE,
    BUILD,
    FAIL,
    NOT_APPLICABLE,
    PACKAGE_INDEX,
    PASS,
    UNKNOWN,
)


def _req(
    req_id: str,
    status: str,
    *,
    reason: str,
    evidence_source: str | None = None,
    source_document: str | None = None,
    page_section_cell: str | None = None,
    owner_action: str | None = None,
    evaluator: str = "p1_prescale_hardening.bid_ready",
) -> dict[str, Any]:
    return {
        "id": req_id,
        "status": status,
        "reason": reason,
        "evidence_source": evidence_source,
        "source_document": source_document,
        "page_section_cell": page_section_cell,
        "last_evaluated": now_utc().isoformat(),
        "evaluator": evaluator,
        "owner_action": owner_action,
        "clears_gate": status in {PASS, NOT_APPLICABLE},
    }


def evaluate_bid_ready(opportunity_id: str, *, context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Fully wire all 17 gates. UNKNOWN/FAIL/ACTION_REQUIRED → BID_READY false."""
    ctx = context or {}
    pkg = _package_for(opportunity_id)
    docs = (pkg.get("documents") or []) if pkg else []
    amd = (pkg.get("amendment_graph") or {}) if pkg else {}
    fields = ((pkg.get("critical_field_traceability") or {}).get("fields") or {}) if pkg else {}

    # Allow test overrides
    overrides = ctx.get("requirement_overrides") or {}

    results: list[dict[str, Any]] = []

    # 1 Docs processed
    if docs and all(d.get("content_hash") for d in docs):
        results.append(
            _req(
                "ALL_SOLICITATION_DOCS_PROCESSED",
                PASS,
                reason=f"{len(docs)} artifacts hashed and inventoried",
                evidence_source="package_provenance",
                source_document=docs[0].get("filename"),
            )
        )
    elif docs:
        results.append(
            _req(
                "ALL_SOLICITATION_DOCS_PROCESSED",
                ACTION_REQUIRED,
                reason="documents present but not fully hashed",
                owner_action="Re-run package provenance hardening",
            )
        )
    else:
        results.append(
            _req(
                "ALL_SOLICITATION_DOCS_PROCESSED",
                FAIL,
                reason="no package artifacts",
                owner_action="Recover solicitation package",
            )
        )

    # 2 Amendments
    unresolved = amd.get("unresolved_acknowledgments") or []
    if not docs:
        results.append(_req("ALL_AMENDMENTS_PROCESSED_ACKNOWLEDGED", UNKNOWN, reason="no package"))
    elif not any(d.get("is_amendment") for d in docs):
        results.append(
            _req(
                "ALL_AMENDMENTS_PROCESSED_ACKNOWLEDGED",
                PASS,
                reason="no amendments detected; original solicitation current",
                evidence_source="amendment_graph",
            )
        )
    elif unresolved and not ctx.get("amendments_acknowledged"):
        results.append(
            _req(
                "ALL_AMENDMENTS_PROCESSED_ACKNOWLEDGED",
                ACTION_REQUIRED,
                reason=f"active amendment(s) require acknowledgment: {unresolved}",
                owner_action="Acknowledge current amendment",
            )
        )
    else:
        results.append(
            _req(
                "ALL_AMENDMENTS_PROCESSED_ACKNOWLEDGED",
                PASS,
                reason="amendments processed; current version identified",
                evidence_source="amendment_graph",
            )
        )

    # 3 Submission
    if fields.get("submission_portal") or ctx.get("submission_method"):
        results.append(
            _req(
                "SUBMISSION_METHOD_PORTAL_CONFIRMED",
                PASS if ctx.get("submission_method") else ACTION_REQUIRED,
                reason=ctx.get("submission_method")
                or "submission instructions pointer exists — confirm exact portal/email/physical method",
                evidence_source="package_field:submission_portal",
                source_document=(fields.get("submission_portal") or {}).get("filename"),
                owner_action=None if ctx.get("submission_method") else "Confirm exact submission method text",
            )
        )
    else:
        results.append(_req("SUBMISSION_METHOD_PORTAL_CONFIRMED", UNKNOWN, reason="submission method not found"))

    # 4 Deadline timezone
    if ctx.get("deadline") and ctx.get("timezone"):
        results.append(
            _req(
                "DEADLINE_TIMEZONE_CONFIRMED",
                PASS,
                reason=f"deadline={ctx['deadline']} tz={ctx['timezone']}",
                evidence_source="opportunity_deadline",
            )
        )
    elif fields.get("deadline"):
        results.append(
            _req(
                "DEADLINE_TIMEZONE_CONFIRMED",
                ACTION_REQUIRED,
                reason="deadline evidence present; timezone not confirmed",
                source_document=(fields.get("deadline") or {}).get("filename"),
                owner_action="Confirm timezone",
            )
        )
    else:
        results.append(_req("DEADLINE_TIMEZONE_CONFIRMED", UNKNOWN, reason="deadline unknown"))

    # 5 Forms
    results.append(
        _evaluate_forms_sigs(
            "REQUIRED_FORMS_IDENTIFIED",
            ctx,
            fields,
            default_na_reason="no dedicated forms schedule identified beyond package docs",
        )
    )
    # 6 Signatures
    results.append(
        _evaluate_forms_sigs(
            "REQUIRED_SIGNATURES_IDENTIFIED",
            ctx,
            fields,
            default_na_reason="no signature schedule identified; confirm before bid",
        )
    )

    # 7 Pricing schedule
    if fields.get("pricing_schedule") or ctx.get("pricing_schedule_complete"):
        results.append(
            _req(
                "PRICING_SCHEDULE_COMPLETE",
                PASS if ctx.get("pricing_schedule_complete") else ACTION_REQUIRED,
                reason="pricing schedule evidence present"
                if not ctx.get("pricing_schedule_complete")
                else "pricing schedule complete",
                source_document=(fields.get("pricing_schedule") or {}).get("filename"),
                owner_action=None if ctx.get("pricing_schedule_complete") else "Verify every CLIN has bid price field",
            )
        )
    else:
        results.append(_req("PRICING_SCHEDULE_COMPLETE", UNKNOWN, reason="no pricing schedule evidence"))

    # 8 Certs/reps
    results.append(_applicability_gate("CERTIFICATIONS_REPS_CLEARED", ctx, fields, "certs_reps"))
    # 9 Eligibility
    elig = ctx.get("eligibility_status") or "UNKNOWN"
    if elig in {"PASS", "CLEARED", True}:
        results.append(_req("ELIGIBILITY_CLEARED", PASS, reason="eligibility cleared", evidence_source="eligibility_engine"))
    elif elig in {"FAIL", "BLOCKED", False}:
        results.append(_req("ELIGIBILITY_CLEARED", FAIL, reason="fatal eligibility issue"))
    else:
        results.append(_req("ELIGIBILITY_CLEARED", UNKNOWN, reason="eligibility not evaluated"))

    # 10 Delivery
    if ctx.get("delivery_cleared") or (fields.get("delivery_location") and ctx.get("delivery_destination")):
        results.append(_req("DELIVERY_REQUIREMENTS_CLEARED", PASS, reason="delivery destination confirmed"))
    elif fields.get("delivery_location"):
        results.append(
            _req(
                "DELIVERY_REQUIREMENTS_CLEARED",
                ACTION_REQUIRED,
                reason="delivery evidence present; confirm FOB/date/responsibility",
                owner_action="Confirm delivery requirements",
            )
        )
    else:
        results.append(_req("DELIVERY_REQUIREMENTS_CLEARED", UNKNOWN, reason="delivery unknown"))

    # 11 Insurance/bonding — applicability-aware
    results.append(_insurance_bonding(ctx, fields))
    # 12 OEM
    results.append(_oem_auth(ctx, fields))
    # 13 COO
    results.append(_applicability_gate("COUNTRY_OF_ORIGIN_CLEARED", ctx, fields, "coo"))
    # 14 Cyber
    results.append(_applicability_gate("CYBERSECURITY_REQUIREMENTS_CLEARED", ctx, fields, "cyber"))
    # 15 Warranty/inspection
    results.append(_applicability_gate("WARRANTY_INSPECTION_CLEARED", ctx, fields, "warranty_inspection"))
    # 16 Past performance / samples
    results.append(_applicability_gate("PAST_PERFORMANCE_SAMPLES_CATALOGS_CLEARED", ctx, fields, "past_performance"))
    # 17 Economics/financing/execution
    results.append(_economics_gate(ctx))

    # Apply explicit overrides last (for tests)
    by_id = {r["id"]: r for r in results}
    for rid, st in overrides.items():
        if rid in by_id:
            by_id[rid]["status"] = st
            by_id[rid]["clears_gate"] = st in {PASS, NOT_APPLICABLE}
            by_id[rid]["reason"] = f"override:{st}"

    ordered = [by_id[r] for r in BID_READY_REQUIREMENTS if r in by_id]
    bid_ready = all(r["clears_gate"] for r in ordered) and len(ordered) == 17

    owner_override = ctx.get("OWNER_OVERRIDE")
    if owner_override and not bid_ready:
        # Override does NOT alter underlying failed states
        return {
            "opportunity_id": opportunity_id,
            "BID_READY": False,  # still false unless all clear — override is audit-only flag
            "OWNER_OVERRIDE": True,
            "OWNER_OVERRIDE_NOTE": ctx.get("OWNER_OVERRIDE_NOTE") or "override recorded; underlying states unchanged",
            "requirements": ordered,
            "pass_count": sum(1 for r in ordered if r["status"] == PASS),
            "na_count": sum(1 for r in ordered if r["status"] == NOT_APPLICABLE),
            "blocking": [r["id"] for r in ordered if not r["clears_gate"]],
            "evaluated_at": now_utc().isoformat(),
            "wired_count": 17,
        }

    out = {
        "opportunity_id": opportunity_id,
        "BID_READY": bid_ready,
        "OWNER_OVERRIDE": False,
        "requirements": ordered,
        "pass_count": sum(1 for r in ordered if r["status"] == PASS),
        "na_count": sum(1 for r in ordered if r["status"] == NOT_APPLICABLE),
        "blocking": [r["id"] for r in ordered if not r["clears_gate"]],
        "evaluated_at": now_utc().isoformat(),
        "wired_count": 17,
    }
    return out


def _package_for(oid: str) -> dict[str, Any] | None:
    p = data_path(PACKAGE_INDEX)
    if not p.exists():
        return None
    idx = json.loads(p.read_text(encoding="utf-8"))
    return (idx.get("opportunities") or {}).get(oid)


def _evaluate_forms_sigs(req_id: str, ctx: dict[str, Any], fields: dict[str, Any], *, default_na_reason: str) -> dict[str, Any]:
    key = "forms_status" if "FORMS" in req_id else "signatures_status"
    st = ctx.get(key)
    if st == PASS:
        return _req(req_id, PASS, reason="identified and complete")
    if st == NOT_APPLICABLE:
        return _req(req_id, NOT_APPLICABLE, reason=default_na_reason)
    if st == FAIL:
        return _req(req_id, FAIL, reason="required forms/signatures missing")
    # Default: ACTION_REQUIRED until explicitly cleared — but allow N/A via ctx
    if ctx.get("forms_not_applicable") and "FORMS" in req_id:
        return _req(req_id, NOT_APPLICABLE, reason=default_na_reason)
    if ctx.get("signatures_not_applicable") and "SIGNATURES" in req_id:
        return _req(req_id, NOT_APPLICABLE, reason=default_na_reason)
    return _req(req_id, ACTION_REQUIRED, reason="identify mandatory forms/signatures", owner_action="Review package forms")


def _applicability_gate(req_id: str, ctx: dict[str, Any], fields: dict[str, Any], key: str) -> dict[str, Any]:
    st = ctx.get(f"{key}_status")
    applicable = ctx.get(f"{key}_applicable")
    if st == PASS:
        return _req(req_id, PASS, reason=f"{key} cleared")
    if applicable is False or st == NOT_APPLICABLE:
        return _req(req_id, NOT_APPLICABLE, reason=f"{key} not applicable to this solicitation")
    if st == FAIL:
        return _req(req_id, FAIL, reason=f"{key} failed")
    if applicable is True and st != PASS:
        return _req(req_id, ACTION_REQUIRED, reason=f"{key} applicable but unresolved", owner_action=f"Clear {key}")
    # Unknown applicability → UNKNOWN blocks BID_READY (no implicit pass)
    return _req(req_id, UNKNOWN, reason=f"{key} applicability unknown")


def _insurance_bonding(ctx: dict[str, Any], fields: dict[str, Any]) -> dict[str, Any]:
    # Boilerplate presence alone must not fail
    if ctx.get("insurance_bonding_applicable") is False:
        return _req(
            "INSURANCE_BONDING_CLEARED",
            NOT_APPLICABLE,
            reason="insurance/bonding boilerplate present but not required for this bid path",
        )
    if ctx.get("insurance_bonding_status") == PASS:
        return _req("INSURANCE_BONDING_CLEARED", PASS, reason="insurance/bonding requirements cleared")
    if ctx.get("insurance_bonding_applicable") is True:
        return _req(
            "INSURANCE_BONDING_CLEARED",
            ACTION_REQUIRED,
            reason="insurance/bonding required and unresolved",
            owner_action="Obtain certificates / bonding capacity",
        )
    return _req("INSURANCE_BONDING_CLEARED", UNKNOWN, reason="insurance/bonding applicability unknown")


def _oem_auth(ctx: dict[str, Any], fields: dict[str, Any]) -> dict[str, Any]:
    st = ctx.get("oem_authorization")
    if st in {"required", True}:
        if ctx.get("oem_authorization_cleared"):
            return _req("OEM_AUTHORIZATION_CLEARED", PASS, reason="OEM authorization on file")
        return _req("OEM_AUTHORIZATION_CLEARED", ACTION_REQUIRED, reason="OEM authorization required", owner_action="Obtain OEM letter")
    if st in {"not_required", False, NOT_APPLICABLE}:
        return _req("OEM_AUTHORIZATION_CLEARED", NOT_APPLICABLE, reason="OEM authorization not required")
    if st == "conditional":
        return _req("OEM_AUTHORIZATION_CLEARED", ACTION_REQUIRED, reason="conditional OEM auth — confirm", owner_action="Confirm OEM path")
    # UNKNOWN blocks
    return _req("OEM_AUTHORIZATION_CLEARED", UNKNOWN, reason="OEM authorization requirement unknown")


def _economics_gate(ctx: dict[str, Any]) -> dict[str, Any]:
    if ctx.get("economics_state") == "ECONOMICS_READY" and ctx.get("financing_ready") and ctx.get("execution_cleared"):
        if ctx.get("owner_cash_pre_payment_ok", True):
            return _req(
                "ECONOMICS_FINANCING_EXECUTION_CLEARED",
                PASS,
                reason="economics/financing/execution cleared",
                evidence_source="economics_engine",
            )
        return _req(
            "ECONOMICS_FINANCING_EXECUTION_CLEARED",
            FAIL,
            reason="owner cash before government payment violates policy",
        )
    if ctx.get("economics_state") in {"ECONOMICS_NOT_READY", None}:
        return _req(
            "ECONOMICS_FINANCING_EXECUTION_CLEARED",
            ACTION_REQUIRED,
            reason="economics not ready",
            owner_action="Complete acquisition quotes / revenue",
        )
    return _req("ECONOMICS_FINANCING_EXECUTION_CLEARED", UNKNOWN, reason="economics state unknown")


def invalidate_bid_ready(state: dict[str, Any], *, reason: str, event: str) -> dict[str, Any]:
    out = deepcopy(state)
    out["BID_READY"] = False
    out["invalidated"] = True
    out["invalidation_event"] = event
    out["invalidation_reason"] = reason
    out["invalidated_at"] = now_utc().isoformat()
    # Do not flip requirement rows to PASS
    return out


def persist_bid_ready_spec() -> dict[str, Any]:
    spec = {
        "register": "BID_READY_REQUIREMENTS_V1",
        "build": BUILD,
        "requirements": BID_READY_REQUIREMENTS,
        "count": 17,
        "statuses_allowed": [PASS, FAIL, ACTION_REQUIRED, NOT_APPLICABLE, UNKNOWN],
        "hard_rule": "BID_READY true only if all 17 are PASS or NOT_APPLICABLE",
        "wired": True,
        "wired_count": "17/17",
    }
    data_path(BID_READY_SPEC).write_text(json.dumps(spec, indent=2), encoding="utf-8")
    return spec


def evaluate_and_store(opportunity_id: str, *, context: dict[str, Any] | None = None) -> dict[str, Any]:
    result = evaluate_bid_ready(opportunity_id, context=context)
    store = {}
    p = data_path(BID_READY_STORE)
    if p.exists():
        store = json.loads(p.read_text(encoding="utf-8"))
    store.setdefault("by_opportunity", {})[opportunity_id] = result
    store["updated_at"] = now_utc().isoformat()
    store["build"] = BUILD
    data_path(BID_READY_STORE).write_text(json.dumps(store, indent=2, default=str), encoding="utf-8")
    return result
