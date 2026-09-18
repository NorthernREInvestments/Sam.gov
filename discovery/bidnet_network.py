"""BidNet Direct statewide network sources — one public open-bids listing unlocks many agencies."""

from __future__ import annotations

from typing import Any

from discovery.constants import ADAPTER_UNVERIFIED_LIVE, PLATFORM_BIDNET

# Public BidNet Direct purchasing-group open-bids endpoints.
# One productive statewide listing covers cities/counties/schools on that network.
# Do NOT treat these as claims of exclusive statewide coverage of all state spending.
BIDNET_STATE_NETWORKS: list[dict[str, Any]] = [
    {"slug": "alabama", "state_code": "AL", "name": "BidNet Direct — Alabama"},
    {"slug": "alaska", "state_code": "AK", "name": "BidNet Direct — Alaska"},
    {"slug": "arizona", "state_code": "AZ", "name": "BidNet Direct — Arizona"},
    {"slug": "arkansas", "state_code": "AR", "name": "BidNet Direct — Arkansas"},
    {"slug": "california", "state_code": "CA", "name": "BidNet Direct — California"},
    {"slug": "colorado", "state_code": "CO", "name": "BidNet Direct — Colorado"},
    {"slug": "connecticut", "state_code": "CT", "name": "BidNet Direct — Connecticut"},
    {"slug": "delaware", "state_code": "DE", "name": "BidNet Direct — Delaware"},
    {"slug": "florida", "state_code": "FL", "name": "BidNet Direct — Florida"},
    {"slug": "georgia", "state_code": "GA", "name": "BidNet Direct — Georgia"},
    {"slug": "hawaii", "state_code": "HI", "name": "BidNet Direct — Hawaii"},
    {"slug": "idaho", "state_code": "ID", "name": "BidNet Direct — Idaho"},
    {"slug": "illinois", "state_code": "IL", "name": "BidNet Direct — Illinois"},
    {"slug": "indiana", "state_code": "IN", "name": "BidNet Direct — Indiana"},
    {"slug": "iowa", "state_code": "IA", "name": "BidNet Direct — Iowa"},
    {"slug": "kansas", "state_code": "KS", "name": "BidNet Direct — Kansas"},
    {"slug": "kentucky", "state_code": "KY", "name": "BidNet Direct — Kentucky"},
    {"slug": "louisiana", "state_code": "LA", "name": "BidNet Direct — Louisiana"},
    {"slug": "maine", "state_code": "ME", "name": "BidNet Direct — Maine"},
    {"slug": "maryland", "state_code": "MD", "name": "BidNet Direct — Maryland"},
    {"slug": "massachusetts", "state_code": "MA", "name": "BidNet Direct — Massachusetts"},
    {"slug": "michigan", "state_code": "MI", "name": "BidNet Direct — Michigan"},
    {"slug": "minnesota", "state_code": "MN", "name": "BidNet Direct — Minnesota"},
    {"slug": "mississippi", "state_code": "MS", "name": "BidNet Direct — Mississippi"},
    {"slug": "missouri", "state_code": "MO", "name": "BidNet Direct — Missouri"},
    {"slug": "montana", "state_code": "MT", "name": "BidNet Direct — Montana"},
    {"slug": "nebraska", "state_code": "NE", "name": "BidNet Direct — Nebraska"},
    {"slug": "nevada", "state_code": "NV", "name": "BidNet Direct — Nevada"},
    {"slug": "new-hampshire", "state_code": "NH", "name": "BidNet Direct — New Hampshire"},
    {"slug": "new-jersey", "state_code": "NJ", "name": "BidNet Direct — New Jersey"},
    {"slug": "new-mexico", "state_code": "NM", "name": "BidNet Direct — New Mexico"},
    {"slug": "new-york", "state_code": "NY", "name": "BidNet Direct — New York"},
    {"slug": "north-carolina", "state_code": "NC", "name": "BidNet Direct — North Carolina"},
    {"slug": "north-dakota", "state_code": "ND", "name": "BidNet Direct — North Dakota"},
    {"slug": "ohio", "state_code": "OH", "name": "BidNet Direct — Ohio"},
    {"slug": "oklahoma", "state_code": "OK", "name": "BidNet Direct — Oklahoma"},
    {"slug": "oregon", "state_code": "OR", "name": "BidNet Direct — Oregon"},
    {"slug": "pennsylvania", "state_code": "PA", "name": "BidNet Direct — Pennsylvania"},
    {"slug": "rhode-island", "state_code": "RI", "name": "BidNet Direct — Rhode Island"},
    {"slug": "south-carolina", "state_code": "SC", "name": "BidNet Direct — South Carolina"},
    {"slug": "south-dakota", "state_code": "SD", "name": "BidNet Direct — South Dakota"},
    {"slug": "tennessee", "state_code": "TN", "name": "BidNet Direct — Tennessee"},
    {"slug": "texas", "state_code": "TX", "name": "BidNet Direct — Texas"},
    {"slug": "utah", "state_code": "UT", "name": "BidNet Direct — Utah"},
    {"slug": "vermont", "state_code": "VT", "name": "BidNet Direct — Vermont"},
    {"slug": "virginia", "state_code": "VA", "name": "BidNet Direct — Virginia"},
    {"slug": "washington", "state_code": "WA", "name": "BidNet Direct — Washington"},
    {"slug": "west-virginia", "state_code": "WV", "name": "BidNet Direct — West Virginia"},
    {"slug": "wisconsin", "state_code": "WI", "name": "BidNet Direct — Wisconsin"},
    {"slug": "wyoming", "state_code": "WY", "name": "BidNet Direct — Wyoming"},
    {"slug": "district-of-columbia", "state_code": "DC", "name": "BidNet Direct — District of Columbia"},
]


def enrich_bidnet_network(row: dict[str, Any]) -> dict[str, Any]:
    slug = row["slug"]
    out = dict(row)
    out["source_id"] = f"network_bidnet_{slug.replace('-', '_')}"
    out["list_url"] = f"https://www.bidnetdirect.com/{slug}/solicitations/open-bids"
    out["adapter_family"] = "live_bidnet"
    out["platform_family"] = PLATFORM_BIDNET
    out["kind"] = "NETWORK"
    out["jurisdiction"] = "STATE_NETWORK"
    out["buyer_type"] = "MULTI_AGENCY_NETWORK"
    out["coverage_class"] = "LOCAL_NETWORK"  # NOT automatic STATEWIDE_SOURCE_VERIFIED
    out["adapter_status"] = ADAPTER_UNVERIFIED_LIVE
    out["live_verified"] = False
    out["live_capable"] = False
    out["validation_candidate"] = True
    out["fetcher_available"] = True
    out["unverified_live"] = True
    out["enabled"] = True
    out["restrictions"] = (
        "BidNet Direct public open-bids metadata; packages often AUTH_GATED; "
        "productive network coverage ≠ exclusive statewide purchasing system"
    )
    return out


def all_bidnet_networks_enriched() -> list[dict[str, Any]]:
    return [enrich_bidnet_network(r) for r in BIDNET_STATE_NETWORKS]
