"""Staged inventory: lifecycle, canonical identity / dedup, queue routing and offline guarantees."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from m3.research import staged_inventory as si

NOW = datetime(2026, 11, 23, 18, 0, tzinfo=timezone.utc)


def _rec(**kw):
    base = si._staged(source_record_id=kw.pop("sid", "x:1"), source=kw.pop("source", "free"),
                      source_family=kw.pop("family", "free_source"), last_seen_at=kw.pop("seen", "2026-11-23T10:00:00+00:00"))
    base.update(kw)
    return base


def _life(**kw):
    r = _rec(**kw)
    return si.determine_lifecycle(r, NOW, max_age_hours=72)["lifecycle_status"]


def test_url_identity_keeps_record_id_query_and_drops_tracking():
    a = si._norm_url("https://www.rampla.org/s/opportunity-details?id=006Ql00000ooOyRIAU")
    b = si._norm_url("https://rampla.org/s/opportunity-details?id=006Ql00000ooOyRIAU&utm_source=x")
    assert a == b and "id=006ql00000ooOyriau".lower() in a
    assert a != si._norm_url("https://www.rampla.org/s/opportunity-details?id=OTHER")
    assert si._norm_url("https://www.commbuys.com/bso/external/bidDetail.sda?docId=S1&external=true&parentUrl=close") == \
        "commbuys.com/bso/external/biddetail.sda?docid=s1"


def test_partial_bidnet_snapshot_never_counts_as_complete(tmp_path):
    row = {"provider_record_id": "444000000001", "title": "Fuel", "official_url": "https://www.bidnetdirect.com/x/444000000001",
           "deadline_raw": "12/16/2026", "retrieved_at": "2026-11-24T14:00:00+00:00"}
    (tmp_path / "snapshot.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    for cov, complete in (({"enumeration_complete": True, "coverage_ratio": 0.8397}, False),
                          ({"enumeration_complete": True, "coverage_ratio": 0.995}, True),
                          ({"enumeration_complete": False, "coverage_ratio": 1.0}, False)):
        (tmp_path / "coverage.json").write_text(json.dumps(cov), encoding="utf-8")
        assert si.load_bidnet_snapshot(tmp_path)["complete"] is complete


def test_official_page_deadline_and_feed_placeholders():
    pages = {  # text as retained from the official pages
        "Bid Status:&nbsp; Open Bid Closing Date:&nbsp; Wed Dec 23, 2026 3:30:00 PM (CST) Question Deadline:&nbsp; Tue Dec 15, 2026":
            ("MN", "2026-12-23T21:30:00+00:00"),
        "Solicitation Start Date: 10/15/26 Solicitation Due Date: 12/17/26 Solicitation Due Time: 1:30 PM Solicitation Opening":
            ("PA", "2026-12-17T18:30:00+00:00"),
        "Release Date September 23, 2026 Submission Deadline January 19, 2027, 11:59 pm Questions Deadline November 20, 2026, 5:00 pm":
            ("CA", "2027-01-20T07:59:00+00:00"),
    }
    for text, (state, want) in pages.items():
        raw = si.page_stated_deadline(text)
        assert si.parse_deadline(raw, tz_name=None, state=state)["deadline_at"].isoformat() == want, (text, raw)
    assert si.page_stated_deadline("Questions Deadline November 20, 2026, 5:00 pm") is None
    feed = si.parse_deadline("2026-12-03T12:00:00+00:00", tz_name=None, state="MN")
    assert feed["deadline_basis"].endswith("_end_of_day") and feed["deadline_at"].isoformat() == "2026-12-04T05:59:59+00:00"
    assert si.parse_deadline("2026-12-03T12:00:00Z", tz_name="UTC", state="CA")["deadline_at"].isoformat() == "2026-12-04T07:59:59+00:00"
    assert si.parse_deadline("2026-12-03T17:00:00+00:00", tz_name=None, state="MN")["deadline_basis"] == "explicit_offset"


def test_bidnet_abstract_deadline_overrides_listing_date(tmp_path):
    row = {"provider_record_id": "2723951867", "title": "39--FAIRLEAD,ROLLER", "location": "Pennsylvania",
           "official_url": "https://www.bidnetdirect.com/pennsylvania/x/2723951867", "deadline_raw": "11/26/2026",
           "retrieved_at": "2026-11-24T14:00:00+00:00"}
    (tmp_path / "snapshot.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    (tmp_path / "abstracts").mkdir()
    (tmp_path / "abstracts" / "abstracts.jsonl").write_text(json.dumps(
        {"provider_record_id": "2723951867", "deadline_raw": "11/25/2026 07:00 PM EST", "deadline_timezone": "EST",
         "retrieved_at": "2026-11-25T01:06:50+00:00"}) + "\n", encoding="utf-8")
    rec = si.load_bidnet_snapshot(tmp_path)["records"][0]
    assert rec["deadline_raw"] == "11/25/2026 07:00 PM EST"
    d = si.parse_deadline(rec["deadline_raw"], tz_name=rec.get("deadline_tz_hint"), state="PA")["deadline_at"]
    assert d.isoformat().startswith("2026-11-26T00:00:00")


def test_lifecycle_states():
    assert _life(deadline_raw="2026-12-10T17:00:00-05:00", declared_status="open") == "VERIFIED_OPEN"
    assert _life(deadline_raw="2026-12-10T17:00:00-05:00", declared_status="open",
                 seen="2026-10-12T19:00:00+00:00") == "LIKELY_OPEN"
    assert _life(deadline_raw="2026-11-01") == "EXPIRED"
    assert _life(deadline_raw=None) == "STATUS_UNKNOWN"
    assert _life(deadline_raw="2099-12-31") == "STATUS_UNKNOWN"
    assert _life(deadline_raw="2026-12-10", declared_status="closed") == "STATUS_UNKNOWN"
    assert _life(deadline_raw="2026-12-10", title="CANCELLED - Road Salt") == "CANCELLED"


def test_date_only_deadline_runs_to_end_of_day_in_state_timezone():
    d = si.parse_deadline("2026-11-23", tz_name=None, state="CA")
    assert d["deadline_at"] > NOW
    assert d["deadline_basis"].startswith("state_timezone")


def _cluster(records):
    for r in records:
        r["identity_keys"] = si.identity_keys(r)
    groups = si.cluster_duplicates(records)
    return sorted(len(g) for g in groups.values())


def test_distinct_dibbs_rfqs_with_same_nomenclature_do_not_merge():
    a = _rec(sid="nb:1", family="nextbid", source="dibbs", provider_record_id="NB1", solicitation_number="SPE4A627T075D",
             title="SCREW, MACHINE", buyer="Defense Logistics Agency", deadline_raw="2026-12-01")
    b = _rec(sid="nb:2", family="nextbid", source="dibbs", provider_record_id="NB2", solicitation_number="SPE4A627T099X",
             title="SCREW, MACHINE", buyer="Defense Logistics Agency", deadline_raw="2026-12-01")
    assert _cluster([a, b]) == [1, 1]


def test_shared_dataset_url_is_not_identity():
    rows = [_rec(sid=f"free:{i}", title=f"Supply and Delivery of Item {name}", official_url="https://winnipeg.ca/matmgt/foldercontents.asp?x=1")
            for i, name in enumerate(["Paper Rolls", "Library Materials", "Server Hardware", "Fitness Equipment", "Chain Link Fencing"])]
    assert _cluster(rows) == [1, 1, 1, 1, 1]


def test_same_solicitation_across_sources_merges():
    a = _rec(sid="legacy:1", family="legacy_other", title="85727B0020-2700009-VANS, PASSENGER - NYPD",
             solicitation_number="85727B0020", buyer="Citywide Administrative Services")
    b = _rec(sid="free:1", title="Correction: 85727B0020-2700009-VANS, PASSENGER - NYPD",
             solicitation_number="85727B0020", buyer="Citywide Administrative Services")
    assert _cluster([a, b]) == [2]


def test_distinct_bidnet_events_do_not_merge_on_title_fallback():
    rows = [_rec(sid=f"bidnet:{i}", family="bidnet_legacy", source="bidnet", provider_record_id=str(444170798220 + i),
                 title="Molalla Fire District Station 81 Tenant Improvement", state="OR", deadline_raw="2026-10-02")
            for i in range(3)]
    assert _cluster(rows) == [1, 1, 1]


def test_junk_titles_are_non_solicitations():
    for t in ["https://publicdata.usac.org/RHC/doc.pdf", "${escapeHTML(item.link)}",
              "Effective Date: 08/01/2026; Provisional Status: Yes; Title Code 10234"]:
        assert si._JUNK_TITLE.search(t)
    assert not si._JUNK_TITLE.search("Supply of Road Salt")


def test_queue_routing_never_promotes_title_only_records():
    rec = _rec(title="Nitrile Gloves", deadline_raw="2026-12-10", declared_status="open")
    rec.update(si.determine_lifecycle(rec, NOW, max_age_hours=72))
    ev = si.evaluate_record(rec)
    q, reason = si.assign_owner_queue(rec, ev)
    assert q == "document_verification", reason
    assert q in si.OWNER_QUEUES


def test_offline_run_dispositions_every_record(tmp_path, monkeypatch):
    monkeypatch.setattr("m3.research.nextbid_controller.acquisition_locked", lambda: True)
    rows = [
        _rec(sid="free:a", title="Road Salt 5000 tons", deadline_raw="2026-12-10", declared_status="open", solicitation_number="RS-2026-001"),
        _rec(sid="free:b", title="Road Salt 5000 tons", deadline_raw="2026-12-10", declared_status="open", solicitation_number="RS-2026-001"),
        _rec(sid="free:c", title="Janitorial Services", deadline_raw="2026-12-10"),
        _rec(sid="free:d", title="Old Bid", deadline_raw="2025-01-01"),
        _rec(sid="free:e", title="Fire Hose", deadline_raw=None),
    ]
    m = si.run_staged_inventory(out_root=tmp_path, now=NOW, loaders={"t": lambda: {"available": True, "records": rows}}, resume=False)
    assert m["all_records_dispositioned"]
    assert m["external_calls"] == 0 and m["database_writes"] == 0
    assert m["stage_counts"]["duplicates_marked"] == 1
    inv = [json.loads(line) for line in (tmp_path / "inventory.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(inv) == 5 and all(r["owner_queue"] in si.OWNER_QUEUES for r in inv)
    assert next(r for r in inv if r["source_record_id"] == "free:e")["owner_queue"] == "status_verification"
    fresh = m["stage_counts"]["evaluation_cache"]
    assert fresh["hits"] == 0 and fresh["evaluated_fresh"] == m["stage_counts"]["evaluated"]
    again = si.run_staged_inventory(out_root=tmp_path, now=NOW, loaders={"t": lambda: {"available": True, "records": rows}}, resume=False)
    assert again["stage_counts"]["evaluation_cache"]["hits"] == m["stage_counts"]["evaluated"]
    assert again["queue_counts"] == m["queue_counts"]
    nextday = si.run_staged_inventory(out_root=tmp_path, now=NOW.replace(day=24), resume=False,
                                      loaders={"t": lambda: {"available": True, "records": rows}})
    assert nextday["stage_counts"]["evaluation_cache"]["hits"] == 0


def test_run_refuses_when_acquisition_unlocked(tmp_path, monkeypatch):
    monkeypatch.setattr("m3.research.nextbid_controller.acquisition_locked", lambda: False)
    with pytest.raises(AssertionError):
        si.run_staged_inventory(out_root=tmp_path, now=NOW, loaders={}, resume=False)
