"""R3 cybersecurity / CMMC / DPAS — only when clauses actually present."""

from __future__ import annotations

import re
from typing import Any

from response_engine.models import new_id
from response_engine.r3_constants import (
    CYBER_NOT_APPLICABLE,
    CYBER_REQUIRED_NOT_MET,
    CYBER_REQUIRED_UNKNOWN,
    CYBER_REQUIRED_VERIFIED,
    CYBER_REVIEW_REQUIRED,
    UNKNOWN,
)

_CMMC = re.compile(r"\bCMMC\b|Cybersecurity\s+Maturity\s+Model", re.I)
_DFARS_7021 = re.compile(
    r"252\.204[-–]?7021|NIST\s*(?:SP)?\s*800[-–]?171|\bSPRS\b|"
    r"Controlled\s+Unclassified\s+Information|"
    r"cybersecurity\s+requirements?\s+for\s+contractors",
    re.I,
)
_DPAS = re.compile(r"\bDPAS\b|Defense\s+Priorities|rated\s+order|\bDO[\s\-]?[A-Z0-9]+|\bDX[\s\-]?[A-Z0-9]+", re.I)


def evaluate_cyber(
    *,
    solicitation_text: str | None = None,
    clauses: list[str] | None = None,
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    blob = " ".join([solicitation_text or "", " ".join(clauses or [])])
    profile = profile or {}
    has_cmmc = bool(_CMMC.search(blob))
    has_dfars = bool(_DFARS_7021.search(blob))
    # Never infer CMMC from DoD alone
    if not blob.strip():
        return {
            "kind": "CyberDecision",
            "decision_id": new_id("CYB"),
            "status": CYBER_REQUIRED_UNKNOWN,
            "plain": "No solicitation text — cyber applicability unknown",
            "clauses": [],
            "LIVE_API_REQUESTS": 0,
        }
    if not has_cmmc and not has_dfars:
        return {
            "kind": "CyberDecision",
            "decision_id": new_id("CYB"),
            "status": CYBER_NOT_APPLICABLE,
            "plain": "No CMMC/DFARS 252.204-7021/NIST 800-171 clause identified",
            "clauses": [],
            "required_level": None,
            "company_level": profile.get("cmmc_level") or UNKNOWN,
            "LIVE_API_REQUESTS": 0,
        }

    company_level = str(profile.get("cmmc_level") or "").upper()
    status = CYBER_REQUIRED_UNKNOWN
    if company_level in ("", "UNKNOWN"):
        status = CYBER_REQUIRED_UNKNOWN
        plain = "Cyber clause present — company CMMC/NIST evidence unknown"
    elif company_level in ("NONE", "NOT_MET", "0"):
        status = CYBER_REQUIRED_NOT_MET
        plain = "Cyber requirement present — company level not met"
    else:
        status = CYBER_REVIEW_REQUIRED
        plain = f"Cyber clause present — company level={company_level}; verify against required level"
        # Only VERIFIED if profile explicitly marks verified
        if profile.get("cmmc_verified") is True:
            status = CYBER_REQUIRED_VERIFIED
            plain = f"Cyber evidence verified at level={company_level}"

    return {
        "kind": "CyberDecision",
        "decision_id": new_id("CYB"),
        "status": status,
        "plain": plain,
        "clauses": [
            *(["CMMC"] if has_cmmc else []),
            *(["DFARS_252.204-7021_OR_NIST_800-171"] if has_dfars else []),
        ],
        "required_level": UNKNOWN,
        "company_level": company_level or UNKNOWN,
        "LIVE_API_REQUESTS": 0,
    }


def evaluate_dpas(*, solicitation_text: str | None = None) -> dict[str, Any]:
    blob = solicitation_text or ""
    m = _DPAS.search(blob)
    if not m:
        return {
            "kind": "DPASDecision",
            "applies": False,
            "status": "NOT_APPLICABLE",
            "plain": "No DPAS rating identified",
        }
    return {
        "kind": "DPASDecision",
        "applies": True,
        "status": CYBER_REVIEW_REQUIRED,
        "rating_span": m.group(0),
        "plain": "PRIORITY-RATED ORDER — REVIEW REQUIRED",
        "owner_review_required": True,
        "notes": "DPAS obligations detected; full legal automation not performed",
    }
