"""Authoritative line inventory + materiality scoring + conservation."""

from __future__ import annotations

import json
import re
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from line_basket_completion_strict_economics.models import (
    BUILD,
    CONSERVATION_BUCKETS,
    EXECUTION_BLOCKED,
    IDENTITY_UNRESOLVED,
    LINE_INVENTORY,
    NO_PUBLIC_PRICE,
    NON_MATERIAL,
    OTHER_EXPLAINED,
    P0,
    P1,
    P2,
    P3,
    PRICED_EXECUTABLE,
    PRIOR_ACQ_CK,
    PRIOR_FUNNEL_CK,
    QUOTE_REQUIRED,
)
from line_basket_completion_strict_economics.provenance import classify_price_record
from m3_data_root import data_path

_INSTALL = re.compile(
    r"\b(furnish\s+and\s+install|install(?:ation)?|removal|grading|tilling|planting\s+soil|"
    r"labor|contractor|site\s+work|construction)\b",
    re.I,
)
_DE_MINIMIS_HINT = re.compile(r"\b(label|sticker|bag|tie|clip|screw|nail|washer)\b", re.I)
_NON_PROCUREMENT = re.compile(
    r"\b(liability\s+coverage|insurance|workers?\s+comp|indemnif|bond(?:ing)?\s+shall|"
    r"limits?\s+of\s+not\s+less|certificate\s+of\s+insurance|hold\s+harmless)\b",
    re.I,
)
_MAX_SANE_QTY = 100_000.0
_MAX_LINE_EST_VALUE = 2_000_000.0


def _sanitize_quantity(raw: Any, description: str | None = None) -> float:
    """Reject OCR noise that turns insurance limits into quantities."""
    try:
        qty = float(raw if raw is not None else 1)
    except Exception:
        qty = 1.0
    if qty <= 0:
        qty = 1.0
    if qty > _MAX_SANE_QTY:
        return 1.0
    if description and _NON_PROCUREMENT.search(description) and qty >= 1000:
        return 1.0
    return qty


def _load(name: str) -> dict[str, Any]:
    p = data_path(name)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(name: str, payload: dict[str, Any]) -> None:
    p = data_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _d(v: Any) -> Decimal:
    try:
        return Decimal(str(v or 0))
    except Exception:
        return Decimal("0")


