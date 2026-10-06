"""Euna/Bonfire portal enumeration from buyer catalog."""

from __future__ import annotations

import re
from typing import Any


def known_euna_portals() -> list[dict[str, Any]]:
    """Bonfirehub public portals + catalog rows tagged Bonfire/Euna."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        from discovery.platform_buyer_catalog import platform_buyer_catalog

        for row in platform_buyer_catalog():
            plat = str(row.get("platform") or row.get("platform_family") or "")
            url = str(row.get("portal_url") or "")
            if "bonfire" not in plat.lower() and "euna" not in plat.lower() and "bonfirehub" not in url.lower():
                continue
            # Prefer real Bonfire hub URLs for public harvest
            if "bonfirehub.com" not in url.lower():
                continue
            key = url.lower().rstrip("/")
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "entity_name": row.get("name"),
                    "entity_type": row.get("buyer_type") or row.get("entity_type"),
                    "state": row.get("state"),
                    "portal_url": url,
                    "platform_family": "EUNA_BONFIRE",
                    "source": "platform_buyer_catalog",
                }
            )
    except Exception:
        pass
    return out


def is_bonfire_url(url: str) -> bool:
    u = (url or "").lower()
    return "bonfirehub.com" in u or "gobonfire.com" in u


def classify_bonfire_fetch(*, html: str, n_opps: int, http_status: int | None = None) -> str:
    low = (html or "").lower()
    if http_status in {401, 403}:
        return "ANTI_BOT" if "cloudflare" in low or "just a moment" in low else "AUTH_REQUIRED"
    if http_status == 404:
        return "INVALID_PORTAL"
    if "just a moment" in low and "cloudflare" in low:
        return "ANTI_BOT"
    if n_opps > 0:
        return "WORKING_PUBLIC"
    if re.search(r"no\s+open\s+opportunit|0\s+opportunit|no\s+results", low):
        return "NO_OPEN_BIDS"
    if re.search(r"log\s*in|sign\s*in", low) and n_opps == 0:
        return "AUTH_REQUIRED"
    return "BROKEN_PARSER" if "bonfire" in low or "opportunity" in low else "SOURCE_ERROR"
