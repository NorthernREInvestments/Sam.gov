"""Full BidNet deadline recovery from list pages + caches → L23.

Uses national open-bids HTTP pagination (stable, no auth required for listing
metadata). Matches by BidNet solicitation id in URL. Never overwrites verified
deadlines. Checkpoints every N pages for safe resume.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from application_clock import now_utc
from bidnet_discovery.parse import parse_search_results_html
from buyer_intelligence.deadline_extraction_repair_v1 import (
    apply_candidate,
    classify_deadline_status,
    load_staged_recovery_evidence,
    run_deadline_repair,
    snapshot_counts,
)
from discovery.deadline import normalize_deadline
from m3_data_root import data_path
from phase_l.l23_full_population_funnel import (
    LIVE_FRESH,
    canonical_id_for,
    load_store,
    merge_into_store,
    save_store,
    to_canonical_record,
)

log = logging.getLogger("govtracker.bidnet_full_deadline_recovery_v1")

BUILD = "20261012-m3-bidnet-full-deadline-recovery-v1"
OUT = "_og_commercial_research_pipeline_out"
CHECKPOINT = f"{OUT}/m3_bn_full_deadline_checkpoint_v1.json"
PROGRESS = f"{OUT}/m3_bn_full_deadline_progress_v1.json"
REPORT = f"{OUT}/m3_bn_full_deadline_report_v1.json"
RESULTS = f"{OUT}/m3_bn_full_deadline_results_v1.json"

NATIONAL = "https://www.bidnetdirect.com/solicitations/open-bids"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_SOL_RE = re.compile(r"/(\d{9,14})(?:/|\?|$)")


def _utc() -> str:
    return now_utc().isoformat()


def _save(rel: str, obj: Any) -> None:
    p = Path(data_path(rel))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")


def _load(rel: str) -> Any:
    p = Path(data_path(rel))
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _progress(**kwargs: Any) -> None:
    cur = _load(PROGRESS) or {"kind": "BidNetFullDeadlineProgress", "build": BUILD}
    cur.update(kwargs)
    cur["heartbeat_at"] = _utc()
    _save(PROGRESS, cur)
    log.info("progress %s", {k: kwargs[k] for k in list(kwargs)[:8]})


def index_store(store: dict[str, dict[str, Any]]) -> dict[str, str]:
    idx: dict[str, str] = {}
    for oid, rec in store.items():
        if not isinstance(rec, dict):
            continue
        url = str(rec.get("authoritative_url") or "")
        m = _SOL_RE.search(url)
        if m:
            idx[m.group(1)] = oid
        sol = re.sub(r"\D", "", str(rec.get("solicitation_event_id") or ""))
        if len(sol) >= 9:
            idx[sol] = oid
        # Exact source URL
        if url:
            idx[f"url:{url.split('?')[0]}"] = oid
    return idx


def match_list_item(item: dict[str, Any], idx: dict[str, str]) -> tuple[str, str] | None:
    href = str(item.get("authoritative_url") or item.get("detail_url") or "")
    m = _SOL_RE.search(href)
    if m and m.group(1) in idx:
        return idx[m.group(1)], "EXACT_SOURCE_ID"
    sol = re.sub(r"\D", "", str(item.get("solicitation_id") or item.get("external_id") or ""))
    if len(sol) >= 9 and sol in idx:
        return idx[sol], "EXACT_SOURCE_ID"
    key = f"url:{href.split('?')[0]}"
    if href and key in idx:
        return idx[key], "EXACT_SOURCE_URL"
    return None


def _list_item_to_row(item: dict[str, Any], *, slug: str, page: int) -> dict[str, Any]:
    """Normalize a BidNet list row for L23 ingest with deadline evidence."""
    sol = str(item.get("solicitation_id") or item.get("external_id") or "").strip()
    href = str(item.get("authoritative_url") or item.get("detail_url") or "")
    # Prefer durable abstract URL when we have a numeric BidNet notice id.
    if sol.isdigit() and len(sol) >= 9:
        href = (
            f"https://www.bidnetdirect.com/public/supplier/solicitations/statewide/"
            f"{sol}/abstract"
        )
    raw = item.get("deadline_raw")
    deadline = item.get("deadline")
    if not deadline and raw:
        nd = normalize_deadline(str(raw))
        deadline = nd.get("utc_deadline") or nd.get("parsed_local")
        tz = nd.get("timezone")
        tzc = nd.get("timezone_confidence")
    else:
        tz = item.get("deadline_timezone")
        tzc = item.get("deadline_tz_confidence")
    if isinstance(deadline, datetime):
        deadline_s = deadline.isoformat()
    else:
        deadline_s = str(deadline) if deadline else None
    return {
        "source_id": "live_bidnet",
        "platform": "BidNet",
        "platform_family": "BidNet",
        "portal": "BidNet",
        "title": item.get("title"),
        "agency": item.get("agency") or item.get("location") or "BidNet",
        "buyer": item.get("agency") or item.get("location") or "BidNet",
        "location": item.get("location"),
        "jurisdiction": item.get("jurisdiction") or item.get("location"),
        "status": "OPEN",
        "live_status": "OPEN",
        "solicitation_id": sol or None,
        "solicitation_number": item.get("solicitation_number") or sol or None,
        "detail_url": href,
        "source_url": href,
        "authoritative_url": href,
        "deadline": deadline_s,
        "deadline_raw": raw,
        "deadline_timezone": tz,
        "deadline_tz_confidence": tzc,
        "inventory_freshness": LIVE_FRESH,
        "freshness": LIVE_FRESH,
        "discovery_universe": "LIVE",
        "raw_metadata": {
            "platform": "BidNet",
            "harvest_mode": "open_bids_list_deadline_recovery",
            "list_slug": slug,
            "list_page": page,
            "bidnet_internal_id": sol,
            "original_list_href": item.get("authoritative_url") or item.get("detail_url"),
        },
    }


def ingest_or_patch_list_item(
    store: dict[str, dict[str, Any]],
    idx: dict[str, str],
    item: dict[str, Any],
    *,
    slug: str,
    page: int,
) -> dict[str, Any] | None:
    """Match existing BidNet record or ingest a new open notice with deadline."""
    raw = item.get("deadline_raw")
    if not raw:
        return None
    hit = match_list_item(item, idx)
    if hit:
        oid, match_kind = hit
        rec = store.get(oid)
        if not isinstance(rec, dict):
            return None
        if rec.get("deadline"):
            return None
        nd = normalize_deadline(str(raw))
        if nd.get("not_stated"):
            return None
        norm = nd.get("utc_deadline") or nd.get("parsed_local")
        if not norm:
            return None
        result = apply_candidate(
            rec,
            deadline=norm.isoformat() if isinstance(norm, datetime) else str(norm),
            deadline_raw=str(raw),
            timezone_name=nd.get("timezone"),
            source=f"open_bids_list:{slug}:p{page}",
            evidence_url=item.get("authoritative_url") or item.get("detail_url"),
        )
        if not result.get("applied"):
            return None
        store[oid] = rec
        return {
            "opportunity_id": oid,
            "action": "PATCH_EXISTING",
            "match_kind": match_kind,
            "deadline": result.get("deadline"),
            "deadline_raw": str(raw),
            "source": f"open_bids_list:{slug}:p{page}",
            "title": (rec.get("title") or "")[:80],
        }

    row = _list_item_to_row(item, slug=slug, page=page)
    if not row.get("deadline"):
        return None
    cid = canonical_id_for(row)
    if cid in store and (store[cid] or {}).get("deadline"):
        # Ensure index knows this BidNet id for later pages
        sol = re.sub(r"\D", "", str(row.get("solicitation_id") or ""))
        if len(sol) >= 9:
            idx[sol] = cid
        return None
    if cid not in store:
        rec = to_canonical_record(row, funnel_state="RAW")
        rec["freshness"] = LIVE_FRESH
        rec["is_federal"] = False
        rec["deadline_status"] = classify_deadline_status(rec)
        rec["source_provenance"] = [
            {
                "source": f"open_bids_list:{slug}:p{page}",
                "retrieved_at": _utc(),
                "match_kind": "NEW_INGEST",
            }
        ]
        merge_into_store(store, [rec])
        action = "INGEST_NEW"
    else:
        rec = store[cid]
        result = apply_candidate(
            rec,
            deadline=row.get("deadline"),
            deadline_raw=str(raw),
            timezone_name=row.get("deadline_timezone"),
            source=f"open_bids_list:{slug}:p{page}",
            evidence_url=row.get("authoritative_url"),
        )
        if not result.get("applied"):
            return None
        store[cid] = rec
        action = "PATCH_BY_CANONICAL"
    sol = re.sub(r"\D", "", str(row.get("solicitation_id") or ""))
    if len(sol) >= 9:
        idx[sol] = cid
    href = str(row.get("authoritative_url") or "")
    if href:
        idx[f"url:{href.split('?')[0]}"] = cid
    return {
        "opportunity_id": cid,
        "action": action,
        "match_kind": "NEW_INGEST" if action == "INGEST_NEW" else "CANONICAL_ID",
        "deadline": row.get("deadline"),
        "deadline_raw": str(raw),
        "source": f"open_bids_list:{slug}:p{page}",
        "title": (row.get("title") or "")[:80],
        "solicitation_id": sol or None,
    }


def mine_html_caches(store: dict[str, dict[str, Any]], idx: dict[str, str]) -> dict[str, dict[str, Any]]:
    recovered: dict[str, dict[str, Any]] = {}
    roots = [
        Path("artifacts"),
        Path(data_path(OUT)),
        Path(data_path(".")),
    ]
    files: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for p in root.rglob("*.html"):
            n = p.name.lower()
            if any(x in n for x in ("bidnet", "list", "open", "iowa", "probe")):
                files.append(p)
    for hf in files:
        try:
            html = hf.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        try:
            items = parse_search_results_html(html, list_url="https://www.bidnetdirect.com/")
        except Exception:
            continue
        if not isinstance(items, list):
            continue
        for it in items:
            if not isinstance(it, dict):
                continue
            raw = it.get("deadline_raw") or it.get("closingDate") or it.get("closeDate")
            if not raw:
                continue
            hit = match_list_item(it, idx)
            if not hit:
                continue
            oid, match_kind = hit
            if (store.get(oid) or {}).get("deadline"):
                continue
            nd = normalize_deadline(str(raw))
            norm = nd.get("utc_deadline") or nd.get("parsed_local")
            if not norm:
                continue
            recovered[oid] = {
                "deadline": norm.isoformat() if isinstance(norm, datetime) else str(norm),
                "deadline_raw": str(raw),
                "deadline_timezone": nd.get("timezone"),
                "source": f"html_cache:{hf.name}",
                "match_kind": match_kind,
                "evidence_url": it.get("authoritative_url") or it.get("detail_url"),
            }
    return recovered


def list_url(slug: str | None, page: int) -> str:
    if not slug or slug in {"statewide-public", "national", "solicitations"}:
        return NATIONAL if page <= 1 else f"{NATIONAL}/page{page}"
    base = f"https://www.bidnetdirect.com/{slug}/solicitations/open-bids"
    return base if page <= 1 else f"{base}/page{page}"


def _cookies_from_storage_state() -> list[dict[str, str]]:
    try:
        from bidnet_auth.session_store import load_storage_state

        state = load_storage_state() or {}
    except Exception:
        return []
    out: list[dict[str, str]] = []
    for c in state.get("cookies") or []:
        if not isinstance(c, dict):
            continue
        name = c.get("name")
        value = c.get("value")
        if not name or value is None:
            continue
        domain = str(c.get("domain") or "www.bidnetdirect.com")
        out.append({"name": str(name), "value": str(value), "domain": domain, "path": str(c.get("path") or "/")})
    return out


def _http_client() -> httpx.Client:
    client = httpx.Client(
        timeout=30.0,
        follow_redirects=True,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    for c in _cookies_from_storage_state():
        try:
            client.cookies.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path") or "/")
        except Exception:
            continue
    return client


def fetch_list_page(client: httpx.Client, url: str) -> tuple[int, str, str]:
    try:
        r = client.get(url)
        return int(r.status_code), r.text or "", str(r.url)
    except Exception as exc:
        return 0, "", f"{type(exc).__name__}"


def _acquire_run_lock() -> Path | None:
    """Single-writer lock so a stale harvest cannot overwrite L23 mid-run."""
    lock = Path(data_path(f"{OUT}/m3_bn_full_deadline_run.lock"))
    lock.parent.mkdir(parents=True, exist_ok=True)
    if lock.exists():
        try:
            age = time.time() - lock.stat().st_mtime
            if age < 7200:
                pid = lock.read_text(encoding="utf-8").strip()
                log.error("Another BidNet deadline recovery holds the lock (pid=%s age_s=%.0f)", pid, age)
                return None
        except Exception:
            pass
    lock.write_text(str(os.getpid()), encoding="utf-8")
    return lock


def _release_run_lock(lock: Path | None) -> None:
    if not lock:
        return
    try:
        lock.unlink(missing_ok=True)
    except Exception:
        pass


def apply_patch_map(
    store: dict[str, dict[str, Any]],
    patches: dict[str, dict[str, Any]],
    *,
    method: str,
) -> list[dict[str, Any]]:
    applied = []
    for oid, patch in patches.items():
        rec = store.get(oid)
        if not isinstance(rec, dict):
            continue
        result = apply_candidate(
            rec,
            deadline=patch.get("deadline"),
            deadline_raw=patch.get("deadline_raw"),
            timezone_name=patch.get("deadline_timezone"),
            source=str(patch.get("source") or method),
            evidence_url=patch.get("evidence_url"),
        )
        if result.get("applied"):
            store[oid] = rec
            applied.append(
                {
                    "opportunity_id": oid,
                    "deadline": result.get("deadline"),
                    "deadline_raw": patch.get("deadline_raw"),
                    "source": patch.get("source") or method,
                    "match_kind": patch.get("match_kind"),
                    "title": (rec.get("title") or "")[:80],
                }
            )
    return applied


def harvest_list_deadlines(
    store: dict[str, dict[str, Any]],
    idx: dict[str, str],
    *,
    slugs: list[str],
    max_pages_per_slug: int = 1200,
    page_pause_s: float = 0.35,
    empty_streak_stop: int = 4,
    checkpoint_every: int = 25,
    resume: bool = True,
) -> dict[str, Any]:
    cp = (_load(CHECKPOINT) or {}) if resume else {}
    done_pages: set[str] = set(cp.get("completed_pages") or [])
    recovered_rows: list[dict[str, Any]] = list(cp.get("recovered_rows") or [])
    stats: Counter[str] = Counter(cp.get("stats") or {})
    applied_ids: set[str] = set(cp.get("applied_ids") or [])

    with _http_client() as client:
        for slug in slugs:
            empty_streak = 0
            waf_strikes = 0
            for page in range(1, max_pages_per_slug + 1):
                page_key = f"{slug}:{page}"
                if page_key in done_pages:
                    continue
                url = list_url(slug if slug != "statewide-public" else None, page)
                status, html, final = 0, "", ""
                for attempt in range(1, 4):
                    status, html, final = fetch_list_page(client, url)
                    stats["list_requests"] += 1
                    stats[f"http_{status}"] += 1
                    if status == 200 and len(html) >= 2000:
                        waf_strikes = 0
                        break
                    if status in {403, 429, 503} or (status == 200 and len(html) < 2000):
                        waf_strikes += 1
                        wait_s = min(120.0, page_pause_s * (4**attempt) + 5.0 * attempt)
                        stats["waf_retries"] += 1
                        _progress(
                            phase="LIST_HARVEST_WAF_BACKOFF",
                            slug=slug,
                            page=page,
                            attempt=attempt,
                            wait_s=round(wait_s, 1),
                            waf_strikes=waf_strikes,
                        )
                        time.sleep(wait_s)
                        continue
                    break

                _progress(
                    phase="LIST_HARVEST",
                    slug=slug,
                    page=page,
                    requests=stats["list_requests"],
                    recovered=len(recovered_rows),
                    applied=len(applied_ids),
                )

                if status != 200 or len(html) < 2000:
                    # Do NOT mark page done on WAF — stop cleanly so resume can retry.
                    if status in {403, 429, 503} or waf_strikes >= 3:
                        stats["stopped_on_waf"] += 1
                        _progress(phase="LIST_HARVEST_STOPPED_WAF", slug=slug, page=page, status=status)
                        break
                    empty_streak += 1
                    done_pages.add(page_key)
                    if empty_streak >= empty_streak_stop:
                        break
                    time.sleep(page_pause_s)
                    continue

                try:
                    items = parse_search_results_html(html, list_url=final or url, page_number=page)
                except Exception:
                    items = []
                    stats["parse_errors"] += 1

                if not items:
                    empty_streak += 1
                    done_pages.add(page_key)
                    if empty_streak >= empty_streak_stop:
                        break
                    time.sleep(page_pause_s)
                    continue

                page_hits = 0
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    raw = it.get("deadline_raw")
                    if not raw:
                        stats["list_rows_no_deadline"] += 1
                        continue
                    stats["list_rows_with_deadline"] += 1
                    row = ingest_or_patch_list_item(store, idx, it, slug=slug, page=page)
                    if not row:
                        stats["list_skipped"] += 1
                        continue
                    page_hits += 1
                    oid = str(row.get("opportunity_id") or "")
                    if oid:
                        applied_ids.add(oid)
                    recovered_rows.append(row)
                    action = str(row.get("action") or "")
                    stats["list_applied"] += 1
                    stats[f"action_{action}"] += 1

                stats["list_page_hits"] += page_hits
                done_pages.add(page_key)
                # BidNet often keeps returning 200 pages after unique inventory is
                # exhausted (duplicates). Stop when consecutive pages add nothing.
                if page_hits == 0:
                    empty_streak += 1
                    stats["duplicate_or_empty_pages"] += 1
                    if empty_streak >= empty_streak_stop:
                        _progress(
                            phase="LIST_HARVEST_END_OF_UNIQUE",
                            slug=slug,
                            page=page,
                            recovered=len(recovered_rows),
                        )
                        break
                else:
                    empty_streak = 0

                if len(done_pages) % checkpoint_every == 0:
                    save_store(store)
                    _save(
                        CHECKPOINT,
                        {
                            "build": BUILD,
                            "updated_at": _utc(),
                            "completed_pages": sorted(done_pages),
                            "applied_ids": sorted(applied_ids),
                            "recovered_rows": recovered_rows[-5000:],
                            "stats": dict(stats),
                        },
                    )

                time.sleep(page_pause_s)

    save_store(store)
    _save(
        CHECKPOINT,
        {
            "build": BUILD,
            "updated_at": _utc(),
            "completed_pages": sorted(done_pages),
            "applied_ids": sorted(applied_ids),
            "recovered_rows": recovered_rows,
            "stats": dict(stats),
            "complete": True,
        },
    )
    return {
        "recovered_rows": recovered_rows,
        "stats": dict(stats),
        "pages_done": len(done_pages),
        "applied_ids": sorted(applied_ids),
    }


def run_full_bidnet_deadline_recovery(
    *,
    max_national_pages: int = 1000,
    max_state_pages: int = 80,
    resume: bool = True,
    page_pause_s: float = 0.7,
    skip_prepass: bool = False,
    empty_streak_stop: int = 40,
) -> dict[str, Any]:
    del max_state_pages  # national-first; state networks deferred
    lock = _acquire_run_lock()
    if lock is None:
        return {"ok": False, "error": "RUN_LOCK_HELD", "build": BUILD}
    t0 = time.time()
    try:
        store = load_store()
        before = snapshot_counts(store)
        idx = index_store(store)
        method_counts: Counter[str] = Counter()
        all_applied: list[dict[str, Any]] = []

        if not skip_prepass:
            # 1) HTML caches
            _progress(phase="HTML_CACHE", status="RUNNING")
            html_patches = mine_html_caches(store, idx)
            applied = apply_patch_map(store, html_patches, method="html_cache")
            all_applied.extend(applied)
            method_counts["html_cache"] = len(applied)
            save_store(store)

            # 2) Staged sidecars (remap)
            _progress(phase="SIDECAR_REMAP", status="RUNNING")
            staged = load_staged_recovery_evidence(store)
            applied = apply_patch_map(store, staged, method="sidecar_remap")
            all_applied.extend(applied)
            method_counts["sidecar_remap"] = len(applied)
            save_store(store)

            # 3) Title / structured promote (idempotent)
            _progress(phase="STRUCTURED_TITLE", status="RUNNING")
            der = run_deadline_repair(apply=True)
            method_counts["structured_title_pass"] = int(der.get("newly_recovered") or 0)

            # Refresh store after der
            store = load_store()
            idx = index_store(store)

        # 4) National open-bids harvest (ingest new + patch existing).
        if not resume:
            _save(
                CHECKPOINT,
                {
                    "build": BUILD,
                    "completed_pages": [],
                    "applied_ids": [],
                    "recovered_rows": [],
                    "stats": {},
                },
            )
        else:
            cp0 = _load(CHECKPOINT) or {}
            stats0 = cp0.get("stats") or {}
            # Clear corrupted / matching-only / zero-ingest checkpoints
            if (
                int(stats0.get("list_unmatched") or 0) > 0
                or (
                    int(stats0.get("list_applied") or 0) == 0
                    and (cp0.get("completed_pages") or [])
                )
                or int(stats0.get("action_INGEST_NEW") or 0) == 0
                and int(stats0.get("list_applied") or 0) < 100
                and (cp0.get("completed_pages") or [])
            ):
                _save(
                    CHECKPOINT,
                    {
                        "build": BUILD,
                        "completed_pages": [],
                        "applied_ids": [],
                        "recovered_rows": [],
                        "stats": {},
                        "note": "cleared_stale_or_matching_only_checkpoint",
                    },
                )

        slugs = ["statewide-public"]
        _progress(phase="LIST_HARVEST_START", status="RUNNING", slugs=slugs, max_pages=max_national_pages)
        harvest = harvest_list_deadlines(
            store,
            idx,
            slugs=slugs,
            max_pages_per_slug=max_national_pages,
            resume=True,
            checkpoint_every=10,
            page_pause_s=page_pause_s,
            empty_streak_stop=empty_streak_stop,
        )
        method_counts["list_harvest"] = int((harvest.get("stats") or {}).get("list_applied") or 0)
        method_counts["list_ingest_new"] = int((harvest.get("stats") or {}).get("action_INGEST_NEW") or 0)
        method_counts["list_patch_existing"] = int(
            (harvest.get("stats") or {}).get("action_PATCH_EXISTING") or 0
        ) + int((harvest.get("stats") or {}).get("action_PATCH_BY_CANONICAL") or 0)
        all_applied.extend(harvest.get("recovered_rows") or [])

        # Stamp statuses on in-memory store (already mutated) then persist once
        for rec in store.values():
            if isinstance(rec, dict):
                rec["deadline_status"] = classify_deadline_status(rec)
        save_store(store)

        after = snapshot_counts(store)
        from buyer_intelligence.available_deals_visibility_v1 import run_visibility_rebuild

        vis = run_visibility_rebuild()

        report = {
            "kind": "m3_bidnet_full_deadline_recovery_v1",
            "build": BUILD,
            "built_at": _utc(),
            "elapsed_s": round(time.time() - t0, 1),
            "bidnet_total": sum(
                1
                for r in store.values()
                if isinstance(r, dict) and "bidnet" in str(r.get("platform") or "").lower()
            ),
            "method_counts": dict(method_counts),
            "list_harvest_stats": harvest.get("stats"),
            "list_pages_done": harvest.get("pages_done"),
            "network_requests": int((harvest.get("stats") or {}).get("list_requests") or 0),
            "before": before["totals"],
            "after": after["totals"],
            "before_by_platform": before.get("by_platform"),
            "after_by_platform": after.get("by_platform"),
            "visibility_counts": vis.get("counts"),
            "sample_recovered": all_applied[:30],
            "total_applied_this_run": len(
                {a["opportunity_id"] for a in all_applied if a.get("opportunity_id")}
            ),
            "cookies_loaded": len(_cookies_from_storage_state()),
            "ok": True,
        }
        _save(REPORT, report)
        _save(RESULTS, {"build": BUILD, "rows": all_applied[-8000:], "count": len(all_applied)})
        _progress(
            phase="COMPLETE",
            status="DONE",
            **{k: report[k] for k in ("elapsed_s", "network_requests", "total_applied_this_run")},
        )
        return report
    finally:
        _release_run_lock(lock)
