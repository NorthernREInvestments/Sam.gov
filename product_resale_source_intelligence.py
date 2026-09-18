"""Product-resale procurement source intelligence — recipes, not archives.

Persists only: source recipes, category profiles, price paths, supplier knowledge,
financing/research methods. Never bulk solicitations or document bodies.
"""

from __future__ import annotations

import json
from typing import Any

from application_clock import now_utc

BUILD_TAG = "20260918-m3-product-resale-discovery-intelligence-1"
SOURCE_INTEL_SETTINGS_KEY = "m3_product_resale_source_intelligence_v1"
IOWA_SEED_SOLICITATION = "645-DOTRFB-2975-2027"

# Coverage states
ACTIVE = "ACTIVE"
PARTIAL = "PARTIAL"
MISSING = "MISSING"
BLOCKED = "BLOCKED"
LOW_VALUE = "LOW_VALUE"

# ROI tiers
HIGH_VALUE_SOURCE = "HIGH_VALUE_SOURCE"
MEDIUM_VALUE_SOURCE = "MEDIUM_VALUE_SOURCE"
LOW_VALUE_SOURCE = "LOW_VALUE_SOURCE"

# Gap classes
GAP_A_SOURCES = "A_MISSING_SOURCES"
GAP_B_SEARCH = "B_SEARCH_METHODS"
GAP_C_PRODUCT_ID = "C_PRODUCT_IDENTIFICATION"
GAP_D_DOCUMENTS = "D_DOCUMENT_ACCESS"
GAP_E_SUPPLIERS = "E_SUPPLIER_IDENTIFICATION"
GAP_F_ECONOMICS = "F_ECONOMICS_ESTIMATION"


def _utc() -> str:
    return now_utc().isoformat()


# ---------------------------------------------------------------------------
# PART 1 — Source recipes (reusable intelligence, not scrapers)
# ---------------------------------------------------------------------------

