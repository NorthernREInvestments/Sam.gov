"""Strict URL type classification for product discovery."""

from __future__ import annotations

import re
from urllib.parse import urlparse

from exact_product_url_discovery.models import (
    BLOG_PAGE,
    CATEGORY_PAGE,
    COLLECTION_PAGE,
    EXACT_PRODUCT_UNVERIFIED,
    EXACT_PRODUCT_VERIFIED,
    PDF_WITHOUT_PRODUCT_ID,
    PRODUCT_FAMILY_ONLY,
    SEARCH_RESULT,
    SEARCH_RESULT_SHELL,
    SITE_SEARCH_SHELL,
    WRONG_PRODUCT,
)
from exact_product_url_discovery.normalize import mpn_in_text, norm_token


_AUTH_PATH_RE = re.compile(
    r"(^|/)(register|login|signin|sign-in|signup|sign-up|account|customer|cart|checkout|"
    r"wishlist|password|forgot|auth|oauth|sso)(/|$)",
    re.I,
)
_NON_PRODUCT_SLUGS = {
    "register",
    "login",
    "signin",
    "signup",
    "account",
    "cart",
    "checkout",
    "search",
    "contact",
    "about",
    "help",
    "faq",
    "blog",
    "news",
    "home",
    "index",
    "wishlist",
    "compare",
}


