"""Per-route / per-domain circuit breakers for public price search.

Build: 20261004-m3-price-search-reliability-v1

A single provider failure must never disable all live pricing.
"""

from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import urlparse

from m3_data_root import data_path

BUILD = "20261004-m3-price-search-reliability-v1"

# Independent route families
ROUTE_FAMILIES = (
    "SEARCH_PROVIDER_A",  # Bing
    "SEARCH_PROVIDER_B",  # DuckDuckGo / alternate
    "MANUFACTURER_SEARCH",
    "DISTRIBUTOR_SEARCH",
    "RESELLER_SEARCH",
    "DIRECT_CATALOG",
    "STRUCTURED_PRODUCT_DATA",
    "KNOWN_DOMAIN_SEARCH",
    "PUBLIC_CATALOG_PDF",
    "CACHED_PRODUCT_INDEX",
)

_STATE: dict[str, dict[str, Any]] = {}
_FAIL_OPEN_AFTER = 3
_COOLDOWN_SEC = 900  # 15 minutes
# These routes must stay available globally — only per-domain cooldowns apply.
_DOMAIN_ONLY_ROUTES = {
    "MANUFACTURER_SEARCH",
    "DISTRIBUTOR_SEARCH",
    "RESELLER_SEARCH",
    "DIRECT_CATALOG",
    "STRUCTURED_PRODUCT_DATA",
    "KNOWN_DOMAIN_SEARCH",
    "PUBLIC_CATALOG_PDF",
    "CACHED_PRODUCT_INDEX",
}


def _now() -> float:
    return time.time()


def _key(route: str, domain: str | None = None) -> str:
    d = (domain or "").lower().strip()
    return f"{route}|{d}" if d else route


def _load_persisted() -> None:
    global _STATE
    if _STATE:
        return
    path = data_path("m3_price_route_circuits.json")
    if not path.exists():
        _STATE = {r: _fresh(r) for r in ROUTE_FAMILIES}
        return
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        _STATE = raw.get("routes") or {}
    except Exception:
        _STATE = {}
    for r in ROUTE_FAMILIES:
        _STATE.setdefault(r, _fresh(r))


def _fresh(route: str) -> dict[str, Any]:
    return {
        "route": route,
        "status": "HEALTHY",
        "failure_count": 0,
        "success_count": 0,
        "last_failure": None,
        "cooldown_until": None,
        "reason": None,
        "fallback_route": _fallback(route),
        "domains": {},
    }


def _fallback(route: str) -> str | None:
    order = list(ROUTE_FAMILIES)
    try:
        i = order.index(route)
    except ValueError:
        return "DIRECT_CATALOG"
    for nxt in order[i + 1 :] + order[:i]:
        if nxt != route:
            return nxt
    return None


def persist() -> None:
    _load_persisted()
    path = data_path("m3_price_route_circuits.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"build": BUILD, "routes": _STATE, "updated_at": _now()}, indent=2, default=str),
        encoding="utf-8",
    )


def reset_all() -> None:
    global _STATE
    _STATE = {r: _fresh(r) for r in ROUTE_FAMILIES}
    persist()


def domain_of(url: str | None) -> str | None:
    if not url:
        return None
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host or None


def is_open(route: str, *, domain: str | None = None) -> bool:
    _load_persisted()
    st = _STATE.setdefault(route, _fresh(route))
    now = _now()
    # Domain-scoped cooldown
    if domain:
        d = st.setdefault("domains", {}).setdefault(
            domain, {"failure_count": 0, "cooldown_until": None, "status": "HEALTHY", "reason": None}
        )
        cd = d.get("cooldown_until")
        if cd and now < float(cd):
            return True
        if cd and now >= float(cd):
            d["cooldown_until"] = None
            d["status"] = "RETRYABLE"
            d["failure_count"] = 0
    cd = st.get("cooldown_until")
    if cd and now < float(cd):
        return True
    if cd and now >= float(cd):
        st["cooldown_until"] = None
        st["status"] = "RETRYABLE"
        st["failure_count"] = 0
    return False