SOURCE_RECIPES: list[dict[str, Any]] = [
    # Federal
    {
        "source_id": "fed_sam_contract_opportunities",
        "name": "SAM.gov Get Opportunities API v2",
        "tier": "FEDERAL",
        "coverage_state": ACTIVE,
        "products_appear": "Broad — IT, industrial NSN parts, facilities, medical, tactical non-weapon",
        "accessibility": "Public API with free API key",
        "auth_required": "API_KEY",
        "search_capability": "Posted-date windows, org path, NAICS/PSC, notice type; paginated",
        "attachment_behavior": "resourceLinks + noticedesc; many bodies public with api_key",
        "api_available": True,
        "reseller_value": "Primary Federal discovery index — high volume, mixed product/service",
        "search_method": "active=yes + 30d posted windows + DLA org filter; enrich via noticedesc",
        "portal_behavior": "Listing is index; description often URL not text",
        "m3_status": "OPERATIONAL",
    },
    {
        "source_id": "dla_dibbs",
        "name": "DLA DIBBS (Internet Bid Board)",
        "tier": "FEDERAL",
        "coverage_state": BLOCKED,
        "products_appear": "NSN/NIIN spare parts, consumables, approved-source items",
        "accessibility": "Browser often reachable; automation bot-blocked",
        "auth_required": "REGISTRATION_FOR_QUOTE",
        "search_capability": "RFQ recents, FSC browse — not machine-reliable",
        "attachment_behavior": "Technical data often via TDMT/JCP; controlled common",
        "api_available": False,
        "reseller_value": "Highest DLA-native product density — access gap is the constraint",
        "search_method": "Do not bypass; reconcile via SAM SPE*/SPR* + noticedesc recovery",
        "portal_behavior": "BOT_BLOCKED_AUTOMATION measured",
        "m3_status": "BOT_BLOCKED_AUTOMATION",
    },
    {
        "source_id": "dla_tdmt",
        "name": "DLA TDMT (technical data — replaced cFolders)",
        "tier": "FEDERAL",
        "coverage_state": PARTIAL,
        "products_appear": "Drawings/specs for DLA NSN items",
        "accessibility": "Often eligibility/JCP/auth gated",
        "auth_required": "OFTEN_JCP_OR_CAGE",
        "search_capability": "Reference from solicitation — not open crawl",
        "attachment_behavior": "Controlled technical data common",
        "api_available": False,
        "reseller_value": "Package completeness for exact-part deals — access-state modeling only",
        "search_method": "Detect TDMT refs in SAM description; classify TECHNICAL_DATA_* state",
        "portal_behavior": "Do not hard-code cFolders; TDMT is current path",
        "m3_status": "STATE_MODELED_NO_BYPASS",
    },
    {
        "source_id": "gsa_advantage",
        "name": "GSA Advantage",
        "tier": "FEDERAL",
        "coverage_state": MISSING,
        "products_appear": "Catalog schedule products — IT, office, industrial, medical",
        "accessibility": "Public browse; transactional features may need login",
        "auth_required": "OPTIONAL_FOR_BROWSE",
        "search_capability": "Keyword/SIN/catalog search",
        "attachment_behavior": "Catalog pages, not solicitations",
        "api_available": False,
        "reseller_value": "Pricing/comps more than open RFQ discovery",
        "search_method": "Use for historical/comparable GSA price after identity known",
        "portal_behavior": "Schedule catalog — not open competition RFQ board",
        "m3_status": "PRICE_PATH_ONLY_PLANNED",
    },
    {
        "source_id": "gsa_ebuy",
        "name": "GSA eBuy",
        "tier": "FEDERAL",
        "coverage_state": MISSING,
        "products_appear": "RFQs against GSA schedules — product-heavy",
        "accessibility": "Vendor registration / schedule holder required for response",
        "auth_required": "VENDOR_ACCOUNT",
        "search_capability": "Logged-in RFQ search",
        "attachment_behavior": "RFQ packages behind auth",
        "api_available": False,
        "reseller_value": "High for schedule holders; limited for cashless first deals without schedule",
        "search_method": "Human/vendor portal — recipe only until schedule path exists",
        "portal_behavior": "Auth-gated RFQ channel",
        "m3_status": "RECIPE_ONLY",
    },
    {
        "source_id": "nasa_sewp",
        "name": "NASA SEWP",
        "tier": "FEDERAL",
        "coverage_state": LOW_VALUE,
        "products_appear": "IT hardware/software via SEWP contract holders",
        "accessibility": "Buyer/holder portals",
        "auth_required": "CONTRACT_HOLDER_OR_BUYER",
        "search_capability": "Vehicle-specific",
        "attachment_behavior": "Holder packages",
        "api_available": False,
        "reseller_value": "Requires being on vehicle or subcontracting — not first-cashless path",
        "search_method": "Monitor only if OEM/distributor relationship exists",
        "portal_behavior": "GWAC channel",
        "m3_status": "LOW_PRIORITY_RECIPE",
    },
    {
        "source_id": "nih_vehicle_style",
        "name": "NIH / CIO-SP style vehicles",
        "tier": "FEDERAL",
        "coverage_state": LOW_VALUE,
        "products_appear": "IT services-dominant; some hardware pass-through",
        "accessibility": "Holder task-order channels",
        "auth_required": "HOLDER",
        "search_capability": "Vehicle-specific",
        "attachment_behavior": "Gated",
        "api_available": False,
        "reseller_value": "Service-heavy — weak pure product resale without teaming",
        "search_method": "Skip for product-resale primary hunt",
        "portal_behavior": "IDIQ/GWAC",
        "m3_status": "LOW_PRIORITY_RECIPE",
    },
    # State / local platforms
    {
        "source_id": "network_bidnet_direct",
        "name": "BidNet Direct statewide networks",
        "tier": "STATE_LOCAL",
        "coverage_state": ACTIVE,
        "products_appear": "City/county/school product buys — fleet, facilities, IT, safety",
        "accessibility": "Public open-bid listings; packages often gated",
        "auth_required": "OFTEN_FOR_DOCUMENTS",
        "search_capability": "State network open solicitations; pagination critical",
        "attachment_behavior": "Metadata public; PDFs frequently registration-gated",
        "api_available": False,
        "reseller_value": "Highest national state/local yield currently measured",
        "search_method": "Statewide open-bids URLs; cheap-screen product vs service",
        "portal_behavior": "PUBLIC_METADATA_ONLY common",
        "m3_status": "OPERATIONAL_NATIONAL_SCALE",
    },
    {
        "source_id": "platform_bonfire",
        "name": "Bonfire public portals",
        "tier": "STATE_LOCAL",
        "coverage_state": PARTIAL,
        "products_appear": "Municipal/university product + construction mix",
        "accessibility": "Public project lists vary by agency",
        "auth_required": "SOMETIMES",
        "search_capability": "Agency-specific list pages",
        "attachment_behavior": "Often download after interest registration",
        "api_available": False,
        "reseller_value": "Good when live-verified agency seeds work",
        "search_method": "Agency seed URLs + live_bonfire adapter",
        "portal_behavior": "Per-agency HTML variance",
        "m3_status": "PARTIAL_ADAPTER",
    },
    {
        "source_id": "platform_opengov",
        "name": "OpenGov / procurement portals",
        "tier": "STATE_LOCAL",
        "coverage_state": PARTIAL,
        "products_appear": "City/county product RFPs",
        "accessibility": "Public lists common",
        "auth_required": "SOMETIMES",
        "search_capability": "Agency list/search",
        "attachment_behavior": "Mixed",
        "api_available": False,
        "reseller_value": "Medium — fewer verified live routes than BidNet",
        "search_method": "Agency seeds + live_opengov",
        "portal_behavior": "HTML",
        "m3_status": "PARTIAL_ADAPTER",
    },
    {
        "source_id": "platform_planetbids",
        "name": "PlanetBids",
        "tier": "STATE_LOCAL",
        "coverage_state": PARTIAL,
        "products_appear": "CA/municipal product + construction",
        "accessibility": "Public bid lists; docs may gate",
        "auth_required": "OFTEN_FOR_DOCS",
        "search_capability": "Agency PlanetBids sites",
        "attachment_behavior": "Vendor registration common for packages",
        "api_available": False,
        "reseller_value": "Medium for CA product buys",
        "search_method": "Agency seeds + live_planetbids",
        "portal_behavior": "HTML",
        "m3_status": "PARTIAL_ADAPTER",
    },
    {
        "source_id": "platform_public_purchase",
        "name": "PublicPurchase",
        "tier": "STATE_LOCAL",
        "coverage_state": BLOCKED,
        "products_appear": "Smaller city/county product RFPs",
        "accessibility": "Login wall for open bids observed",
        "auth_required": "LOGIN",
        "search_capability": "Behind auth",
        "attachment_behavior": "Auth",
        "api_available": False,
        "reseller_value": "Blocked for automation — recipe notes only",
        "search_method": "Do not hammer; mark AUTH_REQUIRED",
        "portal_behavior": "AUTH_REQUIRED",
        "m3_status": "AUTH_REQUIRED",
    },
    {
        "source_id": "state_procurement_portals",
        "name": "State procurement / eProcurement portals",
        "tier": "STATE",
        "coverage_state": PARTIAL,
        "products_appear": "Statewide commodity contracts + open bids",
        "accessibility": "Varies wildly by state",
        "auth_required": "MIXED",
        "search_capability": "State-specific",
        "attachment_behavior": "Mixed",
        "api_available": False,
        "reseller_value": "High when BidNet doesn't cover; coverage gaps by state",
        "search_method": "state_matrix + BidNet first; fill STATE_WITHOUT_STATEWIDE_SOURCE gaps",
        "portal_behavior": "Heterogeneous",
        "m3_status": "PARTIAL_VIA_MATRIX",
    },
    {
        "source_id": "local_schools_universities",
        "name": "School districts / universities / authorities",
        "tier": "LOCAL",
        "coverage_state": PARTIAL,
        "products_appear": "IT, furniture, maintenance, lab, fleet",
        "accessibility": "Often via BidNet/Bonfire/PlanetBids",
        "auth_required": "MIXED",
        "search_capability": "Network or agency portal",
        "attachment_behavior": "Gated packages common",
        "api_available": False,
        "reseller_value": "Strong for IT hardware / facilities products",
        "search_method": "Agency seeds + network expansion",
        "portal_behavior": "Platform-dependent",
        "m3_status": "SEED_COVERAGE",
    },
    # Cooperatives
    {
        "source_id": "coop_sourcewell",
        "name": "Sourcewell",
        "tier": "COOPERATIVE",
        "coverage_state": PARTIAL,
        "products_appear": "Fleet, facilities, IT, public-works products via coop RFPs",
        "accessibility": "Open solicitations page public; awarded catalogs separate",
        "auth_required": "OPTIONAL",
        "search_capability": "Open solicitations listing",
        "attachment_behavior": "RFP packages on solicitation pages",
        "api_available": False,
        "reseller_value": "Medium — national coop RFPs; competition from incumbents high",
        "search_method": "live_cooperative on open solicitations URL only",
        "portal_behavior": "Open RFP ≠ piggyback catalog pricing",
        "m3_status": "ADAPTER_PRESENT",
    },
    {
        "source_id": "coop_omnia",
        "name": "OMNIA Partners",
        "tier": "COOPERATIVE",
        "coverage_state": PARTIAL,
        "products_appear": "Public-sector coop product categories",
        "accessibility": "Public contract opportunities when listed",
        "auth_required": "OPTIONAL",
        "search_capability": "Listed opportunities",
        "attachment_behavior": "Per solicitation",
        "api_available": False,
        "reseller_value": "Medium",
        "search_method": "live_cooperative",
        "portal_behavior": "Coop RFP",
        "m3_status": "ADAPTER_PRESENT",
    },
    {
        "source_id": "coop_hgacbuy",
        "name": "HGACBuy",
        "tier": "COOPERATIVE",
        "coverage_state": PARTIAL,
        "products_appear": "Emergency, fleet, public works products",
        "accessibility": "Bid opportunities page",
        "auth_required": "OPTIONAL",
        "search_capability": "Bid opportunities",
        "attachment_behavior": "Per bid",
        "api_available": False,
        "reseller_value": "Medium for TX-adjacent product categories",
        "search_method": "live_cooperative",
        "portal_behavior": "Coop",
        "m3_status": "ADAPTER_PRESENT",
    },
    {
        "source_id": "coop_buyboard",
        "name": "BuyBoard",
        "tier": "COOPERATIVE",
        "coverage_state": PARTIAL,
        "products_appear": "School/local government products",
        "accessibility": "Current solicitations when public",
        "auth_required": "OPTIONAL",
        "search_capability": "Current solicitations ASPX",
        "attachment_behavior": "Per solicitation",
        "api_available": False,
        "reseller_value": "Medium — education-heavy",
        "search_method": "live_cooperative",
        "portal_behavior": "Coop",
        "m3_status": "ADAPTER_PRESENT",
    },
    {
        "source_id": "coop_naspo",
        "name": "NASPO ValuePoint",
        "tier": "COOPERATIVE",
        "coverage_state": PARTIAL,
        "products_appear": "Multi-state coop IT and commodities",
        "accessibility": "Open solicitations page",
        "auth_required": "OPTIONAL",
        "search_capability": "Solicitations listing",
        "attachment_behavior": "RFP packages",
        "api_available": False,
        "reseller_value": "Medium-high for IT hardware categories when open RFP fits",
        "search_method": "live_cooperative",
        "portal_behavior": "Multi-state coop",
        "m3_status": "ADAPTER_PRESENT",
    },
]


