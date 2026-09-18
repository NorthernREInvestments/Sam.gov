"""SAM API scarcity: routing order, eligibility gate, budget, audit, cache reuse.

SAM is a scarce late-stage authoritative verification resource — not routine discovery.
"""

from __future__ import annotations
from application_clock import now_utc, today_local

import os
from datetime import datetime, timezone
from typing import Any

from data_integrity import STATUS_VERIFIED

# Permanent source routing order (federal product opportunities)
SOURCE_ROUTE_POSTGRES_LOCAL = "POSTGRES_LOCAL_CACHE"
SOURCE_ROUTE_DIRECT_PUBLIC = "DIRECT_PUBLIC_SOURCES"
SOURCE_ROUTE_OPENAI_WEB = "OPENAI_WEB_RESEARCH"
SOURCE_ROUTE_SPECIALIZED = "SPECIALIZED_AUTHORITATIVE"
SOURCE_ROUTE_SAM_API_LAST = "SAM_API_LAST"

SOURCE_ROUTING_ORDER: tuple[str, ...] = (
    SOURCE_ROUTE_POSTGRES_LOCAL,
    SOURCE_ROUTE_DIRECT_PUBLIC,
    SOURCE_ROUTE_OPENAI_WEB,
    SOURCE_ROUTE_SPECIALIZED,
    SOURCE_ROUTE_SAM_API_LAST,
)

SAM_ELIGIBLE = "ELIGIBLE"
SAM_NOT_ELIGIBLE = "NOT_ELIGIBLE"
SAM_NOT_NEEDED = "NOT_NEEDED"

from federal_dla_constants import PURPOSE_FEDERAL_OPPORTUNITIES_ENUMERATION, PURPOSE_DLA_RECONCILIATION

PURPOSE_BROAD_DISCOVERY = "BROAD_DISCOVERY"
PURPOSE_NAICS_SCAN = "NAICS_SCAN"
PURPOSE_DASHBOARD_LOAD = "DASHBOARD_LOAD"
PURPOSE_OPPORTUNITY_VERIFY = "OPPORTUNITY_VERIFY"
PURPOSE_ATTACHMENT_METADATA = "ATTACHMENT_METADATA"
PURPOSE_NOTICE_DESCRIPTION = "NOTICE_DESCRIPTION"
PURPOSE_AMENDMENT_CHECK = "AMENDMENT_CHECK"
PURPOSE_WATCHLIST_SEARCH = "WATCHLIST_SEARCH"

# Federal Contract Opportunities enumeration is intentional broad SAM use (mission-authorized).
_BROAD_PURPOSES = frozenset(
    {
        PURPOSE_BROAD_DISCOVERY,
        PURPOSE_NAICS_SCAN,
        PURPOSE_WATCHLIST_SEARCH,
        PURPOSE_FEDERAL_OPPORTUNITIES_ENUMERATION,
        PURPOSE_DLA_RECONCILIATION,
    }
)


def sam_scarcity_mode() -> bool:
    """Default ON — treat SAM as scarce late-stage verification."""
    raw = os.getenv("SAM_SCARCITY_MODE", "true").strip().lower()
    return raw in ("1", "true", "yes", "")


def sam_api_call_limit() -> int:
    """Configurable hard SAM API call budget (prefer SAM_API_CALL_LIMIT)."""
    from api_budget import _daily_limit

    if os.getenv("SAM_API_CALL_LIMIT") is not None:
        return _daily_limit("SAM_API_CALL_LIMIT", 10)
    return _daily_limit("SAM_DAILY_API_BUDGET", 10)


def sam_budget_snapshot() -> dict[str, Any]:
    from api_budget import _usage_counts

    counts = _usage_counts()
    used = counts["sam_used_today"]
    limit = sam_api_call_limit()
    return {
        "configured_limit": limit,
        "used": used,
        "remaining": max(0, limit - used),
        "env_keys": ["SAM_API_CALL_LIMIT", "SAM_DAILY_API_BUDGET"],
        "scarcity_mode": sam_scarcity_mode(),
        "LIVE_API_REQUESTS": 0,
    }


