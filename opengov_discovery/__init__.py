"""Authenticated OpenGov platform-family discovery."""

from __future__ import annotations

from opengov_discovery.cascade import run_opengov_cascade_discovery, run_opengov_route_map
from opengov_discovery.harvest import run_opengov_authenticated_discovery
from opengov_discovery.portals import classify_portal_fetch, is_opengov_url, known_opengov_portals
from opengov_discovery.public_client import OpenGovPublicDiscoveryClient
from opengov_discovery.government_directory import OpenGovGovernmentDirectory
from opengov_discovery.public_data_client import OpenGovPublicDataClient
from opengov_discovery.public_document_client import OpenGovPublicDocumentClient
from opengov_discovery.public_harvest import run_opengov_public_discovery
from opengov_discovery.route_resolver import OpenGovRouteResolver

__all__ = [
    "run_opengov_authenticated_discovery",
    "run_opengov_cascade_discovery",
    "run_opengov_route_map",
    "run_opengov_public_discovery",
    "OpenGovPublicDiscoveryClient",
    "OpenGovPublicDataClient",
    "OpenGovPublicDocumentClient",
    "OpenGovGovernmentDirectory",
    "OpenGovRouteResolver",
    "known_opengov_portals",
    "classify_portal_fetch",
    "is_opengov_url",
]
