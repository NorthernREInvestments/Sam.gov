"""Phase L.5 — Stage 2 admission: permissive uncertainty, strict certainty late."""

from __future__ import annotations

import re
from typing import Any

from phase_l.acquisition_lanes import (
    _COMMERCIAL_BRAND,
    _COMMERCIAL_CATEGORY,
    _FLEET_OR_EQUIPMENT,
    classify_acquisition_lane,
)
from phase_l.commercial_discovery import COMMERCIAL_CATEGORY_POOLS
from phase_l.product_fitness import (
    MIXED_PRODUCT_SERVICE,
    PRODUCT_RESALE,
    PRODUCT_WITH_INCIDENTAL_SERVICE,
    classify_product_fitness,
)

BUILD = "20260927-m3-phase-l5-commercial-retention-repair"

# Stage 1 outcomes (L.5)
KEEP_HIGH = "KEEP_HIGH"
KEEP_COMMERCIAL = "KEEP_COMMERCIAL"
KEEP_QUOTE_REQUIRED = "KEEP_QUOTE_REQUIRED"
KEEP_UNKNOWN_RESEARCHABLE = "KEEP_UNKNOWN_RESEARCHABLE"
SPECIALTY_ROUTE = "SPECIALTY_ROUTE"
SOFT_HOLD_DOCUMENTS = "SOFT_HOLD_DOCUMENTS"
HARD_REJECT = "HARD_REJECT"

# Identity / product statuses
BRAND_OR_EQUAL = "BRAND_OR_EQUAL"
SPEC_DRIVEN_COMMERCIAL_PRODUCT = "SPEC_DRIVEN_COMMERCIAL_PRODUCT"
CONFIGURATION_RESEARCH_REQUIRED = "CONFIGURATION_RESEARCH_REQUIRED"
DOCUMENT_ENRICHMENT_REQUIRED = "DOCUMENT_ENRICHMENT_REQUIRED"
QUANTITY_UNRESOLVED = "QUANTITY_UNRESOLVED"
HISTORY_NOT_YET_FOUND = "HISTORY_NOT_YET_FOUND"
HISTORY_UNAVAILABLE = "HISTORY_UNAVAILABLE"
HISTORY_FOUND = "HISTORY_FOUND"

# Attrition dispositions
ADVANCED_STAGE2 = "ADVANCED_STAGE2"
SOFT_HOLD = "SOFT_HOLD"
MISSING_EVIDENCE = "MISSING_EVIDENCE"
PARSER_FAILURE = "PARSER_FAILURE"
SOURCE_FAILURE = "SOURCE_FAILURE"
DEADLINE_FAILURE = "DEADLINE_FAILURE"
IDENTITY_INCOMPLETE = "IDENTITY_INCOMPLETE"
COMMERCIAL_SIGNAL_TOO_LOW = "COMMERCIAL_SIGNAL_TOO_LOW"
OTHER = "OTHER"

_BRAND_OR_EQUAL = re.compile(
    r"\b("
    r"or\s+equal|brand[\-\s]?name\s+or\s+equal|equivalent\s+to|"
    r"approved\s+equal|or\s+equivalent|equal\s+to"
    r")\b",
    re.I,
)
_ATTACHMENT_HINT = re.compile(
    r"\b("
    r"spec(?:ification)?s?|pricing|bid\s+form|price\s+sheet|equipment|"
    r"vehicle|schedule|line[\-\s]?items?|quote|proposal\s+form|workbook|"
    r"item\s+list|scope\s+of\s+work|salient"
    r")\b",
    re.I,
)
_DESCRIPTIVE_COMMERCIAL = re.compile(
    r"\b("
    r"HP|kW|gallon|gpm|psi|ton|lb|lbs|CPU|RAM|SSD|GB|TB|"
    r"cab|deck|hydraulic|diesel|gasoline|electric|"
    r"crew\s+cab|4x4|AWD|pickup|tractor|loader|mower|"
    r"server|laptop|desktop|monitor|printer|"
    r"pump|motor|valve|compressor|generator|welder"
    r")\b",
    re.I,
)


def _blob(row: dict[str, Any], commercial: dict[str, Any] | None = None) -> str:
    commercial = commercial or {}
    parts = [
        row.get("title"),
        row.get("description"),
        row.get("nomenclature"),
        commercial.get("manufacturer"),
        commercial.get("model"),
        commercial.get("mpn"),
        row.get("agency"),
    ]
    # attachment filenames / titles if present
    for att in row.get("attachments") or row.get("documents") or []:
        if isinstance(att, dict):
            parts.append(att.get("name") or att.get("filename") or att.get("title") or att.get("url"))
        else:
            parts.append(att)
    return " ".join(str(x or "") for x in parts)


