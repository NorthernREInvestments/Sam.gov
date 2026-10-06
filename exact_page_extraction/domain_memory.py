"""Domain extraction memory + strategy scoring."""

from __future__ import annotations

import json
import time
from typing import Any

from exact_page_extraction.models import DOMAIN_MEMORY
from m3_data_root import data_path


def _load() -> dict[str, Any]:
    p = data_path(DOMAIN_MEMORY)
    if not p.exists():
        return {"domains": {}, "updated_at": None}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"domains": {}, "updated_at": None}


def _save(payload: dict[str, Any]) -> None:
    p = data_path(DOMAIN_MEMORY)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload["updated_at"] = time.time()
    p.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def note_extraction(
    domain: str,
    *,
    route: str,
    success: bool,
    latency_s: float = 0.0,
    endpoint: str | None = None,
) -> None:
    payload = _load()
    d = (domain or "").lower().replace("www.", "")
    row = payload.setdefault("domains", {}).setdefault(
        d,
        {
            "attempts": 0,
            "successes": 0,
            "by_route": {},
            "endpoints": [],
            "latency_sum": 0.0,
            "latency_n": 0,
            "strategy_score": 0.0,
        },
    )
    row["attempts"] += 1
    if success:
        row["successes"] += 1
    br = row["by_route"].setdefault(route, {"attempts": 0, "successes": 0})
    br["attempts"] += 1
    if success:
        br["successes"] += 1
    if latency_s:
        row["latency_sum"] += float(latency_s)
        row["latency_n"] += 1
    if endpoint and endpoint not in row["endpoints"]:
        row["endpoints"].append(endpoint[:200])
    attempts = max(int(row["attempts"]), 1)
    succ = int(row["successes"])
    avg_lat = float(row["latency_sum"]) / max(int(row["latency_n"]), 1)
    row["strategy_score"] = round(succ / attempts - 0.01 * avg_lat, 3)
    _save(payload)


def preferred_routes(domain: str) -> list[str]:
    """Highest-yield routes first for this domain."""
    payload = _load()
    d = (domain or "").lower().replace("www.", "")
    row = (payload.get("domains") or {}).get(d) or {}
    by = row.get("by_route") or {}
    scored = []
    for route, stats in by.items():
        att = max(int(stats.get("attempts") or 0), 1)
        suc = int(stats.get("successes") or 0)
        scored.append((suc / att, suc, route))
    scored.sort(reverse=True)
    default = ["JSON_LD", "HYDRATION", "STATIC_HTML", "API_XHR", "BROWSER", "CART"]
    ordered = [r for _, __, r in scored]
    for r in default:
        if r not in ordered:
            ordered.append(r)
    return ordered


def domain_snapshot(limit: int = 25) -> list[dict[str, Any]]:
    payload = _load()
    rows = []
    for domain, row in (payload.get("domains") or {}).items():
        by = row.get("by_route") or {}
        rows.append(
            {
                "domain": domain,
                "exact_urls_attempted": row.get("attempts", 0),
                "static_successes": (by.get("STATIC_HTML") or {}).get("successes", 0),
                "structured_successes": (by.get("JSON_LD") or {}).get("successes", 0)
                + (by.get("HYDRATION") or {}).get("successes", 0),
                "api_successes": (by.get("API_XHR") or {}).get("successes", 0)
                + (by.get("GRAPHQL") or {}).get("successes", 0),
                "browser_successes": (by.get("BROWSER") or {}).get("successes", 0),
                "total_prices": row.get("successes", 0),
                "success_rate": round(int(row.get("successes") or 0) / max(int(row.get("attempts") or 1), 1), 3),
            }
        )
    rows.sort(key=lambda r: (-r["total_prices"], -r["success_rate"], r["domain"]))
    return rows[:limit]
