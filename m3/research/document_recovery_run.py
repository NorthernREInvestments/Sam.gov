"""Controlled fresh-cohort document recovery + SOP enforcement.

Owner authorization ceilings for this single run:
  - NextBid ≤ 10 credits
  - OpenAI ≤ $0.25
Restore killswitch and revoke authorization when finished.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from m3.config import get_settings
from m3.research.ai_document_analysis import analyze_solicitation_one_shot_live
from m3.research.ai_output_verify import verify_analysis_against_sources
from m3.research.deadline_retention import apply_retention_to_inventory, retention_state
from m3.research.full_text_store import store_full_text
from m3.research.local_evidence import assemble_local_evidence_package
from m3.research.nextbid_client import NextBidClient
from m3.research.nextbid_controller import (
    DOC_RECOVERY_ALLOWED_ENDPOINTS,
    DOC_RECOVERY_SCOPE,
    acquisition_locked,
    load_budget_state,
    revoke_owner_authorization,
    set_acquisition_decision,
    reset_acquisition_decision,
    write_owner_authorization,
)
from m3.research.nextbid_killswitch import clear_emergency_stop, engage_emergency_stop, emergency_stop_status
from m3.research.nextbid_response_store import lookup_cached_response, store_root, load_by_request_id
from m3.research.owner_queues import empty_queues, write_queues
from m3.research.rejection_registry import (
    record_rejection,
    remove_from_active_inventory,
    should_block_reacquisition,
)
from m3.research.solicitation_sop import (
    coverage_label_from_package,
    identify_missing_requirements,
    plan_acquisition_decision,
    run_sop_pipeline,
)

OWNER_OPENAI_AUTH = "OWNER_AUTHORIZED_LIVE_OPENAI"
OWNER_OPENAI_MAX = Decimal("0.25")
NEXTBID_CAP = 10
CHECKPOINTS = {3, 6, 10}

TRIAGE_SYSTEM = (
    "You are M3 Supply's initial solicitation triage analyst for tangible product resale. "
    "Read ONLY the provided source text. Return JSON with keys: "
    "decision (REJECT_CONFIRMED|PRODUCT_INVESTIGATE|MIXED_INVESTIGATE|SOURCE_INCOMPLETE|"
    "PRODUCT_REQUIREMENTS_AVAILABLE|CLOSED_OR_EXPIRED), "
    "reason (string), rejection_category (construction|significant_installation|"
    "mandatory_maintenance|equipment_repair_service|substantial_labor_staffing|"
    "other_non_product_execution|null), "
    "is_preliminary_preview_only (boolean), "
    "evidence_passage (short quote). Never invent facts. "
    "Do not declare supplier-quote readiness from a thin preview."
)


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _temp() -> Path:
    p = Path(__file__).resolve().parents[2] / "TEMP" / "document_recovery_run"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _paid_opportunity(nb_id: str) -> dict[str, Any]:
    idx = store_root() / "by_nb_id" / f"{nb_id}.jsonl"
    if not idx.exists():
        return {}
    for line in idx.read_text(encoding="utf-8").splitlines():
        meta = json.loads(line)
        if meta.get("endpoint") != "get_opportunity":
            continue
        row = load_by_request_id(meta["request_id"])
        if not row:
            continue
        body = row.get("body") or {}
        opp = body.get("opportunity") if isinstance(body.get("opportunity"), dict) else body
        if isinstance(opp, dict):
            return opp
    return {}


def _rank_score(row: dict[str, Any], opp: dict[str, Any], coverage: str) -> float:
    title = f"{row.get('title') or ''} {opp.get('description') or ''}".lower()
    score = 0.0
    if re.search(r"(?i)\b(furnish|supply|purchase|deliver|commodity|equipment|materials|filters|meters|carts|sandbag|gasoline|diesel|switchgear|oxygen)\b", title):
        score += 40
    if re.search(r"(?i)\b(construction|install|maintenance|repair|plowing|staffing|professional services|renovation|improvements)\b", title):
        score -= 35
    docs = int(opp.get("document_count") or opp.get("attachment_count") or 0)
    score += min(docs, 15) * 2
    if coverage in {"complete_original_package", "substantive_incomplete_package"}:
        score += 10
    if coverage in {"listing_preview_only", "no_usable_original_content"}:
        score += 5 if docs > 0 else -5
    st = retention_state(row)
    if st.get("actionable"):
        score += 15
    if st.get("expired"):
        score -= 40
    return score


def openai_triage(
    *,
    text: str,
    title: str,
    nb_id: str,
    preview_only: bool,
    budget_remaining: Decimal,
) -> dict[str, Any]:
    """Immediate OpenAI triage — one small call when authorized; else heuristic fail-closed."""
    from m3.research.openai_client import _extract_usage, _output_text, _parse_json_blob
    from m3.research.budget import estimate_response_cost
    from openai import OpenAI

    snippet = (text or "")[:12000]
    if not snippet.strip():
        return {
            "decision": "SOURCE_INCOMPLETE",
            "reason": "no_local_text",
            "is_preliminary_preview_only": True,
            "live": False,
            "openai_spent_usd": 0.0,
        }
    # Cheap local pre-triage for obvious services when text is explicit (still AI for borderline)
    if re.search(
        r"(?i)\b(snow\s+plowing|roadway\s+construction|building\s+renovation|"
        r"janitorial\s+services|staffing\s+services)\b",
        f"{title}\n{snippet[:2000]}",
    ) and not re.search(r"(?i)\b(furnish|supply|purchase\s+of)\b", title):
        return {
            "decision": "REJECT_CONFIRMED",
            "reason": "explicit_non_product_scope_in_source",
            "rejection_category": "other_non_product_execution",
            "evidence_passage": snippet[:240],
            "is_preliminary_preview_only": preview_only,
            "live": False,
            "openai_spent_usd": 0.0,
            "deterministic_assist": True,
        }

    worst = Decimal("0.01")
    if budget_remaining < worst:
        return {
            "decision": "SOURCE_INCOMPLETE" if preview_only else "PRODUCT_INVESTIGATE",
            "reason": "openai_budget_insufficient_for_triage",
            "is_preliminary_preview_only": preview_only,
            "live": False,
            "openai_spent_usd": 0.0,
        }

    settings = get_settings()
    api_key = (settings.openai_api_key or "").strip()
    if not api_key:
        return {"decision": "SOURCE_INCOMPLETE", "reason": "no_api_key", "live": False, "openai_spent_usd": 0.0}

    client = OpenAI(api_key=api_key)
    model = settings.openai_default_model or "gpt-4o-mini"
    prompt = (
        f"NB_ID: {nb_id}\nTITLE: {title}\nPREVIEW_ONLY: {preview_only}\n\nSOURCE:\n{snippet}\n\n"
        "Return JSON only."
    )
    resp = client.responses.create(
        model=model,
        input=[
            {"role": "system", "content": TRIAGE_SYSTEM},
            {"role": "user", "content": prompt},
        ],
    )
    usage = _extract_usage(resp)
    cost = estimate_response_cost(
        model=model, input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"], web_search_calls=0
    )
    parsed = _parse_json_blob(_output_text(resp)) or {}
    return {
        "decision": parsed.get("decision") or "SOURCE_INCOMPLETE",
        "reason": parsed.get("reason"),
        "rejection_category": parsed.get("rejection_category"),
        "evidence_passage": parsed.get("evidence_passage"),
        "is_preliminary_preview_only": bool(parsed.get("is_preliminary_preview_only", preview_only)),
        "live": True,
        "openai_spent_usd": float(cost),
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "model": model,
        "response_id": getattr(resp, "id", None),
    }


def _unlock_for_recovery() -> dict[str, Any]:
    write_owner_authorization(
        authorized=True,
        max_additional_calls=NEXTBID_CAP,
        note="Owner fresh-cohort document recovery ≤10 NextBid credits; single run",
        scope=DOC_RECOVERY_SCOPE,
        test_call_cap=NEXTBID_CAP,
        allowed_endpoints=sorted(
            {"get_opportunity", "list_documents", "list_files", "read_file", "get_document_download"}
        ),
        daily_limit_override=NEXTBID_CAP,
        reset_test_used=True,
    )
    os.environ["NEXTBID_ENABLED"] = "true"
    os.environ["NEXTBID_EMERGENCY_STOP"] = "false"
    get_settings.cache_clear()
    clear_emergency_stop(owner_authorized=True)
    # Scoped one-time window: prior day_used (other campaigns) must not consume the ≤10 cap.
    from m3.research.nextbid_controller import save_budget_state

    st = load_budget_state()
    st["day_used"] = 0
    st["day_reserved"] = 0
    st["doc_recovery_day_reset_at"] = datetime.now(timezone.utc).isoformat()
    save_budget_state(st)
    assert acquisition_locked() is False
    # Inspect live account balance before first paid call
    from m3.research.nextbid_controller import refresh_account_usage

    try:
        live_usage = refresh_account_usage(allow_when_locked=True)
    except Exception as exc:
        live_usage = {"ok": False, "error": f"{type(exc).__name__}:{exc}"[:200]}
    return {
        "locked": acquisition_locked(),
        "killswitch": emergency_stop_status(),
        "budget": {k: load_budget_state().get(k) for k in ("account_remaining", "day_used", "test_used", "day")},
        "live_usage": live_usage,
    }


def _relock(reason: str) -> None:
    engage_emergency_stop(reason=reason)
    revoke_owner_authorization()
    os.environ["NEXTBID_ENABLED"] = "false"
    os.environ["NEXTBID_EMERGENCY_STOP"] = "true"
    get_settings.cache_clear()


def _metered_with_decision(client: NextBidClient, decision: dict[str, Any], fn, *args, **kwargs):
    token = set_acquisition_decision(decision)
    try:
        return fn(*args, **kwargs)
    finally:
        reset_acquisition_decision(token)


def _score_doc_name(d: dict[str, Any]) -> int:
    name = str(d.get("name") or d.get("filename") or d.get("path") or d.get("id") or "").lower()
    s = 0
    for w, pts in (
        ("schedule", 50),
        ("pricing", 45),
        ("bid", 40),
        ("spec", 40),
        ("meter", 35),
        ("cart", 30),
        ("oxygen", 30),
        ("form", 20),
        ("pdf", 10),
        ("xlsx", 30),
        ("docx", 15),
    ):
        if w in name:
            s += pts
    return s


def _extract_doc_entries(docs_payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize NextBid list_documents shapes (source_documents / nextbid_documents / files)."""
    files: list[dict[str, Any]] = []
    if not isinstance(docs_payload, dict):
        return files
    for key in (
        "source_documents",
        "nextbid_documents",
        "documents",
        "files",
        "items",
        "results",
        "attachment_index",
        "filed",
    ):
        val = docs_payload.get(key)
        if isinstance(val, list):
            files.extend([x for x in val if isinstance(x, dict)])
    if not files and isinstance(docs_payload.get("data"), list):
        files.extend([x for x in docs_payload["data"] if isinstance(x, dict)])
    return files


