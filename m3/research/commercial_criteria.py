"""M3 procurement qualification stages (Build 002H).

Separates procurement fit from supplier quotes and financing.
Never invents fields. Unknown remains unknown with investigation trail.
"""

from __future__ import annotations

import re
from typing import Any

from m3.research.classify import PRODUCT_CLASSES
from m3.research.extract_fields import enrich_item_from_text, extract_quantity_fields

INITIAL_PRODUCT_OK = {"product_supply", "product_with_incidental_services"}
INITIAL_EXCLUDE_CLASSES = {
    "services",
    "professional_services",
    "maintenance_repair_services",
    "construction",
    "software_only",
    "product_rental_leasing",
    "product_with_significant_installation",
    "not_applicable_non_solicitation",
}
IT_MARKERS = re.compile(
    r"(?i)\b(software|saas|cloud\s*license|microsoft\s*365|cisco|network\s*equipment|"
    r"server|laptop|chromebook|endpoint\s*protection|cyber\s*security|it\s*services)\b"
)
BOND_MARKERS = re.compile(
    r"(?i)\b(bid\s*bond|performance\s*bond|payment\s*bond|surety|"
    r"performance\s*and\s*payment\s*bond)\b"
)
INSURANCE_MARKERS = re.compile(
    r"(?i)\b(professional\s*liability|errors?\s*and\s*omissions|"
    r"\$\s*5,?\d{3},?\d{3}\s*(?:general\s*)?liability|ocip|ccip)\b"
)

# Pipeline stages (section 14)
STAGE_A = "VERIFIED_ACTIVE_SOLICITATION"
STAGE_B = "VERIFIED_PRODUCT_OPPORTUNITY"
STAGE_C = "VERIFIED_M3_PROCUREMENT_FIT"
STAGE_D = "COMMERCIAL_RESEARCH_REQUIRED"
STAGE_E = "SUPPLIER_QUOTE_REQUIRED"
STAGE_F = "FINANCIALLY_QUALIFIED"
STAGE_G = "READY_TO_BID"


def apply_initial_m3_criteria(item: dict[str, Any]) -> dict[str, Any]:
    """Backward-compatible wrapper — prefer evaluate_procurement_pipeline."""
    pipe = evaluate_procurement_pipeline(item)
    status = "not_eligible"
    if pipe.get("stage") == STAGE_C:
        status = "verified_initial_fit"
    elif pipe.get("stage") in {STAGE_B, STAGE_D} or pipe.get("product_opportunity"):
        status = "unknown" if pipe.get("unknown_flags") else "verified_initial_fit"
        if pipe.get("stage") == STAGE_B and not pipe.get("quantity_defined"):
            status = "unknown"
        if pipe.get("stage") == STAGE_C:
            status = "verified_initial_fit"
    elif pipe.get("excluded"):
        status = "not_eligible"
    else:
        status = "unknown"
    return {
        "initial_m3_status": status,
        "reasons": pipe.get("reasons") or [],
        "unknown_flags": pipe.get("unknown_flags") or [],
        "pipeline": pipe,
    }


