"""Run pre-bid transaction financing validation + artifacts (no financier outreach)."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from application_clock import (
    complete_run_metadata,
    now_utc,
    reset_clock,
    start_run_metadata,
)
from funding_source_research import build_production_funding_kb
from funding_underwriting import (
    CREDIT_NONE,
    GUARANTEE_NO_PG,
    build_personal_credit_model,
    classify_path_underwriting,
    parse_guarantee_state,
)
from pre_bid_transaction_financing import (
    assess_pre_bid_financing_maturity,
    build_composite_funding_path,
    build_financier_call_sheet,
    build_financier_packet,
    build_operator_pg_review,
    build_transaction_funding_requirement,
    calculate_actual_expected_profit,
    evaluate_funding_timing_compatibility,
    rank_provider_public_fit,
)

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts"
PACKET = ARTIFACTS / "transactional_procurement_packets" / "645-DOTRFB-2975-2027.json"


def _enrich_underwriting_profiles(kb: dict) -> list[dict]:
    profiles = []
    for p in kb.get("programs") or []:
        credit = build_personal_credit_model(p)
        guarantee = parse_guarantee_state(
            personal_guarantee=p.get("personal_guarantee"),
            guarantee_type=p.get("guarantee_type"),
            evidence_status=((p.get("criteria_evidence") or {}).get("personal_guarantee") or {}).get(
                "verification_status"
            ),
        )
        uw = classify_path_underwriting(p, operator_personal_fico=480)
        fit = rank_provider_public_fit(p, deal={"operator_personal_fico": 480, "government_customer_type": "state"})
        profiles.append(
            {
                "organization": p.get("organization") or p.get("source_name"),
                "program": p.get("program"),
                "provider_role": p.get("provider_role") or p.get("role"),
                "path_type": p.get("path_type"),
                "website": p.get("website"),
                "personal_credit": credit,
                "personal_guarantee": guarantee,
                "underwriting_path_class": uw.get("path_class"),
                "public_fit": fit.get("fit_state"),
                "startup_allowed": p.get("startup_allowed"),
                "first_government_contract_allowed": p.get("first_government_contract_allowed"),
                "minimum_personal_fico": p.get("minimum_personal_fico"),
                "personal_credit_checked": p.get("personal_credit_checked"),
                "borrower_cash_contribution_required": p.get("borrower_cash_contribution_required"),
                "maximum_supplier_cost_coverage_pct": p.get("maximum_supplier_cost_coverage_pct"),
                "government_customer_types_supported": p.get("government_customer_types_supported"),
                "product_resale_fit": p.get("product_resale_fit"),
                "launch_fit": p.get("launch_fit"),
                "call_priority": p.get("call_priority"),
                "official_urls_reviewed": p.get("official_urls_reviewed") or ([p.get("website")] if p.get("website") else []),
                "criteria_evidence_keys": list((p.get("criteria_evidence") or {}).keys()),
                "retrieval_timestamp": now_utc().isoformat(),
                "confidence": "MEDIUM",
                "unknowns_requiring_pre_bid_call": [
                    k
                    for k, v in {
                        "personal_guarantee": p.get("personal_guarantee"),
                        "personal_credit_checked": p.get("personal_credit_checked"),
                        "minimum_personal_fico": p.get("minimum_personal_fico"),
                        "borrower_cash_contribution_required": p.get("borrower_cash_contribution_required"),
                        "maximum_supplier_cost_coverage_pct": p.get("maximum_supplier_cost_coverage_pct"),
                        "startup_allowed": p.get("startup_allowed"),
                        "first_government_contract_allowed": p.get("first_government_contract_allowed"),
                    }.items()
                    if v is None or v == "UNKNOWN"
                ],
            }
        )
    return profiles


def _provider_summary(profiles: list[dict]) -> dict:
    def names(pred):
        return sorted({p["organization"] for p in profiles if pred(p) and p.get("organization")})

    return {
        "explicit_no_good_personal_credit_required": names(
            lambda p: (p.get("personal_credit") or {}).get("poor_credit_explicitly_acceptable") is True
        ),
        "explicit_no_personal_credit_pull": names(
            lambda p: (p.get("personal_credit") or {}).get("personal_credit_checked") == CREDIT_NONE
            or p.get("personal_credit_checked") is False
        ),
        "verified_minimum_fico": [
            {"organization": p["organization"], "minimum_fico": p.get("minimum_personal_fico")}
            for p in profiles
            if p.get("minimum_personal_fico") is not None
        ],
        "require_pg": names(
            lambda p: (p.get("personal_guarantee") or {}).get("guarantee_state")
            not in {GUARANTEE_NO_PG, "UNKNOWN", None}
            and p.get("personal_guarantee", {}).get("guarantee_state") != "UNKNOWN"
        ),
        "explicit_no_pg": names(
            lambda p: (p.get("personal_guarantee") or {}).get("guarantee_state") == GUARANTEE_NO_PG
        ),
        "explicitly_support_startups": names(lambda p: p.get("startup_allowed") is True),
        "explicitly_support_first_contracts": names(
            lambda p: p.get("first_government_contract_allowed") is True
        ),
        "can_fund_100_pct_cogs": names(
            lambda p: p.get("maximum_supplier_cost_coverage_pct") is not None
            and float(p.get("maximum_supplier_cost_coverage_pct") or 0) >= 100
        ),
        "support_government_product_transactions": names(
            lambda p: p.get("product_resale_fit") in {"STRONG", "POSSIBLE"}
            or (isinstance(p.get("government_customer_types_supported"), list) and p.get("government_customer_types_supported"))
        ),
        "unknowns_requiring_pre_bid_call_by_provider": {
            p["organization"]: p.get("unknowns_requiring_pre_bid_call")
            for p in profiles
            if p.get("unknowns_requiring_pre_bid_call")
        },
    }


def run_laptop_validation() -> dict:
    """Synthetic TEST_FIXTURE only — must not write into live opportunity packets as production costs."""
    econ = calculate_actual_expected_profit(
        government_revenue=100000,
        supplier_cost=75000,
        freight=1000,
        transaction_expenses=500,
        po_finance_fees=3500,
        factoring_fees=1000,
    )
    req = build_transaction_funding_requirement(
        {
            "supplier_cost": 75000,
            "freight": 1000,
            "other_pre_payment_costs": 500,
            "government_revenue": 100000,
            "data_class": "TEST_FIXTURE",
        }
    )
    composite = build_composite_funding_path(
        req,
        [
            {
                "component": "PO_FINANCE",
                "amount": 76500,
                "fees": 3500,
                "evidence_state": "VERIFIED_PUBLIC",
                "percentage": 100,
                "repayment_source": "government_receivable",
            },
            {
                "component": "AR_FACTORING",
                "amount": 0,
                "fees": 1000,
                "evidence_state": "VERIFIED_PUBLIC",
                "repayment_source": "government_payment",
            },
        ],
    )
    timing = evaluate_funding_timing_compatibility(
        days_until_supplier_payment=21, typical_funding_days=5, application_days=2
    )
    return {
        "data_class": "TEST_FIXTURE",
        "contaminates_live_opportunities": False,
        "economics": econ,
        "requirement": req,
        "composite": composite,
        "timing": timing,
        "expected_profit": 19000,
        "passed": econ.get("actual_expected_profit") == 19000 and econ.get("profit_floor_satisfied") is True,
    }


def run_iowa_validation() -> dict:
    packet = {}
    if PACKET.exists():
        packet = json.loads(PACKET.read_text(encoding="utf-8"))
    req = packet.get("requirement") or {}
    econ = packet.get("economics_block") or {}
    maturity = assess_pre_bid_financing_maturity(
        exact_product_known=False,  # gated spec missing
        supplier_cost_known=False,
        government_revenue_defined=False,
        funding_amount_estimable=False,
        freight_known=False,
    )
    funding_fields = {
        "pre_bid_financing_status": maturity["pre_bid_financing_status"],
        "financier_outreach_appropriate_now": maturity["financier_outreach_appropriate"],
        "reason": "Authoritative gated blade spec, supplier quotes, freight, and defensible supplier cost still missing",
        "likely_provider_categories": [
            "PURCHASE_ORDER_FINANCING",
            "TRADE_FINANCE",
            "GOVERNMENT_RECEIVABLE_FINANCING",
            "HYBRID_PO_PLUS_FACTORING",
        ],
        "personal_credit_model": "operator_fico_approx_480 — evaluate per-provider pull/role/min FICO; not global veto",
        "personal_guarantee_model": "PG alone is OPERATOR_PG_REVIEW — not auto-reject",
        "remaining_prerequisites": [
            "authorized download of Snow Plow Blade Spec 8Sept20263.pdf",
            "supplier quotes for item codes",
            "freight to Ames / delivery point",
            "defensible landed cost",
            "proposed bid economics / pricing range",
            "funding amount and dates",
        ],
        "evaluated_at": now_utc().isoformat(),
        "clock_mode": "SYSTEM",
        "financier_outreach": 0,
        "agency_outreach": 0,
        "supplier_outreach": 0,
        "bid_submissions": 0,
    }
    if PACKET.exists():
        packet["pre_bid_transaction_financing"] = funding_fields
        # Do not overwrite procurement evidence / invent costs
        PACKET.write_text(json.dumps(packet, indent=2, default=str), encoding="utf-8")
        md = PACKET.with_suffix(".md")
        if md.exists():
            block = f"""

