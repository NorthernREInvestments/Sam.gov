"""Shared BidNet browser/session lifecycle — ownership, cleanup, taxonomy.

Playwright sync API is thread-affine. Closing a client from a different thread
than the one that created it (FSPF timeout path) produces playwright ``Error``
and leaves orphan Chromium processes that poison the next launch.

This module consolidates:
- failure classification (browser vs auth vs session)
- crash-safe session persistence with generation fencing
- single-flight auth refresh lock
- orphan Chromium cleanup
- launch circuit breaker
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from application_clock import now_utc
from m3_data_root import data_path

log = logging.getLogger("govtracker.bidnet_auth.lifecycle")

BUILD = "20261016-m3-bidnet-auth-lifecycle-v1b"
OUT = "_og_commercial_research_pipeline_out"
CIRCUIT_FILE = f"{OUT}/m3_bidnet_browser_circuit_v1.json"
REFRESH_LOCK_FILE = f"{OUT}/m3_bidnet_auth_refresh.lock"
SESSION_META_FILE = "bidnet_auth/storage_state_meta_v1.json"

# Taxonomy (mutually exclusive primary classes for browser/auth layer)
BROWSER_STARTUP_FAILED = "BROWSER_STARTUP_FAILED"
BROWSER_PROCESS_FAILED = "BROWSER_PROCESS_FAILED"
BROWSER_CONTEXT_INVALID = "BROWSER_CONTEXT_INVALID"
SESSION_RESTORE_FAILED = "SESSION_RESTORE_FAILED"
SESSION_EXPIRED = "SESSION_EXPIRED"
AUTH_REJECTED = "AUTH_REJECTED"
AUTH_CHALLENGE_REQUIRED = "AUTH_CHALLENGE_REQUIRED"

_refresh_lock = threading.Lock()
# RLock: record_browser_failure holds the lock and returns browser_circuit_status().
_circuit_lock = threading.RLock()

# After N consecutive launch failures, trip breaker for cooldown_s.
CIRCUIT_FAILURE_THRESHOLD = int(os.environ.get("M3_BIDNET_BROWSER_CIRCUIT_FAILS") or "3")
CIRCUIT_COOLDOWN_S = float(os.environ.get("M3_BIDNET_BROWSER_CIRCUIT_COOLDOWN_S") or "90")


def _utc() -> str:
    return now_utc().isoformat()


def classify_browser_exception(exc: BaseException | None, *, message: str | None = None) -> str:
    """Map a Playwright/OS exception to a precise browser/auth class."""
    blob = " ".join(
        [
            type(exc).__name__ if exc is not None else "",
            str(exc or ""),
            str(getattr(exc, "message", "") or ""),
            message or "",
        ]
    ).lower()
    if any(
        x in blob
        for x in (
            "blockingioerror",
            "errno 11",
            "resource temporarily unavailable",
            "too many open files",
            "cannot allocate memory",
        )
    ):
        return BROWSER_STARTUP_FAILED
    if any(
        x in blob
        for x in (
            "executable doesn't exist",
            "browserType.launch",
            "chromium",
            "playwright install",
            "missing dependencies",
            "libnspr",
            "no usable sandbox",
        )
    ) and any(x in blob for x in ("launch", "install", "executable", "dependenc")):
        return BROWSER_STARTUP_FAILED
    if any(
        x in blob
        for x in (
            "target closed",
            "targetclosed",
            "browser has been closed",
            "browser closed",
            "context closed",
            "connection closed",
            "protocol error",
            "pipe closed",
        )
    ):
        return BROWSER_CONTEXT_INVALID
    if any(x in blob for x in ("crash", "sigsegv", "sigabrt", "killed", "oom", "out of memory")):
        return BROWSER_PROCESS_FAILED
    if "greenlet" in blob or ("cannot switch" in blob and "thread" in blob):
        return BROWSER_CONTEXT_INVALID
    # Generic Playwright Error after a timeout close is almost always context death
    if type(exc).__name__ == "Error" and not str(exc or "").strip():
        return BROWSER_CONTEXT_INVALID
    if type(exc).__name__ == "Error":
        return BROWSER_STARTUP_FAILED
    return BROWSER_STARTUP_FAILED


def classify_auth_result_message(*, status: str | None, message: str | None) -> str:
    """Classify ensure_authenticated outcomes that are not browser-launch failures."""
    st = str(status or "").upper()
    msg = str(message or "")
    low = msg.lower()
    if "browser_unavailable" in low:
        # Parse nested class if present: browser_unavailable:CLASS:detail
        m = re.search(r"browser_unavailable:([A-Z_]+)", msg)
        if m and m.group(1) in {
            BROWSER_STARTUP_FAILED,
            BROWSER_PROCESS_FAILED,
            BROWSER_CONTEXT_INVALID,
        }:
            return m.group(1)
        return classify_browser_exception(None, message=msg)
    if st in {"EXPIRED"} or "session expired" in low:
        return SESSION_EXPIRED
    if st in {"AUTH_CHALLENGE"} or "challenge" in low or "captcha" in low:
        return AUTH_CHALLENGE_REQUIRED
    if "bad_credential" in low or "login rejected" in low or "invalid password" in low:
        return AUTH_REJECTED
    if "storage" in low and ("corrupt" in low or "unreadable" in low or "restore" in low):
        return SESSION_RESTORE_FAILED
    if st in {"AUTH_FAILED"}:
        return AUTH_REJECTED
    return st or AUTH_REJECTED


def format_browser_unavailable(exc: BaseException) -> str:
    """Sanitized, precise browser_unavailable message (no secrets)."""
    klass = classify_browser_exception(exc)
    detail = f"{type(exc).__name__}:{exc}"[:140]
    return f"browser_unavailable:{klass}:{detail}"


def load_circuit() -> dict[str, Any]:
    path = Path(data_path(CIRCUIT_FILE))
    if not path.exists():
        return {"kind": "BidNetBrowserCircuit", "build": BUILD, "open": False, "failures": 0}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"open": False, "failures": 0}
    except Exception:
        return {"open": False, "failures": 0}


def _save_circuit(data: dict[str, Any]) -> None:
    path = Path(data_path(CIRCUIT_FILE))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def browser_circuit_status() -> dict[str, Any]:
    with _circuit_lock:
        cur = load_circuit()
        open_until = float(cur.get("open_until") or 0)
        now = time.time()
        is_open = bool(cur.get("open")) and open_until > now
        if cur.get("open") and open_until <= now:
            cur["open"] = False
            cur["failures"] = 0
            cur["closed_at"] = _utc()
            _save_circuit(cur)
            is_open = False
        return {
            "open": is_open,
            "failures": int(cur.get("failures") or 0),
            "remaining_s": round(max(0.0, open_until - now), 1) if is_open else 0.0,
            "last_class": cur.get("last_class"),
            "build": BUILD,
        }


def record_browser_success() -> None:
    with _circuit_lock:
        cur = load_circuit()
        cur.update(
            {
                "kind": "BidNetBrowserCircuit",
                "build": BUILD,
                "open": False,
                "failures": 0,
                "last_success_at": _utc(),
            }
        )
        _save_circuit(cur)


def record_browser_failure(*, klass: str, detail: str | None = None) -> dict[str, Any]:
    with _circuit_lock:
        cur = load_circuit()
        fails = int(cur.get("failures") or 0) + 1
        cur["failures"] = fails
        cur["last_class"] = klass
        cur["last_detail"] = (detail or "")[:160]
        cur["last_failure_at"] = _utc()
        if fails >= CIRCUIT_FAILURE_THRESHOLD:
            cur["open"] = True
            cur["open_until"] = time.time() + CIRCUIT_COOLDOWN_S
            cur["opened_at"] = _utc()
            log.warning(
                "BidNet browser circuit OPEN after %s failures class=%s cooldown=%ss",
                fails,
                klass,
                CIRCUIT_COOLDOWN_S,
            )
        cur["kind"] = "BidNetBrowserCircuit"
        cur["build"] = BUILD
        _save_circuit(cur)
        return browser_circuit_status()


def acquire_auth_refresh_lock(*, holder: str, ttl_s: float = 120.0) -> bool:
    """Single-flight login/refresh. File lock survives process restarts briefly."""
    if not _refresh_lock.acquire(blocking=False):
        return False
    path = Path(data_path(REFRESH_LOCK_FILE))
    path.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    try:
        if path.exists():
            try:
                meta = json.loads(path.read_text(encoding="utf-8"))
                until = float(meta.get("until") or 0)
                if until > now and str(meta.get("holder") or "") != holder:
                    _refresh_lock.release()
                    return False
            except Exception:
                pass
        path.write_text(
            json.dumps({"holder": holder[:80], "until": now + ttl_s, "at": _utc()}),
            encoding="utf-8",
        )
        return True
    except Exception:
        try:
            _refresh_lock.release()
        except Exception:
            pass
        return False


def release_auth_refresh_lock(*, holder: str) -> None:
    path = Path(data_path(REFRESH_LOCK_FILE))
    try:
        if path.exists():
            meta = json.loads(path.read_text(encoding="utf-8"))
            if str(meta.get("holder") or "") in {"", holder}:
                path.unlink(missing_ok=True)
    except Exception:
        pass
    try:
        if _refresh_lock.locked():
            _refresh_lock.release()
    except Exception:
        pass


def session_write_generation() -> int:
    path = Path(data_path(SESSION_META_FILE))
    if not path.exists():
        return 0
    try:
        return int(json.loads(path.read_text(encoding="utf-8")).get("generation") or 0)
    except Exception:
        return 0


def save_storage_state_fenced(
    state: dict[str, Any],
    *,
    writer: str,
    min_generation: int | None = None,
) -> dict[str, Any]:
    """Crash-safe session write; refuse to overwrite a newer generation."""
    from bidnet_auth.session_store import save_storage_state, storage_state_path

    meta_path = Path(data_path(SESSION_META_FILE))
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    current_gen = session_write_generation()
    if min_generation is not None and current_gen > int(min_generation):
        return {
            "ok": False,
            "skipped": True,
            "reason": "SUPERSEDED_GENERATION",
            "current_generation": current_gen,
            "attempted_min": min_generation,
        }
    new_gen = current_gen + 1
    path = save_storage_state(state)
    meta = {
        "kind": "BidNetStorageStateMeta",
        "build": BUILD,
        "generation": new_gen,
        "writer": (writer or "")[:80],
        "saved_at": _utc(),
        "cookies": len(state.get("cookies") or []),
        "path": str(path),
    }
    tmp = meta_path.with_suffix(meta_path.suffix + ".tmp")
    tmp.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    tmp.replace(meta_path)
    return {"ok": True, "generation": new_gen, "path": str(storage_state_path())}


def reset_browser_circuit() -> dict[str, Any]:
    """Clear the launch circuit breaker so the next auth attempt is allowed."""
    with _circuit_lock:
        cur = {
            "kind": "BidNetBrowserCircuit",
            "build": BUILD,
            "open": False,
            "failures": 0,
            "reset_at": _utc(),
        }
        _save_circuit(cur)
        return browser_circuit_status()


def force_cleanup_orphaned_chromium(
    *, max_kill: int = 8, aggressive: bool = False
) -> dict[str, Any]:
    """Best-effort cleanup of leaked Chromium processes from abandoned Playwright threads.

    Safe on Railway Linux. Never raises. Does not touch unrelated browsers.
    Never kills Chromium while the purchasing-group access manager holds the
    BidNet browser lease — that was aborting form navigation in ~5–8s.

    aggressive=True also kills headless chrome/chromium without an explicit
    playwright marker (resource-exhaustion recoveries after BlockingIOError).
    """
    report: dict[str, Any] = {
        "killed": 0,
        "examined": 0,
        "errors": [],
        "aggressive": bool(aggressive),
    }
    try:
        from bidnet_auth.browser_mutex_v1 import browser_mutex_status

        lease = browser_mutex_status()
        holder = str(lease.get("holder") or "")
        if lease.get("held") and (
            holder.startswith("pg-access") or holder.startswith("fspf-")
        ):
            # Only skip for active PG registration; fspf holders that are stale
            # still block forever if we never clean. Skip only when remaining.
            rem = float(lease.get("remaining_s") or 0)
            if holder.startswith("pg-access") or rem > 5.0:
                report["skipped"] = "active_browser_lease"
                report["holder"] = holder
                report["remaining_s"] = lease.get("remaining_s")
                return report
    except Exception as exc:
        report["lease_check_error"] = type(exc).__name__
    try:
        import psutil  # type: ignore
    except Exception:
        psutil = None  # type: ignore
    if psutil is None:
        # Fallback: pgrep/pkill pattern limited to playwright chromium
        try:
            import subprocess

            out = subprocess.run(
                ["pgrep", "-af", "chrome|chromium"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            lines = []
            for ln in (out.stdout or "").splitlines():
                low = ln.lower()
                if "playwright" in low or "ms-playwright" in low:
                    lines.append(ln)
                elif aggressive and ("headless" in low or "--no-sandbox" in low):
                    lines.append(ln)
            report["examined"] = len(lines)
            for ln in lines[:max_kill]:
                try:
                    pid = int(ln.split(None, 1)[0])
                    os.kill(pid, 9)
                    report["killed"] += 1
                except Exception as exc:
                    report["errors"].append(type(exc).__name__)
        except Exception as exc:
            report["errors"].append(type(exc).__name__)
        return report

    try:
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                name = (proc.info.get("name") or "").lower()
                cmd = " ".join(proc.info.get("cmdline") or []).lower()
                if "chrom" not in name and "chrom" not in cmd:
                    continue
                is_pw = "playwright" in cmd or "ms-playwright" in cmd
                is_headless = "headless" in cmd or "--no-sandbox" in cmd
                if not is_pw and not (aggressive and is_headless):
                    continue
                report["examined"] += 1
                if report["killed"] >= max_kill:
                    break
                proc.kill()
                report["killed"] += 1
            except Exception as exc:
                report["errors"].append(type(exc).__name__)
    except Exception as exc:
        report["errors"].append(type(exc).__name__)
    return report


def recover_bidnet_browser(*, max_kill: int = 16) -> dict[str, Any]:
    """Owner/ops recover: kill orphan Chromium, reset circuit, report readiness."""
    cleanup = force_cleanup_orphaned_chromium(max_kill=max_kill, aggressive=True)
    # Brief settle so OS reclaims FDs / process slots after SIGKILL.
    time.sleep(1.5)
    cleanup2 = force_cleanup_orphaned_chromium(max_kill=max(4, max_kill // 2), aggressive=True)
    circuit = reset_browser_circuit()
    return {
        "ok": True,
        "build": BUILD,
        "cleanup": cleanup,
        "cleanup_second_pass": cleanup2,
        "circuit": circuit,
        "at": _utc(),
    }
