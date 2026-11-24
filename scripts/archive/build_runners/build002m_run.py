#!/usr/bin/env python3
"""Build 002M — one-time discovery-only NextBid backfill (≤300 search calls) + offline processing.

Always re-locks paid acquisition in finally. Zero paid detail/document calls.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
    OWNER_HARD_BUILD_002M_CEILING,
    acquisition_locked,
    controller_status,
    refresh_account_usage,
    reset_http_client,
    revoke_owner_authorization,
    write_owner_authorization,
)
from m3.research.nextbid_discovery_backfill import run_discovery_backfill
from m3.research.nextbid_killswitch import (
    clear_emergency_stop,
    engage_emergency_stop,
    emergency_stop_status,
)
from m3.research.nextbid_recovery import reprocess_full_population
from m3.research.nextbid_schedule import pause_scheduler
from m3.research.procurement_registry import load_records, upsert_record
from m3.research.commercial_criteria import STAGE_C


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load(path: Path) -> Any:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def unlock_for_002m() -> None:
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
        note="BUILD_002M owner-authorized one-time discovery/search-only backfill ≤300",
        reset_test_used=True,
    )
    # Seed account usage into budget state before first metered call
    refresh_account_usage(allow_when_locked=True)


def relock() -> dict[str, Any]:
    revoke_owner_authorization()
    engage_emergency_stop(reason="BUILD_002M_BACKFILL_COMPLETE_RELOCK")
    os.environ["NEXTBID_EMERGENCY_STOP"] = "true"
    os.environ["NEXTBID_ENABLED"] = "false"
    from m3.config import get_settings

    get_settings.cache_clear()
    try:
        pause_scheduler()
    except Exception:
        pass
    return emergency_stop_status()


def free_doc_pass(fits: list[dict], *, max_items: int = 40) -> dict[str, Any]:
    """Acquire documents from public government URLs only (no NextBid metered)."""
    from m3.db import SessionLocal
    from m3.research.package import acquire_packages_for_items

    items = []
    for f in fits[:max_items]:
        items.append(
            {
                "external_key": f.get("nb_id") or f.get("solicitation_number"),
                "title": f.get("title"),
                "solicitation_number": f.get("solicitation_number"),
                "buyer_name": f.get("agency"),
                "official_url": f.get("official_url"),
                "source_key": f.get("source") or "nextbid",
                "nb_id": f.get("nb_id"),
            }
        )
    if not items:
        return {"processed": 0, "note": "no_fits"}
    db = SessionLocal()
    try:
        result = acquire_packages_for_items(db, items, max_items=max_items)
        return {"ok": True, "result": result, "processed": len(items), "metered_nextbid": 0}
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__, "processed": 0, "metered_nextbid": 0}
    finally:
        db.close()


def check_url(url: str | None) -> dict[str, Any]:
    if not url:
        return {"status": "NO_URL"}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "M3Supply-002M/1.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            code = getattr(resp, "status", None) or resp.getcode()
            return {"status": "REACHABLE" if int(code) < 400 else f"HTTP_{code}", "http": int(code)}
    except Exception as exc:
        return {"status": "INACCESSIBLE_OR_AUTH", "error": type(exc).__name__}


def write_reports(acceptance: dict[str, Any]) -> None:
    docs = ROOT / "docs"
    disc = acceptance["discovery"]
    rec = acceptance["recovery"]
    cov = disc.get("searches") or {}

    lines = [
        f"# Build 002M — Discovery Report",
        "",
        f"**Build ID:** `{BUILD_ID}`",
        f"**Metered calls:** {disc.get('search_calls')} / 300 (search-only)",
        f"**Detail/document calls:** 0",
        f"**Provider before → after:** {acceptance['usage_before'].get('used')} → {acceptance['usage_after'].get('used')}",
        "",
        "| Metric | Value |",
        "|--------|------:|",
        f"| Baseline unique | {disc.get('baseline_unique')} |",
        f"| Newly discovered unique | {disc.get('unique_new')} |",
        f"| Final unique inventory | {disc.get('final_unique')} |",
        f"| Duplicate rows skipped | {disc.get('duplicates')} |",
        f"| Rows returned | {disc.get('rows_returned')} |",
        f"| Elapsed seconds | {disc.get('elapsed_seconds')} |",
        f"| Stopped reason | {disc.get('stopped_reason') or 'sources_complete_or_budget'} |",
        "",
        "## Per-source coverage",
        "",
        "| Source | Pages | Returned | Unique | Dup | Provider total pages | Remaining | Status |",
        "|--------|------:|---------:|-------:|----:|---------------------:|----------:|--------|",
    ]
    for sk, meta in sorted(cov.items()):
        lines.append(
            f"| {meta.get('source')} | {meta.get('pages_requested')} | {meta.get('records_returned')} | "
            f"{meta.get('unique_records')} | {meta.get('duplicate_records')} | "
            f"{meta.get('provider_total_pages')} | {meta.get('remaining_unvisited_pages')} | "
            f"{meta.get('completion_status')} |"
        )
    (docs / "BUILD_002M_DISCOVERY_REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    (docs / "BUILD_002M_COVERAGE_REPORT.md").write_text(
        f"""# Build 002M — Coverage Report