def broad_sam_discovery_allowed(*, authorize_broad_sam_discovery: bool = False) -> bool:
    """Broad NAICS/product discovery via SAM is never automatic under scarcity mode."""
    if not sam_scarcity_mode():
        return True
    if not authorize_broad_sam_discovery:
        return False
    # Explicit discovery-network kill switch (default unset/false blocks unless legacy allow)
    disc = os.getenv("SAM_API_BROAD_DISCOVERY_ENABLED")
    if disc is not None and disc.strip().lower() in ("0", "false", "no"):
        return False
    env = os.getenv("SAM_ALLOW_BROAD_DISCOVERY", "false").strip().lower()
    return env in ("1", "true", "yes")


def _gate_status(value: Any) -> str:
    if value is None:
        return "UNKNOWN"
    if isinstance(value, dict):
        st = str(value.get("status") or value.get("gate") or "").upper()
        if st in {"PASS", "FAIL", "UNKNOWN", "UNRESOLVED", "N/A", "NOT_APPLICABLE", "VERIFIED"}:
            if st == "VERIFIED":
                return "PASS"
            if st == "NOT_APPLICABLE":
                return "N/A"
            return st
        if value.get("status") == STATUS_VERIFIED or value.get("verification_status") == STATUS_VERIFIED:
            return "PASS"
        if "value" in value and value.get("status") == STATUS_VERIFIED:
            return "PASS"
        return "UNKNOWN"
    if value is True:
        return "PASS"
    if value is False:
        return "FAIL"
    s = str(value).upper()
    if s in {"PASS", "FAIL", "UNKNOWN", "UNRESOLVED", "N/A", "NOT_APPLICABLE"}:
        return "N/A" if s == "NOT_APPLICABLE" else s
    return "UNKNOWN"


def _blocking(status: str) -> bool:
    return status in {"FAIL", "UNKNOWN", "UNRESOLVED"}


