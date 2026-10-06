"""Bounded browser fallback + Bing discovery (Playwright).

Build: 20261004-m3-price-adapters-v1
"""

from __future__ import annotations

import base64
import json
import threading
import time
from typing import Any
from urllib.parse import parse_qs, quote_plus, urlparse

from m3_data_root import data_path

_LOCK = threading.Lock()
_BROWSER_CACHE_FILE = "m3_price_adapters_browser_cache.json"
_ROUTE_CACHE_FILE = "m3_price_adapters_route_cache.json"
_DOMAIN_LAST: dict[str, float] = {}
_MIN_DOMAIN_GAP_SEC = 2.0
_BROWSER_BUDGET = {"renders": 0, "max_renders": 200}


def _load_json(name: str) -> dict[str, Any]:
    p = data_path(name)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_json(name: str, payload: dict[str, Any]) -> None:
    p = data_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def browser_stats() -> dict[str, Any]:
    return dict(_BROWSER_BUDGET)


def _rate_limit(domain: str) -> None:
    now = time.time()
    last = _DOMAIN_LAST.get(domain, 0)
    wait = _MIN_DOMAIN_GAP_SEC - (now - last)
    if wait > 0:
        time.sleep(min(wait, 3.0))
    _DOMAIN_LAST[domain] = time.time()


def decode_bing_redirect(href: str) -> str | None:
    try:
        qs = parse_qs(urlparse(href).query)
        u = (qs.get("u") or [None])[0]
        if not u:
            return None
        if u.startswith("a1"):
            raw = u[2:]
            raw += "=" * (-len(raw) % 4)
            return base64.b64decode(raw).decode("utf-8", errors="ignore")
        return u
    except Exception:
        return None


def bing_discover_urls(query: str, *, limit: int = 8) -> list[dict[str, str]]:
    """Playwright Bing search — returns decoded destination URLs (cached)."""
    cache = _load_json(_BROWSER_CACHE_FILE)
    bing = cache.setdefault("bing", {})
    if query in bing and isinstance(bing[query], list):
        return list(bing[query])[:limit]

    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return []
    with _LOCK:
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                    )
                )
                page.goto(
                    f"https://www.bing.com/search?q={quote_plus(query)}",
                    wait_until="domcontentloaded",
                    timeout=15000,
                )
                page.wait_for_timeout(800)
                rows = page.eval_on_selector_all(
                    "li.b_algo h2 a",
                    "els => els.map(e => ({href: e.href, text: e.innerText}))",
                )
                browser.close()
        except Exception:
            return []
    out: list[dict[str, str]] = []
    for r in rows or []:
        real = decode_bing_redirect(r.get("href") or "") or (r.get("href") or "")
        if not real.startswith("http"):
            continue
        # sanitize non-printable
        real = "".join(ch for ch in real if 32 <= ord(ch) < 127)
        low = real.lower()
        if any(x in low for x in ("bing.com", "microsoft.com", "wikipedia.org", "youtube.com", "facebook.com")):
            continue
        out.append({"url": real, "title": r.get("text") or ""})
        if len(out) >= limit:
            break
    bing[query] = out
    cache["bing"] = bing
    # avoid rewriting huge html cache repeatedly — only update bing index
    try:
        _save_json(_BROWSER_CACHE_FILE, cache)
    except Exception:
        pass
    return out


