"""URL confidence classification — only EXACT_PRODUCT_VERIFIED drives final price."""

from __future__ import annotations

import re
from urllib.parse import urlparse


def classify_url(url: str, *, mpn: str | None = None) -> str:
    from exact_page_extraction.models import (
        CATEGORY_PAGE,
        EXACT_PRODUCT_UNVERIFIED,
        EXACT_PRODUCT_VERIFIED,
        PRODUCT_FAMILY_PAGE,
        SEARCH_RESULT_SHELL,
        WRONG_PRODUCT,
    )

    u = (url or "").strip()
    if not u.startswith("http"):
        return WRONG_PRODUCT
    p = urlparse(u)
    host = (p.netloc or "").lower().replace("www.", "")
    path = (p.path or "").lower()
    qs = (p.query or "").lower()

    # Permanent: nationaldistributor search shells
    if "nationaldistributorllc" in host and ("?s=" in u.lower() or path in {"", "/"}):
        return SEARCH_RESULT_SHELL
    if re.search(r"(^|&)(s|q|query|keywords|search|searchterm|text|term|Ntt)=", qs):
        if not any(x in path for x in ("/product", "/p/", "/parts/", "/item/", "/sku/", "/cbs/")):
            return SEARCH_RESULT_SHELL
    if any(x in path for x in ("/search", "/browse/tn", "/catalog/search", "/s/")):
        # Homedepot /s/ is search; Platt /p/ is product; Quill /cbs/ is product
        if "/p/" not in path and "/product" not in path and "/cbs/" not in path:
            return SEARCH_RESULT_SHELL
    # Quill/Staples keyword search shells are never exact product pages
    if "quill.com" in host and "/search" in path:
        return SEARCH_RESULT_SHELL
    if "staples.com" in host and ("/directory_" in path or "/search" in path):
        return SEARCH_RESULT_SHELL

    # Strong product-detail patterns
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
        )
    )
    # dieselpartsdirect.com/{mpn} style
    if host.endswith("dieselpartsdirect.com") and path.strip("/").count("/") == 0 and len(path.strip("/")) >= 4:
        productish = True
    # rockauto parts path
    if "rockauto.com" in host and "/parts/" in path:
        productish = True
    # summitracing /parts/
    if "summitracing.com" in host and "/parts/" in path:
        productish = True
    # fluke product
    if "fluke.com" in host and "/product/" in path:
        productish = True
    # bradyid product-ish path with digits
    if "bradyid.com" in host and "/tags/" in path:
        productish = True
    # crcautocare product
    if "crcautocare.com" in host and "/product/" in path:
        productish = True
    # platt /p/
    if "platt.com" in host and "/p/" in path:
        productish = True
    # parts-hvac html product
    if "parts-hvac.com" in host and path.endswith(".html"):
        productish = True
    # quill cbs product
    if "quill.com" in host and "/cbs/" in path:
        productish = True
    # globalindustrial /p/
    if "globalindustrial.com" in host and "/p/" in path:
        productish = True
    # leviton products
    if "leviton.com" in host and "/products/" in path:
        productish = True
    # toolbarn product
    if "toolbarn.com" in host and path.count("-") >= 1 and "/s/" not in path:
        productish = True
    # toolnut shopify-style /products/
    if "toolnut.com" in host and "/products/" in path:
        productish = True
    # amazon dp
    if "amazon." in host and "/dp/" in path:
        productish = True
    # motion.com sku pages
    if "motion.com" in host and "/products/sku/" in path:
        productish = True
    # channellock product
    if "channellock.com" in host and re.search(r"/\d+", path):
        productish = True
    # mccoys shop/p
    if "mccoys.com" in host and "/shop/p/" in path:
        productish = True
    # autozone /p/
    if "autozone.com" in host and "/p/" in path:
        productish = True
    # brother-usa /p/
    if "brother-usa.com" in host and "/p/" in path:
        productish = True

    if productish:
        if mpn:
            tok = re.sub(r"[^A-Za-z0-9]", "", mpn).lower()
            # Match within individual path segments so UPC prefixes don't poison MPN hits
            # e.g. .../078477231951/5320-s should verify 5320-S
            segs = [re.sub(r"[^A-Za-z0-9]", "", s).lower() for s in path.split("/") if s]
            segs.append(re.sub(r"[^A-Za-z0-9]", "", qs).lower())
            for seg in segs:
                if not tok or len(tok) < 3 or tok not in seg:
                    continue
                idx = seg.find(tok)
                after = seg[idx + len(tok) : idx + len(tok) + 1]
                before = seg[idx - 1 : idx] if idx > 0 else ""
                # Guard digit-run extensions inside the same segment (117 vs 1170)
                if after.isdigit() and tok[-1].isdigit():
                    continue
                if before.isdigit() and tok[0].isdigit():
                    continue
                return EXACT_PRODUCT_VERIFIED
            # path product pattern without mpn still unverified until page check
            return EXACT_PRODUCT_UNVERIFIED
        return EXACT_PRODUCT_UNVERIFIED

    if any(x in path for x in ("/category", "/c/", "/collections/")):
        return CATEGORY_PAGE
    if "family" in path:
        return PRODUCT_FAMILY_PAGE
    return EXACT_PRODUCT_UNVERIFIED
