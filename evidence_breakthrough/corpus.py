"""Select staged identity corpus from m3_product_identity_store."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from m3_data_root import data_path

GRADE_ORDER = {"A": 0, "B": 1, "C": 2}


def load_identity_store(path: Path | None = None) -> dict[str, Any]:
    p = path or data_path("m3_product_identity_store.json")
    if not p.exists():
        return {"kind": "ProductIdentityStore", "by_opportunity": {}}
    return json.loads(p.read_text(encoding="utf-8"))


_COMMERCIAL_MFR = re.compile(
    r"\b(cummins|ford|smith-?blair|grainger|victaulic|murphy|caterpillar|cat\b|"
    r"detroit\s*diesel|allison|gillig|neopart|skf|timken|gates|dayco|fleetguard|"
    r"donaldson|delco|bosch|parker|emerson|honeywell|siemens|abb|eaton|square\s*d|"
    r"3m|milwaukee|dewalt|makita|hilti|john\s*deere|bobcat|kohler|generac)\b",
    re.I,
)


def _priority_score(ident: dict[str, Any], pack: dict[str, Any]) -> tuple:
    g = str(ident.get("confidence_grade") or "Z")
    grade = GRADE_ORDER.get(g, 9)
    has_mfr = 0 if ident.get("manufacturer") or ident.get("brand") else 1
    pn = str(ident.get("part_number") or ident.get("catalog_number") or "")
    model = str(ident.get("model") or "")
    key = pn or model
    has_mpn = 0 if key else 1
    qty = 0 if ident.get("quantity") else 1
    uom = 0 if (ident.get("uom") or ident.get("uom_normalized")) else 1
    multi = 0 if int(pack.get("usable_count") or 0) >= 3 else 1
    # Prefer digit-bearing commercial MPNs (not ANSI/spec section codes like B16.3 / S1.0)
    digit = 0 if re.search(r"\d", key) else 1
    alnum_len = len(re.sub(r"[^A-Z0-9]", "", key.upper()))
    strong_mpn = 0 if (alnum_len >= 5 and re.search(r"\d", key)) else 1
    # Penalize short spec-like tokens (B16.3, S1.0, A1)
    specish = 0
    if re.fullmatch(r"[A-Z]{1,3}\d{0,2}\.?\d{0,2}", key.upper().replace(" ", "")):
        specish = 1
    mfr_blob = f"{ident.get('manufacturer') or ''} {ident.get('brand') or ''} {ident.get('raw_description') or ''}"
    commercial = 0 if _COMMERCIAL_MFR.search(mfr_blob) else 1
    # Inventory / parts solicitations tend to have recurring history
    oid = str(ident.get("opportunity_id") or "")
    inventory_buyer = 0 if "go-metro" in oid or "inventory" in (ident.get("raw_description") or "").lower() else 1
    return (grade, strong_mpn, commercial, specish, has_mpn, has_mfr, digit, inventory_buyer, multi, qty, uom)


def _buyers_with_priced_history() -> set[str]:
    try:
        root = data_path("opengov_buyer_history")
        if not root.exists():
            return set()
        out: set[str] = set()
        for p in root.glob("*.json"):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            if len(d.get("priced_lines") or []) > 0:
                out.add(str(d.get("government_code") or p.stem).lower())
        return out
    except Exception:
        return set()


def select_identities(
    *,
    limit: int = 50,
    grades: tuple[str, ...] = ("A", "B", "C"),
    store: dict[str, Any] | None = None,
    full: bool = False,
    require_id_token: bool = True,
) -> list[dict[str, Any]]:
    """Return prioritized usable identities with opportunity context.

    full=True: no per-opportunity soft-cap; return all matching identities up to limit
    (use limit>=population for conservation-complete processing).
    """
    store = store or load_identity_store()
    by_opp = store.get("by_opportunity") or {}
    priced_buyers = _buyers_with_priced_history()
    rows: list[dict[str, Any]] = []
    for oid, pack in by_opp.items():
        if not isinstance(pack, dict):
            continue
        for ident in pack.get("identities") or []:
            if not isinstance(ident, dict):
                continue
            g = ident.get("confidence_grade")
            if g not in grades:
                continue
            if require_id_token and not (
                ident.get("part_number")
                or ident.get("catalog_number")
                or ident.get("model")
                or ident.get("sku")
                or ident.get("nsn")
            ):
                continue
            row = {
                **ident,
                "opportunity_id": oid,
                "_pack_usable": pack.get("usable_count"),
            }
            rows.append(row)

    def _full_score(r: dict[str, Any]) -> tuple:
        base = _priority_score(r, by_opp.get(r["opportunity_id"]) or {})
        oid = str(r.get("opportunity_id") or "")
        code = oid.split(":")[1] if oid.startswith("opengov:") and oid.count(":") >= 2 else ""
        # Prefer buyers already known to have public bid tabs
        hist = 0 if code in priced_buyers or code == "go-metro" else 1
        return (hist, *base)

    rows.sort(key=_full_score)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    per_opp: dict[str, int] = {}
    # Soft-cap only when not full — priced-history buyers uncapped within limit
    per_opp_cap = (10**9) if full else (max(8, min(40, limit // 4)) if limit >= 50 else limit)
    for r in rows:
        oid = str(r.get("opportunity_id") or "")
        code = oid.split(":")[1] if oid.startswith("opengov:") and oid.count(":") >= 2 else ""
        cap = limit if (full or code in priced_buyers) else per_opp_cap
        if per_opp.get(oid, 0) >= cap:
            continue
        key = "|".join(
            [
                oid,
                str(r.get("part_number") or r.get("catalog_number") or ""),
                str(r.get("model") or ""),
                str(r.get("manufacturer") or ""),
            ]
        ).upper()
        if key in seen:
            continue
        seen.add(key)
        per_opp[oid] = per_opp.get(oid, 0) + 1
        out.append(r)
        if len(out) >= limit:
            return out
    if not full:
        # Pass 2: fill remaining from best remaining rows
        for r in rows:
            oid = str(r.get("opportunity_id") or "")
            key = "|".join(
                [
                    oid,
                    str(r.get("part_number") or r.get("catalog_number") or ""),
                    str(r.get("model") or ""),
                    str(r.get("manufacturer") or ""),
                ]
            ).upper()
            if key in seen:
                continue
            seen.add(key)
            out.append(r)
            if len(out) >= limit:
                break
    return out


def opportunity_groups(identities: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for ident in identities:
        oid = str(ident.get("opportunity_id") or "")
        groups.setdefault(oid, []).append(ident)
    return groups
