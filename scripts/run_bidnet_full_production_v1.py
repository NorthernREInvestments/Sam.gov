"""BidNet full production build.

  python scripts/run_bidnet_full_production_v1.py
  python scripts/run_bidnet_full_production_v1.py --fresh
  python scripts/run_bidnet_full_production_v1.py --full-discovery
  python scripts/run_bidnet_full_production_v1.py --offline
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_dotenv() -> None:
    import os

    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        if k and k not in os.environ:
            os.environ[k] = v.strip().strip('"').strip("'")


def main() -> int:
    _load_dotenv()
    from bidnet_full_production.models import BUILD, REPORT
    from bidnet_full_production.sweep import run_bidnet_full_production_v1
    from m3_data_root import data_path

    fresh = "--fresh" in sys.argv
    full = "--full-discovery" in sys.argv
    offline = "--offline" in sys.argv
    print(f"=== {BUILD} ===", flush=True)
    report = run_bidnet_full_production_v1(
        resume=not fresh,
        run_full_discovery=full,
        skip_live_detail=offline,
    )
    print(f"\nWrote {data_path(REPORT)}", flush=True)
    print(f"BIDNET_PRODUCTION_PASS={report.get('BIDNET_PRODUCTION_PASS')}", flush=True)
    return 0 if report.get("BIDNET_PRODUCTION_PASS") == "YES" else 1


if __name__ == "__main__":
    raise SystemExit(main())
