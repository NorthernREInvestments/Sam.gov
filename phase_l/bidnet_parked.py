"""Phase L.14 — BidNet authenticated history parked (no engineering effort)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from application_clock import now_utc

BUILD = "20260928-m3-phase-l14-nonbidnet-source-expansion"
ROOT = Path(__file__).resolve().parents[1]
PARK_PATH = ROOT / "data" / "phase_l14_bidnet_auth_history_parked.json"
OUT = ROOT / "artifacts" / "phase_l"

BIDNET_AUTH_HISTORY_PARKED = "BIDNET_AUTH_HISTORY_PARKED"


def _utc() -> str:
    return now_utc().isoformat()


def park_bidnet_auth_history(*, blocked_count: int | None = None) -> dict[str, Any]:
    """Preserve BidNet exact-history gaps for later — discovery may continue; no auth work."""
    n = blocked_count
    if n is None:
        gaps = OUT / "l12_history_access_gaps.json"
        l13 = OUT / "l13_bidnet_recovery.json"
        if l13.exists():
            try:
                d = json.loads(l13.read_text(encoding="utf-8"))
                n = int((d.get("summary") or {}).get("still_blocked") or 0) or None
            except Exception:
                pass
        if n is None and gaps.exists():
            try:
                d = json.loads(gaps.read_text(encoding="utf-8"))
                n = len(d.get("rows") or [])
            except Exception:
                n = 82
        if n is None:
            n = 82

    rec = {
        "kind": BIDNET_AUTH_HISTORY_PARKED,
        "build": BUILD,
        "platform": "BidNet",
        "blocked_history": n,
        "unlock": "free_vendor_registration",
        "priority": "deferred_by_owner",
        "discovery_may_continue": True,
        "no_current_engineering_effort": True,
        "forbidden_this_phase": [
            "BidNet registration",
            "BidNet login",
            "BidNet anti-bot evasion",
            "BidNet history scraping",
            "BidNet CAPTCHA",
            "BidNet award-page workarounds",
        ],
        "parked_at": _utc(),
        "reopen_only_when": "owner_explicitly_unparks_or_registers",
    }
    PARK_PATH.parent.mkdir(parents=True, exist_ok=True)
    PARK_PATH.write_text(json.dumps(rec, indent=2), encoding="utf-8")
    return rec


def is_bidnet_auth_parked() -> bool:
    if not PARK_PATH.exists():
        return False
    try:
        d = json.loads(PARK_PATH.read_text(encoding="utf-8"))
        return d.get("kind") == BIDNET_AUTH_HISTORY_PARKED and d.get("no_current_engineering_effort") is True
    except Exception:
        return False


def bidnet_parked_report() -> dict[str, Any]:
    if PARK_PATH.exists():
        try:
            return json.loads(PARK_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return park_bidnet_auth_history()
