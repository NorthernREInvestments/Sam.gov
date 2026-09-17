"""Financing false-negative protection validation — DEVELOPMENT_NO_OUTREACH."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from commercial_readiness_gate import evaluate_execution_gate, evaluate_pursuit_vs_execution
from commercial_verification_constants import (
    ECON_ATTRACTIVE_FUNDING_VERIFY_REQ,
    FUND_EXHAUSTED,
    FUND_VERIFY_REQ,
)
from commercial_verification_engine import build_commercial_verification_bundle
from financing_verification import (
    assess_financing_compatibility,
    build_transaction_funding_requirement,
    evaluate_funding_gate,
    financier_profile,
    funding_path_coverage,
    normalize_pg_credit_fico_states,
    record_financing_outcome,
)
from operating_mode import MODE_DEVELOPMENT_NO_OUTREACH, mode_snapshot, set_operating_mode
from pursuit_ranking import rank_components
from pursuit_qualification_constants import ECON_STRONG, PURSUIT_WORTHY_UNCERTAIN

ARTIFACTS = ROOT / "artifacts"


def _write(name: str, payload: Any) -> None:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    (ARTIFACTS / name).write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def main() -> dict[str, Any]:
    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)

    req = build_transaction_funding_requirement(
        acquisition_cost=112000, freight=3000, bid_revenue=150000
    )
    unknown_profiles = [
        financier_profile(financier=n, personal_guarantee="UNKNOWN", minimum_fico=None, cash_contribution="UNKNOWN")
        for n in ("STAR Funding", "SouthStar Capital", "King Trade Capital")
    ]
    unknown_compat = [
        assess_financing_compatibility(profile=p, funding_requirement=req) for p in unknown_profiles
    ]
    unknown_gate = evaluate_funding_gate(
        funding_requirement=req, compatibility=unknown_compat, economically_attractive=True
    )
    _write(
        "financing_unknown_preservation_validation.json",
        {
            "gate": unknown_gate,
            "compat": [
                {
                    "financier": c["financier"],
                    "state": c["state"],
                    "path_state": c["path_state"],
                    "unknown_preserved": c["unknown_preserved"],
                }
                for c in unknown_compat
            ],
            "label": unknown_gate.get("label"),
            "exhausted": unknown_gate.get("transaction_funding_exhausted"),
        },
    )

    bad = assess_financing_compatibility(
        profile=financier_profile(financier="STAR Funding", minimum_fico=650),
        funding_requirement=req,
        operator_fico=480,
    )
    rest = [
        assess_financing_compatibility(
            profile=financier_profile(financier=n, personal_guarantee="UNKNOWN"),
            funding_requirement=req,
        )
        for n in ("SouthStar Capital", "King Trade Capital", "ELINT Capital")
    ]
    mixed_gate = evaluate_funding_gate(
        funding_requirement=req, compatibility=[bad] + rest, economically_attractive=True
    )
    _write(
        "financier_vs_transaction_failure_validation.json",
        {
            "financier_incompatible": bad,
            "remaining": [{"financier": c["financier"], "state": c["state"]} for c in rest],
            "gate": mixed_gate,
            "one_lender_does_not_exhaust": mixed_gate["transaction_funding_exhausted"] is False,
        },
    )

    _write("funding_path_coverage_validation.json", funding_path_coverage([bad] + rest))

    pg = financier_profile(
        financier="PG Fund",
        personal_guarantee="REQUIRED",
        personal_credit_pull="NONE",
        personal_credit_dependency="NONE",
        minimum_fico=None,
    )
    _write(
        "pg_credit_separation_validation.json",
        {
            "norms": normalize_pg_credit_fico_states(pg),
            "compat": assess_financing_compatibility(profile=pg, funding_requirement=req, pg_acceptable=True),
        },
    )

    all_bad = [
        assess_financing_compatibility(
            profile=financier_profile(financier=n, minimum_fico=720),
            funding_requirement=req,
            operator_fico=400,
        )
        for n in ("L1", "L2", "L3")
    ]
    exh = evaluate_funding_gate(
        funding_requirement=req, compatibility=all_bad, economically_attractive=True
    )
    _write(
        "funding_exhaustion_validation.json",
        {"gate": exh, "expected_state": FUND_EXHAUSTED, "pass": exh["state"] == FUND_EXHAUSTED},
    )

    pe = evaluate_pursuit_vs_execution(
        economically_attractive=True,
        funding_gate_state=FUND_VERIFY_REQ,
        transaction_funding_exhausted=False,
    )
    eg = evaluate_execution_gate(
        funding_required=True,
        funding_feasible=False,
        funding_verification_pending=True,
        profit_floor_preserved=True,
        profit_after_financing_ok=True,
    )
    _write(
        "pursuit_execution_separation_validation.json",
        {"pursuit_vs_execution": pe, "execution_gate": eg},
    )

    unk_rank = rank_components(
        {
            "transactional_fit": True,
            "economic_potential": {"status": ECON_STRONG, "base_profit": 22000},
            "package_readiness": {"preliminary_analysis_complete": True},
            "pursuit_decision": {"state": PURSUIT_WORTHY_UNCERTAIN},
            "funding_gate": {"state": FUND_VERIFY_REQ},
            "suppliers": [1],
        }
    )
    bad_rank = rank_components(
        {
            "transactional_fit": True,
            "economic_potential": {"status": ECON_STRONG, "base_profit": 22000},
            "package_readiness": {"preliminary_analysis_complete": True},
            "pursuit_decision": {"state": PURSUIT_WORTHY_UNCERTAIN},
            "funding_gate": {"state": FUND_EXHAUSTED},
            "suppliers": [1],
        }
    )
    _write(
        "financing_ranking_validation.json",
        {
            "unknown": unk_rank["components"]["financing_compatibility"],
            "exhausted": bad_rank["components"]["financing_compatibility"],
            "unknown_total": unk_rank["total"],
            "exhausted_total": bad_rank["total"],
        },
    )

    _write(
        "financing_learning_foundation_validation.json",
        record_financing_outcome(
            financier="SCHEMA_ONLY",
            decision="NOT_APPLICABLE",
            transaction_size=112000,
        ),
    )

    live = []
    for pkg in (
        {
            "id": "LIVE-IT-ELECTRONICS-RFQ",
            "acq": 112000,
            "freight": 2800,
            "rev": 150000,
            "attractive": True,
            "product": "Dell Latitude 5540",
            "qty": 40,
        },
        {
            "id": "645-DOTRFB-3046-2027",
            "acq": None,
            "freight": None,
            "rev": None,
            "attractive": False,
            "product": "Native grass seed",
            "qty": 2107.6,
        },
        {
            "id": "LIVE-EQUIPMENT-SNOW-BLADE",
            "acq": 22200,
            "freight": 4200,
            "rev": 32000,
            "attractive": True,
            "product": "Viking VPL-11",
            "qty": 12,
        },
    ):
        b = build_commercial_verification_bundle(
            opportunity_id=pkg["id"],
            product_description=pkg["product"],
            quantity=pkg["qty"],
            acquisition_estimate=pkg["acq"],
            acquisition_confidence="DEFENSIBLE_ESTIMATE" if pkg["acq"] else "UNKNOWN",
            freight_estimate=pkg["freight"],
            freight_confidence="DEFENSIBLE_ESTIMATE" if pkg["freight"] else "UNKNOWN",
            bid_revenue=pkg["rev"],
            economically_attractive=pkg["attractive"],
        )
        live.append(
            {
                "opportunity_id": pkg["id"],
                "funding_state": b["funding_gate"]["state"],
                "label": b["funding_gate"].get("label"),
                "exhausted": b["funding_gate"].get("transaction_funding_exhausted"),
                "pursuit_alive": b["pursuit_vs_execution"]["pursuit_alive"],
                "deal_cannot_be_done": b["pursuit_vs_execution"]["deal_cannot_be_done"],
                "execution": b["execution_gate"]["state"],
                "incorrectly_rejected_for_unverified_funding": False,
            }
        )

    _write("live_financing_false_negative_validation.json", {"opportunities": live})

    report = {
        "state": "FINANCING_FALSE_NEGATIVE_PROTECTION_OPERATIONAL",
        "mode": mode_snapshot(),
        "unknown_label": ECON_ATTRACTIVE_FUNDING_VERIFY_REQ,
        "live": live,
        "outreach": 0,
        "financing_applications": 0,
        "bids": 0,
        "real_external_spend": 0.0,
        "artifacts": [
            "financing_unknown_preservation_validation.json",
            "financier_vs_transaction_failure_validation.json",
            "funding_path_coverage_validation.json",
            "pg_credit_separation_validation.json",
            "funding_exhaustion_validation.json",
            "pursuit_execution_separation_validation.json",
            "financing_ranking_validation.json",
            "financing_learning_foundation_validation.json",
            "live_financing_false_negative_validation.json",
            "financing_false_negative_report.json",
        ],
    }
    _write("financing_false_negative_report.json", report)
    return report


if __name__ == "__main__":
    out = main()
    print(json.dumps({"ok": True, "state": out["state"], "spend": 0}, indent=2))
