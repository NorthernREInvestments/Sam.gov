"""Evidence-only procurement requirements, reseller eligibility, economics and execution feasibility.

Every extracted value carries the verbatim span it came from. Nothing is defaulted:
a missing quantity stays missing, an unverified cost stays unverified, and a record is
never promoted to quote-ready on partial evidence.
"""

from __future__ import annotations

import re
from typing import Any

from m3.research.extract_fields import extract_identity_fields, extract_quantity_fields

REQUIREMENT_STATES = ("COMPLETE", "PARTIAL", "NOT_FOUND", "NO_TEXT")
ELIGIBILITY_STATES = ("ELIGIBLE", "EASY_REGISTRATION_REQUIRED", "ELIGIBILITY_UNKNOWN", "BLOCKED")
PROFIT_CLASSES = ("PROVEN", "LIKELY", "POSSIBLE", "UNPROVEN", "UNPROFITABLE", "EXECUTION_BLOCKED")
EXECUTION_STATES = ("FEASIBLE", "FINANCING_VERIFICATION_REQUIRED", "EXECUTION_BLOCKED", "EXECUTION_UNKNOWN")

# Owner execution policy: zero owner cash, no personal guarantees, no personal credit.
OWNER_EXECUTION_POLICY = {
    "owner_cash_available": 0.0,
    "personal_guarantee_allowed": False,
    "personal_credit_allowed": False,
    "acceptable_mechanisms": (
        "supplier_net_terms",
        "government_purchase_card",
        "purchase_order_financing",
        "contract_financing",
        "assignment_of_claims",
    ),
}

