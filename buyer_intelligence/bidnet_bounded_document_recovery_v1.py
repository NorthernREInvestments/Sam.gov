"""Bounded BidNet document recovery — hard 45s/opportunity, entitlement taxonomy.

Reuses BidNetAuthenticatedClient + process_bidnet_opportunity + free_package_chase.
Does not purchase subscriptions. Stops BidNet doc retries on verified paywall.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import re
import threading
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

from application_clock import now_utc
from m3_data_root import data_path

log = __import__("logging").getLogger("govtracker.bidnet_bounded_document_recovery_v1")

BUILD = "20261014-m3-bidnet-bounded-document-recovery-v2"
OUT = "_og_commercial_research_pipeline_out"
REPORT = f"{OUT}/m3_bdd_v1_report.json"
RESULTS = f"{OUT}/m3_bdd_v1_results.json"
PROGRESS = f"{OUT}/m3_bdd_v1_progress.json"
LOCK = f"{OUT}/m3_bdd_v1_run.lock"
MANIFEST = f"{OUT}/m3_bdd_v1_10_manifest.json"

ITEM_TIMEOUT_S = 45.0
SAVE_EVERY = 1

# Blocker taxonomy (mutually exclusive primary class)
PAID_ENTITLEMENT = "PAID_ENTITLEMENT_REQUIRED"
AUTH_FAILURE = "AUTHENTICATION_FAILURE"
WAF_OR_BOT = "WAF_OR_BOT_BLOCK"
MISSING_DOC_LINKS = "MISSING_DOCUMENT_LINKS"
DOCS_INACCESSIBLE = "DOCUMENTS_INACCESSIBLE"
CACHE_ONLY_INCOMPLETE = "CACHE_INCOMPLETE_NO_NETWORK_GAIN"
FREE_PORTAL_UNAVAILABLE = "FREE_PORTAL_UNAVAILABLE"
PER_OPP_TIMEOUT = "PER_OPPORTUNITY_TIMEOUT"
COMPLETE_PACKAGE = "COMPLETE_PACKAGE_RECOVERED"
PG_REGISTRATION_REQUIRED = "PURCHASING_GROUP_REGISTRATION_REQUIRED"


def _utc() -> str:
    return now_utc().isoformat()


def _save(rel: str, obj: Any) -> Path:
    path = Path(data_path(rel))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)
    return path


def _load(rel: str) -> Any:
    path = Path(data_path(rel))
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _acquire_lock() -> Path | None:
    lock = Path(data_path(LOCK))
    lock.parent.mkdir(parents=True, exist_ok=True)
    if lock.exists():
        try:
            if time.time() - lock.stat().st_mtime < 3600:
                return None
        except Exception:
            pass
    lock.write_text(str(os.getpid()), encoding="utf-8")
    return lock


def _release_lock(lock: Path | None) -> None:
    if lock:
        try:
            lock.unlink(missing_ok=True)
        except Exception:
            pass


def _progress(**kwargs: Any) -> None:
    cur = _load(PROGRESS) or {"kind": "BoundedDocRecoveryProgress", "build": BUILD}
    cur.update(kwargs)
    cur["heartbeat_at"] = _utc()
    _save(PROGRESS, cur)


def classify_document_blocker(
    *,
    auth_ok: bool,
    auth_status: str | None,
    paid_requirement: str | None,
    paid_intercept: str | None,
    package_state: str | None,
    detail_status: str | None,
    docs: list[dict[str, Any]],
    free_chase_status: str | None,
    fetch_error: str | None,
    timed_out: bool,
) -> tuple[str, dict[str, Any]]:
    """Return (primary_blocker, evidence). Entitlement beats auth/WAF when intercept proven."""
    evidence: dict[str, Any] = {
        "auth_ok": auth_ok,
        "auth_status": auth_status,
        "paid_requirement": paid_requirement,
        "paid_intercept_present": bool(paid_intercept),
        "package_state": package_state,
        "detail_status": detail_status,
        "doc_count": len(docs or []),
        "free_chase_status": free_chase_status,
        "fetch_error": (fetch_error or "")[:160] or None,
        "timed_out": timed_out,
    }
    if timed_out:
        return PER_OPP_TIMEOUT, evidence
    if paid_requirement or (
        paid_intercept
        and (
            "subscription-management" in str(paid_intercept).lower()
            or "/intercept/plans" in str(paid_intercept).lower()
        )
    ):
        evidence["classification_basis"] = "bidnet_subscription_intercept_url"
        return PAID_ENTITLEMENT, evidence
    if not auth_ok or str(auth_status or "").upper() in {
        "EXPIRED",
        "AUTH_FAILED",
        "LOGIN_REQUIRED",
        "DISABLED",
    }:
        evidence["classification_basis"] = "auth_status"
        return AUTH_FAILURE, evidence
    err = str(fetch_error or "").lower()
    if any(x in err for x in ("waf", "captcha", "challenge", "cloudflare", "403", "gokuprops")):
        evidence["classification_basis"] = "fetch_error_pattern"
        return WAF_OR_BOT, evidence
    if str(detail_status or "").upper() in {"SUPPLIER_REGISTRATION_REDIRECT", "DETAIL_REGISTRATION_REDIRECT"}:
        evidence["classification_basis"] = "detail_registration_redirect"
        return PAID_ENTITLEMENT, evidence
    pkg = str(package_state or "").upper()
    if pkg in {
        "PACKAGE_PG_REGISTRATION_OWNER_REQUIRED",
        "PACKAGE_PG_REGISTRATION_PREPARED",
        "PACKAGE_PG_REGISTRATION_INCOMPLETE",
    } or str(detail_status or "").upper() == "DETAIL_ACCESS_GATE":
        evidence["classification_basis"] = "purchasing_group_registration_gate"
        return PG_REGISTRATION_REQUIRED, evidence
    if pkg in {"PACKAGE_REGISTRATION_REQUIRED", "PACKAGE_LOCKED_MEMBERSHIP"}:
        evidence["classification_basis"] = "package_state_membership_or_registration"
        return PAID_ENTITLEMENT, evidence
    real_docs = [d for d in (docs or []) if _is_real_doc(d)]
    evidence["real_doc_count"] = len(real_docs)
    if real_docs and any(_doc_has_qty_signal(d) for d in real_docs):
        return COMPLETE_PACKAGE, evidence
    if real_docs:
        evidence["classification_basis"] = "docs_without_extractable_lines"
        return DOCS_INACCESSIBLE, evidence
    if free_chase_status in {"PACKAGE_UNAVAILABLE_FREE", "PACKAGE_RECOVERY_RETRYABLE", "PACKAGE_MATCH_AMBIGUOUS"}:
        evidence["classification_basis"] = "free_chase_exhausted"
        return FREE_PORTAL_UNAVAILABLE, evidence
    if pkg in {"PACKAGE_RETRYABLE", "PACKAGE_EXTERNAL_PORTAL_REQUIRED"} or str(detail_status or "").upper() in {
        "DETAIL_FAILED_RETRYABLE",
        "DETAIL_LOCKED",
        "NO_DETAIL_AVAILABLE",
    }:
        evidence["classification_basis"] = "detail_failed_or_locked_no_docs"
        return DOCS_INACCESSIBLE, evidence
    evidence["classification_basis"] = "no_document_links_after_auth"
    return MISSING_DOC_LINKS, evidence


def _is_real_doc(d: dict[str, Any]) -> bool:
    if not isinstance(d, dict):
        return False
    if d.get("kind") == "public_abstract":
        return False
    u = str(d.get("document_url") or d.get("url") or "").lower()
    name = str(d.get("document_name") or d.get("filename") or "").lower()
    local = str(d.get("local_path") or d.get("canonical_storage_reference") or "").lower()
    blob = f"{u} {name} {local}"
    if re.search(r"\.(js|css)(?:$|\?)|/jawr/|interceptor|/solicitations/search", blob):
        return False
    if re.search(r"\.(pdf|xlsx?|docx?|csv|zip)(?:$|\?)|download/intercept|getfile|fileid", blob):
        return True
    if local and Path(local).exists() and Path(local).stat().st_size >= 512:
        if re.search(r"\.(pdf|xlsx?|docx?|csv|zip)$", local):
            return True
    return False


def _has_downloaded_docs(docs: list[dict[str, Any]] | None) -> bool:
    """True only when at least one real file is on disk (abstract stubs do not count)."""
    for d in docs or []:
        if not isinstance(d, dict) or not _is_real_doc(d):
            continue
        lp = d.get("local_path") or d.get("canonical_storage_reference")
        if not lp:
            continue
        try:
            p = Path(str(lp))
            if p.exists() and p.stat().st_size >= 512:
                return True
        except Exception:
            continue
    return False


def _doc_has_qty_signal(d: dict[str, Any]) -> bool:
    """Weak signal used only after real docs exist; full line extract is separate."""
    return bool(d.get("local_path") or d.get("canonical_storage_reference"))


def select_bounded_10(*, n: int = 10) -> dict[str, Any]:
    """Diverse buyers from CFE-50 manifest; prefer non-PA + variety."""
    man = _load(f"{OUT}/m3_cfe_v1_50_manifest.json") or {}
    rows = list(man.get("rows") or [])
    # diversity: one per buyer first, then fill
    by_buyer: dict[str, list] = {}
    for r in rows:
        b = str(r.get("buyer") or "UNKNOWN")
        by_buyer.setdefault(b, []).append(r)
    selected: list[dict[str, Any]] = []
    def _pick(b: str) -> dict[str, Any]:
        cand = sorted(by_buyer[b], key=lambda r: -float(r.get("days_left") or 0))[0]
        return {
            "opportunity_id": cand["opportunity_id"],
            "title": cand.get("title"),
            "buyer": cand.get("buyer"),
            "deadline": cand.get("deadline"),
            "days_left": cand.get("days_left"),
            "authoritative_url": cand.get("authoritative_url"),
            "selection_score": cand.get("selection_score"),
        }

    # Diversity: unique non-PA buyers first, then force ≥1 PA (paid-wall access condition)
    non_pa = sorted([b for b in by_buyer if b.lower() not in {"pennsylvania", "pa"}], key=str)
    pa_buyers = [b for b in by_buyer if b.lower() in {"pennsylvania", "pa"}]
    for b in non_pa:
        if len(selected) >= max(0, n - (1 if pa_buyers else 0)):
            break
        selected.append(_pick(b))
    if pa_buyers and len(selected) < n:
        selected.append(_pick(pa_buyers[0]))
    for b in non_pa:
        if len(selected) >= n:
            break
        if any(s.get("buyer") == b for s in selected):
            continue
        selected.append(_pick(b))
    # fill remaining from leftover
    if len(selected) < n:
        seen = {s["opportunity_id"] for s in selected}
        rest = [r for r in rows if r["opportunity_id"] not in seen]
        rest.sort(key=lambda r: -float(r.get("days_left") or 0))
        for r in rest:
            if len(selected) >= n:
                break
            selected.append(
                {
                    "opportunity_id": r["opportunity_id"],
                    "title": r.get("title"),
                    "buyer": r.get("buyer"),
                    "deadline": r.get("deadline"),
                    "days_left": r.get("days_left"),
                    "authoritative_url": r.get("authoritative_url"),
                    "selection_score": r.get("selection_score"),
                }
            )
    out = {
        "kind": "BoundedDocRecovery10Manifest",
        "build": BUILD,
        "run_id": f"bdd10-{uuid.uuid4().hex[:8]}",
        "created_at": _utc(),
        "item_timeout_s": ITEM_TIMEOUT_S,
        "selected_n": len(selected),
        "rows": selected[:n],
    }
    _save(MANIFEST, out)
    return out


def _cache_snapshot(l23: dict[str, Any]) -> dict[str, Any]:
    br = l23.get("bidnet_recovery") or {}
    docs = list(l23.get("documents") or l23.get("attachments_metadata") or [])
    real = [d for d in docs if isinstance(d, dict) and _is_real_doc(d)]
    chase = br.get("free_package_chase") or {}
    return {
        "cached_doc_refs": len(docs),
        "cached_real_docs": len(real),
        "cached_blocker": br.get("blocker"),
        "prior_free_chase": chase.get("status"),
        "prior_free_docs": int(chase.get("document_count") or 0),
        "detail_url": br.get("detail_url"),
        "used_cache_only": False,
    }


def _extract_lines(title: str, parsed: dict[str, Any], docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Prefer schedule/spreadsheet extraction from local docs; fall back to text evidence."""
    # 1) Authoritative package schedule extraction (xls/xlsx/pdf/csv)
    local_docs = []
    for d in docs or []:
        if not isinstance(d, dict):
            continue
        lp = d.get("local_path") or d.get("canonical_storage_reference")
        if not lp:
            continue
        p = Path(str(lp))
        if p.exists() and re.search(r"\.(pdf|xlsx?|xls|csv|docx?|txt)$", p.suffix, re.I):
            local_docs.append(
                {
                    **d,
                    "local_path": str(p),
                    "document_name": d.get("document_name") or d.get("filename") or d.get("name") or p.name,
                    "filename": d.get("filename") or d.get("document_name") or d.get("name") or p.name,
                }
            )
    if local_docs:
        try:
            from bidnet_engine.schedule_extraction import extract_schedules_from_package

            sched = extract_schedules_from_package(
                local_docs,
                opportunity_id=str(parsed.get("solicitation_number") or ""),
                body_text=str(parsed.get("description") or parsed.get("overview") or title or ""),
            )
            rows = [r for r in (sched.get("schedule_rows") or []) if isinstance(r, dict)]
            if rows:
                return rows
        except Exception:
            pass

    # 2) Text-evidence fallback (pdf/txt/csv only)
    try:
        from buyer_intelligence.package_recovery_source_depth_v1 import extract_lines_from_evidence
    except Exception:
        return []
    desc = str(parsed.get("description") or parsed.get("overview") or "")
    for d in local_docs[:6]:
        p = Path(str(d.get("local_path") or ""))
        if not p.exists() or not re.search(r"\.(pdf|txt|csv)$", p.suffix, re.I):
            continue
        try:
            if p.suffix.lower() == ".pdf":
                from pypdf import PdfReader

                desc += "\n" + "\n".join((pg.extract_text() or "") for pg in PdfReader(str(p)).pages[:40])
            else:
                desc += "\n" + p.read_text(encoding="utf-8", errors="ignore")[:60000]
        except Exception:
            pass
    try:
        return extract_lines_from_evidence(
            title=title,
            description=desc[:80000],
            source="bounded_doc_recovery",
            location=str(parsed.get("location") or ""),
        )
    except Exception:
        return []


