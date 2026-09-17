"""Source registry seed — LIVE_CAPABLE vs FIXTURE_ONLY vs PLANNED accurately labeled."""

from __future__ import annotations

from typing import Any

from discovery.agency_seeds import all_agencies_enriched, all_coops_enriched, FEDERAL_NON_SAM_LIVE
from discovery.constants import (
    ADAPTER_AUTH_REQUIRED,
    ADAPTER_BLOCKED,
    ADAPTER_FIXTURE_ONLY,
    ADAPTER_IMPLEMENTED,
    ADAPTER_LIVE_VERIFIED,
    ADAPTER_PARTIAL,
    ADAPTER_PLANNED,
    ADAPTER_UNVERIFIED_LIVE,
    CADENCE_FAST,
    CADENCE_NORMAL,
    CADENCE_SLOW,
    HEALTH_DISABLED,
    HEALTH_UNTESTED,
    PLATFORM_BEACON,
    PLATFORM_BIDNET,
    PLATFORM_BONFIRE,
    PLATFORM_DEMANDSTAR,
    PLATFORM_IONWAVE,
    PLATFORM_JAGGAER,
    PLATFORM_OPENGOV,
    PLATFORM_PERISCOPE,
    PLATFORM_PLANETBIDS,
    PLATFORM_PUBLIC_PURCHASE,
    PLATFORM_SIMPLE_HTML,
    SOURCE_COOPERATIVE,
    SOURCE_FEDERAL_PUBLIC,
    SOURCE_STATE,
    TIER_1,
    TIER_2,
    TIER_3,
)
from discovery.state_matrix import STATE_MATRIX, all_states_enriched, enrich_state_row

# Legacy US_STATES for importers
US_STATES = [
    ("AL", "Alabama"), ("AK", "Alaska"), ("AZ", "Arizona"), ("AR", "Arkansas"),
    ("CA", "California"), ("CO", "Colorado"), ("CT", "Connecticut"), ("DE", "Delaware"),
    ("FL", "Florida"), ("GA", "Georgia"), ("HI", "Hawaii"), ("ID", "Idaho"),
    ("IL", "Illinois"), ("IN", "Indiana"), ("IA", "Iowa"), ("KS", "Kansas"),
    ("KY", "Kentucky"), ("LA", "Louisiana"), ("ME", "Maine"), ("MD", "Maryland"),
    ("MA", "Massachusetts"), ("MI", "Michigan"), ("MN", "Minnesota"), ("MS", "Mississippi"),
    ("MO", "Missouri"), ("MT", "Montana"), ("NE", "Nebraska"), ("NV", "Nevada"),
    ("NH", "New Hampshire"), ("NJ", "New Jersey"), ("NM", "New Mexico"), ("NY", "New York"),
    ("NC", "North Carolina"), ("ND", "North Dakota"), ("OH", "Ohio"), ("OK", "Oklahoma"),
    ("OR", "Oregon"), ("PA", "Pennsylvania"), ("RI", "Rhode Island"), ("SC", "South Carolina"),
    ("SD", "South Dakota"), ("TN", "Tennessee"), ("TX", "Texas"), ("UT", "Utah"),
    ("VT", "Vermont"), ("VA", "Virginia"), ("WA", "Washington"), ("WV", "West Virginia"),
    ("WI", "Wisconsin"), ("WY", "Wyoming"),
]

COOPERATIVES = [
    ("coop_naspo", "NASPO ValuePoint", "https://www.naspovaluepoint.org"),
    ("coop_sourcewell", "Sourcewell", "https://www.sourcewell-mn.gov"),
    ("coop_omnia", "OMNIA Partners Public Sector", "https://www.omniapartners.com"),
    ("coop_hgac", "HGACBuy", "https://www.hgacbuy.org"),
    ("coop_buyboard", "BuyBoard", "https://www.buyboard.com"),
    ("coop_1gpa", "1Government Procurement Alliance", None),
]

