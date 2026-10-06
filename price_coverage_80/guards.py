"""UOM/pack + condition protection — fail closed."""

from __future__ import annotations

import re
from typing import Any

from public_price_search.models import (
    CONDITION_MISMATCH,
    CONDITION_NEW,
    CONDITION_RECONDITIONED,
    CONDITION_REMANUFACTURED,
    CONDITION_UNKNOWN,
    CONDITION_USED,
    UOM_AMBIGUOUS,
)

UOM_MATCH_UNKNOWN = "UOM_MATCH_UNKNOWN"
UOM_MATCH_OK = "UOM_MATCH_OK"
UOM_MISMATCH = "UOM_MISMATCH"

_PACK_RE = re.compile(
    r"\b(?:pack|pk|box|bx|case|cs|carton|ct|bag|bg|pair|pr|kit|set)\s*(?:of\s*)?(\d+)\b"
    r"|\b(\d+)\s*(?:pack|pk|box|bx|case|/cs|/bx|/pk)\b",
    re.I,
)


def normalize_uom(uom: str | None) -> str:
    u = (uom or "EA").strip().upper()
    aliases = {
        "EACH": "EA",
        "PC": "EA",
        "PCS": "EA",
        "PIECE": "EA",
        "BOX": "BX",
        "CASE": "CS",
        "PACK": "PK",
        "PAIR": "PR",
        "PAIRS": "PR",
        "KIT": "KT",
        "SET": "SET",
    }
    return aliases.get(u, u)


def extract_pack_from_text(text: str) -> int | None:
    m = _PACK_RE.search(text or "")
    if not m:
        return None
    for g in m.groups():
        if g:
            try:
                return int(g)
            except Exception:
                pass
    return None


def check_uom_pack(
    *,
    expected_uom: str | None,
    expected_pack: int | None,
    page_text: str = "",
    candidate_uom: str | None = None,
    candidate_pack: int | None = None,
) -> dict[str, Any]:
    exp_u = normalize_uom(expected_uom)
    got_u = normalize_uom(candidate_uom) if candidate_uom else None
    pack_from_page = extract_pack_from_text(page_text)
    got_pack = candidate_pack if candidate_pack is not None else pack_from_page

    if expected_pack and got_pack and int(expected_pack) != int(got_pack):
        # Allow EA vs pack only if packs match expectation
        return {
            "status": UOM_MISMATCH,
            "reason": f"pack expected={expected_pack} got={got_pack}",
            "valid": False,
        }
    if got_u and exp_u and got_u != exp_u:
        # EA vs BX with pack evidence — mismatch
        if {got_u, exp_u} <= {"EA", "BX", "CS", "PK", "PR", "KT"} and got_u != exp_u:
            if expected_pack and got_pack and int(expected_pack) == int(got_pack):
                pass  # pack agrees
            else:
                return {
                    "status": UOM_MISMATCH,
                    "reason": f"uom expected={exp_u} got={got_u}",
                    "valid": False,
                }
    if expected_pack and got_pack is None and page_text and len(page_text) > 200:
        # uncertain when pack expected but not found
        if int(expected_pack) > 1:
            return {
                "status": UOM_MATCH_UNKNOWN,
                "reason": "expected_pack>1 but pack not found on page",
                "valid": False,
            }
    return {"status": UOM_MATCH_OK, "reason": "ok", "valid": True}


def check_condition(
    *,
    expected: str = "NEW",
    found: str | None = None,
    new_assumed: bool = True,
) -> dict[str, Any]:
    exp = (expected or "NEW").upper()
    got = (found or CONDITION_UNKNOWN).upper()
    if got == "OPEN_BOX":
        got = CONDITION_USED
    if new_assumed and exp == "NEW":
        if got in {CONDITION_REMANUFACTURED, CONDITION_RECONDITIONED, CONDITION_USED}:
            return {
                "status": CONDITION_MISMATCH,
                "valid": False,
                "reason": f"NEW_ASSUMED but found {got}",
            }
        # UNKNOWN allowed under NEW_ASSUMED (NEW_ASSUMED)
        return {"status": "CONDITION_OK", "valid": True, "reason": f"NEW_ASSUMED/{got}"}
    if exp != got and got != CONDITION_UNKNOWN:
        return {
            "status": CONDITION_MISMATCH,
            "valid": False,
            "reason": f"expected {exp} got {got}",
        }
    return {"status": "CONDITION_OK", "valid": True, "reason": "ok"}


def is_credible_seller(seller: str | None, price: float | None) -> bool:
    from price_coverage_80.models import (
        MIN_CREDIBLE_PRICE,
        NONCREDIBLE_SELLER_PATTERNS,
        TRUSTED_SELLER_SUFFIXES,
    )

    s = (seller or "").lower()
    for pat in NONCREDIBLE_SELLER_PATTERNS:
        if pat in s:
            return False
    if price is not None and float(price) < MIN_CREDIBLE_PRICE:
        return False
    if any(t in s for t in TRUSTED_SELLER_SUFFIXES):
        return True
    # unknown seller with plausible price — allow but scoring may still mark ambiguous
    return bool(s) and price is not None and float(price) >= MIN_CREDIBLE_PRICE
