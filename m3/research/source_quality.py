"""Source purpose classification and active-acquisition gating (Build 002Q)."""

from __future__ import annotations

from typing import Any

from m3.models import SourceRegistry

ACTIVE_PURPOSES = {
    "individual_solicitation_candidate",
    "active_solicitation",
    "open_solicitation",
}
PRICING_PURPOSES = {
    "historical_award",
    "award_history",
    "bid_tabulation",
    "expenditure",
}
FORECAST_PURPOSES = {
    "forecast",
    "procurement_forecast",
    "purchasing_plan",
}
PORTAL_PURPOSES = {
    "portal",
    "procurement_portal",
    "listing",
    "procurement_listing",
    "vendor_registration",
}


def classify_source_purpose(row: SourceRegistry) -> dict[str, Any]:
    """Classify a registry row for acquisition vs pricing research."""
    cfg = dict(row.config or {})
    purpose = str(cfg.get("dataset_purpose") or cfg.get("record_purpose") or "").lower().strip()
    kind = str(cfg.get("dataset_kind") or "").lower()
    name = f"{row.name or ''} {row.source_key or ''} {row.information_available or ''}".lower()
    evidence: list[str] = []

    if cfg.get("acquisition_role") == "pricing_research_only":
        return {
            "purpose": "historical_award",
            "acquisition_role": "pricing_research_only",
            "active_opportunity_ok": False,
            "evidence": ["config.acquisition_role=pricing_research_only"],
        }
    if cfg.get("is_historical") or purpose in PRICING_PURPOSES or "historical" in kind:
        evidence.append("historical_flag_or_kind")
        return {
            "purpose": purpose or "historical_award",
            "acquisition_role": "pricing_research_only",
            "active_opportunity_ok": False,
            "evidence": evidence,
        }
    if cfg.get("is_forecast") or purpose in FORECAST_PURPOSES or "forecast" in kind or "buying plan" in name:
        evidence.append("forecast_or_purchasing_plan")
        return {
            "purpose": purpose or "forecast",
            "acquisition_role": "forecast_only",
            "active_opportunity_ok": False,
            "evidence": evidence,
        }
    if purpose in PORTAL_PURPOSES:
        return {
            "purpose": purpose,
            "acquisition_role": "portal_or_listing",
            "active_opportunity_ok": False,
            "evidence": ["declared_portal_or_listing"],
        }
    sk = (row.source_key or "").lower()
    if (
        "historical" in sk
        or "historical" in name
        or "beginning 20" in name
        or ("award" in name and "open" not in name and "solicitation" not in name)
    ):
        return {
            "purpose": "historical_award",
            "acquisition_role": "pricing_research_only",
            "active_opportunity_ok": False,
            "evidence": ["name_or_key_historical"],
        }
    # City Record / gazette dumps are mixed notices — not individual solicitation packages
    if "city record" in name or "city-record" in sk or "city_record" in sk or "dg92-zbpx" in sk:
        return {
            "purpose": "procurement_listing",
            "acquisition_role": "portal_or_listing",
            "active_opportunity_ok": False,
            "evidence": ["city_record_gazette_listing"],
        }
    # Known employee/portal chrome hosts — listings only, not solicitation packages
    base = f"{row.base_url or ''} {canonical_source_url(row)}".lower()
    if "citydata.mesaaz.gov" in base or "citydata.mesaaz.gov" in sk:
        return {
            "purpose": "procurement_listing",
            "acquisition_role": "portal_or_listing",
            "active_opportunity_ok": False,
            "evidence": ["mesa_employee_data_portal"],
        }
    # Supplier pages are not solicitation acquisition (before operational default)
    if (row.adapter or "") == "supplier_page":
        return {
            "purpose": "supplier_directory",
            "acquisition_role": "supplier_research",
            "active_opportunity_ok": False,
            "evidence": ["adapter:supplier_page"],
        }
    if purpose in ACTIVE_PURPOSES or cfg.get("operational") or row.experimentally_verified:
        return {
            "purpose": purpose or "individual_solicitation_candidate",
            "acquisition_role": "active_opportunity",
            "active_opportunity_ok": True,
            "evidence": ["operational_or_verified_or_declared_active"],
        }
    # Default: allow only free solicitation adapters that are not quarantined
    if cfg.get("quarantine_reason") or cfg.get("operational") is False:
        return {
            "purpose": purpose or "quarantined",
            "acquisition_role": "quarantined",
            "active_opportunity_ok": False,
            "evidence": ["quarantined_or_non_operational"],
        }
    if (row.adapter or "") in {
        "socrata_contracts",
        "georgia_gpr",
        "nebraska_spb",
        "nj_purchase",
        "tn_cpo",
        "uw_procurement",
        "delaware_mmp",
        "erie_ny",
        "worcester_ma",
        "cleveland_oh",
        "jefferson_tx",
        "alaska_opn",
        "tntech",
        "kent_state",
        "tulsa_ok",
        "columbus_ga",
        "providence_ri",
        "sanjuan_usd",
        "pocatello_sd25",
        "danville_va",
        "papillion_plcs",
        "oceanview_ovsd",
        "msu_procurement",
        "indiana_idoa",
        "pa_emarketplace",
        "louisiana_lapac",
    }:
        return {
            "purpose": purpose or "individual_solicitation_candidate",
            "acquisition_role": "active_opportunity",
            "active_opportunity_ok": True,
            "evidence": ["free_solicitation_adapter_default"],
        }
    return {
        "purpose": purpose or "unknown",
        "acquisition_role": "unknown",
        "active_opportunity_ok": False,
        "evidence": ["insufficient_active_evidence"],
    }


def is_active_opportunity_source(row: SourceRegistry) -> bool:
    return bool(classify_source_purpose(row).get("active_opportunity_ok"))


def canonical_source_url(row: SourceRegistry) -> str:
    cfg = row.config or {}
    return str(
        cfg.get("dataset_url")
        or cfg.get("official_listing_url")
        or row.base_url
        or ""
    ).strip().lower()
