"""Bridge verified supplier payment terms into capital-stack inputs.

Does not create a second supplier database — consumes opportunity-selected
supplier_terms and/or M3 supplier commercial terms when verified.
"""

from __future__ import annotations

from typing import Any

from financing_intelligence.store import money, money_str


def _fact_value(node: Any) -> Any:
    if isinstance(node, dict) and "value" in node:
        return node.get("value")
    return node


def _fact_status(node: Any) -> str:
    if isinstance(node, dict):
        return str(node.get("status") or node.get("verification_status") or "").upper()
    return ""


def is_verified_terms(terms: dict[str, Any] | None) -> bool:
    """Supplier cover only when terms are explicitly verified (or operator-marked verified)."""
    if not terms:
        return False
    if terms.get("verified") is True:
        return True
    status = str(terms.get("verification_status") or terms.get("status") or "").upper()
    if status == "VERIFIED":
        return True
    # Nested commercial-terms fact style
    net = terms.get("net_terms") or terms.get("terms")
    if isinstance(net, dict) and str(net.get("status") or "").upper() == "VERIFIED":
        return True
    credit = terms.get("available_credit") or terms.get("approved_credit_limit")
    if isinstance(credit, dict) and str(credit.get("status") or "").upper() == "VERIFIED":
        return True
    return False


def parse_net_days(terms: dict[str, Any]) -> int | None:
    if terms.get("net_days") not in (None, ""):
        try:
            return int(terms["net_days"])
        except (TypeError, ValueError):
            pass
    raw = _fact_value(terms.get("net_terms") or terms.get("payment_terms") or terms.get("terms_label") or "")
    text = str(raw or "").upper().replace(" ", "")
    for n in (15, 30, 45, 60, 90):
        if f"NET{n}" in text or f"NET-{n}" in text.replace("--", "-"):
            return n
    if "PREPAID" in text or text == "COD":
        return 0
    return None


def verified_supplier_cover(terms: dict[str, Any] | None) -> dict[str, Any]:
    """Return stack-usable supplier coverage. Unverified → zero cover."""
    terms = dict(terms or {})
    verified = is_verified_terms(terms)
    credit_raw = (
        terms.get("available_remaining_credit")
        or terms.get("available_credit")
        or terms.get("net_terms_cover")
        or terms.get("approved_credit_limit")
        or terms.get("credit_limit")
        or 0
    )
    credit = money(_fact_value(credit_raw) if isinstance(credit_raw, dict) else credit_raw)
    net_days = parse_net_days(terms)
    deposit_pct = money(_fact_value(terms.get("deposit_percentage") or 0))
    term_type = str(terms.get("term_type") or terms.get("payment_terms") or "").upper()

    # Prepaid / COD never bridges a financing gap
    if term_type in {"PREPAID", "COD"} or net_days == 0:
        credit = money(0)

    if not verified:
        return {
            "verified": False,
            "usable_credit": "0.00",
            "net_days": net_days,
            "supplier_name": terms.get("supplier_name"),
            "reason": "Supplier terms not verified — do not count toward capital stack",
            "term_type": term_type or None,
            "raw": terms,
        }

    # Do not invent credit from "Net-30" alone
    if credit <= 0 and terms.get("covers_full_supplier_cost") and net_days and net_days > 0:
        # Explicit operator flag only — still requires verified
        pass

    return {
        "verified": True,
        "usable_credit": money_str(credit),
        "net_days": net_days,
        "supplier_name": terms.get("supplier_name"),
        "term_type": term_type or (f"NET_{net_days}" if net_days else None),
        "deposit_percentage": money_str(deposit_pct) if deposit_pct else None,
        "reseller_account_status": terms.get("reseller_account_status"),
        "terms_confirmed_date": terms.get("terms_confirmed_date"),
        "terms_expiration_date": terms.get("terms_expiration_date") or terms.get("terms_review_date"),
        "evidence": terms.get("evidence") or terms.get("source"),
        "notes": terms.get("notes") or terms.get("supplier_terms_notes"),
        "reason": None,
        "raw": terms,
    }


def from_m3_commercial_terms(
    commercial: dict[str, Any] | None,
    *,
    supplier_name: str | None = None,
) -> dict[str, Any] | None:
    """Pull one verified supplier entry from M3SupplierCommercialTerms."""
    if not commercial:
        return None
    suppliers = commercial.get("suppliers") or []
    chosen = None
    for s in suppliers:
        if not isinstance(s, dict):
            continue
        if supplier_name and s.get("supplier_name") != supplier_name:
            continue
        terms_node = s.get("terms") or {}
        net = terms_node.get("net_terms")
        if _fact_status(net) == "VERIFIED" or s.get("verified") is True:
            chosen = s
            break
        if not supplier_name and chosen is None:
            # Prefer verified; otherwise skip
            continue
    if not chosen:
        for s in suppliers:
            if isinstance(s, dict) and (not supplier_name or s.get("supplier_name") == supplier_name):
                # Still return structure but unverified
                chosen = s
                break
    if not chosen:
        return None
    terms_node = chosen.get("terms") or {}
    net = terms_node.get("net_terms")
    verified = _fact_status(net) == "VERIFIED" or chosen.get("verified") is True
    credit = chosen.get("available_credit") or chosen.get("approved_credit_limit") or chosen.get("credit_limit")
    return {
        "supplier_name": chosen.get("supplier_name"),
        "verified": verified,
        "verification_status": "VERIFIED" if verified else "ASSUMED",
        "net_terms": _fact_value(net),
        "net_days": parse_net_days({"net_terms": _fact_value(net)}),
        "available_credit": _fact_value(credit) if credit is not None else None,
        "deposit_requirement": _fact_value(terms_node.get("deposit_requirement")),
        "evidence": (net.get("evidence") if isinstance(net, dict) else None),
    }
