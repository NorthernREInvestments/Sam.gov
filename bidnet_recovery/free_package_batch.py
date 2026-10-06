"""Batch free-package recovery for BidNet AUTH_REQUIRED backlog → economics.

No BidNet membership. Staged batches with checkpoint/heartbeat.
FREE_PACKAGE_FOUND flows into line-item extract → profit_first (evidence only).
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from typing import Any, Callable
from uuid import uuid4

from application_clock import now_utc
from bidnet_recovery.free_package_chase import (
    FREE_PACKAGE_FOUND,
    PACKAGE_MATCH_AMBIGUOUS,
    PACKAGE_RECOVERY_RETRYABLE,
    PACKAGE_UNAVAILABLE_FREE,
    PARTIAL_FREE_PACKAGE_FOUND,
    VALID_FREE_PACKAGE_FOUND,
    should_skip_unchanged,
)

_PACKAGE_SUCCESS = {FREE_PACKAGE_FOUND, VALID_FREE_PACKAGE_FOUND, PARTIAL_FREE_PACKAGE_FOUND}
from bidnet_recovery.recover import (
    _chase_free_package_or_dead,
    detail_url_for,
    is_bidnet_rec,
)
from bidnet_recovery.states import (
    AUTH_REQUIRED,
    DOCUMENTS_NOT_AVAILABLE,
    DOCUMENTS_RECOVERED,
    ECONOMICS_READY,
    GOV_VALUE_FOUND,
    PRODUCT_IDENTIFIED,
    PUBLIC_COST_FOUND,
)
from universe_pass.classify import ELIGIBLE_FOR_PROFIT

log = logging.getLogger("govtracker.bidnet_recovery.free_package_batch")

CHECKPOINT = "m3_bidnet_free_package_checkpoint.json"
REPORT = "m3_bidnet_free_package_last_report.json"


def _paths():
    from m3_data_root import data_path

    return data_path(CHECKPOINT), data_path(REPORT)


def _load_ck() -> dict[str, Any]:
    path, _ = _paths()
    if not path.exists():
        return {"kind": "BidNetFreePackageCheckpoint", "processed_ids": [], "stats": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"kind": "BidNetFreePackageCheckpoint", "processed_ids": [], "stats": {}}


def _save_ck(payload: dict[str, Any]) -> None:
    path, _ = _paths()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _save_report(payload: dict[str, Any]) -> None:
    _, path = _paths()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _is_product_eligible(rec: dict[str, Any]) -> bool:
    uc = str(rec.get("universe_class") or rec.get("product_service_classification") or "")
    if uc in ELIGIBLE_FOR_PROFIT:
        return True
    if rec.get("eligible_for_profit_research"):
        return True
    # Prefer tangible / mixed; skip pure service / construction
    if uc in {"PURE_SERVICE", "CONSTRUCTION"}:
        return False
    return uc in {"", "UNKNOWN", "TANGIBLE_PRODUCT", "MIXED_PRODUCT_SERVICE"}


def _needs_free_package(rec: dict[str, Any], *, universe_mode: bool = False) -> bool:
    br = rec.get("bidnet_recovery") if isinstance(rec.get("bidnet_recovery"), dict) else {}
    blockers = set(br.get("blockers") or [])
    chase = br.get("free_package_chase") if isinstance(br.get("free_package_chase"), dict) else {}
    status = str(chase.get("status") or "")

    if status in _PACKAGE_SUCCESS:
        return False
    if should_skip_unchanged(rec):
        return False

    # Already have non-abstract free docs
    atts = rec.get("attachments_metadata") or []
    real = [
        d
        for d in atts
        if isinstance(d, dict)
        and (d.get("document_url") or d.get("url"))
        and "abstract" not in str(d.get("document_url") or d.get("url") or "").lower()
        and "bidnet" not in str(d.get("document_url") or d.get("url") or "").lower()
    ]
    if real:
        return False

    # Full-universe sweep: chase every product BidNet without a conclusive free package
    if universe_mode:
        if status == PACKAGE_UNAVAILABLE_FREE:
            return False
        return True

    # Primary backlog: membership-walled package access
    if AUTH_REQUIRED in blockers or DOCUMENTS_NOT_AVAILABLE in blockers:
        return True
    # Prior free-chase left retryable/ambiguous — still eligible
    if status in {PACKAGE_RECOVERY_RETRYABLE, PACKAGE_MATCH_AMBIGUOUS}:
        return True
    return False


def _priority_score(rec: dict[str, Any]) -> tuple:
    """Lower tuple = higher priority."""
    from bidnet_recovery.free_package_chase import _entity_candidates, _overview_text

    br = rec.get("bidnet_recovery") if isinstance(rec.get("bidnet_recovery"), dict) else {}
    title = str(rec.get("title") or "")
    sol = rec.get("solicitation_event_id") or rec.get("solicitation_number") or br.get("solicitation_number")
    buyer = str(rec.get("buyer") or "")
    overview = _overview_text(rec, None)
    entities = _entity_candidates(title, buyer, overview)
    real_entity = any(
        e.lower()
        not in {
            "california",
            "missouri",
            "virginia",
            "arizona",
            "texas",
            "florida",
            "ohio",
            "pennsylvania",
            "new york",
            "local",
            "unknown",
        }
        and len(e) > 8
        and " " in e
        for e in entities
    )
    # Deprioritize GSA schedules / auctions / pure service-ish titles
    junk = 1 if re.search(
        r"\b(GSA\s+Schedule|Multiple\s+Award\s+Schedule|auction|for\s+sale|as-needed\s+for\s+boiler)\b",
        title,
        re.I,
    ) else 0
    multi = 0 if re_search_multi(title) else 1
    has_sol = 0 if sol else 1
    clear_buyer = 0 if real_entity else (0 if buyer and buyer.lower() not in {
        "california", "missouri", "virginia", "arizona", "texas", "florida", "local", "unknown"
    } else 1)
    has_overview_entity = 0 if real_entity else 1
    product_clue = 0 if re_search_part(title) else 1
    dl = rec.get("deadline") or br.get("deadline_raw")
    soon = 2
    if dl:
        try:
            from datetime import datetime, timezone

            dts = str(dl).replace("Z", "+00:00")
            dt = datetime.fromisoformat(dts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            hours = (now_utc() - dt).total_seconds() / -3600.0
            if 0 < hours <= 72:
                soon = 0
            elif 0 < hours <= 24 * 14:
                soon = 1
        except Exception:
            soon = 2
    history = 0 if "amend" in title.lower() or "addendum" in title.lower() else 1
    return (
        junk,
        has_overview_entity,
        multi,
        has_sol,
        clear_buyer,
        product_clue,
        soon,
        history,
        str(rec.get("canonical_id") or ""),
    )


def re_search_part(blob: str) -> bool:
    import re

    return bool(
        re.search(
            r"\b(NSN|P/?N|MPN|model|part\s*#|SKU|catalog|OEM|brand[\-\s]?name|"
            r"equipment|supply|supplies|materials?|valve|pump|hose|wire|cable)\b",
            blob or "",
            re.I,
        )
    )


def re_search_multi(blob: str) -> bool:
    import re

    return bool(
        re.search(
            r"\b(line\s*items?|bid\s*sheet|schedule|CLINs?|multiple\s+items?|item\s+list|"
            r"pricing\s+sheet|unit\s+price)\b",
            blob or "",
            re.I,
        )
    )


def select_free_package_backlog(
    store: dict[str, Any],
    *,
    limit: int,
    skip_ids: set[str] | None = None,
    universe_mode: bool = False,
) -> list[tuple[str, dict[str, Any]]]:
    skip_ids = skip_ids or set()
    cands: list[tuple[tuple, str, dict[str, Any]]] = []
    for cid, rec in store.items():
        if not isinstance(rec, dict) or not is_bidnet_rec(rec):
            continue
        if cid in skip_ids:
            continue
        if not _is_product_eligible(rec):
            continue
        if not _needs_free_package(rec, universe_mode=universe_mode):
            continue
        cands.append((_priority_score(rec), cid, rec))
    cands.sort(key=lambda x: x[0], reverse=bool(universe_mode))
    return [(cid, rec) for _, cid, rec in cands[: max(0, int(limit))]]


def _ingest_free_documents(cid: str, rec: dict[str, Any]) -> str:
    """Download free package files when possible; return extracted text for line items."""
    import httpx
    from m3_data_root import data_path

    texts: list[str] = []
    atts = rec.get("attachments_metadata") if isinstance(rec.get("attachments_metadata"), list) else []
    out_dir = data_path("bidnet_free_package", "documents", cid[:16])
    out_dir.mkdir(parents=True, exist_ok=True)
    with httpx.Client(
        timeout=httpx.Timeout(45.0, connect=10.0),
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0"},
    ) as client:
        for d in atts:
            if not isinstance(d, dict) or not d.get("free_chase"):
                continue
            u = str(d.get("document_url") or d.get("url") or "")
            if not u.startswith("http"):
                continue
            try:
                r = client.get(u)
                if r.status_code >= 400 or not r.content:
                    continue
                name = str(d.get("document_name") or u.rsplit("/", 1)[-1] or "doc.bin")
                safe = "".join(c if c.isalnum() or c in "._-+" else "_" for c in name)[:140]
                path = out_dir / safe
                path.write_bytes(r.content)
                d["local_path"] = str(path)
                d["byte_size"] = len(r.content)
                d["retrieval_status"] = "DOWNLOADED"
                low = name.lower()
                raw = r.content
                if low.endswith(".pdf") or (r.headers.get("content-type") or "").lower().find("pdf") >= 0:
                    try:
                        import fitz  # PyMuPDF

                        doc = fitz.open(stream=raw, filetype="pdf")
                        page_text = []
                        for i, page in enumerate(doc):
                            if i >= 40:
                                break
                            page_text.append(page.get_text("text") or "")
                        texts.append("\n".join(page_text))
                    except Exception:
                        pass
                elif low.endswith((".csv", ".txt")):
                    texts.append(raw.decode("utf-8", errors="ignore")[:200000])
            except Exception:
                continue
    return "\n\n".join(t for t in texts if t and t.strip())[:400000]


def _run_economics_pipeline(cid: str, rec: dict[str, Any]) -> dict[str, Any]:
    """Line-item extract → profit_first. Evidence only; never invent prices."""
    out: dict[str, Any] = {
        "line_items": 0,
        "gov_value": False,
        "public_cost": False,
        "both_sides": False,
        "economics_ready": False,
        "profit_status": None,
        "expected_profit": None,
    }
    extracted_text = ""
    try:
        extracted_text = _ingest_free_documents(cid, rec)
    except Exception as exc:
        out["ingest_error"] = type(exc).__name__
    body_bits = [
        str(rec.get("title") or ""),
        str(rec.get("description") or ""),
        extracted_text,
    ]
    for d in rec.get("attachments_metadata") or []:
        if isinstance(d, dict):
            body_bits.append(str(d.get("document_name") or ""))
    body_text = "\n".join(body_bits)

    try:
        from line_item_economics.engine import analyze_line_item_economics

        lie = analyze_line_item_economics(
            opportunity_id=cid,
            title=rec.get("title"),
            buyer=rec.get("buyer"),
            body_text=body_text,
            csv_text=extracted_text if extracted_text.count(",") > 10 else None,
            persist=True,
        )
        lines = lie.get("lines") or []
        out["line_items"] = len(lines)
        rec["line_item_economics"] = {
            "line_count": len(lines),
            "analyzed_at": now_utc().isoformat(),
            "status": lie.get("status") or lie.get("analysis_status"),
        }
        if lines:
            rec["universe_class"] = rec.get("universe_class") or "TANGIBLE_PRODUCT"
            br = rec.setdefault("bidnet_recovery", {})
            if isinstance(br, dict) and br.get("state") not in {
                GOV_VALUE_FOUND,
                PUBLIC_COST_FOUND,
                ECONOMICS_READY,
            }:
                br["state"] = PRODUCT_IDENTIFIED
    except Exception as exc:
        out["line_item_error"] = type(exc).__name__
        log.info("line_item extract failed %s: %s", cid, type(exc).__name__)

    try:
        from profit_first.router import evaluate_opportunity_profit
        from line_item_economics.engine import load_analysis

        lie = load_analysis(cid)
        pev = evaluate_opportunity_profit(
            opportunity_id=cid,
            rec=rec,
            title=rec.get("title"),
            buyer=rec.get("buyer"),
            line_item_analysis=lie,
            ranking_signals={"free_package": True},
        )
        econ = pev.get("economics") or {}
        rec["profit_first"] = {
            "profit_status": econ.get("profit_status"),
            "expected_profit": econ.get("expected_profit"),
            "post_financing_profit": econ.get("post_financing_profit"),
            "route": pev.get("route"),
            "owner_card": pev.get("owner_card"),
            "proof_signals": econ.get("proof_signals"),
            "research_priority": (pev.get("research") or {}).get("priority"),
            "evaluated_at": pev.get("evaluated_at"),
            "bidnet_free_package": True,
        }
        out["profit_status"] = econ.get("profit_status")
        out["expected_profit"] = econ.get("expected_profit")
        gov = econ.get("expected_revenue") or econ.get("historical_award_total")
        cost = econ.get("acquisition_cost") or econ.get("public_retail_total")
        out["gov_value"] = gov is not None
        out["public_cost"] = cost is not None
        out["both_sides"] = gov is not None and cost is not None
        br = rec.setdefault("bidnet_recovery", {})
        if isinstance(br, dict):
            if out["both_sides"]:
                br["state"] = ECONOMICS_READY
                out["economics_ready"] = True
            elif out["public_cost"] and out["gov_value"]:
                br["state"] = ECONOMICS_READY
                out["economics_ready"] = True
            elif out["gov_value"]:
                br["state"] = GOV_VALUE_FOUND
            elif out["public_cost"]:
                br["state"] = PUBLIC_COST_FOUND
            elif (rec.get("attachments_metadata") or []) and br.get("state") != PRODUCT_IDENTIFIED:
                br["state"] = DOCUMENTS_RECOVERED
        pe = rec.setdefault("product_economics", {})
        if isinstance(pe, dict):
            if gov is not None:
                pe["gov_value"] = gov
            if cost is not None:
                pe["acquisition_cost"] = cost
            pe["both_sides_known"] = bool(out["both_sides"])
    except Exception as exc:
        out["profit_error"] = type(exc).__name__
        log.info("profit_first failed %s: %s", cid, type(exc).__name__)
    return out


def run_free_package_backlog(
    *,
    limit: int = 500,
    persist: bool = True,
    resume: bool = True,
    force: bool = False,
    run_id: str | None = None,
    on_progress: Callable[..., None] | None = None,
    stop_if_yield_below: float | None = 0.005,
    min_attempted_for_yield_gate: int = 40,
    universe_mode: bool = False,
) -> dict[str, Any]:
    """Process BidNet AUTH_REQUIRED free-package backlog in a bounded batch."""
    from phase_l.l23_full_population_funnel import load_store, save_store

    run_id = run_id or f"FPR-{uuid4().hex[:12]}"
    started = now_utc().isoformat()
    store = load_store()
    ck = _load_ck() if resume else {"kind": "BidNetFreePackageCheckpoint", "processed_ids": [], "stats": {}}
    done = set(ck.get("processed_ids") or []) if resume and not force else set()

    selected = select_free_package_backlog(
        store,
        limit=limit,
        skip_ids=done if not force else set(),
        universe_mode=universe_mode,
    )
    eligible_n = len(
        select_free_package_backlog(store, limit=10**9, skip_ids=set(), universe_mode=universe_mode)
    )

    stats: Counter = Counter()
    samples: list[dict[str, Any]] = []
    processed_ids: list[str] = list(done)

    def _progress(phase: str, pct: int, **extra: Any) -> None:
        if on_progress:
            try:
                on_progress(phase=phase, pct=pct, **extra)
            except Exception:
                pass

    _progress("FREE_PACKAGE_BATCH", 5, retrieved=len(selected), eligible=eligible_n)

    for i, (cid, rec) in enumerate(selected):
        stats["attempted"] += 1
        recovery = dict(rec.get("bidnet_recovery") or {})
        blockers = list(recovery.get("blockers") or [])
        if AUTH_REQUIRED not in blockers:
            blockers.append(AUTH_REQUIRED)

        docs, blockers, status = _chase_free_package_or_dead(
            rec, recovery, blockers=blockers, store=store
        )
        recovery["blockers"] = blockers
        recovery["blocker"] = blockers[0] if blockers else recovery.get("blocker")
        recovery["last_recovery_attempt"] = now_utc().isoformat()
        recovery["membership_used"] = False

        # Document quality gate — reject non-solicitation PDFs before economics
        package_quality: dict[str, Any] = {}
        if status in _PACKAGE_SUCCESS and docs:
            try:
                from document_quality import (
                    PARTIAL_SOLICITATION_PACKAGE,
                    UNRELATED_DOCUMENTS,
                    VALID_SOLICITATION_PACKAGE,
                    classify_package_documents,
                    extract_pdf_text,
                )

                scored_docs = []
                for d in docs:
                    if not isinstance(d, dict):
                        continue
                    text = ""
                    lp = d.get("local_path")
                    if lp:
                        text = extract_pdf_text(str(lp))
                    scored_docs.append(
                        {
                            "document_name": d.get("document_name") or d.get("name") or "",
                            "text": text,
                        }
                    )
                package_quality = classify_package_documents(
                    scored_docs,
                    title=rec.get("title"),
                    buyer=rec.get("buyer"),
                    solicitation_number=rec.get("solicitation_number")
                    or recovery.get("solicitation_number"),
                )
                recovery["package_quality"] = package_quality
                pq = str(package_quality.get("package_quality") or "")
                chase = recovery.get("free_package_chase") if isinstance(recovery.get("free_package_chase"), dict) else {}
                chase = dict(chase)
                if pq == VALID_SOLICITATION_PACKAGE:
                    status = VALID_FREE_PACKAGE_FOUND
                    chase["status"] = VALID_FREE_PACKAGE_FOUND
                elif pq == PARTIAL_SOLICITATION_PACKAGE:
                    status = PARTIAL_FREE_PACKAGE_FOUND
                    chase["status"] = PARTIAL_FREE_PACKAGE_FOUND
                elif not package_quality.get("usable"):
                    stats["FALSE_PACKAGE_MATCH"] += 1
                    if pq == UNRELATED_DOCUMENTS:
                        stats["UNRELATED_DOCUMENTS"] += 1
                    chase["status"] = PACKAGE_MATCH_AMBIGUOUS
                    chase["false_package"] = True
                    chase["package_quality"] = pq
                    status = PACKAGE_MATCH_AMBIGUOUS
                recovery["free_package_chase"] = chase
            except Exception as exc:
                package_quality = {"error": type(exc).__name__}
                recovery["package_quality"] = package_quality

        stats[str(status)] += 1

        econ_out: dict[str, Any] = {}
        if status in _PACKAGE_SUCCESS and docs:
            recovery["state"] = DOCUMENTS_RECOVERED
            econ_out = _run_economics_pipeline(cid, rec)
            stats["documents_recovered_ops"] += 1
            stats["documents_total"] += len(docs)
            stats["line_item_opportunities"] += 1 if econ_out.get("line_items") else 0
            if econ_out.get("gov_value"):
                stats["gov_value_known"] += 1
            if econ_out.get("public_cost"):
                stats["public_cost_known"] += 1
            if econ_out.get("both_sides"):
                stats["both_sides_known"] += 1
            if econ_out.get("economics_ready"):
                stats["economics_ready"] += 1
            ps = str(econ_out.get("profit_status") or "")
            if ps:
                stats[f"profit_{ps}"] += 1
            ep = econ_out.get("expected_profit")
            try:
                if ep is not None and float(ep) > 0:
                    stats["profitable"] += 1
            except Exception:
                pass

        rec["bidnet_recovery"] = recovery
        store[cid] = rec
        processed_ids.append(cid)

        if len(samples) < 25:
            chase = recovery.get("free_package_chase") or {}
            samples.append(
                {
                    "id": cid[:16],
                    "title": str(rec.get("title") or "")[:80],
                    "status": status,
                    "docs": len(docs),
                    "route": chase.get("recovery_route"),
                    "confidence": chase.get("confidence"),
                    "profit_status": econ_out.get("profit_status"),
                }
            )

        if persist and (i + 1) % (500 if universe_mode else 25) == 0:
            save_store(store)
            _save_ck(
                {
                    "kind": "BidNetFreePackageCheckpoint",
                    "run_id": run_id,
                    "processed_ids": processed_ids[-100000:],
                    "stats": dict(stats),
                    "updated_at": now_utc().isoformat(),
                }
            )

        pct = 5 + int(90 * (i + 1) / max(1, len(selected)))
        progress_every = 100 if universe_mode else 10
        if (i + 1) % progress_every == 0 or (i + 1) == len(selected):
            _progress(
                "FREE_PACKAGE_BATCH",
                pct,
                retrieved=i + 1,
                found=stats.get(FREE_PACKAGE_FOUND, 0),
                dead=stats.get(PACKAGE_UNAVAILABLE_FREE, 0),
                retryable=stats.get(PACKAGE_RECOVERY_RETRYABLE, 0),
            )

        # Yield gate after warm-up
        attempted = stats["attempted"]
        found = stats.get(FREE_PACKAGE_FOUND, 0)
        if (
            stop_if_yield_below is not None
            and attempted >= min_attempted_for_yield_gate
            and attempted in {min_attempted_for_yield_gate, 100, 200}
        ):
            yld = found / attempted
            if yld < stop_if_yield_below and found == 0:
                stats["stopped_low_yield"] = 1
                log.warning(
                    "Free-package yield near zero after %s attempts — stopping scale for debug",
                    attempted,
                )
                break

    if persist:
        save_store(store)
        _save_ck(
            {
                "kind": "BidNetFreePackageCheckpoint",
                "run_id": run_id,
                "processed_ids": processed_ids[-100000:],
                "stats": dict(stats),
                "updated_at": now_utc().isoformat(),
            }
        )

    attempted = int(stats.get("attempted") or 0)
    found = int(stats.get(FREE_PACKAGE_FOUND) or 0)
    report = {
        "kind": "BidNetFreePackageBatchReport",
        "run_id": run_id,
        "started_at": started,
        "completed_at": now_utc().isoformat(),
        "eligible_backlog": eligible_n,
        "attempted": attempted,
        "FREE_PACKAGE_FOUND": found,
        "PACKAGE_UNAVAILABLE_FREE": int(stats.get(PACKAGE_UNAVAILABLE_FREE) or 0),
        "PACKAGE_RECOVERY_RETRYABLE": int(stats.get(PACKAGE_RECOVERY_RETRYABLE) or 0),
        "PACKAGE_MATCH_AMBIGUOUS": int(stats.get(PACKAGE_MATCH_AMBIGUOUS) or 0),
        "documents_recovered_ops": int(stats.get("documents_recovered_ops") or 0),
        "documents_total": int(stats.get("documents_total") or 0),
        "line_item_opportunities": int(stats.get("line_item_opportunities") or 0),
        "gov_value_known": int(stats.get("gov_value_known") or 0),
        "public_cost_known": int(stats.get("public_cost_known") or 0),
        "both_sides_known": int(stats.get("both_sides_known") or 0),
        "economics_ready": int(stats.get("economics_ready") or 0),
        "profitable": int(stats.get("profitable") or 0),
        "free_package_yield": round(found / attempted, 4) if attempted else 0.0,
        "stopped_low_yield": bool(stats.get("stopped_low_yield")),
        "universe_mode": bool(universe_mode),
        "FALSE_PACKAGE_MATCH": int(stats.get("FALSE_PACKAGE_MATCH") or 0),
        "UNRELATED_DOCUMENTS": int(stats.get("UNRELATED_DOCUMENTS") or 0),
        "samples": samples,
        "stats": dict(stats),
        "note": "BidNet membership never used. Dead only on PACKAGE_UNAVAILABLE_FREE.",
    }
    _save_report(report)
    _progress("DONE", 100, retrieved=attempted, found=found)
    return report


def free_package_funnel_report(store: dict[str, Any] | None = None) -> dict[str, Any]:
    """Full funnel + yield report after free-package chase."""
    from phase_l.l23_full_population_funnel import available_count, load_store

    store = store if store is not None else load_store()
    status_c: Counter = Counter()
    doc_ops = 0
    doc_total = 0
    pricing = 0
    specs = 0
    item_lists = 0
    tabs = 0
    product_id = 0
    exact_model = 0
    gov = 0
    cost = 0
    both = 0
    econ_ready = 0
    profit_c: Counter = Counter()
    profit_buckets = Counter()
    eligible = 0
    auth_backlog = 0

    for cid, rec in store.items():
        if not isinstance(rec, dict) or not is_bidnet_rec(rec):
            continue
        if not _is_product_eligible(rec):
            continue
        eligible += 1
        br = rec.get("bidnet_recovery") if isinstance(rec.get("bidnet_recovery"), dict) else {}
        chase = br.get("free_package_chase") if isinstance(br.get("free_package_chase"), dict) else {}
        st = str(chase.get("status") or "")
        if st:
            status_c[st] += 1
        blockers = set(br.get("blockers") or [])
        if AUTH_REQUIRED in blockers or DOCUMENTS_NOT_AVAILABLE in blockers:
            if st not in _PACKAGE_SUCCESS:
                auth_backlog += 1

        atts = [
            d
            for d in (rec.get("attachments_metadata") or [])
            if isinstance(d, dict) and (d.get("document_url") or d.get("url"))
            and "abstract" not in str(d.get("document_url") or d.get("url") or "").lower()
        ]
        if atts:
            doc_ops += 1
            doc_total += len(atts)
            for d in atts:
                name = str(d.get("document_name") or d.get("document_url") or "").lower()
                if any(x in name for x in ("price", "pricing", "bid sheet", "schedule", "xlsx", "csv")):
                    pricing += 1
                if any(x in name for x in ("spec", "specification")):
                    specs += 1
                if any(x in name for x in ("item list", "line item", "clin", "bom")):
                    item_lists += 1
                if any(x in name for x in ("tabulation", "bid tab", "award tab", "award")):
                    tabs += 1

        if br.get("state") in {
            PRODUCT_IDENTIFIED,
            GOV_VALUE_FOUND,
            PUBLIC_COST_FOUND,
            ECONOMICS_READY,
            DOCUMENTS_RECOVERED,
        } or (atts and re_search_part(str(rec.get("title") or ""))):
            product_id += 1
        if re_search_part(str(rec.get("title") or "")):
            exact_model += 1

        pe = rec.get("product_economics") if isinstance(rec.get("product_economics"), dict) else {}
        pf = rec.get("profit_first") if isinstance(rec.get("profit_first"), dict) else {}
        if pe.get("gov_value") is not None or pf.get("expected_profit") is not None and pe.get("both_sides_known"):
            # gov from pe
            pass
        if pe.get("gov_value") is not None or br.get("state") in {GOV_VALUE_FOUND, ECONOMICS_READY}:
            gov += 1
        if pe.get("acquisition_cost") is not None or br.get("state") in {PUBLIC_COST_FOUND, ECONOMICS_READY}:
            cost += 1
        if pe.get("both_sides_known") or br.get("state") == ECONOMICS_READY:
            both += 1
        if br.get("state") == ECONOMICS_READY:
            econ_ready += 1
        ps = str(pf.get("profit_status") or "")
        if ps:
            profit_c[ps] += 1
        try:
            ep = float(pf.get("expected_profit")) if pf.get("expected_profit") is not None else None
        except Exception:
            ep = None
        if ep is not None:
            if ep > 0:
                profit_buckets[">$0"] += 1
            for thr, label in (
                (1000, ">=$1K"),
                (2500, ">=$2.5K"),
                (5000, ">=$5K"),
                (10000, ">=$10K"),
                (25000, ">=$25K"),
                (50000, ">=$50K"),
                (75000, ">=$75K"),
            ):
                if ep >= thr:
                    profit_buckets[label] += 1

    found = status_c.get(FREE_PACKAGE_FOUND, 0)
    attempted = sum(status_c.values())
    profitable = sum(profit_c.get(k, 0) for k in ("PROVEN_PROFITABLE", "LIKELY_PROFITABLE", "POSSIBLE_PROFIT"))
    # also count >$0
    profitable = max(profitable, profit_buckets.get(">$0", 0))

    def _y(n: int, d: int) -> float:
        return round(n / d, 4) if d else 0.0

    return {
        "kind": "BidNetFreePackageFunnelReport",
        "generated_at": now_utc().isoformat(),
        "canonical_live": available_count(store),
        "bidnet_backlog": {
            "eligible_product": eligible,
            "auth_or_docs_blocked_remaining": auth_backlog,
            "attempted": attempted,
            "FREE_PACKAGE_FOUND": found,
            "PACKAGE_UNAVAILABLE_FREE": status_c.get(PACKAGE_UNAVAILABLE_FREE, 0),
            "PACKAGE_RECOVERY_RETRYABLE": status_c.get(PACKAGE_RECOVERY_RETRYABLE, 0),
            "PACKAGE_MATCH_AMBIGUOUS": status_c.get(PACKAGE_MATCH_AMBIGUOUS, 0),
        },
        "documents": {
            "opportunities_with_documents": doc_ops,
            "total_documents": doc_total,
            "pricing_schedules": pricing,
            "specifications": specs,
            "item_lists": item_lists,
            "bid_award_tabs": tabs,
        },
        "identity": {
            "product_identified": product_id,
            "exact_model_part_clue": exact_model,
        },
        "evidence": {
            "gov_value_known": gov,
            "public_cost_known": cost,
            "both_sides_known": both,
        },
        "economics": {
            "economics_ready": econ_ready,
            **{k: profit_c.get(k, 0) for k in (
                "PROVEN_PROFITABLE",
                "LIKELY_PROFITABLE",
                "POSSIBLE_PROFIT",
                "UNPROVEN",
                "UNPROFITABLE",
                "EXECUTION_BLOCKED",
            )},
            "PROFITABLE_AT_PUBLIC_RETAIL": profit_c.get("PROFITABLE_AT_PUBLIC_RETAIL", 0),
        },
        "profit_buckets": dict(profit_buckets),
        "yields": {
            "free_package_yield": _y(found, attempted),
            "document_to_identity_yield": _y(product_id, found) if found else 0.0,
            "identity_to_both_sides_yield": _y(both, product_id) if product_id else 0.0,
            "both_sides_to_profit_yield": _y(profitable, both) if both else 0.0,
        },
    }
