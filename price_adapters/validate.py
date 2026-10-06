"""Shared validation + extraction helpers for adapters."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

from public_price_search.catalog_intel import extract_structured_prices
from public_price_search.extract import _f, detect_condition
from public_price_search.models import CONDITION_NEW


def norm_token(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def mpn_in_blob(mpn: str, *parts: str) -> bool:
    """True when MPN token appears in text without digit-run false prefixes/suffixes.

    Prevents DWT-6 matching DWT62 (prefix digit continuation) while still allowing
    LEV5320S / WDF490040EA style catalog embeddings.
    """
    target = norm_token(mpn)
    if len(target) < 3:
        return False
    blob = norm_token(" ".join(str(p or "") for p in parts))
    idx = 0
    while True:
        j = blob.find(target, idx)
        if j < 0:
            return False
        before = blob[j - 1] if j > 0 else ""
        after = blob[j + len(target)] if j + len(target) < len(blob) else ""
        # Reject digit-run extensions: DWT6 vs DWT62, 117 vs 1170
        if after.isdigit() and target[-1].isdigit():
            idx = j + 1
            continue
        if before.isdigit() and target[0].isdigit():
            idx = j + 1
            continue
        return True


def extract_jsonld_exact(html: str, *, mpn: str, manufacturer: str | None = None) -> list[dict[str, Any]]:
    """Only products whose name/sku/mpn contain the exact MPN token."""
    out: list[dict[str, Any]] = []
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
                name = str(node.get("name") or "")
                sku = str(node.get("sku") or node.get("mpn") or node.get("productID") or "")
                if mpn_in_blob(mpn, name, sku):
                    offers = node.get("offers") or {}
                    if isinstance(offers, list):
                        offers = offers[0] if offers else {}
                    price = None
                    if isinstance(offers, dict):
                        price = _f(str(offers.get("price") or ""))
                    if price and price >= 1.51:
                        out.append(
                            {
                                "unit_price": price,
                                "name": name[:160],
                                "sku": sku,
                                "condition": detect_condition(f"{name} {sku}"),
                                "via": "jsonld_exact_mpn",
                            }
                        )
                for v in node.values():
                    if isinstance(v, (dict, list)):
                        stack.append(v)
            elif isinstance(node, list):
                stack.extend(node[:60])
    return out


def extract_near_mpn_price(html: str, *, mpn: str, manufacturer: str | None = None) -> dict[str, Any] | None:
    if not html or not mpn:
        return None
    # Scan multiple MPN occurrences — first hit is often in <title>/meta far from price
    starts = [m.start() for m in re.finditer(re.escape(mpn), html, re.I)]
    if not starts:
        return None
    for idx in starts[:8]:
        window = html[max(0, idx - 1200) : idx + 3500]
        # Require manufacturer token nearby when provided (reduces search-page $ false hits)
        if manufacturer:
            mfr_tok = manufacturer.split()[0]
            if len(mfr_tok) >= 3 and mfr_tok.upper() not in window.upper():
                # still allow if price keys present with exact mpn in same GA4 blob
                if "price" not in window.lower():
                    continue
        for pat in (
            r"['\"]price['\"]\s*:\s*['\"]?(?P<p>\d+\.\d{2})['\"]?",
            r'"price"\s*:\s*"?(?P<p>\d+\.\d{2})"?',
            r'data-price=["\'](?P<p>\d+\.\d{2})["\']',
            r'itemprop=["\']price["\'][^>]*content=["\'](?P<p>\d+\.\d{2})["\']',
            r'price-value[^>]*>\s*\$?\s*(?P<p>\d{1,3}(?:,\d{3})*(?:\.\d{2})?)',
        ):
            m = re.search(pat, window, re.I)
            if m:
                price = _f(m.group("p").replace(",", ""))
                if price and price >= 1.51:
                    return {
                        "unit_price": price,
                        "condition": detect_condition(window),
                        "via": "near_mpn_structured",
                        "name": "",
                    }
        # Dollar scrape is weakest — require price >= $3 and manufacturer nearby
        m = re.search(r"\$\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)", window)
        if m and manufacturer:
            price = _f(m.group(1).replace(",", ""))
            if price and price >= 3.0:
                return {
                    "unit_price": price,
                    "condition": detect_condition(window),
                    "via": "near_mpn_dollar",
                    "name": "",
                }
    return None


_FAMILY_TOKENS = (
    ("led", "light", "lamp", "bulb", "luminaire", "fixture"),
    ("knife", "utility", "blade", "cutter"),
    ("filter", "filtrete", "hvac"),
    ("thermostat", "honeywell"),
    ("valve", "plumbing", "sharkbite", "watts"),
    ("lockout", "tag", "brady"),
    ("cleaner", "crc", "solvent"),
    ("threadlocker", "loctite", "adhesive"),
    ("injector", "cummins", "diesel", "fuel"),
    ("bearing", "timken"),
    ("belt", "gates"),
    ("respirator", "cartridge", "ppe", "mask"),
    ("toner", "cartridge", "printer"),
)


def _family_mismatch(identity: dict[str, Any], candidate: dict[str, Any]) -> str | None:
    """Reject obvious cross-family collisions (e.g. LED light vs gate operator)."""
    desc = f"{identity.get('raw_description') or ''} {identity.get('manufacturer') or ''}".lower()
    name = f"{candidate.get('name') or ''} {candidate.get('url') or ''}".lower()
    if not desc.strip() or not name.strip():
        return None
    # hard conflict pairs
    conflicts = [
        (("led", "light", "lamp", "bulb", "street"), ("gate", "operator", "door", "motor")),
        (("knife", "utility"), ("drill", "saw", "battery")),
        (("toner", "printer"), ("filter", "respirator")),
        (("thermostat",), ("filter", "bulb", "lamp", "wire")),
        (("wire", "connector", "wire-nut", "wirenut"), ("thermostat", "filter", "lamp")),
        (("lockout", "tag"), ("filter", "toner", "knife")),
    ]
    for left, right in conflicts:
        if any(t in desc for t in left) and any(t in name for t in right):
            return f"family_conflict:{left[0]}_vs_{right[0]}"
        if any(t in name for t in left) and any(t in desc for t in right):
            return f"family_conflict:{right[0]}_vs_{left[0]}"
    return None


def validate_candidate(
    identity: dict[str, Any],
    candidate: dict[str, Any],
    *,
    expected_condition: str = "NEW",
) -> dict[str, Any]:
    """Fail-closed validation."""
    from price_adapters.models import (
        CONDITION_MISMATCH,
        FOUND_VALID_PRICE,
        UOM_MISMATCH,
        WRONG_PRODUCT,
    )
    from price_coverage_80.guards import check_condition, check_uom_pack, is_credible_seller

    price = candidate.get("unit_price")
    seller = candidate.get("seller") or ""
    url = str(candidate.get("url") or "")
    # Global Industrial search shells emit sitewide $5/$25 placeholders
    if "globalindustrial" in seller.lower():
        if price is not None and float(price) in {5.0, 25.0}:
            return {"ok": False, "status": WRONG_PRODUCT, "reason": "globalindustrial_placeholder"}
        if "/search" in url.lower():
            return {"ok": False, "status": WRONG_PRODUCT, "reason": "globalindustrial_search_shell"}
    # nationaldistributorllc search shells reuse a sitewide $10.58 placeholder
    if "nationaldistributorllc" in seller.lower() or "nationaldistributorllc" in url.lower():
        if "?s=" in url.lower() or "/?s=" in url.lower() or re.search(r"nationaldistributorllc\.com/?(\?|$)", url.lower()):
            if "/product/" not in url.lower():
                return {"ok": False, "status": WRONG_PRODUCT, "reason": "nationaldistributor_search_shell"}
        if price is not None and abs(float(price) - 10.58) < 0.001 and "/product/" not in url.lower():
            return {"ok": False, "status": WRONG_PRODUCT, "reason": "nationaldistributor_placeholder"}
    if not is_credible_seller(seller, price):
        # allow quill/staples/officedepot/msc even if not in old list
        trusted_extra = (
            "quill.com",
            "staples.com",
            "officedepot.com",
            "homedepot.com",
            "lowes.com",
            "acehardware.com",
            "webstaurantstore.com",
            "bradyid.com",
            "crcindustries.com",
            "sharkbite.com",
            "fleetguard.com",
            "cummins.com",
            "dieselpartsdirect.com",
            "thedieselstore.com",
            "motion.com",
            "1000bulbs.com",
            "mccoys.com",
            "rspsupply.com",
        )
        if not any(t in seller.lower() for t in trusted_extra) or not price or float(price) < 1.51:
            return {"ok": False, "status": WRONG_PRODUCT, "reason": "noncredible"}

    mpn = str(identity.get("part_number") or identity.get("mpn") or "")
    name = str(candidate.get("name") or "")
    sku = str(candidate.get("sku") or "")
    # Require MPN evidence in name/sku/url/via path
    if mpn and not mpn_in_blob(mpn, name, sku, url, candidate.get("via") or ""):
        # near_mpn routes already scoped window to MPN
        if "near_mpn" not in str(candidate.get("via") or "") and "exact_mpn" not in str(candidate.get("via") or ""):
            return {"ok": False, "status": WRONG_PRODUCT, "reason": "mpn_not_evidenced"}

    fam = _family_mismatch(identity, candidate)
    if fam:
        # Long exact MPN matches override weak description family heuristics
        # (corpus description can be wrong; exact sku/name MPN is stronger)
        if not (
            mpn
            and len(norm_token(mpn)) >= 8
            and mpn_in_blob(mpn, name, sku)
            and "exact_mpn" in str(candidate.get("via") or "")
        ):
            return {"ok": False, "status": WRONG_PRODUCT, "reason": fam}

    # manufacturer token when provided
    mfr = str(identity.get("manufacturer") or "").strip()
    if mfr and len(mfr) >= 3 and name:
        mfr_tok = mfr.split()[0]
        mfr_norm = norm_token(mfr_tok)
        name_norm = norm_token(name)
        url_norm = norm_token(url)
        mpn_tok = norm_token(mpn)
        short_or_numeric_mpn = len(mpn_tok) < 8 or (
            sum(ch.isdigit() for ch in mpn_tok) / max(len(mpn_tok), 1) >= 0.7
        )
        if mfr_norm not in name_norm and mfr_norm not in url_norm:
            # Short/numeric MPNs collide across brands — require manufacturer evidence
            if short_or_numeric_mpn:
                return {"ok": False, "status": WRONG_PRODUCT, "reason": "manufacturer_missing_short_mpn"}
            # allow near_mpn (window already checked) and exact sku hits for long unique MPNs
            if "near_mpn" not in str(candidate.get("via") or "") and mpn_in_blob(mpn, name, sku):
                pass  # exact MPN in product name is enough even if brand stylized
            elif "near_mpn" not in str(candidate.get("via") or "") and "exact_mpn" not in str(candidate.get("via") or ""):
                return {"ok": False, "status": WRONG_PRODUCT, "reason": "manufacturer_missing"}

    cond = candidate.get("condition") or CONDITION_NEW
    if cond in {"UNKNOWN", None, ""}:
        cond = CONDITION_NEW
        candidate["condition"] = cond
    cc = check_condition(expected=expected_condition, found=cond, new_assumed=expected_condition == "NEW")
    if not cc["valid"]:
        return {"ok": False, "status": CONDITION_MISMATCH, "reason": cc["reason"]}

    uom = check_uom_pack(
        expected_uom=identity.get("expected_uom") or "EA",
        expected_pack=identity.get("expected_pack") or 1,
        candidate_uom=candidate.get("uom"),
        candidate_pack=candidate.get("pack"),
    )
    if not uom["valid"]:
        return {"ok": False, "status": UOM_MISMATCH, "reason": uom["reason"]}

    return {"ok": True, "status": FOUND_VALID_PRICE, "reason": "validated"}


def seller_of(url: str) -> str:
    return urlparse(url or "").netloc.replace("www.", "").lower()