# Platform family registry — UNVERIFIED until live-validated
PLATFORM_FAMILIES_SEED = [
    ("platform_bonfire", PLATFORM_BONFIRE, CADENCE_NORMAL, ADAPTER_UNVERIFIED_LIVE, "live_bonfire", False),
    ("platform_opengov", PLATFORM_OPENGOV, CADENCE_NORMAL, ADAPTER_UNVERIFIED_LIVE, "live_opengov", False),
    ("platform_ionwave", PLATFORM_IONWAVE, CADENCE_SLOW, ADAPTER_AUTH_REQUIRED, None, True),
    ("platform_planetbids", PLATFORM_PLANETBIDS, CADENCE_NORMAL, ADAPTER_UNVERIFIED_LIVE, "live_planetbids", False),
    ("platform_bidnet", PLATFORM_BIDNET, CADENCE_NORMAL, ADAPTER_UNVERIFIED_LIVE, "live_bidnet", False),
    ("platform_demandstar", PLATFORM_DEMANDSTAR, CADENCE_SLOW, ADAPTER_AUTH_REQUIRED, None, True),
    ("platform_jaggaer", PLATFORM_JAGGAER, CADENCE_SLOW, ADAPTER_UNVERIFIED_LIVE, "live_jaggaer", False),
    ("platform_periscope", PLATFORM_PERISCOPE, CADENCE_SLOW, ADAPTER_AUTH_REQUIRED, None, True),
    ("platform_public_purchase", PLATFORM_PUBLIC_PURCHASE, CADENCE_NORMAL, ADAPTER_UNVERIFIED_LIVE, "live_public_purchase", False),
    ("platform_beacon", PLATFORM_BEACON, CADENCE_SLOW, ADAPTER_AUTH_REQUIRED, None, True),
]