def evaluate_sam_api_eligibility(
    opportunity: Any | None = None,
    *,
    purpose: str | None = None,
    context: dict[str, Any] | None = None,
    exception: dict[str, Any] | None = None,
    authorize_broad_sam_discovery: bool = False,
) -> dict[str, Any]:
    """
    Deterministic SAM API eligibility.

    Returns status ELIGIBLE | NOT_ELIGIBLE | NOT_NEEDED plus reason codes.
    CORE_PRODUCT / title SKU / title qty alone never make a call ELIGIBLE.
    """
    ctx = dict(context or {})
    purpose = (purpose or PURPOSE_OPPORTUNITY_VERIFY).upper()
    reasons: list[str] = []
    checks: dict[str, str] = {}

    if purpose == PURPOSE_DASHBOARD_LOAD:
        return {
            "status": SAM_NOT_NEEDED,
            "sam_api_eligibility": SAM_NOT_NEEDED,
            "sam_api_needed": False,
            "reason_codes": ["dashboard_load_must_not_call_sam"],
            "purpose": purpose,
            "checks": {},
            "exception_applied": False,
            "fact_to_verify": None,
            "cached_evidence_sufficient": True,
        }

    if purpose in _BROAD_PURPOSES:
        if not broad_sam_discovery_allowed(authorize_broad_sam_discovery=authorize_broad_sam_discovery):
            return {
                "status": SAM_NOT_ELIGIBLE,
                "sam_api_eligibility": SAM_NOT_ELIGIBLE,
                "sam_api_needed": False,
                "reason_codes": ["broad_sam_discovery_blocked", f"purpose_{purpose.lower()}"],
                "purpose": purpose,
                "checks": {"broad_discovery": "FAIL"},
                "exception_applied": False,
                "fact_to_verify": None,
                "cached_evidence_sufficient": False,
                "note": "Discover via local/public/OpenAI-web; SAM is late-stage verification only",
            }
        # Explicit rare controlled broad discovery — still not "normal", but allowed when gated.
        return {
            "status": SAM_ELIGIBLE,
            "sam_api_eligibility": SAM_ELIGIBLE,
            "sam_api_needed": True,
            "reason_codes": ["controlled_broad_discovery_explicitly_authorized"],
            "purpose": purpose,
            "checks": {"broad_discovery": "PASS"},
            "exception_applied": False,
            "fact_to_verify": (context or {}).get("requested_fact"),
            "cached_evidence_sufficient": False,
            "note": "Not routine discovery — explicit operator + env authorization only",
        }

    # Cache / local sufficiency for the requested fact
    fact_key = ctx.get("requested_fact")
    if fact_key and local_or_public_satisfies_fact(opportunity, fact_key, ctx):
        return {
            "status": SAM_NOT_NEEDED,
            "sam_api_eligibility": SAM_NOT_NEEDED,
            "sam_api_needed": False,
            "reason_codes": ["fact_already_established_locally_or_public", str(fact_key)],
            "purpose": purpose,
            "checks": {"requested_fact": "PASS"},
            "exception_applied": False,
            "fact_to_verify": fact_key,
            "cached_evidence_sufficient": True,
        }

    # Narrow exception path (must be explicit and complete)
    if exception:
        exc_ok, exc_reasons = _validate_sam_exception(exception)
        if exc_ok:
            return {
                "status": SAM_ELIGIBLE,
                "sam_api_eligibility": SAM_ELIGIBLE,
                "sam_api_needed": True,
                "reason_codes": ["narrow_sam_exception", *exc_reasons],
                "purpose": purpose,
                "checks": {"exception": "PASS"},
                "exception_applied": True,
                "exception": exception,
                "fact_to_verify": exception.get("missing_fact"),
                "cached_evidence_sufficient": False,
            }
        reasons.extend(exc_reasons)

    # Normal product-resale pre-SAM gates
    core_fit = ctx.get("core_fit")
    if core_fit is None and opportunity is not None:
        try:
            from ai_funnel import stage0_evaluate
            from product_deal import resolve_core_fit

            s0 = stage0_evaluate(opportunity)
            core_fit = resolve_core_fit(stage0_classification=s0.get("classification")).get("core_fit")
            ctx.setdefault("stage0_classification", s0.get("classification"))
        except Exception:
            core_fit = "UNKNOWN"
    checks["core_product"] = "PASS" if core_fit == "CORE_PRODUCT" else ("FAIL" if core_fit == "SECONDARY_SERVICE" else "UNKNOWN")

    checks["active_current"] = _gate_status(ctx.get("active_current", _infer_active(opportunity)))
    checks["requirements_understood"] = _gate_status(ctx.get("requirements_understood"))
    checks["quantity_established"] = _gate_status(ctx.get("quantity_established"))
    checks["compliant_product_identified"] = _gate_status(ctx.get("compliant_product_identified"))
    checks["supplier_path_identified"] = _gate_status(ctx.get("supplier_path_identified"))
    checks["acquisition_pricing_established"] = _gate_status(ctx.get("acquisition_pricing_established"))
    checks["supplier_availability"] = _gate_status(ctx.get("supplier_availability"))
    freight_applicable = ctx.get("freight_applicable", True)
    if freight_applicable is False:
        checks["freight"] = "N/A"
    else:
        checks["freight"] = _gate_status(ctx.get("freight_established"))
    checks["delivery_feasibility"] = _gate_status(ctx.get("delivery_feasibility"))
    channel_applicable = ctx.get("manufacturer_channel_applicable", True)
    if channel_applicable is False:
        checks["manufacturer_channel"] = "N/A"
    else:
        checks["manufacturer_channel"] = _gate_status(ctx.get("manufacturer_channel_resolved"))
    coo_applicable = ctx.get("country_of_origin_applicable", True)
    if coo_applicable is False:
        checks["country_of_origin_compliance"] = "N/A"
    else:
        checks["country_of_origin_compliance"] = _gate_status(ctx.get("country_of_origin_resolved"))
    checks["financing_path"] = _gate_status(ctx.get("financing_path_established"))
    checks["no_personal_guarantee"] = _gate_status(ctx.get("no_personal_guarantee"))
    checks["no_personal_credit"] = _gate_status(ctx.get("no_personal_credit"))
    checks["zero_personal_cash_upfront"] = _gate_status(ctx.get("zero_personal_cash_upfront"))
    checks["actual_profit"] = _evaluate_profit_gate(ctx.get("actual_profit"))
    checks["fatal_blocker"] = "FAIL" if ctx.get("fatal_blocker") is True else (
        "PASS" if ctx.get("fatal_blocker") is False else _gate_status(ctx.get("fatal_blocker", "UNKNOWN"))
    )

    # Title SKU / qty are never sufficient — if operator only passed those, still block
    if ctx.get("title_has_part_number") or ctx.get("title_has_quantity"):
        reasons.append("title_identity_insufficient_for_sam")

    for name, status in checks.items():
        if name == "fatal_blocker":
            if status == "FAIL":
                reasons.append("verified_fatal_blocker")
            elif _blocking(status):
                reasons.append("fatal_blocker_unresolved")
            continue
        if status == "N/A":
            continue
        if _blocking(status):
            reasons.append(f"{name}_{status.lower()}")

    if checks.get("actual_profit") == "FAIL" and "actual_profit_below_minimum" not in reasons:
        # distinguish UNKNOWN vs below floor
        profit = ctx.get("actual_profit")
        if isinstance(profit, dict) and profit.get("value") is not None:
            try:
                from economic_integrity import min_actual_profit_usd

                if float(profit["value"]) < float(min_actual_profit_usd()):
                    reasons.append("actual_profit_below_minimum")
            except Exception:
                reasons.append("actual_profit_below_minimum")

    sam_needed = bool(ctx.get("sam_api_needed", False)) or purpose in {
        PURPOSE_OPPORTUNITY_VERIFY,
        PURPOSE_ATTACHMENT_METADATA,
        PURPOSE_NOTICE_DESCRIPTION,
        PURPOSE_AMENDMENT_CHECK,
    }

    if reasons or any(_blocking(s) for n, s in checks.items() if s != "N/A" and n != "fatal_blocker") or checks.get("fatal_blocker") != "PASS":
        # fatal_blocker PASS means no blocker; UNKNOWN/FAIL blocks
        blocking = [r for r in reasons]
        if checks.get("fatal_blocker") in {"FAIL", "UNKNOWN", "UNRESOLVED"}:
            if "fatal_blocker_unresolved" not in blocking and "verified_fatal_blocker" not in blocking:
                blocking.append(f"fatal_blocker_{checks['fatal_blocker'].lower()}")
        return {
            "status": SAM_NOT_ELIGIBLE,
            "sam_api_eligibility": SAM_NOT_ELIGIBLE,
            "sam_api_needed": sam_needed,
            "reason_codes": blocking or ["pre_sam_gates_unresolved"],
            "purpose": purpose,
            "checks": checks,
            "exception_applied": False,
            "fact_to_verify": ctx.get("requested_fact"),
            "cached_evidence_sufficient": False,
            "core_fit": core_fit,
            "note": "CORE_PRODUCT alone does not qualify for SAM API",
        }

    return {
        "status": SAM_ELIGIBLE,
        "sam_api_eligibility": SAM_ELIGIBLE,
        "sam_api_needed": True,
        "reason_codes": ["all_pre_sam_gates_pass"],
        "purpose": purpose,
        "checks": checks,
        "exception_applied": False,
        "fact_to_verify": ctx.get("requested_fact"),
        "cached_evidence_sufficient": False,
        "core_fit": core_fit,
    }


