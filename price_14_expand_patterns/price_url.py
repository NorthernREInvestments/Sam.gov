"""Price a known exact product URL with pack/case/shell guards."""

from __future__ import annotations

import re
from typing import Any

from exact_page_extraction.extract import extract_exact_page
from exact_product_url_discovery.classify import is_search_shell
from exact_product_url_discovery.validate_identity import validate_product_page
from price_14_expand_patterns.models import BUILD
from price_adapters.validate import seller_of


_CASE_HINT = re.compile(
    r"\b(case|carton|cs\b|box of\s*\d+|pack of\s*(?:2[4-9]|[3-9]\d|\d{3,})|/\s*cs\b|pk/cs|50.?pk.?cs)\b",
    re.I,
)
_PAIR_HINT = re.compile(r"\b(pair|2.?pack|2.?pk|2/?pk|per pair)\b", re.I)

# Soft category ceilings to catch hydration/aggregate false prices (not MSRP floors)
_CATEGORY_PRICE_CEILING = {
    "hvac": 800.0,
    "office": 500.0,
    "mro": 400.0,
    "ppe": 400.0,
    "tools": 300.0,
    "electrical": 300.0,
    "plumbing": 500.0,
    "lighting": 400.0,
    "furniture": 2000.0,
    "industrial": 5000.0,
}


def _price_sane(item: dict[str, Any], price: float) -> tuple[bool, str]:
    cat = str(item.get("category") or "").lower()
    ceiling = _CATEGORY_PRICE_CEILING.get(cat)
    if ceiling and price > ceiling:
        return False, "price_outlier_category_ceiling"
    # Thermostats / filters should never be five-figure
    desc = str(item.get("description") or "").lower()
    if any(x in desc for x in ("thermostat", "filter pair", "toner", "ink")) and price > 1000:
        return False, "price_outlier_product_class"
    return True, "ok"


def _pack_compatible(item: dict[str, Any], *, title: str, url: str, price: float | None) -> tuple[bool, str]:
    """Reject obvious case/carton when expected is single/pair."""
    expected_uom = str(item.get("expected_uom") or "EA").upper()
    expected_pack = int(item.get("expected_pack") or 1)
    blob = f"{title} {url}"
    desc = str(item.get("description") or "")

    # Case-priced SKUs on Platt often show huge case totals for pair/EA items
    if expected_pack <= 2 and expected_uom in {"EA", "PR", "PAIR"}:
        if _CASE_HINT.search(blob) and (price or 0) >= 200:
            return False, "wrong_pack_case_price"
        if "pk/cs" in blob.lower() or "50-pk" in blob.lower() or "50 pk" in blob.lower():
            if "pair" in desc.lower() or expected_uom in {"PR", "PAIR"}:
                return False, "wrong_pack_case_vs_pair"
    # Multi-pack ink when expecting single black cartridge
    if "multi-pack" in blob.lower() and "black" in desc.lower() and "cyan" in blob.lower():
        return False, "wrong_pack_multipack"
    return True, "ok"


