"""Phases 15–20 — Funnel gap audit, pre-scale register, large-test design."""

from __future__ import annotations

from typing import Any

from final_pre_scale_proof.models import (
    BLOCKED,
    BUILT_NOT_PROVEN,
    FUNNEL_STAGES,
    NOT_BUILT,
    PARTIAL,
    PROVEN,
)


def funnel_gap_audit(
    *,
    dania_rev: dict[str, Any],
    packet_audit: dict[str, Any],
    ingestion: dict[str, Any],
) -> list[dict[str, Any]]:
    """Classify every canonical funnel stage from actual system evidence."""
    packet_pass_rate = 0.0
    total = packet_audit.get("total_packets") or 0
    if total:
        packet_pass_rate = (packet_audit.get("passed") or 0) / total

    stages: list[dict[str, Any]] = [
        {
            "Stage": "DISCOVERY",
            "Status": PARTIAL,
            "Evidence": "OpenGov/BidNet/SAM harvests operate; national mix incomplete",
            "Gap": "Representative multi-source coverage not proven at 250–500",
            "Severity": "P1",
        },
        {
            "Stage": "CANONICALIZATION",
            "Status": PARTIAL,
            "Evidence": "Canonical opportunity IDs exist; operator UI still has parallel paths",
            "Gap": "Canonical operator UI parallel paths",
            "Severity": "P0",
        },
        {
            "Stage": "PRODUCT QUALIFICATION",
            "Status": PARTIAL,
            "Evidence": "Product filters run; false positives (construction as product) observed on Dania",
            "Gap": "Install/construction lines leaking into product/quote-ready",
            "Severity": "P0",
        },
        {
            "Stage": "PACKAGE",
            "Status": PARTIAL,
            "Evidence": "OpenGov public docs recovered for deep corpus",
            "Gap": "Package completeness + page-level provenance uneven",
            "Severity": "P1",
        },
        {
            "Stage": "ELIGIBILITY",
            "Status": PARTIAL,
            "Evidence": "Eligibility mining exists",
            "Gap": "Not proven end-to-end on large representative sample",
            "Severity": "P2",
        },
        {
            "Stage": "LINE EXTRACTION",
            "Status": PARTIAL,
            "Evidence": "341 material lines in deep corpus",
            "Gap": "OCR/fragment lines; missing page/sheet/cell",
            "Severity": "P0",
        },
        {
            "Stage": "IDENTITY",
            "Status": PARTIAL,
            "Evidence": "314 usable identities A–E class in deep completion",
            "Gap": "False MPN/model extractions (planting notes, size fragments)",
            "Severity": "P0",
        },
        {
            "Stage": "REVENUE",
            "Status": PARTIAL,
            "Evidence": f"Dania $400K corrected to GRANT_TOTAL (PASS_FAIL={dania_rev.get('PASS_FAIL')})",
            "Gap": "Grant/program funding classifier fixed this build; product-scope allocation from grant totals still NOT_READY",
            "Severity": "P0",
        },
        {
            "Stage": "ACQUISITION",
            "Status": PARTIAL,
            "Evidence": "Public price attempts mostly → quote reserve",
            "Gap": "Live public pricing success rate near zero",
            "Severity": "P1",
        },
        {
            "Stage": "QUOTE RESERVE",
            "Status": PARTIAL,
            "Evidence": "18 packets consolidated; owner queue exists",
            "Gap": f"Source-trace packet pass {packet_audit.get('passed')}/{total}; page provenance missing",
            "Severity": "P0",
        },
        {
            "Stage": "BASKET",
            "Status": PARTIAL,
            "Evidence": "Strict economics rejects thin/fake baskets",
            "Gap": "Basket completion behavior under incomplete quotes not proven",
            "Severity": "P0",
        },
        {
            "Stage": "FREIGHT",
            "Status": BUILT_NOT_PROVEN,
            "Evidence": "Freight reserve % modeled",
            "Gap": "No real freight quotes ingested into economics",
            "Severity": "P1",
        },
        {
            "Stage": "FINANCING",
            "Status": BUILT_NOT_PROVEN,
            "Evidence": "Financing reserve / FINANCEABLE_MODELED on some opps",
            "Gap": "Lender path not proven with real quotes",
            "Severity": "P1",
        },
        {
            "Stage": "ECONOMICS",
            "Status": PARTIAL,
            "Evidence": "Fail-closed classifiers; Dania/Bridgeport ceilings invalidated when revenue weak",
            "Gap": "Automatic recompute on quote receipt not fully wired",
            "Severity": "P0",
        },
        {
            "Stage": "EXECUTION",
            "Status": PARTIAL,
            "Evidence": "Execution review flags on deep corpus",
            "Gap": "Install-heavy vs product-supply not consistently blocked",
            "Severity": "P1",
        },
        {
            "Stage": "LENDER READY",
            "Status": BUILT_NOT_PROVEN,
            "Evidence": "Pipeline stubs exist",
            "Gap": "No lender-ready packet proven with real supplier quotes",
            "Severity": "P1",
        },
        {
            "Stage": "BID READY",
            "Status": NOT_BUILT,
            "Evidence": "No bid package assembly proven",
            "Gap": "Bid forms / attachments / compliance package",
            "Severity": "P1",
        },
        {
            "Stage": "SUBMISSION",
            "Status": NOT_BUILT,
            "Evidence": "Outreach/send gated off",
            "Gap": "Submission workflow not built",
            "Severity": "P2",
        },
        {
            "Stage": "AWARD",
            "Status": NOT_BUILT,
            "Evidence": None,
            "Gap": "Award intake / PO matching",
            "Severity": "P2",
        },
        {
            "Stage": "FULFILLMENT",
            "Status": NOT_BUILT,
            "Evidence": None,
            "Gap": "Order / ship / install tracking",
            "Severity": "P3",
        },
        {
            "Stage": "INVOICE",
            "Status": NOT_BUILT,
            "Evidence": None,
            "Gap": "Invoice generation",
            "Severity": "P3",
        },
        {
            "Stage": "PAYMENT",
            "Status": NOT_BUILT,
            "Evidence": None,
            "Gap": "Payment reconciliation",
            "Severity": "P3",
        },
        {
            "Stage": "LEARNING",
            "Status": PARTIAL,
            "Evidence": "Checkpoints / reports persist",
            "Gap": "Closed-loop learning from wins/losses not proven",
            "Severity": "P2",
        },
    ]
    # Ensure all declared stages present
    seen = {s["Stage"] for s in stages}
    for st in FUNNEL_STAGES:
        if st not in seen:
            stages.append(
                {"Stage": st, "Status": NOT_BUILT, "Evidence": None, "Gap": "unreviewed", "Severity": "P2"}
            )
    return stages


