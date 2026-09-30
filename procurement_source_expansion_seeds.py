"""Concrete expansion seed tables for Sweep #2 (K-12, special districts, local, HE)."""

from __future__ import annotations

from typing import Any, Callable


K12_DIRECTORY_SEEDS: list[dict[str, Any]] = [
    {"seed_name": "NCES CCD District Search", "url": "https://nces.ed.gov/ccd/districtsearch/", "kind": "ENTITY_DIRECTORY", "government_level": "NETWORK", "role": "SEED_DIRECTORY", "entity_map_only": True, "notes": "Authoritative district directory — map only"},
    {"seed_name": "NCES CCD Public LEA data files", "url": "https://nces.ed.gov/ccd/files.asp", "kind": "ENTITY_DIRECTORY", "government_level": "NETWORK", "role": "SEED_DIRECTORY", "entity_map_only": True},
    {"seed_name": "Texas TEA school district locator", "url": "https://tea.texas.gov/texas-schools/general-information/school-district-locator/school-district-locator", "kind": "ENTITY_DIRECTORY", "government_level": "STATE", "state": "TX", "role": "SEED_DIRECTORY", "entity_map_only": True},
    {"seed_name": "California CDE school directory", "url": "https://www.cde.ca.gov/schooldirectory/", "kind": "ENTITY_DIRECTORY", "government_level": "STATE", "state": "CA", "role": "SEED_DIRECTORY", "entity_map_only": True},
    {"seed_name": "Florida DOE school districts", "url": "https://www.fldoe.org/accountability/data-sys/edu-info-accountability-services/pk-12-public-school-data-pubs-reports/school-districts.stml", "kind": "ENTITY_DIRECTORY", "government_level": "STATE", "state": "FL", "role": "SEED_DIRECTORY", "entity_map_only": True},
    {"seed_name": "Illinois ISBE data analysis directory", "url": "https://www.isbe.net/Pages/DataAnalysisDirectory.aspx", "kind": "ENTITY_DIRECTORY", "government_level": "STATE", "state": "IL", "role": "SEED_DIRECTORY", "entity_map_only": True},
    {"seed_name": "Ohio Educational Directory System", "url": "https://education.ohio.gov/Topics/Data/Ohio-Educational-Directory-System-OEDS", "kind": "ENTITY_DIRECTORY", "government_level": "STATE", "state": "OH", "role": "SEED_DIRECTORY", "entity_map_only": True},
    {"seed_name": "Pennsylvania PDE educational directories", "url": "https://www.education.pa.gov/Pages/EducationalDirectories.aspx", "kind": "ENTITY_DIRECTORY", "government_level": "STATE", "state": "PA", "role": "SEED_DIRECTORY", "entity_map_only": True},
    {"seed_name": "Georgia DOE schools and districts", "url": "https://www.gadoe.org/External-Affairs-and-Policy/AskDOE/Pages/Schools-and-Districts.aspx", "kind": "ENTITY_DIRECTORY", "government_level": "STATE", "state": "GA", "role": "SEED_DIRECTORY", "entity_map_only": True},
    {"seed_name": "North Carolina DPI districts schools", "url": "https://www.dpi.nc.gov/districts-schools/districts-schools", "kind": "ENTITY_DIRECTORY", "government_level": "STATE", "state": "NC", "role": "SEED_DIRECTORY", "entity_map_only": True},
    {"seed_name": "Washington OSPI about school districts", "url": "https://www.k12.wa.us/about-ospi/about-school-districts", "kind": "ENTITY_DIRECTORY", "government_level": "STATE", "state": "WA", "role": "SEED_DIRECTORY", "entity_map_only": True},
    {"seed_name": "Colorado CDE district directory", "url": "https://www.cde.state.co.us/cdereval/rvdirectorylist", "kind": "ENTITY_DIRECTORY", "government_level": "STATE", "state": "CO", "role": "SEED_DIRECTORY", "entity_map_only": True},
    {"seed_name": "Arizona ADE districts", "url": "https://www.azed.gov/districts", "kind": "ENTITY_DIRECTORY", "government_level": "STATE", "state": "AZ", "role": "SEED_DIRECTORY", "entity_map_only": True},
    {"seed_name": "AEPA member agencies directory", "url": "https://aepacoop.org/members/", "kind": "ENTITY_DIRECTORY", "government_level": "COOPERATIVE", "role": "SEED_DIRECTORY", "entity_map_only": True},
    {"seed_name": "NY BOCES", "url": "https://www.boces.org/", "kind": "ENTITY_DIRECTORY", "government_level": "STATE", "state": "NY", "role": "SEED_DIRECTORY", "entity_map_only": True},
    {"seed_name": "Michigan CEPI", "url": "https://www.michigan.gov/mde/services/school-performance/cepi", "kind": "ENTITY_DIRECTORY", "government_level": "STATE", "state": "MI", "role": "SEED_DIRECTORY", "entity_map_only": True},
]

