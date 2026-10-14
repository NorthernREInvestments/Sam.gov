"""Purchasing-group certification radio mapping (Texas/Oklahoma style)."""

from __future__ import annotations


def test_texas_sctrca_hub_radio_answered_no(monkeypatch):
    from bidnet_auth import purchasing_group_registration as pgr

    monkeypatch.setattr(
        pgr,
        "load_verified_company_answers",
        lambda: {
            "ein_present": True,
            "ein": None,
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
            "name": "fields[sctrcaHub]",
            "label": (
                "Is your company a registered M/W/DBE with the South Central TX "
                "Regional Certification Agency (SCTRCA) or HUB?*"
            ),
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
    plan = pgr.prepare_generic_pg_answers(fields, purchasing_group_id="8407551", name="Texas")
    names = {f["name"] for f in plan["fills"]}
    assert "fields[sctrcaHub]" in names
    assert plan["fills"][0]["value"] == "NO"
    # Conditional other-names not required when answered NO
    assert not any(m["name"] == "fields[otherMBENames]" for m in plan["missing_fields"])
    assert plan["can_submit"] is True