def source_coverage_audit() -> dict[str, Any]:
    by_state: dict[str, list[str]] = {}
    for r in SOURCE_RECIPES:
        by_state.setdefault(r["coverage_state"], []).append(r["source_id"])
    return {
        "kind": "PRODUCT_RESALE_SOURCE_COVERAGE_AUDIT",
        "build": BUILD_TAG,
        "updated_at": _utc(),
        "total_recipes": len(SOURCE_RECIPES),
        "by_coverage_state": {k: sorted(v) for k, v in sorted(by_state.items())},
        "counts": {k: len(v) for k, v in by_state.items()},
        "recipes": SOURCE_RECIPES,
        "iowa_seed_excluded_from_primary_validation": True,
        "iowa_seed_id": IOWA_SEED_SOLICITATION,
    }


# ---------------------------------------------------------------------------
# PART 2 — Source ROI (reseller fit, not contract volume)
# ---------------------------------------------------------------------------

def _roi_score(recipe: dict[str, Any]) -> dict[str, Any]:
    """Score 0–5 each dimension → weighted total. No invented dollar economics."""
    dims = {
        "product_purchase_frequency": 0,
        "reseller_suitability": 0,
        "public_accessibility": 0,
        "margin_potential": 0,
        "competition_level_inverse": 0,  # higher = less brutal competition
        "information_availability": 0,
        "cashless_operability": 0,
    }
    sid = recipe["source_id"]
    state = recipe["coverage_state"]

    # Base by known channel character
    presets: dict[str, dict[str, int]] = {
        "fed_sam_contract_opportunities": {
            "product_purchase_frequency": 5,
            "reseller_suitability": 4,
            "public_accessibility": 5,
            "margin_potential": 4,
            "competition_level_inverse": 2,
            "information_availability": 4,
            "cashless_operability": 4,
        },
        "dla_dibbs": {
            "product_purchase_frequency": 5,
            "reseller_suitability": 5,
            "public_accessibility": 1,
            "margin_potential": 5,
            "competition_level_inverse": 3,
            "information_availability": 2,
            "cashless_operability": 3,
        },
        "dla_tdmt": {
            "product_purchase_frequency": 3,
            "reseller_suitability": 4,
            "public_accessibility": 1,
            "margin_potential": 4,
            "competition_level_inverse": 3,
            "information_availability": 2,
            "cashless_operability": 2,
        },
        "network_bidnet_direct": {
            "product_purchase_frequency": 4,
            "reseller_suitability": 4,
            "public_accessibility": 4,
            "margin_potential": 3,
            "competition_level_inverse": 3,
            "information_availability": 2,
            "cashless_operability": 4,
        },
        "gsa_advantage": {
            "product_purchase_frequency": 3,
            "reseller_suitability": 3,
            "public_accessibility": 4,
            "margin_potential": 2,
            "competition_level_inverse": 2,
            "information_availability": 4,
            "cashless_operability": 2,
        },
        "gsa_ebuy": {
            "product_purchase_frequency": 4,
            "reseller_suitability": 4,
            "public_accessibility": 1,
            "margin_potential": 3,
            "competition_level_inverse": 2,
            "information_availability": 2,
            "cashless_operability": 1,
        },
        "coop_naspo": {
            "product_purchase_frequency": 3,
            "reseller_suitability": 3,
            "public_accessibility": 3,
            "margin_potential": 3,
            "competition_level_inverse": 2,
            "information_availability": 3,
            "cashless_operability": 2,
        },
        "coop_sourcewell": {
            "product_purchase_frequency": 3,
            "reseller_suitability": 3,
            "public_accessibility": 3,
            "margin_potential": 3,
            "competition_level_inverse": 2,
            "information_availability": 3,
            "cashless_operability": 2,
        },
        "nasa_sewp": {
            "product_purchase_frequency": 2,
            "reseller_suitability": 1,
            "public_accessibility": 1,
            "margin_potential": 2,
            "competition_level_inverse": 1,
            "information_availability": 1,
            "cashless_operability": 1,
        },
        "nih_vehicle_style": {
            "product_purchase_frequency": 1,
            "reseller_suitability": 1,
            "public_accessibility": 1,
            "margin_potential": 1,
            "competition_level_inverse": 1,
            "information_availability": 1,
            "cashless_operability": 1,
        },
    }
    dims.update(presets.get(sid, {
        "product_purchase_frequency": 2,
        "reseller_suitability": 2,
        "public_accessibility": 2 if state == ACTIVE else 1,
        "margin_potential": 2,
        "competition_level_inverse": 2,
        "information_availability": 2,
        "cashless_operability": 2 if state in {ACTIVE, PARTIAL} else 1,
    }))

    # Accessibility penalty
    if state == BLOCKED:
        dims["public_accessibility"] = min(dims["public_accessibility"], 1)
        dims["cashless_operability"] = min(dims["cashless_operability"], 2)
    if state == LOW_VALUE:
        dims["reseller_suitability"] = min(dims["reseller_suitability"], 1)
    if state == MISSING:
        dims["information_availability"] = min(dims["information_availability"], 2)

    weights = {
        "product_purchase_frequency": 1.2,
        "reseller_suitability": 1.4,
        "public_accessibility": 1.3,
        "margin_potential": 1.1,
        "competition_level_inverse": 0.8,
        "information_availability": 1.2,
        "cashless_operability": 1.5,
    }
    total = sum(dims[k] * weights[k] for k in dims)
    max_total = 5 * sum(weights.values())
    norm = round(total / max_total, 3)

    if state == BLOCKED and dims["reseller_suitability"] >= 4:
        tier = HIGH_VALUE_SOURCE  # strategically high — access is the gap
        why = "Highest product/reseller fit but automation blocked — value is real, path is SAM-reconciled or human access"
    elif norm >= 0.62 and state in {ACTIVE, PARTIAL}:
        tier = HIGH_VALUE_SOURCE
        why = "Strong product frequency + public access + cashless operability for M3 today"
    elif norm >= 0.42:
        tier = MEDIUM_VALUE_SOURCE
        why = "Useful channel with partial access or higher competition / auth friction"
    else:
        tier = LOW_VALUE_SOURCE
        why = "Weak reseller fit, vehicle-locked, or low public operability for cashless product resale"

    return {
        "source_id": sid,
        "name": recipe["name"],
        "coverage_state": state,
        "roi_tier": tier,
        "normalized_score": norm,
        "dimensions": dims,
        "why": why,
    }


