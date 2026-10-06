"""Bridge live discovery → L23 canonical opportunity store under M3_DATA_ROOT.

Discovery historically wrote only to the M3 pipeline store. Owner UI Available Deals
reads l23_canonical_population_store.json. This module is the single merge path that
keeps those aligned: normalize → dedupe → incremental merge → expiry refresh →
run history → daily change history.
"""

from __future__ import annotations

import json
import logging
import os
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from application_clock import now_utc

log = logging.getLogger("govtracker.canonical_discovery_bridge")

RUN_HISTORY_FILE = "m3_discovery_run_history.json"
DAILY_HISTORY_FILE = "m3_discovery_daily_history.json"
ENRICHMENT_KEYS = (
    "supplier_research",
    "supplier_terms",
    "pricing_history",
    "historical_pricing",
    "financing_assessment",
    "financing_intelligence",
    "eligibility",
    "eligibility_work",
    "owner_decision",
    "owner_decisions",
    "owner_notes",
    "notes",
    "quote_history",
    "quotes",
    "call_sheet",
    "deal_priority_score",
    "priority_score",
    "fast_research_score",
    "cheap_pipeline",
    "research_budget",
    "evidence_references",
    "package_health",
    "commercial_intelligence",
)

MUTABLE_TOP = (
    "deadline",
    "timezone",
    "buyer",
    "title",
    "description",
    "authoritative_url",
    "submission_path",
    "freshness",
    "source_status",
    "access_status",
    "registration_status",
    "jurisdiction",
    "platform",
    "solicitation_event_id",
    "product_service_classification",
    "is_federal",
    "amendment_state",
    "attachments_metadata",
)


def _utc() -> str:
    return now_utc().isoformat()


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def history_path() -> Path:
    from m3_data_root import data_path

    return data_path(RUN_HISTORY_FILE)


def daily_history_path() -> Path:
    from m3_data_root import data_path

    return data_path(DAILY_HISTORY_FILE)


def canonical_read_path() -> Path:
    from m3_data_root import canonical_opportunity_store_path

    return canonical_opportunity_store_path()


def canonical_write_path() -> Path:
    # Authoritative write must equal read — both resolve through M3_DATA_ROOT.
    return canonical_read_path()


def path_parity_report() -> dict[str, Any]:
    read_p = canonical_read_path().resolve()
    write_p = canonical_write_path().resolve()
    ok = read_p == write_p
    return {
        "ok": ok,
        "canonical_read_path": str(read_p),
        "canonical_write_path": str(write_p),
        "data_root": str(read_p.parent),
        "m3_data_root_env_set": bool((os.environ.get("M3_DATA_ROOT") or "").strip()),
        "unhealthy_reason": None if ok else "CANONICAL_READ_WRITE_PATH_MISMATCH",
    }


def assert_path_parity() -> dict[str, Any]:
    report = path_parity_report()
    if not report["ok"]:
        log.error(
            "Canonical path mismatch read=%s write=%s",
            report["canonical_read_path"],
            report["canonical_write_path"],
        )
    return report


def available_count(store: dict[str, dict[str, Any]]) -> int:
    from phase_l.owner_ui_service import _is_available_rec

    return sum(1 for rec in store.values() if _is_available_rec(rec))


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return deepcopy(default)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        log.exception("Failed reading %s", path)
        return deepcopy(default)


