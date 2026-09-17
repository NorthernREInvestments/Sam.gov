"""Editable company capability / eligibility profile.

Capabilities are facts or policies with provenance — never inferred as held.
Statuses: VERIFIED | UNKNOWN | NOT_HELD | POLICY
"""

from __future__ import annotations
from application_clock import now_utc

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from data_integrity import STATUS_POLICY, STATUS_UNKNOWN, STATUS_VERIFIED

CAP_VERIFIED = STATUS_VERIFIED
CAP_UNKNOWN = STATUS_UNKNOWN
CAP_NOT_HELD = "NOT_HELD"
CAP_POLICY = STATUS_POLICY

CAPABILITY_STATUSES = frozenset({CAP_VERIFIED, CAP_UNKNOWN, CAP_NOT_HELD, CAP_POLICY})

# Keys used for startup / eligibility comparisons
KEY_CERTIFICATIONS = "certifications"
KEY_BONDING_CAPACITY = "bonding_capacity"
KEY_SPECIAL_LICENSES = "special_licenses"
KEY_SUPPLIER_CREDIT = "established_supplier_credit"
KEY_FEDERAL_PAST_PERFORMANCE = "federal_past_performance"
KEY_PG_ALLOWED = "personal_guarantee_allowed"
KEY_PERSONAL_CREDIT_ALLOWED = "personal_credit_allowed"
KEY_PERSONAL_CASH_ALLOWED = "personal_cash_upfront_allowed"
KEY_MIN_ACTUAL_PROFIT = "minimum_actual_profit"

DEFAULT_STARTUP_PROFILE: dict[str, dict[str, Any]] = {
    KEY_CERTIFICATIONS: {
        "value": None,
        "status": CAP_NOT_HELD,
        "held": False,
        "notes": "NONE VERIFIED — startup has no verified certifications",
    },
    KEY_BONDING_CAPACITY: {
        "value": None,
        "status": CAP_NOT_HELD,
        "held": False,
        "notes": "NONE VERIFIED",
    },
    KEY_SPECIAL_LICENSES: {
        "value": None,
        "status": CAP_NOT_HELD,
        "held": False,
        "notes": "NONE VERIFIED",
    },
    KEY_SUPPLIER_CREDIT: {
        "value": None,
        "status": CAP_NOT_HELD,
        "held": False,
        "notes": "NONE VERIFIED",
    },
    KEY_FEDERAL_PAST_PERFORMANCE: {
        "value": None,
        "status": CAP_NOT_HELD,
        "held": False,
        "notes": "NONE VERIFIED for new operating company",
    },
    KEY_PG_ALLOWED: {
        "value": True,
        "status": CAP_POLICY,
        "held": True,
        "notes": "POLICY — PG allowed only as OPERATOR RISK DECISION; never auto-accepted; not automatic reject",
    },
    KEY_PERSONAL_CREDIT_ALLOWED: {
        "value": False,
        "status": CAP_POLICY,
        "held": False,
        "notes": "FALSE POLICY — qualifying personal credit as material underwriting gate not assumed satisfied (operator FICO ~480)",
    },
    KEY_PERSONAL_CASH_ALLOWED: {
        "value": False,
        "status": CAP_POLICY,
        "held": False,
        "notes": "FALSE POLICY — $0 personal cash upfront",
    },
    KEY_MIN_ACTUAL_PROFIT: {
        "value": 10000.0,
        "status": CAP_POLICY,
        "held": True,
        "notes": "POLICY minimum actual profit USD",
    },
}


def _now_iso() -> str:
    return now_utc().isoformat()


def capability_record(
    *,
    key: str,
    value: Any = None,
    status: str = CAP_UNKNOWN,
    held: bool | None = None,
    notes: str | None = None,
    source: str | None = None,
    source_url: str | None = None,
    effective_at: str | None = None,
    expires_at: str | None = None,
) -> dict[str, Any]:
    st = str(status or CAP_UNKNOWN).upper()
    if st not in CAPABILITY_STATUSES:
        st = CAP_UNKNOWN
    # UNKNOWN / NOT_HELD never silently become held
    if held is None:
        if st == CAP_VERIFIED:
            held = True
        elif st in {CAP_NOT_HELD, CAP_UNKNOWN}:
            held = False
        elif st == CAP_POLICY:
            held = bool(value) if isinstance(value, bool) else value is not None
        else:
            held = False
    if st == CAP_UNKNOWN:
        held = False
    if st == CAP_NOT_HELD:
        held = False
    return {
        "key": key,
        "value": value,
        "status": st,
        "held": bool(held) if st != CAP_UNKNOWN else False,
        "notes": notes,
        "source": source,
        "source_url": source_url,
        "effective_at": effective_at or _now_iso(),
        "expires_at": expires_at,
        "updated_at": _now_iso(),
    }


def default_startup_profile() -> dict[str, Any]:
    caps = {
        k: capability_record(key=k, **{kk: vv for kk, vv in v.items() if kk != "key"})
        for k, v in DEFAULT_STARTUP_PROFILE.items()
    }
    return {
        "profile_version": "company-capability-v1",
        "company_label": "startup_operating_company",
        "capabilities": caps,
        "LIVE_API_REQUESTS": 0,
    }


