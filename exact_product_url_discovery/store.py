"""Persistent ProductURLRecord store + domain URL pattern rules."""

from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import urlparse

from application_clock import now_utc
from exact_product_url_discovery.models import BUILD, DOMAIN_RULES, READY_FOR_PRICE_EXTRACTION, URL_DB
from m3_data_root import data_path


def _load_db() -> dict[str, Any]:
    p = data_path(URL_DB)
    if not p.exists():
        return {"build": BUILD, "records": {}, "updated_at": None}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"build": BUILD, "records": {}, "updated_at": None}


def _save_db(payload: dict[str, Any]) -> None:
    p = data_path(URL_DB)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload["build"] = BUILD
    payload["updated_at"] = time.time()
    p.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _key(manufacturer: str, mpn: str, url: str) -> str:
    from exact_product_url_discovery.normalize import norm_token

    host = urlparse(url).netloc.lower().replace("www.", "")
    return f"{norm_token(manufacturer)}|{norm_token(mpn)}|{host}|{url}"


def upsert_product_url(
    *,
    manufacturer: str,
    mpn: str,
    url: str,
    url_type: str,
    discovery_method: str,
    confidence: float,
    identity_match: bool,
    price_extractability: str = "UNKNOWN",
    benchmark_id: str | None = None,
    seller_domain: str | None = None,
    pack: Any = None,
    uom: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    db = _load_db()
    host = seller_domain or urlparse(url).netloc.lower().replace("www.", "")
    key = _key(manufacturer, mpn, url)
    rec = {
        "manufacturer": manufacturer,
        "mpn": mpn,
        "seller_domain": host,
        "url": url,
        "url_type": url_type,
        "discovery_method": discovery_method,
        "confidence": round(float(confidence), 3),
        "validated_date": now_utc().isoformat(),
        "identity_match": bool(identity_match),
        "price_extractability": price_extractability,
        "last_success": now_utc().isoformat() if identity_match else None,
        "benchmark_id": benchmark_id,
        "pack": pack,
        "uom": uom,
        "ready_status": READY_FOR_PRICE_EXTRACTION if identity_match and url_type == "EXACT_PRODUCT_VERIFIED" else "URL_DISCOVERY_PENDING",
        "extra": extra or {},
    }
    db.setdefault("records", {})[key] = rec
    _save_db(db)
    return rec


def list_ready_for_price(*, benchmark_ids: set[str] | None = None) -> list[dict[str, Any]]:
    db = _load_db()
    out = []
    for rec in (db.get("records") or {}).values():
        if rec.get("ready_status") != READY_FOR_PRICE_EXTRACTION:
            continue
        if benchmark_ids and rec.get("benchmark_id") not in benchmark_ids:
            continue
        out.append(rec)
    return out


def urls_for_benchmark(benchmark_id: str) -> list[dict[str, Any]]:
    db = _load_db()
    return [r for r in (db.get("records") or {}).values() if r.get("benchmark_id") == benchmark_id]


def load_domain_rules() -> dict[str, Any]:
    p = data_path(DOMAIN_RULES)
    if not p.exists():
        return {"build": BUILD, "domains": {}}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"build": BUILD, "domains": {}}


def note_domain_success(domain: str, url: str, *, mpn: str) -> None:
    """Learn successful URL patterns per domain (no hard-coded assumptions)."""
    payload = load_domain_rules()
    d = (domain or "").lower().replace("www.", "")
    row = payload.setdefault("domains", {}).setdefault(
        d,
        {
            "attempts": 0,
            "successes": 0,
            "patterns": [],
            "mpn_in_path_hits": 0,
            "examples": [],
        },
    )
    row["attempts"] = int(row.get("attempts") or 0) + 1
    row["successes"] = int(row.get("successes") or 0) + 1
    path = urlparse(url).path or ""
    # Capture structural pattern tokens
    tokens = []
    for part in ("/product/", "/products/", "/p/", "/item/", "/sku/", "/parts/", "/cbs/", "/shop/p/", "/pd/"):
        if part in path.lower():
            tokens.append(part)
    for t in tokens:
        if t not in row["patterns"]:
            row["patterns"].append(t)
    from exact_product_url_discovery.normalize import mpn_in_text

    if mpn_in_text(mpn, path):
        row["mpn_in_path_hits"] = int(row.get("mpn_in_path_hits") or 0) + 1
    if url not in row["examples"]:
        row["examples"] = (row.get("examples") or [])[-9:] + [url]
    p = data_path(DOMAIN_RULES)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload["build"] = BUILD
    payload["updated_at"] = time.time()
    p.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def note_domain_attempt(domain: str) -> None:
    payload = load_domain_rules()
    d = (domain or "").lower().replace("www.", "")
    row = payload.setdefault("domains", {}).setdefault(
        d, {"attempts": 0, "successes": 0, "patterns": [], "mpn_in_path_hits": 0, "examples": []}
    )
    row["attempts"] = int(row.get("attempts") or 0) + 1
    p = data_path(DOMAIN_RULES)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload["build"] = BUILD
    p.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def domain_yield_snapshot(limit: int = 25) -> list[dict[str, Any]]:
    payload = load_domain_rules()
    rows = []
    for domain, row in (payload.get("domains") or {}).items():
        att = max(int(row.get("attempts") or 0), 1)
        suc = int(row.get("successes") or 0)
        rows.append(
            {
                "domain": domain,
                "attempts": row.get("attempts", 0),
                "exact_urls": suc,
                "success_rate": round(100.0 * suc / att, 1),
                "patterns": row.get("patterns") or [],
            }
        )
    rows.sort(key=lambda r: (-r["exact_urls"], -r["success_rate"], r["domain"]))
    return rows[:limit]