def _save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def discovery_record_to_canonical(row: dict[str, Any], *, funnel_state: str | None = None) -> dict[str, Any]:
    from phase_l.l23_full_population_funnel import ACCESSIBLE_PRODUCT, RAW, to_canonical_record

    state = funnel_state or (
        ACCESSIBLE_PRODUCT if row.get("cheap_screen_survive") else RAW
    )
    rec = to_canonical_record(row, funnel_state=state)
    status = str(row.get("status") or "").upper()
    if status in {"EXPIRED", "CANCELLED", "CANCELED", "CLOSED", "AWARDED"}:
        rec["freshness"] = "EXPIRED" if status == "EXPIRED" else status
        if status in {"EXPIRED", "CANCELLED", "CANCELED"}:
            rec["current_funnel_state"] = "EXPIRED" if status == "EXPIRED" else "CANCELED"
        if status == "CLOSED":
            rec["source_status"] = "CLOSED_EXPIRED"
    # Preserve attachment / amendment hints when present
    if row.get("attachments") or row.get("attachments_metadata"):
        rec["attachments_metadata"] = row.get("attachments_metadata") or row.get("attachments")
    if row.get("amendment") or row.get("amendment_state"):
        rec["amendment_state"] = row.get("amendment_state") or row.get("amendment")
    return rec


