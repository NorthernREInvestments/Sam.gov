"""Owner queue mapping — Stage C is commercial evaluation, not bid-ready."""

from m3.research.owner_queues import map_pipeline_to_queue, map_to_primary_disposition


def test_stage_c_maps_to_commercial_not_bid_ready():
    pipe = {
        "stage": "VERIFIED_M3_PROCUREMENT_FIT",
        "quantity_defined": True,
        "excluded": False,
        "unknown_flags": [],
        "product_opportunity": True,
    }
    assert map_pipeline_to_queue(pipe) == "qualified_commercial_evaluation"


def test_restricted_package_queue():
    pipe = {"stage": "VERIFIED_PRODUCT_OPPORTUNITY", "excluded": False, "unknown_flags": []}
    assert map_pipeline_to_queue(pipe, package_status="ACCESS_RESTRICTED") == "restricted_document"


def test_confirmed_services_title_dq():
    pipe = {
        "stage": None,
        "excluded": True,
        "reasons": ["class:services_title_confirmed"],
        "unknown_flags": [],
    }
    assert map_pipeline_to_queue(pipe) == "confirmed_disqualification"


def test_unknown_mixed_is_potential_not_dq():
    pipe = {
        "stage": "VERIFIED_PRODUCT_OPPORTUNITY",
        "excluded": False,
        "product_opportunity": True,
        "quantity_defined": False,
        "unknown_flags": ["potential_product_insufficient_classification", "requirement_class_unknown"],
        "reasons": [],
    }
    item = {"requirement_class": "unknown_mixed", "solicitation_status": "open"}
    assert map_to_primary_disposition(item, pipe) == "potential_product_classification_needed"


def test_missing_qty_is_info_needed_not_dq():
    pipe = {
        "stage": "VERIFIED_PRODUCT_OPPORTUNITY",
        "excluded": False,
        "product_opportunity": True,
        "quantity_defined": False,
        "unknown_flags": [],
        "reasons": [],
    }
    item = {"requirement_class": "product_supply", "solicitation_status": "open"}
    assert map_to_primary_disposition(item, pipe) == "verified_product_information_needed"
