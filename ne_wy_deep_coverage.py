"""Nebraska + Wyoming deep public-procurement coverage sweep.

Maps the practical publicly discoverable procurement universe for NE and WY
using official state seeds + authoritative education directories. Does not claim
theoretical completeness.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from application_clock import now_utc
from discovery.platform_detect import detect_platform
from ne_wy_directory_parsers import (
    PROCUREMENT_PATH_CANDIDATES,
    parse_nde_quickdisplay_markdown,
    parse_wde_directory_text,
    website_from_email,
)
from portal_document_resolver import live_http_get
from procurement_source_coverage_expansion import (
    classify_access_layers,
    guess_entity_type_expanded,
    opportunity_identity_key,
    reclassify_unknown_portal,
)
from procurement_source_registry import ProcurementSourceRegistry, bootstrap_registry
from procurement_source_seed_sweep import (
    ARTIFACTS,
    MAX_OPPORTUNITIES_STORE,
    ProcurementSourceSeedSweep,
    _norm_url,
    _seed_id,
    extract_procurement_links,
)

BUILD_TAG = "20260921-m3-ne-wy-deep-coverage-1"
NDE_PUBLIC_DISTRICTS_URL = "https://educdirsrc.education.ne.gov/QuickDisplay.aspx?code=pda&sort=name"
WDE_DIRECTORY_PDF_URL = "https://edu.wyoming.gov/downloads/wde-resources/directory.pdf"
WDE_DISTRICTS_PDF_URL = "https://edu.wyoming.gov/wp-content/uploads/2024/08/Wyoming-School-Districts.pdf"

MAX_SEED_ATTEMPTS = 280
MAX_SECONDARY = 120
MAX_DISTRICT_SITE_PROBES = 180  # home + procurement path attempts across both states


def _utc() -> str:
    return now_utc().isoformat()


def _seed(
    name: str,
    url: str,
    *,
    state: str,
    entity_type: str,
    government_level: str = "LOCAL",
    platform: str | None = None,
    role: str = "PROCUREMENT_ENTRY",
    entity_map_only: bool = False,
    notes: str | None = None,
) -> dict[str, Any]:
    return {
        "seed_name": name,
        "url": url,
        "kind": "LOCAL" if government_level == "LOCAL" else government_level,
        "government_level": government_level,
        "state": state,
        "platform_family": platform,
        "role": role,
        "entity_map_only": entity_map_only,
        "entity_type_hint": entity_type,
        "notes": notes,
        "provenance": "ne_wy_deep_coverage",
        "seed_id": _seed_id(state, url, name),
    }


# ---------------------------------------------------------------------------
# Official starting seeds + curated local/special entities
# ---------------------------------------------------------------------------

NE_OFFICIAL_SEEDS: list[dict[str, Any]] = [
    _seed("Nebraska DAS Materiel vendor information", "https://das.nebraska.gov/materiel/vendor-information.html", state="NE", entity_type="STATE", government_level="STATE", role="SUPPLIER_RESOURCES"),
    _seed("Nebraska DAS bid opportunities", "https://das.nebraska.gov/materiel/bid-opportunities.html", state="NE", entity_type="STATE", government_level="STATE", platform="SimpleHTML"),
    _seed("Nebraska DAS Materiel purchasing", "https://das.nebraska.gov/materiel/purchasing.html", state="NE", entity_type="STATE", government_level="STATE", platform="SimpleHTML"),
    _seed("Nebraska State Contracts Database", "https://statecontracts.nebraska.gov/", state="NE", entity_type="STATE", government_level="STATE", role="AWARD_HISTORY"),
    _seed("Nebraska Contract Search", "https://www.nebraska.gov/das/materiel/purchasing/contract_search/index.php", state="NE", entity_type="STATE", government_level="STATE", role="AWARD_HISTORY"),
    _seed("NDE public districts QuickDisplay (entity map)", NDE_PUBLIC_DISTRICTS_URL, state="NE", entity_type="K12_SCHOOL_DISTRICT", government_level="STATE", role="SEED_DIRECTORY", entity_map_only=True, notes="Authoritative 244 public districts — map only"),
    _seed("NDE Quick Lists main", "https://educdirsrc.education.ne.gov/QuickMain.aspx", state="NE", entity_type="K12_SCHOOL_DISTRICT", government_level="STATE", role="SEED_DIRECTORY", entity_map_only=True),
    _seed("NDE education directory page", "https://www.education.ne.gov/dataservices/education-directory/", state="NE", entity_type="K12_SCHOOL_DISTRICT", government_level="STATE", role="SEED_DIRECTORY", entity_map_only=True),
    _seed("NDE ESU/district resources", "https://www.education.ne.gov/comm/esu-district-resources/", state="NE", entity_type="K12_SCHOOL_DISTRICT", government_level="STATE", role="SEED_DIRECTORY", entity_map_only=True),
    _seed("University of Nebraska system purchasing", "https://nebraska.edu/offices-policies/business-and-finance/procurement", state="NE", entity_type="HIGHER_EDUCATION"),
    _seed("University of Nebraska-Lincoln procurement", "https://procurement.unl.edu/", state="NE", entity_type="HIGHER_EDUCATION"),
    _seed("University of Nebraska Omaha purchasing", "https://www.unomaha.edu/business-and-finance/purchasing/index.php", state="NE", entity_type="HIGHER_EDUCATION"),
    _seed("University of Nebraska Kearney purchasing", "https://www.unk.edu/offices/business_services/purchasing.php", state="NE", entity_type="HIGHER_EDUCATION"),
    _seed("Nebraska State College System", "https://www.nscs.edu/", state="NE", entity_type="HIGHER_EDUCATION", role="AGENCY_HOME"),
    _seed("Chadron State College", "https://www.csc.edu/businessoffice/", state="NE", entity_type="HIGHER_EDUCATION"),
    _seed("Wayne State College", "https://www.wsc.edu/business-services", state="NE", entity_type="HIGHER_EDUCATION"),
    _seed("Peru State College", "https://www.peru.edu/business-services", state="NE", entity_type="HIGHER_EDUCATION"),
    _seed("Metropolitan Community College purchasing", "https://www.mccneb.edu/About-MCC/Purchasing", state="NE", entity_type="HIGHER_EDUCATION"),
    _seed("Southeast Community College purchasing", "https://www.southeast.edu/purchasing/", state="NE", entity_type="HIGHER_EDUCATION"),
    _seed("Central Community College purchasing", "https://www.cccneb.edu/purchasing", state="NE", entity_type="HIGHER_EDUCATION"),
    _seed("Northeast Community College purchasing", "https://northeast.edu/about-us/purchasing", state="NE", entity_type="HIGHER_EDUCATION"),
    _seed("Western Nebraska Community College purchasing", "https://www.wncc.edu/about-wncc/purchasing", state="NE", entity_type="HIGHER_EDUCATION"),
    _seed("Mid-Plains Community College", "https://www.mpcc.edu/", state="NE", entity_type="HIGHER_EDUCATION", role="AGENCY_HOME"),
    _seed("Nebraska BidNet open bids", "https://www.bidnetdirect.com/nebraska/solicitations/open-bids", state="NE", entity_type="OTHER_PUBLIC", government_level="NETWORK", platform="BidNet", notes="Statewide public metadata network"),
]

WY_OFFICIAL_SEEDS: list[dict[str, Any]] = [
    _seed("Wyoming A&I bid opportunities", "https://ai.wyo.gov/divisions/general-services/purchasing/bid-opportunities", state="WY", entity_type="STATE", government_level="STATE", platform="PublicPurchase"),
    _seed("Wyoming A&I home", "https://ai.wyo.gov/", state="WY", entity_type="STATE", government_level="STATE", role="AGENCY_HOME"),
    _seed("Wyoming Public Purchase state buyer", "https://www.publicpurchase.com/gems/wyoming/buyer/public/home", state="WY", entity_type="STATE", government_level="STATE", platform="PublicPurchase", notes="Free registration typically required to view open bids"),
    _seed("Wyoming State Construction bid listings", "https://stateconstruction.wyo.gov/procurement/bid-listings-contractors", state="WY", entity_type="STATE", government_level="STATE", platform="SimpleHTML"),
    _seed("Wyoming School Facilities procurement docs", "https://stateconstruction.wyo.gov/school-facilities/procurement-docs-sfd", state="WY", entity_type="STATE", government_level="STATE", platform="SimpleHTML"),
    _seed("Wyoming DOT", "https://www.dot.state.wy.us/", state="WY", entity_type="STATE", government_level="STATE", role="AGENCY_HOME"),
    _seed("Wyoming DOT doing business", "https://www.dot.state.wy.us/home/business_with_wydot.html", state="WY", entity_type="STATE", government_level="STATE"),
    _seed("WDE home", "https://edu.wyoming.gov/", state="WY", entity_type="K12_SCHOOL_DISTRICT", government_level="STATE", role="SEED_DIRECTORY", entity_map_only=True),
    _seed("WDE school districts PDF", WDE_DISTRICTS_PDF_URL, state="WY", entity_type="K12_SCHOOL_DISTRICT", government_level="STATE", role="SEED_DIRECTORY", entity_map_only=True),
    _seed("WDE education directory PDF", WDE_DIRECTORY_PDF_URL, state="WY", entity_type="K12_SCHOOL_DISTRICT", government_level="STATE", role="SEED_DIRECTORY", entity_map_only=True),
    _seed("University of Wyoming procurement", "https://www.uwyo.edu/procurement/", state="WY", entity_type="HIGHER_EDUCATION"),
    _seed("Casper College purchasing", "https://www.caspercollege.edu/about/administration/business-services/", state="WY", entity_type="HIGHER_EDUCATION"),
    _seed("Laramie County Community College", "https://lccc.wy.edu/about/purchasing.aspx", state="WY", entity_type="HIGHER_EDUCATION"),
    _seed("Western Wyoming Community College", "https://www.westernwyoming.edu/", state="WY", entity_type="HIGHER_EDUCATION", role="AGENCY_HOME"),
    _seed("Northwest College", "https://www.nwc.edu/", state="WY", entity_type="HIGHER_EDUCATION", role="AGENCY_HOME"),
    _seed("Central Wyoming College", "https://www.cwc.edu/", state="WY", entity_type="HIGHER_EDUCATION", role="AGENCY_HOME"),
    _seed("Eastern Wyoming College", "https://ewc.wy.edu/", state="WY", entity_type="HIGHER_EDUCATION", role="AGENCY_HOME"),
    _seed("Northern Wyoming Community College District", "https://www.sheridan.edu/", state="WY", entity_type="HIGHER_EDUCATION", role="AGENCY_HOME"),
    _seed("Wyoming BidNet open bids", "https://www.bidnetdirect.com/wyoming/solicitations/open-bids", state="WY", entity_type="OTHER_PUBLIC", government_level="NETWORK", platform="BidNet"),
]

# Nebraska ESUs 1–19 (skip missing numbers historically)
NE_ESU_SEEDS: list[dict[str, Any]] = [
    _seed(f"Nebraska ESU {n}", url, state="NE", entity_type="K12_SCHOOL_DISTRICT", notes="Educational Service Unit — purchasing/cooperative channel")
    for n, url in [
        (1, "https://www.esu1.org/"),
        (2, "https://www.esu2.org/"),
        (3, "https://www.esu3.org/"),
        (4, "https://www.esu4.org/"),
        (5, "https://www.esu5.org/"),
        (6, "https://www.esu6.org/"),
        (7, "https://www.esu7.org/"),
        (8, "https://www.esu8.org/"),
        (9, "https://www.esu9.org/"),
        (10, "https://www.esu10.org/"),
        (11, "https://www.esu11.org/"),
        (13, "https://www.esu13.org/"),
        (15, "https://www.esu15.org/"),
        (16, "https://www.esu16.org/"),
        (17, "https://www.esu17.org/"),
        (18, "https://www.esu18.org/"),
        (19, "https://www.esu19.org/"),
    ]
]

NE_LOCAL_SPECIAL: list[dict[str, Any]] = [
    _seed("Douglas County NE", "https://www.douglascounty-ne.gov/departments/purchasing", state="NE", entity_type="COUNTY"),
    _seed("Lancaster County NE", "https://www.lancaster.ne.gov/140/Purchasing", state="NE", entity_type="COUNTY"),
    _seed("Sarpy County NE", "https://www.sarpy.gov/147/Purchasing", state="NE", entity_type="COUNTY"),
    _seed("Hall County NE", "https://www.hallcountyne.gov/purchasing", state="NE", entity_type="COUNTY"),
    _seed("Buffalo County NE", "https://www.buffalogov.org/", state="NE", entity_type="COUNTY", role="AGENCY_HOME"),
    _seed("Scotts Bluff County NE", "https://www.scottsbluffcounty.org/", state="NE", entity_type="COUNTY", role="AGENCY_HOME"),
    _seed("Madison County NE", "https://madisoncountyne.com/", state="NE", entity_type="COUNTY", role="AGENCY_HOME"),
    _seed("Dodge County NE", "https://www.dodgecounty.ne.gov/", state="NE", entity_type="COUNTY", role="AGENCY_HOME"),
    _seed("City of Omaha purchasing", "https://www.cityofomaha.org/finance/purchasing", state="NE", entity_type="CITY_MUNICIPAL"),
    _seed("City of Lincoln purchasing", "https://www.lincoln.ne.gov/City/Departments/Finance/Purchasing", state="NE", entity_type="CITY_MUNICIPAL"),
    _seed("City of Bellevue NE", "https://www.bellevue.net/", state="NE", entity_type="CITY_MUNICIPAL", role="AGENCY_HOME"),
    _seed("City of Grand Island NE", "https://www.grand-island.com/", state="NE", entity_type="CITY_MUNICIPAL", role="AGENCY_HOME"),
    _seed("City of Kearney NE", "https://www.cityofkearney.org/", state="NE", entity_type="CITY_MUNICIPAL", role="AGENCY_HOME"),
    _seed("City of Fremont NE", "https://www.fremontne.gov/", state="NE", entity_type="CITY_MUNICIPAL", role="AGENCY_HOME"),
    _seed("City of Norfolk NE", "https://www.norfolkne.gov/", state="NE", entity_type="CITY_MUNICIPAL", role="AGENCY_HOME"),
    _seed("City of North Platte NE", "https://www.ci.north-platte.ne.us/", state="NE", entity_type="CITY_MUNICIPAL", role="AGENCY_HOME"),
    _seed("City of Hastings NE", "https://www.cityofhastings.org/", state="NE", entity_type="CITY_MUNICIPAL", role="AGENCY_HOME"),
    _seed("City of Columbus NE", "https://www.columbusne.us/", state="NE", entity_type="CITY_MUNICIPAL", role="AGENCY_HOME"),
    _seed("Eppley Airfield / Omaha Airport Authority", "https://www.flyoma.com/business/", state="NE", entity_type="AIRPORT"),
    _seed("Lincoln Airport Authority", "https://www.lincolnairport.com/", state="NE", entity_type="AIRPORT", role="AGENCY_HOME"),
    _seed("Metro Transit Omaha", "https://www.ometro.com/", state="NE", entity_type="TRANSIT", role="AGENCY_HOME"),
    _seed("StarTran Lincoln", "https://www.lincoln.ne.gov/City/Departments/LTU/StarTran", state="NE", entity_type="TRANSIT"),
    _seed("Omaha Public Power District", "https://www.oppd.com/about/doing-business/", state="NE", entity_type="UTILITY"),
    _seed("Nebraska Public Power District", "https://www.nppd.com/about-us/doing-business-with-nppd", state="NE", entity_type="UTILITY"),
    _seed("Lincoln Electric System", "https://www.les.com/business", state="NE", entity_type="UTILITY"),
    _seed("Metropolitan Utilities District", "https://www.mudomaha.com/about-us/doing-business", state="NE", entity_type="UTILITY"),
    _seed("Papio-Missouri River NRD", "https://www.papionrd.org/", state="NE", entity_type="SPECIAL_DISTRICT", role="AGENCY_HOME"),
    _seed("Lower Platte South NRD", "https://www.lpsnrd.org/", state="NE", entity_type="SPECIAL_DISTRICT", role="AGENCY_HOME"),
    _seed("Central Platte NRD", "https://www.cpnrd.org/", state="NE", entity_type="SPECIAL_DISTRICT", role="AGENCY_HOME"),
    _seed("Upper Big Blue NRD", "https://www.upperbigblue.org/", state="NE", entity_type="SPECIAL_DISTRICT", role="AGENCY_HOME"),
    _seed("Lower Elkhorn NRD", "https://www.lenrd.org/", state="NE", entity_type="SPECIAL_DISTRICT", role="AGENCY_HOME"),
    _seed("Omaha Housing Authority", "https://www.ohauthority.org/", state="NE", entity_type="HOUSING_AUTHORITY", role="AGENCY_HOME"),
    _seed("Lincoln Housing Authority", "https://www.l-housing.com/", state="NE", entity_type="HOUSING_AUTHORITY", role="AGENCY_HOME"),
    _seed("Omaha Public Library", "https://omahalibrary.org/", state="NE", entity_type="LIBRARY", role="AGENCY_HOME"),
    _seed("Lincoln City Libraries", "https://lincolnlibraries.org/", state="NE", entity_type="LIBRARY", role="AGENCY_HOME"),
    _seed("NASB / Nebraska school board cooperative info", "https://www.nasbonline.org/", state="NE", entity_type="COOPERATIVE", role="SEED_DIRECTORY", entity_map_only=True),
]

WY_LOCAL_SPECIAL: list[dict[str, Any]] = [
    _seed("Laramie County WY", "https://www.laramiecounty.com/", state="WY", entity_type="COUNTY", role="AGENCY_HOME"),
    _seed("Natrona County WY", "https://www.natronacounty-wy.gov/", state="WY", entity_type="COUNTY", role="AGENCY_HOME"),
    _seed("Campbell County WY", "https://www.campbellcountywy.gov/", state="WY", entity_type="COUNTY", role="AGENCY_HOME"),
    _seed("Sweetwater County WY", "https://www.sweetwatercountywy.gov/", state="WY", entity_type="COUNTY", role="AGENCY_HOME"),
    _seed("Albany County WY", "https://www.co.albany.wy.us/", state="WY", entity_type="COUNTY", role="AGENCY_HOME"),
    _seed("Teton County WY", "https://www.tetoncountywy.gov/", state="WY", entity_type="COUNTY", role="AGENCY_HOME"),
    _seed("Park County WY", "https://www.parkcounty.us/", state="WY", entity_type="COUNTY", role="AGENCY_HOME"),
    _seed("Fremont County WY", "https://www.fremontcountywy.org/", state="WY", entity_type="COUNTY", role="AGENCY_HOME"),
    _seed("City of Cheyenne purchasing", "https://www.cheyennecity.org/Your-Government/Departments/Finance/Purchasing", state="WY", entity_type="CITY_MUNICIPAL"),
    _seed("City of Cheyenne Public Purchase", "https://www.publicpurchase.com/gems/cheyenne/buyer/public/home", state="WY", entity_type="CITY_MUNICIPAL", platform="PublicPurchase"),
    _seed("City of Casper", "https://www.casperwy.gov/", state="WY", entity_type="CITY_MUNICIPAL", role="AGENCY_HOME"),
    _seed("City of Laramie", "https://www.cityoflaramie.org/", state="WY", entity_type="CITY_MUNICIPAL", role="AGENCY_HOME"),
    _seed("City of Gillette", "https://www.gillettewy.gov/", state="WY", entity_type="CITY_MUNICIPAL", role="AGENCY_HOME"),
    _seed("City of Rock Springs", "https://www.rswy.net/", state="WY", entity_type="CITY_MUNICIPAL", role="AGENCY_HOME"),
    _seed("City of Sheridan", "https://www.sheridanwy.net/", state="WY", entity_type="CITY_MUNICIPAL", role="AGENCY_HOME"),
    _seed("Town of Jackson WY", "https://www.jacksonwy.gov/", state="WY", entity_type="CITY_MUNICIPAL", role="AGENCY_HOME"),
    _seed("City of Cody", "https://www.codywy.gov/", state="WY", entity_type="CITY_MUNICIPAL", role="AGENCY_HOME"),
    _seed("City of Evanston", "https://www.evanstonwy.org/", state="WY", entity_type="CITY_MUNICIPAL", role="AGENCY_HOME"),
    _seed("Cheyenne Regional Airport", "https://www.cheyenneairport.com/", state="WY", entity_type="AIRPORT", role="AGENCY_HOME"),
    _seed("Casper/Natrona County International Airport", "https://www.iflycasper.com/", state="WY", entity_type="AIRPORT", role="AGENCY_HOME"),
    _seed("Jackson Hole Airport", "https://www.jacksonholeairport.com/", state="WY", entity_type="AIRPORT", role="AGENCY_HOME"),
    _seed("Cheyenne Transit", "https://www.cheyennecity.org/Your-Government/Departments/Transit", state="WY", entity_type="TRANSIT"),
    _seed("Cheyenne Light Fuel & Power / Black Hills Energy area", "https://www.blackhillsenergy.com/", state="WY", entity_type="UTILITY", role="AGENCY_HOME"),
    _seed("Cheyenne Board of Public Utilities", "https://www.cheyennebopu.org/", state="WY", entity_type="UTILITY", role="AGENCY_HOME"),
    _seed("Casper Public Utilities", "https://www.casperwy.gov/cms/one.aspx?pageId=87604", state="WY", entity_type="UTILITY"),
    _seed("Cheyenne Housing Authority", "https://www.cheyennehousing.org/", state="WY", entity_type="HOUSING_AUTHORITY", role="AGENCY_HOME"),
    _seed("Casper Housing Authority", "https://www.casperha.com/", state="WY", entity_type="HOUSING_AUTHORITY", role="AGENCY_HOME"),
    _seed("Laramie County Library System", "https://lclsonline.org/", state="WY", entity_type="LIBRARY", role="AGENCY_HOME"),
    _seed("Natrona County Library", "https://www.natronacountylibrary.org/", state="WY", entity_type="LIBRARY", role="AGENCY_HOME"),
    _seed("Wyoming Association of Municipalities", "https://wyomuni.org/", state="WY", entity_type="COOPERATIVE", role="SEED_DIRECTORY", entity_map_only=True),
]


class NeWyDeepCoverageSweep(ProcurementSourceSeedSweep):
    """Deep coverage for Nebraska and Wyoming public procurement entry points."""

    def __init__(
        self,
        *,
        registry: ProcurementSourceRegistry | None = None,
        artifacts_dir: Path | None = None,
        max_seed_attempts: int = MAX_SEED_ATTEMPTS,
        max_secondary_visits: int = MAX_SECONDARY,
        max_district_probes: int = MAX_DISTRICT_SITE_PROBES,
    ) -> None:
        super().__init__(
            registry=registry or bootstrap_registry(),
            artifacts_dir=artifacts_dir or ARTIFACTS,
            max_seed_attempts=max_seed_attempts,
            max_secondary_visits=max_secondary_visits,
        )
        self.max_district_probes = max_district_probes
        self.directory_entities: list[dict[str, Any]] = []
        self.gaps: list[dict[str, Any]] = []
        self._opp_keys: set[str] = set()
        self._district_probes = 0
        self.state_stats: dict[str, dict[str, Any]] = {"NE": {}, "WY": {}}

    def _register_entity(self, ent: dict[str, Any]) -> str:
        if ent.get("entity_type") in (None, "", "OTHER_PUBLIC"):
            ent = dict(ent)
            ent["entity_type"] = guess_entity_type_expanded(ent.get("name"))
        # ESU naming
        name = (ent.get("name") or "").lower()
        if re.search(r"\besu\s*\d+\b|educational service unit", name):
            ent["entity_type"] = "K12_SCHOOL_DISTRICT"
            ent["subtype"] = "ESU"
        return super()._register_entity(ent)

    def visit(self, seed: dict[str, Any], *, is_secondary: bool = False) -> dict[str, Any]:
        result = super().visit(seed, is_secondary=is_secondary)
        hint = seed.get("entity_type_hint")
        if hint and seed.get("seed_name"):
            for ent in self.entities.values():
                if ent.get("name") == seed.get("seed_name") and ent.get("state") == seed.get("state"):
                    ent["entity_type"] = hint
                    if seed.get("notes") and "Educational Service Unit" in str(seed.get("notes")):
                        ent["subtype"] = "ESU"
        url = _norm_url(seed.get("url")).lower()
        if url in self.portals:
            p = self.portals[url]
            p.update(
                classify_access_layers(
                    str(p.get("source_health") or ""),
                    p.get("notes"),
                    p.get("access_level"),
                )
            )
            p["state"] = p.get("state") or seed.get("state")
        # opp dedupe
        uniq: dict[str, dict[str, Any]] = {}
        for o in self.opportunities:
            k = opportunity_identity_key(o)
            self._opp_keys.add(k)
            uniq[k] = o
        self.opportunities = list(uniq.values())[:MAX_OPPORTUNITIES_STORE]
        return result

    def load_directory_entities(self) -> None:
        """Fetch authoritative education directories and register all districts."""
        # Nebraska NDE — live fetch, then cached markdown/JSON fallback
        ne_hit = live_http_get(NDE_PUBLIC_DISTRICTS_URL, source_id="nde_pda_deep")
        ne_text = ne_hit.get("text") or ""
        ne_districts = parse_nde_quickdisplay_markdown(ne_text)
        cache_json = self.artifacts_dir / "_ne_districts_from_nde.json"
        cache_md_candidates = [
            Path.home()
            / ".cursor"
            / "projects"
            / "c-Users-M-gra-OneDrive-Desktop-Sam-Gov-App"
            / "agent-tools"
            / "bb7a55e4-1656-4100-ada6-46e79a25dc6f.txt",
            self.artifacts_dir / "_ne_nde_quickdisplay.md",
        ]
        if len(ne_districts) < 200:
            for p in cache_md_candidates:
                if p.exists():
                    ne_districts = parse_nde_quickdisplay_markdown(
                        p.read_text(encoding="utf-8", errors="ignore")
                    )
                    if len(ne_districts) >= 200:
                        break
        if len(ne_districts) < 200 and cache_json.exists():
            try:
                ne_districts = json.loads(cache_json.read_text(encoding="utf-8"))
            except Exception:
                pass
        if ne_districts:
            cache_json.write_text(json.dumps(ne_districts, indent=2, default=str), encoding="utf-8")

        for d in ne_districts:
            if not d.get("website_guess") and d.get("email"):
                d["website_guess"] = website_from_email(d.get("email"))
            eid = self._register_entity(
                {
                    "name": d["name"],
                    "entity_type": "K12_SCHOOL_DISTRICT",
                    "state": "NE",
                    "government_level": "LOCAL",
                    "agency_id": d.get("agency_id"),
                    "esu": d.get("esu"),
                    "county": d.get("county"),
                    "website": d.get("website_guess"),
                    "provenance": d.get("provenance") or "nde_directory",
                    "procurement_portals": [],
                }
            )
            self.directory_entities.append({**d, "entity_id": eid})
            if not d.get("website_guess"):
                self.gaps.append(
                    {
                        "state": "NE",
                        "entity_id": eid,
                        "name": d["name"],
                        "gap": "NO_OFFICIAL_WEBSITE_RESOLVED",
                        "detail": "NDE directory row lacked resolvable public website",
                    }
                )

        # Wyoming WDE directory PDF (often 403 to bots) — parse text cache / fallback list
        wy_hit = live_http_get(WDE_DIRECTORY_PDF_URL, source_id="wde_dir_pdf")
        wy_text = wy_hit.get("text") or ""
        wy_districts = parse_wde_directory_text(wy_text)
        wy_cache = (
            Path.home()
            / ".cursor"
            / "projects"
            / "c-Users-M-gra-OneDrive-Desktop-Sam-Gov-App"
            / "agent-tools"
            / "b591bb56-e85d-4326-8f94-fd2b4c1efa0f.txt"
        )
        if len(wy_districts) < 40 and wy_cache.exists():
            wy_districts = parse_wde_directory_text(
                wy_cache.read_text(encoding="utf-8", errors="ignore")
            )
        if len(wy_districts) < 40:
            wy2 = live_http_get(WDE_DISTRICTS_PDF_URL, source_id="wde_districts_pdf")
            wy_districts.extend(parse_wde_directory_text(wy2.get("text") or ""))
        if len(wy_districts) < 40:
            for name, url in _WY_DISTRICT_FALLBACK:
                wy_districts.append(
                    {
                        "name": name,
                        "state": "WY",
                        "entity_type": "K12_SCHOOL_DISTRICT",
                        "website": url,
                        "website_guess": url,
                        "provenance": "wde_district_fallback_list",
                    }
                )
        seen_wy: set[str] = set()
        for d in wy_districts:
            key = d["name"].lower()
            if key in seen_wy:
                continue
            seen_wy.add(key)
            eid = self._register_entity(
                {
                    "name": d["name"],
                    "entity_type": "K12_SCHOOL_DISTRICT",
                    "state": "WY",
                    "government_level": "LOCAL",
                    "agency_id": d.get("agency_id"),
                    "website": d.get("website") or d.get("website_guess"),
                    "provenance": d.get("provenance") or "wde_directory",
                    "procurement_portals": [],
                }
            )
            self.directory_entities.append({**d, "entity_id": eid})
        # Persist WY list
        (self.artifacts_dir / "_wy_districts_from_wde.json").write_text(
            json.dumps(wy_districts, indent=2, default=str), encoding="utf-8"
        )

    def probe_district_procurement(self) -> None:
        """For directory districts with websites, find purchasing/bid portals."""
        candidates = [
            d
            for d in self.directory_entities
            if d.get("website_guess") or d.get("website")
        ]
        # Prefer larger known districts first by name heuristics, then rest
        def _rank(d: dict[str, Any]) -> int:
            n = (d.get("name") or "").lower()
            score = 0
            for token in (
                "omaha", "lincoln", "bellevue", "papillion", "elkhorn", "grand island",
                "cheyenne", "casper", "laramie", "gillette", "rock springs", "sheridan",
                "natrona", "campbell", "albany",
            ):
                if token in n:
                    score += 5
            return -score

        candidates.sort(key=_rank)
        for d in candidates:
            if self._district_probes >= self.max_district_probes:
                self.gaps.append(
                    {
                        "state": d.get("state"),
                        "name": d.get("name"),
                        "gap": "DISTRICT_PROBE_BUDGET_EXHAUSTED",
                        "detail": "Bounded district website probes reached limit",
                    }
                )
                break
            base = _norm_url(d.get("website") or d.get("website_guess"))
            if not base:
                continue
            # Visit home
            home_seed = {
                "seed_id": _seed_id("district", base, d["name"]),
                "seed_name": d["name"],
                "url": base,
                "kind": "LOCAL",
                "government_level": "LOCAL",
                "state": d.get("state"),
                "platform_family": None,
                "role": "AGENCY_HOME",
                "entity_type_hint": "K12_SCHOOL_DISTRICT",
                "provenance": "district_website_probe",
            }
            self._visited.discard(base.lower())
            self.visit(home_seed, is_secondary=False)
            self._district_probes += 1

            # Follow procurement links from home when present
            portal = self.portals.get(base.lower()) or {}
            # try path candidates when home didn't yield procurement portal
            found_proc = False
            home_html_visit = next(
                (v for v in reversed(self.visits) if v.get("url") == base),
                None,
            )
            # Attempt a few procurement path URLs
            for path in PROCUREMENT_PATH_CANDIDATES[:4]:
                if self._district_probes >= self.max_district_probes:
                    break
                proc_url = _norm_url(urljoin(base + "/", path.lstrip("/")))
                if proc_url.lower() in self._visited:
                    continue
                # Only hit if likely (save budget): always try for ranked large districts
                seed = {
                    "seed_id": _seed_id("district_proc", proc_url, d["name"]),
                    "seed_name": f"{d['name']} {path.strip('/')}",
                    "url": proc_url,
                    "kind": "LOCAL",
                    "government_level": "LOCAL",
                    "state": d.get("state"),
                    "role": "PROCUREMENT_ENTRY",
                    "entity_type_hint": "K12_SCHOOL_DISTRICT",
                    "provenance": "district_procurement_path_probe",
                    "parent_source": home_seed["seed_id"],
                }
                visit = self.visit(seed, is_secondary=True)
                self._district_probes += 1
                if visit.get("health") in {"HEALTHY", "DEGRADED"} and visit.get("status_code") in {200, 301, 302}:
                    found_proc = True
                    break
            if not found_proc and not any(
                base in (u or "")
                for u in (self.entities.get(d.get("entity_id") or "", {}).get("procurement_portals") or [])
            ):
                # If home itself had procurement signals, count it
                if (portal.get("opportunities_parsed") or 0) > 0:
                    found_proc = True
                else:
                    self.gaps.append(
                        {
                            "state": d.get("state"),
                            "entity_id": d.get("entity_id"),
                            "name": d.get("name"),
                            "website": base,
                            "gap": "NO_PROCUREMENT_PORTAL_CONFIRMED",
                            "detail": "District website reached; no clear public bid listing confirmed within probe budget",
                        }
                    )

    def build_state_reports(self) -> dict[str, dict[str, Any]]:
        reports: dict[str, dict[str, Any]] = {}
        for st in ("NE", "WY"):
            portals = [
                p
                for p in self.portals.values()
                if p.get("state") == st and not p.get("entity_map_only")
            ]
            ents = [e for e in self.entities.values() if e.get("state") == st]
            visits = [v for v in self.visits if any(
                p.get("url") == v.get("url") and p.get("state") == st for p in self.portals.values()
            ) or (v.get("url") or "").lower().find(
                ".ne.us" if st == "NE" else ".wy.us"
            ) >= 0 or any(
                s.get("state") == st and _norm_url(s.get("url")) == v.get("url")
                for s in self.seeds
            )]
            # Simpler: visits tied to seeds with state
            health = Counter(p.get("source_health") for p in portals)
            et = Counter(e.get("entity_type") for e in ents)
            opps = [
                o
                for o in self.opportunities
                if any(
                    (o.get("discovered_from_url") or "").lower() == (p.get("url") or "").lower()
                    for p in portals
                )
            ]
            gaps = [g for g in self.gaps if g.get("state") == st]
            k12 = [e for e in ents if e.get("entity_type") == "K12_SCHOOL_DISTRICT"]
            esu = [e for e in k12 if e.get("subtype") == "ESU" or "esu" in (e.get("name") or "").lower()]

            source_cov = {
                "kind": f"{st}SourceCoverage",
                "build": BUILD_TAG,
                "generated_at": _utc(),
                "state": st,
                "total_procurement_portals": len(portals),
                "total_sources_tested": len([v for v in self.visits if self._visit_state(v) == st]),
                "healthy": health.get("HEALTHY", 0),
                "degraded": health.get("DEGRADED", 0),
                "auth_required": health.get("AUTH_REQUIRED", 0),
                "blocked": health.get("BLOCKED", 0),
                "unavailable": health.get("UNAVAILABLE", 0),
                "unknown_health": health.get("UNKNOWN", 0),
                "current_opportunities_found": len(opps),
                "access_breakdown": dict(Counter(p.get("access_level") for p in portals)),
                "platform_families": dict(Counter(p.get("platform_family") or "UNKNOWN" for p in portals)),
                "portals": sorted(portals, key=lambda p: str(p.get("source_id") or "")),
                "honesty": {
                    "not_claiming_complete_census": True,
                    "directory_entities_may_lack_resolved_portals": True,
                },
            }
            entity_cov = {
                "kind": f"{st}EntityCoverage",
                "build": BUILD_TAG,
                "generated_at": _utc(),
                "state": st,
                "total_public_entities_discovered": len(ents),
                "school_districts": len(k12) - len(esu),
                "esu_or_education_service": len(esu),
                "counties": et.get("COUNTY", 0),
                "cities": et.get("CITY_MUNICIPAL", 0),
                "special_districts": et.get("SPECIAL_DISTRICT", 0),
                "higher_ed": et.get("HIGHER_EDUCATION", 0),
                "airports": et.get("AIRPORT", 0),
                "transit": et.get("TRANSIT", 0),
                "utilities": et.get("UTILITY", 0),
                "housing": et.get("HOUSING_AUTHORITY", 0),
                "libraries": et.get("LIBRARY", 0),
                "cooperatives": et.get("COOPERATIVE", 0),
                "state_entities": et.get("STATE", 0),
                "other_public": et.get("OTHER_PUBLIC", 0) + et.get("PORT_AUTHORITY", 0),
                "entities_by_type": dict(et),
                "entities": sorted(ents, key=lambda e: str(e.get("entity_id") or "")),
            }
            gap_report = {
                "kind": f"{st}ProcurementGaps",
                "build": BUILD_TAG,
                "generated_at": _utc(),
                "state": st,
                "gap_count": len(gaps),
                "gaps": gaps,
                "summary": dict(Counter(g.get("gap") for g in gaps)),
                "known_structural_gaps": [
                    "Many small districts publish bids only via shared ESU/state/PublicPurchase/BidNet channels",
                    "Wyoming state A&I uses Public Purchase (often FREE_REGISTRATION for bid viewing)",
                    "NDE directory is authoritative for district identity; procurement URLs require per-district resolution",
                ],
            }
            reports[st] = {
                "source_coverage": source_cov,
                "entity_coverage": entity_cov,
                "gaps": gap_report,
            }
            self.state_stats[st] = {
                "entities": len(ents),
                "portals": len(portals),
                "k12": entity_cov["school_districts"],
                "esu": entity_cov["esu_or_education_service"],
                "opportunities": len(opps),
                "gaps": len(gaps),
            }
        return reports

    def _visit_state(self, visit: dict[str, Any]) -> str | None:
        url = (visit.get("url") or "").lower()
        for p in self.portals.values():
            if (p.get("url") or "").lower() == url:
                return p.get("state")
        if ".ne.gov" in url or "nebraska" in url or ".k12.ne.us" in url:
            return "NE"
        if ".wyo.gov" in url or "wyoming" in url or ".k12.wy.us" in url or ".wy.us" in url:
            return "WY"
        return None

    def run(self) -> dict[str, Any]:
        self.load_directory_entities()

        seeds: list[dict[str, Any]] = []
        seen: set[str] = set()
        for group in (
            NE_OFFICIAL_SEEDS,
            WY_OFFICIAL_SEEDS,
            NE_ESU_SEEDS,
            NE_LOCAL_SPECIAL,
            WY_LOCAL_SPECIAL,
        ):
            for s in group:
                key = _norm_url(s["url"]).lower()
                if key in seen:
                    continue
                seen.add(key)
                seeds.append(s)
        self.seeds = seeds

        attempted = 0
        for seed in seeds:
            if attempted >= self.max_seed_attempts:
                break
            self._visited.discard(_norm_url(seed["url"]).lower())
            self.visit(seed, is_secondary=False)
            attempted += 1

        self.probe_district_procurement()

        reports = self.build_reports(seeds_loaded=len(seeds), seeds_attempted=attempted)
        state_reports = self.build_state_reports()
        reports["coverage"]["build"] = BUILD_TAG
        reports["coverage"]["sweep"] = "ne_wy_deep"
        reports["coverage"]["nebraska"] = {
            k: v for k, v in state_reports["NE"]["entity_coverage"].items() if k != "entities"
        }
        reports["coverage"]["wyoming"] = {
            k: v for k, v in state_reports["WY"]["entity_coverage"].items() if k != "entities"
        }
        reports["coverage"]["nebraska_source_summary"] = {
            k: v for k, v in state_reports["NE"]["source_coverage"].items() if k != "portals"
        }
        reports["coverage"]["wyoming_source_summary"] = {
            k: v for k, v in state_reports["WY"]["source_coverage"].items() if k != "portals"
        }
        reports["run"]["build"] = BUILD_TAG
        reports["run"]["directory_entities_loaded"] = len(self.directory_entities)
        reports["run"]["district_probes"] = self._district_probes
        reports["run"]["state_stats"] = self.state_stats

        # Write NE/WY dedicated artifacts; merge into existing national inventories
        paths = self.write_state_artifacts(state_reports)
        paths.update(self.merge_into_national_artifacts(reports, state_reports))
        try:
            self.registry.save()
        except Exception:
            pass
        return {
            "reports": reports,
            "state_reports": state_reports,
            "artifact_paths": paths,
            "build": BUILD_TAG,
            "state_stats": self.state_stats,
        }

    def merge_into_national_artifacts(
        self, reports: dict[str, Any], state_reports: dict[str, dict[str, Any]]
    ) -> dict[str, str]:
        """Extend existing Sweep #2 inventories with NE/WY finds — do not wipe national coverage."""
        paths: dict[str, str] = {}
        inv_path = self.artifacts_dir / "procurement_source_inventory.json"
        ent_path = self.artifacts_dir / "procurement_entity_inventory.json"
        rel_path = self.artifacts_dir / "source_entity_relationships.json"
        cov_path = self.artifacts_dir / "source_coverage_report.json"
        run_path = self.artifacts_dir / "source_discovery_run.json"

        # Portals merge
        existing_portals: dict[str, dict[str, Any]] = {}
        if inv_path.exists():
            try:
                inv = json.loads(inv_path.read_text(encoding="utf-8"))
                for p in inv.get("portals") or []:
                    key = _norm_url(p.get("url") or p.get("portal_url")).lower()
                    if key:
                        existing_portals[key] = p
            except Exception:
                pass
        for key, p in self.portals.items():
            if p.get("entity_map_only"):
                continue
            prev = existing_portals.get(key)
            if prev:
                for k, v in p.items():
                    if v is not None and prev.get(k) in (None, "", "UNKNOWN"):
                        prev[k] = v
            else:
                existing_portals[key] = p
        inv_out = {
            "kind": "ProcurementSourceInventory",
            "build": BUILD_TAG,
            "generated_at": _utc(),
            "portal_count": len(existing_portals),
            "portals": sorted(existing_portals.values(), key=lambda p: str(p.get("source_id") or "")),
            "ne_wy_deep_merge": True,
        }
        inv_path.write_text(json.dumps(inv_out, indent=2, default=str), encoding="utf-8")
        paths[inv_path.name] = str(inv_path)

        # Entities merge
        existing_ents: dict[str, dict[str, Any]] = {}
        if ent_path.exists():
            try:
                ent = json.loads(ent_path.read_text(encoding="utf-8"))
                for e in ent.get("entities") or []:
                    if e.get("entity_id"):
                        existing_ents[e["entity_id"]] = e
            except Exception:
                pass
        for eid, e in self.entities.items():
            if eid in existing_ents:
                prev = existing_ents[eid]
                for k, v in e.items():
                    if v is not None and prev.get(k) in (None, "", "UNKNOWN", "OTHER_PUBLIC"):
                        prev[k] = v
                portals = list(prev.get("procurement_portals") or [])
                for u in e.get("procurement_portals") or []:
                    if u and u not in portals:
                        portals.append(u)
                prev["procurement_portals"] = portals
            else:
                existing_ents[eid] = e
        ent_out = {
            "kind": "ProcurementEntityInventory",
            "build": BUILD_TAG,
            "generated_at": _utc(),
            "entity_count": len(existing_ents),
            "entities": sorted(existing_ents.values(), key=lambda e: str(e.get("entity_id") or "")),
            "ne_wy_deep_merge": True,
        }
        ent_path.write_text(json.dumps(ent_out, indent=2, default=str), encoding="utf-8")
        paths[ent_path.name] = str(ent_path)

        # Relationships append (dedupe by entity+source+url)
        rels = []
        seen_rel: set[str] = set()
        if rel_path.exists():
            try:
                prev_rel = json.loads(rel_path.read_text(encoding="utf-8"))
                for r in prev_rel.get("relationships") or []:
                    key = f"{r.get('entity_id')}|{r.get('source_id')}|{r.get('portal_url')}"
                    if key not in seen_rel:
                        seen_rel.add(key)
                        rels.append(r)
            except Exception:
                pass
        for r in self.relationships:
            key = f"{r.get('entity_id')}|{r.get('source_id')}|{r.get('portal_url')}"
            if key not in seen_rel:
                seen_rel.add(key)
                rels.append(r)
        rel_out = {
            "kind": "SourceEntityRelationships",
            "build": BUILD_TAG,
            "generated_at": _utc(),
            "relationship_count": len(rels),
            "relationships": rels,
        }
        rel_path.write_text(json.dumps(rel_out, indent=2, default=str), encoding="utf-8")
        paths[rel_path.name] = str(rel_path)

        # Coverage report — preserve national totals, attach NE/WY sections
        cov = {}
        if cov_path.exists():
            try:
                cov = json.loads(cov_path.read_text(encoding="utf-8"))
            except Exception:
                cov = {}
        cov["nebraska"] = reports["coverage"].get("nebraska")
        cov["wyoming"] = reports["coverage"].get("wyoming")
        cov["nebraska_source_summary"] = reports["coverage"].get("nebraska_source_summary")
        cov["wyoming_source_summary"] = reports["coverage"].get("wyoming_source_summary")
        cov["ne_wy_deep_build"] = BUILD_TAG
        cov["ne_wy_deep_generated_at"] = _utc()
        # Refresh top-level entity/portal counts from merged inventories
        cov["unique_procurement_portals"] = len(existing_portals)
        cov["unique_government_entities"] = len(existing_ents)
        et = Counter(e.get("entity_type") for e in existing_ents.values())
        cov["entities_by_type"] = {
            "k12": et.get("K12_SCHOOL_DISTRICT", 0),
            "higher_education": et.get("HIGHER_EDUCATION", 0),
            "county": et.get("COUNTY", 0),
            "city": et.get("CITY_MUNICIPAL", 0),
            "special_district": et.get("SPECIAL_DISTRICT", 0),
            "airport": et.get("AIRPORT", 0),
            "transit": et.get("TRANSIT", 0),
            "utility": et.get("UTILITY", 0),
            "housing": et.get("HOUSING_AUTHORITY", 0),
            "library": et.get("LIBRARY", 0),
            "cooperative": et.get("COOPERATIVE", 0),
            "federal": et.get("FEDERAL", 0),
            "state": et.get("STATE", 0),
            "other": et.get("OTHER_PUBLIC", 0),
        }
        cov_path.write_text(json.dumps(cov, indent=2, default=str), encoding="utf-8")
        paths[cov_path.name] = str(cov_path)

        # Append discovery run note
        run_out = {
            "kind": "NeWyDeepDiscoveryRun",
            "build": BUILD_TAG,
            "generated_at": _utc(),
            "directory_entities_loaded": len(self.directory_entities),
            "district_probes": self._district_probes,
            "seeds_attempted": reports["run"].get("seeds_attempted"),
            "visits": self.visits,
            "state_stats": self.state_stats,
            "opportunity_count": len(self.opportunities),
            "opportunities_sample": self.opportunities[:100],
        }
        deep_run = self.artifacts_dir / "ne_wy_source_discovery_run.json"
        deep_run.write_text(json.dumps(run_out, indent=2, default=str), encoding="utf-8")
        paths[deep_run.name] = str(deep_run)
        return paths

    def write_state_artifacts(self, state_reports: dict[str, dict[str, Any]]) -> dict[str, str]:
        mapping = {
            "nebraska_source_coverage.json": state_reports["NE"]["source_coverage"],
            "nebraska_entity_coverage.json": state_reports["NE"]["entity_coverage"],
            "nebraska_procurement_gaps.json": state_reports["NE"]["gaps"],
            "wyoming_source_coverage.json": state_reports["WY"]["source_coverage"],
            "wyoming_entity_coverage.json": state_reports["WY"]["entity_coverage"],
            "wyoming_procurement_gaps.json": state_reports["WY"]["gaps"],
        }
        paths: dict[str, str] = {}
        for name, payload in mapping.items():
            path = self.artifacts_dir / name
            path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
            paths[name] = str(path)

        # Human summary
        ne_e = state_reports["NE"]["entity_coverage"]
        ne_s = state_reports["NE"]["source_coverage"]
        wy_e = state_reports["WY"]["entity_coverage"]
        wy_s = state_reports["WY"]["source_coverage"]
        md = [
            "# Nebraska + Wyoming Deep Procurement Coverage",
            "",
            f"Build: `{BUILD_TAG}`",
            f"Generated: {_utc()}",
            "",
            "## Nebraska",
            "",
            f"- Entities: {ne_e['total_public_entities_discovered']}",
            f"- Portals: {ne_s['total_procurement_portals']}",
            f"- Sources tested: {ne_s['total_sources_tested']}",
            f"- Healthy/Degraded/Auth/Blocked: {ne_s['healthy']}/{ne_s['degraded']}/{ne_s['auth_required']}/{ne_s['blocked']}",
            f"- Opportunities: {ne_s['current_opportunities_found']}",
            f"- School districts: {ne_e['school_districts']} · ESU: {ne_e['esu_or_education_service']}",
            f"- Counties/Cities/Special: {ne_e['counties']}/{ne_e['cities']}/{ne_e['special_districts']}",
            f"- Higher-ed/Airports/Transit/Utilities/Housing/Libraries: "
            f"{ne_e['higher_ed']}/{ne_e['airports']}/{ne_e['transit']}/{ne_e['utilities']}/{ne_e['housing']}/{ne_e['libraries']}",
            f"- Gaps recorded: {state_reports['NE']['gaps']['gap_count']}",
            "",
            "## Wyoming",
            "",
            f"- Entities: {wy_e['total_public_entities_discovered']}",
            f"- Portals: {wy_s['total_procurement_portals']}",
            f"- Sources tested: {wy_s['total_sources_tested']}",
            f"- Healthy/Degraded/Auth/Blocked: {wy_s['healthy']}/{wy_s['degraded']}/{wy_s['auth_required']}/{wy_s['blocked']}",
            f"- Opportunities: {wy_s['current_opportunities_found']}",
            f"- School districts: {wy_e['school_districts']} · ESU/BOCES-like: {wy_e['esu_or_education_service']}",
            f"- Counties/Cities/Special: {wy_e['counties']}/{wy_e['cities']}/{wy_e['special_districts']}",
            f"- Higher-ed/Airports/Transit/Utilities/Housing/Libraries: "
            f"{wy_e['higher_ed']}/{wy_e['airports']}/{wy_e['transit']}/{wy_e['utilities']}/{wy_e['housing']}/{wy_e['libraries']}",
            f"- Gaps recorded: {state_reports['WY']['gaps']['gap_count']}",
            "",
            "## Honesty",
            "",
            "- Maximum practical discovery — not a theoretical completeness claim.",
            "- Directory-listed districts without resolved bid portals are recorded as gaps.",
            "",
        ]
        human = self.artifacts_dir / "ne_wy_deep_coverage_report.md"
        human.write_text("\n".join(md), encoding="utf-8")
        paths["ne_wy_deep_coverage_report.md"] = str(human)
        return paths