_UOM = (
    r"EA|EACH|BX|BOX(?:ES)?|CS|CASES?|PK|PKG|PACK(?:S|AGES?)?|DZ|DOZ(?:EN)?|PR|PAIRS?|SET|SETS|RL|ROLLS?|"
    r"GAL|GALLONS?|LB|LBS|POUNDS?|TON|TONS|CY|YD|YDS|FT|LF|SF|SQ\s?FT|KIT|KITS|LOT|LOTS|UNITS?|HR|HRS|"
    r"DRUMS?|BAGS?|PALLETS?|CTN|CARTONS?|RM|REAMS?|TUBES?|BOTTLES?|CANS?|SH|SHEETS?"
)
_LINE_START = re.compile(
    r"(?im)(?:^|\s)(?P<label>(?:CLIN|SLIN|Line(?:\s+Item)?|Item(?:\s+No\.?)?|Lot)\s*#?\s*(?P<num>\d{1,5}[A-Z]?))\b[\s:.\-–)]*"
)
_QTY_UOM = re.compile(
    rf"(?i)\b(?:qty|quantity)?\s*[:=]?\s*(?P<qty>\d[\d,]*(?:\.\d+)?)\s*(?P<uom>{_UOM})\b"
)
_QTY_LABELLED = re.compile(rf"(?i)\b(?:qty|quantity)\s*[:=]?\s*(?P<qty>\d[\d,]*(?:\.\d+)?)(?:\s*(?P<uom>{_UOM})\b)?")
_PACK = re.compile(r"(?i)\b(?P<pack>\d+\s*(?:/|per)\s*(?:box|case|pack|pkg|carton|roll|bag)|(?:box|case|pack|carton)\s+of\s+\d+)\b")
_MFR = re.compile(
    r"(?i)\b(?:(?:manufacturer|mfr|mfg)(?!\s+(?:p/n|part))\s*[:#]?|(?:make|brand)\s*:)\s*"
    r"(?P<v>(?!name\b|or\b)[A-Z][\w&.\- ]{1,40}?)(?=\s*(?:,|;|\.|model|part|p/n|mpn|$))"
)
_MODEL = re.compile(r"(?i)\bmodel\s*(?:no\.?|number|#)?\s*[:#]?\s*(?P<v>[A-Z0-9](?:[\w\-/]|\.(?=\w)){1,30})")
_MPN = re.compile(r"(?i)\b(?:p/n|part\s*(?:no\.?|number|#)|mfr\.?\s*(?:p/n|part(?:\s*(?:no\.?|number))?)|mpn)\s*[:#]?\s*(?P<v>[A-Z0-9][\w\-./]{2,30})")
_NSN = re.compile(r"\b(?P<v>\d{4}-\d{2}-\d{3}-\d{4})\b|(?i:\bnsn\b)[:#\s]*(?P<v2>\d{13})\b")
_OR_EQUAL = re.compile(r"(?i)\b(?:or\s+(?:approved\s+)?equal|equivalent\s+(?:products?\s+)?(?:will\s+be\s+)?(?:accepted|considered)|brand\s+name\s+or\s+equal)\b")
_BRAND_ONLY = re.compile(r"(?i)\b(?:no\s+substitut\w+|brand\s+name\s+only|exact\s+(?:make|brand|part)\s+(?:and\s+model\s+)?(?:only|required)|no\s+(?:equals?|equivalents?))\b")
_SPEC = re.compile(r"(?i)\b(?:shall\s+(?:meet|conform\s+to|comply\s+with)|specifications?\s*[:\-]|minimum\s+requirements?|salient\s+characteristics?|in\s+accordance\s+with\s+(?:astm|ansi|mil-|nfpa|ul|aashto)\S*)[^.;]{0,160}")
_DELIVERY = re.compile(r"(?i)\b(?:deliver(?:y|ed)?\s+(?:to|within|by|no\s+later\s+than|schedule|date|location|f\.?o\.?b)|f\.?o\.?b\.?\s+(?:destination|origin)|ship\s+to|\d+\s+(?:calendar\s+|business\s+)?days\s+(?:ARO|after\s+receipt\s+of\s+order))[^.;]{0,140}")
_PACKAGING = re.compile(r"(?i)\b(?:packag(?:ing|ed)|palletiz\w+|marking\s+and\s+labeling|MIL-STD-129|MIL-STD-2073|level\s+[ABC]\s+packaging)[^.;]{0,120}")
_CERT = re.compile(r"(?i)\b(?:certificate\s+of\s+(?:conformance|compliance|analysis)|C\s?of\s?C|ISO\s?\d{4,5}|UL[- ]listed|NSF[- ]\d+|FDA[- ]approved|ANSI\s+[A-Z]?\d+|EPA[- ]registered|Buy\s+American|BAA|TAA[- ]compliant)[^.;]{0,100}")
_INSPECTION = re.compile(r"(?i)\b(?:inspection\s+and\s+acceptance|inspected\s+(?:at|upon)|acceptance\s+testing|first\s+article(?:\s+test(?:ing)?)?|source\s+inspection|destination\s+inspection)[^.;]{0,120}")
_INSTALLATION = re.compile(r"(?i)\b(?:install(?:ation|ed)?|set[- ]?up|assembl(?:y|ed)\s+on\s+site|commissioning|upfit\w*)[^.;]{0,120}")
_SPECIAL = re.compile(r"(?i)\b(?:liquidated\s+damages|prevailing\s+wage|davis[- ]bacon|warranty\s+of\s+\d+|minimum\s+warranty|shelf\s+life|hazmat|hazardous\s+material|SDS\s+required|recycled\s+content|security\s+clearance|background\s+check)[^.;]{0,120}")


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _num(v: str | None) -> float | None:
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _span(text: str, m: re.Match[str], pad: int = 0) -> str:
    return text[max(0, m.start() - pad): m.end() + pad].strip()[:220]


def _first(pat: re.Pattern[str], text: str, group: str = "v") -> tuple[str | None, str | None]:
    m = pat.search(text)
    if not m:
        return None, None
    val = m.groupdict().get(group) or m.groupdict().get(group + "2") or m.group(0)
    return _clean(val), _span(text, m)


def _segment_line_items(text: str) -> list[dict[str, Any]]:
    starts = list(_LINE_START.finditer(text))
    blocks: list[dict[str, Any]] = []
    for i, m in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else min(len(text), m.end() + 600)
        body = text[m.end(): end].strip()
        if len(body) < 3:
            continue
        blocks.append({"label": _clean(m.group("label")), "number": m.group("num"), "text": body[:600], "offset": m.start()})
    return blocks


