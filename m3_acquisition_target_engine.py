"""Specification-to-Supplier + Acquisition Target Engine — research only.

Advance READY_WITH_SPECIFICATIONS products toward executable economics for a
new small government reseller. Never invent prices, manufacturers, or
compliance. UNKNOWN ≠ rejected; public-price failure ≠ wholesale blocked.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from copy import deepcopy
from typing import Any

from application_clock import now_utc
from economic_integrity import min_actual_profit_usd
from m3_commercial_engine import (
    PRICE_LEVEL_1_ACTUAL,
    PRICE_LEVEL_2_PUBLIC,
    PRICE_LEVEL_3_COMPARABLE,
    PRICE_LEVEL_4_UNKNOWN,
)
from m3_deal_economics import (
    STATUS_BELOW,
    STATUS_EXCEEDS,
    STATUS_MEETS,
    STATUS_UNVIABLE,
    STATUS_UNKNOWN,
    STATUS_WITHIN,
    acceptable_profit_floor,
    build_deal_economics_profile,
    classify_profit_target_status,
    resolve_pricing_evidence,
    resolve_quantity,
    resolve_revenue,
    target_profit_usd,
)
from m3_product_identity_resolution import (
    CATEGORY_ONLY,
    EXACT_IDENTITY,
    EXACT_ONLY,
    EQUIVALENTS_ALLOWED,
    MATCH_HIGH,
    MATCH_LOW,
    MATCH_MEDIUM,
    MATCH_UNKNOWN,
    READY_FOR_SUPPLIER_SEARCH,
    READY_WITH_SPECIFICATIONS,
    SPECIFICATION_BASED,
    SPECIFICATION_IDENTITY,
    UNKNOWN,
    reject_false_identifier,
    resolve_opportunity_product_identities,
)
from m3_supplier_intelligence import (
    collect_existing_price_evidence,
    map_supply_chain,
    research_public_pricing_web,
)

log = logging.getLogger("govtracker.m3_acquisition_target")

ACQ_INDEX_KEY = "m3_acquisition_target_v1"
TARGET_PROFIT_DEFAULT = 10000.0

# Acquisition channel status
PUBLIC_PRICE_AVAILABLE = "PUBLIC_PRICE_AVAILABLE"
WHOLESALE_ACCESS_UNVERIFIED = "WHOLESALE_ACCESS_UNVERIFIED"
RESELLER_PROGRAM_IDENTIFIED = "RESELLER_PROGRAM_IDENTIFIED"
PROJECT_PRICING_IDENTIFIED = "PROJECT_PRICING_IDENTIFIED"
VOLUME_PRICING_IDENTIFIED = "VOLUME_PRICING_IDENTIFIED"
MANUFACTURER_DIRECT_PATH_IDENTIFIED = "MANUFACTURER_DIRECT_PATH_IDENTIFIED"
VERIFIED_ACQUISITION_PRICE = "VERIFIED_ACQUISITION_PRICE"
PRICE_ACCESS_BLOCKED = "PRICE_ACCESS_BLOCKED"
PUBLIC_ECONOMICS_FAILS_WHOLESALE_VERIFICATION_REQUIRED = (
    "PUBLIC_ECONOMICS_FAILS_WHOLESALE_VERIFICATION_REQUIRED"
)

# Revenue statuses
CURRENT_VERIFIED_REVENUE = "CURRENT_VERIFIED_REVENUE"
CURRENT_ESTIMATED_REVENUE = "CURRENT_ESTIMATED_REVENUE"
HISTORICAL_EXACT_BENCHMARK = "HISTORICAL_EXACT_BENCHMARK"
HISTORICAL_COMPARABLE_BENCHMARK = "HISTORICAL_COMPARABLE_BENCHMARK"
INSUFFICIENT_REVENUE_EVIDENCE = "INSUFFICIENT_REVENUE_EVIDENCE"

# Pricing levels (mission ladder maps onto existing L1–L4 + L5)
LEVEL_5_NO_PRICE = "LEVEL_5_NO_USABLE_PRICE"

# First transaction
NORMAL_PROFIT_TARGET = "NORMAL_PROFIT_TARGET"
FIRST_TRANSACTION_CANDIDATE = "FIRST_TRANSACTION_CANDIDATE"
TRACK_RECORD_BUILDER = "TRACK_RECORD_BUILDER"
NOT_ATTRACTIVE = "NOT_ATTRACTIVE"

# VA queues
Q_NEEDS_COMMERCIAL_PRODUCT_MATCH = "NEEDS_COMMERCIAL_PRODUCT_MATCH"
Q_NEEDS_PUBLIC_PRICING = "NEEDS_PUBLIC_PRICING"
Q_NEEDS_WHOLESALE_VERIFICATION = "NEEDS_WHOLESALE_VERIFICATION"
Q_NEEDS_REVENUE_EVIDENCE = "NEEDS_REVENUE_EVIDENCE"
Q_NEEDS_FINANCING_VERIFICATION = "NEEDS_FINANCING_VERIFICATION"
Q_ECONOMICS_READY = "ECONOMICS_READY"
Q_OWNER_REVIEW = "OWNER_REVIEW"

VA_ALLOWED = frozenset(
    {
        "RESEARCH_PUBLIC_SUPPLIERS",
        "ATTACH_PRICING_EVIDENCE",
        "RESEARCH_PRODUCT_SPECS",
        "ATTACH_GOVERNMENT_PRICING_HISTORY",
        "ADD_NOTES",
        "NOTE",
        "CORRECT_EVIDENCE_BACKED_DATA",
        "UPDATE_STATUS",
    }
)
VA_FORBIDDEN = frozenset(
    {
        "CONTACT_SUPPLIER",
        "APPLY_FOR_ACCOUNT",
        "APPLY_FOR_FINANCING",
        "SUBMIT_BID",
        "APPROVE_SUBSTITUTION",
        "APPROVE_DEAL",
        "ALTER_SCORING",
        "CHANGE_SCORING",
        "SPEND_MONEY",
    }
)

REAL_VALIDATION_TITLES = (
    "Tungsten-Carbide Blades",
    "Wildflower",
    "Native Grass Seed",
    "Law Enforcement Badges",
    "Wheelchair Lift",
)

CHANNEL_HINTS: list[tuple[str, re.Pattern[str]]] = [
    ("industrial_mro", re.compile(r"\b(blade|carbide|industrial|mro|bearing|fastener)\b", re.I)),
    ("agricultural_grounds", re.compile(r"\b(seed|native\s+grass|wildflower|fertilizer|turf)\b", re.I)),
    ("safety_uniform", re.compile(r"\b(badge|insignia|uniform|law\s+enforcement)\b", re.I)),
    ("medical_equipment", re.compile(r"\b(wheelchair|medical|patient\s+lift|hospital)\b", re.I)),
    ("it_distribution", re.compile(r"\b(server|switch|laptop|network|cisco|dell)\b", re.I)),
    ("fleet_automotive", re.compile(r"\b(snow/?ice|plow|vehicle|fleet|tire)\b", re.I)),
    ("specialty_equipment", re.compile(r"\b(lift|hoist|crane|specialty)\b", re.I)),
]


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
        return float(str(v).replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return None


def load_acq_index() -> dict[str, Any]:
    try:
        from models import AppSetting, session_scope

        with session_scope() as session:
            row = session.get(AppSetting, ACQ_INDEX_KEY)
            if row and isinstance(row.value, dict):
                return deepcopy(row.value)
    except Exception:
        log.debug("acquisition target index load failed", exc_info=True)
    return {"kind": "M3AcquisitionTargetIndex", "by_id": {}, "updated_at": None}


def save_acq_index(index: dict[str, Any]) -> None:
    index = deepcopy(index)
    index["kind"] = "M3AcquisitionTargetIndex"
    index["updated_at"] = _utc()
    try:
        from models import AppSetting, session_scope

        with session_scope() as session:
            row = session.get(AppSetting, ACQ_INDEX_KEY)
            if row is None:
                session.add(AppSetting(key=ACQ_INDEX_KEY, value=index))
            else:
                row.value = index
    except Exception:
        log.debug("acquisition target index save failed", exc_info=True)


def _identity_bundle(row: dict[str, Any]) -> dict[str, Any]:
    full = row.get("product_identity_resolution_full")
    if isinstance(full, dict) and full.get("kind") == "M3ProductIdentityResolution":
        return full
    return resolve_opportunity_product_identities(row, update_learning=False)


def _eligible_profiles(identity_bundle: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for p in identity_bundle.get("PRODUCT_IDENTITY_RESOLUTION_PROFILES") or []:
        if not isinstance(p, dict):
            continue
        readiness = (p.get("SUPPLIER_RESEARCH_READINESS_SCORE") or {}).get("status")
        itype = p.get("Identity_type")
        if readiness in {READY_WITH_SPECIFICATIONS, READY_FOR_SUPPLIER_SEARCH}:
            out.append(p)
        elif itype in {SPECIFICATION_IDENTITY, EXACT_IDENTITY}:
            out.append(p)
    return out


# ---------------------------------------------------------------------------
# Phase 1 — commercial product candidates (evidence only; no invention)
# ---------------------------------------------------------------------------

def build_commercial_product_candidates(
    row: dict[str, Any],
    profile: dict[str, Any],
    *,
    allow_paid_web: bool = False,
) -> list[dict[str, Any]]:
    """COMMERCIAL_PRODUCT_CANDIDATE list from evidence + optional gated web research."""
    candidates: list[dict[str, Any]] = []
    desc = str(profile.get("Original_description") or profile.get("Resolved_product_name") or "")
    spec = profile.get("SPECIFICATION_PROFILE") or {}
    mfr = profile.get("Manufacturer") if _known(profile.get("Manufacturer")) else None
    pn = profile.get("Manufacturer_part_number") if _known(profile.get("Manufacturer_part_number")) else None
    model = profile.get("Model_number") if _known(profile.get("Model_number")) else None

    if mfr or pn or model:
        candidates.append(
            {
                "kind": "COMMERCIAL_PRODUCT_CANDIDATE",
                "Opportunity_ID": row.get("canonical_id"),
                "Line_item_ID": profile.get("Line_item"),
                "Manufacturer": mfr or "UNKNOWN",
                "Product_name": profile.get("Resolved_product_name") or desc or "UNKNOWN",
                "Model": model or "UNKNOWN",
                "Part_number": pn or "UNKNOWN",
                "SKU": profile.get("SKU") or "UNKNOWN",
                "Supplier": "UNKNOWN",
                "Product_URL_reference": "UNKNOWN",
                "Specifications": profile.get("Specifications") or {},
                "Match_confidence": profile.get("Confidence") or MATCH_MEDIUM,
                "Evidence_source": profile.get("Evidence_source") or "identity_resolution",
                "Pricing_evidence": "UNKNOWN",
                "candidate_origin": "resolved_identity",
            }
        )

    # Evidence-backed brand mentions in attachment text near the description
    blob = " ".join(
        [
            str(row.get("attachment_text") or "")[:4000],
            str(row.get("governing_text") or "")[:2000],
            str(row.get("title") or ""),
        ]
    )
    from m3_commercial_engine import KNOWN_MANUFACTURERS, AMBIGUOUS_MFR_TOKENS

    for known in KNOWN_MANUFACTURERS:
        if known in AMBIGUOUS_MFR_TOKENS:
            continue
        if re.search(rf"\b{re.escape(known)}\b", blob, re.I) and re.search(
            rf"\b{re.escape(known)}\b.{{0,80}}\b(model|part|p/?n|catalog)\b",
            blob,
            re.I,
        ):
            if any(c.get("Manufacturer") == known for c in candidates):
                continue
            candidates.append(
                {
                    "kind": "COMMERCIAL_PRODUCT_CANDIDATE",
                    "Opportunity_ID": row.get("canonical_id"),
                    "Line_item_ID": profile.get("Line_item"),
                    "Manufacturer": known,
                    "Product_name": desc or "UNKNOWN",
                    "Model": "UNKNOWN",
                    "Part_number": "UNKNOWN",
                    "SKU": "UNKNOWN",
                    "Supplier": "UNKNOWN",
                    "Product_URL_reference": "UNKNOWN",
                    "Specifications": spec,
                    "Match_confidence": MATCH_LOW,
                    "Evidence_source": "solicitation_manufacturer_mention",
                    "Pricing_evidence": "UNKNOWN",
                    "candidate_origin": "evidence_brand_mention",
                    "notes": ["brand_mentioned_not_confirmed_as_required_product"],
                }
            )

    # Spec search brief — not a fake product; always provide research starting point
    if not candidates and (
        spec.get("well_defined")
        or profile.get("Identity_type") in {SPECIFICATION_IDENTITY, CATEGORY_ONLY, EXACT_IDENTITY}
        or _known(desc)
    ):
        candidates.append(
            {
                "kind": "COMMERCIAL_PRODUCT_CANDIDATE",
                "Opportunity_ID": row.get("canonical_id"),
                "Line_item_ID": profile.get("Line_item"),
                "Manufacturer": "UNKNOWN",
                "Product_name": profile.get("Resolved_product_name") or desc or "UNKNOWN",
                "Model": "UNKNOWN",
                "Part_number": "UNKNOWN",
                "SKU": "UNKNOWN",
                "Supplier": "UNKNOWN",
                "Product_URL_reference": "UNKNOWN",
                "Specifications": {
                    "Materials": (spec.get("Materials") if isinstance(spec, dict) else None)
                    or (profile.get("Specifications") or {}).get("Materials"),
                    "Dimensions": (spec.get("Dimensions") if isinstance(spec, dict) else None)
                    or (profile.get("Specifications") or {}).get("Dimensions"),
                    "Standards": (spec.get("Standards") if isinstance(spec, dict) else None)
                    or (profile.get("Specifications") or {}).get("Standards"),
                    "Application": (spec.get("Application") if isinstance(spec, dict) else None)
                    or (profile.get("Specifications") or {}).get("Application"),
                    "Required_features": (spec.get("Required_features") if isinstance(spec, dict) else None)
                    or (profile.get("Specifications") or {}).get("Required_features"),
                },
                "Match_confidence": MATCH_UNKNOWN,
                "Evidence_source": "specification_search_brief",
                "Pricing_evidence": "UNKNOWN",
                "candidate_origin": "specification_search_brief",
                "status": "NEEDS_COMMERCIAL_MATCH_RESEARCH",
                "search_query": _build_search_query(profile),
            }
        )

    # Optional paid web — cost-gated externally; only attach evidenced products/prices
    if allow_paid_web:
        product_for_web = {
            "Manufacturer": mfr or "UNKNOWN",
            "Model": model or "UNKNOWN",
            "Part_number": pn or "UNKNOWN",
            "NSN": profile.get("NSN") or "UNKNOWN",
            "SKU": profile.get("SKU") or "UNKNOWN",
            "sufficient_for_pricing_research": bool(mfr and (pn or model))
            or bool(spec.get("well_defined")),
            "Technical_description": _build_search_query(profile),
        }
        try:
            web = research_public_pricing_web(row, product_for_web, allow_paid=True)
            for e in web.get("evidence") or []:
                if not isinstance(e, dict):
                    continue
                amount = _num(e.get("amount"))
                candidates.append(
                    {
                        "kind": "COMMERCIAL_PRODUCT_CANDIDATE",
                        "Opportunity_ID": row.get("canonical_id"),
                        "Line_item_ID": profile.get("Line_item"),
                        "Manufacturer": e.get("Manufacturer") or mfr or "UNKNOWN",
                        "Product_name": e.get("product_matched") or desc or "UNKNOWN",
                        "Model": e.get("Model") or "UNKNOWN",
                        "Part_number": e.get("Part_number") or pn or "UNKNOWN",
                        "SKU": "UNKNOWN",
                        "Supplier": e.get("Source") or "UNKNOWN",
                        "Product_URL_reference": e.get("url") or e.get("Source") or "UNKNOWN",
                        "Specifications": spec,
                        "Match_confidence": e.get("Product_match_confidence") or MATCH_LOW,
                        "Evidence_source": "public_web_pricing_research",
                        "Pricing_evidence": amount if amount is not None else "UNKNOWN",
                        "pricing_level": e.get("level") or PRICE_LEVEL_2_PUBLIC,
                        "candidate_origin": "web_research",
                    }
                )
        except Exception as exc:  # noqa: BLE001
            log.debug("web candidate research skipped: %s", exc)

    return candidates[:12]


def _build_search_query(profile: dict[str, Any]) -> str:
    spec = profile.get("SPECIFICATION_PROFILE") or {}
    parts = [
        str(profile.get("Resolved_product_name") or profile.get("Original_description") or ""),
    ]
    mats = spec.get("Materials")
    if isinstance(mats, list):
        parts.extend(str(m) for m in mats[:3])
    dims = spec.get("Dimensions")
    if isinstance(dims, list):
        parts.extend(str(d) for d in dims[:2])
    apps = spec.get("Application")
    if isinstance(apps, list):
        parts.extend(str(a) for a in apps[:2] if not re.search(r"\bbids?\b", str(a), re.I))
    return " ".join(p for p in parts if p and p != "UNKNOWN")[:240]


# ---------------------------------------------------------------------------
# Phase 2 — compliance matching
# ---------------------------------------------------------------------------

def build_compliance_match_profile(
    profile: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, Any]:
    """GOVERNMENT REQUIREMENT vs commercial candidate — no assumed compliance."""
    # Spec search brief is not a commercial product — never compliance-score it as a match
    if candidate.get("candidate_origin") == "specification_search_brief":
        return {
            "kind": "COMPLIANCE_MATCH_PROFILE",
            "comparisons": [],
            "COMPLIANCE_CONFIDENCE": MATCH_UNKNOWN,
            "overall": "SEARCH_BRIEF_NOT_A_PRODUCT",
            "mismatch_count": 0,
            "match_count": 0,
            "possible_count": 0,
            "unknown_count": 0,
            "notes": [
                "search_brief_is_research_starting_point_not_compliant_product",
                "known_material_mismatch_not_presented_as_compliant",
            ],
        }

    gov_spec = profile.get("Specifications") or {}
    cand_spec = candidate.get("Specifications") or {}
    if not isinstance(cand_spec, dict):
        cand_spec = {}

    comparisons: list[dict[str, Any]] = []

    def _cmp(req_name: str, gov_val: Any, cand_val: Any) -> None:
        g = "UNKNOWN" if not _known(gov_val) or gov_val == "UNKNOWN" else gov_val
        c = "UNKNOWN" if not _known(cand_val) or cand_val == "UNKNOWN" else cand_val
        if g == "UNKNOWN":
            result = UNKNOWN
        elif c == "UNKNOWN":
            result = UNKNOWN
        else:
            g_txt = " ".join(map(str, g)) if isinstance(g, list) else str(g)
            c_txt = " ".join(map(str, c)) if isinstance(c, list) else str(c)
            g_l, c_l = g_txt.lower(), c_txt.lower()
            if g_l in c_l or c_l in g_l:
                result = "MATCH"
            elif any(tok in c_l for tok in re.findall(r"[a-z0-9\-]{4,}", g_l)[:6]):
                result = "POSSIBLE_MATCH"
            else:
                result = "MISMATCH"
        comparisons.append(
            {
                "Requirement": req_name,
                "Government": g if not isinstance(g, list) else g[:5],
                "Candidate": c if not isinstance(c, list) else c[:5],
                "Result": result,
            }
        )

    _cmp("Materials", gov_spec.get("Materials"), cand_spec.get("Materials"))
    _cmp("Dimensions", gov_spec.get("Dimensions"), cand_spec.get("Dimensions"))
    _cmp("Standards", gov_spec.get("Standards"), cand_spec.get("Standards"))
    _cmp("Application", gov_spec.get("Application"), cand_spec.get("Application"))
    _cmp("Required_features", gov_spec.get("Required_features"), cand_spec.get("Required_features"))

    # Identity fields
    if _known(profile.get("Manufacturer")) and _known(candidate.get("Manufacturer")):
        same = str(profile["Manufacturer"]).lower() == str(candidate["Manufacturer"]).lower()
        comparisons.append(
            {
                "Requirement": "Manufacturer",
                "Government": profile["Manufacturer"],
                "Candidate": candidate["Manufacturer"],
                "Result": "MATCH" if same else "MISMATCH",
            }
        )

    results = [c["Result"] for c in comparisons]
    mismatches = results.count("MISMATCH")
    matches = results.count("MATCH")
    possibles = results.count("POSSIBLE_MATCH")
    unknowns = results.count(UNKNOWN)

    if mismatches:
        conf = MATCH_LOW
        overall = "NOT_COMPLIANT_EVIDENCED"
    elif matches >= 2 and possibles + matches >= 3:
        conf = MATCH_HIGH
        overall = "COMPLIANT_EVIDENCED"
    elif matches >= 1 or possibles >= 2:
        conf = MATCH_MEDIUM
        overall = "POSSIBLY_COMPLIANT"
    else:
        conf = MATCH_UNKNOWN
        overall = "INSUFFICIENT_COMPARISON"

    return {
        "kind": "COMPLIANCE_MATCH_PROFILE",
        "comparisons": comparisons,
        "COMPLIANCE_CONFIDENCE": conf,
        "overall": overall,
        "mismatch_count": mismatches,
        "match_count": matches,
        "possible_count": possibles,
        "unknown_count": unknowns,
        "notes": ["known_material_mismatch_not_presented_as_compliant"],
    }


# ---------------------------------------------------------------------------
# Phases 4–5 — supplier channels + access
# ---------------------------------------------------------------------------

def discover_supplier_channels(row: dict[str, Any], profile: dict[str, Any], candidate: dict[str, Any]) -> list[dict[str, Any]]:
    """Realistic channel types from category/evidence — no preferred vendors hardcoded."""
    blob = f"{profile.get('Original_description')} {profile.get('Resolved_product_name')} {row.get('title')}"
    channels: list[dict[str, Any]] = []
    for ctype, pat in CHANNEL_HINTS:
        if pat.search(blob or ""):
            channels.append(
                {
                    "Channel_type": ctype,
                    "Evidence": pat.search(blob).group(0),
                    "Confidence": MATCH_MEDIUM,
                    "status": "PATH_IDENTIFIED_UNVERIFIED",
                }
            )
    # Reuse supplier intelligence mapping when identity allows
    product = {
        "Manufacturer": candidate.get("Manufacturer") or "UNKNOWN",
        "Model": candidate.get("Model") or "UNKNOWN",
        "Part_number": candidate.get("Part_number") or "UNKNOWN",
        "NSN": profile.get("NSN") or "UNKNOWN",
        "quantity": profile.get("Quantity"),
        "sufficient_for_pricing_research": _known(candidate.get("Manufacturer"))
        and (_known(candidate.get("Part_number")) or _known(candidate.get("Model"))),
    }
    try:
        supply = map_supply_chain(row, product)
        for role_key, role_name in (
            ("OEM", "manufacturer_oem_direct"),
            ("Public_distributors", "authorized_or_public_distributor"),
            ("all_channels", "mapped_channel"),
        ):
            for ch in supply.get(role_key) or []:
                if not isinstance(ch, dict):
                    continue
                channels.append(
                    {
                        "Channel_type": role_name,
                        "Supplier": ch.get("name") or ch.get("Supplier") or "UNKNOWN",
                        "Evidence": ch.get("evidence") or ch.get("source") or "supplier_intelligence_map",
                        "Confidence": ch.get("confidence") or MATCH_LOW,
                        "status": "MAPPED_FROM_SUPPLIER_INTELLIGENCE",
                    }
                )
    except Exception:
        pass

    if not channels:
        channels.append(
            {
                "Channel_type": "specialty_or_unknown",
                "Evidence": "no_channel_fingerprint_yet",
                "Confidence": MATCH_UNKNOWN,
                "status": "CHANNEL_RESEARCH_REQUIRED",
            }
        )
    # Dedup by type+supplier
    seen = set()
    out = []
    for ch in channels:
        key = (ch.get("Channel_type"), ch.get("Supplier"))
        if key in seen:
            continue
        seen.add(key)
        out.append(ch)
    return out[:10]


def build_supplier_access_profile(channel: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    """SUPPLIER_ACCESS_PROFILE — unknowns stay UNKNOWN."""
    return {
        "kind": "SUPPLIER_ACCESS_PROFILE",
        "Supplier": channel.get("Supplier") or candidate.get("Supplier") or "UNKNOWN",
        "Manufacturer": candidate.get("Manufacturer") or "UNKNOWN",
        "Channel_type": channel.get("Channel_type") or "UNKNOWN",
        "Authorized_status": "UNKNOWN",
        "Public_price_available": candidate.get("Pricing_evidence") not in {None, "UNKNOWN"},
        "Volume_pricing_available": "UNKNOWN",
        "Dealer_reseller_program_evidence": "UNKNOWN",
        "Special_project_pricing_evidence": "UNKNOWN",
        "Government_public_sector_program_evidence": "UNKNOWN",
        "Account_required": "UNKNOWN",
        "Credit_required": "UNKNOWN",
        "Prepayment_possible": "UNKNOWN",
        "Third_party_financier_payment_compatibility": "UNKNOWN",
        "Minimum_order": "UNKNOWN",
        "Lead_time": "UNKNOWN",
        "Evidence": channel.get("Evidence") or "UNKNOWN",
        "Confidence": channel.get("Confidence") or MATCH_UNKNOWN,
        "notes": ["do_not_assume_net30_credit_or_authorization"],
    }


# ---------------------------------------------------------------------------
# Phase 6–7 — pricing ladder + acquisition channel status
# ---------------------------------------------------------------------------

def collect_pricing_ladder(
    row: dict[str, Any],
    profile: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    """Public pricing evidence ladder LEVEL 1–5."""
    product = {
        "Manufacturer": profile.get("Manufacturer"),
        "Model_number": profile.get("Model_number"),
        "Manufacturer_part_number": profile.get("Manufacturer_part_number"),
        "NSN": profile.get("NSN"),
        "Description": profile.get("Resolved_product_name"),
    }
    evidence = collect_existing_price_evidence(row, product)
    for c in candidates:
        amt = _num(c.get("Pricing_evidence"))
        if amt is not None:
            evidence.append(
                {
                    "Source": c.get("Supplier") or c.get("Evidence_source"),
                    "amount": amt,
                    "level": c.get("pricing_level") or PRICE_LEVEL_2_PUBLIC,
                    "Date": _utc()[:10],
                    "product_matched": c.get("Product_name"),
                    "Product_match_confidence": c.get("Match_confidence"),
                }
            )
    # Row commercial pricing
    cp = row.get("commercial_pricing") if isinstance(row.get("commercial_pricing"), dict) else {}
    for key, level in (
        ("verified_wholesale_unit", PRICE_LEVEL_1_ACTUAL),
        ("lowest_public_new_unit", PRICE_LEVEL_2_PUBLIC),
        ("public_unit_price", PRICE_LEVEL_2_PUBLIC),
        ("comparable_unit", PRICE_LEVEL_3_COMPARABLE),
        ("msrp", PRICE_LEVEL_3_COMPARABLE),
    ):
        amt = _num(cp.get(key))
        if amt is not None:
            evidence.append({"Source": f"row.commercial_pricing.{key}", "amount": amt, "level": level, "Date": "UNKNOWN"})

    by_level = {
        "LEVEL_1": [],
        "LEVEL_2": [],
        "LEVEL_3": [],
        "LEVEL_4": [],
        "LEVEL_5": [],
    }
    for e in evidence:
        if not isinstance(e, dict) or _num(e.get("amount")) is None:
            continue
        lvl = str(e.get("level") or "")
        item = {
            "Price": _num(e.get("amount")),
            "Quantity_tier": e.get("quantity_tier") or "UNKNOWN",
            "Supplier": e.get("Source") or "UNKNOWN",
            "Date_observed": e.get("Date") or "UNKNOWN",
            "Freight_included": "UNKNOWN",
            "Configuration": e.get("product_matched") or "UNKNOWN",
            "Evidence_URL_reference": e.get("url") or e.get("Source") or "UNKNOWN",
            "Pricing_level": lvl,
            "Confidence": e.get("Product_match_confidence") or MATCH_LOW,
        }
        if lvl == PRICE_LEVEL_1_ACTUAL:
            by_level["LEVEL_1"].append(item)
        elif lvl == PRICE_LEVEL_2_PUBLIC:
            by_level["LEVEL_2"].append(item)
        elif lvl == PRICE_LEVEL_3_COMPARABLE:
            by_level["LEVEL_3"].append(item)
        else:
            by_level["LEVEL_4"].append(item)

    if not any(by_level[k] for k in ("LEVEL_1", "LEVEL_2", "LEVEL_3", "LEVEL_4")):
        by_level["LEVEL_5"] = [{"Pricing_level": LEVEL_5_NO_PRICE, "Price": "UNKNOWN"}]

    prices = [
        i["Price"]
        for k in ("LEVEL_1", "LEVEL_2", "LEVEL_3", "LEVEL_4")
        for i in by_level[k]
        if isinstance(i.get("Price"), (int, float))
    ]
    best = min(prices) if prices else None
    median = sorted(prices)[len(prices) // 2] if prices else None
    highest = max(prices) if prices else None

    return {
        "kind": "MARKET_PRICING_LADDER",
        "levels": by_level,
        "best_observed_unit": best if best is not None else "UNKNOWN",
        "median_observed_unit": median if median is not None else "UNKNOWN",
        "highest_observed_unit": highest if highest is not None else "UNKNOWN",
        "counts": {k: len(v) for k, v in by_level.items()},
    }


def classify_acquisition_channel_status(
    pricing: dict[str, Any],
    channels: list[dict[str, Any]],
    *,
    public_fails_target: bool = False,
) -> dict[str, Any]:
    """Phase 7 — do not mark blocked when wholesale paths remain untested."""
    has_public = bool(pricing.get("counts", {}).get("LEVEL_1") or pricing.get("counts", {}).get("LEVEL_2"))
    has_l1 = bool(pricing.get("counts", {}).get("LEVEL_1"))
    channel_types = {c.get("Channel_type") for c in channels}

    if has_l1:
        primary = VERIFIED_ACQUISITION_PRICE
    elif has_public and public_fails_target:
        primary = PUBLIC_ECONOMICS_FAILS_WHOLESALE_VERIFICATION_REQUIRED
    elif has_public:
        primary = PUBLIC_PRICE_AVAILABLE
    elif "manufacturer_oem_direct" in channel_types:
        primary = MANUFACTURER_DIRECT_PATH_IDENTIFIED
    elif any("distributor" in str(t) for t in channel_types):
        primary = WHOLESALE_ACCESS_UNVERIFIED
    else:
        primary = WHOLESALE_ACCESS_UNVERIFIED

    return {
        "kind": "ACQUISITION_CHANNEL_STATUS",
        "status": primary,
        "wholesale_unverified": primary
        in {
            WHOLESALE_ACCESS_UNVERIFIED,
            PUBLIC_ECONOMICS_FAILS_WHOLESALE_VERIFICATION_REQUIRED,
            PUBLIC_PRICE_AVAILABLE,
            PROJECT_PRICING_IDENTIFIED,
            VOLUME_PRICING_IDENTIFIED,
            RESELLER_PROGRAM_IDENTIFIED,
            MANUFACTURER_DIRECT_PATH_IDENTIFIED,
        },
        "notes": [
            "public_price_above_target_does_not_mean_deal_impossible",
            "unknown_wholesale_not_price_access_blocked",
        ],
    }


# ---------------------------------------------------------------------------
# Phase 8 — government revenue ladder
# ---------------------------------------------------------------------------

def build_government_revenue_profile(row: dict[str, Any]) -> dict[str, Any]:
    """Revenue evidence ladder — historical is benchmark, not current value."""
    sources: list[dict[str, Any]] = []
    award = _num(row.get("award_amount"))
    est = _num(row.get("estimated_value") or row.get("government_revenue") or row.get("solicitation_value"))
    meta = row.get("raw_metadata") if isinstance(row.get("raw_metadata"), dict) else {}
    hist = _num(meta.get("historical_award_amount") or row.get("historical_award_amount"))
    qty = resolve_quantity(row)

    if award is not None:
        sources.append(
            {
                "Revenue_value": award,
                "Unit_price": round(award / qty, 2) if qty else "UNKNOWN",
                "Quantity": qty if qty is not None else "UNKNOWN",
                "Evidence_type": "award_amount",
                "Source": "row.award_amount",
                "Date": row.get("award_date") or "UNKNOWN",
                "Exact_current_historical_comparable": "current",
                "Confidence": MATCH_HIGH,
            }
        )
        status = CURRENT_VERIFIED_REVENUE
        primary = award
    elif est is not None:
        sources.append(
            {
                "Revenue_value": est,
                "Unit_price": round(est / qty, 2) if qty else "UNKNOWN",
                "Quantity": qty if qty is not None else "UNKNOWN",
                "Evidence_type": "estimated_value",
                "Source": "row.estimated_value",
                "Date": "UNKNOWN",
                "Exact_current_historical_comparable": "current_estimated",
                "Confidence": MATCH_MEDIUM,
            }
        )
        status = CURRENT_ESTIMATED_REVENUE
        primary = est
    elif hist is not None:
        sources.append(
            {
                "Revenue_value": hist,
                "Unit_price": "UNKNOWN",
                "Quantity": qty if qty is not None else "UNKNOWN",
                "Evidence_type": "historical_award",
                "Source": "historical_metadata",
                "Date": meta.get("historical_award_date") or "UNKNOWN",
                "Exact_current_historical_comparable": "historical",
                "Confidence": MATCH_LOW,
            }
        )
        status = HISTORICAL_EXACT_BENCHMARK
        primary = hist
    else:
        # Comparable from competitive intelligence if present
        comp = row.get("competitive_intelligence") if isinstance(row.get("competitive_intelligence"), dict) else {}
        hist_price = _num((comp.get("HISTORICAL") or {}).get("typical_award") or (comp.get("Winner_foundation") or {}).get("award_amount"))
        if hist_price is not None:
            sources.append(
                {
                    "Revenue_value": hist_price,
                    "Unit_price": "UNKNOWN",
                    "Quantity": qty if qty is not None else "UNKNOWN",
                    "Evidence_type": "comparable_historical",
                    "Source": "competitive_intelligence",
                    "Date": "UNKNOWN",
                    "Exact_current_historical_comparable": "comparable",
                    "Confidence": MATCH_LOW,
                }
            )
            status = HISTORICAL_COMPARABLE_BENCHMARK
            primary = hist_price
        else:
            status = INSUFFICIENT_REVENUE_EVIDENCE
            primary = None

    return {
        "kind": "GOVERNMENT_REVENUE_PROFILE",
        "status": status,
        "primary_revenue": primary if primary is not None else "UNKNOWN",
        "sources": sources,
        "notes": ["historical_benchmark_is_not_automatic_current_contract_value"],
    }


# ---------------------------------------------------------------------------
# Phases 9–12 — target cost, profit gap, scenarios
# ---------------------------------------------------------------------------

def calculate_acquisition_target(
    revenue_profile: dict[str, Any],
    row: dict[str, Any],
    *,
    quantity: float | None,
) -> dict[str, Any]:
    """MAX_ACQUISITION_COST = revenue - target profit - freight - financing - expenses."""
    revenue = _num(revenue_profile.get("primary_revenue"))
    # Only use current verified/estimated for target calc — not historical alone
    status = revenue_profile.get("status")
    if status not in {CURRENT_VERIFIED_REVENUE, CURRENT_ESTIMATED_REVENUE} or revenue is None:
        return {
            "kind": "ACQUISITION_TARGET",
            "calculable": False,
            "reason": f"revenue_status_{status}",
            "TOTAL_TARGET_ACQUISITION_COST": "UNKNOWN",
            "TARGET_UNIT_ACQUISITION_COST": "UNKNOWN",
            "Target_profit": target_profit_usd(row),
        }

    target_profit = target_profit_usd(row)
    op = row.get("operator_economics") if isinstance(row.get("operator_economics"), dict) else {}
    freight = _num(op.get("estimated_freight_usd"))  # UNKNOWN → not invented as 0 in formula display
    financing = _num(op.get("estimated_financing_cost_usd"))
    expenses = _num(op.get("known_fees_usd")) or 0.0

    # Conservative: if freight/financing unknown, leave as 0 in formula but flag
    freight_used = freight if freight is not None else 0.0
    financing_used = financing if financing is not None else 0.0
    total_target = round(revenue - target_profit - freight_used - financing_used - expenses, 2)
    unit_target = round(total_target / quantity, 2) if quantity and quantity > 0 else "UNKNOWN"

    return {
        "kind": "ACQUISITION_TARGET",
        "calculable": True,
        "Government_revenue": revenue,
        "Target_profit": target_profit,
        "Estimated_freight": freight if freight is not None else "UNKNOWN",
        "Estimated_financing_cost": financing if financing is not None else "UNKNOWN",
        "Known_transaction_expenses": expenses,
        "TOTAL_TARGET_ACQUISITION_COST": total_target,
        "TARGET_UNIT_ACQUISITION_COST": unit_target,
        "unknown_cost_components": [
            x
            for x, v in (("freight", freight), ("financing", financing))
            if v is None
        ],
        "notes": ["unknown_freight_financing_not_invented_as_positive_costs"],
    }


def build_acquisition_price_gap(
    target: dict[str, Any],
    pricing: dict[str, Any],
    *,
    quantity: float | None,
    revenue: float | None,
    row: dict[str, Any],
) -> dict[str, Any]:
    """Phase 10 — required profit-gap math when observed price exists."""
    target_unit = _num(target.get("TARGET_UNIT_ACQUISITION_COST"))
    observed_unit = _num(pricing.get("best_observed_unit"))
    qty = quantity if quantity and quantity > 0 else None
    target_profit = target_profit_usd(row)

    if target_unit is None or observed_unit is None or qty is None:
        return {
            "kind": "ACQUISITION_PRICE_GAP_PROFILE",
            "calculable": False,
            "Target_unit_cost": target_unit if target_unit is not None else "UNKNOWN",
            "Observed_unit_cost": observed_unit if observed_unit is not None else "UNKNOWN",
            "Unit_difference": "UNKNOWN",
            "Quantity": qty if qty is not None else "UNKNOWN",
            "Total_difference": "UNKNOWN",
            "Target_profit": target_profit,
            "Expected_profit_at_observed_price": "UNKNOWN",
            "Margin_pct": "UNKNOWN",
            "Profit_target_status": STATUS_UNKNOWN,
            "Confidence": MATCH_UNKNOWN,
        }

    unit_diff = round(observed_unit - target_unit, 2)
    total_diff = round(unit_diff * qty, 2)
    # Expected profit at observed: revenue - observed_total - known fees (freight/fin unknown flagged)
    op = row.get("operator_economics") if isinstance(row.get("operator_economics"), dict) else {}
    fees = (_num(op.get("estimated_freight_usd")) or 0.0) + (_num(op.get("estimated_financing_cost_usd")) or 0.0) + (
        _num(op.get("known_fees_usd")) or 0.0
    )
    observed_total = round(observed_unit * qty, 2)
    expected_profit = round(revenue - observed_total - fees, 2) if revenue is not None else None
    margin = round((expected_profit / revenue) * 100, 2) if expected_profit is not None and revenue else None
    status = classify_profit_target_status(
        projected_profit=expected_profit, target=target_profit, has_cost=True
    )

    return {
        "kind": "ACQUISITION_PRICE_GAP_PROFILE",
        "calculable": True,
        "Target_unit_cost": target_unit,
        "Observed_unit_cost": observed_unit,
        "Unit_difference": unit_diff,
        "Quantity": qty,
        "Total_difference": total_diff,
        "Target_profit": target_profit,
        "Expected_profit_at_observed_price": expected_profit if expected_profit is not None else "UNKNOWN",
        "Margin_pct": margin if margin is not None else "UNKNOWN",
        "Profit_target_status": status,
        "Confidence": MATCH_MEDIUM,
        "notes": ["positive_unit_difference_means_observed_above_target_acquisition"],
    }


def build_price_scenarios(
    target: dict[str, Any],
    pricing: dict[str, Any],
    *,
    quantity: float | None,
    revenue: float | None,
    row: dict[str, Any],
) -> dict[str, Any]:
    """Phase 12 — multiple price scenarios."""
    qty = quantity if quantity and quantity > 0 else None
    target_profit = target_profit_usd(row)
    op = row.get("operator_economics") if isinstance(row.get("operator_economics"), dict) else {}
    fees = (_num(op.get("estimated_freight_usd")) or 0.0) + (_num(op.get("estimated_financing_cost_usd")) or 0.0) + (
        _num(op.get("known_fees_usd")) or 0.0
    )
    operator_price = _num(op.get("operator_entered_unit_price"))

    def _scenario(name: str, unit: float | None) -> dict[str, Any]:
        if unit is None or qty is None or revenue is None:
            return {
                "name": name,
                "unit_price": unit if unit is not None else "UNKNOWN",
                "Expected_profit": "UNKNOWN",
                "Profit_margin_pct": "UNKNOWN",
                "Capital_required": "UNKNOWN",
                "Difference_from_10k_target": "UNKNOWN",
            }
        total = round(unit * qty, 2)
        profit = round(revenue - total - fees, 2)
        margin = round((profit / revenue) * 100, 2) if revenue else None
        return {
            "name": name,
            "unit_price": unit,
            "total_acquisition": total,
            "Expected_profit": profit,
            "Profit_margin_pct": margin,
            "Capital_required": total,
            "Difference_from_10k_target": round(profit - target_profit, 2),
        }

    return {
        "kind": "PRICE_SCENARIOS",
        "scenarios": [
            _scenario("best_current_observed", _num(pricing.get("best_observed_unit"))),
            _scenario("median_credible_observed", _num(pricing.get("median_observed_unit"))),
            _scenario("highest_credible_observed", _num(pricing.get("highest_observed_unit"))),
            _scenario("target_acquisition", _num(target.get("TARGET_UNIT_ACQUISITION_COST"))),
            _scenario("operator_entered", operator_price),
        ],
        "Target_profit": target_profit,
    }


# ---------------------------------------------------------------------------
# Phases 13–15 — first transaction, financing, reseller signals
# ---------------------------------------------------------------------------

def build_first_transaction_profile(
    row: dict[str, Any],
    gap: dict[str, Any],
    channel_status: dict[str, Any],
    financing: dict[str, Any],
) -> dict[str, Any]:
    """Positive profit + manageable execution may be TRACK_RECORD_BUILDER — not auto-pursue."""
    profit = _num(gap.get("Expected_profit_at_observed_price"))
    status = gap.get("Profit_target_status") or STATUS_UNKNOWN
    exec_i = row.get("execution_intelligence") if isinstance(row.get("execution_intelligence"), dict) else {}
    complexity = str((exec_i.get("EXECUTION") or {}).get("complexity") or row.get("execution_complexity") or "UNKNOWN")

    reasons: list[str] = []
    state = NORMAL_PROFIT_TARGET
    if status in {STATUS_EXCEEDS, STATUS_MEETS}:
        state = NORMAL_PROFIT_TARGET
        reasons.append("meets_or_exceeds_profit_target")
    elif profit is not None and profit > 0 and status in {STATUS_BELOW, STATUS_WITHIN}:
        if channel_status.get("wholesale_unverified"):
            state = FIRST_TRANSACTION_CANDIDATE
            reasons.append("positive_profit_public_path_wholesale_unverified")
        else:
            state = TRACK_RECORD_BUILDER
            reasons.append("positive_profit_below_target_manageable_candidate")
        if financing.get("status") == "ECONOMICALLY_ATTRACTIVE_FUNDING_VERIFICATION_REQUIRED":
            reasons.append("funding_verification_still_required")
    elif profit is not None and profit <= 0:
        state = NOT_ATTRACTIVE
        reasons.append("non_positive_expected_profit_at_observed_price")
    elif status == STATUS_UNKNOWN:
        state = FIRST_TRANSACTION_CANDIDATE if channel_status.get("wholesale_unverified") else NORMAL_PROFIT_TARGET
        reasons.append("economics_unknown_not_rejected")

    return {
        "kind": "FIRST_TRANSACTION_ECONOMIC_PROFILE",
        "status": state,
        "Expected_profit": profit if profit is not None else "UNKNOWN",
        "Financing_friendliness": financing.get("status") or "UNKNOWN",
        "Execution_simplicity": complexity,
        "Supplier_accessibility": channel_status.get("status") or "UNKNOWN",
        "Government_buyer_quality": "UNKNOWN",
        "Payment_timing": "UNKNOWN",
        "Contract_size": resolve_revenue(row) if resolve_revenue(row) is not None else "UNKNOWN",
        "Repeat_potential": "UNKNOWN",
        "Relationship_building_value": "UNKNOWN",
        "reasons": reasons,
        "auto_recommend_pursue": False,
        "notes": ["track_record_builder_is_not_take_a_bad_deal", "present_facts_to_operator"],
    }


def build_financing_compatibility(row: dict[str, Any], gap: dict[str, Any]) -> dict[str, Any]:
    """Phase 14 — unknown financing ≠ rejection."""
    profit = _num(gap.get("Expected_profit_at_observed_price"))
    capital = _num(gap.get("Observed_unit_cost"))
    qty = _num(gap.get("Quantity"))
    capital_total = round(capital * qty, 2) if capital is not None and qty is not None else None
    fi = row.get("financing_intelligence") or row.get("funding_requirement") or {}
    if isinstance(fi, dict) and fi.get("status"):
        fin_status = fi.get("status")
    elif profit is not None and profit > 0 and capital_total is not None:
        fin_status = "ECONOMICALLY_ATTRACTIVE_FUNDING_VERIFICATION_REQUIRED"
    else:
        fin_status = "UNKNOWN"

    return {
        "kind": "FINANCING_COMPATIBILITY",
        "status": fin_status,
        "Required_external_capital": capital_total if capital_total is not None else "UNKNOWN",
        "Supplier_payment_timing": "UNKNOWN",
        "Government_payment_timing": "UNKNOWN",
        "Transaction_duration": "UNKNOWN",
        "Gross_spread_for_financing_cost": profit if profit is not None else "UNKNOWN",
        "notes": ["unknown_financing_eligibility_is_not_rejection"],
    }


def build_reseller_acquisition_signal(row: dict[str, Any]) -> dict[str, Any]:
    """Phase 15 — intelligence only; no claimed competitor profit without cost evidence."""
    comp = row.get("competitive_intelligence") if isinstance(row.get("competitive_intelligence"), dict) else {}
    winners = (comp.get("Winner_foundation") or comp.get("INCUMBENT") or {})
    gov_paid = _num(winners.get("award_amount") or resolve_revenue(row))
    upstream = _num(winners.get("disclosed_acquisition") or winners.get("upstream_price"))
    spread = round(gov_paid - upstream, 2) if gov_paid is not None and upstream is not None else None
    return {
        "kind": "RESELLER_ACQUISITION_SIGNAL",
        "Previous_winners": winners.get("winners") or winners.get("incumbent") or "UNKNOWN",
        "Government_paid": gov_paid if gov_paid is not None else "UNKNOWN",
        "Historical_public_upstream_evidence": upstream if upstream is not None else "UNKNOWN",
        "Potential_gross_spread": spread if spread is not None else "UNKNOWN",
        "Confidence": MATCH_MEDIUM if spread is not None else MATCH_LOW if gov_paid is not None else MATCH_UNKNOWN,
        "notes": ["do_not_claim_competitor_profit_without_public_acquisition_cost"],
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def analyze_line_acquisition(
    row: dict[str, Any],
    profile: dict[str, Any],
    *,
    allow_paid_web: bool = False,
) -> dict[str, Any]:
    """Full acquisition intelligence for one identity profile / line."""
    substitution = profile.get("PRODUCT_SUBSTITUTION_PROFILE") or {}
    subst_status = substitution.get("Status") or UNKNOWN

    # Phase 3 — respect substitution
    search_mode = "specification_broad"
    if subst_status == EXACT_ONLY:
        search_mode = "exact_only"
    elif subst_status == EQUIVALENTS_ALLOWED:
        search_mode = "equivalents_allowed"
    elif subst_status == SPECIFICATION_BASED:
        search_mode = "specification_broad"
    else:
        search_mode = "research_substitution_rules_first"

    candidates = build_commercial_product_candidates(row, profile, allow_paid_web=allow_paid_web)
    if search_mode == "exact_only":
        # Filter to exact manufacturer/part when known
        mfr = profile.get("Manufacturer")
        pn = profile.get("Manufacturer_part_number")
        if _known(mfr) or _known(pn):
            filtered = []
            for c in candidates:
                if _known(pn) and str(c.get("Part_number")) == str(pn):
                    filtered.append(c)
                elif _known(mfr) and str(c.get("Manufacturer")).lower() == str(mfr).lower():
                    filtered.append(c)
                elif c.get("candidate_origin") == "specification_search_brief":
                    filtered.append({**c, "notes": ["exact_only_search_brief"]})
            candidates = filtered or candidates

    enriched: list[dict[str, Any]] = []
    high_compliant = 0
    possible = 0
    commercial_n = 0
    for c in candidates:
        comp = build_compliance_match_profile(profile, c)
        c = {**c, "COMPLIANCE_MATCH_PROFILE": comp}
        overall = comp.get("overall")
        if overall == "NOT_COMPLIANT_EVIDENCED":
            c["presented_as_compliant"] = False
        else:
            c["presented_as_compliant"] = overall in {
                "COMPLIANT_EVIDENCED",
                "POSSIBLY_COMPLIANT",
            }
        if c.get("candidate_origin") != "specification_search_brief":
            commercial_n += 1
            if overall == "COMPLIANT_EVIDENCED" and comp.get("COMPLIANCE_CONFIDENCE") == MATCH_HIGH:
                high_compliant += 1
            if overall == "POSSIBLY_COMPLIANT":
                possible += 1
        enriched.append(c)
    candidates = enriched

    channels: list[dict[str, Any]] = []
    access_profiles = []
    for c in candidates[:5]:
        chs = discover_supplier_channels(row, profile, c)
        channels.extend(chs)
        for ch in chs[:3]:
            access_profiles.append(build_supplier_access_profile(ch, c))

    pricing = collect_pricing_ladder(row, profile, candidates)
    revenue = build_government_revenue_profile(row)
    qty = _num(profile.get("Quantity")) or resolve_quantity(row)
    target = calculate_acquisition_target(revenue, row, quantity=qty)

    # Detect if public price fails target
    public_fails = False
    gap_preview = build_acquisition_price_gap(
        target, pricing, quantity=qty, revenue=_num(revenue.get("primary_revenue")), row=row
    )
    if gap_preview.get("calculable") and gap_preview.get("Profit_target_status") in {
        STATUS_BELOW,
        STATUS_UNVIABLE,
    }:
        public_fails = True

    channel_status = classify_acquisition_channel_status(
        pricing, channels, public_fails_target=public_fails
    )
    # If below target but wholesale unverified, annotate profit status context
    profit_status = gap_preview.get("Profit_target_status") or STATUS_UNKNOWN
    profit_annotation = None
    if profit_status == STATUS_BELOW and channel_status.get("wholesale_unverified"):
        profit_annotation = f"{STATUS_BELOW}+{WHOLESALE_ACCESS_UNVERIFIED}"
    elif profit_status == STATUS_UNVIABLE and channel_status.get("wholesale_unverified"):
        # Downgrade false unviable when wholesale untested
        profit_status = STATUS_BELOW
        profit_annotation = f"{STATUS_BELOW}+{WHOLESALE_ACCESS_UNVERIFIED}"
        channel_status = {
            **channel_status,
            "status": PUBLIC_ECONOMICS_FAILS_WHOLESALE_VERIFICATION_REQUIRED,
        }

    gap = {**gap_preview, "Profit_target_status": profit_status, "status_annotation": profit_annotation}
    scenarios = build_price_scenarios(
        target, pricing, quantity=qty, revenue=_num(revenue.get("primary_revenue")), row=row
    )
    financing = build_financing_compatibility(row, gap)
    first_tx = build_first_transaction_profile(row, gap, channel_status, financing)
    reseller = build_reseller_acquisition_signal(row)

    return {
        "kind": "M3LineAcquisitionIntelligence",
        "Opportunity_ID": row.get("canonical_id"),
        "Line_item": profile.get("Line_item"),
        "Government_requirement": profile.get("Original_description") or profile.get("Resolved_product_name"),
        "Identity_type": profile.get("Identity_type"),
        "Substitution_status": subst_status,
        "search_mode": search_mode,
        "Quantity": qty if qty is not None else "UNKNOWN",
        "COMMERCIAL_PRODUCT_CANDIDATES": candidates,
        "candidates_found": commercial_n,
        "search_briefs": sum(1 for c in candidates if c.get("candidate_origin") == "specification_search_brief"),
        "high_confidence_compliant": high_compliant,
        "possible_matches": possible,
        "no_match_found": commercial_n == 0,
        "SUPPLIER_CHANNELS": channels,
        "SUPPLIER_ACCESS_PROFILES": access_profiles,
        "MARKET_PRICING_LADDER": pricing,
        "ACQUISITION_CHANNEL_STATUS": channel_status,
        "GOVERNMENT_REVENUE_PROFILE": revenue,
        "ACQUISITION_TARGET": target,
        "ACQUISITION_PRICE_GAP_PROFILE": gap,
        "PRICE_SCENARIOS": scenarios,
        "FIRST_TRANSACTION_ECONOMIC_PROFILE": first_tx,
        "FINANCING_COMPATIBILITY": financing,
        "RESELLER_ACQUISITION_SIGNAL": reseller,
        "Missing_information": _missing_list(target, pricing, revenue, financing, candidates),
        "Next_Action": _next_action(target, pricing, revenue, channel_status, candidates),
        "DEVELOPMENT_NO_OUTREACH": True,
    }


def _missing_list(target, pricing, revenue, financing, candidates) -> list[str]:
    missing = []
    if revenue.get("status") == INSUFFICIENT_REVENUE_EVIDENCE:
        missing.append("government_revenue_evidence")
    if pricing.get("counts", {}).get("LEVEL_5"):
        missing.append("public_acquisition_pricing")
    if not any(c.get("Manufacturer") not in {None, "UNKNOWN"} for c in candidates):
        missing.append("commercial_manufacturer_match")
    if financing.get("status") in {"UNKNOWN", "ECONOMICALLY_ATTRACTIVE_FUNDING_VERIFICATION_REQUIRED"}:
        missing.append("financing_verification")
    if not target.get("calculable"):
        missing.append("target_acquisition_calculation_inputs")
    return missing


def _next_action(target, pricing, revenue, channel_status, candidates) -> str:
    if all(c.get("candidate_origin") == "specification_search_brief" for c in candidates) or not candidates:
        return "Research commercial products matching specification profile"
    if pricing.get("counts", {}).get("LEVEL_5"):
        return "Locate public supplier pricing for matched products"
    if channel_status.get("status") == PUBLIC_ECONOMICS_FAILS_WHOLESALE_VERIFICATION_REQUIRED:
        return "Verify wholesale/project/distributor pricing (public path below target)"
    if revenue.get("status") == INSUFFICIENT_REVENUE_EVIDENCE:
        return "Recover government revenue / award benchmark evidence"
    if target.get("calculable") and _num(pricing.get("best_observed_unit")) is not None:
        return "Owner review: compare observed vs target acquisition economics"
    return "Continue specification-to-acquisition research"


def assign_acquisition_queue(line_intel: dict[str, Any]) -> str:
    missing = line_intel.get("Missing_information") or []
    gap = line_intel.get("ACQUISITION_PRICE_GAP_PROFILE") or {}
    channel = line_intel.get("ACQUISITION_CHANNEL_STATUS") or {}
    if line_intel.get("no_match_found"):
        return Q_NEEDS_COMMERCIAL_PRODUCT_MATCH
    if "public_acquisition_pricing" in missing:
        return Q_NEEDS_PUBLIC_PRICING
    if channel.get("status") == PUBLIC_ECONOMICS_FAILS_WHOLESALE_VERIFICATION_REQUIRED:
        return Q_NEEDS_WHOLESALE_VERIFICATION
    if "government_revenue_evidence" in missing:
        return Q_NEEDS_REVENUE_EVIDENCE
    if "financing_verification" in missing and gap.get("calculable"):
        return Q_NEEDS_FINANCING_VERIFICATION
    if gap.get("calculable") and gap.get("Profit_target_status") in {
        STATUS_EXCEEDS,
        STATUS_MEETS,
        STATUS_WITHIN,
        STATUS_BELOW,
    }:
        return Q_ECONOMICS_READY if gap.get("Profit_target_status") in {STATUS_EXCEEDS, STATUS_MEETS} else Q_OWNER_REVIEW
    return Q_OWNER_REVIEW


def analyze_opportunity_acquisition(
    row: dict[str, Any],
    *,
    allow_paid_web: bool = False,
) -> dict[str, Any]:
    """Opportunity-level acquisition intelligence across eligible identity lines."""
    identity = _identity_bundle(row)
    profiles = _eligible_profiles(identity)
    if not profiles:
        # Fall back to primary profile even if not ready
        primary = identity.get("primary_profile")
        profiles = [primary] if isinstance(primary, dict) else []

    lines = [
        analyze_line_acquisition(row, p, allow_paid_web=allow_paid_web) for p in profiles[:15]
    ]
    queues: dict[str, list] = defaultdict(list)
    for li in lines:
        q = assign_acquisition_queue(li)
        queues[q].append(
            {
                "Opportunity": row.get("title"),
                "canonical_id": row.get("canonical_id"),
                "Line_item": li.get("Line_item"),
                "Requirement": li.get("Government_requirement"),
                "Next_Action": li.get("Next_Action"),
            }
        )

    # Also attach deal economics reuse
    try:
        deal_econ = build_deal_economics_profile(row)
    except Exception:
        deal_econ = {"kind": "DEAL_ECONOMICS_PROFILE", "PROFIT_TARGET_STATUS": STATUS_UNKNOWN}

    return {
        "kind": "M3AcquisitionTargetIntelligence",
        "Opportunity_ID": row.get("canonical_id"),
        "Opportunity": row.get("title"),
        "Identity_summary": {
            "Exact": identity.get("Exact_identities"),
            "Specification": identity.get("Specification_identities"),
            "Category_only": identity.get("Category_only"),
            "Unknown": identity.get("Unknown"),
        },
        "LINE_ACQUISITION": lines,
        "lines_analyzed": len(lines),
        "DEAL_ECONOMICS_REUSED": {
            "PROFIT_TARGET_STATUS": deal_econ.get("PROFIT_TARGET_STATUS"),
            "Target_acquisition_cost": deal_econ.get("Target_acquisition_cost"),
            "Projected_profit": deal_econ.get("Projected_profit"),
        },
        "queues": dict(queues),
        "VA": {
            "allowed_actions": sorted(VA_ALLOWED),
            "forbidden_actions": sorted(VA_FORBIDDEN),
            "may_contact_suppliers": False,
            "may_apply_for_accounts": False,
            "may_bid": False,
        },
        "Timestamp": _utc(),
        "DEVELOPMENT_NO_OUTREACH": True,
        "working_row_patch": {
            "acquisition_target_intelligence": {
                "kind": "M3AcquisitionTargetSummary",
                "lines_analyzed": len(lines),
                "primary_Next_Action": (lines[0].get("Next_Action") if lines else "No eligible lines"),
                "primary_channel_status": (lines[0].get("ACQUISITION_CHANNEL_STATUS") or {}).get("status")
                if lines
                else "UNKNOWN",
                "primary_profit_status": (lines[0].get("ACQUISITION_PRICE_GAP_PROFILE") or {}).get(
                    "Profit_target_status"
                )
                if lines
                else "UNKNOWN",
            }
        },
    }


def _priority_rows(store: Any, *, limit: int) -> list[dict[str, Any]]:
    rows = [r for r in (store.all() if hasattr(store, "all") else []) if isinstance(r, dict)]

    def score(r: dict[str, Any]) -> tuple:
        title = str(r.get("title") or "")
        real_boost = 100 if any(t.lower() in title.lower() for t in REAL_VALIDATION_TITLES) else 0
        ident = r.get("product_identity_resolution") or {}
        ready = int((ident.get("SUPPLIER_READINESS") or {}).get("Ready_with_specifications") or 0)
        exact = int(ident.get("Exact_identities") or 0)
        lines = r.get("line_items") or []
        qty = 1 if any(isinstance(li, dict) and _num(li.get("quantity")) for li in lines) else 0
        return (-(real_boost + ready * 10 + exact * 20 + qty * 5), str(r.get("canonical_id") or ""))

    return sorted(rows, key=score)[: max(1, min(limit, 40))]


def analyze_acquisition_targets_top(
    store: Any,
    *,
    limit: int = 25,
    allow_paid_web: bool = False,
    paid_limit: int = 0,
    persist: bool = True,
) -> dict[str, Any]:
    """Batch acquisition analysis — Cost Governor: paid only for paid_limit rows."""
    ranked = _priority_rows(store, limit=limit)
    index = load_acq_index() if persist else {"by_id": {}}
    by_id = index.setdefault("by_id", {})

    items = []
    paid_used = 0
    products_n = candidates_n = high_n = possible_n = no_match_n = 0
    mfrs: set[str] = set()
    channels_n = 0
    public_prices = 0
    wholesale_paths = 0
    level_counts = {"LEVEL_1": 0, "LEVEL_2": 0, "LEVEL_3": 0, "LEVEL_4": 0, "LEVEL_5": 0}
    rev_counts = {
        CURRENT_VERIFIED_REVENUE: 0,
        CURRENT_ESTIMATED_REVENUE: 0,
        HISTORICAL_EXACT_BENCHMARK: 0,
        HISTORICAL_COMPARABLE_BENCHMARK: 0,
        INSUFFICIENT_REVENUE_EVIDENCE: 0,
    }
    targets_calc = 0
    unit_targets = 0
    below = near = above = 0
    profit_gaps: list[dict[str, Any]] = []
    first_tx_candidates: list[dict[str, Any]] = []
    real_results: dict[str, Any] = {}
    queues: dict[str, list] = defaultdict(list)

    for row in ranked:
        use_paid = allow_paid_web and paid_used < paid_limit
        result = analyze_opportunity_acquisition(row, allow_paid_web=use_paid)
        if use_paid:
            paid_used += 1
        items.append(result)

        for li in result.get("LINE_ACQUISITION") or []:
            products_n += 1
            candidates_n += int(li.get("candidates_found") or 0)
            high_n += int(li.get("high_confidence_compliant") or 0)
            possible_n += int(li.get("possible_matches") or 0)
            if li.get("no_match_found"):
                no_match_n += 1
            for c in li.get("COMMERCIAL_PRODUCT_CANDIDATES") or []:
                if _known(c.get("Manufacturer")):
                    mfrs.add(str(c.get("Manufacturer")))
            channels_n += len(li.get("SUPPLIER_CHANNELS") or [])
            pricing = li.get("MARKET_PRICING_LADDER") or {}
            for lk, cnt in (pricing.get("counts") or {}).items():
                if lk in level_counts:
                    level_counts[lk] += int(cnt or 0)
            if _num(pricing.get("best_observed_unit")) is not None:
                public_prices += 1
            chs = li.get("ACQUISITION_CHANNEL_STATUS") or {}
            if chs.get("wholesale_unverified") or chs.get("status") in {
                WHOLESALE_ACCESS_UNVERIFIED,
                PUBLIC_ECONOMICS_FAILS_WHOLESALE_VERIFICATION_REQUIRED,
                PROJECT_PRICING_IDENTIFIED,
                VOLUME_PRICING_IDENTIFIED,
                MANUFACTURER_DIRECT_PATH_IDENTIFIED,
            }:
                wholesale_paths += 1
            rev = li.get("GOVERNMENT_REVENUE_PROFILE") or {}
            st = rev.get("status")
            if st in rev_counts:
                rev_counts[st] += 1
            tgt = li.get("ACQUISITION_TARGET") or {}
            if tgt.get("calculable"):
                targets_calc += 1
                if _num(tgt.get("TARGET_UNIT_ACQUISITION_COST")) is not None:
                    unit_targets += 1
            gap = li.get("ACQUISITION_PRICE_GAP_PROFILE") or {}
            if gap.get("calculable"):
                ud = _num(gap.get("Unit_difference"))
                if ud is not None:
                    if ud < -0.01:
                        below += 1  # observed below target cost = good
                    elif abs(ud) <= (_num(gap.get("Target_unit_cost")) or 0) * 0.05:
                        near += 1
                    else:
                        above += 1
                profit_gaps.append(
                    {
                        "Opportunity": row.get("title"),
                        "Government_requirement": li.get("Government_requirement"),
                        "Quantity": li.get("Quantity"),
                        "Revenue_evidence": (li.get("GOVERNMENT_REVENUE_PROFILE") or {}).get("status"),
                        "Target_acquisition_cost": tgt.get("TOTAL_TARGET_ACQUISITION_COST"),
                        "Best_observed_acquisition_price": gap.get("Observed_unit_cost"),
                        "Difference_per_unit": gap.get("Unit_difference"),
                        "Total_difference": gap.get("Total_difference"),
                        "Expected_profit": gap.get("Expected_profit_at_observed_price"),
                        "Profit_target_status": gap.get("Profit_target_status"),
                        "status_annotation": gap.get("status_annotation"),
                        "Wholesale_status": chs.get("status"),
                        "Financing_status": (li.get("FINANCING_COMPATIBILITY") or {}).get("status"),
                        "Confidence": gap.get("Confidence"),
                    }
                )
            ftx = li.get("FIRST_TRANSACTION_ECONOMIC_PROFILE") or {}
            if ftx.get("status") in {FIRST_TRANSACTION_CANDIDATE, TRACK_RECORD_BUILDER}:
                first_tx_candidates.append(
                    {
                        "Opportunity": row.get("title"),
                        "Why": ftx.get("reasons"),
                        "Expected_profit": ftx.get("Expected_profit"),
                        "Capital_requirement": (li.get("FINANCING_COMPATIBILITY") or {}).get(
                            "Required_external_capital"
                        ),
                        "Financing_status": ftx.get("Financing_friendliness"),
                        "Execution_complexity": ftx.get("Execution_simplicity"),
                        "Remaining_verification": li.get("Missing_information"),
                        "auto_recommend_pursue": False,
                    }
                )
            q = assign_acquisition_queue(li)
            queues[q].append(
                {
                    "Opportunity": row.get("title"),
                    "canonical_id": row.get("canonical_id"),
                    "Requirement": li.get("Government_requirement"),
                    "Next_Action": li.get("Next_Action"),
                }
            )

        title = str(row.get("title") or "")
        for key in REAL_VALIDATION_TITLES:
            if key.lower() in title.lower() and key not in real_results:
                primary = (result.get("LINE_ACQUISITION") or [{}])[0]
                real_results[key] = {
                    "Opportunity": title,
                    "Government_requirement": primary.get("Government_requirement"),
                    "Quantity": primary.get("Quantity"),
                    "Commercial_candidates": primary.get("candidates_found"),
                    "Compliance_high": primary.get("high_confidence_compliant"),
                    "Supplier_channels": len(primary.get("SUPPLIER_CHANNELS") or []),
                    "Observed_public_pricing": (primary.get("MARKET_PRICING_LADDER") or {}).get(
                        "best_observed_unit"
                    ),
                    "Government_revenue_evidence": (primary.get("GOVERNMENT_REVENUE_PROFILE") or {}).get(
                        "status"
                    ),
                    "Target_acquisition_cost": (primary.get("ACQUISITION_TARGET") or {}).get(
                        "TOTAL_TARGET_ACQUISITION_COST"
                    ),
                    "Expected_profit_at_observed": (
                        primary.get("ACQUISITION_PRICE_GAP_PROFILE") or {}
                    ).get("Expected_profit_at_observed_price"),
                    "Remaining_unknowns": primary.get("Missing_information"),
                    "Next_Action": primary.get("Next_Action"),
                }

        cid = row.get("canonical_id")
        if cid and hasattr(store, "_rows"):
            existing = store._rows.get(cid) or dict(row)
            patch = result.get("working_row_patch") or {}
            existing.update({k: v for k, v in patch.items() if v is not None})
            existing["acquisition_target_full"] = result
            store._rows[cid] = existing
        if cid:
            by_id[str(cid)] = {
                "summary": result.get("working_row_patch", {}).get("acquisition_target_intelligence"),
                "updated_at": _utc(),
            }

    if persist:
        try:
            if hasattr(store, "save"):
                store.save()
        except Exception:
            pass
        save_acq_index(index)

    return {
        "kind": "M3AcquisitionTargetRun",
        "analyzed": len(items),
        "SPECIFICATION_TO_PRODUCT": {
            "Products_analyzed": products_n,
            "Commercial_candidates_found": candidates_n,
            "High_confidence_compliant_matches": high_n,
            "Possible_matches": possible_n,
            "No_match_found": no_match_n,
        },
        "SUPPLIER_INTELLIGENCE": {
            "Manufacturers_identified": len(mfrs),
            "Supplier_channels_identified": channels_n,
            "Public_prices_found": public_prices,
            "Wholesale_project_pricing_paths_identified": wholesale_paths,
            "manufacturer_list": sorted(mfrs)[:20],
        },
        "PRICING_EVIDENCE": level_counts,
        "GOVERNMENT_REVENUE": rev_counts,
        "ACQUISITION_TARGETS": {
            "Opportunities_with_target_acquisition_price": targets_calc,
            "Target_unit_prices_calculated": unit_targets,
            "Observed_price_below_target": below,
            "Observed_price_near_target": near,
            "Observed_price_above_target": above,
        },
        "PROFIT_GAP": profit_gaps[:10],
        "FIRST_TRANSACTION": first_tx_candidates[:8],
        "REAL_OPPORTUNITY_RESULTS": real_results,
        "queues": {k: v[:20] for k, v in queues.items()},
        "COST": {"Paid_spend_actions": paid_used, "Paid_spend": 0},
        "SAFETY": {"Outreach_actions": 0},
        "items": items,
        "NEXT_STATE": "SPECIFICATION_TO_ACQUISITION_ECONOMICS_OPERATIONAL",
        "DEVELOPMENT_NO_OUTREACH": True,
    }


def deal_room_acquisition_section(row: dict[str, Any]) -> dict[str, Any]:
    """Phase 17 — ACQUISITION INTELLIGENCE panel."""
    full = row.get("acquisition_target_full")
    if not isinstance(full, dict) or full.get("kind") != "M3AcquisitionTargetIntelligence":
        try:
            full = analyze_opportunity_acquisition(row, allow_paid_web=False)
        except Exception:
            full = {}
    primary = (full.get("LINE_ACQUISITION") or [{}])[0]
    gap = primary.get("ACQUISITION_PRICE_GAP_PROFILE") or {}
    pricing = primary.get("MARKET_PRICING_LADDER") or {}
    return {
        "kind": "M3DealRoomAcquisitionIntelligence",
        "Government_requirement": primary.get("Government_requirement") or row.get("title"),
        "Commercial_product_candidates": (primary.get("COMMERCIAL_PRODUCT_CANDIDATES") or [])[:8],
        "Compliance_comparison": [
            (c.get("COMPLIANCE_MATCH_PROFILE") or {})
            for c in (primary.get("COMMERCIAL_PRODUCT_CANDIDATES") or [])[:5]
        ],
        "Manufacturer": next(
            (
                c.get("Manufacturer")
                for c in (primary.get("COMMERCIAL_PRODUCT_CANDIDATES") or [])
                if _known(c.get("Manufacturer"))
            ),
            "UNKNOWN",
        ),
        "Model": next(
            (
                c.get("Model")
                for c in (primary.get("COMMERCIAL_PRODUCT_CANDIDATES") or [])
                if _known(c.get("Model"))
            ),
            "UNKNOWN",
        ),
        "Part_number": next(
            (
                c.get("Part_number")
                for c in (primary.get("COMMERCIAL_PRODUCT_CANDIDATES") or [])
                if _known(c.get("Part_number"))
            ),
            "UNKNOWN",
        ),
        "Supplier_channels": primary.get("SUPPLIER_CHANNELS") or [],
        "Best_current_public_price": pricing.get("best_observed_unit"),
        "Other_observed_prices": {
            "median": pricing.get("median_observed_unit"),
            "highest": pricing.get("highest_observed_unit"),
        },
        "Government_revenue_evidence": primary.get("GOVERNMENT_REVENUE_PROFILE"),
        "Target_acquisition_price": (primary.get("ACQUISITION_TARGET") or {}).get(
            "TOTAL_TARGET_ACQUISITION_COST"
        ),
        "Difference_from_target": gap.get("Total_difference"),
        "Expected_profit_at_observed_price": gap.get("Expected_profit_at_observed_price"),
        "Profit_target_status": gap.get("Profit_target_status"),
        "status_annotation": gap.get("status_annotation"),
        "Wholesale_access_status": (primary.get("ACQUISITION_CHANNEL_STATUS") or {}).get("status"),
        "Financing_status": (primary.get("FINANCING_COMPATIBILITY") or {}).get("status"),
        "Missing_information": primary.get("Missing_information") or [],
        "Next_Action": primary.get("Next_Action") or "Run acquisition analysis",
        "PRICE_SCENARIOS": primary.get("PRICE_SCENARIOS"),
        "FIRST_TRANSACTION": primary.get("FIRST_TRANSACTION_ECONOMIC_PROFILE"),
        "VA_allowed_actions": sorted(VA_ALLOWED),
        "VA_forbidden_actions": sorted(VA_FORBIDDEN),
        "DEVELOPMENT_NO_OUTREACH": True,
    }


def apply_va_acquisition_update(
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

    notes = list(row.get("acquisition_va_notes") or [])
    if action_u in {"ADD_NOTES", "NOTE"} and note:
        notes.append({"at": _utc(), "note": str(note)[:2000], "action": action_u})
    if action_u == "ATTACH_PRICING_EVIDENCE" and isinstance(evidence, dict):
        cp = dict(row.get("commercial_pricing") or {})
        amt = _num(evidence.get("unit_price") or evidence.get("Price"))
        if amt is not None:
            level = str(evidence.get("level") or "public").lower()
            if "wholesale" in level or "verified" in level:
                cp["verified_wholesale_unit"] = amt
            else:
                cp["public_unit_price"] = amt
            row["commercial_pricing"] = cp
        notes.append({"at": _utc(), "note": note or "pricing_attached", "action": action_u})
    if action_u == "ATTACH_GOVERNMENT_PRICING_HISTORY" and isinstance(evidence, dict):
        amt = _num(evidence.get("award_amount") or evidence.get("Revenue_value"))
        if amt is not None:
            row["historical_award_amount"] = amt
        notes.append({"at": _utc(), "note": note or "gov_history_attached", "action": action_u})
    if action_u in {"RESEARCH_PUBLIC_SUPPLIERS", "RESEARCH_PRODUCT_SPECS", "CORRECT_EVIDENCE_BACKED_DATA"}:
        notes.append({"at": _utc(), "note": note or action_u, "action": action_u})
    if action_u == "UPDATE_STATUS" and status:
        row["acquisition_va_status"] = str(status).upper()
        notes.append({"at": _utc(), "note": f"status:{status}", "action": action_u})

    row["acquisition_va_notes"] = notes[-30:]
    if hasattr(store, "_rows"):
        store._rows[canonical_id] = row
    if hasattr(store, "save"):
        try:
            store.save()
        except Exception:
            pass
    return {"ok": True, "canonical_id": canonical_id, "action": action_u, "VA_notes": notes[-10:]}


def build_va_acquisition_queues(limit: int = 25) -> dict[str, Any]:
    """Phase 18 — operator/VA acquisition queues (populated by /analyze)."""
    idx = load_acq_index()
    return {
        "kind": "M3AcquisitionTargetQueues",
        "queues": {
            Q_NEEDS_COMMERCIAL_PRODUCT_MATCH: [],
            Q_NEEDS_PUBLIC_PRICING: [],
            Q_NEEDS_WHOLESALE_VERIFICATION: [],
            Q_NEEDS_REVENUE_EVIDENCE: [],
            Q_NEEDS_FINANCING_VERIFICATION: [],
            Q_ECONOMICS_READY: [],
            Q_OWNER_REVIEW: [],
        },
        "researched": len(idx.get("by_id") or {}),
        "VA_allowed_actions": sorted(VA_ALLOWED),
        "VA_forbidden_actions": sorted(VA_FORBIDDEN),
        "note": "Run /api/m3/acquisition-targets/analyze to populate queue cards from live pipeline",
        "limit": limit,
        "DEVELOPMENT_NO_OUTREACH": True,
    }
