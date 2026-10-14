"""Regression: commercial evidence index reconnects SPF historical prices."""

from __future__ import annotations

from buyer_intelligence.commercial_evidence_index_v1 import _norm_title, _stem


def test_stem_normalization():
    assert _stem("Socket Cap Screws — Grade 8") == "socket cap screws grade 8"
    assert len(_norm_title("  Foo   BAR  ")) > 0


def test_build_index_smoke(tmp_path, monkeypatch):
    import buyer_intelligence.commercial_evidence_index_v1 as m

    # Point data_path into tmp with minimal SPF fixture
    out = tmp_path / "_og_commercial_research_pipeline_out"
    out.mkdir(parents=True)
    (tmp_path / "dummy").mkdir(exist_ok=True)

    def fake_data_path(rel: str) -> str:
        return str(tmp_path / rel)

    monkeypatch.setattr(m, "data_path", fake_data_path)
    spf = {
        "records": [
            {
                "product": "SOCKET CAP SCREW",
                "unit_price": "1.25",
                "mpn": "SCS-1",
                "match_level": "EXACT",
            }
        ]
    }
    (out / "m3_spf_v1_historical_unit_price_store.json").write_text(
        __import__("json").dumps(spf), encoding="utf-8"
    )
    idx = m.build_commercial_evidence_index()
    assert idx["by_product_stem_count"] >= 1
    hit = m.lookup_evidence_for_opportunity("noid", title="SOCKET CAP SCREW kit", index=idx)
    assert hit.get("historical_unit") == "1.25"
