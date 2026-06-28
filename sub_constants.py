"""Subcontractor search status values and search-term mapping."""

from __future__ import annotations

SUB_STATUSES: tuple[str, ...] = (
    "Not Contacted",
    "Called — Left Voicemail",
    "Spoke With — Interested",
    "Spoke With — Not Interested",
    "Quote Received",
    "Selected",
)

DEFAULT_SUB_STATUS = SUB_STATUSES[0]

AUTO_SUB_SEARCH_MIN_SCORE = 6


def normalize_sub_type(sub_type: str | None) -> str:
    if not sub_type:
        return "general"
    return sub_type.strip()[:128] or "general"


def search_terms_for_sub_type(sub_type: str | None) -> list[str]:
    """Return Google Places text queries for the subcontractor type."""
    text = (sub_type or "").lower()
    if any(k in text for k in ("janitorial", "custodial", "cleaning", "custodian")):
        return ["commercial janitorial service", "commercial cleaning company"]
    if any(k in text for k in ("landscap", "grounds", "lawn", "mowing", "yard")):
        return ["commercial landscaping", "grounds maintenance company"]
    if "pest" in text:
        return ["commercial pest control"]
    if "security" in text or "guard" in text:
        return ["security guard company"]
    if "hvac" in text or "heating" in text or "air conditioning" in text:
        return ["commercial HVAC service"]
    cleaned = (sub_type or "commercial service company").strip()
    return [cleaned] if cleaned else ["commercial service company"]


def classify_sub_type(sub_type: str | None) -> str:
    """Short category label stored on master sub records."""
    text = (sub_type or "").lower()
    if any(k in text for k in ("janitorial", "custodial", "cleaning")):
        return "janitorial"
    if any(k in text for k in ("landscap", "grounds", "lawn", "mowing")):
        return "landscaping"
    if "pest" in text:
        return "pest control"
    if "security" in text or "guard" in text:
        return "security"
    if "hvac" in text:
        return "hvac"
    return normalize_sub_type(sub_type)[:64]
