"""PG-unlock deferred recovery handoff — preserve eligibility on re-defer."""

from __future__ import annotations


def test_redefer_preserves_recovery_eligible(tmp_path, monkeypatch):
    monkeypatch.setenv("M3_DATA_ROOT", str(tmp_path))
    from m3_data_root import set_data_root

    set_data_root(tmp_path)
    from buyer_intelligence import full_scale_production_funnel_v1 as fspf

    fspf._defer_opportunity(
        opportunity_id="unlock-a",
        stage="ACCESS_DOCS",
        outcome="PG_UNLOCK_RECOVERY",
        defect_key=None,
        shared_access_key="pg:8407551",
        blocker="AWAITING_PURCHASING_GROUP_REGISTRATION_RESOLVED",
        index=12,
        recovery_eligible=True,
        pg_unlocked="8407551",
    )
    dq = fspf._load_deferred_queue()
    assert dq["pg_unlock_eligible_count"] == 1
    assert (dq["items"] or [])[0]["recovery_eligible"] is True

    # Failed browser attempt re-defers without dropping unlock eligibility.
    fspf._defer_opportunity(
        opportunity_id="unlock-a",
        stage="ACCESS_DOCS",
        outcome="DEFERRED_EXTERNAL",
        defect_key="DETAIL_FETCH_EMPTY",
        shared_access_key="pg:8407551",
        blocker="DETAIL_FETCH_EMPTY",
        index=12,
        # omit recovery_eligible → preserve prior
    )
    dq2 = fspf._load_deferred_queue()
    assert len(dq2["items"]) == 1
    item = dq2["items"][0]
    assert item["opportunity_id"] == "unlock-a"
    assert item["recovery_eligible"] is True
    assert item["pg_unlocked"] == "8407551"
    assert item["shared_access_key"] == "pg:8407551"
    assert item["funnel_outcome"] == "PG_UNLOCK_RECOVERY"
    assert dq2["pg_unlock_eligible_count"] == 1


def test_paid_entitlement_clears_recovery_eligible(tmp_path, monkeypatch):
    monkeypatch.setenv("M3_DATA_ROOT", str(tmp_path))
    from m3_data_root import set_data_root

    set_data_root(tmp_path)
    from buyer_intelligence import full_scale_production_funnel_v1 as fspf

    fspf._defer_opportunity(
        opportunity_id="unlock-b",
        stage="ACCESS_DOCS",
        outcome="PG_UNLOCK_RECOVERY",
        defect_key=None,
        shared_access_key="pg:8407551",
        blocker="AWAITING_PURCHASING_GROUP_REGISTRATION_RESOLVED",
        index=3,
        recovery_eligible=True,
        pg_unlocked="8407551",
    )
    fspf._defer_opportunity(
        opportunity_id="unlock-b",
        stage="ACCESS_DOCS",
        outcome="EXTERNAL_DEPENDENCY",
        defect_key=None,
        shared_access_key="paid:PAID_ENTITLEMENT:x",
        blocker="PAID_ENTITLEMENT:x",
        index=3,
        recovery_eligible=False,
        pg_unlocked=None,
    )
    dq = fspf._load_deferred_queue()
    item = dq["items"][0]
    assert item.get("recovery_eligible") is not True
    assert dq.get("pg_unlock_eligible_count") == 0
