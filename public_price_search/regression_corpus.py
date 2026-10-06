"""Permanent public-price regression corpus (manual-known + A-corpus MPNs)."""

from __future__ import annotations

from typing import Any

# Manual-known easy cases — M3 must not return NO_PUBLIC_PRICE for these when live net works.
MANUAL_CASES: list[dict[str, Any]] = [
    {
        "id": "cummins-5579409PX",
        "part_number": "5579409PX",
        "manufacturer": "Cummins",
        "raw_description": "Cummins Injector Kit",
        "expected_seller_substr": "cummins.com|alliantpower|dieselstore|finditparts|advancedtruckparts",
        "expected_price_min": 1500.0,
        "expected_price_max": 3500.0,
        "expected_condition_in": ["RECONDITIONED", "REMANUFACTURED", "NEW", "UNKNOWN"],
        "notes": "Human: shop.cummins.com ~$2065.61 ReCon + $303.75 core",
    },
    {
        "id": "ford-Y502-placeholder",
        "part_number": "Y502",
        "manufacturer": "Ford",
        "raw_description": "Ford Y502",
        "optional": True,
        "notes": "Included for corpus coverage; may be weak identity",
    },
    {
        "id": "smith-blair-317",
        "part_number": "317",
        "manufacturer": "Smith-Blair",
        "raw_description": "Smith-Blair 317 coupling",
        "optional": True,
        "notes": "Short PN — requires manufacturer disambiguation",
    },
]


def a_confidence_mpn_sample(limit: int = 20) -> list[dict[str, Any]]:
    """Pull exact A-confidence MPNs from current identity store."""
    from evidence_breakthrough.corpus import select_identities

    rows = select_identities(limit=max(limit * 4, 80), grades=("A",))
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in rows:
        pn = str(r.get("part_number") or r.get("catalog_number") or "").strip()
        if len(pn) < 5:
            continue
        key = pn.upper()
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "id": f"corpus-{key}",
                "part_number": pn,
                "manufacturer": r.get("manufacturer") or r.get("brand"),
                "raw_description": r.get("raw_description"),
                "opportunity_id": r.get("opportunity_id"),
                "confidence_grade": "A",
                "from_corpus": True,
            }
        )
        if len(out) >= limit:
            break
    return out


def full_regression_set(*, corpus_n: int = 20) -> list[dict[str, Any]]:
    return list(MANUAL_CASES) + a_confidence_mpn_sample(corpus_n)
