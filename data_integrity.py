"""System-wide data integrity / no-fabrication policy.

IF IT IS NOT A VERIFIED FACT, IT DOES NOT EXIST AS A FACT.

Statuses:
  VERIFIED   — directly supported by an identifiable source
  CALCULATED — math from verified (or labeled) inputs; retains formula + inputs
  ASSESSMENT — AI/rule inference; never interchangeable with VERIFIED
  ESTIMATE   — explicitly allowed estimate; labeled; never VERIFIED
  UNKNOWN    — missing evidence; value is normally null

POLICY / CONFIGURATION values (budgets, profit floors, token ceilings, owner
default margins) are business rules — not external facts. Use status=POLICY
only for those.
"""

from __future__ import annotations
from application_clock import now_utc

import re
from datetime import datetime, timezone
from typing import Any

# --- Status vocabulary -------------------------------------------------------

STATUS_VERIFIED = "VERIFIED"
STATUS_CALCULATED = "CALCULATED"
STATUS_ASSESSMENT = "ASSESSMENT"
STATUS_ESTIMATE = "ESTIMATE"
STATUS_UNKNOWN = "UNKNOWN"
STATUS_POLICY = "POLICY"  # owner/business configuration — not an external fact
STATUS_UNPROVEN_LEGACY = "UNPROVEN_LEGACY"  # historical value; not proven as fact
STATUS_SOURCE_CONFLICT = "SOURCE_CONFLICT"  # ORM vs authoritative source disagree

FACT_STATUSES = frozenset(
    {
        STATUS_VERIFIED,
        STATUS_CALCULATED,
        STATUS_ASSESSMENT,
        STATUS_ESTIMATE,
        STATUS_UNKNOWN,
        STATUS_POLICY,
        STATUS_UNPROVEN_LEGACY,
        STATUS_SOURCE_CONFLICT,
    }
)

# Confidence for hard gates: only HIGH + VERIFIED may drive factual REJECT.
CONFIDENCE_HIGH = "HIGH"
CONFIDENCE_MEDIUM = "MEDIUM"
CONFIDENCE_LOW = "LOW"

# Shared AI system preamble — prepend / inject into every factual AI task.
AI_NO_FABRICATION_GUARDRAIL = """DATA INTEGRITY — NON-NEGOTIABLE:
Never fabricate missing facts or numeric values.
If the supplied evidence does not establish a value, return null/UNKNOWN.
Do not infer a factual price, quantity, contract value, bid count, cost,
deadline, certification, license, bond, financing term, historical value,
supplier term, or other factual field.
Clearly distinguish assessments from sourced facts.
Assessments (e.g. "installation likely") must remain labeled as assessments —
never as verified facts.
Unknown is acceptable. A fabricated value is never acceptable."""


_MONEY_STRICT_RE = re.compile(
    r"^\s*(?:USD\s*)?\$?\s*([0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.([0-9]+))?\s*(million|billion|M|B|k|K)?\s*$",
    re.I,
)
_MONEY_LOOSE_RE = re.compile(
    r"(?:USD\s*)?\$\s*([0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.([0-9]+))?(?:\s*(million|billion|M|B|k|K))?",
    re.I,
)
_UNKNOWN_TOKENS = frozenset({"unknown", "n/a", "na", "none", "tbd", "null", "", "—"})


def utc_now_iso() -> str:
    return now_utc().isoformat()


