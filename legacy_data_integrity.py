"""Legacy data integrity: classify / quarantine historical fact-like values.

Read-time quarantine — does NOT mutate production rows.
IF PROVENANCE CANNOT BE ESTABLISHED, THE VALUE IS NOT A VERIFIED FACT.
"""

from __future__ import annotations

from typing import Any

from data_integrity import (
    STATUS_ASSESSMENT,
    STATUS_CALCULATED,
    STATUS_SOURCE_CONFLICT,
    STATUS_UNKNOWN,
    STATUS_UNPROVEN_LEGACY,
    STATUS_VERIFIED,
    assessment_fact,
    calculated_fact,
    make_fact,
    parse_money,
    source_conflict_fact,
    unknown_fact,
    unproven_legacy_fact,
    verified_fact,
    verified_numeric_or_none,
)

# Structured SAM / federal monetary keys that semantically mean contract/award value.
_STRUCTURED_MONEY_PATHS: tuple[tuple[str, ...], ...] = (
    ("award", "amount"),
    ("award", "value"),
    ("awardAmount",),
    ("estimatedValue",),
    ("baseAndAllOptionsValue",),
    ("baseAndAllOptions",),
    ("totalContractValue",),
    ("ceiling",),
)

_MONEY_EPSILON = 0.02  # cents-level float tolerance for exact match


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _get_path(blob: dict[str, Any], path: tuple[str, ...]) -> Any:
    cur: Any = blob
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def amounts_equal(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) <= _MONEY_EPSILON


def extract_structured_sam_money(sam_raw: Any) -> list[dict[str, Any]]:
    """Authoritative structured monetary candidates from retained SAM raw JSON."""
    raw = _as_dict(sam_raw)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in _STRUCTURED_MONEY_PATHS:
        val = _get_path(raw, path)
        amount = parse_money(val, allow_loose=False)
        if amount is None:
            continue
        field = "sam_raw." + ".".join(path)
        if field in seen:
            continue
        seen.add(field)
        out.append({"source_field": field, "amount": amount, "raw": val})
    return out


def classify_estimated_value(
    *,
    orm_estimated_value: Any,
    sam_raw: Any = None,
    analysis: Any = None,
) -> dict[str, Any]:
    """
    Classify contracts.estimated_value for quarantine.

    VERIFIED only when ORM value exactly matches a structured SAM monetary field
    (or ORM is empty and SAM structured amount exists — then VERIFIED from SAM alone).
    """
    analysis = _as_dict(analysis)
    orm_amount = parse_money(orm_estimated_value, allow_loose=False)
    sources = extract_structured_sam_money(sam_raw)

    # Explicit labeled fact in analysis (post-policy writes)
    labeled = analysis.get("estimated_value_fact")
    if isinstance(labeled, dict) and labeled.get("status"):
        st = str(labeled.get("status") or "").upper()
        if st == STATUS_VERIFIED and labeled.get("source_type") and labeled.get("source_field"):
            # Still require non-AI self-cert
            if str(labeled.get("source_type") or "").upper() not in {"AI", "ANALYSIS", "CLAUDE", "OPENAI"}:
                return dict(labeled)
        if st == STATUS_ASSESSMENT:
            return dict(labeled)
        if st in {STATUS_UNPROVEN_LEGACY, STATUS_SOURCE_CONFLICT, STATUS_UNKNOWN}:
            return dict(labeled)

    # AI analysis value (never VERIFIED by itself)
    ai_val = analysis.get("estimated_value")
    ai_amount = None
    if ai_val is not None:
        ai_amount = parse_money(ai_val, allow_loose=False)
        if ai_amount is None:
            ai_amount = parse_money(ai_val, allow_loose=True)

    if orm_amount is not None and sources:
        matches = [s for s in sources if amounts_equal(orm_amount, s["amount"])]
        if matches:
            src = matches[0]
            return verified_fact(
                orm_amount,
                source_type="SAM",
                source_field=src["source_field"],
                confidence="HIGH",
            ) | {
                "amount": orm_amount,
                "source": "SAM",
                "raw_preview": str(orm_estimated_value)[:40],
                "legacy_orm_field": "estimated_value",
            }
        # Conflict with at least one structured source
        src = sources[0]
        conflict = source_conflict_fact(
            orm_amount,
            source_value=src["amount"],
            source_field=src["source_field"],
            source_type="SAM",
            legacy_field="estimated_value",
        )
        conflict["amount"] = orm_amount
        conflict["source"] = "LEGACY_CONFLICT"
        conflict["raw_preview"] = str(orm_estimated_value)[:40]
        conflict["structured_sources"] = sources
        return conflict

    if orm_amount is None and sources:
        src = sources[0]
        return verified_fact(
            src["amount"],
            source_type="SAM",
            source_field=src["source_field"],
            confidence="HIGH",
        ) | {
            "amount": src["amount"],
            "source": "SAM",
            "raw_preview": str(src.get("raw"))[:40],
        }

    if orm_amount is not None:
        # ORM present, no structured source — check AI coincidence (still not VERIFIED)
        if ai_amount is not None and amounts_equal(orm_amount, ai_amount):
            fact = assessment_fact(
                orm_amount,
                source_type="AI",
                source_field="analysis.estimated_value",
                notes="Legacy ORM matches AI analysis — ASSESSMENT, not independently verified",
            )
            fact["amount"] = orm_amount
            fact["source"] = "AI"
            fact["raw_preview"] = str(orm_estimated_value)[:40]
            fact["legacy_orm_field"] = "estimated_value"
            return fact
        # Arbitrary text coincidence is NOT verification (handled by structured-only match)
        fact = unproven_legacy_fact(
            orm_amount,
            source_field="estimated_value",
            notes="Legacy ORM estimated_value without matching structured SAM monetary source",
        )
        fact["amount"] = orm_amount
        fact["source"] = "LEGACY"
        fact["raw_preview"] = str(orm_estimated_value)[:40]
        return fact

    if ai_amount is not None:
        fact = assessment_fact(
            ai_amount,
            source_type="AI",
            source_field="analysis.estimated_value",
        )
        fact["amount"] = ai_amount
        fact["source"] = "ANALYSIS"
        fact["raw_preview"] = str(ai_val)[:40]
        return fact

    return unknown_fact(source_field="estimated_value")


