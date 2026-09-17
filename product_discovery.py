"""Product-first SAM discovery using the existing sam_client.

NO automatic live calls. Explicit authorize_live required for sync.
"""

from __future__ import annotations

import os
import re
from typing import Any

from ai_funnel import (
    CLASS_PRODUCT_PLUS_SERVICE,
    CLASS_PRODUCT_RESELL,
    classify_opportunity,
)
from product_deal import FIT_CORE_PRODUCT, resolve_core_fit

# Verified wholesale/retail/manufacturing NAICS prefixes used by Stage 0 already.
# These are structural NAICS sector prefixes — not invented PSC mappings.
PRODUCT_NAICS_PREFIXES: tuple[str, ...] = ("33", "42", "44", "45")

# Explicit product-oriented NAICS codes for discovery mode (known sectors).
# Kept as a curated allowlist; UNKNOWN codes are not invented.
PRODUCT_DISCOVERY_NAICS: list[str] = [
    # Wholesale
    "423210",  # Furniture Merchant Wholesalers
    "423420",  # Office Equipment Merchant Wholesalers
    "423430",  # Computer & Peripheral Equipment Merchant Wholesalers
    "423450",  # Medical/Dental/Hospital Equipment Merchant Wholesalers
    "423610",  # Electrical Apparatus Merchant Wholesalers
    "423710",  # Hardware Merchant Wholesalers
    "423830",  # Industrial Machinery Merchant Wholesalers
    "423840",  # Industrial Supplies Merchant Wholesalers
    "423850",  # Service Establishment Equipment Merchant Wholesalers
    "423990",  # Other Durable Goods Merchant Wholesalers
    "424120",  # Stationery & Office Supplies Merchant Wholesalers
    "424130",  # Industrial & Personal Service Paper Merchant Wholesalers
    # Manufacturing (parts/equipment often procured as products)
    "333120",  # Construction Machinery Manufacturing
    "333318",  # Other Commercial and Service Industry Machinery
    "334111",  # Electronic Computer Manufacturing
    "334118",  # Computer Terminal and Other Computer Peripheral Equipment
    "335999",  # All Other Miscellaneous Electrical Equipment
    "337214",  # Office Furniture (except Wood) Manufacturing
    "339112",  # Surgical and Medical Instrument Manufacturing
    "339113",  # Surgical Appliance and Supplies Manufacturing
]

# PSC: letter-first codes are typically services (Sxxx, Rxxx, Zxxx, etc.).
# Digit-first codes are typically product commodity groups in SAM classificationCode.
# We do NOT invent a full PSC dictionary — only this structural rule + UNKNOWN.
_SERVICE_PSC_LETTER_PREFIXES = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ")


def psc_product_indicator(psc: str | None) -> dict[str, Any]:
    """
    Structural PSC hint only.
    Digit-leading → possible product commodity.
    Letter-leading → often service. Not a hard reject alone.
    """
    code = str(psc or "").strip().upper()
    if not code:
        return {"indicator": "UNKNOWN", "basis": "psc_missing", "psc": None}
    first = code[0]
    if first.isdigit():
        return {"indicator": "POSSIBLE_PRODUCT", "basis": "digit_leading_psc", "psc": code}
    if first in _SERVICE_PSC_LETTER_PREFIXES:
        return {"indicator": "LIKELY_SERVICE", "basis": "letter_leading_psc", "psc": code}
    return {"indicator": "UNKNOWN", "basis": "unrecognized_psc_shape", "psc": code}


def naics_product_indicator(naics: str | None) -> dict[str, Any]:
    code = str(naics or "").strip()
    if not code:
        return {"indicator": "UNKNOWN", "basis": "naics_missing", "naics": None}
    if any(code.startswith(p) for p in PRODUCT_NAICS_PREFIXES):
        return {"indicator": "PRODUCT_SECTOR", "basis": "naics_prefix_33_42_44_45", "naics": code}
    if code in PRODUCT_DISCOVERY_NAICS:
        return {"indicator": "PRODUCT_SECTOR", "basis": "product_discovery_allowlist", "naics": code}
    return {"indicator": "NON_PRODUCT_OR_UNKNOWN", "basis": "outside_product_naics_set", "naics": code}


_PRODUCT_TITLE_RE = re.compile(
    r"\b(equipment|supply|supplies|hardware|laptop|computer|monitor|server|parts?|"
    r"furniture|appliance|tools?|electronics|printer|tablet|generator|pump|"
    r"safety\s+equipment|industrial\s+supplies|chassis|router|switch)\b",
    re.I,
)
_SERVICE_TITLE_RE = re.compile(
    r"\b(services?|janitorial|custodial|groundskeeping|mowing|staffing|"
    r"maintenance\s+and\s+repair|consulting|management)\b",
    re.I,
)


