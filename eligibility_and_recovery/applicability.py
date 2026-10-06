"""Eligibility applicability audit — classify requirements by real bid impact.

Build: 20261004-m3-price-search-reliability-v1

Do not mark BID_ELIGIBLE_WITH_ACTION merely because a keyword appears.
"""

from __future__ import annotations

import json
import re
from typing import Any

from application_clock import now_utc
from eligibility_and_recovery.models import (
    BID_ELIGIBLE,
    BID_ELIGIBLE_WITH_ACTION,
    BID_INELIGIBLE,
    ELIGIBILITY_UNKNOWN,
)
from m3_data_root import data_path

BUILD = "20261004-m3-price-search-reliability-v1"

APPLICABILITY = (
    "MANDATORY_FOR_BID",
    "MANDATORY_POST_AWARD",
    "CONDITIONAL",
    "BOILERPLATE_REFERENCE",
    "INFORMATIONAL",
    "SUPERSEDED_BY_AMENDMENT",
    "NOT_APPLICABLE",
    "UNKNOWN_APPLICABILITY",
)

# Soft / non-binding language
_SOFT = re.compile(
    r"\b(?:may\s+be\s+required|if\s+required|if\s+applicable|as\s+applicable|"
    r"when\s+required|where\s+applicable|optional|not\s+required|"
    r"unless\s+(?:otherwise|required)|at\s+the\s+(?:option|discretion)\s+of|"
    r"buyer\s+reserves\s+the\s+right|may\s+require)\b",
    re.I,
)
_ONLY_APPLICABLE = re.compile(
    r"\bonly\s+applicable\s+to\b|\bdoes\s+not\s+apply\b|\bnot\s+applicable\b|"
    r"\bN/?A\b|\bexcept\s+(?:as|when|if)\b",
    re.I,
)
_POST_AWARD = re.compile(
    r"\b(?:after\s+award|upon\s+award|following\s+award|prior\s+to\s+(?:commencement|"
    r"performance|start\s+of\s+work)|before\s+commencement|within\s+\d+\s+days?\s+"
    r"(?:after|of)\s+award|performance\s+bond|payment\s+bond|"
    r"certificate\s+of\s+insurance\s+(?:prior\s+to|before|upon)\s+(?:award|commencement|"
    r"performance|work)|COI\s+(?:prior\s+to|before)\s+(?:award|commencement))\b",
    re.I,
)
_WITH_BID = re.compile(
    r"\b(?:must\s+accompany\s+(?:the\s+)?bid|accompany\s+(?:the\s+)?bid|"
    r"submit(?:ted)?\s+with\s+(?:the\s+)?bid|required\s+with\s+(?:the\s+)?bid|"
    r"at\s+time\s+of\s+bid|bid\s+opening|proposal\s+submission|"
    r"include\s+with\s+(?:your\s+)?(?:bid|proposal)|attach(?:ed)?\s+to\s+(?:the\s+)?bid)\b",
    re.I,
)
_MUST = re.compile(
    r"\b(?:shall|must|required\s+to|is\s+required|are\s+required|"
    r"bidder\s+shall|offeror\s+shall|contractor\s+shall)\b",
    re.I,
)
_REFERENCE = re.compile(
    r"\b(?:incorporated\s+by\s+reference|see\s+(?:clause|FAR|DFARS|section)|"
    r"as\s+set\s+forth\s+in|for\s+(?:definitions?|information)|"
    r"table\s+of\s+contents|index\s+of\s+clauses|list\s+of\s+clauses|"
    r"clause\s+matrix|full\s+text\s+available)\b",
    re.I,
)
_FORM_PACKET = re.compile(
    r"\b(?:Form\s+W-?9|Request\s+for\s+Taxpayer|"
    r"CERTIFICATION\s+OF\s+COMPLIANCE|"
    r"sample\s+form|example\s+form|blank\s+form)\b",
    re.I,
)
_BID_BOND = re.compile(
    r"\b(?:bid\s+bond|bid\s+guarantee|bid\s+security)\b",
    re.I,
)
_PERF_BOND = re.compile(r"\bperformance\s+bond\b", re.I)
_PAY_BOND = re.compile(r"\bpayment\s+bond\b", re.I)
_BOND_PCT = re.compile(
    r"(\d+(?:\.\d+)?)\s*%|\b(?:five|ten|twenty|one\s+hundred)\s+percent\b",
    re.I,
)
_MANDATORY_SITE = re.compile(
    r"\b(?:mandatory\s+site\s+visit|attendance\s+is\s+mandatory|"
    r"must\s+attend.{0,40}(?:site\s+visit|pre[- ]bid)|"
    r"failure\s+to\s+attend.{0,40}(?:disqualif|ineligib|reject))\b",
    re.I,
)
_OPTIONAL_SITE = re.compile(
    r"\b(?:optional\s+site\s+visit|recommended\s+site\s+visit|"
    r"site\s+visit\s+(?:is\s+)?optional|pre[- ]bid\s+(?:conference|meeting)\s+"
    r"(?:is\s+)?(?:optional|recommended))\b",
    re.I,
)
_BERRY_PRODUCT = re.compile(
    r"\b(?:food|clothing|fabric|tent|cotton|wool|silk|fiber|yarn|"
    r"hand\s+tool|measuring\s+tool|specialty\s+metal|stainless|"
    r"berry\s+amendment\s+applies|domestic\s+commodity)\b",
    re.I,
)
_SCHOOL_LUNCH_COND = re.compile(
    r"\b(?:national\s+school\s+lunch|only\s+applicable\s+to\s+contracts?\s+funded|"
    r"child\s+nutrition|USDA\s+funded)\b",
    re.I,
)
_COMMERCIAL_ITEM = re.compile(
    r"\b(?:commercial\s+item|commercial\s+product|FAR\s+Part\s+12|"
    r"commercially\s+available\s+off[- ]the[- ]shelf|COTS)\b",
    re.I,
)


