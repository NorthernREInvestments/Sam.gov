"""50-state procurement coverage matrix — UNVERIFIED until live-validated."""

from __future__ import annotations

from typing import Any

from discovery.constants import (
    ADAPTER_AUTH_REQUIRED,
    ADAPTER_BLOCKED,
    ADAPTER_BROKEN,
    ADAPTER_DEGRADED,
    ADAPTER_LIVE_VERIFIED,
    ADAPTER_PLANNED,
    ADAPTER_UNVERIFIED_LIVE,
    PLATFORM_BIDNET,
    PLATFORM_JAGGAER,
    PLATFORM_PUBLIC_PURCHASE,
    PLATFORM_SIMPLE_HTML,
    PLATFORM_STATE_OWNED,
)

# Official primary sources. adapter_status here is the *configured* starting status
# before live validation. LIVE_VERIFIED only after successful real validation.
STATE_MATRIX: list[dict[str, Any]] = [
    # --- First five TINY failures: honest classification / repaired where possible ---
    {
        "state": "AL", "name": "Alabama", "portal_name": "Alabama Buys",
        "list_url": "https://www.alabamabuys.gov/",
        "platform_family": PLATFORM_JAGGAER, "adapter_family": "live_jaggaer",
        "publicly_searchable": False, "docs_public": False, "auth_required": True,
        "adapter_status_seed": ADAPTER_AUTH_REQUIRED,
        "restrictions": "Public Solicitations link exists but portal presents login; automated public listing not realistically available without auth/JS portal",
        "validation_candidate": False,
    },
    {
        "state": "AK", "name": "Alaska", "portal_name": "Alaska IRIS VSS",
        "list_url": "https://iris-vss.alaska.gov/",
        "platform_family": PLATFORM_JAGGAER, "adapter_family": "live_jaggaer",
        "publicly_searchable": False, "docs_public": False, "auth_required": True,
        "adapter_status_seed": ADAPTER_AUTH_REQUIRED,
        "restrictions": "Vendor Self Service requires vendor login to view/respond to solicitations",
        "validation_candidate": False,
    },
    {
        "state": "AZ", "name": "Arizona", "portal_name": "Arizona Procurement Portal (APP)",
        "list_url": "https://app.az.gov/page.aspx/en/rfp/request_browse_public",
        "platform_family": PLATFORM_STATE_OWNED, "adapter_family": "live_state_owned_html",
        "publicly_searchable": False, "docs_public": False, "auth_required": True,
        "adapter_status_seed": ADAPTER_AUTH_REQUIRED,
        "restrictions": "Public browse URL presents login page (TINY 2026-09-15)",
        "validation_candidate": False,
    },
    {
        "state": "AR", "name": "Arkansas", "portal_name": "SAS OSP Bid Opportunities",
        "list_url": "https://sas.arkansas.gov/procurement/bid-opportunities/",
        "platform_family": PLATFORM_SIMPLE_HTML, "adapter_family": "live_simple_html",
        "publicly_searchable": True, "docs_public": True, "auth_required": False,
        "adapter_status_seed": ADAPTER_UNVERIFIED_LIVE,
        "restrictions": "Replaced dead arkansas.gov/dba/procurement (HTTP 410). Current OSP listing page.",
        "validation_candidate": True,
    },
    {
        "state": "CA", "name": "California", "portal_name": "Cal eProcure",
        "list_url": "https://caleprocure.ca.gov/pages/Events-BS3/event-search.aspx",
        "platform_family": PLATFORM_STATE_OWNED, "adapter_family": "live_state_owned_html",
        "publicly_searchable": False, "docs_public": False, "auth_required": True,
        "adapter_status_seed": ADAPTER_BLOCKED,
        "restrictions": "Event search returns HTTP 403 to automated clients (TINY 2026-09-15)",
        "validation_candidate": False,
    },
    # --- Easy-win / validation priority states (UNVERIFIED until proven) ---
    {"state": "CO", "name": "Colorado", "portal_name": "Colorado VSS", "list_url": "https://www.colorado.gov/vss", "platform_family": PLATFORM_JAGGAER, "adapter_family": "live_jaggaer", "publicly_searchable": True, "docs_public": True, "auth_required": False, "adapter_status_seed": ADAPTER_UNVERIFIED_LIVE, "restrictions": None, "validation_candidate": False},
    {"state": "CT", "name": "Connecticut", "portal_name": "CTSource", "list_url": "https://portal.ct.gov/DAS/CTSource/CTSource", "platform_family": PLATFORM_STATE_OWNED, "adapter_family": "live_state_owned_html", "publicly_searchable": True, "docs_public": True, "auth_required": False, "adapter_status_seed": ADAPTER_UNVERIFIED_LIVE, "restrictions": None, "validation_candidate": False},
    {"state": "DE", "name": "Delaware", "portal_name": "Delaware Marketplace", "list_url": "https://mmp.delaware.gov/Desktop", "platform_family": PLATFORM_JAGGAER, "adapter_family": "live_jaggaer", "publicly_searchable": True, "docs_public": True, "auth_required": False, "adapter_status_seed": ADAPTER_UNVERIFIED_LIVE, "restrictions": None, "validation_candidate": False},
    {"state": "FL", "name": "Florida", "portal_name": "MyFloridaMarketPlace", "list_url": "https://vendor.myfloridamarketplace.com/", "platform_family": PLATFORM_STATE_OWNED, "adapter_family": "live_state_owned_html", "publicly_searchable": True, "docs_public": True, "auth_required": False, "adapter_status_seed": ADAPTER_UNVERIFIED_LIVE, "restrictions": "Some detail may require vendor account", "validation_candidate": False},
    {"state": "GA", "name": "Georgia", "portal_name": "Georgia Procurement Registry", "list_url": "https://ssl.doas.state.ga.us/gpr/", "platform_family": PLATFORM_SIMPLE_HTML, "adapter_family": "live_simple_html", "publicly_searchable": True, "docs_public": True, "auth_required": False, "adapter_status_seed": ADAPTER_UNVERIFIED_LIVE, "restrictions": "View without login; register to bid", "validation_candidate": True},
    {"state": "HI", "name": "Hawaii", "portal_name": "HANDS", "list_url": "https://hands.ehawaii.gov/hands/", "platform_family": PLATFORM_STATE_OWNED, "adapter_family": "live_state_owned_html", "publicly_searchable": True, "docs_public": True, "auth_required": False, "adapter_status_seed": ADAPTER_UNVERIFIED_LIVE, "restrictions": None, "validation_candidate": False},
    {"state": "ID", "name": "Idaho", "portal_name": "Idaho Purchasing", "list_url": "https://purchasing.idaho.gov/", "platform_family": PLATFORM_SIMPLE_HTML, "adapter_family": "live_simple_html", "publicly_searchable": True, "docs_public": True, "auth_required": False, "adapter_status_seed": ADAPTER_UNVERIFIED_LIVE, "restrictions": "forward-bidding/ path returns 404 — use purchasing.idaho.gov root", "validation_candidate": True},
    {"state": "IL", "name": "Illinois", "portal_name": "BidBuy", "list_url": "https://www.bidbuy.illinois.gov/bso/view/search/external/advancedSearchBid.xhtml", "platform_family": PLATFORM_STATE_OWNED, "adapter_family": "live_state_owned_html", "publicly_searchable": True, "docs_public": True, "auth_required": False, "adapter_status_seed": ADAPTER_UNVERIFIED_LIVE, "restrictions": None, "validation_candidate": False},
    {"state": "IN", "name": "Indiana", "portal_name": "IDOA Procurement", "list_url": "https://www.in.gov/idoa/procurement/", "platform_family": PLATFORM_STATE_OWNED, "adapter_family": "live_state_owned_html", "publicly_searchable": True, "docs_public": True, "auth_required": False, "adapter_status_seed": ADAPTER_UNVERIFIED_LIVE, "restrictions": None, "validation_candidate": False},
    {"state": "IA", "name": "Iowa", "portal_name": "IMPACS / SciQuest Public Events", "list_url": "https://bids.sciquest.com/apps/Router/PublicEvent?CustomerOrg=DASIowa", "platform_family": PLATFORM_JAGGAER, "adapter_family": "live_jaggaer", "publicly_searchable": True, "docs_public": True, "auth_required": False, "adapter_status_seed": ADAPTER_UNVERIFIED_LIVE, "restrictions": "Public open events; PDF view without login; register to respond", "validation_candidate": True},
    {"state": "KS", "name": "Kansas", "portal_name": "Kansas Procurement", "list_url": "https://admin.ks.gov/offices/procurement-and-contracts", "platform_family": PLATFORM_STATE_OWNED, "adapter_family": "live_state_owned_html", "publicly_searchable": True, "docs_public": True, "auth_required": False, "adapter_status_seed": ADAPTER_UNVERIFIED_LIVE, "restrictions": None, "validation_candidate": False},
    {"state": "KY", "name": "Kentucky", "portal_name": "Kentucky VSS", "list_url": "https://vss.ky.gov/webapp/vssKY/AltSelfService", "platform_family": PLATFORM_JAGGAER, "adapter_family": "live_jaggaer", "publicly_searchable": True, "docs_public": True, "auth_required": False, "adapter_status_seed": ADAPTER_UNVERIFIED_LIVE, "restrictions": None, "validation_candidate": False},
    {"state": "LA", "name": "Louisiana", "portal_name": "LaPAC", "list_url": "https://wwwcfprd.doa.louisiana.gov/osp/lapac/pubMain.cfm", "platform_family": PLATFORM_SIMPLE_HTML, "adapter_family": "live_simple_html", "publicly_searchable": True, "docs_public": True, "auth_required": False, "adapter_status_seed": ADAPTER_UNVERIFIED_LIVE, "restrictions": None, "validation_candidate": True},
    {"state": "ME", "name": "Maine", "portal_name": "Maine Procurement", "list_url": "https://www.maine.gov/dafs/bbm/procurementservices/vendors/procurements", "platform_family": PLATFORM_SIMPLE_HTML, "adapter_family": "live_simple_html", "publicly_searchable": True, "docs_public": True, "auth_required": False, "adapter_status_seed": ADAPTER_UNVERIFIED_LIVE, "restrictions": None, "validation_candidate": False},
    {"state": "MD", "name": "Maryland", "portal_name": "eMMA", "list_url": "https://emma.maryland.gov/page.aspx/en/rfp/request_browse_public", "platform_family": PLATFORM_SIMPLE_HTML, "adapter_family": "live_simple_html", "publicly_searchable": True, "docs_public": True, "auth_required": False, "adapter_status_seed": ADAPTER_UNVERIFIED_LIVE, "restrictions": "May resemble AZ login pattern", "validation_candidate": False},
    {"state": "MA", "name": "Massachusetts", "portal_name": "COMMBUYS", "list_url": "https://www.commbuys.com/bso/", "platform_family": PLATFORM_STATE_OWNED, "adapter_family": "live_state_owned_html", "publicly_searchable": True, "docs_public": True, "auth_required": False, "adapter_status_seed": ADAPTER_UNVERIFIED_LIVE, "restrictions": None, "validation_candidate": False},
    {"state": "MI", "name": "Michigan", "portal_name": "SIGMA VSS", "list_url": "https://sigma.michigan.gov/webapp/PRDVSS2X1/AltSelfService", "platform_family": PLATFORM_JAGGAER, "adapter_family": "live_jaggaer", "publicly_searchable": True, "docs_public": True, "auth_required": False, "adapter_status_seed": ADAPTER_UNVERIFIED_LIVE, "restrictions": None, "validation_candidate": False},
    {"state": "MN", "name": "Minnesota", "portal_name": "MMD SWIFT", "list_url": "https://www.mmd.admin.state.mn.us/process/search/", "platform_family": PLATFORM_STATE_OWNED, "adapter_family": "live_state_owned_html", "publicly_searchable": True, "docs_public": True, "auth_required": False, "adapter_status_seed": ADAPTER_UNVERIFIED_LIVE, "restrictions": None, "validation_candidate": False},
    {"state": "MS", "name": "Mississippi", "portal_name": "MAGIC", "list_url": "https://www.ms.gov/dfa/contract_bid_search", "platform_family": PLATFORM_SIMPLE_HTML, "adapter_family": "live_simple_html", "publicly_searchable": True, "docs_public": True, "auth_required": False, "adapter_status_seed": ADAPTER_UNVERIFIED_LIVE, "restrictions": None, "validation_candidate": True},
    {"state": "MO", "name": "Missouri", "portal_name": "MissouriBUYS", "list_url": "https://missouribuys.mo.gov/", "platform_family": PLATFORM_STATE_OWNED, "adapter_family": "live_state_owned_html", "publicly_searchable": True, "docs_public": True, "auth_required": False, "adapter_status_seed": ADAPTER_UNVERIFIED_LIVE, "restrictions": None, "validation_candidate": False},
    {"state": "MT", "name": "Montana", "portal_name": "eMACS / SciQuest Public", "list_url": "https://bids.sciquest.com/apps/Router/PublicEvent?CustomerOrg=StateOfMontana", "platform_family": PLATFORM_JAGGAER, "adapter_family": "live_jaggaer", "publicly_searchable": True, "docs_public": True, "auth_required": False, "adapter_status_seed": ADAPTER_UNVERIFIED_LIVE, "restrictions": None, "validation_candidate": True},
    {"state": "NE", "name": "Nebraska", "portal_name": "Nebraska Purchasing", "list_url": "https://das.nebraska.gov/materiel/purchasing.html", "platform_family": PLATFORM_SIMPLE_HTML, "adapter_family": "live_simple_html", "publicly_searchable": True, "docs_public": True, "auth_required": False, "adapter_status_seed": ADAPTER_UNVERIFIED_LIVE, "restrictions": None, "validation_candidate": True},
    {"state": "NV", "name": "Nevada", "portal_name": "NevadaEPro", "list_url": "https://nevadaepro.com/bso/", "platform_family": PLATFORM_STATE_OWNED, "adapter_family": "live_state_owned_html", "publicly_searchable": True, "docs_public": True, "auth_required": False, "adapter_status_seed": ADAPTER_UNVERIFIED_LIVE, "restrictions": None, "validation_candidate": False},
    {"state": "NH", "name": "New Hampshire", "portal_name": "NH Bureau of Purchase", "list_url": "https://apps.das.nh.gov/bidscontracts/bids.aspx", "platform_family": PLATFORM_SIMPLE_HTML, "adapter_family": "live_simple_html", "publicly_searchable": True, "docs_public": True, "auth_required": False, "adapter_status_seed": ADAPTER_UNVERIFIED_LIVE, "restrictions": "Legacy das.nh.gov/purchasing/bidding.aspx returns 404 — use apps.das.nh.gov", "validation_candidate": True},
    {"state": "NJ", "name": "New Jersey", "portal_name": "NJSTART", "list_url": "https://www.njstart.gov/bso/", "platform_family": PLATFORM_STATE_OWNED, "adapter_family": "live_state_owned_html", "publicly_searchable": True, "docs_public": True, "auth_required": False, "adapter_status_seed": ADAPTER_UNVERIFIED_LIVE, "restrictions": None, "validation_candidate": False},
    {"state": "NM", "name": "New Mexico", "portal_name": "NM State Purchasing", "list_url": "https://www.generalservices.state.nm.us/statepurchasing/", "platform_family": PLATFORM_STATE_OWNED, "adapter_family": "live_state_owned_html", "publicly_searchable": True, "docs_public": True, "auth_required": False, "adapter_status_seed": ADAPTER_UNVERIFIED_LIVE, "restrictions": None, "validation_candidate": False},
    {"state": "NY", "name": "New York", "portal_name": "NYS Contract Reporter", "list_url": "https://www.nyscr.ny.gov/", "platform_family": PLATFORM_BIDNET, "adapter_family": "live_bidnet", "publicly_searchable": True, "docs_public": True, "auth_required": False, "adapter_status_seed": ADAPTER_UNVERIFIED_LIVE, "restrictions": "Some views may require free registration", "validation_candidate": False},
    {"state": "NC", "name": "North Carolina", "portal_name": "NC IPS", "list_url": "https://www.ips.state.nc.us/IPS/Search.aspx", "platform_family": PLATFORM_SIMPLE_HTML, "adapter_family": "live_simple_html", "publicly_searchable": True, "docs_public": True, "auth_required": False, "adapter_status_seed": ADAPTER_UNVERIFIED_LIVE, "restrictions": None, "validation_candidate": True},
    {"state": "ND", "name": "North Dakota", "portal_name": "ND State Procurement", "list_url": "https://www.nd.gov/omb/agency/state-procurement", "platform_family": PLATFORM_STATE_OWNED, "adapter_family": "live_state_owned_html", "publicly_searchable": True, "docs_public": True, "auth_required": False, "adapter_status_seed": ADAPTER_UNVERIFIED_LIVE, "restrictions": None, "validation_candidate": False},
    {"state": "OH", "name": "Ohio", "portal_name": "OhioBuys", "list_url": "https://ohiobuys.ohio.gov/", "platform_family": PLATFORM_STATE_OWNED, "adapter_family": "live_state_owned_html", "publicly_searchable": True, "docs_public": True, "auth_required": False, "adapter_status_seed": ADAPTER_UNVERIFIED_LIVE, "restrictions": None, "validation_candidate": False},
    {"state": "OK", "name": "Oklahoma", "portal_name": "OK Central Purchasing", "list_url": "https://www.ok.gov/dcs/solicit/", "platform_family": PLATFORM_SIMPLE_HTML, "adapter_family": "live_simple_html", "publicly_searchable": True, "docs_public": True, "auth_required": False, "adapter_status_seed": ADAPTER_UNVERIFIED_LIVE, "restrictions": None, "validation_candidate": True},
    {"state": "OR", "name": "Oregon", "portal_name": "OregonBuys", "list_url": "https://oregonbuys.gov/bso/", "platform_family": PLATFORM_STATE_OWNED, "adapter_family": "live_state_owned_html", "publicly_searchable": True, "docs_public": True, "auth_required": False, "adapter_status_seed": ADAPTER_UNVERIFIED_LIVE, "restrictions": None, "validation_candidate": False},
    {"state": "PA", "name": "Pennsylvania", "portal_name": "PA eMarketplace", "list_url": "https://www.emarketplace.state.pa.us/Solicitations.aspx", "platform_family": PLATFORM_SIMPLE_HTML, "adapter_family": "live_simple_html", "publicly_searchable": True, "docs_public": True, "auth_required": False, "adapter_status_seed": ADAPTER_UNVERIFIED_LIVE, "restrictions": "Homepage is marketing; open solicitations at /Solicitations.aspx", "validation_candidate": True},
    {"state": "RI", "name": "Rhode Island", "portal_name": "RI Division of Purchases", "list_url": "https://www.ridop.ri.gov/bidding/current-opportunities.php", "platform_family": PLATFORM_SIMPLE_HTML, "adapter_family": "live_simple_html", "publicly_searchable": True, "docs_public": True, "auth_required": False, "adapter_status_seed": ADAPTER_UNVERIFIED_LIVE, "restrictions": "URL updated from closed-bids page", "validation_candidate": False},
    {"state": "SC", "name": "South Carolina", "portal_name": "SC Sourcing", "list_url": "https://sourcing.sc.gov/", "platform_family": PLATFORM_STATE_OWNED, "adapter_family": "live_state_owned_html", "publicly_searchable": True, "docs_public": True, "auth_required": False, "adapter_status_seed": ADAPTER_UNVERIFIED_LIVE, "restrictions": None, "validation_candidate": False},
    {"state": "SD", "name": "South Dakota", "portal_name": "Open.SD.Gov", "list_url": "https://open.sd.gov/", "platform_family": PLATFORM_STATE_OWNED, "adapter_family": "live_state_owned_html", "publicly_searchable": True, "docs_public": True, "auth_required": False, "adapter_status_seed": ADAPTER_UNVERIFIED_LIVE, "restrictions": None, "validation_candidate": False},
    {"state": "TN", "name": "Tennessee", "portal_name": "TN Central Procurement", "list_url": "https://www.tn.gov/generalservices/procurement.html", "platform_family": PLATFORM_STATE_OWNED, "adapter_family": "live_state_owned_html", "publicly_searchable": True, "docs_public": True, "auth_required": False, "adapter_status_seed": ADAPTER_UNVERIFIED_LIVE, "restrictions": None, "validation_candidate": False},
    {"state": "TX", "name": "Texas", "portal_name": "ESBD / TxSmartBuy", "list_url": "https://www.txsmartbuy.com/esbd", "platform_family": PLATFORM_SIMPLE_HTML, "adapter_family": "live_simple_html", "publicly_searchable": True, "docs_public": True, "auth_required": False, "adapter_status_seed": ADAPTER_UNVERIFIED_LIVE, "restrictions": "Sign-in NOT required to view solicitations", "validation_candidate": True},
    {"state": "UT", "name": "Utah", "portal_name": "Utah Purchasing", "list_url": "https://purchasing.utah.gov/for-vendors/", "platform_family": PLATFORM_STATE_OWNED, "adapter_family": "live_state_owned_html", "publicly_searchable": True, "docs_public": True, "auth_required": False, "adapter_status_seed": ADAPTER_UNVERIFIED_LIVE, "restrictions": None, "validation_candidate": False},
    {"state": "VT", "name": "Vermont", "portal_name": "Vermont Business Registry", "list_url": "https://www.vermontbusinessregistry.com/", "platform_family": PLATFORM_SIMPLE_HTML, "adapter_family": "live_simple_html", "publicly_searchable": True, "docs_public": True, "auth_required": False, "adapter_status_seed": ADAPTER_UNVERIFIED_LIVE, "restrictions": None, "validation_candidate": False},
    {"state": "VA", "name": "Virginia", "portal_name": "eVA", "list_url": "https://eva.virginia.gov/", "platform_family": PLATFORM_STATE_OWNED, "adapter_family": "live_state_owned_html", "publicly_searchable": True, "docs_public": True, "auth_required": False, "adapter_status_seed": ADAPTER_UNVERIFIED_LIVE, "restrictions": "Some functionality may require vendor login", "validation_candidate": False},
    {"state": "WA", "name": "Washington", "portal_name": "WEBS", "list_url": "https://pr-webs-vendor.des.wa.gov/", "platform_family": PLATFORM_STATE_OWNED, "adapter_family": "live_state_owned_html", "publicly_searchable": True, "docs_public": True, "auth_required": False, "adapter_status_seed": ADAPTER_UNVERIFIED_LIVE, "restrictions": None, "validation_candidate": False},
    {"state": "WV", "name": "West Virginia", "portal_name": "WV Purchasing Division / wvOASIS", "list_url": "https://www.state.wv.us/admin/purchase/bids.html", "platform_family": PLATFORM_SIMPLE_HTML, "adapter_family": "live_simple_html", "publicly_searchable": False, "docs_public": False, "auth_required": True, "adapter_status_seed": ADAPTER_UNVERIFIED_LIVE, "restrictions": "Public bids.html 404; active bulletin requires wvOASIS VSS login — skip until public feed", "validation_candidate": False},
    {"state": "WI", "name": "Wisconsin", "portal_name": "VendorNet", "list_url": "https://vendornet.wi.gov/", "platform_family": PLATFORM_STATE_OWNED, "adapter_family": "live_state_owned_html", "publicly_searchable": True, "docs_public": True, "auth_required": False, "adapter_status_seed": ADAPTER_UNVERIFIED_LIVE, "restrictions": None, "validation_candidate": False},
    {"state": "WY", "name": "Wyoming", "portal_name": "Wyoming Public Purchase", "list_url": "https://www.publicpurchase.com/gems/wyoming/buyer/public/home", "platform_family": PLATFORM_PUBLIC_PURCHASE, "adapter_family": "live_public_purchase", "publicly_searchable": False, "docs_public": False, "auth_required": True, "adapter_status_seed": ADAPTER_AUTH_REQUIRED, "restrictions": "PublicPurchase /home is marketing/vendor-registration chrome; open bids not publicly listable without login (observed 2026-09-15)", "validation_candidate": False},
]


