"""Missing-information / clarification vocabulary and fact classification.

CO_CLARIFICATION_REQUIRED is intentionally hard to reach.
"""

from __future__ import annotations

from typing import Any

# Lifecycle statuses
MISSING_UNCHECKED = "MISSING_UNCHECKED"
SEARCHING_LOCAL_PACKAGE = "SEARCHING_LOCAL_PACKAGE"
POSSIBLE_MATCH_FOUND = "POSSIBLE_MATCH_FOUND"
MISSING_CONFIRMED_LOCAL = "MISSING_CONFIRMED_LOCAL"
EXTERNAL_REFERENCE_REQUIRED = "EXTERNAL_REFERENCE_REQUIRED"
CO_CLARIFICATION_CANDIDATE = "CO_CLARIFICATION_CANDIDATE"
CO_CLARIFICATION_REQUIRED = "CO_CLARIFICATION_REQUIRED"
RESOLVED = "RESOLVED"
NOT_APPLICABLE = "NOT_APPLICABLE"

MISSING_STATUSES = frozenset(
    {
        MISSING_UNCHECKED,
        SEARCHING_LOCAL_PACKAGE,
        POSSIBLE_MATCH_FOUND,
        MISSING_CONFIRMED_LOCAL,
        EXTERNAL_REFERENCE_REQUIRED,
        CO_CLARIFICATION_CANDIDATE,
        CO_CLARIFICATION_REQUIRED,
        RESOLVED,
        NOT_APPLICABLE,
    }
)

# Fact domains — only SOLICITATION_FACT normally becomes CO candidate
FACT_SOLICITATION = "SOLICITATION_FACT"
FACT_COMMERCIAL = "COMMERCIAL_FACT"
FACT_MANUFACTURER = "MANUFACTURER_FACT"
FACT_REGULATORY = "REGULATORY_FACT"
FACT_FINANCING = "FINANCING_FACT"
FACT_OTHER = "OTHER"

FACT_CLASSES = frozenset(
    {
        FACT_SOLICITATION,
        FACT_COMMERCIAL,
        FACT_MANUFACTURER,
        FACT_REGULATORY,
        FACT_FINANCING,
        FACT_OTHER,
    }
)

# Match classes
MATCH_DIRECT = "DIRECT_MATCH"
MATCH_RELATED = "RELATED_MATCH"
MATCH_POSSIBLE_INDIRECT = "POSSIBLE_INDIRECT_MATCH"
MATCH_NONE = "NO_MATCH"

# Second-pass AI review (architecture only — no live AI in this module)
SECOND_PASS_NOT_NEEDED = "NOT_NEEDED"
SECOND_PASS_PENDING = "PENDING_AUTHORIZED"
SECOND_PASS_COMPLETE = "COMPLETE"
SECOND_PASS_SKIPPED = "SKIPPED"

CONFIDENCE_LOW = "LOW"
CONFIDENCE_MEDIUM = "MEDIUM"
CONFIDENCE_HIGH = "HIGH"

# Synonym / alternate terminology maps for common product-resale gaps
SYNONYM_BANK: dict[str, list[str]] = {
    "memory_quantity": [
        "memory",
        "RAM",
        "RDIMM",
        "DIMM",
        "16GB",
        "6400MT/s",
        "6400MT",
        "memory configuration",
        "memory capacity",
        "memory module",
        "DIMM quantity",
        "GB RDIMM",
        "Single Rank",
        "dual rank",
        "populated",
        "slots",
    ],
    "storage_quantity": [
        "storage",
        "hard drive",
        "HDD",
        "SSD",
        "SAS",
        "2.4TB",
        "2.4 TB",
        "drive",
        "drives",
        "disk",
        "bay",
        "quantity",
        "qty",
        "RAID",
        "PERC",
    ],
    "installation": [
        "installation",
        "install",
        "setup",
        "rack",
        "on-site",
        "onsite",
        "assembly",
        "deployment",
        "services included",
        "FOB destination installation",
    ],
    "freight_fob": [
        "FOB",
        "freight",
        "shipping",
        "delivery terms",
        "ship to",
        "transportation",
        "destination",
        "origin",
        "carrier",
        "prepaid",
        "collect",
        "shipping cost",
    ],
    "oem_letter": [
        "OEM",
        "authorization",
        "authorized dealer",
        "authorized distributor",
        "reseller letter",
        "manufacturer letter",
        "letter of authorization",
        "LOA",
    ],
}


def classify_fact_class(fact_key: str, *, hint: str | None = None) -> str:
    if hint and hint in FACT_CLASSES:
        return hint
    k = str(fact_key or "").lower()
    if any(x in k for x in ("price", "quote", "availability", "lead_time", "stock", "supplier")):
        return FACT_COMMERCIAL
    if any(x in k for x in ("financing", "pg", "personal_credit", "cash_upfront")):
        return FACT_FINANCING
    if any(x in k for x in ("dell_config", "part_number_decode", "sku_decode", "bom_from_mfr")):
        return FACT_MANUFACTURER
    if any(x in k for x in ("far_", "baa_clause", "taa_clause", "regulation")):
        return FACT_REGULATORY
    if any(
        x in k
        for x in (
            "memory",
            "storage",
            "install",
            "freight",
            "fob",
            "quantity",
            "delivery",
            "oem",
            "set_aside",
            "solicitation",
            "clin",
            "bom",
        )
    ):
        return FACT_SOLICITATION
    return FACT_OTHER


def search_terms_for(fact_key: str, extra: list[str] | None = None) -> list[str]:
    key = str(fact_key or "").lower()
    terms: list[str] = []
    if "memory" in key or "rdimm" in key or "dimm" in key or "ram" in key:
        terms.extend(SYNONYM_BANK["memory_quantity"])
    if "storage" in key or "drive" in key or "disk" in key or "hdd" in key or "ssd" in key:
        terms.extend(SYNONYM_BANK["storage_quantity"])
    if "install" in key:
        terms.extend(SYNONYM_BANK["installation"])
    if "freight" in key or "fob" in key or "ship" in key:
        terms.extend(SYNONYM_BANK["freight_fob"])
    if "oem" in key or "authoriz" in key:
        terms.extend(SYNONYM_BANK["oem_letter"])
    for t in extra or []:
        if t and t not in terms:
            terms.append(t)
    # Always include the raw key tokens
    for tok in key.replace("_", " ").split():
        if tok and tok not in terms:
            terms.append(tok)
    # Deduplicate preserve order
    seen: set[str] = set()
    out: list[str] = []
    for t in terms:
        tl = t.lower()
        if tl not in seen:
            seen.add(tl)
            out.append(t)
    return out


def missing_info_record(
    *,
    fact_key: str,
    description: str,
    fact_class: str | None = None,
    status: str = MISSING_UNCHECKED,
    necessary_for_bid: bool = True,
    necessary_for_execution: bool = True,
) -> dict[str, Any]:
    fc = classify_fact_class(fact_key, hint=fact_class)
    return {
        "fact_key": fact_key,
        "description": description,
        "fact_class": fc,
        "status": status if status in MISSING_STATUSES else MISSING_UNCHECKED,
        "necessary_for_bid": necessary_for_bid,
        "necessary_for_execution": necessary_for_execution,
        "search_audit": None,
        "second_pass_status": SECOND_PASS_NOT_NEEDED,
        "resolved_value": None,
        "resolved_by": None,
        "already_answered": False,
    }