# (name, state, url, platform)
K12_PORTAL_SEEDS: list[tuple[str, str, str, str]] = [
    ("Los Angeles Unified School District", "CA", "https://www.lausd.org/Page/3341", "PlanetBids"),
    ("Houston Independent School District", "TX", "https://www.houstonisd.org/Page/32487", "Bonfire"),
    ("Dallas Independent School District", "TX", "https://www.dallasisd.org/Page/547", "Bonfire"),
    ("Cypress-Fairbanks ISD", "TX", "https://www.cfisd.net/en/about/departments/business-financial-services/purchasing/", "Bonfire"),
    ("Northside ISD", "TX", "https://www.nisd.net/district/purchasing", "Bonfire"),
    ("Fort Worth ISD", "TX", "https://www.fwisd.org/departments/procurement-and-materials-management", "Bonfire"),
    ("Austin ISD", "TX", "https://www.austinisd.org/purchasing", "Bonfire"),
    ("San Antonio ISD", "TX", "https://www.saisd.net/page/purchasing", "Bonfire"),
    ("Aldine ISD", "TX", "https://www.aldineisd.org/departments/purchasing/", "Bonfire"),
    ("Chicago Public Schools", "IL", "https://www.cps.edu/about/departments/procurement/", "BidNet"),
    ("Miami-Dade County Public Schools", "FL", "https://procurement.dadeschools.net/", "BidNet"),
    ("Broward County Public Schools", "FL", "https://www.browardschools.com/Page/32440", "Bonfire"),
    ("Hillsborough County Public Schools", "FL", "https://www.hillsboroughschools.org/Page/4458", "Bonfire"),
    ("Orange County Public Schools FL", "FL", "https://www.ocps.net/departments/procurement_services", "Bonfire"),
    ("Clark County School District", "NV", "https://www.ccsd.net/departments/purchasing/", "Bonfire"),
    ("Fairfax County Public Schools", "VA", "https://www.fcps.edu/department/office-procurement-services", "Bonfire"),
    ("Prince William County Public Schools", "VA", "https://www.pwcs.edu/departments/procurement", "Bonfire"),
    ("New York City Department of Education", "NY", "https://www.schools.nyc.gov/about-us/funding/doing-business-with-the-doe", "SimpleHTML"),
    ("Philadelphia School District", "PA", "https://www.philasd.org/procurement/", "Bonfire"),
    ("Pittsburgh Public Schools", "PA", "https://www.pghschools.org/Page/412", "Bonfire"),
    ("Baltimore City Public Schools", "MD", "https://www.baltimorecityschools.org/page/procurement", "Bonfire"),
    ("Montgomery County Public Schools MD", "MD", "https://www.montgomeryschoolsmd.org/departments/procurement/", "Bonfire"),
    ("Gwinnett County Public Schools", "GA", "https://www.gcpsk12.org/departments/business-and-finance/procurement", "Bonfire"),
    ("Fulton County Schools", "GA", "https://www.fultonschools.org/Page/424", "Bonfire"),
    ("Cobb County School District", "GA", "https://www.cobbk12.org/page/302", "Bonfire"),
    ("Charlotte-Mecklenburg Schools", "NC", "https://www.cmsk12.org/Page/73", "Bonfire"),
    ("Wake County Public School System", "NC", "https://www.wcpss.net/Page/34", "Bonfire"),
    ("Denver Public Schools", "CO", "https://www.dpsk12.org/departments/financial-services/procurement/", "Bonfire"),
    ("Jefferson County Public Schools CO", "CO", "https://www.jeffcopublicschools.org/departments/financial_services/purchasing", "Bonfire"),
    ("Seattle Public Schools", "WA", "https://www.seattleschools.org/departments/procurement/", "OpenGov"),
    ("Portland Public Schools", "OR", "https://www.pps.net/Page/1636", "Bonfire"),
    ("San Diego Unified School District", "CA", "https://www.sandiegounified.org/departments/contracts_and_procurement", "PlanetBids"),
    ("San Francisco Unified School District", "CA", "https://www.sfusd.edu/about-sfusd/departments/contracts-and-purchasing", "PlanetBids"),
    ("Long Beach Unified School District", "CA", "https://www.lbschools.net/departments/purchasing", "PlanetBids"),
    ("Fresno Unified School District", "CA", "https://www.fresnounified.org/dept/purchasing/", "PlanetBids"),
    ("Detroit Public Schools Community District", "MI", "https://www.detroitk12.org/Page/9972", "Bonfire"),
    ("Columbus City Schools", "OH", "https://www.ccsoh.us/Page/1234", "Bonfire"),
    ("Cleveland Metropolitan School District", "OH", "https://www.clevelandmetroschools.org/Page/543", "Bonfire"),
    ("Indianapolis Public Schools", "IN", "https://www.myips.org/departments/operations/procurement/", "Bonfire"),
    ("Milwaukee Public Schools", "WI", "https://mps.milwaukee.k12.wi.us/en/District/About-MPS/Departments/Financial-Services/Purchasing.htm", "SimpleHTML"),
    ("Boston Public Schools", "MA", "https://www.bostonpublicschools.org/Page/6647", "OpenGov"),
    ("Nashville Metro Public Schools", "TN", "https://www.mnps.org/about/departments/procurement", "Bonfire"),
    ("Shelby County Schools", "TN", "https://www.scsk12.org/procurement", "Bonfire"),
    ("Albuquerque Public Schools", "NM", "https://www.aps.edu/procurement", "Bonfire"),
    ("Oklahoma City Public Schools", "OK", "https://www.okcps.org/Page/234", "Bonfire"),
    ("Tulsa Public Schools", "OK", "https://www.tulsaschools.org/about/departments/financial-services/purchasing", "Bonfire"),
    ("Anchorage School District", "AK", "https://www.asdk12.org/Page/5805", "PublicPurchase"),
    ("Hawaii DOE Procurement", "HI", "https://www.hawaiipublicschools.org/ConnectWithUs/Vendors/Pages/home.aspx", "SimpleHTML"),
    ("Jordan School District", "UT", "https://purchasing.jordandistrict.org/", "Bonfire"),
    ("Davis School District", "UT", "https://www.davis.k12.ut.us/departments/purchasing", "Bonfire"),
]

