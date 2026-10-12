"""Cheapest BidNet deadline recovery via open-bids LIST pages (not per-opp abstracts).

READ-ONLY vs L23 / enrichment. Writes only under _og_commercial_research_pipeline_out/.
Excludes IDs already recovered in the abstract sidecar.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from application_clock import now_utc
from bidnet_discovery.deadline_persist import apply_deadline_to_row
from bidnet_discovery.parse import parse_search_results_html
from buyer_intelligence.fresh_true_open_detail_package_enrichment_repair_v1 import _platform_family
from buyer_intelligence.true_live_accepting_bids_gate_v1 import (
    CLOSED_DEADLINE_PASSED,
    OPEN_ACCEPTING_BIDS,
    OPEN_DEADLINE_UNKNOWN,
    determine_canonical_live_state,
)
from discovery.deadline import normalize_deadline

log = logging.getLogger("govtracker.bidnet_list_deadline_recovery_v1")

BUILD = "20261010-m3-bidnet-list-deadline-recovery-v1"
SOURCE_RUN = "prod-live-20261010T023015Z-6d8c5be7"
OUT_DIR = "_og_commercial_research_pipeline_out"

PRIOR_SIDECAR = "m3_bidnet_deadline_recovery_sidecar_staged_v1.json"
PRIOR_RESULTS = "m3_bidnet_deadline_recovery_results_v1.json"
CHECKPOINT = "m3_bidnet_list_deadline_recovery_checkpoint_v1.json"
REPORT = "m3_bidnet_list_deadline_recovery_report_v1.json"
RESULTS = "m3_bidnet_list_deadline_recovery_results_v1.json"
PROGRESS = "m3_bidnet_list_deadline_recovery_progress_v1.json"
SIDECAR = "m3_bidnet_list_deadline_recovery_sidecar_staged_v1.json"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
PAGE_PAUSE_S = 1.5
DEFAULT_TIMEOUT = 25.0


def _utc() -> str:
    return now_utc().isoformat()


def _canon() -> Path:
    return Path(__file__).resolve().parents[1] / "data"


def _out() -> Path:
    d = _canon() / OUT_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save(name: str, obj: Any) -> Path:
    import os

    p = _out() / name
    payload = json.dumps(obj, indent=2, default=str)
    for attempt in range(5):
        tmp = p.with_suffix(p.suffix + f".tmp{attempt}")
        try:
            tmp.write_text(payload, encoding="utf-8")
            try:
                os.replace(tmp, p)
            except PermissionError:
                p.write_text(payload, encoding="utf-8")
                tmp.unlink(missing_ok=True)
            return p
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            time.sleep(0.1 * (attempt + 1))
    p.write_text(payload, encoding="utf-8")
    return p


def _load(name: str) -> Any:
    p = _out() / name
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _progress(**kwargs: Any) -> None:
    cur = _load(PROGRESS) or {"kind": "BidNetListDeadlineRecoveryProgress", "build": BUILD}
    cur.update(kwargs)
    cur["heartbeat_at"] = _utc()
    _save(PROGRESS, cur)


def load_opps() -> dict[str, dict[str, Any]]:
    path = _canon() / "_prod_snapshot" / "l23_canonical_population_store.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    return {str(k): v for k, v in (doc.get("opportunities") or {}).items() if isinstance(v, dict)}


def already_recovered_ids() -> set[str]:
    ids: set[str] = set()
    sc = _load(PRIOR_SIDECAR) or {}
    ids.update((sc.get("patches") or {}).keys())
    res = _load(PRIOR_RESULTS) or {}
    for r in res.get("rows") or []:
        if r.get("DEADLINE_RAW") or r.get("NOT_STATED"):
            ids.add(str(r["OPPORTUNITY_ID"]))
    return ids


def _network_slug_from_url(url: str) -> str | None:
    # https://www.bidnetdirect.com/{slug}/solicitations/open-bids/...
    m = re.search(r"bidnetdirect\.com/([^/]+)/solicitations", url or "", re.I)
    if m:
        slug = m.group(1).lower()
        if slug not in {"public", "www", "supplier"}:
            return slug
    return None


def cohort_missing_by_network(
    opps: dict[str, dict[str, Any]], *, exclude: set[str]
) -> dict[str, list[tuple[str, dict[str, Any]]]]:
    by: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for cid, rec in opps.items():
        if cid in exclude:
            continue
        if _platform_family(rec) != "BidNet":
            continue
        if rec.get("deadline"):
            continue
        url = str(rec.get("authoritative_url") or rec.get("detail_url") or "")
        if "/abstract" not in url and "/open-bids/" not in url:
            continue
        slug = _network_slug_from_url(url) or "statewide-public"
        by.setdefault(slug, []).append((cid, rec))
    return by


def _list_page_urls(slug: str, page: int) -> list[str]:
    """Candidate open-bids list URLs for a BidNet network slug."""
    base = f"https://www.bidnetdirect.com/{slug}/solicitations/open-bids"
    if page <= 1:
        return [base, f"{base}/", f"{base}/statewide"]
    return [
        f"{base}/page{page}",
        f"{base}/statewide/page{page}",
        f"{base}?page={page}",
    ]


def _fetch_list_html(
    urls: list[str],
    *,
    client: httpx.Client,
    allow_playwright: bool = True,
) -> tuple[int, str, str, str]:
    last_status = 0
    last_url = ""
    waf_or_403 = False
    for u in urls:
        try:
            r = client.get(u)
            last_status = int(r.status_code)
            last_url = str(r.url)
            text = r.text or ""
            if last_status == 403:
                waf_or_403 = True
                continue
            if last_status == 202 or (
                len(text) < 3000 and ("gokuProps" in text or "awsWaf" in text)
            ):
                waf_or_403 = True
                continue
            if (
                "sol-closing-date" in text
                or "closingDate" in text
                or "mets-table-row" in text
                or "solicitation-link" in text
            ):
                return last_status, text, last_url, "OK"
            if last_status == 200 and len(text) > 5000:
                return last_status, text, last_url, "OK_NO_MARKERS"
        except Exception as exc:
            return 0, "", u, type(exc).__name__
    if waf_or_403 and allow_playwright:
        try:
            html, final = _playwright_list(urls[0])
            if html and (
                "sol-closing-date" in html
                or "mets-table-row" in html
                or "solicitation-link" in html
            ):
                return 200, html, final, "OK_PLAYWRIGHT"
            if html:
                return 200, html, final, "PW_NO_MARKERS"
        except Exception as exc:
            return last_status or 0, "", last_url or urls[0], f"PW_{type(exc).__name__}"
    if waf_or_403:
        return last_status or 403, "", last_url, "WAF"
    return last_status, "", last_url, "NO_URL_WORKED"


def _match_list_row_to_opp(
    list_row: dict[str, Any], opps_by_url_tail: dict[str, list[tuple[str, dict[str, Any]]]]
) -> tuple[str, dict[str, Any]] | None:
    href = str(list_row.get("authoritative_url") or list_row.get("detail_url") or list_row.get("href") or "")
    if not href:
        return None
    # Normalize tails
    for key in (href, href.split("?")[0], href.rstrip("/").split("/")[-1]):
        hits = opps_by_url_tail.get(key) or opps_by_url_tail.get(key.lower())
        if hits:
            return hits[0]
    # Fuzzy: solicitation id in URL
    m = re.search(r"/(\d{6,})(?:\?|$)", href)
    if m:
        hits = opps_by_url_tail.get(m.group(1))
        if hits:
            return hits[0]
    return None


def _index_opps(opps_list: list[tuple[str, dict[str, Any]]]) -> dict[str, list[tuple[str, dict[str, Any]]]]:
    idx: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for cid, rec in opps_list:
        url = str(rec.get("authoritative_url") or rec.get("detail_url") or "")
        tails = {
            url,
            url.split("?")[0],
            url.rstrip("/").split("/")[-1] if url else "",
        }
        m = re.search(r"/(\d{6,})(?:\?|$)", url)
        if m:
            tails.add(m.group(1))
        for t in tails:
            if not t:
                continue
            idx.setdefault(t, []).append((cid, rec))
            idx.setdefault(t.lower(), []).append((cid, rec))
    return idx


def run_list_recovery_sample(
    *,
    target_matches: int = 100,
    max_pages_per_network: int = 8,
    max_networks: int = 12,
    seed_networks: list[str] | None = None,
) -> dict[str, Any]:
    """Validate list-page deadline recovery on NEW records (excludes prior sidecar)."""
    t0 = time.time()
    run_id = f"bn-list-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
    _progress(status="RUNNING", phase="LIST_RECOVERY", RUN_ID=run_id, target=target_matches)

    opps = load_opps()
    exclude = already_recovered_ids()
    by_net = cohort_missing_by_network(opps, exclude=exclude)

    # Prefer networks with the most missing deadlines
    ranked = sorted(by_net.items(), key=lambda kv: -len(kv[1]))
    if seed_networks:
        ranked = [(s, by_net[s]) for s in seed_networks if s in by_net] + [
            x for x in ranked if x[0] not in (seed_networks or [])
        ]
    ranked = ranked[:max_networks]

    stats: Counter[str] = Counter()
    matched_rows: list[dict[str, Any]] = []
    page_fetches: list[dict[str, Any]] = []
    patches: dict[str, Any] = {}
    seen_cids: set[str] = set()

    with httpx.Client(
        timeout=DEFAULT_TIMEOUT,
        headers={"User-Agent": UA, "Accept": "text/html"},
        follow_redirects=True,
    ) as client:
        for slug, cohort in ranked:
            if len(matched_rows) >= target_matches:
                break
            idx = _index_opps(cohort)
            consecutive_fail = 0
            for page in range(1, max_pages_per_network + 1):
                if len(matched_rows) >= target_matches:
                    break
                urls = _list_page_urls(slug, page)
                status, html, final, reason = _fetch_list_html(urls, client=client)
                stats["list_requests"] += 1
                page_fetches.append(
                    {
                        "slug": slug,
                        "page": page,
                        "status": status,
                        "reason": reason,
                        "url": final,
                        "html_len": len(html),
                    }
                )
                _progress(
                    phase="LIST_FETCH",
                    slug=slug,
                    page=page,
                    matched=len(matched_rows),
                    list_requests=stats["list_requests"],
                    last_reason=reason,
                )
                if reason in {"HTTP_403", "WAF"}:
                    stats[f"fail:{reason}"] += 1
                    consecutive_fail += 1
                    if consecutive_fail >= 2:
                        stats["network_aborted_403"] += 1
                        break
                    time.sleep(PAGE_PAUSE_S)
                    continue
                if not html:
                    stats["fail:empty"] += 1
                    consecutive_fail += 1
                    if consecutive_fail >= 2:
                        break
                    continue
                consecutive_fail = 0
                parsed = parse_search_results_html(html, list_url=final or urls[0], page_number=page)
                stats["list_rows_parsed"] += len(parsed)
                if not parsed:
                    stats["fail:no_rows"] += 1
                    # Try Playwright once per network if HTTP HTML has no rows
                    if page == 1:
                        try:
                            html2, final2 = _playwright_list(urls[0])
                            stats["list_requests_playwright"] += 1
                            if html2:
                                parsed = parse_search_results_html(
                                    html2, list_url=final2 or urls[0], page_number=page
                                )
                                stats["list_rows_parsed"] += len(parsed)
                        except Exception as exc:
                            stats[f"pw_error:{type(exc).__name__}"] += 1
                    if not parsed:
                        continue

                for lr in parsed:
                    raw = lr.get("deadline_raw")
                    hit = _match_list_row_to_opp(lr, idx)
                    if not hit:
                        stats["list_row_unmatched"] += 1
                        continue
                    cid, rec = hit
                    if cid in seen_cids or cid in exclude:
                        stats["list_row_dup_skip"] += 1
                        continue
                    seen_cids.add(cid)
                    before = determine_canonical_live_state({**rec, "canonical_opportunity_id": cid})
                    not_stated = bool(raw and re.search(r"not\s*stated", str(raw), re.I))
                    nd = normalize_deadline(raw) if raw and not not_stated else {}
                    extracted = {
                        "ok": bool(nd.get("utc_deadline") or nd.get("parsed_local")) or not_stated,
                        "deadline_raw": raw,
                        "deadline": None
                        if not_stated
                        else (nd.get("utc_deadline") or nd.get("parsed_local")),
                        "not_stated": not_stated,
                        "timezone": nd.get("timezone"),
                        "timezone_confidence": nd.get("timezone_confidence"),
                        "source_url": final,
                        "retrieved_at": _utc(),
                        "reason": "LIST_PAGE" if raw else "LIST_ROW_NO_CLOSING",
                    }
                    merged = apply_deadline_to_row(rec, extracted, overwrite_existing=False)
                    after = determine_canonical_live_state({**merged, "canonical_opportunity_id": cid})
                    row = {
                        "OPPORTUNITY_ID": cid,
                        "TITLE": (rec.get("title") or lr.get("title") or "")[:140],
                        "NETWORK": slug,
                        "LIST_URL": final,
                        "LIST_PAGE": page,
                        "DEADLINE_RAW": raw,
                        "DEADLINE_NORMALIZED": extracted.get("deadline"),
                        "NOT_STATED": not_stated,
                        "BEFORE_STATE": before.get("CANONICAL_STATE"),
                        "AFTER_STATE": after.get("CANONICAL_STATE"),
                        "DAYS_REMAINING": after.get("DAYS_REMAINING"),
                        "FAR_FUTURE_FLAG": after.get("FAR_FUTURE_DEADLINE_FLAG"),
                        "PROMOTED_TO_OPEN_ACCEPTING": after.get("CANONICAL_STATE")
                        == OPEN_ACCEPTING_BIDS
                        and before.get("CANONICAL_STATE") != OPEN_ACCEPTING_BIDS,
                        "FETCH_MODE": "OPEN_BIDS_LIST",
                        "OK": True,
                        "STAGED_ROW_PATCH": {
                            "deadline": merged.get("deadline"),
                            "deadline_raw": merged.get("deadline_raw"),
                            "deadline_timezone": merged.get("deadline_timezone"),
                        },
                    }
                    matched_rows.append(row)
                    stats["matched"] += 1
                    if raw and not not_stated:
                        stats["closing_extracted"] += 1
                    if not_stated:
                        stats["not_stated"] += 1
                    if row["PROMOTED_TO_OPEN_ACCEPTING"]:
                        stats["promoted"] += 1
                    if after.get("CANONICAL_STATE") == CLOSED_DEADLINE_PASSED:
                        stats["expired"] += 1
                    if row["STAGED_ROW_PATCH"].get("deadline") or row["STAGED_ROW_PATCH"].get(
                        "deadline_raw"
                    ):
                        patches[cid] = row["STAGED_ROW_PATCH"]
                    if len(matched_rows) >= target_matches:
                        break
                time.sleep(PAGE_PAUSE_S)

    reqs = int(stats["list_requests"]) + int(stats["list_requests_playwright"])
    recovered = int(stats["closing_extracted"])
    report = {
        "kind": "BidNetListDeadlineRecoveryReport",
        "build": BUILD,
        "RUN_ID": run_id,
        "created_at": _utc(),
        "SOURCE_RUN": SOURCE_RUN,
        "WALL_CLOCK_SECONDS": round(time.time() - t0, 2),
        "EXCLUDED_PRIOR_RECOVERED_IDS": len(exclude),
        "TARGET_MATCHES": target_matches,
        "MATCHED": len(matched_rows),
        "BATCH_STATS": dict(stats),
        "REQUESTS_ATTEMPTED": reqs,
        "DATES_RECOVERED": recovered,
        "VERIFIED_FUTURE_PROMOTED": int(stats["promoted"]),
        "EXPIRED": int(stats["expired"]),
        "NOT_STATED": int(stats["not_stated"]),
        "ACCESS_FAILURES_403_WAF": int(stats.get("fail:HTTP_403", 0))
        + int(stats.get("fail:WAF", 0)),
        "REQUESTS_PER_RECOVERED_DEADLINE": round(reqs / max(1, recovered), 4) if recovered else None,
        "AVG_SECONDS_PER_MATCH": round((time.time() - t0) / max(1, len(matched_rows)), 3),
        "PAGE_FETCHES": page_fetches[:80],
        "ISOLATION": {
            "TOUCHED_L23": False,
            "TOUCHED_ENRICHMENT": False,
            "SAM_API_CALLS": 0,
            "APPLY_SIDECAR": False,
        },
        "ROUTE": "open-bids list pagination (cheapest)",
        "FINAL_STATUS": "COMPLETE",
    }
    # Persist this run under a run-id file first (never lose a good sample to a later WAF run)
    _save(
        f"m3_bidnet_list_deadline_recovery_results_{run_id}.json",
        {"build": BUILD, "RUN_ID": run_id, "count": len(matched_rows), "rows": matched_rows},
    )
    _save(f"m3_bidnet_list_deadline_recovery_report_{run_id}.json", report)

    # Merge into cumulative results/sidecar — never replace a non-empty store with empty
    prior_res = _load(RESULTS) or {"rows": []}
    prior_rows = list(prior_res.get("rows") or [])
    by_id = {r["OPPORTUNITY_ID"]: r for r in prior_rows if r.get("OPPORTUNITY_ID")}
    for r in matched_rows:
        by_id[r["OPPORTUNITY_ID"]] = r
    merged_rows = list(by_id.values())
    _save(RESULTS, {"build": BUILD, "RUN_ID": run_id, "count": len(merged_rows), "rows": merged_rows})

    prior_sc = _load(SIDECAR) or {"patches": {}}
    merged_patches = dict(prior_sc.get("patches") or {})
    merged_patches.update(patches)
    _save(
        SIDECAR,
        {
            "kind": "BidNetListDeadlineRecoverySidecar",
            "build": BUILD,
            "created_at": _utc(),
            "APPLY_TO_PRODUCTION": False,
            "count": len(merged_patches),
            "patches": merged_patches,
            "NOTE": "Cumulative list-page recoveries; merge only after enrichment idle.",
            "last_run_id": run_id,
            "last_run_matched": len(matched_rows),
        },
    )
    _save(REPORT, report)
    _progress(status="COMPLETE", phase="DONE", FINAL_STATUS="COMPLETE", matched=len(matched_rows))
    return report


def _playwright_list(url: str) -> tuple[str, str]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=UA)
            page.goto(url, wait_until="domcontentloaded", timeout=25000)
            page.wait_for_timeout(1200)
            return page.content() or "", page.url
        finally:
            browser.close()


def run_validation_100_then_maybe_500() -> dict[str, Any]:
    r100 = run_list_recovery_sample(target_matches=100, max_pages_per_network=10, max_networks=15)
    recovered = int(r100.get("DATES_RECOVERED") or 0)
    reqs = int(r100.get("REQUESTS_ATTEMPTED") or 0)
    access_fail = int(r100.get("ACCESS_FAILURES_403_WAF") or 0)
    # Expand only if list route is efficient and not blocked
    efficient = recovered >= 40 and (reqs / max(1, recovered)) <= 0.5
    not_blocked = access_fail < 8
    if not (efficient and not_blocked):
        r100["EXPANSION"] = "SKIPPED_LIST_ROUTE_NOT_YET_RELIABLE"
        r100["EXPANSION_REASON"] = {
            "recovered": recovered,
            "reqs_per_deadline": r100.get("REQUESTS_PER_RECOVERED_DEADLINE"),
            "access_failures": access_fail,
            "needed": ">=40 recovered and <=0.5 req/deadline and <8 access failures",
        }
        return {"V100_LIST": r100, "V500_LIST": None}
    r500 = run_list_recovery_sample(target_matches=500, max_pages_per_network=20, max_networks=25)
    return {"V100_LIST": r100, "V500_LIST": r500}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    out = run_validation_100_then_maybe_500()
    print(json.dumps({k: (v or {}).get("BATCH_STATS") for k, v in out.items()}, indent=2, default=str))