def _attempt_pg_gate_unlock(
    auth_client: Any,
    *,
    html: str,
    page_url: str,
    source_url: str,
    title: str,
    opportunity_id: str,
    deadline: float,
    remain_ms_fn: Any,
    out: dict[str, Any],
) -> tuple[str, str, str, list[dict[str, Any]], dict[str, Any]]:
    """Invoke existing PG registration automation, then retry detail when registered.

    Returns (html, page_url, detail_status, docs, parsed).
    """
    from bidnet_auth.purchasing_group_registration import (
        extract_pg_gate_from_html,
        extract_pg_id_from_url,
        resolve_access_gate_registration,
    )
    from bidnet_auth.purchasing_group_tracker import (
        STATUS_OWNER_APPROVAL_REQUIRED,
        STATUS_PAID_REQUIRED,
        STATUS_PREPARED,
        STATUS_REGISTERED,
        get_group,
        is_registered,
        note_blocked_opportunity,
        upsert_group,
    )
    from bidnet_recovery.parse_abstract import parse_bidnet_abstract

    docs: list[dict[str, Any]] = []
    parsed: dict[str, Any] = {}
    detail_status = "DETAIL_ACCESS_GATE"
    gate = extract_pg_gate_from_html(html, page_url=page_url)
    pg_id = (
        (gate or {}).get("purchasing_group_id")
        or extract_pg_id_from_url(page_url)
        or extract_pg_id_from_url(source_url)
    )
    already_registered = False
    if pg_id:
        note_blocked_opportunity(str(pg_id), opportunity_id)
        out["purchasing_group_gate"] = {
            k: (gate or {}).get(k)
            for k in ("name", "purchasing_group_id", "gate_kind", "fee_mentioned")
        } if gate else {
            "name": None,
            "purchasing_group_id": pg_id,
            "gate_kind": "PURCHASING_GROUP_FROM_URL",
            "fee_mentioned": False,
        }
        # Already registered at group level — skip form, retry detail only
        if is_registered(str(pg_id)):
            already_registered = True
            out["purchasing_group_registration"] = {
                "status": STATUS_REGISTERED,
                "already_registered": True,
                "skipped": True,
                "purchasing_group_id": pg_id,
            }
        else:
            tracked = get_group(str(pg_id)) or {}
            if tracked.get("status") in {
                STATUS_PAID_REQUIRED,
                STATUS_OWNER_APPROVAL_REQUIRED,
                "REGISTERING",
                "ERROR",
            }:
                # Do not burn opportunity budget re-attempting group registration.
                # Group-level worker owns registration; opportunity waits & retries.
                try:
                    from bidnet_auth.purchasing_group_queue import enqueue_group_registration

                    enqueue_group_registration(
                        str(pg_id),
                        name=(gate or {}).get("name"),
                        opportunity_id=opportunity_id,
                        source_url=source_url,
                        form_url=(gate or {}).get("complete_form_url"),
                    )
                except Exception:
                    pass
                out["purchasing_group_registration"] = {
                    "status": tracked.get("status") or "QUEUED",
                    "skipped": True,
                    "reason": "deferred_to_group_level_queue",
                    "purchasing_group_id": pg_id,
                    "owner_blocker_reason": tracked.get("owner_blocker_reason"),
                }
                out["package_blocker_hint"] = "AWAITING_PURCHASING_GROUP_REGISTRATION"
                return html, page_url, detail_status, docs, parsed

    if time.monotonic() >= deadline and not already_registered:
        out["purchasing_group_registration"] = {
            "status": "DEADLINE_EXCEEDED",
            "purchasing_group_id": pg_id,
        }
        return html, page_url, detail_status, docs, parsed

    try:
        if already_registered:
            reg = {
                "status": STATUS_REGISTERED,
                "already_registered": True,
                "skipped": True,
                "purchasing_group_id": pg_id,
            }
        elif pg_id:
            # Opportunity path: enqueue group work; do NOT run long form fill here.
            try:
                from bidnet_auth.purchasing_group_queue import enqueue_group_registration

                enqueue_group_registration(
                    str(pg_id),
                    name=(gate or {}).get("name"),
                    opportunity_id=opportunity_id,
                    source_url=source_url,
                    form_url=(gate or {}).get("complete_form_url"),
                )
            except Exception as exc:
                out["pg_enqueue_error"] = type(exc).__name__
            out["purchasing_group_registration"] = {
                "status": "QUEUED",
                "skipped": True,
                "reason": "deferred_to_group_level_queue",
                "purchasing_group_id": pg_id,
            }
            out["package_blocker_hint"] = "AWAITING_PURCHASING_GROUP_REGISTRATION"
            out["access_note"] = "pg_registration_deferred_group_queue"
            return html, page_url, detail_status, docs, parsed
        else:
            reg = resolve_access_gate_registration(
                auth_client,
                html=html,
                page_url=page_url,
                allow_submit=True,
                opportunity_id=opportunity_id,
                source_url=source_url,
            )
            out["purchasing_group_registration"] = {
                "status": reg.get("status"),
                "submitted": reg.get("submitted"),
                "skipped": reg.get("skipped"),
                "already_registered": reg.get("already_registered"),
                "owner_next_steps": reg.get("owner_next_steps"),
                "purchasing_group_id": reg.get("purchasing_group_id") or pg_id,
                "reason": reg.get("reason"),
            }
            if not out.get("purchasing_group_gate") and reg.get("gate"):
                g = reg["gate"]
                out["purchasing_group_gate"] = {
                    k: g.get(k)
                    for k in ("name", "purchasing_group_id", "gate_kind", "fee_mentioned")
                }
        st = str(reg.get("status") or "")
        unlocked = st == STATUS_REGISTERED or bool(reg.get("already_registered"))
        if unlocked:
            # Registered membership: reopen via title search → interception → private
            # detail → Documents tab. Never invent /private/.../{statewide_id}/.
            out["network_requests"] = int(out.get("network_requests") or 0) + 1
            retry: dict[str, Any] = {}
            if hasattr(auth_client, "resolve_detail_via_authenticated_search"):
                retry = (
                    auth_client.resolve_detail_via_authenticated_search(
                        title=title,
                        source_url=source_url,
                        timeout_ms=remain_ms_fn(35_000),
                    )
                    or {}
                )
            out["search_to_detail_retry"] = {
                "status": retry.get("status"),
                "access_gate": retry.get("access_gate"),
                "detail_url": (retry.get("detail_url") or "")[:160] or None,
                "solicitation_id": retry.get("solicitation_id"),
            }
            if (
                retry.get("ok")
                and retry.get("html")
                and retry.get("access_gate") != "ACTION_REQUIRED"
            ):
                html = retry["html"]
                page_url = str(retry.get("detail_url") or page_url)
                out["final_url"] = page_url[:220]
                out["access_note"] = "pg_registration_unlocked_detail"
                if retry.get("solicitation_id"):
                    out["bidnet_solicitation_id"] = retry.get("solicitation_id")
                if pg_id:
                    prev = int((get_group(str(pg_id)) or {}).get("solicitations_unlocked") or 0)
                    upsert_group(str(pg_id), solicitations_unlocked=prev + 1)
                parsed = parse_bidnet_abstract(html, detail_url=page_url) or {}
                detail_status = (
                    "DETAIL_OK"
                    if parsed.get("parse_ok") or parsed.get("title")
                    else "DETAIL_PARTIAL"
                )
                docs = [d for d in (parsed.get("documents") or []) if isinstance(d, dict)]
            else:
                out["access_note"] = "pg_registered_detail_retry_pending"
                detail_status = detail_status or "DETAIL_ACCESS_GATE"

            # Documents tab / title-based discovery (shared production path)
            if (
                not _has_downloaded_docs(docs)
                and time.monotonic() < deadline
            ):
                try:
                    discovered: list[dict[str, Any]] = []
                    priv = str(out.get("final_url") or page_url or "")
                    if (
                        priv
                        and "/private/solicitations/" in priv.lower()
                        and hasattr(auth_client, "discover_attachment_links")
                    ):
                        out["network_requests"] = int(out.get("network_requests") or 0) + 1
                        discovered = (
                            auth_client.discover_attachment_links(
                                priv, timeout_ms=remain_ms_fn(40_000)
                            )
                            or []
                        )
                    if (
                        not discovered
                        and hasattr(auth_client, "discover_attachments_by_title")
                        and title
                    ):
                        out["network_requests"] = int(out.get("network_requests") or 0) + 1
                        discovered = (
                            auth_client.discover_attachments_by_title(
                                title, timeout_ms=remain_ms_fn(40_000)
                            )
                            or []
                        )
                    if (
                        not discovered
                        and hasattr(auth_client, "discover_attachments_from_statewide")
                        and source_url
                        and (
                            "/statewide/" in source_url.lower()
                            or "/open-bids/" in source_url.lower()
                        )
                    ):
                        out["network_requests"] = int(out.get("network_requests") or 0) + 1
                        discovered = (
                            auth_client.discover_attachments_from_statewide(
                                source_url,
                                title=title,
                                timeout_ms=remain_ms_fn(40_000),
                            )
                            or []
                        )
                    docs = [
                        d for d in discovered if isinstance(d, dict) and _is_real_doc(d)
                    ]
                    out["document_links_discovered"] = len(discovered)
                    if docs:
                        out["access_note"] = "pg_registered_docs_tab_recovered"
                        detail_status = "DETAIL_OK"
                except Exception as exc:
                    out["discover_after_pg_error"] = type(exc).__name__
        elif st == STATUS_PAID_REQUIRED:
            out["access_note"] = "pg_registration_paid_required"
        elif st in {STATUS_OWNER_APPROVAL_REQUIRED, STATUS_PREPARED}:
            out["access_note"] = f"pg_registration_{st.lower()}"
        else:
            out["access_note"] = f"pg_registration_{st.lower() or 'incomplete'}"
    except Exception as exc:
        out["purchasing_group_registration_error"] = type(exc).__name__
        out["access_note"] = "pg_registration_exception"
    return html, page_url, detail_status, docs, parsed


