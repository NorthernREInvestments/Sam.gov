"""SupplierProfile persistence — durable supplier acquisition memory (L.20)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from application_clock import now_utc
from discovery.manufacturer_channels import (
    ACTUAL_SUPPLIER_QUOTE,
    CONTRACT_CATALOG_PRICE,
    KNOWN_SUPPLIER_FOR_PRODUCT,
    MANUFACTURER_LIST_PRICE,
    NO_ACQUISITION_EVIDENCE,
    PUBLIC_ACQUISITION_PRICE,
    QUOTE_REQUIRED,
    marketplace_blocked,
)
from phase_l.history_graphs import link_supplier
from phase_l.quality_audit import SUPPLIER_A, SUPPLIER_B, SUPPLIER_C, SUPPLIER_D, grade_supplier
from phase_l.quote_economics import SUPPLIER_MEMORY_PATH, load_json, save_json
from phase_l.quote_readiness import classify_supplier_authorization

BUILD = "20260928-m3-phase-l20-supplier-acquisition-evidence-recovery"
ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
PROFILES_PATH = DATA / "supplier_profiles.json"


def _utc() -> str:
    return now_utc().isoformat()


def supplier_key(name_or_domain: str | None) -> str:
    return re.sub(r"[^a-z0-9.]+", "", str(name_or_domain or "unknown").lower())[:80]


def empty_supplier_profile(supplier: str) -> dict[str, Any]:
    return {
        "kind": "SupplierProfile",
        "supplier": supplier,
        "supplier_id": supplier_key(supplier),
        "website": None,
        "manufacturer_relationships": [],
        "authorization": None,
        "exact_products": [],
        "public_pricing": False,
        "quote_required": True,
        "terms": "unknown",
        "freight": "unknown",
        "financing_compatibility": "unknown",
        "past_m3_usage": 0,
        "price_kind": NO_ACQUISITION_EVIDENCE,
        "grades_seen": [],
        "updated_at": _utc(),
    }


def _load() -> dict[str, Any]:
    if PROFILES_PATH.exists():
        try:
            return json.loads(PROFILES_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"kind": "SupplierProfileRegistry", "build": BUILD, "profiles": {}}


def _save(data: dict[str, Any]) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    data["build"] = BUILD
    data["updated_at"] = _utc()
    PROFILES_PATH.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def get_supplier_profile(supplier: str) -> dict[str, Any]:
    data = _load()
    key = supplier_key(supplier)
    return (data.get("profiles") or {}).get(key) or empty_supplier_profile(supplier)


def upsert_supplier_profile(candidate: dict[str, Any], *, commercial: dict[str, Any] | None = None) -> dict[str, Any]:
    commercial = commercial or {}
    domain = str(candidate.get("supplier_domain") or candidate.get("name") or "unknown")
    if marketplace_blocked(domain):
        candidate = {
            **candidate,
            "marketplace_blocked": True,
            "supplier_grade_cap": SUPPLIER_D,
            "note": "marketplace_guardrail_recon_only",
        }
    auth = classify_supplier_authorization(candidate)
    candidate["authorization_state"] = auth
    grade = grade_supplier(candidate, commercial=commercial)
    if candidate.get("marketplace_blocked"):
        grade = SUPPLIER_D
    candidate["supplier_grade"] = grade

    prof = get_supplier_profile(domain)
    prof["website"] = candidate.get("locator_url") or candidate.get("url") or prof.get("website")
    mfr = commercial.get("manufacturer") or candidate.get("manufacturer")
    if mfr and mfr not in (prof.get("manufacturer_relationships") or []):
        prof.setdefault("manufacturer_relationships", []).append(mfr)
    prof["authorization"] = auth
    model = commercial.get("model") or candidate.get("model")
    if model and model not in (prof.get("exact_products") or []):
        prof.setdefault("exact_products", []).append(str(model))
    pk = candidate.get("price_kind") or QUOTE_REQUIRED
    prof["price_kind"] = pk
    prof["public_pricing"] = pk in {PUBLIC_ACQUISITION_PRICE, CONTRACT_CATALOG_PRICE}
    prof["quote_required"] = pk in {QUOTE_REQUIRED, NO_ACQUISITION_EVIDENCE} or not prof["public_pricing"]
    if candidate.get("payment_terms"):
        prof["terms"] = candidate["payment_terms"]
    if candidate.get("freight_state"):
        prof["freight"] = candidate["freight_state"]
    if candidate.get("financing_compatibility"):
        prof["financing_compatibility"] = candidate["financing_compatibility"]
    prof["past_m3_usage"] = int(prof.get("past_m3_usage") or 0) + 1
    grades = list(prof.get("grades_seen") or [])
    if grade not in grades:
        grades.append(grade)
    prof["grades_seen"] = grades
    prof["updated_at"] = _utc()

    data = _load()
    data.setdefault("profiles", {})[prof["supplier_id"]] = prof
    _save(data)

    # Write-through product memory
    try:
        mem = load_json(SUPPLIER_MEMORY_PATH)
        by_prod = mem.setdefault("by_product", {})
        pk2 = str(model or commercial.get("mpn") or mfr or "unknown")[:80]
        entry = by_prod.setdefault(pk2, {"vendors": [], "sources": []})
        if domain not in entry["vendors"]:
            entry["vendors"].append(domain)
        entry["sources"].append({"domain": domain, "grade": grade, "auth": auth, "at": _utc()})
        mem["updated_at"] = _utc()
        save_json(SUPPLIER_MEMORY_PATH, mem)
    except Exception:
        pass

    if mfr:
        try:
            link_supplier(
                supplier=domain,
                manufacturer=str(mfr),
                product_family=str(commercial.get("product_family") or ""),
                exact_model=str(model or "") or None,
                authorization=str(auth),
                quote_capability=bool(prof.get("quote_required")),
            )
        except Exception:
            pass

    candidate["supplier_profile_id"] = prof["supplier_id"]
    return candidate


def known_supplier_for_product(commercial: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return persisted suppliers for exact product — KNOWN_SUPPLIER_FOR_PRODUCT."""
    commercial = commercial or {}
    model = str(commercial.get("model") or commercial.get("mpn") or "")
    mfr = str(commercial.get("manufacturer") or "")
    if not model and not mfr:
        return []
    mem = load_json(SUPPLIER_MEMORY_PATH)
    by_prod = mem.get("by_product") or {}
    hits = []
    for key, entry in by_prod.items():
        if model and model.lower() in key.lower() or (mfr and mfr.lower() in key.lower()):
            for v in entry.get("vendors") or []:
                hits.append(
                    {
                        "supplier_domain": v,
                        "name": v,
                        "source_type": "MEMORY",
                        "product_fit": "EXACT" if model else "FAMILY",
                        "exact_product_evidence": bool(model),
                        "known_signal": KNOWN_SUPPLIER_FOR_PRODUCT,
                        "price_kind": QUOTE_REQUIRED,
                    }
                )
    return hits[:6]


