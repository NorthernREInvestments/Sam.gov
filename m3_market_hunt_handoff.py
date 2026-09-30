"""BUILD 10 — Market Hunt → Deal Room handoff (read/experience layer).

Assembles plain-language "why this matters" + context panels from existing
BUILD 3–9 intelligence. Does not create engines, change ranking, or redesign
Deal Room.
"""

from __future__ import annotations

from typing import Any

from application_clock import now_utc

BUILD_TAG = "20260918-m3-market-hunt-handoff-1"

HANDOFF_ACTIONS = (
    {"action": "open_deal_room", "label": "Open Deal Room"},
    {"action": "view_intelligence_graph", "label": "View Intelligence Graph"},
    {"action": "view_research_queue", "label": "View Research Queue"},
)


def _utc() -> str:
    return now_utc().isoformat()


def _unknown(v: Any) -> Any:
    if v is None or v == "" or str(v).upper() in {"UNKNOWN", "NONE", "NULL"}:
        return "UNKNOWN"
    return v


def _plain(status: Any) -> str:
    s = str(status or "UNKNOWN").upper()
    mapping = {
        "VALIDATED": "Validated",
        "AVAILABLE": "Available",
        "EXECUTION_READY": "Ready to act",
        "POSSIBLE": "Needs check",
        "RESEARCH_REQUIRED": "Needs research",
        "UNKNOWN": "UNKNOWN",
        "HIGH": "High",
        "MEDIUM": "Medium",
        "LOW": "Low",
        "COMPLETE": "Complete",
        "INCOMPLETE": "Incomplete",
    }
    return mapping.get(s, s.replace("_", " ").title())


def _node(graph: dict[str, Any], key: str) -> dict[str, Any]:
    n = graph.get(key)
    return n if isinstance(n, dict) else {}


def _facts(node: dict[str, Any]) -> dict[str, Any]:
    f = node.get("facts")
    return f if isinstance(f, dict) else {}


def _evidence_snippets(node: dict[str, Any], *, limit: int = 4) -> list[str]:
    out: list[str] = []
    for ev in node.get("evidence") or []:
        if not isinstance(ev, dict):
            continue
        snip = ev.get("snippet") or ev.get("field") or ev.get("source")
        if snip and str(snip) not in out:
            out.append(str(snip))
        if len(out) >= limit:
            break
    if not out:
        reason = node.get("reason")
        if reason:
            out.append(str(reason))
    return out or ["UNKNOWN"]


def _why_this_matters(
    *,
    graph: dict[str, Any],
    demand_bits: dict[str, Any],
    supplier_bits: dict[str, Any],
    research_bits: dict[str, Any],
) -> list[str]:
    lines: list[str] = []
    hist = demand_bits.get("historical_purchases")
    agencies = demand_bits.get("agencies") or []
    known_agencies = [a for a in agencies if a and a != "UNKNOWN"]
    suppliers = supplier_bits.get("suppliers") or []
    known_suppliers = [s for s in suppliers if isinstance(s, dict) and s.get("name") and s.get("name") != "UNKNOWN"]

    hist_node = _node(graph, "historical_intelligence")
    if str(hist_node.get("status") or "").upper() in {"VALIDATED", "AVAILABLE", "POSSIBLE"} or (
        isinstance(hist, int) and hist > 0
    ):
        lines.append("Government has repeatedly purchased this product.")
    if len(known_agencies) >= 2:
        lines.append(f"{len(known_agencies)} agencies have recent demand.")
    elif len(known_agencies) == 1:
        lines.append(f"{known_agencies[0]} has recent demand.")
    if known_suppliers:
        lines.append(
            f"{len(known_suppliers)} supplier{'s' if len(known_suppliers) != 1 else ''} identified."
        )
    else:
        lines.append("Supplier: UNKNOWN — no validated commercial supplier relationship.")

    gaps = research_bits.get("gaps") or []
    if gaps:
        top = gaps[0]
        gap_label = top.get("gap_label") or top.get("research_type") or "missing information"
        lines.append(f"Current gap: {gap_label}.")
    elif not lines:
        lines.append("Opportunity needs review — intelligence still incomplete.")

    # Always keep UNKNOWN visible when product identity is weak
    prod = _node(graph, "product_identity")
    if str(prod.get("status") or "").upper() in {"UNKNOWN", "RESEARCH_REQUIRED", ""}:
        lines.append("Product identity: UNKNOWN — confirm what is being bought.")

    return lines[:6]