def bounded_browser_fetch(
    url: str,
    *,
    timeout_ms: int = 12000,
    wait_ms: int = 2000,
    capture_json: bool = True,
    force_refresh: bool = False,
    capture_bodies: bool = False,
    wait_price_selector: str | None = None,
) -> dict[str, Any]:
    """Bounded browser page fetch with cache + domain rate limit."""
    cache = _load_json(_BROWSER_CACHE_FILE)
    pages = cache.setdefault("pages", {})
    if (not force_refresh) and url in pages and pages[url].get("html"):
        cached = pages[url]
        # Do not reuse denial/404 caches for extraction builds
        title = ""
        hm = re_search_title(cached.get("html") or "")
        title = hm.lower()
        if any(
            x in title
            for x in (
                "access to this page has been denied",
                "attention required",
                "just a moment",
                "page not found",
                "404 not found",
                "access denied",
            )
        ):
            pass  # fall through to live fetch
        elif len(cached.get("html") or "") >= 800:
            return {**cached, "cached": True}

    if _BROWSER_BUDGET["renders"] >= _BROWSER_BUDGET["max_renders"]:
        return {"ok": False, "error": "BROWSER_BUDGET_EXHAUSTED", "html": "", "text": ""}

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        return {"ok": False, "error": f"NO_PLAYWRIGHT:{exc}", "html": "", "text": ""}

    domain = urlparse(url).netloc.replace("www.", "")
    _rate_limit(domain)
    json_urls: list[str] = []
    network_json: list[Any] = []
    with _LOCK:
        _BROWSER_BUDGET["renders"] += 1
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                    )
                )

                def on_response(resp):
                    try:
                        ct = (resp.headers.get("content-type") or "").lower()
                        if resp.status == 200 and "json" in ct:
                            u = resp.url
                            if any(
                                x in u.lower()
                                for x in ("product", "search", "price", "graphql", "api", "catalog", "variant", "cart")
                            ):
                                json_urls.append(u)
                                if capture_bodies and len(network_json) < 6:
                                    try:
                                        network_json.append(resp.json())
                                    except Exception:
                                        try:
                                            txt = resp.text()
                                            if txt and len(txt) < 500_000:
                                                network_json.append(json.loads(txt))
                                        except Exception:
                                            pass
                    except Exception:
                        pass

                if capture_json:
                    page.on("response", on_response)
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                page.wait_for_timeout(wait_ms)
                if wait_price_selector:
                    try:
                        page.wait_for_selector(wait_price_selector, timeout=4000)
                    except Exception:
                        pass
                # Soft wait for common price nodes
                try:
                    page.wait_for_selector(
                        "[itemprop=price], .price, .product-price, [data-price-amount], .price-item",
                        timeout=2500,
                    )
                except Exception:
                    pass
                html = page.content()
                text = page.inner_text("body")[:100000]
                browser.close()
        except Exception as exc:
            return {
                "ok": False,
                "error": type(exc).__name__,
                "html": "",
                "text": "",
                "json_urls": [],
                "network_json": [],
            }

    row = {
        "ok": True,
        "url": url,
        "html": html,
        "text": text,
        "json_urls": list(dict.fromkeys(json_urls))[:8],
        "network_json": network_json[:6],
        "cached": False,
    }
    # Only cache usable pages
    title = re_search_title(html).lower()
    deny = any(
        x in title
        for x in (
            "access to this page has been denied",
            "attention required",
            "just a moment",
            "page not found",
            "404 not found",
            "access denied",
        )
    )
    if not deny and len(html) >= 800:
        pages[url] = {
            "ok": True,
            "url": url,
            "html": html[:500_000],
            "text": text[:80_000],
            "json_urls": row["json_urls"],
        }
        cache["pages"] = pages
        _save_json(_BROWSER_CACHE_FILE, cache)
    return row


def re_search_title(html: str) -> str:
    import re

    m = re.search(r"<title[^>]*>([^<]{0,160})", html or "", re.I)
    return (m.group(1).strip() if m else "")


def clear_denied_browser_cache() -> int:
    """Drop cached denial/404 pages so extraction can retry live."""
    cache = _load_json(_BROWSER_CACHE_FILE)
    pages = cache.get("pages") or {}
    drop = []
    for u, row in pages.items():
        title = re_search_title(row.get("html") or "").lower()
        if any(
            x in title
            for x in (
                "access to this page has been denied",
                "attention required",
                "page not found",
                "404 not found",
                "access denied",
            )
        ) or len(row.get("html") or "") < 200:
            drop.append(u)
    for u in drop:
        pages.pop(u, None)
    cache["pages"] = pages
    _save_json(_BROWSER_CACHE_FILE, cache)
    return len(drop)


def note_route_success(domain: str, pattern: str, method: str) -> None:
    cache = _load_json(_ROUTE_CACHE_FILE)
    domains = cache.setdefault("domains", {})
    d = domains.setdefault(domain, {"success": [], "methods": {}})
    d["success"].append({"pattern": pattern, "method": method, "ts": time.time()})
    d["success"] = d["success"][-20:]
    d["methods"][method] = int(d["methods"].get(method) or 0) + 1
    _save_json(_ROUTE_CACHE_FILE, cache)


def load_route_cache() -> dict[str, Any]:
    return _load_json(_ROUTE_CACHE_FILE)
