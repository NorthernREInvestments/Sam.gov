"""Eligibility / vehicle / access gate — eligibility outranks economics."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from application_clock import now_utc

BUILD_TAG = "20260925-m3-eligibility-gate-1"

ELIGIBLE_CONFIRMED = "ELIGIBLE_CONFIRMED"
ELIGIBLE_CONDITIONAL = "ELIGIBLE_CONDITIONAL"
NOT_CURRENTLY_ELIGIBLE = "NOT_CURRENTLY_ELIGIBLE"
ELIGIBILITY_UNKNOWN = "ELIGIBILITY_UNKNOWN"
ELIGIBILITY_NOT_APPLICABLE = "ELIGIBILITY_NOT_APPLICABLE"

# Operator-facing states
VERIFY_ELIGIBILITY = "VERIFY_ELIGIBILITY"
OBTAIN_VEHICLE_ACCESS = "OBTAIN_VEHICLE_ACCESS"
COMPLETE_BOA_ONRAMP = "COMPLETE_BOA_ONRAMP"
VERIFY_JCP_STATUS = "VERIFY_JCP_STATUS"
OBTAIN_TDP_ACCESS = "OBTAIN_TDP_ACCESS"
VERIFY_SET_ASIDE_ELIGIBILITY = "VERIFY_SET_ASIDE_ELIGIBILITY"
VERIFY_APPROVED_SOURCE_STATUS = "VERIFY_APPROVED_SOURCE_STATUS"
NOT_ELIGIBLE_FOR_THIS_OPPORTUNITY = "NOT_ELIGIBLE_FOR_THIS_OPPORTUNITY"
ELIGIBILITY_ACTION_REQUIRED = "ELIGIBILITY_ACTION_REQUIRED"

STATUS_HELD = "HELD"
STATUS_MISSING = "MISSING"
STATUS_UNKNOWN = "UNKNOWN"
STATUS_NOT_REQUIRED = "NOT_REQUIRED"
STATUS_CONDITIONAL = "CONDITIONAL"

_ROOT = Path(__file__).resolve().parents[1]
_PROFILE_PATH = _ROOT / "data" / "company_eligibility_profile.json"

# Deterministic vehicle / restriction detectors: (code, display_name, compiled patterns)
_VEHICLE_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    (
        "BOAST_BOA",
        "Army BOAST BOA",
        re.compile(
            r"\bBOAST\b|"
            r"Basic\s+Ordering\s+Agreement.{0,40}BOAST|"
            r"BOAST.{0,60}(?:BOA|Basic\s+Ordering)|"
            r"RFOP.{0,40}BOAST|BOAST.{0,40}RFOP|"
            r"active\s+BOAST|"
            r"BOAST\s+(?:holders?|contractors?|awardees?)",
            re.I | re.S,
        ),
    ),
    (
        "IDIQ_HOLDER",
        "IDIQ / MAC / MATOC holder",
        re.compile(
            r"(?:only|limited\s+to|restricted\s+to|must\s+(?:be|have)|offerors?\s+must).{0,80}"
            r"(?:IDIQ|MATOC|MAC|multiple\s+award)\s+holders?|"
            r"(?:IDIQ|MATOC|MAC)\s+holders?\s+only|"
            r"task\s+order\s+competition\s+among\s+(?:existing\s+)?(?:IDIQ|MATOC|MAC)|"
            r"delivery\s+order\s+competition\s+among",
            re.I | re.S,
        ),
    ),
    (
        "BPA_HOLDER",
        "BPA holder",
        re.compile(
            r"(?:only|limited\s+to|restricted\s+to).{0,60}BPA\s+holders?|"
            r"BPA\s+holders?\s+only|"
            r"must\s+hold.{0,40}BPA",
            re.I | re.S,
        ),
    ),
    (
        "GSA_SCHEDULE",
        "GSA Schedule",
        re.compile(
            r"(?:only|limited\s+to|restricted\s+to).{0,60}GSA\s+Schedule|"
            r"must\s+(?:hold|have).{0,40}GSA\s+Schedule|"
            r"GSA\s+Schedule\s+holders?\s+only",
            re.I | re.S,
        ),
    ),
    (
        "SEAPORT_NXG",
        "SeaPort-NxG",
        re.compile(r"\bSeaPort[\s\-]?NxG\b|SeaPort\s+Next\s+Generation", re.I),
    ),
    (
        "OASIS",
        "OASIS",
        re.compile(r"\bOASIS\s*(?:\+|SB)?\b.{0,40}holders?|holders?.{0,40}\bOASIS\b", re.I | re.S),
    ),
    (
        "SEWP",
        "NASA SEWP",
        re.compile(r"\bSEWP\b.{0,40}holders?|NASA\s+SEWP", re.I | re.S),
    ),
    (
        "CIO_SP",
        "CIO-SP3/SP4",
        re.compile(r"\bCIO[\s\-]?SP[34]\b", re.I),
    ),
]

_HOLDER_ONLY_RE = re.compile(
    r"(?:only\s+(?:open\s+to\s+)?(?:current\s+|active\s+|existing\s+)?(?:contract\s+)?holders?|"
    r"limited\s+to\s+(?:current\s+|active\s+|existing\s+)?(?:contract\s+)?holders?|"
    r"restricted\s+to\s+(?:current\s+|active\s+|existing\s+)?(?:contract\s+)?holders?|"
    r"offerors?\s+must\s+(?:hold|have)\s+an?\s+active|"
    r"must\s+have\s+an?\s+active|"
    r"current\s+BOA|"
    r"existing\s+contract\s+holders?|"
    r"awardees?\s+under|"
    r"eligible\s+ordering\s+vehicle|"
    r"only\s+contractors?\s+listed|"
    r"competition\s+among\s+(?:existing\s+)?(?:IDIQ|BOA|BPA|MAC|MATOC)|"
    r"qualified\s+bidders?\s+list|"
    r"prequalified\s+supplier|"
    r"vendor\s+pool)",
    re.I | re.S,
)

_JCP_RE = re.compile(
    r"\bJCP\b|DD[\s\-]?Form\s*2345|DD2345|"
    r"Joint\s+Certification\s+Program|"
    r"certified\s+contractor\s+access|"
    r"export[\s\-]?controlled\s+(?:technical\s+data|TDP|drawings?)",
    re.I,
)

_APPROVED_SOURCE_RE = re.compile(
    r"approved\s+source(?:s)?\s+only|"
    r"source\s+approval\s+required|"
    r"QPL|QML|"
    r"only\s+from\s+approved\s+(?:manufacturers?|sources?)",
    re.I,
)

_RFOP_RE = re.compile(r"\bRFOP\b|Request\s+for\s+(?:Order\s+)?Proposal", re.I)


def _utc() -> str:
    return now_utc().isoformat()


def default_company_profile() -> dict[str, Any]:
    """Company eligibility profile — UNKNOWN/empty until operator confirms. No invented holdings."""
    return {
        "kind": "CompanyEligibilityProfile",
        "build": BUILD_TAG,
        "sam_active": "UNKNOWN",
        "uei": "UNKNOWN",
        "cage": "UNKNOWN",
        "sba_size_status": "UNKNOWN",
        "set_aside_qualifications": [],
        "vehicles_held": [],  # e.g. ["BOAST_BOA"]
        "vehicles_pending": [],
        "jcp_status": "UNKNOWN",  # HELD | MISSING | UNKNOWN
        "export_control_access": "UNKNOWN",
        "facility_clearance": "UNKNOWN",
        "personnel_clearance": "UNKNOWN",
        "approved_sources": [],
        "manufacturer_authorizations": [],
        "licenses": [],
        "bonding_capability": "UNKNOWN",
        "notes": "Default profile: no contract vehicles confirmed. Do not invent eligibility.",
        "updated_at": None,
        "source": "default_empty",
    }


def load_company_eligibility_profile(path: Path | None = None) -> dict[str, Any]:
    path = path or _PROFILE_PATH
    base = default_company_profile()
    # Env override: COMPANY_VEHICLES_HELD=BOAST_BOA,GSA_SCHEDULE
    env_veh = (os.getenv("COMPANY_VEHICLES_HELD") or "").strip()
    if env_veh:
        base["vehicles_held"] = [v.strip().upper() for v in env_veh.split(",") if v.strip()]
        base["source"] = "env:COMPANY_VEHICLES_HELD"
    env_jcp = (os.getenv("COMPANY_JCP_STATUS") or "").strip().upper()
    if env_jcp in {STATUS_HELD, STATUS_MISSING, STATUS_UNKNOWN}:
        base["jcp_status"] = env_jcp
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                merged = {**base, **data}
                # list fields: prefer file if present
                for k in (
                    "vehicles_held",
                    "vehicles_pending",
                    "set_aside_qualifications",
                    "approved_sources",
                    "manufacturer_authorizations",
                    "licenses",
                ):
                    if k in data and isinstance(data[k], list):
                        merged[k] = data[k]
                merged["kind"] = "CompanyEligibilityProfile"
                return merged
        except Exception:
            pass
    return base


def save_company_eligibility_profile(profile: dict[str, Any], path: Path | None = None) -> Path:
    path = path or _PROFILE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    out = dict(profile)
    out["updated_at"] = _utc()
    path.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    return path


def _collect_text(row: dict[str, Any], extra_text: str | None = None) -> str:
    parts: list[str] = []
    for k in (
        "title",
        "description",
        "solicitation_text",
        "notice_text",
        "body",
        "extracted_text",
    ):
        v = row.get(k)
        if v:
            parts.append(str(v))
    facts = row.get("extracted_facts") if isinstance(row.get("extracted_facts"), dict) else {}
    if facts:
        parts.append(json.dumps(facts, default=str))
    for d in row.get("documents") or []:
        if isinstance(d, dict):
            parts.append(str(d.get("text") or d.get("excerpt") or d.get("title") or ""))
        else:
            parts.append(str(d))
    # Phase H packet fields
    for k in ("phase_h_noticedesc", "document_review"):
        v = row.get(k)
        if isinstance(v, dict):
            parts.append(json.dumps(v, default=str)[:5000])
        elif v:
            parts.append(str(v)[:5000])
    if extra_text:
        parts.append(extra_text)
    return "\n".join(parts)


def detect_vehicle_requirements(text: str) -> list[dict[str, Any]]:
    """Deterministic vehicle / holder-only detection from solicitation text."""
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    blob = text or ""
    for code, name, pat in _VEHICLE_PATTERNS:
        m = pat.search(blob)
        if not m:
            continue
        if code in seen:
            continue
        seen.add(code)
        snippet = m.group(0)
        if len(snippet) > 180:
            snippet = snippet[:180]
        found.append(
            {
                "code": code,
                "name": name,
                "matched_text": snippet,
                "holder_restriction_likely": bool(_HOLDER_ONLY_RE.search(blob))
                or code in {"BOAST_BOA", "IDIQ_HOLDER", "BPA_HOLDER"}
                or bool(_RFOP_RE.search(blob) and code == "BOAST_BOA"),
                "evidence_source": "solicitation_text",
            }
        )
    # Generic holder-only without named vehicle still flags unknown vehicle
    if not found and _HOLDER_ONLY_RE.search(blob):
        m = _HOLDER_ONLY_RE.search(blob)
        found.append(
            {
                "code": "UNSPECIFIED_VEHICLE_HOLDER",
                "name": "Contract vehicle / holder restriction (unspecified)",
                "matched_text": (m.group(0) if m else "")[:180],
                "holder_restriction_likely": True,
                "evidence_source": "solicitation_text",
            }
        )
    # BOAST in title even without long holder phrase (RFOP pattern)
    if "BOAST_BOA" not in seen and re.search(r"\bBOAST\b", blob, re.I):
        if _RFOP_RE.search(blob) or re.search(r"\bBOA\b", blob, re.I) or _HOLDER_ONLY_RE.search(blob):
            found.append(
                {
                    "code": "BOAST_BOA",
                    "name": "Army BOAST BOA",
                    "matched_text": "BOAST (title/description signal)",
                    "holder_restriction_likely": True,
                    "evidence_source": "solicitation_text",
                }
            )
    return found


def detect_access_requirements(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    blob = text or ""
    if _JCP_RE.search(blob):
        m = _JCP_RE.search(blob)
        out.append(
            {
                "code": "JCP",
                "name": "JCP / DD2345 / export-controlled data access",
                "matched_text": (m.group(0) if m else "JCP")[:120],
                "evidence_source": "solicitation_text",
            }
        )
    if _APPROVED_SOURCE_RE.search(blob):
        m = _APPROVED_SOURCE_RE.search(blob)
        out.append(
            {
                "code": "APPROVED_SOURCE",
                "name": "Approved source / QPL",
                "matched_text": (m.group(0) if m else "")[:120],
                "evidence_source": "solicitation_text",
            }
        )
    return out


def _vehicle_status(code: str, profile: dict[str, Any]) -> str:
    held = {str(x).upper() for x in (profile.get("vehicles_held") or [])}
    pending = {str(x).upper() for x in (profile.get("vehicles_pending") or [])}
    if code.upper() in held:
        return STATUS_HELD
    if code.upper() in pending:
        return STATUS_CONDITIONAL
    # Explicit empty list + default profile → treat as missing confirmation (UNKNOWN for generic, MISSING for named)
    if code == "UNSPECIFIED_VEHICLE_HOLDER":
        return STATUS_UNKNOWN
    return STATUS_MISSING  # not confirmed held


def evaluate_eligibility_gate(
    row: dict[str, Any] | None = None,
    *,
    text: str | None = None,
    profile: dict[str, Any] | None = None,
    deadline_runway_days: float | None = None,
) -> dict[str, Any]:
    """
    Evaluate eligibility/vehicle/access before quote/bid actionability.
    Does not invent company holdings. UNKNOWN / MISSING block actionable readiness.
    """
    row = row if isinstance(row, dict) else {}
    profile = profile or load_company_eligibility_profile()
    blob = _collect_text(row, extra_text=text)
    vehicles = detect_vehicle_requirements(blob)
    access = detect_access_requirements(blob)

    evidence: list[dict[str, Any]] = []
    other_prerequisites: list[dict[str, Any]] = []
    blocking: list[str] = []
    next_action = None
    operator_state = None

    vehicle_required = bool(vehicles)
    vehicle_name = vehicles[0]["name"] if vehicles else None
    vehicle_code = vehicles[0]["code"] if vehicles else None
    vehicle_status = STATUS_NOT_REQUIRED
    boa_required = False
    boa_status = STATUS_NOT_REQUIRED

    if vehicles:
        primary = vehicles[0]
        vehicle_status = _vehicle_status(primary["code"], profile)
        evidence.append(
            {
                "claim": f"Vehicle/holder restriction detected: {primary['name']}",
                "matched_text": primary.get("matched_text"),
                "source": primary.get("evidence_source"),
                "code": primary["code"],
            }
        )
        if primary["code"] == "BOAST_BOA" or "BOA" in primary["code"]:
            boa_required = True
            boa_status = vehicle_status

        if vehicle_status == STATUS_HELD:
            pass
        elif vehicle_status == STATUS_CONDITIONAL:
            blocking.append(f"vehicle_pending:{primary['code']}")
            next_action = (
                f"Complete {primary['name']} on-ramp/enrollment before requesting supplier pricing."
            )
            operator_state = COMPLETE_BOA_ONRAMP if primary["code"] == "BOAST_BOA" else OBTAIN_VEHICLE_ACCESS
        elif vehicle_status == STATUS_MISSING:
            blocking.append(f"vehicle_not_held:{primary['code']}")
            if primary["code"] == "BOAST_BOA":
                next_action = (
                    "Confirm or obtain Army BOAST BOA eligibility before requesting supplier pricing. "
                    "This RFOP is restricted to active BOAST BOA holders."
                )
                operator_state = COMPLETE_BOA_ONRAMP
            else:
                next_action = (
                    f"Verify/obtain {primary['name']} holder status before quote outreach or bid prep."
                )
                operator_state = OBTAIN_VEHICLE_ACCESS
        else:
            blocking.append(f"vehicle_status_unknown:{primary['code']}")
            next_action = f"Confirm whether company holds {primary['name']} before any supplier outreach."
            operator_state = VERIFY_ELIGIBILITY

    # Additional vehicles beyond primary
    for v in vehicles[1:]:
        st = _vehicle_status(v["code"], profile)
        other_prerequisites.append({**v, "company_status": st})
        if st not in {STATUS_HELD, STATUS_NOT_REQUIRED}:
            blocking.append(f"vehicle_not_held:{v['code']}")

    jcp_required = any(a["code"] == "JCP" for a in access)
    jcp_status = STATUS_NOT_REQUIRED
    if jcp_required:
        js = str(profile.get("jcp_status") or STATUS_UNKNOWN).upper()
        jcp_status = js if js in {STATUS_HELD, STATUS_MISSING, STATUS_UNKNOWN} else STATUS_UNKNOWN
        evidence.append({"claim": "JCP/export-controlled access indicated", "source": "solicitation_text"})
        if jcp_status != STATUS_HELD:
            blocking.append("jcp_not_confirmed")
            if not next_action:
                next_action = "Verify JCP / DD2345 status before pursuing controlled technical data or quotes that depend on it."
                operator_state = VERIFY_JCP_STATUS

    approved_source_required = any(a["code"] == "APPROVED_SOURCE" for a in access)
    approved_source_status = STATUS_NOT_REQUIRED
    if approved_source_required:
        approved = {str(x).upper() for x in (profile.get("approved_sources") or [])}
        # Without a specific source code on the opp, status is UNKNOWN unless profile claims general capability
        approved_source_status = STATUS_HELD if approved else STATUS_UNKNOWN
        if approved_source_status != STATUS_HELD:
            blocking.append("approved_source_unconfirmed")
            if not next_action:
                next_action = "Verify approved-source / QPL status before quote outreach."
                operator_state = VERIFY_APPROVED_SOURCE_STATUS

    # Set-aside via existing company_eligibility helper (non-blocking unknown)
    set_aside_raw = row.get("set_aside") or row.get("typeOfSetAside") or row.get("typeOfSetAsideDescription")
    set_aside_eligibility = "UNKNOWN"
    set_aside_status = STATUS_NOT_REQUIRED
    try:
        from company_eligibility import set_aside_eligibility as _sa

        sa = _sa(set_aside_raw)
        if sa.get("eligible") is True:
            set_aside_eligibility = "ELIGIBLE"
            set_aside_status = STATUS_HELD
        elif sa.get("eligible") is False:
            set_aside_eligibility = "NOT_ELIGIBLE"
            set_aside_status = STATUS_MISSING
            blocking.append(f"set_aside:{sa.get('matched')}")
            if not next_action:
                next_action = f"Company is not eligible for set-aside {sa.get('matched')}."
                operator_state = VERIFY_SET_ASIDE_ELIGIBILITY
        elif set_aside_raw:
            set_aside_eligibility = "UNKNOWN"
            set_aside_status = STATUS_UNKNOWN
    except Exception:
        pass

    # Deadline-aware conditional: only if pending path AND runway known and generous — still NOT quote-ready
    overall = ELIGIBILITY_NOT_APPLICABLE
    if not vehicles and not jcp_required and not approved_source_required and set_aside_status != STATUS_MISSING:
        overall = ELIGIBILITY_NOT_APPLICABLE
    elif STATUS_MISSING in (
        vehicle_status if vehicle_required else STATUS_NOT_REQUIRED,
        jcp_status if jcp_required else STATUS_NOT_REQUIRED,
    ) or set_aside_status == STATUS_MISSING:
        # Missing confirmed holding
        if vehicle_status == STATUS_CONDITIONAL or (
            vehicle_status == STATUS_MISSING
            and deadline_runway_days is not None
            and deadline_runway_days >= 30
            and vehicle_code in (profile.get("vehicles_pending") or [])
        ):
            overall = ELIGIBLE_CONDITIONAL
        elif vehicle_status == STATUS_MISSING or set_aside_status == STATUS_MISSING:
            overall = NOT_CURRENTLY_ELIGIBLE
        else:
            overall = ELIGIBILITY_UNKNOWN
    elif any(
        x == STATUS_UNKNOWN
        for x in (
            vehicle_status if vehicle_required else STATUS_NOT_REQUIRED,
            jcp_status if jcp_required else STATUS_NOT_REQUIRED,
            approved_source_status if approved_source_required else STATUS_NOT_REQUIRED,
        )
    ):
        overall = ELIGIBILITY_UNKNOWN
    elif vehicle_status == STATUS_CONDITIONAL:
        overall = ELIGIBLE_CONDITIONAL
    elif blocking:
        overall = ELIGIBILITY_UNKNOWN if "unknown" in " ".join(blocking) else NOT_CURRENTLY_ELIGIBLE
    else:
        overall = ELIGIBLE_CONFIRMED

    # Normalize: if we detected BOAST and not held → NOT_CURRENTLY_ELIGIBLE (not unknown)
    if boa_required and boa_status == STATUS_MISSING:
        overall = NOT_CURRENTLY_ELIGIBLE
        operator_state = operator_state or COMPLETE_BOA_ONRAMP
        next_action = next_action or (
            "Confirm or obtain Army BOAST BOA eligibility before requesting supplier pricing."
        )

    if overall == ELIGIBLE_CONDITIONAL:
        operator_state = operator_state or ELIGIBILITY_ACTION_REQUIRED
    if overall == NOT_CURRENTLY_ELIGIBLE:
        operator_state = operator_state or NOT_ELIGIBLE_FOR_THIS_OPPORTUNITY
    if overall == ELIGIBILITY_UNKNOWN:
        operator_state = operator_state or VERIFY_ELIGIBILITY

    actionable = overall in {ELIGIBLE_CONFIRMED, ELIGIBILITY_NOT_APPLICABLE}

    return {
        "kind": "EligibilityGateResult",
        "build": BUILD_TAG,
        "overall_status": overall,
        "actionable_for_quote_or_bid": actionable,
        "vehicle_required": vehicle_required,
        "vehicle_name": vehicle_name,
        "vehicle_code": vehicle_code,
        "vehicle_status": vehicle_status,
        "boa_required": boa_required,
        "boa_status": boa_status,
        "idiq_holder_required": any(v["code"] == "IDIQ_HOLDER" for v in vehicles),
        "idiq_holder_status": _vehicle_status("IDIQ_HOLDER", profile)
        if any(v["code"] == "IDIQ_HOLDER" for v in vehicles)
        else STATUS_NOT_REQUIRED,
        "schedule_required": any(v["code"] == "GSA_SCHEDULE" for v in vehicles),
        "schedule_status": _vehicle_status("GSA_SCHEDULE", profile)
        if any(v["code"] == "GSA_SCHEDULE" for v in vehicles)
        else STATUS_NOT_REQUIRED,
        "set_aside_eligibility": set_aside_eligibility,
        "set_aside_status": set_aside_status,
        "jcp_required": jcp_required,
        "jcp_status": jcp_status,
        "cage_required": False,
        "cage_status": STATUS_NOT_REQUIRED,
        "sam_active_required": False,
        "sam_status": profile.get("sam_active") or STATUS_UNKNOWN,
        "security_clearance_required": False,
        "security_clearance_status": STATUS_NOT_REQUIRED,
        "facility_clearance_required": False,
        "facility_clearance_status": profile.get("facility_clearance") or STATUS_UNKNOWN,
        "export_controlled": jcp_required,
        "export_access_status": profile.get("export_control_access") or STATUS_UNKNOWN,
        "source_approval_required": approved_source_required,
        "source_approval_status": approved_source_status,
        "approved_source_required": approved_source_required,
        "approved_source_status": approved_source_status,
        "manufacturer_authorization_required": False,
        "manufacturer_authorization_status": STATUS_NOT_REQUIRED,
        "license_required": False,
        "license_status": STATUS_NOT_REQUIRED,
        "bond_required": False,
        "bond_status": STATUS_NOT_REQUIRED,
        "deposit_required": False,
        "deposit_status": STATUS_NOT_REQUIRED,
        "other_prerequisites": other_prerequisites + access,
        "vehicles_detected": vehicles,
        "access_detected": access,
        "evidence": evidence,
        "blocking_reasons": blocking,
        "blocking_reason": blocking[0] if blocking else None,
        "next_action": next_action,
        "operator_state": operator_state,
        "deadline_runway_days": deadline_runway_days,
        "company_profile_source": profile.get("source"),
        "plain": _plain_summary(overall, vehicle_name, boa_required, boa_status, next_action),
        "generated_at": _utc(),
    }


def _plain_summary(
    overall: str,
    vehicle_name: str | None,
    boa_required: bool,
    boa_status: str,
    next_action: str | None,
) -> dict[str, str]:
    if overall == ELIGIBILITY_NOT_APPLICABLE:
        return {"label": "No special vehicle prerequisite detected", "detail": ""}
    if boa_required and boa_status != STATUS_HELD:
        return {
            "label": "Blocked — BOAST BOA required",
            "detail": (
                "This RFOP is restricted to active BOAST BOA holders. "
                "M3 does not have confirmed BOAST status for your company. "
                "Do not request supplier pricing yet."
            ),
            "next": next_action or "Complete or verify BOAST BOA enrollment.",
        }
    if overall == NOT_CURRENTLY_ELIGIBLE:
        return {
            "label": f"Blocked — {vehicle_name or 'eligibility'} required",
            "detail": next_action or "Company is not currently eligible for this opportunity.",
            "next": next_action or "Resolve eligibility before outreach.",
        }
    if overall == ELIGIBLE_CONDITIONAL:
        return {
            "label": "Eligibility action required",
            "detail": next_action or "Complete conditional eligibility path before quote outreach.",
            "next": next_action or "Complete eligibility on-ramp.",
        }
    if overall == ELIGIBILITY_UNKNOWN:
        return {
            "label": "Eligibility unknown",
            "detail": next_action or "Confirm eligibility before spending time on quotes.",
            "next": next_action or "Verify eligibility.",
        }
    return {"label": "Eligibility confirmed", "detail": ""}


def blocks_quote_outreach(gate: dict[str, Any] | None) -> bool:
    if not isinstance(gate, dict):
        return True  # fail closed if gate missing when required
    return gate.get("actionable_for_quote_or_bid") is not True
