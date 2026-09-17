"""Supplier Intelligence + Acquisition Cost Verification — research only.

No supplier contact, accounts, quotes requests, purchases, or fabricated margins.
Government award price alone is NOT profit.
"""

from __future__ import annotations

import json
import logging
import re
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
    build_commercial_intelligence,
    build_commercial_research_queue,
    extract_identification,
    classify_winner_type,
)

log = logging.getLogger("govtracker.m3_supplier_intelligence")

PRODUCT_IDENTITY_INCOMPLETE = "PRODUCT_IDENTITY_INCOMPLETE"
MARGIN_PENDING = "MARGIN_PENDING_SUPPLIER_VERIFICATION"
SUPPLIER_INTEL_SETTINGS_KEY = "m3_supplier_intelligence_v1"

OEM = "OEM"
AUTHORIZED_DISTRIBUTOR = "AUTHORIZED_DISTRIBUTOR"
DISTRIBUTOR = "DISTRIBUTOR"
RESELLER = "RESELLER"
UNKNOWN_ROLE = "UNKNOWN"

# Public channel map — research pointers only (no login/outreach)
PUBLIC_CHANNELS: dict[str, list[dict[str, Any]]] = {
    "Cisco": [
        {"company": "Cisco", "website": "https://www.cisco.com", "role": OEM, "confidence": "HIGH"},
        {"company": "CDW", "website": "https://www.cdw.com", "role": DISTRIBUTOR, "confidence": "MEDIUM"},
        {"company": "SHI", "website": "https://www.shi.com", "role": DISTRIBUTOR, "confidence": "MEDIUM"},
        {"company": "Insight", "website": "https://www.insight.com", "role": DISTRIBUTOR, "confidence": "MEDIUM"},
    ],
    "Dell": [
        {"company": "Dell", "website": "https://www.dell.com", "role": OEM, "confidence": "HIGH"},
        {"company": "CDW", "website": "https://www.cdw.com", "role": DISTRIBUTOR, "confidence": "MEDIUM"},
        {"company": "SHI", "website": "https://www.shi.com", "role": DISTRIBUTOR, "confidence": "MEDIUM"},
        {"company": "Insight", "website": "https://www.insight.com", "role": DISTRIBUTOR, "confidence": "MEDIUM"},
    ],
    "HP": [
        {"company": "HP", "website": "https://www.hp.com", "role": OEM, "confidence": "HIGH"},
        {"company": "CDW", "website": "https://www.cdw.com", "role": DISTRIBUTOR, "confidence": "MEDIUM"},
    ],
    "Lenovo": [
        {"company": "Lenovo", "website": "https://www.lenovo.com", "role": OEM, "confidence": "HIGH"},
        {"company": "CDW", "website": "https://www.cdw.com", "role": DISTRIBUTOR, "confidence": "MEDIUM"},
    ],
    "DEFAULT_IT": [
        {"company": "CDW", "website": "https://www.cdw.com", "role": DISTRIBUTOR, "confidence": "LOW"},
        {"company": "SHI", "website": "https://www.shi.com", "role": DISTRIBUTOR, "confidence": "LOW"},
        {"company": "Insight", "website": "https://www.insight.com", "role": DISTRIBUTOR, "confidence": "LOW"},
    ],
    "INDUSTRIAL": [
        {"company": "Grainger", "website": "https://www.grainger.com", "role": DISTRIBUTOR, "confidence": "MEDIUM"},
        {"company": "Fastenal", "website": "https://www.fastenal.com", "role": DISTRIBUTOR, "confidence": "MEDIUM"},
        {"company": "MSC Industrial", "website": "https://www.mscdirect.com", "role": DISTRIBUTOR, "confidence": "LOW"},
    ],
    "DLA": [
        {"company": "DLA / SAM.gov cross-publish", "website": "https://sam.gov", "role": "GOVERNMENT_PORTAL", "confidence": "MEDIUM"},
        {"company": "Authorized DLA suppliers (CAGE-specific)", "website": "UNKNOWN", "role": UNKNOWN_ROLE, "confidence": "LOW"},
    ],
}

MODEL_RE = re.compile(
    r"\b((?:Catalyst|Latitude|Precision|OptiPlex|PowerEdge|ThinkPad|ProLiant|EliteBook|ISR|ASA|Nexus)[\s\-][A-Z0-9][A-Z0-9\-/\.]{1,24})\b",
    re.I,
)
SKU_RE = re.compile(r"\b(?:SKU|ITEM\s*#|ITEM\s*NO)[:\s#]*([A-Z0-9\-]{3,24})\b", re.I)
UPC_RE = re.compile(r"\bUPC[:\s#]*(\d{12,14})\b", re.I)
FSC_TITLE_RE = re.compile(r"^(\d{2,4})--([A-Z0-9 ,\-/]+)$", re.I)
PRICE_RE = re.compile(
    r"(?:\$|USD\s*)(\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\d+(?:\.\d{2})?)",
    re.I,
)


def _utc() -> str:
    return now_utc().isoformat()


def _num(v: Any) -> float | None:
    if v is None or v == "" or v == "UNKNOWN":
        return None
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def load_supplier_intel_index() -> dict[str, Any]:
    """Durable SI map keyed by canonical_id — survives concurrent pipeline saves."""
    try:
        from database import SessionLocal
        from models import AppSetting

        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == SUPPLIER_INTEL_SETTINGS_KEY).one_or_none()
            if not row or not row.value:
                return {}
            data = json.loads(row.value)
            return data if isinstance(data, dict) else {}
        finally:
            db.close()
    except Exception:
        log.exception("Failed loading supplier intelligence index")
        return {}


