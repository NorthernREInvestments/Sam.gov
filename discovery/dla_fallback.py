"""Federal/DLA discovery fallback when direct DIBBS is bot-blocked.

Routes (in order):
1. Targeted SAM.gov search for DLA/SPE product solicitations (scarce, budgeted — NOT broad NAICS)
2. Controlled OpenAI web search only if SAM returns nothing (Cost Governor)
3. Preserve PUBLICLY_DISCOVERED_AUTHORITY_ACCESS_BLOCKED when DIBBS remains blocked

Never bypasses CAPTCHA/bot protection. Never injects known-answer IDs into production.
"""

from __future__ import annotations

import json
import os
import re
from datetime import timedelta
from typing import Any

from application_clock import now_utc, today_local
from discovery.schema import CanonicalOpportunity
from discovery.source_failure_taxonomy import TIER_A_AUTHORITATIVE, TIER_D_WEB_DISCOVERY

SPE_RE = re.compile(r"\b(SPE[0-9A-Z]{2,6}[-]?[0-9]{2,3}[-]?[A-Z]?[-]?[0-9A-Z]{3,8})\b", re.I)
NSN_RE = re.compile(r"\b(\d{4}-\d{2}-\d{3}-\d{4})\b")
SPR_RE = re.compile(r"\b(SPR[0-9A-Z]{2,8}[-]?[0-9A-Z]{2,12})\b", re.I)

DLA_WEB_QUERY_FAMILIES: list[str] = [
    "DLA DIBBS RFQ SPE current return by date",
    "Defense Logistics Agency solicitation SPE RFQ open quote",
    "DIBBS RFQ NSN site:sam.gov",
]

PURPOSE_DLA_CROSS_PUBLISH = "DLA_CROSS_PUBLISH_FALLBACK"


def _utc() -> str:
    return now_utc().isoformat()


def search_sam_dla_product_opportunities(
    *,
    authorize_live: bool = False,
    max_results: int = 50,
    posted_days: int = 21,
) -> dict[str, Any]:
    """Targeted SAM search for DLA product RFQs (SPE*/SPR*). Not broad NAICS discovery."""
    if not authorize_live:
        return {
            "executed": False,
            "error": "authorize_live_required",
            "opportunities": [],
            "LIVE_SAM_CALLS": 0,
            "paid": 0,
        }

    from api_budget import can_spend_sam, record_sam_usage

    api_key = (os.getenv("SAM_GOV_API_KEY") or "").strip()
    if not api_key:
        return {"executed": False, "error": "SAM_GOV_API_KEY_missing", "opportunities": [], "LIVE_SAM_CALLS": 0}

    if not can_spend_sam(1):
        return {"executed": False, "error": "sam_budget_exhausted", "opportunities": [], "LIVE_SAM_CALLS": 0}

    import httpx

    posted_to = today_local()
    posted_from = posted_to - timedelta(days=max(1, posted_days))
    attempts = [
        {"organizationName": "DEFENSE LOGISTICS AGENCY", "label": "org_dla"},
        {"keyword": "SPE", "label": "keyword_spe"},
    ]
    all_raw: list[dict[str, Any]] = []
    used_label = None
    calls = 0
    with httpx.Client(timeout=60.0) as client:
        for att in attempts:
            if calls >= 2:
                break
            if not can_spend_sam(1):
                break
            params = {
                "api_key": api_key,
                "postedFrom": posted_from.strftime("%m/%d/%Y"),
                "postedTo": posted_to.strftime("%m/%d/%Y"),
                "limit": min(100, max_results),
                "offset": 0,
                "active": "yes",
            }
            for k, v in att.items():
                if k != "label":
                    params[k] = v
            resp = client.get("https://api.sam.gov/opportunities/v2/search", params=params)
            calls += 1
            record_sam_usage(1)
            if resp.status_code != 200:
                continue
            batch = resp.json().get("opportunitiesData") or []
            if batch:
                all_raw.extend(batch)
                used_label = att["label"]
                break

    from sam_client import normalize_opportunity

    opps: list[CanonicalOpportunity] = []
    seen: set[str] = set()
    for raw in all_raw:
        sol = str(raw.get("solicitationNumber") or "")
        title = str(raw.get("title") or "")
        parent = str(raw.get("fullParentPathName") or raw.get("department") or "")
        blob = f"{sol} {title} {parent}".upper()
        is_dla = (
            "DEFENSE LOGISTICS" in parent.upper()
            or SPE_RE.search(blob)
            or SPR_RE.search(blob)
            or "DIBBS" in blob
        )
        if not is_dla:
            continue
        norm = normalize_opportunity(raw)
        ext = str(norm.get("notice_id") or sol or title)[:160]
        if ext in seen:
            continue
        seen.add(ext)
        nsn_m = NSN_RE.search(title) or NSN_RE.search(str(raw.get("description") or ""))
        opps.append(
            CanonicalOpportunity(
                external_id=ext,
                source_id="fed_dla_sam_cross_publish",
                source_url=norm.get("sam_url") or f"https://sam.gov/opp/{ext}/view",
                detail_url=norm.get("sam_url") or f"https://sam.gov/opp/{ext}/view",
                title=title or f"DLA solicitation {sol}",
                solicitation_number=sol or None,
                agency="Defense Logistics Agency",
                status="OPEN",
                jurisdiction="FEDERAL",
                buyer_type="FEDERAL_PUBLIC",
                deadline_raw=str(raw.get("responseDeadLine") or raw.get("reponseDeadLine") or "") or None,
                description=(raw.get("description") if isinstance(raw.get("description"), str) else None),
                trust_tier=1,
                raw_metadata={
                    "platform": "SAM_GOV",
                    "discovery_route": "SAM_DLA_CROSS_PUBLISH",
                    "provenance_tier": TIER_A_AUTHORITATIVE,
                    "authority_verification_state": "SAM_AUTHORITATIVE",
                    "sam_query": used_label,
                    "nsn": nsn_m.group(1) if nsn_m else None,
                    "notice_type": raw.get("type") or "Solicitation",
                },
            )
        )
        if len(opps) >= max_results:
            break

    return {
        "executed": True,
        "query_label": used_label,
        "opportunities": opps,
        "raw_count": len(all_raw),
        "kept": len(opps),
        "LIVE_SAM_CALLS": calls,
        "paid": 0,
        "OpenAI": 0,
        "provenance_tier": TIER_A_AUTHORITATIVE,
        "source_id": "fed_dla_sam_cross_publish",
    }


