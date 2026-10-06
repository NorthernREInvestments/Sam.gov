"""OpenGov portal enumeration + failure classification."""

from __future__ import annotations

import re
from typing import Any

from opengov_auth.states import (
    ANTI_BOT,
    AUTH_REQUIRED,
    BROKEN_PARSER,
    INVALID_PORTAL,
    MOVED,
    NO_OPEN_BIDS,
    PARTIAL,
    SOURCE_ERROR,
    WORKING,
)


def known_opengov_portals() -> list[dict[str, Any]]:
    """Union buyer catalog + registry OpenGov CDN portals."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        from discovery.platform_buyer_catalog import platform_buyer_catalog

        for row in platform_buyer_catalog():
            plat = str(row.get("platform") or row.get("platform_family") or "")
            url = str(row.get("portal_url") or "")
            if "opengov" not in plat.lower() and "opengov.com" not in url.lower():
                continue
            key = url.lower().rstrip("/")
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "entity_name": row.get("name"),
                    "entity_type": row.get("buyer_type") or row.get("entity_type"),
                    "state": row.get("state"),
                    "portal_url": url,
                    "platform_family": "OPENGOV",
                    "source": "platform_buyer_catalog",
                }
            )
    except Exception:
        pass

    try:
        from discovery.platform_adapters import load_registry

        reg = load_registry()
        for j in (reg.get("jurisdictions") or {}).values():
            if str(j.get("procurement_platform") or "") != "OpenGov":
                continue
            url = j.get("bid_portal") or j.get("procurement_page") or ""
            if "opengov.com" not in str(url).lower():
                # Prefer CDN URL when present; keep mirrors separately
                mirror = j.get("opengov_mirror")
                if mirror:
                    url = j.get("bid_portal")  # may still be non-cdn
            if not url:
                continue
            key = str(url).lower().rstrip("/")
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "entity_name": j.get("name") or j.get("jurisdiction_id"),
                    "entity_type": j.get("entity_type") or j.get("type"),
                    "state": j.get("state"),
                    "portal_url": url,
                    "platform_family": "OPENGOV",
                    "jurisdiction_id": j.get("jurisdiction_id"),
                    "source": "jurisdiction_registry",
                }
            )
    except Exception:
        pass
    return out


def is_opengov_url(url: str) -> bool:
    u = (url or "").lower()
    return "opengov.com" in u or "procurement.opengov" in u


def classify_portal_fetch(
    *,
    url: str,
    final_url: str | None,
    html: str | None,
    n_opps: int,
    parse_signals: bool,
    error: str | None = None,
    http_status: int | None = None,
) -> str:
    """Accurate portal status — do not call empty when parser failed."""
    body = html or ""
    low = body.lower()
    fu = (final_url or url or "").lower()

    if error:
        if "AUTH_CHALLENGE" in error or "challenge" in error.lower():
            return AUTH_REQUIRED
        return SOURCE_ERROR

    if http_status in {401, 403}:
        return AUTH_REQUIRED
    if http_status == 404 or "page not found" in low or "portal not found" in low:
        return INVALID_PORTAL
    if "just a moment" in low and ("cloudflare" in low or "cf-ray" in low):
        return ANTI_BOT
    if re.search(r"sign\s*in\s*to\s*(view|continue)|login\s*required", low) and n_opps == 0:
        return AUTH_REQUIRED

    # Moved: redirected off opengov or to a different host marketing page
    if final_url and "opengov.com" not in fu and "opengov.com" in (url or "").lower():
        return MOVED
    if re.search(r"this\s+portal\s+has\s+moved|relocated\s+to", low):
        return MOVED

    if n_opps > 0:
        return WORKING if n_opps >= 2 else PARTIAL

    # Zero opps — distinguish true empty vs broken parser
    empty_markers = re.search(
        r"(no\s+(current\s+)?(open\s+)?(bids?|projects?|solicitations?|opportunities?)|"
        r"there\s+are\s+no\s+(active|open)|0\s+results|no\s+results\s+found)",
        low,
    )
    structure_markers = bool(
        re.search(r"projectTitle|proposalDeadline|/projects/|OpenGov|solicitation", body, re.I)
    )
    if empty_markers and not parse_signals:
        return NO_OPEN_BIDS
    if structure_markers and not parse_signals and n_opps == 0:
        return BROKEN_PARSER
    if not structure_markers and n_opps == 0 and len(body) < 800:
        return INVALID_PORTAL
    if n_opps == 0 and empty_markers:
        return NO_OPEN_BIDS
    if n_opps == 0 and structure_markers:
        return BROKEN_PARSER
    return NO_OPEN_BIDS if n_opps == 0 else WORKING
