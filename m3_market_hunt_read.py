"""BUILD 9 — Operator Market Hunt view (read/experience layer).

Assembles products-to-watch, discovery plans, research queue, and opportunity
cards from existing BUILD 3–8 intelligence. No new engines, no ranking changes.
"""

from __future__ import annotations

from typing import Any

from application_clock import now_utc

BUILD_TAG = "20260918-m3-market-hunt-1"


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


def _plain_status(status: Any) -> str:
    s = str(status or "UNKNOWN").upper()
    mapping = {
        "VALIDATED": "Ready",
        "AVAILABLE": "Ready",
        "EXECUTION_READY": "Ready to act",
        "POSSIBLE": "Needs check",
        "RESEARCH_REQUIRED": "Needs research",
        "UNKNOWN": "Unknown",
        "DETECTED": "Spotted",
        "APPROVED": "Ready to search",
        "DRAFT": "Draft",
        "ACTIVE": "Active",
        "HIGH": "Strong",
        "MEDIUM": "Medium",
        "LOW": "Weak",
    }
    return mapping.get(s, s.replace("_", " ").title())


def _product_watch_cards(
    demand_bundle: dict[str, Any],
    plan_bundle: dict[str, Any],
    supplier_view: dict[str, Any],
) -> list[dict[str, Any]]:
    """Section 1 — products/markets to watch (plain language)."""
    monitor = list(demand_bundle.get("products_to_monitor") or [])
    signals = list(demand_bundle.get("signals") or [])
    plans = list(plan_bundle.get("plans") or [])
    edges = list(supplier_view.get("edges") or [])

    # Index suppliers by product key
    suppliers_by_product: dict[str, list[dict[str, Any]]] = {}
    for e in edges:
        if not isinstance(e, dict):
            continue
        key = str(e.get("product_dedupe_key") or e.get("product_nsn") or "")
        if not key:
            continue
        suppliers_by_product.setdefault(key, []).append(e)

    # Index plans by product
    plans_by_product: dict[str, list[dict[str, Any]]] = {}
    for p in plans:
        if not isinstance(p, dict):
            continue
        prod = p.get("product") if isinstance(p.get("product"), dict) else {}
        key = str(prod.get("dedupe_key") or prod.get("nsn") or p.get("plan_id") or "")
        if key:
            plans_by_product.setdefault(key, []).append(p)

    # Enrich monitor list from signals if empty
    if not monitor:
        seen: set[str] = set()
        for s in signals:
            if not isinstance(s, dict):
                continue
            if str(s.get("status") or "").upper() in {"UNKNOWN", "EXPIRED"}:
                continue
            if str(s.get("confidence") or "").upper() in {"LOW", "UNKNOWN", ""}:
                continue
            prod = s.get("related_product") if isinstance(s.get("related_product"), dict) else {}
            key = str(s.get("product_dedupe_key") or prod.get("nsn") or prod.get("title") or s.get("related_agency") or "")
            if not key or key in seen:
                continue
            seen.add(key)
            monitor.append(
                {
                    "product_key": key,
                    "product": prod,
                    "agency": s.get("related_agency"),
                    "signal_types": [s.get("signal_type")],
                    "why_monitor": ((s.get("evidence") or [{}])[0] or {}).get("snippet") or s.get("signal_type"),
                    "confidence": s.get("confidence"),
                    "status": s.get("status"),
                }
            )

    cards: list[dict[str, Any]] = []
    for m in monitor[:20]:
        if not isinstance(m, dict):
            continue
        prod = m.get("product") if isinstance(m.get("product"), dict) else {}
        key = str(m.get("product_key") or prod.get("product_dedupe_key") or prod.get("nsn") or "")
        title = (
            prod.get("title")
            or prod.get("nsn")
            or prod.get("part_number")
            or key
            or "Product"
        )
        # Count related signals for this product/agency
        related_signals = [
            s
            for s in signals
            if isinstance(s, dict)
            and (
                str(s.get("product_dedupe_key") or "") == key
                or str((s.get("related_product") or {}).get("nsn") or "") == str(prod.get("nsn") or "___")
                or (not key and s.get("related_agency") == m.get("agency"))
            )
        ]
        agencies = sorted(
            {
                str(s.get("related_agency"))
                for s in related_signals
                if s.get("related_agency") and str(s.get("related_agency")) != "UNKNOWN"
            }
            | ({str(m["agency"])} if m.get("agency") else set())
        )
        # Historical purchase count heuristic from signal evidence / types
        hist_signals = [s for s in related_signals if str(s.get("signal_type")) == "HISTORICAL_PATTERN"]
        purchase_count = 0
        for s in hist_signals:
            for ev in s.get("evidence") or []:
                if isinstance(ev, dict) and ev.get("field") == "count":
                    try:
                        purchase_count = max(purchase_count, int(float(ev.get("snippet") or 0)))
                    except (TypeError, ValueError):
                        pass
        if purchase_count == 0 and hist_signals:
            purchase_count = len(hist_signals)

        supplier_edges = suppliers_by_product.get(key) or suppliers_by_product.get(
            f"nsn:{str(prod.get('nsn') or '').upper()}"
        ) or []
        supplier_names = sorted(
            {
                str(e.get("supplier_name"))
                for e in supplier_edges
                if e.get("supplier_name")
            }
        )[:6]

        related_plans = plans_by_product.get(key) or plans_by_product.get(
            f"nsn:{str(prod.get('nsn') or '').upper()}"
        ) or []
        discovery_status = "Not planned yet"
        if related_plans:
            st = str(related_plans[0].get("status") or "DRAFT").upper()
            discovery_status = _plain_status(st)

        why_bits = []
        if purchase_count:
            why_bits.append(f"{purchase_count} historical purchase{'s' if purchase_count != 1 else ''}")
        if agencies:
            why_bits.append(f"{len(agencies)} agency{'ies' if len(agencies) != 1 else ''} buying")
        if supplier_names:
            why_bits.append(f"{len(supplier_names)} known supplier{'s' if len(supplier_names) != 1 else ''}")
        types = [str(t) for t in (m.get("signal_types") or []) if t]
        if "HISTORICAL_PATTERN" in types or hist_signals:
            why_bits.append("recurring demand detected")
        if "RECOMPETE" in types:
            why_bits.append("possible recompete window")
        if "SOURCES_SOUGHT" in types:
            why_bits.append("early market research posted")
        if "WATCHLIST" in types:
            why_bits.append("on your watchlist")
        if not why_bits:
            why_bits.append(str(m.get("why_monitor") or "Demand signal detected"))

        demand_strength = _plain_status(m.get("confidence") or m.get("status") or "UNKNOWN")

        related_opportunity_id = None
        for s in related_signals:
            if s.get("opportunity_id"):
                related_opportunity_id = s.get("opportunity_id")
                break

        cards.append(
            {
                "kind": "M3MarketHuntProductCard",
                "product_key": key or title,
                "product_label": title,
                "nsn": prod.get("nsn") or "UNKNOWN",
                "category": "UNKNOWN",
                "demand_strength": demand_strength,
                "demand_strength_raw": m.get("confidence") or m.get("status") or "UNKNOWN",
                "historical_purchases": purchase_count if purchase_count else "UNKNOWN",
                "agencies": agencies or ["UNKNOWN"],
                "agency_count": len(agencies),
                "suppliers": supplier_names or ["UNKNOWN"],
                "supplier_count": len(supplier_names),
                "supplier_availability": (
                    "Available" if supplier_names else "Unknown — needs research"
                ),
                "discovery_status": discovery_status,
                "why": why_bits,
                "why_summary": "; ".join(why_bits[:4]),
                "next_action": (
                    "Review discovery plan"
                    if related_plans
                    else ("Find suppliers" if not supplier_names else "Watch for new postings")
                ),
                "related_plan_ids": [p.get("plan_id") for p in related_plans[:3]],
                "related_opportunity_id": related_opportunity_id,
                "signal_types": types or ["UNKNOWN"],
                "handoff_actions": [
                    {"action": "open_deal_room", "label": "Open Deal Room"},
                    {"action": "view_intelligence_graph", "label": "View Intelligence Graph"},
                    {"action": "view_research_queue", "label": "View Research Queue"},
                ]
                if related_opportunity_id
                else [
                    {"action": "view_research_queue", "label": "View Research Queue"},
                ],
            }
        )
    return cards


