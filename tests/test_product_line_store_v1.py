"""Regression tests for canonical product-line persistence."""

from __future__ import annotations

from buyer_intelligence.product_line_store_v1 import (
    _merge_line,
    get_lines,
    load_store,
    persist_ht_lines,
    upsert_opportunity_lines,
)


def test_upsert_merge_prefers_richer_mpn():
    oid = "__test_pl_store_merge__"
    upsert_opportunity_lines(
        oid,
        [{"LINE_NUMBER": "1", "DESCRIPTION": "Brake kit", "QTY": 10, "UOM": "EA"}],
        meta={"TITLE": "Test", "PROVENANCE_SOURCES": ["weak"]},
        persist=True,
    )
    upsert_opportunity_lines(
        oid,
        [
            {
                "LINE_NUMBER": "1",
                "DESCRIPTION": "Brake kit Meritor",
                "QTY": 10,
                "UOM": "EA",
                "MANUFACTURER": "Meritor",
                "PART_NUMBER": "82-59990-000DD",
            }
        ],
        meta={"PROVENANCE_SOURCES": ["rich"]},
        persist=True,
    )
    lines = get_lines(oid)
    assert len(lines) == 1
    assert lines[0].get("MPN") == "82-59990-000DD"
    assert lines[0].get("MANUFACTURER") == "Meritor"


def test_merge_does_not_wipe_existing_qty():
    a = {"DESCRIPTION": "Item", "QTY": 5, "UOM": "EA", "MPN": None}
    b = {"DESCRIPTION": "Item", "QTY": None, "UOM": None, "MPN": "ABC1234"}
    m = _merge_line(a, b)
    assert m.get("QTY") == 5
    assert m.get("MPN") == "ABC1234"


def test_ht_persist_helper_roundtrip():
    oid = "__test_ht_persist__"
    stored = persist_ht_lines(
        oid,
        [
            {"description": "Paper towels", "qty": 12, "uom": "CS", "LINE_NUMBER": 1},
            {"description": "Toilet paper SCA #TM1616S", "qty": 100, "uom": "CS", "LINE_NUMBER": 2, "PART_NUMBER": "TM1616S", "MANUFACTURER": "SCA"},
        ],
        rec={"title": "Paper supplies", "buyer": "City Test", "source": "OpenGov"},
    )
    assert stored is not None
    assert stored["LINE_COUNT"] >= 2
    load_store(force=True)
    lines = get_lines(oid)
    assert len(lines) >= 2
    assert any(ln.get("MPN") == "TM1616S" for ln in lines)
