"""Probe Platt public GraphQL for product search."""
from __future__ import annotations

import json
import httpx

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Origin": "https://www.platt.com",
    "Referer": "https://www.platt.com/search?q=11055",
}

queries = [
    # common ecommerce patterns
    {
        "query": "query { __typename }",
    },
    {
        "query": '{ __schema { queryType { name fields { name } } } }',
    },
]

with httpx.Client(timeout=15.0, headers=UA, follow_redirects=True) as c:
    # GET probe
    r = c.get("https://www.platt.com/graphql")
    print("GET", r.status_code, r.text[:200])
    for q in queries:
        r = c.post("https://www.platt.com/graphql", json=q)
        print("POST", r.status_code, r.text[:500])

# Also try known Nuxt commerce search APIs
candidates = [
    "https://www.platt.com/api/catalog/vue/v1/products?searchCriteria[filter_groups][0][filters][0][field]=sku&searchCriteria[filter_groups][0][filters][0][value]=11055",
    "https://www.platt.com/rest/V1/products?searchCriteria[requestName]=quick_search_container&searchCriteria[filter_groups][0][filters][0][field]=search_term&searchCriteria[filter_groups][0][filters][0][value]=11055",
    "https://www.platt.com/search-api?q=11055",
]
with httpx.Client(timeout=12.0, headers=UA, follow_redirects=True) as c:
    for u in candidates:
        try:
            r = c.get(u)
            print("API", r.status_code, u.split("?")[0][-40:], r.text[:180].replace("\n", " "))
        except Exception as e:
            print("API ERR", type(e).__name__, u[:60])