def save_supplier_intel_index(index: dict[str, Any]) -> bool:
    try:
        from database import SessionLocal
        from models import AppSetting

        payload = {
            "kind": "M3SupplierIntelligenceIndex",
            "updated_at": _utc(),
            "by_id": index,
            "count": len(index),
        }
        raw = json.dumps(payload, default=str)
        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == SUPPLIER_INTEL_SETTINGS_KEY).one_or_none()
            if row:
                row.value = raw
            else:
                db.add(AppSetting(key=SUPPLIER_INTEL_SETTINGS_KEY, value=raw))
            db.commit()
            return True
        finally:
            db.close()
    except Exception:
        log.exception("Failed saving supplier intelligence index")
        return False


def get_persisted_supplier_intelligence(canonical_id: str) -> dict[str, Any] | None:
    if not canonical_id:
        return None
    data = load_supplier_intel_index()
    by_id = data.get("by_id") if isinstance(data.get("by_id"), dict) else data
    si = by_id.get(canonical_id) if isinstance(by_id, dict) else None
    return si if isinstance(si, dict) else None


def upsert_persisted_supplier_intelligence(canonical_id: str, si: dict[str, Any]) -> bool:
    data = load_supplier_intel_index()
    by_id = data.get("by_id") if isinstance(data.get("by_id"), dict) else {}
    if not isinstance(by_id, dict):
        by_id = {}
    # If load returned flat map already
    if data and "by_id" not in data and all(isinstance(v, dict) for v in data.values()):
        by_id = dict(data)
    by_id[canonical_id] = si
    return save_supplier_intel_index(by_id)

def _title_blob(row: dict[str, Any]) -> str:
    parts = [str(row.get("title") or ""), str(row.get("description") or "")[:1500]]
    for li in row.get("line_items") or row.get("bom") or []:
        if isinstance(li, dict):
            parts.append(str(li.get("description") or ""))
            parts.append(str(li.get("part_number") or ""))
            parts.append(str(li.get("manufacturer") or ""))
    return " ".join(parts)


def build_product_identity(row: dict[str, Any]) -> dict[str, Any]:
    """Phase 2 — product identification. Incomplete blocks pricing research."""
    ident = extract_identification(row)
    blob = _title_blob(row)
    meta = row.get("raw_metadata") if isinstance(row.get("raw_metadata"), dict) else {}

    model = "UNKNOWN"
    mm = MODEL_RE.search(blob)
    if mm:
        model = mm.group(1).strip()
    # Dell/Cisco short titles: "Dell Storage", "Cisco Systems Network Switches"
    title = str(row.get("title") or "")
    if model == "UNKNOWN" and ident.get("manufacturer") not in {None, "UNKNOWN"}:
        # Keep product family from title after manufacturer name
        mfr = str(ident["manufacturer"])
        if title.lower().startswith(mfr.lower()):
            rest = title[len(mfr) :].strip(" -:")
            if rest and len(rest) >= 3:
                model = rest[:80]

    sku = SKU_RE.search(blob)
    upc = UPC_RE.search(blob)
    nsn = ident.get("NSN")
    if nsn == "UNKNOWN":
        nsn = meta.get("nsn") or "UNKNOWN"
    # DLA FSC title form: 31--BUSHING,SLEEVE
    fsc = FSC_TITLE_RE.match(title.strip())
    technical = title
    if fsc:
        technical = f"FSC {fsc.group(1)} — {fsc.group(2).strip()}"

    product = {
        "Manufacturer": ident.get("manufacturer") or "UNKNOWN",
        "Model": model,
        "Part_number": ident.get("part_number") or "UNKNOWN",
        "SKU": sku.group(1) if sku else "UNKNOWN",
        "NSN": nsn if nsn else "UNKNOWN",
        "CAGE": ident.get("CAGE") or meta.get("cage") or "UNKNOWN",
        "UPC": upc.group(1) if upc else "UNKNOWN",
        "Technical_description": technical or "UNKNOWN",
        "product_category": ident.get("product_category") or "UNKNOWN",
        "quantity": ident.get("quantity") or "UNKNOWN",
    }

    # Sufficiency: need manufacturer OR NSN OR (part/model)
    has_mfr = product["Manufacturer"] not in {None, "UNKNOWN"}
    has_nsn = product["NSN"] not in {None, "UNKNOWN"}
    has_part = product["Part_number"] not in {None, "UNKNOWN"}
    has_model = product["Model"] not in {None, "UNKNOWN"}
    sufficient = bool(has_mfr and (has_model or has_part or has_nsn)) or has_nsn
    # Cisco/Dell named product family (switches/storage) counts as partial identity
    if has_mfr and any(x in title.lower() for x in ("switch", "storage", "laptop", "server", "monitor", "router")):
        sufficient = True
        product["identity_note"] = "manufacturer_plus_product_family"
    elif has_nsn or (fsc and str(row.get("agency") or "").upper().find("DEFENSE LOGISTICS") >= 0):
        sufficient = True
        product["identity_note"] = "nsn_or_dla_fsc_identity"
        if not has_mfr:
            product["Manufacturer"] = product["Manufacturer"] if has_mfr else "DLA_CATALOG_ITEM"

    product["identity_status"] = "COMPLETE" if sufficient else PRODUCT_IDENTITY_INCOMPLETE
    product["sufficient_for_pricing_research"] = sufficient
    return product


def _deprioritize(row: dict[str, Any]) -> bool:
    title = str(row.get("title") or "").lower()
    cat = str(row.get("product_category") or "").upper()
    if any(
        x in title
        for x in (
            "service",
            "services",
            "construction",
            "restoration",
            "install",
            "installation",
            "wetland",
            "elevator replacement",
            "window washing",
            "how to protest",
            "repair services",
        )
    ):
        # Allow "Equipment Only" / product supply exceptions
        if "equipment only" in title or "supply of" in title:
            return False
        if "repair" in title and "badges" in title:
            return True
        return True
    if "LIKELY_SERVICE" in cat or cat == "SERVICE":
        return True
    return False


