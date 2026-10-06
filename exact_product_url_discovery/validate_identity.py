"""Page-content identity validation (no price required)."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from exact_product_url_discovery.classify import classify_candidate_url, is_search_shell
from exact_product_url_discovery.models import (
    EXACT_PRODUCT_UNVERIFIED,
    EXACT_PRODUCT_VERIFIED,
    PAGE_CACHE,
    REJECTED,
)
from exact_product_url_discovery.normalize import is_short_mpn, mpn_in_text, norm_token
from m3_data_root import data_path
from public_price_search.search import fetch_page


def _cache_get(url: str) -> dict[str, Any] | None:
    p = data_path(PAGE_CACHE)
    if not p.exists():
        return None
    try:
        cache = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    key = hashlib.sha1(url.encode()).hexdigest()
    return (cache.get("pages") or {}).get(key)


def _cache_put(url: str, payload: dict[str, Any]) -> None:
    p = data_path(PAGE_CACHE)
    cache: dict[str, Any] = {"pages": {}}
    if p.exists():
        try:
            cache = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            cache = {"pages": {}}
    key = hashlib.sha1(url.encode()).hexdigest()
    html = payload.get("html") or ""
    cache.setdefault("pages", {})[key] = {
        "url": url,
        "ok": payload.get("ok"),
        "status_code": payload.get("status_code"),
        "final_url": payload.get("final_url") or url,
        "via": payload.get("via"),
        "html": html[:350000],
    }
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        p.write_text(json.dumps(cache, default=str), encoding="utf-8")
    except Exception:
        pass


def _title(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html[:12000] if html else "", re.I | re.S)
    return re.sub(r"<[^>]+>", "", m.group(1) if m else "").strip()


def _jsonld_identity(html: str) -> list[dict[str, str]]:
    out = []
    for m in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html or "",
        re.I | re.S,
    ):
        try:
            data = json.loads(m.group(1))
        except Exception:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                t = str(node.get("@type") or "")
                if "Product" in t or node.get("sku") or node.get("mpn"):
                    out.append(
                        {
                            "name": str(node.get("name") or "")[:200],
                            "sku": str(node.get("sku") or node.get("mpn") or node.get("productID") or ""),
                            "brand": str(
                                (node.get("brand") or {}).get("name")
                                if isinstance(node.get("brand"), dict)
                                else node.get("brand") or ""
                            ),
                        }
                    )
                for v in node.values():
                    if isinstance(v, (dict, list)):
                        stack.append(v)
            elif isinstance(node, list):
                stack.extend(node[:40])
    return out


_AUTH_TITLE_RE = re.compile(
    r"^(register|login|sign\s*in|sign\s*up|create\s+account|my\s+account|cart|checkout|"
    r"password|access\s+denied|page\s+not\s+found|404|product\s+not\s+found|"
    r"not\s+found|just\s+a\s+moment|attention\s+required|access\s+denied)\b",
    re.I,
)
_GENERIC_TITLE_RE = re.compile(
    r"^(products?|product\s+catalog|catalog|search|home|shop|store|welcome|"
    r"all\s+products|results?)\b",
    re.I,
)
_NOT_FOUND_RE = re.compile(
    r"product\s+not\s+found|page\s+not\s+found|\b404\b|no\s+longer\s+available|"
    r"item\s+not\s+found|we\s+couldn.?t\s+find|sorry.?,\s+this\s+page",
    re.I,
)


def _url_path_only(url: str) -> str:
    from urllib.parse import urlparse

    try:
        return urlparse(url).path or ""
    except Exception:
        return url


def _has_product_page_signal(html: str, title: str, ld: list[dict[str, str]]) -> bool:
    if ld:
        return True
    head = f"{title} {(html or '')[:100000]}"
    if re.search(
        r'"@type"\s*:\s*"Product"|itemtype=["\'][^"\']*Product["\']|og:type["\'\s]+content=["\']product',
        head,
        re.I,
    ):
        return True
    if re.search(
        r"add[\s_-]?to[\s_-]?cart|buy\s+now|add[\s_-]?to[\s_-]?bag|product:price|sku["
        r"\'\s:=]|item_id|product_id|data-product",
        head,
        re.I,
    ):
        return True
    if re.search(r'"price"\s*:|product:price:amount|itemprop=["\']price["\']', head, re.I):
        return True
    return False


def _h1(html: str) -> str:
    m = re.search(r"<h1[^>]*>(.*?)</h1>", html[:40000] if html else "", re.I | re.S)
    return re.sub(r"<[^>]+>", " ", m.group(1) if m else "").strip()


def validate_product_page(
    url: str,
    *,
    mpn: str,
    manufacturer: str | None = None,
    description: str | None = None,
    category: str | None = None,
    allow_browser: bool = False,
) -> dict[str, Any]:
    """Validate exact product identity on a live page. No price required."""
    url_class = classify_candidate_url(url, mpn=mpn)
    out: dict[str, Any] = {
        "url": url,
        "url_class_structural": url_class,
        "ok": False,
        "identity_match": False,
        "reason": None,
        "title": None,
        "pack_hint": None,
        "uom_hint": None,
        "manufacturer_match": False,
        "mpn_match": False,
    }
    if url_class in REJECTED:
        out["reason"] = f"rejected_structural:{url_class}"
        return out

    cached = _cache_get(url)
    if cached is not None and (cached.get("blocked") or not cached.get("ok")) and not cached.get("via"):
        # Prior HTTP failure — allow one browser retry for product candidates
        if allow_browser:
            cached = None
    if cached is None:
        fr = fetch_page(url)
        html_text = fr.get("text") or ""
        ok = bool(fr.get("ok"))
        blocked = bool(fr.get("blocked"))
        # Browser only when explicitly allowed (curated / high-confidence candidates)
        if allow_browser and (blocked or not ok) and url_class in {EXACT_PRODUCT_VERIFIED, EXACT_PRODUCT_UNVERIFIED}:
            try:
                from price_adapters.browser import bounded_browser_fetch

                br = bounded_browser_fetch(url, timeout_ms=12000, wait_ms=1200, capture_json=False)
                br_html = br.get("html") or br.get("text") or ""
                if br.get("ok") and len(br_html) >= 1500:
                    html_text = br_html
                    ok = True
                    blocked = False
                    fr = {
                        "ok": True,
                        "status_code": 200,
                        "text": html_text,
                        "url": br.get("url") or url,
                        "blocked": False,
                        "via": "BROWSER_ASSISTED",
                    }
            except Exception:
                pass
        cached = {
            "ok": ok,
            "status_code": fr.get("status_code"),
            "html": html_text,
            "blocked": blocked,
            "final_url": fr.get("url") or url,
            "via": fr.get("via"),
        }
        _cache_put(url, cached)
    if cached.get("blocked") or not cached.get("ok"):
        out["reason"] = "fetch_blocked_or_failed"
        out["status_code"] = cached.get("status_code")
        return out

    html = cached.get("html") or ""
    title = _title(html)
    h1 = _h1(html)
    out["title"] = title[:200]
    if _AUTH_TITLE_RE.search((title or "").strip()) or _NOT_FOUND_RE.search(title or ""):
        out["reason"] = "auth_or_non_product_title"
        return out
    if _GENERIC_TITLE_RE.search((title or "").strip()):
        out["reason"] = "generic_catalog_title"
        return out
    if _NOT_FOUND_RE.search(h1 or "") or _NOT_FOUND_RE.search((html or "")[:3000]):
        out["reason"] = "product_not_found_page"
        return out

    # Redirect to search shell is fatal; other redirects validate against final content
    final_url = str(cached.get("final_url") or url)
    if final_url and is_search_shell(final_url):
        out["reason"] = "redirected_away_from_candidate"
        return out

    ld = _jsonld_identity(html)
    blob_head = f"{title} {h1} {html[:25000]}"
    path_only = _url_path_only(final_url or url)
    short = is_short_mpn(mpn)

    # Surface MPN required — body-only matches on catalog shells are false positives
    mpn_strong = mpn_in_text(mpn, title, h1) or any(
        mpn_in_text(mpn, row.get("name") or "", row.get("sku") or "") for row in ld
    )
    mpn_path = mpn_in_text(mpn, path_only)
    mpn_ok = bool(mpn_strong)
    if not mpn_ok and mpn_path and mpn_in_text(mpn, title):
        mpn_ok = True
    out["mpn_match"] = mpn_ok
    if not mpn_ok:
        out["reason"] = "mpn_not_on_page"
        return out

    if not _has_product_page_signal(html, title, ld):
        out["reason"] = "no_product_page_signal"
        return out

    mfr_ok = True
    if manufacturer:
        mfr0 = manufacturer.split()[0]
        mfr_tok = norm_token(mfr0)
        surface = f"{title} {h1} " + " ".join(f"{r.get('brand')} {r.get('name')}" for r in ld[:5])
        surface_tok = norm_token(surface)
        mfr_ok = bool(mfr_tok and mfr_tok in surface_tok) or any(
            mfr0.lower() in (r.get("brand") or "").lower() or mfr0.lower() in (r.get("name") or "").lower() for r in ld
        )
        host = ""
        try:
            from urllib.parse import urlparse as _up

            host = (_up(url).netloc or "").lower().replace("www.", "").replace("-", "").replace(".", "")
        except Exception:
            host = ""
        if not mfr_ok and mfr_tok and mfr_tok.lower() in host and mpn_strong:
            mfr_ok = True
        if not mfr_ok:
            out["reason"] = "manufacturer_missing_short_mpn" if short else "manufacturer_mismatch"
            out["manufacturer_match"] = False
            return out
    out["manufacturer_match"] = mfr_ok

    # Pack hints (capture only)
    pack_m = re.search(r"\b(pack of|case of|box of)\s*(\d+)\b", blob_head, re.I)
    if pack_m:
        out["pack_hint"] = pack_m.group(0)
    if re.search(r"\bgallon\b|\b128\s*fl\b", blob_head, re.I):
        out["pack_hint"] = (out.get("pack_hint") or "") + ";gallon"
    uom_m = re.search(r"\b(EA|EACH|PAIR|SET|QT|GAL|OZ)\b", blob_head, re.I)
    if uom_m:
        out["uom_hint"] = uom_m.group(1).upper()

    if short and description:
        toks = [t for t in re.findall(r"[A-Za-z]{4,}", description.lower()) if t not in {"with", "from", "this"}]
        if toks:
            hit = sum(1 for t in toks[:6] if t in blob_head.lower())
            if hit == 0:
                if category and category.lower() in blob_head.lower():
                    pass
                else:
                    out["reason"] = "description_category_mismatch_short_mpn"
                    return out

    out["ok"] = True
    out["identity_match"] = True
    out["url_class"] = EXACT_PRODUCT_VERIFIED
    out["reason"] = "identity_validated"
    out["via"] = cached.get("via")
    out["price_extractability"] = (
        "LIKELY"
        if re.search(r'"price"\s*:|product:price:amount|itemprop=["\']price["\']', html[:80000], re.I)
        else "UNKNOWN"
    )
    return out
