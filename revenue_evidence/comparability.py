"""Comparability scoring for prior awards vs current opportunity.

Build: 20261004-m3-revenue-evidence-v1
"""

from __future__ import annotations

import re
from typing import Any

from revenue_evidence.buyer import normalize_title, title_without_buyer_prefix
from revenue_evidence.models import (
    EXACT_MATCH,
    NOT_COMPARABLE,
    STRONG_COMPARABLE,
    WEAK_COMPARABLE,
)


def _norm_pn(s: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def score_comparability(
    *,
    buyer_match: bool,
    current: dict[str, Any],
    prior: dict[str, Any],
    match_grade: str | None = None,
) -> dict[str, Any]:
    """Return comparability class + numeric score components."""
    score = 0
    reasons: list[str] = []

    if buyer_match:
        score += 25
        reasons.append("buyer_match")
    else:
        return {
            "comparability": NOT_COMPARABLE,
            "score": 0,
            "reasons": ["buyer_mismatch"],
        }

    grade = (match_grade or "").upper()
    if grade in {"EXACT_PN", "EXACT_PN_TOKEN", "EXACT_MODEL", "EXACT_MODEL_AS_PN"}:
        score += 40
        reasons.append(f"grade:{grade}")
    elif grade in {"MFR_MODEL_IN_DESC", "EXACT_MODEL_TOKEN"}:
        score += 30
        reasons.append(f"grade:{grade}")
    elif grade in {"SAME_SOLICITATION"}:
        score += 35
        reasons.append("same_solicitation")
    elif grade in {"SAME_TITLE"}:
        score += 20
        reasons.append("same_title")
    elif grade in {"DESC_OVERLAP"}:
        score += 10
        reasons.append("desc_overlap")

    cur_pn = _norm_pn(current.get("part_number") or current.get("model"))
    prior_pn = _norm_pn(prior.get("part_number") or prior.get("model"))
    if cur_pn and prior_pn and cur_pn == prior_pn:
        score += 15
        reasons.append("exact_mpn_overlap")

    cur_title = normalize_title(current.get("title") or current.get("project_title"))
    prior_title = normalize_title(prior.get("project_title") or prior.get("title"))
    if cur_title and prior_title:
        ct = title_without_buyer_prefix(cur_title, current.get("buyer_code"))
        pt = title_without_buyer_prefix(prior_title, prior.get("government_code"))
        if ct and pt and (ct == pt or ct in pt or pt in ct):
            score += 15
            reasons.append("title_pattern")

    # Quantity / UOM
    cq = current.get("quantity")
    pq = prior.get("quantity")
    try:
        if cq is not None and pq is not None and float(cq) > 0 and float(pq) > 0:
            ratio = min(float(cq), float(pq)) / max(float(cq), float(pq))
            if ratio >= 0.5:
                score += 8
                reasons.append("qty_similar")
    except (TypeError, ValueError):
        pass

    cu = str(current.get("uom") or "").upper()
    pu = str(prior.get("uom") or "").upper()
    if cu and pu and cu == pu:
        score += 5
        reasons.append("uom_match")

    if score >= 70 or grade in {"EXACT_PN", "EXACT_PN_TOKEN"}:
        cls = EXACT_MATCH
    elif score >= 45:
        cls = STRONG_COMPARABLE
    elif score >= 25:
        cls = WEAK_COMPARABLE
    else:
        cls = NOT_COMPARABLE

    return {"comparability": cls, "score": score, "reasons": reasons}