def supplier_research_priority(row: dict[str, Any], ci: dict[str, Any] | None = None) -> tuple:
    """Rank for SUPPLIER_RESEARCH_QUEUE — Cisco/Dell/DLA/consumables first."""
    title = str(row.get("title") or "").lower()
    mfr = str((ci or {}).get("IDENTIFICATION", {}).get("manufacturer") or row.get("manufacturer") or "").lower()
    agency = str(row.get("agency") or "").lower()
    band = (ci or {}).get("COMMERCIAL_OPPORTUNITY_SCORE") or row.get("commercial_opportunity_score") or SCORE_LOW

    if _deprioritize(row):
        return (9, 0, title)

    score = 0
    if "cisco" in title or mfr == "cisco":
        score += 100
    if "dell" in title or mfr == "dell":
        score += 95
    if "defense logistics" in agency or title.startswith(tuple(str(i) for i in range(10))) and "--" in title:
        score += 90
    if any(x in title for x in ("nsn", "bushing", "blade", "valve", "bearing", "fastener")):
        score += 70
    if any(x in title for x in ("switch", "storage", "laptop", "server", "monitor", "router")):
        score += 60
    if band == SCORE_HIGH:
        score += 40
    elif band == SCORE_MEDIUM:
        score += 25
    # Tangible goods boost
    cat = str(row.get("product_category") or "").upper()
    if any(x in cat for x in ("ELECTRONIC", "INDUSTRIAL", "PARTS", "TOOLS", "IT_", "VEHICLE")):
        score += 15
    return (0 if score > 0 else 5, -score, title)


def map_supply_chain(row: dict[str, Any], product: dict[str, Any]) -> dict[str, Any]:
    """Phase 3 — public channel mapping. No contact."""
    mfr = str(product.get("Manufacturer") or "UNKNOWN")
    channels: list[dict[str, Any]] = []
    key = mfr if mfr in PUBLIC_CHANNELS else None
    title = str(row.get("title") or "").lower()
    agency = str(row.get("agency") or "").lower()

    if key:
        for c in PUBLIC_CHANNELS[key]:
            channels.append(
                {
                    **c,
                    "product_match": product.get("Model") or product.get("Technical_description"),
                    "evidence_source": "public_channel_knowledge_base",
                }
            )
    elif "defense logistics" in agency or product.get("NSN") not in {None, "UNKNOWN"} or mfr == "DLA_CATALOG_ITEM":
        for c in PUBLIC_CHANNELS["DLA"]:
            channels.append({**c, "product_match": product.get("Technical_description"), "evidence_source": "dla_public_routes"})
    elif any(x in title for x in ("blade", "seed", "tank", "bushing", "valve", "bearing")):
        for c in PUBLIC_CHANNELS["INDUSTRIAL"]:
            channels.append({**c, "product_match": product.get("Technical_description"), "evidence_source": "industrial_distributor_map"})
    elif any(x in title for x in ("switch", "storage", "laptop", "server", "network", "computer")):
        for c in PUBLIC_CHANNELS["DEFAULT_IT"]:
            channels.append({**c, "product_match": product.get("Model") or title, "evidence_source": "it_distributor_map"})

    # Historical winners as government-supplier candidates
    for w in (row.get("historical_awards") or [])[:6]:
        if not isinstance(w, dict):
            continue
        name = str(w.get("winner") or w.get("vendor") or w.get("awardee") or "").strip()
        if not name:
            continue
        channels.append(
            {
                "company": name,
                "website": "UNKNOWN",
                "role": classify_winner_type(name, w),
                "product_match": "historical_award_overlap",
                "evidence_source": "historical_award",
                "confidence": "MEDIUM",
            }
        )

    oem = [c for c in channels if c.get("role") == OEM]
    auth = [c for c in channels if c.get("role") == AUTHORIZED_DISTRIBUTOR]
    dist = [c for c in channels if c.get("role") == DISTRIBUTOR]
    resellers = [c for c in channels if c.get("role") == RESELLER]

    supplier_confidence = SCORE_LOW
    if oem and dist:
        supplier_confidence = SCORE_HIGH
    elif oem or len(dist) >= 2:
        supplier_confidence = SCORE_MEDIUM
    elif channels:
        supplier_confidence = SCORE_LOW

    return {
        "kind": "M3SupplyChainMap",
        "Manufacturer": oem[:3],
        "Authorized_distributors": auth[:5],
        "Public_distributors": dist[:8],
        "Government_suppliers": [c for c in channels if c.get("evidence_source") == "historical_award"][:8],
        "Commercial_marketplaces": [],
        "Dealer_channels": resellers[:5],
        "all_channels": channels[:20],
        "supplier_confidence": supplier_confidence,
        "DEVELOPMENT_NO_OUTREACH": True,
    }


def collect_existing_price_evidence(row: dict[str, Any], product: dict[str, Any]) -> list[dict[str, Any]]:
    """Gather already-known price facts from the opportunity row — never invent."""
    evidence: list[dict[str, Any]] = []
    pricing = row.get("commercial_pricing") or row.get("public_pricing") or {}
    verified = _num(pricing.get("verified_wholesale_unit") or row.get("supplier_unit_cost"))
    public = _num(pricing.get("lowest_public_new_unit") or pricing.get("public_unit_price"))
    comparable = _num(pricing.get("comparable_unit") or pricing.get("msrp"))

    if verified is not None:
        evidence.append(
            {
                "level": PRICE_LEVEL_1_ACTUAL,
                "amount": verified,
                "unit": "USD",
                "Source": pricing.get("verified_source") or "row_verified_wholesale",
                "Date": pricing.get("as_of") or row.get("last_seen_at") or _utc(),
                "Product_match_confidence": SCORE_HIGH,
            }
        )
    if public is not None:
        evidence.append(
            {
                "level": PRICE_LEVEL_2_PUBLIC,
                "amount": public,
                "unit": "USD",
                "Source": pricing.get("public_source") or "public_commercial_listing",
                "Date": pricing.get("as_of") or row.get("last_seen_at") or _utc(),
                "Product_match_confidence": SCORE_MEDIUM,
            }
        )
    if comparable is not None:
        evidence.append(
            {
                "level": PRICE_LEVEL_3_COMPARABLE,
                "amount": comparable,
                "unit": "USD",
                "Source": pricing.get("comparable_source") or "comparable_product",
                "Date": pricing.get("as_of") or _utc(),
                "Product_match_confidence": SCORE_LOW,
            }
        )
    return evidence


