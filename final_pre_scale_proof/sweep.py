"""Orchestrate final pre-scale proof and emit Phase 21 report."""

from __future__ import annotations

import json
from typing import Any

from application_clock import now_utc
from final_pre_scale_proof.dania_revenue import validate_dania_400k
from final_pre_scale_proof.dania_scope import align_dania_lines
from final_pre_scale_proof.gap_register import build_gap_register, funnel_gap_audit, large_test_design
from final_pre_scale_proof.models import (
    BUILD,
    CK,
    GAP_REGISTER,
    INGESTION_FIXTURES,
    INGESTION_GATE,
    LARGE_TEST_DESIGN,
    OUTREACH_QUEUE,
    PRIOR_DC_REPORT,
    QUOTE_PACKETS_AUDITED,
    QUOTE_REQUESTS,
    REPORT,
)
from final_pre_scale_proof.outreach_and_economics import (
    dania_target_economics,
    generate_quote_request_texts,
    rerank_outreach,
)
from final_pre_scale_proof.packet_audit import audit_packets
from final_pre_scale_proof.quote_ingestion import run_ingestion_tests
from final_pre_scale_proof.revenue_search import (
    search_bridgeport,
    search_dekalb_bounded,
    search_go_metro_bounded,
)
from final_pre_scale_proof.supplier_contact import validate_dania_suppliers
from m3_data_root import data_path


def _dump(name: str, obj: Any) -> None:
    data_path(name).write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")


