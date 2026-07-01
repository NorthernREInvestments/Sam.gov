"""Contract performance, invoicing, and payment tracking constants."""

from __future__ import annotations

CONTRACT_STATUSES = (
    "new",
    "reviewing",
    "bidding",
    "submitted",
    "skipped",
    "won",
    "lost",
    "awarded",
    "active",
    "option_year",
    "stop_work",
    "completed",
    "not_awarded",
)

PERFORMANCE_STATUSES = (
    "Awarded",
    "Active",
    "Option Year",
    "Stop Work",
    "Completed",
    "Not Awarded",
)

PERFORMANCE_STATUS_DB = {
    "Awarded": "awarded",
    "Active": "active",
    "Option Year": "option_year",
    "Stop Work": "stop_work",
    "Completed": "completed",
    "Not Awarded": "not_awarded",
}

INVOICING_SYSTEMS = ("WAWF", "IPP", "Email", "Paper Check", "Other")

INVOICE_STATUSES = (
    "Not Started",
    "Submitted",
    "Accepted",
    "Paid",
    "Rejected",
    "Overdue",
)

SUB_PAYMENT_STATUSES = (
    "Pending Signoff",
    "Ready to Pay",
    "Paid",
    "Overdue",
)

PAYMENT_METHODS = ("ACH", "Check", "Wire", "Other")

CPARS_RATINGS = (
    "Pending",
    "Exceptional",
    "Very Good",
    "Satisfactory",
    "Marginal",
    "Unsatisfactory",
)

AMENDMENT_MONITOR_STATUSES = frozenset(
    {"bidding", "submitted", "active", "awarded", "option_year", "stop_work", "won", "reviewing"}
)

INVOICE_OVERDUE_DAYS = 45
EXPECTED_PAYMENT_DAYS = 30
SUB_PAYMENT_NET_DAYS = 45
WAWF_PASSWORD_CYCLE_DAYS = 55
WAWF_PASSWORD_WARN_DAYS = 7