def research_public_pricing_web(
    row: dict[str, Any],
    product: dict[str, Any],
    *,
    allow_paid: bool = True,
) -> dict[str, Any]:
    """Cost-Governor-gated public price search. Returns evidence or explicit failure — never invents."""
    if not product.get("sufficient_for_pricing_research"):
        return {"executed": False, "reason": PRODUCT_IDENTITY_INCOMPLETE, "evidence": [], "OpenAI": 0, "paid": 0}
    if not allow_paid:
        return {"executed": False, "reason": "paid_blocked", "evidence": [], "OpenAI": 0, "paid": 0}

    # Idempotent fingerprint
    fp = f"supplier_price_v1|{product.get('Manufacturer')}|{product.get('Model')}|{product.get('Part_number')}|{product.get('NSN')}"
    prior = (row.get("supplier_price_research") or {})
    if prior.get("query_fingerprint") == fp and prior.get("evidence") is not None:
        return {
            "executed": True,
            "reused": True,
            "evidence": prior.get("evidence") or [],
            "OpenAI": 0,
            "paid": 0,
            "raw_notes": prior.get("raw_notes"),
        }

    try:
        from cost_governor import get_cost_governor, TIER_1_ACTIVE

        gov = get_cost_governor()
        if hasattr(gov, "authorize"):
            oid = str(row.get("canonical_id") or "")
            auth = gov.authorize(
                {
                    "provider": "openai",
                    "action_type": "AI_COMPLETION",
                    "estimated_max_cost": 0.15,
                    "priority_tier": TIER_1_ACTIVE,
                    "tracked": True,
                    "question": (
                        f"Public distributor/list price for "
                        f"{product.get('Manufacturer')} {product.get('Model') or product.get('Part_number') or product.get('NSN')}"
                    ),
                    "could_change_decision": True,
                    "deal_id": oid,
                    "opportunity_id": oid,
                    "idempotency_key": f"supplier_price_v1:{fp}",
                }
            )
            if isinstance(auth, dict) and not auth.get("authorized", True):
                return {
                    "executed": False,
                    "reason": f"cost_governor_blocked:{auth.get('cost_status') or auth.get('reason')}",
                    "evidence": [],
                    "OpenAI": 0,
                    "paid": 0,
                }
    except Exception as exc:
        return {"executed": False, "reason": f"cost_governor:{exc}", "evidence": [], "OpenAI": 0, "paid": 0}

    try:
        from openai_runtime import create_response, text_part, extract_json_object
        from ai_model_router import FunnelStage
    except Exception as exc:
        return {"executed": False, "reason": f"openai_unavailable:{exc}", "evidence": [], "OpenAI": 0, "paid": 0}

    instructions = (
        "Find PUBLICLY LISTED commercial prices for this exact product if available. "
        "Research only — do not invent prices, part numbers, or URLs. "
        "Return JSON only: {"
        '"prices":[{"amount":number,"currency":"USD","source_url":str,"source_name":str,'
        '"match_confidence":"HIGH|MEDIUM|LOW","level":"LEVEL_1_ACTUAL_SUPPLIER_PRICE|LEVEL_2_PUBLIC_COMMERCIAL|LEVEL_3_COMPARABLE",'
        '"product_matched":str,"as_of":str}],'
        '"distributors":[{"name":str,"website":str,"role":str}],'
        '"notes":str,"identity_sufficient":bool}. '
        "If no public price found, return prices:[]."
    )
    try:
        raw = create_response(
            task="m3_supplier_public_price_research",
            instructions=instructions,
            content=[
                text_part(
                    json.dumps(
                        {
                            "title": row.get("title"),
                            "agency": row.get("agency"),
                            "product": product,
                            "solicitation": row.get("solicitation_number"),
                        },
                        default=str,
                    )
                )
            ],
            max_output_tokens=900,
            web_search=True,
            funnel_stage=FunnelStage.STAGE_3,
            automatic=True,
            notice_id=str(row.get("canonical_id") or "")[:80] or None,
        )
    except Exception as exc:
        return {"executed": False, "reason": f"web_search_failed:{exc}", "evidence": [], "OpenAI": 0, "paid": 0}

    parsed = {}
    try:
        parsed = extract_json_object(raw) if raw else {}
        if not isinstance(parsed, dict):
            parsed = {}
    except Exception as exc:
        return {
            "executed": True,
            "reused": False,
            "evidence": [],
            "extra_channels": [],
            "OpenAI": 1,
            "paid": 1,
            "raw_notes": f"json_parse_failed:{exc}",
            "query_fingerprint": fp,
            "reason": f"json_parse_failed:{exc}",
        }

    evidence: list[dict[str, Any]] = []
    for p in parsed.get("prices") or []:
        if not isinstance(p, dict):
            continue
        amt = _num(p.get("amount"))
        src = p.get("source_url") or p.get("source_name")
        if amt is None or not src:
            continue
        # Reject invent-looking zeros / absurd values without source URL
        level = str(p.get("level") or PRICE_LEVEL_2_PUBLIC)
        if level not in {PRICE_LEVEL_1_ACTUAL, PRICE_LEVEL_2_PUBLIC, PRICE_LEVEL_3_COMPARABLE}:
            level = PRICE_LEVEL_2_PUBLIC
        # Without http source, demote to LEVEL_3 at best
        if isinstance(src, str) and not src.startswith("http"):
            level = PRICE_LEVEL_3_COMPARABLE
        evidence.append(
            {
                "level": level,
                "amount": amt,
                "unit": p.get("currency") or "USD",
                "Source": src,
                "Date": p.get("as_of") or _utc(),
                "Product_match_confidence": p.get("match_confidence") or SCORE_LOW,
                "product_matched": p.get("product_matched") or product.get("Model"),
            }
        )

    extra_channels = []
    for d in parsed.get("distributors") or []:
        if isinstance(d, dict) and d.get("name"):
            extra_channels.append(
                {
                    "company": d.get("name"),
                    "website": d.get("website") or "UNKNOWN",
                    "role": d.get("role") or DISTRIBUTOR,
                    "product_match": product.get("Model"),
                    "evidence_source": "openai_web_search",
                    "confidence": SCORE_LOW,
                }
            )

    return {
        "executed": True,
        "reused": False,
        "evidence": evidence,
        "extra_channels": extra_channels,
        "OpenAI": 1,
        "paid": 1,
        "raw_notes": parsed.get("notes"),
        "query_fingerprint": fp,
    }


