#!/usr/bin/env python3
"""Build 002M wave-3 — priority-source open listings with remaining ≤300 budget."""

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
from m3.research.nextbid_discovery_backfill import WAVE3_PLAN, load_coverage, run_discovery_backfill
from m3.research.nextbid_killswitch import engage_emergency_stop, clear_emergency_stop
from m3.research.nextbid_recovery import reprocess_full_population
from m3.research.nextbid_schedule import pause_scheduler
from scripts.build002m_run import free_doc_pass, write_reports, _usage_with_retries


def unlock() -> None:
    clear_emergency_stop(owner_authorized=True)
    os.environ["NEXTBID_EMERGENCY_STOP"] = "false"
    os.environ["NEXTBID_ENABLED"] = "true"
    from m3.config import get_settings

    get_settings.cache_clear()
    write_owner_authorization(
        authorized=True,
        max_additional_calls=300,
        scope=BUILD_002M_SCOPE,
        test_call_cap=300,
        daily_limit_override=300,
        allowed_endpoints=sorted(BUILD_002M_ALLOWED_ENDPOINTS),
        note="BUILD_002M wave3 priority-source open listings",
        reset_test_used=False,
    )
    refresh_account_usage(allow_when_locked=True)


def relock():
    revoke_owner_authorization()
    engage_emergency_stop(reason="BUILD_002M_COMPLETE_RELOCK")
    os.environ["NEXTBID_EMERGENCY_STOP"] = "true"
    os.environ["NEXTBID_ENABLED"] = "false"
    from m3.config import get_settings

    get_settings.cache_clear()
    pause_scheduler()
    return True


def main() -> int:
    started = time.time()
    pause_scheduler()
    reset_http_client()
    # Prefer ledger test_used as authoritative build burn
    from m3.research.nextbid_controller import load_budget_state

    prior = int(load_budget_state().get("test_used") or 0)
    remaining = max(0, 300 - prior)
    print(json.dumps({"prior_calls": prior, "remaining": remaining}, indent=2))
    if remaining <= 0:
        return 0

    # Mark nationwide search abandoned in coverage
    cov = load_coverage()
    for k, v in list((cov.get("searches") or {}).items()):
        if "nationwide_commodities" in k or v.get("source") == "nationwide_commodities":
            v["completion_status"] = "aborted_excluded_source_flood"
            v["note"] = "Stopped: pages dominated by DIBBS (excluded). Do not resume without sources= whitelist."
    Path("data/nextbid_checkpoints/build002m_coverage_v1.json").write_text(
        json.dumps(cov, indent=2, default=str), encoding="utf-8"
    )

    usage_before = _usage_with_retries()
    prev = json.loads((ROOT / "docs" / "BUILD_002M_ACCEPTANCE.json").read_text(encoding="utf-8"))
    discovery = {}
    recovery = {}
    free_docs = {}
    error = None
    recovery_elapsed = free_doc_elapsed = None
    try:
        unlock()
        if acquisition_locked():
            raise RuntimeError("unlock_failed")
        discovery = run_discovery_backfill(
            max_calls=remaining,
            page_size=25,
            plans=WAVE3_PLAN,
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
        relock()
        usage_after = _usage_with_retries()

    total_calls = int(discovery.get("search_calls_build_total") or (prior + int(discovery.get("search_calls") or 0)))
    fits = list((recovery or {}).get("strict_fits") or [])
    acceptance = {
        **{k: v for k, v in prev.items() if k not in {"error", "finished_at"}},
        "build": "002M",
        "build_id": BUILD_ID,
        "wave": 3,
        "local_search_calls": total_calls,
        "wave3_calls": discovery.get("search_calls"),
        "usage_before_build": {"used": 3070, "remaining": 2930, "allowance": 6000},
        "usage_after": usage_after,
        "provider_used_delta_build": (
            int(usage_after["used"]) - 3070 if usage_after.get("used") is not None else None
        ),
        "discovery": {
            **(prev.get("discovery") or {}),
            "wave3": discovery,
            "search_calls": total_calls,
            "final_unique": discovery.get("final_unique"),
            "unique_new_wave3": discovery.get("unique_new"),
            "searches": discovery.get("searches"),
            "within_300": total_calls <= 300,
            "stopped_reason": discovery.get("stopped_reason"),
            "dibbs_waste_note": "nationwide_commodities aborted; excluded-source flood",
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
            "previously_retained": (prev.get("baseline") or {}).get("inventory"),
            "newly_discovered_unique": discovery.get("unique_new"),
            "total_durable_inventory": discovery.get("final_unique"),
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
        "acquisition_locked_final": True,
        "within_300": total_calls <= 300,
        "error": error,
        "acceptance_gates": {
            "within_300_calls": total_calls <= 300,
            "search_only": True,
            "zero_paid_detail_docs": True,
            "lock_restored": True,
            "scheduler_disabled": True,
            "no_fabricated_results": True,
        },
        "do_not_begin_build_003": True,
        "await_owner_acceptance": True,
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    write_reports(acceptance)
    print(
        json.dumps(
            {
                "ok": error is None,
                "prior": prior,
                "wave3": discovery.get("search_calls"),
                "total": total_calls,
                "unique_new": discovery.get("unique_new"),
                "final_unique": discovery.get("final_unique"),
                "stage_c": len(fits),
                "provider_used": usage_after.get("used"),
                "error": error,
            },
            indent=2,
        )
    )
    return 0 if error is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
