"""Forms, registration, submission instructions, evaluation basis — no outreach."""

from __future__ import annotations

import re
from typing import Any

from bid_compliance_constants import FUTURE_ACTION

PORTALS = (
    ("SAM.gov", r"\bsam\.gov\b"),
    ("Bonfire", r"\bbonfire\b"),
    ("BidNet", r"\bbidnet\b"),
    ("OpenGov", r"\bopengov\b"),
    ("IonWave", r"\bionwave\b"),
    ("JAGGAER", r"\bjaggaer|sciquest\b"),
    ("PlanetBids", r"\bplanetbids\b"),
    ("Public Purchase", r"\bpublic\s+purchase\b"),
)

FORM_PATTERNS = (
    ("pricing_sheet", r"\bpricing\s+(?:sheet|schedule)\b", "REQUIRED"),
    ("bid_schedule", r"\bbid\s+schedule\b", "REQUIRED"),
    ("representations_certs", r"\brepresentations?\s+and\s+certifications?\b", "REQUIRED"),
    ("w9", r"\bw[\s-]?9\b", "CONDITIONAL"),
    ("non_collusion", r"\bnon[\s-]*collusion\b", "REQUIRED"),
    ("lobbying", r"\blobbying\s+certification\b", "CONDITIONAL"),
    ("debarment", r"\bdebarment\b", "REQUIRED"),
    ("amendment_ack", r"\bamendment\s+acknowledg", "REQUIRED"),
    ("signature_page", r"\bsignature\s+page\b|\bauthorized\s+signature\b", "REQUIRED"),
    ("notarized_affidavit", r"\bnotar(?:y|ized)\b", "CONDITIONAL"),
    ("vendor_info", r"\bvendor\s+information\s+form\b", "OPTIONAL"),
    ("conflict_disclosure", r"\bconflict\s+(?:of\s+interest\s+)?disclosure\b", "CONDITIONAL"),
)


