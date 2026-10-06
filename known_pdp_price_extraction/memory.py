"""Domain extraction memory + endpoint persistence."""

from __future__ import annotations

import json
from typing import Any

from application_clock import now_utc
from m3_data_root import data_path
from known_pdp_price_extraction.models import DOMAIN_MEMORY, ENDPOINT_DB


def _load(name: str) -> dict[str, Any]:
    p = data_path(name)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(name: str, payload: dict[str, Any]) -> None:
    p = data_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def note_domain_extraction(
    domain: str,
    *,
    method: str,
    success: bool,
    url: str | None = None,
    endpoint: str | None = None,
    latency_s: float | None = None,
) -> None:
    db = _load(DOMAIN_MEMORY) or {"domains": {}, "build": "20261005-m3-known-pdp-price-extraction-v2"}
    row = db.setdefault("domains", {}).setdefault(
        domain,
        {
            "domain": domain,
            "success_count": 0,
            "failure_count": 0,
            "methods": {},
            "endpoints": [],
            "pdp_patterns": [],
            "last_verified": None,
            "latencies": [],
        },
    )
    if success:
        row["success_count"] = int(row.get("success_count") or 0) + 1
    else:
        row["failure_count"] = int(row.get("failure_count") or 0) + 1
    row["methods"][method] = int((row.get("methods") or {}).get(method) or 0) + (1 if success else 0)
    if endpoint and endpoint not in row["endpoints"]:
        row["endpoints"].append(endpoint)
        row["endpoints"] = row["endpoints"][-12:]
    if url:
        from urllib.parse import urlparse

        path = urlparse(url).path
        parts = [x for x in path.split("/") if x]
        if parts:
            pat = f"/{parts[0]}/…/{parts[-1]}" if len(parts) > 1 else f"/{parts[0]}"
            if pat not in row["pdp_patterns"]:
                row["pdp_patterns"].append(pat)
                row["pdp_patterns"] = row["pdp_patterns"][-10:]
    if latency_s is not None:
        row.setdefault("latencies", []).append(round(latency_s, 2))
        row["latencies"] = row["latencies"][-20:]
        row["average_latency"] = round(sum(row["latencies"]) / len(row["latencies"]), 2)
    attempts = max(1, int(row["success_count"]) + int(row["failure_count"]))
    row["success_rate"] = round(100.0 * int(row["success_count"]) / attempts, 1)
    row["last_verified"] = now_utc().isoformat()
    _save(DOMAIN_MEMORY, db)


def persist_endpoint(domain: str, endpoint: str, *, method: str) -> None:
    db = _load(ENDPOINT_DB) or {"endpoints": [], "build": "20261005-m3-known-pdp-price-extraction-v2"}
    rows = db.setdefault("endpoints", [])
    key = f"{domain}|{endpoint}"
    existing = {f"{r.get('domain')}|{r.get('endpoint')}": r for r in rows}
    if key in existing:
        existing[key]["hits"] = int(existing[key].get("hits") or 0) + 1
        existing[key]["last_verified"] = now_utc().isoformat()
    else:
        rows.append(
            {
                "domain": domain,
                "endpoint": endpoint,
                "method": method,
                "hits": 1,
                "last_verified": now_utc().isoformat(),
            }
        )
    db["endpoints"] = list(existing.values()) if existing else rows
    # rebuild from dict if we mutated existing
    if existing:
        db["endpoints"] = list(existing.values())
    _save(ENDPOINT_DB, db)


def domain_memory_snapshot(top_n: int = 25) -> list[dict[str, Any]]:
    db = _load(DOMAIN_MEMORY)
    rows = list((db.get("domains") or {}).values())
    rows.sort(key=lambda r: (-int(r.get("success_count") or 0), -float(r.get("success_rate") or 0)))
    return rows[:top_n]
