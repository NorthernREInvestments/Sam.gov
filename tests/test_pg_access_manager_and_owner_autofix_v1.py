"""PG access manager + auto-fixable owner blocker regression."""

from __future__ import annotations


def test_other_mbe_names_not_owner_required_when_answered_no(monkeypatch):
    from bidnet_auth import purchasing_group_registration as pgr

    monkeypatch.setattr(
        pgr,
        "load_verified_company_answers",
        lambda: {
            "ein_present": True,
            "ein_value": "12-3456789",
            "certified_mbe": "NO",
            "other_mbe_dbe": "NO",
            "mbe_verified_held": False,
            "dbe_verified_held": False,
            "legal_name": "Test Co",
            "held_certifications": [],
        },
    )
    fields = [
        {
            "name": "fields[certifiedMBE]",
            "label": "MBE?",
            "type": "radio",
            "required": True,
            "has_value": False,
        },
        {
            "name": "fields[otherMBENames]",
            "label": "Other certification names:*",
            "type": "text",
            "required": True,
            "has_value": False,
        },
    ]
    plan = pgr.prepare_generic_pg_answers(
        fields, purchasing_group_id="8409951", name="Colorado"
    )
    assert plan["can_submit"] is True
    assert plan["owner_required"] is False
    assert not any(m["name"] == "fields[otherMBENames]" for m in plan["missing_fields"])
    assert pgr.missing_fields_are_auto_fixable(
        [{"name": "fields[otherMBENames]", "label": "Other certification names:*"}]
    )


def test_w9_upload_remains_owner_required(monkeypatch):
    from bidnet_auth import purchasing_group_registration as pgr

    monkeypatch.setattr(
        pgr,
        "load_verified_company_answers",
        lambda: {
            "ein_present": True,
            "ein_value": "12-3456789",
            "certified_mbe": "NO",
            "other_mbe_dbe": "NO",
            "mbe_verified_held": False,
            "dbe_verified_held": False,
            "legal_name": "Test Co",
            "held_certifications": [],
        },
    )
    fields = [
        {
            "name": "files[]",
            "label": "W9 Form - please upload your W9 Internal Revenue Service*",
            "type": "file",
            "required": True,
            "has_value": False,
        },
    ]
    plan = pgr.prepare_generic_pg_answers(
        fields, purchasing_group_id="8405151", name="New Jersey"
    )
    assert plan["owner_required"] is True
    assert plan["can_submit"] is False
    assert any("document upload" in r for r in plan["owner_reasons"])
    assert not pgr.missing_fields_are_auto_fixable(plan["missing_fields"])


def test_strip_stale_accessed_element():
    from bidnet_auth.purchasing_group_registration import strip_stale_accessed_element

    dirty = (
        "https://www.bidnetdirect.com/private/supplier/9785456456/purchasing-groups/"
        "8407551/group-form/edit?accessedElementId=9773582606&originalAction=SOLICITATION_OPEN"
    )
    clean = strip_stale_accessed_element(dirty)
    assert clean is not None
    assert "accessedElementId" not in clean
    assert clean.endswith("/group-form/edit")


def test_clear_auto_fixable_owner_blockers(tmp_path, monkeypatch):
    monkeypatch.setenv("M3_DATA_ROOT", str(tmp_path))
    from m3_data_root import set_data_root

    set_data_root(tmp_path)
    from bidnet_auth.purchasing_group_tracker import (
        STATUS_OWNER_APPROVAL_REQUIRED,
        STATUS_PAID_REQUIRED,
        clear_auto_fixable_owner_blockers,
        get_group,
        upsert_group,
    )

    upsert_group(
        "8409951",
        name="Colorado",
        status=STATUS_OWNER_APPROVAL_REQUIRED,
        missing_fields=[
            {"name": "fields[otherMBENames]", "label": "Other certification names:*"}
        ],
        owner_blocker_reason="unmapped required fields",
        relevant=True,
    )
    upsert_group(
        "8405151",
        name="New Jersey",
        status=STATUS_OWNER_APPROVAL_REQUIRED,
        missing_fields=[
            {
                "name": "files[]",
                "label": "W9 Form - please upload your W9 Internal Revenue Service*",
            }
        ],
        owner_blocker_reason="document upload requires owner",
        relevant=True,
    )
    upsert_group(
        "999",
        name="Paid",
        status=STATUS_PAID_REQUIRED,
        owner_blocker_reason="fee",
        relevant=True,
    )
    out = clear_auto_fixable_owner_blockers()
    assert "8409951" in out["cleared"]
    assert get_group("8409951")["status"] == "INCOMPLETE"
    assert get_group("8405151")["status"] == STATUS_OWNER_APPROVAL_REQUIRED
    assert any(k["purchasing_group_id"] == "8405151" for k in out["kept_owner_required"])


def test_release_does_not_reset_resume_cursor(tmp_path, monkeypatch):
    monkeypatch.setenv("M3_DATA_ROOT", str(tmp_path))
    from m3_data_root import set_data_root

    set_data_root(tmp_path)
    from m3_jobs.store import JobStore
    from bidnet_auth.purchasing_group_tracker import STATUS_REGISTERED, upsert_group
    from buyer_intelligence import full_scale_production_funnel_v1 as fspf

    upsert_group("88020151", status=STATUS_REGISTERED, relevant=True)
    store = JobStore(fspf.FAMILY)
    job = store.create_job(total=2, opportunity_ids=["a", "b"], meta={"build": fspf.BUILD})
    jid = job["JOB_ID"]
    job["RESUME_CURSOR"] = 42
    store.save_job(job)
    store.write_item_checkpoint(
        jid,
        {
            "OPPORTUNITY_ID": "a",
            "opportunity_id": "a",
            "RESULT": "DEFERRED_EXTERNAL",
            "terminal_blocker": "AWAITING_PURCHASING_GROUP_REGISTRATION",
            "purchasing_group_id": "88020151",
            "shared_access_key": "pg:88020151",
            "stage_reached": "ACCESS_DOCS",
        },
    )
    out = fspf.release_pg_blocked_for_registered_groups(jid)
    assert out["released"] >= 1
    job2 = store.load_job(jid)
    assert int(job2.get("RESUME_CURSOR") or 0) == 42
    item = store.load_item(jid, "a") or {}
    assert item.get("RESULT") == "PENDING_RETRY"
    assert item.get("PG_UNLOCK_ELIGIBLE") is True
