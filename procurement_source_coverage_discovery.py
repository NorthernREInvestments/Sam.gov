"""Procurement Source Coverage Discovery — measure reachable public procurement universe.

Extends ProcurementSourceRegistry + SourceDiscoveryEngine + state/agency/coop seeds.
Does NOT create a parallel source database or paid-aggregator dependency.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from application_clock import now_utc
from coverage_gap_intelligence import build_coverage_gap_report
from discovery.agency_seeds import (
    FEDERAL_NON_SAM_LIVE,
    all_agencies_enriched,
    all_coops_enriched,
)
from discovery.bidnet_network import all_bidnet_networks_enriched
from discovery.selection import _pool_map
from discovery.state_matrix import STATE_MATRIX
from entity_geographic_coverage import US_STATES, entity_type_coverage, geographic_coverage
from national_discovery_constants import ENTITY_TYPES
from procurement_source_registry import (
    ProcurementSourceRegistry,
    bootstrap_registry,
    source_record,
)
from source_discovery_engine import SourceDiscoveryEngine, score_discovery_candidate, validate_source_candidate
from source_network_audit import (
    audit_source_network,
    coverage_metrics_honest,
    platform_family_inventory,
    platform_leverage_ranking,
)

# Access classification (public-first inventory)
ACCESS_PUBLIC = "PUBLIC"  # Level 1
ACCESS_FREE_REG = "FREE_REGISTRATION"  # Level 2
ACCESS_PUBLIC_PLUS_FREE_BID = "PUBLIC_LISTING_FREE_REG_TO_BID"  # Level 3
ACCESS_LIMITED_FREE = "LIMITED_FREE"  # Level 4
ACCESS_PAID = "PAID"  # Level 5 — not a dependency
ACCESS_UNKNOWN = "UNKNOWN"

BUILD_TAG = "20260920-m3-procurement-source-coverage-1"

# Paid commercial aggregators — never count as primary M3 sources
_PAID_AGGREGATOR_HOSTS = frozenset(
    {
        "demandstar.com",
        "bidsync.com",
        "periscopeholdings.com",  # paid products; public agency portals differ
        "govwin.com",
        "bloomberggovernment.com",
        "construction.com",
        "bidclerk.com",
        "onvia.com",
    }
)

# Commercial directories usable ONLY as entity discovery maps (not M3 sources)
_DIRECTORY_MAP_ONLY = frozenset({"bidnetdirect.com"})  # statewide open-bids = network portal, OK as NETWORK source

ARTIFACT_NAMES = {
    "inventory": "procurement_source_coverage_inventory.json",
    "relationships": "procurement_source_entity_relationships.json",
    "summary": "procurement_source_coverage_summary.json",
    "access": "procurement_source_access_status_summary.json",
    "states": "procurement_source_state_coverage.json",
    "gaps": "procurement_source_gap_report.json",
    "results": "procurement_source_coverage_discovery_results.json",
    "human": "procurement_source_coverage_report.md",
}


def _utc() -> str:
    return now_utc().isoformat()


def _norm_url(url: str | None) -> str:
    if not url:
        return ""
    u = str(url).strip().rstrip("/").lower()
    # Drop fragment / trivial query noise for identity
    if "?" in u and "customerorg=" not in u and "x-amz" not in u:
        u = u.split("?", 1)[0]
    return u


def _host(url: str | None) -> str:
    try:
        return (urlparse(url or "").netloc or "").lower().replace("www.", "")
    except Exception:
        return ""


def is_paid_aggregator_url(url: str | None) -> bool:
    h = _host(url)
    return any(h == p or h.endswith("." + p) for p in _PAID_AGGREGATOR_HOSTS)


def classify_access_level(row: dict[str, Any]) -> str:
    """Map existing auth/public flags → LEVEL 1–5 access taxonomy."""
    if is_paid_aggregator_url(row.get("discovery_url") or row.get("portal_url") or row.get("list_url")):
        return ACCESS_PAID

    auth = str(row.get("auth_requirement") or row.get("auth_required") or "").upper()
    health = str(row.get("health_state") or "").upper()
    notes = str(row.get("notes") or row.get("restrictions") or "").lower()
    publicly = row.get("publicly_searchable")
    docs = row.get("documents_public") if "documents_public" in row else row.get("docs_public")
    reg_cost = str(row.get("registration_cost") or "").upper()
    access_method = str(row.get("access_method") or "").upper()
    limited = bool(row.get("limited_free_tier")) or "limited free" in notes or "marketing" in notes
    free_reg_to_bid = bool(row.get("registration_required_to_bid")) or "register to bid" in notes or "register to respond" in notes
    free_reg_view = bool(row.get("registration_required_to_view")) or auth in {
        "ACCOUNT_REQUIRED",
        "LOGIN_REQUIRED",
        "REGISTRATION_REQUIRED",
    }

    if reg_cost == "PAID" or health == "PAID" or "subscription required" in notes:
        return ACCESS_PAID
    if limited or health in {"PUBLIC_METADATA_ONLY"} and "not publicly listable" in notes:
        return ACCESS_LIMITED_FREE
    if health == "REGISTRATION_REQUIRED" or (
        free_reg_view and publicly is False
    ):
        # Free account needed for useful listing
        if "hides" in notes or "marketing" in notes or limited:
            return ACCESS_LIMITED_FREE
        return ACCESS_FREE_REG
    if health == "AUTH_REQUIRED" or auth in {"LOGIN_REQUIRED"} and publicly is False:
        # Could be free or paid vendor portal — unknown unless noted free
        if "free" in notes and "registration" in notes:
            return ACCESS_FREE_REG
        if "paid" in notes or "subscription" in notes:
            return ACCESS_PAID
        return ACCESS_UNKNOWN
    if publicly is True or access_method in {"PUBLIC_HTTP", "PUBLIC_API"}:
        if free_reg_to_bid or (docs is False and "register" in notes):
            return ACCESS_PUBLIC_PLUS_FREE_BID
        if docs is True or docs is None:
            return ACCESS_PUBLIC
        return ACCESS_PUBLIC_PLUS_FREE_BID
    if publicly is False and auth in {"NONE", "", "FALSE"}:
        return ACCESS_UNKNOWN
    # Fallback from health
    if health in {"HEALTHY_PRODUCTION", "PARTIALLY_PRODUCTIVE", "DEGRADED"}:
        return ACCESS_PUBLIC if not free_reg_view else ACCESS_PUBLIC_PLUS_FREE_BID
    if health == "PUBLIC_METADATA_ONLY":
        return ACCESS_PUBLIC_PLUS_FREE_BID
    return ACCESS_UNKNOWN


def source_identity_key(row: dict[str, Any]) -> str:
    """Deduplicate by actual source identity (URL + platform), not discovery path."""
    url = _norm_url(row.get("discovery_url") or row.get("portal_url") or row.get("list_url") or row.get("canonical_base_url"))
    sid = str(row.get("source_id") or "")
    if url:
        return f"url:{url}"
    return f"id:{sid.lower()}"


def state_entity_id(state_code: str) -> str:
    st = str(state_code or "").strip().upper()
    return f"ent:state:{st}"


def entity_id_for(
    *,
    name: str,
    entity_type: str,
    state: str | None = None,
    source_hint: str | None = None,
) -> str:
    base = f"{entity_type}|{(state or '').upper()}|{name.strip().lower()}"
    digest = hashlib.sha1(base.encode("utf-8")).hexdigest()[:10]
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:48]
    return f"ent:{(entity_type or 'OTHER').lower()}:{slug}:{digest}"


def _entity_type_from_buyer(buyer_type: str | None, name: str = "") -> str:
    b = (buyer_type or "").upper()
    n = (name or "").lower()
    mapping = {
        "CITY": "CITY_MUNICIPAL",
        "MUNICIPAL": "CITY_MUNICIPAL",
        "COUNTY": "COUNTY",
        "SCHOOL_DISTRICT": "K12_SCHOOL_DISTRICT",
        "K12": "K12_SCHOOL_DISTRICT",
        "PUBLIC_UNIVERSITY": "HIGHER_EDUCATION",
        "UNIVERSITY": "HIGHER_EDUCATION",
        "COMMUNITY_COLLEGE": "HIGHER_EDUCATION",
        "AIRPORT": "AIRPORT",
        "TRANSIT": "TRANSIT",
        "PUBLIC_UTILITY": "UTILITY",
        "UTILITY": "UTILITY",
        "HOUSING": "HOUSING_AUTHORITY",
        "LIBRARY": "OTHER_PUBLIC",
        "SPECIAL_DISTRICT": "SPECIAL_DISTRICT",
        "PORT": "PORT_AUTHORITY",
        "COOPERATIVE": "COOPERATIVE",
        "STATE": "STATE",
        "FEDERAL": "FEDERAL",
        "MULTI_AGENCY_NETWORK": "OTHER_PUBLIC",
    }
    if b in mapping:
        return mapping[b]
    if "school" in n or "isd" in n or "usd" in n:
        return "K12_SCHOOL_DISTRICT"
    if "university" in n or "college" in n:
        return "HIGHER_EDUCATION"
    if "airport" in n:
        return "AIRPORT"
    if "transit" in n or "metro" in n:
        return "TRANSIT"
    if "housing" in n:
        return "HOUSING_AUTHORITY"
    if "library" in n:
        return "OTHER_PUBLIC"
    if "county" in n:
        return "COUNTY"
    if "city of" in n or "town of" in n:
        return "CITY_MUNICIPAL"
    return "OTHER_PUBLIC"


# Curated REAL public procurement portals / entities not yet in thin agency_seeds.
# Provenance: official agency URLs / well-known public bid boards (2026 inventory pass).
COVERAGE_EXPANSION_ENTITIES: list[dict[str, Any]] = [
    # Additional major cities
    {"name": "City of New York", "buyer_type": "CITY", "state_code": "NY", "procurement_url": "https://a856-cityrecord.nyc.gov/", "platform_family": "SimpleHTML", "access_hint": ACCESS_PUBLIC},
    {"name": "City of Philadelphia", "buyer_type": "CITY", "state_code": "PA", "procurement_url": "https://www.phila.gov/departments/procurement-department/", "platform_family": "SimpleHTML", "access_hint": ACCESS_PUBLIC},
    {"name": "City of San Antonio", "buyer_type": "CITY", "state_code": "TX", "procurement_url": "https://www.sanantonio.gov/purchasing", "platform_family": "Bonfire", "access_hint": ACCESS_PUBLIC_PLUS_FREE_BID},
    {"name": "City of San Diego", "buyer_type": "CITY", "state_code": "CA", "procurement_url": "https://www.sandiego.gov/purchasing", "platform_family": "PlanetBids", "access_hint": ACCESS_PUBLIC_PLUS_FREE_BID},
    {"name": "City of Dallas", "buyer_type": "CITY", "state_code": "TX", "procurement_url": "https://dallascityhall.com/departments/procurement/pages/default.aspx", "platform_family": "Bonfire", "access_hint": ACCESS_PUBLIC_PLUS_FREE_BID},
    {"name": "City of Austin", "buyer_type": "CITY", "state_code": "TX", "procurement_url": "https://financeonline.austintexas.gov/afo/account_services/solicitation/solicitations.cfm", "platform_family": "SimpleHTML", "access_hint": ACCESS_PUBLIC},
    {"name": "City of Columbus", "buyer_type": "CITY", "state_code": "OH", "procurement_url": "https://www.columbus.gov/procurement/", "platform_family": "Bonfire", "access_hint": ACCESS_PUBLIC_PLUS_FREE_BID},
    {"name": "City of Charlotte", "buyer_type": "CITY", "state_code": "NC", "procurement_url": "https://charlottenc.gov/DoingBusiness/Pages/default.aspx", "platform_family": "Bonfire", "access_hint": ACCESS_PUBLIC_PLUS_FREE_BID},
    {"name": "City of Detroit", "buyer_type": "CITY", "state_code": "MI", "procurement_url": "https://www.detroitmi.gov/government/departments/office-contracting-and-procurement", "platform_family": "Bonfire", "access_hint": ACCESS_PUBLIC_PLUS_FREE_BID},
    {"name": "City of Portland", "buyer_type": "CITY", "state_code": "OR", "procurement_url": "https://www.portland.gov/omf/brfs/procurement", "platform_family": "SimpleHTML", "access_hint": ACCESS_PUBLIC},
    {"name": "City of Minneapolis", "buyer_type": "CITY", "state_code": "MN", "procurement_url": "https://www.minneapolismn.gov/government/departments/finance/procurement/", "platform_family": "SimpleHTML", "access_hint": ACCESS_PUBLIC},
    {"name": "City of Nashville", "buyer_type": "CITY", "state_code": "TN", "procurement_url": "https://www.nashville.gov/departments/finance/procurement", "platform_family": "Bonfire", "access_hint": ACCESS_PUBLIC_PLUS_FREE_BID},
    # Counties
    {"name": "Los Angeles County", "buyer_type": "COUNTY", "state_code": "CA", "procurement_url": "https://lacounty.gov/business/doing-business-with-the-county/", "platform_family": "PlanetBids", "access_hint": ACCESS_PUBLIC_PLUS_FREE_BID},
    {"name": "Orange County CA", "buyer_type": "COUNTY", "state_code": "CA", "procurement_url": "https://www.ocgov.com/gov/ceo/procure", "platform_family": "PlanetBids", "access_hint": ACCESS_PUBLIC_PLUS_FREE_BID},
    {"name": "Fairfax County", "buyer_type": "COUNTY", "state_code": "VA", "procurement_url": "https://www.fairfaxcounty.gov/procurement/", "platform_family": "Bonfire", "access_hint": ACCESS_PUBLIC_PLUS_FREE_BID},
    {"name": "Montgomery County MD", "buyer_type": "COUNTY", "state_code": "MD", "procurement_url": "https://www.montgomerycountymd.gov/PRO/", "platform_family": "Bonfire", "access_hint": ACCESS_PUBLIC_PLUS_FREE_BID},
    {"name": "Clark County NV", "buyer_type": "COUNTY", "state_code": "NV", "procurement_url": "https://www.clarkcountynv.gov/government/departments/finance/purchasing/", "platform_family": "Bonfire", "access_hint": ACCESS_PUBLIC_PLUS_FREE_BID},
    {"name": "Allegheny County", "buyer_type": "COUNTY", "state_code": "PA", "procurement_url": "https://www.alleghenycounty.us/purchasing/", "platform_family": "SimpleHTML", "access_hint": ACCESS_PUBLIC},
    # K-12
    {"name": "Chicago Public Schools", "buyer_type": "SCHOOL_DISTRICT", "state_code": "IL", "procurement_url": "https://www.cps.edu/about/departments/procurement/", "platform_family": "BidNet", "access_hint": ACCESS_PUBLIC_PLUS_FREE_BID},
    {"name": "Miami-Dade County Public Schools", "buyer_type": "SCHOOL_DISTRICT", "state_code": "FL", "procurement_url": "https://www3.dadeschools.net/home", "platform_family": "BidNet", "access_hint": ACCESS_PUBLIC_PLUS_FREE_BID},
    {"name": "Dallas Independent School District", "buyer_type": "SCHOOL_DISTRICT", "state_code": "TX", "procurement_url": "https://www.dallasisd.org/Page/547", "platform_family": "Bonfire", "access_hint": ACCESS_PUBLIC_PLUS_FREE_BID},
    {"name": "Clark County School District", "buyer_type": "SCHOOL_DISTRICT", "state_code": "NV", "procurement_url": "https://www.ccsd.net/departments/purchasing/", "platform_family": "Bonfire", "access_hint": ACCESS_PUBLIC_PLUS_FREE_BID},
    {"name": "Fairfax County Public Schools", "buyer_type": "SCHOOL_DISTRICT", "state_code": "VA", "procurement_url": "https://www.fcps.edu/department/office-procurement-services", "platform_family": "Bonfire", "access_hint": ACCESS_PUBLIC_PLUS_FREE_BID},
    {"name": "New York City Department of Education", "buyer_type": "SCHOOL_DISTRICT", "state_code": "NY", "procurement_url": "https://www.schools.nyc.gov/about-us/funding/doing-business-with-the-doe", "platform_family": "SimpleHTML", "access_hint": ACCESS_PUBLIC},
    # Higher ed
    {"name": "Pennsylvania State University", "buyer_type": "PUBLIC_UNIVERSITY", "state_code": "PA", "procurement_url": "https://purchasing.psu.edu/", "platform_family": "SimpleHTML", "access_hint": ACCESS_PUBLIC},
    {"name": "Ohio State University", "buyer_type": "PUBLIC_UNIVERSITY", "state_code": "OH", "procurement_url": "https://busfin.osu.edu/buy-ohio-state", "platform_family": "SimpleHTML", "access_hint": ACCESS_PUBLIC},
    {"name": "University of Washington", "buyer_type": "PUBLIC_UNIVERSITY", "state_code": "WA", "procurement_url": "https://finance.uw.edu/ps/", "platform_family": "SimpleHTML", "access_hint": ACCESS_PUBLIC},
    {"name": "University of Florida", "buyer_type": "PUBLIC_UNIVERSITY", "state_code": "FL", "procurement_url": "https://procurement.ufl.edu/", "platform_family": "SimpleHTML", "access_hint": ACCESS_PUBLIC},
    {"name": "Georgia Institute of Technology", "buyer_type": "PUBLIC_UNIVERSITY", "state_code": "GA", "procurement_url": "https://www.procurement.gatech.edu/", "platform_family": "SimpleHTML", "access_hint": ACCESS_PUBLIC},
    {"name": "Arizona State University", "buyer_type": "PUBLIC_UNIVERSITY", "state_code": "AZ", "procurement_url": "https://cfo.asu.edu/purchasing", "platform_family": "SimpleHTML", "access_hint": ACCESS_PUBLIC},
    # Airports / transit / ports
    {"name": "Hartsfield-Jackson Atlanta International Airport", "buyer_type": "AIRPORT", "state_code": "GA", "procurement_url": "https://www.atl.com/business-opportunities/", "platform_family": "Bonfire", "access_hint": ACCESS_PUBLIC_PLUS_FREE_BID},
    {"name": "O'Hare / Chicago Department of Aviation", "buyer_type": "AIRPORT", "state_code": "IL", "procurement_url": "https://www.flychicago.com/business/opportunities/Pages/default.aspx", "platform_family": "BidNet", "access_hint": ACCESS_PUBLIC_PLUS_FREE_BID},
    {"name": "Port Authority of New York and New Jersey", "buyer_type": "PORT", "state_code": "NY", "procurement_url": "https://www.panynj.gov/port-authority/en/business-opportunities/solicitations-advertisements.html", "platform_family": "SimpleHTML", "access_hint": ACCESS_PUBLIC},
    {"name": "Port of Los Angeles", "buyer_type": "PORT", "state_code": "CA", "procurement_url": "https://www.portoflosangeles.org/business/contracting-opportunities", "platform_family": "PlanetBids", "access_hint": ACCESS_PUBLIC_PLUS_FREE_BID},
    {"name": "Washington Metropolitan Area Transit Authority", "buyer_type": "TRANSIT", "state_code": "DC", "procurement_url": "https://www.wmata.com/business/procurement/", "platform_family": "SimpleHTML", "access_hint": ACCESS_PUBLIC},
    {"name": "LA Metro", "buyer_type": "TRANSIT", "state_code": "CA", "procurement_url": "https://www.metro.net/about/business/", "platform_family": "PlanetBids", "access_hint": ACCESS_PUBLIC_PLUS_FREE_BID},
    # Housing / libraries / special
    {"name": "New York City Housing Authority", "buyer_type": "HOUSING", "state_code": "NY", "procurement_url": "https://www.nyc.gov/site/nycha/business/procurement.page", "platform_family": "SimpleHTML", "access_hint": ACCESS_PUBLIC},
    {"name": "Los Angeles Public Library", "buyer_type": "LIBRARY", "state_code": "CA", "procurement_url": "https://www.lapl.org/about-lapl/business-opportunities", "platform_family": "SimpleHTML", "access_hint": ACCESS_PUBLIC},
    {"name": "Metropolitan Water District of Southern California", "buyer_type": "SPECIAL_DISTRICT", "state_code": "CA", "procurement_url": "https://www.mwdh2o.com/doing-business/", "platform_family": "PlanetBids", "access_hint": ACCESS_PUBLIC_PLUS_FREE_BID},
    # Cooperative / education coops (public solicitation pages)
    {"name": "AEPA — Association of Educational Purchasing Agencies", "buyer_type": "COOPERATIVE", "state_code": None, "procurement_url": "https://aepacoop.org/solicitations/", "platform_family": "SimpleHTML", "access_hint": ACCESS_PUBLIC, "government_level": "COOPERATIVE"},
    {"name": "CREC — Capitol Region Education Council Bids", "buyer_type": "COOPERATIVE", "state_code": "CT", "procurement_url": "https://www.crec.org/about/business-office", "platform_family": "SimpleHTML", "access_hint": ACCESS_PUBLIC, "government_level": "COOPERATIVE"},
    {"name": "E&I Cooperative Services Solicitations", "buyer_type": "COOPERATIVE", "state_code": None, "procurement_url": "https://www.eandi.org/contract-opportunities/", "platform_family": "SimpleHTML", "access_hint": ACCESS_PUBLIC, "government_level": "COOPERATIVE"},
    # Federal award/history / forecast adjacent (public)
    {"name": "USAspending.gov", "buyer_type": "FEDERAL", "state_code": None, "procurement_url": "https://www.usaspending.gov/", "platform_family": "JSON", "access_hint": ACCESS_PUBLIC, "government_level": "FEDERAL", "source_type": "AWARD_HISTORY"},
    {"name": "GSA eBuy / Schedules public pages", "buyer_type": "FEDERAL", "state_code": None, "procurement_url": "https://www.gsa.gov/buy-through-us", "platform_family": "SimpleHTML", "access_hint": ACCESS_LIMITED_FREE, "government_level": "FEDERAL", "source_type": "FORECAST_LEAD", "notes": "Lead/forecast surface — not primary open solicitation feed"},
]

# Estimated national K-12 universe (authoritative ballpark — NCES ~13k districts). Not used as invented coverage %.
K12_NATIONAL_ESTIMATE_NOTE = (
    "NCES reports roughly 13,000+ public school districts nationally. "
    "M3 does not invent a coverage percentage; report only discovered entities vs this estimate when cited."
)
K12_NATIONAL_ESTIMATE_RANGE = {"low": 13000, "high": 14000, "source": "NCES public school district counts (approximate)"}


def extract_bidnet_buyer_names(html: str, *, state_code: str | None = None) -> list[dict[str, Any]]:
    """Extract buyer/agency names from BidNet open-bids HTML when present."""
    if not html:
        return []
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    # Common BidNet card patterns
    patterns = [
        r'class="[^"]*agency[^"]*"[^>]*>([^<]{3,120})<',
        r'data-agency="([^"]{3,120})"',
        r"<span[^>]*agency[^>]*>([^<]{3,120})</span>",
        r"(?:Agency|Organization|Buyer)\s*[:\-]\s*([A-Za-z0-9][^<\n]{2,100})",
    ]
    for pat in patterns:
        for m in re.finditer(pat, html, re.I):
            name = re.sub(r"\s+", " ", m.group(1)).strip()
            if len(name) < 4 or name.lower() in seen:
                continue
            if re.search(r"login|register|password|cookie|javascript", name, re.I):
                continue
            seen.add(name.lower())
            found.append(
                {
                    "name": name[:200],
                    "entity_type": _entity_type_from_buyer(None, name),
                    "state": state_code,
                    "provenance": "bidnet_open_bids_html",
                    "parent_platform": "BidNet",
                }
            )
            if len(found) >= 80:
                return found
    return found


def _probe_url(url: str, *, source_id: str = "coverage_probe") -> dict[str, Any]:
    """Public-first lightweight probe — reuses portal live_http_get when available."""
    try:
        from portal_document_resolver import live_http_get

        hit = live_http_get(url, source_id=source_id)
        text = hit.get("text") or ""
        status = hit.get("status_code")
        validation = validate_source_candidate(listing_html=text, status_code=status, url=url)
        health = "UNKNOWN"
        if status and status in {401, 403}:
            health = "AUTH_REQUIRED"
        elif status and status >= 500:
            health = "UNAVAILABLE"
        elif status and status >= 400:
            health = "UNAVAILABLE"
        elif validation.get("state") == "AUTH_REQUIRED":
            health = "AUTH_REQUIRED"
        elif validation.get("state") == "ANTI_AUTOMATION":
            health = "BLOCKED"
        elif validation.get("promote"):
            health = "HEALTHY"
        elif validation.get("state") == "NOT_A_SOLICITATION_SOURCE":
            health = "DEGRADED"
        elif hit.get("ok") and status == 200:
            # 200 alone ≠ healthy (Iowa/Cheyenne lesson)
            if re.search(r"sign\s*in|log\s*in|register", text[:4000], re.I) and validation.get("signals", 0) == 0:
                health = "AUTH_REQUIRED"
            else:
                health = "DEGRADED" if not validation.get("promote") else "HEALTHY"
        return {
            "ok": bool(hit.get("ok")),
            "status_code": status,
            "health": health,
            "validation": validation,
            "bytes": len(text),
            "text": text[:500000] if text else "",
            "url": url,
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "health": "UNKNOWN", "error": str(exc)[:200], "url": url}


class ProcurementSourceCoverageDiscovery:
    """Inventory reachable sources/entities using existing M3 discovery architecture."""

    def __init__(
        self,
        registry: ProcurementSourceRegistry | None = None,
        *,
        artifacts_dir: Path | None = None,
        live_probe: bool = True,
        max_live_probes: int = 40,
    ) -> None:
        self.registry = registry or bootstrap_registry()
        self.engine = SourceDiscoveryEngine(self.registry)
        self.artifacts_dir = artifacts_dir or Path(__file__).resolve().parent / "artifacts"
        self.live_probe = live_probe
        self.max_live_probes = max_live_probes
        self.entities: dict[str, dict[str, Any]] = {}
        self.relationships: list[dict[str, Any]] = []
        self.sources: dict[str, dict[str, Any]] = {}
        self.probe_results: list[dict[str, Any]] = []
        self.rejected_paid: list[dict[str, Any]] = []
        self.discovery_stats: dict[str, Any] = {}

    def _register_entity(self, ent: dict[str, Any]) -> str:
        eid = ent.get("entity_id") or entity_id_for(
            name=ent["name"],
            entity_type=ent.get("entity_type") or "OTHER_PUBLIC",
            state=ent.get("state"),
        )
        existing = self.entities.get(eid)
        if existing:
            # Merge portals without overwrite
            portals = list(existing.get("procurement_portals") or [])
            for p in ent.get("procurement_portals") or []:
                if p and p not in portals:
                    portals.append(p)
            existing["procurement_portals"] = portals
            for k, v in ent.items():
                if k in {"procurement_portals", "entity_id"}:
                    continue
                if v is not None and existing.get(k) in (None, "", "UNKNOWN"):
                    existing[k] = v
            return eid
        row = dict(ent)
        row["entity_id"] = eid
        row.setdefault("procurement_portals", [])
        self.entities[eid] = row
        return eid

    def _upsert_source(self, rec: dict[str, Any]) -> str | None:
        url = rec.get("discovery_url") or rec.get("portal_url") or rec.get("list_url")
        if is_paid_aggregator_url(url):
            self.rejected_paid.append(
                {
                    "url": url,
                    "name": rec.get("source_name"),
                    "reason": "paid_aggregator_excluded_as_primary_source",
                }
            )
            return None
        key = source_identity_key(rec)
        access = classify_access_level(rec)
        rec = {
            **rec,
            "access_level": access,
            "public_listing_access": access
            in {ACCESS_PUBLIC, ACCESS_PUBLIC_PLUS_FREE_BID, ACCESS_LIMITED_FREE},
            "registration_required": access
            in {ACCESS_FREE_REG, ACCESS_PUBLIC_PLUS_FREE_BID, ACCESS_LIMITED_FREE},
            "registration_cost": "FREE"
            if access in {ACCESS_FREE_REG, ACCESS_PUBLIC_PLUS_FREE_BID, ACCESS_LIMITED_FREE}
            else ("PAID" if access == ACCESS_PAID else ("NONE" if access == ACCESS_PUBLIC else "UNKNOWN")),
            "last_verified": rec.get("last_verified") or _utc(),
            "identity_key": key,
        }
        if key in self.sources:
            prev = self.sources[key]
            # Preserve provenance paths
            paths = list(prev.get("discovery_paths") or [])
            path = rec.get("provenance") or rec.get("discovery_path")
            if path and path not in paths:
                paths.append(path)
            prev["discovery_paths"] = paths
            for k, v in rec.items():
                if v is not None and prev.get(k) in (None, "", "UNKNOWN"):
                    prev[k] = v
            # Prefer stronger access evidence from later ingest paths
            for flag in (
                "publicly_searchable",
                "documents_public",
                "registration_required_to_bid",
                "registration_required_to_view",
                "limited_free_tier",
                "notes",
            ):
                if rec.get(flag) is not None:
                    prev[flag] = rec[flag]
            prev["access_level"] = classify_access_level(prev)
            prev["public_listing_access"] = prev["access_level"] in {
                ACCESS_PUBLIC,
                ACCESS_PUBLIC_PLUS_FREE_BID,
                ACCESS_LIMITED_FREE,
            }
            prev["registration_required"] = prev["access_level"] in {
                ACCESS_FREE_REG,
                ACCESS_PUBLIC_PLUS_FREE_BID,
                ACCESS_LIMITED_FREE,
            }
            # Merge entity links
            ents = list(prev.get("entities_covered") or [])
            for e in rec.get("entities_covered") or []:
                if e not in ents:
                    ents.append(e)
            prev["entities_covered"] = ents
            return prev.get("source_id")
        rec.setdefault("discovery_paths", [rec.get("provenance")] if rec.get("provenance") else [])
        rec.setdefault("entities_covered", [])
        self.sources[key] = rec
        # Also mirror into registry when new
        sid = rec.get("source_id")
        if sid and not self.registry.get(sid):
            try:
                self.registry.upsert(
                    source_record(
                        source_id=sid,
                        source_name=rec.get("source_name") or sid,
                        discovery_url=url,
                        platform_family=rec.get("source_platform") or rec.get("platform_family"),
                        government_level=rec.get("government_level"),
                        entity_type=rec.get("entity_type") or "OTHER_PUBLIC",
                        geographic_scope=rec.get("state"),
                        jurisdiction=rec.get("state"),
                        health_state=rec.get("health_state") or "DISCOVERED_UNVALIDATED",
                        auth_requirement=rec.get("auth_requirement") or "NONE",
                        provenance=rec.get("provenance") or "coverage_discovery",
                        access_method=rec.get("access_method") or "PUBLIC_HTTP",
                        notes=rec.get("notes"),
                    )
                )
            except Exception:
                pass
        return sid

    def _link(self, entity_id: str, source_id: str | None, *, portal_url: str | None = None, platform: str | None = None) -> None:
        if not source_id and not portal_url:
            return
        self.relationships.append(
            {
                "entity_id": entity_id,
                "source_id": source_id,
                "portal_url": portal_url,
                "source_platform": platform,
                "relationship": "PROCURES_VIA",
                "at": _utc(),
            }
        )
        if source_id:
            for s in self.sources.values():
                if s.get("source_id") == source_id:
                    ents = list(s.get("entities_covered") or [])
                    if entity_id not in ents:
                        ents.append(entity_id)
                    s["entities_covered"] = ents
                    break
        ent = self.entities.get(entity_id)
        if ent and portal_url:
            portals = list(ent.get("procurement_portals") or [])
            if portal_url not in portals:
                portals.append(portal_url)
            ent["procurement_portals"] = portals

    def ingest_registry_and_seeds(self) -> dict[str, Any]:
        """Load existing registry + state/agency/coop/bidnet/fed seeds into inventory."""
        counts = Counter()
        # Registry
        for s in self.registry.all_sources():
            st = s.get("jurisdiction") or s.get("geographic_scope")
            rec = {
                "source_id": s["source_id"],
                "source_name": s.get("source_name"),
                "source_type": s.get("government_level") or "PROCUREMENT_PORTAL",
                "government_level": s.get("government_level"),
                "state": st,
                "entity_type": s.get("entity_type"),
                "portal_url": s.get("discovery_url") or s.get("canonical_base_url"),
                "discovery_url": s.get("discovery_url"),
                "canonical_base_url": s.get("canonical_base_url"),
                "source_platform": s.get("platform_family") or s.get("source_family"),
                "platform_family": s.get("platform_family"),
                "auth_requirement": s.get("auth_requirement"),
                "health_state": s.get("health_state"),
                "access_method": s.get("access_method"),
                "api_available": "API" in str(s.get("access_method") or "").upper(),
                "provenance": s.get("provenance") or "procurement_source_registry",
                "notes": s.get("notes"),
                "publicly_searchable": None,
            }
            self._upsert_source(rec)
            counts["registry"] += 1
            # Entity for named buyer sources
            if s.get("government_level") in {"STATE", "LOCAL", "FEDERAL", "COOPERATIVE"} or s.get("entity_type"):
                et = s.get("entity_type") or "OTHER_PUBLIC"
                if s.get("government_level") == "NETWORK":
                    continue  # networks cover many entities — handled separately
                if s.get("government_level") == "STATE" and st:
                    st_code = str(st).strip().upper()
                    if len(st_code) == 2 and st_code.isalpha():
                        eid = self._register_entity(
                            {
                                "entity_id": state_entity_id(st_code),
                                "name": f"State of {st_code}",
                                "entity_type": "STATE",
                                "state": st_code,
                                "government_level": "STATE",
                                "provenance": "registry",
                            }
                        )
                    else:
                        eid = self._register_entity(
                            {
                                "name": s.get("source_name") or s["source_id"],
                                "entity_type": "STATE",
                                "state": st,
                                "government_level": "STATE",
                                "provenance": "registry",
                            }
                        )
                else:
                    eid = self._register_entity(
                        {
                            "name": s.get("source_name") or s["source_id"],
                            "entity_type": et,
                            "state": st,
                            "government_level": s.get("government_level"),
                            "provenance": "registry",
                        }
                    )
                self._link(eid, s["source_id"], portal_url=s.get("discovery_url"), platform=s.get("platform_family"))

        # State matrix
        for sm in STATE_MATRIX:
            sid = f"state_{sm['state'].lower()}"
            rec = {
                "source_id": sid,
                "source_name": sm.get("portal_name") or sm.get("name"),
                "source_type": "STATE_PROCUREMENT_PORTAL",
                "government_level": "STATE",
                "state": sm["state"],
                "entity_type": "STATE",
                "portal_url": sm.get("list_url"),
                "discovery_url": sm.get("list_url"),
                "source_platform": sm.get("platform_family"),
                "platform_family": sm.get("platform_family"),
                "publicly_searchable": sm.get("publicly_searchable"),
                "documents_public": sm.get("docs_public"),
                "auth_requirement": "LOGIN_REQUIRED" if sm.get("auth_required") else "NONE",
                "registration_required_to_bid": bool(sm.get("auth_required") is False and "register" in str(sm.get("restrictions") or "").lower()),
                "notes": sm.get("restrictions"),
                "provenance": "state_matrix",
                "health_state": None,
            }
            self._upsert_source(rec)
            eid = self._register_entity(
                {
                    "entity_id": state_entity_id(sm["state"]),
                    "name": f"State of {sm['name']}",
                    "entity_type": "STATE",
                    "state": sm["state"],
                    "government_level": "STATE",
                    "provenance": "state_matrix",
                }
            )
            self._link(eid, sid, portal_url=sm.get("list_url"), platform=sm.get("platform_family"))
            counts["state_matrix"] += 1

        # Agencies
        for a in all_agencies_enriched():
            url = a.get("procurement_url") or a.get("list_url")
            sid = a.get("source_id") or f"agency_{a.get('agency_key')}"
            et = _entity_type_from_buyer(a.get("buyer_type") or a.get("jurisdiction"), a.get("name") or "")
            rec = {
                "source_id": sid,
                "source_name": a.get("name"),
                "source_type": "LOCAL_PROCUREMENT_PORTAL",
                "government_level": "LOCAL",
                "state": a.get("state_code"),
                "entity_type": et,
                "portal_url": url,
                "discovery_url": url,
                "source_platform": a.get("platform_family"),
                "platform_family": a.get("platform_family"),
                "auth_requirement": "LOGIN_REQUIRED" if a.get("auth_required") else "NONE",
                "notes": a.get("restrictions"),
                "provenance": "agency_seeds",
                "limited_free_tier": bool(a.get("auth_required")),
            }
            self._upsert_source(rec)
            eid = self._register_entity(
                {
                    "name": a.get("name") or sid,
                    "entity_type": et,
                    "state": a.get("state_code"),
                    "government_level": "LOCAL",
                    "city": a.get("city"),
                    "provenance": "agency_seeds",
                }
            )
            self._link(eid, sid, portal_url=url, platform=a.get("platform_family"))
            counts["agency_seeds"] += 1

        # Coops
        for c in all_coops_enriched():
            url = c.get("list_url")
            sid = c.get("source_id")
            rec = {
                "source_id": sid,
                "source_name": c.get("name"),
                "source_type": "COOPERATIVE",
                "government_level": "COOPERATIVE",
                "state": None,
                "entity_type": "COOPERATIVE",
                "portal_url": url,
                "discovery_url": url,
                "source_platform": "COOPERATIVE",
                "platform_family": "COOPERATIVE",
                "publicly_searchable": True,
                "notes": c.get("note"),
                "provenance": "cooperative_seeds",
            }
            self._upsert_source(rec)
            eid = self._register_entity(
                {
                    "name": c.get("name") or sid,
                    "entity_type": "COOPERATIVE",
                    "government_level": "COOPERATIVE",
                    "provenance": "cooperative_seeds",
                }
            )
            self._link(eid, sid, portal_url=url, platform="COOPERATIVE")
            counts["coops"] += 1

        # BidNet networks — platform sources covering many local entities
        for n in all_bidnet_networks_enriched():
            sid = n.get("source_id")
            url = n.get("list_url")
            rec = {
                "source_id": sid,
                "source_name": n.get("name"),
                "source_type": "MULTI_AGENCY_NETWORK",
                "government_level": "NETWORK",
                "state": n.get("state_code"),
                "entity_type": "OTHER_PUBLIC",
                "portal_url": url,
                "discovery_url": url,
                "source_platform": "BidNet",
                "platform_family": "BidNet",
                "publicly_searchable": True,
                "documents_public": False,
                "registration_required_to_bid": True,
                "notes": "Statewide BidNet Direct open-bids network — public metadata; packages often gated",
                "provenance": "bidnet_network",
                "coverage_count_if_known": None,  # do not invent agency counts
            }
            self._upsert_source(rec)
            counts["bidnet_networks"] += 1

        # Federal
        for f in FEDERAL_NON_SAM_LIVE:
            if not f.get("list_url"):
                continue
            sid = f.get("source_id")
            rec = {
                "source_id": sid,
                "source_name": f.get("name"),
                "source_type": f.get("notice_type") or "FEDERAL",
                "government_level": "FEDERAL",
                "entity_type": "FEDERAL",
                "portal_url": f.get("list_url"),
                "discovery_url": f.get("list_url"),
                "source_platform": f.get("platform_family"),
                "platform_family": f.get("platform_family"),
                "api_available": bool(f.get("api_source")),
                "publicly_searchable": True,
                "notes": f.get("note"),
                "provenance": "federal_non_sam_live",
                "access_method": "PUBLIC_API" if f.get("api_source") else "PUBLIC_HTTP",
            }
            self._upsert_source(rec)
            eid = self._register_entity(
                {
                    "name": f.get("name") or sid,
                    "entity_type": "FEDERAL",
                    "government_level": "FEDERAL",
                    "provenance": "federal_non_sam_live",
                }
            )
            self._link(eid, sid, portal_url=f.get("list_url"), platform=f.get("platform_family"))
            counts["federal"] += 1

        self.discovery_stats["seed_ingest"] = dict(counts)
        return counts

    def ingest_coverage_expansion(self) -> dict[str, Any]:
        """Register curated real public entity portals via SourceDiscoveryEngine patterns."""
        added = 0
        skipped_dup = 0
        hits = []
        for row in COVERAGE_EXPANSION_ENTITIES:
            url = row.get("procurement_url")
            if not url:
                continue
            scored = score_discovery_candidate(url, row.get("name") or "", "procurement bid solicitation")
            if not scored["accept"] and row.get("source_type") not in {"AWARD_HISTORY", "FORECAST_LEAD"}:
                continue
            et = _entity_type_from_buyer(row.get("buyer_type"), row.get("name") or "")
            sid = "cov_" + hashlib.sha1(url.encode()).hexdigest()[:12]
            level = row.get("government_level")
            if not level:
                if et == "FEDERAL":
                    level = "FEDERAL"
                elif et == "STATE":
                    level = "STATE"
                elif et == "COOPERATIVE":
                    level = "COOPERATIVE"
                else:
                    level = "LOCAL"
            rec = {
                "source_id": sid,
                "source_name": f"{row['name']} Procurement",
                "source_type": row.get("source_type") or "PROCUREMENT_PORTAL",
                "government_level": level,
                "state": row.get("state_code"),
                "entity_type": et,
                "portal_url": url,
                "discovery_url": url,
                "source_platform": row.get("platform_family"),
                "platform_family": row.get("platform_family"),
                "publicly_searchable": row.get("access_hint")
                in {ACCESS_PUBLIC, ACCESS_PUBLIC_PLUS_FREE_BID},
                "registration_required_to_bid": row.get("access_hint") == ACCESS_PUBLIC_PLUS_FREE_BID,
                "limited_free_tier": row.get("access_hint") == ACCESS_LIMITED_FREE,
                "notes": row.get("notes"),
                "provenance": "coverage_expansion_catalog",
                "access_hint": row.get("access_hint"),
            }
            before = len(self.sources)
            self._upsert_source(rec)
            if len(self.sources) == before:
                skipped_dup += 1
            else:
                added += 1
            eid = self._register_entity(
                {
                    "name": row["name"],
                    "entity_type": et,
                    "state": row.get("state_code"),
                    "government_level": level,
                    "provenance": "coverage_expansion_catalog",
                }
            )
            self._link(eid, sid, portal_url=url, platform=row.get("platform_family"))
            hits.append({"url": url, "title": row["name"], "snippet": "public procurement portal", "entity_type": et})

        # Also feed SourceDiscoveryEngine for registry candidate tracking
        eng = self.engine.ingest_search_hits(hits)
        self.discovery_stats["coverage_expansion"] = {
            "added_sources": added,
            "duplicate_skipped": skipped_dup,
            "engine": eng,
        }
        return self.discovery_stats["coverage_expansion"]

    def expand_bidnet_entities_from_live(self, *, max_networks: int = 8) -> dict[str, Any]:
        """Public HTML entity discovery from BidNet open-bids pages (sample of states)."""
        if not self.live_probe:
            return {"skipped": True, "reason": "live_probe_disabled"}
        networks = all_bidnet_networks_enriched()
        # Prefer states already known productive + geographic diversity
        priority = {"IL", "FL", "TX", "CA", "NY", "PA", "OH", "GA", "NC", "MI"}
        ordered = sorted(
            networks,
            key=lambda n: (0 if n.get("state_code") in priority else 1, n.get("state_code") or ""),
        )
        extracted = 0
        probed = 0
        for n in ordered[:max_networks]:
            url = n.get("list_url")
            if not url:
                continue
            probed += 1
            result = _probe_url(url, source_id=str(n.get("source_id") or "bidnet"))
            # Do not retain full HTML in probe log
            text = result.pop("text", "") or ""
            self.probe_results.append({**result, "source_id": n.get("source_id"), "kind": "bidnet_entity_expand"})
            buyers = extract_bidnet_buyer_names(text, state_code=n.get("state_code"))
            for b in buyers:
                eid = self._register_entity(
                    {
                        "name": b["name"],
                        "entity_type": b.get("entity_type") or "OTHER_PUBLIC",
                        "state": b.get("state") or n.get("state_code"),
                        "government_level": "LOCAL",
                        "provenance": "bidnet_open_bids_html",
                        "parent_platform": "BidNet",
                    }
                )
                self._link(
                    eid,
                    n.get("source_id"),
                    portal_url=url,
                    platform="BidNet",
                )
                extracted += 1
        out = {"networks_probed": probed, "entities_extracted": extracted}
        self.discovery_stats["bidnet_entity_expand"] = out
        return out

    def probe_sample_sources(self) -> dict[str, Any]:
        """Probe a diverse sample for health/access — not full production crawl."""
        if not self.live_probe:
            return {"skipped": True}
        # Diverse sample: federal, states, local, coop, network
        candidates: list[dict[str, Any]] = []
        for s in self.sources.values():
            url = s.get("discovery_url") or s.get("portal_url")
            if not url:
                continue
            candidates.append(s)
        # Prioritize unverified / important
        def _prio(s: dict[str, Any]) -> tuple:
            lvl = s.get("government_level") or ""
            order = {"FEDERAL": 0, "STATE": 1, "COOPERATIVE": 2, "LOCAL": 3, "NETWORK": 4}.get(str(lvl), 5)
            return (order, s.get("source_id") or "")

        candidates.sort(key=_prio)
        seen_hosts: set[str] = set()
        selected = []
        for s in candidates:
            h = _host(s.get("discovery_url") or s.get("portal_url"))
            # Limit per-host to avoid BidNet stampede
            if h in seen_hosts and str(s.get("government_level")) == "NETWORK":
                continue
            selected.append(s)
            seen_hosts.add(h)
            if len(selected) >= self.max_live_probes:
                break

        healthy = failed = auth = blocked = 0
        for s in selected:
            url = s.get("discovery_url") or s.get("portal_url")
            result = _probe_url(str(url), source_id=str(s.get("source_id") or "probe"))
            result.pop("text", None)
            result["source_id"] = s.get("source_id")
            result["kind"] = "coverage_probe"
            self.probe_results.append(result)
            h = result.get("health")
            if h == "HEALTHY":
                healthy += 1
            elif h == "AUTH_REQUIRED":
                auth += 1
            elif h == "BLOCKED":
                blocked += 1
            else:
                failed += 1
            # Annotate inventory source
            key = s.get("identity_key")
            if key and key in self.sources:
                self.sources[key]["probe_health"] = h
                self.sources[key]["probe_status_code"] = result.get("status_code")
                if h == "HEALTHY" and not self.sources[key].get("health_state"):
                    self.sources[key]["health_state"] = "HEALTHY_PRODUCTION"
                elif h == "AUTH_REQUIRED":
                    self.sources[key]["health_state"] = "AUTH_REQUIRED"
        out = {
            "probed": len(selected),
            "healthy": healthy,
            "auth_required": auth,
            "blocked": blocked,
            "failed_or_degraded": failed,
        }
        self.discovery_stats["live_probe"] = out
        return out

    def build_reports(self) -> dict[str, Any]:
        sources = list(self.sources.values())
        entities = list(self.entities.values())
        access_counts = Counter(s.get("access_level") or ACCESS_UNKNOWN for s in sources)
        by_level = Counter(s.get("government_level") or "UNKNOWN" for s in sources)
        by_entity_type = Counter(e.get("entity_type") or "OTHER_PUBLIC" for e in entities)
        by_platform = Counter(s.get("source_platform") or s.get("platform_family") or "UNKNOWN" for s in sources)

        def _ent_count(*types: str) -> int:
            want = {t.upper() for t in types}
            return sum(1 for e in entities if str(e.get("entity_type") or "").upper() in want)

        states_with_state_portal = {
            s.get("state") for s in sources if s.get("government_level") == "STATE" and s.get("state")
        }
        states_with_local = {
            e.get("state") for e in entities if e.get("government_level") == "LOCAL" and e.get("state")
        }
        states_with_k12 = {
            e.get("state") for e in entities if e.get("entity_type") == "K12_SCHOOL_DISTRICT" and e.get("state")
        }
        states_with_hed = {
            e.get("state") for e in entities if e.get("entity_type") == "HIGHER_EDUCATION" and e.get("state")
        }

        state_table = []
        for st in US_STATES:
            st_sources = [s for s in sources if s.get("state") == st]
            st_ents = [e for e in entities if e.get("state") == st]
            platforms = sorted(
                {
                    str(s.get("source_platform") or s.get("platform_family"))
                    for s in st_sources
                    if s.get("source_platform") or s.get("platform_family")
                }
            )
            gaps = []
            if st not in states_with_state_portal:
                gaps.append("NO_STATE_PORTAL_IN_INVENTORY")
            if not any(e.get("entity_type") == "K12_SCHOOL_DISTRICT" for e in st_ents):
                gaps.append("NO_K12_ENTITY")
            if not any(e.get("entity_type") == "HIGHER_EDUCATION" for e in st_ents):
                gaps.append("NO_HIGHER_ED_ENTITY")
            if not any(e.get("entity_type") == "COUNTY" for e in st_ents):
                gaps.append("NO_COUNTY_ENTITY")
            state_table.append(
                {
                    "state": st,
                    "state_procurement_source_found": st in states_with_state_portal,
                    "local_procurement_sources_found": any(
                        s.get("government_level") == "LOCAL" for s in st_sources
                    )
                    or any(e.get("government_level") == "LOCAL" for e in st_ents),
                    "school_procurement_sources_found": any(
                        e.get("entity_type") == "K12_SCHOOL_DISTRICT" for e in st_ents
                    ),
                    "higher_ed_sources_found": any(
                        e.get("entity_type") == "HIGHER_EDUCATION" for e in st_ents
                    ),
                    "major_platform_families_found": platforms,
                    "cooperative_sources_found": any(
                        s.get("government_level") == "COOPERATIVE" for s in sources
                    ),
                    "independent_portals_found": any(
                        (s.get("source_platform") or "") in {"SimpleHTML", "StateOwned"} for s in st_sources
                    ),
                    "source_count": len(st_sources),
                    "entity_count": len(st_ents),
                    "known_gaps": gaps,
                }
            )

        # Platform breakdown
        platform_rows = []
        for fam, cnt in by_platform.most_common():
            fam_sources = [s for s in sources if (s.get("source_platform") or s.get("platform_family")) == fam]
            fam_states = sorted({s.get("state") for s in fam_sources if s.get("state")})
            fam_types = Counter(s.get("entity_type") or "OTHER" for s in fam_sources)
            access = Counter(s.get("access_level") for s in fam_sources)
            platform_rows.append(
                {
                    "platform": fam,
                    "sources": cnt,
                    "entities_linked": len(
                        {e for s in fam_sources for e in (s.get("entities_covered") or [])}
                    ),
                    "states": fam_states,
                    "entity_types": dict(fam_types),
                    "access_breakdown": dict(access),
                    "public_access": access.get(ACCESS_PUBLIC, 0) + access.get(ACCESS_PUBLIC_PLUS_FREE_BID, 0),
                    "free_registration": access.get(ACCESS_FREE_REG, 0),
                    "limited_free": access.get(ACCESS_LIMITED_FREE, 0),
                    "paid": access.get(ACCESS_PAID, 0),
                    "api": sum(1 for s in fam_sources if s.get("api_available")),
                    "known_limitations": sorted(
                        {
                            str(s.get("notes"))[:120]
                            for s in fam_sources
                            if s.get("notes")
                        }
                    )[:5],
                }
            )

        probed_ok = [p for p in self.probe_results if p.get("health") == "HEALTHY"]
        probed_fail = [p for p in self.probe_results if p.get("health") not in {"HEALTHY", None}]

        # Gaps via existing intelligence + this inventory
        try:
            gap_existing = build_coverage_gap_report(self.registry)
        except Exception as exc:  # noqa: BLE001
            gap_existing = {"error": str(exc)[:200]}

        biggest_gaps = [
            {
                "gap": "K12_DEPTH",
                "detail": (
                    f"Only {_ent_count('K12_SCHOOL_DISTRICT')} K-12 entities discovered vs "
                    f"~{K12_NATIONAL_ESTIMATE_RANGE['low']}+ NCES districts nationally"
                ),
            },
            {
                "gap": "COUNTY_AND_SPECIAL_DISTRICT_DEPTH",
                "detail": (
                    f"Counties={_ent_count('COUNTY')}, special districts={_ent_count('SPECIAL_DISTRICT')} "
                    "— thin vs US local-government universe"
                ),
            },
            {
                "gap": "HIGHER_ED_DEPTH",
                "detail": f"Higher-ed entities={_ent_count('HIGHER_EDUCATION')} — sparse relative to public institutions",
            },
            {
                "gap": "AUTH_GATED_STATE_PORTALS",
                "detail": "Several statewide portals require login for useful listings (AL, AK, AZ, CA patterns)",
            },
            {
                "gap": "BIDNET_PACKAGE_GATING",
                "detail": "BidNet networks provide public metadata; document packages often need free registration",
            },
            {
                "gap": "NO_PAID_AGGREGATOR_DEPENDENCY",
                "detail": "Paid aggregators intentionally excluded — entity maps only when public buyer portals found",
            },
        ]

        next_targets = [
            {
                "priority": 1,
                "target": "Expand K-12 via state education cooperatives + BuyBoard/AEPA/CREC public listings",
                "why": "Largest entity universe blind spot vs NCES district count",
            },
            {
                "priority": 2,
                "target": "Deepen county/special-district portals on PlanetBids/Bonfire/OpenGov families",
                "why": "High adapter leverage — one family unlocks many locals",
            },
            {
                "priority": 3,
                "target": "Repair/validate auth-gated state portals with true public feeds where they exist",
                "why": "50-state matrix present but several not publicly listable",
            },
            {
                "priority": 4,
                "target": "Extract buyers from more BidNet statewide open-bids pages into entity graph",
                "why": "Public HTML already reachable; entity coverage under-counted",
            },
            {
                "priority": 5,
                "target": "Higher-ed independent procurement boards + community college systems",
                "why": "Thin vs public university/college footprint",
            },
        ]

        summary = {
            "kind": "ProcurementSourceCoverageSummary",
            "build": BUILD_TAG,
            "generated_at": _utc(),
            "total_unique_procurement_sources": len(sources),
            "total_unique_buying_entities": len(entities),
            "total_entity_source_relationships": len(self.relationships),
            "total_states_covered": len(states_with_state_portal | states_with_local),
            "states_with_state_portal": len(states_with_state_portal),
            "federal_sources": by_level.get("FEDERAL", 0),
            "state_sources": by_level.get("STATE", 0),
            "local_sources": by_level.get("LOCAL", 0),
            "network_sources": by_level.get("NETWORK", 0),
            "cooperative_sources": by_level.get("COOPERATIVE", 0),
            "entities_by_type": {
                "federal": _ent_count("FEDERAL"),
                "state": _ent_count("STATE"),
                "city_municipal": _ent_count("CITY_MUNICIPAL"),
                "county": _ent_count("COUNTY"),
                "k12_school_district": _ent_count("K12_SCHOOL_DISTRICT"),
                "higher_education": _ent_count("HIGHER_EDUCATION"),
                "special_district": _ent_count("SPECIAL_DISTRICT"),
                "airport": _ent_count("AIRPORT"),
                "transit": _ent_count("TRANSIT"),
                "utility": _ent_count("UTILITY"),
                "housing": _ent_count("HOUSING_AUTHORITY"),
                "port": _ent_count("PORT_AUTHORITY"),
                "cooperative": _ent_count("COOPERATIVE"),
                "other_public": _ent_count("OTHER_PUBLIC"),
            },
            "access_breakdown": dict(access_counts),
            "platform_families": dict(by_platform),
            "k12": {
                "entities_discovered": _ent_count("K12_SCHOOL_DISTRICT"),
                "national_estimate": K12_NATIONAL_ESTIMATE_RANGE,
                "note": K12_NATIONAL_ESTIMATE_NOTE,
                "coverage_percent_invented": False,
            },
            "sources_tested_successfully": len(probed_ok),
            "sources_tested_failed_or_gated": len(probed_fail),
            "paid_aggregators_excluded": len(self.rejected_paid),
            "biggest_gaps": biggest_gaps,
            "recommended_next_integration_targets": next_targets,
            "honesty": {
                "not_complete_national_census": True,
                "source_count_not_entity_coverage": True,
                "paid_aggregators_not_counted_as_coverage": True,
                "http_200_not_automatically_healthy": True,
            },
        }

        access_summary = {
            "kind": "ProcurementSourceAccessStatusSummary",
            "build": BUILD_TAG,
            "generated_at": _utc(),
            "counts": dict(access_counts),
            "level_definitions": {
                "PUBLIC": "Level 1 — useful info without registration",
                "FREE_REGISTRATION": "Level 2 — free vendor registration for useful access",
                "PUBLIC_LISTING_FREE_REG_TO_BID": "Level 3 — public discovery; free reg to submit",
                "LIMITED_FREE": "Level 4 — free tier hides opportunities/documents",
                "PAID": "Level 5 — paid; not an M3 dependency",
                "UNKNOWN": "Access not yet established",
            },
            "probe_results_count": len(self.probe_results),
            "probe_health": Counter(p.get("health") for p in self.probe_results),
        }

        inventory = {
            "kind": "ProcurementSourceCoverageInventory",
            "build": BUILD_TAG,
            "generated_at": _utc(),
            "source_count": len(sources),
            "sources": sorted(sources, key=lambda s: str(s.get("source_id") or "")),
        }
        relationships = {
            "kind": "ProcurementSourceEntityRelationships",
            "build": BUILD_TAG,
            "generated_at": _utc(),
            "entity_count": len(entities),
            "relationship_count": len(self.relationships),
            "entities": sorted(entities, key=lambda e: str(e.get("entity_id") or "")),
            "relationships": self.relationships,
        }
        states_report = {
            "kind": "ProcurementSourceStateCoverage",
            "build": BUILD_TAG,
            "generated_at": _utc(),
            "states": state_table,
        }
        gaps = {
            "kind": "ProcurementSourceGapReport",
            "build": BUILD_TAG,
            "generated_at": _utc(),
            "biggest_gaps": biggest_gaps,
            "existing_gap_intelligence": gap_existing,
            "states_missing_k12": [r["state"] for r in state_table if "NO_K12_ENTITY" in (r.get("known_gaps") or [])],
            "states_missing_higher_ed": [
                r["state"] for r in state_table if "NO_HIGHER_ED_ENTITY" in (r.get("known_gaps") or [])
            ],
            "rejected_paid_aggregators": self.rejected_paid,
        }
        results = {
            "kind": "ProcurementSourceCoverageDiscoveryResults",
            "build": BUILD_TAG,
            "generated_at": _utc(),
            "discovery_stats": self.discovery_stats,
            "probe_results": self.probe_results,
            "platform_breakdown": platform_rows,
            "registry_audit": None,
            "summary_ref": ARTIFACT_NAMES["summary"],
        }
        try:
            results["registry_audit"] = {
                "coverage_metrics": coverage_metrics_honest(audit_source_network(self.registry)),
                "platform_inventory": platform_family_inventory(self.registry),
                "leverage": platform_leverage_ranking(self.registry)[:15],
                "entity_type_coverage": entity_type_coverage(self.registry),
                "geographic_coverage": geographic_coverage(self.registry),
            }
        except Exception as exc:  # noqa: BLE001
            results["registry_audit_error"] = str(exc)[:300]

        human = self._human_markdown(summary, platform_rows, state_table, biggest_gaps, next_targets)

        return {
            "inventory": inventory,
            "relationships": relationships,
            "summary": summary,
            "access": access_summary,
            "states": states_report,
            "gaps": gaps,
            "results": results,
            "human": human,
            "platform_breakdown": platform_rows,
        }

    def _human_markdown(
        self,
        summary: dict[str, Any],
        platforms: list[dict[str, Any]],
        state_table: list[dict[str, Any]],
        gaps: list[dict[str, Any]],
        next_targets: list[dict[str, Any]],
    ) -> str:
        lines = [
            "# M3 Procurement Source Coverage Discovery Report",
            "",
            f"Build: `{BUILD_TAG}`",
            f"Generated: {summary.get('generated_at')}",
            "",
            "## Headline",
            "",
            f"- **Unique procurement sources:** {summary['total_unique_procurement_sources']}",
            f"- **Unique buying entities:** {summary['total_unique_buying_entities']}",
            f"- **Entity↔source relationships:** {summary['total_entity_source_relationships']}",
            f"- **States with any coverage signal:** {summary['total_states_covered']}",
            f"- **States with state portal in inventory:** {summary['states_with_state_portal']}",
            "",
            "## Access breakdown",
            "",
        ]
        for k, v in sorted((summary.get("access_breakdown") or {}).items()):
            lines.append(f"- {k}: {v}")
        lines += ["", "## Entity coverage (discovered — not a census)", ""]
        for k, v in (summary.get("entities_by_type") or {}).items():
            lines.append(f"- {k}: {v}")
        k12 = summary.get("k12") or {}
        lines += [
            "",
            "## K-12",
            "",
            f"- Entities discovered: {k12.get('entities_discovered')}",
            f"- National estimate (NCES approx): {k12.get('national_estimate')}",
            f"- Note: {k12.get('note')}",
            "",
            "## Major platform families",
            "",
        ]
        for p in platforms[:20]:
            lines.append(
                f"- **{p['platform']}**: sources={p['sources']}, entities_linked={p['entities_linked']}, "
                f"states={len(p.get('states') or [])}"
            )
        lines += ["", "## Biggest gaps", ""]
        for g in gaps:
            lines.append(f"- **{g['gap']}**: {g['detail']}")
        lines += ["", "## Recommended next integration targets", ""]
        for t in next_targets:
            lines.append(f"{t['priority']}. {t['target']} — {t['why']}")
        lines += [
            "",
            "## Honesty",
            "",
            "- This is an inventory of what M3 can discover/register — not complete US procurement coverage.",
            "- Source count ≠ entity coverage.",
            "- Paid aggregators are excluded as primary sources.",
            "- HTTP 200 does not automatically mean HEALTHY.",
            "",
        ]
        return "\n".join(lines)

    def write_artifacts(self, reports: dict[str, Any] | None = None) -> dict[str, str]:
        reports = reports or self.build_reports()
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        paths = {}
        mapping = {
            "inventory": reports["inventory"],
            "relationships": reports["relationships"],
            "summary": reports["summary"],
            "access": reports["access"],
            "states": reports["states"],
            "gaps": reports["gaps"],
            "results": reports["results"],
        }
        for key, payload in mapping.items():
            path = self.artifacts_dir / ARTIFACT_NAMES[key]
            path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
            paths[key] = str(path)
        human_path = self.artifacts_dir / ARTIFACT_NAMES["human"]
        human_path.write_text(reports["human"], encoding="utf-8")
        paths["human"] = str(human_path)
        try:
            self.registry.save()
        except Exception:
            pass
        return paths

    def run(self) -> dict[str, Any]:
        self.ingest_registry_and_seeds()
        self.ingest_coverage_expansion()
        self.expand_bidnet_entities_from_live()
        self.probe_sample_sources()
        reports = self.build_reports()
        paths = self.write_artifacts(reports)
        return {
            "summary": reports["summary"],
            "access": reports["access"],
            "artifact_paths": paths,
            "discovery_stats": self.discovery_stats,
            "build": BUILD_TAG,
        }


def run_coverage_discovery(**kwargs: Any) -> dict[str, Any]:
    return ProcurementSourceCoverageDiscovery(**kwargs).run()
