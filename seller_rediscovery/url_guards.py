"""URL identity guards for rediscovery candidates."""

from __future__ import annotations

import re


def reject_dwt6_wrong_page(url: str) -> bool:
    """Return True if URL is the known wrong Global Industrial DWT-6→DWT62 page."""
    u = (url or "").lower().rstrip("/")
    if "globalindustrial.com/p/dwt-6" in u:
        # Exact /p/dwt-6 (with optional trailing junk that still is the bad SKU page)
        path_end = u.split("globalindustrial.com", 1)[-1]
        if re.search(r"/p/dwt-6/?$", path_end) or "/p/dwt-6?" in path_end:
            return True
    if re.search(r"dwt-?62\b", u):
        return True
    return False
