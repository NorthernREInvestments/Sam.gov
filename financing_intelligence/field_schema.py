"""Canonical lender/source qualification field keys (verified facts only).

Blank values never invent defaults for calculations.
"""

from __future__ import annotations

# Deal eligibility
DEAL_ELIGIBILITY_FIELDS = (
    "minimum_transaction_size",
    "maximum_transaction_size",
    "preferred_deal_size_min",
    "preferred_deal_size_max",
    "preferred_deal_size",
    "minimum_gross_margin_pct",
    "minimum_expected_profit",
    "federal_contracts_accepted",
    "state_contracts_accepted",
    "local_contracts_accepted",
    "cooperative_contracts_accepted",
    "product_resale_supported",
    "service_contracts_supported",
    "construction_supported",
    "excluded_industries",
    "excluded_product_categories",
)

# Borrower eligibility
BORROWER_ELIGIBILITY_FIELDS = (
    "startup_new_company_allowed",
    "minimum_time_in_business_months",
    "minimum_annual_revenue",
    "minimum_historical_transactions",
    "business_credit_requirement",
    "personal_credit",
    "personal_guarantee",
    "owner_fico_minimum",
    "ucc_filing_required",
    "lien_requirement",
    "other_collateral_requirement",
)

# Transaction mechanics
TRANSACTION_MECHANICS_FIELDS = (
    "typical_advance_pct",
    "max_advance_pct",
    "minimum_owner_equity_contribution_pct",
    "supplier_deposit_funding_allowed",
    "pays_supplier_directly",
    "can_fund_freight",
    "can_fund_taxes",
    "can_fund_duties_import",
    "partial_shipment_funding_supported",
    "multiple_supplier_funding_supported",
    "purchase_order_financing_supported",
    "receivables_factoring_supported",
    "assignment_of_claims",  # REQUIRED / ALLOWED / UNSUPPORTED / UNKNOWN
    "government_receivable_purchase_supported",
    "can_fund_100_percent_supplier_invoice",
)

# Approval mechanics
APPROVAL_MECHANICS_FIELDS = (
    "lender_prequalification_supported",
    "every_transaction_requires_separate_approval",
    "underwriting_days_min",
    "underwriting_days_max",
    "expedited_underwriting_available",
    "documents_normally_required",
    "award_required_before_approval",
    "supplier_quote_required",
    "buyer_po_contract_required",
    "government_payment_terms_required",
    "supplier_confirmation_required",
)

# Pricing (verified only)
PRICING_FIELDS = (
    "origination_fee",
    "transaction_fee",
    "minimum_fee",
    "fee_pct_of_advance",
    "discount_rate",
    "monthly_rate",
    "weekly_rate",
    "daily_rate",
    "factoring_rate",
    "wire_fee",
    "documentation_fee",
    "extension_fee",
    "other_verified_fee",
    "fee_calculation_notes",
)

ALL_LENDER_TERM_FIELDS = (
    DEAL_ELIGIBILITY_FIELDS
    + BORROWER_ELIGIBILITY_FIELDS
    + TRANSACTION_MECHANICS_FIELDS
    + APPROVAL_MECHANICS_FIELDS
    + PRICING_FIELDS
)

PROVENANCE_FIELDS = (
    "evidence_type",
    "source_url",
    "call_contact_reference",
    "entered_by",
    "entered_at",
    "verified_by",
    "verified_at",
    "confidence",
    "notes",
)
