"""Strict MPN alternate forms — no casual fuzzy matching."""

from __future__ import annotations

import re


def normalize_mpn(mpn: str) -> str:
    return re.sub(r"\s+", "", (mpn or "").strip())


def mpn_variants(mpn: str, *, manufacturer: str | None = None) -> list[str]:
    """Return deterministic alternate forms. Leading zeros preserved as primary."""
    raw = normalize_mpn(mpn)
    if not raw:
        return []
    out: list[str] = []
    seen: set[str] = set()

    def add(v: str) -> None:
        v = (v or "").strip()
        if not v or v in seen:
            return
        seen.add(v)
        out.append(v)

    add(raw)
    add(raw.upper())
    add(raw.lower())
    no_hyphen = raw.replace("-", "")
    add(no_hyphen)
    add(no_hyphen.upper())
    add(no_hyphen.lower())
    no_space = re.sub(r"\s+", "", raw)
    add(no_space)
    # slash variants
    if "/" in raw:
        add(raw.replace("/", "-"))
        add(raw.replace("/", ""))
    # brand-prefixed (safe)
    mfr = (manufacturer or "").strip()
    if mfr and len(mfr) >= 2:
        tok = mfr.split()[0]
        add(f"{tok}{raw}")
        add(f"{tok}-{raw}")
        add(f"{tok} {raw}")
    # hyphen insert for digit-letter boundaries (known-safe pattern only)
    m = re.match(r"^([A-Za-z]+)(\d.+)$", raw)
    if m and "-" not in raw:
        add(f"{m.group(1)}-{m.group(2)}")
    m2 = re.match(r"^(\d+)([A-Za-z].+)$", raw)
    if m2 and "-" not in raw:
        add(f"{m2.group(1)}-{m2.group(2)}")
    return out
