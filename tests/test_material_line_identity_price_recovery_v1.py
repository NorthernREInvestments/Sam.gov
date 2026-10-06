"""Revenue-context + Collier bonding-threshold regressions."""

from __future__ import annotations

from material_line_identity_price_recovery.models import BOND_THRESHOLD, INSURANCE_THRESHOLD
from material_line_identity_price_recovery.revenue_context import (
    classify_money_context,
    run_revenue_context_regression,
)


def test_collier_bonding_threshold_not_revenue():
    text = (
        "of $250,000 may require that persons interested in performing work under "
        "contract first be certified or qualified to perform such work."
    )
    row = classify_money_context(250000.0, text_window=text, document="26-8702_Construction_Bid_Instructions_Version_1.pdf")
    assert row["semantic_role"] == BOND_THRESHOLD
    assert row["usable_as_revenue"] is False


def test_insurance_threshold_not_revenue():
    row = classify_money_context(
        1000000.0,
        text_window="Commercial General Liability insurance with limits of not less than $1,000,000 each occurrence.",
    )
    assert row["semantic_role"] == INSURANCE_THRESHOLD
    assert row["usable_as_revenue"] is False


def test_revenue_context_regression_corpus_passes():
    result = run_revenue_context_regression()
    assert result["collier_bonding_threshold_classified_correctly"] is True
    assert result["collier_contract_revenue_incorrectly_created"] is False
    assert result["all_pass"] is True
