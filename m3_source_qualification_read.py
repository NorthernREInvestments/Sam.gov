"""BUILD 13 — QPL / Source Approval Research Lane (read/evidence layer).

Answers: Can we legally and practically source this item for this contract?

Does not assume qualification, does not mark suppliers approved without
evidence, does not change scoring, does not replace supplier intelligence /
supplier-product graph.
"""

from __future__ import annotations

import re
from typing import Any

from application_clock import now_utc

BUILD_TAG = "20260918-m3-source-qualification-1"

# Qualification requirement types
APPROVED_SOURCE_REQUIRED = "APPROVED_SOURCE_REQUIRED"
QPL_REQUIRED = "QPL_REQUIRED"
QML_REQUIRED = "QML_REQUIRED"
SOURCE_CONTROLLED = "SOURCE_CONTROLLED"
OEM_ONLY = "OEM_ONLY"
TRACEABILITY_REQUIRED = "TRACEABILITY_REQUIRED"
QUAL_UNKNOWN = "UNKNOWN"

QUALIFICATION_TYPES = (
    APPROVED_SOURCE_REQUIRED,
    QPL_REQUIRED,
    QML_REQUIRED,
    SOURCE_CONTROLLED,
    OEM_ONLY,
    TRACEABILITY_REQUIRED,
    QUAL_UNKNOWN,
)

# Status vocabulary
ST_UNKNOWN = "UNKNOWN"
ST_DETECTED = "DETECTED"
ST_RESEARCH = "RESEARCH_REQUIRED"
ST_VALIDATED = "VALIDATED"
ST_BLOCKED = "BLOCKED"

# Evidence patterns — only DETECT when text matches (never invent)
_TYPE_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    (
        QPL_REQUIRED,
        re.compile(r"\bQPL\b|qualified\s+products?\s+list", re.I),
        "QPL / Qualified Products List referenced",
    ),
    (
        QML_REQUIRED,
        re.compile(r"\bQML\b|qualified\s+manufacturers?\s+list", re.I),
        "QML / Qualified Manufacturers List referenced",
    ),
    (
        APPROVED_SOURCE_REQUIRED,
        re.compile(
            r"approved\s+source(?:s)?\s+required|source\s+approval\s+required|"
            r"only\s+approved\s+sources?|qualified\s+source(?:s)?\s+required",
            re.I,
        ),
        "Approved source required",
    ),
    (
        SOURCE_CONTROLLED,
        re.compile(
            r"source\s+controlled|controlled\s+source|exact\s+part\s+only|"
            r"brand[\s-]name[\s-]only|no\s+substitut(?:e|ion)s?",
            re.I,
        ),
        "Source-controlled / no substitution",
    ),
    (
        OEM_ONLY,
        re.compile(
            r"\bOEM\s+only\b|manufacturer[\s-]only|must\s+be\s+(?:the\s+)?OEM|"
            r"purchase\s+from\s+(?:the\s+)?manufacturer",
            re.I,
        ),
        "OEM-only supply required",
    ),
    (
        TRACEABILITY_REQUIRED,
        re.compile(
            r"traceability\s+required|full\s+traceability|certificate\s+of\s+traceability|"
            r"supply\s+chain\s+traceability",
            re.I,
        ),
        "Traceability required",
    ),
]

# Explicit approved-source listing near a supplier/CAGE — VALIDATED evidence only
_LISTED_SOURCE_RE = re.compile(
    r"(?:approved\s+source|qualified\s+source|QPL\s+(?:listed|entry)|"
    r"listed\s+(?:approved\s+)?source)\s*[:#]?\s*"
    r"([A-Z0-9][A-Z0-9 &.,\-']{2,80})",
    re.I,
)
_CAGE_LISTED_RE = re.compile(
    r"(?:approved\s+source|qualified\s+source|QPL).{0,40}CAGE(?:\s*CODE)?\s*[:#]?\s*([A-Z0-9]{5})",
    re.I,
)


def _utc() -> str:
    return now_utc().isoformat()


