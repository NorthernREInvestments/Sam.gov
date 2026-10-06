"""Phase 1 — inventory of BidNet code paths (read-only audit artifact)."""

from __future__ import annotations

from application_clock import now_utc
from bidnet_full_production.models import BUILD

MODULES = {
    "discovery": [
        "bidnet_discovery.harvest.run_bidnet_authenticated_harvest",
        "bidnet_discovery.partitioned.run_bidnet_partitioned_harvest",
        "bidnet_discovery.parse.parse_search_results_html",
        "discovery.bidnet_network",
        "discovery.live_fetchers",
    ],
    "login_session": [
        "bidnet_auth.client.BidNetAuthenticatedClient",
        "bidnet_auth.session_store",
        "bidnet_auth.scheduled",
        "bidnet_auth.telemetry",
    ],
    "detail": [
        "bidnet_discovery.detail.recover_detail",
        "bidnet_recovery.parse_abstract.parse_bidnet_abstract",
        "bidnet_recovery.recover.recover_one",
    ],
    "documents_packages": [
        "bidnet_recovery.free_package_chase.chase_free_package",
        "bidnet_recovery.free_package_batch",
        "package_recovery_sam_budget.recover.recover_bidnet",
        "official_source.batch",
    ],
    "canonical_merge": [
        "phase_l.l23_full_population_funnel.load_store",
        "m3_discovery_service",
        "full_funnel_sweep",
    ],
    "scheduler_jobs": [
        "m3_auth_jobs.start_bidnet_harvest_job",
        "bidnet_auth.scheduled",
    ],
    "ui_api": [
        "app.api_m3_bidnet_auth_status",
        "app.api_m3_bidnet_discovery_harvest",
        "app.api_m3_bidnet_recovery_funnel",
    ],
}

KNOWN_CAPS = {
    "harvest_default_max_results": 100,
    "harvest_default_max_pages": 20,
    "harvest_default_detail_limit": 100,
    "partitioned_default_max_results": 30000,
    "partitioned_max_pages_per_partition": 1200,
    "national_soft_cap_note": "~6-7k unique on national open-bids without state partitions",
    "detail_document_download_cap": 8,
    "app_default_bidnet_max_results": 25000,
}

DUPLICATE_LEGACY = [
    "bidnet_discovery.harvest vs bidnet_discovery.partitioned (two discovery paths)",
    "bidnet_recovery.recover vs bidnet_discovery.detail (overlapping detail parse)",
    "free_package_chase vs package_recovery recover_bidnet",
]


def build_architecture_audit() -> dict:
    return {
        "build": BUILD,
        "kind": "BIDNET_ARCHITECTURE_AUDIT",
        "audited_at": now_utc().isoformat(),
        "modules": MODULES,
        "known_caps": KNOWN_CAPS,
        "duplicate_legacy_paths": DUPLICATE_LEGACY,
        "production_intent": "Use partitioned harvest for full universe; authenticated harvest for account search; "
        "detail via BidNetAuthenticatedClient; merge via L23 canonical store.",
    }