# (name, state, entity_type, url, platform)
SPECIAL_DISTRICT_SEEDS: list[tuple[str, str, str, str, str]] = [
    ("Metropolitan Water District of Southern California", "CA", "SPECIAL_DISTRICT", "https://www.mwdh2o.com/doing-business/", "PlanetBids"),
    ("East Bay Municipal Utility District", "CA", "SPECIAL_DISTRICT", "https://www.ebmud.com/business-center/bids-and-proposals/", "PlanetBids"),
    ("Santa Clara Valley Water District", "CA", "SPECIAL_DISTRICT", "https://www.valleywater.org/contractors/doing-businesses/bids-rfps", "PlanetBids"),
    ("Metropolitan Water Reclamation District of Greater Chicago", "IL", "SPECIAL_DISTRICT", "https://mwrd.org/doing-business", "BidNet"),
    ("King County Wastewater Treatment Division", "WA", "SPECIAL_DISTRICT", "https://kingcounty.gov/en/dept/dnrp/waste-services/wastewater-treatment/contractor-resources", "OpenGov"),
    ("Central Arizona Project", "AZ", "SPECIAL_DISTRICT", "https://www.cap-az.com/doing-business/", "Bonfire"),
    ("Southern Nevada Water Authority", "NV", "SPECIAL_DISTRICT", "https://www.snwa.com/business/procurement/index.html", "Bonfire"),
    ("Tampa Bay Water", "FL", "SPECIAL_DISTRICT", "https://www.tampabaywater.org/business/procurement", "Bonfire"),
    ("North Texas Municipal Water District", "TX", "SPECIAL_DISTRICT", "https://www.ntmwd.com/doing-business/", "Bonfire"),
    ("Lower Colorado River Authority", "TX", "SPECIAL_DISTRICT", "https://www.lcra.org/about/doing-business-with-lcra/", "Bonfire"),
    ("Sacramento Municipal Utility District", "CA", "UTILITY", "https://www.smud.org/en/Corporate/Doing-Business-with-SMUD/Purchasing-and-Contracts", "PlanetBids"),
    ("Seattle City Light", "WA", "UTILITY", "https://www.seattle.gov/city-light/about-us/doing-business", "OpenGov"),
    ("Austin Energy", "TX", "UTILITY", "https://austinenergy.com/ae/about/doing-business", "SimpleHTML"),
    ("Los Angeles Department of Water and Power", "CA", "UTILITY", "https://www.ladwp.com/ladwp/faces/ladwp/aboutus/a-procurement", "PlanetBids"),
    ("Chicago Park District", "IL", "SPECIAL_DISTRICT", "https://www.chicagoparkdistrict.com/about-us/doing-business", "BidNet"),
    ("East Bay Regional Park District", "CA", "SPECIAL_DISTRICT", "https://www.ebparks.org/about/procurement", "PlanetBids"),
    ("Cook County Forest Preserve District", "IL", "SPECIAL_DISTRICT", "https://fpdcc.com/about/doing-business/", "BidNet"),
    ("Orange County Fire Authority", "CA", "SPECIAL_DISTRICT", "https://www.ocfa.org/AboutUs/Procurement.aspx", "PlanetBids"),
    ("Harris County Flood Control District", "TX", "SPECIAL_DISTRICT", "https://www.hcfcd.org/About/Doing-Business", "Bonfire"),
    ("Dallas Area Rapid Transit", "TX", "TRANSIT", "https://www.dart.org/about/doingbusiness/procurement.asp", "Bonfire"),
    ("Metropolitan Atlanta Rapid Transit Authority", "GA", "TRANSIT", "https://www.itsmarta.com/doing-business.aspx", "Bonfire"),
    ("Bay Area Rapid Transit", "CA", "TRANSIT", "https://www.bart.gov/about/business", "PlanetBids"),
    ("TriMet", "OR", "TRANSIT", "https://trimet.org/about/procurement.htm", "Bonfire"),
    ("Sound Transit", "WA", "TRANSIT", "https://www.soundtransit.org/get-to-know-us/doing-business", "OpenGov"),
    ("Port of Seattle", "WA", "PORT_AUTHORITY", "https://www.portseattle.org/business/procurement", "OpenGov"),
    ("Port of Houston Authority", "TX", "PORT_AUTHORITY", "https://porthouston.com/business/procurement/", "Bonfire"),
    ("Port of Long Beach", "CA", "PORT_AUTHORITY", "https://polb.com/business/bids-proposals/", "PlanetBids"),
    ("Massachusetts Port Authority", "MA", "PORT_AUTHORITY", "https://www.massport.com/massport/business/bids-opportunities/", "OpenGov"),
    ("New York City Housing Authority", "NY", "HOUSING_AUTHORITY", "https://www.nyc.gov/site/nycha/business/procurement.page", "SimpleHTML"),
    ("Chicago Housing Authority", "IL", "HOUSING_AUTHORITY", "https://www.thecha.org/doing-business", "BidNet"),
    ("Housing Authority of the City of Los Angeles", "CA", "HOUSING_AUTHORITY", "https://www.hacla.org/en/about-hacla/doing-business", "PlanetBids"),
    ("Atlanta Housing", "GA", "HOUSING_AUTHORITY", "https://www.atlantahousing.org/business/", "Bonfire"),
    ("Los Angeles Public Library", "CA", "LIBRARY", "https://www.lapl.org/about-lapl/business-opportunities", "SimpleHTML"),
    ("Chicago Public Library", "IL", "LIBRARY", "https://www.chipublib.org/about-us/doing-business/", "BidNet"),
    ("New York Public Library", "NY", "LIBRARY", "https://www.nypl.org/about/locations/procurement", "SimpleHTML"),
    ("Denver Public Library", "CO", "LIBRARY", "https://www.denverlibrary.org/procurement", "Bonfire"),
    ("Hartsfield-Jackson Atlanta International Airport", "GA", "AIRPORT", "https://www.atl.com/business-opportunities/", "Bonfire"),
    ("Dallas/Fort Worth International Airport", "TX", "AIRPORT", "https://www.dfwairport.com/business/solicitations/", "Bonfire"),
    ("Los Angeles World Airports", "CA", "AIRPORT", "https://www.lawa.org/lawa-businesses/lawa-business-opportunities", "PlanetBids"),
    ("Chicago Department of Aviation", "IL", "AIRPORT", "https://www.flychicago.com/business/opportunities/Pages/default.aspx", "BidNet"),
    ("Denver International Airport", "CO", "AIRPORT", "https://www.flydenver.com/about/business_opportunities", "Bonfire"),
    ("Sea-Tac Airport / Port of Seattle", "WA", "AIRPORT", "https://www.portseattle.org/sea-tac/business", "OpenGov"),
    ("Miami International Airport", "FL", "AIRPORT", "https://www.miami-airport.com/business_opportunities.asp", "BidNet"),
    ("Phoenix Sky Harbor", "AZ", "AIRPORT", "https://www.skyharbor.com/business/doing-business", "Bonfire"),
]

