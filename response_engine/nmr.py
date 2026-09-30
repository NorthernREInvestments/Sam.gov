"""R3 Nonmanufacturer Rule engine — fail-closed, never boolean-only."""

from __future__ import annotations

import re
from typing import Any

from response_engine.models import new_id
from response_engine.r3_constants import (
    NMR_APPLIES_COMPLIANT,
    NMR_APPLIES_NONCOMPLIANT,
    NMR_APPLIES_WAIVER,
    NMR_NOT_APPLICABLE,
    NMR_REVIEW_REQUIRED,
    NMR_UNKNOWN,
)

_UNRESTRICTED = re.compile(
    r"unrestricted|full[\s\-]?and[\s\-]?open|none|no\s+set[\s\-]?aside|not\s+set[\s\-]?aside",
    re.I,
)
_SB_SETASIDE = re.compile(
    r"small\s+business|total\s+small|partial\s+small|sba|8\(a\)|hubzone|wosb|edwosb|sdvosb|vosb|set[\s\-]?aside",
    re.I,
)


def _is_unrestricted(set_aside: str | None) -> bool:
    if set_aside is None or str(set_aside).strip() == "":
        return False
    s = str(set_aside).strip()
    if _UNRESTRICTED.search(s) and not _SB_SETASIDE.search(s):
        return True
    up = s.upper()
    return up in {"UNRESTRICTED", "NONE", "FULL_AND_OPEN", "N/A", "NA", "NOSBA"}


def _is_set_aside(set_aside: str | None) -> bool | None:
    if set_aside is None or str(set_aside).strip() == "":
        return None
    if _is_unrestricted(set_aside):
        return False
    if _SB_SETASIDE.search(str(set_aside)):
        return True
    return None


