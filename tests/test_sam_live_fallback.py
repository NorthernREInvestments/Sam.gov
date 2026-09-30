"""Regression tests for SAM live multi-source fallback retrieval."""

from __future__ import annotations

from sam_live_fallback import (
    DISCOVERY_DEGRADED,
    LIVE_AGENCY_SOURCE_CONFIRMED,
    LIVE_API_CONFIRMED,
    LIVE_ATTACHMENT_CONFIRMED,
    LIVE_PUBLIC_WEB_CONFIRMED,
    LIVE_SOURCE_UNAVAILABLE,
    PUBLIC_WEB_BLOCKED,
    SAM_API_AUTH_FAILED,
    SAM_API_OK,
    SAM_API_TIMEOUT,
    SOURCE_CONFLICT,
    STALE_CACHE_ONLY,
    classify_sam_api_failure,
    discovery_coverage_after_api_failure,
    parse_qty_uom_from_text,
    prefer_live_field,
    retrieve_sam_live,
)


PIPEFITTER_NOTICE = "40c00954331b4d67953ad235dc2242b5"


def test_classify_api_401():
    assert classify_sam_api_failure(status_code=401) == SAM_API_AUTH_FAILED
    assert classify_sam_api_failure(error="HTTP_401") == SAM_API_AUTH_FAILED


def test_api_success_path():
    def api(_nid):
        return {
            "ok": True,
            "api_failure": SAM_API_OK,
            "found": True,
            "raw": {
                "title": "TOOL KIT PIPEFITTER",
                "solicitationNumber": "W912CH-26-B-A015",
                "responseDeadLine": "2026-10-09T15:30:00-04:00",
                "typeOfSetAside": "SBA",
                "quantity": "40 EA",
            },
        }

    out = retrieve_sam_live(
        notice_id=PIPEFITTER_NOTICE,
        api_fetcher=api,
        public_page_fetcher=lambda nid: {"ok": False},
        public_detail_fetcher=lambda nid, key=None: {"ok": False},
        fetch_attachments=False,
    )
    assert out["confirmation"] == LIVE_API_CONFIRMED
    assert out["live_verified"] is True
    assert out["api_failure"] == SAM_API_OK
    assert out["response_deadline"] == "2026-10-09T15:30:00-04:00"
    assert out["qty_uom"]["confirmed"] is True
    assert out["qty_uom"]["quantity"] == 40.0


def test_api_401_public_fallback_succeeds():
    def api(_nid):
        return {"ok": False, "api_failure": SAM_API_AUTH_FAILED, "error": "HTTP_401", "raw": None}

    def page(_nid):
        return {
            "ok": False,
            "blocked": PUBLIC_WEB_BLOCKED,
            "umbrella_key": "test-umbrella",
            "reason": "spa_shell_no_opportunity_facts",
        }

    def detail(_nid, key=None):
        assert key == "test-umbrella"
        return {
            "ok": True,
            "url": f"https://sam.gov/api/prod/opps/v2/opportunities/{_nid}",
            "data": {
                "status": {"code": "published", "value": "Published"},
                "archived": False,
                "cancelled": False,
                "latest": True,
                "description": [
                    {
                        "body": "<p>Quantity: 40 EA toolkit</p>",
                    }
                ],
                "data2": {
                    "title": "Solicitation Toolkit Pipefitter NSN: 5180-00-596-1509",
                    "solicitationNumber": "BA015",
                    "version": "2",
                    "solicitation": {
                        "setAside": "SBA",
                        "deadlines": {"response": "2026-10-09T15:30:00-04:00"},
                    },
                },
            },
        }

    out = retrieve_sam_live(
        notice_id=PIPEFITTER_NOTICE,
        api_fetcher=api,
        public_page_fetcher=page,
        public_detail_fetcher=detail,
        fetch_attachments=False,
    )
    assert out["api_failure"] == SAM_API_AUTH_FAILED
    assert out["confirmation"] == LIVE_PUBLIC_WEB_CONFIRMED
    assert out["live_verified"] is True
    assert out["open_status"] == "OPEN"
    assert out["response_deadline"] == "2026-10-09T15:30:00-04:00"
    assert out["qty_uom"]["quantity"] == 40.0
    assert out["discovery_coverage"] == DISCOVERY_DEGRADED


