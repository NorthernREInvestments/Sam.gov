"""BUILD 11 — Offer Blocker Read Model (read/experience layer).

Unifies existing compliance, bid readiness, research queue, document, and
execution intelligence into one operator answer:

  Can we submit a compliant offer?
  If not — what exactly is blocking us?

Does not create compliance engines, change scoring, or automate submission.
UNKNOWN is never treated as an automatic hard failure.
"""

from __future__ import annotations

from typing import Any

from application_clock import now_utc

BUILD_TAG = "20260918-m3-offer-readiness-1"

# Operator-facing statuses (mission vocabulary)
ST_UNKNOWN = "UNKNOWN"
ST_DETECTED = "DETECTED"
ST_RESEARCH = "RESEARCH_REQUIRED"
ST_VALIDATED = "VALIDATED"
ST_SATISFIED = "SATISFIED"
ST_BLOCKED = "BLOCKED"

# Overall readiness
READY = "READY"
BLOCKED = "BLOCKED"
RESEARCH_REQUIRED = "RESEARCH_REQUIRED"

HARD = "HARD_BLOCKER"
SOFT = "SOFT_BLOCKER"
COMPLETE = "COMPLETE"

# Mission requirement categories
OFFER_CATEGORIES = (
    "PRODUCT",
    "SOURCE_APPROVAL",
    "NMR",
    "COUNTRY_OF_ORIGIN",
    "SET_ASIDE",
    "PACKAGING",
    "MARKING",
    "INSPECTION",
    "ACCEPTANCE",
    "DELIVERY",
    "FREIGHT",
    "PRICING",
    "FORMS",
    "CERTIFICATIONS",
    "CYBER",
    "PAST_PERFORMANCE",
    "FINANCING",
)

# Map existing bid_compliance / DLA / research categories → offer categories
_CATEGORY_MAP = {
    "PRODUCT": "PRODUCT",
    "QUANTITY": "PRODUCT",
    "TECHNICAL_SPECIFICATION": "PRODUCT",
    "BRAND_OR_EQUAL": "PRODUCT",
    "EXACT_BRAND_REQUIRED": "PRODUCT",
    "OEM_REQUIREMENT": "PRODUCT",
    "AUTHORIZED_RESELLER": "SOURCE_APPROVAL",
    "AUTHORIZED_DISTRIBUTOR": "SOURCE_APPROVAL",
    "MANUFACTURER_AUTHORIZATION": "SOURCE_APPROVAL",
    "SOURCE_APPROVAL": "SOURCE_APPROVAL",
    "NMR": "NMR",
    "COUNTRY_OF_ORIGIN": "COUNTRY_OF_ORIGIN",
    "SET_ASIDE": "SET_ASIDE",
    "SOCIOECONOMIC_REQUIREMENT": "SET_ASIDE",
    "SMALL_BUSINESS": "SET_ASIDE",
    "DOMESTIC_CONTENT": "COUNTRY_OF_ORIGIN",
    "BUY_AMERICAN": "COUNTRY_OF_ORIGIN",
    "TRADE_AGREEMENTS": "COUNTRY_OF_ORIGIN",
    "PACKAGING": "PACKAGING",
    "LABELING": "MARKING",
    "MARKING": "MARKING",
    "SERIAL_NUMBER": "MARKING",
    "TESTING": "INSPECTION",
    "SAMPLE": "INSPECTION",
    "INSPECTION": "INSPECTION",
    "ACCEPTANCE": "ACCEPTANCE",
    "DELIVERY_DATE": "DELIVERY",
    "DELIVERY_LOCATION": "DELIVERY",
    "FREIGHT": "FREIGHT",
    "PRICING_FORM": "PRICING",
    "OPTION": "PRICING",
    "FORMS": "FORMS",
    "REQUIRED_FORMS": "FORMS",
    "REPRESENTATION": "FORMS",
    "CERTIFICATION_FORM": "FORMS",
    "SIGNATURE": "FORMS",
    "NOTARIZATION": "FORMS",
    "ACKNOWLEDGEMENT": "FORMS",
    "AMENDMENT_ACKNOWLEDGEMENT": "FORMS",
    "CERTIFICATION": "CERTIFICATIONS",
    "LICENSE": "CERTIFICATIONS",
    "INSURANCE": "CERTIFICATIONS",
    "BOND": "CERTIFICATIONS",
    "CYBER": "CYBER",
    "CMMC": "CYBER",
    "PAST_PERFORMANCE": "PAST_PERFORMANCE",
    "EXPERIENCE": "PAST_PERFORMANCE",
    "BIDDER_QUALIFICATION": "PAST_PERFORMANCE",
    "FINANCING": "FINANCING",
    "PAYMENT_TERMS": "FINANCING",
}

