"""R3 company compliance profile — versioned, source-backed, fail-closed.

Canonical store: data/company_eligibility_profile.json (extended).
Set-aside held certs also consult company_eligibility.env (bootstrap).
Never invents SAM/CAGE/certs. Conflicts surface as COMPANY_DATA_CONFLICT.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from application_clock import now_utc

from eligibility_gate import load_company_eligibility_profile
from response_engine.models import new_id
from response_engine.r3_constants import BUILD, COMPANY_DATA_CONFLICT, UNKNOWN

ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = ROOT / "data" / "company_eligibility_profile.json"
PROFILE_HISTORY = ROOT / "data" / "company_compliance_profile_history.json"
ATTESTATION_LIBRARY = ROOT / "data" / "company_attestation_library.json"


def _utc() -> str:
    return now_utc().isoformat()


def _sha(obj: Any) -> str:
    import hashlib

    raw = json.dumps(obj, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def load_company_compliance_profile(*, path: Path | None = None) -> dict[str, Any]:
    """Load eligibility JSON + normalize into CompanyComplianceProfile shape."""
    base = load_company_eligibility_profile(path or PROFILE_PATH)
    try:
        from company_eligibility import held_certifications

        held = sorted(held_certifications())
    except Exception:
        held = []

    profile = {
        "kind": "CompanyComplianceProfile",
        "build": BUILD,
        "profile_version": base.get("profile_version") or f"cp-{_sha(base)}",
        "legal_name": base.get("legal_name") or UNKNOWN,
        "entity_type": base.get("entity_type") or UNKNOWN,
        "jurisdiction": base.get("business_formation_state") or UNKNOWN,
        "principal_business_address": base.get("principal_business_address") or UNKNOWN,
        "mailing_address": base.get("mailing_address") or UNKNOWN,
        "UEI": base.get("uei") or UNKNOWN,
        "CAGE": base.get("cage") or UNKNOWN,
        "SAM_registration_status": base.get("sam_active") or UNKNOWN,
        "SAM_expiration": base.get("sam_expiration") or UNKNOWN,
        "SAM_reps_certs_date": base.get("sam_reps_certs_date") or UNKNOWN,
        "NAICS_codes": base.get("naics_codes") or [],
        "primary_NAICS": base.get("primary_naics") or UNKNOWN,
        "employee_count": base.get("employee_count") or UNKNOWN,
        "annual_receipts": base.get("annual_receipts") or UNKNOWN,
        "small_business_status": base.get("sba_size_status") or UNKNOWN,
        "set_aside_qualifications": list(base.get("set_aside_qualifications") or []) or held,
        "held_certifications_env": held,
        "ownership_certifications": base.get("ownership_certifications") or [],
        "vehicles_held": base.get("vehicles_held") or [],
        "vehicles_pending": base.get("vehicles_pending") or [],
        "jcp_status": base.get("jcp_status") or UNKNOWN,
        "export_control_access": base.get("export_control_access") or UNKNOWN,
        "approved_sources": base.get("approved_sources") or [],
        "manufacturer_authorizations": base.get("manufacturer_authorizations") or [],
        "licenses": base.get("licenses") or [],
        "bonding_capability": base.get("bonding_capability") or UNKNOWN,
        "state_local_registrations": base.get("state_local_registrations") or [],
        "cmmc_level": base.get("cmmc_level") or UNKNOWN,
        "section_889_annual": base.get("section_889_annual") or UNKNOWN,
        "authorized_signers": base.get("authorized_signers") or [],
        "field_sources": base.get("field_sources") or {},
        "sam_snapshot": base.get("sam_snapshot"),
        "source": base.get("source") or "company_eligibility_profile",
        "verification_date": base.get("updated_at") or base.get("verification_date"),
        "notes": base.get("notes"),
        "raw_eligibility_profile": {k: v for k, v in base.items() if k != "notes"},
    }
    profile["completeness"] = profile_completeness(profile)
    profile["conflicts"] = detect_profile_conflicts(profile)
    return profile


def profile_completeness(profile: dict[str, Any]) -> dict[str, Any]:
    required_identity = ["UEI", "CAGE", "SAM_registration_status", "small_business_status"]
    known, unknown, stale = [], [], []
    for f in required_identity:
        v = profile.get(f)
        if v in (None, "", UNKNOWN, "UNKNOWN"):
            unknown.append(f)
        else:
            known.append(f)
    return {
        "required_known": known,
        "required_unknown": unknown,
        "stale": stale,
        "incomplete": bool(unknown),
    }


def detect_profile_conflicts(profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Surface SAM vs owner conflicts — never silent merge."""
    conflicts = []
    snap = profile.get("sam_snapshot") or {}
    if not isinstance(snap, dict):
        return conflicts
    if snap.get("uei") and profile.get("UEI") not in (None, UNKNOWN, "UNKNOWN"):
        if str(snap["uei"]).upper() != str(profile["UEI"]).upper():
            conflicts.append(
                {
                    "type": COMPANY_DATA_CONFLICT,
                    "field": "UEI",
                    "sam": snap.get("uei"),
                    "profile": profile.get("UEI"),
                }
            )
    if snap.get("cage") and profile.get("CAGE") not in (None, UNKNOWN, "UNKNOWN"):
        if str(snap["cage"]).upper() != str(profile["CAGE"]).upper():
            conflicts.append(
                {
                    "type": COMPANY_DATA_CONFLICT,
                    "field": "CAGE",
                    "sam": snap.get("cage"),
                    "profile": profile.get("CAGE"),
                }
            )
    if snap.get("entity_name") and profile.get("legal_name") not in (None, UNKNOWN, "UNKNOWN"):
        if str(snap["entity_name"]).strip().upper() != str(profile["legal_name"]).strip().upper():
            conflicts.append(
                {
                    "type": COMPANY_DATA_CONFLICT,
                    "field": "legal_name",
                    "sam": snap.get("entity_name"),
                    "profile": profile.get("legal_name"),
                }
            )
    if snap.get("physical_address") and profile.get("principal_business_address") not in (
        None,
        UNKNOWN,
        "UNKNOWN",
    ):
        a = str(snap["physical_address"]).strip().upper()
        b = str(profile["principal_business_address"]).strip().upper()
        if a and b and a != b:
            conflicts.append(
                {
                    "type": COMPANY_DATA_CONFLICT,
                    "field": "principal_business_address",
                    "sam": snap.get("physical_address"),
                    "profile": profile.get("principal_business_address"),
                }
            )
    return conflicts


