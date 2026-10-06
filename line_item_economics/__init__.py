"""Line-item economics + retail profit proof engine.

Multi-line solicitations are priced and evaluated line-by-line, then rolled up.
Never invent missing fields. Unknown lines are never treated as zero cost.
"""

from __future__ import annotations

from line_item_economics.engine import (
    analyze_line_item_economics,
    load_analysis,
    owner_summary,
)
from line_item_economics.coverage_gap import record_source_coverage_gap, source_coverage_gap_report

__all__ = [
    "analyze_line_item_economics",
    "load_analysis",
    "owner_summary",
    "record_source_coverage_gap",
    "source_coverage_gap_report",
]