def _discovery_plan_cards(plan_bundle: dict[str, Any]) -> list[dict[str, Any]]:
    """Section 2 — active/approved discovery plans in plain language."""
    cards = []
    for p in plan_bundle.get("plans") or []:
        if not isinstance(p, dict):
            continue
        status = str(p.get("status") or "DRAFT").upper()
        if status in {"COMPLETED", "EXPIRED"}:
            continue
        prod = p.get("product") if isinstance(p.get("product"), dict) else {}
        cards.append(
            {
                "kind": "M3MarketHuntPlanCard",
                "plan_id": p.get("plan_id"),
                "product_label": prod.get("title") or prod.get("nsn") or prod.get("part_number") or "Product",
                "nsn": prod.get("nsn") or "UNKNOWN",
                "search_sources": p.get("target_sources") or ["UNKNOWN"],
                "identifiers": p.get("identifiers") or {},
                "search_terms": (p.get("search_terms") or [])[:6],
                "reason": p.get("reason") or "Demand supports monitoring",
                "priority": p.get("priority"),
                "priority_label": p.get("priority_label") or _plain_status(p.get("priority")),
                "status": status,
                "status_label": _plain_status(status),
                "agencies": p.get("target_agencies") or ["UNKNOWN"],
                "actions": [
                    {"action": "view_opportunities", "label": "View opportunities"},
                    {"action": "review_plan", "label": "Review plan"},
                    {"action": "mark_watched", "label": "Mark watched"},
                ],
                "estimated_search_cost": p.get("estimated_search_cost"),
                "auto_execute": False,
            }
        )
    cards.sort(key=lambda c: (-int(c.get("priority") or 0), str(c.get("plan_id") or "")))
    return cards[:25]


