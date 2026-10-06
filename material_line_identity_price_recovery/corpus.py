"""Freeze MATERIAL_LINE_RECOVERY_CORPUS_V1 — all P0+P1 lines from 12 basket opps."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from application_clock import now_utc
from material_line_identity_price_recovery.models import BUILD, CORPUS, PRIOR_CORPUS_V2, PRIOR_STRICT_CK
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


def freeze_material_line_recovery_corpus(*, force: bool = False) -> dict[str, Any]:
    existing = _load(CORPUS)
    if existing.get("immutable") and existing.get("lines") and not force:
        n = len(existing["lines"])
        assert int(existing.get("count") or 0) == n == len({l["line_id"] for l in existing["lines"]})
        return existing

    prior_ids = list((_load(PRIOR_CORPUS_V2).get("opportunity_ids") or []))
    strict = _load(PRIOR_STRICT_CK)
    ops = strict.get("opportunities") or {}

    lines: list[dict[str, Any]] = []
    for oid in prior_ids:
        o = ops.get(oid) or {}
        for ln in ((o.get("lines") or {}).get("lines") or []):
            if ln.get("materiality_tier") not in {"P0", "P1"}:
                continue
            line_id = ln.get("line_id") or f"{oid}|unknown-{len(lines)}"
            lines.append(
                {
                    "opportunity_id": oid,
                    "line_id": line_id,
                    "clin": ln.get("clin"),
                    "raw_solicitation_description": ln.get("description"),
                    "manufacturer_text": ln.get("manufacturer"),
                    "part_model_text": ln.get("mpn") or ln.get("model"),
                    "nsn": ln.get("nsn"),
                    "brand_equal_language": ln.get("equal_rule"),
                    "quantity": ln.get("quantity"),
                    "uom": ln.get("uom"),
                    "pack": ln.get("pack"),
                    "specifications": ln.get("description"),
                    "current_identity_state": ln.get("terminal_state"),
                    "current_acquisition_state": ln.get("acquisition_status") or ln.get("terminal_state"),
                    "materiality_tier": ln.get("materiality_tier"),
                    "materiality_score": ln.get("materiality_score"),
                    "estimated_extended_value": ln.get("estimated_extended_value"),
                    "category": ln.get("category"),
                    "revenue_evidence": (o.get("revenue_evidence") or o.get("economics") or {}).get("revenue_evidence")
                    or o.get("revenue_evidence"),
                    "source_document": None,
                    "page_sheet_cell_provenance": None,
                    "prior_mpn": ln.get("mpn"),
                    "prior_seller": ln.get("seller"),
                    "prior_unit_cost": ln.get("unit_cost"),
                    "prior_price_origin": ln.get("price_origin"),
                }
            )

    # Deduplicate by line_id
    by_id: dict[str, dict[str, Any]] = {}
    for ln in lines:
        by_id[ln["line_id"]] = ln
    lines = list(by_id.values())

    p0 = sum(1 for l in lines if l.get("materiality_tier") == "P0")
    p1 = sum(1 for l in lines if l.get("materiality_tier") == "P1")
    payload = {
        "name": "MATERIAL_LINE_RECOVERY_CORPUS_V1",
        "build": BUILD,
        "frozen_at": now_utc().isoformat(),
        "immutable": True,
        "run_id": f"MLR-{uuid4().hex[:10]}",
        "opportunity_ids": prior_ids,
        "opportunities": len(prior_ids),
        "count": len(lines),
        "p0": p0,
        "p1": p1,
        "lines": lines,
        "note": "All P0+P1 material lines from BASKET_COMPLETION_CORPUS_V2. Do not substitute.",
    }
    assert payload["count"] == len({l["line_id"] for l in lines})
    assert payload["count"] == p0 + p1
    _save(CORPUS, payload)
    return payload


def load_corpus_lines() -> list[dict[str, Any]]:
    data = _load(CORPUS)
    if not data.get("lines"):
        data = freeze_material_line_recovery_corpus()
    return list(data.get("lines") or [])