def _evaluate_profit_gate(profit: Any) -> str:
    if profit is None:
        return "UNKNOWN"
    if isinstance(profit, dict):
        st = str(profit.get("status") or "").upper()
        if st in {"UNKNOWN", "UNRESOLVED", ""} and profit.get("value") is None:
            return "UNKNOWN"
        if profit.get("value") is None:
            return "UNKNOWN"
        try:
            from economic_integrity import min_actual_profit_usd

            val = float(profit["value"])
            if st and st not in {STATUS_VERIFIED, "CALCULATED", "PASS"}:
                # only trust verified/calculated numbers
                if st not in {"VERIFIED", "CALCULATED"}:
                    return "UNKNOWN"
            return "PASS" if val >= float(min_actual_profit_usd()) else "FAIL"
        except Exception:
            return "UNKNOWN"
    try:
        from economic_integrity import min_actual_profit_usd

        return "PASS" if float(profit) >= float(min_actual_profit_usd()) else "FAIL"
    except Exception:
        return "UNKNOWN"


def _infer_active(opportunity: Any | None) -> str:
    if opportunity is None:
        return "UNKNOWN"
    due = getattr(opportunity, "due_date", None)
    if due is None:
        return "UNKNOWN"
    try:
        from datetime import date

        return "PASS" if due >= today_local() else "FAIL"
    except Exception:
        return "UNKNOWN"


