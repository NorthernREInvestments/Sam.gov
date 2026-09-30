"""Phase L.2.2 — exact product-page resolution + identity-verified price evidence.

Extends L.2.1 market_price: search/listing → product URL → verify → price.
Search/category/homepage pages are discovery only — never economics evidence.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

from application_clock import now_utc
from phase_l.market_price import (
    CONDITION_OPEN_BOX,
    CONDITION_REFURB,
    CONDITION_USED,
    classify_condition,
    classify_seller_type,
    extract_prices_from_page,
    normalize_mpn_variants,
)

# Page types
EXACT_PRODUCT_PAGE = "EXACT_PRODUCT_PAGE"
LIKELY_PRODUCT_PAGE = "LIKELY_PRODUCT_PAGE"
SEARCH_RESULTS_PAGE = "SEARCH_RESULTS_PAGE"
CATEGORY_PAGE = "CATEGORY_PAGE"
MANUFACTURER_INFO_PAGE = "MANUFACTURER_INFO_PAGE"
PDF_PRICE_LIST = "PDF_PRICE_LIST"
CONTRACT_CATALOG = "CONTRACT_CATALOG"
MARKETPLACE_LISTING = "MARKETPLACE_LISTING"
HOMEPAGE = "HOMEPAGE"
UNRELATED = "UNRELATED"
UNKNOWN_PAGE = "UNKNOWN"

# Identity match
EXACT_MPN = "EXACT_MPN"
EXACT_ALT_MPN = "EXACT_ALT_MPN"
EXACT_MODEL = "EXACT_MODEL"
EXACT_SKU = "EXACT_SKU"
EXACT_NSN_PRODUCT = "EXACT_NSN_PRODUCT"
STRONG_TITLE_MATCH = "STRONG_TITLE_MATCH"
PARTIAL_MATCH = "PARTIAL_MATCH"
CONFLICT = "CONFLICT"
NO_MATCH = "NO_MATCH"

# Configuration
CFG_EXACT = "EXACT_CONFIGURATION"
CFG_STRONG = "STRONG_CONFIGURATION_MATCH"
CFG_APPROX = "APPROXIMATE_CONFIGURATION"
CFG_CONFLICT = "CONFIGURATION_CONFLICT"
CFG_UNKNOWN = "UNKNOWN_CONFIGURATION"

# Price confidence (L.2.2)
EXACT_VERIFIED = "EXACT_VERIFIED"
STRONG_VERIFIED = "STRONG_VERIFIED"
APPROXIMATE = "APPROXIMATE"
WEAK = "WEAK"
REJECTED = "REJECTED"

_REJECT_PATH = re.compile(
    r"/(cart|checkout|account|login|signin|register|help|support|faq|privacy|"
    r"terms|wishlist|compare|blog|news|about|contact)(/|$|\?)",
    re.I,
)
_SEARCH_PATH = re.compile(
    r"/(search|find|results|catalogsearch|p/pl)(/|$|\?)|[?&](q|query|keyword|searchQuery|Ntt|searchterm|d)=",
    re.I,
)
_CATEGORY_PATH = re.compile(
    r"/(category|categories|c/|shop/all|collections?|department)(/|$|\?)",
    re.I,
)
_PRODUCT_PATH = re.compile(
    r"/(product|products|p/|dp/|item|sku|part|buy|pd/|ip/)(/|$|\?)|"
    r"/[A-Za-z0-9_-]{6,}\.html$",
    re.I,
)
_NOISE_PRICE_CTX = re.compile(
    r"\b(shipping|free\s+ship|finance|mo\.|per\s+month|/mo|save\s+\$|you\s+save|"
    r"warranty|protection\s+plan|accessory|accessories|starting\s+at|"
    r"as\s+low\s+as|deposit|tax\s+est)\b",
    re.I,
)


def _utc() -> str:
    return now_utc().isoformat()


def _norm_token(s: str | None) -> str:
    if not s:
        return ""
    return re.sub(r"[^A-Z0-9]+", "", str(s).upper())


def normalize_url(url: str, *, base: str | None = None) -> str | None:
    if not url:
        return None
    u = url.strip()
    if u.startswith("//"):
        u = "https:" + u
    if base and u.startswith("/"):
        u = urljoin(base, u)
    if not u.startswith("http"):
        return None
    try:
        p = urlparse(u)
        # strip fragment + common tracking
        q = "&".join(
            part
            for part in (p.query or "").split("&")
            if part and not part.lower().startswith(("utm_", "gclid", "fbclid", "ref="))
        )
        clean = urlunparse((p.scheme, p.netloc.lower(), p.path.rstrip("/") or "/", "", q, ""))
        return clean
    except Exception:
        return None


def classify_page_type(url: str, html: str | None = None) -> str:
    u = (url or "").lower()
    path = urlparse(u).path or "/"
    text = (html or "")[:12000].lower()

    if u.endswith(".pdf") or "application/pdf" in text[:500]:
        if any(x in text for x in ("contract", "schedule", "gsa", "sourcewell", "omnia")):
            return CONTRACT_CATALOG
        return PDF_PRICE_LIST

    host = urlparse(u).netloc
    if path in {"", "/"} or path.count("/") <= 1 and len(path) < 3:
        if not _PRODUCT_PATH.search(u):
            return HOMEPAGE

    if _REJECT_PATH.search(u):
        return UNRELATED
    if _SEARCH_PATH.search(u) or "search results" in text[:2000] or 'id="search"' in text[:3000]:
        # listing with many products
        if text.count("$") > 15 and text.count("href") > 40:
            return SEARCH_RESULTS_PAGE
        return SEARCH_RESULTS_PAGE
    if _CATEGORY_PATH.search(u):
        return CATEGORY_PAGE

    if any(x in host for x in ("amazon.", "ebay.", "walmart.")):
        if _PRODUCT_PATH.search(u) or "/dp/" in u:
            return MARKETPLACE_LISTING
        return SEARCH_RESULTS_PAGE

    # Structured product signals
    if html:
        if re.search(r'"@type"\s*:\s*"Product"', html, re.I) or 'itemtype="http://schema.org/Product"' in text:
            return EXACT_PRODUCT_PAGE if _PRODUCT_PATH.search(u) or "sku" in text[:8000] else LIKELY_PRODUCT_PAGE
        if "og:type\" content=\"product" in text or "property=\"og:type\" content='product" in text:
            return LIKELY_PRODUCT_PAGE

    if _PRODUCT_PATH.search(u):
        return LIKELY_PRODUCT_PAGE
    if "datasheet" in u or "specification" in u:
        return MANUFACTURER_INFO_PAGE
    return UNKNOWN_PAGE


def page_type_economics_eligible(page_type: str) -> bool:
    return page_type in {
        EXACT_PRODUCT_PAGE,
        LIKELY_PRODUCT_PAGE,
        PDF_PRICE_LIST,
        CONTRACT_CATALOG,
        MARKETPLACE_LISTING,  # conditional — still needs identity verify
    }


def extract_product_candidate_links(
    html: str,
    *,
    base_url: str,
    search_id: dict[str, Any],
    limit: int = 12,
) -> list[dict[str, Any]]:
    """From a search/listing page, extract likely product URLs ranked by identity hints."""
    if not html:
        return []
    keys = []
    for k in (
        search_id.get("primary_mpn"),
        search_id.get("model"),
        search_id.get("sku"),
        *(search_id.get("mpn_variants") or [])[:4],
        *(search_id.get("alternate_mpns") or [])[:3],
    ):
        n = _norm_token(k)
        if n and n not in keys:
            keys.append(n)
    mfr = _norm_token(search_id.get("manufacturer"))

    # (url, nearby_text) pairs
    pairs: list[tuple[str, str]] = []
    for m in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        re.I | re.S,
    ):
        block = m.group(1)
        for um in re.finditer(r'"url"\s*:\s*"(https?://[^"]+)"', block):
            pairs.append((um.group(1), block[:400]))

    cm = re.search(r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)["\']', html, re.I)
    if cm:
        pairs.append((cm.group(1), "canonical"))

    for m in re.finditer(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html, re.I | re.S):
        href = m.group(1)
        inner = re.sub(r"<[^>]+>", " ", m.group(2))[:160]
        pairs.append((href, inner))

    for m in re.finditer(
        r'(?:data-(?:product-)?url|data-href|data-item-url)\s*=\s*["\']([^"\']+)["\']',
        html,
        re.I,
    ):
        pairs.append((m.group(1), "data-url"))

    for k_raw in (
        search_id.get("primary_mpn"),
        search_id.get("model"),
        search_id.get("sku"),
        *(search_id.get("mpn_variants") or [])[:3],
    ):
        if not k_raw or len(str(k_raw)) < 3:
            continue
        esc = re.escape(str(k_raw))
        for m in re.finditer(rf'["\']((?:https?:)?/?/?[^"\']*{esc}[^"\']*)["\']', html, re.I):
            pairs.append((m.group(1), str(k_raw)))

    for m in re.finditer(
        r'["\']((?:https?://[^"\']+)?/(?:product|products|p|dp|ip|item|sku|part)/[^"\']{3,160})["\']',
        html,
        re.I,
    ):
        pairs.append((m.group(1), "product_path"))

    scored: list[tuple[float, str]] = []
    seen: set[str] = set()
    for href, nearby in pairs:
        nu = normalize_url(href, base=base_url)
        if not nu or nu in seen:
            continue
        seen.add(nu)
        low = nu.lower()
        if _REJECT_PATH.search(low):
            continue
        if re.search(r"(accessory|accessories|warranty|cable|case|mount|adapter)", low):
            if not keys or not any(k in _norm_token(low + nearby) for k in keys):
                continue
        page_guess = classify_page_type(nu)
        if page_guess in {HOMEPAGE, SEARCH_RESULTS_PAGE, CATEGORY_PAGE, UNRELATED}:
            if not _PRODUCT_PATH.search(low) and not any(k in _norm_token(low) for k in keys):
                continue
        score = 0.0
        path_norm = _norm_token(low)
        near_norm = _norm_token(nearby)
        for k in keys:
            if k and k in path_norm:
                score += 50
            if k and k in near_norm:
                score += 25
        if mfr and (mfr in path_norm or mfr in near_norm):
            score += 8
        if _PRODUCT_PATH.search(low):
            score += 20
        if any(x in low for x in ("/dp/", "/product/", "/p/", "item/", "sku", "/ip/")):
            score += 15
        if keys and not any(k in path_norm or k in near_norm for k in keys):
            if score < 30:
                continue
            score -= 10
        if score <= 0 and not keys:
            score = 5 if _PRODUCT_PATH.search(low) else 0
        if score > 0:
            scored.append((score, nu))

    scored.sort(key=lambda x: -x[0])
    out = []
    for score, u in scored[:limit]:
        out.append({"url": u, "score": score, "source": "listing_extract"})
    return out


def verify_product_identity_on_page(
    html: str,
    *,
    search_id: dict[str, Any],
    url: str = "",
) -> dict[str, Any]:
    """Deterministic identity match against page text."""
    blob = f"{url}\n{html or ''}"
    blob_norm = _norm_token(blob[:200000])
    title_m = re.search(r"<title[^>]*>([^<]{3,200})</title>", html or "", re.I)
    title = title_m.group(1).strip() if title_m else ""

    target_mpn = search_id.get("primary_mpn")
    variants = list(search_id.get("mpn_variants") or normalize_mpn_variants(target_mpn))
    alts = list(search_id.get("alternate_mpns") or [])
    model = search_id.get("model")
    sku = search_id.get("sku")
    nsn = search_id.get("nsn")
    mfr = search_id.get("manufacturer")

    mfr_match = bool(mfr and _norm_token(mfr) in blob_norm)
    matched_text = None
    match_level = NO_MATCH
    confidence = 0.0

    def find_variant(options: list[str]) -> str | None:
        for v in options:
            nv = _norm_token(v)
            if len(nv) >= 3 and nv in blob_norm:
                return v
        return None

    hit = find_variant(variants)
    mpn_expected = bool(target_mpn and _norm_token(target_mpn))
    mpn_found = bool(hit)

    if hit and target_mpn and _norm_token(hit) == _norm_token(target_mpn):
        match_level = EXACT_MPN
        matched_text = hit
        confidence = 0.97
    elif hit and any(_norm_token(hit) == _norm_token(a) for a in alts):
        match_level = EXACT_ALT_MPN
        matched_text = hit
        confidence = 0.93
    elif hit:
        match_level = EXACT_MPN
        matched_text = hit
        confidence = 0.95
    elif mpn_expected and not mpn_found:
        # Hard rule: when an exact MPN was requested and is absent, do not
        # promote short model/title matches (prevents F110 switch → Getac F110).
        title_toks = [t for t in re.findall(r"[A-Za-z0-9]{4,}", (search_id.get("product_title") or "").lower())[:6]]
        hits = sum(1 for t in title_toks if t in (title or html or "")[:3000].lower())
        if mfr_match and hits >= 3 and model and len(_norm_token(model)) >= 6 and _norm_token(model) in blob_norm:
            match_level = PARTIAL_MATCH
            matched_text = str(model)
            confidence = 0.45
        else:
            match_level = NO_MATCH
            confidence = 0.1
            matched_text = None
    elif model and _norm_token(model) in blob_norm:
        model_norm = _norm_token(model)
        # Short model tokens require manufacturer match
        if len(model_norm) < 6 and not mfr_match:
            match_level = PARTIAL_MATCH
            matched_text = str(model)
            confidence = 0.35
        elif len(model_norm) < 6 and mfr_match:
            match_level = EXACT_MODEL
            matched_text = str(model)
            confidence = 0.82
        else:
            match_level = EXACT_MODEL
            matched_text = str(model)
            confidence = 0.88 if mfr_match else 0.75
    elif sku and str(sku).upper() not in {"UNKNOWN", ""} and _norm_token(sku) in blob_norm:
        match_level = EXACT_SKU
        matched_text = str(sku)
        confidence = 0.9
    elif nsn and _norm_token(nsn) in blob_norm and mfr_match:
        match_level = EXACT_NSN_PRODUCT
        matched_text = str(nsn)
        confidence = 0.85
    else:
        title_toks = [t for t in re.findall(r"[A-Za-z0-9]{4,}", (search_id.get("product_title") or "").lower())[:6]]
        hits = sum(1 for t in title_toks if t in (title or html or "")[:3000].lower())
        if mfr_match and hits >= 2:
            match_level = STRONG_TITLE_MATCH
            matched_text = title[:80]
            confidence = 0.7
        elif mfr_match or hits >= 1:
            match_level = PARTIAL_MATCH
            matched_text = title[:80] or None
            confidence = 0.4

    if model and match_level == PARTIAL_MATCH:
        om = re.search(r"\bModel\s*[:#]?\s*([A-Z0-9][A-Z0-9\-_/]{2,})", html or "", re.I)
        if om and _norm_token(om.group(1)) != _norm_token(model) and _norm_token(model) not in blob_norm:
            match_level = CONFLICT
            confidence = 0.2
            matched_text = om.group(1)

    return {
        "match_level": match_level,
        "target_mpn": target_mpn,
        "matched_text": matched_text,
        "manufacturer_match": mfr_match,
        "model_match": bool(model and _norm_token(model) in blob_norm),
        "confidence": confidence,
        "page_title": title[:160] if title else None,
        "mpn_expected": mpn_expected,
        "mpn_found": mpn_found,
    }


def classify_configuration_match(
    html: str,
    *,
    row: dict[str, Any] | None = None,
) -> str:
    """Lightweight config gate — CONFLICT only when explicit contradiction found."""
    row = row or {}
    blob = (html or "")[:30000].lower()
    req = " ".join(
        str(x)
        for x in (row.get("description"), row.get("title"), row.get("required_specs"))
        if x
    ).lower()
    # RAM conflict example
    ram_req = re.search(r"(\d+)\s*gb\s*(ram|memory)", req)
    ram_page = re.search(r"(\d+)\s*gb\s*(ram|memory|ddr)", blob)
    if ram_req and ram_page and ram_req.group(1) != ram_page.group(1):
        return CFG_CONFLICT
    storage_req = re.search(r"(\d+)\s*(tb|gb)\s*(ssd|hdd|storage)", req)
    storage_page = re.search(r"(\d+)\s*(tb|gb)\s*(ssd|hdd|nvme)", blob)
    if storage_req and storage_page:
        if storage_req.group(1) != storage_page.group(1) or storage_req.group(2) != storage_page.group(2):
            return CFG_CONFLICT
    if ram_req or storage_req:
        return CFG_STRONG if (ram_page or storage_page) else CFG_UNKNOWN
    return CFG_UNKNOWN


def is_price_noise(price: float, context: str, *, extraction_method: str) -> bool:
    if price <= 0:
        return True
    if price < 5 and extraction_method in {"html_text", "TEXT", "TEXT_WEAK"}:
        return True
    if _NOISE_PRICE_CTX.search(context or ""):
        return True
    if re.search(r"function\s*\(|getTime|\\\\\$1", context or ""):
        return True
    return False


def build_market_price_evidence(
    *,
    source_url: str,
    page_type: str,
    identity_match: dict[str, Any],
    configuration_match: str,
    condition: str,
    price: float,
    currency: str = "USD",
    extraction_method: str,
    search_id: dict[str, Any],
    evidence_text: str | None = None,
    seller: str | None = None,
    availability: str | None = None,
) -> dict[str, Any]:
    match_level = identity_match.get("match_level")
    rejection = None
    confidence = WEAK
    economics_eligible = False

    if match_level in {CONFLICT, NO_MATCH, PARTIAL_MATCH}:
        confidence = REJECTED
        rejection = f"identity_{match_level}"
    elif configuration_match == CFG_CONFLICT:
        confidence = REJECTED
        rejection = "CONFIGURATION_MISMATCH"
    elif condition in {CONDITION_USED, CONDITION_REFURB, CONDITION_OPEN_BOX, "OPEN_BOX"}:
        confidence = REJECTED
        rejection = "USED_ONLY"
    elif page_type in {SEARCH_RESULTS_PAGE, CATEGORY_PAGE, HOMEPAGE, UNRELATED}:
        confidence = REJECTED
        rejection = "SEARCH_PAGE_ONLY"
    elif match_level == EXACT_MPN and page_type in {
        EXACT_PRODUCT_PAGE,
        LIKELY_PRODUCT_PAGE,
        PDF_PRICE_LIST,
        CONTRACT_CATALOG,
    }:
        confidence = EXACT_VERIFIED
        economics_eligible = True
    elif match_level in {EXACT_ALT_MPN, EXACT_SKU, EXACT_MODEL, EXACT_NSN_PRODUCT} and page_type_economics_eligible(
        page_type
    ):
        confidence = STRONG_VERIFIED
        economics_eligible = configuration_match != CFG_APPROX
    elif match_level == STRONG_TITLE_MATCH and page_type_economics_eligible(page_type):
        confidence = APPROXIMATE
        economics_eligible = False
        rejection = "APPROXIMATE_NOT_ECONOMICS"
    else:
        confidence = WEAK
        rejection = "PRICE_TOO_WEAK"

    if is_price_noise(price, evidence_text or "", extraction_method=extraction_method):
        confidence = REJECTED
        economics_eligible = False
        rejection = "PRICE_NOISE"

    return {
        "kind": "MarketPriceEvidence",
        "build": "20260926-m3-phase-l22-exact-product-page-resolution",
        "source_url": source_url,
        "seller": seller,
        "domain": urlparse(source_url).netloc if source_url else None,
        "page_type": page_type,
        "product_match_level": match_level,
        "configuration_match": configuration_match,
        "condition": condition,
        "price": price,
        "currency": currency,
        "price_unit": "EA",
        "availability": availability or "UNKNOWN",
        "extraction_method": extraction_method,
        "target_mpn": search_id.get("primary_mpn"),
        "observed_mpn": identity_match.get("matched_text") if match_level in {EXACT_MPN, EXACT_ALT_MPN} else None,
        "target_model": search_id.get("model"),
        "observed_model": identity_match.get("matched_text") if match_level == EXACT_MODEL else None,
        "evidence_text": (evidence_text or "")[:240],
        "fetched_at": _utc(),
        "confidence": confidence,
        "economics_eligible": economics_eligible,
        "rejection_reason": rejection,
        "identity_match": identity_match,
        "seller_type": classify_seller_type(source_url, seller),
    }


def resolve_and_verify_market_price(
    *,
    html: str,
    url: str,
    search_id: dict[str, Any],
    row: dict[str, Any] | None = None,
    from_listing: bool = False,
) -> dict[str, Any]:
    """Classify page, optionally extract product links, verify identity, extract prices."""
    page_type = classify_page_type(url, html)
    result: dict[str, Any] = {
        "url": url,
        "page_type": page_type,
        "candidate_links": [],
        "evidence": [],
        "drop_reason": None,
    }

    if page_type in {SEARCH_RESULTS_PAGE, CATEGORY_PAGE, HOMEPAGE}:
        links = extract_product_candidate_links(html, base_url=url, search_id=search_id)
        result["candidate_links"] = links
        result["drop_reason"] = "SEARCH_PAGE_ONLY"
        return result

    if page_type == UNRELATED:
        result["drop_reason"] = "UNRELATED"
        return result

    identity = verify_product_identity_on_page(html, search_id=search_id, url=url)
    result["identity_match"] = identity
    if identity["match_level"] in {NO_MATCH, CONFLICT, PARTIAL_MATCH}:
        result["drop_reason"] = (
            "PRODUCT_IDENTITY_MISMATCH"
            if identity["match_level"] != CONFLICT
            else "PRODUCT_IDENTITY_MISMATCH"
        )
        return result

    cfg = classify_configuration_match(html, row=row)
    result["configuration_match"] = cfg
    if cfg == CFG_CONFLICT:
        result["drop_reason"] = "CONFIGURATION_MISMATCH"
        return result

    hint = " ".join(
        str(x)
        for x in (search_id.get("primary_mpn"), search_id.get("model"), search_id.get("manufacturer"))
        if x
    )
    raw_prices = extract_prices_from_page(html, source_url=url, product_hint=hint)
    if not raw_prices:
        result["drop_reason"] = "NO_PRICE_ON_MATCHING_PAGE"
        return result

    evidence_list = []
    for p in raw_prices:
        price = float(p["unit_price"])
        cond = p.get("condition") or classify_condition(str(p.get("context_snippet") or html[:2000]))
        ev = build_market_price_evidence(
            source_url=url,
            page_type=page_type if page_type != UNKNOWN_PAGE else LIKELY_PRODUCT_PAGE,
            identity_match=identity,
            configuration_match=cfg,
            condition=cond,
            price=price,
            extraction_method=str(p.get("source_type") or p.get("exact_match") or "html"),
            search_id=search_id,
            evidence_text=p.get("context_snippet"),
            seller=p.get("seller"),
        )
        # Promote page type when identity exact on likely page
        if identity["match_level"] == EXACT_MPN and page_type == LIKELY_PRODUCT_PAGE:
            ev["page_type"] = EXACT_PRODUCT_PAGE
            if ev["confidence"] == STRONG_VERIFIED:
                ev["confidence"] = EXACT_VERIFIED
                ev["economics_eligible"] = True
                ev["rejection_reason"] = None
        evidence_list.append(ev)

    result["evidence"] = evidence_list
    eligible = [e for e in evidence_list if e.get("economics_eligible")]
    if not eligible:
        # keep best rejected reason
        result["drop_reason"] = evidence_list[0].get("rejection_reason") if evidence_list else "PRICE_TOO_WEAK"
    return result
