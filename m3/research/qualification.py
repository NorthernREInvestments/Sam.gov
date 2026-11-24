"""Progressive business qualification before full-package acquisition (Build 002F/002G).

Discovery remains unrestricted. Full-package acquisition is gated.
Record identity (002G) runs before requirement classification intensity.
"""

from __future__ import annotations

from typing import Any

from m3.research.classify import PRODUCT_CLASSES
from m3.research.identity import NON_OPPORTUNITY_TYPES, VERIFIED_SOLICITATION_TYPES

CONFIRMED_NON_PRODUCT = {
    "services",
    "professional_services",
    "maintenance_repair_services",
    "construction",
    "software_only",
}

ELIGIBLE_PRODUCT = set(PRODUCT_CLASSES) | {"mixed_product_service"}

INACTIVE_STATUSES = {
    "closed",
    "closed_by_deadline",
    "cancelled",
    "historical_award",
}

# Forecasts are not active bid packages for automatic full acquisition
AUTO_SKIP_STATUSES = INACTIVE_STATUSES | {"forecast"}

UNCERTAIN = {"unknown_mixed", "unknown", "pending_classification", ""}

SKIP_IDENTITY_TYPES = set(NON_OPPORTUNITY_TYPES)

DISPOSITIONS = {
    "ELIGIBLE_FULL_PACKAGE",
    "TARGETED_INVESTIGATION",
    "SKIP_CONFIRMED_NON_PRODUCT",
    "SKIP_INACTIVE_STATUS",
    "SKIP_NON_SOLICITATION_IDENTITY",
    "MANUAL_OVERRIDE",
    "SAVED_PRIORITY",
}


