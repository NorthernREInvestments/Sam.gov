"""Deterministic bid requirement extraction from solicitation package text.

Never fabricates requirements. Severity grounded in source language (SHALL/MUST vs preferred).
"""

from __future__ import annotations

import hashlib
import re
from typing import Any
from uuid import uuid4

from application_clock import now_utc
from bid_compliance_constants import (
    FUTURE_ACTION,
    SEV_CONDITIONAL,
    SEV_HARD_BLOCKER,
    SEV_INFORMATIONAL,
    SEV_MANDATORY,
    SEV_MANDATORY_MATERIAL,
    SEV_PREFERENCE,
    ST_FUTURE,
    ST_UNRESOLVED,
)

# (category, patterns, default_severity, mandatory_hint)
_PATTERNS: list[tuple[str, list[str], str, bool]] = [
    ("QUANTITY", [r"\bquantity[:\s]+([0-9,]+)", r"\bqty[:\s]+([0-9,]+)", r"\b([0-9,]+)\s+(?:each|ea|units?)\b"], SEV_MANDATORY_MATERIAL, True),
    ("BRAND_OR_EQUAL", [r"\bbrand[\s-]*name\s+or\s+equal\b", r"\bor\s+equal\b", r"\bequivalent\s+product\b"], SEV_MANDATORY, True),
    ("EXACT_BRAND_REQUIRED", [r"\bno\s+substitut(?:e|ion)s?\b", r"\bbrand[\s-]*name\s+only\b", r"\bexact\s+(?:brand|model|part)\b"], SEV_HARD_BLOCKER, True),
    ("AUTHORIZED_RESELLER", [r"\bauthorized\s+reseller\b", r"\bmanufacturer[\s-]*authorized\s+reseller\b"], SEV_HARD_BLOCKER, True),
    ("AUTHORIZED_DISTRIBUTOR", [r"\bauthorized\s+distributor\b"], SEV_HARD_BLOCKER, True),
    ("MANUFACTURER_AUTHORIZATION", [r"\bmanufacturer\s+authorization\b", r"\boem\s+letter\b", r"\bletter\s+of\s+authorization\b"], SEV_MANDATORY_MATERIAL, True),
    ("OEM_REQUIREMENT", [r"\boem\s+(?:product|only|required)\b", r"\boriginal\s+equipment\s+manufacturer\b"], SEV_MANDATORY, True),
    ("BUY_AMERICAN", [r"\bbuy\s+american\b", r"\bbaa\b"], SEV_MANDATORY_MATERIAL, True),
    ("TRADE_AGREEMENTS", [r"\btrade\s+agreements?\s+act\b", r"\btaa\b"], SEV_MANDATORY_MATERIAL, True),
    ("COUNTRY_OF_ORIGIN", [r"\bcountry\s+of\s+origin\b", r"\bmade\s+in\s+(?:usa|the\s+united\s+states)\b"], SEV_MANDATORY, True),
    ("DOMESTIC_CONTENT", [r"\bdomestic\s+(?:content|end[\s-]*product|preference)\b"], SEV_MANDATORY_MATERIAL, True),
    ("SAMPLE", [r"\bsample(?:s)?\s+(?:shall|must|required|may\s+be\s+required)\b", r"\bproduct\s+sample\b"], SEV_CONDITIONAL, True),
    ("WARRANTY", [r"\bwarranty\b.{0,40}(?:shall|must|required|year)"], SEV_MANDATORY, True),
    ("DELIVERY_DATE", [r"\bdelivery\s+(?:date|within|required)[:\s]+([^\n.]{3,80})", r"\bshall\s+deliver\b.{0,60}"], SEV_MANDATORY_MATERIAL, True),
    ("DELIVERY_LOCATION", [r"\b(?:f\.?o\.?b\.?|destination|ship\s+to)[:\s]+([^\n.]{3,100})"], SEV_MANDATORY, True),
    ("FREIGHT", [r"\bfreight\b", r"\bshipping\s+(?:terms|included)\b", r"\blift[\s-]*gate\b"], SEV_CONDITIONAL, False),
    ("INSTALLATION", [r"\binstallation\s+(?:required|shall|must)\b"], SEV_MANDATORY, True),
    ("PACKAGING", [r"\bpackag(?:e|ing)\s+(?:shall|must|requirement)"], SEV_MANDATORY, True),
    ("BIDDER_QUALIFICATION", [r"\bbidder\s+(?:shall|must)\s+(?:have|be|possess)\b", r"\bminimum\s+(?:years?|experience|revenue)\b"], SEV_HARD_BLOCKER, True),
    ("PAST_PERFORMANCE", [r"\bpast\s+performance\b", r"\bprior\s+(?:contracts?|experience)\s+required\b"], SEV_MANDATORY, True),
    ("LICENSE", [r"\blicense(?:d|s)?\s+(?:required|shall)\b"], SEV_MANDATORY, True),
    ("INSURANCE", [r"\binsurance\s+(?:required|shall|certificate|minimum)\b"], SEV_MANDATORY, True),
    ("BOND", [r"\b(?:bid|performance|payment)\s+bond\b", r"\bbid\s+security\b"], SEV_MANDATORY_MATERIAL, True),
    ("SAM_REGISTRATION", [r"\bsam\.gov\b", r"\bregistered\s+in\s+sam\b", r"\bunique\s+entity\s+id\b|\buei\b"], SEV_MANDATORY, True),
    ("STATE_VENDOR_REGISTRATION", [r"\bstate\s+vendor\s+registration\b", r"\bregistered\s+vendor\b"], SEV_MANDATORY, True),
    ("PORTAL_REGISTRATION", [r"\b(?:bonfire|bidnet|opengov|ionwave|jaggaer|planetbids|public\s+purchase)\b"], SEV_MANDATORY, True),
    ("SET_ASIDE", [r"\bset[\s-]*aside\b", r"\bsmall\s+business\s+set[\s-]*aside\b"], SEV_MANDATORY_MATERIAL, True),
    ("SMALL_BUSINESS", [r"\bsmall\s+business\b"], SEV_CONDITIONAL, False),
    ("NAICS", [r"\bnaics\s*(?:code)?[:\s]*([0-9]{4,6})"], SEV_MANDATORY, True),
    ("CERTIFICATION_FORM", [r"\bcertification(?:s)?\s+(?:form|required)\b", r"\brepresentations?\s+and\s+certifications?\b"], SEV_MANDATORY, True),
    ("PRICING_FORM", [r"\bpricing\s+(?:sheet|schedule|form)\b", r"\bbid\s+schedule\b"], SEV_MANDATORY, True),
    ("SIGNATURE", [r"\bsign(?:ed|ature)\s+(?:required|page|block)\b", r"\bauthorized\s+signature\b"], SEV_MANDATORY, True),
    ("NOTARIZATION", [r"\bnotar(?:y|ized|ization)\b"], SEV_MANDATORY, True),
    ("AMENDMENT_ACKNOWLEDGEMENT", [r"\backnowledge\s+all\s+amendments\b", r"\bamendment\s+acknowledg(?:e|ement)\b"], SEV_MANDATORY, True),
    ("SUBMISSION_METHOD", [r"\bsubmit(?:ted)?\s+(?:via|through|electronically|by\s+email|online)\b"], SEV_MANDATORY_MATERIAL, True),
    ("SUBMISSION_EMAIL", [r"\bsubmit(?:tal)?\s+(?:to|via)\s+e?-?mail[:\s]+([\w.+-]+@[\w.-]+)"], SEV_MANDATORY, True),
    ("SUBMISSION_PORTAL", [r"\bsubmit\b.{0,40}\b(?:portal|website|online)\b"], SEV_MANDATORY, True),
    ("FILE_FORMAT", [r"\b(?:pdf|xlsx?|docx?)\s+(?:only|format|required)\b", r"\bfile\s+format\b"], SEV_MANDATORY, True),
    ("BID_DEADLINE", [r"\b(?:bid|offer|proposal)\s+(?:due|deadline|closing)[:\s]+([^\n]{5,80})", r"\bresponse\s+due[:\s]+([^\n]{5,80})"], SEV_MANDATORY_MATERIAL, True),
    ("QUESTION_DEADLINE", [r"\bquestions?\s+(?:due|deadline|must\s+be\s+submitted)[:\s]+([^\n]{5,80})"], SEV_MANDATORY, True),
    ("SITE_VISIT", [r"\bsite\s+visit\b", r"\bpre[\s-]*bid\s+(?:conference|meeting)\b"], SEV_CONDITIONAL, True),
    ("PREBID_MEETING", [r"\bpre[\s-]*bid\s+(?:conference|meeting)\b"], SEV_CONDITIONAL, True),
    ("PAYMENT_TERMS", [r"\bpayment\s+terms?\b", r"\bnet\s+30\b"], SEV_INFORMATIONAL, False),
    ("LPTA", [r"\blowest\s+price\s+technically\s+acceptable\b", r"\blpta\b"], SEV_INFORMATIONAL, False),
    ("BEST_VALUE", [r"\bbest\s+value\b"], SEV_INFORMATIONAL, False),
    ("ALL_OR_NONE", [r"\ball[\s-]*or[\s-]*none\b"], SEV_MANDATORY, True),
    ("PARTIAL_AWARD", [r"\bpartial\s+award\b", r"\bline[\s-]*item\s+award\b"], SEV_INFORMATIONAL, False),
    ("EVALUATION_METHOD", [r"\bevaluation\s+(?:criteria|factors|method)\b"], SEV_INFORMATIONAL, False),
    ("AWARD_BASIS", [r"\bawarded?\s+(?:to|based\s+on)\b", r"\blowest\s+responsive\b"], SEV_INFORMATIONAL, False),
]


