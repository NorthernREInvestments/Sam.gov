"""Regression: live SAM set-aside phrasing must match Total SB filter."""

from __future__ import annotations

from sam_client import _set_aside_matches


def test_sam_small_business_set_aside_total_matches():
    assert _set_aside_matches({"typeOfSetAsideDescription": "Small Business Set Aside - Total"}) is True
    assert _set_aside_matches({"typeOfSetAsideDescription": "Total Small Business"}) is True
    assert _set_aside_matches({"typeOfSetAsideDescription": "Total Small Business Set-Aside"}) is True
    assert _set_aside_matches({"typeOfSetAsideDescription": "Small Business Set-Aside"}) is True


def test_sam_unrestricted_and_socioeconomic_rejected_by_filter():
    assert _set_aside_matches({"typeOfSetAsideDescription": "No Set aside used"}) is False
    assert _set_aside_matches({"typeOfSetAsideDescription": None}) is False
    assert _set_aside_matches({}) is False
    assert _set_aside_matches({"typeOfSetAsideDescription": "Women-Owned Small Business"}) is False
    assert _set_aside_matches({"typeOfSetAsideDescription": "SDVOSB"}) is False
    assert _set_aside_matches({"typeOfSetAsideDescription": "8(a) Set-Aside"}) is False
