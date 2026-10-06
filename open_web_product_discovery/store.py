"""Persist product URL records + domain pattern rules + seller graph."""

from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import urlparse

from application_clock import now_utc
from m3_data_root import data_path
from open_web_product_discovery.models import (
    BUILD,
    DOMAIN_RULES,
    READY_FOR_PRICE_EXTRACTION,
    SELLER_GRAPH,
    URL_DB,
)
from price_adapters.validate import seller_of


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
    pack: str | None = None,
    uom: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    db = _load(URL_DB) or {"build": BUILD, "records": {}}
    db.setdefault("records", {})
    domain = seller_domain or seller_of(url)
    key = f"{(manufacturer or '').upper()}|{(mpn or '').upper()}|{domain}|{url}"
    now = now_utc().isoformat()
    rec = {
        "manufacturer": manufacturer,
        "mpn": mpn,
        "seller_domain": domain,
        "url": url,
        "url_type": url_type,
        "discovery_method": discovery_method,
        "confidence": confidence,
        "validated_date": now,
        "identity_match": identity_match,
        "price_extractability": price_extractability,
        "last_success": now if identity_match else None,
        "benchmark_id": benchmark_id,
        "pack": pack,
        "uom": uom,
        "ready_status": READY_FOR_PRICE_EXTRACTION if identity_match else "URL_DISCOVERY_PENDING",
        "extra": extra or {},
    }
    db["records"][key] = rec
    db["updated_at"] = time.time()
    db["build"] = BUILD
    _save(URL_DB, db)
    if identity_match and domain:
        learn_domain_pattern(domain, url, mpn=mpn, success=True)
        note_seller_graph(manufacturer, domain, url=url, method=discovery_method)
    return rec


def list_ready_for_price(*, benchmark_ids: set[str] | None = None) -> list[dict[str, Any]]:
    db = _load(URL_DB) or {}
    out = []
    for rec in (db.get("records") or {}).values():
        if rec.get("ready_status") != READY_FOR_PRICE_EXTRACTION:
            continue
        if benchmark_ids is not None and rec.get("benchmark_id") not in benchmark_ids:
            continue
        out.append(rec)
    return out


def learn_domain_pattern(domain: str, url: str, *, mpn: str = "", success: bool = True) -> None:
    rules = _load(DOMAIN_RULES) or {"build": BUILD, "domains": {}}
    rules.setdefault("domains", {})
    host = (domain or "").lower().replace("www.", "")
    row = rules["domains"].setdefault(
        host,
        {"patterns": [], "examples": [], "success_count": 0, "failure_count": 0, "mpn_placement": []},
    )
    path = urlparse(url).path or ""
    # crude pattern: keep path segments replacing mpn token with {mpn}
    pat = path
    if mpn:
        for tok in {mpn, mpn.lower(), mpn.upper(), mpn.replace("-", "")}:
            if tok and tok in path:
                pat = path.replace(tok, "{mpn}")
                break
    if success:
        row["success_count"] = int(row.get("success_count") or 0) + 1
        if pat and pat not in row["patterns"]:
            row["patterns"] = (row.get("patterns") or [])[:12] + [pat]
        examples = row.setdefault("examples", [])
        if url not in examples:
            examples.append(url)
            row["examples"] = examples[-15:]
    else:
        row["failure_count"] = int(row.get("failure_count") or 0) + 1
    rules["updated_at"] = now_utc().isoformat()
    _save(DOMAIN_RULES, rules)


def note_seller_graph(manufacturer: str, domain: str, *, url: str = "", method: str = "") -> None:
    g = _load(SELLER_GRAPH) or {"build": BUILD, "manufacturers": {}}
    g.setdefault("manufacturers", {})
    m = (manufacturer or "?").strip() or "?"
    row = g["manufacturers"].setdefault(m, {"sellers": {}})
    sellers = row.setdefault("sellers", {})
    s = sellers.setdefault(
        domain,
        {"domain": domain, "hits": 0, "methods": {}, "urls": [], "authorization_evidence": "observed_product_page"},
    )
    s["hits"] = int(s.get("hits") or 0) + 1
    if method:
        s["methods"][method] = int((s.get("methods") or {}).get(method) or 0) + 1
    if url and url not in (s.get("urls") or []):
        s["urls"] = (s.get("urls") or [])[-8:] + [url]
    g["updated_at"] = now_utc().isoformat()
    _save(SELLER_GRAPH, g)


def domain_yield_snapshot(limit: int = 15) -> list[dict[str, Any]]:
    rules = _load(DOMAIN_RULES) or {}
    db = _load(URL_DB) or {}
    counts: dict[str, dict[str, Any]] = {}
    for rec in (db.get("records") or {}).values():
        d = rec.get("seller_domain") or "?"
        counts.setdefault(d, {"domain": d, "attempts": 0, "candidates": 0, "validated": 0})
        counts[d]["validated"] = int(counts[d]["validated"]) + (1 if rec.get("identity_match") else 0)
    for d, row in (rules.get("domains") or {}).items():
        counts.setdefault(d, {"domain": d, "attempts": 0, "candidates": 0, "validated": 0})
        counts[d]["attempts"] = int(row.get("success_count") or 0) + int(row.get("failure_count") or 0)
        counts[d]["patterns"] = row.get("patterns") or []
    out = list(counts.values())
    for r in out:
        att = int(r.get("attempts") or 0) or int(r.get("validated") or 0)
        val = int(r.get("validated") or 0)
        r["success_rate"] = round(100.0 * val / att, 1) if att else (100.0 if val else 0.0)
    out.sort(key=lambda r: (-int(r.get("validated") or 0), -float(r.get("success_rate") or 0), r["domain"]))
    return out[:limit]
