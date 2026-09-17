"""Operator result ingestion + knowledge promotion. No auto-communication."""

from __future__ import annotations

from typing import Any

from application_clock import now_utc
from evidence_maturity import mature_fact
from executable_deal_constants import (
    EV_OPERATOR_CONFIRMED,
    EV_QUOTE_REQUIRED,
    PROMOTE_DEAL_SPECIFIC,
    PROMOTE_REUSABLE,
)
from reusable_knowledge import ReusableKnowledgeStore, finance_fact, supplier_fact


def _utc() -> str:
    return now_utc().isoformat()


def classify_promotion(field: str, *, deal_specific_quote: bool = False) -> str:
    if deal_specific_quote or field in {
        "quoted_total",
        "unit_pricing",
        "quote_number",
        "quote_expiration",
        "this_quantity_price",
    }:
        return PROMOTE_DEAL_SPECIFIC
    if field in {
        "payment_before_shipment",
        "payment_terms_general",
        "authorized_distributor",
        "manufacturer_authorization",
        "direct_ship_capability",
        "government_sales_capability",
        "min_fico",
        "pg_requirement",
        "personal_credit_role",
        "zero_cash_accepted",
        "fee_structure_general",
    }:
        return PROMOTE_REUSABLE
    return PROMOTE_DEAL_SPECIFIC


def ingest_supplier_call_result(
    deal: dict[str, Any],
    result: dict[str, Any],
    *,
    reusable: ReusableKnowledgeStore | None = None,
    synthetic_test: bool = False,
) -> dict[str, Any]:
    """Update deal facts; promote reusable supplier knowledge when appropriate."""
    if synthetic_test and deal.get("is_live"):
        raise ValueError("refusing synthetic operator results on live deal")

    deal = dict(deal)
    deal["supplier_quote"] = {
        "supplier": result.get("supplier"),
        "contact": result.get("contact"),
        "called_at": result.get("date_time") or _utc(),
        "quote_number": result.get("quote_number"),
        "quoted_total": mature_fact(result.get("quoted_total"), EV_OPERATOR_CONFIRMED, source="operator_supplier_call"),
        "unit_pricing": mature_fact(result.get("unit_pricing"), EV_OPERATOR_CONFIRMED, source="operator_supplier_call"),
        "freight_included": result.get("freight_included"),
        "payment_terms": result.get("payment_terms"),
        "deposit": result.get("deposit"),
        "lead_time": result.get("lead_time"),
        "direct_ship": result.get("direct_ship"),
        "product_match_confirmed": result.get("product_match_confirmed"),
        "authorization_confirmed": result.get("authorization_confirmed"),
        "quote_expiration": result.get("quote_expiration"),
        "notes": result.get("notes"),
        "evidence_ref": result.get("evidence_attachment_reference"),
        "synthetic_test_label": bool(synthetic_test),
    }
    deal["supplier_cost_maturity"] = EV_OPERATOR_CONFIRMED
    deal["cost_established"] = result.get("quoted_total") is not None
    if result.get("freight_included") is True:
        deal["freight"] = mature_fact(0, EV_OPERATOR_CONFIRMED, notes="included_in_quote")
        deal["freight_mature"] = True
    elif result.get("freight_amount") is not None:
        deal["freight"] = mature_fact(result.get("freight_amount"), EV_OPERATOR_CONFIRMED, source="operator_supplier_call")
        deal["freight_mature"] = True
    else:
        deal["freight"] = mature_fact(None, EV_QUOTE_REQUIRED)
        deal["freight_mature"] = False

    promotions: list[dict[str, Any]] = []
    if reusable is not None:
        # Deal-specific quote amount — do NOT promote as universal price
        promotions.append(
            {
                "field": "quoted_total",
                "promotion": PROMOTE_DEAL_SPECIFIC,
                "reason": "quantity-specific quote is deal-specific",
            }
        )
        if result.get("payment_terms"):
            reusable.add_supplier(
                supplier_fact(
                    str(result.get("supplier") or "unknown"),
                    category=deal.get("product_category"),
                    source="operator_supplier_call",
                    quote_required=False,
                    government_sales=True if result.get("product_match_confirmed") else None,
                )
            )
            promotions.append(
                {
                    "field": "payment_terms_general",
                    "promotion": classify_promotion("payment_terms_general"),
                    "value": result.get("payment_terms"),
                }
            )
            terms_l = str(result.get("payment_terms")).lower()
            if "before shipment" in terms_l or "payment before" in terms_l or "prepay" in terms_l:
                promotions.append(
                    {
                        "field": "payment_before_shipment",
                        "promotion": PROMOTE_REUSABLE,
                        "value": True,
                    }
                )
        if result.get("authorization_confirmed"):
            reusable.add_supplier(
                supplier_fact(
                    str(result.get("supplier") or "unknown"),
                    manufacturer=result.get("manufacturer"),
                    authorization_evidence="operator_confirmed",
                    source="operator_supplier_call",
                )
            )
            promotions.append({"field": "authorized_distributor", "promotion": PROMOTE_REUSABLE})

    return {"deal": deal, "promotions": promotions, "kind": "SupplierCallIngestion"}


