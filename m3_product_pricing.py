"""Product Identity + Market Pricing Acquisition — research only.

Converts requirements into researchable products, then collects pricing evidence.
Never fabricates prices or assumes cheapest public price = acquisition cost.
Incomplete identity does not reject opportunities from rankings.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from typing import Any

from application_clock import now_utc
from m3_commercial_engine import (
    PRICE_LEVEL_1_ACTUAL,
    PRICE_LEVEL_2_PUBLIC,
    PRICE_LEVEL_3_COMPARABLE,
    PRICE_LEVEL_4_UNKNOWN,
    SCORE_HIGH,
    SCORE_LOW,
    SCORE_MEDIUM,
    extract_identification,
)
from m3_supplier_intelligence import (
    MODEL_RE,
    SKU_RE,
    UPC_RE,
    FSC_TITLE_RE,
    build_product_identity as _supplier_product_identity,
    research_public_pricing_web,
    collect_existing_price_evidence,
)

log = logging.getLogger("govtracker.m3_product_pricing")

PRODUCT_INDEX_KEY = "m3_product_pricing_v1"

MATCH_HIGH = "HIGH"
MATCH_MEDIUM = "MEDIUM"
MATCH_LOW = "LOW"
MATCH_UNKNOWN = "UNKNOWN"

READY = "READY_FOR_PRICING"
NEEDS_ID = "NEEDS_PRODUCT_IDENTIFICATION"
NEEDS_CONTRACT = "NEEDS_CONTRACT_DETAILS"
INSUFFICIENT = "INSUFFICIENT_DATA"

VAL_VERIFIED = "VERIFIED"
VAL_ESTIMATED = "ESTIMATED"
VAL_UNKNOWN = "UNKNOWN"

CERT_RE = re.compile(r"\b(UL|CE|ISO\s?\d+|MIL[- ]STD|Buy\s*American|TAA|Berry)\b", re.I)
UOM_RE = re.compile(r"\b(EA|EACH|BOX|CS|CASE|SET|KIT|LB|KG|FT|GAL|PAIR|PK|PACK)\b", re.I)
QTY_LINE_RE = re.compile(r"\b(?:QTY|QUANTITY|QTY\.?)[:\s#]*(\d+(?:\.\d+)?)\b", re.I)
ALT_RE = re.compile(r"\b(or\s+equal|equivalent|alternate|substitute|approved\s+equal)\b", re.I)


def _utc() -> str:
    return now_utc().isoformat()


def _num(v: Any) -> float | None:
    if v is None or v == "" or v == "UNKNOWN":
        return None
    try:
        return float(str(v).replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return None


def _blob(row: dict[str, Any]) -> str:
    parts = [str(row.get("title") or ""), str(row.get("description") or "")[:3000]]
    for key in ("solicitation_text", "attachment_text", "governing_text", "pws_text"):
        if row.get(key):
            parts.append(str(row.get(key))[:4000])
    for li in row.get("line_items") or row.get("bom") or []:
        if isinstance(li, dict):
            parts.append(str(li.get("description") or ""))
            parts.append(str(li.get("part_number") or ""))
            parts.append(str(li.get("manufacturer") or ""))
            parts.append(str(li.get("specification") or ""))
    docs = row.get("documents") or []
    if isinstance(docs, list):
        for d in docs[:8]:
            if isinstance(d, dict):
                parts.append(str(d.get("extracted_text") or d.get("text") or "")[:2000])
    return " ".join(parts)


def extract_requirements(row: dict[str, Any]) -> dict[str, Any]:
    """Phase 2 — requirement extraction from title/BOM/attachments text."""
    blob = _blob(row)
    ident = extract_identification(row)
    line_items = []
    for li in row.get("line_items") or row.get("bom") or []:
        if not isinstance(li, dict):
            continue
        line_items.append(
            {
                "description": li.get("description") or "UNKNOWN",
                "part_number": li.get("part_number") or "UNKNOWN",
                "manufacturer": li.get("manufacturer") or "UNKNOWN",
                "quantity": _num(li.get("quantity")) if _num(li.get("quantity")) is not None else "UNKNOWN",
                "unit": li.get("unit") or li.get("uom") or "UNKNOWN",
                "specification": li.get("specification") or "UNKNOWN",
            }
        )

    certs = sorted({m.group(1) for m in CERT_RE.finditer(blob)})
    alts = bool(ALT_RE.search(blob))
    uom = UOM_RE.search(blob)
    qty = ident.get("quantity") if ident.get("quantity") != "UNKNOWN" else None
    if qty is None:
        qm = QTY_LINE_RE.search(blob)
        qty = float(qm.group(1)) if qm else None

    return {
        "kind": "REQUIREMENT_EXTRACTION",
        "item_description": row.get("title") or "UNKNOWN",
        "part_numbers": [ident.get("part_number")] if ident.get("part_number") not in {None, "UNKNOWN"} else [],
        "manufacturer_references": [ident.get("manufacturer")] if ident.get("manufacturer") not in {None, "UNKNOWN"} else [],
        "quantities": qty if qty is not None else "UNKNOWN",
        "units": uom.group(1) if uom else "UNKNOWN",
        "options": [],
        "alternates_allowed": alts,
        "required_certifications": certs or [],
        "delivery_requirements": ident.get("delivery_requirements") or "UNKNOWN",
        "line_items": line_items[:20],
        "NSN": ident.get("NSN") or "UNKNOWN",
    }


def build_product_identity_profile(row: dict[str, Any]) -> dict[str, Any]:
    """Phase 1 — PRODUCT_IDENTITY_PROFILE."""
    base = _supplier_product_identity(row)
    req = extract_requirements(row)
    blob = _blob(row)
    meta = row.get("raw_metadata") if isinstance(row.get("raw_metadata"), dict) else {}

    mfr = base.get("Manufacturer") or "UNKNOWN"
    part = base.get("Part_number") or "UNKNOWN"
    nsn = base.get("NSN") or "UNKNOWN"
    model = base.get("Model") or "UNKNOWN"
    sku = base.get("SKU") or "UNKNOWN"
    qty = base.get("quantity") if base.get("quantity") != "UNKNOWN" else req.get("quantities")
    if qty == "UNKNOWN":
        qty = None

    # Brand / equivalent
    brand_required = mfr not in {None, "UNKNOWN"} and not req.get("alternates_allowed")
    config = "UNKNOWN"
    if "configuration" in blob.lower() or "config" in blob.lower():
        config = "SEE_SPECIFICATION"

    # Identity confidence
    if part not in {None, "UNKNOWN"} or nsn not in {None, "UNKNOWN"} or sku not in {None, "UNKNOWN"}:
        conf = MATCH_HIGH if mfr not in {None, "UNKNOWN"} or nsn not in {None, "UNKNOWN"} else MATCH_MEDIUM
    elif mfr not in {None, "UNKNOWN"} and model not in {None, "UNKNOWN"}:
        conf = MATCH_MEDIUM
    elif mfr not in {None, "UNKNOWN"} or FSC_TITLE_RE.match(str(row.get("title") or "").strip()):
        conf = MATCH_LOW
    elif base.get("sufficient_for_pricing_research"):
        conf = MATCH_LOW
    else:
        conf = MATCH_UNKNOWN

    return {
        "kind": "PRODUCT_IDENTITY_PROFILE",
        "Manufacturer": mfr,
        "Manufacturer_part_number": part,
        "SKU": sku,
        "NSN": nsn,
        "Model_number": model,
        "Category": base.get("product_category") or row.get("product_category") or "UNKNOWN",
        "Description": base.get("Technical_description") or row.get("title") or "UNKNOWN",
        "Quantity": qty if qty is not None else "UNKNOWN",
        "Unit_of_measure": req.get("units") or "UNKNOWN",
        "Configuration": config,
        "Specification_requirements": [
            li.get("specification") for li in (req.get("line_items") or []) if li.get("specification") not in {None, "UNKNOWN"}
        ][:10]
        or "UNKNOWN",
        "Brand_requirements": mfr if brand_required else ("OPEN_OR_EQUAL" if req.get("alternates_allowed") else "UNKNOWN"),
        "Equivalent_substitute_allowed": bool(req.get("alternates_allowed")),
        "Identity_confidence": conf,
        "sufficient_for_pricing_research": conf in {MATCH_HIGH, MATCH_MEDIUM},
        "UPC": base.get("UPC") or "UNKNOWN",
        "CAGE": base.get("CAGE") or meta.get("cage") or "UNKNOWN",
        "requirements": req,
    }


def product_match_confidence(identity: dict[str, Any]) -> dict[str, Any]:
    """Phase 3 — PRODUCT_MATCH_CONFIDENCE."""
    conf = identity.get("Identity_confidence") or MATCH_UNKNOWN
    reasons = []
    if conf == MATCH_HIGH:
        reasons.append("exact_sku_part_or_nsn")
    elif conf == MATCH_MEDIUM:
        reasons.append("manufacturer_plus_model_or_family")
    elif conf == MATCH_LOW:
        reasons.append("general_category_or_manufacturer_only")
    else:
        reasons.append("insufficient_identity")
    return {
        "PRODUCT_MATCH_CONFIDENCE": conf,
        "pricing_research_allowed": conf in {MATCH_HIGH, MATCH_MEDIUM},
        "reasons": reasons,
    }


def build_contract_value_model(row: dict[str, Any]) -> dict[str, Any]:
    """Phase 4 — contract value inputs."""
    meta = row.get("raw_metadata") if isinstance(row.get("raw_metadata"), dict) else {}
    verified = _num(
        row.get("award_amount")
        or meta.get("award_amount")
        or meta.get("contract_value_verified")
    )
    estimated = _num(
        row.get("estimated_value")
        or row.get("government_revenue")
        or row.get("solicitation_value")
        or meta.get("estimated_value")
        or meta.get("awardFloor")
        or meta.get("awardCeiling")
    )
    unit = _num(row.get("unit_price") or meta.get("unit_price"))
    qty = None
    for li in row.get("line_items") or row.get("bom") or []:
        if isinstance(li, dict) and _num(li.get("quantity")) is not None:
            qty = _num(li.get("quantity"))
            break
    if qty is None:
        ident = extract_identification(row)
        qty = _num(ident.get("quantity")) if ident.get("quantity") != "UNKNOWN" else None
    line_total = None
    if unit is not None and qty is not None:
        line_total = round(unit * qty, 2)

    hist = []
    for a in row.get("historical_awards") or row.get("award_history") or []:
        if isinstance(a, dict):
            amt = _num(a.get("amount") or a.get("award_amount"))
            if amt is not None:
                hist.append(amt)

    if verified is not None:
        value, conf = verified, VAL_VERIFIED
    elif estimated is not None:
        value, conf = estimated, VAL_ESTIMATED
    elif line_total is not None:
        value, conf = line_total, VAL_ESTIMATED
    elif hist:
        value, conf = round(sum(hist) / len(hist), 2), VAL_ESTIMATED
    else:
        value, conf = None, VAL_UNKNOWN

    return {
        "kind": "CONTRACT_VALUE_MODEL",
        "contract_value": value if value is not None else "UNKNOWN",
        "award_amount": verified if verified is not None else "UNKNOWN",
        "estimated_value": estimated if estimated is not None else "UNKNOWN",
        "quantity": qty if qty is not None else "UNKNOWN",
        "unit_price": unit if unit is not None else "UNKNOWN",
        "line_item_totals": line_total if line_total is not None else "UNKNOWN",
        "historical_award_values": hist[:10] or "UNKNOWN",
        "confidence": conf,
    }


def build_market_price_profile(
    row: dict[str, Any],
    identity: dict[str, Any],
    match: dict[str, Any],
    *,
    allow_paid_web: bool = False,
) -> dict[str, Any]:
    """Phases 5–6 — MARKET_PRICE_PROFILE. No research until identity allows."""
    existing = collect_existing_price_evidence(row, identity)
    web_meta = {"executed": False, "OpenAI": 0, "paid": 0, "reason": None}
    evidence = list(existing)

    if not match.get("pricing_research_allowed"):
        return {
            "kind": "MARKET_PRICE_PROFILE",
            "primary_level": PRICE_LEVEL_4_UNKNOWN,
            "items": [],
            "PRICE_CONFIDENCE": MATCH_UNKNOWN,
            "research_blocked_reason": "identity_insufficient_for_pricing",
            "web_research": web_meta,
            "notes": ["do_not_assume_cheapest_public_equals_acquisition", "no_fabricated_prices"],
        }

    product_for_web = {
        "Manufacturer": identity.get("Manufacturer"),
        "Model": identity.get("Model_number"),
        "Part_number": identity.get("Manufacturer_part_number"),
        "NSN": identity.get("NSN"),
        "SKU": identity.get("SKU"),
        "sufficient_for_pricing_research": True,
        "Technical_description": identity.get("Description"),
    }

    if allow_paid_web and not evidence:
        web_meta = research_public_pricing_web(row, product_for_web, allow_paid=True)
        evidence.extend(web_meta.get("evidence") or [])

    # Dedup
    seen = set()
    uniq = []
    for e in evidence:
        key = (e.get("Source"), e.get("amount"), e.get("level"))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(e)
    evidence = uniq

    if not evidence:
        level = PRICE_LEVEL_4_UNKNOWN
        conf = MATCH_UNKNOWN
    else:
        level = PRICE_LEVEL_4_UNKNOWN
        for pref in (PRICE_LEVEL_1_ACTUAL, PRICE_LEVEL_2_PUBLIC, PRICE_LEVEL_3_COMPARABLE):
            if any(e.get("level") == pref for e in evidence):
                level = pref
                break
        conf = SCORE_HIGH if level == PRICE_LEVEL_1_ACTUAL else (
            SCORE_MEDIUM if level == PRICE_LEVEL_2_PUBLIC else SCORE_LOW
        )

    return {
        "kind": "MARKET_PRICE_PROFILE",
        "primary_level": level,
        "items": [
            {
                "Supplier_source": e.get("Source"),
                "Product_match": e.get("product_matched") or identity.get("Description"),
                "Price": e.get("amount"),
                "Date": e.get("Date"),
                "Confidence": e.get("Product_match_confidence") or conf,
                "level": e.get("level"),
                "Notes": "not_assumed_as_final_acquisition_cost",
            }
            for e in evidence[:8]
        ],
        "PRICE_CONFIDENCE": conf if evidence else MATCH_UNKNOWN,
        "web_research": {
            "executed": web_meta.get("executed"),
            "reason": web_meta.get("reason"),
            "OpenAI": web_meta.get("OpenAI") or 0,
            "paid": web_meta.get("paid") or 0,
        },
        "notes": ["do_not_assume_cheapest_public_equals_acquisition", "no_fabricated_prices"],
    }


def research_readiness_score(
    identity: dict[str, Any],
    match: dict[str, Any],
    contract: dict[str, Any],
    market: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Phase 7 — RESEARCH_READINESS_SCORE."""
    missing: list[str] = []
    if identity.get("Identity_confidence") in {MATCH_UNKNOWN, MATCH_LOW}:
        missing.append("manufacturer_part_number_or_stronger_identity")
    if identity.get("Manufacturer") in {None, "UNKNOWN"} and identity.get("NSN") in {None, "UNKNOWN"}:
        missing.append("manufacturer")
    if identity.get("Manufacturer_part_number") in {None, "UNKNOWN"} and identity.get("NSN") in {None, "UNKNOWN"}:
        missing.append("manufacturer_part_number")
    if identity.get("Quantity") in {None, "UNKNOWN"}:
        missing.append("quantity")
    if contract.get("confidence") == VAL_UNKNOWN:
        missing.append("contract_value")
    if market and market.get("primary_level") == PRICE_LEVEL_4_UNKNOWN and match.get("pricing_research_allowed"):
        missing.append("acquisition_cost")

    if match.get("pricing_research_allowed") and contract.get("confidence") != VAL_UNKNOWN:
        status = READY
    elif match.get("pricing_research_allowed") and contract.get("confidence") == VAL_UNKNOWN:
        status = NEEDS_CONTRACT
    elif identity.get("Identity_confidence") in {MATCH_UNKNOWN, MATCH_LOW}:
        status = NEEDS_ID if missing else INSUFFICIENT
    else:
        status = INSUFFICIENT

    # Score for queue ranking
    score = 0
    if match.get("PRODUCT_MATCH_CONFIDENCE") == MATCH_HIGH:
        score += 40
    elif match.get("PRODUCT_MATCH_CONFIDENCE") == MATCH_MEDIUM:
        score += 25
    elif match.get("PRODUCT_MATCH_CONFIDENCE") == MATCH_LOW:
        score += 10
    if contract.get("confidence") == VAL_VERIFIED:
        score += 25
    elif contract.get("confidence") == VAL_ESTIMATED:
        score += 15
    if identity.get("Quantity") not in {None, "UNKNOWN"}:
        score += 10
    if identity.get("Manufacturer") not in {None, "UNKNOWN"}:
        score += 10
    if market and market.get("primary_level") != PRICE_LEVEL_4_UNKNOWN:
        score += 15

    return {
        "RESEARCH_READINESS": status,
        "RESEARCH_READINESS_SCORE": min(100, score),
        "missing_information": missing,
    }


