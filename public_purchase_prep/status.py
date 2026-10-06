"""Public Purchase owner status — credentials may not be active yet."""

from __future__ import annotations

import os
from typing import Any


def public_purchase_status() -> dict[str, Any]:
    user = (os.environ.get("PUBLIC_PURCHASE_USERNAME") or "").strip()
    password = (os.environ.get("PUBLIC_PURCHASE_PASSWORD") or "").strip()
    auth_enabled = (os.environ.get("PUBLIC_PURCHASE_AUTH_ENABLED") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    pending = (os.environ.get("PUBLIC_PURCHASE_ACCOUNT_PENDING") or "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    credentials_present = bool(user and password)
    if pending or not credentials_present:
        status = "FREE_PENDING_ACCOUNT"
    elif auth_enabled:
        status = "READY_FOR_ACTIVATION"
    else:
        status = "CONFIGURED_DISABLED"

    return {
        "kind": "PublicPurchaseStatus",
        "platform": "PublicPurchase",
        "coverage_category": "FREE_PENDING_ACCOUNT",
        "status": status,
        "account_pending_approval": pending,
        "credentials_configured": credentials_present,
        "auth_enabled": auth_enabled,
        "discovery_scope": "BROAD_PRODUCT_RESALE",
        "vendor_profile_codes_ignored": True,
        "planned_pipeline": [
            "auth/session",
            "broad_opportunity_search",
            "full_pagination",
            "current_detail_links",
            "documents",
            "canonical_dedupe",
            "product_economics_pipeline",
        ],
        "notes": (
            "Do not assume credentials are active. Architecture prepared; "
            "activate harvest only after vendor account approval."
        ),
    }