def _money(v: Decimal | float | int | None) -> float:
    if v is None:
        return 0.0
    return float(Decimal(str(v)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _estimate_unit_value(line: dict[str, Any]) -> float:
    """Defensible relative unit value for materiality — never invented wholesale cost."""
    desc = f"{line.get('description') or ''} {line.get('manufacturer') or ''}"
    if _NON_PROCUREMENT.search(desc):
        return 1.0
    gov = _d(line.get("government_line_value") or line.get("gov_unit"))
    if 0 < gov <= _MAX_LINE_EST_VALUE:
        return float(gov)
    cat = str(line.get("category") or "")
    base = 50.0
    if _INSTALL.search(desc) or cat == "CONSTRUCTION_INSTALL":
        base = 200.0
    elif any(x in cat for x in ("HVAC", "ELECTRICAL", "AUTO")):
        base = 150.0
    elif any(x in cat for x in ("TOOLS", "PLUMBING", "MRO")):
        base = 75.0
    elif _DE_MINIMIS_HINT.search(desc):
        base = 5.0
    if line.get("mpn"):
        base *= 1.2
    prior = line.get("prior_acq") or {}
    audit = classify_price_record(prior) if prior else {}
    if audit.get("is_valid_production") and audit.get("unit_cost"):
        base = max(base, float(audit["unit_cost"]))
    return min(base, _MAX_LINE_EST_VALUE)


def score_materiality(line: dict[str, Any], *, basket_value_total: float) -> dict[str, Any]:
    desc = str(line.get("description") or "")
    if _NON_PROCUREMENT.search(desc):
        return {
            "materiality_tier": P3,
            "materiality_score": 0.0,
            "estimated_unit_value": 1.0,
            "estimated_extended_value": 1.0,
            "basket_share": 0.0,
            "non_procurement_noise": True,
        }
    qty = _sanitize_quantity(line.get("quantity"), desc)
    line["quantity"] = qty
    unit_est = float(line.get("estimated_unit_value") or _estimate_unit_value(line))
    ext = min(unit_est * qty, _MAX_LINE_EST_VALUE)
    share = (ext / basket_value_total) if basket_value_total > 0 else 0.0
    gov = float(line.get("government_line_value") or 0) or 0.0
    if gov > _MAX_LINE_EST_VALUE:
        gov = 0.0

    if share >= 0.05 or ext >= 2500 or gov >= 2500:
        tier = P0
    elif share >= 0.01 or ext >= 500 or gov >= 500 or (line.get("mpn") and ext >= 100):
        tier = P1
    elif share >= 0.002 or ext >= 50:
        tier = P2
    else:
        tier = P3

    return {
        "materiality_tier": tier,
        "materiality_score": round(share * 1000 + ext / 100.0, 4),
        "estimated_unit_value": round(unit_est, 2),
        "estimated_extended_value": round(ext, 2),
        "basket_share": round(share, 6),
    }


def classify_category(line: dict[str, Any]) -> str:
    from product_category_yield import classify_product_category

    blob = f"{line.get('description') or ''} {line.get('manufacturer') or ''} {line.get('mpn') or ''}"
    if _INSTALL.search(blob) and not line.get("mpn"):
        return "CONSTRUCTION_INSTALL"
    cat = classify_product_category(
        title=str(line.get("description") or "")[:120],
        description=blob,
        category=str(line.get("manufacturer") or ""),
    ).get("category") or "UNKNOWN"
    mapping = {
        "TOOLS": "TOOLS",
        "PLUMBING": "PLUMBING",
        "HVAC": "HVAC",
        "ELECTRICAL": "ELECTRICAL",
        "SAFETY_PPE": "PPE",
        "OFFICE_FURNITURE": "FURNITURE",
        "PARTS": "AUTO_HD",
        "INDUSTRIAL_EQUIPMENT": "MRO",
        "JANITORIAL_FACILITY_SUPPLIES": "MRO",
        "AGRICULTURAL_GROUNDS": "IRRIGATION",
        "BUILDING_MATERIALS": "MRO",
        "IT_COMPUTERS": "OFFICE",
        "ELECTRONICS": "ELECTRICAL",
    }
    if "light" in blob.lower() or "lamp" in blob.lower() or "bulb" in blob.lower():
        return "LIGHTING"
    return mapping.get(cat, "MRO" if cat not in {"UNKNOWN", "LIKELY_SERVICE_FALSE_POSITIVE", "MIXED_GOODS_SERVICES"} else "OTHER")


def enumerate_opportunity_lines(opportunity_id: str) -> list[dict[str, Any]]:
    from evidence_breakthrough.corpus import load_identity_store

    id_pack = (load_identity_store().get("by_opportunity") or {}).get(opportunity_id) or {}
    identities = list(id_pack.get("identities") or [])
    if len(identities) > 600:
        identities = identities[:600]

    acq = _load(PRIOR_ACQ_CK)
    by_line = acq.get("by_line") or {}
    acq_lines = [r for r in by_line.values() if r.get("opportunity_id") == opportunity_id]
    acq_by_mpn: dict[str, dict[str, Any]] = {}
    for row in acq_lines:
        ident = row.get("identity") or {}
        mpn = str(ident.get("part_number") or row.get("mpn") or "").strip()
        if not mpn and isinstance(row.get("key"), str) and "::" in row["key"]:
            mpn = row["key"].rsplit("::", 1)[-1].strip()
        if mpn:
            acq_by_mpn[mpn.upper()] = row

    # Prior funnel lines for seller evidence
    funnel = _load(PRIOR_FUNNEL_CK)
    prior_opp = (funnel.get("opportunities") or {}).get(opportunity_id) or {}
    prior_line_block = prior_opp.get("lines") or {}
    prior_list = prior_line_block.get("lines") or []
    prior_funnel_lines = {ln.get("line_key"): ln for ln in prior_list if isinstance(ln, dict)}

    lines: list[dict[str, Any]] = []
    seen: set[str] = set()

    for idx, ident in enumerate(identities):
        if not isinstance(ident, dict):
            continue
        mpn = str(ident.get("part_number") or ident.get("mpn") or "").strip() or None
        line_id = f"{opportunity_id}|{mpn or f'idx-{idx+1}'}"
        if line_id in seen:
            continue
        seen.add(line_id)
        prior = acq_by_mpn.get((mpn or "").upper()) if mpn else None
        funnel_prior = prior_funnel_lines.get(line_id) or prior_funnel_lines.get(f"{opportunity_id}|{mpn}")
        desc = ident.get("raw_description") or ident.get("description")
        qty = _sanitize_quantity(ident.get("quantity") or (prior or {}).get("quantity") or 1, desc)
        row = {
            "opportunity_id": opportunity_id,
            "line_id": line_id,
            "clin": ident.get("clin") or ident.get("line_number") or str(idx + 1),
            "description": desc,
            "manufacturer": ident.get("manufacturer"),
            "mpn": mpn,
            "model": ident.get("model"),
            "quantity": qty,
            "uom": ident.get("uom") or ident.get("unit_of_measure") or "EA",
            "pack": ident.get("pack") or ident.get("pack_qty") or 1,
            "equal_rule": ident.get("equal_rule") or ident.get("brand_name_or_equal"),
            "condition": ident.get("condition") or "NEW",
            "government_line_value": ident.get("government_line_value") or ident.get("unit_price") or ident.get("extended_price"),
            "prior_acq": prior,
            "prior_funnel": funnel_prior,
            "identity": ident,
        }
        row["category"] = classify_category(row)
        row["estimated_unit_value"] = _estimate_unit_value(row)
        lines.append(row)

    for row in acq_lines:
        ident = row.get("identity") or {}
        mpn = str(ident.get("part_number") or row.get("mpn") or "").strip() or None
        if not mpn and isinstance(row.get("key"), str) and "::" in row["key"]:
            mpn = row["key"].rsplit("::", 1)[-1].strip() or None
        line_id = row.get("key") or f"{opportunity_id}|{mpn or 'acq'}"
        if line_id in seen or (mpn and f"{opportunity_id}|{mpn}" in seen):
            continue
        seen.add(line_id)
        desc = ident.get("description") or row.get("description")
        out = {
            "opportunity_id": opportunity_id,
            "line_id": line_id,
            "clin": row.get("clin"),
            "description": desc,
            "manufacturer": ident.get("manufacturer") or row.get("manufacturer"),
            "mpn": mpn,
            "model": ident.get("model"),
            "quantity": _sanitize_quantity(row.get("quantity") or 1, desc),
            "uom": row.get("uom") or "EA",
            "pack": row.get("pack") or 1,
            "equal_rule": row.get("equal_rule"),
            "condition": row.get("condition") or "NEW",
            "government_line_value": row.get("gov_unit"),
            "prior_acq": row,
            "prior_funnel": None,
            "identity": ident or row,
        }
        out["category"] = classify_category(out)
        out["estimated_unit_value"] = _estimate_unit_value(out)
        lines.append(out)

    basket_total = sum(float(l.get("estimated_unit_value") or 0) * float(l.get("quantity") or 1) for l in lines) or 1.0
    for l in lines:
        l.update(score_materiality(l, basket_value_total=basket_total))
    return lines


def conservation_bucket(terminal: str | None) -> str:
    t = str(terminal or OTHER_EXPLAINED)
    if t == PRICED_EXECUTABLE:
        return PRICED_EXECUTABLE
    if t in {QUOTE_REQUIRED, "OEM_QUOTE_REQUIRED", "DISTRIBUTOR_QUOTE_REQUIRED", "SUPPLIER_QUOTE_REQUIRED"}:
        return QUOTE_REQUIRED
    if t in {IDENTITY_UNRESOLVED, "IDENTITY_AMBIGUOUS"}:
        return IDENTITY_UNRESOLVED
    if t in {NO_PUBLIC_PRICE, "PUBLIC_PRICE_UNAVAILABLE", "TIME_BUDGET_EXHAUSTED", "RESEARCH_EXHAUSTED"}:
        # TIME_BUDGET is tracked separately in exhaustion audit; conservation maps to NO_PUBLIC_PRICE only when truly market
        if t == "TIME_BUDGET_EXHAUSTED":
            return OTHER_EXPLAINED
        return NO_PUBLIC_PRICE
    if t == NON_MATERIAL:
        return NON_MATERIAL
    if t in {EXECUTION_BLOCKED, "NO_COMPLIANT_SOURCE", "UOM_UNRESOLVED", "PACK_UNRESOLVED"}:
        return EXECUTION_BLOCKED
    return OTHER_EXPLAINED


def apply_conservation(lines: list[dict[str, Any]]) -> dict[str, Any]:
    source = len(lines)
    buckets = {b: 0 for b in CONSERVATION_BUCKETS}
    for ln in lines:
        b = conservation_bucket(ln.get("terminal_state") or ln.get("acquisition_status"))
        ln["conservation_bucket"] = b
        buckets[b] = buckets.get(b, 0) + 1
    summed = sum(buckets.values())
    diff = source - summed
    return {
        "SOURCE_LINES": source,
        "buckets": buckets,
        "SUM": summed,
        "DIFF": diff,
        "ok": diff == 0 and summed == source,
    }


def coverage_metrics(lines: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(lines) or 1
    priced = [l for l in lines if l.get("terminal_state") == PRICED_EXECUTABLE and l.get("price_origin")]
    # Material = P0+P1 (and not NON_MATERIAL)
    material = [l for l in lines if l.get("materiality_tier") in {P0, P1} and l.get("terminal_state") != NON_MATERIAL]
    if not material:
        material = [l for l in lines if l.get("materiality_tier") != P3 and l.get("terminal_state") != NON_MATERIAL]
    material_priced = [l for l in material if l.get("terminal_state") == PRICED_EXECUTABLE]

    line_count_cov = len(priced) / total
    qty_tot = sum(float(l.get("quantity") or 1) for l in lines) or 1.0
    qty_priced = sum(float(l.get("quantity") or 1) for l in priced)
    qty_cov = qty_priced / qty_tot

    val_tot = sum(float(l.get("estimated_extended_value") or 0) for l in lines) or 1.0
    val_priced = sum(float(l.get("estimated_extended_value") or 0) for l in priced)
    value_weighted = val_priced / val_tot

    mat_val_tot = sum(float(l.get("estimated_extended_value") or 0) for l in material) or 1.0
    mat_val_priced = sum(float(l.get("estimated_extended_value") or 0) for l in material_priced)
    # Also count terminal material lines with conservative bounds toward coverage only when priced
    material_value_coverage = mat_val_priced / mat_val_tot

    return {
        "LINE_COUNT_COVERAGE": round(line_count_cov, 4),
        "QUANTITY_COVERAGE": round(qty_cov, 4),
        "VALUE_WEIGHTED_COVERAGE": round(value_weighted, 4),
        "MATERIAL_VALUE_COVERAGE": round(material_value_coverage, 4),
        "MATERIAL_BASKET_COVERAGE": round(material_value_coverage, 4),
        "material_lines": len(material),
        "material_priced": len(material_priced),
        "priced_executable": len(priced),
        "total_lines": len(lines),
        "p0": sum(1 for l in lines if l.get("materiality_tier") == P0),
        "p1": sum(1 for l in lines if l.get("materiality_tier") == P1),
        "p2": sum(1 for l in lines if l.get("materiality_tier") == P2),
        "p3": sum(1 for l in lines if l.get("materiality_tier") == P3),
    }


def persist_line_inventory(by_opportunity: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    slim: dict[str, Any] = {}
    totals = {"opportunities": 0, "lines": 0, "p0": 0, "p1": 0, "p2": 0, "p3": 0}
    for oid, lines in by_opportunity.items():
        totals["opportunities"] += 1
        totals["lines"] += len(lines)
        for l in lines:
            tier = l.get("materiality_tier")
            if tier in totals:
                totals[tier.lower()] = totals.get(tier.lower(), 0)  # noqa — keep counts below
            if tier == P0:
                totals["p0"] += 1
            elif tier == P1:
                totals["p1"] += 1
            elif tier == P2:
                totals["p2"] += 1
            elif tier == P3:
                totals["p3"] += 1
        slim[oid] = [
            {
                k: l.get(k)
                for k in (
                    "opportunity_id",
                    "line_id",
                    "clin",
                    "description",
                    "manufacturer",
                    "mpn",
                    "quantity",
                    "uom",
                    "pack",
                    "equal_rule",
                    "condition",
                    "government_line_value",
                    "category",
                    "materiality_tier",
                    "materiality_score",
                    "estimated_unit_value",
                    "estimated_extended_value",
                    "acquisition_status",
                    "terminal_state",
                    "conservation_bucket",
                    "seller",
                    "unit_cost",
                    "extended_line_cost",
                    "price_origin",
                    "source_url",
                    "quote_subtype",
                    "exhaustion_reason",
                    "cluster_id",
                )
            }
            for l in lines
        ]
    payload = {"build": BUILD, "totals": totals, "by_opportunity": slim}
    _save(LINE_INVENTORY, payload)
    return payload
