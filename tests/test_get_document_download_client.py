"""Offline wiring for get_document_download (no live API)."""
from __future__ import annotations

from m3.research.nextbid_controller import DETAIL_ENDPOINTS
from m3.research.solicitation_document_manifest import document_download_retention


def test_get_document_download_in_detail_endpoints():
    assert "get_document_download" in DETAIL_ENDPOINTS


def test_client_method_exists():
    from m3.research import nextbid_client as mod

    assert hasattr(mod.NextBidClient, "get_document_download")


def test_document_download_retention_blocks_when_local(tmp_path, monkeypatch):
    import gzip
    import hashlib

    root = tmp_path / "data"
    ev = root / "evidence_vault"
    sha = hashlib.sha256(b"%PDF-1.4 fake").hexdigest()
    blob = ev / "blobs" / sha[:2] / f"{sha}.gz"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(gzip.compress(b"%PDF-1.4 fake", mtime=0))

    class S:
        def data_root(self=None):
            return root

        def evidence_vault_root(self=None):
            return ev

    monkeypatch.setattr("m3.research.solicitation_document_manifest.get_settings", lambda: S())
    monkeypatch.setattr("m3.research.evidence_vault.get_settings", lambda: S())
    monkeypatch.setattr(
        "m3.research.nextbid_response_store.get_settings",
        lambda: S(),
    )
    st = document_download_retention("NB00000001", sha)
    assert st["already_retained"] is True
    assert st["duplicate_purchase_blocked"] is True