COUNTY_CITY_SEEDS: list[tuple[str, str, str, str, str]] = [
    ("Los Angeles County", "CA", "COUNTY", "https://lacounty.gov/business/doing-business-with-the-county/", "PlanetBids"),
    ("Orange County CA", "CA", "COUNTY", "https://www.ocgov.com/gov/ceo/procure", "PlanetBids"),
    ("San Diego County", "CA", "COUNTY", "https://www.sandiegocounty.gov/content/sdc/purchasing.html", "PlanetBids"),
    ("Riverside County", "CA", "COUNTY", "https://rivco.org/services/purchasing", "PlanetBids"),
    ("San Bernardino County", "CA", "COUNTY", "https://wp.sbcounty.gov/purchasing/", "PlanetBids"),
    ("Santa Clara County", "CA", "COUNTY", "https://procurement.sccgov.org/", "PlanetBids"),
    ("Alameda County", "CA", "COUNTY", "https://www.acgov.org/gsa_app/gsa/purchasing/", "PlanetBids"),
    ("Cook County", "IL", "COUNTY", "https://www.cookcountyil.gov/service/procurement", "BidNet"),
    ("DuPage County", "IL", "COUNTY", "https://www.dupagecounty.gov/government/departments/procurement/", "BidNet"),
    ("Harris County", "TX", "COUNTY", "https://www.harriscountytx.gov/Purchasing", "Bonfire"),
    ("Dallas County", "TX", "COUNTY", "https://www.dallascounty.org/departments/purchasing/", "Bonfire"),
    ("Tarrant County", "TX", "COUNTY", "https://www.tarrantcounty.com/en/purchasing.html", "Bonfire"),
    ("Bexar County", "TX", "COUNTY", "https://www.bexar.org/1483/Purchasing", "Bonfire"),
    ("Travis County", "TX", "COUNTY", "https://www.traviscountytx.gov/purchasing", "Bonfire"),
    ("Maricopa County", "AZ", "COUNTY", "https://www.maricopa.gov/3978/Procurement", "Bonfire"),
    ("Pima County", "AZ", "COUNTY", "https://www.pima.gov/government/procurement", "Bonfire"),
    ("Miami-Dade County", "FL", "COUNTY", "https://www.miamidade.gov/global/economy/procurement/home.page", "BidNet"),
    ("Broward County", "FL", "COUNTY", "https://www.broward.org/Purchasing/Pages/Default.aspx", "Bonfire"),
    ("Palm Beach County", "FL", "COUNTY", "https://discover.pbcgov.org/purchasing/Pages/default.aspx", "Bonfire"),
    ("Hillsborough County", "FL", "COUNTY", "https://www.hillsboroughcounty.org/en/government/departments/procurement-services", "Bonfire"),
    ("King County", "WA", "COUNTY", "https://kingcounty.gov/en/dept/executive-services/business-operations/procurement", "OpenGov"),
    ("Pierce County", "WA", "COUNTY", "https://www.piercecountywa.gov/159/Purchasing", "OpenGov"),
    ("Snohomish County", "WA", "COUNTY", "https://snohomishcountywa.gov/256/Purchasing", "OpenGov"),
    ("Fairfax County", "VA", "COUNTY", "https://www.fairfaxcounty.gov/procurement/", "Bonfire"),
    ("Loudoun County", "VA", "COUNTY", "https://www.loudoun.gov/procurement", "Bonfire"),
    ("Prince William County", "VA", "COUNTY", "https://www.pwcgov.org/government/dept/finance/Pages/Procurement.aspx", "Bonfire"),
    ("Montgomery County MD", "MD", "COUNTY", "https://www.montgomerycountymd.gov/PRO/", "Bonfire"),
    ("Prince George's County", "MD", "COUNTY", "https://www.princegeorgescountymd.gov/departments-offices/central-services/procurement", "Bonfire"),
    ("Baltimore County", "MD", "COUNTY", "https://www.baltimorecountymd.gov/departments/budfin/purchasing/", "Bonfire"),
    ("Clark County NV", "NV", "COUNTY", "https://www.clarkcountynv.gov/government/departments/finance/purchasing/", "Bonfire"),
    ("Washoe County", "NV", "COUNTY", "https://www.washoecounty.gov/purchasing/", "Bonfire"),
    ("Allegheny County", "PA", "COUNTY", "https://www.alleghenycounty.us/purchasing/", "SimpleHTML"),
    ("Franklin County OH", "OH", "COUNTY", "https://procurement.franklincountyohio.gov/", "Bonfire"),
    ("Cuyahoga County", "OH", "COUNTY", "https://cuyahogacounty.gov/purchasing", "Bonfire"),
    ("Wayne County MI", "MI", "COUNTY", "https://www.waynecounty.com/departments/purchasing", "Bonfire"),
    ("Oakland County MI", "MI", "COUNTY", "https://www.oakgov.com/government/departments/purchasing", "Bonfire"),
    ("Hennepin County", "MN", "COUNTY", "https://www.hennepin.us/business/work-with-hennepin/purchasing", "SimpleHTML"),
    ("Ramsey County", "MN", "COUNTY", "https://www.ramseycounty.us/businesses/work-ramsey-county/purchasing", "SimpleHTML"),
    ("Multnomah County", "OR", "COUNTY", "https://www.multco.us/purchasing", "Bonfire"),
    ("Salt Lake County", "UT", "COUNTY", "https://slco.org/purchasing/", "Bonfire"),
    ("Bernalillo County", "NM", "COUNTY", "https://www.bernco.gov/finance/purchasing/", "Bonfire"),
    ("Oklahoma County", "OK", "COUNTY", "https://www.oklahomacounty.org/departments/purchasing", "Bonfire"),
    ("Shelby County TN", "TN", "COUNTY", "https://www.shelbycountytn.gov/117/Purchasing", "Bonfire"),
    ("Davidson County / Metro Nashville", "TN", "COUNTY", "https://www.nashville.gov/departments/finance/procurement", "Bonfire"),
    ("Jefferson County AL", "AL", "COUNTY", "https://www.jccal.org/Default.asp?ID=95&pg=Purchasing", "SimpleHTML"),
    ("Fulton County GA", "GA", "COUNTY", "https://www.fultoncountyga.gov/services/doing-business/purchasing", "Bonfire"),
    ("DeKalb County GA", "GA", "COUNTY", "https://www.dekalbcountyga.gov/purchasing", "Bonfire"),
    ("Mecklenburg County", "NC", "COUNTY", "https://www.mecknc.gov/Finance/Procurement/Pages/default.aspx", "Bonfire"),
    ("Wake County", "NC", "COUNTY", "https://www.wake.gov/departments-government/finance/procurement-services", "Bonfire"),
    ("City of New York", "NY", "CITY_MUNICIPAL", "https://a856-cityrecord.nyc.gov/", "SimpleHTML"),
    ("City of Philadelphia", "PA", "CITY_MUNICIPAL", "https://www.phila.gov/departments/procurement-department/", "SimpleHTML"),
    ("City of San Antonio", "TX", "CITY_MUNICIPAL", "https://www.sanantonio.gov/purchasing", "Bonfire"),
    ("City of San Diego", "CA", "CITY_MUNICIPAL", "https://www.sandiego.gov/purchasing", "PlanetBids"),
    ("City of Dallas", "TX", "CITY_MUNICIPAL", "https://dallascityhall.com/departments/procurement/pages/default.aspx", "Bonfire"),
    ("City of Austin", "TX", "CITY_MUNICIPAL", "https://financeonline.austintexas.gov/afo/account_services/solicitation/solicitations.cfm", "SimpleHTML"),
    ("City of Fort Worth", "TX", "CITY_MUNICIPAL", "https://www.fortworthtexas.gov/departments/finance/purchasing", "Bonfire"),
    ("City of El Paso", "TX", "CITY_MUNICIPAL", "https://www.elpasotexas.gov/purchasing/", "Bonfire"),
    ("City of Columbus", "OH", "CITY_MUNICIPAL", "https://www.columbus.gov/procurement/", "Bonfire"),
    ("City of Charlotte", "NC", "CITY_MUNICIPAL", "https://charlottenc.gov/DoingBusiness/Pages/default.aspx", "Bonfire"),
    ("City of Detroit", "MI", "CITY_MUNICIPAL", "https://www.detroitmi.gov/government/departments/office-contracting-and-procurement", "Bonfire"),
    ("City of Portland", "OR", "CITY_MUNICIPAL", "https://www.portland.gov/omf/brfs/procurement", "SimpleHTML"),
    ("City of Minneapolis", "MN", "CITY_MUNICIPAL", "https://www.minneapolismn.gov/government/departments/finance/procurement/", "SimpleHTML"),
    ("City of Nashville", "TN", "CITY_MUNICIPAL", "https://www.nashville.gov/departments/finance/procurement", "Bonfire"),
    ("City of Memphis", "TN", "CITY_MUNICIPAL", "https://www.memphistn.gov/government/finance/purchasing/", "Bonfire"),
    ("City of Kansas City MO", "MO", "CITY_MUNICIPAL", "https://www.kcmo.gov/city-hall/departments/finance/procurement-services", "Bonfire"),
    ("City of St. Louis", "MO", "CITY_MUNICIPAL", "https://www.stlouis-mo.gov/government/departments/supply/index.cfm", "SimpleHTML"),
    ("City of Indianapolis", "IN", "CITY_MUNICIPAL", "https://www.indy.gov/agency/office-of-finance-and-management", "Bonfire"),
    ("City of Milwaukee", "WI", "CITY_MUNICIPAL", "https://city.milwaukee.gov/Purchasing", "SimpleHTML"),
    ("City of Baltimore", "MD", "CITY_MUNICIPAL", "https://www.baltimorecity.gov/departments/finance/bureau-purchases", "Bonfire"),
    ("City of Virginia Beach", "VA", "CITY_MUNICIPAL", "https://www.vbgov.com/government/departments/finance/purchasing/", "Bonfire"),
    ("City of Raleigh", "NC", "CITY_MUNICIPAL", "https://raleighnc.gov/business/content/FinanceAdmin/Articles/Purchasing.html", "Bonfire"),
    ("City of Omaha", "NE", "CITY_MUNICIPAL", "https://www.cityofomaha.org/finance/purchasing", "SimpleHTML"),
    ("City of Tulsa", "OK", "CITY_MUNICIPAL", "https://www.cityoftulsa.org/government/departments/finance/purchasing/", "Bonfire"),
    ("City of Oklahoma City", "OK", "CITY_MUNICIPAL", "https://www.okc.gov/departments/finance/purchasing", "Bonfire"),
    ("City of Albuquerque", "NM", "CITY_MUNICIPAL", "https://www.cabq.gov/dfa/purchasing", "Bonfire"),
    ("City of Tucson", "AZ", "CITY_MUNICIPAL", "https://www.tucsonaz.gov/Departments/Procurement", "Bonfire"),
    ("City of Mesa", "AZ", "CITY_MUNICIPAL", "https://www.mesaaz.gov/business/procurement", "Bonfire"),
    ("City of Sacramento", "CA", "CITY_MUNICIPAL", "https://www.cityofsacramento.gov/finance/procurement", "PlanetBids"),
    ("City of Oakland", "CA", "CITY_MUNICIPAL", "https://www.oaklandca.gov/departments/contracts-and-compliance", "PlanetBids"),
    ("City of Fresno", "CA", "CITY_MUNICIPAL", "https://www.fresno.gov/finance/purchasing/", "PlanetBids"),
    ("City of Long Beach", "CA", "CITY_MUNICIPAL", "https://www.longbeach.gov/purchasing/", "PlanetBids"),
    ("City of Anaheim", "CA", "CITY_MUNICIPAL", "https://www.anaheim.net/2176/Purchasing", "PlanetBids"),
    ("City of Tampa", "FL", "CITY_MUNICIPAL", "https://www.tampa.gov/purchasing", "Bonfire"),
    ("City of Orlando", "FL", "CITY_MUNICIPAL", "https://www.orlando.gov/Business-Development/Doing-Business-with-the-City", "Bonfire"),
    ("City of Jacksonville", "FL", "CITY_MUNICIPAL", "https://www.jacksonville.gov/departments/finance/purchasing", "BidNet"),
    ("City of Miami", "FL", "CITY_MUNICIPAL", "https://www.miamigov.com/Government/Departments-Organizations/Procurement", "BidNet"),
    ("City of New Orleans", "LA", "CITY_MUNICIPAL", "https://www.nola.gov/purchasing/", "SimpleHTML"),
    ("City of Baton Rouge", "LA", "CITY_MUNICIPAL", "https://www.brla.gov/149/Purchasing", "SimpleHTML"),
    ("City of Birmingham", "AL", "CITY_MUNICIPAL", "https://www.birminghamal.gov/about/city-directory/finance/purchasing/", "SimpleHTML"),
    ("City of Little Rock", "AR", "CITY_MUNICIPAL", "https://www.littlerock.gov/city-administration/city-departments/finance/purchasing/", "SimpleHTML"),
    ("City of Des Moines", "IA", "CITY_MUNICIPAL", "https://www.dsm.city/departments/finance_department/purchasing/", "Jaggaer"),
    ("City of Boise", "ID", "CITY_MUNICIPAL", "https://www.cityofboise.org/departments/finance-and-administration/purchasing/", "SimpleHTML"),
    ("City of Billings", "MT", "CITY_MUNICIPAL", "https://www.billingsmt.gov/149/Purchasing", "SimpleHTML"),
    ("City of Sioux Falls", "SD", "CITY_MUNICIPAL", "https://www.siouxfalls.org/finance/purchasing", "SimpleHTML"),
    ("City of Fargo", "ND", "CITY_MUNICIPAL", "https://fargond.gov/city-government/departments/finance/purchasing", "SimpleHTML"),
    ("City of Burlington VT", "VT", "CITY_MUNICIPAL", "https://www.burlingtonvt.gov/Purchasing", "SimpleHTML"),
    ("City of Portland ME", "ME", "CITY_MUNICIPAL", "https://www.portlandmaine.gov/156/Purchasing", "SimpleHTML"),
    ("City of Manchester NH", "NH", "CITY_MUNICIPAL", "https://www.manchesternh.gov/Departments/Finance/Purchasing", "SimpleHTML"),
    ("City of Providence", "RI", "CITY_MUNICIPAL", "https://www.providenceri.gov/purchasing/", "SimpleHTML"),
    ("City of Bridgeport", "CT", "CITY_MUNICIPAL", "https://www.bridgeportct.gov/purchasing", "SimpleHTML"),
    ("City of Wilmington DE", "DE", "CITY_MUNICIPAL", "https://www.wilmingtonde.gov/government/city-departments/procurement-and-records", "Jaggaer"),
    ("City of Charleston SC", "SC", "CITY_MUNICIPAL", "https://www.charleston-sc.gov/148/Procurement", "SimpleHTML"),
    ("City of Columbia SC", "SC", "CITY_MUNICIPAL", "https://www.columbiasc.gov/procurement", "SimpleHTML"),
    ("City of Richmond VA", "VA", "CITY_MUNICIPAL", "https://www.rva.gov/procurement-services", "Bonfire"),
    ("City of Lexington KY", "KY", "CITY_MUNICIPAL", "https://www.lexingtonky.gov/procurement", "SimpleHTML"),
    ("City of Louisville", "KY", "CITY_MUNICIPAL", "https://louisvilleky.gov/government/procurement", "SimpleHTML"),
    ("City of Wichita", "KS", "CITY_MUNICIPAL", "https://www.wichita.gov/Purchasing", "SimpleHTML"),
    ("City of Lincoln NE", "NE", "CITY_MUNICIPAL", "https://www.lincoln.ne.gov/City/Departments/Finance/Purchasing", "SimpleHTML"),
    ("City of Anchorage", "AK", "CITY_MUNICIPAL", "https://www.muni.org/Departments/purchasing/Pages/default.aspx", "PublicPurchase"),
    ("City of Honolulu", "HI", "CITY_MUNICIPAL", "https://www.honolulu.gov/pur/", "SimpleHTML"),
    ("City of Cheyenne", "WY", "CITY_MUNICIPAL", "https://www.cheyennecity.org/Your-Government/Departments/Finance/Purchasing", "PublicPurchase"),
]

