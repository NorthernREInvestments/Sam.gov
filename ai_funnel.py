"""Funnel Stage 0 (free) deterministic filters + escalation framework.

NO OpenAI calls in this module.

Principle: REJECT only with HIGH confidence. When uncertain → ADVANCE (or REVIEW).
"""

from __future__ import annotations
from application_clock import now_utc, today_local

import os
import re
from datetime import date, datetime, timezone
from typing import Any

from ai_model_router import FunnelStage
from company_eligibility import set_aside_eligibility
from data_integrity import (
    STATUS_ASSESSMENT,
    STATUS_SOURCE_CONFLICT,
    STATUS_UNPROVEN_LEGACY,
    STATUS_VERIFIED,
    can_hard_reject_on_fact,
    make_fact,
    parse_money,
)
from legacy_data_integrity import classify_estimated_value


# Opportunity classifications (Stage 0 / Stage 1 shared vocabulary)
CLASS_PRODUCT_RESELL = "PRODUCT_RESELL"
CLASS_PRODUCT_PLUS_SERVICE = "PRODUCT_PLUS_SERVICE"
CLASS_SUBCONTRACTABLE_SERVICE = "SUBCONTRACTABLE_SERVICE"
CLASS_LABOR_HEAVY = "LABOR_HEAVY"
CLASS_SPECIALIST_SERVICE = "SPECIALIST_SERVICE"
CLASS_CONSTRUCTION = "CONSTRUCTION"
CLASS_UNKNOWN = "UNKNOWN"

SOURCE_TYPES = frozenset(
    {"FEDERAL", "STATE", "LOCAL", "UNIVERSITY", "SCHOOL", "AUTHORITY", "UTILITY", "COOPERATIVE", "UNKNOWN"}
)

# NAICS prefixes → classification hints (deterministic, not fatal alone)
_PRODUCT_NAICS_PREFIXES = (
    "33",  # manufacturing
    "42",  # wholesale
    "44",  # retail
    "45",
)
_CONSTRUCTION_NAICS_PREFIXES = ("23",)
_SERVICE_NAICS_PREFIXES = ("54", "56", "62", "61", "81", "48", "49", "51", "52", "53", "71", "72")

_PRODUCT_TITLE = re.compile(
    r"\b(equipment|vehicle|truck|van|trailer|machinery|tool|tools|supply|supplies|"
    r"hardware|laptop|computer|monitor|server|part|parts|replacement|material|materials|"
    r"furniture|appliance|generator|pump|motor|instrument|device|commodity|commodities|"
    r"electronics|safety\s+equipment|PPE|printer|tablet|router|switch|chassis|"
    r"industrial\s+supplies|maintenance\s+supplies|facility\s+products)\b",
    re.I,
)
_INSTALL_TITLE = re.compile(r"\b(install|installation|delivery\s+and\s+install|set[\s-]?up)\b", re.I)
_CONSTRUCTION_TITLE = re.compile(
    r"\b(construction|renovation|remodel|build[\s-]?out|concrete|roofing|paving|excavation)\b",
    re.I,
)
_LABOR_TITLE = re.compile(
    r"\b(staffing|temporary\s+labor|janitorial|custodial|mowing|landscap|hauling|"
    r"consulting|engineering\s+services|medical\s+staff|nurse|physician|"
    r"professional\s+services|management\s+services)\b",
    re.I,
)
_SPECIALIST_TITLE = re.compile(
    r"\b(architect|design[\s-]build|R&D|research\s+and\s+development|clinical|"
    r"laboratory\s+services|legal\s+services)\b",
    re.I,
)

_BOND_RE = re.compile(r"\b(bid\s+bond|performance\s+bond|payment\s+bond|surety\s+bond)\b", re.I)
_DEPOSIT_RE = re.compile(r"\b(upfront\s+deposit|earnest\s+money|cash\s+deposit\s+required)\b", re.I)
_BRAND_RE = re.compile(
    r"\b(brand[\s-]?name\s+only|no\s+substitut|authorized\s+reseller|authorized\s+dealer|"
    r"manufacturer[\s-]?certified|oem\s+only|must\s+be\s+genuine)\b",
    re.I,
)
_LOCAL_PRESENCE_RE = re.compile(
    r"\b(must\s+be\s+located\s+in|local\s+presence\s+required|within\s+\d+\s+miles|"
    r"office\s+within|reside\s+in\s+the\s+(city|county|state))\b",
    re.I,
)
_NMR_HINT_RE = re.compile(r"\b(non[\s-]?manufacturer\s+rule|NMR|nonmanufacturer)\b", re.I)

