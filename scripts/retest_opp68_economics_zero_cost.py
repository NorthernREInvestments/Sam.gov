"""ZERO-COST Opp 68 economic retest from cached Stage 1 + Stage 2.

NO OpenAI. NO SAM. NO web. Recalculates economics via local normalize only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

EXPECTED_S1 = "526f0a0fd7a304b3308cd8c48402c3e35a3638667b8179533906cc108bba6eb9"
EXPECTED_S2 = "9a90fc474c003967f31fd8c7cbd7ecf625da3b7bc261c8817caee9118e8c724a"


class LiveApiAttempted(RuntimeError):
    pass


def main() -> int:
    import openai_runtime
    from ai_funnel import stage0_evaluate
    from ai_stage1 import resolve_current_stage1_result
    from ai_stage2 import run_stage2_evidence
    from database import SessionLocal
    from models import Contract

    openai_calls = {"n": 0}

    def blocked(*_a, **_k):
        openai_calls["n"] += 1
        raise LiveApiAttempted("LIVE OpenAI forbidden in zero-cost retest")

    openai_runtime.get_openai_client = blocked  # type: ignore[assignment]

    session = SessionLocal()
    try:
        c = session.query(Contract).filter_by(id=68).one()
        stage0 = stage0_evaluate(c)
        s1_res = resolve_current_stage1_result(c, stage0=stage0)
        stage1 = s1_res.get("result") or {}
        if not s1_res.get("current") or not stage1:
            print(json.dumps({"error": "stage1_missing", "resolution": s1_res}, default=str, indent=2))
            return 2

        result = run_stage2_evidence(
            c,
            stage0=stage0,
            stage1=stage1,
            stage1_resolution=s1_res,
            automatic=False,
        )
    finally:
        session.close()

    econ = result.get("economic_requirements") or {}
    costs = econ.get("costs") or {}
    research = result.get("research_needs") or []
    codes = [n.get("code") for n in research]

    report = {
        "LIVE_API_REQUESTS": openai_calls["n"],
        "cache_hit": result.get("cache_hit"),
        "reason_code": result.get("reason_code"),
        "stage1_category": stage1.get("category"),
        "stage1_fingerprint": s1_res.get("fingerprint"),
        "stage1_fp_expected": EXPECTED_S1,
        "canonical_execution_class": econ.get("canonical_execution_class"),
        "classification_resolution": econ.get("classification_resolution"),
        "Supplier": _cost_summary(costs.get("supplier")),
        "Freight": _cost_summary(costs.get("freight")),
        "Installation": _cost_summary(costs.get("installation")),
        "Subcontract": _cost_summary(costs.get("subcontract")),
        "Financing": _cost_summary(costs.get("financing")),
        "actual_profit": econ.get("actual_profit"),
        "actual_profit_status": econ.get("actual_profit_status"),
        "research_need_codes": codes,
        "stage2_descriptive_category": (
            ((result.get("facts") or {}).get("procurement") or {}).get("category") or {}
        ).get("value"),
    }
    print(json.dumps(report, indent=2, default=str))

    if openai_calls["n"] != 0:
        return 3
    if report["Subcontract"].get("status") != "REQUIRED_UNKNOWN":
        return 4
    if report["Supplier"].get("status") == "REQUIRED_UNKNOWN" and not (
        (econ.get("classification_resolution") or {}).get("has_product_procurement_evidence")
    ):
        return 5
    if "SUBCONTRACTOR_QUOTE_REQUIRED" not in codes:
        return 6
    if report["actual_profit"] is not None or report["actual_profit_status"] != "INCOMPLETE":
        return 7
    # Soft check fingerprints when present
    if s1_res.get("fingerprint") and s1_res.get("fingerprint") != EXPECTED_S1:
        report["note"] = "stage1 fingerprint differs from expected constant (report only)"
    return 0


def _cost_summary(item: dict | None) -> dict:
    if not item:
        return {"status": None}
    return {
        "status": item.get("status"),
        "required": item.get("required"),
        "basis": item.get("basis"),
        "value": item.get("value"),
    }


if __name__ == "__main__":
    sys.exit(main())