def run_final_pre_scale_proof_v1() -> dict[str, Any]:
    dania_rev = validate_dania_400k()
    dania_scope = align_dania_lines(revenue_validation=dania_rev)
    dania_econ = dania_target_economics(revenue_validation=dania_rev, scope=dania_scope)
    dania_suppliers = validate_dania_suppliers()

    bridgeport = search_bridgeport()
    go_metro = search_go_metro_bounded()
    dekalb = search_dekalb_bounded()

    packet_audit = audit_packets()
    quote_texts = generate_quote_request_texts()
    ingestion = run_ingestion_tests()
    outreach = rerank_outreach(
        packet_audit=packet_audit,
        dania_rev=dania_rev,
        bridgeport_rev=bridgeport,
        go_metro_rev=go_metro,
        dekalb_rev=dekalb,
        dania_economics=dania_econ,
    )

    stages = funnel_gap_audit(
        dania_rev=dania_rev, packet_audit=packet_audit, ingestion=ingestion
    )
    gaps = build_gap_register(stages)
    large = large_test_design(gap_register=gaps)

    owner_ready_n = sum(1 for p in packet_audit.get("packets") or [] if p.get("owner_ready_after_audit"))
    send_first = [r for r in outreach if r.get("READY_NOT_READY") in {"READY", "READY_AS_CHANNEL_TEST"}][:5]

    report = {
        "build": BUILD,
        "completed_at": now_utc().isoformat(),
        "DANIA_REVENUE_VALIDATION": {
            "Current_$400K_claim": "GRANT_TOTAL / PROGRAM_FUNDING (was misclassified CURRENT_CONTRACT_VALUE)",
            "Source": (dania_rev.get("source") or {}).get("document"),
            "Semantic_classification": (dania_rev.get("corrected") or {}).get("semantic_classification"),
            "Applies_to_this_solicitation": dania_rev.get("applies_to_this_solicitation"),
            "Applies_to_product_scope": dania_rev.get("applies_to_product_portion"),
            "Install_service_included": dania_rev.get("install_service_included"),
            "Usable_economic_revenue": dania_rev.get("usable_economic_revenue"),
            "PASS_FAIL": dania_rev.get("PASS_FAIL"),
            "detail": dania_rev,
        },
        "DANIA_PRODUCT_VALUE": {
            "Total_program_project_value": dania_scope.get("Total_program_project_value"),
            "Product_scope_value": dania_scope.get("PRODUCT_SCOPE_VALUE"),
            "Install_service_value": dania_scope.get("INSTALL_SCOPE_VALUE"),
            "Unallocated": dania_scope.get("UNALLOCATED_VALUE"),
            "Confidence": dania_scope.get("confidence"),
            "line_counts": dania_scope.get("counts"),
            "lines": dania_scope.get("lines"),
        },
        "DANIA_QUOTE_PACKETS": packet_audit.get("DANIA_QUOTE_PACKETS"),
        "DANIA_SUPPLIER_CONTACTS": dania_suppliers,
        "DANIA_TARGET_ECONOMICS": {
            "Max_acquisition_for_$5K": dania_econ.get("MAX_PRODUCT_ACQUISITION_FOR_$5K"),
            "Max_acquisition_for_$10K": dania_econ.get("MAX_PRODUCT_ACQUISITION_FOR_$10K"),
            "Max_acquisition_for_15%": dania_econ.get("MAX_PRODUCT_ACQUISITION_FOR_15_PERCENT"),
            "Max_acquisition_for_20%": dania_econ.get("MAX_PRODUCT_ACQUISITION_FOR_20_PERCENT"),
            "Freight_reserve": dania_econ.get("Freight_reserve"),
            "Financing_reserve": dania_econ.get("Financing_reserve"),
            "Economics_target_valid": dania_econ.get("Economics_target_valid"),
            "detail": dania_econ,
        },
        "BRIDGEPORT_REVENUE": {
            "Current_defensible_value": bridgeport.get("current_defensible_value"),
            "Source": bridgeport.get("source"),
            "Classification": bridgeport.get("classification"),
            "PASS_FAIL": bridgeport.get("PASS_FAIL"),
            "detail": bridgeport,
        },
        "GO_METRO_REVENUE": {
            "Current_defensible_value": go_metro.get("current_defensible_value"),
            "Source": go_metro.get("source"),
            "Classification": go_metro.get("classification"),
            "PASS_FAIL": go_metro.get("PASS_FAIL"),
            "detail": go_metro,
        },
        "DEKALB_REVENUE": {
            "Current_defensible_value": dekalb.get("current_defensible_value"),
            "Source": dekalb.get("source"),
            "Classification": dekalb.get("classification"),
            "PASS_FAIL": dekalb.get("PASS_FAIL"),
            "detail": dekalb,
        },
        "QUOTE_PACKET_AUDIT": {
            "Total_packets": packet_audit.get("total_packets"),
            "Passed": packet_audit.get("passed"),
            "Failed": packet_audit.get("failed"),
            "Lines_traceable": packet_audit.get("lines_traceable"),
            "Lines_untraceable": packet_audit.get("lines_untraceable"),
            "Quantity_diff": packet_audit.get("quantity_diff_abs_sum"),
            "Duplicate_lines": packet_audit.get("duplicate_lines"),
            "line_qty_mismatches": packet_audit.get("line_qty_mismatches"),
        },
        "OWNER_OUTREACH_PRIORITY": outreach,
        "QUOTE_INGESTION": {
            "PDF_parser": (ingestion.get("parser_results") or {}).get("PDF parser", {}).get("parsed_ok"),
            "Email_parser": (ingestion.get("parser_results") or {}).get("Email parser", {}).get("parsed_ok"),
            "Spreadsheet_parser": (ingestion.get("parser_results") or {}).get("Spreadsheet parser", {}).get(
                "parsed_ok"
            ),
            "Manual_entry": (ingestion.get("parser_results") or {}).get("Manual entry", {}).get("parsed_ok"),
            "Production_gate": ingestion.get("Production_gate"),
            "PASS_FAIL": ingestion.get("PASS_FAIL"),
            "automatic_recompute_wired": ingestion.get("automatic_recompute_wired"),
            "economics_recompute_sequence": ingestion.get("economics_recompute_sequence"),
        },
        "FULL_FUNNEL_GAP_AUDIT": stages,
        "P0_PRE_SCALE_GAPS": {
            "Count": gaps.get("P0_count"),
            "List": gaps.get("P0"),
        },
        "P1_LIVE_BID_GAPS": {
            "Count": gaps.get("P1_count"),
            "List": gaps.get("P1"),
        },
        "LARGE_TEST_READINESS": {
            "Ready_now": large.get("Ready_now"),
            "Must_fix_before_250_500": large.get("Must_fix_before_250_500"),
            "Can_defer_until_after_large_test": large.get("Can_defer_until_after_large_test"),
        },
        "LARGE_TEST_DESIGN": large.get("LARGE_TEST_DESIGN"),
        "SAFE_FOR_LARGE_TEST": large.get("SAFE_FOR_LARGE_TEST"),
        "LARGE_TEST_PASS": large.get("LARGE_TEST_PASS"),
        "SAFE_TO_FULL_SCALE": large.get("SAFE_TO_FULL_SCALE"),
        "GATE": {
            "do_not_send_automatically": True,
            "do_not_scale_universe": True,
            "NEXT_RUN_ALLOWED": "FIX_P0_THEN_OWNER_CHANNEL_TESTS"
            if large.get("SAFE_FOR_LARGE_TEST") == "NO"
            else "OWNER_QUOTE_OUTREACH",
            "owner_ready_packets_after_audit": owner_ready_n,
            "send_first_candidates": send_first,
        },
        "MOST_IMPORTANT_ANSWERS": {
            "1_dania_400k_usable_as_contract_product_revenue": False,
            "2_defensible_usable_value": None,
            "2_note": "Grant total $400K (FDEP $200K + match $200K) is program funding only; product-scope $ unknown",
            "3_dania_packets_source_perfect": False,
            "4_dania_beat_for_5k": None,
            "5_dania_beat_for_10k": None,
            "6_bridgeport_gained_current_revenue": bridgeport.get("PASS_FAIL") == "PASS",
            "7_go_metro_gained_meaningful_current_revenue": go_metro.get("PASS_FAIL") == "PASS",
            "8_dekalb_gained_meaningful_current_revenue": dekalb.get("PASS_FAIL") == "PASS",
            "9_owner_ready_packets_of_18": owner_ready_n,
            "10_packets_send_first": send_first,
            "11_quotes_flow_automatically_into_economics": False,
            "11_note": "Production gate + recompute sequence defined; auto-wiring PARTIAL",
            "12_p0_funnel_gaps": gaps.get("P0"),
            "13_p1_before_live_bid": gaps.get("P1"),
            "14_ready_for_250_500": large.get("Ready_now") == "YES",
            "15_must_fix_first": large.get("Must_fix_before_250_500"),
            "16_large_test_should_measure": (large.get("LARGE_TEST_DESIGN") or {}).get("metrics_required"),
        },
    }

    ck = {
        "build": BUILD,
        "updated_at": now_utc().isoformat(),
        "report_ready": True,
        "dania_revenue": dania_rev,
        "dania_scope": {k: v for k, v in dania_scope.items() if k != "lines"},
        "bridgeport": bridgeport,
        "go_metro": go_metro,
        "dekalb": dekalb,
        "packet_audit_summary": {
            k: packet_audit.get(k)
            for k in (
                "total_packets",
                "passed",
                "failed",
                "lines_traceable",
                "lines_untraceable",
                "duplicate_count",
                "quantity_diff_abs_sum",
            )
        },
        "outreach_top": outreach[:8],
        "SAFE_FOR_LARGE_TEST": large.get("SAFE_FOR_LARGE_TEST"),
    }

    _dump(CK, ck)
    _dump(REPORT, report)
    _dump(GAP_REGISTER, gaps)
    _dump(QUOTE_PACKETS_AUDITED, packet_audit)
    _dump(OUTREACH_QUEUE, {"build": BUILD, "queue": outreach, "do_not_send_automatically": True})
    _dump(QUOTE_REQUESTS, quote_texts)
    _dump(INGESTION_FIXTURES, ingestion.get("fixtures"))
    _dump(INGESTION_GATE, ingestion.get("Production_gate"))
    _dump(LARGE_TEST_DESIGN, large)

    # Harden prior revenue store classification for Dania (correction)
    _correct_revenue_store(dania_rev)

    return report


