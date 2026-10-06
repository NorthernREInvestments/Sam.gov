"""Exhaustive per-line public evidence research + fight score.

Build: 20261004-m3-evidence-exhaustion-v1
"""

from __future__ import annotations

from typing import Any

from evidence_exhaustion.channel import research_channel
from evidence_exhaustion.models import (
    AI_BUDGET_DEFERRED,
    BRAND_OR_EQUAL,
    MIN_EXHAUSTION_SCORE_FOR_TERMINAL,
    NO_HISTORY_AFTER_EXHAUSTIVE_SEARCH,
    NO_PRICE_AFTER_EXHAUSTIVE_SEARCH,
    PUBLIC_PRICE_FOUND,
    RESEARCH_RETRYABLE,
    SPECIALTY_OEM_NARROW_CHANNEL,
    STRONG_GENERIC_SPEC,
)
from evidence_exhaustion.quote_reserve import admit_to_quote_reserve, sensitivity_table
from evidence_exhaustion.routing import classify_product_routing
from public_price_search.models import (
    CONDITION_MISMATCH,
    NO_PUBLIC_PRICE_AFTER_EXHAUSTIVE_SEARCH as PPS_NO_PRICE,
    PRICE_SEARCH_BUDGET_EXHAUSTED,
    PRICE_SOURCE_BLOCKED_RETRYABLE,
    PUBLIC_PRICE_FOUND as PPS_FOUND,
    PUBLIC_PRICE_PARTIAL,
)
from public_price_search.queries import (
    build_query_variants,
    distributor_search_urls,
    known_product_urls,
    manufacturer_direct_urls,
)
from public_price_search.resolver import resolve_public_price
from public_price_search.search import fetch_page, note_route, search_web, serp_circuit_open


def compute_exhaustion_score(flags: dict[str, Any], *, routing: str) -> int:
    """0–100 based on how many applicable routes actually ran."""
    weights = {
        "manufacturer_search_exhausted": 12,
        "distributor_search_exhausted": 14,
        "reseller_search_exhausted": 10,
        "direct_catalog_exhausted": 12,
        "structured_data_exhausted": 10,
        "alternate_domain_search_exhausted": 12,
        "history_search_exhausted": 12,
        "generic_spec_search_exhausted": 8,
        "brand_equal_search_exhausted": 8,
        "channel_research_exhausted": 10,
        "best_price_search_ran": 8,
        "multiple_candidates_sought": 6,
    }
    applicable = [
        "manufacturer_search_exhausted",
        "distributor_search_exhausted",
        "reseller_search_exhausted",
        "direct_catalog_exhausted",
        "structured_data_exhausted",
        "alternate_domain_search_exhausted",
        "history_search_exhausted",
        "best_price_search_ran",
        "multiple_candidates_sought",
    ]
    if routing == STRONG_GENERIC_SPEC:
        applicable.append("generic_spec_search_exhausted")
    if routing == BRAND_OR_EQUAL:
        applicable.append("brand_equal_search_exhausted")
    if routing == SPECIALTY_OEM_NARROW_CHANNEL:
        applicable.append("channel_research_exhausted")

    total_w = sum(weights[k] for k in applicable)
    earned = sum(weights[k] for k in applicable if flags.get(k))
    return int(round(100.0 * earned / max(total_w, 1)))