def classify_price_kind(candidate: dict[str, Any]) -> str:
    explicit = str(candidate.get("price_kind") or "").upper()
    mapping = {
        "ACTUAL_SUPPLIER_QUOTE": ACTUAL_SUPPLIER_QUOTE,
        "PUBLIC_ACQUISITION_PRICE": PUBLIC_ACQUISITION_PRICE,
        "CONTRACT_CATALOG_PRICE": CONTRACT_CATALOG_PRICE,
        "MANUFACTURER_LIST_PRICE": MANUFACTURER_LIST_PRICE,
        "QUOTE_REQUIRED": QUOTE_REQUIRED,
        "NO_ACQUISITION_EVIDENCE": NO_ACQUISITION_EVIDENCE,
    }
    if explicit in mapping:
        return mapping[explicit]
    if candidate.get("actual_quote") or candidate.get("written_quote"):
        return ACTUAL_SUPPLIER_QUOTE
    if candidate.get("verified_public_price") or candidate.get("unit_price"):
        st = str(candidate.get("source_type") or "").upper()
        if "COOPERATIVE" in st or "CONTRACT" in st:
            return CONTRACT_CATALOG_PRICE
        return PUBLIC_ACQUISITION_PRICE
    if str(candidate.get("source_type") or "").upper() == "OEM" and candidate.get("list_price"):
        return MANUFACTURER_LIST_PRICE
    if candidate.get("supplier_domain") or candidate.get("name"):
        return QUOTE_REQUIRED
    return NO_ACQUISITION_EVIDENCE
