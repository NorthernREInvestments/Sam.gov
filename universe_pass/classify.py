"""Canonical cheap product classification for the full live universe.

Taxonomy (owner-facing):
  TANGIBLE_PRODUCT | MIXED_PRODUCT_SERVICE | PURE_SERVICE | CONSTRUCTION | UNKNOWN

Uses deterministic evidence only — title, description, commodity/NAICS/PSC/NIGP,
line-item hints, existing classifications. No expensive model calls.
"""

from __future__ import annotations

import re
from typing import Any

from discovery.classify import classify_discovery_opportunity
from discovery.constants import (
    CLASS_CLEARLY_IRRELEVANT,
    CLASS_CORE_PRODUCT,
    CLASS_PRODUCT_PLUS_SERVICE,
    CLASS_SERVICE,
    CLASS_UNKNOWN,
)

TANGIBLE_PRODUCT = "TANGIBLE_PRODUCT"
MIXED_PRODUCT_SERVICE = "MIXED_PRODUCT_SERVICE"
PURE_SERVICE = "PURE_SERVICE"
CONSTRUCTION = "CONSTRUCTION"
UNKNOWN = "UNKNOWN"

ELIGIBLE_FOR_PROFIT = frozenset({TANGIBLE_PRODUCT, MIXED_PRODUCT_SERVICE})

_CONSTRUCTION = re.compile(
    r"\b("
    r"construction|renovation|remodel|demolition|paving|asphalt|"
    r"concrete\s+pour|site\s+work|general\s+contractor|hvac\s+installation\s+contract|"
    r"roofing\s+replacement|building\s+addition|roadway|bridge\s+repair|"
    r"excavation|grading\s+and\s+drainage"
    r")\b",
    re.I,
)
_INSTALL_HEAVY = re.compile(
    r"\b(labor[\-\s]?only|turnkey\s+construction|design[\-\s]?build|"
    r"installation[\-\s]?only|construction\s+services)\b",
    re.I,
)
_MIXED_KEEP = re.compile(
    r"\b(furnish\s+and\s+install|supply\s+and\s+install|provide\s+and\s+install|"
    r"equipment\s+and\s+installation|furniture\s+.*delivery|with\s+delivery|"
    r"including\s+delivery|minor\s+setup|setup\s+included)\b",
    re.I,
)
_PRODUCT_CODES_HINT = re.compile(
    r"\b(NSN|FSC|PSC|NIGP|UNSPSC|MPN|SKU|P/?N)\b|commodity\s*code",
    re.I,
)

# Map legacy labels onto the canonical taxonomy
_LEGACY_MAP = {
    "PRODUCT": TANGIBLE_PRODUCT,
    "CORE_PRODUCT": TANGIBLE_PRODUCT,
    "PRODUCT_RESALE": TANGIBLE_PRODUCT,
    "TANGIBLE_PRODUCT": TANGIBLE_PRODUCT,
    "PRODUCT_PLUS_SERVICE": MIXED_PRODUCT_SERVICE,
    "MIXED": MIXED_PRODUCT_SERVICE,
    "MIXED_PRODUCT": MIXED_PRODUCT_SERVICE,
    "MIXED_PRODUCT_SERVICE": MIXED_PRODUCT_SERVICE,
    "SERVICE": PURE_SERVICE,
    "PURE_SERVICE": PURE_SERVICE,
    "CONSTRUCTION": CONSTRUCTION,
    "UNKNOWN": UNKNOWN,
    "CLEARLY_IRRELEVANT": UNKNOWN,
}


def _blob(rec: dict[str, Any]) -> str:
    rr = rec.get("row_ref") if isinstance(rec.get("row_ref"), dict) else {}
    parts = [
        rec.get("title"),
        rec.get("description"),
        rr.get("title"),
        rr.get("description"),
        rec.get("naics"),
        rr.get("naics"),
        rec.get("psc"),
        rr.get("psc"),
        rec.get("commodity"),
        rr.get("commodity"),
        " ".join(str(x) for x in (rec.get("commodity_codes") or rr.get("commodity_codes") or [])),
        rec.get("product_service_classification"),
    ]
    # line-item text snippets
    lines = rec.get("line_items") or rr.get("line_items") or []
    if isinstance(lines, list):
        for li in lines[:40]:
            if isinstance(li, dict):
                parts.append(li.get("description") or li.get("item") or "")
            else:
                parts.append(str(li)[:120])
    return " ".join(str(p or "") for p in parts)


def _commodity_hint(rec: dict[str, Any]) -> str:
    rr = rec.get("row_ref") if isinstance(rec.get("row_ref"), dict) else {}
    bits = [
        rec.get("naics"),
        rr.get("naics"),
        rec.get("psc"),
        rr.get("psc"),
        rec.get("nigp"),
        rr.get("nigp"),
        rec.get("unspsc"),
        " ".join(str(x) for x in (rec.get("commodity_codes") or [])),
    ]
    return " ".join(str(b) for b in bits if b)


