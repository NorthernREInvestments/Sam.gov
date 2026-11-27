"""On-demand USAspending.gov queries — free API, no historical warehouse.

Contract-level award amounts are NOT product unit prices. Callers must treat
results as leads for further document/comprehension research unless line-item
evidence is separately retrieved and verified.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from urllib import error, request

USASPENDING_SPENDING_BY_AWARD = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
USASPENDING_DOCS = "https://api.usaspending.gov/docs/endpoints"
USASPENDING_UI = "https://www.usaspending.gov/"


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def search_awards_by_keyword(
    keywords: list[str],
    *,
    start_date: str = "2020-01-01",
    end_date: str | None = None,
    limit: int = 10,
    timeout: float = 40.0,
) -> dict[str, Any]:
    """Targeted keyword search against USAspending spending_by_award (free)."""
    end = end_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    kws = [k.strip() for k in keywords if k and str(k).strip()]
    if not kws:
        return {
            "ok": False,
            "items": [],
            "errors": ["no_keywords"],
            "pricing_note": "USAspending returns award totals, not unit prices.",
        }
    body = {
        "filters": {
            "keywords": kws[:5],
            "time_period": [{"start_date": start_date, "end_date": end}],
            "award_type_codes": ["A", "B", "C", "D"],
        },
        "fields": [
            "Award ID",
            "Recipient Name",
            "Award Amount",
            "Description",
            "Awarding Agency",
            "Start Date",
            "End Date",
            "NAICS Code",
            "PSC Code",
        ],
        "page": 1,
        "limit": max(1, min(int(limit), 25)),
        "sort": "Award Amount",
        "order": "desc",
    }
    req = request.Request(
        USASPENDING_SPENDING_BY_AWARD,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            raw = json.loads(resp.read().decode("utf-8", errors="replace"))
            status = getattr(resp, "status", 200)
    except error.HTTPError as exc:
        return {
            "ok": False,
            "items": [],
            "errors": [f"http_{exc.code}"],
            "endpoint": USASPENDING_SPENDING_BY_AWARD,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "items": [],
            "errors": [f"{type(exc).__name__}:{exc}"],
            "endpoint": USASPENDING_SPENDING_BY_AWARD,
        }

    items: list[dict[str, Any]] = []
    for row in raw.get("results") or []:
        if not isinstance(row, dict):
            continue
        items.append(
            {
                "award_id": row.get("Award ID"),
                "awardee": row.get("Recipient Name"),
                "total_award_value": row.get("Award Amount"),
                "description": row.get("Description"),
                "buyer": row.get("Awarding Agency"),
                "award_date": row.get("Start Date"),
                "end_date": row.get("End Date"),
                "naics": row.get("NAICS Code"),
                "psc": row.get("PSC Code"),
                "pricing_type": "total_contract_award_amount",
                "unit_price": None,
                "match_class": "GENERAL_MARKET_CONTEXT_ONLY",
                "match_reason": (
                    "USAspending contract-level total only; not verified product unit pricing. "
                    "Requires original award docs / CLIN schedule before bidding use."
                ),
                "relevance": "MARKET_CONTEXT_ONLY",
                "source_url": USASPENDING_UI,
                "api_endpoint": USASPENDING_SPENDING_BY_AWARD,
                "not_guaranteed_future_price": True,
                "evidence_class": "historical_benchmark",
            }
        )
    return {
        "ok": True,
        "items": items,
        "errors": [],
        "endpoint": USASPENDING_SPENDING_BY_AWARD,
        "docs": USASPENDING_DOCS,
        "verified_at": _utc(),
        "pricing_note": (
            "Award Amount is a contract/obligation total. Do not divide across products "
            "to invent unit prices."
        ),
        "status": status,
        "rows_returned": len(items),
    }
