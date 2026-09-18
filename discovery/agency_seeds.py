"""Initial geographically diverse local agency seeds for platform-family adapters."""

from __future__ import annotations

from typing import Any

from discovery.constants import (
    PLATFORM_BIDNET,
    PLATFORM_BONFIRE,
    PLATFORM_OPENGOV,
    PLATFORM_PLANETBIDS,
    PLATFORM_PUBLIC_PURCHASE,
)

# Enough nationwide diversity to prove architecture — not every agency.
AGENCY_SEEDS: list[dict[str, Any]] = [
    # Large cities
    {"agency_key": "city_los_angeles_ca", "name": "City of Los Angeles", "buyer_type": "CITY", "state_code": "CA", "city": "Los Angeles", "jurisdiction": "CITY", "procurement_url": "https://www.labavn.org/", "platform_family": PLATFORM_PLANETBIDS, "adapter_family": "live_planetbids"},
    {"agency_key": "city_houston_tx", "name": "City of Houston", "buyer_type": "CITY", "state_code": "TX", "city": "Houston", "jurisdiction": "CITY", "procurement_url": "https://purchasing.houstontx.gov/", "platform_family": PLATFORM_BONFIRE, "adapter_family": "live_bonfire"},
    {"agency_key": "city_chicago_il", "name": "City of Chicago", "buyer_type": "CITY", "state_code": "IL", "city": "Chicago", "jurisdiction": "CITY", "procurement_url": "https://www.bidnetdirect.com/illinois", "platform_family": PLATFORM_BIDNET, "adapter_family": "live_bidnet", "restrictions": "BidNet Direct IL public open solicitations (metadata); packages often gated"},
    {"agency_key": "city_phoenix_az", "name": "City of Phoenix", "buyer_type": "CITY", "state_code": "AZ", "city": "Phoenix", "jurisdiction": "CITY", "procurement_url": "https://solicitations.phoenix.gov/", "platform_family": PLATFORM_BONFIRE, "adapter_family": "live_bonfire"},
    {"agency_key": "city_seattle_wa", "name": "City of Seattle", "buyer_type": "CITY", "state_code": "WA", "city": "Seattle", "jurisdiction": "CITY", "procurement_url": "https://www.seattle.gov/purchasing-and-contracting", "platform_family": PLATFORM_OPENGOV, "adapter_family": "live_opengov"},
    {"agency_key": "city_denver_co", "name": "City and County of Denver", "buyer_type": "CITY", "state_code": "CO", "city": "Denver", "jurisdiction": "CITY", "procurement_url": "https://www.denvergov.org/Government/Agencies-Departments-Offices/Agencies-Departments-Offices-Directory/Department-of-Finance/Our-Divisions/Purchasing", "platform_family": PLATFORM_BONFIRE, "adapter_family": "live_bonfire"},
    {"agency_key": "city_atlanta_ga", "name": "City of Atlanta", "buyer_type": "CITY", "state_code": "GA", "city": "Atlanta", "jurisdiction": "CITY", "procurement_url": "https://www.atlantaga.gov/government/departments/procurement", "platform_family": PLATFORM_BONFIRE, "adapter_family": "live_bonfire"},
    {"agency_key": "city_boston_ma", "name": "City of Boston", "buyer_type": "CITY", "state_code": "MA", "city": "Boston", "jurisdiction": "CITY", "procurement_url": "https://www.boston.gov/departments/procurement", "platform_family": PLATFORM_OPENGOV, "adapter_family": "live_opengov"},
    # Large counties
    {"agency_key": "county_cook_il", "name": "Cook County", "buyer_type": "COUNTY", "state_code": "IL", "city": "Chicago", "jurisdiction": "COUNTY", "procurement_url": "https://www.bidnetdirect.com/illinois", "platform_family": PLATFORM_BIDNET, "adapter_family": "live_bidnet", "restrictions": "Shares IL BidNet Direct public listing"},
    {"agency_key": "county_harris_tx", "name": "Harris County", "buyer_type": "COUNTY", "state_code": "TX", "city": "Houston", "jurisdiction": "COUNTY", "procurement_url": "https://www.harriscountytx.gov/Purchasing", "platform_family": PLATFORM_BONFIRE, "adapter_family": "live_bonfire"},
    {"agency_key": "county_maricopa_az", "name": "Maricopa County", "buyer_type": "COUNTY", "state_code": "AZ", "city": "Phoenix", "jurisdiction": "COUNTY", "procurement_url": "https://www.maricopa.gov/3978/Procurement", "platform_family": PLATFORM_BONFIRE, "adapter_family": "live_bonfire"},
    {"agency_key": "county_miami_dade_fl", "name": "Miami-Dade County", "buyer_type": "COUNTY", "state_code": "FL", "city": "Miami", "jurisdiction": "COUNTY", "procurement_url": "https://www.bidnetdirect.com/florida", "platform_family": PLATFORM_BIDNET, "adapter_family": "live_bidnet", "restrictions": "BidNet Direct FL public open solicitations"},
    {"agency_key": "county_king_wa", "name": "King County", "buyer_type": "COUNTY", "state_code": "WA", "city": "Seattle", "jurisdiction": "COUNTY", "procurement_url": "https://kingcounty.gov/depts/finance/procurement.aspx", "platform_family": PLATFORM_OPENGOV, "adapter_family": "live_opengov"},
    # School districts / universities
    {"agency_key": "isd_houston_tx", "name": "Houston Independent School District", "buyer_type": "SCHOOL_DISTRICT", "state_code": "TX", "city": "Houston", "jurisdiction": "SCHOOL_DISTRICT", "procurement_url": "https://www.houstonisd.org/procurement", "platform_family": PLATFORM_BONFIRE, "adapter_family": "live_bonfire"},
    {"agency_key": "usd_la_ca", "name": "Los Angeles Unified School District", "buyer_type": "SCHOOL_DISTRICT", "state_code": "CA", "city": "Los Angeles", "jurisdiction": "SCHOOL_DISTRICT", "procurement_url": "https://www.lausd.org/procurement", "platform_family": PLATFORM_PLANETBIDS, "adapter_family": "live_planetbids"},
    {"agency_key": "univ_texas_austin", "name": "University of Texas at Austin", "buyer_type": "PUBLIC_UNIVERSITY", "state_code": "TX", "city": "Austin", "jurisdiction": "PUBLIC_UNIVERSITY", "procurement_url": "https://procurement.utexas.edu/", "platform_family": PLATFORM_BONFIRE, "adapter_family": "live_bonfire"},
    {"agency_key": "univ_uc_system", "name": "University of California Procurement", "buyer_type": "PUBLIC_UNIVERSITY", "state_code": "CA", "city": "Oakland", "jurisdiction": "PUBLIC_UNIVERSITY", "procurement_url": "https://www.ucop.edu/procurement-services/", "platform_family": PLATFORM_OPENGOV, "adapter_family": "live_opengov"},
    {"agency_key": "univ_michigan", "name": "University of Michigan", "buyer_type": "PUBLIC_UNIVERSITY", "state_code": "MI", "city": "Ann Arbor", "jurisdiction": "PUBLIC_UNIVERSITY", "procurement_url": "https://procurement.umich.edu/", "platform_family": PLATFORM_OPENGOV, "adapter_family": "live_opengov"},
    # Airports / transit / utilities
    {"agency_key": "airport_dfw_tx", "name": "Dallas/Fort Worth International Airport", "buyer_type": "AIRPORT", "state_code": "TX", "city": "DFW", "jurisdiction": "AIRPORT", "procurement_url": "https://www.dfwairport.com/business/solicitations/", "platform_family": PLATFORM_BONFIRE, "adapter_family": "live_bonfire"},
    {"agency_key": "airport_lax_ca", "name": "Los Angeles World Airports", "buyer_type": "AIRPORT", "state_code": "CA", "city": "Los Angeles", "jurisdiction": "AIRPORT", "procurement_url": "https://www.lawa.org/lawa-businesses/lawa-business-opportunities", "platform_family": PLATFORM_PLANETBIDS, "adapter_family": "live_planetbids"},
    {"agency_key": "transit_cta_il", "name": "Chicago Transit Authority", "buyer_type": "TRANSIT", "state_code": "IL", "city": "Chicago", "jurisdiction": "TRANSIT", "procurement_url": "https://www.bidnetdirect.com/illinois", "platform_family": PLATFORM_BIDNET, "adapter_family": "live_bidnet"},
    {"agency_key": "transit_mta_ny", "name": "MTA New York", "buyer_type": "TRANSIT", "state_code": "NY", "city": "New York", "jurisdiction": "TRANSIT", "procurement_url": "https://new.mta.info/doing-business-with-us/procurement", "platform_family": PLATFORM_BIDNET, "adapter_family": "live_bidnet"},
    {"agency_key": "utility_ladwp_ca", "name": "Los Angeles Department of Water and Power", "buyer_type": "PUBLIC_UTILITY", "state_code": "CA", "city": "Los Angeles", "jurisdiction": "PUBLIC_UTILITY", "procurement_url": "https://www.ladwp.com/ladwp/faces/ladwp/aboutus/a-procurement", "platform_family": PLATFORM_PLANETBIDS, "adapter_family": "live_planetbids"},
    {"agency_key": "city_cheyenne_wy", "name": "City of Cheyenne", "buyer_type": "CITY", "state_code": "WY", "city": "Cheyenne", "jurisdiction": "CITY", "procurement_url": "https://www.publicpurchase.com/gems/cheyenne/buyer/public/home", "platform_family": PLATFORM_PUBLIC_PURCHASE, "adapter_family": "live_public_purchase", "auth_required": True, "restrictions": "PublicPurchase /home is marketing/vendor-registration; open bids not publicly listable without login (observed 2026-09-15)", "validation_candidate": False},
]