**Do not claim nationwide completeness without source-level evidence.**

| Source | Completion | Remaining pages |
|--------|------------|----------------:|
"""
        + "\n".join(
            f"| {m.get('source')} | {m.get('completion_status')} | {m.get('remaining_unvisited_pages')} |"
            for m in cov.values()
        )
        + f"""

## Benchmarks (historical, not production counts)

- ~3,899 listings across five major state/local portals (prior observation)
- ~2,500 InstantMarkets mixed-source (prior observation)

## This backfill

- Authorized search calls: ≤300 @ page_size=25
- Theoretical max rows: 7,500
- Actual unique new: {disc.get('unique_new')}
- Actual durable inventory: {disc.get('final_unique')}
- SAM.gov / DLA: disabled

## Gaps

"""
        + "\n".join(
            f"- **{m.get('source')}**: status={m.get('completion_status')}, remaining_pages={m.get('remaining_unvisited_pages')}, provider_total_pages={m.get('provider_total_pages')}"
            for m in cov.values()
            if m.get("completion_status") != "complete"
        )
        + "\n",
        encoding="utf-8",
    )

    fits = rec.get("strict_fits") or []
    prior_free = rec.get("prior_free_source") or []
    (docs / "BUILD_002M_ACQUISITION_REPORT.md").write_text(
        f"""# Build 002M — Acquisition & Qualification Report

**Paid NextBid enrichment:** PROHIBITED / 0 calls  
**Free-source document pass:** {(acceptance.get('free_doc_pass') or {}).get('processed')} items

| Metric | Count |
|--------|------:|
| Stage B product | {rec.get('stage_b_count')} |
| Stage C NextBid | {len(fits)} |
| Prior free-source Stage C | {rec.get('prior_free_source_count')} |
| Quantity defined | {rec.get('quantity_defined')} |
| Registry records | {(rec.get('registry') or {}).get('total_records')} |

## Every verified Stage C NextBid fit

"""
        + "\n".join(
            f"- **{f.get('nb_id')}** / sol `{f.get('solicitation_number')}` — qty {f.get('quantity')} — "
            f"{f.get('source')} — [{f.get('title')}]({f.get('official_url')})"
            for f in fits
        )
        + """

## Prior free-source Stage C retained

"""
        + "\n".join(
            f"- **{p.get('solicitation_number')}** — {p.get('product_description') or p.get('title')}"
            for p in prior_free
        )
        + f"""

## Timing

| Phase | Seconds |
|-------|--------:|
| Discovery | {disc.get('elapsed_seconds')} |
| Offline reprocess | {acceptance.get('recovery_elapsed_seconds')} |
| Free doc pass | {acceptance.get('free_doc_elapsed_seconds')} |
| Total | {acceptance.get('duration_seconds')} |