def economics_impact(row: dict[str, Any], identity: dict[str, Any], contract: dict[str, Any], market: dict[str, Any]) -> dict[str, Any]:
    """Phase 8 — connect to deal economics; show exactly what is missing."""
    missing = []
    if identity.get("Manufacturer_part_number") in {None, "UNKNOWN"} and identity.get("NSN") in {None, "UNKNOWN"}:
        missing.append("manufacturer part number")
    if identity.get("Quantity") in {None, "UNKNOWN"}:
        missing.append("quantity")
    if market.get("primary_level") == PRICE_LEVEL_4_UNKNOWN or not market.get("items"):
        missing.append("acquisition cost")
    if contract.get("confidence") == VAL_UNKNOWN:
        missing.append("contract value / revenue")

    can_calc = (
        contract.get("confidence") != VAL_UNKNOWN
        and market.get("items")
        and market.get("primary_level") != PRICE_LEVEL_4_UNKNOWN
    )

    economics = None
    if can_calc:
        # Inject evidence into a shallow row copy for deal economics
        try:
            from m3_deal_economics import build_deal_economics

            best = min(
                (e for e in market["items"] if _num(e.get("Price")) is not None),
                key=lambda e: float(e["Price"]),
                default=None,
            )
            enriched = {
                **row,
                "estimated_value": contract.get("contract_value")
                if contract.get("contract_value") != "UNKNOWN"
                else row.get("estimated_value"),
                "supplier_intelligence": {
                    **(row.get("supplier_intelligence") or {}),
                    "Pricing_evidence": {
                        "primary_level": market.get("primary_level"),
                        "items": [
                            {
                                "level": best.get("level") if best else market.get("primary_level"),
                                "amount": best.get("Price") if best else None,
                                "Source": best.get("Supplier_source") if best else None,
                                "Date": best.get("Date") if best else _utc(),
                                "Product_match_confidence": best.get("Confidence") if best else SCORE_LOW,
                            }
                        ]
                        if best
                        else [],
                    },
                    "cost_detail": {"best_price": best.get("Price") if best else None},
                    "ACQUISITION_COST_CONFIDENCE": market.get("PRICE_CONFIDENCE"),
                },
            }
            economics = build_deal_economics(enriched)
        except Exception as exc:
            economics = {"error": str(exc)}

    if can_calc:
        msg = "Economics calculable from current evidence"
    else:
        msg = "Cannot calculate profit because:\nMissing:\n- " + "\n- ".join(missing or ["insufficient inputs"])

    return {
        "can_calculate_profit": can_calc,
        "missing": missing,
        "message": msg,
        "deal_economics": economics,
    }


