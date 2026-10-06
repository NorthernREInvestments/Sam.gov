"""Adaptive alt-seller-first recovery with per-route budgets.

Build: 20261004-m3-full100-throughput-v1
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import quote_plus, urlparse

from full100_throughput.category_map import seller_candidates_for_item
from full100_throughput.domain_intel import note_attempt
from full100_throughput.models import LOW_YIELD_PRIMARY, ROUTE_TIMEOUT
from price_adapters.base import QuillAdapter, get_adapter_for_domain
from price_adapters.browser import bing_discover_urls, note_route_success
from price_adapters.models import FOUND_VALID_PRICE
from price_adapters.orchestrate import (
    _KNOWN_PRODUCT_URLS,
    _identity,
    _try_page,
    _seller_trusted,
)
from price_adapters.validate import seller_of

# Per-route wall budgets (seconds)
BUDGET_STATIC = 4.0
BUDGET_PRIMARY_LOW_YIELD = 3.5
BUDGET_ALT = 5.0
BUDGET_BING = 18.0
BUDGET_BROWSER = 12.0
BUDGET_ITEM_DEFAULT = 28.0


def _accept(diag: dict[str, Any], stats: dict[str, Any], cand: dict[str, Any], *, structured: bool = True, browser: bool = False, alternate: bool = False) -> None:
    if not cand:
        return
    known = str((diag.get("identity") or {}).get("known_public_seller") or "").lower()
    if not _seller_trusted(cand.get("seller") or ""):
        if not known or known not in str(cand.get("seller") or "").lower():
            return
    diag["candidates"].append(cand)
    if alternate:
        stats["alternate_seller_recoveries"] = int(stats.get("alternate_seller_recoveries") or 0) + 1
    if browser:
        stats["browser_recoveries"] = int(stats.get("browser_recoveries") or 0) + 1
    elif structured:
        stats["structured_recoveries"] = int(stats.get("structured_recoveries") or 0) + 1


def recover_fast(
    item: dict[str, Any],
    *,
    allow_browser: bool = True,
    stats: dict[str, Any] | None = None,
    item_deadline: float | None = None,
) -> dict[str, Any]:
    """High-throughput recovery: known URL → category alts → Quill → brief primary → Bing."""
    stats = stats if stats is not None else {}
    stats.setdefault("http_requests", 0)
    stats.setdefault("adapter_attempts", 0)
    stats.setdefault("structured_recoveries", 0)
    stats.setdefault("browser_recoveries", 0)
    stats.setdefault("alternate_seller_recoveries", 0)
    stats.setdefault("route_timeouts", 0)
    stats.setdefault("domain_stats", {})
    stats.setdefault("timeouts", 0)

    started = time.time()
    if item_deadline is None:
        item_deadline = started + BUDGET_ITEM_DEFAULT

    def timed_out() -> bool:
        return time.time() > item_deadline

    identity = _identity(item)
    known = (item.get("known_public_seller") or "").lower().replace("www.", "")
    diag: dict[str, Any] = {
        "build": "20261004-m3-full100-throughput-v1",
        "identity": identity,
        "primary": None,
        "alternates": [],
        "bing": [],
        "candidates": [],
        "best": None,
        "status": "EXHAUSTED",
        "routes": [],
    }
    pn = str(identity.get("part_number") or "")
    mfr = identity.get("manufacturer") or ""

    def try_url(url: str, *, browser: bool, tag: str, domain: str | None = None) -> dict[str, Any] | None:
        if timed_out():
            stats["route_timeouts"] += 1
            diag["routes"].append({"tag": tag, "status": ROUTE_TIMEOUT})
            return None
        t0 = time.time()
        host = domain or seller_of(url)
        cand = _try_page(url, identity, allow_browser=browser)
        dt = time.time() - t0
        stats["http_requests"] += 1
        note_attempt(
            host,
            latency_s=dt,
            priced=bool(cand),
            structured=bool(cand) and "browser" not in str((cand or {}).get("via") or ""),
            browser=browser,
            browser_ok=bool(cand) and browser,
            alternate=True,
            status=None if cand else "MISS",
        )
        dstat = stats["domain_stats"].setdefault(host, {"attempted": 0, "recovered": 0, "direct": 0, "alternate": 0, "time_s": 0.0})
        dstat["attempted"] += 1
        dstat["time_s"] += dt
        if cand:
            dstat["recovered"] += 1
            dstat["alternate"] += 1
            note_route_success(host, url, cand.get("via") or tag)
        diag["routes"].append({"tag": tag, "url": url, "hit": bool(cand), "dt": round(dt, 3)})
        return cand

    # 1) Known high-yield exact URLs
    hints: list[str] = []
    if identity.get("known_url_hint"):
        hints.append(str(identity["known_url_hint"]))
    hints.extend(_KNOWN_PRODUCT_URLS.get(pn.upper(), []) or [])
    for hint in list(dict.fromkeys(hints)):
        if timed_out() or diag["candidates"]:
            break
        # Skip burning time on known low-yield primary search shells
        host = seller_of(hint)
        browser = allow_browser and host not in LOW_YIELD_PRIMARY
        cand = try_url(hint, browser=False, tag="known_url", domain=host)
        if not cand and browser and not timed_out():
            cand = try_url(hint, browser=True, tag="known_url_browser", domain=host)
        if cand:
            _accept(diag, stats, cand, alternate=True, browser="browser" in cand.get("via", ""))

    # 2) Category alternate sellers first (ranked by yield)
    if len(diag["candidates"]) < 2 and not timed_out():
        for domain, url in seller_candidates_for_item(item, limit=5):
            if timed_out() or len(diag["candidates"]) >= 2:
                break
            if known and domain == known and domain in LOW_YIELD_PRIMARY:
                continue
            cand = try_url(url, browser=False, tag="category_alt", domain=domain)
            if cand:
                _accept(diag, stats, cand, alternate=True)

    # 3) Quill exact-MPN (high yield static)
    if len(diag["candidates"]) < 2 and not timed_out():
        stats["adapter_attempts"] += 1
        t0 = time.time()
        q = QuillAdapter().recover(identity, allow_browser=False)
        note_attempt("quill.com", latency_s=time.time() - t0, priced=q.get("status") == FOUND_VALID_PRICE, structured=True, alternate=True)
        diag["alternates"].append({"adapter": "Quill", "status": q.get("status")})
        if q.get("status") == FOUND_VALID_PRICE and q.get("candidate"):
            _accept(diag, stats, q["candidate"], alternate=True)

    # 4) Brief primary low-yield attempt (static only, short)
    primary = get_adapter_for_domain(known) if known else None
    if primary and len(diag["candidates"]) < 1 and not timed_out():
        stats["adapter_attempts"] += 1
        dstat = stats["domain_stats"].setdefault(primary.domain, {"attempted": 0, "recovered": 0, "direct": 0, "alternate": 0, "time_s": 0.0})
        dstat["attempted"] += 1
        t0 = time.time()
        # Hard cap low-yield primary
        local_deadline = min(item_deadline, time.time() + BUDGET_PRIMARY_LOW_YIELD)
        if known in LOW_YIELD_PRIMARY and time.time() > local_deadline - 0.1:
            diag["primary"] = {"adapter": primary.name, "status": ROUTE_TIMEOUT}
            stats["route_timeouts"] += 1
        else:
            result = primary.recover(identity, allow_browser=False)
            dt = time.time() - t0
            dstat["time_s"] += dt
            diag["primary"] = {"adapter": primary.name, "status": result.get("status"), "dt": round(dt, 3)}
            note_attempt(primary.domain, latency_s=dt, status=result.get("status"), priced=result.get("status") == FOUND_VALID_PRICE)
            if result.get("status") == FOUND_VALID_PRICE and result.get("candidate"):
                _accept(diag, stats, result["candidate"], structured=True)
                dstat["recovered"] += 1
                dstat["direct"] += 1

    # 5) Bing only if still empty (single query, trusted hosts)
    if not diag["candidates"] and allow_browser and not timed_out():
        desc = str(identity.get("raw_description") or "")
        words = [w for w in __import__("re").findall(r"[A-Za-z]{4,}", desc) if w.lower() not in {"with", "from", "that", "this"}]
        query = f'"{pn}" {mfr} {" ".join(words[:2])} buy -amazon -ebay'.strip()
        t0 = time.time()
        links = bing_discover_urls(query, limit=4)
        diag["bing"].append({"query": query, "n": len(links), "dt": round(time.time() - t0, 3)})
        for row in links:
            if timed_out() or diag["candidates"]:
                break
            url = row.get("url") or ""
            host = seller_of(url)
            if not _seller_trusted(host) and (not known or known not in host):
                continue
            path = urlparse(url).path or ""
            if path in {"", "/"} and pn.lower() not in url.lower():
                continue
            cand = try_url(url, browser=False, tag="bing_static", domain=host)
            if not cand and allow_browser and not timed_out():
                # Browser only for exact-ish product paths
                if any(x in path.lower() for x in ("/p/", "/product", "/sku", pn.lower().replace(" ", ""))):
                    cand = try_url(url, browser=True, tag="bing_browser", domain=host)
            if cand:
                _accept(
                    diag,
                    stats,
                    cand,
                    alternate=True,
                    browser="browser" in cand.get("via", ""),
                    structured="browser" not in cand.get("via", ""),
                )
                break

    if diag["candidates"]:
        diag["candidates"].sort(key=lambda c: float(c["unit_price"]))
        best = diag["candidates"][0]
        best["selection_reason"] = (
            f"BEST_DEFENSIBLE_NEW_LANDED_PRICE; among {len(diag['candidates'])} valid; "
            f"seller={best.get('seller')}; via={best.get('via')}"
        )
        diag["best"] = best
        diag["status"] = FOUND_VALID_PRICE
        diag["best_price_improvement"] = len(diag["candidates"]) >= 2
    elif timed_out():
        diag["status"] = ROUTE_TIMEOUT
        stats["timeouts"] += 1
    else:
        diag["status"] = "EXHAUSTED"

    diag["elapsed_s"] = round(time.time() - started, 3)
    return diag
