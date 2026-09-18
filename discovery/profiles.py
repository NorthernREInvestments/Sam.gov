"""Controlled live discovery profiles — DO NOT auto-run."""

from __future__ import annotations

from discovery.http_client import RequestBudget


PROFILE_TINY = {
    "name": "TINY",
    "max_sources": 5,  # tiny validation mix only
    "all_eligible_sources": False,
    "budget": RequestBudget(
        max_total_requests=25,
        max_requests_per_source=5,
        max_pages_per_source=1,
        max_records_per_source=20,
        max_runtime_seconds=90,
        max_retries=1,
        min_interval_seconds=2.0,
        timeout_seconds=20.0,
    ),
    "max_records_total": 100,
    "fetch_details": False,
    "fetch_documents": False,
    "pagination_exhaust": False,
    "pagination_safety_max_pages": 1,
    "SAM": 0,
    "OpenAI": 0,
    "USAspending": 0,
    "paid": 0,
}

# BROAD = production scheduled discovery — ALL eligible sources, real pagination
# Caps are capacity/safety headroom — NOT discovery targets. Prefer false positives.
PROFILE_BROAD = {
    "name": "BROAD",
    "max_sources": None,  # None = no coverage cap; registry determines eligible set
    "all_eligible_sources": True,
    "budget": RequestBudget(
        max_total_requests=8000,
        max_requests_per_source=120,
        max_pages_per_source=80,  # soft request budget per source; exhaustion stops earlier
        max_records_per_source=5000,
        max_runtime_seconds=5400,
        max_retries=1,
        min_interval_seconds=0.5,
        timeout_seconds=25.0,
    ),
    "max_records_total": 50000,  # capacity headroom, not a target
    "fetch_details": False,
    "fetch_documents": False,
    "pagination_exhaust": True,
    "pagination_safety_max_pages": 120,  # hitting this ⇒ PAGINATION_INCOMPLETE, not SUCCESS
    "SAM": 0,
    "OpenAI": 0,
    "USAspending": 0,
    "paid": 0,
}

# NATIONAL = market bootstrap / catch-up — same full coverage, more page budget
PROFILE_NATIONAL = {
    "name": "NATIONAL",
    "max_sources": None,
    "all_eligible_sources": True,
    "budget": RequestBudget(
        max_total_requests=12000,
        max_requests_per_source=180,
        max_pages_per_source=120,
        max_records_per_source=10000,
        max_runtime_seconds=10800,
        max_retries=1,
        min_interval_seconds=0.4,
        timeout_seconds=25.0,
    ),
    "max_records_total": 50000,
    "incremental": True,
    "bootstrap": True,
    "fetch_details": True,  # staged/gated — still subject to should_fetch_detail
    "fetch_documents": False,
    "pagination_exhaust": True,
    "pagination_safety_max_pages": 200,
    "SAM": 0,
    "OpenAI": 0,
    "USAspending": 0,
    "paid": 0,
}

PROFILES = {
    "tiny": PROFILE_TINY,
    "broad": PROFILE_BROAD,
    "national": PROFILE_NATIONAL,
}


def get_profile(name: str) -> dict:
    return PROFILES[name.lower()]