def next_product_action(readiness: dict[str, Any], identity: dict[str, Any], market: dict[str, Any]) -> str:
    st = readiness.get("RESEARCH_READINESS")
    if st == NEEDS_ID:
        return "Extract manufacturer part number / NSN from package"
    if st == NEEDS_CONTRACT:
        return "Locate contract or estimated value"
    if st == READY and market.get("primary_level") == PRICE_LEVEL_4_UNKNOWN:
        return "Run distributor/public pricing research"
    if st == READY:
        return "Verify pricing evidence then update deal economics"
    return "Gather product identity and contract details"


def build_product_pricing_intelligence(
    row: dict[str, Any],
    *,
    allow_paid_web: bool = False,
) -> dict[str, Any]:
    identity = build_product_identity_profile(row)
    match = product_match_confidence(identity)
    contract = build_contract_value_model(row)
    # Avoid recursive identity build inside contract — already computed; patch quantity from identity
    if contract.get("quantity") == "UNKNOWN" and identity.get("Quantity") != "UNKNOWN":
        contract["quantity"] = identity.get("Quantity")

    market = build_market_price_profile(row, identity, match, allow_paid_web=allow_paid_web)
    readiness = research_readiness_score(identity, match, contract, market)
    impact = economics_impact(row, identity, contract, market)
    action = next_product_action(readiness, identity, market)

    return {
        "kind": "M3ProductPricingIntelligence",
        "generated_at": _utc(),
        "PRODUCT_IDENTITY": identity,
        "PRODUCT_MATCH": match,
        "CONTRACT_VALUE": contract,
        "MARKET_PRICE": market,
        "RESEARCH_READINESS": readiness,
        "ECONOMICS_IMPACT": impact,
        "Next_Action": action,
        "DEVELOPMENT_NO_OUTREACH": True,
        "commercial_outreach": False,
    }