def build_registry_seed() -> list[dict[str, Any]]:
    """Full registry — statuses reflect production live capability honestly."""
    rows: list[dict[str, Any]] = []

    # Fixture-only adapters (parser proven; NOT live-capable)
    for sid, name, stype, method in [
        ("fixture_html_city_bids", "Fixture City Bid Table", "CITY", "HTML_TABLE"),
        ("fixture_json_state_bids", "Fixture State JSON Portal", SOURCE_STATE, "JSON_ENDPOINT"),
        ("fixture_rss_county_bids", "Fixture County RSS Feed", "COUNTY", "RSS"),
        ("fixture_coop_sourcewell_style", "Fixture Cooperative Listing", SOURCE_COOPERATIVE, "JSON_ENDPOINT"),
        ("fixture_federal_public_notice", "Fixture Federal Public Notice", SOURCE_FEDERAL_PUBLIC, "PUBLIC_HTML"),
        ("fixture_shared_platform_listing", "Fixture Shared Platform Style", "CITY", "PLATFORM_FAMILY"),
    ]:
        rows.append(
            {
                "source_id": sid,
                "source_name": name,
                "source_type": stype,
                "jurisdiction": stype,
                "platform_family": PLATFORM_SIMPLE_HTML if "html" in sid else None,
                "discovery_method": method,
                "adapter_status": ADAPTER_FIXTURE_ONLY,
                "trust_tier": TIER_2 if "coop" in sid else TIER_1,
                "enabled": True,
                "health_status": HEALTH_UNTESTED,
                "auth_required": False,
                "notes": "FIXTURE_ONLY — parser proven on fixtures; not LIVE_CAPABLE",
                "metadata_json": {
                    "cadence": CADENCE_FAST,
                    "operational_for_live": False,
                    "live_capable": False,
                },
            }
        )

    # Federal non-SAM
    for f in FEDERAL_NON_SAM_LIVE:
        rows.append(
            {
                "source_id": f["source_id"],
                "source_name": f["name"],
                "source_type": SOURCE_FEDERAL_PUBLIC,
                "jurisdiction": "FEDERAL",
                "list_url": f.get("list_url"),
                "discovery_method": "PUBLIC_HTML",
                "adapter_status": ADAPTER_UNVERIFIED_LIVE if f.get("list_url") else ADAPTER_PARTIAL,
                "trust_tier": TIER_1,
                "enabled": False,  # only after LIVE_VERIFIED
                "health_status": HEALTH_UNTESTED,
                "auth_required": False,
                "notes": f.get("note") or "",
                "metadata_json": {
                    "adapter_family": f.get("adapter_family"),
                    "live_capable": False,
                    "unverified_live": bool(f.get("list_url")),
                    "sam_api_broad_discovery": False,
                    "cadence": CADENCE_NORMAL,
                },
            }
        )

    rows.append(
        {
            "source_id": "sam_gov_api",
            "source_name": "SAM.gov API (scarce — not broad discovery)",
            "source_type": SOURCE_FEDERAL_PUBLIC,
            "jurisdiction": "FEDERAL",
            "discovery_method": "SAM_API",
            "adapter_status": ADAPTER_AUTH_REQUIRED,
            "trust_tier": TIER_1,
            "enabled": False,
            "health_status": HEALTH_DISABLED,
            "auth_required": True,
            "notes": "DISABLED for broad scanning. Late-stage verification only.",
            "metadata_json": {
                "sam_api_broad_discovery_enabled": False,
                "operational": False,
                "live_capable": False,
            },
        }
    )

    # 50 states from matrix (configured seed status — not live overrides)
    for raw in STATE_MATRIX:
        s = enrich_state_row(raw, status_overrides={})  # seed ignores live overrides
        # Prefer explicit seed status for registry upsert baseline
        seed_status = raw.get("adapter_status_seed") or s["adapter_status"]
        rows.append(
            {
                "source_id": s["source_id"],
                "source_name": f"{s['name']} — {s['portal_name']}",
                "source_type": SOURCE_STATE,
                "jurisdiction": "STATE",
                "state_code": s["state"],
                "platform_family": s.get("platform_family"),
                "list_url": s.get("list_url"),
                "discovery_method": "PORTAL",
                "adapter_status": seed_status,
                "trust_tier": TIER_1,
                "enabled": seed_status == ADAPTER_LIVE_VERIFIED,
                "health_status": HEALTH_UNTESTED,
                "auth_required": bool(s.get("auth_required")),
                "notes": s.get("restrictions") or "State primary procurement portal",
                "metadata_json": {
                    "adapter_family": s.get("adapter_family"),
                    "live_capable": seed_status == ADAPTER_LIVE_VERIFIED,
                    "unverified_live": seed_status == ADAPTER_UNVERIFIED_LIVE,
                    "validation_candidate": s.get("validation_candidate"),
                    "docs_public": s.get("docs_public"),
                    "publicly_searchable": s.get("publicly_searchable"),
                    "last_verified_architecture": s.get("last_verified_architecture"),
                    "cadence": CADENCE_NORMAL,
                },
            }
        )

    # Platform families
    for sid, family, cadence, status, adapter_family, auth in PLATFORM_FAMILIES_SEED:
        rows.append(
            {
                "source_id": sid,
                "source_name": f"{family} Platform Family",
                "source_type": "OTHER_PUBLIC",
                "jurisdiction": "MULTI",
                "platform_family": family,
                "discovery_method": "PLATFORM_FAMILY",
                "adapter_status": status,
                "trust_tier": TIER_1,
                "enabled": status == ADAPTER_LIVE_VERIFIED,
                "health_status": HEALTH_UNTESTED,
                "auth_required": auth,
                "notes": (
                    "Shared platform — agencies onboard via agency registry; UNVERIFIED until live-validated"
                    if status == ADAPTER_UNVERIFIED_LIVE
                    else "Public automated retrieval blocked or auth-gated"
                ),
                "metadata_json": {
                    "cadence": cadence,
                    "adapter_family": adapter_family,
                    "live_capable": False,
                    "unverified_live": status == ADAPTER_UNVERIFIED_LIVE,
                },
            }
        )

    # Cooperative LIVE sources (open solicitations)
    for c in all_coops_enriched():
        rows.append(
            {
                "source_id": c["source_id"],
                "source_name": c["name"],
                "source_type": SOURCE_COOPERATIVE,
                "jurisdiction": "NATIONAL",
                "list_url": c.get("list_url"),
                "discovery_method": "COOPERATIVE_PORTAL",
                "adapter_status": c["adapter_status"],
                "trust_tier": TIER_2,
                "enabled": bool(c.get("live_verified")),
                "health_status": HEALTH_UNTESTED,
                "auth_required": False,
                "notes": c.get("note") or "Open solicitations only — not awarded catalogs",
                "metadata_json": {
                    "adapter_family": c.get("adapter_family"),
                    "live_capable": bool(c.get("live_verified")),
                    "unverified_live": bool(c.get("unverified_live")),
                    "open_solicitations_only": True,
                    "cadence": CADENCE_NORMAL,
                },
            }
        )

    # Legacy coop name stubs (PLANNED redirects to live ids)
    for sid, name, url in COOPERATIVES:
        if any(c["source_id"].startswith(sid) for c in all_coops_enriched()):
            continue
        rows.append(
            {
                "source_id": sid,
                "source_name": name,
                "source_type": SOURCE_COOPERATIVE,
                "jurisdiction": "NATIONAL",
                "list_url": url,
                "discovery_method": "COOPERATIVE_PORTAL",
                "adapter_status": ADAPTER_PLANNED,
                "trust_tier": TIER_2,
                "enabled": False,
                "health_status": HEALTH_UNTESTED,
                "auth_required": False,
                "notes": "See coop_*_live LIVE_CAPABLE entries for open solicitations",
                "metadata_json": {"operational": False, "live_capable": False},
            }
        )

    # Aggregators
    for sid, name in [
        ("agg_bidnet_index", "BidNet-style public index (discovery lead)"),
        ("agg_generic_bid_index", "Generic public bid index (discovery lead)"),
    ]:
        rows.append(
            {
                "source_id": sid,
                "source_name": name,
                "source_type": "OTHER_PUBLIC",
                "jurisdiction": "MULTI",
                "discovery_method": "AGGREGATOR",
                "adapter_status": ADAPTER_PLANNED,
                "trust_tier": TIER_3,
                "enabled": False,
                "health_status": HEALTH_UNTESTED,
                "auth_required": False,
                "notes": "Discovery lead only — prefer official source when established.",
                "metadata_json": {"operational": False, "live_capable": False},
            }
        )

    # Local category buckets (agencies live in agency registry)
    for sid, name, stype in [
        ("local_counties_future", "County procurement (agency registry)", "COUNTY"),
        ("local_cities_future", "City procurement (agency registry)", "CITY"),
        ("local_schools_future", "School district procurement", "SCHOOL_DISTRICT"),
        ("local_universities_future", "Public university/college procurement", "PUBLIC_UNIVERSITY"),
        ("local_airports_future", "Airport procurement", "AIRPORT"),
        ("local_transit_future", "Transit agency procurement", "TRANSIT"),
        ("local_utilities_future", "Public utility procurement", "PUBLIC_UTILITY"),
        ("local_special_districts_future", "Special district procurement", "SPECIAL_DISTRICT"),
    ]:
        rows.append(
            {
                "source_id": sid,
                "source_name": name,
                "source_type": stype,
                "jurisdiction": "LOCAL",
                "discovery_method": "AGENCY_REGISTRY",
                "adapter_status": ADAPTER_PLANNED,
                "trust_tier": TIER_1,
                "enabled": False,
                "health_status": HEALTH_UNTESTED,
                "auth_required": False,
                "notes": "Scalable via platform-family + agency seeds — not hand-coded per agency.",
                "metadata_json": {"operational": False, "live_capable": False},
            }
        )

    return rows


