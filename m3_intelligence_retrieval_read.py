"""BUILD 20 — Intelligence Retrieval Architecture (read models).

M3 is NOT a database replacement. External systems remain authoritative.
Postgres stores metadata, relationships, decisions, workflows, lessons,
evidence references, cached results, and important historical state.

This layer answers: What information do I need? Where can I find it?
How should I retrieve it? What evidence supports it? What remains unknown?

Does NOT invent facts, create numeric scores, or warehouse external data.
Cache is not truth. UNKNOWN stays UNKNOWN.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from application_clock import now_utc

BUILD_TAG = "20260919-m3-intelligence-retrieval-1"

ST_UNKNOWN = "UNKNOWN"
ST_OPEN = "OPEN"
ST_IN_PROGRESS = "IN_PROGRESS"
ST_COMPLETE = "COMPLETE"
ST_FAILED = "FAILED"
ST_STALE = "STALE"

# Question types
Q_PRODUCT = "PRODUCT"
Q_SUPPLIER = "SUPPLIER"
Q_PRICE = "PRICE"
Q_CONTRACT = "CONTRACT"
Q_COMPLIANCE = "COMPLIANCE"
Q_EXECUTION = "EXECUTION"
Q_AGENCY = "AGENCY"
Q_MARKET = "MARKET"
Q_UNKNOWN = "UNKNOWN"

QUESTION_TYPES = (
    Q_PRODUCT,
    Q_SUPPLIER,
    Q_PRICE,
    Q_CONTRACT,
    Q_COMPLIANCE,
    Q_EXECUTION,
    Q_AGENCY,
    Q_MARKET,
    Q_UNKNOWN,
)

QUESTION_INDEX_KEY = "m3_research_questions_v1"
SESSION_INDEX_KEY = "m3_retrieval_sessions_v1"
MEMORY_INDEX_KEY = "m3_research_memory_v1"
CACHE_INDEX_KEY = "m3_retrieval_cache_v1"
GAP_INDEX_KEY = "m3_knowledge_gaps_v1"
PACKET_INDEX_KEY = "m3_evidence_packets_v1"

# Built-in source registry — metadata map, not a warehouse of content
DEFAULT_SOURCES: list[dict[str, Any]] = [
    {
        "source_name": "SAM.gov",
        "source_type": "government_solicitation",
        "authority": "EXTERNAL_AUTHORITATIVE",
        "data_domains": ["solicitations", "opportunities", "amendments", "notices"],
        "access_method": "API/web",
        "search_capability": "keyword, NAICS, notice type",
        "update_frequency": "continuous",
        "known_limitations": ["access controls", "document packaging varies"],
        "evidence_requirements": ["notice_id", "posted_date", "document_reference"],
    },
    {
        "source_name": "USAspending",
        "source_type": "government_award_history",
        "authority": "EXTERNAL_AUTHORITATIVE",
        "data_domains": ["awards", "historical_prices", "agencies", "contractors"],
        "access_method": "API",
        "search_capability": "award search by product/agency",
        "update_frequency": "periodic",
        "known_limitations": ["lag", "incomplete CLIN detail"],
        "evidence_requirements": ["award_id", "amount", "date"],
    },
    {
        "source_name": "DLA/DIBBS",
        "source_type": "dla_procurement",
        "authority": "EXTERNAL_AUTHORITATIVE",
        "data_domains": ["NSN", "DLA solicitations", "packaging", "inspection"],
        "access_method": "portal/API",
        "search_capability": "NSN, solicitation",
        "update_frequency": "continuous",
        "known_limitations": ["portal access mode", "auth may be required"],
        "evidence_requirements": ["NSN", "solicitation_number", "clause_text"],
    },
    {
        "source_name": "Manufacturer websites",
        "source_type": "commercial_product",
        "authority": "EXTERNAL_OBSERVATION",
        "data_domains": ["specifications", "part numbers", "datasheets"],
        "access_method": "web",
        "search_capability": "part number / model search",
        "update_frequency": "unknown",
        "known_limitations": ["may be outdated", "not government-certified"],
        "evidence_requirements": ["URL", "retrieved_date", "screenshot_or_excerpt"],
    },
    {
        "source_name": "Supplier catalogs",
        "source_type": "commercial_supplier",
        "authority": "EXTERNAL_OBSERVATION",
        "data_domains": ["availability", "list_price", "lead_time"],
        "access_method": "web/catalog/API",
        "search_capability": "SKU / part search",
        "update_frequency": "unknown",
        "known_limitations": ["list price ≠ quote", "stock not commitment"],
        "evidence_requirements": ["supplier_name", "quote_or_catalog_ref", "date"],
    },
    {
        "source_name": "Government documents",
        "source_type": "solicitation_document",
        "authority": "EXTERNAL_AUTHORITATIVE",
        "data_domains": ["requirements", "compliance", "packaging", "forms"],
        "access_method": "attachment download",
        "search_capability": "document extract / keyword",
        "update_frequency": "per solicitation",
        "known_limitations": ["OCR quality", "amendment supersession"],
        "evidence_requirements": ["filename", "page_or_clause", "extracted_text"],
    },
    {
        "source_name": "Internal history",
        "source_type": "m3_institutional_memory",
        "authority": "INTERNAL_MEMORY",
        "data_domains": ["lessons", "outcomes", "supplier_performance", "prior_research"],
        "access_method": "AppSetting/pipeline indexes",
        "search_capability": "intelligence search / learning indexes",
        "update_frequency": "as recorded",
        "known_limitations": ["incomplete if not recorded", "not a substitute for live sources"],
        "evidence_requirements": ["recorded_at", "evidence_ref"],
    },
    {
        "source_name": "User-provided evidence",
        "source_type": "operator_upload",
        "authority": "OPERATOR_EVIDENCE",
        "data_domains": ["quotes", "emails", "certificates", "confirmations"],
        "access_method": "upload/manual entry",
        "search_capability": "by opportunity / entity",
        "update_frequency": "manual",
        "known_limitations": ["must be dated and sourced"],
        "evidence_requirements": ["provider", "date", "file_or_note"],
    },
]

# Preferred / fallback routing (metadata only — does not fetch)
ROUTING_RULES: list[dict[str, Any]] = [
    {
        "question_type": Q_PRODUCT,
        "preferred_sources": ["DLA/DIBBS", "Manufacturer websites", "Government documents"],
        "fallback_sources": ["SAM.gov", "Internal history"],
        "limitations": ["Catalog listing ≠ approved source"],
    },
    {
        "question_type": Q_SUPPLIER,
        "preferred_sources": ["Supplier catalogs", "User-provided evidence", "Internal history"],
        "fallback_sources": ["Manufacturer websites", "USAspending"],
        "limitations": ["Catalog availability ≠ commitment"],
    },
    {
        "question_type": Q_PRICE,
        "preferred_sources": ["User-provided evidence", "USAspending", "Supplier catalogs"],
        "fallback_sources": ["Internal history"],
        "limitations": ["Historical award ≠ current quote; never invent margins"],
    },
    {
        "question_type": Q_CONTRACT,
        "preferred_sources": ["SAM.gov", "Government documents", "Internal history"],
        "fallback_sources": ["USAspending"],
        "limitations": ["Award status ≠ execution success"],
    },
    {
        "question_type": Q_COMPLIANCE,
        "preferred_sources": ["Government documents", "DLA/DIBBS", "SAM.gov"],
        "fallback_sources": ["User-provided evidence"],
        "limitations": ["Detection ≠ validation"],
    },
    {
        "question_type": Q_EXECUTION,
        "preferred_sources": ["User-provided evidence", "Internal history", "Government documents"],
        "fallback_sources": ["Supplier catalogs"],
        "limitations": ["Expectation ≠ receipt/payment"],
    },
    {
        "question_type": Q_AGENCY,
        "preferred_sources": ["USAspending", "SAM.gov", "Internal history"],
        "fallback_sources": ["Government documents"],
        "limitations": ["Agency pattern ≠ future award"],
    },
    {
        "question_type": Q_MARKET,
        "preferred_sources": ["USAspending", "SAM.gov", "Internal history"],
        "fallback_sources": ["DLA/DIBBS"],
        "limitations": ["Demand signals are evidence, not predictions"],
    },
]


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


def _known(v: Any) -> bool:
    return v not in (None, "", "UNKNOWN")


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


def _append(key: str, entry: dict[str, Any], *, by: str | None = None, cap: int = 600) -> dict[str, Any]:
    idx = _load_index(key)
    entries = list(idx.get("entries") or [])
    entries.append(entry)
    idx["entries"] = entries[-cap:]
    if by:
        bag = _as_dict(idx.get("by_key"))
        bag.setdefault(by, []).append(entry)
        bag[by] = bag[by][-80:]
        idx["by_key"] = bag
    _save_index(key, idx)
    return entry


def _query_key(query: str, source: str) -> str:
    raw = f"{source}|{query}".strip().lower()
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


# ---------------------------------------------------------------------------
# BUILD 1 — Intelligence Source Registry
# ---------------------------------------------------------------------------
def build_intelligence_source_registry(*, extras: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    sources = []
    for s in DEFAULT_SOURCES + list(extras or []):
        if not isinstance(s, dict):
            continue
        sources.append(
            {
                "kind": "M3IntelligenceSource",
                "source_name": s.get("source_name") or "UNKNOWN",
                "source_type": s.get("source_type") or "UNKNOWN",
                "authority": s.get("authority") or "UNKNOWN",
                "data_domains": s.get("data_domains") or ["UNKNOWN"],
                "access_method": s.get("access_method") or "UNKNOWN",
                "search_capability": s.get("search_capability") or "UNKNOWN",
                "update_frequency": s.get("update_frequency") or "UNKNOWN",
                "known_limitations": s.get("known_limitations") or ["UNKNOWN"],
                "evidence_requirements": s.get("evidence_requirements") or ["UNKNOWN"],
                "stores_full_external_data": False,
            }
        )
    return {
        "kind": "M3IntelligenceSourceRegistry",
        "sources": sources,
        "note": "Registry maps WHERE to look — external systems remain authoritative; M3 is not a warehouse",
        "not_a_database_replacement": True,
        "build": BUILD_TAG,
    }


# ---------------------------------------------------------------------------
# BUILD 2 — Question Routing
# ---------------------------------------------------------------------------
_TYPE_HINTS: list[tuple[str, re.Pattern[str]]] = [
    (Q_PRODUCT, re.compile(r"\b(?:nsn|part\s*number|product|specification|oem|manufacturer)\b", re.I)),
    (Q_SUPPLIER, re.compile(r"\b(?:supplier|distributor|source|vendor|quote|capability)\b", re.I)),
    (Q_PRICE, re.compile(r"\b(?:price|cost|pricing|margin|acquisition\s+cost)\b", re.I)),
    (Q_CONTRACT, re.compile(r"\b(?:contract|award|solicitation|clin|modification)\b", re.I)),
    (Q_COMPLIANCE, re.compile(r"\b(?:compliance|clause|cmmc|packaging|inspection|set[\s-]?aside|nmr|taa|baa)\b", re.I)),
    (Q_EXECUTION, re.compile(r"\b(?:delivery|acceptance|invoice|payment|shipment|execute|fulfill)\b", re.I)),
    (Q_AGENCY, re.compile(r"\b(?:agency|buyer|dla|gsa|buying\s+activity)\b", re.I)),
    (Q_MARKET, re.compile(r"\b(?:market|demand|historical|repeat\s+buy|recompete)\b", re.I)),
]


def classify_question_type(question: str) -> str:
    q = str(question or "")
    # Prefer more specific commercial/sourcing intents when overlapping with product wording
    if re.search(r"\b(?:supplier|distributor|source|vendor|quote|capability)\b", q, re.I):
        if re.search(r"\b(?:price|cost|pricing|margin)\b", q, re.I):
            return Q_PRICE
        return Q_SUPPLIER
    for qtype, pat in _TYPE_HINTS:
        if pat.search(q):
            return qtype
    return Q_UNKNOWN


def create_research_question(
    payload: dict[str, Any],
    *,
    persist: bool = True,
) -> dict[str, Any]:
    question = str(payload.get("question") or "UNKNOWN").strip() or "UNKNOWN"
    qtype = str(payload.get("question_type") or classify_question_type(question)).upper()
    if qtype not in QUESTION_TYPES:
        qtype = Q_UNKNOWN
    rule = next((r for r in ROUTING_RULES if r["question_type"] == qtype), None)
    entry = {
        "kind": "M3ResearchQuestion",
        "question_id": payload.get("question_id")
        or f"q:{hashlib.sha1(question.encode('utf-8')).hexdigest()[:10]}",
        "question": question,
        "question_type": qtype,
        "related_entities": payload.get("related_entities") or ["UNKNOWN"],
        "required_information": payload.get("required_information")
        or _default_required_info(qtype),
        "potential_sources": payload.get("potential_sources")
        or ((rule or {}).get("preferred_sources") or ["UNKNOWN"]),
        "status": payload.get("status") or ST_OPEN,
        "opportunity_id": payload.get("opportunity_id") or "UNKNOWN",
        "created_at": _utc(),
        "owner": payload.get("owner") or "RESEARCHER",
    }
    if persist:
        _append(QUESTION_INDEX_KEY, entry, by=str(entry["opportunity_id"]))
    return entry


def _default_required_info(qtype: str) -> list[str]:
    defaults = {
        Q_PRODUCT: ["identity", "specifications", "approved_source_status"],
        Q_SUPPLIER: ["capability_evidence", "quote", "lead_time", "terms"],
        Q_PRICE: ["supplier_quote", "quantity_context", "date"],
        Q_CONTRACT: ["solicitation_version", "award_status", "clauses"],
        Q_COMPLIANCE: ["applicable_clauses", "evidence_of_compliance_or_gap"],
        Q_EXECUTION: ["delivery_evidence", "acceptance_status", "payment_status"],
        Q_AGENCY: ["agency_name", "historical_purchase_evidence"],
        Q_MARKET: ["demand_signals", "historical_awards"],
        Q_UNKNOWN: ["UNKNOWN"],
    }
    return list(defaults.get(qtype, ["UNKNOWN"]))


# ---------------------------------------------------------------------------
# BUILD 3 — Retrieval Plan Generator
# ---------------------------------------------------------------------------
_PLAN_STEPS: dict[str, list[str]] = {
    Q_SUPPLIER: [
        "Identify manufacturer",
        "Find distributors",
        "Check supplier capability evidence",
        "Review historical purchases",
        "Identify unknowns",
    ],
    Q_PRODUCT: [
        "Confirm NSN / part number from solicitation",
        "Retrieve manufacturer documentation",
        "Check DLA/DIBBS product structure",
        "Identify approved-source requirements",
        "Identify unknowns",
    ],
    Q_PRICE: [
        "Collect supplier quote with quantity/date",
        "Review historical award prices with context",
        "Note configuration/date/quantity differences",
        "Identify unknowns — never invent margins",
    ],
    Q_CONTRACT: [
        "Locate current solicitation version",
        "Review amendments",
        "Map contract/award identifiers",
        "Identify unknowns",
    ],
    Q_COMPLIANCE: [
        "Extract applicable clauses from documents",
        "Map compliance gaps to evidence needs",
        "Identify unknowns",
    ],
    Q_EXECUTION: [
        "Review lifecycle stage evidence",
        "Check delivery/acceptance/payment records",
        "Identify missing execution evidence",
    ],
    Q_AGENCY: [
        "Identify agency/buyer",
        "Review historical awards via USAspending",
        "Identify unknowns",
    ],
    Q_MARKET: [
        "Review demand signals / award history",
        "Check internal research memory",
        "Identify unknowns — do not predict awards",
    ],
    Q_UNKNOWN: [
        "Clarify question type",
        "Identify required information",
        "Select candidate sources",
        "Identify unknowns",
    ],
}


def build_retrieval_plan(
    question: dict[str, Any] | str,
    *,
    row: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if isinstance(question, str):
        question = create_research_question({"question": question}, persist=False)
    qtype = str(question.get("question_type") or Q_UNKNOWN)
    rule = next((r for r in ROUTING_RULES if r["question_type"] == qtype), None) or {
        "preferred_sources": ["Internal history"],
        "fallback_sources": ["User-provided evidence"],
        "limitations": ["UNKNOWN"],
    }
    sources = list(rule.get("preferred_sources") or []) + [
        s for s in (rule.get("fallback_sources") or []) if s not in (rule.get("preferred_sources") or [])
    ]
    steps = _PLAN_STEPS.get(qtype, _PLAN_STEPS[Q_UNKNOWN])
    missing = []
    if isinstance(row, dict):
        # Surface known gaps from existing layers without inventing
        if qtype == Q_SUPPLIER and not _as_dict(row.get("supplier_product_graph")).get("edges"):
            missing.append("supplier_graph_empty")
        if qtype == Q_PRICE and not _as_dict(row.get("supplier_commercial_terms")).get("by_supplier"):
            missing.append("no_supplier_quote_evidenced")
        if qtype == Q_PRODUCT:
            pi = _as_dict(row.get("product_identity"))
            struct = _as_dict(row.get("dla_product_structure"))
            if not (pi.get("nsn") or _as_dict(struct.get("fields")).get("nsn") or struct.get("nsn")):
                missing.append("product_identity_unknown")

    return {
        "kind": "M3RetrievalPlan",
        "question": question.get("question"),
        "question_type": qtype,
        "sources_to_query": sources,
        "order_of_retrieval": [
            {"step": i + 1, "action": step, "source_hint": sources[min(i, len(sources) - 1)] if sources else "UNKNOWN"}
            for i, step in enumerate(steps)
        ],
        "required_evidence": question.get("required_information") or _default_required_info(qtype),
        "expected_outputs": ["evidence_references", "unknowns_list", "retrieval_trace"],
        "missing_information": missing or ["UNKNOWN"],
        "limitations": rule.get("limitations") or ["UNKNOWN"],
        "executes_external_fetch": False,
        "note": "Plan only — does not warehouse or blindly fetch; external sources remain authoritative",
    }


# ---------------------------------------------------------------------------
# BUILD 4 — Source Routing Rules
# ---------------------------------------------------------------------------
def build_source_routing_rules() -> dict[str, Any]:
    return {
        "kind": "M3SourceRoutingRuleSet",
        "rules": [
            {
                "kind": "M3SourceRoutingRule",
                **r,
            }
            for r in ROUTING_RULES
        ],
        "examples": {
            "product_specifications": "Manufacturer documentation",
            "historical_awards": "USAspending",
            "current_solicitations": "SAM.gov",
            "supplier_capability": "Supplier evidence / user-provided",
        },
        "not_a_warehouse": True,
    }


def route_sources_for_question_type(question_type: str) -> dict[str, Any]:
    qtype = str(question_type or Q_UNKNOWN).upper()
    rule = next((r for r in ROUTING_RULES if r["question_type"] == qtype), None)
    if not rule:
        return {
            "question_type": Q_UNKNOWN,
            "preferred_sources": ["Internal history", "User-provided evidence"],
            "fallback_sources": ["SAM.gov"],
            "limitations": ["Question type UNKNOWN — clarify before retrieval"],
        }
    return dict(rule)


# ---------------------------------------------------------------------------
# BUILD 5 — Retrieval Session
# ---------------------------------------------------------------------------
def start_retrieval_session(
    payload: dict[str, Any],
    *,
    persist: bool = True,
) -> dict[str, Any]:
    question = payload.get("question")
    if isinstance(question, str):
        qobj = create_research_question(
            {
                "question": question,
                "opportunity_id": payload.get("opportunity_id"),
                "related_entities": payload.get("related_entities"),
            },
            persist=False,
        )
    else:
        qobj = question if isinstance(question, dict) else create_research_question({"question": "UNKNOWN"}, persist=False)
    plan = build_retrieval_plan(qobj, row=payload.get("row"))
    entry = {
        "kind": "M3RetrievalSession",
        "session_id": payload.get("session_id") or f"rs:{hashlib.sha1((_utc() + qobj.get('question', '')).encode()).hexdigest()[:10]}",
        "question": qobj,
        "plan": plan,
        "sources_checked": payload.get("sources_checked") or [],
        "queries_performed": payload.get("queries_performed") or [],
        "results_found": payload.get("results_found") or [],
        "evidence_collected": payload.get("evidence_collected") or [],
        "unknowns_remaining": payload.get("unknowns_remaining") or plan.get("missing_information") or ["UNKNOWN"],
        "date": _utc(),
        "user_or_system": payload.get("user_or_system") or "system",
        "status": payload.get("status") or ST_IN_PROGRESS,
        "opportunity_id": payload.get("opportunity_id") or qobj.get("opportunity_id") or "UNKNOWN",
    }
    if persist:
        _append(SESSION_INDEX_KEY, entry, by=str(entry["opportunity_id"]))
    return entry


def update_retrieval_session(
    session_id: str,
    updates: dict[str, Any],
    *,
    persist: bool = True,
) -> dict[str, Any]:
    idx = _load_index(SESSION_INDEX_KEY)
    found = None
    for e in idx.get("entries") or []:
        if isinstance(e, dict) and e.get("session_id") == session_id:
            found = dict(e)
            break
    if not found:
        found = {
            "kind": "M3RetrievalSession",
            "session_id": session_id,
            "question": {"question": "UNKNOWN"},
            "sources_checked": [],
            "queries_performed": [],
            "results_found": [],
            "evidence_collected": [],
            "unknowns_remaining": ["UNKNOWN"],
            "date": _utc(),
            "user_or_system": "system",
            "status": ST_UNKNOWN,
        }
    for k in ("sources_checked", "queries_performed", "results_found", "evidence_collected", "unknowns_remaining"):
        if k in updates and isinstance(updates[k], list):
            found[k] = list(found.get(k) or []) + list(updates[k])
    if "status" in updates:
        found["status"] = updates["status"]
    found["updated_at"] = _utc()
    if persist:
        # replace in entries
        entries = [e for e in (idx.get("entries") or []) if not (isinstance(e, dict) and e.get("session_id") == session_id)]
        entries.append(found)
        idx["entries"] = entries[-400:]
        _save_index(SESSION_INDEX_KEY, idx)
    return found


# ---------------------------------------------------------------------------
# BUILD 6 — Evidence Packet
# ---------------------------------------------------------------------------
def build_evidence_packet(
    *,
    question: dict[str, Any] | str,
    row: dict[str, Any] | None = None,
    session: dict[str, Any] | None = None,
    persist: bool = False,
) -> dict[str, Any]:
    if isinstance(question, str):
        qobj = create_research_question({"question": question, "opportunity_id": (row or {}).get("canonical_id")}, persist=False)
    else:
        qobj = question
    row = row if isinstance(row, dict) else {}
    plan = build_retrieval_plan(qobj, row=row)

    relevant_facts = []
    documents = []
    relationships = []
    conflicts = []
    unknowns = list(plan.get("missing_information") or [])

    # Assemble from existing intelligence — references only, not warehouse dump
    if row.get("canonical_id"):
        relevant_facts.append(fact(row.get("canonical_id"), source="pipeline", evidence="opportunity id"))
    if row.get("title"):
        relevant_facts.append(fact(row.get("title"), source="pipeline", evidence="title"))
    pi = _as_dict(row.get("product_identity"))
    struct = _as_dict(row.get("dla_product_structure"))
    fields = _as_dict(struct.get("fields"))
    for key in ("nsn", "part_number"):
        val = fields.get(key) if isinstance(fields.get(key), dict) else fields.get(key) or pi.get(key)
        if isinstance(val, dict):
            val = val.get("value")
        if _known(val):
            relevant_facts.append(fact(val, source="product_identity|dla", evidence=key, status="OBSERVED"))
        else:
            unknowns.append(f"{key}_unknown")

    for e in _as_dict(row.get("supplier_product_graph")).get("edges") or []:
        if isinstance(e, dict):
            relationships.append(
                {
                    "type": e.get("relationship_type") or "UNKNOWN",
                    "supplier": e.get("supplier_name") or "UNKNOWN",
                    "evidence": e.get("source") or "supplier_product_graph",
                }
            )

    for d in row.get("documents") or []:
        if isinstance(d, dict):
            documents.append(
                {
                    "filename": d.get("filename") or "UNKNOWN",
                    "document_type": d.get("document_type") or "UNKNOWN",
                    "evidence_ref": d.get("filename") or "documents",
                }
            )

    if session:
        for ev in session.get("evidence_collected") or []:
            relevant_facts.append(fact(ev if not isinstance(ev, dict) else ev.get("value"), source="retrieval_session", evidence=ev))
        unknowns = list(session.get("unknowns_remaining") or unknowns)

    # Dedupe unknowns
    seen = set()
    unk = []
    for u in unknowns:
        if u in seen:
            continue
        seen.add(u)
        unk.append(u)

    packet = {
        "kind": "M3EvidencePacket",
        "question": qobj.get("question"),
        "question_type": qobj.get("question_type"),
        "relevant_facts": relevant_facts[:40],
        "sources": plan.get("sources_to_query") or [],
        "documents": documents[:20],
        "relationships": relationships[:20],
        "unknowns": unk or ["UNKNOWN"],
        "conflicts": conflicts,
        "retrieval_plan": plan,
        "ai_input_ready": True,
        "contains_assumptions": False,
        "note": "Evidence package for AI/human review — not an assumption set",
        "created_at": _utc(),
        "opportunity_id": row.get("canonical_id") or qobj.get("opportunity_id") or "UNKNOWN",
    }
    if persist:
        _append(PACKET_INDEX_KEY, packet, by=str(packet["opportunity_id"]))
    return packet


# ---------------------------------------------------------------------------
# BUILD 7 — Retrieval Trace / Explanation
# ---------------------------------------------------------------------------
def build_retrieval_trace(
    *,
    question: dict[str, Any] | str,
    sources_selected: list[str] | None = None,
    information_returned: list[Any] | None = None,
    row: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if isinstance(question, str):
        qobj = create_research_question({"question": question}, persist=False)
    else:
        qobj = question
    plan = build_retrieval_plan(qobj, row=row)
    selected = sources_selected or list(plan.get("sources_to_query") or [])[:3]
    steps = []
    for src in selected:
        rule = route_sources_for_question_type(qobj.get("question_type"))
        reason = "preferred" if src in (rule.get("preferred_sources") or []) else "fallback"
        steps.append(
            {
                "source_selected": src,
                "reason_selected": f"{reason} for question_type={qobj.get('question_type')}",
                "information_returned": "UNKNOWN" if not information_returned else information_returned,
                "limitations": (rule.get("limitations") or ["UNKNOWN"]),
            }
        )
    return {
        "kind": "M3RetrievalTrace",
        "question": qobj.get("question"),
        "steps": steps,
        "where_from": selected or ["UNKNOWN"],
        "why_source_used": [s.get("reason_selected") for s in steps] or ["UNKNOWN"],
        "what_not_found": plan.get("missing_information") or ["UNKNOWN"],
        "what_remains_unknown": plan.get("missing_information") or ["UNKNOWN"],
        "external_sources_authoritative": True,
        "cache_is_not_truth": True,
    }


# ---------------------------------------------------------------------------
# BUILD 8 — Knowledge Gap Detection
# ---------------------------------------------------------------------------
def detect_knowledge_gaps(
    row: dict[str, Any] | None = None,
    *,
    question: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row = row if isinstance(row, dict) else {}
    gaps = []

    def _gap(missing: str, impact: str, sources: list[str], action: str, owner: str = "RESEARCHER") -> None:
        gaps.append(
            {
                "kind": "M3KnowledgeGap",
                "missing_information": missing,
                "impact": impact,
                "possible_sources": sources,
                "research_action": action,
                "owner": owner,
                "status": ST_OPEN,
                "opportunity_id": row.get("canonical_id") or "UNKNOWN",
            }
        )

    # Product identified?
    struct = _as_dict(row.get("dla_product_structure"))
    fields = _as_dict(struct.get("fields"))
    pi = _as_dict(row.get("product_identity"))
    has_product = any(
        _known((fields.get(k) or {}).get("value") if isinstance(fields.get(k), dict) else fields.get(k) or pi.get(k))
        for k in ("nsn", "part_number")
    )
    if has_product:
        edges = _as_dict(row.get("supplier_product_graph")).get("edges") or []
        if not edges:
            _gap(
                "current_supplier_availability",
                "Cannot confirm sourcing path",
                ["Supplier catalogs", "Manufacturer websites", "User-provided evidence"],
                "Research distributors / request capability evidence",
            )
        else:
            # supplier present ≠ availability
            _gap(
                "supplier_commitment_evidence",
                "Graph presence does not prove quote or PO acceptance",
                ["User-provided evidence", "Supplier catalogs"],
                "Obtain quote + terms with evidence",
            )
    else:
        _gap(
            "product_identity",
            "Blocks supplier and pricing research",
            ["DLA/DIBBS", "Government documents", "SAM.gov"],
            "Extract NSN / part number from solicitation documents",
            "MANAGER",
        )

    if question:
        plan = build_retrieval_plan(question, row=row)
        for m in plan.get("missing_information") or []:
            if m != "UNKNOWN":
                _gap(
                    str(m),
                    "Blocks answering the research question",
                    list(plan.get("sources_to_query") or [])[:3],
                    f"Retrieve via plan for {plan.get('question_type')}",
                )

    return {
        "kind": "M3KnowledgeGapSet",
        "gaps": gaps[:20],
        "example": {
            "known": "Product identified" if has_product else "UNKNOWN",
            "unknown": "Current supplier availability" if has_product else "Product identity",
            "action": "Research distributors" if has_product else "Extract product identity",
        },
    }


def record_knowledge_gap(payload: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
    entry = {
        "kind": "M3KnowledgeGap",
        "missing_information": payload.get("missing_information") or "UNKNOWN",
        "impact": payload.get("impact") or "UNKNOWN",
        "possible_sources": payload.get("possible_sources") or ["UNKNOWN"],
        "research_action": payload.get("research_action") or "UNKNOWN",
        "owner": payload.get("owner") or "RESEARCHER",
        "status": payload.get("status") or ST_OPEN,
        "opportunity_id": payload.get("opportunity_id") or "UNKNOWN",
        "recorded_at": _utc(),
    }
    if persist:
        _append(GAP_INDEX_KEY, entry, by=str(entry["opportunity_id"]))
    return entry


# ---------------------------------------------------------------------------
# BUILD 9 — Research Memory
# ---------------------------------------------------------------------------
def record_research_memory(payload: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
    entry = {
        "kind": "M3ResearchMemory",
        "topic": payload.get("topic") or "UNKNOWN",
        "sources_checked": payload.get("sources_checked") or ["UNKNOWN"],
        "previous_findings": payload.get("previous_findings") or ["UNKNOWN"],
        "date_researched": payload.get("date_researched") or _utc(),
        "remaining_unknowns": payload.get("remaining_unknowns") or ["UNKNOWN"],
        "related_entities": payload.get("related_entities") or ["UNKNOWN"],
        "opportunity_id": payload.get("opportunity_id") or "UNKNOWN",
        "evidence": payload.get("evidence") if payload.get("evidence") not in (None, "") else "UNKNOWN",
        "note": "Avoid repeating expensive research — memory is reference, not live truth",
    }
    if persist:
        _append(MEMORY_INDEX_KEY, entry, by=str(entry["topic"]))
    return entry


def build_research_memory(*, topic: str | None = None, opportunity_id: str | None = None, limit: int = 30) -> dict[str, Any]:
    entries = [e for e in (_load_index(MEMORY_INDEX_KEY).get("entries") or []) if isinstance(e, dict)]
    if topic:
        t = topic.lower()
        entries = [e for e in entries if t in str(e.get("topic") or "").lower()]
    if opportunity_id:
        entries = [e for e in entries if e.get("opportunity_id") == opportunity_id]
    return {
        "kind": "M3ResearchMemorySet",
        "entries": entries[-limit:],
        "avoids_duplicate_expensive_research": True,
    }


# ---------------------------------------------------------------------------
# BUILD 10 — Retrieval Cache
# ---------------------------------------------------------------------------
def put_retrieval_cache(
    payload: dict[str, Any],
    *,
    persist: bool = True,
) -> dict[str, Any]:
    query = str(payload.get("query") or "UNKNOWN")
    source = str(payload.get("source") or "UNKNOWN")
    entry = {
        "kind": "M3RetrievalCache",
        "cache_key": _query_key(query, source),
        "query": query,
        "results": payload.get("results") if payload.get("results") is not None else "UNKNOWN",
        "source": source,
        "timestamp": _utc(),
        "expiration": payload.get("expiration") or "UNKNOWN",
        "validation_status": payload.get("validation_status") or "CACHED_OBSERVATION",
        "is_truth": False,
        "original_source_authoritative": True,
        "note": "Cache is not truth — original source remains authoritative",
    }
    if persist:
        idx = _load_index(CACHE_INDEX_KEY)
        by = _as_dict(idx.get("by_key"))
        by[entry["cache_key"]] = entry
        idx["by_key"] = by
        entries = list(idx.get("entries") or [])
        entries.append(entry)
        idx["entries"] = entries[-400:]
        _save_index(CACHE_INDEX_KEY, idx)
    return entry


def get_retrieval_cache(query: str, source: str) -> dict[str, Any] | None:
    key = _query_key(query, source)
    by = _as_dict(_load_index(CACHE_INDEX_KEY).get("by_key"))
    entry = by.get(key)
    if not isinstance(entry, dict):
        return None
    # Soft expiration check — if expiration ISO is past, mark stale but do not invent
    exp = entry.get("expiration")
    if _known(exp) and exp != "UNKNOWN":
        try:
            from datetime import datetime, timezone

            exp_dt = datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
            now = now_utc()
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=timezone.utc)
            if exp_dt < now:
                out = dict(entry)
                out["validation_status"] = ST_STALE
                out["expired"] = True
                out["note"] = "Cache expired — re-query authoritative source; cache is not truth"
                return out
        except Exception:
            pass
    out = dict(entry)
    out["expired"] = False
    out["is_truth"] = False
    return out


# ---------------------------------------------------------------------------
# BUILD 11 — Multi-source Answer Assembly
# ---------------------------------------------------------------------------
def assemble_multi_source_answer(
    *,
    question: str | dict[str, Any],
    evidence_streams: list[dict[str, Any]] | None = None,
    row: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if isinstance(question, str):
        qobj = create_research_question({"question": question}, persist=False)
    else:
        qobj = question
    streams = list(evidence_streams or [])
    # Auto-collect light streams from row as references
    row = row if isinstance(row, dict) else {}
    if row.get("supplier_product_graph"):
        streams.append(
            {
                "stream": "supplier_graph",
                "claim": "Supplier relationship observed on graph",
                "evidence": "supplier_product_graph",
                "assumption": False,
            }
        )
    if row.get("award_history") or _as_dict(row.get("demand_signal")).get("awards"):
        streams.append(
            {
                "stream": "historical_award",
                "claim": "Government purchased similar item historically",
                "evidence": "award_history|demand_signal",
                "assumption": False,
            }
        )
    packet = build_evidence_packet(question=qobj, row=row, persist=False)
    return {
        "kind": "M3AnswerAssembly",
        "question": qobj.get("question"),
        "evidence_streams": streams,
        "evidence_package": packet,
        "assembled_as": "evidence_package",
        "assembled_as_assumption": False,
        "note": "Assembles evidence streams — never converts observations into assumptions",
        "example_shape": {
            "supplier_website": "Product exists (observation)",
            "historical_award": "Government purchased similar item (evidence)",
            "supplier_communication": "Quote available (if evidenced)",
            "m3_output": "Evidence package — not assumption",
        },
    }


# ---------------------------------------------------------------------------
# BUILD 12 — AI Context Handoff
# ---------------------------------------------------------------------------
def build_ai_context_handoff(
    *,
    user_question: str,
    row: dict[str, Any] | None = None,
    persist_session: bool = False,
) -> dict[str, Any]:
    """User question → plan → evidence packet → AI analysis input → decision trace hook.

    AI should not search blindly — receives evidence context only.
    """
    qobj = create_research_question(
        {"question": user_question, "opportunity_id": (row or {}).get("canonical_id")},
        persist=False,
    )
    plan = build_retrieval_plan(qobj, row=row)
    session = start_retrieval_session(
        {"question": qobj, "opportunity_id": (row or {}).get("canonical_id"), "row": row, "user_or_system": "ai_handoff"},
        persist=persist_session,
    )
    packet = build_evidence_packet(question=qobj, row=row, session=session, persist=False)
    trace = build_retrieval_trace(question=qobj, row=row)
    gaps = detect_knowledge_gaps(row, question=qobj)

    decision_trace = None
    try:
        from m3_economic_learning_read import build_decision_trace

        decision_trace = build_decision_trace(
            question=user_question,
            row=row or {},
            persist=False,
        )
    except Exception:
        decision_trace = {
            "kind": "M3DecisionTrace",
            "question": user_question,
            "unknowns": packet.get("unknowns"),
            "generated_action": "Resolve knowledge gaps via retrieval plan",
        }

    return {
        "kind": "M3AIContextHandoff",
        "flow": [
            "User question",
            "Retrieval plan",
            "Evidence packet",
            "AI analysis (evidence context only)",
            "Decision trace",
        ],
        "user_question": user_question,
        "research_question": qobj,
        "retrieval_plan": plan,
        "retrieval_session": {"session_id": session.get("session_id"), "status": session.get("status")},
        "evidence_packet": packet,
        "retrieval_trace": trace,
        "knowledge_gaps": gaps,
        "decision_trace": decision_trace,
        "ai_should_search_blindly": False,
        "ai_receives_evidence_context_only": True,
        "invents_facts": False,
        "not_a_database_replacement": True,
        "build": BUILD_TAG,
    }


# ---------------------------------------------------------------------------
# Opportunity profile + deal-room attach
# ---------------------------------------------------------------------------
def build_intelligence_retrieval_profile(row: dict[str, Any] | None) -> dict[str, Any]:
    row = row if isinstance(row, dict) else {}
    # Seed a default navigator question from opportunity context
    default_q = "Can we source this product?" if row.get("canonical_id") else "What information do we need?"
    qobj = create_research_question(
        {"question": default_q, "opportunity_id": row.get("canonical_id"), "question_type": Q_SUPPLIER},
        persist=False,
    )
    return {
        "kind": "M3IntelligenceRetrievalProfile",
        "build": BUILD_TAG,
        "opportunity_id": row.get("canonical_id") or "UNKNOWN",
        "source_registry": build_intelligence_source_registry(),
        "routing_rules": build_source_routing_rules(),
        "default_question": qobj,
        "retrieval_plan": build_retrieval_plan(qobj, row=row),
        "knowledge_gaps": detect_knowledge_gaps(row, question=qobj),
        "evidence_packet": build_evidence_packet(question=qobj, row=row, persist=False),
        "retrieval_trace": build_retrieval_trace(question=qobj, row=row),
        "research_memory": build_research_memory(opportunity_id=str(row.get("canonical_id") or ""), limit=10),
        "questions": [
            "What information do I need?",
            "Where can I find it?",
            "How should I retrieve it?",
            "What evidence supports it?",
            "What remains unknown?",
        ],
        "navigator_questions": [
            "What question are we answering?",
            "What sources contain the answer?",
            "What evidence is available?",
            "What information is missing?",
            "How do we get the remaining answer?",
        ],
        "not_a_database_replacement": True,
        "not_a_data_warehouse": True,
        "external_sources_authoritative": True,
        "cache_is_not_truth": True,
        "facts_only": True,
        "unknown_preserved": True,
        "no_numeric_scores": True,
        "engines_unchanged": True,
        "scoring_unchanged": True,
        "generated_at": _utc(),
        "read_only": True,
    }


def attach_retrieval_to_deal_room(deal: dict[str, Any], *, row: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(deal, dict):
        return deal
    out = dict(deal)
    try:
        out["intelligence_retrieval"] = build_intelligence_retrieval_profile(
            row or {"canonical_id": deal.get("canonical_id")}
        )
    except Exception:
        out["intelligence_retrieval"] = {
            "kind": "M3IntelligenceRetrievalProfile",
            "build": BUILD_TAG,
            "error": "intelligence_retrieval_unavailable",
            "read_only": True,
        }
    return out


# ---------------------------------------------------------------------------
# BUILD 13 — Command Center
# ---------------------------------------------------------------------------
def enrich_command_center_retrieval(
    sections: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    period: str,
) -> dict[str, Any]:
    out = dict(sections or {})
    unanswered = []
    failed = []
    missing_ev = []
    stale = []
    completed = []
    new_sources = []
    gaps_created = []
    gaps_resolved = []

    for e in (_load_index(QUESTION_INDEX_KEY).get("entries") or [])[-40:]:
        if isinstance(e, dict) and e.get("status") in {ST_OPEN, ST_IN_PROGRESS, ST_UNKNOWN}:
            unanswered.append(e)

    for s in (_load_index(SESSION_INDEX_KEY).get("entries") or [])[-40:]:
        if not isinstance(s, dict):
            continue
        if s.get("status") == ST_FAILED:
            failed.append(s)
        if s.get("status") == ST_COMPLETE:
            completed.append(s)

    for c in (_as_dict(_load_index(CACHE_INDEX_KEY).get("by_key"))).values():
        if isinstance(c, dict) and (c.get("validation_status") == ST_STALE or c.get("expired")):
            stale.append(c)

    for g in (_load_index(GAP_INDEX_KEY).get("entries") or [])[-40:]:
        if not isinstance(g, dict):
            continue
        if g.get("status") == ST_OPEN:
            gaps_created.append(g)
        elif g.get("status") == ST_COMPLETE:
            gaps_resolved.append(g)

    for r in rows[:40]:
        if not isinstance(r, dict):
            continue
        gaps = detect_knowledge_gaps(r)
        for g in gaps.get("gaps") or []:
            missing_ev.append(
                {
                    "opportunity_id": r.get("canonical_id"),
                    "title": (r.get("title") or "")[:80],
                    "missing": g.get("missing_information"),
                    "action": g.get("research_action"),
                    "owner": g.get("owner"),
                }
            )

    # Registry itself is "discovered" static metadata — surface once as available sources
    new_sources = [{"source_name": s["source_name"], "authority": s["authority"]} for s in DEFAULT_SOURCES]

    if period == "morning":
        out["unanswered_research_questions"] = unanswered[:15]
        out["failed_retrievals"] = failed[:15]
        out["missing_evidence"] = (list(out.get("missing_evidence") or []) + missing_ev)[:15]
        out["stale_information"] = stale[:15]
    else:
        out["completed_research"] = completed[:15]
        out["new_sources_discovered"] = new_sources[:15]
        out["knowledge_gaps_created"] = gaps_created[:15] or missing_ev[:10]
        out["knowledge_gaps_resolved"] = gaps_resolved[:15]
    return out
