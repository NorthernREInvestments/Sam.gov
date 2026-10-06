"""Domain priceability filter + open priceable seller corpus."""

from __future__ import annotations

import json
from typing import Any

from application_clock import now_utc
from live_seller_benchmark_reverify.models import (
    ACCESS_BLOCKED,
    BUILD,
    DOMAIN_CLASS,
    IDENTITY_ONLY,
    LOW_YIELD,
    OPEN_SELLER_CORPUS,
    PRICEABLE,
    QUOTE_ONLY_DOMAIN,
)
from m3_data_root import data_path
from price_adapters.validate import seller_of


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


def note_domain(
    domain: str,
    *,
    state: str,
    extractable: bool,
    priced: bool,
    category: str | None = None,
    manufacturer: str | None = None,
    offer_format: str | None = None,
    extraction_method: str | None = None,
    search_route: str | None = None,
) -> None:
    domain = seller_of(domain) if "://" in (domain or "") else (domain or "").replace("www.", "")
    if not domain:
        return
    db = _load(DOMAIN_CLASS) or {"build": BUILD, "domains": {}}
    row = db.setdefault("domains", {}).setdefault(
        domain,
        {
            "domain": domain,
            "candidates": 0,
            "live_exact": 0,
            "exact_match": 0,
            "public_offers": 0,
            "prices": 0,
            "blocks": 0,
            "quote_only": 0,
            "no_offer": 0,
            "wrong": 0,
            "categories": [],
            "manufacturers": [],
            "search_routes": [],
            "offer_formats": [],
            "extraction_methods": [],
            "class": LOW_YIELD,
            "success_rate": 0.0,
            "last_verified": None,
        },
    )
    row["candidates"] = int(row.get("candidates") or 0) + 1
    st = state or ""
    if st in {
        "LIVE_EXACT_PRICED_PDP",
        "LIVE_EXACT_PDP_PRICE_HIDDEN",
        "LIVE_EXACT_PDP_NO_PUBLIC_PRICE",
        "LIVE_EXACT_PDP_QUOTE_ONLY",
    }:
        row["live_exact"] = int(row.get("live_exact") or 0) + 1
    if st in {"LIVE_EXACT_PRICED_PDP", "LIVE_EXACT_PDP_PRICE_HIDDEN", "LIVE_EXACT_PDP_NO_PUBLIC_PRICE", "LIVE_EXACT_PDP_QUOTE_ONLY"}:
        row["exact_match"] = int(row.get("exact_match") or 0) + 1
    if extractable or st in {"LIVE_EXACT_PRICED_PDP", "LIVE_EXACT_PDP_PRICE_HIDDEN"}:
        row["public_offers"] = int(row.get("public_offers") or 0) + 1
    if priced:
        row["prices"] = int(row.get("prices") or 0) + 1
    if st == "LIVE_EXACT_PDP_ACCESS_BLOCKED":
        row["blocks"] = int(row.get("blocks") or 0) + 1
    if st in {"LIVE_EXACT_PDP_QUOTE_ONLY", "LIVE_EXACT_PDP_LOGIN_REQUIRED"}:
        row["quote_only"] = int(row.get("quote_only") or 0) + 1
    if st == "LIVE_EXACT_PDP_NO_PUBLIC_PRICE":
        row["no_offer"] = int(row.get("no_offer") or 0) + 1
    if st in {"WRONG_MPN", "WRONG_PRODUCT", "WRONG_MANUFACTURER"}:
        row["wrong"] = int(row.get("wrong") or 0) + 1

    def _uniq_add(key: str, val: str | None) -> None:
        if not val:
            return
        lst = list(row.get(key) or [])
        if val not in lst:
            lst.append(val)
            row[key] = lst[:20]

    _uniq_add("categories", category)
    _uniq_add("manufacturers", manufacturer)
    _uniq_add("search_routes", search_route)
    _uniq_add("offer_formats", offer_format)
    _uniq_add("extraction_methods", extraction_method)

    cand = max(1, int(row["candidates"]))
    row["success_rate"] = round(int(row["prices"]) / cand, 4)
    row["class"] = _classify(row)
    row["last_verified"] = now_utc().isoformat()
    _save(DOMAIN_CLASS, db)
    _sync_open_corpus(domain, row)