def source_roi_ranking() -> dict[str, Any]:
    ranked = [_roi_score(r) for r in SOURCE_RECIPES]
    ranked.sort(key=lambda x: (-x["normalized_score"], x["source_id"]))
    by_tier: dict[str, list[str]] = {}
    for row in ranked:
        by_tier.setdefault(row["roi_tier"], []).append(row["source_id"])
    return {
        "kind": "PRODUCT_RESALE_SOURCE_ROI",
        "build": BUILD_TAG,
        "ranking_basis": [
            "product_purchase_frequency",
            "reseller_suitability",
            "public_accessibility",
            "margin_potential",
            "competition_level_inverse",
            "information_availability",
            "cashless_operability",
        ],
        "not_ranked_by": "total_contract_volume",
        "by_tier": by_tier,
        "ranked": ranked,
    }


# ---------------------------------------------------------------------------
# PART 3 — Category intelligence profiles
# ---------------------------------------------------------------------------

CATEGORY_PROFILES: list[dict[str, Any]] = [
    {
        "category_id": "it_hardware",
        "name": "IT hardware",
        "common_buyers": ["Federal agencies", "K12", "Universities", "Cities"],
        "common_sources": ["fed_sam_contract_opportunities", "network_bidnet_direct", "coop_naspo", "gsa_ebuy"],
        "solicitation_language": ["laptops", "desktops", "servers", "switches", "brand name or equal", "Cisco", "Dell", "HP"],
        "common_manufacturers": ["Dell", "HP", "Lenovo", "Cisco", "Apple"],
        "common_distributors": ["CDW", "SHI", "Connection", "Insight"],
        "common_restrictions": ["Brand-name-or-equal", "authorized reseller", "TAA", "schedule preference"],
        "resale_fit": "HIGH",
        "notes": "Strong identity + public catalog pricing paths",
    },
    {
        "category_id": "industrial_parts",
        "name": "Industrial parts / NSN",
        "common_buyers": ["DLA", "DoD", "Federal maintenance"],
        "common_sources": ["fed_sam_contract_opportunities", "dla_dibbs", "dla_tdmt"],
        "solicitation_language": ["NSN", "NIIN", "approved source", "CAGE", "P/N", "exact part"],
        "common_manufacturers": ["OEM cage-coded manufacturers"],
        "common_distributors": ["Authorized OEM distributors", "surplus specialists"],
        "common_restrictions": ["Approved source", "QPL", "FAT", "traceability", "JCP"],
        "resale_fit": "HIGH",
        "notes": "Best $10k+ profit density when identity+qty recovered; package access is bottleneck",
    },
    {
        "category_id": "tools",
        "name": "Tools",
        "common_buyers": ["Cities", "Counties", "DOT", "Facilities"],
        "common_sources": ["network_bidnet_direct", "fed_sam_contract_opportunities", "coop_sourcewell"],
        "solicitation_language": ["tool kit", "hand tools", "power tools", "or equal"],
        "common_manufacturers": ["Milwaukee", "DeWalt", "Snap-on", "Klein"],
        "common_distributors": ["Grainger", "Fastenal", "MSC", "Home Depot Pro"],
        "common_restrictions": ["Brand or equal", "warranty"],
        "resale_fit": "MEDIUM_HIGH",
        "notes": "Good cashless distributor relationships",
    },
    {
        "category_id": "safety_equipment",
        "name": "Safety equipment",
        "common_buyers": ["Cities", "Schools", "Industrial agencies"],
        "common_sources": ["network_bidnet_direct", "fed_sam_contract_opportunities"],
        "solicitation_language": ["PPE", "safety", "ANSI", "hard hats", "vests"],
        "common_manufacturers": ["3M", "Honeywell", "MSA"],
        "common_distributors": ["Grainger", "Fastenal", "Uline"],
        "common_restrictions": ["Standards compliance", "brand or equal"],
        "resale_fit": "MEDIUM_HIGH",
        "notes": "Repeatable commodity buys",
    },
    {
        "category_id": "maintenance_supplies",
        "name": "Maintenance supplies",
        "common_buyers": ["Facilities", "Housing authorities", "Universities"],
        "common_sources": ["network_bidnet_direct", "coop_buyboard"],
        "solicitation_language": ["janitorial", "MRO", "consumables", "annual supply"],
        "common_manufacturers": ["Diverse", "commodity brands"],
        "common_distributors": ["Grainger", "HD Supply", "Fastenal"],
        "common_restrictions": ["Green products", "delivery schedule"],
        "resale_fit": "MEDIUM",
        "notes": "Often IDC/annual — distinguish guaranteed min vs max",
    },
    {
        "category_id": "fleet_parts",
        "name": "Fleet parts",
        "common_buyers": ["Cities", "Transit", "Counties", "DLA"],
        "common_sources": ["network_bidnet_direct", "fed_sam_contract_opportunities", "coop_hgacbuy"],
        "solicitation_language": ["OEM part", "aftermarket", "fleet", "vehicle parts"],
        "common_manufacturers": ["OEM vehicle makers", "tier-1 suppliers"],
        "common_distributors": ["Authorized dealers", "fleet specialists"],
        "common_restrictions": ["OEM required vs or-equal", "core returns"],
        "resale_fit": "HIGH",
        "notes": "Strong when OEM P/N explicit",
    },
    {
        "category_id": "road_maintenance",
        "name": "Road maintenance products",
        "common_buyers": ["DOT", "Counties", "Cities"],
        "common_sources": ["network_bidnet_direct", "state_procurement_portals"],
        "solicitation_language": ["asphalt", "signage", "guardrail", "deicer", "paint"],
        "common_manufacturers": ["Specialty materials vendors"],
        "common_distributors": ["Regional materials distributors"],
        "common_restrictions": ["DOT approved materials lists"],
        "resale_fit": "MEDIUM",
        "notes": "Iowa seed is isolated historical case — not primary validation",
    },
    {
        "category_id": "hvac",
        "name": "HVAC equipment",
        "common_buyers": ["Facilities", "Schools", "Federal buildings"],
        "common_sources": ["network_bidnet_direct", "fed_sam_contract_opportunities", "coop_sourcewell"],
        "solicitation_language": ["RTU", "chiller", "HVAC", "install sometimes bundled"],
        "common_manufacturers": ["Carrier", "Trane", "Lennox", "Daikin"],
        "common_distributors": ["Manufacturer reps", "Johnstone", "Baker"],
        "common_restrictions": ["Install/service bundling can kill pure resale"],
        "resale_fit": "MEDIUM",
        "notes": "Prefer supply-only CLINs",
    },
    {
        "category_id": "electrical_supplies",
        "name": "Electrical supplies",
        "common_buyers": ["Facilities", "Utilities", "DLA"],
        "common_sources": ["fed_sam_contract_opportunities", "network_bidnet_direct"],
        "solicitation_language": ["wire", "panel", "breaker", "conduit", "NSN electrical"],
        "common_manufacturers": ["Eaton", "Schneider", "ABB", "Leviton"],
        "common_distributors": ["Graybar", "Rexel", "CED"],
        "common_restrictions": ["UL", "approved manufacturer"],
        "resale_fit": "HIGH",
        "notes": "Good distributor quote paths",
    },
    {
        "category_id": "pumps_motors",
        "name": "Pumps / motors",
        "common_buyers": ["DLA", "Utilities", "Facilities"],
        "common_sources": ["fed_sam_contract_opportunities", "dla_dibbs"],
        "solicitation_language": ["pump", "motor", "NSN", "rotary", "centrifugal"],
        "common_manufacturers": ["OEM cage-coded"],
        "common_distributors": ["Authorized OEM distributors"],
        "common_restrictions": ["Exact NSN", "approved source", "FAT"],
        "resale_fit": "HIGH",
        "notes": "SAM noticedesc often yields NSN+qty for DLA",
    },
    {
        "category_id": "generators",
        "name": "Generators",
        "common_buyers": ["Emergency management", "Facilities", "Military"],
        "common_sources": ["fed_sam_contract_opportunities", "network_bidnet_direct", "coop_hgacbuy"],
        "solicitation_language": ["generator", "genset", "kW", "diesel"],
        "common_manufacturers": ["Cummins", "Caterpillar", "Generac", "Kohler"],
        "common_distributors": ["Dealer networks"],
        "common_restrictions": ["Install", "fuel", "emissions"],
        "resale_fit": "MEDIUM_HIGH",
        "notes": "Watch service/install bundling",
    },
    {
        "category_id": "facility_products",
        "name": "Facility products",
        "common_buyers": ["Cities", "Universities", "Federal facilities"],
        "common_sources": ["network_bidnet_direct", "coop_omnia"],
        "solicitation_language": ["furniture", "flooring", "fixtures", "supply"],
        "common_manufacturers": ["Varied"],
        "common_distributors": ["Facility supply houses"],
        "common_restrictions": ["Delivery/install"],
        "resale_fit": "MEDIUM",
        "notes": "Filter out construction-dominant",
    },
    {
        "category_id": "medical_equipment",
        "name": "Medical equipment",
        "common_buyers": ["VA", "Public hospitals", "Universities"],
        "common_sources": ["fed_sam_contract_opportunities", "network_bidnet_direct"],
        "solicitation_language": ["medical device", "FDA", "brand name"],
        "common_manufacturers": ["OEM device makers"],
        "common_distributors": ["Authorized medical distributors"],
        "common_restrictions": ["FDA", "authorized distributor", "service contracts"],
        "resale_fit": "MEDIUM",
        "notes": "Auth/service friction higher",
    },
    {
        "category_id": "laboratory_supplies",
        "name": "Laboratory supplies",
        "common_buyers": ["Universities", "Public health", "Federal labs"],
        "common_sources": ["fed_sam_contract_opportunities", "network_bidnet_direct"],
        "solicitation_language": ["lab", "reagents", "consumables", "catalog"],
        "common_manufacturers": ["Thermo", "VWR brands", "Eppendorf"],
        "common_distributors": ["VWR", "Fisher Scientific", "Thomas Scientific"],
        "common_restrictions": ["Catalog pricing", "cold chain"],
        "resale_fit": "MEDIUM_HIGH",
        "notes": "Catalog identity helps",
    },
    {
        "category_id": "tactical_non_weapon",
        "name": "Tactical / non-weapon equipment",
        "common_buyers": ["Federal LE", "DHS", "DoD"],
        "common_sources": ["fed_sam_contract_opportunities"],
        "solicitation_language": ["tactical", "armor (non-weapon)", "optics mounts", "kits"],
        "common_manufacturers": ["Specialty tactical OEMs"],
        "common_distributors": ["Authorized LE distributors"],
        "common_restrictions": ["Eligibility", "export", "LE only"],
        "resale_fit": "MEDIUM",
        "notes": "Eligibility can block cashless outsiders",
    },
    {
        "category_id": "furniture",
        "name": "Furniture",
        "common_buyers": ["Schools", "Cities", "Federal"],
        "common_sources": ["network_bidnet_direct", "coop_buyboard", "gsa_advantage"],
        "solicitation_language": ["furniture", "workstation", "seating"],
        "common_manufacturers": ["Herman Miller", "Steelcase", "Hon"],
        "common_distributors": ["Contract furniture dealers"],
        "common_restrictions": ["Install", "design services"],
        "resale_fit": "MEDIUM",
        "notes": "Install bundling common",
    },
    {
        "category_id": "containers_storage",
        "name": "Containers / storage",
        "common_buyers": ["DLA", "Cities", "Facilities"],
        "common_sources": ["fed_sam_contract_opportunities", "network_bidnet_direct"],
        "solicitation_language": ["container", "cabinet", "storage", "bin"],
        "common_manufacturers": ["Commodity / specialty"],
        "common_distributors": ["Uline", "Grainger", "Global"],
        "common_restrictions": ["Spec dimensions", "delivery"],
        "resale_fit": "MEDIUM_HIGH",
        "notes": "Clear catalog matches",
    },
]


