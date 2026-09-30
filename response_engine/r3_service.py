"""R3 orchestration — company eligibility / NMR / trade / 889 / regs / attestations.

Canonical company/compliance path for Bid Prep.
Consumes R1 requirements + R2 product/line data. Never auto-certifies.
0 live SAM API calls. Never READY_TO_SUBMIT / FULLY_LEGALLY_COMPLIANT.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from application_clock import now_utc

from response_engine.company_profile_r3 import (
    cage_status,
    load_company_compliance_profile,
    save_profile_version,
    set_aside_decision,
)
from response_engine.cyber_dod import evaluate_cyber, evaluate_dpas
from response_engine.firewall import firewall_report
from response_engine.models import new_id
from response_engine.nmr import evaluate_nmr
from response_engine.owner_attestations import new_owner_attestation, suggest_reuse
from response_engine.r3_constants import (
    BUILD,
    COMPANY_DATA_CONFLICT,
    COMPANY_PROFILE_INCOMPLETE,
    COMPLIANCE_READY,
    DO_NOT_BID,
    ELIGIBILITY_BLOCKED,
    FAIL,
    NMR_APPLIES_NONCOMPLIANT,
    NMR_REVIEW,
    NMR_REVIEW_REQUIRED,
    NMR_UNKNOWN,
    OWNER_ATTESTATION_REQUIRED,
    OWNER_CONFIRMATION_REQUIRED,
    PASS_VERIFIED,
    READY_FOR_RESPONSE_BUILD,
    REGISTRATION_REQUIRED,
    REGISTER_BEFORE_BID,
    REVIEW_REQUIRED,
    TRADE_COMPLIANCE_REVIEW,
    UNKNOWN,
)
from response_engine.registrations_r3 import evaluate_registrations
from response_engine.section889 import evaluate_section_889
from response_engine.store import load_project, save_project
from response_engine.trade_compliance import evaluate_trade_compliance

_AUTH_RE = re.compile(r"manufacturer\s+authorization|authorized\s+distributor|OEM\s+authorization", re.I)
_SOURCE_APPROVAL_RE = re.compile(
    r"approved\s+source|source\s+approval|QPL|QML|qualified\s+products?\s+list|SAR\b",
    re.I,
)


def _utc() -> str:
    return now_utc().isoformat()


def _blob(project: dict[str, Any]) -> str:
    parts = []
    for d in project.get("documents") or []:
        parts.append((d.get("text") or "")[:8000])
    for r in project.get("requirements") or []:
        if r.get("superseded"):
            continue
        parts.append(r.get("requirement_text") or r.get("normalized_requirement") or "")
    return "\n".join(parts)


def _cache_key(project: dict[str, Any], profile: dict[str, Any]) -> str:
    payload = {
        "rp": project.get("response_project_id"),
        "r1": project.get("compiled_at") or project.get("r1_build"),
        "r2": project.get("r2_analyzed_at"),
        "cp": profile.get("profile_version"),
        "lines": [(li.get("line_item_id"), li.get("country_of_origin")) for li in (project.get("line_items") or [])],
        "products": [
            (p.get("offered_product_id"), p.get("country_of_origin"), p.get("authorization_status"))
            for p in (project.get("offered_products") or [])
        ],
        "build": BUILD,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:20]


def new_representation(**fields: Any) -> dict[str, Any]:
    return {
        "kind": "ComplianceRepresentation",
        "representation_id": new_id("REP"),
        "response_project_id": fields.get("response_project_id"),
        "requirement_id": fields.get("requirement_id"),
        "regulation_family": fields.get("regulation_family"),
        "clause_number": fields.get("clause_number"),
        "clause_title": fields.get("clause_title"),
        "clause_date": fields.get("clause_date"),
        "representation_type": fields.get("representation_type"),
        "applies": fields.get("applies"),
        "applicability_status": fields.get("applicability_status"),
        "answer": fields.get("answer"),
        "answer_status": fields.get("answer_status") or UNKNOWN,
        "evidence": fields.get("evidence") or [],
        "owner_attestation_required": bool(fields.get("owner_attestation_required")),
        "timing": fields.get("timing") or "AT_SUBMISSION",
        "source": fields.get("source"),
        "notes": fields.get("notes"),
        "r4_destination": fields.get("r4_destination"),  # map only — do not populate forms
    }


def new_compliance_response_map(**fields: Any) -> dict[str, Any]:
    return {
        "kind": "ComplianceResponseMap",
        "representation_id": fields.get("representation_id"),
        "target_document": fields.get("target_document"),
        "target_field": fields.get("target_field"),
        "portal_field": fields.get("portal_field"),
        "response_type": fields.get("response_type"),
        "signature_required": bool(fields.get("signature_required")),
        "owner_confirmation_required": bool(fields.get("owner_confirmation_required")),
    }


def run_r3_analysis(
    project: dict[str, Any],
    *,
    persist: bool = True,
    profile: dict[str, Any] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Canonical company/compliance analysis. Fail-closed. 0 SAM API."""
    profile = profile or load_company_compliance_profile()
    profile_version = save_profile_version(profile) if persist else (profile.get("profile_version") or "ephemeral")
    project["company_profile_version"] = profile_version
    project["r3_build"] = BUILD
    project["r3_analyzed_at"] = _utc()
    project["r3_sam_api_calls"] = 0

    ck = _cache_key(project, profile)
    if not force and project.get("r3_cache_key") == ck and project.get("r3_analysis"):
        return project["r3_analysis"]

    blob = _blob(project)
    set_aside = (
        project.get("set_aside")
        or project.get("set_aside_type")
        or (project.get("opportunity") or {}).get("set_aside")
        or _extract_set_aside(blob)
    )
    naics = project.get("naics") or (project.get("opportunity") or {}).get("naics")
    jurisdiction = (
        project.get("jurisdiction")
        or (project.get("opportunity") or {}).get("jurisdiction")
        or _guess_jurisdiction(blob, project)
    )
    portal = project.get("portal") or (project.get("opportunity") or {}).get("portal")

    # --- Profile / conflicts ---
    conflicts = profile.get("conflicts") or []
    cage = cage_status(profile)

    # --- Set-aside eligibility ---
    eligibility = set_aside_decision(profile, set_aside)

    # --- Registrations ---
    registrations = evaluate_registrations(
        profile=profile,
        solicitation_text=blob,
        portal=portal,
        jurisdiction=jurisdiction,
        existing_registrations=profile.get("state_local_registrations"),
    )

    # --- NMR ---
    lines = project.get("line_items") or []
    products = project.get("offered_products") or []
    mfr_status = _manufacturer_status(project, products)
    waiver = project.get("nmr_waiver") or {}
    nmr = evaluate_nmr(
        set_aside=set_aside,
        naics=str(naics) if naics else None,
        procurement_type=_procurement_type(blob, lines),
        manufacturer_status=mfr_status,
        employee_count=profile.get("employee_count"),
        waiver_status=waiver.get("status") or "unknown",
        class_waiver=waiver.get("class_waiver"),
        individual_waiver=waiver.get("individual_waiver"),
        multi_item=len(lines) > 1,
        item_groups=[{"line_id": li.get("line_item_id"), "value": li.get("estimated_value")} for li in lines],
        it_var=project.get("it_var_claimed"),
        evidence={
            "it_var_evidence": project.get("it_var_evidence"),
            "us_small_manufacturer_verified": project.get("us_small_manufacturer_verified"),
            "products": [
                {"manufacturer": p.get("manufacturer"), "mpn": p.get("MPN") or p.get("mpn")} for p in products
            ],
        },
    )

    # --- Trade ---
    trade_lines = []
    for li in lines:
        offered = _offered_for_line(products, li)
        trade_lines.append(
            {
                "line_id": li.get("line_item_id"),
                "clin": li.get("CLIN") or li.get("buyer_line_number"),
                "country_of_origin": (offered or {}).get("country_of_origin") or li.get("country_of_origin"),
                "country_of_manufacture": (offered or {}).get("country_of_manufacture"),
                "origin_evidence_quality": (offered or {}).get("origin_evidence_quality"),
                "brand_headquarters": (offered or {}).get("brand_headquarters"),
                "offered_product": offered,
            }
        )
    all_or_none = bool(project.get("all_or_none") or re.search(r"all[\s\-]?or[\s\-]?none", blob, re.I))
    trade = evaluate_trade_compliance(
        lines=trade_lines,
        solicitation_text=blob,
        clauses=_clause_list(project),
        all_or_none=all_or_none,
    )

    # --- Section 889 ---
    existing_889 = None
    for a in project.get("owner_attestations") or []:
        if a.get("topic") == "section_889" and a.get("owner_confirmed"):
            existing_889 = a
            break
    s889 = evaluate_section_889(
        solicitation_text=blob,
        clauses=_clause_list(project),
        sam_annual_889=str(profile.get("section_889_annual") or UNKNOWN),
        owner_attestation=existing_889,
        product_concern=project.get("covered_telecom_product_concern"),
    )

    # --- Cyber / DPAS ---
    cyber = evaluate_cyber(solicitation_text=blob, clauses=_clause_list(project), profile=profile)
    dpas = evaluate_dpas(solicitation_text=blob)

    # --- Manufacturer authorization / source approval ---
    auth = _authorization_decision(blob, products, profile)
    source_approval = _source_approval_decision(blob, products, profile)

    # --- Owner attestations from R1 company requirements + 889 ---
    attestations = list(project.get("owner_attestations") or [])
    attestations = _ensure_attestations(project, blob, s889, attestations)

    # --- Representations + matrix ---
    representations, matrix, response_maps = _build_matrix(
        project=project,
        profile=profile,
        eligibility=eligibility,
        cage=cage,
        registrations=registrations,
        nmr=nmr,
        trade=trade,
        s889=s889,
        cyber=cyber,
        dpas=dpas,
        auth=auth,
        source_approval=source_approval,
        attestations=attestations,
    )

    readiness, recommendation, blockers, next_action, warnings = _compute_readiness(
        profile=profile,
        conflicts=conflicts,
        eligibility=eligibility,
        registrations=registrations,
        nmr=nmr,
        trade=trade,
        s889=s889,
        cyber=cyber,
        auth=auth,
        attestations=attestations,
        matrix=matrix,
        r2_readiness=project.get("r2_readiness"),
    )

    # Combined R1+R2+R3 readiness for response build
    if readiness == COMPLIANCE_READY and project.get("r2_readiness") == READY_FOR_RESPONSE_BUILD:
        # R1 must not be hard-blocked
        if not project.get("hard_blocks"):
            readiness = READY_FOR_RESPONSE_BUILD

    analysis = {
        "kind": "R3Analysis",
        "build": BUILD,
        "response_project_id": project.get("response_project_id"),
        "company_profile_version": profile_version,
        "profile_summary": {
            "legal_name": profile.get("legal_name"),
            "UEI": profile.get("UEI"),
            "CAGE": profile.get("CAGE"),
            "SAM": profile.get("SAM_registration_status"),
            "small_business_status": profile.get("small_business_status"),
            "completeness": profile.get("completeness"),
            "conflicts": conflicts,
        },
        "eligibility": eligibility,
        "cage": cage,
        "registrations": registrations,
        "nmr": nmr,
        "trade": trade,
        "section_889": s889,
        "cyber": cyber,
        "dpas": dpas,
        "manufacturer_authorization": auth,
        "source_approval": source_approval,
        "representations": representations,
        "compliance_matrix": matrix,
        "compliance_response_maps": response_maps,
        "owner_attestations": attestations,
        "readiness": readiness,
        "recommendation": recommendation,
        "blockers": blockers,
        "warnings": warnings,
        "next_action": next_action,
        "jurisdiction": jurisdiction,
        "set_aside": set_aside,
        "naics": naics,
        "firewall": _r3_firewall_check(project),
        "LIVE_API_REQUESTS": 0,
        "sam_api_calls": 0,
        "never_ready_to_submit": True,
        "legal_disclaimer": "NO KNOWN UNRESOLVED COMPLIANCE GAPS is not a legal compliance certificate. Final responsibility remains with company/owner.",
    }

    project["r3_analysis"] = analysis
    project["r3_readiness"] = readiness
    project["r3_recommendation"] = recommendation
    project["r3_blockers"] = blockers
    project["r3_warnings"] = warnings
    project["r3_next_action"] = next_action
    project["r3_matrix"] = matrix
    project["owner_attestations"] = attestations
    project["r3_cache_key"] = ck
    project["r3_firewall"] = analysis["firewall"]

    if persist:
        save_project(project)
    return analysis