def build_gap_register(stages: list[dict[str, Any]]) -> dict[str, Any]:
    gaps = []
    for s in stages:
        if s["Status"] in {PROVEN}:
            continue
        sev = s.get("Severity") or "P2"
        gaps.append(
            {
                "stage": s["Stage"],
                "gap": s.get("Gap"),
                "severity": sev,
                "status": s["Status"],
                "production_impact": _impact(sev, s["Stage"]),
                "fix_required_before_250_500": sev in {"P0"},
                "fix_required_before_live_bidding": sev in {"P0", "P1"},
                "fix_required_before_award_execution": sev in {"P0", "P1", "P2"},
                "recommended_phase": _phase(sev),
            }
        )
    p0 = [g for g in gaps if g["severity"] == "P0"]
    p1 = [g for g in gaps if g["severity"] == "P1"]
    return {
        "register": "PRE_SCALE_GAP_REGISTER_V1",
        "gaps": gaps,
        "P0_count": len(p0),
        "P1_count": len(p1),
        "P0": p0,
        "P1": p1,
        "P2": [g for g in gaps if g["severity"] == "P2"],
        "P3": [g for g in gaps if g["severity"] == "P3"],
    }


def _impact(sev: str, stage: str) -> str:
    if sev == "P0":
        return f"Blocks honest large-test measurement at {stage}"
    if sev == "P1":
        return f"Blocks live bidding / lender trust at {stage}"
    if sev == "P2":
        return f"Limits scale operations at {stage}"
    return f"Post-award capability gap at {stage}"


