"""Multi-supplier public price discovery — extends existing resolver architecture.

Never stops at the first supplier price. Collects and ranks independent offers.
Does not invent inventory, discounts, freight, or payment terms.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from application_clock import now_utc

log = logging.getLogger("govtracker.multi_supplier_price_discovery_v1")

BUILD = "20261010-m3-multi-supplier-price-discovery-v1"
OUT_DIR = "_og_commercial_research_pipeline_out"
SUPPLIER_INTEL_FILE = "m3_supplier_intelligence_layer_v1.json"

EXACT_VERIFIED = "EXACT_VERIFIED"
APPROVED_EQUIVALENT_VERIFIED = "APPROVED_EQUIVALENT_VERIFIED"
POSSIBLE_MATCH_REVIEW_REQUIRED = "POSSIBLE_MATCH_REVIEW_REQUIRED"
NOT_MATCHING = "NOT_MATCHING"
QUOTE_REQUIRED = "QUOTE_REQUIRED"

# Bounded defaults — stop-loss against uncontrolled crawl
DEFAULT_MAX_QUERIES = 4
DEFAULT_MAX_PAGES = 8
DEFAULT_MIN_SUPPLIERS_TARGET = 3
DEFAULT_TIMEOUT_S = 12.0
PER_PRODUCT_WALL_S = 45.0


def _utc() -> str:
    return now_utc().isoformat()


def _canon() -> Path:
    return Path(__file__).resolve().parents[1] / "data"


def _out() -> Path:
    d = _canon() / OUT_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_supplier_intel() -> dict[str, Any]:
    p = _out() / SUPPLIER_INTEL_FILE
    if not p.exists():
        return {
            "kind": "SupplierIntelligenceLayer",
            "build": BUILD,
            "suppliers": {},
            "sku_index": {},
            "updated_at": _utc(),
        }
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {
            "kind": "SupplierIntelligenceLayer",
            "build": BUILD,
            "suppliers": {},
            "sku_index": {},
            "updated_at": _utc(),
        }


def _save_supplier_intel(doc: dict[str, Any]) -> Path:
    doc["updated_at"] = _utc()
    doc["build"] = BUILD
    p = _out() / SUPPLIER_INTEL_FILE
    p.write_text(json.dumps(doc, indent=2, default=str), encoding="utf-8")
    return p


def _match_class(candidate: dict[str, Any], identity: dict[str, Any] | None = None) -> str:
    if not candidate.get("unit_price"):
        return QUOTE_REQUIRED
    if candidate.get("exact_match") is False:
        return POSSIBLE_MATCH_REVIEW_REQUIRED
    via = str(candidate.get("via") or "")
    if "snippet" in via or candidate.get("snippet_only"):
        return POSSIBLE_MATCH_REVIEW_REQUIRED
    # If a catalog/config code was requested, require it in the URL/title path
    # (avoids selling a nearby Burks config as exact H18PG00).
    cat = str((identity or {}).get("catalog_number") or "").strip().upper()
    cat_compact = cat.replace("-", "")
    if cat_compact and len(cat_compact) >= 5:
        import re

        blob = (
            str(candidate.get("url") or "")
            + " "
            + str(candidate.get("title") or "")
            + " "
            + str(candidate.get("name") or "")
        ).upper()
        # Tokenize on non-alnum so burks-h18pg00-t310 keeps H18PG00 as its own token
        tokens = re.findall(r"[A-Z0-9]+", blob)
        matched = False
        for tok in tokens:
            if tok == cat_compact:
                matched = True
                break
            # Allow token that starts with catalog only when not a longer numeric extension
            if tok.startswith(cat_compact) and len(tok) > len(cat_compact):
                # H18PG0006 starts with H18PG00 + digit => reject this token
                if tok[len(cat_compact)].isdigit():
                    continue
        if not matched:
            return POSSIBLE_MATCH_REVIEW_REQUIRED
    return EXACT_VERIFIED


def _identity_key(identity: dict[str, Any]) -> str:
    parts = [
        str(identity.get("manufacturer") or identity.get("brand") or "").upper().strip(),
        str(
            identity.get("part_number")
            or identity.get("catalog_number")
            or identity.get("sku")
            or identity.get("model")
            or ""
        )
        .upper()
        .replace("-", "")
        .replace(" ", ""),
    ]
    return "|".join(p for p in parts if p)


def discover_multi_supplier_prices(
    identity: dict[str, Any],
    *,
    client: httpx.Client | None = None,
    max_queries: int = DEFAULT_MAX_QUERIES,
    max_pages: int = DEFAULT_MAX_PAGES,
    min_suppliers_target: int = DEFAULT_MIN_SUPPLIERS_TARGET,
    use_budget: bool = True,
    qty: float | None = None,
    update_intel: bool = True,
) -> dict[str, Any]:
    """Resolve public prices across multiple suppliers for one product identity.

    Uses existing ``resolve_public_price`` cascade with raised caps, then ranks
    all PRICE_OK candidates. Never hardcodes benchmark prices.
    """
    t0 = time.time()
    from public_price_search.resolver import resolve_public_price

    own = client is None
    c = client or httpx.Client(timeout=DEFAULT_TIMEOUT_S, follow_redirects=True)
    try:
        raw = resolve_public_price(
            identity,
            client=c,
            use_budget=use_budget,
            max_queries=max_queries,
            max_pages=max_pages,
        )
    finally:
        if own:
            c.close()

    candidates = list(raw.get("candidates") or [])
    # Also fold selected evidence if missing from candidates
    ev = raw.get("evidence") or {}
    if ev.get("unit_price") and ev.get("source_url"):
        if not any(
            str(x.get("url")) == str(ev.get("source_url"))
            and float(x.get("unit_price") or 0) == float(ev["unit_price"])
            for x in candidates
        ):
            candidates.append(
                {
                    "status": "PRICE_OK",
                    "url": ev.get("source_url"),
                    "unit_price": ev.get("unit_price"),
                    "seller": ev.get("seller"),
                    "seller_class": ev.get("seller_class"),
                    "condition": ev.get("condition"),
                    "via": ev.get("retrieved_via"),
                    "exact_match": ev.get("exact_match", True),
                    "shipping": ev.get("shipping"),
                }
            )

    offers: list[dict[str, Any]] = []
    seen: set[str] = set()
    for cand in candidates:
        if cand.get("status") != "PRICE_OK" or not cand.get("unit_price"):
            continue
        try:
            price = float(cand["unit_price"])
        except (TypeError, ValueError):
            continue
        if price <= 0 or price > 75000:
            continue
        seller = str(cand.get("seller") or cand.get("host") or "unknown")
        url = str(cand.get("url") or "")
        key = f"{seller.lower()}|{price:.2f}|{url[:80]}"
        if key in seen:
            continue
        seen.add(key)
        match_cls = _match_class(cand, identity)
        if match_cls == NOT_MATCHING:
            continue
        qty_f = float(qty) if qty and qty > 0 else None
        offers.append(
            {
                "SUPPLIER_NAME": seller,
                "PRODUCT_URL": url,
                "UNIT_PRICE": price,
                "REQUIRED_QTY": qty_f,
                "EXTENDED_PRODUCT_COST": round(price * qty_f, 2) if qty_f else None,
                "FREIGHT": cand.get("shipping") if isinstance(cand.get("shipping"), (int, float)) else None,
                "FREIGHT_STATUS": (
                    "VERIFIED"
                    if isinstance(cand.get("shipping"), (int, float))
                    else "UNKNOWN"
                ),
                "TAXES_FEES": None,
                "LEAD_TIME": None,
                "PAYMENT_TERMS": None,
                "PAYMENT_TERMS_STATUS": "UNKNOWN",
                "MATCH_CLASS": match_cls,
                "SELLER_CLASS": cand.get("seller_class"),
                "CONDITION": cand.get("condition"),
                "VIA": cand.get("via"),
                "VERIFIED_AT": _utc(),
                "LANDED_COST_KNOWN": price
                + (float(cand["shipping"]) if isinstance(cand.get("shipping"), (int, float)) else 0.0),
                "LANDED_COST_NOTE": (
                    "Unit price only; freight/taxes UNKNOWN unless verified on page"
                    if not isinstance(cand.get("shipping"), (int, float))
                    else "Unit + verified shipping component"
                ),
            }
        )

    def _rank_key(o: dict[str, Any]) -> tuple:
        url = str(o.get("PRODUCT_URL") or "").lower()
        via = str(o.get("VIA") or "")
        # Prefer PDP / structured over search-listing HTML scrapes
        searchish = 1 if any(x in url for x in ("/search", "?s=", "/collections/")) else 0
        structured = 0 if via == "structured_data" else 1
        return (searchish, structured, float(o["LANDED_COST_KNOWN"]))

    # Rank by landed known cost (unit + verified freight only), PDP-first
    exact = [o for o in offers if o["MATCH_CLASS"] == EXACT_VERIFIED]
    review = [o for o in offers if o["MATCH_CLASS"] == POSSIBLE_MATCH_REVIEW_REQUIRED]
    exact.sort(key=_rank_key)
    review.sort(key=_rank_key)
    ranked = exact + review

    best = ranked[0] if ranked else None
    wall = round(time.time() - t0, 2)

    result = {
        "kind": "MultiSupplierPriceDiscovery",
        "build": BUILD,
        "created_at": _utc(),
        "IDENTITY_KEY": _identity_key(identity),
        "IDENTITY": {
            "manufacturer": identity.get("manufacturer") or identity.get("brand"),
            "model": identity.get("model"),
            "part_number": identity.get("part_number"),
            "catalog_number": identity.get("catalog_number"),
            "sku": identity.get("sku"),
        },
        "RESOLVER_STATUS": raw.get("status"),
        "SUPPLIER_COUNT": len(ranked),
        "EXACT_VERIFIED_COUNT": len(exact),
        "TARGET_SUPPLIERS": min_suppliers_target,
        "MET_SUPPLIER_TARGET": len(exact) >= min_suppliers_target,
        "OFFERS": ranked[:8],
        "BEST_OFFER": best,
        "LOWEST_VERIFIED_UNIT_PRICE": best["UNIT_PRICE"] if best else None,
        "LOWEST_VERIFIED_SUPPLIER": best["SUPPLIER_NAME"] if best else None,
        "LOWEST_VERIFIED_URL": best["PRODUCT_URL"] if best else None,
        "WALL_CLOCK_S": wall,
        "SEARCH_TRACE_SUMMARY": {
            "queries": (raw.get("search_trace") or {}).get("queries_attempted"),
            "blocked": len((raw.get("search_trace") or {}).get("blocked_sources") or []),
            "prices_found": len((raw.get("search_trace") or {}).get("prices_found") or []),
        },
        "NOTE": (
            "Automated public listings only. Freight/terms UNKNOWN unless page-verified. "
            "Not executable quotes."
        ),
    }

    if update_intel and ranked:
        _record_intel(identity, ranked)

    return result


def _record_intel(identity: dict[str, Any], offers: list[dict[str, Any]]) -> None:
    """Cache supplier knowledge for reuse — prices stamped, never treated as current forever."""
    doc = _load_supplier_intel()
    suppliers = doc.setdefault("suppliers", {})
    sku_index = doc.setdefault("sku_index", {})
    ikey = _identity_key(identity)
    sku_index.setdefault(ikey, {"offers": [], "manufacturer": identity.get("manufacturer")})
    for o in offers[:5]:
        name = str(o.get("SUPPLIER_NAME") or "unknown")
        entry = suppliers.setdefault(
            name.lower(),
            {
                "name": name,
                "categories": [],
                "verified_skus": [],
                "prior_prices": [],
                "payment_terms": [],
                "authorization": None,
            },
        )
        entry["prior_prices"].append(
            {
                "identity_key": ikey,
                "unit_price": o.get("UNIT_PRICE"),
                "url": o.get("PRODUCT_URL"),
                "verified_at": o.get("VERIFIED_AT"),
                "match_class": o.get("MATCH_CLASS"),
                "STALE_UNLESS_REFRESHED": True,
            }
        )
        entry["prior_prices"] = entry["prior_prices"][-20:]
        if ikey and ikey not in entry["verified_skus"]:
            entry["verified_skus"].append(ikey)
            entry["verified_skus"] = entry["verified_skus"][-50:]
        sku_index[ikey]["offers"].append(
            {
                "supplier": name,
                "unit_price": o.get("UNIT_PRICE"),
                "url": o.get("PRODUCT_URL"),
                "verified_at": o.get("VERIFIED_AT"),
                "STALE_UNLESS_REFRESHED": True,
            }
        )
        sku_index[ikey]["offers"] = sku_index[ikey]["offers"][-15:]
    _save_supplier_intel(doc)


def lookup_retail_multi(
    ident: dict[str, Any],
    *,
    client: httpx.Client | None,
    allow_live: bool,
    cache: dict[str, Any],
    qty: float | None = None,
) -> dict[str, Any]:
    """Drop-in richer retail lookup returning multi-supplier offers.

    Compatible keys with ``lookup_retail`` plus SUPPLIER_OFFERS / SUPPLIER_COUNT.
    """
    # Reuse cache path from repair module for speed, then optionally expand live
    from buyer_intelligence.opengov_commercial_id_pricing_repair_v1 import (
        identity_strength,
        lookup_retail,
    )

    base = lookup_retail(ident, client=client, allow_live=False, cache=cache)
    if not allow_live:
        base["SUPPLIER_OFFERS"] = []
        base["SUPPLIER_COUNT"] = 1 if base.get("RETAIL_UNIT_PRICE") else 0
        return base

    strength = identity_strength(ident)
    if strength in {"NONE", "BRAND_ONLY"}:
        base["SUPPLIER_OFFERS"] = []
        base["SUPPLIER_COUNT"] = 0
        base["REASON"] = base.get("REASON") or "INSUFFICIENT_IDENTITY_FOR_PUBLIC_LOOKUP"
        return base

    multi = discover_multi_supplier_prices(
        {
            "manufacturer": ident.get("manufacturer"),
            "model": ident.get("model"),
            "part_number": ident.get("part_number"),
            "catalog_number": ident.get("catalog_number"),
            "sku": ident.get("sku"),
            "nsn": ident.get("nsn"),
            "raw_description": ident.get("raw_description"),
        },
        client=client,
        qty=qty,
        use_budget=True,
    )
    best = multi.get("BEST_OFFER")
    if best:
        return {
            "RETAIL_UNIT_PRICE": best["UNIT_PRICE"],
            "RETAIL_STATE": "PROVEN",
            "RETAIL_SELLER": best["SUPPLIER_NAME"],
            "RETAIL_URL": best["PRODUCT_URL"],
            "REASON": f"MULTI_SUPPLIER:{multi.get('RESOLVER_STATUS')}",
            "SUPPLIER_COUNT": multi.get("SUPPLIER_COUNT"),
            "EXACT_VERIFIED_COUNT": multi.get("EXACT_VERIFIED_COUNT"),
            "SUPPLIER_OFFERS": multi.get("OFFERS"),
            "MULTI_SUPPLIER": multi,
        }
    # Fall back to single-path live if multi found nothing but cache had a hit
    if base.get("RETAIL_UNIT_PRICE"):
        base["SUPPLIER_OFFERS"] = [
            {
                "SUPPLIER_NAME": base.get("RETAIL_SELLER"),
                "PRODUCT_URL": base.get("RETAIL_URL"),
                "UNIT_PRICE": base.get("RETAIL_UNIT_PRICE"),
                "MATCH_CLASS": EXACT_VERIFIED,
                "VIA": "COMMERCIAL_CACHE",
                "VERIFIED_AT": _utc(),
            }
        ]
        base["SUPPLIER_COUNT"] = 1
        return base
    live = lookup_retail(ident, client=client, allow_live=True, cache=cache)
    live["SUPPLIER_OFFERS"] = multi.get("OFFERS") or []
    live["SUPPLIER_COUNT"] = multi.get("SUPPLIER_COUNT") or (
        1 if live.get("RETAIL_UNIT_PRICE") else 0
    )
    live["MULTI_SUPPLIER"] = multi
    return live