def enrich_state_row(row: dict[str, Any], *, status_overrides: dict[str, str] | None = None) -> dict[str, Any]:
    """Derive live flags. LIVE_VERIFIED only via override from real validation."""
    from discovery.live_fetchers import get_live_fetcher

    out = dict(row)
    fetcher = get_live_fetcher(row.get("adapter_family") or "")
    has_url = bool(row.get("list_url"))
    seed = row.get("adapter_status_seed") or ADAPTER_PLANNED
    sid = f"state_{row['state'].lower()}"

    if status_overrides and sid in status_overrides:
        out["adapter_status"] = status_overrides[sid]
    elif seed in {ADAPTER_AUTH_REQUIRED, ADAPTER_BLOCKED, ADAPTER_BROKEN, ADAPTER_LIVE_VERIFIED, ADAPTER_DEGRADED}:
        out["adapter_status"] = seed
    elif row.get("auth_required"):
        out["adapter_status"] = ADAPTER_AUTH_REQUIRED
    elif fetcher and has_url and seed == ADAPTER_UNVERIFIED_LIVE:
        out["adapter_status"] = ADAPTER_UNVERIFIED_LIVE
    elif fetcher and has_url:
        out["adapter_status"] = ADAPTER_UNVERIFIED_LIVE
    else:
        out["adapter_status"] = ADAPTER_PLANNED

    st = out["adapter_status"]
    out["live_verified"] = st == ADAPTER_LIVE_VERIFIED
    out["fetcher_available"] = bool(fetcher)
    out["live_capable"] = st == ADAPTER_LIVE_VERIFIED  # strict: only verified
    out["unverified_live"] = st == ADAPTER_UNVERIFIED_LIVE
    out["source_id"] = sid
    out["last_verified_architecture"] = "2026-09-15-repair"
    out["validation_candidate"] = bool(row.get("validation_candidate"))
    return out


