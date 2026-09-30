"""Atomic requirement compiler — deterministic extraction with provenance."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from response_engine.constants import (
    AT_DELIVERY,
    AT_SUBMISSION,
    DEFAULT_MATERIAL_CATEGORIES,
    MATERIAL,
    MAY_BE_CURABLE,
    NOT_CURABLE_AFTER_SUBMISSION,
    NOT_SAFE_TO_ASSUME_CURABLE,
    POST_AWARD,
    POST_AWARD_REQUIREMENT,
    POTENTIALLY_MATERIAL,
    PRE_AWARD,
    PRE_AWARD_RESOLUTION_ALLOWED,
    PRE_QUESTION_DEADLINE,
    UNKNOWN_CURABILITY,
    UNKNOWN_MATERIALITY,
    UNKNOWN_TIMING,
)
from response_engine.models import empty_provenance, new_requirement

# (category, patterns, mandatory, timing_hint, curability_hint)
_RULES: list[tuple[str, list[str], bool, str, str]] = [
    ("QUANTITY", [r"\bquantity[:\s]+([0-9,]+)", r"\bqty\.?[:\s]+([0-9,]+)", r"\bqty\.?\b", r"\b([0-9,]+)\s+(?:each|ea|units?)\b", r"\bquantity\b.{0,40}(?:minimum|min\.?|of)?\s*(?:\(?\d+\)?|one|two|three|four|five|six|seven|eight|nine|ten)\b", r"\bup\s+to\s+(\d+)\s+cameras?\b"], True, AT_SUBMISSION, NOT_CURABLE_AFTER_SUBMISSION),
    ("EXACT_BRAND", [r"\bno\s+substitut(?:e|ion)s?\b", r"\bbrand[\s-]*name\s+only\b", r"\b([A-Za-z0-9\-]+)\s+model\s+([A-Za-z0-9\-]+)\s+only\b", r"\bapproved\s+sources?\b", r"\bonly\s+approved\s+sources?\b", r"\bexact\s+(?:part|oem|item)\b", r"\bno\s+alternates?\s+allowed\b"], True, AT_SUBMISSION, NOT_CURABLE_AFTER_SUBMISSION),
    ("BRAND_OR_EQUAL", [r"\bbrand[\s-]*name\s+or\s+equal\b", r"\bor\s+equal\b"], True, AT_SUBMISSION, NOT_SAFE_TO_ASSUME_CURABLE),
    ("SALIENT_CHARACTERISTIC", [r"\bsalient\s+characteristics?[:\s]+([^\n]+)", r"\bmust\s+meet\s+the\s+following\s+salient"], True, AT_SUBMISSION, NOT_CURABLE_AFTER_SUBMISSION),
    ("COUNTRY_OF_ORIGIN", [r"\bcountry\s+of\s+origin\b", r"\bmade\s+in\s+(?:usa|the\s+united\s+states)\b"], True, AT_SUBMISSION, NOT_SAFE_TO_ASSUME_CURABLE),
    ("BUY_AMERICAN", [r"\bbuy\s+american\b", r"\bbaa\b"], True, AT_SUBMISSION, NOT_SAFE_TO_ASSUME_CURABLE),
    ("TRADE_AGREEMENTS", [r"\btrade\s+agreements?\s+act\b", r"\btaa\b"], True, AT_SUBMISSION, NOT_SAFE_TO_ASSUME_CURABLE),
    ("NMR", [r"\bnonmanufacturer\s+rule\b|\bnmr\b"], True, AT_SUBMISSION, NOT_SAFE_TO_ASSUME_CURABLE),
    ("SECTION_889", [r"\bsection\s+889\b", r"\b889\b.{0,40}telecommunication"], True, AT_SUBMISSION, NOT_SAFE_TO_ASSUME_CURABLE),
    ("WARRANTY", [r"\bwarranty\b.{0,60}(?:shall|must|required|year)"], True, AT_SUBMISSION, NOT_SAFE_TO_ASSUME_CURABLE),
    ("DELIVERY", [r"\bdelivery\s+(?:date|by|within|required|time)[:\s]+([^\n.]{3,80})", r"\bshall\s+deliver\b.{0,80}", r"\bdeliver(?:y|ed)?\s+(?:within|by|to|ARO)\b", r"\bcompletion\s*/\s*delivery\s+time\b", r"\bdeliverables?\b.{0,60}(?:prior\s+to\s+delivery|assembled)"], True, AT_DELIVERY, NOT_SAFE_TO_ASSUME_CURABLE),
    ("FOB", [r"\bf\.?o\.?b\.?\s*(?:destination|origin)?", r"\bfob\s+(?:destination|origin)\b"], True, AT_SUBMISSION, NOT_SAFE_TO_ASSUME_CURABLE),
    ("FREIGHT", [r"\bfreight\b", r"\bshipping\s+(?:terms|included)\b"], False, AT_SUBMISSION, MAY_BE_CURABLE),
    ("PACKAGING", [r"\bpackag(?:e|ing)\s+(?:shall|must|requirement)"], True, AT_DELIVERY, POST_AWARD_REQUIREMENT),
    ("MARKING", [r"\bmarking\s+(?:shall|must|required)\b", r"\bmanufacturer\s+labels?\b"], True, AT_DELIVERY, POST_AWARD_REQUIREMENT),
    ("INSPECTION", [r"\binspection\s+(?:and\s+acceptance|required|shall)\b"], True, AT_DELIVERY, POST_AWARD_REQUIREMENT),
    ("ACCEPTANCE", [r"\bacceptance\s+(?:shall|criteria|required)\b"], True, AT_DELIVERY, POST_AWARD_REQUIREMENT),
    ("SIGNATURE", [r"\bsign(?:ed|ature)\s+(?:required|page|block|sf\s*1449|certification|form)", r"\bauthorized\s+signature\b", r"\bsigned\s+sf\s*1449\b", r"\bmust\s+be\s+signed\b", r"\b(?:fully\s+)?complete\s+and\s+sign\b", r"\bsign\s+Part\s+\d+", r"\b(?:company\s+)?rep(?:resentative)?\s+signature\b", r"\bsignature\s+of\s+(?:authorized|offeror|contractor)\b", r"\b30b?\.\s*signature\b", r"\bofferor\s+to\s+complete\s+blocks?\b.{0,40}\b30\b", r"\bsigned\b.{0,40}\b(?:bid|offer|proposal|quote)\b"], True, AT_SUBMISSION, NOT_CURABLE_AFTER_SUBMISSION),
    # NOTE: use \\b + 'acknowledge' carefully — r"\backknowledge" becomes word-boundary + 'ackknowledge' (typo).
    ("AMENDMENT_ACK", [r"\backnowledg\w*\s+of\s+all\s+amendments\b", r"\backnowledg\w*\s+all\s+amendments\b", r"\bamendment\s+acknowledg\w*\b", r"\backnowledge\s+the\s+amendment\b", r"\backnowledge\s+and\s+(?:certify|resubmit)", r"\bmust\s+acknowledge\b.{0,40}\bamendment", r"\backnowledg\w*\s+the\s+amendment"], True, AT_SUBMISSION, NOT_CURABLE_AFTER_SUBMISSION),
    ("FORM", [r"\bsf\s*1449\b", r"\bsf\s*33\b", r"\bcompleted?\s+pricing\s+schedule\b", r"\bpricing\s+(?:sheet|schedule|form)\b", r"\bbid\s+form\b", r"\boffer\s+form\b", r"\bForm\s+\d+\b"], True, AT_SUBMISSION, NOT_CURABLE_AFTER_SUBMISSION),
    ("TECHNICAL_LITERATURE", [r"\bproduct\s+literature\b", r"\btechnical\s+(?:literature|brochure|cut[\s-]*sheet)\b"], True, AT_SUBMISSION, NOT_SAFE_TO_ASSUME_CURABLE),
    ("ATTACHMENT", [r"\battach(?:ment|ed)\s+(?:required|shall|must)\b", r"\brequired\s+attachments?\b"], True, AT_SUBMISSION, NOT_CURABLE_AFTER_SUBMISSION),
    ("CERTIFICATION", [r"\bcertification(?:s)?\s+(?:form|required|of)\b", r"\brepresentations?\s+and\s+certifications?\b", r"\bmake\s+certifications\b", r"\bcertify\s+and\s+resubmit\b"], True, AT_SUBMISSION, NOT_CURABLE_AFTER_SUBMISSION),
    ("REGISTRATION", [r"\bsam\.gov\b", r"\bregistered\s+in\s+sam\b", r"\bcage\b", r"\buei\b"], True, AT_SUBMISSION, NOT_SAFE_TO_ASSUME_CURABLE),
    ("PAST_PERFORMANCE", [r"\bpast\s+performance\b"], True, AT_SUBMISSION, NOT_SAFE_TO_ASSUME_CURABLE),
    ("SUBMISSION_METHOD", [r"\bsubmit(?:ted)?\s+(?:via|through|electronically|by\s+e-?mail|online|through\s+piee|via\s+dibbs)\b", r"\b(PIEE|DIBBS|ShareFile)\b", r"\be-?mail(?:ed)?\s+(?:to|submission|quotes?)\b", r"\bupload(?:ed)?\s+(?:via|to|through)\b", r"\belectronic\s+(?:submission|proposal|bid)\b", r"\bportal\s+submission\b", r"\bsubmit\s+(?:your|the)\s+(?:bid|quote|proposal|response)\b", r"\bsealed\s+(?:bids?|envelopes?)\b", r"\breceive\s+sealed\s+bids?\b", r"\bbids?\s+shall\s+be\s+received\s+in\s+a\s+sealed\b"], True, AT_SUBMISSION, NOT_CURABLE_AFTER_SUBMISSION),
    ("SUBMISSION_DEADLINE", [r"\b(?:bid|offer|proposal|quote|response)\s+(?:due|deadline|closing)[:\s]+([^\n]{5,100})", r"\bdue\s+(?:date|by)[:\s]+([^\n]{5,100})", r"\bsubmit\b.{0,40}\bby\s+(\d{1,2}:\d{2}\s*[AP]M\s*[A-Z]{2,3}\b[^\n]{0,40})", r"\bby\s+\d{1,2}:\d{2}\s*[AP]M\s*[A-Z]{2,3}\s+on\s+[A-Za-z]+\s+\d{1,2},\s*\d{4}", r"\bClose\s+(\d{1,2}/\d{1,2}/\d{2,4}(?:,\s*\d{1,2}:\d{2}\s*[AP]M\s*[A-Z]{2,4})?)", r"\bSealed\s+Until\s+(\d{1,2}/\d{1,2}/\d{2,4}(?:,\s*\d{1,2}:\d{2}\s*[AP]M\s*[A-Z]{2,4})?)", r"\bopening\s+date[:\s]+([^\n]{5,80})", r"\bbid\s+opening\b"], True, AT_SUBMISSION, NOT_CURABLE_AFTER_SUBMISSION),
    ("QUESTION_DEADLINE", [r"\bquestions?\s+(?:due|deadline|must\s+be\s+submitted)[:\s]+([^\n]{5,100})", r"\bwritten\s+questions?\b.{0,80}\b(?:due|no\s+later\s+than|by)\b", r"\blast\s+day\s+to\s+submit\s+written\s+questions\b", r"\bdeadline\s+for\s+questions\b"], True, PRE_QUESTION_DEADLINE, NOT_CURABLE_AFTER_SUBMISSION),
    ("PAGE_LIMIT", [r"\b(\d+)\s*[\-\s]?page\s+(?:limit|maximum|max)\b", r"\bmaximum\s+of\s+(\d+)\s+pages?\b", r"\btechnical\s+volume\s+maximum\b"], True, AT_SUBMISSION, NOT_CURABLE_AFTER_SUBMISSION),
    ("FONT", [r"\bfont\s*(?:size)?[:\s]+(\d+)\s*pt\b", r"\b(?:times\s+new\s+roman|arial|calibri)\b.{0,20}\d+\s*pt"], True, AT_SUBMISSION, NOT_CURABLE_AFTER_SUBMISSION),
    ("MARGIN", [r"\bmargins?\s+of\s+(\d+(?:\.\d+)?)\s*(?:inch|in)\b", r"\b\d+(?:\.\d+)?\s*(?:inch|in)\s+margins?\b"], True, AT_SUBMISSION, NOT_CURABLE_AFTER_SUBMISSION),
    ("FILE_FORMAT", [r"\b(?:pdf|xlsx?|docx?)\s+(?:only|format|required)\b", r"\bfile\s+format\b"], True, AT_SUBMISSION, NOT_CURABLE_AFTER_SUBMISSION),
    ("FILE_NAME", [r"\bfile\s*name[:\s]+([^\n]{3,80})", r"\bfilename\s+must\b"], True, AT_SUBMISSION, NOT_CURABLE_AFTER_SUBMISSION),
    ("BID_GUARANTEE", [r"\b(?:bid|performance|payment)\s+bond\b", r"\bbid\s+security\b", r"\bbid\s+guarantee\b"], True, AT_SUBMISSION, NOT_CURABLE_AFTER_SUBMISSION),
    ("PHYSICAL_SAMPLE", [r"\bphysical\s+sample\b", r"\bsample(?:s)?\s+(?:shall|must|required)\b"], True, AT_SUBMISSION, NOT_SAFE_TO_ASSUME_CURABLE),
    ("AUTHORIZATION", [r"\bauthorized\s+(?:reseller|distributor|dealer)\b", r"\bmanufacturer\s+authorization\b"], True, AT_SUBMISSION, NOT_SAFE_TO_ASSUME_CURABLE),
    ("CONDITION", [r"\bnew\s+(?:oem|only|condition)\b", r"\bremanufactured\b", r"\bsurplus\b"], True, AT_SUBMISSION, NOT_CURABLE_AFTER_SUBMISSION),
    ("PRICE", [r"\bunit\s+price\b", r"\bextended\s+price\b", r"\btotal\s+price\b"], True, AT_SUBMISSION, NOT_CURABLE_AFTER_SUBMISSION),
]


def _stable_id(category: str, excerpt: str, doc_id: str | None) -> str:
    h = hashlib.sha256(f"{category}|{excerpt[:160]}|{doc_id or ''}".encode()).hexdigest()[:12]
    return f"REQ-{h}"


def _materiality(category: str, mandatory: bool) -> str:
    if category in DEFAULT_MATERIAL_CATEGORIES and mandatory:
        return MATERIAL
    if mandatory:
        return POTENTIALLY_MATERIAL
    return UNKNOWN_MATERIALITY


def compile_requirements_from_text(
    *,
    response_project_id: str,
    text: str,
    source_document_id: str | None,
    amendment_version: str | None = None,
    existing_keys: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Deterministic atomic extraction. Never invents requirements without a text match."""
    existing_keys = existing_keys or set()
    out: list[dict[str, Any]] = []
    t = text or ""
    if not t.strip():
        return out

    for category, patterns, mandatory, timing, curability in _RULES:
        for pat in patterns:
            for m in re.finditer(pat, t, re.I | re.M):
                start = max(0, m.start() - 40)
                end = min(len(t), m.end() + 120)
                excerpt = re.sub(r"\s+", " ", t[start:end]).strip()
                key = f"{category}|{excerpt[:120]}|{source_document_id}"
                if key in existing_keys:
                    continue
                existing_keys.add(key)
                # Page/section heuristics
                page = None
                section = None
                page_m = re.search(r"(?:page|p\.?)\s*(\d+)", excerpt, re.I)
                if page_m:
                    page = page_m.group(1)
                sec_m = re.search(r"(section\s+[A-M]\.?[\d.]*)", excerpt, re.I)
                if sec_m:
                    section = sec_m.group(1)

                # Pre-award language softens curability / timing
                local_timing = timing
                local_cur = curability
                ctx = t[max(0, m.start() - 200) : m.end() + 200]
                if re.search(r"\bbefore\s+award\b|\bprior\s+to\s+award\b|\bpre[\s-]*award\b", ctx, re.I):
                    local_timing = PRE_AWARD
                    local_cur = PRE_AWARD_RESOLUTION_ALLOWED
                if re.search(r"\bafter\s+award\b|\bpost[\s-]*award\b|\bat\s+delivery\b", ctx, re.I) and category in {
                    "PACKAGING", "MARKING", "INSPECTION", "ACCEPTANCE", "DELIVERY",
                }:
                    local_timing = AT_DELIVERY if "delivery" in ctx.lower() else POST_AWARD
                    local_cur = POST_AWARD_REQUIREMENT

                rid = _stable_id(category, excerpt, source_document_id)
                req = new_requirement(
                    response_project_id=response_project_id,
                    requirement_text=excerpt,
                    requirement_category=category,
                    source_document_id=source_document_id,
                    mandatory=mandatory,
                    materiality=_materiality(category, mandatory),
                    timing=local_timing or UNKNOWN_TIMING,
                    curability=local_cur or UNKNOWN_CURABILITY,
                    provenance=empty_provenance(
                        document_id=source_document_id,
                        source_page=page,
                        source_section=section,
                        source_anchor=section or (f"page {page}" if page else None),
                        excerpt=excerpt,
                        extractor="r1_deterministic_regex",
                        confidence="HIGH" if mandatory else "MEDIUM",
                    ),
                    amendment_version=amendment_version,
                    confidence="HIGH" if mandatory else "MEDIUM",
                    applies_to_product=category in {
                        "EXACT_BRAND", "BRAND_OR_EQUAL", "SALIENT_CHARACTERISTIC",
                        "COUNTRY_OF_ORIGIN", "CONDITION", "TECHNICAL_SPECIFICATION",
                    },
                    applies_to_company=category in {"REGISTRATION", "CERTIFICATION", "NMR", "SECTION_889", "SIGNATURE"},
                )
                req["requirement_id"] = rid  # stable for idempotence
                out.append(req)

    # Expand brand-or-equal salient characteristics into individual requirements
    out.extend(_expand_salient_characteristics(response_project_id, t, source_document_id, existing_keys, amendment_version))
    return out