# Structured fields that may carry HIGH-confidence monetary contract/award value.
_HIGH_VALUE_ATTRS = (
    "estimated_value",
    "contract_value",
    "award_amount",
    "ceiling",
    "total_contract_value",
)
_HIGH_VALUE_RAW_KEYS = (
    "awardAmount",
    "estimatedValue",
    "baseAndAllOptionsValue",
    "baseAndAllOptions",
    "totalContractValue",
    "ceiling",
)


def _parse_money(value: Any, *, allow_loose: bool = False) -> float | None:
    """Delegate to shared data_integrity.parse_money (strict / $ -loose)."""
    return parse_money(value, allow_loose=allow_loose)


def _value_fact(
    amount: float,
    *,
    confidence: str,
    source_field: str,
    source: str,
    status: str,
    raw_preview: str | None = None,
) -> dict[str, Any]:
    """Contract-value envelope used by Stage 0 (backward-compatible keys + provenance)."""
    fact = make_fact(
        amount,
        status=status,
        source_type=source,
        source_field=source_field,
        confidence=confidence,
    )
    # Legacy keys retained for callers
    fact["amount"] = amount
    fact["source"] = source
    fact["raw_preview"] = (raw_preview or "")[:40]
    return fact


def resolve_known_contract_value(opportunity: Any) -> dict[str, Any] | None:
    """
    Return provenance-aware contract value or None.

    Legacy ORM estimated_value is NOT automatically VERIFIED.
    VERIFIED only when reconciled to structured SAM monetary fields.
    UNPROVEN_LEGACY / ASSESSMENT / SOURCE_CONFLICT never drive hard Stage 0 reject.
    """
    analysis = _analysis(opportunity)
    raw = _raw_blob(opportunity)
    orm_val = None
    for attr in _HIGH_VALUE_ATTRS:
        raw_val = _field(opportunity, attr, default=None)
        if raw_val is not None:
            orm_val = raw_val
            break

    classified = classify_estimated_value(
        orm_estimated_value=orm_val,
        sam_raw=raw,
        analysis=analysis,
    )
    st = str(classified.get("status") or "")
    if st == "UNKNOWN":
        return None

    if classified.get("amount") is None and classified.get("value") is not None:
        try:
            classified["amount"] = float(classified["value"])
        except (TypeError, ValueError):
            pass

    if classified.get("amount") is None and st not in {
        STATUS_SOURCE_CONFLICT,
        STATUS_UNPROVEN_LEGACY,
        STATUS_ASSESSMENT,
    }:
        return None
    return classified


def parse_known_contract_value(opportunity: Any) -> float | None:
    """Scalar amount only when VERIFIED — unproven legacy / AI return None."""
    from data_integrity import verified_numeric_or_none

    return verified_numeric_or_none(resolve_known_contract_value(opportunity))


def _field(opportunity: Any, *names: str, default: Any = None) -> Any:
    if isinstance(opportunity, dict):
        for n in names:
            if n in opportunity and opportunity[n] is not None:
                return opportunity[n]
        return default
    for n in names:
        if hasattr(opportunity, n):
            val = getattr(opportunity, n)
            if val is not None:
                return val
    return default


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str) and value.strip():
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    return None


def _raw_blob(opportunity: Any) -> dict[str, Any]:
    raw = _field(opportunity, "sam_raw", "raw", "source_raw", default={})
    return raw if isinstance(raw, dict) else {}


def _analysis(opportunity: Any) -> dict[str, Any]:
    a = _field(opportunity, "analysis", default={})
    return a if isinstance(a, dict) else {}