def classify_product_discovery_hit(raw_or_opp: dict[str, Any]) -> dict[str, Any]:
    """Deterministic free filter for whether a SAM hit is a product candidate."""
    naics = raw_or_opp.get("naics_code") or raw_or_opp.get("naicsCode") or raw_or_opp.get("naics")
    psc = (
        raw_or_opp.get("psc_code")
        or raw_or_opp.get("classificationCode")
        or (raw_or_opp.get("sam_raw") or {}).get("classificationCode")
        if isinstance(raw_or_opp.get("sam_raw"), dict)
        else raw_or_opp.get("classificationCode")
    )
    title = str(raw_or_opp.get("title") or "")
    n = naics_product_indicator(str(naics) if naics else None)
    p = psc_product_indicator(str(psc) if psc else None)

    score = 0
    reasons: list[str] = []
    if n["indicator"] == "PRODUCT_SECTOR":
        score += 50
        reasons.append(n["basis"])
    if p["indicator"] == "POSSIBLE_PRODUCT":
        score += 30
        reasons.append(p["basis"])
    if p["indicator"] == "LIKELY_SERVICE":
        score -= 25
        reasons.append(p["basis"])
    if _PRODUCT_TITLE_RE.search(title):
        score += 15
        reasons.append("product_title_keyword")
    if _SERVICE_TITLE_RE.search(title) and not _PRODUCT_TITLE_RE.search(title):
        score -= 20
        reasons.append("service_title_keyword")

    # Use Stage 0 classifier on a lightweight namespace
    from types import SimpleNamespace

    opp = SimpleNamespace(
        notice_id=raw_or_opp.get("notice_id") or raw_or_opp.get("noticeId"),
        title=title,
        description=raw_or_opp.get("description"),
        naics_code=naics,
        sam_raw=raw_or_opp.get("sam_raw") or raw_or_opp,
        set_aside=raw_or_opp.get("set_aside") or raw_or_opp.get("typeOfSetAsideDescription"),
    )
    cls, conf = classify_opportunity(opp)
    fit = resolve_core_fit(stage0_classification=cls)
    if fit["core_fit"] == FIT_CORE_PRODUCT:
        score += 40
        reasons.append(f"stage0_{cls}")

    is_product_candidate = score >= 40 and fit["core_fit"] == FIT_CORE_PRODUCT
    # Also accept high structural product signal even if Stage 0 UNKNOWN
    if not is_product_candidate and score >= 60 and cls in {
        CLASS_PRODUCT_RESELL,
        CLASS_PRODUCT_PLUS_SERVICE,
        "UNKNOWN",
    }:
        is_product_candidate = n["indicator"] == "PRODUCT_SECTOR" or p["indicator"] == "POSSIBLE_PRODUCT"

    return {
        "is_product_candidate": bool(is_product_candidate),
        "score": score,
        "reasons": reasons,
        "stage0_classification": cls,
        "stage0_confidence": conf,
        "core_fit": fit["core_fit"],
        "naics_indicator": n,
        "psc_indicator": p,
    }


def build_product_sam_query(*, limit_per_naics: int = 50, naics_codes: list[str] | None = None) -> dict[str, Any]:
    codes = list(naics_codes or PRODUCT_DISCOVERY_NAICS)
    # Hard separation: never accept legacy facilities-service NAICS on product path
    try:
        from m3_procurement_profile import LEGACY_SERVICE_NAICS

        codes = [c for c in codes if str(c) not in LEGACY_SERVICE_NAICS]
    except Exception:
        pass
    if not codes:
        codes = list(PRODUCT_DISCOVERY_NAICS)
    return {
        "mode": "PRODUCT_DISCOVERY",
        "endpoint": "https://api.sam.gov/opportunities/v2/search",
        "naics_codes": codes,
        "limit_per_naics": limit_per_naics,
        "posted_window_days": 30,
        "active": "yes",
        "filters": {
            "set_aside": "total_small_business_or_small_business_existing_rule",
            "post_filter": "classify_product_discovery_hit.is_product_candidate",
        },
        "note": "Uses PRODUCT_DISCOVERY_NAICS only — never settings_store.get_naics_codes / legacy service NAICS",
    }