## Pre-bid transaction financing maturity
- status: {funding_fields['pre_bid_financing_status']}
- financier outreach appropriate NOW: {funding_fields['financier_outreach_appropriate_now']}
- reason: {funding_fields['reason']}
- remaining prerequisites: {', '.join(funding_fields['remaining_prerequisites'])}
"""
            text = md.read_text(encoding="utf-8")
            if "## Pre-bid transaction financing maturity" in text:
                text = text.split("## Pre-bid transaction financing maturity")[0].rstrip() + "\n" + block
            else:
                text = text.rstrip() + "\n" + block
            md.write_text(text, encoding="utf-8")
    return funding_fields


def main() -> None:
    reset_clock()
    meta = start_run_metadata(extra={"run_kind": "pre_bid_transaction_financing"})
    kb = build_production_funding_kb()
    profiles = _enrich_underwriting_profiles(kb)
    summary = _provider_summary(profiles)
    laptop = run_laptop_validation()
    iowa = run_iowa_validation()

    ARTIFACTS.mkdir(exist_ok=True)
    (ARTIFACTS / "funding_underwriting_profiles.json").write_text(
        json.dumps({"generated_at": now_utc().isoformat(), "profiles": profiles, "summary": summary}, indent=2, default=str),
        encoding="utf-8",
    )

    # CSV
    with (ARTIFACTS / "funding_underwriting_profiles.csv").open("w", newline="", encoding="utf-8") as f:
        fields = [
            "organization",
            "program",
            "provider_role",
            "path_type",
            "underwriting_path_class",
            "public_fit",
            "personal_credit_checked",
            "minimum_personal_fico",
            "guarantee_state",
            "startup_allowed",
            "first_government_contract_allowed",
            "max_cogs_pct",
            "product_resale_fit",
        ]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for p in profiles:
            w.writerow(
                {
                    "organization": p.get("organization"),
                    "program": p.get("program"),
                    "provider_role": p.get("provider_role"),
                    "path_type": p.get("path_type"),
                    "underwriting_path_class": p.get("underwriting_path_class"),
                    "public_fit": p.get("public_fit"),
                    "personal_credit_checked": (p.get("personal_credit") or {}).get("personal_credit_checked"),
                    "minimum_personal_fico": p.get("minimum_personal_fico"),
                    "guarantee_state": (p.get("personal_guarantee") or {}).get("guarantee_state"),
                    "startup_allowed": p.get("startup_allowed"),
                    "first_government_contract_allowed": p.get("first_government_contract_allowed"),
                    "max_cogs_pct": p.get("maximum_supplier_cost_coverage_pct"),
                    "product_resale_fit": p.get("product_resale_fit"),
                }
            )

    evidence = {
        "generated_at": now_utc().isoformat(),
        "evidence_class_preference": "PROVIDER_OFFICIAL_CURRENT",
        "http_requests_this_run": 0,
        "note": "Profiles derived from existing seeded official-provider evidence; no new financier contact",
        "profiles": [
            {
                "organization": p["organization"],
                "urls": p.get("official_urls_reviewed"),
                "evidence_keys": p.get("criteria_evidence_keys"),
                "unknowns": p.get("unknowns_requiring_pre_bid_call"),
            }
            for p in profiles
        ],
    }
    (ARTIFACTS / "funding_provider_evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")

    # Pre-bid financing queue — only mature deals; Iowa is NOT ready
    queue_rows = []
    if iowa.get("financier_outreach_appropriate_now"):
        queue_rows.append({"solicitation": "645-DOTRFB-2975-2027", "status": iowa["pre_bid_financing_status"]})
    with (ARTIFACTS / "pre_bid_financing_queue.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=["solicitation", "status", "outreach_appropriate", "note"],
        )
        w.writeheader()
        w.writerow(
            {
                "solicitation": "645-DOTRFB-2975-2027",
                "status": iowa["pre_bid_financing_status"],
                "outreach_appropriate": iowa["financier_outreach_appropriate_now"],
                "note": iowa["reason"],
            }
        )

    (ARTIFACTS / "funding_timing_validation.json").write_text(
        json.dumps({"laptop_fixture": laptop["timing"], "iowa": {"status": "FUNDING_TIMING_UNKNOWN", "reason": "no_supplier_payment_dates"}}, indent=2),
        encoding="utf-8",
    )
    (ARTIFACTS / "funding_economics_validation.json").write_text(
        json.dumps({"laptop_fixture": laptop, "iowa_live_economics": "UNKNOWN_insufficient_cost_evidence"}, indent=2, default=str),
        encoding="utf-8",
    )

    sheet = build_financier_call_sheet(
        provider={"source_name": "Generic PO / Government Contract Financier"},
        deal={"solicitation_number": "645-DOTRFB-2975-2027"},
    )
    call_md = f"""# Financier Call Sheet Template