def _merge_provenance(existing: dict[str, Any], incoming: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in list(existing.get("source_provenance") or []) + list(incoming.get("source_provenance") or []):
        if not isinstance(item, dict):
            continue
        key = f"{item.get('source_id')}|{item.get('url')}|{item.get('feed')}"
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _preserve_enrichment(existing: dict[str, Any], updated: dict[str, Any]) -> None:
    for key in ENRICHMENT_KEYS:
        if existing.get(key) is not None and updated.get(key) is None:
            updated[key] = deepcopy(existing[key])
    # row_ref economics / research nests
    er = existing.get("row_ref") if isinstance(existing.get("row_ref"), dict) else {}
    ur = updated.get("row_ref") if isinstance(updated.get("row_ref"), dict) else {}
    merged_ref = deepcopy(er)
    for k, v in ur.items():
        if v is None:
            continue
        if k in {"economics", "supplier_research", "financing_assessment", "historical_award_price"} and er.get(k):
            # Keep existing verified economics unless incoming explicitly supersedes
            if k == "economics" and isinstance(v, dict) and v.get("supersedes_existing"):
                merged_ref[k] = v
            elif k not in er or er.get(k) in (None, {}, []):
                merged_ref[k] = v
            # else keep existing
        else:
            if k not in merged_ref or merged_ref.get(k) in (None, "", [], {}):
                merged_ref[k] = v
            elif k in {"detail_url", "source_url", "deadline", "solicitation_number", "live_status"}:
                merged_ref[k] = v
    updated["row_ref"] = merged_ref
    # Never regress funnel past RAW/ACCESSIBLE when enrichment already advanced
    from phase_l.l23_full_population_funnel import FUNNEL_STATES

    order = {s: i for i, s in enumerate(FUNNEL_STATES)}
    ex_state = str(existing.get("current_funnel_state") or "RAW")
    in_state = str(updated.get("current_funnel_state") or "RAW")
    if order.get(ex_state, 0) > order.get(in_state, 0) and ex_state not in {
        "FAST_REJECT",
        "EXPIRED",
        "CANCELED",
        "CANCELLED",
        "HARD_REJECT",
    }:
        updated["current_funnel_state"] = ex_state


def incremental_merge(
    store: dict[str, dict[str, Any]],
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Merge discovery-derived canonical records. Returns counters + example ids."""
    added = 0
    updated = 0
    duplicates = 0
    seen_in_batch: set[str] = set()
    new_ids: list[str] = []
    updated_ids: list[str] = []

    for raw in records:
        if not isinstance(raw, dict):
            continue
        if raw.get("canonical_opportunity_id") and "kind" in raw:
            incoming = deepcopy(raw)
        else:
            incoming = discovery_record_to_canonical(raw)
        cid = str(incoming.get("canonical_opportunity_id") or "").strip()
        if not cid:
            continue
        if cid in seen_in_batch:
            duplicates += 1
            if cid in store:
                store[cid]["source_provenance"] = _merge_provenance(store[cid], incoming)
            continue
        seen_in_batch.add(cid)

        if cid not in store:
            store[cid] = incoming
            added += 1
            if len(new_ids) < 20:
                new_ids.append(cid)
            continue

        existing = store[cid]
        duplicates += 1
        before = json.dumps(
            {k: existing.get(k) for k in MUTABLE_TOP},
            sort_keys=True,
            default=str,
        )
        patched = deepcopy(existing)
        for key in MUTABLE_TOP:
            val = incoming.get(key)
            if val is not None and val != "":
                patched[key] = val
        patched["source_provenance"] = _merge_provenance(existing, incoming)
        patched["updated_at"] = _utc()
        _preserve_enrichment(existing, patched)
        # Prefer incoming funnel only when existing is RAW-like
        if str(existing.get("current_funnel_state") or "RAW") in {"RAW", "NEEDS_SOURCE_DATA"} and incoming.get(
            "current_funnel_state"
        ):
            patched["current_funnel_state"] = incoming["current_funnel_state"]
        after = json.dumps(
            {k: patched.get(k) for k in MUTABLE_TOP},
            sort_keys=True,
            default=str,
        )
        store[cid] = patched
        if before != after or len(patched.get("source_provenance") or []) > len(existing.get("source_provenance") or []):
            updated += 1
            if len(updated_ids) < 20:
                updated_ids.append(cid)

    return {
        "new_canonical_opportunities_added": added,
        "existing_opportunities_updated": updated,
        "duplicates_detected": duplicates,
        "example_new_ids": new_ids,
        "example_updated_ids": updated_ids,
    }


def refresh_expiry_status(store: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Reevaluate deadlines/status without requiring rediscovery."""
    now = now_utc()
    expired = 0
    canceled = 0
    due_soon = 0
    closed = 0
    amended = 0
    active = 0
    changed_ids: list[str] = []

    for cid, rec in store.items():
        changed = False
        deadline = _parse_dt(rec.get("deadline"))
        status = str(rec.get("source_status") or "").upper()
        freshness = str(rec.get("freshness") or "").upper()
        funnel = str(rec.get("current_funnel_state") or "").upper()
        amend = rec.get("amendment_state")

        if amend and str(amend).upper() not in {"NONE", "NO", "FALSE", ""}:
            amended += 1

        if status in {"CANCELED", "CANCELLED"} or freshness in {"CANCELED", "CANCELLED"}:
            if funnel not in {"CANCELED", "CANCELLED", "EXPIRED"}:
                rec["current_funnel_state"] = "CANCELED"
                changed = True
            canceled += 1
        elif status in {"CLOSED", "CLOSED_EXPIRED", "AWARDED"} or freshness in {"CLOSED", "AWARDED"}:
            if freshness != "EXPIRED" and status == "CLOSED_EXPIRED":
                rec["freshness"] = "EXPIRED"
                changed = True
            closed += 1
        elif deadline and deadline < now:
            if freshness != "EXPIRED":
                rec["freshness"] = "EXPIRED"
                changed = True
            if funnel not in {"EXPIRED", "CANCELED", "CANCELLED", "FAST_REJECT", "HARD_REJECT"}:
                # Keep FAST_REJECT; otherwise mark expired for available-count drop
                if funnel not in {"FAST_REJECT", "HARD_REJECT", "REJECTED"}:
                    rec["current_funnel_state"] = "EXPIRED"
                    changed = True
            if str(rec.get("source_status") or "").upper() not in {
                "CANCELED",
                "CANCELLED",
                "CLOSED_EXPIRED",
            }:
                # Leave ACTIVE_* statuses but freshness EXPIRED drives availability
                pass
            expired += 1
        else:
            active += 1
            if deadline and deadline <= now + timedelta(days=7):
                due_soon += 1
                if not rec.get("due_soon"):
                    rec["due_soon"] = True
                    changed = True

        if changed:
            rec["updated_at"] = _utc()
            if len(changed_ids) < 25:
                changed_ids.append(cid)

    return {
        "expired": expired,
        "canceled": canceled,
        "closed": closed,
        "amended": amended,
        "active": active,
        "due_soon": due_soon,
        "status_changed_ids": changed_ids,
        "expired_or_removed_from_available": expired + canceled,
    }


def append_run_history(record: dict[str, Any]) -> dict[str, Any]:
    path = history_path()
    payload = _load_json(path, {"kind": "M3DiscoveryRunHistory", "runs": []})
    runs = list(payload.get("runs") or [])
    runs.insert(0, record)
    payload["runs"] = runs[:90]  # ~3 months of twice-daily
    payload["updated_at"] = _utc()
    payload["kind"] = "M3DiscoveryRunHistory"
    _save_json(path, payload)
    return record


def upsert_daily_history(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per calendar day (UTC date). Does not fabricate missing days."""
    path = daily_history_path()
    payload = _load_json(path, {"kind": "M3DiscoveryDailyHistory", "days": []})
    days = list(payload.get("days") or [])
    day_key = str(snapshot.get("date") or now_utc().date().isoformat())
    remaining = [d for d in days if str(d.get("date")) != day_key]
    remaining.insert(0, snapshot)
    # Keep last 7 completed day rows only (trim older)
    remaining = remaining[:7]
    payload["days"] = remaining
    payload["updated_at"] = _utc()
    payload["kind"] = "M3DiscoveryDailyHistory"
    _save_json(path, payload)
    return remaining


def load_run_history(limit: int = 20) -> list[dict[str, Any]]:
    payload = _load_json(history_path(), {"runs": []})
    return list(payload.get("runs") or [])[:limit]


def load_daily_history() -> list[dict[str, Any]]:
    payload = _load_json(daily_history_path(), {"days": []})
    return list(payload.get("days") or [])[:7]


def _sam_usage_today() -> dict[str, Any]:
    try:
        from discovery.sam_budgeted_client import sam_budget_status

        st = sam_budget_status()
        return {
            "used": st.get("calls_used_today"),
            "limit": st.get("daily_limit") or 10,
            "remaining": st.get("calls_remaining"),
            "raw": st,
        }
    except Exception:
        try:
            from discovery.sam_budgeted_client import dashboard

            st = dashboard()
            return {
                "used": st.get("calls_used"),
                "limit": st.get("daily_limit") or 10,
                "remaining": st.get("calls_remaining"),
                "raw": st,
            }
        except Exception:
            try:
                from api_budget import sam_daily_call_budget

                return {"used": None, "limit": sam_daily_call_budget(), "remaining": None}
            except Exception:
                return {"used": None, "limit": 10, "remaining": None}


def classify_run_status(
    *,
    sources_failed: int,
    sources_successful: int,
    fatal: bool = False,
) -> str:
    if fatal or (sources_successful <= 0 and sources_failed > 0):
        return "FAILED"
    if sources_failed > 0 and sources_successful > 0:
        return "PARTIAL"
    if sources_successful > 0 or sources_failed == 0:
        return "SUCCESS"
    return "FAILED"


def merge_discovery_into_canonical(
    *,
    run_id: str,
    trigger: str,
    records: list[dict[str, Any]],
    sources_attempted: list[str] | None = None,
    sources_succeeded: list[str] | None = None,
    sources_failed: list[str] | None = None,
    source_counts: dict[str, Any] | None = None,
    raw_opportunities_found: int = 0,
    records_normalized: int = 0,
    error_summary: str | None = None,
    api_usage: dict[str, Any] | None = None,
    environment: str | None = None,
    started_at: str | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """Full post-discovery cycle against the authoritative L23 store."""
    from phase_l.l23_full_population_funnel import load_store, save_store

    parity = assert_path_parity()
    started = started_at or _utc()
    store = load_store()
    before_total = len(store)
    before_available = available_count(store)

    merge_stats = incremental_merge(store, records)
    expiry_stats = refresh_expiry_status(store)

    after_total = len(store)
    after_available = available_count(store)

    if persist:
        # Write only through canonical_write_path (== read path)
        write_p = canonical_write_path()
        if write_p.resolve() != canonical_read_path().resolve():
            raise RuntimeError("CANONICAL_READ_WRITE_PATH_MISMATCH")
        save_store(store)

    sam = _sam_usage_today()
    attempted = list(sources_attempted or [])
    succeeded = list(sources_succeeded or [])
    failed = list(sources_failed or [])
    run_status = classify_run_status(
        sources_failed=len(failed),
        sources_successful=len(succeeded) if succeeded or failed else 1,
        fatal=bool(error_summary and not succeeded and not merge_stats["new_canonical_opportunities_added"]),
    )
    if error_summary and merge_stats["new_canonical_opportunities_added"] == 0 and after_total == before_total:
        if failed and not succeeded:
            run_status = "FAILED"
        elif failed and succeeded:
            run_status = "PARTIAL"

    completed = _utc()
    duration_s = None
    try:
        duration_s = round((_parse_dt(completed) - _parse_dt(started)).total_seconds(), 2)  # type: ignore[operator]
    except Exception:
        duration_s = None

    record = {
        "run_id": run_id,
        "started_at": started,
        "completed_at": completed,
        "trigger": str(trigger or "manual").lower(),
        "environment": environment or os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("ENV") or "local",
        "sources_attempted": attempted,
        "sources_succeeded": succeeded,
        "sources_failed": failed,
        "source_specific_counts": source_counts or {},
        "raw_opportunities_found": int(raw_opportunities_found),
        "records_normalized": int(records_normalized or len(records)),
        "duplicates_detected": merge_stats["duplicates_detected"],
        "new_canonical_opportunities_added": merge_stats["new_canonical_opportunities_added"],
        "existing_opportunities_updated": merge_stats["existing_opportunities_updated"],
        "expired_canceled_opportunities_changed": expiry_stats["expired_or_removed_from_available"],
        "currently_available_before": before_available,
        "currently_available_after": after_available,
        "canonical_total_before": before_total,
        "canonical_total_after": after_total,
        "error_summary": error_summary,
        "api_usage": api_usage or {},
        "sam_calls_used": sam.get("used") if isinstance(sam, dict) else None,
        "sam_calls_limit": sam.get("limit") if isinstance(sam, dict) else 10,
        "sam_budget": sam,
        "duration_seconds": duration_s,
        "run_status": run_status,
        "path_parity": parity,
        "example_new_ids": merge_stats["example_new_ids"],
        "example_updated_ids": merge_stats["example_updated_ids"],
        "expiry": expiry_stats,
    }

    if persist:
        append_run_history(record)
        day = now_utc().date().isoformat()
        upsert_daily_history(
            {
                "date": day,
                "canonical": after_total,
                "available": after_available,
                "new": merge_stats["new_canonical_opportunities_added"],
                "updated": merge_stats["existing_opportunities_updated"],
                "expired": expiry_stats["expired"],
                "run_id": run_id,
                "run_status": run_status,
                "updated_at": completed,
            }
        )

    return record


def discovery_health_payload() -> dict[str, Any]:
    from phase_l.l23_full_population_funnel import load_store

    parity = path_parity_report()
    runs = load_run_history(limit=10)
    daily = load_daily_history()
    store = load_store()
    last = runs[0] if runs else None
    last_success = next((r for r in runs if r.get("run_status") == "SUCCESS"), None)
    last_okish = next((r for r in runs if r.get("run_status") in {"SUCCESS", "PARTIAL"}), None)

    try:
        from m3_discovery_service import compute_next_scheduled_run, discovery_status

        status = discovery_status()
        next_run = status.get("next_scheduled_run") or compute_next_scheduled_run()
        running = bool(status.get("running"))
        last_attempt_state = status.get("last_attempt") or status.get("last_successful_completion")
    except Exception:
        status = {}
        next_run = None
        running = False
        last_attempt_state = None

    run_status = "NEVER_RUN"
    if running:
        run_status = "RUNNING"
    elif last:
        run_status = str(last.get("run_status") or "FAILED")
    elif last_attempt_state:
        st = str((last_attempt_state or {}).get("status") or "")
        if "COMPLETE" in st:
            run_status = "PARTIAL" if "WARN" in st else "SUCCESS"
        elif st == "FAILED":
            run_status = "FAILED"

    sam = _sam_usage_today()
    today = now_utc().date().isoformat()
    today_row = next((d for d in daily if str(d.get("date")) == today), None)

    return {
        "kind": "DiscoveryHealth",
        "last_successful_daily_run": (last_success or last_okish or {}).get("completed_at") if (last_success or last_okish) else None,
        "last_attempted_run": (last or {}).get("completed_at") or (last or {}).get("started_at"),
        "run_status": run_status,
        "canonical_opportunities": len(store),
        "currently_available": available_count(store),
        "new_opportunities_added_today": (today_row or {}).get("new") or 0,
        "opportunities_updated_today": (today_row or {}).get("updated") or 0,
        "opportunities_expired_removed_today": (today_row or {}).get("expired") or 0,
        "sources_succeeded": len((last or {}).get("sources_succeeded") or []),
        "sources_failed": len((last or {}).get("sources_failed") or []),
        "sources_attempted": len((last or {}).get("sources_attempted") or []),
        "sam_calls_used_today": sam.get("used") if isinstance(sam, dict) else None,
        "sam_calls_limit": sam.get("limit") if isinstance(sam, dict) else 10,
        "next_scheduled_run": next_run,
        "last_run": last,
        "daily_history": daily,
        "path_parity": parity,
        "discovery_scheduler": {
            "enabled": (os.environ.get("M3_DISCOVERY_ENABLED") or "true").strip().lower()
            in {"1", "true", "yes", "on"},
            "running": running,
            "status": status,
        },
    }


def discovery_diagnostics() -> dict[str, Any]:
    parity = path_parity_report()
    health = discovery_health_payload()
    runs = load_run_history(limit=5)
    last = runs[0] if runs else None
    per_source_success: dict[str, Any] = {}
    per_source_error: dict[str, Any] = {}
    for run in runs:
        for sid in run.get("sources_succeeded") or []:
            per_source_success.setdefault(sid, run.get("completed_at"))
        for sid in run.get("sources_failed") or []:
            per_source_error.setdefault(sid, run.get("error_summary") or "failed")

    lock = {}
    try:
        from m3_discovery_service import discovery_status

        st = discovery_status()
        lock = {
            "held": bool(st.get("running")),
            "run_id": (st.get("current_run") or {}).get("run_id") if isinstance(st.get("current_run"), dict) else st.get("run_id"),
        }
    except Exception:
        lock = {"held": False, "run_id": None}

    sam = _sam_usage_today()
    return {
        "kind": "DiscoveryDiagnostics",
        "scheduler_enabled": health["discovery_scheduler"]["enabled"],
        "next_scheduled_run": health.get("next_scheduled_run"),
        "current_run_active": bool(health["discovery_scheduler"].get("running")),
        "data_root": parity.get("data_root"),
        "canonical_read_path": parity.get("canonical_read_path"),
        "canonical_write_path": parity.get("canonical_write_path"),
        "path_parity_ok": parity.get("ok"),
        "unhealthy_reason": parity.get("unhealthy_reason"),
        "canonical_count": health.get("canonical_opportunities"),
        "available_count": health.get("currently_available"),
        "last_run_id": (last or {}).get("run_id"),
        "last_successful_run": health.get("last_successful_daily_run"),
        "per_source_last_success": per_source_success,
        "per_source_last_error": per_source_error,
        "sam_calls_used_today": sam.get("used") if isinstance(sam, dict) else None,
        "sam_calls_limit": sam.get("limit") if isinstance(sam, dict) else 10,
        "lock_status": lock,
        "run_history_path": str(history_path()),
        "daily_history_path": str(daily_history_path()),
    }
