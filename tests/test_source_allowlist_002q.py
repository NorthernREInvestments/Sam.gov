"""Build 002Q — owner allowlist gates. No live API calls."""

from __future__ import annotations

from m3.research import source_allowlist as al


def test_default_allowlist_nextbid_offline_only(tmp_path, monkeypatch):
    monkeypatch.setenv("M3_DATA_ROOT", str(tmp_path))
    from m3.config import get_settings

    get_settings.cache_clear()
    data = al.load_allowlist()
    assert data["discovery_auto_activation"] is False
    assert data["openai_source_discovery"] is False
    assert al.is_owner_approved("nextbid_state_local")
    assert al.can_execute_live_acquisition("nextbid_state_local") is False
    assert al.can_execute_live_acquisition("discovered_anything") is False
    assert al.is_discovered_source_key("discovered_foo")
    get_settings.cache_clear()


def test_discovered_never_live_without_explicit_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("M3_DATA_ROOT", str(tmp_path))
    from m3.config import get_settings

    get_settings.cache_clear()
    data = al.load_allowlist()
    data["sources"]["discovered_example"] = {
        "approved": True,
        "live_acquisition": False,
    }
    al.save_allowlist(data)
    assert al.is_owner_approved("discovered_example")
    assert al.can_execute_live_acquisition("discovered_example") is False
    data["sources"]["verified_free_example"] = {
        "approved": True,
        "live_acquisition": True,
        "acquisition_url": "https://example.gov/bids",
    }
    al.save_allowlist(data)
    assert al.can_execute_live_acquisition("verified_free_example") is True
    get_settings.cache_clear()
