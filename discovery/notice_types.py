"""Notice type classification — forecast/award ≠ open solicitation."""

from __future__ import annotations

import re
from typing import Any

from discovery.constants import (
    NOTICE_AWARD,
    NOTICE_AWARDED_CONTRACT_CATALOG,
    NOTICE_CANCELLED,
    NOTICE_FORECAST,
    NOTICE_OPEN_SOLICITATION,
    NOTICE_PRESOLICITATION,
    NOTICE_UNKNOWN,
)


def classify_notice_type(
    *,
    title: str | None = None,
    description: str | None = None,
    status: str | None = None,
    source_hint: str | None = None,
) -> dict[str, Any]:
    blob = f"{title or ''} {description or ''} {status or ''} {source_hint or ''}".lower()
    if re.search(r"\bawarded?\s+contract\b|\bcontract\s+catalog\b|\bcurrent\s+contracts?\b", blob):
        return {
            "notice_type": NOTICE_AWARDED_CONTRACT_CATALOG,
            "is_open_solicitation": False,
            "note": "Existing awarded cooperative/catalog contract — not an open bid",
        }
    if re.search(r"\bawarded?\b|\baward\s+notice\b|\bintent\s+to\s+award\b", blob):
        return {"notice_type": NOTICE_AWARD, "is_open_solicitation": False}
    if re.search(r"\bcancell?ed\b|\bwithdrawn\b", blob):
        return {"notice_type": NOTICE_CANCELLED, "is_open_solicitation": False}
    if re.search(r"\bforecast\b|\banticipated\s+procurement\b", blob):
        return {
            "notice_type": NOTICE_FORECAST,
            "is_open_solicitation": False,
            "note": "Forecast is a lead only — not an open solicitation",
        }
    if re.search(r"\bpre[\s-]?solicitation\b|\bsources?\s+sought\b|\brfi\b", blob):
        return {
            "notice_type": NOTICE_PRESOLICITATION,
            "is_open_solicitation": False,
            "note": "Presolicitation is not necessarily bid-ready",
        }
    if re.search(r"\brfp\b|\brfq\b|\bifb\b|\bitb\b|\bbid\b|\bsolicitation\b|\binvitation\b", blob):
        return {"notice_type": NOTICE_OPEN_SOLICITATION, "is_open_solicitation": True}
    return {"notice_type": NOTICE_UNKNOWN, "is_open_solicitation": None}