def _recover_one_inner(
    item: dict[str, Any],
    *,
    auth_client: Any,
    l23: dict[str, Any],
    timeout_s: float = ITEM_TIMEOUT_S,
) -> dict[str, Any]:
    """Single opportunity recovery body (runs under hard timeout)."""
    from bidnet_recovery.free_package_chase import chase_free_package
    from package_recovery_sam_budget.quality import is_valid_package_evidence

    oid = item["opportunity_id"]
    title = str(item.get("title") or "")
    url = str(item.get("authoritative_url") or "")
    cache = _cache_snapshot(l23)
    out: dict[str, Any] = {
        "opportunity_id": oid,
        "title": title,
        "buyer": item.get("buyer"),
        "deadline": item.get("deadline"),
        "authoritative_url": url,
        "cache": cache,
        "started_at": _utc(),
        "network_requests": 0,
    }

    # Cache short-circuit: real docs already present
    cached_docs = [
        d
        for d in (l23.get("documents") or l23.get("attachments_metadata") or [])
        if isinstance(d, dict) and _is_real_doc(d)
    ]
    if cached_docs and is_valid_package_evidence(cached_docs).get("valid"):
        parsed = {}
        lines = _extract_lines(title, parsed, cached_docs)
        out["documents"] = cached_docs
        out["line_items"] = lines
        out["cache"]["used_cache_only"] = True
        out["package_state"] = "PACKAGE_ACQUIRED_CACHED"
        blocker, evidence = classify_document_blocker(
            auth_ok=True,
            auth_status="SESSION_REUSED",
            paid_requirement=None,
            paid_intercept=None,
            package_state="PACKAGE_ACQUIRED_CACHED",
            detail_status="CACHE",
            docs=cached_docs,
            free_chase_status=None,
            fetch_error=None,
            timed_out=False,
        )
        # If lines incomplete, still report docs recovered
        qty_ok = any(
            (isinstance(li.get("QTY"), dict) and li["QTY"].get("VALUE") not in (None, ""))
            or li.get("quantity") not in (None, "")
            for li in lines
            if isinstance(li, dict)
        )
        if qty_ok and lines and not (
            len(lines) == 1 and lines[0].get("LINE_SOURCE_TYPE") == "TITLE_FALLBACK"
        ):
            blocker = COMPLETE_PACKAGE
        else:
            blocker = DOCS_INACCESSIBLE if cached_docs else CACHE_ONLY_INCOMPLETE
        out["primary_blocker"] = blocker
        out["blocker_evidence"] = evidence
        out["complete_spec"] = blocker == COMPLETE_PACKAGE
        out["finished_at"] = _utc()
        return out

    meta = {
        "opportunity_id": oid,
        "title": title,
        "buyer": item.get("buyer"),
        "authoritative_url": url,
        "deadline": item.get("deadline"),
    }
    # Fast path (not full process_bidnet): must finish inside wall clock.
    # Full process hangs on multi-candidate Playwright discovery.
    from bidnet_full_production.process import _sol_from_paid_intercept
    from bidnet_recovery.parse_abstract import parse_bidnet_abstract
    from bidnet_auth.purchasing_group_registration import extract_pg_id_from_url
    from bidnet_auth.purchasing_group_tracker import is_registered

    deadline = time.monotonic() + max(5.0, float(timeout_s) - 3.0)

    def _remain_ms(cap: int = 12_000) -> int:
        return max(500, min(cap, int((deadline - time.monotonic()) * 1000)))

    parsed: dict[str, Any] = {}
    docs: list[dict[str, Any]] = []
    paid_req = None
    paid_intercept = None
    detail_status = "FAST_PATH"
    package_state = "PACKAGE_RETRYABLE"
    fetch_error = None
    html = ""

    # 0) Registered PG + statewide/open-bids: skip public URL (wrong ID namespace).
    # Go straight to authenticated search → private detail → Documents tab.
    # Try title, open-bids/statewide numeric IDs, and URL slug — not title alone.
    pg_early = extract_pg_id_from_url(url)
    url_l0 = (url or "").lower()
    if (
        pg_early
        and is_registered(str(pg_early))
        and (
            "/statewide/" in url_l0
            or "/open-bids/" in url_l0
            or "/public/supplier/" in url_l0
            or "purchasinggroupid=" in url_l0
        )
        and hasattr(auth_client, "resolve_detail_via_authenticated_search")
        and time.monotonic() < deadline
    ):
        out["purchasing_group_id"] = str(pg_early)
        out["purchasing_group_registration"] = {
            "status": "REGISTERED",
            "already_registered": True,
            "purchasing_group_id": str(pg_early),
            "early_unlock": True,
        }
        search_queries: list[str] = []
        if title and len(title.strip()) >= 6:
            search_queries.append(title.strip()[:120])
        m_ob = re.search(r"/open-bids/[^/]+/(\d{6,})", url, re.I)
        m_sw = re.search(r"/statewide/(\d{6,})", url, re.I)
        m_slug = re.search(r"/open-bids/([^/]+)/\d+", url, re.I)
        if m_ob:
            search_queries.append(m_ob.group(1))
            search_queries.append(m_ob.group(1).lstrip("0") or m_ob.group(1))
        if m_sw:
            search_queries.append(m_sw.group(1))
        if m_slug:
            slug_title = re.sub(r"[-_]+", " ", m_slug.group(1)).strip()
            if slug_title and slug_title.lower() not in {q.lower() for q in search_queries}:
                search_queries.append(slug_title[:120])
        # de-dupe preserve order
        seen_q: set[str] = set()
        uniq_q: list[str] = []
        for q in search_queries:
            k = q.lower()
            if k in seen_q:
                continue
            seen_q.add(k)
            uniq_q.append(q)

        early: dict[str, Any] = {}
        for q in uniq_q:
            if time.monotonic() >= deadline:
                break
            out["network_requests"] += 1
            early = (
                auth_client.resolve_detail_via_authenticated_search(
                    title=q,
                    source_url=url,
                    timeout_ms=_remain_ms(35_000),
                )
                or {}
            )
            out["early_search_to_detail"] = {
                "status": early.get("status"),
                "query": q[:120],
                "detail_url": (early.get("detail_url") or "")[:160] or None,
                "solicitation_id": early.get("solicitation_id"),
                "access_gate": early.get("access_gate"),
            }
            if early.get("ok") and early.get("detail_url"):
                break

        if (
            early.get("ok")
            and early.get("detail_url")
            and early.get("access_gate") != "ACTION_REQUIRED"
        ):
            url = str(early["detail_url"])
            out["final_url"] = url[:220]
            out["bidnet_solicitation_id"] = early.get("solicitation_id")
            html = early.get("html") or ""
            detail_status = "DETAIL_OK"
            if html:
                parsed = parse_bidnet_abstract(html, detail_url=url) or {}
            if hasattr(auth_client, "discover_attachment_links") and time.monotonic() < deadline:
                out["network_requests"] += 1
                discovered = (
                    auth_client.discover_attachment_links(url, timeout_ms=_remain_ms(45_000))
                    or []
                )
                docs = [d for d in discovered if isinstance(d, dict) and _is_real_doc(d)]
                out["document_links_discovered"] = len(discovered)
                out["discover_url"] = url[:220]
                if docs:
                    package_state = "PACKAGE_ACQUIRED_BIDNET"
                    out["access_note"] = "early_registered_pg_docs_recovered"
                else:
                    out["access_note"] = "early_registered_detail_no_docs"
            else:
                out["access_note"] = "early_registered_detail_opened"
        elif early.get("access_gate") == "ACTION_REQUIRED":
            detail_status = "DETAIL_ACCESS_GATE"
            out["access_note"] = "early_registered_still_action_required"
        else:
            out["access_note"] = f"early_registered_resolve_{early.get('status') or 'failed'}"

    # 1) Authenticated detail fetch — browser path required for public/statewide +
    # purchasing-group URLs (prefer_http=True was causing DETAIL_FETCH_EMPTY at scale;
    # Copy Paper / docs-tab recovery proved prefer_http=False + browser navigation).
    if url and not _has_downloaded_docs(docs) and time.monotonic() < deadline:
        out["network_requests"] += 1
        try:
            url_l = url.lower()
            needs_browser = bool(
                "purchasinggroupid=" in url_l
                or "/public/supplier/" in url_l
                or "/statewide/" in url_l
                or "/open-bids/" in url_l
                or "bidnetdirect.com" in url_l
            )
            html = (
                auth_client.fetch_html(
                    url,
                    timeout_ms=_remain_ms(18_000 if needs_browser else 12_000),
                    prefer_http=not needs_browser,
                )
                or ""
            )
            out["fetch_prefer_http"] = not needs_browser
            page_url = str(getattr(getattr(auth_client, "_page", None), "url", None) or url)
            out["final_url"] = page_url[:220]
            page_l = page_url.lower()
            # Entitlement ONLY from navigated URL (not footer/nav HTML mentioning plans)
            if "subscription-management" in page_l or "/intercept/plans" in page_l:
                paid_intercept = page_url
                mreq = re.search(r"requirements=([^&\s#]+)", page_url, re.I)
                paid_req = mreq.group(1) if mreq else "INTERCEPT_PLANS"
                detail_status = "SUPPLIER_REGISTRATION_REDIRECT"
                package_state = "PACKAGE_REGISTRATION_REQUIRED"
                sol = _sol_from_paid_intercept(paid_intercept)
                if sol:
                    out["solicitation_from_intercept"] = sol
                    parsed["solicitation_number"] = sol
            elif html:
                # Explicit document-lock copy (not generic subscription nav links)
                lock_hit = re.search(
                    r"(bid\s*documents?\s*(are\s*)?(locked|restricted)|"
                    r"requires?\s+(an?\s+)?(additional\s+)?(paid\s+)?subscription|"
                    r"upgrade\s+your\s+subscription\s+to\s+(view|download)\s+documents)",
                    html,
                    re.I,
                )
                href_intercept = re.search(
                    r'href=["\']([^"\']*(?:subscription-management|/intercept/plans)[^"\']*requirements=[^"\']+)["\']',
                    html,
                    re.I,
                )
                if href_intercept and lock_hit:
                    paid_intercept = href_intercept.group(1)[:220]
                    mreq = re.search(r"requirements=([^&\s#]+)", paid_intercept, re.I)
                    paid_req = mreq.group(1) if mreq else "INTERCEPT_PLANS"
                    detail_status = "SUPPLIER_REGISTRATION_REDIRECT"
                    package_state = "PACKAGE_REGISTRATION_REQUIRED"
                    sol = _sol_from_paid_intercept(paid_intercept)
                    if sol:
                        out["solicitation_from_intercept"] = sol
                        parsed["solicitation_number"] = sol
                else:
                    from bidnet_engine.package_materialization import is_bidnet_search_page_url

                    if is_bidnet_search_page_url(page_url):
                        detail_status = "SEARCH_PAGE_NOT_DETAIL"
                        package_state = "PACKAGE_RETRYABLE"
                        out["access_note"] = "auth_landed_on_private_search_not_solicitation"
                        # Repair path: search → interception/open-solicitation → detail
                        if hasattr(auth_client, "resolve_detail_via_authenticated_search") and (
                            time.monotonic() < deadline
                        ):
                            out["network_requests"] += 1
                            resolved = auth_client.resolve_detail_via_authenticated_search(
                                title=title,
                                source_url=url,
                                search_html=html,
                                timeout_ms=_remain_ms(18_000),
                            ) or {}
                            out["search_to_detail"] = {
                                "status": resolved.get("status"),
                                "via": resolved.get("via"),
                                "solicitation_id": resolved.get("solicitation_id"),
                                "detail_url": (resolved.get("detail_url") or "")[:160] or None,
                                "access_gate": resolved.get("access_gate"),
                            }
                            if resolved.get("ok") and resolved.get("html"):
                                html = resolved["html"]
                                page_url = str(resolved.get("detail_url") or page_url)
                                out["final_url"] = page_url[:220]
                                out["bidnet_solicitation_id"] = resolved.get("solicitation_id")
                                if resolved.get("access_gate") == "ACTION_REQUIRED":
                                    detail_status = "DETAIL_ACCESS_GATE"
                                    package_state = "PACKAGE_LOCKED_MEMBERSHIP"
                                    out["access_note"] = "interception_action_required_before_access"
                                    # Access recovery: purchasing-group registration → retry detail
                                    html, page_url, detail_status, docs, parsed = (
                                        _attempt_pg_gate_unlock(
                                            auth_client,
                                            html=html,
                                            page_url=page_url,
                                            source_url=url,
                                            title=title,
                                            opportunity_id=oid,
                                            deadline=deadline,
                                            remain_ms_fn=_remain_ms,
                                            out=out,
                                        )
                                    )
                                    st = str(
                                        (out.get("purchasing_group_registration") or {}).get(
                                            "status"
                                        )
                                        or ""
                                    )
                                    if docs:
                                        package_state = "PACKAGE_ACQUIRED_BIDNET"
                                    elif st == "PAID_REQUIRED":
                                        package_state = "PACKAGE_REGISTRATION_REQUIRED"
                                    elif st == "OWNER_APPROVAL_REQUIRED":
                                        package_state = "PACKAGE_PG_REGISTRATION_OWNER_REQUIRED"
                                    elif st == "PREPARED":
                                        package_state = "PACKAGE_PG_REGISTRATION_PREPARED"
                                    elif st == "REGISTERED" or (
                                        out.get("purchasing_group_registration") or {}
                                    ).get("already_registered"):
                                        package_state = (
                                            "PACKAGE_RETRYABLE"
                                            if detail_status.startswith("DETAIL")
                                            else "PACKAGE_PG_REGISTRATION_INCOMPLETE"
                                        )
                                    else:
                                        package_state = "PACKAGE_PG_REGISTRATION_INCOMPLETE"
                                else:
                                    parsed = parse_bidnet_abstract(html, detail_url=page_url) or {}
                                    # Private abstract may only expose H1 title
                                    if not parsed.get("title"):
                                        hm = re.search(r"<h1[^>]*>([^<]+)</h1>", html, re.I)
                                        if hm:
                                            parsed["title"] = hm.group(1).strip()
                                            parsed["parse_ok"] = True
                                    detail_status = (
                                        "DETAIL_OK" if parsed.get("parse_ok") or parsed.get("title") else "DETAIL_PARTIAL"
                                    )
                                    docs = [
                                        d
                                        for d in (parsed.get("documents") or [])
                                        if isinstance(d, dict)
                                    ]
                                    if docs:
                                        package_state = "PACKAGE_ACQUIRED_BIDNET"
                                    out["access_note"] = "search_to_detail_via_interception"
                    else:
                        parsed = parse_bidnet_abstract(html, detail_url=page_url) or {}
                        detail_status = "DETAIL_OK" if parsed.get("parse_ok") else "DETAIL_PARTIAL"
                        docs = [d for d in (parsed.get("documents") or []) if isinstance(d, dict)]
        except Exception as exc:
            fetch_error = f"{type(exc).__name__}:{exc}"[:160]
            detail_status = "DETAIL_FAILED_RETRYABLE"
            out["fetch_error"] = fetch_error

    # 1b) Production fix: URL/HTML PG gates must invoke registration even when
    # search-to-detail did not return ACTION_REQUIRED (statewide public links).
    if (
        not _has_downloaded_docs(docs)
        and not out.get("purchasing_group_registration")
        and time.monotonic() < deadline
        and (
            "purchasinggroupid=" in url.lower()
            or detail_status == "DETAIL_ACCESS_GATE"
            or "incomplete purchasing group" in (html or "").lower()
            or "action is required before access" in (html or "").lower()
            or "interceptorProceedBtn" in (html or "")
        )
    ):
        # Prefer navigating to private detail first so the gate HTML / proceed URL exists
        page_url = str(out.get("final_url") or url)
        if (
            hasattr(auth_client, "resolve_detail_via_authenticated_search")
            and detail_status
            in {"SEARCH_PAGE_NOT_DETAIL", "FAST_PATH", "DETAIL_PARTIAL", "DETAIL_OK", "DETAIL_FAILED_RETRYABLE"}
            and "action is required" not in (html or "").lower()
            and "incomplete purchasing group" not in (html or "").lower()
        ):
            try:
                out["network_requests"] += 1
                resolved = (
                    auth_client.resolve_detail_via_authenticated_search(
                        title=title,
                        source_url=url,
                        search_html=html or None,
                        timeout_ms=_remain_ms(18_000),
                    )
                    or {}
                )
                out["search_to_detail_pg"] = {
                    "status": resolved.get("status"),
                    "access_gate": resolved.get("access_gate"),
                    "detail_url": (resolved.get("detail_url") or "")[:160] or None,
                }
                if resolved.get("html"):
                    html = resolved["html"]
                    page_url = str(resolved.get("detail_url") or page_url)
                    out["final_url"] = page_url[:220]
            except Exception as exc:
                out["search_to_detail_pg_error"] = type(exc).__name__

        html, page_url, detail_status, docs, parsed_pg = _attempt_pg_gate_unlock(
            auth_client,
            html=html or "",
            page_url=page_url,
            source_url=url,
            title=title,
            opportunity_id=oid,
            deadline=deadline,
            remain_ms_fn=_remain_ms,
            out=out,
        )
        if parsed_pg:
            parsed = parsed_pg
        st = str((out.get("purchasing_group_registration") or {}).get("status") or "")
        if docs:
            package_state = "PACKAGE_ACQUIRED_BIDNET"
        elif st == "PAID_REQUIRED":
            package_state = "PACKAGE_REGISTRATION_REQUIRED"
        elif st == "OWNER_APPROVAL_REQUIRED":
            package_state = "PACKAGE_PG_REGISTRATION_OWNER_REQUIRED"
        elif st == "PREPARED":
            package_state = "PACKAGE_PG_REGISTRATION_PREPARED"
        elif st or out.get("purchasing_group_gate"):
            package_state = "PACKAGE_PG_REGISTRATION_INCOMPLETE"
            detail_status = detail_status or "DETAIL_ACCESS_GATE"

    # 2) Bounded path: attachment scrape for REAL private detail URLs only.
    # Statewide / open-bids IDs are a different namespace — never invent
    # /private/solicitations/{statewide_id}/ (root cause of DETAIL_FETCH_EMPTY).
    if (
        not _has_downloaded_docs(docs)
        and time.monotonic() < deadline
        and detail_status not in {"SUPPLIER_REGISTRATION_REDIRECT"}
    ):
        try:
            from bidnet_engine.package_materialization import is_bidnet_solicitation_detail_url

            fu = str(out.get("final_url") or url or "")
            candidates: list[str] = []
            # Only accept private solicitation IDs already present on final/url
            for raw in (fu, url, str(out.get("discover_url") or "")):
                m_priv = re.search(r"/private/solicitations/(\d{6,})", raw, re.I)
                if m_priv:
                    cand = (
                        f"https://www.bidnetdirect.com/private/solicitations/"
                        f"{m_priv.group(1)}/abstract?target=view"
                    )
                    if cand not in candidates:
                        candidates.append(cand)
            if url and url not in candidates and "/private/solicitations/" in url.lower():
                candidates.append(url)
            if (
                fu
                and fu not in candidates
                and "search" not in fu.lower()
                and "/private/solicitations/" in fu.lower()
            ):
                candidates.append(fu)

            # Statewide/open-bids: resolve private detail via title search first
            needs_resolve = (
                not candidates
                or "/statewide/" in url.lower()
                or "/open-bids/" in url.lower()
                or "/public/supplier/" in url.lower()
            )
            if (
                needs_resolve
                and title
                and hasattr(auth_client, "resolve_detail_via_authenticated_search")
                and time.monotonic() < deadline
            ):
                out["network_requests"] += 1
                resolved = (
                    auth_client.resolve_detail_via_authenticated_search(
                        title=title,
                        source_url=url,
                        timeout_ms=_remain_ms(35_000),
                    )
                    or {}
                )
                out["search_to_detail_docs"] = {
                    "status": resolved.get("status"),
                    "detail_url": (resolved.get("detail_url") or "")[:160] or None,
                    "solicitation_id": resolved.get("solicitation_id"),
                    "access_gate": resolved.get("access_gate"),
                }
                if (
                    resolved.get("ok")
                    and resolved.get("detail_url")
                    and resolved.get("access_gate") != "ACTION_REQUIRED"
                ):
                    priv = str(resolved["detail_url"])
                    if priv not in candidates:
                        candidates.insert(0, priv)
                    out["final_url"] = priv[:220]
                    if resolved.get("solicitation_id"):
                        out["bidnet_solicitation_id"] = resolved.get("solicitation_id")
                    if resolved.get("html"):
                        html = resolved["html"]
                        parsed = parse_bidnet_abstract(html, detail_url=priv) or parsed

            for cand in candidates:
                if not (
                    is_bidnet_solicitation_detail_url(cand)
                    or re.search(r"/private/solicitations/\d+", cand)
                ):
                    continue
                if time.monotonic() >= deadline:
                    break
                if not hasattr(auth_client, "discover_attachment_links"):
                    break
                out["network_requests"] += 1
                discovered = (
                    auth_client.discover_attachment_links(
                        cand, timeout_ms=_remain_ms(45_000)
                    )
                    or []
                )
                docs = [d for d in discovered if isinstance(d, dict) and _is_real_doc(d)]
                out["document_links_discovered"] = len(discovered)
                out["discover_url"] = cand[:220]
                if _has_downloaded_docs(docs) or docs:
                    package_state = "PACKAGE_ACQUIRED_BIDNET"
                    out["final_url"] = cand[:220]
                    detail_status = "DETAIL_OK"
                    break

            # Last resort: title / statewide helpers (still shared production client)
            if not _has_downloaded_docs(docs) and time.monotonic() < deadline:
                discovered = []
                if hasattr(auth_client, "discover_attachments_by_title") and title:
                    out["network_requests"] += 1
                    discovered = (
                        auth_client.discover_attachments_by_title(
                            title, timeout_ms=_remain_ms(40_000)
                        )
                        or []
                    )
                if (
                    not discovered
                    and hasattr(auth_client, "discover_attachments_from_statewide")
                    and (
                        "/statewide/" in url.lower()
                        or "/open-bids/" in url.lower()
                    )
                ):
                    out["network_requests"] += 1
                    discovered = (
                        auth_client.discover_attachments_from_statewide(
                            url, title=title, timeout_ms=_remain_ms(40_000)
                        )
                        or []
                    )
                docs = [d for d in discovered if isinstance(d, dict) and _is_real_doc(d)]
                out["document_links_discovered"] = max(
                    int(out.get("document_links_discovered") or 0), len(discovered)
                )
                if docs:
                    package_state = "PACKAGE_ACQUIRED_BIDNET"
                    detail_status = "DETAIL_OK"
                    out["access_note"] = "title_or_statewide_docs_recovered"
        except Exception as exc:
            out["discover_error"] = type(exc).__name__

    out["detail_status"] = detail_status
    out["package_state"] = package_state
    out["paid_entitlement_requirement"] = paid_req
    out["paid_entitlement_intercept"] = paid_intercept

    free_chase = None
    entitlement_hit = bool(paid_req) or bool(
        paid_intercept
        and (
            "subscription-management" in str(paid_intercept).lower()
            or "/intercept/plans" in str(paid_intercept).lower()
        )
    )
    if entitlement_hit:
        out["entitlement_stop"] = True
    # 3) One free/official portal chase when BidNet docs missing — never retry BidNet paywall
    remain_for_chase = deadline - time.monotonic()
    missing_downloads = not _has_downloaded_docs(docs)
    need_free = (entitlement_hit or missing_downloads) and remain_for_chase >= 12.0
    if (entitlement_hit or missing_downloads) and remain_for_chase < 12.0:
        out["free_chase_skipped"] = "insufficient_budget_remaining"
        free_chase = {"status": "PACKAGE_RECOVERY_RETRYABLE"}
        out["free_chase_status"] = "PACKAGE_RECOVERY_RETRYABLE"
    if need_free:
        out["network_requests"] += 1
        try:
            rec = dict(l23)
            rec.update(meta)
            if out.get("solicitation_from_intercept"):
                rec["solicitation_number"] = out["solicitation_from_intercept"]
                parsed["solicitation_number"] = parsed.get("solicitation_number") or out[
                    "solicitation_from_intercept"
                ]
            if parsed.get("agency_source_url"):
                rec["agency_source_url"] = parsed["agency_source_url"]
                rec["original_posting_url"] = parsed["agency_source_url"]
            if parsed.get("solicitation_number"):
                rec["solicitation_number"] = parsed["solicitation_number"]
            free_chase = chase_free_package(rec, parsed=parsed or None)
            out["free_chase_status"] = free_chase.get("status")
            free_docs = [
                d for d in (free_chase.get("documents") or []) if isinstance(d, dict) and _is_real_doc(d)
            ]
            if free_docs:
                docs = free_docs
                out["package_state"] = "PACKAGE_ACQUIRED_OFFICIAL_ALTERNATE"
                out["free_portal_recovered"] = True
        except Exception as exc:
            out["free_chase_error"] = type(exc).__name__
            free_chase = {"status": "PACKAGE_RECOVERY_RETRYABLE"}

    # Filter junk docs
    docs = [d for d in docs if isinstance(d, dict) and _is_real_doc(d)]
    out["documents"] = [
        {
            "name": d.get("document_name") or d.get("filename") or d.get("name"),
            "url": str(d.get("document_url") or d.get("url") or "")[:200] or None,
            # Preserve real path for schedule extraction / CFE — boolean alone is insufficient
            "local_path": d.get("local_path") or d.get("canonical_storage_reference"),
            "canonical_storage_reference": d.get("canonical_storage_reference") or d.get("local_path"),
            "local_path_set": bool(d.get("local_path") or d.get("canonical_storage_reference")),
            "retrieval_status": d.get("retrieval_status"),
            "source": d.get("source") or d.get("free_chase_via"),
        }
        for d in docs[:20]
    ]
    # Extract from path-bearing docs (original list), not the slim summary alone
    lines = _extract_lines(title, parsed, docs)
    out["line_items"] = lines
    out["line_count"] = len(lines)
    qty_ok = any(
        (isinstance(li.get("QTY"), dict) and li["QTY"].get("VALUE") not in (None, ""))
        or li.get("quantity") not in (None, "")
        for li in lines
        if isinstance(li, dict)
    )
    title_only = bool(
        lines
        and len(lines) == 1
        and lines[0].get("LINE_SOURCE_TYPE") == "TITLE_FALLBACK"
    )

    blocker, evidence = classify_document_blocker(
        auth_ok=True,
        auth_status="AUTHENTICATED",
        paid_requirement=str(paid_req) if paid_req else None,
        paid_intercept=str(paid_intercept) if paid_intercept else None,
        package_state=str(out.get("package_state") or ""),
        detail_status=str(out.get("detail_status") or ""),
        docs=docs,
        free_chase_status=(free_chase or {}).get("status") if free_chase else out.get("free_chase_status"),
        fetch_error=fetch_error,
        timed_out=False,
    )
    if docs and qty_ok and not title_only:
        blocker = COMPLETE_PACKAGE
        evidence["classification_basis"] = "docs_plus_qty_lines"
    evidence["final_url"] = (out.get("final_url") or "")[:220] or None
    evidence["access_note"] = out.get("access_note")
    evidence["bidnet_discovery_skipped"] = out.get("bidnet_discovery_skipped")
    evidence["entitlement_stop"] = bool(out.get("entitlement_stop"))
    evidence["free_portal_recovered"] = bool(out.get("free_portal_recovered"))
    out["primary_blocker"] = blocker
    out["blocker_evidence"] = evidence
    out["complete_spec"] = blocker == COMPLETE_PACKAGE
    out["finished_at"] = _utc()
    return out