def category_intelligence() -> dict[str, Any]:
    return {
        "kind": "PRODUCT_RESALE_CATEGORY_INTELLIGENCE",
        "build": BUILD_TAG,
        "profiles": CATEGORY_PROFILES,
        "high_resale_fit": [c["category_id"] for c in CATEGORY_PROFILES if c["resale_fit"] == "HIGH"],
    }


def infer_category(row: dict[str, Any]) -> str | None:
    blob = f"{row.get('title') or ''} {row.get('description') or ''}".lower()
    best: tuple[int, str] | None = None
    for c in CATEGORY_PROFILES:
        for term in c.get("solicitation_language") or []:
            t = str(term).lower()
            if t and t in blob:
                score = len(t)
                # Prefer specific equipment terms over generic NSN/P/N tokens
                if t in {"nsn", "niin", "p/n", "part number", "supply", "equipment"}:
                    score -= 20
                if best is None or score > best[0]:
                    best = (score, c["category_id"])
    return best[1] if best else None


# ---------------------------------------------------------------------------
# PART 4 — Historical price paths (methods, not bulk prices)
# ---------------------------------------------------------------------------

PRICE_PATH_RECIPES: list[dict[str, Any]] = [
    {
        "path_id": "nsn_usaspending_vendor_catalog",
        "when": "Exact NSN/NIIN known",
        "steps": [
            "NSN found on solicitation/description",
            "Query USAspending awards mentioning NSN or related PIID",
            "Extract historical unit prices when line-item evidence exists",
            "Identify awardee / vendor names (not invent)",
            "Search vendor public catalog or authorized distributor for current offerability",
            "Mark economics UNKNOWN until supplier quote or verified public price",
        ],
        "sources": ["USAspending", "SAM award notices", "vendor catalog pages"],
        "never": ["Fabricate unit prices", "Treat award ceiling as expected revenue"],
    },
    {
        "path_id": "oem_pn_gsa_distributor",
        "when": "OEM + part number known",
        "steps": [
            "Confirm OEM + P/N from evidence",
            "Check GSA Advantage / schedule public prices if listed",
            "Check manufacturer public list / MAP pages if any",
            "Identify authorized distributors from manufacturer site (no scrape of full catalogs)",
            "Queue human/supplier quote for acquisition cost",
        ],
        "sources": ["GSA Advantage", "manufacturer pages", "distributor pages"],
        "never": ["Store entire catalogs", "Invent distributor quotes"],
    },
    {
        "path_id": "brand_or_equal_comparable",
        "when": "Brand-name-or-equal without exact alternate",
        "steps": [
            "Capture brand + model as evidence-backed identity",
            "Find public commercial comparables at LEVEL_2 confidence only",
            "Do not claim exact match without evidence",
            "Prefer supplier quote path for bid economics",
        ],
        "sources": ["public commercial listings", "distributor search"],
        "never": ["Upgrade comparable to exact identity without evidence"],
    },
]