def _phase(sev: str) -> str:
    return {
        "P0": "BEFORE_LARGE_TEST",
        "P1": "BEFORE_LIVE_BID",
        "P2": "BEFORE_AWARD_EXECUTION",
        "P3": "POST_AWARD",
    }.get(sev, "TBD")


def large_test_design(*, gap_register: dict[str, Any]) -> dict[str, Any]:
    p0 = gap_register.get("P0") or []
    ready_now = len(p0) == 0
    must_fix = [
        g["gap"] for g in p0
    ] or [
        "Resolve P0 gaps listed in PRE_SCALE_GAP_REGISTER_V1",
    ]
    defer = [g["gap"] for g in (gap_register.get("P2") or []) + (gap_register.get("P3") or [])]

    design = {
        "Sample_size": "250–500 real opportunities (target 400)",
        "Source_mix": {
            "federal_SAM": ">=15%",
            "state": ">=20%",
            "local": ">=30%",
            "education": ">=10%",
            "utilities_if_available": ">=5%",
            "remainder": "natural harvest mix — no cherry-picking",
        },
        "Opportunity_mix": {
            "single_line": "15–25%",
            "multiline": "75–85%",
            "common_products": "included",
            "specialty_OEM": "included",
            "public_price": "included",
            "quote_required": "included",
            "brand_equal": "included",
            "generic_spec": "included",
        },
        "Max_runtime": "bounded staged batches; checkpoint every 25 opps; no unbounded shell",
        "Checkpointing": "required — resume-safe per opportunity",
        "Cost_caps": {
            "AI_USD": 150,
            "SAM_calls": 2000,
            "browser_sessions": 100,
            "live_public_price_attempts_per_opp": 6,
        },
        "Success_gates": [
            "funnel conservation pass",
            "zero revenue contamination (bond/insurance/grant-as-contract)",
            "economics fail-closed on thin baskets",
            "packet source provenance >= threshold OR explicit PACKET_FAIL",
            "quote fixtures cannot enter production economics",
            "checkpoint/resume works",
            "owner UI shows same stage as backend",
        ],
        "metrics_required": [
            "sample_size",
            "product_qualified",
            "package_verified",
            "eligible",
            "identity_ready",
            "revenue_ready",
            "public_priced",
            "quote_required",
            "basket_ready",
            "economics_ready",
            "profitable",
            "ge_5k",
            "ge_10k",
            "lender_ready",
            "bid_ready",
            "funnel_conservation",
            "runtime",
            "AI_cost",
            "SAM_calls",
            "browser_usage",
            "retry_rates",
            "cache_hit_rate",
        ],
    }

    large_test_pass = {
        "name": "LARGE_TEST_PASS",
        "criteria": [
            "N in 250–500 with representative mix",
            "conservation identity: inputs = outputs + explained drops",
            "contamination = 0 (bond/insurance/grant misread as product revenue)",
            "economics fail-closed: no sentinel/$1/search-URL profits",
            ">=1 repeatable public-price path OR quote path proven on real (non-fixture) artifact",
            "basket completion behavior documented for partial quotes",
            "owner UI stage == backend stage on sampled opps",
            "checkpoint/resume verified mid-run",
            "cost caps not exceeded without explicit owner override",
        ],
    }
    safe_full_scale = {
        "name": "SAFE_TO_FULL_SCALE",
        "criteria": [
            "LARGE_TEST_PASS",
            "proven source provenance on quote packets",
            "repeatable acquisition path",
            "repeatable quote ingestion → economics auto-recompute",
            "revenue semantics regression suite green (grant/bond/insurance)",
            "no single-contract profitability used as scale justification",
            "controlled costs at large-test rates extrapolated",
        ],
        "note": "Do not mark SAFE_TO_FULL_SCALE from one profitable contract",
    }

    return {
        "Ready_now": "YES" if ready_now else "NO",
        "Must_fix_before_250_500": must_fix,
        "Can_defer_until_after_large_test": defer[:12],
        "LARGE_TEST_DESIGN": design,
        "LARGE_TEST_PASS": large_test_pass,
        "SAFE_TO_FULL_SCALE": safe_full_scale,
        "SAFE_FOR_LARGE_TEST": "YES" if ready_now else "NO",
        "packet_pass_rate_note": "Owner-ready packets require source page/sheet trace — currently failing audit",
    }
