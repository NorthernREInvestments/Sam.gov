"""M3 procurement qualification stages (Build 002H).

Separates procurement fit from supplier quotes and financing.
Never invents fields. Unknown remains unknown with investigation trail.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from m3.research.classify import PRODUCT_CLASSES
from m3.research.extract_fields import enrich_item_from_text, extract_quantity_fields

INITIAL_PRODUCT_OK = {
    "product_supply",
    "product_with_incidental_services",
    "mixed_product_service",
}
# Owner permanent rule: reject service, installation, maintenance, construction.
# Significant installation is a hard reject (not eligibility deferral).
INITIAL_EXCLUDE_CLASSES = {
    "services",
    "professional_services",
    "maintenance_repair_services",
    "construction",
    "software_only",
    "not_applicable_non_solicitation",
    "product_with_significant_installation",
}
# Tangible product classes that require eligibility evidence (not auto Stage C)
PRODUCT_NEEDS_ELIGIBILITY_REVIEW = {
    "product_rental_leasing",
}
IT_MARKERS = re.compile(
    r"(?i)\b(software|saas|cloud\s*license|microsoft\s*365|"
    r"endpoint\s*protection|cyber\s*security|it\s*services)\b"
)
IT_HARDWARE_MARKERS = re.compile(
    r"(?i)\b(laptops?|desktops?|computers?|chromebooks?|servers?|printers?|"
    r"monitors?|network\s+equipment|routers?|switches?|tablets?)\b"
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

# Owner labels (promotion must not collapse these)
LABEL_SUPPLIER_QUOTE_READY = "supplier_quote_ready"
LABEL_CURRENTLY_OPEN_VERIFIED = "currently_open_verified"
LABEL_BID_SUBMISSION_READY = "bid_submission_ready"
LABEL_OPEN_STATUS_UNVERIFIED = "OPEN_STATUS_UNVERIFIED"
LABEL_EXPIRED_HISTORICAL = "expired_historical"


def parse_deadline_utc(value: Any) -> datetime | None:
    """Parse an authoritative deadline into timezone-aware UTC. Never invents."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    s = str(value).strip()
    if not s or s.lower() in {"unknown", "none", "n/a", "null", "tbd"}:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def resolve_authoritative_deadline(item: dict[str, Any]) -> dict[str, Any]:
    """Prefer amendment/extension deadline when present; else response/closing deadline."""
    candidates: list[tuple[str, Any]] = []
    for key in (
        "amended_deadline_at",
        "extension_deadline_at",
        "deadline_at",
        "deadline",  # owner-queue card field
        "response_deadline",
        "closing_date",
        "bid_opening_at",
        "deadline_extracted",
    ):
        if item.get(key) not in (None, "", []):
            candidates.append((key, item.get(key)))
    # Nested amendment fields
    for amd in item.get("amendments") or []:
        if isinstance(amd, dict):
            for key in ("deadline_at", "response_deadline", "closing_date"):
                if amd.get(key) not in (None, "", []):
                    candidates.append((f"amendment:{key}", amd.get(key)))
    best_key = None
    best_dt: datetime | None = None
    for key, raw in candidates:
        dt = parse_deadline_utc(raw)
        if dt is None:
            continue
        # Later amendment/extension wins when multiple parse
        if best_dt is None or dt > best_dt or str(key).startswith("amendment") or "amend" in key or "extension" in key:
            # Prefer amended/extension over earlier base even if not later clock? Prefer max UTC.
            if best_dt is None or dt >= best_dt:
                best_dt = dt
                best_key = key
    return {
        "deadline_at": best_dt.isoformat() if best_dt else None,
        "deadline_source_field": best_key,
        "deadline_dt": best_dt,
    }


