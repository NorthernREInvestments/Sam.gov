"""Commercial product fingerprints + light UPC/title enrichment."""

from __future__ import annotations

import json
import re
from typing import Any

from m3_data_root import data_path
from open_seller_expansion.models import FINGERPRINT_DB
from price_adapters.validate import seller_of

_UPC_RE = re.compile(r"\b(?:UPC|EAN|GTIN|Barcode)[:\s#]*([0-9]{8,14})\b", re.I)
_BARE_UPC = re.compile(r"\b([0-9]{12,14})\b")


def normalize_mpn(mpn: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (mpn or "").upper())


def build_fingerprint(item: dict[str, Any]) -> dict[str, Any]:
    mpn = str(item.get("mpn") or item.get("part_number") or "").strip()
    mfr = str(item.get("manufacturer") or "").strip()
    desc = str(item.get("description") or "").strip()
    cat = str(item.get("category") or "").strip().lower()
    pack = int(item.get("expected_pack") or 1)
    uom = str(item.get("expected_uom") or "EA").upper()
    title = desc or f"{mfr} {mpn}".strip()
    # Canonical title for quoted search
    bits = [mfr, mpn]
    for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9\-/\.]{2,}", desc):
        t = token.strip()
        if t.lower() in {mfr.lower(), mpn.lower()}:
            continue
        if t not in bits:
            bits.append(t)
        if len(bits) >= 6:
            break
    canonical = " ".join(bits[:6])
    short = len(normalize_mpn(mpn)) <= 5
    return {
        "benchmark_id": item.get("benchmark_id"),
        "manufacturer": mfr,
        "mpn": mpn,
        "normalized_mpn": normalize_mpn(mpn),
        "upc": item.get("upc") or item.get("upc_gtin") or None,
        "gtin": item.get("gtin") or None,
        "manufacturer_sku": item.get("manufacturer_sku") or mpn,
        "exact_title": title[:160],
        "canonical_title": canonical[:160],
        "pack": pack,
        "uom": uom,
        "category": cat,
        "product_family": item.get("product_family") or cat,
        "short_mpn": short,
        "requires_mfr_family": short,
    }


def enrich_upc_from_html(html: str, fp: dict[str, Any]) -> dict[str, Any]:
    """Pull UPC/GTIN from identity-reference HTML if present."""
    if not html:
        return fp
    m = _UPC_RE.search(html)
    if m:
        code = m.group(1)
        if not fp.get("upc"):
            fp["upc"] = code
        if len(code) >= 13 and not fp.get("gtin"):
            fp["gtin"] = code
        return fp
    # Prefer schema.org gtin
    for key in ("gtin13", "gtin12", "gtin14", "gtin", "sku"):
        mm = re.search(rf'"{key}"\s*:\s*"([^"]+)"', html, re.I)
        if mm:
            val = mm.group(1).strip()
            if val.isdigit() and len(val) >= 8:
                if key.startswith("gtin") and not fp.get("gtin"):
                    fp["gtin"] = val
                if not fp.get("upc") and len(val) in {12, 13, 14}:
                    fp["upc"] = val
                break
    return fp


def persist_fingerprint(fp: dict[str, Any]) -> None:
    p = data_path(FINGERPRINT_DB)
    p.parent.mkdir(parents=True, exist_ok=True)
    db: dict[str, Any] = {}
    if p.exists():
        try:
            db = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            db = {}
    items = db.setdefault("items", {})
    bid = str(fp.get("benchmark_id") or "")
    if bid:
        items[bid] = {**items.get(bid, {}), **fp}
    db["build"] = "20261005-m3-open-seller-expansion-v1"
    p.write_text(json.dumps(db, indent=2, default=str), encoding="utf-8")


def try_upc_from_identity_url(url: str, fp: dict[str, Any]) -> dict[str, Any]:
    """Fetch identity-reference page lightly for UPC only (no price extract)."""
    if not url or not url.startswith("http"):
        return fp
    try:
        from public_price_search.search import fetch_page

        fr = fetch_page(url, use_budget=True)
        html = fr.get("text") or ""
        if not html:
            return fp
        fp = enrich_upc_from_html(html, fp)
        fp.setdefault("identity_refs", []).append(
            {"url": url, "domain": seller_of(url), "upc_found": bool(fp.get("upc"))}
        )
    except Exception:
        pass
    return fp
