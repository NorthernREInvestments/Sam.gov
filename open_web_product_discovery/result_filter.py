"""Classify open-web search hits before page validation."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from exact_product_url_discovery.classify import classify_candidate_url, is_search_shell
from exact_product_url_discovery.normalize import mpn_in_text, norm_token
from open_web_product_discovery.models import (
    BLOG,
    CATEGORY_PAGE,
    EXACT_PRODUCT_CANDIDATE,
    MARKETING_PAGE,
    PARTIAL_MATCH,
    PDF_ONLY,
    PLACEHOLDER,
    REJECTED_RESULT_TYPES,
    SEARCH_PAGE,
    WRONG_PRODUCT,
)


def classify_search_hit(
    *,
    url: str,
    title: str = "",
    snippet: str = "",
    mpn: str,
    manufacturer: str | None = None,
) -> dict[str, Any]:
    """Return result classification + mention flags. Never treats snippet as price."""
    u = (url or "").strip()
    title = title or ""
    snippet = snippet or ""
    host = urlparse(u).netloc.lower().replace("www.", "")
    out: dict[str, Any] = {
        "url": u,
        "title": title[:200],
        "snippet": snippet[:300],
        "domain": host,
        "mpn_in_url": False,
        "mpn_in_title": False,
        "mpn_in_snippet": False,
        "manufacturer_in_title": False,
        "manufacturer_in_snippet": False,
        "result_type": WRONG_PRODUCT,
        "structural_class": None,
    }
    if not u.startswith("http"):
        out["result_type"] = PLACEHOLDER
        return out
    if u.lower().endswith(".pdf"):
        out["result_type"] = PDF_ONLY
        return out
    if is_search_shell(u):
        out["result_type"] = SEARCH_PAGE
        return out

    structural = classify_candidate_url(u, mpn=mpn)
    out["structural_class"] = structural
    if structural in {
        "SEARCH_RESULT",
        "SEARCH_RESULT_SHELL",
        "SITE_SEARCH_SHELL",
    }:
        out["result_type"] = SEARCH_PAGE
        return out
    if structural in {"CATEGORY_PAGE", "COLLECTION_PAGE"}:
        out["result_type"] = CATEGORY_PAGE
        return out
    if structural == "BLOG_PAGE":
        out["result_type"] = BLOG
        return out
    if structural == "PRODUCT_FAMILY_ONLY":
        out["result_type"] = PARTIAL_MATCH
        return out

    path = urlparse(u).path.lower()
    if any(x in path for x in ("/about", "/careers", "/investor", "/newsroom", "/support/contact")):
        out["result_type"] = MARKETING_PAGE
        return out

    out["mpn_in_url"] = mpn_in_text(mpn, path)
    out["mpn_in_title"] = mpn_in_text(mpn, title)
    out["mpn_in_snippet"] = mpn_in_text(mpn, snippet)
    if manufacturer:
        mfr0 = manufacturer.split()[0]
        out["manufacturer_in_title"] = mfr0.lower() in title.lower()
        out["manufacturer_in_snippet"] = mfr0.lower() in snippet.lower()

    # Short numeric MPN: require manufacturer signal in title/url or structural product path
    tok = norm_token(mpn)
    short = len(tok) <= 5 and tok.isdigit()
    has_mpn = out["mpn_in_url"] or out["mpn_in_title"]
    if short:
        if not has_mpn:
            out["result_type"] = PARTIAL_MATCH
            return out
        if manufacturer and not (out["manufacturer_in_title"] or out["mpn_in_url"]):
            # allow if structural product page with mpn in path
            if structural not in {"EXACT_PRODUCT_VERIFIED", "EXACT_PRODUCT_UNVERIFIED"}:
                out["result_type"] = PARTIAL_MATCH
                return out
    else:
        if not (has_mpn or out["mpn_in_snippet"]):
            # still allow strong structural product URLs; page validation will decide
            if structural not in {"EXACT_PRODUCT_VERIFIED", "EXACT_PRODUCT_UNVERIFIED"}:
                out["result_type"] = PARTIAL_MATCH
                return out

    out["result_type"] = EXACT_PRODUCT_CANDIDATE
    return out


def is_allowed_candidate(row: dict[str, Any]) -> bool:
    return row.get("result_type") == EXACT_PRODUCT_CANDIDATE