def price_path_intelligence() -> dict[str, Any]:
    return {
        "kind": "PRODUCT_RESALE_PRICE_PATH_INTELLIGENCE",
        "build": BUILD_TAG,
        "paths": PRICE_PATH_RECIPES,
        "note": "Methods only — no bulk price retrieval in this build",
    }


def select_price_path(row: dict[str, Any]) -> dict[str, Any]:
    struct = row.get("dla_product_structure") or {}
    if row.get("exact_nsn") or struct.get("nsn") or struct.get("has_exact_nsn"):
        path = next(p for p in PRICE_PATH_RECIPES if p["path_id"] == "nsn_usaspending_vendor_catalog")
    elif row.get("exact_part_number") or struct.get("part_number") or struct.get("has_exact_pn"):
        path = next(p for p in PRICE_PATH_RECIPES if p["path_id"] == "oem_pn_gsa_distributor")
    else:
        path = next(p for p in PRICE_PATH_RECIPES if p["path_id"] == "brand_or_equal_comparable")
    return {"selected_path_id": path["path_id"], "path": path, "economics_invented": False}


# ---------------------------------------------------------------------------
# PART 5 — Supplier knowledge (compact reusable records)
# ---------------------------------------------------------------------------

SUPPLIER_KNOWLEDGE_SEED: list[dict[str, Any]] = [
    {
        "supplier_id": "cdw",
        "name": "CDW",
        "categories": ["it_hardware"],
        "products": ["laptops", "networking", "servers"],
        "government_sales_evidence": "Frequent Federal/state IT awardee (public awards)",
        "quote_requirements": "Business account; government desk common",
        "geography": "US national",
        "terms_verified": False,
    },
    {
        "supplier_id": "grainger",
        "name": "Grainger",
        "categories": ["tools", "safety_equipment", "maintenance_supplies", "electrical_supplies"],
        "products": ["MRO", "PPE", "tools"],
        "government_sales_evidence": "Broad public-sector MRO supplier",
        "quote_requirements": "Account; GSA schedule presence for some lines",
        "geography": "US national",
        "terms_verified": False,
    },
    {
        "supplier_id": "fastenal",
        "name": "Fastenal",
        "categories": ["tools", "maintenance_supplies", "fleet_parts"],
        "products": ["fasteners", "MRO", "fleet supplies"],
        "government_sales_evidence": "Public-sector branch network",
        "quote_requirements": "Local branch quote",
        "geography": "US national / local branches",
        "terms_verified": False,
    },
    {
        "supplier_id": "graybar",
        "name": "Graybar",
        "categories": ["electrical_supplies"],
        "products": ["electrical distribution products"],
        "government_sales_evidence": "Utility/government electrical supply",
        "quote_requirements": "Account / project quote",
        "geography": "US national",
        "terms_verified": False,
    },
    {
        "supplier_id": "shi",
        "name": "SHI International",
        "categories": ["it_hardware"],
        "products": ["enterprise IT"],
        "government_sales_evidence": "Frequent public IT awards",
        "quote_requirements": "Public sector team",
        "geography": "US national",
        "terms_verified": False,
    },
]


