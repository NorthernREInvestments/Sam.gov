"""PlanetBids adapter audit — status without building a full new connector."""

from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger("govtracker.planetbids_audit")

WORKING = "WORKING"
PARTIAL = "PARTIAL"
AUTH_REQUIRED = "AUTH_REQUIRED"
BROKEN = "BROKEN"
NOT_IMPLEMENTED = "NOT_IMPLEMENTED"


def _adapter_present() -> bool:
    try:
        from discovery.live_fetchers import PlanetBidsLiveFetcher

        return PlanetBidsLiveFetcher is not None
    except Exception:
        return False


def _load_l173_results() -> dict[str, Any] | None:
    """Best-effort load of prior L.17.3 PlanetBids validation artifact."""
    candidates = (
        "data/phase_l173_planetbids_results.json",
        "artifacts/phase_l173_planetbids_results.json",
        "docs/phase_l173_planetbids.md",
    )
    from pathlib import Path

    root = Path(__file__).resolve().parent
    for rel in candidates:
        path = root / rel
        if not path.exists():
            continue
        if path.suffix == ".json":
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
        # Markdown embeds a JSON block — parse first fenced block if present
        try:
            text = path.read_text(encoding="utf-8")
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                return json.loads(text[start : end + 1])
        except Exception:
            continue
    return None


def planetbids_audit_summary() -> dict[str, Any]:
    adapter_ok = _adapter_present()
    l173 = _load_l173_results() or {}
    access_counts = dict(l173.get("access_state_counts") or {})
    mapped = int(l173.get("mapped") or 0)
    tested = int(l173.get("tested") or 0)
    live_rows = int(l173.get("live_rows") or 0)
    ingestion = int(l173.get("ingestion_active") or 0)

    working = int(access_counts.get("WORKING") or access_counts.get("WORKING_PUBLIC") or 0)
    partial = int(access_counts.get("PARTIAL") or 0)
    auth_req = int(access_counts.get("AUTH_REQUIRED") or 0)
    broken = int(
        access_counts.get("ADAPTER_FAILED")
        or access_counts.get("BROKEN")
        or access_counts.get("SOURCE_ERROR")
        or 0
    )

    if not adapter_ok:
        overall = NOT_IMPLEMENTED
        category = "NOT_IMPLEMENTED"
    elif working > 0 and live_rows > 0:
        overall = WORKING
        category = "FREE_ACTIVE"
    elif adapter_ok and (partial > 0 or mapped > 0):
        overall = PARTIAL
        category = "FREE_PARTIAL"
    elif broken and not working:
        overall = BROKEN
        category = "BROKEN"
    else:
        overall = PARTIAL
        category = "FREE_PARTIAL"

    return {
        "kind": "PlanetBidsAudit",
        "platform": "PlanetBids",
        "adapter_present": adapter_ok,
        "overall_state": overall,
        "coverage_category": category,
        "mapped": mapped,
        "tested": tested,
        "working": working,
        "partial": partial,
        "auth_required": auth_req,
        "broken": broken,
        "live_rows": live_rows,
        "ingestion_active": ingestion,
        "portal_family_patterns": [
            "{agency}.planetbids.com",
            "pbsystem.planetbids.com",
            "vendors.planetbids.com/portal/{id}/bo/bo-search",
        ],
        "search_model": "AGENCY_SPECIFIC",  # no confirmed free global national search
        "free_vendor_login": "OFTEN_REQUIRED_FOR_DOCS",
        "document_access": "UNKNOWN_OFTEN_GATED",
        "pagination": "PORTAL_DEPENDENT",
        "notes": (
            "Existing PlanetBidsLiveFetcher parses HTML bid title links. "
            "L.17.3 sample mapped CA county portals mostly ADAPTER_FAILED (DNS/host). "
            "No full connector rebuild in this phase — fix reachable portals next."
        ),
        "l173_access_state_counts": access_counts,
    }
