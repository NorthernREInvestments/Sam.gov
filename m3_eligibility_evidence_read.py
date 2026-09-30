"""BUILD 14 — COO / NMR / Set-Aside evidence binding (read layer).

Binds existing compliance/eligibility signals into Offer Readiness with
evidence. Does not create a new eligibility engine, assume eligibility,
change scoring, or reject on missing evidence. UNKNOWN stays UNKNOWN.

Truth rules:
- "appears applicable" ≠ "applicable"
- "supplier manufactures" ≠ "country of origin verified"
- "small business set-aside" ≠ "company eligible"
"""

from __future__ import annotations

import re
from typing import Any

from application_clock import now_utc

BUILD_TAG = "20260918-m3-eligibility-evidence-1"

ST_UNKNOWN = "UNKNOWN"
ST_DETECTED = "DETECTED"
ST_RESEARCH = "RESEARCH_REQUIRED"
ST_VALIDATED = "VALIDATED"
ST_BLOCKED = "BLOCKED"

CAT_COO = "COUNTRY_OF_ORIGIN"
CAT_NMR = "NMR"
CAT_SET_ASIDE = "SET_ASIDE"

_SET_ASIDE_RE = re.compile(
    r"(?P<label>"
    r"total\s+small\s+business(?:\s+set[\s-]?aside)?|"
    r"small\s+business\s+set[\s-]?aside|"
    r"set[\s-]?aside\s*[:#]?\s*[A-Za-z0-9 /()\-]{3,60}|"
    r"8\(a\)|HUBZone|SDVOSB|WOSB|EDWOSB|"
    r"service[\s-]disabled\s+veteran|"
    r"women[\s-]owned\s+small\s+business|"
    r"historically\s+underutilized"
    r")",
    re.I,
)
_NMR_RE = re.compile(
    r"\b(?:NMR|non[\s-]?manufacturer(?:\s+rule)?)\b|"
    r"nonmanufacturer\s+rule|"
    r"must\s+be\s+(?:the\s+)?manufacturer|"
    r"manufacturer\s+(?:status|requirement)\s+required",
    re.I,
)
_NMR_WAIVER_RE = re.compile(
    r"(?:NMR\s+waiver|waiver\s+of\s+(?:the\s+)?(?:NMR|non[\s-]?manufacturer)|"
    r"nonmanufacturer\s+rule\s+waiver|"
    r"SBA\s+waiver.{0,40}(?:NMR|non[\s-]?manufacturer))",
    re.I,
)
_COO_CLAUSE_RE = re.compile(
    r"(?:Buy\s+American(?:\s+Act)?|\bBAA\b|Trade\s+Agreements?\s+Act|\bTAA\b|"
    r"country\s+of\s+origin|domestic\s+(?:end[\s-]*product|content|preference)|"
    r"Berry\s+Amendment)",
    re.I,
)
_ORIGIN_STATED_RE = re.compile(
    r"(?:country\s+of\s+origin|product\s+origin|made\s+in|manufactured\s+in)\s*[:#]?\s*"
    r"([A-Za-z][A-Za-z\s.]{1,40})",
    re.I,
)
_ORIGIN_VERIFIED_RE = re.compile(
    r"(?:origin\s+verified|COO\s+verified|certificate\s+of\s+origin\s+(?:provided|on\s+file)|"
    r"country\s+of\s+origin\s+(?:confirmed|documented))",
    re.I,
)
_SIZE_CERT_RE = re.compile(
    r"(?:SBA\s+certification|SAM\.gov\s+representations\s+representation|"
    r"size\s+standard|NAICS\s*[:#]?\s*\d{6})",
    re.I,
)


def _utc() -> str:
    return now_utc().isoformat()


