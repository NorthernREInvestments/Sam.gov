"""Permanent ordered solicitation analysis — body first, reject non-product, no guess."""

from m3.research.reprocess import run_ordered_solicitation_analysis


def test_rejects_service_from_body():
    body = (
        "Solicitation No. SVC-2026-01\n"
        "The City seeks janitorial maintenance services for all facilities.\n"
        "Bid due: December 1, 2026\n"
    )
    result = run_ordered_solicitation_analysis(
        {"title": "Janitorial Maintenance Services", "verified_individual_solicitation": True},
        body_text=body,
        allow_network=False,
    )
    assert result["rejected"] is True
    assert result["promoted"] is False
    assert result["disposition"] == "confirmed_disqualification"


def test_extracts_product_fields_from_body_without_guessing():
    body = (
        "Solicitation Number: IFB-7788\n"
        "Purchase of office chairs\n"
        "Item description: Ergonomic task chairs, black\n"
        "Quantity: 25 each\n"
        "Bid due: January 15, 2027\n"
        "Buyer: City Purchasing Department\n"
        "Delivery to: City Warehouse, 100 Main St\n"
        "Submit bids electronically via the portal\n"
        "Full and open competition\n"
    )
    result = run_ordered_solicitation_analysis(
        {
            "title": "Purchase of Office Chairs",
            "verified_individual_solicitation": True,
            "solicitation_status": "open",
        },
        body_text=body,
        allow_network=False,
    )
    item = result["item"]
    assert result["rejected"] is False
    assert item.get("quantity") == 25 or item.get("quantity_semantic")
    assert item.get("deadline_at") or item.get("deadline_extracted")
    assert item.get("buyer_name") or item.get("buyer_extracted")
    # Incomplete still never invents promotion without firm Stage C + bid gates
    if result["promoted"]:
        assert result["complete_verified"] is True
        assert result["bid"].get("bid_eligible") is True
    else:
        assert "unknown" not in str(item.get("product_description") or "").lower()


def test_network_blocked_without_authorization():
    try:
        run_ordered_solicitation_analysis(
            {"title": "x"},
            body_text="Purchase of widgets quantity 10 each bid due January 1 2027",
            allow_network=True,
        )
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "authorization" in str(exc).lower()
