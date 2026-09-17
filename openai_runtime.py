"""Shared OpenAI Responses API helpers with funnel routing, budget, and cache.

DO NOT make live calls during automated tests when mocked.
"""

from __future__ import annotations
from application_clock import now_utc, today_local

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv
from pathlib import Path

from ai_analysis_cache import (
    PROMPT_SCHEMA_VERSION,
    analysis_fingerprint,
    content_source_hash,
    get_cached_analysis,
    put_cached_analysis,
)
from ai_cost_budget import (
    AIDollarBudgetExceeded,
    AIInputTooLarge,
    AIStageBudgetExceeded,
    get_cost_snapshot,
    record_ai_dollar_spend,
    require_automatic_budget,
)
from ai_funnel import default_stage_for_task
from ai_model_router import (
    FunnelStage,
    ModelTier,
    clamp_max_output_tokens,
    max_input_chars_for_stage,
    model_for_tier,
    resolve_model,
    stage_allows_automatic,
    tier_for_stage,
    web_search_allowed_for_stage,
)
from ai_pricing import (
    estimate_call_cost_usd,
    estimate_pre_call_cost_usd,
    estimate_tokens_from_chars,
    extract_usage_tokens,
)

load_dotenv(Path(__file__).resolve().parent / ".env")

logger = logging.getLogger("govtracker.ai")

DEFAULT_OPENAI_MODEL = "gpt-5.6-luna"  # cheap default; prefer tier env vars
OPENAI_REQUEST_TIMEOUT = float(os.getenv("OPENAI_REQUEST_TIMEOUT", "120"))

# Last create_response outcome (cache-hit metrics without changing return type).
_LAST_RESPONSE_META: dict[str, Any] = {}


def get_last_response_meta() -> dict[str, Any]:
    return dict(_LAST_RESPONSE_META)


def openai_api_key() -> str:
    key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not key:
        raise ValueError("OPENAI_API_KEY is missing from environment")
    return key


def openai_model() -> str:
    """Legacy helper — returns cheap-tier model unless OPENAI_MODEL overrides."""
    return resolve_model()


def openai_web_search_enabled_default() -> bool:
    raw = (os.getenv("OPENAI_WEB_SEARCH_ENABLED") or "false").strip().lower()
    return raw in ("1", "true", "yes")


def get_openai_client():
    from openai import OpenAI

    return OpenAI(api_key=openai_api_key(), timeout=OPENAI_REQUEST_TIMEOUT)