def test_api_timeout_public_fallback_succeeds():
    def api(_nid):
        return {"ok": False, "api_failure": SAM_API_TIMEOUT, "error": "timeout", "raw": None}

    def detail(_nid, key=None):
        return {
            "ok": True,
            "url": "https://sam.gov/api/prod/opps/v2/opportunities/x",
            "data": {
                "status": {"code": "published"},
                "archived": False,
                "cancelled": False,
                "data2": {
                    "title": "Widget",
                    "solicitation": {"deadlines": {"response": "2026-11-01T12:00:00-05:00"}, "setAside": None},
                },
                "description": [{"body": "Buy 12 EA of widget"}],
            },
        }

    out = retrieve_sam_live(
        notice_id="abc",
        api_fetcher=api,
        public_page_fetcher=lambda nid: {"ok": False, "umbrella_key": "k"},
        public_detail_fetcher=detail,
        fetch_attachments=False,
    )
    assert out["api_failure"] == SAM_API_TIMEOUT
    assert out["confirmation"] == LIVE_PUBLIC_WEB_CONFIRMED
    assert out["qty_uom"]["confirmed"] is True


def test_api_failure_attachment_confirms_live_data():
    def api(_nid):
        return {"ok": False, "api_failure": SAM_API_AUTH_FAILED, "raw": None}

    def detail(_nid, key=None):
        return {
            "ok": True,
            "url": "https://sam.gov/api/prod/opps/v2/opportunities/x",
            "data": {
                "status": {"code": "published"},
                "archived": False,
                "cancelled": False,
                "data2": {
                    "title": "Pipefitter",
                    "solicitation": {"deadlines": {"response": "2026-10-09T15:30:00-04:00"}, "setAside": "SBA"},
                    "solicitationNumber": "BA015",
                    "version": "2",
                },
                "description": [{"body": "TDP distribution code A. No qty here."}],
            },
        }

    def alist(_nid, key=None):
        return {
            "ok": True,
            "attachments": [
                {"name": "W912CH26BA015.pdf", "resource_id": "RID1", "posted_date": "2026-09-02"},
            ],
        }

    def adl(rid, key=None):
        assert rid == "RID1"
        # Minimal PDF-like bytes not required — extractor patched via text in content path:
        # use plain text through extract by providing non-pdf and mocking is hard;
        # instead return bytes that document_ingestion won't parse, and inject via
        # monkeypatch-free path: put Quantity in a fake .txt by naming .pdf and
        # relying on extract_text_from_attachment_bytes falling through.
        # Better: return PDF text via monkeypatch in this test using a .txt name.
        return {"ok": True, "url": f"file/{rid}", "content": b"Quantity: 40\nToolkit kit item EA schedule"}

    # Force text extraction by naming as .txt in list — downloader uses name from list
    def alist_txt(_nid, key=None):
        return {
            "ok": True,
            "attachments": [
                {"name": "schedule.txt", "resource_id": "RID1", "posted_date": "2026-09-02"},
            ],
        }

    # Only PDFs are downloaded in retrieve_sam_live — so use PDF name and patch extractor
    import sam_live_fallback as mod

    original = mod.extract_text_from_attachment_bytes

    def fake_extract(data, *, filename):
        return "Quantity: 40\nPipefitter Supplemental Toolkit (PSTK)\nUnexercised Production 40\n"

    mod.extract_text_from_attachment_bytes = fake_extract
    try:
        out = retrieve_sam_live(
            notice_id=PIPEFITTER_NOTICE,
            api_fetcher=api,
            public_page_fetcher=lambda nid: {"ok": False, "umbrella_key": "k"},
            public_detail_fetcher=detail,
            attachment_list_fetcher=alist,
            attachment_downloader=adl,
            fetch_attachments=True,
            max_attachments=1,
        )
    finally:
        mod.extract_text_from_attachment_bytes = original

    assert out["confirmation"] == LIVE_ATTACHMENT_CONFIRMED
    assert out["qty_uom"]["quantity"] == 40.0
    assert out["qty_uom"]["option_quantity"] == 40.0
    assert out["qty_uom"]["confirmed"] is True
    assert "W912CH26BA015.pdf" in out["attachment_names"]


