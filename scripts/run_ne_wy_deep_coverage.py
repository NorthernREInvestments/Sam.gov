"""Run Nebraska + Wyoming deep procurement coverage sweep."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ne_wy_deep_coverage import run_ne_wy_deep_coverage


def main() -> int:
    out = run_ne_wy_deep_coverage()
    print(
        json.dumps(
            {
                "build": out.get("build"),
                "state_stats": out.get("state_stats"),
                "artifacts": out.get("artifact_paths"),
            },
            indent=2,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
