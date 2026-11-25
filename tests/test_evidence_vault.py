"""Evidence vault: originals kept once, versions never overwritten, cleanup only after verified closure."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from m3.research import evidence_vault as ev
from m3.research import nextbid_cache

NOW = datetime(2026, 11, 24, 18, 0, tzinfo=timezone.utc)


def test_original_bytes_survive_restart_and_identical_store_is_one_version(tmp_path):
    html = "<html><h1>Purchase of 53 pagers</h1>ünïcode</html>"
    a = ev.put("bidnet", "bidnet:1", "solicitation_page", html, url="https://x/1", status=200, root=tmp_path)
    b = ev.put("bidnet", "bidnet:1", "solicitation_page", html, url="https://x/1", status=200, root=tmp_path)
    assert a["version"] == b["version"] == 1 and not b["changed"] and a["first_for_record"] and not b["first_for_record"]
    # A fresh reader (new process) sees the exact original bytes
    got = ev.latest("bidnet", "bidnet:1", "solicitation_page", root=tmp_path)
    assert got["body"].decode("utf-8") == html and got["status"] == 200


def test_cache_first_decision_reuses_unchanged_listing(tmp_path):
    assert ev.needs_retrieval("bidnet", "bidnet:2", "solicitation_page", root=tmp_path)["reason"] == "no_retained_body"
    ev.put("bidnet", "bidnet:2", "solicitation_page", "<p>a</p>", meta={"listing_fingerprint": "f1"}, root=tmp_path)
    assert not ev.needs_retrieval("bidnet", "bidnet:2", "solicitation_page", listing_fingerprint="f1", root=tmp_path)["retrieve"]
    assert ev.needs_retrieval("bidnet", "bidnet:2", "solicitation_page", listing_fingerprint="f2", root=tmp_path)["reason"] == "listing_changed"


def test_amendment_adds_version_and_keeps_original(tmp_path):
    v1 = ev.put("opengov", "og:9", "solicitation_page", "<p>due 12/01 qty 10</p>", meta={"deadline_at": "2026-12-01T20:00:00+00:00"}, root=tmp_path)
    v2 = ev.put("opengov", "og:9", "solicitation_page", "<p>Addendum 1: due 12/08 qty 12</p>",
                meta={"deadline_at": "2026-12-08T20:00:00+00:00"}, root=tmp_path)
    assert v2["changed"] and v2["version"] == 2
    assert ev.get_body(v1["sha256"], root=tmp_path) == b"<p>due 12/01 qty 10</p>"
    hist = ev.amendment_history("opengov", "og:9", "solicitation_page", root=tmp_path)
    assert hist[0]["changes"]["deadline_at"]["to"].startswith("2026-12-08")


def _rows():
    old = (NOW - timedelta(days=5)).isoformat()
    return [
        {"source": "s", "source_record_id": "closed", "lifecycle_status": "EXPIRED", "deadline_at": old},
        {"source": "s", "source_record_id": "recent", "lifecycle_status": "EXPIRED", "deadline_at": (NOW - timedelta(hours=10)).isoformat()},
        {"source": "s", "source_record_id": "open", "lifecycle_status": "VERIFIED_OPEN", "deadline_at": (NOW + timedelta(days=3)).isoformat()},
        {"source": "s", "source_record_id": "unknown", "lifecycle_status": "STATUS_UNKNOWN", "deadline_at": None},
        {"source": "s", "source_record_id": "extended", "lifecycle_status": "EXPIRED", "deadline_at": old},
        {"source": "s", "source_record_id": "bid", "lifecycle_status": "EXPIRED", "deadline_at": old},
        {"source": "s", "source_record_id": "queued", "lifecycle_status": "EXPIRED", "deadline_at": old, "owner_queue": "supplier_research_ready"},
    ]


def test_retention_dry_run_only_targets_verified_closed_unprotected(tmp_path):
    for r in _rows():
        ev.put("s", r["source_record_id"], "solicitation_page", f"<p>{r['source_record_id']}</p>", root=tmp_path)
    ev.put("s", "extended", "solicitation_page", "<p>extended</p> addendum",
           meta={"deadline_at": (NOW + timedelta(days=7)).isoformat()}, root=tmp_path)
    plan = ev.retention_plan(_rows(), now=NOW, protected_ids={"bid"}, root=tmp_path)
    assert plan["dry_run"] and [e["source_record_id"] for e in plan["eligible"]] == ["closed"]
    assert plan["kept_by_reason"] == {"within_retention_window": 1, "not_verified_closed": 2, "extended_deadline_in_evidence": 1,
                                      "protected_business_record": 1, "active_owner_queue": 1}
    refused = ev.apply_retention(plan, root=tmp_path)
    assert refused["applied"] is False and ev.latest("s", "closed", "solicitation_page", root=tmp_path)["body"]


def test_retention_apply_keeps_manifest_and_shared_blobs(tmp_path):
    ev.put("s", "closed", "solicitation_page", "<p>shared</p>", root=tmp_path)
    ev.put("s", "open", "solicitation_page", "<p>shared</p>", root=tmp_path)
    plan = ev.retention_plan(_rows()[:3], now=NOW, root=tmp_path)
    res = ev.apply_retention(plan, enabled=True, root=tmp_path)
    assert res == {"applied": True, "records": 1, "blobs_removed": 0}
    man = ev.load_manifest("s", "closed", root=tmp_path)
    assert man["retention"]["reason"] == "verified_closed_past_retention" and man["kinds"]["solicitation_page"][0]["sha256"]
    assert ev.latest("s", "open", "solicitation_page", root=tmp_path)["body"] == b"<p>shared</p>"
    assert ev.latest("s", "closed", "solicitation_page", root=tmp_path)["body"] is None


def test_nextbid_cache_keeps_superseded_paid_response(tmp_path, monkeypatch):
    monkeypatch.setattr(nextbid_cache, "cache_root", lambda: tmp_path)
    nextbid_cache.cache_put("detail", {"v": 1}, "NB1")
    nextbid_cache.cache_put("detail", {"v": 1}, "NB1")
    nextbid_cache.cache_put("detail", {"v": 2}, "NB1")
    assert nextbid_cache.cache_get("detail", "NB1")["payload"] == {"v": 2}
    kept = [json.loads(p.read_text(encoding="utf-8"))["payload"] for p in (tmp_path / "detail" / "versions").glob("*.json")]
    assert kept == [{"v": 1}]
