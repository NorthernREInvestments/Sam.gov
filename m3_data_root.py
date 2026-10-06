"""Canonical data-root resolution for file-backed M3 stores (local + Railway).

Set M3_DATA_ROOT to a persistent volume mount in production.
Default: <repo>/data
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger("govtracker.data_root")

_REPO_ROOT = Path(__file__).resolve().parent
_DEFAULT_DATA = _REPO_ROOT / "data"

_DATA_ROOT: Path | None = None


def get_repo_root() -> Path:
    return _REPO_ROOT


def get_data_root() -> Path:
    """Resolve M3 data directory. Never prints secrets."""
    global _DATA_ROOT
    if _DATA_ROOT is not None:
        return _DATA_ROOT
    raw = (os.environ.get("M3_DATA_ROOT") or "").strip()
    if raw:
        path = Path(raw).expanduser().resolve()
    else:
        path = _DEFAULT_DATA.resolve()
    path.mkdir(parents=True, exist_ok=True)
    _DATA_ROOT = path
    return path


def set_data_root(path: Path | str | None) -> Path:
    """Test/isolation hook."""
    global _DATA_ROOT
    if path is None:
        _DATA_ROOT = None
        return get_data_root()
    p = Path(path).expanduser().resolve()
    p.mkdir(parents=True, exist_ok=True)
    _DATA_ROOT = p
    return p


def reset_data_root() -> Path:
    return set_data_root(None)


def data_path(*parts: str) -> Path:
    return get_data_root().joinpath(*parts)


def canonical_opportunity_store_path() -> Path:
    return data_path("l23_canonical_population_store.json")


def registration_tracker_path() -> Path:
    return data_path("buyer_portal_registration_tracker.json")


def log_data_root_startup() -> dict[str, object]:
    """Startup-safe diagnostics (no credentials)."""
    root = get_data_root()
    store = canonical_opportunity_store_path()
    exists = store.exists()
    count = 0
    if exists:
        try:
            import json

            payload = json.loads(store.read_text(encoding="utf-8"))
            opps = payload.get("opportunities") if isinstance(payload, dict) else None
            if isinstance(opps, dict):
                count = len(opps)
            elif isinstance(opps, list):
                count = len(opps)
            elif isinstance(payload, dict) and "count" in payload:
                count = int(payload.get("count") or 0)
        except Exception as exc:
            log.warning("Could not read canonical opportunity store count: %s", type(exc).__name__)
    env_set = bool((os.environ.get("M3_DATA_ROOT") or "").strip())
    info = {
        "data_root": str(root),
        "canonical_opportunity_store": str(store),
        "store_exists": exists,
        "record_count": count,
        "m3_data_root_env_set": env_set,
        "railway_volume_hint": (
            None
            if env_set
            else "Set M3_DATA_ROOT to a Railway volume mount (e.g. /data) or deploys will use ephemeral disk."
        ),
    }
    try:
        from m3_canonical_discovery_bridge import path_parity_report

        parity = path_parity_report()
        info["canonical_path_parity_ok"] = parity.get("ok")
        if not parity.get("ok"):
            info["canonical_path_parity_error"] = parity.get("unhealthy_reason")
            print(
                f"govtracker: FATAL path parity failure read={parity.get('canonical_read_path')} "
                f"write={parity.get('canonical_write_path')}",
                flush=True,
            )
    except Exception as exc:
        info["canonical_path_parity_ok"] = None
        log.warning("Path parity check skipped: %s", type(exc).__name__)
    log.info(
        "M3 data root=%s store_exists=%s record_count=%s parity=%s",
        info["data_root"],
        info["store_exists"],
        info["record_count"],
        info.get("canonical_path_parity_ok"),
    )
    print(
        f"govtracker: data_root={info['data_root']} "
        f"opportunity_store_exists={exists} record_count={count} "
        f"m3_data_root_env_set={env_set}",
        flush=True,
    )
    return info