def _validate_sam_exception(exception: dict[str, Any]) -> tuple[bool, list[str]]:
    required = (
        "missing_fact",
        "why_necessary",
        "why_local_public_insufficient",
        "why_sam_authoritative",
        "reason_code",
    )
    missing = [k for k in required if not str(exception.get(k) or "").strip()]
    if missing:
        return False, [f"exception_incomplete_{m}" for m in missing]
    why = str(exception.get("why_necessary") or "").strip().lower()
    if why in {"need more information", "need more info", "more information"}:
        return False, ["exception_generic_need_more_information_rejected"]
    return True, [str(exception.get("reason_code"))]


def local_or_public_satisfies_fact(
    opportunity: Any | None,
    fact_key: str,
    ctx: dict[str, Any],
) -> bool:
    """True when local cache or known public evidence already establishes the fact."""
    if ctx.get("local_fact_satisfied") is True:
        return True
    cached = ctx.get("cached_sam_facts") or {}
    if isinstance(cached, dict) and fact_key in cached and cached[fact_key] is not None:
        meta = cached.get(f"{fact_key}_meta") or {}
        if meta.get("status") == STATUS_VERIFIED or cached.get(fact_key) is not None:
            if ctx.get("force_sam_refresh"):
                return False
            return True
    if opportunity is None:
        return False
    raw = getattr(opportunity, "sam_raw", None)
    if not isinstance(raw, dict):
        return False
    # Common identity facts already stored from prior SAM retrieval
    identity_map = {
        "notice_id": getattr(opportunity, "notice_id", None) or raw.get("noticeId"),
        "set_aside": getattr(opportunity, "set_aside", None)
        or raw.get("typeOfSetAsideDescription")
        or raw.get("typeOfSetAside"),
        "deadline": getattr(opportunity, "due_date", None) or raw.get("responseDeadLine"),
        "solicitation_number": raw.get("solicitationNumber"),
        "title": getattr(opportunity, "title", None),
        "naics": getattr(opportunity, "naics_code", None) or raw.get("naicsCode"),
        "psc": raw.get("classificationCode"),
        "resource_links": raw.get("resourceLinks"),
    }
    if fact_key in identity_map and identity_map[fact_key] not in (None, "", [], {}):
        return True
    return False