def normalize_profile(raw: dict[str, Any] | None) -> dict[str, Any]:
    base = default_startup_profile()
    if not isinstance(raw, dict):
        return base
    caps_in = raw.get("capabilities") if isinstance(raw.get("capabilities"), dict) else raw
    out_caps = dict(base["capabilities"])
    if isinstance(caps_in, dict):
        for key, val in caps_in.items():
            if isinstance(val, dict):
                out_caps[key] = capability_record(
                    key=key,
                    value=val.get("value"),
                    status=str(val.get("status") or CAP_UNKNOWN),
                    held=val.get("held"),
                    notes=val.get("notes"),
                    source=val.get("source"),
                    source_url=val.get("source_url"),
                    effective_at=val.get("effective_at"),
                    expires_at=val.get("expires_at"),
                )
    out = deepcopy(base)
    out["capabilities"] = out_caps
    if raw.get("company_label"):
        out["company_label"] = raw["company_label"]
    return out


def get_capability(profile: dict[str, Any] | None, key: str) -> dict[str, Any]:
    norm = normalize_profile(profile)
    return norm["capabilities"].get(key) or capability_record(key=key, status=CAP_UNKNOWN)


def capability_is_held(profile: dict[str, Any] | None, key: str) -> bool:
    """True only when status is VERIFIED (or POLICY with affirmative value) AND held."""
    cap = get_capability(profile, key)
    st = cap.get("status")
    if st == CAP_UNKNOWN or st == CAP_NOT_HELD:
        return False
    if st == CAP_VERIFIED:
        return bool(cap.get("held"))
    if st == CAP_POLICY:
        # Policy flags like PG_ALLOWED=False are held=False by design
        return bool(cap.get("held")) and bool(cap.get("value"))
    return False


def unknown_never_becomes_held(cap: dict[str, Any]) -> bool:
    """Invariant helper for tests/gates."""
    if str(cap.get("status")) == CAP_UNKNOWN:
        return cap.get("held") is False
    return True


def evaluate_requirement_against_capability(
    *,
    requirement_type: str,
    required: bool,
    profile: dict[str, Any] | None,
    capability_key: str | None = None,
) -> dict[str, Any]:
    """
    Compare a solicitation requirement to current company capability.
    Do not infer certification eligibility from UNKNOWN.
    """
    if not required:
        return {
            "status": "NOT_APPLICABLE",
            "blocker": False,
            "reason": "requirement_not_required",
        }
    key = capability_key
    rtype = str(requirement_type or "").upper()
    if key is None:
        mapping = {
            "CERTIFICATION": KEY_CERTIFICATIONS,
            "BOND": KEY_BONDING_CAPACITY,
            "LICENSE": KEY_SPECIAL_LICENSES,
            "PAST_PERFORMANCE": KEY_FEDERAL_PAST_PERFORMANCE,
        }
        key = mapping.get(rtype)
    if not key:
        return {
            "status": "UNKNOWN",
            "blocker": True,
            "reason": "no_capability_mapping",
            "capability_key": None,
        }
    cap = get_capability(profile, key)
    st = cap.get("status")
    if st == CAP_VERIFIED and cap.get("held"):
        return {
            "status": "SATISFIED",
            "blocker": False,
            "reason": "capability_verified_held",
            "capability_key": key,
            "capability": cap,
        }
    if st == CAP_NOT_HELD:
        return {
            "status": "UNSATISFIED",
            "blocker": True,
            "reason": "capability_not_held",
            "capability_key": key,
            "capability": cap,
        }
    # UNKNOWN — never treat as held
    return {
        "status": "UNKNOWN",
        "blocker": True,
        "reason": "capability_unknown_not_assumed_held",
        "capability_key": key,
        "capability": cap,
    }


def load_company_profile(session: Any | None = None) -> dict[str, Any]:
    """Load from DB rows if present; else default startup profile."""
    if session is None:
        return default_startup_profile()
    try:
        from models import CompanyCapability

        rows = session.query(CompanyCapability).all()
        if not rows:
            return default_startup_profile()
        caps = {}
        for row in rows:
            caps[row.capability_key] = capability_record(
                key=row.capability_key,
                value=row.value_json,
                status=row.status or CAP_UNKNOWN,
                held=row.held,
                notes=row.notes,
                source=row.source,
                source_url=row.source_url,
                effective_at=row.effective_at.isoformat() if row.effective_at else None,
                expires_at=row.expires_at.isoformat() if row.expires_at else None,
            )
        return normalize_profile({"capabilities": caps})
    except Exception:
        return default_startup_profile()


def ensure_default_company_profile(session: Any) -> dict[str, Any]:
    """Persist default startup capabilities if table empty (editable later)."""
    from models import CompanyCapability

    existing = session.query(CompanyCapability).count()
    if existing:
        return load_company_profile(session)
    profile = default_startup_profile()
    for key, cap in profile["capabilities"].items():
        session.add(
            CompanyCapability(
                capability_key=key,
                value_json=cap.get("value"),
                status=cap.get("status"),
                held=bool(cap.get("held")),
                notes=cap.get("notes"),
                source="default_startup_seed",
                source_url=None,
            )
        )
    session.flush()
    return load_company_profile(session)


def upsert_capability(session: Any, key: str, **fields: Any) -> Any:
    """Operator/safe mutation — never triggers paid AI."""
    from models import CompanyCapability

    row = session.query(CompanyCapability).filter_by(capability_key=key).first()
    if row is None:
        row = CompanyCapability(capability_key=key)
        session.add(row)
    rec = capability_record(key=key, **fields)
    row.value_json = rec["value"]
    row.status = rec["status"]
    row.held = bool(rec["held"])
    row.notes = rec.get("notes")
    row.source = rec.get("source")
    row.source_url = rec.get("source_url")
    session.flush()
    return row