def _classify(row: dict[str, Any]) -> str:
    cand = int(row.get("candidates") or 0)
    prices = int(row.get("prices") or 0)
    offers = int(row.get("public_offers") or 0)
    live = int(row.get("live_exact") or 0)
    blocks = int(row.get("blocks") or 0)
    quote = int(row.get("quote_only") or 0)
    no_offer = int(row.get("no_offer") or 0)
    if cand < 2:
        return LOW_YIELD
    if prices >= 1 and offers >= 1:
        return PRICEABLE
    if blocks >= max(2, cand // 2) and prices == 0:
        return ACCESS_BLOCKED
    if quote >= 2 and live >= 1 and prices == 0:
        return QUOTE_ONLY_DOMAIN
    if live >= 2 and offers == 0 and prices == 0:
        return IDENTITY_ONLY
    if no_offer >= 2 and live >= 2 and prices == 0:
        return IDENTITY_ONLY
    if prices == 0 and live == 0 and cand >= 3:
        return LOW_YIELD
    return LOW_YIELD


def _sync_open_corpus(domain: str, row: dict[str, Any]) -> None:
    if row.get("class") != PRICEABLE:
        return
    db = _load(OPEN_SELLER_CORPUS) or {"name": "OPEN_PRICEABLE_SELLER_CORPUS_V1", "build": BUILD, "sellers": {}}
    db.setdefault("sellers", {})[domain] = {
        "domain": domain,
        "category": (row.get("categories") or [None])[0],
        "categories": row.get("categories") or [],
        "manufacturers_carried": row.get("manufacturers") or [],
        "search_route": (row.get("search_routes") or ["internal_or_serp"])[0],
        "exact_pdp_pattern": "validated_live_exact",
        "offer_format": (row.get("offer_formats") or ["structured_or_visible"])[0],
        "price_extraction_method": (row.get("extraction_methods") or ["json_ld_static_browser"])[0],
        "success_rate": row.get("success_rate"),
        "prices": row.get("prices"),
        "public_offers": row.get("public_offers"),
        "exact_pdps": row.get("live_exact"),
        "last_verified": row.get("last_verified"),
    }
    _save(OPEN_SELLER_CORPUS, db)


def is_demoted(domain: str) -> bool:
    d = (domain or "").replace("www.", "")
    db = _load(DOMAIN_CLASS)
    row = (db.get("domains") or {}).get(d) or {}
    return row.get("class") in {ACCESS_BLOCKED, IDENTITY_ONLY, QUOTE_ONLY_DOMAIN}


def domain_snapshot() -> dict[str, list[str]]:
    db = _load(DOMAIN_CLASS)
    out = {
        PRICEABLE: [],
        IDENTITY_ONLY: [],
        QUOTE_ONLY_DOMAIN: [],
        ACCESS_BLOCKED: [],
        LOW_YIELD: [],
    }
    for d, row in (db.get("domains") or {}).items():
        klass = row.get("class") or LOW_YIELD
        out.setdefault(klass, []).append(d)
    return out


def top_priceable_domains(limit: int = 30) -> list[dict[str, Any]]:
    db = _load(DOMAIN_CLASS)
    rows = list((db.get("domains") or {}).values())
    rows.sort(
        key=lambda r: (
            -int(r.get("prices") or 0),
            -int(r.get("public_offers") or 0),
            -float(r.get("success_rate") or 0),
            -int(r.get("candidates") or 0),
        )
    )
    return rows[:limit]


def seed_prior_demotions() -> None:
    """Import ACCESS_BLOCKED / NON_PRICEABLE from prior LEP demotions as starting filter."""
    from live_seller_benchmark_reverify.models import PRIOR_DEMOTIONS

    prior = _load(PRIOR_DEMOTIONS)
    db = _load(DOMAIN_CLASS) or {"build": BUILD, "domains": {}}
    for d, row in (prior.get("domains") or {}).items():
        klass = row.get("class")
        mapped = {
            "ACCESS_BLOCKED": ACCESS_BLOCKED,
            "NON_PRICEABLE": LOW_YIELD,
            "IDENTITY_ONLY": IDENTITY_ONLY,
            "QUOTE_ONLY": QUOTE_ONLY_DOMAIN,
        }.get(klass)
        if not mapped:
            continue
        cur = db.setdefault("domains", {}).setdefault(
            d,
            {
                "domain": d,
                "candidates": 0,
                "live_exact": 0,
                "exact_match": 0,
                "public_offers": 0,
                "prices": 0,
                "blocks": 0,
                "quote_only": 0,
                "no_offer": 0,
                "wrong": 0,
                "categories": [],
                "manufacturers": [],
                "search_routes": [],
                "offer_formats": [],
                "extraction_methods": [],
                "class": mapped,
                "success_rate": 0.0,
                "last_verified": None,
                "seeded_from": "lep_demotions",
            },
        )
        if int(cur.get("candidates") or 0) == 0:
            cur["class"] = mapped
            cur["seeded_from"] = "lep_demotions"
    _save(DOMAIN_CLASS, db)