HIGHER_ED_SEEDS: list[tuple[str, str, str, str]] = [
    ("Pennsylvania State University", "PA", "https://purchasing.psu.edu/", "SimpleHTML"),
    ("Ohio State University", "OH", "https://busfin.osu.edu/buy-ohio-state", "SimpleHTML"),
    ("University of Washington", "WA", "https://finance.uw.edu/ps/", "SimpleHTML"),
    ("University of Florida", "FL", "https://procurement.ufl.edu/", "SimpleHTML"),
    ("Georgia Institute of Technology", "GA", "https://www.procurement.gatech.edu/", "SimpleHTML"),
    ("Arizona State University", "AZ", "https://cfo.asu.edu/purchasing", "SimpleHTML"),
    ("University of Texas at Austin", "TX", "https://procurement.utexas.edu/", "Bonfire"),
    ("Texas A&M University", "TX", "https://purchasing.tamu.edu/", "Bonfire"),
    ("University of California Procurement", "CA", "https://www.ucop.edu/procurement-services/", "OpenGov"),
    ("University of Michigan", "MI", "https://procurement.umich.edu/", "OpenGov"),
    ("University of Illinois", "IL", "https://www.obfs.uillinois.edu/purchases/", "BidNet"),
    ("University of Wisconsin System", "WI", "https://www.wisconsin.edu/procurement/", "SimpleHTML"),
    ("University of Minnesota", "MN", "https://purchasing.umn.edu/", "SimpleHTML"),
    ("University of North Carolina at Chapel Hill", "NC", "https://finance.unc.edu/procurement/", "SimpleHTML"),
    ("University of Virginia", "VA", "https://procurement.virginia.edu/", "Bonfire"),
    ("University of Maryland", "MD", "https://purchase.umd.edu/", "Bonfire"),
    ("Rutgers University", "NJ", "https://procurementservices.rutgers.edu/", "SimpleHTML"),
    ("University of Colorado", "CO", "https://www.cu.edu/psc", "Bonfire"),
    ("University of Oregon", "OR", "https://pcs.uoregon.edu/", "Bonfire"),
    ("University of Utah", "UT", "https://purchasing.utah.edu/", "Bonfire"),
    ("University of New Mexico", "NM", "https://purchase.unm.edu/", "Bonfire"),
    ("University of Oklahoma", "OK", "https://www.ou.edu/purchasing", "Bonfire"),
    ("Louisiana State University", "LA", "https://www.lsu.edu/administration/ofa/procurement/", "SimpleHTML"),
    ("University of Alabama", "AL", "https://purchasing.ua.edu/", "SimpleHTML"),
    ("University of Iowa", "IA", "https://uiowa.edu/purchasing", "Jaggaer"),
    ("Iowa State University", "IA", "https://www.procurement.iastate.edu/", "Jaggaer"),
    ("Purdue University", "IN", "https://www.purdue.edu/procurement/", "Bonfire"),
    ("Indiana University", "IN", "https://purchasing.iu.edu/", "Bonfire"),
    ("University of South Carolina", "SC", "https://www.sc.edu/about/offices_and_divisions/purchasing/", "SimpleHTML"),
    ("Clemson University", "SC", "https://www.clemson.edu/procurement/", "SimpleHTML"),
    ("Florida State University", "FL", "https://procurement.fsu.edu/", "SimpleHTML"),
    ("University of Central Florida", "FL", "https://procurement.ucf.edu/", "SimpleHTML"),
    ("Northern Virginia Community College", "VA", "https://www.nvcc.edu/business-services/", "Bonfire"),
    ("Lone Star College", "TX", "https://www.lonestar.edu/purchasing.htm", "Bonfire"),
    ("Maricopa Community Colleges", "AZ", "https://www.maricopa.edu/about/purchasing", "Bonfire"),
]

