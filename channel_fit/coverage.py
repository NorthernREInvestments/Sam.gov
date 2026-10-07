"""MSRP/public basket coverage + match grades — value-weighted first."""

from __future__ import annotations

from typing import Any

USABLE_MATCH_GRADES = {
    "A_EXACT_CURRENT_PUBLIC",
    "B_EXACT_CURRENT_MANUFACTURER_LIST",
    "C_EXACT_AUTHORIZED_DISTRIBUTOR",
    "D_ALLOWED_EQUAL_CURRENT",
    "E_STRONG_COMPARABLE",
}


def price_match_grade(line: dict[str, Any]) -> str:
    """Classify one priced line. F/G do not count toward usable coverage."""
    explicit = str(line.get("price_match_grade") or line.get("match_grade") or "").upper()
    if explicit in USABLE_MATCH_GRADES or explicit in {"F_WEAK_COMPARABLE", "G_UNKNOWN"}:
        return explicit
    retail = line.get("retail") if isinstance(line.get("retail"), dict) else {}
    source = str(
        line.get("price_source")
        or retail.get("source")
        or retail.get("seller_type")
        or ""
    ).lower()
    if retail.get("unit_price") or retail.get("price") or line.get("unit_price"):
        if "manufacturer" in source or "list" in source or "msrp" in source:
            return "B_EXACT_CURRENT_MANUFACTURER_LIST"
        if "authorized" in source or "distributor" in source:
            return "C_EXACT_AUTHORIZED_DISTRIBUTOR"
        if "equal" in source or line.get("permitted_equal"):
            return "D_ALLOWED_EQUAL_CURRENT"
        if "comparable" in source or "weak" in source:
            return "F_WEAK_COMPARABLE"
        return "A_EXACT_CURRENT_PUBLIC"
    return "G_UNKNOWN"


def _line_extended_value(line: dict[str, Any]) -> float | None:
    for key in ("extended_value", "extended", "line_value", "gov_extended", "extended_price"):
        v = line.get(key)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    qty = line.get("quantity") or line.get("qty")
    unit = line.get("unit_price") or line.get("gov_unit_price") or (line.get("retail") or {}).get("unit_price")
    try:
        if qty is not None and unit is not None:
            return float(qty) * float(unit)
    except (TypeError, ValueError):
        return None
    return None


def _public_unit(line: dict[str, Any]) -> float | None:
    retail = line.get("retail") if isinstance(line.get("retail"), dict) else {}
    for key in ("unit_price", "price", "list_price", "msrp"):
        v = retail.get(key) if key in retail else line.get(key if key != "price" else "public_unit_price")
        if v is None and key in retail:
            continue
        try:
            if retail.get(key) is not None:
                return float(retail[key])
        except (TypeError, ValueError):
            pass
    try:
        if line.get("public_unit_price") is not None:
            return float(line["public_unit_price"])
    except (TypeError, ValueError):
        return None
    return None


def public_price_coverage_class(value_coverage_pct: float | None, *, provisional_line_pct: float | None = None) -> str:
    """Prefer value coverage; fall back to provisional line % only if value unknown."""
    pct = value_coverage_pct
    if pct is None:
        pct = provisional_line_pct
    if pct is None:
        return "INSUFFICIENT"
    if pct < 50:
        return "INSUFFICIENT"
    if pct < 75:
        return "WEAK"
    if pct < 90:
        return "USABLE"
    if pct < 100:
        return "STRONG"
    return "COMPLETE"


def compute_public_basket(lines: list[dict[str, Any]], *, government_value: float | None = None) -> dict[str, Any]:
    """Compute PUBLIC_BASKET_* and VISIBLE_HEADROOM from line evidence. No invention."""
    material = []
    for li in lines or []:
        if not isinstance(li, dict):
            continue
        kind = str(li.get("line_kind") or li.get("kind") or "product").lower()
        if any(x in kind for x in ("install", "service", "labor")):
            continue
        material.append(li)

    total_value = 0.0
    known_value = 0.0
    value_weights_known = 0
    priced_usable = 0
    public_basket = 0.0
    grade_counts: dict[str, int] = {}

    for li in material:
        grade = price_match_grade(li)
        grade_counts[grade] = grade_counts.get(grade, 0) + 1
        ext = _line_extended_value(li)
        if ext is not None and ext > 0:
            total_value += ext
            value_weights_known += 1
            known_value += ext
        pub_unit = _public_unit(li)
        qty = li.get("quantity") or li.get("qty") or 1
        usable = grade in USABLE_MATCH_GRADES and pub_unit is not None
        if usable:
            priced_usable += 1
            try:
                public_basket += float(pub_unit) * float(qty)
            except (TypeError, ValueError):
                pass

    line_count = len(material)
    line_coverage_pct = round(100.0 * priced_usable / line_count, 1) if line_count else 0.0

    # Value-weighted: among lines with known extended value, share whose match is usable A–E
    covered_value = 0.0
    for li in material:
        grade = price_match_grade(li)
        ext = _line_extended_value(li)
        if ext is None or ext <= 0:
            continue
        if grade in USABLE_MATCH_GRADES and _public_unit(li) is not None:
            covered_value += ext

    value_coverage_pct = None
    if known_value > 0:
        value_coverage_pct = round(100.0 * covered_value / known_value, 1)

    coverage_class = public_price_coverage_class(
        value_coverage_pct,
        provisional_line_pct=line_coverage_pct if value_coverage_pct is None else None,
    )

    gov = None
    try:
        if government_value is not None:
            gov = float(government_value)
    except (TypeError, ValueError):
        gov = None

    visible_headroom = None
    visible_headroom_pct = None
    if gov is not None and public_basket > 0 and coverage_class in {"USABLE", "STRONG", "COMPLETE"}:
        visible_headroom = round(gov - public_basket, 2)
        visible_headroom_pct = round(100.0 * visible_headroom / gov, 1) if gov else None

    return {
        "PUBLIC_BASKET_VALUE": round(public_basket, 2) if public_basket else None,
        "PUBLIC_BASKET_LINE_COVERAGE": line_coverage_pct,
        "PUBLIC_BASKET_VALUE_COVERAGE": value_coverage_pct,
        "PUBLIC_PRICE_COVERAGE_CLASS": coverage_class,
        "VISIBLE_HEADROOM": visible_headroom,
        "VISIBLE_HEADROOM_PERCENT": visible_headroom_pct,
        "government_value": gov,
        "material_line_count": line_count,
        "priced_usable_lines": priced_usable,
        "value_weights_known_lines": value_weights_known,
        "price_match_grades": grade_counts,
        "coverage_provisional_line_only": value_coverage_pct is None,
    }
