"""BUILD 16 — Supplier + Capital + Operations Intelligence (read models).

Extends Execution OS with supplier engagement, commercial terms, relationship
memory, financing/acquisition paths, cyber/insurance applicability, supply-chain
path graph, and pursuit readiness gate.

Does NOT rebuild engines, invent capability/financing/terms, or create scores.
UNKNOWN remains UNKNOWN. Append-only history only.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any

from application_clock import now_utc

BUILD_TAG = "20260919-m3-supplier-capital-ops-1"

# Shared states
ST_UNKNOWN = "UNKNOWN"
ST_DETECTED = "DETECTED"
ST_VERIFIED = "VERIFIED"
ST_EXPIRED = "EXPIRED"
ST_RESEARCH = "RESEARCH_REQUIRED"
ST_VALIDATED = "VALIDATED"
ST_BLOCKED = "BLOCKED"
ST_NOT_APPLICABLE = "NOT_APPLICABLE"

# Pursuit gate (no numeric score)
GATE_PURSUE = "PURSUE"
GATE_RESEARCH = "RESEARCH_REQUIRED"
GATE_ACCESS_BLOCKED = "ACCESS_BLOCKED"
GATE_EXEC_UNKNOWN = "EXECUTION_UNKNOWN"

# Supply chain relationship types
REL_MANUFACTURER = "MANUFACTURER"
REL_AUTHORIZED = "AUTHORIZED_DISTRIBUTOR"
REL_DISTRIBUTOR = "DISTRIBUTOR"
REL_RESELLER = "RESELLER"
REL_BROKER = "BROKER"
REL_SURPLUS = "SURPLUS_SOURCE"
REL_UNKNOWN = "UNKNOWN"

RELATIONSHIP_TYPES = (
    REL_MANUFACTURER,
    REL_AUTHORIZED,
    REL_DISTRIBUTOR,
    REL_RESELLER,
    REL_BROKER,
    REL_SURPLUS,
    REL_UNKNOWN,
)

SUPPLIER_REL_MEMORY_KEY = "m3_supplier_relationship_memory_v1"
SUPPLIER_TERMS_HISTORY_KEY = "m3_supplier_commercial_terms_history_v1"

# Acquisition path labels
ACQ_OPEN = "Open Market"
ACQ_DLA = "DLA/DIBBS"
ACQ_GSA = "GSA Schedule"
ACQ_BPA = "BPA"
ACQ_IDIQ = "IDIQ Task Order"
ACQ_GWAC = "GWAC"
ACQ_MAC = "MAC"
ACQ_OTHER = "Other"

_ACQ_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (ACQ_DLA, re.compile(r"\b(?:DLA|DIBBS|DIBBS\.|Defense\s+Logistics\s+Agency)\b", re.I)),
    (ACQ_GSA, re.compile(r"\b(?:GSA\s+Schedule|GSA\.gov|Federal\s+Supply\s+Schedule|\bFSS\b)\b", re.I)),
    (ACQ_BPA, re.compile(r"\b(?:BPA|Blanket\s+Purchase\s+Agreement)\b", re.I)),
    (ACQ_IDIQ, re.compile(r"\b(?:IDIQ|task\s+order|indefinite[\s-]delivery)\b", re.I)),
    (ACQ_GWAC, re.compile(r"\b(?:GWAC|Governmentwide\s+Acquisition)\b", re.I)),
    (ACQ_MAC, re.compile(r"\b(?:\bMAC\b|Multiple\s+Award\s+Contract)\b", re.I)),
    (ACQ_OPEN, re.compile(r"\b(?:open\s+market|full\s+and\s+open|unrestricted)\b", re.I)),
]

_CYBER_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("CUI", re.compile(r"\b(?:CUI|Controlled\s+Unclassified\s+Information)\b", re.I)),
    ("CTI", re.compile(r"\b(?:Controlled\s+Technical\s+Information|CTI)\b", re.I)),
    ("DFARS_CYBER", re.compile(r"DFARS\s*252\.204[\-/]?(?:7012|7019|7020|7021)", re.I)),
    ("CMMC", re.compile(r"\b(?:CMMC|Cybersecurity\s+Maturity\s+Model\s+Certification)\b", re.I)),
    ("NIST", re.compile(r"\b(?:NIST\s*800[\-/]?171|NIST\s*SP\s*800)\b", re.I)),
    ("EXPORT", re.compile(r"\b(?:ITAR|EAR|export[\s-]controlled)\b", re.I)),
]

_INSURANCE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("BOND", re.compile(r"\b(?:bid\s+bond|performance\s+bond|payment\s+bond|surety)\b", re.I)),
    ("LIABILITY", re.compile(r"\b(?:general\s+liability|commercial\s+general\s+liability|\bCGL\b)\b", re.I)),
    ("CARGO", re.compile(r"\b(?:cargo\s+insurance|in[\s-]transit\s+insurance|marine\s+cargo)\b", re.I)),
    ("PROPERTY", re.compile(r"\b(?:property\s+insurance|all[\s-]risk\s+property)\b", re.I)),
    ("INSURANCE_GENERAL", re.compile(r"\b(?:insurance\s+required|certificate\s+of\s+insurance|\bCOI\b)\b", re.I)),
]

SUPPLIER_QUESTIONS = (
    "Can supplier provide exact item?",
    "New condition?",
    "OEM?",
    "Authorized channel?",
    "Traceability available?",
    "Lead time?",
    "Minimum order quantity?",
    "Warranty?",
    "Replacement process?",
)

FINANCING_PATH_NAMES = (
    "supplier_terms",
    "purchase_order_financing",
    "receivables_financing",
    "government_contract_financing",
    "sba_lender_options",
    "other_identified_paths",
)

REL_MEMORY_STAGES = (
    "SUPPLIER_CONTACTED",
    "CAPABILITY_VERIFIED",
    "QUOTE_REQUESTED",
    "QUOTE_RECEIVED",
    "CONTRACT_WON_LOST",
    "EXECUTION_RESULT",
    "FUTURE_RELATIONSHIP_STATUS",
)


def _utc() -> str:
    return now_utc().isoformat()


def fact(
    value: Any = None,
    *,
    source: str = "UNKNOWN",
    date: str | None = None,
    confidence: str = "UNKNOWN",
    evidence: Any = "UNKNOWN",
    status: str = ST_UNKNOWN,
) -> dict[str, Any]:
    if value is None or value == "" or str(value).upper() in {"NONE", "NULL"}:
        value = "UNKNOWN"
    if evidence is None or evidence == "":
        evidence = "UNKNOWN"
    return {
        "value": value,
        "source": source or "UNKNOWN",
        "date": date or "UNKNOWN",
        "confidence": confidence or "UNKNOWN",
        "status": status or ST_UNKNOWN,
        "evidence": evidence,
    }


def _as_dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _unwrap(v: Any) -> Any:
    if isinstance(v, dict) and "value" in v:
        return v.get("value")
    return v


def _text_blob(row: dict[str, Any]) -> str:
    parts: list[str] = []
    for k in ("title", "description", "solicitation_text", "notice_text", "set_aside"):
        if row.get(k):
            parts.append(str(row.get(k)))
    for d in row.get("documents") or []:
        if isinstance(d, dict):
            for k in ("extracted_text", "text", "filename"):
                if d.get(k):
                    parts.append(str(d.get(k)))
    return "\n".join(parts)


def _id_field(key: str, *bags: dict[str, Any]) -> dict[str, Any]:
    for b in bags:
        if not isinstance(b, dict):
            continue
        f = b.get(key)
        if isinstance(f, dict) and _unwrap(f) not in (None, "", "UNKNOWN"):
            return fact(
                _unwrap(f),
                source=f.get("evidence_source") or f.get("source") or "product_identity",
                confidence=str(f.get("confidence") or "UNKNOWN"),
                evidence=f.get("evidence_snippet") or f.get("evidence") or "UNKNOWN",
                status=ST_DETECTED,
                date=str(f.get("date") or "UNKNOWN"),
            )
        if f not in (None, "", "UNKNOWN") and not isinstance(f, dict):
            return fact(f, source="pipeline", status=ST_DETECTED)
    return fact()


def _load_index(key: str) -> dict[str, Any]:
    try:
        from database import SessionLocal
        from models import AppSetting

        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == key).one_or_none()
            if row and row.value:
                data = json.loads(row.value)
                if isinstance(data, dict):
                    data.setdefault("by_key", {})
                    data.setdefault("entries", [])
                    return data
        finally:
            db.close()
    except Exception:
        pass
    return {"by_key": {}, "entries": []}


def _save_index(key: str, payload: dict[str, Any]) -> bool:
    try:
        from database import SessionLocal
        from models import AppSetting

        payload = dict(payload)
        payload["updated_at"] = _utc()
        payload["build"] = BUILD_TAG
        raw = json.dumps(payload, default=str)
        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == key).one_or_none()
            if row:
                row.value = raw
            else:
                db.add(AppSetting(key=key, value=raw))
            db.commit()
            return True
        finally:
            db.close()
    except Exception:
        return False


def _map_rel(raw: Any) -> str:
    s = str(raw or "").strip().upper().replace(" ", "_").replace("-", "_")
    aliases = {
        "OEM": REL_MANUFACTURER,
        "MANUFACTURER": REL_MANUFACTURER,
        "AUTHORIZED": REL_AUTHORIZED,
        "AUTHORIZED_DISTRIBUTOR": REL_AUTHORIZED,
        "DISTRIBUTOR": REL_DISTRIBUTOR,
        "RESELLER": REL_RESELLER,
        "BROKER": REL_BROKER,
        "SURPLUS": REL_SURPLUS,
        "SURPLUS_SOURCE": REL_SURPLUS,
    }
    return aliases.get(s, REL_UNKNOWN)


# ---------------------------------------------------------------------------
# BUILD 1 — Supplier Engagement Intelligence
# ---------------------------------------------------------------------------
def build_supplier_engagement_profile(row: dict[str, Any]) -> dict[str, Any]:
    """Prepare complete supplier conversation before contact — capable only with evidence."""
    struct = _as_dict(row.get("dla_product_structure"))
    fields = _as_dict(struct.get("fields"))
    pi = _as_dict(row.get("product_identity"))
    demand = _as_dict(row.get("demand_signal") or row.get("demand_signals"))
    awards = row.get("award_history") or demand.get("awards") or []
    agencies = demand.get("agencies") or row.get("agencies") or []
    if isinstance(agencies, str):
        agencies = [agencies]

    product = {
        "nsn": _id_field("nsn", fields, struct, pi, row),
        "part_number": _id_field("part_number", fields, struct, pi, row),
        "manufacturer": _id_field("oem", fields, struct, pi) if fields else _id_field("manufacturer", pi, struct, row),
        "cage": _id_field("cage", fields, struct, pi, row),
        "specifications": fact(
            _unwrap(fields.get("specification") or struct.get("specification") or row.get("specifications")),
            source="dla_product_structure" if struct else "UNKNOWN",
            status=ST_DETECTED if (struct.get("specification") or row.get("specifications")) else ST_UNKNOWN,
        ),
    }

    buyers = []
    for a in awards if isinstance(awards, list) else []:
        if not isinstance(a, dict):
            continue
        buyers.append(
            {
                "buyer": a.get("agency") or a.get("buyer") or "UNKNOWN",
                "award_id": a.get("award_id") or a.get("contract_number") or "UNKNOWN",
                "quantity": a.get("quantity") or "UNKNOWN",
                "evidence": fact(
                    a.get("award_id") or a.get("amount"),
                    source="award_history",
                    evidence="Historical award evidence — not a capability claim",
                    status=ST_DETECTED,
                ),
            }
        )

    questions = [
        {
            "question": q,
            "answer": fact(),
            "status": ST_UNKNOWN,
            "note": "Do not mark capable until evidence exists",
        }
        for q in SUPPLIER_QUESTIONS
    ]
    # Overlay stored answers if present
    stored = _as_dict(row.get("supplier_engagement"))
    for qa in questions:
        key = qa["question"]
        ans = _as_dict(stored.get("answers")).get(key)
        if isinstance(ans, dict) and _unwrap(ans) not in (None, "", "UNKNOWN"):
            qa["answer"] = fact(
                _unwrap(ans),
                source=ans.get("source") or "supplier_engagement",
                evidence=ans.get("evidence") or "UNKNOWN",
                status=str(ans.get("status") or ST_DETECTED),
                date=str(ans.get("date") or "UNKNOWN"),
                confidence=str(ans.get("confidence") or "UNKNOWN"),
            )
            qa["status"] = qa["answer"]["status"]

    capable = all(
        q["status"] in {ST_VERIFIED, ST_VALIDATED} and q["answer"]["value"] not in (None, "", "UNKNOWN")
        for q in questions
        if q["question"] in {"Can supplier provide exact item?", "New condition?"}
    )

    return {
        "kind": "M3SupplierEngagementProfile",
        "opportunity_id": row.get("canonical_id") or "UNKNOWN",
        "product": product,
        "demand_evidence": {
            "historical_buyers": buyers[:12] or [{"buyer": "UNKNOWN", "evidence": fact()}],
            "agencies": agencies if agencies else ["UNKNOWN"],
            "award_evidence": fact(
                len(buyers) if buyers else "UNKNOWN",
                source="award_history|demand_signal",
                evidence=f"{len(buyers)} award record(s)" if buyers else "UNKNOWN",
                status=ST_DETECTED if buyers else ST_UNKNOWN,
            ),
            "quantities": [b.get("quantity") for b in buyers if b.get("quantity") not in (None, "UNKNOWN")] or ["UNKNOWN"],
        },
        "supplier_questions": questions,
        "supplier_capable": False if not capable else True,
        "question": "What must we ask before contacting a supplier?",
        "note": "Do not mark supplier capable until evidence exists",
        "fabricated": False,
        "outreach_automated": False,
    }


# ---------------------------------------------------------------------------
# BUILD 2 — Supplier Commercial Terms Intelligence
# ---------------------------------------------------------------------------
def _term_state(value: Any, *, expiration: Any = None) -> str:
    if value in (None, "", "UNKNOWN"):
        return ST_UNKNOWN
    if expiration not in (None, "", "UNKNOWN"):
        try:
            # ISO date compare soft — if string looks past, mark expired without inventing
            from datetime import datetime, timezone

            exp = datetime.fromisoformat(str(expiration).replace("Z", "+00:00"))
            now = now_utc()
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if exp < now:
                return ST_EXPIRED
        except Exception:
            pass
    return ST_DETECTED


def build_supplier_commercial_terms(row: dict[str, Any]) -> dict[str, Any]:
    """Quote + terms + lead time with UNKNOWN/DETECTED/VERIFIED/EXPIRED — history preserved."""
    commit = _as_dict(row.get("supplier_commitment"))
    terms_in = _as_dict(row.get("supplier_commercial_terms"))
    by_supplier = _as_dict(terms_in.get("by_supplier") or commit.get("by_supplier"))
    spg = _as_dict(row.get("supplier_product_graph"))
    edges = [e for e in (spg.get("edges") or []) if isinstance(e, dict)]

    suppliers = []
    names = list(by_supplier.keys()) or [e.get("supplier_name") for e in edges[:8]] or ["UNKNOWN"]
    for name in names[:12]:
        name = str(name or "UNKNOWN")
        stored = _as_dict(by_supplier.get(name))
        quote = _as_dict(stored.get("quote"))
        terms = _as_dict(stored.get("terms"))
        lead = _as_dict(stored.get("lead_time") if isinstance(stored.get("lead_time"), dict) else {"stated": stored.get("lead_time")})

        price = quote.get("price") or stored.get("price")
        exp = quote.get("expiration") or stored.get("quote_expiration")
        q_state = str(quote.get("status") or _term_state(price, expiration=exp))
        if q_state == ST_DETECTED and quote.get("verified"):
            q_state = ST_VERIFIED

        entry = {
            "supplier_name": name,
            "quote": {
                "price": fact(price, source=quote.get("source") or "UNKNOWN", status=q_state, evidence=quote.get("evidence") or "UNKNOWN", date=str(quote.get("date") or "UNKNOWN")),
                "date": fact(quote.get("date") or stored.get("quote_date"), status=_term_state(quote.get("date") or stored.get("quote_date"))),
                "expiration": fact(exp, status=_term_state(exp) if exp else ST_UNKNOWN),
                "quantity": fact(quote.get("quantity") or stored.get("quantity")),
                "uom": fact(quote.get("uom") or stored.get("uom")),
                "status": q_state if q_state in {ST_UNKNOWN, ST_DETECTED, ST_VERIFIED, ST_EXPIRED} else ST_UNKNOWN,
            },
            "terms": {
                "net_terms": fact(terms.get("net_terms") or stored.get("payment_terms"), status=_term_state(terms.get("net_terms") or stored.get("payment_terms"))),
                "deposit_requirement": fact(terms.get("deposit_requirement") or stored.get("deposit")),
                "payment_timing": fact(terms.get("payment_timing")),
                "po_acceptance": fact(terms.get("po_acceptance") or stored.get("po_acceptance")),
                "shipping_terms": fact(terms.get("shipping_terms") or stored.get("shipping_terms")),
            },
            "lead_time": {
                "stated_lead_time": fact(lead.get("stated") or lead.get("value") or stored.get("lead_time")),
                "evidence_source": fact(lead.get("evidence_source") or "UNKNOWN"),
                "verification_date": fact(lead.get("verification_date")),
                "status": str(lead.get("status") or _term_state(lead.get("stated") or lead.get("value") or stored.get("lead_time"))),
            },
        }
        suppliers.append(entry)

    # Local history (never overwrite) — row-level + index
    history = list(_as_dict(terms_in.get("history")).get("entries") or [])
    idx = _load_index(SUPPLIER_TERMS_HISTORY_KEY)
    cid = str(row.get("canonical_id") or "")
    for e in idx.get("entries") or []:
        if isinstance(e, dict) and e.get("opportunity_id") == cid:
            history.append(e)

    return {
        "kind": "M3SupplierCommercialTerms",
        "opportunity_id": cid or "UNKNOWN",
        "suppliers": suppliers,
        "history": history[-50:],
        "overwrite_forbidden": True,
        "states": [ST_UNKNOWN, ST_DETECTED, ST_VERIFIED, ST_EXPIRED],
        "note": "Never overwrite old supplier information — append history only",
        "invents_terms": False,
    }


def record_supplier_commercial_terms_change(
    *,
    opportunity_id: str,
    supplier_name: str,
    field: str,
    previous_value: Any,
    new_value: Any,
    evidence: Any = "UNKNOWN",
    user: str = "operator",
    persist: bool = True,
) -> dict[str, Any]:
    entry = {
        "opportunity_id": opportunity_id,
        "supplier_name": supplier_name,
        "field": field,
        "previous_value": previous_value if previous_value is not None else "UNKNOWN",
        "new_value": new_value if new_value is not None else "UNKNOWN",
        "change_date": _utc(),
        "evidence": evidence if evidence not in (None, "") else "UNKNOWN",
        "user": user,
    }
    idx = _load_index(SUPPLIER_TERMS_HISTORY_KEY)
    entries = list(idx.get("entries") or [])
    entries.append(entry)
    idx["entries"] = entries[-500:]
    by = _as_dict(idx.get("by_key"))
    key = f"{opportunity_id}|{supplier_name}|{field}"
    by.setdefault(key, []).append(entry)
    by[key] = by[key][-40:]
    idx["by_key"] = by
    if persist:
        _save_index(SUPPLIER_TERMS_HISTORY_KEY, idx)
    return entry


# ---------------------------------------------------------------------------
# BUILD 3 — Supplier Relationship Memory
# ---------------------------------------------------------------------------
def record_supplier_relationship_event(
    payload: dict[str, Any],
    *,
    persist: bool = True,
) -> dict[str, Any]:
    stage = str(payload.get("stage") or "UNKNOWN").upper()
    if stage not in REL_MEMORY_STAGES and stage != "UNKNOWN":
        # allow freeform but flag
        pass
    entry = {
        "supplier_name": payload.get("supplier_name") or "UNKNOWN",
        "opportunity_id": payload.get("opportunity_id") or "UNKNOWN",
        "stage": stage if stage else "UNKNOWN",
        "date": payload.get("date") or _utc(),
        "evidence": payload.get("evidence") if payload.get("evidence") not in (None, "") else "UNKNOWN",
        "outcome": payload.get("outcome") or "UNKNOWN",
        "notes": payload.get("notes") or "UNKNOWN",
        "fabricated_score": False,
    }
    idx = _load_index(SUPPLIER_REL_MEMORY_KEY)
    entries = list(idx.get("entries") or [])
    entries.append(entry)
    idx["entries"] = entries[-500:]
    by = _as_dict(idx.get("by_key"))
    key = str(entry["supplier_name"])
    by.setdefault(key, []).append(entry)
    by[key] = by[key][-80:]
    idx["by_key"] = by
    if persist:
        _save_index(SUPPLIER_REL_MEMORY_KEY, idx)
    return entry


def build_supplier_relationship_memory(
    row: dict[str, Any] | None = None,
    *,
    supplier_name: str | None = None,
    limit: int = 40,
) -> dict[str, Any]:
    idx = _load_index(SUPPLIER_REL_MEMORY_KEY)
    entries = [e for e in (idx.get("entries") or []) if isinstance(e, dict)]
    local = []
    if isinstance(row, dict):
        local = [e for e in (_as_dict(row.get("supplier_relationship_memory")).get("entries") or []) if isinstance(e, dict)]
        cid = row.get("canonical_id")
        if cid:
            entries = [e for e in entries if e.get("opportunity_id") == cid] + local
        else:
            entries = entries + local
    if supplier_name:
        entries = [e for e in entries if e.get("supplier_name") == supplier_name]
    entries.sort(key=lambda e: str(e.get("date") or ""), reverse=True)
    return {
        "kind": "M3SupplierRelationshipMemory",
        "stages": list(REL_MEMORY_STAGES),
        "entries": entries[:limit],
        "append_only": True,
        "fake_scores_forbidden": True,
        "note": "Factual relationship history only — no supplier scores",
    }


# ---------------------------------------------------------------------------
# BUILD 4 — Financing Path Intelligence
# ---------------------------------------------------------------------------
def build_financing_path_intelligence(row: dict[str, Any]) -> dict[str, Any]:
    """Identified paths + missing requirements — never 'financeable'."""
    de = _as_dict(row.get("deal_economics"))
    profile = _as_dict(de.get("DEAL_ECONOMICS_PROFILE") or de)
    exec_i = _as_dict(row.get("execution_intelligence"))
    cash = _as_dict(row.get("cash_survival") or row.get("financing_profile"))
    funding = _as_dict(row.get("funding_requirement"))

    contract = {
        "value": fact(
            cash.get("contract_value") or profile.get("Contract_Value") or row.get("award_amount") or exec_i.get("Contract_Value"),
            source="deal_economics|pipeline",
            evidence="Contract value field — not a financing approval",
            status=ST_DETECTED if (profile.get("Contract_Value") or row.get("award_amount")) else ST_UNKNOWN,
        ),
        "payment_timing": fact(cash.get("payment_timing") or cash.get("invoice_timing")),
        "acceptance_timing": fact(cash.get("acceptance_timing")),
    }

    cash_req = {
        "supplier_payment": fact(
            cash.get("supplier_payment_required")
            or profile.get("Required_Acquisition_Cost")
            or exec_i.get("Estimated_Capital_Needed")
            or funding.get("capital_amount"),
            source="cash_survival|deal_economics|execution_intelligence",
            evidence="Mapped cash need — not an approval",
            status=ST_DETECTED if (profile.get("Required_Acquisition_Cost") or exec_i.get("Estimated_Capital_Needed")) else ST_UNKNOWN,
        ),
        "deposits": fact(cash.get("deposit") or cash.get("deposits")),
        "freight": fact(cash.get("shipping_cost") or cash.get("freight")),
        "inventory_requirement": fact(cash.get("inventory_requirement")),
    }

    stored_paths = cash.get("financing_paths") or _as_dict(row.get("financing_path_intelligence")).get("paths") or []
    by_name = {str(p.get("path") or p.get("name")): p for p in stored_paths if isinstance(p, dict)}

    paths = []
    for name in FINANCING_PATH_NAMES:
        p = _as_dict(by_name.get(name))
        paths.append(
            {
                "path": name,
                "requirements": p.get("requirements") or ["UNKNOWN"],
                "evidence": p.get("evidence") or fact(evidence="Path not evidenced"),
                "unknowns": p.get("unknowns") or p.get("missing_information") or ["requirements_not_evidenced"],
                "next_action": p.get("next_action") or "Research path requirements — do not assume available",
                "status": p.get("status") or ST_UNKNOWN,
            }
        )

    return {
        "kind": "M3FinancingPathIntelligence",
        "opportunity_id": row.get("canonical_id") or "UNKNOWN",
        "question": "What financing paths are identified, and what requirements are missing?",
        "contract": contract,
        "cash_requirements": cash_req,
        "paths": paths,
        "is_financeable_claim": False,
        "note": "Shows identified financing paths and missing requirements — never claims financeable",
        "fabricated": False,
    }


# ---------------------------------------------------------------------------
# BUILD 5 — Acquisition Path Intelligence
# ---------------------------------------------------------------------------
def build_acquisition_path_intelligence(row: dict[str, Any]) -> dict[str, Any]:
    blob = _text_blob(row)
    source = str(row.get("source") or row.get("source_system") or "").upper()
    stored = _as_dict(row.get("acquisition_path"))

    detected = stored.get("path")
    evidence_text = stored.get("evidence_text") or "UNKNOWN"
    if not detected or detected == "UNKNOWN":
        if "DIBBS" in source or "DLA" in source:
            detected = ACQ_DLA
            evidence_text = f"source={source}"
        else:
            for label, pat in _ACQ_PATTERNS:
                m = pat.search(blob)
                if m:
                    detected = label
                    evidence_text = m.group(0)
                    break
    if not detected:
        detected = ST_UNKNOWN

    access = _as_dict(stored.get("access"))
    return {
        "kind": "M3AcquisitionPathIntelligence",
        "opportunity_id": row.get("canonical_id") or "UNKNOWN",
        "path": fact(
            detected,
            source=stored.get("source") or ("pipeline.source" if source else "text_detection"),
            evidence=evidence_text,
            status=ST_DETECTED if detected not in (None, "", ST_UNKNOWN) else ST_UNKNOWN,
        ),
        "access_requirements": fact(access.get("requirements") or stored.get("access_requirements")),
        "company_eligibility": fact(access.get("company_eligibility") or stored.get("company_eligibility")),
        "required_registrations": fact(access.get("required_registrations") or stored.get("required_registrations")),
        "vehicle_restrictions": fact(access.get("vehicle_restrictions") or stored.get("vehicle_restrictions")),
        "vehicle_access_equals_opportunity_access": False,
        "note": "Never assume vehicle access = opportunity access",
        "known_paths": [ACQ_OPEN, ACQ_DLA, ACQ_GSA, ACQ_BPA, ACQ_IDIQ, ACQ_GWAC, ACQ_MAC, ACQ_OTHER],
    }


# ---------------------------------------------------------------------------
# BUILD 6 — Cyber Applicability Detector
# ---------------------------------------------------------------------------
def build_cyber_applicability(row: dict[str, Any]) -> dict[str, Any]:
    blob = _text_blob(row)
    stored = _as_dict(row.get("cyber_applicability"))
    findings = []
    for label, pat in _CYBER_PATTERNS:
        m = pat.search(blob)
        if m:
            findings.append(
                {
                    "requirement": label,
                    "source_clause": m.group(0),
                    "evidence_text": blob[max(0, m.start() - 40) : m.end() + 40].strip(),
                    "status": ST_DETECTED,
                    "next_action": "Research cyber applicability — detection is not validation",
                }
            )

    overall = ST_NOT_APPLICABLE
    if findings:
        overall = ST_DETECTED
    if stored.get("status") in {ST_VALIDATED, ST_BLOCKED, ST_RESEARCH, ST_NOT_APPLICABLE, ST_DETECTED}:
        # Explicit operator override only when stored
        if stored.get("status") == ST_VALIDATED and not findings:
            overall = ST_NOT_APPLICABLE  # don't invent validation without evidence
        elif stored.get("status") == ST_BLOCKED:
            overall = ST_BLOCKED
        elif stored.get("status") == ST_VALIDATED and findings:
            overall = ST_VALIDATED
        elif findings:
            overall = ST_RESEARCH if stored.get("status") == ST_RESEARCH else ST_DETECTED

    if findings and overall == ST_DETECTED:
        overall = ST_RESEARCH  # detection → research before claiming validated

    return {
        "kind": "M3CyberApplicability",
        "opportunity_id": row.get("canonical_id") or "UNKNOWN",
        "status": overall,
        "findings": findings,
        "states": [ST_NOT_APPLICABLE, ST_DETECTED, ST_RESEARCH, ST_VALIDATED, ST_BLOCKED],
        "note": "Read-only detector — not a full cybersecurity compliance engine",
        "full_compliance_engine": False,
        "fabricated": False,
    }


# ---------------------------------------------------------------------------
# BUILD 7 — Insurance / Bonding Applicability
# ---------------------------------------------------------------------------
def build_insurance_bonding_applicability(row: dict[str, Any]) -> dict[str, Any]:
    blob = _text_blob(row)
    stored = _as_dict(row.get("insurance_bonding"))
    findings = []
    for label, pat in _INSURANCE_PATTERNS:
        m = pat.search(blob)
        if m:
            findings.append(
                {
                    "detected_requirement": label,
                    "evidence": blob[max(0, m.start() - 40) : m.end() + 40].strip(),
                    "timing": stored.get("timing") or "UNKNOWN",
                    "action": "Research requirement — do not auto-reject",
                    "status": ST_DETECTED,
                }
            )

    if findings:
        status = ST_RESEARCH
    else:
        status = ST_UNKNOWN

    if stored.get("status") in {ST_VALIDATED, ST_RESEARCH, ST_DETECTED, ST_UNKNOWN}:
        if stored.get("status") == ST_VALIDATED and findings:
            status = ST_VALIDATED
        elif stored.get("status") == ST_RESEARCH:
            status = ST_RESEARCH

    return {
        "kind": "M3InsuranceBondingApplicability",
        "opportunity_id": row.get("canonical_id") or "UNKNOWN",
        "status": status,
        "findings": findings,
        "auto_reject": False,
        "states": [ST_UNKNOWN, ST_DETECTED, ST_RESEARCH, ST_VALIDATED],
        "note": "Detection only — do not reject automatically",
        "fabricated": False,
    }


# ---------------------------------------------------------------------------
# BUILD 8 — Supply Chain Path Graph
# ---------------------------------------------------------------------------
def build_supply_chain_path_graph(row: dict[str, Any]) -> dict[str, Any]:
    """Product→Manufacturer→Distributor→Reseller→M3→Government — no UNKNOWN promotion."""
    spg = _as_dict(row.get("supplier_product_graph"))
    pi = _as_dict(row.get("product_identity"))
    struct = _as_dict(row.get("dla_product_structure"))
    fields = _as_dict(struct.get("fields"))
    edges_in = [e for e in (spg.get("edges") or []) if isinstance(e, dict)]

    product_label = (
        _unwrap(fields.get("nsn"))
        or _unwrap(pi.get("nsn"))
        or _unwrap(fields.get("part_number"))
        or row.get("title")
        or "UNKNOWN"
    )

    nodes = [
        {"id": "PRODUCT", "label": str(product_label), "role": "PRODUCT"},
        {"id": "M3_COMPANY", "label": "M3 Company", "role": "M3_COMPANY"},
        {"id": "GOVERNMENT", "label": str(row.get("agency") or row.get("buyer") or "Government"), "role": "GOVERNMENT"},
    ]
    relationships = []

    mfr = None
    for e in edges_in:
        rel = _map_rel(e.get("relationship_type") or e.get("role"))
        conf = str(e.get("confidence") or ST_UNKNOWN).upper()
        # Do not promote UNKNOWN / POSSIBLE into validated
        if conf in {"VALIDATED", "HIGH", "MEDIUM", "LOW", "POSSIBLE", ST_UNKNOWN}:
            pass
        name = e.get("supplier_name") or "UNKNOWN"
        node_id = f"SUPPLIER:{name}"
        if not any(n["id"] == node_id for n in nodes):
            role = "MANUFACTURER" if rel == REL_MANUFACTURER else "DISTRIBUTOR" if rel in {REL_DISTRIBUTOR, REL_AUTHORIZED} else "RESELLER" if rel == REL_RESELLER else "BROKER" if rel == REL_BROKER else "SURPLUS" if rel == REL_SURPLUS else "UNKNOWN"
            nodes.append({"id": node_id, "label": name, "role": role})
        relationships.append(
            {
                "from": "PRODUCT",
                "to": node_id,
                "type": rel if rel in RELATIONSHIP_TYPES else REL_UNKNOWN,
                "evidence": e.get("evidence") or e.get("source") or "supplier_product_graph",
                "confidence": conf if conf != "VALIDATED" or rel != REL_UNKNOWN else ST_UNKNOWN,
                "timestamp": e.get("updated_at") or e.get("timestamp") or "UNKNOWN",
                "promoted_from_unknown": False,
            }
        )
        if rel == REL_MANUFACTURER and mfr is None:
            mfr = node_id

    # Path skeleton (evidence may be UNKNOWN)
    path = [
        {"step": "PRODUCT", "node": "PRODUCT"},
        {"step": "MANUFACTURER", "node": mfr or "UNKNOWN"},
        {"step": "DISTRIBUTOR", "node": "UNKNOWN"},
        {"step": "RESELLER", "node": "UNKNOWN"},
        {"step": "M3_COMPANY", "node": "M3_COMPANY"},
        {"step": "GOVERNMENT", "node": "GOVERNMENT"},
    ]
    # Fill distributor/reseller from edges when typed
    for e in relationships:
        if e["type"] in {REL_DISTRIBUTOR, REL_AUTHORIZED} and path[2]["node"] == "UNKNOWN":
            path[2]["node"] = e["to"]
        if e["type"] == REL_RESELLER and path[3]["node"] == "UNKNOWN":
            path[3]["node"] = e["to"]

    # Company → Government always present as planned path, evidence UNKNOWN unless award
    relationships.append(
        {
            "from": "M3_COMPANY",
            "to": "GOVERNMENT",
            "type": REL_RESELLER,
            "evidence": "planned_resale_path" if not row.get("award_number") else "award_fields",
            "confidence": ST_DETECTED if row.get("award_number") else ST_UNKNOWN,
            "timestamp": str(row.get("award_date") or "UNKNOWN"),
            "promoted_from_unknown": False,
        }
    )

    return {
        "kind": "M3SupplyChainPathGraph",
        "opportunity_id": row.get("canonical_id") or "UNKNOWN",
        "nodes": nodes,
        "relationships": relationships,
        "path": path,
        "relationship_types": list(RELATIONSHIP_TYPES),
        "note": "Do not promote UNKNOWN into validated relationships",
        "fabricated": False,
    }


# ---------------------------------------------------------------------------
# BUILD 9 — Execution Readiness Gate
# ---------------------------------------------------------------------------
def build_execution_readiness_gate(row: dict[str, Any]) -> dict[str, Any]:
    """Non-numeric pursue gate across demand/product/supplier/compliance/acq/fin/exec."""
    dimensions: list[dict[str, Any]] = []

    # 1 Demand
    demand = _as_dict(row.get("demand_signal") or row.get("demand_signals"))
    has_demand = bool(demand.get("agencies") or demand.get("awards") or row.get("award_history"))
    dimensions.append(
        {
            "name": "demand",
            "status": ST_DETECTED if has_demand else ST_UNKNOWN,
            "evidence": "demand_signal|award_history" if has_demand else "UNKNOWN",
        }
    )

    # 2 Product identity
    pi = _as_dict(row.get("product_identity"))
    struct = _as_dict(row.get("dla_product_structure"))
    fields = _as_dict(struct.get("fields"))
    has_id = any(
        _unwrap(fields.get(k) or pi.get(k) or struct.get(k)) not in (None, "", "UNKNOWN")
        for k in ("nsn", "part_number")
    )
    dimensions.append(
        {
            "name": "product_identity",
            "status": ST_DETECTED if has_id else ST_UNKNOWN,
            "evidence": "nsn|part_number" if has_id else "UNKNOWN",
        }
    )

    # 3 Supplier path
    eng = build_supplier_engagement_profile(row)
    chain = build_supply_chain_path_graph(row)
    has_supplier = any(
        r.get("type") != REL_UNKNOWN and r.get("to") not in ("GOVERNMENT", "M3_COMPANY")
        for r in (chain.get("relationships") or [])
    )
    dimensions.append(
        {
            "name": "supplier_path",
            "status": ST_DETECTED if has_supplier else ST_UNKNOWN,
            "evidence": "supply_chain_path_graph" if has_supplier else "UNKNOWN",
            "capable": eng.get("supplier_capable") is True,
        }
    )

    # 4 Compliance
    try:
        from m3_offer_readiness_read import build_offer_readiness_profile

        offer = build_offer_readiness_profile(row, include_category_scaffold=False)
        state = str(offer.get("readiness_state") or ST_UNKNOWN)
        if state == "BLOCKED":
            comp_st = ST_BLOCKED
        elif state == "READY":
            comp_st = ST_VALIDATED
        else:
            comp_st = ST_RESEARCH
    except Exception:
        comp_st = ST_UNKNOWN
        state = ST_UNKNOWN
    dimensions.append({"name": "compliance", "status": comp_st, "evidence": f"offer_readiness:{state}"})

    # 5 Acquisition path
    acq = build_acquisition_path_intelligence(row)
    acq_val = (acq.get("path") or {}).get("value")
    acq_elig = (acq.get("company_eligibility") or {}).get("value")
    if acq_elig not in (None, "", "UNKNOWN") and str(acq_elig).upper() in {"INELIGIBLE", "NO", "FALSE", "BLOCKED"}:
        acq_st = ST_BLOCKED
    elif acq_val not in (None, "", "UNKNOWN"):
        acq_st = ST_DETECTED
    else:
        acq_st = ST_UNKNOWN
    dimensions.append({"name": "acquisition_path", "status": acq_st, "evidence": acq_val or "UNKNOWN"})

    # 6 Financing path
    fin = build_financing_path_intelligence(row)
    fin_known = any(p.get("status") not in (None, ST_UNKNOWN) for p in (fin.get("paths") or []))
    dimensions.append(
        {
            "name": "financing_path",
            "status": ST_DETECTED if fin_known else ST_UNKNOWN,
            "evidence": "financing_path_intelligence",
            "is_financeable_claim": False,
        }
    )

    # 7 Execution path
    try:
        from m3_execution_os_read import build_award_execution_control

        exec_ctrl = build_award_execution_control(row)
        blocked_stages = [s for s in (exec_ctrl.get("stages") or []) if s.get("status") == "BLOCKED"]
        if blocked_stages:
            exec_st = ST_BLOCKED
        elif exec_ctrl.get("awarded_signal"):
            exec_st = ST_DETECTED
        else:
            exec_st = ST_UNKNOWN
    except Exception:
        exec_st = ST_UNKNOWN
    dimensions.append({"name": "execution_path", "status": exec_st, "evidence": "award_execution_control"})

    statuses = {d["name"]: d["status"] for d in dimensions}
    if ST_BLOCKED in statuses.values() or statuses.get("compliance") == ST_BLOCKED or statuses.get("acquisition_path") == ST_BLOCKED:
        decision = GATE_ACCESS_BLOCKED
    elif all(s in {ST_DETECTED, ST_VALIDATED, ST_VERIFIED} for s in statuses.values()) and eng.get("supplier_capable"):
        decision = GATE_PURSUE
    elif any(s == ST_UNKNOWN for s in statuses.values()) and not any(
        s == ST_BLOCKED for s in statuses.values()
    ):
        # Prefer RESEARCH when unknowns dominate; EXECUTION_UNKNOWN when identity+supplier missing hard
        if statuses.get("product_identity") == ST_UNKNOWN and statuses.get("supplier_path") == ST_UNKNOWN:
            decision = GATE_EXEC_UNKNOWN
        else:
            decision = GATE_RESEARCH
    else:
        decision = GATE_RESEARCH

    return {
        "kind": "M3ExecutionReadinessGate",
        "opportunity_id": row.get("canonical_id") or "UNKNOWN",
        "decision": decision,
        "dimensions": dimensions,
        "numeric_score": None,
        "questions": [
            "Can we find this?",
            "Can we verify this?",
            "Can we buy this?",
            "Can we finance this?",
            "Can we submit this?",
            "Can we execute this?",
            "Can we survive this?",
        ],
        "note": "Never create a numeric score — decision labels only",
        "fabricated": False,
    }


# ---------------------------------------------------------------------------
# Full profile + deal-room attach
# ---------------------------------------------------------------------------
def build_supplier_capital_ops_profile(row: dict[str, Any] | None) -> dict[str, Any]:
    row = row if isinstance(row, dict) else {}
    return {
        "kind": "M3SupplierCapitalOpsProfile",
        "build": BUILD_TAG,
        "opportunity_id": row.get("canonical_id") or "UNKNOWN",
        "supplier_engagement": build_supplier_engagement_profile(row),
        "supplier_commercial_terms": build_supplier_commercial_terms(row),
        "supplier_relationship_memory": build_supplier_relationship_memory(row),
        "financing_path": build_financing_path_intelligence(row),
        "acquisition_path": build_acquisition_path_intelligence(row),
        "cyber_applicability": build_cyber_applicability(row),
        "insurance_bonding": build_insurance_bonding_applicability(row),
        "supply_chain_path": build_supply_chain_path_graph(row),
        "execution_readiness_gate": build_execution_readiness_gate(row),
        "facts_only": True,
        "unknown_preserved": True,
        "no_fabricated_prices": True,
        "no_assumed_financing": True,
        "no_fake_supplier_scores": True,
        "engines_unchanged": True,
        "scoring_unchanged": True,
        "generated_at": _utc(),
        "read_only": True,
    }


def attach_supplier_capital_ops_to_deal_room(deal: dict[str, Any], *, row: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(deal, dict):
        return deal
    out = dict(deal)
    try:
        profile = build_supplier_capital_ops_profile(row or {"canonical_id": deal.get("canonical_id")})
        out["supplier_capital_ops"] = profile
        # Also surface gate on execution_os if present
        eos = out.get("execution_os")
        if isinstance(eos, dict):
            eos = dict(eos)
            eos["execution_readiness_gate"] = profile.get("execution_readiness_gate")
            eos["supplier_capital_ops_build"] = BUILD_TAG
            out["execution_os"] = eos
    except Exception:
        out["supplier_capital_ops"] = {
            "kind": "M3SupplierCapitalOpsProfile",
            "build": BUILD_TAG,
            "error": "supplier_capital_ops_unavailable",
            "read_only": True,
        }
    return out


def enrich_command_center_sections(
    sections: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    period: str,
) -> dict[str, Any]:
    """BUILD 10 — expand morning/evening with supplier/financing/compliance signals."""
    out = dict(sections or {})
    supplier_responses = list(out.get("supplier_responses") or [])
    quote_exp = list(out.get("quote_expiration") or out.get("expiring_information") or [])
    financing_actions = []
    compliance_changes = []
    solicitation_changes = list(out.get("amendments") or out.get("solicitation_changes") or [])
    execution_blockers = list(out.get("blockers") or out.get("unresolved_blockers") or [])
    supplier_followups = []
    next_day = list(out.get("next_priorities") or out.get("required_actions") or [])

    for r in rows[:60]:
        if not isinstance(r, dict):
            continue
        cid = r.get("canonical_id")
        title = (r.get("title") or "")[:80]
        terms = build_supplier_commercial_terms(r)
        for s in terms.get("suppliers") or []:
            q = _as_dict(s.get("quote"))
            if q.get("status") == ST_EXPIRED or (_as_dict(q.get("expiration")).get("value") not in (None, "", "UNKNOWN")):
                quote_exp.append(
                    {
                        "opportunity_id": cid,
                        "title": title,
                        "supplier": s.get("supplier_name"),
                        "expiration": _as_dict(q.get("expiration")).get("value"),
                        "status": q.get("status"),
                    }
                )
            if q.get("status") in {ST_DETECTED, ST_VERIFIED}:
                supplier_responses.append(
                    {
                        "opportunity_id": cid,
                        "title": title,
                        "supplier": s.get("supplier_name"),
                        "what": "quote",
                        "status": q.get("status"),
                    }
                )
            if _as_dict(s.get("terms")).get("po_acceptance", {}).get("value") == "UNKNOWN":
                supplier_followups.append(
                    {
                        "opportunity_id": cid,
                        "title": title,
                        "supplier": s.get("supplier_name"),
                        "action": "Follow up on PO acceptance / terms",
                    }
                )

        fin = build_financing_path_intelligence(r)
        for p in fin.get("paths") or []:
            if p.get("status") == ST_UNKNOWN:
                financing_actions.append(
                    {
                        "opportunity_id": cid,
                        "title": title,
                        "path": p.get("path"),
                        "next_action": p.get("next_action"),
                    }
                )

        cyber = build_cyber_applicability(r)
        if cyber.get("status") in {ST_DETECTED, ST_RESEARCH, ST_BLOCKED}:
            compliance_changes.append(
                {
                    "opportunity_id": cid,
                    "title": title,
                    "domain": "cyber",
                    "status": cyber.get("status"),
                    "findings": len(cyber.get("findings") or []),
                }
            )
        ins = build_insurance_bonding_applicability(r)
        if ins.get("findings"):
            compliance_changes.append(
                {
                    "opportunity_id": cid,
                    "title": title,
                    "domain": "insurance_bonding",
                    "status": ins.get("status"),
                    "findings": len(ins.get("findings") or []),
                }
            )

        gate = build_execution_readiness_gate(r)
        if gate.get("decision") in {GATE_ACCESS_BLOCKED, GATE_EXEC_UNKNOWN}:
            execution_blockers.append(
                {
                    "opportunity_id": cid,
                    "title": title,
                    "problem": f"Gate: {gate.get('decision')}",
                    "owner": "MANAGER",
                    "action": "Resolve readiness dimensions",
                    "status": "BLOCKED" if gate.get("decision") == GATE_ACCESS_BLOCKED else "UNKNOWN",
                }
            )

    if period == "morning":
        out["supplier_responses"] = supplier_responses[:15]
        out["quote_expiration"] = quote_exp[:15]
        out["financing_actions"] = financing_actions[:15]
        out["compliance_changes"] = compliance_changes[:15]
        out["solicitation_changes"] = solicitation_changes[:15]
        out["execution_blockers"] = execution_blockers[:15]
    else:
        out["supplier_followups"] = supplier_followups[:15]
        out["upcoming_deadlines"] = list(out.get("deadlines") or [])[:15]
        out["next_day_priorities"] = next_day[:15]
        out["quote_expiration"] = quote_exp[:15]
        out["unresolved_blockers"] = execution_blockers[:15]
        out.setdefault("completed_actions", out.get("completed_actions") or [])
    return out
