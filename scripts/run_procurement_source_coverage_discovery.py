"""Run M3 Procurement Source Coverage Discovery test — public-first inventory."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from procurement_source_coverage_discovery import run_coverage_discovery


def main() -> int:
    out = run_coverage_discovery(live_probe=True, max_live_probes=35)
    summary = out.get("summary") or {}
    print(
        json.dumps(
            {
                "build": out.get("build"),
                "unique_sources": summary.get("total_unique_procurement_sources"),
                "unique_entities": summary.get("total_unique_buying_entities"),
                "relationships": summary.get("total_entity_source_relationships"),
                "states_covered": summary.get("total_states_covered"),
                "access": summary.get("access_breakdown"),
                "entities_by_type": summary.get("entities_by_type"),
                "probed_ok": summary.get("sources_tested_successfully"),
                "probed_fail": summary.get("sources_tested_failed_or_gated"),
                "artifacts": out.get("artifact_paths"),
            },
            indent=2,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