def _text_haystack(opportunity: Any) -> str:
    parts = [
        str(_field(opportunity, "title", default="") or ""),
        str(_field(opportunity, "description", default="") or "")[:4000],
        str(_field(opportunity, "set_aside", default="") or ""),
        str(_field(opportunity, "notice_type", "type", default="") or ""),
    ]
    raw = _raw_blob(opportunity)
    for key in ("descriptionText", "typeOfSetAsideDescription", "typeOfSetAside", "classificationCode"):
        if raw.get(key):
            parts.append(str(raw[key])[:2000])
    return "\n".join(parts)


def normalize_source(opportunity: Any) -> str:
    src = _field(opportunity, "source", "procurement_source", "source_type", default=None)
    if isinstance(src, str) and src.strip().upper() in SOURCE_TYPES:
        return src.strip().upper()
    raw = _raw_blob(opportunity)
    if raw or _field(opportunity, "notice_id"):
        # Default federal when SAM-like fields present; still source-agnostic API
        if _field(opportunity, "link") and "sam.gov" in str(_field(opportunity, "link") or "").lower():
            return "FEDERAL"
        if raw.get("noticeId") or raw.get("solicitationNumber"):
            return "FEDERAL"
    return "UNKNOWN"


def classify_opportunity(opportunity: Any) -> tuple[str, str]:
    """Return (classification, confidence HIGH|MEDIUM|LOW). Never rejects by itself."""
    naics = str(_field(opportunity, "naics_code", "naics", default="") or "")
    psc = str(_field(opportunity, "psc", "classification_code", "product_service_code", default="") or "")
    raw = _raw_blob(opportunity)
    if not psc:
        psc = str(raw.get("classificationCode") or raw.get("psc") or "")
    title = str(_field(opportunity, "title", default="") or "")
    hay = f"{title}\n{_text_haystack(opportunity)}"

    if _CONSTRUCTION_TITLE.search(hay) or any(naics.startswith(p) for p in _CONSTRUCTION_NAICS_PREFIXES):
        return CLASS_CONSTRUCTION, "HIGH" if naics.startswith("23") else "MEDIUM"

    productish = any(naics.startswith(p) for p in _PRODUCT_NAICS_PREFIXES) or bool(_PRODUCT_TITLE.search(title))
    # PSC starting with digits often product commodity codes; letters often services — soft hint only
    if psc and psc[0].isdigit() and productish:
        productish = True

    if productish and (_INSTALL_TITLE.search(hay) or re.search(r"\b(train|training|maintenance|warranty\s+service)\b", hay, re.I)):
        return CLASS_PRODUCT_PLUS_SERVICE, "MEDIUM"
    if productish:
        return CLASS_PRODUCT_RESELL, "HIGH" if any(naics.startswith(p) for p in _PRODUCT_NAICS_PREFIXES) else "MEDIUM"

    if _SPECIALIST_TITLE.search(hay):
        return CLASS_SPECIALIST_SERVICE, "MEDIUM"
    if _LABOR_TITLE.search(hay) or any(naics.startswith(p) for p in ("561720", "561730", "561710")):
        # Janitorial/mowing etc. — subcontractable often, but labor-heavy
        if re.search(r"\b(janitorial|custodial|mowing|staffing)\b", hay, re.I):
            return CLASS_LABOR_HEAVY, "MEDIUM"
        return CLASS_SUBCONTRACTABLE_SERVICE, "MEDIUM"
    if any(naics.startswith(p) for p in _SERVICE_NAICS_PREFIXES):
        return CLASS_SUBCONTRACTABLE_SERVICE, "LOW"

    return CLASS_UNKNOWN, "LOW"


def solicitation_key(opportunity: Any) -> str | None:
    """Canonical solicitation identity for dedupe (prefer solicitation # over notice id)."""
    raw = _raw_blob(opportunity)
    for cand in (
        _field(opportunity, "solicitation_number", "solicitation_id", default=None),
        raw.get("solicitationNumber"),
        raw.get("solicitation_number"),
    ):
        if cand and str(cand).strip():
            return f"sol:{str(cand).strip().upper()}"
    notice = _field(opportunity, "notice_id", "id", default=None)
    if notice:
        return f"notice:{str(notice).strip().upper()}"
    url = _field(opportunity, "link", "url", "canonical_url", default=None)
    if url and str(url).strip():
        return f"url:{str(url).strip().lower()}"
    return None


