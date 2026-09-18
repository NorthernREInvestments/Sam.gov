"""Commercial Product Matching + Supplier Pricing Research Engine.

Bridge: government specification → commercial candidates → suppliers →
observed pricing → acquisition economics.

Never invent products, prices, or supplier authorization.
Unknown is acceptable. Public price ≠ wholesale fail.
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from typing import Any

from application_clock import now_utc
from m3_acquisition_target_engine import (
    LEVEL_5_NO_PRICE,
    PUBLIC_ECONOMICS_FAILS_WHOLESALE_VERIFICATION_REQUIRED,
    WHOLESALE_ACCESS_UNVERIFIED,
    build_acquisition_price_gap,
    build_compliance_match_profile,
    build_government_revenue_profile,
    calculate_acquisition_target,
    collect_pricing_ladder,
    discover_supplier_channels,
)
from m3_deal_economics import (
    STATUS_UNKNOWN,
    build_deal_economics_profile,
    resolve_quantity,
    target_profit_usd,
)
from m3_product_identity_resolution import (
    CATEGORY_ONLY,
    EXACT_IDENTITY,
    MATCH_HIGH,
    MATCH_LOW,
    MATCH_MEDIUM,
    MATCH_UNKNOWN,
    READY_FOR_SUPPLIER_SEARCH,
    READY_WITH_SPECIFICATIONS,
    SPECIFICATION_IDENTITY,
    resolve_opportunity_product_identities,
)
from m3_supplier_intelligence import (
    DISTRIBUTOR,
    OEM,
    RESELLER,
    collect_existing_price_evidence,
    map_supply_chain,
    research_public_pricing_web,
)

log = logging.getLogger("govtracker.m3_commercial_matching")

MATCH_INDEX_KEY = "m3_commercial_product_matching_v1"
LEARNING_KEY = "m3_commercial_search_learning_v1"

PUBLIC_PRICE_ONLY = "PUBLIC_PRICE_ONLY"
DISTRIBUTOR_PATH_FOUND = "DISTRIBUTOR_PATH_FOUND"
MANUFACTURER_PATH_FOUND = "MANUFACTURER_PATH_FOUND"
VOLUME_PRICING_POSSIBLE = "VOLUME_PRICING_POSSIBLE"
WHOLESALE_UNVERIFIED = "WHOLESALE_UNVERIFIED"

Q_NEEDS_PRODUCT_MATCH = "NEEDS_PRODUCT_MATCH"
Q_NEEDS_SPEC_VALIDATION = "NEEDS_SPEC_VALIDATION"
Q_NEEDS_SUPPLIER_RESEARCH = "NEEDS_SUPPLIER_RESEARCH"
Q_NEEDS_PRICING = "NEEDS_PRICING"
Q_NEEDS_WHOLESALE_VERIFICATION = "NEEDS_WHOLESALE_VERIFICATION"
Q_NEEDS_GOVERNMENT_REVENUE_RESEARCH = "NEEDS_GOVERNMENT_REVENUE_RESEARCH"
Q_READY_FOR_ECONOMICS = "READY_FOR_ECONOMICS"

VA_ALLOWED = frozenset(
    {
        "RESEARCH_PRODUCTS",
        "COLLECT_PRICING_EVIDENCE",
        "COMPARE_SPECIFICATIONS",
        "ATTACH_SOURCES",
        "ADD_NOTES",
        "NOTE",
        "UPDATE_STATUS",
    }
)
VA_FORBIDDEN = frozenset(
    {
        "APPROVE_COMPLIANCE",
        "CONTACT_SUPPLIER",
        "NEGOTIATE_PRICING",
        "SUBMIT_BID",
        "CHANGE_SCORING",
        "ALTER_SCORING",
        "APPLY_FOR_ACCOUNT",
        "APPROVE_DEAL",
    }
)

PRIORITY_TITLES = (
    "Tungsten-Carbide Blades",
    "Wildflower",
    "Native Grass Seed",
    "Law Enforcement Badges",
    "Wheelchair Lift",
)

AGENCY_SKU_RE = re.compile(r"\b(\d{6,9})\s*[-–—]\s*(.+)$")
CATALOG_RE = re.compile(
    r"\b(?:catalog|cat\.?|item)\s*(?:#|no\.?|number)?\s*[:#]?\s*([A-Z0-9][A-Z0-9\-/]{2,24})\b",
    re.I,
)
MFR_LINE_RE = re.compile(
    r"(?i)(?:manufacturer|mfr|oem|brand)\s*(?:name)?\s*[-:=]\s*([A-Za-z][A-Za-z0-9 &\-.]{2,40})"
)
PRICE_IN_TEXT_RE = re.compile(
    r"(?:\$|USD\s*)(\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\d+(?:\.\d{2})?)"
)
MODEL_HINT_RE = re.compile(
    r"\b(?:model|p/?n|part\s*(?:no\.?|number)?)\s*[:#]?\s*([A-Z0-9][A-Z0-9\-/]{2,24})\b",
    re.I,
)


def _utc() -> str:
    return now_utc().isoformat()


def _known(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, (list, tuple, set, dict)):
        return len(v) > 0
    if isinstance(v, str):
        return v.strip() not in {"", "UNKNOWN", "unknown"}
    return True


def _num(v: Any) -> float | None:
    if v is None or v == "" or v == "UNKNOWN":
        return None
    try:
        return float(str(v).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None


def _load_setting(key: str) -> dict[str, Any]:
    try:
        from database import SessionLocal
        from models import AppSetting

        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == key).one_or_none()
            if not row or not row.value:
                return {"by_id": {}, "updated_at": None}
            data = json.loads(row.value)
            return data if isinstance(data, dict) else {"by_id": {}, "updated_at": None}
        finally:
            db.close()
    except Exception:
        return {"by_id": {}, "updated_at": None}


def _save_setting(key: str, index: dict[str, Any]) -> None:
    index["updated_at"] = _utc()
    try:
        from database import SessionLocal
        from models import AppSetting

        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == key).one_or_none()
            payload = json.dumps(index, default=str)
            if row is None:
                db.add(AppSetting(key=key, value=payload))
            else:
                row.value = payload
            db.commit()
        finally:
            db.close()
    except Exception:
        log.debug("commercial matching index save failed", exc_info=True)


def load_match_index() -> dict[str, Any]:
    return _load_setting(MATCH_INDEX_KEY)


def save_match_index(index: dict[str, Any]) -> None:
    _save_setting(MATCH_INDEX_KEY, index)


def load_search_learning() -> dict[str, Any]:
    idx = _load_setting(LEARNING_KEY)
    idx.setdefault("successful_terms", {})
    idx.setdefault("failed_terms", {})
    return idx


def save_search_learning(index: dict[str, Any]) -> None:
    _save_setting(LEARNING_KEY, index)


def _document_blob(row: dict[str, Any], *, limit: int = 20000) -> str:
    parts: list[str] = []
    for d in row.get("documents") or []:
        if not isinstance(d, dict):
            continue
        for k in ("extracted_text", "text_preview", "text", "content"):
            tx = d.get(k)
            if _known(tx):
                parts.append(str(tx)[:8000])
                break
    for k in ("attachment_text", "governing_text", "package_text", "description"):
        if _known(row.get(k)):
            parts.append(str(row.get(k))[:4000])
    return "\n".join(parts)[:limit]


def _identity_bundle(row: dict[str, Any]) -> dict[str, Any]:
    full = row.get("product_identity_resolution_full")
    if isinstance(full, dict) and full.get("kind") == "M3ProductIdentityResolution":
        return full
    return resolve_opportunity_product_identities(row, update_learning=False)


def _eligible_profiles(identity: dict[str, Any], row: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    title = str(row.get("title") or "").lower()
    for p in identity.get("PRODUCT_IDENTITY_RESOLUTION_PROFILES") or []:
        if not isinstance(p, dict):
            continue
        readiness = (p.get("SUPPLIER_RESEARCH_READINESS_SCORE") or {}).get("status")
        itype = p.get("Identity_type")
        if readiness in {READY_WITH_SPECIFICATIONS, READY_FOR_SUPPLIER_SEARCH}:
            out.append(p)
        elif itype in {SPECIFICATION_IDENTITY, EXACT_IDENTITY}:
            out.append(p)
        elif itype == CATEGORY_ONLY and any(t.lower() in title for t in PRIORITY_TITLES):
            out.append(p)
    if not out:
        primary = identity.get("primary_profile")
        if isinstance(primary, dict):
            out = [primary]
    return out


def build_search_strategies(profile: dict[str, Any], row: dict[str, Any]) -> list[dict[str, Any]]:
    desc = str(profile.get("Original_description") or profile.get("Resolved_product_name") or "")
    spec = profile.get("SPECIFICATION_PROFILE") or profile.get("Specifications") or {}

    def _flat(v: Any) -> str:
        if isinstance(v, list):
            return " ".join(str(x) for x in v[:4] if _known(x))
        return str(v) if _known(v) else ""

    materials = _flat(spec.get("Materials") if isinstance(spec, dict) else None)
    dims = _flat(spec.get("Dimensions") if isinstance(spec, dict) else None)
    app = _flat(spec.get("Application") if isinstance(spec, dict) else None)
    standards = _flat(spec.get("Standards") if isinstance(spec, dict) else None)

    strategies = [
        {"approach": "exact_description", "query": desc.strip()[:180], "status": "PLANNED"},
        {
            "approach": "specification",
            "query": " ".join(x for x in (materials, dims, app, standards) if x).strip()[:180],
            "status": "PLANNED",
        },
        {
            "approach": "category",
            "query": f"{row.get('title') or ''} {desc}".strip()[:160],
            "status": "PLANNED",
        },
        {
            "approach": "manufacturer_catalog",
            "query": (
                f"{profile.get('Manufacturer')} {profile.get('Model_number') or profile.get('Manufacturer_part_number')}"
                if _known(profile.get("Manufacturer"))
                else ""
            ),
            "status": "PLANNED",
        },
        {
            "approach": "distributor_catalog",
            "query": f"{desc} distributor catalog buy".strip()[:160],
            "status": "PLANNED",
        },
    ]
    for s in strategies:
        if not s.get("query"):
            s["status"] = "SKIPPED"
    return strategies


def update_search_learning(
    strategies: list[dict[str, Any]], candidates: list[dict[str, Any]]
) -> dict[str, Any]:
    learning = load_search_learning()
    success = learning.setdefault("successful_terms", {})
    failed = learning.setdefault("failed_terms", {})
    named = [
        c
        for c in candidates
        if c.get("candidate_origin")
        not in {"specification_search_brief", "agency_catalog_reference"}
        and (
            _known(c.get("Manufacturer"))
            or _known(c.get("Product_URL_reference"))
            or c.get("candidate_origin")
            in {"botanical_commercial_identity", "web_research_evidenced", "va_attached"}
        )
    ]
    for s in strategies:
        q = str(s.get("query") or "").strip().lower()
        if not q or s.get("status") == "SKIPPED":
            continue
        if named:
            success[q] = int(success.get(q) or 0) + 1
            s["status"] = "SUCCEEDED"
        else:
            failed[q] = int(failed.get(q) or 0) + 1
            s["status"] = "NO_NAMED_PRODUCT_YET"
    learning["kind"] = "SEARCH_LEARNING_PROFILE"
    learning["updated_at"] = _utc()
    save_search_learning(learning)
    return {
        "kind": "SEARCH_LEARNING_PROFILE",
        "strategies": strategies,
        "successful_terms_sample": list(success.keys())[:15],
        "failed_terms_sample": list(failed.keys())[:15],
    }


def _candidate_base(row: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "COMMERCIAL_PRODUCT_CANDIDATE",
        "Opportunity": row.get("title"),
        "Opportunity_ID": row.get("canonical_id"),
        "Government_requirement": profile.get("Original_description")
        or profile.get("Resolved_product_name"),
        "Line_item_ID": profile.get("Line_item"),
        "Commercial_product_name": "UNKNOWN",
        "Manufacturer": "UNKNOWN",
        "Model": "UNKNOWN",
        "Part_number": "UNKNOWN",
        "SKU": "UNKNOWN",
        "Supplier": "UNKNOWN",
        "Product_URL_reference": "UNKNOWN",
        "Specifications": profile.get("Specifications") or {},
        "Match_confidence": MATCH_UNKNOWN,
        "Evidence_source": "UNKNOWN",
    }


def extract_evidence_candidates(
    row: dict[str, Any], profile: dict[str, Any]
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    desc = str(profile.get("Original_description") or profile.get("Resolved_product_name") or "")
    blob = _document_blob(row)
    spec = profile.get("SPECIFICATION_PROFILE") or {}

    if (
        _known(profile.get("Manufacturer"))
        or _known(profile.get("Manufacturer_part_number"))
        or _known(profile.get("Model_number"))
    ):
        c = _candidate_base(row, profile)
        c.update(
            {
                "Commercial_product_name": profile.get("Resolved_product_name") or desc or "UNKNOWN",
                "Manufacturer": profile.get("Manufacturer")
                if _known(profile.get("Manufacturer"))
                else "UNKNOWN",
                "Model": profile.get("Model_number")
                if _known(profile.get("Model_number"))
                else "UNKNOWN",
                "Part_number": profile.get("Manufacturer_part_number")
                if _known(profile.get("Manufacturer_part_number"))
                else "UNKNOWN",
                "SKU": profile.get("SKU") if _known(profile.get("SKU")) else "UNKNOWN",
                "Match_confidence": profile.get("Confidence") or MATCH_MEDIUM,
                "Evidence_source": profile.get("Evidence_source") or "identity_resolution",
                "candidate_origin": "resolved_identity",
            }
        )
        candidates.append(c)

    m = AGENCY_SKU_RE.match(desc.strip())
    if m:
        c = _candidate_base(row, profile)
        c.update(
            {
                "Commercial_product_name": m.group(2).strip(),
                "SKU": m.group(1),
                "Match_confidence": MATCH_LOW,
                "Evidence_source": "agency_line_item_code",
                "candidate_origin": "agency_catalog_reference",
                "notes": ["agency_sku_is_reference_not_oem_part"],
            }
        )
        candidates.append(c)

    paren = re.findall(r"\(([A-Z][a-z]+\s+[a-z]+)\)", desc)
    for name in paren[:2]:
        c = _candidate_base(row, profile)
        c.update(
            {
                "Commercial_product_name": f"{name} seed",
                "Match_confidence": MATCH_MEDIUM,
                "Evidence_source": "solicitation_botanical_identity",
                "candidate_origin": "botanical_commercial_identity",
                "Specifications": {
                    **(profile.get("Specifications") or {}),
                    "Species": name,
                    "Application": "native grass / wildflower seeding",
                },
                "notes": ["species_commercially_traded_oem_unverified"],
            }
        )
        candidates.append(c)

    for mm in MFR_LINE_RE.finditer(blob):
        name = mm.group(1).strip().rstrip(".-")
        if len(name) < 3 or name.lower() in {"name", "names", "unknown", "n/a", "tbd"}:
            continue
        if any(str(x.get("Manufacturer")).lower() == name.lower() for x in candidates):
            continue
        c = _candidate_base(row, profile)
        model = "UNKNOWN"
        pm = MODEL_HINT_RE.search(blob[mm.start() : mm.start() + 120])
        if pm:
            model = pm.group(1)
        c.update(
            {
                "Commercial_product_name": desc or name,
                "Manufacturer": name,
                "Model": model,
                "Match_confidence": MATCH_LOW,
                "Evidence_source": "solicitation_manufacturer_field",
                "candidate_origin": "document_manufacturer_mention",
                "notes": ["manufacturer_field_present_product_unconfirmed"],
            }
        )
        candidates.append(c)

    for mm in CATALOG_RE.finditer(blob):
        cat = mm.group(1)
        if any(str(x.get("SKU")) == cat or str(x.get("Part_number")) == cat for x in candidates):
            continue
        c = _candidate_base(row, profile)
        c.update(
            {
                "Commercial_product_name": desc or "UNKNOWN",
                "SKU": cat,
                "Part_number": cat,
                "Match_confidence": MATCH_LOW,
                "Evidence_source": "document_catalog_reference",
                "candidate_origin": "document_catalog_reference",
            }
        )
        candidates.append(c)

    if not candidates and (
        (isinstance(spec, dict) and spec.get("well_defined"))
        or profile.get("Identity_type") in {SPECIFICATION_IDENTITY, CATEGORY_ONLY, EXACT_IDENTITY}
        or _known(desc)
    ):
        c = _candidate_base(row, profile)
        c.update(
            {
                "Commercial_product_name": profile.get("Resolved_product_name") or desc or "UNKNOWN",
                "Match_confidence": MATCH_UNKNOWN,
                "Evidence_source": "specification_search_brief",
                "candidate_origin": "specification_search_brief",
                "status": "NEEDS_COMMERCIAL_MATCH_RESEARCH",
                "Specifications": {
                    "Materials": (spec.get("Materials") if isinstance(spec, dict) else None)
                    or (profile.get("Specifications") or {}).get("Materials"),
                    "Dimensions": (spec.get("Dimensions") if isinstance(spec, dict) else None)
                    or (profile.get("Specifications") or {}).get("Dimensions"),
                    "Standards": (spec.get("Standards") if isinstance(spec, dict) else None)
                    or (profile.get("Specifications") or {}).get("Standards"),
                    "Application": (spec.get("Application") if isinstance(spec, dict) else None)
                    or (profile.get("Specifications") or {}).get("Application"),
                    "Required_features": (
                        spec.get("Required_features") if isinstance(spec, dict) else None
                    )
                    or (profile.get("Specifications") or {}).get("Required_features"),
                },
            }
        )
        candidates.append(c)

    # Persisted web research candidates (evidenced URLs only)
    prior_web = row.get("commercial_match_research") or {}
    if isinstance(prior_web, dict):
        for a in prior_web.get("candidates") or []:
            if not isinstance(a, dict):
                continue
            url = a.get("Product_URL_reference") or a.get("url")
            if not (isinstance(url, str) and url.startswith("http")):
                continue
            c = _candidate_base(row, profile)
            c.update(
                {
                    "Commercial_product_name": a.get("Commercial_product_name")
                    or a.get("Product_name")
                    or "UNKNOWN",
                    "Manufacturer": a.get("Manufacturer") or "UNKNOWN",
                    "Model": a.get("Model") or "UNKNOWN",
                    "Part_number": a.get("Part_number") or "UNKNOWN",
                    "SKU": a.get("SKU") or "UNKNOWN",
                    "Supplier": a.get("Supplier") or "UNKNOWN",
                    "Product_URL_reference": url,
                    "Specifications": a.get("Specifications") or {},
                    "Match_confidence": a.get("Match_confidence") or MATCH_LOW,
                    "Evidence_source": a.get("Evidence_source") or url,
                    "candidate_origin": "web_research_evidenced",
                }
            )
            candidates.append(c)

    attached = row.get("commercial_product_candidates") or row.get("va_commercial_candidates") or []
    if isinstance(attached, list):
        for a in attached[:8]:
            if not isinstance(a, dict):
                continue
            if not (
                _known(a.get("Commercial_product_name") or a.get("Product_name"))
                or _known(a.get("Manufacturer"))
            ):
                continue
            c = _candidate_base(row, profile)
            c.update(
                {
                    "Commercial_product_name": a.get("Commercial_product_name")
                    or a.get("Product_name")
                    or "UNKNOWN",
                    "Manufacturer": a.get("Manufacturer") or "UNKNOWN",
                    "Model": a.get("Model") or "UNKNOWN",
                    "Part_number": a.get("Part_number") or "UNKNOWN",
                    "SKU": a.get("SKU") or "UNKNOWN",
                    "Supplier": a.get("Supplier") or "UNKNOWN",
                    "Product_URL_reference": a.get("Product_URL_reference")
                    or a.get("url")
                    or "UNKNOWN",
                    "Specifications": a.get("Specifications") or profile.get("Specifications") or {},
                    "Match_confidence": a.get("Match_confidence") or MATCH_MEDIUM,
                    "Evidence_source": a.get("Evidence_source") or "va_attached",
                    "candidate_origin": "va_attached",
                }
            )
            candidates.append(c)

    return candidates


def research_commercial_matches_web(
    row: dict[str, Any],
    profile: dict[str, Any],
    strategies: list[dict[str, Any]],
    *,
    allow_paid: bool = False,
) -> dict[str, Any]:
    if not allow_paid:
        return {"executed": False, "reason": "paid_blocked", "candidates": [], "prices": [], "OpenAI": 0, "paid": 0}

    queries = [s["query"] for s in strategies if s.get("status") != "SKIPPED" and s.get("query")]
    if not queries:
        return {"executed": False, "reason": "no_queries", "candidates": [], "prices": [], "OpenAI": 0, "paid": 0}

    fp = f"commercial_match_v1|{row.get('canonical_id')}|{profile.get('Line_item')}|{queries[0][:80]}"
    prior = row.get("commercial_match_research") or {}
    if isinstance(prior, dict) and prior.get("query_fingerprint") == fp and prior.get("candidates") is not None:
        return {
            "executed": True,
            "reused": True,
            "candidates": prior.get("candidates") or [],
            "prices": prior.get("prices") or [],
            "OpenAI": 0,
            "paid": 0,
        }

    try:
        from cost_governor import TIER_1_ACTIVE, get_cost_governor

        gov = get_cost_governor()
        if hasattr(gov, "authorize"):
            oid = str(row.get("canonical_id") or "")
            auth = gov.authorize(
                {
                    "provider": "openai",
                    "action_type": "AI_COMPLETION",
                    "estimated_max_cost": 0.20,
                    "priority_tier": TIER_1_ACTIVE,
                    "tracked": True,
                    "question": f"Commercial products matching: {queries[0][:120]}",
                    "could_change_decision": True,
                    "deal_id": oid,
                    "opportunity_id": oid,
                    "idempotency_key": f"commercial_match_v1:{fp}",
                }
            )
            if isinstance(auth, dict) and not auth.get("authorized", True):
                return {
                    "executed": False,
                    "reason": f"cost_governor_blocked:{auth.get('cost_status') or auth.get('reason')}",
                    "candidates": [],
                    "prices": [],
                    "OpenAI": 0,
                    "paid": 0,
                }
    except Exception as exc:
        return {
            "executed": False,
            "reason": f"cost_governor:{exc}",
            "candidates": [],
            "prices": [],
            "OpenAI": 0,
            "paid": 0,
        }

    try:
        from ai_model_router import FunnelStage
        from openai_runtime import create_response, extract_json_object, text_part
    except Exception as exc:
        return {
            "executed": False,
            "reason": f"openai_unavailable:{exc}",
            "candidates": [],
            "prices": [],
            "OpenAI": 0,
            "paid": 0,
        }

    instructions = (
        "Find REAL commercially available products that may satisfy this government specification. "
        "Research only. Do NOT invent manufacturers, models, part numbers, SKUs, prices, or URLs. "
        "Only include products with a public product page or catalog URL. "
        "Return JSON only: {"
        '"products":[{"name":str,"manufacturer":str,"model":str,"part_number":str,"sku":str,'
        '"supplier":str,"url":str,"specifications":object,"match_confidence":"HIGH|MEDIUM|LOW",'
        '"notes":str}],'
        '"prices":[{"amount":number,"currency":"USD","source_url":str,"product_name":str,'
        '"level":"LEVEL_2|LEVEL_3|LEVEL_4","as_of":str}],'
        '"notes":str}. '
        "If none found with evidence, return products:[], prices:[]."
    )
    try:
        raw = create_response(
            task="m3_commercial_product_match_research",
            instructions=instructions,
            content=[
                text_part(
                    json.dumps(
                        {
                            "title": row.get("title"),
                            "agency": row.get("agency"),
                            "requirement": profile.get("Original_description"),
                            "specifications": profile.get("Specifications")
                            or profile.get("SPECIFICATION_PROFILE"),
                            "quantity": profile.get("Quantity"),
                            "search_queries": queries[:5],
                        },
                        default=str,
                    )
                )
            ],
            max_output_tokens=1200,
            web_search=True,
            funnel_stage=FunnelStage.STAGE_3,
            automatic=True,
            notice_id=str(row.get("canonical_id") or "")[:80] or None,
        )
    except Exception as exc:
        return {
            "executed": False,
            "reason": f"web_search_failed:{exc}",
            "candidates": [],
            "prices": [],
            "OpenAI": 0,
            "paid": 0,
        }

    parsed: dict[str, Any] = {}
    try:
        parsed = extract_json_object(raw) if raw else {}
        if not isinstance(parsed, dict):
            parsed = {}
    except Exception as exc:
        return {
            "executed": True,
            "candidates": [],
            "prices": [],
            "OpenAI": 1,
            "paid": 1,
            "reason": f"json_parse_failed:{exc}",
            "query_fingerprint": fp,
        }

    out_cands: list[dict[str, Any]] = []
    for p in parsed.get("products") or []:
        if not isinstance(p, dict):
            continue
        url = p.get("url") or p.get("source_url")
        name = p.get("name") or p.get("product_name")
        mfr = p.get("manufacturer")
        if not (isinstance(url, str) and url.startswith("http")):
            continue
        if not (_known(name) or _known(mfr)):
            continue
        conf = str(p.get("match_confidence") or MATCH_LOW).upper()
        if conf not in {MATCH_HIGH, MATCH_MEDIUM, MATCH_LOW}:
            conf = MATCH_LOW
        c = _candidate_base(row, profile)
        c.update(
            {
                "Commercial_product_name": name or "UNKNOWN",
                "Manufacturer": mfr if _known(mfr) else "UNKNOWN",
                "Model": p.get("model") if _known(p.get("model")) else "UNKNOWN",
                "Part_number": p.get("part_number") if _known(p.get("part_number")) else "UNKNOWN",
                "SKU": p.get("sku") if _known(p.get("sku")) else "UNKNOWN",
                "Supplier": p.get("supplier") if _known(p.get("supplier")) else "UNKNOWN",
                "Product_URL_reference": url,
                "Specifications": p.get("specifications")
                if isinstance(p.get("specifications"), dict)
                else (profile.get("Specifications") or {}),
                "Match_confidence": conf,
                "Evidence_source": url,
                "candidate_origin": "web_research_evidenced",
                "notes": [str(p.get("notes") or "web_evidenced_candidate")][:1],
            }
        )
        out_cands.append(c)

    prices = []
    for pr in parsed.get("prices") or []:
        if not isinstance(pr, dict):
            continue
        amt = _num(pr.get("amount"))
        src = pr.get("source_url")
        if amt is None or not (isinstance(src, str) and src.startswith("http")):
            continue
        prices.append(
            {
                "Price": amt,
                "Source": src,
                "product_name": pr.get("product_name"),
                "level": pr.get("level") or "LEVEL_2",
                "Date": pr.get("as_of") or _utc(),
                "Confidence": MATCH_MEDIUM,
            }
        )

    return {
        "executed": True,
        "reused": False,
        "candidates": out_cands,
        "prices": prices,
        "OpenAI": 1,
        "paid": 1,
        "query_fingerprint": fp,
        "notes": parsed.get("notes"),
    }


def build_commercial_candidates(
    row: dict[str, Any],
    profile: dict[str, Any],
    *,
    allow_paid_web: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    strategies = build_search_strategies(profile, row)
    candidates = extract_evidence_candidates(row, profile)
    web = research_commercial_matches_web(row, profile, strategies, allow_paid=allow_paid_web)
    candidates.extend(web.get("candidates") or [])
    learning = update_search_learning(strategies, candidates)
    return candidates, strategies, learning, web


def build_commercial_compliance_matrix(
    profile: dict[str, Any], candidate: dict[str, Any]
) -> dict[str, Any]:
    origin = candidate.get("candidate_origin")
    if origin == "specification_search_brief":
        return {
            "kind": "COMMERCIAL_COMPLIANCE_MATRIX",
            "rows": [],
            "overall": "SEARCH_BRIEF_NOT_A_PRODUCT",
            "COMPLIANCE_CONFIDENCE": MATCH_UNKNOWN,
            "presented_as_compliant": False,
            "notes": ["search_brief_not_scored_as_compliant"],
        }

    # Candidates that only restate the government requirement lack independent commercial specs
    dependent = {
        "agency_catalog_reference",
        "botanical_commercial_identity",
        "document_catalog_reference",
        "document_manufacturer_mention",
    }
    independent = origin in {
        "web_research_evidenced",
        "va_attached",
        "resolved_identity",
    } or (
        _known(candidate.get("Product_URL_reference"))
        and str(candidate.get("Product_URL_reference")).startswith("http")
    )

    base = build_compliance_match_profile(profile, candidate)
    rows = [
        {
            "Requirement": cmp_row.get("Requirement"),
            "Required_specification": cmp_row.get("Government"),
            "Commercial_specification": cmp_row.get("Candidate"),
            "Result": cmp_row.get("Result"),
            "Evidence": candidate.get("Evidence_source") or candidate.get("Product_URL_reference"),
        }
        for cmp_row in (base.get("comparisons") or [])
    ]
    overall = base.get("overall")
    conf = base.get("COMPLIANCE_CONFIDENCE")

    if origin in dependent and not independent:
        # Do not treat self-copied government specs as evidenced commercial compliance
        if overall == "COMPLIANT_EVIDENCED":
            overall = "POSSIBLY_COMPLIANT"
            conf = MATCH_LOW
        elif overall not in {"NOT_COMPLIANT_EVIDENCED"}:
            overall = "REQUIRES_INDEPENDENT_SPEC_EVIDENCE"
            conf = MATCH_UNKNOWN
        return {
            "kind": "COMMERCIAL_COMPLIANCE_MATRIX",
            "rows": rows,
            "overall": overall,
            "COMPLIANCE_CONFIDENCE": conf,
            "presented_as_compliant": False,
            "mismatch_count": base.get("mismatch_count"),
            "notes": [
                "candidate_lacks_independent_commercial_specification_evidence",
                "not_presented_as_compliant",
            ],
        }

    return {
        "kind": "COMMERCIAL_COMPLIANCE_MATRIX",
        "rows": rows,
        "overall": overall,
        "COMPLIANCE_CONFIDENCE": conf,
        "presented_as_compliant": overall in {"COMPLIANT_EVIDENCED", "POSSIBLY_COMPLIANT"},
        "mismatch_count": base.get("mismatch_count"),
        "notes": base.get("notes") or [],
    }


def build_supplier_product_relationships(
    row: dict[str, Any], profile: dict[str, Any], candidate: dict[str, Any]
) -> list[dict[str, Any]]:
    rels: list[dict[str, Any]] = []
    for ch in discover_supplier_channels(row, profile, candidate):
        rels.append(
            {
                "kind": "SUPPLIER_PRODUCT_RELATIONSHIP",
                "Product": candidate.get("Commercial_product_name") or candidate.get("Product_name"),
                "Supplier": ch.get("Channel_type") or ch.get("company") or "UNKNOWN",
                "Supplier_type": ch.get("Channel_type") or "channel_path",
                "Availability": "UNKNOWN",
                "Source": ch.get("Evidence") or ch.get("evidence_source") or "channel_hint",
                "Confidence": ch.get("Confidence") or MATCH_LOW,
                "authorized_status": "UNKNOWN",
                "notes": ["authorization_not_assumed"],
            }
        )

    product = {
        "Manufacturer": candidate.get("Manufacturer") or "UNKNOWN",
        "Model": candidate.get("Model") or "UNKNOWN",
        "Part_number": candidate.get("Part_number") or "UNKNOWN",
        "NSN": profile.get("NSN") or "UNKNOWN",
        "Technical_description": candidate.get("Commercial_product_name"),
        "sufficient_for_pricing_research": _known(candidate.get("Manufacturer"))
        and (_known(candidate.get("Part_number")) or _known(candidate.get("Model"))),
    }
    try:
        supply = map_supply_chain(row, product)
        for bucket, stype in (
            ("Manufacturer", OEM),
            ("Authorized_distributors", "AUTHORIZED_DISTRIBUTOR"),
            ("Public_distributors", DISTRIBUTOR),
            ("Dealer_channels", RESELLER),
            ("Government_suppliers", "GOVERNMENT_SUPPLIER"),
        ):
            for item in supply.get(bucket) or []:
                if not isinstance(item, dict):
                    continue
                name = item.get("company") or item.get("name")
                if not _known(name):
                    continue
                rels.append(
                    {
                        "kind": "SUPPLIER_PRODUCT_RELATIONSHIP",
                        "Product": candidate.get("Commercial_product_name"),
                        "Supplier": name,
                        "Supplier_type": stype,
                        "Availability": "UNKNOWN",
                        "Source": item.get("evidence_source")
                        or item.get("website")
                        or "supply_chain_map",
                        "Confidence": item.get("confidence") or MATCH_LOW,
                        "authorized_status": "UNKNOWN",
                        "website": item.get("website") or "UNKNOWN",
                        "notes": ["authorization_not_assumed"],
                    }
                )
    except Exception:
        pass

    if _known(candidate.get("Supplier")):
        rels.append(
            {
                "kind": "SUPPLIER_PRODUCT_RELATIONSHIP",
                "Product": candidate.get("Commercial_product_name"),
                "Supplier": candidate.get("Supplier"),
                "Supplier_type": "listed_supplier",
                "Availability": "UNKNOWN",
                "Source": candidate.get("Evidence_source") or candidate.get("Product_URL_reference"),
                "Confidence": candidate.get("Match_confidence") or MATCH_LOW,
                "authorized_status": "UNKNOWN",
            }
        )
    return rels


def build_commercial_price_profile(
    row: dict[str, Any],
    profile: dict[str, Any],
    candidates: list[dict[str, Any]],
    web_prices: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    work = row
    if web_prices:
        cp = dict(row.get("commercial_pricing") or {})
        best = min(
            (_num(p.get("Price")) for p in web_prices if _num(p.get("Price")) is not None),
            default=None,
        )
        if best is not None and _num(cp.get("public_unit_price")) is None:
            cp["public_unit_price"] = best
            cp["public_source"] = next(
                (p.get("Source") for p in web_prices if _num(p.get("Price")) == best),
                "web_research",
            )
            work = {**row, "commercial_pricing": cp}

    ladder = collect_pricing_ladder(work, profile, candidates)
    levels = dict(ladder.get("levels") or {})
    counts = dict(ladder.get("counts") or {})
    best = ladder.get("best_observed_unit")

    for wp in web_prices or []:
        amt = _num(wp.get("Price"))
        if amt is None:
            continue
        lvl = str(wp.get("level") or "LEVEL_2").upper()
        if "1" in lvl:
            key = "LEVEL_1"
        elif "3" in lvl:
            key = "LEVEL_3"
        elif "4" in lvl:
            key = "LEVEL_4"
        else:
            key = "LEVEL_2"
        entry = {
            "Price": amt,
            "Quantity_tier": "UNKNOWN",
            "Supplier": wp.get("Supplier") or "UNKNOWN",
            "Date_observed": wp.get("Date") or _utc(),
            "Freight": "UNKNOWN",
            "Configuration": wp.get("product_name") or "UNKNOWN",
            "Evidence_URL": wp.get("Source"),
            "Pricing_level": key,
            "Confidence": wp.get("Confidence") or MATCH_MEDIUM,
        }
        levels.setdefault(key, []).append(entry)
        counts[key] = counts.get(key, 0) + 1
        if best in (None, "UNKNOWN") or (_num(best) is not None and amt < float(best)):
            best = amt

    if not any(counts.get(k) for k in ("LEVEL_1", "LEVEL_2", "LEVEL_3", "LEVEL_4")):
        counts["LEVEL_5"] = max(int(counts.get("LEVEL_5") or 0), 1)
        levels.setdefault("LEVEL_5", []).append(
            {
                "Price": "UNKNOWN",
                "Pricing_level": LEVEL_5_NO_PRICE,
                "Confidence": MATCH_UNKNOWN,
                "notes": ["no_usable_public_price"],
            }
        )

    blob = _document_blob(row)
    text_prices = [_num(mm.group(1)) for mm in PRICE_IN_TEXT_RE.finditer(blob)]
    text_prices = [p for p in text_prices if p is not None and p >= 1]

    return {
        "kind": "COMMERCIAL_PRICE_PROFILE",
        "levels": levels,
        "counts": {
            "LEVEL_1": int(counts.get("LEVEL_1") or 0),
            "LEVEL_2": int(counts.get("LEVEL_2") or 0),
            "LEVEL_3": int(counts.get("LEVEL_3") or 0),
            "LEVEL_4": int(counts.get("LEVEL_4") or 0),
            "LEVEL_5": int(counts.get("LEVEL_5") or 0),
        },
        "best_observed_unit": best if best is not None else "UNKNOWN",
        "median_observed_unit": ladder.get("median_observed_unit"),
        "highest_observed_unit": ladder.get("highest_observed_unit"),
        "document_dollar_mentions": text_prices[:10],
        "notes": [
            "retail_public_price_is_not_automatic_acquisition_cost",
            "document_dollars_not_used_as_acquisition_cost_without_product_match",
        ],
    }


def compare_supplier_offers(
    candidates: list[dict[str, Any]],
    relationships: list[dict[str, Any]],
    pricing: dict[str, Any],
) -> dict[str, Any]:
    offers = []
    for c in candidates:
        if c.get("candidate_origin") == "specification_search_brief":
            continue
        rels = [r for r in relationships if r.get("Product") == c.get("Commercial_product_name")]
        offers.append(
            {
                "Supplier": c.get("Supplier") or (rels[0].get("Supplier") if rels else "UNKNOWN"),
                "Product": c.get("Commercial_product_name"),
                "Manufacturer": c.get("Manufacturer"),
                "Price": pricing.get("best_observed_unit")
                if _num(pricing.get("best_observed_unit")) is not None and len(
                    [
                        x
                        for x in candidates
                        if x.get("candidate_origin") != "specification_search_brief"
                    ]
                )
                == 1
                else "UNKNOWN",
                "Lead_time": "UNKNOWN",
                "Availability": "UNKNOWN",
                "Shipping_information": "UNKNOWN",
                "Compliance_confidence": (c.get("COMMERCIAL_COMPLIANCE_MATRIX") or {}).get(
                    "COMPLIANCE_CONFIDENCE"
                )
                or MATCH_UNKNOWN,
                "Evidence_quality": c.get("Match_confidence") or MATCH_UNKNOWN,
                "Confidence": c.get("Match_confidence") or MATCH_UNKNOWN,
                "auto_selected_lowest": False,
            }
        )
    return {
        "kind": "SUPPLIER_OFFER_COMPARISON",
        "offers": offers,
        "selection_rule": "operator_reviews_compliance_availability_reliability_evidence",
        "auto_chose_lowest_price": False,
    }


def build_wholesale_opportunity_profile(
    pricing: dict[str, Any],
    relationships: list[dict[str, Any]],
) -> dict[str, Any]:
    has_public = _num(pricing.get("best_observed_unit")) is not None
    dist = [
        r
        for r in relationships
        if "distribut" in str(r.get("Supplier_type") or "").lower()
        or "industrial" in str(r.get("Supplier_type") or "").lower()
        or "agricultural" in str(r.get("Supplier_type") or "").lower()
        or str(r.get("Supplier_type") or "") in {DISTRIBUTOR, "AUTHORIZED_DISTRIBUTOR", "channel_path"}
    ]
    mfr = [
        r
        for r in relationships
        if str(r.get("Supplier_type") or "").upper() in {OEM, "MANUFACTURER", "OEM"}
    ]
    statuses: list[str] = []
    if has_public and not dist and not mfr:
        statuses.append(PUBLIC_PRICE_ONLY)
    if dist:
        statuses.append(DISTRIBUTOR_PATH_FOUND)
    if mfr:
        statuses.append(MANUFACTURER_PATH_FOUND)
    qty_paths = any("volume" in str(r).lower() or "project" in str(r).lower() for r in relationships)
    if qty_paths:
        statuses.append(VOLUME_PRICING_POSSIBLE)
    statuses.append(WHOLESALE_UNVERIFIED)

    primary = WHOLESALE_UNVERIFIED
    if dist and not has_public:
        primary = DISTRIBUTOR_PATH_FOUND
    elif mfr and not has_public:
        primary = MANUFACTURER_PATH_FOUND
    elif has_public and (dist or mfr):
        primary = WHOLESALE_UNVERIFIED

    return {
        "kind": "WHOLESALE_OPPORTUNITY_PROFILE",
        "status": primary,
        "statuses": list(dict.fromkeys(statuses)),
        "annotation": PUBLIC_ECONOMICS_FAILS_WHOLESALE_VERIFICATION_REQUIRED
        if has_public
        else WHOLESALE_ACCESS_UNVERIFIED,
        "distributor_paths": len(dist),
        "manufacturer_paths": len(mfr),
        "volume_opportunities": 1 if qty_paths else 0,
        "still_unknown": True,
        "notes": ["retail_above_target_is_not_automatic_failure"],
    }


def economics_handoff(
    row: dict[str, Any],
    profile: dict[str, Any],
    pricing: dict[str, Any],
) -> dict[str, Any]:
    revenue = build_government_revenue_profile(row)
    qty = _num(profile.get("Quantity")) or resolve_quantity(row)
    target = calculate_acquisition_target(revenue, row, quantity=qty)
    gap = build_acquisition_price_gap(
        target,
        pricing,
        quantity=qty,
        revenue=_num(revenue.get("primary_revenue")),
        row=row,
    )
    try:
        deal = build_deal_economics_profile(row)
    except Exception:
        deal = {"PROFIT_TARGET_STATUS": STATUS_UNKNOWN}

    return {
        "kind": "ACQUISITION_ECONOMICS_HANDOFF",
        "GOVERNMENT_REVENUE_PROFILE": revenue,
        "ACQUISITION_TARGET": target,
        "ACQUISITION_PRICE_GAP_PROFILE": gap,
        "DEAL_ECONOMICS_REUSED": {
            "PROFIT_TARGET_STATUS": deal.get("PROFIT_TARGET_STATUS"),
            "Target_acquisition_cost": deal.get("Target_acquisition_cost"),
            "Projected_profit": deal.get("Projected_profit"),
        },
        "Target_profit": target_profit_usd(row),
        "Observed_acquisition_cost": pricing.get("best_observed_unit"),
        "needs_government_revenue_research": revenue.get("status")
        not in {"CURRENT_VERIFIED_REVENUE", "CURRENT_ESTIMATED_REVENUE"},
    }


def assign_matching_queue(line: dict[str, Any]) -> str:
    missing = line.get("Missing_information") or []
    wholesale = line.get("WHOLESALE_OPPORTUNITY_PROFILE") or {}
    gap = line.get("ACQUISITION_PRICE_GAP_PROFILE") or {}
    if line.get("no_named_commercial_match"):
        return Q_NEEDS_PRODUCT_MATCH
    if "spec_validation" in missing:
        return Q_NEEDS_SPEC_VALIDATION
    if "supplier_relationship" in missing:
        return Q_NEEDS_SUPPLIER_RESEARCH
    if "public_acquisition_pricing" in missing:
        return Q_NEEDS_PRICING
    if wholesale.get("still_unknown") and _num(
        (line.get("COMMERCIAL_PRICE_PROFILE") or {}).get("best_observed_unit")
    ):
        return Q_NEEDS_WHOLESALE_VERIFICATION
    if line.get("needs_government_revenue_research"):
        return Q_NEEDS_GOVERNMENT_REVENUE_RESEARCH
    if gap.get("calculable"):
        return Q_READY_FOR_ECONOMICS
    return Q_NEEDS_PRODUCT_MATCH


def _is_named_candidate(c: dict[str, Any]) -> bool:
    origin = c.get("candidate_origin")
    if origin in {"specification_search_brief", "agency_catalog_reference", "document_catalog_reference"}:
        return False
    if origin == "document_manufacturer_mention":
        return bool(
            _known(c.get("Product_URL_reference"))
            and str(c.get("Product_URL_reference")).startswith("http")
        )
    if origin == "botanical_commercial_identity":
        return True
    return bool(
        _known(c.get("Manufacturer"))
        or (
            _known(c.get("Product_URL_reference"))
            and str(c.get("Product_URL_reference")).startswith("http")
        )
        or origin in {"web_research_evidenced", "va_attached", "resolved_identity"}
    )


def analyze_line_commercial_match(
    row: dict[str, Any],
    profile: dict[str, Any],
    *,
    allow_paid_web: bool = False,
) -> dict[str, Any]:
    candidates, strategies, learning, web = build_commercial_candidates(
        row, profile, allow_paid_web=allow_paid_web
    )

    enriched: list[dict[str, Any]] = []
    high = possible = named = 0
    all_rels: list[dict[str, Any]] = []
    for c in candidates:
        matrix = build_commercial_compliance_matrix(profile, c)
        c = {
            **c,
            "COMMERCIAL_COMPLIANCE_MATRIX": matrix,
            "presented_as_compliant": matrix.get("presented_as_compliant"),
        }
        rels = build_supplier_product_relationships(row, profile, c)
        c["SUPPLIER_PRODUCT_RELATIONSHIPS"] = rels
        all_rels.extend(rels)
        if _is_named_candidate(c):
            named += 1
        if (
            matrix.get("overall") == "COMPLIANT_EVIDENCED"
            and matrix.get("COMPLIANCE_CONFIDENCE") == MATCH_HIGH
            and matrix.get("presented_as_compliant")
            and _is_named_candidate(c)
        ):
            high += 1
        if matrix.get("overall") == "POSSIBLY_COMPLIANT" and _is_named_candidate(c):
            possible += 1
        enriched.append(c)

    pricing = build_commercial_price_profile(
        row, profile, enriched, web_prices=web.get("prices") or []
    )

    if allow_paid_web and _num(pricing.get("best_observed_unit")) is None:
        for c in enriched:
            if c.get("candidate_origin") == "specification_search_brief":
                continue
            product = {
                "Manufacturer": c.get("Manufacturer") or "UNKNOWN",
                "Model": c.get("Model") or "UNKNOWN",
                "Part_number": c.get("Part_number") or "UNKNOWN",
                "NSN": profile.get("NSN") or "UNKNOWN",
                "Technical_description": c.get("Commercial_product_name"),
                "sufficient_for_pricing_research": bool(
                    _known(c.get("Manufacturer"))
                    and (
                        _known(c.get("Part_number"))
                        or _known(c.get("Model"))
                        or _known(c.get("Product_URL_reference"))
                    )
                )
                or c.get("candidate_origin") == "botanical_commercial_identity",
            }
            if not product["sufficient_for_pricing_research"]:
                continue
            try:
                web_price = research_public_pricing_web(row, product, allow_paid=True)
                existing = collect_existing_price_evidence(row, product)
                for e in list(web_price.get("evidence") or []) + existing:
                    amt = _num(e.get("amount"))
                    if amt is None:
                        continue
                    pricing = build_commercial_price_profile(
                        row,
                        profile,
                        enriched,
                        web_prices=[
                            {
                                "Price": amt,
                                "Source": e.get("Source"),
                                "level": e.get("level") or "LEVEL_2",
                                "Date": e.get("Date"),
                                "product_name": c.get("Commercial_product_name"),
                                "Confidence": e.get("Product_match_confidence") or MATCH_MEDIUM,
                            }
                        ],
                    )
                    break
            except Exception:
                pass
            break

    comparison = compare_supplier_offers(enriched, all_rels, pricing)
    wholesale = build_wholesale_opportunity_profile(pricing, all_rels)
    econ = economics_handoff(row, profile, pricing)
    econ["needs_government_revenue_research"] = bool(
        named > 0 and econ.get("needs_government_revenue_research")
    )

    missing: list[str] = []
    if named == 0:
        missing.append("named_commercial_product")
    if not all_rels:
        missing.append("supplier_relationship")
    if _num(pricing.get("best_observed_unit")) is None:
        missing.append("public_acquisition_pricing")
    if econ.get("needs_government_revenue_research"):
        missing.append("government_revenue_evidence")
    if wholesale.get("still_unknown"):
        missing.append("wholesale_verification")
    if named and high == 0:
        missing.append("spec_validation")

    if named == 0:
        next_action = "Research commercial products matching specification"
    elif _num(pricing.get("best_observed_unit")) is None:
        next_action = "Collect public supplier pricing evidence"
    elif econ.get("needs_government_revenue_research"):
        next_action = "Research government revenue / award value evidence"
    elif wholesale.get("still_unknown"):
        next_action = "Verify wholesale/distributor/project pricing paths"
    elif (econ.get("ACQUISITION_PRICE_GAP_PROFILE") or {}).get("calculable"):
        next_action = "Review acquisition economics in deal room"
    else:
        next_action = "Continue commercial market research"

    result = {
        "kind": "M3LineCommercialMatch",
        "Opportunity": row.get("title"),
        "Opportunity_ID": row.get("canonical_id"),
        "Government_requirement": profile.get("Original_description")
        or profile.get("Resolved_product_name"),
        "Identity_type": profile.get("Identity_type"),
        "Quantity": profile.get("Quantity")
        if _known(profile.get("Quantity"))
        else resolve_quantity(row),
        "COMMERCIAL_PRODUCT_CANDIDATES": enriched,
        "candidates_found": named,
        "search_briefs": sum(
            1 for c in enriched if c.get("candidate_origin") == "specification_search_brief"
        ),
        "high_confidence_matches": high,
        "possible_matches": possible,
        "no_named_commercial_match": named == 0,
        "SEARCH_STRATEGIES": strategies,
        "SEARCH_LEARNING_PROFILE": learning,
        "SUPPLIER_PRODUCT_RELATIONSHIPS": all_rels,
        "COMMERCIAL_PRICE_PROFILE": pricing,
        "SUPPLIER_OFFER_COMPARISON": comparison,
        "WHOLESALE_OPPORTUNITY_PROFILE": wholesale,
        "ACQUISITION_TARGET": econ.get("ACQUISITION_TARGET"),
        "ACQUISITION_PRICE_GAP_PROFILE": econ.get("ACQUISITION_PRICE_GAP_PROFILE"),
        "GOVERNMENT_REVENUE_PROFILE": econ.get("GOVERNMENT_REVENUE_PROFILE"),
        "DEAL_ECONOMICS_REUSED": econ.get("DEAL_ECONOMICS_REUSED"),
        "needs_government_revenue_research": econ.get("needs_government_revenue_research"),
        "Missing_information": missing,
        "Next_Action": next_action,
        "web_research": {
            "executed": web.get("executed"),
            "reason": web.get("reason"),
            "paid": web.get("paid") or 0,
            "reused": web.get("reused"),
        },
        "DEVELOPMENT_NO_OUTREACH": True,
    }
    result["queue"] = assign_matching_queue(result)
    return result


def analyze_opportunity_commercial_match(
    row: dict[str, Any],
    *,
    allow_paid_web: bool = False,
    max_lines: int = 8,
) -> dict[str, Any]:
    identity = _identity_bundle(row)
    profiles = _eligible_profiles(identity, row)[:max_lines]
    lines = [
        analyze_line_commercial_match(row, p, allow_paid_web=allow_paid_web) for p in profiles
    ]
    queues: dict[str, list] = defaultdict(list)
    for li in lines:
        queues[li.get("queue") or Q_NEEDS_PRODUCT_MATCH].append(
            {
                "Opportunity": row.get("title"),
                "canonical_id": row.get("canonical_id"),
                "Requirement": li.get("Government_requirement"),
                "Next_Action": li.get("Next_Action"),
            }
        )

    return {
        "kind": "M3CommercialProductMatching",
        "Opportunity_ID": row.get("canonical_id"),
        "Opportunity": row.get("title"),
        "LINE_MATCHES": lines,
        "lines_analyzed": len(lines),
        "queues": dict(queues),
        "VA": {
            "allowed_actions": sorted(VA_ALLOWED),
            "forbidden_actions": sorted(VA_FORBIDDEN),
        },
        "Timestamp": _utc(),
        "DEVELOPMENT_NO_OUTREACH": True,
        "working_row_patch": {
            "commercial_product_matching": {
                "kind": "M3CommercialMatchSummary",
                "lines_analyzed": len(lines),
                "named_candidates": sum(int(li.get("candidates_found") or 0) for li in lines),
                "primary_Next_Action": lines[0].get("Next_Action") if lines else "No lines",
                "primary_queue": lines[0].get("queue") if lines else Q_NEEDS_PRODUCT_MATCH,
            }
        },
    }


def _priority_rows(store: Any, *, limit: int) -> list[dict[str, Any]]:
    rows = [r for r in (store.all() if hasattr(store, "all") else []) if isinstance(r, dict)]

    def score(r: dict[str, Any]) -> tuple:
        title = str(r.get("title") or "")
        boost = 0
        for i, t in enumerate(PRIORITY_TITLES):
            if t.lower() in title.lower():
                boost = 200 - i * 10
                break
        ident = r.get("product_identity_resolution") or {}
        ready = int((ident.get("SUPPLIER_READINESS") or {}).get("Ready_with_specifications") or 0)
        return (-(boost + ready * 10), str(r.get("canonical_id") or ""))

    return sorted(rows, key=score)[: max(1, min(limit, 40))]


def analyze_commercial_matching_top(
    store: Any,
    *,
    limit: int = 20,
    allow_paid_web: bool = False,
    paid_limit: int = 0,
    persist: bool = True,
) -> dict[str, Any]:
    ranked = _priority_rows(store, limit=limit)
    index = load_match_index() if persist else {"by_id": {}}
    by_id = index.setdefault("by_id", {})

    items = []
    paid_used = 0
    products_n = cand_n = high_n = poss_n = none_n = 0
    mfrs: set[str] = set()
    dists = 0
    channels = 0
    level_counts = {"LEVEL_1": 0, "LEVEL_2": 0, "LEVEL_3": 0, "LEVEL_4": 0, "LEVEL_5": 0}
    wholesale = {
        "distributor_paths": 0,
        "manufacturer_paths": 0,
        "volume_opportunities": 0,
        "still_unknown": 0,
    }
    econ = {"with_acquisition_prices": 0, "target_prices": 0, "profit_calcs": 0}
    top_opps: list[dict[str, Any]] = []
    real_results: dict[str, Any] = {}
    queues: dict[str, list] = defaultdict(list)

    for row in ranked:
        title = str(row.get("title") or "")
        is_priority = any(t.lower() in title.lower() for t in PRIORITY_TITLES)
        use_paid = allow_paid_web and paid_used < paid_limit
        if allow_paid_web and is_priority and paid_used < max(paid_limit, 1):
            use_paid = paid_used < max(paid_limit, 4)

        result = analyze_opportunity_commercial_match(row, allow_paid_web=use_paid)
        if use_paid:
            paid_used += 1
            for li in result.get("LINE_MATCHES") or []:
                wr = li.get("web_research") or {}
                if wr.get("executed"):
                    row["commercial_match_research"] = {
                        "query_fingerprint": f"commercial_match_v1|{row.get('canonical_id')}|{(li.get('Government_requirement') or '')[:40]}",
                        "candidates": [
                            c
                            for c in (li.get("COMMERCIAL_PRODUCT_CANDIDATES") or [])
                            if c.get("candidate_origin") == "web_research_evidenced"
                        ],
                        "at": _utc(),
                    }
                    break
        items.append(result)

        for li in result.get("LINE_MATCHES") or []:
            products_n += 1
            cand_n += int(li.get("candidates_found") or 0)
            high_n += int(li.get("high_confidence_matches") or 0)
            poss_n += int(li.get("possible_matches") or 0)
            if li.get("no_named_commercial_match"):
                none_n += 1
            for c in li.get("COMMERCIAL_PRODUCT_CANDIDATES") or []:
                if _known(c.get("Manufacturer")):
                    mfrs.add(str(c.get("Manufacturer")))
            for r in li.get("SUPPLIER_PRODUCT_RELATIONSHIPS") or []:
                channels += 1
                st = str(r.get("Supplier_type") or "")
                if (
                    "DISTRIBUT" in st.upper()
                    or "industrial" in st.lower()
                    or "agricultural" in st.lower()
                ):
                    dists += 1
            pricing = li.get("COMMERCIAL_PRICE_PROFILE") or {}
            for k in level_counts:
                level_counts[k] += int((pricing.get("counts") or {}).get(k) or 0)
            wh = li.get("WHOLESALE_OPPORTUNITY_PROFILE") or {}
            wholesale["distributor_paths"] += int(wh.get("distributor_paths") or 0)
            wholesale["manufacturer_paths"] += int(wh.get("manufacturer_paths") or 0)
            wholesale["volume_opportunities"] += int(wh.get("volume_opportunities") or 0)
            if wh.get("still_unknown"):
                wholesale["still_unknown"] += 1
            if _num(pricing.get("best_observed_unit")) is not None:
                econ["with_acquisition_prices"] += 1
            if (li.get("ACQUISITION_TARGET") or {}).get("calculable"):
                econ["target_prices"] += 1
            if (li.get("ACQUISITION_PRICE_GAP_PROFILE") or {}).get("calculable"):
                econ["profit_calcs"] += 1
            q = li.get("queue") or Q_NEEDS_PRODUCT_MATCH
            queues[q].append(
                {
                    "Opportunity": row.get("title"),
                    "canonical_id": row.get("canonical_id"),
                    "Requirement": li.get("Government_requirement"),
                    "Next_Action": li.get("Next_Action"),
                }
            )

        primary = (result.get("LINE_MATCHES") or [{}])[0]
        top_opps.append(
            {
                "Opportunity": row.get("title"),
                "Government_requirement": primary.get("Government_requirement"),
                "Commercial_product": next(
                    (
                        c.get("Commercial_product_name")
                        for c in (primary.get("COMMERCIAL_PRODUCT_CANDIDATES") or [])
                        if _is_named_candidate(c)
                    ),
                    "UNKNOWN",
                ),
                "Supplier": next(
                    (
                        r.get("Supplier")
                        for r in (primary.get("SUPPLIER_PRODUCT_RELATIONSHIPS") or [])
                        if _known(r.get("Supplier"))
                    ),
                    "UNKNOWN",
                ),
                "Price_evidence": (primary.get("COMMERCIAL_PRICE_PROFILE") or {}).get(
                    "best_observed_unit"
                ),
                "Compliance_confidence": next(
                    (
                        (c.get("COMMERCIAL_COMPLIANCE_MATRIX") or {}).get("COMPLIANCE_CONFIDENCE")
                        for c in (primary.get("COMMERCIAL_PRODUCT_CANDIDATES") or [])
                        if _is_named_candidate(c)
                    ),
                    MATCH_UNKNOWN,
                ),
                "Acquisition_status": (primary.get("ACQUISITION_PRICE_GAP_PROFILE") or {}).get(
                    "Profit_target_status"
                )
                or STATUS_UNKNOWN,
                "Next_action": primary.get("Next_Action"),
            }
        )

        for key in PRIORITY_TITLES:
            if key.lower() in title.lower() and key not in real_results:
                real_results[key] = top_opps[-1]

        cid = row.get("canonical_id")
        if cid and hasattr(store, "_rows"):
            existing = store._rows.get(cid) or dict(row)
            patch = result.get("working_row_patch") or {}
            existing.update({k: v for k, v in patch.items() if v is not None})
            existing["commercial_product_matching_full"] = result
            if row.get("commercial_match_research"):
                existing["commercial_match_research"] = row["commercial_match_research"]
            store._rows[cid] = existing
        if cid:
            by_id[str(cid)] = {
                "summary": result.get("working_row_patch", {}).get("commercial_product_matching"),
                "updated_at": _utc(),
            }

    if persist:
        try:
            if hasattr(store, "save"):
                store.save()
        except Exception:
            pass
        save_match_index(index)

    return {
        "kind": "M3CommercialProductMatchingRun",
        "analyzed": len(items),
        "PRODUCT_MATCHING": {
            "Products_analyzed": products_n,
            "Candidates_found": cand_n,
            "High_confidence_matches": high_n,
            "Possible_matches": poss_n,
            "No_matches": none_n,
        },
        "SUPPLIERS": {
            "Manufacturers_found": len(mfrs),
            "Distributors_found": dists,
            "Supplier_channels": channels,
            "manufacturer_list": sorted(mfrs)[:20],
        },
        "PRICING": level_counts,
        "WHOLESALE": wholesale,
        "ECONOMICS": econ,
        "TOP_OPPORTUNITIES": top_opps[:12],
        "REAL_OPPORTUNITY_RESULTS": real_results,
        "queues": {k: v[:20] for k, v in queues.items()},
        "COST": {"Paid_spend_actions": paid_used, "Paid_spend": 0},
        "SAFETY": {"Outreach_actions": 0},
        "items": items,
        "NEXT_STATE": "COMMERCIAL_PRODUCT_MATCHING_OPERATIONAL",
        "DEVELOPMENT_NO_OUTREACH": True,
    }


def deal_room_commercial_market_section(row: dict[str, Any]) -> dict[str, Any]:
    full = row.get("commercial_product_matching_full")
    if not isinstance(full, dict) or full.get("kind") != "M3CommercialProductMatching":
        try:
            full = analyze_opportunity_commercial_match(row, allow_paid_web=False)
        except Exception:
            full = {}
    primary = (full.get("LINE_MATCHES") or [{}])[0]
    pricing = primary.get("COMMERCIAL_PRICE_PROFILE") or {}
    return {
        "kind": "M3DealRoomCommercialMarketResearch",
        "Government_requirement": primary.get("Government_requirement") or row.get("title"),
        "Commercial_candidates": (primary.get("COMMERCIAL_PRODUCT_CANDIDATES") or [])[:8],
        "Compliance_comparison": [
            c.get("COMMERCIAL_COMPLIANCE_MATRIX")
            for c in (primary.get("COMMERCIAL_PRODUCT_CANDIDATES") or [])[:5]
        ],
        "Suppliers": primary.get("SUPPLIER_PRODUCT_RELATIONSHIPS") or [],
        "Observed_prices": {
            "best": pricing.get("best_observed_unit"),
            "median": pricing.get("median_observed_unit"),
            "highest": pricing.get("highest_observed_unit"),
            "counts": pricing.get("counts"),
        },
        "Pricing_confidence": (
            MATCH_MEDIUM if _num(pricing.get("best_observed_unit")) is not None else MATCH_UNKNOWN
        ),
        "Wholesale_paths": primary.get("WHOLESALE_OPPORTUNITY_PROFILE"),
        "Acquisition_status": (primary.get("ACQUISITION_PRICE_GAP_PROFILE") or {}).get(
            "Profit_target_status"
        )
        or STATUS_UNKNOWN,
        "Missing_information": primary.get("Missing_information") or [],
        "Next_Action": primary.get("Next_Action") or "Run commercial matching analysis",
        "VA_allowed_actions": sorted(VA_ALLOWED),
        "VA_forbidden_actions": sorted(VA_FORBIDDEN),
        "DEVELOPMENT_NO_OUTREACH": True,
    }


def build_va_matching_queues(limit: int = 25) -> dict[str, Any]:
    idx = load_match_index()
    return {
        "kind": "M3CommercialMatchingQueues",
        "queues": {
            Q_NEEDS_PRODUCT_MATCH: [],
            Q_NEEDS_SPEC_VALIDATION: [],
            Q_NEEDS_SUPPLIER_RESEARCH: [],
            Q_NEEDS_PRICING: [],
            Q_NEEDS_WHOLESALE_VERIFICATION: [],
            Q_NEEDS_GOVERNMENT_REVENUE_RESEARCH: [],
            Q_READY_FOR_ECONOMICS: [],
        },
        "researched": len(idx.get("by_id") or {}),
        "VA_allowed_actions": sorted(VA_ALLOWED),
        "VA_forbidden_actions": sorted(VA_FORBIDDEN),
        "note": "Run /api/m3/commercial-matching/analyze to populate queue cards",
        "limit": limit,
        "DEVELOPMENT_NO_OUTREACH": True,
    }


def apply_va_matching_update(
    store: Any,
    canonical_id: str,
    *,
    action: str,
    note: str | None = None,
    evidence: dict[str, Any] | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    action_u = str(action or "").upper().strip()
    if action_u not in VA_ALLOWED:
        return {
            "ok": False,
            "error": "action_not_allowed",
            "action": action_u,
            "VA_allowed_actions": sorted(VA_ALLOWED),
            "VA_forbidden_actions": sorted(VA_FORBIDDEN),
        }
    row = store._rows.get(canonical_id) if hasattr(store, "_rows") else None
    if not isinstance(row, dict) and hasattr(store, "get"):
        row = store.get(canonical_id)
    if not isinstance(row, dict):
        return {"ok": False, "error": "opportunity_not_found"}

    notes = list(row.get("commercial_match_va_notes") or [])
    if action_u in {"ADD_NOTES", "NOTE"} and note:
        notes.append({"at": _utc(), "note": str(note)[:2000], "action": action_u})
    if action_u == "COLLECT_PRICING_EVIDENCE" and isinstance(evidence, dict):
        cp = dict(row.get("commercial_pricing") or {})
        amt = _num(evidence.get("unit_price") or evidence.get("Price"))
        if amt is not None:
            cp["public_unit_price"] = amt
            if evidence.get("url") or evidence.get("source"):
                cp["public_source"] = evidence.get("url") or evidence.get("source")
            row["commercial_pricing"] = cp
        notes.append({"at": _utc(), "note": note or "pricing_attached", "action": action_u})
    if action_u in {"RESEARCH_PRODUCTS", "ATTACH_SOURCES"} and isinstance(evidence, dict):
        cands = list(row.get("va_commercial_candidates") or [])
        if _known(
            evidence.get("Commercial_product_name")
            or evidence.get("name")
            or evidence.get("Manufacturer")
        ):
            cands.append(
                {
                    "Commercial_product_name": evidence.get("Commercial_product_name")
                    or evidence.get("name"),
                    "Manufacturer": evidence.get("Manufacturer"),
                    "Model": evidence.get("Model"),
                    "Part_number": evidence.get("Part_number"),
                    "SKU": evidence.get("SKU"),
                    "Supplier": evidence.get("Supplier"),
                    "Product_URL_reference": evidence.get("url")
                    or evidence.get("Product_URL_reference"),
                    "Specifications": evidence.get("Specifications") or {},
                    "Match_confidence": evidence.get("Match_confidence") or MATCH_MEDIUM,
                    "Evidence_source": evidence.get("url") or "va_attached",
                }
            )
            row["va_commercial_candidates"] = cands[-20:]
        notes.append({"at": _utc(), "note": note or action_u, "action": action_u})
    if action_u == "COMPARE_SPECIFICATIONS":
        notes.append({"at": _utc(), "note": note or "spec_comparison_noted", "action": action_u})
    if action_u == "UPDATE_STATUS" and status:
        row["commercial_match_va_status"] = str(status).upper()
        notes.append({"at": _utc(), "note": f"status:{status}", "action": action_u})

    row["commercial_match_va_notes"] = notes[-30:]
    if hasattr(store, "_rows"):
        store._rows[canonical_id] = row
    if hasattr(store, "save"):
        try:
            store.save()
        except Exception:
            pass
    return {"ok": True, "canonical_id": canonical_id, "action": action_u, "VA_notes": notes[-10:]}
