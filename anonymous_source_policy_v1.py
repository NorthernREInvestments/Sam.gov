"""Hard policy: BidNet and Euna abandoned for discovery/retrieval.

Owner may reverse only by explicit env override after this module defaults to abandon.
"""

from __future__ import annotations

import os
from typing import Any


def _truthy(raw: str | None, default: bool = False) -> bool:
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def bidnet_abandoned() -> bool:
    """Default TRUE — BidNet discovery/auth/harvest disabled unless owner re-enables."""
    # Explicit re-enable requires BOTH abandon=false AND auth enabled.
    if _truthy(os.environ.get("M3_BIDNET_ABANDONED"), default=True):
        return True
    return not _truthy(os.environ.get("BIDNET_AUTH_ENABLED"), default=False) and _truthy(
        os.environ.get("M3_FORCE_BIDNET_OFF"), default=False
    )


def euna_abandoned() -> bool:
    """Default TRUE — Euna/Bonfire paid/account workflows disabled."""
    return _truthy(os.environ.get("M3_EUNA_ABANDONED"), default=True)


def is_bidnet_source_id(source_id: str | None) -> bool:
    sid = str(source_id or "").lower()
    return (
        "bidnet" in sid
        or sid.startswith("network_bidnet_")
        or sid.startswith("live_bidnet")
        or sid == "bidnet"
    )


def is_bidnet_platform(platform: str | None) -> bool:
    p = str(platform or "").lower().replace(" ", "")
    return p in {"bidnet", "bidnetdirect"} or "bidnet" in p


def is_euna_platform(platform: str | None) -> bool:
    p = str(platform or "").lower().replace(" ", "")
    return p in {"euna", "bonfire", "euna/bonfire", "eunabonfire"} or "bonfire" in p or "euna" in p


def filter_out_abandoned_sources(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop BidNet/Euna rows from a discovery source selection list."""
    out: list[dict[str, Any]] = []
    drop_bn = bidnet_abandoned()
    drop_eu = euna_abandoned()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        sid = str(row.get("source_id") or "")
        plat = str(row.get("platform_family") or row.get("platform") or "")
        adapter = str(row.get("adapter_family") or "")
        if drop_bn and (
            is_bidnet_source_id(sid)
            or is_bidnet_platform(plat)
            or "bidnet" in adapter.lower()
            or "bidnetdirect.com" in str(row.get("list_url") or "").lower()
        ):
            continue
        if drop_eu and (is_euna_platform(plat) or "bonfire" in adapter.lower() or "euna" in adapter.lower()):
            continue
        out.append(row)
    return out


def policy_status() -> dict[str, Any]:
    return {
        "kind": "AnonymousSourcePolicy",
        "bidnet_abandoned": bidnet_abandoned(),
        "euna_abandoned": euna_abandoned(),
        "allowed": [
            "OpenGov (preserve/improve)",
            "SAM.gov (budgeted)",
            "DLA/DIBBS",
            "PlanetBids public (when not WAF-blocked)",
            "Socrata/CKAN/ArcGIS open data",
            "State/local public HTML boards",
            "IonWave / Jaggaer public event pages",
        ],
        "forbidden": [
            "BidNet auth/discovery/harvest/PG",
            "Euna/Bonfire account workflows",
            "Paid subscriptions without owner approval",
            "CAPTCHA solving / auth bypass",
        ],
        "env": {
            "M3_BIDNET_ABANDONED": os.environ.get("M3_BIDNET_ABANDONED", "(default true)"),
            "M3_EUNA_ABANDONED": os.environ.get("M3_EUNA_ABANDONED", "(default true)"),
            "BIDNET_AUTH_ENABLED": os.environ.get("BIDNET_AUTH_ENABLED", ""),
            "EUNA_AUTH_ENABLED": os.environ.get("EUNA_AUTH_ENABLED", ""),
        },
    }
