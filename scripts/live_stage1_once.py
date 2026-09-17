"""One-shot Stage 0 + optional single Stage 1 live call for Opportunity 68.

LIVE_STAGE1=1 enables the single OpenAI request. FORCE_NOTICE_ID overrides selection.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")  # canonical: govtracker/.env

from ai_analysis_cache import (  # noqa: E402
    PROMPT_SCHEMA_VERSION,
    analysis_fingerprint,
    content_source_hash,
    get_cached_analysis,
    put_cached_analysis,
)
from ai_funnel import normalize_source, should_advance_to_next_stage, stage0_evaluate  # noqa: E402
from ai_model_router import ModelTier, resolve_model  # noqa: E402
from ai_pricing import breakdown_call_cost_usd, extract_usage_tokens  # noqa: E402
from ai_stage1 import (  # noqa: E402
    STAGE1_INSTRUCTIONS,
    STAGE1_MAX_OUTPUT_TOKENS,
    STAGE1_SCHEMA_VERSION,
    build_stage1_input,
    normalize_stage1_result,
    stage1_response_format,
    stage1_schema_fingerprint_component,
)
from database import SessionLocal  # noqa: E402
from models import Contract  # noqa: E402
from openai_runtime import extract_json_object  # noqa: E402


def main() -> int:
    live = os.getenv("LIVE_STAGE1", "").strip() == "1"
    force_id = (os.getenv("FORCE_NOTICE_ID") or "").strip()
    force_pk = (os.getenv("FORCE_CONTRACT_ID") or "68").strip()

    session = SessionLocal()
    try:
        row = None
        if force_id:
            row = session.query(Contract).filter_by(notice_id=force_id).first()
        if row is None and force_pk.isdigit():
            row = session.query(Contract).filter_by(id=int(force_pk)).first()
        if not row:
            print(json.dumps({"error": "opportunity_not_found"}))
            return 2

        raw = row.sam_raw if isinstance(row.sam_raw, dict) else {}
        opportunity = {
            "id": row.id,
            "notice_id": row.notice_id,
            "solicitation_number": raw.get("solicitationNumber"),
            "title": row.title,
            "source": normalize_source(row),
        }
        print("SELECTED_OPPORTUNITY")
        print(json.dumps(opportunity, indent=2, default=str))

        s0 = stage0_evaluate(row)
        print("STAGE0_RESULT")
        print(json.dumps(s0, indent=2, default=str))

        if s0.get("decision") == "REJECT" or not s0.get("advance", True):
            print("STOP_REASON: stage0_reject — no OpenAI call made")
            return 0

        built = build_stage1_input(row, stage0=s0)
        print("STAGE1_INPUT")
        print(
            json.dumps(
                {
                    "characters": built["char_count"],
                    "estimated_tokens": built["estimated_tokens"],
                    "pdf_count": 0,
                    "web_search": False,
                    "schema_version": STAGE1_SCHEMA_VERSION,
                    "max_output_tokens": STAGE1_MAX_OUTPUT_TOKENS,
                },
                indent=2,
            )
        )

        content = [{"type": "input_text", "text": built["text"]}]
        src_hash = content_source_hash(content, STAGE1_INSTRUCTIONS)
        fp = analysis_fingerprint(
            notice_id=row.notice_id,
            task="screen_contract_text",
            model_tier=ModelTier.CHEAP.value,
            source_hash=src_hash,
            schema_version=stage1_schema_fingerprint_component(),
            funnel_stage=1,
        )

        # v2 schema — do not reuse broken v1 cache
        cached = get_cached_analysis(fp)
        if cached and cached.get("result_text") and os.getenv("ALLOW_STAGE1_CACHE_HIT", "").strip() == "1":
            print("CACHE_HIT — skipping live call")
            result = normalize_stage1_result(extract_json_object(cached["result_text"]), stage0=s0)
            print("STAGE1_RESULT")
            print(json.dumps(result, indent=2, default=str))
            return 0

        key = (os.getenv("OPENAI_API_KEY") or "").strip()
        if not key:
            print("STOP_REASON: OPENAI_API_KEY missing — no OpenAI call")
            return 3
        if not live:
            print("STOP_REASON: LIVE_STAGE1 not set — dry run")
            print(json.dumps({"fingerprint": fp, "ready_for_live": True}))
            return 0

        from openai import OpenAI

        model = resolve_model(stage=1)
        print("LIVE OPENAI CALL ABOUT TO RUN")
        print(
            json.dumps(
                {
                    "model": model,
                    "opportunity_id": row.notice_id,
                    "input_character_count": built["char_count"],
                    "web_search": False,
                    "files": 0,
                    "max_output_tokens": STAGE1_MAX_OUTPUT_TOKENS,
                    "structured_output": True,
                },
                indent=2,
            )
        )

        client = OpenAI(api_key=key, timeout=120)
        response = client.responses.create(
            model=model,
            instructions=STAGE1_INSTRUCTIONS,
            input=[{"role": "user", "content": content}],
            max_output_tokens=STAGE1_MAX_OUTPUT_TOKENS,
            text={"format": stage1_response_format()},
        )
        # NO RETRIES.

        usage = extract_usage_tokens(response)
        text = getattr(response, "output_text", None) or ""
        if not text:
            chunks = []
            for item in getattr(response, "output", None) or []:
                for part in getattr(item, "content", None) or []:
                    if getattr(part, "text", None):
                        chunks.append(part.text)
            text = "".join(chunks)

        try:
            raw_json = extract_json_object(text)
            parse_ok = True
        except Exception:
            parse_ok = False
            raw_json = {
                "category": "UNKNOWN",
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
                "reason_code": "UNCERTAIN_ADVANCE",
                "_raw_text": (text or "")[:500],
            }

        result = normalize_stage1_result(raw_json, stage0=s0)

        # Persist ONLY after successful parse preference; still store raw for diagnostics
        store_text = text if text.strip().startswith("{") else json.dumps(raw_json)
        write_ok = put_cached_analysis(
            fp,
            result_text=store_text,
            meta={
                "task": "screen_contract_text",
                "model": getattr(response, "model", None) or model,
                "notice_id": row.notice_id,
                "schema": STAGE1_SCHEMA_VERSION,
            },
        )
        # Independent read-back
        read_back = get_cached_analysis(fp)
        read_ok = bool(read_back and read_back.get("result_text") == store_text)

        costs = breakdown_call_cost_usd(
            model=model,
            input_tokens=usage.get("input_tokens") or 0,
            cached_input_tokens=usage.get("cached_input_tokens") or 0,
            output_tokens=usage.get("output_tokens") or 0,
            web_search=False,
        )

        funnel = should_advance_to_next_stage(row, 1, evidence=result)

        print("OPENAI_USAGE")
        print(
            json.dumps(
                {
                    "model": getattr(response, "model", None) or model,
                    "id": getattr(response, "id", None),
                    "input_tokens": usage.get("input_tokens"),
                    "cached_input_tokens": usage.get("cached_input_tokens"),
                    "output_tokens": usage.get("output_tokens"),
                    "reasoning_tokens": usage.get("reasoning_tokens"),
                    "total_tokens": usage.get("total_tokens"),
                    "parse_ok": parse_ok,
                },
                indent=2,
                default=str,
            )
        )
        print("COST")
        print(
            json.dumps(
                {
                    **costs,
                    "reasoning_double_counted": False,
                    "note": "reasoning_tokens logged but not charged separately",
                },
                indent=2,
            )
        )
        print("STAGE1_RESULT")
        print(json.dumps(result, indent=2, default=str))
        print("CACHE")
        print(
            json.dumps(
                {
                    "fingerprint": fp,
                    "write_successful": write_ok,
                    "commit_and_verify": write_ok,
                    "independent_read_successful": read_ok,
                    "cached_matches_live": read_ok,
                },
                indent=2,
            )
        )
        print("FUNNEL")
        print(json.dumps(funnel, indent=2, default=str))
        print("LIVE_CALLS: 1 — STOPPING")
        return 0
    except Exception as exc:
        msg = str(exc)
        if "sk-" in msg or "api_key" in msg.lower() or "authorization" in msg.lower():
            msg = "API error (details redacted)"
        print("LIVE_CALL_FAILED")
        print(json.dumps({"error_type": type(exc).__name__, "error": msg[:500], "live_calls": 1, "retried": False}))
        return 1
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