def _timeout_row(item: dict[str, Any], timeout_s: float, t0: float, *, basis: str) -> dict[str, Any]:
    return {
        "opportunity_id": item.get("opportunity_id"),
        "title": item.get("title"),
        "buyer": item.get("buyer"),
        "deadline": item.get("deadline"),
        "authoritative_url": item.get("authoritative_url"),
        "primary_blocker": PER_OPP_TIMEOUT,
        "blocker_evidence": {
            "timed_out": True,
            "timeout_s": timeout_s,
            "classification_basis": basis,
        },
        "documents": [],
        "line_items": [],
        "complete_spec": False,
        "timed_out": True,
        "elapsed_s": round(time.time() - t0, 2),
        "finished_at": _utc(),
        "NOTE": "Timed out; worker/browser killed; no further BidNet retries for this item",
    }


def _process_worker(
    q: Any,
    item: dict[str, Any],
    l23: dict[str, Any],
    timeout_s: float,
    data_root: str | None,
) -> None:
    """Child process: own Playwright lifecycle; must be picklable at module scope."""
    if data_root:
        os.environ["M3_DATA_ROOT"] = data_root
    client = None
    try:
        from bidnet_auth.client import BidNetAuthenticatedClient
        from bidnet_auth.config import load_bidnet_auth_config

        cfg = load_bidnet_auth_config()
        if not cfg.credentials_present:
            q.put(
                {
                    "opportunity_id": item.get("opportunity_id"),
                    "primary_blocker": AUTH_FAILURE,
                    "blocker_evidence": {"classification_basis": "no_credentials_in_child"},
                    "complete_spec": False,
                    "timed_out": False,
                }
            )
            return
        client = BidNetAuthenticatedClient(cfg)
        auth = client.ensure_authenticated()
        if not auth.authenticated:
            q.put(
                {
                    "opportunity_id": item.get("opportunity_id"),
                    "title": item.get("title"),
                    "buyer": item.get("buyer"),
                    "primary_blocker": AUTH_FAILURE,
                    "blocker_evidence": {
                        "auth_status": auth.status,
                        "message": str(auth.message or "")[:160],
                        "classification_basis": "child_auth_failed",
                    },
                    "complete_spec": False,
                    "timed_out": False,
                }
            )
            return
        # Inner soft deadline slightly under parent kill window
        row = _recover_one_inner(
            item,
            auth_client=client,
            l23=l23,
            timeout_s=max(5.0, float(timeout_s) - 1.0),
        )
        q.put(row)
    except Exception as exc:
        q.put(
            {
                "opportunity_id": item.get("opportunity_id"),
                "title": item.get("title"),
                "buyer": item.get("buyer"),
                "primary_blocker": DOCS_INACCESSIBLE,
                "blocker_evidence": {
                    "error": type(exc).__name__,
                    "message": str(exc)[:160],
                    "classification_basis": "child_exception",
                },
                "documents": [],
                "line_items": [],
                "complete_spec": False,
                "timed_out": False,
            }
        )
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