def discover_dla_via_web_search(
    *,
    authorize_paid: bool = False,
    max_queries: int = 1,
) -> dict[str, Any]:
    """Cost-governed OpenAI web search — only when SAM path yields nothing."""
    if not authorize_paid:
        return {"executed": False, "error": "authorize_paid_required", "opportunities": [], "OpenAI": 0, "paid": 0}

    try:
        from cost_governor import get_cost_governor

        state = str(get_cost_governor().budget_snapshot().get("budget_state") or "")
        if state in {"HARD_CAP_EXHAUSTED", "PAID_DISCOVERY_PAUSED"}:
            return {"executed": False, "error": f"cost_governor_{state}", "opportunities": [], "OpenAI": 0, "paid": 0}
    except Exception as exc:
        return {"executed": False, "error": f"cost_governor_unavailable:{exc}", "opportunities": [], "OpenAI": 0, "paid": 0}

    try:
        from openai_runtime import create_response, text_part
    except Exception as exc:
        return {"executed": False, "error": f"openai_runtime_unavailable:{exc}", "opportunities": [], "OpenAI": 0, "paid": 0}

    opportunities: list[CanonicalOpportunity] = []
    openai_calls = 0
    for q in DLA_WEB_QUERY_FAMILIES[: max(1, max_queries)]:
        try:
            text = create_response(
                task="dla_discovery_web_search",
                instructions=(
                    "Find CURRENT open Defense Logistics Agency (DLA) / DIBBS product RFQ solicitations. "
                    "Return a JSON array with keys: solicitation_number, title, nsn, part_number, "
                    "quantity, response_deadline, buyer, source_url, confidence. "
                    "Only currently open solicitations. Do not invent identifiers."
                ),
                content=[text_part(f"Search focus: {q}")],
                max_output_tokens=1200,
                web_search=True,
                funnel_stage=3,
                automatic=True,
            )
            openai_calls += 1
        except Exception:
            continue
        text = text or ""
        for m in SPE_RE.finditer(text):
            sol = m.group(1).upper().replace(" ", "")
            window = text[max(0, m.start() - 40) : m.end() + 200]
            nsn_m = NSN_RE.search(window)
            opportunities.append(
                CanonicalOpportunity(
                    external_id=sol,
                    source_id="fed_dla_web_discovery_fallback",
                    title=f"DLA RFQ {sol}" + (f" NSN {nsn_m.group(1)}" if nsn_m else ""),
                    solicitation_number=sol,
                    agency="Defense Logistics Agency",
                    status="OPEN",
                    jurisdiction="FEDERAL",
                    buyer_type="FEDERAL_PUBLIC",
                    trust_tier=3,
                    raw_metadata={
                        "platform": "WEB_DISCOVERY",
                        "discovery_route": "OPENAI_WEB_SEARCH_DLA",
                        "provenance_tier": TIER_D_WEB_DISCOVERY,
                        "authority_verification_state": "PUBLICLY_DISCOVERED_AUTHORITY_ACCESS_BLOCKED",
                        "query": q,
                        "nsn": nsn_m.group(1) if nsn_m else None,
                    },
                )
            )
        try:
            start, end = text.find("["), text.rfind("]")
            if start >= 0 and end > start:
                for item in json.loads(text[start : end + 1]):
                    if not isinstance(item, dict):
                        continue
                    sol = str(item.get("solicitation_number") or "").strip().upper()
                    if not sol or not SPE_RE.search(sol):
                        continue
                    opportunities.append(
                        CanonicalOpportunity(
                            external_id=sol,
                            source_id="fed_dla_web_discovery_fallback",
                            source_url=item.get("source_url"),
                            detail_url=item.get("source_url"),
                            title=str(item.get("title") or f"DLA RFQ {sol}")[:500],
                            solicitation_number=sol,
                            agency=str(item.get("buyer") or "Defense Logistics Agency"),
                            status="OPEN",
                            jurisdiction="FEDERAL",
                            buyer_type="FEDERAL_PUBLIC",
                            deadline_raw=str(item.get("response_deadline") or "") or None,
                            trust_tier=3,
                            raw_metadata={
                                "platform": "WEB_DISCOVERY",
                                "discovery_route": "OPENAI_WEB_SEARCH_DLA",
                                "provenance_tier": TIER_D_WEB_DISCOVERY,
                                "authority_verification_state": "PUBLICLY_DISCOVERED_AUTHORITY_ACCESS_BLOCKED",
                                "nsn": item.get("nsn"),
                                "part_number": item.get("part_number"),
                                "quantity": item.get("quantity"),
                                "query": q,
                            },
                        )
                    )
        except Exception:
            pass

    seen: set[str] = set()
    deduped: list[CanonicalOpportunity] = []
    for o in opportunities:
        key = (o.solicitation_number or o.external_id or "").upper()
        if key and key not in seen:
            seen.add(key)
            deduped.append(o)

    return {
        "executed": openai_calls > 0,
        "opportunities": deduped,
        "OpenAI": openai_calls,
        "paid": openai_calls,
        "LIVE_SAM_CALLS": 0,
        "provenance_tier": TIER_D_WEB_DISCOVERY,
        "source_id": "fed_dla_web_discovery_fallback",
    }


