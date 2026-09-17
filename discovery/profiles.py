"""Controlled live discovery profiles — DO NOT auto-run."""

from __future__ import annotations

from discovery.http_client import RequestBudget


PROFILE_TINY = {
    "name": "TINY",
    "max_sources": 5,
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
    "SAM": 0,
    "OpenAI": 0,
    "USAspending": 0,
    "paid": 0,
}

PROFILE_BROAD = {
    "name": "BROAD",
    "max_sources": 25,
    "budget": RequestBudget(
        max_total_requests=80,
        max_requests_per_source=4,
        max_pages_per_source=1,
        max_records_per_source=40,
        max_runtime_seconds=300,
        max_retries=1,
        min_interval_seconds=2.0,
        timeout_seconds=20.0,
    ),
    "max_records_total": 500,
    "fetch_details": False,
    "fetch_documents": False,
    "SAM": 0,
    "OpenAI": 0,
    "USAspending": 0,
    "paid": 0,
}

PROFILE_NATIONAL = {
    "name": "NATIONAL",
    "max_sources": 999,
    "budget": RequestBudget(
        max_total_requests=400,
        max_requests_per_source=6,
        max_pages_per_source=2,
        max_records_per_source=100,
        max_runtime_seconds=1800,
        max_retries=1,
        min_interval_seconds=2.5,
        timeout_seconds=20.0,
    ),
    "max_records_total": 5000,
    "incremental": True,
    "fetch_details": True,  # staged/gated — still subject to should_fetch_detail
    "fetch_documents": False,  # document bodies later stage
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
