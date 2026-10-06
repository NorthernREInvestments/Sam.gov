"""Skeleton plan for Public Purchase discovery (not a live runner until approved)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PublicPurchaseDiscoveryPlan:
    """Future session-bound discovery contract — NAICS must not define M3 scope."""

    discovery_scope: str = "BROAD_PRODUCT_RESALE"
    vendor_profile_codes_ignored: bool = True
    require_active_credentials: bool = True
    steps: list[str] = field(
        default_factory=lambda: [
            "ensure_authenticated_session",
            "broad_opportunity_search",
            "paginate_all_current_results",
            "recover_detail_links",
            "recover_documents",
            "canonical_dedupe_merge",
            "hand_off_product_economics",
        ]
    )

    def readiness(self, *, credentials_present: bool, account_pending: bool) -> dict[str, Any]:
        if account_pending:
            return {"ready": False, "reason": "ACCOUNT_PENDING_APPROVAL"}
        if not credentials_present:
            return {"ready": False, "reason": "CREDENTIALS_MISSING"}
        return {"ready": True, "reason": None, "steps": list(self.steps)}
