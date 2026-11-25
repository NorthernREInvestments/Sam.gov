"""UOM must be a grounded purchase unit — never an adjacent brand/model token."""
from __future__ import annotations

from m3.research.extract_fields import extract_quantity_fields, normalize_quantity_unit
from m3.research.procurement_requirements import extract_requirements


def test_normalize_rejects_brand_tokens():
    assert normalize_quantity_unit("Unication") is None
    assert normalize_quantity_unit("Ford") is None
    assert normalize_quantity_unit("WEG") is None
    assert normalize_quantity_unit("EA") == "EA"
    assert normalize_quantity_unit("each") == "EA"
    assert normalize_quantity_unit("TON") == "TON"
    assert normalize_quantity_unit("pagers") == "EA"


def test_unication_estimated_qty_does_not_use_brand_as_uom():
    text = (
        "The St. Mary's County Government is seeking Small Procurement Quotes to furnish "
        "and deliver an estimated quantity of 53 Unication G5 Dual Band P25 Voice Pagers, "
        "700-800 MHz/VHF (136-174 MHz), for the Radio Division."
    )
    q = extract_quantity_fields(text, title="Unication G5 Dual Band P25 Voice Pagers")
    assert q.get("quantity_extracted")
    assert float(q.get("quantity") or 0) == 53.0
    unit = (q.get("quantity_unit") or "").upper()
    assert unit != "UNICATION"
    assert unit in {"", "EA"} or q.get("quantity_unit") is None


def test_extract_requirements_unication_uom_not_brand():
    text = (
        "estimated quantity of 53 Unication G5 Dual Band P25 Voice Pagers, "
        "700-800 MHz/VHF (136-174 MHz)"
    )
    r = extract_requirements(
        "Unication G5 Dual Band P25 Voice Pagers",
        description=text,
    )
    assert r["line_items"]
    uom = r["line_items"][0].get("unit_of_measure")
    assert uom != "UNICATION"
    assert (uom or "").upper() != "UNICATION"


def test_grounded_uom_still_extracted():
    q = extract_quantity_fields("Quantity: 250 each of battery chargers for warehouse use.")
    assert float(q["quantity"]) == 250.0
    assert (q.get("quantity_unit") or "").upper() in {"EA", "EACH"}

    r = extract_requirements(
        "Rock salt",
        description="Item # 1 SALT, ROCK, BULK Quantity: 6400 TON delivery to Elk Grove",
    )
    assert r["line_items"]
    assert r["line_items"][0]["quantity"] == 6400.0
    assert r["line_items"][0]["unit_of_measure"] == "TON"
