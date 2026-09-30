"""Run M3 real procurement source seed sweep — visit seeds, follow directories."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from procurement_source_seed_sweep import run_seed_sweep


def main() -> int:
    out = run_seed_sweep()
    cov = (out.get("reports") or {}).get("coverage") or {}
    print(
        json.dumps(
            {
                "build": out.get("build"),
                "seeds_loaded": cov.get("seeds_loaded"),
                "seeds_attempted": cov.get("seeds_attempted"),
                "visits": cov.get("visits"),
                "healthy": cov.get("successful_healthy"),
                "degraded": cov.get("degraded"),
                "auth_required": cov.get("auth_required"),
                "blocked_or_unavailable": cov.get("blocked_or_unavailable"),
                "unique_portals": cov.get("unique_procurement_portals"),
                "unique_entities": cov.get("unique_government_entities"),
                "unique_opportunities": cov.get("unique_opportunities_discovered"),
                "newly_discovered": cov.get("newly_discovered_not_explicitly_seeded"),
                "entities_by_type": cov.get("entities_by_type"),
                "access": cov.get("access_breakdown"),
                "health": cov.get("health_breakdown"),
                "artifacts": out.get("artifact_paths"),
            },
            indent=2,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