_ACTION_BY_CATEGORY = {
    "PRODUCT": "Confirm exact product identity",
    "SOURCE_APPROVAL": "Contact supplier / confirm authorization",
    "NMR": "Verify NMR applicability",
    "COUNTRY_OF_ORIGIN": "Verify country of origin",
    "SET_ASIDE": "Verify size/status requirement",
    "PACKAGING": "Review packaging instructions",
    "MARKING": "Review marking requirements",
    "INSPECTION": "Confirm inspection point and quality requirements",
    "ACCEPTANCE": "Confirm acceptance point",
    "DELIVERY": "Confirm delivery date and destination",
    "FREIGHT": "Clarify freight responsibility",
    "PRICING": "Complete pricing / acquisition cost research",
    "FORMS": "Complete required forms",
    "CERTIFICATIONS": "Obtain required certifications / bonds / insurance",
    "CYBER": "Assess cybersecurity / CMMC requirements",
    "PAST_PERFORMANCE": "Assemble past performance evidence",
    "FINANCING": "Confirm financing path",
}

# Soft research categories — UNKNOWN here is soft, not hard
_SOFT_CATEGORIES = frozenset(
    {
        "PACKAGING",
        "MARKING",
        "INSPECTION",
        "ACCEPTANCE",
        "DELIVERY",
        "FREIGHT",
        "PRICING",
        "FINANCING",
        "CYBER",
    }
)

# Hard when mandatory and unsatisfied/blocked (not merely UNKNOWN)
_HARD_CATEGORIES = frozenset(
    {
        "SOURCE_APPROVAL",
        "NMR",
        "FORMS",
        "CERTIFICATIONS",
        "PRODUCT",
        "COUNTRY_OF_ORIGIN",
        "PAST_PERFORMANCE",
    }
)


def _utc() -> str:
    return now_utc().isoformat()


