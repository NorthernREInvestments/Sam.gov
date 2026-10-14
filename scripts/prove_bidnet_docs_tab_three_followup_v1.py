"""Bounded follow-up: 3 BidNet product opps from already-accessible groups (no new PG reg).

railway run -- python scripts/prove_bidnet_docs_tab_three_followup_v1.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _prep() -> None:
    raw = (os.environ.get("M3_DATA_ROOT") or "").strip()
    local = (ROOT / "data").resolve()
    if raw in {"/data", "/app/data"} or (raw and not Path(raw).exists() and os.name == "nt"):
        os.environ["M3_DATA_ROOT"] = str(local)


def main() -> int:
    _prep()
    from bidnet_auth.client import BidNetAuthenticatedClient
    from bidnet_auth.config import load_bidnet_auth_config
    from bidnet_auth.purchasing_group_registration import extract_pg_id_from_url
    from bidnet_auth.purchasing_group_tracker import STATUS_REGISTERED, get_group
    from bidnet_engine.schedule_extraction import extract_schedules_from_package
    from m3_data_root import data_path
    from phase_l.l23_full_population_funnel import load_store

    store = load_store()
    # Prefer Empire State (registered) product-ish titles, skip copy paper itself
    candidates = []
    for oid, row in store.items():
        if not isinstance(row, dict):
            continue
        url = str(row.get("authoritative_url") or "")
        if "bidnet" not in url.lower():
            continue
        pg = extract_pg_id_from_url(url)
        title = str(row.get("title") or "")
        if oid == "b5d99d0c52d52b22" or "9797265001" in url:
            continue
        # Only groups already REGISTERED in tracker (Empire State) — no new registrations
        if pg != "8411151":
            continue
        if not re.search(
            r"paper|supply|toner|ink|filter|glove|battery|cable|lamp|chemical|equipment",
            title,
            re.I,
        ):
            continue
        candidates.append(
            {
                "opportunity_id": oid,
                "title": title[:120],
                "url": url[:200],
                "buyer": row.get("buyer"),
            }
        )
        if len(candidates) >= 8:
            break

    report = {"kind": "DocsTabThreeFollowup", "selected": candidates[:3], "results": []}
    if not candidates:
        report["note"] = "No additional Empire-State product candidates found in store"
        path = Path(data_path("_og_commercial_research_pipeline_out/m3_prove_docs_tab_three_followup_v1.json"))
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
        return 0

    client = BidNetAuthenticatedClient(load_bidnet_auth_config())
    try:
        auth = client.ensure_authenticated()
        report["auth"] = auth.status
        if not auth.authenticated:
            print(json.dumps(report, indent=2))
            return 1
        for item in candidates[:3]:
            t0 = time.time()
            row: dict = {"opportunity_id": item["opportunity_id"], "title": item["title"]}
            try:
                resolved = client.resolve_detail_via_authenticated_search(
                    title=item["title"][:80], timeout_ms=20_000
                )
                row["detail_ok"] = bool(resolved.get("ok"))
                row["access_gate"] = resolved.get("access_gate")
                row["detail_url"] = (resolved.get("detail_url") or "")[:160]
                if not resolved.get("ok") or resolved.get("access_gate") == "ACTION_REQUIRED":
                    row["blocker"] = resolved.get("access_gate") or resolved.get("status")
                    report["results"].append(row)
                    continue
                detail = str(resolved.get("detail_url") or "")
                docs = client.discover_attachment_links(detail, timeout_ms=40_000) or []
                downloaded = [d for d in docs if d.get("local_path")]
                row["docs_identified"] = len(docs)
                row["docs_downloaded"] = len(downloaded)
                sched = extract_schedules_from_package(docs, opportunity_id=item["opportunity_id"])
                rows = sched.get("schedule_rows") or []
                row["product_lines"] = len(rows)
                row["elapsed_s"] = round(time.time() - t0, 2)
            except Exception as exc:
                row["error"] = type(exc).__name__
            report["results"].append(row)
    finally:
        try:
            client.close()
        except Exception:
            pass

    path = Path(data_path("_og_commercial_research_pipeline_out/m3_prove_docs_tab_three_followup_v1.json"))
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, indent=2, default=str)[:5000])
    print("WROTE", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