def seed_discovery_sources(session: Any, *, overwrite_notes: bool = False) -> dict[str, Any]:
    """Upsert registry rows. Does not invent opportunity facts."""
    from models import DiscoverySource

    seed = build_registry_seed()
    added = updated = 0
    for row in seed:
        existing = session.query(DiscoverySource).filter_by(source_id=row["source_id"]).first()
        if existing is None:
            session.add(DiscoverySource(**{k: v for k, v in row.items() if hasattr(DiscoverySource, k)}))
            added += 1
        else:
            # Never wipe live validation results with seed defaults
            if existing.last_live_validation_result:
                row = dict(row)
                row["adapter_status"] = existing.last_live_validation_result
                row["enabled"] = existing.last_live_validation_result == ADAPTER_LIVE_VERIFIED
            for k, v in row.items():
                if k == "source_id":
                    continue
                # Preserve FIXTURE_ONLY / legacy IMPLEMENTED fixture rows from accidental wipe
                if k == "adapter_status" and existing.adapter_status in {
                    ADAPTER_FIXTURE_ONLY,
                    ADAPTER_IMPLEMENTED,
                    "IMPLEMENTED",
                    ADAPTER_LIVE_VERIFIED,
                }:
                    if existing.adapter_status == ADAPTER_LIVE_VERIFIED:
                        continue
                    if v not in {ADAPTER_FIXTURE_ONLY, ADAPTER_IMPLEMENTED}:
                        # Allow correcting IMPLEMENTED → FIXTURE_ONLY
                        if existing.adapter_status in {"IMPLEMENTED", ADAPTER_IMPLEMENTED} and v == ADAPTER_FIXTURE_ONLY:
                            setattr(existing, k, v)
                        continue
                if k.startswith("last_live_") or k == "validation_notes":
                    continue
                if hasattr(existing, k):
                    if k == "notes" and not overwrite_notes and existing.notes:
                        continue
                    setattr(existing, k, v)
            updated += 1
    session.flush()
    agencies = seed_discovery_agencies(session)
    return {
        "added": added,
        "updated": updated,
        "total_seed": len(seed),
        "agencies": agencies,
        "LIVE_API_REQUESTS": 0,
    }


