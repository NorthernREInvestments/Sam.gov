"""Freeze KNOWN_PDP_PRICE_CORPUS_V1 from open-seller exact PDPs."""

from __future__ import annotations

import json
from typing import Any

from application_clock import now_utc
from m3_data_root import data_path
from known_pdp_price_extraction.models import (
    BUILD,
    CORPUS,
    HONEST_BASELINE,
    PRIOR_OSE_CK,
    PRIORITY_DOMAINS,
)
from price_coverage_80.corpus import load_corpus


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


def freeze_known_pdp_corpus() -> dict[str, Any]:
    """Build corpus of unresolved items that have claimed exact PDPs."""
    ose = _load(PRIOR_OSE_CK)
    full = {i["benchmark_id"]: i for i in (load_corpus().get("items") or [])}
    rows: list[dict[str, Any]] = []
    by_domain: dict[str, int] = {}

    for bid, row in (ose.get("items") or {}).items():
        if row.get("status") == "EXECUTABLE_PRICE" and row.get("price"):
            continue  # already priced in OSE
        pdps = row.get("exact_pdps") or []
        if not pdps:
            # still include unresolved with zero PDPs so hard cases are tracked
            item = full.get(bid) or {"benchmark_id": bid}
            rows.append(
                {
                    "benchmark_id": bid,
                    "manufacturer": item.get("manufacturer"),
                    "mpn": item.get("mpn") or item.get("part_number"),
                    "description": item.get("description"),
                    "category": item.get("category"),
                    "expected_pack": item.get("expected_pack") or 1,
                    "expected_uom": item.get("expected_uom") or "EA",
                    "condition_required": "NEW",
                    "prior_status": row.get("status"),
                    "prior_fails": (row.get("fails") or [])[:8],
                    "pdps": [],
                }
            )
            continue
        item = full.get(bid) or {"benchmark_id": bid}
        pdp_rows = []
        for p in pdps:
            dom = (p.get("domain") or "").replace("www.", "")
            by_domain[dom] = by_domain.get(dom, 0) + 1
            pdp_rows.append(
                {
                    "url": p.get("url"),
                    "domain": dom,
                    "title": p.get("title"),
                    "discovery_method": p.get("discovery_method"),
                    "soft_identity": bool(p.get("soft_identity")),
                    "prior_failure": next(
                        (f.get("reason") for f in (row.get("fails") or []) if f.get("url") == p.get("url")),
                        "NO_PRICE",
                    ),
                }
            )
        rows.append(
            {
                "benchmark_id": bid,
                "manufacturer": item.get("manufacturer"),
                "mpn": item.get("mpn") or item.get("part_number"),
                "description": item.get("description"),
                "category": item.get("category"),
                "expected_pack": item.get("expected_pack") or 1,
                "expected_uom": item.get("expected_uom") or "EA",
                "condition_required": "NEW",
                "prior_status": row.get("status"),
                "prior_fails": (row.get("fails") or [])[:8],
                "pdps": pdp_rows,
            }
        )

    # Domain-first sort key
    pri = {d: i for i, d in enumerate(PRIORITY_DOMAINS)}

    def _sort_key(r: dict[str, Any]) -> tuple:
        doms = [p.get("domain") for p in (r.get("pdps") or [])]
        best = min((pri.get(d, 99) for d in doms), default=99)
        return (best, -(len(r.get("pdps") or [])), r.get("benchmark_id") or "")

    rows.sort(key=_sort_key)
    payload = {
        "name": "KNOWN_PDP_PRICE_CORPUS_V1",
        "build": BUILD,
        "frozen_at": now_utc().isoformat(),
        "baseline_honest_priced": HONEST_BASELINE,
        "products": len(rows),
        "pdp_urls": sum(len(r.get("pdps") or []) for r in rows),
        "domains": sorted(by_domain.keys()),
        "domain_counts": dict(sorted(by_domain.items(), key=lambda x: -x[1])),
        "items": rows,
    }
    _save(CORPUS, payload)
    return payload
