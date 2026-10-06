"""Checkpointed OpenGov recovery batch runner."""

from __future__ import annotations

import json
import logging
from collections import Counter
from typing import Any
from uuid import uuid4

from application_clock import now_utc
from bidnet_recovery.states import ECONOMICS_READY, NOT_PRODUCT, RECOVERY_BLOCKED
from opengov_auth.config import load_opengov_auth_config
from opengov_auth.telemetry import record_discovery_counters
from opengov_recovery.recover import is_opengov_rec, recover_one, recovery_priority_tier

log = logging.getLogger("govtracker.opengov_recovery.batch")

CHECKPOINT = "opengov_auth/recovery_checkpoint.json"
REPORT = "opengov_auth/last_recovery_report.json"


def _paths():
    from m3_data_root import data_path

    return data_path(CHECKPOINT), data_path(REPORT)


def _load_ck() -> dict[str, Any]:
    path, _ = _paths()
    if not path.exists():
        return {"kind": "OpenGovRecoveryCheckpoint", "processed_ids": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"kind": "OpenGovRecoveryCheckpoint", "processed_ids": []}


def _save_ck(payload: dict[str, Any]) -> None:
    path, _ = _paths()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def opengov_recovery_funnel(store: dict[str, Any] | None = None) -> dict[str, Any]:
    from phase_l.l23_full_population_funnel import load_store

    store = store if store is not None else load_store()
    counts: Counter = Counter()
    docs_with_files = 0
    total = 0
    for rec in store.values():
        if not isinstance(rec, dict) or not is_opengov_rec(rec):
            continue
        total += 1
        br = rec.get("opengov_recovery") if isinstance(rec.get("opengov_recovery"), dict) else {}
        counts[br.get("state") or "DISCOVERED"] += 1
        docs_meta = rec.get("attachments_metadata") or []
        real = [
            d
            for d in docs_meta
            if isinstance(d, dict)
            and d.get("document_type") in {"attachment", "linked"}
            and d.get("document_url")
        ]
        if real:
            docs_with_files += 1
    ready = counts.get(ECONOMICS_READY, 0)
    detail = sum(counts.get(s, 0) for s in counts if s not in {"DISCOVERED", RECOVERY_BLOCKED, NOT_PRODUCT})
    return {
        "kind": "OpenGovRecoveryFunnel",
        "generated_at": now_utc().isoformat(),
        "OpenGov_discovered": total,
        "Detail_recovered": detail,
        "Documents_recovered": docs_with_files,
        "Economics_ready": ready,
        "state_counts": dict(counts),
        "OPENGOV_DETAIL_RECOVERY_RATE": round(100.0 * detail / total, 2) if total else 0.0,
        "OPENGOV_DOCUMENT_RECOVERY_RATE": round(100.0 * docs_with_files / total, 2) if total else 0.0,
        "OPENGOV_ECONOMICS_READY_RATE": round(100.0 * ready / total, 2) if total else 0.0,
    }