def enrich_agency(row: dict[str, Any]) -> dict[str, Any]:
    from discovery.live_fetchers import get_live_fetcher
    from discovery.constants import ADAPTER_UNVERIFIED_LIVE
    from discovery.state_matrix import get_status_overrides

    out = dict(row)
    fetcher = get_live_fetcher(row.get("adapter_family") or "")
    out["source_id"] = f"agency_{row['agency_key']}"
    overrides = get_status_overrides()
    if out["source_id"] in overrides:
        out["adapter_status"] = overrides[out["source_id"]]
        out["live_verified"] = overrides[out["source_id"]] == "LIVE_VERIFIED"
        out["live_capable"] = out["live_verified"]
    else:
        out["live_verified"] = False
        out["live_capable"] = False  # strict
        out["adapter_status"] = ADAPTER_UNVERIFIED_LIVE if (fetcher and row.get("procurement_url")) else "PLANNED"
    out["fetcher_available"] = bool(fetcher)
    out["unverified_live"] = out["adapter_status"] == ADAPTER_UNVERIFIED_LIVE
    out["enabled"] = out.get("live_verified", False)
    out["last_verified"] = None
    if row.get("auth_required"):
        out["adapter_status"] = "AUTH_REQUIRED"
        out["unverified_live"] = False
        out["validation_candidate"] = False
    else:
        out["validation_candidate"] = bool(row.get("validation_candidate", True))
    return out


