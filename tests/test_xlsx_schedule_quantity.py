"""Bid-sheet Estimated Usage / Quantity must become real title quantities."""

from __future__ import annotations

import io

from m3.research.documents import (
    extract_xlsx_text,
    prefer_schedule_quantity,
    quantity_is_unspecified,
    summarize_schedule_quantities,
)
from m3.web.titles import build_product_list_title


def _workbook_bytes(rows: list[list[object]]) -> bytes:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Core List"
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_estimated_usage_column_totals_with_uom():
    data = _workbook_bytes(
        [
            ["Facility Name & Address", "Grade/Type", "Estimated Usage", "UOM"],
            ["Facility A", "Extra Coarse Salt Crystals", 63, "BG"],
            ["Facility B", "Extra Coarse Salt Crystals", 1890, "BG"],
            ["Facility C", "Extra Coarse Salt Crystals", 378, "BG"],
        ]
    )
    out = extract_xlsx_text(data)
    assert out["ok"] is True
    assert out["schedule_summary"]["title_quantity"] == "2,331 bags"
    assert out["schedule_summary"]["line_count"] == 3
    assert "SCHEDULE_TOTAL_QUANTITY: 2,331 bags" in (out.get("text") or "")


def test_estimated_usage_with_bag_weight_human_title():
    data = _workbook_bytes(
        [
            [
                "Line #",
                "Facility Name & Address",
                "Grade/Type",
                "Estimated Usage",
                "UoM",
                "Bag Weight",
            ],
            [1, "Facility A", "Extra Coarse Salt Crystals", 63, "BG", 40],
            [2, "Facility B", "Extra Coarse Salt Crystals", 1890, "BG", 40],
            [3, "Facility C", "Extra Coarse Salt Crystals", 378, "BG", 40],
        ]
    )
    out = extract_xlsx_text(data)
    assert out["schedule_summary"]["title_quantity"] == "2,331 40lb bags"
    assert out["schedule_summary"]["bag_weight_lb"] == 40
    qty = prefer_schedule_quantity("Not specified.", out["schedule_summary"])
    title = build_product_list_title(
        what_they_want="Water Softener Salt",
        quantity=qty,
        official_title="Water Softener Salt",
    )
    assert title == "2,331 40lb bags of water softening salt"


def test_prefer_schedule_over_ai_not_specified():
    schedule = summarize_schedule_quantities(
        [
            {"quantity": 63, "uom": "BG", "bag_weight": 40},
            {"quantity": 9000 - 63, "uom": "BG", "bag_weight": 40},
        ]
    )
    assert schedule["title_quantity"] == "9,000 40lb bags"
    assert quantity_is_unspecified("Not specified.")
    assert prefer_schedule_quantity("Not specified.", schedule) == "9,000 40lb bags"
    title = build_product_list_title(
        what_they_want="Water Softener Salt",
        quantity=prefer_schedule_quantity("Not specified.", schedule),
        official_title="Water Softener Salt",
    )
    assert title == "9,000 40lb bags of water softening salt"
