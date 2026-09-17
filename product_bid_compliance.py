"""Product, brand-or-equal, authorization, and origin compliance for bids."""

from __future__ import annotations

import re
from typing import Any

from bid_compliance_constants import (
    AMBIGUOUS_SUB,
    AUTHZ_AMBIGUOUS,
    AUTHZ_BIDDER,
    AUTHZ_DOC,
    AUTHZ_FUTURE,
    AUTHZ_NONE,
    AUTHZ_PRODUCT,
    BRAND_ONLY,
    BRAND_OR_EQUAL,
    EQUAL_PREAPPROVAL,
    EQUAL_WITH_DOC,
    FUTURE_ACTION,
    ORIGIN_DOC,
    ORIGIN_LIKELY,
    ORIGIN_POTENTIAL_FAIL,
    ORIGIN_UNKNOWN,
    ORIGIN_VERIFIED,
    PROD_AUTH_GAP,
    PROD_DOC_GAP,
    PROD_EQUIV,
    PROD_EXACT,
    PROD_LIKELY,
    PROD_NONCOMPLIANT,
    PROD_ORIGIN_GAP,
    PROD_SPEC_GAP,
    PROD_UNKNOWN,
)


def classify_substitution_policy(text: str | None, *, brand_or_equal: bool | None = None, brand_only: bool | None = None) -> dict[str, Any]:
    t = (text or "").lower()
    if brand_only is True or re.search(r"\bno\s+substitut|\bbrand[\s-]*name\s+only\b|\bexact\s+(?:oem|brand|model)\b", t):
        return {"policy": BRAND_ONLY, "brand_or_equal_permitted": False, "source": "text_or_flag"}
    if re.search(r"\bequal\s+requires?\s+pre[\s-]*approval\b|\bprior\s+approval\b.{0,30}\bequal", t):
        return {"policy": EQUAL_PREAPPROVAL, "brand_or_equal_permitted": True, "source": "text"}
    if re.search(r"\bequal\b.{0,40}\b(documentation|salient|cut[\s-]*sheet)\b", t) or re.search(
        r"\bdocumentation\b.{0,40}\bequal", t
    ):
        return {"policy": EQUAL_WITH_DOC, "brand_or_equal_permitted": True, "source": "text"}
    if brand_or_equal is True or re.search(r"\bbrand[\s-]*name\s+or\s+equal\b|\bor\s+equal\b", t):
        return {"policy": BRAND_OR_EQUAL, "brand_or_equal_permitted": True, "source": "text_or_flag"}
    if re.search(r"\bsubstitut", t):
        return {"policy": AMBIGUOUS_SUB, "brand_or_equal_permitted": None, "source": "ambiguous_substitution_language"}
    return {"policy": AMBIGUOUS_SUB, "brand_or_equal_permitted": None, "source": "no_clear_policy"}


def extract_brand_or_equal_details(text: str | None) -> dict[str, Any]:
    t = text or ""
    policy = classify_substitution_policy(t)
    named = None
    m = re.search(
        r"(?:brand|manufacturer|model|part\s*(?:number|no\.?))[:\s]+([A-Za-z0-9][A-Za-z0-9\s\-/]{2,60})",
        t,
        re.I,
    )
    if m:
        named = m.group(1).strip()
    salient = None
    sm = re.search(r"salient\s+characteristics?[:\s]+([^\n]{10,200})", t, re.I)
    if sm:
        salient = sm.group(1).strip()
    return {
        **policy,
        "named_brand_model": named,
        "salient_characteristics": salient,
        "documentation_for_equal": policy["policy"] in {EQUAL_WITH_DOC, EQUAL_PREAPPROVAL},
        "preapproval_required": policy["policy"] == EQUAL_PREAPPROVAL,
        "note": "Do not auto-replace requested product",
    }


def classify_authorization_requirement(text: str | None) -> dict[str, Any]:
    """Distinguish product authenticity from bidder authorization."""
    t = (text or "").lower()
    bidder_auth = bool(
        re.search(
            r"\b(?:bidder|offeror|vendor)\s+(?:must|shall)\s+be\s+(?:an?\s+)?(?:authorized|oem)",
            t,
        )
        or re.search(r"\bonly\s+authorized\s+(?:resellers?|distributors?)\s+may\s+(?:bid|submit)", t)
    )
    doc_req = bool(re.search(r"\b(letter\s+of\s+authorization|manufacturer\s+authorization|oem\s+letter)\b", t))
    product_channel = bool(re.search(r"\bnew\s+(?:oem|genuine)\s+product\b|\bmust\s+be\s+(?:new|genuine)\b", t))
    auth_channel_product = bool(re.search(r"\bauthorized\s+channel\b|\bfrom\s+authorized\s+(?:reseller|distributor)", t))

    if bidder_auth:
        state = AUTHZ_BIDDER
    elif doc_req:
        state = AUTHZ_DOC
    elif auth_channel_product:
        state = AUTHZ_PRODUCT
    elif product_channel and not bidder_auth:
        # Genuine/new OEM product ≠ bidder must be authorized
        state = AUTHZ_NONE
        return {
            "state": state,
            "bidder_authorization_required": False,
            "product_authenticity_required": True,
            "documentation_required": False,
            "distinction": "new_oem_product_is_not_bidder_authorization",
            "operator_action": None,
        }
    elif re.search(r"\bauthorized\b", t) and re.search(r"\b(reseller|distributor|oem)\b", t):
        state = AUTHZ_AMBIGUOUS
    else:
        state = AUTHZ_NONE

    return {
        "state": state if state != AUTHZ_NONE else AUTHZ_NONE,
        "bidder_authorization_required": state == AUTHZ_BIDDER,
        "product_authenticity_required": product_channel,
        "documentation_required": state in {AUTHZ_DOC, AUTHZ_BIDDER},
        "distinction": "product_authenticity_vs_bidder_authorization",
        "operator_action": FUTURE_ACTION if state in {AUTHZ_BIDDER, AUTHZ_DOC, AUTHZ_AMBIGUOUS} else None,
        "future_state": AUTHZ_FUTURE if state in {AUTHZ_BIDDER, AUTHZ_DOC} else None,
    }