def test_api_and_public_fail_agency_succeeds():
    def api(_nid):
        return {"ok": False, "api_failure": SAM_API_AUTH_FAILED, "raw": None}

    out = retrieve_sam_live(
        notice_id="n1",
        agency_url="https://agency.example/sol.pdf",
        api_fetcher=api,
        public_page_fetcher=lambda nid: {"ok": False},
        public_detail_fetcher=lambda nid, key=None: {"ok": False},
        agency_fetcher=lambda url: {
            "ok": True,
            "body": "Quantity: 5 EA engines",
            "response_deadline": "2026-12-01",
            "open_status": "OPEN",
        },
        fetch_attachments=False,
    )
    assert out["confirmation"] == LIVE_AGENCY_SOURCE_CONFIRMED
    assert out["qty_uom"]["confirmed"] is True
    assert out["open_status"] == "OPEN"


def test_all_live_paths_fail_unavailable():
    out = retrieve_sam_live(
        notice_id="n1",
        api_fetcher=lambda nid: {"ok": False, "api_failure": SAM_API_AUTH_FAILED, "raw": None},
        public_page_fetcher=lambda nid: {"ok": False, "blocked": PUBLIC_WEB_BLOCKED},
        public_detail_fetcher=lambda nid, key=None: {"ok": False},
        fetch_attachments=False,
    )
    assert out["confirmation"] == LIVE_SOURCE_UNAVAILABLE
    assert out["live_verified"] is False


def test_stale_cache_cannot_satisfy_live_status():
    out = retrieve_sam_live(
        notice_id="n1",
        api_fetcher=lambda nid: {"ok": False, "api_failure": SAM_API_AUTH_FAILED, "raw": None},
        public_page_fetcher=lambda nid: {"ok": False},
        public_detail_fetcher=lambda nid, key=None: {"ok": False},
        stale_cache={
            "open_status": "OPEN",
            "response_deadline": "2026-10-09",
            "quantity": 40,
            "uom": "EA",
            "source_url": "cache://phase_k",
        },
        fetch_attachments=False,
    )
    assert out["confirmation"] == STALE_CACHE_ONLY
    assert out["live_verified"] is False
    assert out["qty_uom"]["confirmed"] is False  # not live-confirmed


def test_source_conflict_prefers_latest_amendment():
    chosen = prefer_live_field(
        [
            {
                "value": "40",
                "source_type": "SAM_OPPORTUNITIES_API",
                "source_url": "api",
            },
            {
                "value": "20",
                "source_type": "LATEST_AMENDMENT",
                "source_url": "amd.pdf",
            },
        ]
    )
    assert chosen["value"] == "20"
    assert chosen["conflict"] is True
    assert chosen["conflict_flag"] == SOURCE_CONFLICT


