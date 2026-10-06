"""Search transport abstraction with independent providers + telemetry.

Providers:
  A — Bing HTML
  B — DuckDuckGo HTML
  C — Bing Playwright (recovery)
  D — phase_l fallback

NO_RESULTS does not trip hard circuits (only HTTP/transport failures do).
"""

from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import quote_plus

from m3_data_root import data_path
from open_web_product_discovery.models import TRANSPORT_STATS
from public_price_search.budget import consume, remaining
from public_price_search.circuits import is_open, note, reset_all
from public_price_search.search import (
    _clean_url,
    bing_results,
    ddg_results,
    reset_serp_circuit,
)

# Soft telemetry (separate from hard circuit breakers)
_TELEM: dict[str, Any] = {
    "providers": {},
    "queries": [],
}


def _pstats(name: str) -> dict[str, Any]:
    row = _TELEM["providers"].setdefault(
        name,
        {
            "attempts": 0,
            "results": 0,
            "blocked": 0,
            "empty": 0,
            "errors": 0,
            "latency_ms_sum": 0,
            "product_url_yield": 0,
            "successes": 0,
        },
    )
    return row


def reset_transport(*, soft: bool = True) -> None:
    """Reset SERP circuits and optional telemetry."""
    reset_all()
    reset_serp_circuit()
    if soft:
        _TELEM["providers"] = {}
        _TELEM["queries"] = []


def persist_transport_stats() -> None:
    p = data_path(TRANSPORT_STATS)
    p.parent.mkdir(parents=True, exist_ok=True)
    snap = transport_snapshot()
    p.write_text(json.dumps(snap, indent=2, default=str), encoding="utf-8")


def transport_snapshot() -> dict[str, Any]:
    providers = {}
    for name, row in (_TELEM.get("providers") or {}).items():
        attempts = int(row.get("attempts") or 0)
        successes = int(row.get("successes") or 0)
        providers[name] = {
            **row,
            "success_rate": round(100.0 * successes / attempts, 1) if attempts else 0.0,
            "avg_latency_ms": int(row["latency_ms_sum"] / attempts) if attempts else 0,
        }
    return {
        "providers": providers,
        "queries_logged": len(_TELEM.get("queries") or []),
        "recent_queries": (_TELEM.get("queries") or [])[-30:],
    }


def note_product_yield(provider: str, n: int) -> None:
    if n <= 0:
        return
    _pstats(provider)["product_url_yield"] = int(_pstats(provider).get("product_url_yield") or 0) + n


def _bing_playwright(query: str, *, limit: int = 8) -> list[dict[str, Any]]:
    """SEARCH_PROVIDER_C — Playwright Bing href extraction."""
    try:
        from price_adapters.browser import bing_discover_urls
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for row in bing_discover_urls(query, limit=limit) or []:
        url = _clean_url(row.get("url") or "")
        if not url:
            continue
        out.append(
            {
                "url": url,
                "title": row.get("title") or "",
                "snippet": "",
                "provider": "bing_playwright",
            }
        )
        if len(out) >= limit:
            break
    return out


def search_open_web(
    query: str,
    *,
    limit: int = 8,
    use_budget: bool = True,
    prefer_playwright: bool = False,
) -> dict[str, Any]:
    """Independent multi-provider search with telemetry."""
    if use_budget and remaining() <= 0:
        return {"ok": False, "budget_exhausted": True, "results": [], "query": query, "providers_tried": []}

    if use_budget:
        consume(1, kind="query")

    providers_tried: list[str] = []
    results: list[dict[str, Any]] = []
    provider_used = None

    order = ["bing_playwright", "bing", "duckduckgo"] if prefer_playwright else ["bing", "duckduckgo", "bing_playwright"]

    for name in order:
        if results:
            break
        t0 = time.time()
        row = _pstats(name)
        row["attempts"] = int(row["attempts"]) + 1
        providers_tried.append(name)
        got: list[dict[str, Any]] = []
        blocked = False
        err = False
        try:
            if name == "bing":
                if is_open("SEARCH_PROVIDER_A"):
                    blocked = True
                else:
                    got = bing_results(query, limit=limit)
                    # Soft: empty is not a hard circuit failure
                    if got:
                        note("SEARCH_PROVIDER_A", ok=True)
            elif name == "duckduckgo":
                if is_open("SEARCH_PROVIDER_B"):
                    blocked = True
                else:
                    got = ddg_results(query, limit=limit)
                    if got:
                        note("SEARCH_PROVIDER_B", ok=True)
            elif name == "bing_playwright":
                got = _bing_playwright(query, limit=limit)
        except Exception:
            err = True
            got = []

        latency = int((time.time() - t0) * 1000)
        row["latency_ms_sum"] = int(row["latency_ms_sum"]) + latency
        if blocked:
            row["blocked"] = int(row["blocked"]) + 1
        elif err:
            row["errors"] = int(row["errors"]) + 1
        elif not got:
            row["empty"] = int(row["empty"]) + 1
        else:
            row["successes"] = int(row["successes"]) + 1
            row["results"] = int(row["results"]) + len(got)
            results = got
            provider_used = name

    # phase_l last resort
    if not results:
        name = "phase_l"
        t0 = time.time()
        row = _pstats(name)
        row["attempts"] = int(row["attempts"]) + 1
        providers_tried.append(name)
        try:
            from phase_l.market_price import search_urls_with_fallback

            fb = search_urls_with_fallback(query, limit=limit)
            for u in fb.get("urls") or []:
                cu = _clean_url(u)
                if cu:
                    results.append(
                        {
                            "url": cu,
                            "title": "",
                            "snippet": "",
                            "provider": fb.get("provider") or "phase_l",
                        }
                    )
            if results:
                provider_used = name
                row["successes"] = int(row["successes"]) + 1
                row["results"] = int(row["results"]) + len(results)
            else:
                row["empty"] = int(row["empty"]) + 1
        except Exception:
            row["errors"] = int(row["errors"]) + 1
        row["latency_ms_sum"] = int(row["latency_ms_sum"]) + int((time.time() - t0) * 1000)

    _TELEM.setdefault("queries", []).append(
        {
            "query": query,
            "provider": provider_used,
            "n": len(results),
            "providers_tried": providers_tried,
            "ts": time.time(),
        }
    )
    if len(_TELEM["queries"]) > 500:
        _TELEM["queries"] = _TELEM["queries"][-300:]

    return {
        "ok": bool(results),
        "budget_exhausted": False,
        "results": results[:limit],
        "provider": provider_used,
        "providers_tried": providers_tried,
        "query": query,
        "route_status": "HEALTHY" if results else "RETRYABLE",
    }


def site_query(domain: str, mpn: str, manufacturer: str | None = None) -> str:
    host = (domain or "").lower().replace("www.", "")
    if manufacturer:
        return f'site:{host} {manufacturer} "{mpn}"'
    return f'site:{host} "{mpn}"'
