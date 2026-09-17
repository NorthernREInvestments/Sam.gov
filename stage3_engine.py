"""Stage 3 targeted research engine — Postgres first; external only when authorized.

No Luna for deterministic retrieval. Web OFF by default.
"""

from __future__ import annotations
from application_clock import now_utc

from datetime import datetime, timezone
from typing import Any, Callable

from historical_context import get_historical_context_for_opportunity
from knowledge_store import load_current_supplier_offers, record_research_event
from product_deal import plan_product_research

# Provider registry: task_code → callable(ctx) -> result dict
ProviderFn = Callable[[dict[str, Any]], dict[str, Any]]
_PROVIDERS: dict[str, ProviderFn] = {}


def register_provider(task_code: str, fn: ProviderFn) -> None:
    _PROVIDERS[task_code] = fn


def _postgres_historical(ctx: dict[str, Any]) -> dict[str, Any]:
    hist = get_historical_context_for_opportunity(
        ctx.get("opportunity"),
        session=ctx.get("session"),
        include_gs_lookup=True,
    )
    facts = hist.get("from_pricing_intel") or []
    facts += hist.get("from_gs_contracts") or []
    return {
        "task_code": "HISTORICAL_PRICING_REQUIRED",
        "source": "postgres_pricing_intel_gs_contracts",
        "retrieved_at": now_utc().isoformat(),
        "fact_status": "VERIFIED" if facts else "UNKNOWN",
        "facts": facts,
        "evidence": {"count": len(facts)},
        "freshness": "HISTORICAL",
        "cost_usd": 0.0,
        "reusable": True,
        "external_api_calls": 0,
        "cache_hit": False,
        "notes": "Historical only — not current revenue",
    }


def _postgres_competition(ctx: dict[str, Any]) -> dict[str, Any]:
    hist = get_historical_context_for_opportunity(
        ctx.get("opportunity"), session=ctx.get("session"), include_gs_lookup=True
    )
    offers = []
    for item in (hist.get("from_pricing_intel") or []) + (hist.get("from_gs_contracts") or []):
        if item.get("number_of_offers") is not None:
            offers.append(item["number_of_offers"])
    return {
        "task_code": "COMPETITION_HISTORY_REQUIRED",
        "source": "postgres_historical",
        "retrieved_at": now_utc().isoformat(),
        "fact_status": "VERIFIED" if offers else "UNKNOWN",
        "facts": [{"number_of_offers": o, "temporal": "HISTORICAL"} for o in offers],
        "evidence": {"offer_counts_found": len(offers)},
        "freshness": "HISTORICAL",
        "cost_usd": 0.0,
        "reusable": True,
        "external_api_calls": 0,
        "notes": "No invented competition count",
    }


def _postgres_current_supplier_quote(ctx: dict[str, Any]) -> dict[str, Any]:
    session = ctx.get("session")
    opp = ctx.get("opportunity")
    cid = getattr(opp, "id", None) if opp is not None else None
    if session is None or cid is None:
        return {
            "task_code": "CURRENT_SUPPLIER_QUOTE_REQUIRED",
            "source": "postgres_gt_supplier_offers",
            "fact_status": "UNKNOWN",
            "facts": [],
            "cost_usd": 0.0,
            "external_api_calls": 0,
            "notes": "No session/contract for quote lookup",
            "retrieved_at": now_utc().isoformat(),
            "reusable": False,
        }
    offers = load_current_supplier_offers(session, int(cid))
    return {
        "task_code": "CURRENT_SUPPLIER_QUOTE_REQUIRED",
        "source": "postgres_gt_supplier_offers",
        "retrieved_at": now_utc().isoformat(),
        "fact_status": "VERIFIED" if offers else "UNKNOWN",
        "facts": offers,
        "evidence": {"current_offers": len(offers)},
        "freshness": "CURRENT" if offers else "NONE",
        "cost_usd": 0.0,
        "reusable": bool(offers),
        "external_api_calls": 0,
        "notes": "Historical offers intentionally excluded",
    }


register_provider("HISTORICAL_PRICING_REQUIRED", _postgres_historical)
register_provider("COMPETITION_HISTORY_REQUIRED", _postgres_competition)
register_provider("CURRENT_SUPPLIER_QUOTE_REQUIRED", _postgres_current_supplier_quote)