def score_acquisition_cost_confidence(
    *,
    product: dict[str, Any],
    supply: dict[str, Any],
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    """Phase 5 — ACQUISITION_COST_CONFIDENCE."""
    if not product.get("sufficient_for_pricing_research"):
        return {
            "ACQUISITION_COST_CONFIDENCE": "UNKNOWN",
            "reason": PRODUCT_IDENTITY_INCOMPLETE,
            "best_price": None,
            "best_level": PRICE_LEVEL_4_UNKNOWN,
        }
    levels = {e.get("level") for e in evidence}
    best = None
    best_level = PRICE_LEVEL_4_UNKNOWN
    for pref in (PRICE_LEVEL_1_ACTUAL, PRICE_LEVEL_2_PUBLIC, PRICE_LEVEL_3_COMPARABLE):
        cands = [e for e in evidence if e.get("level") == pref]
        if cands:
            best = min(cands, key=lambda e: float(e["amount"]))
            best_level = pref
            break

    sc = supply.get("supplier_confidence")
    if best_level == PRICE_LEVEL_1_ACTUAL and sc in {SCORE_HIGH, SCORE_MEDIUM}:
        conf = SCORE_HIGH
    elif best_level == PRICE_LEVEL_2_PUBLIC and product.get("identity_status") == "COMPLETE":
        conf = SCORE_MEDIUM
    elif best_level == PRICE_LEVEL_3_COMPARABLE:
        conf = SCORE_LOW
    elif sc in {SCORE_HIGH, SCORE_MEDIUM} and product.get("sufficient_for_pricing_research"):
        conf = "UNKNOWN"  # channels known, price not
    else:
        conf = "UNKNOWN"

    return {
        "ACQUISITION_COST_CONFIDENCE": conf,
        "best_price": best.get("amount") if best else None,
        "best_level": best_level if best else PRICE_LEVEL_4_UNKNOWN,
        "best_source": best.get("Source") if best else None,
        "reason": "price_evidence_present" if best else "no_public_price_evidence",
    }


def compute_margin_status(
    row: dict[str, Any],
    *,
    cost_conf: dict[str, Any],
) -> dict[str, Any]:
    """Phase 6 — only calculate when verified/public cost evidence exists."""
    gov = _num(
        row.get("government_revenue")
        or row.get("estimated_value")
        or row.get("solicitation_value")
        or ((row.get("commercial_pricing") or {}).get("government_historical_unit"))
    )
    acq = _num(cost_conf.get("best_price"))
    level = cost_conf.get("best_level")

    if acq is None or level not in {PRICE_LEVEL_1_ACTUAL, PRICE_LEVEL_2_PUBLIC}:
        return {
            "margin_status": MARGIN_PENDING,
            "estimated_gross_margin": None,
            "government_revenue": gov if gov is not None else "UNKNOWN",
            "verified_acquisition_cost": "UNKNOWN",
            "known_fees": "UNKNOWN",
            "notes": ["do_not_calculate_without_cost_evidence"],
        }

    # Quantity: if unit price, multiply when qty known
    qty = _num((row.get("line_items") or [{}])[0].get("quantity") if row.get("line_items") else None)
    acq_total = acq * qty if qty and qty > 1 and acq < 10000 else acq
    # If gov is unit historical and acq is unit — compare units
    if gov is None:
        return {
            "margin_status": MARGIN_PENDING,
            "estimated_gross_margin": None,
            "government_revenue": "UNKNOWN",
            "verified_acquisition_cost": acq_total,
            "acquisition_cost_level": level,
            "known_fees": "UNKNOWN",
            "notes": ["government_revenue_unknown_cannot_compute_margin"],
        }

    fees = 0.0  # unknown fees stay out — do not invent
    margin = gov - acq_total - fees
    return {
        "margin_status": "PRELIMINARY_GROSS_MARGIN",
        "estimated_gross_margin": round(margin, 2),
        "estimated_gross_margin_pct": round((margin / gov) * 100, 1) if gov else None,
        "government_revenue": gov,
        "verified_acquisition_cost": acq_total,
        "acquisition_cost_level": level,
        "known_fees": fees,
        "notes": [
            "fees_unknown_not_invented",
            "preliminary_only_requires_operator_verification",
            f"price_level_{level}",
        ],
    }


def score_first_deal_fit(
    row: dict[str, Any],
    *,
    product: dict[str, Any],
    supply: dict[str, Any],
    cost_conf: dict[str, Any],
) -> dict[str, Any]:
    """Phase 7 — FIRST_DEAL_FIT_SCORE."""
    score = 0
    reasons: list[str] = []
    title = str(row.get("title") or "").lower()

    if product.get("sufficient_for_pricing_research"):
        score += 20
        reasons.append("product_identity_usable")
    if product.get("Manufacturer") in {"Cisco", "Dell", "HP", "Lenovo"}:
        score += 20
        reasons.append("common_branded_it_product")
    if supply.get("supplier_confidence") == SCORE_HIGH:
        score += 20
        reasons.append("multiple_supplier_channels")
    elif supply.get("supplier_confidence") == SCORE_MEDIUM:
        score += 12
        reasons.append("some_supplier_channels")
    if cost_conf.get("ACQUISITION_COST_CONFIDENCE") in {SCORE_HIGH, SCORE_MEDIUM}:
        score += 15
        reasons.append("cost_evidence_present")
    if not any(x in title for x in ("install", "custom", "clearance", "classified", "sole source", "fabricat")):
        score += 15
        reasons.append("low_compliance_burden_signal")
    else:
        score -= 20
        reasons.append("custom_install_or_clearance_risk")
    if any(x in title for x in ("switch", "storage", "laptop", "blade", "bushing", "equipment only")):
        score += 10
        reasons.append("easy_shipping_tangible_goods")

    score = max(0, min(100, score))
    band = SCORE_HIGH if score >= 65 else (SCORE_MEDIUM if score >= 40 else SCORE_LOW)
    return {"FIRST_DEAL_FIT_SCORE": score, "band": band, "reasons": reasons}


def winner_analysis_foundation(row: dict[str, Any]) -> dict[str, Any]:
    """Phase 8 — prepare future winner analysis without claiming profit."""
    awards = row.get("historical_awards") or row.get("award_history") or []
    winners = []
    for a in awards if isinstance(awards, list) else []:
        if not isinstance(a, dict):
            continue
        name = str(a.get("winner") or a.get("vendor") or a.get("awardee") or "").strip()
        if not name:
            continue
        winners.append(
            {
                "Historical_winner": name,
                "type": classify_winner_type(name, a),
                "amount": a.get("amount") or a.get("award_amount") or "UNKNOWN",
                "date": a.get("date") or a.get("award_date") or "UNKNOWN",
            }
        )
    return {
        "kind": "M3WinnerAnalysisFoundation",
        "Historical_winners": winners[:12],
        "Award_frequency": len(winners),
        "Category": row.get("product_category") or "UNKNOWN",
        "Supplier_pattern": "UNKNOWN" if not winners else "SEE_WINNERS",
        "notes": ["do_not_claim_profit_from_historical_awards"],
    }


def next_supplier_action(product: dict[str, Any], cost_conf: dict[str, Any], supply: dict[str, Any]) -> str:
    if not product.get("sufficient_for_pricing_research"):
        return "Find exact part number"
    if cost_conf.get("best_level") == PRICE_LEVEL_4_UNKNOWN:
        return "Locate distributor pricing"
    if supply.get("supplier_confidence") == SCORE_LOW:
        return "Verify approved source"
    if cost_conf.get("ACQUISITION_COST_CONFIDENCE") in {SCORE_HIGH, SCORE_MEDIUM}:
        return "Verify approved source"
    return "Locate distributor pricing"


def build_supplier_intelligence(
    row: dict[str, Any],
    *,
    allow_paid_web: bool = False,
) -> dict[str, Any]:
    """Full supplier intelligence package for one opportunity."""
    product = build_product_identity(row)
    supply = map_supply_chain(row, product)
    evidence = collect_existing_price_evidence(row, product)
    web_meta = {"executed": False, "OpenAI": 0, "paid": 0}
    if allow_paid_web and product.get("sufficient_for_pricing_research") and not evidence:
        web_meta = research_public_pricing_web(row, product, allow_paid=True)
        evidence.extend(web_meta.get("evidence") or [])
        for ch in web_meta.get("extra_channels") or []:
            supply.setdefault("all_channels", []).append(ch)
            if ch.get("role") == DISTRIBUTOR:
                supply.setdefault("Public_distributors", []).append(ch)

    # Deduplicate evidence by source+amount
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
        evidence_summary = {"primary_level": PRICE_LEVEL_4_UNKNOWN, "items": []}
    else:
        best_level = PRICE_LEVEL_4_UNKNOWN
        for pref in (PRICE_LEVEL_1_ACTUAL, PRICE_LEVEL_2_PUBLIC, PRICE_LEVEL_3_COMPARABLE):
            if any(e.get("level") == pref for e in evidence):
                best_level = pref
                break
        evidence_summary = {"primary_level": best_level, "items": evidence}

    cost_conf = score_acquisition_cost_confidence(product=product, supply=supply, evidence=evidence)
    margin = compute_margin_status(row, cost_conf=cost_conf)
    first_deal = score_first_deal_fit(row, product=product, supply=supply, cost_conf=cost_conf)
    winners = winner_analysis_foundation(row)
    action = next_supplier_action(product, cost_conf, supply)

    return {
        "kind": "M3SupplierIntelligence",
        "generated_at": _utc(),
        "Product": product,
        "Supply_chain": supply,
        "Pricing_evidence": evidence_summary,
        "ACQUISITION_COST_CONFIDENCE": cost_conf.get("ACQUISITION_COST_CONFIDENCE"),
        "cost_detail": cost_conf,
        "Margin": margin,
        "FIRST_DEAL_FIT": first_deal,
        "Winner_foundation": winners,
        "Next_Action": action,
        "web_research": {
            "executed": web_meta.get("executed"),
            "reused": web_meta.get("reused"),
            "reason": web_meta.get("reason"),
            "OpenAI": web_meta.get("OpenAI") or 0,
            "paid": web_meta.get("paid") or 0,
        },
        "DEVELOPMENT_NO_OUTREACH": True,
        "commercial_outreach": False,
    }


def _is_branded_it(row: dict[str, Any], product: dict[str, Any] | None = None) -> bool:
    title = str(row.get("title") or "").lower()
    mfr = str((product or {}).get("Manufacturer") or row.get("manufacturer") or "").lower()
    return any(b in title or mfr == b for b in ("cisco", "dell", "hp", "lenovo", "hewlett"))


def _is_dla_parts(row: dict[str, Any]) -> bool:
    agency = str(row.get("agency") or "").lower()
    title = str(row.get("title") or "")
    return "defense logistics" in agency or bool(FSC_TITLE_RE.match(title.strip()))


def build_supplier_research_queue(
    opportunities: list[dict[str, Any]],
    *,
    limit: int = 10,
) -> dict[str, Any]:
    """SUPPLIER_RESEARCH_QUEUE — prioritize Cisco/Dell/DLA/consumables."""
    # Prefer commercial queue ordering when available
    try:
        cq = build_commercial_research_queue(opportunities, limit=50)
        ordered_ids = [c["canonical_id"] for c in cq.get("queue") or []]
        by_id = {r["canonical_id"]: r for r in opportunities if r.get("canonical_id")}
        seed = [by_id[i] for i in ordered_ids if i in by_id]
        # Append remaining
        seen = set(ordered_ids)
        seed.extend([r for r in opportunities if r.get("canonical_id") not in seen])
    except Exception:
        seed = list(opportunities)

    ranked = []
    for row in seed:
        if not isinstance(row, dict) or not row.get("canonical_id"):
            continue
        ci = row.get("commercial_intelligence")
        if not isinstance(ci, dict):
            try:
                ci = build_commercial_intelligence(row)
            except Exception:
                ci = {}
        ranked.append((supplier_research_priority(row, ci), row, ci))
    ranked.sort(key=lambda x: x[0])

    # Balance: ensure branded IT is not crowded out by DLA FSC flood
    branded, dla, other = [], [], []
    for item in ranked:
        row = item[1]
        product = build_product_identity(row)
        if _is_branded_it(row, product):
            branded.append(item)
        elif _is_dla_parts(row):
            dla.append(item)
        else:
            other.append(item)

    branded_slots = min(len(branded), max(3, limit // 3))
    dla_slots = min(len(dla), max(3, limit // 3))
    top: list = []
    top.extend(branded[:branded_slots])
    top.extend(dla[:dla_slots])
    top.extend(other)
    # Fill remaining from full ranked order without dupes
    seen_ids = {t[1]["canonical_id"] for t in top}
    for item in ranked:
        if len(top) >= limit:
            break
        cid = item[1]["canonical_id"]
        if cid in seen_ids:
            continue
        top.append(item)
        seen_ids.add(cid)
    top = top[:limit]

    queue = []
    for _, row, ci in top:
        product = build_product_identity(row)
        queue.append(
            {
                "canonical_id": row.get("canonical_id"),
                "title": row.get("title"),
                "agency": row.get("agency"),
                "manufacturer": product.get("Manufacturer"),
                "product_identity": product.get("identity_status"),
                "commercial_score": ci.get("COMMERCIAL_OPPORTUNITY_SCORE") or row.get("commercial_opportunity_score"),
                "category": row.get("product_category"),
                "deprioritized": _deprioritize(row),
            }
        )
    return {
        "kind": "SUPPLIER_RESEARCH_QUEUE",
        "question": "Which opportunities should get acquisition-channel research first?",
        "queue_size": len(queue),
        "queue": queue,
        "DEVELOPMENT_NO_OUTREACH": True,
    }


def analyze_supplier_top_opportunities(
    store: Any,
    *,
    limit: int = 10,
    allow_paid_web: bool = True,
) -> dict[str, Any]:
    """Run supplier intelligence on TOP N commercial candidates."""
    rows = store.all() if hasattr(store, "all") else list(store)
    q = build_supplier_research_queue(rows, limit=limit)
    targets = []
    by_id = {r["canonical_id"]: r for r in rows if r.get("canonical_id")}
    for item in q.get("queue") or []:
        row = by_id.get(item["canonical_id"])
        if row:
            targets.append(row)

    results = []
    paid_total = 0
    openai_total = 0
    level_counts = {PRICE_LEVEL_1_ACTUAL: 0, PRICE_LEVEL_2_PUBLIC: 0, PRICE_LEVEL_3_COMPARABLE: 0, PRICE_LEVEL_4_UNKNOWN: 0}
    products_identified = 0
    manufacturers = set()
    distributors = set()
    index_updates: dict[str, Any] = {}

    for row in targets:
        si = build_supplier_intelligence(row, allow_paid_web=allow_paid_web)
        paid_total += int((si.get("web_research") or {}).get("paid") or 0)
        openai_total += int((si.get("web_research") or {}).get("OpenAI") or 0)

        # Persist on row + durable SI index (pipeline concurrent saves cannot wipe index)
        full = {**(store.get(row["canonical_id"]) or row)}
        full["supplier_intelligence"] = si
        # Keep price research fingerprint for idempotency
        wr = si.get("web_research") or {}
        if wr.get("executed"):
            full["supplier_price_research"] = {
                "query_fingerprint": f"supplier_price_v1|{(si.get('Product') or {}).get('Manufacturer')}|{(si.get('Product') or {}).get('Model')}|{(si.get('Product') or {}).get('Part_number')}|{(si.get('Product') or {}).get('NSN')}",
                "evidence": (si.get("Pricing_evidence") or {}).get("items") or [],
                "raw_notes": wr.get("reason"),
                "at": _utc(),
            }
        store._rows[row["canonical_id"]] = full
        index_updates[row["canonical_id"]] = si

        product = si.get("Product") or {}
        if product.get("sufficient_for_pricing_research"):
            products_identified += 1
        if product.get("Manufacturer") not in {None, "UNKNOWN", "DLA_CATALOG_ITEM"}:
            manufacturers.add(product["Manufacturer"])
        for d in (si.get("Supply_chain") or {}).get("Public_distributors") or []:
            if d.get("company"):
                distributors.add(d["company"])
        for d in (si.get("Supply_chain") or {}).get("Manufacturer") or []:
            if d.get("company"):
                manufacturers.add(d["company"])

        lvl = (si.get("Pricing_evidence") or {}).get("primary_level") or PRICE_LEVEL_4_UNKNOWN
        level_counts[lvl] = level_counts.get(lvl, 0) + 1

        results.append(
            {
                "canonical_id": row.get("canonical_id"),
                "Opportunity": row.get("title"),
                "Product": product.get("Technical_description") or product.get("Model"),
                "Manufacturer": product.get("Manufacturer"),
                "Supplier_evidence": (si.get("Supply_chain") or {}).get("supplier_confidence"),
                "Cost_evidence": si.get("ACQUISITION_COST_CONFIDENCE"),
                "pricing_level": lvl,
                "Government_value": (si.get("Margin") or {}).get("government_revenue"),
                "Margin_status": (si.get("Margin") or {}).get("margin_status"),
                "First_deal_fit": (si.get("FIRST_DEAL_FIT") or {}).get("band"),
                "First_deal_score": (si.get("FIRST_DEAL_FIT") or {}).get("FIRST_DEAL_FIT_SCORE"),
                "Next_Action": si.get("Next_Action"),
                "web_research_reason": (si.get("web_research") or {}).get("reason"),
                "web_executed": (si.get("web_research") or {}).get("executed"),
                "channels": [
                    c.get("company")
                    for c in ((si.get("Supply_chain") or {}).get("all_channels") or [])[:6]
                ],
            }
        )

    # Batch durable SI index write
    try:
        existing = load_supplier_intel_index()
        by_id = existing.get("by_id") if isinstance(existing.get("by_id"), dict) else {}
        if not by_id and existing and "by_id" not in existing:
            by_id = {k: v for k, v in existing.items() if isinstance(v, dict)}
        by_id.update(index_updates)
        save_supplier_intel_index(by_id)
    except Exception:
        log.exception("Batch SI index save failed")

    save_ok = False
    save_error = None
    index_count = 0
    try:
        store.save()
        # Confirm at least one persisted on row or durable index
        idx = load_supplier_intel_index()
        by_id = idx.get("by_id") if isinstance(idx.get("by_id"), dict) else idx
        if isinstance(by_id, dict):
            index_count = len([v for v in by_id.values() if isinstance(v, dict) and v.get("kind") == "M3SupplierIntelligence"])
            if index_count == 0 and isinstance(by_id, dict):
                index_count = len(by_id)
        save_ok = index_count > 0 or any(
            bool((store.get(r["canonical_id"]) or {}).get("supplier_intelligence"))
            for r in results
            if r.get("canonical_id")
        )
    except Exception as exc:
        save_error = str(exc)

    return {
        "kind": "M3SupplierAnalysisRun",
        "generated_at": _utc(),
        "analyzed": len(results),
        "products_identified": products_identified,
        "manufacturers_found": sorted(manufacturers),
        "distributors_found": sorted(distributors),
        "pricing_evidence_levels": {
            "LEVEL_1": level_counts.get(PRICE_LEVEL_1_ACTUAL, 0),
            "LEVEL_2": level_counts.get(PRICE_LEVEL_2_PUBLIC, 0),
            "LEVEL_3": level_counts.get(PRICE_LEVEL_3_COMPARABLE, 0),
            "LEVEL_4": level_counts.get(PRICE_LEVEL_4_UNKNOWN, 0),
        },
        "TOP_OPPORTUNITIES": results,
        "OpenAI": openai_total,
        "paid": paid_total,
        "persist_ok": save_ok,
        "persist_error": save_error,
        "index_count": index_count,
        "DEVELOPMENT_NO_OUTREACH": True,
        "commercial_outreach": False,
    }


def deal_room_supplier_section(row: dict[str, Any]) -> dict[str, Any]:
    si = row.get("supplier_intelligence")
    if not isinstance(si, dict) or si.get("kind") != "M3SupplierIntelligence":
        persisted = get_persisted_supplier_intelligence(str(row.get("canonical_id") or ""))
        if isinstance(persisted, dict) and persisted.get("kind") == "M3SupplierIntelligence":
            si = persisted
        else:
            si = build_supplier_intelligence(row, allow_paid_web=False)
    product = si.get("Product") or {}
    supply = si.get("Supply_chain") or {}
    pricing = si.get("Pricing_evidence") or {}
    return {
        "kind": "M3DealRoomSupplierIntelligence",
        "Product": product.get("Technical_description") or product.get("Model"),
        "Manufacturer": product.get("Manufacturer"),
        "Possible_Suppliers": [
            {"company": c.get("company"), "role": c.get("role"), "website": c.get("website")}
            for c in (supply.get("all_channels") or [])[:8]
        ],
        "Pricing_Evidence": {
            "level": pricing.get("primary_level"),
            "items": (pricing.get("items") or [])[:5],
        },
        "Cost_Confidence": si.get("ACQUISITION_COST_CONFIDENCE"),
        "Margin_Status": (si.get("Margin") or {}).get("margin_status"),
        "First_Deal_Fit": (si.get("FIRST_DEAL_FIT") or {}).get("band"),
        "Next_Action": si.get("Next_Action"),
        "full": si,
    }
