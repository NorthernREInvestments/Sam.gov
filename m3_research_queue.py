"""BUILD 4 — Graph-driven research queue.

Turns Intelligence Graph UNKNOWN / RESEARCH_REQUIRED nodes into prioritized
research actions. Does not create engines, change scoring, or modify discovery.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from application_clock import now_utc
from m3_intelligence_graph_read import (
    GRAPH_NODES,
    assemble_intelligence_graph,
)
from m3_opportunity_identity import OpportunityIdentityResolver
from m3_product_fact_projection import (
    INTEL_AVAILABLE,
    INTEL_EXECUTION_READY,
    INTEL_POSSIBLE,
    INTEL_RESEARCH_REQUIRED,
    INTEL_UNKNOWN,
    INTEL_VALIDATED,
)

BUILD_TAG = "20260918-m3-graph-research-queue-1"
QUEUE_STATUS_KEY = "m3_graph_research_queue_status_v1"

# Research types (operator-facing)
PRODUCT_IDENTITY = "PRODUCT_IDENTITY"
SUPPLIER = "SUPPLIER"
HISTORICAL = "HISTORICAL"
PROCUREMENT_PATH = "PROCUREMENT_PATH"
FINANCING = "FINANCING"
ECONOMICS = "ECONOMICS"
DOCUMENT_REVIEW = "DOCUMENT_REVIEW"

# Item status
STATUS_NEW = "NEW"
STATUS_IN_PROGRESS = "IN_PROGRESS"
STATUS_COMPLETE = "COMPLETE"
STATUS_BLOCKED = "BLOCKED"

# Nodes that should NOT emit research items when already strong
_SATISFIED = {INTEL_VALIDATED, INTEL_AVAILABLE, INTEL_EXECUTION_READY}

# Graph node → research type
_NODE_TO_TYPE: dict[str, str] = {
    "product_identity": PRODUCT_IDENTITY,
    "requirement": PRODUCT_IDENTITY,
    "supplier_intelligence": SUPPLIER,
    "historical_intelligence": HISTORICAL,
    "procurement_path": PROCUREMENT_PATH,
    "financing": FINANCING,
    "economics": ECONOMICS,
}

# Criticality weight (higher = more blocking for pursuit decision)
_TYPE_CRITICALITY: dict[str, int] = {
    PRODUCT_IDENTITY: 40,
    DOCUMENT_REVIEW: 35,
    SUPPLIER: 30,
    HISTORICAL: 22,
    ECONOMICS: 20,
    PROCUREMENT_PATH: 18,
    FINANCING: 12,
}

_TYPE_ACTIONS: dict[str, str] = {
    PRODUCT_IDENTITY: "Recover exact NSN/part/OEM evidence from description or package",
    SUPPLIER: "Identify validated supplier channels and acquisition pricing evidence",
    HISTORICAL: "Retrieve government award / unit-price history for this product identity",
    PROCUREMENT_PATH: "Establish procurement / price path (NSN-USAspending, OEM-distributor, or quote)",
    FINANCING: "Verify capital requirement and financing path (unknown is not rejection)",
    ECONOMICS: "Complete revenue / acquisition / profit evidence without inventing margins",
    DOCUMENT_REVIEW: "Recover governing documents or resolve package access blocker",
}

_TYPE_WHY: dict[str, str] = {
    PRODUCT_IDENTITY: "Without product identity, supplier and economics research cannot be trusted",
    SUPPLIER: "Supplier evidence unlocks acquisition cost and margin viability",
    HISTORICAL: "Historical demand/pricing anchors whether pursuit economics are realistic",
    PROCUREMENT_PATH: "A clear procurement path determines how acquisition will be executed",
    FINANCING: "Financing unknowns block execution readiness even when product economics look viable",
    ECONOMICS: "Economic evidence is required to decide if the opportunity is worth pursuing",
    DOCUMENT_REVIEW: "Package/document gaps block requirement and identity validation",
}


def _utc() -> str:
    return now_utc().isoformat()


def _clean(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _num(v: Any) -> float | None:
    if v is None or v == "" or str(v).upper() == "UNKNOWN":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _node_needs_research(node: dict[str, Any] | None) -> bool:
    if not isinstance(node, dict):
        return False
    status = str(node.get("status") or "").upper()
    if status in _SATISFIED:
        return False
    if status in {INTEL_UNKNOWN, INTEL_RESEARCH_REQUIRED}:
        return True
    # POSSIBLE on product/supplier still warrants research to validate
    if status == INTEL_POSSIBLE:
        return True
    return False


def _document_gap(row: dict[str, Any], graph: dict[str, Any]) -> bool:
    access = str(row.get("package_access") or row.get("source_access_state") or "").upper()
    if access in {"AUTH_REQUIRED", "REGISTRATION_REQUIRED", "BOT_PROTECTED", "AUTH_GATED"}:
        return True
    req = (graph.get("requirement") or {}) if isinstance(graph.get("requirement"), dict) else {}
    missing = req.get("missing") or []
    if "requirement_structure" in missing and not row.get("line_items") and not row.get("bom"):
        return True
    return False


def load_queue_status_index() -> dict[str, Any]:
    try:
        from database import SessionLocal
        from models import AppSetting

        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == QUEUE_STATUS_KEY).one_or_none()
            if row and row.value:
                data = json.loads(row.value)
                if isinstance(data, dict):
                    data.setdefault("by_key", {})
                    return data
        finally:
            db.close()
    except Exception:
        pass
    return {"kind": "M3GraphResearchQueueStatus", "by_key": {}, "build": BUILD_TAG}


def save_queue_status_index(index: dict[str, Any]) -> bool:
    try:
        from database import SessionLocal
        from models import AppSetting

        index = dict(index)
        index["updated_at"] = _utc()
        index["build"] = BUILD_TAG
        raw = json.dumps(index, default=str)
        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == QUEUE_STATUS_KEY).one_or_none()
            if row:
                row.value = raw
            else:
                db.add(AppSetting(key=QUEUE_STATUS_KEY, value=raw))
            db.commit()
            return True
        finally:
            db.close()
    except Exception:
        return False


def item_key(opportunity_id: str, research_type: str) -> str:
    return f"{opportunity_id}|{research_type}"


def _resolve_status(
    opportunity_id: str,
    research_type: str,
    *,
    status_index: dict[str, Any] | None,
    package_blocked: bool,
) -> str:
    by = (status_index or {}).get("by_key") or {}
    stored = by.get(item_key(opportunity_id, research_type))
    if isinstance(stored, dict) and stored.get("status") in {
        STATUS_NEW,
        STATUS_IN_PROGRESS,
        STATUS_COMPLETE,
        STATUS_BLOCKED,
    }:
        return str(stored["status"])
    if package_blocked and research_type in {DOCUMENT_REVIEW, PRODUCT_IDENTITY, PROCUREMENT_PATH}:
        return STATUS_BLOCKED
    return STATUS_NEW


def _opportunity_potential(row: dict[str, Any], graph: dict[str, Any]) -> dict[str, Any]:
    """Multi-factor potential — not contract value alone."""
    g = graph.get("graph") or graph
    product = g.get("product_identity") if isinstance(g.get("product_identity"), dict) else {}
    hist = g.get("historical_intelligence") if isinstance(g.get("historical_intelligence"), dict) else {}
    econ = g.get("economics") if isinstance(g.get("economics"), dict) else {}
    supplier = g.get("supplier_intelligence") if isinstance(g.get("supplier_intelligence"), dict) else {}

    product_status = str(product.get("status") or "").upper()
    hist_status = str(hist.get("status") or "").upper()
    econ_facts = econ.get("facts") if isinstance(econ.get("facts"), dict) else {}
    product_facts = product.get("facts") if isinstance(product.get("facts"), dict) else {}

    score = 0
    factors: list[str] = []

    # Product confidence (strong signal)
    if product_status in {INTEL_VALIDATED, INTEL_AVAILABLE}:
        score += 28
        factors.append("product_validated")
    elif product_status == INTEL_POSSIBLE:
        score += 10
        factors.append("product_possible")
    elif product_facts.get("nsn") not in (None, "", "UNKNOWN"):
        score += 16
        factors.append("nsn_present")

    # Historical demand (not dollar size alone)
    if hist_status in {INTEL_VALIDATED, INTEL_AVAILABLE}:
        score += 18
        factors.append("historical_validated")
    elif hist_status == INTEL_RESEARCH_REQUIRED:
        score += 4
        factors.append("historical_research_needed")

    # Execution / readiness potential
    readiness = str(row.get("readiness_state") or "")
    if "COMMERCIAL_RESEARCH" in readiness.upper() or "PRODUCT_IDENTITY_EXACT" in readiness.upper():
        score += 16
        factors.append("research_ready")
    if str(row.get("package_access") or "").upper() in {"PUBLIC", "PACKAGE_RECOVERED", "OPEN"}:
        score += 6
        factors.append("package_accessible")

    # Economics signal — profit/confidence preferred over raw revenue
    profit = _num(econ_facts.get("projected_profit"))
    revenue = _num(econ_facts.get("revenue"))
    conf = str(econ_facts.get("pricing_confidence") or econ.get("confidence") or "").upper()
    if profit is not None and profit >= 10000:
        score += 14
        factors.append("profit_signal_10k+")
    elif profit is not None and profit >= 3000:
        score += 8
        factors.append("profit_signal")
    if conf in {"HIGH", "VALIDATED", "LEVEL_1", "LEVEL_2"}:
        score += 8
        factors.append("pricing_confidence")
    # Mild value signal only as secondary (capped)
    if revenue is not None:
        if revenue >= 100000:
            score += 6
            factors.append("revenue_band_high")
        elif revenue >= 25000:
            score += 3
            factors.append("revenue_band_mid")

    # Deadline urgency (execution potential)
    days = None
    de = row.get("deadline_evaluation") if isinstance(row.get("deadline_evaluation"), dict) else {}
    days = de.get("calendar_days_remaining")
    if isinstance(days, (int, float)):
        if days <= 5:
            score += 12
            factors.append("deadline_urgent")
        elif days <= 14:
            score += 6
            factors.append("deadline_near")

    # Supplier already partial → finishing research has high leverage
    if str(supplier.get("status") or "").upper() == INTEL_POSSIBLE:
        score += 5
        factors.append("supplier_partial")

    return {"potential_score": score, "factors": factors}


def _priority_for_item(
    research_type: str,
    *,
    potential: dict[str, Any],
    node: dict[str, Any],
    product_known: bool,
) -> tuple[int, str]:
    """Higher priority number = more important. Not contract-value-only."""
    base = int(_TYPE_CRITICALITY.get(research_type, 10))
    pot = int(potential.get("potential_score") or 0)
    status = str(node.get("status") or "").upper()

    priority = base + pot

    # Leverage: if product known, supplier/historical/economics research is more valuable
    if product_known and research_type in {SUPPLIER, HISTORICAL, ECONOMICS, PROCUREMENT_PATH}:
        priority += 15

    # If product unknown, de-prioritize downstream a bit (still emit, but rank lower)
    if not product_known and research_type in {SUPPLIER, FINANCING, ECONOMICS}:
        priority -= 10

    if status == INTEL_RESEARCH_REQUIRED:
        priority += 5
    if status == INTEL_UNKNOWN:
        priority += 3

    # Financing is important but rarely the first gate
    if research_type == FINANCING and product_known:
        priority += 2

    # Clamp
    priority = max(1, min(100, priority))

    label = "HIGH" if priority >= 70 else ("MEDIUM" if priority >= 45 else "LOW")
    return priority, label


def research_items_from_graph(
    row: dict[str, Any],
    graph: dict[str, Any],
    *,
    status_index: dict[str, Any] | None = None,
    created_at: str | None = None,
) -> list[dict[str, Any]]:
    """Emit research queue items from one assembled graph. Idempotent shape."""
    oid = _clean(row.get("canonical_id")) or _clean(graph.get("opportunity_id")) or "UNKNOWN"
    nodes = graph.get("graph") if isinstance(graph.get("graph"), dict) else {}
    # Flat aliases also work
    for name in GRAPH_NODES:
        if name not in nodes and isinstance(graph.get(name), dict):
            nodes[name] = graph[name]

    potential = _opportunity_potential(row, graph)
    product_node = nodes.get("product_identity") if isinstance(nodes.get("product_identity"), dict) else {}
    product_known = str(product_node.get("status") or "").upper() in _SATISFIED

    ts = created_at or _utc()
    items: list[dict[str, Any]] = []
    emitted_types: set[str] = set()

    # Document review (package/access gaps) — separate type
    if _document_gap(row, graph if graph.get("requirement") else {"requirement": nodes.get("requirement")}):
        rtype = DOCUMENT_REVIEW
        package_blocked = True
        status = _resolve_status(oid, rtype, status_index=status_index, package_blocked=package_blocked)
        if status != STATUS_COMPLETE:
            pri, label = _priority_for_item(
                rtype, potential=potential, node=nodes.get("requirement") or {}, product_known=product_known
            )
            missing = list((nodes.get("requirement") or {}).get("missing") or [])
            if row.get("package_access"):
                missing = list(dict.fromkeys(missing + [f"package_access:{row.get('package_access')}"]))
            items.append(
                {
                    "kind": "M3ResearchQueueItem",
                    "opportunity_id": oid,
                    "opportunity_uid": graph.get("opportunity_uid"),
                    "title": row.get("title") or "UNKNOWN",
                    "agency": row.get("agency") or row.get("buyer") or "UNKNOWN",
                    "research_type": rtype,
                    "priority": pri,
                    "priority_label": label,
                    "reason": _TYPE_WHY[rtype],
                    "why_this_matters": _TYPE_WHY[rtype],
                    "missing_information": missing or ["governing_documents_or_package_access"],
                    "recommended_action": _TYPE_ACTIONS[rtype],
                    "graph_node": "requirement",
                    "graph_status": (nodes.get("requirement") or {}).get("status") or INTEL_UNKNOWN,
                    "created_at": ts,
                    "status": status,
                    "potential_factors": potential.get("factors") or [],
                    "build": BUILD_TAG,
                }
            )
            emitted_types.add(rtype)

    for node_name, rtype in _NODE_TO_TYPE.items():
        if rtype in emitted_types and rtype == PRODUCT_IDENTITY and node_name == "requirement":
            # Prefer product_identity node for PRODUCT_IDENTITY; skip duplicate from requirement
            # unless product node does not need research
            if _node_needs_research(product_node):
                continue
        node = nodes.get(node_name) if isinstance(nodes.get(node_name), dict) else {}
        if not _node_needs_research(node):
            continue
        # Financing verified on row → do not queue
        if rtype == FINANCING:
            funding_raw = str(
                row.get("funding_status")
                or (row.get("funding_requirement") or {}).get("status")
                or ""
            ).upper()
            if funding_raw in {"VERIFIED", "APPROVED", "FUNDED", "VALIDATED"}:
                continue
        # Dedupe research_type: keep highest-criticality node (first in map order wins for product)
        if rtype in emitted_types:
            continue

        package_blocked = str(row.get("package_access") or "").upper() in {
            "AUTH_REQUIRED",
            "REGISTRATION_REQUIRED",
            "BOT_PROTECTED",
            "AUTH_GATED",
        }
        status = _resolve_status(oid, rtype, status_index=status_index, package_blocked=package_blocked)
        if status == STATUS_COMPLETE:
            continue

        pri, label = _priority_for_item(
            rtype, potential=potential, node=node, product_known=product_known
        )
        missing = list(node.get("missing") or [])
        if node.get("reason") and node["reason"] not in missing:
            # keep structured missing; reason goes to why
            pass

        items.append(
            {
                "kind": "M3ResearchQueueItem",
                "opportunity_id": oid,
                "opportunity_uid": graph.get("opportunity_uid"),
                "title": row.get("title") or "UNKNOWN",
                "agency": row.get("agency") or row.get("buyer") or "UNKNOWN",
                "research_type": rtype,
                "priority": pri,
                "priority_label": label,
                "reason": node.get("reason") or _TYPE_WHY[rtype],
                "why_this_matters": _TYPE_WHY[rtype],
                "missing_information": missing or [f"{node_name}_incomplete"],
                "recommended_action": _TYPE_ACTIONS[rtype],
                "graph_node": node_name,
                "graph_status": node.get("status") or INTEL_UNKNOWN,
                "created_at": ts,
                "status": status,
                "potential_factors": potential.get("factors") or [],
                "build": BUILD_TAG,
            }
        )
        emitted_types.add(rtype)

    return items


def build_research_queue(
    rows: list[dict[str, Any]] | None = None,
    *,
    store: Any | None = None,
    identity_resolver: OpportunityIdentityResolver | None = None,
    status_index: dict[str, Any] | None = None,
    limit: int = 100,
    include_complete: bool = False,
) -> dict[str, Any]:
    """
    Build prioritized research queue from pipeline opportunities via Intelligence Graph.

    Does not run engines. Read-only assembly + ranking.
    """
    if rows is None:
        if store is None:
            from m3_pipeline_store import M3PipelineStore

            store = M3PipelineStore()
        rows = list(store.all()) if hasattr(store, "all") else []

    resolver = identity_resolver or OpportunityIdentityResolver()
    status_index = status_index if status_index is not None else load_queue_status_index()
    created_at = _utc()
    all_items: list[dict[str, Any]] = []

    for row in rows:
        if not isinstance(row, dict) or not row.get("canonical_id"):
            continue
        # Skip terminal rejected/closed
        lc = str(row.get("lifecycle") or "").upper()
        if lc in {"REJECTED", "REJECTED_CHEAP_SCREEN", "CANCELLED", "CLOSED", "AWARDED", "ARCHIVED", "LOST"}:
            continue
        try:
            ident = resolver.resolve_pipeline_row(row, register=True)
        except Exception:
            ident = {}
        graph = assemble_intelligence_graph(row, identity=ident, opportunity_id=row.get("canonical_id"))
        items = research_items_from_graph(
            row, graph, status_index=status_index, created_at=created_at
        )
        all_items.extend(items)
        # BUILD 12 — additive clause research hints (does not change existing priorities)
        try:
            from m3_dla_clause_extraction import (
                clauses_to_research_items,
                collect_document_texts,
                extract_dla_clauses,
            )

            if collect_document_texts(row):
                bundle = row.get("dla_clause_extraction")
                if not isinstance(bundle, dict):
                    bundle = extract_dla_clauses(row)
                all_items.extend(
                    clauses_to_research_items(
                        bundle, opportunity_id=str(row.get("canonical_id"))
                    )
                )
        except Exception:
            pass
        # BUILD 13 — source qualification research actions
        try:
            from m3_source_qualification_read import (
                build_source_qualification_profile,
                qualification_to_research_items,
            )

            sq = row.get("source_qualification")
            if not isinstance(sq, dict):
                sq = build_source_qualification_profile(row)
            all_items.extend(
                qualification_to_research_items(sq, opportunity_id=str(row.get("canonical_id")))
            )
        except Exception:
            pass
        # BUILD 14 — eligibility evidence research actions
        try:
            from m3_eligibility_evidence_read import (
                build_eligibility_evidence_profile,
                eligibility_to_research_items,
            )

            ep = row.get("eligibility_evidence")
            if not isinstance(ep, dict):
                ep = build_eligibility_evidence_profile(row)
            all_items.extend(
                eligibility_to_research_items(ep, opportunity_id=str(row.get("canonical_id")))
            )
        except Exception:
            pass

    if not include_complete:
        all_items = [i for i in all_items if i.get("status") != STATUS_COMPLETE]

    # Sort: priority desc, then research criticality, then opportunity id
    all_items.sort(
        key=lambda i: (
            -int(i.get("priority") or 0),
            -int(_TYPE_CRITICALITY.get(str(i.get("research_type")), 0)),
            str(i.get("opportunity_id") or ""),
            str(i.get("research_type") or ""),
        )
    )
    limited = all_items[: max(1, min(500, int(limit or 100)))]

    by_type: dict[str, int] = {}
    for i in limited:
        t = str(i.get("research_type") or "UNKNOWN")
        by_type[t] = by_type.get(t, 0) + 1

    return {
        "kind": "M3GraphResearchQueue",
        "build": BUILD_TAG,
        "question": "What information is missing before we can decide if this opportunity is worth pursuing?",
        "count": len(limited),
        "total_candidates": len(all_items),
        "by_type": by_type,
        "items": limited,
        "priority_note": "Ranked by product confidence, historical demand, execution potential, and missing critical info — not contract value alone",
        "read_only": True,
        "engines_unchanged": True,
        "generated_at": created_at,
    }


def set_research_item_status(
    opportunity_id: str,
    research_type: str,
    status: str,
    *,
    status_index: dict[str, Any] | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """Optional operator status update — AppSetting index only, no new models."""
    if status not in {STATUS_NEW, STATUS_IN_PROGRESS, STATUS_COMPLETE, STATUS_BLOCKED}:
        return {"ok": False, "error": "invalid_status"}
    index = status_index if status_index is not None else load_queue_status_index()
    by = index.setdefault("by_key", {})
    key = item_key(opportunity_id, research_type)
    by[key] = {
        "opportunity_id": opportunity_id,
        "research_type": research_type,
        "status": status,
        "updated_at": _utc(),
    }
    if persist:
        save_queue_status_index(index)
    return {"ok": True, "item": by[key], "build": BUILD_TAG}
