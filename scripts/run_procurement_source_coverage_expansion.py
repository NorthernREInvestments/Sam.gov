"""Run M3 Source Coverage Expansion Sweep #2."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from procurement_source_coverage_expansion import run_coverage_expansion


def main() -> int:
    out = run_coverage_expansion()
    cov = (out.get("reports") or {}).get("coverage") or {}
    before = cov.get("baseline_sweep1") or {}
    et = cov.get("entities_by_type") or {}
    print(
        json.dumps(
            {
                "build": out.get("build"),
                "before": {
                    "portals": before.get("unique_portals"),
                    "entities": before.get("unique_entities"),
                    "opportunities": before.get("unique_opportunities"),
                    "k12": before.get("k12"),
                    "special_district": before.get("special_district"),
                    "county": before.get("county"),
                    "city": before.get("city"),
                },
                "after": {
                    "portals": cov.get("unique_procurement_portals"),
                    "entities": cov.get("unique_government_entities"),
                    "opportunities": cov.get("unique_opportunities_discovered"),
                    "k12": et.get("k12"),
                    "special_district": et.get("special_district"),
                    "county": et.get("county"),
                    "city": et.get("city"),
                    "higher_education": et.get("higher_education"),
                    "airport": et.get("airport"),
                    "transit": et.get("transit"),
                    "utility": et.get("utility"),
                    "housing": et.get("housing"),
                    "library": et.get("library"),
                },
                "sweep2_new": {
                    "portals": cov.get("sweep2_new_portals"),
                    "entities": cov.get("sweep2_new_entities"),
                    "opportunities": cov.get("sweep2_new_opportunities"),
                    "revisits": cov.get("revisits"),
                },
                "access": cov.get("access_breakdown"),
                "artifacts": out.get("artifact_paths"),
            },
            indent=2,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
