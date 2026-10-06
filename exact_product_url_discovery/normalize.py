"""Safe MPN normalization — punctuation-only variants."""

from __future__ import annotations

import re

from exact_product_url_discovery.models import SHORT_MPN_TOKENS


def norm_token(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", s or "").upper()


def mpn_variants(mpn: str) -> list[str]:
    """Generate safe variants: original, lower, no-punct. No fuzzy/family matches."""
    raw = (mpn or "").strip()
    if not raw:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for v in (raw, raw.lower(), raw.upper(), norm_token(raw), norm_token(raw).lower()):
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    # dashed lower if original has dashes
    if "-" in raw:
        compact = raw.replace("-", "").replace(" ", "")
        for v in (compact, compact.lower(), compact.upper()):
            if v and v not in seen:
                seen.add(v)
                out.append(v)
    return out


def is_short_mpn(mpn: str) -> bool:
    tok = norm_token(mpn)
    if tok in SHORT_MPN_TOKENS:
        return True
    # Pure/mostly-numeric catalog codes are collision-prone
    if len(tok) <= 5 and tok.isdigit():
        return True
    if len(tok) <= 4 and sum(ch.isdigit() for ch in tok) >= 3:
        return True
    return False


def mpn_in_text(mpn: str, *parts: str) -> bool:
    """Digit-run-safe MPN presence check."""
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
        if after.isdigit() and target[-1].isdigit():
            idx = j + 1
            continue
        if before.isdigit() and target[0].isdigit():
            idx = j + 1
            continue
        return True