def _correct_revenue_store(dania_rev: dict[str, Any]) -> None:
    from final_pre_scale_proof.models import DANIA_OID, PRIOR_REV

    p = data_path(PRIOR_REV)
    if not p.exists():
        return
    try:
        store = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return
    by = store.setdefault("by_opportunity", {})
    entry = by.setdefault(DANIA_OID, {})
    rev = entry.setdefault("revenue", {})
    rev["final_pre_scale_correction"] = {
        "build": BUILD,
        "prior_classification": "CURRENT_CONTRACT_VALUE",
        "corrected_classification": (dania_rev.get("corrected") or {}).get("semantic_classification"),
        "usable_as_product_scope_revenue": False,
        "usable_as_program_funding_ceiling": True,
        "value": (dania_rev.get("corrected") or {}).get("value"),
        "PASS_FAIL": dania_rev.get("PASS_FAIL"),
        "source": dania_rev.get("source"),
    }
    # Downgrade best if it claimed CURRENT
    best = rev.get("best") or {}
    if best.get("reference_value") == 400000 or best.get("revenue_evidence_type") == "CURRENT_VALUE_EXPLICIT":
        best["semantic_role_corrected"] = "GRANT_TOTAL"
        best["usable_as_product_revenue"] = False
        best["note"] = "Corrected by final-pre-scale-proof: FDEP grant+match, not product contract value"
        rev["best"] = best
    p.write_text(json.dumps(store, indent=2), encoding="utf-8")


