"""ProductIdentityNormalizer — normalize fields without destroying part-number punctuation."""

from __future__ import annotations

import re
from typing import Any

_UOM_MAP = {
    "EA": "EA",
    "EACH": "EA",
    "PC": "EA",
    "PCS": "EA",
    "PIECE": "EA",
    "PK": "PACK",
    "PACK": "PACK",
    "PKG": "PACK",
    "BX": "BOX",
    "BOX": "BOX",
    "CS": "CASE",
    "CASE": "CASE",
    "SET": "SET",
    "KIT": "KIT",
    "PAIR": "PAIR",
    "PR": "PAIR",
    "FT": "FT",
    "LF": "LF",
    "LB": "LB",
    "LBS": "LB",
    "GAL": "GAL",
    "GALLON": "GAL",
    "DZ": "DOZEN",
    "DOZEN": "DOZEN",
    "RL": "ROLL",
    "ROLL": "ROLL",
}

_PACK = re.compile(
    r"\b(?:box|case|pack|pkg|carton)\s*(?:of|\/)\s*(\d{1,5})\b|\b(\d{1,5})\s*[-/]?\s*pack\b|\b(\d{1,5})\s*/\s*(?:box|case|pack)\b",
    re.I,
)
_NSN = re.compile(r"\b(\d{4})[- ]?(\d{2})[- ]?(\d{3})[- ]?(\d{4})\b")
_UPC = re.compile(r"\b(\d{12,14})\b")


def _clean_keep_pn(s: Any) -> str | None:
    if s is None:
        return None
    t = str(s).strip()
    if not t or t.lower() in {"none", "n/a", "na", "-", "tbd"}:
        return None
    # Collapse whitespace but keep hyphens/slashes/dots
    t = re.sub(r"\s+", " ", t)
    return t or None


def _norm_uom(uom: Any) -> str | None:
    if uom is None:
        return None
    key = re.sub(r"[^A-Za-z]", "", str(uom)).upper()
    return _UOM_MAP.get(key) or (key if key else None)


class ProductIdentityNormalizer:
    """Normalize product identity fields; keep raw originals."""

    def normalize_row(self, row: dict[str, Any]) -> dict[str, Any]:
        mfr_raw = _clean_keep_pn(row.get("manufacturer") or row.get("brand"))
        model_raw = _clean_keep_pn(row.get("model"))
        pn_raw = _clean_keep_pn(row.get("part_number") or row.get("mpn"))
        cat_raw = _clean_keep_pn(row.get("catalog_number") or row.get("cat_no"))
        sku_raw = _clean_keep_pn(row.get("sku"))
        desc_raw = _clean_keep_pn(row.get("description") or row.get("product_description") or row.get("raw_description"))
        nsn_raw = _clean_keep_pn(row.get("nsn"))
        upc_raw = _clean_keep_pn(row.get("upc"))

        nsn = None
        if nsn_raw:
            m = _NSN.search(nsn_raw.replace(" ", ""))
            if m:
                nsn = f"{m.group(1)}-{m.group(2)}-{m.group(3)}-{m.group(4)}"
            elif re.fullmatch(r"\d{13}", re.sub(r"\D", "", nsn_raw) or ""):
                d = re.sub(r"\D", "", nsn_raw)
                nsn = f"{d[0:4]}-{d[4:6]}-{d[6:9]}-{d[9:13]}"

        upc = None
        if upc_raw and _UPC.fullmatch(re.sub(r"\D", "", upc_raw) or ""):
            upc = re.sub(r"\D", "", upc_raw)

        pack_size = None
        pack_note = None
        pack_src = row.get("pack") or row.get("pack_size")
        if pack_src not in (None, ""):
            try:
                pack_size = float(str(pack_src).replace(",", ""))
                pack_note = f"pack={pack_size}"
            except (TypeError, ValueError):
                pack_note = str(pack_src)
        if pack_size is None and desc_raw:
            m = _PACK.search(desc_raw)
            if m:
                pack_size = float(next(g for g in m.groups() if g))
                pack_note = m.group(0)

        qty = row.get("quantity")
        try:
            qty_f = float(str(qty).replace(",", "")) if qty not in (None, "") else None
        except (TypeError, ValueError):
            qty_f = None

        uom_raw = _clean_keep_pn(row.get("uom") or row.get("unit_of_measure"))
        uom_n = _norm_uom(uom_raw)

        # Manufacturer normalize: title-case words, keep Inc/LLC
        mfr = None
        if mfr_raw:
            mfr = re.sub(r"\s+", " ", mfr_raw).strip()
            if (
                len(mfr.split()) > 6
                or len(mfr) > 48
                or re.search(r"\b(notes?:|shipping|requirement|solicitation|insurance)\b", mfr, re.I)
            ):
                mfr = None

        # Part/model: uppercase alnum tokens but preserve separators
        def pn_norm(v: str | None) -> str | None:
            if not v:
                return None
            return v.strip().upper() if re.search(r"\d", v) else v.strip()

        return {
            "manufacturer": mfr,
            "manufacturer_raw": mfr_raw,
            "brand": mfr,
            "model": pn_norm(model_raw),
            "model_raw": model_raw,
            "part_number": pn_norm(pn_raw),
            "part_number_raw": pn_raw,
            "catalog_number": pn_norm(cat_raw),
            "sku": pn_norm(sku_raw),
            "nsn": nsn,
            "upc": upc,
            "raw_description": desc_raw,
            "quantity": qty_f,
            "uom": uom_raw,
            "uom_normalized": uom_n,
            "pack_size": pack_size,
            "pack_note": pack_note,
            "size": _clean_keep_pn(row.get("size")),
            "dimensions": _clean_keep_pn(row.get("dimensions")),
            "material": _clean_keep_pn(row.get("material")),
            "color": _clean_keep_pn(row.get("color")),
            "item": _clean_keep_pn(row.get("item") or row.get("clin") or row.get("line_number")),
            "inherited_manufacturer": bool(row.get("inherited_manufacturer")),
            "unit_price": row.get("unit_price"),
            "extended_price": row.get("extended_price"),
        }