def _product_panel(graph: dict[str, Any]) -> dict[str, Any]:
    node = _node(graph, "product_identity")
    facts = _facts(node)
    status = _unknown(node.get("status"))
    return {
        "title": "Product Intelligence",
        "identity": _unknown(
            facts.get("nsn")
            or facts.get("part_number")
            or facts.get("title")
            or facts.get("product_label")
        ),
        "nsn": _unknown(facts.get("nsn")),
        "part_number": _unknown(facts.get("part_number")),
        "confidence": _unknown(node.get("confidence")),
        "status": status,
        "status_label": _plain(status),
        "evidence": _evidence_snippets(node),
        "reason": _unknown(node.get("reason") or "Product facts from intelligence graph"),
        "missing": list(node.get("missing") or []) or (
            ["product_identity"] if str(status).upper() in {"UNKNOWN", "RESEARCH_REQUIRED"} else []
        ),
    }


def _demand_panel(graph: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    demand = _node(graph, "demand_signals")
    hist = _node(graph, "historical_intelligence")
    award = row.get("award_product_projection") if isinstance(row.get("award_product_projection"), dict) else {}
    agencies = list(award.get("buying_agencies") or [])
    evidence = award.get("demand_evidence") if isinstance(award.get("demand_evidence"), list) else []
    purchase_count: Any = len(evidence) if evidence else "UNKNOWN"
    # Prefer graph facts
    df = _facts(demand)
    hf = _facts(hist)
    if df.get("agency_count") not in (None, "", "UNKNOWN"):
        try:
            purchase_count = max(
                int(purchase_count) if isinstance(purchase_count, int) else 0,
                int(df.get("signal_count") or 0),
            ) or purchase_count
        except (TypeError, ValueError):
            pass
    if hf.get("award_count") not in (None, "", "UNKNOWN"):
        purchase_count = hf.get("award_count")
    for a in df.get("agencies") or []:
        if a and str(a) not in agencies:
            agencies.append(str(a))
    if not agencies:
        agencies = ["UNKNOWN"]
    signal_types = list(df.get("signal_types") or []) or ["UNKNOWN"]
    status = _unknown(demand.get("status") or hist.get("status"))
    return {
        "title": "Demand Intelligence",
        "historical_purchases": purchase_count if purchase_count != 0 else "UNKNOWN",
        "agencies": agencies,
        "agency_count": len([a for a in agencies if a != "UNKNOWN"]),
        "demand_signals": signal_types,
        "status": status,
        "status_label": _plain(status),
        "evidence": _evidence_snippets(demand) if demand else _evidence_snippets(hist),
        "reason": _unknown(
            demand.get("reason") or hist.get("reason") or "No validated demand signal yet"
        ),
        "missing": list(demand.get("missing") or hist.get("missing") or [])
        or (["demand_signals"] if str(status).upper() in {"UNKNOWN", "RESEARCH_REQUIRED"} else []),
    }


def _supplier_panel(graph: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    node = _node(graph, "supplier_intelligence")
    facts = _facts(node)
    spg = row.get("supplier_product_graph") if isinstance(row.get("supplier_product_graph"), dict) else {}
    edges = list(spg.get("edges") or [])
    suppliers: list[dict[str, Any]] = []
    for e in edges:
        if not isinstance(e, dict) or not e.get("supplier_name"):
            continue
        suppliers.append(
            {
                "name": e.get("supplier_name"),
                "relationship_type": _unknown(e.get("relationship_type")),
                "confidence": _unknown(e.get("confidence")),
            }
        )
    if not suppliers:
        for name in facts.get("suppliers") or facts.get("supplier_names") or []:
            if name:
                suppliers.append(
                    {
                        "name": name,
                        "relationship_type": _unknown(facts.get("relationship_type")),
                        "confidence": _unknown(node.get("confidence")),
                    }
                )
    status = _unknown(node.get("status"))
    missing = str(status).upper() in {"UNKNOWN", "RESEARCH_REQUIRED"} or not suppliers
    if not suppliers:
        suppliers = [
            {
                "name": "UNKNOWN",
                "relationship_type": "UNKNOWN",
                "confidence": "UNKNOWN",
            }
        ]
    return {
        "title": "Supplier Intelligence",
        "suppliers": suppliers[:8],
        "supplier_count": 0 if missing and suppliers[0]["name"] == "UNKNOWN" else len(
            [s for s in suppliers if s.get("name") != "UNKNOWN"]
        ),
        "status": status,
        "status_label": _plain(status),
        "evidence": _evidence_snippets(node),
        "reason": _unknown(
            node.get("reason")
            or (
                "No validated commercial supplier relationship."
                if missing
                else "Supplier relationships from product graph"
            )
        ),
        "missing": list(node.get("missing") or [])
        or (["supplier_channels"] if missing else []),
        "unknown_visible": True,
    }


def _research_panel(row: dict[str, Any], graph: dict[str, Any]) -> dict[str, Any]:
    from m3_research_queue import build_research_queue
    from m3_opportunity_identity import OpportunityIdentityResolver

    try:
        queue = build_research_queue(
            rows=[row],
            identity_resolver=OpportunityIdentityResolver(),
            status_index={"by_key": {}},
            limit=20,
        )
        items = list(queue.get("items") or [])
    except Exception:
        items = []

    type_labels = {
        "PRODUCT_IDENTITY": "Product not clear yet",
        "SUPPLIER": "Supplier missing",
        "HISTORICAL": "Historical pricing missing",
        "PROCUREMENT_PATH": "Buy path unclear",
        "FINANCING": "Financing unknown",
        "ECONOMICS": "Economics incomplete",
        "DOCUMENT_REVIEW": "Documents needed",
    }
    action_labels = {
        "SUPPLIER": "Find acquisition path",
        "HISTORICAL": "Research market pricing",
        "ECONOMICS": "Complete economics",
        "PRODUCT_IDENTITY": "Identify exact product",
    }
    gaps = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("status") or "").upper() == "COMPLETE":
            continue
        rtype = str(item.get("research_type") or "UNKNOWN")
        gaps.append(
            {
                "research_type": rtype,
                "gap_label": type_labels.get(rtype, rtype.replace("_", " ").title()),
                "action_label": action_labels.get(
                    rtype, item.get("recommended_action") or "Research"
                ),
                "why": item.get("why_this_matters") or item.get("reason") or "UNKNOWN",
                "missing_information": item.get("missing_information") or ["UNKNOWN"],
                "priority": item.get("priority"),
                "status": item.get("status") or "NEW",
            }
        )

    if not gaps:
        # Surface graph-level unknowns so UNKNOWN stays visible
        for key, label in (
            ("product_identity", "Product not clear yet"),
            ("supplier_intelligence", "Supplier missing"),
            ("economics", "Economics incomplete"),
        ):
            node = _node(graph, key)
            if str(node.get("status") or "").upper() in {"UNKNOWN", "RESEARCH_REQUIRED"}:
                gaps.append(
                    {
                        "research_type": key.upper(),
                        "gap_label": label,
                        "action_label": action_labels.get(
                            "SUPPLIER" if "supplier" in key else "ECONOMICS",
                            "Research",
                        ),
                        "why": _unknown(node.get("reason")),
                        "missing_information": list(node.get("missing") or [key]),
                        "priority": 50,
                        "status": "NEW",
                    }
                )

    return {
        "title": "Research Needed",
        "gaps": gaps[:10],
        "gap_count": len(gaps),
        "next_actions": [g.get("action_label") for g in gaps[:5]] or ["UNKNOWN"],
        "status": "NEEDS_RESEARCH" if gaps else "CLEAR",
    }


def _economics_panel(graph: dict[str, Any]) -> dict[str, Any]:
    node = _node(graph, "economics")
    facts = _facts(node)
    status = _unknown(node.get("status"))
    incomplete = str(status).upper() in {"UNKNOWN", "RESEARCH_REQUIRED", "POSSIBLE"}
    missing = list(node.get("missing") or [])
    if incomplete and not missing:
        missing = ["acquisition_cost", "margin_evidence"]
        if facts.get("revenue") in (None, "", "UNKNOWN"):
            missing.insert(0, "revenue")
    return {
        "title": "Economics Status",
        "complete": not incomplete,
        "status": status,
        "status_label": "Incomplete" if incomplete else _plain(status),
        "revenue": _unknown(facts.get("revenue")),
        "projected_profit": _unknown(facts.get("projected_profit") or facts.get("profit")),
        "missing_inputs": missing or (["UNKNOWN"] if incomplete else []),
        "reason": _unknown(
            node.get("reason")
            or ("Economics incomplete — missing inputs" if incomplete else "Economics available")
        ),
        "evidence": _evidence_snippets(node),
    }


def build_market_hunt_handoff(
    opportunity_id: str,
    *,
    row: dict[str, Any] | None = None,
    store: Any | None = None,
    graph: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Read-only handoff payload: Hunt card → Deal Room context.

    Preserves UNKNOWN. Attaches product/demand/supplier/research/economics panels.
    """
    oid = str(opportunity_id or "").strip()
    if graph is None or row is None:
        from m3_intelligence_graph_read import get_intelligence_graph, resolve_pipeline_row
        from m3_opportunity_identity import OpportunityIdentityResolver

        resolver = OpportunityIdentityResolver()
        resolved_row, ident = resolve_pipeline_row(oid, store=store, identity_resolver=resolver)
        if row is None:
            row = resolved_row
        if graph is None:
            if row is not None:
                from m3_intelligence_graph_read import assemble_intelligence_graph

                graph = assemble_intelligence_graph(
                    row, identity=ident, opportunity_id=oid or row.get("canonical_id")
                )
            else:
                graph = get_intelligence_graph(oid, store=store, restore_from_db=False)

    if not isinstance(row, dict):
        row = {}
    if not isinstance(graph, dict):
        graph = {}

    cid = row.get("canonical_id") or graph.get("opportunity_id") or oid or "UNKNOWN"
    found = bool(row.get("canonical_id") or graph.get("found"))

    demand_panel = _demand_panel(graph, row)
    supplier_panel = _supplier_panel(graph, row)
    research_panel = _research_panel(row, graph)
    product_panel = _product_panel(graph)
    economics_panel = _economics_panel(graph)

    why = _why_this_matters(
        graph=graph,
        demand_bits=demand_panel,
        supplier_bits=supplier_panel,
        research_bits=research_panel,
    )

    return {
        "kind": "M3MarketHuntHandoff",
        "build": BUILD_TAG,
        "opportunity_id": cid,
        "found": found,
        "generated_at": _utc(),
        "question": "What exactly is this opportunity and what should I do next?",
        "why_this_matters": {
            "title": "Why M3 Thinks This Matters",
            "lines": why,
            "summary": " ".join(why[:3]),
        },
        "context": {
            "product": product_panel,
            "demand": demand_panel,
            "supplier": supplier_panel,
            "research": research_panel,
            "economics": economics_panel,
        },
        "actions": [dict(a) for a in HANDOFF_ACTIONS],
        "links": {
            "deal_room": f"/api/m3/mobile/deal/{cid}",
            "intelligence_graph": f"/api/m3/intelligence-graph/{cid}",
            "research_queue": "/api/m3/research-queue",
        },
        "read_only": True,
        "engines_unchanged": True,
        "unknown_preserved": True,
    }


def attach_handoff_to_deal_room(
    deal: dict[str, Any],
    *,
    row: dict[str, Any] | None = None,
    store: Any | None = None,
) -> dict[str, Any]:
    """Additive attach — does not redesign existing Deal Room keys."""
    if not isinstance(deal, dict):
        return deal
    cid = str(deal.get("canonical_id") or "").strip()
    if not cid:
        return deal
    try:
        handoff = build_market_hunt_handoff(cid, row=row, store=store)
    except Exception:
        handoff = {
            "kind": "M3MarketHuntHandoff",
            "build": BUILD_TAG,
            "opportunity_id": cid,
            "found": False,
            "why_this_matters": {
                "title": "Why M3 Thinks This Matters",
                "lines": ["Intelligence handoff unavailable — Deal Room sections still load."],
                "summary": "Handoff unavailable",
            },
            "context": {},
            "actions": [dict(a) for a in HANDOFF_ACTIONS],
            "read_only": True,
            "engines_unchanged": True,
            "unknown_preserved": True,
        }
    out = dict(deal)
    out["hunt_handoff"] = handoff
    return out