PLATFORM_DIRECTORY_SEEDS: list[dict[str, Any]] = [
    {"seed_name": "OpenGov procurement portal", "url": "https://procurement.opengov.com/", "kind": "PLATFORM", "government_level": "NETWORK", "platform_family": "OpenGov", "role": "PLATFORM_ENTRY", "one_account_many_entities": True},
    {"seed_name": "Bonfire vendor hub", "url": "https://vendors.bonfirehub.com/", "kind": "PLATFORM", "government_level": "NETWORK", "platform_family": "Bonfire", "role": "PLATFORM_ENTRY", "one_account_many_entities": True},
    {"seed_name": "Public Purchase home", "url": "https://www.publicpurchase.com/", "kind": "PLATFORM", "government_level": "NETWORK", "platform_family": "PublicPurchase", "role": "PLATFORM_ENTRY", "one_account_many_entities": True, "notes": "Often FREE_REGISTRATION for bid access"},
    {"seed_name": "PlanetBids home", "url": "https://home.planetbids.com/", "kind": "PLATFORM", "government_level": "NETWORK", "platform_family": "PlanetBids", "role": "PLATFORM_ENTRY", "one_account_many_entities": True},
    {"seed_name": "IonWave", "url": "https://www.ionwave.net/", "kind": "PLATFORM", "government_level": "NETWORK", "platform_family": "IonWave", "role": "PLATFORM_ENTRY", "one_account_many_entities": True},
    {"seed_name": "bids&tenders", "url": "https://www.bidsandtenders.com/", "kind": "PLATFORM", "government_level": "NETWORK", "platform_family": "BidsAndTenders", "role": "PLATFORM_ENTRY", "one_account_many_entities": True},
    {"seed_name": "VendorLink", "url": "https://www.vendorlink.com/", "kind": "PLATFORM", "government_level": "NETWORK", "platform_family": "VendorLink", "role": "PLATFORM_ENTRY", "one_account_many_entities": True},
    {"seed_name": "WebProcure", "url": "https://www.webprocure.com/", "kind": "PLATFORM", "government_level": "NETWORK", "platform_family": "WebProcure", "role": "PLATFORM_ENTRY", "one_account_many_entities": True},
    {"seed_name": "NASPO ValuePoint (NASPO eProcurement 403 alternate)", "url": "https://www.naspovaluepoint.org/", "kind": "COOPERATIVE", "government_level": "COOPERATIVE", "role": "SEED_DIRECTORY"},
    {"seed_name": "NASPO research publications index", "url": "https://www.naspo.org/research-and-innovation/", "kind": "STATE_DIRECTORY", "government_level": "NETWORK", "role": "SEED_DIRECTORY", "entity_map_only": True},
]