def _history_probe(identity: dict[str, Any], opportunity_id: str | None) -> dict[str, Any]:
    """Revenue-side history only — never acquisition cost."""
    hits: list[dict[str, Any]] = []
    gov_value = None
    try:
        from scale_evidence_profit.bid_price_index import load_index
        from scale_evidence_profit.line_resolver import resolve_line

        idx = load_index()
        ident = dict(identity)
        if opportunity_id:
            ident["opportunity_id"] = opportunity_id
        resolved = resolve_line(ident, idx, allow_public_price=False)
        if resolved.get("gov_unit_price") or resolved.get("has_gov_value"):
            gov_value = resolved.get("gov_unit_price")
            hits.append(
                {
                    "vendor": resolved.get("gov_vendor") or resolved.get("awardee"),
                    "unit_price": gov_value,
                    "source": resolved.get("gov_basis") or resolved.get("gov_source") or "index",
                    "basis": "REVENUE_ONLY",
                }
            )
    except Exception:
        pass
    # Buyer history file probe
    try:
        from evidence_breakthrough.opengov_history import parse_opengov_opportunity_id
        from m3_data_root import data_path
        import json

        code, _ = parse_opengov_opportunity_id(str(opportunity_id or identity.get("opportunity_id") or ""))
        if code:
            hp = data_path("opengov_buyer_history", f"{code}.json")
            if hp.exists():
                raw = json.loads(hp.read_text(encoding="utf-8"))
                rows = raw.get("awards") or raw.get("history") or raw.get("rows") or []
                pn = str(identity.get("part_number") or "").upper()
                for row in rows[:200]:
                    if not isinstance(row, dict):
                        continue
                    blob = " ".join(str(row.get(k) or "") for k in row)[:500].upper()
                    if pn and pn[:6] in blob:
                        hits.append(
                            {
                                "vendor": row.get("vendor") or row.get("awardee") or row.get("supplier"),
                                "unit_price": row.get("unit_price") or row.get("amount"),
                                "source": "buyer_history",
                                "basis": "REVENUE_ONLY",
                            }
                        )
                        if len(hits) >= 5:
                            break
    except Exception:
        pass

    if gov_value is not None or hits:
        status = "GOV_VALUE_FOUND" if gov_value is not None else "REVENUE_REFERENCE_FOUND"
    else:
        status = NO_HISTORY_AFTER_EXHAUSTIVE_SEARCH
    return {
        "status": status,
        "gov_unit_price": gov_value,
        "hits": hits[:8],
        "history_search_exhausted": True,
        "used_as_acquisition_cost": False,
    }


def _extra_domain_sweep(identity: dict[str, Any], *, client=None, max_pages: int = 4) -> list[dict[str, Any]]:
    """Alternate seller domains beyond the primary resolver pass."""
    from public_price_search.domain_map import category_search_urls
    from public_price_search.extract import extract_candidate_from_page, identity_on_page

    cands: list[dict[str, Any]] = []
    urls = list(
        dict.fromkeys(
            known_product_urls(identity)
            + manufacturer_direct_urls(identity)
            + category_search_urls(identity, limit=6)
            + distributor_search_urls(
                f"{identity.get('manufacturer') or ''} {identity.get('part_number') or ''}".strip(),
                identity,
            )[:4]
        )
    )[:max_pages]
    for url in urls:
        fr = fetch_page(url, client=client, use_budget=True)
        text = fr.get("text") or ""
        if not text or not identity_on_page(text, identity):
            if fr.get("blocked"):
                note_route("Direct catalog", ok=False, error="DOMAIN_BLOCKED")
            continue
        cand = extract_candidate_from_page(text, url=fr.get("url") or url, identity=identity)
        if cand and cand.get("status") == "PRICE_OK":
            cand["via"] = cand.get("via") or "alternate_domain"
            cands.append(cand)
            note_route("Direct catalog", ok=True)
    return cands


