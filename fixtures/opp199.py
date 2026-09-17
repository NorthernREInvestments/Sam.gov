"""Opportunity 199 regression fixture — allowed Opp199-specific content."""

from __future__ import annotations

from datetime import date
from typing import Any

OPP199_ID = 199
OPP199_NOTICE = "610bea2df5c1436994e316259e29a9a6"
OPP199_SOLICITATION = "47QACA26Q0439"

OPP199_BOM: list[dict[str, Any]] = [
    {"component": "base_system", "value": "Dell PowerEdge R670", "quantity": 14, "status": "VERIFIED"},
    {"component": "part_number", "value": "210-BNZH", "quantity": 14, "status": "VERIFIED"},
    {
        "component": "memory_module",
        "value": "16GB RDIMM 6400MT/s, Single Rank",
        "quantity": 112,
        "quantity_per_server": 8,
        "status": "VERIFIED",
    },
    {
        "component": "memory_module_quantity_per_server",
        "value": 8,
        "quantity": 8,
        "status": "CALCULATED",
    },
    {
        "component": "storage_drive",
        "value": "2.4TB SAS",
        "quantity": 84,
        "quantity_per_server": 6,
        "status": "VERIFIED",
    },
    {
        "component": "installation",
        "value": "On-Site Installation Declined",
        "quantity": 14,
        "status": "VERIFIED",
    },
]

OPP199_SUPPLIER_NAMES = [
    "Dell Technologies Federal / Dell Federal Systems L.P.",
    "CDW",
]

OPP199_DESTINATION = "2415 Eisenhower Avenue, Alexandria, VA 22314"
OPP199_DELIVERY = "within 30 days ARO"
OPP199_CLOSE_LABEL = "2026-09-16 5:00PM PST"


def opp199_critical_facts() -> list[dict[str, Any]]:
    from missing_info import (
        FACT_COMMERCIAL,
        FACT_FINANCING,
        FACT_SOLICITATION,
        missing_info_record,
    )

    return [
        missing_info_record(
            fact_key="memory_module_quantity_per_server",
            description="Required memory module quantity per server",
            fact_class=FACT_SOLICITATION,
            necessary_for_bid=True,
            necessary_for_execution=True,
        ),
        missing_info_record(
            fact_key="storage_drive_quantity",
            description="Required storage drive quantity per server",
            fact_class=FACT_SOLICITATION,
            necessary_for_bid=True,
            necessary_for_execution=True,
        ),
        missing_info_record(
            fact_key="installation_requirement",
            description="Whether installation/setup services are required",
            fact_class=FACT_SOLICITATION,
            necessary_for_bid=True,
            necessary_for_execution=True,
        ),
        missing_info_record(
            fact_key="freight_fob_responsibility",
            description="FOB / freight / shipping responsibility and cost allocation",
            fact_class=FACT_SOLICITATION,
            necessary_for_bid=True,
            necessary_for_execution=True,
        ),
        missing_info_record(
            fact_key="supplier_acquisition_price",
            description="Current supplier acquisition price for exact BOM",
            fact_class=FACT_COMMERCIAL,
            necessary_for_bid=True,
            necessary_for_execution=True,
        ),
        missing_info_record(
            fact_key="financing_pg_requirement",
            description="Whether financing requires personal guarantee",
            fact_class=FACT_FINANCING,
            necessary_for_bid=False,
            necessary_for_execution=True,
        ),
    ]


def opp199_verified_contexts() -> dict[str, str]:
    return {
        "memory_module_quantity_per_server": "Configuration identifies memory module type from solicitation",
        "storage_drive_quantity": "Configuration references storage drives from solicitation",
        "installation_requirement": "Product resale RFQ — verify whether installation is required",
        "freight_fob_responsibility": "Delivery destination and timing per solicitation",
    }


def seed_opp199_commercial_artifacts(**kwargs: Any) -> dict[str, Any]:
    from commercial_execution import seed_commercial_artifacts

    defaults = {
        "solicitation_number": OPP199_SOLICITATION,
        "agency": "GSA/FAS",
        "bom": OPP199_BOM,
        "destination": OPP199_DESTINATION,
        "delivery_requirement": OPP199_DELIVERY,
        "installation_note": "On-Site Installation Declined — not required (VERIFIED)",
        "quantity": 14,
        "product_summary": "Dell PowerEdge R670 servers (part 210-BNZH)",
        "suppliers": [{"id": i + 1, "name": n, "contact_verified": False} for i, n in enumerate(OPP199_SUPPLIER_NAMES)],
        "due_date": date(2026, 9, 16),
    }
    defaults.update(kwargs)
    return seed_commercial_artifacts(**defaults)