_MANDATORY_WORDS = re.compile(r"\b(shall|must|required|mandatory)\b", re.I)
_PREFERENCE_WORDS = re.compile(r"\b(prefer(?:red|ence)?|should|encouraged|desired)\b", re.I)
_MAY_WORDS = re.compile(r"\b(may|optional|if\s+applicable)\b", re.I)


def _severity_from_snippet(snippet: str, default: str) -> str:
    if _MANDATORY_WORDS.search(snippet):
        if default == SEV_HARD_BLOCKER:
            return SEV_HARD_BLOCKER
        if default in {SEV_MANDATORY_MATERIAL, SEV_HARD_BLOCKER}:
            return default
        return SEV_MANDATORY
    if _PREFERENCE_WORDS.search(snippet) and not _MANDATORY_WORDS.search(snippet):
        return SEV_PREFERENCE
    if _MAY_WORDS.search(snippet) and not _MANDATORY_WORDS.search(snippet):
        return SEV_CONDITIONAL
    return default


def _req_id(category: str, snippet: str) -> str:
    h = hashlib.sha256(f"{category}|{snippet[:120]}".encode()).hexdigest()[:10]
    return f"REQ-{category[:12]}-{h}"


def bid_requirement(
    *,
    solicitation_id: str,
    category: str,
    normalized: str,
    snippet: str | None = None,
    source_document_id: str | None = None,
    source_document: str | None = None,
    page_or_section: str | None = None,
    severity: str = SEV_MANDATORY,
    mandatory: bool = True,
    confidence: str = "EXTRACTED",
    status: str = ST_UNRESOLVED,
    evidence: list[dict[str, Any]] | None = None,
    dependencies: list[str] | None = None,
    operator_action: str | None = None,
    captured_value: Any = None,
) -> dict[str, Any]:
    return {
        "kind": "BidRequirement",
        "requirement_id": _req_id(category, snippet or normalized),
        "solicitation_id": solicitation_id,
        "category": category,
        "normalized_requirement": normalized,
        "source_snippet": (snippet or "")[:280] if snippet else None,
        "source_document_id": source_document_id,
        "source_document": source_document,
        "page_or_section": page_or_section,
        "provenance": {
            "method": "regex_extraction",
            "document_id": source_document_id,
            "filename": source_document,
        },
        "confidence": confidence,
        "severity": severity,
        "mandatory": bool(mandatory) and severity not in {SEV_INFORMATIONAL, SEV_PREFERENCE},
        "deadline_relevance": category in {"BID_DEADLINE", "QUESTION_DEADLINE", "DELIVERY_DATE"},
        "compliance_consequence": (
            "blocks_bid_assembly" if severity in {SEV_HARD_BLOCKER, SEV_MANDATORY_MATERIAL} else "tracked"
        ),
        "status": status,
        "evidence": evidence or [],
        "dependencies": dependencies or [],
        "operator_action": operator_action,
        "captured_value": captured_value,
        "last_verified_at": now_utc().isoformat(),
        "authority_state": "ACTIVE_REQUIREMENT",
    }


