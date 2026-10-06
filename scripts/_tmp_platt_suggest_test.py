"""Extract and test Platt ProductSuggest GraphQL."""
from __future__ import annotations

import json
import httpx

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Origin": "https://www.platt.com",
    "Referer": "https://www.platt.com/",
}

CUSTOMERS_Q = """
query GetAccountCustomers($bannerCode: BannerCodeEnum!) {
  viewer(bannerCode: $bannerCode) {
    customers {
      nodes { customerId type isShoppable }
    }
  }
}
"""

SUGGEST_Q = """
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


def gql(c: httpx.Client, query: str, variables: dict) -> dict:
    r = c.post(
        "https://www.platt.com/graphql",
        json={"query": query, "variables": variables},
    )
    r.raise_for_status()
    return r.json()


with httpx.Client(timeout=20.0, headers=UA, follow_redirects=True) as c:
    cust = gql(c, CUSTOMERS_Q, {"bannerCode": "PLATT"})
    nodes = (((cust.get("data") or {}).get("viewer") or {}).get("customers") or {}).get("nodes") or []
    print("customers", nodes)
    cid = nodes[0]["customerId"] if nodes else None
    for q in ["11055", "430", "5320-S", "D2000-9NEAT", "LF777M2-QT", "121943", "TH8320U1008"]:
        data = gql(c, SUGGEST_Q, {"bannerCode": "PLATT", "customerId": cid, "query": q})
        all_ = (
            ((((data.get("data") or {}).get("viewer") or {}).get("customerById") or {}).get("suggest") or {}).get("all")
            or []
        )
        products = [x for x in all_ if x.get("__typename") == "ProductSuggestResponseLineProduct"]
        print("==", q, "n", len(products))
        for p in products[:3]:
            s = ((p.get("product") or {}).get("summary") or {})
            ui = s.get("urlInternal") or {}
            url = f"https://www.platt.com/p/{ui.get('routeId')}/{ui.get('slug')}" if ui.get("routeId") and ui.get("slug") else None
            print(" ", s.get("catNum"), s.get("title"), (s.get("manufacturer") or {}).get("name"), url)