def save_profile_version(profile: dict[str, Any]) -> str:
    """Append versioned history for audit reproducibility."""
    hist: dict[str, Any] = {"versions": []}
    if PROFILE_HISTORY.exists():
        try:
            hist = json.loads(PROFILE_HISTORY.read_text(encoding="utf-8"))
        except Exception:
            hist = {"versions": []}
    version = profile.get("profile_version") or f"cp-{_sha(profile)}"
    entry = {
        "profile_version": version,
        "saved_at": _utc(),
        "build": BUILD,
        "snapshot": {k: v for k, v in profile.items() if k != "raw_eligibility_profile"},
    }
    hist.setdefault("versions", []).append(entry)
    hist["versions"] = hist["versions"][-50:]
    PROFILE_HISTORY.parent.mkdir(parents=True, exist_ok=True)
    PROFILE_HISTORY.write_text(json.dumps(hist, indent=2, default=str), encoding="utf-8")
    return version


def cage_status(profile: dict[str, Any]) -> dict[str, Any]:
    cage = str(profile.get("CAGE") or "").upper()
    if cage in ("", "UNKNOWN", "PENDING", "VALIDATION"):
        state = "PENDING" if cage in ("PENDING", "VALIDATION") else "UNKNOWN"
        return {
            "status": state,
            "cage": cage or None,
            "dibbs_eligible": False,
            "block": "DIBBS_CAGE_REQUIRED",
            "plain": "CAGE not active — DIBBS unavailable",
        }
    sam = str(profile.get("SAM_registration_status") or "").upper()
    if sam in ("ACTIVE", "TRUE", "YES", "1"):
        return {
            "status": "ACTIVE",
            "cage": cage,
            "dibbs_eligible": True,
            "block": None,
            "plain": "CAGE present + SAM active",
        }
    return {
        "status": "UNKNOWN",
        "cage": cage,
        "dibbs_eligible": False,
        "block": "SAM_OR_CAGE_UNVERIFIED",
        "plain": "CAGE value present but SAM/CAGE not verified ACTIVE",
    }


def set_aside_decision(profile: dict[str, Any], set_aside_raw: str | None) -> dict[str, Any]:
    """Reuse company_eligibility.set_aside_eligibility — do not invent certs."""
    from company_eligibility import set_aside_eligibility

    result = set_aside_eligibility(set_aside_raw)
    eligible = result.get("eligible")
    if eligible is True:
        status = "PASS_VERIFIED"
        if profile.get("small_business_status") in (None, UNKNOWN, "UNKNOWN"):
            # Env may hold SB but size status not verified in profile
            status = "REVIEW_REQUIRED"
        return {
            **result,
            "status": status,
            "set_aside": set_aside_raw,
            "plain": "Set-aside allowed by held certifications",
        }
    if eligible is False:
        return {
            **result,
            "status": "FAIL",
            "set_aside": set_aside_raw,
            "plain": f"Not eligible for {result.get('matched')}",
        }
    return {
        **result,
        "status": "UNKNOWN",
        "set_aside": set_aside_raw,
        "plain": "Set-aside eligibility unknown",
    }


def new_sam_snapshot(**fields: Any) -> dict[str, Any]:
    """Operator/cached SAM snapshot only — never live API in R3 tests."""
    return {
        "kind": "SAMComplianceSnapshot",
        "snapshot_id": new_id("SAM"),
        "uei": fields.get("uei"),
        "cage": fields.get("cage"),
        "registration_status": fields.get("registration_status") or UNKNOWN,
        "expiration": fields.get("expiration"),
        "entity_name": fields.get("entity_name"),
        "physical_address": fields.get("physical_address"),
        "reps_certs_date": fields.get("reps_certs_date"),
        "naics": fields.get("naics") or [],
        "size_representations": fields.get("size_representations"),
        "exclusions": fields.get("exclusions") or UNKNOWN,
        "source": fields.get("source") or "CACHED_OR_OWNER",
        "retrieved_at": fields.get("retrieved_at") or _utc(),
        "live_sam_api": False,
    }


def load_attestation_library() -> dict[str, Any]:
    if not ATTESTATION_LIBRARY.exists():
        return {"kind": "CompanyAttestationLibrary", "entries": []}
    try:
        return json.loads(ATTESTATION_LIBRARY.read_text(encoding="utf-8"))
    except Exception:
        return {"kind": "CompanyAttestationLibrary", "entries": []}


def save_attestation_library(lib: dict[str, Any]) -> None:
    ATTESTATION_LIBRARY.parent.mkdir(parents=True, exist_ok=True)
    ATTESTATION_LIBRARY.write_text(json.dumps(lib, indent=2, default=str), encoding="utf-8")
