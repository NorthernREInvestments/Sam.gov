"""Last bounded public-price check before quote packets finalize."""

from __future__ import annotations

import time
from decimal import Decimal, ROUND_HALF_UP
from typing import Any
from urllib.parse import urlparse

from deep_completion_3to5.models import (
    AUTHORIZED_DISTRIBUTOR_CURRENT,
    MANUFACTURER_CURRENT,
    MIN_EXECUTABLE_PRICE,
    PUBLIC_CHECK_ITEM_S,
    PUBLIC_CHECK_MAX_LIVE,
    PUBLIC_CURRENT,
)
from line_basket_completion_strict_economics.provenance import classify_price_record


def _money(v: Any) -> float | None:
    try:
        if v is None:
            return None
        return float(Decimal(str(v)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    except Exception:
        return None


def _origin(seller: str, url: str) -> str:
    host = (urlparse(url).hostname or seller or "").lower().replace("www.", "")
    if "cummins" in host or "manufacturer" in host:
        return MANUFACTURER_CURRENT
    if any(d in host for d in ("grainger.com", "boundtree.com", "henryschein.com", "medline.com", "siteone.com")):
        return AUTHORIZED_DISTRIBUTOR_CURRENT
    return PUBLIC_CURRENT


def last_public_price_check(
    lines: list[dict[str, Any]],
    *,
    stats: dict[str, Any] | None = None,
    max_live: int = PUBLIC_CHECK_MAX_LIVE,
    deadline_s: float = 90.0,
) -> list[dict[str, Any]]:
    """Bounded last-chance public price for top material identities."""
    stats = stats if stats is not None else {}
    deadline = time.time() + deadline_s
    # Prefer exact MPN lines first
    ordered = sorted(
        lines,
        key=lambda l: (
            0 if l.get("identity_class", "").startswith("A_") else 1,
            0 if (l.get("mpn") or l.get("part_number")) else 1,
            -float(l.get("quantity") or 1),
        ),
    )
    live = 0
    out = []
    for ln in ordered:
        row = dict(ln)
        if live >= max_live or time.time() > deadline:
            out.append(row)
            continue
        mpn = ln.get("mpn") or ln.get("part_number") or ln.get("model")
        if not mpn or not ln.get("identity_usable"):
            out.append(row)
            continue
        if ln.get("quote_required_state") == "PRICED_EXECUTABLE" and ln.get("unit_cost"):
            out.append(row)
            continue
        try:
            from price_coverage_80.resolve import resolve_accurate_price

            found = resolve_accurate_price(
                {
                    "benchmark_id": f"dc35-{ln.get('line_id')}",
                    "mpn": mpn,
                    "manufacturer": ln.get("manufacturer"),
                    "description": ln.get("description"),
                    "expected_condition": "NEW",
                    "expected_uom": ln.get("uom") or "EA",
                    "expected_pack": int(ln.get("pack") or 1),
                    "category": "mro",
                },
                use_budget=True,
                max_sellers=2,
                max_queries=1,
                max_pages=3,
                stats=stats,
            )
            live += 1
            stats["public_check_attempts"] = int(stats.get("public_check_attempts") or 0) + 1
            price = found.get("price") or found.get("unit_price")
            url = str(found.get("source_url") or found.get("url") or "")
            seller = str(found.get("seller") or found.get("domain") or "")
            audit = classify_price_record(
                {"unit_cost": price, "source_url": url, "seller": seller, "price_origin": "LIVE"}
            )
            if (
                found.get("usable")
                and audit.get("is_valid_production")
                and _money(price)
                and float(price) >= MIN_EXECUTABLE_PRICE
                and "search" not in url.lower()
            ):
                row["quote_required_state"] = "PRICED_EXECUTABLE"
                row["acquisition_state"] = "PRICED_EXECUTABLE"
                row["unit_cost"] = _money(price)
                row["seller"] = seller
                row["source_url"] = url
                row["price_origin"] = _origin(seller, url)
                row["public_check_hit"] = True
                stats["public_check_hits"] = int(stats.get("public_check_hits") or 0) + 1
            else:
                row["public_check_miss"] = True
                row["public_check_reason"] = found.get("reason") or audit.get("verdict")
        except Exception as exc:
            row["public_check_error"] = str(exc)[:160]
            live += 1
        out.append(row)
    # Preserve order by original line_id map
    by_id = {r.get("line_id"): r for r in out}
    return [by_id.get(l.get("line_id"), l) for l in lines]
