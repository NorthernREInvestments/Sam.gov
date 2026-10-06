"""Hard-miss URL / pack / short-MPN guards."""

from __future__ import annotations

import re

from hard_miss_recovery.models import SHORT_MPN_IDS


def is_collision_prone_mpn(mpn: str) -> bool:
    tok = re.sub(r"[^A-Za-z0-9]", "", mpn or "").upper()
    if tok in SHORT_MPN_IDS:
        return True
    if len(tok) < 6 and sum(ch.isdigit() for ch in tok) >= 3:
        return True
    return False


def reject_wrong_pack_blob(blob: str, *, pack: int = 1, price: float = 0.0) -> bool:
    """True when blob+price indicates wrong pack for expected EA/unit pack."""
    b = (blob or "").lower()
    if pack <= 1 and price >= 40 and any(x in b for x in ("gallon", "128-fl", "128 fl", "case of", "pack of 12", "box of 12")):
        return True
    return False
