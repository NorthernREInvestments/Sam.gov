"""Selective stage invalidation from change types."""

from __future__ import annotations

from bidnet_engine.models import STAGES

# Stages that must rerun for each change type (selective, not full cascade).
_INVALIDATE: dict[str, tuple[str, ...]] = {
    "NO_CHANGE": (),
    "NEW_OPPORTUNITY": STAGES,
    "STATUS_CHANGED": ("DETAIL", "PACKAGE", "ELIGIBILITY", "BASKET", "ECONOMICS"),
    "DEADLINE_ONLY": ("BASKET", "ECONOMICS"),  # timing/runway only — not identity/pricing
    "METADATA_CHANGED": ("DETAIL", "ELIGIBILITY"),
    "DETAIL_CHANGED": ("DETAIL", "PACKAGE", "ELIGIBILITY"),
    "DOCUMENT_LIST_CHANGED": ("PACKAGE", "LINES", "IDENTITY", "REVENUE", "ACQUISITION", "QUOTE", "BASKET"),
    "NEW_AMENDMENT": ("DETAIL", "PACKAGE", "ELIGIBILITY", "LINES"),
    "PACKAGE_CHANGED": ("PACKAGE", "ELIGIBILITY", "LINES", "IDENTITY", "REVENUE", "ACQUISITION", "QUOTE", "BASKET"),
    "LINE_RELEVANT_CHANGE": ("LINES", "IDENTITY", "REVENUE", "ACQUISITION", "QUOTE", "BASKET", "ECONOMICS"),
    "CLOSED": ("ECONOMICS",),
    "REOPENED": STAGES,
}


def invalidated_stages(change_type: str) -> list[str]:
    return list(_INVALIDATE.get(change_type, ("DETAIL",)))


def full_rerun_avoided(change_type: str) -> bool:
    stages = invalidated_stages(change_type)
    return len(stages) < len(STAGES)
