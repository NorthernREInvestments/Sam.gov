"""M3 runtime mode + production data-path hygiene.

M3_RUNTIME_MODE=production|test|cleanroom

Production/cleanroom modes must not load fixture/synthetic/acceptance/replay stores.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

NAMESPACES = (
    "PRODUCTION",
    "HISTORICAL",
    "TEST_FIXTURE",
    "SYNTHETIC",
    "ACCEPTANCE_ARTIFACT",
    "REPLAY_CACHE",
    "ARCHIVE",
)

# Basename allowlist for production opportunity/evidence reads
PRODUCTION_STORE_ALLOWLIST = frozenset(
    {
        "l23_canonical_population_store.json",
        "buyer_portal_registration_tracker.json",
        "m3_discovery_run_history.json",
        "m3_opengov_live_deep_enrichment_v1.json",
        "m3_opengov_live_deep_enrichment_v1_report.json",
        "m3_opengov_live_deep_enrichment_v1_today.json",
        "m3_bidnet_paid_backlog_v1.json",
        "m3_bidnet_entitlement_split_v1.json",
        "m3_agency_procurement_profiles.json",
        # cleanroom outputs (this build)
        "m3_production_data_hygiene_cleanroom_v1_inventory.json",
        "m3_production_data_hygiene_cleanroom_v1_report.json",
        "m3_cleanroom_run_manifest.json",
        "m3_cleanroom_fresh_opportunities.json",
        "m3_cleanroom_cohort_v1.json",
        "m3_cleanroom_results_v1.json",
        "m3_cleanroom_actionable_v1.json",
        "m3_cleanroom_today_v1.json",
        "m3_production_data_hygiene_cleanroom_v1_job.json",
    }
)

_BLOCK_NAME_FRAGMENTS = (
    "fixture",
    "synthetic",
    "mock_provider",
    "_tmp_",
    "test_set",
    "test_pool",
    "large_test",
    "reality_test",
    "stage1_full_acceptance_cohort",
    "full_production_acceptance_v2_cohort",
    "acceptance_cohort",
    "historical_awards",
    "historical_profit",
    "replay",
)

_read_lock = threading.Lock()
_production_reads: list[str] = []
_nonprod_reads: list[str] = []
_mock_calls_in_production = 0


def runtime_mode() -> str:
    raw = (os.environ.get("M3_RUNTIME_MODE") or "").strip().lower()
    if raw in {"production", "prod"}:
        return "production"
    if raw in {"cleanroom", "clean"}:
        return "cleanroom"
    if raw in {"test", "testing"}:
        return "test"
    # Default: production on Railway-like roots, else test locally unless forced
    if (os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("M3_DATA_ROOT") or "").strip():
        return "production"
    return "test"


def is_production_like() -> bool:
    return runtime_mode() in {"production", "cleanroom"}


def classify_namespace(path: Path | str) -> str:
    p = Path(path)
    name = p.name.lower()
    rel = str(p).replace("\\", "/").lower()
    if "/archive/" in rel or rel.startswith("archive/") or "/quarantine" in rel:
        return "ARCHIVE"
    if any(x in name or x in rel for x in ("fixture", "fixtures", "test/", "/tests/", "large_test", "test_set", "test_pool", "_tmp_")):
        return "TEST_FIXTURE"
    if "synthetic" in name or "mock" in name:
        return "SYNTHETIC"
    if any(x in name for x in ("acceptance", "cohort", "stage1_", "truth_audit", "near_ready", "actionable_shortlist")):
        return "ACCEPTANCE_ARTIFACT"
    if "cache" in name or "replay" in name:
        return "REPLAY_CACHE"
    if "histor" in name or "award" in name or "buyer_history" in rel:
        return "HISTORICAL"
    if name in PRODUCTION_STORE_ALLOWLIST or name.startswith("m3_") or name == "l23_canonical_population_store.json":
        # m3_* may still be acceptance — re-check
        if any(x in name for x in ("acceptance", "cohort", "stage1_", "full_production_acceptance")):
            return "ACCEPTANCE_ARTIFACT"
        return "PRODUCTION"
    return "HISTORICAL" if "history" in name else "PRODUCTION"


def production_read_allowed(path: Path | str) -> bool:
    if not is_production_like():
        return True
    p = Path(path)
    ns = classify_namespace(p)
    if ns in {"TEST_FIXTURE", "SYNTHETIC", "ACCEPTANCE_ARTIFACT", "ARCHIVE"}:
        return False
    name = p.name
    if any(frag in name.lower() for frag in _BLOCK_NAME_FRAGMENTS):
        return False
    # Cleanroom: also block opportunity-level caches
    if runtime_mode() == "cleanroom" and ns == "REPLAY_CACHE":
        return False
    if name in PRODUCTION_STORE_ALLOWLIST:
        return True
    # Allow other production candidate m3_* reports written by live jobs, but not cohorts
    if name.startswith("m3_") and "cohort" not in name.lower() and "acceptance" not in name.lower():
        return True
    if name == "l23_canonical_population_store.json":
        return True
    # Deny unknown by default in cleanroom; production allows l23 + m3 live stores
    if runtime_mode() == "cleanroom":
        return name in PRODUCTION_STORE_ALLOWLIST or name == "l23_canonical_population_store.json"
    return ns == "PRODUCTION"


def record_data_read(path: Path | str) -> None:
    """Instrument a data file open. Raises in production-like if non-prod."""
    global _mock_calls_in_production
    p = Path(path)
    rel = str(p)
    allowed = production_read_allowed(p)
    with _read_lock:
        if allowed:
            _production_reads.append(rel)
        else:
            _nonprod_reads.append(rel)
    if is_production_like() and not allowed:
        raise RuntimeError(f"M3_RUNTIME_MODE={runtime_mode()} blocked non-production data read: {p.name}")


def production_safe_data_path(*parts: str) -> Path:
    """Like data_path but enforces production hygiene on read intent."""
    from m3_data_root import data_path

    path = data_path(*parts)
    # Only enforce when caller will read existing files
    if path.exists():
        record_data_read(path)
    return path


def note_mock_provider_call(provider: str = "unknown") -> None:
    global _mock_calls_in_production
    if is_production_like():
        _mock_calls_in_production += 1
        raise RuntimeError(f"MOCK_PROVIDER_CALLS_IN_PRODUCTION blocked ({provider})")


def reset_access_counters() -> None:
    global _mock_calls_in_production
    with _read_lock:
        _production_reads.clear()
        _nonprod_reads.clear()
        _mock_calls_in_production = 0


def access_snapshot() -> dict[str, Any]:
    with _read_lock:
        return {
            "RUNTIME_MODE": runtime_mode(),
            "PRODUCTION_FILES_READ": list(_production_reads),
            "NON_PRODUCTION_FILES_READ": list(_nonprod_reads),
            "PRODUCTION_READ_COUNT": len(_production_reads),
            "NON_PRODUCTION_READ_COUNT": len(_nonprod_reads),
            "MOCK_PROVIDER_CALLS_IN_PRODUCTION": _mock_calls_in_production,
        }
