"""Phase L.2.9 — source inventory registry + health dashboard."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from application_clock import now_utc
from phase_l.source_roles import (
    AUTHORIZED_DISTRIBUTOR,
    COMPETITION,
    CURRENT_ACQUISITION_PRICE,
    CURRENT_COMMERCIAL_PRICE,
    CURRENT_GOV_CONTRACT_PRICE,
    HISTORICAL_AWARD,
    HISTORICAL_GOV_PRICE,
    LIVE_OPPORTUNITY,
    MPN_CROSS_REFERENCE,
    PRICE_LEAD_ONLY,
    PRODUCT_IDENTITY,
    RECURRING_BUY,
    SOURCE_AUTH_REQUIRED,
    SOURCE_BOT_BLOCKED,
    SOURCE_DEGRADED,
    SOURCE_DISABLED,
    SOURCE_HEALTHY,
    SOURCE_NO_RESULTS,
    SOURCE_PARSE_BROKEN,
    SOURCE_RESTRICTED,
)

ROOT = Path(__file__).resolve().parents[1]
HEALTH_PATH = ROOT / "data" / "phase_l29_source_health.json"


def _utc() -> str:
    return now_utc().isoformat()


# Explicit inventory: source → roles, access, format, categories
SOURCE_INVENTORY: list[dict[str, Any]] = [
    {
        "source": "SAM.gov",
        "family": "SAM",
        "roles": [LIVE_OPPORTUNITY, PRODUCT_IDENTITY],
        "access": "public_api_html",
        "format": "html/api",
        "auth": "none",
        "bot_behavior": "rate_limited",
        "categories": ["ALL"],
    },
    {
        "source": "USAspending",
        "family": "USAspending",
        "roles": [HISTORICAL_AWARD, HISTORICAL_GOV_PRICE],
        "access": "public_api",
        "format": "json",
        "auth": "none",
        "bot_behavior": "stable",
        "categories": ["ALL"],
    },
    {
        "source": "DLA/DIBBS",
        "family": "DLA/DIBBS",
        "roles": [LIVE_OPPORTUNITY, HISTORICAL_GOV_PRICE, PRODUCT_IDENTITY, MPN_CROSS_REFERENCE],
        "access": "public_html",
        "format": "html/pdf",
        "auth": "none_or_cac",
        "bot_behavior": "variable",
        "categories": ["MRO", "AERO"],
    },
    {
        "source": "GSA Advantage / eLibrary",
        "family": "GSA",
        "roles": [CURRENT_GOV_CONTRACT_PRICE, PRODUCT_IDENTITY],
        "access": "public_html",
        "format": "html",
        "auth": "none_for_catalog",
        "bot_behavior": "variable",
        "categories": ["IT", "MRO", "OFFICE"],
        "note": "Schedule price = GOVERNMENT_CHANNEL_PRICE not auto acquisition",
    },
    {
        "source": "NASA SEWP",
        "family": "SEWP",
        "roles": [CURRENT_GOV_CONTRACT_PRICE, HISTORICAL_GOV_PRICE, PRODUCT_IDENTITY],
        "access": "public_docs",
        "format": "html/pdf",
        "auth": "none_for_public",
        "bot_behavior": "stable",
        "categories": ["IT"],
    },
    {
        "source": "OMNIA Partners",
        "family": "OMNIA",
        "roles": [CURRENT_GOV_CONTRACT_PRICE, HISTORICAL_GOV_PRICE, PRODUCT_IDENTITY],
        "access": "public_html_docs",
        "format": "html/pdf/xlsx",
        "auth": "none_for_public_docs",
        "bot_behavior": "shell_possible",
        "categories": ["EQUIPMENT", "VEHICLE", "IT", "MRO"],
    },
    {
        "source": "Sourcewell",
        "family": "Sourcewell",
        "roles": [CURRENT_GOV_CONTRACT_PRICE, HISTORICAL_GOV_PRICE],
        "access": "public_html_docs",
        "format": "html/pdf/xlsx",
        "auth": "none_for_public",
        "bot_behavior": "timeout_prone",
        "categories": ["EQUIPMENT", "VEHICLE", "FLEET"],
    },
    {
        "source": "NASPO ValuePoint",
        "family": "NASPO",
        "roles": [CURRENT_GOV_CONTRACT_PRICE, AUTHORIZED_DISTRIBUTOR],
        "access": "public_html_docs",
        "format": "html/pdf",
        "auth": "none_for_public",
        "bot_behavior": "variable",
        "categories": ["IT", "VEHICLE", "TOOLS"],
    },
    {
        "source": "State term contracts",
        "family": "state term contracts",
        "roles": [CURRENT_GOV_CONTRACT_PRICE, HISTORICAL_GOV_PRICE, AUTHORIZED_DISTRIBUTOR],
        "access": "public_html_docs",
        "format": "html/pdf/xlsx/csv",
        "auth": "none_or_login_for_some",
        "bot_behavior": "portal_specific",
        "categories": ["ALL"],
    },
    {
        "source": "Local bid tabs / portals",
        "family": "local bid tabs",
        "roles": [HISTORICAL_GOV_PRICE, LIVE_OPPORTUNITY, COMPETITION],
        "access": "public_html",
        "format": "html/pdf",
        "auth": "often_auth_wall",
        "bot_behavior": "platform_specific",
        "categories": ["ALL"],
    },
    {
        "source": "Government board records",
        "family": "government board records",
        "roles": [HISTORICAL_GOV_PRICE, PRODUCT_IDENTITY, RECURRING_BUY],
        "access": "public_pdf_html",
        "format": "pdf/html",
        "auth": "none",
        "bot_behavior": "stable",
        "categories": ["VEHICLE", "EQUIPMENT", "IT"],
    },
    {
        "source": "Open-data portals",
        "family": "open-data datasets",
        "roles": [HISTORICAL_GOV_PRICE, RECURRING_BUY],
        "access": "public_api",
        "format": "json/csv",
        "auth": "none",
        "bot_behavior": "stable",
        "categories": ["ALL"],
    },
    {
        "source": "OEM storefronts",
        "family": "OEM",
        "roles": [CURRENT_COMMERCIAL_PRICE, PRODUCT_IDENTITY, CURRENT_ACQUISITION_PRICE],
        "access": "public_html",
        "format": "html",
        "auth": "none",
        "bot_behavior": "frequently_bot_blocked",
        "categories": ["IT", "EQUIPMENT", "VEHICLE", "TOOLS"],
    },
    {
        "source": "Dealers",
        "family": "dealers",
        "roles": [CURRENT_ACQUISITION_PRICE, CURRENT_COMMERCIAL_PRICE, AUTHORIZED_DISTRIBUTOR],
        "access": "public_html",
        "format": "html/pdf",
        "auth": "none",
        "bot_behavior": "variable",
        "categories": ["EQUIPMENT", "VEHICLE"],
    },
    {
        "source": "Distributors",
        "family": "distributors",
        "roles": [CURRENT_ACQUISITION_PRICE, CURRENT_COMMERCIAL_PRICE],
        "access": "public_html",
        "format": "html",
        "auth": "none",
        "bot_behavior": "frequently_bot_blocked",
        "categories": ["MRO", "IT", "LAB", "ELECTRONICS"],
    },
    {
        "source": "Static PDFs",
        "family": "static PDFs",
        "roles": [CURRENT_ACQUISITION_PRICE, CURRENT_GOV_CONTRACT_PRICE, HISTORICAL_GOV_PRICE, PRICE_LEAD_ONLY],
        "access": "public_http",
        "format": "pdf",
        "auth": "none",
        "bot_behavior": "usually_ok",
        "categories": ["ALL"],
    },
    {
        "source": "XLSX/CSV catalogs",
        "family": "XLSX/CSV",
        "roles": [CURRENT_ACQUISITION_PRICE, CURRENT_GOV_CONTRACT_PRICE, HISTORICAL_GOV_PRICE],
        "access": "public_http",
        "format": "xlsx/csv",
        "auth": "none",
        "bot_behavior": "usually_ok",
        "categories": ["ALL"],
    },
    {
        "source": "Secondary award aggregators",
        "family": "secondary award sources",
        "roles": [HISTORICAL_AWARD, PRICE_LEAD_ONLY],
        "access": "public_html",
        "format": "html",
        "auth": "often_paywall",
        "bot_behavior": "variable",
        "categories": ["ALL"],
        "note": "Supplemental only; reconcile to official source",
    },
]


def load_health() -> dict[str, Any]:
    if HEALTH_PATH.exists():
        try:
            return json.loads(HEALTH_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {"sources": {}, "updated_at": None}
    return {"sources": {}, "updated_at": None}


def save_health(data: dict[str, Any]) -> None:
    HEALTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = _utc()
    HEALTH_PATH.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def record_source_health(
    health: dict[str, Any],
    *,
    family: str,
    attempts: int = 0,
    exact_hits: int = 0,
    price_leads: int = 0,
    verified_prices: int = 0,
    history_hits: int = 0,
    blocks: int = 0,
    parse_failures: int = 0,
    auth_required: bool = False,
    restricted: bool = False,
    success: bool = False,
) -> None:
    sources = health.setdefault("sources", {})
    rec = sources.setdefault(
        family,
        {
            "attempts": 0,
            "exact_hits": 0,
            "price_leads": 0,
            "verified_prices": 0,
            "history_hits": 0,
            "blocks": 0,
            "parse_failures": 0,
            "auth_required": 0,
            "restricted": 0,
            "successes": 0,
            "last_success_at": None,
            "status": SOURCE_NO_RESULTS,
        },
    )
    rec["attempts"] += attempts
    rec["exact_hits"] += exact_hits
    rec["price_leads"] += price_leads
    rec["verified_prices"] += verified_prices
    rec["history_hits"] += history_hits
    rec["blocks"] += blocks
    rec["parse_failures"] += parse_failures
    if auth_required:
        rec["auth_required"] += 1
    if restricted:
        rec["restricted"] += 1
    if success:
        rec["successes"] += 1
        rec["last_success_at"] = _utc()
    rec["status"] = derive_status(rec)


def derive_status(rec: dict[str, Any]) -> str:
    attempts = max(1, int(rec.get("attempts") or 0))
    blocks = int(rec.get("blocks") or 0)
    auth = int(rec.get("auth_required") or 0)
    restricted = int(rec.get("restricted") or 0)
    parse_f = int(rec.get("parse_failures") or 0)
    successes = int(rec.get("successes") or 0)
    if restricted and successes == 0:
        return SOURCE_RESTRICTED
    if auth and successes == 0:
        return SOURCE_AUTH_REQUIRED
    if blocks / attempts >= 0.6:
        return SOURCE_BOT_BLOCKED
    if parse_f / attempts >= 0.5 and successes == 0:
        return SOURCE_PARSE_BROKEN
    if successes == 0 and attempts > 0:
        return SOURCE_NO_RESULTS
    if blocks / attempts >= 0.25 or parse_f > 0:
        return SOURCE_DEGRADED
    if successes > 0:
        return SOURCE_HEALTHY
    return SOURCE_DISABLED


def health_dashboard(health: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    health = health or load_health()
    sources = health.get("sources") or {}
    rows = []
    for inv in SOURCE_INVENTORY:
        fam = inv["family"]
        rec = sources.get(fam) or {}
        rows.append(
            {
                "source": inv["source"],
                "family": fam,
                "status": rec.get("status") or SOURCE_NO_RESULTS,
                "attempts": int(rec.get("attempts") or 0),
                "exact_hits": int(rec.get("exact_hits") or 0),
                "price_leads": int(rec.get("price_leads") or 0),
                "verified_prices": int(rec.get("verified_prices") or 0),
                "history_hits": int(rec.get("history_hits") or 0),
                "blocks": int(rec.get("blocks") or 0),
                "last_success_at": rec.get("last_success_at"),
                "roles": inv.get("roles"),
                "format": inv.get("format"),
                "auth": inv.get("auth"),
            }
        )
    return rows


def inventory_markdown_rows() -> list[dict[str, Any]]:
    return list(SOURCE_INVENTORY)
