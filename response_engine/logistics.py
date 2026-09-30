"""R2 logistics — freight / delivery / packaging (UNKNOWN ≠ $0)."""

from __future__ import annotations

import re
from typing import Any

from response_engine.money import D, as_str, money
from response_engine.r2_constants import (
    FREIGHT_INCLUDED_VERIFIED,
    FREIGHT_NA,
    FREIGHT_QUOTED_SEPARATELY,
    FREIGHT_UNKNOWN,
    OWNER_ENTERED,
    PASS_VERIFIED,
    TECHNICAL_FAIL,
    TECHNICAL_UNKNOWN,
    UNKNOWN,
    VERIFIED,
)


def classify_freight(*, included: bool | None = None, amount: Any = None, evidence_state: str | None = None) -> dict[str, Any]:
    amt = D(amount)
    if included is True and evidence_state == VERIFIED:
        return {"freight_status": FREIGHT_INCLUDED_VERIFIED, "amount": "0.00", "evidence_state": VERIFIED}
    if amt is not None and evidence_state in {VERIFIED, OWNER_ENTERED}:
        return {
            "freight_status": FREIGHT_QUOTED_SEPARATELY,
            "amount": as_str(money(amt)),
            "evidence_state": evidence_state,
        }
    if evidence_state == "ESTIMATED" and amt is not None:
        return {"freight_status": "ESTIMATED", "amount": as_str(money(amt)), "evidence_state": "ESTIMATED"}
    if included is False and amt is None:
        return {"freight_status": FREIGHT_UNKNOWN, "amount": None, "evidence_state": UNKNOWN, "blocks_verified_profit": True}
    return {"freight_status": FREIGHT_UNKNOWN, "amount": None, "evidence_state": UNKNOWN, "blocks_verified_profit": True}


def reconcile_fob(*, government_fob: str | None, supplier_fob: str | None) -> dict[str, Any]:
    g = (government_fob or "").lower()
    s = (supplier_fob or "").lower()
    if not g:
        return {"status": UNKNOWN, "additional_transport_required": None, "note": "government FOB unknown"}
    if "destination" in g and ("origin" in s or not s):
        return {
            "status": "TRANSPORT_GAP",
            "additional_transport_required": True,
            "note": "Government FOB Destination; supplier FOB Origin or unknown — price inbound/outbound freight",
        }
    if "destination" in g and "destination" in s:
        return {"status": "ALIGNED", "additional_transport_required": False, "note": "both FOB Destination"}
    return {"status": "REVIEW", "additional_transport_required": None, "note": f"gov={government_fob} supplier={supplier_fob}"}


def evaluate_delivery(
    *,
    buyer_days_aro: Any = None,
    supplier_days_aro: Any = None,
    buffer_days: int = 0,
) -> dict[str, Any]:
    b = D(buyer_days_aro)
    s = D(supplier_days_aro)
    if b is None or s is None:
        return {"delivery_status": TECHNICAL_UNKNOWN, "note": "delivery timing unknown"}
    if s > b:
        return {"delivery_status": TECHNICAL_FAIL, "note": f"supplier {s} days > buyer {b} days ARO"}
    remaining = b - s
    if buffer_days and remaining < buffer_days:
        return {
            "delivery_status": "PASS_WITH_BUFFER",
            "note": f"PASS — LOW BUFFER ({remaining} days)",
            "buffer_days": as_str(remaining),
        }
    return {"delivery_status": PASS_VERIFIED, "note": "within required delivery", "buffer_days": as_str(remaining)}


def packaging_cost_state(
    *,
    military_packaging_required: bool = False,
    supplier_includes: bool | None = None,
    amount: Any = None,
    evidence_state: str | None = None,
) -> dict[str, Any]:
    if not military_packaging_required:
        return {"packaging_status": FREIGHT_NA, "amount": "0.00", "blocks_verified_profit": False}
    if supplier_includes is True and evidence_state == VERIFIED:
        return {
            "packaging_status": "SUPPLIER_INCLUDED",
            "amount": "0.00",
            "evidence_state": VERIFIED,
            "blocks_verified_profit": False,
        }
    amt = D(amount)
    if amt is not None and evidence_state in {VERIFIED, OWNER_ENTERED}:
        return {
            "packaging_status": "QUOTED",
            "amount": as_str(money(amt)),
            "evidence_state": evidence_state,
            "blocks_verified_profit": False,
        }
    return {
        "packaging_status": UNKNOWN,
        "amount": None,
        "evidence_state": UNKNOWN,
        "blocks_verified_profit": True,
        "note": "military packaging required — cost unknown; do not assume $0",
    }


def detect_military_packaging(text: str | None) -> bool:
    t = (text or "").lower()
    return bool(re.search(r"military\s+packag|mil[\-\s]?std\s*2073|special\s+packag", t))