def evaluate_nmr(
    *,
    set_aside: str | None = None,
    naics: str | None = None,
    procurement_type: str | None = None,  # supply | service | unknown
    manufacturer_status: str | None = None,  # manufacturer | nonmanufacturer | unknown
    employee_count: Any = None,
    normally_sells_item_type: bool | None = None,
    takes_ownership_or_possession: bool | None = None,
    us_small_manufacturer_requirement: bool | None = None,
    waiver_status: str | None = None,  # class | individual | none | unknown
    individual_waiver: dict[str, Any] | None = None,
    class_waiver: dict[str, Any] | None = None,
    multi_item: bool = False,
    item_groups: list[dict[str, Any]] | None = None,
    it_var: bool | None = None,
    evidence: dict[str, Any] | None = None,
    owner_review_forced: bool = False,
) -> dict[str, Any]:
    """
    NMRDecision — never reduces to boolean.
    Does not invent waiver or manufacturer size.
    """
    ev = evidence or {}
    sa = set_aside if set_aside is not None else ev.get("set_aside")
    set_aside_flag = _is_set_aside(sa)
    proc = (procurement_type or ev.get("procurement_type") or "unknown").lower()
    mfr = (manufacturer_status or ev.get("manufacturer_status") or "unknown").lower()
    waiver = (waiver_status or ev.get("waiver_status") or "unknown").lower()
    if class_waiver and class_waiver.get("applicable") is True:
        waiver = "class"
    if individual_waiver and individual_waiver.get("applicable") is True:
        waiver = "individual"

    decision = {
        "kind": "NMRDecision",
        "decision_id": new_id("NMR"),
        "applies": None,
        "set_aside_type": sa,
        "NAICS": naics or ev.get("naics"),
        "procurement_type": proc,
        "supply_service_determination": proc,
        "manufacturer_status": mfr,
        "nonmanufacturer_status": "yes" if mfr == "nonmanufacturer" else ("no" if mfr == "manufacturer" else "unknown"),
        "employee_limit": ev.get("employee_limit") or "500_or_size_standard_unknown",
        "employee_count": employee_count if employee_count is not None else ev.get("employee_count"),
        "normally_sells_item_type": normally_sells_item_type
        if normally_sells_item_type is not None
        else ev.get("normally_sells_item_type"),
        "takes_ownership_or_possession": takes_ownership_or_possession
        if takes_ownership_or_possession is not None
        else ev.get("takes_ownership_or_possession"),
        "U.S._small_business_manufacturer_requirement": us_small_manufacturer_requirement
        if us_small_manufacturer_requirement is not None
        else ev.get("us_small_manufacturer_requirement"),
        "waiver_status": waiver,
        "individual_waiver": individual_waiver,
        "class_waiver": class_waiver,
        "multi_item_rule": multi_item or bool(item_groups),
        "item_groups": item_groups or [],
        "IT_VAR_rule": bool(it_var) if it_var is not None else False,
        "IT_VAR_claimed_without_evidence": bool(it_var) and not ev.get("it_var_evidence"),
        "evidence": ev,
        "decision_status": NMR_UNKNOWN,
        "owner_review_required": False,
        "governing_sources": [
            "13 CFR 121.406",
            "FAR 19.505 / 19.102",
            "solicitation set-aside + NAICS control",
        ],
        "rationale": "",
        "LIVE_API_REQUESTS": 0,
    }

    # Services: NMR generally N/A for pure services
    if proc == "service":
        decision["applies"] = False
        decision["decision_status"] = NMR_NOT_APPLICABLE
        decision["rationale"] = "procurement classified as service — NMR not applied"
        return decision

    # Unrestricted supply
    if set_aside_flag is False:
        decision["applies"] = False
        decision["decision_status"] = NMR_NOT_APPLICABLE
        decision["rationale"] = "unrestricted / no set-aside — NMR not applicable"
        return decision

    # Unknown set-aside
    if set_aside_flag is None:
        decision["applies"] = None
        decision["decision_status"] = NMR_UNKNOWN
        decision["owner_review_required"] = True
        decision["rationale"] = "set-aside status unknown — cannot determine NMR applicability"
        return decision

    # Set-aside present → NMR possibly applies for supply
    decision["applies"] = True

    if owner_review_forced:
        decision["decision_status"] = NMR_REVIEW_REQUIRED
        decision["owner_review_required"] = True
        decision["rationale"] = "owner review forced"
        return decision

    # IT VAR — only if explicitly evidenced, never because product is IT
    if it_var is True and ev.get("it_var_evidence"):
        decision["decision_status"] = NMR_REVIEW_REQUIRED
        decision["owner_review_required"] = True
        decision["rationale"] = "IT VAR treatment claimed with evidence — owner/legal review required"
        return decision
    if it_var is True and not ev.get("it_var_evidence"):
        decision["IT_VAR_rule"] = False
        decision["IT_VAR_claimed_without_evidence"] = True

    # Waiver present with source
    if waiver in ("class", "individual"):
        wsrc = (class_waiver or individual_waiver or {}).get("source")
        if wsrc:
            decision["decision_status"] = NMR_APPLIES_WAIVER
            decision["rationale"] = f"{waiver} waiver present — source={wsrc}"
            return decision
        decision["decision_status"] = NMR_REVIEW_REQUIRED
        decision["owner_review_required"] = True
        decision["rationale"] = f"{waiver} waiver claimed without SBA/source evidence"
        return decision

    if waiver == "none":
        # No waiver — need manufacturer compliance facts
        if mfr == "manufacturer":
            # Still need size/ownership facts for compliance claim
            if decision["normally_sells_item_type"] is True and decision["takes_ownership_or_possession"] is True:
                decision["decision_status"] = NMR_REVIEW_REQUIRED
                decision["owner_review_required"] = True
                decision["rationale"] = "manufacturer claim — size/domestic manufacturer evidence still required"
                return decision
            decision["decision_status"] = NMR_REVIEW_REQUIRED
            decision["owner_review_required"] = True
            decision["rationale"] = "offeror claims manufacturer — supporting evidence incomplete"
            return decision

        if mfr == "nonmanufacturer":
            # Nonmanufacturer without waiver → typically noncompliant unless all NMR conditions verified
            missing = []
            if decision["employee_count"] in (None, "UNKNOWN", "unknown"):
                missing.append("employee_count")
            if decision["normally_sells_item_type"] is None:
                missing.append("normally_sells_item_type")
            if decision["takes_ownership_or_possession"] is None:
                missing.append("takes_ownership_or_possession")
            if decision["U.S._small_business_manufacturer_requirement"] is not False:
                # Must supply from US small manufacturer unless waived — unknown manufacturer fails closed
                if not ev.get("us_small_manufacturer_verified"):
                    missing.append("us_small_manufacturer_verified")
            if missing:
                decision["decision_status"] = NMR_APPLIES_NONCOMPLIANT
                decision["owner_review_required"] = True
                decision["rationale"] = (
                    "nonmanufacturer on set-aside with no waiver; missing verified facts: "
                    + ", ".join(missing)
                )
                return decision
            decision["decision_status"] = NMR_APPLIES_COMPLIANT
            decision["rationale"] = "nonmanufacturer conditions verified with evidence (no waiver needed path)"
            return decision

        # Unknown manufacturer status
        decision["decision_status"] = NMR_UNKNOWN
        decision["owner_review_required"] = True
        decision["rationale"] = "set-aside supply; manufacturer vs nonmanufacturer unknown; no waiver"
        return decision

    # waiver unknown
    decision["decision_status"] = NMR_REVIEW_REQUIRED
    decision["owner_review_required"] = True
    decision["rationale"] = "set-aside supply procurement — waiver and manufacturer status need review"
    return decision


def nmr_from_deep_deal_compat(**kwargs: Any) -> dict[str, Any]:
    """Bridge legacy deep_deal_compliance.evaluate_nonmanufacturer_rule into R3 states."""
    from deep_deal_compliance import evaluate_nonmanufacturer_rule
    from deep_deal_constants import NMR_HARD, NMR_NEEDS, NMR_PASS, NMR_POTENTIAL

    legacy = evaluate_nonmanufacturer_rule(**kwargs)
    status = legacy.get("nmr_status")
    map_st = {
        NMR_PASS: NMR_NOT_APPLICABLE,
        NMR_HARD: NMR_APPLIES_NONCOMPLIANT,
        NMR_POTENTIAL: NMR_REVIEW_REQUIRED,
        NMR_NEEDS: NMR_UNKNOWN,
    }.get(status, NMR_UNKNOWN)
    decision = evaluate_nmr(
        set_aside=legacy.get("set_aside_status"),
        waiver_status="unknown" if legacy.get("waiver_visible") is None else ("class" if legacy.get("waiver_visible") else "none"),
        us_small_manufacturer_requirement=legacy.get("domestic_manufacturer_requirement"),
        evidence={"legacy": legacy, **(kwargs.get("evidence") or {})},
    )
    # Prefer R3 engine; annotate legacy
    decision["legacy_nmr_status"] = status
    decision["legacy_mapped"] = map_st
    return decision
