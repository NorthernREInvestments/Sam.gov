"""Lightweight domain adapters for proven high-yield open distributors.

JSON-LD / structured product data only — no browser rendering.
"""

from __future__ import annotations

import json
import re
from typing import Any

from price_adapters.validate import extract_jsonld_exact, mpn_in_blob, seller_of, validate_candidate
from public_price_search.extract import _f, detect_condition
from seller_rediscovery.models import ROUTE_ADAPTER, ROUTE_JSONLD, ROUTE_SELLER_ENDPOINT


def _identity_from_item(item: dict[str, Any]) -> dict[str, Any]:
    from price_adapters.orchestrate import _identity

    return _identity(item)


def _mpn_needs_manufacturer(mpn: str) -> bool:
    tok = re.sub(r"[^A-Za-z0-9]", "", mpn or "")
    if len(tok) < 8:
        return True
    digits = sum(ch.isdigit() for ch in tok)
    return digits / max(len(tok), 1) >= 0.7


def _manufacturer_evidenced(identity: dict[str, Any], cand: dict[str, Any], url: str) -> bool:
    """Reject cross-brand MPN collisions (e.g. Sylvania 40771 vs Sullair 40771)."""
    mfr = str(identity.get("manufacturer") or "").strip()
    mpn = str(identity.get("part_number") or identity.get("mpn") or "")
    if not mfr:
        return True
    name = str(cand.get("name") or "")
    brand = str(cand.get("brand") or "")
    blob = f"{name} {brand} {url}".lower()
    blob_tok = re.sub(r"[^a-z0-9]", "", blob)
    mfr_first = re.sub(r"[^a-z0-9]", "", mfr.lower().split()[0])
    if len(mfr_first) >= 3 and mfr_first in blob_tok:
        return True
    # Known conflict stems in URL/name while expected mfr absent
    conflict_stems = (
        "sullair",
        "weidmuller",
        "weidmueller",
        "mahle",
        "com08884",
        "packing",
        "gasket-replacement",
    )
    if any(stem in blob for stem in conflict_stems) and mfr_first not in blob_tok:
        return False
    if _mpn_needs_manufacturer(mpn):
        return False
    return True


def _validate(item: dict[str, Any], cand: dict[str, Any], url: str) -> dict[str, Any] | None:
    if not cand or cand.get("unit_price") is None:
        return None
    price = float(cand["unit_price"])
    if price < 1.51 or abs(price - 10.58) < 0.001:
        return None
    # call-for-price markers
    via = str(cand.get("via") or "").lower()
    if "call" in via and "price" in via:
        return None
    identity = _identity_from_item(item)
    if not _manufacturer_evidenced(identity, cand, url):
        return None
    # Absurd single-unit prices without known anchor (pack contamination)
    known = item.get("known_public_price")
    exp_pack = int(item.get("expected_pack") or identity.get("expected_pack") or 1)
    if known is None and price > 5000:
        return None
    # Commodity EA items rarely exceed $400 without a known catalog anchor
    if known is None and exp_pack <= 1 and price > 400:
        cat = str(item.get("category") or item.get("benchmark_id") or "").lower()
        if any(
            x in cat
            for x in ("mro", "ppe", "plumb", "light", "office", "auto", "tool", "ind-", "elec")
        ):
            return None
    # Reject obvious wrong pack sizes in offer name/url (gallon vs small retail EA)
    pack_blob = f"{cand.get('name') or ''} {url}".lower()
    if exp_pack <= 1 and any(x in pack_blob for x in ("gallon", "128-fl", "128 fl", "case of", "pack of 12", "box of 12")):
        if price >= 40:
            return None
    payload = {
        **cand,
        "url": url,
        "seller": seller_of(url),
        "displayed_price": cand.get("unit_price"),
        "landed_estimate": cand.get("unit_price"),
        "freight_status": "FREIGHT_UNKNOWN",
    }
    v = validate_candidate(identity, payload, expected_condition=str(identity.get("expected_condition") or "NEW"))
    if not v.get("ok"):
        return None
    return payload


def _meta_product_price(html: str) -> float | None:
    for pat in (
        r'property=["\']product:price:amount["\'][^>]*content=["\']([0-9]+(?:\.[0-9]+)?)["\']',
        r'content=["\']([0-9]+(?:\.[0-9]+)?)["\'][^>]*property=["\']product:price:amount["\']',
        r'itemprop=["\']price["\'][^>]*content=["\']([0-9]+(?:\.[0-9]+)?)["\']',
        r'"price"\s*:\s*"?(?P<p>\d+\.\d{2})"?',
    ):
        m = re.search(pat, html or "", re.I)
        if m:
            p = _f(m.group(1) if m.lastindex else m.group("p"))
            if p and p >= 1.51:
                return float(p)
    return None


