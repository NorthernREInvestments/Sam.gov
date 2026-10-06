from __future__ import annotations

import json
import os
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://samgov-production.up.railway.app"


def _load_dotenv() -> None:
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
    c = httpx.Client(base_url=BASE, timeout=45, follow_redirects=True)
    health = c.get("/api/health").json()
    print("build", health.get("build_version"), flush=True)
    c.post(
        "/api/login",
        json={"email": os.environ["APP_EMAIL"], "password": os.environ["APP_PASSWORD"]},
    ).raise_for_status()
    eu = c.get("/api/m3/euna-auth/status").json()
    print(
        "euna",
        json.dumps(
            {
                "status": eu.get("status"),
                "owner_label": eu.get("owner_label"),
                "national_discovery_enabled": eu.get("national_discovery_enabled"),
                "enabled_states": eu.get("enabled_states_display"),
                "counts_toward_national_health": eu.get("counts_toward_national_health"),
            },
            default=str,
        ),
        flush=True,
    )
    cov = c.get("/api/m3/source-coverage/production").json()
    print(
        "coverage",
        json.dumps(
            {
                "kind": cov.get("kind"),
                "national_health_sources": cov.get("national_health_sources"),
                "euna_category": (cov.get("owner") or {}).get("euna", {}).get("coverage_category"),
                "euna_affects": (cov.get("national_source_health") or {}).get("euna_affects_score"),
                "canonical_live": (cov.get("combined") or {}).get("canonical_live"),
            },
            default=str,
        ),
        flush=True,
    )
    rm = c.get("/api/m3/source-coverage/roadmap").json()
    print(
        "roadmap",
        json.dumps(
            {
                "primary_repair": rm.get("primary_repair_target"),
                "next_free": rm.get("next_free_source_after_opengov"),
                "euna": next(
                    (r for r in (rm.get("sources") or []) if r.get("source") == "Euna/Bonfire"),
                    {},
                ),
            },
            default=str,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
