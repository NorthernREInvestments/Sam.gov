"""Response type + evaluation method classifiers — evidence-backed, no forced guesses."""

from __future__ import annotations

import re
from typing import Any

from response_engine.constants import (
    BEST_VALUE_TRADEOFF,
    EMAIL_QUOTE,
    FEDERAL_COMMERCIAL_RFQ,
    FEDERAL_DIBBS_WEB_QUOTE,
    FEDERAL_IFB,
    FEDERAL_PIEE_OFFER,
    FEDERAL_RFP,
    FEDERAL_SIMPLIFIED_RFQ,
    LINE_ITEM_AWARD,
    LOW_PRICE,
    LOWEST_RESPONSIVE_RESPONSIBLE,
    LPTA,
    PHYSICAL_BID,
    STATE_LOCAL_IFB,
    STATE_LOCAL_PORTAL_QUOTE,
    STATE_LOCAL_RFP,
    STATE_LOCAL_RFQ,
    UNKNOWN_EVALUATION,
    UNKNOWN_RESPONSE_TYPE,
)


def classify_response_type(
    *,
    text: str,
    jurisdiction: str | None = None,
    discovery_source: str | None = None,
    authoritative_source: str | None = None,
    submission_system: str | None = None,
) -> dict[str, Any]:
    t = (text or "").lower()
    evid: list[dict[str, Any]] = []
    candidates: list[str] = []

    def hit(label: str, pattern: str, loc: str = "body") -> None:
        if re.search(pattern, t, re.I):
            evid.append({"result_hint": label, "pattern": pattern, "source_location": loc, "excerpt": _excerpt(t, pattern)})
            candidates.append(label)

    hit(FEDERAL_DIBBS_WEB_QUOTE, r"\bdibbs\b|\bdla\s+internet\s+bid\b|\bweb\s+quote\b")
    hit(FEDERAL_PIEE_OFFER, r"\bpiee\b|\bprocurement\s+integrated\s+enterprise\b")
    hit(FEDERAL_RFP, r"\brequest\s+for\s+proposal\b|\brfp\b")
    hit(FEDERAL_IFB, r"\binvitation\s+for\s+bid\b|\bifb\b|\bsealed\s+bid\b")
    hit(FEDERAL_COMMERCIAL_RFQ, r"\bcommercial\s+items?\b|\bfar\s+12\b|\bsf\s*1449\b")
    hit(FEDERAL_SIMPLIFIED_RFQ, r"\brequest\s+for\s+quotation\b|\brfq\b|\bsimplified\s+acquisition\b")
    hit(STATE_LOCAL_PORTAL_QUOTE, r"\bbonfire\b|\bopengov\b|\bplanetbids\b|\bionwave\b|\bbidnet\b|\bcommbuys\b")
    hit(EMAIL_QUOTE, r"\bsubmit\s+(?:quotes?|bids?)\s+by\s+e-?mail\b|\be-?mail\s+quotes?\s+to\b")
    hit(PHYSICAL_BID, r"\bdeliver\s+(?:bids?|proposals?)\s+to\b|\bsealed\s+envelope\b|\bhand[\s-]*deliver\b")

    jur = (jurisdiction or "").upper()
    sub = (submission_system or "").upper()
    auth = (authoritative_source or "").upper()

    if "DIBBS" in sub or "DIBBS" in auth:
        candidates.insert(0, FEDERAL_DIBBS_WEB_QUOTE)
        evid.append({"result_hint": FEDERAL_DIBBS_WEB_QUOTE, "source_location": "submission_system", "excerpt": submission_system or authoritative_source})
    if "PIEE" in sub or "PIEE" in auth:
        candidates.insert(0, FEDERAL_PIEE_OFFER)
        evid.append({"result_hint": FEDERAL_PIEE_OFFER, "source_location": "submission_system", "excerpt": submission_system or authoritative_source})

    # Prefer state/local when jurisdiction clearly non-federal and portal hints
    if jur in {"STATE", "LOCAL", "COUNTY", "CITY"} or (jur and "FEDERAL" not in jur and "DLA" not in jur):
        if STATE_LOCAL_PORTAL_QUOTE in candidates:
            result = STATE_LOCAL_PORTAL_QUOTE
        elif re.search(r"\brfp\b", t):
            result = STATE_LOCAL_RFP
            evid.append({"result_hint": result, "source_location": "body", "excerpt": "RFP + state/local jurisdiction"})
        elif re.search(r"\bifb\b|\binvitation\s+for\s+bid\b", t):
            result = STATE_LOCAL_IFB
        else:
            result = STATE_LOCAL_RFQ if re.search(r"\brfq\b|quotation", t) else (
                STATE_LOCAL_PORTAL_QUOTE if any(c.startswith("STATE_") for c in candidates) else UNKNOWN_RESPONSE_TYPE
            )
    else:
        # Federal preference order
        priority = [
            FEDERAL_DIBBS_WEB_QUOTE, FEDERAL_PIEE_OFFER, FEDERAL_RFP, FEDERAL_IFB,
            FEDERAL_COMMERCIAL_RFQ, FEDERAL_SIMPLIFIED_RFQ, EMAIL_QUOTE, PHYSICAL_BID,
        ]
        result = UNKNOWN_RESPONSE_TYPE
        for p in priority:
            if p in candidates:
                result = p
                break
        if result == UNKNOWN_RESPONSE_TYPE and candidates:
            result = candidates[0]

    unique_cands = list(dict.fromkeys(candidates))
    ambiguous = len(set(unique_cands)) > 1 and result == UNKNOWN_RESPONSE_TYPE
    confidence = "HIGH" if evid and result != UNKNOWN_RESPONSE_TYPE and len(set(unique_cands)) <= 2 else (
        "MEDIUM" if result != UNKNOWN_RESPONSE_TYPE else "LOW"
    )
    owner_review = ambiguous or result == UNKNOWN_RESPONSE_TYPE

    return {
        "result": result,
        "confidence": confidence,
        "evidence": evid,
        "alternate_possibilities": [c for c in unique_cands if c != result],
        "owner_review_required": owner_review,
        "clarification_or_owner_review_required": owner_review and len(unique_cands) > 1,
        # Keep sources separate — never collapse
        "discovery_source": discovery_source,
        "authoritative_source": authoritative_source,
        "submission_system": submission_system,
    }


