"""Subcontractor search status values and search-term mapping."""

from __future__ import annotations

from naics_labels import NAICS_SEARCH_TERMS

SUB_STATUSES: tuple[str, ...] = (
    "Not Contacted",
    "Called — Left Voicemail",
    "Spoke With — Interested",
    "Spoke With — Not Interested",
    "Quote Received",
    "Selected",
)

AGREEMENT_SIGNATURE_STATUSES: tuple[str, ...] = (
    "Agreement Not Generated",
    "Agreement Sent",
    "Agreement Signed",
    "Agreement Declined",
)

DEFAULT_AGREEMENT_SIGNATURE_STATUS = AGREEMENT_SIGNATURE_STATUSES[0]

DEFAULT_SUB_STATUS = SUB_STATUSES[0]

AUTO_SUB_SEARCH_MIN_SCORE = 6

_NAICS_CATEGORY: dict[str, str] = {
    "561720": "janitorial",
    "561210": "facilities support",
    "561790": "janitorial",
    "561740": "janitorial",
    "561730": "landscaping",
    "561710": "pest control",
    "562111": "waste",
    "562119": "waste",
    "561439": "document shredding",
    "541930": "translation",
    "811192": "vehicle washing",
    "238220": "hvac maintenance",
    "562910": "remediation",
    "484210": "moving",
    "484110": "freight",
    "492110": "courier",
    "711320": "photography",
    "532490": "equipment rental",
    "561422": "telephone answering",
}


def normalize_sub_type(sub_type: str | None) -> str:
    if not sub_type:
        return "general"
    return sub_type.strip()[:128] or "general"


def search_terms_for_naics(naics_code: str | None) -> list[str] | None:
    code = str(naics_code or "").strip()
    if not code:
        return None
    return NAICS_SEARCH_TERMS.get(code)


def _sub_type_is_specific(sub_type: str | None) -> bool:
    if not sub_type or not str(sub_type).strip():
        return False
    generic = {
        "general",
        "commercial service company",
        "subcontractor",
        "local subcontractor",
        "service provider",
        "unknown",
    }
    return str(sub_type).strip().lower() not in generic


def search_terms_for_sub_type(sub_type: str | None) -> list[str]:
    """Return Google Places text queries inferred from Claude sub_type text."""
    text = (sub_type or "").lower()
    if any(
        k in text
        for k in (
            "facilities maintenance",
            "facilities support",
            "building maintenance",
            "general maintenance",
            "integrated facilities",
            "mro",
        )
    ):
        return ["facilities maintenance company", "commercial building maintenance"]
    if any(k in text for k in ("janitorial", "custodial", "cleaning", "custodian", "carpet")):
        return ["commercial cleaning company", "janitorial services"]
    if any(k in text for k in ("landscap", "grounds", "lawn", "mowing", "yard")):
        return ["commercial landscaping", "grounds maintenance"]
    if any(k in text for k in ("pest", "exterminat", "rodent", "termite")):
        return ["commercial pest control", "exterminator"]
    if any(k in text for k in ("shred", "document destruction", "records destruction")):
        return ["document shredding service", "secure document destruction"]
    if any(k in text for k in ("translat", "interpret")):
        return ["translation services", "interpretation services"]
    if any(k in text for k in ("car wash", "vehicle wash", "fleet wash")):
        return ["fleet vehicle washing", "mobile car wash"]
    if any(k in text for k in ("hvac", "plumb", "heating", "air conditioning")):
        return ["commercial HVAC maintenance", "plumbing maintenance"]
    if any(k in text for k in ("waste", "trash", "hauling", "dumpster", "recycl")):
        return ["waste removal company", "waste hauling"]
    if any(k in text for k in ("remediation", "environmental cleanup", "soil", "hazmat")):
        return ["environmental remediation", "soil remediation company"]
    if any(k in text for k in ("moving", "relocation", "office move")):
        return ["commercial moving company", "office relocation"]
    if any(k in text for k in ("courier", "messenger", "same day delivery")):
        return ["courier service", "same day delivery service"]
    if any(k in text for k in ("freight", "trucking", "delivery", "logistics")):
        return ["freight trucking", "local freight delivery"]
    if any(k in text for k in ("photograph", "videograph", "photo")):
        return ["commercial photography", "event photographer"]
    if any(k in text for k in ("equipment rental", "equipment leasing", "rental company")):
        return ["equipment rental company", "commercial equipment leasing"]
    if any(k in text for k in ("answering service", "call center", "telephone answering")):
        return ["telephone answering service", "call center service"]
    if "security" in text or "guard" in text:
        return ["security guard company"]
    cleaned = (sub_type or "commercial service company").strip()
    return [cleaned] if cleaned else ["commercial service company"]


def resolve_search_terms(
    sub_type_needed: str | None = None,
    naics_code: str | None = None,
) -> list[str]:
    """Prefer Claude sub_type from attachment review; fall back to NAICS defaults."""
    if _sub_type_is_specific(sub_type_needed):
        terms = search_terms_for_sub_type(sub_type_needed)
        if terms and terms != ["commercial service company"]:
            return terms
    naics_terms = search_terms_for_naics(naics_code)
    if naics_terms:
        return naics_terms
    return search_terms_for_sub_type(sub_type_needed)


def classify_sub_type(sub_type_needed: str | None = None, naics_code: str | None = None) -> str:
    """Short category label stored on master sub records."""
    text = (sub_type_needed or "").lower()
    if any(
        k in text
        for k in (
            "facilities maintenance",
            "facilities support",
            "building maintenance",
            "general maintenance",
            "integrated facilities",
        )
    ):
        return "facilities support"
    if any(k in text for k in ("janitorial", "custodial", "cleaning", "carpet")):
        return "janitorial"
    if any(k in text for k in ("landscap", "grounds", "lawn", "mowing")):
        return "landscaping"
    if any(k in text for k in ("pest", "exterminat")):
        return "pest control"
    if any(k in text for k in ("shred", "document destruction")):
        return "document shredding"
    if any(k in text for k in ("translat", "interpret")):
        return "translation"
    if any(k in text for k in ("car wash", "vehicle wash")):
        return "vehicle washing"
    if any(k in text for k in ("hvac", "plumb")):
        return "hvac maintenance"
    if any(k in text for k in ("waste", "trash", "hauling")):
        return "waste"
    if any(k in text for k in ("remediation", "environmental cleanup")):
        return "remediation"
    if any(k in text for k in ("moving", "relocation")):
        return "moving"
    if any(k in text for k in ("courier", "messenger")):
        return "courier"
    if any(k in text for k in ("freight", "trucking")):
        return "freight"
    if any(k in text for k in ("photograph", "videograph")):
        return "photography"
    if "rental" in text or "leasing" in text:
        return "equipment rental"
    if any(k in text for k in ("answering", "call center")):
        return "telephone answering"

    code = str(naics_code or "").strip()
    if code in _NAICS_CATEGORY:
        return _NAICS_CATEGORY[code]
    return normalize_sub_type(sub_type_needed)[:64]
