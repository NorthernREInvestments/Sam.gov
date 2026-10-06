"""Stage-2 production validation after browser fix — BidNet AUTH_REQUIRED cohort + OpenGov."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=False)
BASE = "https://samgov-production.up.railway.app"
OUT = ROOT / "data" / "auth_production_validation_stage2.json"

TARGET_IDS = [
    "44cfff919d0b34de",
    "53335c3ff214ac32",
    "4706a9ace3aadd41",
    "775f10be98d07352",
    "11c0bc09620b751c",
    "245b9a211b351eaf",
    "5d56659e7d8db873",
    "3fb2c95b555825f1",
    "4aed58fd8b05a79c",
    "18fc084a01db4a42",
]


def main() -> None:
    email = os.environ["APP_EMAIL"]
    password = os.environ["APP_PASSWORD"]
    report: dict = {"started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    with httpx.Client(follow_redirects=True, timeout=1800) as c:
        c.post(f"{BASE}/api/login", json={"email": email, "password": password})

        print("BidNet test-connection (reuse expected)…", flush=True)
        bn1 = c.post(f"{BASE}/api/m3/bidnet-auth/test-connection", json={}).json()
        report["bidnet_auth"] = {
            k: bn1.get(k) for k in ("status", "authenticated", "message", "reused_session")
        }
        print(report["bidnet_auth"], flush=True)

        print("BidNet recovery 10 AUTH_REQUIRED…", flush=True)
        bn10 = c.post(
            f"{BASE}/api/m3/bidnet-recovery/run",
            json={
                "limit": 10,
                "use_auth": True,
                "resume": False,
                "force": True,
                "target_ids": TARGET_IDS,
                "require_auth_blocker": True,
            },
        ).json()
        report["bidnet_10"] = bn10
        st = bn10.get("stats") or {}
        print(
            "processed",
            bn10.get("processed"),
            "org",
            st.get("issuing_org_recovered"),
            "sol",
            st.get("solicitation_number_recovered"),
            "docs",
            st.get("documents_recovered"),
            "detail",
            st.get("detail_recovered"),
            flush=True,
        )

        bn_ok = bool(bn1.get("authenticated"))
        if bn_ok:
            print("BidNet recovery 100…", flush=True)
            bn100 = c.post(
                f"{BASE}/api/m3/bidnet-recovery/run",
                json={
                    "limit": 100,
                    "use_auth": True,
                    "resume": False,
                    "force": True,
                    "require_auth_blocker": True,
                },
            ).json()
            report["bidnet_100"] = bn100
            print("100 processed", bn100.get("processed"), "stats", bn100.get("stats"), flush=True)
        else:
            report["bidnet_100"] = {"skipped": True}

        print("OpenGov test-connection…", flush=True)
        og1 = c.post(f"{BASE}/api/m3/opengov-auth/test-connection", json={}).json()
        report["opengov_auth"] = {
            k: og1.get(k) for k in ("status", "authenticated", "message", "reused_session")
        }
        print(report["opengov_auth"], flush=True)

        if og1.get("authenticated"):
            print("OpenGov discovery 10…", flush=True)
            og10 = c.post(
                f"{BASE}/api/m3/opengov-discovery/run",
                json={"max_entities": 10, "max_pages": 5, "use_auth": True},
            ).json()
            report["opengov_10"] = og10
            print(
                "entities",
                og10.get("entities_successful"),
                "/",
                og10.get("entities_attempted"),
                "raw",
                og10.get("raw_opportunities"),
                "statuses",
                og10.get("portal_status_counts"),
                flush=True,
            )
            print("OpenGov recovery 100…", flush=True)
            ogr = c.post(
                f"{BASE}/api/m3/opengov-recovery/run",
                json={"limit": 100, "use_auth": True, "resume": True},
            ).json()
            report["opengov_100"] = ogr
        else:
            report["opengov_10"] = {"skipped": True, "reason": og1.get("message") or og1.get("status")}
            report["opengov_100"] = {"skipped": True}

        # status snapshots for UI check
        report["bidnet_status"] = c.get(f"{BASE}/api/m3/bidnet-auth/status").json()
        report["opengov_status"] = c.get(f"{BASE}/api/m3/opengov-auth/status").json()
        report["bidnet_funnel"] = c.get(f"{BASE}/api/m3/bidnet-recovery/funnel").json()
        report["opengov_funnel"] = c.get(f"{BASE}/api/m3/opengov-recovery/funnel").json()
        report["opportunity_health"] = c.get(f"{BASE}/api/ui/opportunity-health").json()

        # Scheduler isolation smoke (do not wait for full run)
        report["scheduler_invoke"] = c.post(f"{BASE}/api/m3/discovery/run", json={}).json()

    report["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    # Scrub
    text = json.dumps(report, indent=2, default=str)
    for secret in (password, email):
        if secret:
            text = text.replace(secret, "[REDACTED]")
    OUT.write_text(text, encoding="utf-8")
    print("Wrote", OUT, flush=True)


if __name__ == "__main__":
    main()
