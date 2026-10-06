"""Freeze DEEP_COMPLETION_CORPUS_V1 — exact 5 candidates."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from application_clock import now_utc
from deep_completion_3to5.models import BUILD, CORPUS, DEEP_OIDS, PRIOR_MLR_CK, PRIOR_REV
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


def freeze_deep_completion_corpus(*, force: bool = False) -> dict[str, Any]:
    existing = _load(CORPUS)
    if existing.get("immutable") and existing.get("opportunity_ids") == DEEP_OIDS and not force:
        assert int(existing.get("opportunities") or 0) == len(DEEP_OIDS)
        assert int(existing.get("material_lines") or 0) == len(existing.get("lines") or [])
        return existing

    mlr = _load(PRIOR_MLR_CK)
    ops = mlr.get("opportunities") or {}
    rev = (_load(PRIOR_REV).get("by_opportunity") or {})

    lines: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    for oid in DEEP_OIDS:
        o = ops.get(oid) or {}
        buyer = oid.split(":")[1] if ":" in oid else None
        rev_row = rev.get(oid) or {}
        rev_best = ((rev_row.get("revenue") or {}).get("best") or {})
        opp_lines = list(o.get("lines") or [])
        items.append(
            {
                "opportunity_id": oid,
                "buyer": buyer,
                "solicitation_id": oid.split(":")[-1] if ":" in oid else oid,
                "deadline": None,
                "material_line_count": len(opp_lines),
                "usable_identities": sum(1 for l in opp_lines if l.get("identity_usable")),
                "quote_required": sum(1 for l in opp_lines if l.get("acquisition_state") == "QUOTE_REQUIRED"),
                "coverage": o.get("coverage"),
                "revenue_seed": {
                    "type": rev_best.get("revenue_evidence_type"),
                    "value": rev_best.get("reference_value"),
                    "source": rev_best.get("source"),
                    "snippet": (rev_best.get("snippet") or "")[:240],
                },
                "execution_risks_seed": [],
                "priority_rank": DEEP_OIDS.index(oid) + 1,
            }
        )
        for ln in opp_lines:
            lines.append(
                {
                    "opportunity_id": oid,
                    "line_id": ln.get("line_id"),
                    "clin": ln.get("clin"),
                    "identity_class": ln.get("identity_confidence"),
                    "identity_usable": ln.get("identity_usable"),
                    "manufacturer": ln.get("manufacturer"),
                    "mpn": ln.get("mpn") or ln.get("part_number"),
                    "model": ln.get("model"),
                    "description": ln.get("description"),
                    "quantity": ln.get("quantity"),
                    "uom": ln.get("uom"),
                    "pack": ln.get("pack"),
                    "brand_equal": ln.get("equal_rule"),
                    "quote_required_state": ln.get("acquisition_state"),
                    "quote_subtype": ln.get("quote_subtype"),
                    "materiality_tier": ln.get("materiality_tier"),
                    "category": ln.get("category"),
                    "source_document": ln.get("source_document"),
                    "source_kind": ln.get("source_kind"),
                    "source_provenance": ln.get("page_sheet_cell_provenance"),
                    "nsn": ln.get("nsn"),
                    "cluster_id": ln.get("cluster_id"),
                }
            )

    payload = {
        "name": "DEEP_COMPLETION_CORPUS_V1",
        "build": BUILD,
        "frozen_at": now_utc().isoformat(),
        "immutable": True,
        "run_id": f"DC35-{uuid4().hex[:10]}",
        "opportunities": len(DEEP_OIDS),
        "opportunity_ids": list(DEEP_OIDS),
        "material_lines": len(lines),
        "usable_identities": sum(1 for l in lines if l.get("identity_usable")),
        "quote_required": sum(1 for l in lines if l.get("quote_required_state") == "QUOTE_REQUIRED"),
        "public_priced": sum(1 for l in lines if l.get("quote_required_state") == "PRICED_EXECUTABLE"),
        "items": items,
        "lines": lines,
        "note": "Exact 5 deep-completion candidates. Do not substitute.",
    }
    assert payload["opportunity_ids"] == DEEP_OIDS
    assert payload["material_lines"] == len({l["line_id"] for l in lines})
    _save(CORPUS, payload)
    return payload


def load_corpus() -> dict[str, Any]:
    data = _load(CORPUS)
    if not data.get("lines"):
        data = freeze_deep_completion_corpus()
    return data
