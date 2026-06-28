"""Default owner and business information for proposal generation."""

from __future__ import annotations

DEFAULT_OWNER_SETTINGS: dict[str, str | float | int] = {
    "legal_business_name": "Northern RE Investments, LLC",
    "owner_name": "Mark Graham II",
    "owner_title": "Owner",
    "primary_naics_code": "561720",
    "business_state": "Wyoming",
    "business_phone": "970-380-0862",
    "business_email": "NorthernREIncestments@outlook.com",
    "address_line_1": "",
    "address_line_2": "",
    "city": "",
    "state": "WY",
    "zip": "",
    "uei": "",
    "cage_code": "",
    "ein": "",
    "sam_expiration": "",
    "default_margin_pct": 18,
    "default_option_year_increase_pct": 3,
    "commercial_experience": (
        "Pipeline construction and soil reclamation — managing field crews to strict performance "
        "standards, coordinating logistics across job sites, and maintaining environmental and "
        "safety compliance. Retail management — staff performance, operational consistency, "
        "customer service accountability, and attention to detail in daily operations."
    ),
    "certifications": "",
    "past_performance": "",
    "small_business_size_standard": "22000000",
}

PROPOSAL_STATUSES = ("draft", "ready", "submitted", "won", "lost")

PROPOSAL_SECTIONS = (
    "cover_letter",
    "technical_approach",
    "price_schedule",
    "past_performance",
    "capability_statement",
    "certifications",
)

SECTION_TITLES = {
    "cover_letter": "Cover Letter",
    "technical_approach": "Technical Approach",
    "price_schedule": "Price Schedule",
    "past_performance": "Past Performance",
    "capability_statement": "Capability Statement",
    "certifications": "Certifications and Representations",
}
