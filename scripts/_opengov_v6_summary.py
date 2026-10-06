"""Pull OpenGov cascade v6 job summary from production."""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
JID = "OGD-7a0921f43086"
OUT = ROOT / "data" / "opengov_cascade_v6_summary.json"


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


def main() -> None:
    _load_dotenv()
    c = httpx.Client(
        base_url="https://samgov-production.up.railway.app",
        timeout=90,
        follow_redirects=True,
    )
    c.post(
        "/api/login",
        json={"email": os.environ["APP_EMAIL"], "password": os.environ["APP_PASSWORD"]},
    ).raise_for_status()
    d = c.get(f"/api/m3/auth-jobs/{JID}").json()
    res = d.get("result") or {}
    blocked = res.get("blocked_entities") or []
    per = res.get("per_entity") or {}
    catalog_like = []
    directory_like = []
    for _k, v in per.items():
        if not isinstance(v, dict):
            continue
        src = (v.get("portal_url") or "")
        # rough: directory portals use /portal/{code}
        name = v.get("entity_name")
        st = v.get("status")
        row = {
            "entity_name": name,
            "status": st,
            "working_route": v.get("working_route"),
            "retrieved_total": v.get("retrieved_total"),
            "portal_url": v.get("portal_url"),
        }
        # Job truncates per_entity; use blocked + status counts primarily
        if st == "RECOVERY_BLOCKED":
            catalog_like.append(row)

    slim = {
        "job_id": JID,
        "status": d.get("status"),
        "attempted": res.get("entities_attempted"),
        "successful": res.get("entities_successful"),
        "portal_status_counts": res.get("portal_status_counts"),
        "route_counts": res.get("route_counts"),
        "raw": res.get("raw_opportunities"),
        "unique": res.get("unique_records"),
        "net_new": res.get("net_new"),
        "existing_enriched": (res.get("canonical_merge") or {}).get("existing_enriched")
        or (res.get("canonical_merge") or {}).get("updated"),
        "pagination_complete_entities": res.get("pagination_complete_entities"),
        "anti_bot_primary_failures": res.get("anti_bot_primary_failures"),
        "anti_bot_recovered_via_fallback": res.get("anti_bot_recovered_via_fallback"),
        "recovery_blocked": res.get("recovery_blocked"),
        "catalog_entities": res.get("catalog_entities"),
        "directory_codes": res.get("directory_codes"),
        "known_entities": res.get("known_entities"),
        "canonical_merge": res.get("canonical_merge"),
        "blocked_entities": blocked,
        "resolver_telemetry": res.get("resolver_telemetry"),
        "vendor_global_search": res.get("vendor_global_search"),
        "per_entity_count": len(per),
    }
    OUT.write_text(json.dumps(slim, indent=2, default=str), encoding="utf-8")
    print(json.dumps({k: slim[k] for k in slim if k != "blocked_entities"}, indent=2, default=str))
    print("blocked:")
    for b in blocked:
        print(
            " ",
            b.get("entity_name"),
            b.get("portal_url"),
            b.get("failure_per_route"),
        )


if __name__ == "__main__":
    main()
