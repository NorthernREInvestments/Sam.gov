"""Recurring-buy intelligence from structured history (Phase L.16).

Flags RECURRING_BUY_SIGNAL — does NOT promote into live opportunity inventory.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

RECURRING_BUY_SIGNAL = "RECURRING_BUY_SIGNAL"
KNOWN_PROFITABLE_REPEAT_BUY = "KNOWN_PROFITABLE_REPEAT_BUY"  # reserved — not auto-created

_NORM_RE = re.compile(r"[^a-z0-9]+")


def normalize_product_key(text: str | None) -> str:
    s = _NORM_RE.sub(" ", str(text or "").lower()).strip()
    toks = [t for t in s.split() if len(t) > 2][:8]
    return " ".join(toks)


def detect_recurring_buys(
    history_rows: list[dict[str, Any]],
    *,
    min_occurrences: int = 2,
) -> list[dict[str, Any]]:
    """Group history by buyer+normalized product; emit recurring signals."""
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for h in history_rows:
        buyer = str(h.get("buyer") or "unknown").strip().lower()[:80]
        prod = normalize_product_key(h.get("product"))
        if not prod or len(prod) < 4:
            continue
        buckets[(buyer, prod)].append(h)

    signals: list[dict[str, Any]] = []
    for (buyer, prod), items in buckets.items():
        if len(items) < min_occurrences:
            continue
        dates = sorted({str(i.get("award_date") or "")[:10] for i in items if i.get("award_date")})
        vendors = [str(i.get("vendor") or "") for i in items if i.get("vendor")]
        vendor_mode = max(set(vendors), key=vendors.count) if vendors else None
        amounts = []
        qtys = []
        for i in items:
            try:
                if i.get("total") is not None:
                    amounts.append(float(str(i["total"]).replace(",", "").replace("$", "")))
            except (TypeError, ValueError):
                pass
            if i.get("quantity") not in (None, ""):
                qtys.append(i.get("quantity"))
        signals.append(
            {
                "kind": RECURRING_BUY_SIGNAL,
                "buyer": buyer,
                "normalized_product": prod,
                "prior_vendor": vendor_mode,
                "purchase_dates": dates[:12],
                "occurrence_count": len(items),
                "quantity_samples": qtys[:8],
                "amount_samples": amounts[:8],
                "likely_cadence": _infer_cadence(dates),
                "sources": sorted({str(i.get("source") or "") for i in items}),
                "not_live_inventory": True,
                "known_profitable_repeat_buy": False,  # requires verified economics later
            }
        )
    signals.sort(key=lambda x: -int(x.get("occurrence_count") or 0))
    return signals


def _infer_cadence(dates: list[str]) -> str | None:
    if len(dates) < 2:
        return None
    # Rough gap in days between first/last
    try:
        from datetime import date

        ds = [date.fromisoformat(d) for d in dates if len(d) >= 10]
        if len(ds) < 2:
            return None
        span = (ds[-1] - ds[0]).days
        if span <= 0:
            return "same_period"
        avg = span / max(1, len(ds) - 1)
        if avg < 45:
            return "monthly_ish"
        if avg < 120:
            return "quarterly_ish"
        if avg < 400:
            return "annual_ish"
        return "multi_year"
    except Exception:
        return None


def feed_supplier_product_memory(
    history_rows: list[dict[str, Any]],
    supplier_memory: dict[str, Any] | None = None,
    *,
    max_entries: int = 80,
) -> dict[str, Any]:
    """Upsert product→vendor links into supplier memory with evidence grade preserved."""
    mem = dict(supplier_memory or {})
    by_key = mem.setdefault("by_product", {})
    added = 0
    for h in history_rows:
        prod = normalize_product_key(h.get("product"))
        vendor = str(h.get("vendor") or "").strip()
        if not prod or not vendor:
            continue
        grade = h.get("evidence_grade_candidate") or "GOV_D"
        entry = by_key.setdefault(prod, {"vendors": [], "sources": []})
        if vendor not in entry["vendors"]:
            entry["vendors"].append(vendor)
            added += 1
        src = str(h.get("source") or h.get("source_url") or "")
        if src and src not in entry["sources"]:
            entry["sources"].append(src)
        entry["evidence_grade_candidate"] = grade
        entry["last_award_date"] = h.get("award_date")
        if added >= max_entries:
            break
    mem["updated_from"] = "L16_STRUCTURED_HISTORY"
    mem["entries"] = len(by_key)
    return mem
