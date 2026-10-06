"""Materiality-first line research pipeline with exhaustion reason codes."""

from __future__ import annotations

import re
import time
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from line_basket_completion_strict_economics.cluster import resolve_category_hint, sellers_for_line
from line_basket_completion_strict_economics.models import (
    DISTRIBUTOR_QUOTE_REQUIRED,
    EXECUTION_BLOCKED,
    IDENTITY_UNRESOLVED,
    MIN_EXECUTABLE_PRICE,
    NO_COMPLIANT_SOURCE,
    NO_PUBLIC_PRICE,
    NON_MATERIAL,
    OEM_QUOTE_REQUIRED,
    P0,
    P1,
    P2,
    P3,
    PACK_UNRESOLVED,
    PRICED_EXECUTABLE,
    PUBLIC_CURRENT,
    PUBLIC_PRICE_UNAVAILABLE,
    QUOTE_REQUIRED,
    SUPPLIER_QUOTE_REQUIRED,
    TIME_BUDGET_EXHAUSTED,
    UOM_UNRESOLVED,
)
from line_basket_completion_strict_economics.provenance import classify_price_record

_INSTALL = re.compile(
    r"\b(furnish\s+and\s+install|install(?:ation)?|removal|grading|tilling|planting\s+soil|labor|site\s+work)\b",
    re.I,
)
_DEAD_DOMAINS = {"advancedtruckparts.com"}  # known sentinel/search pollution host for this corpus