Generated: {now_utc().isoformat()}

## Opening
{sheet['opening_script']}

## Questions
"""
    for q in sheet["questions"]:
        call_md += f"{len(call_md.splitlines())-5}. **{q['key']}**: {q['question']}\n"
    call_md += """
## Rules
- Personal credit questions are separate from personal guarantee questions.
- Public marketing claims are not deal approval.
- Do not autonomously call financiers.
"""
    (ARTIFACTS / "financier_call_sheet_template.md").write_text(call_md, encoding="utf-8")

    meta = complete_run_metadata(meta)
    report = f"""# Funding Underwriting Report

Generated: {now_utc().isoformat()}
Clock mode: {meta.get('clock_mode')}

## Hierarchy
PREFERRED / WORKABLE_WITH_OPERATOR_APPROVAL / NEEDS_VERIFICATION / REJECT
Personal guarantee alone does **not** auto-REJECT.

## Provider summary
{json.dumps(summary, indent=2)}

## Controlled laptop validation (TEST_FIXTURE)
- expected profit: $19,000
- passed: {laptop['passed']}
- contaminates live: {laptop['contaminates_live_opportunities']}

## Iowa blade 645-DOTRFB-2975-2027
- pre-bid status: {iowa['pre_bid_financing_status']}
- financier outreach appropriate NOW: {iowa['financier_outreach_appropriate_now']}
- reason: {iowa['reason']}

## Request counts
public_http_search: 0 (reused seeded official evidence)
SAM: 0
OpenAI: 0
financier_outreach: 0
supplier_outreach: 0
agency_outreach: 0
bid_submissions: 0

NEXT STATE:
PRE_BID_TRANSACTION_FINANCING_INTELLIGENCE_OPERATIONAL
"""
    (ARTIFACTS / "funding_underwriting_report.md").write_text(report, encoding="utf-8")
    print(
        json.dumps(
            {
                "providers": len(profiles),
                "laptop_passed": laptop["passed"],
                "iowa_outreach_now": iowa["financier_outreach_appropriate_now"],
                "iowa_status": iowa["pre_bid_financing_status"],
                "summary_keys": list(summary.keys()),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
