"""Safe live-test CLI commands for product beta. Default = no live APIs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def _print(obj: dict) -> None:
    print(json.dumps(obj, indent=2, default=str))


def cmd_discovery_preflight(args: argparse.Namespace) -> int:
    from product_discovery import preflight_product_sync

    _print(preflight_product_sync(limit_per_naics=args.limit_per_naics, max_naics=args.max_naics))
    return 0


def cmd_product_sync(args: argparse.Namespace) -> int:
    from product_discovery import run_product_sync

    if not args.authorize_live:
        _print(run_product_sync(authorize_live=False, max_naics=args.max_naics, limit_per_naics=args.limit_per_naics))
        return 2
    _print(
        run_product_sync(
            authorize_live=True,
            authorize_broad_sam_discovery=bool(args.authorize_broad_sam_discovery),
            max_naics=args.max_naics,
            limit_per_naics=args.limit_per_naics,
            persist=not args.no_persist,
        )
    )
    return 0


def cmd_funnel_preflight(args: argparse.Namespace) -> int:
    from database import SessionLocal
    from models import Contract
    from product_funnel import funnel_preflight

    session = SessionLocal()
    try:
        c = session.query(Contract).filter_by(id=args.opportunity_id).one()
        _print(funnel_preflight(c, session=session))
    finally:
        session.close()
    return 0


def cmd_funnel_run(args: argparse.Namespace) -> int:
    from database import SessionLocal
    from models import Contract
    from product_funnel import run_product_funnel

    session = SessionLocal()
    try:
        c = session.query(Contract).filter_by(id=args.opportunity_id).one()
        _print(
            run_product_funnel(
                c,
                session=session,
                authorize_stage1=args.authorize_stage1,
                authorize_stage2=args.authorize_stage2,
                authorize_stage3_external=False,
                persist=True,
            )
        )
    finally:
        session.close()
    return 0


def cmd_stage3_preflight(args: argparse.Namespace) -> int:
    from ai_funnel import stage0_evaluate
    from ai_stage1 import resolve_current_stage1_result
    from database import SessionLocal
    from models import Contract
    from stage3_engine import stage3_preflight

    session = SessionLocal()
    try:
        c = session.query(Contract).filter_by(id=args.opportunity_id).one()
        s0 = stage0_evaluate(c)
        s1 = resolve_current_stage1_result(c, stage0=s0)
        _print(stage3_preflight(c, stage0=s0, stage1=s1.get("result"), session=session))
    finally:
        session.close()
    return 0


def cmd_stage3_run(args: argparse.Namespace) -> int:
    from ai_funnel import stage0_evaluate
    from ai_stage1 import resolve_current_stage1_result
    from database import SessionLocal
    from models import Contract
    from stage3_engine import run_stage3

    if not args.authorize_external and args.require_auth_flag:
        _print({"error": "pass --authorize-external to enable (still no web/openai unless also flagged)"})
        return 2
    session = SessionLocal()
    try:
        c = session.query(Contract).filter_by(id=args.opportunity_id).one()
        s0 = stage0_evaluate(c)
        s1 = resolve_current_stage1_result(c, stage0=s0)
        result = run_stage3(
            c,
            stage0=s0,
            stage1=s1.get("result"),
            session=session,
            authorize_external=args.authorize_external,
            authorize_web=args.authorize_web,
            authorize_openai=args.authorize_openai,
            persist_events=True,
        )
        session.commit()
        _print(result)
    finally:
        session.close()
    return 0


def cmd_direct_docs(args: argparse.Namespace) -> int:
    """Retrieve already-stored public document URLs — zero SAM API."""
    from database import SessionLocal
    from direct_document_retrieval import inspect_document_readiness, retrieve_direct_public_documents
    from models import Contract
    from sam_scarcity import PURPOSE_OPPORTUNITY_VERIFY, evaluate_sam_api_eligibility, gate_sam_api_call

    session = SessionLocal()
    try:
        c = session.query(Contract).filter_by(id=args.opportunity_id).one()
        # Prove another SAM call is blocked for this opportunity
        sam_block = gate_sam_api_call(
            purpose=PURPOSE_OPPORTUNITY_VERIFY,
            opportunity=c,
            authorize_live=True,
            context={
                "title_has_part_number": True,
                "title_has_quantity": True,
                "core_fit": "CORE_PRODUCT",
            },
        )
        retrieval = retrieve_direct_public_documents(c, session)
        session.commit()
        session.refresh(c)
        readiness = inspect_document_readiness(c)
        eligibility = evaluate_sam_api_eligibility(
            c,
            purpose=PURPOSE_OPPORTUNITY_VERIFY,
            context={
                "title_has_part_number": True,
                "title_has_quantity": True,
                "core_fit": "CORE_PRODUCT",
            },
        )
        _print(
            {
                "sam_api_gate_before_docs": {
                    "allowed": sam_block["allowed"],
                    "blocked_reason": sam_block.get("blocked_reason"),
                    "eligibility": sam_block.get("eligibility"),
                },
                "direct_retrieval": retrieval,
                "document_readiness": readiness,
                "sam_eligibility_after": eligibility,
                "LIVE_SAM_API": 0,
                "LIVE_OPENAI": 0,
            }
        )
    finally:
        session.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Product beta safe CLI (default: no live APIs)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("discovery-preflight", help="Show SAM product sync plan — no execution")
    p1.add_argument("--max-naics", type=int, default=3)
    p1.add_argument("--limit-per-naics", type=int, default=50)
    p1.set_defaults(func=cmd_discovery_preflight)

    p2 = sub.add_parser("product-sync", help="Rare controlled SAM product sync — dual auth required")
    p2.add_argument("--authorize-live", action="store_true")
    p2.add_argument(
        "--authorize-broad-sam-discovery",
        action="store_true",
        help="Also requires SAM_ALLOW_BROAD_DISCOVERY=true — not routine discovery",
    )
    p2.add_argument("--max-naics", type=int, default=3)
    p2.add_argument("--limit-per-naics", type=int, default=50)
    p2.add_argument("--no-persist", action="store_true")
    p2.set_defaults(func=cmd_product_sync)

    p3 = sub.add_parser("funnel-preflight", help="Funnel preflight for one opportunity")
    p3.add_argument("--opportunity-id", type=int, required=True)
    p3.set_defaults(func=cmd_funnel_preflight)

    p4 = sub.add_parser("funnel-run", help="One-opportunity funnel (explicit stage auth)")
    p4.add_argument("--opportunity-id", type=int, required=True)
    p4.add_argument("--authorize-stage1", action="store_true")
    p4.add_argument("--authorize-stage2", action="store_true")
    p4.set_defaults(func=cmd_funnel_run)

    p5 = sub.add_parser("stage3-preflight", help="Stage 3 research preflight")
    p5.add_argument("--opportunity-id", type=int, required=True)
    p5.set_defaults(func=cmd_stage3_preflight)

    p6 = sub.add_parser("stage3-run", help="Stage 3 run (postgres-first; external needs auth)")
    p6.add_argument("--opportunity-id", type=int, required=True)
    p6.add_argument("--authorize-external", action="store_true")
    p6.add_argument("--authorize-web", action="store_true")
    p6.add_argument("--authorize-openai", action="store_true")
    p6.add_argument("--require-auth-flag", action="store_true", default=True)
    p6.set_defaults(func=cmd_stage3_run)

    p7 = sub.add_parser(
        "direct-docs",
        help="Fetch already-stored public document URLs for one opportunity (no SAM API)",
    )
    p7.add_argument("--opportunity-id", type=int, required=True)
    p7.set_defaults(func=cmd_direct_docs)

    args = parser.parse_args()
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