def _money(v: Any) -> float | None:
    try:
        if v is None:
            return None
        return float(Decimal(str(v)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    except Exception:
        return None


def _apply_cached_valid(line: dict[str, Any]) -> dict[str, Any] | None:
    for src in (line.get("prior_funnel"), line.get("prior_acq"), line.get("cluster_price")):
        if not src:
            continue
        row = dict(src)
        if row.get("unit_cost") is None and row.get("unit_price") is not None:
            row["unit_cost"] = row.get("unit_price")
        audit = classify_price_record(row)
        if audit.get("is_valid_production"):
            out = dict(line)
            out["terminal_state"] = PRICED_EXECUTABLE
            out["acquisition_status"] = PRICED_EXECUTABLE
            out["unit_cost"] = audit["unit_cost"]
            out["extended_line_cost"] = _money(Decimal(str(audit["unit_cost"])) * Decimal(str(line.get("quantity") or 1)))
            out["seller"] = audit.get("seller") or row.get("seller")
            out["source_url"] = audit.get("source_url") or row.get("source_url")
            out["price_origin"] = audit.get("price_origin") or PUBLIC_CURRENT
            out["research_route"] = "CACHED_VALID_PRODUCTION"
            out["provenance_audit"] = audit
            return out
    return None


def _quote_subtype(line: dict[str, Any]) -> str:
    cat = line.get("category") or ""
    mfr = str(line.get("manufacturer") or "")
    if cat in {"CONSTRUCTION_INSTALL"} or _INSTALL.search(str(line.get("description") or "")):
        return OEM_QUOTE_REQUIRED
    if mfr and len(mfr) > 2 and line.get("mpn"):
        return DISTRIBUTOR_QUOTE_REQUIRED
    return SUPPLIER_QUOTE_REQUIRED


def research_line(
    line: dict[str, Any],
    *,
    deadline: float,
    stats: dict[str, Any],
    allow_live: bool,
    opportunity_potential: float,
) -> dict[str, Any]:
    out = dict(line)

    # Insurance / bonding boilerplate extracted as fake lines
    if line.get("non_procurement_noise") or (
        line.get("description")
        and re.search(
            r"\b(liability\s+coverage|insurance|workers?\s+comp|limits?\s+of\s+not\s+less)\b",
            str(line.get("description")),
            re.I,
        )
    ):
        out["terminal_state"] = NON_MATERIAL
        out["acquisition_status"] = NON_MATERIAL
        out["research_route"] = "NON_PROCUREMENT_NOISE"
        out["exhaustion_reason"] = "other"
        return out

    # De minimis non-material early exit for P3 without MPN
    if line.get("materiality_tier") == P3 and not line.get("mpn"):
        out["terminal_state"] = NON_MATERIAL
        out["acquisition_status"] = NON_MATERIAL
        out["research_route"] = "DE_MINIMIS"
        out["exhaustion_reason"] = "low_priority_stopped"
        return out

    cached = _apply_cached_valid(line)
    if cached:
        stats["cached_valid"] = int(stats.get("cached_valid") or 0) + 1
        return cached

    # Construction/install without commercial identity
    desc = str(line.get("description") or "")
    if (line.get("category") == "CONSTRUCTION_INSTALL" or _INSTALL.search(desc)) and not line.get("mpn"):
        # Material install → quote reserve if opportunity has potential; else identity unresolved
        if line.get("materiality_tier") in {P0, P1} and opportunity_potential >= 5000:
            subtype = _quote_subtype(line)
            out["terminal_state"] = QUOTE_REQUIRED
            out["acquisition_status"] = QUOTE_REQUIRED
            out["quote_subtype"] = subtype
            out["research_route"] = "INSTALL_QUOTE_PATH"
            out["exhaustion_reason"] = "no_public_price_install"
            return out
        out["terminal_state"] = IDENTITY_UNRESOLVED
        out["acquisition_status"] = IDENTITY_UNRESOLVED
        out["research_route"] = "INSTALL_NO_COMMERCIAL_IDENTITY"
        out["exhaustion_reason"] = "identity_unresolved"
        return out

    mpn = line.get("mpn")
    if not mpn or len(str(mpn)) < 3:
        out["terminal_state"] = IDENTITY_UNRESOLVED
        out["acquisition_status"] = IDENTITY_UNRESOLVED
        out["research_route"] = "NO_MPN"
        out["exhaustion_reason"] = "identity_unresolved"
        return out

    if time.time() > deadline:
        out["terminal_state"] = TIME_BUDGET_EXHAUSTED
        out["acquisition_status"] = TIME_BUDGET_EXHAUSTED
        out["research_route"] = "DEADLINE"
        out["exhaustion_reason"] = "time_budget_exhausted"
        return out

    if not allow_live:
        # Low priority stopped without live spend
        if line.get("materiality_tier") in {P2, P3}:
            out["terminal_state"] = NO_PUBLIC_PRICE
            out["acquisition_status"] = NO_PUBLIC_PRICE
            out["research_route"] = "LOW_PRIORITY_STOPPED"
            out["exhaustion_reason"] = "low_priority_stopped"
            return out
        out["terminal_state"] = TIME_BUDGET_EXHAUSTED
        out["acquisition_status"] = TIME_BUDGET_EXHAUSTED
        out["research_route"] = "LIVE_BUDGET"
        out["exhaustion_reason"] = "time_budget_exhausted"
        return out

    # Live accurate price via proven stack, category-routed
    try:
        from price_coverage_80.resolve import resolve_accurate_price

        preferred = [d for d in sellers_for_line(line) if d not in _DEAD_DOMAINS]
        item = {
            "benchmark_id": f"lbc-{line.get('line_id')}",
            "mpn": mpn,
            "manufacturer": line.get("manufacturer"),
            "description": line.get("description"),
            "expected_condition": line.get("condition") or "NEW",
            "expected_uom": line.get("uom") or "EA",
            "expected_pack": int(line.get("pack") or 1),
            "category": resolve_category_hint(line),
            "known_public_seller": preferred[0] if preferred else None,
        }
        found = resolve_accurate_price(
            item,
            use_budget=True,
            max_sellers=min(4, max(2, len(preferred) or 2)),
            max_queries=2,
            max_pages=4,
            stats=stats,
        )
        stats["live_attempts"] = int(stats.get("live_attempts") or 0) + 1
        price = found.get("price") or found.get("unit_price")
        url = str(found.get("source_url") or found.get("url") or "")
        seller = str(found.get("seller") or found.get("domain") or "")
        audit = classify_price_record(
            {
                "unit_cost": price,
                "source_url": url,
                "seller": seller,
                "price_origin": found.get("extraction_route") or "LIVE",
                "research_route": found.get("extraction_route"),
            }
        )
        if found.get("usable") and audit.get("is_valid_production"):
            out["terminal_state"] = PRICED_EXECUTABLE
            out["acquisition_status"] = PRICED_EXECUTABLE
            out["unit_cost"] = audit["unit_cost"]
            out["extended_line_cost"] = _money(Decimal(str(audit["unit_cost"])) * Decimal(str(line.get("quantity") or 1)))
            out["seller"] = seller
            out["source_url"] = url
            out["price_origin"] = audit.get("price_origin") or PUBLIC_CURRENT
            out["research_route"] = "LIVE_ACCURATE_PRICE"
            out["provenance_audit"] = audit
            stats["live_priced"] = int(stats.get("live_priced") or 0) + 1
            return out

        reason = str(found.get("reason") or found.get("status") or "").upper()
        if "QUOTE" in reason or "LOGIN" in reason:
            if line.get("materiality_tier") in {P0, P1} and opportunity_potential >= 1000:
                subtype = _quote_subtype(line)
                out["terminal_state"] = QUOTE_REQUIRED
                out["acquisition_status"] = QUOTE_REQUIRED
                out["quote_subtype"] = subtype
                out["research_route"] = "LIVE_QUOTE_SIGNAL"
                return out
        if "PACK" in reason:
            out["terminal_state"] = PACK_UNRESOLVED
            out["acquisition_status"] = PACK_UNRESOLVED
            out["exhaustion_reason"] = "pack_unresolved"
            return out
        if "UOM" in reason:
            out["terminal_state"] = UOM_UNRESOLVED
            out["acquisition_status"] = UOM_UNRESOLVED
            out["exhaustion_reason"] = "uom_unresolved"
            return out
        if "BLOCK" in reason or "BOT" in reason:
            out["terminal_state"] = NO_COMPLIANT_SOURCE
            out["acquisition_status"] = NO_COMPLIANT_SOURCE
            out["exhaustion_reason"] = "source_blocked"
            return out
        if "IDENTITY" in reason or "WRONG" in reason:
            out["terminal_state"] = IDENTITY_UNRESOLVED
            out["acquisition_status"] = IDENTITY_UNRESOLVED
            out["exhaustion_reason"] = "identity_unresolved"
            return out

        # Public research exhausted for material lines with usable identity → quote reserve
        if line.get("materiality_tier") in {P0, P1} and opportunity_potential >= 5000:
            subtype = _quote_subtype(line)
            out["terminal_state"] = QUOTE_REQUIRED
            out["acquisition_status"] = QUOTE_REQUIRED
            out["quote_subtype"] = subtype
            out["research_route"] = "PUBLIC_EXHAUSTED_QUOTE"
            out["exhaustion_reason"] = "no_public_price"
            return out

        out["terminal_state"] = NO_PUBLIC_PRICE
        out["acquisition_status"] = NO_PUBLIC_PRICE
        out["research_route"] = "LIVE_EXHAUSTED"
        out["exhaustion_reason"] = "true_market_exhaustion"
        out["failure_reason"] = found.get("reason") or found.get("status")
        return out
    except Exception as exc:
        out["terminal_state"] = NO_PUBLIC_PRICE
        out["acquisition_status"] = NO_PUBLIC_PRICE
        out["research_route"] = "EXCEPTION"
        out["exhaustion_reason"] = "other"
        out["failure_reason"] = str(exc)[:200]
        return out


def research_opportunity_lines(
    lines: list[dict[str, Any]],
    *,
    opportunity_id: str,
    revenue_hint: float,
    stats: dict[str, Any],
    deadline_s: float,
    cluster_prices: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    started = time.time()
    deadline = started + deadline_s
    cluster_prices = cluster_prices or {}

    # Attach cluster shared prices
    for ln in lines:
        cid = ln.get("cluster_id")
        if cid and cid in cluster_prices:
            ln["cluster_price"] = cluster_prices[cid]

    # Adaptive live budget by opportunity potential
    potential = float(revenue_hint or 0)
    if potential >= 50000:
        max_live = 18
    elif potential >= 10000:
        max_live = 12
    elif potential >= 5000:
        max_live = 8
    else:
        max_live = 4

    # Rank P0 then P1 then rest
    order = {P0: 0, P1: 1, P2: 2, P3: 3}

    def _rank(ln: dict[str, Any]) -> tuple:
        return (
            order.get(ln.get("materiality_tier") or P3, 9),
            -float(ln.get("materiality_score") or 0),
            0 if ln.get("mpn") else 1,
            str(ln.get("line_id") or ""),
        )

    ordered = sorted(lines, key=_rank)
    researched: list[dict[str, Any]] = []
    live_used = 0

    for ln in ordered:
        tier = ln.get("materiality_tier")
        allow_live = False
        if tier in {P0, P1} and live_used < max_live and time.time() < deadline:
            allow_live = True
        elif tier == P2 and live_used < max(2, max_live // 3) and potential >= 10000 and time.time() < deadline:
            allow_live = True

        result = research_line(
            ln,
            deadline=deadline,
            stats=stats,
            allow_live=allow_live,
            opportunity_potential=potential,
        )
        if result.get("research_route") == "LIVE_ACCURATE_PRICE" or (
            allow_live and result.get("research_route") not in {"CACHED_VALID_PRODUCTION", "DE_MINIMIS", "NO_MPN", "INSTALL_NO_COMMERCIAL_IDENTITY", "INSTALL_QUOTE_PATH"}
        ):
            if allow_live and result.get("research_route") not in {
                "CACHED_VALID_PRODUCTION",
                "DE_MINIMIS",
                "NO_MPN",
                "INSTALL_NO_COMMERCIAL_IDENTITY",
                "INSTALL_QUOTE_PATH",
                "LOW_PRIORITY_STOPPED",
            }:
                live_used += 1

        # Propagate cluster price
        if result.get("terminal_state") == PRICED_EXECUTABLE and result.get("cluster_id"):
            cluster_prices[result["cluster_id"]] = {
                "unit_cost": result.get("unit_cost"),
                "seller": result.get("seller"),
                "source_url": result.get("source_url"),
                "price_origin": result.get("price_origin"),
            }
        researched.append(result)

    return researched