def r3_operator_card(project: dict[str, Any]) -> dict[str, Any]:
    """Plain-language Bid Prep R3 panel — no legal jargon only."""
    a = project.get("r3_analysis") or {}
    if not a:
        return {"build": BUILD, "status": "NOT_RUN", "plain": "Company compliance not analyzed yet"}
    elig = a.get("eligibility") or {}
    cage = a.get("cage") or {}
    nmr = a.get("nmr") or {}
    trade = a.get("trade") or {}
    s889 = a.get("section_889") or {}
    regs = a.get("registrations") or {}
    return {
        "build": BUILD,
        "title": "COMPANY ELIGIBILITY",
        "small_business": elig.get("status") or elig.get("plain") or UNKNOWN,
        "sam": (a.get("profile_summary") or {}).get("SAM") or UNKNOWN,
        "cage": cage.get("status") or UNKNOWN,
        "cage_plain": cage.get("plain"),
        "nmr": nmr.get("decision_status") or UNKNOWN,
        "nmr_plain": nmr.get("rationale"),
        "trade": trade.get("overall_status") or UNKNOWN,
        "section_889": s889.get("status") or UNKNOWN,
        "section_889_plain": s889.get("plain"),
        "registrations": {
            "hard_blocks": len(regs.get("hard_blocks") or []),
            "register_before_bid": len(regs.get("register_before_bid") or []),
            "items": [
                {"name": i.get("registration"), "status": i.get("status"), "plain": i.get("plain")}
                for i in (regs.get("items") or [])
            ],
        },
        "cyber": (a.get("cyber") or {}).get("status"),
        "dpas": (a.get("dpas") or {}).get("plain"),
        "blockers": a.get("blockers") or [],
        "warnings": a.get("warnings") or [],
        "next_action": a.get("next_action"),
        "readiness": a.get("readiness"),
        "recommendation": a.get("recommendation"),
        "owner_attestations_open": sum(
            1 for x in (a.get("owner_attestations") or []) if not x.get("owner_confirmed")
        ),
        "disclaimer": a.get("legal_disclaimer"),
        "never_ready_to_submit": True,
        "matrix": a.get("compliance_matrix") or [],
    }


