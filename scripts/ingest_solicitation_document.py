"""Operator CLI: ingest authorized solicitation documents and optionally reprocess.

Example:
  python scripts/ingest_solicitation_document.py \\
    --solicitation 645-DOTRFB-2975-2027 \\
    --file path/to/Snow_Plow_Blade_Spec.pdf \\
    --source sciquest \\
    --acquisition authorized_operator_download \\
    --reprocess
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from document_ingestion import ingest_many, ingest_solicitation_document  # noqa: E402
from document_ingestion_constants import ACQ_AUTHORIZED_OPERATOR_DOWNLOAD  # noqa: E402
from procurement_reprocess import reprocess_solicitation  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="Ingest operator-authorized solicitation documents")
    p.add_argument("--solicitation", required=True, help="Solicitation number")
    p.add_argument("--file", action="append", dest="files", required=True, help="File path (repeatable)")
    p.add_argument("--source", default="sciquest", help="Source system label")
    p.add_argument(
        "--acquisition",
        default=ACQ_AUTHORIZED_OPERATOR_DOWNLOAD,
        help="Acquisition method (authorized_operator_download|manual_upload|authorized_portal_export|public_fetch)",
    )
    p.add_argument("--title", default=None, help="Override document title")
    p.add_argument("--type", dest="document_type", default=None, help="Override document class")
    p.add_argument("--url", default=None, help="Operator-provided source URL")
    p.add_argument("--deal-id", default=None)
    p.add_argument("--opportunity-id", default=None)
    p.add_argument("--amendment-sequence", type=int, default=None)
    p.add_argument("--reprocess", action="store_true", help="Re-run transactional procurement after ingest")
    args = p.parse_args()

    if len(args.files) == 1:
        result = ingest_solicitation_document(
            solicitation_number=args.solicitation,
            file_path=args.files[0],
            source_system=args.source,
            acquisition_method=args.acquisition,
            document_title=args.title,
            document_type=args.document_type,
            source_url=args.url,
            deal_id=args.deal_id,
            opportunity_id=args.opportunity_id or args.solicitation,
            amendment_sequence=args.amendment_sequence,
        )
    else:
        result = ingest_many(
            solicitation_number=args.solicitation,
            file_paths=args.files,
            source_system=args.source,
            acquisition_method=args.acquisition,
            document_title=args.title,
            document_type=args.document_type,
            source_url=args.url,
            deal_id=args.deal_id,
            opportunity_id=args.opportunity_id or args.solicitation,
            amendment_sequence=args.amendment_sequence,
        )

    out: dict = {"ingest": result}
    if args.reprocess:
        out["reprocess"] = reprocess_solicitation(args.solicitation)
        # Shrink packet in stdout
        if isinstance(out["reprocess"].get("packet"), dict):
            pkt = out["reprocess"]["packet"]
            out["reprocess"] = {
                "ok": out["reprocess"].get("ok"),
                "changes": out["reprocess"].get("changes"),
                "blocker_model": out["reprocess"].get("blocker_model"),
                "decision": pkt.get("decision"),
                "primary_blocker": (pkt.get("blocker_model") or {}).get("primary_blocker"),
                "supplier_outreach": 0,
                "lender_outreach": 0,
                "bid_submissions": 0,
            }

    print(json.dumps(out, indent=2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
