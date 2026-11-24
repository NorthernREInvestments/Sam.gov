"""Verified vs unverified/restricted procurement source registry helpers.

A source is verified only when it retrieves legitimate solicitation records
(or correctly reports zero active records) — not merely HTTP 200.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from m3.config import get_settings
from m3.models import SourceRegistry
from m3.research.adapters import search_georgia_gpr, search_html_links, search_socrata
from m3.research.nextbid_controller import acquisition_locked

VERIFIED = "verified"
UNVERIFIED = "unverified_or_restricted"


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def registry_paths() -> dict[str, Path]:
    root = get_settings().data_root() / "source_registries"
    root.mkdir(parents=True, exist_ok=True)
    return {
        "root": root,
        "verified": root / "verified_sources.json",
        "unverified": root / "unverified_or_restricted_sources.json",
        "index": root / "index.json",
    }


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def probe_source(src: SourceRegistry | dict[str, Any], *, sample_limit: int = 50) -> dict[str, Any]:
    """Live probe one free source. Never calls metered NextBid."""
    if isinstance(src, SourceRegistry):
        payload = {
            "source_key": src.source_key,
            "base_url": src.base_url,
            "name": src.name,
            "config": src.config or {},
            "adapter": src.adapter,
            "access_cost": src.access_cost,
        }
    else:
        payload = dict(src)
        payload.setdefault("config", {})

    key = payload.get("source_key") or "unknown"
    adapter = payload.get("adapter") or ""
    access = payload.get("access_cost") or "free"
    out: dict[str, Any] = {
        "source_key": key,
        "name": payload.get("name"),
        "adapter": adapter,
        "access_cost": access,
        "official_url": (payload.get("config") or {}).get("official_listing_url")
        or payload.get("base_url"),
        "geo_state": (payload.get("config") or {}).get("geo_state"),
        "probed_at": _utc(),
        "status": UNVERIFIED,
        "records_retrieved": 0,
        "active_like": 0,
        "pagination_complete": False,
        "reason": None,
        "sample_titles": [],
        "capability_level": (payload.get("config") or {}).get("capability_level"),
    }

    if access == "paid_nextbid" or adapter == "nextbid":
        out["reason"] = "nextbid_locked" if acquisition_locked() else "paid_nextbid_not_used_in_probe"
        out["access_requirements"] = "NEXTBID_API_KEY + owner unlock"
        return out

    if adapter == "supplier_page":
        out["reason"] = "supplier_directory_not_procurement_feed"
        return out

    try:
        if adapter == "socrata_contracts":
            result = search_socrata(
                payload,
                keywords=None,
                limit=sample_limit,
                page_size=min(100, sample_limit),
                max_pages=1,
                apply_adapter_keywords=False,
            )
        elif adapter == "georgia_gpr":
            result = search_georgia_gpr(payload, limit=sample_limit)
        elif adapter == "html_link_harvest":
            result = search_html_links(
                payload, keywords=None, limit=sample_limit, apply_adapter_keywords=False
            )
        else:
            out["reason"] = f"unsupported_adapter:{adapter}"
            return out
    except Exception as exc:  # noqa: BLE001 — probe must isolate failures
        out["reason"] = f"{type(exc).__name__}: {exc}"[:300]
        return out

    items = list(result.get("items") or [])
    # HTML harvest often returns nav/footer links — require solicitation-like signals
    if adapter == "html_link_harvest":
        import re

        sol_like = []
        for it in items:
            title = str(it.get("title") or "")
            sol = str(it.get("solicitation_number") or "")
            url = str(it.get("official_url") or "")
            if sol or re.search(
                r"(?i)\b(rfp|rfq|ifb|itb|rfi|solicitation|invitation\s+to\s+bid|"
                r"request\s+for\s+(?:proposal|quote|bid)|bid\s*#|opportunity)\b",
                f"{title} {url}",
            ):
                sol_like.append(it)
        out["html_raw_links"] = len(items)
        items = sol_like

    out["records_retrieved"] = len(items)
    out["pagination_complete"] = bool(result.get("pagination_complete"))
    out["more_available"] = bool(result.get("more_available"))
    out["rows_examined"] = result.get("rows_examined")
    out["http_ok"] = bool(result.get("ok"))
    out["errors"] = result.get("errors") or []
    out["sample_titles"] = [str(i.get("title") or "")[:120] for i in items[:8]]
    active = 0
    for it in items:
        st = str(it.get("solicitation_status") or "").lower()
        if st in {"open", "amended", "upcoming", "unknown"} or not st:
            active += 1
    out["active_like"] = active

    # Federal feeds remain parked until owner authorization
    blob = f"{key} {payload.get('name') or ''} {payload.get('base_url') or ''}".lower()
    if "sam.gov" in blob or "dibbs" in blob or "sam_live" in blob:
        out["status"] = UNVERIFIED
        out["reason"] = "federal_sam_or_dibbs_disabled_until_authorized"
        out["future_investigation_status"] = "parked"
        return out

    if result.get("ok") and len(items) > 0:
        out["status"] = VERIFIED
        out["reason"] = "retrieved_legitimate_solicitation_records"
        cfg_level = int((payload.get("config") or {}).get("capability_level") or 2)
        out["capability_level"] = max(cfg_level, 2 if adapter != "html_link_harvest" else 1)
    elif result.get("ok") and len(items) == 0 and adapter == "socrata_contracts":
        # Zero active can still be a verified operational open-data feed
        out["status"] = VERIFIED
        out["reason"] = "reachable_zero_active_records_in_sample"
        out["capability_level"] = max(1, int((payload.get("config") or {}).get("capability_level") or 1))
    elif result.get("ok") and len(items) == 0 and adapter == "html_link_harvest":
        out["reason"] = "html_portal_reachable_but_no_solicitation_like_links"
        out["future_investigation_status"] = "queued"
    else:
        out["reason"] = ";".join(out["errors"]) or "no_records_and_not_ok"
        out["access_requirements"] = "public_http_or_open_data"
        out["possible_free_alternative"] = None
        out["future_investigation_status"] = "queued"
    return out


def verify_enabled_free_sources(db: Session, *, sample_limit: int = 50) -> dict[str, Any]:
    """Probe all enabled free non-supplier sources; persist dual registries."""
    rows = (
        db.query(SourceRegistry)
        .filter(SourceRegistry.access_cost == "free")
        .filter(SourceRegistry.is_enabled.is_(True))
        .filter(SourceRegistry.adapter != "supplier_page")
        .all()
    )
    verified: list[dict] = []
    unverified: list[dict] = []
    for row in rows:
        result = probe_source(row, sample_limit=sample_limit)
        cfg = dict(row.config or {})
        # Commercial aggregators are not official purchasing feeds
        host = (row.base_url or "").lower()
        officialish = any(
            x in host
            for x in (
                ".gov",
                ".us",
                "socrata.com",
                "lacity.org",
                "montgomerycountymd.gov",
                "delaware.gov",
                "sfgov.org",
                "sf.gov",
                "cityofnewyork.us",
                "cookcountyil.gov",
                "mesaaz.gov",
                "cambridgema.gov",
                "illinois.gov",
                "doas.state.ga.us",
            )
        )
        if result["status"] == VERIFIED and not officialish and row.adapter == "html_link_harvest":
            result["status"] = UNVERIFIED
            result["reason"] = "non_official_aggregator_or_unverified_host"
            result["future_investigation_status"] = "queued"
        if result["status"] == VERIFIED:
            cfg["last_verification"] = result
            cfg["operational"] = True
            if result.get("capability_level") is not None:
                cfg["capability_level"] = result["capability_level"]
            row.experimentally_verified = True
            row.config = cfg
            # Keep acquisition enabled for verified feeds
            row.is_enabled = True
            db.add(row)
            verified.append(result)
        else:
            cfg["last_verification"] = result
            cfg["operational"] = False
            row.config = cfg
            # HTML portals without solicitation evidence stay registered but out of acquire path
            if row.adapter == "html_link_harvest":
                row.is_enabled = False
            db.add(row)
            unverified.append(result)
    db.commit()

    paths = registry_paths()
    _write(
        paths["verified"],
        {"updated_at": _utc(), "count": len(verified), "sources": verified},
    )
    _write(
        paths["unverified"],
        {"updated_at": _utc(), "count": len(unverified), "sources": unverified},
    )
    index = {
        "updated_at": _utc(),
        "verified_count": len(verified),
        "unverified_count": len(unverified),
        "purchasing_authorities_verified": sorted(
            {s.get("name") for s in verified if s.get("name")}
        ),
        "states_touched": sorted(
            {s.get("geo_state") for s in verified if s.get("geo_state")}
        ),
        "paths": {k: str(v) for k, v in paths.items() if k != "root"},
    }
    _write(paths["index"], index)
    return index
