"""LIVE_PRICEABILITY_SCORE + seller demotion memory."""

from __future__ import annotations

import json
from typing import Any

from application_clock import now_utc
from live_exact_priced_pdp.models import (
    ACCESS_BLOCKED_DOMAIN,
    DEMOTIONS,
    IDENTITY_ONLY,
    LIVE_EXACT_PDP_ACCESS_BLOCKED,
    LIVE_EXACT_PDP_LOGIN_REQUIRED,
    LIVE_EXACT_PDP_NO_PUBLIC_PRICE,
    LIVE_EXACT_PDP_QUOTE_ONLY,
    LIVE_EXACT_PDP_PRICE_HIDDEN,
    LIVE_EXACT_PRICED_PDP,
    NON_PRICEABLE,
    QUOTE_ONLY_DOMAIN,
    SELLER_SCORE,
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


def note_seller_outcome(domain: str, *, state: str, extractable: bool, priced: bool) -> None:
    domain = seller_of(domain) if "://" in domain else (domain or "").replace("www.", "")
    if not domain:
        return
    db = _load(SELLER_SCORE) or {"sellers": {}, "build": "20261005-m3-live-exact-priced-pdp-v1"}
    row = db.setdefault("sellers", {}).setdefault(
        domain,
        {
            "domain": domain,
            "candidates": 0,
            "live_exact": 0,
            "public_offers": 0,
            "prices": 0,
            "blocks": 0,
            "no_offer": 0,
            "quote_only": 0,
            "last_verified": None,
        },
    )
    row["candidates"] = int(row.get("candidates") or 0) + 1
    if state in {LIVE_EXACT_PRICED_PDP, LIVE_EXACT_PDP_PRICE_HIDDEN, LIVE_EXACT_PDP_NO_PUBLIC_PRICE, LIVE_EXACT_PDP_QUOTE_ONLY}:
        row["live_exact"] = int(row.get("live_exact") or 0) + 1
    if extractable or state in {LIVE_EXACT_PRICED_PDP, LIVE_EXACT_PDP_PRICE_HIDDEN}:
        row["public_offers"] = int(row.get("public_offers") or 0) + 1
    if priced:
        row["prices"] = int(row.get("prices") or 0) + 1
    if state == LIVE_EXACT_PDP_ACCESS_BLOCKED:
        row["blocks"] = int(row.get("blocks") or 0) + 1
    if state == LIVE_EXACT_PDP_NO_PUBLIC_PRICE:
        row["no_offer"] = int(row.get("no_offer") or 0) + 1
    if state in {LIVE_EXACT_PDP_QUOTE_ONLY, LIVE_EXACT_PDP_LOGIN_REQUIRED}:
        row["quote_only"] = int(row.get("quote_only") or 0) + 1
    row["priceability_score"] = _score(row)
    row["last_verified"] = now_utc().isoformat()
    _maybe_demote(domain, row)
    _save(SELLER_SCORE, db)


def _score(row: dict[str, Any]) -> float:
    cand = max(1, int(row.get("candidates") or 0))
    live = int(row.get("live_exact") or 0) / cand
    offers = int(row.get("public_offers") or 0) / cand
    prices = int(row.get("prices") or 0) / cand
    blocks = int(row.get("blocks") or 0) / cand
    return round(100.0 * (0.25 * live + 0.35 * offers + 0.40 * prices - 0.30 * blocks), 1)


def _maybe_demote(domain: str, row: dict[str, Any]) -> None:
    cand = int(row.get("candidates") or 0)
    if cand < 3:
        return
    dem = _load(DEMOTIONS) or {"domains": {}, "build": "20261005-m3-live-exact-priced-pdp-v1"}
    no_offer = int(row.get("no_offer") or 0)
    quote = int(row.get("quote_only") or 0)
    blocks = int(row.get("blocks") or 0)
    prices = int(row.get("prices") or 0)
    live = int(row.get("live_exact") or 0)
    klass = None
    if prices == 0 and blocks >= max(2, cand // 2):
        klass = ACCESS_BLOCKED_DOMAIN
    elif prices == 0 and quote >= 2 and live >= 2:
        klass = QUOTE_ONLY_DOMAIN
    elif prices == 0 and no_offer >= 2 and live >= 2:
        klass = IDENTITY_ONLY
    elif prices == 0 and live == 0 and cand >= 4:
        klass = NON_PRICEABLE
    if klass:
        dem.setdefault("domains", {})[domain] = {
            "domain": domain,
            "class": klass,
            "reason": f"cand={cand} live={live} prices={prices} no_offer={no_offer} quote={quote} blocks={blocks}",
            "last_verified": now_utc().isoformat(),
        }
        _save(DEMOTIONS, dem)


def is_demoted(domain: str) -> bool:
    dem = _load(DEMOTIONS)
    row = (dem.get("domains") or {}).get((domain or "").replace("www.", ""))
    return bool(row and row.get("class") in {IDENTITY_ONLY, QUOTE_ONLY_DOMAIN, NON_PRICEABLE, ACCESS_BLOCKED_DOMAIN})


def demotion_snapshot() -> dict[str, list[str]]:
    dem = _load(DEMOTIONS)
    out = {
        IDENTITY_ONLY: [],
        QUOTE_ONLY_DOMAIN: [],
        NON_PRICEABLE: [],
        ACCESS_BLOCKED_DOMAIN: [],
    }
    for dom, row in (dem.get("domains") or {}).items():
        klass = row.get("class")
        if klass in out:
            out[klass].append(dom)
    return out


def top_priceable_domains(n: int = 25) -> list[dict[str, Any]]:
    db = _load(SELLER_SCORE)
    rows = list((db.get("sellers") or {}).values())
    rows.sort(key=lambda r: (-float(r.get("priceability_score") or 0), -int(r.get("prices") or 0)))
    return rows[:n]