def make_fact(
    value: Any = None,
    *,
    status: str = STATUS_UNKNOWN,
    source_type: str | None = None,
    source_reference: str | None = None,
    source_field: str | None = None,
    retrieved_at: str | None = None,
    confidence: str | None = None,
    calculation: str | None = None,
    inputs: list[Any] | None = None,
    method: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Canonical provenance envelope."""
    st = str(status or STATUS_UNKNOWN).upper()
    if st not in FACT_STATUSES:
        st = STATUS_UNKNOWN
    if st == STATUS_UNKNOWN and value is not None and notes is None:
        # Prefer null value for UNKNOWN; keep value only if explicitly provided
        # with notes explaining the anomaly.
        pass
    fact: dict[str, Any] = {
        "value": value,
        "status": st,
        "source_type": source_type,
        "source_reference": source_reference,
        "source_field": source_field,
        "retrieved_at": retrieved_at or utc_now_iso(),
        "confidence": confidence,
        "calculation": calculation,
        "inputs": list(inputs or []),
    }
    if method is not None:
        fact["method"] = method
    if notes is not None:
        fact["notes"] = notes
    # UNKNOWN has null value by default; UNPROVEN_LEGACY / CONFLICT keep the legacy value for audit.
    if st == STATUS_UNKNOWN and value is None:
        fact["value"] = None
    elif st == STATUS_UNKNOWN and value is not None and notes is None:
        fact["value"] = None
    return fact


def unknown_fact(
    *,
    source_field: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    return make_fact(
        None,
        status=STATUS_UNKNOWN,
        source_field=source_field,
        confidence=None,
        notes=notes,
    )


def verified_fact(
    value: Any,
    *,
    source_type: str,
    source_field: str,
    source_reference: str | None = None,
    confidence: str = CONFIDENCE_HIGH,
) -> dict[str, Any]:
    return make_fact(
        value,
        status=STATUS_VERIFIED,
        source_type=source_type,
        source_field=source_field,
        source_reference=source_reference,
        confidence=confidence,
    )


def calculated_fact(
    value: Any,
    *,
    calculation: str,
    inputs: list[Any],
    confidence: str = CONFIDENCE_HIGH,
    source_field: str | None = None,
) -> dict[str, Any]:
    return make_fact(
        value,
        status=STATUS_CALCULATED,
        source_type="CALCULATION",
        source_field=source_field,
        confidence=confidence,
        calculation=calculation,
        inputs=inputs,
    )


def assessment_fact(
    value: Any,
    *,
    source_type: str = "AI",
    source_field: str | None = None,
    confidence: str = CONFIDENCE_MEDIUM,
    notes: str | None = None,
) -> dict[str, Any]:
    return make_fact(
        value,
        status=STATUS_ASSESSMENT,
        source_type=source_type,
        source_field=source_field,
        confidence=confidence,
        notes=notes,
    )


def unproven_legacy_fact(
    value: Any,
    *,
    source_field: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Preserve legacy scalar for audit; never treat as VERIFIED."""
    return make_fact(
        value,
        status=STATUS_UNPROVEN_LEGACY,
        source_type="LEGACY",
        source_field=source_field,
        confidence=CONFIDENCE_LOW,
        notes=notes or "Legacy value without provable authoritative source",
    )


def source_conflict_fact(
    legacy_value: Any,
    *,
    source_value: Any,
    source_field: str,
    source_type: str = "SAM",
    legacy_field: str = "estimated_value",
) -> dict[str, Any]:
    return make_fact(
        legacy_value,
        status=STATUS_SOURCE_CONFLICT,
        source_type=source_type,
        source_field=source_field,
        confidence=CONFIDENCE_LOW,
        notes="Legacy ORM value disagrees with authoritative structured source",
        method="legacy_vs_source",
    ) | {
        "legacy_value": legacy_value,
        "source_value": source_value,
        "legacy_field": legacy_field,
        "conflict_source_field": source_field,
    }


def is_unproven_legacy(fact: Any) -> bool:
    return isinstance(fact, dict) and str(fact.get("status") or "").upper() == STATUS_UNPROVEN_LEGACY


def is_source_conflict(fact: Any) -> bool:
    return isinstance(fact, dict) and str(fact.get("status") or "").upper() == STATUS_SOURCE_CONFLICT


def verified_numeric_or_none(fact: Any) -> float | None:
    """Return a number only when the fact is VERIFIED — never for unproven/AI/conflict."""
    if not is_verified(fact):
        return None
    raw = fact.get("value", fact.get("amount"))
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def require_provenance_for_verified_write(
    *,
    field: str,
    value: Any,
    provenance: dict[str, Any] | None,
) -> dict[str, Any]:
    """
    Future write protection: refuse to treat a write as VERIFIED without provenance.
    Returns a safe fact envelope; raises ValueError if caller insists on VERIFIED without proof.
    """
    if value is None or value == "":
        return unknown_fact(source_field=field)
    if not isinstance(provenance, dict) or not provenance.get("status"):
        return unproven_legacy_fact(value, source_field=field, notes="write blocked: missing provenance")
    st = str(provenance.get("status") or "").upper()
    if st == STATUS_VERIFIED:
        if not provenance.get("source_type") or not provenance.get("source_field"):
            raise ValueError(
                f"Cannot write VERIFIED {field} without source_type and source_field provenance"
            )
        if str(provenance.get("source_type") or "").upper() in {"AI", "ANALYSIS", "CLAUDE", "OPENAI"}:
            raise ValueError(f"AI cannot self-certify VERIFIED write for {field}")
    return make_fact(
        value,
        status=st if st in FACT_STATUSES else STATUS_UNPROVEN_LEGACY,
        source_type=provenance.get("source_type"),
        source_field=provenance.get("source_field") or field,
        source_reference=provenance.get("source_reference"),
        confidence=provenance.get("confidence"),
        calculation=provenance.get("calculation"),
        inputs=provenance.get("inputs") or [],
        notes=provenance.get("notes"),
    )


def policy_fact(
    value: Any,
    *,
    source_field: str,
    notes: str | None = None,
) -> dict[str, Any]:
    """Owner/business configuration — not an external market fact."""
    return make_fact(
        value,
        status=STATUS_POLICY,
        source_type="CONFIGURATION",
        source_field=source_field,
        confidence=CONFIDENCE_HIGH,
        notes=notes,
    )


def is_unknown(fact: Any) -> bool:
    if fact is None:
        return True
    if isinstance(fact, dict):
        st = str(fact.get("status") or "").upper()
        if st == STATUS_UNKNOWN:
            return True
        return fact.get("value") is None and st not in {
            STATUS_VERIFIED,
            STATUS_CALCULATED,
            STATUS_POLICY,
            STATUS_ESTIMATE,
        }
    return False


def is_verified(fact: Any) -> bool:
    return isinstance(fact, dict) and str(fact.get("status") or "").upper() == STATUS_VERIFIED


def is_assessment(fact: Any) -> bool:
    return isinstance(fact, dict) and str(fact.get("status") or "").upper() == STATUS_ASSESSMENT


def can_hard_reject_on_fact(fact: Any) -> bool:
    """
    Hard factual rejection is allowed ONLY for VERIFIED facts with HIGH confidence.
    UNKNOWN / ASSESSMENT / ESTIMATE / CALCULATED-without-verified-inputs → False.
    """
    if not isinstance(fact, dict):
        return False
    if str(fact.get("status") or "").upper() != STATUS_VERIFIED:
        return False
    if str(fact.get("confidence") or "").upper() != CONFIDENCE_HIGH:
        return False
    return fact.get("value") is not None or fact.get("amount") is not None


def fact_value(fact: Any, default: Any = None) -> Any:
    """Extract scalar value; never invent a fallback number for UNKNOWN."""
    if fact is None:
        return default
    if isinstance(fact, dict):
        if str(fact.get("status") or "").upper() == STATUS_UNKNOWN:
            return default
        return fact.get("value", default)
    return fact


def display_fact(fact: Any, *, unknown_label: str = "Unknown") -> str:
    """UI/export string: Unknown vs verified zero/false preserved."""
    if fact is None:
        return unknown_label
    if isinstance(fact, dict):
        st = str(fact.get("status") or "").upper()
        if st == STATUS_UNKNOWN or (fact.get("value") is None and st not in {
            STATUS_UNPROVEN_LEGACY,
            STATUS_SOURCE_CONFLICT,
            STATUS_VERIFIED,
            STATUS_CALCULATED,
            STATUS_POLICY,
            STATUS_ESTIMATE,
            STATUS_ASSESSMENT,
        }):
            return unknown_label
        val = fact.get("value")
        if st == STATUS_UNPROVEN_LEGACY:
            return f"{val} (unverified legacy)" if val is not None else "Needs verification"
        if st == STATUS_SOURCE_CONFLICT:
            return "Needs verification"
        if st == STATUS_ASSESSMENT:
            return f"{val} (assessment)" if val is not None else "Not verified"
        if st == STATUS_ESTIMATE:
            return f"{val} (estimate)" if val is not None else "Needs research"
        if st == STATUS_CALCULATED:
            return f"{val} (calculated)" if val is not None else unknown_label
        if val is None:
            return unknown_label
        return str(val)
    return str(fact)


def serialize_fact_for_api(fact: Any) -> dict[str, Any] | None:
    """Preserve UNKNOWN vs verified false/zero in API payloads."""
    if fact is None:
        return unknown_fact()
    if not isinstance(fact, dict):
        return assessment_fact(fact, source_type="LEGACY", notes="unlabeled scalar")
    out = dict(fact)
    out.setdefault("status", STATUS_UNKNOWN)
    if str(out.get("status")).upper() == STATUS_UNKNOWN:
        out["value"] = None
    return out


def coerce_legacy_to_fact(
    value: Any,
    *,
    default_status: str = STATUS_UNKNOWN,
    source_field: str | None = None,
    source_type: str | None = None,
) -> dict[str, Any]:
    """Wrap a bare scalar; null/empty → UNKNOWN (never 0)."""
    if value is None:
        return unknown_fact(source_field=source_field)
    if isinstance(value, dict) and "status" in value:
        return serialize_fact_for_api(value) or unknown_fact(source_field=source_field)
    if isinstance(value, str) and value.strip().lower() in _UNKNOWN_TOKENS:
        return unknown_fact(source_field=source_field)
    st = default_status if default_status in FACT_STATUSES else STATUS_UNKNOWN
    if st == STATUS_UNKNOWN:
        return unknown_fact(source_field=source_field)
    return make_fact(
        value,
        status=st,
        source_type=source_type,
        source_field=source_field,
        confidence=CONFIDENCE_MEDIUM if st == STATUS_ASSESSMENT else CONFIDENCE_HIGH,
    )


def _coerce_money_number(num: float, suffix: str | None) -> float | None:
    if num <= 0:
        return None
    suf = (suffix or "").lower()
    if suf in {"million", "m"}:
        num *= 1_000_000
    elif suf in {"billion", "b"}:
        num *= 1_000_000_000
    elif suf == "k":
        num *= 1_000
    return num


def parse_money(value: Any, *, allow_loose: bool = False) -> float | None:
    """
    Parse a monetary amount with semantic safety.
    Strict: whole-string / numeric types only — never truncate 27081 → 270.
    Loose: only match explicit $ amounts (ZIP/building/NAICS never become money).
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if float(value) > 0 else None
    try:
        from decimal import Decimal

        if isinstance(value, Decimal):
            f = float(value)
            return f if f > 0 else None
    except Exception:
        pass

    if isinstance(value, dict):
        if "value" in value and value.get("status"):
            if str(value.get("status")).upper() == STATUS_UNKNOWN:
                return None
            return parse_money(value.get("value"), allow_loose=False)
        for k in ("amount", "value", "total", "ceiling"):
            if value.get(k) is not None:
                parsed = parse_money(value.get(k), allow_loose=False)
                if parsed is not None:
                    return parsed
        return None

    text = str(value).strip()
    if not text or text.lower() in _UNKNOWN_TOKENS:
        return None

    m = _MONEY_STRICT_RE.match(text)
    if m:
        whole = m.group(1).replace(",", "")
        frac = m.group(2)
        num = float(f"{whole}.{frac}" if frac else whole)
        return _coerce_money_number(num, m.group(3))

    if not allow_loose:
        return None

    m2 = _MONEY_LOOSE_RE.search(text)
    if not m2:
        return None
    whole = m2.group(1).replace(",", "")
    frac = m2.group(2)
    num = float(f"{whole}.{frac}" if frac else whole)
    return _coerce_money_number(num, m2.group(3))


def parse_quantity(value: Any) -> int | None:
    """Parse an explicit quantity; bare building/NAICS-style numbers are NOT quantities."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        q = int(value)
        return q if q > 0 else None
    text = str(value).strip().lower()
    if text in _UNKNOWN_TOKENS:
        return None
    # Require quantity context words OR a plain integer string with qty hint nearby
    qty_ctx = re.search(
        r"(?:qty|quantity|units?|each|ea\.?|count)\s*[:=]?\s*(\d{1,6})\b",
        text,
        re.I,
    )
    if qty_ctx:
        return int(qty_ctx.group(1))
    # Whole-string small integer only (explicit field, not free text)
    if re.fullmatch(r"\d{1,6}", text):
        return int(text)
    return None


def parse_square_footage(value: Any) -> int | None:
    """Parse sqft only with unit context or whole-string integer field."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        n = int(value)
        return n if n >= 100 else None
    text = str(value).strip()
    if text.lower() in _UNKNOWN_TOKENS:
        return None
    if re.fullmatch(r"[\d,]+", text):
        n = int(text.replace(",", ""))
        return n if n >= 100 else None
    m = re.search(
        r"([\d,]{3,})\s*(?:sq\.?\s*ft|sf|square\s+feet)\b",
        text,
        re.I,
    )
    if m:
        n = int(m.group(1).replace(",", ""))
        return n if n >= 100 else None
    return None


def with_ai_guardrail(system_prompt: str | None) -> str:
    """Prepend the no-fabrication guardrail to a system prompt."""
    base = (system_prompt or "").strip()
    if AI_NO_FABRICATION_GUARDRAIL in base:
        return base
    if not base:
        return AI_NO_FABRICATION_GUARDRAIL
    return f"{AI_NO_FABRICATION_GUARDRAIL}\n\n{base}"