def adapter_platt(html: str, *, url: str, item: dict[str, Any]) -> dict[str, Any] | None:
    """Platt.com — JSON-LD Product/Offer is the proven productive route."""
    if "platt.com" not in seller_of(url):
        return None
    identity = _identity_from_item(item)
    mpn = str(identity.get("part_number") or "")
    mfr = identity.get("manufacturer")

    # 1) JSON-LD exact
    for c in extract_jsonld_exact(html, mpn=mpn, manufacturer=mfr):
        ok = _validate(item, {**c, "via": "platt_adapter+jsonld"}, url)
        if ok:
            ok["via"] = "platt_adapter+jsonld"
            ok["route"] = ROUTE_ADAPTER
            return ok

    # 2) Embedded product object with MPN
    tok = re.sub(r"[^A-Za-z0-9]", "", mpn).lower()
    for m in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html or "",
        re.I | re.S,
    ):
        try:
            data = json.loads(m.group(1))
        except Exception:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                blob = json.dumps(node)[:2000]
                if tok and tok not in re.sub(r"[^a-z0-9]", "", blob.lower()):
                    for v in node.values():
                        if isinstance(v, (dict, list)):
                            stack.append(v)
                    continue
                offers = node.get("offers")
                offer_list = offers if isinstance(offers, list) else ([offers] if isinstance(offers, dict) else [])
                for off in offer_list:
                    if not isinstance(off, dict):
                        continue
                    price = _f(str(off.get("price") or ""))
                    if not price or price < 1.51:
                        continue
                    # skip call-for-price style availability without price
                    ok = _validate(
                        item,
                        {
                            "unit_price": float(price),
                            "condition": detect_condition(str(off.get("itemCondition") or "")),
                            "name": str(node.get("name") or mpn)[:160],
                            "sku": str(node.get("sku") or node.get("mpn") or mpn),
                            "via": "platt_adapter+offer",
                        },
                        url,
                    )
                    if ok:
                        ok["route"] = ROUTE_ADAPTER
                        return ok
                for v in node.values():
                    if isinstance(v, (dict, list)):
                        stack.append(v)
            elif isinstance(node, list):
                stack.extend(node[:40])
    return None


def adapter_fluke(html: str, *, url: str, item: dict[str, Any]) -> dict[str, Any] | None:
    """Fluke.com — exact product JSON-LD."""
    if "fluke.com" not in seller_of(url):
        return None
    identity = _identity_from_item(item)
    mpn = str(identity.get("part_number") or "")
    mfr = identity.get("manufacturer")
    for c in extract_jsonld_exact(html, mpn=mpn, manufacturer=mfr):
        ok = _validate(item, {**c, "via": "fluke_adapter+jsonld"}, url)
        if ok:
            ok["via"] = "fluke_adapter+jsonld"
            ok["route"] = ROUTE_ADAPTER
            return ok
    # Meta price when MPN clearly on page
    if mpn_in_blob(mpn, html[:60000] if html else ""):
        p = _meta_product_price(html)
        if p:
            ok = _validate(
                item,
                {
                    "unit_price": p,
                    "condition": "NEW",
                    "name": mpn,
                    "sku": mpn,
                    "via": "fluke_adapter+meta",
                },
                url,
            )
            if ok:
                ok["route"] = ROUTE_ADAPTER
                return ok
    return None


ADAPTERS = {
    "platt.com": adapter_platt,
    "fluke.com": adapter_fluke,
}


def run_domain_adapter(html: str, *, url: str, item: dict[str, Any]) -> dict[str, Any] | None:
    host = seller_of(url)
    fn = ADAPTERS.get(host)
    if not fn:
        # also match subdomain roots
        for key, adapter in ADAPTERS.items():
            if host.endswith(key):
                fn = adapter
                break
    if not fn:
        return None
    return fn(html, url=url, item=item)


def jsonld_first_extract(html: str, *, url: str, item: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Policy order: adapter → JSON-LD → structured → static markup. No browser."""
    # 0 domain adapter
    hit = run_domain_adapter(html, url=url, item=item)
    if hit:
        return hit, hit.get("route") or ROUTE_ADAPTER

    identity = _identity_from_item(item)
    mpn = str(identity.get("part_number") or "")
    mfr = identity.get("manufacturer")

    # 1 JSON-LD
    for c in extract_jsonld_exact(html, mpn=mpn, manufacturer=mfr):
        ok = _validate(item, {**c, "via": "jsonld_first"}, url)
        if ok:
            ok["via"] = "jsonld_first"
            ok["route"] = ROUTE_JSONLD
            return ok, ROUTE_JSONLD

    # 2 Reuse exact_page extract_from_html for structured/static (no browser)
    try:
        from exact_page_extraction.extract import extract_from_html

        best, _raw, route = extract_from_html(html, url=url, identity=identity, route_prefix="rediscovery")
        if best:
            # Map route
            rmap = {
                "JSON_LD": ROUTE_JSONLD,
                "HYDRATION": "STRUCTURED_EMBEDDED",
                "STATIC_HTML": "STATIC_MARKUP",
            }
            best["route"] = rmap.get(route or "", route or "STATIC_MARKUP")
            if best["route"] == "STRUCTURED_EMBEDDED":
                best["via"] = (best.get("via") or "") + "+structured"
            return best, best["route"]
    except Exception:
        pass

    # 3 Meta fallback with MPN on page
    if mpn_in_blob(mpn, html[:80000] if html else ""):
        p = _meta_product_price(html)
        if p:
            ok = _validate(
                item,
                {"unit_price": p, "condition": "NEW", "name": mpn, "sku": mpn, "via": "meta_price"},
                url,
            )
            if ok:
                ok["route"] = "STATIC_MARKUP"
                return ok, "STATIC_MARKUP"

    return None, None


# Keep symbols referenced for route accounting
__all__ = [
    "ADAPTERS",
    "run_domain_adapter",
    "jsonld_first_extract",
    "adapter_platt",
    "adapter_fluke",
    "ROUTE_SELLER_ENDPOINT",
]
