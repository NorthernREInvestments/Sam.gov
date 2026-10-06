"""Supplier coverage across line items."""

from __future__ import annotations

from collections import defaultdict
from typing import Any


def compute_supplier_coverage(lines: list[dict[str, Any]]) -> dict[str, Any]:
    """lines[*].suppliers_covering = list of supplier names/ids."""
    total = len(lines)
    per_supplier: dict[str, set[str]] = defaultdict(set)
    per_line_counts: list[dict[str, Any]] = []

    for line in lines:
        lid = str(line.get("line_id") or line.get("clin") or "")
        suppliers = line.get("suppliers_covering") or []
        names = []
        for s in suppliers:
            if isinstance(s, dict):
                name = str(s.get("supplier") or s.get("name") or s.get("id") or "").strip()
            else:
                name = str(s).strip()
            if not name:
                continue
            names.append(name)
            per_supplier[name].add(lid)
        per_line_counts.append({"line_id": lid, "supplier_count": len(names), "suppliers": names})

    ranking = sorted(
        (
            {
                "supplier": name,
                "lines_covered": len(ids),
                "coverage_pct": round((len(ids) / total * 100.0) if total else 0.0, 2),
            }
            for name, ids in per_supplier.items()
        ),
        key=lambda x: -x["lines_covered"],
    )
    best = ranking[0] if ranking else None
    return {
        "total_lines": total,
        "suppliers_ranked": ranking,
        "one_stop_best": best,
        "one_stop_coverage_pct": (best or {}).get("coverage_pct"),
        "per_line": per_line_counts,
    }