# In-memory validation overrides applied after live validation runs in-process
_STATUS_OVERRIDES: dict[str, str] = {}


def set_status_override(source_id: str, status: str) -> None:
    _STATUS_OVERRIDES[source_id] = status


def get_status_overrides() -> dict[str, str]:
    return dict(_STATUS_OVERRIDES)


def load_status_overrides_from_db(session: Any) -> dict[str, str]:
    """Load persisted validation results into process overrides for accurate coverage."""
    from models import DiscoverySource

    out: dict[str, str] = {}
    rows = (
        session.query(DiscoverySource)
        .filter(DiscoverySource.last_live_validation_result.isnot(None))
        .all()
    )
    for r in rows:
        st = (r.last_live_validation_result or r.adapter_status or "").upper()
        if st:
            out[r.source_id] = st
            set_status_override(r.source_id, st)
    return out


def all_states_enriched() -> list[dict[str, Any]]:
    return [enrich_state_row(r, status_overrides=_STATUS_OVERRIDES) for r in STATE_MATRIX]


def state_coverage_summary() -> dict[str, Any]:
    rows = all_states_enriched()
    assert len(rows) == 50

    def cnt(status: str) -> int:
        return sum(1 for r in rows if r["adapter_status"] == status)

    return {
        "states_total": 50,
        "states_LIVE_VERIFIED": cnt(ADAPTER_LIVE_VERIFIED),
        "states_UNVERIFIED_LIVE": cnt(ADAPTER_UNVERIFIED_LIVE),
        "states_AUTH_REQUIRED": cnt(ADAPTER_AUTH_REQUIRED),
        "states_BLOCKED": cnt(ADAPTER_BLOCKED),
        "states_BROKEN": cnt(ADAPTER_BROKEN),
        "states_PLANNED": cnt(ADAPTER_PLANNED),
        # Legacy keys — never inflate LIVE_CAPABLE beyond verified
        "states_LIVE_CAPABLE": cnt(ADAPTER_LIVE_VERIFIED),
        "live_verified_states": [r["state"] for r in rows if r["live_verified"]],
        "validation_candidates": [r["state"] for r in rows if r.get("validation_candidate")],
        "note": "LIVE_VERIFIED requires real live validation. UNVERIFIED_LIVE has fetcher+URL only.",
        "LIVE_API_REQUESTS": 0,
    }
