"""Constants for SAM.gov ContractOpportunitiesFullCSV import."""

from __future__ import annotations

PROTECTED_CSV_STATUSES: frozenset[str] = frozenset(
    {
        "Pursuing",
        "Proposal Built",
        "Submitted",
        "Won",
        "Lost",
    }
)

DELETABLE_CSV_STATUSES: frozenset[str] = frozenset(
    {
        "Reviewing",
        "New",
        "Skipped",
        "Rejected",
        "Stale",
    }
)

CSV_COLUMN_MAP: dict[str, str] = {
    "NoticeId": "notice_id",
    "Title": "title",
    "Sol#": "solicitation_number",
    "Department/Ind.Agency": "agency",
    "Office": "contracting_office",
    "ResponseDeadLine": "due_date",
    "NaicsCode": "naics_code",
    "SetASide": "set_aside",
    "SetASideCode": "set_aside_code",
    "PopCity": "location_city",
    "PopState": "location_state",
    "PrimaryContactFullname": "co_name",
    "PrimaryContactEmail": "co_email",
    "PrimaryContactPhone": "co_phone",
    "Link": "sam_url",
    "Description": "description",
    "Active": "active",
}