def _parse_line(block: dict[str, Any]) -> dict[str, Any]:
    t = block["text"]
    qm = _QTY_LABELLED.search(t) or _QTY_UOM.search(t)
    qty = _num(qm.group("qty")) if qm else None
    uom = ((qm.group("uom") or "").upper() or None) if qm else None
    desc = re.split(rf"(?i)\b(?:qty|quantity)\b|\s\d[\d,]*\s*(?:{_UOM})\b", t, maxsplit=1)[0].strip(" ,;:-")
    mfr, mfr_ev = _first(_MFR, t)
    model, model_ev = _first(_MODEL, t)
    mpn, mpn_ev = _first(_MPN, t)
    nm = _NSN.search(t)
    nsn = (nm.group("v") or nm.group("v2")) if nm else None
    pack, pack_ev = _first(_PACK, t, "pack")
    evidence = {k: v for k, v in {
        "quantity": _span(t, qm) if qm else None,
        "manufacturer": mfr_ev, "model": model_ev, "mpn": mpn_ev,
        "nsn": _span(t, nm) if nm else None, "pack_size": pack_ev,
    }.items() if v}
    return {
        "clin": block["label"],
        "description": desc[:200] or None,
        "quantity": qty,
        "unit_of_measure": uom,
        "pack_size": pack,
        "manufacturer": mfr,
        "model": model,
        "mpn": mpn,
        "nsn": nsn,
        "brand_name_or_equal": bool(_OR_EQUAL.search(t)),
        "brand_name_only": bool(_BRAND_ONLY.search(t)),
        "complete": bool(desc and qty is not None and uom),
        "evidence": evidence,
    }