def detect_brand_or_equal(row: dict[str, Any], commercial: dict[str, Any] | None = None) -> dict[str, Any]:
    commercial = commercial or {}
    blob = _blob(row, commercial)
    hit = bool(_BRAND_OR_EQUAL.search(blob))
    brand = None
    m = _COMMERCIAL_BRAND.search(blob)
    if m:
        brand = m.group(1)
    return {
        "status": BRAND_OR_EQUAL if hit or (brand and hit) else (BRAND_OR_EQUAL if hit else None),
        "brand_or_equal": hit,
        "reference_brand": brand or commercial.get("manufacturer"),
        "reference_model": commercial.get("model"),
    }


def detect_spec_driven_commercial(row: dict[str, Any], commercial: dict[str, Any] | None = None) -> bool:
    """Descriptive commercial spec without exact MPN — still researchable."""
    commercial = commercial or {}
    blob = _blob(row, commercial)
    title = str(row.get("title") or "")
    if len(title) < 12:
        return False
    fit = classify_product_fitness(row).get("product_fitness")
    # UNKNOWN fitness OK when clear commercial category / brand / descriptive hardware language
    tangible_ok = fit in {
        PRODUCT_RESALE,
        PRODUCT_WITH_INCIDENTAL_SERVICE,
        MIXED_PRODUCT_SERVICE,
        None,
        "",
        "UNKNOWN",
        "UNKNOWN_PRODUCT",
    } or fit is None or str(fit).upper() in {"UNKNOWN", "UNKNOWN_PRODUCT", "PRODUCT_UNKNOWN"}
    if fit and str(fit).upper() in {"SERVICE", "ENGINEERING_SUPPORT", "REPAIR_OVERHAUL"}:
        return False
    if commercial.get("mpn") or commercial.get("model"):
        return True
    return bool(
        tangible_ok
        and (
            _COMMERCIAL_CATEGORY.search(blob)
            or _FLEET_OR_EQUIPMENT.search(blob)
            or _COMMERCIAL_BRAND.search(blob)
            or _DESCRIPTIVE_COMMERCIAL.search(blob)
            or commercial_category_hit(row)
        )
    )


def attachment_enrichment_required(row: dict[str, Any]) -> bool:
    blob = _blob(row)
    atts = row.get("attachments") or row.get("documents") or row.get("document_links") or []
    if atts and _ATTACHMENT_HINT.search(blob):
        return True
    # filename-only check
    for att in atts:
        name = ""
        if isinstance(att, dict):
            name = str(att.get("name") or att.get("filename") or att.get("url") or "")
        else:
            name = str(att)
        if _ATTACHMENT_HINT.search(name):
            return True
    return False


def commercial_category_hit(row: dict[str, Any]) -> list[str]:
    blob = _blob(row)
    hits = []
    for cat, kws in COMMERCIAL_CATEGORY_POOLS.items():
        if any(re.search(rf"\b{re.escape(k)}\b", blob, re.I) for k in kws):
            hits.append(cat)
    return hits


def stage2_researchability_score(
    row: dict[str, Any],
    *,
    commercial: dict[str, Any] | None = None,
    fitness: str | None = None,
) -> dict[str, Any]:
    """Priority score for Stage 2 research — not final eligibility."""
    commercial = commercial or {}
    blob = _blob(row, commercial)
    score = 0
    factors: list[str] = []
    fit = fitness or classify_product_fitness(row).get("product_fitness")
    if fit in {PRODUCT_RESALE, PRODUCT_WITH_INCIDENTAL_SERVICE}:
        score += 25
        factors.append("tangible_product")
    elif fit == MIXED_PRODUCT_SERVICE:
        score += 12
        factors.append("mixed_product")
    cats = commercial_category_hit(row)
    if cats:
        score += 20
        factors.append("commercial_category:" + ",".join(cats[:3]))
    if _COMMERCIAL_BRAND.search(blob) or commercial.get("manufacturer"):
        score += 15
        factors.append("brand")
    if commercial.get("model") or commercial.get("mpn"):
        score += 15
        factors.append("model_or_mpn")
    if detect_brand_or_equal(row, commercial).get("brand_or_equal"):
        score += 12
        factors.append("brand_or_equal")
    if detect_spec_driven_commercial(row, commercial):
        score += 10
        factors.append("spec_driven")
    if attachment_enrichment_required(row):
        score += 8
        factors.append("attachment_signal")
    lane = classify_acquisition_lane(row, commercial=commercial)
    if lane.get("primary_research_priority"):
        score += 10
        factors.append("commercial_lane")
    if lane.get("specialty_pipeline"):
        score -= 15
        factors.append("specialty_lane")
    score = max(0, min(100, score))
    return {"score": score, "factors": factors, "categories": cats, "lane": lane.get("acquisition_lane")}


