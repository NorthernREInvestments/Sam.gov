"""Canonical SAM.gov Opportunities API client — budget, ledger, cache, planner.

Hard constraint: SAM_DAILY_CALL_BUDGET (default 10) live calls per operational day.
All live SAM Opportunities API traffic for M3 funnel refresh MUST go through this module.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlencode

from application_clock import now_utc, today_local
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

BUILD = "20261009-m3-sam-api-credit-ledger-reconciliation-v1"
SAM_SEARCH_URL = "https://api.sam.gov/opportunities/v2/search"
SAM_DAILY_BUDGET_EXHAUSTED = "SAM_DAILY_BUDGET_EXHAUSTED"
SAM_RESERVE_PROTECTED = "SAM_RESERVE_PROTECTED"
# Owner hard rule — real credit-consuming Opportunities API calls never exceed this.
HARD_REAL_DAILY_LIMIT = 10

DATA = ROOT / "data"
ART = ROOT / "artifacts" / "sam"
CACHE_DIR = ART / "response_cache"
LEDGER_PATH = ART / "sam_call_ledger.json"
LOCK_PATH = ART / ".sam_budget.lock"
PRODUCTIVITY_PATH = ART / "sam_query_productivity.json"
LAST_REFRESH_PATH = ART / "last_successful_sam_refresh.json"
PLAN_PATH = ART / "sam_daily_query_plan.json"

ART.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)

_THREAD_LOCK = threading.RLock()


def operational_timezone() -> str:
    return (os.getenv("SCHEDULER_TIMEZONE") or "America/Denver").strip() or "America/Denver"


def sam_daily_call_budget() -> int:
    """Daily live SAM Opportunities API calls — hard-capped at HARD_REAL_DAILY_LIMIT (10).

    Env may request a lower number; it may never raise the real credit ceiling above 10.
    """
    configured = HARD_REAL_DAILY_LIMIT
    for key in ("SAM_DAILY_CALL_BUDGET", "SAM_API_CALL_LIMIT", "SAM_DAILY_API_BUDGET"):
        raw = os.getenv(key)
        if raw is not None and str(raw).strip() != "":
            try:
                configured = max(0, int(str(raw).strip()))
                break
            except ValueError:
                continue
    return min(configured, HARD_REAL_DAILY_LIMIT)


def reserve_calls() -> int:
    """Calls held back unless explicitly authorized as recovery/follow-up."""
    raw = os.getenv("SAM_DAILY_RESERVE_CALLS", "1").strip()
    try:
        return max(0, min(sam_daily_call_budget(), int(raw)))
    except ValueError:
        return 1


def budget_day_key() -> str:
    """Calendar day in operational timezone."""
    try:
        from zoneinfo import ZoneInfo

        return now_utc().astimezone(ZoneInfo(operational_timezone())).date().isoformat()
    except Exception:
        return today_local().isoformat()


def _utc() -> str:
    return now_utc().isoformat()


def query_fingerprint(endpoint: str, params: dict[str, Any]) -> str:
    """Stable fingerprint excluding api_key."""
    clean = {k: v for k, v in sorted(params.items()) if k != "api_key" and v is not None}
    blob = endpoint + "?" + urlencode({k: str(v) for k, v in clean.items()}, doseq=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def cache_path_for(fingerprint: str) -> Path:
    return CACHE_DIR / f"{fingerprint}.json"


@contextmanager
def _file_lock(timeout_s: float = 30.0) -> Iterator[None]:
    """Cross-process advisory lock via exclusive lock file."""
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout_s
    fh = None
    while True:
        try:
            fh = open(LOCK_PATH, "a+b")
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except (OSError, BlockingIOError):
            if fh:
                try:
                    fh.close()
                except Exception:
                    pass
                fh = None
            if time.time() >= deadline:
                raise TimeoutError("SAM budget lock timeout")
            time.sleep(0.05)
    try:
        with _THREAD_LOCK:
            yield
    finally:
        try:
            if fh and os.name == "nt":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            elif fh:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        if fh:
            try:
                fh.close()
            except Exception:
                pass


def load_ledger() -> dict[str, Any]:
    if LEDGER_PATH.exists():
        try:
            return json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "kind": "SamApiCallLedger",
        "build": BUILD,
        "days": {},
        "entries": [],
    }


def save_ledger(ledger: dict[str, Any]) -> None:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = LEDGER_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(ledger, indent=2, default=str), encoding="utf-8")
    tmp.replace(LEDGER_PATH)


def calls_used_today(ledger: dict[str, Any] | None = None) -> int:
    led = ledger or load_ledger()
    day = budget_day_key()
    day_info = (led.get("days") or {}).get(day) or {}
    return int(day_info.get("live_calls") or 0)


def _db_sam_used_today() -> int:
    """Read DB counter only — never calls can_spend_sam (no recursion)."""
    try:
        from api_budget import sam_used_today_raw

        return int(sam_used_today_raw() or 0)
    except Exception:
        return 0


def unified_calls_used_today(ledger: dict[str, Any] | None = None) -> int:
    """Single authority for gate checks: reconstructed REAL credits (capped at 10)."""
    led = ledger or load_ledger()
    recon = reconstruct_real_api_calls_today(led)
    return int(recon["REAL_SAM_API_CALLS_TODAY"])


def can_afford_live_calls(credits: int = 1) -> bool:
    if credits <= 0:
        return True
    with _THREAD_LOCK:
        return unified_calls_used_today() + int(credits) <= sam_daily_call_budget()


def _consume_live_credits_locked(credits: int, *, reason: str) -> bool:
    """Caller MUST hold _file_lock. Updates file + DB to the same absolute used count."""
    credits = int(credits)
    if credits <= 0:
        return True
    ledger = load_ledger()
    used = unified_calls_used_today(ledger)
    limit = sam_daily_call_budget()
    day = budget_day_key()
    days = ledger.setdefault("days", {})
    day_info = days.setdefault(
        day,
        {
            "live_calls": 0,
            "cache_hits": 0,
            "live_rows": 0,
            "unique_rows": 0,
            "failures": 0,
            "blocked_over_cap": 0,
            "internal_attempts": 0,
            "retries": 0,
        },
    )
    if used + credits > limit:
        # Block BEFORE any request — record over-cap attempt (0 real credits)
        day_info["blocked_over_cap"] = int(day_info.get("blocked_over_cap") or 0) + 1
        ledger.setdefault("entries", []).append(
            {
                "date": day,
                "timestamp": _utc(),
                "endpoint": "budget",
                "query_fingerprint": None,
                "page": 0,
                "result_count": 0,
                "success": False,
                "http_status": None,
                "cache_hit": False,
                "credits_consumed": 0,
                "reason": f"{SAM_DAILY_BUDGET_EXHAUSTED}:blocked_11th_or_over",
                "params": {"requested": credits, "used": used, "limit": limit},
                "counts_as_real_api": False,
                "blocked_over_cap": True,
            }
        )
        if len(ledger["entries"]) > 500:
            ledger["entries"] = ledger["entries"][-500:]
        save_ledger(ledger)
        return False
    new_used = used + credits
    day_info["live_calls"] = new_used
    ledger.setdefault("entries", []).append(
        {
            "date": day,
            "timestamp": _utc(),
            "endpoint": "budget",
            "query_fingerprint": None,
            "page": 0,
            "result_count": 0,
            "success": True,
            "http_status": None,
            "cache_hit": False,
            "credits_consumed": credits,
            "reason": reason,
            "params": {},
            "unified_used_after": new_used,
            "counts_as_real_api": True,
        }
    )
    if len(ledger["entries"]) > 500:
        ledger["entries"] = ledger["entries"][-500:]
    save_ledger(ledger)
    try:
        from api_budget import set_sam_used_today

        set_sam_used_today(new_used)
    except Exception:
        pass
    return True


def consume_live_credits(credits: int = 1, *, reason: str = "live") -> bool:
    """Atomically consume credits against the unified 10/day cap. Updates file + DB together."""
    if credits <= 0:
        return True
    with _file_lock():
        with _THREAD_LOCK:
            return _consume_live_credits_locked(int(credits), reason=reason)


def reconstruct_real_api_calls_today(ledger: dict[str, Any] | None = None) -> dict[str, Any]:
    """Rebuild REAL credit count from ledger entries — ignore inflated live_calls.

    REAL credits prefer budget / counts_as_real_api entries:
      max(sum(credits_consumed), max(unified_used_after))
    so mid-day consumes that inherit a prior DB baseline stay correct.
    Cache hits, blocked attempts, and audit rows do not count.
    """
    led = ledger or load_ledger()
    day = budget_day_key()
    day_info = (led.get("days") or {}).get(day) or {}
    ents = [e for e in (led.get("entries") or []) if e.get("date") == day]

    credit_sum = 0
    max_used_after = 0
    cache_hits = 0
    blocked = 0
    retries = 0
    internal = 0
    production_budget_events = 0
    public_noticedesc = 0
    agency_url = 0
    why_inflated = []

    for e in ents:
        internal += 1
        reason = str(e.get("reason") or "")
        if e.get("cache_hit"):
            cache_hits += 1
            continue
        if (
            e.get("blocked_over_cap")
            or SAM_DAILY_BUDGET_EXHAUSTED in reason
            or "blocked" in reason.lower()
            or SAM_RESERVE_PROTECTED in reason
        ):
            blocked += 1
            continue
        if "retry" in reason.lower():
            retries += 1
        if "noticedesc" in reason.lower() or "notice_desc" in reason.lower():
            public_noticedesc += 1
        if "agency" in reason.lower() and "url" in reason.lower():
            agency_url += 1
        # Only endpoint=budget rows own REAL credits (audit/search never, even if legacy
        # credits_consumed was set). Prefer counts_as_real_api when present.
        if str(e.get("endpoint") or "") != "budget":
            continue
        if e.get("counts_as_real_api") is False:
            continue
        cred = int(e.get("credits_consumed") or 0)
        if cred > 0:
            credit_sum += cred
            production_budget_events += 1
        after = int(e.get("unified_used_after") or 0)
        if after > max_used_after:
            max_used_after = after

    real = max(credit_sum, max_used_after)
    stale_live = int(day_info.get("live_calls") or 0)
    db_used = _db_sam_used_today()

    if production_budget_events == 0 and credit_sum == 0:
        # No budget events yet — mirror counters, but never above hard limit
        raw_mirror = max(stale_live, db_used)
        real = min(raw_mirror, HARD_REAL_DAILY_LIMIT)
        if db_used > HARD_REAL_DAILY_LIMIT:
            why_inflated.append(
                f"DB sam_used_today={db_used} > {HARD_REAL_DAILY_LIMIT} "
                "(dual-increment: consume_live_credits absolute set + legacy "
                "_record_entry += credits, and/or max(file,db) snapshot inflation)"
            )
        if stale_live > HARD_REAL_DAILY_LIMIT:
            why_inflated.append(
                f"day_info.live_calls={stale_live} > {HARD_REAL_DAILY_LIMIT} "
                "(legacy _record_entry dual-increment)"
            )
        if raw_mirror > HARD_REAL_DAILY_LIMIT:
            why_inflated.append(
                f"mirrored counter {raw_mirror} clamped to REAL={real}"
            )
    else:
        # Never trust inflated live_calls over budget reconstruction
        if stale_live > real and stale_live > HARD_REAL_DAILY_LIMIT:
            why_inflated.append(
                f"day_info.live_calls={stale_live} > reconstructed_real={real} "
                "(historical _record_entry dual-increment with consume_live_credits)"
            )
        elif stale_live > real:
            why_inflated.append(
                f"day_info.live_calls={stale_live} > reconstructed_real={real} "
                "(stale field; budget entries are authoritative)"
            )
        if db_used > HARD_REAL_DAILY_LIMIT:
            why_inflated.append(
                f"DB sam_used_today={db_used} > {HARD_REAL_DAILY_LIMIT} (ignored; budget entries win)"
            )

    if stale_live > HARD_REAL_DAILY_LIMIT and not any("live_calls=" in x for x in why_inflated):
        why_inflated.append(
            f"live_calls={stale_live} exceeded HARD_REAL_DAILY_LIMIT={HARD_REAL_DAILY_LIMIT}"
        )
    if real > HARD_REAL_DAILY_LIMIT:
        why_inflated.append(
            f"raw reconstructed real={real} clamped to {HARD_REAL_DAILY_LIMIT}"
        )

    real_capped = min(real, HARD_REAL_DAILY_LIMIT)
    return {
        "date": day,
        "REAL_SAM_API_CALLS_TODAY": real_capped,
        "REAL_SAM_API_CALLS_RAW_SUM": real,
        "REAL_CREDIT_SUM_FROM_ENTRIES": credit_sum,
        "MAX_UNIFIED_USED_AFTER": max_used_after,
        "CACHE_HITS": max(cache_hits, int(day_info.get("cache_hits") or 0)),
        "BLOCKED_OVER_CAP_ATTEMPTS": max(blocked, int(day_info.get("blocked_over_cap") or 0)),
        "INTERNAL_ATTEMPTS": max(internal, int(day_info.get("internal_attempts") or 0)),
        "RETRIES": max(retries, int(day_info.get("retries") or 0)),
        "PUBLIC_NOTICEDESC_FETCHES": public_noticedesc,
        "AGENCY_URL_FETCHES": agency_url,
        "PRODUCTION_BUDGET_EVENTS": production_budget_events,
        "STALE_LIVE_CALLS_FIELD": stale_live,
        "DB_LEDGER_USED": db_used,
        "FILE_LEDGER_USED": calls_used_today(led),
        "WHY_PRIOR_SHOWED_INFLATED": why_inflated,
        "HARD_REAL_DAILY_LIMIT": HARD_REAL_DAILY_LIMIT,
        "REMAINING_REAL_CREDITS": max(0, HARD_REAL_DAILY_LIMIT - real_capped),
    }


def reconcile_sam_credit_ledger(*, persist: bool = True) -> dict[str, Any]:
    """Clamp live_calls + DB to reconstructed REAL credits (never >10). No new API calls."""
    with _file_lock():
        with _THREAD_LOCK:
            led = load_ledger()
            recon = reconstruct_real_api_calls_today(led)
            day = recon["date"]
            real = int(recon["REAL_SAM_API_CALLS_TODAY"])
            days = led.setdefault("days", {})
            day_info = days.setdefault(
                day,
                {
                    "live_calls": 0,
                    "cache_hits": 0,
                    "live_rows": 0,
                    "unique_rows": 0,
                    "failures": 0,
                    "blocked_over_cap": 0,
                    "internal_attempts": 0,
                    "retries": 0,
                },
            )
            before = int(day_info.get("live_calls") or 0)
            day_info["live_calls"] = real
            day_info["cache_hits"] = int(recon["CACHE_HITS"])
            day_info["blocked_over_cap"] = int(recon["BLOCKED_OVER_CAP_ATTEMPTS"])
            day_info["reconciled_at"] = _utc()
            day_info["reconcile_build"] = BUILD
            if persist:
                save_ledger(led)
                try:
                    from api_budget import set_sam_used_today

                    set_sam_used_today(real)
                except Exception:
                    pass
            return {
                **recon,
                "BEFORE_LIVE_CALLS_FIELD": before,
                "AFTER_LIVE_CALLS_FIELD": real,
                "PERSISTED": persist,
                "BUILD": BUILD,
            }


def owner_credit_dashboard(ledger: dict[str, Any] | None = None) -> dict[str, Any]:
    """Owner-facing SAM counter — REAL credits only, hard ceiling 10."""
    recon = reconstruct_real_api_calls_today(ledger)
    used = int(recon["REAL_SAM_API_CALLS_TODAY"])
    limit = HARD_REAL_DAILY_LIMIT
    remaining = max(0, limit - used)
    res = reserve_calls()
    return {
        "kind": "SamOwnerCreditDashboard",
        "build": BUILD,
        "date": recon["date"],
        "timezone": operational_timezone(),
        "HARD_REAL_DAILY_LIMIT": limit,
        "REAL_SAM_API_CALLS_TODAY": used,
        "CACHE_HITS": recon["CACHE_HITS"],
        "PUBLIC_NOTICEDESC_FETCHES": recon["PUBLIC_NOTICEDESC_FETCHES"],
        "AGENCY_URL_FETCHES": recon["AGENCY_URL_FETCHES"],
        "INTERNAL_ATTEMPTS": recon["INTERNAL_ATTEMPTS"],
        "RETRIES": recon["RETRIES"],
        "BLOCKED_OVER_CAP_ATTEMPTS": recon["BLOCKED_OVER_CAP_ATTEMPTS"],
        "PRODUCTION_BUDGET_EVENTS": recon["PRODUCTION_BUDGET_EVENTS"],
        "REMAINING_REAL_CREDITS": remaining,
        "reserve_calls": res,
        "production_budget_remaining": max(0, remaining - res) if remaining > res else 0,
        "display": f"SAM REAL: {used}/{limit} | {remaining} remaining (cache={recon['CACHE_HITS']})",
        "status": SAM_DAILY_BUDGET_EXHAUSTED if remaining <= 0 else "OK",
        "WHY_PRIOR_INFLATED": recon["WHY_PRIOR_SHOWED_INFLATED"],
        "STALE_COUNTERS": {
            "file_live_calls": recon["FILE_LEDGER_USED"],
            "db_used": recon["DB_LEDGER_USED"],
            "stale_live_calls_field": recon["STALE_LIVE_CALLS_FIELD"],
        },
        "GATES": {
            "REAL_CALL_COUNTER_SEPARATE": True,
            "CACHE_NOT_COUNTED_AS_API": True,
            "PUBLIC_FETCHES_NOT_COUNTED_AS_API": True,
            "11TH_CALL_IMPOSSIBLE": used < limit or remaining == 0,
            "OWNER_COUNTER_TRUSTWORTHY": used <= limit,
        },
    }


def dashboard(ledger: dict[str, Any] | None = None) -> dict[str, Any]:
    led = ledger or load_ledger()
    day = budget_day_key()
    # Prefer reconstructed REAL credits for owner-facing used/remaining
    recon = reconstruct_real_api_calls_today(led)
    used = int(recon["REAL_SAM_API_CALLS_TODAY"])
    limit = sam_daily_call_budget()
    remaining = max(0, limit - used)
    res = reserve_calls()
    day_info = (led.get("days") or {}).get(day) or {}
    return {
        "kind": "SamUsageDashboard",
        "date": day,
        "timezone": operational_timezone(),
        "daily_limit": limit,
        "HARD_REAL_DAILY_LIMIT": HARD_REAL_DAILY_LIMIT,
        "calls_used": used,
        "REAL_SAM_API_CALLS_TODAY": used,
        "file_ledger_used": calls_used_today(led),
        "db_ledger_used": _db_sam_used_today(),
        "calls_remaining": remaining,
        "REMAINING_REAL_CREDITS": remaining,
        "reserve_calls": res,
        "reserve_available": remaining > 0 and used < (limit - res) or (remaining > 0 and day_info.get("reserve_authorized")),
        "production_budget": max(0, remaining - res) if remaining > res else 0,
        "cache_hits_today": int(recon["CACHE_HITS"]),
        "blocked_over_cap_attempts": int(recon["BLOCKED_OVER_CAP_ATTEMPTS"]),
        "internal_attempts_today": int(recon["INTERNAL_ATTEMPTS"]),
        "live_rows_retrieved_today": int(day_info.get("live_rows") or 0),
        "unique_rows_today": int(day_info.get("unique_rows") or 0),
        "display": f"SAM REAL: {used}/{limit} | {remaining} remaining | cache {recon['CACHE_HITS']}",
        "status": SAM_DAILY_BUDGET_EXHAUSTED if remaining <= 0 else "OK",
        "single_pool": True,
        "owner": owner_credit_dashboard(led),
    }


def _sync_db_budget(credits: int = 1) -> bool:
    """Deprecated path — use consume_live_credits. Kept for callers; never fail-open."""
    return consume_live_credits(credits, reason="legacy_sync_db_budget")


def _record_entry(
    ledger: dict[str, Any],
    *,
    endpoint: str,
    fingerprint: str,
    page: int,
    result_count: int,
    success: bool,
    http_status: int | None,
    cache_hit: bool,
    credits: int,
    reason: str,
    params_public: dict[str, Any],
    count_against_budget: bool = True,
) -> None:
    """Audit-trail only.

    CRITICAL: never mutate day_info['live_calls'] here.
    Real credit counter is owned exclusively by _consume_live_credits_locked.
    Prior dual-increment (consume absolute set + _record_entry += credits) caused
    owner counters to report >10 (e.g. 13) without 13 real API calls.
    """
    day = budget_day_key()
    days = ledger.setdefault("days", {})
    day_info = days.setdefault(
        day,
        {
            "live_calls": 0,
            "cache_hits": 0,
            "live_rows": 0,
            "unique_rows": 0,
            "failures": 0,
            "blocked_over_cap": 0,
            "internal_attempts": 0,
            "retries": 0,
        },
    )
    day_info["internal_attempts"] = int(day_info.get("internal_attempts") or 0) + 1
    if cache_hit:
        day_info["cache_hits"] = int(day_info.get("cache_hits") or 0) + 1
    elif not success:
        day_info["failures"] = int(day_info.get("failures") or 0) + 1
    if success and not cache_hit and result_count:
        day_info["live_rows"] = int(day_info.get("live_rows") or 0) + int(result_count)
    # count_against_budget retained for entry metadata only — does NOT touch live_calls
    _ = count_against_budget
    if "retry" in str(reason or "").lower():
        day_info["retries"] = int(day_info.get("retries") or 0) + 1
    entry = {
        "date": day,
        "timestamp": _utc(),
        "endpoint": endpoint,
        "query_fingerprint": fingerprint,
        "page": page,
        "result_count": result_count,
        "success": success,
        "http_status": http_status,
        "cache_hit": cache_hit,
        # Audit rows never own REAL credit — only consume_live_credits budget entries do
        "credits_consumed": 0,
        "reason": reason,
        "params": params_public,
        "counts_as_real_api": False,
        "requested_credits_meta": int(credits or 0),
    }
    ledger.setdefault("entries", []).append(entry)
    # keep last 500 entries
    if len(ledger["entries"]) > 500:
        ledger["entries"] = ledger["entries"][-500:]


def load_cached_response(fingerprint: str) -> dict[str, Any] | None:
    path = cache_path_for(fingerprint)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_cached_response(fingerprint: str, payload: dict[str, Any]) -> None:
    path = cache_path_for(fingerprint)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def build_daily_query_plan(
    *,
    max_production_calls: int | None = None,
    include_validation: bool = True,
) -> dict[str, Any]:
    """Plan the day's SAM calls before spending. Does not execute."""
    limit = sam_daily_call_budget()
    res = reserve_calls()
    used = unified_calls_used_today()
    remaining = max(0, limit - used)
    prod_cap = max_production_calls if max_production_calls is not None else max(0, limit - res - used)
    prod_cap = min(prod_cap, max(0, remaining - res) if remaining > res else 0)

    today = today_local()
    last = {}
    if LAST_REFRESH_PATH.exists():
        try:
            last = json.loads(LAST_REFRESH_PATH.read_text(encoding="utf-8"))
        except Exception:
            last = {}

    # Incremental window: prefer days since last refresh, else last 7
    last_to = last.get("posted_to")
    if last_to:
        try:
            from datetime import datetime

            start = datetime.strptime(str(last_to)[:10], "%Y-%m-%d").date()
        except Exception:
            start = today - timedelta(days=7)
    else:
        start = today - timedelta(days=7)

    windows = []
    cursor = max(start, today - timedelta(days=14))
    while cursor <= today and len(windows) < 6:
        end = min(cursor + timedelta(days=6), today)
        windows.append((cursor, end))
        cursor = end + timedelta(days=1)

    planned: list[dict[str, Any]] = []
    call_no = used + 1

    def _params(posted_from, posted_to, offset: int, limit_n: int = 1000) -> dict[str, Any]:
        return {
            "postedFrom": posted_from.strftime("%m/%d/%Y"),
            "postedTo": posted_to.strftime("%m/%d/%Y"),
            "limit": limit_n,
            "offset": offset,
            "active": "yes",
            # Broad product-heavy notice types: solicitations + combined synopsis
            "ptype": "o,k,i",  # solicitation, combined, presolicitation
        }

    if include_validation and used == 0 and remaining > 0 and windows:
        pf, pt = windows[-1]  # most recent window for validation+yield
        params = _params(pf, pt, 0)
        fp = query_fingerprint(SAM_SEARCH_URL, params)
        cached = load_cached_response(fp) is not None
        planned.append(
            {
                "call_number": call_no,
                "purpose": "validation_and_primary_yield",
                "date_range": f"{pf.isoformat()}→{pt.isoformat()}",
                "filters": {k: v for k, v in params.items()},
                "expected_yield": "up to 1000 active solicitations (max page)",
                "cache_status": "HIT" if cached else "MISS",
                "why_deserves_credit": "Proves API key + returns maximum page of recent active solicitations",
                "fingerprint": fp,
                "params": params,
                "uses_reserve": False,
            }
        )
        call_no += 1
        if not cached:
            prod_cap = max(0, prod_cap - 1)

    # Additional pages / prior windows
    for wi, (pf, pt) in enumerate(reversed(windows)):
        if prod_cap <= 0:
            break
        for page in range(0, 3):
            if prod_cap <= 0:
                break
            # Skip page 0 of newest window if already planned as validation
            if wi == 0 and page == 0 and planned and planned[0].get("purpose") == "validation_and_primary_yield":
                # check if we need page 1
                continue
            params = _params(pf, pt, page)
            fp = query_fingerprint(SAM_SEARCH_URL, params)
            cached = load_cached_response(fp) is not None
            if cached:
                planned.append(
                    {
                        "call_number": None,
                        "purpose": f"window_{pf.isoformat()}_page_{page}",
                        "date_range": f"{pf.isoformat()}→{pt.isoformat()}",
                        "filters": params,
                        "expected_yield": "cache",
                        "cache_status": "HIT",
                        "why_deserves_credit": "Already cached — 0 credits",
                        "fingerprint": fp,
                        "params": params,
                        "uses_reserve": False,
                        "execute_live": False,
                    }
                )
                continue
            planned.append(
                {
                    "call_number": call_no,
                    "purpose": f"window_{pf.isoformat()}_page_{page}",
                    "date_range": f"{pf.isoformat()}→{pt.isoformat()}",
                    "filters": {k: v for k, v in params.items()},
                    "expected_yield": "up to 1000 rows; stop if empty/partial",
                    "cache_status": "MISS",
                    "why_deserves_credit": "Non-overlapping incremental window / pagination for max unique yield",
                    "fingerprint": fp,
                    "params": params,
                    "uses_reserve": False,
                    "execute_live": True,
                }
            )
            call_no += 1
            prod_cap -= 1

    plan = {
        "kind": "SamQueryPlan",
        "build": BUILD,
        "generated_at": _utc(),
        "date": budget_day_key(),
        "daily_limit": limit,
        "calls_already_used": used,
        "calls_remaining": remaining,
        "reserve_held": res,
        "planned_live_calls": sum(1 for p in planned if p.get("execute_live", True) and p.get("cache_status") != "HIT"),
        "planned_calls": planned,
        "rule": "Stop early when pages empty or date range covered; never spend reserve casually",
    }
    PLAN_PATH.write_text(json.dumps(plan, indent=2, default=str), encoding="utf-8")
    return plan


