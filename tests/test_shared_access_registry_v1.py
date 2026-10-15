"""Shared-access fast path — known PG/paid barriers defer without browser."""

from __future__ import annotations

from buyer_intelligence import shared_access_registry_v1 as reg


def test_solicitation_key_and_learn_resolve(tmp_path, monkeypatch):
    monkeypatch.setattr(reg, "registry_path", lambda: tmp_path / "reg.json")
    monkeypatch.setattr(reg, "_pg_status", lambda pg: "QUEUED")
    monkeypatch.setattr(
        "bidnet_auth.purchasing_group_tracker.note_blocked_opportunity",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "bidnet_auth.purchasing_group_queue.enqueue_group_registration",
        lambda *a, **k: {"ok": True},
    )

    url = "https://www.bidnetdirect.com/public/supplier/solicitations/statewide/44418066865"
    reg.learn_access(
        opportunity_id="oid-a",
        url=url,
        buyer="City of Springfield",
        purchasing_group_id="8407551",
        blocker="AWAITING_PURCHASING_GROUP_REGISTRATION",
    )
    # Second observation for buyer consistency
    reg.learn_access(
        opportunity_id="oid-b",
        url="https://www.bidnetdirect.com/public/supplier/solicitations/statewide/44418066899",
        buyer="City of Springfield",
        purchasing_group_id="8407551",
    )

    # Exact solicitation hit
    d1 = reg.resolve_fast_access(
        {
            "opportunity_id": "oid-new",
            "authoritative_url": url,
            "buyer": "Other",
        }
    )
    assert d1["decision"] == "ACCESS_BLOCKED_KNOWN_PG"
    assert d1["defer"] is True
    assert d1["access_fast_path"] is True
    assert d1["purchasing_group_id"] == "8407551"

    # Buyer-consistent hit for a new solicitation
    d2 = reg.resolve_fast_access(
        {
            "opportunity_id": "oid-c",
            "authoritative_url": "https://www.bidnetdirect.com/public/supplier/solicitations/statewide/99999999999",
            "buyer": "City of Springfield",
        }
    )
    assert d2["decision"] == "ACCESS_BLOCKED_KNOWN_PG"
    assert d2["purchasing_group_id"] == "8407551"


def test_registered_pg_not_deferred(tmp_path, monkeypatch):
    monkeypatch.setattr(reg, "registry_path", lambda: tmp_path / "reg2.json")
    monkeypatch.setattr(reg, "_pg_status", lambda pg: "REGISTERED")
    reg.learn_access(
        opportunity_id="oid-r",
        url="https://www.bidnetdirect.com/public/supplier/solicitations/statewide/44418000111",
        purchasing_group_id="8411151",
    )
    d = reg.resolve_fast_access(
        {
            "opportunity_id": "oid-r2",
            "authoritative_url": "https://www.bidnetdirect.com/public/supplier/solicitations/statewide/44418000111",
        }
    )
    assert d["decision"] == "ACCESS_VERIFIED"
    assert d["defer"] is False


def test_unknown_when_no_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(reg, "registry_path", lambda: tmp_path / "reg3.json")
    d = reg.resolve_fast_access(
        {
            "opportunity_id": "fresh",
            "authoritative_url": "https://www.bidnetdirect.com/public/supplier/solicitations/statewide/42",
            "buyer": "Unknown Agency XYZ",
        }
    )
    assert d["decision"] == "UNKNOWN"
    assert d["defer"] is False
