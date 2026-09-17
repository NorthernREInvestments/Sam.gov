"""Canonical PostgreSQL table names for GovTracker (gt_ prefix) and shared read-only tables."""

from __future__ import annotations

import os

# GovTracker-owned tables (gt_ prefix avoids conflicts in shared Railway Postgres)
GT_APP_SETTINGS = "gt_app_settings"
GT_CONTRACTS = "gt_contracts"
GT_CONTRACT_ATTACHMENTS = "gt_contract_attachments"
GT_CONTRACT_INVOICES = "gt_contract_invoices"
GT_SUB_PAYMENTS = "gt_sub_payments"
GT_SUBS = "gt_subs"
GT_SUB_CONTACTS = "gt_sub_contacts"
GT_CONTRACT_SUBS = "gt_contract_subs"
GT_SUBCONTRACT_AGREEMENTS = "gt_subcontract_agreements"
GT_PROPOSALS = "gt_proposals"
GT_CSV_OPPORTUNITIES = "gt_csv_opportunities"
GT_ATTACHMENT_QUEUE = "gt_attachment_queue"

# Product-resale knowledge namespace (GovCon-owned; never sibling `products`)
GT_PRODUCT_REQUIREMENTS = "gt_product_requirements"
GT_KNOWLEDGE_PRODUCTS = "gt_knowledge_products"
GT_KNOWLEDGE_SUPPLIERS = "gt_knowledge_suppliers"
GT_SUPPLIER_OFFERS = "gt_supplier_offers"
GT_FREIGHT_QUOTES = "gt_freight_quotes"
GT_FINANCING_PROVIDERS = "gt_financing_providers"
GT_FINANCING_TERMS = "gt_financing_terms"
GT_RESEARCH_EVENTS = "gt_research_events"
GT_DEAL_STATES = "gt_deal_states"
GT_SAM_API_AUDIT = "gt_sam_api_audit"

# Deal Workspace / CRM foundation
GT_COMPANY_CAPABILITIES = "gt_company_capabilities"
GT_SOLICITATION_DOCUMENTS = "gt_solicitation_documents"
GT_REQUIREMENT_REGISTER = "gt_requirement_register"
GT_SUPPLIER_CONTACTS = "gt_supplier_contacts"
GT_CRM_ACTIVITIES = "gt_crm_activities"
GT_SUPPLIER_PURSUIT_PLANS = "gt_supplier_pursuit_plans"
GT_FINANCING_PURSUITS = "gt_financing_pursuits"
GT_BID_PACKAGES = "gt_bid_packages"
GT_MISSING_INFO_ITEMS = "gt_missing_info_items"
GT_GOVERNMENT_CONTACTS = "gt_government_contacts"
GT_OPERATOR_NOTES = "gt_operator_notes"
GT_CALL_TRANSCRIPTS = "gt_call_transcripts"
GT_ACTION_QUEUE_ITEMS = "gt_action_queue_items"
GT_QUOTE_DOCUMENTS = "gt_quote_documents"

# Broad non-SAM discovery network
GT_DISCOVERY_SOURCES = "gt_discovery_sources"
GT_DISCOVERY_AGENCIES = "gt_discovery_agencies"
GT_DISCOVERED_OPPORTUNITIES = "gt_discovered_opportunities"
GT_OPPORTUNITY_SIGHTINGS = "gt_opportunity_sightings"
GT_DISCOVERY_RUNS = "gt_discovery_runs"

GT_TABLE_RENAMES: list[tuple[str, str]] = [
    ("app_settings", GT_APP_SETTINGS),
    ("contracts", GT_CONTRACTS),
    ("contract_attachments", GT_CONTRACT_ATTACHMENTS),
    ("contract_invoices", GT_CONTRACT_INVOICES),
    ("sub_payments", GT_SUB_PAYMENTS),
    ("subs", GT_SUBS),
    ("sub_contacts", GT_SUB_CONTACTS),
    ("contract_subs", GT_CONTRACT_SUBS),
    ("subcontract_agreements", GT_SUBCONTRACT_AGREEMENTS),
    ("proposals", GT_PROPOSALS),
]

# Shared app table — read-only from GovTracker (owned by GovScraper / sibling app)
GS_WATCHLIST_TABLE = os.getenv("GS_WATCHLIST_TABLE", "gs_watchlist").strip() or "gs_watchlist"
