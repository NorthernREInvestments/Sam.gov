"""SIMPLE_RESALE score — prefer high-profit ordinary commercial goods."""

from __future__ import annotations

import re
from typing import Any

_NEG = re.compile(
    r"\b(install|installation|labor|hazmat|hazardous|cold\s*chain|refrigerat|"
    r"oversized|crane|rigging|source\s*approv|manufacturer\s*authoriz|OEM\s*only|"
    r"controlled|ITAR|custom\s*fabricat|bond(ing|ed)?|certified\s*only|"
    r"staging|multiple\s*deliver)\b",
    re.I,
)
_POS = re.compile(
    r"\b(office|janitorial|plumbing|ppe|glove|towel|bulb|lamp|tool|hardware|"
    r"furniture|supply|supplies|paper|cleaner|filter)\b",
    re.I,
)


def simple_resale_score(
    *,
    lines: list[dict[str, Any]],
    title: str | None = None,
    delivery_locations: int | None = None,
    public_pricing_available: bool = False,
    multiple_public_suppliers: bool = False,
    install_required: bool | None = None,
    bonding_required: bool | None = None,
    special_certification: bool | None = None,
    manufacturer_authorization: bool | None = None,
    source_approval: bool | None = None,
    exact_source_restriction: bool | None = None,
) -> dict[str, Any]:
    score = 50
    factors: list[str] = []
    text = " ".join(
        [
            title or "",
            " ".join(str(l.get("product_description") or "") for l in lines[:40]),
        ]
    )
    n = len(lines)
    if 1 <= n <= 50:
        score += 12
        factors.append("+line_count_1_50")
    elif n > 100:
        score -= 10
        factors.append("-too_many_lines")

    if _POS.search(text):
        score += 3  # ease signal only — not economic truth
        factors.append("+ordinary_commercial_ease")
    if _NEG.search(text):
        score -= 15
        factors.append("-specialized_language")

    if public_pricing_available:
        score += 10
        factors.append("+public_pricing")
    if multiple_public_suppliers:
        score += 8
        factors.append("+multi_supplier")

    flags = {
        "install_required": install_required,
        "bonding_required": bonding_required,
        "special_certification": special_certification,
        "manufacturer_authorization": manufacturer_authorization,
        "source_approval": source_approval,
        "exact_source_restriction": exact_source_restriction,
    }
    for k, v in flags.items():
        if v is True:
            score -= 12
            factors.append(f"-{k}")
        elif v is False:
            score += 4
            factors.append(f"+no_{k}")

    if delivery_locations is not None:
        if delivery_locations <= 2:
            score += 6
            factors.append("+few_delivery_locations")
        elif delivery_locations >= 10:
            score -= 12
            factors.append("-many_delivery_locations")

    # Identity ease
    exact = sum(1 for l in lines if str(l.get("identity_class") or "").startswith("EXACT"))
    if n and exact / n >= 0.7:
        score += 8
        factors.append("+mostly_exact_identity")

    score = max(0, min(100, score))
    band = "HIGH" if score >= 70 else ("MEDIUM" if score >= 45 else "LOW")
    return {
        "simple_resale_score": score,
        "band": band,
        "factors": factors,
    }