def classify_square_footage(contract: Any) -> dict[str, Any]:
    """Classify ORM square_footage — estimated drawings / AI → not VERIFIED."""
    analysis = _as_dict(getattr(contract, "analysis", None) if not isinstance(contract, dict) else contract.get("analysis"))
    sqft = None
    if isinstance(contract, dict):
        sqft = contract.get("square_footage")
    else:
        sqft = getattr(contract, "square_footage", None)

    drawing = _as_dict(analysis.get("drawing_sqft_extraction"))
    if drawing.get("estimated") and drawing.get("square_footage"):
        return assessment_fact(
            drawing.get("square_footage"),
            source_type="AI_DRAWING",
            source_field="drawing_sqft_extraction",
            notes="Estimated drawing sqft — assessment only",
        )

    if analysis.get("square_footage_estimated"):
        return assessment_fact(
            analysis.get("square_footage_assessment") or sqft,
            source_type="AI_DRAWING",
            source_field="square_footage_assessment",
        )

    pws = _as_dict(analysis.get("pws_extraction"))
    if pws.get("square_footage_fact"):
        return dict(pws["square_footage_fact"])

    if sqft is not None:
        # Could be AI PWS extraction written historically — unproven unless labeled
        if pws.get("square_footage") is not None:
            return assessment_fact(
                sqft,
                source_type="AI_OR_REGEX",
                source_field="pws_extraction.square_footage",
                notes="Legacy PWS/AI sqft on ORM — not independently verified",
            )
        return unproven_legacy_fact(sqft, source_field="square_footage")

    return unknown_fact(source_field="square_footage")


def classify_unit_rate(
    *,
    rate: Any,
    awarded_amount: Any,
    square_footage_fact: dict[str, Any] | None,
    visits_per_year: float | None,
    field: str,
) -> dict[str, Any]:
    """Unit rates are CALCULATED only when inputs are verified and formula reproduces."""
    if rate is None:
        return unknown_fact(source_field=field)
    award = parse_money(awarded_amount, allow_loose=False)
    sqft = verified_numeric_or_none(square_footage_fact) if square_footage_fact else None
    if award is None or sqft is None or sqft <= 0:
        return unproven_legacy_fact(
            float(rate) if rate is not None else None,
            source_field=field,
            notes="Cannot reproduce unit rate from verified inputs",
        )
    try:
        rate_f = float(rate)
    except (TypeError, ValueError):
        return unknown_fact(source_field=field)

    if field.endswith("per_year") or field == "price_per_sqft_per_year":
        expected = award / sqft
        if abs(expected - rate_f) <= 1e-4:
            return calculated_fact(
                rate_f,
                calculation="awarded_amount / square_footage",
                inputs=[
                    verified_fact(award, source_type="CONTRACT", source_field="awarded_amount"),
                    square_footage_fact,
                ],
                source_field=field,
            )
    if visits_per_year and visits_per_year > 0 and "visit" in field:
        expected = award / (sqft * visits_per_year)
        if abs(expected - rate_f) <= 1e-6:
            return calculated_fact(
                rate_f,
                calculation="awarded_amount / (square_footage * visits_per_year)",
                inputs=[
                    verified_fact(award, source_type="CONTRACT", source_field="awarded_amount"),
                    square_footage_fact,
                    make_fact(visits_per_year, status=STATUS_CALCULATED, source_field="visits_per_year"),
                ],
                source_field=field,
            )
    return unproven_legacy_fact(rate_f, source_field=field, notes="Legacy unit rate not reproducible")