def extract_requirements_from_text(
    text: str,
    *,
    solicitation_id: str,
    source_document_id: str | None = None,
    source_document: str | None = None,
    governing: bool = True,
) -> list[dict[str, Any]]:
    """Extract requirements; empty if no evidence — never invents."""
    if not text or not str(text).strip():
        return []
    t = str(text)
    found: list[dict[str, Any]] = []
    seen: set[str] = set()

    for category, patterns, default_sev, mandatory_hint in _PATTERNS:
        for pat in patterns:
            for m in re.finditer(pat, t, re.I):
                snippet = m.group(0).strip()
                # Expand snippet slightly for severity context
                start = max(0, m.start() - 40)
                end = min(len(t), m.end() + 40)
                context = t[start:end]
                sev = _severity_from_snippet(context, default_sev)
                captured = m.group(1).strip() if m.lastindex and m.group(1) else None
                key = f"{category}|{snippet[:80].lower()}"
                if key in seen:
                    continue
                seen.add(key)
                if not governing and sev == SEV_HARD_BLOCKER:
                    # Reference docs do not create hard blockers alone
                    sev = SEV_INFORMATIONAL
                found.append(
                    bid_requirement(
                        solicitation_id=solicitation_id,
                        category=category,
                        normalized=f"{category}: {captured or snippet[:120]}",
                        snippet=snippet,
                        source_document_id=source_document_id,
                        source_document=source_document,
                        severity=sev,
                        mandatory=mandatory_hint and sev not in {SEV_INFORMATIONAL, SEV_PREFERENCE},
                        captured_value=captured,
                        operator_action=FUTURE_ACTION if category in {
                            "MANUFACTURER_AUTHORIZATION",
                            "SAMPLE",
                            "PORTAL_REGISTRATION",
                            "STATE_VENDOR_REGISTRATION",
                            "SIGNATURE",
                            "NOTARIZATION",
                        } else None,
                        status=ST_FUTURE if category in {
                            "MANUFACTURER_AUTHORIZATION",
                            "PORTAL_REGISTRATION",
                            "SIGNATURE",
                            "NOTARIZATION",
                        } else ST_UNRESOLVED,
                    )
                )
                break  # one hit per pattern family enough for compact register
    return found