def collect_stage2_anchors_l5(
    row: dict[str, Any],
    *,
    commercial: dict[str, Any],
    identity: dict[str, Any],
) -> dict[str, Any]:
    """
    L.5 Stage 2 anchors — permissive uncertainty.
    Missing MPN/qty/history/price is NOT a failure.
    """
    anchors: list[str] = []
    statuses: list[str] = []
    boe = detect_brand_or_equal(row, commercial)

    if commercial.get("mpn") or identity.get("mpn"):
        anchors.append("mpn")
    if identity.get("nsn") or commercial.get("nsn"):
        anchors.append("nsn")
    if commercial.get("manufacturer") and commercial.get("model"):
        anchors.append("manufacturer_model")
    if commercial.get("model") and len(str(commercial.get("model"))) >= 4:
        anchors.append("commercial_model")
    st = str(commercial.get("commercial_identity_state") or "")
    if any(x in st for x in ("EXACT_EQUIPMENT", "EXACT_VEHICLE", "EXACT_COMMERCIAL", "STRONG")):
        anchors.append("commercial_state")
    if identity.get("sku"):
        anchors.append("sku")
    title = str(row.get("title") or "")
    if re.search(r"\b(NSN|P/?N|MPN)\b", title, re.I) and len(title) >= 20:
        anchors.append("strong_description")

    # --- L.5 permissive anchors ---
    if boe.get("brand_or_equal") or (
        commercial.get("manufacturer") and _BRAND_OR_EQUAL.search(_blob(row, commercial))
    ):
        anchors.append("brand_or_equal")
        statuses.append(BRAND_OR_EQUAL)
    elif commercial.get("manufacturer") or _COMMERCIAL_BRAND.search(_blob(row, commercial)):
        # Brand/manufacturer alone is enough to attempt Stage 2 research
        anchors.append("brand_clue")
        if "PARTIAL" in st or not commercial.get("model"):
            statuses.append("PARTIAL_COMMERCIAL_IDENTITY")

    if "PARTIAL" in st:
        anchors.append("partial_commercial")

    if detect_spec_driven_commercial(row, commercial):
        anchors.append("descriptive_spec")
        statuses.append(SPEC_DRIVEN_COMMERCIAL_PRODUCT)
        statuses.append(CONFIGURATION_RESEARCH_REQUIRED)

    if attachment_enrichment_required(row):
        anchors.append("document_signal")
        statuses.append(DOCUMENT_ENRICHMENT_REQUIRED)

    # Generic but commercial-category tangible product with enough title
    fit = classify_product_fitness(row).get("product_fitness")
    fit_u = str(fit or "UNKNOWN").upper()
    if (
        not anchors
        and fit_u not in {"SERVICE", "ENGINEERING_SUPPORT", "REPAIR_OVERHAUL"}
        and len(title) >= 18
        and (
            _COMMERCIAL_CATEGORY.search(title)
            or _FLEET_OR_EQUIPMENT.search(title)
            or commercial_category_hit(row)
            or _DESCRIPTIVE_COMMERCIAL.search(title)
        )
    ):
        anchors.append("category_tangible")
        statuses.append(SPEC_DRIVEN_COMMERCIAL_PRODUCT)

    # Deduplicate preserving order
    seen = set()
    uniq = []
    for a in anchors:
        if a not in seen:
            seen.add(a)
            uniq.append(a)

    rs = stage2_researchability_score(row, commercial=commercial, fitness=fit)
    qty = None  # quantity unresolved is OK
    qty_status = QUANTITY_UNRESOLVED

    return {
        "anchors": uniq,
        "pass": bool(uniq),
        "statuses": statuses,
        "brand_or_equal": boe,
        "researchability": rs,
        "quantity_status": qty_status,
        "history_status": HISTORY_NOT_YET_FOUND,
        "admission_policy": "L5_PERMISSIVE",
        "legacy_strict_would_pass": bool(
            set(uniq)
            & {
                "mpn",
                "nsn",
                "manufacturer_model",
                "commercial_model",
                "commercial_state",
                "sku",
                "strong_description",
            }
        ),
    }


def stage1_l5_outcome(row: dict[str, Any], *, lane: dict[str, Any] | None = None) -> str:
    from phase_l.acquisition_lanes import (
        COMMERCIAL_DISTRIBUTOR_CHANNEL,
        COMMERCIAL_OPEN_CHANNEL,
        MILSPEC_SPECIALTY,
        QUOTE_REQUIRED_COMMERCIAL,
        SOLE_SOURCE_RESTRICTED,
        SOURCE_APPROVAL_REQUIRED,
        classify_acquisition_lane,
    )

    lane = lane or classify_acquisition_lane(row)
    al = lane.get("acquisition_lane")
    if al in {MILSPEC_SPECIALTY, SOURCE_APPROVAL_REQUIRED, SOLE_SOURCE_RESTRICTED}:
        return SPECIALTY_ROUTE
    if al == QUOTE_REQUIRED_COMMERCIAL:
        return KEEP_QUOTE_REQUIRED
    if al in {COMMERCIAL_OPEN_CHANNEL, COMMERCIAL_DISTRIBUTOR_CHANNEL}:
        return KEEP_COMMERCIAL
    rs = stage2_researchability_score(row)
    if rs["score"] >= 55:
        return KEEP_HIGH
    if rs["score"] >= 30:
        return KEEP_UNKNOWN_RESEARCHABLE
    if attachment_enrichment_required(row):
        return SOFT_HOLD_DOCUMENTS
    return KEEP_UNKNOWN_RESEARCHABLE