def classify_origin_compliance(
    text: str | None,
    *,
    product_origin: str | None = None,
    origin_verified: bool | None = None,
) -> dict[str, Any]:
    t = (text or "").lower()
    requires_baa = bool(re.search(r"\bbuy\s+american\b|\bbaa\b", t))
    requires_taa = bool(re.search(r"\btrade\s+agreements?\s+act\b|\btaa\b", t))
    domestic = bool(re.search(r"\bdomestic\s+(?:end[\s-]*product|content|preference)\b", t))
    restriction = requires_baa or requires_taa or domestic or bool(re.search(r"\bcountry\s+of\s+origin\b", t))

    if not restriction:
        return {
            "requirement_found": False,
            "state": ORIGIN_UNKNOWN,
            "note": "no_domestic_or_origin_clause_detected",
            "legal_advice": False,
        }

    if origin_verified is True and product_origin:
        state = ORIGIN_VERIFIED
    elif product_origin and origin_verified is not False:
        state = ORIGIN_LIKELY
    elif product_origin is None:
        state = ORIGIN_DOC if restriction else ORIGIN_UNKNOWN
    else:
        state = ORIGIN_POTENTIAL_FAIL if origin_verified is False else ORIGIN_DOC

    return {
        "requirement_found": True,
        "buy_american": requires_baa,
        "trade_agreements_act": requires_taa,
        "domestic_preference": domestic,
        "product_origin": product_origin,
        "state": state,
        "note": "Not legal advice; source-backed signals only",
        "legal_advice": False,
        "operator_action": FUTURE_ACTION if state in {ORIGIN_DOC, ORIGIN_UNKNOWN, ORIGIN_POTENTIAL_FAIL} else None,
    }


def evaluate_product_compliance(
    *,
    required_manufacturer: str | None = None,
    required_model: str | None = None,
    required_part: str | None = None,
    offered_manufacturer: str | None = None,
    offered_model: str | None = None,
    offered_part: str | None = None,
    substitution_policy: dict[str, Any] | None = None,
    authorization: dict[str, Any] | None = None,
    origin: dict[str, Any] | None = None,
    package_text: str | None = None,
) -> dict[str, Any]:
    """Never infer brand-or-equal unless governing text permits it."""
    policy = substitution_policy or classify_substitution_policy(package_text)
    authz = authorization or classify_authorization_requirement(package_text)
    orig = origin or classify_origin_compliance(package_text)

    def _norm(x: str | None) -> str:
        return re.sub(r"[^a-z0-9]+", "", (x or "").lower())

    exact = False
    if required_part and offered_part and _norm(required_part) == _norm(offered_part):
        exact = True
    elif (
        required_manufacturer
        and offered_manufacturer
        and required_model
        and offered_model
        and _norm(required_manufacturer) == _norm(offered_manufacturer)
        and _norm(required_model) == _norm(offered_model)
    ):
        exact = True

    if exact:
        state = PROD_EXACT
    elif policy.get("brand_or_equal_permitted") is True and offered_model:
        if policy.get("policy") == EQUAL_WITH_DOC:
            state = PROD_DOC_GAP
        elif policy.get("policy") == EQUAL_PREAPPROVAL:
            state = PROD_DOC_GAP
        else:
            state = PROD_EQUIV  # candidate equivalent — not auto-accepted as awardable
    elif policy.get("policy") == BRAND_ONLY and offered_model and not exact:
        state = PROD_NONCOMPLIANT
    elif required_model or required_part:
        if not offered_model and not offered_part:
            state = PROD_UNKNOWN
        else:
            state = PROD_SPEC_GAP if policy.get("brand_or_equal_permitted") is not True else PROD_LIKELY
    else:
        state = PROD_UNKNOWN

    if authz.get("bidder_authorization_required") and authz.get("state") == AUTHZ_BIDDER:
        if state in {PROD_EXACT, PROD_EQUIV, PROD_LIKELY}:
            state = PROD_AUTH_GAP
    origin_gap = orig.get("state") in {ORIGIN_DOC, ORIGIN_POTENTIAL_FAIL, ORIGIN_UNKNOWN} and orig.get(
        "requirement_found"
    )
    if orig.get("state") == ORIGIN_POTENTIAL_FAIL and state == PROD_EXACT:
        state = PROD_ORIGIN_GAP
    elif origin_gap and state == PROD_EXACT:
        # Keep exact product match; origin documentation tracked separately
        pass

    # Unsupported substitution rejection
    unsupported_substitution = (
        policy.get("policy") == BRAND_ONLY
        and bool(offered_model or offered_part)
        and not exact
    )

    return {
        "kind": "ProductCompliance",
        "state": state,
        "exact_match": exact,
        "origin_documentation_required": bool(origin_gap and orig.get("state") == ORIGIN_DOC),
        "substitution_policy": policy,
        "authorization": authz,
        "origin": orig,
        "unsupported_substitution_rejected": unsupported_substitution,
        "required": {
            "manufacturer": required_manufacturer,
            "model": required_model,
            "part": required_part,
        },
        "offered": {
            "manufacturer": offered_manufacturer,
            "model": offered_model,
            "part": offered_part,
        },
        "note": "Never auto-replace requested product; brand-or-equal only when permitted",
    }
