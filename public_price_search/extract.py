"""Price / condition / core extraction from HTML and snippets."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

from public_price_search.models import (
    CONDITION_NEW,
    CONDITION_RECONDITIONED,
    CONDITION_REMANUFACTURED,
    CONDITION_UNKNOWN,
    CONDITION_USED,
    SELLER_DISTRIBUTOR,
    SELLER_MANUFACTURER,
    SELLER_MARKETPLACE,
    SELLER_RESELLER,
    SELLER_SNIPPET,
)

_PRICE_DOLLAR = re.compile(
    r"(?:\$|USD\s*)(\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\d+\.\d{2})"
)
_JSON_PRICE_KEYS = re.compile(
    r'"(?:productPrice|price|unitPrice|listPrice|salePrice|amount)"\s*:\s*"?(\d+(?:\.\d{1,2})?)"?',
    re.I,
)
_CORE_RE = re.compile(
    r"core(?:\s+charge|\s+deposit|\s+price)?[^$0-9]{0,40}(?:\$|USD\s*)(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)",
    re.I,
)
_FREE_SHIP = re.compile(r"free\s+shipping(?:\s+on\s+orders\s+over\s*\$?\s*(\d+(?:\.\d{2})?))?", re.I)


def _f(s: str) -> float | None:
    try:
        v = float(s.replace(",", ""))
    except (TypeError, ValueError):
        return None
    if 0.5 <= v <= 500_000:
        return v
    return None


def _norm_pn(s: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def identity_tokens(identity: dict[str, Any]) -> list[str]:
    toks = []
    for k in ("part_number", "catalog_number", "model", "sku"):
        t = identity.get(k)
        if t and len(str(t)) >= 3:
            toks.append(_norm_pn(str(t)))
    return [t for t in toks if t]


def identity_on_page(text: str, identity: dict[str, Any]) -> bool:
    blob = _norm_pn(text)
    return any(t and t in blob for t in identity_tokens(identity))


def detect_condition(text: str) -> str:
    t = (text or "").lower()
    # Prefer explicit product-condition phrases over incidental English ("used for")
    if re.search(
        r"\b(?:condition\s*[:=]\s*)?recon(?:ditioned)?\b|\breconditioned\s+(?:part|unit|injector|filter)\b",
        t,
    ):
        return CONDITION_RECONDITIONED
    if re.search(
        r"\breman(?:ufactured)?\b|\brem\b\s+fuel|\brem\b\s+injector|\bcondition\s*[:=]\s*reman",
        t,
    ):
        return CONDITION_REMANUFACTURED
    if re.search(
        r"\b(?:condition\s*[:=]\s*used|used\s+condition|pre-?owned|refurbished|"
        r"used\s+(?:part|unit|injector|filter|oem)|for\s+used\s+engines?)\b",
        t,
    ):
        return CONDITION_USED
    if re.search(
        r"\b(?:brand\s+new|new\s+oem|oem\s+new|condition\s*[:=]\s*new|"
        r"new\s+(?:part|unit|injector|filter)|genuine\s+new|\bnew\b)\b|\boe[mn]\b|\bgenuine\b",
        t,
    ):
        return CONDITION_NEW
    return CONDITION_UNKNOWN


def classify_seller(url: str, manufacturer: str | None = None) -> str:
    host = urlparse(url or "").netloc.lower()
    mfr = (manufacturer or "").lower()
    if any(x in host for x in ("cummins.com", "ford.com", "smith-blair.com", "cat.com", "deere.com")):
        return SELLER_MANUFACTURER
    if mfr and mfr.split()[0] in host:
        return SELLER_MANUFACTURER
    if any(
        x in host
        for x in (
            "grainger.com",
            "zoro.com",
            "mscdirect",
            "fastenal",
            "fleetpride",
            "alliantpower",
            "finditparts",
            "dieselpartsdirect",
            "thedieselstore",
            "advancedtruckparts",
            "bigrigworld",
        )
    ):
        return SELLER_DISTRIBUTOR
    if any(x in host for x in ("amazon.", "ebay.", "walmart.", "alibaba.")):
        return SELLER_MARKETPLACE
    if host:
        return SELLER_RESELLER
    return SELLER_SNIPPET


def extract_core_charge(text: str, *, displayed_price: float | None = None) -> tuple[float | None, bool | None]:
    m = _CORE_RE.search(text or "")
    if not m:
        return None, None
    amt = _f(m.group(1))
    if amt is None:
        return None, None
    # Core is almost never larger than the displayed part price
    if displayed_price and amt >= float(displayed_price) * 0.95:
        return None, None
    refundable = bool(re.search(r"refundable", text[max(0, m.start() - 40) : m.end() + 60], re.I))
    return amt, refundable


def extract_shipping(text: str) -> dict[str, Any]:
    out: dict[str, Any] = {
        "shipping_amount": None,
        "free_shipping_threshold": None,
        "shipping_unknown": True,
        "pickup_option": bool(re.search(r"\bpickup\b|\bstore\s+pickup\b", text or "", re.I)),
    }
    m = _FREE_SHIP.search(text or "")
    if m:
        out["shipping_unknown"] = False
        out["shipping_amount"] = 0.0
        if m.group(1):
            out["free_shipping_threshold"] = _f(m.group(1))
    ship_m = re.search(
        r"(?:shipping|delivery)[^$]{0,30}\$\s*(\d+(?:\.\d{2})?)",
        text or "",
        re.I,
    )
    if ship_m:
        out["shipping_amount"] = _f(ship_m.group(1))
        out["shipping_unknown"] = False
    return out


def _json_ld_prices(html: str) -> list[float]:
    vals: list[float] = []
    for m in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html or "",
        re.I | re.S,
    ):
        raw = m.group(1).strip()
        try:
            data = json.loads(raw)
        except Exception:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                offers = node.get("offers")
                if isinstance(offers, dict):
                    stack.append(offers)
                elif isinstance(offers, list):
                    stack.extend(offers)
                for k in ("price", "lowPrice", "highPrice"):
                    v = _f(str(node.get(k))) if node.get(k) is not None else None
                    if v:
                        vals.append(v)
                for v in node.values():
                    if isinstance(v, (dict, list)):
                        stack.append(v)
            elif isinstance(node, list):
                stack.extend(node)
    return vals


def extract_prices_from_html(html: str) -> list[float]:
    # Strong signals first — structured/hydration before DOM $ scrape
    preferred: list[float] = []
    try:
        from public_price_search.catalog_intel import extract_structured_prices

        for row in extract_structured_prices(html or ""):
            v = row.get("price")
            if v:
                preferred.append(float(v))
    except Exception:
        pass
    for m in re.finditer(r'"productPrice"\s*:\s*"([^"]+)"', html or "", re.I):
        v = _f(m.group(1))
        if v:
            preferred.append(v)
    for m in re.finditer(
        r'"offers"\s*:\s*\{[^}]{0,400}?"price"\s*:\s*"([^"]+)"',
        html or "",
        re.I | re.S,
    ):
        v = _f(m.group(1))
        if v:
            preferred.append(v)
    preferred.extend(_json_ld_prices(html))

    secondary: list[float] = []
    for m in _JSON_PRICE_KEYS.finditer(html or ""):
        v = _f(m.group(1))
        if v:
            secondary.append(v)
    for m in _PRICE_DOLLAR.finditer(html or ""):
        v = _f(m.group(1))
        if v:
            secondary.append(v)

    # If strong signals exist, ignore bare $-noise that often includes carts/totals
    vals = preferred if preferred else secondary
    out: list[float] = []
    seen: set[float] = set()
    for v in vals:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out[:12]


def extract_candidate_from_page(
    html: str,
    *,
    url: str,
    identity: dict[str, Any],
) -> dict[str, Any] | None:
    if not identity_on_page(html, identity):
        return None
    prices = extract_prices_from_html(html)
    structured_via = False
    if prices:
        try:
            from public_price_search.catalog_intel import extract_structured_prices

            structured_via = bool(extract_structured_prices(html))
        except Exception:
            structured_via = False
    if not prices:
        return {
            "status": "PAGE_NO_PRICE",
            "url": url,
            "identity_matched": True,
            "condition": detect_condition(html),
            "blocked_price_render": True,
            "js_render_candidate": True,
        }
    # Heuristic: pick primary product price — prefer mid-range over tiny accessory prices
    pn = _norm_pn(
        str(
            identity.get("part_number")
            or identity.get("catalog_number")
            or identity.get("model")
            or ""
        )
    )
    # Prefer prices near PN mention
    primary = None
    for m in re.finditer(re.escape(pn[:8]) if len(pn) >= 8 else re.escape(pn), _norm_pn(html)):
        # search original html window — approximate via dollar near index
        break
    # Prefer productPrice / schema values already first; filter tiny noise
    candidates = [p for p in prices if p >= 5.0]
    if not candidates:
        candidates = prices
    primary = candidates[0]
    core, core_ref = extract_core_charge(html, displayed_price=primary)
    # Avoid mistaking core for main price
    if core and abs(primary - core) < 1e-9 and len(candidates) > 1:
        primary = candidates[1]
        core, core_ref = extract_core_charge(html, displayed_price=primary)
    ship = extract_shipping(html)
    condition = detect_condition(html)
    seller_class = classify_seller(url, identity.get("manufacturer") or identity.get("brand"))
    # Economics basis = displayed part price; core tracked separately (never silently netted)
    net_after_core = primary if (core is None or core_ref) else (primary + float(core))
    gross = primary + float(core) if core else primary
    return {
        "status": "PRICE_OK",
        "url": url,
        "unit_price": primary,
        "displayed_price": primary,
        "core_charge": core,
        "core_refundable": core_ref,
        "net_cost_if_core_returned": net_after_core,
        "gross_cash_required": gross,
        "core_return_requirement": bool(core),
        "condition": condition,
        "seller_class": seller_class,
        "seller": urlparse(url).netloc,
        "shipping": ship,
        "all_prices_seen": candidates[:6],
        "via": "structured_data" if structured_via else "page_html",
        "exact_match": True,
    }


def extract_product_links(html: str, identity: dict[str, Any], *, base_host: str = "") -> list[str]:
    """Pull product URLs that embed the MPN from a search/results page."""
    toks = identity_tokens(identity)
    if not toks:
        return []
    urls: list[str] = []
    for m in re.finditer(r'href=["\']([^"\']+)["\']', html or "", re.I):
        href = m.group(1)
        if href.startswith("/"):
            if base_host:
                href = f"https://{base_host}{href}"
            else:
                continue
        if not href.startswith("http"):
            continue
        norm = _norm_pn(href)
        if any(t in norm for t in toks if len(t) >= 5):
            if any(x in href.lower() for x in ("/product", "/products", "/p/", "/item", "injector", "part")):
                if href not in urls:
                    urls.append(href)
    return urls[:8]


def extract_from_serp_snippet(
    title: str,
    snippet: str,
    url: str,
    identity: dict[str, Any],
) -> dict[str, Any] | None:
    blob = f"{title} {snippet}"
    if not identity_on_page(blob, identity) and not identity_on_page(url, identity):
        return None
    prices = []
    for m in re.finditer(r"USD\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)", blob, re.I):
        v = _f(m.group(1))
        if v:
            prices.append(v)
    for m in _PRICE_DOLLAR.finditer(blob):
        v = _f(m.group(1))
        if v:
            prices.append(v)
    if not prices:
        return None
    primary = max(prices) if max(prices) < 50000 else prices[0]
    # Prefer the first USD price in snippet (often list price)
    if re.search(r"USD\s*", blob, re.I):
        m = re.search(r"USD\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)", blob, re.I)
        if m:
            primary = _f(m.group(1)) or primary
    core, core_ref = extract_core_charge(blob)
    return {
        "status": "PRICE_OK",
        "url": url,
        "unit_price": primary,
        "displayed_price": primary,
        "core_charge": core,
        "core_refundable": core_ref,
        "net_cost_if_core_returned": primary,
        "gross_cash_required": primary + (core or 0.0) if core else primary,
        "core_return_requirement": bool(core),
        "condition": detect_condition(blob),
        "seller_class": classify_seller(url, identity.get("manufacturer")),
        "seller": urlparse(url).netloc or SELLER_SNIPPET,
        "shipping": extract_shipping(blob),
        "via": "serp_snippet",
        "exact_match": True,
        "snippet_only": True,
        "confidence": "C",
    }
