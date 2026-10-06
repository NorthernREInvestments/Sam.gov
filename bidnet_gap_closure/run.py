"""Recover only the BidNet list holes. Do not re-walk the harvested 21,969."""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from application_clock import now_utc
from bidnet_discovery.partitioned import NATIONAL_URL, UA, _harvest_list_url
from bidnet_discovery.parse import parse_search_results_html, reported_total_from_html
from bidnet_gap_closure.accounting import (
    build_accounting,
    classify_found_row,
    explain_unlisted_residual,
    retry_targets,
)
from bidnet_gap_closure.identity import is_bidnet_record, stable_bidnet_key
from bidnet_gap_closure.models import (
    BASELINE_HARVESTED,
    BASELINE_MISSING,
    BASELINE_REPORTED,
    CHECKPOINT,
    REPORT_JSON,
    REPORT_TXT,
)
from bidnet_gap_closure.report import format_gap_report

def _load(name: str) -> dict[str, Any]:
    from m3_data_root import data_path

    path = data_path(name)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(name: str, payload: dict[str, Any]) -> None:
    from m3_data_root import data_path

    path = data_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _harvested_keys() -> set[str]:
    from phase_l.l23_full_population_funnel import load_store

    keys: set[str] = set()
    store = load_store()
    for row in store.values():
        if not isinstance(row, dict) or not is_bidnet_record(row):
            continue
        key = stable_bidnet_key(row)
        if key:
            keys.add(key)
    return keys


def _baseline_partitions() -> list[dict[str, Any]]:
    report = _load("bidnet_auth/last_partitioned_harvest.json")
    parts = report.get("partitions") if isinstance(report.get("partitions"), list) else None
    if parts:
        return parts
    # Job result copy written by the production poller.
    prod = _load("m3_bidnet_partitioned_harvest_prod.json")
    result = prod.get("result") if isinstance(prod.get("result"), dict) else {}
    return list(result.get("partitions") or [])


def _national_prior(parts: list[dict[str, Any]]) -> dict[str, Any]:
    for part in parts:
        if part.get("partition_id") == "national":
            return part
    return {"partition_id": "national", "pages": 0, "retrieved": 0}


def _fresh_reported() -> int | None:
    headers = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml"}
    try:
        with httpx.Client(timeout=40.0, follow_redirects=True, headers=headers) as client:
            resp = client.get(NATIONAL_URL)
            if resp.status_code != 200:
                return None
            return reported_total_from_html(resp.text or "")
    except Exception:
        return None


def _take_new_rows(
    rows: list[dict[str, Any]],
    *,
    harvested: set[str],
    already: set[str],
    partition_id: str,
    prior: dict[str, Any],
) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for row in rows:
        key = stable_bidnet_key(row)
        if key and (key in harvested or key in already):
            continue
        if not key and int(prior.get("retrieved") or 0) > 0:
            continue
        kind = classify_found_row(row, partition_id=partition_id, prior=prior)
        if key:
            already.add(key)
        meta = dict(row.get("raw_metadata") or {})
        meta["gap_closure"] = True
        meta["gap_class"] = kind
        row = dict(row)
        row["raw_metadata"] = meta
        found.append(
            {
                "key": key or f"malformed:{partition_id}:{len(found)}",
                "classification": kind,
                "recovered": kind in {
                    "PAGINATION_MISS",
                    "STATE_SWEEP_GAP",
                    "TEMPORARY_FETCH_FAILURE",
                    "ACCESSIBLE_BUT_MISSED",
                    "AUTH_SESSION_VARIANCE",
                }
                and key is not None,
                "partition": partition_id,
                "evidence": (
                    f"prior pages={prior.get('pages')} retrieved={prior.get('retrieved')} "
                    f"error={prior.get('error')}"
                ),
                "record": row if key else None,
            }
        )
    return found


def _fetch_page(client: httpx.Client, url: str) -> tuple[int, str]:
    """Retry one list page through rate limits. A 404 is final."""
    last_status = 0
    for round_i in range(3):
        for attempt in range(6):
            try:
                resp = client.get(url)
            except Exception:
                time.sleep(min(12, 1.5 * (attempt + 1)))
                continue
            last_status = resp.status_code
            body = resp.text or ""
            if last_status == 404:
                return 404, body
            if last_status == 200 and len(body) >= 500:
                return 200, body
            time.sleep(min(12, 1.5 * (attempt + 1)))
        time.sleep(15 + 10 * round_i)
    return last_status, ""


