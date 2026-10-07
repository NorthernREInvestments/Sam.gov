"""Downstream priority classes for BidNet product candidates."""

from __future__ import annotations

import re
from typing import Any

_CARE = re.compile(
    r"\b(tool|mro|ppe|glove|office|furniture|lighting|plumb|hvac|parts|"
    r"janitorial|paper|electrical|supply|supplies|hardware|safety)\b",
    re.I,
)
_HARD = re.compile(
    r"\b(custom fabrication|design[\-\s]?build|oem[\-\s]?only|"
    r"professional services|installation contract|turnkey)\b",
    re.I,
)


def priority_class(row: dict[str, Any]) -> str:
    score = int(row.get("priority_score") or 40)
    blob = f"{row.get('title') or ''} {row.get('description') or ''}"
    if _CARE.search(blob):
        score += 15
    if _HARD.search(blob):
        score -= 20
    pkg = str(row.get("package_state") or "")
    if pkg.startswith("PACKAGE_ACQUIRED"):
        score += 10
    if pkg == "PACKAGE_REGISTRATION_REQUIRED":
        score -= 5
    deadline = str(row.get("deadline") or "")
    if deadline:
        score += 5
    if score >= 75:
        return "P0_IMMEDIATE"
    if score >= 55:
        return "P1_HIGH"
    if score >= 35:
        return "P2_NORMAL"
    return "P3_LOW"
