"""Free staged document acquisition — fake fetchers, no network."""
from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone

import docx

from m3.research import nextbid_free_doc_recovery as fd

NOW = datetime(2026, 11, 24, tzinfo=timezone.utc)


def _docx_bytes(text: str) -> bytes:
    d = docx.Document()
    for line in text.split("\n"):
        d.add_paragraph(line)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _rec(sid, url, days=10, presence="EXPLICIT_PRODUCT", **kw):
    return {"staged_id": sid, "official_url": url, "deadline_at": (NOW + timedelta(days=days)).isoformat(),
            "classification": {"product_presence": presence, "evidence_completeness": "SUMMARY"}, **kw}


def test_target_selection_filters_and_orders():
    rows = [
        _rec("a", "https://city.gov/bid/1", days=20, presence="LIKELY_PRODUCT"),
        _rec("b", "https://city.gov/bid/2", days=9),
        _rec("c", "https://sam.gov/opp/1", days=9),
        _rec("d", "https://city.gov/bid/3", days=1),
        _rec("e", "https://city.gov/bid/4", days=9, federal_policy_hold="FEDERAL_PARKED"),
        _rec("f", "https://city.gov/bid/5", days=9, duplicate_of="x"),
        {**_rec("g", "https://city.gov/bid/6"), "classification": {"product_presence": "EXPLICIT_PRODUCT", "evidence_completeness": "DOCUMENT"}},
    ]
    assert [r["staged_id"] for r in fd.select_staged_document_targets(rows, now=NOW)] == ["b", "a"]


def test_recover_extracts_docx_respects_robots_and_resumes(tmp_path):
    spec = "INVITATION FOR BID\nItem 1: Fire hose, 1.75 in x 50 ft, Qty 40 EA\nItem 2: Nozzle, Qty 10 EA\n" * 3
    pages = {
        "https://city.gov/robots.txt": (200, "User-agent: *\nDisallow: /private/\n"),
        "https://city.gov/bid/1": (200, '<html><h1>Invitation for Bid: Fire Hose</h1><p>Submit bids by Dec 10.</p>'
                                        '<a href="/files/spec.docx">Specifications</a><a href="/private/x.pdf">x</a></html>'),
        "https://other.gov/robots.txt": (404, ""),
        "https://other.gov/p/9": (403, "denied"),
    }
    calls = []

    def ft(u):
        calls.append(u)
        s, t = pages.get(u, (404, ""))
        return s, u, t

    def fb(u):
        calls.append(u)
        if u.endswith("spec.docx"):
            return 200, u, _docx_bytes(spec), "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        return 404, u, b"", ""

    targets = [_rec("s1", "https://city.gov/bid/1", source="city", source_record_id="rec:s1", title="Fire Hose"),
               _rec("s2", "https://other.gov/p/9")]
    vault = tmp_path / "vault"
    res = fd.recover_staged_documents(targets, out_root=tmp_path, fetch_text_fn=ft, fetch_bytes_fn=fb, sleep=lambda s: None,
                                      vault=vault)
    from m3.research import evidence_vault as ev
    man = ev.load_manifest("city", "rec:s1", root=vault)
    assert ev.latest("city", "rec:s1", "solicitation_page", root=vault)["body"].decode() == pages["https://city.gov/bid/1"][1]
    att_kinds = [k for k in man["kinds"] if k.startswith("attachment:")]
    assert len(att_kinds) == 1 and ev.latest("city", "rec:s1", att_kinds[0], root=vault)["body"] == _docx_bytes(spec)
    ck = json.loads((tmp_path / "doc_checkpoint.json").read_text())
    assert res["stats"].get("with_documents") == 1 and res["stats"].get("restricted") == 1, ck
    docs = ck["done"]["s1"]["documents"]
    assert any(d["status"] == "robots_disallow" for d in docs)
    assert "https://city.gov/private/x.pdf" not in calls
    texts = [json.loads(l) for l in (tmp_path / "doc_text.jsonl").read_text().splitlines()]
    assert any(t["kind"] == "attachment" and "Fire hose" in t["text"] for t in texts)
    assert list((tmp_path / "raw").glob("*.docx"))
    n = len(calls)
    res2 = fd.recover_staged_documents(targets, out_root=tmp_path, fetch_text_fn=ft, fetch_bytes_fn=fb, sleep=lambda s: None,
                                       vault=vault)
    assert res2["stats"]["skipped_checkpoint"] == 2 and len(calls) == n


def test_excluded_hosts_never_fetched_as_attachments(tmp_path):
    pages = {"https://city.gov/robots.txt": (404, ""),
             "https://city.gov/bid/1": (200, '<a href="https://api.sam.gov/x.pdf">sam</a><p>Invitation for bid supply</p>')}
    calls = []

    def ft(u):
        calls.append(u)
        s, t = pages.get(u, (404, ""))
        return s, u, t

    fd.recover_staged_documents([_rec("s1", "https://city.gov/bid/1")], out_root=tmp_path, fetch_text_fn=ft,
                                fetch_bytes_fn=lambda u: calls.append(u) or (404, u, b"", ""), sleep=lambda s: None,
                                vault=tmp_path / "vault")
    assert not any("sam.gov" in c for c in calls)
