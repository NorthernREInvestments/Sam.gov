"""Product Identity Resolution Engine — research only.

Convert extracted procurement requirements into validated, researchable
product identities. Never invent manufacturers, part numbers, or equivalents.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from copy import deepcopy
from typing import Any

from application_clock import now_utc
from m3_commercial_engine import (
    AMBIGUOUS_MFR_TOKENS,
    CAGE_RE,
    KNOWN_MANUFACTURERS,
    NSN_RE,
    PART_RE,
)
from m3_procurement_package import (
    MATCH_HIGH,
    MATCH_LOW,
    MATCH_MEDIUM,
    MATCH_UNKNOWN,
    validate_identifier,
)
from m3_supplier_intelligence import MODEL_RE, SKU_RE, UPC_RE

log = logging.getLogger("govtracker.m3_product_identity_resolution")

RESOLUTION_INDEX_KEY = "m3_product_identity_resolution_v1"
LEARNING_INDEX_KEY = "m3_product_identity_learning_v1"

# Identity types
EXACT_IDENTITY = "EXACT_IDENTITY"
SPECIFICATION_IDENTITY = "SPECIFICATION_IDENTITY"
CATEGORY_ONLY = "CATEGORY_ONLY"
UNKNOWN = "UNKNOWN"

# Substitution
EXACT_ONLY = "EXACT_ONLY"
EQUIVALENTS_ALLOWED = "EQUIVALENTS_ALLOWED"
SPECIFICATION_BASED = "SPECIFICATION_BASED"
SUBSTITUTION_UNKNOWN = "UNKNOWN"

# Supplier readiness
READY_FOR_SUPPLIER_SEARCH = "READY_FOR_SUPPLIER_SEARCH"
READY_WITH_SPECIFICATIONS = "READY_WITH_SPECIFICATIONS"
NEEDS_IDENTITY_RESEARCH = "NEEDS_IDENTITY_RESEARCH"
READINESS_UNKNOWN = "UNKNOWN"

# VA queues
Q_NEEDS_PRODUCT_IDENTITY = "NEEDS_PRODUCT_IDENTITY"
Q_NEEDS_SPECIFICATION_RESEARCH = "NEEDS_SPECIFICATION_RESEARCH"
Q_NEEDS_VALIDATION = "NEEDS_VALIDATION"
Q_READY_FOR_SUPPLIER_RESEARCH = "READY_FOR_SUPPLIER_RESEARCH"

# Mission categories
CAT_IT = "IT_EQUIPMENT"
CAT_INDUSTRIAL = "INDUSTRIAL_COMPONENTS"
CAT_CONSUMABLES = "CONSUMABLES"
CAT_SAFETY = "SAFETY_EQUIPMENT"
CAT_TOOLS = "TOOLS"
CAT_VEHICLE = "VEHICLE_PARTS"
CAT_MEDICAL = "MEDICAL"
CAT_CONSTRUCTION = "CONSTRUCTION_MATERIALS"
CAT_OTHER = "OTHER"

CATEGORY_RULES: list[tuple[str, re.Pattern[str]]] = [
    (CAT_IT, re.compile(
        r"\b(laptop|desktop|computer|server|switch|router|printer|monitor|tablet|"
        r"network|storage\s+array|chromebook|workstation)\b", re.I)),
    (CAT_MEDICAL, re.compile(
        r"\b(medical|hospital|syringe|wheelchair|patient|pharmaceutical|surgical|"
        r"diagnostic|ambulance\s+equipment)\b", re.I)),
    (CAT_SAFETY, re.compile(
        r"\b(ppe|safety\s+(?:vest|gear|equipment)|hard\s+hat|respirator|"
        r"fire\s+extinguisher|protective)\b", re.I)),
    (CAT_TOOLS, re.compile(
        r"\b(hand\s+tools?|power\s+tools?|wrench|drill|saw|tool\s+set)\b", re.I)),
    (CAT_VEHICLE, re.compile(
        r"\b(vehicle\s+part|brake|tire|filter|belt|automotive|truck\s+part|"
        r"snow/?ice|plow\s+blade)\b", re.I)),
    (CAT_CONSTRUCTION, re.compile(
        r"\b(lumber|concrete|asphalt|roofing|drywall|steel\s+beam|aggregate|"
        r"building\s+material)\b", re.I)),
    (CAT_CONSUMABLES, re.compile(
        r"\b(consumable|seed|fertilizer|paper\s+towel|toner|ink|janitorial|"
        r"cleaning\s+supplies|office\s+supplies)\b", re.I)),
    (CAT_INDUSTRIAL, re.compile(
        r"\b(blade|carbide|industrial|pump|motor|compressor|bearing|valve|"
        r"component|machinery|equipment\s+package|maintenance\s+component)\b", re.I)),
]

CLIN_RE = re.compile(r"\bCLIN[-_\s]?\d+\b", re.I)
PAGE_RE = re.compile(r"\b(?:page|pg\.?)\s*\d+\b", re.I)
BRAND_REQUIRED_RE = re.compile(
    r"\b(brand\s+name\s+only|no\s+substitut|exact\s+match\s+required|"
    r"or\s+equal\s+not\s+accepted|must\s+be\s+[A-Z][a-z]+|"
    r"OEM\s+only|no\s+equivalents?)\b",
    re.I,
)
EQUIVALENTS_ALLOWED_RE = re.compile(
    r"\b(or\s+equal|or\s+equivalent|approved\s+equal|brand\s+name\s+or\s+equal|"
    r"equivalent\s+products?\s+accepted|substitutions?\s+allowed)\b",
    re.I,
)
SPEC_CONTROLLING_RE = re.compile(
    r"\b(per\s+specification|conforming\s+to|must\s+meet\s+(?:the\s+)?(?:following\s+)?spec|"
    r"salient\s+characteristics?|minimum\s+specifications?)\b",
    re.I,
)

DIM_RE = re.compile(
    r"\b(\d+(?:\.\d+)?\s*(?:\"|in|inch|inches|mm|cm|ft|feet)\s*"
    r"(?:[x×]\s*\d+(?:\.\d+)?\s*(?:\"|in|inch|inches|mm|cm|ft|feet))?)\b",
    re.I,
)
MATERIAL_RE = re.compile(
    r"\b(tungsten[\s-]?carbide|carbide|stainless\s+steel|carbon\s+steel|aluminum|"
    r"brass|copper|polyethylene|PVC|rubber|nylon|kevlar|leather|brass)\b",
    re.I,
)
STANDARD_RE = re.compile(
    r"\b((?:ASTM|ANSI|ISO|MIL[\s-]?STD|NFPA|UL|SAE)[\s-]?\d*[A-Z0-9\-]*)\b",
    re.I,
)
APPLICATION_RE = re.compile(
    r"\b(?:for|used\s+for|application[:\s]+|intended\s+for)\s+([A-Za-z0-9 ,/\-]{4,60})",
    re.I,
)
FEATURE_RE = re.compile(
    r"\b(required\s+features?|must\s+include|shall\s+include|equipped\s+with)"
    r"[:\s]+([^\n.]{5,120})",
    re.I,
)
COMPAT_RE = re.compile(
    r"\b(compatible\s+with|fits|mounts?\s+to|for\s+use\s+with)\s+([A-Za-z0-9 \-/\.]{3,60})",
    re.I,
)
OEM_REF_RE = re.compile(
    r"\b(?:OEM|manufacturer|mfr|made\s+by|brand)[:\s]+([A-Z][A-Za-z0-9 &\-]{2,40})",
    re.I,
)
CATALOG_RE = re.compile(
    r"\b(?:catalog|catalogue)\s*(?:#|no\.?|number)?[:\s]*([A-Z0-9\-]{3,24})\b",
    re.I,
)

GENERIC_IDENTITY_WORDS = frozenset(
    {
        "names",
        "name",
        "numbers",
        "number",
        "information",
        "details",
        "description",
        "specifications",
        "specification",
        "requirements",
        "requirement",
        "products",
        "product",
        "items",
        "item",
        "vendor",
        "vendors",
        "contractor",
        "contractors",
        "supplier",
        "suppliers",
        "manufacturer",
        "manufacturers",
        "brand",
        "brands",
        "model",
        "models",
        "see",
        "above",
        "below",
        "attached",
        "attachment",
        "section",
        "page",
        "table",
        "list",
        "following",
        "herein",
        "thereof",
        "general",
        "standard",
        "various",
        "other",
        "unknown",
        "tbd",
        "n/a",
        "na",
        "none",
        "same",
        "as",
        "specified",
    }
)

VA_ALLOWED_ACTIONS = frozenset(
    {
        "RESEARCH_IDENTITY",
        "ATTACH_EVIDENCE",
        "UPDATE_NOTES",
        "NOTE",
        "VALIDATE_EXTRACTION",
        "FLAG_NEEDS_RESEARCH",
        "UPDATE_STATUS",
    }
)
VA_FORBIDDEN_ACTIONS = frozenset(
    {
        "INVENT_IDENTITY",
        "APPROVE_SUBSTITUTION",
        "CONTACT_SUPPLIER",
        "SUBMIT_BID",
        "CHANGE_SCORING",
        "APPROVE_DEAL",
        "SPEND_MONEY",
    }
)


def _utc() -> str:
    return now_utc().isoformat()


def _known(v: Any) -> bool:
    return v not in {None, "", "UNKNOWN", "unknown"}


def _num(v: Any) -> float | None:
    if v is None or v == "" or v == "UNKNOWN":
        return None
    try:
        return float(str(v).replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return None


def load_resolution_index() -> dict[str, Any]:
    try:
        from models import AppSetting, session_scope

        with session_scope() as session:
            row = session.get(AppSetting, RESOLUTION_INDEX_KEY)
            if row and isinstance(row.value, dict):
                return deepcopy(row.value)
    except Exception:
        log.debug("identity resolution index load failed", exc_info=True)
    return {"kind": "M3ProductIdentityResolutionIndex", "by_id": {}, "updated_at": None}


def save_resolution_index(index: dict[str, Any]) -> None:
    index = deepcopy(index)
    index["kind"] = "M3ProductIdentityResolutionIndex"
    index["updated_at"] = _utc()
    try:
        from models import AppSetting, session_scope

        with session_scope() as session:
            row = session.get(AppSetting, RESOLUTION_INDEX_KEY)
            if row is None:
                session.add(AppSetting(key=RESOLUTION_INDEX_KEY, value=index))
            else:
                row.value = index
    except Exception:
        log.debug("identity resolution index save failed", exc_info=True)


def load_learning_index() -> dict[str, Any]:
    try:
        from models import AppSetting, session_scope

        with session_scope() as session:
            row = session.get(AppSetting, LEARNING_INDEX_KEY)
            if row and isinstance(row.value, dict):
                return deepcopy(row.value)
    except Exception:
        log.debug("identity learning load failed", exc_info=True)
    return {
        "kind": "M3ProductIdentityLearning",
        "resolved_exact": 0,
        "resolved_spec": 0,
        "unresolved_categories": {},
        "manufacturers_found": {},
        "common_descriptions": {},
        "portal_success": {},
        "updated_at": None,
    }


def save_learning_index(index: dict[str, Any]) -> None:
    index = deepcopy(index)
    index["kind"] = "M3ProductIdentityLearning"
    index["updated_at"] = _utc()
    try:
        from models import AppSetting, session_scope

        with session_scope() as session:
            row = session.get(AppSetting, LEARNING_INDEX_KEY)
            if row is None:
                session.add(AppSetting(key=LEARNING_INDEX_KEY, value=index))
            else:
                row.value = index
    except Exception:
        log.debug("identity learning save failed", exc_info=True)


def reject_false_identifier(identifier: Any, *, kind: str = "part_number") -> dict[str, Any]:
    """Stricter false-ID gate including CLIN / page numbers / boilerplate words."""
    raw = str(identifier or "").strip()
    base = validate_identifier(raw, source="identity_resolution", kind=kind)
    if not base.get("accepted"):
        return base
    extra: list[str] = []
    if CLIN_RE.search(raw) or raw.upper().startswith("CLIN"):
        extra.append("clin_number")
    if PAGE_RE.fullmatch(raw) or re.fullmatch(r"\d{1,3}", raw):
        extra.append("page_or_small_number")
    if re.fullmatch(r"\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}", raw):
        extra.append("date_pattern")
    if raw.lower().strip(".:;,") in GENERIC_IDENTITY_WORDS:
        extra.append("generic_boilerplate_word")
    # Catalog/item codes that are only digits under 5 chars are weak
    if kind == "part_number" and re.fullmatch(r"\d{1,5}", raw):
        extra.append("short_numeric_code")
    if extra:
        return {
            **base,
            "accepted": False,
            "Validation_status": "REJECTED",
            "rejection_reasons": list(base.get("rejection_reasons") or []) + extra,
            "Confidence": MATCH_UNKNOWN,
        }
    return base


def _extract_manufacturer(text: str, *, allow_ambiguous: bool = False) -> tuple[str, str]:
    """Return (manufacturer, evidence_source) — never invent."""
    blob = text or ""
    oem = OEM_REF_RE.search(blob)
    if oem:
        name = oem.group(1).strip(" .:;,")
        if name.lower() in GENERIC_IDENTITY_WORDS:
            name = ""
        if name:
            for known in KNOWN_MANUFACTURERS:
                if known.lower() in name.lower() or name.lower() in known.lower():
                    if known in AMBIGUOUS_MFR_TOKENS and not allow_ambiguous:
                        break
                    return known, "oem_reference"
            # Free-text OEM only if multi-token or known-length proper noun (not single generic)
            if len(name) >= 3 and " " in name and not CLIN_RE.search(name):
                return name[:60], "oem_reference"
            if len(name) >= 4 and name[:1].isupper() and name.lower() not in GENERIC_IDENTITY_WORDS:
                # Single token: only accept if in known list (already checked) — else skip
                pass

    for known in KNOWN_MANUFACTURERS:
        if known in AMBIGUOUS_MFR_TOKENS and not allow_ambiguous:
            if not re.search(rf"\b(?:OEM|manufacturer|mfr|brand)[:\s]+{re.escape(known)}\b", blob, re.I):
                continue
        # Avoid matching manufacturer names inside unrelated boilerplate lists
        if re.search(rf"\b{re.escape(known)}\b", blob, re.I):
            # Skip if only appears as software/meta chrome
            if known in {"Microsoft", "Adobe", "Oracle"} and not re.search(
                rf"\b(?:OEM|manufacturer|mfr|brand|product)[:\s]+{re.escape(known)}\b",
                blob,
                re.I,
            ):
                continue
            return known, "manufacturer_name_in_evidence"
    return "UNKNOWN", "UNKNOWN"


def classify_resolution_category(description: str, title: str = "") -> dict[str, Any]:
    """Phase 4 — product category for supplier research guidance."""
    blob = f"{title} {description}".strip()
    if not blob:
        return {"Category": CAT_OTHER, "Confidence": MATCH_LOW, "evidence": "empty"}
    for cat, pat in CATEGORY_RULES:
        m = pat.search(blob)
        if m:
            return {
                "Category": cat,
                "Confidence": MATCH_MEDIUM if len(m.group(0)) > 4 else MATCH_LOW,
                "evidence": m.group(0),
            }
    return {"Category": CAT_OTHER, "Confidence": MATCH_LOW, "evidence": "no_rule_match"}


def build_specification_profile(description: str, evidence_text: str = "") -> dict[str, Any]:
    """Phase 3 — SPECIFICATION_PROFILE when exact identity is absent."""
    blob = f"{description}\n{evidence_text}"[:8000]
    materials = list({m.group(1) for m in MATERIAL_RE.finditer(blob)})[:8]
    dimensions = list({m.group(1).strip() for m in DIM_RE.finditer(blob)})[:8]
    standards = list({m.group(1) for m in STANDARD_RE.finditer(blob)})[:8]
    applications = []
    for m in APPLICATION_RE.finditer(blob):
        app = m.group(1).strip()[:80]
        if re.search(
            r"\b(bids?|project|solicitation|contract|proposal|prerequisite|award)\b",
            app,
            re.I,
        ):
            continue
        applications.append(app)
        if len(applications) >= 5:
            break
    features = []
    for m in FEATURE_RE.finditer(blob):
        feat = m.group(2).strip()[:120]
        if re.search(
            r"\b(bid\s+bond|prerequisite|proposal|submit|login|registration)\b",
            feat,
            re.I,
        ):
            continue
        features.append(feat)
        if len(features) >= 5:
            break
    compatibility = []
    for m in COMPAT_RE.finditer(blob):
        compatibility.append(m.group(0).strip()[:100])
        if len(compatibility) >= 5:
            break

    # Resolved readable name from description nouns — not a manufacturer invent
    resolved = re.sub(r"\s+", " ", (description or "").strip())[:120] or "UNKNOWN"
    # Light normalization of generic openers
    resolved = re.sub(r"^(purchase|procurement|supply|furnish)\s+(of\s+)?", "", resolved, flags=re.I).strip()
    if not resolved:
        resolved = "UNKNOWN"

    # Product-relevant signals only (materials/dims/standards/compat outweigh process text)
    hard_signals = sum(1 for x in (materials, dimensions, standards, compatibility) if x)
    soft_signals = sum(1 for x in (applications, features) if x)
    spec_count = hard_signals + soft_signals
    if hard_signals >= 2 or (hard_signals >= 1 and soft_signals >= 1):
        conf = MATCH_HIGH if hard_signals >= 2 else MATCH_MEDIUM
    elif hard_signals == 1 or (resolved != "UNKNOWN" and len(resolved) > 20 and soft_signals >= 1):
        conf = MATCH_MEDIUM
    elif resolved != "UNKNOWN" and len(resolved) > 12:
        conf = MATCH_LOW
    else:
        conf = MATCH_LOW

    well_defined = conf in {MATCH_HIGH, MATCH_MEDIUM} and resolved != "UNKNOWN" and hard_signals >= 1

    return {
        "kind": "SPECIFICATION_PROFILE",
        "Resolved_product_name": resolved if _known(resolved) else "UNKNOWN",
        "Dimensions": dimensions or "UNKNOWN",
        "Materials": materials or "UNKNOWN",
        "Performance_requirements": features or "UNKNOWN",
        "Compatibility": compatibility or "UNKNOWN",
        "Application": applications or "UNKNOWN",
        "Standards": standards or "UNKNOWN",
        "Certifications": [s for s in standards if re.search(r"UL|NFPA|ISO", s, re.I)] or "UNKNOWN",
        "Operating_requirements": "UNKNOWN",
        "Required_features": features or "UNKNOWN",
        "Confidence": conf,
        "spec_signal_count": spec_count,
        "well_defined": well_defined,
    }


def build_substitution_profile(evidence_text: str, identity_type: str) -> dict[str, Any]:
    """Phase 5 — never assume substitution is allowed."""
    blob = evidence_text or ""
    if BRAND_REQUIRED_RE.search(blob):
        status = EXACT_ONLY
        evidence = BRAND_REQUIRED_RE.search(blob).group(0)
        conf = MATCH_HIGH
    elif EQUIVALENTS_ALLOWED_RE.search(blob):
        status = EQUIVALENTS_ALLOWED
        evidence = EQUIVALENTS_ALLOWED_RE.search(blob).group(0)
        conf = MATCH_HIGH
    elif SPEC_CONTROLLING_RE.search(blob) or identity_type == SPECIFICATION_IDENTITY:
        status = SPECIFICATION_BASED
        m = SPEC_CONTROLLING_RE.search(blob)
        evidence = m.group(0) if m else "specification_identity_without_brand_rule"
        conf = MATCH_MEDIUM if m else MATCH_LOW
    else:
        status = SUBSTITUTION_UNKNOWN
        evidence = "no_substitution_language_found"
        conf = MATCH_UNKNOWN

    return {
        "kind": "PRODUCT_SUBSTITUTION_PROFILE",
        "Status": status,
        "Exact_brand_required": status == EXACT_ONLY,
        "Equivalents_allowed": status == EQUIVALENTS_ALLOWED,
        "Substitutions_restricted": status == EXACT_ONLY,
        "Specifications_controlling": status == SPECIFICATION_BASED,
        "Evidence": evidence,
        "Confidence": conf,
        "notes": ["never_assume_substitution_allowed"],
    }


def build_supplier_research_readiness(
    *,
    identity_type: str,
    manufacturer: str,
    part_number: str,
    model: str,
    nsn: str,
    spec: dict[str, Any],
    substitution: dict[str, Any],
    category: str,
) -> dict[str, Any]:
    """Phase 6 — SUPPLIER_RESEARCH_READINESS_SCORE."""
    mfr_ok = _known(manufacturer)
    pn_ok = _known(part_number)
    model_ok = _known(model)
    nsn_ok = _known(nsn)
    spec_ok = bool(spec.get("well_defined"))
    subst_known = substitution.get("Status") not in {SUBSTITUTION_UNKNOWN, None}
    channels = category not in {CAT_OTHER, "UNKNOWN", None}

    flags = {
        "Exact_manufacturer_known": mfr_ok,
        "Part_number_known": pn_ok,
        "Model_known": model_ok,
        "NSN_known": nsn_ok,
        "Specifications_sufficient": spec_ok,
        "Supplier_channels_available": channels,
        "Substitution_rules_known": subst_known,
    }
    score = sum(12 for v in flags.values() if v)
    if pn_ok or nsn_ok:
        score += 20
    if mfr_ok and (pn_ok or model_ok):
        score += 15
    if spec_ok and not (pn_ok or nsn_ok):
        score += 10

    missing = []
    if not mfr_ok:
        missing.append("manufacturer")
    if not pn_ok and not nsn_ok and not model_ok:
        missing.append("part_number_or_model_or_nsn")
    if not spec_ok and identity_type != EXACT_IDENTITY:
        missing.append("detailed_specifications")
    if not subst_known:
        missing.append("substitution_rules")

    if identity_type == EXACT_IDENTITY and (pn_ok or nsn_ok) and mfr_ok:
        status = READY_FOR_SUPPLIER_SEARCH
    elif identity_type == EXACT_IDENTITY and (pn_ok or nsn_ok or (mfr_ok and model_ok)):
        status = READY_FOR_SUPPLIER_SEARCH
    elif identity_type == SPECIFICATION_IDENTITY and spec_ok:
        status = READY_WITH_SPECIFICATIONS
    elif identity_type in {CATEGORY_ONLY, UNKNOWN} or not (mfr_ok or spec_ok or pn_ok):
        status = NEEDS_IDENTITY_RESEARCH if identity_type != UNKNOWN else READINESS_UNKNOWN
        if identity_type == CATEGORY_ONLY:
            status = NEEDS_IDENTITY_RESEARCH
    else:
        status = NEEDS_IDENTITY_RESEARCH

    return {
        "kind": "SUPPLIER_RESEARCH_READINESS_SCORE",
        "score": min(100, score),
        "status": status,
        "flags": flags,
        "missing": missing,
        "Category": category,
    }


def build_pricing_handoff(profile: dict[str, Any], *, quantity: Any = None) -> dict[str, Any]:
    """Phase 7 — only HIGH confidence exact or well-defined specs."""
    identity_type = profile.get("Identity_type")
    conf = profile.get("Confidence")
    spec = profile.get("SPECIFICATION_PROFILE") or {}
    eligible = False
    reason = "not_eligible"

    if identity_type == EXACT_IDENTITY and conf == MATCH_HIGH:
        eligible = True
        reason = "high_confidence_exact_identity"
    elif identity_type == EXACT_IDENTITY and conf == MATCH_MEDIUM and (
        _known(profile.get("Manufacturer_part_number")) or _known(profile.get("NSN"))
    ):
        eligible = True
        reason = "medium_exact_with_validated_identifier"
    elif identity_type == SPECIFICATION_IDENTITY and spec.get("well_defined") and conf in {
        MATCH_HIGH,
        MATCH_MEDIUM,
    }:
        eligible = True
        reason = "well_defined_specification_profile"
    elif identity_type in {CATEGORY_ONLY, UNKNOWN}:
        eligible = False
        reason = "generic_description_only_do_not_price"

    return {
        "kind": "PRODUCT_PRICING_HANDOFF",
        "eligible": eligible,
        "reason": reason,
        "Product_identity": {
            "Resolved_product_name": profile.get("Resolved_product_name"),
            "Manufacturer": profile.get("Manufacturer"),
            "Manufacturer_part_number": profile.get("Manufacturer_part_number"),
            "Model_number": profile.get("Model_number"),
            "NSN": profile.get("NSN"),
            "Identity_type": identity_type,
            "Confidence": conf,
        },
        "Configuration": profile.get("Specifications"),
        "Quantity": quantity if _num(quantity) is not None else "UNKNOWN",
        "Requirements": spec if identity_type == SPECIFICATION_IDENTITY else "UNKNOWN",
        "Evidence_source": profile.get("Evidence_source"),
        "notes": ["do_not_price_generic_descriptions_only"],
    }


def _line_candidates(row: dict[str, Any]) -> list[dict[str, Any]]:
    items = []
    for src_key in ("line_items", "bom"):
        raw = row.get(src_key)
        if isinstance(raw, list):
            for li in raw:
                if isinstance(li, dict) and (li.get("description") or li.get("part_number")):
                    items.append(li)
    # Document intelligence line items
    di = row.get("document_intelligence_full") if isinstance(row.get("document_intelligence_full"), dict) else {}
    for li in di.get("PROCUREMENT_LINE_ITEMS") or []:
        if isinstance(li, dict):
            items.append(
                {
                    "description": li.get("Description") or li.get("description"),
                    "part_number": li.get("Part_number") or li.get("part_number"),
                    "manufacturer": li.get("Manufacturer") or li.get("manufacturer"),
                    "model": li.get("Model") or li.get("model"),
                    "nsn": li.get("NSN") or li.get("nsn"),
                    "quantity": li.get("Quantity") if li.get("Quantity") != "UNKNOWN" else li.get("quantity"),
                    "unit": li.get("Unit") or li.get("unit"),
                    "source": li.get("Source_document") or "document_intelligence",
                }
            )
    if not items and row.get("title"):
        items.append({"description": row.get("title"), "source": "opportunity_title"})
    # Dedupe by description
    seen: set[str] = set()
    out = []
    for li in items:
        key = str(li.get("description") or "").lower()[:80]
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(li)
        if len(out) >= 25:
            break
    return out


def _evidence_text(row: dict[str, Any], line: dict[str, Any]) -> str:
    parts = [
        str(line.get("description") or ""),
        str(row.get("title") or ""),
        str(row.get("description") or "")[:2000],
    ]
    for key in ("attachment_text", "governing_text", "solicitation_text", "evidence_text_excerpt"):
        if row.get(key):
            parts.append(str(row.get(key))[:3500])
    for d in (row.get("documents") or [])[:8]:
        if isinstance(d, dict):
            parts.append(str(d.get("extracted_text") or d.get("text_preview") or "")[:2500])
    return " ".join(parts)[:12000]


def resolve_line_product_identity(
    row: dict[str, Any],
    line: dict[str, Any],
    *,
    index: int = 1,
) -> dict[str, Any]:
    """Phase 1+2 — PRODUCT_IDENTITY_RESOLUTION_PROFILE for one line."""
    desc = str(line.get("description") or line.get("Description") or "").strip() or "UNKNOWN"
    evidence = _evidence_text(row, line)
    search_blob = f"{desc} {evidence}"[:10000]

    # Exact fields from line first (highest priority)
    pn = reject_false_identifier(line.get("part_number") or line.get("Part_number"), kind="part_number")
    nsn = reject_false_identifier(line.get("nsn") or line.get("NSN"), kind="nsn")
    model = reject_false_identifier(line.get("model") or line.get("model_number") or line.get("Model"), kind="model")
    sku = reject_false_identifier(line.get("sku") or line.get("SKU"), kind="sku")
    cage = reject_false_identifier(line.get("cage") or line.get("CAGE"), kind="cage")

    evidence_sources: list[str] = []
    if line.get("source"):
        evidence_sources.append(str(line.get("source")))

    # Labeled extraction from evidence (priority order)
    if not pn.get("accepted"):
        for m in PART_RE.finditer(search_blob):
            cand = reject_false_identifier(m.group(1), kind="part_number")
            if cand.get("accepted"):
                pn = {**cand, "Source": "technical_or_solicitation_labeled"}
                evidence_sources.append("part_number_label")
                break
    if not pn.get("accepted"):
        for m in CATALOG_RE.finditer(search_blob):
            cand = reject_false_identifier(m.group(1), kind="part_number")
            if cand.get("accepted"):
                pn = {**cand, "Source": "official_catalog"}
                evidence_sources.append("catalog_reference")
                break
    if not nsn.get("accepted"):
        for m in NSN_RE.finditer(search_blob):
            cand = reject_false_identifier(m.group(1), kind="nsn")
            if cand.get("accepted"):
                nsn = {**cand, "Source": "nsn_in_evidence"}
                evidence_sources.append("nsn")
                break
    if not model.get("accepted"):
        for m in MODEL_RE.finditer(search_blob):
            cand = reject_false_identifier(m.group(1), kind="model")
            if cand.get("accepted"):
                model = {**cand, "Source": "model_label"}
                evidence_sources.append("model_label")
                break
    if not sku.get("accepted"):
        for m in SKU_RE.finditer(search_blob):
            cand = reject_false_identifier(m.group(1), kind="sku")
            if cand.get("accepted"):
                sku = {**cand, "Source": "sku_label"}
                evidence_sources.append("sku_label")
                break
    if not cage.get("accepted"):
        for m in CAGE_RE.finditer(search_blob):
            cand = reject_false_identifier(m.group(1), kind="cage")
            if cand.get("accepted"):
                cage = {**cand, "Source": "cage_label"}
                evidence_sources.append("cage_label")
                break

    mfr_line = str(line.get("manufacturer") or line.get("Manufacturer") or "").strip()
    if mfr_line and mfr_line.upper() != "UNKNOWN" and mfr_line.lower() not in GENERIC_IDENTITY_WORDS:
        manufacturer, mfr_src = mfr_line[:60], "line_item_manufacturer"
    else:
        manufacturer, mfr_src = _extract_manufacturer(search_blob)
    if _known(manufacturer) and manufacturer.lower() in GENERIC_IDENTITY_WORDS:
        manufacturer, mfr_src = "UNKNOWN", "UNKNOWN"
    if _known(manufacturer):
        evidence_sources.append(mfr_src)

    category = classify_resolution_category(desc, str(row.get("title") or ""))
    spec = build_specification_profile(desc, evidence)

    has_exact_id = bool(
        (pn.get("accepted") and _known(pn.get("Identifier")))
        or (nsn.get("accepted") and _known(nsn.get("Identifier")))
        or (
            _known(manufacturer)
            and model.get("accepted")
            and _known(model.get("Identifier"))
        )
    )

    if has_exact_id:
        identity_type = EXACT_IDENTITY
        resolved_name = desc if _known(desc) else "UNKNOWN"
        if _known(manufacturer) and model.get("accepted"):
            resolved_name = f"{manufacturer} {model.get('Identifier')}"
        elif _known(manufacturer) and pn.get("accepted"):
            resolved_name = f"{manufacturer} {pn.get('Identifier')}"
        conf = MATCH_HIGH if (
            (pn.get("accepted") or nsn.get("accepted")) and _known(manufacturer)
        ) else MATCH_MEDIUM
    elif spec.get("well_defined"):
        identity_type = SPECIFICATION_IDENTITY
        resolved_name = spec.get("Resolved_product_name") or desc
        conf = spec.get("Confidence") or MATCH_MEDIUM
        evidence_sources.append("specification_extraction")
    elif category.get("Category") not in {CAT_OTHER, "UNKNOWN"} and _known(desc) and len(desc) > 8:
        identity_type = CATEGORY_ONLY
        resolved_name = desc
        conf = MATCH_LOW
        evidence_sources.append("category_classification_only")
    else:
        identity_type = UNKNOWN
        resolved_name = desc if _known(desc) else "UNKNOWN"
        conf = MATCH_UNKNOWN

    # Prefer human description over bare catalog codes for resolved name
    if identity_type == SPECIFICATION_IDENTITY:
        if re.fullmatch(r"[\d\-]+", desc.strip()) or len(desc.strip()) < 6:
            title = str(row.get("title") or "").strip()
            if title:
                resolved_name = title[:120]
                spec = {**spec, "Resolved_product_name": resolved_name}

    substitution = build_substitution_profile(evidence, identity_type)
    readiness = build_supplier_research_readiness(
        identity_type=identity_type,
        manufacturer=manufacturer,
        part_number=pn.get("Identifier") if pn.get("accepted") else "UNKNOWN",
        model=model.get("Identifier") if model.get("accepted") else "UNKNOWN",
        nsn=nsn.get("Identifier") if nsn.get("accepted") else "UNKNOWN",
        spec=spec,
        substitution=substitution,
        category=str(category.get("Category") or CAT_OTHER),
    )

    profile = {
        "kind": "PRODUCT_IDENTITY_RESOLUTION_PROFILE",
        "Opportunity_ID": row.get("canonical_id") or "UNKNOWN",
        "Line_item": index,
        "Original_description": desc,
        "Resolved_product_name": resolved_name if _known(resolved_name) else "UNKNOWN",
        "Manufacturer": manufacturer if _known(manufacturer) else "UNKNOWN",
        "Manufacturer_part_number": pn.get("Identifier") if pn.get("accepted") else "UNKNOWN",
        "Model_number": model.get("Identifier") if model.get("accepted") else "UNKNOWN",
        "SKU": sku.get("Identifier") if sku.get("accepted") else "UNKNOWN",
        "NSN": nsn.get("Identifier") if nsn.get("accepted") else "UNKNOWN",
        "CAGE": cage.get("Identifier") if cage.get("accepted") else "UNKNOWN",
        "Category": category.get("Category") or CAT_OTHER,
        "Specifications": {
            "Materials": spec.get("Materials"),
            "Dimensions": spec.get("Dimensions"),
            "Standards": spec.get("Standards"),
            "Application": spec.get("Application"),
            "Required_features": spec.get("Required_features"),
        },
        "Identity_type": identity_type,
        "Evidence_source": ", ".join(evidence_sources) if evidence_sources else "UNKNOWN",
        "Confidence": conf,
        "SPECIFICATION_PROFILE": spec,
        "PRODUCT_SUBSTITUTION_PROFILE": substitution,
        "SUPPLIER_RESEARCH_READINESS_SCORE": readiness,
        "rejected_identifiers": [
            r
            for r in (pn, nsn, model, sku, cage)
            if isinstance(r, dict) and not r.get("accepted") and _known(r.get("Identifier"))
        ][:10],
        "Quantity": line.get("quantity") if _num(line.get("quantity")) is not None else "UNKNOWN",
        "Timestamp": _utc(),
        "DEVELOPMENT_NO_OUTREACH": True,
    }
    profile["PRICING_HANDOFF"] = build_pricing_handoff(profile, quantity=line.get("quantity"))
    return profile


def assign_identity_queue(profile: dict[str, Any]) -> str:
    readiness = (profile.get("SUPPLIER_RESEARCH_READINESS_SCORE") or {}).get("status")
    identity_type = profile.get("Identity_type")
    if readiness in {READY_FOR_SUPPLIER_SEARCH, READY_WITH_SPECIFICATIONS}:
        return Q_READY_FOR_SUPPLIER_RESEARCH
    if identity_type == SPECIFICATION_IDENTITY and readiness == NEEDS_IDENTITY_RESEARCH:
        return Q_NEEDS_SPECIFICATION_RESEARCH
    if identity_type == EXACT_IDENTITY and profile.get("Confidence") == MATCH_LOW:
        return Q_NEEDS_VALIDATION
    if identity_type in {CATEGORY_ONLY, UNKNOWN}:
        return Q_NEEDS_PRODUCT_IDENTITY
    if readiness == NEEDS_IDENTITY_RESEARCH:
        return Q_NEEDS_PRODUCT_IDENTITY
    return Q_NEEDS_VALIDATION


def update_identity_learning(
    *,
    profile: dict[str, Any],
    portal_family: str,
) -> None:
    idx = load_learning_index()
    itype = profile.get("Identity_type")
    if itype == EXACT_IDENTITY:
        idx["resolved_exact"] = int(idx.get("resolved_exact") or 0) + 1
    elif itype == SPECIFICATION_IDENTITY:
        idx["resolved_spec"] = int(idx.get("resolved_spec") or 0) + 1
    else:
        cat = str(profile.get("Category") or CAT_OTHER)
        unresolved = idx.setdefault("unresolved_categories", {})
        unresolved[cat] = int(unresolved.get(cat) or 0) + 1

    mfr = profile.get("Manufacturer")
    if _known(mfr):
        found = idx.setdefault("manufacturers_found", {})
        found[str(mfr)] = int(found.get(str(mfr)) or 0) + 1

    desc = str(profile.get("Original_description") or "")[:80].lower()
    if desc:
        common = idx.setdefault("common_descriptions", {})
        common[desc] = int(common.get(desc) or 0) + 1
        # Cap growth
        if len(common) > 200:
            idx["common_descriptions"] = dict(sorted(common.items(), key=lambda x: -x[1])[:150])

    portal = idx.setdefault("portal_success", {})
    bucket = portal.setdefault(portal_family or "UNKNOWN", {"attempts": 0, "exact": 0, "spec": 0})
    bucket["attempts"] = int(bucket.get("attempts") or 0) + 1
    if itype == EXACT_IDENTITY:
        bucket["exact"] = int(bucket.get("exact") or 0) + 1
    elif itype == SPECIFICATION_IDENTITY:
        bucket["spec"] = int(bucket.get("spec") or 0) + 1
    portal[portal_family or "UNKNOWN"] = bucket
    save_learning_index(idx)


def resolve_opportunity_product_identities(
    row: dict[str, Any],
    *,
    update_learning: bool = True,
) -> dict[str, Any]:
    """Resolve all line-level product identities for an opportunity."""
    from portal_document_resolver import classify_portal_family

    lines = _line_candidates(row)
    profiles = [
        resolve_line_product_identity(row, li, index=i)
        for i, li in enumerate(lines, start=1)
    ]
    family = classify_portal_family(row)
    if update_learning:
        for p in profiles:
            update_identity_learning(profile=p, portal_family=family)

    queues: dict[str, list] = defaultdict(list)
    for p in profiles:
        q = assign_identity_queue(p)
        queues[q].append(
            {
                "Opportunity": row.get("title"),
                "canonical_id": row.get("canonical_id"),
                "Line_item": p.get("Line_item"),
                "Original_description": p.get("Original_description"),
                "Identity_type": p.get("Identity_type"),
                "Confidence": p.get("Confidence"),
                "Missing": (p.get("SUPPLIER_RESEARCH_READINESS_SCORE") or {}).get("missing") or [],
            }
        )

    exact = sum(1 for p in profiles if p.get("Identity_type") == EXACT_IDENTITY)
    spec = sum(1 for p in profiles if p.get("Identity_type") == SPECIFICATION_IDENTITY)
    cat_only = sum(1 for p in profiles if p.get("Identity_type") == CATEGORY_ONLY)
    unknown = sum(1 for p in profiles if p.get("Identity_type") == UNKNOWN)

    ready_search = sum(
        1
        for p in profiles
        if (p.get("SUPPLIER_RESEARCH_READINESS_SCORE") or {}).get("status") == READY_FOR_SUPPLIER_SEARCH
    )
    ready_spec = sum(
        1
        for p in profiles
        if (p.get("SUPPLIER_RESEARCH_READINESS_SCORE") or {}).get("status") == READY_WITH_SPECIFICATIONS
    )
    needs = sum(
        1
        for p in profiles
        if (p.get("SUPPLIER_RESEARCH_READINESS_SCORE") or {}).get("status") == NEEDS_IDENTITY_RESEARCH
    )

    pricing_eligible = [p for p in profiles if (p.get("PRICING_HANDOFF") or {}).get("eligible")]

    primary = profiles[0] if profiles else None
    return {
        "kind": "M3ProductIdentityResolution",
        "Opportunity_ID": row.get("canonical_id") or "UNKNOWN",
        "Portal_family": family,
        "PRODUCT_IDENTITY_RESOLUTION_PROFILES": profiles,
        "Products_analyzed": len(profiles),
        "Exact_identities": exact,
        "Specification_identities": spec,
        "Category_only": cat_only,
        "Unknown": unknown,
        "Manufacturers_found": sorted(
            {p.get("Manufacturer") for p in profiles if _known(p.get("Manufacturer"))}
        ),
        "Models_found": sorted(
            {p.get("Model_number") for p in profiles if _known(p.get("Model_number"))}
        ),
        "Part_numbers_found": sorted(
            {p.get("Manufacturer_part_number") for p in profiles if _known(p.get("Manufacturer_part_number"))}
        ),
        "NSNs_found": sorted({p.get("NSN") for p in profiles if _known(p.get("NSN"))}),
        "SUPPLIER_READINESS": {
            "Ready_for_supplier_search": ready_search,
            "Ready_with_specifications": ready_spec,
            "Needs_research": needs,
            "Unknown": len(profiles) - ready_search - ready_spec - needs,
        },
        "PRICING_ELIGIBLE_PROFILES": pricing_eligible,
        "queues": dict(queues),
        "primary_profile": primary,
        "VA": {
            "allowed_actions": sorted(VA_ALLOWED_ACTIONS),
            "forbidden_actions": sorted(VA_FORBIDDEN_ACTIONS),
            "role": "PRODUCT_IDENTITY_OPERATOR",
            "may_invent_identities": False,
            "may_approve_substitutions": False,
            "may_contact_suppliers": False,
            "may_bid": False,
            "may_change_scoring": False,
        },
        "working_row_patch": {
            "product_identity_resolution": {
                "kind": "M3ProductIdentityResolutionSummary",
                "Exact_identities": exact,
                "Specification_identities": spec,
                "Category_only": cat_only,
                "Unknown": unknown,
                "SUPPLIER_READINESS": {
                    "Ready_for_supplier_search": ready_search,
                    "Ready_with_specifications": ready_spec,
                    "Needs_research": needs,
                },
                "primary_Identity_type": (primary or {}).get("Identity_type"),
                "primary_Confidence": (primary or {}).get("Confidence"),
                "primary_Manufacturer": (primary or {}).get("Manufacturer"),
                "primary_Part_number": (primary or {}).get("Manufacturer_part_number"),
            },
            "product_category": (primary or {}).get("Category") or row.get("product_category"),
        },
        "Timestamp": _utc(),
        "DEVELOPMENT_NO_OUTREACH": True,
    }


def analyze_product_identity_resolution_top(
    store: Any,
    *,
    limit: int = 25,
    persist: bool = True,
) -> dict[str, Any]:
    """Batch resolve product identities across pipeline opportunities."""
    rows = [r for r in (store.all() if hasattr(store, "all") else []) if isinstance(r, dict)]

    def _score(r: dict[str, Any]) -> tuple:
        lines = r.get("line_items") or r.get("bom") or []
        has_lines = 20 if isinstance(lines, list) and lines else 0
        di = r.get("document_intelligence_full")
        has_di = 15 if isinstance(di, dict) and di.get("PROCUREMENT_LINE_ITEMS") else 0
        docs = r.get("documents") if isinstance(r.get("documents"), list) else []
        bytes_n = sum(1 for d in docs if isinstance(d, dict) and (d.get("bytes_recovered") or d.get("extracted_text")))
        return (-(has_lines + has_di + min(20, bytes_n * 5)), str(r.get("canonical_id") or ""))

    ranked = sorted(rows, key=_score)[: max(1, min(limit, 50))]
    index = load_resolution_index() if persist else {"by_id": {}}
    by_id = index.setdefault("by_id", {})

    items = []
    products_n = exact_n = spec_n = cat_n = unk_n = 0
    mfrs: set[str] = set()
    models: set[str] = set()
    parts: set[str] = set()
    nsns: set[str] = set()
    ready_search = ready_spec = needs = unk_ready = 0
    improved = []
    queues: dict[str, list] = defaultdict(list)

    for row in ranked:
        result = resolve_opportunity_product_identities(row, update_learning=persist)
        items.append(result)
        products_n += int(result.get("Products_analyzed") or 0)
        exact_n += int(result.get("Exact_identities") or 0)
        spec_n += int(result.get("Specification_identities") or 0)
        cat_n += int(result.get("Category_only") or 0)
        unk_n += int(result.get("Unknown") or 0)
        mfrs.update(result.get("Manufacturers_found") or [])
        models.update(result.get("Models_found") or [])
        parts.update(result.get("Part_numbers_found") or [])
        nsns.update(result.get("NSNs_found") or [])
        sr = result.get("SUPPLIER_READINESS") or {}
        ready_search += int(sr.get("Ready_for_supplier_search") or 0)
        ready_spec += int(sr.get("Ready_with_specifications") or 0)
        needs += int(sr.get("Needs_research") or 0)
        unk_ready += int(sr.get("Unknown") or 0)

        for qname, cards in (result.get("queues") or {}).items():
            queues[qname].extend(cards)

        for p in result.get("PRODUCT_IDENTITY_RESOLUTION_PROFILES") or []:
            if p.get("Identity_type") in {EXACT_IDENTITY, SPECIFICATION_IDENTITY}:
                improved.append(
                    {
                        "Opportunity": row.get("title"),
                        "canonical_id": row.get("canonical_id"),
                        "Original_description": p.get("Original_description"),
                        "Resolved_product": p.get("Resolved_product_name"),
                        "Identity_type": p.get("Identity_type"),
                        "Manufacturer": p.get("Manufacturer"),
                        "Part_number": p.get("Manufacturer_part_number"),
                        "Specifications": p.get("Specifications"),
                        "Confidence": p.get("Confidence"),
                        "Supplier_readiness": (p.get("SUPPLIER_RESEARCH_READINESS_SCORE") or {}).get("status"),
                    }
                )

        cid = row.get("canonical_id")
        if cid and hasattr(store, "_rows"):
            existing = store._rows.get(cid) or dict(row)
            patch = result.get("working_row_patch") or {}
            for k, v in patch.items():
                if v is not None:
                    existing[k] = v
            existing["product_identity_resolution_full"] = result
            store._rows[cid] = existing
        if cid:
            by_id[str(cid)] = {
                "summary": {
                    "Exact_identities": result.get("Exact_identities"),
                    "Specification_identities": result.get("Specification_identities"),
                    "Category_only": result.get("Category_only"),
                    "Unknown": result.get("Unknown"),
                    "SUPPLIER_READINESS": result.get("SUPPLIER_READINESS"),
                    "primary_Identity_type": (result.get("primary_profile") or {}).get("Identity_type"),
                },
                "updated_at": _utc(),
            }

    if persist:
        try:
            if hasattr(store, "save"):
                store.save()
        except Exception:
            pass
        save_resolution_index(index)

    improved.sort(
        key=lambda x: (
            0 if x.get("Identity_type") == EXACT_IDENTITY else 1,
            0 if x.get("Supplier_readiness") == READY_FOR_SUPPLIER_SEARCH else 1,
            str(x.get("Confidence") or ""),
        )
    )

    return {
        "kind": "M3ProductIdentityResolutionRun",
        "analyzed": len(items),
        "PRODUCT_IDENTITY": {
            "Products_analyzed": products_n,
            "Exact_identities": exact_n,
            "Specification_identities": spec_n,
            "Category_only": cat_n,
            "Unknown": unk_n,
        },
        "IDENTIFIERS": {
            "Manufacturers_found": len(mfrs),
            "Models_found": len(models),
            "Part_numbers_found": len(parts),
            "NSNs_found": len(nsns),
            "manufacturer_list": sorted(mfrs)[:20],
            "part_number_list": sorted(parts)[:20],
        },
        "SUPPLIER_READINESS": {
            "Ready_for_supplier_search": ready_search,
            "Ready_with_specifications": ready_spec,
            "Needs_research": needs,
            "Unknown": unk_ready,
        },
        "TOP_IMPROVED_OPPORTUNITIES": improved[:10],
        "queues": {k: v[:25] for k, v in queues.items()},
        "LIMITATIONS": {
            "Missing": sorted(
                {
                    m
                    for it in items
                    for p in (it.get("PRODUCT_IDENTITY_RESOLUTION_PROFILES") or [])
                    for m in ((p.get("SUPPLIER_RESEARCH_READINESS_SCORE") or {}).get("missing") or [])
                }
            )[:12],
            "Unknown": unk_n,
            "Blocked": needs,
        },
        "COST": {"Paid_spend": 0},
        "SAFETY": {"Outreach_actions": 0},
        "items": items,
        "NEXT_STATE": "PRODUCT_IDENTITY_RESOLUTION_OPERATIONAL",
        "DEVELOPMENT_NO_OUTREACH": True,
    }


def deal_room_product_identity_resolution_section(row: dict[str, Any]) -> dict[str, Any]:
    """Phase 9 — Product Identity Resolution Panel."""
    full = row.get("product_identity_resolution_full")
    if not isinstance(full, dict) or full.get("kind") != "M3ProductIdentityResolution":
        try:
            full = resolve_opportunity_product_identities(row, update_learning=False)
        except Exception:
            full = {}

    primary = full.get("primary_profile") or {}
    readiness = primary.get("SUPPLIER_RESEARCH_READINESS_SCORE") or {}
    return {
        "kind": "M3DealRoomProductIdentityResolution",
        "Original_description": primary.get("Original_description") or row.get("title") or "UNKNOWN",
        "Resolved_identity": primary.get("Resolved_product_name") or "UNKNOWN",
        "Manufacturer": primary.get("Manufacturer") or "UNKNOWN",
        "Part_number": primary.get("Manufacturer_part_number") or "UNKNOWN",
        "Model_number": primary.get("Model_number") or "UNKNOWN",
        "NSN": primary.get("NSN") or "UNKNOWN",
        "Specifications": primary.get("Specifications") or {},
        "Identity_type": primary.get("Identity_type") or UNKNOWN,
        "Confidence": primary.get("Confidence") or MATCH_UNKNOWN,
        "Evidence_source": primary.get("Evidence_source") or "UNKNOWN",
        "Supplier_readiness": readiness.get("status") or READINESS_UNKNOWN,
        "Missing_information": readiness.get("missing") or [],
        "Profiles_count": full.get("Products_analyzed") or 0,
        "Exact_identities": full.get("Exact_identities") or 0,
        "Specification_identities": full.get("Specification_identities") or 0,
        "Pricing_eligible": len(full.get("PRICING_ELIGIBLE_PROFILES") or []),
        "VA_allowed_actions": sorted(VA_ALLOWED_ACTIONS),
        "VA_forbidden_actions": sorted(VA_FORBIDDEN_ACTIONS),
        "Next_Action": (
            "Run supplier search on validated identity"
            if readiness.get("status") == READY_FOR_SUPPLIER_SEARCH
            else "Research missing identity: " + ", ".join((readiness.get("missing") or ["evidence"])[:3])
        ),
        "DEVELOPMENT_NO_OUTREACH": True,
    }


def build_va_identity_queues(limit: int = 25) -> dict[str, Any]:
    idx = load_resolution_index()
    # Rebuild from persisted summaries is thin — expose queue names + VA rules
    return {
        "kind": "M3ProductIdentityResolutionQueues",
        "queues": {
            Q_NEEDS_PRODUCT_IDENTITY: [],
            Q_NEEDS_SPECIFICATION_RESEARCH: [],
            Q_NEEDS_VALIDATION: [],
            Q_READY_FOR_SUPPLIER_RESEARCH: [],
        },
        "researched": len(idx.get("by_id") or {}),
        "VA_allowed_actions": sorted(VA_ALLOWED_ACTIONS),
        "VA_forbidden_actions": sorted(VA_FORBIDDEN_ACTIONS),
        "note": "Run /analyze to populate queue cards from live pipeline",
        "DEVELOPMENT_NO_OUTREACH": True,
    }


def apply_va_identity_update(
    store: Any,
    canonical_id: str,
    *,
    action: str,
    note: str | None = None,
    evidence: dict[str, Any] | None = None,
    status: str | None = None,
    line_index: int | None = None,
) -> dict[str, Any]:
    """Phase 10 — VA research/attach/validate/notes. No invent/substitute/bid/score."""
    action_u = str(action or "").upper().strip()
    if action_u not in VA_ALLOWED_ACTIONS:
        return {
            "ok": False,
            "error": "action_not_allowed",
            "action": action_u,
            "VA_allowed_actions": sorted(VA_ALLOWED_ACTIONS),
            "VA_forbidden_actions": sorted(VA_FORBIDDEN_ACTIONS),
        }

    row = None
    if hasattr(store, "_rows") and canonical_id in store._rows:
        row = store._rows[canonical_id]
    elif hasattr(store, "get"):
        row = store.get(canonical_id)
    if not isinstance(row, dict):
        return {"ok": False, "error": "opportunity_not_found", "canonical_id": canonical_id}

    notes = list(row.get("product_identity_va_notes") or [])
    full = row.get("product_identity_resolution_full")
    if not isinstance(full, dict):
        full = resolve_opportunity_product_identities(row, update_learning=False)

    if action_u in {"UPDATE_NOTES", "NOTE"} and note:
        notes.append({"at": _utc(), "note": str(note)[:2000], "action": action_u})
    if action_u == "FLAG_NEEDS_RESEARCH":
        notes.append({"at": _utc(), "note": note or "needs_research", "action": action_u})
    if action_u == "RESEARCH_IDENTITY":
        notes.append({"at": _utc(), "note": note or "identity_research_logged", "action": action_u})
    if action_u == "VALIDATE_EXTRACTION":
        notes.append({"at": _utc(), "note": note or "validated", "action": action_u})
        profiles = list(full.get("PRODUCT_IDENTITY_RESOLUTION_PROFILES") or [])
        idx = line_index if isinstance(line_index, int) else 0
        if 0 <= idx < len(profiles):
            profiles[idx] = {**profiles[idx], "VA_validated": True, "Confidence": profiles[idx].get("Confidence")}
            full["PRODUCT_IDENTITY_RESOLUTION_PROFILES"] = profiles
    if action_u == "ATTACH_EVIDENCE" and isinstance(evidence, dict):
        # VA may attach evidenced identity fields — still validate identifiers
        profiles = list(full.get("PRODUCT_IDENTITY_RESOLUTION_PROFILES") or [])
        idx = line_index if isinstance(line_index, int) else 0
        if 0 <= idx < len(profiles):
            p = dict(profiles[idx])
            if evidence.get("Manufacturer"):
                # Do not invent — only accept non-empty provided evidence from VA
                p["Manufacturer"] = str(evidence.get("Manufacturer"))[:60]
                p["Evidence_source"] = (p.get("Evidence_source") or "") + ",va_attached_evidence"
            if evidence.get("Manufacturer_part_number"):
                chk = reject_false_identifier(evidence.get("Manufacturer_part_number"), kind="part_number")
                p["Manufacturer_part_number"] = chk["Identifier"] if chk.get("accepted") else "UNKNOWN"
            if evidence.get("Model_number"):
                chk = reject_false_identifier(evidence.get("Model_number"), kind="model")
                p["Model_number"] = chk["Identifier"] if chk.get("accepted") else "UNKNOWN"
            if evidence.get("NSN"):
                chk = reject_false_identifier(evidence.get("NSN"), kind="nsn")
                p["NSN"] = chk["Identifier"] if chk.get("accepted") else "UNKNOWN"
            # Recompute type if identifiers present
            if _known(p.get("Manufacturer_part_number")) or _known(p.get("NSN")):
                p["Identity_type"] = EXACT_IDENTITY
                p["Confidence"] = MATCH_MEDIUM
            profiles[idx] = p
            full["PRODUCT_IDENTITY_RESOLUTION_PROFILES"] = profiles
            full["primary_profile"] = profiles[0]
        notes.append({"at": _utc(), "note": note or "evidence_attached", "action": action_u})
    if action_u == "UPDATE_STATUS" and status:
        full["VA_status"] = str(status).upper()
        notes.append({"at": _utc(), "note": f"status:{status}", "action": action_u})

    row["product_identity_va_notes"] = notes[-30:]
    row["product_identity_resolution_full"] = full
    if hasattr(store, "_rows"):
        store._rows[canonical_id] = row
    if hasattr(store, "save"):
        try:
            store.save()
        except Exception:
            pass

    return {
        "ok": True,
        "canonical_id": canonical_id,
        "action": action_u,
        "VA_notes": notes[-10:],
        "DEVELOPMENT_NO_OUTREACH": True,
    }
