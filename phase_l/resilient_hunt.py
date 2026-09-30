"""Phase L.11 — resilient live hunt: per-source watchdog, checkpoint, resume."""

from __future__ import annotations

import json
import multiprocessing as mp
import time
from pathlib import Path
from typing import Any

from application_clock import now_utc

BUILD = "20260928-m3-phase-l11-exact-award-history-recovery"
ROOT = Path(__file__).resolve().parents[1]
CHECKPOINT_PATH = ROOT / "data" / "phase_l11_hunt_checkpoint.json"
SOURCE_HEALTH_PATH = ROOT / "data" / "phase_l11_source_health.json"

SOURCE_TIMEOUT = "SOURCE_TIMEOUT"
HUNT_COMPLETE = "COMPLETE"
HUNT_COMPLETE_WITH_FAILURES = "COMPLETE_WITH_SOURCE_FAILURES"
HUNT_PARTIAL = "PARTIAL"
HUNT_FAILED = "FAILED"

# Bounded envelopes
DEFAULT_SOURCE_TIMEOUT_S = 45.0
DEFAULT_CONNECT_TIMEOUT_S = 8.0
DEFAULT_READ_TIMEOUT_S = 15.0
DEFAULT_MAX_RESPONSE_BYTES = 2_500_000
DEFAULT_MAX_RETRIES = 1
DEFAULT_MAX_REDIRECTS = 5


def _utc() -> str:
    return now_utc().isoformat()


def _load(path: Path) -> dict[str, Any]:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = _utc()
    data["build"] = BUILD
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def load_checkpoint() -> dict[str, Any]:
    return _load(CHECKPOINT_PATH) or {
        "completed_sources": [],
        "failed_sources": [],
        "timed_out_sources": [],
        "records_by_source": {},
        "last_completed_source": None,
        "status": "EMPTY",
    }


def save_checkpoint(cp: dict[str, Any]) -> None:
    _save(CHECKPOINT_PATH, cp)


def update_source_health(source_id: str, *, ok: bool, latency_ms: float, records: int = 0, error: str | None = None) -> None:
    h = _load(SOURCE_HEALTH_PATH)
    sources = h.setdefault("sources", {})
    rec = sources.setdefault(
        source_id,
        {
            "last_success": None,
            "last_failure": None,
            "consecutive_failures": 0,
            "avg_latency_ms": 0.0,
            "result_count_total": 0,
            "timeout_count": 0,
            "attempts": 0,
        },
    )
    rec["attempts"] = int(rec.get("attempts") or 0) + 1
    prev_avg = float(rec.get("avg_latency_ms") or 0)
    n = int(rec["attempts"])
    rec["avg_latency_ms"] = round(((prev_avg * (n - 1)) + latency_ms) / n, 1)
    if ok:
        rec["last_success"] = _utc()
        rec["consecutive_failures"] = 0
        rec["result_count_total"] = int(rec.get("result_count_total") or 0) + records
    else:
        rec["last_failure"] = _utc()
        rec["consecutive_failures"] = int(rec.get("consecutive_failures") or 0) + 1
        if error and "TIMEOUT" in str(error).upper():
            rec["timeout_count"] = int(rec.get("timeout_count") or 0) + 1
    _save(SOURCE_HEALTH_PATH, h)