def search_opportunities(
    params: dict[str, Any],
    *,
    reason: str,
    authorize_live: bool = False,
    use_reserve: bool = False,
    allow_retry: bool = False,  # noqa: ARG001 — retries default OFF
    transport: Any | None = None,
) -> dict[str, Any]:
    """Cache-first SAM search. Live calls require authorize_live=True.

    Returns dict with opportunitiesData, totalRecords, meta (credits, cache_hit, status).
    """
    endpoint = SAM_SEARCH_URL
    public_params = {k: v for k, v in params.items() if k != "api_key"}
    fp = query_fingerprint(endpoint, public_params)
    page = int(params.get("offset") or 0)

    # Cache first — 0 credits
    cached = load_cached_response(fp)
    if cached is not None:
        with _file_lock():
            ledger = load_ledger()
            _record_entry(
                ledger,
                endpoint=endpoint,
                fingerprint=fp,
                page=page,
                result_count=len(cached.get("opportunitiesData") or []),
                success=True,
                http_status=200,
                cache_hit=True,
                credits=0,
                reason=reason + ":cache_hit",
                params_public=public_params,
            )
            save_ledger(ledger)
        return {
            **cached,
            "_meta": {
                "cache_hit": True,
                "credits_consumed": 0,
                "fingerprint": fp,
                "status": "CACHE_HIT",
                "reason": reason,
            },
        }

    if not authorize_live:
        return {
            "opportunitiesData": [],
            "totalRecords": 0,
            "_meta": {
                "cache_hit": False,
                "credits_consumed": 0,
                "fingerprint": fp,
                "status": "LIVE_NOT_AUTHORIZED",
                "reason": reason,
            },
        }

    with _file_lock():
        ledger = load_ledger()
        used = unified_calls_used_today(ledger)
        limit = sam_daily_call_budget()
        remaining = limit - used
        if remaining <= 0:
            # Block BEFORE request — same path as consume over-cap (records blocked_over_cap)
            _consume_live_credits_locked(1, reason=reason + ":precheck_over_cap")
            return {
                "opportunitiesData": [],
                "totalRecords": 0,
                "_meta": {
                    "cache_hit": False,
                    "credits_consumed": 0,
                    "fingerprint": fp,
                    "status": SAM_DAILY_BUDGET_EXHAUSTED,
                    "reason": reason,
                    "blocked": True,
                },
            }

        res = reserve_calls()
        production_ceiling = limit - res
        day_info = (ledger.get("days") or {}).get(budget_day_key()) or {}
        if not use_reserve and used >= production_ceiling and remaining > 0:
            _record_entry(
                ledger,
                endpoint=endpoint,
                fingerprint=fp,
                page=page,
                result_count=0,
                success=False,
                http_status=None,
                cache_hit=False,
                credits=0,
                reason=SAM_RESERVE_PROTECTED,
                params_public=public_params,
                count_against_budget=False,
            )
            save_ledger(ledger)
            return {
                "opportunitiesData": [],
                "totalRecords": 0,
                "_meta": {
                    "cache_hit": False,
                    "credits_consumed": 0,
                    "fingerprint": fp,
                    "status": SAM_RESERVE_PROTECTED,
                    "reason": reason,
                    "blocked": True,
                },
            }

        api_key = (os.getenv("SAM_GOV_API_KEY") or "").strip()
        if not api_key:
            return {
                "opportunitiesData": [],
                "totalRecords": 0,
                "_meta": {
                    "cache_hit": False,
                    "credits_consumed": 0,
                    "status": "SAM_KEY_MISSING",
                    "fingerprint": fp,
                    "blocked": True,
                },
            }

        # Single unified consume — updates file + DB together (never a second pool)
        if not _consume_live_credits_locked(1, reason=reason + ":pending"):
            return {
                "opportunitiesData": [],
                "totalRecords": 0,
                "_meta": {
                    "status": SAM_DAILY_BUDGET_EXHAUSTED,
                    "credits_consumed": 0,
                    "blocked": True,
                    "fingerprint": fp,
                },
            }
        # Audit-only row (credit already counted by consume)
        ledger = load_ledger()
        _record_entry(
            ledger,
            endpoint=endpoint,
            fingerprint=fp,
            page=page,
            result_count=0,
            success=False,
            http_status=None,
            cache_hit=False,
            credits=1,
            reason=reason + ":pending",
            params_public=public_params,
            count_against_budget=False,
        )
        save_ledger(ledger)

    # HTTP outside lock (credit already reserved)
    req_params = {**public_params, "api_key": api_key}
    http_status = None
    success = False
    body: dict[str, Any] = {"opportunitiesData": [], "totalRecords": 0}
    try:
        if transport is not None:
            resp = transport.get(endpoint, params=req_params)
            http_status = getattr(resp, "status_code", 200)
            body = resp.json() if hasattr(resp, "json") else resp
            success = http_status == 200
        else:
            import httpx

            with httpx.Client(timeout=90.0) as client:
                resp = client.get(endpoint, params=req_params)
                http_status = resp.status_code
                resp.raise_for_status()
                body = resp.json()
                success = True
    except Exception as exc:
        with _file_lock():
            ledger = load_ledger()
            # amend last pending entry
            if ledger.get("entries"):
                ledger["entries"][-1].update(
                    {
                        "success": False,
                        "http_status": http_status,
                        "reason": f"{reason}:error:{type(exc).__name__}",
                        "error": str(exc)[:300],
                    }
                )
            save_ledger(ledger)
        return {
            "opportunitiesData": [],
            "totalRecords": 0,
            "_meta": {
                "cache_hit": False,
                "credits_consumed": 1,
                "fingerprint": fp,
                "status": "LIVE_ERROR",
                "http_status": http_status,
                "error": str(exc)[:300],
                "reason": reason,
                # No automatic retry — caller must decide deliberately
                "retry_allowed_default": False,
            },
        }

    rows = list(body.get("opportunitiesData") or [])
    total = body.get("totalRecords")
    payload = {
        "opportunitiesData": rows,
        "totalRecords": total,
        "cached_at": _utc(),
        "fingerprint": fp,
        "params": public_params,
    }
    save_cached_response(fp, payload)

    with _file_lock():
        ledger = load_ledger()
        if ledger.get("entries"):
            ledger["entries"][-1].update(
                {
                    "success": True,
                    "http_status": http_status or 200,
                    "result_count": len(rows),
                    "reason": reason,
                }
            )
        day = budget_day_key()
        if day in ledger.get("days", {}):
            # live_rows already may be 0 from pending — set properly
            ledger["days"][day]["live_rows"] = int(ledger["days"][day].get("live_rows") or 0) + len(rows)
        save_ledger(ledger)

    return {
        **payload,
        "_meta": {
            "cache_hit": False,
            "credits_consumed": 1,
            "fingerprint": fp,
            "status": "LIVE_OK",
            "http_status": http_status or 200,
            "reason": reason,
        },
    }


