"""Persistent ProductSourceGraph store."""

from __future__ import annotations

import json
import time
from typing import Any

from manufacturer_distributor_graph.models import GRAPH_STORE, URL_MEMORY, DOMAIN_ROLES, LOW_YIELD_PRIMARY, BUILD
from m3_data_root import data_path


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


def load_graph() -> dict[str, Any]:
    g = _load(GRAPH_STORE)
    if not g:
        g = {"build": BUILD, "products": {}, "manufacturers": {}, "edges": [], "updated_at": None}
    return g


def save_graph(g: dict[str, Any]) -> None:
    g["updated_at"] = time.time()
    g["build"] = BUILD
    _save(GRAPH_STORE, g)


def upsert_product(g: dict[str, Any], item: dict[str, Any], *, mfr_res: dict[str, Any]) -> str:
    bid = item.get("benchmark_id") or f"{item.get('manufacturer')}-{item.get('mpn')}"
    products = g.setdefault("products", {})
    products[bid] = {
        "benchmark_id": bid,
        "manufacturer": item.get("manufacturer") or mfr_res.get("manufacturer"),
        "brand": item.get("brand") or item.get("manufacturer"),
        "mpn": item.get("mpn") or item.get("part_number"),
        "normalized_mpn": (item.get("mpn") or "").upper().replace(" ", ""),
        "category": item.get("category"),
        "product_family": item.get("product_family"),
        "manufacturer_resolved": mfr_res,
        "seller_paths": products.get(bid, {}).get("seller_paths") or [],
        "exact_urls": products.get(bid, {}).get("exact_urls") or [],
        "prices": products.get(bid, {}).get("prices") or [],
    }
    mk = mfr_res.get("manufacturer_key")
    if mk:
        mfrs = g.setdefault("manufacturers", {})
        mfrs[mk] = {
            "key": mk,
            "domain": mfr_res.get("manufacturer_domain"),
            "locator": mfr_res.get("manufacturer_distributor_locator"),
            "confidence": mfr_res.get("confidence"),
        }
    return str(bid)


def add_edge(g: dict[str, Any], edge: dict[str, Any]) -> None:
    edges = g.setdefault("edges", [])
    edges.append({**edge, "ts": time.time()})


def add_seller_path(g: dict[str, Any], bid: str, path: dict[str, Any]) -> None:
    prod = (g.get("products") or {}).get(bid)
    if not prod:
        return
    paths = prod.setdefault("seller_paths", [])
    key = (path.get("domain"), path.get("url"))
    if any((p.get("domain"), p.get("url")) == key for p in paths):
        return
    paths.append(path)


def add_exact_url(g: dict[str, Any], bid: str, url_row: dict[str, Any]) -> None:
    prod = (g.get("products") or {}).get(bid)
    if not prod:
        return
    urls = prod.setdefault("exact_urls", [])
    if any(u.get("url") == url_row.get("url") for u in urls):
        return
    urls.append(url_row)


def add_price(g: dict[str, Any], bid: str, price_row: dict[str, Any]) -> None:
    prod = (g.get("products") or {}).get(bid)
    if not prod:
        return
    prod.setdefault("prices", []).append(price_row)


def load_url_memory() -> dict[str, Any]:
    m = _load(URL_MEMORY)
    if not m:
        m = {"domains": {}, "updated_at": None}
    return m


def save_url_memory(m: dict[str, Any]) -> None:
    m["updated_at"] = time.time()
    _save(URL_MEMORY, m)


def note_url_pattern(domain: str, *, url: str, mpn: str, success: bool, method: str) -> None:
    mem = load_url_memory()
    domains = mem.setdefault("domains", {})
    d = (domain or "").lower().replace("www.", "")
    row = domains.setdefault(
        d,
        {
            "attempts": 0,
            "successes": 0,
            "search_patterns": [],
            "product_url_patterns": [],
            "mpn_transforms": {},
            "case_norm": "preserve",
            "success_rate": 0.0,
        },
    )
    row["attempts"] += 1
    if success:
        row["successes"] += 1
        # learn path token
        from urllib.parse import urlparse

        path = urlparse(url).path or ""
        if path and path not in row["product_url_patterns"]:
            # store template-ish path with mpn replaced
            tmpl = path
            for form in {mpn, mpn.lower(), mpn.upper(), mpn.replace("-", "")}:
                if form and form in tmpl:
                    tmpl = tmpl.replace(form, "{mpn}")
            if "{mpn}" in tmpl and tmpl not in row["product_url_patterns"]:
                row["product_url_patterns"].append(tmpl[:120])
        if method and method not in row["search_patterns"]:
            row["search_patterns"].append(method)
    row["success_rate"] = round(row["successes"] / max(row["attempts"], 1), 3)
    save_url_memory(mem)


def ensure_domain_roles() -> dict[str, Any]:
    roles = _load(DOMAIN_ROLES)
    if not roles.get("domains"):
        roles = {"build": BUILD, "domains": dict(LOW_YIELD_PRIMARY)}
        # high-yield defaults
        for d in (
            "quill.com",
            "dieselpartsdirect.com",
            "1000bulbs.com",
            "motion.com",
            "mccoys.com",
            "nationaldistributorllc.com",
            "parts-hvac.com",
            "rspsupply.com",
            "platt.com",
            "homedepot.com",
            "mscdirect.com",
            "bradyid.com",
            "rockauto.com",
            "summitracing.com",
            "autozone.com",
            "crcautocare.com",
            "pexuniverse.com",
        ):
            roles["domains"][d] = "PRICE_SOURCE"
        _save(DOMAIN_ROLES, roles)
    return roles


def domain_role(domain: str) -> str:
    roles = ensure_domain_roles()
    d = (domain or "").lower().replace("www.", "")
    return (roles.get("domains") or {}).get(d, "PRICE_SOURCE")


def graph_metrics(g: dict[str, Any]) -> dict[str, Any]:
    products = g.get("products") or {}
    mfrs = g.get("manufacturers") or {}
    edges = g.get("edges") or []
    dist_domains = set()
    reseller_domains = set()
    exact_urls = 0
    ge3 = 0
    ge1_price = 0
    locators = 0
    for m in mfrs.values():
        if m.get("locator"):
            locators += 1
    for e in edges:
        et = e.get("edge_type") or ""
        dom = e.get("domain") or e.get("distributor_domain") or e.get("to")
        if "DISTRIBUTOR" in et or "AUTHORIZED" in et:
            if dom:
                dist_domains.add(dom)
        if "RESELLER" in et or "CATALOG" in et or "PRODUCT" in et:
            if dom:
                reseller_domains.add(dom)
    for p in products.values():
        exact_urls += len(p.get("exact_urls") or [])
        n_paths = len(p.get("seller_paths") or [])
        if n_paths >= 3:
            ge3 += 1
        if p.get("prices"):
            ge1_price += 1
        for sp in p.get("seller_paths") or []:
            d = sp.get("domain")
            if d:
                reseller_domains.add(d)
    return {
        "manufacturers_resolved": len(mfrs),
        "manufacturer_domains": sum(1 for m in mfrs.values() if m.get("domain")),
        "distributor_locators_found": locators,
        "authorized_distributors_discovered": sum(
            1 for e in edges if "AUTHORIZED" in str(e.get("edge_type") or "")
        ),
        "unique_distributor_domains": len(dist_domains),
        "unique_reseller_domains": len(reseller_domains),
        "exact_product_urls_discovered": exact_urls,
        "products_with_ge3_seller_paths": ge3,
        "products_with_ge1_priceable_path": ge1_price,
        "n_products": len(products),
        "n_edges": len(edges),
    }
