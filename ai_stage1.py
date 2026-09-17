"""Stage 1 — minimum-cost Luna triage (no PDFs, compact structured JSON).

NO live API calls from tests; all calls go through openai_runtime.create_response.
"""

from __future__ import annotations
from application_clock import now_utc

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

from ai_analysis_cache import PROMPT_SCHEMA_VERSION
from ai_funnel import (
    CLASS_UNKNOWN,
    classify_opportunity,
    normalize_source,
    resolve_known_contract_value,
)
from ai_model_router import FunnelStage
from ai_pricing import estimate_call_cost_usd, estimate_tokens_from_chars

logger = logging.getLogger("govtracker.ai.stage1")

# Bump version so truncated v1 cache entries are not reused.
STAGE1_SCHEMA_VERSION = "stage1-compact-v2"
STAGE1_INSTRUCTION_VERSION = "stage1-instr-v2"  # tracks instruction semantics (integrity rules)
STAGE1_TASK = "screen_contract_text"
STAGE1_MAX_OUTPUT_TOKENS = int(os.getenv("AI_STAGE1_MAX_OUTPUT_TOKENS", "400"))
STAGE1_TARGET_INPUT_CHARS = int(os.getenv("AI_STAGE1_TARGET_INPUT_CHARS", "3500"))
STAGE1_DESC_CHARS = int(os.getenv("AI_STAGE1_DESC_CHARS", "1200"))
STAGE1_EXTRACT_CHARS = int(os.getenv("AI_STAGE1_EXTRACT_CHARS", "800"))

# Historical Opp 68 live Stage 1 fingerprint (audit only — NEVER treat as current authorization)
HISTORICAL_STAGE1_FP_OPP68 = "b3c7aeca16fd5ac28b3d434e2f7dc1465c8ffa297548da5ceee80cc60b23816d"

REASON_CODES = (
    "FIT",
    "POOR_FIT",
    "FATAL",
    "NEEDS_STAGE2_DOCUMENT_REVIEW",
    "UNCERTAIN_ADVANCE",
    "UNSTATED",
)

# Static instructions — stable prefix for prompt caching. No timestamps. No score/pursue.
STAGE1_INSTRUCTIONS = """GovCon Stage-1 triage. Output ONLY the structured schema. No markdown. No prose. No chain-of-thought.

DATA INTEGRITY:
Never fabricate missing facts or numeric values.
If evidence does not establish a value, return null.
Do not invent quantity, price, contract value, bid count, bond, license, or similar facts.
bond_likely / license_likely / install_required / channel_restriction_likely are ASSESSMENTS (null if unknown) — never claim verified absence.
quantity must be null unless an explicit quantity is stated in the supplied text.
Building numbers, NAICS codes, ZIP codes, and day counts are NOT quantities or money.

Rules:
- Decide only if worth deeper paid review.
- If PDFs/attachments needed: reason_code=NEEDS_STAGE2_DOCUMENT_REVIEW, advance=true, fatal_issue=null.
- Uncertain => advance=true.
- advance=false ONLY with clear fatal_issue.
- buying <= 80 chars. Keep fields compact.
- Do NOT invent score or pursue fields."""

# Strict JSON Schema for Responses API text.format
STAGE1_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "category": {
            "type": "string",
            "enum": [
                "PRODUCT_RESELL",
                "PRODUCT_PLUS_SERVICE",
                "SUBCONTRACTABLE_SERVICE",
                "LABOR_HEAVY",
                "SPECIALIST_SERVICE",
                "CONSTRUCTION",
                "UNKNOWN",
            ],
        },
        "buying": {"type": "string", "maxLength": 80},
        "quantity": {"type": ["number", "null"]},
        "exact_model_identified": {"type": "boolean"},
        "install_required": {"type": ["boolean", "null"]},
        "bond_likely": {"type": ["boolean", "null"]},
        "license_likely": {"type": ["boolean", "null"]},
        "channel_restriction_likely": {"type": ["boolean", "null"]},
        "reseller_fit": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW", "NONE"]},
        "execution_complexity": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH"]},
        "fatal_issue": {"type": ["string", "null"], "maxLength": 80},
        "advance": {"type": "boolean"},
        "reason_code": {
            "type": "string",
            "enum": list(REASON_CODES),
        },
    },
    "required": [
        "category",
        "buying",
        "quantity",
        "exact_model_identified",
        "install_required",
        "bond_likely",
        "license_likely",
        "channel_restriction_likely",
        "reseller_fit",
        "execution_complexity",
        "fatal_issue",
        "advance",
        "reason_code",
    ],
}


