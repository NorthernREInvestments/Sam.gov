"""Source-agnostic opportunity adapter contract + registry.

SAM is the first working source. Downstream funnel must not be SAM-specific.
No live connectors beyond existing SAM client in this module.
"""

from __future__ import annotations
from application_clock import now_utc

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Protocol


SOURCE_FEDERAL = "FEDERAL"
SOURCE_STATE = "STATE"
SOURCE_COUNTY = "COUNTY"
SOURCE_CITY = "CITY"
SOURCE_MUNICIPAL = "MUNICIPAL"
SOURCE_PUBLIC_UNIVERSITY = "PUBLIC_UNIVERSITY"
SOURCE_PUBLIC_SCHOOL = "PUBLIC_SCHOOL"
SOURCE_TRANSIT = "TRANSIT"
SOURCE_AIRPORT = "AIRPORT"
SOURCE_PUBLIC_UTILITY = "PUBLIC_UTILITY"
SOURCE_COOPERATIVE = "COOPERATIVE"
SOURCE_OTHER_PUBLIC = "OTHER_PUBLIC"

SOURCE_CATEGORIES = (
    SOURCE_FEDERAL,
    SOURCE_STATE,
    SOURCE_COUNTY,
    SOURCE_CITY,
    SOURCE_MUNICIPAL,
    SOURCE_PUBLIC_UNIVERSITY,
    SOURCE_PUBLIC_SCHOOL,
    SOURCE_TRANSIT,
    SOURCE_AIRPORT,
    SOURCE_PUBLIC_UTILITY,
    SOURCE_COOPERATIVE,
    SOURCE_OTHER_PUBLIC,
)


@dataclass
class NormalizedOpportunity:
    """Canonical opportunity identity for any public procurement source."""

    source: str
    source_opportunity_id: str
    title: str
    solicitation_number: str | None = None
    agency: str | None = None
    description: str | None = None
    due_date: date | None = None
    posted_date: date | None = None
    status: str | None = None
    set_aside: str | None = None
    naics_code: str | None = None
    psc_code: str | None = None
    nigp_codes: list[str] = field(default_factory=list)
    commodity_codes: list[str] = field(default_factory=list)
    location: str | None = None
    link: str | None = None
    document_links: list[dict[str, Any]] = field(default_factory=list)
    raw_payload: dict[str, Any] = field(default_factory=dict)
    source_retrieved_at: str | None = None
    source_updated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if isinstance(d.get("due_date"), date):
            d["due_date"] = d["due_date"].isoformat()
        if isinstance(d.get("posted_date"), date):
            d["posted_date"] = d["posted_date"].isoformat()
        return d

    def to_gt_contract_fields(self) -> dict[str, Any]:
        """Map into gt_contracts upsert field shape used by sync.upsert_contracts."""
        return {
            "notice_id": self.source_opportunity_id,
            "title": self.title,
            "agency": self.agency,
            "location": self.location,
            "naics_code": self.naics_code,
            "set_aside": self.set_aside,
            "due_date": self.due_date.isoformat() if self.due_date else None,
            "link": self.link,
            "description": self.description,
            "sam_raw": {
                **(self.raw_payload or {}),
                "_normalized_source": self.source,
                "_solicitation_number": self.solicitation_number,
                "classificationCode": self.psc_code or (self.raw_payload or {}).get("classificationCode"),
                "solicitationNumber": self.solicitation_number
                or (self.raw_payload or {}).get("solicitationNumber"),
            },
        }


class OpportunitySourceAdapter(Protocol):
    """Adapter contract for future state/local/co-op connectors."""

    source_category: str
    source_name: str

    def preflight(self, **kwargs: Any) -> dict[str, Any]:
        """Describe what a live fetch would do — no network by default."""
        ...

    def fetch(self, *, authorize_live: bool = False, **kwargs: Any) -> list[NormalizedOpportunity]:
        """Fetch opportunities. Must refuse unless authorize_live=True."""
        ...

    def normalize(self, raw: dict[str, Any]) -> NormalizedOpportunity:
        ...