def recover_one_bounded(
    item: dict[str, Any],
    *,
    auth_client: Any = None,
    l23: dict[str, Any],
    timeout_s: float = ITEM_TIMEOUT_S,
    isolate_process: bool = True,
) -> dict[str, Any]:
    """Hard ≤45s wall clock. Default: spawn a child process so timeout can kill Playwright.

    Pass isolate_process=False only for unit tests with a mocked client.
    """
    t0 = time.time()
    timeout_s = float(timeout_s)

    if not isolate_process:
        if auth_client is None:
            return {
                "opportunity_id": item.get("opportunity_id"),
                "primary_blocker": AUTH_FAILURE,
                "blocker_evidence": {"classification_basis": "no_client_inline"},
                "complete_spec": False,
                "timed_out": False,
                "elapsed_s": 0.0,
            }
        timed_out = {"flag": False}

        def _watchdog() -> None:
            timed_out["flag"] = True
            try:
                auth_client.close()
            except Exception:
                pass

        timer = threading.Timer(timeout_s, _watchdog)
        timer.daemon = True
        timer.start()
        try:
            row = _recover_one_inner(
                item, auth_client=auth_client, l23=l23, timeout_s=timeout_s
            )
            row["elapsed_s"] = round(time.time() - t0, 2)
            row["timed_out"] = bool(timed_out["flag"])
            if timed_out["flag"]:
                row["primary_blocker"] = PER_OPP_TIMEOUT
            return row
        except Exception as exc:
            if timed_out["flag"]:
                return _timeout_row(item, timeout_s, t0, basis="inline_watchdog")
            return {
                "opportunity_id": item.get("opportunity_id"),
                "primary_blocker": DOCS_INACCESSIBLE,
                "blocker_evidence": {
                    "error": type(exc).__name__,
                    "message": str(exc)[:160],
                    "classification_basis": "exception",
                },
                "complete_spec": False,
                "timed_out": False,
                "elapsed_s": round(time.time() - t0, 2),
            }
        finally:
            timer.cancel()

    # Process isolation — parent can hard-kill hung Chromium/Playwright
    ctx = mp.get_context("spawn")
    q: mp.Queue = ctx.Queue()
    data_root = (os.environ.get("M3_DATA_ROOT") or "").strip() or None
    proc = ctx.Process(
        target=_process_worker,
        args=(q, dict(item), dict(l23 or {}), timeout_s, data_root),
        daemon=True,
    )
    proc.start()
    proc.join(timeout_s + 3.0)
    if proc.is_alive():
        try:
            proc.kill()
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
        try:
            proc.join(5.0)
        except Exception:
            pass
        return _timeout_row(item, timeout_s, t0, basis="process_kill")
    try:
        row = q.get_nowait()
    except Exception:
        return _timeout_row(item, timeout_s, t0, basis="process_no_result")
    if not isinstance(row, dict):
        return _timeout_row(item, timeout_s, t0, basis="process_bad_result")
    row["elapsed_s"] = round(time.time() - t0, 2)
    row.setdefault("timed_out", False)
    return row


