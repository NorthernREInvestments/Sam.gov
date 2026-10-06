"""Free national discovery expansion — volume access, not profit filtering.

Discovery creates a large live raw universe. Profit-first decides usefulness later.
"""

from __future__ import annotations

from discovery_expansion.coverage import discovery_coverage_dashboard
from discovery_expansion.harvest import run_expansion_harvest
from discovery_expansion.platform_status import platform_family_status_matrix

__all__ = [
    "discovery_coverage_dashboard",
    "run_expansion_harvest",
    "platform_family_status_matrix",
]
