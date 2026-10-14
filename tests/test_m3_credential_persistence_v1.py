"""Credential encryption, overwrite protection, and portal access map."""

from __future__ import annotations


def test_encrypt_decrypt_roundtrip(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "unit-test-secret-key-xyz")
    from m3_source_credentials import decrypt_secret, encrypt_secret

    enc = encrypt_secret("s3cret-value")
    assert enc.startswith("enc:v1:")
    assert "s3cret" not in enc
    assert decrypt_secret(enc) == "s3cret-value"


def test_upsert_blocks_overwrite_without_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "unit-test-secret-key-xyz")
    monkeypatch.setenv("M3_DATA_ROOT", str(tmp_path))

    # Fake AppSetting via in-memory monkeypatch of load/save
    import m3_source_credentials as mod

    store = {"kind": "M3PortalCredentials", "portals": {}, "updated_at": None}

    def _load():
        return dict(store)

    def _save(state):
        store.clear()
        store.update(state)

    monkeypatch.setattr(mod, "_load", _load)
    monkeypatch.setattr(mod, "_save", _save)

    first = mod.upsert_credential(
        "bidnet",
        {"username": "a@example.com", "password": "initial-pass-1"},
    )
    assert first["ok"] is True
    assert first["credential"]["password"] == "********"

    blocked = mod.upsert_credential(
        "bidnet",
        {"username": "b@example.com", "password": "new-pass"},
    )
    assert blocked["ok"] is False
    assert blocked["error"] == "owner_approved_overwrite_required"

    ok = mod.upsert_credential(
        "bidnet",
        {
            "username": "b@example.com",
            "password": "new-pass-2",
            "owner_approved_overwrite": True,
        },
    )
    assert ok["ok"] is True
    secret = mod.get_credential("bidnet", include_secrets=True)
    assert secret["username"] == "b@example.com"
    assert secret["password"] == "new-pass-2"
    assert str(store["portals"]["bidnet"]["password"]).startswith("enc:v1:")


def test_portal_access_map_membership(tmp_path, monkeypatch):
    monkeypatch.setenv("M3_DATA_ROOT", str(tmp_path))
    from m3_portal_access_map import (
        PORTAL_BIDNET,
        bind_membership,
        is_membership_registered,
        seed_bidnet_defaults,
        ui_summary,
    )

    seed_bidnet_defaults(username_hint="abc***")
    bind_membership(
        PORTAL_BIDNET,
        "8411151",
        status="REGISTERED",
        name="Empire State Purchasing Group",
    )
    assert is_membership_registered(PORTAL_BIDNET, "8411151")
    ui = ui_summary()
    assert any(p["portal"] == "bidnet" for p in ui["portals"])
    # No secrets in map file
    raw = (tmp_path / "m3_auth" / "portal_access_map_v1.json").read_text(encoding="utf-8")
    assert "password" not in raw.lower() or "password_set" in raw  # map shouldn't have password keys
    assert "enc:v1:" not in raw


def test_generate_password_complexity():
    from m3_source_credentials import generate_portal_password

    p = generate_portal_password(length=24)
    assert len(p) >= 16
    assert any(c.islower() for c in p)
    assert any(c.isupper() for c in p)
    assert any(c.isdigit() for c in p)


def test_session_verify_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("M3_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("SECRET_KEY", "unit-test-secret-key-xyz")
    from m3_session_persistence import verify_session_restore

    report = verify_session_restore(attempt_live_reuse=False)
    assert report["kind"] == "M3SessionPersistenceVerification"
    assert "portals" in report
    assert report["policy"]["no_plaintext_secrets"] is True
    # Public credentials listing must not leak secrets
    pubs = (report.get("credentials_public") or {}).get("portals") or {}
    for rec in pubs.values():
        assert rec.get("password") in ("", "********", None)
