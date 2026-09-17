"""Discovery network API — zero external calls on GET; fixture sync only by default."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from database import SessionLocal
from discovery.analytics import analyze_run_results, quality_sample
from discovery.coverage import build_coverage_report
from discovery.fixtures import ALL_FIXTURES
from discovery.metrics import build_discovery_funnel_metrics, list_discovered_opportunities
from discovery.onboarding import onboard_source, persist_onboarded_agency
from discovery.profiles import PROFILES
from discovery.registry import registry_summary, seed_discovery_sources
from discovery.runner import run_fixture_discovery
from discovery.sam_policy import assert_no_broad_sam_discovery
from discovery.start_deal import set_operator_status, start_deal_from_discovered
from discovery.state_matrix import state_coverage_summary
from discovery.taxonomy import taxonomy_report

router = APIRouter(prefix="/api/discovery", tags=["discovery"])


@router.get("/funnel")
def get_discovery_funnel() -> dict[str, Any]:
    session = SessionLocal()
    try:
        return build_discovery_funnel_metrics(session)
    finally:
        session.close()


@router.get("/opportunities")
def get_discovered_opportunities(
    classification: str | None = Query(None),
    status: str | None = Query(None),
    jurisdiction: str | None = Query(None),
    buyer_type: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    session = SessionLocal()
    try:
        return list_discovered_opportunities(
            session,
            classification=classification,
            operator_status=status,
            jurisdiction=jurisdiction,
            buyer_type=buyer_type,
            limit=limit,
        )
    finally:
        session.close()


@router.get("/sources")
def get_discovery_sources(include_planned: bool = Query(True)) -> dict[str, Any]:
    from models import DiscoverySource

    session = SessionLocal()
    try:
        seed_discovery_sources(session)
        session.commit()
        rows = session.query(DiscoverySource).order_by(DiscoverySource.source_id.asc()).all()
        items = []
        for r in rows:
            st = (r.adapter_status or "").upper()
            if not include_planned and st == "PLANNED":
                continue
            items.append(
                {
                    "source_id": r.source_id,
                    "source_name": r.source_name,
                    "source_type": r.source_type,
                    "jurisdiction": r.jurisdiction,
                    "state_code": r.state_code,
                    "platform_family": r.platform_family,
                    "adapter_status": r.adapter_status,
                    "list_url": r.list_url,
                    "enabled": r.enabled,
                    "health": r.health_status,
                    "last_sync": r.last_successful_sync.isoformat() if r.last_successful_sync else None,
                    "records_seen": r.records_seen,
                    "trust_tier": r.trust_tier,
                    "live_capable": st == "LIVE_CAPABLE",
                    "fixture_only": st in {"FIXTURE_ONLY", "IMPLEMENTED"},
                    "operational": st == "LIVE_CAPABLE" and r.enabled,
                }
            )
        summary = registry_summary(session)
        return {
            "sources": items,
            "summary": summary,
            "coverage": build_coverage_report(session),
            "sam_policy": assert_no_broad_sam_discovery(),
            "LIVE_API_REQUESTS": 0,
        }
    finally:
        session.close()


@router.get("/coverage")
def get_coverage() -> dict[str, Any]:
    return build_coverage_report()


@router.get("/state-matrix")
def get_state_matrix() -> dict[str, Any]:
    return state_coverage_summary()


@router.get("/taxonomy")
def get_taxonomy() -> dict[str, Any]:
    return taxonomy_report()


@router.get("/profiles")
def get_profiles() -> dict[str, Any]:
    return {
        "profiles": {
            k: {
                "name": v["name"],
                "max_sources": v["max_sources"],
                "max_records_total": v["max_records_total"],
                "budget": v["budget"].to_dict(),
                "SAM": v["SAM"],
                "OpenAI": v["OpenAI"],
                "USAspending": v["USAspending"],
                "paid": v["paid"],
            }
            for k, v in PROFILES.items()
        },
        "note": "Profiles prepared — do not auto-run live",
        "LIVE_API_REQUESTS": 0,
    }


@router.post("/seed-registry")
def post_seed_registry() -> dict[str, Any]:
    session = SessionLocal()
    try:
        out = seed_discovery_sources(session)
        session.commit()
        return {**out, "summary": registry_summary(session), "coverage": build_coverage_report(session)}
    finally:
        session.close()


class FixtureSyncBody(BaseModel):
    dry_run: bool = False
    sources: list[str] | None = Field(
        default=None,
        description="Optional subset of fixture source_ids; default all fixtures",
    )


@router.post("/run-fixtures")
def post_run_fixtures(body: FixtureSyncBody | None = None) -> dict[str, Any]:
    """Deterministic fixture discovery — no network."""
    body = body or FixtureSyncBody()
    payloads = dict(ALL_FIXTURES)
    if body.sources:
        payloads = {k: v for k, v in payloads.items() if k in body.sources}
    session = SessionLocal()
    try:
        out = run_fixture_discovery(session, source_payloads=payloads, dry_run=body.dry_run)
        if not body.dry_run:
            session.commit()
        else:
            session.rollback()
        return out
    finally:
        session.close()


class OnboardBody(BaseModel):
    name: str
    url: str
    jurisdiction: str | None = None
    agency_type: str | None = None
    state_code: str | None = None
    persist: bool = False
    authorize_fetch: bool = False


@router.post("/onboard")
def post_onboard(body: OnboardBody) -> dict[str, Any]:
    """Detect platform / suggest adapter — fetches nothing unless authorize_fetch (still not auto-crawled)."""
    out = onboard_source(
        name=body.name,
        url=body.url,
        jurisdiction=body.jurisdiction,
        agency_type=body.agency_type,
        state_code=body.state_code,
        authorize_fetch=body.authorize_fetch,
    )
    if body.persist:
        session = SessionLocal()
        try:
            persisted = persist_onboarded_agency(session, out)
            session.commit()
            out["persisted"] = persisted
        finally:
            session.close()
    return out


class StartDealBody(BaseModel):
    operator: str | None = None


@router.post("/opportunities/{discovered_id}/start-deal")
def post_start_deal(discovered_id: int, body: StartDealBody | None = None) -> dict[str, Any]:
    body = body or StartDealBody()
    session = SessionLocal()
    try:
        out = start_deal_from_discovered(session, discovered_id, operator=body.operator)
        session.commit()
        return out
    finally:
        session.close()


class StatusBody(BaseModel):
    status: str


@router.post("/opportunities/{discovered_id}/status")
def post_opportunity_status(discovered_id: int, body: StatusBody) -> dict[str, Any]:
    session = SessionLocal()
    try:
        out = set_operator_status(session, discovered_id, body.status)
        session.commit()
        return out
    finally:
        session.close()


@router.get("/sam-policy")
def get_sam_policy() -> dict[str, Any]:
    from sam_scarcity import sam_budget_snapshot

    return {
        **assert_no_broad_sam_discovery(),
        "budget": sam_budget_snapshot(),
        "LIVE_API_REQUESTS": 0,
    }


@router.post("/analytics/sample")
def post_analytics_sample(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Analyze in-memory opportunity list (no network)."""
    opps = (payload or {}).get("opportunities") or []
    return {
        "analytics": analyze_run_results(opps),
        "quality_samples": quality_sample(opps),
        "LIVE_API_REQUESTS": 0,
        "OpenAI": 0,
    }