def _research_queue_cards(queue_bundle: dict[str, Any]) -> list[dict[str, Any]]:
    """Section 3 — highest priority missing information."""
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
        "PRODUCT_IDENTITY": "Identify exact product",
        "SUPPLIER": "Find acquisition path",
        "HISTORICAL": "Research market pricing",
        "PROCUREMENT_PATH": "Choose how to buy",
        "FINANCING": "Check financing needs",
        "ECONOMICS": "Complete economics",
        "DOCUMENT_REVIEW": "Recover package documents",
    }
    cards = []
    for item in queue_bundle.get("items") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("status") or "").upper() in {"COMPLETE", "BLOCKED"}:
            # Still show BLOCKED briefly
            if str(item.get("status") or "").upper() == "COMPLETE":
                continue
        rtype = str(item.get("research_type") or "UNKNOWN")
        cards.append(
            {
                "kind": "M3MarketHuntResearchCard",
                "opportunity_id": item.get("opportunity_id"),
                "title": item.get("title") or "Opportunity",
                "agency": item.get("agency") or "UNKNOWN",
                "research_type": rtype,
                "gap_label": type_labels.get(rtype, rtype.replace("_", " ").title()),
                "action_label": action_labels.get(rtype, item.get("recommended_action") or "Research"),
                "recommended_action": item.get("recommended_action"),
                "why": item.get("why_this_matters") or item.get("reason") or "Missing information blocks a decision",
                "missing_information": item.get("missing_information") or ["UNKNOWN"],
                "priority": item.get("priority"),
                "priority_label": item.get("priority_label") or "NORMAL",
                "status": item.get("status") or "NEW",
                "handoff_actions": [
                    {"action": "open_deal_room", "label": "Open Deal Room"},
                    {"action": "view_intelligence_graph", "label": "View Intelligence Graph"},
                    {"action": "view_research_queue", "label": "View Research Queue"},
                ],
            }
        )
    return cards[:20]


def _opportunity_cards(rows: list[dict[str, Any]], *, limit: int = 12) -> list[dict[str, Any]]:
    """Section 4 — simple live opportunity cards."""
    cards = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("canonical_id"):
            continue
        lc = str(row.get("lifecycle") or "").upper()
        if lc in {"REJECTED", "REJECTED_CHEAP_SCREEN", "CANCELLED", "CLOSED", "AWARDED", "ARCHIVED", "LOST"}:
            continue
        try:
            from m3_intelligence_graph_read import assemble_intelligence_graph
            from m3_opportunity_identity import OpportunityIdentityResolver

            ident = OpportunityIdentityResolver().resolve_pipeline_row(row, register=False)
            graph = assemble_intelligence_graph(row, identity=ident, opportunity_id=row.get("canonical_id"))
        except Exception:
            graph = {}

        product = graph.get("product_identity") if isinstance(graph.get("product_identity"), dict) else {}
        supplier = graph.get("supplier_intelligence") if isinstance(graph.get("supplier_intelligence"), dict) else {}
        economics = graph.get("economics") if isinstance(graph.get("economics"), dict) else {}
        decision = graph.get("decision") if isinstance(graph.get("decision"), dict) else {}
        pf = product.get("facts") if isinstance(product.get("facts"), dict) else {}
        ef = economics.get("facts") if isinstance(economics.get("facts"), dict) else {}
        df = decision.get("facts") if isinstance(decision.get("facts"), dict) else {}

        product_label = (
            pf.get("nsn")
            if pf.get("nsn") not in (None, "", "UNKNOWN")
            else (row.get("title") or "Opportunity")
        )
        if pf.get("nsn") not in (None, "", "UNKNOWN") and row.get("title"):
            product_label = f"{pf.get('nsn')} — {(row.get('title') or '')[:60]}"

        value = ef.get("revenue") if ef.get("revenue") not in (None, "", "UNKNOWN") else "UNKNOWN"
        next_action = df.get("next_action") or row.get("pending_next_action") or "Review"
        if isinstance(next_action, dict):
            next_action = next_action.get("next_action") or "Review"

        cards.append(
            {
                "kind": "M3MarketHuntOpportunityCard",
                "canonical_id": row.get("canonical_id"),
                "product": product_label,
                "agency": row.get("agency") or row.get("buyer") or "UNKNOWN",
                "value": value,
                "confidence": _plain_status(product.get("confidence") or product.get("status") or "UNKNOWN"),
                "product_status": _plain_status(product.get("status")),
                "supplier_status": _plain_status(supplier.get("status")),
                "economics_status": _plain_status(economics.get("status")),
                "supplier_missing": str(supplier.get("status") or "").upper() in {"UNKNOWN", "RESEARCH_REQUIRED"},
                "economics_missing": str(economics.get("status") or "").upper() in {"UNKNOWN", "RESEARCH_REQUIRED"},
                "next_action": next_action if next_action not in (None, "", "UNKNOWN") else "Review opportunity",
                "deadline": row.get("deadline") or "UNKNOWN",
                "handoff_actions": [
                    {"action": "open_deal_room", "label": "Open Deal Room"},
                    {"action": "view_intelligence_graph", "label": "View Intelligence Graph"},
                    {"action": "view_research_queue", "label": "View Research Queue"},
                ],
            }
        )
        if len(cards) >= limit:
            break
    return cards