def extract_registration_requirements(
    text: str | None,
    *,
    known_registrations: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Identify portals/registrations. Never registers during DEVELOPMENT_NO_OUTREACH."""
    t = text or ""
    known = known_registrations or {}
    items = []
    for name, pat in PORTALS:
        if re.search(pat, t, re.I):
            status_known = known.get(name)
            if status_known is True:
                reg_status = "ALREADY_REGISTERED"
            elif status_known is False:
                reg_status = "NOT_REGISTERED"
            else:
                reg_status = "REGISTRATION_STATUS_UNKNOWN"
            items.append(
                {
                    "portal": name,
                    "requirement": "REQUIRED",
                    "registration_status": reg_status,
                    "operator_action": FUTURE_ACTION if reg_status != "ALREADY_REGISTERED" else None,
                    "autonomous_registration": False,
                }
            )
    # Generic SAM / state vendor if mentioned without brand portal
    if re.search(r"\bstate\s+vendor\s+registration\b", t, re.I):
        items.append(
            {
                "portal": "STATE_VENDOR",
                "requirement": "REQUIRED",
                "registration_status": known.get("STATE_VENDOR") and "ALREADY_REGISTERED" or "REGISTRATION_STATUS_UNKNOWN",
                "operator_action": FUTURE_ACTION,
                "autonomous_registration": False,
            }
        )
    return {
        "kind": "RegistrationRequirements",
        "items": items,
        "autonomous_registration_performed": False,
        "note": "DEVELOPMENT_NO_OUTREACH — record FUTURE_ACTION_IF_PURSUED only",
    }


def extract_required_forms(text: str | None) -> dict[str, Any]:
    t = text or ""
    forms = []
    for key, pat, req in FORM_PATTERNS:
        if re.search(pat, t, re.I):
            signature = bool(re.search(r"sign", key) or key == "signature_page")
            notary = key == "notarized_affidavit" or bool(re.search(r"notar", pat))
            forms.append(
                {
                    "form_key": key,
                    "requirement": req,
                    "form_status": "FORM_MISSING",  # presence in package unknown unless caller fills
                    "completable_from_known_data": False,
                    "operator_input_required": True,
                    "signature_required": signature or key in {"reps_certs", "non_collusion", "amendment_ack"},
                    "notary_required": notary,
                    "operator_action": FUTURE_ACTION,
                }
            )
    return {
        "kind": "RequiredForms",
        "forms": forms,
        "signed": False,
        "submitted": False,
        "note": "Do not sign or submit",
    }


def extract_submission_instructions(
    text: str | None,
    *,
    deadline: str | None = None,
    timezone: str | None = None,
    method: str | None = None,
) -> dict[str, Any]:
    """Never silently infer timezone."""
    t = text or ""
    tz = timezone
    if tz is None:
        m = re.search(r"\b(UTC|GMT|[ECMP][DS]?T|Central|Eastern|Pacific|Mountain)\b", t, re.I)
        tz = m.group(1) if m else None  # explicit None if not found — do not guess

    dl = deadline
    if dl is None:
        m = re.search(
            r"(?:bid|offer|proposal|response)\s+(?:due|deadline|closing)[:\s]+([^\n]{5,80})",
            t,
            re.I,
        )
        if m:
            dl = m.group(1).strip()
            if tz is None:
                tm = re.search(r"\b(UTC|GMT|[ECMP][DS]?T)\b", dl, re.I)
                if tm:
                    tz = tm.group(1)

    meth = method
    portal = None
    email = None
    address = None
    if meth is None:
        for name, pat in PORTALS:
            if re.search(pat, t, re.I) and re.search(r"\bsubmit\b", t, re.I):
                meth = "PORTAL"
                portal = name
                break
        if meth is None and re.search(r"\bsubmit\b.{0,60}\bemail\b", t, re.I):
            meth = "EMAIL"
            em = re.search(r"([\w.+-]+@[\w.-]+)", t)
            email = em.group(1) if em else None
        elif meth is None and re.search(r"\bsubmit\b.{0,60}\b(portal|online|electronically|via)\b", t, re.I):
            meth = "PORTAL"
            for name, pat in PORTALS:
                if re.search(pat, t, re.I):
                    portal = name
                    break
        elif meth is None and re.search(r"\bhand[\s-]*deliver|sealed\s+bid|mail\s+to\b", t, re.I):
            meth = "PHYSICAL"
            am = re.search(r"(?:deliver|mail)\s+to[:\s]+([^\n]{10,120})", t, re.I)
            address = am.group(1).strip() if am else None

    formats = []
    for fmt in ("PDF", "XLSX", "XLS", "DOCX", "DOC"):
        if re.search(rf"\b{fmt}\b", t, re.I):
            formats.append(fmt)

    page_limit = None
    pm = re.search(r"\b(?:maximum|max|not\s+to\s+exceed)\s+(\d+)\s+pages?\b", t, re.I)
    if pm:
        page_limit = int(pm.group(1))

    naming = None
    nm = re.search(r"\bfile\s+name(?:\s+must)?[:\s]+([^\n]{5,80})", t, re.I)
    if nm:
        naming = nm.group(1).strip()

    return {
        "kind": "BidSubmissionInstruction",
        "deadline": dl,
        "timezone": tz,
        "timezone_inferred": False,
        "timezone_known": tz is not None,
        "submission_method": meth,
        "portal": portal,
        "email": email,
        "physical_address": address,
        "accepted_formats": formats,
        "page_limit": page_limit,
        "naming_convention": naming,
        "copies": None,
        "max_file_size": None,
        "late_bid_rules": "UNKNOWN" if not re.search(r"\blate\b", t, re.I) else "MENTIONED",
        "note": "Timezone never guessed when absent",
    }


def extract_evaluation_basis(text: str | None) -> dict[str, Any]:
    t = text or ""
    factors = []
    basis = None
    if re.search(r"\blowest\s+price\s+technically\s+acceptable\b|\blpta\b", t, re.I):
        basis = "LPTA"
        factors.append("LPTA")
    if re.search(r"\bbest\s+value\b", t, re.I):
        basis = basis or "BEST_VALUE"
        factors.append("BEST_VALUE")
    if re.search(r"\blowest\s+responsive(?:/|\s+and\s+)responsible\b", t, re.I):
        basis = basis or "LOWEST_RESPONSIVE_RESPONSIBLE"
        factors.append("LOWEST_RESPONSIVE_RESPONSIBLE")
    if re.search(r"\btechnical\b.{0,20}\bprice\b|\bprice\b.{0,20}\btechnical\b", t, re.I):
        factors.append("TECHNICAL_PLUS_PRICE")
    if re.search(r"\bpast\s+performance\b.{0,40}\bevaluat", t, re.I):
        factors.append("PAST_PERFORMANCE")
    if re.search(r"\bmultiple\s+award\b", t, re.I):
        factors.append("MULTIPLE_AWARD")
    if re.search(r"\ball[\s-]*or[\s-]*none\b", t, re.I):
        factors.append("ALL_OR_NONE")
    if re.search(r"\bpartial\s+award\b|\bline[\s-]*item\s+award\b", t, re.I):
        factors.append("PARTIAL_OR_LINE_ITEM_AWARD")
    return {
        "kind": "EvaluationBasis",
        "award_basis": basis,
        "factors": factors,
        "source_backed_only": True,
        "invented_preferences": False,
        "note": "Only explicit source-backed factors",
    }


def extract_delivery_logistics(text: str | None) -> dict[str, Any]:
    t = text or ""
    delivery_date = None
    dm = re.search(r"\bdelivery\s+(?:date|within|required)[:\s]+([^\n.]{3,80})", t, re.I)
    if dm:
        delivery_date = dm.group(1).strip()
    days = None
    ddm = re.search(r"\bwithin\s+(\d+)\s+days?\b", t, re.I)
    if ddm:
        days = int(ddm.group(1))
    fob = None
    fm = re.search(r"\bf\.?o\.?b\.?\s+([^\n,]{3,40})", t, re.I)
    if fm:
        fob = fm.group(0).strip()
    dest = None
    dem = re.search(r"\b(?:ship\s+to|destination)[:\s]+([^\n]{5,100})", t, re.I)
    if dem:
        dest = dem.group(1).strip()
    return {
        "kind": "DeliveryLogisticsCompliance",
        "required_delivery_date": delivery_date,
        "delivery_window_days": days,
        "fob_terms": fob,
        "destination": dest,
        "inside_delivery": bool(re.search(r"\binside\s+delivery\b", t, re.I)),
        "liftgate": bool(re.search(r"\blift[\s-]*gate\b", t, re.I)),
        "installation": bool(re.search(r"\binstallation\s+(?:required|shall|must)\b", t, re.I)),
        "packaging": bool(re.search(r"\bpackag", t, re.I)),
        "all_or_none_delivery": bool(re.search(r"\ball[\s-]*or[\s-]*none\b", t, re.I)),
    }


def delivery_invalidates_economics(
    *,
    assumed_lead_time_days: int | None,
    required_delivery_days: int | None,
) -> dict[str, Any]:
    """If economics assumed longer lead time than required, invalidate assumptions."""
    if assumed_lead_time_days is None or required_delivery_days is None:
        return {"invalidated": False, "reason": "insufficient_data"}
    if assumed_lead_time_days > required_delivery_days:
        return {
            "invalidated": True,
            "reason": "delivery_requirement_tighter_than_economic_assumption",
            "assumed_lead_time_days": assumed_lead_time_days,
            "required_delivery_days": required_delivery_days,
            "invalidate": ["supplier_lead_time", "freight_mode", "delivery_viability", "economics"],
        }
    return {"invalidated": False, "reason": "compatible"}