def stage1_response_format() -> dict[str, Any]:
    return {
        "type": "json_schema",
        "name": "stage1_triage",
        "strict": True,
        "schema": STAGE1_JSON_SCHEMA,
    }


def _field(obj: Any, *names: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        for n in names:
            if n in obj and obj[n] is not None:
                return obj[n]
        return default
    for n in names:
        if hasattr(obj, n):
            v = getattr(obj, n)
            if v is not None:
                return v
    return default


def build_stage1_input(
    opportunity: Any,
    *,
    stage0: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compact input builder. Never includes PDF/file payloads."""
    title = str(_field(opportunity, "title", default="") or "")[:200]
    desc = str(_field(opportunity, "description", default="") or "")
    raw = _field(opportunity, "sam_raw", "raw", default={})
    if isinstance(raw, dict) and not desc:
        desc = str(raw.get("descriptionText") or "")[: STAGE1_DESC_CHARS * 2]
    desc = re.sub(r"\s+", " ", desc).strip()[:STAGE1_DESC_CHARS]

    value_meta = resolve_known_contract_value(opportunity)
    value_status = str((value_meta or {}).get("status") or "UNKNOWN")
    # AI must not receive unproven legacy amounts labeled as facts
    value_for_ai = None
    if value_status == "VERIFIED":
        value_for_ai = (value_meta or {}).get("amount")
    meta = {
        "notice_id": _field(opportunity, "notice_id", default=None),
        "naics": _field(opportunity, "naics_code", "naics", default=None),
        "psc": _field(opportunity, "psc", "classification_code", default=None)
        or (raw.get("classificationCode") if isinstance(raw, dict) else None),
        "set_aside": _field(opportunity, "set_aside", default=None),
        "agency": _field(opportunity, "agency", "entity", "department", default=None),
        "deadline": (
            _field(opportunity, "due_date").isoformat()
            if hasattr(_field(opportunity, "due_date", default=None), "isoformat")
            else _field(opportunity, "due_date", default=None)
        ),
        "value": value_for_ai,
        "value_status": value_status,
        "value_confidence": (value_meta or {}).get("confidence"),
        "value_note": (
            None
            if value_status == "VERIFIED"
            else "Contract value not verified — do not treat any dollar figure as factual"
        ),
        "source": normalize_source(opportunity),
        "location": str(_field(opportunity, "location", default="") or "")[:80] or None,
    }

    s0 = stage0 or {}
    flags = {
        "stage0_decision": s0.get("decision"),
        "stage0_class": s0.get("classification"),
        "stage0_flags": (s0.get("flags") or [])[:12],
        "set_aside_eligible": s0.get("set_aside_eligible"),
        "nmr_review_required": s0.get("nmr_review_required"),
        "bond_review_required": s0.get("bond_review_required"),
        "channel_review_required": s0.get("channel_review_required"),
    }

    extract = str(_field(opportunity, "attachment_text", default="") or "").strip()
    extract = re.sub(r"\s+", " ", extract)[:STAGE1_EXTRACT_CHARS] if extract else ""

    det_class, det_conf = classify_opportunity(opportunity)

    payload = {
        "title": title,
        "description": desc,
        "meta": {k: v for k, v in meta.items() if v is not None},
        "stage0": flags,
        "det_class": det_class,
        "det_class_confidence": det_conf,
    }
    if extract:
        payload["extract"] = extract

    text = json.dumps(payload, separators=(",", ":"), default=str)
    if len(text) > STAGE1_TARGET_INPUT_CHARS:
        payload.pop("extract", None)
        text = json.dumps(payload, separators=(",", ":"), default=str)
    if len(text) > STAGE1_TARGET_INPUT_CHARS:
        keep = max(200, STAGE1_TARGET_INPUT_CHARS - 800)
        payload["description"] = (payload.get("description") or "")[:keep]
        text = json.dumps(payload, separators=(",", ":"), default=str)

    return {
        "text": text,
        "char_count": len(text),
        "estimated_tokens": estimate_tokens_from_chars(len(text)),
        "has_pdf": False,
        "schema_version": STAGE1_SCHEMA_VERSION,
    }


def content_has_pdf_parts(content: list[dict[str, Any]]) -> bool:
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") in {"input_file", "document", "input_image", "image"}:
            return True
        if part.get("file_data") or (isinstance(part.get("source"), dict) and part["source"].get("data")):
            return True
    return False


def normalize_stage1_result(raw: dict[str, Any], *, stage0: dict[str, Any] | None = None) -> dict[str, Any]:
    """Normalize Stage 1 JSON; map score/pursue locally (never from the model).

    AI fields are ASSESSMENTS — never silently promoted to VERIFIED facts.
    Missing factual fields stay null/UNKNOWN.
    """
    from data_integrity import STATUS_ASSESSMENT, assessment_fact, unknown_fact

    category = str(raw.get("category") or CLASS_UNKNOWN).upper()
    allowed = {
        "PRODUCT_RESELL",
        "PRODUCT_PLUS_SERVICE",
        "SUBCONTRACTABLE_SERVICE",
        "LABOR_HEAVY",
        "SPECIALIST_SERVICE",
        "CONSTRUCTION",
        "UNKNOWN",
    }
    if category not in allowed:
        category = CLASS_UNKNOWN

    reseller_fit = str(raw.get("reseller_fit") or "LOW").upper()
    if reseller_fit not in {"HIGH", "MEDIUM", "LOW", "NONE"}:
        reseller_fit = "LOW"

    reason_code = str(raw.get("reason_code") or "UNSTATED")[:64]
    if reason_code not in REASON_CODES:
        # Allow unknown codes but keep compact
        reason_code = reason_code[:32] or "UNSTATED"

    fatal = raw.get("fatal_issue")
    if fatal is not None:
        fatal = str(fatal).strip()[:80] or None

    advance = raw.get("advance")
    if advance is None:
        advance = True
    advance = bool(advance)

    if reason_code == "NEEDS_STAGE2_DOCUMENT_REVIEW":
        advance = True
        fatal = None

    if not advance and not fatal:
        advance = True
        reason_code = "UNCERTAIN_ADVANCE"

    # Deterministic legacy mapping — NOT generated by the model
    if not advance and fatal:
        score = 2
        pursue = False
    elif reseller_fit == "HIGH":
        score = 8
        pursue = True
    elif reseller_fit == "MEDIUM":
        score = 7
        pursue = True
    elif reseller_fit == "LOW":
        score = 5
        pursue = True if advance else False
    else:
        score = 3
        pursue = False if not advance else True

    if reason_code == "NEEDS_STAGE2_DOCUMENT_REVIEW":
        score = max(score, 6)
        pursue = True

    qty = raw.get("quantity")
    if qty is not None:
        try:
            qty = float(qty)
        except (TypeError, ValueError):
            qty = None

    def _tri_state(v: Any) -> bool | None:
        if v is None:
            return None
        if isinstance(v, bool):
            return v
        return None

    bond = _tri_state(raw.get("bond_likely"))
    license_ = _tri_state(raw.get("license_likely"))
    install = _tri_state(raw.get("install_required"))
    channel = _tri_state(raw.get("channel_restriction_likely"))

    complexity = str(raw.get("execution_complexity") or "MEDIUM").upper()[:16]
    if complexity not in {"LOW", "MEDIUM", "HIGH"}:
        complexity = "MEDIUM"

    from product_deal import paid_work_priority_rank, resolve_core_fit

    core_fit_meta = resolve_core_fit(
        stage1_category=category,
        stage0_classification=(stage0 or {}).get("classification"),
    )
    # Prefer CORE_PRODUCT for paid work without deleting service pursue paths
    paid_rank = paid_work_priority_rank(core_fit_meta)

    return {
        "category": category,
        "buying": str(raw.get("buying") or "")[:80],
        "quantity": qty,
        "quantity_fact": (
            assessment_fact(qty, source_type="AI", source_field="stage1.quantity")
            if qty is not None
            else unknown_fact(source_field="stage1.quantity")
        ),
        "exact_model_identified": bool(raw.get("exact_model_identified")),
        "install_required": install,
        "bond_likely": bond,
        "license_likely": license_,
        "channel_restriction_likely": channel,
        "bond_fact": (
            assessment_fact(bond, source_type="AI", source_field="stage1.bond_likely")
            if bond is not None
            else unknown_fact(source_field="stage1.bond_likely")
        ),
        "license_fact": (
            assessment_fact(license_, source_type="AI", source_field="stage1.license_likely")
            if license_ is not None
            else unknown_fact(source_field="stage1.license_likely")
        ),
        "reseller_fit": reseller_fit,
        "execution_complexity": complexity,
        "fatal_issue": fatal,
        "advance": advance,
        "reason_code": reason_code,
        "score": score,
        "text_score": score,
        "pursue": pursue,
        "core_fit": core_fit_meta["core_fit"],
        "product_purity": core_fit_meta.get("product_purity"),
        "paid_work_priority_rank": paid_rank,
        "core_fit_status": "POLICY",
        "reason": reason_code,
        "plain_english_summary": str(raw.get("buying") or reason_code)[:120],
        "screening_stage": "text",
        "funnel_stage": FunnelStage.STAGE_1.value,
        "stage1_schema_version": STAGE1_SCHEMA_VERSION,
        "stage1_field_status": STATUS_ASSESSMENT,
        "pdfs_sent_to_claude": 0,
        "stage0": stage0,
    }


def record_stage1_metric(entry: dict[str, Any]) -> None:
    try:
        from database import SessionLocal
        from models import AppSetting

        key = "ai_stage1_metrics"
        session = SessionLocal()
        try:
            row = session.query(AppSetting).filter_by(key=key).first()
            data: dict[str, Any] = {
                "count": 0,
                "cache_hits": 0,
                "rejects": 0,
                "advances": 0,
                "sum_input_chars": 0,
                "sum_input_tokens": 0,
                "sum_output_tokens": 0,
                "sum_estimated_cost_usd": 0.0,
            }
            if row and row.value:
                try:
                    parsed = json.loads(row.value)
                    if isinstance(parsed, dict):
                        data.update(parsed)
                except json.JSONDecodeError:
                    pass
            data["count"] = int(data.get("count") or 0) + 1
            if entry.get("cache_hit"):
                data["cache_hits"] = int(data.get("cache_hits") or 0) + 1
            if entry.get("advance") is False:
                data["rejects"] = int(data.get("rejects") or 0) + 1
            else:
                data["advances"] = int(data.get("advances") or 0) + 1
            data["sum_input_chars"] = int(data.get("sum_input_chars") or 0) + int(entry.get("input_chars") or 0)
            data["sum_input_tokens"] = int(data.get("sum_input_tokens") or 0) + int(entry.get("input_tokens") or 0)
            data["sum_output_tokens"] = int(data.get("sum_output_tokens") or 0) + int(entry.get("output_tokens") or 0)
            data["sum_estimated_cost_usd"] = float(data.get("sum_estimated_cost_usd") or 0) + float(
                entry.get("estimated_cost_usd") or 0
            )
            n = max(1, int(data["count"]))
            data["average_stage1_input_chars"] = round(data["sum_input_chars"] / n, 1)
            data["average_stage1_input_tokens"] = round(data["sum_input_tokens"] / n, 1)
            data["average_stage1_output_tokens"] = round(data["sum_output_tokens"] / n, 1)
            data["average_stage1_estimated_cost"] = round(data["sum_estimated_cost_usd"] / n, 6)
            data["updated_at"] = now_utc().isoformat()
            serialized = json.dumps(data)
            if row:
                row.value = serialized
            else:
                session.add(AppSetting(key=key, value=serialized))
            session.commit()
        finally:
            session.close()
    except Exception:
        logger.debug("stage1 metrics skipped", exc_info=True)


def get_stage1_metrics() -> dict[str, Any]:
    try:
        from database import SessionLocal
        from models import AppSetting

        session = SessionLocal()
        try:
            row = session.query(AppSetting).filter_by(key="ai_stage1_metrics").first()
            if row and row.value:
                return json.loads(row.value)
        finally:
            session.close()
    except Exception:
        pass
    return {}


def stage1_schema_fingerprint_component() -> str:
    """Schema string embedded in analysis fingerprints (must stay stable for cache hits)."""
    return f"{PROMPT_SCHEMA_VERSION}:{STAGE1_SCHEMA_VERSION}:{STAGE1_INSTRUCTION_VERSION}"


def compute_stage1_fingerprint(
    opportunity: Any,
    *,
    stage0: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the CURRENT Stage 1 fingerprint from current evidence + config. No OpenAI."""
    from ai_analysis_cache import analysis_fingerprint, content_source_hash
    from ai_funnel import stage0_evaluate
    from ai_model_router import ModelTier, model_for_tier

    s0 = stage0 if stage0 is not None else stage0_evaluate(opportunity)
    built = build_stage1_input(opportunity, stage0=s0)
    content = [{"type": "input_text", "text": built["text"]}]
    source_hash = content_source_hash(content, STAGE1_INSTRUCTIONS)
    schema_v = stage1_schema_fingerprint_component()
    notice_id = str(_field(opportunity, "notice_id", default="") or "") or None
    fingerprint = analysis_fingerprint(
        notice_id=notice_id,
        task=STAGE1_TASK,
        model_tier=ModelTier.CHEAP.value,
        source_hash=source_hash,
        schema_version=schema_v,
        funnel_stage=FunnelStage.STAGE_1.value,
    )
    return {
        "fingerprint": fingerprint,
        "source_hash": source_hash,
        "schema_version": STAGE1_SCHEMA_VERSION,
        "instruction_version": STAGE1_INSTRUCTION_VERSION,
        "schema_fingerprint_component": schema_v,
        "model": model_for_tier(ModelTier.CHEAP),
        "tier": ModelTier.CHEAP.value,
        "task": STAGE1_TASK,
        "funnel_stage": FunnelStage.STAGE_1.value,
        "notice_id": notice_id,
        "input_chars": built["char_count"],
        "input_text": built["text"],
        "stage0": s0,
    }


def resolve_current_stage1_result(
    opportunity: Any,
    *,
    stage0: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Resolve the latest VALID Stage 1 result for downstream stages.

    1) Build CURRENT fingerprint from current evidence/config
    2) Look up exact fingerprint in analysis cache
    3) HIT → return normalized result (current=True)
    4) MISS → STAGE1_CURRENT_RESULT_MISSING (do NOT use stale fingerprints)

    Never calls OpenAI. Never uses ORM analysis as Stage 1 authority.
    Historical fingerprints may be reported as audit-only stale entries.
    """
    from ai_analysis_cache import get_cached_analysis
    from openai_runtime import extract_json_object

    computed = compute_stage1_fingerprint(opportunity, stage0=stage0)
    fp = computed["fingerprint"]
    cached = get_cached_analysis(fp)

    # Audit-only: note historical Opp68 entry if present and different
    historical = None
    notice = computed.get("notice_id") or ""
    if notice == "dc6ac7432d224e06a44b6340db61442f" and fp != HISTORICAL_STAGE1_FP_OPP68:
        hist_cached = get_cached_analysis(HISTORICAL_STAGE1_FP_OPP68)
        if hist_cached and hist_cached.get("result_text") is not None:
            historical = {
                "fingerprint": HISTORICAL_STAGE1_FP_OPP68,
                "status": "STALE_HISTORICAL",
                "cached_at": hist_cached.get("cached_at"),
                "meta": hist_cached.get("meta"),
                "note": "Historical Stage 1 result exists but does not match current fingerprint — not authorization",
            }

    if not cached or cached.get("result_text") is None:
        return {
            "status": "STAGE1_CURRENT_RESULT_MISSING",
            "current": False,
            "fingerprint": fp,
            "result": None,
            "cache_record": None,
            "components": {k: v for k, v in computed.items() if k != "input_text"},
            "historical_stale": historical,
            "openai_called": False,
        }

    try:
        raw = extract_json_object(str(cached["result_text"]))
    except Exception:
        raw = json.loads(str(cached["result_text"]))

    result = normalize_stage1_result(raw, stage0=computed.get("stage0"))
    result["cache_hit"] = True
    result["incremental_cost_usd"] = 0.0
    result["fingerprint"] = fp
    result["stage1_schema_version"] = STAGE1_SCHEMA_VERSION
    result["stage1_instruction_version"] = STAGE1_INSTRUCTION_VERSION

    return {
        "status": "HIT",
        "current": True,
        "fingerprint": fp,
        "result": result,
        "cache_record": {
            "stage": 1,
            "opportunity_identity": computed.get("notice_id"),
            "fingerprint": fp,
            "schema_version": STAGE1_SCHEMA_VERSION,
            "instruction_version": STAGE1_INSTRUCTION_VERSION,
            "model": computed.get("model"),
            "tier": computed.get("tier"),
            "created_at": cached.get("cached_at"),
            "result_text": cached.get("result_text"),
            "meta": cached.get("meta"),
            "current_stale_status": "current",
        },
        "components": {k: v for k, v in computed.items() if k != "input_text"},
        "historical_stale": historical,
        "openai_called": False,
    }


def run_stage1_triage(
    opportunity: Any,
    *,
    stage0: dict[str, Any] | None = None,
    automatic: bool = False,
) -> dict[str, Any]:
    """Execute Stage 1 compact triage via OpenAI runtime (mocked in tests)."""
    from openai_runtime import create_response, extract_json_object

    built = build_stage1_input(opportunity, stage0=stage0)
    content = [{"type": "input_text", "text": built["text"]}]
    assert not content_has_pdf_parts(content)

    text = create_response(
        task=STAGE1_TASK,
        instructions=STAGE1_INSTRUCTIONS,
        content=content,
        max_output_tokens=STAGE1_MAX_OUTPUT_TOKENS,
        web_search=False,
        notice_id=str(_field(opportunity, "notice_id", default="") or "") or None,
        funnel_stage=FunnelStage.STAGE_1.value,
        automatic=automatic,
        use_cache=True,
        schema_version=stage1_schema_fingerprint_component(),
        text_format=stage1_response_format(),
    )
    try:
        raw = extract_json_object(text)
    except Exception:
        raw = {
            "category": CLASS_UNKNOWN,
            "buying": "parse_error",
            "quantity": None,
            "exact_model_identified": False,
            "install_required": None,
            "bond_likely": None,
            "license_likely": None,
            "channel_restriction_likely": None,
            "reseller_fit": "LOW",
            "execution_complexity": "MEDIUM",
            "fatal_issue": None,
            "advance": True,
            "reason_code": "PARSE_ERROR_ADVANCE",
        }

    result = normalize_stage1_result(raw, stage0=stage0)
    result["stage1_input_chars"] = built["char_count"]
    result["stage1_input_tokens_est"] = built["estimated_tokens"]
    result["text_screened_at"] = now_utc().isoformat()

    from openai_runtime import get_last_response_meta

    meta = get_last_response_meta()
    cache_hit = bool(meta.get("cache_hit"))
    result["cache_hit"] = cache_hit
    result["incremental_cost_usd"] = 0.0 if cache_hit else float(meta.get("estimated_cost_usd") or 0.0)

    record_stage1_metric(
        {
            "input_chars": built["char_count"],
            "input_tokens": built["estimated_tokens"] if not cache_hit else 0,
            "output_tokens": 0 if cache_hit else min(120, STAGE1_MAX_OUTPUT_TOKENS),
            "estimated_cost_usd": 0.0
            if cache_hit
            else estimate_call_cost_usd(
                model=os.getenv("OPENAI_MODEL_CHEAP") or "gpt-5.6-luna",
                input_tokens=built["estimated_tokens"],
                output_tokens=min(120, STAGE1_MAX_OUTPUT_TOKENS),
            ),
            "advance": result["advance"],
            "cache_hit": cache_hit,
        }
    )
    return result
