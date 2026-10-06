"""Prioritize same-100 opportunities for acquisition-cost research.

Build: 20261004-m3-acquisition-scale-v1
"""

from __future__ import annotations

import json
from typing import Any

from acquisition_scale.models import (
    PRIORITY_CURRENT_VALUE,
    PRIORITY_EXACT_PRIOR_LINE,
    PRIORITY_MANAGEABLE_BASKET,
    PRIORITY_OPEN_RESELLER,
    PRIORITY_OTHER,
    PRIORITY_RECURRING_BASKET,
    PRIORITY_STRONG_R1_R3,
)
from eligibility_and_recovery.spec_identity import enrich_identity_for_research
from evidence_breakthrough.corpus import load_identity_store
from m3_data_root import data_path
from revenue_evidence.models import (
    COMPARABLE_PRIOR_BASKET_VALUE,
    CURRENT_VALUE_EXPLICIT,
    EXACT_PRIOR_LINE_VALUE,
)


def count_researchable_pns(idents: list[dict[str, Any]]) -> int:
    n = 0
    for i in idents:
        if not isinstance(i, dict):
            continue
        e = enrich_identity_for_research(dict(i))
        pn = str(e.get("part_number") or e.get("catalog_number") or e.get("model") or "")
        tok = pn.split()[0] if pn else ""
        if tok and any(ch.isdigit() for ch in tok) and len(tok) >= 4:
            n += 1
    return n


def _load(name: str) -> dict[str, Any]:
    p = data_path(name)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def load_same_100() -> tuple[list[str], bool]:
    ck = _load("m3_evidence_exhaustion_v1_checkpoint.json")
    sample = ck.get("sample") or []
    if len(sample) >= 100:
        return [o["opportunity_id"] for o in sample[:100]], True
    rev = _load("m3_revenue_evidence_v1_store.json")
    by = rev.get("by_opportunity") or {}
    if by:
        return list(by.keys())[:100], len(by) >= 100
    return [], False


def prioritize_opportunities(oids: list[str]) -> list[dict[str, Any]]:
    """Order research targets — strong revenue first."""
    rev_store = _load("m3_revenue_evidence_v1_store.json")
    rev_by = rev_store.get("by_opportunity") or {}
    ee = _load("m3_evidence_exhaustion_v1_store.json")
    ee_by = ee.get("by_opportunity") or {}
    id_store = load_identity_store()
    packs = id_store.get("by_opportunity") or {}

    rows: list[dict[str, Any]] = []
    for oid in oids:
        rev = (rev_by.get(oid) or {}).get("revenue") or {}
        types = set(rev.get("evidence_types") or [])
        ch = rev.get("channel") or {}
        prior_cost = int((ee_by.get(oid) or {}).get("cost_lines") or 0)
        pack = packs.get(oid) or {}
        idents = [
            i
            for i in (pack.get("identities") or [])
            if isinstance(i, dict)
            and (
                i.get("confidence_grade") in {"A", "B", "C"}
                or i.get("part_number")
                or (i.get("raw_description") and len(str(i.get("raw_description"))) > 24)
            )
        ]
        n_lines = len(idents)
        n_pn = count_researchable_pns(idents)
        manageable = 1 <= n_lines <= 40

        if rev.get("has_strong_r1_r3") and n_pn > 0:
            pri = PRIORITY_STRONG_R1_R3
        elif CURRENT_VALUE_EXPLICIT in types and n_pn > 0:
            pri = PRIORITY_CURRENT_VALUE
        elif EXACT_PRIOR_LINE_VALUE in types and n_pn > 0:
            pri = PRIORITY_EXACT_PRIOR_LINE
        elif COMPARABLE_PRIOR_BASKET_VALUE in types and n_pn > 0:
            pri = PRIORITY_RECURRING_BASKET
        elif ch.get("open_reseller_channel") and n_pn > 0:
            pri = PRIORITY_OPEN_RESELLER
        elif rev.get("has_strong_r1_r3"):
            # Strong revenue but no extractable PN — behind PN-ready targets
            pri = PRIORITY_OPEN_RESELLER if ch.get("open_reseller_channel") else PRIORITY_MANAGEABLE_BASKET
        elif manageable and n_pn > 0:
            pri = PRIORITY_MANAGEABLE_BASKET
        else:
            pri = PRIORITY_OTHER

        rows.append(
            {
                "opportunity_id": oid,
                "priority": pri,
                "has_strong_revenue": bool(rev.get("has_strong_r1_r3")),
                "has_defensible_revenue": bool(rev.get("has_defensible_revenue")),
                "evidence_types": list(types),
                "open_reseller": bool(ch.get("open_reseller_channel")),
                "incumbent_advantage": bool(ch.get("incumbent_advantage")),
                "channel": ch,
                "prior_cost_lines": prior_cost,
                "n_identities": n_lines,
                "n_researchable_pns": n_pn,
                "manageable_basket": manageable,
                "routing_class": (ee_by.get(oid) or {}).get("routing_class"),
                "best_revenue": rev.get("best"),
            }
        )

    rows.sort(
        key=lambda r: (
            r["priority"],
            0 if r["n_researchable_pns"] > 0 else 1,  # PN-ready first
            0 if r["prior_cost_lines"] == 0 else 1,  # new cost first among same pri
            -r["n_researchable_pns"],
            r["n_identities"],
            r["opportunity_id"],
        )
    )
    return rows