def amendment_parent_key(opportunity: Any) -> str | None:
    """If this row is an amendment, return parent solicitation key."""
    raw = _raw_blob(opportunity)
    notice_type = str(
        _field(opportunity, "notice_type", "type", default=None)
        or raw.get("type")
        or raw.get("noticeType")
        or ""
    ).lower()
    parent = (
        raw.get("parentNoticeId")
        or raw.get("relatedNoticeId")
        or raw.get("amendedNoticeId")
        or _field(opportunity, "parent_notice_id", "related_notice_id", default=None)
    )
    if parent:
        return f"notice:{str(parent).strip().upper()}"
    if "amend" in notice_type:
        sol = (
            _field(opportunity, "solicitation_number", default=None)
            or raw.get("solicitationNumber")
        )
        if sol:
            return f"sol:{str(sol).strip().upper()}"
    return None


def is_duplicate_of_existing(
    opportunity: Any,
    existing: list[Any] | None = None,
) -> dict[str, Any] | None:
    """
    High-confidence duplicate / amendment-of-existing detection.
    Returns {reason, parent_key, existing_id} or None.
    """
    if not existing:
        return None
    my_notice = str(_field(opportunity, "notice_id", default="") or "").strip().upper()
    my_sol = solicitation_key(opportunity)
    parent = amendment_parent_key(opportunity)

    for other in existing:
        other_notice = str(_field(other, "notice_id", default="") or "").strip().upper()
        if not other_notice or other_notice == my_notice:
            continue
        other_sol = solicitation_key(other)
        # Exact same solicitation number → duplicate/amendment family
        if my_sol and other_sol and my_sol == other_sol and my_sol.startswith("sol:"):
            return {
                "reason": "duplicate_solicitation",
                "parent_key": my_sol,
                "existing_id": other_notice,
            }
        # Amendment pointing at existing notice
        if parent and (parent == f"notice:{other_notice}" or parent == other_sol):
            return {
                "reason": "amendment_of_existing",
                "parent_key": parent,
                "existing_id": other_notice,
            }
    return None


def min_profit_target_usd() -> float:
    try:
        return max(0.0, float(os.getenv("AI_MIN_ACTUAL_PROFIT_USD", "10000")))
    except ValueError:
        return 10000.0


def stage0_reject_reasons(opportunity: Any, *, today: date | None = None) -> list[str]:
    """Backward-compatible list of HIGH-confidence reject codes only."""
    result = stage0_evaluate(opportunity, today=today)
    return list(result.get("reject_reasons") or [])