def build_market_hunt_dashboard(
    *,
    store: Any | None = None,
    rows: list[dict[str, Any]] | None = None,
    demand_bundle: dict[str, Any] | None = None,
    plan_bundle: dict[str, Any] | None = None,
    research_bundle: dict[str, Any] | None = None,
    supplier_view: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Operator Market Hunt dashboard.

    Question: What should I spend my time on today?
    """
    if store is None and rows is None:
        from m3_pipeline_store import M3PipelineStore

        store = M3PipelineStore()
    if rows is None:
        rows = list(store.all()) if store is not None and hasattr(store, "all") else []

    if demand_bundle is None:
        from m3_demand_signal import build_demand_signals

        demand_bundle = build_demand_signals(rows=rows, persist=False, limit=150)

    if plan_bundle is None:
        from m3_discovery_planner import build_discovery_plans

        plan_bundle = build_discovery_plans(
            signals=list(demand_bundle.get("signals") or []),
            rows=rows,
            max_plans=25,
            persist=False,
            include_draft=True,
        )

    if research_bundle is None:
        from m3_research_queue import build_research_queue
        from m3_opportunity_identity import OpportunityIdentityResolver

        research_bundle = build_research_queue(
            rows=rows,
            identity_resolver=OpportunityIdentityResolver(),
            status_index={"by_key": {}},
            limit=40,
        )

    if supplier_view is None:
        from m3_supplier_product_graph import build_supplier_product_graph_view

        supplier_view = build_supplier_product_graph_view(rows=rows, limit=150)

    products = _product_watch_cards(demand_bundle, plan_bundle, supplier_view)
    plans = _discovery_plan_cards(plan_bundle)
    research = _research_queue_cards(research_bundle)
    opportunities = _opportunity_cards(rows, limit=12)

    # Today focus — one plain sentence
    focus = "Review opportunities and research gaps."
    if research:
        focus = f"Priority: {research[0].get('gap_label')} — {research[0].get('action_label')}"
    elif plans:
        focus = f"Priority: run or review search plan for {plans[0].get('product_label')}"
    elif products:
        focus = f"Priority: watch {products[0].get('product_label')}"

    return {
        "kind": "M3MarketHuntDashboard",
        "build": BUILD_TAG,
        "question": "What should I spend my time on today?",
        "focus": focus,
        "generated_at": _utc(),
        "sections": {
            "products_to_watch": {
                "title": "Products to watch",
                "subtitle": "Markets with demand evidence",
                "count": len(products),
                "items": products,
            },
            "discovery_plans": {
                "title": "Search plans",
                "subtitle": "Where to look next (not auto-run)",
                "count": len(plans),
                "items": plans,
            },
            "research_queue": {
                "title": "Missing information",
                "subtitle": "What blocks a go/no-go decision",
                "count": len(research),
                "items": research,
            },
            "opportunities": {
                "title": "Live opportunities",
                "subtitle": "Simple status — open deal room for detail",
                "count": len(opportunities),
                "items": opportunities,
            },
        },
        # Flat aliases for mobile
        "products_to_watch": products,
        "discovery_plans": plans,
        "research_queue": research,
        "opportunities": opportunities,
        "empty": not (products or plans or research or opportunities),
        "read_only": True,
        "engines_unchanged": True,
    }
