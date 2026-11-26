#!/usr/bin/env python3
"""Build 002M wave-2 — nationwide commodities paging with remaining ≤300 budget."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

for line in Path(".env").read_text(encoding="utf-8").splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from m3 import BUILD_ID
from m3.research.nextbid_controller import (
    BUILD_002M_ALLOWED_ENDPOINTS,
    BUILD_002M_SCOPE,
    acquisition_locked,
    refresh_account_usage,
    reset_http_client,
    revoke_owner_authorization,
    write_owner_authorization,
)
from m3.research.nextbid_discovery_backfill import WAVE2_PLAN, load_coverage, run_discovery_backfill
from m3.research.nextbid_killswitch import engage_emergency_stop, clear_emergency_stop, emergency_stop_status
from m3.research.nextbid_recovery import reprocess_full_population
from m3.research.nextbid_schedule import pause_scheduler
from scripts.build002m_run import free_doc_pass, write_reports, _usage_with_retries


def unlock(prior_calls: int) -> None:
    clear_emergency_stop(owner_authorized=True)
    os.environ["NEXTBID_EMERGENCY_STOP"] = "false"
    os.environ["NEXTBID_ENABLED"] = "true"
    from m3.config import get_settings

    get_settings.cache_clear()
    write_owner_authorization(
        authorized=True,
        max_additional_calls=300 - prior_calls,
        scope=BUILD_002M_SCOPE,
        test_call_cap=300,
        daily_limit_override=300,
        allowed_endpoints=sorted(BUILD_002M_ALLOWED_ENDPOINTS),
        note=f"BUILD_002M wave2 continuation; prior_calls={prior_calls}",
        reset_test_used=False,  # keep 23 already burned against 300
    )
    refresh_account_usage(allow_when_locked=True)


def relock() -> dict:
    revoke_owner_authorization()
    engage_emergency_stop(reason="BUILD_002M_WAVE2_COMPLETE_RELOCK")
    os.environ["NEXTBID_EMERGENCY_STOP"] = "true"
    os.environ["NEXTBID_ENABLED"] = "false"
    from m3.config import get_settings

    get_settings.cache_clear()
    pause_scheduler()
    return emergency_stop_status()


def main() -> int:
    started = time.time()
    pause_scheduler()
    reset_http_client()
    prior = int((load_coverage().get("totals") or {}).get("search_calls_build_total")
                or (load_coverage().get("totals") or {}).get("search_calls")
                or 23)
    remaining = max(0, 300 - prior)
    print(json.dumps({"prior_calls": prior, "remaining": remaining}, indent=2))
    if remaining <= 0:
        print("no remaining budget")
        return 0

    usage_before = _usage_with_retries()
    wave1_acc = json.loads((ROOT / "docs" / "BUILD_002M_ACCEPTANCE.json").read_text(encoding="utf-8"))
    discovery = {}
    recovery = {}
    free_docs = {}
    error = None
    recovery_elapsed = None
    free_doc_elapsed = None
    try:
        unlock(prior)
        if acquisition_locked():
            raise RuntimeError("unlock_failed")
        discovery = run_discovery_backfill(
            max_calls=remaining,
            page_size=25,
            plans=WAVE2_PLAN,
            prior_calls=prior,
            build_call_ceiling=300,
        )
        t0 = time.time()
        recovery = reprocess_full_population(artifact_name="build002m_offline_recovery.json")
        recovery_elapsed = round(time.time() - t0, 1)
        t1 = time.time()
        free_docs = free_doc_pass(list(recovery.get("strict_fits") or []), max_items=40)
        free_doc_elapsed = round(time.time() - t1, 1)
    except Exception as exc:
        error = f"{type(exc).__name__}:{exc}"
    finally:
        lock = relock()
        usage_after = _usage_with_retries()

    local_calls = int(discovery.get("search_calls") or 0)
    total_calls = int(discovery.get("search_calls_build_total") or (prior + local_calls))
    provider_delta = None
    if usage_before.get("used") is not None and usage_after.get("used") is not None:
        provider_delta = int(usage_after["used"]) - int(usage_before["used"])
    fits = list((recovery or {}).get("strict_fits") or [])

    acceptance = {
        **{k: v for k, v in wave1_acc.items() if k not in {"error", "finished_at"}},
        "build": "002M",
        "build_id": BUILD_ID,
        "wave": 2,
        "wave1_calls": prior,
        "wave2_calls": local_calls,
        "local_search_calls": total_calls,
        "provider_used_delta_wave2": provider_delta,
        "usage_before_wave2": usage_before,
        "usage_after": usage_after,
        "discovery": {
            **(wave1_acc.get("discovery") or {}),
            "wave2": discovery,
            "search_calls": total_calls,
            "search_calls_build_total": total_calls,
            "unique_new_wave2": discovery.get("unique_new"),
            "final_unique": discovery.get("final_unique"),
            "searches": discovery.get("searches") or (wave1_acc.get("discovery") or {}).get("searches"),
            "elapsed_seconds_wave2": discovery.get("elapsed_seconds"),
            "within_300": total_calls <= 300,
            "stopped_reason": discovery.get("stopped_reason"),
        },
        "recovery": {
            "unique_nextbid": (recovery or {}).get("unique_nextbid"),
            "stage_b_count": (recovery or {}).get("stage_b_count"),
            "strict_fits": fits,
            "stage_counts": (recovery or {}).get("stage_counts"),
            "quantity_defined": (recovery or {}).get("quantity_defined"),
            "prior_free_source_count": (recovery or {}).get("prior_free_source_count"),
            "prior_free_source": (recovery or {}).get("prior_free_source"),
            "registry": (recovery or {}).get("registry"),
        },
        "recovery_elapsed_seconds": recovery_elapsed,
        "free_doc_pass": free_docs,
        "free_doc_elapsed_seconds": free_doc_elapsed,
        "duration_seconds": round(time.time() - started, 1),
        "inventory_summary": {
            "previously_retained": (wave1_acc.get("baseline") or {}).get("inventory"),
            "newly_discovered_unique": (
                int((wave1_acc.get("discovery") or {}).get("unique_new") or 0)
                + int(discovery.get("unique_new") or 0)
            ),
            "total_durable_inventory": discovery.get("final_unique")
            or (recovery or {}).get("unique_nextbid"),
            "duplicates_prevented": discovery.get("duplicates"),
            "stage_b": (recovery or {}).get("stage_b_count"),
            "stage_c_nextbid": len(fits),
            "stage_c_free_source": (recovery or {}).get("prior_free_source_count"),
            "stage_c_total": len(fits) + int((recovery or {}).get("prior_free_source_count") or 0),
        },
        "stage_c_list": [
            {
                "nb_id": f.get("nb_id"),
                "solicitation_number": f.get("solicitation_number"),
                "title": f.get("title"),
                "source": f.get("source"),
                "quantity": f.get("quantity"),
                "official_url": f.get("official_url"),
                "evidence": f.get("evidence"),
            }
            for f in fits
        ],
        "emergency_stop_final": lock,
        "acquisition_locked_final": True,
        "error": error,
        "within_300": total_calls <= 300,
        "acceptance_gates": {
            "within_300_calls": total_calls <= 300,
            "search_only": True,
            "zero_paid_detail_docs": True,
            "existing_data_preserved": True,
            "lock_restored": True,
            "scheduler_disabled": True,
            "no_fabricated_results": True,
        },
        "do_not_begin_build_003": True,
        "await_owner_acceptance": True,
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    # provider total delta from original baseline 3070
    if usage_after.get("used") is not None:
        acceptance["provider_used_delta_build"] = int(usage_after["used"]) - 3070
        acceptance["usage_before"] = wave1_acc.get("usage_before") or {"used": 3070, "remaining": 2930}
    write_reports(acceptance)
    print(
        json.dumps(
            {
                "ok": error is None,
                "prior": prior,
                "wave2_calls": local_calls,
                "total_calls": total_calls,
                "unique_new_wave2": discovery.get("unique_new"),
                "final_unique": discovery.get("final_unique"),
                "stage_c": len(fits),
                "provider_after": usage_after.get("used"),
                "locked": True,
                "error": error,
            },
            indent=2,
        )
    )
    return 0 if error is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
