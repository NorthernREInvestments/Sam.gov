"""Production-valid price discovery for A–E identities only."""

from __future__ import annotations

import re
import time
from decimal import Decimal, ROUND_HALF_UP
from typing import Any
from urllib.parse import urlparse

from line_basket_completion_strict_economics.provenance import classify_price_record
from material_line_identity_price_recovery.models import (
    AUTHORIZED_DISTRIBUTOR_CURRENT,
    DISTRIBUTOR_QUOTE_REQUIRED,
    MANUFACTURER_CURRENT,
    MIN_EXECUTABLE_PRICE,
    OEM_QUOTE_REQUIRED,
    PUBLIC_CURRENT,
    SUPPLIER_QUOTE,
    SUPPLIER_QUOTE_REQUIRED,
    USABLE_IDENTITY,
)

_SEARCH_URL = re.compile(r"(?:/search(?:\?|/|$)|[?&](?:q|query|searchQuery|keywords|Ntt|searchterm)=)", re.I)
_AUTH_DIST = {"grainger.com", "zoro.com", "mscdirect.com", "supplyhouse.com", "globalindustrial.com", "1000bulbs.com"}
_DEAD = {"advancedtruckparts.com"}


def _money(v: Any) -> float | None:
    try:
        if v is None:
            return None
        return float(Decimal(str(v)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    except Exception:
        return None


def _origin_for(seller: str, url: str) -> str:
    host = (urlparse(url).hostname or seller or "").lower().replace("www.", "")
    if "manufacturer" in host or host.endswith((".com",)) and any(
        x in host for x in ("hunterindustries", "netafim", "cummins", "mfr", "oem")
    ):
        return MANUFACTURER_CURRENT
    if any(d in host for d in _AUTH_DIST):
        return AUTHORIZED_DISTRIBUTOR_CURRENT
    return PUBLIC_CURRENT


def _is_product_url(url: str) -> bool:
    if not url or _SEARCH_URL.search(url):
        return False
    path = (urlparse(url).path or "").lower()
    if "/search" in path:
        return False
    # Accept product-like paths or non-empty path that isn't a bare search
    return bool(path and path != "/")


def research_production_price(
    line: dict[str, Any],
    *,
    deadline: float,
    stats: dict[str, Any],
    allow_live: bool,
) -> dict[str, Any]:
    out = dict(line)
    conf = line.get("identity_confidence")
    if conf not in USABLE_IDENTITY:
        out["acquisition_state"] = "IDENTITY_INSUFFICIENT"
        out["exhaustion_reason"] = "identity_insufficient"
        return out

    if time.time() > deadline:
        out["acquisition_state"] = "TIME_BUDGET_EXHAUSTED"
        out["exhaustion_reason"] = "time_budget_exhausted"
        return out

    if not allow_live:
        # After live budget is spent on this opportunity, strong identities with
        # known commercial channels enter quote reserve (not false "public unavailable").
        if conf in USABLE_IDENTITY and (line.get("mpn") or line.get("part_number") or line.get("model") or line.get("manufacturer")):
            out["acquisition_state"] = "QUOTE_REQUIRED"
            out["quote_subtype"] = _quote_subtype(line)
            out["exhaustion_reason"] = "live_budget_exhausted_quote_reserve"
            return out
        out["acquisition_state"] = "TIME_BUDGET_EXHAUSTED"
        out["exhaustion_reason"] = "time_budget_exhausted"
        return out

    mpn = line.get("mpn") or line.get("part_number") or line.get("model")
    desc = line.get("description") or line.get("raw_solicitation_description")
    if not mpn and conf not in {"E_STRONG_GENERIC_SPEC", "D_PERMITTED_EQUAL_WITH_SALIENT_SPECS"}:
        out["acquisition_state"] = "IDENTITY_INSUFFICIENT"
        out["exhaustion_reason"] = "identity_insufficient"
        return out

    try:
        from price_coverage_80.resolve import resolve_accurate_price

        item = {
            "benchmark_id": f"mlr-{line.get('line_id')}",
            "mpn": mpn,
            "manufacturer": line.get("manufacturer"),
            "description": desc,
            "expected_condition": "NEW",
            "expected_uom": line.get("uom") or "EA",
            "expected_pack": int(line.get("pack") or 1),
            "category": _category_hint(line),
        }
        found = resolve_accurate_price(
            item,
            use_budget=True,
            max_sellers=3,
            max_queries=2,
            max_pages=4,
            stats=stats,
        )
        stats["live_attempts"] = int(stats.get("live_attempts") or 0) + 1

        price = found.get("price") or found.get("unit_price")
        url = str(found.get("source_url") or found.get("url") or "")
        seller = str(found.get("seller") or found.get("domain") or "")
        if seller.lower().replace("www.", "") in _DEAD:
            found = {**found, "usable": False, "reason": "dead_domain"}

        audit = classify_price_record(
            {
                "unit_cost": price,
                "source_url": url,
                "seller": seller,
                "price_origin": "LIVE",
                "research_route": found.get("extraction_route"),
            }
        )
        # Extra hard reject search URLs even if classify missed
        if not _is_product_url(url):
            audit = {**audit, "is_valid_production": False, "verdict": "OTHER_INVALID", "reasons": (audit.get("reasons") or []) + ["search_or_non_pdp"]}

        if found.get("usable") and audit.get("is_valid_production") and _money(price) and float(price) >= MIN_EXECUTABLE_PRICE:
            origin = _origin_for(seller, url)
            out["acquisition_state"] = "PRICED_EXECUTABLE"
            out["production_price"] = {
                "unit_cost": _money(price),
                "seller": seller,
                "source_url": url,
                "price_origin": origin,
                "condition": "NEW",
                "retrieved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "provenance_verdict": "VALID_PRODUCTION_PRICE",
            }
            out["price_origin"] = origin
            out["unit_cost"] = _money(price)
            out["seller"] = seller
            out["source_url"] = url
            stats["production_priced"] = int(stats.get("production_priced") or 0) + 1
            return out

        reason = str(found.get("reason") or found.get("status") or "").upper()
        routes = found.get("sellers_attempted") or found.get("domains_attempted") or []
        out["routes_attempted"] = routes
        if "QUOTE" in reason or "LOGIN" in reason:
            out["acquisition_state"] = "QUOTE_REQUIRED"
            out["quote_subtype"] = _quote_subtype(line)
            return out
        if "BLOCK" in reason or "BOT" in reason or "403" in reason:
            out["acquisition_state"] = "ACCESS_BLOCKED"
            out["exhaustion_reason"] = "access_blocked"
            return out
        if not routes and not found.get("usable"):
            out["acquisition_state"] = "NO_SELLER_FOUND"
            out["exhaustion_reason"] = "no_seller_found"
            return out

        # Public exhausted with strong identity → quote reserve
        out["acquisition_state"] = "QUOTE_REQUIRED"
        out["quote_subtype"] = _quote_subtype(line)
        out["exhaustion_reason"] = "true_public_unavailable"
        out["public_price_unavailable_after_routes"] = True
        return out
    except Exception as exc:
        out["acquisition_state"] = "NO_SELLER_FOUND"
        out["exhaustion_reason"] = "other"
        out["failure_reason"] = str(exc)[:200]
        return out


def _quote_subtype(line: dict[str, Any]) -> str:
    if line.get("manufacturer") and (line.get("mpn") or line.get("model")):
        return DISTRIBUTOR_QUOTE_REQUIRED
    if line.get("manufacturer"):
        return OEM_QUOTE_REQUIRED
    return SUPPLIER_QUOTE_REQUIRED


def _category_hint(line: dict[str, Any]) -> str:
    cat = str(line.get("category") or "").upper()
    mapping = {
        "LIGHTING": "lighting",
        "PLUMBING": "plumbing",
        "HVAC": "hvac",
        "PPE": "ppe",
        "TOOLS": "tools",
        "ELECTRICAL": "electrical",
        "AUTO_HD": "automotive_heavy",
        "IRRIGATION": "plumbing",
        "MRO": "mro",
        "OFFICE": "office",
        "FURNITURE": "furniture",
    }
    return mapping.get(cat, "mro")
