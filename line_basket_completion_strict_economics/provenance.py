"""Price provenance audit — reject sentinel/fixture/search-URL noise."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from line_basket_completion_strict_economics.models import (
    BUILD,
    FIXTURE,
    MIN_EXECUTABLE_PRICE,
    OTHER_INVALID,
    PRIOR_FUNNEL_CK,
    PROVENANCE_AUDIT,
    PUBLIC_CURRENT,
    SENTINEL,
    SENTINEL_PRICES,
    STALE,
    VALID_PRODUCTION_PRICE,
    WRONG_IDENTITY,
    WRONG_PACK,
)
from m3_data_root import data_path

_SEARCH_URL = re.compile(r"(?:/search(?:\?|/|$)|[?&](?:q|query|searchQuery|keywords|Ntt|searchterm)=)", re.I)
_FIXTURE_MARKERS = re.compile(r"(fixture|test[_-]?price|placeholder|synthetic|mock|dummy|sentinel)", re.I)


def _load(name: str) -> dict[str, Any]:
    p = data_path(name)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(name: str, payload: dict[str, Any]) -> None:
    p = data_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _f(v: Any) -> float | None:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except Exception:
        return None


def classify_price_record(row: dict[str, Any]) -> dict[str, Any]:
    """Classify one acquisition-cost / priced-line record."""
    price = _f(row.get("unit_cost") if row.get("unit_cost") is not None else row.get("unit_price") or row.get("price"))
    url = str(row.get("source_url") or row.get("url") or "")
    seller = str(row.get("seller") or row.get("domain") or "")
    origin = str(row.get("price_origin") or row.get("research_route") or "")
    blob = " ".join(str(row.get(k) or "") for k in ("note", "failure_reason", "research_route", "extraction_route", "source"))

    verdict = VALID_PRODUCTION_PRICE
    reasons: list[str] = []

    if price is None:
        verdict = OTHER_INVALID
        reasons.append("missing_price")
    elif price in SENTINEL_PRICES or abs(price - 1.0) < 1e-9 or abs(price) < 1e-9:
        verdict = SENTINEL
        reasons.append("sentinel_0_or_1")
    elif price < MIN_EXECUTABLE_PRICE:
        verdict = SENTINEL
        reasons.append(f"below_min_executable_{MIN_EXECUTABLE_PRICE}")

    if url and _SEARCH_URL.search(url):
        verdict = OTHER_INVALID if verdict == VALID_PRODUCTION_PRICE else verdict
        reasons.append("search_url_not_pdp")
        if verdict == VALID_PRODUCTION_PRICE:
            verdict = OTHER_INVALID

    if _FIXTURE_MARKERS.search(blob) or _FIXTURE_MARKERS.search(origin):
        verdict = FIXTURE
        reasons.append("fixture_or_test_marker")

    # Stale: retrieved_date older than 90 days when present
    retrieved = row.get("retrieved_at") or row.get("retrieved_date") or row.get("priced_at")
    if retrieved and verdict == VALID_PRODUCTION_PRICE:
        try:
            ts = datetime.fromisoformat(str(retrieved).replace("Z", "+00:00"))
            age_days = (datetime.now(timezone.utc) - ts.astimezone(timezone.utc)).days
            if age_days > 90:
                verdict = STALE
                reasons.append(f"age_days_{age_days}")
        except Exception:
            pass

    # Wrong pack signals
    if str(row.get("pack_status") or "").upper() in {"WRONG", "MISMATCH", "UNRESOLVED"}:
        verdict = WRONG_PACK
        reasons.append("pack_mismatch")

    if str(row.get("identity_status") or "").upper() in {"WRONG", "MISMATCH"}:
        verdict = WRONG_IDENTITY
        reasons.append("identity_mismatch")

    # Cache-only advancedtruckparts $1 pattern already caught; also reject bare domain search hosts without product path
    try:
        path = urlparse(url).path or ""
        if url and ("/product" not in path.lower() and "/p/" not in path.lower() and "/item" not in path.lower()):
            if "/search" in path.lower() or not path or path == "/":
                if verdict == VALID_PRODUCTION_PRICE:
                    verdict = OTHER_INVALID
                    reasons.append("non_product_url_path")
    except Exception:
        pass

    price_origin = None
    if verdict == VALID_PRODUCTION_PRICE:
        if "manufacturer" in origin.lower():
            price_origin = "MANUFACTURER_CURRENT"
        elif "distributor" in origin.lower() or "authorized" in origin.lower():
            price_origin = "AUTHORIZED_DISTRIBUTOR_CURRENT"
        elif "quote" in origin.lower():
            price_origin = "SUPPLIER_QUOTE"
        else:
            price_origin = PUBLIC_CURRENT

    return {
        "verdict": verdict,
        "reasons": reasons,
        "unit_cost": price,
        "seller": seller or None,
        "source_url": url or None,
        "price_origin": price_origin,
        "is_valid_production": verdict == VALID_PRODUCTION_PRICE,
    }


def audit_prior_priced_lines(opportunity_ids: list[str] | None = None) -> dict[str, Any]:
    """Audit every previously priced line from funnel checkpoint (+ acq cache)."""
    funnel = _load(PRIOR_FUNNEL_CK)
    ops = funnel.get("opportunities") or {}
    idset = set(opportunity_ids) if opportunity_ids else set(ops.keys())

    rows: list[dict[str, Any]] = []
    for oid, o in ops.items():
        if oid not in idset:
            continue
        for ln in ((o.get("lines") or {}).get("lines") or []):
            if ln.get("terminal_state") != "PRICED_EXECUTABLE":
                continue
            audit = classify_price_record(ln)
            rows.append(
                {
                    "opportunity_id": oid,
                    "line_key": ln.get("line_key"),
                    "mpn": ln.get("mpn"),
                    "description": (ln.get("description") or "")[:160],
                    "prior_unit_cost": ln.get("unit_cost"),
                    "seller": ln.get("seller"),
                    "source_url": ln.get("source_url"),
                    "research_route": ln.get("research_route"),
                    **audit,
                }
            )

    counts = {
        VALID_PRODUCTION_PRICE: 0,
        SENTINEL: 0,
        FIXTURE: 0,
        STALE: 0,
        WRONG_IDENTITY: 0,
        WRONG_PACK: 0,
        OTHER_INVALID: 0,
    }
    for r in rows:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1

    payload = {
        "build": BUILD,
        "previously_priced_lines": len(rows),
        "counts": counts,
        "valid_production_prices": counts[VALID_PRODUCTION_PRICE],
        "sentinel": counts[SENTINEL],
        "fixture": counts[FIXTURE],
        "stale": counts[STALE],
        "wrong_identity": counts[WRONG_IDENTITY],
        "wrong_pack": counts[WRONG_PACK],
        "other_invalid": counts[OTHER_INVALID],
        "rows": rows,
        "note": "Search-URL and $0/$1 records are never executable production evidence.",
    }
    _save(PROVENANCE_AUDIT, payload)
    return payload


def is_executable_price(row: dict[str, Any]) -> bool:
    return bool(classify_price_record(row).get("is_valid_production"))