_REGISTRY: dict[str, OpportunitySourceAdapter] = {}


def register_source(adapter: OpportunitySourceAdapter) -> None:
    _REGISTRY[adapter.source_name] = adapter


def get_source(name: str) -> OpportunitySourceAdapter | None:
    return _REGISTRY.get(name)


def list_sources() -> list[dict[str, str]]:
    return [
        {"source_name": a.source_name, "source_category": a.source_category}
        for a in _REGISTRY.values()
    ]


def normalize_sam_raw(raw: dict[str, Any]) -> NormalizedOpportunity:
    """Normalize existing SAM.gov opportunity payload into the common shape."""
    from sam_client import _parse_date, normalize_opportunity

    base = normalize_opportunity(raw)
    due = _parse_date(base.get("due_date")) if isinstance(base.get("due_date"), str) else base.get("due_date")
    if isinstance(due, str):
        due = _parse_date(due)
    posted = _parse_date(raw.get("postedDate") or raw.get("publishDate"))
    return NormalizedOpportunity(
        source=SOURCE_FEDERAL,
        source_opportunity_id=str(base.get("notice_id") or raw.get("noticeId") or ""),
        title=str(base.get("title") or "Untitled"),
        solicitation_number=raw.get("solicitationNumber"),
        agency=base.get("agency"),
        description=(
            raw.get("description")
            if isinstance(raw.get("description"), str) and not str(raw.get("description")).startswith("http")
            else base.get("description")
        ),
        due_date=due if isinstance(due, date) else None,
        posted_date=posted,
        status=str(raw.get("active") or raw.get("status") or "active"),
        set_aside=base.get("set_aside"),
        naics_code=base.get("naics_code"),
        psc_code=raw.get("classificationCode") or raw.get("psc"),
        location=base.get("location"),
        link=base.get("link"),
        document_links=_document_links_from_sam_raw(raw),
        raw_payload=dict(raw),
        source_retrieved_at=now_utc().isoformat(),
    )


def _document_links_from_sam_raw(raw: dict[str, Any]) -> list[dict[str, Any]]:
    links: list[dict[str, Any]] = []
    for item in raw.get("resourceLinks") or []:
        if isinstance(item, str) and item.startswith("http"):
            links.append({"url": item, "kind": "resourceLinks", "verification_status": "URL_STORED_UNVERIFIED"})
        elif isinstance(item, dict):
            u = item.get("url") or item.get("download_url")
            if u:
                links.append({**item, "url": u, "verification_status": "URL_STORED_UNVERIFIED"})
    return links


class SamFederalAdapter:
    source_category = SOURCE_FEDERAL
    source_name = "sam_gov_federal"

    def preflight(self, **kwargs: Any) -> dict[str, Any]:
        import os

        return {
            "source": self.source_name,
            "category": self.source_category,
            "credentials_present": bool((os.getenv("SAM_GOV_API_KEY") or "").strip()),
            "would_call_live": False,
            "routing_order": "SAM_API_LAST",
            "note": "SAM is scarce late-stage verification — not routine discovery. "
            "Use non-SAM adapters first; rare SAM sync via product_discovery with broad auth.",
            "params": {k: v for k, v in kwargs.items() if k != "api_key"},
        }

    def fetch(self, *, authorize_live: bool = False, **kwargs: Any) -> list[NormalizedOpportunity]:
        if not authorize_live:
            raise PermissionError("SAM live fetch requires authorize_live=True")
        raise NotImplementedError(
            "SAM is not the default discovery path. Use non-SAM adapters, or "
            "product_discovery.run_product_sync(authorize_live=True, authorize_broad_sam_discovery=True) "
            "only for rare controlled tests."
        )

    def normalize(self, raw: dict[str, Any]) -> NormalizedOpportunity:
        return normalize_sam_raw(raw)