def _ctx(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def classify_bonding(exact: str) -> dict[str, Any]:
    t = _ctx(exact)
    kind = "REFERENCE_ONLY"
    if _SOFT.search(t) and not _MUST.search(t) and not _WITH_BID.search(t):
        kind = "OPTIONAL"
    elif _BID_BOND.search(t) and (_WITH_BID.search(t) or (_MUST.search(t) and not _POST_AWARD.search(t))):
        kind = "BID_BOND_REQUIRED"
    elif _PERF_BOND.search(t) and (_MUST.search(t) or _POST_AWARD.search(t)):
        kind = "PERFORMANCE_BOND_REQUIRED"
    elif _PAY_BOND.search(t) and (_MUST.search(t) or _POST_AWARD.search(t)):
        kind = "PAYMENT_BOND_REQUIRED"
    elif _BID_BOND.search(t) and _SOFT.search(t):
        kind = "OPTIONAL"
    elif _BID_BOND.search(t) or _PERF_BOND.search(t) or _PAY_BOND.search(t):
        if _REFERENCE.search(t) or not _MUST.search(t):
            kind = "REFERENCE_ONLY"
        else:
            kind = "BID_BOND_REQUIRED" if _BID_BOND.search(t) else (
                "PERFORMANCE_BOND_REQUIRED" if _PERF_BOND.search(t) else "PAYMENT_BOND_REQUIRED"
            )
    else:
        kind = "NOT_APPLICABLE"

    pct_m = _BOND_PCT.search(t)
    pre_bid = kind == "BID_BOND_REQUIRED"
    applicability = (
        "MANDATORY_FOR_BID"
        if kind == "BID_BOND_REQUIRED"
        else "MANDATORY_POST_AWARD"
        if kind in {"PERFORMANCE_BOND_REQUIRED", "PAYMENT_BOND_REQUIRED"}
        else "CONDITIONAL"
        if kind == "OPTIONAL"
        else "BOILERPLATE_REFERENCE"
        if kind == "REFERENCE_ONLY"
        else "NOT_APPLICABLE"
    )
    return {
        "bonding_class": kind,
        "amount_or_pct": pct_m.group(0) if pct_m else None,
        "required_before_bid": pre_bid,
        "fatal_for_model": pre_bid,  # bid bond cash/credit is a real pre-bid action
        "applicability": applicability,
        "creates_action": applicability == "MANDATORY_FOR_BID",
    }


def classify_site_visit(exact: str) -> dict[str, Any]:
    t = _ctx(exact)
    if _MANDATORY_SITE.search(t):
        cls = "MANDATORY_PREBID"
    elif _OPTIONAL_SITE.search(t):
        cls = "OPTIONAL_PREBID"
    elif re.search(r"\brecommended\b", t, re.I) and re.search(r"\bsite\s+visit|pre[- ]bid\b", t, re.I):
        cls = "RECOMMENDED"
    elif _POST_AWARD.search(t):
        cls = "POST_AWARD_ONLY"
    elif _SOFT.search(t) or _REFERENCE.search(t):
        cls = "REFERENCE_ONLY"
    elif re.search(r"\b(?:site\s+visit|pre[- ]bid)\b", t, re.I) and _MUST.search(t):
        cls = "MANDATORY_PREBID"
    elif re.search(r"\b(?:site\s+visit|pre[- ]bid)\b", t, re.I):
        cls = "REFERENCE_ONLY"
    else:
        cls = "REFERENCE_ONLY"
    applicability = (
        "MANDATORY_FOR_BID"
        if cls == "MANDATORY_PREBID"
        else "CONDITIONAL"
        if cls in {"OPTIONAL_PREBID", "RECOMMENDED"}
        else "MANDATORY_POST_AWARD"
        if cls == "POST_AWARD_ONLY"
        else "BOILERPLATE_REFERENCE"
    )
    return {
        "site_visit_class": cls,
        "applicability": applicability,
        "creates_action": cls == "MANDATORY_PREBID",
    }


def classify_insurance(exact: str) -> dict[str, Any]:
    t = _ctx(exact)
    if _ONLY_APPLICABLE.search(t) and not _MUST.search(t):
        cls = "NOT_APPLICABLE"
    elif _WITH_BID.search(t) and re.search(r"\binsurance|COI|certificate\b", t, re.I):
        cls = "REQUIRED_WITH_BID"
    elif re.search(r"\bprior\s+to\s+award\b|\bbefore\s+award\b", t, re.I):
        cls = "REQUIRED_BEFORE_AWARD"
    elif _POST_AWARD.search(t) or re.search(
        r"\b(?:prior\s+to\s+(?:commencement|performance|start)|upon\s+request|"
        r"contractor\s+shall\s+maintain|shall\s+maintain\s+insurance)\b",
        t,
        re.I,
    ):
        cls = "REQUIRED_AFTER_AWARD"
    elif _SOFT.search(t) or _REFERENCE.search(t) or _FORM_PACKET.search(t):
        cls = "REFERENCE_ONLY"
    elif _MUST.search(t) and re.search(r"\binsurance|COI|certificate\b", t, re.I):
        # Default: insurance is almost always post-award for supply buys
        cls = "REQUIRED_AFTER_AWARD"
    else:
        cls = "REFERENCE_ONLY"
    applicability = {
        "REQUIRED_WITH_BID": "MANDATORY_FOR_BID",
        "REQUIRED_BEFORE_AWARD": "MANDATORY_FOR_BID",  # still an action before award decision
        "REQUIRED_AFTER_AWARD": "MANDATORY_POST_AWARD",
        "REFERENCE_ONLY": "BOILERPLATE_REFERENCE",
        "NOT_APPLICABLE": "NOT_APPLICABLE",
    }[cls]
    return {
        "insurance_class": cls,
        "applicability": applicability,
        # Only force WITH_ACTION when COI is needed with/before bid
        "creates_action": cls in {"REQUIRED_WITH_BID", "REQUIRED_BEFORE_AWARD"},
    }


def classify_generic_action(code: str, exact: str) -> dict[str, Any]:
    t = _ctx(exact)
    code_u = (code or "").upper()

    if _ONLY_APPLICABLE.search(t) or (_SCHOOL_LUNCH_COND.search(t) and "BUY_AMERICAN" in code_u):
        return {
            "applicability": "NOT_APPLICABLE",
            "creates_action": False,
            "reason": "conditional_or_not_applicable_language",
        }
    if _SOFT.search(t) and not _MUST.search(t) and not _WITH_BID.search(t):
        return {
            "applicability": "CONDITIONAL",
            "creates_action": False,
            "reason": "soft_may_be_required",
        }
    if _REFERENCE.search(t) and not _MUST.search(t):
        return {
            "applicability": "BOILERPLATE_REFERENCE",
            "creates_action": False,
            "reason": "clause_reference_only",
        }

    # W-9 / tax forms in packet — usually post-award / award admin
    if code_u in {"W9_SUBMISSION"} or _FORM_PACKET.search(t):
        if _WITH_BID.search(t):
            return {"applicability": "MANDATORY_FOR_BID", "creates_action": True, "reason": "required_with_bid"}
        return {
            "applicability": "MANDATORY_POST_AWARD",
            "creates_action": False,
            "reason": "form_packet_or_post_award_admin",
        }

    if code_u in {"VENDOR_REGISTRATION", "PORTAL_SETUP", "SAM_ACTIVE", "CAGE_REQUIRED"}:
        if _SOFT.search(t):
            return {"applicability": "CONDITIONAL", "creates_action": False, "reason": "soft"}
        # Registration is usually pre-bid actionable
        return {"applicability": "MANDATORY_FOR_BID", "creates_action": True, "reason": "registration_prebid"}

    if code_u in {"BUY_AMERICAN", "TAA", "FAR 52.225-1", "FAR 52.225-5"}:
        if _SCHOOL_LUNCH_COND.search(t) or _ONLY_APPLICABLE.search(t):
            return {"applicability": "NOT_APPLICABLE", "creates_action": False, "reason": "funding_condition"}
        if _COMMERCIAL_ITEM.search(t) and _SOFT.search(t):
            return {"applicability": "CONDITIONAL", "creates_action": False, "reason": "commercial_exception_possible"}
        if _MUST.search(t) or re.search(r"\bcertif(?:y|ication)\b", t, re.I):
            return {"applicability": "MANDATORY_FOR_BID", "creates_action": True, "reason": "domestic_cert_required"}
        return {"applicability": "CONDITIONAL", "creates_action": False, "reason": "domestic_mention_weak"}

    if code_u in {"LOCAL_REQUIREMENT", "PAST_PERFORMANCE_YEARS", "SAMPLES_REQUIRED"}:
        if _MUST.search(t) or _WITH_BID.search(t):
            return {"applicability": "MANDATORY_FOR_BID", "creates_action": True, "reason": "explicit_must"}
        return {"applicability": "CONDITIONAL", "creates_action": False, "reason": "weak_local_or_pp"}

    if code_u.startswith("FAR ") or code_u.startswith("DFARS ") or code_u == "CMMC":
        return classify_far_applicability(code_u, t)

    if _WITH_BID.search(t) and _MUST.search(t):
        return {"applicability": "MANDATORY_FOR_BID", "creates_action": True, "reason": "must_with_bid"}
    if _POST_AWARD.search(t):
        return {"applicability": "MANDATORY_POST_AWARD", "creates_action": False, "reason": "post_award"}
    if _MUST.search(t):
        return {"applicability": "MANDATORY_FOR_BID", "creates_action": True, "reason": "shall_must"}
    return {"applicability": "UNKNOWN_APPLICABILITY", "creates_action": False, "reason": "insufficient_context"}


def classify_far_applicability(clause: str, exact: str) -> dict[str, Any]:
    t = _ctx(exact)
    clause_u = (clause or "").upper()

    # Berry / DFARS 252.225-7012 — only blocker when product category actually covered
    if "252.225-7012" in clause_u or "BERRY" in clause_u:
        if _ONLY_APPLICABLE.search(t) or _SOFT.search(t):
            return {
                "applicability": "NOT_APPLICABLE",
                "creates_action": False,
                "far_status": "DOES_NOT_APPLY",
                "eligibility_blocker": False,
                "reason": "soft_or_not_applicable",
            }
        if _BERRY_PRODUCT.search(t) and (_MUST.search(t) or re.search(r"\bapplies\b", t, re.I)):
            return {
                "applicability": "MANDATORY_FOR_BID",
                "creates_action": False,
                "far_status": "BID_BLOCKER",
                "eligibility_blocker": True,
                "reason": "berry_product_category_applies",
            }
        # Mere clause listing without product tie → needs review, not kill
        if _REFERENCE.search(t) or not _BERRY_PRODUCT.search(t):
            return {
                "applicability": "BOILERPLATE_REFERENCE",
                "creates_action": False,
                "far_status": "NEEDS_REVIEW",
                "eligibility_blocker": False,
                "reason": "clause_present_without_product_applicability",
            }
        return {
            "applicability": "UNKNOWN_APPLICABILITY",
            "creates_action": False,
            "far_status": "NEEDS_REVIEW",
            "eligibility_blocker": False,
            "reason": "berry_unclear",
        }

    if "252.225-7009" in clause_u or "SPECIALTY METALS" in clause_u:
        if _BERRY_PRODUCT.search(t) or re.search(r"\bmetal|steel|alloy\b", t, re.I):
            return {
                "applicability": "MANDATORY_FOR_BID",
                "creates_action": True,
                "far_status": "ACTION_REQUIRED",
                "eligibility_blocker": False,
                "reason": "specialty_metals_product",
            }
        return {
            "applicability": "BOILERPLATE_REFERENCE",
            "creates_action": False,
            "far_status": "DOES_NOT_APPLY",
            "eligibility_blocker": False,
            "reason": "specialty_metals_no_product_tie",
        }

    # Socioeconomic set-asides remain blockers when company ineligible (handled upstream)
    if any(x in clause_u for x in ("52.219-27", "52.219-29", "52.219-30", "52.219-3", "SDVOSB", "WOSB", "HUBZONE", "EDWOSB")):
        return {
            "applicability": "MANDATORY_FOR_BID",
            "creates_action": False,
            "far_status": "BID_BLOCKER",
            "eligibility_blocker": True,
            "reason": "socioeconomic_set_aside",
        }

    if "52.219-6" in clause_u:
        return {
            "applicability": "MANDATORY_FOR_BID",
            "creates_action": False,
            "far_status": "APPLIES",
            "eligibility_blocker": False,
            "reason": "small_business_set_aside_company_ok",
        }

    if _REFERENCE.search(t) and not _MUST.search(t):
        return {
            "applicability": "BOILERPLATE_REFERENCE",
            "creates_action": False,
            "far_status": "DOES_NOT_APPLY",
            "eligibility_blocker": False,
            "reason": "ibr_reference",
        }

    if _POST_AWARD.search(t) or "52.204-21" in clause_u or "252.204-7012" in clause_u:
        return {
            "applicability": "MANDATORY_POST_AWARD",
            "creates_action": False,
            "far_status": "ACTION_REQUIRED",
            "eligibility_blocker": False,
            "reason": "cyber_or_post_award_execution",
        }

    return {
        "applicability": "CONDITIONAL",
        "creates_action": False,
        "far_status": "NEEDS_REVIEW",
        "eligibility_blocker": False,
        "reason": "far_needs_review",
    }


def audit_requirement(hit: dict[str, Any], *, kind: str = "action") -> dict[str, Any]:
    """Classify one fatal/action/FAR hit for real applicability."""
    code = str(hit.get("blocking_requirement") or hit.get("clause") or "")
    exact = str(hit.get("exact_requirement") or hit.get("plain_english") or "")
    code_u = code.upper()

    if kind == "far" or code_u.startswith("FAR ") or code_u.startswith("DFARS ") or code_u == "CMMC":
        detail = classify_far_applicability(code, exact)
    elif code_u == "BONDING" or "BOND" in code_u:
        detail = classify_bonding(exact)
    elif code_u == "SITE_VISIT_OR_PREBID" or "SITE" in code_u:
        detail = classify_site_visit(exact)
    elif code_u == "INSURANCE_COI" or "INSURANCE" in code_u:
        detail = classify_insurance(exact)
    elif code_u in {"BERRY_AMENDMENT"} or "252.225-7012" in code_u:
        detail = classify_far_applicability(code if "252" in code_u else "BERRY", exact)
    else:
        detail = classify_generic_action(code, exact)

    applies_pre = detail.get("applicability") == "MANDATORY_FOR_BID"
    applies_post = detail.get("applicability") == "MANDATORY_POST_AWARD"
    out = {
        **hit,
        "requirement_code": code,
        "surrounding_text": exact[:400],
        "applies_to_bidder": detail.get("applicability")
        not in {"NOT_APPLICABLE", "BOILERPLATE_REFERENCE", "INFORMATIONAL", "SUPERSEDED_BY_AMENDMENT"},
        "applies_pre_bid": applies_pre,
        "applies_post_award": applies_post,
        "product_service_specific": bool(_BERRY_PRODUCT.search(exact)),
        "threshold_dependent": bool(re.search(r"\bthreshold|\$\s*\d|simplified\s+acquisition\b", exact, re.I)),
        "amendment_status": "UNKNOWN",
        **detail,
    }
    return out


def recompute_opportunity(ev: dict[str, Any]) -> dict[str, Any]:
    """Recompute eligibility status from audited requirements."""
    before = str(ev.get("eligibility_status") or ELIGIBILITY_UNKNOWN)
    audited_fatal = [audit_requirement(h, kind="fatal") for h in (ev.get("fatal_blockers") or [])]
    audited_action = [audit_requirement(h, kind="action") for h in (ev.get("actionable_blockers") or [])]
    audited_far = [audit_requirement(h, kind="far") for h in (ev.get("far_dfars") or [])]

    hard_codes = {
        "SDVOSB_SET_ASIDE",
        "WOSB_SET_ASIDE",
        "EDWOSB_SET_ASIDE",
        "HUBZONE_SET_ASIDE",
        "8A_SET_ASIDE",
        "MANDATORY_VEHICLE",
        "FACILITY_CLEARANCE",
        "PERSONNEL_CLEARANCE",
        "CMMC_REQUIRED",
        "SOURCE_APPROVAL",
        "DEALER_AUTHORIZATION",
    }
    real_fatal = []
    for h in audited_fatal + audited_far:
        code = str(h.get("requirement_code") or h.get("clause") or "").upper()
        if h.get("eligibility_blocker") or h.get("far_status") == "BID_BLOCKER":
            real_fatal.append(h)
        elif code in hard_codes and h.get("applicability") == "MANDATORY_FOR_BID":
            real_fatal.append(h)
        elif code == "BERRY_AMENDMENT" and h.get("far_status") == "BID_BLOCKER":
            real_fatal.append(h)
    # Deduplicate
    seen: set[str] = set()
    fatal_u: list[dict[str, Any]] = []
    for h in real_fatal:
        k = str(h.get("requirement_code") or h.get("clause") or "")
        if k in seen:
            continue
        seen.add(k)
        fatal_u.append(h)

    real_actions = [
        h
        for h in audited_action
        if h.get("creates_action") and h.get("applicability") == "MANDATORY_FOR_BID"
    ]
    # Also FAR ACTION that is pre-bid
    for h in audited_far:
        if h.get("creates_action") and h.get("applicability") == "MANDATORY_FOR_BID":
            real_actions.append(h)

    seen_a: set[str] = set()
    actions_u: list[dict[str, Any]] = []
    for h in real_actions:
        k = str(h.get("requirement_code") or h.get("clause") or "")
        if k in seen_a:
            continue
        seen_a.add(k)
        actions_u.append(h)

    fc = ev.get("file_coverage") or {}
    if fatal_u:
        status = BID_INELIGIBLE
    elif int(fc.get("documents_total") or 0) == 0:
        status = ELIGIBILITY_UNKNOWN
    elif int(fc.get("documents_processed") or 0) == 0 and int(fc.get("documents_total") or 0) > 0:
        status = ELIGIBILITY_UNKNOWN
    elif actions_u:
        status = BID_ELIGIBLE_WITH_ACTION
    else:
        # Dropped false-positive actions → eligible
        status = BID_ELIGIBLE

    removed_actions = [
        h
        for h in audited_action
        if not h.get("creates_action") or h.get("applicability") != "MANDATORY_FOR_BID"
    ]
    far_corrections = [
        h
        for h in audited_far
        if str(h.get("status") or "") != str(h.get("far_status") or h.get("status") or "")
        or h.get("applicability") in {"NOT_APPLICABLE", "BOILERPLATE_REFERENCE"}
    ]

    primary = None
    if status == BID_INELIGIBLE and fatal_u:
        primary = fatal_u[0]
    elif status == BID_ELIGIBLE_WITH_ACTION and actions_u:
        primary = actions_u[0]

    return {
        **ev,
        "eligibility_status_before": before,
        "eligibility_status": status,
        "can_we_bid": {
            BID_ELIGIBLE: "YES",
            BID_ELIGIBLE_WITH_ACTION: "YES WITH ACTION",
            ELIGIBILITY_UNKNOWN: "UNKNOWN",
            BID_INELIGIBLE: "NO",
        }.get(status, "UNKNOWN"),
        "why": (primary or {}).get("requirement_code") or (primary or {}).get("blocking_requirement"),
        "blocking_requirement": (primary or {}).get("requirement_code")
        or (primary or {}).get("blocking_requirement"),
        "required_action": (primary or {}).get("required_action")
        or ("Complete pre-bid action" if status == BID_ELIGIBLE_WITH_ACTION else None),
        "proceed_to_research": status in {BID_ELIGIBLE, BID_ELIGIBLE_WITH_ACTION},
        "audited_fatal_blockers": fatal_u,
        "audited_actionable_blockers": actions_u,
        "audited_far_dfars": audited_far,
        "false_positive_actions_removed": removed_actions,
        "far_applicability_corrections": far_corrections,
        "applicability_build": BUILD,
        "audited_at": now_utc().isoformat(),
    }


def run_applicability_audit(
    mine_store: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Audit all mined opportunities; persist audited store + before/after summary."""
    if mine_store is None:
        path = data_path("m3_eligibility_file_mine_store.json")
        mine_store = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    by = mine_store.get("by_opportunity") or {}
    before_counts = {
        BID_ELIGIBLE: 0,
        BID_ELIGIBLE_WITH_ACTION: 0,
        ELIGIBILITY_UNKNOWN: 0,
        BID_INELIGIBLE: 0,
    }
    after_counts = dict(before_counts)
    fp_actions = 0
    fp_bonding = 0
    fp_insurance = 0
    fp_site = 0
    far_corr = 0
    audited_by: dict[str, Any] = {}

    for oid, ev in by.items():
        if not isinstance(ev, dict):
            continue
        st = str(ev.get("eligibility_status") or ELIGIBILITY_UNKNOWN)
        if st in before_counts:
            before_counts[st] += 1
        audited = recompute_opportunity(ev)
        audited_by[oid] = audited
        ast = audited["eligibility_status"]
        if ast in after_counts:
            after_counts[ast] += 1
        removed = audited.get("false_positive_actions_removed") or []
        fp_actions += len(removed)
        for h in removed:
            code = str(h.get("requirement_code") or "").upper()
            if code == "BONDING" or "BOND" in code:
                fp_bonding += 1
            elif code == "INSURANCE_COI" or "INSURANCE" in code:
                fp_insurance += 1
            elif "SITE" in code:
                fp_site += 1
        far_corr += len(audited.get("far_applicability_corrections") or [])

    store = {
        "kind": "EligibilityApplicabilityAuditStore",
        "build": BUILD,
        "by_opportunity": audited_by,
        "before": before_counts,
        "after": after_counts,
        "false_positive_actions_removed": fp_actions,
        "bonding_false_positives": fp_bonding,
        "insurance_false_positives": fp_insurance,
        "site_visit_false_positives": fp_site,
        "far_dfars_applicability_corrections": far_corr,
        "updated_at": now_utc().isoformat(),
    }
    out_path = data_path("m3_eligibility_applicability_audit_store.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(store, indent=2, default=str), encoding="utf-8")

    # Also refresh mine store statuses so downstream uses audited gate
    refreshed = dict(mine_store)
    refreshed["by_opportunity"] = audited_by
    refreshed["applicability_audit"] = {
        "before": before_counts,
        "after": after_counts,
        "build": BUILD,
    }
    refreshed["summary"] = {
        **(mine_store.get("summary") or {}),
        **after_counts,
        "applicability_before": before_counts,
        "false_positive_actions_removed": fp_actions,
    }
    refreshed["build"] = BUILD
    data_path("m3_eligibility_file_mine_store.json").write_text(
        json.dumps(refreshed, indent=2, default=str), encoding="utf-8"
    )
    return store