def qualify_for_acquisition(
    item: dict[str, Any],
    *,
    criteria: dict[str, Any] | None = None,
    saved: bool = False,
    manual_override: bool = False,
) -> dict[str, Any]:
    """Decide acquisition intensity. Unknown ≠ confirmed failure."""
    criteria = criteria or {}
    req = (item.get("requirement_class") or "unknown_mixed").lower()
    status = (item.get("solicitation_status") or "unknown").lower()

    record_type = str(item.get("record_type") or "").upper()
    verified_sol = bool(item.get("verified_individual_solicitation"))

    if manual_override:
        return {
            "disposition": "MANUAL_OVERRIDE",
            "acquire_full_package": True,
            "targeted_only": False,
            "reason": "owner_or_manual_override",
            "requirement_class": req,
            "solicitation_status": status,
            "record_type": record_type or None,
        }

    if saved and criteria.get("always_acquire_saved", True):
        return {
            "disposition": "SAVED_PRIORITY",
            "acquire_full_package": True,
            "targeted_only": False,
            "reason": "saved_opportunity",
            "requirement_class": req,
            "solicitation_status": status,
            "record_type": record_type or None,
        }

    # Identity gate — do not full-package forecasts/portals/historical/open-data rows
    if record_type in SKIP_IDENTITY_TYPES:
        return {
            "disposition": "SKIP_NON_SOLICITATION_IDENTITY",
            "acquire_full_package": False,
            "targeted_only": False,
            "reason": f"record_type:{record_type}",
            "requirement_class": req,
            "solicitation_status": status,
            "record_type": record_type,
        }

    if record_type and record_type not in VERIFIED_SOLICITATION_TYPES and not verified_sol:
        # Listings / unresolved → targeted investigation only (may resolve to individuals)
        if record_type in {"PROCUREMENT_LISTING", "UNRESOLVED_RECORD", "SUPPORTING_DOCUMENT", "PROCUREMENT_ANNOUNCEMENT"}:
            return {
                "disposition": "TARGETED_INVESTIGATION",
                "acquire_full_package": False,
                "targeted_only": True,
                "reason": f"identity_lead:{record_type}",
                "requirement_class": req,
                "solicitation_status": status,
                "record_type": record_type,
            }

    # Configurable class allow-list (default: product-like + unknown for targeted)
    allow_full = set(criteria.get("full_package_classes") or list(ELIGIBLE_PRODUCT))
    skip_classes = set(criteria.get("skip_full_classes") or list(CONFIRMED_NON_PRODUCT))
    skip_statuses = set(criteria.get("skip_statuses") or list(AUTO_SKIP_STATUSES))

    # Title-confirmed service / construction / irrelevant IT — screen before download
    title = str(item.get("title") or "")
    import re

    if re.search(
        r"(?i)\b("
        r"janitorial\s+services?|pest\s+control\s+services?|painting\s+services?|"
        r"professional\s+services?|consulting\s+services?|staffing\s+services?|"
        r"construction\s+of|roadway\s+reconstruction|bridge\s+replacement|"
        r"software\s+license|saas\s+subscription|microsoft\s+365"
        r")\b",
        title,
    ) and not re.search(
        r"(?i)\b(purchase\s+of|supply\s+of|equipment|supplies?|materials?)\b",
        title,
    ):
        return {
            "disposition": "SKIP_CONFIRMED_NON_PRODUCT",
            "acquire_full_package": False,
            "targeted_only": False,
            "reason": "title_confirmed_non_product_screen",
            "requirement_class": req,
            "solicitation_status": status,
            "record_type": record_type or None,
        }

    if req in skip_classes:
        return {
            "disposition": "SKIP_CONFIRMED_NON_PRODUCT",
            "acquire_full_package": False,
            "targeted_only": False,
            "reason": f"confirmed_class:{req}",
            "requirement_class": req,
            "solicitation_status": status,
        }

    if status in skip_statuses and not criteria.get("include_forecasts"):
        # Unknown status is NOT skipped
        return {
            "disposition": "SKIP_INACTIVE_STATUS",
            "acquire_full_package": False,
            "targeted_only": False,
            "reason": f"status:{status}",
            "requirement_class": req,
            "solicitation_status": status,
        }

    # Optional soft filters — missing values do not exclude
    if criteria.get("require_defined_quantity"):
        qty = item.get("quantity")
        if qty not in (None, "", "unknown") and str(qty).strip() == "":
            pass  # never treat blank as failure for unknown
        # Only exclude when quantity explicitly indicates N/A service-only marker
        if str(qty or "").lower() in {"n/a", "na", "not applicable"}:
            return {
                "disposition": "SKIP_CONFIRMED_NON_PRODUCT",
                "acquire_full_package": False,
                "targeted_only": False,
                "reason": "quantity_not_applicable",
                "requirement_class": req,
                "solicitation_status": status,
            }

    geo = (criteria.get("geo_states") or [])
    if geo:
        st = (item.get("geo_state") or "").upper()
        if st and st not in {g.upper() for g in geo}:
            # Known out-of-area → skip full; unknown geo stays eligible for investigate
            return {
                "disposition": "SKIP_INACTIVE_STATUS",
                "acquire_full_package": False,
                "targeted_only": False,
                "reason": f"geo_mismatch:{st}",
                "requirement_class": req,
                "solicitation_status": status,
            }

    if req in UNCERTAIN:
        return {
            "disposition": "TARGETED_INVESTIGATION",
            "acquire_full_package": False,
            "targeted_only": True,
            "reason": "uncertain_needs_targeted_evidence",
            "requirement_class": req,
            "solicitation_status": status,
        }

    if req in allow_full:
        return {
            "disposition": "ELIGIBLE_FULL_PACKAGE",
            "acquire_full_package": True,
            "targeted_only": False,
            "reason": f"eligible_class:{req}",
            "requirement_class": req,
            "solicitation_status": status,
        }

    # Mixed / other → targeted unless configured
    return {
        "disposition": "TARGETED_INVESTIGATION",
        "acquire_full_package": False,
        "targeted_only": True,
        "reason": f"default_targeted:{req}",
        "requirement_class": req,
        "solicitation_status": status,
    }


def default_auto_criteria() -> dict[str, Any]:
    return {
        "always_acquire_saved": True,
        "full_package_classes": list(ELIGIBLE_PRODUCT),
        "skip_full_classes": list(CONFIRMED_NON_PRODUCT),
        "skip_statuses": list(AUTO_SKIP_STATUSES),
        "include_forecasts": False,
        "require_defined_quantity": False,
    }