def build_expansion_seed_catalog(
    *,
    coverage_expansion_entities: list[dict[str, Any]],
    guess_entity_type: Callable[[str | None], str],
    row_to_seed: Callable[..., dict[str, Any]],
    seed_id_fn: Callable[..., str],
    norm_url: Callable[[str | None], str],
) -> list[dict[str, Any]]:
    seeds: list[dict[str, Any]] = []

    for d in K12_DIRECTORY_SEEDS:
        seeds.append({**d, "platform_family": d.get("platform_family"), "provenance": "expansion_sweep_2", "entity_type_hint": None})

    for name, st, url, plat in K12_PORTAL_SEEDS:
        seeds.append(row_to_seed(name, st, url, entity_type="K12_SCHOOL_DISTRICT", platform=plat))

    for name, st, et, url, plat in SPECIAL_DISTRICT_SEEDS:
        seeds.append(row_to_seed(name, st, url, entity_type=et, platform=plat))

    for name, st, et, url, plat in COUNTY_CITY_SEEDS:
        seeds.append(row_to_seed(name, st, url, entity_type=et, platform=plat))

    for name, st, url, plat in HIGHER_ED_SEEDS:
        seeds.append(row_to_seed(name, st, url, entity_type="HIGHER_EDUCATION", platform=plat))

    for d in PLATFORM_DIRECTORY_SEEDS:
        seeds.append({**d, "provenance": "expansion_sweep_2", "entity_type_hint": None})

    buyer_map = {
        "SCHOOL_DISTRICT": "K12_SCHOOL_DISTRICT",
        "SPECIAL_DISTRICT": "SPECIAL_DISTRICT",
        "COUNTY": "COUNTY",
        "CITY": "CITY_MUNICIPAL",
        "PUBLIC_UNIVERSITY": "HIGHER_EDUCATION",
        "AIRPORT": "AIRPORT",
        "TRANSIT": "TRANSIT",
        "PORT": "PORT_AUTHORITY",
        "HOUSING": "HOUSING_AUTHORITY",
        "LIBRARY": "LIBRARY",
        "COOPERATIVE": "COOPERATIVE",
    }
    for row in coverage_expansion_entities:
        url = row.get("procurement_url")
        if not url:
            continue
        et = buyer_map.get(row.get("buyer_type") or "", guess_entity_type(row.get("name")))
        seeds.append(
            row_to_seed(
                row["name"],
                row.get("state_code"),
                url,
                entity_type=et,
                platform=row.get("platform_family"),
                government_level=row.get("government_level") or "LOCAL",
            )
        )

    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for s in seeds:
        key = norm_url(s["url"]).lower()
        if not key or key in seen:
            continue
        seen.add(key)
        s["seed_id"] = seed_id_fn(s.get("kind") or "LOCAL", s["url"], s.get("seed_name") or "")
        out.append(s)
    return out
