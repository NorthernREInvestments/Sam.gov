"""Owner-authorized bulk OpenAI solicitation document understanding.

Hard ceilings (enforced here):
- OpenAI job spend ≤ owner_max_usd (default $5.00)
- NextBid metered calls: ZERO
- No new downloads / acquisition / deploy / recurring jobs

Uses local evidence assembly → live model → source verification → owner queues.
Does NOT trust prior keyword classifications to exclude packages from AI review.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from m3.config import get_settings
from m3.research.ai_document_analysis import (
    DOCUMENT_ANALYSIS_SCHEMA,
    DOCUMENT_ANALYSIS_SYSTEM,
    PROMPT_VERSION,
    analysis_cache_key,
    analyze_solicitation_one_shot_live,
    build_model_input_from_evidence,
    build_single_request_corpus,
    estimate_live_analysis_cost_usd,
    reconcile_chunk_analyses,
    _save_analysis_cache,
    _load_analysis_cache,
)
from m3.research.ai_output_verify import verify_analysis_against_sources
from m3.research.budget import estimate_response_cost
from m3.research.local_evidence import assemble_local_evidence_package
from m3.research.openai_client import _extract_usage, _output_text, _parse_json_blob
from m3.research.owner_queues import empty_queues, map_to_primary_disposition, write_queues
from m3.research.queue_deadline_repair import demote_expired_from_owner_queues

OWNER_AUTH = "OWNER_AUTHORIZED_LIVE_OPENAI"
OWNER_BULK_AUTH = "OWNER_AUTHORIZED_BULK_AI_DOC_ANALYSIS"
DEFAULT_MAX_USD = Decimal("5.00")
MIN_SUBSTANTIVE_CHARS = 800


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _temp_root() -> Path:
    p = Path(__file__).resolve().parents[2] / "TEMP" / "bulk_ai_doc_analysis"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _results_root() -> Path:
    p = get_settings().data_root() / "ai_doc_understanding"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _load_inventory() -> dict[str, dict[str, Any]]:
    path = get_settings().data_root() / "nextbid_inventory" / "opportunities.jsonl"
    out: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return out
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            nb = str(row.get("nb_id") or "").strip()
            if nb:
                out[nb] = row
    return out


def _candidate_nb_ids(inv: dict[str, dict[str, Any]]) -> set[str]:
    ids = set(inv.keys())
    root = get_settings().data_root()
    ft = root / "solicitation_full_text"
    if ft.exists():
        ids.update(p.stem for p in ft.glob("*.json"))
    paid = root / "nextbid_paid_responses" / "by_nb_id"
    if paid.exists():
        ids.update(p.stem for p in paid.glob("*"))
    return ids


def _likely_content_ids() -> set[str]:
    """Fast prefilter: IDs with non-thin full_text and/or paid response index."""
    root = get_settings().data_root()
    out: set[str] = set()
    ft = root / "solicitation_full_text"
    if ft.exists():
        for p in ft.glob("*.json"):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            text = str(d.get("full_text") or "")
            if len(text.strip()) > 500 and not d.get("is_listing_preview_only"):
                out.add(p.stem)
    paid = root / "nextbid_paid_responses" / "by_nb_id"
    if paid.exists():
        out.update(p.stem for p in paid.glob("*"))
    return out


def _priority_key(row: dict[str, Any]) -> tuple:
    """Current/open state-local first, then remaining substantive."""
    status = str(row.get("status") or "").lower()
    openish = 0 if status in {"open", "active", "posted", "accepting", ""} else 1
    expired = 1 if row.get("deadline_expired") else 0
    # prefer nearer future deadlines among open
    dl = str(row.get("deadline") or "9999")
    return (expired, openish, dl, -int(row.get("chars") or 0), str(row.get("nb_id")))


def _deadline_expired(meta: dict[str, Any]) -> bool:
    from m3.research.commercial_criteria import evaluate_deadline_gate

    return bool(evaluate_deadline_gate(meta).get("expired"))


def inventory_local_packages(db: Session) -> dict[str, Any]:
    """Assemble coverage classification for all retained candidates (no OpenAI)."""
    inv = _load_inventory()
    all_ids = _candidate_nb_ids(inv)
    likely = _likely_content_ids()
    # Always try package-associated IDs from DB
    try:
        from m3.models import SolicitationPackage

        for pk in db.query(SolicitationPackage).all():
            k = str(pk.external_key or "")
            if k.startswith("nextbid:"):
                k = k.split(":", 1)[1]
            if k.startswith("NB"):
                likely.add(k)
    except Exception:
        pass

    buckets = {
        "complete_available_package": [],
        "substantive_incomplete": [],
        "notice_only": [],
        "thin_listing_preview": [],
        "metadata_only": [],
        "no_retained_content": [],
        "ai_eligible": [],
    }
    hash_to_nbs: dict[str, list[str]] = defaultdict(list)
    examined = 0

    # Thin inventory rows without likely content → thin/metadata without full assemble
    thin_ids = all_ids - likely
    for nb in thin_ids:
        meta = inv.get(nb) or {}
        buckets["thin_listing_preview"].append(
            {"nb_id": nb, "title": (meta.get("title") or "")[:120], "reason": "no_local_substantive_store"}
        )

    for nb in sorted(likely):
        examined += 1
        meta = dict(inv.get(nb) or {})
        evidence = assemble_local_evidence_package(
            nb,
            db=db,
            title=meta.get("title"),
            include_http_cache=False,
            include_package_db=False,  # fast inventory; full package text used at analysis time
        )
        cov = evidence.get("coverage") or {}
        title = str(evidence.get("title") or meta.get("title") or "")[:160]
        meta.setdefault("nb_id", nb)
        meta.setdefault("title", title)
        for k in ("deadline_at", "response_deadline", "solicitation_status", "lifecycle_status"):
            if evidence.get("inventory", {}).get(k) and not meta.get(k):
                meta[k] = evidence["inventory"][k]

        docs = evidence.get("document_texts") or []
        meta_only = all(d.get("is_metadata_only") for d in docs) if docs else False
        listing = bool(cov.get("listing_preview_count")) and not cov.get("has_substantive_document_content")
        chars = int(cov.get("total_content_chars") or 0)

        if not docs or (chars == 0 and not any((d.get("text") or "").strip() for d in docs)):
            buckets["no_retained_content"].append({"nb_id": nb, "title": title})
            continue
        if meta_only and not cov.get("has_substantive_document_content"):
            buckets["metadata_only"].append({"nb_id": nb, "title": title})
            continue
        if listing or chars <= 500:
            buckets["thin_listing_preview"].append({"nb_id": nb, "title": title, "chars": chars})
            continue

        single = build_single_request_corpus(evidence)
        if single["corpus_chars"] < MIN_SUBSTANTIVE_CHARS:
            buckets["substantive_incomplete"].append(
                {"nb_id": nb, "title": title, "chars": single["corpus_chars"]}
            )
            continue

        est = estimate_live_analysis_cost_usd(evidence)
        expired = _deadline_expired(meta)
        status = str(meta.get("solicitation_status") or meta.get("lifecycle_status") or "")
        row = {
            "nb_id": nb,
            "title": title,
            "chars": single["corpus_chars"],
            "corpus_hash": single["corpus_hash"],
            "doc_count": single["doc_count"],
            "approx_input_tokens": single["approx_input_tokens"],
            "estimated_usd": float(est["estimated_usd"]),
            "status": status,
            "deadline": meta.get("deadline_at") or meta.get("response_deadline"),
            "deadline_expired": expired,
            "coverage": {
                "substantive_doc_count": cov.get("substantive_doc_count"),
                "total_content_chars": cov.get("total_content_chars"),
                "primary_source": cov.get("primary_source"),
            },
            "sources_inspected": evidence.get("sources_inspected"),
        }
        # Distinguish complete-enough vs incomplete package (attachments may still be missing)
        if int(cov.get("substantive_doc_count") or 0) >= 1 and chars >= 2000:
            buckets["complete_available_package"].append(row)
        else:
            buckets["substantive_incomplete"].append(row)
            # still AI-eligible if substantive
        buckets["ai_eligible"].append(row)
        hash_to_nbs[single["corpus_hash"]].append(nb)

    # Sort eligible for processing order
    buckets["ai_eligible"].sort(key=_priority_key)
    unique_hashes = len(hash_to_nbs)
    est_unique = 0.0
    seen: set[str] = set()
    for r in buckets["ai_eligible"]:
        h = r["corpus_hash"]
        if h in seen:
            continue
        seen.add(h)
        est_unique += float(r["estimated_usd"])

    return {
        "inventoried_at": _utc(),
        "inventory_total_records": len(all_ids),
        "likely_content_candidates": len(likely),
        "examined_with_assembly": examined,
        "counts": {k: len(v) for k, v in buckets.items()},
        "unique_corpus_hashes": unique_hashes,
        "estimated_unique_openai_usd": round(est_unique, 4),
        "buckets": buckets,
        "hash_to_nbs": dict(hash_to_nbs),
        "nextbid_api_calls": 0,
        "openai_spent_usd": 0.0,
    }


def _analyze_chunked_live(
    db: Session | None,
    *,
    evidence: dict[str, Any],
    max_spend_usd: float,
    model: str,
) -> dict[str, Any]:
    """Bounded multi-chunk live analysis with hard per-solicitation spend cap."""
    settings = get_settings()
    api_key = (settings.openai_api_key or "").strip()
    if not api_key:
        return {"ok": False, "live_ai_invoked": False, "error": "OPENAI_API_KEY missing", "openai_spent_usd": 0.0}

    model_input = build_model_input_from_evidence(evidence)
    chunks = model_input["chunks"][:8]
    if not chunks:
        return {"ok": False, "live_ai_invoked": False, "error": "no_chunks", "openai_spent_usd": 0.0}

    from openai import OpenAI

    client = OpenAI(api_key=api_key)
    partials: list[dict[str, Any]] = []
    total_in = 0
    total_out = 0
    spent = Decimal("0")
    response_ids: list[str] = []

    for chunk in chunks:
        # Pre-check remaining room for another call (conservative)
        approx_in = max(1, int(chunk.get("chars") or 0) // 4) + 800
        worst = Decimal(str((approx_in / 1_000_000) * 0.15 + (2500 / 1_000_000) * 0.60))
        if spent + worst > Decimal(str(max_spend_usd)):
            return {
                "ok": False,
                "live_ai_invoked": bool(partials),
                "error": "chunk_would_exceed_remaining_cap",
                "openai_spent_usd": float(spent),
                "input_tokens": total_in,
                "output_tokens": total_out,
                "partials": len(partials),
                "stopped_before_spend": not partials,
                "parsed": reconcile_chunk_analyses(partials) if partials else None,
            }
        prompt = (
            model_input["user_prompt_head"]
            + f"CHUNK {chunk['chunk_id']}:\n{chunk['text']}\n\n"
            + "Respond with JSON only matching the schema:\n"
            + json.dumps(DOCUMENT_ANALYSIS_SCHEMA)
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
        cost = estimate_response_cost(
            model=model,
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            web_search_calls=0,
        )
        spent += Decimal(str(cost))
        rid = getattr(resp, "id", None)
        if rid:
            response_ids.append(str(rid))
        parsed = _parse_json_blob(_output_text(resp))
        if isinstance(parsed, dict):
            partials.append(parsed)
        if spent >= Decimal(str(max_spend_usd)):
            break

    merged = reconcile_chunk_analyses(partials)
    result = {
        "ok": True,
        "live_ai_invoked": True,
        "mocked": False,
        "single_request": False,
        "chunked": True,
        "chunks_analyzed": len(partials),
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "corpus_hash": model_input["corpus_hash"],
        "corpus_chars": model_input["corpus_chars"],
        "input_tokens": total_in,
        "output_tokens": total_out,
        "openai_spent_usd": float(spent),
        "cost_basis": "application_estimate_not_invoice",
        "response_ids": response_ids,
        "parsed": merged,
        "parse_ok": bool(merged),
    }
    if db is not None:
        _save_analysis_cache(
            db,
            analysis_cache_key(corpus_hash=model_input["corpus_hash"], model=model),
            {
                "parsed": merged,
                "live": True,
                "mocked": False,
                "chunked": True,
                "openai_spent_usd": float(spent),
                "input_tokens": total_in,
                "output_tokens": total_out,
            },
        )
    return result


def _live_analyze_package(
    db: Session,
    *,
    evidence: dict[str, Any],
    remaining_usd: Decimal,
    model: str,
) -> dict[str, Any]:
    """Choose one-shot or chunked; never spend without room under remaining_usd."""
    if remaining_usd < Decimal("0.001"):
        return {
            "ok": False,
            "live_ai_invoked": False,
            "error": "job_budget_exhausted",
            "openai_spent_usd": 0.0,
            "stopped_before_spend": True,
        }

    single = build_single_request_corpus(evidence)
    cache_key = analysis_cache_key(corpus_hash=single["corpus_hash"], model=model)
    cached = _load_analysis_cache(db, cache_key)
    if cached and cached.get("parsed") is not None and cached.get("live"):
        return {
            "ok": True,
            "cached": True,
            "live_ai_invoked": False,
            "parsed": cached.get("parsed"),
            "openai_spent_usd": 0.0,
            "corpus_hash": single["corpus_hash"],
            "corpus_chars": single["corpus_chars"],
            "model": model,
            "cache_key": cache_key,
        }

    # Prefer one-shot when safe under remaining budget
    approx_in = single["approx_input_tokens"] + 1500
    if approx_in <= 100_000:
        worst = Decimal(str((approx_in / 1_000_000) * 0.15 + (8000 / 1_000_000) * 0.60))
        if worst <= remaining_usd:
            return analyze_solicitation_one_shot_live(
                db,
                evidence_package=evidence,
                owner_live_authorization=OWNER_AUTH,
                max_spend_usd=float(min(remaining_usd, worst + Decimal("0.01"))),
                model=model,
            )
        if worst > remaining_usd:
            # try with tighter remaining as max — function checks worst case first
            return analyze_solicitation_one_shot_live(
                db,
                evidence_package=evidence,
                owner_live_authorization=OWNER_AUTH,
                max_spend_usd=float(remaining_usd),
                model=model,
            )

    # Large corpus → chunked within remaining
    return _analyze_chunked_live(
        db,
        evidence=evidence,
        max_spend_usd=float(remaining_usd),
        model=model,
    )


def _row_from_verification(
    nb_id: str,
    evidence: dict[str, Any],
    ai: dict[str, Any],
    verified: dict[str, Any],
) -> dict[str, Any]:
    item = dict(verified.get("item") or {})
    item["nb_id"] = nb_id
    item["title"] = evidence.get("title") or item.get("title")
    inv = evidence.get("inventory") or {}
    for k, v in inv.items():
        if v and not item.get(k):
            item[k] = v
    parsed = ai.get("parsed") or {}
    item["ai_document_understanding"] = {
        "model": ai.get("model"),
        "prompt_version": PROMPT_VERSION,
        "corpus_hash": ai.get("corpus_hash"),
        "live_ai_invoked": ai.get("live_ai_invoked"),
        "cached": ai.get("cached"),
        "openai_spent_usd": ai.get("openai_spent_usd"),
        "requirement_class": parsed.get("requirement_class"),
        "is_product_purchase": parsed.get("is_product_purchase"),
        "line_item_count": len(verified.get("verified_line_items") or []),
        "confidence": parsed.get("confidence"),
        "verified_at": _utc(),
    }
    item["verified_line_items"] = verified.get("verified_line_items") or []
    item["source_refs"] = evidence.get("source_refs") or []
    pipe = verified.get("pipeline") or {}
    bid = verified.get("bid") or {}
    promo = verified.get("promotion") or {}
    if verified.get("blocks_product_only"):
        pipe = dict(pipe)
        pipe["excluded"] = True
        pipe["product_opportunity"] = False
        pipe["reasons"] = list(pipe.get("reasons") or []) + ["ai_verified_service_or_install_blocker"]
    disposition = map_to_primary_disposition(item, pipe, bid=bid)
    if promo.get("bid_submission_ready") and not verified.get("blocks_product_only"):
        disposition = "verified_bid_opportunities"
    elif promo.get("supplier_quote_ready") and not (verified.get("deadline_gate") or {}).get("expired"):
        disposition = "verified_product_information_needed"
    elif verified.get("blocks_product_only"):
        disposition = "confirmed_disqualification"
    elif (verified.get("deadline_gate") or {}).get("expired"):
        disposition = "closed_expired_non_solicitation"
    item["disposition"] = disposition
    item["promotion"] = promo
    item["queue"] = disposition
    return {
        "item": item,
        "disposition": disposition,
        "pipe": pipe,
        "bid": bid,
        "verified": verified,
        "parsed": parsed,
    }


class AtomicJobBudget:
    """Thread-safe job ceiling: spent + in-flight reserved ≤ max_usd."""

    def __init__(self, max_usd: Decimal):
        self._lock = threading.Lock()
        self.max_usd = Decimal(max_usd)
        self.spent = Decimal("0")
        self.reserved = Decimal("0")
        self.stop = False
        self.stop_reason: str | None = None

    def try_reserve(self, amount: Decimal) -> bool:
        amt = Decimal(amount)
        if amt <= 0:
            amt = Decimal("0.002")
        with self._lock:
            if self.stop:
                return False
            if self.spent + self.reserved + amt > self.max_usd:
                self.stop = True
                self.stop_reason = self.stop_reason or "openai_job_ceiling_reached"
                return False
            self.reserved += amt
            return True

    def commit(self, reserved: Decimal, actual: Decimal) -> None:
        with self._lock:
            self.reserved = max(Decimal("0"), self.reserved - Decimal(reserved))
            self.spent += Decimal(actual)
            if self.spent >= self.max_usd:
                self.stop = True
                self.stop_reason = self.stop_reason or "openai_job_ceiling_reached"

    def release(self, reserved: Decimal) -> None:
        with self._lock:
            self.reserved = max(Decimal("0"), self.reserved - Decimal(reserved))

    def snapshot(self) -> dict[str, float]:
        with self._lock:
            return {
                "spent": float(self.spent),
                "reserved": float(self.reserved),
                "remaining": float(max(Decimal("0"), self.max_usd - self.spent - self.reserved)),
                "max_usd": float(self.max_usd),
            }


def _load_persisted_result(nb_id: str) -> dict[str, Any] | None:
    path = _results_root() / "by_nb_id" / f"{nb_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _worker_analyze_one(
    *,
    nb_id: str,
    title: str | None,
    inv_row: dict[str, Any],
    model: str,
    budget: AtomicJobBudget,
    reserve_usd: Decimal,
) -> dict[str, Any]:
    """Independent per-solicitation worker. Own DB session. Never mixes contexts."""
    from m3.db import SessionLocal

    db = SessionLocal()
    reserved = Decimal(reserve_usd)
    try:
        evidence = assemble_local_evidence_package(
            nb_id,
            db=db,
            title=title,
            include_http_cache=False,
            include_package_db=True,
        )
        single = build_single_request_corpus(evidence)
        if single["corpus_chars"] < MIN_SUBSTANTIVE_CHARS:
            budget.release(reserved)
            return {
                "ok": False,
                "nb_id": nb_id,
                "skipped": True,
                "error": "insufficient_substantive_after_full_assembly",
                "openai_spent_usd": 0.0,
            }

        # Disk/DB cache — no charge
        cache_key = analysis_cache_key(corpus_hash=single["corpus_hash"], model=model)
        cached = _load_analysis_cache(db, cache_key)
        if cached and cached.get("parsed") is not None and (cached.get("live") or cached.get("parsed")):
            budget.release(reserved)
            ai = {
                "ok": True,
                "cached": True,
                "live_ai_invoked": False,
                "parsed": cached.get("parsed"),
                "openai_spent_usd": 0.0,
                "corpus_hash": single["corpus_hash"],
                "corpus_chars": single["corpus_chars"],
                "model": model,
            }
            verified = verify_analysis_against_sources(ai["parsed"], evidence, item=dict(inv_row))
            packed = _row_from_verification(nb_id, evidence, ai, verified)
            _persist_result(nb_id, evidence, ai, verified, packed)
            return {
                "ok": True,
                "nb_id": nb_id,
                "cached": True,
                "ai": ai,
                "verified": verified,
                "packed": packed,
                "corpus_hash": single["corpus_hash"],
                "rate_limited": False,
            }

        persisted = _load_persisted_result(nb_id)
        if (
            persisted
            and persisted.get("parsed")
            and persisted.get("corpus_hash") == single["corpus_hash"]
            and persisted.get("live_ai_invoked")
        ):
            budget.release(reserved)
            ai = {
                "ok": True,
                "cached": True,
                "live_ai_invoked": False,
                "parsed": persisted.get("parsed"),
                "openai_spent_usd": 0.0,
                "corpus_hash": single["corpus_hash"],
                "model": model,
            }
            verified = verify_analysis_against_sources(ai["parsed"], evidence, item=dict(inv_row))
            packed = _row_from_verification(nb_id, evidence, ai, verified)
            return {
                "ok": True,
                "nb_id": nb_id,
                "cached": True,
                "resumed": True,
                "ai": ai,
                "verified": verified,
                "packed": packed,
                "corpus_hash": single["corpus_hash"],
                "rate_limited": False,
            }

        snap = budget.snapshot()
        remaining = Decimal(str(snap["remaining"])) + reserved  # this worker's reservation is available
        try:
            ai = _live_analyze_package(db, evidence=evidence, remaining_usd=remaining, model=model)
        except Exception as exc:
            msg = f"{type(exc).__name__}: {exc}"
            rate_limited = bool(re.search(r"(?i)rate.?limit|429|timeout|503|overloaded", msg))
            budget.release(reserved)
            return {
                "ok": False,
                "nb_id": nb_id,
                "error": msg[:400],
                "rate_limited": rate_limited,
                "openai_spent_usd": 0.0,
            }

        if ai.get("error") == "corpus_exceeds_safe_single_request_context":
            try:
                ai = _analyze_chunked_live(
                    db, evidence=evidence, max_spend_usd=float(remaining), model=model
                )
            except Exception as exc:
                msg = f"chunked:{type(exc).__name__}:{exc}"
                rate_limited = bool(re.search(r"(?i)rate.?limit|429|timeout|503|overloaded", msg))
                budget.release(reserved)
                return {
                    "ok": False,
                    "nb_id": nb_id,
                    "error": msg[:400],
                    "rate_limited": rate_limited,
                    "openai_spent_usd": 0.0,
                }

        cost = Decimal(str(ai.get("openai_spent_usd") or 0))
        if ai.get("live_ai_invoked"):
            budget.commit(reserved, cost)
        else:
            budget.release(reserved)
            if ai.get("stopped_before_spend") or not ai.get("ok"):
                return {
                    "ok": False,
                    "nb_id": nb_id,
                    "error": ai.get("error") or "analyze_failed",
                    "budget_stop": True,
                    "openai_spent_usd": float(cost),
                    "rate_limited": False,
                }

        if not ai.get("ok") and not ai.get("parsed"):
            return {
                "ok": False,
                "nb_id": nb_id,
                "error": ai.get("error") or "analyze_failed",
                "openai_spent_usd": float(cost),
                "rate_limited": False,
            }

        verified = verify_analysis_against_sources(ai.get("parsed"), evidence, item=dict(inv_row))
        packed = _row_from_verification(nb_id, evidence, ai, verified)
        _persist_result(nb_id, evidence, ai, verified, packed)
        return {
            "ok": True,
            "nb_id": nb_id,
            "cached": bool(ai.get("cached")),
            "ai": ai,
            "verified": verified,
            "packed": packed,
            "corpus_hash": ai.get("corpus_hash") or single["corpus_hash"],
            "rate_limited": False,
        }
    except Exception as exc:
        budget.release(reserved)
        msg = f"{type(exc).__name__}: {exc}"
        return {
            "ok": False,
            "nb_id": nb_id,
            "error": msg[:400],
            "rate_limited": bool(re.search(r"(?i)rate.?limit|429", msg)),
            "openai_spent_usd": 0.0,
        }
    finally:
        try:
            db.close()
        except Exception:
            pass


def run_bulk_ai_document_analysis(
    db: Session,
    *,
    owner_authorization: str,
    max_usd: float = 5.0,
    write_owner_queues_flag: bool = True,
    dry_run: bool = False,
    concurrency: int = 5,
) -> dict[str, Any]:
    """Parallel bulk worker: up to N concurrent independent OpenAI analyses, hard $ ceiling."""
    if owner_authorization != OWNER_BULK_AUTH:
        return {
            "ok": False,
            "error": "missing_owner_bulk_authorization",
            "openai_spent_usd": 0.0,
            "nextbid_api_calls": 0,
        }

    demote = demote_expired_from_owner_queues()
    print(json.dumps({"phase": "inventory_start", "at": _utc()}), flush=True)
    inv_report = inventory_local_packages(db)
    eligible = list(inv_report["buckets"]["ai_eligible"])
    model = get_settings().openai_default_model or "gpt-4o-mini"
    max_budget = Decimal(str(max_usd)).quantize(Decimal("0.0001"))
    budget = AtomicJobBudget(max_budget)
    workers = max(1, min(int(concurrency or 10), 10))

    queues = empty_queues()
    if write_owner_queues_flag:
        from m3.research.owner_queues import queues_root

        qroot = queues_root()
        for qname in list(queues.keys()):
            path = qroot / f"{qname}.json"
            if not path.exists():
                continue
            try:
                blob = json.loads(path.read_text(encoding="utf-8"))
                queues[qname] = list(blob.get("rows") or [])
            except Exception:
                pass
    analyzed_nb_set: set[str] = set()
    state_lock = threading.Lock()

    metrics = {
        "total_solicitation_records_inventoried": inv_report["inventory_total_records"],
        "locally_retained_substantive_packages": len(eligible),
        "packages_analyzed_by_openai": 0,
        "packages_skipped_missing_bodies": (
            inv_report["counts"]["thin_listing_preview"]
            + inv_report["counts"]["metadata_only"]
            + inv_report["counts"]["no_retained_content"]
        ),
        "packages_reused_from_cache": 0,
        "real_openai_requests": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "openai_spent_usd": 0.0,
        "confirmed_physical_product": 0,
        "service_construction_mixed": 0,
        "qty_uom_corrections": 0,
        "duplicate_line_corrections": 0,
        "previously_missed_products": 0,
        "previously_missed_service_obligations": 0,
        "complete_purchasing_requirements": 0,
        "supplier_pricing_ready": 0,
        "blocked_expired_deadline": 0,
        "needs_missing_attachments": 0,
        "unanalyzed_due_to_cost_cap": 0,
        "source_verification_failures": 0,
        "unresolved_ambiguities": 0,
        "concurrency_start": workers,
        "concurrency_final": workers,
        "rate_limit_events": 0,
    }

    analyzed_rows: list[dict[str, Any]] = []
    supplier_ready: list[dict[str, Any]] = []
    needs_info: list[dict[str, Any]] = []
    hash_done: dict[str, dict[str, Any]] = {}
    failures: list[dict[str, Any]] = []
    inv_meta = _load_inventory()

    print(
        json.dumps(
            {
                "phase": "inventory_done",
                "eligible": len(eligible),
                "unique_hashes": inv_report["unique_corpus_hashes"],
                "est_usd": inv_report["estimated_unique_openai_usd"],
                "inventory_total": inv_report["inventory_total_records"],
                "counts": inv_report["counts"],
            },
            default=str,
        ),
        flush=True,
    )

    if dry_run:
        report = {
            "ok": True,
            "dry_run": True,
            "concurrency": workers,
            "inventory": {
                k: inv_report[k]
                for k in (
                    "inventory_total_records",
                    "likely_content_candidates",
                    "counts",
                    "unique_corpus_hashes",
                    "estimated_unique_openai_usd",
                )
            },
            "metrics": metrics,
            "max_usd": float(max_budget),
            "demote_expired": demote,
            "nextbid_api_calls": 0,
            "note": "Dry run — no OpenAI calls",
        }
        (_temp_root() / "DRY_RUN.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        return report

    # Deduplicate work units by corpus_hash (analyze once, apply to siblings)
    work_units: list[dict[str, Any]] = []
    seen_hash: set[str] = set()
    siblings: dict[str, list[str]] = defaultdict(list)
    for row in eligible:
        h = row["corpus_hash"]
        siblings[h].append(row["nb_id"])
        if h in seen_hash:
            continue
        seen_hash.add(h)
        work_units.append(row)

    def _apply_result(result: dict[str, Any], *, primary_row: dict[str, Any] | None = None) -> None:
        nonlocal workers
        with state_lock:
            if result.get("rate_limited"):
                metrics["rate_limit_events"] += 1
                workers = max(1, workers - 1)
                metrics["concurrency_final"] = workers
            if not result.get("ok"):
                failures.append(
                    {"nb_id": result.get("nb_id"), "error": result.get("error"), "budget_stop": result.get("budget_stop")}
                )
                return
            ai = result["ai"]
            verified = result["verified"]
            packed = result["packed"]
            nb = result["nb_id"]
            ch = result.get("corpus_hash")
            if ch:
                hash_done[ch] = {"parsed": ai.get("parsed"), "nb_id": nb}
            analyzed_rows.append(_summary_row(packed, ai, reused=bool(result.get("cached"))))
            _accumulate_metrics(metrics, packed, verified)
            if ai.get("live_ai_invoked"):
                metrics["packages_analyzed_by_openai"] += 1
                metrics["real_openai_requests"] += (
                    1 if ai.get("single_request") else int(ai.get("chunks_analyzed") or 1)
                )
                metrics["input_tokens"] += int(ai.get("input_tokens") or 0)
                metrics["output_tokens"] += int(ai.get("output_tokens") or 0)
            elif result.get("cached"):
                metrics["packages_reused_from_cache"] += 1
            if verified.get("rejected_assertions"):
                metrics["source_verification_failures"] += len(verified.get("rejected_assertions") or [])
            if verified.get("unknown_fields"):
                metrics["unresolved_ambiguities"] += len(verified.get("unknown_fields") or [])
            analyzed_nb_set.add(nb)
            if write_owner_queues_flag:
                _enqueue(queues, packed, analyzed_nb_set)
            promo_now = (verified.get("promotion") or {})
            if promo_now.get("supplier_quote_ready") or packed["disposition"] == "verified_bid_opportunities":
                supplier_ready.append(_owner_table_row(packed))
            elif packed["disposition"] == "verified_product_information_needed":
                needs_info.append(_owner_table_row(packed))

            # Apply same parsed analysis to hash siblings (independent verify/save)
            if ch and primary_row is not None:
                for sib in siblings.get(ch, []):
                    if sib == nb or sib in analyzed_nb_set:
                        continue
                    sev = assemble_local_evidence_package(
                        sib,
                        db=db,
                        title=(inv_meta.get(sib) or {}).get("title"),
                        include_http_cache=False,
                        include_package_db=True,
                    )
                    sai = {
                        "ok": True,
                        "cached": True,
                        "live_ai_invoked": False,
                        "parsed": ai.get("parsed"),
                        "openai_spent_usd": 0.0,
                        "corpus_hash": ch,
                        "model": model,
                    }
                    sver = verify_analysis_against_sources(sai["parsed"], sev, item=dict(inv_meta.get(sib) or {}))
                    spacked = _row_from_verification(sib, sev, sai, sver)
                    _persist_result(sib, sev, sai, sver, spacked)
                    analyzed_rows.append(_summary_row(spacked, sai, reused=True))
                    _accumulate_metrics(metrics, spacked, sver)
                    metrics["packages_reused_from_cache"] += 1
                    analyzed_nb_set.add(sib)
                    if write_owner_queues_flag:
                        _enqueue(queues, spacked, analyzed_nb_set)

            snap = budget.snapshot()
            metrics["openai_spent_usd"] = snap["spent"]
            if metrics["packages_analyzed_by_openai"] and metrics["packages_analyzed_by_openai"] % 5 == 0:
                _write_checkpoint(
                    metrics,
                    Decimal(str(snap["spent"])),
                    Decimal(str(snap["remaining"])),
                    len(analyzed_rows),
                    budget.stop_reason,
                )
                # Continuous owner report refresh
                (_temp_root() / "PROGRESS.json").write_text(
                    json.dumps(
                        {
                            "at": _utc(),
                            "analyzed": len(analyzed_rows),
                            "live": metrics["packages_analyzed_by_openai"],
                            "spent": snap["spent"],
                            "remaining": snap["remaining"],
                            "workers": workers,
                            "supplier_ready": len(supplier_ready),
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                print(
                    json.dumps(
                        {
                            "phase": "progress",
                            "analyzed": len(analyzed_rows),
                            "live": metrics["packages_analyzed_by_openai"],
                            "spent": snap["spent"],
                            "workers": workers,
                        }
                    ),
                    flush=True,
                )

    # Parallel dispatcher: keep up to `workers` in flight; refill as each finishes
    pending_idx = 0
    in_flight: dict[Any, dict[str, Any]] = {}

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="m3-ai") as pool:
        while pending_idx < len(work_units) or in_flight:
            # Fill slots
            while pending_idx < len(work_units) and len(in_flight) < workers and not budget.stop:
                row = work_units[pending_idx]
                pending_idx += 1
                nb = row["nb_id"]
                # Resume: already persisted with live analysis for this hash/id
                prior = _load_persisted_result(nb)
                if prior and prior.get("parsed") and prior.get("live_ai_invoked"):
                    # Treat as cache hit without OpenAI
                    evidence = assemble_local_evidence_package(
                        nb,
                        db=db,
                        title=(inv_meta.get(nb) or {}).get("title"),
                        include_http_cache=False,
                        include_package_db=True,
                    )
                    ai = {
                        "ok": True,
                        "cached": True,
                        "live_ai_invoked": False,
                        "parsed": prior.get("parsed"),
                        "openai_spent_usd": 0.0,
                        "corpus_hash": prior.get("corpus_hash") or row["corpus_hash"],
                        "model": model,
                    }
                    verified = verify_analysis_against_sources(
                        ai["parsed"], evidence, item=dict(inv_meta.get(nb) or {})
                    )
                    packed = _row_from_verification(nb, evidence, ai, verified)
                    _apply_result(
                        {
                            "ok": True,
                            "nb_id": nb,
                            "cached": True,
                            "resumed": True,
                            "ai": ai,
                            "verified": verified,
                            "packed": packed,
                            "corpus_hash": ai["corpus_hash"],
                        },
                        primary_row=row,
                    )
                    continue

                est = Decimal(str(row.get("estimated_usd") or 0.003))
                # Conservative reserve for one-shot (covers typical + headroom)
                reserve = max(est, Decimal("0.01"))
                if not budget.try_reserve(reserve):
                    budget.stop = True
                    budget.stop_reason = budget.stop_reason or "openai_job_ceiling_reached"
                    break
                fut = pool.submit(
                    _worker_analyze_one,
                    nb_id=nb,
                    title=(inv_meta.get(nb) or {}).get("title") or row.get("title"),
                    inv_row=dict(inv_meta.get(nb) or {}),
                    model=model,
                    budget=budget,
                    reserve_usd=reserve,
                )
                in_flight[fut] = {"row": row, "reserve": reserve, "submitted_at": time.time()}

            if not in_flight:
                break

            done, _ = wait(list(in_flight.keys()), return_when=FIRST_COMPLETED, timeout=120)
            if not done:
                # timeout wait — check hung
                continue
            for fut in done:
                meta = in_flight.pop(fut)
                try:
                    result = fut.result()
                except Exception as exc:
                    budget.release(meta["reserve"])
                    result = {
                        "ok": False,
                        "nb_id": meta["row"]["nb_id"],
                        "error": f"{type(exc).__name__}: {exc}"[:400],
                        "rate_limited": False,
                    }
                _apply_result(result, primary_row=meta["row"])

            if budget.stop and not in_flight:
                break

        # Drain any remaining in-flight after stop (they already reserved)
        while in_flight:
            done, _ = wait(list(in_flight.keys()), return_when=FIRST_COMPLETED)
            for fut in done:
                meta = in_flight.pop(fut)
                try:
                    result = fut.result()
                except Exception as exc:
                    budget.release(meta["reserve"])
                    result = {
                        "ok": False,
                        "nb_id": meta["row"]["nb_id"],
                        "error": f"{type(exc).__name__}: {exc}"[:400],
                    }
                _apply_result(result, primary_row=meta["row"])

    snap = budget.snapshot()
    analyzed_nbs = {r["nb_id"] for r in analyzed_rows}
    still = [r for r in eligible if r["nb_id"] not in analyzed_nbs]
    metrics["unanalyzed_due_to_cost_cap"] = len(still)
    metrics["openai_spent_usd"] = snap["spent"]
    metrics["concurrency_final"] = workers

    queue_summary = {}
    if write_owner_queues_flag:
        queue_summary = write_queues(queues, build="bulk-ai-doc-understanding")

    report = {
        "ok": True,
        "finished_at": _utc(),
        "cleanup_commit": None,
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "owner_max_usd": float(max_budget),
        "openai_spent_usd": snap["spent"],
        "cost_basis": "application_estimate_from_reported_tokens_gpt4o_mini_rates",
        "concurrency": {"start": metrics["concurrency_start"], "final": workers},
        "nextbid_api_calls": 0,
        "nextbid_killswitch_preserved": True,
        "no_deployment": True,
        "no_recurring_ai_job": True,
        "stopped_reason": budget.stop_reason,
        "demote_expired": demote,
        "inventory": {
            "inventory_total_records": inv_report["inventory_total_records"],
            "counts": inv_report["counts"],
            "unique_corpus_hashes": inv_report["unique_corpus_hashes"],
            "estimated_unique_openai_usd": inv_report["estimated_unique_openai_usd"],
        },
        "metrics": metrics,
        "supplier_quote_ready": supplier_ready,
        "needs_information": needs_info[:50],
        "failures": failures[:50],
        "analyzed_count": len(analyzed_rows),
        "queue_summary": queue_summary,
        "results_dir": str(_results_root()),
    }

    temp = _temp_root()
    (temp / "OWNER_REPORT.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    (temp / "analyzed_summary.json").write_text(
        json.dumps(analyzed_rows, indent=2, default=str), encoding="utf-8"
    )
    durable = get_settings().data_root() / "acceptance" / "jobs_bulk_ai_doc_analysis_last.json"
    durable.parent.mkdir(parents=True, exist_ok=True)
    durable.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    (_results_root() / "inventory_snapshot.json").write_text(
        json.dumps(
            {
                "inventoried_at": inv_report["inventoried_at"],
                "inventory_total_records": inv_report["inventory_total_records"],
                "counts": inv_report["counts"],
                "unique_corpus_hashes": inv_report["unique_corpus_hashes"],
                "ai_eligible_nb_ids": [r["nb_id"] for r in eligible],
                "unanalyzed_nb_ids": [r["nb_id"] for r in still],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return report


def _summary_row(packed: dict[str, Any], ai: dict[str, Any], *, reused: bool) -> dict[str, Any]:
    item = packed["item"]
    promo = packed.get("verified", {}).get("promotion") or {}
    return {
        "nb_id": item.get("nb_id"),
        "title": item.get("title"),
        "disposition": packed.get("disposition"),
        "requirement_class": (packed.get("parsed") or {}).get("requirement_class"),
        "line_items": len(packed.get("verified", {}).get("verified_line_items") or []),
        "supplier_quote_ready": promo.get("supplier_quote_ready"),
        "bid_submission_ready": promo.get("bid_submission_ready"),
        "blocks_product_only": packed.get("verified", {}).get("blocks_product_only"),
        "deadline_expired": (packed.get("verified", {}).get("deadline_gate") or {}).get("expired"),
        "openai_spent_usd": ai.get("openai_spent_usd"),
        "cached_or_reused": reused or ai.get("cached"),
        "live_ai_invoked": ai.get("live_ai_invoked"),
    }


def _owner_table_row(packed: dict[str, Any]) -> dict[str, Any]:
    item = packed["item"]
    lines = packed.get("verified", {}).get("verified_line_items") or []
    products = "; ".join(str(li.get("description") or "")[:80] for li in lines[:8])
    qty = "; ".join(
        f"{li.get('quantity') or 'UNKNOWN'} {li.get('unit') or ''}".strip() for li in lines[:8]
    )
    specs = "; ".join(
        ", ".join(li.get("specs") or [])[:60] for li in lines[:3] if li.get("specs")
    )
    promo = packed.get("verified", {}).get("promotion") or {}
    return {
        "solicitation": item.get("solicitation_number") or item.get("nb_id"),
        "nb_id": item.get("nb_id"),
        "agency": item.get("agency") or item.get("organization") or "",
        "products": products,
        "qty_uom": qty,
        "specs": specs,
        "deadline": item.get("deadline_at") or item.get("response_deadline"),
        "eligibility": "verified" if promo.get("bid_submission_ready") else (
            "blocked" if packed.get("verified", {}).get("blocks_product_only") else "unverified"
        ),
        "next_action": (
            "supplier_pricing"
            if promo.get("supplier_quote_ready")
            else packed.get("disposition")
        ),
        "disposition": packed.get("disposition"),
    }


def _accumulate_metrics(metrics: dict[str, Any], packed: dict[str, Any], verified: dict[str, Any]) -> None:
    parsed = packed.get("parsed") or {}
    req = str(parsed.get("requirement_class") or "")
    if parsed.get("is_product_purchase") and not verified.get("blocks_product_only"):
        metrics["confirmed_physical_product"] += 1
    if req in {
        "services",
        "construction",
        "maintenance_repair_services",
        "professional_services",
        "mixed_product_service",
        "product_with_significant_installation",
    } or verified.get("blocks_product_only"):
        metrics["service_construction_mixed"] += 1
    if any(
        (li.get("quantity_semantic") or "") in {"ESTIMATED", "EVALUATION", "ORDERED"}
        for li in (verified.get("verified_line_items") or [])
    ):
        metrics["qty_uom_corrections"] += 1
    recon = str(parsed.get("duplicate_line_reconciliation") or "")
    if recon:
        metrics["duplicate_line_corrections"] += 1
    if verified.get("blocks_product_only"):
        metrics["previously_missed_service_obligations"] += 1
    if len(verified.get("verified_line_items") or []) >= 1 and parsed.get("is_product_purchase"):
        metrics["previously_missed_products"] += 1
    promo = verified.get("promotion") or {}
    if promo.get("supplier_quote_ready"):
        metrics["supplier_pricing_ready"] += 1
        metrics["complete_purchasing_requirements"] += 1
    if (verified.get("deadline_gate") or {}).get("expired"):
        metrics["blocked_expired_deadline"] += 1
    missing = parsed.get("missing_information") or []
    if any(re.search(r"(?i)attach|amend|addend|schedule|spec", str(m)) for m in missing):
        metrics["needs_missing_attachments"] += 1


def _enqueue(queues: dict[str, list], packed: dict[str, Any], analyzed_nbs: set[str]) -> None:
    disp = packed.get("disposition") or "potential_product_classification_needed"
    item = packed["item"]
    nb = str(item.get("nb_id") or "")
    card = {
        "nb_id": nb,
        "title": item.get("title"),
        "solicitation_number": item.get("solicitation_number"),
        "deadline": item.get("deadline_at") or item.get("response_deadline"),
        "deadline_at": item.get("deadline_at") or item.get("response_deadline"),
        "disposition": disp,
        "queue": disp,
        "requirement_class": (packed.get("parsed") or {}).get("requirement_class"),
        "verified_line_items": packed.get("verified", {}).get("verified_line_items") or [],
        "promotion": packed.get("verified", {}).get("promotion"),
        "ai_document_understanding": item.get("ai_document_understanding"),
        "source_refs": item.get("source_refs") or [],
        "official_url": item.get("official_url"),
    }
    # Remove this NB from all queues before re-homing (avoid duplicates / stale disposition)
    for qname, rows in list(queues.items()):
        queues[qname] = [r for r in rows if str(r.get("nb_id") or "") != nb]
    if disp in queues:
        queues[disp].append(card)
    promo = packed.get("verified", {}).get("promotion") or {}
    if promo.get("supplier_quote_ready") and not promo.get("bid_submission_ready"):
        queues.setdefault("awaiting_supplier_quotations", []).append(dict(card))


def _persist_result(
    nb_id: str,
    evidence: dict[str, Any],
    ai: dict[str, Any],
    verified: dict[str, Any],
    packed: dict[str, Any],
) -> None:
    path = _results_root() / "by_nb_id" / f"{nb_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "nb_id": nb_id,
        "saved_at": _utc(),
        "title": evidence.get("title"),
        "corpus_hash": ai.get("corpus_hash"),
        "model": ai.get("model"),
        "openai_spent_usd": ai.get("openai_spent_usd"),
        "live_ai_invoked": ai.get("live_ai_invoked"),
        "cached": ai.get("cached"),
        "parsed": ai.get("parsed"),
        "verification": {
            "verified": verified.get("verified"),
            "rejected_assertions": verified.get("rejected_assertions"),
            "unknown_fields": verified.get("unknown_fields"),
            "blocks_product_only": verified.get("blocks_product_only"),
            "promotion": verified.get("promotion"),
            "deadline_gate": verified.get("deadline_gate"),
            "verified_line_items": verified.get("verified_line_items"),
        },
        "disposition": packed.get("disposition"),
        "source_refs": evidence.get("source_refs"),
        "coverage": evidence.get("coverage"),
    }
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _write_checkpoint(
    metrics: dict[str, Any],
    spent: Decimal,
    remaining: Decimal,
    analyzed: int,
    stopped_reason: str | None,
) -> None:
    path = _temp_root() / "CHECKPOINT.json"
    path.write_text(
        json.dumps(
            {
                "at": _utc(),
                "spent": float(spent),
                "remaining": float(remaining),
                "analyzed": analyzed,
                "metrics": metrics,
                "stopped_reason": stopped_reason,
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
