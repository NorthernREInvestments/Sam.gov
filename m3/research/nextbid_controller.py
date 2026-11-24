"""Centralized NextBid request controller — fail-closed budget enforcement.

ALL metered NextBid HTTP must go through `metered_request`.
No worker, endpoint, scheduler, or agent may bypass this module.

Defaults (post-owner-authorization only):
  - 50 metered calls / job
  - 100 metered calls / day  (HARD OWNER CEILING — never exceed without explicit auth)
  - keep ≥1000 reserve of monthly allowance
  - emergency stop / disabled → STOP

Build 002K: permanent hard limits + optional bounded authorized test session.
Build 002M: one-time owner-authorized discovery backfill (search-only ≤300).
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from m3.config import get_settings
from m3.research.nextbid_killswitch import emergency_stop_status

BASE_URL = "https://api.nextbid.us/v1"
MAX_CONCURRENCY = 4
OWNER_HARD_DAILY_CEILING = 100  # Owner directive: never more than 100 credits/day system-wide
DEFAULT_JOB_LIMIT = 50
DEFAULT_MONTHLY_RESERVE = 1000
OWNER_HARD_TEST_CEILING = 100  # Build 002K authorized test max
OWNER_HARD_BUILD_002M_CEILING = 300  # One-time discovery backfill only
BUILD_002M_SCOPE = "build_002m_discovery_backfill"
BUILD_002K_SCOPE = "build_002k_bounded_test"
BUILD_002Q_SCOPE = "build_002q_controlled_production"
BUILD_002M_ALLOWED_ENDPOINTS = frozenset({"search"})
BUILD_002Q_ALLOWED_ENDPOINTS = frozenset(
    {"search", "get_opportunity", "list_documents", "list_files", "read_file"}
)
OWNER_HARD_BUILD_002Q_CAMPAIGN = 1646
OWNER_HARD_BUILD_002Q_LISTING = 400
OWNER_HARD_BUILD_002Q_DETAIL = 1246
LISTING_ENDPOINTS = frozenset({"search"})
DETAIL_ENDPOINTS = frozenset(
    {"get_opportunity", "list_documents", "list_files", "read_file"}
)

_lock = threading.RLock()
_sem = threading.Semaphore(MAX_CONCURRENCY)
_clients: threading.local = threading.local()


class NextBidControllerError(RuntimeError):
    pass


class NextBidBudgetExhausted(NextBidControllerError):
    pass


class NextBidUnauthorized(NextBidControllerError):
    pass


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ledger_dir() -> Path:
    p = get_settings().data_root() / "nextbid_ledger"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _ledger_path() -> Path:
    return _ledger_dir() / "consumption_ledger.jsonl"


def _state_path() -> Path:
    return _ledger_dir() / "budget_state.json"


def _auth_path() -> Path:
    return _ledger_dir() / "owner_authorization.json"


def load_budget_state() -> dict[str, Any]:
    path = _state_path()
    if not path.exists():
        return {
            "day": str(date.today()),
            "day_used": 0,
            "month_used_local": 0,
            "jobs": {},
            "account_remaining": None,
            "account_allowance": None,
            "account_used": None,
            "updated_at": None,
        }
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {
            "day": str(date.today()),
            "day_used": 0,
            "month_used_local": 0,
            "jobs": {},
            "corrupt": True,
        }


def save_budget_state(state: dict[str, Any]) -> None:
    state["updated_at"] = _utc()
    tmp = _state_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
    tmp.replace(_state_path())


def append_ledger(entry: dict[str, Any]) -> None:
    entry = dict(entry)
    entry.setdefault("at", _utc())
    with _ledger_path().open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def owner_authorization() -> dict[str, Any] | None:
    path = _auth_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not data.get("authorized"):
        return None
    return data


def write_owner_authorization(
    *,
    authorized: bool,
    max_additional_calls: int | None = None,
    note: str = "",
    scope: str | None = None,
    test_call_cap: int | None = None,
    allowed_endpoints: list[str] | None = None,
    daily_limit_override: int | None = None,
    reset_test_used: bool = False,
) -> Path:
    """Write explicit owner authorization. Never auto-upgrade budgets beyond documented ceilings."""
    payload = {
        "authorized": bool(authorized),
        "max_additional_calls": max_additional_calls,
        "scope": scope,
        "test_call_cap": test_call_cap,
        "allowed_endpoints": allowed_endpoints,
        "daily_limit_override": daily_limit_override,
        "note": note,
        "written_at": _utc(),
    }
    _auth_path().write_text(json.dumps(payload, indent=2), encoding="utf-8")
    if reset_test_used:
        with _lock:
            state = load_budget_state()
            state["test_used"] = 0
            state["test_reserved"] = 0
            # New authorized window: do not carry prior build test burn into 002M cap
            save_budget_state(state)
    return _auth_path()


def revoke_owner_authorization() -> None:
    path = _auth_path()
    if path.exists():
        path.unlink()


def _limits() -> dict[str, int]:
    settings = get_settings()
    auth = owner_authorization()
    reserve = int(getattr(settings, "nextbid_budget_reserve", DEFAULT_MONTHLY_RESERVE) or DEFAULT_MONTHLY_RESERVE)
    job = min(
        int(getattr(settings, "nextbid_job_call_limit", DEFAULT_JOB_LIMIT) or DEFAULT_JOB_LIMIT),
        DEFAULT_JOB_LIMIT,
    )

    # Build 002M one-time exception: search-only backfill may use up to 300 for day+test caps
    if auth and auth.get("authorized") and auth.get("scope") == BUILD_002M_SCOPE:
        test_cap = min(
            OWNER_HARD_BUILD_002M_CEILING,
            int(auth.get("test_call_cap") or OWNER_HARD_BUILD_002M_CEILING),
        )
        daily_override = auth.get("daily_limit_override")
        if daily_override is None:
            daily = test_cap
        else:
            daily = min(OWNER_HARD_BUILD_002M_CEILING, int(daily_override))
        return {
            "daily": daily,
            "job": job,
            "monthly_reserve": reserve,
            "test": test_cap,
            "owner_hard_daily_ceiling": OWNER_HARD_DAILY_CEILING,
            "owner_hard_test_ceiling": OWNER_HARD_TEST_CEILING,
            "owner_hard_build_002m_ceiling": OWNER_HARD_BUILD_002M_CEILING,
            "scope": BUILD_002M_SCOPE,
            "allowed_endpoints": sorted(BUILD_002M_ALLOWED_ENDPOINTS),
        }

    # Build 002Q controlled production: campaign ≤1646 but daily≤100 and job≤50 still bind
    if auth and auth.get("authorized") and auth.get("scope") == BUILD_002Q_SCOPE:
        campaign = min(
            OWNER_HARD_BUILD_002Q_CAMPAIGN,
            int(auth.get("test_call_cap") or OWNER_HARD_BUILD_002Q_CAMPAIGN),
        )
        daily = min(
            OWNER_HARD_DAILY_CEILING,
            int(getattr(settings, "nextbid_daily_call_limit", 100) or 100),
        )
        return {
            "daily": daily,
            "job": job,
            "monthly_reserve": reserve,
            "test": campaign,  # campaign ceiling tracked via test_used
            "listing_cap": OWNER_HARD_BUILD_002Q_LISTING,
            "detail_cap": OWNER_HARD_BUILD_002Q_DETAIL,
            "campaign_cap": OWNER_HARD_BUILD_002Q_CAMPAIGN,
            "owner_hard_daily_ceiling": OWNER_HARD_DAILY_CEILING,
            "owner_hard_test_ceiling": OWNER_HARD_TEST_CEILING,
            "scope": BUILD_002Q_SCOPE,
            "allowed_endpoints": sorted(BUILD_002Q_ALLOWED_ENDPOINTS),
        }

    daily = min(int(getattr(settings, "nextbid_daily_call_limit", 100) or 100), OWNER_HARD_DAILY_CEILING)
    job = min(job, daily)
    test_cap = OWNER_HARD_TEST_CEILING
    if auth and auth.get("test_call_cap") is not None:
        test_cap = min(OWNER_HARD_TEST_CEILING, int(auth["test_call_cap"]))
    return {
        "daily": daily,
        "job": job,
        "monthly_reserve": reserve,
        "test": test_cap,
        "owner_hard_daily_ceiling": OWNER_HARD_DAILY_CEILING,
        "owner_hard_test_ceiling": OWNER_HARD_TEST_CEILING,
        "scope": (auth or {}).get("scope"),
        "allowed_endpoints": None,
    }


def acquisition_locked() -> bool:
    """Fail-closed by default.

    Unlock only when explicit owner authorization exists.
    Temporary scopes that may run while emergency-stop env flags remain set:
      - build_002k_bounded_test
      - build_002m_discovery_backfill (search-only ≤300)
    All other metered access requires enabled=true, emergency_stop=false,
    killswitch clear, and authorized=true.
    """
    auth = owner_authorization()
    if auth and auth.get("authorized") and auth.get("scope") in {
        BUILD_002K_SCOPE,
        BUILD_002M_SCOPE,
        BUILD_002Q_SCOPE,
    }:
        return False
    settings = get_settings()
    kill = emergency_stop_status()
    if kill.get("engaged") or settings.nextbid_emergency_stop or not settings.nextbid_enabled:
        return True
    if auth is None or not auth.get("authorized"):
        return True
    return False


def _endpoint_allowed(endpoint: str) -> bool:
    auth = owner_authorization()
    if not auth or not auth.get("authorized"):
        return True  # non-scoped paths use normal lock; endpoint filter only for restricted scopes
    scope = auth.get("scope")
    if scope == BUILD_002M_SCOPE:
        allowed = set(auth.get("allowed_endpoints") or BUILD_002M_ALLOWED_ENDPOINTS)
        return endpoint in allowed
    if scope == BUILD_002Q_SCOPE:
        allowed = set(auth.get("allowed_endpoints") or BUILD_002Q_ALLOWED_ENDPOINTS)
        return endpoint in allowed
    return True


def controller_status() -> dict[str, Any]:
    with _lock:
        state = load_budget_state()
        today = str(date.today())
        if state.get("day") != today:
            state["day"] = today
            state["day_used"] = 0
            # Do not reset test_used across days unless new auth — keep until revoked
        limits = _limits()
        day_used = int(state.get("day_used") or 0)
        test_used = int(state.get("test_used") or 0)
        rem_daily = max(0, limits["daily"] - day_used)
        rem_test = max(0, limits["test"] - test_used)
        effective = min(rem_daily, rem_test, limits["job"])
        if state.get("account_remaining") is not None:
            spendable_month = max(0, int(state["account_remaining"]) - limits["monthly_reserve"])
            effective = min(effective, spendable_month)
        return {
            "locked": acquisition_locked(),
            "killswitch": emergency_stop_status(),
            "owner_authorization": owner_authorization(),
            "limits": limits,
            "budget_state": {
                "day": state.get("day"),
                "day_used": day_used,
                "test_used": test_used,
                "month_used_local": state.get("month_used_local"),
                "account_remaining": state.get("account_remaining"),
                "account_used": state.get("account_used"),
                "account_allowance": state.get("account_allowance"),
                "effective_available": effective,
            },
            "resume_requires_owner_authorization": True,
        }


def reset_http_client() -> None:
    """Drop sticky httpx client (recover from hung sockets)."""
    client = getattr(_clients, "client", None)
    if client is not None and not client.is_closed:
        try:
            client.close()
        except Exception:
            pass
    _clients.client = None


def _http() -> httpx.Client:
    client = getattr(_clients, "client", None)
    if client is None or client.is_closed:
        key = (get_settings().nextbid_api_key or "").strip()
        client = httpx.Client(
            base_url=BASE_URL,
            timeout=httpx.Timeout(30.0, connect=8.0),
            headers={
                "X-API-Key": key,
                "Accept": "application/json",
                "User-Agent": "M3Supply-NextBidController/002J",
            },
            follow_redirects=True,
        )
        _clients.client = client
    return client


def refresh_account_usage(*, allow_when_locked: bool = True) -> dict[str, Any]:
    """FREE /v1/usage — allowed during emergency stop for audit/verify."""
    settings = get_settings()
    if not (settings.nextbid_api_key or "").strip():
        raise NextBidUnauthorized("NEXTBID_API_KEY missing")
    try:
        resp = _http().get("/usage", timeout=httpx.Timeout(45.0, connect=15.0))
    except httpx.HTTPError as exc:
        append_ledger({"op": "usage", "ok": False, "error": type(exc).__name__, "metered": False})
        reset_http_client()
        raise NextBidControllerError(f"usage_unavailable:{type(exc).__name__}") from exc
    try:
        data = resp.json()
    except Exception as exc:
        raise NextBidControllerError("usage_non_json") from exc
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else data
    rem = used = allowance = None
    if isinstance(usage, dict):
        rem = usage.get("remaining")
        used = usage.get("used")
        allowance = usage.get("allowance")
        if rem is None and isinstance(usage.get("requests"), dict):
            rem = usage["requests"].get("remaining")
        if used is None and isinstance(usage.get("requests"), dict):
            used = usage["requests"].get("used")
        if allowance is None and isinstance(usage.get("requests"), dict):
            allowance = usage["requests"].get("allowance")
    # Prefer response headers when present
    def _i(h: str) -> int | None:
        v = resp.headers.get(h)
        try:
            return int(float(v)) if v is not None else None
        except ValueError:
            return None

    rem = _i("x-quota-remaining") if _i("x-quota-remaining") is not None else rem
    used = _i("x-usage-used") if _i("x-usage-used") is not None else used
    with _lock:
        state = load_budget_state()
        if rem is not None:
            state["account_remaining"] = int(rem)
        if used is not None:
            state["account_used"] = int(used)
        if allowance is not None:
            state["account_allowance"] = int(allowance)
        save_budget_state(state)
    append_ledger(
        {
            "op": "usage",
            "ok": resp.status_code == 200,
            "metered": False,
            "remaining": rem,
            "used": used,
            "allowance": allowance,
        }
    )
    return {
        "ok": resp.status_code == 200,
        "remaining": rem,
        "used": used,
        "allowance": allowance,
        "raw_ok": data.get("ok") if isinstance(data, dict) else None,
    }


def _reserve(job_id: str, units: int = 1, *, endpoint: str | None = None) -> dict[str, Any]:
    """Atomic budget reservation. Raises if cannot spend."""
    if acquisition_locked():
        raise NextBidUnauthorized(
            "acquisition_locked: emergency stop or missing owner authorization"
        )
    if units < 1:
        raise NextBidControllerError("invalid_units")
    limits = _limits()
    with _lock:
        state = load_budget_state()
        today = str(date.today())
        if state.get("day") != today:
            state["day"] = today
            state["day_used"] = 0
        rem = state.get("account_remaining")
        used = state.get("account_used")
        allowance = state.get("account_allowance")
        if rem is None or used is None or allowance is None:
            raise NextBidUnauthorized("account_usage_unknown_STOP")
        # Monthly reserve floor
        if int(rem) - units < limits["monthly_reserve"]:
            raise NextBidBudgetExhausted(
                f"monthly_reserve_floor:{limits['monthly_reserve']}_remaining:{rem}"
            )
        day_used = int(state.get("day_used") or 0)
        day_reserved = int(state.get("day_reserved") or 0)
        test_used = int(state.get("test_used") or 0)
        test_reserved = int(state.get("test_reserved") or 0)
        day_committed = day_used + day_reserved
        test_committed = test_used + test_reserved
        if day_committed + units > limits["daily"]:
            raise NextBidBudgetExhausted(f"daily_limit:{limits['daily']}_used:{day_committed}")
        if test_committed + units > limits["test"]:
            raise NextBidBudgetExhausted(f"test_limit:{limits['test']}_used:{test_committed}")
        # 002Q listing/detail campaign buckets
        listing_cap = limits.get("listing_cap")
        detail_cap = limits.get("detail_cap")
        bucket = None
        if listing_cap is not None or detail_cap is not None:
            ep = (endpoint or "").lower()
            if ep in LISTING_ENDPOINTS:
                bucket = "listing"
                listing_committed = int(state.get("campaign_listing_used") or 0) + int(
                    state.get("campaign_listing_reserved") or 0
                )
                if listing_committed + units > int(listing_cap or 0):
                    raise NextBidBudgetExhausted(
                        f"campaign_listing_cap:{listing_cap}_used:{listing_committed}"
                    )
            elif ep in DETAIL_ENDPOINTS:
                bucket = "detail"
                detail_committed = int(state.get("campaign_detail_used") or 0) + int(
                    state.get("campaign_detail_reserved") or 0
                )
                if detail_committed + units > int(detail_cap or 0):
                    raise NextBidBudgetExhausted(
                        f"campaign_detail_cap:{detail_cap}_used:{detail_committed}"
                    )
        jobs = state.setdefault("jobs", {})
        job = jobs.setdefault(job_id, {"used": 0, "reserved": 0})
        if int(job.get("used", 0)) + int(job.get("reserved", 0)) + units > limits["job"]:
            raise NextBidBudgetExhausted(f"job_limit:{limits['job']}_job:{job_id}")
        # Effective lowest remaining (including in-flight reservations)
        effective = min(
            limits["daily"] - day_committed,
            limits["test"] - test_committed,
            limits["job"] - (int(job.get("used", 0)) + int(job.get("reserved", 0))),
            int(rem) - limits["monthly_reserve"],
        )
        if effective < units:
            raise NextBidBudgetExhausted(f"effective_available:{effective}")
        # Reserve (counts against concurrent ceilings immediately)
        job["reserved"] = int(job.get("reserved", 0)) + units
        state["pending_reservations"] = int(state.get("pending_reservations") or 0) + units
        state["day_reserved"] = day_reserved + units
        state["test_reserved"] = test_reserved + units
        if bucket == "listing":
            state["campaign_listing_reserved"] = int(state.get("campaign_listing_reserved") or 0) + units
        elif bucket == "detail":
            state["campaign_detail_reserved"] = int(state.get("campaign_detail_reserved") or 0) + units
        # Optimistically reduce remaining for concurrent safety
        state["account_remaining"] = int(rem) - units
        save_budget_state(state)
        reservation_id = str(uuid.uuid4())
        append_ledger(
            {
                "op": "reserve",
                "reservation_id": reservation_id,
                "job_id": job_id,
                "units": units,
                "endpoint": endpoint,
                "bucket": bucket,
                "day_used": day_used,
                "test_used": test_used,
                "remaining_after_reserve": state["account_remaining"],
            }
        )
        return {
            "reservation_id": reservation_id,
            "units": units,
            "job_id": job_id,
            "endpoint": endpoint,
            "bucket": bucket,
        }


def _commit(reservation: dict[str, Any], *, endpoint: str, ok: bool, meta: dict | None = None) -> None:
    units = int(reservation["units"])
    job_id = reservation["job_id"]
    bucket = reservation.get("bucket")
    with _lock:
        state = load_budget_state()
        today = str(date.today())
        if state.get("day") != today:
            state["day"] = today
            state["day_used"] = 0
        jobs = state.setdefault("jobs", {})
        job = jobs.setdefault(job_id, {"used": 0, "reserved": 0})
        job["reserved"] = max(0, int(job.get("reserved", 0)) - units)
        if bucket == "listing":
            state["campaign_listing_reserved"] = max(
                0, int(state.get("campaign_listing_reserved") or 0) - units
            )
        elif bucket == "detail":
            state["campaign_detail_reserved"] = max(
                0, int(state.get("campaign_detail_reserved") or 0) - units
            )
        if ok:
            job["used"] = int(job.get("used", 0)) + units
            state["day_used"] = int(state.get("day_used") or 0) + units
            state["test_used"] = int(state.get("test_used") or 0) + units
            state["month_used_local"] = int(state.get("month_used_local") or 0) + units
            if bucket == "listing":
                state["campaign_listing_used"] = int(state.get("campaign_listing_used") or 0) + units
            elif bucket == "detail":
                state["campaign_detail_used"] = int(state.get("campaign_detail_used") or 0) + units
            by_ep = state.setdefault("by_endpoint", {})
            by_ep[endpoint] = int(by_ep.get(endpoint) or 0) + units
        else:
            # Release optimistic remaining reduction on failure
            if state.get("account_remaining") is not None:
                state["account_remaining"] = int(state["account_remaining"]) + units
        state["pending_reservations"] = max(
            0, int(state.get("pending_reservations") or 0) - units
        )
        state["day_reserved"] = max(0, int(state.get("day_reserved") or 0) - units)
        state["test_reserved"] = max(0, int(state.get("test_reserved") or 0) - units)
        save_budget_state(state)
        append_ledger(
            {
                "op": "commit" if ok else "release",
                "reservation_id": reservation.get("reservation_id"),
                "job_id": job_id,
                "endpoint": endpoint,
                "bucket": bucket,
                "units": units,
                "ok": ok,
                "meta": meta or {},
            }
        )


def check_provider_usage_discrepancy(*, raise_on_stop: bool = True) -> dict[str, Any]:
    """Compare local accounting to provider usage. Fail closed on unexplained burn."""
    with _lock:
        state = load_budget_state()
        if state.get("provider_discrepancy_stop"):
            info = {
                "stop": True,
                "reason": state.get("provider_discrepancy_reason") or "provider_discrepancy_stop",
                "account_used": state.get("account_used"),
                "reconciled_provider_used": state.get("reconciled_provider_used"),
                "local_metered_since_reconcile": state.get("local_metered_since_reconcile"),
            }
            if raise_on_stop:
                raise NextBidBudgetExhausted(f"provider_usage_discrepancy:{info['reason']}")
            return info
        account_used = state.get("account_used")
        reconciled = state.get("reconciled_provider_used")
        local_since = int(state.get("local_metered_since_reconcile") or 0)
        if account_used is None or reconciled is None:
            return {"stop": False, "reason": "no_baseline_yet"}
        unexplained = int(account_used) - int(reconciled) - local_since
        # Allow 1-unit lag for in-flight header sync
        if unexplained > 1:
            state["provider_discrepancy_stop"] = True
            state["provider_discrepancy_reason"] = (
                f"unexplained_provider_burn:{unexplained}_account_used:{account_used}"
                f"_reconciled:{reconciled}_local_since:{local_since}"
            )
            save_budget_state(state)
            append_ledger(
                {
                    "op": "provider_discrepancy_stop",
                    "metered": False,
                    "unexplained": unexplained,
                    "account_used": account_used,
                    "reconciled": reconciled,
                    "local_since": local_since,
                }
            )
            if raise_on_stop:
                raise NextBidBudgetExhausted(state["provider_discrepancy_reason"])
            return {"stop": True, "reason": state["provider_discrepancy_reason"], "unexplained": unexplained}
        return {"stop": False, "unexplained": unexplained}


def set_usage_reconcile_baseline(*, account_used: int, note: str = "") -> dict[str, Any]:
    """Owner/repair: lock local accounting to current provider used (no metered spend)."""
    with _lock:
        state = load_budget_state()
        state["reconciled_provider_used"] = int(account_used)
        state["local_metered_since_reconcile"] = 0
        state["provider_discrepancy_stop"] = False
        state["provider_discrepancy_reason"] = None
        state["reconcile_note"] = note
        state["reconciled_at"] = _utc()
        save_budget_state(state)
        append_ledger(
            {
                "op": "usage_reconcile_baseline",
                "metered": False,
                "account_used": account_used,
                "note": note,
            }
        )
        return {
            "reconciled_provider_used": account_used,
            "local_metered_since_reconcile": 0,
            "at": state["reconciled_at"],
        }


def metered_request(
    method: str,
    path: str,
    *,
    job_id: str,
    endpoint: str,
    params: dict[str, Any] | None = None,
    nb_id: str | None = None,
    max_retries: int = 0,
    use_cache: bool = True,
) -> dict[str, Any]:
    """ONLY entrypoint for metered NextBid calls. Retries count against budget.

    Order: lock → durable-store hit → HTTP cache hit → reserve → transmit →
    persist raw body BEFORE parse → commit. Uncertain outcomes count as consumed.
    """
    if acquisition_locked():
        raise NextBidUnauthorized("acquisition_locked")
    if not _endpoint_allowed(endpoint):
        raise NextBidUnauthorized(
            f"endpoint_forbidden_under_authorization:{endpoint}"
        )
    check_provider_usage_discrepancy(raise_on_stop=True)
    # Build 002M: path-level guard — listing search only (no detail/docs/files)
    auth = owner_authorization()
    if auth and auth.get("scope") == BUILD_002M_SCOPE:
        p = (path or "").rstrip("/")
        if p != "/opportunities" or method.upper() != "GET":
            raise NextBidUnauthorized(f"build_002m_search_only_path_forbidden:{method}:{path}")
        if nb_id:
            raise NextBidUnauthorized("build_002m_nb_id_detail_forbidden")
    settings = get_settings()
    if not (settings.nextbid_api_key or "").strip():
        raise NextBidUnauthorized("missing_api_key")

    from m3.research.nextbid_response_store import (
        end_inflight,
        lookup_cached_response,
        persist_paid_response,
        request_identity,
        try_begin_inflight,
    )

    req_id = request_identity(method, path, params, nb_id=nb_id)

    # Durable paid-response store hit → zero metered spend
    if use_cache and method.upper() == "GET":
        durable = lookup_cached_response(method, path, params, nb_id=nb_id)
        if durable is not None:
            append_ledger(
                {
                    "op": "durable_store_hit",
                    "endpoint": endpoint,
                    "nb_id": nb_id,
                    "path": path,
                    "request_id": req_id,
                    "metered": False,
                }
            )
            return durable

        from m3.research.nextbid_cache import cache_get

        cache_parts = [path, json.dumps(params or {}, sort_keys=True, default=str)]
        hit = cache_get("http", *cache_parts)
        if hit and isinstance(hit.get("payload"), dict):
            # Promote legacy cache into durable store (no re-pay)
            try:
                persist_paid_response(
                    method=method,
                    path=path,
                    params=params,
                    endpoint=endpoint,
                    nb_id=nb_id,
                    status_code=200,
                    body=hit["payload"],
                    job_id=job_id,
                )
            except Exception:
                pass
            append_ledger(
                {
                    "op": "cache_hit",
                    "endpoint": endpoint,
                    "nb_id": nb_id,
                    "path": path,
                    "metered": False,
                }
            )
            data = dict(hit["payload"])
            data["_cache_hit"] = True
            data["_request_id"] = req_id
            return data

    # Concurrent identical request → do not double-pay
    if not try_begin_inflight(req_id):
        # Wait briefly for peer to finish, then reuse store
        import time

        for _ in range(20):
            time.sleep(0.05)
            durable = lookup_cached_response(method, path, params, nb_id=nb_id)
            if durable is not None:
                append_ledger(
                    {
                        "op": "inflight_dedupe_hit",
                        "endpoint": endpoint,
                        "request_id": req_id,
                        "metered": False,
                    }
                )
                return durable
        raise NextBidControllerError(f"inflight_duplicate_unresolved:{endpoint}:{req_id[:12]}")

    # Retries count against budget; default max_retries=0 (no auto-retry burn)
    attempts_allowed = 1 + max(0, min(int(max_retries), 1))  # hard cap 1 retry
    last_err: str | None = None
    try:
        for attempt in range(attempts_allowed):
            reservation = _reserve(job_id, 1, endpoint=endpoint)
            with _sem:
                try:
                    resp = _http().request(method, path, params=params)
                except httpx.HTTPError as exc:
                    # Uncertain outcome: treat as potentially consumed (fail closed)
                    _commit(
                        reservation,
                        endpoint=endpoint,
                        ok=True,
                        meta={
                            "error": type(exc).__name__,
                            "uncertain_consumed": True,
                            "nb_id": nb_id,
                            "attempt": attempt,
                        },
                    )
                    with _lock:
                        st = load_budget_state()
                        st["local_metered_since_reconcile"] = int(
                            st.get("local_metered_since_reconcile") or 0
                        ) + 1
                        save_budget_state(st)
                    last_err = type(exc).__name__
                    # Do not auto-retry uncertain network outcomes (prevents uncontrolled burn)
                    break

            # Sync meter from headers
            def _i(h: str) -> int | None:
                v = resp.headers.get(h)
                try:
                    return int(float(v)) if v is not None else None
                except ValueError:
                    return None

            with _lock:
                state = load_budget_state()
                if _i("x-quota-remaining") is not None:
                    state["account_remaining"] = _i("x-quota-remaining")
                if _i("x-usage-used") is not None:
                    state["account_used"] = _i("x-usage-used")
                save_budget_state(state)

            units_hdr = _i("x-units") or 1
            raw_text: str | None = None
            try:
                raw_text = resp.text
            except Exception:
                raw_text = None

            # Persist BEFORE json parse / classification
            try:
                try:
                    body_for_store: Any = resp.json()
                except Exception:
                    body_for_store = {
                        "ok": False,
                        "error": "non_json",
                        "status_code": resp.status_code,
                        "raw_text": (raw_text or "")[:50000],
                    }
                persist_paid_response(
                    method=method,
                    path=path,
                    params=params,
                    endpoint=endpoint,
                    nb_id=nb_id,
                    status_code=resp.status_code,
                    body=body_for_store,
                    job_id=job_id,
                    raw_text=raw_text,
                )
            except Exception as persist_exc:
                append_ledger(
                    {
                        "op": "persist_failed_STOP",
                        "endpoint": endpoint,
                        "error": type(persist_exc).__name__,
                        "nb_id": nb_id,
                        "metered": True,
                    }
                )
                _commit(
                    reservation,
                    endpoint=endpoint,
                    ok=True,
                    meta={"persist_failed": True, "status": resp.status_code},
                )
                raise NextBidControllerError(
                    f"paid_response_persist_failed:{type(persist_exc).__name__}"
                ) from persist_exc

            if resp.status_code in {429, 500, 502, 503, 504}:
                _commit(
                    reservation,
                    endpoint=endpoint,
                    ok=True,  # consumed; no uncontrolled retry loop (max 1)
                    meta={
                        "status": resp.status_code,
                        "retry_attempt": attempt,
                        "nb_id": nb_id,
                        "charged": True,
                    },
                )
                with _lock:
                    st = load_budget_state()
                    st["local_metered_since_reconcile"] = int(
                        st.get("local_metered_since_reconcile") or 0
                    ) + 1
                    save_budget_state(st)
                last_err = f"http_{resp.status_code}"
                continue
            if resp.status_code == 402:
                _commit(reservation, endpoint=endpoint, ok=True, meta={"status": 402, "nb_id": nb_id})
                with _lock:
                    st = load_budget_state()
                    st["local_metered_since_reconcile"] = int(
                        st.get("local_metered_since_reconcile") or 0
                    ) + 1
                    save_budget_state(st)
                raise NextBidBudgetExhausted("provider_quota_exhausted_402")

            data = body_for_store if isinstance(body_for_store, dict) else {"ok": True, "data": body_for_store}

            _commit(
                reservation,
                endpoint=endpoint,
                ok=True,
                meta={
                    "status": resp.status_code,
                    "nb_id": nb_id,
                    "units_hdr": units_hdr,
                    "attempt": attempt,
                    "request_id": req_id,
                },
            )
            with _lock:
                st = load_budget_state()
                st["local_metered_since_reconcile"] = int(
                    st.get("local_metered_since_reconcile") or 0
                ) + 1
                save_budget_state(st)

            if isinstance(data, dict):
                data.setdefault("ok", resp.status_code < 400)
                data["_controller"] = {
                    "job_id": job_id,
                    "endpoint": endpoint,
                    "nb_id": nb_id,
                    "attempt": attempt,
                    "request_id": req_id,
                }
                data["_request_id"] = req_id
                if use_cache and method.upper() == "GET" and resp.status_code < 400:
                    from m3.research.nextbid_cache import cache_put

                    cache_put(
                        "http",
                        {k: v for k, v in data.items() if not str(k).startswith("_")},
                        path,
                        json.dumps(params or {}, sort_keys=True, default=str),
                    )
                return data
            return {"ok": True, "data": data, "_request_id": req_id}

        raise NextBidControllerError(f"request_failed:{endpoint}:{last_err}")
    finally:
        end_inflight(req_id)


def free_request(path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Non-metered NextBid calls (usage/activity/tradelines/stats). Allowed under lock."""
    settings = get_settings()
    if not (settings.nextbid_api_key or "").strip():
        raise NextBidUnauthorized("missing_api_key")
    try:
        resp = _http().get(path, params=params, timeout=httpx.Timeout(25.0, connect=8.0))
        data = resp.json()
    except Exception as exc:
        append_ledger({"op": "free", "path": path, "ok": False, "error": type(exc).__name__, "metered": False})
        raise NextBidControllerError(type(exc).__name__) from exc
    append_ledger({"op": "free", "path": path, "ok": resp.status_code < 400, "metered": False})
    if isinstance(data, dict):
        data.setdefault("ok", resp.status_code < 400)
        return data
    return {"ok": True, "data": data}


def new_job_id(prefix: str = "job") -> str:
    return f"{prefix}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
