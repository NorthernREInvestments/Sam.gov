"""M3 procurement profile — government product-resale configuration.

Isolated from legacy Northern RE facilities-service acquisition NAICS
(naics_labels.ALL_NAICS_CODES / settings_store.get_naics_codes / SAM sync).

NAICS may appear here only as optional metadata for product sectors —
never as the primary discovery filter for M3.
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

from application_clock import now_utc

PROFILE_KIND = "M3ProcurementProfile"
PROFILE_VERSION = "1.0"
SETTINGS_KEY = "m3_procurement_profile"
DEFAULT_PATH = Path(__file__).resolve().parent / "artifacts" / "m3_procurement_profile.json"

# Legacy service-business NAICS (facilities subcontracting) — M3 must not target these
LEGACY_SERVICE_NAICS: frozenset[str] = frozenset(
    {
        "561720",
        "561210",
        "561730",
        "561710",
        "562111",
        "561790",
        "561740",
        "562119",
        "561439",
        "541930",
        "811192",
        "238220",
        "562910",
        "484210",
        "484110",
        "492110",
        "711320",
        "532490",
        "561422",
    }
)

PRIMARY_PURPOSE = "Government product-resale opportunities"

TARGET_PRODUCT_TYPES: list[str] = [
    "equipment",
    "IT hardware",
    "electronics",
    "tools",
    "parts",
    "materials",
    "commodities",
    "vehicles/equipment",
    "industrial products",
    "facility supplies",
    "safety products",
    "electrical products",
    "HVAC products",
    "plumbing products",
    "agricultural/grounds equipment",
    "office/furniture products",
    "other tangible goods",
]

PROCUREMENT_TYPES: list[str] = [
    "IFB",
    "ITB",
    "RFQ",
    "sealed bids",
    "supply contracts",
    "commodity purchases",
    "equipment purchases",
]

# Primary discovery signals (ordered) — NAICS is NOT primary
DISCOVERY_PRIMARY_SIGNALS: list[str] = [
    "procurement_type",
    "product_category",
    "solicitation_metadata",
    "title_category_evidence",
    "downstream_product_screening",
]

RANKING_CRITERIA: list[str] = [
    "product_fit",
    "opportunity_value",
    "deadline",
    "package_availability",
    "economics_potential",
    "funding_feasibility",
    "compliance_complexity",
    "evidence_confidence",
]

# Explicitly excluded from M3 ranking (legacy acquisition / service-business)
RANKING_EXCLUSIONS: list[str] = [
    "service_business_acquisition_scores",
    "owner_operator_criteria",
    "acquisition_targets",
    "business_age",
    "seller_revenue_bands",
    "employee_counts",
    "seller_signals",
    "legacy_facilities_service_naics_fit",
    "subcontracting_middleman_screening_score",
]


def _product_naics_metadata() -> list[str]:
    """Optional product-sector NAICS metadata — never from legacy get_naics_codes()."""
    try:
        from product_discovery import PRODUCT_DISCOVERY_NAICS

        return list(PRODUCT_DISCOVERY_NAICS)
    except Exception:
        return []


def default_m3_procurement_profile() -> dict[str, Any]:
    product_naics = _product_naics_metadata()
    # Guard: never include legacy service codes even if lists drift
    product_naics = [c for c in product_naics if c not in LEGACY_SERVICE_NAICS]
    return {
        "kind": PROFILE_KIND,
        "version": PROFILE_VERSION,
        "updated_at": now_utc().isoformat(),
        "primary_purpose": PRIMARY_PURPOSE,
        "business_model": "government_product_resale",
        "isolated_from_legacy_acquisition": True,
        "legacy_system": {
            "name": "Northern_RE_facilities_service_subcontracting",
            "naics_source": "naics_labels.ALL_NAICS_CODES / settings_store.get_naics_codes",
            "m3_uses_legacy_naics": False,
            "legacy_naics_codes": sorted(LEGACY_SERVICE_NAICS),
        },
        "target_product_types": list(TARGET_PRODUCT_TYPES),
        "procurement_types": list(PROCUREMENT_TYPES),
        "discovery": {
            "primary_signals": list(DISCOVERY_PRIMARY_SIGNALS),
            "naics_is_primary_filter": False,
            "naics_role": "metadata_only_when_product_sector",
            "product_sector_naics_metadata": product_naics,
            "rejects_legacy_service_naics": True,
        },
        "ranking": {
            "criteria": list(RANKING_CRITERIA),
            "exclusions": list(RANKING_EXCLUSIONS),
            "uses_legacy_screening_score": False,
            "uses_legacy_min_score_threshold": False,
        },
        "ui": {
            "terminology": "M3 government product-resale procurement",
            "hide_legacy_naics_filters": True,
            "hide_service_business_acquisition_copy": True,
        },
        "safety": {
            "DEVELOPMENT_NO_OUTREACH_default": True,
            "no_automatic_external_actions": True,
        },
    }


def _load_override_from_settings() -> dict[str, Any] | None:
    try:
        from database import SessionLocal
        from models import AppSetting

        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == SETTINGS_KEY).one_or_none()
            if not row or not row.value:
                return None
            data = json.loads(row.value)
            return data if isinstance(data, dict) else None
        finally:
            db.close()
    except Exception:
        return None


def _load_override_from_file(path: Path | None = None) -> dict[str, Any] | None:
    p = path or DEFAULT_PATH
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def load_m3_procurement_profile(*, path: Path | None = None) -> dict[str, Any]:
    """Load M3 profile: defaults ← optional file ← optional DB override (shallow merge of known keys)."""
    profile = default_m3_procurement_profile()
    for override in (_load_override_from_file(path), _load_override_from_settings()):
        if not override:
            continue
        for key in (
            "primary_purpose",
            "target_product_types",
            "procurement_types",
            "discovery",
            "ranking",
            "ui",
        ):
            if key in override and override[key] is not None:
                if isinstance(profile.get(key), dict) and isinstance(override[key], dict):
                    profile[key] = {**profile[key], **override[key]}
                else:
                    profile[key] = deepcopy(override[key])
    # Hard invariants — never allow legacy contamination via override
    profile["isolated_from_legacy_acquisition"] = True
    profile["kind"] = PROFILE_KIND
    profile.setdefault("discovery", {})["naics_is_primary_filter"] = False
    profile.setdefault("ranking", {})["uses_legacy_screening_score"] = False
    meta = list(profile.get("discovery", {}).get("product_sector_naics_metadata") or [])
    profile["discovery"]["product_sector_naics_metadata"] = [
        c for c in meta if str(c) not in LEGACY_SERVICE_NAICS
    ]
    profile["updated_at"] = now_utc().isoformat()
    return profile


def save_m3_procurement_profile(profile: dict[str, Any], *, path: Path | None = None) -> Path:
    """Persist profile JSON (artifacts) — does not touch legacy naics_codes settings."""
    p = path or DEFAULT_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = load_m3_procurement_profile()  # start clean
    # Only allow safe field updates from caller
    for key in ("target_product_types", "procurement_types", "ui"):
        if key in profile:
            payload[key] = profile[key]
    payload["updated_at"] = now_utc().isoformat()
    p.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return p


def legacy_naics_codes() -> list[str]:
    """Read legacy service NAICS without importing them into M3 discovery."""
    try:
        from naics_labels import ALL_NAICS_CODES

        return list(ALL_NAICS_CODES)
    except Exception:
        return sorted(LEGACY_SERVICE_NAICS)


def m3_uses_legacy_naics(profile: dict[str, Any] | None = None) -> bool:
    profile = profile or load_m3_procurement_profile()
    meta = set(str(c) for c in (profile.get("discovery") or {}).get("product_sector_naics_metadata") or [])
    if meta & LEGACY_SERVICE_NAICS:
        return True
    if profile.get("discovery", {}).get("naics_is_primary_filter"):
        return True
    if profile.get("ranking", {}).get("uses_legacy_screening_score"):
        return True
    return bool(profile.get("m3_uses_legacy_naics"))


def assert_m3_isolated_from_legacy() -> dict[str, Any]:
    """Runtime/test guard — M3 profile and product NAICS must not equal legacy list."""
    profile = load_m3_procurement_profile()
    legacy = set(legacy_naics_codes())
    product_meta = set(profile["discovery"]["product_sector_naics_metadata"])
    overlap = sorted(product_meta & legacy & LEGACY_SERVICE_NAICS)
    # Also ensure product_discovery allowlist doesn't import settings
    uses_settings = False
    try:
        import product_discovery as pd
        import ast
        from pathlib import Path

        src_path = Path(pd.__file__)
        tree = ast.parse(src_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mod = getattr(node, "module", None) or ""
                names = [a.name for a in getattr(node, "names", [])]
                joined = " ".join([mod] + names)
                if "get_naics_codes" in joined or "naics_labels" in joined or "settings_store" in joined:
                    uses_settings = True
                    break
    except Exception:
        uses_settings = False
    ok = (
        not m3_uses_legacy_naics(profile)
        and not overlap
        and not uses_settings
        and profile["discovery"]["naics_is_primary_filter"] is False
        and profile["isolated_from_legacy_acquisition"] is True
    )
    return {
        "ok": ok,
        "overlap_legacy_service_naics": overlap,
        "product_discovery_imports_legacy_naics": uses_settings,
        "naics_is_primary_filter": profile["discovery"]["naics_is_primary_filter"],
        "profile_kind": profile["kind"],
        "legacy_code_count": len(legacy),
        "product_metadata_naics_count": len(product_meta),
    }


def ranking_uses_legacy_acquisition_signals(opportunity: dict[str, Any]) -> bool:
    """Detect forbidden legacy acquisition keys on a ranking payload."""
    forbidden = {
        "seller_signals",
        "business_age",
        "years_in_business",
        "acquisition_target_score",
        "owner_operator_fit",
        "employee_count_score",
        "service_business_score",
        "screening_score_legacy",
    }
    keys = set(opportunity.keys())
    nested = opportunity.get("rank_components") or opportunity.get("components") or {}
    if isinstance(nested, dict):
        keys |= set(nested.keys())
    return bool(keys & forbidden)


def env_separation_notes() -> dict[str, Any]:
    return {
        "legacy_env_keys": ["NAICS_CODES", "MIN_SCORE_THRESHOLD", "SCHEDULED_NAICS_PER_SYNC"],
        "m3_does_not_read": ["NAICS_CODES", "MIN_SCORE_THRESHOLD"],
        "m3_product_naics_source": "product_discovery.PRODUCT_DISCOVERY_NAICS (code constant)",
        "optional_override": f"gt_app_settings.key={SETTINGS_KEY} or {DEFAULT_PATH.name}",
        "env_naics_codes_present": bool(os.environ.get("NAICS_CODES")),
    }
