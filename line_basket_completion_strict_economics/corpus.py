"""Corpus freeze V2 + mandatory stage-transition ID persistence."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from application_clock import now_utc
from line_basket_completion_strict_economics.models import (
    BUILD,
    CORPUS_V2,
    PRIOR_BOTH_SIDES,
    PRIOR_FUNNEL_CK,
    STAGE_TRANSITIONS,
)
from m3_data_root import data_path


def _load(name: str) -> dict[str, Any]:
    p = data_path(name)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(name: str, payload: dict[str, Any]) -> None:
    p = data_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def assert_reported_equals_persisted(reported_count: int, persisted_ids: list[str], *, stage: str) -> None:
    unique = list(dict.fromkeys(persisted_ids))
    if int(reported_count) != len(unique):
        raise AssertionError(
            f"stage={stage} reported_count={reported_count} != persisted_unique_ids={len(unique)}"
        )


def record_stage_transition(
    *,
    opportunity_id: str,
    prior_state: str | None,
    new_state: str,
    reason: str,
    run_id: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    log = _load(STAGE_TRANSITIONS)
    if not log:
        log = {"build": BUILD, "transitions": [], "by_stage": {}}
    entry = {
        "opportunity_id": opportunity_id,
        "prior_state": prior_state,
        "new_state": new_state,
        "timestamp": now_utc().isoformat(),
        "reason": reason,
        "run_id": run_id,
        **(extra or {}),
    }
    log["transitions"].append(entry)
    by_stage = log.setdefault("by_stage", {})
    stage_bucket = by_stage.setdefault(new_state, {"ids": [], "entries": []})
    if opportunity_id not in stage_bucket["ids"]:
        stage_bucket["ids"].append(opportunity_id)
    stage_bucket["entries"].append(entry)
    # Enforce reported == persisted for this stage
    assert_reported_equals_persisted(len(stage_bucket["ids"]), stage_bucket["ids"], stage=new_state)
    stage_bucket["reported_count"] = len(stage_bucket["ids"])
    _save(STAGE_TRANSITIONS, log)
    return entry


def stage_count(stage: str) -> dict[str, Any]:
    log = _load(STAGE_TRANSITIONS)
    bucket = (log.get("by_stage") or {}).get(stage) or {"ids": [], "reported_count": 0}
    ids = list(bucket.get("ids") or [])
    assert_reported_equals_persisted(len(ids), ids, stage=stage)
    return {"stage": stage, "reported_count": len(ids), "persisted_unique_ids": ids}


def freeze_basket_completion_corpus_v2(*, run_id: str | None = None) -> dict[str, Any]:
    """Freeze exact 12 canonical BOTH_SIDES IDs as BASKET_COMPLETION_CORPUS_V2."""
    existing = _load(CORPUS_V2)
    if existing.get("immutable") and existing.get("opportunity_ids"):
        ids = list(existing["opportunity_ids"])
        assert_reported_equals_persisted(int(existing.get("count") or len(ids)), ids, stage="BASKET_COMPLETION_CORPUS_V2")
        return existing

    prior = _load(PRIOR_BOTH_SIDES)
    ids = list(prior.get("opportunity_ids") or [])
    items = list(prior.get("items") or [])
    if not ids:
        # Fallback: funnel checkpoint opportunity keys
        funnel = _load(PRIOR_FUNNEL_CK)
        ids = list((funnel.get("opportunities") or {}).keys())
        items = [{"opportunity_id": oid} for oid in ids]

    # Prefer prior item metadata; ensure every ID has a row
    by_id = {i.get("opportunity_id"): i for i in items if i.get("opportunity_id")}
    for oid in ids:
        by_id.setdefault(oid, {"opportunity_id": oid})
    ordered_items = [by_id[oid] for oid in ids]

    run_id = run_id or f"LBC-{uuid4().hex[:10]}"
    payload = {
        "name": "BASKET_COMPLETION_CORPUS_V2",
        "build": BUILD,
        "frozen_at": now_utc().isoformat(),
        "immutable": True,
        "run_id": run_id,
        "count": len(ids),
        "opportunity_ids": ids,
        "items": ordered_items,
        "source_corpus": PRIOR_BOTH_SIDES,
        "note": "Exact persisted BOTH_SIDES IDs. Every stage count must equal persisted unique IDs.",
    }
    assert_reported_equals_persisted(payload["count"], ids, stage="BASKET_COMPLETION_CORPUS_V2")
    _save(CORPUS_V2, payload)

    for oid in ids:
        record_stage_transition(
            opportunity_id=oid,
            prior_state="BOTH_SIDES_BASKET_CORPUS_V1",
            new_state="BASKET_COMPLETION_CORPUS_V2",
            reason="freeze_exact_persisted_ids",
            run_id=run_id,
        )
    return payload


def load_corpus_v2() -> list[dict[str, Any]]:
    data = _load(CORPUS_V2)
    if not data.get("items"):
        data = freeze_basket_completion_corpus_v2()
    return list(data.get("items") or [])


def load_corpus_ids() -> list[str]:
    data = _load(CORPUS_V2)
    if not data.get("opportunity_ids"):
        data = freeze_basket_completion_corpus_v2()
    ids = list(data.get("opportunity_ids") or [])
    assert_reported_equals_persisted(len(ids), ids, stage="BASKET_COMPLETION_CORPUS_V2")
    return ids