def extract_requirements(
    title: str,
    *,
    description: str | None = None,
    document_text: str | None = None,
    amendments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Line items and solicitation-level requirements with evidence references.

    ``amendments`` (newest last, each ``{"id", "text"}``) take precedence over the base text:
    a line restated in a later amendment replaces the earlier one.
    """
    base = _clean(" ".join(x for x in (description, document_text) if x))
    sources: list[tuple[str, str]] = [("base", base)] if base else []
    for a in amendments or []:
        if _clean(a.get("text")):
            sources.append((f"amendment:{a.get('id') or len(sources)}", _clean(a.get("text"))))
    if not sources:
        return {"requirements_state": "NO_TEXT", "line_items": [], "line_item_count": 0, "complete_line_items": 0,
                "solicitation_requirements": {}, "quote_ready": False, "quote_ready_blockers": ["no_solicitation_text_retained"],
                "title": _clean(title)}

    lines_by_key: dict[str, dict[str, Any]] = {}
    for src, text in sources:
        for block in _segment_line_items(text):
            li = _parse_line(block)
            li["source"] = src
            lines_by_key[block["label"].upper()] = li

    if not lines_by_key:
        # Single-item solicitations: a firm quantity from the existing extractor (never defaulted)
        joined = " ".join(t for _, t in sources)
        q = extract_quantity_fields(joined, title=_clean(title))
        if q.get("quantity_extracted") and q.get("quantity_is_ordered"):
            ident = extract_identity_fields(joined)
            lines_by_key["SINGLE"] = {
                "clin": None, "description": _clean(title)[:200], "quantity": _num(q.get("quantity")),
                "unit_of_measure": (q.get("quantity_unit") or None), "pack_size": None,
                "manufacturer": None, "model": None, "mpn": ident.get("manufacturer_part_number"),
                "nsn": ident.get("nsn"), "brand_name_or_equal": bool(_OR_EQUAL.search(joined)),
                "brand_name_only": bool(_BRAND_ONLY.search(joined)),
                "complete": bool(q.get("quantity") and q.get("quantity_unit")),
                "evidence": {"quantity": "; ".join(str(x) for x in (q.get("quantity_evidence") or [])[:2])},
                "source": "single_item_text", "quantity_semantic": q.get("quantity_semantic"),
            }

    full = " ".join(t for _, t in sources)
    req: dict[str, Any] = {}
    for name, pat in (("specifications", _SPEC), ("delivery", _DELIVERY), ("packaging", _PACKAGING),
                      ("certifications", _CERT), ("inspection", _INSPECTION), ("installation", _INSTALLATION),
                      ("special_conditions", _SPECIAL)):
        hits = [_span(full, m) for m in list(pat.finditer(full))[:3]]
        if hits:
            req[name] = hits
    req["brand_name_or_equal"] = bool(_OR_EQUAL.search(full))
    req["brand_name_only"] = bool(_BRAND_ONLY.search(full))

    lines = list(lines_by_key.values())
    complete = [li for li in lines if li.get("complete")]
    blockers: list[str] = []
    if not lines:
        blockers.append("no_line_items_or_firm_quantity_in_retained_text")
    elif len(complete) < len(lines):
        blockers.append(f"incomplete_line_items:{len(lines) - len(complete)}_of_{len(lines)}")
    if not any(li.get("manufacturer") or li.get("mpn") or li.get("nsn") or li.get("model") for li in lines) and not req.get("specifications"):
        blockers.append("product_identity_not_specified")
    if "delivery" not in req:
        blockers.append("delivery_terms_not_found")
    state = "COMPLETE" if lines and not blockers else ("PARTIAL" if lines or req.get("specifications") else "NOT_FOUND")
    return {
        "requirements_state": state,
        "line_items": lines,
        "line_item_count": len(lines),
        "complete_line_items": len(complete),
        "solicitation_requirements": req,
        "amendments_applied": [s for s, _ in sources if s.startswith("amendment:")],
        "quote_ready": state == "COMPLETE",
        "quote_ready_blockers": blockers,
        "title": _clean(title),
    }


# --- Reseller eligibility --------------------------------------------------------------

_ELIG_RULES: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("socioeconomic_set_aside", "BLOCKED", re.compile(
        r"(?i)\b(?:8\s?\(a\)\s+(?:set[- ]aside|program|participants?)|sdvosb|service[- ]disabled\s+veteran[- ]owned\s+small\s+business\s+set[- ]aside|"
        r"hubzone\s+set[- ]aside|wosb\s+set[- ]aside|edwosb|women[- ]owned\s+small\s+business\s+set[- ]aside|"
        r"(?:set[- ]aside|reserved)\s+(?:exclusively\s+)?for\s+(?:certified\s+)?(?:mbe|wbe|dbe|sdvosb|8\(a\)|hubzone))\b")),
    ("small_business_set_aside", "UNKNOWN", re.compile(r"(?i)\b(?:total\s+)?small\s+business\s+set[- ]aside\b|\bset[- ]aside\s+for\s+small\s+business")),
    ("gsa_schedule_required", "BLOCKED", re.compile(r"(?i)\b(?:gsa\s+(?:schedule|mas)\s+(?:holders?|contractors?)\s+only|must\s+(?:hold|have)\s+(?:a\s+)?(?:current\s+)?gsa\s+(?:schedule|contract))\b")),
    ("existing_vehicle_holders_only", "BLOCKED", re.compile(
        r"(?i)\b(?:(?:idiq|bpa|boa|maCC?|ma[- ]idiq)\s+(?:holders?|awardees?)\s+only|task\s+order\s+(?:request|under)|"
        r"fair\s+opportunity|limited\s+to\s+(?:current\s+)?(?:contract|bpa|idiq)\s+holders)\b")),
    ("approved_source_only", "UNKNOWN", re.compile(r"(?i)\b(?:approved\s+sources?\s+(?:only|list)|qualified\s+products?\s+list|\bQPL\b|source[- ]controlled|only\s+the\s+following\s+(?:approved\s+)?sources?)\b")),
    ("oem_or_authorized_dealer_only", "UNKNOWN", re.compile(
        r"(?i)\b(?:oem\s+only|manufacturer\s+only|authorized\s+(?:dealer|distributor|reseller)s?\s+(?:only|letter|required)|"
        r"(?:letter\s+of\s+)?(?:manufacturer|factory)(?:'?s)?\s+authorization|factory[- ]authorized)\b")),
    ("prequalified_bidders_only", "BLOCKED", re.compile(r"(?i)\b(?:only\s+pre[- ]?qualified\s+(?:bidders|vendors|contractors)|must\s+be\s+pre[- ]?qualified)\b")),
    ("geographic_restriction", "UNKNOWN", re.compile(r"(?i)\b(?:local\s+(?:vendors?|bidders?|businesses?)\s+only|must\s+be\s+located\s+(?:within|in)|within\s+\d+\s+miles\s+of|in[- ]state\s+(?:vendors?|bidders?)\s+only)\b")),
    ("mandatory_site_visit", "UNKNOWN", re.compile(r"(?i)\bmandatory\s+(?:pre[- ]?bid\s+(?:meeting|conference)|site\s+(?:visit|inspection)|walk[- ]?(?:thru|through))\b")),
    ("contractor_license_required", "UNKNOWN", re.compile(r"(?i)\b(?:contractor'?s?\s+licen[cs]e|licensed\s+(?:general\s+)?contractor|class\s+[A-C]\d*\s+licen[cs]e|state\s+licensed\s+(?:electrician|plumber|contractor))\b")),
    ("bond_required", "UNKNOWN", re.compile(r"(?i)\b(?:bid\s+(?:bond|guarantee|security)|performance\s+(?:and\s+payment\s+)?bond|payment\s+bond|surety\s+bond)\b")),
    ("certification_required", "UNKNOWN", re.compile(r"(?i)\b(?:must\s+be\s+(?:iso|as9100)\s*\d*\s*certified|certified\s+installer|factory[- ]trained\s+technicians?)\b")),
)
_BOND_WAIVED = re.compile(r"(?i)\b(?:no\s+(?:bid\s+)?bond\s+(?:is\s+)?required|bonds?\s+(?:is\s+|are\s+)?(?:not\s+required|waived))\b")
_REGISTRATION = re.compile(
    r"(?i)\b(?:register(?:ed)?\s+(?:in|with|on)\s+(?:the\s+)?(?:sam|system\s+for\s+award\s+management|portal|bonfire|planetbids|opengov|bidnet|"
    r"vendor\s+(?:portal|self[- ]service)|supplier\s+portal)|vendor\s+registration\s+(?:is\s+)?required|active\s+sam\s+registration|unique\s+entity\s+id)\b"
)
_PORTAL_REGISTRATION_SOURCES = {"sam.gov", "dibbs", "bonfire", "planetbids", "opengov", "bidnet", "publicpurchase", "calprocure"}


def assess_reseller_eligibility(
    *,
    title: str,
    text: str | None,
    source: str | None,
    evidence_completeness: str,
    product_presence: str | None = None,
    owner_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Can a reseller of a manufacturer's product lawfully respond? Fail-closed on missing evidence."""
    owner = owner_profile or {}
    blob = _clean(f"{title} {text or ''}")
    findings: list[dict[str, Any]] = []
    for name, severity, pat in _ELIG_RULES:
        m = pat.search(blob)
        if not m:
            continue
        if name == "bond_required" and _BOND_WAIVED.search(blob):
            continue
        sev = severity
        if name == "small_business_set_aside" and owner.get("small_business") is True:
            sev = "PASS"
        if name == "contractor_license_required" and product_presence == "EXPLICIT_PRODUCT" and re.search(r"(?i)\bmaterials?\s+only\b", blob):
            sev = "PASS"
        findings.append({"restriction": name, "severity": sev, "evidence": _span(blob, m, 40)})
    registration = bool(_REGISTRATION.search(blob)) or str(source or "").lower() in _PORTAL_REGISTRATION_SOURCES
    if registration:
        m = _REGISTRATION.search(blob)
        findings.append({"restriction": "vendor_registration", "severity": "EASY",
                         "evidence": _span(blob, m, 30) if m else f"portal_source:{source}"})
    blocked = [f for f in findings if f["severity"] == "BLOCKED"]
    unknown = [f for f in findings if f["severity"] == "UNKNOWN"]
    sufficient = evidence_completeness == "DOCUMENT"
    if blocked:
        state = "BLOCKED"
    elif unknown or not sufficient:
        state = "ELIGIBILITY_UNKNOWN"
    elif registration:
        state = "EASY_REGISTRATION_REQUIRED"
    else:
        state = "ELIGIBLE"
    reasons = [f["restriction"] for f in blocked + unknown]
    if not sufficient and not blocked:
        reasons.append(f"terms_not_verifiable_from_{evidence_completeness.lower()}")
    return {"eligibility_state": state, "eligibility_findings": findings, "eligibility_reasons": reasons,
            "registration_required": registration}


# --- Economics (verified inputs only) --------------------------------------------------

def assess_economics(
    *,
    line_items: list[dict[str, Any]],
    verified_inputs: dict[str, Any] | None = None,
    execution_state: str | None = None,
    pricing_reference: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Profit from verified evidence only.

    ``verified_inputs`` keys (each must cite evidence): government_unit_price, supplier_unit_cost,
    freight, packaging, transaction_fees, financing_cost, evidence_refs. Without a verified supplier
    cost and a verified government price the class is UNPROVEN — never estimated into PROVEN/LIKELY.
    """
    vi = dict(verified_inputs or {})
    qty = sum(float(li["quantity"]) for li in line_items if li.get("quantity") is not None) if line_items else None
    missing = [k for k in ("government_unit_price", "supplier_unit_cost") if vi.get(k) in (None, "")]
    if not qty:
        missing.append("quantity")
    if not vi.get("evidence_refs"):
        missing.append("evidence_refs")
    out: dict[str, Any] = {
        "quantity_total": qty, "revenue": None, "cost_of_goods": None, "freight": vi.get("freight"),
        "packaging": vi.get("packaging"), "transaction_fees": vi.get("transaction_fees"),
        "financing_cost": vi.get("financing_cost"), "gross_profit": None, "net_profit": None, "margin": None,
        "missing_inputs": missing, "evidence_quality": "NONE" if missing else "VERIFIED",
        "pricing_reference": pricing_reference, "risks": [],
    }
    if execution_state == "EXECUTION_BLOCKED":
        out["profit_class"] = "EXECUTION_BLOCKED"
        out["risks"].append("execution_blocked_under_owner_policy")
        return out
    if missing:
        out["profit_class"] = "POSSIBLE" if (pricing_reference and pricing_reference.get("matches")) else "UNPROVEN"
        if out["profit_class"] == "POSSIBLE":
            out["risks"].append("historical_award_reference_is_not_a_quote")
        return out
    revenue = float(vi["government_unit_price"]) * qty
    cogs = float(vi["supplier_unit_cost"]) * qty
    extras = sum(float(vi.get(k) or 0) for k in ("freight", "packaging", "transaction_fees", "financing_cost"))
    gross = revenue - cogs
    net = gross - extras
    out.update(revenue=round(revenue, 2), cost_of_goods=round(cogs, 2), gross_profit=round(gross, 2),
               net_profit=round(net, 2), margin=round(net / revenue, 4) if revenue else None)
    if net <= 0:
        out["profit_class"] = "UNPROFITABLE"
    elif vi.get("government_price_basis") == "firm_award_or_published_price" and vi.get("supplier_cost_basis") == "written_quote":
        out["profit_class"] = "PROVEN"
    else:
        out["profit_class"] = "LIKELY"
    return out


# --- Execution feasibility (separate from profitability) -------------------------------

_EXEC_BLOCK = (
    ("bid_or_performance_bond", re.compile(r"(?i)\b(?:bid\s+(?:bond|guarantee|security)|performance\s+(?:and\s+payment\s+)?bond|payment\s+bond|surety)\b")),
    ("prepayment_or_deposit", re.compile(r"(?i)\b(?:deposit\s+(?:of|required)|certified\s+check|cashier'?s\s+check|letter\s+of\s+credit|prepayment\s+by\s+(?:the\s+)?(?:vendor|contractor))\b")),
    ("personal_guarantee", re.compile(r"(?i)\bpersonal(?:ly)?\s+guarant\w+\b")),
)
_EXEC_SUPPORT = (
    ("purchase_card_accepted", re.compile(r"(?i)\b(?:p[- ]?card|purchase\s+card|procurement\s+card|government\s+(?:purchase\s+|credit\s+)?card)\b")),
    ("net_payment_terms", re.compile(r"(?i)\bnet\s*\d{2}\b|\bpayment\s+(?:within|in)\s+\d{2}\s+days\b|\bprompt\s+payment\s+act\b")),
    ("partial_or_progress_payments", re.compile(r"(?i)\b(?:partial\s+(?:payments?|deliveries)|progress\s+payments?|performance[- ]based\s+payments?)\b")),
    ("assignment_of_claims", re.compile(r"(?i)\bassignment\s+of\s+claims\b")),
)


def assess_execution_feasibility(*, text: str | None, source: str | None, evidence_completeness: str) -> dict[str, Any]:
    """Can the owner perform with zero owner cash, no personal guarantee and no personal credit?"""
    blob = _clean(text)
    blocks = []
    for name, pat in _EXEC_BLOCK:
        m = pat.search(blob)
        if m and not (name == "bid_or_performance_bond" and _BOND_WAIVED.search(blob)):
            blocks.append({"factor": name, "evidence": _span(blob, m, 40)})
    supports = []
    for name, pat in _EXEC_SUPPORT:
        m = pat.search(blob)
        if m:
            supports.append({"factor": name, "evidence": _span(blob, m, 30)})
    if str(source or "").lower() in {"sam.gov", "dibbs"}:
        supports.append({"factor": "federal_prompt_payment_and_assignment_of_claims_available",
                         "evidence": "FAR 32.9 Prompt Payment / FAR 32.8 Assignment of Claims apply to federal awards"})
    if blocks:
        state = "EXECUTION_BLOCKED"
    elif evidence_completeness != "DOCUMENT":
        state = "EXECUTION_UNKNOWN"
    else:
        # Even with supportive terms, supplier credit terms are not yet verified for any record
        state = "FINANCING_VERIFICATION_REQUIRED"
    return {"execution_state": state, "execution_blockers": blocks, "execution_supports": supports,
            "owner_policy": {k: v for k, v in OWNER_EXECUTION_POLICY.items() if k != "acceptable_mechanisms"}}
