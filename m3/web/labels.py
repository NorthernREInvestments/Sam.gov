"""Human-readable labels for status codes and categories."""

from __future__ import annotations

STATUS_LABELS: dict[str, str] = {
    "not_started": "Not Started",
    "in_progress": "In Progress",
    "submitted": "Submitted",
    "pending": "Pending",
    "approved": "Approved",
    "denied": "Denied",
    "n_a": "N/A",
    "lead": "Lead",
    "contacted": "Contacted",
    "active": "Active",
    "inactive": "Inactive",
    "blocked": "Blocked",
    "unknown": "Unknown",
    "yes": "Yes",
    "no": "No",
    "saved": "Saved",
    "pursuing": "Actively Pursuing",
    "archived": "Archived",
    "not_interested": "Not Interested",
    "discovered": "Discovered",
    "owner": "Owner",
    "supplier_manager": "Supplier Manager",
    "contract_specialist": "Contract Specialist",
    "custom": "Custom",
    "product_supply": "Product Supply",
    "product_with_incidental_services": "Product with Incidental Services",
    "services": "Services",
    "construction": "Construction",
    "unknown_mixed": "Unknown / Mixed",
    "general_products": "General Products",
    "not_yet_researched": "Not Yet Researched",
    "research_in_progress": "Research In Progress",
    "verified": "Verified",
    "not_published": "Not Published",
    "unable_to_retrieve": "Unable to Retrieve",
    "published_estimate": "Published Estimate",
    "published_amount": "Published Amount",
    "published_estimate_band": "Published Estimate Band",
    "historical_award": "Historical Award",
    "comparable_purchase": "Comparable Purchase",
    "quantity_market_benchmark": "Quantity × Market Benchmark",
    "headquarters": "Headquarters",
    "sales_office": "Sales Office",
    "regional_office": "Regional Office",
    "distribution_location": "Distribution Location",
}


def label(code: str | None, *, fallback: str | None = None) -> str:
    if code is None or str(code).strip() == "":
        return fallback or "Unknown"
    raw = str(code).strip()
    key = raw.lower().replace(" ", "_")
    if key in STATUS_LABELS:
        return STATUS_LABELS[key]
    # Snake/category → Title Case words
    if "_" in raw or raw.islower():
        return " ".join(w.capitalize() for w in raw.replace("-", "_").split("_") if w)
    return raw
