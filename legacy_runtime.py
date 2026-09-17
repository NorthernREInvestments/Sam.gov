"""Legacy Northern RE facilities-service runtime classification + M3-only gate.

Does NOT delete historical code or data. Controls whether legacy UI/runtime
paths are active in production.
"""

from __future__ import annotations

import os
from typing import Any

# Default ON for production M3-only deployment
_ENV = "M3_ONLY_PRODUCTION"


def is_m3_only_production() -> bool:
    raw = (os.environ.get(_ENV) or "true").strip().lower()
    return raw in {"1", "true", "yes", "on"}


# Classification from configuration-separation audit + runtime dependency check
LEGACY_COMPONENT_CLASSIFICATION: dict[str, dict[str, Any]] = {
    "static_main_nav_gos_tabs": {
        "class": "LEGACY_ONLY_SAFE_TO_RETIRE",
        "action": "hidden_in_m3_only_ui",
        "note": "GOS dashboard/today/suppliers tabs mash with M3",
    },
    "static_view_dashboard_contracts": {
        "class": "LEGACY_ONLY_SAFE_TO_RETIRE",
        "action": "hidden_default_not_entry",
        "note": "Facilities-service contracts dashboard; DOM preserved, not entry",
    },
    "static_legacy_settings_naics": {
        "class": "LEGACY_ONLY_SAFE_TO_RETIRE",
        "action": "not_linked_from_m3_nav",
        "note": "NAICS toggles / screening prompt",
    },
    "naics_labels.ALL_NAICS_CODES": {
        "class": "HISTORICAL_DATA_PRESERVE",
        "action": "keep_module_unused_by_m3",
    },
    "settings_store.get_naics_codes": {
        "class": "UNCERTAIN_REQUIRES_SAFE_ISOLATION",
        "action": "keep_for_legacy_apis_m3_does_not_call",
    },
    "sync.sync_from_sam": {
        "class": "UNCERTAIN_REQUIRES_SAFE_ISOLATION",
        "action": "keep_code_hide_ui_controls",
        "note": "Scheduler may still exist; M3 UI does not expose sync buttons",
    },
    "openai_client.DEFAULT_SCREENING_PROMPT": {
        "class": "LEGACY_ONLY_SAFE_TO_RETIRE",
        "action": "not_used_by_m3_path",
    },
    "database_engine_auth_cost_governor": {
        "class": "SHARED_INFRASTRUCTURE_KEEP",
        "action": "keep",
    },
    "m3_pipeline_store": {"class": "M3_REQUIRED_KEEP", "action": "keep"},
    "m3_procurement_profile": {"class": "M3_REQUIRED_KEEP", "action": "keep"},
    "m3_mobile_ui": {"class": "M3_REQUIRED_KEEP", "action": "keep"},
    "product_discovery.PRODUCT_DISCOVERY_NAICS": {
        "class": "M3_REQUIRED_KEEP",
        "action": "metadata_only",
    },
    "gt_contracts_historical_rows": {
        "class": "HISTORICAL_DATA_PRESERVE",
        "action": "preserve_no_delete",
    },
}


def legacy_retirement_snapshot() -> dict[str, Any]:
    by_class: dict[str, list[str]] = {}
    for name, meta in LEGACY_COMPONENT_CLASSIFICATION.items():
        by_class.setdefault(meta["class"], []).append(name)
    return {
        "kind": "M3LegacyRuntimeRetirement",
        "m3_only_production": is_m3_only_production(),
        "by_class": by_class,
        "components": LEGACY_COMPONENT_CLASSIFICATION,
        "active_application": "M3",
        "parent_holding": "Northern RE Investments (capital allocation — not active app)",
    }
