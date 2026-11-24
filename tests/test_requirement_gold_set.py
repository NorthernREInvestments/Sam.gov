"""Regression gate: classifier vs blind-labelled gold records from the retained inventory.

Gold labels were assigned by two independent annotators who never saw classifier output
(disagreements adjudicated from record text). Thresholds sit below measured performance
so the gate catches regressions, not noise; they are not claims of accuracy.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from m3.research.classify import classify_requirement

GOLD_PATH = Path(__file__).parent / "fixtures" / "requirement_gold_set.jsonl"
PRODUCT_SIDE = {"PRODUCT", "PRODUCT_WITH_SERVICE"}
NON_PRODUCT = {"SERVICE", "CONSTRUCTION", "NOT_A_PURCHASE"}
PRODUCT_CLASSES = {
    "product_supply",
    "product_with_significant_installation",
    "product_with_incidental_services",
    "mixed_product_service",
}


def _gold() -> list[dict]:
    return [json.loads(line) for line in GOLD_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]


def _predict(rec: dict) -> dict:
    return classify_requirement(
        rec["title"],
        description=rec.get("summary") or None,
        agency_codes=rec.get("agency_codes"),
        procurement_kind=rec.get("provider_procurement_kind"),
    )


@pytest.fixture(scope="module")
def scored() -> list[tuple[dict, dict]]:
    return [(rec, _predict(rec)) for rec in _gold()]


def test_gold_fixture_present_and_blind_labelled() -> None:
    rows = _gold()
    assert len(rows) >= 400
    assert {r["gold"] for r in rows} >= PRODUCT_SIDE | NON_PRODUCT


def test_product_side_precision_and_recall(scored) -> None:
    predicted = [(r, p) for r, p in scored if p["requirement_class"] in PRODUCT_CLASSES]
    tp = sum(1 for r, _ in predicted if r["gold"] in PRODUCT_SIDE)
    gold_products = [r for r, _ in scored if r["gold"] in PRODUCT_SIDE]
    found = sum(1 for r, p in scored if r["gold"] in PRODUCT_SIDE and p["requirement_class"] in PRODUCT_CLASSES)
    assert tp / len(predicted) >= 0.88, f"product precision {tp}/{len(predicted)}"
    assert found / len(gold_products) >= 0.72, f"product recall {found}/{len(gold_products)}"


def test_non_products_rarely_called_products(scored) -> None:
    non_products = [(r, p) for r, p in scored if r["gold"] in NON_PRODUCT]
    false_products = [r["title"] for r, p in non_products if p["requirement_class"] in PRODUCT_CLASSES]
    assert len(false_products) / len(non_products) <= 0.03, false_products


def test_high_confidence_product_calls_are_reliable(scored) -> None:
    high = [(r, p) for r, p in scored if p["requirement_class"] in PRODUCT_CLASSES and p["decision_confidence"] == "high"]
    wrong = [r["title"] for r, _ in high if r["gold"] in NON_PRODUCT]
    assert len(wrong) <= max(2, int(0.08 * len(high))), wrong


def test_clear_non_products_mostly_resolved(scored) -> None:
    non_products = [(r, p) for r, p in scored if r["gold"] in NON_PRODUCT]
    unresolved = sum(1 for _, p in non_products if p["requirement_class"] == "unknown_mixed")
    assert unresolved / len(non_products) <= 0.25


def test_every_decision_carries_quoted_evidence(scored) -> None:
    for rec, pred in scored:
        if pred["requirement_class"] == "unknown_mixed":
            continue
        quotes = [s["quote"] for s in pred["evidence_signals"] if s.get("quote")]
        assert quotes, rec["nb_id"]
