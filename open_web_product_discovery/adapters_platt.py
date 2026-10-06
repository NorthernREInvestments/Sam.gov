"""Platt.com GraphQL ProductSuggest adapter (public guest).

Exact catNum + manufacturer filters prevent near-MPN collisions.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from exact_product_url_discovery.normalize import is_short_mpn, mpn_variants, norm_token

_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Origin": "https://www.platt.com",
    "Referer": "https://www.platt.com/",
}

_CUSTOMERS_Q = """
query GetAccountCustomers($bannerCode: BannerCodeEnum!) {
  viewer(bannerCode: $bannerCode) {
    customers { nodes { customerId type isShoppable } }
  }
}
"""

_SUGGEST_Q = """
query ProductSuggest($bannerCode: BannerCodeEnum!, $customerId: ID!, $query: String) {
  viewer(bannerCode: $bannerCode) {
    customerById(customerId: $customerId) {
      suggest(query: $query) {
        all {
          __typename
          relevance
          text
          ... on ProductSuggestResponseLineProduct {
            product {
              summary {
                ... on Product {
                  productNumberFormatted
                  title
                  manufacturer { name }
                  catNum
                  urlInternal { routeId page slug }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""

_guest_customer_id: str | None = None
_SUGGEST_CACHE: dict[str, list[dict[str, Any]]] = {}
_LAST_CALL_TS: float = 0.0
_MIN_GAP_S = 0.55


def _throttle() -> None:
    import time

    global _LAST_CALL_TS
    gap = time.time() - _LAST_CALL_TS
    if gap < _MIN_GAP_S:
        time.sleep(_MIN_GAP_S - gap)
    _LAST_CALL_TS = time.time()


def _gql(client: httpx.Client, query: str, variables: dict[str, Any]) -> dict[str, Any]:
    r = client.post("https://www.platt.com/graphql", json={"query": query, "variables": variables})
    if r.status_code >= 400:
        return {}
    try:
        return r.json()
    except Exception:
        return {}


def _guest_id(client: httpx.Client) -> str | None:
    global _guest_customer_id
    if _guest_customer_id:
        return _guest_customer_id
    data = _gql(client, _CUSTOMERS_Q, {"bannerCode": "PLATT"})
    nodes = (((data.get("data") or {}).get("viewer") or {}).get("customers") or {}).get("nodes") or []
    for n in nodes:
        if n.get("customerId"):
            _guest_customer_id = str(n["customerId"])
            return _guest_customer_id
    return None


def _cat_match(mpn: str, cat_num: str) -> bool:
    a = norm_token(mpn)
    b = norm_token(cat_num)
    if not a or not b:
        return False
    if a == b:
        return True
    # allow hyphen/case variants already covered by norm_token
    return False


def _mfr_match(manufacturer: str | None, brand: str) -> bool:
    if not manufacturer:
        return True
    m0 = manufacturer.split()[0].lower()
    b = (brand or "").lower()
    if not m0:
        return True
    if m0 in b:
        return True
    # common aliases
    aliases = {
        "channellock": ["channellock"],
        "klein": ["klein"],
        "leviton": ["leviton"],
        "honeywell": ["honeywell", "resideo"],
        "resideo": ["resideo", "honeywell"],
        "ideal": ["ideal"],
        "hubbell": ["hubbell"],
    }
    for a in aliases.get(m0, []):
        if a in b:
            return True
    return False


def platt_suggest_urls(
    mpn: str,
    *,
    manufacturer: str | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Return exact-product candidate URLs from Platt ProductSuggest."""
    cache_key = f"{(manufacturer or '').strip().lower()}|{norm_token(mpn)}|{limit}"
    if cache_key in _SUGGEST_CACHE:
        return list(_SUGGEST_CACHE[cache_key])[:limit]

    out: list[dict[str, Any]] = []
    queries = []
    if manufacturer:
        queries.append(f"{manufacturer} {mpn}")
    queries.append(mpn)
    for v in mpn_variants(mpn)[:2]:
        if v not in queries:
            queries.append(v)

    with httpx.Client(timeout=18.0, headers=_UA, follow_redirects=True) as client:
        cid = _guest_id(client)
        if not cid:
            return []
        seen: set[str] = set()
        for q in queries[:3]:
            _throttle()
            data = _gql(
                client,
                _SUGGEST_Q,
                {"bannerCode": "PLATT", "customerId": cid, "query": q},
            )
            rows = (
                ((((data.get("data") or {}).get("viewer") or {}).get("customerById") or {}).get("suggest") or {}).get(
                    "all"
                )
                or []
            )
            for row in rows:
                if row.get("__typename") != "ProductSuggestResponseLineProduct":
                    continue
                summary = ((row.get("product") or {}).get("summary") or {})
                cat = str(summary.get("catNum") or "")
                if not _cat_match(mpn, cat):
                    continue
                brand = str(((summary.get("manufacturer") or {}).get("name")) or "")
                if not _mfr_match(manufacturer, brand):
                    # short MPN without brand match is dangerous
                    if is_short_mpn(mpn) or manufacturer:
                        continue
                ui = summary.get("urlInternal") or {}
                route_id = ui.get("routeId")
                slug = ui.get("slug")
                if not route_id or not slug:
                    continue
                url = f"https://www.platt.com/p/{route_id}/{slug}"
                if url in seen:
                    continue
                seen.add(url)
                out.append(
                    {
                        "url": url,
                        "domain": "platt.com",
                        "title": str(summary.get("title") or "")[:160],
                        "cat_num": cat,
                        "manufacturer": brand,
                        "discovery_method": "PLATT_GRAPHQL_SUGGEST",
                        "confidence": 0.9,
                        "sku": str(summary.get("productNumberFormatted") or route_id),
                    }
                )
                if len(out) >= limit:
                    _SUGGEST_CACHE[cache_key] = list(out)
                    return out
    _SUGGEST_CACHE[cache_key] = list(out)
    return out


def platt_url_pattern_learn(url: str) -> dict[str, str] | None:
    m = re.search(r"platt\.com/p/([^/]+)/(.+)$", url)
    if not m:
        return None
    return {"pattern": "/p/{routeId}/{slug}", "route_id": m.group(1), "slug": m.group(2)}
