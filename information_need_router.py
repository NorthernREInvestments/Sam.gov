"""Information needs + source router — where to look first for each need."""

from __future__ import annotations

from typing import Any

from procurement_source_knowledge import ProcurementSourceKnowledgeBase

# Need types
NEED_SOLICITATION_NOTICE = "SOLICITATION_NOTICE"
NEED_SOLICITATION_PACKAGE = "SOLICITATION_PACKAGE"
NEED_SPECIFICATION = "SPECIFICATION"
NEED_ATTACHMENT = "ATTACHMENT"
NEED_AMENDMENT = "AMENDMENT"
NEED_QA = "QUESTION_AND_ANSWER"
NEED_PRICING_SHEET = "PRICING_SHEET"
NEED_BID_FORM = "BID_FORM"
NEED_DELIVERY_TERMS = "DELIVERY_TERMS"
NEED_PRODUCT_REQUIREMENT = "PRODUCT_REQUIREMENT"
NEED_BOM = "BOM"
NEED_AWARD_NOTICE = "AWARD_NOTICE"
NEED_BID_TAB = "BID_TAB"
NEED_HISTORICAL_AWARD = "HISTORICAL_AWARD"
NEED_BUYER_HISTORY = "BUYER_HISTORY"
NEED_SUPPLIER = "SUPPLIER"
NEED_MANUFACTURER = "MANUFACTURER"
NEED_AUTHORIZED_DISTRIBUTOR = "AUTHORIZED_DISTRIBUTOR"
NEED_PUBLIC_PRODUCT_PRICE = "PUBLIC_PRODUCT_PRICE"
NEED_FREIGHT_EVIDENCE = "FREIGHT_EVIDENCE"
NEED_FINANCING_INFORMATION = "FINANCING_INFORMATION"
NEED_OTHER = "OTHER"

# Priority order for research loop (economic usefulness)
RESEARCH_PRIORITY: tuple[str, ...] = (
    "transactional_fit",
    "deadline_viability",
    NEED_PRODUCT_REQUIREMENT,
    NEED_BOM,
    NEED_SPECIFICATION,
    NEED_SUPPLIER,
    NEED_PUBLIC_PRODUCT_PRICE,
    NEED_FREIGHT_EVIDENCE,
    "profit_plausibility",
    "working_capital",
    NEED_FINANCING_INFORMATION,
    "compliance_portal",
    "bid_prep",
)


def information_need(
    need_type: str,
    *,
    reason: str | None = None,
    priority: int = 50,
    identifiers: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "kind": "ProcurementInformationNeed",
        "need_type": need_type,
        "reason": reason,
        "priority": priority,
        "identifiers": identifiers or {},
    }


# Default route tables: ordered source_id preferences
_ROUTES: dict[str, list[str]] = {
    NEED_HISTORICAL_AWARD: [
        "agency_archive",
        "usaspending",
        "sam_gov",
        "public_contract_register",
    ],
    NEED_SPECIFICATION: [
        "event_attachments",
        "solicitation_package",
        "public_event_pdf",
        "amendment_package",
        "agency_alternate_page",
    ],
    NEED_SOLICITATION_PACKAGE: [
        "public_event_pdf",
        "event_attachments",
        "agency_portal",
        "sam_gov",
    ],
    NEED_ATTACHMENT: [
        "event_attachments",
        "public_event_pdf",
        "supplier_portal_auth",
    ],
    NEED_AMENDMENT: [
        "event_amendments",
        "public_event_pdf",
        "agency_portal",
    ],
    NEED_QA: [
        "event_attachments",
        "agency_portal",
        "supplier_portal_auth",
    ],
    NEED_AWARD_NOTICE: [
        "agency_award_posting",
        "usaspending",
        "sam_gov",
    ],
    NEED_BID_TAB: [
        "agency_bid_results",
        "public_bid_tab_repository",
    ],
    NEED_SUPPLIER: [
        "manufacturer_distributor_public",
        "prior_buyer_awardees_public",
    ],
    NEED_PUBLIC_PRODUCT_PRICE: [
        "distributor_catalog_public",
        "manufacturer_msrp_public",
        "prior_government_price_public",
    ],
    NEED_FINANCING_INFORMATION: [
        "persisted_finance_knowledge",
        "provider_public_pages",
    ],
    NEED_SOLICITATION_NOTICE: [
        "agency_portal",
        "sam_gov",
        "bidnet_public",
    ],
}


