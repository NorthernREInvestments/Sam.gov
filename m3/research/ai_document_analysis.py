"""OpenAI language-model solicitation document understanding (production path).

CRITICAL:
- This module is the real AI document-analysis path.
- Deterministic rules elsewhere are NOT AI understanding.
- Default allow_live=False — NO OpenAI API calls without explicit owner authorization.
- Offline validation uses mock_response only; never describe mocks as live AI analysis.
"""

from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from m3.config import get_settings
from m3.research.full_text_store import content_hash

PROMPT_VERSION = "m3_solicitation_doc_understanding_v1"

DOCUMENT_ANALYSIS_SYSTEM = (
    "You are M3 Supply's government solicitation document analyst. "
    "Read ONLY the provided solicitation source text. Never invent facts. "
    "If a field is not supported by a source passage, return null / UNKNOWN. "
    "Distinguish firm ordered quantities from estimates, evaluation quantities, "
    "and optional purchases. Identify products vs services/installation/maintenance/"
    "construction. Cite exact source snippets for every material claim. "
    "Return JSON only."
)

DOCUMENT_ANALYSIS_SCHEMA = {
    "procurement_purpose": "string",
    "requirement_class": (
        "product_supply|product_with_incidental_services|product_with_significant_installation|"
        "services|maintenance_repair_services|construction|professional_services|mixed_product_service|"
        "unknown"
    ),
    "is_product_purchase": "boolean",
    "line_items": [
        {
            "line_ref": "string|null",
            "description": "string",
            "quantity": "string|null",
            "unit": "string|null",
            "quantity_semantic": "ORDERED|ESTIMATED|EVALUATION|OPTIONAL|INDEFINITE|UNKNOWN",
            "specs": ["string"],
            "brand_or_model": "string|null",
            "equivalents_allowed": "boolean|null",
            "source_snippet": "string",
            "source_doc_id": "string|null",
        }
    ],
    "duplicate_line_reconciliation": "string|null",
    "technical_requirements": ["string"],
    "brand_restrictions": ["string"],
    "delivery_obligations": ["string"],
    "submission_requirements": ["string"],
    "eligibility_requirements": ["string"],
    "deadlines": [
        {
            "kind": "submission|opening|questions|other",
            "value": "string",
            "timezone": "string|null",
            "source_snippet": "string",
        }
    ],
    "hidden_obligations": [
        {
            "kind": "bond|insurance|license|mandatory_meeting|installation|other",
            "detail": "string",
            "source_snippet": "string",
            "blocks_product_only": "boolean",
        }
    ],
    "missing_information": ["string"],
    "coverage_limitations": ["string"],
    "confidence": "high|medium|low",
}


def analysis_cache_key(*, corpus_hash: str, model: str, prompt_version: str = PROMPT_VERSION) -> str:
    return f"doc_understanding:{prompt_version}:{model}:{corpus_hash}"


def _chunk_documents(
    documents: list[dict[str, Any]],
    *,
    max_chunk_chars: int = 20000,
) -> list[dict[str, Any]]:
    """Bounded document-aware chunks — never silently drop sources; emit multiple chunks."""
    chunks: list[dict[str, Any]] = []
    for doc in documents:
        if doc.get("is_metadata_only") or doc.get("is_listing_preview"):
            continue
        text = str(doc.get("text") or "")
        if len(text.strip()) < 40:
            continue
        doc_id = str(doc.get("doc_id") or "doc")
        if len(text) <= max_chunk_chars:
            chunks.append(
                {
                    "chunk_id": f"{doc_id}:0",
                    "doc_id": doc_id,
                    "text": text,
                    "chars": len(text),
                }
            )
            continue
        # Overlapping windows so tables/amendments at boundaries are not lost
        step = max_chunk_chars - 1500
        start = 0
        idx = 0
        while start < len(text):
            piece = text[start : start + max_chunk_chars]
            chunks.append(
                {
                    "chunk_id": f"{doc_id}:{idx}",
                    "doc_id": doc_id,
                    "text": piece,
                    "chars": len(piece),
                    "offset": start,
                }
            )
            idx += 1
            if start + max_chunk_chars >= len(text):
                break
            start += step
    return chunks


