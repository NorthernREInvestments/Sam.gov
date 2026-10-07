"""One-shot schedule recovery status."""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://samgov-production.up.railway.app"


def main() -> None:
    out = {}
    for path in (
        "/api/m3/schedule-recovery/status",
        "/api/m3/schedule-recovery/report",
        "/api/m3/env",
    ):
        try:
            req = urllib.request.Request(BASE + path, method="GET")
            with urllib.request.urlopen(req, timeout=60) as resp:
                out[path] = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            out[path] = {"error": str(exc)}
    print(json.dumps(out, indent=2, default=str)[:12000])
    (ROOT / "data" / "m3_schedule_recovery_status_once.json").write_text(
        json.dumps(out, indent=2, default=str), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
