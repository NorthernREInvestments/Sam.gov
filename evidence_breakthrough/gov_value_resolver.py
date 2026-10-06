"""GovernmentValueResolver — exhaustive buyer-history first, independent of acquisition cost."""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from evidence_breakthrough.models import (
    FOUND,
    GOV_BASKET_HISTORY,
    GOV_CLOSE_PRODUCT_HISTORY,
    GOV_CURRENT_BUDGET,
    GOV_CURRENT_ESTIMATE,
    GOV_EXACT_LINE_HISTORY,
    GOV_EXACT_PRODUCT_HISTORY,
    GOV_EXACT_SOLICITATION_HISTORY,
    GOV_NO_USABLE_HISTORY,
    HISTORY_TOO_GENERIC,
    INSUFFICIENT_IDENTITY,
    NO_BUYER_HISTORY,
    NO_MATCHING_HISTORY,
    NOT_FOUND_AFTER_EXHAUSTIVE_SEARCH,
    RETRYABLE_FAILURE,
    RETRYABLE_SOURCE_FAILURE,
    empty_resolver_result,
)
from evidence_breakthrough.opengov_history import (
    build_or_load_buyer_history,
    fetch_project,
    parse_opengov_opportunity_id,
    search_buyer_history,
)

log = logging.getLogger("govtracker.evidence_breakthrough.gov_value")


def _confidence_for_grade(match_grade: str, score: int) -> str:
    if match_grade in {"EXACT_PN", "EXACT_PN_TOKEN", "EXACT_MODEL", "EXACT_MODEL_TOKEN"} and score >= 85:
        return "A"
    if match_grade in {"MFR_MODEL_IN_DESC", "SAME_SOLICITATION"} or score >= 70:
        return "B"
    if score >= 40:
        return "C"
    return "D"


def _gov_match_type(match_grade: str, score: int) -> str:
    if match_grade in {"EXACT_PN", "EXACT_PN_TOKEN"}:
        return GOV_EXACT_LINE_HISTORY
    if match_grade in {"EXACT_MODEL", "EXACT_MODEL_TOKEN", "MFR_MODEL_IN_DESC"}:
        return GOV_EXACT_PRODUCT_HISTORY
    if match_grade in {"SAME_SOLICITATION", "SAME_TITLE"}:
        return GOV_EXACT_SOLICITATION_HISTORY
    if match_grade == "DESC_OVERLAP" and score >= 55:
        return GOV_CLOSE_PRODUCT_HISTORY
    if match_grade == "DESC_OVERLAP":
        return GOV_BASKET_HISTORY
    return GOV_CLOSE_PRODUCT_HISTORY


def _has_identity(identity: dict[str, Any]) -> bool:
    return bool(
        identity.get("part_number")
        or identity.get("catalog_number")
        or identity.get("model")
        or identity.get("sku")
        or identity.get("nsn")
        or (
            identity.get("manufacturer")
            and identity.get("raw_description")
            and len(str(identity.get("raw_description") or "")) >= 12
        )
    )