def stage0_evaluate(
    opportunity: Any,
    *,
    today: date | None = None,
    existing: list[Any] | None = None,
) -> dict[str, Any]:
    """
    Stage 0 structured result — never calls OpenAI.

    decision: ADVANCE | REJECT | REVIEW
    REJECT requires HIGH confidence deterministic facts.
    """
    today = today or today_local()
    flags: list[str] = []
    reject_reasons: list[str] = []
    confidence = "HIGH"

    notice_id = _field(opportunity, "notice_id", "id", default=None)
    if not notice_id:
        # Cannot process without identity — REVIEW not REJECT (might be adapter bug)
        from product_deal import paid_work_priority_rank, resolve_core_fit

        _fit = resolve_core_fit(stage0_classification=CLASS_UNKNOWN)
        return {
            "funnel_stage": FunnelStage.STAGE_0.value,
            "decision": "REVIEW",
            "advance": True,
            "classification": CLASS_UNKNOWN,
            "core_fit": _fit["core_fit"],
            "product_purity": _fit.get("product_purity"),
            "paid_work_priority_rank": paid_work_priority_rank(_fit),
            "core_fit_status": "POLICY",
            "reject_reasons": [],
            "flags": ["missing_notice_id"],
            "set_aside_eligible": None,
            "nmr_review_required": False,
            "bond_review_required": False,
            "license_review_required": False,
            "channel_review_required": False,
            "known_contract_value": None,
            "source": normalize_source(opportunity),
            "confidence": "LOW",
            "ai_cost_usd": 0.0,
            "evaluated_at": now_utc().isoformat(),
        }

    # --- Deadlines / status ---
    due_d = _as_date(_field(opportunity, "due_date", "response_deadline", default=None))
    if due_d is not None and due_d < today:
        reject_reasons.append("expired_deadline")
    # Missing/unparseable deadline → do NOT reject

    status = str(_field(opportunity, "status", default="") or "").strip().lower()
    if status in {"cancelled", "canceled", "expired", "inactive", "archived", "deleted"}:
        reject_reasons.append(f"status_{status}")
    # "rejected"/"skipped" are app workflow states — flag but don't auto-reject as procurement cancelled
    if status in {"rejected", "skipped"}:
        flags.append(f"workflow_status_{status}")

    analysis = _analysis(opportunity)
    if analysis.get("stage0_reject") and analysis.get("stage0_reject_confidence") == "HIGH":
        reject_reasons.append("previously_stage0_rejected")
    if analysis.get("funnel_terminal") is True:
        reject_reasons.append("funnel_terminal")

    # --- Duplicates / amendments ---
    dup = is_duplicate_of_existing(opportunity, existing)
    amendment_info = None
    if dup:
        if dup["reason"] == "amendment_of_existing":
            flags.append("amendment_of_existing")
            amendment_info = dup
            # Do not reject amendments — preserve info; treat as non-independent for funnel advance
            # High confidence: skip paid AI as duplicate family member
            reject_reasons.append("amendment_duplicate")
        else:
            reject_reasons.append("duplicate_solicitation")
            amendment_info = dup

    # --- Set-aside ---
    sa = set_aside_eligibility(_field(opportunity, "set_aside", default=None) or _raw_blob(opportunity).get("typeOfSetAside"))
    set_aside_eligible = sa["eligible"]
    if set_aside_eligible is False:
        reject_reasons.append("unsupported_set_aside")
        flags.append(f"set_aside:{sa.get('matched')}")
    elif set_aside_eligible is None:
        flags.append("set_aside_unknown")
        confidence = "MEDIUM"

    # --- Classification (flag only) ---
    classification, class_conf = classify_opportunity(opportunity)
    if classification in {CLASS_LABOR_HEAVY, CLASS_SPECIALIST_SERVICE, CLASS_CONSTRUCTION}:
        flags.append(f"class_{classification.lower()}")
    if class_conf == "LOW":
        confidence = "LOW" if confidence != "HIGH" or not reject_reasons else confidence

    # --- Bonds / cash / license (explicit text only) ---
    hay = _text_haystack(opportunity)
    bond_review = bool(_BOND_RE.search(hay))
    if bond_review:
        flags.append("bond_mentioned")
    if _DEPOSIT_RE.search(hay):
        flags.append("upfront_deposit_mentioned")
        # Not automatic reject — needs review
    license_review = bool(re.search(r"\b(contractor\s+license\s+required|must\s+hold\s+.*license)\b", hay, re.I))
    if license_review:
        flags.append("license_mentioned")

    # --- Contract value (HIGH-confidence provenance only for reject) ---
    value_info = resolve_known_contract_value(opportunity)
    known_value = value_info  # structured provenance (or None)
    profit_target = min_profit_target_usd()
    # Hard reject only on VERIFIED + HIGH (UNKNOWN/ASSESSMENT never factual-reject)
    if (
        value_info is not None
        and can_hard_reject_on_fact(value_info)
        and float(value_info["amount"]) < profit_target
    ):
        reject_reasons.append("value_below_min_profit")
        flags.append(f"known_value:{value_info['amount']}")
    elif value_info is not None and not can_hard_reject_on_fact(value_info):
        flags.append(
            f"value_{str(value_info.get('status') or value_info.get('confidence') or 'unknown').lower()}"
            f":{value_info.get('amount')}"
        )
    elif value_info is None:
        flags.append("value_unknown")

    # --- Brand / channel ---
    channel_review = bool(_BRAND_RE.search(hay))
    if channel_review:
        flags.append("channel_restriction_mentioned")

    # --- NMR ---
    naics = str(_field(opportunity, "naics_code", "naics", default="") or "")
    nmr_review = False
    if _NMR_HINT_RE.search(hay):
        nmr_review = True
        flags.append("nmr_indicator")
    elif (
        set_aside_eligible is not False
        and any(naics.startswith(p) for p in _PRODUCT_NAICS_PREFIXES)
        and sa.get("reason") == "allowed_set_aside"
        and re.search(r"small\s+business", str(_field(opportunity, "set_aside", default="") or ""), re.I)
    ):
        nmr_review = True
        flags.append("nmr_possible_supply_sb")

    # --- Geography: never reject for out-of-state; flag local-presence only ---
    if _LOCAL_PRESENCE_RE.search(hay):
        flags.append("local_presence_requirement")
    # Explicit: Nebraska/home-state absence is NOT a reject

    # --- Decision ---
    # Only HIGH-confidence rejects stick; strip soft reasons if somehow uncertain
    high_confidence_rejects = {
        "expired_deadline",
        "status_cancelled",
        "status_canceled",
        "status_expired",
        "status_inactive",
        "status_archived",
        "status_deleted",
        "unsupported_set_aside",
        "value_below_min_profit",
        "duplicate_solicitation",
        "amendment_duplicate",
        "previously_stage0_rejected",
        "funnel_terminal",
    }
    hard = [r for r in reject_reasons if r in high_confidence_rejects or r.startswith("status_")]

    if hard:
        decision = "REJECT"
        advance = False
        conf_out = "HIGH"
    elif flags and classification == CLASS_UNKNOWN and set_aside_eligible is None:
        decision = "REVIEW"
        advance = True  # prefer Luna over false reject
        conf_out = "LOW"
    else:
        decision = "ADVANCE"
        advance = True
        conf_out = confidence if confidence in {"HIGH", "MEDIUM", "LOW"} else "MEDIUM"

    from product_deal import paid_work_priority_rank, resolve_core_fit

    core_fit_meta = resolve_core_fit(stage0_classification=classification)

    return {
        "funnel_stage": FunnelStage.STAGE_0.value,
        "decision": decision,
        "advance": advance,
        "classification": classification,
        "core_fit": core_fit_meta["core_fit"],
        "product_purity": core_fit_meta.get("product_purity"),
        "paid_work_priority_rank": paid_work_priority_rank(core_fit_meta),
        "core_fit_status": "POLICY",
        "reject_reasons": hard if not advance else [],
        "flags": flags,
        "set_aside_eligible": set_aside_eligible,
        "nmr_review_required": nmr_review,
        "bond_review_required": bond_review,
        "license_review_required": license_review,
        "channel_review_required": channel_review,
        "known_contract_value": known_value,
        "source": normalize_source(opportunity),
        "confidence": conf_out,
        "amendment": amendment_info,
        "ai_cost_usd": 0.0,
        "evaluated_at": now_utc().isoformat(),
    }


