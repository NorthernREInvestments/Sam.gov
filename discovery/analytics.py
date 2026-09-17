"""Discovery analytics + quality sampling (deterministic, no AI)."""

from __future__ import annotations
from application_clock import now_utc, today_local

import hashlib
from datetime import date, datetime, timedelta, timezone
from typing import Any


def analyze_run_results(opportunities: list[dict[str, Any]]) -> dict[str, Any]:
    """Post-run analytics answering breadth/quality questions."""
    today = today_local()
    by_class: dict[str, int] = {}
    by_state: dict[str, int] = {}
    by_source: dict[str, int] = {}
    by_category: dict[str, int] = {}
    expired = 0
    runway_7 = 0
    runway_14 = 0
    known_value = 0
    with_docs = 0
    rejects = 0

    for o in opportunities:
        cls = o.get("product_classification") or o.get("classification") or "UNKNOWN"
        by_class[cls] = by_class.get(cls, 0) + 1
        st = o.get("state_code") or o.get("jurisdiction") or "UNK"
        by_state[str(st)] = by_state.get(str(st), 0) + 1
        src = o.get("preferred_source_id") or o.get("source_id") or "UNK"
        by_source[str(src)] = by_source.get(str(src), 0) + 1
        if o.get("obvious_blocker") or o.get("operator_status") == "REJECTED":
            rejects += 1
        if o.get("estimated_value_status") == "KNOWN" or o.get("estimated_value"):
            known_value += 1
        docs = o.get("document_links_json") or o.get("document_links") or []
        if docs:
            with_docs += 1
        # category hint from title keywords
        title = (o.get("title") or "").lower()
        for cat, keys in {
            "IT": ["server", "computer", "network", "laptop"],
            "VEHICLE": ["vehicle", "truck", "trailer"],
            "HVAC": ["hvac"],
            "FURNITURE": ["furniture"],
            "MEDICAL": ["medical"],
            "LAB": ["laboratory", "lab "],
        }.items():
            if any(k in title for k in keys):
                by_category[cat] = by_category.get(cat, 0) + 1
                break
        # runway
        dl = o.get("response_deadline") or o.get("deadline_raw")
        d = None
        if isinstance(dl, datetime):
            d = dl.date()
        elif isinstance(dl, str) and len(dl) >= 10:
            try:
                d = date.fromisoformat(dl[:10])
            except ValueError:
                d = None
        if d:
            days = (d - today).days
            if days < 0:
                expired += 1
            if days > 7:
                runway_7 += 1
            if days > 14:
                runway_14 += 1

    return {
        "raw_or_unique_count": len(opportunities),
        "by_classification": by_class,
        "expired": expired,
        "services": by_class.get("SERVICE", 0),
        "CORE_PRODUCT": by_class.get("CORE_PRODUCT", 0),
        "PRODUCT_PLUS_SERVICE": by_class.get("PRODUCT_PLUS_SERVICE", 0),
        "UNKNOWN": by_class.get("UNKNOWN", 0),
        "obvious_rejects": rejects,
        "research_candidates": by_class.get("CORE_PRODUCT", 0) + by_class.get("PRODUCT_PLUS_SERVICE", 0),
        "by_state_or_jurisdiction": by_state,
        "by_source": by_source,
        "by_product_category_hint": by_category,
        "runway_gt_7_days": runway_7,
        "runway_gt_14_days": runway_14,
        "known_estimated_value": known_value,
        "with_solicitation_documents": with_docs,
        "LIVE_API_REQUESTS": 0,
    }


def quality_sample(opportunities: list[dict[str, Any]], *, seed: str = "govtracker") -> dict[str, Any]:
    """Deterministic samples for operator false-positive review — no AI."""
    if not opportunities:
        return {"samples": {}, "LIVE_API_REQUESTS": 0}

    def _h(o: dict[str, Any]) -> int:
        key = f"{seed}:{o.get('id') or o.get('external_id') or o.get('title')}"
        return int(hashlib.sha256(key.encode()).hexdigest()[:8], 16)

    core = [o for o in opportunities if (o.get("product_classification") or o.get("classification")) == "CORE_PRODUCT"]
    pps = [o for o in opportunities if (o.get("product_classification") or o.get("classification")) == "PRODUCT_PLUS_SERVICE"]
    ranked = sorted(opportunities, key=lambda o: -(o.get("research_priority") or 0))

    def pick(pool: list, n: int = 3) -> list:
        if not pool:
            return []
        ordered = sorted(pool, key=_h)
        return ordered[:n]

    # Different jurisdictions / sources
    by_jur: dict[str, list] = {}
    for o in opportunities:
        j = str(o.get("jurisdiction") or o.get("state_code") or "UNK")
        by_jur.setdefault(j, []).append(o)
    jur_samples = []
    for j, pool in sorted(by_jur.items())[:5]:
        jur_samples.extend(pick(pool, 1))

    return {
        "samples": {
            "highest_research_priority": ranked[:5],
            "random_CORE_PRODUCT": pick(core, 3),
            "random_PRODUCT_PLUS_SERVICE": pick(pps, 3),
            "different_jurisdictions": jur_samples[:5],
            "different_sources": pick(
                list({(o.get("preferred_source_id") or o.get("source_id") or "UNK"): o for o in opportunities}.values()),
                5,
            ),
        },
        "selection": "deterministic_hash",
        "OpenAI": 0,
        "LIVE_API_REQUESTS": 0,
    }
