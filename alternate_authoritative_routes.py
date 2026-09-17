"""Authoritative alternate discovery routes — agency/state aggregators vs downstream platforms."""

from __future__ import annotations

from typing import Any

# Registry source_id → preferred public discovery URL + provenance
# Prefer authoritative public listings over gated package portals when both exist.
ALTERNATE_ROUTES: dict[str, dict[str, Any]] = {
    # OpenGov-labeled agency pages with public agency listings
    "agency_city_boston_ma": {
        "discovery_url": "https://www.boston.gov/bid-listings",
        "discovery_source": "boston.gov_bid_listings",
        "authoritative_buyer": "City of Boston",
        "downstream_platform": None,
        "platform_family_override": "SimpleHTML",
        "adapter_family_override": "live_simple_html",
        "reason": "Agency publishes public Drupal bid listings; OpenGov portal not required for discovery",
    },
    "agency_city_seattle_wa": {
        "discovery_url": "https://procurement.opengov.com/portal/seattle?status=open&page=1&limit=10",
        "discovery_source": "opengov_seattle_portal",
        "authoritative_buyer": "City of Seattle",
        "downstream_platform": "OpenGov",
        "agency_landing_url": "https://www.seattle.gov/purchasing-and-contracting",
        "reason": "Agency landing links to OpenGov portal; portal may be bot-protected for automated clients",
        "expected_access": "BOT_PROTECTED_OR_PUBLIC",
    },
    "agency_county_king_wa": {
        "discovery_url": "https://kingcounty.gov/depts/finance/procurement.aspx",
        "discovery_source": "kingcounty_procurement_page",
        "authoritative_buyer": "King County",
        "downstream_platform": "OpenGov",
        "reason": "Prior URL returned 404; restored to active agency procurement page",
    },
    # BidNet — agency pages remapped to BidNet Direct open-bids aggregators
    "agency_city_chicago_il": {
        "discovery_url": "https://www.bidnetdirect.com/illinois/solicitations/open-bids",
        "discovery_source": "bidnet_illinois_open_bids",
        "authoritative_buyer": "City of Chicago / Illinois public agencies",
        "downstream_platform": "BidNet",
        "agency_landing_url": "https://www.chicago.gov/city/en/depts/dps.html",
        "reason": "Illinois BidNet Direct exposes public open-bid metadata; city DPS page is navigation-only",
    },
    "agency_county_cook_il": {
        "discovery_url": "https://www.bidnetdirect.com/illinois/solicitations/open-bids",
        "discovery_source": "bidnet_illinois_open_bids",
        "authoritative_buyer": "Cook County / Illinois public agencies",
        "downstream_platform": "BidNet",
        "reason": "Same Illinois BidNet Direct public aggregator",
    },
    "agency_transit_cta_il": {
        "discovery_url": "https://www.bidnetdirect.com/illinois/solicitations/open-bids",
        "discovery_source": "bidnet_illinois_open_bids",
        "authoritative_buyer": "CTA / Illinois public agencies",
        "downstream_platform": "BidNet",
        "reason": "CTA procurement often surfaces via Illinois BidNet Direct",
    },
    "agency_county_miami_dade_fl": {
        "discovery_url": "https://www.bidnetdirect.com/florida/solicitations/open-bids",
        "discovery_source": "bidnet_florida_open_bids",
        "authoritative_buyer": "Miami-Dade / Florida public agencies",
        "downstream_platform": "BidNet",
        "reason": "Florida BidNet Direct exposes public open-bid titles/closing metadata",
    },
    "state_ny": {
        "discovery_url": "https://www.nyscr.ny.gov/",
        "discovery_source": "nyscr",
        "authoritative_buyer": "New York State",
        "downstream_platform": "NYSCR",
        "reason": "NYSCR is the state contract reporter; BidNet labeling was registry mis-fingerprint",
        "expected_access": "AUTH_OR_REGISTRATION",
    },
    "agency_transit_mta_ny": {
        "discovery_url": "https://new.mta.info/doing-business-with-us/procurement",
        "discovery_source": "mta_procurement_landing",
        "authoritative_buyer": "MTA",
        "downstream_platform": None,
        "reason": "Agency procurement landing; may require follow-on listing discovery",
    },
    # PublicPurchase — keep package platform, discover via state if available
    "state_wy": {
        "discovery_url": "https://www.publicpurchase.com/gems/wyoming/buyer/public/home",
        "discovery_source": "publicpurchase_wyoming_home",
        "authoritative_buyer": "State of Wyoming",
        "downstream_platform": "PublicPurchase",
        "reason": "PublicPurchase buyer home is registration marketing; no public bid table observed",
        "expected_access": "REGISTRATION_REQUIRED",
        "alternate_agency_search": "https://www.wyoming.gov/",
    },
    "agency_city_cheyenne_wy": {
        "discovery_url": "https://www.publicpurchase.com/gems/cheyenne/buyer/public/home",
        "discovery_source": "publicpurchase_cheyenne_home",
        "authoritative_buyer": "City of Cheyenne",
        "downstream_platform": "PublicPurchase",
        "expected_access": "REGISTRATION_REQUIRED",
        "reason": "PublicPurchase marketing landing without public bid list",
    },
}


def apply_alternate_routes(registry) -> dict[str, Any]:
    """Update registry discovery URLs / provenance from alternate route map."""
    applied = []
    for sid, route in ALTERNATE_ROUTES.items():
        s = registry.get(sid)
        if not s:
            continue
        patch = dict(s)
        patch["discovery_url"] = route["discovery_url"]
        patch["alternate_route"] = route
        patch["discovery_source"] = route.get("discovery_source")
        patch["authoritative_buyer"] = route.get("authoritative_buyer")
        patch["downstream_platform"] = route.get("downstream_platform")
        if route.get("platform_family_override"):
            patch["platform_family"] = route["platform_family_override"]
        if route.get("adapter_family_override"):
            patch["adapter_family"] = route["adapter_family_override"]
        registry.upsert(patch)
        applied.append({"source_id": sid, "discovery_url": route["discovery_url"], "reason": route.get("reason")})
    registry.save()
    return {
        "kind": "AlternateAuthoritativeRoutes",
        "applied": applied,
        "count": len(applied),
        "auth_bypassed": False,
        "accounts_created": 0,
    }


def alternate_routes_report() -> dict[str, Any]:
    return {
        "kind": "AlternateAuthoritativeRoutes",
        "routes": [
            {"source_id": sid, **route}
            for sid, route in sorted(ALTERNATE_ROUTES.items())
        ],
        "note": "Authoritative agency/state aggregators preferred over third-party scrapers",
    }
