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
