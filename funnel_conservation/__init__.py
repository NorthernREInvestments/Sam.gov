"""M3 Funnel Population Conservation Audit.

Build: 20261004-m3-funnel-conservation-v1

Hard invariant: at every stage, INPUT COUNT == SUM(terminal/next statuses).
"""

from funnel_conservation.audit import run_funnel_conservation_audit
from funnel_conservation.models import BUILD

__all__ = ["BUILD", "run_funnel_conservation_audit"]
