"""Tests for procurement document discovery adapters + attachment recovery engine."""

from __future__ import annotations

from portal_document_resolver import classify_portal_family
from portal_resolvers.attachment_extract import (
    classify_access_from_html,
    extract_attachment_candidates,
    extract_docs_from_json,
    extract_file_hrefs,
)
from portal_resolvers.sam import extract_sam_notice_id, resolve_sam_documents
from portal_resolvers.portal_families import resolve_bidnet_documents, resolve_bonfire_documents
from m3_document_discovery import (
    ACCESS_BLOCKED,
    AUTH_REQUIRED,
    DOCUMENTS_FOUND,
    NO_DOCUMENTS_AVAILABLE,
    Q_AUTH_REQUIRED,
    Q_DOCUMENTS_FOUND_PENDING_PROCESSING,
    Q_NO_DOCUMENTS_AVAILABLE,
    apply_discovery_to_row,
    apply_va_document_update,
    assign_recovery_queue,
    build_document_discovery_result,
    discover_documents_for_opportunity,
    analyze_document_discovery_top,
)


def test_classify_portal_families_extended():
    assert classify_portal_family({"source_id": "state_ia", "agency": "State of Iowa"}) == "IOWA"
    assert classify_portal_family({"detail_url": "https://sam.gov/opp/abc", "source_id": "sam_live"}) == "SAM"
    assert classify_portal_family({"source_id": "bonfire_city", "platform_family": "bonfire"}) == "BONFIRE"
    assert classify_portal_family({"detail_url": "https://www.bidnetdirect.com/x", "source_id": "bidnet"}) == "BIDNET"
    assert classify_portal_family({"detail_url": "https://procurement.opengov.com/x"}) == "OPENGOV"
    assert classify_portal_family({"detail_url": "https://www.publicpurchase.com/gems/x"}) == "PUBLIC_PURCHASE"
    assert classify_portal_family({"source_id": "state_tx_esbd"}) == "STATE_PORTAL"
    assert classify_portal_family({"source_id": "dla_dibbs", "detail_url": "https://www.dibbs.bsm.dla.mil/x"}) == "DLA"


def test_extract_file_hrefs_and_json_docs():
    html = """
    <html><body>
      <a href="/files/Solicitation.pdf">Solicitation</a>
      <a href="https://example.test/docs/Pricing_Schedule.xlsx">Pricing</a>
      <table><tr><td>Attachment: Technical Specs</td><td><a href="/a/tech_specs.pdf">dl</a></td></tr></table>
    </body></html>
    """
    hrefs = extract_file_hrefs(html, base_url="https://portal.example/")
    urls = {h["url"] for h in hrefs}
    assert any(u.endswith("Solicitation.pdf") for u in urls)
    assert any("Pricing_Schedule.xlsx" in u for u in urls)

    payload = {
        "attachments": [
            {"name": "BOM.xlsx", "downloadUrl": "https://example.test/bom.xlsx", "id": "A1"},
            {"title": "Award Notice", "url": "https://example.test/award.pdf"},
        ]
    }
    docs = extract_docs_from_json(payload)
    assert any(d["attachment_id"] == "A1" for d in docs)
    assert any("award" in d["document_type"] or "award" in d["name"].lower() for d in docs)

    mixed = extract_attachment_candidates(html=html, json_payload=payload, base_url="https://portal.example/")
    assert len(mixed) >= 3


def test_classify_access_from_html():
    assert classify_access_from_html("<html>Please sign in to continue</html>") == "AUTH_REQUIRED"
    assert classify_access_from_html("<html>Vendor registration required</html>") == "REGISTRATION_REQUIRED"
    assert classify_access_from_html("<html>Cloudflare captcha challenge</html>") == "ACCESS_BLOCKED"
    assert classify_access_from_html("<html>Public bid documents</html>") is None


def test_sam_notice_id_and_resolver(monkeypatch):
    row = {
        "canonical_id": "sam-1",
        "title": "Network Gear",
        "source_id": "sam_gov",
        "detail_url": "https://sam.gov/opp/11111111-2222-3333-4444-555555555555/view",
        "notice_id": "11111111-2222-3333-4444-555555555555",
    }
    assert extract_sam_notice_id(row) == "11111111-2222-3333-4444-555555555555"

    monkeypatch.setattr(
        "sam_enrich.fetch_opportunity_attachments",
        lambda notice_id, api_key=None: [
            {
                "type": "file",
                "description": "Combined Synopsis Solicitation.pdf",
                "resource_id": "R1",
                "download_url": "https://sam.gov/api/prod/opps/v3/opportunities/resources/files/R1/download",
            },
            {
                "type": "link",
                "description": "Pricing Schedule.xlsx",
                "url": "https://agency.example/pricing.xlsx",
            },
        ],
    )

    def fake_fetch(url, **kwargs):
        content = b"%PDF-1.4 sam solicitation package"
        if url.endswith(".xlsx"):
            content = b"PK\x03\x04" + b"xl/" + b"\x00" * 100
        return {
            "attempt": {"ok": True, "url": url, "status": 200},
            "document": {
                "url": url.split("?")[0],
                "download_url": url,
                "title": "doc",
                "document_type": "solicitation",
                "bytes_recovered": True,
                "byte_count": len(content),
                "extracted_text": "Part Number ABC-123 Qty 10",
                "authority": "authoritative",
                "source": "sam_attachment",
            },
            "access": None,
        }

    monkeypatch.setattr("portal_resolvers.sam.fetch_document_bytes", fake_fetch)
    res = resolve_sam_documents(row)
    assert res["family"] == "SAM"
    assert res["ok"] is True
    assert res["access_status"] == DOCUMENTS_FOUND
    assert len(res["documents"]) >= 1