def _walk_national(
    client: httpx.Client,
    *,
    start_page: int,
    harvested: set[str],
    already: set[str],
    prior: dict[str, Any],
    on_page: Any | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Continue the national open-bids list past the harvested page cursor.

    Rate-limit empties are retried. The walk ends only on a real empty page
    (HTTP 200, no rows) or when the reported total is covered.
    """
    page = max(1, int(start_page))
    found: list[dict[str, Any]] = []
    stats: dict[str, Any] = {
        "start_page": page,
        "pages": 0,
        "rows_seen": 0,
        "new": 0,
        "complete": False,
        "stopped_reason": None,
        "reported": None,
        "next_page": page,
    }
    prior_rows = int(prior.get("retrieved") or 0)
    while page <= 1300:
        url = NATIONAL_URL if page == 1 else f"{NATIONAL_URL}/page{page}"
        status, body = _fetch_page(client, url)
        if status != 200:
            stats["stopped_reason"] = f"http_{status or 'error'}"
            stats["next_page"] = page
            return found, stats
        rows = parse_search_results_html(body, list_url=url, page_number=page)
        reported = reported_total_from_html(body)
        if reported:
            stats["reported"] = reported
        stats["pages"] += 1
        stats["rows_seen"] += len(rows)
        if not rows:
            stats["complete"] = True
            stats["stopped_reason"] = "end"
            stats["next_page"] = page
            return found, stats
        batch = _take_new_rows(
            rows,
            harvested=harvested,
            already=already,
            partition_id="national",
            prior=prior,
        )
        for item in batch:
            key = item.get("key")
            if key:
                already.add(str(key))
        found.extend(batch)
        stats["new"] += len(batch)
        covered = prior_rows + stats["rows_seen"]
        if reported and covered >= int(reported):
            stats["complete"] = True
            stats["stopped_reason"] = "reported_covered"
            stats["next_page"] = page + 1
            return found, stats
        page += 1
        stats["next_page"] = page
        if on_page and stats["pages"] % 5 == 0:
            on_page(page, stats, found)
        time.sleep(0.35)
    stats["stopped_reason"] = "max_pages"
    return found, stats


def run_bidnet_gap_closure(*, on_progress: Any | None = None) -> dict[str, Any]:
    """List-only retry of failed partitions. Known harvested keys are not reprocessed."""
    parts = _baseline_partitions()
    national = _national_prior(parts)
    targets = retry_targets(parts)
    ck = _load(CHECKPOINT)
    done = set(ck.get("partitions_done") or [])
    found: list[dict[str, Any]] = [dict(item) for item in (ck.get("found") or []) if isinstance(item, dict)]
    harvested = _harvested_keys()
    if ck.get("merged"):
        for item in found:
            key = item.get("key")
            if key:
                harvested.discard(str(key))
    keys_before = len(harvested)
    already = {str(item.get("key")) for item in found if item.get("key")}

    def _progress(phase: str, pct: int, **extra: Any) -> None:
        if not on_progress:
            return
        try:
            on_progress(phase=phase, pct=pct, **extra)
        except Exception:
            pass

    headers = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml"}
    # A previous run marked national done when rate limits stopped it early.
    # Only a real end-of-list flag may skip the national continuation.
    national_complete = bool(ck.get("national_complete"))
    done.discard("national")
    state_work = [t for t in targets if str(t.get("partition_id")) not in done]
    national_stats: dict[str, Any] = dict(ck.get("national_stats") or {})

    def _checkpoint(**extra: Any) -> None:
        _save(
            CHECKPOINT,
            {
                "updated_at": now_utc().isoformat(),
                "partitions_done": sorted(done),
                "found": found,
                "harvested_keys_count": keys_before,
                "national_complete": national_complete,
                "national_next_page": ck.get("national_next_page"),
                "national_stats": national_stats,
                "merged": bool(ck.get("merged")),
                **extra,
            },
        )

    with httpx.Client(timeout=40.0, follow_redirects=True, headers=headers) as client:
        for index, prior in enumerate(state_work):
            pid = str(prior.get("partition_id"))
            _progress("GAP_RETRY", min(20, index), partition=pid, retrieved=len(found))
            harvested_result = _harvest_list_url(
                client,
                list_url=str(prior.get("list_url") or ""),
                partition_id=pid,
                max_pages=800,
                page_delay_sec=0.6,
                empty_stop=8,
            )
            if harvested_result.get("error") == "empty_or_rate_limited" and int(harvested_result.get("pages") or 0) == 0:
                # Confirm a dead state slug (404) versus a temporary miss.
                status, _body = _fetch_page(client, str(prior.get("list_url") or ""))
                if status == 404:
                    done.add(pid)
                    _checkpoint()
                    continue
            batch = _take_new_rows(
                list(harvested_result.get("rows") or []),
                harvested=harvested,
                already=already,
                partition_id=pid,
                prior=prior,
            )
            for item in batch:
                already.add(str(item.get("key")))
            found.extend(batch)
            done.add(pid)
            _checkpoint()

        if not national_complete:
            start_page = int(ck.get("national_next_page") or int(national.get("pages") or 0) + 1)
            synced = 0

            def _on_page(page: int, stats: dict[str, Any], batch_found: list[dict[str, Any]]) -> None:
                nonlocal national_stats, synced
                national_stats = stats
                found.extend(batch_found[synced:])
                synced = len(batch_found)
                ck["national_next_page"] = stats.get("next_page")
                _progress(
                    "NATIONAL_RESUME",
                    min(95, 20 + int(70 * page / 1000)),
                    partition="national",
                    pages=page,
                    retrieved=len(found),
                )
                _checkpoint()

            batch, national_stats = _walk_national(
                client,
                start_page=start_page,
                harvested=harvested,
                already=already,
                prior=national,
                on_page=_on_page,
            )
            found.extend(batch[synced:])
            national_complete = bool(national_stats.get("complete"))
            ck["national_next_page"] = national_stats.get("next_page")
            _checkpoint()

    records = [item["record"] for item in found if item.get("record") and item.get("recovered")]
    merge_stats: dict[str, Any] = {}
    if records:
        from m3_canonical_discovery_bridge import merge_discovery_into_canonical

        merge_stats = merge_discovery_into_canonical(
            run_id=f"GAP-{now_utc().strftime('%Y%m%d%H%M%S')}",
            trigger="bidnet_gap_closure",
            records=records,
            sources_attempted=["bidnet_gap_retry"],
            sources_succeeded=["bidnet_gap_retry"],
            raw_opportunities_found=len(records),
            persist=True,
        )
    _save(
        CHECKPOINT,
        {
            "updated_at": now_utc().isoformat(),
            "partitions_done": sorted(done),
            "found": found,
            "harvested_keys_count": keys_before,
            "national_complete": national_complete,
            "national_next_page": ck.get("national_next_page"),
            "national_stats": national_stats,
            "merged": True,
        },
    )
    ck["merged"] = True
    keys_after = _harvested_keys()
    dropped = harvested - keys_after
    for item in found:
        kind = str(item.get("classification") or "")
        key = str(item.get("key") or "")
        item["recovered"] = bool(key) and key in keys_after and key not in harvested and kind in {
            "PAGINATION_MISS",
            "STATE_SWEEP_GAP",
            "TEMPORARY_FETCH_FAILURE",
            "ACCESSIBLE_BUT_MISSED",
            "AUTH_SESSION_VARIANCE",
        }
    fresh = national_stats.get("reported") or _fresh_reported()
    covered = (
        national_stats.get("complete")
        and national_stats.get("stopped_reason") == "reported_covered"
        and fresh is not None
    )
    prior_rows = int(national.get("retrieved") or 0)
    rendered = prior_rows + int(national_stats.get("rows_seen") or 0)
    duplicate_slots = 0
    if covered and rendered >= int(fresh):
        duplicate_slots = BASELINE_MISSING
    explanations = explain_unlisted_residual(
        found=sum(1 for item in found if item.get("key")),
        fresh_reported=fresh,
        duplicate_slots=duplicate_slots,
    )
    ledger = [{k: v for k, v in item.items() if k != "record"} for item in found]
    ledger.extend(explanations)
    accounting = build_accounting(
        classifications=ledger,
        fresh_reported=fresh,
        harvested_keys_before=keys_before,
        harvested_keys_after=len(keys_after),
        dropped_keys=len(dropped),
    )
    report = {
        **accounting,
        "started_from": {
            "reported_open": BASELINE_REPORTED,
            "harvested": BASELINE_HARVESTED,
            "missing": BASELINE_MISSING,
            "national_pages_already_fetched": national.get("pages"),
            "retry_partitions": [t.get("partition_id") for t in targets],
            "national_stats": national_stats,
        },
        "merge": {
            "new": merge_stats.get("new_canonical_opportunities_added"),
            "updated": merge_stats.get("existing_opportunities_updated"),
            "duplicates_detected": merge_stats.get("duplicates_detected"),
        },
        "sample_recovered_keys": [item.get("key") for item in found if item.get("recovered")][:30],
    }
    _save(REPORT_JSON, report)
    _save(REPORT_TXT, {"text": format_gap_report(report)})
    from m3_data_root import data_path

    data_path(REPORT_TXT).write_text(format_gap_report(report), encoding="utf-8")
    _progress("DONE", 100, retrieved=report["recovery"]["recovered"])
    return report