def run_opengov_recovery(
    *,
    limit: int | None = None,
    batch_size: int = 20,
    resume: bool = True,
    persist: bool = True,
    force: bool = False,
    use_auth: bool | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    from phase_l.l23_full_population_funnel import load_store, save_store
    from phase_l.owner_ui_service import _is_available_rec

    cfg = load_opengov_auth_config()
    run_id = run_id or f"OGR-{uuid4().hex[:12]}"
    started = now_utc().isoformat()
    store = load_store()
    ck = _load_ck() if resume else {"kind": "OpenGovRecoveryCheckpoint", "processed_ids": []}
    done = set(ck.get("processed_ids") or [])

    rec_budget = int(limit if limit is not None else cfg.recovery_batch_size)
    back_budget = int(cfg.backlog_batch_size)

    priority: list[tuple[int, str, dict]] = []
    backlog: list[tuple[int, str, dict]] = []
    for cid, rec in store.items():
        if not isinstance(rec, dict) or not is_opengov_rec(rec):
            continue
        if not _is_available_rec(rec):
            continue
        if not force and cid in done:
            br = rec.get("opengov_recovery") or {}
            if br.get("state") in {ECONOMICS_READY, NOT_PRODUCT, "EXPIRED"}:
                continue
            if br.get("state") and br.get("state") != "DISCOVERED" and "AUTH_REQUIRED" not in (br.get("blockers") or []):
                continue
        tier = recovery_priority_tier(rec)
        if tier == 9:
            continue
        if tier <= 2:
            priority.append((tier, cid, rec))
        else:
            backlog.append((tier, cid, rec))
    priority.sort(key=lambda x: (x[0], x[1]))
    backlog.sort(key=lambda x: (x[0], x[1]))
    selected = priority[:rec_budget]
    selected.extend(backlog[:back_budget])

    should_auth = use_auth if use_auth is not None else cfg.can_authenticate
    auth_info = None
    auth_client = None
    auth_stop = None

    if should_auth:
        from opengov_auth import OpenGovAuthenticatedClient

        auth_client = OpenGovAuthenticatedClient()
        auth_result = auth_client.ensure_authenticated()
        auth_info = auth_result.to_dict()
        if not auth_result.authenticated:
            auth_stop = auth_result.status
            try:
                auth_client.close()
            except Exception:
                pass
            report = {
                "kind": "OpenGovRecoveryBatchReport",
                "run_id": run_id,
                "started_at": started,
                "completed_at": now_utc().isoformat(),
                "auth": auth_info,
                "auth_stop": auth_stop,
                "processed": 0,
                "selected": len(selected),
                "stats": {"auth_blocked": 1},
                "funnel": opengov_recovery_funnel(store),
                "note": "Authenticated OpenGov recovery stopped before processing records.",
            }
            if persist:
                _, rpath = _paths()
                rpath.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
            return report

    stats: Counter = Counter()
    sample: list[dict[str, Any]] = []
    processed = 0
    try:
        for tier, cid, rec in selected:
            out = recover_one(
                cid,
                rec,
                auth_client=auth_client if auth_client and auth_client.is_authenticated else None,
                fetch_live=True,
            )
            if out.get("auth_challenge"):
                auth_stop = "AUTH_CHALLENGE"
                break
            processed += 1
            done.add(cid)
            stats["attempted"] += 1
            stats[f"state:{out.get('state')}"] += 1
            for k in (
                "deadline_recovered",
                "documents",
                "improved",
                "issuing_org",
                "solicitation_number",
                "gov_value",
                "public_cost",
                "economics_ready",
                "authenticated",
            ):
                if out.get(k):
                    stats[k if k != "documents" else "documents_recovered"] += (
                        int(out["documents"]) if k == "documents" else 1
                    )
            if len(sample) < 20:
                sample.append({"id": cid, "tier": tier, "title": (rec.get("title") or "")[:80], **out})
            if persist and processed % batch_size == 0:
                _save_ck({"kind": "OpenGovRecoveryCheckpoint", "run_id": run_id, "processed_ids": list(done)[-100000:]})
                save_store(store)
    finally:
        if auth_client is not None:
            try:
                auth_client.close()
            except Exception:
                pass

    funnel = opengov_recovery_funnel(store)
    record_discovery_counters(
        {
            "opengov_documents_recovered": funnel.get("Documents_recovered") or 0,
            "opengov_economics_ready": funnel.get("Economics_ready") or 0,
            "opengov_product_candidates": sum(
                1
                for r in store.values()
                if isinstance(r, dict)
                and is_opengov_rec(r)
                and str(r.get("universe_class") or "") in {"TANGIBLE_PRODUCT", "MIXED_PRODUCT_SERVICE"}
            ),
            "OPENGOV_DETAIL_RECOVERY_RATE": funnel.get("OPENGOV_DETAIL_RECOVERY_RATE") or 0,
            "OPENGOV_DOCUMENT_RECOVERY_RATE": funnel.get("OPENGOV_DOCUMENT_RECOVERY_RATE") or 0,
            "OPENGOV_ECONOMICS_READY_RATE": funnel.get("OPENGOV_ECONOMICS_READY_RATE") or 0,
        }
    )

    report = {
        "kind": "OpenGovRecoveryBatchReport",
        "run_id": run_id,
        "started_at": started,
        "completed_at": now_utc().isoformat(),
        "selected": len(selected),
        "processed": processed,
        "auth": auth_info,
        "auth_stop": auth_stop,
        "stats": dict(stats),
        "funnel": funnel,
        "sample": sample,
        "SAM_API_CALLS": 0,
    }
    if persist:
        save_store(store)
        _save_ck(
            {
                "kind": "OpenGovRecoveryCheckpoint",
                "run_id": run_id,
                "processed_ids": list(done)[-100000:],
                "updated_at": now_utc().isoformat(),
            }
        )
        _, rpath = _paths()
        rpath.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return report