def build_model_input_from_evidence(package: dict[str, Any]) -> dict[str, Any]:
    """Construct the exact payload the model would receive (for live or mock)."""
    docs = [
        d
        for d in (package.get("document_texts") or [])
        if not d.get("is_metadata_only") and not d.get("is_listing_preview")
    ]
    # Always include substantive docs; if only listing exists, mark coverage limitation
    if not docs:
        docs = [d for d in (package.get("document_texts") or []) if (d.get("text") or "").strip()]
    chunks = _chunk_documents(docs)
    corpus = "\n\n".join(
        f"[{c['chunk_id']} doc={c['doc_id']}]\n{c['text']}" for c in chunks
    )
    ch = content_hash(corpus)
    return {
        "title": package.get("title"),
        "nb_id": package.get("nb_id"),
        "chunks": chunks,
        "chunk_count": len(chunks),
        "corpus_chars": len(corpus),
        "corpus_hash": ch,
        "source_refs": package.get("source_refs") or [],
        "coverage": package.get("coverage") or {},
        "prompt_version": PROMPT_VERSION,
        "system": DOCUMENT_ANALYSIS_SYSTEM,
        "schema": DOCUMENT_ANALYSIS_SCHEMA,
        "user_prompt_head": (
            f"Analyze this government solicitation for M3 Supply (tangible product resale).\n"
            f"NB_ID: {package.get('nb_id')}\nTITLE: {package.get('title')}\n"
            f"Return JSON matching the schema. Cite source_doc_id / snippets from the labeled chunks.\n"
            f"CHUNK_COUNT: {len(chunks)} CORPUS_CHARS: {len(corpus)}\n\n"
        ),
        "corpus": corpus,
    }


def _load_analysis_cache(db: Session | None, cache_key: str) -> dict[str, Any] | None:
    if db is None:
        return None
    try:
        from m3.models import ResearchCache

        row = db.query(ResearchCache).filter_by(cache_key=cache_key).one_or_none()
        if not row or not isinstance(row.payload, dict):
            return None
        return dict(row.payload)
    except Exception:
        return None


def _save_analysis_cache(db: Session | None, cache_key: str, payload: dict[str, Any]) -> None:
    if db is None:
        return
    try:
        from m3.models import ResearchCache

        row = db.query(ResearchCache).filter_by(cache_key=cache_key).one_or_none()
        if not row:
            row = ResearchCache(cache_key=cache_key, kind="solicitation_doc_understanding", payload=payload)
        else:
            row.payload = payload
        db.add(row)
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