def classify_candidate_url(url: str, *, mpn: str | None = None) -> str:
    """Classify URL structure before page-content identity validation."""
    u = (url or "").strip()
    if not u.startswith("http"):
        return WRONG_PRODUCT
    # sanitize
    u = "".join(ch for ch in u if 32 <= ord(ch) < 127)
    p = urlparse(u)
    host = (p.netloc or "").lower().replace("www.", "")
    path = (p.path or "").lower()
    qs = (p.query or "").lower()
    slug = path.strip("/").split("/")[0] if path.strip("/") else ""

    if path.endswith(".pdf"):
        return PDF_WITHOUT_PRODUCT_ID

    # Auth / account shells (never product pages — even with MPN in returnUrl)
    if _AUTH_PATH_RE.search(path) or slug in _NON_PRODUCT_SLUGS:
        return SITE_SEARCH_SHELL
    if "returnurl=" in qs and ("/search" in qs or "search%3f" in qs or "search?" in qs):
        return SITE_SEARCH_SHELL

    # Permanent search shells
    if "quill.com" in host and "/search" in path:
        return SITE_SEARCH_SHELL
    if "staples.com" in host and ("/directory_" in path or "/search" in path):
        return SITE_SEARCH_SHELL
    if "1000bulbs.com" in host and "/search" in path:
        return SITE_SEARCH_SHELL
    if "nationaldistributorllc" in host and ("?s=" in u.lower() or path in {"", "/"}):
        return SEARCH_RESULT_SHELL
    if "homedepot.com" in host and path.rstrip("/").endswith("/s"):
        return SITE_SEARCH_SHELL
    if "lowes.com" in host and "/search" in path:
        return SITE_SEARCH_SHELL
    if "mscdirect.com" in host and "/browse/tn" in path:
        return SITE_SEARCH_SHELL
    if "officedepot.com" in host and "/catalog/search" in path:
        return SITE_SEARCH_SHELL

    if re.search(r"(^|&)(s|q|query|keywords|search|searchterm|text|term|ntt)=", qs):
        if not any(x in path for x in ("/product", "/p/", "/parts/", "/item/", "/sku/", "/cbs/")):
            return SEARCH_RESULT_SHELL
    if any(x in path for x in ("/search", "/browse/tn", "/catalog/search", "/s/", "/find", "/catalogsearch/")):
        if "/p/" not in path and "/product" not in path and "/cbs/" not in path and "/parts/" not in path:
            return SITE_SEARCH_SHELL

    if any(x in path for x in ("/blog", "/article", "/news/", "/resources/")):
        return BLOG_PAGE
    if any(x in path for x in ("/category", "/c/", "/collections/", "/collection/")):
        return CATEGORY_PAGE if "/collections/" not in path else COLLECTION_PAGE
    if "family" in path and "/product" not in path:
        return PRODUCT_FAMILY_ONLY

    productish = any(
        x in path
        for x in (
            "/product/",
            "/products/",
            "/p/",
            "/pd/",
            "/item/",
            "/sku/",
            "/parts/",
            "/shop/p/",
            "/en/products/",
            "/cbs/",
            "/catalog/product",
            "/dp/",
            "/labels/",
            "/prod-",
        )
    )
    # Domain-specific product patterns (slug must look like a product, not auth)
    if (
        host.endswith("dieselpartsdirect.com")
        and path.strip("/").count("/") == 0
        and len(slug) >= 4
        and slug not in _NON_PRODUCT_SLUGS
        and not _AUTH_PATH_RE.search(path)
    ):
        productish = True
    if "bradyid.com" in host and "/tags/" in path:
        productish = True
    if "parts-hvac.com" in host and path.endswith(".html"):
        productish = True
    if "channellock.com" in host and re.search(r"/\d+", path):
        productish = True
    if "mccoys.com" in host and "/shop/p/" in path:
        productish = True
    if "filtersfast.com" in host and ("/prod-" in path or "/product" in path):
        productish = True
    if "seton.com" in host and path.endswith(".html") and slug not in _NON_PRODUCT_SLUGS:
        productish = True
    if "carid.com" in host and path.endswith(".html"):
        productish = True
    if "bulbs.com" in host and "/product/" in path:
        productish = True
    if "northernsafety.com" in host and "/product/" in path:
        productish = True
    if "toolbarn.com" in host and path.strip("/") and slug not in _NON_PRODUCT_SLUGS:
        productish = True
    if "globalindustrial.com" in host and "/p/" in path:
        productish = True
    if "thedieselstore.com" in host and path.endswith(".html"):
        productish = True
    if "fluke.com" in host and "/product/" in path:
        productish = True
    if "leviton.com" in host and "/products/" in path:
        productish = True
    if "acmetools.com" in host and path.endswith(".html"):
        productish = True
    if "officedepot.com" in host and "/a/products/" in path:
        productish = True
    if "quill.com" in host and "/cbs/" in path:
        productish = True
    if "pexuniverse.com" in host and path.strip("/") and slug not in _NON_PRODUCT_SLUGS:
        productish = True
    if "kleintools.com" in host and "/catalog/" in path:
        productish = True
    if "3m.com" in host and "/p/" in path:
        productish = True
    if "crcautocare.com" in host and "/product/" in path:
        productish = True
    if "wd40.com" in host and "/products/" in path:
        productish = True
    if "feit.com" in host and "/products/" in path:
        productish = True
    if "gorillatough.com" in host and "/product/" in path:
        productish = True
    if "filtersfast.com" in host and ("/prod-" in path or "prod-" in path):
        productish = True
    if "supplyhouse.com" in host and path.strip("/") and "/" not in path.strip("/") and slug not in _NON_PRODUCT_SLUGS:
        productish = True
    if "ferguson.com" in host and "/product/" in path:
        productish = True
    if "honeywellhome.com" in host and "/products/" in path:
        productish = True
    if "permatex.com" in host and "/products/" in path:
        productish = True
    if "crcindustries.com" in host and "/products/" in path:
        productish = True
    if "summitracing.com" in host and "/parts/" in path:
        productish = True
    if "milwaukeetool.com" in host and "/products/" in path:
        productish = True
    if "channellock.com" in host and re.search(r"/\d+", path):
        productish = True
    if "homedepot.com" in host and re.search(r"/p/", path):
        productish = True
    if "amazon.com" in host and "/dp/" in path:
        productish = True
    if "acuitybrands.com" in host and "/products/detail/" in path:
        productish = True
    if "hubbell.com" in host and "/products/" in path:
        productish = True

    if not productish:
        # bare path with MPN may still be product on thin catalogs
        if (
            mpn
            and mpn_in_text(mpn, path)
            and path.strip("/").count("/") <= 2
            and len(path.strip("/")) >= 4
            and slug not in _NON_PRODUCT_SLUGS
        ):
            return EXACT_PRODUCT_UNVERIFIED
        return SEARCH_RESULT if "search" in path or qs else EXACT_PRODUCT_UNVERIFIED

    if mpn:
        tok = norm_token(mpn)
        # Only match MPN in PATH — never query/returnUrl (auth redirect shells)
        segs = [norm_token(s) for s in path.split("/") if s]
        for seg in segs:
            if not tok or len(tok) < 3 or tok not in seg:
                continue
            idx = seg.find(tok)
            after = seg[idx + len(tok) : idx + len(tok) + 1]
            before = seg[idx - 1 : idx] if idx > 0 else ""
            if after.isdigit() and tok[-1].isdigit():
                continue
            if before.isdigit() and tok[0].isdigit():
                continue
            return EXACT_PRODUCT_VERIFIED
        return EXACT_PRODUCT_UNVERIFIED
    return EXACT_PRODUCT_UNVERIFIED


def is_search_shell(url: str) -> bool:
    cls = classify_candidate_url(url)
    return cls in {SEARCH_RESULT, SEARCH_RESULT_SHELL, SITE_SEARCH_SHELL}
