"""Railway BidNet runtime wiring audit helpers — never returns secrets."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


SEED_ALLOWLIST = frozenset(
    {
        "m3_bidnet_auth_required_cohort_v1.json",
        "m3_stage1_package_blocker_cohort_v1.json",
        "m3_package_recovery_source_depth_v1_results.json",
        "m3_package_recovery_source_depth_v1_report.json",
        "m3_stage1_full_acceptance_results_v1.json",
        "m3_stage1_full_acceptance_cohort_v1.json",
        "m3_stage1_full_production_acceptance_v1_report.json",
        "m3_commercial_acquisition_cache_v1.json",
        "m3_processed_real_quotes_v1.json",
    }
)


def _present(name: str) -> str:
    return "PRESENT" if (os.environ.get(name) or "").strip() else "MISSING"


def env_presence_audit() -> dict[str, str]:
    keys = [
        "BIDNET_USERNAME",
        "BIDNET_PASSWORD",
        "BIDNET_USER",
        "BIDNET_PASS",
        "BIDNET_EMAIL",
        "BIDNETDIRECT_USERNAME",
        "BIDNETDIRECT_PASSWORD",
        "BIDNET_AUTH_ENABLED",
        "BIDNET_STORAGE_STATE_PATH",
        "M3_DATA_ROOT",
        "RAILWAY_ENVIRONMENT",
        "RAILWAY_SERVICE_NAME",
        "RAILWAY_VOLUME_MOUNT_PATH",
    ]
    return {k: _present(k) for k in keys}


def data_root_persistence_probe() -> dict[str, Any]:
    from m3_data_root import data_path, get_data_root
    from bidnet_auth.session_store import storage_state_exists, storage_state_path

    root = get_data_root()
    auth_dir = data_path("bidnet_auth")
    auth_dir.mkdir(parents=True, exist_ok=True)
    sentinel = auth_dir / "runtime_probe.txt"
    write_ok = False
    read_ok = False
    err = None
    try:
        marker = f"probe:{time.time()}"
        sentinel.write_text(marker, encoding="utf-8")
        write_ok = True
        read_ok = sentinel.read_text(encoding="utf-8") == marker
    except Exception as exc:
        err = type(exc).__name__
    return {
        "M3_DATA_ROOT_ENV": _present("M3_DATA_ROOT"),
        "M3_DATA_ROOT_RESOLVED": str(root),
        "PATH_EXISTS": root.exists(),
        "PERSISTENT_VOLUME_HINT": os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or "UNKNOWN",
        "WRITE_OK": write_ok,
        "READ_OK": read_ok,
        "ERROR": err,
        "SESSION_PATH": str(storage_state_path()),
        "SESSION_PRESENT": storage_state_exists(),
        "AUTH_DIR": str(auth_dir),
    }


def path_consistency_audit() -> dict[str, Any]:
    from bidnet_auth.config import load_bidnet_auth_config
    from bidnet_auth.session_store import storage_state_path
    from m3_data_root import data_path

    cfg = load_bidnet_auth_config()
    canonical = storage_state_path()
    via_data = data_path("bidnet_auth", "storage_state.json")
    same = canonical.resolve() == via_data.resolve()
    if cfg.storage_state_path:
        same = Path(cfg.storage_state_path).expanduser().resolve() == canonical.resolve()
    return {
        "canonical_session_path": str(canonical),
        "data_path_session": str(via_data),
        "writer_reader_same": same,
        "config_override": bool(cfg.storage_state_path),
        "auth_enabled": cfg.auth_enabled,
        "credentials_present": cfg.credentials_present,
    }


def service_scope_audit() -> dict[str, Any]:
    return {
        "RAILWAY_SERVICE_NAME": os.environ.get("RAILWAY_SERVICE_NAME") or "ABSENT",
        "RAILWAY_ENVIRONMENT": os.environ.get("RAILWAY_ENVIRONMENT") or "ABSENT",
        "single_service_assumption": True,
        "note": "Sam.gov single service runs web + scheduler + BidNet workers",
        "volume_mount": os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or "ABSENT",
    }


def runtime_wiring_snapshot() -> dict[str, Any]:
    from bidnet_auth.telemetry import auth_health_snapshot

    return {
        "BUILD": "20261009-m3-railway-bidnet-runtime-wiring-v1",
        "execution_environment": {
            "cwd": str(Path.cwd()),
            "RAILWAY_ENVIRONMENT": os.environ.get("RAILWAY_ENVIRONMENT") or "ABSENT",
            "RAILWAY_SERVICE_NAME": os.environ.get("RAILWAY_SERVICE_NAME") or "ABSENT",
        },
        "ENV": env_presence_audit(),
        "SERVICE_SCOPE": service_scope_audit(),
        "DATA_ROOT": data_root_persistence_probe(),
        "PATH_CONSISTENCY": path_consistency_audit(),
        "AUTH_HEALTH": auth_health_snapshot(),
    }


def seed_allowlisted_files(files: dict[str, Any]) -> dict[str, Any]:
    """Write allowlisted JSON payloads into M3_DATA_ROOT. Never logs contents with secrets."""
    from m3_data_root import data_path

    written: list[str] = []
    skipped: list[str] = []
    errors: dict[str, str] = {}
    for name, payload in (files or {}).items():
        base = Path(str(name)).name
        if base not in SEED_ALLOWLIST:
            skipped.append(base)
            continue
        try:
            dest = data_path(base)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
            written.append(base)
        except Exception as exc:
            errors[base] = type(exc).__name__
    return {"written": written, "skipped": skipped, "errors": errors}


def seed_file_presence() -> dict[str, str]:
    from m3_data_root import data_path

    out: dict[str, str] = {}
    for name in sorted(SEED_ALLOWLIST):
        out[name] = "PRESENT" if data_path(name).exists() else "MISSING"
    return out