def price_exact_url(
    url: str,
    item: dict[str, Any],
    *,
    allow_browser: bool = True,
    stats: dict[str, Any] | None = None,
    revalidate_identity: bool = True,
) -> dict[str, Any]:
    """Validate identity + extract executable NEW price from exact PDP."""
    stats = stats if stats is not None else {}
    mpn = str(item.get("mpn") or item.get("part_number") or "")
    mfr = str(item.get("manufacturer") or "")
    out: dict[str, Any] = {
        "build": BUILD,
        "benchmark_id": item.get("benchmark_id"),
        "url": url,
        "domain": seller_of(url),
        "status": "FAIL",
        "price": None,
        "condition": None,
        "pack_uom": None,
        "extraction_route": None,
        "rejection": None,
        "identity_ok": False,
    }
    if not url or not url.startswith("http"):
        out["rejection"] = "missing_url"
        return out
    if is_search_shell(url):
        out["rejection"] = "search_shell"
        stats.setdefault("rejections", {})
        stats["rejections"]["search_shell"] = int(stats["rejections"].get("search_shell") or 0) + 1
        return out

    if revalidate_identity:
        v = validate_product_page(
            url,
            mpn=mpn,
            manufacturer=mfr,
            description=item.get("description"),
            category=item.get("category"),
            allow_browser=False,
        )
        if not v.get("identity_match") and allow_browser:
            v = validate_product_page(
                url,
                mpn=mpn,
                manufacturer=mfr,
                description=item.get("description"),
                category=item.get("category"),
                allow_browser=True,
            )
        # READY URLs already passed open-web identity; allow price extract to proceed when
        # bot walls break re-fetch but extraction can still validate MPN via JSON-LD.
        if not v.get("identity_match"):
            reason = str(v.get("reason") or "identity_fail")
            soft_ok = reason in {"fetch_blocked_or_failed", "auth_or_non_product_title", "mpn_not_on_page"} and bool(
                item.get("_ready_verified")
            )
            if not soft_ok:
                out["rejection"] = reason
                out["identity_detail"] = reason
                key = "wrong_mpn" if "mpn" in reason else ("wrong_manufacturer" if "manufacturer" in reason else "other")
                stats.setdefault("rejections", {})
                stats["rejections"][key] = int(stats["rejections"].get(key) or 0) + 1
                return out
            out["identity_soft"] = reason
        out["identity_ok"] = True
        out["title"] = v.get("title")
    else:
        out["identity_ok"] = True

    diag = extract_exact_page(url, item, allow_browser=allow_browser, allow_api=True, stats=stats)
    best = diag.get("best") or {}
    if diag.get("status") != "FOUND_VALID_PRICE" or not best.get("unit_price"):
        out["rejection"] = diag.get("rejection") or diag.get("status") or "no_price"
        out["extraction_detail"] = {
            "routes": diag.get("routes_attempted"),
            "url_class": diag.get("url_class"),
        }
        stats.setdefault("rejections", {})
        stats["rejections"]["no_price"] = int(stats["rejections"].get("no_price") or 0) + 1
        return out

    price = float(best["unit_price"])
    title = str(best.get("name") or out.get("title") or "")
    ok_pack, pack_reason = _pack_compatible(item, title=title, url=url, price=price)
    if not ok_pack:
        out["rejection"] = pack_reason
        stats.setdefault("rejections", {})
        stats["rejections"]["wrong_pack"] = int(stats["rejections"].get("wrong_pack") or 0) + 1
        return out

    sane, sane_reason = _price_sane(item, price)
    if not sane:
        out["rejection"] = sane_reason
        stats.setdefault("rejections", {})
        stats["rejections"]["other"] = int(stats["rejections"].get("other") or 0) + 1
        return out

    cond = str(best.get("condition") or "NEW").upper()
    if cond and cond not in {"NEW", "CONDITION_NEW", ""} and "NEW" not in cond:
        if any(x in cond for x in ("USED", "REFURB", "REMAN", "RECON")):
            out["rejection"] = "wrong_condition"
            stats.setdefault("rejections", {})
            stats["rejections"]["wrong_condition"] = int(stats["rejections"].get("wrong_condition") or 0) + 1
            return out

    route = diag.get("route") or best.get("via") or "UNKNOWN"
    out.update(
        {
            "status": "PASS",
            "price": price,
            "condition": "NEW",
            "pack_uom": f"{item.get('expected_pack') or 1} {item.get('expected_uom') or 'EA'}",
            "extraction_route": route,
            "via": best.get("via"),
            "seller": best.get("seller") or seller_of(url),
            "title": title[:160],
            "usable": True,
        }
    )
    stats.setdefault("prices_extracted", 0)
    stats["prices_extracted"] = int(stats["prices_extracted"]) + 1
    route_key = str(route).upper()
    stats.setdefault("price_sources", {})
    if "JSON" in route_key or "jsonld" in str(best.get("via") or "").lower():
        sk = "jsonld"
    elif "BROWSER" in route_key:
        sk = "browser"
    elif "API" in route_key or "GRAPHQL" in route_key:
        sk = "graphql_api"
    elif "HYDR" in route_key or "structured" in str(best.get("via") or "").lower():
        sk = "structured_state"
    else:
        sk = "static"
    stats["price_sources"][sk] = int(stats["price_sources"].get(sk) or 0) + 1
    return out
