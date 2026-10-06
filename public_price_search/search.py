"""Multi-provider SERP discovery + page fetch with scoped circuit breakers.

Build: 20261004-m3-price-search-reliability-v1

SEARCH_PROVIDER_A (Bing) and SEARCH_PROVIDER_B (DDG) fail independently.
A single provider circuit never disables manufacturer/distributor/catalog routes.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import quote_plus, unquote, urlparse

import httpx

from public_price_search.budget import consume, remaining
from public_price_search.circuits import (
    domain_of,
    is_open,
    note,
    report_health,
    reset_all,
    snapshot,
    status_of,
)
from public_price_search.extract import extract_from_serp_snippet

log = logging.getLogger("govtracker.public_price_search.search")

# Legacy aliases kept for callers from liveprice_v2
_ROUTE_HEALTH: dict[str, dict[str, Any]] = {}


def reset_serp_circuit() -> None:
    """Reset all route circuits (legacy name retained)."""
    reset_all()
    _ROUTE_HEALTH.clear()


def serp_circuit_open() -> bool:
    """True only when BOTH search providers are open — never a global pricing kill."""
    return is_open("SEARCH_PROVIDER_A") and is_open("SEARCH_PROVIDER_B")


def note_route(route: str, *, ok: bool, error: str | None = None) -> None:
    """Map legacy route labels onto independent families."""
    mapping = {
        "SERP": "SEARCH_PROVIDER_A",
        "Manufacturer": "MANUFACTURER_SEARCH",
        "Distributor": "DISTRIBUTOR_SEARCH",
        "Reseller": "RESELLER_SEARCH",
        "Structured catalog": "STRUCTURED_PRODUCT_DATA",
        "Direct catalog": "DIRECT_CATALOG",
        "Other": "KNOWN_DOMAIN_SEARCH",
    }
    fam = mapping.get(route, route if route in {
        "SEARCH_PROVIDER_A", "SEARCH_PROVIDER_B", "MANUFACTURER_SEARCH",
        "DISTRIBUTOR_SEARCH", "RESELLER_SEARCH", "DIRECT_CATALOG",
        "STRUCTURED_PRODUCT_DATA", "KNOWN_DOMAIN_SEARCH", "PUBLIC_CATALOG_PDF",
        "CACHED_PRODUCT_INDEX",
    } else "KNOWN_DOMAIN_SEARCH")
    note(fam, ok=ok, reason=error)
    # Keep legacy snapshot shape
    h = _ROUTE_HEALTH.setdefault(
        route, {"status": "HEALTHY", "failures": 0, "successes": 0, "last_error": None}
    )
    if ok:
        h["successes"] = int(h.get("successes") or 0) + 1
        h["status"] = "HEALTHY"
    else:
        h["failures"] = int(h.get("failures") or 0) + 1
        h["last_error"] = error
        h["status"] = status_of(fam)


def route_health_snapshot() -> dict[str, Any]:
    """Combined legacy + family health for reports."""
    fam = snapshot()
    legacy = {
        "SERP": {
            "status": (
                "CIRCUIT_OPEN"
                if serp_circuit_open()
                else (
                    "DEGRADED"
                    if is_open("SEARCH_PROVIDER_A") or is_open("SEARCH_PROVIDER_B")
                    else fam.get("SEARCH_PROVIDER_A", {}).get("status", "HEALTHY")
                )
            ),
            "provider_a": fam.get("SEARCH_PROVIDER_A"),
            "provider_b": fam.get("SEARCH_PROVIDER_B"),
        },
        "Manufacturer": fam.get("MANUFACTURER_SEARCH"),
        "Distributor": fam.get("DISTRIBUTOR_SEARCH"),
        "Reseller": fam.get("RESELLER_SEARCH"),
        "Structured catalog": fam.get("STRUCTURED_PRODUCT_DATA"),
        "Direct catalog": fam.get("DIRECT_CATALOG"),
        "Other": fam.get("KNOWN_DOMAIN_SEARCH"),
        "families": fam,
        "report": report_health(),
    }
    return legacy


_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
}


def _clean_url(u: str) -> str | None:
    if not u:
        return None
    u = u.strip()
    if u.startswith("//"):
        u = "https:" + u
    if "uddg=" in u:
        m = re.search(r"uddg=([^&]+)", u)
        if m:
            u = unquote(m.group(1))
    if not u.startswith("http"):
        return None
    low = u.lower()
    if any(x in low for x in ("duckduckgo.com", "bing.com", "google.com", "youtube.com", "facebook.com")):
        return None
    return u


def bing_results(query: str, *, limit: int = 8, client: httpx.Client | None = None) -> list[dict[str, Any]]:
    """SEARCH_PROVIDER_A — Bing HTML."""
    if is_open("SEARCH_PROVIDER_A"):
        return []
    own = client is None
    c = client or httpx.Client(timeout=8.0, follow_redirects=True, headers=_UA)
    out: list[dict[str, Any]] = []
    try:
        r = c.get(f"https://www.bing.com/search?q={quote_plus(query)}")
        if r.status_code >= 400:
            note("SEARCH_PROVIDER_A", ok=False, reason=f"HTTP_{r.status_code}")
            return []
        text = r.text or ""
        for m in re.finditer(r'<li class="b_algo".*?</li>', text, re.I | re.S):
            block = m.group(0)
            hm = re.search(r'href="(https?://[^"]+)"', block)
            tm = re.search(r"<h2[^>]*>\s*<a[^>]*>(.*?)</a>", block, re.I | re.S)
            sm = re.search(r'class="b_caption"[^>]*>.*?<p[^>]*>(.*?)</p>', block, re.I | re.S)
            if not hm:
                continue
            url = _clean_url(hm.group(1))
            if not url:
                continue
            title = re.sub(r"<[^>]+>", "", tm.group(1) if tm else "")
            snippet = re.sub(r"<[^>]+>", "", sm.group(1) if sm else "")
            out.append({"url": url, "title": title, "snippet": snippet, "provider": "bing"})
            if len(out) >= limit:
                break
        if not out:
            try:
                from phase_l.market_price import bing_search_urls

                urls, _ = bing_search_urls(query, limit=limit)
                for u in urls or []:
                    cu = _clean_url(u)
                    if cu:
                        out.append({"url": cu, "title": "", "snippet": "", "provider": "bing"})
            except Exception:
                pass
        note("SEARCH_PROVIDER_A", ok=bool(out), reason=None if out else "NO_RESULTS")
    except Exception as exc:
        note("SEARCH_PROVIDER_A", ok=False, reason=type(exc).__name__)
        log.debug("bing_results failed: %s", type(exc).__name__)
    finally:
        if own:
            c.close()
    return out[:limit]


def ddg_results(query: str, *, limit: int = 8, client: httpx.Client | None = None) -> list[dict[str, Any]]:
    """SEARCH_PROVIDER_B — DuckDuckGo HTML."""
    if is_open("SEARCH_PROVIDER_B"):
        return []
    own = client is None
    c = client or httpx.Client(timeout=8.0, follow_redirects=True, headers=_UA)
    out: list[dict[str, Any]] = []
    try:
        r = c.get(f"https://html.duckduckgo.com/html/?q={quote_plus(query)}")
        if r.status_code >= 400:
            note("SEARCH_PROVIDER_B", ok=False, reason=f"HTTP_{r.status_code}")
            return []
        text = r.text or ""
        for m in re.finditer(
            r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?class="result__snippet"[^>]*>(.*?)</a>',
            text,
            re.I | re.S,
        ):
            url = _clean_url(m.group(1))
            if not url:
                continue
            title = re.sub(r"<[^>]+>", "", m.group(2))
            snippet = re.sub(r"<[^>]+>", "", m.group(3))
            out.append({"url": url, "title": title, "snippet": snippet, "provider": "duckduckgo"})
            if len(out) >= limit:
                break
        if not out:
            for m in re.finditer(r'uddg=([^&"]+)', text):
                url = _clean_url(unquote(m.group(1)))
                if url and not any(x["url"] == url for x in out):
                    out.append({"url": url, "title": "", "snippet": "", "provider": "duckduckgo"})
                if len(out) >= limit:
                    break
        note("SEARCH_PROVIDER_B", ok=bool(out), reason=None if out else "NO_RESULTS")
    except Exception as exc:
        note("SEARCH_PROVIDER_B", ok=False, reason=type(exc).__name__)
        log.debug("ddg_results failed: %s", type(exc).__name__)
    finally:
        if own:
            c.close()
    return out[:limit]


def search_web(
    query: str,
    *,
    limit: int = 8,
    client: httpx.Client | None = None,
    use_budget: bool = True,
) -> dict[str, Any]:
    """Try provider A then B independently. Never open a global pricing kill switch."""
    if use_budget and remaining() <= 0:
        return {"ok": False, "budget_exhausted": True, "results": [], "query": query}

    a_open = is_open("SEARCH_PROVIDER_A")
    b_open = is_open("SEARCH_PROVIDER_B")
    if a_open and b_open:
        return {
            "ok": False,
            "budget_exhausted": False,
            "results": [],
            "query": query,
            "provider": None,
            "skipped": "ALL_SEARCH_PROVIDERS_CIRCUIT_OPEN",
            "route_status": "CIRCUIT_OPEN",
            "note": "Both search providers cooling down; manufacturer/distributor/catalog continue",
        }

    if use_budget:
        consume(1, kind="query")

    results: list[dict[str, Any]] = []
    provider = None

    if not a_open:
        results = bing_results(query, limit=limit, client=client)
        provider = "bing" if results else None
    if not results and not b_open:
        results = ddg_results(query, limit=limit, client=client)
        provider = "duckduckgo" if results else None

    # phase_l fallback — counts as provider B alternate, not a global SERP kill
    if not results and not b_open:
        try:
            from phase_l.market_price import search_urls_with_fallback

            fb = search_urls_with_fallback(query, limit=limit)
            for u in fb.get("urls") or []:
                cu = _clean_url(u)
                if cu:
                    results.append({"url": cu, "title": "", "snippet": "", "provider": fb.get("provider") or "phase_l"})
            if results:
                provider = fb.get("provider") or "phase_l"
                note("SEARCH_PROVIDER_B", ok=True)
        except Exception:
            pass

    route_status = "HEALTHY" if results else (
        "CIRCUIT_OPEN" if (a_open and b_open) else "RETRYABLE"
    )
    return {
        "ok": bool(results),
        "budget_exhausted": False,
        "results": results,
        "provider": provider,
        "query": query,
        "route_status": route_status,
        "provider_a_open": a_open,
        "provider_b_open": b_open,
    }


def fetch_page(
    url: str,
    *,
    client: httpx.Client | None = None,
    use_budget: bool = True,
) -> dict[str, Any]:
    dom = domain_of(url)
    if dom and is_open("DIRECT_CATALOG", domain=dom):
        return {
            "ok": False,
            "status_code": None,
            "url": url,
            "text": "",
            "blocked": True,
            "error": "DOMAIN_CIRCUIT_OPEN",
            "host": dom,
        }
    if use_budget:
        consume(1, kind="page")
    own = client is None
    c = client or httpx.Client(timeout=6.0, follow_redirects=True, headers=_UA)
    try:
        r = c.get(url)
        status = r.status_code
        text = r.text or ""
        blocked = status in {401, 403, 429, 503} or bool(
            re.search(r"captcha|cf-challenge|bot.?detect|access.?denied", text[:4000], re.I)
        )
        if blocked:
            note("DIRECT_CATALOG", ok=False, domain=dom, reason=f"BLOCKED_{status}")
        else:
            note("DIRECT_CATALOG", ok=status < 400, domain=dom, reason=None if status < 400 else f"HTTP_{status}")
        return {
            "ok": status < 400 and not blocked,
            "status_code": status,
            "url": str(r.url),
            "text": text,
            "blocked": blocked,
            "host": urlparse(str(r.url)).netloc,
        }
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        note("DIRECT_CATALOG", ok=False, domain=dom, reason=type(exc).__name__)
        return {
            "ok": False,
            "status_code": None,
            "url": url,
            "text": "",
            "blocked": True,
            "error": type(exc).__name__,
            "host": dom,
        }
    finally:
        if own:
            c.close()


def snippet_candidates(
    results: list[dict[str, Any]],
    identity: dict[str, Any],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in results:
        cand = extract_from_serp_snippet(
            r.get("title") or "",
            r.get("snippet") or "",
            r.get("url") or "",
            identity,
        )
        if cand:
            out.append(cand)
    return out