# Fallback WY district list with websites (from WDE public materials) if PDF parse fails
_WY_DISTRICT_FALLBACK: list[tuple[str, str]] = [
    ("Albany County School District #1", "https://www.acsd1.org"),
    ("Big Horn County School District #1", "https://www.bighorn1.com"),
    ("Big Horn County School District #2", "https://www.bgh2.org"),
    ("Big Horn County School District #3", "https://greybullschools.com"),
    ("Big Horn County School District #4", "https://www.bgh4.k12.wy.us"),
    ("Campbell County School District #1", "https://www.campbellcountyschools.net"),
    ("Carbon County School District #1", "https://www.crb1.net"),
    ("Carbon County School District #2", "https://www.crb2.org"),
    ("Converse County School District #1", "https://www.converse1.org"),
    ("Converse County School District #2", "https://www.ccs2.org"),
    ("Crook County School District #1", "https://www.crook1.com"),
    ("Fremont County School District #1", "https://www.landerschools.org"),
    ("Fremont County School District #2", "https://www.fremont2.org"),
    ("Fremont County School District #6", "https://www.fremont6.org"),
    ("Fremont County School District #14", "https://www.fcsd14.org"),
    ("Fremont County School District #21", "https://www.fcsd21.org"),
    ("Fremont County School District #24", "https://www.fcsd24.org"),
    ("Fremont County School District #25", "https://www.riverton.k12.wy.us"),
    ("Fremont County School District #38", "https://www.fcsd38.org"),
    ("Goshen County School District #1", "https://www.goshen1.org"),
    ("Hot Springs County School District #1", "https://www.hotsprings1.org"),
    ("Johnson County School District #1", "https://www.johnson1.org"),
    ("Laramie County School District #1", "https://www.laramie1.org"),
    ("Laramie County School District #2", "https://www.laramie2.org"),
    ("Lincoln County School District #1", "https://www.lcsd1.org"),
    ("Lincoln County School District #2", "https://www.lcsd2.org"),
    ("Natrona County School District #1", "https://www.natronaschools.org"),
    ("Niobrara County School District #1", "https://www.niobraracounty.org"),
    ("Park County School District #1", "https://www.pcsd1.org"),
    ("Park County School District #6", "https://www.park6.org"),
    ("Park County School District #16", "https://www.pcsd16.org"),
    ("Platte County School District #1", "https://www.platte1.org"),
    ("Platte County School District #2", "https://www.platte2.org"),
    ("Sheridan County School District #1", "https://www.sheridan.k12.wy.us"),
    ("Sheridan County School District #2", "https://www.scsd2.com"),
    ("Sheridan County School District #3", "https://www.sheridan3.com"),
    ("Sublette County School District #1", "https://www.sublette1.org"),
    ("Sublette County School District #9", "https://www.sublette9.org"),
    ("Sweetwater County School District #1", "https://www.sws1.org"),
    ("Sweetwater County School District #2", "https://www.sws2.org"),
    ("Teton County School District #1", "https://www.tcsd.org"),
    ("Uinta County School District #1", "https://www.uinta1.com"),
    ("Uinta County School District #4", "https://www.uinta4.com"),
    ("Uinta County School District #6", "https://www.uinta6.com"),
    ("Washakie County School District #1", "https://www.wsh1.org"),
    ("Washakie County School District #2", "https://www.washakie2.org"),
    ("Weston County School District #1", "https://www.weston1.org"),
    ("Weston County School District #7", "https://www.weston7.org"),
]


def run_ne_wy_deep_coverage(**kwargs: Any) -> dict[str, Any]:
    return NeWyDeepCoverageSweep(**kwargs).run()
