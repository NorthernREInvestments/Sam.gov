"""Helpers to inspect public Socrata catalogs (canonical; used by jobs/verify)."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from m3.research.http_util import fetch_json


def fetch_socrata_rows(resource_url: str, *, limit: int = 5) -> dict[str, Any]:
    sep = "&" if "?" in resource_url else "?"
    url = f"{resource_url}{sep}$limit={int(limit)}"
    status, final, data = fetch_json(url)
    if status >= 400 or data is None:
        return {"ok": False, "status": status, "url": final, "error": "http_or_parse"}
    if not isinstance(data, list):
        return {"ok": False, "status": status, "url": final, "error": f"unexpected:{type(data)}"}
    return {
        "ok": True,
        "status": status,
        "url": final,
        "count": len(data),
        "keys": list(data[0].keys()) if data else [],
        "sample": data[:3],
    }


def search_socrata_views(domain: str, query: str, *, limit: int = 8) -> dict[str, Any]:
    url = f"https://{domain}/api/views.json?$q={quote(query)}&$limit={int(limit)}"
    status, final, data = fetch_json(url)
    if status >= 400 or data is None:
        return {"ok": False, "status": status, "url": final, "error": "http_or_parse"}
    views = data if isinstance(data, list) else []
    out = []
    for v in views[:limit]:
        if not isinstance(v, dict):
            continue
        vid = v.get("id")
        out.append(
            {
                "id": vid,
                "name": v.get("name"),
                "attribution": v.get("attribution"),
                "viewType": v.get("viewType"),
                "resource": f"https://{domain}/resource/{vid}.json" if vid else None,
            }
        )
    return {"ok": True, "domain": domain, "views": out}
