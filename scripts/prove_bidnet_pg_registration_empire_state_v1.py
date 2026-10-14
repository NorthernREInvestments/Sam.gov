"""Bounded Empire State purchasing-group registration proof.

railway run -- python scripts/prove_bidnet_pg_registration_empire_state_v1.py

Does NOT purchase. Submits Save only when EIN is verified and
M3_BIDNET_PG_AUTO_SUBMIT=1 (or --submit --owner-approved).
"""

from __future__ import annotations

import argparse
import json
import os
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
    ap = argparse.ArgumentParser()
    ap.add_argument("--submit", action="store_true", help="Allow Save when form is complete")
    ap.add_argument(
        "--owner-approved",
        action="store_true",
        help="Owner explicitly authorized submit (or set M3_BIDNET_PG_AUTO_SUBMIT=1)",
    )
    ap.add_argument("--audit-only", action="store_true", help="Only audit PG opportunity counts")
    args = ap.parse_args()

    from bidnet_auth.client import BidNetAuthenticatedClient
    from bidnet_auth.config import load_bidnet_auth_config
    from bidnet_auth.purchasing_group_registration import (
        audit_purchasing_groups_from_store,
        extract_pg_gate_from_html,
        load_verified_company_answers,
        prepare_empire_state_answers,
        run_empire_state_registration,
    )
    from bidnet_auth.purchasing_group_tracker import load_tracker, ui_summary
    from m3_data_root import data_path

    out_dir = Path(data_path("_og_commercial_research_pipeline_out"))
    out_dir.mkdir(parents=True, exist_ok=True)
    report: dict = {
        "kind": "EmpireStatePgRegistrationProof",
        "build": "20261013-m3-bidnet-pg-registration-v1",
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    audit = audit_purchasing_groups_from_store()
    report["audit"] = audit
    if args.audit_only:
        report["ui"] = ui_summary()
        path = out_dir / "m3_prove_pg_registration_empire_state_v1.json"
        path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print(json.dumps({"ok": True, "audit_only": True, "path": str(path)}, indent=2))
        return 0

    answers = load_verified_company_answers()
    plan = prepare_empire_state_answers(answers)
    report["company_answers_meta"] = plan.get("company_answers_meta")
    report["prepare_plan"] = {
        "can_submit": plan.get("can_submit"),
        "owner_required": plan.get("owner_required"),
        "owner_reasons": plan.get("owner_reasons"),
        "missing_fields": plan.get("missing_fields"),
        "fills": [
            {k: v for k, v in f.items() if k != "value"} for f in (plan.get("fills") or [])
        ],
    }

    cfg = load_bidnet_auth_config()
    client = BidNetAuthenticatedClient(cfg)
    t0 = time.time()
    try:
        auth = client.ensure_authenticated()
        report["auth"] = {
            "authenticated": bool(auth.authenticated),
            "status": auth.status,
            "reused": bool(getattr(auth, "reused_session", False)),
        }
        if not auth.authenticated:
            report["ok"] = False
            report["blocker"] = "AUTH_FAILED"
            (out_dir / "m3_prove_pg_registration_empire_state_v1.json").write_text(
                json.dumps(report, indent=2, default=str), encoding="utf-8"
            )
            print(json.dumps(report, indent=2, default=str)[:2000])
            return 1

        # Open solicitation to capture gate
        resolved = client.resolve_detail_via_authenticated_search(
            title="Copy Paper", timeout_ms=25_000
        )
        client._page.wait_for_timeout(1_200)
        html = client._page.content() or ""
        gate = extract_pg_gate_from_html(html, page_url=str(client._page.url or ""))
        report["pre_gate"] = {
            "resolved_ok": resolved.get("ok"),
            "access_gate": resolved.get("access_gate"),
            "solicitation_id": resolved.get("solicitation_id"),
            "detail_url": (resolved.get("detail_url") or "")[:160],
            "gate": {
                k: (gate or {}).get(k)
                for k in ("name", "purchasing_group_id", "gate_kind", "fee_mentioned")
            }
            if gate
            else None,
            "complete_form_url_set": bool((gate or {}).get("complete_form_url")),
        }

        reg = run_empire_state_registration(
            client,
            solicitation_id="9797265001",
            form_url=(gate or {}).get("complete_form_url"),
            allow_submit=bool(args.submit),
            owner_approved_submit=bool(args.owner_approved),
        )
        # Never echo EIN
        safe_reg = dict(reg)
        if "plan" in safe_reg and isinstance(safe_reg["plan"], dict):
            safe_reg["plan"] = {
                **safe_reg["plan"],
                "company_answers_meta": (safe_reg["plan"].get("company_answers_meta") or {}),
            }
        report["registration"] = safe_reg

        # Retry solicitation if registered OR if prior live unlock already happened
        from bidnet_auth.purchasing_group_tracker import STATUS_REGISTERED, upsert_group

        retry = client.resolve_detail_via_authenticated_search(
            title="Copy Paper", timeout_ms=20_000
        )
        report["post_retry"] = {
            "ok": retry.get("ok"),
            "access_gate": retry.get("access_gate"),
            "detail_url": (retry.get("detail_url") or "")[:160],
            "solicitation_id": retry.get("solicitation_id"),
        }
        unlocked = bool(
            retry.get("ok") and retry.get("access_gate") != "ACTION_REQUIRED"
        )
        report["solicitation_unlocked"] = unlocked
        if unlocked:
            try:
                report["post_retry"]["h1"] = (
                    client._page.locator("h1").first.inner_text(timeout=2_000) or ""
                )[:160]
            except Exception:
                report["post_retry"]["h1"] = None
            docs = []
            try:
                docs = client.discover_attachment_links(
                    str(retry.get("detail_url") or ""), timeout_ms=12_000
                ) or []
            except Exception as exc:
                report["doc_discover_error"] = type(exc).__name__
            report["documents"] = [
                {
                    "name": (d.get("document_name") or d.get("name") or "")[:120],
                    "url_set": bool(d.get("document_url") or d.get("url")),
                }
                for d in docs[:15]
                if isinstance(d, dict)
            ]
            report["document_count"] = len(report["documents"])
            upsert_group(
                "8411151",
                name="Empire State Purchasing Group",
                status=STATUS_REGISTERED,
                registration_completed_at=report.get("started_at"),
                solicitations_unlocked=1,
                relevant=True,
            )
            if reg.get("status") != STATUS_REGISTERED:
                report["registration"]["status"] = STATUS_REGISTERED
                report["registration"]["verified_via"] = "solicitation_retry_unlocked"
        else:
            report["blocker"] = reg.get("status")
            report["owner_next_steps"] = reg.get("owner_next_steps")

        report["tracker"] = load_tracker().get("metrics")
        report["ui"] = ui_summary()
        report["ok"] = bool(
            report.get("solicitation_unlocked")
            or reg.get("status") in {"OWNER_APPROVAL_REQUIRED", "PREPARED", "REGISTERED"}
        )
        report["elapsed_s"] = round(time.time() - t0, 2)
    finally:
        try:
            client.close()
        except Exception:
            pass

    path = out_dir / "m3_prove_pg_registration_empire_state_v1.json"
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps({k: report[k] for k in report if k != "ui"}, indent=2, default=str)[:4000])
    print(f"\nWROTE {path}")
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