def extract_json_object(text: str) -> dict[str, Any]:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.rfind("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise
        data = json.loads(cleaned[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("AI response was not a JSON object")
    return data


def response_output_text(response: Any) -> str:
    text = getattr(response, "output_text", None)
    if isinstance(text, str) and text.strip():
        return text
    chunks: list[str] = []
    for item in getattr(response, "output", None) or []:
        for part in getattr(item, "content", None) or []:
            part_text = getattr(part, "text", None)
            if part_text:
                chunks.append(str(part_text))
    return "".join(chunks)


def _content_char_count(content: list[dict[str, Any]]) -> int:
    total = 0
    for part in content:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind in {"input_text", "text"}:
            total += len(str(part.get("text") or ""))
        elif kind in {"input_file", "document"}:
            # File payloads are large; count decoded-ish size from base64 length * 0.75
            file_data = str(part.get("file_data") or "")
            src = part.get("source") if isinstance(part.get("source"), dict) else {}
            data_b64 = str(src.get("data") or "")
            blob = file_data or data_b64
            total += int(len(blob) * 0.75)
        elif kind in {"input_image", "image"}:
            total += len(str(part.get("image_url") or ""))
    return total


def log_ai_usage(
    *,
    task: str,
    model: str,
    notice_id: str | None = None,
    web_search: bool = False,
    success: bool = True,
    error: str | None = None,
    input_tokens: int | None = None,
    cached_input_tokens: int | None = None,
    output_tokens: int | None = None,
    reasoning_tokens: int | None = None,
    total_tokens: int | None = None,
    funnel_stage: int | None = None,
    model_tier: str | None = None,
    estimated_cost_usd: float | None = None,
    cache_hit: bool = False,
    automatic: bool = False,
) -> None:
    """Usage + cost log — no API keys or document bodies."""
    if estimated_cost_usd is None and success and not cache_hit:
        estimated_cost_usd = estimate_call_cost_usd(
            model=model,
            input_tokens=input_tokens or 0,
            cached_input_tokens=cached_input_tokens or 0,
            output_tokens=output_tokens or 0,
            reasoning_tokens=reasoning_tokens or 0,
            web_search=web_search,
        )
    elif cache_hit:
        estimated_cost_usd = 0.0

    payload = {
        "ts": now_utc().isoformat(),
        "task": task,
        "funnel_stage": funnel_stage,
        "model": model,
        "model_tier": model_tier,
        "notice_id": notice_id,
        "web_search": bool(web_search),
        "success": bool(success),
        "cache_hit": bool(cache_hit),
        "automatic": bool(automatic),
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "total_tokens": total_tokens,
        "estimated_cost_usd": estimated_cost_usd,
        "error": (error or "")[:240] or None,
    }
    logger.info("ai_usage %s", json.dumps(payload, default=str))
    if web_search:
        logger.info("ai_web_search %s", json.dumps({"task": task, "notice_id": notice_id, "model": model}, default=str))

    try:
        from database import SessionLocal
        from models import AppSetting

        key = f"ai_usage_log_{today_local().isoformat()}"
        session = SessionLocal()
        try:
            row = session.query(AppSetting).filter_by(key=key).first()
            entries: list[Any] = []
            if row and row.value:
                try:
                    parsed = json.loads(row.value)
                    if isinstance(parsed, list):
                        entries = parsed
                except json.JSONDecodeError:
                    entries = []
            entries.append(payload)
            entries = entries[-500:]
            serialized = json.dumps(entries)
            if row:
                row.value = serialized
            else:
                session.add(AppSetting(key=key, value=serialized))
            session.commit()
        finally:
            session.close()
    except Exception:
        logger.debug("ai_usage DB log skipped", exc_info=True)

    if success and not cache_hit and (estimated_cost_usd or 0) > 0:
        try:
            record_ai_dollar_spend(payload)
        except Exception:
            logger.debug("ai dollar spend record skipped", exc_info=True)


def normalize_content_parts(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Accept OpenAI parts or legacy Anthropic-style blocks from the migrated client."""
    out: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind in {"input_text", "input_file", "input_image"}:
            out.append(part)
        elif kind == "text":
            out.append(text_part(str(part.get("text") or "")))
        elif kind == "document":
            source = part.get("source") if isinstance(part.get("source"), dict) else {}
            data_b64 = str(source.get("data") or "")
            if data_b64:
                out.append(
                    {
                        "type": "input_file",
                        "filename": str(part.get("filename") or "document.pdf"),
                        "file_data": f"data:application/pdf;base64,{data_b64}",
                    }
                )
        elif kind == "image":
            source = part.get("source") if isinstance(part.get("source"), dict) else {}
            data_b64 = str(source.get("data") or "")
            media = str(source.get("media_type") or "image/png")
            if data_b64:
                out.append({"type": "input_image", "image_url": f"data:{media};base64,{data_b64}"})
        else:
            out.append(part)
    return out


def create_response(
    *,
    task: str,
    instructions: str | None,
    content: list[dict[str, Any]],
    max_output_tokens: int,
    model: str | None = None,
    web_search: bool | None = None,
    notice_id: str | None = None,
    funnel_stage: int | FunnelStage | None = None,
    model_tier: ModelTier | str | None = None,
    automatic: bool = False,
    use_cache: bool = True,
    schema_version: str = PROMPT_SCHEMA_VERSION,
    text_format: dict[str, Any] | None = None,
) -> str:
    """
    Single entry point for OpenAI Responses API calls.

    - Stage 0 must never call this.
    - Web search only Stage 3+ and only when explicitly requested (or global default — still blocked <3).
    - Stage 5 cannot be automatic.
    - Cache identical fingerprints.
    - Automatic calls respect dollar / stage budgets.
    - text_format: optional Responses API structured output (json_schema / json_object).
    """
    global _LAST_RESPONSE_META

    stage_int: int | None
    if funnel_stage is None:
        stage_int = default_stage_for_task(task)
    else:
        stage_int = int(funnel_stage)

    if stage_int == FunnelStage.STAGE_0:
        raise ValueError("Stage 0 must not call OpenAI")

    if automatic and not stage_allows_automatic(stage_int):
        raise ValueError("Stage 5 premium analysis requires explicit user action — not automatic")

    tier = model_tier
    if tier is None and stage_int is not None:
        tier = tier_for_stage(stage_int)
    model_name = resolve_model(stage=stage_int, tier=tier, model=model)
    tier_name = str(tier.value if isinstance(tier, ModelTier) else (tier or ""))

    # Web search: never global for stages 0-2; stage 3+ only if explicitly True
    explicit_search = bool(web_search) if web_search is not None else False
    if web_search is None and openai_web_search_enabled_default() and web_search_allowed_for_stage(stage_int):
        explicit_search = False
    use_search = explicit_search and web_search_allowed_for_stage(stage_int)

    normalized = normalize_content_parts(content)
    input_chars = _content_char_count(normalized) + len(instructions or "")

    limit = max_input_chars_for_stage(stage_int)
    if limit is not None and input_chars > limit:
        raise AIInputTooLarge(
            f"Stage {stage_int} input too large ({input_chars} chars > {limit}). "
            f"Condense text or defer to a deeper stage.",
            stage=stage_int,
            chars=input_chars,
            limit=limit,
        )

    max_out = clamp_max_output_tokens(stage_int, max_output_tokens)

    source_hash = content_source_hash(normalized, instructions)
    fingerprint = analysis_fingerprint(
        notice_id=notice_id,
        task=task,
        model_tier=tier_name or ModelTier.CHEAP.value,
        source_hash=source_hash,
        schema_version=schema_version,
        funnel_stage=stage_int,
    )
    if use_cache:
        cached = get_cached_analysis(fingerprint)
        if cached and cached.get("result_text") is not None:
            _LAST_RESPONSE_META = {
                "cache_hit": True,
                "fingerprint": fingerprint,
                "estimated_cost_usd": 0.0,
                "openai_called": False,
            }
            log_ai_usage(
                task=task,
                model=model_name,
                notice_id=notice_id,
                web_search=False,
                success=True,
                funnel_stage=stage_int,
                model_tier=tier_name,
                cache_hit=True,
                automatic=automatic,
                estimated_cost_usd=0.0,
            )
            return str(cached["result_text"])

    est_cost = estimate_pre_call_cost_usd(
        model=model_name,
        input_chars=input_chars,
        max_output_tokens=max_out,
        web_search=use_search,
    )
    if automatic:
        require_automatic_budget(stage=stage_int, estimated_cost_usd=est_cost)
        # Optional central CostGovernor enforcement (default off to avoid suite side-effects;
        # production enables via M3_COST_GOVERNOR_ENFORCE=1)
        import os

        if os.getenv("M3_COST_GOVERNOR_ENFORCE", "").strip().lower() in {"1", "true", "yes"}:
            from uuid import uuid4

            from cost_governor import get_cost_governor

            auth = get_cost_governor().authorize(
                {
                    "provider": "openai",
                    "action_type": "AI_COMPLETION",
                    "model": model_name,
                    "estimated_max_cost": float(est_cost) if est_cost else 0.05,
                    "research_stage": str(stage_int),
                    "question": f"openai_automatic_stage_{stage_int}",
                    "could_change_decision": True,
                    "idempotency_key": f"openai-call-{uuid4().hex}",
                }
            )
            if not auth.get("authorized"):
                raise AIDollarBudgetExceeded(
                    auth.get("reason") or "CostGovernor blocked paid OpenAI call",
                    reason="cost_governor",
                )

    kwargs: dict[str, Any] = {
        "model": model_name,
        "input": [{"role": "user", "content": normalized}],
        "max_output_tokens": max_out,
    }
    if instructions:
        kwargs["instructions"] = instructions
    if use_search:
        kwargs["tools"] = [{"type": "web_search"}]
    if text_format:
        kwargs["text"] = {"format": text_format}

    try:
        client = get_openai_client()
        response = client.responses.create(**kwargs)
        usage = extract_usage_tokens(response)
        cost = estimate_call_cost_usd(
            model=model_name,
            input_tokens=usage["input_tokens"] or 0,
            cached_input_tokens=usage["cached_input_tokens"] or 0,
            output_tokens=usage["output_tokens"] or 0,
            reasoning_tokens=usage["reasoning_tokens"] or 0,
            web_search=use_search,
        )
        text = response_output_text(response)
        _LAST_RESPONSE_META = {
            "cache_hit": False,
            "fingerprint": fingerprint,
            "estimated_cost_usd": cost,
            "openai_called": True,
            "model": model_name,
            "request_id": getattr(response, "id", None),
            "input_tokens": usage.get("input_tokens"),
            "cached_input_tokens": usage.get("cached_input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "reasoning_tokens": usage.get("reasoning_tokens"),
            "total_tokens": usage.get("total_tokens"),
        }
        log_ai_usage(
            task=task,
            model=model_name,
            notice_id=notice_id,
            web_search=use_search,
            success=True,
            input_tokens=usage["input_tokens"],
            cached_input_tokens=usage["cached_input_tokens"],
            output_tokens=usage["output_tokens"],
            reasoning_tokens=usage["reasoning_tokens"],
            total_tokens=usage["total_tokens"],
            funnel_stage=stage_int,
            model_tier=tier_name,
            estimated_cost_usd=cost,
            automatic=automatic,
        )
        if use_cache and text:
            stored = put_cached_analysis(
                fingerprint,
                result_text=text,
                meta={"task": task, "model": model_name, "funnel_stage": stage_int, "notice_id": notice_id},
            )
            if not stored:
                logger.warning("Stage %s cache persist failed for task=%s", stage_int, task)
        return text
    except (AIDollarBudgetExceeded, AIStageBudgetExceeded, AIInputTooLarge):
        raise
    except Exception as exc:
        log_ai_usage(
            task=task,
            model=model_name,
            notice_id=notice_id,
            web_search=use_search,
            success=False,
            error=str(exc),
            funnel_stage=stage_int,
            model_tier=tier_name,
            automatic=automatic,
            estimated_cost_usd=0.0,
        )
        raise


def text_part(text: str) -> dict[str, Any]:
    return {"type": "input_text", "text": text}


def pdf_file_part(filename: str, data: bytes) -> dict[str, Any]:
    import base64

    b64 = base64.standard_b64encode(data).decode("ascii")
    return {
        "type": "input_file",
        "filename": filename or "document.pdf",
        "file_data": f"data:application/pdf;base64,{b64}",
    }


def image_part(png_bytes: bytes) -> dict[str, Any]:
    import base64

    b64 = base64.standard_b64encode(png_bytes).decode("ascii")
    return {
        "type": "input_image",
        "image_url": f"data:image/png;base64,{b64}",
    }


# Re-export budget snapshot helper for settings/UI
def ai_cost_snapshot() -> dict[str, Any]:
    return get_cost_snapshot()
