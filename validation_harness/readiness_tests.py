"""Adversarial false-readiness / false-rejection helpers (programmatic cases)."""

from __future__ import annotations

from typing import Any

from validation_harness.models import SOURCE_TYPE_SYNTHETIC, empty_case


def adversarial_cases() -> list[dict[str, Any]]:
    """
    Explicit false-readiness suite.
    These supplement JSON corpus cases tagged adversarial.
    Primary assertion: ready_for_owner_approval must be False.
    Blocker lists are soft hints (optional) — exact blocker taxonomy varies by gate.
    """
    base_text = "RFQ TEST — product laptop Quantity: 10 EA FOB DESTINATION."
    cases = []

    def adv(cid: str, name: str, *, text: str, overrides: dict[str, Any], tags: list[str], amendment: str | None = None) -> dict[str, Any]:
        c = empty_case(
            case_id=cid,
            case_name=name,
            description=name,
            source_type=SOURCE_TYPE_SYNTHETIC,
            source_fixture=text,
            row_overrides=overrides,
            expected={
                "owner_readiness": {"ready_for_owner_approval": False},
            },
            expected_owner_readiness=False,
            expected_blockers=[],
            tags=["adversarial", "false_readiness"] + tags,
            adversarial=True,
            notes="SYNTHETIC_VALIDATION_FIXTURE — adversarial readiness gate",
        )
        if amendment:
            c["amendment_text"] = amendment
        return c

    cases.append(
        adv(
            "ADV_SUPPLIER_UNKNOWN",
            "Supplier unknown must not reach owner approval",
            text=base_text + " Submit via email.",
            overrides={"supplier_unknown": True, "Deal_state": "READY_FOR_OPERATOR_ACTION", "lifecycle": "BID_READY"},
            tags=["supplier"],
        )
    )
    cases.append(
        adv(
            "ADV_ECONOMICS_UNKNOWN",
            "Economics unknown must not reach owner approval",
            text=base_text,
            overrides={"supported_profit": "UNKNOWN", "Known_gross_spread": "UNKNOWN", "Deal_state": "DECISION_READY"},
            tags=["economics"],
        )
    )
    cases.append(
        adv(
            "ADV_QTY_AMBIGUOUS",
            "Ambiguous quantity must not reach owner approval",
            text="RFQ — estimated quantity approx dozen units. Brand or equal laptop.",
            overrides={},
            tags=["quantity"],
        )
    )
    cases.append(
        adv(
            "ADV_PACKAGING_UNRESOLVED",
            "Mandatory MIL-STD packaging unresolved blocks readiness",
            text="Quantity: 5 EA. MIL-STD-2073 packaging required. Part Number: X-1",
            overrides={},
            tags=["packaging"],
        )
    )
    cases.append(
        adv(
            "ADV_FINANCING_PG",
            "Personal guarantee required must not silently pass financing",
            text=base_text + " Payment Net 30.",
            overrides={"funding_state": "UNRESOLVED_PG_REQUIRED", "personal_guarantee_required": True, "Deal_state": "READY"},
            tags=["financing"],
        )
    )
    cases.append(
        adv(
            "ADV_AMENDMENT_CONFLICT",
            "Unresolved amendment conflict blocks readiness",
            text="Quantity: 100 EA. Deadline October 10. FOB ORIGIN.",
            overrides={},
            tags=["amendment"],
            amendment="Amendment 0001: Quantity changed to 150. FOB DESTINATION. Deadline October 14.",
        )
    )
    cases.append(
        adv(
            "ADV_DEADLINE_EXPIRED",
            "Expired solicitation should not remain actionable as ready",
            text="Quantity: 10 EA. Bid deadline: 2020-01-01. Submit via email.",
            overrides={"deadline": "2020-01-01", "days_remaining": -100, "Deal_state": "READY"},
            tags=["submission", "deadline"],
        )
    )
    return cases


def false_rejection_cases() -> list[dict[str, Any]]:
    """UNKNOWN ≠ FAIL / military packaging alone ≠ reject."""
    return [
        empty_case(
            case_id="ADV_FALSE_REJ_MIL_PACK",
            case_name="MIL-STD packaging must be detected but not auto-reject",
            source_type=SOURCE_TYPE_SYNTHETIC,
            source_fixture="Quantity: 20 EA Part Number: ZZ-9. MIL-STD-2073 packaging required. Specialist packaging house may be required.",
            expected={
                "packaging": {
                    "mil_std_2073_detected": True,
                    "specialist_packaging_flagged": True,
                },
                "financing": {"financing_assumed": False},
            },
            expected_owner_readiness=False,
            tags=["false_rejection", "packaging", "adversarial"],
            adversarial=True,
            notes="Military packaging exists but can be outsourced — detect, don't invent auto-reject.",
        ),
        empty_case(
            case_id="ADV_FALSE_REJ_HISTORY_UNKNOWN",
            case_name="Missing history stays UNKNOWN — not instant fail token",
            source_type=SOURCE_TYPE_SYNTHETIC,
            source_fixture="Commercial RFQ Quantity: 5 EA Dell laptop brand or equal. FOB DESTINATION.",
            expected={
                "financing": {"financing_assumed": False, "cash_unknowns_preserved": True},
            },
            expected_owner_readiness=False,
            allowed_unknowns=["history"],
            tags=["false_rejection", "history", "adversarial"],
            adversarial=True,
        ),
    ]
