"""Procurement blocker classification — funding stays downstream of economics."""

from __future__ import annotations

from typing import Any

from document_ingestion_constants import (
    BLOCKER_AUTH,
    BLOCKER_COMPLIANCE,
    BLOCKER_FREIGHT,
    BLOCKER_FUNDING,
    BLOCKER_GOV_REVENUE,
    BLOCKER_OPERATOR_DOC,
    BLOCKER_OTHER,
    BLOCKER_SUPPLIER_QUOTE,
)


def classify_procurement_blockers(packet: dict[str, Any]) -> dict[str, Any]:
    """
    Explicit blocker model. Funding is NOT immediate while supplier cost unknown.
    """
    blockers: list[dict[str, Any]] = []
    docs = packet.get("documents") or []
    req = packet.get("requirement") or {}
    completeness = req.get("completeness") or packet.get("completeness") or {}
    econ = packet.get("economics_block") or {}
    compliance = packet.get("compliance") or {}
    suppliers = packet.get("suppliers") or []
    ingested = packet.get("ingested_documents") or []

    auth_docs = [
        d
        for d in docs
        if d.get("access_status") in {"LOGIN_REQUIRED", "AUTH_REQUIRED"}
        and d.get("document_class") in {"SPECIFICATION", "AMENDMENT", "BID_SCHEDULE", "PRICING_SHEET", "DRAWING"}
    ]
    # Resolved if an ingested non-superseded doc of same class/title exists
    ingested_titles = {
        (d.get("document_title") or "").lower()
        for d in ingested
        if not d.get("superseded")
    }
    ingested_classes = {
        d.get("document_class")
        for d in ingested
        if not d.get("superseded") and d.get("document_class") == "SPECIFICATION"
    }

    unresolved_auth = []
    for d in auth_docs:
        title = (d.get("document_title") or "").lower()
        if title in ingested_titles or (
            d.get("document_class") == "SPECIFICATION" and "SPECIFICATION" in ingested_classes
        ):
            continue
        unresolved_auth.append(d)

    if unresolved_auth:
        blockers.append(
            {
                "code": BLOCKER_AUTH,
                "severity": "immediate",
                "documents": [d.get("document_title") for d in unresolved_auth],
                "detail": "Authoritative attachment listed but requires authorized portal access",
            }
        )
        blockers.append(
            {
                "code": BLOCKER_OPERATOR_DOC,
                "severity": "immediate",
                "documents": [d.get("document_title") for d in unresolved_auth],
                "detail": "Operator must download via authorized account and ingest with CLI",
            }
        )

    if completeness.get("critical_spec_missing") and not ingested_classes:
        if not any(b["code"] == BLOCKER_OPERATOR_DOC for b in blockers):
            blockers.append(
                {
                    "code": BLOCKER_OPERATOR_DOC,
                    "severity": "immediate",
                    "documents": completeness.get("missing") or [],
                    "detail": "Critical specification still missing from package",
                }
            )

    # Supplier quote — after product ID is possible
    pid = (req.get("product_identification") or {}).get("product_id_state")
    if pid and pid != "INSUFFICIENT_INFORMATION":
        if econ.get("supplier_cost") is None and not econ.get("supplier_cost_range"):
            blockers.append(
                {
                    "code": BLOCKER_SUPPLIER_QUOTE,
                    "severity": "next",
                    "detail": "No defensible public unit price; supplier quote required",
                    "supplier_count": len(suppliers),
                }
            )

    if econ.get("freight_status") == "UNKNOWN" and (
        econ.get("supplier_cost") is not None or any(b["code"] == BLOCKER_SUPPLIER_QUOTE for b in blockers)
    ):
        # Freight required once we are in quote/cost path — still not funding
        blockers.append(
            {
                "code": BLOCKER_FREIGHT,
                "severity": "after_quote",
                "detail": "Freight/landed cost unknown; request freight in quote packet",
            }
        )

    # Bidder-priced: government revenue may never exist as budget
    gov = packet.get("government_value_evidence") or {}
    bidder_priced = bool(packet.get("bidder_priced") or gov.get("bidder_priced"))
    if econ.get("expected_revenue") is None and not bidder_priced:
        blockers.append(
            {
                "code": BLOCKER_GOV_REVENUE,
                "severity": "parallel",
                "detail": "No budget/historical award evidence; or use bid-price threshold model if bidder-priced",
            }
        )
    elif econ.get("expected_revenue") is None and bidder_priced:
        blockers.append(
            {
                "code": BLOCKER_OTHER,
                "severity": "informational",
                "detail": "Bidder-priced solicitation — use bid-price thresholds; no fixed government revenue assumed",
                "subtype": "BIDDER_PRICED_NO_FIXED_REVENUE",
            }
        )

    if compliance.get("entity_eligibility") not in {"PASS", "NOT_APPLICABLE"}:
        blockers.append(
            {
                "code": BLOCKER_COMPLIANCE,
                "severity": "parallel",
                "detail": "Entity/set-aside/NMR/mill-cert compliance still needs review",
            }
        )

    # Funding ONLY when meaningful acquisition cost exists
    funding_warranted = bool(econ.get("funding_warranted")) or (
        econ.get("supplier_cost") is not None or bool(econ.get("supplier_cost_range"))
    )
    if funding_warranted:
        funding = packet.get("funding") or {}
        if funding.get("status") not in {"SECURED", "VERIFIED"}:
            blockers.append(
                {
                    "code": BLOCKER_FUNDING,
                    "severity": "downstream",
                    "detail": "Working capital estimable — funding verification relevant (match≠approval)",
                }
            )
    # Explicitly record that funding is NOT an immediate blocker
    funding_premature = not funding_warranted

    primary = next(
        (b for b in blockers if b.get("severity") == "immediate"),
        blockers[0] if blockers else None,
    )

    return {
        "blockers": blockers,
        "primary_blocker": (primary or {}).get("code"),
        "funding_is_immediate_blocker": False,
        "funding_premature": funding_premature,
        "codes": [b["code"] for b in blockers],
    }
