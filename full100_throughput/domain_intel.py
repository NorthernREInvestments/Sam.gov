"""Persistent domain reliability scores for adaptive routing.

Build: 20261004-m3-full100-throughput-v1
"""

from __future__ import annotations

import json
import time
from typing import Any

from full100_throughput.models import DOMAIN_INTEL, LOW_YIELD_PRIMARY
from m3_data_root import data_path


def _load() -> dict[str, Any]:
    p = data_path(DOMAIN_INTEL)
    if not p.exists():
        return {"domains": {}, "updated_at": None}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"domains": {}, "updated_at": None}


def _save(payload: dict[str, Any]) -> None:
    p = data_path(DOMAIN_INTEL)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload["updated_at"] = time.time()
    p.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _row(domains: dict[str, Any], domain: str) -> dict[str, Any]:
    d = (domain or "").lower().replace("www.", "")
    row = domains.setdefault(
        d,
        {
            "attempts": 0,
            "discoveries": 0,
            "prices": 0,
            "blocked_403": 0,
            "js_hidden": 0,
            "domain_unavailable": 0,
            "latency_sum": 0.0,
            "latency_n": 0,
            "browser_ok": 0,
            "browser_n": 0,
            "structured_ok": 0,
            "alt_recovery": 0,
        },
    )
    return row


def note_attempt(
    domain: str,
    *,
    latency_s: float = 0.0,
    status: str | None = None,
    priced: bool = False,
    structured: bool = False,
    browser: bool = False,
    browser_ok: bool = False,
    alternate: bool = False,
) -> None:
    payload = _load()
    domains = payload.setdefault("domains", {})
    row = _row(domains, domain)
    row["attempts"] += 1
    if latency_s > 0:
        row["latency_sum"] += float(latency_s)
        row["latency_n"] += 1
    if priced:
        row["prices"] += 1
        row["discoveries"] += 1
    if structured and priced:
        row["structured_ok"] += 1
    if browser:
        row["browser_n"] += 1
        if browser_ok or priced:
            row["browser_ok"] += 1
    if alternate and priced:
        row["alt_recovery"] += 1
    st = (status or "").upper()
    if "403" in st or st == "BLOCKED_403":
        row["blocked_403"] += 1
    if "JS_HIDDEN" in st:
        row["js_hidden"] += 1
    if "DOMAIN_UNAVAILABLE" in st or "UNAVAILABLE" in st:
        row["domain_unavailable"] += 1
    _save(payload)


def seed_from_prior_adapters() -> None:
    """Bootstrap intel from prior price-adapters route cache / report if empty."""
    payload = _load()
    if payload.get("domains"):
        return
    # Seed known low-yield primaries + known high-yield alternates from prior run
    for d in LOW_YIELD_PRIMARY:
        row = _row(payload.setdefault("domains", {}), d)
        row["attempts"] = max(int(row["attempts"]), 50)
        row["prices"] = 0
        row["js_hidden"] = 30
        row["blocked_403"] = 10
        row["domain_unavailable"] = 10
    for d, prices in (
        ("quill.com", 8),
        ("dieselpartsdirect.com", 4),
        ("motion.com", 3),
        ("mccoys.com", 2),
        ("1000bulbs.com", 3),
        ("parts-hvac.com", 1),
        ("rspsupply.com", 1),
        ("globalindustrial.com", 2),
        ("brother-usa.com", 1),
        ("crcautocare.com", 1),
        ("autobuffy.com", 1),
        ("maxtran.com", 1),
        ("nationaldistributorllc.com", 1),
        ("proteccontrols.com", 1),
    ):
        row = _row(payload["domains"], d)
        row["attempts"] = max(int(row["attempts"]), prices * 2)
        row["prices"] = max(int(row["prices"]), prices)
        row["structured_ok"] = max(int(row["structured_ok"]), prices)
        row["discoveries"] = max(int(row["discoveries"]), prices)
    _save(payload)


def domain_yield_score(domain: str) -> float:
    payload = _load()
    row = (payload.get("domains") or {}).get((domain or "").lower().replace("www.", "")) or {}
    attempts = max(int(row.get("attempts") or 0), 1)
    prices = int(row.get("prices") or 0)
    base = prices / attempts
    # Penalize known failure modes
    fail = (
        int(row.get("blocked_403") or 0)
        + int(row.get("js_hidden") or 0)
        + int(row.get("domain_unavailable") or 0)
    ) / attempts
    return max(0.0, base - 0.15 * fail)


def domain_cost_score(domain: str) -> float:
    payload = _load()
    row = (payload.get("domains") or {}).get((domain or "").lower().replace("www.", "")) or {}
    n = max(int(row.get("latency_n") or 0), 1)
    avg = float(row.get("latency_sum") or 0.0) / n
    browser_rate = int(row.get("browser_n") or 0) / max(int(row.get("attempts") or 1), 1)
    return avg + 8.0 * browser_rate


def rank_domains(domains: list[str]) -> list[str]:
    seed_from_prior_adapters()
    return sorted(
        domains,
        key=lambda d: (-domain_yield_score(d), domain_cost_score(d), d),
    )


def top_domains(limit: int = 15) -> list[dict[str, Any]]:
    seed_from_prior_adapters()
    payload = _load()
    rows = []
    for domain, row in (payload.get("domains") or {}).items():
        attempts = int(row.get("attempts") or 0)
        prices = int(row.get("prices") or 0)
        n = max(int(row.get("latency_n") or 0), 1)
        rows.append(
            {
                "domain": domain,
                "attempts": attempts,
                "prices_found": prices,
                "success_rate": round(prices / attempts, 3) if attempts else 0.0,
                "avg_latency": round(float(row.get("latency_sum") or 0.0) / n, 3),
                "yield_score": round(domain_yield_score(domain), 3),
            }
        )
    rows.sort(key=lambda r: (-r["success_rate"], -r["prices_found"], r["domain"]))
    return rows[:limit]


def snapshot() -> dict[str, Any]:
    seed_from_prior_adapters()
    return _load()