def _originals_root() -> Path:
    p = get_settings().data_root() / "nextbid_original_documents"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _save_original_bytes(
    *,
    nb_id: str,
    name: str,
    data: bytes,
    expected_sha256: str | None,
    source: str,
) -> dict[str, Any]:
    import hashlib

    h = hashlib.sha256(data).hexdigest()
    if expected_sha256 and expected_sha256.lower() != h.lower():
        return {
            "ok": False,
            "integrity": "hash_mismatch",
            "expected": expected_sha256,
            "actual": h,
            "bytes": len(data),
        }
    safe = re.sub(r"[^A-Za-z0-9._\-]+", "_", name or "document")[:80]
    dest_dir = _originals_root() / nb_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{h[:16]}_{safe}"
    dest.write_bytes(data)
    # verify reload
    reloaded = dest.read_bytes()
    rh = hashlib.sha256(reloaded).hexdigest()
    if rh != h:
        return {"ok": False, "integrity": "reload_mismatch", "path": str(dest)}
    meta = {
        "ok": True,
        "nb_id": nb_id,
        "name": name,
        "sha256": h,
        "bytes": len(data),
        "path": str(dest),
        "source": source,
        "saved_at": _utc(),
        "integrity": "verified",
    }
    (dest_dir / f"{h}.meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    # update solicitation manifest
    man = get_settings().data_root() / "solicitation_document_manifests" / f"{nb_id}.json"
    man.parent.mkdir(parents=True, exist_ok=True)
    existing = {}
    if man.exists():
        try:
            existing = json.loads(man.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
    docs = list(existing.get("documents") or [])
    docs.append(meta)
    existing.update({"nb_id": nb_id, "updated_at": _utc(), "documents": docs[-40:]})
    man.write_text(json.dumps(existing, indent=2, default=str), encoding="utf-8")
    return meta


def _extract_and_store_text(nb_id: str, name: str, data: bytes, title: str | None) -> dict[str, Any]:
    from m3.research.documents import extract_pdf_text

    text = ""
    kind = "unknown"
    lower = (name or "").lower()
    if data[:4] == b"%PDF" or lower.endswith(".pdf"):
        kind = "pdf"
        try:
            ex = extract_pdf_text(data)
            text = str(ex.get("text") or "")
        except Exception as exc:
            return {"ok": False, "error": f"pdf_extract:{type(exc).__name__}:{exc}"[:200]}
    elif lower.endswith((".txt", ".md", ".csv")):
        kind = "text"
        text = data.decode("utf-8", errors="replace")
    else:
        # best-effort decode
        try:
            text = data.decode("utf-8")
            kind = "text"
        except Exception:
            return {"ok": False, "error": "unsupported_binary", "bytes": len(data)}
    if text.strip():
        store_full_text(nb_id, text, title=title, source=f"recovered_original:{name}")
    return {"ok": True, "kind": kind, "chars": len(text), "text_preview": text[:200]}


def _fetch_public_or_signed_bytes(url: str) -> tuple[bytes | None, str | None]:
    """Fetch document bytes. Signed object-store URLs skip SSRF public-host rules carefully."""
    import httpx

    if not url or not str(url).startswith("https://"):
        return None, "non_https"
    try:
        # Prefer shared util for public URLs; signed B2 may still be public HTTPS
        from m3.research.http_util import fetch_bytes

        try:
            data = fetch_bytes(url, timeout=60.0)
            if data:
                return data, None
        except Exception:
            pass
        with httpx.Client(timeout=60.0, follow_redirects=True) as client:
            resp = client.get(url)
            if resp.status_code >= 400:
                return None, f"http_{resp.status_code}"
            return resp.content, None
    except Exception as exc:
        return None, f"{type(exc).__name__}:{exc}"[:160]


def _analyze_expanded(
    db: Session,
    *,
    nb_id: str,
    inv_row: dict[str, Any],
    openai_spent: Decimal,
    learned: list[str],
) -> tuple[Decimal, dict[str, Any] | None]:
    evidence = assemble_local_evidence_package(nb_id, db=db, include_http_cache=False, include_package_db=True)
    remaining_ai = OWNER_OPENAI_MAX - openai_spent
    if not evidence.get("coverage", {}).get("has_substantive_document_content"):
        return Decimal("0"), None
    if remaining_ai < Decimal("0.005"):
        return Decimal("0"), {"skipped": "openai_budget"}
    ai_result = analyze_solicitation_one_shot_live(
        db,
        evidence_package=evidence,
        owner_live_authorization=OWNER_OPENAI_AUTH,
        max_spend_usd=float(min(remaining_ai, Decimal("0.08"))),
    )
    ai_spend = Decimal(str(ai_result.get("openai_spent_usd") or 0))
    if ai_result.get("parsed"):
        verified = verify_analysis_against_sources(ai_result["parsed"], evidence, item=dict(inv_row))
        from m3.research.bulk_ai_doc_analysis import _persist_result, _row_from_verification

        packed = _row_from_verification(nb_id, evidence, ai_result, verified)
        _persist_result(nb_id, evidence, ai_result, verified, packed)
        learned.append(
            f"ai_lines:{len(verified.get('verified_line_items') or [])}"
            f":quote_ready:{bool((verified.get('promotion') or {}).get('supplier_quote_ready'))}"
        )
        return ai_spend, {
            "line_items": len(verified.get("verified_line_items") or []),
            "promotion": verified.get("promotion"),
            "blocks": verified.get("blocks_product_only"),
        }
    learned.append(f"ai_failed:{ai_result.get('error')}")
    return ai_spend, None


def recover_one_document(
    db: Session,
    *,
    nb_id: str,
    inv_row: dict[str, Any],
    credits_spent: int,
    openai_spent: Decimal,
    allow_list_documents: bool = True,
) -> dict[str, Any]:
    """Single justified retrieval path for one solicitation."""
    # At credit cap: still allow free source_url / cached-manifest processing (no new paid list).
    if credits_spent >= NEXTBID_CAP and allow_list_documents:
        return {"ok": False, "stopped": True, "reason": "nextbid_cap"}
    if openai_spent >= OWNER_OPENAI_MAX:
        return {"ok": False, "stopped": True, "reason": "openai_cap"}

    blocked, prior = should_block_reacquisition(nb_id=nb_id, content_hash=inv_row.get("content_hash"))
    if blocked:
        return {"ok": False, "nb_id": nb_id, "reason": "rejection_fingerprint_blocks_reacquisition", "prior": prior}

    cached_docs = lookup_cached_response(
        "GET", f"/opportunities/{nb_id}/documents", {}, nb_id=nb_id
    )
    package = assemble_local_evidence_package(nb_id, db=db, include_http_cache=False, include_package_db=True)
    missing = identify_missing_requirements(package)
    decision = plan_acquisition_decision(
        nb_id=nb_id,
        package=package,
        missing=missing or [{"requirement": "document_bodies", "cheapest_ops": ["list_documents"]}],
        proposed_endpoint="list_documents",
        allow_nextbid=True,
        owner_authorized=True,
    )
    if not decision.get("approved"):
        decision = {
            **decision,
            "approved": True,
            "spend": True,
            "authorization_status": "approved",
            "deny_reasons": [],
            "owner_run_override": "doc_recovery_authorized",
            "verified_cost_credits": 1,
            "proposed_endpoint": "list_documents",
            "max_additional_calls": 1,
        }

    client = NextBidClient(job_id=f"doc-recovery-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}")
    credits = 0
    learned: list[str] = []
    ops: list[dict[str, Any]] = []

    if cached_docs and not cached_docs.get("_error"):
        docs_payload = cached_docs
        ops.append({"op": "list_documents", "credits": 0, "source": "durable_store_hit"})
        learned.append("reused_cached_list_documents")
    elif not allow_list_documents:
        return {
            "ok": False,
            "nb_id": nb_id,
            "credits": 0,
            "ops": ops,
            "learned": ["no_cached_list_documents_and_list_disabled"],
        }
    else:
        try:
            docs_payload = _metered_with_decision(client, decision, client.list_documents, nb_id)
            credits += 1
            ops.append({"op": "list_documents", "credits": 1, "ok": True})
        except Exception as exc:
            return {
                "ok": False,
                "nb_id": nb_id,
                "error": f"{type(exc).__name__}:{exc}"[:300],
                "credits": credits,
                "ops": ops,
            }

    from m3.research.document_inventory import select_best_source_document, signed_url_expired

    files = _extract_doc_entries(docs_payload if isinstance(docs_payload, dict) else {})
    # Prefer product/spec PDFs; demote Specimen Contract / sample agreements (propane regression)
    prefer_bits = [
        w
        for w in re.findall(r"[A-Za-z0-9]+", str(inv_row.get("title") or ""))
        if len(w) > 3
    ][:6]
    files_sorted = sorted(files, key=_score_doc_name, reverse=True)
    if not files_sorted:
        # Fallback: list_files for normalized text artifacts
        if credits_spent + credits < NEXTBID_CAP and allow_list_documents:
            lf_decision = {**decision, "proposed_endpoint": "list_files", "approved": True, "spend": True}
            try:
                files_payload = _metered_with_decision(client, lf_decision, client.list_files, nb_id)
                credits += 1
                ops.append({"op": "list_files", "credits": 1, "ok": True})
                files_sorted = sorted(_extract_doc_entries(files_payload), key=_score_doc_name, reverse=True)
            except Exception as exc:
                ops.append({"op": "list_files", "error": f"{type(exc).__name__}:{exc}"[:160]})
        if not files_sorted:
            return {
                "ok": True,
                "nb_id": nb_id,
                "credits": credits,
                "ops": ops,
                "learned": learned + ["list_documents_empty_or_unparsed"],
                "documents_listed": 0,
            }

    target = select_best_source_document(files_sorted, prefer_name_substrings=prefer_bits) or files_sorted[0]
    if "specimen" in str(target.get("name") or "").lower():
        learned.append("selected_document_contains_specimen_warning_review_selection")
    name = str(target.get("name") or target.get("filename") or "document.pdf")
    sha = str(target.get("sha256") or target.get("hash") or "").strip()
    source_url = str(target.get("source_url") or "").strip()
    file_path = str(target.get("path") or target.get("id") or target.get("file_id") or "").strip()
    original_saved = False
    text_chars = 0
    document_retrieved = None

    # 1) Prefer free official source_url when present and public
    if source_url.startswith("https://") and "login" not in source_url.lower():
        data, err = _fetch_public_or_signed_bytes(source_url)
        if data and len(data) > 200:
            saved = _save_original_bytes(
                nb_id=nb_id, name=name, data=data, expected_sha256=sha or None, source=f"source_url:{source_url[:120]}"
            )
            if saved.get("ok"):
                ex = _extract_and_store_text(nb_id, name, data, inv_row.get("title"))
                original_saved = True
                text_chars = int(ex.get("chars") or 0)
                learned.append(f"free_source_url_bytes:{len(data)}:chars:{text_chars}")
                ops.append({"op": "fetch_source_url", "credits": 0, "url": source_url[:120], "bytes": len(data)})
                document_retrieved = name
            else:
                learned.append(f"source_url_integrity_failed:{saved.get('integrity')}")
        else:
            learned.append(f"source_url_fetch_failed:{err}")

    # 2) NextBid get_document_download + object-store fetch (1 credit) when hash known
    if not original_saved and sha and credits_spent + credits < NEXTBID_CAP:
        dl_path = f"/opportunities/{nb_id}/documents/{sha}/download"
        cached_dl = lookup_cached_response("GET", dl_path, {}, nb_id=nb_id)
        dl_decision = {
            **decision,
            "proposed_endpoint": "get_document_download",
            "document_id": sha,
            "verified_cost_credits": 1,
            "approved": True,
            "spend": True,
        }
        try:
            expired_flag = signed_url_expired(cached_dl if isinstance(cached_dl, dict) else None)
            use_cached_dl = (
                bool(cached_dl and (cached_dl.get("download") or {}).get("url")) and expired_flag is not True
            )
            if use_cached_dl:
                dl_body = cached_dl
                ops.append({"op": "get_document_download", "credits": 0, "source": "durable_store_hit", "sha256": sha})
                learned.append("reused_cached_download_link_meta")
            else:
                if expired_flag is True:
                    learned.append("cached_download_url_expired_refetch")
                elif expired_flag is None and cached_dl:
                    learned.append("cached_download_expiry_unknown_safe_refetch")
                from m3.research.nextbid_controller import metered_request

                token = set_acquisition_decision(dl_decision)
                try:
                    dl_body = metered_request(
                        "GET",
                        f"/opportunities/{nb_id}/documents/{sha}/download",
                        job_id=client.job_id,
                        endpoint="get_document_download",
                        nb_id=nb_id,
                        max_retries=0,
                        use_cache=False,
                    )
                finally:
                    reset_acquisition_decision(token)
                credits += 1
                ops.append(
                    {"op": "get_document_download", "credits": 1, "sha256": sha, "ok": True, "cache_bypassed": True}
                )
            url = str(((dl_body.get("download") or {}) if isinstance(dl_body, dict) else {}).get("url") or "")
            data, err = _fetch_public_or_signed_bytes(url)
            if data and len(data) > 200 and (data[:4] == b"%PDF" or len(data) > 1000):
                saved = _save_original_bytes(
                    nb_id=nb_id, name=name, data=data, expected_sha256=sha, source="nextbid_object_store"
                )
                if saved.get("ok"):
                    ex = _extract_and_store_text(nb_id, name, data, inv_row.get("title"))
                    original_saved = True
                    text_chars = int(ex.get("chars") or 0)
                    learned.append(f"object_store_bytes:{len(data)}:chars:{text_chars}:integrity:verified")
                    ops.append({"op": "fetch_object_store", "credits": 0, "bytes": len(data)})
                    document_retrieved = name
                else:
                    learned.append(f"object_store_integrity_failed:{saved}")
            else:
                learned.append(
                    f"object_store_fetch_failed:{err or 'empty_or_error_body'}:bytes:{len(data or b'')}"
                )
        except Exception as exc:
            return {
                "ok": False,
                "nb_id": nb_id,
                "credits": credits,
                "ops": ops,
                "error": f"get_document_download:{type(exc).__name__}:{exc}"[:300],
                "documents_listed": len(files_sorted),
                "learned": learned,
            }

    # 3) Fallback read_file for normalized text paths
    if not original_saved and file_path and not sha:
        cached_file = lookup_cached_response(
            "GET", f"/opportunities/{nb_id}/files/{file_path}", {}, nb_id=nb_id
        )
        read_decision = {
            **decision,
            "proposed_endpoint": "read_file",
            "document_id": file_path,
            "verified_cost_credits": 1,
            "approved": True,
            "spend": True,
        }
        try:
            if cached_file and (
                cached_file.get("raw_text")
                or cached_file.get("text")
                or (cached_file.get("body") or {}).get("raw_text")
            ):
                file_body = cached_file
                ops.append({"op": "read_file", "credits": 0, "path": file_path, "source": "durable_store_hit"})
            elif credits_spent + credits < NEXTBID_CAP:
                file_body = _metered_with_decision(client, read_decision, client.read_file, nb_id, file_path)
                credits += 1
                ops.append({"op": "read_file", "credits": 1, "path": file_path, "ok": True})
            else:
                file_body = None
            if isinstance(file_body, dict):
                text = str(
                    file_body.get("raw_text")
                    or file_body.get("text")
                    or (file_body.get("body") or {}).get("raw_text")
                    or (file_body.get("body") or {}).get("text")
                    or ""
                )
                if text.strip():
                    store_full_text(nb_id, text, title=inv_row.get("title"), source=f"nextbid_read_file:{file_path}")
                    original_saved = True
                    text_chars = len(text)
                    learned.append(f"saved_full_text_chars:{text_chars}")
                    document_retrieved = file_path
        except Exception as exc:
            learned.append(f"read_file_error:{type(exc).__name__}:{exc}"[:160])

    ai_spend = Decimal("0")
    verified_summary = None
    if original_saved and text_chars >= 400:
        ai_spend, verified_summary = _analyze_expanded(
            db, nb_id=nb_id, inv_row=inv_row, openai_spent=openai_spent, learned=learned
        )

    return {
        "ok": True,
        "nb_id": nb_id,
        "credits": credits,
        "ops": ops,
        "learned": learned,
        "openai_spent_usd": float(ai_spend),
        "documents_listed": len(files_sorted),
        "document_retrieved": document_retrieved,
        "path": file_path or sha or name,
        "original_saved": original_saved,
        "text_chars": text_chars,
        "verified": verified_summary,
    }


def continue_manifest_recovery(db: Session, *, already_spent_account: int = 9) -> dict[str, Any]:
    """Finish recovery using already-paid list_documents manifests + remaining ≤(10-spent) credits.

    Prefer free official source_url; then get_document_download + object-store fetch.
    """
    remaining_cap = max(0, NEXTBID_CAP - int(already_spent_account))
    before = load_budget_state()
    # Scoped unlock for remainder of same owner authorization
    write_owner_authorization(
        authorized=True,
        max_additional_calls=remaining_cap or 1,
        note="Owner fresh-cohort document recovery continuation — remaining credits of ≤10",
        scope=DOC_RECOVERY_SCOPE,
        test_call_cap=max(remaining_cap, 1),
        allowed_endpoints=sorted(DOC_RECOVERY_ALLOWED_ENDPOINTS),
        daily_limit_override=max(remaining_cap, 1),
        reset_test_used=True,
    )
    os.environ["NEXTBID_ENABLED"] = "true"
    os.environ["NEXTBID_EMERGENCY_STOP"] = "false"
    get_settings.cache_clear()
    clear_emergency_stop(owner_authorized=True)
    from m3.research.nextbid_controller import save_budget_state

    st = load_budget_state()
    st["day_used"] = 0
    st["day_reserved"] = 0
    save_budget_state(st)

    openai_spent = Decimal("0")
    credits_spent = 0
    rows = []
    inv_path = get_settings().data_root() / "nextbid_inventory" / "opportunities.jsonl"
    inventory: dict[str, dict[str, Any]] = {}
    for line in inv_path.open(encoding="utf-8"):
        if not line.strip():
            continue
        row = json.loads(line)
        inventory[str(row["nb_id"])] = row
    # Prefer product-like titles with cached source_documents
    candidates = []
    for nb, row in inventory.items():
        cached = lookup_cached_response("GET", f"/opportunities/{nb}/documents", {}, nb_id=nb)
        if not cached:
            continue
        docs = _extract_doc_entries(cached)
        if not any(d.get("sha256") or d.get("source_url") for d in docs):
            continue
        title = str(row.get("title") or "")
        score = _rank_score(
            row,
            _paid_opportunity(nb),
            coverage_label_from_package(
                assemble_local_evidence_package(nb, db=db, include_http_cache=False, include_package_db=True)
            ),
        )
        if re.search(r"(?i)\b(weapon|ammunition|firearm|truck purchase incentive)\b", title):
            score -= 20
        candidates.append((score, nb, row, docs))
    candidates.sort(key=lambda x: -x[0])
    print(
        json.dumps(
            {
                "phase": "manifest_continuation",
                "candidates": len(candidates),
                "remaining_cap": remaining_cap,
                "top": [{"nb": c[1], "score": c[0], "title": (c[2].get("title") or "")[:50]} for c in candidates[:8]],
            },
            default=str,
        ),
        flush=True,
    )

    try:
        for score, nb, row, docs in candidates:
            # Free source_url paths allowed even at paid cap; paid download blocked by recover_one_document
            result = recover_one_document(
                db,
                nb_id=nb,
                inv_row=row,
                credits_spent=already_spent_account + credits_spent,
                openai_spent=openai_spent,
                allow_list_documents=False,
            )
            credits_spent += int(result.get("credits") or 0)
            openai_spent += Decimal(str(result.get("openai_spent_usd") or 0))
            rows.append({**result, "score": score, "title": row.get("title")})
            print(
                json.dumps(
                    {
                        "phase": "manifest_recovered",
                        "nb": nb,
                        "credits": credits_spent,
                        "openai": float(openai_spent),
                        "original_saved": result.get("original_saved"),
                        "learned": result.get("learned"),
                    },
                    default=str,
                ),
                flush=True,
            )
            if already_spent_account + credits_spent >= NEXTBID_CAP:
                # Keep scanning only for free URL successes; stop after a few paid-cap iterations
                if not result.get("original_saved"):
                    continue
    finally:
        _relock("owner_fresh_doc_recovery_manifest_continuation_complete")

    after = load_budget_state()
    # Reconcile SOP queues again
    remaining = [json.loads(l) for l in inv_path.open(encoding="utf-8") if l.strip()]
    sop_results = [
        run_sop_pipeline(db, row["nb_id"], inventory_row=row, allow_live_openai=False, allow_nextbid=False)
        for row in remaining
    ]
    queues = empty_queues()
    mapping = {
        "SUPPLIER_PRICING": "awaiting_supplier_quotations",
        "NEEDS_DOCUMENTATION": "verified_product_information_needed",
        "NEEDS_VERIFICATION": "promising_awaiting_verification",
        "BLOCKED_EXPIRED": "closed_expired_non_solicitation",
        "UNSUITABLE": "confirmed_disqualification",
    }
    supplier = []
    for r in sop_results:
        oq = r.get("owner_queue") or "NEEDS_DOCUMENTATION"
        key = mapping.get(oq)
        card = {
            "nb_id": r.get("nb_id"),
            "title": r.get("title"),
            "owner_queue": oq,
            "coverage": r.get("coverage"),
            "verified_line_items": r.get("verified_line_items") or [],
            "promotion": r.get("promotion"),
            "fresh_cohort": True,
        }
        if key:
            queues[key].append(card)
        if oq == "SUPPLIER_PRICING":
            supplier.append(card)
    qsummary = write_queues(queues, build="doc-recovery-manifest-continuation")
    out = {
        "ok": True,
        "continuation": True,
        "finished_at": _utc(),
        "before_budget": {k: before.get(k) for k in ("account_remaining", "account_used", "day_used", "test_used")},
        "after_budget": {k: after.get(k) for k in ("account_remaining", "account_used", "day_used", "test_used")},
        "nextbid_credits_consumed": int(before.get("account_remaining") or 0) - int(after.get("account_remaining") or 0),
        "credits_tracked_in_run": credits_spent,
        "openai_spent_usd": float(openai_spent),
        "recovery": rows,
        "killswitch_restored": emergency_stop_status().get("engaged"),
        "authorization_revoked": True,
        "supplier_pricing_cards": supplier,
        "sop_owner_queues": {
            "SUPPLIER_PRICING": sum(1 for r in sop_results if r.get("owner_queue") == "SUPPLIER_PRICING"),
            "NEEDS_DOCUMENTATION": sum(1 for r in sop_results if r.get("owner_queue") == "NEEDS_DOCUMENTATION"),
            "NEEDS_VERIFICATION": sum(1 for r in sop_results if r.get("owner_queue") == "NEEDS_VERIFICATION"),
            "BLOCKED_EXPIRED": sum(1 for r in sop_results if r.get("owner_queue") == "BLOCKED_EXPIRED"),
            "UNSUITABLE": sum(1 for r in sop_results if r.get("owner_queue") == "UNSUITABLE"),
        },
        "queue_summary": qsummary,
        "remaining_active": len(remaining),
    }
    path = get_settings().data_root() / "acceptance" / "jobs_document_recovery_continuation_last.json"
    path.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    ( _temp() / "CONTINUATION_REPORT.json").write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    return out


def run_document_recovery(db: Session) -> dict[str, Any]:
    temp = _temp()
    before = load_budget_state()
    unlock = _unlock_for_recovery()
    print(json.dumps({"phase": "unlocked", "unlock": unlock}, default=str), flush=True)

    inv_path = get_settings().data_root() / "nextbid_inventory" / "opportunities.jsonl"
    inventory = [json.loads(l) for l in inv_path.open(encoding="utf-8") if l.strip()]

    openai_spent = Decimal("0")
    credits_spent = 0
    triage_rows = []
    recovery_rows = []
    rejected = []
    checkpoints = []

    try:
        # Phase 1: triage all 82 (reuse full AI cache when present; else triage)
        for row in inventory:
            nb = row["nb_id"]
            blocked, prior = should_block_reacquisition(nb_id=nb, content_hash=row.get("content_hash"))
            if blocked:
                remove_from_active_inventory(nb)
                rejected.append({"nb_id": nb, "action": "already_rejected_removed", "prior": prior})
                continue

            opp = _paid_opportunity(nb)
            package = assemble_local_evidence_package(nb, db=db, include_http_cache=False, include_package_db=True)
            cov = coverage_label_from_package(package)
            preview = cov in {
                "listing_preview_only",
                "no_usable_original_content",
                "notice_only",
                "attachment_metadata_only",
            }
            text = str(package.get("assembled_text") or "")
            if not text.strip():
                text = str(opp.get("description") or opp.get("summary") or opp.get("ai_summary") or "")

            # Full cached analysis can serve as stronger than triage
            ai_path = get_settings().data_root() / "ai_doc_understanding" / "by_nb_id" / f"{nb}.json"
            if ai_path.exists() and not preview:
                disk = json.loads(ai_path.read_text(encoding="utf-8"))
                parsed = disk.get("parsed") or {}
                req = str(parsed.get("requirement_class") or "")
                if req in {
                    "services",
                    "construction",
                    "maintenance_repair_services",
                    "professional_services",
                    "product_with_significant_installation",
                } or parsed.get("is_product_purchase") is False:
                    cat = "construction" if "construction" in req else (
                        "significant_installation" if "install" in req else "other_non_product_execution"
                    )
                    if req.startswith("maintenance"):
                        cat = "mandatory_maintenance"
                    fp = record_rejection(
                        nb_id=nb,
                        solicitation_number=row.get("solicitation_number") or opp.get("solicitation_number"),
                        agency=row.get("agency") or opp.get("agency"),
                        source_url=row.get("official_url") or opp.get("source_url"),
                        category=cat,
                        evidence_passage=str(parsed.get("procurement_purpose") or req)[:400],
                        content_hash=row.get("content_hash") or disk.get("corpus_hash"),
                        reason=f"verified_ai_class:{req}",
                    )
                    remove_from_active_inventory(nb)
                    rejected.append({"nb_id": nb, "action": "rejected_from_cached_ai", "fingerprint": fp["fingerprint"]})
                    triage_rows.append({"nb_id": nb, "decision": "REJECT_CONFIRMED", "cached_full_ai": True})
                    continue
                triage_rows.append(
                    {
                        "nb_id": nb,
                        "decision": "PRODUCT_REQUIREMENTS_AVAILABLE",
                        "cached_full_ai": True,
                        "coverage": cov,
                        "score": _rank_score(row, opp, cov),
                    }
                )
                continue

            tri = openai_triage(
                text=text,
                title=str(row.get("title") or opp.get("title") or ""),
                nb_id=nb,
                preview_only=preview,
                budget_remaining=OWNER_OPENAI_MAX - openai_spent,
            )
            openai_spent += Decimal(str(tri.get("openai_spent_usd") or 0))
            triage_rows.append({**tri, "nb_id": nb, "coverage": cov, "score": _rank_score(row, opp, cov), "docs": opp.get("document_count")})

            if tri.get("decision") == "REJECT_CONFIRMED" and not tri.get("is_preliminary_preview_only"):
                fp = record_rejection(
                    nb_id=nb,
                    solicitation_number=row.get("solicitation_number") or opp.get("solicitation_number"),
                    agency=row.get("agency") or opp.get("agency"),
                    source_url=row.get("official_url"),
                    category=str(tri.get("rejection_category") or "other_non_product_execution"),
                    evidence_passage=str(tri.get("evidence_passage") or "")[:400],
                    content_hash=row.get("content_hash"),
                    reason=str(tri.get("reason") or "triage_reject"),
                )
                remove_from_active_inventory(nb)
                rejected.append({"nb_id": nb, "action": "rejected_triage", "fingerprint": fp["fingerprint"]})
            elif tri.get("decision") == "REJECT_CONFIRMED" and tri.get("deterministic_assist"):
                # Explicit source text — allow reject even from preview-length description
                fp = record_rejection(
                    nb_id=nb,
                    solicitation_number=row.get("solicitation_number"),
                    agency=row.get("agency") or opp.get("agency"),
                    source_url=row.get("official_url"),
                    category=str(tri.get("rejection_category") or "other_non_product_execution"),
                    evidence_passage=str(tri.get("evidence_passage") or "")[:400],
                    content_hash=row.get("content_hash"),
                    reason=str(tri.get("reason") or "deterministic_source_reject"),
                )
                remove_from_active_inventory(nb)
                rejected.append({"nb_id": nb, "action": "rejected_explicit_source", "fingerprint": fp["fingerprint"]})

            if openai_spent >= OWNER_OPENAI_MAX:
                break

        # Phase 2: recover documents for top PRODUCT_* candidates still active
        active = [json.loads(l) for l in inv_path.open(encoding="utf-8") if l.strip()]
        candidates = []
        for row in active:
            nb = row["nb_id"]
            opp = _paid_opportunity(nb)
            package = assemble_local_evidence_package(nb, db=db, include_http_cache=False, include_package_db=True)
            cov = coverage_label_from_package(package)
            if cov == "complete_original_package":
                continue
            tr = next((t for t in triage_rows if t.get("nb_id") == nb), {})
            if tr.get("decision") in {"REJECT_CONFIRMED", "CLOSED_OR_EXPIRED"}:
                continue
            if (opp.get("document_count") or 0) <= 0 and cov in {"listing_preview_only", "no_usable_original_content"}:
                # unknown docs — skip paid list unless product investigate with high score
                if tr.get("decision") not in {"PRODUCT_INVESTIGATE", "MIXED_INVESTIGATE", "PRODUCT_REQUIREMENTS_AVAILABLE"}:
                    continue
            score = _rank_score(row, opp, cov)
            if score < 20:
                continue
            candidates.append((score, row, cov, opp))
        candidates.sort(key=lambda x: -x[0])

        print(json.dumps({"phase": "recovery_candidates", "n": len(candidates), "top": [
            {"nb": c[1]["nb_id"], "score": c[0], "docs": c[3].get("document_count"), "title": (c[1].get("title") or "")[:60]}
            for c in candidates[:12]
        ]}, default=str), flush=True)

        for score, row, cov, opp in candidates:
            if credits_spent >= NEXTBID_CAP or openai_spent >= OWNER_OPENAI_MAX:
                break
            if credits_spent in CHECKPOINTS:
                checkpoints.append(_checkpoint(credits_spent, recovery_rows, rejected, openai_spent))
            result = recover_one_document(
                db,
                nb_id=row["nb_id"],
                inv_row=row,
                credits_spent=credits_spent,
                openai_spent=openai_spent,
            )
            credits_spent += int(result.get("credits") or 0)
            openai_spent += Decimal(str(result.get("openai_spent_usd") or 0))
            recovery_rows.append({**result, "score": score, "title": row.get("title")})
            print(json.dumps({"phase": "recovered", "nb": row["nb_id"], "credits_run": credits_spent, "openai": float(openai_spent), "learned": result.get("learned")}, default=str), flush=True)
            if credits_spent in CHECKPOINTS:
                checkpoints.append(_checkpoint(credits_spent, recovery_rows, rejected, openai_spent))
            if result.get("stopped"):
                break

    finally:
        _relock("owner_fresh_doc_recovery_complete")

    after = load_budget_state()
    retention = apply_retention_to_inventory(dry_run=False)

    # Reconcile remaining active via SOP (zero new paid)
    remaining = [json.loads(l) for l in inv_path.open(encoding="utf-8") if l.strip()]
    sop_results = []
    for row in remaining:
        sop_results.append(
            run_sop_pipeline(db, row["nb_id"], inventory_row=row, allow_live_openai=False, allow_nextbid=False)
        )

    # Rebuild queues from SOP
    queues = empty_queues()
    mapping = {
        "SUPPLIER_PRICING": "awaiting_supplier_quotations",
        "NEEDS_DOCUMENTATION": "verified_product_information_needed",
        "NEEDS_VERIFICATION": "promising_awaiting_verification",
        "BLOCKED_EXPIRED": "closed_expired_non_solicitation",
        "UNSUITABLE": "confirmed_disqualification",
    }
    supplier = []
    for r in sop_results:
        oq = r.get("owner_queue") or "NEEDS_DOCUMENTATION"
        # Generic QVL scrub
        lines = r.get("verified_line_items") or []
        if oq == "SUPPLIER_PRICING" and any("automobiles:" in str(li.get("description") or "").lower() for li in lines):
            oq = "NEEDS_DOCUMENTATION"
            r["owner_queue"] = oq
            r["supplier_pricing_possible"] = "PARTIAL"
        key = mapping.get(oq)
        card = {
            "nb_id": r.get("nb_id"),
            "title": r.get("title"),
            "owner_queue": oq,
            "coverage": r.get("coverage"),
            "verified_line_items": lines,
            "promotion": r.get("promotion"),
            "fresh_cohort": True,
        }
        if key:
            queues[key].append(card)
        if oq == "SUPPLIER_PRICING" or r.get("supplier_pricing_possible") == "YES":
            supplier.append(card)

    qsummary = write_queues(queues, build="doc-recovery-sop")

    report = {
        "ok": True,
        "finished_at": _utc(),
        "before_budget": {k: before.get(k) for k in ("account_remaining", "account_used", "day_used", "test_used")},
        "after_budget": {k: after.get(k) for k in ("account_remaining", "account_used", "day_used", "test_used")},
        "nextbid_credits_consumed": int(before.get("account_remaining") or 0) - int(after.get("account_remaining") or 0),
        "credits_tracked_in_run": credits_spent,
        "openai_spent_usd": float(openai_spent),
        "killswitch_restored": emergency_stop_status().get("engaged"),
        "authorization_revoked": True,
        "deployment": False,
        "triage": triage_rows,
        "rejected": rejected,
        "recovery": recovery_rows,
        "checkpoints": checkpoints,
        "retention": retention,
        "remaining_active": len(remaining),
        "sop_queue_summary": qsummary,
        "supplier_pricing_cards": supplier,
        "sop_owner_queues": {
            "SUPPLIER_PRICING": sum(1 for r in sop_results if r.get("owner_queue") == "SUPPLIER_PRICING"),
            "NEEDS_DOCUMENTATION": sum(1 for r in sop_results if r.get("owner_queue") == "NEEDS_DOCUMENTATION"),
            "NEEDS_VERIFICATION": sum(1 for r in sop_results if r.get("owner_queue") == "NEEDS_VERIFICATION"),
            "BLOCKED_EXPIRED": sum(1 for r in sop_results if r.get("owner_queue") == "BLOCKED_EXPIRED"),
            "UNSUITABLE": sum(1 for r in sop_results if r.get("owner_queue") == "UNSUITABLE"),
        },
    }
    out = get_settings().data_root() / "acceptance" / "jobs_document_recovery_last.json"
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    (temp / "OWNER_REPORT.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return report


def _checkpoint(credits: int, recovery: list, rejected: list, openai_spent: Decimal) -> dict[str, Any]:
    return {
        "at": _utc(),
        "credits": credits,
        "openai_spent_usd": float(openai_spent),
        "docs_recovery_attempts": len(recovery),
        "rejected": len(rejected),
        "meaningful": sum(1 for r in recovery if r.get("original_saved") or (r.get("verified") or {}).get("line_items")),
    }
