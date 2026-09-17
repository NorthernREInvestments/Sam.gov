"""Compact reusable knowledge — source / finance / supplier / buyer (not catalogs)."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from application_clock import now_utc
from persistence_policy import (
    KIND_BUYER_INTELLIGENCE,
    KIND_FUNDING_FACT,
    KIND_SUPPLIER_INTELLIGENCE,
    persistence_decision,
)
from procurement_source_knowledge import STALE_AGING, STALE_CURRENT, STALE_STALE, assess_staleness


def _utc() -> str:
    return now_utc().isoformat()


def finance_fact(
    provider_id: str,
    field: str,
    value: Any,
    *,
    source: str,
    verified: bool = False,
    observed_at: str | None = None,
) -> dict[str, Any]:
    observed = observed_at or _utc()
    return {
        "kind": "FinanceKnowledgeFact",
        "provider_id": provider_id,
        "field": field,
        "value": value,
        "source": source,
        "observed_at": observed,
        "verified_at": observed if verified else None,
        "staleness": assess_staleness(observed, knowledge_class="financier_criteria"),
        "confidence": "HIGH" if verified else "MEDIUM",
        "persistence": persistence_decision(KIND_FUNDING_FACT),
    }


def supplier_fact(
    supplier: str,
    *,
    manufacturer: str | None = None,
    category: str | None = None,
    authorization_evidence: str | None = None,
    quote_required: bool | None = None,
    government_sales: bool | None = None,
    source: str,
    observed_at: str | None = None,
) -> dict[str, Any]:
    observed = observed_at or _utc()
    return {
        "kind": "SupplierKnowledgeFact",
        "supplier": supplier,
        "manufacturer": manufacturer,
        "category": category,
        "authorization_evidence": authorization_evidence,
        "quote_required": quote_required,
        "government_sales_capability": government_sales,
        "source": source,
        "observed_at": observed,
        "staleness": assess_staleness(observed, knowledge_class="supplier_authorization"),
        "persistence": persistence_decision(KIND_SUPPLIER_INTELLIGENCE),
        "note": "Compact relationship — not a catalog mirror",
    }


def buyer_fact(
    agency: str,
    *,
    buying_office: str | None = None,
    portal: str | None = None,
    common_categories: list[str] | None = None,
    typical_mechanism: str | None = None,
    source_recipe_id: str | None = None,
    source: str,
    observed_at: str | None = None,
) -> dict[str, Any]:
    observed = observed_at or _utc()
    return {
        "kind": "BuyerKnowledgeFact",
        "agency": agency,
        "buying_office": buying_office,
        "portal": portal,
        "common_product_categories": list(common_categories or [])[:20],
        "typical_procurement_mechanism": typical_mechanism,
        "source_recipe_id": source_recipe_id,
        "source": source,
        "observed_at": observed,
        "staleness": assess_staleness(observed, knowledge_class="buyer_portal"),
        "persistence": persistence_decision(KIND_BUYER_INTELLIGENCE),
        "note": "Compact buyer intelligence — not every historical transaction",
    }


class ReusableKnowledgeStore:
    def __init__(self) -> None:
        self.finance: list[dict[str, Any]] = []
        self.suppliers: list[dict[str, Any]] = []
        self.buyers: list[dict[str, Any]] = []

    def add_finance(self, fact: dict[str, Any]) -> None:
        self.finance.append(fact)

    def add_supplier(self, fact: dict[str, Any]) -> None:
        # Refuse catalog dumps
        if fact.get("catalog_items") or fact.get("full_price_list"):
            raise ValueError("supplier catalogs are not mirrored")
        self.suppliers.append(fact)

    def add_buyer(self, fact: dict[str, Any]) -> None:
        if fact.get("all_historical_awards"):
            raise ValueError("do not mirror every historical transaction")
        self.buyers.append(fact)

    def finance_needs_reverify(self, provider_id: str) -> bool:
        facts = [f for f in self.finance if f.get("provider_id") == provider_id]
        if not facts:
            return True
        return any(f.get("staleness") in {STALE_STALE, "UNKNOWN"} for f in facts)

    def counts(self) -> dict[str, int]:
        return {
            "finance_facts": len(self.finance),
            "supplier_facts": len(self.suppliers),
            "buyer_facts": len(self.buyers),
        }

    def export(self) -> dict[str, Any]:
        return {
            "finance": self.finance,
            "suppliers": self.suppliers,
            "buyers": self.buyers,
            "counts": self.counts(),
        }

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.export(), indent=2), encoding="utf-8")