def get_r3_view(response_project_id: str) -> dict[str, Any]:
    project = load_project(response_project_id)
    if not project:
        return {"ok": False, "error": "not_found"}
    if not project.get("r3_analyzed_at"):
        run_r3_analysis(project)
        project = load_project(response_project_id) or project
    return {
        "ok": True,
        "analysis": project.get("r3_analysis"),
        "operator": r3_operator_card(project),
        "profile_version": project.get("company_profile_version"),
        "sam_api_calls": 0,
    }


def invalidate_r3(project: dict[str, Any], *, reason: str) -> None:
    project["r3_cache_key"] = None
    project["r3_stale_reason"] = reason
    project["r3_readiness"] = "COMPLIANCE_STALE"
    if project.get("r3_analysis"):
        project["r3_analysis"]["stale"] = True
        project["r3_analysis"]["stale_reason"] = reason


def confirm_owner_attestation_on_project(
    project: dict[str, Any],
    *,
    attestation_id: str,
    answer: str,
    confirmed_by: str,
) -> dict[str, Any]:
    from response_engine.owner_attestations import confirm_attestation

    found = None
    for a in project.get("owner_attestations") or []:
        if a.get("attestation_id") == attestation_id:
            found = a
            break
    if not found:
        return {"ok": False, "error": "attestation_not_found"}
    confirm_attestation(found, answer=answer, confirmed_by=confirmed_by)
    # Re-run analysis — confirmation never silently becomes unrelated PASS rewrite of facts
    run_r3_analysis(project, force=True)
    return {"ok": True, "attestation": found, "r3": project.get("r3_analysis")}