def all_agencies_enriched() -> list[dict[str, Any]]:
    return [enrich_agency(a) for a in AGENCY_SEEDS]


COOPERATIVE_LIVE_SOURCES: list[dict[str, Any]] = [
    {
        "source_id": "coop_naspo_live",
        "name": "NASPO ValuePoint — Open Solicitations",
        "list_url": "https://www.naspovaluepoint.org/solicitations/",
        "adapter_family": "live_cooperative",
        "note": "Open solicitations only — not awarded contract catalogs",
    },
    {
        "source_id": "coop_sourcewell_live",
        "name": "Sourcewell — Open Solicitations",
        "list_url": "https://www.sourcewell-mn.gov/solicitations",
        "adapter_family": "live_cooperative",
        "note": "Open solicitations page — catalogs are not open bids",
    },
    {
        "source_id": "coop_omnia_live",
        "name": "OMNIA Partners Public Sector — Solicitations",
        "list_url": "https://www.omniapartners.com/publicsector/contract-opportunities",
        "adapter_family": "live_cooperative",
        "note": "Public sector contract opportunities when listed publicly",
    },
    {
        "source_id": "coop_hgac_live",
        "name": "HGACBuy — Bid Opportunities",
        "list_url": "https://www.hgacbuy.org/bid-opportunities",
        "adapter_family": "live_cooperative",
        "note": "Bid opportunities ≠ awarded contracts",
    },
    {
        "source_id": "coop_buyboard_live",
        "name": "BuyBoard — Current Solicitations",
        "list_url": "https://www.buyboard.com/Vendors/CurrentSolicitations.aspx",
        "adapter_family": "live_cooperative",
        "note": "Current solicitations listing when public",
    },
    {
        "source_id": "coop_1gpa_live",
        "name": "1Government Procurement Alliance",
        "list_url": "https://www.1gpa.org/solicitations/",
        "adapter_family": "live_cooperative",
        "note": "Open solicitations when published publicly",
    },
]