def resolve_government_value(
    identity: dict[str, Any],
    *,
    opportunity_id: str | None = None,
    buyer_name: str | None = None,
    solicitation_title: str | None = None,
    financial_id: str | None = None,
    client: httpx.Client | None = None,
    history_cache: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Cascade buyer-specific OpenGov history; NO_GOV_VALUE only after exhaustion."""
    result = empty_resolver_result(side="government_value")
    oid = opportunity_id or identity.get("opportunity_id") or ""
    gov_code, project_id = parse_opengov_opportunity_id(str(oid))

    if not _has_identity(identity):
        result["status"] = INSUFFICIENT_IDENTITY
        result["failure_reason"] = "INSUFFICIENT_IDENTITY"
        result["stop_reason"] = INSUFFICIENT_IDENTITY
        result["match_type"] = GOV_NO_USABLE_HISTORY
        return result

    routes: list[str] = []
    queries: list[str] = []
    sources: list[str] = []

    # Route 1–2: exact solicitation / buyer+solicitation via current project meta
    if gov_code and project_id:
        routes.append("exact_solicitation_project")
        sources.append(f"opengov_project:{project_id}")
        fr = fetch_project(project_id, gov_code, client=client)
        if fr.get("retryable"):
            result["status"] = RETRYABLE_FAILURE
            result["failure_reason"] = RETRYABLE_SOURCE_FAILURE
            result["stop_reason"] = RETRYABLE_FAILURE
            result["routes_attempted"] = routes
            return result
        if fr.get("ok") and isinstance(fr.get("project"), dict):
            proj = fr["project"]
            financial_id = financial_id or proj.get("financialId")
            solicitation_title = solicitation_title or proj.get("title")
            buyer_name = buyer_name or ((proj.get("government") or {}).get("name"))
            # Current estimate / budget from price table headers / maxBid
            for pt in proj.get("priceTables") or []:
                if not isinstance(pt, dict):
                    continue
                for key, mtype in (("maxBid", GOV_CURRENT_BUDGET), ("minBid", GOV_CURRENT_ESTIMATE)):
                    val = pt.get(key)
                    try:
                        num = float(val) if val is not None else None
                    except (TypeError, ValueError):
                        num = None
                    if num and num > 0:
                        result.update(
                            {
                                "status": FOUND,
                                "match_type": mtype,
                                "confidence": "C",
                                "source": "opengov_price_table",
                                "evidence": {
                                    "price_role": "ESTIMATE" if mtype == GOV_CURRENT_ESTIMATE else "BUDGET",
                                    "amount": num,
                                    "project_id": project_id,
                                    "title": solicitation_title,
                                    "note": "current solicitation table bound — not historical paid",
                                },
                                "provenance": [
                                    {
                                        "route": "current_price_table",
                                        "url": f"https://procurement.opengov.com/portal/{gov_code}/projects/{project_id}",
                                    }
                                ],
                                "stop_reason": "CURRENT_ESTIMATE_OR_BUDGET",
                            }
                        )
                        # Do not return yet — prefer paid history if available
                        result["_soft_estimate"] = dict(result)

    if not gov_code:
        routes.append("no_opengov_buyer_code")
        result["status"] = NOT_FOUND_AFTER_EXHAUSTIVE_SEARCH
        result["failure_reason"] = NO_BUYER_HISTORY
        result["match_type"] = GOV_NO_USABLE_HISTORY
        result["stop_reason"] = NO_BUYER_HISTORY
        result["routes_attempted"] = routes
        return result

    # Routes 3–15: buyer history cascade via OpenGov closed bid tabs
    routes.extend(
        [
            "buyer_opengov_closed_projects",
            "prior_solicitation_title",
            "buyer_product_mpn",
            "buyer_manufacturer_model",
            "normalized_description",
            "prior_bid_tabs",
            "award_notices_via_bid_results",
        ]
    )
    sources.append(f"opengov_buyer_history:{gov_code}")

    if history_cache is not None and gov_code in history_cache:
        profile = history_cache[gov_code]
    else:
        profile = build_or_load_buyer_history(gov_code, client=client)
        if history_cache is not None:
            history_cache[gov_code] = profile

    if profile.get("retryable") and not profile.get("priced_lines"):
        result["status"] = RETRYABLE_FAILURE
        result["failure_reason"] = RETRYABLE_SOURCE_FAILURE
        result["stop_reason"] = RETRYABLE_FAILURE
        result["routes_attempted"] = routes
        result["sources_attempted"] = sources
        return result

    if not profile.get("priced_lines"):
        # Exhausted OpenGov buyer history with no public tabs
        soft = result.pop("_soft_estimate", None)
        if soft and soft.get("status") == FOUND:
            soft["routes_attempted"] = routes
            soft["sources_attempted"] = sources
            soft["queries_attempted"] = queries
            return soft
        result["status"] = NOT_FOUND_AFTER_EXHAUSTIVE_SEARCH
        result["failure_reason"] = NO_BUYER_HISTORY
        result["match_type"] = GOV_NO_USABLE_HISTORY
        result["stop_reason"] = "BUYER_HISTORY_SCANNED_NO_PUBLIC_TABS"
        result["routes_attempted"] = routes
        result["sources_attempted"] = sources
        result["provenance"] = [
            {
                "route": "opengov_buyer_history",
                "projects_scanned": profile.get("historical_projects_scanned"),
                "bid_tabs_found": profile.get("bid_tabs_found"),
            }
        ]
        return result

    pn = identity.get("part_number") or identity.get("catalog_number") or identity.get("sku")
    model = identity.get("model")
    mfr = identity.get("manufacturer") or identity.get("brand")
    desc = identity.get("raw_description") or identity.get("commercial_search_key")
    queries.append(f"pn={pn}|model={model}|mfr={mfr}")

    ranked = search_buyer_history(
        profile,
        part_number=str(pn) if pn else None,
        model=str(model) if model else None,
        manufacturer=str(mfr) if mfr else None,
        description=str(desc) if desc else None,
        financial_id=str(financial_id) if financial_id else None,
        title=str(solicitation_title) if solicitation_title else None,
    )

    # Prefer exact product matches over solicitation-only basket
    usable = [
        h
        for h in ranked
        if h["match_grade"]
        in {
            "EXACT_PN",
            "EXACT_PN_TOKEN",
            "EXACT_MODEL",
            "EXACT_MODEL_TOKEN",
            "MFR_MODEL_IN_DESC",
            "SAME_SOLICITATION",
            "DESC_OVERLAP",
        }
        and h["score"] >= 40
    ]
    if not usable:
        soft = result.pop("_soft_estimate", None)
        if soft and soft.get("status") == FOUND:
            soft["routes_attempted"] = routes
            soft["sources_attempted"] = sources
            soft["queries_attempted"] = queries
            return soft
        result["status"] = NOT_FOUND_AFTER_EXHAUSTIVE_SEARCH
        result["failure_reason"] = NO_MATCHING_HISTORY if ranked else NO_MATCHING_HISTORY
        if ranked and ranked[0]["score"] < 40:
            result["failure_reason"] = HISTORY_TOO_GENERIC
        result["match_type"] = GOV_NO_USABLE_HISTORY
        result["stop_reason"] = NOT_FOUND_AFTER_EXHAUSTIVE_SEARCH
        result["routes_attempted"] = routes
        result["sources_attempted"] = sources
        result["queries_attempted"] = queries
        result["best_match"] = ranked[0] if ranked else None
        result["provenance"] = [
            {
                "route": "opengov_buyer_history",
                "projects_scanned": profile.get("historical_projects_scanned"),
                "priced_lines": len(profile.get("priced_lines") or []),
                "candidates_seen": len(ranked),
            }
        ]
        return result

    best = usable[0]
    ln = best["line"]
    match_type = _gov_match_type(best["match_grade"], best["score"])
    # Basket-level solicitation match without product identity → mark basket, lower confidence
    if best["match_grade"] in {"SAME_SOLICITATION", "SAME_TITLE"} and best["score"] < 85:
        # Only accept if same part also matches somehow — else basket
        if best["match_grade"] == "SAME_SOLICITATION" and not (
            pn and str(pn).upper() in str(ln.get("part_number") or ln.get("description") or "").upper()
        ):
            match_type = GOV_BASKET_HISTORY

    conf = _confidence_for_grade(best["match_grade"], best["score"])
    if match_type == GOV_BASKET_HISTORY:
        conf = "C"

    unit = ln.get("unit_price")
    result.pop("_soft_estimate", None)
    result.update(
        {
            "status": FOUND,
            "match_type": match_type,
            "confidence": conf,
            "source": ln.get("source") or "opengov_public_bid_tabulation",
            "evidence": {
                "historical_buyer": ln.get("buyer") or buyer_name or gov_code,
                "government_code": gov_code,
                "award_date": ln.get("closed_at"),
                "exact_item": ln.get("description"),
                "part_number": ln.get("part_number"),
                "quantity": ln.get("quantity"),
                "uom": ln.get("uom"),
                "awarded_unit_price": unit,
                "unit_price": unit,
                "nominal_historical_price": unit,
                "awarded_extended_total": ln.get("extended"),
                "winning_vendor": ln.get("winning_vendor"),
                "bidder_count": ln.get("bidder_count"),
                "project_id": ln.get("project_id"),
                "project_title": ln.get("project_title"),
                "financial_id": ln.get("financial_id"),
                "price_role": "PAID_OR_BID_TAB",
                "match_grade_internal": best["match_grade"],
                "match_score": best["score"],
                "source_url": ln.get("source_url"),
            },
            "provenance": [
                {
                    "route": "opengov_bid_tab",
                    "url": ln.get("source_url"),
                    "project_id": ln.get("project_id"),
                    "match_grade": best["match_grade"],
                }
            ],
            "best_match": {"match_grade": best["match_grade"], "score": best["score"]},
            "stop_reason": "MATCHED_BUYER_HISTORY",
            "failure_reason": None,
        }
    )
    result["routes_attempted"] = routes
    result["sources_attempted"] = sources
    result["queries_attempted"] = queries
    return result
