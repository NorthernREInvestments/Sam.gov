"""Live PDP audit: live check → identity → public offer → state classification."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

from exact_product_url_discovery.classify import classify_candidate_url, is_search_shell
from exact_product_url_discovery.validate_identity import validate_product_page
from live_exact_priced_pdp.models import (
    CATEGORY_PAGE,
    DEAD_404,
    FAMILY_PAGE,
    HUMAN_ACCESS_BLOCKED,
    HUMAN_NO_PUBLIC_PRICE,
    HUMAN_PUBLIC_PRICE_HIDDEN_BUT_OFFER_EXISTS,
    HUMAN_PUBLIC_PRICE_VISIBLE,
    HUMAN_UNKNOWN,
    LIVE_EXACT_PDP_ACCESS_BLOCKED,
    LIVE_EXACT_PDP_LOGIN_REQUIRED,
    LIVE_EXACT_PDP_NO_PUBLIC_PRICE,
    LIVE_EXACT_PDP_PRICE_HIDDEN,
    LIVE_EXACT_PDP_QUOTE_ONLY,
    LIVE_EXACT_PDP_WRONG_PACK,
    LIVE_EXACT_PDP_WRONG_VARIANT,
    LIVE_EXACT_PRICED_PDP,
    LOGIN_REQUIRED,
    MARKETING_PAGE,
    NO_PUBLIC_OFFER,
    PUBLIC_PRICE_JS_HIDDEN,
    PUBLIC_PRICE_STRUCTURED,
    PUBLIC_PRICE_VISIBLE,
    QUOTE_ONLY,
    SEARCH_SHELL,
    SOFT_URL_UNVERIFIED,
    WRONG_MANUFACTURER,
    WRONG_MPN,
    WRONG_PRODUCT,
)
from price_adapters.validate import seller_of
from public_price_search.extract import _f
from public_price_search.search import fetch_page


_BOT_MARKERS = (
    "attention required",
    "just a moment",
    "access denied",
    "access to this page has been denied",
    "cf-browser-verification",
    "captcha",
    "enable javascript and cookies",
)
_404_MARKERS = ("page not found", "404 not found", "404 error", "product not found", "we can't find")
_QUOTE_MARKERS = ("request a quote", "call for price", "call for pricing", "contact for price", "rfq", "get a quote")
_LOGIN_MARKERS = ("sign in to see", "login to see price", "log in to view", "sign in for pricing", "create an account to see")
_CART_MARKERS = ("add to cart", "add to bag", "buy now", "add to basket", "purchase")
_PRICE_STRUCT_RE = re.compile(
    r'("price"\s*:\s*"?\d|"offers"\s*:|itemprop=["\']price["\']|schema\.org/Offer|currentPrice|salePrice|data-price)',
    re.I,
)


def _title_of(html: str) -> str:
    m = re.search(r"<title[^>]*>([^<]{0,180})", html or "", re.I)
    return (m.group(1).strip() if m else "")


def live_fetch(url: str, *, allow_browser: bool = False) -> dict[str, Any]:
    """Fetch page and capture live status markers."""
    fr = fetch_page(url, use_budget=True)
    html = fr.get("text") or ""
    status = fr.get("status_code")
    final = fr.get("final_url") or url
    title = _title_of(html)
    blocked = bool(fr.get("blocked"))
    blob = f"{title} {html[:3000]}".lower()

    bot = blocked or any(x in blob for x in _BOT_MARKERS)
    is_404 = status in {404, 410} or any(x in blob for x in _404_MARKERS)
    login = any(x in blob for x in _LOGIN_MARKERS)
    if allow_browser and (bot or len(html) < 500) and not is_404:
        try:
            from price_adapters.browser import bounded_browser_fetch

            br = bounded_browser_fetch(url, timeout_ms=15000, wait_ms=2000, force_refresh=True)
            if br.get("ok") and (br.get("html") or ""):
                html = br.get("html") or html
                title = _title_of(html)
                blob = f"{title} {html[:3000]}".lower()
                bot = any(x in blob for x in _BOT_MARKERS)
                is_404 = any(x in blob for x in _404_MARKERS)
                login = any(x in blob for x in _LOGIN_MARKERS)
                final = br.get("url") or final
        except Exception:
            pass

    return {
        "url": url,
        "final_url": final,
        "status_code": status,
        "title": title[:160],
        "html": html,
        "text_sample": (fr.get("text") or "")[:0],  # kept short in evidence
        "ok": bool(fr.get("ok")) and not is_404 and not bot,
        "bot_wall": bot,
        "is_404": is_404,
        "login_wall": login,
        "content_type": fr.get("content_type"),
        "canonical": _canonical(html),
        "domain": seller_of(final or url),
    }


def _canonical(html: str) -> str | None:
    m = re.search(r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)["\']', html or "", re.I)
    if m:
        return m.group(1)
    m = re.search(r'<link[^>]+href=["\']([^"\']+)["\'][^>]+rel=["\']canonical["\']', html or "", re.I)
    return m.group(1) if m else None


def detect_public_offer(html: str, text: str = "") -> dict[str, Any]:
    """Classify whether a public commercial offer exists on the page."""
    blob = f"{html[:200000]} {text[:20000]}".lower()
    dollars = re.findall(r"\$\s*(\d{1,5}(?:\.\d{2})?)", text[:30000] if text else html[:50000])
    visible_prices = []
    for d in dollars[:8]:
        p = _f(d)
        if p and p >= 1.51:
            visible_prices.append(p)

    has_jsonld_offer = bool(
        re.search(
            r'application/ld\+json[^>]*>[^<]*"offers"',
            html or "",
            re.I | re.S,
        )
    ) or ('"@type":"offer"' in blob or '"@type": "offer"' in blob)
    has_struct = bool(_PRICE_STRUCT_RE.search(html or ""))
    has_cart = any(x in blob for x in _CART_MARKERS)
    quote = any(x in blob for x in _QUOTE_MARKERS)
    login = any(x in blob for x in _LOGIN_MARKERS)

    if quote and not visible_prices and not has_jsonld_offer:
        offer = QUOTE_ONLY
    elif login and not visible_prices:
        offer = LOGIN_REQUIRED
    elif visible_prices:
        offer = PUBLIC_PRICE_VISIBLE
    elif has_jsonld_offer or has_struct:
        offer = PUBLIC_PRICE_STRUCTURED if has_jsonld_offer else PUBLIC_PRICE_JS_HIDDEN
    elif has_cart:
        offer = PUBLIC_PRICE_JS_HIDDEN
    else:
        offer = NO_PUBLIC_OFFER

    return {
        "offer_status": offer,
        "visible_prices": visible_prices[:5],
        "has_jsonld_offer": has_jsonld_offer,
        "has_structured_price": has_struct,
        "has_add_to_cart": has_cart,
        "quote_markers": quote,
        "login_markers": login,
    }


def _human_visibility(state: str, offer: str, live: dict[str, Any]) -> str:
    if live.get("bot_wall") or state == LIVE_EXACT_PDP_ACCESS_BLOCKED:
        return HUMAN_ACCESS_BLOCKED
    if offer == PUBLIC_PRICE_VISIBLE:
        return HUMAN_PUBLIC_PRICE_VISIBLE
    if offer in {PUBLIC_PRICE_STRUCTURED, PUBLIC_PRICE_JS_HIDDEN}:
        return HUMAN_PUBLIC_PRICE_HIDDEN_BUT_OFFER_EXISTS
    if offer in {NO_PUBLIC_OFFER, QUOTE_ONLY, LOGIN_REQUIRED}:
        return HUMAN_NO_PUBLIC_PRICE
    return HUMAN_UNKNOWN


def audit_url(url: str, item: dict[str, Any], *, allow_browser: bool = True, soft: bool = False) -> dict[str, Any]:
    """Full live audit of one candidate URL against an unresolved item."""
    mpn = str(item.get("mpn") or item.get("part_number") or "")
    mfr = str(item.get("manufacturer") or "")
    out: dict[str, Any] = {
        "url": url,
        "benchmark_id": item.get("benchmark_id"),
        "domain": seller_of(url),
        "soft_input": soft,
        "state": SOFT_URL_UNVERIFIED if soft else None,
        "offer_status": None,
        "human_visibility": HUMAN_UNKNOWN,
        "identity_ok": False,
        "extractable": False,
        "evidence": {},
    }
    if not url or not url.startswith("http"):
        out["state"] = WRONG_PRODUCT
        return out
    if is_search_shell(url):
        out["state"] = SEARCH_SHELL
        return out

    structural = classify_candidate_url(url, mpn=mpn)
    if structural in {"SEARCH_RESULT", "SEARCH_RESULT_SHELL", "SITE_SEARCH_SHELL"}:
        out["state"] = SEARCH_SHELL
        out["evidence"]["structural"] = structural
        return out
    if structural in {"CATEGORY_PAGE", "COLLECTION_PAGE"}:
        out["state"] = CATEGORY_PAGE
        out["evidence"]["structural"] = structural
        return out
    if structural == "PRODUCT_FAMILY_ONLY":
        out["state"] = FAMILY_PAGE
        out["evidence"]["structural"] = structural
        return out
    if structural == "BLOG_PAGE":
        out["state"] = MARKETING_PAGE
        out["evidence"]["structural"] = structural
        return out

    live = live_fetch(url, allow_browser=allow_browser)
    out["domain"] = live.get("domain") or out["domain"]
    out["evidence"]["live"] = {
        k: live.get(k)
        for k in (
            "status_code",
            "final_url",
            "title",
            "bot_wall",
            "is_404",
            "login_wall",
            "canonical",
            "ok",
        )
    }
    html = live.get("html") or ""

    if live.get("is_404"):
        out["state"] = DEAD_404
        out["human_visibility"] = HUMAN_NO_PUBLIC_PRICE
        return out

    if live.get("bot_wall"):
        # Without independent identity proof, do not claim exact PDP
        out["state"] = LIVE_EXACT_PDP_ACCESS_BLOCKED
        out["human_visibility"] = HUMAN_ACCESS_BLOCKED
        out["offer_status"] = NO_PUBLIC_OFFER
        return out

    # Soft URLs that never got hard identity stay soft until proven
    if soft and not html:
        out["state"] = SOFT_URL_UNVERIFIED
        return out

    # Identity validation
    v = validate_product_page(
        url,
        mpn=mpn,
        manufacturer=mfr,
        description=item.get("description"),
        category=item.get("category"),
        allow_browser=False,
    )
    if not v.get("identity_match") and allow_browser:
        v = validate_product_page(
            url,
            mpn=mpn,
            manufacturer=mfr,
            description=item.get("description"),
            category=item.get("category"),
            allow_browser=True,
        )
    out["evidence"]["identity"] = {
        "identity_match": v.get("identity_match"),
        "reason": v.get("reason"),
        "title": v.get("title"),
    }
    if not v.get("identity_match"):
        reason = str(v.get("reason") or "identity_fail")
        if "manufacturer" in reason:
            out["state"] = WRONG_MANUFACTURER
        elif "mpn" in reason or "part" in reason:
            out["state"] = WRONG_MPN
        elif "pack" in reason:
            out["state"] = LIVE_EXACT_PDP_WRONG_PACK
        elif "variant" in reason:
            out["state"] = LIVE_EXACT_PDP_WRONG_VARIANT
        elif "auth" in reason or "fetch" in reason:
            out["state"] = LIVE_EXACT_PDP_ACCESS_BLOCKED
            out["human_visibility"] = HUMAN_ACCESS_BLOCKED
        else:
            out["state"] = WRONG_PRODUCT
        return out

    out["identity_ok"] = True
    # Public offer detection (use HTML; optional text via simple strip)
    text = re.sub(r"<[^>]+>", " ", html or "")
    offer = detect_public_offer(html, text)
    out["offer_status"] = offer["offer_status"]
    out["evidence"]["offer"] = {k: offer.get(k) for k in offer if k != "visible_prices"}
    out["evidence"]["visible_prices"] = offer.get("visible_prices")

    if live.get("login_wall") or offer["offer_status"] == LOGIN_REQUIRED:
        out["state"] = LIVE_EXACT_PDP_LOGIN_REQUIRED
    elif offer["offer_status"] == QUOTE_ONLY:
        out["state"] = LIVE_EXACT_PDP_QUOTE_ONLY
    elif offer["offer_status"] == PUBLIC_PRICE_VISIBLE:
        out["state"] = LIVE_EXACT_PRICED_PDP
        out["extractable"] = True
    elif offer["offer_status"] in {PUBLIC_PRICE_STRUCTURED, PUBLIC_PRICE_JS_HIDDEN}:
        out["state"] = LIVE_EXACT_PDP_PRICE_HIDDEN
        out["extractable"] = True
    else:
        out["state"] = LIVE_EXACT_PDP_NO_PUBLIC_PRICE

    out["human_visibility"] = _human_visibility(out["state"], out["offer_status"], live)
    # Marketing heuristic: live + identity but about/support path already handled; OEM brochure
    path = urlparse(url).path.lower()
    if any(x in path for x in ("/about", "/support", "/resources", "/where-to-buy")) and out["state"] == LIVE_EXACT_PDP_NO_PUBLIC_PRICE:
        out["state"] = MARKETING_PAGE
        out["extractable"] = False
    return out