def enrich_coop(row: dict[str, Any]) -> dict[str, Any]:
    from discovery.live_fetchers import get_live_fetcher
    from discovery.constants import ADAPTER_UNVERIFIED_LIVE
    from discovery.state_matrix import get_status_overrides

    out = dict(row)
    fetcher = get_live_fetcher(row.get("adapter_family") or "")
    overrides = get_status_overrides()
    if out["source_id"] in overrides:
        out["adapter_status"] = overrides[out["source_id"]]
        out["live_verified"] = overrides[out["source_id"]] == "LIVE_VERIFIED"
        out["live_capable"] = out["live_verified"]
    else:
        out["live_verified"] = False
        out["live_capable"] = False
        out["adapter_status"] = ADAPTER_UNVERIFIED_LIVE if (fetcher and row.get("list_url")) else "PLANNED"
    out["fetcher_available"] = bool(fetcher)
    out["unverified_live"] = out["adapter_status"] == ADAPTER_UNVERIFIED_LIVE
    out["source_type"] = "COOPERATIVE"
    out["validation_candidate"] = True
    return out


def all_coops_enriched() -> list[dict[str, Any]]:
    return [enrich_coop(c) for c in COOPERATIVE_LIVE_SOURCES]


FEDERAL_NON_SAM_LIVE: list[dict[str, Any]] = [
    {
        "source_id": "fed_sam_contract_opportunities",
        "name": "SAM.gov Contract Opportunities (official API v2)",
        "list_url": "https://api.sam.gov/opportunities/v2/search",
        "adapter_family": "live_sam_api",
        "platform_family": "SAM_GOV",
        "note": (
            "Authoritative Federal Contract Opportunities via Get Opportunities Public API. "
            "Ingested by federal_sam_ingest (not HTML scrape). Broad enumeration — no DLA/NAICS prefilter. "
            "Requires SAM_GOV_API_KEY; gated by SAM_API_CALL_LIMIT / Cost Governor."
        ),
        "live_capable": True,
        "notice_type": "CONTRACT_OPPORTUNITY",
        "api_source": True,
    },
    {
        "source_id": "fed_dla_dibbs_rfq",
        "name": "DLA DIBBS Public Recent RFQs",
        "list_url": "https://www.dibbs.bsm.dla.mil/RFQ/RfqRecents.aspx",
        "adapter_family": "live_dibbs",
        "platform_family": "DIBBS",
        "note": "Public RFQ listing for DLA SPE* NSN/part procurements. Quote submission may require vendor login; listing metadata is public when accessible.",
        "live_capable": True,
        "notice_type": "RFQ",
    },
    {
        "source_id": "fed_dla_dibbs_rfq_by_fsc",
        "name": "DLA DIBBS RFQs by FSC (entry)",
        "list_url": "https://www.dibbs.bsm.dla.mil/RFQ/RfqByFsc.aspx",
        "adapter_family": "live_dibbs",
        "platform_family": "DIBBS",
        "note": "Alternate DIBBS public RFQ browse entry. Bot/auth barriers recorded explicitly when hit.",
        "live_capable": True,
        "notice_type": "RFQ",
    },
    {
        "source_id": "fed_piee_public_solicitations",
        "name": "PIEE Public Solicitation Index (unauthenticated)",
        "list_url": "https://piee.eb.mil/sol/xhtml/unauth/index.xhtml",
        "adapter_family": "live_piee_public",
        "platform_family": "PIEE",
        "note": "DoD PIEE public solicitation search — no login required for many open notices.",
        "live_capable": True,
        "notice_type": "SOLICITATION",
    },
    {
        "source_id": "fed_gsa_schedules_forecast_lead",
        "name": "GSA / FAS public procurement pages (LEAD_ONLY forecast — not open solicitations)",
        "list_url": None,  # buy-through-us is not a solicitation feed; removed from discovery
        "adapter_family": "live_federal_public",
        "note": "FORECAST/LEAD_ONLY — not OPEN_SOLICITATION. Do not use buy-through-us as discovery feed. No SAM.",
        "live_capable": False,
        "lead_only": True,
        "notice_type": "FORECAST",
    },
    {
        "source_id": "fed_agency_public_rfq_pages",
        "name": "Federal agency public RFQ/bid pages (generic placeholder)",
        "list_url": None,
        "adapter_family": "live_federal_public",
        "note": "Fetcher exists; specific agency URLs onboarded via fed_dla_dibbs_* / fed_piee_*. No SAM broad scan.",
        "live_capable": False,
    },
]
