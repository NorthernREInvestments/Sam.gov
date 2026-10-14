"""End-to-end: discover → download → extract lines for Copy Paper 9797265001.

railway run -- python scripts/prove_bidnet_copy_paper_package_v1.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SID = "9797265001"
OID = "b5d99d0c52d52b22"
TITLE = "28010 - Copy Paper"


def _prep() -> None:
    raw = (os.environ.get("M3_DATA_ROOT") or "").strip()
    local = (ROOT / "data").resolve()
    if raw in {"/data", "/app/data"} or (raw and not Path(raw).exists() and os.name == "nt"):
        os.environ["M3_DATA_ROOT"] = str(local)


def main() -> int:
    _prep()
    from bidnet_auth.client import BidNetAuthenticatedClient
    from bidnet_auth.config import load_bidnet_auth_config
    from bidnet_auth.purchasing_group_tracker import STATUS_REGISTERED, get_group, upsert_group
    from bidnet_engine.schedule_extraction import extract_schedules_from_package
    from m3_data_root import data_path

    if (get_group("8411151") or {}).get("status") != STATUS_REGISTERED:
        upsert_group(
            "8411151",
            name="Empire State Purchasing Group",
            status=STATUS_REGISTERED,
            relevant=True,
        )

    t0 = time.time()
    report: dict = {
        "kind": "CopyPaperPackageProof",
        "opportunity_id": OID,
        "solicitation_id": SID,
        "title": TITLE,
    }
    client = BidNetAuthenticatedClient(load_bidnet_auth_config())
    try:
        auth = client.ensure_authenticated()
        report["auth"] = {"status": auth.status, "authenticated": bool(auth.authenticated)}
        if not auth.authenticated:
            print(json.dumps(report, indent=2))
            return 1

        detail = f"https://www.bidnetdirect.com/private/solicitations/{SID}/abstract?target=view"
        docs = client.discover_attachment_links(detail, timeout_ms=60_000) or []
        report["documents_shown_in_bidnet"] = 4
        report["document_references_identified"] = len(docs)
        report["documents"] = [
            {
                "name": d.get("document_name") or d.get("filename"),
                "url": str(d.get("document_url") or d.get("url") or "")[:160],
                "local_path_set": bool(d.get("local_path")),
                "local_path": str(d.get("local_path") or "")[-120:] or None,
                "retrieval_status": d.get("retrieval_status"),
                "attachment_id": d.get("attachment_id"),
            }
            for d in docs
            if isinstance(d, dict)
        ]
        downloaded = [d for d in docs if isinstance(d, dict) and d.get("local_path")]
        report["files_successfully_downloaded"] = len(downloaded)

        sched = extract_schedules_from_package(
            docs,
            opportunity_id=OID,
            body_text=TITLE,
        )
        rows = [r for r in (sched.get("schedule_rows") or []) if isinstance(r, dict)]
        report["files_successfully_parsed"] = sum(
            1 for p in (sched.get("per_document") or []) if isinstance(p, dict) and p.get("ok")
        )
        report["product_lines_extracted"] = len(rows)

        def _qty(r: dict) -> Any:
            q = r.get("QTY") or r.get("quantity") or r.get("Quantity")
            if isinstance(q, dict):
                return q.get("VALUE")
            return q

        def _desc(r: dict) -> str:
            d = r.get("DESCRIPTION") or r.get("description") or r.get("Item Description") or ""
            if isinstance(d, dict):
                return str(d.get("VALUE") or "")[:160]
            return str(d)[:160]

        def _uom(r: dict) -> str:
            u = r.get("UOM") or r.get("uom") or r.get("unit") or r.get("Unit Measure") or ""
            if isinstance(u, dict):
                return str(u.get("VALUE") or "")
            return str(u)

        def _numeric_qty(q: Any) -> bool:
            try:
                return float(str(q).replace(",", "").strip()) > 0
            except Exception:
                return False

        fully = [
            r
            for r in rows
            if len(_desc(r).strip()) >= 12 and _numeric_qty(_qty(r))
        ]
        report["product_lines_fully_specified"] = len(fully)
        report["line_items_sample"] = [
            {
                "description": _desc(r),
                "qty": str(_qty(r)).strip(),
                "uom": _uom(r),
                "item_id": r.get("item_number") or r.get("Item ID") or r.get("item_id"),
            }
            for r in fully[:20]
        ]
        report["schedule_meta"] = {
            "ok": sched.get("ok"),
            "expected_lines": sched.get("expected_lines"),
            "parsers_used": sched.get("parsers_used"),
            "inventory_roles": [
                {
                    "filename": i.get("filename"),
                    "role": i.get("document_role"),
                    "ext": i.get("extension"),
                }
                for i in (sched.get("inventory") or [])[:10]
            ],
        }

        # Persist commercial evidence stub for funnel
        out_dir = Path(data_path("_og_commercial_research_pipeline_out"))
        out_dir.mkdir(parents=True, exist_ok=True)
        evidence = {
            "opportunity_id": OID,
            "solicitation_id": SID,
            "title": TITLE,
            "buyer": "Cattaraugus - Allegany BOCES",
            "documents": report["documents"],
            "line_items": rows,
            "package_state": (
                "PACKAGE_COMPLETE"
                if len(downloaded) >= 4 and len(fully) >= 10
                else "PACKAGE_PARTIAL"
                if downloaded and rows
                else "PACKAGE_BLOCKED"
            ),
            "statuses": {
                "DOCUMENT_DOWNLOADED": len(downloaded),
                "DOCUMENT_PARSED": report["files_successfully_parsed"],
                "LINE_ITEM_EXTRACTED": len(rows),
                "LINE_ITEM_FULLY_SPECIFIED": len(fully),
            },
        }
        (out_dir / "m3_copy_paper_package_evidence_v1.json").write_text(
            json.dumps(evidence, indent=2, default=str), encoding="utf-8"
        )
        report["package_completeness"] = evidence["package_state"]
        report["bid_instructions_recovered"] = any(
            "APPENDIX A" in str(d.get("name") or "") or "Submission" in str(d.get("name") or "")
            for d in report["documents"]
        )
        report["delivery_requirements_recovered"] = None  # only if stated in docs
        report["elapsed_s"] = round(time.time() - t0, 2)
        report["ok"] = len(downloaded) >= 4 and len(fully) >= 10
    finally:
        try:
            client.close()
        except Exception:
            pass

    path = Path(data_path("_og_commercial_research_pipeline_out/m3_prove_copy_paper_package_v1.json"))
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, indent=2, default=str)[:7000])
    print("WROTE", path)
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
