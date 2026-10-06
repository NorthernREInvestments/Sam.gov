"""Autonomous OpenGov authentication for scheduled discovery + recovery."""

from __future__ import annotations

from opengov_auth.client import OpenGovAuthenticatedClient, test_connection
from opengov_auth.config import load_opengov_auth_config
from opengov_auth.scheduled import run_scheduled_opengov_auth_pipeline
from opengov_auth.telemetry import owner_connection_status, startup_health_report

__all__ = [
    "OpenGovAuthenticatedClient",
    "load_opengov_auth_config",
    "owner_connection_status",
    "run_scheduled_opengov_auth_pipeline",
    "startup_health_report",
    "test_connection",
]
