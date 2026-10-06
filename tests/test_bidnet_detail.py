"""BidNet authenticated detail failure classification."""

from __future__ import annotations

from bidnet_discovery.detail import (
    AUTH_REQUIRED,
    NOT_FOUND_404,
    SESSION_LOST,
    SUPPLIER_REGISTRATION_REDIRECT,
    classify_detail_failure,
)


def test_classify_registration_redirect():
    assert (
        classify_detail_failure(
            final_url="https://www.bidnetdirect.com/public/supplier-registration",
            html="<title>Supplier Registration | BidNet Direct</title>",
        )
        == SUPPLIER_REGISTRATION_REDIRECT
    )


def test_classify_404():
    assert (
        classify_detail_failure(final_url="https://x/y", html="<h1>Page Not Found</h1> 404")
        == NOT_FOUND_404
    )


def test_classify_session_lost():
    assert (
        classify_detail_failure(
            final_url="https://www.bidnetdirect.com/public/authentication/login",
            html="<input type='password'>Please log in",
        )
        == SESSION_LOST
    )


def test_classify_auth_required_member_only():
    assert (
        classify_detail_failure(
            final_url="https://www.bidnetdirect.com/private/solicitations/1/abstract",
            html="<div class='member-only-info'>Registered members only</div>",
        )
        == AUTH_REQUIRED
    )


def test_ok_not_misclassified():
    html = "<div class='mets-field-body'>Issuing Organization</div><div>City of Austin</div>"
    # No hard failure markers — OTHER means "check parser", not a redirect class
    assert classify_detail_failure(final_url="https://x/abstract", html=html) == "OTHER"