def evaluate_procurement_pipeline(
    item: dict[str, Any],
    *,
    document_text: str | None = None,
) -> dict[str, Any]:
    """Evaluate stages A–C (procurement). D–G reserved until commercial evidence exists.

    Quantity: extract from document_text / item fields when published.
    Missing quantity after extraction → unknown flag, not fabricated fit.
    Defined quantity + product + active individual → VERIFIED_M3_PROCUREMENT_FIT
    without requiring supplier quote (stage E) or financing (stage F).
    """
    work = dict(item)
    texts = [
        str(document_text or ""),
        str(work.get("document_text") or ""),
        str(work.get("description") or ""),
        "\n".join(str(x) for x in (work.get("quantity_evidence") or []) if x),
    ]
    # Pull from package classification hint / detail excerpts if present
    hint = work.get("classification_hint") or work.get("detail_fields") or {}
    if isinstance(hint, dict):
        texts.append(str(hint.get("description_excerpt") or ""))
        for ex in hint.get("evidence") or []:
            texts.append(str(ex))

    combined = "\n".join(t for t in texts if t)
    if combined.strip() or work.get("title"):
        work = enrich_item_from_text(work, combined, source="pipeline_enrichment")
        # Also try title-only patterns
        if not work.get("quantity"):
            q = extract_quantity_fields("", title=str(work.get("title") or ""))
            if q.get("quantity_extracted"):
                work["quantity"] = q["quantity"]
                work["quantity_unit"] = q.get("quantity_unit")
                work["quantity_kind"] = q.get("quantity_kind")
                work["quantity_evidence"] = q.get("quantity_evidence")

    req = (work.get("requirement_class") or "unknown").lower()
    status = (work.get("solicitation_status") or "unknown").lower()
    title = str(work.get("title") or "")
    blob = f"{title}\n{combined}"
    reasons: list[str] = []
    unknown_flags: list[str] = []
    investigation: list[str] = list(work.get("investigation_trail") or [])

    verified_individual = bool(
        work.get("verified_individual_solicitation")
        or work.get("record_type") == "INDIVIDUAL_SOLICITATION"
    )
    if not verified_individual:
        return _result(
            stage=None,
            excluded=True,
            reasons=[f"record_type:{work.get('record_type') or 'missing'}"],
            work=work,
        )

    # Stage A — verified active solicitation
    inactive = {"closed", "cancelled", "historical_award", "forecast", "closed_by_deadline"}
    if status in inactive:
        return _result(stage=None, excluded=True, reasons=[f"status:{status}"], work=work)
    if status == "unknown":
        unknown_flags.append("active_status_unverified")
        investigation.append("status_unknown_not_assumed_open")

    stage_a = status in {"open", "amended", "upcoming"} or (
        status == "unknown" and verified_individual
    )
    # Unknown status: count as solicitation but not fully verified active
    verified_active = status in {"open", "amended"}

    if req in INITIAL_EXCLUDE_CLASSES:
        return _result(stage=STAGE_A if verified_active else None, excluded=True, reasons=[f"class:{req}"], work=work)

    if IT_MARKERS.search(blob) and req not in INITIAL_PRODUCT_OK:
        # IT in title of non-product class
        pass
    if IT_MARKERS.search(title) and req in INITIAL_PRODUCT_OK | {"unknown", "unknown_mixed", "pending_classification", ""}:
        # Soft IT signal — flag unless clearly commodity hardware resale later
        if re.search(r"(?i)\b(software|saas|license|cyber)\b", title):
            reasons.append("it_focus_detected")

    if req in {"unknown", "unknown_mixed", "pending_classification", ""}:
        unknown_flags.append("requirement_class_unknown")

    product_opportunity = req in INITIAL_PRODUCT_OK or (
        req in PRODUCT_CLASSES and req not in INITIAL_EXCLUDE_CLASSES
    )
    if req == "mixed_product_service":
        unknown_flags.append("mixed_requires_line_review")
        product_opportunity = True  # investigate, don't auto-exclude

    if reasons and "it_focus_detected" in reasons and not product_opportunity:
        return _result(stage=STAGE_A if verified_active else None, excluded=True, reasons=reasons, work=work)

    # Quantity handling
    qty = work.get("quantity")
    qty_kind = (work.get("quantity_kind") or "unknown").lower()
    quantity_defined = False
    if qty not in (None, "", "unknown") and str(qty).lower() not in {"n/a", "na", "not applicable"}:
        if qty_kind == "indefinite":
            unknown_flags.append("quantity_indefinite_not_fixed_purchase")
            investigation.append("idiq_detected_not_counted_as_defined")
        elif qty_kind == "estimated":
            unknown_flags.append("quantity_estimated_not_guaranteed")
            # Still allow procurement fit for estimated one-time buys with numeric qty
            quantity_defined = True
            investigation.append("estimated_numeric_quantity_accepted_with_flag")
        else:
            quantity_defined = True
            if not work.get("quantity_evidence"):
                work["quantity_evidence"] = ["quantity_field_present"]
    else:
        if str(qty).lower() in {"n/a", "na", "not applicable"}:
            reasons.append("quantity_not_applicable")
        else:
            unknown_flags.append("quantity_unknown")
            investigation.append("quantity_not_found_in_accessible_text")

    if BOND_MARKERS.search(blob):
        unknown_flags.append("bonding_language_present_unverified")
    if INSURANCE_MARKERS.search(blob):
        unknown_flags.append("insurance_language_present_unverified")

    # Hard exclusions
    if "quantity_not_applicable" in reasons or ("it_focus_detected" in reasons and re.search(r"(?i)software|saas", title)):
        return _result(
            stage=STAGE_B if product_opportunity else (STAGE_A if verified_active else None),
            excluded=True,
            reasons=reasons,
            unknown_flags=unknown_flags,
            work=work,
            product_opportunity=product_opportunity,
            verified_active=verified_active,
            quantity_defined=quantity_defined,
            investigation=investigation,
        )

    # Stage C — M3 procurement fit: product + defined qty + individual + not excluded
    # Does NOT require supplier quote or financing.
    if product_opportunity and quantity_defined and verified_individual and not reasons:
        soft_unknown = [f for f in unknown_flags if f.startswith("bonding") or f.startswith("insurance") or f == "quantity_estimated_not_guaranteed"]
        hard_unknown = [f for f in unknown_flags if f not in soft_unknown and f != "active_status_unverified"]
        if not hard_unknown:
            stage = STAGE_C
            # Still note if status unknown
            if "active_status_unverified" in unknown_flags:
                investigation.append("fit_with_unverified_status_owner_should_confirm_deadline")
            return _result(
                stage=stage,
                excluded=False,
                reasons=["product_class_defined_quantity_individual"],
                unknown_flags=soft_unknown + (["active_status_unverified"] if "active_status_unverified" in unknown_flags else []),
                work=work,
                product_opportunity=True,
                verified_active=verified_active,
                quantity_defined=True,
                investigation=investigation,
                next_stage=STAGE_D,
            )

    if product_opportunity:
        return _result(
            stage=STAGE_B,
            excluded=False,
            reasons=reasons,
            unknown_flags=unknown_flags,
            work=work,
            product_opportunity=True,
            verified_active=verified_active,
            quantity_defined=quantity_defined,
            investigation=investigation,
            next_stage=STAGE_D if quantity_defined else "EXTRACT_QUANTITY_OR_ACQUIRE_DOCS",
        )

    return _result(
        stage=STAGE_A if (verified_active or stage_a) else None,
        excluded=False,
        reasons=reasons,
        unknown_flags=unknown_flags or ["insufficient_verified_fields"],
        work=work,
        product_opportunity=False,
        verified_active=verified_active,
        quantity_defined=quantity_defined,
        investigation=investigation,
    )


def _result(
    *,
    stage: str | None,
    excluded: bool,
    reasons: list[str],
    work: dict[str, Any],
    unknown_flags: list[str] | None = None,
    product_opportunity: bool = False,
    verified_active: bool = False,
    quantity_defined: bool = False,
    investigation: list[str] | None = None,
    next_stage: str | None = None,
) -> dict[str, Any]:
    return {
        "stage": stage,
        "excluded": excluded,
        "reasons": reasons,
        "unknown_flags": unknown_flags or [],
        "product_opportunity": product_opportunity,
        "verified_active": verified_active,
        "quantity_defined": quantity_defined,
        "quantity": work.get("quantity"),
        "quantity_kind": work.get("quantity_kind"),
        "quantity_evidence": work.get("quantity_evidence"),
        "investigation_trail": investigation or [],
        "next_stage": next_stage,
        "enriched_fields": {
            k: work.get(k)
            for k in (
                "quantity",
                "quantity_unit",
                "quantity_kind",
                "manufacturer_part_number",
                "nsn",
                "deadline_extracted",
            )
            if work.get(k) is not None
        },
    }
