"""NextBid emergency stop — blocks metered acquisition without deleting data."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from m3.config import get_settings

FLAG_NAME = "NEXTBID_EMERGENCY_STOP.json"


def _flag_path() -> Path:
    root = get_settings().data_root() / "nextbid_checkpoints"
    root.mkdir(parents=True, exist_ok=True)
    return root / FLAG_NAME


def engage_emergency_stop(*, reason: str = "owner_emergency_stop") -> dict[str, Any]:
    payload = {
        "engaged": True,
        "reason": reason,
        "engaged_at": datetime.now(timezone.utc).isoformat(),
        "metered_calls_forbidden": True,
        "scheduler_forbidden": True,
        "retries_forbidden": True,
        "resume_requires_owner_authorization": True,
    }
    _flag_path().write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def clear_emergency_stop(*, owner_authorized: bool = False) -> dict[str, Any]:
    if not owner_authorized:
        return {"ok": False, "error": "owner_authorization_required"}
    path = _flag_path()
    if path.exists():
        path.unlink()
    return {"ok": True, "engaged": False}


def emergency_stop_status() -> dict[str, Any]:
    settings = get_settings()
    path = _flag_path()
    file_flag = None
    if path.exists():
        try:
            file_flag = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            file_flag = {"engaged": True, "corrupt": True}
    env_stop = bool(settings.nextbid_emergency_stop)
    enabled = bool(settings.nextbid_enabled)
    engaged = env_stop or bool((file_flag or {}).get("engaged")) or (not enabled)
    return {
        "engaged": engaged,
        "nextbid_enabled": enabled,
        "env_emergency_stop": env_stop,
        "file_flag": file_flag,
        "metered_calls_allowed": (not engaged) and enabled,
        "resume_requires_owner_authorization": True,
    }


def assert_metered_allowed() -> None:
    """Raise if any metered NextBid call is attempted while stopped."""
    st = emergency_stop_status()
    if not st.get("metered_calls_allowed"):
        raise RuntimeError(
            "NEXTBID_EMERGENCY_STOP: metered NextBid API calls are forbidden. "
            "Await owner authorization before resuming."
        )