def classify_margin(value: Any) -> dict[str, Any]:
    """Contract margin is POLICY/owner config when present — not an external market fact."""
    from data_integrity import STATUS_POLICY, policy_fact

    if value is None or value == "":
        return unknown_fact(source_field="margin_percentage")
    try:
        return policy_fact(float(value), source_field="margin_percentage", notes="Owner/contract policy margin")
    except (TypeError, ValueError):
        return unknown_fact(source_field="margin_percentage")


def classify_bid_count(value: Any) -> dict[str, Any]:
    if value is None or value == "":
        return unknown_fact(source_field="number_of_offers_received")
    try:
        n = int(value)
    except (TypeError, ValueError):
        return unproven_legacy_fact(value, source_field="number_of_offers_received")
    # Only VERIFIED when caller attaches USAspending provenance separately
    return unproven_legacy_fact(n, source_field="number_of_offers_received", notes="Offer count needs authoritative source label")


def classify_freight_or_supplier(value: Any, *, field: str) -> dict[str, Any]:
    if value is None or value == "":
        return unknown_fact(source_field=field)
    return unproven_legacy_fact(value, source_field=field)


def audit_contract_record(contract: Any) -> list[dict[str, Any]]:
    """Non-destructive findings for one contract. Never mutates."""
    findings: list[dict[str, Any]] = []
    cid = getattr(contract, "id", None)
    notice = getattr(contract, "notice_id", None)
    analysis = _as_dict(getattr(contract, "analysis", None))
    sam_raw = getattr(contract, "sam_raw", None)
    pricing = _as_dict(getattr(contract, "pricing_intel", None))

    def _add(field: str, fact: dict[str, Any], *, extra: str | None = None) -> None:
        st = str(fact.get("status") or STATUS_UNKNOWN)
        findings.append(
            {
                "record_id": cid,
                "notice_id": notice,
                "field": field,
                "existing_value": fact.get("value", fact.get("amount")),
                "classification": st,
                "reason": fact.get("notes") or extra or st,
                "provable_source_field": fact.get("source_field") or fact.get("conflict_source_field"),
                "source_type": fact.get("source_type") or fact.get("source"),
            }
        )

    ev = classify_estimated_value(
        orm_estimated_value=getattr(contract, "estimated_value", None),
        sam_raw=sam_raw,
        analysis=analysis,
    )
    if getattr(contract, "estimated_value", None) is not None or ev.get("status") != STATUS_UNKNOWN:
        _add("estimated_value", ev)

    if analysis.get("estimated_value") is not None:
        _add(
            "analysis.estimated_value",
            assessment_fact(analysis.get("estimated_value"), source_type="AI", source_field="analysis.estimated_value"),
            extra="AI-derived analysis field",
        )

    sq = classify_square_footage(contract)
    if getattr(contract, "square_footage", None) is not None or sq.get("status") != STATUS_UNKNOWN:
        _add("square_footage", sq)

    for rate_field in ("price_per_sqft_per_year", "price_per_sqft_per_visit"):
        rate_val = getattr(contract, rate_field, None)
        if rate_val is None:
            continue
        freq = getattr(contract, "cleaning_frequency_per_week", None)
        visits = float(freq) * 52 if freq is not None else None
        _add(
            rate_field,
            classify_unit_rate(
                rate=rate_val,
                awarded_amount=getattr(contract, "awarded_amount", None),
                square_footage_fact=sq if sq.get("status") == STATUS_VERIFIED else None,
                visits_per_year=visits,
                field=rate_field,
            ),
        )

    if getattr(contract, "awarded_amount", None) is not None:
        # Awarded amount on won contracts — unproven unless linked to USAspending id in pricing
        pred = _as_dict(pricing.get("predecessor_award"))
        amt = parse_money(getattr(contract, "awarded_amount", None), allow_loose=False)
        pred_amt = parse_money(pred.get("award_amount") or pred.get("annual_amount"), allow_loose=False)
        if amt is not None and pred_amt is not None and amounts_equal(amt, pred_amt):
            _add(
                "awarded_amount",
                verified_fact(amt, source_type="USASPENDING", source_field="pricing_intel.predecessor_award"),
            )
        else:
            _add("awarded_amount", unproven_legacy_fact(amt, source_field="awarded_amount"))

    if getattr(contract, "margin_percentage", None) is not None:
        _add("margin_percentage", classify_margin(getattr(contract, "margin_percentage", None)))

    if getattr(contract, "selected_sub_quote", None) is not None:
        _add(
            "selected_sub_quote",
            # Sub quotes from user entry are closer to VERIFIED user input — still label carefully
            make_fact(
                float(getattr(contract, "selected_sub_quote")),
                status=STATUS_UNPROVEN_LEGACY,
                source_type="USER_OR_LEGACY",
                source_field="selected_sub_quote",
                confidence="MEDIUM",
                notes="Sub quote present; treat as user-entered unless quote record provenance exists",
            ),
        )

    # Pricing intel annualized amounts
    for key in ("recommended_annual_bid", "estimated_annual_bid"):
        if pricing.get(key) is not None:
            _add(f"pricing_intel.{key}", unproven_legacy_fact(pricing.get(key), source_field=key))

    pred = _as_dict(pricing.get("predecessor_award"))
    if pred.get("annual_amount") is not None and not pred.get("start_date"):
        _add(
            "pricing_intel.predecessor_award.annual_amount",
            unproven_legacy_fact(
                pred.get("annual_amount"),
                source_field="predecessor_award.annual_amount",
                notes="Annualized amount without proven period may be fabricated annualization",
            ),
        )
    if pred.get("number_of_offers_received") is not None:
        _add("pricing_intel.number_of_offers_received", classify_bid_count(pred.get("number_of_offers_received")))

    freq = getattr(contract, "cleaning_frequency_per_week", None)
    if freq is not None:
        pws = _as_dict(analysis.get("pws_extraction"))
        if pws.get("cleaning_frequency_fact"):
            _add("cleaning_frequency_per_week", dict(pws["cleaning_frequency_fact"]))
        else:
            _add(
                "cleaning_frequency_per_week",
                unproven_legacy_fact(float(freq), source_field="cleaning_frequency_per_week"),
            )

    return findings