def get_cached_sam_verification(opportunity: Any | None) -> dict[str, Any]:
    """Summarize last authoritative SAM payload already stored locally."""
    if opportunity is None:
        return {"has_cached_sam": False}
    raw = getattr(opportunity, "sam_raw", None)
    if not isinstance(raw, dict) or not raw:
        return {"has_cached_sam": False}
    retrieved_at = raw.get("_sam_retrieved_at") or raw.get("source_retrieved_at")
    return {
        "has_cached_sam": True,
        "retrieved_at": retrieved_at,
        "source": "sam_raw_local",
        "verification_status": STATUS_VERIFIED if retrieved_at or raw.get("noticeId") else "UNKNOWN",
        "notice_id": getattr(opportunity, "notice_id", None) or raw.get("noticeId"),
        "has_resource_links": bool(raw.get("resourceLinks")),
        "has_set_aside": bool(
            getattr(opportunity, "set_aside", None)
            or raw.get("typeOfSetAsideDescription")
            or raw.get("typeOfSetAside")
        ),
        "has_deadline": bool(getattr(opportunity, "due_date", None) or raw.get("responseDeadLine")),
    }


def gate_sam_api_call(
    *,
    purpose: str,
    opportunity: Any | None = None,
    context: dict[str, Any] | None = None,
    exception: dict[str, Any] | None = None,
    authorize_live: bool = False,
    authorize_broad_sam_discovery: bool = False,
    endpoint: str | None = None,
    persist_audit: bool = True,
) -> dict[str, Any]:
    """
    Before EVERY SAM API call: cache → fact exists → public alt → eligibility → budget → audit.
    """
    ctx = dict(context or {})
    purpose_u = purpose.upper()

    # 1–3 cache / public
    eligibility = evaluate_sam_api_eligibility(
        opportunity,
        purpose=purpose_u,
        context=ctx,
        exception=exception,
        authorize_broad_sam_discovery=authorize_broad_sam_discovery,
    )

    # Broad discovery still requires explicit live auth even if env allows
    if purpose_u in _BROAD_PURPOSES and not authorize_live:
        eligibility = {
            **eligibility,
            "status": SAM_NOT_ELIGIBLE,
            "sam_api_eligibility": SAM_NOT_ELIGIBLE,
            "reason_codes": list(eligibility.get("reason_codes") or []) + ["authorize_live_required"],
        }

    allowed = eligibility.get("status") == SAM_ELIGIBLE
    blocked_reason = None
    if not allowed:
        blocked_reason = ",".join(eligibility.get("reason_codes") or ["not_eligible"])

    budget_ok = True
    if allowed:
        from api_budget import can_spend_sam

        # Align spend check with configured limit
        budget_ok = can_spend_sam(1)
        if not budget_ok:
            allowed = False
            blocked_reason = "sam_budget_exhausted"
            eligibility = {
                **eligibility,
                "status": SAM_NOT_ELIGIBLE,
                "sam_api_eligibility": SAM_NOT_ELIGIBLE,
                "reason_codes": list(eligibility.get("reason_codes") or []) + ["sam_budget_exhausted"],
            }

    audit_id = None
    if persist_audit:
        audit_id = record_sam_api_audit(
            contract_id=getattr(opportunity, "id", None) if opportunity is not None else ctx.get("contract_id"),
            purpose=purpose_u,
            eligibility=eligibility,
            authorized=bool(authorize_live and allowed),
            executed=False,
            blocked_reason=None if allowed else blocked_reason,
            endpoint=endpoint,
            useful_new_evidence=None,
        )

    return {
        "allowed": allowed,
        "eligibility": eligibility,
        "budget_ok": budget_ok,
        "budget": sam_budget_snapshot(),
        "audit_id": audit_id,
        "blocked_reason": blocked_reason,
        "LIVE_SAM_API": 0,
    }