def run_dla_discovery_fallback(
    *,
    authorize_live: bool = False,
    allow_web_search: bool = True,
    dibbs_blocked: bool = True,
) -> dict[str, Any]:
    """SAM cross-publish first; web search only if SAM empty and DIBBS blocked."""
    routes_tried: list[str] = []
    all_opps: list[CanonicalOpportunity] = []
    sam_calls = 0
    openai_calls = 0
    authoritative = 0
    fallback = 0

    sam = search_sam_dla_product_opportunities(authorize_live=authorize_live)
    routes_tried.append("SAM_DLA_CROSS_PUBLISH")
    sam_calls += int(sam.get("LIVE_SAM_CALLS") or 0)
    for o in sam.get("opportunities") or []:
        all_opps.append(o)
        authoritative += 1

    web: dict[str, Any] = {"executed": False, "opportunities": [], "OpenAI": 0}
    if dibbs_blocked and allow_web_search and authorize_live and len(all_opps) == 0:
        web = discover_dla_via_web_search(authorize_paid=True, max_queries=1)
        routes_tried.append("OPENAI_WEB_SEARCH_DLA")
        openai_calls += int(web.get("OpenAI") or 0)
        for o in web.get("opportunities") or []:
            all_opps.append(o)
            fallback += 1

    return {
        "kind": "DlaDiscoveryFallback",
        "at": _utc(),
        "dibbs_blocked": dibbs_blocked,
        "routes_tried": routes_tried,
        "opportunities": all_opps,
        "authoritative_count": authoritative,
        "fallback_count": fallback,
        "LIVE_SAM_CALLS": sam_calls,
        "OpenAI": openai_calls,
        "paid": openai_calls,
        "SAM": sam_calls,
        "sam_result": {k: v for k, v in sam.items() if k != "opportunities"},
        "web_result": {k: v for k, v in web.items() if k != "opportunities"},
    }


def probe_known_answer_status(identifiers: list[str], *, discovered: list[CanonicalOpportunity]) -> list[dict[str, Any]]:
    found = {(o.solicitation_number or o.external_id or "").upper().replace("-", "") for o in discovered}
    out = []
    for ident in identifiers:
        key = ident.upper().replace("-", "").replace(" ", "")
        hit = any(key in f or f in key for f in found)
        out.append(
            {
                "identifier": ident,
                "status": "OPEN_AND_FOUND" if hit else "UNVERIFIABLE",
                "note": "Probe only — not injected into production",
            }
        )
    return out
