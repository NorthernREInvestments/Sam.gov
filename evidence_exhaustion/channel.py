"""Specialty/OEM channel intelligence — revenue/channel evidence only.

Prior award history is NEVER acquisition-cost evidence.
Build: 20261004-m3-evidence-exhaustion-v1
"""

from __future__ import annotations

import re
from typing import Any

from evidence_exhaustion.models import (
    AUTHORIZED_DISTRIBUTOR_PATH,
    INCUMBENT_CHANNEL_ADVANTAGE,
    OEM_DIRECT_POSSIBLE,
    OPEN_RESELLER_CHANNEL_CONFIRMED,
)

_OEM_HINT = re.compile(
    r"\b(?:inc|llc|corp|corporation|company|manufactur|oem|industries)\b",
    re.I,
)
_DISTRIBUTOR_HINT = re.compile(
    r"\b(?:distribut|wholesale|supply|supplies|parts\s+co|fleet|diesel|"
    r"grainger|fastenal|zoro|msc)\b",
    re.I,
)
_RESELLER_HINT = re.compile(
    r"\b(?:reseller|dealer|retail|auto\s+parts|truck\s+parts|industrial)\b",
    re.I,
)


def _classify_winner(name: str) -> str:
    n = (name or "").strip()
    if not n:
        return "UNKNOWN"
    if _DISTRIBUTOR_HINT.search(n):
        return "DISTRIBUTOR"
    if _RESELLER_HINT.search(n):
        return "RESELLER"
    if _OEM_HINT.search(n) and not _DISTRIBUTOR_HINT.search(n):
        return "OEM"
    # Default independent seller → reseller channel signal
    return "RESELLER"


def research_channel(
    identity: dict[str, Any],
    *,
    opportunity_id: str | None = None,
    history_hits: list[dict[str, Any]] | None = None,
    manufacturer: str | None = None,
) -> dict[str, Any]:
    """Build channel signals from prior winners / manufacturer path — not cost."""
    mfr = manufacturer or identity.get("manufacturer") or identity.get("brand")
    hits = list(history_hits or [])
    winners: list[dict[str, Any]] = []
    for h in hits:
        name = str(
            h.get("vendor")
            or h.get("awardee")
            or h.get("bidder")
            or h.get("supplier")
            or h.get("company")
            or ""
        )
        if not name:
            continue
        winners.append(
            {
                "name": name,
                "class": _classify_winner(name),
                "unit_price": h.get("unit_price") or h.get("gov_unit_price"),
                "source": h.get("source") or h.get("basis"),
                "note": "CHANNEL_EVIDENCE_ONLY_NOT_ACQUISITION_COST",
            }
        )

    classes = [w["class"] for w in winners]
    reseller_n = sum(1 for c in classes if c in {"RESELLER", "DISTRIBUTOR"})
    oem_n = sum(1 for c in classes if c == "OEM")
    distinct_resellers = len(
        {w["name"].upper() for w in winners if w["class"] in {"RESELLER", "DISTRIBUTOR"}}
    )

    signals: list[str] = []
    if reseller_n >= 1 and oem_n == 0:
        signals.append(OPEN_RESELLER_CHANNEL_CONFIRMED)
    if distinct_resellers >= 2:
        if OPEN_RESELLER_CHANNEL_CONFIRMED not in signals:
            signals.append(OPEN_RESELLER_CHANNEL_CONFIRMED)
    # Same winner repeatedly
    names = [w["name"].upper() for w in winners]
    if names:
        top = max(set(names), key=names.count)
        if names.count(top) >= 2 and len(names) >= 2:
            signals.append(INCUMBENT_CHANNEL_ADVANTAGE)

    if mfr:
        # Manufacturer contact path always possible to attempt
        signals.append(OEM_DIRECT_POSSIBLE)
        # Known broad brands often publish distributor maps
        mfr_u = str(mfr).upper()
        if any(x in mfr_u for x in ("CUMMINS", "FLEETGUARD", "CATERPILLAR", "FORD", "3M", "GRAINGER")):
            signals.append(AUTHORIZED_DISTRIBUTOR_PATH)

    quote_sub = None
    if OEM_DIRECT_POSSIBLE in signals and OPEN_RESELLER_CHANNEL_CONFIRMED not in signals:
        quote_sub = "OEM_QUOTE_REQUIRED"
    elif AUTHORIZED_DISTRIBUTOR_PATH in signals:
        quote_sub = "DISTRIBUTOR_QUOTE_REQUIRED"
    elif OPEN_RESELLER_CHANNEL_CONFIRMED in signals:
        quote_sub = "SUPPLIER_QUOTE_REQUIRED"
    else:
        quote_sub = "CHANNEL_CONTACT_REQUIRED"

    return {
        "channel_research_exhausted": True,
        "prior_winners": winners[:8],
        "prior_winner_oem": oem_n,
        "prior_winner_distributor": sum(1 for c in classes if c == "DISTRIBUTOR"),
        "prior_winner_reseller": sum(1 for c in classes if c == "RESELLER"),
        "signals": signals,
        "open_reseller_channel": OPEN_RESELLER_CHANNEL_CONFIRMED in signals,
        "incumbent_advantage": INCUMBENT_CHANNEL_ADVANTAGE in signals,
        "oem_direct_possible": OEM_DIRECT_POSSIBLE in signals,
        "authorized_distributor_path": AUTHORIZED_DISTRIBUTOR_PATH in signals,
        "suggested_quote_substatus": quote_sub,
        "acquisition_cost_from_history": False,  # hard rule
        "opportunity_id": opportunity_id,
    }