def load_product_index() -> dict[str, Any]:
    try:
        from database import SessionLocal
        from models import AppSetting

        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == PRODUCT_INDEX_KEY).one_or_none()
            if not row or not row.value:
                return {}
            data = json.loads(row.value)
            return data if isinstance(data, dict) else {}
        finally:
            db.close()
    except Exception:
        log.exception("load product index failed")
        return {}


def save_product_index(by_id: dict[str, Any]) -> bool:
    try:
        from database import SessionLocal
        from models import AppSetting

        payload = {"kind": "M3ProductPricingIndex", "updated_at": _utc(), "by_id": by_id, "count": len(by_id)}
        raw = json.dumps(payload, default=str)
        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == PRODUCT_INDEX_KEY).one_or_none()
            if row:
                row.value = raw
            else:
                db.add(AppSetting(key=PRODUCT_INDEX_KEY, value=raw))
            db.commit()
            return True
        finally:
            db.close()
    except Exception:
        log.exception("save product index failed")
        return False


def get_persisted_product(canonical_id: str) -> dict[str, Any] | None:
    data = load_product_index()
    by_id = data.get("by_id") if isinstance(data.get("by_id"), dict) else {}
    v = by_id.get(canonical_id)
    return v if isinstance(v, dict) else None


def _priority_seed(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Phase 9 — research queue: commercial + execution + readiness, not value alone."""
    by_id = {r["canonical_id"]: r for r in rows if r.get("canonical_id")}
    scored = []
    for r in rows:
        if not r.get("canonical_id"):
            continue
        ci = r.get("commercial_intelligence") or {}
        ei = r.get("execution_intelligence") or {}
        commercial = 70 if (ci.get("COMMERCIAL_OPPORTUNITY_SCORE") or r.get("commercial_opportunity_score")) == SCORE_HIGH else (
            50 if (ci.get("COMMERCIAL_OPPORTUNITY_SCORE") or r.get("commercial_opportunity_score")) == SCORE_MEDIUM else 20
        )
        execution = int((ei.get("EXECUTION") or {}).get("EXECUTION_SCORE") or 0)
        # cheap readiness preview
        ident = build_product_identity_profile(r)
        match = product_match_confidence(ident)
        contract = build_contract_value_model(r)
        ready = research_readiness_score(ident, match, contract)
        total = commercial + execution + int(ready.get("RESEARCH_READINESS_SCORE") or 0)
        scored.append((total, r))
    scored.sort(key=lambda x: -x[0])
    return [r for _, r in scored[:limit]]


def analyze_product_pricing_top(
    store: Any,
    *,
    limit: int = 25,
    allow_paid_web: bool = False,
    paid_limit: int = 5,
) -> dict[str, Any]:
    """TOP N product identity + optional gated pricing research."""
    rows = store.all() if hasattr(store, "all") else list(store)

    # Attach persisted layers
    try:
        from m3_supplier_intelligence import get_persisted_supplier_intelligence
        from m3_execution_intelligence import get_persisted_execution
        from m3_competitive_intelligence import get_persisted_competitive
        from m3_deal_economics import get_persisted_economics
    except Exception:
        get_persisted_supplier_intelligence = get_persisted_execution = None  # type: ignore
        get_persisted_competitive = get_persisted_economics = None  # type: ignore

    enriched = []
    for r in rows:
        row = dict(r)
        cid = str(row.get("canonical_id") or "")
        for key, loader in (
            ("supplier_intelligence", get_persisted_supplier_intelligence),
            ("execution_intelligence", get_persisted_execution),
            ("competitive_intelligence", get_persisted_competitive),
            ("deal_economics", get_persisted_economics),
        ):
            if not row.get(key) and callable(loader):
                try:
                    got = loader(cid)
                    if got:
                        row[key] = got
                except Exception:
                    pass
        enriched.append(row)

    targets = _priority_seed(enriched, limit)
    results = []
    index_updates: dict[str, Any] = {}
    match_counts: Counter[str] = Counter()
    level_counts = {"LEVEL_1": 0, "LEVEL_2": 0, "LEVEL_3": 0, "LEVEL_4": 0}
    paid_total = 0
    openai_total = 0
    can_calc = 0
    paid_used = 0

    for row in targets:
        use_paid = allow_paid_web and paid_used < paid_limit
        # Only spend paid when medium/high identity
        ident_preview = build_product_identity_profile(row)
        match_preview = product_match_confidence(ident_preview)
        if use_paid and match_preview.get("pricing_research_allowed"):
            pkg = build_product_pricing_intelligence(row, allow_paid_web=True)
            if (pkg.get("MARKET_PRICE") or {}).get("web_research", {}).get("paid"):
                paid_used += 1
        else:
            pkg = build_product_pricing_intelligence(row, allow_paid_web=False)

        index_updates[row["canonical_id"]] = pkg
        full = {**(store.get(row["canonical_id"]) or row)}
        full["product_pricing_intelligence"] = pkg
        # Feed pricing into supplier intel cost path when found (evidence only)
        market = pkg.get("MARKET_PRICE") or {}
        if market.get("items") and market.get("primary_level") != PRICE_LEVEL_4_UNKNOWN:
            best = min(
                (e for e in market["items"] if _num(e.get("Price")) is not None),
                key=lambda e: float(e["Price"]),
                default=None,
            )
            if best:
                si = dict(full.get("supplier_intelligence") or {})
                si["Pricing_evidence"] = {
                    "primary_level": market.get("primary_level"),
                    "items": [
                        {
                            "level": best.get("level") or market.get("primary_level"),
                            "amount": best.get("Price"),
                            "Source": best.get("Supplier_source"),
                            "Date": best.get("Date") or _utc(),
                            "Product_match_confidence": best.get("Confidence") or SCORE_LOW,
                            "product_matched": best.get("Product_match"),
                        }
                    ],
                }
                si["cost_detail"] = {"best_price": best.get("Price"), "best_level": market.get("primary_level")}
                full["supplier_intelligence"] = si
        store._rows[row["canonical_id"]] = full

        identity = pkg["PRODUCT_IDENTITY"]
        match = pkg["PRODUCT_MATCH"]
        match_counts[match.get("PRODUCT_MATCH_CONFIDENCE") or MATCH_UNKNOWN] += 1
        lvl = str((pkg.get("MARKET_PRICE") or {}).get("primary_level") or PRICE_LEVEL_4_UNKNOWN)
        if "LEVEL_1" in lvl:
            level_counts["LEVEL_1"] += 1
        elif "LEVEL_2" in lvl:
            level_counts["LEVEL_2"] += 1
        elif "LEVEL_3" in lvl:
            level_counts["LEVEL_3"] += 1
        else:
            level_counts["LEVEL_4"] += 1

        wr = (pkg.get("MARKET_PRICE") or {}).get("web_research") or {}
        paid_total += int(wr.get("paid") or 0)
        openai_total += int(wr.get("OpenAI") or 0)
        if (pkg.get("ECONOMICS_IMPACT") or {}).get("can_calculate_profit"):
            can_calc += 1

        results.append(
            {
                "canonical_id": row.get("canonical_id"),
                "Opportunity": row.get("title"),
                "Product": identity.get("Description"),
                "Manufacturer": identity.get("Manufacturer"),
                "Part_number": identity.get("Manufacturer_part_number"),
                "Quantity": identity.get("Quantity"),
                "Contract_value": (pkg.get("CONTRACT_VALUE") or {}).get("contract_value"),
                "Contract_value_confidence": (pkg.get("CONTRACT_VALUE") or {}).get("confidence"),
                "Identity_confidence": identity.get("Identity_confidence"),
                "Match_confidence": match.get("PRODUCT_MATCH_CONFIDENCE"),
                "Pricing_level": lvl,
                "Pricing_evidence_count": len((pkg.get("MARKET_PRICE") or {}).get("items") or []),
                "Research_readiness": (pkg.get("RESEARCH_READINESS") or {}).get("RESEARCH_READINESS"),
                "Research_score": (pkg.get("RESEARCH_READINESS") or {}).get("RESEARCH_READINESS_SCORE"),
                "Missing": (pkg.get("RESEARCH_READINESS") or {}).get("missing_information"),
                "Next_action": pkg.get("Next_Action"),
                "Can_calculate_profit": (pkg.get("ECONOMICS_IMPACT") or {}).get("can_calculate_profit"),
            }
        )

    results.sort(
        key=lambda r: (
            0 if r.get("Research_readiness") == READY else 1,
            -int(r.get("Research_score") or 0),
        )
    )

    try:
        existing = load_product_index()
        by = existing.get("by_id") if isinstance(existing.get("by_id"), dict) else {}
        by.update(index_updates)
        save_product_index(by)
    except Exception:
        log.exception("product index save failed")
    try:
        store.save()
    except Exception:
        log.exception("pipeline save failed")

    return {
        "kind": "M3ProductPricingRun",
        "generated_at": _utc(),
        "analyzed": len(results),
        "exact_product_matches": match_counts.get(MATCH_HIGH, 0),
        "partial_matches": match_counts.get(MATCH_MEDIUM, 0) + match_counts.get(MATCH_LOW, 0),
        "unknown_identity": match_counts.get(MATCH_UNKNOWN, 0),
        "match_breakdown": dict(match_counts),
        "pricing_levels": level_counts,
        "TOP_RESEARCH_READY": [r for r in results if r.get("Research_readiness") == READY][:10]
        or results[:10],
        "ALL_SCORED": results,
        "economics_impact": {
            "can_calculate_profit": can_calc,
            "still_missing_data": len(results) - can_calc,
        },
        "OpenAI": openai_total,
        "paid": paid_total,
        "DEVELOPMENT_NO_OUTREACH": True,
        "commercial_outreach": False,
    }


def deal_room_product_section(row: dict[str, Any]) -> dict[str, Any]:
    pp = row.get("product_pricing_intelligence")
    if not isinstance(pp, dict) or pp.get("kind") != "M3ProductPricingIntelligence":
        persisted = get_persisted_product(str(row.get("canonical_id") or ""))
        if isinstance(persisted, dict) and persisted.get("kind") == "M3ProductPricingIntelligence":
            pp = persisted
        else:
            pp = build_product_pricing_intelligence(row, allow_paid_web=False)
    identity = pp.get("PRODUCT_IDENTITY") or {}
    market = pp.get("MARKET_PRICE") or {}
    ready = pp.get("RESEARCH_READINESS") or {}
    contract = pp.get("CONTRACT_VALUE") or {}
    return {
        "kind": "M3DealRoomProductIntelligence",
        "Product_identity": identity.get("Description"),
        "Identity_confidence": identity.get("Identity_confidence"),
        "Manufacturer": identity.get("Manufacturer"),
        "Part_number": identity.get("Manufacturer_part_number"),
        "NSN": identity.get("NSN"),
        "Quantity": identity.get("Quantity"),
        "Contract_value": contract.get("contract_value"),
        "Contract_value_confidence": contract.get("confidence"),
        "Pricing_evidence": {
            "level": market.get("primary_level"),
            "items": (market.get("items") or [])[:5],
            "confidence": market.get("PRICE_CONFIDENCE"),
        },
        "Research_readiness": ready.get("RESEARCH_READINESS"),
        "Missing_information": ready.get("missing_information") or [],
        "Economics_message": (pp.get("ECONOMICS_IMPACT") or {}).get("message"),
        "Next_Action": pp.get("Next_Action"),
        "full": pp,
    }