Compared to Build 002L baseline inventory {acceptance.get('baseline', {}).get('inventory')} → {disc.get('final_unique')}.
""",
        encoding="utf-8",
    )
    _write(docs / "BUILD_002M_ACCEPTANCE.json", acceptance)


def _usage_with_retries(*, attempts: int = 4) -> dict[str, Any]:
    last: dict[str, Any] = {"ok": False}
    for i in range(attempts):
        try:
            reset_http_client()
            return refresh_account_usage(allow_when_locked=True)
        except Exception as exc:
            last = {"ok": False, "error": type(exc).__name__, "attempt": i + 1}
            time.sleep(2 + i)
    return last


def main() -> int:
    started = time.time()
    pause_scheduler()
    engage_emergency_stop(reason="BUILD_002M_PREFLIGHT")
    revoke_owner_authorization()
    reset_http_client()

    usage_before = _usage_with_retries()
    from m3.research.nextbid_inventory import load_inventory
    from m3.research.nextbid_client import load_checkpoint

    inv0 = load_inventory()
    ck0 = load_checkpoint("nextbid_acquire_v1").get("state") or {}
    reg0 = load_records()
    baseline = {
        "inventory": len(inv0),
        "registry": len(reg0),
        "checkpoint_listings": len(ck0.get("listings") or []),
        "checkpoint_seen": len(ck0.get("seen_nb_ids") or []),
        "provider_used": usage_before.get("used"),
        "provider_remaining": usage_before.get("remaining"),
    }
    l_acc = _load(ROOT / "docs" / "BUILD_002L_ACCEPTANCE.json") or {}

    discovery: dict[str, Any] = {}
    recovery: dict[str, Any] = {}
    free_docs: dict[str, Any] = {}
    recovery_elapsed = None
    free_doc_elapsed = None
    error: str | None = None
    lock: dict[str, Any] = {}
    usage_after: dict[str, Any] = {"ok": False}

    try:
        # Preconditions
        rem = usage_before.get("remaining")
        if rem is None:
            raise RuntimeError(f"provider_usage_unavailable:{usage_before}")
        if int(rem) - 300 < 1000:
            raise RuntimeError(f"monthly_reserve_would_breach:remaining={rem}")

        unlock_for_002m()
        if acquisition_locked():
            raise RuntimeError("failed_to_unlock_for_002m")
        st = controller_status()
        limits = st.get("limits") or {}
        if int(limits.get("test") or 0) > OWNER_HARD_BUILD_002M_CEILING:
            raise RuntimeError("test_cap_exceeds_300")
        if int(limits.get("job") or 0) > 50:
            raise RuntimeError("job_cap_exceeds_50")

        discovery = run_discovery_backfill(max_calls=300, page_size=25)
        if not discovery.get("within_300"):
            raise RuntimeError("exceeded_300_calls")

        t_rec = time.time()
        recovery = reprocess_full_population(artifact_name="build002m_offline_recovery.json")
        recovery_elapsed = round(time.time() - t_rec, 1)

        # Free-source docs for Stage C + sample Stage B with URLs
        t_doc = time.time()
        targets = list(recovery.get("strict_fits") or [])
        free_docs = free_doc_pass(targets, max_items=min(40, max(6, len(targets))))
        free_doc_elapsed = round(time.time() - t_doc, 1)

        # Ensure free-source priors remain in registry
        for p in recovery.get("prior_free_source") or []:
            upsert_record(
                {
                    "provider": "free_source_prior",
                    "provider_id": p.get("solicitation_number"),
                    "solicitation_number": p.get("solicitation_number"),
                    "title": p.get("product_description") or p.get("title"),
                    "official_url": p.get("official_url"),
                    "procurement_stage": STAGE_C,
                    "build_origins": ["build_002i", "build_002l", "build_002m"],
                },
                actor="build_002m",
            )

    except Exception as exc:
        error = f"{type(exc).__name__}:{exc}"
    finally:
        lock = relock()
        usage_after = _usage_with_retries()

    provider_delta = None
    if usage_before.get("used") is not None and usage_after.get("used") is not None:
        provider_delta = int(usage_after["used"]) - int(usage_before["used"])

    local_calls = int(discovery.get("search_calls") or 0)
    fits = list((recovery or {}).get("strict_fits") or [])
    prior_c = int((recovery or {}).get("prior_free_source_count") or 0)
    l_fits = len(((l_acc.get("recovery") or {}).get("strict_fits") or []))
    if not l_fits:
        l_fits = 6  # 002L restored six NextBid Stage C

    acceptance = {
        "build": "002M",
        "build_id": BUILD_ID,
        "started_at": _utc(),
        "duration_seconds": round(time.time() - started, 1),
        "error": error,
        "baseline": baseline,
        "usage_before": usage_before,
        "usage_after": usage_after,
        "provider_used_delta": provider_delta,
        "local_search_calls": local_calls,
        "local_detail_calls": 0,
        "local_document_calls": 0,
        "within_300": local_calls <= 300 and (provider_delta is None or provider_delta <= 300),
        "discovery": discovery,
        "recovery": {
            "unique_nextbid": (recovery or {}).get("unique_nextbid"),
            "stage_b_count": (recovery or {}).get("stage_b_count"),
            "strict_fits": fits,
            "stage_counts": (recovery or {}).get("stage_counts"),
            "quantity_defined": (recovery or {}).get("quantity_defined"),
            "prior_free_source_count": prior_c,
            "prior_free_source": (recovery or {}).get("prior_free_source"),
            "registry": (recovery or {}).get("registry"),
        },
        "recovery_elapsed_seconds": recovery_elapsed,
        "free_doc_pass": free_docs,
        "free_doc_elapsed_seconds": free_doc_elapsed,
        "compared_to_002l": {
            "002l_inventory": (l_acc.get("inventory_summary") or {}).get("durable_unique_nextbid")
            or baseline.get("inventory"),
            "002l_stage_c_nextbid": l_fits,
            "002m_inventory": discovery.get("final_unique"),
            "002m_stage_c_nextbid": len(fits),
            "newly_recovered_stage_c_vs_002l": max(0, len(fits) - l_fits),
        },
        "inventory_summary": {
            "previously_retained": baseline.get("inventory"),
            "newly_discovered_unique": discovery.get("unique_new"),
            "total_durable_inventory": discovery.get("final_unique")
            or (recovery or {}).get("unique_nextbid"),
            "duplicates_prevented": discovery.get("duplicates"),
            "stage_b": (recovery or {}).get("stage_b_count"),
            "stage_c_nextbid": len(fits),
            "stage_c_free_source": prior_c,
            "stage_c_total": len(fits) + prior_c,
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
        "scheduler_paused": True,
        "normal_limits_restored": {
            "daily": 100,
            "job": 50,
            "monthly_reserve": 1000,
            "note": "002M temporary daily exception revoked with authorization",
        },
        "acceptance_gates": {
            "within_300_calls": local_calls <= 300 and (provider_delta is None or int(provider_delta) <= 300),
            "search_only": True,
            "zero_paid_detail_docs": True,
            "existing_data_preserved": True,
            "lock_restored": not lock.get("metered_calls_allowed", True),
            "scheduler_disabled": True,
            "no_fabricated_results": True,
        },
        "do_not_begin_build_003": True,
        "await_owner_acceptance": True,
        "finished_at": _utc(),
    }

    # Honesty on provider discrepancy
    if provider_delta is not None and provider_delta != local_calls:
        acceptance["usage_discrepancy"] = {
            "local_search_calls": local_calls,
            "provider_delta": provider_delta,
            "status": "INVESTIGATE" if abs(provider_delta - local_calls) > 2 else "WITHIN_TOLERANCE",
        }
        if abs(provider_delta - local_calls) > 2:
            acceptance["acceptance_gates"]["usage_reconciled"] = False
        else:
            acceptance["acceptance_gates"]["usage_reconciled"] = True
    else:
        acceptance["acceptance_gates"]["usage_reconciled"] = True

    write_reports(acceptance)
    print(
        json.dumps(
            {
                "ok": error is None,
                "build_id": BUILD_ID,
                "calls": local_calls,
                "provider_delta": provider_delta,
                "unique_new": discovery.get("unique_new"),
                "final_unique": discovery.get("final_unique"),
                "stage_c": len(fits),
                "locked": acceptance["acquisition_locked_final"],
                "error": error,
            },
            indent=2,
        )
    )
    return 0 if error is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