def seed_discovery_agencies(session: Any) -> dict[str, Any]:
    """Upsert agency seeds — platform adapter reuse, no new parsers."""
    from models import DiscoveryAgency

    added = updated = 0
    for a in all_agencies_enriched():
        existing = session.query(DiscoveryAgency).filter_by(agency_key=a["agency_key"]).first()
        payload = {
            "agency_key": a["agency_key"],
            "name": a["name"],
            "buyer_type": a["buyer_type"],
            "state_code": a.get("state_code"),
            "city": a.get("city"),
            "jurisdiction": a.get("jurisdiction"),
            "procurement_url": a.get("procurement_url"),
            "platform_family": a.get("platform_family"),
            "source_id": a.get("source_id"),
            "platform_detection": a.get("platform_family"),
            "enabled": bool(a.get("enabled")),
            "live_capable": bool(a.get("live_capable")),
            "last_verified": a.get("last_verified"),
            "notes": f"adapter_family={a.get('adapter_family')}",
            "metadata_json": {
                "adapter_family": a.get("adapter_family"),
                "live_capable": a.get("live_capable"),
            },
        }
        if existing is None:
            session.add(DiscoveryAgency(**{k: v for k, v in payload.items() if hasattr(DiscoveryAgency, k)}))
            added += 1
        else:
            for k, v in payload.items():
                if k == "agency_key":
                    continue
                if hasattr(existing, k):
                    setattr(existing, k, v)
            updated += 1
    session.flush()
    return {"added": added, "updated": updated, "total": len(all_agencies_enriched())}


def registry_summary(session: Any) -> dict[str, Any]:
    from models import DiscoverySource

    rows = session.query(DiscoverySource).all()
    counts = {
        "total": len(rows),
        "live_verified": 0,
        "unverified_live": 0,
        "live_capable": 0,  # strict alias of live_verified
        "fixture_only": 0,
        "implemented": 0,
        "partial": 0,
        "planned": 0,
        "auth_required": 0,
        "blocked": 0,
        "broken": 0,
        "degraded": 0,
        "unsupported": 0,
        "enabled": 0,
        "operational_false_planned": 0,
    }
    for r in rows:
        st = (r.adapter_status or "").upper()
        if st == ADAPTER_LIVE_VERIFIED:
            counts["live_verified"] += 1
            counts["live_capable"] += 1
        elif st in {ADAPTER_UNVERIFIED_LIVE, "LIVE_CAPABLE"}:
            counts["unverified_live"] += 1
        elif st in {ADAPTER_FIXTURE_ONLY, "IMPLEMENTED", ADAPTER_IMPLEMENTED}:
            counts["fixture_only"] += 1
            counts["implemented"] += 1
        elif st == ADAPTER_PARTIAL:
            counts["partial"] += 1
        elif st == ADAPTER_AUTH_REQUIRED:
            counts["auth_required"] += 1
        elif st == ADAPTER_BLOCKED:
            counts["blocked"] += 1
        elif st == "BROKEN":
            counts["broken"] += 1
        elif st == "DEGRADED":
            counts["degraded"] += 1
        elif st == ADAPTER_PLANNED:
            counts["planned"] += 1
            counts["operational_false_planned"] += 1
        else:
            counts["unsupported"] += 1
        if r.enabled:
            counts["enabled"] += 1
    return {**counts, "LIVE_API_REQUESTS": 0}