def _clean(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _unknown(v: Any) -> Any:
    if v is None or v == "" or str(v).upper() in {"UNKNOWN", "NONE", "NULL"}:
        return "UNKNOWN"
    return v


def map_offer_category(raw: Any) -> str:
    cat = str(raw or "").upper().strip()
    if cat in OFFER_CATEGORIES:
        return cat
    return _CATEGORY_MAP.get(cat, "PRODUCT" if cat else "PRODUCT")


def map_status(raw: Any, *, detected: bool = False) -> str:
    """Normalize existing matrix/engine statuses → offer vocabulary."""
    s = str(raw or "").upper().strip()
    if s in {ST_UNKNOWN, ST_DETECTED, ST_RESEARCH, ST_VALIDATED, ST_SATISFIED, ST_BLOCKED}:
        return s
    mapping = {
        "SATISFIED": ST_SATISFIED,
        "LIKELY_SATISFIED": ST_VALIDATED,
        "VALIDATED": ST_VALIDATED,
        "AVAILABLE": ST_VALIDATED,
        "EXECUTION_READY": ST_SATISFIED,
        "UNRESOLVED": ST_UNKNOWN,
        "RESEARCH_REQUIRED": ST_RESEARCH,
        "DOCUMENT_REQUIRED": ST_RESEARCH,
        "FUTURE_ACTION_IF_PURSUED": ST_RESEARCH,
        "UNSATISFIED": ST_BLOCKED,
        "BLOCKED": ST_BLOCKED,
        "NONCOMPLIANT": ST_BLOCKED,
        "CONFLICTING": ST_BLOCKED,
        "NOT_APPLICABLE": ST_SATISFIED,
        "SUPERSEDED": ST_SATISFIED,
        "DETECTED": ST_DETECTED,
        "POSSIBLE": ST_DETECTED,
        "NEW": ST_RESEARCH,
        "HIGH": ST_VALIDATED,
        "MEDIUM": ST_DETECTED,
        "LOW": ST_UNKNOWN,
        "EXACT_NSN": ST_VALIDATED,
        "AMBIGUOUS": ST_RESEARCH,
        "COMPLETE": ST_SATISFIED,
        "INCOMPLETE": ST_RESEARCH,
    }
    out = mapping.get(s)
    if out:
        return out
    if detected and s not in {"", "UNKNOWN"}:
        return ST_DETECTED
    return ST_UNKNOWN


def _classify_blocker(
    *,
    category: str,
    status: str,
    mandatory: bool,
    severity: str | None,
    blocking_flag: bool,
) -> str | None:
    """
    Classify blocker type.

    UNKNOWN alone is never a hard failure.
    HARD: cannot responsibly submit (mandatory form missing, approved source missing, etc.)
    SOFT: needs research / confirmation
    COMPLETE: evidence exists (no blocker)
    """
    sev = str(severity or "").upper()
    if status in {ST_SATISFIED, ST_VALIDATED}:
        return COMPLETE
    if status == ST_BLOCKED or blocking_flag:
        return HARD
    if "HARD_BLOCKER" in sev and status not in {ST_UNKNOWN}:
        return HARD
    if status == ST_UNKNOWN:
        # Unknown ≠ automatic failure
        if category in _SOFT_CATEGORIES:
            return SOFT
        if mandatory and category in _HARD_CATEGORIES:
            # Still soft until proven missing — operator must research
            return SOFT
        return SOFT
    if status in {ST_DETECTED, ST_RESEARCH}:
        if mandatory and category in _HARD_CATEGORIES and "HARD_BLOCKER" in sev:
            return HARD
        if mandatory and category in {"FORMS", "SOURCE_APPROVAL"} and status == ST_RESEARCH:
            # Missing mandatory form / auth path that was explicitly flagged for research
            # remains soft until evidence says blocked/unsatisfied — unless severity hard
            return SOFT if "HARD_BLOCKER" not in sev else HARD
        return SOFT
    return SOFT


def _action_for(category: str, status: str, explicit: Any = None) -> str:
    if _clean(explicit):
        return str(explicit)
    if status == ST_SATISFIED:
        return "None — evidence present"
    return _ACTION_BY_CATEGORY.get(category, "Research requirement")


def _requirement_row(
    *,
    category: str,
    requirement: str,
    status: str,
    evidence: Any,
    missing_action: str,
    blocker_type: str | None,
    mandatory: bool = False,
    source: str | None = None,
    raw_status: Any = None,
) -> dict[str, Any]:
    return {
        "kind": "M3OfferRequirement",
        "category": category,
        "requirement": requirement,
        "status": status,
        "evidence": evidence if evidence not in (None, "") else "UNKNOWN",
        "missing_action": missing_action,
        "blocker_type": blocker_type,
        "mandatory": mandatory,
        "source": source or "UNKNOWN",
        "raw_status": raw_status,
    }


def _from_compliance_matrix(bid_compliance: dict[str, Any]) -> list[dict[str, Any]]:
    matrix = bid_compliance.get("compliance_matrix") if isinstance(bid_compliance.get("compliance_matrix"), dict) else {}
    rows_in = list(matrix.get("rows") or bid_compliance.get("matrix_rows") or [])
    # Also accept flattened unresolved lists
    if not rows_in and isinstance(bid_compliance.get("unresolved"), list):
        for u in bid_compliance["unresolved"]:
            if isinstance(u, dict):
                rows_in.append(u)
            else:
                rows_in.append({"category": str(u), "requirement": str(u), "current_status": "UNRESOLVED", "mandatory": True})
    if not rows_in and isinstance(bid_compliance.get("mandatory_unresolved"), list):
        for u in bid_compliance["mandatory_unresolved"]:
            if isinstance(u, dict):
                rows_in.append({**u, "mandatory": True})
            else:
                rows_in.append(
                    {
                        "category": str(u),
                        "requirement": str(u),
                        "current_status": "UNRESOLVED",
                        "mandatory": True,
                        "severity": "HARD_BLOCKER_IF_UNSATISFIED",
                    }
                )

    out: list[dict[str, Any]] = []
    for r in rows_in:
        if not isinstance(r, dict):
            continue
        cat = map_offer_category(r.get("category") or r.get("requirement"))
        raw = r.get("current_status") or r.get("status") or "UNKNOWN"
        status = map_status(raw)
        mandatory = bool(r.get("mandatory"))
        severity = r.get("severity")
        blocking = bool(r.get("blocking"))
        # Explicit unsatisfied mandatory form → hard
        if cat == "FORMS" and status == ST_BLOCKED:
            blocking = True
        btype = _classify_blocker(
            category=cat,
            status=status,
            mandatory=mandatory,
            severity=severity,
            blocking_flag=blocking,
        )
        evidence = r.get("evidence") or r.get("source_snippet") or "UNKNOWN"
        if isinstance(evidence, dict):
            evidence = evidence.get("fact") or evidence.get("capability") or evidence.get("value") or str(evidence)
        out.append(
            _requirement_row(
                category=cat,
                requirement=str(r.get("requirement") or r.get("normalized_requirement") or cat),
                status=status,
                evidence=evidence,
                missing_action=_action_for(cat, status, r.get("operator_action") or r.get("resolution_path")),
                blocker_type=btype,
                mandatory=mandatory,
                source=str(r.get("source") or "compliance_matrix"),
                raw_status=raw,
            )
        )
    return out


def _from_dla_signals(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Surface DLA structure signals; prefer evidence-backed clause + source qualification."""
    out: list[dict[str, Any]] = []

    # BUILD 12 — evidence-backed clauses override bare signals for same categories
    clause_bundle = row.get("dla_clause_extraction") if isinstance(row.get("dla_clause_extraction"), dict) else None
    if clause_bundle is None:
        try:
            from m3_dla_clause_extraction import extract_dla_clauses

            # Only run when document text exists (avoid inventing from empty)
            from m3_dla_clause_extraction import collect_document_texts

            if collect_document_texts(row):
                clause_bundle = extract_dla_clauses(row)
        except Exception:
            clause_bundle = None

    clause_cats: set[str] = set()
    if clause_bundle:
        try:
            from m3_dla_clause_extraction import clauses_to_offer_requirements

            clause_rows = clauses_to_offer_requirements(clause_bundle)
            out.extend(clause_rows)
            clause_cats = {str(r.get("category")) for r in clause_rows}
        except Exception:
            pass

    # BUILD 13 — source qualification lane owns SOURCE_APPROVAL when available
    qual_profile = row.get("source_qualification") if isinstance(row.get("source_qualification"), dict) else None
    if qual_profile is None:
        try:
            from m3_source_qualification_read import build_source_qualification_profile

            qual_profile = build_source_qualification_profile(row)
        except Exception:
            qual_profile = None
    if qual_profile:
        try:
            from m3_source_qualification_read import qualification_to_offer_requirements

            qual_rows = qualification_to_offer_requirements(qual_profile)
            out.extend(qual_rows)
            clause_cats.add("SOURCE_APPROVAL")
        except Exception:
            pass

    # BUILD 14 — COO / NMR / Set-Aside evidence binding
    elig_profile = row.get("eligibility_evidence") if isinstance(row.get("eligibility_evidence"), dict) else None
    if elig_profile is None:
        try:
            from m3_eligibility_evidence_read import build_eligibility_evidence_profile

            elig_profile = build_eligibility_evidence_profile(row)
        except Exception:
            elig_profile = None
    if elig_profile:
        try:
            from m3_eligibility_evidence_read import eligibility_to_offer_requirements

            elig_rows = eligibility_to_offer_requirements(elig_profile)
            out.extend(elig_rows)
            for r in elig_rows:
                if r.get("category"):
                    clause_cats.add(str(r.get("category")))
        except Exception:
            pass

    struct = row.get("dla_product_structure") if isinstance(row.get("dla_product_structure"), dict) else {}

    def add(cat: str, req: str, signal: bool, action: str) -> None:
        if cat in clause_cats:
            return  # evidence-backed clause / qualification already covers this category
        if signal:
            out.append(
                _requirement_row(
                    category=cat,
                    requirement=req,
                    status=ST_DETECTED,
                    evidence="Signal detected in solicitation text",
                    missing_action=action,
                    blocker_type=SOFT,
                    mandatory=False,
                    source="dla_product_structure",
                    raw_status="DETECTED",
                )
            )

    if not struct and not row.get("award_product_projection") and not clause_bundle and not qual_profile and not elig_profile:
        return out

    add(
        "SOURCE_APPROVAL",
        "Approved source / QPL requirement",
        bool(struct.get("approved_source_signal")),
        "Confirm approved source / supplier authorization",
    )
    add(
        "PACKAGING",
        "Packaging / preservation / marking",
        bool(struct.get("packaging_signal")),
        "Review packaging instructions",
    )
    add(
        "INSPECTION",
        "Inspection / acceptance requirements",
        bool(struct.get("inspection_signal") or struct.get("fat_required_signal")),
        "Confirm inspection point and quality requirements",
    )
    return out


def _from_product(row: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    pi = row.get("product_identity") if isinstance(row.get("product_identity"), dict) else {}
    struct = row.get("dla_product_structure") if isinstance(row.get("dla_product_structure"), dict) else {}
    fields = struct.get("fields") if isinstance(struct.get("fields"), dict) else {}

    def field_val(key: str) -> Any:
        f = fields.get(key)
        if isinstance(f, dict):
            return f.get("value")
        return f

    nsn = _unknown(field_val("nsn") or struct.get("nsn") or pi.get("nsn"))
    pn = _unknown(field_val("part_number") or struct.get("part_number") or pi.get("part_number"))
    cage = _unknown(field_val("cage") or struct.get("cage") or pi.get("cage"))
    identity_state = pi.get("identity_state") or ("EXACT_NSN" if struct.get("has_exact_nsn") else "UNKNOWN")
    confidence = _unknown(
        pi.get("confidence")
        or (
            (fields.get("nsn") or {}).get("confidence")
            if isinstance(fields.get("nsn"), dict)
            else None
        )
    )
    status = map_status(identity_state if identity_state != "UNKNOWN" else confidence)
    approved = bool(struct.get("approved_source_signal"))
    approved_status = ST_DETECTED if approved else ST_UNKNOWN

    product = {
        "identity_status": status,
        "identity_state": _unknown(identity_state),
        "nsn": nsn,
        "part_number": pn,
        "cage": cage,
        "approved_source_status": approved_status,
        "product_confidence": confidence,
    }

    reqs: list[dict[str, Any]] = []
    if status in {ST_UNKNOWN, ST_RESEARCH}:
        reqs.append(
            _requirement_row(
                category="PRODUCT",
                requirement="Product identity",
                status=status,
                evidence="UNKNOWN" if status == ST_UNKNOWN else str(identity_state),
                missing_action="Identify exact product (NSN / P/N / CAGE)",
                blocker_type=SOFT,
                mandatory=True,
                source="product_identity",
                raw_status=identity_state,
            )
        )
    elif status in {ST_VALIDATED, ST_SATISFIED, ST_DETECTED}:
        reqs.append(
            _requirement_row(
                category="PRODUCT",
                requirement="Product identity",
                status=status if status != ST_DETECTED else ST_VALIDATED,
                evidence=f"NSN={nsn}; P/N={pn}; CAGE={cage}",
                missing_action="None — evidence present",
                blocker_type=COMPLETE,
                mandatory=True,
                source="product_identity",
                raw_status=identity_state,
            )
        )
    return product, reqs


def _from_research_queue(row: dict[str, Any], research_items: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    items = research_items
    if items is None:
        # Prefer embedded queue; else build lightly from this row only
        try:
            from m3_research_queue import build_research_queue
            from m3_opportunity_identity import OpportunityIdentityResolver

            bundle = build_research_queue(
                rows=[row],
                identity_resolver=OpportunityIdentityResolver(),
                status_index={"by_key": {}},
                limit=20,
            )
            items = list(bundle.get("items") or [])
        except Exception:
            items = []

    type_map = {
        "PRODUCT_IDENTITY": "PRODUCT",
        "SUPPLIER": "SOURCE_APPROVAL",
        "HISTORICAL": "PRICING",
        "PROCUREMENT_PATH": "SOURCE_APPROVAL",
        "FINANCING": "FINANCING",
        "ECONOMICS": "PRICING",
        "DOCUMENT_REVIEW": "FORMS",
    }
    action_map = {
        "SUPPLIER": "Contact supplier / find acquisition path",
        "PRODUCT_IDENTITY": "Identify exact product",
        "HISTORICAL": "Research market pricing",
        "ECONOMICS": "Complete economics inputs",
        "FINANCING": "Confirm financing path",
        "DOCUMENT_REVIEW": "Recover / review package documents",
    }
    out: list[dict[str, Any]] = []
    cid = row.get("canonical_id")
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if cid and item.get("opportunity_id") and item.get("opportunity_id") != cid:
            continue
        if str(item.get("status") or "").upper() == "COMPLETE":
            continue
        rtype = str(item.get("research_type") or "UNKNOWN")
        cat = type_map.get(rtype, map_offer_category(rtype))
        status = map_status(item.get("status") or ST_RESEARCH)
        if status == ST_UNKNOWN:
            status = ST_RESEARCH
        out.append(
            _requirement_row(
                category=cat,
                requirement=str(item.get("why_this_matters") or rtype),
                status=status,
                evidence=", ".join(item.get("missing_information") or []) or "UNKNOWN",
                missing_action=action_map.get(
                    rtype, item.get("recommended_action") or _action_for(cat, status)
                ),
                blocker_type=SOFT,
                mandatory=False,
                source="research_queue",
                raw_status=item.get("status"),
            )
        )
    return out


def _from_economics_financing(row: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    de = row.get("deal_economics") if isinstance(row.get("deal_economics"), dict) else {}
    price_conf = de.get("PRICE_CONFIDENCE") or de.get("Pricing_Confidence")
    if price_conf:
        st = map_status(price_conf)
        out.append(
            _requirement_row(
                category="PRICING",
                requirement="Acquisition / pricing evidence",
                status=st if st != ST_UNKNOWN else ST_RESEARCH,
                evidence=str(price_conf),
                missing_action="Research supplier pricing" if st in {ST_UNKNOWN, ST_RESEARCH} else "None — evidence present",
                blocker_type=COMPLETE if st in {ST_SATISFIED, ST_VALIDATED} else SOFT,
                source="deal_economics",
                raw_status=price_conf,
            )
        )
    else:
        out.append(
            _requirement_row(
                category="PRICING",
                requirement="Acquisition / pricing evidence",
                status=ST_UNKNOWN,
                evidence="UNKNOWN",
                missing_action="Research supplier pricing",
                blocker_type=SOFT,
                source="deal_economics",
                raw_status="UNKNOWN",
            )
        )

    exec_i = row.get("execution_intelligence") if isinstance(row.get("execution_intelligence"), dict) else {}
    fin = exec_i.get("Financing_Fit") or row.get("funding_state")
    if fin:
        st = map_status(fin)
        out.append(
            _requirement_row(
                category="FINANCING",
                requirement="Financing readiness",
                status=st if st != ST_UNKNOWN else ST_UNKNOWN,
                evidence=str(fin),
                missing_action="Confirm financing path" if st in {ST_UNKNOWN, ST_RESEARCH} else "None — evidence present",
                blocker_type=SOFT if st not in {ST_SATISFIED, ST_VALIDATED} else COMPLETE,
                source="execution_intelligence",
                raw_status=fin,
            )
        )
    else:
        out.append(
            _requirement_row(
                category="FINANCING",
                requirement="Financing readiness",
                status=ST_UNKNOWN,
                evidence="UNKNOWN",
                missing_action="Confirm financing path",
                blocker_type=SOFT,
                source="execution_intelligence",
                raw_status="UNKNOWN",
            )
        )
    return out


def _from_submission(row: dict[str, Any], bid_compliance: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    sub = (
        bid_compliance.get("submission_instructions")
        if isinstance(bid_compliance.get("submission_instructions"), dict)
        else {}
    )
    bsi = row.get("bid_submission_intelligence") if isinstance(row.get("bid_submission_intelligence"), dict) else {}
    method = (
        sub.get("method")
        or sub.get("submission_method")
        or bsi.get("submission_method")
        or bsi.get("portal")
        or "UNKNOWN"
    )
    forms = bid_compliance.get("forms") if isinstance(bid_compliance.get("forms"), dict) else {}
    form_list = list(forms.get("forms") or [])
    reqs: list[dict[str, Any]] = []
    for f in form_list:
        if not isinstance(f, dict):
            continue
        fst = str(f.get("form_status") or "UNKNOWN").upper()
        if fst == "FORM_MISSING" or f.get("requirement") == "REQUIRED" and fst != "PRESENT":
            status = ST_BLOCKED if fst == "FORM_MISSING" else ST_RESEARCH
            btype = HARD if fst == "FORM_MISSING" else SOFT
            reqs.append(
                _requirement_row(
                    category="FORMS",
                    requirement=str(f.get("form_name") or f.get("name") or "Required form"),
                    status=status,
                    evidence=fst,
                    missing_action="Complete required form",
                    blocker_type=btype,
                    mandatory=True,
                    source="bid_compliance.forms",
                    raw_status=fst,
                )
            )
    return {"submission_method": _unknown(method)}, reqs


def _dedupe_requirements(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Prefer richer / more severe rows per category+requirement key."""
    severity_rank = {
        HARD: 3,
        SOFT: 2,
        COMPLETE: 1,
        None: 0,
    }
    status_rank = {
        ST_BLOCKED: 5,
        ST_RESEARCH: 4,
        ST_DETECTED: 3,
        ST_UNKNOWN: 2,
        ST_VALIDATED: 1,
        ST_SATISFIED: 0,
    }
    best: dict[str, dict[str, Any]] = {}
    for r in rows:
        key = f"{r.get('category')}|{str(r.get('requirement') or '')[:80]}"
        prev = best.get(key)
        if not prev:
            best[key] = r
            continue
        # Prefer non-scaffold
        if prev.get("scaffold") and not r.get("scaffold"):
            best[key] = r
            continue
        if r.get("scaffold") and not prev.get("scaffold"):
            continue
        score = (severity_rank.get(r.get("blocker_type"), 0), status_rank.get(r.get("status"), 0))
        prev_score = (severity_rank.get(prev.get("blocker_type"), 0), status_rank.get(prev.get("status"), 0))
        if score >= prev_score:
            best[key] = r
    return list(best.values())


def _opportunity_section(row: dict[str, Any], bid_compliance: dict[str, Any], submission: dict[str, Any]) -> dict[str, Any]:
    pkg = bid_compliance.get("package_map") if isinstance(bid_compliance.get("package_map"), dict) else {}
    docs = list(pkg.get("documents") or row.get("documents") or [])
    amendments = []
    for d in docs:
        if not isinstance(d, dict):
            continue
        if str(d.get("document_type") or "").upper() == "AMENDMENT" or d.get("amendment_number"):
            amendments.append(
                {
                    "amendment_number": d.get("amendment_number") or "UNKNOWN",
                    "filename": d.get("filename") or d.get("document_name") or "UNKNOWN",
                }
            )
    return {
        "solicitation": _unknown(
            row.get("solicitation_number") or row.get("notice_id") or row.get("canonical_id")
        ),
        "agency": _unknown(row.get("agency") or row.get("buyer")),
        "buyer": _unknown(row.get("buyer") or row.get("agency")),
        "deadline": _unknown(row.get("deadline") or row.get("response_deadline")),
        "amendments": amendments or [{"amendment_number": "UNKNOWN", "filename": "UNKNOWN"}],
        "submission_method": submission.get("submission_method") or "UNKNOWN",
        "title": _unknown(row.get("title")),
    }


def _overall_state(requirements: list[dict[str, Any]]) -> tuple[str, str]:
    active = [r for r in requirements if not r.get("scaffold")]
    hard = [r for r in active if r.get("blocker_type") == HARD]
    soft = [r for r in active if r.get("blocker_type") == SOFT]
    if hard:
        return BLOCKED, f"{len(hard)} hard blocker(s) prevent a responsible submission."
    if soft:
        return RESEARCH_REQUIRED, f"{len(soft)} item(s) need research or confirmation before submit."
    if active and all(r.get("blocker_type") == COMPLETE for r in active):
        return READY, "No hard or soft blockers — evidence present for tracked requirements."
    if not active:
        return RESEARCH_REQUIRED, "No compliance evidence loaded yet — research required."
    return READY, "Offer readiness clear for tracked requirements."


def build_offer_readiness_profile(
    row: dict[str, Any] | None,
    *,
    research_items: list[dict[str, Any]] | None = None,
    include_category_scaffold: bool = True,
) -> dict[str, Any]:
    """
    Offer Readiness Profile — read-only assembly.

    Question: Can we submit a compliant offer?
    """
    row = row if isinstance(row, dict) else {}
    bid_compliance = row.get("bid_compliance") if isinstance(row.get("bid_compliance"), dict) else {}
    # Nested analysis shape from analyze_bid_compliance stored whole
    if bid_compliance.get("kind") == "BidComplianceAnalysis":
        pass
    elif isinstance(row.get("bid_compliance_analysis"), dict):
        bid_compliance = row["bid_compliance_analysis"]

    product, product_reqs = _from_product(row)
    submission_meta, form_reqs = _from_submission(row, bid_compliance)
    opportunity = _opportunity_section(row, bid_compliance, submission_meta)

    requirements: list[dict[str, Any]] = []
    requirements.extend(product_reqs)
    requirements.extend(_from_compliance_matrix(bid_compliance))
    requirements.extend(form_reqs)
    requirements.extend(_from_dla_signals(row))
    requirements.extend(_from_research_queue(row, research_items))
    requirements.extend(_from_economics_financing(row))

    # Ensure mission categories appear (UNKNOWN visible) when scaffold requested
    if include_category_scaffold:
        present = {r.get("category") for r in requirements}
        for cat in OFFER_CATEGORIES:
            if cat not in present:
                requirements.append(
                    {
                        **_requirement_row(
                            category=cat,
                            requirement=f"{cat.replace('_', ' ').title()} requirement",
                            status=ST_UNKNOWN,
                            evidence="UNKNOWN",
                            missing_action=_ACTION_BY_CATEGORY.get(cat, "Research requirement"),
                            blocker_type=None,
                            mandatory=False,
                            source="category_scaffold",
                            raw_status="UNKNOWN",
                        ),
                        "scaffold": True,
                    }
                )

    requirements = _dedupe_requirements(requirements)

    active = [r for r in requirements if not r.get("scaffold")]
    hard = [r for r in active if r.get("blocker_type") == HARD]
    soft = [r for r in active if r.get("blocker_type") == SOFT]
    complete = [r for r in active if r.get("blocker_type") == COMPLETE]

    # Sort blockers: hard first, then soft; UNKNOWN stays visible in soft list
    def _sort_key(r: dict[str, Any]) -> tuple:
        rank = 0 if r.get("blocker_type") == HARD else 1
        return (rank, 0 if r.get("mandatory") else 1, str(r.get("category") or ""))

    blockers = sorted(hard + soft, key=_sort_key)
    top_blockers = [
        {
            "rank": i + 1,
            "category": b.get("category"),
            "requirement": b.get("requirement"),
            "status": b.get("status"),
            "blocker_type": b.get("blocker_type"),
            "action": b.get("missing_action"),
            "evidence": b.get("evidence"),
        }
        for i, b in enumerate(blockers[:12])
    ]

    state, answer = _overall_state(requirements)
    question = "Can we submit a compliant offer?"
    if state == READY:
        plain = "READY — tracked requirements have evidence."
    elif state == BLOCKED:
        plain = "BLOCKED — resolve hard blockers before submitting."
    else:
        plain = "RESEARCH REQUIRED — gaps remain; UNKNOWN is not automatic failure."

    return {
        "kind": "M3OfferReadinessProfile",
        "build": BUILD_TAG,
        "opportunity_id": row.get("canonical_id") or "UNKNOWN",
        "question": question,
        "answer": answer,
        "readiness_state": state,
        "readiness_label": plain,
        "opportunity": opportunity,
        "product": product,
        "compliance": {
            "requirements": requirements,
            "by_category": {
                cat: [r for r in requirements if r.get("category") == cat] for cat in OFFER_CATEGORIES
            },
            "counts": {
                "total": len(requirements),
                "hard_blockers": len(hard),
                "soft_blockers": len(soft),
                "complete": len(complete),
                "unknown": sum(1 for r in requirements if r.get("status") == ST_UNKNOWN),
            },
        },
        "blockers": {
            "hard": hard,
            "soft": soft,
            "top": top_blockers,
        },
        "submission_automated": False,
        "engines_unchanged": True,
        "scoring_unchanged": True,
        "unknown_is_not_automatic_failure": True,
        "generated_at": _utc(),
        "read_only": True,
    }


def attach_offer_readiness_to_deal_room(
    deal: dict[str, Any],
    *,
    row: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Additive attach — does not redesign existing Deal Room keys."""
    if not isinstance(deal, dict):
        return deal
    try:
        profile = build_offer_readiness_profile(row or {"canonical_id": deal.get("canonical_id")})
    except Exception:
        profile = {
            "kind": "M3OfferReadinessProfile",
            "build": BUILD_TAG,
            "readiness_state": RESEARCH_REQUIRED,
            "readiness_label": "RESEARCH REQUIRED — offer readiness unavailable",
            "blockers": {"hard": [], "soft": [], "top": []},
            "engines_unchanged": True,
            "read_only": True,
        }
    out = dict(deal)
    out["offer_readiness"] = profile
    # BUILD 13 — attach source qualification profile (additive)
    try:
        from m3_source_qualification_read import build_source_qualification_profile

        out["source_qualification"] = build_source_qualification_profile(
            row or {"canonical_id": deal.get("canonical_id")}
        )
    except Exception:
        pass
    # BUILD 14 — attach eligibility evidence (additive)
    try:
        from m3_eligibility_evidence_read import build_eligibility_evidence_profile

        out["eligibility_evidence"] = build_eligibility_evidence_profile(
            row or {"canonical_id": deal.get("canonical_id")}
        )
    except Exception:
        pass
    return out
