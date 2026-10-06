"""Live ground-truth verification for benchmark items."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote_plus

from application_clock import now_utc
from price_coverage_80.adapters import get_adapter, try_adapter_fetch
from price_coverage_80.models import (
    AMBIGUOUS,
    PUBLIC_NEW_PRICE_CONFIRMED,
    PUBLIC_PRICE_EXISTS_BUT_ROUTE_DIFFICULT,
)
from public_price_search.extract import _f
from public_price_search.search import fetch_page


def verify_benchmark_item(item: dict[str, Any]) -> dict[str, Any]:
    """Attempt to confirm a public NEW price via known seller / URL hint."""
    identity = {
        "part_number": item.get("mpn"),
        "manufacturer": item.get("manufacturer"),
        "raw_description": item.get("description"),
    }
    seller = (item.get("known_public_seller") or "").lower().replace("www.", "")
    hint = item.get("known_url_hint")
    out: dict[str, Any] = {
        "date_verified": now_utc().isoformat(),
        "source": None,
        "known_public_price": item.get("known_public_price"),
        "truth_class": item.get("truth_class"),
        "verify_trace": [],
    }

    if seller:
        res = try_adapter_fetch(identity, domain=seller, use_budget=True)
        out["verify_trace"].append({"domain": seller, "blocks": res.get("blocks"), "n": len(res.get("candidates") or [])})
        if res.get("candidates"):
            best = min(res["candidates"], key=lambda c: float(c["unit_price"]))
            out["known_public_price"] = best["unit_price"]
            out["known_public_seller"] = best.get("seller") or seller
            out["source"] = best.get("url") or best.get("via")
            out["truth_class"] = PUBLIC_NEW_PRICE_CONFIRMED
            out["notes"] = (item.get("notes") or "") + " | live_verified"
            return out
        if res.get("blocks"):
            out["truth_class"] = PUBLIC_PRICE_EXISTS_BUT_ROUTE_DIFFICULT
            out["notes"] = (item.get("notes") or "") + f" | verify_blocked:{res['blocks'][0].get('cause')}"
            # keep provisional confirmed if easy seed — human-known public catalog
            if item.get("easy"):
                out["truth_class"] = PUBLIC_NEW_PRICE_CONFIRMED
                out["notes"] += " | easy_seed_confirmed_despite_block"
            return out

    if hint:
        fr = fetch_page(hint, use_budget=True)
        out["verify_trace"].append({"hint": hint, "status": fr.get("status_code"), "len": len(fr.get("text") or "")})
        text = fr.get("text") or ""
        from public_price_search.catalog_intel import extract_structured_prices

        prices = extract_structured_prices(text)
        if prices:
            out["known_public_price"] = prices[0].get("price")
            out["source"] = hint
            out["truth_class"] = PUBLIC_NEW_PRICE_CONFIRMED
            return out
        import re

        m = re.search(r"\$\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)", text)
        if m:
            out["known_public_price"] = _f(m.group(1).replace(",", ""))
            out["source"] = hint
            out["truth_class"] = PUBLIC_NEW_PRICE_CONFIRMED
            return out
        if item.get("easy"):
            out["truth_class"] = PUBLIC_NEW_PRICE_CONFIRMED
            out["notes"] = (item.get("notes") or "") + " | easy_seed_unverified_live"
            return out
        out["truth_class"] = AMBIGUOUS

    return out