def _clean(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _norm_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def _evidence(
    *,
    source_document: str,
    requirement_text: str,
    product: Any,
    supplier: Any,
    evidence: str,
    confidence: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out = {
        "source_document": source_document or "UNKNOWN",
        "requirement_text": (requirement_text or "UNKNOWN")[:240],
        "product": product if product not in (None, "") else "UNKNOWN",
        "supplier": supplier if supplier not in (None, "") else "UNKNOWN",
        "evidence": (evidence or "UNKNOWN")[:240],
        "confidence": confidence or "UNKNOWN",
        "timestamp": _utc(),
    }
    if extra:
        out.update(extra)
    return out


def _collect_text_sources(row: dict[str, Any]) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    for d in row.get("documents") or []:
        if not isinstance(d, dict):
            continue
        text = _clean(d.get("extracted_text") or d.get("text_preview") or d.get("text"))
        if not text:
            continue
        sources.append(
            {
                "source_document": d.get("filename")
                or d.get("document_name")
                or d.get("document_id")
                or "document",
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
            sources.append({"source_document": key, "text": text[:200000]})
    # Deduplicate
    seen: set[str] = set()
    unique = []
    for s in sources:
        h = s["text"][:180]
        if h in seen:
            continue
        seen.add(h)
        unique.append(s)
    return unique


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
        "cage": fv("cage") or struct.get("cage") or pi.get("cage") or "UNKNOWN",
        "title": row.get("title") or "UNKNOWN",
    }


def detect_qualification_requirements(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Detect qualification types from signals + evidenced text. No inference."""
    requirements: list[dict[str, Any]] = []
    struct = row.get("dla_product_structure") if isinstance(row.get("dla_product_structure"), dict) else {}
    product = _product_ref(row)
    sources = _collect_text_sources(row)
    blob = "\n".join(s["text"] for s in sources)
    primary_doc = sources[0]["source_document"] if sources else "UNKNOWN"

    # Legacy boolean signal → DETECTED (not VALIDATED)
    if struct.get("approved_source_signal") and not any(
        t[0] == APPROVED_SOURCE_REQUIRED for t in _TYPE_PATTERNS if t[1].search(blob or "")
    ):
        # Still emit DETECTED from signal even without specific phrase if signal set
        pass

    found_types: set[str] = set()
    for qtype, pattern, label in _TYPE_PATTERNS:
        m = pattern.search(blob) if blob else None
        if m:
            found_types.add(qtype)
            snip = m.group(0)
            # Prefer source doc containing the match
            src_doc = primary_doc
            for s in sources:
                if snip.lower() in s["text"].lower() or pattern.search(s["text"]):
                    src_doc = s["source_document"]
                    break
            requirements.append(
                {
                    "kind": "M3SourceQualificationRequirement",
                    "qualification_type": qtype,
                    "label": label,
                    "status": ST_DETECTED,
                    "evidence": _evidence(
                        source_document=src_doc,
                        requirement_text=snip[:200],
                        product=product,
                        supplier="UNKNOWN",
                        evidence=f"Requirement text matched: {snip[:160]}",
                        confidence="HIGH" if qtype in {QPL_REQUIRED, QML_REQUIRED} else "MEDIUM",
                    ),
                    "missing_action": _research_action(qtype),
                }
            )

    # Signal-only fallback (existing DLA extract) — DETECTED, never VALIDATED
    if struct.get("approved_source_signal") and APPROVED_SOURCE_REQUIRED not in found_types:
        # Map broad signal; may also imply QPL if "qpl" in description via pattern above
        requirements.append(
            {
                "kind": "M3SourceQualificationRequirement",
                "qualification_type": APPROVED_SOURCE_REQUIRED,
                "label": "Approved source signal (DLA structure)",
                "status": ST_DETECTED,
                "evidence": _evidence(
                    source_document="dla_product_structure",
                    requirement_text="approved_source_signal=true",
                    product=product,
                    supplier="UNKNOWN",
                    evidence="DLA product extract flagged approved_source_signal — not a validated listing",
                    confidence="MEDIUM",
                ),
                "missing_action": _research_action(APPROVED_SOURCE_REQUIRED),
            }
        )
        found_types.add(APPROVED_SOURCE_REQUIRED)

    if not requirements:
        requirements.append(
            {
                "kind": "M3SourceQualificationRequirement",
                "qualification_type": QUAL_UNKNOWN,
                "label": "Source qualification unknown",
                "status": ST_UNKNOWN,
                "evidence": _evidence(
                    source_document="UNKNOWN",
                    requirement_text="UNKNOWN",
                    product=product,
                    supplier="UNKNOWN",
                    evidence="Insufficient information to determine source qualification",
                    confidence="UNKNOWN",
                ),
                "missing_action": "Review solicitation for approved source / QPL / QML language",
            }
        )

    return requirements


def _research_action(qtype: str) -> str:
    return {
        QPL_REQUIRED: "Verify QPL listing",
        QML_REQUIRED: "Verify QML / qualified manufacturer listing",
        APPROVED_SOURCE_REQUIRED: "Verify approved source",
        SOURCE_CONTROLLED: "Verify manufacturer authorization / exact source",
        OEM_ONLY: "Verify manufacturer authorization",
        TRACEABILITY_REQUIRED: "Verify traceability",
        QUAL_UNKNOWN: "Research source qualification requirements",
    }.get(qtype, "Verify source qualification")


def _supplier_candidates(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Read suppliers from existing graph annotation — do not harden/replace."""
    candidates: list[dict[str, Any]] = []
    spg = row.get("supplier_product_graph") if isinstance(row.get("supplier_product_graph"), dict) else {}
    for e in spg.get("edges") or []:
        if isinstance(e, dict) and e.get("supplier_name"):
            candidates.append(dict(e))
    # Award projection relationships (historical — NOT auto-approved)
    award = row.get("award_product_projection") if isinstance(row.get("award_product_projection"), dict) else {}
    for rel in award.get("supplier_relationships") or []:
        if not isinstance(rel, dict) or not rel.get("supplier_name"):
            continue
        candidates.append(
            {
                "supplier_name": rel.get("supplier_name"),
                "relationship_type": "HISTORICAL_GOVERNMENT_SUPPLIER",
                "confidence": rel.get("confidence") or "UNKNOWN",
                "source": "award_product_projection",
                "evidence": rel.get("evidence"),
            }
        )
    # Explicit operator/structured approved list if present (evidence only)
    for key in ("approved_sources", "qualified_sources", "qpl_sources"):
        for item in row.get(key) or []:
            if isinstance(item, dict) and item.get("supplier_name"):
                candidates.append({**item, "explicit_approved_list": True})
            elif isinstance(item, str) and item.strip():
                candidates.append(
                    {
                        "supplier_name": item.strip(),
                        "explicit_approved_list": True,
                        "relationship_type": "LISTED_APPROVED_SOURCE",
                        "confidence": "HIGH",
                    }
                )
    return candidates


def evaluate_supplier_against_requirements(
    *,
    supplier: dict[str, Any],
    requirements: list[dict[str, Any]],
    product: dict[str, Any],
    text_blob: str,
) -> dict[str, Any]:
    """
    Link Product → Supplier → Qualification → Evidence.

    GOOD: listed approved source for NSN → VALIDATED
    BAD: website sells similar / historical awardee alone → UNKNOWN (not VALIDATED)
    """
    name = str(supplier.get("supplier_name") or "UNKNOWN")
    norm = _norm_name(name)
    rel = str(supplier.get("relationship_type") or "UNKNOWN").upper()
    conf = str(supplier.get("confidence") or "UNKNOWN").upper()

    # Explicit structured list membership
    if supplier.get("explicit_approved_list") or rel in {
        "LISTED_APPROVED_SOURCE",
        "QPL_LISTED",
        "QML_LISTED",
        "APPROVED_SOURCE",
    }:
        return {
            "supplier_name": name,
            "status": ST_VALIDATED,
            "qualification_match": True,
            "evidence": _evidence(
                source_document=str(supplier.get("source") or "approved_sources"),
                requirement_text="Listed on approved/qualified source record",
                product=product,
                supplier=name,
                evidence=f"Listed approved source for product {product.get('nsn')}",
                confidence="HIGH",
            ),
            "note": "Structured approved-source list membership",
        }

    # Textual listing: "approved source: ABC Manufacturing" matching this supplier
    listed = False
    list_snip = None
    for m in _LISTED_SOURCE_RE.finditer(text_blob or ""):
        listed_name = _norm_name(m.group(1))
        if listed_name and (norm in listed_name or listed_name in norm):
            listed = True
            list_snip = m.group(0)
            break
    cage = str(product.get("cage") or "").upper()
    if not listed and cage and cage != "UNKNOWN":
        for m in _CAGE_LISTED_RE.finditer(text_blob or ""):
            if m.group(1).upper() == cage and norm:
                # CAGE listed near approved-source language — still need name link; keep RESEARCH
                pass

    if listed and list_snip:
        return {
            "supplier_name": name,
            "status": ST_VALIDATED,
            "qualification_match": True,
            "evidence": _evidence(
                source_document="governing_text",
                requirement_text=list_snip[:200],
                product=product,
                supplier=name,
                evidence=f"Listed approved source for NSN {product.get('nsn')}: {list_snip[:120]}",
                confidence="HIGH",
            ),
            "note": "Name matched explicit approved-source listing in solicitation text",
        }

    # Historical government supplier / website / commercial — NOT qualification proof
    if rel in {"HISTORICAL_GOVERNMENT_SUPPLIER", "HISTORICAL_AWARDEE", "DISTRIBUTOR", "MANUFACTURER"}:
        return {
            "supplier_name": name,
            "status": ST_UNKNOWN,
            "qualification_match": False,
            "evidence": _evidence(
                source_document=str(supplier.get("source") or "supplier_product_graph"),
                requirement_text="UNKNOWN — no approved-source listing matched",
                product=product,
                supplier=name,
                evidence=(
                    f"Supplier relationship={rel}, confidence={conf}. "
                    "Historical award or commercial presence is not QPL/approved-source proof."
                ),
                confidence="LOW",
            ),
            "note": "Unsupported for auto-validation — research required",
        }

    return {
        "supplier_name": name,
        "status": ST_UNKNOWN,
        "qualification_match": False,
        "evidence": _evidence(
            source_document="UNKNOWN",
            requirement_text="UNKNOWN",
            product=product,
            supplier=name,
            evidence="No evidenced approved-source / QPL link for this supplier",
            confidence="UNKNOWN",
        ),
        "note": "Insufficient evidence",
    }


def build_source_qualification_profile(row: dict[str, Any] | None) -> dict[str, Any]:
    """
    Source qualification intelligence profile.

    Question: Can we legally and practically source this item for this contract?
    """
    row = row if isinstance(row, dict) else {}
    product = _product_ref(row)
    requirements = detect_qualification_requirements(row)
    sources = _collect_text_sources(row)
    blob = "\n".join(s["text"] for s in sources)
    suppliers_raw = _supplier_candidates(row)

    supplier_evals = [
        evaluate_supplier_against_requirements(
            supplier=s, requirements=requirements, product=product, text_blob=blob
        )
        for s in suppliers_raw
    ]

    # Dedupe by supplier name keeping best status
    rank = {ST_VALIDATED: 3, ST_RESEARCH: 2, ST_DETECTED: 1, ST_BLOCKED: 2, ST_UNKNOWN: 0}
    by_name: dict[str, dict[str, Any]] = {}
    for ev in supplier_evals:
        key = _norm_name(str(ev.get("supplier_name") or ""))
        if not key:
            continue
        prev = by_name.get(key)
        if not prev or rank.get(ev.get("status"), 0) >= rank.get(prev.get("status"), 0):
            by_name[key] = ev
    supplier_evals = list(by_name.values())

    active_reqs = [r for r in requirements if r.get("qualification_type") != QUAL_UNKNOWN]
    validated_suppliers = [s for s in supplier_evals if s.get("status") == ST_VALIDATED]
    hard_qual = any(
        r.get("qualification_type")
        in {APPROVED_SOURCE_REQUIRED, QPL_REQUIRED, QML_REQUIRED, SOURCE_CONTROLLED, OEM_ONLY}
        for r in active_reqs
    )

    if validated_suppliers and active_reqs:
        overall = ST_VALIDATED
        answer = "Approved / qualified source confirmed with evidence."
        offer_status = ST_VALIDATED
        offer_label = "Approved source confirmed."
        blocker_type = "COMPLETE"
    elif hard_qual and not supplier_evals:
        overall = ST_BLOCKED
        answer = "Qualification required but no eligible source identified."
        offer_status = ST_BLOCKED
        offer_label = "No eligible source identified."
        blocker_type = "HARD_BLOCKER"
    elif hard_qual and not validated_suppliers:
        overall = ST_RESEARCH
        answer = "Qualification requirement exists — supplier eligibility not evidenced."
        offer_status = ST_RESEARCH
        offer_label = "Qualification requirement exists."
        blocker_type = "SOFT_BLOCKER"
    elif active_reqs:
        overall = ST_DETECTED
        answer = "Source qualification language detected — research required."
        offer_status = ST_RESEARCH
        offer_label = "Qualification requirement exists."
        blocker_type = "SOFT_BLOCKER"
    else:
        overall = ST_UNKNOWN
        answer = "Insufficient information to determine source qualification."
        offer_status = ST_UNKNOWN
        offer_label = "Source qualification unknown."
        blocker_type = None

    research_actions = []
    for r in active_reqs:
        qtype = str(r.get("qualification_type"))
        research_actions.append(
            {
                "action": _research_action(qtype),
                "qualification_type": qtype,
                "research_type": "SUPPLIER",
                "priority": 75 if qtype in {QPL_REQUIRED, APPROVED_SOURCE_REQUIRED, SOURCE_CONTROLLED} else 60,
            }
        )
    # Deduplicate actions
    seen_a: set[str] = set()
    uniq_actions = []
    for a in research_actions:
        if a["action"] in seen_a:
            continue
        seen_a.add(a["action"])
        uniq_actions.append(a)

    return {
        "kind": "M3SourceQualificationProfile",
        "build": BUILD_TAG,
        "opportunity_id": row.get("canonical_id") or "UNKNOWN",
        "question": "Can we legally and practically source this item for this contract?",
        "answer": answer,
        "status": overall,
        "product": product,
        "requirements": requirements,
        "suppliers": supplier_evals,
        "validated_supplier_count": len(validated_suppliers),
        "research_actions": uniq_actions,
        "offer_readiness": {
            "category": "SOURCE_APPROVAL",
            "status": offer_status,
            "label": offer_label,
            "blocker_type": blocker_type,
            "evidence": (
                validated_suppliers[0]["evidence"]
                if validated_suppliers
                else (active_reqs[0]["evidence"] if active_reqs else requirements[0]["evidence"])
            ),
            "missing_action": (
                "None — approved source confirmed"
                if overall == ST_VALIDATED
                else (uniq_actions[0]["action"] if uniq_actions else "Research source qualification")
            ),
        },
        "false_approvals_forbidden": True,
        "assumes_qualification": False,
        "supplier_graph_unchanged": True,
        "scoring_unchanged": True,
        "engines_unchanged": True,
        "generated_at": _utc(),
        "read_only": True,
    }


def qualification_to_offer_requirements(profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Map profile → Offer Readiness SOURCE_APPROVAL rows."""
    offer = profile.get("offer_readiness") if isinstance(profile.get("offer_readiness"), dict) else {}
    status = str(offer.get("status") or ST_UNKNOWN)
    btype = offer.get("blocker_type")
    ev = offer.get("evidence") if isinstance(offer.get("evidence"), dict) else {}
    evidence_str = (
        f"{ev.get('evidence')} · doc={ev.get('source_document')} · "
        f"supplier={ev.get('supplier')} · conf={ev.get('confidence')}"
    )
    return [
        {
            "kind": "M3OfferRequirement",
            "category": "SOURCE_APPROVAL",
            "requirement": offer.get("label") or "Source approval / QPL qualification",
            "status": status,
            "evidence": evidence_str,
            "missing_action": offer.get("missing_action"),
            "blocker_type": btype,
            "mandatory": status in {ST_BLOCKED, ST_RESEARCH, ST_DETECTED, ST_VALIDATED}
            and profile.get("status") != ST_UNKNOWN,
            "source": "source_qualification",
            "raw_status": profile.get("status"),
            "qualification_types": [
                r.get("qualification_type") for r in (profile.get("requirements") or []) if isinstance(r, dict)
            ],
            "clause_evidence": ev,
        }
    ]


def qualification_to_research_items(profile: dict[str, Any], *, opportunity_id: str) -> list[dict[str, Any]]:
    """Research queue actions for source qualification gaps."""
    if profile.get("status") == ST_VALIDATED:
        return []
    if profile.get("status") == ST_UNKNOWN and not [
        r for r in (profile.get("requirements") or []) if r.get("qualification_type") != QUAL_UNKNOWN
    ]:
        return []
    items = []
    for a in profile.get("research_actions") or []:
        items.append(
            {
                "opportunity_id": opportunity_id,
                "research_type": "SUPPLIER",
                "status": "NEW",
                "priority": int(a.get("priority") or 60),
                "priority_label": "HIGH" if int(a.get("priority") or 0) >= 70 else "MEDIUM",
                "why_this_matters": f"Source qualification: {a.get('qualification_type')}",
                "recommended_action": a.get("action"),
                "missing_information": [a.get("qualification_type") or "SOURCE_APPROVAL"],
                "category": "SOURCE_APPROVAL",
                "source": "source_qualification",
            }
        )
    return items[:10]