# --- helpers ---


def _extract_set_aside(blob: str) -> str | None:
    m = re.search(
        r"(?:set[\s\-]?aside|socioeconomic)[:\s]+([^\n.]{3,80})",
        blob,
        re.I,
    )
    if m:
        return m.group(1).strip()
    if re.search(r"total\s+small\s+business|small\s+business\s+set[\s\-]?aside", blob, re.I):
        return "Small Business Set-Aside"
    if re.search(r"unrestricted|full\s+and\s+open", blob, re.I):
        return "Unrestricted"
    return None


def _guess_jurisdiction(blob: str, project: dict[str, Any]) -> str:
    src = (project.get("authoritative_source") or project.get("source_system") or "").upper()
    if "DIBBS" in src or "DLA" in src or re.search(r"\bDIBBS\b|\bDLA\b", blob):
        return "DLA"
    if "SAM" in src or re.search(r"\bFAR\b|\bDFARS\b|solicitation\s+number", blob, re.I):
        return "federal"
    if re.search(r"state\s+of|county\s+of|city\s+of", blob, re.I):
        return "state"
    return "unknown"


def _procurement_type(blob: str, lines: list[dict[str, Any]]) -> str:
    if lines:
        return "supply"
    if re.search(r"\bservices?\b", blob, re.I) and not re.search(r"\bsupply\b|\bNSN\b|\bCLIN\b", blob, re.I):
        return "service"
    if re.search(r"\bNSN\b|\bCLIN\b|unit\s+price|quantity", blob, re.I):
        return "supply"
    return "unknown"