def evaluate_deadline_gate(
    item: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Hard rule: past submission deadline blocks active bidding regardless of inventory open flag."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    resolved = resolve_authoritative_deadline(item)
    dt = resolved.get("deadline_dt")
    inventory_open = str(item.get("solicitation_status") or "").lower() in {"open", "amended"}
    if dt is None:
        return {
            "status": "unknown",
            "label": LABEL_OPEN_STATUS_UNVERIFIED if inventory_open else LABEL_OPEN_STATUS_UNVERIFIED,
            "evidence": "no_authoritative_deadline_parsed",
            "deadline_at": None,
            "expired": False,
            "inventory_open_ignored": False,
            **{k: resolved.get(k) for k in ("deadline_source_field",)},
        }
    expired = dt < now
    if expired:
        return {
            "status": "fail",
            "label": LABEL_EXPIRED_HISTORICAL,
            "evidence": f"deadline_passed:{dt.isoformat()};inventory_status={item.get('solicitation_status')}",
            "deadline_at": dt.isoformat(),
            "expired": True,
            "inventory_open_ignored": inventory_open,
            "deadline_source_field": resolved.get("deadline_source_field"),
        }
    return {
        "status": "pass",
        "label": LABEL_CURRENTLY_OPEN_VERIFIED if inventory_open else LABEL_OPEN_STATUS_UNVERIFIED,
        "evidence": f"deadline_future:{dt.isoformat()}",
        "deadline_at": dt.isoformat(),
        "expired": False,
        "inventory_open_ignored": False,
        "deadline_source_field": resolved.get("deadline_source_field"),
    }


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
        # Title-only patterns: only affirmative ordered semantics (002I)
        # Build 002L: also restore semantic/evidence when quantity survived slim
        # checkpoint persistence but quantity_semantic was dropped.
        need_title_qty = not work.get("quantity")
        need_semantic_restore = bool(work.get("quantity")) and not work.get("quantity_semantic")
        if need_title_qty or need_semantic_restore:
            q = extract_quantity_fields("", title=str(work.get("title") or ""))
            if q.get("quantity_extracted") and q.get("quantity_is_ordered"):
                if need_title_qty:
                    work["quantity"] = q["quantity"]
                    work["quantity_unit"] = q.get("quantity_unit")
                    work["quantity_kind"] = q.get("quantity_kind") or work.get("quantity_kind")
                work["quantity_semantic"] = work.get("quantity_semantic") or q.get("quantity_semantic")
                if not work.get("quantity_evidence"):
                    work["quantity_evidence"] = list(q.get("quantity_evidence") or []) + [
                        "source:pipeline_enrichment",
                        "build_002l_semantic_restore",
                    ]
                elif need_semantic_restore:
                    work["quantity_evidence"] = list(work.get("quantity_evidence") or []) + [
                        "build_002l_semantic_restore"
                    ]
            elif need_title_qty and q.get("ambiguous_quantity_candidates"):
                work["quantity_semantic"] = q.get("quantity_semantic")
                work["ambiguous_quantity_candidates"] = q.get("ambiguous_quantity_candidates")
                investigation_pre = list(work.get("investigation_trail") or [])
                investigation_pre.append("title_numeric_not_affirmative_ordered_quantity")
                work["investigation_trail"] = investigation_pre

    req = (work.get("requirement_class") or "unknown").lower()
    status = (work.get("solicitation_status") or "unknown").lower()
    life = str(work.get("lifecycle_status") or "").lower()
    if status in {"", "unknown", "null", "none"} and life in {
        "open",
        "active",
        "amended",
        "posted",
        "advertised",
    }:
        status = "amended" if life == "amended" else "open"
        work["solicitation_status"] = status
    elif status in {"", "unknown", "null", "none"} and life in {
        "closed",
        "expired",
        "cancelled",
        "canceled",
        "awarded",
    }:
        status = (
            "cancelled"
            if "cancel" in life
            else ("historical_award" if life == "awarded" else "closed")
        )
        work["solicitation_status"] = status
    # Map provider document_set_status into package_status when package missing
    if not work.get("package_status") and work.get("document_set_status"):
        ds = str(work.get("document_set_status") or "").upper()
        if ds in {"COMPLETE", "PARTIAL", "AVAILABLE"}:
            work["package_status"] = "COMPLETE" if ds == "AVAILABLE" else ds
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

    # Stage A — verified active solicitation (deadline beats inventory open)
    dl_gate = evaluate_deadline_gate(work)
    work["deadline_gate"] = dl_gate
    if dl_gate.get("expired"):
        work["solicitation_status"] = "closed_by_deadline"
        status = "closed_by_deadline"
        return _result(
            stage=None,
            excluded=True,
            reasons=["status:closed_by_deadline", str(dl_gate.get("evidence") or "deadline_passed")],
            work=work,
        )
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

    # Confirmed service/construction/software — only when class is confirmed AND
    # title lacks tangible goods purchase signals (supplies/equipment/materials).
    goods_title = bool(
        re.search(
            r"(?i)\b("
            r"supplies?|equipment|materials?|goods|commodit|furniture|vehicles?|"
            r"purchase\s+of|procurement\s+of|furnish|hardware|machinery|"
            r"medical\s+equipment|safety\s+equipment|construction\s+materials?"
            r")\b",
            title,
        )
    )
    if req in INITIAL_EXCLUDE_CLASSES and not goods_title:
        return _result(
            stage=STAGE_A if verified_active else None,
            excluded=True,
            reasons=[f"class:{req}"],
            work=work,
        )
    if req in INITIAL_EXCLUDE_CLASSES and goods_title:
        # Mis-tagged or mixed title — investigate as potential product, do not hard-DQ
        unknown_flags.append("class_conflict_goods_title_vs_exclude_class")
        investigation.append(f"requirement_class:{req}_overridden_by_goods_title_for_investigation")
        req = "unknown_mixed"
        work["requirement_class"] = "unknown_mixed"

    # Software-only titles without hardware → soft flag; hardware IT is a product
    if IT_MARKERS.search(title) and not IT_HARDWARE_MARKERS.search(title) and not goods_title:
        if re.search(r"(?i)\b(software|saas|license|cyber)\b", title):
            reasons.append("it_software_focus_detected")
    elif IT_HARDWARE_MARKERS.search(title):
        investigation.append("it_hardware_treated_as_tangible_product")

    if req in {"unknown", "unknown_mixed", "pending_classification", ""}:
        unknown_flags.append("requirement_class_unknown")

    product_opportunity = req in INITIAL_PRODUCT_OK or (
        req in PRODUCT_CLASSES and req not in INITIAL_EXCLUDE_CLASSES
    )
    # Uncertainty = investigate as potential product (do NOT exclude)
    if req in {"unknown", "unknown_mixed", "pending_classification", ""}:
        product_opportunity = True
        unknown_flags.append("potential_product_insufficient_classification")
    if req in PRODUCT_NEEDS_ELIGIBILITY_REVIEW:
        product_opportunity = True
        unknown_flags.append(f"eligibility_review_required:{req}")
        investigation.append(f"product_class_needs_eligibility_evidence:{req}")
    if req == "mixed_product_service":
        unknown_flags.append("mixed_requires_line_review")
        product_opportunity = True

    # Software-only without hardware remains non-product exclude
    if "it_software_focus_detected" in reasons and not product_opportunity and not IT_HARDWARE_MARKERS.search(title):
        return _result(
            stage=STAGE_A if verified_active else None,
            excluded=True,
            reasons=reasons,
            work=work,
        )

    # Quantity handling (Build 002N):
    # - Firm Stage C requires ORDERED / LINE_ITEM / MINIMUM_GUARANTEED only.
    # - ESTIMATED / MAXIMUM_AUTHORIZED are tracked but never represented as firm Stage C.
    qty = work.get("quantity")
    qty_kind = (work.get("quantity_kind") or "unknown").lower()
    qty_sem = (work.get("quantity_semantic") or "").upper()
    firm_semantics = {
        "ORDERED_QUANTITY",
        "LINE_ITEM_QUANTITY",
        "MINIMUM_GUARANTEED_QUANTITY",
    }
    estimated_semantics = {
        "ESTIMATED_QUANTITY",
        "MAXIMUM_AUTHORIZED_QUANTITY",
    }
    quantity_defined = False  # firm ordered/line-item only (Stage C basis)
    quantity_estimated = False
    # Multi-line schedules with explicit line quantities = defined purchase obligation
    line_items = work.get("line_items") or []
    verified_lines = work.get("verified_line_items") or []
    if qty_sem == "MULTI_LINE_ITEM" or len(verified_lines) >= 2:
        # Do NOT collapse distinct products into one scalar quantity.
        quantity_defined = True
        work["quantity_status"] = "MULTI_LINE_ITEM"
        work["quantity_semantic"] = "MULTI_LINE_ITEM"
        work["quantity_kind"] = work.get("quantity_kind") or "fixed"
        work["quantity_evidence"] = list(work.get("quantity_evidence") or []) + [
            f"multi_line_item_count:{len(verified_lines) or len(line_items)}"
        ]
        investigation.append("multi_line_products_preserved_not_collapsed")
    elif not qty and line_items:
        line_qtys = [li for li in line_items if li.get("quantity") not in (None, "")]
        if len(line_qtys) >= 2:
            quantity_defined = True
            work["quantity_semantic"] = "MULTI_LINE_ITEM"
            work["quantity_status"] = "MULTI_LINE_ITEM"
            qty_sem = "MULTI_LINE_ITEM"
            work["quantity_evidence"] = list(work.get("quantity_evidence") or []) + [
                f"line_item_quantities:{len(line_qtys)}"
            ]
        elif line_qtys:
            work["quantity"] = str(line_qtys[0].get("quantity"))
            work["quantity_semantic"] = "LINE_ITEM_QUANTITY"
            work["quantity_kind"] = work.get("quantity_kind") or "fixed"
            work["quantity_evidence"] = list(work.get("quantity_evidence") or []) + [
                f"line_item_quantities:{len(line_qtys)}"
            ]
            qty = work["quantity"]
            qty_sem = "LINE_ITEM_QUANTITY"

    if qty_sem != "MULTI_LINE_ITEM" and qty not in (None, "", "unknown") and str(qty).lower() not in {"n/a", "na", "not applicable"}:
        if qty_kind == "indefinite":
            unknown_flags.append("quantity_indefinite_not_fixed_purchase")
            investigation.append("idiq_detected_not_counted_as_defined")
        elif qty_sem in estimated_semantics or (
            qty_kind == "estimated" and qty_sem not in firm_semantics
        ):
            # Estimated is evidence, not firm Stage C.
            # Do NOT demote firm ORDERED/LINE_ITEM semantics just because distant
            # "up to"/"approximately" boilerplate set qty_kind=estimated.
            quantity_estimated = True
            unknown_flags.append("quantity_estimated_not_firm_order")
            investigation.append("estimated_quantity_retained_not_stage_c_firm")
            if not work.get("quantity_evidence"):
                work["quantity_evidence"] = ["estimated_quantity_field_present"]
        elif qty_sem and qty_sem not in firm_semantics and qty_sem not in estimated_semantics:
            unknown_flags.append("quantity_semantics_not_ordered")
            investigation.append(f"rejected_quantity_semantic:{qty_sem}")
            work["quantity"] = None
            qty = None
        elif qty_sem in firm_semantics or qty_sem == "":
            ev = " ".join(str(x) for x in (work.get("quantity_evidence") or []))
            if qty_sem in firm_semantics or re.search(
                r"(?i)quantity|furnish|purchase|line_item|affirmative_ordered", ev
            ):
                quantity_defined = True
                if not work.get("quantity_evidence"):
                    work["quantity_evidence"] = ["quantity_field_present"]
            else:
                unknown_flags.append("quantity_semantics_unverified")
                investigation.append("numeric_present_without_ordered_evidence")
                work["quantity"] = None
                qty = None
        else:
            unknown_flags.append("quantity_unknown")
            investigation.append("quantity_not_affirmatively_ordered")
    if qty_sem != "MULTI_LINE_ITEM" and (
        qty in (None, "", "unknown") or str(qty).lower() in {"n/a", "na", "not applicable"}
    ):
        if str(work.get("quantity")).lower() in {"n/a", "na", "not applicable"}:
            reasons.append("quantity_not_applicable")
        elif not quantity_defined and not quantity_estimated:
            if "quantity_semantics_not_ordered" not in unknown_flags:
                unknown_flags.append("quantity_unknown")
            if "quantity_not_found_in_accessible_text" not in investigation:
                investigation.append("quantity_not_found_in_accessible_text")
            if work.get("ambiguous_quantity_candidates"):
                investigation.append("ambiguous_numeric_requires_document_confirmation")

    if BOND_MARKERS.search(blob):
        unknown_flags.append("bonding_language_present_unverified")
    if INSURANCE_MARKERS.search(blob):
        unknown_flags.append("insurance_language_present_unverified")

    # Hard exclusions
    if "quantity_not_applicable" in reasons or (
        "it_software_focus_detected" in reasons
        and re.search(r"(?i)software|saas", title)
        and not IT_HARDWARE_MARKERS.search(title)
        and not goods_title
    ):
        return _result(
            stage=STAGE_B if product_opportunity else (STAGE_A if verified_active else None),
            excluded=True,
            reasons=reasons,
            unknown_flags=unknown_flags,
            work=work,
            product_opportunity=False,
            verified_active=verified_active,
            quantity_defined=quantity_defined,
            investigation=investigation,
        )

    # Service-title handling (Build 002Q repair): do NOT reject product purchases
    # solely on ambiguous keywords (maintenance supplies, equipment lease-purchase, etc.).
    # Confirmed service *contracts* still hard-DQ; ambiguous language only flags review.
    # Title-only product signal (do not trust mis-tagged requirement_class here).
    product_supply_signal = bool(
        re.search(
            r"(?i)\b("
            r"purchase\s+of|procurement\s+of|supply\s+of|furnish(?:ing)?|"
            r"equipment|supplies?|materials?|goods|commodit(?:y|ies)|"
            r"vehicles?|trucks?|trailers?|generators?|pumps?|furniture|"
            r"appliances?|tools?\b"
            r")\b",
            title,
        )
    )
    confirmed_service_contract = bool(
        re.search(
            r"(?i)\b("
            r"pest\s+control\s+services?|auctioneer\s+services?|"
            r"professional\s+services?|consulting\s+services?|painting\s+services?|"
            r"janitorial\s+services?|staffing\s+services?|"
            r"shuttle\s+bus\s+services?|maintenance\s+services?|"
            r"repair\s+services?|cleaning\s+services?"
            r")\b",
            title,
        )
    ) and not product_supply_signal
    ambiguous_service_language = bool(
        re.search(
            r"(?i)\b("
            r"catalyst\s+fund|investment\s+fund|underwriting|"
            r"demolition\s+services?|site\s+preparation\s+services?"
            r")\b",
            title,
        )
    ) and not product_supply_signal
    if confirmed_service_contract:
        return _result(
            stage=STAGE_A if verified_active else None,
            excluded=True,
            reasons=["class:services_title_confirmed"],
            unknown_flags=unknown_flags,
            work=work,
            product_opportunity=False,
            verified_active=verified_active,
            quantity_defined=False,
            investigation=investigation
            + ["confirmed_service_contract_title_not_product_purchase"],
        )
    # Construction / grading / erosion control — never product bid-ready
    req_cls = str(work.get("requirement_class") or "").lower()
    confirmed_construction = req_cls == "construction" or bool(
        re.search(
            r"(?i)\b("
            r"erosion\s+(?:and\s+sediment\s+)?(?:control|pollution)|"
            r"sediment\s+pollution\s+control|site\s+grading|principal\s+items?\s+of\s+work|"
            r"abandoned\s+mine\s+reclamation|construction\s+services?"
            r")\b",
            title,
        )
    ) and not product_supply_signal
    if confirmed_construction or req_cls in {"vendor_qualification", "services", "professional_services"}:
        return _result(
            stage=STAGE_A if verified_active else None,
            excluded=True,
            reasons=[f"class:non_product:{req_cls or 'construction'}"],
            unknown_flags=unknown_flags,
            work=work,
            product_opportunity=False,
            verified_active=verified_active,
            quantity_defined=False,
            investigation=investigation
            + ["non_product_requirement_not_supplier_pricing_candidate"],
        )
    if ambiguous_service_language and not product_supply_signal:
        unknown_flags.append("service_or_non_product_title_requires_review")
        investigation.append("ambiguous_non_product_title_requires_document_review")
        # Do not clear an already firm ordered quantity solely for ambiguity.

    # Stage C — firm ordered/line-item quantity only (Build 002N).
    # Estimated / ambiguous never represented as firm Stage C.
    if product_opportunity and quantity_defined and verified_individual and not reasons:
        soft_unknown = [
            f
            for f in unknown_flags
            if f.startswith("bonding")
            or f.startswith("insurance")
            or f == "service_or_non_product_title_requires_review"
        ]
        hard_unknown = [
            f
            for f in unknown_flags
            if f not in soft_unknown
            and f != "active_status_unverified"
            and f != "quantity_estimated_not_firm_order"
        ]
        if not hard_unknown:
            stage = STAGE_C
            if "active_status_unverified" in unknown_flags:
                investigation.append("fit_with_unverified_status_owner_should_confirm_deadline")
            return _result(
                stage=stage,
                excluded=False,
                reasons=["product_class_firm_ordered_quantity_individual"],
                unknown_flags=soft_unknown
                + (["active_status_unverified"] if "active_status_unverified" in unknown_flags else []),
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
        "quantity_semantic": work.get("quantity_semantic"),
        "quantity_evidence": work.get("quantity_evidence"),
        "investigation_trail": investigation or [],
        "next_stage": next_stage,
        "enriched_fields": {
            k: work.get(k)
            for k in (
                "quantity",
                "quantity_unit",
                "quantity_kind",
                "quantity_semantic",
                "manufacturer_part_number",
                "nsn",
                "deadline_extracted",
                "line_items",
                "submission_method",
                "submission_instructions",
                "brand_restrictions",
                "approved_equivalents",
            )
            if work.get(k) is not None
        },
    }


# --- Bid eligibility (procurement gates only; no pricing / financing / supplier stock) ---

_OEM_ONLY_RE = re.compile(
    r"(?i)\b("
    r"oem\s*only|manufacturer\s*only|manufacturers?\s*only|"
    r"no\s*resellers?|resellers?\s*not\s*(?:allowed|eligible|permitted)|"
    r"authorized\s*oem\s*only|factory\s*direct\s*only"
    r")\b"
)
_RESELLER_OK_RE = re.compile(
    r"(?i)\b("
    r"authorized\s*(?:dealers?|distributors?|resellers?)|"
    r"dealers?\s*(?:and|&)\s*distributors?|"
    r"resellers?\s*(?:are\s*)?(?:welcome|allowed|eligible|permitted)|"
    r"or\s*equal|brand\s*name\s*or\s*equal|approved\s*equals?|"
    r"equal\s*product|equivalent\s*(?:product|item|brand)"
    r")\b"
)
_INSTALL_BLOCK_RE = re.compile(
    r"(?i)\b("
    r"installation\s*required|must\s*(?:provide|include|perform)\s*install(?:ation)?|"
    r"turnkey\s*install|install\s*(?:and|&)\s*commission"
    r")\b"
)
_LICENSE_BLOCK_RE = re.compile(
    r"(?i)\b("
    r"contractor'?s?\s*license\s*required|"
    r"must\s*hold\s*(?:a\s*)?(?:valid\s*)?(?:class\s*)?[abc]\-?\s*license|"
    r"licensed\s*contractor\s*only|gc\s*license\s*required"
    r")\b"
)
_SUBMISSION_RE = re.compile(
    r"(?i)\b("
    r"submit\s*(?:bids?|proposals?|quotes?|offers?)|"
    r"sealed\s*bid|electronic\s*submission|e-?procurement|"
    r"bid\s*submission|proposal\s*due|how\s*to\s*respond|"
    r"upload\s*(?:your\s*)?(?:bid|proposal)|portal\s*submission|"
    r"respond\s+(?:via|through|using)\s+(?:the\s+)?(?:portal|system|website)|"
    r"bids?\s+must\s+be\s+(?:submitted|received)|"
    r"proposals?\s+must\s+be\s+(?:submitted|received)"
    r")\b"
)
# Instrument titles (IFB/RFP) identify a solicitation type — NOT submission instructions.
_INSTRUMENT_RE = re.compile(
    r"(?i)\b("
    r"invitation\s+for\s+bids?|request\s+for\s+(?:proposals?|quotes?|bids?|information)|"
    r"\bIFB\b|\bRFB\b|\bRFP\b|\bRFQ\b|\bRFI\b"
    r")\b"
)
_BOND_WAIVED_RE = re.compile(
    r"(?i)\b(bid\s*bond\s*(?:not\s*required|waived)|no\s*bid\s*bond|"
    r"performance\s*bond\s*(?:not\s*required|waived))\b"
)


def evaluate_bid_eligibility(
    item: dict[str, Any],
    pipe: dict[str, Any] | None = None,
    *,
    document_text: str | None = None,
) -> dict[str, Any]:
    """Evidence-based procurement bid eligibility.

    Separate from supplier availability, pricing, profitability, and financing.
    Fail-closed: missing evidence for a required gate → not bid-eligible.
    """
    work = dict(item)
    pipe = pipe or evaluate_procurement_pipeline(work, document_text=document_text)
    blob = "\n".join(
        str(x)
        for x in (
            work.get("title"),
            work.get("description"),
            document_text,
            work.get("document_text"),
            work.get("submission_instructions"),
            work.get("submission_method"),
            " ".join(str(x) for x in (work.get("brand_restrictions") or [])),
            " ".join(str(x) for x in (work.get("approved_equivalents") or [])),
        )
        if x
    )
    gates: dict[str, dict[str, Any]] = {}

    def _gate(name: str, status: str, evidence: str) -> None:
        gates[name] = {"status": status, "evidence": evidence}

    # 0) Authoritative deadline — past deadline ALWAYS blocks actionable bidding.
    # Inventory "open" must never override a past deadline.
    deadline_gate = evaluate_deadline_gate(work)
    work["deadline_gate"] = deadline_gate
    work["open_status_label"] = deadline_gate.get("label")
    if deadline_gate.get("deadline_at") and not work.get("deadline_at"):
        work["deadline_at"] = deadline_gate["deadline_at"]
    if deadline_gate.get("expired"):
        _gate("deadline_not_passed", "fail", str(deadline_gate.get("evidence") or "deadline_passed"))
        _gate("active_solicitation", "fail", "closed_by_deadline")
        work["solicitation_status"] = "closed_by_deadline"
    else:
        _gate(
            "deadline_not_passed",
            str(deadline_gate.get("status") or "unknown"),
            str(deadline_gate.get("evidence") or "deadline_gate"),
        )

    # 1) Active solicitation — map provider lifecycle when solicitation_status missing
    #    (skipped/overridden above when deadline already passed)
    status = str(work.get("solicitation_status") or "unknown").lower()
    life = str(work.get("lifecycle_status") or "").lower()
    if not deadline_gate.get("expired"):
        if status not in {"open", "amended"} and life in {"open", "active", "amended", "posted"}:
            status = "amended" if life == "amended" else "open"
            work["solicitation_status"] = status
        if status in {"open", "amended"}:
            # Open flag alone is not independent verification when deadline unknown
            if deadline_gate.get("status") == "unknown":
                _gate("active_solicitation", "unknown", LABEL_OPEN_STATUS_UNVERIFIED)
            else:
                _gate("active_solicitation", "pass", f"status:{status}")
        elif status == "upcoming":
            _gate("active_solicitation", "fail", "status:upcoming_not_yet_open")
        elif status in {"closed", "cancelled", "historical_award", "closed_by_deadline", "forecast"}:
            _gate("active_solicitation", "fail", f"status:{status}")
        else:
            _gate("active_solicitation", "unknown", f"{LABEL_OPEN_STATUS_UNVERIFIED}:{status}")

    # 2) Tangible products + 3) defined quantities (Stage C)
    stage = pipe.get("stage")
    qty_ok = bool(pipe.get("quantity_defined"))
    product_ok = bool(pipe.get("product_opportunity")) and stage == STAGE_C
    if product_ok:
        _gate("tangible_products", "pass", f"stage:{STAGE_C}")
    elif pipe.get("product_opportunity"):
        _gate("tangible_products", "fail", f"product_not_stage_c:{stage}")
    else:
        _gate("tangible_products", "fail", "not_product_opportunity")
    if qty_ok and stage == STAGE_C:
        _gate("defined_quantities", "pass", f"semantic:{pipe.get('quantity_semantic') or work.get('quantity_semantic')}")
    elif qty_ok:
        _gate("defined_quantities", "fail", "quantity_defined_but_not_stage_c")
    else:
        _gate("defined_quantities", "fail", "quantity_not_firm_ordered")

    # 4) Reseller eligibility (solicitation terms — not supplier onboarding)
    if _OEM_ONLY_RE.search(blob):
        _gate("reseller_eligibility", "fail", "oem_or_manufacturer_only_language")
    elif _RESELLER_OK_RE.search(blob) or work.get("approved_equivalents"):
        _gate("reseller_eligibility", "pass", "dealer_reseller_or_equal_language")
    elif blob.strip() and len(blob.strip()) >= 80:
        # Document/description present with no OEM-only restriction → open competition assumed
        _gate("reseller_eligibility", "pass", "no_oem_only_restriction_in_accessible_text")
    else:
        _gate("reseller_eligibility", "unknown", "insufficient_text_to_verify_reseller_terms")

    # 5) No blocking install / insurance / bond / license
    blocking: list[str] = []
    if _INSTALL_BLOCK_RE.search(blob) or (work.get("requirement_class") or "") == "product_with_significant_installation":
        blocking.append("installation_required")
    if _LICENSE_BLOCK_RE.search(blob):
        blocking.append("contractor_license_required")
    if BOND_MARKERS.search(blob) and not _BOND_WAIVED_RE.search(blob):
        blocking.append("bonding_language_unverified")
    if INSURANCE_MARKERS.search(blob):
        blocking.append("insurance_language_unverified")
    if blocking:
        _gate("no_blocking_install_insurance_bond_license", "fail", ",".join(blocking))
    elif blob.strip() and len(blob.strip()) >= 80:
        _gate("no_blocking_install_insurance_bond_license", "pass", "no_blocking_markers_in_accessible_text")
    else:
        _gate(
            "no_blocking_install_insurance_bond_license",
            "unknown",
            "insufficient_text_to_verify_bond_insurance_install",
        )

    # 6) Submission instructions — IFB/RFP title alone is NOT enough (owner 002Q)
    if work.get("submission_method") or work.get("submission_instructions"):
        _gate(
            "submission_instructions",
            "pass",
            "submission_field:"
            + ("method" if work.get("submission_method") else "instructions"),
        )
    elif _SUBMISSION_RE.search(blob):
        _gate("submission_instructions", "pass", "submission_language_in_accessible_text")
    elif _INSTRUMENT_RE.search(blob):
        _gate(
            "submission_instructions",
            "unknown",
            "instrument_title_only_not_submission_instructions",
        )
    else:
        _gate("submission_instructions", "unknown", "submission_instructions_not_found")

    # 6b) Ordinary registration vs inaccessible vehicle
    url = str(work.get("official_url") or work.get("source_url") or "").lower()
    if "sam.gov" in url or "dibbs" in url:
        work["vendor_registration_required"] = True
        work["contract_vehicle"] = "federal_sam"
        # Ordinary SAM registration is acceptable — do not hard-fail eligibility;
        # disposition mapper routes to easy_vendor_registration when otherwise ready.
        _gate("vendor_registration", "pass", "ordinary_sam_registration_acceptable")
    elif "planetbids" in url or "bonfirehub" in url or "opengov" in url:
        work.setdefault("vendor_registration_required", True)
        _gate("vendor_registration", "pass", "ordinary_portal_vendor_registration")

    # 7) Sufficient documents — honor provider document_set_status; Stage C text counts
    pkg = str(work.get("package_status") or work.get("document_set_status") or "").upper()
    doc_len = int(
        work.get("document_text_len")
        or len(str(work.get("document_text") or work.get("description") or ""))
    )
    atts = work.get("attachment_candidates") or []
    if pkg in {"COMPLETE", "PARTIAL"} and (pkg == "COMPLETE" or doc_len >= 200):
        _gate("sufficient_documents", "pass", f"package_or_document_set_status:{pkg}")
    elif doc_len >= 500:
        _gate("sufficient_documents", "pass", f"document_text_len:{doc_len}")
    elif stage == STAGE_C and qty_ok and doc_len >= 200:
        # Firm qty + Stage C already required usable text; do not demand 500-char preview floor
        _gate("sufficient_documents", "pass", f"stage_c_qty_document_text_len:{doc_len}")
    elif isinstance(atts, list) and len(atts) >= 1 and (work.get("description") or "").strip():
        _gate("sufficient_documents", "pass", f"attachments:{len(atts)}_with_description")
    elif (work.get("description") or "").strip() and len(str(work.get("description"))) >= 200:
        _gate("sufficient_documents", "unknown", "description_only_package_incomplete")
    else:
        _gate("sufficient_documents", "unknown", "documents_insufficient_or_missing")

    # unknown = information recovery; fail = confirmed block. Ready requires all pass.
    failed = [k for k, v in gates.items() if v["status"] == "fail"]
    unknown_gates = [k for k, v in gates.items() if v["status"] == "unknown"]
    bid_eligible = len(failed) == 0 and len(unknown_gates) == 0 and stage == STAGE_C
    # AI analysis alone never satisfies these gates — deterministic only.
    supplier_quote_ready = bool(
        bid_eligible
        and not deadline_gate.get("expired")
        and stage == STAGE_C
        and pipe.get("quantity_defined")
    )
    currently_open_verified = bool(
        deadline_gate.get("label") == LABEL_CURRENTLY_OPEN_VERIFIED
        and not deadline_gate.get("expired")
    )
    ready_to_submit = bool(supplier_quote_ready and currently_open_verified and bid_eligible)

    return {
        "bid_eligible": bid_eligible,
        "ready_to_submit": ready_to_submit,
        "supplier_quote_ready": supplier_quote_ready,
        "currently_open_verified": currently_open_verified,
        "open_status_label": deadline_gate.get("label"),
        "deadline_gate": deadline_gate,
        "gates": gates,
        "failed_gates": failed,
        "unknown_gates": unknown_gates,
        "information_recovery_required": bool(unknown_gates) and len(failed) == 0,
        "stage": stage,
        "procurement_only": True,
        "excludes_pricing_financing_supplier_stock": True,
        "ai_analysis_alone_insufficient": True,
        "next_commercial_stage": STAGE_D if bid_eligible else pipe.get("next_stage"),
    }