def test_api_failure_does_not_block_when_fallback_sufficient():
    """Quote-readiness retrieval gate: live_verified True despite SAM_API_AUTH_FAILED."""
    out = retrieve_sam_live(
        notice_id=PIPEFITTER_NOTICE,
        api_fetcher=lambda nid: {"ok": False, "api_failure": SAM_API_AUTH_FAILED, "raw": None},
        public_page_fetcher=lambda nid: {"ok": False, "umbrella_key": "k"},
        public_detail_fetcher=lambda nid, key=None: {
            "ok": True,
            "url": "u",
            "data": {
                "status": {"code": "published"},
                "archived": False,
                "cancelled": False,
                "data2": {
                    "title": "Pipefitter NSN 5180-00-596-1509",
                    "solicitation": {
                        "setAside": "SBA",
                        "deadlines": {"response": "2026-10-09T15:30:00-04:00"},
                    },
                },
                "description": [{"body": "Quantity: 40 EA"}],
            },
        },
        fetch_attachments=False,
    )
    assert out["api_failure"] == SAM_API_AUTH_FAILED
    assert out["live_verified"] is True
    assert out["confirmation"] == LIVE_PUBLIC_WEB_CONFIRMED
    # Business readiness is separate; retrieval gate is cleared
    assert out["qty_uom"]["confirmed"] is True


def test_phase_k_pipefitter_without_api_fixture():
    """Pipefitter NSN path: 401 API + public detail yields live open/deadline/qty."""
    out = retrieve_sam_live(
        notice_id=PIPEFITTER_NOTICE,
        solicitation_number="BA015",
        api_fetcher=lambda nid: {
            "ok": False,
            "api_failure": SAM_API_AUTH_FAILED,
            "error": "HTTP_401",
            "raw": None,
        },
        public_page_fetcher=lambda nid: {
            "ok": False,
            "blocked": PUBLIC_WEB_BLOCKED,
            "umbrella_key": "pub",
        },
        public_detail_fetcher=lambda nid, key=None: {
            "ok": True,
            "url": f"https://sam.gov/api/prod/opps/v2/opportunities/{nid}",
            "data": {
                "status": {"code": "published", "value": "Published"},
                "archived": False,
                "cancelled": False,
                "latest": True,
                "modifiedDate": "2026-09-17T19:33:14.672+00:00",
                "description": [
                    {
                        "body": (
                            "<p>Update 1</p><p>Bid submissions extended to "
                            "October 9, 2026, at 3:30 PM EST.</p>"
                            "<p>Small Business Total Set-Aside. TDP distribution code A. "
                            "NSN: 5180-00-596-1509. Quantity: 40 EA with 100% option.</p>"
                        )
                    }
                ],
                "data2": {
                    "title": "Solicitation-Testing Kits, Toolkit, Pipefitter; NSN: 5180-00-596-1509",
                    "solicitationNumber": "BA015",
                    "version": "2",
                    "solicitation": {
                        "setAside": "SBA",
                        "deadlines": {
                            "response": "2026-10-09T15:30:00-04:00",
                            "responseTz": "America/New_York",
                        },
                    },
                },
            },
        },
        fetch_attachments=False,
    )
    assert out["api_failure"] == SAM_API_AUTH_FAILED
    assert out["live_verified"] is True
    assert out["open_status"] == "OPEN"
    assert "2026-10-09" in str(out["response_deadline"])
    assert out["set_aside"] == "SBA"
    assert out["qty_uom"]["quantity"] == 40.0
    assert out["qty_uom"]["uom"] == "EA"
    assert "5180-00-596-1509" in str(out["title"])


def test_parse_qty_toolkit_defaults_uom_ea():
    q = parse_qty_uom_from_text("Quantity: 40\nPipefitter Supplemental Toolkit (PSTK)")
    assert q["quantity"] == 40.0
    assert q["uom"] == "EA"
    assert q["confirmed"] is True


def test_parse_qty_line_broken_schedule():
    text = (
        "Quantity: \nPipefitter Supplemental \nToolkit (PSTK)\n40\n180 Days\n"
        "0003\nUnexercised Production \nQuantity: Pipefitter \n"
    )
    q = parse_qty_uom_from_text(text)
    assert q["quantity"] == 40.0
    assert q["uom"] == "EA"
    assert q["option_quantity"] == 40.0
    assert q["confirmed"] is True


def test_discovery_degraded_label():
    assert discovery_coverage_after_api_failure(SAM_API_AUTH_FAILED) == DISCOVERY_DEGRADED