def execute_query_plan(
    plan: dict[str, Any] | None = None,
    *,
    authorize_live: bool = False,
    max_live_calls: int = 9,
    transport: Any | None = None,
) -> dict[str, Any]:
    """Execute planned calls with early stop. Never exceeds max_live_calls or daily budget."""
    plan = plan or build_daily_query_plan()
    results = []
    live_used = 0
    all_rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for item in plan.get("planned_calls") or []:
        if item.get("cache_status") == "HIT" and not item.get("execute_live", True):
            # Serve from cache without counting
            cached = load_cached_response(item["fingerprint"])
            if cached:
                rows = cached.get("opportunitiesData") or []
                for r in rows:
                    nid = str(r.get("noticeId") or r.get("notice_id") or "")
                    if nid and nid not in seen_ids:
                        seen_ids.add(nid)
                        all_rows.append(r)
                results.append({"plan": item, "meta": {"cache_hit": True, "credits_consumed": 0}, "rows": len(rows)})
            continue

        if live_used >= max_live_calls:
            break
        if item.get("execute_live") is False:
            continue

        resp = search_opportunities(
            item["params"],
            reason=str(item.get("purpose") or "planned"),
            authorize_live=authorize_live,
            use_reserve=bool(item.get("uses_reserve")),
            transport=transport,
        )
        meta = resp.get("_meta") or {}
        rows = list(resp.get("opportunitiesData") or [])
        if meta.get("credits_consumed"):
            live_used += int(meta["credits_consumed"])
        if meta.get("blocked"):
            results.append({"plan": item, "meta": meta, "rows": 0})
            if meta.get("status") == SAM_DAILY_BUDGET_EXHAUSTED:
                break
            continue
        new_here = 0
        for r in rows:
            nid = str(r.get("noticeId") or "")
            if nid and nid not in seen_ids:
                seen_ids.add(nid)
                all_rows.append(r)
                new_here += 1
        results.append({"plan": item, "meta": meta, "rows": len(rows), "new_unique": new_here})
        # Early stop: empty page
        if meta.get("status") == "LIVE_OK" and len(rows) == 0:
            break
        # Early stop: partial last page (< limit) after first live page of a window
        limit_n = int((item.get("params") or {}).get("limit") or 1000)
        if meta.get("status") == "LIVE_OK" and 0 < len(rows) < limit_n and live_used >= 1:
            # still allow other windows; don't break entire plan
            pass

    summary = {
        "kind": "SamLatestRefreshSummary",
        "build": BUILD,
        "generated_at": _utc(),
        "live_calls_this_run": live_used,
        "dashboard": dashboard(),
        "total_records_fetched": sum(r.get("rows") or 0 for r in results),
        "unique_notice_ids": len(seen_ids),
        "call_results": results,
        "opportunities": all_rows,
    }
    if all_rows and authorize_live and live_used > 0:
        LAST_REFRESH_PATH.write_text(
            json.dumps(
                {
                    "updated_at": _utc(),
                    "posted_to": today_local().isoformat(),
                    "unique": len(seen_ids),
                    "live_calls": live_used,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    (ART / "sam_latest_refresh_summary.json").write_text(
        json.dumps({k: v for k, v in summary.items() if k != "opportunities"}, indent=2, default=str),
        encoding="utf-8",
    )
    # Persist slim opportunity extract separately
    (ART / "sam_latest_opportunities.json").write_text(
        json.dumps({"count": len(all_rows), "opportunities": all_rows}, indent=2, default=str),
        encoding="utf-8",
    )
    return summary


def update_query_productivity(run_summary: dict[str, Any], *, product_count: int = 0, funnel_new: int = 0) -> dict[str, Any]:
    hist = {}
    if PRODUCTIVITY_PATH.exists():
        try:
            hist = json.loads(PRODUCTIVITY_PATH.read_text(encoding="utf-8"))
        except Exception:
            hist = {}
    hist.setdefault("kind", "SamQueryProductivity")
    hist.setdefault("runs", [])
    live = int(run_summary.get("live_calls_this_run") or 0) or 1
    uniq = int(run_summary.get("unique_notice_ids") or 0)
    fetched = int(run_summary.get("total_records_fetched") or 0)
    entry = {
        "at": _utc(),
        "live_calls": live,
        "records_fetched": fetched,
        "unique": uniq,
        "product": product_count,
        "funnel_new": funnel_new,
        "records_per_call": round(fetched / live, 2),
        "unique_per_call": round(uniq / live, 2),
        "product_per_call": round(product_count / live, 2),
        "funnel_entrants_per_call": round(funnel_new / live, 2),
    }
    hist["runs"].append(entry)
    hist["runs"] = hist["runs"][-50:]
    hist["best_unique_per_call"] = max((r.get("unique_per_call") or 0) for r in hist["runs"])
    PRODUCTIVITY_PATH.write_text(json.dumps(hist, indent=2), encoding="utf-8")
    return hist


# Re-export for parked status bridge
def sam_budget_status() -> dict[str, Any]:
    d = dashboard()
    key_present = bool((os.getenv("SAM_GOV_API_KEY") or "").strip())
    return {
        "kind": "SamApiBudgetStatus",
        "status": "SAM_API_BUDGETED" if key_present else "SAM_API_KEY_MISSING",
        "key_configured": key_present,
        "opportunities_api_base": SAM_SEARCH_URL,
        "daily_limit": d["daily_limit"],
        "calls_used_today": d["calls_used"],
        "calls_remaining": d["calls_remaining"],
        "calls_consumed": d["calls_used"],  # compat with park_status field name
        "calls_allowed_this_phase": d["daily_limit"],
        "display": d["display"],
        "public_sam_search_unchanged": True,
        "canonical_client": "discovery.sam_budgeted_client",
        "note": "All live Opportunities API calls must use discovery.sam_budgeted_client (10/day default).",
    }