def ingest_financier_call_result(
    deal: dict[str, Any],
    result: dict[str, Any],
    *,
    reusable: ReusableKnowledgeStore | None = None,
    synthetic_test: bool = False,
) -> dict[str, Any]:
    if synthetic_test and deal.get("is_live"):
        raise ValueError("refusing synthetic operator results on live deal")

    deal = dict(deal)
    deal["financier_result"] = {
        "provider": result.get("provider"),
        "contact": result.get("contact"),
        "called_at": result.get("date_time") or _utc(),
        "accepted_in_principle": result.get("transaction_accepted_in_principle"),
        "funding_amount_or_pct": result.get("funding_amount_percentage"),
        "fees": result.get("fees"),
        "timing": result.get("timing"),
        "personal_credit_pull": result.get("personal_credit_pull"),
        "personal_credit_role": result.get("personal_credit_role"),
        "minimum_fico": result.get("minimum_fico"),
        "pg_requirement": result.get("pg_requirement"),
        "pg_type": result.get("pg_type"),
        "cash_injection": result.get("cash_injection"),
        "required_documents": result.get("required_documents"),
        "pre_bid_conditional": result.get("pre_bid_conditional_indication"),
        "post_award_requirements": result.get("post_award_requirements"),
        "notes": result.get("notes"),
        "synthetic_test_label": bool(synthetic_test),
        "maturity": EV_OPERATOR_CONFIRMED,
    }
    if result.get("fees") is not None:
        try:
            deal["financing_cost_amount"] = float(result.get("fees"))
        except (TypeError, ValueError):
            deal["financing_cost_amount"] = result.get("fees")
    deal["funding_deal_specific_confirmed"] = bool(
        result.get("pre_bid_conditional_indication") or result.get("transaction_accepted_in_principle")
    )
    promotions: list[dict[str, Any]] = []
    if reusable is not None:
        pid = str(result.get("provider") or "unknown_provider")
        mapping = [
            ("minimum_fico", result.get("minimum_fico")),
            ("pg_requirement", result.get("pg_requirement")),
            ("personal_credit_role", result.get("personal_credit_role")),
            (
                "zero_cash_accepted",
                result.get("cash_injection") in {0, "0", False, None, "none", "None", "no", "NO"},
            ),
            ("fee_structure_general", result.get("fees")),
        ]
        for field, val in mapping:
            if val is None and field != "zero_cash_accepted":
                continue
            if field == "zero_cash_accepted" and result.get("cash_injection") is None and "cash_injection" not in result:
                continue
            reusable.add_finance(
                finance_fact(pid, field, val, source="operator_financier_call", verified=True)
            )
            promotions.append({"field": field, "promotion": PROMOTE_REUSABLE})
    return {"deal": deal, "promotions": promotions, "kind": "FinancierCallIngestion"}
