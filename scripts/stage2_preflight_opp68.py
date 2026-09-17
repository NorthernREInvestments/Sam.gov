"""Stage 2 REAL-DATA PREFLIGHT for Opportunity 68 (post v2 compaction).

ABSOLUTE MAXIMUM LIVE OPENAI REQUESTS = 0.
Stops after Stage 1 current resolution + Stage 2 prep/fingerprint.
Does NOT execute Stage 1 or Stage 2 model calls.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


class OpenAICallAttemptedDuringStage2Preflight(RuntimeError):
    pass


def _kw_present(text: str, patterns: list[str]) -> bool:
    if not text:
        return False
    for p in patterns:
        if re.search(p, text, re.I):
            return True
    return False


def _classify(structured: bool, local_text: bool) -> str:
    if structured:
        return "PRESENT_STRUCTURED"
    if local_text:
        return "PRESENT_LOCAL_TEXT"
    return "UNKNOWN"


def main() -> int:
    import openai_runtime
    from ai_analysis_cache import analysis_fingerprint, content_source_hash, get_cached_analysis
    from ai_cost_budget import stage_monthly_ceiling_usd
    from ai_funnel import stage0_evaluate
    from ai_model_router import FunnelStage, ModelTier, model_for_tier
    from ai_pricing import breakdown_call_cost_usd, estimate_tokens_from_chars, rates_for_model
    from ai_stage1 import (
        HISTORICAL_STAGE1_FP_OPP68,
        compute_stage1_fingerprint,
        resolve_current_stage1_result,
    )
    from ai_stage2 import (
        STAGE2_INSTRUCTIONS,
        STAGE2_INSTRUCTION_VERSION,
        STAGE2_MAX_OUTPUT_TOKENS,
        STAGE2_SCHEMA_VERSION,
        STAGE2_TASK,
        build_stage2_input,
        can_run_stage2,
        measure_stage2_output_fixtures,
        stage2_schema_fingerprint_component,
    )
    from database import SessionLocal
    from models import Contract, ContractAttachment

    openai_entered = {"n": 0}

    def block_openai(*args: Any, **kwargs: Any) -> None:
        openai_entered["n"] += 1
        raise OpenAICallAttemptedDuringStage2Preflight(
            "OPENAI_CALL_ATTEMPTED_DURING_STAGE2_PREFLIGHT"
        )

    openai_runtime.get_openai_client = block_openai  # type: ignore[assignment]

    session = SessionLocal()
    try:
        row = session.query(Contract).filter_by(id=68).first()
        if not row:
            print(json.dumps({"error": "opportunity_68_not_found"}, indent=2))
            return 2
        notice_id = row.notice_id
        title = row.title
        description = row.description or ""
        attachment_text = row.attachment_text or ""
        analysis = row.analysis if isinstance(row.analysis, dict) else {}
        sam_raw = row.sam_raw if isinstance(row.sam_raw, dict) else {}
        estimated_value = row.estimated_value
        naics = row.naics_code
        set_aside = row.set_aside
        due_date = row.due_date
        agency = row.agency
        location = row.location
        status = row.status
        attachments = [
            {
                "id": a.id,
                "filename": a.filename,
                "extracted_text_len": len(a.extracted_text or ""),
                "has_extracted_text": bool((a.extracted_text or "").strip()),
            }
            for a in session.query(ContractAttachment).filter_by(contract_id=68).all()
        ]
    finally:
        session.close()

    s0 = stage0_evaluate(row)
    s1_components = compute_stage1_fingerprint(row, stage0=s0)
    s1_resolution = resolve_current_stage1_result(row, stage0=s0)
    gate = can_run_stage2(row, stage0=s0, stage1_resolution=s1_resolution)

    # Historical Stage 1 result — STALE fixture only (not production authorization)
    hist = get_cached_analysis(HISTORICAL_STAGE1_FP_OPP68)
    historical_fixture = None
    if hist and hist.get("result_text"):
        try:
            historical_fixture = json.loads(hist["result_text"])
        except json.JSONDecodeError:
            from openai_runtime import extract_json_object

            historical_fixture = extract_json_object(hist["result_text"])
        if isinstance(historical_fixture, dict):
            historical_fixture = {
                **historical_fixture,
                "_fixture_authorize": True,
                "_stale_historical": True,
                "_historical_fingerprint": HISTORICAL_STAGE1_FP_OPP68,
            }

    # Stage 2 payload prep always (for schema/cost measurement). Production eligibility separate.
    s1_for_payload = s1_resolution.get("result") if s1_resolution.get("status") == "HIT" else None
    payload_mode = "CURRENT_STAGE1"
    if s1_for_payload is None and historical_fixture is not None:
        s1_for_payload = historical_fixture
        payload_mode = "STALE_HISTORICAL_FIXTURE_ONLY"

    built = build_stage2_input(row, stage0=s0, stage1=s1_for_payload)
    manifest = built["manifest"]
    corpus = built["corpus"]
    oversize = built["oversize"]

    duplicates_removed = sum(1 for d in manifest if d.get("duplicate_of"))
    with_text = [d for d in manifest if d.get("text") and not d.get("duplicate_of")]
    unique_retained = sum(1 for d in manifest if not d.get("duplicate_of"))

    structured_chars = description_chars = document_chars = analysis_chars = 0
    inventory = []
    for d in manifest:
        text = "" if d.get("duplicate_of") else (d.get("text") or "")
        n = len(text)
        dtype = d.get("document_type")
        if not d.get("duplicate_of"):
            if dtype == "structured_fields":
                structured_chars += n
            elif dtype == "description":
                description_chars += n
            elif dtype in {"pws_extract", "solicitation_extract"}:
                analysis_chars += n
            else:
                document_chars += n
        inventory.append(
            {
                "document_id": d.get("document_id"),
                "filename_title": d.get("filename") or d.get("title"),
                "document_type": dtype,
                "source": d.get("source"),
                "text_available": bool(text),
                "character_count": n,
                "content_hash_prefix": (d.get("content_hash") or "")[:12] or None,
                "duplicate_of": d.get("duplicate_of"),
                "amendment_status": d.get("amendment_status"),
            }
        )

    corpus_chars = int(corpus.get("char_count") or 0)
    largest = max(with_text, key=lambda d: len(d.get("text") or ""), default=None)
    largest_chars = len((largest or {}).get("text") or "") if largest else 0
    largest_pct = round(100.0 * largest_chars / corpus_chars, 2) if corpus_chars else 0.0

    model_name = model_for_tier(ModelTier.CHEAP)
    schema_v = stage2_schema_fingerprint_component()
    content = [{"type": "input_text", "text": built["text"]}]
    input_chars_for_estimate = len(built["text"]) + len(STAGE2_INSTRUCTIONS)
    src_hash = content_source_hash(content, STAGE2_INSTRUCTIONS)
    fingerprint = analysis_fingerprint(
        notice_id=notice_id,
        task=STAGE2_TASK,
        model_tier=ModelTier.CHEAP.value,
        source_hash=src_hash,
        schema_version=schema_v,
        funnel_stage=FunnelStage.STAGE_2.value,
    )
    cached2 = get_cached_analysis(fingerprint)
    cache_status = "HIT" if cached2 and cached2.get("result_text") is not None else "MISS"

    est_input_tokens = estimate_tokens_from_chars(input_chars_for_estimate)
    max_out = STAGE2_MAX_OUTPUT_TOKENS
    rates = rates_for_model(model_name)
    cost_parts = breakdown_call_cost_usd(
        model=model_name, input_tokens=est_input_tokens, output_tokens=max_out, web_search=False
    )
    stage2_ceiling = stage_monthly_ceiling_usd(2)
    pct_of_ceiling = (
        round(100.0 * cost_parts["total_cost"] / stage2_ceiling, 4) if stage2_ceiling else None
    )

    limit = int(oversize.get("limit") or 80000)
    pct_limit = round(100.0 * corpus_chars / limit, 2) if limit else None
    fixtures = measure_stage2_output_fixtures()

    local_blob = "\n".join(d.get("text") or "" for d in with_text)
    psc_val = sam_raw.get("classificationCode") or sam_raw.get("classification_code")
    coverage = {
        "identity": _classify(bool(notice_id or title), _kw_present(local_blob, [r"solicitation", r"n0025326p0003"])),
        "NAICS": _classify(bool(naics), _kw_present(local_blob, [r"\bnaics\b", r"\b561730\b"])),
        "PSC": _classify(bool(psc_val), _kw_present(local_blob, [r"\bpsc\b", r"product service code"])),
        "set-aside": _classify(bool(set_aside), _kw_present(local_blob, [r"set[- ]?aside", r"small business"])),
        "deadline": _classify(
            bool(due_date) or bool(sam_raw.get("responseDeadLine")),
            _kw_present(local_blob, [r"offer due", r"deadline", r"closing"]),
        ),
        "estimated/award value": _classify(
            bool(estimated_value)
            or bool((sam_raw.get("award") or {}).get("amount") if isinstance(sam_raw.get("award"), dict) else None),
            _kw_present(local_blob, [r"\$\s*\d"]),
        ),
        "quantity": _classify(False, _kw_present(local_blob, [r"\bquantity\b", r"\bqty\b"])),
        "product/model/part number": _classify(False, _kw_present(local_blob, [r"\bpart\s*number\b", r"\bmodel\b"])),
        "place of performance": _classify(bool(location), _kw_present(local_blob, [r"kapolei", r"place of performance"])),
        "delivery": _classify(False, _kw_present(local_blob, [r"\bdelivery\b", r"f\.?o\.?b\.?"])),
        "installation": _classify(False, _kw_present(local_blob, [r"\binstall"])),
        "bond": _classify(False, _kw_present(local_blob, [r"\bbond\b"])),
        "license": _classify(False, _kw_present(local_blob, [r"\blicen[cs]e\b"])),
        "insurance": _classify(False, _kw_present(local_blob, [r"\binsurance\b"])),
        "wage": _classify(False, _kw_present(local_blob, [r"wage determination", r"prevailing wage", r"\bsca\b"])),
        "submission": _classify(False, _kw_present(local_blob, [r"submit", r"quote", r"proposal"])),
        "option periods": _classify(False, _kw_present(local_blob, [r"\boption\b", r"base period"])),
        "manufacturer authorization": _classify(False, _kw_present(local_blob, [r"manufacturer.?authoriz", r"oem"])),
    }

    openai_calls = openai_entered["n"]
    reasons: list[str] = []
    if openai_calls != 0:
        decision = "NOT_READY_FOR_LIVE_AI"
        reasons.append(f"openai_kill_switch_entered={openai_calls}")
    elif s1_resolution.get("status") != "HIT":
        decision = "NEEDS_ONE_LIVE_STAGE1_REFRESH_FIRST"
        reasons.append("STAGE1_CURRENT_RESULT_MISSING")
        reasons.append(
            "Current Stage 1 fingerprint differs from historical b3c7aeca… due to legitimate "
            "input/instruction changes (data-integrity value_status fields + DATA INTEGRITY "
            "instruction block + explicit instruction_version in fingerprint). Old cache is STALE."
        )
        if not oversize.get("ok"):
            decision = "NOT_READY_FOR_LIVE_AI"
            reasons.append("oversize_evidence")
        if fixtures["configured_ceiling"] < fixtures["max_fixture_est_tokens"]:
            decision = "NOT_READY_FOR_LIVE_AI"
            reasons.append("output_ceiling_below_measured_fixtures")
    elif not gate.get("allowed"):
        decision = "NOT_READY_FOR_LIVE_AI"
        reasons.append(f"entry_gate_blocked:{gate.get('reason')}")
    elif not oversize.get("ok"):
        decision = "NOT_READY_FOR_LIVE_AI"
        reasons.append("oversize_evidence")
    elif fixtures["configured_ceiling"] < fixtures["max_fixture_est_tokens"]:
        decision = "NOT_READY_FOR_LIVE_AI"
        reasons.append("output_ceiling_below_measured_fixtures")
    else:
        decision = "READY_FOR_ONE_LIVE_STAGE2_CALL"
        reasons.append("current_stage1_HIT_gate_ok_corpus_within_limit_fixtures_fit_ceiling")

    # Fingerprint audit components
    hist_meta = (hist or {}).get("meta") if hist else None
    audit = {
        "historical_fingerprint": HISTORICAL_STAGE1_FP_OPP68,
        "current_fingerprint": s1_components["fingerprint"],
        "same": s1_components["fingerprint"] == HISTORICAL_STAGE1_FP_OPP68,
        "current_components": {
            "source_hash": s1_components["source_hash"],
            "schema_version": s1_components["schema_version"],
            "instruction_version": s1_components["instruction_version"],
            "schema_fingerprint_component": s1_components["schema_fingerprint_component"],
            "model": s1_components["model"],
            "tier": s1_components["tier"],
            "task": s1_components["task"],
            "funnel_stage": s1_components["funnel_stage"],
            "input_chars": s1_components["input_chars"],
        },
        "historical_cache_meta": hist_meta,
        "legitimate_invalidation": True,
        "explanation": (
            "Source hash changed because Stage 1 input now includes data-integrity fields "
            "(value_status/value_confidence) and STAGE1_INSTRUCTIONS include a DATA INTEGRITY "
            "block (hashed into content_source_hash). Schema fingerprint component also now "
            "includes STAGE1_INSTRUCTION_VERSION. Historical meta recorded schema "
            "'stage1-compact-v2' only. This is correct invalidation — do not force old FP."
        ),
    }

    report = {
        "STAGE_2_REAL_DATA_PREFLIGHT": True,
        "opportunity": {
            "id": 68,
            "notice_id": notice_id,
            "title": title,
            "status": status,
            "db_attachments": attachments,
        },
        "stage1_fingerprint_audit": audit,
        "stage1_current_resolution": {
            "status": s1_resolution.get("status"),
            "fingerprint": s1_resolution.get("fingerprint"),
            "historical_stale": s1_resolution.get("historical_stale"),
            "openai_called": s1_resolution.get("openai_called"),
        },
        "entry_gate_production": {
            "allowed": gate.get("allowed"),
            "reason": gate.get("reason"),
            "stage0_decision": s0.get("decision"),
        },
        "stage2_payload_mode": payload_mode,
        "document_inventory": inventory,
        "documents_discovered": len(manifest),
        "documents_with_usable_text": len(with_text),
        "duplicates_removed": duplicates_removed,
        "unique_retained": unique_retained,
        "corpus": {
            "structured_chars": structured_chars,
            "description_chars": description_chars,
            "document_chars": document_chars,
            "analysis_extract_chars": analysis_chars,
            "corpus_chars": corpus_chars,
            "estimated_input_tokens": est_input_tokens,
            "largest_source_document_id": (largest or {}).get("document_id"),
            "largest_source_chars": largest_chars,
            "largest_source_pct_of_corpus": largest_pct,
        },
        "evidence_coverage": coverage,
        "size": {
            "limit": limit,
            "used_corpus_chars": corpus_chars,
            "percent_of_limit": pct_limit,
            "within_limit": bool(oversize.get("ok")),
        },
        "cache": {
            "fingerprint": fingerprint,
            "schema_version": STAGE2_SCHEMA_VERSION,
            "instruction_version": STAGE2_INSTRUCTION_VERSION,
            "model": model_name,
            "tier": ModelTier.CHEAP.value,
            "source_hash": src_hash,
            "cache": cache_status,
            "cache_miss_not_filled": True,
        },
        "output_fixtures": fixtures,
        "cost_preflight_ESTIMATED_NOT_BILLED": {
            "estimated_input_tokens": est_input_tokens,
            "max_output_tokens": max_out,
            "luna_rates_per_mtok": {"input": rates.get("input_per_mtok"), "output": rates.get("output_per_mtok")},
            "estimated_maximum_input_cost_usd": cost_parts["input_cost"],
            "estimated_maximum_output_cost_usd": cost_parts["output_cost"],
            "estimated_upper_bound_total_usd": cost_parts["total_cost"],
            "stage2_monthly_ceiling_usd": stage2_ceiling,
            "percent_of_8_usd_stage2_ceiling": pct_of_ceiling,
            "label": "ESTIMATED / NOT BILLED",
        },
        "orm_analysis_has_stage1_fields": bool(analysis.get("reason_code") or "advance" in analysis),
        "preflight_decision": decision,
        "reasons": reasons,
        "TOTAL_LIVE_OPENAI_REQUESTS": openai_calls,
    }
    print(json.dumps(report, indent=2, default=str))
    return 0 if openai_calls == 0 else 3


if __name__ == "__main__":
    sys.exit(main())
