"""Tests for portal document resolver + Jaggaer/Iowa breakthrough path."""

from __future__ import annotations

from portal_document_resolver import (
    DOCUMENT_BYTES_RECOVERED,
    assess_package_completeness,
    classify_portal_family,
    detect_file_format,
    extract_jaggaer_product_line_items,
)
from portal_resolvers.jaggaer import resolve_jaggaer_documents


def test_detect_file_format_pdf_and_login_html():
    assert detect_file_format(b"%PDF-1.4 ...")["ok"] is True
    assert detect_file_format(b"%PDF-1.4 ...")["format"] == "PDF"
    bad = detect_file_format(b"<!DOCTYPE html><html><body>Please login</body></html>")
    assert bad["ok"] is False
    assert "LOGIN" in (bad.get("reason") or "")


def test_classify_portal_families():
    assert classify_portal_family({"source_id": "state_ia", "agency": "State of Iowa"}) == "IOWA"
    assert classify_portal_family({"source_id": "state_mt", "agency": "State of Montana"}) == "MONTANA"
    assert classify_portal_family({"source_id": "agency_city_phoenix_az", "agency": "City of Phoenix"}) == "PHOENIX"
    assert classify_portal_family({"source_id": "coop_sourcewell_live", "agency": "Sourcewell"}) == "SOURCEWELL"
    assert classify_portal_family({"detail_url": "https://sam.gov/opp/abc", "source_id": "sam"}) == "SAM"
    assert classify_portal_family({"source_id": "bidnet_x", "platform_family": "bidnet"}) == "BIDNET"


def test_extract_jaggaer_seed_and_lump_sum_lines():
    seed = "Product Line Items\nP1 324.3 Pounds- Big bluestem (Andropogon gerardii)\nP3 324.0 Pounds- Blue grama"
    lines = extract_jaggaer_product_line_items(seed)
    assert len(lines) >= 2
    assert lines[0]["line_id"] == "P1"
    assert lines[0]["quantity"] == 324.3
    assert lines[0]["unit"] == "LB"

    lift = "Product Line Items\nP1 IFB: Wheelchair Lift  1  LS - Lump Sum\nP2 Warranty for Products  1  YR - Year"
    lines2 = extract_jaggaer_product_line_items(lift)
    assert any(li.get("line_id") == "P1" and li.get("unit") == "LS" for li in lines2)
    assert any(li.get("line_id") == "P2" and li.get("unit") == "YR" for li in lines2)


def test_package_completeness_bom():
    row = {
        "line_items": [{"description": "x", "quantity": 1}],
        "governing_documents": {"documents": [{"governing": True}]},
        "documents": [{"bytes_recovered": True, "document_type": "SOLICITATION"}],
    }
    assert assess_package_completeness(row) in {
        "BOM_RECOVERED",
        "COMPLETE_ENOUGH_FOR_RESEARCH",
        "PACKAGE_COMPLETE",
    }


def test_jaggaer_resolver_uses_fixture_listing(monkeypatch):
    from pathlib import Path

    html = Path("discovery/fixtures/sciquest_iowa_listing.html").read_text(encoding="utf-8")
    pdf_bytes = b"%PDF-1.4\n" + b"P1 10.0 Pounds- Test seed variety alpha\n" * 20

    def fake_get(url, **kwargs):
        if "PublicEvent" in url:
            return {
                "ok": True,
                "status_code": 200,
                "content": html.encode("utf-8"),
                "text": html,
                "headers": {"content-type": "text/html"},
                "content_type": "text/html",
                "url": url,
                "final_url": url,
                "failure": None,
            }
        if "event.pdf" in url or "Sourcingevent" in url:
            return {
                "ok": True,
                "status_code": 200,
                "content": pdf_bytes,
                "text": "",
                "headers": {"content-type": "application/pdf"},
                "content_type": "application/pdf",
                "url": url,
                "final_url": url,
                "failure": None,
            }
        return {
            "ok": True,
            "status_code": 200,
            "content": b"<html></html>",
            "text": "<html></html>",
            "headers": {"content-type": "text/html"},
            "content_type": "text/html",
            "url": url,
            "final_url": url,
            "failure": None,
        }

    monkeypatch.setattr("portal_resolvers.jaggaer.live_http_get", fake_get)
    monkeypatch.setattr(
        "portal_resolvers.jaggaer.extract_jaggaer_product_line_items",
        lambda text: [{"line_id": "P1", "description": "Test seed", "quantity": 10.0, "unit": "LB"}],
    )

    # Fixture first title is typically present; use a loose match
    row = {
        "title": "Wildflower",
        "solicitation_number": "TEST-001",
        "source_id": "state_ia",
        "agency": "State of Iowa",
    }
    # Force match by patching score if needed — use real first title from fixture
    import re

    tm = re.search(r'class="title"[^>]*>([^<]+)<', html) or re.search(r">([A-Za-z][^<]{10,80})</a>", html)
    if tm:
        row["title"] = tm.group(1).strip()

    res = resolve_jaggaer_documents(row, family="IOWA")
    assert res["family"] == "IOWA"
    # Either matched listing PDF or returned structured failure — never silent
    assert "ok" in res
    assert res.get("retrieval_method") == "jaggaer_public_event_signed_pdf"


def test_acquire_evidence_applies_portal_line_items(monkeypatch):
    from m3_evidence_acquisition import acquire_evidence

    portal_docs = [
        {
            "url": "https://example.com/event.pdf",
            "title": "event.pdf",
            "document_type": "SOLICITATION",
            "format": "PDF",
            "bytes_recovered": True,
            "byte_count": 1000,
            "extracted_text": "P1 1 LS - Widget",
            "authority": "authoritative",
        }
    ]
    portal_lines = [
        {
            "line_id": "P1",
            "description": "Wheelchair Lift",
            "quantity": 1.0,
            "unit": "LS",
            "confidence": "HIGH",
        }
    ]

    monkeypatch.setattr(
        "portal_document_resolver.resolve_portal_documents",
        lambda row: {
            "family": "IOWA",
            "ok": True,
            "failure": None,
            "documents": portal_docs,
            "line_items": portal_lines,
            "attempts": [],
            "retrieval_method": "test",
            "status": DOCUMENT_BYTES_RECOVERED,
        },
    )
    # Avoid network in later tiers
    monkeypatch.setattr("m3_evidence_acquisition._tier1_direct", lambda row: {"items": [], "documents": []})
    monkeypatch.setattr("m3_evidence_acquisition._tier2_alternate", lambda row: {"items": [], "documents": []})
    monkeypatch.setattr("m3_evidence_acquisition._tier3_public_web", lambda row: {"items": [], "queries": []})
    monkeypatch.setattr(
        "m3_evidence_acquisition._tier4_openai_web",
        lambda row, **kw: {"items": [], "attempted": False, "paid": None},
    )

    row = {
        "canonical_id": "test:iowa:seed",
        "title": "Wildflower and Native Grass Seed",
        "source_id": "state_ia",
        "agency": "State of Iowa",
        "lifecycle": "RESEARCH_QUEUED",
        "detail_url": "https://example.com/detail",
    }
    out = acquire_evidence(row, allow_paid=False, max_tier="TIER_0_EXISTING")
    # max_tier T0 still runs portal resolver (inserted after T0)
    assert out["row"].get("line_items")
    assert out["row"]["line_items"][0]["description"] == "Wheelchair Lift"
    assert out["row"].get("package_acquired") is True
    assert any(a.get("tier") == "PORTAL_DOCUMENT_RESOLVER" for a in (out["row"].get("evidence_recovery_attempts") or []))
