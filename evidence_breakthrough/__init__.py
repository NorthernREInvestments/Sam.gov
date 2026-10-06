"""M3 Evidence Breakthrough — government value + public acquisition cost.

Build: 20261003-m3-evidence-breakthrough-v1
"""

from evidence_breakthrough.batch import run_evidence_breakthrough_stage
from evidence_breakthrough.report import build_completion_report

__all__ = [
    "run_evidence_breakthrough_stage",
    "build_completion_report",
]