def run_bounded_document_recovery_10(
    *,
    n: int = 10,
    timeout_s: float = ITEM_TIMEOUT_S,
    resume: bool = False,
) -> dict[str, Any]:
    lock = _acquire_lock()
    if not lock:
        return {"ok": False, "error": "run_lock_held"}
    t0 = time.time()
    auth_client = None
    try:
        from bidnet_auth.client import BidNetAuthenticatedClient
        from bidnet_auth.config import auth_credential_source, load_bidnet_auth_config
        from phase_l.l23_full_population_funnel import load_store

        man = _load(MANIFEST) if resume else None
        if not man or not man.get("rows"):
            man = select_bounded_10(n=n)
        selected = man.get("rows") or []
        store = load_store()

        cfg = load_bidnet_auth_config()
        cred = auth_credential_source()
        auth_meta = {
            "credentials_present": cfg.credentials_present,
            "credential_source": cred.get("credential_source"),
            "auth_enabled": cfg.auth_enabled,
        }
        if not cfg.credentials_present:
            report = {
                "ok": False,
                "error": "NO_CREDENTIALS_IN_PROCESS",
                "hint": "Run via railway run so BIDNET_* env is injected",
                "auth_meta": auth_meta,
            }
            _save(REPORT, report)
            return report

        # Smoke-auth once in parent (session state file), then close — each opp runs
        # in an isolated child process so a hung Playwright can be hard-killed ≤45s.
        auth_client = BidNetAuthenticatedClient(cfg)
        try:
            auth = auth_client.ensure_authenticated()
        except Exception as exc:
            report = {
                "ok": False,
                "error": "AUTH_EXCEPTION",
                "auth_meta": {**auth_meta, "exception": type(exc).__name__, "message": str(exc)[:160]},
            }
            _save(REPORT, report)
            return report
        auth_meta.update(
            {
                "authenticated": bool(auth.authenticated),
                "status": auth.status,
                "message": str(auth.message or "")[:160],
                "session_reused": bool(getattr(auth, "reused_session", False)),
                "isolation": "process_per_opportunity",
            }
        )
        try:
            auth_client.close()
        except Exception:
            pass
        auth_client = None
        if not auth.authenticated:
            report = {"ok": False, "error": "AUTH_FAILED", "auth_meta": auth_meta}
            _save(REPORT, report)
            return report

        results: list[dict[str, Any]] = []
        _progress(phase="EXECUTE", status="RUNNING", selected=len(selected), completed=0, auth=auth_meta)

        for item in selected:
            oid = item["opportunity_id"]
            row = recover_one_bounded(
                item,
                auth_client=None,
                l23=store.get(oid) or {},
                timeout_s=timeout_s,
                isolate_process=True,
            )
            results.append(row)
            _save(RESULTS, {"build": BUILD, "count": len(results), "rows": results})
            _progress(
                phase="EXECUTE",
                completed=len(results),
                selected=len(selected),
                last_oid=oid,
                last_blocker=row.get("primary_blocker"),
                last_elapsed_s=row.get("elapsed_s"),
            )

        blockers = Counter(r.get("primary_blocker") or "UNKNOWN" for r in results)
        report = {
            "kind": "BoundedDocRecoveryReport",
            "build": BUILD,
            "run_id": man.get("run_id"),
            "built_at": _utc(),
            "elapsed_s": round(time.time() - t0, 2),
            "item_timeout_s": timeout_s,
            "auth_meta": auth_meta,
            "AUTO_SEND": False,
            "openai_spend_usd": 0,
            "sam_api_calls": sum(
                1
                for r in results
                if (r.get("official_hit") or {}).get("credits_spent")
                or r.get("sam_alternate_state")
            ),
            "metrics": {
                "opportunities_tested": len(results),
                "complete_packages": blockers.get(COMPLETE_PACKAGE, 0),
                "paid_entitlement": blockers.get(PAID_ENTITLEMENT, 0),
                "auth_failure": blockers.get(AUTH_FAILURE, 0),
                "waf_or_bot": blockers.get(WAF_OR_BOT, 0),
                "missing_doc_links": blockers.get(MISSING_DOC_LINKS, 0),
                "docs_inaccessible": blockers.get(DOCS_INACCESSIBLE, 0),
                "free_portal_unavailable": blockers.get(FREE_PORTAL_UNAVAILABLE, 0),
                "timeouts": blockers.get(PER_OPP_TIMEOUT, 0),
                "documents_recovered_total": sum(len(r.get("documents") or []) for r in results),
                "complete_specs": sum(1 for r in results if r.get("complete_spec")),
            },
            "blockers": dict(blockers),
            "rows": [
                {
                    "opportunity_id": r.get("opportunity_id"),
                    "title": r.get("title"),
                    "buyer": r.get("buyer"),
                    "primary_blocker": r.get("primary_blocker"),
                    "paid_entitlement_requirement": r.get("paid_entitlement_requirement"),
                    "documents": len(r.get("documents") or []),
                    "line_count": r.get("line_count") or len(r.get("line_items") or []),
                    "complete_spec": r.get("complete_spec"),
                    "elapsed_s": r.get("elapsed_s"),
                    "timed_out": r.get("timed_out"),
                    "free_chase_status": r.get("free_chase_status"),
                    "cache_real_docs": (r.get("cache") or {}).get("cached_real_docs"),
                    "final_url": r.get("final_url"),
                    "access_note": r.get("access_note"),
                    "classification_basis": (r.get("blocker_evidence") or {}).get("classification_basis"),
                    "entitlement_stop": r.get("entitlement_stop"),
                }
                for r in results
            ],
            "acceptance": {
                "complete_package_recovered": blockers.get(COMPLETE_PACKAGE, 0) >= 1,
                "all_failures_classified": all(r.get("primary_blocker") for r in results),
            },
        }
        _save(REPORT, report)
        _progress(phase="DONE", status="COMPLETE", completed=len(results))
        return {"ok": True, "final_status": "COMPLETE", **report}
    finally:
        if auth_client is not None:
            try:
                auth_client.close()
            except Exception:
                pass
        _release_lock(lock)