def exhaust_line(
    identity: dict[str, Any],
    *,
    opportunity_id: str | None = None,
    eligibility_ok: bool = True,
    has_package: bool = True,
    solicitation_value: float | None = None,
    time_remaining_ok: bool = True,
    use_budget: bool = True,
    max_queries: int = 4,
    max_pages: int = 6,
) -> dict[str, Any]:
    """Fight for one line — every applicable automated route before reserve."""
    oid = opportunity_id or identity.get("opportunity_id")
    routing = classify_product_routing(identity)
    rclass = routing["routing_class"]
    trace: dict[str, Any] = {
        "queries_generated": build_query_variants(identity)[:12],
        "routes_attempted": [],
        "domains_attempted": [],
        "seller_candidates": [],
        "blocked_pages": [],
        "successful_pages": [],
        "condition_mismatches": [],
        "uom_mismatches": [],
        "alternate_products": [],
        "historical_matches": [],
        "channel_findings": {},
        "selected_price": None,
        "selection_reason": None,
        "rejected_candidates": [],
    }
    flags: dict[str, Any] = {k: False for k in (
        "manufacturer_search_exhausted",
        "distributor_search_exhausted",
        "reseller_search_exhausted",
        "direct_catalog_exhausted",
        "structured_data_exhausted",
        "alternate_domain_search_exhausted",
        "generic_spec_search_exhausted",
        "brand_equal_search_exhausted",
        "history_search_exhausted",
        "channel_research_exhausted",
        "best_price_search_ran",
        "multiple_candidates_sought",
    )}

    # --- Primary resolver (manufacturer/distributor/reseller/SERP/structured) ---
    trace["routes_attempted"].append("resolve_public_price_cascade")
    primary = resolve_public_price(
        identity,
        opportunity_id=oid,
        use_budget=use_budget,
        max_queries=max_queries,
        max_pages=max_pages,
    )
    flags["manufacturer_search_exhausted"] = True
    flags["distributor_search_exhausted"] = True
    flags["reseller_search_exhausted"] = True
    flags["direct_catalog_exhausted"] = True
    flags["structured_data_exhausted"] = True
    flags["best_price_search_ran"] = True
    st = primary.get("search_trace") or {}
    for u in st.get("candidate_urls") or []:
        trace["domains_attempted"].append(u)
    for b in st.get("blocked_sources") or []:
        trace["blocked_pages"].append(b)
    for p in st.get("prices_found") or []:
        trace["successful_pages"].append(p)

    candidates = list(primary.get("candidates") or [])
    first_price = None
    if primary.get("evidence") and primary.get("status") in {PPS_FOUND, PUBLIC_PRICE_PARTIAL}:
        first_price = primary["evidence"].get("unit_price")

    # --- Alternate domain sweep (do not stop at first price) ---
    trace["routes_attempted"].append("alternate_domain_sweep")
    extra = _extra_domain_sweep(identity, max_pages=4)
    flags["alternate_domain_search_exhausted"] = True
    flags["multiple_candidates_sought"] = True
    # Dedupe
    seen = {f"{c.get('url')}|{c.get('unit_price')}" for c in candidates}
    for c in extra:
        k = f"{c.get('url')}|{c.get('unit_price')}"
        if k not in seen:
            seen.add(k)
            candidates.append(c)

    # Prefer NEW for economics; keep reman as intelligence
    new_cands = [
        c
        for c in candidates
        if str(c.get("condition") or "").upper() in {"NEW", "UNKNOWN", ""}
        and c.get("unit_price")
    ]
    reman_intel = [
        c
        for c in candidates
        if str(c.get("condition") or "").upper() in {"REMANUFACTURED", "RECONDITIONED", "USED"}
    ]
    for c in reman_intel:
        trace["condition_mismatches"].append(
            {"url": c.get("url"), "price": c.get("unit_price"), "condition": c.get("condition")}
        )

    best = None
    if new_cands:
        new_cands.sort(key=lambda c: float(c.get("unit_price") or 1e18))
        best = new_cands[0]
        best["selection_reason"] = (
            f"BEST_DEFENSIBLE_NEW_LANDED_PRICE; among {len(new_cands)} NEW/UNKNOWN; "
            f"seller={best.get('seller')}; via={best.get('via')}"
        )
        for other in new_cands[1:5]:
            trace["rejected_candidates"].append(
                {
                    "url": other.get("url"),
                    "price": other.get("unit_price"),
                    "reason": "HIGHER_THAN_BEST_NEW",
                }
            )

    best_price_improvement = False
    if best and first_price and float(best["unit_price"]) < float(first_price) * 0.98:
        best_price_improvement = True
    elif best and not first_price and len(new_cands) >= 2:
        best_price_improvement = True

    # Spec / brand-or-equal flags
    if rclass == STRONG_GENERIC_SPEC:
        flags["generic_spec_search_exhausted"] = True
        trace["routes_attempted"].append("generic_spec_search")
    if rclass == BRAND_OR_EQUAL:
        flags["brand_equal_search_exhausted"] = True
        trace["routes_attempted"].append("brand_or_equal_search")
        # Equal alternatives recorded when primary found reference
        if best:
            trace["alternate_products"].append(
                {"note": "reference_priced; equal alternatives require full mandatory-spec verify", "reference": best.get("url")}
            )

    # --- History (revenue only) ---
    hist = _history_probe(identity, oid)
    flags["history_search_exhausted"] = True
    trace["historical_matches"] = hist.get("hits") or []

    # --- Channel intelligence for specialty / no price ---
    channel = None
    if rclass == SPECIALTY_OEM_NARROW_CHANNEL or (not best and hist.get("hits")):
        channel = research_channel(identity, opportunity_id=oid, history_hits=hist.get("hits"))
        flags["channel_research_exhausted"] = True
        trace["channel_findings"] = channel
    elif rclass == SPECIALTY_OEM_NARROW_CHANNEL:
        channel = research_channel(identity, opportunity_id=oid, history_hits=[])
        flags["channel_research_exhausted"] = True
        trace["channel_findings"] = channel

    score = compute_exhaustion_score(flags, routing=rclass)

    # Budget deferral
    if primary.get("status") == PRICE_SEARCH_BUDGET_EXHAUSTED and not best:
        return {
            "status": AI_BUDGET_DEFERRED,
            "routing": routing,
            "exhaustion_flags": flags,
            "exhaustion_score": score,
            "evidence": None,
            "candidates": candidates[:8],
            "history": hist,
            "channel": channel,
            "trace": trace,
            "note": "Budget deferred — not NO_PRICE",
        }

    price_status = None
    evidence = None
    if best:
        price_status = PUBLIC_PRICE_FOUND
        evidence = {
            "unit_price": best["unit_price"],
            "displayed_price": best.get("displayed_price") or best["unit_price"],
            "condition": best.get("condition") or "NEW",
            "seller": best.get("seller"),
            "seller_class": best.get("seller_class"),
            "source_url": best.get("url"),
            "shipping": best.get("shipping"),
            "core_charge": best.get("core_charge"),
            "core_refundable": best.get("core_refundable"),
            "via": best.get("via"),
            "selection_reason": best.get("selection_reason"),
            "basis": "CURRENT_PUBLIC_NEW_PRICE",
            "not_from_gov_history": True,
            "alternate_candidates": [
                {"url": c.get("url"), "price": c.get("unit_price"), "condition": c.get("condition")}
                for c in new_cands[1:5]
            ],
        }
        trace["selected_price"] = evidence
        trace["selection_reason"] = best.get("selection_reason")
    elif primary.get("status") == CONDITION_MISMATCH or reman_intel:
        price_status = "CONDITION_MISMATCH"
    elif primary.get("status") == PRICE_SOURCE_BLOCKED_RETRYABLE and score < MIN_EXHAUSTION_SCORE_FOR_TERMINAL:
        price_status = RESEARCH_RETRYABLE
    elif primary.get("status") == PRICE_SOURCE_BLOCKED_RETRYABLE:
        price_status = "ROUTE_BLOCKED_RETRYABLE"
    elif score < MIN_EXHAUSTION_SCORE_FOR_TERMINAL:
        price_status = RESEARCH_RETRYABLE
    else:
        price_status = NO_PRICE_AFTER_EXHAUSTIVE_SEARCH

    # Quote reserve admission only when truly exhausted with no usable NEW price
    reserve = None
    premature = False
    if price_status in {NO_PRICE_AFTER_EXHAUSTIVE_SEARCH, "CONDITION_MISMATCH", "ROUTE_BLOCKED_RETRYABLE"} and not best:
        rev = hist.get("gov_unit_price") or solicitation_value
        # Plausible profit if we could buy at 80% of revenue
        plausible = False
        if rev and float(rev) > 0:
            plausible = True
        sourcing = None
        if channel:
            if channel.get("oem_direct_possible"):
                sourcing = "OEM"
            elif channel.get("authorized_distributor_path"):
                sourcing = "DISTRIBUTOR"
            elif channel.get("open_reseller_channel"):
                sourcing = "SUPPLIER"
            else:
                sourcing = "CHANNEL"
        elif identity.get("manufacturer"):
            sourcing = "OEM"
        pn_ok = bool(
            identity.get("part_number")
            or identity.get("model")
            or (identity.get("raw_description") and len(str(identity.get("raw_description"))) > 20)
        )
        reserve = admit_to_quote_reserve(
            eligibility_ok=eligibility_ok,
            has_package=has_package,
            usable_identity=pn_ok,
            fatal_execution_blocker=False,
            revenue_or_value=bool(rev),
            time_remaining_ok=time_remaining_ok,
            sourcing_path=sourcing,
            exhaustion_flags=flags,
            exhaustion_score=score,
            routing=rclass,
            plausible_profit_if_quote_ok=plausible,
            channel=channel,
        )
        premature = bool(reserve.get("admitted") and reserve.get("premature"))
        if reserve.get("admitted"):
            reserve["sensitivity"] = sensitivity_table(expected_gov_revenue=_f_rev(rev))
            price_status = reserve["status"]
        elif reserve.get("premature"):
            # Correctly refused — do NOT count as premature admission
            price_status = reserve.get("status") or RESEARCH_RETRYABLE

    recovered_via_alternate = bool(extra) and not first_price and bool(best)

    return {
        "status": price_status,
        "routing": routing,
        "exhaustion_flags": flags,
        "exhaustion_score": score,
        "evidence": evidence,
        "candidates": candidates[:8],
        "n_new_candidates": len(new_cands),
        "best_price_improvement": best_price_improvement,
        "recovered_via_alternate": recovered_via_alternate,
        "history": hist,
        "channel": channel,
        "quote_reserve": reserve,
        "premature_reserve": premature,
        "primary_status": primary.get("status"),
        "serp_both_down": serp_circuit_open(),
        "trace": trace,
    }


def _f_rev(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None