def note(
    route: str,
    *,
    ok: bool,
    domain: str | None = None,
    reason: str | None = None,
) -> None:
    _load_persisted()
    st = _STATE.setdefault(route, _fresh(route))
    if ok:
        st["success_count"] = int(st.get("success_count") or 0) + 1
        st["failure_count"] = 0
        st["reason"] = None
        if st.get("status") != "CIRCUIT_OPEN" or not st.get("cooldown_until"):
            st["status"] = "HEALTHY"
        if domain:
            d = st.setdefault("domains", {}).setdefault(
                domain, {"failure_count": 0, "cooldown_until": None, "status": "HEALTHY", "reason": None}
            )
            d["failure_count"] = 0
            d["status"] = "HEALTHY"
            d["reason"] = None
            d["cooldown_until"] = None
        return

    st["failure_count"] = int(st.get("failure_count") or 0) + 1
    st["last_failure"] = _now()
    st["reason"] = reason
    if domain:
        d = st.setdefault("domains", {}).setdefault(
            domain, {"failure_count": 0, "cooldown_until": None, "status": "HEALTHY", "reason": None}
        )
        d["failure_count"] = int(d.get("failure_count") or 0) + 1
        d["reason"] = reason
        if d["failure_count"] >= _FAIL_OPEN_AFTER:
            d["cooldown_until"] = _now() + _COOLDOWN_SEC
            d["status"] = "CIRCUIT_OPEN"
        else:
            d["status"] = "RETRYABLE"
    # Never globally disable manufacturer/distributor/catalog — domain cooldown only.
    if route in _DOMAIN_ONLY_ROUTES:
        st["status"] = "DEGRADED" if st["failure_count"] >= 2 else "RETRYABLE"
        st["cooldown_until"] = None
        return
    if st["failure_count"] >= _FAIL_OPEN_AFTER:
        st["cooldown_until"] = _now() + _COOLDOWN_SEC
        st["status"] = "CIRCUIT_OPEN"
    elif int(st.get("success_count") or 0) == 0:
        st["status"] = "BLOCKED" if st["failure_count"] >= 2 else "RETRYABLE"
    else:
        st["status"] = "DEGRADED"


def status_of(route: str, *, domain: str | None = None) -> str:
    if is_open(route, domain=domain):
        return "CIRCUIT_OPEN"
    _load_persisted()
    st = _STATE.get(route) or _fresh(route)
    if domain:
        d = (st.get("domains") or {}).get(domain) or {}
        if d.get("status"):
            return str(d["status"])
    return str(st.get("status") or "HEALTHY")


def snapshot() -> dict[str, Any]:
    _load_persisted()
    out = {}
    for r in ROUTE_FAMILIES:
        st = _STATE.get(r) or _fresh(r)
        out[r] = {
            "status": status_of(r),
            "failure_count": st.get("failure_count"),
            "success_count": st.get("success_count"),
            "last_failure": st.get("last_failure"),
            "cooldown_until": st.get("cooldown_until"),
            "reason": st.get("reason"),
            "fallback_route": st.get("fallback_route"),
            "open_domains": [
                d
                for d, info in (st.get("domains") or {}).items()
                if info.get("status") == "CIRCUIT_OPEN"
            ][:20],
        }
    return out


# Friendly aliases used in reports
REPORT_ALIASES = {
    "Search provider A": "SEARCH_PROVIDER_A",
    "Search provider B": "SEARCH_PROVIDER_B",
    "Manufacturer": "MANUFACTURER_SEARCH",
    "Distributor": "DISTRIBUTOR_SEARCH",
    "Reseller": "RESELLER_SEARCH",
    "Direct catalog": "DIRECT_CATALOG",
    "Structured data": "STRUCTURED_PRODUCT_DATA",
    "JS-rendered": "DIRECT_CATALOG",
    "Catalog/PDF": "PUBLIC_CATALOG_PDF",
}


def report_health() -> dict[str, str]:
    snap = snapshot()
    return {label: snap.get(key, {}).get("status", "HEALTHY") for label, key in REPORT_ALIASES.items()}