def format_report(report: dict[str, Any]) -> str:
    lines = [
        f"BUILD {report.get('build')}",
        "",
        "=== DANIA REVENUE VALIDATION ===",
        json.dumps(report.get("DANIA_REVENUE_VALIDATION"), indent=2, default=str)[:2500],
        "",
        "=== DANIA PRODUCT VALUE ===",
        json.dumps({k: v for k, v in (report.get("DANIA_PRODUCT_VALUE") or {}).items() if k != "lines"}, indent=2),
        "",
        "=== DANIA QUOTE PACKETS ===",
        json.dumps(report.get("DANIA_QUOTE_PACKETS"), indent=2),
        "",
        "=== DANIA TARGET ECONOMICS ===",
        json.dumps(report.get("DANIA_TARGET_ECONOMICS"), indent=2, default=str)[:1500],
        "",
        "=== BRIDGEPORT / GO-METRO / DEKALB ===",
        json.dumps(report.get("BRIDGEPORT_REVENUE"), indent=2, default=str)[:800],
        json.dumps(report.get("GO_METRO_REVENUE"), indent=2, default=str)[:800],
        json.dumps(report.get("DEKALB_REVENUE"), indent=2, default=str)[:800],
        "",
        "=== QUOTE PACKET AUDIT ===",
        json.dumps(report.get("QUOTE_PACKET_AUDIT"), indent=2, default=str)[:1200],
        "",
        "=== OWNER OUTREACH PRIORITY (top 8) ===",
        json.dumps((report.get("OWNER_OUTREACH_PRIORITY") or [])[:8], indent=2, default=str),
        "",
        "=== QUOTE INGESTION ===",
        json.dumps(report.get("QUOTE_INGESTION"), indent=2, default=str)[:1200],
        "",
        "=== P0 PRE-SCALE GAPS ===",
        json.dumps(report.get("P0_PRE_SCALE_GAPS"), indent=2, default=str)[:2000],
        "",
        "=== P1 LIVE-BID GAPS ===",
        json.dumps(report.get("P1_LIVE_BID_GAPS"), indent=2, default=str)[:1500],
        "",
        "=== LARGE TEST ===",
        json.dumps(report.get("LARGE_TEST_READINESS"), indent=2, default=str),
        f"SAFE_FOR_LARGE_TEST: {report.get('SAFE_FOR_LARGE_TEST')}",
        "",
        "=== MOST IMPORTANT ANSWERS ===",
        json.dumps(report.get("MOST_IMPORTANT_ANSWERS"), indent=2, default=str),
        "",
        "=== GATE ===",
        json.dumps(report.get("GATE"), indent=2, default=str),
    ]
    return "\n".join(lines)
