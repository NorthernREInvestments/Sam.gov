from m3.research.opportunity_ui_ready import opportunity_ui_ready, solicitation_ok


def test_unknown_closes_is_fail():
    ok, reasons = opportunity_ui_ready(
        {
            "title": "Firewall Appliance and Support",
            "solicitation_number": "RFP 27-109",
            "official_url": "https://example.com/rfp",
            "deadline_at": None,
        }
    )
    assert ok is False
    assert "unknown_closes" in reasons


def test_ready_row_passes():
    ok, reasons = opportunity_ui_ready(
        {
            "title": "Water Softener Salt",
            "solicitation_number": "127641 OR",
            "official_url": "https://example.com/itb",
            "deadline_at": "2027-01-05T12:00:00+00:00",
        }
    )
    assert ok is True
    assert reasons == []


def test_providence_title_fragment_sol_fails():
    assert solicitation_ok("interested suppliers of snow and ice control 26-") is False
    assert solicitation_ok("MT-54361") is True
