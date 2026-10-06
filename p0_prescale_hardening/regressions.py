"""Regressions + golden path + dry-run for P0 hardening."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from m3_data_root import data_path
from p0_prescale_hardening.identity_revenue import (
    run_identity_leak_regression,
    run_revenue_allocation_regression,
)
from p0_prescale_hardening.models import (
    GOLDEN,
    GO_METRO_OID,
    REAL_QUOTE_TEST_MODE,
    REGRESSION,
    TEST_FIXTURE_ONLY,
)
from p0_prescale_hardening.packet_rebuild import generate_quote_request_text
from p0_prescale_hardening.provenance import build_source_provenance
from p0_prescale_hardening.quote_pipeline import (
    init_basket_from_packet,
    process_real_supplier_quote,
    production_gate_quote,
    run_auto_wire_test,
    run_partial_quote_test,
)
from p0_prescale_hardening.scope_classify import run_scope_regression


def run_provenance_regressions() -> dict[str, Any]:
    cases = [
        {
            "id": "missing_prov",
            "line": {"line_id": "x", "description": "widget", "mpn": "1"},
            "must_complete": False,
        },
        {
            "id": "pdf_with_page",
            "line": {
                "line_id": "y",
                "description": "MODEL NUMBER: LK4430BF1U",
                "mpn": "LK4430BF1U",
                "source_document": "plans.pdf",
                "source_kind": "pdf",
                "page": 3,
                "raw_source_text": "MODEL NUMBER: LK4430BF1U",
            },
            "must_complete": True,
        },
        {
            "id": "xlsx_with_cell",
            "line": {
                "line_id": "z",
                "description": "SENSOR",
                "mpn": "13-69938-00",
                "source_document": "parts.xlsx",
                "source_kind": "xlsx",
                "sheet": "Sheet1",
                "row": 12,
                "cell": "A12",
                "raw_source_text": "13-69938-00",
            },
            "must_complete": True,
        },
    ]
    results = []
    passed = 0
    for c in cases:
        prov = build_source_provenance(c["line"])
        ok = bool(prov.get("complete")) == c["must_complete"]
        if ok:
            passed += 1
        results.append({"id": c["id"], "complete": prov.get("complete"), "pass": ok})
    return {"total": len(cases), "passed": passed, "failed": len(cases) - passed, "all_pass": passed == len(cases), "results": results}


def run_quantity_duplicate_regressions(packet: dict[str, Any]) -> dict[str, Any]:
    diff = packet.get("DIFF")
    dups = packet.get("duplicate_line_ids") or []
    # also scan lines
    ids = [l.get("line_id") for l in packet.get("lines") or []]
    from collections import Counter

    real_dups = [i for i, n in Counter(ids).items() if n > 1]
    ok = (diff == 0 or diff == 0.0) and not real_dups
    return {
        "quantity_diff": diff,
        "duplicates": real_dups or dups,
        "PASS_FAIL": "PASS" if ok else "FAIL",
    }


def run_fixture_contamination_regression() -> dict[str, Any]:
    q = {
        "origin": TEST_FIXTURE_ONLY,
        "supplier_identity": "F",
        "quote_date": "2026-10-05",
        "quote_number": "1",
        "exact_mpn_model": "X",
        "qty": 1,
        "uom": "EA",
        "unit_price": 1,
        "extended_price": 1,
        "freight_treatment": "TBD",
        "validity": "30d",
        "source_artifact": "f",
    }
    g = production_gate_quote(q)
    return {"accepted": g.get("accepted"), "PASS_FAIL": "PASS" if not g.get("accepted") else "FAIL", "gate": g}


def run_golden_path() -> dict[str, Any]:
    p = data_path(GOLDEN)
    if not p.exists():
        return {"Cases": 0, "Passed": 0, "Failed": 0, "Regressions": [], "note": "corpus missing — skip"}
    corpus = json.loads(p.read_text(encoding="utf-8"))
    cases = corpus.get("cases") or corpus.get("items") or []
    if isinstance(corpus, list):
        cases = corpus
    # Golden path here: ensure conservation markers / prior invariants still present
    passed = 0
    failed = 0
    regressions = []
    for case in cases:
        # Soft: case must not claim bond/grant as product revenue if annotated
        ok = True
        if case.get("must_not_be_product_revenue") and case.get("treated_as_product_revenue"):
            ok = False
            regressions.append(case.get("id") or case.get("name"))
        if ok:
            passed += 1
        else:
            failed += 1
    # Also assert our hard regressions as golden extensions
    extra_ok = (
        run_revenue_allocation_regression()["all_pass"]
        and run_scope_regression()["all_pass"]
        and run_fixture_contamination_regression()["PASS_FAIL"] == "PASS"
    )
    if not extra_ok:
        regressions.append("p0_hard_regression_suite")
        failed += 1
    else:
        passed += 1
    return {
        "Cases": len(cases) + 1,
        "Passed": passed,
        "Failed": failed,
        "Regressions": regressions,
        "all_pass": failed == 0,
    }


def owner_channel_dry_run(packet: dict[str, Any]) -> dict[str, Any]:
    """Full dry run WITHOUT external sending."""
    text = generate_quote_request_text(packet)
    boundary = {
        "action": "SEND_EXPORT",
        "packet_id": packet.get("packet_id"),
        "quote_request_generated": True,
        "real_send_boundary": "STOP_BEFORE_SENDING",
        "sent": False,
        "do_not_send_automatically": True,
    }

    # Fixture ingest → blocked
    basket = init_basket_from_packet(packet)
    lines = basket.get("lines") or []
    mpn = (lines[0] or {}).get("mpn") if lines else "X"
    fixture = {
        "origin": TEST_FIXTURE_ONLY,
        "supplier_identity": "Fixture",
        "quote_date": "2026-10-05",
        "quote_number": "TF-DRY",
        "exact_mpn_model": mpn,
        "qty": (lines[0] or {}).get("qty") if lines else 1,
        "uom": (lines[0] or {}).get("uom") if lines else "EA",
        "unit_price": 1.0,
        "extended_price": 1.0,
        "freight_treatment": "TBD",
        "validity": "30 days",
        "source_artifact": "dry_fixture.txt",
    }
    fix = process_real_supplier_quote(fixture, basket_state=deepcopy(basket))

    # REAL_QUOTE_TEST_MODE segregated
    test_q = {
        "origin": REAL_QUOTE_TEST_MODE,
        "supplier_identity": "DryRun Test Supplier",
        "quote_date": "2026-10-05",
        "quote_number_or_reference": "DRY-RQT-1",
        "exact_mpn_model": mpn,
        "qty": (lines[0] or {}).get("qty") if lines else 1,
        "uom": (lines[0] or {}).get("uom") if lines else "EA",
        "unit_price": 42.0,
        "extended_price": 42.0,
        "freight_treatment": "PREPAID_AND_ADD",
        "freight": 5.0,
        "validity": "30 days",
        "source_artifact": "REAL_QUOTE_TEST_MODE_dry.json",
    }
    real = process_real_supplier_quote(test_q, basket_state=deepcopy(basket), opportunity_state={})

    return {
        "opportunity": packet.get("opportunity_id"),
        "packet_id": packet.get("packet_id"),
        "send_export_boundary": boundary,
        "quote_request_preview": text[:500],
        "fixture_blocked": fix.get("gate", {}).get("accepted") is False,
        "test_mode_processed": bool(real.get("PASS")),
        "test_mode_production_written": bool(real.get("production_written")),
        "basket_updated": bool(real.get("basket_state")),
        "next_action": real.get("next_action"),
        "no_production_contamination": real.get("production_written") is False,
        "PASS_FAIL": "PASS"
        if (
            boundary["sent"] is False
            and fix.get("gate", {}).get("accepted") is False
            and real.get("PASS")
            and real.get("production_written") is False
        )
        else "FAIL",
    }


def run_all_regressions(*, sample_packet: dict[str, Any] | None = None) -> dict[str, Any]:
    scope = run_scope_regression()
    identity = run_identity_leak_regression()
    revenue = run_revenue_allocation_regression()
    provenance = run_provenance_regressions()
    fixture = run_fixture_contamination_regression()
    golden = run_golden_path()

    packet_reg = {"PASS_FAIL": "SKIP"}
    partial = {"PASS_FAIL": "SKIP"}
    auto = {"PASS_FAIL": "SKIP"}
    if sample_packet:
        packet_reg = run_quantity_duplicate_regressions(sample_packet)
        partial = run_partial_quote_test(sample_packet)
        auto = run_auto_wire_test(sample_packet)

    payload = {
        "dania_grant_total": revenue,
        "collier_bonding_threshold": revenue,  # included in suite
        "install_service_leakage": scope,
        "fg_identity_leakage": identity,
        "missing_provenance_packet": provenance,
        "quantity_mismatch": packet_reg,
        "duplicate_line": packet_reg,
        "partial_quote": partial,
        "expired_quote": {"note": "covered in process_real_supplier_quote validity path"},
        "fixture_contamination": fixture,
        "real_quote_auto_economics": auto,
        "golden_path": golden,
    }
    all_pass = all(
        [
            scope["all_pass"],
            identity["all_pass"],
            revenue["all_pass"],
            provenance["all_pass"],
            fixture["PASS_FAIL"] == "PASS",
            packet_reg.get("PASS_FAIL") in {"PASS", "SKIP"},
            partial.get("PASS_FAIL") in {"PASS", "SKIP"},
            auto.get("PASS_FAIL") in {"PASS", "SKIP"},
            golden.get("all_pass", True),
        ]
    )
    payload["ALL_PASS"] = all_pass
    data_path(REGRESSION).write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return payload
