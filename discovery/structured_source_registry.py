"""Curated Tier-1 / Tier-2 structured commercial source registry for Phase L.15.

Each entry: domain/dataset or endpoint + field_map + role (LIVE|HISTORY|FORECAST).
Generic adapters consume these — no per-dataset one-off clients.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from discovery.constants import ADAPTER_UNVERIFIED_LIVE
from discovery.structured_adapters import (
    TIER_FRAGILE,
    TIER_OFFICIAL,
    TIER_STABLE,
    TIER_STATIC,
    build_socrata_url,
)

BUILD = "20260928-m3-phase-l16-public-structured-expansion-no-sam-api"
BUILD_L15 = "20260928-m3-phase-l15-tier1-tier2-structured-commercial-expansion"


def _socrata(
    *,
    source_id: str,
    name: str,
    domain: str,
    dataset_id: str,
    field_map: dict[str, Any],
    role: str,
    state_code: str | None = None,
    buyer_type: str = "CITY",
    kind: str = "LOCAL",
    params: dict[str, Any] | None = None,
    tier: str = TIER_STABLE,
    estimated_unique_commercial: str = "medium",
    integration_effort: str = "low",
    commercial_categories: list[str] | None = None,
) -> dict[str, Any]:
    p = dict(params or {})
    p.setdefault("$limit", "400")
    url = build_socrata_url(domain, dataset_id, params=p)
    return {
        "source_id": source_id,
        "name": name,
        "list_url": url,
        "adapter_family": "live_structured",
        "platform_family": "Socrata",
        "adapter_kind": "socrata",
        "domain": domain,
        "dataset_id": dataset_id,
        "field_map": field_map,
        "role": role,
        "kind": kind,
        "state_code": state_code,
        "buyer_type": buyer_type,
        "tier": tier,
        "auth": "PUBLIC_NO_AUTH",
        "data_type": "SOCRATA_JSON",
        "estimated_unique_commercial_value": estimated_unique_commercial,
        "integration_effort": integration_effort,
        "commercial_categories": commercial_categories
        or ["IT", "fleet", "equipment", "MRO", "facility"],
        "adapter_status": ADAPTER_UNVERIFIED_LIVE,
        "validation_candidate": True,
        "defaults": {
            "agency": name,
            "state_code": state_code,
            "buyer_type": buyer_type,
            "jurisdiction": buyer_type,
            "status": "OPEN",
        },
    }


# --- Live solicitation / opportunity structured sources ---
STRUCTURED_LIVE_SOURCES: list[dict[str, Any]] = [
    _socrata(
        source_id="structured_socrata_montgomery_md_solicitations",
        name="Montgomery County MD Solicitations",
        domain="data.montgomerycountymd.gov",
        dataset_id="eeq6-nnwe",
        field_map={
            "title": "description",
            "solicitation_number": "number",
            "status": "status",
            "deadline": "closingdate",
            "open_date": "issuancedate",
            "buyer": ["department", "buyer"],
            "category": "type",
            "live_status_values": ["Active", "Open", "OPEN", "ACTIVE"],
        },
        role="LIVE",
        state_code="MD",
        buyer_type="COUNTY",
        params={"$where": "status='Active'", "$limit": "500"},
        estimated_unique_commercial="high",
        commercial_categories=["IT", "fleet", "equipment", "facility", "safety", "MRO"],
    ),
    _socrata(
        source_id="structured_socrata_delaware_open_bids",
        name="Delaware Open Bids",
        domain="data.delaware.gov",
        dataset_id="2hnj-zwix",
        field_map={
            "title": "contracttitle",
            "solicitation_number": "contractnumber",
            "deadline": "deadlinedate",
            "open_date": "opendate",
            "buyer": "agencycode",
            "detail_url": "bidurl",
            "category": "unspsc",
        },
        role="LIVE",
        state_code="DE",
        buyer_type="STATE",
        kind="STATE",
        params={"$limit": "500", "$order": "deadlinedate DESC"},
        estimated_unique_commercial="high",
        commercial_categories=["IT", "equipment", "facility", "fleet", "MRO"],
    ),
    _socrata(
        source_id="structured_socrata_nyc_city_record",
        name="NYC City Record Online Solicitations",
        domain="data.cityofnewyork.us",
        dataset_id="dg92-zbpx",
        field_map={
            "title": "short_title",
            "solicitation_number": ["pin", "request_id"],
            "deadline": "due_date",
            "open_date": "start_date",
            "buyer": "agency_name",
            "description": ["additional_description_1", "category_description"],
            "category": "category_description",
            "status": "type_of_notice_description",
            "live_status_values": ["Solicitation", "Public Hearing", "Award"],
        },
        role="LIVE",
        state_code="NY",
        buyer_type="CITY",
        # Recent solicitations only — avoid decade-old archive rows
        params={
            "$where": "type_of_notice_description='Solicitation' AND start_date > '2025-01-01T00:00:00.000'",
            "$limit": "400",
            "$order": "start_date DESC",
        },
        estimated_unique_commercial="high",
        commercial_categories=["IT", "fleet", "equipment", "electronics", "facility", "lab"],
    ),
    _socrata(
        source_id="structured_socrata_nyc_mwbe_upcoming",
        name="NYC M/WBE Upcoming Procurements",
        domain="data.cityofnewyork.us",
        dataset_id="ww83-bcks",
        field_map={
            "title": "procurement_name",
            "category": "procurement_industry",
            "description": "mwbe_small_purchase_method",
            "external_id": "procurement_name",
        },
        role="LIVE",
        state_code="NY",
        buyer_type="CITY",
        params={"$limit": "200"},
        estimated_unique_commercial="medium",
        tier=TIER_STABLE,
    ),
    # --- Phase L.16 public structured expansion (no SAM Opportunities API) ---
    _socrata(
        source_id="structured_socrata_la_ramp_open_bids",
        name="Los Angeles RAMP Open Bid Opportunities",
        domain="data.lacity.org",
        dataset_id="hf3r-utnq",
        field_map={
            "title": "title",
            "solicitation_number": "rampid",
            "status": "stagename",
            "deadline": "closedate",
            "open_date": "bidpost",
            "buyer": "department",
            "category": ["category", "type"],
            "detail_url": "url",
            "description": "type",
            "live_status_values": ["Open", "OPEN", "Active", "ACTIVE"],
        },
        role="LIVE",
        state_code="CA",
        buyer_type="CITY",
        params={"$where": "stagename='Open'", "$limit": "500", "$order": "closedate ASC"},
        estimated_unique_commercial="high",
        commercial_categories=["electrical", "IT", "fleet", "MRO", "facility", "tools", "safety"],
    ),
    _socrata(
        source_id="structured_socrata_cook_buying_plan_2026",
        name="Cook County Procurement Buying Plan FY2026",
        domain="datacatalog.cookcountyil.gov",
        dataset_id="cbhc-xvrm",
        field_map={
            "title": "project_description",
            "solicitation_number": ["buying_plan_number", "current_contract_number"],
            "buyer": "department",
            "category": "contract_commodity_category",
            "description": "proposed_procurement_method",
            "open_date": "anticipated_advertise_date",
            "external_id": "buying_plan_number",
        },
        role="LIVE",
        state_code="IL",
        buyer_type="COUNTY",
        params={"$limit": "300"},
        estimated_unique_commercial="medium",
        commercial_categories=["IT", "facility", "fleet", "equipment"],
        # Pipeline / advertise-plan — counted as FUTURE-leaning live discovery when open window
    ),
]

# L.16 append: utility-tagged LADWP rows come through LA RAMP department filter naturally

# --- History / award / PO structured sources ---
STRUCTURED_HISTORY_SOURCES: list[dict[str, Any]] = [
    {
        "source_id": "structured_usaspending_awards",
        "name": "USAspending Awards API",
        "list_url": "https://api.usaspending.gov/api/v2/search/spending_by_award/",
        "adapter_family": "live_structured",
        "platform_family": "USAspending",
        "adapter_kind": "rest_json",
        "role": "HISTORY",
        "kind": "FEDERAL",
        "tier": TIER_OFFICIAL,
        "auth": "PUBLIC_NO_AUTH",
        "data_type": "REST_API",
        "estimated_unique_commercial_value": "high",
        "integration_effort": "low",
        "field_map": {
            "product": "Description",
            "vendor": "Recipient Name",
            "total": "Award Amount",
            "award_date": "Start Date",
            "solicitation_id": "Award ID",
        },
        "implementation_status": "ACTIVE_PARTIAL",
        "note": "Use existing usaspending_client; exact identity queries preferred",
    },
    _socrata(
        source_id="structured_socrata_brla_po",
        name="Baton Rouge Purchase Orders and Contracts",
        domain="data.brla.gov",
        dataset_id="2ung-w7t4",
        field_map={
            "product": "source_doc_desc",
            "buyer": ["dept_name", "cost_center_name"],
            "total": "total_amount",
            "award_date": "input_date",
            "po_number": "source_document",
            "solicitation_id": "requisition_no",
        },
        role="HISTORY",
        state_code="LA",
        buyer_type="CITY",
        params={"$limit": "300", "$order": "input_date DESC"},
        estimated_unique_commercial="medium",
        commercial_categories=["equipment", "MRO", "fleet", "tools"],
    ),
    _socrata(
        source_id="structured_socrata_nyc_discretionary_awards",
        name="NYC Discretionary Contract Awards",
        domain="data.cityofnewyork.us",
        dataset_id="tsb8-3rct",
        field_map={
            "product": ["purpose", "project_name", "title", "description"],
            "vendor": ["vendor_name", "contractor", "recipient"],
            "total": ["amount", "contract_amount", "award_amount"],
            "buyer": ["agency", "agency_name"],
            "award_date": ["start_date", "award_date", "fiscal_year"],
        },
        role="HISTORY",
        state_code="NY",
        buyer_type="CITY",
        params={"$limit": "200"},
        estimated_unique_commercial="medium",
    ),
    _socrata(
        source_id="structured_socrata_montgomery_contracts",
        name="Montgomery County MD Contracts",
        domain="data.montgomerycountymd.gov",
        dataset_id="vmu2-pnrc",
        field_map={
            "product": ["description", "title", "commodity"],
            "vendor": ["vendor", "vendorname", "supplier"],
            "total": ["amount", "contractamount", "value"],
            "buyer": ["department", "agency"],
            "award_date": ["startdate", "awarddate", "effectivedate"],
            "solicitation_id": ["contractnumber", "number"],
        },
        role="HISTORY",
        state_code="MD",
        buyer_type="COUNTY",
        params={"$limit": "200"},
        estimated_unique_commercial="medium",
    ),
    # --- L.16 history / term-contract / utility expansions ---
    _socrata(
        source_id="structured_socrata_chicago_contracts",
        name="City of Chicago Contracts",
        domain="data.cityofchicago.org",
        dataset_id="rsxa-ify5",
        field_map={
            "product": "purchase_order_description",
            "vendor": "vendor_name",
            "buyer": "department",
            "solicitation_id": ["purchase_order_contract_number", "specification_number"],
            "award_date": "approval_date",
            "category": "contract_type",
        },
        role="HISTORY",
        state_code="IL",
        buyer_type="CITY",
        params={"$limit": "300", "$order": "approval_date DESC"},
        estimated_unique_commercial="high",
        commercial_categories=["IT", "software", "equipment", "MRO"],
    ),
    _socrata(
        source_id="structured_socrata_austin_contracts",
        name="City of Austin Contracts",
        domain="datahub.austintexas.gov",
        dataset_id="84ih-p28j",
        field_map={
            "product": "doc_dscr",
            "solicitation_id": "doc_id",
            "buyer": "doc_dept_cd",
            "total": "ma_prch_lmt_am",
            "award_date": "efbgn_dt",
            "vendor": "contract_contact_nm",
        },
        role="HISTORY",
        state_code="TX",
        buyer_type="CITY",
        params={"$limit": "300", "$order": "efbgn_dt DESC"},
        estimated_unique_commercial="high",
        commercial_categories=["chemicals", "MRO", "facility", "equipment", "IT"],
    ),
    _socrata(
        source_id="structured_socrata_king_county_contracts",
        name="King County Procurement Contracts",
        domain="data.kingcounty.gov",
        dataset_id="dqit-zt74",
        field_map={
            "product": "description",
            "vendor": "vendor_supplier_name",
            "solicitation_id": "contract",
            "buyer": ["agency", "site"],
            "total": ["not_to_exceed", "spend_to_date"],
            "award_date": "start_date",
            "category": "type",
        },
        role="HISTORY",
        state_code="WA",
        buyer_type="COUNTY",
        params={"$limit": "250"},
        estimated_unique_commercial="medium",
        commercial_categories=["facility", "equipment", "MRO", "fleet"],
    ),
    _socrata(
        source_id="structured_socrata_richmond_contracts",
        name="Richmond VA City Contracts",
        domain="data.richmondgov.com",
        dataset_id="xqn7-jvv2",
        field_map={
            "product": "description",
            "vendor": "supplier",
            "solicitation_id": "contract_number",
            "buyer": "agency_department",
            "total": "contract_value",
            "award_date": "effective_from",
            "category": ["procurement_type", "type_of_solicitation"],
        },
        role="HISTORY",
        state_code="VA",
        buyer_type="CITY",
        params={"$limit": "250"},
        estimated_unique_commercial="high",
        commercial_categories=["utility", "electrical", "equipment", "MRO", "fleet"],
    ),
    _socrata(
        source_id="structured_socrata_de_central_contract_spend",
        name="Delaware Statewide Central Contract Spend",
        domain="data.delaware.gov",
        dataset_id="sifm-293u",
        field_map={
            "product": "contract_description",
            "vendor": "vendor_name",
            "solicitation_id": ["short_contract_number", "long_contract_number"],
            "buyer": "gss_gsa_agency",
            "total": "total_spend",
            "award_date": "month_of_spend",
            "category": "spend_group",
        },
        role="HISTORY",
        state_code="DE",
        buyer_type="STATE",
        kind="STATE",
        params={"$limit": "300", "$order": "calendar_year DESC"},
        estimated_unique_commercial="high",
        commercial_categories=["IT", "facility", "fleet", "MRO", "term_contract"],
    ),
    _socrata(
        source_id="structured_socrata_de_coop_spend",
        name="Delaware Cooperative Spend By Vendor",
        domain="data.delaware.gov",
        dataset_id="6a9e-y46r",
        field_map={
            "product": "contract_description",
            "vendor": "vendor",
            "solicitation_id": ["short_contract_number", "long_contract_number"],
            "buyer": "agency",
            "total": "grand_total",
            "award_date": "month_of_spend",
            "category": "spend_group",
        },
        role="HISTORY",
        state_code="DE",
        buyer_type="STATE",
        kind="STATE",
        params={"$limit": "300"},
        estimated_unique_commercial="high",
        commercial_categories=["fleet", "tires", "MRO", "cooperative", "term_contract"],
    ),
    _socrata(
        source_id="structured_socrata_cambridge_contracts",
        name="Cambridge MA Contracts Bid List",
        domain="data.cambridgema.gov",
        dataset_id="gp98-ja4f",
        field_map={
            "product": "contract_title",
            "vendor": "vendor_name",
            "solicitation_id": "contract_id",
            "buyer": "department",
            "award_date": "start_date",
            "category": "procurement_classification",
            "status": "status",
        },
        role="HISTORY",
        state_code="MA",
        buyer_type="CITY",
        params={"$limit": "250", "$order": "start_date DESC"},
        estimated_unique_commercial="medium",
        commercial_categories=["safety", "equipment", "facility", "IT"],
    ),
    _socrata(
        source_id="structured_socrata_tx_tceq_contracts",
        name="Texas TCEQ Current Contracts & Purchase Orders",
        domain="data.texas.gov",
        dataset_id="svjm-sdfz",
        field_map={
            "product": "project_name",
            "vendor": "vendor_name_description",
            "solicitation_id": "po_contract_number",
            "total": "total_amount",
            "award_date": "start_date",
            "buyer": "contract_or_po_2",
            "category": "pcc_code",
        },
        role="HISTORY",
        state_code="TX",
        buyer_type="STATE",
        kind="STATE",
        params={"$limit": "200", "$order": "start_date DESC"},
        estimated_unique_commercial="medium",
        commercial_categories=["IT", "equipment", "services_mixed"],
    ),
]

# Forecast / pipeline (NOT live bid inventory)
STRUCTURED_FORECAST_SOURCES: list[dict[str, Any]] = [
    {
        "source_id": "structured_forecast_placeholder",
        "name": "Federal/State Procurement Forecasts",
        "role": "FUTURE_OPPORTUNITY_WATCH",
        "tier": TIER_STABLE,
        "auth": "PUBLIC_NO_AUTH",
        "note": "Use as FUTURE_OPPORTUNITY_WATCH only — never count as live inventory",
        "implementation_status": "DOCUMENTED",
    }
]

# Free API key / account queue (do NOT register automatically)
FREE_STRUCTURED_ACCESS_QUEUE: list[dict[str, Any]] = [
    {
        "source": "SAM.gov Opportunities API",
        "expected_unlock": "Official federal opportunity search + attachments metadata",
        "cost": "FREE",
        "setup_effort": "low",
        "data_unlocked": "active opportunities, notice metadata, set-asides, agency, solicitation IDs",
        "signup_url": "https://open.gsa.gov/api/get-opportunities-public-api/",
        "priority": 99,
        "status": "SAM_API_PENDING_REPLACEMENT_KEY",
        "note": "Parked for L.16 — do not request/use key or consume quota this phase",
    },
    {
        "source": "DemandStar free account",
        "expected_unlock": "Local solicitation browse behind registration wall",
        "cost": "FREE_ACCOUNT",
        "setup_effort": "medium",
        "data_unlocked": "agency open bids (often gated)",
        "priority": 40,
        "note": "Tier-4 portal; prefer structured open-data first",
    },
    {
        "source": "Public Purchase free account",
        "expected_unlock": "Buyer open-bid lists",
        "cost": "FREE_ACCOUNT",
        "setup_effort": "medium",
        "data_unlocked": "multi-agency network listings",
        "priority": 45,
        "note": "Login-walled browse; parked unless uniquely valuable",
    },
]

PAID_API_QUEUE: list[dict[str, Any]] = [
    {
        "source": "Commercial bid aggregators (GovWin/Bloomberg etc.)",
        "cost": "PAID",
        "likely_unique_coverage": "high_overlap_with_SAM_plus_state",
        "equivalent_public_data_exists": True,
        "recommendation": "Do not purchase while Tier-1/2 public structured coverage expands",
    }
]

# Fragile sources parked under stop-loss
PARKED_FRAGILE_SOURCES: list[dict[str, Any]] = [
    {
        "source": "OpenGov CDN procurement.opengov.com",
        "tier": TIER_FRAGILE,
        "reason": "Cloudflare anti-bot; use agency alternate HTML or open-data",
        "unique_value_lost": "medium_if_no_agency_alternate",
        "status": "PARKED_FRAGILE_SOURCE",
    },
    {
        "source": "Bonfire Hub",
        "tier": TIER_FRAGILE,
        "reason": "JS hub; low live yield; stop-loss applied",
        "unique_value_lost": "low",
        "status": "PARKED_FRAGILE_SOURCE",
    },
    {
        "source": "IonWave PublicPortal",
        "tier": TIER_FRAGILE,
        "reason": "Zero yield / brittle ASP.NET; stop-loss applied",
        "unique_value_lost": "low",
        "status": "PARKED_FRAGILE_SOURCE",
    },
    {
        "source": "BidNet Direct auth history",
        "tier": TIER_FRAGILE,
        "reason": "BIDNET_AUTH_HISTORY_PARKED by owner",
        "unique_value_lost": "high_history_only",
        "status": "BIDNET_AUTH_HISTORY_PARKED",
    },
    {
        "source": "DemandStar browse",
        "tier": TIER_FRAGILE,
        "reason": "Registration wall",
        "unique_value_lost": "medium",
        "status": "PARKED_FRAGILE_SOURCE",
    },
    {
        "source": "Public Purchase browse",
        "tier": TIER_FRAGILE,
        "reason": "Login-walled browse",
        "unique_value_lost": "medium",
        "status": "PARKED_FRAGILE_SOURCE",
    },
]


def all_structured_live_candidates() -> list[dict[str, Any]]:
    """Selection-pool shaped candidates for live_runner."""
    out = []
    for s in STRUCTURED_LIVE_SOURCES:
        out.append(
            {
                "source_id": s["source_id"],
                "name": s["name"],
                "list_url": s["list_url"],
                "adapter_family": "live_structured",
                "platform_family": s.get("platform_family") or "Socrata",
                "kind": s.get("kind") or "LOCAL",
                "state_code": s.get("state_code"),
                "buyer_type": s.get("buyer_type"),
                "adapter_status": s.get("adapter_status") or ADAPTER_UNVERIFIED_LIVE,
                "validation_candidate": True,
                "structured_meta": {
                    "adapter_kind": s.get("adapter_kind"),
                    "field_map": s.get("field_map"),
                    "role": s.get("role"),
                    "defaults": s.get("defaults"),
                    "tier": s.get("tier"),
                    "domain": s.get("domain"),
                    "dataset_id": s.get("dataset_id"),
                },
            }
        )
    return out


def registry_by_source_id() -> dict[str, dict[str, Any]]:
    all_items = list(STRUCTURED_LIVE_SOURCES) + [
        s for s in STRUCTURED_HISTORY_SOURCES if s.get("list_url") and s.get("field_map")
    ]
    return {s["source_id"]: s for s in all_items}


def expansion_queue() -> list[dict[str, Any]]:
    """STRUCTURED_SOURCE_EXPANSION_QUEUE entries."""
    queue = []
    for s in STRUCTURED_LIVE_SOURCES + STRUCTURED_HISTORY_SOURCES:
        queue.append(
            {
                "source": s.get("name") or s.get("source_id"),
                "source_id": s.get("source_id"),
                "tier": s.get("tier") or TIER_STABLE,
                "data_type": s.get("data_type") or s.get("adapter_kind"),
                "access_requirement": s.get("auth") or "PUBLIC_NO_AUTH",
                "estimated_unique_commercial_value": s.get("estimated_unique_commercial_value")
                or s.get("estimated_unique_commercial"),
                "integration_effort": s.get("integration_effort") or "low",
                "current_status": s.get("implementation_status") or "QUEUED_FOR_INTEGRATION",
                "recommended_priority": 1
                if (s.get("estimated_unique_commercial_value") or s.get("estimated_unique_commercial"))
                == "high"
                else 5,
                "role": s.get("role"),
            }
        )
    for p in PARKED_FRAGILE_SOURCES:
        queue.append(
            {
                "source": p["source"],
                "tier": TIER_FRAGILE,
                "data_type": "FRAGILE_WEB",
                "access_requirement": "VARIES",
                "estimated_unique_commercial_value": p.get("unique_value_lost"),
                "integration_effort": "high",
                "current_status": p.get("status"),
                "recommended_priority": 90,
                "park_reason": p.get("reason"),
            }
        )
    # High-value future structured opportunities (not yet wired)
    queue.extend(
        [
            {
                "source": "data.ca.gov / data.ny.gov / data.texas.gov procurement datasets",
                "tier": TIER_STABLE,
                "data_type": "SOCRATA_CKAN",
                "access_requirement": "PUBLIC_NO_AUTH",
                "estimated_unique_commercial_value": "high",
                "integration_effort": "low",
                "current_status": "DISCOVERED_NOT_WIRED",
                "recommended_priority": 10,
            },
            {
                "source": "State term-contract CSV/XLSX exports (NASPO/state catalogs)",
                "tier": TIER_STATIC,
                "data_type": "CSV_XLSX",
                "access_requirement": "PUBLIC_NO_AUTH",
                "estimated_unique_commercial_value": "high",
                "integration_effort": "medium",
                "current_status": "DISCOVERED_NOT_WIRED",
                "recommended_priority": 12,
            },
            {
                "source": "Sourcewell / OMNIA / HGAC structured contract lists",
                "tier": TIER_STATIC,
                "data_type": "HTML_OR_EXPORT",
                "access_requirement": "PUBLIC_NO_AUTH",
                "estimated_unique_commercial_value": "medium",
                "integration_effort": "medium",
                "current_status": "HTML_ACTIVE_STRUCTURED_PARTIAL",
                "recommended_priority": 20,
            },
        ]
    )
    queue.sort(key=lambda x: int(x.get("recommended_priority") or 99))
    return queue


# Minimal 50-state + DC structured coverage matrix (audit snapshot)
def state_structured_matrix() -> list[dict[str, Any]]:
    """All 50 states + DC — structured access audit (not claiming live integration)."""
    from discovery.state_matrix import STATE_MATRIX

    # Known structured open-data hints by state
    structured_hints: dict[str, dict[str, Any]] = {
        "CA": {
            "open_data": "data.ca.gov / data.lacity.org",
            "type": "CKAN/Socrata",
            "live": "yes_la_ramp",
            "awards": "yes",
        },
        "NY": {"open_data": "data.ny.gov / data.cityofnewyork.us", "type": "Socrata", "live": "yes_nyc", "awards": "yes"},
        "TX": {
            "open_data": "data.texas.gov / datahub.austintexas.gov",
            "type": "Socrata",
            "live": "portal_html",
            "awards": "yes_austin_tceq",
        },
        "MD": {
            "open_data": "data.montgomerycountymd.gov / opendata.maryland.gov",
            "type": "Socrata",
            "live": "yes_montgomery",
            "awards": "yes",
        },
        "DE": {"open_data": "data.delaware.gov", "type": "Socrata", "live": "yes_open_bids", "awards": "yes_term_spend"},
        "LA": {"open_data": "data.brla.gov", "type": "Socrata", "live": "no", "awards": "yes_po"},
        "IL": {
            "open_data": "data.cityofchicago.org / datacatalog.cookcountyil.gov",
            "type": "Socrata",
            "live": "yes_cook_buying_plan",
            "awards": "yes_chicago",
        },
        "WA": {
            "open_data": "data.wa.gov / data.kingcounty.gov",
            "type": "Socrata",
            "live": "partial",
            "awards": "yes_king",
        },
        "MA": {
            "open_data": "opendata.mass.gov / data.cambridgema.gov",
            "type": "Socrata/CKAN",
            "live": "partial",
            "awards": "yes_cambridge",
        },
        "CO": {"open_data": "data.colorado.gov", "type": "Socrata", "live": "partial", "awards": "partial"},
        "FL": {"open_data": "floridagov.data.socrata.com", "type": "Socrata", "live": "portal_html", "awards": "partial"},
        "PA": {"open_data": "data.pa.gov", "type": "Socrata", "live": "partial", "awards": "partial"},
        "OH": {"open_data": "data.ohio.gov", "type": "Socrata", "live": "partial", "awards": "partial"},
        "GA": {"open_data": "opendata.georgia.gov", "type": "Socrata", "live": "gpr_html", "awards": "partial"},
        "NC": {"open_data": "data.ncdot.gov / local", "type": "ArcGIS/Socrata", "live": "partial", "awards": "partial"},
        "VA": {
            "open_data": "data.virginia.gov / data.richmondgov.com",
            "type": "Socrata",
            "live": "partial",
            "awards": "yes_richmond_utility",
        },
        "MI": {"open_data": "data.michigan.gov", "type": "Socrata", "live": "partial", "awards": "partial"},
        "MN": {"open_data": "www.data.mn.gov", "type": "Socrata", "live": "partial", "awards": "partial"},
        "OR": {"open_data": "data.oregon.gov", "type": "Socrata", "live": "partial", "awards": "partial"},
        "AZ": {"open_data": "open.az.gov", "type": "Socrata", "live": "partial", "awards": "partial"},
        "DC": {"open_data": "opendata.dc.gov", "type": "ArcGIS/Socrata", "live": "partial", "awards": "yes"},
    }
    rows = []
    for s in STATE_MATRIX:
        code = s.get("state") or s.get("state_code")
        hint = structured_hints.get(code) or {
            "open_data": None,
            "type": "unknown",
            "live": "portal_html_only",
            "awards": "unknown",
        }
        integrated = any(
            x.get("state_code") == code for x in STRUCTURED_LIVE_SOURCES + STRUCTURED_HISTORY_SOURCES
        )
        priority = "HIGH" if hint.get("live", "").startswith("yes") or integrated else (
            "MEDIUM" if hint.get("open_data") else "LOW"
        )
        rows.append(
            {
                "state": code,
                "name": s.get("name"),
                "procurement_portal": s.get("list_url") or s.get("portal_name"),
                "structured_access_type": hint.get("type"),
                "open_data_portal": hint.get("open_data"),
                "live_solicitation_support": hint.get("live"),
                "award_history_support": hint.get("awards"),
                "contract_price_support": "partial" if hint.get("awards") not in {None, "unknown", "no"} else "unknown",
                "buyer_enumeration": "portal",
                "auth_requirement": "PUBLIC_OR_VENDOR_REG",
                "integration_priority": priority,
                "current_implementation_status": "INTEGRATED_L15" if integrated else "AUDIT_ONLY",
            }
        )
    return rows