def order_candidates_by_health(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Healthy / high-yield sources first; preserve input order on ties (L4 priority)."""
    h = _load(SOURCE_HEALTH_PATH).get("sources") or {}

    def score(item: tuple[int, dict[str, Any]]) -> tuple:
        idx, c = item
        sid = str(c.get("source_id") or "")
        rec = h.get(sid) or {}
        fails = int(rec.get("consecutive_failures") or 0)
        timeouts = int(rec.get("timeout_count") or 0)
        yields = int(rec.get("result_count_total") or 0)
        boost = 0
        sl = sid.lower()
        if sid.startswith("structured_") or "structured" in sl:
            # L.15: prefer Tier-1/2 structured open-data over fragile HTML
            boost = 5000
        elif "bidnet" in sl:
            boost = 1000
            # Prefer historically productive BidNet states
            for pref in ("texas", "ohio", "massachusetts", "pennsylvania", "georgia", "new_york", "florida"):
                if pref in sl:
                    boost += 200
                    break
        elif "sam" in sl:
            boost = 800
        # Do not let prior false timeouts permanently bury structured sources
        if sid.startswith("structured_"):
            timeouts = 0
            fails = min(fails, 1)
        return (-boost - yields + fails * 50 + timeouts * 80, idx)

    return [c for _, c in sorted(enumerate(candidates), key=score)]


def _listing_worker(payload: dict[str, Any], queue: Any) -> None:
    """Child process: fetch one listing; must be picklable top-level."""
    try:
        from discovery.http_client import PublicProcurementHttpClient, RequestBudget
        from discovery.live_fetchers import get_live_fetcher

        budget = RequestBudget(
            max_total_requests=int(payload.get("max_requests") or 20),
            max_pages_per_source=int(payload.get("max_pages") or 3),
            max_records_per_source=int(payload.get("max_records") or 80),
            max_runtime_seconds=float(payload.get("source_timeout") or 40.0),
            max_retries=1,
            timeout_seconds=float(payload.get("read_timeout") or DEFAULT_READ_TIMEOUT_S),
        )
        client = PublicProcurementHttpClient(budget=budget, authorize_live=True)
        fetcher = get_live_fetcher(payload["adapter_family"])
        if not fetcher:
            queue.put({"ok": False, "error": "no_fetcher", "opportunities": []})
            return
        result = fetcher.fetch_listing(
            client,
            list_url=payload["list_url"],
            source_id=payload["source_id"],
            max_pages=int(payload.get("max_pages") or 3),
            pagination_exhaust=False,
            pagination_safety_max_pages=int(payload.get("max_pages") or 3),
        )
        # Serialize opportunities lightly
        opps = []
        for o in result.get("opportunities") or []:
            if hasattr(o, "to_dict"):
                opps.append(o.to_dict())
            elif isinstance(o, dict):
                opps.append(o)
            else:
                opps.append(
                    {
                        "title": getattr(o, "title", None),
                        "external_id": getattr(o, "external_id", None),
                        "detail_url": getattr(o, "detail_url", None),
                        "deadline_raw": getattr(o, "deadline_raw", None),
                        "agency": getattr(o, "agency", None),
                        "status": getattr(o, "status", None),
                        "source_id": payload["source_id"],
                        "state_code": getattr(o, "state_code", None),
                        "jurisdiction": getattr(o, "jurisdiction", None),
                        "estimated_value": getattr(o, "estimated_value", None),
                        "description": (getattr(o, "description", None) or "")[:500],
                    }
                )
        queue.put(
            {
                "ok": bool((result.get("validation") or {}).get("valid", True)),
                "validation": result.get("validation"),
                "pages_fetched": result.get("pages_fetched"),
                "records_fetched": result.get("records_fetched"),
                "pagination_stop_reason": result.get("pagination_stop_reason"),
                "opportunities": opps,
                "error": None,
            }
        )
    except Exception as exc:  # noqa: BLE001
        queue.put({"ok": False, "error": str(exc)[:300], "opportunities": []})


def fetch_source_with_watchdog(
    cand: dict[str, Any],
    *,
    timeout_s: float = DEFAULT_SOURCE_TIMEOUT_S,
    max_pages: int = 3,
) -> dict[str, Any]:
    """Process-isolated listing fetch — parent can terminate hung adapters on Windows.

    Tier-1/2 structured JSON adapters (Socrata etc.) run in-process: they are fast
    and stable; Windows spawn+join often false-timeouts them under 75s.
    """
    sid = str(cand.get("source_id") or "")
    adapter = str(cand.get("adapter_family") or "")
    if adapter == "live_structured" or sid.startswith("structured_"):
        t0 = time.monotonic()
        try:
            from discovery.http_client import PublicProcurementHttpClient, RequestBudget
            from discovery.live_fetchers import get_live_fetcher

            budget = RequestBudget(
                max_total_requests=12,
                max_pages_per_source=max(1, max_pages),
                max_records_per_source=200,
                max_runtime_seconds=min(60.0, float(timeout_s)),
                max_retries=1,
                timeout_seconds=DEFAULT_READ_TIMEOUT_S,
            )
            client = PublicProcurementHttpClient(budget=budget, authorize_live=True)
            fetcher = get_live_fetcher(adapter or "live_structured")
            if not fetcher:
                return {
                    "source_id": sid,
                    "ok": False,
                    "status": "no_fetcher",
                    "explicit_state": "FAILED",
                    "error": "no_fetcher",
                    "elapsed_ms": (time.monotonic() - t0) * 1000,
                    "opportunities": [],
                }
            # Prefer registry URL; inject field map via temporary monkey on parse by
            # ensuring list_url matches registry (already does).
            result = fetcher.fetch_listing(
                client,
                list_url=cand.get("list_url") or "",
                source_id=sid,
                max_pages=max(1, max_pages),
                pagination_exhaust=False,
                pagination_safety_max_pages=max(1, max_pages),
            )
            opps = []
            for o in result.get("opportunities") or []:
                if hasattr(o, "to_dict"):
                    d = o.to_dict()
                elif isinstance(o, dict):
                    d = dict(o)
                else:
                    continue
                d["source_id"] = sid
                if cand.get("state_code") and not d.get("state_code"):
                    d["state_code"] = cand.get("state_code")
                # Preserve structured field map defaults
                meta = cand.get("structured_meta") or {}
                if meta:
                    d.setdefault("raw_metadata", {})
                    if isinstance(d["raw_metadata"], dict):
                        d["raw_metadata"] = {**d["raw_metadata"], "structured_meta": meta, "structured_adapter": True}
                opps.append(d)
            ok = bool((result.get("validation") or {}).get("valid", True)) or bool(opps)
            elapsed = (time.monotonic() - t0) * 1000
            update_source_health(sid, ok=ok, latency_ms=elapsed, records=len(opps))
            return {
                "source_id": sid,
                "ok": ok,
                "status": "SUCCESS" if ok else "FAILED",
                "explicit_state": "SUCCESS" if ok else "FAILED",
                "error": None,
                "elapsed_ms": elapsed,
                "opportunities": opps,
                "pages_fetched": result.get("pages_fetched"),
                "validation": result.get("validation"),
            }
        except Exception as exc:  # noqa: BLE001
            elapsed = (time.monotonic() - t0) * 1000
            update_source_health(sid, ok=False, latency_ms=elapsed, error=str(exc)[:160])
            return {
                "source_id": sid,
                "ok": False,
                "status": "FAILED",
                "explicit_state": "FAILED",
                "error": str(exc)[:300],
                "elapsed_ms": elapsed,
                "opportunities": [],
            }

    t0 = time.monotonic()
    queue: mp.Queue = mp.Queue()
    payload = {
        "source_id": sid,
        "list_url": cand.get("list_url"),
        "adapter_family": cand.get("adapter_family"),
        "max_pages": max_pages,
        "max_records": 80,
        "max_requests": 20,
        "connect_timeout": DEFAULT_CONNECT_TIMEOUT_S,
        "read_timeout": DEFAULT_READ_TIMEOUT_S,
        "source_timeout": timeout_s,
    }
    proc = mp.Process(target=_listing_worker, args=(payload, queue))
    proc.start()
    proc.join(timeout_s)
    elapsed = (time.monotonic() - t0) * 1000

    if proc.is_alive():
        proc.terminate()
        proc.join(5)
        if proc.is_alive():
            proc.kill()
            proc.join(2)
        update_source_health(sid, ok=False, latency_ms=elapsed, error=SOURCE_TIMEOUT)
        return {
            "source_id": sid,
            "ok": False,
            "status": SOURCE_TIMEOUT,
            "explicit_state": SOURCE_TIMEOUT,
            "error": f"exceeded {timeout_s}s",
            "elapsed_ms": elapsed,
            "opportunities": [],
        }

    result: dict[str, Any] = {"ok": False, "error": "no_result", "opportunities": []}
    try:
        if not queue.empty():
            result = queue.get_nowait()
    except Exception:
        pass

    ok = bool(result.get("ok")) and not result.get("error")
    n = len(result.get("opportunities") or [])
    update_source_health(sid, ok=ok or n > 0, latency_ms=elapsed, records=n, error=result.get("error"))
    validation = result.get("validation") or {}
    fail_type = (validation.get("failure_type") or "").upper() if not ok else ""
    status = "SUCCESS" if (ok or n > 0) else (fail_type or result.get("error") or "FAILED")
    return {
        "source_id": sid,
        "ok": ok or n > 0,
        "status": status,
        "explicit_state": status,
        "error": result.get("error"),
        "elapsed_ms": elapsed,
        "opportunities": result.get("opportunities") or [],
        "pages_fetched": result.get("pages_fetched"),
        "validation": validation,
    }


def run_resilient_live_discovery(
    candidates: list[dict[str, Any]],
    *,
    authorize_live: bool = True,
    resume: bool = True,
    source_timeout_s: float = DEFAULT_SOURCE_TIMEOUT_S,
    max_pages: int = 3,
    on_source_complete: Any | None = None,
) -> dict[str, Any]:
    """Crawl candidates one-by-one with watchdog + checkpoint. Never hang the hunt."""
    if not authorize_live:
        return {"run_status": HUNT_FAILED, "error": "authorize_live_required", "opportunities": [], "metrics": {}}

    cp = load_checkpoint() if resume else {
        "completed_sources": [],
        "failed_sources": [],
        "timed_out_sources": [],
        "records_by_source": {},
        "last_completed_source": None,
    }
    done = set(cp.get("completed_sources") or [])
    ordered = order_candidates_by_health(candidates)

    collected: list[dict[str, Any]] = []
    per_source: dict[str, Any] = {}
    timed_out: list[str] = list(cp.get("timed_out_sources") or [])
    failed: list[str] = list(cp.get("failed_sources") or [])
    attempted = 0
    successful = 0

    for cand in ordered:
        sid = str(cand.get("source_id") or "")
        if not sid or sid in done:
            continue
        # Circuit breaker: skip sources with many consecutive failures this run family
        health = (_load(SOURCE_HEALTH_PATH).get("sources") or {}).get(sid) or {}
        if int(health.get("consecutive_failures") or 0) >= 5 and int(health.get("timeout_count") or 0) >= 3:
            per_source[sid] = {
                "ok": False,
                "attempt": False,
                "explicit_state": "CIRCUIT_OPEN",
                "source_stop_reason": "CIRCUIT_OPEN",
            }
            continue

        attempted += 1
        print(f"[l11-hunt] source {attempted}/{len(ordered)} {sid} timeout={source_timeout_s}s", flush=True)
        res = fetch_source_with_watchdog(cand, timeout_s=source_timeout_s, max_pages=max_pages)
        per_source[sid] = {
            "ok": res.get("ok"),
            "attempt": True,
            "raw": len(res.get("opportunities") or []),
            "unique": len(res.get("opportunities") or []),
            "elapsed_ms": res.get("elapsed_ms"),
            "explicit_state": res.get("explicit_state"),
            "source_stop_reason": res.get("status"),
            "error": res.get("error"),
            "pages_fetched": res.get("pages_fetched"),
        }
        opps = res.get("opportunities") or []
        for o in opps:
            if isinstance(o, dict):
                o.setdefault("source_id", sid)
                if cand.get("state_code") and not o.get("state_code"):
                    o["state_code"] = cand.get("state_code")
                collected.append(o)

        if res.get("status") == SOURCE_TIMEOUT:
            timed_out.append(sid)
            failed.append(sid)
        elif res.get("ok"):
            successful += 1
        else:
            failed.append(sid)

        done.add(sid)
        cp["completed_sources"] = sorted(done)
        cp["failed_sources"] = sorted(set(failed))
        cp["timed_out_sources"] = sorted(set(timed_out))
        cp["records_by_source"][sid] = len(opps)
        cp["last_completed_source"] = sid
        cp["status"] = "IN_PROGRESS"
        save_checkpoint(cp)
        if on_source_complete:
            try:
                on_source_complete(per_source, sid)
            except Exception:
                pass

    unattempted = [c["source_id"] for c in ordered if c["source_id"] not in done]
    if timed_out or failed:
        status = HUNT_COMPLETE_WITH_FAILURES if not unattempted else HUNT_PARTIAL
    elif unattempted:
        status = HUNT_PARTIAL
    else:
        status = HUNT_COMPLETE

    cp["status"] = status
    save_checkpoint(cp)

    metrics = {
        "sources_attempted": attempted,
        "sources_successful": successful,
        "sources_failed": len(set(failed)),
        "sources_timed_out": len(set(timed_out)),
        "unique_records": len(collected),
        "raw_records": len(collected),
        "per_source": per_source,
        "unattempted": unattempted,
        "timed_out_sources": sorted(set(timed_out)),
        "failed_sources": sorted(set(failed)),
    }
    return {
        "run_status": status,
        "opportunities": collected,
        "handoff_records": collected,
        "metrics": metrics,
        "checkpoint": cp,
        "build": BUILD,
        "error": None,
    }


def reset_checkpoint() -> None:
    save_checkpoint(
        {
            "completed_sources": [],
            "failed_sources": [],
            "timed_out_sources": [],
            "records_by_source": {},
            "last_completed_source": None,
            "status": "RESET",
        }
    )