def record_sam_api_audit(
    *,
    contract_id: int | None,
    purpose: str,
    eligibility: dict[str, Any],
    authorized: bool,
    executed: bool,
    blocked_reason: str | None = None,
    endpoint: str | None = None,
    useful_new_evidence: bool | None = None,
    result_status: str | None = None,
) -> int | None:
    """Persist SAM API attempt audit row. Never raises to callers."""
    try:
        from database import SessionLocal
        from models import SamApiAudit

        session = SessionLocal()
        try:
            row = SamApiAudit(
                contract_id=contract_id,
                purpose=purpose[:128],
                eligibility_status=str(eligibility.get("status") or "")[:32],
                eligibility_reasons_json=list(eligibility.get("reason_codes") or []),
                sam_api_needed=bool(eligibility.get("sam_api_needed")),
                fact_to_verify=(str(eligibility.get("fact_to_verify"))[:512] if eligibility.get("fact_to_verify") else None),
                authorized=authorized,
                executed=executed,
                blocked_reason=(blocked_reason[:512] if blocked_reason else None),
                useful_new_evidence=useful_new_evidence,
                result_status=(result_status or ("BLOCKED" if not authorized else "PENDING"))[:32],
                endpoint=(endpoint[:1024] if endpoint else None),
                retrieved_at=now_utc() if executed else None,
            )
            session.add(row)
            session.commit()
            return int(row.id)
        finally:
            session.close()
    except Exception:
        return None


def mark_sam_audit_executed(
    audit_id: int | None,
    *,
    useful_new_evidence: bool | None = None,
    result_status: str = "EXECUTED",
) -> None:
    if not audit_id:
        return
    try:
        from database import SessionLocal
        from models import SamApiAudit

        session = SessionLocal()
        try:
            row = session.query(SamApiAudit).filter_by(id=audit_id).first()
            if not row:
                return
            row.executed = True
            row.result_status = result_status[:32]
            row.useful_new_evidence = useful_new_evidence
            row.retrieved_at = now_utc()
            session.commit()
        finally:
            session.close()
    except Exception:
        return


def opportunity_sam_dashboard_fields(opportunity: Any, *, context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Read-only SAM eligibility view for dashboard — never calls SAM."""
    elig = evaluate_sam_api_eligibility(opportunity, purpose=PURPOSE_OPPORTUNITY_VERIFY, context=context)
    cached = get_cached_sam_verification(opportunity)
    return {
        "SAM_API_ELIGIBILITY": elig["sam_api_eligibility"],
        "SAM_API_NEEDED": elig["sam_api_needed"],
        "reason_codes": elig["reason_codes"],
        "last_sam_verification": cached,
        "fact_to_verify": elig.get("fact_to_verify"),
        "cached_evidence_can_satisfy": elig.get("cached_evidence_sufficient"),
        "checks": elig.get("checks"),
        "LIVE_API_REQUESTS": 0,
    }


def fully_qualified_sam_context(**overrides: Any) -> dict[str, Any]:
    """Test/helper: context where all normal pre-SAM gates PASS."""
    base = {
        "core_fit": "CORE_PRODUCT",
        "active_current": "PASS",
        "requirements_understood": "PASS",
        "quantity_established": "PASS",
        "compliant_product_identified": "PASS",
        "supplier_path_identified": "PASS",
        "acquisition_pricing_established": "PASS",
        "supplier_availability": "PASS",
        "freight_applicable": True,
        "freight_established": "PASS",
        "delivery_feasibility": "PASS",
        "manufacturer_channel_applicable": True,
        "manufacturer_channel_resolved": "PASS",
        "country_of_origin_applicable": True,
        "country_of_origin_resolved": "PASS",
        "financing_path_established": "PASS",
        "no_personal_guarantee": "PASS",
        "no_personal_credit": "PASS",
        "zero_personal_cash_upfront": "PASS",
        "actual_profit": {"value": 10000, "status": "CALCULATED"},
        "fatal_blocker": False,
        "sam_api_needed": True,
        "requested_fact": "authoritative_amendment_status",
    }
    base.update(overrides)
    return base
