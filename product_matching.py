"""Product matching — EXACT / COMPLIANT_EQUAL / POSSIBLE / NONCOMPLIANT / UNKNOWN.

AI similarity alone cannot establish compliance.
"""

from __future__ import annotations

from typing import Any

from data_integrity import STATUS_VERIFIED

MATCH_EXACT = "EXACT_MATCH"
MATCH_COMPLIANT_EQUAL = "COMPLIANT_EQUAL"
MATCH_POSSIBLE = "POSSIBLE_MATCH"
MATCH_NONCOMPLIANT = "NONCOMPLIANT"
MATCH_UNKNOWN = "UNKNOWN"


def _norm(v: Any) -> str:
    return str(v or "").strip().upper()


def _fact_value(req: dict[str, Any] | None) -> Any:
    if not isinstance(req, dict):
        return None
    if str(req.get("status") or "").upper() != STATUS_VERIFIED:
        return None
    return req.get("value")


def match_product_to_requirements(
    *,
    product: dict[str, Any],
    product_requirements: dict[str, Any],
) -> dict[str, Any]:
    """
    Classify candidate product against solicitation requirements.

    product keys: manufacturer, brand, model, part_number, specifications, country_of_origin
    """
    reasons: list[str] = []
    req_model = _fact_value(product_requirements.get("part_model_number"))
    req_brand = _fact_value(product_requirements.get("brand"))
    brand_only = product_requirements.get("brand_name_only") or {}
    brand_only_true = brand_only.get("value") is True and str(brand_only.get("status")) == STATUS_VERIFIED
    approved_equal = product_requirements.get("approved_equal_language") or {}
    equal_ok = approved_equal.get("value") is True and str(approved_equal.get("status")) == STATUS_VERIFIED
    auth_req = product_requirements.get("manufacturer_authorization_requirement") or {}
    auth_needed = auth_req.get("value") in (True, "required", "REQUIRED") or (
        isinstance(auth_req.get("value"), str) and "authoriz" in str(auth_req.get("value")).lower()
    )
    auth_status = str(auth_req.get("status") or "UNKNOWN").upper()

    p_model = _norm(product.get("model") or product.get("part_number"))
    p_brand = _norm(product.get("brand") or product.get("manufacturer"))
    r_model = _norm(req_model)
    r_brand = _norm(req_brand)

    # Authorization gate — unresolved blocks bid-ready classes
    auth_gate = "PASS"
    if auth_needed:
        if auth_status != STATUS_VERIFIED:
            auth_gate = "UNRESOLVED"
            reasons.append("manufacturer_authorization_unresolved")
        elif product.get("authorized_dealer") is True:
            auth_gate = "PASS"
            reasons.append("authorized_dealer_verified")
        elif product.get("authorized_dealer") is False:
            auth_gate = "FAIL"
            reasons.append("not_authorized_dealer")
        else:
            auth_gate = "UNRESOLVED"
            reasons.append("authorization_unknown_for_candidate")

    if auth_gate == "FAIL":
        return {
            "match_class": MATCH_NONCOMPLIANT,
            "bid_ready": False,
            "reasons": reasons,
            "authorization_gate": auth_gate,
        }

    if not r_model and not r_brand:
        return {
            "match_class": MATCH_UNKNOWN,
            "bid_ready": False,
            "reasons": reasons + ["requirements_identity_unknown"],
            "authorization_gate": auth_gate,
        }

    if not p_model and not p_brand:
        return {
            "match_class": MATCH_UNKNOWN,
            "bid_ready": False,
            "reasons": reasons + ["candidate_identity_unknown"],
            "authorization_gate": auth_gate,
        }

    exact_model = bool(r_model and p_model and r_model == p_model)
    brand_match = bool(r_brand and p_brand and (r_brand in p_brand or p_brand in r_brand))

    if brand_only_true:
        if not brand_match:
            return {
                "match_class": MATCH_NONCOMPLIANT,
                "bid_ready": False,
                "reasons": reasons + ["brand_name_only_mismatch"],
                "authorization_gate": auth_gate,
            }
        if exact_model and auth_gate == "PASS":
            return {
                "match_class": MATCH_EXACT,
                "bid_ready": True,
                "reasons": reasons + ["exact_model_brand_name_only"],
                "authorization_gate": auth_gate,
            }
        return {
            "match_class": MATCH_POSSIBLE if brand_match else MATCH_NONCOMPLIANT,
            "bid_ready": False,
            "reasons": reasons + ["brand_only_incomplete_model"],
            "authorization_gate": auth_gate,
        }

    if exact_model and (not r_brand or brand_match) and auth_gate == "PASS":
        return {
            "match_class": MATCH_EXACT,
            "bid_ready": True,
            "reasons": reasons + ["exact_model_verified"],
            "authorization_gate": auth_gate,
        }

    if equal_ok and brand_match and product.get("salient_characteristics_verified") is True and auth_gate == "PASS":
        return {
            "match_class": MATCH_COMPLIANT_EQUAL,
            "bid_ready": True,
            "reasons": reasons + ["compliant_equal_salient_verified"],
            "authorization_gate": auth_gate,
        }

    if brand_match or (r_model and p_model and r_model[:4] == p_model[:4]):
        return {
            "match_class": MATCH_POSSIBLE,
            "bid_ready": False,
            "reasons": reasons + ["possible_match_not_bid_ready"],
            "authorization_gate": auth_gate,
        }

    if r_model and p_model and r_model != p_model and not equal_ok:
        return {
            "match_class": MATCH_NONCOMPLIANT,
            "bid_ready": False,
            "reasons": reasons + ["model_mismatch_no_or_equal"],
            "authorization_gate": auth_gate,
        }

    return {
        "match_class": MATCH_UNKNOWN,
        "bid_ready": False,
        "reasons": reasons + ["insufficient_verified_evidence"],
        "authorization_gate": auth_gate,
    }
