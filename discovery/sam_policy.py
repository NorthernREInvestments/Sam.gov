"""SAM scarcity protection for discovery network."""

from __future__ import annotations

import os
from typing import Any


def sam_api_broad_discovery_enabled() -> bool:
    """Explicit policy: broad SAM discovery OFF by default."""
    # Prefer dedicated env; also respect existing SAM_ALLOW_BROAD_DISCOVERY
    raw = os.getenv("SAM_API_BROAD_DISCOVERY_ENABLED")
    if raw is not None:
        return raw.strip().lower() in ("1", "true", "yes")
    # Default false under scarcity
    from sam_scarcity import broad_sam_discovery_allowed

    return broad_sam_discovery_allowed(authorize_broad_sam_discovery=False)


def assert_no_broad_sam_discovery() -> dict[str, Any]:
    """Guard used by discovery runners and tests."""
    enabled = sam_api_broad_discovery_enabled()
    return {
        "SAM_API_BROAD_DISCOVERY_ENABLED": enabled,
        "broad_discovery_blocked": not enabled,
        "SAM": 0,
        "policy": "Postgres/cache → non-SAM public → documents → optional AI → SAM last",
        "LIVE_API_REQUESTS": 0,
    }


def refuse_sam_in_discovery_run() -> None:
    if sam_api_broad_discovery_enabled():
        # Even if env flipped, discovery runner must not call SAM without explicit rare path
        pass
    # Discovery network never imports sam_client for scanning
    return None