def classify_evaluation_method(*, text: str) -> dict[str, Any]:
    t = (text or "").lower()
    methods: list[str] = []
    evid: list[dict[str, Any]] = []

    checks = [
        (LPTA, r"\blowest\s+price\s+technically\s+acceptable\b|\blpta\b"),
        (BEST_VALUE_TRADEOFF, r"\bbest\s+value\b|\btrade[\s-]*off\b"),
        (LOWEST_RESPONSIVE_RESPONSIBLE, r"\blowest\s+responsive\s+(?:and\s+)?responsible\b"),
        (LOW_PRICE, r"\blow(?:est)?\s+price\b|\bawarded?\s+to\s+the\s+lowest\b"),
        (LINE_ITEM_AWARD, r"\bline[\s-]*item\s+award\b|\bpartial\s+award\b"),
        ("POINT_SCORED", r"\bpoint(?:s)?\s+(?:scored|based)\b|\bevaluation\s+score\b"),
        ("PRICE_PLUS_TECHNICAL", r"\bprice\s+and\s+technical\b|\btechnical\s+and\s+price\b"),
        ("MULTIPLE_AWARD", r"\bmultiple\s+award\b"),
        ("LOT_AWARD", r"\blot\s+award\b"),
        ("AGGREGATE_AWARD", r"\baggregate\s+award\b"),
    ]
    for label, pat in checks:
        if re.search(pat, t, re.I):
            # Never infer LPTA merely because price appears important — LPTA needs explicit language
            if label == LPTA and not re.search(r"\blpta\b|lowest\s+price\s+technically\s+acceptable", t, re.I):
                continue
            methods.append(label)
            evid.append({"method": label, "pattern": pat, "excerpt": _excerpt(t, pat)})

    if not methods:
        methods = [UNKNOWN_EVALUATION]
        confidence = "LOW"
    else:
        confidence = "HIGH" if any(m in {LPTA, BEST_VALUE_TRADEOFF, LOWEST_RESPONSIVE_RESPONSIBLE} for m in methods) else "MEDIUM"

    return {
        "methods": methods,
        "confidence": confidence,
        "evidence": evid,
    }


def detect_federal_sections(text: str) -> list[str]:
    found = []
    for letter in "ABCDEFGHIJKLM":
        if re.search(rf"\bsection\s+{letter}\b|\b{letter}\.\d+", text or "", re.I):
            found.append(letter)
    return found


def detect_product_mode(text: str) -> dict[str, Any]:
    t = text or ""
    evid = []
    if re.search(r"\bno\s+substitut(?:e|ion)s?\b|\bbrand[\s-]*name\s+only\b|\bexact\s+(?:brand|model|part)\b", t, re.I):
        mode = "EXACT_PART_NUMBER" if re.search(r"\bpart\s*(?:number|#|no\.?)\b|\bmodel\b", t, re.I) else "BRAND_NAME_ONLY"
        evid.append({"mode": mode, "excerpt": _excerpt(t.lower(), r"no\s+substitut|brand[\s-]*name\s+only|exact")})
        return {"mode": mode, "confidence": "HIGH", "evidence": evid}
    if re.search(r"\bbrand[\s-]*name\s+or\s+equal\b|\bor\s+equal\b", t, re.I):
        return {"mode": "BRAND_NAME_OR_EQUAL", "confidence": "HIGH", "evidence": [{"excerpt": _excerpt(t.lower(), r"or\s+equal")}]}
    if re.search(r"\balternate(?:s)?\s+allowed\b|\bapproved\s+equal\b", t, re.I):
        return {"mode": "ALTERNATES_ALLOWED", "confidence": "MEDIUM", "evidence": []}
    if re.search(r"\bperformance\s+spec(?:ification)?\b|\bsalient\s+characteristic", t, re.I):
        return {"mode": "PERFORMANCE_SPEC", "confidence": "MEDIUM", "evidence": []}
    return {"mode": "UNKNOWN_PRODUCT_MODE", "confidence": "LOW", "evidence": []}


def _excerpt(text: str, pattern: str, window: int = 80) -> str:
    m = re.search(pattern, text, re.I)
    if not m:
        return ""
    start = max(0, m.start() - 20)
    end = min(len(text), m.end() + window)
    return text[start:end].replace("\n", " ")[:160]