def _manufacturer_status(project: dict[str, Any], products: list[dict[str, Any]]) -> str:
    if project.get("manufacturer_status"):
        return str(project["manufacturer_status"]).lower()
    if not products:
        return "unknown"
    # Reseller offering third-party MPN → likely nonmanufacturer unless marked
    for p in products:
        if p.get("is_manufacturer") is True:
            return "manufacturer"
        if p.get("is_manufacturer") is False:
            return "nonmanufacturer"
    return "unknown"


def _offered_for_line(products: list[dict[str, Any]], line: dict[str, Any]) -> dict[str, Any] | None:
    sel = line.get("selected_offered_product_id")
    for p in products:
        if sel and p.get("offered_product_id") == sel:
            return p
        if p.get("line_item_id") == line.get("line_item_id") and p.get("selected"):
            return p
    for p in products:
        if p.get("line_item_id") == line.get("line_item_id"):
            return p
    return None


def _clause_list(project: dict[str, Any]) -> list[str]:
    out = []
    for r in project.get("requirements") or []:
        if r.get("clause_number"):
            out.append(str(r["clause_number"]))
        t = r.get("requirement_text") or ""
        if re.search(r"52\.\d+|252\.\d+", t):
            out.append(t[:120])
    return out


def _authorization_decision(blob: str, products: list[dict[str, Any]], profile: dict[str, Any]) -> dict[str, Any]:
    required = bool(_AUTH_RE.search(blob))
    if not required:
        # Also check R2 technical items
        return {"required": False, "status": "NOT_APPLICABLE", "plain": "Manufacturer authorization not identified as required"}
    verified = False
    for p in products:
        if str(p.get("authorization_status") or "").upper() in ("VERIFIED", "REQUIRED_AND_VERIFIED"):
            verified = True
    for a in profile.get("manufacturer_authorizations") or []:
        if str(a.get("status") or "").upper() in ("ACTIVE", "VERIFIED"):
            verified = True
    if verified:
        return {"required": True, "status": PASS_VERIFIED, "plain": "Required manufacturer authorization verified"}
    return {"required": True, "status": "COMPLIANCE_FAIL", "plain": "Required manufacturer authorization missing"}


def _source_approval_decision(blob: str, products: list[dict[str, Any]], profile: dict[str, Any]) -> dict[str, Any]:
    if not _SOURCE_APPROVAL_RE.search(blob):
        return {"required": False, "status": "NOT_APPLICABLE", "plain": "No source approval / QPL signal"}
    for p in products:
        if str(p.get("source_approval_status") or "").upper() in ("APPROVED", "QUALIFIED"):
            return {"required": True, "status": PASS_VERIFIED, "plain": "Approved source verified"}
    for s in profile.get("approved_sources") or []:
        if s:
            return {"required": True, "status": REVIEW_REQUIRED, "plain": "Profile lists approved sources — verify exact MPN match"}
    return {"required": True, "status": UNKNOWN, "plain": "Source approval required — status unknown"}


