"""Real procurement source seeding + bounded live discovery sweep.

Extends existing ProcurementSourceRegistry / live fetchers / state_matrix.
Seeds are concrete URLs M3 visits; directories are followed within budget.
Paid aggregators are never primary sources (entity-map only when useful).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from application_clock import now_utc
from discovery.agency_seeds import (
    FEDERAL_NON_SAM_LIVE,
    all_agencies_enriched,
    all_coops_enriched,
)
from discovery.bidnet_network import all_bidnet_networks_enriched
from discovery.live_fetchers import (
    BidNetLiveFetcher,
    BonfireLiveFetcher,
    JaggaerPublicLiveFetcher,
    OpenGovLiveFetcher,
    SimpleHtmlLiveFetcher,
)
from discovery.platform_detect import detect_platform
from discovery.state_matrix import STATE_MATRIX
from entity_geographic_coverage import US_STATES
from portal_document_resolver import live_http_get
from procurement_source_coverage_discovery import (
    ACCESS_FREE_REG,
    ACCESS_LIMITED_FREE,
    ACCESS_PAID,
    ACCESS_PUBLIC,
    ACCESS_PUBLIC_PLUS_FREE_BID,
    ACCESS_UNKNOWN,
    classify_access_level,
    entity_id_for,
    extract_bidnet_buyer_names,
    is_paid_aggregator_url,
    state_entity_id,
)
from procurement_source_registry import (
    ProcurementSourceRegistry,
    bootstrap_registry,
    source_record,
)
from source_discovery_engine import validate_source_candidate

BUILD_TAG = "20260921-m3-source-seed-sweep-1"
ROOT = Path(__file__).resolve().parent
ARTIFACTS = ROOT / "artifacts"

# Bounded crawl controls — cover full seed catalog + limited follow depth
MAX_SEED_ATTEMPTS = 180
MAX_FOLLOW_LINKS_PER_PAGE = 8
MAX_SECONDARY_VISITS = 80
MAX_OPPORTUNITIES_STORE = 2500
MAX_ENTITIES_FROM_DIRECTORY = 400


def _guess_entity_type(name: str | None) -> str:
    low = (name or "").lower()
    if not low:
        return "OTHER_PUBLIC"
    if "school" in low or " isd" in low or low.endswith(" isd") or "usd" in low or "k-12" in low:
        return "K12_SCHOOL_DISTRICT"
    if "university" in low or "college" in low or "community college" in low:
        return "HIGHER_EDUCATION"
    if "county" in low:
        return "COUNTY"
    if "city of" in low or "town of" in low or "village of" in low or "borough of" in low:
        return "CITY_MUNICIPAL"
    if "airport" in low:
        return "AIRPORT"
    if "transit" in low or "metro" in low or "mta" in low:
        return "TRANSIT"
    if "housing" in low:
        return "HOUSING_AUTHORITY"
    if "library" in low:
        return "LIBRARY"
    if "port authority" in low or "port of" in low:
        return "PORT_AUTHORITY"
    if "water" in low or "sewer" in low or "utility" in low or "power" in low:
        return "UTILITY"
    if "fire district" in low or "special district" in low or "redevelopment" in low:
        return "SPECIAL_DISTRICT"
    if "cooperative" in low or "coop" in low or "buyboard" in low or "sourcewell" in low:
        return "COOPERATIVE"
    return "OTHER_PUBLIC"

_PROCUREMENT_HREF = re.compile(
    r"(bid|rfp|rfq|ifb|solicitation|procurement|purchasing|vendor|opportunity|"
    r"open.?bid|current.?bid|eprocure|marketplace|contract.?opportunit)",
    re.I,
)
_JUNK_HREF = re.compile(
    r"(login|signin|sign-in|cart|facebook|twitter|linkedin|youtube|instagram|"
    r"privacy|cookie|javascript:|mailto:|tel:)",
    re.I,
)


def _utc() -> str:
    return now_utc().isoformat()


def _norm_url(url: str | None) -> str:
    if not url:
        return ""
    u = str(url).strip()
    if u.startswith("//"):
        u = "https:" + u
    # strip fragment
    u = u.split("#", 1)[0]
    return u.rstrip("/")


def _host(url: str | None) -> str:
    try:
        return (urlparse(url or "").netloc or "").lower().replace("www.", "")
    except Exception:
        return ""


def _seed_id(kind: str, url: str, name: str = "") -> str:
    digest = hashlib.sha1(f"{kind}|{_norm_url(url).lower()}".encode()).hexdigest()[:10]
    slug = re.sub(r"[^a-z0-9]+", "-", (name or kind).lower()).strip("-")[:40]
    return f"seed:{slug}:{digest}"


def build_seed_catalog() -> list[dict[str, Any]]:
    """Concrete seed URLs — starting points M3 will visit (not platform names alone)."""
    seeds: list[dict[str, Any]] = []

    def add(
        *,
        name: str,
        url: str,
        kind: str,
        government_level: str,
        state: str | None = None,
        platform: str | None = None,
        role: str = "PROCUREMENT_ENTRY",
        entity_map_only: bool = False,
        notes: str | None = None,
    ) -> None:
        if not url or not str(url).startswith("http"):
            return
        seeds.append(
            {
                "seed_name": name,
                "url": url,
                "kind": kind,
                "government_level": government_level,
                "state": state,
                "platform_family": platform,
                "role": role,
                "entity_map_only": entity_map_only,
                "notes": notes,
                "provenance": "seed_catalog",
            }
        )

    # --- Federal ---
    add(name="SAM.gov Contract Opportunities", url="https://sam.gov/opportunities", kind="FEDERAL", government_level="FEDERAL", platform="SAM_GOV")
    add(name="SAM.gov FPDS / awards transition", url="https://sam.gov/fpds", kind="FEDERAL_AWARD", government_level="FEDERAL", platform="SAM_GOV", role="AWARD_HISTORY")
    add(name="USAspending", url="https://www.usaspending.gov/", kind="FEDERAL_AWARD", government_level="FEDERAL", platform="JSON", role="AWARD_HISTORY")
    add(name="DLA home", url="https://www.dla.mil/", kind="FEDERAL", government_level="FEDERAL", role="AGENCY_HOME")
    add(name="DIBBS", url="https://www.dibbs.bsm.dla.mil/", kind="FEDERAL", government_level="FEDERAL", platform="DIBBS")
    add(name="DIBBS Recent RFQs", url="https://www.dibbs.bsm.dla.mil/RFQ/RfqRecents.aspx", kind="FEDERAL", government_level="FEDERAL", platform="DIBBS")
    add(name="DLA EBS Supplier Resources", url="https://www.dla.mil/Information-Operations/EBS-Supplier-Resources/", kind="FEDERAL", government_level="FEDERAL", role="SUPPLIER_RESOURCES")
    add(name="Federal procurement forecasts", url="https://www.acquisition.gov/procurement-forecasts/business-opportunities", kind="FEDERAL_FORECAST", government_level="FEDERAL", role="FORECAST")
    add(name="GSA buying/selling tools", url="https://www.gsa.gov/tools-overview/buying-and-selling-tools", kind="FEDERAL", government_level="FEDERAL", role="TOOLS_INDEX")
    add(name="GSA state/local programs", url="https://www.gsa.gov/buy-through-us/purchasing-programs/programs-for-state-and-local-governments", kind="FEDERAL", government_level="FEDERAL", role="PROGRAM_INDEX")
    add(name="VA NAC vendor portal", url="https://www.vendorportal.ecms.va.gov/nac/", kind="FEDERAL", government_level="FEDERAL")
    add(name="FedMall suppliers", url="https://www.suppliers.fedmall.mil/", kind="FEDERAL", government_level="FEDERAL")
    add(name="PIEE public solicitations", url="https://piee.eb.mil/sol/xhtml/unauth/index.xhtml", kind="FEDERAL", government_level="FEDERAL", platform="PIEE")

    # --- State known starting points (verify live; also covered by STATE_MATRIX) ---
    known_states = [
        ("NC", "North Carolina eProcurement", "https://eprocurement.nc.gov/"),
        ("DE", "Delaware MyMarketplace", "https://mymarketplace.delaware.gov/"),
        ("VA", "Virginia eVA", "https://eva.virginia.gov/"),
        ("MA", "Massachusetts COMMBUYS", "https://www.commbuys.com/"),
        ("NY", "New York Contract Reporter", "https://www.nyscr.ny.gov/"),
        ("IL", "Illinois BidBuy", "https://www.bidbuy.illinois.gov/"),
        ("GA", "Georgia Procurement Registry", "https://ssl.doas.state.ga.us/gpr/"),
        ("SC", "South Carolina Business Opportunities", "https://procurement.sc.gov/"),
        ("TX", "Texas SmartBuy / purchasing", "https://comptroller.texas.gov/purchasing/"),
        ("MO", "Missouri purchasing", "https://oa.mo.gov/purchasing"),
        ("WI", "Wisconsin VendorNet", "https://vendornet.wi.gov/"),
        ("IA", "Iowa SciQuest Public Events", "https://bids.sciquest.com/apps/Router/PublicEvent?CustomerOrg=DASIowa"),
        ("MT", "Montana SciQuest Public Events", "https://bids.sciquest.com/apps/Router/PublicEvent?CustomerOrg=StateOfMontana"),
    ]
    for st, name, url in known_states:
        add(name=name, url=url, kind="STATE", government_level="STATE", state=st, platform=None)

    # Full 50-state matrix (authoritative seeded list URLs)
    for sm in STATE_MATRIX:
        add(
            name=sm.get("portal_name") or sm.get("name") or sm["state"],
            url=sm["list_url"],
            kind="STATE",
            government_level="STATE",
            state=sm["state"],
            platform=sm.get("platform_family"),
            notes=sm.get("restrictions"),
        )

    # --- Platform public entry points (portals, not paid deps) ---
    add(name="OpenGov procurement", url="https://procurement.opengov.com/", kind="PLATFORM", government_level="NETWORK", platform="OpenGov", role="PLATFORM_ENTRY")
    add(name="OpenGov for vendors", url="https://opengov.com/products/procurement/for-vendor/", kind="PLATFORM", government_level="NETWORK", platform="OpenGov", role="PLATFORM_MARKETING")
    add(name="Bonfire vendor hub", url="https://vendors.bonfirehub.com/", kind="PLATFORM", government_level="NETWORK", platform="Bonfire", role="PLATFORM_ENTRY")
    add(name="Bonfire home", url="https://gobonfire.com/", kind="PLATFORM", government_level="NETWORK", platform="Bonfire", role="PLATFORM_MARKETING")
    add(name="Public Purchase", url="https://www.publicpurchase.com/", kind="PLATFORM", government_level="NETWORK", platform="PublicPurchase", role="PLATFORM_ENTRY")
    add(name="PlanetBids home", url="https://home.planetbids.com/", kind="PLATFORM", government_level="NETWORK", platform="PlanetBids", role="PLATFORM_ENTRY")
    add(name="bids&tenders", url="https://www.bidsandtenders.com/", kind="PLATFORM", government_level="NETWORK", platform="BidsAndTenders", role="PLATFORM_ENTRY")
    add(name="IonWave public info", url="https://www.ionwave.net/", kind="PLATFORM", government_level="NETWORK", platform="IonWave", role="PLATFORM_ENTRY")
    add(name="VendorLink public", url="https://www.vendorlink.com/", kind="PLATFORM", government_level="NETWORK", platform="VendorLink", role="PLATFORM_ENTRY")
    add(name="Periscope / BidSync public", url="https://www.periscopeholdings.com/", kind="PLATFORM", government_level="NETWORK", platform="Periscope", role="PLATFORM_MARKETING")
    add(name="WebProcure / Proactis", url="https://www.webprocure.com/", kind="PLATFORM", government_level="NETWORK", platform="WebProcure", role="PLATFORM_ENTRY")

    # --- Cooperatives ---
    add(name="NASPO ValuePoint", url="https://www.naspovaluepoint.org/", kind="COOPERATIVE", government_level="COOPERATIVE")
    add(name="NASPO ValuePoint solicitations", url="https://www.naspovaluepoint.org/solicitations/", kind="COOPERATIVE", government_level="COOPERATIVE")
    add(name="Sourcewell", url="https://www.sourcewell-mn.gov/", kind="COOPERATIVE", government_level="COOPERATIVE")
    add(name="Sourcewell solicitations", url="https://www.sourcewell-mn.gov/solicitations", kind="COOPERATIVE", government_level="COOPERATIVE")
    add(name="BuyBoard", url="https://www.buyboard.com/", kind="COOPERATIVE", government_level="COOPERATIVE")
    add(name="BuyBoard current solicitations", url="https://www.buyboard.com/Vendors/CurrentSolicitations.aspx", kind="COOPERATIVE", government_level="COOPERATIVE")
    add(name="AEPA", url="https://aepa.coop/", kind="COOPERATIVE", government_level="COOPERATIVE")
    add(name="AEPA solicitations", url="https://aepacoop.org/solicitations/", kind="COOPERATIVE", government_level="COOPERATIVE")
    add(name="CREC cooperative purchasing", url="https://crec.org/coop/", kind="COOPERATIVE", government_level="COOPERATIVE", state="CT")
    add(name="Minnesota CPV", url="https://mn.gov/admin/osp/other-purchasers/cpv/", kind="COOPERATIVE", government_level="COOPERATIVE", state="MN")

    # --- Entity discovery maps (NOT primary M3 sources) ---
    add(
        name="BidNet participating buyers (entity map only)",
        url="https://www.bidnetdirect.com/participating-buyers",
        kind="ENTITY_DIRECTORY",
        government_level="NETWORK",
        platform="BidNet",
        role="ENTITY_MAP",
        entity_map_only=True,
        notes="Commercial directory used only to identify public buyers — not a paid M3 dependency",
    )
    add(
        name="NASPO eProcurement directory (seed directory)",
        url="https://www.naspo.org/research-and-innovation/rosp-category/eprocurement/",
        kind="STATE_DIRECTORY",
        government_level="NETWORK",
        role="SEED_DIRECTORY",
        entity_map_only=True,
        notes="Directory of state eProcurement systems — NASPO is not the procurement source",
    )

    # Agency + coop existing seeds
    for a in all_agencies_enriched():
        url = a.get("procurement_url") or a.get("list_url")
        if url:
            add(
                name=a.get("name") or a.get("agency_key"),
                url=url,
                kind="LOCAL",
                government_level="LOCAL",
                state=a.get("state_code"),
                platform=a.get("platform_family"),
            )
    for c in all_coops_enriched():
        if c.get("list_url"):
            add(
                name=c.get("name") or c.get("source_id"),
                url=c["list_url"],
                kind="COOPERATIVE",
                government_level="COOPERATIVE",
                platform="COOPERATIVE",
            )
    for n in all_bidnet_networks_enriched():
        add(
            name=n.get("name") or n.get("source_id"),
            url=n["list_url"],
            kind="NETWORK",
            government_level="NETWORK",
            state=n.get("state_code"),
            platform="BidNet",
            notes="Statewide BidNet open-bids listing — public metadata network",
        )
    for f in FEDERAL_NON_SAM_LIVE:
        if f.get("list_url"):
            add(
                name=f.get("name") or f.get("source_id"),
                url=f["list_url"],
                kind="FEDERAL",
                government_level="FEDERAL",
                platform=f.get("platform_family"),
                notes=f.get("note"),
            )

    # Deduplicate by normalized URL — keep first
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for s in seeds:
        key = _norm_url(s["url"]).lower()
        if key in seen:
            continue
        seen.add(key)
        s["seed_id"] = _seed_id(s["kind"], s["url"], s["seed_name"])
        out.append(s)
    return out


def extract_procurement_links(html: str, *, base_url: str) -> list[dict[str, Any]]:
    """Extract likely procurement portal/listing links from a page."""
    if not html:
        return []
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for m in re.finditer(r'href=["\']([^"\']+)["\'][^>]*>([^<]{0,120})', html, re.I):
        href, text = m.group(1).strip(), re.sub(r"\s+", " ", m.group(2)).strip()
        if not href or href.startswith("#") or href.lower().startswith("javascript:"):
            continue
        abs_url = _norm_url(urljoin(base_url, href))
        if not abs_url.startswith("http"):
            continue
        blob = f"{abs_url} {text}"
        if _JUNK_HREF.search(blob) and not _PROCUREMENT_HREF.search(blob):
            continue
        if not _PROCUREMENT_HREF.search(blob) and detect_platform(abs_url).get("platform") in {
            None,
            "UNKNOWN",
            "OTHER",
        }:
            # Keep platform-family URLs even without keyword
            if not re.search(r"bonfire|opengov|planetbids|ionwave|sciquest|bidnet|publicpurchase", abs_url, re.I):
                continue
        key = abs_url.lower()
        if key in seen:
            continue
        seen.add(key)
        plat = detect_platform(abs_url)
        found.append(
            {
                "url": abs_url,
                "anchor_text": text[:120] or None,
                "platform_family": plat.get("platform") if isinstance(plat, dict) else plat,
            }
        )
        if len(found) >= 40:
            break
    return found


def extract_directory_entities(html: str, *, source_hint: str = "") -> list[dict[str, Any]]:
    """Pull buyer/agency names from directory HTML (BidNet participating-buyers etc.)."""
    if not html:
        return []
    ents: list[dict[str, Any]] = []
    seen: set[str] = set()

    patterns = [
        r'data-agency=["\']([^"\']{4,120})["\']',
        r'class=["\'][^"\']*agency[^"\']*["\'][^>]*>([^<]{4,120})<',
        r"<h[23][^>]*>([^<]{4,120})</h[23]>",
        r"(City of [A-Z][^<\n|]{2,80})",
        r"(County of [A-Z][^<\n|]{2,80})",
        r"([A-Z][A-Za-z .&'\-]{2,70} School District)",
        r"([A-Z][A-Za-z .&'\-]{2,70} (?:ISD|USD|Unified School District))",
        r"([A-Z][A-Za-z .&'\-]{2,70} (?:Community College|University|College))",
        r"([A-Z][A-Za-z .&'\-]{2,70} (?:Housing Authority|Transit|Airport|Library|Port Authority))",
    ]
    for pat in patterns:
        for m in re.finditer(pat, html):
            name = re.sub(r"\s+", " ", m.group(1)).strip(" .-|")
            if len(name) < 5 or name.lower() in seen:
                continue
            if re.search(r"login|register|password|cookie|javascript|click here|learn more", name, re.I):
                continue
            seen.add(name.lower())
            et = "OTHER_PUBLIC"
            low = name.lower()
            if "school" in low or "isd" in low or "usd" in low:
                et = "K12_SCHOOL_DISTRICT"
            elif "university" in low or "college" in low:
                et = "HIGHER_EDUCATION"
            elif "county" in low:
                et = "COUNTY"
            elif "city of" in low or "town of" in low:
                et = "CITY_MUNICIPAL"
            elif "airport" in low:
                et = "AIRPORT"
            elif "transit" in low or "metro" in low:
                et = "TRANSIT"
            elif "housing" in low:
                et = "HOUSING_AUTHORITY"
            elif "library" in low:
                et = "OTHER_PUBLIC"
            elif "port" in low:
                et = "PORT_AUTHORITY"
            ents.append(
                {
                    "name": name[:200],
                    "entity_type": et,
                    "provenance": source_hint or "directory_html",
                    "discovered_from_directory": True,
                }
            )
            if len(ents) >= MAX_ENTITIES_FROM_DIRECTORY:
                return ents
    return ents


def classify_visit_health(
    *,
    status_code: int | None,
    text: str,
    opportunities: int,
    validation: dict[str, Any] | None = None,
) -> str:
    """HTTP 200 ≠ healthy. Login chrome ≠ automatic AUTH. Zero opps may be DEGRADED."""
    validation = validation or {}
    low = (text or "")[:6000].lower()
    if status_code in {401, 403}:
        return "AUTH_REQUIRED" if status_code == 401 or "login" in low else "BLOCKED"
    if status_code and status_code >= 500:
        return "UNAVAILABLE"
    if status_code and status_code >= 400:
        return "UNAVAILABLE"
    if validation.get("state") == "ANTI_AUTOMATION" or ("captcha" in low and len(text) < 8000):
        return "BLOCKED"
    if opportunities > 0:
        return "HEALTHY"
    if validation.get("promote"):
        return "HEALTHY"
    # Page loaded with login wall and no solicitation signals
    signals = len(re.findall(r"\b(rfp|rfq|ifb|solicitation|bid\s*#|open\s+bids?)\b", text or "", re.I))
    if signals == 0 and re.search(r"sign\s*in|log\s*in|register\s*now|create\s*account", low):
        return "AUTH_REQUIRED"
    if status_code and 200 <= status_code < 400:
        if signals >= 1:
            return "DEGRADED"  # structure present, zero parsed opps
        return "DEGRADED"
    return "UNKNOWN"


def _try_parse_opportunities(url: str, text: str, platform_hint: str | None = None) -> list[dict[str, Any]]:
    """Reuse existing live fetchers — bounded parse of listing HTML/JSON."""
    plat = platform_hint or (detect_platform(url, html_snippet=text[:4000]) or {}).get("platform")
    fetchers = []
    # Prefer family-specific
    mapping = {
        "BidNet": BidNetLiveFetcher,
        "Bonfire": BonfireLiveFetcher,
        "OpenGov": OpenGovLiveFetcher,
        "Jaggaer": JaggaerPublicLiveFetcher,
        "SimpleHTML": SimpleHtmlLiveFetcher,
        "StateOwned": SimpleHtmlLiveFetcher,
    }
    cls = mapping.get(str(plat or ""))
    if cls:
        fetchers.append(cls())
    # Always try simple HTML as fallback for table listings
    if not fetchers or not isinstance(fetchers[0], SimpleHtmlLiveFetcher):
        fetchers.append(SimpleHtmlLiveFetcher())
    if "sciquest" in url.lower() or "jaggaer" in url.lower():
        fetchers.insert(0, JaggaerPublicLiveFetcher())

    out: list[dict[str, Any]] = []
    for fetcher in fetchers[:3]:
        try:
            if hasattr(fetcher, "structure_recognized") and not fetcher.structure_recognized(text, list_url=url):
                # Still attempt Jaggaer/BidNet when URL matches
                if fetcher.source_id not in {"live_jaggaer", "live_bidnet"}:
                    continue
            opps = fetcher.parse_listing(text, list_url=url) or []
            for o in opps:
                if hasattr(o, "__dict__"):
                    row = {
                        "title": getattr(o, "title", None),
                        "solicitation_number": getattr(o, "solicitation_number", None),
                        "agency": getattr(o, "agency", None),
                        "deadline_raw": getattr(o, "deadline_raw", None),
                        "detail_url": getattr(o, "detail_url", None),
                        "source_id": getattr(o, "source_id", None),
                        "external_id": getattr(o, "external_id", None),
                    }
                elif isinstance(o, dict):
                    row = {
                        "title": o.get("title"),
                        "solicitation_number": o.get("solicitation_number"),
                        "agency": o.get("agency"),
                        "deadline_raw": o.get("deadline_raw"),
                        "detail_url": o.get("detail_url"),
                        "source_id": o.get("source_id"),
                        "external_id": o.get("external_id"),
                    }
                else:
                    continue
                if not row.get("title"):
                    continue
                row["discovered_from_url"] = url
                row["parser"] = getattr(fetcher, "source_id", "unknown")
                out.append(row)
            if out:
                break
        except Exception:
            continue
    return out[:200]


class ProcurementSourceSeedSweep:
    """Visit seeded procurement destinations; follow directories within budget."""

    def __init__(
        self,
        *,
        registry: ProcurementSourceRegistry | None = None,
        artifacts_dir: Path | None = None,
        max_seed_attempts: int = MAX_SEED_ATTEMPTS,
        max_secondary_visits: int = MAX_SECONDARY_VISITS,
    ) -> None:
        self.registry = registry or bootstrap_registry()
        self.artifacts_dir = artifacts_dir or ARTIFACTS
        self.max_seed_attempts = max_seed_attempts
        self.max_secondary_visits = max_secondary_visits
        self.seeds: list[dict[str, Any]] = []
        self.portals: dict[str, dict[str, Any]] = {}
        self.entities: dict[str, dict[str, Any]] = {}
        self.relationships: list[dict[str, Any]] = []
        self.opportunities: list[dict[str, Any]] = []
        self.failures: list[dict[str, Any]] = []
        self.visits: list[dict[str, Any]] = []
        self.discovered_not_seeded: list[dict[str, Any]] = []
        self._visited: set[str] = set()
        self._secondary_budget = 0

    def _register_entity(self, ent: dict[str, Any]) -> str:
        name = ent.get("name") or "UNKNOWN"
        et = ent.get("entity_type") or "OTHER_PUBLIC"
        st = ent.get("state")
        if et == "STATE" and st:
            eid = state_entity_id(str(st))
        else:
            eid = ent.get("entity_id") or entity_id_for(name=name, entity_type=et, state=st)
        if eid in self.entities:
            existing = self.entities[eid]
            for k, v in ent.items():
                if v is not None and existing.get(k) in (None, "", "UNKNOWN"):
                    existing[k] = v
            portals = list(existing.get("procurement_portals") or [])
            for p in ent.get("procurement_portals") or []:
                if p and p not in portals:
                    portals.append(p)
            existing["procurement_portals"] = portals
            return eid
        row = dict(ent)
        row["entity_id"] = eid
        row.setdefault("procurement_portals", [])
        self.entities[eid] = row
        return eid

    def _register_portal(self, portal: dict[str, Any]) -> str:
        url = _norm_url(portal.get("url") or portal.get("portal_url"))
        key = url.lower()
        if is_paid_aggregator_url(url) and not portal.get("entity_map_only"):
            self.failures.append(
                {
                    "url": url,
                    "reason": "paid_aggregator_excluded_as_primary_source",
                    "at": _utc(),
                }
            )
            return ""
        if key in self.portals:
            prev = self.portals[key]
            for k, v in portal.items():
                if v is not None and prev.get(k) in (None, "", "UNKNOWN"):
                    prev[k] = v
            return prev.get("source_id") or prev.get("portal_id") or key
        pid = portal.get("source_id") or _seed_id("portal", url, portal.get("source_name") or "")
        row = {
            **portal,
            "portal_id": pid,
            "source_id": pid,
            "url": url,
            "portal_url": url,
            "discovery_url": url,
            "last_verified": _utc(),
        }
        # Align field names with classify_access_level expectations
        if row.get("health_state") is None and row.get("source_health"):
            row["health_state"] = row["source_health"]
        if row.get("publicly_searchable") is None:
            sh = str(row.get("source_health") or row.get("health_state") or "").upper()
            if sh in {"HEALTHY", "DEGRADED", "HEALTHY_PRODUCTION", "PARTIALLY_PRODUCTIVE"}:
                row["publicly_searchable"] = True
            elif sh in {"AUTH_REQUIRED", "BLOCKED", "UNAVAILABLE"}:
                row["publicly_searchable"] = False
            elif row.get("public_access") is not None:
                row["publicly_searchable"] = bool(row.get("public_access"))
        # Prefer already-computed access when present and not UNKNOWN
        prior = row.get("access_level")
        computed = classify_access_level(row)
        if prior and prior != ACCESS_UNKNOWN and computed == ACCESS_UNKNOWN:
            row["access_level"] = prior
        else:
            row["access_level"] = computed
        # Map HEALTHY source_health → PUBLIC when classifier still unknown
        if row["access_level"] == ACCESS_UNKNOWN:
            sh = str(row.get("source_health") or "").upper()
            if sh == "HEALTHY":
                row["access_level"] = ACCESS_PUBLIC
            elif sh == "DEGRADED":
                notes = str(row.get("notes") or "").lower()
                if "register to bid" in notes or "register to respond" in notes:
                    row["access_level"] = ACCESS_PUBLIC_PLUS_FREE_BID
                else:
                    row["access_level"] = ACCESS_PUBLIC
            elif sh == "AUTH_REQUIRED":
                row["access_level"] = ACCESS_FREE_REG
        self.portals[key] = row
        # Mirror into existing registry
        if not portal.get("entity_map_only"):
            try:
                if not self.registry.get(pid):
                    self.registry.upsert(
                        source_record(
                            source_id=pid,
                            source_name=row.get("source_name") or row.get("seed_name") or pid,
                            discovery_url=url,
                            platform_family=row.get("platform_family"),
                            government_level=row.get("government_level"),
                            entity_type=row.get("entity_type") or "OTHER_PUBLIC",
                            geographic_scope=row.get("state"),
                            jurisdiction=row.get("state"),
                            health_state=row.get("source_health") or "DISCOVERED_UNVALIDATED",
                            auth_requirement=row.get("auth_requirement") or "NONE",
                            provenance=row.get("provenance") or "seed_sweep",
                            notes=row.get("notes"),
                        )
                    )
            except Exception:
                pass
        return pid

    def _link(self, entity_id: str, portal_id: str, url: str | None = None, platform: str | None = None) -> None:
        self.relationships.append(
            {
                "entity_id": entity_id,
                "source_id": portal_id,
                "portal_url": url,
                "source_platform": platform,
                "relationship": "PROCURES_VIA",
                "at": _utc(),
            }
        )
        ent = self.entities.get(entity_id)
        if ent and url:
            portals = list(ent.get("procurement_portals") or [])
            if url not in portals:
                portals.append(url)
            ent["procurement_portals"] = portals

    def visit(self, seed: dict[str, Any], *, is_secondary: bool = False) -> dict[str, Any]:
        url = _norm_url(seed.get("url"))
        key = url.lower()
        if not url or key in self._visited:
            return {"skipped": True, "url": url}
        if is_secondary:
            if self._secondary_budget >= self.max_secondary_visits:
                return {"skipped": True, "reason": "secondary_budget_exhausted", "url": url}
            self._secondary_budget += 1
        self._visited.add(key)

        hit = live_http_get(url, source_id=str(seed.get("seed_id") or "seed_sweep")[:80])
        text = hit.get("text") or ""
        status = hit.get("status_code")
        validation = validate_source_candidate(listing_html=text, status_code=status, url=url)
        opps: list[dict[str, Any]] = []
        if not seed.get("entity_map_only") and text:
            opps = _try_parse_opportunities(url, text, seed.get("platform_family"))
            for o in opps:
                if len(self.opportunities) < MAX_OPPORTUNITIES_STORE:
                    self.opportunities.append(o)
                # Entity from opportunity agency when present
                agency = o.get("agency")
                if agency and str(agency).strip():
                    eid = self._register_entity(
                        {
                            "name": str(agency).strip(),
                            "entity_type": "OTHER_PUBLIC",
                            "state": seed.get("state"),
                            "government_level": seed.get("government_level") or "LOCAL",
                            "provenance": "opportunity_agency_field",
                            "procurement_portals": [url],
                        }
                    )
                    self._link(eid, seed.get("seed_id") or url, url=url, platform=seed.get("platform_family"))

        health = classify_visit_health(
            status_code=status,
            text=text,
            opportunities=len(opps),
            validation=validation,
        )
        access = classify_access_level(
            {
                **seed,
                "discovery_url": url,
                "portal_url": url,
                "publicly_searchable": health in {"HEALTHY", "DEGRADED"},
                "docs_public": True if health == "HEALTHY" else None,
                "auth_requirement": "LOGIN_REQUIRED" if health == "AUTH_REQUIRED" else "NONE",
                "health_state": health,
                "notes": seed.get("notes") or seed.get("restrictions"),
                "registration_required_to_bid": "register to bid" in str(seed.get("notes") or "").lower()
                or "register to respond" in str(seed.get("notes") or "").lower(),
            }
        )
        if access == ACCESS_UNKNOWN and health == "HEALTHY":
            access = ACCESS_PUBLIC
        elif access == ACCESS_UNKNOWN and health == "DEGRADED":
            access = (
                ACCESS_PUBLIC_PLUS_FREE_BID
                if "register to bid" in str(seed.get("notes") or "").lower()
                else ACCESS_PUBLIC
            )
        elif access == ACCESS_UNKNOWN and health == "AUTH_REQUIRED":
            access = ACCESS_FREE_REG

        portal_id = self._register_portal(
            {
                **seed,
                "source_name": seed.get("seed_name"),
                "url": url,
                "source_health": health,
                "access_level": access,
                "public_access": access in {ACCESS_PUBLIC, ACCESS_PUBLIC_PLUS_FREE_BID},
                "documents_public": None,
                "registration_required": access
                in {ACCESS_FREE_REG, ACCESS_PUBLIC_PLUS_FREE_BID, ACCESS_LIMITED_FREE},
                "registration_cost": "FREE"
                if access in {ACCESS_FREE_REG, ACCESS_PUBLIC_PLUS_FREE_BID, ACCESS_LIMITED_FREE}
                else ("NONE" if access == ACCESS_PUBLIC else "UNKNOWN"),
                "opportunities_parsed": len(opps),
                "http_status": status,
                "entity_map_only": bool(seed.get("entity_map_only")),
                "seeded": not is_secondary,
                "discovered_not_seeded": is_secondary,
            }
        )

        # State entity linkage
        if seed.get("government_level") == "STATE" and seed.get("state"):
            eid = self._register_entity(
                {
                    "entity_id": state_entity_id(str(seed["state"])),
                    "name": f"State of {seed['state']}",
                    "entity_type": "STATE",
                    "state": seed["state"],
                    "government_level": "STATE",
                    "provenance": "seed_sweep",
                    "procurement_portals": [url],
                }
            )
            self._link(eid, portal_id, url=url, platform=seed.get("platform_family"))
        elif seed.get("government_level") == "LOCAL" and seed.get("seed_name"):
            eid = self._register_entity(
                {
                    "name": seed["seed_name"],
                    "entity_type": _guess_entity_type(seed.get("seed_name")),
                    "state": seed.get("state"),
                    "government_level": "LOCAL",
                    "provenance": "seed_catalog",
                    "procurement_portals": [url],
                }
            )
            self._link(eid, portal_id, url=url, platform=seed.get("platform_family"))
        elif seed.get("government_level") == "COOPERATIVE":
            eid = self._register_entity(
                {
                    "name": seed.get("seed_name") or "Cooperative",
                    "entity_type": "COOPERATIVE",
                    "government_level": "COOPERATIVE",
                    "provenance": "seed_catalog",
                    "procurement_portals": [url],
                }
            )
            self._link(eid, portal_id, url=url, platform=seed.get("platform_family"))
        elif seed.get("government_level") == "FEDERAL":
            eid = self._register_entity(
                {
                    "name": seed.get("seed_name") or "Federal",
                    "entity_type": "FEDERAL",
                    "government_level": "FEDERAL",
                    "provenance": "seed_catalog",
                    "procurement_portals": [url],
                }
            )
            self._link(eid, portal_id, url=url, platform=seed.get("platform_family"))
        elif seed.get("government_level") == "NETWORK" and seed.get("platform_family") == "BidNet" and not seed.get("entity_map_only"):
            # Statewide BidNet open-bids — extract buyer names as entity map, not portal claim
            for be in extract_bidnet_buyer_names(text, state_code=seed.get("state")):
                self._register_entity(
                    {
                        **be,
                        "government_level": "LOCAL",
                        "discovered_from_directory": True,
                    }
                )

        follow_links: list[dict[str, Any]] = []
        directory_entities: list[dict[str, Any]] = []
        follow_roles = {
            "SEED_DIRECTORY",
            "ENTITY_MAP",
            "PLATFORM_ENTRY",
            "AGENCY_HOME",
            "TOOLS_INDEX",
            "PROGRAM_INDEX",
            "SUPPLIER_RESOURCES",
            "FORECAST",
        }
        if text and (
            seed.get("role") in follow_roles
            or seed.get("entity_map_only")
            or seed.get("kind") in {"STATE_DIRECTORY", "ENTITY_DIRECTORY", "PLATFORM", "FEDERAL"}
            or seed.get("government_level") in {"STATE", "COOPERATIVE"}
        ):
            follow_links = extract_procurement_links(text, base_url=url)[:MAX_FOLLOW_LINKS_PER_PAGE]
            if seed.get("entity_map_only") or seed.get("role") == "ENTITY_MAP":
                directory_entities = extract_directory_entities(
                    text, source_hint=seed.get("seed_name") or url
                )
                if "bidnet" in url.lower():
                    for be in extract_bidnet_buyer_names(text, state_code=seed.get("state")):
                        directory_entities.append(
                            {
                                "name": be["name"],
                                "entity_type": be.get("entity_type") or "OTHER_PUBLIC",
                                "state": be.get("state"),
                                "provenance": be.get("provenance") or "bidnet_entity_map",
                                "discovered_from_directory": True,
                            }
                        )
                for de in directory_entities:
                    de["state"] = de.get("state") or seed.get("state")
                    eid = self._register_entity(de)
                    # Entity map only — do NOT claim BidNet as the entity's procurement portal
                    if not seed.get("entity_map_only"):
                        self._link(eid, portal_id, url=url, platform=seed.get("platform_family"))

        visit = {
            "seed_id": seed.get("seed_id"),
            "url": url,
            "final_url": hit.get("final_url"),
            "status_code": status,
            "ok": hit.get("ok"),
            "health": health,
            "access_level": access,
            "opportunities_parsed": len(opps),
            "follow_links": len(follow_links),
            "directory_entities": len(directory_entities),
            "is_secondary": is_secondary,
            "entity_map_only": bool(seed.get("entity_map_only")),
            "error": hit.get("error") or hit.get("failure"),
            "at": _utc(),
        }
        self.visits.append(visit)
        if health in {"AUTH_REQUIRED", "BLOCKED", "UNAVAILABLE"} or not hit.get("ok"):
            self.failures.append({**visit, "failure_class": health})

        # Follow discovered procurement links (bounded)
        for link in follow_links:
            link_url = link["url"]
            if is_paid_aggregator_url(link_url):
                continue
            if _norm_url(link_url).lower() in self._visited:
                continue
            if self._secondary_budget >= self.max_secondary_visits:
                break
            child = {
                "seed_id": _seed_id("follow", link_url, link.get("anchor_text") or "follow"),
                "seed_name": link.get("anchor_text") or link_url,
                "url": link_url,
                "kind": "DISCOVERED_PORTAL",
                "government_level": seed.get("government_level") or "LOCAL",
                "state": seed.get("state"),
                "platform_family": link.get("platform_family") or seed.get("platform_family"),
                "role": "DISCOVERED_FROM_SEED",
                "provenance": f"followed_from:{url}",
                "parent_source": seed.get("seed_id") or url,
            }
            self.discovered_not_seeded.append(
                {"url": link_url, "from": url, "anchor": link.get("anchor_text"), "at": _utc()}
            )
            self.visit(child, is_secondary=True)

        return visit

    def run(self) -> dict[str, Any]:
        self.seeds = build_seed_catalog()
        attempted = 0
        for seed in self.seeds:
            if attempted >= self.max_seed_attempts:
                break
            # Always attempt ENTITY_MAP / STATE seeds / FEDERAL / COOP first priority already in order
            self.visit(seed, is_secondary=False)
            attempted += 1

        reports = self.build_reports(seeds_loaded=len(self.seeds), seeds_attempted=attempted)
        paths = self.write_artifacts(reports)
        try:
            self.registry.save()
        except Exception:
            pass
        return {"reports": reports, "artifact_paths": paths, "build": BUILD_TAG}

    def build_reports(self, *, seeds_loaded: int, seeds_attempted: int) -> dict[str, Any]:
        portals = [p for p in self.portals.values() if not p.get("entity_map_only")]
        health_counts = Counter(p.get("source_health") for p in portals)
        access_counts = Counter(p.get("access_level") for p in portals)
        by_level = Counter(p.get("government_level") for p in portals)
        ent_types = Counter(e.get("entity_type") for e in self.entities.values())

        def _ec(*types: str) -> int:
            want = {t.upper() for t in types}
            return sum(1 for e in self.entities.values() if str(e.get("entity_type") or "").upper() in want)

        state_rows = []
        for st in US_STATES:
            st_portals = [p for p in portals if p.get("state") == st]
            st_ents = [e for e in self.entities.values() if e.get("state") == st]
            platforms = sorted(
                {
                    str(p.get("platform_family"))
                    for p in st_portals
                    if p.get("platform_family")
                }
            )
            gaps = []
            if not any(p.get("government_level") == "STATE" for p in st_portals):
                gaps.append("NO_STATE_PORTAL_REACHED")
            if not any(e.get("entity_type") == "K12_SCHOOL_DISTRICT" for e in st_ents):
                gaps.append("NO_K12_ENTITY")
            if not any(e.get("entity_type") == "HIGHER_EDUCATION" for e in st_ents):
                gaps.append("NO_HIGHER_ED")
            state_rows.append(
                {
                    "state": st,
                    "state_procurement_source": any(p.get("government_level") == "STATE" for p in st_portals),
                    "local_source_count": sum(1 for p in st_portals if p.get("government_level") == "LOCAL"),
                    "education_source_count": sum(
                        1 for e in st_ents if e.get("entity_type") == "K12_SCHOOL_DISTRICT"
                    ),
                    "higher_ed_source_count": sum(
                        1 for e in st_ents if e.get("entity_type") == "HIGHER_EDUCATION"
                    ),
                    "special_district_source_count": sum(
                        1 for e in st_ents if e.get("entity_type") == "SPECIAL_DISTRICT"
                    ),
                    "major_platform_families": platforms,
                    "cooperative_sources": sum(
                        1
                        for p in portals
                        if p.get("government_level") == "COOPERATIVE"
                        and (p.get("state") in (None, "", st) or not p.get("state"))
                    ),
                    "independent_portals": sum(
                        1
                        for p in st_portals
                        if (p.get("platform_family") or "") in {"SimpleHTML", "StateOwned", None, ""}
                    ),
                    "portal_count": len(st_portals),
                    "entity_count": len(st_ents),
                    "known_gaps": gaps,
                }
            )

        successful = sum(1 for v in self.visits if v.get("health") == "HEALTHY")
        degraded = sum(1 for v in self.visits if v.get("health") == "DEGRADED")
        auth = sum(1 for v in self.visits if v.get("health") == "AUTH_REQUIRED")
        blocked = sum(1 for v in self.visits if v.get("health") in {"BLOCKED", "UNAVAILABLE"})

        newly_discovered_families = Counter(
            p.get("platform_family") or "UNKNOWN"
            for p in portals
            if p.get("discovered_not_seeded")
        )

        biggest_blind_spots = [
            {
                "gap": "K12_DEPTH",
                "detail": f"Only {_ec('K12_SCHOOL_DISTRICT')} K-12 entities vs ~13k+ NCES districts nationally",
            },
            {
                "gap": "COUNTY_SPECIAL_DISTRICT_DEPTH",
                "detail": f"Counties={_ec('COUNTY')}, special districts={_ec('SPECIAL_DISTRICT')}",
            },
            {
                "gap": "DIRECTORY_403_OR_JS",
                "detail": "Some seed directories (e.g. NASPO eProcurement page) return 403/JS walls to automated clients",
            },
            {
                "gap": "AUTH_GATED_STATE_PORTALS",
                "detail": f"{auth} visits classified AUTH_REQUIRED during sweep",
            },
            {
                "gap": "ENTITY_MAP_WITHOUT_OFFICIAL_PORTAL",
                "detail": "BidNet participating-buyers yields entity names; official district/city portals still need resolution",
            },
        ]

        coverage = {
            "kind": "SourceCoverageReport",
            "build": BUILD_TAG,
            "generated_at": _utc(),
            "seeds_loaded": seeds_loaded,
            "seeds_attempted": seeds_attempted,
            "visits": len(self.visits),
            "successful_healthy": successful,
            "degraded": degraded,
            "auth_required": auth,
            "blocked_or_unavailable": blocked,
            "unique_procurement_portals": len(portals),
            "unique_government_entities": len(self.entities),
            "unique_opportunities_discovered": len(self.opportunities),
            "entity_source_relationships": len(self.relationships),
            "federal_sources": by_level.get("FEDERAL", 0),
            "state_sources": by_level.get("STATE", 0),
            "local_sources": by_level.get("LOCAL", 0),
            "network_sources": by_level.get("NETWORK", 0),
            "cooperative_sources": by_level.get("COOPERATIVE", 0),
            "entities_by_type": {
                "k12": _ec("K12_SCHOOL_DISTRICT"),
                "higher_education": _ec("HIGHER_EDUCATION"),
                "county": _ec("COUNTY"),
                "city": _ec("CITY_MUNICIPAL"),
                "special_district": _ec("SPECIAL_DISTRICT"),
                "airport": _ec("AIRPORT"),
                "transit": _ec("TRANSIT"),
                "utility": _ec("UTILITY"),
                "housing": _ec("HOUSING_AUTHORITY"),
                "port": _ec("PORT_AUTHORITY"),
                "library_or_other": _ec("OTHER_PUBLIC"),
                "cooperative": _ec("COOPERATIVE"),
                "federal": _ec("FEDERAL"),
                "state": _ec("STATE"),
            },
            "access_breakdown": dict(access_counts),
            "health_breakdown": dict(health_counts),
            "newly_discovered_not_explicitly_seeded": len(self.discovered_not_seeded),
            "newly_discovered_platform_families": dict(newly_discovered_families),
            "biggest_blind_spots": biggest_blind_spots,
            "honesty": {
                "not_complete_national_census": True,
                "paid_aggregators_not_primary_sources": True,
                "http_200_not_automatically_healthy": True,
                "entity_map_is_not_portal_coverage": True,
            },
        }

        return {
            "inventory": {
                "kind": "ProcurementSourceInventory",
                "build": BUILD_TAG,
                "generated_at": _utc(),
                "portal_count": len(portals),
                "portals": sorted(portals, key=lambda p: str(p.get("source_id") or "")),
                "seeds": self.seeds,
            },
            "entities": {
                "kind": "ProcurementEntityInventory",
                "build": BUILD_TAG,
                "generated_at": _utc(),
                "entity_count": len(self.entities),
                "entities": sorted(self.entities.values(), key=lambda e: str(e.get("entity_id") or "")),
            },
            "relationships": {
                "kind": "SourceEntityRelationships",
                "build": BUILD_TAG,
                "generated_at": _utc(),
                "relationship_count": len(self.relationships),
                "relationships": self.relationships,
            },
            "coverage": coverage,
            "states": {
                "kind": "StateCoverageReport",
                "build": BUILD_TAG,
                "generated_at": _utc(),
                "states": state_rows,
            },
            "failures": {
                "kind": "SourceFailures",
                "build": BUILD_TAG,
                "generated_at": _utc(),
                "failure_count": len(self.failures),
                "failures": self.failures,
            },
            "run": {
                "kind": "SourceDiscoveryRun",
                "build": BUILD_TAG,
                "generated_at": _utc(),
                "seeds_loaded": seeds_loaded,
                "seeds_attempted": seeds_attempted,
                "secondary_visits": self._secondary_budget,
                "visits": self.visits,
                "discovered_not_seeded": self.discovered_not_seeded,
                "opportunities_sample": self.opportunities[:100],
                "opportunity_count": len(self.opportunities),
            },
        }

    def write_artifacts(self, reports: dict[str, Any]) -> dict[str, str]:
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        mapping = {
            "procurement_source_inventory.json": reports["inventory"],
            "procurement_entity_inventory.json": reports["entities"],
            "source_entity_relationships.json": reports["relationships"],
            "source_coverage_report.json": reports["coverage"],
            "state_coverage_report.json": reports["states"],
            "source_failures.json": reports["failures"],
            "source_discovery_run.json": reports["run"],
        }
        paths = {}
        for name, payload in mapping.items():
            path = self.artifacts_dir / name
            path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
            paths[name] = str(path)
        # Human summary
        cov = reports["coverage"]
        md = [
            "# M3 Real Procurement Source Seed Sweep",
            "",
            f"Build: `{BUILD_TAG}`",
            f"Generated: {cov.get('generated_at')}",
            "",
            "## Sweep results",
            "",
            f"- Seeds loaded: {cov['seeds_loaded']}",
            f"- Seeds/visits attempted: {cov['seeds_attempted']} / {cov['visits']}",
            f"- Healthy: {cov['successful_healthy']}",
            f"- Degraded: {cov['degraded']}",
            f"- Auth required: {cov['auth_required']}",
            f"- Blocked/unavailable: {cov['blocked_or_unavailable']}",
            f"- Unique portals: {cov['unique_procurement_portals']}",
            f"- Unique entities: {cov['unique_government_entities']}",
            f"- Unique opportunities parsed: {cov['unique_opportunities_discovered']}",
            f"- Newly discovered (not explicitly seeded): {cov['newly_discovered_not_explicitly_seeded']}",
            "",
            "## Access",
            "",
        ]
        for k, v in sorted((cov.get("access_breakdown") or {}).items(), key=lambda x: str(x[0])):
            md.append(f"- {k}: {v}")
        md += ["", "## Blind spots", ""]
        for g in cov.get("biggest_blind_spots") or []:
            md.append(f"- **{g['gap']}**: {g['detail']}")
        md += [
            "",
            "## Honesty",
            "",
            "- This measures reachable coverage from seeds — not a complete US census.",
            "- Paid aggregators are not primary sources.",
            "- Entity-map directories are not counted as portal coverage for those entities.",
            "",
        ]
        human = self.artifacts_dir / "source_seed_sweep_report.md"
        human.write_text("\n".join(md), encoding="utf-8")
        paths["source_seed_sweep_report.md"] = str(human)
        return paths


def run_seed_sweep(**kwargs: Any) -> dict[str, Any]:
    return ProcurementSourceSeedSweep(**kwargs).run()
