"""Freeze baseline + load unresolved hard corpus with prior candidate URLs."""

from __future__ import annotations

import json
from typing import Any

from application_clock import now_utc
from live_exact_priced_pdp.models import (
    BASELINE,
    BUILD,
    HONEST_BASELINE,
    ORIGINAL_DENOM,
    PRIOR_KPE_CORPUS,
    PRIOR_OSE_CK,
)
from m3_data_root import data_path
from price_coverage_80.corpus import load_corpus
from price_14_expand_patterns.models import READY_14_IDS
from exact_product_url_discovery.sweep import unresolved_items as epu_unresolved


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


def freeze_baseline() -> dict[str, Any]:
    payload = {
        "name": "FULL100_LIVE_PDP_BASELINE_V1",
        "build": BUILD,
        "frozen_at": now_utc().isoformat(),
        "denominator": ORIGINAL_DENOM,
        "executable_prices": HONEST_BASELINE,
        "coverage": round(HONEST_BASELINE / ORIGINAL_DENOM, 4),
        "coverage_pct": round(100.0 * HONEST_BASELINE / ORIGINAL_DENOM, 1),
        "accuracy": 1.0,
        "note": "Frozen after known-PDP extraction v2 (0 new). Soft URLs excluded from valid pool.",
    }
    _save(BASELINE, payload)
    return payload


def load_unresolved_corpus() -> list[dict[str, Any]]:
    """Same unresolved identities with prior candidate URLs for reaudit."""
    full = {i["benchmark_id"]: i for i in (load_corpus().get("items") or [])}
    kpe = _load(PRIOR_KPE_CORPUS)
    by_id: dict[str, dict[str, Any]] = {}

    for row in kpe.get("items") or []:
        bid = row.get("benchmark_id")
        if not bid:
            continue
        base = dict(full.get(bid) or {"benchmark_id": bid})
        base["pdps"] = list(row.get("pdps") or [])
        base["prior_status"] = row.get("prior_status")
        by_id[bid] = base

    # Fill any remaining unresolved not in KPE corpus
    ready = set(READY_14_IDS)
    ose = _load(PRIOR_OSE_CK)
    for bid, row in (ose.get("items") or {}).items():
        if row.get("status") == "EXECUTABLE_PRICE":
            continue
        if bid in by_id:
            continue
        base = dict(full.get(bid) or {"benchmark_id": bid})
        pdps = []
        for p in row.get("exact_pdps") or []:
            pdps.append(
                {
                    "url": p.get("url"),
                    "domain": p.get("domain"),
                    "title": p.get("title"),
                    "discovery_method": p.get("discovery_method"),
                    "soft_identity": bool(p.get("soft_identity")),
                }
            )
        base["pdps"] = pdps
        by_id[bid] = base

    if not by_id:
        for item in epu_unresolved():
            if item["benchmark_id"] in ready:
                continue
            by_id[item["benchmark_id"]] = {**item, "pdps": []}

    return sorted(by_id.values(), key=lambda x: x.get("benchmark_id") or "")