def extract_requirements_from_package(
    package_map: dict[str, Any],
    *,
    document_texts: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Extract from governing docs only for mandatory; reference docs informational."""
    texts = document_texts or {}
    all_reqs: list[dict[str, Any]] = []
    sid = package_map.get("solicitation_id") or "unknown"
    for doc in package_map.get("documents") or []:
        did = doc.get("document_id")
        body = texts.get(did) or texts.get(doc.get("filename") or "") or doc.get("text_snippet") or ""
        if not body:
            continue
        governing = bool(doc.get("governing")) and not doc.get("superseded_by")
        all_reqs.extend(
            extract_requirements_from_text(
                body,
                solicitation_id=sid,
                source_document_id=did,
                source_document=doc.get("filename"),
                governing=governing,
            )
        )
    # Dedupe by category keeping highest severity from governing sources
    by_cat: dict[str, dict[str, Any]] = {}
    sev_rank = {
        SEV_HARD_BLOCKER: 5,
        SEV_MANDATORY_MATERIAL: 4,
        SEV_MANDATORY: 3,
        SEV_CONDITIONAL: 2,
        SEV_PREFERENCE: 1,
        SEV_INFORMATIONAL: 0,
    }
    for r in all_reqs:
        cat = r["category"]
        prev = by_cat.get(cat)
        if not prev or sev_rank.get(r["severity"], 0) > sev_rank.get(prev["severity"], 0):
            by_cat[cat] = r
    return list(by_cat.values())


def distinguish_mandatory_vs_informational(requirements: list[dict[str, Any]]) -> dict[str, Any]:
    mandatory = [r for r in requirements if r.get("mandatory")]
    informational = [r for r in requirements if r.get("severity") in {SEV_INFORMATIONAL, SEV_PREFERENCE}]
    return {
        "mandatory_count": len(mandatory),
        "informational_count": len(informational),
        "mandatory": mandatory,
        "informational": informational,
    }