def classify_universe_opportunity(rec: dict[str, Any]) -> dict[str, Any]:
    """Cheap deterministic classification → canonical taxonomy."""
    title = str(rec.get("title") or (rec.get("row_ref") or {}).get("title") or "")
    desc = str(rec.get("description") or (rec.get("row_ref") or {}).get("description") or "")
    blob = _blob(rec)
    commodity = _commodity_hint(rec)

    # Honor strong prior classifications when already in taxonomy / legacy map
    prior = str(rec.get("product_service_classification") or "").upper().strip()
    mapped_prior = _LEGACY_MAP.get(prior)

    # Construction first when language dominates and no strong goods signal
    construction_hit = bool(_CONSTRUCTION.search(blob))
    install_heavy = bool(_INSTALL_HEAVY.search(blob))
    mixed_keep = bool(_MIXED_KEEP.search(blob))
    code_hint = bool(_PRODUCT_CODES_HINT.search(blob) or commodity.strip())

    disc = classify_discovery_opportunity(
        title=title,
        description=desc,
        commodity_hint=commodity or None,
        status=str(rec.get("source_status") or rec.get("status") or ""),
    )
    disc_cls = disc.get("classification")

    # Line items / schedules → strong tangible signal
    has_lines = bool(
        rec.get("line_items")
        or (isinstance(rec.get("row_ref"), dict) and rec["row_ref"].get("line_items"))
        or rec.get("attachments_metadata")
    )
    lie = rec.get("line_item_economics") or (rec.get("row_ref") or {}).get("line_item_economics")
    if isinstance(lie, dict) and (lie.get("total_lines") or lie.get("rollup")):
        has_lines = True

    if mapped_prior == TANGIBLE_PRODUCT and not (construction_hit and install_heavy and not mixed_keep):
        final = TANGIBLE_PRODUCT
        reason = "prior_product_class"
    elif mapped_prior == MIXED_PRODUCT_SERVICE:
        final = MIXED_PRODUCT_SERVICE
        reason = "prior_mixed_class"
    elif mapped_prior == PURE_SERVICE and not code_hint and not has_lines:
        final = PURE_SERVICE
        reason = "prior_service_class"
    elif mapped_prior == CONSTRUCTION:
        final = CONSTRUCTION
        reason = "prior_construction_class"
    elif construction_hit and install_heavy and not mixed_keep and disc_cls not in {
        CLASS_CORE_PRODUCT,
        CLASS_PRODUCT_PLUS_SERVICE,
    }:
        final = CONSTRUCTION
        reason = "construction_phrase"
    elif construction_hit and not mixed_keep and disc_cls == CLASS_SERVICE:
        final = CONSTRUCTION
        reason = "construction_service"
    elif disc_cls == CLASS_CORE_PRODUCT or (has_lines and disc_cls != CLASS_SERVICE):
        final = TANGIBLE_PRODUCT
        reason = "discovery_core_product" if disc_cls == CLASS_CORE_PRODUCT else "line_items_or_schedule"
    elif disc_cls == CLASS_PRODUCT_PLUS_SERVICE or mixed_keep:
        # Material product component → keep eligible
        if install_heavy and construction_hit and not mixed_keep:
            final = CONSTRUCTION
            reason = "install_heavy_construction"
        else:
            final = MIXED_PRODUCT_SERVICE
            reason = "mixed_product_service"
    elif disc_cls == CLASS_SERVICE:
        final = PURE_SERVICE
        reason = "discovery_service"
    elif disc_cls == CLASS_CLEARLY_IRRELEVANT:
        final = UNKNOWN
        reason = "irrelevant_or_award_notice"
    elif code_hint and not construction_hit:
        final = TANGIBLE_PRODUCT
        reason = "commodity_code_hint"
    else:
        final = UNKNOWN
        reason = "ambiguous"

    eligible = final in ELIGIBLE_FOR_PROFIT
    # Mixed with substantial product (mixed_keep / product tokens) stays eligible;
    # construction-heavy mixed already mapped to CONSTRUCTION above.
    return {
        "class": final,
        "eligible_for_profit_research": eligible,
        "reason": reason,
        "discovery_classification": disc_cls,
        "signals": {
            **(disc.get("signals") or {}),
            "construction": construction_hit,
            "install_heavy": install_heavy,
            "mixed_keep": mixed_keep,
            "commodity_codes": bool(commodity.strip()),
            "has_line_items_or_schedule": has_lines,
            "prior": prior or None,
        },
        "method": "deterministic_v1",
        "LIVE_API_REQUESTS": 0,
        "OpenAI": 0,
    }
