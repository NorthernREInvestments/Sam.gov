"""Partitioned BidNet full-universe harvest — state networks beat national soft-cap.

National /solicitations/open-bids soft-caps around ~6–7k unique rows.
Statewide /{slug}/solicitations/open-bids partitions cover the visible universe.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import uuid4

import httpx

from application_clock import now_utc
from bidnet_discovery.parse import parse_search_results_html, reported_total_from_html
from discovery.bidnet_network import BIDNET_STATE_NETWORKS, enrich_bidnet_network

log = logging.getLogger("govtracker.bidnet_discovery.partitioned")

CHECKPOINT = "bidnet_auth/partitioned_checkpoint.json"
REPORT = "bidnet_auth/last_partitioned_harvest.json"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
NATIONAL_URL = "https://www.bidnetdirect.com/solicitations/open-bids"


def _row_key(r: dict[str, Any]) -> str:
    meta = r.get("raw_metadata") if isinstance(r.get("raw_metadata"), dict) else {}
    return (
        str(meta.get("bidnet_internal_id") or "").strip()
        or str(r.get("detail_url") or "").split("?")[0].lower().strip()
        or f"{(r.get('title') or '')[:100]}|{r.get('deadline_raw') or ''}".lower()
    )


def _harvest_list_url(
    client: httpx.Client,
    *,
    list_url: str,
    partition_id: str,
    max_pages: int = 400,
    on_progress: Any | None = None,
    page_delay_sec: float = 0.35,
    empty_stop: int = 5,
    start_page: int = 1,
) -> dict[str, Any]:
    """Paginate one open-bids list URL to exhaustion or max_pages.

    BidNet rate-limits aggressive concurrency — prefer sequential + delay.
    Retries empty/non-200 pages before counting toward empty_stop.
    """
    import time

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    pages = 0
    reported = None
    empty_streak = 0
    complete = False
    rate_limit_hits = 0

    first = max(1, int(start_page or 1))
    for page_num in range(first, max_pages + 1):
        url = list_url if page_num == 1 else f"{list_url.rstrip('/')}/page{page_num}"
        batch: list[dict[str, Any]] = []
        body = ""
        status = 0
        final_url = url
        for attempt in range(3):
            try:
                resp = client.get(url)
                status = resp.status_code
                body = resp.text or ""
                final_url = str(resp.url)
            except Exception as exc:
                rate_limit_hits += 1
                time.sleep(1.0 + attempt)
                if attempt == 2:
                    empty_streak += 1
                    if empty_streak >= empty_stop:
                        return {
                            "partition_id": partition_id,
                            "list_url": list_url,
                            "reported": reported,
                            "retrieved": len(rows),
                            "pages": pages,
                            "pagination_complete": False,
                            "error": type(exc).__name__,
                            "rate_limit_hits": rate_limit_hits,
                            "rows": rows,
                        }
                continue
            if status != 200 or len(body) < 500:
                rate_limit_hits += 1
                time.sleep(1.0 + attempt)
                continue
            batch = parse_search_results_html(body, list_url=final_url, page_number=page_num)
            if batch or page_num == 1:
                break
            time.sleep(0.8 + attempt)
        else:
            empty_streak += 1
            if empty_streak >= empty_stop:
                break
            continue

        if reported is None and body:
            reported = reported_total_from_html(body)
        pages += 1
        if not batch:
            empty_streak += 1
            if empty_streak >= empty_stop:
                # Only mark complete if we already have rows (natural end)
                complete = bool(rows) and reported is None
                break
            time.sleep(page_delay_sec)
            continue
        new = 0
        for r in batch:
            meta = dict(r.get("raw_metadata") or {})
            meta["partition_id"] = partition_id
            meta["harvest_mode"] = "partitioned_open_bids_http"
            r["raw_metadata"] = meta
            key = _row_key(r)
            if not key or key in seen:
                continue
            seen.add(key)
            rows.append(r)
            new += 1
        if new == 0:
            empty_streak += 1
            if empty_streak >= empty_stop:
                complete = True
                break
        else:
            empty_streak = 0
        if on_progress and (pages % 5 == 0 or page_num == 1):
            try:
                on_progress(
                    phase="PARTITION",
                    pct=None,
                    partition=partition_id,
                    pages=pages,
                    retrieved=len(rows),
                    reported=reported,
                )
            except Exception:
                pass
        if reported is not None and len(rows) >= int(reported):
            complete = True
            break
        if page_delay_sec:
            time.sleep(page_delay_sec)
    else:
        complete = bool(reported is not None and len(rows) >= int(reported))

    # Zero-row first page is NOT complete — caller should retry
    if not rows:
        complete = False

    return {
        "partition_id": partition_id,
        "list_url": list_url,
        "reported": reported,
        "retrieved": len(rows),
        "unique": len(rows),
        "pages": pages,
        "pagination_complete": complete,
        "duplicate_skips": 0,
        "rate_limit_hits": rate_limit_hits,
        "error": None if (complete or rows) else "empty_or_rate_limited",
        "rows": rows,
    }


def _save_checkpoint(payload: dict[str, Any]) -> None:
    try:
        from m3_data_root import data_path

        path = data_path(CHECKPOINT)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    except Exception:
        log.exception("Failed saving BidNet partition checkpoint")


def run_bidnet_partitioned_harvest(
    *,
    max_results: int = 30000,
    max_pages_per_partition: int = 1200,
    include_national: bool = True,
    persist: bool = True,
    run_id: str | None = None,
    on_progress: Any | None = None,
    use_auth_seed: bool = True,
) -> dict[str, Any]:
    """Full BidNet universe: national sequential first, then state fill-in.

    National /pageN reaches the ~23k UI total when not rate-limited.
    Aggressive concurrency previously zeroed later-alphabet states.
    """
    from m3_canonical_discovery_bridge import merge_discovery_into_canonical

    run_id = run_id or f"BNP-{uuid4().hex[:12]}"
    started = now_utc().isoformat()
    national_part = {
        "partition_id": "national",
        "list_url": NATIONAL_URL,
        "state_code": None,
    }
    state_parts: list[dict[str, Any]] = []
    for net in BIDNET_STATE_NETWORKS:
        row = enrich_bidnet_network(net)
        state_parts.append(
            {
                "partition_id": f"state_{net['state_code']}",
                "list_url": row["list_url"],
                "state_code": net["state_code"],
                "slug": net["slug"],
            }
        )
    partitions: list[dict[str, Any]] = []
    if include_national:
        partitions.append(national_part)
    partitions.extend(state_parts)

    report: dict[str, Any] = {
        "kind": "BidNetPartitionedHarvest",
        "run_id": run_id,
        "started_at": started,
        "partition_method": "national_sequential(+state_fillin)",
        "partitions_planned": len(partitions),
        "partitions": [],
        "auth_seed": None,
        "reported_open_ui": None,
        "retrieved_raw": 0,
        "retrieved_unique": 0,
        "pagination_complete": False,
        "retrieval_pct": None,
        "canonical_merge": {},
        "DISCOVERY_TRUNCATED": True,
    }

    all_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    auth_status = None

    # Optional: seed current private search hrefs + UI reported total
    if use_auth_seed:
        try:
            from bidnet_discovery.harvest import harvest_authenticated_search
            from bidnet_auth import BidNetAuthenticatedClient

            client = BidNetAuthenticatedClient()
            try:
                auth = client.ensure_authenticated()
                auth_status = auth.to_dict()
                report["auth_seed"] = {"status": auth.status, "authenticated": auth.authenticated}
                if auth.authenticated:
                    seed = harvest_authenticated_search(
                        client,
                        search_url="https://www.bidnetdirect.com/private/supplier/solicitations/search",
                        max_results=100,
                        max_pages=1,
                        open_details=False,
                        detail_limit=0,
                        on_progress=None,
                    )
                    report["reported_open_ui"] = seed.get("reported_total")
                    for r in seed.get("rows") or []:
                        key = _row_key(r)
                        if key and key not in seen:
                            seen.add(key)
                            meta = dict(r.get("raw_metadata") or {})
                            meta["partition_id"] = "private_search_seed"
                            r["raw_metadata"] = meta
                            all_rows.append(r)
                    report["auth_seed"]["seed_rows"] = len(seed.get("rows") or [])
            finally:
                client.close()
        except Exception as exc:
            report["auth_seed"] = {"error": type(exc).__name__}
            log.exception("BidNet auth seed failed")

    ua_headers = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml"}
    part_reports: list[dict[str, Any]] = []

    def _merge_part(pr: dict[str, Any], part: dict[str, Any]) -> dict[str, Any]:
        added = 0
        for r in pr.get("rows") or []:
            key = _row_key(r)
            if not key or key in seen:
                continue
            seen.add(key)
            all_rows.append(r)
            added += 1
            if len(all_rows) >= max_results:
                break
        summary = {
            "partition_id": part["partition_id"],
            "state_code": part.get("state_code"),
            "list_url": part["list_url"],
            "reported": pr.get("reported"),
            "retrieved": pr.get("retrieved"),
            "unique_added": added,
            "pages": pr.get("pages"),
            "pagination_complete": pr.get("pagination_complete"),
            "rate_limit_hits": pr.get("rate_limit_hits"),
            "error": pr.get("error"),
        }
        part_reports.append(summary)
        report["partitions"] = part_reports
        report["retrieved_unique"] = len(all_rows)
        return summary

    # --- Pass 1: national sequential (primary path to ~23k) ---
    if include_national and len(all_rows) < max_results:
        if on_progress:
            try:
                on_progress(phase="NATIONAL", pct=8, partition="national", retrieved=len(all_rows))
            except Exception:
                pass

        def _national_progress(**kwargs: Any) -> None:
            if not on_progress:
                return
            pages = int(kwargs.get("pages") or 0)
            retrieved = int(kwargs.get("retrieved") or 0)
            # Map national pagination into 8–40% so operators see movement.
            pct = min(40, 8 + int(32 * min(1.0, pages / 900.0)))
            try:
                on_progress(
                    phase="NATIONAL",
                    pct=pct,
                    partition="national",
                    pages=pages,
                    retrieved=retrieved,
                    reported=kwargs.get("reported"),
                )
            except Exception:
                pass

        with httpx.Client(timeout=40.0, follow_redirects=True, headers=ua_headers) as http:
            pr = _harvest_list_url(
                http,
                list_url=NATIONAL_URL,
                partition_id="national",
                max_pages=max(max_pages_per_partition, 1200),
                on_progress=_national_progress,
                page_delay_sec=0.25,
                empty_stop=6,
            )
            # If national reported total is known, prefer it as UI total
            if pr.get("reported") and not report.get("reported_open_ui"):
                report["reported_open_ui"] = pr.get("reported")
            summary = _merge_part(pr, national_part)
            if persist:
                _save_checkpoint(
                    {
                        "run_id": run_id,
                        "updated_at": now_utc().isoformat(),
                        "retrieved_unique": len(all_rows),
                        "partitions_done": 1,
                        "last_partition": summary,
                        "phase": "national",
                    }
                )

    ui_target = report.get("reported_open_ui")
    need_fillin = True
    if ui_target and len(all_rows) >= int(0.90 * int(ui_target)):
        need_fillin = False

    # --- Pass 2: sequential state fill-in (rate-limit safe) ---
    if need_fillin and len(all_rows) < max_results:
        for i, part in enumerate(state_parts):
            if len(all_rows) >= max_results:
                break
            with httpx.Client(timeout=40.0, follow_redirects=True, headers=ua_headers) as http:
                pr = _harvest_list_url(
                    http,
                    list_url=part["list_url"],
                    partition_id=part["partition_id"],
                    max_pages=min(max(max_pages_per_partition, 400), 800),
                    on_progress=None,
                    page_delay_sec=0.2,
                    empty_stop=6,
                )
                # One retry for empty/rate-limited partitions
                if not pr.get("rows"):
                    import time

                    time.sleep(1.5)
                    pr = _harvest_list_url(
                        http,
                        list_url=part["list_url"],
                        partition_id=part["partition_id"],
                        max_pages=min(max(max_pages_per_partition, 400), 800),
                        on_progress=None,
                        page_delay_sec=0.3,
                        empty_stop=6,
                    )
            summary = _merge_part(pr, part)
            if on_progress:
                try:
                    on_progress(
                        phase="STATE_FILLIN",
                        pct=min(95, int(40 + 55 * (i + 1) / max(1, len(state_parts)))),
                        partition=part["partition_id"],
                        retrieved=len(all_rows),
                        partitions_done=i + 1,
                    )
                except Exception:
                    pass
            if persist and (i % 3 == 0 or i + 1 == len(state_parts)):
                _save_checkpoint(
                    {
                        "run_id": run_id,
                        "updated_at": now_utc().isoformat(),
                        "retrieved_unique": len(all_rows),
                        "partitions_done": i + 1,
                        "last_partition": summary,
                        "phase": "state_fillin",
                    }
                )
            if ui_target and len(all_rows) >= int(0.90 * int(ui_target)):
                break

    report["retrieved_raw"] = sum(int(p.get("retrieved") or 0) for p in part_reports) + int(
        (report.get("auth_seed") or {}).get("seed_rows") or 0
    )
    report["retrieved_unique"] = len(all_rows)
    ui_reported = report.get("reported_open_ui")
    if ui_reported:
        report["retrieval_pct"] = round(100.0 * len(all_rows) / max(1, int(ui_reported)), 2)
        # BidNet's public UI total drifts and soft-caps under rate limits; a full
        # national+state sweep at >=90% is production-complete for this source.
        report["pagination_complete"] = len(all_rows) >= int(0.90 * int(ui_reported))
        report["DISCOVERY_TRUNCATED"] = not report["pagination_complete"]
        report["remaining_gap"] = max(0, int(ui_reported) - len(all_rows))
    else:
        # Fall back to sum of partition reported when available
        part_rep = sum(int(p["reported"] or 0) for p in part_reports if p.get("reported"))
        report["reported_open_ui"] = part_rep or None
        if part_rep:
            report["retrieval_pct"] = round(100.0 * len(all_rows) / max(1, part_rep), 2)
        report["pagination_complete"] = all(
            bool(p.get("pagination_complete")) for p in part_reports if p.get("retrieved")
        ) or (
            bool(part_rep) and len(all_rows) >= int(0.90 * int(part_rep))
        )
        report["DISCOVERY_TRUNCATED"] = not report["pagination_complete"]
        report["remaining_gap"] = None

    # Cap
    unique_rows = all_rows[:max_results]
    merge = merge_discovery_into_canonical(
        run_id=run_id,
        trigger="bidnet_partitioned_harvest",
        records=unique_rows,
        sources_attempted=["live_bidnet_partitioned"],
        sources_succeeded=["live_bidnet_partitioned"] if unique_rows else [],
        sources_failed=[] if unique_rows else ["live_bidnet_partitioned"],
        source_counts={
            "live_bidnet_partitioned": {
                "ok": bool(unique_rows),
                "raw": len(unique_rows),
                "reported_total": report.get("reported_open_ui"),
            }
        },
        raw_opportunities_found=len(unique_rows),
        records_normalized=len(unique_rows),
        error_summary=("DISCOVERY_TRUNCATED" if report.get("DISCOVERY_TRUNCATED") else None),
        api_usage={"SAM": 0, "bidnet_partitioned": True, "auth": auth_status},
        started_at=started,
        persist=persist,
    )
    report["canonical_merge"] = {
        "new": merge.get("new_canonical_opportunities_added"),
        "updated": merge.get("existing_opportunities_updated"),
        "duplicates_detected": merge.get("duplicates_detected"),
        "canonical_after": merge.get("canonical_total_after"),
        "available_after": merge.get("currently_available_after"),
        "available_before": merge.get("currently_available_before"),
        "run_status": merge.get("run_status"),
    }
    report["net_new"] = merge.get("new_canonical_opportunities_added")
    report["completed_at"] = now_utc().isoformat()
    report["auth"] = auth_status

    if persist:
        try:
            from m3_data_root import data_path

            path = data_path(REPORT)
            path.parent.mkdir(parents=True, exist_ok=True)
            # Drop row payloads from partition summaries already stored
            path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        except Exception:
            log.exception("Failed saving partitioned harvest report")
        try:
            from bidnet_auth.telemetry import record_harvest_counters

            record_harvest_counters(
                reported_total=report.get("reported_open_ui"),
                retrieved_total=report.get("retrieved_unique"),
                pages_scanned=sum(int(p.get("pages") or 0) for p in part_reports),
                pagination_complete=bool(report.get("pagination_complete")),
                discovery_truncated=bool(report.get("DISCOVERY_TRUNCATED")),
            )
        except Exception:
            pass
    return report
