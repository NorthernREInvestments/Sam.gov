"""Phase L.2.5 — recurring-buy watch + buyer cadence aggregation."""

from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Any

from application_clock import now_utc


def _utc() -> str:
    return now_utc().isoformat()


def _norm_key(manufacturer: str | None, model: str | None, mpn: str | None) -> str:
    parts = [str(x or "").strip().upper() for x in (manufacturer, model or mpn)]
    return "|".join(p for p in parts if p)


def aggregate_recurring_purchases(
    history_events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Build RecurringProductWatch rows from historical purchase events.

    Each event expects: manufacturer, model/mpn, buyer, unit_price, quantity,
    purchase_date (optional), competition (optional).
    """
    by_prod: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for ev in history_events or []:
        key = _norm_key(ev.get("manufacturer"), ev.get("model"), ev.get("mpn") or ev.get("primary_mpn"))
        if not key or "|" not in key and len(key) < 4:
            continue
        if ev.get("unit_price") is None and ev.get("price") is None:
            continue
        by_prod[key].append(ev)

    watches: list[dict[str, Any]] = []
    for key, events in by_prod.items():
        prices = []
        qtys = []
        buyers: dict[str, int] = defaultdict(int)
        dates: list[str] = []
        for e in events:
            p = e.get("unit_price") if e.get("unit_price") is not None else e.get("price")
            try:
                prices.append(float(p))
            except (TypeError, ValueError):
                pass
            if e.get("quantity") is not None:
                try:
                    qtys.append(float(e["quantity"]))
                except (TypeError, ValueError):
                    pass
            buyer = str(e.get("buyer") or e.get("agency") or "UNKNOWN").strip()
            buyers[buyer] += 1
            if e.get("purchase_date"):
                dates.append(str(e["purchase_date"])[:10])

        if not prices:
            continue
        purchase_count = len(events)
        median_gov = float(statistics.median(prices))
        typical_qty = float(statistics.median(qtys)) if qtys else None
        dates_sorted = sorted(dates)
        # crude next-window: if 3+ dated buys, estimate month cadence
        next_window = None
        if len(dates_sorted) >= 3:
            next_window = "ESTIMATE_FROM_CADENCE"
        known_acq = None
        for e in events:
            if e.get("acquisition_benchmark") is not None:
                known_acq = float(e["acquisition_benchmark"])
                break
        margin = None
        if known_acq is not None and median_gov > known_acq:
            margin = round(median_gov - known_acq, 2)

        status = "RECURRING_CANDIDATE"
        if purchase_count >= 3 and known_acq is not None and margin and margin > 0:
            status = "KNOWN_PROFITABLE_REPEAT_BUY_CANDIDATE"

        parts = key.split("|")
        watches.append(
            {
                "kind": "RecurringProductWatch",
                "product_key": key,
                "manufacturer": parts[0] if parts else None,
                "model_or_mpn": parts[1] if len(parts) > 1 else parts[0],
                "buyers": sorted(buyers.keys()),
                "buyer_counts": dict(buyers),
                "purchase_count": purchase_count,
                "recent_purchase_dates": dates_sorted[-5:],
                "typical_quantity": typical_qty,
                "typical_unit_price": round(float(statistics.mean(prices)), 2),
                "median_government_price": median_gov,
                "observed_competition": [e.get("competition") for e in events if e.get("competition")],
                "next_likely_buy_window": next_window,
                "known_acquisition_benchmark": known_acq,
                "historical_margin_estimate": margin,
                "status": status,
                "updated_at": _utc(),
            }
        )

    watches.sort(key=lambda w: (-int(w["purchase_count"]), -(w.get("historical_margin_estimate") or 0)))
    return watches


def prioritize_recurring(watches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Prefer 3+ buys, recent, stable identity, accessible acquisition, positive spread."""
    scored = []
    for w in watches:
        score = 0
        if int(w.get("purchase_count") or 0) >= 3:
            score += 40
        if w.get("recent_purchase_dates"):
            score += 15
        if w.get("typical_quantity") is not None:
            score += 10
        if w.get("known_acquisition_benchmark") is not None:
            score += 25
        if (w.get("historical_margin_estimate") or 0) > 0:
            score += 30
        if len(w.get("buyers") or []) >= 2:
            score += 10
        scored.append({**w, "priority_score": score})
    scored.sort(key=lambda x: -x["priority_score"])
    return scored


def aggregate_buyer_watchlist(
    history_events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Buyer cadence / category watchlist."""
    by_buyer: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for ev in history_events or []:
        buyer = str(ev.get("buyer") or ev.get("agency") or "").strip()
        if not buyer:
            continue
        by_buyer[buyer].append(ev)

    out: list[dict[str, Any]] = []
    for buyer, events in by_buyer.items():
        products = []
        months = []
        for e in events:
            label = e.get("model") or e.get("mpn") or e.get("title")
            if label and label not in products:
                products.append(str(label)[:80])
            if e.get("purchase_date") and len(str(e["purchase_date"])) >= 7:
                months.append(str(e["purchase_date"])[5:7])
        out.append(
            {
                "kind": "BuyerWatch",
                "buyer": buyer,
                "products_purchased": products[:20],
                "purchase_count": len(events),
                "typical_months": sorted(set(months)),
                "portal": events[0].get("portal"),
                "registration_status": events[0].get("registration_status"),
                "competition_notes": [e.get("competition") for e in events if e.get("competition")][:5],
                "award_history_count": len(events),
                "relevant_recurring_categories": list(
                    {
                        str(e.get("category") or e.get("product_class") or "general")
                        for e in events
                    }
                )[:8],
                "updated_at": _utc(),
            }
        )
    out.sort(key=lambda b: -int(b["purchase_count"]))
    return out


def events_from_candidate_audits(audits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Derive synthetic history events from L.2.5 candidate audit rows when history found."""
    events: list[dict[str, Any]] = []
    for a in audits or []:
        if a.get("history_unit") is None:
            continue
        events.append(
            {
                "manufacturer": a.get("manufacturer"),
                "model": a.get("model"),
                "mpn": a.get("mpn"),
                "buyer": a.get("buyer") or a.get("agency") or "UNKNOWN",
                "unit_price": a.get("history_unit"),
                "quantity": a.get("quantity"),
                "purchase_date": a.get("history_date"),
                "competition": a.get("competition"),
                "acquisition_benchmark": a.get("current_unit") or a.get("usable_acq"),
                "portal": a.get("portal"),
                "category": a.get("identity_state") or a.get("family"),
            }
        )
    return events


def persist_known_product_economics(
    known: dict[str, Any],
    *,
    product_key: str,
    hist_unit: float,
    acq_unit: float,
    source_urls: list[str] | None = None,
    solicitation: str | None = None,
) -> dict[str, Any]:
    """Persist KnownProductEconomics when history + usable acquisition prove positive spread."""
    products = known.setdefault("products", {})
    spread = round(float(hist_unit) - float(acq_unit), 2)
    rec = {
        "kind": "KnownProductEconomics",
        "product_key": product_key,
        "historical_unit": float(hist_unit),
        "acquisition_unit": float(acq_unit),
        "unit_spread": spread,
        "status": "KNOWN_PRODUCT — PRIOR ECONOMICS AVAILABLE" if spread > 0 else "KNOWN_PRODUCT_NONPOSITIVE",
        "source_urls": list(source_urls or [])[:8],
        "last_solicitation": solicitation,
        "updated_at": _utc(),
    }
    products[product_key] = rec
    known["updated_at"] = _utc()
    return rec


def reverse_live_hunt(
    *,
    watches: list[dict[str, Any]],
    live_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Known profitable/repeated historical product → match current live solicitations
    by exact MPN / model / NSN / buyer-product family tokens.
    """
    matches: list[dict[str, Any]] = []
    for w in watches or []:
        tokens = []
        for t in (w.get("model_or_mpn"), w.get("manufacturer"), *(w.get("buyers") or [])[:2]):
            if t and len(str(t)) >= 3:
                tokens.append(str(t).upper())
        if not tokens:
            continue
        for row in live_rows or []:
            blob = " ".join(
                str(row.get(k) or "")
                for k in ("title", "description", "solicitation_id", "nsn", "mpn", "model", "agency")
            ).upper()
            hit = sum(1 for t in tokens if t in blob)
            if hit < 1:
                continue
            # Prefer identity-ish hits (model/mpn) over buyer-only
            identity_hit = any(
                t in blob for t in tokens if t == str(w.get("model_or_mpn") or "").upper() or len(t) >= 5
            )
            if not identity_hit and hit < 2:
                continue
            matches.append(
                {
                    "kind": "ReverseHuntMatch",
                    "product_key": w.get("product_key"),
                    "watch_status": w.get("status"),
                    "solicitation": row.get("solicitation_id") or row.get("notice_id"),
                    "title": (row.get("title") or "")[:120],
                    "our_bid_access": row.get("our_bid_access"),
                    "token_hits": hit,
                    "median_government_price": w.get("median_government_price"),
                    "known_acquisition_benchmark": w.get("known_acquisition_benchmark"),
                    "historical_margin_estimate": w.get("historical_margin_estimate"),
                }
            )
    # de-dupe by solicitation+product
    seen: set[str] = set()
    out = []
    for m in matches:
        k = f"{m.get('solicitation')}|{m.get('product_key')}"
        if k in seen:
            continue
        seen.add(k)
        out.append(m)
    out.sort(key=lambda x: (-(x.get("historical_margin_estimate") or 0), -int(x.get("token_hits") or 0)))
    return out


def recall_known_product(known: dict[str, Any], product_key: str | None) -> dict[str, Any] | None:
    if not product_key:
        return None
    return (known.get("products") or {}).get(product_key)