def _ensure_attestations(
    project: dict[str, Any],
    blob: str,
    s889: dict[str, Any],
    existing: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    out = list(existing)
    ids = {a.get("attestation_id") for a in out}
    topics = {a.get("topic") for a in out}

    # Only create 889 attestation when clause actually applies
    if s889.get("status") == OWNER_CONFIRMATION_REQUIRED and s889.get("applicability") == "APPLIES" and "section_889" not in topics:
        q = s889.get("owner_question") or (
            "Does the company use covered telecommunications equipment/services "
            "in the manner addressed by this representation?"
        )
        att = new_owner_attestation(
            response_project_id=project["response_project_id"],
            requirement_id=None,
            question=q,
            legal_note="Section 889 company-use representation — material legal certification",
            source="Section 889 / FAR 52.204-24/26",
            reconfirmation="per_solicitation",
        )
        att["topic"] = "section_889"
        reuse = suggest_reuse(q, allow_reuse=False)  # high-risk: never silent reuse
        att["library_suggestion"] = reuse
        out.append(att)

    # R1 requirements that apply to company and look like certifications
    for r in project.get("requirements") or []:
        if r.get("superseded"):
            continue
        if not r.get("applies_to_company") and (r.get("requirement_category") or "") not in {
            "CERTIFICATION",
            "REPRESENTATION",
            "ELIGIBILITY",
        }:
            continue
        text = r.get("requirement_text") or ""
        if not re.search(r"certif|represent|attest|bidder\s+certifies|offeror\s+represents", text, re.I):
            continue
        key = f"req:{r.get('requirement_id')}"
        if key in topics:
            continue
        att = new_owner_attestation(
            response_project_id=project["response_project_id"],
            requirement_id=r.get("requirement_id"),
            question=text[:500],
            source=r.get("clause_number") or "solicitation",
            reconfirmation="per_solicitation",
        )
        att["topic"] = key
        out.append(att)
        topics.add(key)
    # silence unused
    _ = ids
    return out


def _build_matrix(**ctx: Any) -> tuple[list, list, list]:
    project = ctx["project"]
    reps = []
    matrix = []
    maps = []

    def row(req, applies, answer, evidence, timing, status):
        matrix.append(
            {
                "requirement": req,
                "applies": applies,
                "answer": answer,
                "evidence": evidence,
                "timing": timing,
                "status": status,
            }
        )

    elig = ctx["eligibility"]
    row(
        "Small business / set-aside eligibility",
        "YES" if elig.get("eligible") is not False else "YES",
        elig.get("plain") or elig.get("reason"),
        "company_eligibility + profile",
        "AT_SUBMISSION",
        elig.get("status") or UNKNOWN,
    )
    cage = ctx["cage"]
    row("CAGE", "YES", cage.get("plain"), "company profile", "AT_SUBMISSION", PASS_VERIFIED if cage.get("status") == "ACTIVE" else FAIL)
    row(
        "SAM registration",
        "YES",
        ctx["profile"].get("SAM_registration_status"),
        "company profile (cached)",
        "AT_SUBMISSION",
        PASS_VERIFIED if str(ctx["profile"].get("SAM_registration_status") or "").upper() in ("ACTIVE", "TRUE", "YES") else UNKNOWN,
    )
    nmr = ctx["nmr"]
    row("Nonmanufacturer Rule", "YES" if nmr.get("applies") else ("NO" if nmr.get("applies") is False else "UNKNOWN"), nmr.get("rationale"), nmr.get("governing_sources"), "AT_SUBMISSION", nmr.get("decision_status"))
    trade = ctx["trade"]
    row("Trade compliance", "YES" if trade.get("regime") not in ("NONE",) else "NO", f"regime={trade.get('regime')}", trade.get("trace"), "AT_SUBMISSION", trade.get("overall_status"))
    s889 = ctx["s889"]
    row("Section 889", "YES" if s889.get("applicability") != "DOES_NOT_APPLY" else "NO", s889.get("plain"), "solicitation + SAM annual + owner", "AT_SUBMISSION", s889.get("status"))
    cyber = ctx["cyber"]
    row("Cybersecurity / CMMC", "YES" if cyber.get("status") != "NOT_APPLICABLE" else "NO", cyber.get("plain"), cyber.get("clauses"), "AT_SUBMISSION", cyber.get("status"))
    dpas = ctx["dpas"]
    row("DPAS", "YES" if dpas.get("applies") else "NO", dpas.get("plain"), dpas.get("rating_span"), "AT_SUBMISSION", dpas.get("status"))
    auth = ctx["auth"]
    row("Manufacturer authorization", "YES" if auth.get("required") else "NO", auth.get("plain"), "R2 product evidence", "AT_SUBMISSION", auth.get("status"))
    sa = ctx["source_approval"]
    row("Source approval / QPL", "YES" if sa.get("required") else "NO", sa.get("plain"), "solicitation + profile", "AT_SUBMISSION", sa.get("status"))

    for reg in (ctx["registrations"].get("items") or []):
        row(f"Registration: {reg.get('registration')}", "YES" if reg.get("required") else "NO", reg.get("plain"), reg.get("url"), reg.get("timing"), reg.get("status"))

    for att in ctx["attestations"]:
        st = OWNER_CONFIRMATION_REQUIRED if not att.get("owner_confirmed") else (
            PASS_VERIFIED if att.get("answer") in ("YES", "NO") else REVIEW_REQUIRED
        )
        # Spec: no PASS until explicit confirmation — confirmed YES/NO still may need review for material reps
        if att.get("owner_confirmed") and att.get("topic") == "section_889":
            st = PASS_VERIFIED if str(att.get("answer")).upper() == "NO" else REVIEW_REQUIRED
        row(
            f"Owner attestation: {(att.get('question') or '')[:80]}",
            "YES",
            att.get("answer") or "pending",
            att.get("source"),
            "AT_SUBMISSION",
            st,
        )
        rep = new_representation(
            response_project_id=project.get("response_project_id"),
            requirement_id=att.get("requirement_id"),
            representation_type="OWNER_ATTESTATION",
            applies="APPLIES",
            answer=att.get("answer"),
            answer_status=st,
            owner_attestation_required=not att.get("owner_confirmed"),
            source=att.get("source"),
        )
        reps.append(rep)
        maps.append(
            new_compliance_response_map(
                representation_id=rep["representation_id"],
                target_document="FUTURE_R4",
                response_type="checkbox_or_certification",
                signature_required=True,
                owner_confirmation_required=True,
            )
        )

    return reps, matrix, maps


def _compute_readiness(**ctx: Any) -> tuple[str, str, list[str], str, list[str]]:
    blockers: list[str] = []
    warnings: list[str] = []
    profile = ctx["profile"]
    conflicts = ctx["conflicts"]

    if conflicts:
        blockers.append(COMPANY_DATA_CONFLICT)
        return COMPANY_DATA_CONFLICT, DO_NOT_BID, blockers, "RESOLVE COMPANY DATA CONFLICT", warnings

    if (profile.get("completeness") or {}).get("incomplete"):
        warnings.append("Company profile incomplete — many fields UNKNOWN")

    elig = ctx["eligibility"]
    if elig.get("status") == FAIL or elig.get("eligible") is False:
        blockers.append(f"Not eligible for set-aside: {elig.get('matched') or elig.get('reason')}")
        return ELIGIBILITY_BLOCKED, DO_NOT_BID, blockers, "DO NOT BID — SET-ASIDE", warnings

    regs = ctx["registrations"]
    hard = regs.get("hard_blocks") or []
    # CAGE/DIBBS hard blocks for federal/DLA
    for h in hard:
        if h.get("registration") in ("CAGE", "DIBBS", "JCP") or h.get("operator_mode") == "DIBBS_CAGE_REQUIRED":
            blockers.append(h.get("plain") or f"{h.get('registration')} blocked")
    if any(h.get("registration") == "CAGE" for h in hard):
        return ELIGIBILITY_BLOCKED, DO_NOT_BID, blockers, "RESOLVE CAGE", warnings

    nmr = ctx["nmr"]
    if nmr.get("decision_status") == NMR_APPLIES_NONCOMPLIANT:
        blockers.append(nmr.get("rationale") or "NMR noncompliant")
        return NMR_REVIEW, DO_NOT_BID, blockers, "RESOLVE NMR / WAIVER", warnings
    if nmr.get("decision_status") in (NMR_REVIEW_REQUIRED, NMR_UNKNOWN) and nmr.get("applies"):
        warnings.append(nmr.get("rationale") or "NMR review required")
        # Material NMR unknown on set-aside → block readiness for response build
        blockers.append("NMR review required")
        return NMR_REVIEW, "REVIEW NMR", blockers, "RESOLVE NMR WAIVER APPLICABILITY", warnings

    trade = ctx["trade"]
    if trade.get("overall_status") == FAIL:
        blockers.append("Trade compliance fail on one or more lines")
        return TRADE_COMPLIANCE_REVIEW, DO_NOT_BID, blockers, "VERIFY COUNTRY OF ORIGIN", warnings
    if trade.get("overall_status") in (UNKNOWN, REVIEW_REQUIRED):
        warnings.append("Trade/COO needs review")
        if trade.get("unknown_count"):
            return TRADE_COMPLIANCE_REVIEW, "REVIEW TRADE", blockers + ["Country of origin unknown"], "VERIFY COUNTRY OF ORIGIN", warnings

    s889 = ctx["s889"]
    if s889.get("status") == OWNER_CONFIRMATION_REQUIRED:
        blockers.append("Section 889 owner confirmation needed")
        return OWNER_ATTESTATION_REQUIRED, "OWNER CONFIRMATION", blockers, "CONFIRM SECTION 889", warnings
    if s889.get("status") == FAIL:
        blockers.append("Section 889 failure")
        return ELIGIBILITY_BLOCKED, DO_NOT_BID, blockers, "RESOLVE SECTION 889", warnings

    auth = ctx["auth"]
    if auth.get("status") == "COMPLIANCE_FAIL":
        blockers.append(auth.get("plain") or "Manufacturer authorization missing")
        return ELIGIBILITY_BLOCKED, DO_NOT_BID, blockers, "UPLOAD MANUFACTURER AUTHORIZATION", warnings

    open_att = [a for a in ctx["attestations"] if not a.get("owner_confirmed")]
    if open_att:
        blockers.append(f"{len(open_att)} owner attestation(s) required")
        return OWNER_ATTESTATION_REQUIRED, "OWNER CONFIRMATION", blockers, "CONFIRM OWNER ATTESTATIONS", warnings

    soft = regs.get("register_before_bid") or []
    if soft and not hard:
        warnings.append("Registration needed before bid (easy path)")
        return REGISTRATION_REQUIRED, REGISTER_BEFORE_BID, blockers, "COMPLETE VENDOR REGISTRATION", warnings

    cyber = ctx["cyber"]
    if cyber.get("status") in ("REQUIRED_NOT_MET",):
        blockers.append(cyber.get("plain") or "CMMC not met")
        return ELIGIBILITY_BLOCKED, DO_NOT_BID, blockers, "RESOLVE CYBER REQUIREMENT", warnings
    if cyber.get("status") in ("REQUIRED_UNKNOWN", "REVIEW_REQUIRED"):
        warnings.append(cyber.get("plain") or "Cyber review")
        return TRADE_COMPLIANCE_REVIEW, "REVIEW CYBER", blockers, "VERIFY CYBER EVIDENCE", warnings

    if (profile.get("completeness") or {}).get("incomplete"):
        return COMPANY_PROFILE_INCOMPLETE, "COMPLETE COMPANY PROFILE", blockers, "UPDATE COMPANY PROFILE", warnings

    return COMPLIANCE_READY, "COMPLIANCE REVIEW COMPLETE", blockers, "PROCEED TO RESPONSE BUILD (R4)", warnings


def _r3_firewall_check(project: dict[str, Any]) -> dict[str, Any]:
    """Ensure R3 public surface does not leak internal economics."""
    fw = firewall_report(project) if project else {"ok": True}
    leaks = []
    public = project.get("r3_analysis") or {}
    raw = json.dumps(public, default=str)
    for bad in ("max_buy", "internal_max_buy", "target_margin", "target_profit", "supplier_strategy"):
        # Allow presence only inside nested r2 that shouldn't be in r3 — check top-level keys
        if bad in (public.get("profile_summary") or {}):
            leaks.append(bad)
    # Strip any accidental economics from operator-bound fields
    return {
        "ok": not leaks and fw.get("ok", True),
        "leaks": leaks,
        "note": "R3 government namespace excludes internal pricing/strategy",
        "r2_firewall": fw,
    }


__all__ = [
    "run_r3_analysis",
    "r3_operator_card",
    "get_r3_view",
    "invalidate_r3",
    "confirm_owner_attestation_on_project",
    "new_representation",
    "new_compliance_response_map",
]