def _expand_salient_characteristics(
    response_project_id: str,
    text: str,
    source_document_id: str | None,
    existing_keys: set[str],
    amendment_version: str | None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    # Match "Salient characteristics: A, B, and C" or bullet lists after the phrase
    m = re.search(
        r"salient\s+characteristics?\s*(?:are|:)\s*([^\n]+(?:\n[\-\*•].+)*)",
        text,
        re.I,
    )
    if not m:
        return out
    block = m.group(1)
    parts = re.split(r"[;\n]|,\s*(?:and\s+)?|\band\b|[\-\*•]", block)
    for part in parts:
        name = part.strip(" .:;\t")
        if len(name) < 2 or len(name) > 80:
            continue
        if name.lower() in {"the following", "as follows", "include", "including"}:
            continue
        key = f"SALIENT_CHARACTERISTIC|{name.lower()}|{source_document_id}"
        if key in existing_keys:
            continue
        existing_keys.add(key)
        excerpt = f"Salient characteristic: {name}"
        req = new_requirement(
            response_project_id=response_project_id,
            requirement_text=excerpt,
            normalized_requirement=f"salient:{name.lower()}",
            requirement_category="SALIENT_CHARACTERISTIC",
            source_document_id=source_document_id,
            mandatory=True,
            materiality=MATERIAL,
            timing=AT_SUBMISSION,
            curability=NOT_CURABLE_AFTER_SUBMISSION,
            provenance=empty_provenance(
                document_id=source_document_id,
                excerpt=excerpt,
                extractor="r1_salient_expander",
                confidence="HIGH",
            ),
            applies_to_product=True,
            evidence_required=True,
            amendment_version=amendment_version,
            confidence="HIGH",
        )
        req["requirement_id"] = _stable_id("SALIENT_CHARACTERISTIC", name.lower(), source_document_id)
        out.append(req)
    return out


def merge_requirements(project: dict[str, Any], new_reqs: list[dict[str, Any]]) -> int:
    """Idempotent merge by requirement_id. Returns count added.

    Rejects requirements lacking valid provenance (no silent canonicalize).
    """
    existing = {r["requirement_id"]: r for r in project.get("requirements") or []}
    added = 0
    rejected = project.setdefault("rejected_requirements_no_provenance", [])
    for req in new_reqs:
        rid = req["requirement_id"]
        if rid in existing:
            continue
        prov = req.get("provenance") or {}
        src = req.get("source_document_id") or prov.get("document_id")
        excerpt = prov.get("excerpt") or req.get("requirement_text")
        if not src or not excerpt:
            rejected.append(
                {
                    "requirement_id": rid,
                    "reason": "MISSING_PROVENANCE",
                    "category": req.get("requirement_category"),
                }
            )
            continue
        # normalize provenance link
        req["source_document_id"] = src
        prov["document_id"] = src
        req["provenance"] = prov
        existing[rid] = req
        added += 1
    project["requirements"] = list(existing.values())
    return added