def preflight_product_sync(*, limit_per_naics: int = 50, max_naics: int = 3) -> dict[str, Any]:
    """Show what a live product sync would do — NO network."""
    key_present = bool((os.getenv("SAM_GOV_API_KEY") or "").strip())
    query = build_product_sam_query(limit_per_naics=limit_per_naics)
    codes = query["naics_codes"][:max_naics]
    return {
        "command": "product_discovery_preflight",
        "LIVE_API_REQUESTS": 0,
        "would_execute_live": False,
        "credentials_present": key_present,
        "sam_calls_if_executed": len(codes),
        "naics_batch": codes,
        "query": {**query, "naics_codes": codes},
        "expected_result_limit": len(codes) * limit_per_naics,
        "instructions": (
            "SAM NAICS discovery is scarce/late-stage only. Normal discovery should use "
            "non-SAM adapters (OpenAI/web, public procurement pages). "
            "Rare controlled SAM sync requires SAM_ALLOW_BROAD_DISCOVERY=true and "
            "authorize_broad_sam_discovery. "
            f"CLI: python scripts/product_beta_cli.py product-sync --authorize-live "
            f"--authorize-broad-sam-discovery --max-naics {max_naics} --limit-per-naics {limit_per_naics}"
        ),
        "sam_scarcity": True,
        "broad_discovery_default": False,
    }


def run_product_sync(
    *,
    authorize_live: bool = False,
    authorize_broad_sam_discovery: bool = False,
    limit_per_naics: int = 50,
    max_naics: int = 3,
    persist: bool = True,
) -> dict[str, Any]:
    """
    Fetch a small controlled set of REAL product solicitations from SAM.

    Requires authorize_live=True AND authorize_broad_sam_discovery=True
    (plus SAM_ALLOW_BROAD_DISCOVERY=true). Not the normal discovery path.
    """
    if not authorize_live:
        return {
            **preflight_product_sync(limit_per_naics=limit_per_naics, max_naics=max_naics),
            "executed": False,
            "error": "authorize_live_required",
        }

    from sam_scarcity import PURPOSE_BROAD_DISCOVERY, broad_sam_discovery_allowed, gate_sam_api_call

    if not broad_sam_discovery_allowed(authorize_broad_sam_discovery=authorize_broad_sam_discovery):
        return {
            **preflight_product_sync(limit_per_naics=limit_per_naics, max_naics=max_naics),
            "executed": False,
            "error": "broad_sam_discovery_blocked",
            "note": "SAM NAICS scanning is not routine discovery. Set SAM_ALLOW_BROAD_DISCOVERY=true "
            "and pass authorize_broad_sam_discovery only for rare controlled tests.",
            "LIVE_SAM_CALLS": 0,
        }

    gate = gate_sam_api_call(
        purpose=PURPOSE_BROAD_DISCOVERY,
        authorize_live=True,
        authorize_broad_sam_discovery=True,
        context={"requested_fact": "product_naics_discovery"},
    )
    if not gate["allowed"]:
        return {
            **preflight_product_sync(limit_per_naics=limit_per_naics, max_naics=max_naics),
            "executed": False,
            "error": "sam_scarcity_gate_blocked",
            "gate": gate,
            "LIVE_SAM_CALLS": 0,
        }

    from sam_client import fetch_naics_from_sam
    from sync import upsert_contracts

    query = build_product_sam_query(limit_per_naics=limit_per_naics)
    codes = query["naics_codes"][:max_naics]
    all_hits: list[dict[str, Any]] = []
    product_hits: list[dict[str, Any]] = []
    classifications: list[dict[str, Any]] = []
    sam_calls = 0

    for code in codes:
        batch = fetch_naics_from_sam(
            code,
            authorize_live=True,
            authorize_broad_sam_discovery=True,
            purpose=PURPOSE_BROAD_DISCOVERY,
        )
        sam_calls += 1
        for opp in batch[:limit_per_naics]:
            all_hits.append(opp)
            verdict = classify_product_discovery_hit(opp)
            classifications.append({"notice_id": opp.get("notice_id"), **verdict})
            if verdict["is_product_candidate"]:
                product_hits.append(opp)

    inserted = updated = 0
    if persist and product_hits:
        from database import SessionLocal

        session = SessionLocal()
        try:
            inserted, updated = upsert_contracts(session, product_hits)
            session.commit()
        finally:
            session.close()

    return {
        "executed": True,
        "authorize_live": True,
        "authorize_broad_sam_discovery": True,
        "LIVE_SAM_CALLS": sam_calls,
        "LIVE_OPENAI_CALLS": 0,
        "naics_searched": codes,
        "raw_hits": len(all_hits),
        "product_candidates": len(product_hits),
        "inserted": inserted,
        "updated": updated,
        "classifications_sample": classifications[:20],
        "notice_ids": [h.get("notice_id") for h in product_hits],
    }
