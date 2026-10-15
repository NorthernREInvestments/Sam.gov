"""Expanded production queue uniqueness / cursor preservation (unit-level)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch


def test_verify_expanded_queue_integrity_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("M3_DATA_ROOT", str(tmp_path))
    from m3_data_root import set_data_root

    set_data_root(tmp_path)
    out = tmp_path / "out"
    out.mkdir(parents=True)
    rows = []
    for i in range(10099):
        rows.append({"opportunity_id": f"orig-{i:05d}", "product_class": "PRODUCT"})
    for i in range(19746):
        rows.append(
            {
                "opportunity_id": f"app-{i:05d}",
                "product_class": "UNKNOWN_PRODUCTNESS",
                "l23_universe_append": True,
            }
        )
    queue = {
        "kind": "FullScaleProductionFunnelQueue",
        "eligible_total": 29845,
        "rows": rows,
    }
    qpath = out / "m3_fspf_v1_eligible_queue.json"
    qpath.write_text(json.dumps(queue), encoding="utf-8")
    man = {
        "appended": 19746,
        "queue_after": 29845,
        "sample_appended_ids": ["app-00000", "app-00001", "app-19745"],
    }
    (out / "m3_l23_universe_append_manifest_v1.json").write_text(
        json.dumps(man), encoding="utf-8"
    )

    import buyer_intelligence.full_scale_production_funnel_v1 as fspf

    monkeypatch.setattr(fspf, "OUT", str(out).replace("\\", "/"))
    # QUEUE_FILE is relative under data_path — patch loader to use our files
    def fake_load(rel: str):
        name = Path(rel).name
        p = out / name
        if not p.exists():
            # also try basename of OUT-relative paths
            p = out / Path(rel).name
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))

    with patch.object(fspf, "_load", side_effect=fake_load):
        with patch.object(fspf, "QUEUE_FILE", str(qpath)):
            # JobStore will see empty jobs — that's fine
            result = fspf.verify_expanded_queue_integrity()
    assert result["unique_ids"] == 29845
    assert result["duplicate_ids"] == 0
    assert result["row_count"] == 29845
    assert result["l23_append_flag_count"] == 19746
    assert result["ok"] is True


def test_verify_detects_duplicates(tmp_path, monkeypatch):
    monkeypatch.setenv("M3_DATA_ROOT", str(tmp_path))
    from m3_data_root import set_data_root

    set_data_root(tmp_path)
    out = tmp_path / "out"
    out.mkdir(parents=True)
    rows = [
        {"opportunity_id": "a", "l23_universe_append": True},
        {"opportunity_id": "a"},
        {"opportunity_id": "b"},
    ]
    (out / "q.json").write_text(
        json.dumps({"eligible_total": 3, "rows": rows}), encoding="utf-8"
    )
    import buyer_intelligence.full_scale_production_funnel_v1 as fspf

    def fake_load(rel: str):
        if "append_manifest" in str(rel):
            return {"appended": 1, "queue_after": 3, "sample_appended_ids": ["a"]}
        return json.loads((out / "q.json").read_text(encoding="utf-8"))

    with patch.object(fspf, "_load", side_effect=fake_load):
        result = fspf.verify_expanded_queue_integrity(
            expected_total=3, expected_original=2, expected_appended=1
        )
    assert result["duplicate_ids"] == 1
    assert result["ok"] is False
