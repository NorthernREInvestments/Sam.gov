"""Supplier execution-state ladder — market visibility ≠ validated supplier path."""

from __future__ import annotations

from typing import Any

NO_SUPPLIER_SIGNAL = "NO_SUPPLIER_SIGNAL"
COMMERCIAL_SOURCE_FOUND = "COMMERCIAL_SOURCE_FOUND"
SUPPLIER_CANDIDATE = "SUPPLIER_CANDIDATE"
SUPPLIER_CONTACTABLE = "SUPPLIER_CONTACTABLE"
QUOTE_REQUESTED = "QUOTE_REQUESTED"
QUOTE_RECEIVED = "QUOTE_RECEIVED"
SUPPLIER_VALIDATED = "SUPPLIER_VALIDATED"
QUOTE_EXECUTABLE = "QUOTE_EXECUTABLE"

SUPPLIER_LADDER: tuple[str, ...] = (
    NO_SUPPLIER_SIGNAL,
    COMMERCIAL_SOURCE_FOUND,
    SUPPLIER_CANDIDATE,
    SUPPLIER_CONTACTABLE,
    QUOTE_REQUESTED,
    QUOTE_RECEIVED,
    SUPPLIER_VALIDATED,
    QUOTE_EXECUTABLE,
)

_RANK = {s: i for i, s in enumerate(SUPPLIER_LADDER)}


def _known_cost(row: dict[str, Any]) -> bool:
    for key in ("acquisition_cost", "Observed_acquisition", "supplier_unit_cost"):
        v = row.get(key)
        if v not in (None, "", "UNKNOWN"):
            try:
                float(str(v).replace(",", "").replace("$", ""))
                return True
            except (TypeError, ValueError):
                continue
    sq = row.get("supplier_quote")
    if isinstance(sq, dict) and sq.get("quoted_total") not in (None, "", "UNKNOWN"):
        return True
    return False


def _has_formal_quote(row: dict[str, Any]) -> bool:
    if row.get("formal_quote") is True:
        return True
    sq = row.get("supplier_quote")
    if isinstance(sq, dict) and sq.get("quoted_total") not in (None, "", "UNKNOWN"):
        src = str(sq.get("notes") or sq.get("source") or "").upper()
        if "PUBLIC_SCRAPE" in src or src == "PUBLIC":
            return False
        return True
    quotes = row.get("supplier_quotes") or []
    if isinstance(quotes, list):
        for q in quotes:
            if isinstance(q, dict) and q.get("price") not in (None, "", "UNKNOWN"):
                if "PUBLIC" in str(q.get("source") or "").upper() and "SUPPLIER" not in str(q.get("source") or "").upper():
                    continue
                return True
    return False


def _has_public_market_price(row: dict[str, Any]) -> bool:
    if row.get("current_public_price") not in (None, "", "UNKNOWN"):
        return True
    if row.get("public_price_total") not in (None, "", "UNKNOWN"):
        return True
    si = row.get("supplier_intelligence") if isinstance(row.get("supplier_intelligence"), dict) else {}
    pe = si.get("Pricing_Evidence") or si.get("Pricing_evidence") or {}
    if isinstance(pe, dict):
        for it in pe.get("items") or []:
            if isinstance(it, dict) and "PUBLIC" in str(it.get("Source") or it.get("level") or "").upper():
                return True
    return False


def _has_candidate(row: dict[str, Any]) -> bool:
    if row.get("preferred_supplier") or row.get("supplier"):
        return True
    return bool(row.get("supplier_candidates"))


def resolve_supplier_execution_state(row: dict[str, Any] | None) -> dict[str, Any]:
    """
    Resolve supplier ladder.

    Definitions:
    - COMMERCIAL_SOURCE_FOUND: retail/public listing only — not validation
    - SUPPLIER_CANDIDATE: plausible entity identified
    - QUOTE_RECEIVED: a supplier quote exists (not public scrape)
    - SUPPLIER_VALIDATED: explicit validation flag / confirmed path
    - QUOTE_EXECUTABLE: validated + formal quote + known acquisition cost
    """
    row = row if isinstance(row, dict) else {}
    public = _has_public_market_price(row)
    formal = _has_formal_quote(row)
    cost = _known_cost(row)
    candidate = _has_candidate(row)

    if row.get("supplier_unknown") is True and row.get("supplier_validated") is not True:
        state = NO_SUPPLIER_SIGNAL
    elif row.get("quote_executable") is True and row.get("supplier_validated") is True:
        state = QUOTE_EXECUTABLE
    elif row.get("supplier_validated") is True and formal and cost:
        state = QUOTE_EXECUTABLE if row.get("quote_executable") is not False else SUPPLIER_VALIDATED
    elif row.get("supplier_validated") is True:
        state = SUPPLIER_VALIDATED
    elif formal:
        state = QUOTE_RECEIVED
    elif row.get("quote_requested") is True:
        state = QUOTE_REQUESTED
    elif row.get("supplier_contactable") is True and candidate:
        state = SUPPLIER_CONTACTABLE
    elif candidate:
        state = SUPPLIER_CANDIDATE
    elif public:
        state = COMMERCIAL_SOURCE_FOUND
    else:
        state = NO_SUPPLIER_SIGNAL

    # Hard rule: public price alone never validates or executes
    if public and not formal and row.get("supplier_validated") is not True:
        if _RANK[state] >= _RANK[QUOTE_RECEIVED]:
            state = SUPPLIER_CANDIDATE if candidate else COMMERCIAL_SOURCE_FOUND

    supplier_validated: bool | None
    if row.get("supplier_validated") is True:
        supplier_validated = True
    elif row.get("supplier_validated") is False or row.get("supplier_unknown") is True:
        supplier_validated = False
    elif state in {SUPPLIER_VALIDATED, QUOTE_EXECUTABLE}:
        supplier_validated = True
    elif state in {NO_SUPPLIER_SIGNAL, COMMERCIAL_SOURCE_FOUND}:
        supplier_validated = False
    else:
        supplier_validated = False  # candidate/contactable/quote_received without explicit validation

    quote_executable: bool | None
    if row.get("quote_executable") is True and supplier_validated is True and (formal or cost):
        quote_executable = True
    elif row.get("quote_executable") is False:
        quote_executable = False
    elif state == QUOTE_EXECUTABLE and supplier_validated is True:
        quote_executable = True
    elif public and not formal:
        quote_executable = False
    elif not formal and not cost:
        quote_executable = None  # UNKNOWN
    else:
        quote_executable = False

    return {
        "kind": "SupplierExecutionState",
        "state": state,
        "supplier_validated": supplier_validated,
        "quote_executable": quote_executable,
        "has_public_market_price": public,
        "has_formal_quote": formal,
        "has_known_acquisition_cost": cost,
        "note": "Public/market price ≠ supplier validation or executable quote.",
    }