def should_advance_to_next_stage(
    opportunity: Any,
    current_stage: int | FunnelStage,
    *,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Framework for earning expensive AI. Not AI-opinion-only.
    Returns {advance: bool, next_stage: int|None, reasons: list[str]}.
    """
    evidence = evidence or {}
    stage = int(current_stage)
    reasons: list[str] = []

    if stage <= 0:
        s0 = stage0_evaluate(opportunity)
        return {
            "advance": s0["advance"],
            "next_stage": FunnelStage.STAGE_1.value if s0["advance"] else None,
            "reasons": s0["reject_reasons"] or s0.get("flags") or ["stage0_ok"],
            "stage0": s0,
        }

    # Deterministic blockers shared across paid stages
    if evidence.get("fatal_issue") and evidence.get("fatal_confidence") == "HIGH":
        reasons.append("fatal_or_ineligible")
    elif evidence.get("ineligible") is True and evidence.get("confidence") == "HIGH":
        reasons.append("fatal_or_ineligible")
    if evidence.get("requires_personal_guarantee") is True:
        reasons.append("personal_guarantee")
    if evidence.get("requires_owner_cash") is True or evidence.get("cash_upfront_required") is True:
        reasons.append("cash_upfront")
    # Weak AI "advance: false" alone must NOT kill — false-negative protection
    if evidence.get("advance") is False and evidence.get("fatal_issue") and evidence.get("fatal_confidence") == "HIGH":
        reasons.append("explicit_no_advance_with_fatal")

    positives: list[str] = []
    # Only CALCULATED actual profit counts — never POLICY scenarios or bare floats
    from economic_integrity import ECON_CALCULATED, min_actual_profit_usd as econ_min

    profit_result = evidence.get("actual_profit_result")
    if isinstance(profit_result, dict) and profit_result.get("status") == ECON_CALCULATED:
        try:
            if float(profit_result.get("actual_profit")) >= econ_min():
                positives.append("profit_ge_target")
        except (TypeError, ValueError):
            pass
    else:
        # Legacy key: ignore unless explicitly marked calculated
        est_profit = evidence.get("estimated_actual_profit")
        if (
            evidence.get("actual_profit_status") == ECON_CALCULATED
            and est_profit is not None
        ):
            try:
                if float(est_profit) >= min_profit_target_usd():
                    positives.append("profit_ge_target")
            except (TypeError, ValueError):
                pass
    rf = evidence.get("reseller_fit")
    if rf in {True, "HIGH", "MEDIUM"}:
        positives.append("reseller_fit")
    if evidence.get("eligible_set_aside") is True:
        positives.append("eligible_set_aside")
    if evidence.get("financing_viable") is True:
        positives.append("financing_viable")
    if evidence.get("research_confidence") in {"high", "medium"}:
        positives.append(f"confidence_{evidence.get('research_confidence')}")

    if reasons:
        return {"advance": False, "next_stage": None, "reasons": reasons, "positives": positives}

    next_stage = stage + 1
    if next_stage > FunnelStage.STAGE_5:
        return {"advance": False, "next_stage": None, "reasons": ["already_at_terminal_stage"], "positives": positives}

    if next_stage == FunnelStage.STAGE_5 and not evidence.get("explicit_premium_escalation"):
        return {
            "advance": False,
            "next_stage": None,
            "reasons": ["stage5_requires_explicit_action"],
            "positives": positives,
        }

    if next_stage == FunnelStage.STAGE_4:
        if not positives and evidence.get("estimated_actual_profit") is None:
            if evidence.get("advance") is not True:
                return {
                    "advance": False,
                    "next_stage": None,
                    "reasons": ["insufficient_value_evidence_for_stage4"],
                    "positives": positives,
                }

    return {
        "advance": True,
        "next_stage": next_stage,
        "reasons": ["gates_passed"],
        "positives": positives,
    }


# Map legacy openai_client tasks → funnel stages for routing.
TASK_DEFAULT_STAGE: dict[str, int] = {
    "screen_contract_text": FunnelStage.STAGE_1.value,
    "stage2_evidence_extract": FunnelStage.STAGE_2.value,
    "screen_contract": FunnelStage.STAGE_2.value,
    "try_extract_sqft_from_drawings": FunnelStage.STAGE_2.value,
    "extract_sqft_from_drawings": FunnelStage.STAGE_2.value,
    "extract_sub_type_for_sub_search": FunnelStage.STAGE_2.value,
    "extract_solicitation_meta": FunnelStage.STAGE_2.value,
    "analyze_subcontractors": FunnelStage.STAGE_2.value,
    "extract_proposal_requirements": FunnelStage.STAGE_2.value,
    "generate_proposal_content": FunnelStage.STAGE_4.value,
    "regenerate_proposal_section": FunnelStage.STAGE_4.value,
    "humanize_proposal_text": FunnelStage.STAGE_4.value,
    "reduce_proposal_ai_score": FunnelStage.STAGE_4.value,
    "generate_subcontract_agreement": FunnelStage.STAGE_4.value,
    "generate_contract_advice": FunnelStage.STAGE_4.value,
}


def default_stage_for_task(task: str) -> int:
    return TASK_DEFAULT_STAGE.get(task, FunnelStage.STAGE_2.value)
