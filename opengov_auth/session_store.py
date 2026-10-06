"""Persist OpenGov Playwright storage state under M3_DATA_ROOT."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from opengov_auth.config import DEFAULT_STORAGE_REL, load_opengov_auth_config

log = logging.getLogger("govtracker.opengov_auth.session")


def storage_state_path() -> Path:
    cfg = load_opengov_auth_config()
    if cfg.storage_state_path:
        return Path(cfg.storage_state_path).expanduser().resolve()
    from m3_data_root import data_path

    return data_path(DEFAULT_STORAGE_REL)


def load_storage_state() -> dict[str, Any] | None:
    path = storage_state_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and (data.get("cookies") is not None or data.get("origins") is not None):
            return data
    except Exception as exc:
        log.warning("OpenGov storage state unreadable: %s", type(exc).__name__)
    return None


def save_storage_state(state: dict[str, Any]) -> Path:
    path = storage_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(path)
    log.info("OpenGov storage state saved path=%s cookies=%s", path, len(state.get("cookies") or []))
    return path


def clear_storage_state() -> None:
    path = storage_state_path()
    if path.exists():
        try:
            path.unlink()
        except OSError as exc:
            log.warning("Could not clear OpenGov storage state: %s", type(exc).__name__)


def storage_state_exists() -> bool:
    return storage_state_path().exists()