class InformationSourceRouter:
    def __init__(self, knowledge: ProcurementSourceKnowledgeBase | None = None) -> None:
        self.knowledge = knowledge or ProcurementSourceKnowledgeBase()

    def route(
        self,
        need: dict[str, Any] | str,
        *,
        opportunity: dict[str, Any] | None = None,
        known_urls: list[str] | None = None,
        agency: str | None = None,
        jurisdiction: str | None = None,
        source: str | None = None,
    ) -> dict[str, Any]:
        need_type = need if isinstance(need, str) else need.get("need_type")
        opp = opportunity or {}
        agency = agency or opp.get("agency")
        jurisdiction = jurisdiction or opp.get("jurisdiction") or opp.get("state")
        source = source or opp.get("source") or opp.get("portal_family")

        base_route = list(_ROUTES.get(str(need_type), ["agency_portal", "public_search"]))
        profiles = self.knowledge.match_by_jurisdiction(jurisdiction, agency)

        # Prefer matching portal family first
        ordered_sources: list[dict[str, Any]] = []
        if (source and "sciquest" in str(source).lower()) or (
            agency and "iowa" in str(agency).lower()
        ):
            iowa = self.knowledge.get("iowa_sciquest_jaggaer")
            if iowa:
                ordered_sources.append(
                    {
                        "step": 1,
                        "source_id": iowa["source_id"],
                        "method": iowa.get("event_lookup_method"),
                        "why": "jurisdiction/agency maps to Iowa SciQuest recipe",
                    }
                )

        for i, step in enumerate(base_route, start=len(ordered_sources) + 1):
            # Map abstract steps to concrete profiles when possible
            concrete = None
            if step in {"public_event_pdf", "event_attachments", "agency_portal"} and profiles:
                concrete = profiles[0]
            elif step == "usaspending":
                concrete = self.knowledge.get("usaspending")
            elif step == "sam_gov":
                concrete = self.knowledge.get("sam_gov")
            elif step == "bidnet_public":
                concrete = self.knowledge.get("bidnet_public")

            ordered_sources.append(
                {
                    "step": i,
                    "route_key": step,
                    "source_id": (concrete or {}).get("source_id"),
                    "method": (concrete or {}).get("attachment_discovery_method")
                    or (concrete or {}).get("solicitation_lookup_method"),
                    "known_urls": list(known_urls or []) if i == 1 else [],
                    "why": f"default route for {need_type}",
                }
            )

        return {
            "kind": "InformationSourceRoute",
            "need_type": need_type,
            "agency": agency,
            "jurisdiction": jurisdiction,
            "ordered_sources": ordered_sources,
            "known_urls_first": bool(known_urls),
            "note": "Router answers WHERE TO LOOK — does not fetch bulk databases",
        }

    def missing_needs_from_deal_state(self, deal: dict[str, Any]) -> list[dict[str, Any]]:
        """Derive highest-priority missing needs from deal/research state."""
        needs: list[dict[str, Any]] = []
        if not deal.get("transactional_fit_ok"):
            needs.append(information_need(NEED_SOLICITATION_NOTICE, reason="confirm_product_buy", priority=10))
        if not deal.get("deadline_known"):
            needs.append(information_need(NEED_SOLICITATION_NOTICE, reason="deadline", priority=15))
        if not deal.get("requirements_complete"):
            needs.append(information_need(NEED_SPECIFICATION, reason="requirements_incomplete", priority=20))
            needs.append(information_need(NEED_PRODUCT_REQUIREMENT, reason="requirements_incomplete", priority=21))
        if not deal.get("bom_present"):
            needs.append(information_need(NEED_BOM, reason="bom_missing", priority=25))
        if not deal.get("supplier_identified"):
            needs.append(information_need(NEED_SUPPLIER, reason="supplier_gap", priority=40))
        if not deal.get("cost_established"):
            needs.append(information_need(NEED_PUBLIC_PRODUCT_PRICE, reason="economics", priority=50))
        # Funding only after economics sufficiently mature
        if deal.get("cost_established") and not deal.get("funding_assessed"):
            needs.append(information_need(NEED_FINANCING_INFORMATION, reason="funding_after_economics", priority=80))
        needs.sort(key=lambda n: n["priority"])
        return needs
