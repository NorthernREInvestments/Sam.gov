"""Domain / manufacturer product-family pattern memory."""

from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import quote_plus

from application_clock import now_utc
from m3_data_root import data_path
from price_14_expand_patterns.models import BUILD, PATTERN_DB, ROUTE_MEMORY, SEED_FAMILY_ROUTES
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


def ensure_pattern_db() -> dict[str, Any]:
    db = _load(PATTERN_DB)
    if not db.get("adapters"):
        db = {
            "build": BUILD,
            "adapters": {f"{a['domain']}|{a['manufacturer_family']}": a for a in SEED_FAMILY_ROUTES},
            "updated_at": now_utc().isoformat(),
        }
        _save(PATTERN_DB, db)
    return db


def learn_success(
    *,
    manufacturer: str,
    category: str | None,
    domain: str,
    discovery_route: str,
    price_route: str,
    url: str,
    mpn: str,
) -> None:
    db = ensure_pattern_db()
    adapters = db.setdefault("adapters", {})
    key = f"{domain}|{(manufacturer or '?').split()[0]}"
    row = adapters.setdefault(
        key,
        {
            "manufacturer_family": manufacturer,
            "manufacturers": [manufacturer],
            "categories": [category] if category else [],
            "domain": domain,
            "discovery_route": discovery_route,
            "price_route": price_route,
            "pdp_pattern": "",
            "success_count": 0,
            "failure_count": 0,
            "examples": [],
        },
    )
    row["success_count"] = int(row.get("success_count") or 0) + 1
    row["discovery_route"] = discovery_route or row.get("discovery_route")
    row["price_route"] = price_route or row.get("price_route")
    if category and category not in (row.get("categories") or []):
        row.setdefault("categories", []).append(category)
    # crude path pattern with mpn placeholder
    path = url.split(domain)[-1] if domain in url else url
    for tok in {mpn, mpn.lower(), mpn.upper(), mpn.replace("-", "")}:
        if tok and tok in path:
            path = path.replace(tok, "{mpn}")
            break
    row["pdp_pattern"] = path[:120]
    examples = row.setdefault("examples", [])
    if url not in examples:
        examples.append(url)
        row["examples"] = examples[-12:]
    db["updated_at"] = now_utc().isoformat()
    db["build"] = BUILD
    _save(PATTERN_DB, db)

    mem = _load(ROUTE_MEMORY) or {"build": BUILD, "routes": {}}
    routes = mem.setdefault("routes", {})
    mkey = (manufacturer or "?").strip() or "?"
    r = routes.setdefault(
        mkey,
        {
            "manufacturer": mkey,
            "preferred_domains": [],
            "by_domain": {},
            "last_verified": None,
        },
    )
    if domain not in r["preferred_domains"]:
        r["preferred_domains"].insert(0, domain)
        r["preferred_domains"] = r["preferred_domains"][:6]
    drow = r["by_domain"].setdefault(
        domain,
        {"domain": domain, "hits": 0, "discovery_routes": {}, "price_routes": {}},
    )
    drow["hits"] = int(drow.get("hits") or 0) + 1
    drow["discovery_routes"][discovery_route] = int((drow.get("discovery_routes") or {}).get(discovery_route) or 0) + 1
    drow["price_routes"][price_route] = int((drow.get("price_routes") or {}).get(price_route) or 0) + 1
    r["last_verified"] = now_utc().isoformat()
    mem["updated_at"] = time.time()
    mem["build"] = BUILD
    _save(ROUTE_MEMORY, mem)


def learn_failure(domain: str, manufacturer: str) -> None:
    db = ensure_pattern_db()
    key = f"{domain}|{(manufacturer or '?').split()[0]}"
    row = (db.get("adapters") or {}).get(key)
    if row:
        row["failure_count"] = int(row.get("failure_count") or 0) + 1
        _save(PATTERN_DB, db)


def preferred_domains_for_item(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Return ordered domain adapters for sibling discovery."""
    ensure_pattern_db()
    db = _load(PATTERN_DB) or {}
    mfr = str(item.get("manufacturer") or "")
    mfr0 = mfr.split()[0] if mfr else ""
    cat = str(item.get("category") or "").lower()
    scored: list[tuple[float, dict[str, Any]]] = []
    for row in (db.get("adapters") or {}).values():
        score = 0.0
        manuf = [str(x) for x in (row.get("manufacturers") or [])]
        if any(mfr0 and mfr0.lower() in str(m).lower() for m in manuf):
            score += 5.0
        if cat and cat in [str(c).lower() for c in (row.get("categories") or [])]:
            score += 2.0
        score += 0.5 * int(row.get("success_count") or 0)
        score -= 0.2 * int(row.get("failure_count") or 0)
        if score > 0:
            scored.append((score, row))
    # also seed routes not yet in DB
    for row in SEED_FAMILY_ROUTES:
        manuf = [str(x) for x in (row.get("manufacturers") or [])]
        if any(mfr0 and mfr0.lower() in str(m).lower() for m in manuf) or (
            cat and cat in [str(c).lower() for c in (row.get("categories") or [])]
        ):
            scored.append((3.0, row))
    scored.sort(key=lambda x: -x[0])
    # dedupe domain
    seen: set[str] = set()
    out = []
    for _, row in scored:
        d = row.get("domain")
        if not d or d in seen:
            continue
        seen.add(d)
        out.append(row)
    return out[:5]


def quill_search_url(mpn: str, manufacturer: str | None = None) -> str:
    q = f"{manufacturer or ''} {mpn}".strip()
    return f"https://www.quill.com/search?keywords={quote_plus(q)}"


def pattern_snapshot() -> dict[str, Any]:
    db = ensure_pattern_db()
    mem = _load(ROUTE_MEMORY) or {}
    return {
        "domains_learned": sorted({(a.get("domain") or "") for a in (db.get("adapters") or {}).values() if a.get("domain")}),
        "adapter_count": len(db.get("adapters") or {}),
        "adapters": list((db.get("adapters") or {}).values())[:40],
        "route_memory_manufacturers": sorted((mem.get("routes") or {}).keys()),
    }
