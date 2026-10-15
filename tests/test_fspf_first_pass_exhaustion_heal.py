"""Regression: false first-pass exhaustion must not strand the universe."""

from __future__ import annotations

from buyer_intelligence import full_scale_production_funnel_v1 as fspf


class _FakeStore:
    def __init__(self, items: dict[str, dict]):
        self._items = items
        self.saved = None

    def load_item(self, job_id: str, oid: str):
        return self._items.get(oid)

    def save_job(self, job):
        self.saved = job


def test_heal_restores_first_pass_when_unattempted_remain():
    rows = [{"opportunity_id": f"oid-{i}"} for i in range(100)]
    items = {f"oid-{i}": {"RESULT": "TIMEOUT"} for i in range(10)}
    store = _FakeStore(items)
    job = {
        "JOB_ID": "job-1",
        "RESUME_CURSOR": 100,
        "META": {"pass_mode": "DEFERRED_RECOVERY"},
    }
    job2, mode, first_pass, cursor = fspf._heal_false_first_pass_exhaustion(store, job, rows)
    assert mode == "FIRST_PASS"
    assert first_pass is True
    assert cursor == 10
    assert job2["RESUME_CURSOR"] == 10


def test_collect_skips_attempted_and_finds_later_work():
    rows = [{"opportunity_id": f"oid-{i}"} for i in range(50)]
    items = {f"oid-{i}": {"RESULT": "FETCH_FAILED"} for i in range(30)}
    store = _FakeStore(items)
    pending, reg, scan_end = fspf._collect_first_pass_pending(
        store,
        "job-1",
        rows,
        cursor=0,
        slice_size=5,
        registered_pgs=set(),
    )
    assert len(pending) == 5
    assert pending[0][0] == 30
    assert scan_end > 30


def test_heal_noop_when_truly_exhausted():
    rows = [{"opportunity_id": f"oid-{i}"} for i in range(5)]
    items = {f"oid-{i}": {"RESULT": "TIMEOUT"} for i in range(5)}
    store = _FakeStore(items)
    job = {
        "JOB_ID": "job-1",
        "RESUME_CURSOR": 5,
        "META": {"pass_mode": "DEFERRED_RECOVERY"},
    }
    job2, mode, first_pass, cursor = fspf._heal_false_first_pass_exhaustion(store, job, rows)
    assert mode == "DEFERRED_RECOVERY"
    assert first_pass is False
    assert cursor == 5
    assert job2["RESUME_CURSOR"] == 5