def summarize_findings(findings: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {
        STATUS_VERIFIED: 0,
        STATUS_ASSESSMENT: 0,
        STATUS_UNPROVEN_LEGACY: 0,
        STATUS_SOURCE_CONFLICT: 0,
        STATUS_UNKNOWN: 0,
        STATUS_CALCULATED: 0,
        "POLICY": 0,
        "other": 0,
    }
    by_field: dict[str, dict[str, int]] = {}
    examples: dict[str, list[dict[str, Any]]] = {
        STATUS_UNPROVEN_LEGACY: [],
        STATUS_SOURCE_CONFLICT: [],
        STATUS_ASSESSMENT: [],
        STATUS_VERIFIED: [],
    }
    for f in findings:
        st = f.get("classification") or "other"
        if st in counts:
            counts[st] += 1
        else:
            counts["other"] += 1
        field = f.get("field") or "?"
        by_field.setdefault(field, {})
        by_field[field][st] = by_field[field].get(st, 0) + 1
        if st in examples and len(examples[st]) < 8:
            examples[st].append(
                {
                    "record_id": f.get("record_id"),
                    "notice_id": f.get("notice_id"),
                    "field": field,
                    "existing_value": f.get("existing_value"),
                    "reason": f.get("reason"),
                    "provable_source_field": f.get("provable_source_field"),
                }
            )
    return {
        "total_findings": len(findings),
        "counts_by_status": counts,
        "by_field": by_field,
        "examples": examples,
    }


def usable_in_actual_profit(fact: dict[str, Any] | None) -> bool:
    """Profit math may use VERIFIED (and CALCULATED-from-verified) only — never unproven/AI."""
    if not fact:
        return False
    st = str(fact.get("status") or "").upper()
    if st == STATUS_VERIFIED:
        return verified_numeric_or_none(fact) is not None
    if st == STATUS_CALCULATED:
        inputs = fact.get("inputs") or []
        if not inputs:
            return False
        return all(
            str(i.get("status") or "").upper() in {STATUS_VERIFIED, STATUS_CALCULATED, "POLICY"}
            for i in inputs
            if isinstance(i, dict)
        )
    return False