def supplier_knowledge_bundle() -> dict[str, Any]:
    return {
        "kind": "PRODUCT_RESALE_SUPPLIER_KNOWLEDGE",
        "build": BUILD_TAG,
        "suppliers": SUPPLIER_KNOWLEDGE_SEED,
        "storage_rule": "Compact knowledge only — no catalogs scraped or stored",
        "fabricated_matches_forbidden": True,
    }


def suppliers_for_category(category_id: str | None) -> list[dict[str, Any]]:
    if not category_id:
        return []
    return [s for s in SUPPLIER_KNOWLEDGE_SEED if category_id in (s.get("categories") or [])]


# ---------------------------------------------------------------------------
# PART 6 — Discovery gap diagnostic
# ---------------------------------------------------------------------------

def diagnose_discovery_gaps(
    *,
    coverage: dict[str, Any] | None = None,
    roi: dict[str, Any] | None = None,
    enrichment_metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    coverage = coverage or source_coverage_audit()
    roi = roi or source_roi_ranking()
    enrichment_metrics = enrichment_metrics or {}

    counts = coverage.get("counts") or {}
    high = (roi.get("by_tier") or {}).get(HIGH_VALUE_SOURCE) or []
    blocked_high = [
        r["source_id"]
        for r in roi.get("ranked") or []
        if r["roi_tier"] == HIGH_VALUE_SOURCE and r["coverage_state"] == BLOCKED
    ]

    gaps = [
        {
            "gap_class": GAP_D_DOCUMENTS,
            "severity": "CRITICAL",
            "summary": "Public package/attachment body recovery still weak vs description recovery",
            "evidence": {
                "descriptions_recovered_sample": enrichment_metrics.get("descriptions_recovered"),
                "packages_recovered_sample": enrichment_metrics.get("packages_recovered"),
            },
            "leverage": "Unlocks qty/OEM/restrictions for BidNet+SAM product deals already found",
        },
        {
            "gap_class": GAP_A_SOURCES,
            "severity": "HIGH",
            "summary": "GSA eBuy / Advantage not operational as discovery; DIBBS blocked",
            "evidence": {
                "missing": counts.get(MISSING, 0),
                "blocked": counts.get(BLOCKED, 0),
                "blocked_high_value": blocked_high,
            },
            "leverage": "Adds schedule RFQ + DLA-native density beyond SAM index",
        },
        {
            "gap_class": GAP_C_PRODUCT_ID,
            "severity": "HIGH",
            "summary": "Many notices still lack exact identity until description/package recovery",
            "evidence": {"note": "Title-only BidNet/SAM listings under-identify products"},
            "leverage": "Identity is prerequisite for pricing/supplier engines",
        },
        {
            "gap_class": GAP_F_ECONOMICS,
            "severity": "HIGH",
            "summary": "Historical/supplier economics path exists but not yet systematically applied to new research-ready set",
            "evidence": {"commercial_research_ready_sample": enrichment_metrics.get("commercial_research_ready")},
            "leverage": "Converts research-ready into $10k+ profit candidates",
        },
        {
            "gap_class": GAP_B_SEARCH,
            "severity": "MEDIUM",
            "summary": "State portal gaps where BidNet absent; coop adapters partial",
            "evidence": {"partial": counts.get(PARTIAL, 0)},
            "leverage": "Fills geographic holes — secondary to document+economics",
        },
        {
            "gap_class": GAP_E_SUPPLIERS,
            "severity": "MEDIUM",
            "summary": "Reusable supplier seeds exist; not auto-linked at scale without identity",
            "evidence": {"supplier_seeds": len(SUPPLIER_KNOWLEDGE_SEED)},
            "leverage": "After identity — quote path acceleration",
        },
    ]

    # Highest leverage: documents/package recovery enabling economics on already-discovered universe
    biggest = gaps[0]
    if enrichment_metrics.get("packages_recovered") == 0 and int(enrichment_metrics.get("descriptions_recovered") or 0) > 0:
        biggest = gaps[0]
    elif blocked_high:
        # If description recovery already solved for SAM, still documents; keep D
        biggest = gaps[0]

    return {
        "kind": "PRODUCT_RESALE_DISCOVERY_GAP_DIAGNOSTIC",
        "build": BUILD_TAG,
        "gaps": gaps,
        "biggest_remaining_capability_gap": biggest,
        "high_value_sources": high,
        "answer_to_core_question": (
            "M3 searches enough HIGH-VALUE places for a 3–5 deal/month hunt (SAM + BidNet + DLA-via-SAM + coop recipes), "
            "but does not yet convert enough found opportunities into document-complete, economically verified product "
            "transactions — especially public package recovery and systematic historical/supplier pricing on research-ready records."
        ),
    }


# ---------------------------------------------------------------------------
# Persistence — recipes only
# ---------------------------------------------------------------------------

def build_persisted_payload(
    *,
    enrichment_metrics: dict[str, Any] | None = None,
    live_validation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    coverage = source_coverage_audit()
    roi = source_roi_ranking()
    cats = category_intelligence()
    prices = price_path_intelligence()
    suppliers = supplier_knowledge_bundle()
    gaps = diagnose_discovery_gaps(coverage=coverage, roi=roi, enrichment_metrics=enrichment_metrics)
    return {
        "kind": "PRODUCT_RESALE_SOURCE_INTELLIGENCE_BUNDLE",
        "build": BUILD_TAG,
        "updated_at": _utc(),
        "persists": [
            "source_recipes",
            "category_intelligence",
            "price_path_methods",
            "supplier_knowledge",
            "gap_diagnostic",
            "roi_model",
        ],
        "does_not_persist": [
            "bulk_documents",
            "giant_solicitation_archives",
            "temporary_search_result_dumps",
        ],
        "coverage_audit": {
            "counts": coverage["counts"],
            "by_coverage_state": coverage["by_coverage_state"],
        },
        "recipes_compact": [
            {
                "source_id": r["source_id"],
                "coverage_state": r["coverage_state"],
                "auth_required": r["auth_required"],
                "api_available": r["api_available"],
                "m3_status": r["m3_status"],
                "search_method": r["search_method"],
            }
            for r in SOURCE_RECIPES
        ],
        "roi": {"by_tier": roi["by_tier"], "top": roi["ranked"][:8]},
        "categories": cats,
        "price_paths": prices,
        "suppliers": suppliers,
        "gaps": gaps,
        "live_validation_summary": {
            "raw_scanned": (live_validation or {}).get("raw_scanned"),
            "product_candidates": (live_validation or {}).get("product_candidates"),
            "top20_count": len((live_validation or {}).get("top20") or []),
        }
        if live_validation
        else None,
        "preserved_states": [
            "TRANSACTIONAL_RESALE_DISCOVERY_OPERATIONAL",
            "TRANSACTIONAL_PROCUREMENT_INTELLIGENCE_OPERATIONAL",
            "ON_DEMAND_PROCUREMENT_INTELLIGENCE_OPERATIONAL",
            "EXECUTABLE_DEAL_PIPELINE_OPERATIONAL",
            "FEDERAL_DLA_PRODUCT_INTELLIGENCE_OPERATIONAL_WITH_MEASURED_ACCESS_GAPS",
        ],
        "next_state": "PRODUCT_RESALE_DISCOVERY_INTELLIGENCE_EXPANDED",
    }


def save_source_intelligence(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or build_persisted_payload()
    # File artifact (always)
    try:
        from pathlib import Path

        art = Path(__file__).resolve().parent / "artifacts" / "m3_product_resale_source_intelligence.json"
        art.parent.mkdir(parents=True, exist_ok=True)
        art.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        payload["artifact_path"] = str(art)
    except Exception as exc:  # noqa: BLE001
        payload["artifact_error"] = str(exc)[:200]
    # Compact AppSetting — recipes/knowledge only
    try:
        from database import SessionLocal
        from models import AppSetting

        compact = {
            k: payload.get(k)
            for k in (
                "kind",
                "build",
                "updated_at",
                "persists",
                "does_not_persist",
                "coverage_audit",
                "recipes_compact",
                "roi",
                "categories",
                "price_paths",
                "suppliers",
                "gaps",
                "live_validation_summary",
                "preserved_states",
                "next_state",
            )
        }
        raw = json.dumps(compact, default=str)
        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == SOURCE_INTEL_SETTINGS_KEY).one_or_none()
            if row:
                row.value = raw
            else:
                db.add(AppSetting(key=SOURCE_INTEL_SETTINGS_KEY, value=raw))
            db.commit()
            payload["durable_saved"] = True
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001
        payload["durable_saved"] = False
        payload["durable_error"] = str(exc)[:200]
    return payload


def load_source_intelligence() -> dict[str, Any]:
    try:
        from database import SessionLocal
        from models import AppSetting

        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == SOURCE_INTEL_SETTINGS_KEY).one_or_none()
            if row and row.value:
                return json.loads(row.value)
        finally:
            db.close()
    except Exception:
        pass
    try:
        from pathlib import Path

        art = Path(__file__).resolve().parent / "artifacts" / "m3_product_resale_source_intelligence.json"
        if art.exists():
            return json.loads(art.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}