def stage3_preflight(
    opportunity: Any,
    *,
    stage0: dict[str, Any] | None = None,
    stage1: dict[str, Any] | None = None,
    stage2: dict[str, Any] | None = None,
    session: Any = None,
) -> dict[str, Any]:
    """Show exact external research tasks BEFORE execution — no paid calls."""
    plan = plan_product_research(
        opportunity, stage0=stage0, stage1=stage1, stage2=stage2, session=session
    )
    tasks = plan.get("research_tasks") or []
    providers_available = []
    needs_external = []
    for t in tasks:
        code = t.get("code")
        if code in _PROVIDERS:
            providers_available.append(
                {
                    "code": code,
                    "provider": "postgres_first",
                    "would_call_external": False,
                    "estimated_cost_usd": 0.0,
                }
            )
        else:
            needs_external.append(
                {
                    "code": code,
                    "provider": "NOT_IMPLEMENTED_EXTERNAL",
                    "would_call_external": True,
                    "estimated_cost_usd": None,
                    "status": "BLOCKED_UNTIL_AUTHORIZED",
                    "allowed_sources": t.get("allowed_sources") or [],
                }
            )
    return {
        "opportunity_id": getattr(opportunity, "id", None),
        "core_fit": plan.get("core_fit"),
        "research_plan": plan,
        "postgres_resolvable": providers_available,
        "external_tasks_requiring_authorization": needs_external,
        "LIVE_API_REQUESTS_IF_EXECUTED_POSTGRES_ONLY": 0,
        "LIVE_API_REQUESTS_IF_EXTERNAL_AUTHORIZED": "unknown_until_providers_implemented",
        "web_search_default": False,
        "authorize_required_for_external": True,
    }


def run_stage3(
    opportunity: Any,
    *,
    stage0: dict[str, Any] | None = None,
    stage1: dict[str, Any] | None = None,
    stage2: dict[str, Any] | None = None,
    session: Any = None,
    authorize_external: bool = False,
    authorize_web: bool = False,
    authorize_openai: bool = False,
    persist_events: bool = True,
) -> dict[str, Any]:
    """
    Execute Stage 3 postgres-first providers.
    External/web/OpenAI remain blocked unless explicitly authorized
    (and providers must still refuse if not implemented).
    """
    if authorize_web or authorize_openai:
        # Keep hard-off until real adapters exist — do not silently succeed
        if not authorize_external:
            return {
                "error": "web_or_openai_requires_authorize_external",
                "executed": False,
                "LIVE_API_REQUESTS": 0,
            }

    pre = stage3_preflight(
        opportunity, stage0=stage0, stage1=stage1, stage2=stage2, session=session
    )
    plan = pre["research_plan"]
    results: list[dict[str, Any]] = []
    external_blocked: list[dict[str, Any]] = []
    ctx = {"opportunity": opportunity, "session": session, "plan": plan}

    for item in pre["postgres_resolvable"]:
        code = item["code"]
        fn = _PROVIDERS[code]
        result = fn(ctx)
        result["authorized"] = True
        result["executed"] = True
        results.append(result)
        if persist_events and session is not None:
            record_research_event(
                session,
                contract_id=getattr(opportunity, "id", None),
                task_code=code,
                reason=(next((t.get("reason") for t in plan.get("research_tasks") or [] if t.get("code") == code), None)),
                source=result.get("source"),
                result_status=result.get("fact_status"),
                facts_produced_json=result.get("facts"),
                evidence_json=result.get("evidence"),
                cost_usd=result.get("cost_usd") or 0,
                cache_hit=bool(result.get("cache_hit")),
                reusable=bool(result.get("reusable")),
                authorized=True,
                executed=True,
                external_api_calls=int(result.get("external_api_calls") or 0),
                retrieved_at=now_utc(),
            )

    for item in pre["external_tasks_requiring_authorization"]:
        if not authorize_external:
            external_blocked.append({**item, "result_status": "UNKNOWN", "executed": False})
            continue
        # Authorized but no provider yet — remain UNKNOWN, do not fabricate
        external_blocked.append(
            {
                **item,
                "result_status": "UNKNOWN",
                "executed": False,
                "notes": "External provider not implemented — failure stays UNKNOWN",
            }
        )
        if persist_events and session is not None:
            record_research_event(
                session,
                contract_id=getattr(opportunity, "id", None),
                task_code=item["code"],
                reason="external_authorized_but_provider_missing",
                source="none",
                result_status="UNKNOWN",
                facts_produced_json=[],
                cost_usd=0,
                authorized=True,
                executed=False,
                external_api_calls=0,
                retrieved_at=now_utc(),
            )

    return {
        "executed": True,
        "authorize_external": authorize_external,
        "authorize_web": authorize_web,
        "authorize_openai": authorize_openai,
        "LIVE_API_REQUESTS": 0,
        "postgres_results": results,
        "external_blocked_or_unimplemented": external_blocked,
        "research_plan": plan,
        "preflight": pre,
    }