def _clean(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _evidence(
    *,
    requirement: str,
    source_document: str,
    section_page: Any,
    extracted_text: str,
    product: Any,
    supplier: Any,
    confidence: str,
    reason: str,
    status: str,
) -> dict[str, Any]:
    return {
        "requirement": requirement or "UNKNOWN",
        "source_document": source_document or "UNKNOWN",
        "section_page": section_page if section_page not in (None, "") else "UNKNOWN",
        "extracted_text": (extracted_text or "UNKNOWN")[:240],
        "product": product if product not in (None, "") else "UNKNOWN",
        "supplier": supplier if supplier not in (None, "") else "UNKNOWN",
        "confidence": confidence or "UNKNOWN",
        "timestamp": _utc(),
        "reason": reason or "UNKNOWN",
        "status": status,
    }


def _collect_sources(row: dict[str, Any]) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    for d in row.get("documents") or []:
        if not isinstance(d, dict):
            continue
        text = _clean(d.get("extracted_text") or d.get("text_preview") or d.get("text"))
        if not text:
            continue
        sources.append(
            {
                "source_document": d.get("filename") or d.get("document_name") or "document",
                "section_page": d.get("page") or d.get("page_number") or "UNKNOWN",
                "text": text[:200000],
            }
        )
    for key in (
        "governing_text",
        "solicitation_text",
        "attachment_text",
        "evidence_text_excerpt",
        "recovered_description",
        "description",
        "title",
    ):
        text = _clean(row.get(key))
        if text and len(text) >= 8:
            sources.append({"source_document": key, "section_page": "UNKNOWN", "text": text[:200000]})
    seen: set[str] = set()
    out = []
    for s in sources:
        h = s["text"][:160]
        if h in seen:
            continue
        seen.add(h)
        out.append(s)
    return out


def _product_ref(row: dict[str, Any]) -> dict[str, Any]:
    struct = row.get("dla_product_structure") if isinstance(row.get("dla_product_structure"), dict) else {}
    pi = row.get("product_identity") if isinstance(row.get("product_identity"), dict) else {}
    fields = struct.get("fields") if isinstance(struct.get("fields"), dict) else {}

    def fv(k: str) -> Any:
        f = fields.get(k)
        return f.get("value") if isinstance(f, dict) else f

    return {
        "nsn": fv("nsn") or struct.get("nsn") or pi.get("nsn") or "UNKNOWN",
        "part_number": fv("part_number") or struct.get("part_number") or pi.get("part_number") or "UNKNOWN",
        "title": row.get("title") or "UNKNOWN",
    }


def _blob_and_match_source(sources: list[dict[str, Any]], pattern: re.Pattern[str]) -> tuple[str, dict[str, Any] | None, re.Match[str] | None]:
    blob = "\n".join(s["text"] for s in sources)
    for s in sources:
        m = pattern.search(s["text"])
        if m:
            return blob, s, m
    m = pattern.search(blob) if blob else None
    return blob, (sources[0] if sources and m else None), m


def _company_facts(row: dict[str, Any]) -> dict[str, Any]:
    facts = row.get("company_facts") if isinstance(row.get("company_facts"), dict) else {}
    profile = row.get("company_profile") if isinstance(row.get("company_profile"), dict) else {}
    # Never invent — only explicit keys
    return {**profile, **facts}


def bind_country_of_origin(row: dict[str, Any], sources: list[dict[str, Any]], product: dict[str, Any]) -> dict[str, Any]:
    """Bind COO using existing classify_origin_compliance — no origin inference from supplier location."""
    from product_bid_compliance import (
        ORIGIN_DOC,
        ORIGIN_LIKELY,
        ORIGIN_POTENTIAL_FAIL,
        ORIGIN_UNKNOWN,
        ORIGIN_VERIFIED,
        classify_origin_compliance,
    )

    blob, src, match = _blob_and_match_source(sources, _COO_CLAUSE_RE)
    # Explicit product origin only if documented on row (never from supplier address)
    product_origin = None
    for key in ("product_origin", "country_of_origin", "origin_country"):
        if _clean(row.get(key)):
            product_origin = _clean(row.get(key))
            break
    # Stated origin in governing text for the product (not supplier HQ)
    stated = _ORIGIN_STATED_RE.search(blob) if blob else None
    if stated and not product_origin:
        # Capture as stated claim — not verified unless verification language present
        product_origin = stated.group(1).strip()[:60]

    origin_verified = None
    if row.get("origin_verified") is True or (blob and _ORIGIN_VERIFIED_RE.search(blob)):
        origin_verified = True
    elif row.get("origin_verified") is False:
        origin_verified = False

    # Reuse existing classifier
    classified = classify_origin_compliance(
        blob,
        product_origin=product_origin if origin_verified is True or row.get("origin_verified") is True else (
            product_origin if origin_verified is False else None
        ),
        origin_verified=origin_verified,
    )
    # If we have a stated but unverified origin, pass product_origin only when verified flag set;
    # otherwise keep documentation-required path by calling with origin unverified.
    if product_origin and origin_verified is not True:
        classified = classify_origin_compliance(
            blob,
            product_origin=None,  # do not treat stated claim as verified origin
            origin_verified=None,
        )
        # Attach stated claim separately for research
        classified = dict(classified)
        classified["stated_origin_claim"] = product_origin
        classified["stated_origin_verified"] = False

    if not classified.get("requirement_found") and not match and not row.get("set_aside"):
        # Also check bid_compliance matrix rows
        bc = row.get("bid_compliance") if isinstance(row.get("bid_compliance"), dict) else {}
        matrix = bc.get("compliance_matrix") if isinstance(bc.get("compliance_matrix"), dict) else {}
        has_coo_row = any(
            str(r.get("category") or "").upper()
            in {"COUNTRY_OF_ORIGIN", "BUY_AMERICAN", "TRADE_AGREEMENTS", "DOMESTIC_CONTENT"}
            for r in (matrix.get("rows") or [])
            if isinstance(r, dict)
        )
        if not has_coo_row:
            return {
                "category": CAT_COO,
                "status": ST_UNKNOWN,
                "label": "Country of origin requirement unknown",
                "next_action": "Review solicitation for Buy American / TAA / COO language",
                "blocker_type": None,
                "evidence": _evidence(
                    requirement="COUNTRY_OF_ORIGIN",
                    source_document="UNKNOWN",
                    section_page="UNKNOWN",
                    extracted_text="UNKNOWN",
                    product=product,
                    supplier="UNKNOWN",
                    confidence="UNKNOWN",
                    reason="No COO / BAA / TAA clause detected in available text",
                    status=ST_UNKNOWN,
                ),
                "engine": "product_bid_compliance.classify_origin_compliance",
                "raw": classified,
            }

    state = str(classified.get("state") or ORIGIN_UNKNOWN)
    extracted = match.group(0) if match else (classified.get("note") or "origin clause")
    src_doc = (src or {}).get("source_document") or "governing_text"
    page = (src or {}).get("section_page") or "UNKNOWN"

    clauses = []
    if classified.get("buy_american"):
        clauses.append("Buy American Act")
    if classified.get("trade_agreements_act"):
        clauses.append("Trade Agreements Act")
    if classified.get("domestic_preference"):
        clauses.append("domestic preference")
    req_label = ", ".join(clauses) if clauses else "Country of origin / domestic content"

    if state == ORIGIN_VERIFIED and origin_verified is True and product_origin:
        status = ST_VALIDATED
        reason = f"Origin verified with evidence: {product_origin}"
        action = "None — origin evidence on file"
        btype = "COMPLETE"
        conf = "HIGH"
    elif state == ORIGIN_POTENTIAL_FAIL and origin_verified is False:
        status = ST_BLOCKED
        reason = "Origin verified as noncompliant with documented requirement"
        action = "Resolve country-of-origin noncompliance or find alternate product"
        btype = "HARD_BLOCKER"
        conf = "HIGH"
    elif classified.get("requirement_found") or match:
        status = ST_RESEARCH if state in {ORIGIN_DOC, ORIGIN_LIKELY, ORIGIN_UNKNOWN} else ST_DETECTED
        if status == ST_DETECTED and classified.get("requirement_found"):
            status = ST_RESEARCH
        reason = (
            "COO clause detected; product origin not verified "
            f"(stated claim={classified.get('stated_origin_claim') or 'UNKNOWN'}). "
            "Supplier location is not origin evidence."
        )
        action = "Verify country of origin"
        btype = "SOFT_BLOCKER"
        conf = "MEDIUM"
    else:
        status = ST_UNKNOWN
        reason = "Insufficient COO evidence"
        action = "Review solicitation for origin clauses"
        btype = None
        conf = "UNKNOWN"

    return {
        "category": CAT_COO,
        "status": status,
        "label": req_label,
        "next_action": action,
        "blocker_type": btype,
        "evidence": _evidence(
            requirement=req_label,
            source_document=src_doc,
            section_page=page,
            extracted_text=str(extracted)[:200],
            product=product,
            supplier="UNKNOWN",
            confidence=conf,
            reason=reason,
            status=status,
        ),
        "engine": "product_bid_compliance.classify_origin_compliance",
        "raw": classified,
        "truth": {
            "does_not_infer_origin_from_supplier_location": True,
            "stated_claim_is_not_verification": True,
        },
    }


def bind_nmr(row: dict[str, Any], sources: list[dict[str, Any]], product: dict[str, Any]) -> dict[str, Any]:
    """Bind NMR using deep_deal_compliance.evaluate_nonmanufacturer_rule — no auto-prohibit reseller."""
    from deep_deal_compliance import evaluate_nonmanufacturer_rule
    from deep_deal_constants import NMR_HARD, NMR_NEEDS, NMR_PASS, NMR_POTENTIAL

    blob, src, match = _blob_and_match_source(sources, _NMR_RE)
    waiver_m = _NMR_WAIVER_RE.search(blob) if blob else None
    set_aside = row.get("set_aside") or row.get("type_of_set_aside")
    struct = row.get("dla_product_structure") if isinstance(row.get("dla_product_structure"), dict) else {}
    if not set_aside and struct.get("set_aside_signal"):
        set_aside = "SET_ASIDE_SIGNAL_DETECTED"

    # Explicit evidence flags only — never invent manufacturer_requirement=True from "reseller"
    mfr_req = row.get("manufacturer_requirement")
    if mfr_req is None and match and re.search(r"must\s+be\s+(?:the\s+)?manufacturer", match.group(0), re.I):
        mfr_req = True  # only when text explicitly says so

    domestic = row.get("domestic_manufacturer_required")
    waiver = True if waiver_m or row.get("nmr_waiver") is True else (False if row.get("nmr_waiver") is False else None)
    naics = row.get("naics") or row.get("naics_code")

    nmr = evaluate_nonmanufacturer_rule(
        set_aside=str(set_aside) if set_aside else None,
        manufacturer_requirement=mfr_req,
        domestic_manufacturer_required=domestic,
        waiver_visible=waiver,
        size_standard=row.get("size_standard"),
        evidence={
            "set_aside": set_aside,
            "manufacturer_requirement": mfr_req,
            "waiver_visible": waiver,
            "naics": naics,
        },
    )

    # If no NMR language and no set-aside context → UNKNOWN (do not invent applicability)
    if not match and not set_aside and not struct.get("set_aside_signal"):
        if not blob or not _SET_ASIDE_RE.search(blob):
            return {
                "category": CAT_NMR,
                "status": ST_UNKNOWN,
                "label": "NMR applicability unknown",
                "next_action": "Verify NMR applicability",
                "blocker_type": None,
                "evidence": _evidence(
                    requirement="NMR",
                    source_document="UNKNOWN",
                    section_page="UNKNOWN",
                    extracted_text="UNKNOWN",
                    product=product,
                    supplier="UNKNOWN",
                    confidence="UNKNOWN",
                    reason="No NMR / set-aside context in available evidence",
                    status=ST_UNKNOWN,
                ),
                "engine": "deep_deal_compliance.evaluate_nonmanufacturer_rule",
                "raw": nmr,
                "truth": {
                    "potentially_applicable_is_not_applicable": True,
                    "reseller_not_automatically_prohibited": True,
                    "small_business_not_automatically_sufficient": True,
                },
            }

    raw_status = str(nmr.get("nmr_status") or NMR_NEEDS)
    src_doc = (src or {}).get("source_document") or ("description" if set_aside else "UNKNOWN")
    extracted = match.group(0) if match else (f"set_aside={set_aside}" if set_aside else "NMR context")
    if waiver_m:
        extracted = f"{extracted}; waiver text: {waiver_m.group(0)[:120]}"

    if waiver is True and waiver_m:
        status = ST_VALIDATED
        reason = "NMR waiver documented in source text (waiver visible — not a legal conclusion beyond evidence)"
        action = "Retain waiver evidence in package"
        btype = "COMPLETE"
        conf = "HIGH"
    elif raw_status == NMR_HARD and mfr_req is True and waiver is not True:
        # Hard manufacturer requirement without waiver — RESEARCH unless explicit ineligible evidence
        status = ST_RESEARCH
        reason = (
            f"Manufacturer requirement appears in evidence without visible waiver "
            f"(nmr_status={raw_status}). Reseller is not automatically prohibited — verify applicability."
        )
        action = "Verify NMR applicability"
        btype = "SOFT_BLOCKER"
        conf = "MEDIUM"
    elif raw_status in {NMR_POTENTIAL, NMR_NEEDS} or match or set_aside:
        status = ST_RESEARCH if (match or set_aside) else ST_DETECTED
        if status == ST_DETECTED:
            status = ST_RESEARCH
        reason = (
            f"NMR may appear applicable based on set-aside/NMR language "
            f"(nonmanufacturer_rule_potentially_applicable="
            f"{nmr.get('nonmanufacturer_rule_potentially_applicable')}). "
            "This is not a determination that NMR is applicable."
        )
        action = "Verify NMR applicability" if not waiver_m else "Verify SBA waiver"
        if waiver_m:
            action = "Verify SBA waiver"
        btype = "SOFT_BLOCKER"
        conf = "MEDIUM"
    elif raw_status == NMR_PASS and (match is None):
        status = ST_UNKNOWN
        reason = "No NMR signal detected"
        action = "Verify NMR applicability"
        btype = None
        conf = "LOW"
    else:
        status = ST_UNKNOWN
        reason = "Insufficient NMR evidence"
        action = "Verify NMR applicability"
        btype = None
        conf = "UNKNOWN"

    return {
        "category": CAT_NMR,
        "status": status,
        "label": "Nonmanufacturer Rule",
        "next_action": action,
        "blocker_type": btype,
        "evidence": _evidence(
            requirement="Nonmanufacturer Rule",
            source_document=src_doc,
            section_page=(src or {}).get("section_page") or "UNKNOWN",
            extracted_text=str(extracted)[:200],
            product={**product, "naics": naics or "UNKNOWN"},
            supplier="UNKNOWN",
            confidence=conf,
            reason=reason,
            status=status,
        ),
        "engine": "deep_deal_compliance.evaluate_nonmanufacturer_rule",
        "raw": nmr,
        "waiver_evidence": bool(waiver_m) or waiver is True,
        "truth": {
            "potentially_applicable_is_not_applicable": True,
            "reseller_not_automatically_prohibited": True,
            "small_business_not_automatically_sufficient": True,
        },
    }


def bind_set_aside(row: dict[str, Any], sources: list[dict[str, Any]], product: dict[str, Any]) -> dict[str, Any]:
    """Detect set-aside type — never assume company eligibility."""
    blob, src, match = _blob_and_match_source(sources, _SET_ASIDE_RE)
    struct = row.get("dla_product_structure") if isinstance(row.get("dla_product_structure"), dict) else {}
    set_aside = row.get("set_aside") or row.get("type_of_set_aside") or row.get("typeOfSetAsideDescription")
    if not set_aside and match:
        set_aside = match.group("label").strip()
    if not set_aside and struct.get("set_aside_signal"):
        set_aside = "SET_ASIDE_SIGNAL_DETECTED"

    company = _company_facts(row)
    # Explicit eligibility evidence only
    eligible_flag = company.get("set_aside_eligible")
    size_verified = company.get("size_standard_verified") is True or company.get("small_business") is True
    cert_verified = bool(company.get("socioeconomic_certifications") or company.get("sba_certification_verified"))

    if not set_aside and not match and not struct.get("set_aside_signal"):
        return {
            "category": CAT_SET_ASIDE,
            "status": ST_UNKNOWN,
            "label": "Set-aside requirement unknown",
            "next_action": "Verify size/status requirement",
            "blocker_type": None,
            "evidence": _evidence(
                requirement="SET_ASIDE",
                source_document="UNKNOWN",
                section_page="UNKNOWN",
                extracted_text="UNKNOWN",
                product=product,
                supplier="UNKNOWN",
                confidence="UNKNOWN",
                reason="No set-aside language detected",
                status=ST_UNKNOWN,
            ),
            "engine": "row.set_aside + text detection",
            "raw": {"set_aside": None},
            "truth": {"set_aside_is_not_company_eligibility": True},
        }

    src_doc = (src or {}).get("source_document") or "opportunity.set_aside"
    extracted = match.group(0) if match else str(set_aside)
    # Size/cert language in package
    size_m = _SIZE_CERT_RE.search(blob) if blob else None

    if eligible_flag is True and (size_verified or cert_verified):
        status = ST_VALIDATED
        reason = "Company eligibility evidenced in company_facts (explicit) for this set-aside"
        action = "Retain eligibility evidence"
        btype = "COMPLETE"
        conf = "HIGH"
    elif eligible_flag is False:
        status = ST_BLOCKED
        reason = "Company facts explicitly mark set_aside_eligible=false"
        action = "Confirm ineligibility or find alternate vehicle"
        btype = "HARD_BLOCKER"
        conf = "HIGH"
    else:
        status = ST_RESEARCH
        reason = (
            f"Set-aside detected ({set_aside}). "
            "This does not mean the company is eligible — verify size/status/certification."
        )
        action = "Verify size/status requirement"
        if re.search(r"8\(a\)|HUBZone|SDVOSB|WOSB", str(set_aside), re.I):
            action = "Verify required certification"
        btype = "SOFT_BLOCKER"
        conf = "MEDIUM"

    return {
        "category": CAT_SET_ASIDE,
        "status": status,
        "label": f"Set-aside: {set_aside}",
        "set_aside_type": set_aside,
        "next_action": action,
        "blocker_type": btype,
        "evidence": _evidence(
            requirement=f"Set-aside: {set_aside}",
            source_document=src_doc,
            section_page=(src or {}).get("section_page") or "UNKNOWN",
            extracted_text=str(extracted)[:200],
            product={**product, "size_standard_hint": size_m.group(0) if size_m else "UNKNOWN"},
            supplier="UNKNOWN",
            confidence=conf,
            reason=reason,
            status=status,
        ),
        "engine": "row.set_aside + deep_deal_compliance.set_aside category patterns",
        "raw": {
            "set_aside": set_aside,
            "company_set_aside_eligible": eligible_flag if eligible_flag is not None else "UNKNOWN",
            "size_language_detected": bool(size_m),
        },
        "truth": {"set_aside_is_not_company_eligibility": True},
    }


def build_eligibility_evidence_profile(row: dict[str, Any] | None) -> dict[str, Any]:
    """Unified COO / NMR / Set-Aside evidence profile for Offer Readiness."""
    row = row if isinstance(row, dict) else {}
    sources = _collect_sources(row)
    product = _product_ref(row)

    coo = bind_country_of_origin(row, sources, product)
    nmr = bind_nmr(row, sources, product)
    set_aside = bind_set_aside(row, sources, product)
    bindings = [coo, nmr, set_aside]

    research_actions = []
    action_map = {
        CAT_COO: "Verify country of origin",
        CAT_NMR: "Verify NMR applicability",
        CAT_SET_ASIDE: "Verify size/status requirement",
    }
    for b in bindings:
        if b.get("status") in {ST_RESEARCH, ST_DETECTED}:
            act = b.get("next_action") or action_map.get(str(b.get("category")), "Research eligibility")
            research_actions.append(
                {
                    "action": act,
                    "category": b.get("category"),
                    "priority": 70 if b.get("category") in {CAT_NMR, CAT_SET_ASIDE} else 65,
                }
            )
            # Extra specific actions
            if b.get("category") == CAT_NMR and b.get("waiver_evidence"):
                research_actions.append(
                    {"action": "Verify SBA waiver", "category": CAT_NMR, "priority": 72}
                )
            if b.get("category") == CAT_SET_ASIDE and re.search(
                r"8\(a\)|HUBZone|SDVOSB|WOSB|certif", str(b.get("label") or ""), re.I
            ):
                research_actions.append(
                    {
                        "action": "Verify required certification",
                        "category": CAT_SET_ASIDE,
                        "priority": 72,
                    }
                )

    # Dedupe actions
    seen: set[str] = set()
    uniq = []
    for a in research_actions:
        if a["action"] in seen:
            continue
        seen.add(a["action"])
        uniq.append(a)

    return {
        "kind": "M3EligibilityEvidenceProfile",
        "build": BUILD_TAG,
        "opportunity_id": row.get("canonical_id") or "UNKNOWN",
        "question": "What eligibility requirements apply, and what evidence do we have?",
        "country_of_origin": coo,
        "nmr": nmr,
        "set_aside": set_aside,
        "bindings": bindings,
        "research_actions": uniq,
        "assumes_eligibility": False,
        "rejects_on_missing_evidence": False,
        "unknown_preserved": True,
        "scoring_unchanged": True,
        "engines_unchanged": True,
        "reuses": [
            "product_bid_compliance.classify_origin_compliance",
            "deep_deal_compliance.evaluate_nonmanufacturer_rule",
            "compliance_matrix / bid_compliance rows (when present)",
        ],
        "generated_at": _utc(),
        "read_only": True,
    }


def eligibility_to_offer_requirements(profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Map bindings → Offer Readiness requirement rows."""
    out = []
    for b in profile.get("bindings") or []:
        if not isinstance(b, dict):
            continue
        ev = b.get("evidence") if isinstance(b.get("evidence"), dict) else {}
        evidence_str = (
            f"{ev.get('extracted_text')} · reason={ev.get('reason')} · "
            f"doc={ev.get('source_document')} · page={ev.get('section_page')} · "
            f"conf={ev.get('confidence')}"
        )
        out.append(
            {
                "kind": "M3OfferRequirement",
                "category": b.get("category"),
                "requirement": b.get("label") or b.get("category"),
                "status": b.get("status"),
                "evidence": evidence_str,
                "missing_action": b.get("next_action"),
                "blocker_type": b.get("blocker_type"),
                "mandatory": b.get("status") in {ST_RESEARCH, ST_DETECTED, ST_BLOCKED, ST_VALIDATED}
                and b.get("status") != ST_UNKNOWN,
                "source": "eligibility_evidence",
                "raw_status": b.get("status"),
                "reason": ev.get("reason"),
                "clause_evidence": ev,
            }
        )
    return out


def eligibility_to_research_items(profile: dict[str, Any], *, opportunity_id: str) -> list[dict[str, Any]]:
    items = []
    for a in profile.get("research_actions") or []:
        items.append(
            {
                "opportunity_id": opportunity_id,
                "research_type": "PROCUREMENT_PATH",
                "status": "NEW",
                "priority": int(a.get("priority") or 65),
                "priority_label": "HIGH" if int(a.get("priority") or 0) >= 70 else "MEDIUM",
                "why_this_matters": f"Eligibility evidence: {a.get('category')}",
                "recommended_action": a.get("action"),
                "missing_information": [a.get("category") or "ELIGIBILITY"],
                "category": a.get("category"),
                "source": "eligibility_evidence",
            }
        )
    return items[:12]
