"""BUILD 22 — Strategic Intelligence Layer (read models).

Converts accumulated operational intelligence into strategic understanding.

Canonical stack position:
… → EXECUTION INTELLIGENCE → STRATEGIC INTELLIGENCE → DECISION SUPPORT
  → ACTION ORCHESTRATION → EXECUTION → LEARNING MEMORY

NOT a prediction engine. No scores, rankings, forecasts, automated strategy,
profitability assumptions, or opportunity ratings.

UNKNOWN stays UNKNOWN. Evidence mandatory. External sources authoritative.
AI assists organization; does not replace human judgment.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from application_clock import now_utc

BUILD_TAG = "20260919-m3-strategic-intelligence-1"

ST_OBSERVED = "OBSERVED"
ST_UNDER_REVIEW = "UNDER_REVIEW"
ST_DOCUMENTED = "DOCUMENTED"
ST_HISTORICAL = "HISTORICAL"
ST_UNKNOWN = "UNKNOWN"

STRATEGIC_STATES = (
    ST_OBSERVED,
    ST_UNDER_REVIEW,
    ST_DOCUMENTED,
    ST_HISTORICAL,
    ST_UNKNOWN,
)

BRIEF_MARKET = "Market Brief"
BRIEF_SUPPLIER = "Supplier Ecosystem Brief"
BRIEF_CAPABILITY = "Capability Brief"
BRIEF_PRODUCT = "Product Family Brief"
BRIEF_TYPES = (BRIEF_MARKET, BRIEF_SUPPLIER, BRIEF_CAPABILITY, BRIEF_PRODUCT)

MS_OPEN = "OPEN"
MS_IN_PROGRESS = "IN_PROGRESS"
MS_COMPLETE = "COMPLETE"
MS_BLOCKED = "BLOCKED"
MS_UNKNOWN = "UNKNOWN"

SI_INDEX = "m3_strategic_intelligence_v1"
MARKET_INDEX = "m3_market_evolution_v1"
CAP_INDEX = "m3_capabilities_v1"
GAP_INDEX = "m3_capability_gaps_v1"
REL_INDEX = "m3_strategic_relationships_v1"
MEM_INDEX = "m3_strategic_memory_v1"
MISSION_INDEX = "m3_strategic_missions_v1"
OBS_INDEX = "m3_trend_observations_v1"
BRIEF_INDEX = "m3_strategic_briefs_v1"
TRACE_INDEX = "m3_strategic_decision_traces_v1"


def _utc() -> str:
    return now_utc().isoformat()


def _as_dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _known(v: Any) -> bool:
    return v not in (None, "", "UNKNOWN")


def _sid(prefix: str, seed: str) -> str:
    return f"{prefix}:{hashlib.sha1(seed.encode('utf-8')).hexdigest()[:12]}"


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


def _upsert(index_key: str, id_field: str, record: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
    idx = _load_index(index_key)
    by = _as_dict(idx.get("by_key"))
    rid = record[id_field]
    by[rid] = record
    idx["by_key"] = by
    entries = [e for e in (idx.get("entries") or []) if isinstance(e, dict) and e.get(id_field) != rid]
    entries.append(record)
    idx["entries"] = entries[-600:]
    if persist:
        _save_index(index_key, idx)
    return record


def _list_entries(index_key: str, *, limit: int = 50) -> list[dict[str, Any]]:
    entries = [e for e in (_load_index(index_key).get("entries") or []) if isinstance(e, dict)]
    entries.sort(key=lambda e: str(e.get("updated_at") or e.get("created_at") or ""), reverse=True)
    return entries[:limit]


def _get(index_key: str, rid: str) -> dict[str, Any] | None:
    by = _as_dict(_load_index(index_key).get("by_key"))
    r = by.get(rid)
    return r if isinstance(r, dict) else None


# ---------------------------------------------------------------------------
# 1 Strategic Intelligence Record
# ---------------------------------------------------------------------------
def create_strategic_intelligence(payload: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
    topic = str(payload.get("strategic_topic") or payload.get("topic") or "").strip()
    if not topic:
        raise ValueError("strategic_topic required")
    evidence = payload.get("supporting_evidence") or payload.get("evidence")
    if not _known(evidence) and not (isinstance(evidence, list) and evidence):
        raise ValueError("supporting_evidence required — no strategic claim without evidence")

    status = str(payload.get("status") or ST_OBSERVED).upper()
    if status not in STRATEGIC_STATES:
        status = ST_OBSERVED

    now = _utc()
    sid = payload.get("strategic_id") or _sid("si", f"{topic}|{now}")
    record = {
        "kind": "M3StrategicIntelligence",
        "strategic_id": sid,
        "strategic_topic": topic,
        "related_markets": payload.get("related_markets") or ["UNKNOWN"],
        "related_products": payload.get("related_products") or ["UNKNOWN"],
        "related_suppliers": payload.get("related_suppliers") or ["UNKNOWN"],
        "related_agencies": payload.get("related_agencies") or ["UNKNOWN"],
        "supporting_evidence": evidence if isinstance(evidence, list) else [evidence],
        "observed_patterns": payload.get("observed_patterns") or [],
        "unknowns": payload.get("unknowns") if payload.get("unknowns") is not None else ["UNKNOWN"],
        "research_opportunities": payload.get("research_opportunities") or [],
        "related_decisions": payload.get("related_decisions") or [],
        "status": status,
        "created_at": payload.get("created_at") or now,
        "updated_at": now,
        "numeric_score": None,
        "ranking": None,
        "prediction": None,
        "fabricated": False,
        "build": BUILD_TAG,
    }
    return _upsert(SI_INDEX, "strategic_id", record, persist=persist)


def get_strategic_intelligence(strategic_id: str) -> dict[str, Any] | None:
    return _get(SI_INDEX, strategic_id)


def list_strategic_topics(*, limit: int = 50) -> dict[str, Any]:
    entries = _list_entries(SI_INDEX, limit=limit)
    return {
        "kind": "M3StrategicTopicList",
        "topics": [
            {
                "strategic_id": e.get("strategic_id"),
                "strategic_topic": e.get("strategic_topic"),
                "status": e.get("status"),
                "unknowns": e.get("unknowns"),
            }
            for e in entries
        ],
        "no_scores": True,
        "no_rankings": True,
    }


# ---------------------------------------------------------------------------
# 2 Market Evolution + 9 Trend Observation
# ---------------------------------------------------------------------------
def create_trend_observation(payload: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
    observation = payload.get("observation")
    evidence = payload.get("evidence")
    if not _known(observation):
        raise ValueError("observation required")
    if not _known(evidence) and not (isinstance(evidence, list) and evidence):
        raise ValueError("evidence required")

    text = str(observation).lower()
    if "market is expanding" in text or "market is declining" in text:
        raise ValueError('Do NOT convert into "The market is expanding." — observation only')

    now = _utc()
    oid = payload.get("observation_id") or _sid("obs", f"{observation}|{now}")
    record = {
        "kind": "M3TrendObservation",
        "observation_id": oid,
        "observation": observation,
        "evidence": evidence if isinstance(evidence, list) else [evidence],
        "timeframe": payload.get("timeframe") or "UNKNOWN",
        "related_entities": payload.get("related_entities") or ["UNKNOWN"],
        "limitations": payload.get("limitations")
        if payload.get("limitations") is not None
        else ["observation only — not a forecast or ranking"],
        "category": payload.get("category") or "UNKNOWN",
        "not_a_forecast": True,
        "created_at": now,
        "updated_at": now,
        "build": BUILD_TAG,
    }
    return _upsert(OBS_INDEX, "observation_id", record, persist=persist)


def create_market_observation(payload: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
    category = str(payload.get("category") or "").strip()
    observation = payload.get("observation") or payload.get("observed_changes")
    evidence = payload.get("evidence")
    if not category:
        raise ValueError("category required")
    if not _known(observation):
        raise ValueError("observation/observed_changes required")
    if not _known(evidence) and not (isinstance(evidence, list) and evidence):
        raise ValueError("evidence required — do not infer growth/decline without evidence")

    note = str(observation).lower()
    if any(x in note for x in ("market is expanding", "market is declining", "will grow", "forecast")):
        raise ValueError("do not convert observations into growth/decline forecasts")

    now = _utc()
    mid = payload.get("market_id") or _sid("mkt", f"{category}|{observation}|{now}")
    record = {
        "kind": "M3MarketEvolution",
        "market_id": mid,
        "category": category,
        "historical_observations": payload.get("historical_observations") or [],
        "observed_changes": observation if isinstance(observation, list) else [observation],
        "affected_products": payload.get("affected_products") or ["UNKNOWN"],
        "affected_suppliers": payload.get("affected_suppliers") or ["UNKNOWN"],
        "evidence": evidence if isinstance(evidence, list) else [evidence],
        "unknowns": payload.get("unknowns") if payload.get("unknowns") is not None else ["UNKNOWN"],
        "status": str(payload.get("status") or ST_OBSERVED).upper()
        if str(payload.get("status") or ST_OBSERVED).upper() in STRATEGIC_STATES
        else ST_OBSERVED,
        "created_at": now,
        "updated_at": now,
        "no_growth_inference": True,
        "build": BUILD_TAG,
    }
    create_trend_observation(
        {
            "observation": observation if not isinstance(observation, list) else observation[0],
            "evidence": evidence,
            "timeframe": payload.get("timeframe") or "UNKNOWN",
            "related_entities": list(payload.get("affected_products") or [])
            + list(payload.get("affected_suppliers") or []),
            "limitations": payload.get("limitations") or ["observation only — not a forecast"],
            "category": category,
        },
        persist=persist,
    )
    return _upsert(MARKET_INDEX, "market_id", record, persist=persist)


# ---------------------------------------------------------------------------
# 3 Capability Intelligence
# ---------------------------------------------------------------------------
def create_capability(payload: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
    capability = str(payload.get("capability") or "").strip()
    evidence = payload.get("evidence")
    if not capability:
        raise ValueError("capability required")
    if not _known(evidence) and not (isinstance(evidence, list) and evidence):
        raise ValueError("evidence required — do not declare capabilities without evidence")

    now = _utc()
    cid = payload.get("capability_id") or _sid("cap", f"{capability}|{now}")
    record = {
        "kind": "M3Capability",
        "capability_id": cid,
        "capability": capability,
        "evidence": evidence if isinstance(evidence, list) else [evidence],
        "originating_activities": payload.get("originating_activities") or [],
        "related_opportunities": payload.get("related_opportunities") or [],
        "related_suppliers": payload.get("related_suppliers") or [],
        "limitations": payload.get("limitations") if payload.get("limitations") is not None else ["UNKNOWN"],
        "unknowns": payload.get("unknowns") if payload.get("unknowns") is not None else ["UNKNOWN"],
        "status": str(payload.get("status") or ST_DOCUMENTED).upper()
        if str(payload.get("status") or ST_DOCUMENTED).upper() in STRATEGIC_STATES
        else ST_DOCUMENTED,
        "created_at": now,
        "updated_at": now,
        "declared_without_evidence": False,
        "build": BUILD_TAG,
    }
    return _upsert(CAP_INDEX, "capability_id", record, persist=persist)


def list_capabilities(*, limit: int = 50) -> dict[str, Any]:
    return {
        "kind": "M3CapabilityList",
        "capabilities": _list_entries(CAP_INDEX, limit=limit),
        "no_scores": True,
    }


# ---------------------------------------------------------------------------
# 4 Capability Gap Intelligence
# ---------------------------------------------------------------------------
def create_capability_gap(payload: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
    missing = str(payload.get("missing_capability") or payload.get("gap") or "").strip()
    why = payload.get("why_it_matters") or payload.get("why")
    if not missing:
        raise ValueError("missing_capability required")
    if not _known(why):
        raise ValueError("why_it_matters required")

    now = _utc()
    gid = payload.get("gap_id") or _sid("gap", f"{missing}|{now}")
    ev = payload.get("evidence")
    if not _known(ev) and not (isinstance(ev, list) and ev):
        ev = ["UNKNOWN"]
    record = {
        "kind": "M3CapabilityGap",
        "gap_id": gid,
        "missing_capability": missing,
        "why_it_matters": why,
        "related_opportunities": payload.get("related_opportunities") or [],
        "evidence": ev if isinstance(ev, list) else [ev],
        "required_research": payload.get("required_research") or [],
        "possible_actions": payload.get("possible_actions") or [],
        "status": ST_OBSERVED,
        "created_at": now,
        "updated_at": now,
        "example": {
            "gap": "Limited manufacturer relationships",
            "related_action": "Research manufacturer channels",
        },
        "build": BUILD_TAG,
    }
    return _upsert(GAP_INDEX, "gap_id", record, persist=persist)


def list_capability_gaps(*, limit: int = 50) -> dict[str, Any]:
    return {
        "kind": "M3CapabilityGapList",
        "gaps": _list_entries(GAP_INDEX, limit=limit),
        "no_scores": True,
    }


# ---------------------------------------------------------------------------
# 5 Strategic Relationship Intelligence
# ---------------------------------------------------------------------------
def create_strategic_relationship(payload: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
    a = payload.get("entity_a") or payload.get("from_entity")
    b = payload.get("entity_b") or payload.get("to_entity")
    rtype = payload.get("relationship_type") or payload.get("type")
    evidence = payload.get("evidence")
    if not _known(a) or not _known(b):
        raise ValueError("entity_a and entity_b required")
    if not _known(rtype):
        raise ValueError("relationship_type required")
    if not _known(evidence) and not (isinstance(evidence, list) and evidence):
        raise ValueError("evidence required — no relationship assumptions")

    now = _utc()
    rid = payload.get("relationship_id") or _sid("srel", f"{a}|{rtype}|{b}|{now}")
    record = {
        "kind": "M3StrategicRelationship",
        "relationship_id": rid,
        "entity_a": a,
        "entity_b": b,
        "relationship_type": rtype,
        "evidence": evidence if isinstance(evidence, list) else [evidence],
        "history": payload.get("history") or [],
        "observations": payload.get("observations") or [],
        "uses_master_record_relationships": True,
        "no_assumptions": True,
        "created_at": now,
        "updated_at": now,
        "build": BUILD_TAG,
    }
    return _upsert(REL_INDEX, "relationship_id", record, persist=persist)


def list_strategic_relationships(*, limit: int = 50) -> list[dict[str, Any]]:
    return _list_entries(REL_INDEX, limit=limit)


# ---------------------------------------------------------------------------
# 6 Opportunity Ecosystem View
# ---------------------------------------------------------------------------
def build_opportunity_ecosystem_view(row: dict[str, Any] | None) -> dict[str, Any]:
    row = row if isinstance(row, dict) else {}
    cid = row.get("canonical_id") or "UNKNOWN"
    title = row.get("title") or "UNKNOWN"

    products: list[dict[str, Any]] = []
    manufacturers: list[dict[str, Any]] = []
    suppliers: list[dict[str, Any]] = []
    agencies: list[dict[str, Any]] = []
    contracts: list[dict[str, Any]] = []
    lessons: list[dict[str, Any]] = []

    dla = _as_dict(row.get("dla_product_structure"))
    fields = _as_dict(dla.get("fields"))
    nsn = _as_dict(fields.get("nsn")).get("value")
    if _known(nsn):
        products.append({"id": nsn, "type": "NSN", "evidence": "dla_product_structure"})
    else:
        products.append({"id": "UNKNOWN", "type": "product", "evidence": "UNKNOWN"})

    spg = _as_dict(row.get("supplier_product_graph"))
    for edge in (spg.get("edges") or [])[:8]:
        if not isinstance(edge, dict):
            continue
        name = edge.get("supplier_name") or edge.get("company")
        role = edge.get("role") or "supplier"
        if _known(name):
            if "manufacturer" in str(role).lower():
                manufacturers.append({"name": name, "evidence": "supplier_product_graph"})
            else:
                suppliers.append({"name": name, "role": role, "evidence": "supplier_product_graph"})

    if not manufacturers:
        manufacturers.append({"name": "UNKNOWN", "evidence": "UNKNOWN"})
    if not suppliers:
        suppliers.append({"name": "UNKNOWN", "evidence": "UNKNOWN"})

    buyer = row.get("buyer") or row.get("agency") or _as_dict(row.get("opportunity")).get("buyer")
    if _known(buyer):
        agencies.append({"name": buyer, "evidence": "opportunity_row"})
    else:
        agencies.append({"name": "UNKNOWN", "evidence": "UNKNOWN"})

    award = row.get("award_id") or row.get("contract_number")
    if _known(award):
        contracts.append({"id": award, "evidence": "opportunity_row"})
    else:
        contracts.append({"id": "UNKNOWN", "evidence": "UNKNOWN"})

    for m in _list_entries(MEM_INDEX, limit=20):
        if cid != "UNKNOWN" and cid in (m.get("related_opportunities") or []):
            lessons.append({"lesson": m.get("lessons"), "question": m.get("strategic_question")})

    return {
        "kind": "M3OpportunityEcosystemView",
        "opportunity": {"id": cid, "title": title},
        "chain": [
            "Opportunity",
            "Product family",
            "Manufacturers",
            "Suppliers",
            "Agencies",
            "Contracts",
            "Lessons",
        ],
        "product_family": products,
        "manufacturers": manufacturers,
        "suppliers": suppliers,
        "agencies": agencies,
        "contracts": contracts,
        "lessons": lessons or [{"lesson": "UNKNOWN", "note": "no linked strategic memory yet"}],
        "relationships": list_strategic_relationships(limit=10),
        "connected_not_isolated": True,
        "no_invented_links": True,
        "unknown_preserved": True,
        "build": BUILD_TAG,
    }


# ---------------------------------------------------------------------------
# 7 Strategic Memory
# ---------------------------------------------------------------------------
def create_strategic_memory(payload: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
    question = str(payload.get("strategic_question") or payload.get("question") or "").strip()
    if not question:
        raise ValueError("strategic_question required")

    now = _utc()
    mid = payload.get("memory_id") or _sid("smem", f"{question}|{now}")
    record = {
        "kind": "M3StrategicMemory",
        "memory_id": mid,
        "strategic_question": question,
        "evidence_reviewed": payload.get("evidence_reviewed") or payload.get("evidence") or [],
        "known_information": payload.get("known_information") or payload.get("known") or [],
        "unknown_information": payload.get("unknown_information")
        if payload.get("unknown_information") is not None
        else (payload.get("unknowns") if payload.get("unknowns") is not None else ["UNKNOWN"]),
        "actions_taken": payload.get("actions_taken") or [],
        "outcome": payload.get("outcome") if _known(payload.get("outcome")) else "UNKNOWN",
        "lessons": payload.get("lessons") if _known(payload.get("lessons")) else "UNKNOWN",
        "future_applicability": payload.get("future_applicability") or ["UNKNOWN"],
        "related_opportunities": payload.get("related_opportunities") or [],
        "created_at": now,
        "updated_at": now,
        "build": BUILD_TAG,
    }
    for k in ("evidence_reviewed", "known_information", "unknown_information"):
        if isinstance(record[k], str):
            record[k] = [record[k]]
    return _upsert(MEM_INDEX, "memory_id", record, persist=persist)


# ---------------------------------------------------------------------------
# 8 Strategic Research Missions
# ---------------------------------------------------------------------------
def create_strategic_research_mission(payload: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
    objective = str(payload.get("objective") or "").strip()
    trigger = payload.get("trigger")
    if not objective:
        raise ValueError("objective required")
    if not _known(trigger):
        raise ValueError("trigger required — missions come from intelligence gaps")

    now = _utc()
    mid = payload.get("mission_id") or _sid("srm", f"{objective}|{now}")
    status = str(payload.get("status") or MS_OPEN).upper()
    if status not in {MS_OPEN, MS_IN_PROGRESS, MS_COMPLETE, MS_BLOCKED, MS_UNKNOWN}:
        status = MS_OPEN

    record = {
        "kind": "M3StrategicResearchMission",
        "mission_id": mid,
        "objective": objective,
        "trigger": trigger,
        "related_intelligence": payload.get("related_intelligence") or [],
        "required_evidence": payload.get("required_evidence") or [],
        "status": status,
        "results": payload.get("results") if _known(payload.get("results")) else "UNKNOWN",
        "created_at": now,
        "updated_at": now,
        "automated_decision": False,
        "build": BUILD_TAG,
    }
    return _upsert(MISSION_INDEX, "mission_id", record, persist=persist)


def list_research_missions(*, limit: int = 50) -> list[dict[str, Any]]:
    return _list_entries(MISSION_INDEX, limit=limit)


# ---------------------------------------------------------------------------
# 10 Strategic Brief Generation
# ---------------------------------------------------------------------------
def generate_strategic_brief(payload: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
    brief_type = payload.get("brief_type") or payload.get("type") or BRIEF_MARKET
    if brief_type not in BRIEF_TYPES:
        mapping = {
            "market": BRIEF_MARKET,
            "supplier": BRIEF_SUPPLIER,
            "supplier_ecosystem": BRIEF_SUPPLIER,
            "capability": BRIEF_CAPABILITY,
            "product": BRIEF_PRODUCT,
            "product_family": BRIEF_PRODUCT,
        }
        brief_type = mapping.get(str(brief_type).lower(), BRIEF_MARKET)

    subject = str(payload.get("subject") or payload.get("title") or "UNKNOWN").strip()
    now = _utc()
    bid = payload.get("brief_id") or _sid("brief", f"{brief_type}|{subject}|{now}")

    known = payload.get("known_facts") or payload.get("known") or []
    evidence = payload.get("evidence") or []
    relationships = payload.get("relationships") or []
    unknowns = payload.get("unknowns") if payload.get("unknowns") is not None else ["UNKNOWN"]
    research = payload.get("research_opportunities") or []

    oid = payload.get("opportunity_id")
    if oid and not relationships:
        eco = build_opportunity_ecosystem_view({"canonical_id": oid, "title": subject})
        relationships = [
            f"{r.get('entity_a')} —{r.get('relationship_type')}→ {r.get('entity_b')}"
            for r in (eco.get("relationships") or [])[:5]
        ]
        if not known:
            known = [f"opportunity:{oid}", f"suppliers_listed:{len(eco.get('suppliers') or [])}"]

    if isinstance(known, str):
        known = [known]
    if isinstance(evidence, str):
        evidence = [evidence]
    if isinstance(unknowns, str):
        unknowns = [unknowns]

    record = {
        "kind": "M3StrategicBrief",
        "brief_id": bid,
        "brief_type": brief_type,
        "subject": subject,
        "known_facts": known or ["UNKNOWN"],
        "evidence": evidence or ["UNKNOWN"],
        "relationships": relationships or ["UNKNOWN"],
        "unknowns": unknowns,
        "research_opportunities": research or [],
        "no_scores": True,
        "no_predictions": True,
        "no_rankings": True,
        "created_at": now,
        "updated_at": now,
        "build": BUILD_TAG,
    }
    return _upsert(BRIEF_INDEX, "brief_id", record, persist=persist)


def get_strategic_brief(brief_id: str) -> dict[str, Any] | None:
    return _get(BRIEF_INDEX, brief_id)


# ---------------------------------------------------------------------------
# 11 Strategic Decision Trace
# ---------------------------------------------------------------------------
def create_strategic_decision_trace(payload: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
    question = payload.get("question") or payload.get("strategic_question")
    if not _known(question):
        raise ValueError("question required")

    now = _utc()
    tid = payload.get("trace_id") or _sid("sdt", f"{question}|{now}")
    record = {
        "kind": "M3StrategicDecisionTrace",
        "trace_id": tid,
        "question": question,
        "evidence": payload.get("evidence") if payload.get("evidence") is not None else ["UNKNOWN"],
        "known": payload.get("known") if payload.get("known") is not None else ["UNKNOWN"],
        "unknown": payload.get("unknown") if payload.get("unknown") is not None else ["UNKNOWN"],
        "action": payload.get("action") if _known(payload.get("action")) else "UNKNOWN",
        "outcome": payload.get("outcome") if _known(payload.get("outcome")) else "UNKNOWN",
        "flow": ["Question", "Evidence", "Known", "Unknown", "Action", "Outcome"],
        "automated_decision": False,
        "human_judgment_required": True,
        "created_at": now,
        "updated_at": now,
        "build": BUILD_TAG,
    }
    for k in ("evidence", "known", "unknown"):
        if isinstance(record[k], str):
            record[k] = [record[k]]
    return _upsert(TRACE_INDEX, "trace_id", record, persist=persist)


# ---------------------------------------------------------------------------
# Profile + deal room + command center
# ---------------------------------------------------------------------------
def build_strategic_intelligence_profile(row: dict[str, Any] | None) -> dict[str, Any]:
    row = row if isinstance(row, dict) else {}
    cid = row.get("canonical_id")
    return {
        "kind": "M3StrategicIntelligenceProfile",
        "build": BUILD_TAG,
        "opportunity_id": cid or "UNKNOWN",
        "ecosystem": build_opportunity_ecosystem_view(row),
        "strategic_topics": list_strategic_topics(limit=10),
        "capabilities": list_capabilities(limit=10),
        "capability_gaps": list_capability_gaps(limit=10),
        "research_missions": list_research_missions(limit=10),
        "relationships": list_strategic_relationships(limit=10),
        "states": list(STRATEGIC_STATES),
        "brief_types": list(BRIEF_TYPES),
        "decision_trace_flow": ["Question", "Evidence", "Known", "Unknown", "Action", "Outcome"],
        "architecture_flow": [
            "EXTERNAL WORLD",
            "CONNECTORS / SOURCES",
            "RETRIEVAL INTELLIGENCE",
            "MASTER RECORD GOVERNANCE",
            "KNOWLEDGE GRAPH",
            "PRODUCT INTELLIGENCE",
            "SUPPLIER INTELLIGENCE",
            "BUYER INTELLIGENCE",
            "ACQUISITION INTELLIGENCE",
            "ECONOMIC INTELLIGENCE",
            "EXECUTION INTELLIGENCE",
            "STRATEGIC INTELLIGENCE",
            "DECISION SUPPORT",
            "ACTION ORCHESTRATION",
            "EXECUTION",
            "LEARNING MEMORY",
        ],
        "not_a_prediction_engine": True,
        "not_a_data_warehouse": True,
        "no_scores": True,
        "no_rankings": True,
        "no_forecasts": True,
        "no_automated_strategy": True,
        "external_sources_authoritative": True,
        "unknown_preserved": True,
        "ai_does_not_replace_human_judgment": True,
        "engines_unchanged": True,
        "scoring_unchanged": True,
        "generated_at": _utc(),
        "read_only": True,
    }


def attach_strategic_intelligence_to_deal_room(
    deal: dict[str, Any], *, row: dict[str, Any] | None = None
) -> dict[str, Any]:
    if not isinstance(deal, dict):
        return deal
    out = dict(deal)
    try:
        out["strategic_intelligence"] = build_strategic_intelligence_profile(
            row or {"canonical_id": deal.get("canonical_id")}
        )
    except Exception:
        out["strategic_intelligence"] = {
            "kind": "M3StrategicIntelligenceProfile",
            "build": BUILD_TAG,
            "error": "strategic_intelligence_unavailable",
            "read_only": True,
        }
    return out


def enrich_command_center_strategic(
    sections: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    period: str,
) -> dict[str, Any]:
    out = dict(sections or {})
    missions = list_research_missions(limit=20)
    observations = _list_entries(OBS_INDEX, limit=20)
    gaps = _list_entries(GAP_INDEX, limit=20)
    relationships = list_strategic_relationships(limit=20)
    memories = _list_entries(MEM_INDEX, limit=20)
    topics = _list_entries(SI_INDEX, limit=20)

    if period == "morning":
        out["strategic_research_missions"] = [
            {
                "mission_id": m.get("mission_id"),
                "objective": m.get("objective"),
                "status": m.get("status"),
                "trigger": m.get("trigger"),
            }
            for m in missions
            if m.get("status") in {MS_OPEN, MS_IN_PROGRESS, MS_BLOCKED, MS_UNKNOWN}
        ][:15]
        out["new_observations"] = [
            {
                "observation": o.get("observation"),
                "timeframe": o.get("timeframe"),
                "limitations": o.get("limitations"),
            }
            for o in observations
        ][:15]
        out["capability_gaps"] = [
            {
                "gap": g.get("missing_capability"),
                "why": g.get("why_it_matters"),
                "actions": g.get("possible_actions"),
            }
            for g in gaps
        ][:15]
        out["relationship_changes"] = [
            {
                "relationship": f"{r.get('entity_a')} → {r.get('entity_b')}",
                "type": r.get("relationship_type"),
                "evidence": (r.get("evidence") or ["UNKNOWN"])[0],
            }
            for r in relationships
        ][:15]
        derived = []
        for r in (rows or [])[:15]:
            if not isinstance(r, dict):
                continue
            eco = build_opportunity_ecosystem_view(r)
            if any(s.get("name") == "UNKNOWN" for s in (eco.get("suppliers") or [])):
                derived.append(
                    {
                        "opportunity_id": r.get("canonical_id"),
                        "objective": "Understand supplier ecosystem",
                        "trigger": "supplier UNKNOWN in ecosystem view",
                        "status": MS_OPEN,
                    }
                )
        out["strategic_research_missions"] = (
            list(out.get("strategic_research_missions") or []) + derived
        )[:15]
    else:
        out["new_strategic_knowledge"] = [
            {
                "topic": t.get("strategic_topic"),
                "status": t.get("status"),
                "id": t.get("strategic_id"),
            }
            for t in topics
            if t.get("status") in {ST_DOCUMENTED, ST_OBSERVED, ST_UNDER_REVIEW}
        ][:15]
        out["resolved_questions"] = [
            {
                "question": m.get("strategic_question"),
                "outcome": m.get("outcome"),
                "lessons": m.get("lessons"),
            }
            for m in memories
            if _known(m.get("outcome")) and m.get("outcome") != "UNKNOWN"
        ][:15]
        out["updated_relationships"] = [
            {
                "relationship": f"{r.get('entity_a')} → {r.get('entity_b')}",
                "type": r.get("relationship_type"),
            }
            for r in relationships
        ][:15]
        out["lessons_created"] = [
            {"lesson": m.get("lessons"), "question": m.get("strategic_question")}
            for m in memories
            if _known(m.get("lessons")) and m.get("lessons") != "UNKNOWN"
        ][:15]

    return out
