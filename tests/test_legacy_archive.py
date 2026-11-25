import json

import pytest

from m3.research.legacy_archive import build_legacy_archive, restore_legacy_archive, verify_legacy_archive


def test_archive_dedupes_verifies_and_restores(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    (a / "sub").mkdir(parents=True)
    b.mkdir()
    (a / "sub" / "x.json").write_bytes(b'{"x": 1}')
    (a / "pricing.json").write_bytes(b"prices")
    (b / "x_copy.json").write_bytes(b'{"x": 1}')
    arch = tmp_path / "out" / "legacy.zip"
    res = build_legacy_archive({"a": a, "b": b}, archive_path=arch, keep_active=[a / "pricing.json"])
    assert res["files"] == 3 and res["unique_blobs"] == 2 and res["keep_active"] == 1
    assert (a / "sub" / "x.json").exists()  # nothing deleted
    assert verify_legacy_archive(arch)["ok"]
    purge = [json.loads(l) for l in open(res["purge_manifest"], encoding="utf-8")]
    assert {p["action"] for p in purge} == {"KEEP_ACTIVE", "ELIGIBLE_FOR_REMOVAL_AFTER_OWNER_AUTHORIZATION"}
    out = restore_legacy_archive(arch, tmp_path / "restore")
    assert out["restored"] == 3
    assert (tmp_path / "restore" / "b" / "x_copy.json").read_bytes() == b'{"x": 1}'
    with pytest.raises(FileExistsError):
        restore_legacy_archive(arch, tmp_path / "restore")
    with pytest.raises(FileExistsError):
        build_legacy_archive({"a": a}, archive_path=arch)