def reconcile_chunk_analyses(parts: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge per-chunk structured outputs without inventing fields."""
    if not parts:
        return {
            "procurement_purpose": None,
            "requirement_class": "unknown",
            "is_product_purchase": None,
            "line_items": [],
            "missing_information": ["no_chunk_analyses"],
            "coverage_limitations": ["empty_analysis"],
            "confidence": "low",
        }
    if len(parts) == 1 and isinstance(parts[0], dict):
        return dict(parts[0])
    merged = {
        "procurement_purpose": None,
        "requirement_class": "unknown",
        "is_product_purchase": None,
        "line_items": [],
        "duplicate_line_reconciliation": None,
        "technical_requirements": [],
        "brand_restrictions": [],
        "delivery_obligations": [],
        "submission_requirements": [],
        "eligibility_requirements": [],
        "deadlines": [],
        "hidden_obligations": [],
        "missing_information": [],
        "coverage_limitations": [],
        "confidence": "low",
    }
    seen_lines: set[str] = set()
    for part in parts:
        if not isinstance(part, dict):
            continue
        if not merged["procurement_purpose"] and part.get("procurement_purpose"):
            merged["procurement_purpose"] = part.get("procurement_purpose")
        if part.get("requirement_class") and part.get("requirement_class") != "unknown":
            merged["requirement_class"] = part.get("requirement_class")
        if part.get("is_product_purchase") is not None and merged["is_product_purchase"] is None:
            merged["is_product_purchase"] = part.get("is_product_purchase")
        for li in part.get("line_items") or []:
            if not isinstance(li, dict):
                continue
            key = f"{li.get('description')}|{li.get('quantity')}|{li.get('unit')}".lower()
            if key in seen_lines:
                continue
            seen_lines.add(key)
            merged["line_items"].append(li)
        for field in (
            "technical_requirements",
            "brand_restrictions",
            "delivery_obligations",
            "submission_requirements",
            "eligibility_requirements",
            "deadlines",
            "hidden_obligations",
            "missing_information",
            "coverage_limitations",
        ):
            for item in part.get(field) or []:
                if item not in merged[field]:
                    merged[field].append(item)
        if part.get("confidence") == "high":
            merged["confidence"] = "high"
        elif part.get("confidence") == "medium" and merged["confidence"] != "high":
            merged["confidence"] = "medium"
    if len(merged["line_items"]) > 1:
        merged["duplicate_line_reconciliation"] = (
            f"merged_{len(parts)}_chunks_deduped_to_{len(merged['line_items'])}_line_items"
        )
    return merged


def analyze_solicitation_documents(
    db: Session | None,
    *,
    evidence_package: dict[str, Any],
    allow_live: bool = False,
    mock_response: dict[str, Any] | None = None,
    force: bool = False,
    owner_live_authorization: str | None = None,
) -> dict[str, Any]:
    """Run (or simulate) language-model document understanding over assembled evidence.

    allow_live=False (default): no OpenAI network call.
    mock_response: offline fixture — labeled as mocked, not live AI.
    Live calls require allow_live=True AND owner_live_authorization == 'OWNER_AUTHORIZED_LIVE_OPENAI'.
    """
    from m3.research.local_evidence import cheap_prefilter_from_evidence

    pre = cheap_prefilter_from_evidence(evidence_package)
    model_input = build_model_input_from_evidence(evidence_package)
    settings = get_settings()
    model = settings.openai_default_model or "gpt-4o-mini"
    cache_key = analysis_cache_key(corpus_hash=model_input["corpus_hash"], model=model)

    base = {
        "ok": False,
        "live_ai_invoked": False,
        "mocked": False,
        "cached": False,
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "corpus_hash": model_input["corpus_hash"],
        "corpus_chars": model_input["corpus_chars"],
        "chunk_count": model_input["chunk_count"],
        "prefilter": pre,
        "openai_spent_usd": 0.0,
        "parsed": None,
        "error": None,
    }

    if pre.get("reject") == "obvious_non_product":
        return {
            **base,
            "ok": True,
            "skipped_ai": True,
            "skip_reason": "cheap_prefilter_obvious_non_product",
            "parsed": {
                "requirement_class": "services",
                "is_product_purchase": False,
                "line_items": [],
                "missing_information": [],
                "coverage_limitations": ["ai_not_invoked_obvious_non_product"],
                "confidence": "high",
            },
        }

    if not pre.get("ai_eligible") and mock_response is None:
        return {
            **base,
            "ok": False,
            "skipped_ai": True,
            "skip_reason": pre.get("reason"),
            "error": pre.get("reason"),
        }

    if not force:
        cached = _load_analysis_cache(db, cache_key)
        if cached and cached.get("parsed") is not None:
            return {
                **base,
                "ok": True,
                "cached": True,
                "live_ai_invoked": False,
                "parsed": cached.get("parsed"),
                "openai_spent_usd": 0.0,
                "cache_key": cache_key,
            }

    # Offline mock path — NOT live AI analysis
    if mock_response is not None:
        parsed = mock_response.get("parsed") if "parsed" in mock_response else mock_response
        result = {
            **base,
            "ok": True,
            "mocked": True,
            "live_ai_invoked": False,
            "parsed": parsed if isinstance(parsed, dict) else None,
            "note": "MOCKED_MODEL_RESPONSE — not live OpenAI analysis",
            "cache_key": cache_key,
        }
        _save_analysis_cache(db, cache_key, {"parsed": result["parsed"], "mocked": True})
        return result

    if not allow_live:
        return {
            **base,
            "ok": False,
            "error": "live_openai_disabled_allow_live_false",
            "skipped_ai": True,
            "skip_reason": "owner_has_not_authorized_live_openai_for_this_build",
            "prepared_model_input_chars": model_input["corpus_chars"],
            "estimated_input_tokens_approx": max(1, model_input["corpus_chars"] // 4),
        }

    if owner_live_authorization != "OWNER_AUTHORIZED_LIVE_OPENAI":
        return {
            **base,
            "ok": False,
            "error": "missing_owner_live_authorization_token",
            "skipped_ai": True,
        }

    # Live path — still gated by openai_ready + budget (not used in this assignment)
    from m3.research.budget import (
        BudgetDenied,
        commit_spend,
        ensure_budget_policy,
        estimate_response_cost,
        release_reservation,
        reserve_budget,
        spending_snapshot,
    )
    from m3.research.openai_client import _extract_usage, _output_text, _parse_json_blob, openai_ready

    ready, reason = openai_ready()
    if not ready:
        return {**base, "ok": False, "error": reason}

    policy = ensure_budget_policy(db) if db is not None else None
    if policy is not None and not policy.paid_research_enabled:
        return {**base, "ok": False, "error": "paid_research_disabled"}

    # Fail-closed budget
    if db is None:
        return {**base, "ok": False, "error": "db_required_for_live_budget"}
    snap = spending_snapshot(db)
    remaining = Decimal(str(snap.get("today_remaining_usd") or 0))
    if remaining < Decimal("0.01"):
        return {**base, "ok": False, "error": "daily_budget_exhausted", "budget_denied": True}

    reserve_amt = Decimal("0.05")
    if remaining < reserve_amt:
        reserve_amt = remaining
    try:
        reservation = reserve_budget(
            db,
            user_id=None,
            objective="solicitation_document_understanding",
            model=model,
            estimated_usd=reserve_amt,
            meta={"corpus_hash": model_input["corpus_hash"], "nb_id": evidence_package.get("nb_id")},
        )
    except BudgetDenied as e:
        return {**base, "ok": False, "error": e.reason, "budget_denied": True}

    # Multi-chunk: one call per chunk then reconcile (bounded)
    try:
        from openai import OpenAI

        client = OpenAI(api_key=settings.openai_api_key)
        partials: list[dict[str, Any]] = []
        total_in = 0
        total_out = 0
        for chunk in model_input["chunks"][:8]:  # hard cap chunks per solicitation
            prompt = (
                model_input["user_prompt_head"]
                + f"CHUNK {chunk['chunk_id']}:\n{chunk['text']}\n\n"
                + "Respond with JSON only matching the schema."
            )
            resp = client.responses.create(
                model=model,
                input=[
                    {"role": "system", "content": DOCUMENT_ANALYSIS_SYSTEM},
                    {"role": "user", "content": prompt},
                ],
            )
            usage = _extract_usage(resp)
            total_in += usage["input_tokens"]
            total_out += usage["output_tokens"]
            parsed = _parse_json_blob(_output_text(resp))
            if isinstance(parsed, dict):
                partials.append(parsed)
        merged = reconcile_chunk_analyses(partials)
        cost = estimate_response_cost(
            model=model, input_tokens=total_in, output_tokens=total_out, web_search_calls=0
        )
        commit_spend(
            db,
            reservation,
            actual_estimated_usd=cost,
            input_tokens=total_in,
            output_tokens=total_out,
            web_search_calls=0,
            meta={"objective": "solicitation_document_understanding", "chunks": len(partials)},
        )
        result = {
            **base,
            "ok": True,
            "live_ai_invoked": True,
            "mocked": False,
            "parsed": merged,
            "openai_spent_usd": float(cost),
            "input_tokens": total_in,
            "output_tokens": total_out,
            "cache_key": cache_key,
        }
        _save_analysis_cache(
            db,
            cache_key,
            {"parsed": merged, "mocked": False, "live": True, "model": model},
        )
        return result
    except Exception as exc:
        release_reservation(db, reservation, reason=f"{type(exc).__name__}:{exc}")
        return {**base, "ok": False, "error": f"{type(exc).__name__}: {exc}"[:400]}


def build_single_request_corpus(evidence_package: dict[str, Any]) -> dict[str, Any]:
    """Build one non-truncated corpus from substantive local docs (deduped).

    Used for owner-authorized single-call live tests where multi-chunk is forbidden.
    """
    seen_hashes: set[str] = set()
    parts: list[str] = []
    refs: list[dict[str, Any]] = []
    for d in evidence_package.get("document_texts") or []:
        if d.get("is_metadata_only") or d.get("is_listing_preview"):
            continue
        text = str(d.get("text") or "")
        if len(text.strip()) < 40:
            continue
        h = content_hash(text)
        if h in seen_hashes:
            continue
        seen_hashes.add(h)
        doc_id = str(d.get("doc_id") or "doc")
        parts.append(f"=== SOURCE {doc_id} ({d.get('kind')}) path={d.get('source_path')} ===\n{text}")
        refs.append(
            {
                "doc_id": doc_id,
                "kind": d.get("kind"),
                "source_path": d.get("source_path"),
                "chars": len(text),
            }
        )
    corpus = "\n\n".join(parts)
    return {
        "corpus": corpus,
        "corpus_chars": len(corpus),
        "corpus_hash": content_hash(corpus),
        "source_refs": refs,
        "doc_count": len(refs),
        "approx_input_tokens": max(1, len(corpus) // 4),
    }


def analyze_solicitation_one_shot_live(
    db: Session | None,
    *,
    evidence_package: dict[str, Any],
    owner_live_authorization: str,
    max_spend_usd: float = 0.05,
    model: str | None = None,
) -> dict[str, Any]:
    """Owner-authorized SINGLE OpenAI Responses call over the full assembled corpus.

    Refuses if authorization token missing, if estimated cost exceeds max_spend_usd,
    or if multi-call would be required by caller policy (this function never loops).
    No NextBid. No retries.
    """
    from decimal import Decimal

    from m3.research.openai_client import _extract_usage, _output_text, _parse_json_blob

    if owner_live_authorization != "OWNER_AUTHORIZED_LIVE_OPENAI":
        return {
            "ok": False,
            "live_ai_invoked": False,
            "error": "missing_owner_live_authorization_token",
            "openai_spent_usd": 0.0,
        }

    settings = get_settings()
    model = model or settings.openai_default_model or "gpt-4o-mini"
    single = build_single_request_corpus(evidence_package)
    # Context check: gpt-4o-mini / gpt-4o family ≥ 128k; keep headroom
    approx_in = single["approx_input_tokens"] + 1500
    if approx_in > 100_000:
        return {
            "ok": False,
            "live_ai_invoked": False,
            "error": "corpus_exceeds_safe_single_request_context",
            "approx_input_tokens": approx_in,
            "openai_spent_usd": 0.0,
            "stopped_before_spend": True,
        }

    # Conservative max cost using published gpt-4o-mini rates + large output allowance
    worst_out = 8000
    worst_usd = (approx_in / 1_000_000) * 0.15 + (worst_out / 1_000_000) * 0.60
    if worst_usd > float(max_spend_usd):
        return {
            "ok": False,
            "live_ai_invoked": False,
            "error": "worst_case_cost_exceeds_owner_cap",
            "worst_case_usd": round(worst_usd, 4),
            "max_spend_usd": max_spend_usd,
            "openai_spent_usd": 0.0,
            "stopped_before_spend": True,
        }

    api_key = (settings.openai_api_key or "").strip()
    if not api_key:
        return {"ok": False, "live_ai_invoked": False, "error": "OPENAI_API_KEY missing", "openai_spent_usd": 0.0}

    prompt = (
        "Independently interpret this COMPLETE locally retained government solicitation package "
        "for M3 Supply (tangible product resale).\n"
        "Include all available tables, schedules, specifications, amendments, and requirements "
        "present in the source text below.\n"
        "Extract: every product line item; quantities and units; technical specs; brand/model "
        "restrictions and equivalents; ancillary services; delivery/execution obligations; "
        "mandatory meetings; bonding/insurance/licensing; deadlines (with timezone if stated); "
        "eligibility; submission requirements.\n"
        "For EVERY material fact include source_snippet copied from the labeled SOURCE blocks.\n"
        "Distinguish firm ordered quantity vs evaluation/estimated quantity vs tank capacity/"
        "equipment rating vs optional purchases. Never invent.\n"
        f"NB_ID: {evidence_package.get('nb_id')}\n"
        f"TITLE: {evidence_package.get('title')}\n"
        f"DOC_COUNT: {single['doc_count']} CORPUS_CHARS: {single['corpus_chars']}\n\n"
        f"{single['corpus']}\n\n"
        "Respond with JSON only matching this schema:\n"
        + json.dumps(DOCUMENT_ANALYSIS_SCHEMA)
    )

    from openai import OpenAI

    client = OpenAI(api_key=api_key)
    resp = client.responses.create(
        model=model,
        input=[
            {"role": "system", "content": DOCUMENT_ANALYSIS_SYSTEM},
            {"role": "user", "content": prompt},
        ],
    )
    usage = _extract_usage(resp)
    # Prefer official usage when present; fall back to estimate
    from m3.research.budget import estimate_response_cost

    cost = estimate_response_cost(
        model=model,
        input_tokens=usage["input_tokens"],
        output_tokens=usage["output_tokens"],
        web_search_calls=0,
    )
    if float(cost) > float(max_spend_usd):
        # Call already happened — report breach; do not retry
        cost_note = "actual_estimate_exceeded_cap_after_single_call"
    else:
        cost_note = "within_owner_cap"

    text_out = _output_text(resp)
    parsed = _parse_json_blob(text_out)
    result = {
        "ok": True,
        "live_ai_invoked": True,
        "mocked": False,
        "single_request": True,
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "corpus_hash": single["corpus_hash"],
        "corpus_chars": single["corpus_chars"],
        "doc_count": single["doc_count"],
        "source_refs": single["source_refs"],
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "openai_spent_usd": float(cost),
        "cost_basis": "application_estimate_not_invoice",
        "cost_note": cost_note,
        "max_spend_usd": max_spend_usd,
        "response_id": getattr(resp, "id", None),
        "raw_text": text_out,
        "parsed": parsed if isinstance(parsed, dict) else None,
        "parse_ok": isinstance(parsed, dict),
    }
    if db is not None:
        try:
            _save_analysis_cache(
                db,
                analysis_cache_key(corpus_hash=single["corpus_hash"], model=model),
                {
                    "parsed": result["parsed"],
                    "live": True,
                    "mocked": False,
                    "single_request": True,
                    "response_id": result["response_id"],
                    "openai_spent_usd": result["openai_spent_usd"],
                    "input_tokens": result["input_tokens"],
                    "output_tokens": result["output_tokens"],
                },
            )
        except Exception:
            pass
    return result


def estimate_live_analysis_cost_usd(evidence_package: dict[str, Any]) -> dict[str, Any]:
    """Rough application estimate for one complete solicitation (not an invoice)."""
    model_input = build_model_input_from_evidence(evidence_package)
    # ~4 chars/token; gpt-4o-mini ballpark $0.15/1M in + $0.60/1M out
    in_tokens = max(1, model_input["corpus_chars"] // 4) + 800
    out_tokens = 1200 * max(1, min(8, model_input["chunk_count"]))
    in_cost = (in_tokens / 1_000_000) * 0.15
    out_cost = (out_tokens / 1_000_000) * 0.60
    return {
        "model": get_settings().openai_default_model or "gpt-4o-mini",
        "corpus_chars": model_input["corpus_chars"],
        "chunk_count": model_input["chunk_count"],
        "approx_input_tokens": in_tokens,
        "approx_output_tokens": out_tokens,
        "estimated_usd": round(in_cost + out_cost, 4),
        "cost_basis": "application_estimate_not_invoice",
        "live_ai_invoked": False,
    }