class _NonSamDiscoveryAdapterBase:
    """Stub adapters for non-SAM federal discovery — no live calls in this build."""

    source_category = SOURCE_FEDERAL
    discovery_mode = "CANDIDATE_ONLY"

    def preflight(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "source": self.source_name,
            "category": self.source_category,
            "would_call_live": False,
            "LIVE_API_REQUESTS": 0,
            "candidate_policy": "Discovered fields are CANDIDATE — not VERIFIED without underlying source",
            "routing": "before_SAM_API",
            "params": kwargs,
        }

    def fetch(self, *, authorize_live: bool = False, **kwargs: Any) -> list[NormalizedOpportunity]:
        if not authorize_live:
            raise PermissionError(f"{self.source_name} live fetch requires authorize_live=True")
        # Controlled live discovery is intentionally not implemented in this scarcity realignment.
        raise NotImplementedError(
            f"{self.source_name} live discovery is ready for later controlled testing — not executed here"
        )

    def normalize(self, raw: dict[str, Any]) -> NormalizedOpportunity:
        """Normalize a candidate payload. Fields remain unverified unless evidence says otherwise."""
        evidence = raw.get("evidence_urls") or raw.get("source_urls") or []
        if not isinstance(evidence, list):
            evidence = [evidence] if evidence else []
        doc_links = []
        for u in evidence:
            if isinstance(u, str):
                doc_links.append({"url": u, "verification_status": "CANDIDATE_URL"})
            elif isinstance(u, dict) and u.get("url"):
                doc_links.append({**u, "verification_status": u.get("verification_status") or "CANDIDATE_URL"})
        return NormalizedOpportunity(
            source=SOURCE_FEDERAL,
            source_opportunity_id=str(raw.get("source_opportunity_id") or raw.get("id") or ""),
            title=str(raw.get("title") or "Untitled"),
            solicitation_number=raw.get("solicitation_number"),
            agency=raw.get("agency"),
            description=raw.get("description"),
            due_date=raw.get("due_date") if isinstance(raw.get("due_date"), date) else None,
            set_aside=raw.get("set_aside"),
            naics_code=str(raw.get("naics_code") or "") or None,
            psc_code=raw.get("psc_code"),
            link=raw.get("link"),
            document_links=doc_links,
            raw_payload={
                **raw,
                "_discovery_source": self.source_name,
                "_verification_policy": "CANDIDATE_NOT_AUTO_VERIFIED",
            },
            source_retrieved_at=now_utc().isoformat(),
        )


class OpenAIWebDiscoveryAdapter(_NonSamDiscoveryAdapterBase):
    source_name = "openai_web_discovery"


class PublicProcurementPageAdapter(_NonSamDiscoveryAdapterBase):
    source_name = "public_procurement_page"


class DirectPublicSourceAdapter(_NonSamDiscoveryAdapterBase):
    source_name = "direct_public_source"


# Register built-in adapters at import
register_source(SamFederalAdapter())
register_source(OpenAIWebDiscoveryAdapter())
register_source(PublicProcurementPageAdapter())
register_source(DirectPublicSourceAdapter())

# Placeholders for next connectors — see discovery.registry for full 50-state + platform seed
NEXT_CONNECTORS = (
    "state_procurement_portals",  # 50-state registry in discovery.registry
    "local_city_county_portals",
    "public_university_school",
    "airport_transit_utility",
    "NASPO_ValuePoint",
    "Sourcewell",
    "OMNIA",
    "HGACBuy",
    "BuyBoard",
    "fixture_html_city_bids",  # IMPLEMENTED in discovery.adapters
    "fixture_json_state_bids",
    "fixture_rss_county_bids",
    "fixture_coop_sourcewell_style",
    "fixture_federal_public_notice",
    "fixture_shared_platform_listing",
)
