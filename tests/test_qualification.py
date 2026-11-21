from m3.research.qualification import qualify_for_acquisition, default_auto_criteria


def test_services_skip_full():
    q = qualify_for_acquisition({"requirement_class": "services", "solicitation_status": "open"})
    assert q["disposition"] == "SKIP_CONFIRMED_NON_PRODUCT"
    assert q["acquire_full_package"] is False


def test_construction_skip():
    q = qualify_for_acquisition({"requirement_class": "construction"})
    assert not q["acquire_full_package"]


def test_unknown_targeted_not_skip():
    q = qualify_for_acquisition({"requirement_class": "unknown_mixed", "solicitation_status": "open"})
    assert q["disposition"] == "TARGETED_INVESTIGATION"
    assert q["targeted_only"] is True
    assert q["acquire_full_package"] is False


def test_product_eligible():
    q = qualify_for_acquisition({"requirement_class": "product_supply", "solicitation_status": "open"})
    assert q["acquire_full_package"] is True


def test_forecast_skip():
    q = qualify_for_acquisition(
        {"requirement_class": "product_supply", "solicitation_status": "forecast"},
        criteria=default_auto_criteria(),
    )
    assert q["disposition"] == "SKIP_INACTIVE_STATUS"


def test_saved_override():
    q = qualify_for_acquisition(
        {"requirement_class": "services"},
        saved=True,
    )
    assert q["acquire_full_package"] is True


def test_manual_override():
    q = qualify_for_acquisition({"requirement_class": "construction"}, manual_override=True)
    assert q["disposition"] == "MANUAL_OVERRIDE"