def test_bidnet_bonfire_adapters_extract_and_classify(monkeypatch):
    html = """
    <html><body>
      <div class="documents">
        <a href="/downloads/IFB_Package.pdf">Bid Package</a>
        <a href="/downloads/Line_Items.xlsx">Line Items</a>
      </div>
    </body></html>
    """

    def fake_get(url, **kwargs):
        if url.endswith(".pdf"):
            return {
                "ok": True,
                "status_code": 200,
                "content": b"%PDF-1.4 bid package",
                "text": "",
                "headers": {"content-type": "application/pdf"},
                "content_type": "application/pdf",
                "url": url,
                "final_url": url,
                "failure": None,
            }
        if url.endswith(".xlsx"):
            return {
                "ok": True,
                "status_code": 200,
                "content": b"PK\x03\x04xl/" + b"\x00" * 40,
                "text": "",
                "headers": {"content-type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
                "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "url": url,
                "final_url": url,
                "failure": None,
            }
        return {
            "ok": True,
            "status_code": 200,
            "content": html.encode(),
            "text": html,
            "headers": {"content-type": "text/html"},
            "content_type": "text/html",
            "url": url,
            "final_url": url,
            "failure": None,
        }

    monkeypatch.setattr("portal_resolvers.portal_families.live_http_get", fake_get)
    monkeypatch.setattr("portal_document_resolver.live_http_get", fake_get)

    bidnet = resolve_bidnet_documents(
        {
            "title": "City Trucks",
            "source_id": "bidnet_direct",
            "detail_url": "https://www.bidnetdirect.com/public/solicitations/1",
        }
    )
    assert bidnet["family"] == "BIDNET"
    assert bidnet["access_status"] == DOCUMENTS_FOUND
    assert len(bidnet["documents"]) >= 1

    bonfire = resolve_bonfire_documents(
        {
            "title": "School Laptops",
            "source_id": "bonfire",
            "detail_url": "https://university.bonfirehub.com/opportunities/1",
        }
    )
    assert bonfire["family"] == "BONFIRE"
    assert bonfire["ok"] is True


def test_document_discovery_result_and_queues():
    row = {
        "canonical_id": "opp-1",
        "title": "Widget Buy",
        "source_id": "sam_gov",
        "detail_url": "https://sam.gov/opp/x",
    }
    portal = {
        "family": "SAM",
        "ok": True,
        "access_status": DOCUMENTS_FOUND,
        "documents": [
            {
                "url": "https://sam.gov/a.pdf",
                "title": "Solicitation.pdf",
                "document_type": "solicitation",
                "bytes_recovered": True,
                "source": "sam_gov",
            }
        ],
        "attempts": [{"step": "SAM_RESOURCES_API", "ok": True}],
        "line_items": [],
    }
    result = build_document_discovery_result(row, portal)
    assert result["kind"] == "DOCUMENT_DISCOVERY_RESULT"
    assert result["Access_status"] == DOCUMENTS_FOUND
    assert result["Portal_family"] == "SAM"
    assert result["Documents_found"]
    q = assign_recovery_queue(result)
    assert q["queue"] == Q_DOCUMENTS_FOUND_PENDING_PROCESSING

    empty = build_document_discovery_result(
        row,
        {"family": "SAM", "ok": False, "access_status": NO_DOCUMENTS_AVAILABLE, "documents": [], "attempts": []},
    )
    assert assign_recovery_queue(empty)["queue"] == Q_NO_DOCUMENTS_AVAILABLE

    auth = build_document_discovery_result(
        row,
        {"family": "OPENGOV", "ok": False, "access_status": AUTH_REQUIRED, "documents": [], "attempts": []},
    )
    assert assign_recovery_queue(auth)["queue"] == Q_AUTH_REQUIRED


def test_evidence_chain_update_and_processing_handoff(monkeypatch):
    row = {
        "canonical_id": "opp-chain-1",
        "title": "Cisco Switches",
        "source_id": "sam_gov",
        "detail_url": "https://sam.gov/opp/y",
        "documents": [],
        "discovery_evidence": {
            "kind": "DISCOVERY_EVIDENCE_RECORD",
            "Source_URL": "https://sam.gov/opp/y",
            "Source": "sam_gov",
            "Evidence_confidence": "MEDIUM",
            "Document_references": [],
        },
    }
    result = build_document_discovery_result(
        row,
        {
            "family": "SAM",
            "ok": True,
            "access_status": DOCUMENTS_FOUND,
            "documents": [
                {
                    "url": "https://sam.gov/sol.pdf",
                    "title": "Solicitation.pdf",
                    "document_type": "solicitation",
                    "bytes_recovered": True,
                    "extracted_text": "Cisco Catalyst C9300 Qty 25 EA",
                    "source": "sam_gov",
                }
            ],
            "attempts": [],
            "line_items": [{"description": "Cisco Catalyst C9300", "quantity": 25, "unit": "EA"}],
        },
    )
    patched = apply_discovery_to_row(row, result)
    assert any(d.get("bytes_recovered") for d in patched["documents"])
    assert patched.get("line_items")
    assert patched.get("document_discovery", {}).get("Access_status") == DOCUMENTS_FOUND
    assert patched.get("evidence_chain") or patched.get("document_discovery")


def test_failure_classification_blocked(monkeypatch):
    def fake_resolve(row):
        return {
            "family": "BONFIRE",
            "ok": False,
            "access_status": ACCESS_BLOCKED,
            "access_state": "BOT_PROTECTED",
            "documents": [],
            "documents_missing": ["attachments"],
            "attempts": [{"step": "DETAIL_PAGE", "failure": "BOT_CHALLENGE"}],
            "line_items": [],
        }

    monkeypatch.setattr("m3_document_discovery.resolve_portal_documents", fake_resolve)
    monkeypatch.setattr("m3_document_discovery.classify_portal_family", lambda row: "BONFIRE")
    item = discover_documents_for_opportunity(
        {
            "canonical_id": "blocked-1",
            "title": "Blocked Opp",
            "source_id": "bonfire",
            "detail_url": "https://x.bonfirehub.com/1",
        },
        run_processing=False,
    )
    assert item["DOCUMENT_DISCOVERY_RESULT"]["Access_status"] == ACCESS_BLOCKED
    assert item["RECOVERY_QUEUE"]["queue"] == "ACCESS_BLOCKED"
    assert "BYPASS" not in str(item["VA"]["allowed_actions"])


def test_analyze_top_and_va_attach(monkeypatch):
    class FakeStore:
        def __init__(self):
            self._rows = {
                "c1": {
                    "canonical_id": "c1",
                    "title": "Servers",
                    "source_id": "sam_gov",
                    "detail_url": "https://sam.gov/opp/z",
                    "documents": [],
                }
            }

        def all(self):
            return list(self._rows.values())

        def get(self, cid):
            return dict(self._rows.get(cid) or {})

        def save(self):
            return None

    monkeypatch.setattr(
        "m3_document_discovery.resolve_portal_documents",
        lambda row: {
            "family": "SAM",
            "ok": True,
            "access_status": DOCUMENTS_FOUND,
            "documents": [
                {
                    "url": "https://sam.gov/a.pdf",
                    "title": "Solicitation.pdf",
                    "document_type": "solicitation",
                    "bytes_recovered": True,
                    "extracted_text": "Dell R750 Qty 2",
                    "source": "sam_gov",
                }
            ],
            "attempts": [],
            "line_items": [{"description": "Dell R750", "quantity": 2, "unit": "EA"}],
            "retrieval_method": "sam_resources_api",
        },
    )
    monkeypatch.setattr(
        "m3_document_discovery.load_discovery_index",
        lambda: {"kind": "M3DocumentDiscoveryIndex", "by_id": {}},
    )
    monkeypatch.setattr("m3_document_discovery.save_discovery_index", lambda idx: None)

    store = FakeStore()
    run = analyze_document_discovery_top(store, limit=5, run_processing=True, persist=True)
    assert run["kind"] == "M3DocumentDiscoveryRun"
    assert run["DOCUMENT_RECOVERY"]["Opportunities_analyzed"] == 1
    assert run["NEXT_STATE"] == "PROCUREMENT_DOCUMENT_RECOVERY_OPERATIONAL"
    assert store._rows["c1"].get("documents")

    # VA cannot bypass
    denied = apply_va_document_update(store, "c1", action="BYPASS_ACCESS")
    assert denied["ok"] is False

    ok = apply_va_document_update(
        store,
        "c1",
        action="ATTACH_PERMITTED_FILE",
        attached_document={"url": "https://agency.example/extra.pdf", "title": "Extra.pdf", "bytes_recovered": True},
        note="Public file obtained offline",
    )
    assert ok["ok"] is True
    assert any(d.get("title") == "Extra.pdf" for d in store._rows["c1"]["documents"])
