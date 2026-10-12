"""Detect as-needed / IDIQ-style award terms that invalidate full-qty profit assumptions.

Reusable screening for OpenGov (and similar) solicitations.
Does not modify operator UI layout — emits structured flags for downstream gates.
"""

from __future__ import annotations

import re
from typing import Any

BUILD = "20261010-m3-as-needed-contract-screening-v1"

_AS_NEEDED = re.compile(
    r"\b(as[-\s]?needed|when[-\s]?needed|indefinite\s+quantity|IDIQ|"
    r"requirements\s+contract|blanket\s+purchase|BPA)\b",
    re.I,
)
_ESTIMATE_ONLY = re.compile(
    r"(quantit(?:y|ies)\s+(?:listed\s+)?(?:are\s+)?(?:an?\s+)?estimate|"
    r"estimate\s+only|estimated\s+quantit|"
    r"not\s+(?:be\s+)?required\s+to\s+purchase\s+any\s+minimum|"
    r"no\s+minimum\s+(?:or\s+maximum\s+)?(?:purchase\s+)?(?:commitment|quantit))",
    re.I,
)
_MIN_ORDER = re.compile(
    r"\b(minimum\s+order(?:\s+quantity)?|MOQ|minimum\s+purchase\s+of\s+\$?\d)",
    re.I,
)
_SHORT_DELIVERY = re.compile(
    r"(?:within|inside)\s+(?:(\d+)|(one|two|three|four|five|six|seven|eight|nine|ten))"
    r"(?:\s*\(\s*(\d+)\s*\))?\s*(?:calendar\s+|business\s+|working\s+)?days?",
    re.I,
)
_WORD_DAYS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}
_FOB_DEST = re.compile(
    r"\bFOB\b.{0,20}\bDestination\b|\bFreight\s+On\s+Board\b.{0,20}\bDestination\b|"
    r"\bfreight\s+(?:on\s+board\s+)?destination\b",
    re.I,
)
_FREIGHT_IN_PRICE = re.compile(
    r"(shipping|handling|freight).{0,40}(included\s+in\s+(?:the\s+)?(?:quote|bid|price)|"
    r"shall\s+be\s+included)|prices?\s+must\s+include.{0,30}(shipping|freight)",
    re.I,
)
_FIRM_PRICE = re.compile(
    r"(bid\s+prices?\s+shall\s+be\s+firm|prices?\s+(?:shall|must)\s+remain\s+firm|"
    r"shall\s+not\s+be\s+amended\s+after)",
    re.I,
)
_LINE_OR_AGG = re.compile(
    r"award.{0,40}(by\s+division|line\s+item|aggregate)|"
    r"reserves\s+the\s+right\s+to\s+award.{0,60}(line\s+item|aggregate)",
    re.I,
)
_STOCKING = re.compile(
    r"\b(stock(?:ing)?\s+required|maintain\s+inventory|must\s+be\s+in\s+stock|"
    r"warehouse\s+in\s+.{0,20}florida|local\s+stock)\b",
    re.I,
)
_ESCALATION = re.compile(
    r"\b(price\s+escalation|CPI\s+adjustment|annual\s+increase|escalat(?:e|ion)\s+clause)\b",
    re.I,
)


def screen_as_needed_contract_text(text: str, *, title: str | None = None) -> dict[str, Any]:
    blob = f"{title or ''}\n{text or ''}"
    m_del = _SHORT_DELIVERY.search(blob)
    delivery_days = None
    if m_del:
        if m_del.group(1):
            delivery_days = int(m_del.group(1))
        elif m_del.group(3):
            delivery_days = int(m_del.group(3))
        elif m_del.group(2):
            delivery_days = _WORD_DAYS[m_del.group(2).lower()]

    as_needed = bool(_AS_NEEDED.search(blob) or _ESTIMATE_ONLY.search(blob))
    estimate_only = bool(_ESTIMATE_ONLY.search(blob))
    has_min_order = bool(_MIN_ORDER.search(blob)) and not estimate_only
    # If estimate-only says no minimum, that is NOT an MOQ — it means zero guarantee
    no_min_purchase = bool(
        re.search(r"not\s+(?:be\s+)?required\s+to\s+purchase\s+any\s+minimum", blob, re.I)
        or re.search(r"no\s+minimum\s+(?:or\s+maximum\s+)?(?:purchase\s+)?commitment", blob, re.I)
    )

    flags = {
        "AS_NEEDED_OR_IDIQ": as_needed,
        "QUANTITIES_ESTIMATE_ONLY": estimate_only or as_needed,
        "NO_MINIMUM_PURCHASE_COMMITMENT": no_min_purchase or (estimate_only and as_needed),
        "MINIMUM_ORDER_QUANTITY_PRESENT": has_min_order and not no_min_purchase,
        "SHORT_DELIVERY_ARO_DAYS": delivery_days,
        "SHORT_DELIVERY_LE_5_DAYS": bool(delivery_days is not None and delivery_days <= 5),
        "FOB_DESTINATION": bool(_FOB_DEST.search(blob)),
        "FREIGHT_MUST_BE_IN_BID_PRICE": bool(_FREIGHT_IN_PRICE.search(blob) or _FOB_DEST.search(blob)),
        "FIRM_FIXED_PRICE_TERM": bool(_FIRM_PRICE.search(blob)),
        "AWARD_MAY_BE_LINE_OR_AGGREGATE": bool(_LINE_OR_AGG.search(blob)),
        "STOCKING_OR_INVENTORY_HINT": bool(_STOCKING.search(blob)),
        "PRICE_ESCALATION_CLAUSE_PRESENT": bool(_ESCALATION.search(blob)),
    }

    # Profitability model impacts
    impacts = []
    if flags["QUANTITIES_ESTIMATE_ONLY"] or flags["NO_MINIMUM_PURCHASE_COMMITMENT"]:
        impacts.append("DO_NOT_TREAT_SCHEDULE_QTY_AS_GUARANTEED_REVENUE")
        impacts.append("MODEL_DRAWDOWN_SCENARIOS_1_UNIT_25_50_100")
    if flags["SHORT_DELIVERY_LE_5_DAYS"]:
        impacts.append("REQUIRES_STOCK_OR_FAST_SUPPLIER_PATH")
    if flags["FREIGHT_MUST_BE_IN_BID_PRICE"]:
        impacts.append("FREIGHT_MUST_BE_IN_UNIT_OR_EXTENDED_PRICE")
    if flags["FIRM_FIXED_PRICE_TERM"] and not flags["PRICE_ESCALATION_CLAUSE_PRESENT"]:
        impacts.append("FIXED_PRICE_ESCALATION_RISK_OVER_TERM")

    return {
        "build": BUILD,
        "FLAGS": flags,
        "IMPACTS": impacts,
        "GUARANTEED_MINIMUM_REVENUE": 0.0 if flags["NO_MINIMUM_PURCHASE_COMMITMENT"] or flags["QUANTITIES_ESTIMATE_ONLY"] else None,
        "GUARANTEED_MINIMUM_REVENUE_STATE": (
            "PROVEN_ZERO"
            if flags["NO_MINIMUM_PURCHASE_COMMITMENT"] or flags["QUANTITIES_ESTIMATE_ONLY"]
            else "UNKNOWN"
        ),
    }
