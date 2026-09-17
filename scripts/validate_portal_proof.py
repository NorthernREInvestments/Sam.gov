#!/usr/bin/env python3
"""
Controlled live portal proof — LISTING ONLY, max 8 public requests.
Does NOT run TINY/BROAD/NATIONAL. No persistence. No SAM/OpenAI/paid.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from discovery.classify import classify_discovery_opportunity
from discovery.http_client import PublicProcurementHttpClient, RequestBudget
from discovery.live_fetchers import get_live_fetcher
from discovery.selection import _pool_map
from discovery.state_matrix import set_status_override


PROOF_SOURCES = [
    "state_ia",
    "state_mt",
    "coop_sourcewell_live",
    "state_ne",
    "state_pa",
    "state_tx",
    "agency_airport_dfw_tx",
    "state_ga",
]


def main() -> int:
    pool = _pool_map()
    budget = RequestBudget(
        max_total_requests=8,
        max_requests_per_source=1,
        max_pages_per_source=1,
        max_records_per_source=40,
        max_runtime_seconds=120,
        min_interval_seconds=2.0,
        timeout_seconds=20.0,
    )
    client = PublicProcurementHttpClient(budget=budget, authorize_live=True)
    results = []
    print("=== Controlled Portal Proof (listing-only, max 8) ===")
    print("SAM=0 OpenAI=0 USAspending=0 paid=0")

    for sid in PROOF_SOURCES:
        if client.request_count >= budget.max_total_requests:
            print("GLOBAL budget reached — stopping")
            break
        cand = pool.get(sid)
        if not cand or not cand.get("list_url"):
            print(f"SKIP {sid} (missing)")
            continue
        fetcher = get_live_fetcher(cand["adapter_family"])
        if not fetcher:
            print(f"SKIP {sid} (no fetcher)")
            continue
        before = client.request_count
        try:
            out = fetcher.fetch_listing(
                client, list_url=cand["list_url"], source_id=sid, max_pages=1
            )
        except Exception as exc:
            row = {
                "source_id": sid,
                "kind": cand.get("kind"),
                "jurisdiction": cand.get("state_code") or cand.get("kind"),
                "platform": cand.get("platform_family"),
                "url": cand["list_url"],
                "error": str(exc),
                "parser_recognized_structure": "NO",
                "raw_candidate_rows": 0,
                "structurally_valid": 0,
                "malformed_rejected": 0,
                "health": "BROKEN",
            }
            results.append(row)
            print(f"FAIL {sid}: {exc}")
            continue

        opps = out.get("opportunities") or []
        val = out.get("validation") or {}
        meta = out.get("request_meta") or {}
        malformed = out.get("malformed_rejected") or 0
        # classifications
        classes = {"CORE_PRODUCT": 0, "PRODUCT_PLUS_SERVICE": 0, "SERVICE": 0, "UNKNOWN": 0, "CLEARLY_IRRELEVANT": 0}
        for o in opps:
            cls = classify_discovery_opportunity(title=o.title, description=o.description, status=o.status)[
                "classification"
            ]
            classes[cls] = classes.get(cls, 0) + 1
        sample = opps[0] if opps else None
        recognized = bool(val.get("structure_recognized")) or val.get("failure_type") == "PARSER_FAILURE"
        if opps:
            recognized = True

        health = val.get("health_status") or "UNKNOWN"
        if opps:
            set_status_override(sid, "LIVE_VERIFIED")
            health = "LIVE_VERIFIED"
        elif val.get("valid") and val.get("zero_records_ok"):
            set_status_override(sid, "LIVE_VERIFIED")
            health = "LIVE_VERIFIED"
        elif val.get("failure_type") == "AUTH_REQUIRED":
            set_status_override(sid, "AUTH_REQUIRED")
            health = "AUTH_REQUIRED"
        elif val.get("health_status"):
            set_status_override(sid, str(val.get("health_status")))

        row = {
            "source_id": sid,
            "kind": cand.get("kind"),
            "jurisdiction": cand.get("state_code") or cand.get("kind"),
            "platform": cand.get("platform_family"),
            "url": cand["list_url"],
            "host": urlparse(cand["list_url"]).netloc,
            "http": meta.get("http_status"),
            "bytes": meta.get("bytes"),
            "requests": client.request_count - before,
            "parser_recognized_structure": "YES" if recognized else "NO",
            "raw_candidate_rows": len(opps) + malformed,
            "structurally_valid": len(opps),
            "malformed_rejected": malformed,
            "unique": len({o.external_id for o in opps}),
            "classes": classes,
            "sample_title": (sample.title if sample else None),
            "sample_id": (sample.solicitation_number or sample.external_id) if sample else None,
            "sample_deadline": sample.deadline_raw if sample else None,
            "sample_classification": (
                classify_discovery_opportunity(title=sample.title)["classification"] if sample else None
            ),
            "health": health,
            "validation": val.get("failure_type") or val.get("health_status"),
        }
        results.append(row)
        print(
            f"{sid}: http={row['http']} bytes={row['bytes']} struct={row['parser_recognized_structure']} "
            f"valid={row['structurally_valid']} malformed={row['malformed_rejected']} health={row['health']}"
        )
        if sample:
            print(f"  sample: {row['sample_id']} | {row['sample_title'][:70]} | {row['sample_deadline']} | {row['sample_classification']}")

    out_path = ROOT / "artifacts" / "portal_proof_results.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "results": results,
        "request_count": client.request_count,
        "accounting": client.accounting(),
        "SAM": 0,
        "OpenAI": 0,
        "USAspending": 0,
        "paid": 0,
        "LIVE_VERIFIED": sum(1 for r in results if r.get("health") == "LIVE_VERIFIED"),
        "sources_with_opps": sum(1 for r in results if (r.get("structurally_valid") or 0) > 0),
    }
    out_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nrequests={client.request_count} LIVE_VERIFIED={payload['LIVE_VERIFIED']} with_opps={payload['sources_with_opps']}")
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
