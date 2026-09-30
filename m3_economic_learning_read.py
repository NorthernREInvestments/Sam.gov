"""BUILD 17 — Economic Intelligence + Learning Loop Integration (read models).

Connects demand, supplier commercial, execution, and capital evidence into
economic decision support, institutional memory, search, traces, and monitoring.

Does NOT invent prices/margins/financeability, create numeric scores, or
modify scoring engines. UNKNOWN remains UNKNOWN. Append-only history only.
"""

from __future__ import annotations

import json
import re
from typing import Any

from application_clock import now_utc

BUILD_TAG = "20260919-m3-economic-learning-1"

ST_UNKNOWN = "UNKNOWN"
ST_NOT_READY = "NOT_READY"
ST_PARTIAL = "PARTIAL"
ST_COMPLETE = "COMPLETE"
ST_DETECTED = "DETECTED"

LEARNING_INDEX_KEY = "m3_operational_learning_v1"
OUTCOME_INDEX_KEY = "m3_pursuit_outcome_v1"
SUPPLIER_PERF_KEY = "m3_supplier_performance_memory_v1"
TRACE_INDEX_KEY = "m3_decision_trace_v1"
MONITOR_INDEX_KEY = "m3_monitoring_events_v1"


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


def _known(v: Any) -> bool:
    u = _unwrap(v)
    return u not in (None, "", "UNKNOWN")


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


def _append(key: str, entry: dict[str, Any], *, by: str | None = None, cap: int = 500) -> dict[str, Any]:
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


# ---------------------------------------------------------------------------
# BUILD 1 — Economic Intelligence Layer
# ---------------------------------------------------------------------------
def build_economic_profile(row: dict[str, Any]) -> dict[str, Any]:
    """Combine demand + supplier commercial + execution + capital evidence."""
    de = _as_dict(row.get("deal_economics"))
    profile = _as_dict(de.get("DEAL_ECONOMICS_PROFILE") or de)
    exec_i = _as_dict(row.get("execution_intelligence"))
    cash = _as_dict(row.get("cash_survival"))
    si = _as_dict(row.get("supplier_intelligence"))
    pricing = _as_dict(si.get("Pricing_Evidence") or {})

    # Prefer SCO commercial terms when present
    try:
        from m3_supplier_capital_ops_read import build_supplier_commercial_terms, build_financing_path_intelligence

        terms = build_supplier_commercial_terms(row)
        fin = build_financing_path_intelligence(row)
    except Exception:
        terms = {"suppliers": []}
        fin = {"paths": [], "cash_requirements": {}, "is_financeable_claim": False}

    quote_price = "UNKNOWN"
    quote_ev = "UNKNOWN"
    for s in terms.get("suppliers") or []:
        q = _as_dict(s.get("quote"))
        pv = _unwrap(q.get("price"))
        if _known(pv):
            quote_price = pv
            quote_ev = f"supplier_quote:{s.get('supplier_name')}"
            break

    revenue = fact(
        profile.get("Contract_Value")
        or de.get("Contract_Value")
        or row.get("award_amount")
        or exec_i.get("Contract_Value")
        or row.get("estimated_value"),
        source="deal_economics|award|pipeline",
        evidence="Government demand / contract value field — not a margin",
        status=ST_DETECTED if _known(profile.get("Contract_Value") or row.get("award_amount")) else ST_UNKNOWN,
        confidence=str(de.get("PRICE_CONFIDENCE") or "UNKNOWN"),
    )
    supplier_cost = fact(
        quote_price
        if quote_price != "UNKNOWN"
        else (
            profile.get("Required_Acquisition_Cost")
            or exec_i.get("Estimated_Capital_Needed")
            or pricing.get("amount")
        ),
        source=quote_ev if quote_price != "UNKNOWN" else "deal_economics|supplier_intelligence",
        evidence="Supplier cost evidence only when quoted or mapped — never invented",
        status=ST_DETECTED if quote_price != "UNKNOWN" or _known(profile.get("Required_Acquisition_Cost")) else ST_UNKNOWN,
        confidence=str(de.get("PRICE_CONFIDENCE") or "UNKNOWN"),
    )
    freight = fact(
        cash.get("shipping_cost") or cash.get("freight") or profile.get("freight") or row.get("freight_cost"),
        source="cash_survival|deal_economics",
        evidence="Freight evidenced only when recorded",
        status=ST_DETECTED if _known(cash.get("shipping_cost") or cash.get("freight")) else ST_UNKNOWN,
    )
    packaging = fact(
        cash.get("packaging_cost") or profile.get("packaging_cost") or row.get("packaging_cost"),
        source="cash_survival|pipeline",
        evidence="Packaging cost UNKNOWN unless evidenced",
    )
    insurance = fact(
        cash.get("insurance_cost") or profile.get("insurance_cost") or row.get("insurance_cost"),
        source="cash_survival|pipeline",
        evidence="Insurance cost UNKNOWN unless evidenced",
    )
    financing_req = fact(
        (_as_dict(fin.get("cash_requirements")).get("supplier_payment") or {}).get("value")
        if isinstance(fin.get("cash_requirements"), dict)
        else cash.get("financing_requirements"),
        source="financing_path_intelligence|cash_survival",
        evidence="Financing requirement label/amount — not an approval",
        status=ST_DETECTED
        if _known((_as_dict((_as_dict(fin.get("cash_requirements")).get("supplier_payment"))).get("value")))
        else ST_UNKNOWN,
    )
    other_costs = fact(
        cash.get("other_execution_costs") or profile.get("other_costs"),
        source="cash_survival",
        evidence="Other execution costs UNKNOWN unless evidenced",
    )

    cost_fields = {
        "revenue": revenue,
        "supplier_cost": supplier_cost,
        "freight": freight,
        "packaging": packaging,
        "insurance": insurance,
        "financing_requirements": financing_req,
        "other_execution_costs": other_costs,
    }
    unknown_costs = [k for k, v in cost_fields.items() if v.get("value") == "UNKNOWN"]
    known_n = len(cost_fields) - len(unknown_costs)
    if known_n == 0:
        status = ST_NOT_READY
    elif unknown_costs:
        status = ST_PARTIAL
    else:
        status = ST_COMPLETE

    return {
        "kind": "M3EconomicProfile",
        "opportunity_id": row.get("canonical_id") or "UNKNOWN",
        "status": status,
        "fields": cost_fields,
        "unknown_costs": unknown_costs,
        "margin_invented": False,
        "is_financeable_claim": False,
        "note": "Combines evidenced layers only — never invents margins or financeability",
        "questions": [
            "What do we know?",
            "What do we not know?",
            "What will this likely require to execute?",
        ],
        "fabricated": False,
    }


# ---------------------------------------------------------------------------
# BUILD 2 — Price Evidence Model
# ---------------------------------------------------------------------------
def build_price_evidence_model(row: dict[str, Any]) -> dict[str, Any]:
    gov_prices = []
    demand = _as_dict(row.get("demand_signal") or row.get("demand_signals"))
    awards = row.get("award_history") or demand.get("awards") or []
    for a in awards if isinstance(awards, list) else []:
        if not isinstance(a, dict):
            continue
        gov_prices.append(
            {
                "award_price": a.get("unit_price") or a.get("amount") or a.get("price") or "UNKNOWN",
                "quantity": a.get("quantity") or "UNKNOWN",
                "date": a.get("date") or a.get("award_date") or "UNKNOWN",
                "agency": a.get("agency") or "UNKNOWN",
                "buyer": a.get("buyer") or a.get("agency") or "UNKNOWN",
                "source": a.get("source") or "award_history",
                "evidence": fact(
                    a.get("award_id") or a.get("contract_number"),
                    source="award_history",
                    evidence="Historical award price — context required before comparison",
                    status=ST_DETECTED if _known(a.get("unit_price") or a.get("amount")) else ST_UNKNOWN,
                ),
            }
        )

    supplier_prices = []
    try:
        from m3_supplier_capital_ops_read import build_supplier_commercial_terms

        terms = build_supplier_commercial_terms(row)
        for s in terms.get("suppliers") or []:
            q = _as_dict(s.get("quote"))
            supplier_prices.append(
                {
                    "quote": _unwrap(q.get("price")),
                    "date": _unwrap(q.get("date")),
                    "quantity": _unwrap(q.get("quantity")),
                    "supplier": s.get("supplier_name"),
                    "expiration": _unwrap(q.get("expiration")),
                    "terms": _as_dict(s.get("terms")),
                    "status": q.get("status") or ST_UNKNOWN,
                    "evidence": _as_dict(q.get("price")).get("evidence") or "supplier_commercial_terms",
                }
            )
    except Exception:
        pass

    commercial = []
    si = _as_dict(row.get("supplier_intelligence"))
    for item in (_as_dict(si.get("Pricing_Evidence")).get("items") or []):
        if isinstance(item, dict):
            commercial.append(
                {
                    "source": item.get("Source") or item.get("source") or "UNKNOWN",
                    "date": item.get("date") or "UNKNOWN",
                    "amount": item.get("amount") or "UNKNOWN",
                    "level": item.get("level") or "UNKNOWN",
                    "evidence": fact(item.get("amount"), source=str(item.get("Source") or "commercial"), status=ST_DETECTED if _known(item.get("amount")) else ST_UNKNOWN),
                }
            )

    # Context deltas — never raw comparison as conclusion
    context = {
        "quantity_differences": fact(
            "COMPARE_WITH_CONTEXT" if (gov_prices or supplier_prices) else "UNKNOWN",
            evidence="Quantity must be compared before using price evidence",
        ),
        "configuration_differences": fact("UNKNOWN", evidence="Configuration differences not auto-resolved"),
        "date_differences": fact("UNKNOWN", evidence="Price dates must be considered"),
        "requirement_differences": fact("UNKNOWN", evidence="Requirement differences not auto-resolved"),
    }

    return {
        "kind": "M3PriceEvidenceModel",
        "opportunity_id": row.get("canonical_id") or "UNKNOWN",
        "government_historical_prices": gov_prices[:20],
        "supplier_pricing": supplier_prices[:20],
        "commercial_pricing": commercial[:20],
        "comparison_context": context,
        "never_compare_without_context": True,
        "invents_prices": False,
        "note": "Never compare prices without quantity/config/date/requirement context",
    }


# ---------------------------------------------------------------------------
# BUILD 3 — Economic Uncertainty Tracking
# ---------------------------------------------------------------------------
_UNKNOWN_IMPACT = {
    "freight": "Affects landed cost and cash timing",
    "supplier_terms": "Affects cash exposure and PO acceptance",
    "packaging": "Affects compliance cost and acceptance risk",
    "acceptance_timing": "Affects invoice/payment cash gap",
    "insurance": "Affects risk transfer cost",
    "supplier_cost": "Blocks economic completeness",
    "revenue": "Blocks economic completeness",
    "financing_requirements": "Blocks capital path clarity",
    "other_execution_costs": "May understate cash need",
}


def build_economic_unknowns(row: dict[str, Any]) -> dict[str, Any]:
    econ = build_economic_profile(row)
    items = []
    for key in econ.get("unknown_costs") or []:
        items.append(
            {
                "unknown": key,
                "impact": _UNKNOWN_IMPACT.get(key, "May affect execute/survive decision"),
                "required_action": f"Obtain evidenced {key.replace('_', ' ')}",
                "owner": "RESEARCHER" if key in {"freight", "packaging", "insurance", "supplier_cost"} else "MANAGER",
                "status": ST_UNKNOWN,
            }
        )
    # Extra operational unknowns from SCO/execution
    try:
        from m3_supplier_capital_ops_read import build_supplier_commercial_terms

        terms = build_supplier_commercial_terms(row)
        if not any(_known(_as_dict(s.get("terms")).get("net_terms")) for s in (terms.get("suppliers") or [])):
            items.append(
                {
                    "unknown": "supplier_terms",
                    "impact": _UNKNOWN_IMPACT["supplier_terms"],
                    "required_action": "Obtain supplier payment terms with evidence",
                    "owner": "RESEARCHER",
                    "status": ST_UNKNOWN,
                }
            )
    except Exception:
        pass

    cash = _as_dict(row.get("cash_survival"))
    if not _known(cash.get("acceptance_timing")):
        items.append(
            {
                "unknown": "acceptance_timing",
                "impact": _UNKNOWN_IMPACT["acceptance_timing"],
                "required_action": "Document acceptance timing from contract/solicitation",
                "owner": "MANAGER",
                "status": ST_UNKNOWN,
            }
        )

    # Deduplicate by unknown key; prioritize cash-blocking first (no scores)
    priority_order = (
        "supplier_cost",
        "revenue",
        "supplier_terms",
        "acceptance_timing",
        "freight",
        "packaging",
        "insurance",
        "financing_requirements",
        "other_execution_costs",
    )
    seen: set[str] = set()
    ordered = []
    by_key = {i["unknown"]: i for i in items}
    for k in priority_order:
        if k in by_key and k not in seen:
            ordered.append(by_key[k])
            seen.add(k)
    for i in items:
        if i["unknown"] not in seen:
            ordered.append(i)
            seen.add(i["unknown"])

    return {
        "kind": "M3EconomicUnknown",
        "opportunity_id": row.get("canonical_id") or "UNKNOWN",
        "unknowns": ordered,
        "matters_most": [u["unknown"] for u in ordered[:3]] or ["UNKNOWN"],
        "numeric_score": None,
        "note": "Identifies what information matters most — no scores",
    }


# ---------------------------------------------------------------------------
# BUILD 4 — Learning Loop
# ---------------------------------------------------------------------------
def record_operational_learning(payload: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
    entry = {
        "opportunity": payload.get("opportunity") or payload.get("opportunity_id") or "UNKNOWN",
        "product": payload.get("product") or "UNKNOWN",
        "supplier": payload.get("supplier") or "UNKNOWN",
        "agency": payload.get("agency") or "UNKNOWN",
        "outcome": payload.get("outcome") or "UNKNOWN",
        "problems": payload.get("problems") or ["UNKNOWN"],
        "solutions": payload.get("solutions") or ["UNKNOWN"],
        "reusable_knowledge": payload.get("reusable_knowledge") or ["UNKNOWN"],
        "evidence": payload.get("evidence") if payload.get("evidence") not in (None, "") else "UNKNOWN",
        "recorded_at": _utc(),
        "overwrite_forbidden": True,
        "fabricated_score": False,
    }
    if persist:
        _append(LEARNING_INDEX_KEY, entry, by=str(entry["opportunity"]))
    return entry


def build_operational_learning(
    row: dict[str, Any] | None = None,
    *,
    limit: int = 40,
) -> dict[str, Any]:
    idx = _load_index(LEARNING_INDEX_KEY)
    entries = [e for e in (idx.get("entries") or []) if isinstance(e, dict)]
    local = []
    if isinstance(row, dict):
        local = [e for e in (_as_dict(row.get("operational_learning")).get("entries") or []) if isinstance(e, dict)]
        cid = row.get("canonical_id")
        if cid:
            entries = [e for e in entries if e.get("opportunity") == cid] + local
        else:
            entries = entries + local
    # Also surface execution OS performance learning without duplicating engine
    try:
        from m3_execution_os_read import build_performance_learning

        cid = (row or {}).get("canonical_id") if isinstance(row, dict) else None
        perf = build_performance_learning(opportunity_id=str(cid) if cid else None, limit=10)
        for e in perf.get("entries") or []:
            entries.append(
                {
                    "opportunity": e.get("opportunity_id") or "UNKNOWN",
                    "product": e.get("product") or "UNKNOWN",
                    "supplier": e.get("supplier") or "UNKNOWN",
                    "agency": e.get("agency") or "UNKNOWN",
                    "outcome": e.get("acceptance_result") or "UNKNOWN",
                    "problems": e.get("problems") or ["UNKNOWN"],
                    "solutions": e.get("resolutions") or ["UNKNOWN"],
                    "reusable_knowledge": e.get("lessons_learned") or ["UNKNOWN"],
                    "evidence": "performance_learning",
                    "recorded_at": e.get("recorded_at") or "UNKNOWN",
                    "source_layer": "execution_os_performance_learning",
                }
            )
    except Exception:
        pass
    entries.sort(key=lambda e: str(e.get("recorded_at") or ""), reverse=True)
    return {
        "kind": "M3OperationalLearning",
        "entries": entries[:limit],
        "append_only": True,
        "overwrite_forbidden": True,
        "question": "What happened last time?",
    }


# ---------------------------------------------------------------------------
# BUILD 5 — Win/Loss Memory
# ---------------------------------------------------------------------------
def record_pursuit_outcome(payload: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
    decision = str(payload.get("decision") or "UNKNOWN").upper()
    if decision not in {"PURSUED", "NOT_PURSUED", "UNKNOWN"}:
        # normalize common forms
        if decision in {"PURSUE", "YES", "TRUE"}:
            decision = "PURSUED"
        elif decision in {"NO", "FALSE", "SKIP", "PASS"}:
            decision = "NOT_PURSUED"
        else:
            decision = "UNKNOWN"
    reason = payload.get("reason")
    if reason in (None, ""):
        reason = "UNKNOWN"
    entry = {
        "opportunity_id": payload.get("opportunity_id") or "UNKNOWN",
        "decision": decision,
        "submitted": payload.get("submitted") if payload.get("submitted") is not None else "UNKNOWN",
        "won": payload.get("won") if payload.get("won") is not None else "UNKNOWN",
        "lost": payload.get("lost") if payload.get("lost") is not None else "UNKNOWN",
        "reason": reason,
        "evidence": payload.get("evidence") if payload.get("evidence") not in (None, "") else "UNKNOWN",
        "recorded_at": _utc(),
        "inferred_reason": False,
        "ranking_created": False,
    }
    if persist:
        _append(OUTCOME_INDEX_KEY, entry, by=str(entry["opportunity_id"]))
    return entry


def build_pursuit_outcomes(row: dict[str, Any] | None = None, *, limit: int = 40) -> dict[str, Any]:
    idx = _load_index(OUTCOME_INDEX_KEY)
    entries = [e for e in (idx.get("entries") or []) if isinstance(e, dict)]
    if isinstance(row, dict) and row.get("canonical_id"):
        local = [e for e in (_as_dict(row.get("pursuit_outcomes")).get("entries") or []) if isinstance(e, dict)]
        cid = row.get("canonical_id")
        entries = [e for e in entries if e.get("opportunity_id") == cid] + local
    return {
        "kind": "M3PursuitOutcome",
        "entries": entries[:limit],
        "note": "Only documented reasons — do not infer or rank",
        "rankings_forbidden": True,
    }


# ---------------------------------------------------------------------------
# BUILD 6 — Supplier Performance Memory
# ---------------------------------------------------------------------------
def record_supplier_performance(payload: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
    entry = {
        "supplier": payload.get("supplier") or "UNKNOWN",
        "opportunity_id": payload.get("opportunity_id") or "UNKNOWN",
        "orders": payload.get("orders") or "UNKNOWN",
        "delivery_results": payload.get("delivery_results") or "UNKNOWN",
        "documentation_results": payload.get("documentation_results") or "UNKNOWN",
        "issue_history": payload.get("issue_history") or ["UNKNOWN"],
        "resolution_history": payload.get("resolution_history") or ["UNKNOWN"],
        "repeat_usage": payload.get("repeat_usage") or "UNKNOWN",
        "evidence": payload.get("evidence") if payload.get("evidence") not in (None, "") else "UNKNOWN",
        "recorded_at": _utc(),
        "score": None,
        "rating": None,
    }
    if persist:
        _append(SUPPLIER_PERF_KEY, entry, by=str(entry["supplier"]))
    return entry


def build_supplier_performance_memory(
    row: dict[str, Any] | None = None,
    *,
    supplier: str | None = None,
    limit: int = 40,
) -> dict[str, Any]:
    idx = _load_index(SUPPLIER_PERF_KEY)
    entries = [e for e in (idx.get("entries") or []) if isinstance(e, dict)]
    # Merge SCO relationship memory as factual history (no scores)
    try:
        from m3_supplier_capital_ops_read import build_supplier_relationship_memory

        rel = build_supplier_relationship_memory(row, supplier_name=supplier, limit=20)
        for e in rel.get("entries") or []:
            entries.append(
                {
                    "supplier": e.get("supplier_name") or "UNKNOWN",
                    "opportunity_id": e.get("opportunity_id") or "UNKNOWN",
                    "orders": "UNKNOWN",
                    "delivery_results": "UNKNOWN",
                    "documentation_results": "UNKNOWN",
                    "issue_history": [e.get("notes") or "UNKNOWN"],
                    "resolution_history": [e.get("outcome") or "UNKNOWN"],
                    "repeat_usage": "UNKNOWN",
                    "evidence": e.get("evidence") or "supplier_relationship_memory",
                    "recorded_at": e.get("date") or "UNKNOWN",
                    "source_layer": "supplier_relationship_memory",
                    "score": None,
                    "rating": None,
                }
            )
    except Exception:
        pass
    if supplier:
        entries = [e for e in entries if e.get("supplier") == supplier]
    elif isinstance(row, dict) and row.get("canonical_id"):
        entries = [e for e in entries if e.get("opportunity_id") == row.get("canonical_id")]
    return {
        "kind": "M3SupplierPerformanceMemory",
        "entries": entries[:limit],
        "scores_forbidden": True,
        "ratings_forbidden": True,
        "note": "Factual history only — no supplier scores or ratings",
    }


# ---------------------------------------------------------------------------
# BUILD 7 — Intelligence Search Layer
# ---------------------------------------------------------------------------
def search_intelligence(
    query: str,
    *,
    store: Any | None = None,
    rows: list[dict[str, Any]] | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    q = str(query or "").strip().lower()
    if not q:
        return {
            "kind": "M3IntelligenceSearch",
            "query": "",
            "results": [],
            "note": "Empty query — no matches",
        }
    if rows is None:
        if store is None:
            from m3_pipeline_store import M3PipelineStore

            store = M3PipelineStore()
        rows = list(store.all()) if hasattr(store, "all") else []

    results = []
    tokens = [t for t in re.split(r"\s+", q) if t]

    def _match_blob(blob: str, label: str, record: dict[str, Any], evidence: str) -> None:
        blob_l = blob.lower()
        hits = [t for t in tokens if t in blob_l]
        if not hits:
            return
        results.append(
            {
                "type": label,
                "why_matched": f"Matched tokens: {', '.join(hits)}",
                "evidence": evidence,
                "related_records": record,
                "opportunity_id": record.get("canonical_id") or record.get("opportunity_id") or "UNKNOWN",
            }
        )

    for r in rows:
        if not isinstance(r, dict):
            continue
        cid = r.get("canonical_id") or "UNKNOWN"
        _match_blob(
            " ".join(str(r.get(k) or "") for k in ("title", "description", "solicitation", "agency", "canonical_id")),
            "opportunity",
            {"canonical_id": cid, "title": r.get("title"), "agency": r.get("agency")},
            "pipeline opportunity fields",
        )
        pi = _as_dict(r.get("product_identity"))
        struct = _as_dict(r.get("dla_product_structure"))
        fields = _as_dict(struct.get("fields"))
        prod_blob = " ".join(
            str(_unwrap(fields.get(k)) or _unwrap(pi.get(k)) or struct.get(k) or "")
            for k in ("nsn", "part_number", "oem", "manufacturer", "cage")
        )
        _match_blob(prod_blob, "product", {"canonical_id": cid, "product": prod_blob}, "product_identity|dla_product_structure")

        spg = _as_dict(r.get("supplier_product_graph"))
        for e in spg.get("edges") or []:
            if isinstance(e, dict):
                _match_blob(
                    str(e.get("supplier_name") or ""),
                    "supplier",
                    {"canonical_id": cid, "supplier": e.get("supplier_name"), "relationship": e.get("relationship_type")},
                    "supplier_product_graph",
                )

        for d in r.get("documents") or []:
            if isinstance(d, dict):
                _match_blob(
                    " ".join(str(d.get(k) or "") for k in ("filename", "document_type", "extracted_text")),
                    "document",
                    {"canonical_id": cid, "filename": d.get("filename")},
                    "documents",
                )

        # Requirements / actions from offer readiness lightweight
        _match_blob(str(r.get("next_action") or ""), "action", {"canonical_id": cid, "action": r.get("next_action")}, "pipeline.next_action")

    # Lessons from learning indexes
    for e in (_load_index(LEARNING_INDEX_KEY).get("entries") or [])[:200]:
        if not isinstance(e, dict):
            continue
        blob = " ".join(str(e.get(k) or "") for k in ("product", "supplier", "agency", "outcome", "reusable_knowledge", "problems"))
        _match_blob(blob, "lesson", e, "operational_learning")

    # Deduplicate by type+opportunity+why
    seen: set[str] = set()
    uniq = []
    for r in results:
        key = f"{r['type']}|{r.get('opportunity_id')}|{r['why_matched']}"
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)

    return {
        "kind": "M3IntelligenceSearch",
        "query": query,
        "results": uniq[:limit],
        "count": len(uniq[:limit]),
        "fabricated": False,
        "note": "Cross-system search — why matched + evidence required",
    }


# ---------------------------------------------------------------------------
# BUILD 8 — Knowledge Trace
# ---------------------------------------------------------------------------
def build_decision_trace(
    *,
    question: str,
    row: dict[str, Any] | None = None,
    persist: bool = False,
) -> dict[str, Any]:
    row = row if isinstance(row, dict) else {}
    econ = build_economic_profile(row)
    unknowns = build_economic_unknowns(row)
    known_facts = []
    for k, v in (econ.get("fields") or {}).items():
        if isinstance(v, dict) and v.get("value") != "UNKNOWN":
            known_facts.append({"fact": k, "value": v.get("value"), "evidence": v.get("evidence"), "source": v.get("source")})

    evidence_used = [
        {"layer": "economic_profile", "status": econ.get("status")},
        {"layer": "price_evidence", "count_gov": len(build_price_evidence_model(row).get("government_historical_prices") or [])},
    ]
    try:
        from m3_supplier_capital_ops_read import build_execution_readiness_gate

        gate = build_execution_readiness_gate(row)
        evidence_used.append({"layer": "execution_readiness_gate", "decision": gate.get("decision")})
        action = f"Gate decision: {gate.get('decision')} — resolve: {', '.join(unknowns.get('matters_most') or [])}"
    except Exception:
        gate = {}
        action = f"Resolve economic unknowns: {', '.join(unknowns.get('matters_most') or ['UNKNOWN'])}"

    trace = {
        "kind": "M3DecisionTrace",
        "opportunity_id": row.get("canonical_id") or "UNKNOWN",
        "question": question or "What should we do next?",
        "evidence_used": evidence_used,
        "known_facts": known_facts,
        "unknowns": [u.get("unknown") for u in (unknowns.get("unknowns") or [])],
        "generated_action": action,
        "date": _utc(),
        "numeric_score": None,
        "note": "Explainability only — human can see why M3 produced the output",
    }
    if persist:
        _append(TRACE_INDEX_KEY, trace, by=str(trace["opportunity_id"]), cap=300)
    return trace


# ---------------------------------------------------------------------------
# BUILD 9 — Monitoring Foundation
# ---------------------------------------------------------------------------
def build_monitoring_foundation(row: dict[str, Any]) -> dict[str, Any]:
    """Internal change-tracking model — no external integrations."""
    docs = row.get("documents") if isinstance(row.get("documents"), list) else []
    amendments = [
        d
        for d in docs
        if isinstance(d, dict)
        and (str(d.get("document_type") or "").upper() == "AMENDMENT" or d.get("amendment_number"))
    ]
    opportunity_changes = {
        "amendments": fact(len(amendments) if amendments else "UNKNOWN", evidence=f"{len(amendments)} amendment doc(s)" if amendments else "UNKNOWN"),
        "deadlines": fact(row.get("deadline"), source="pipeline", status=ST_DETECTED if row.get("deadline") else ST_UNKNOWN),
        "status_changes": fact(row.get("lifecycle"), source="pipeline", status=ST_DETECTED if row.get("lifecycle") else ST_UNKNOWN),
    }

    supplier_changes = {"quote_expiration": [], "terms_changes": [], "availability_changes": []}
    try:
        from m3_supplier_capital_ops_read import build_supplier_commercial_terms

        terms = build_supplier_commercial_terms(row)
        for s in terms.get("suppliers") or []:
            q = _as_dict(s.get("quote"))
            if _known(_unwrap(q.get("expiration"))):
                supplier_changes["quote_expiration"].append(
                    {"supplier": s.get("supplier_name"), "expiration": _unwrap(q.get("expiration")), "status": q.get("status")}
                )
            for e in (terms.get("history") or [])[:10]:
                if isinstance(e, dict):
                    supplier_changes["terms_changes"].append(e)
    except Exception:
        pass

    contract_changes = {
        "milestones": fact(_as_dict(row.get("award_execution")).get("current_focus") or "UNKNOWN"),
        "acceptance": fact(_as_dict(row.get("payment_readiness")).get("acceptance_evidence") or "UNKNOWN"),
        "payment": fact(_as_dict(row.get("payment_readiness")).get("status") or "UNKNOWN"),
    }

    return {
        "kind": "M3MonitoringFoundation",
        "opportunity_id": row.get("canonical_id") or "UNKNOWN",
        "opportunity": opportunity_changes,
        "supplier": supplier_changes,
        "contract": contract_changes,
        "external_integrations": False,
        "note": "Internal models only — no external monitoring integrations yet",
    }


# ---------------------------------------------------------------------------
# Full profile + attach + command center enrichment
# ---------------------------------------------------------------------------
def build_economic_learning_profile(row: dict[str, Any] | None) -> dict[str, Any]:
    row = row if isinstance(row, dict) else {}
    econ = build_economic_profile(row)
    unknowns = build_economic_unknowns(row)
    trace = build_decision_trace(question="What should we do next?", row=row, persist=False)
    return {
        "kind": "M3EconomicLearningProfile",
        "build": BUILD_TAG,
        "opportunity_id": row.get("canonical_id") or "UNKNOWN",
        "economic_profile": econ,
        "price_evidence": build_price_evidence_model(row),
        "economic_unknowns": unknowns,
        "operational_learning": build_operational_learning(row),
        "pursuit_outcomes": build_pursuit_outcomes(row),
        "supplier_performance": build_supplier_performance_memory(row),
        "decision_trace": trace,
        "monitoring": build_monitoring_foundation(row),
        "questions": [
            "What do we know?",
            "What do we not know?",
            "What will this likely require to execute?",
            "What happened last time?",
            "What should we do next?",
        ],
        "facts_only": True,
        "unknown_preserved": True,
        "no_fabricated_margins": True,
        "no_fabricated_financing": True,
        "no_automatic_conclusions": True,
        "no_numeric_scores": True,
        "engines_unchanged": True,
        "scoring_unchanged": True,
        "generated_at": _utc(),
        "read_only": True,
    }


def attach_economic_learning_to_deal_room(deal: dict[str, Any], *, row: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(deal, dict):
        return deal
    out = dict(deal)
    try:
        profile = build_economic_learning_profile(row or {"canonical_id": deal.get("canonical_id")})
        out["economic_learning"] = profile
    except Exception:
        out["economic_learning"] = {
            "kind": "M3EconomicLearningProfile",
            "build": BUILD_TAG,
            "error": "economic_learning_unavailable",
            "read_only": True,
        }
    return out


def enrich_command_center_economic(
    sections: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    period: str,
) -> dict[str, Any]:
    """BUILD 10 — actionable economic/learning signals for morning/evening."""
    out = dict(sections or {})
    economic_blockers = []
    missing_evidence = []
    pending_decisions = []
    new_intelligence = []
    supplier_actions = list(out.get("supplier_responses") or out.get("supplier_followups") or [])
    expiring = list(out.get("quote_expiration") or out.get("expiring_information") or [])

    for r in rows[:50]:
        if not isinstance(r, dict):
            continue
        cid = r.get("canonical_id")
        title = (r.get("title") or "")[:80]
        econ = build_economic_profile(r)
        unk = build_economic_unknowns(r)
        if econ.get("status") in {ST_NOT_READY, ST_PARTIAL}:
            for u in (unk.get("unknowns") or [])[:3]:
                economic_blockers.append(
                    {
                        "opportunity_id": cid,
                        "title": title,
                        "unknown": u.get("unknown"),
                        "impact": u.get("impact"),
                        "action": u.get("required_action"),
                        "owner": u.get("owner"),
                    }
                )
                missing_evidence.append(
                    {
                        "opportunity_id": cid,
                        "title": title,
                        "missing": u.get("unknown"),
                        "action": u.get("required_action"),
                    }
                )
        # Pending decisions from gate / outcomes
        try:
            from m3_supplier_capital_ops_read import build_execution_readiness_gate

            gate = build_execution_readiness_gate(r)
            if gate.get("decision") in {"RESEARCH_REQUIRED", "EXECUTION_UNKNOWN", "ACCESS_BLOCKED"}:
                pending_decisions.append(
                    {
                        "opportunity_id": cid,
                        "title": title,
                        "decision_needed": gate.get("decision"),
                        "action": "Resolve readiness / economic unknowns",
                    }
                )
        except Exception:
            pass

        mon = build_monitoring_foundation(r)
        if _known(_unwrap(_as_dict(mon.get("opportunity")).get("amendments"))):
            new_intelligence.append(
                {
                    "opportunity_id": cid,
                    "title": title,
                    "what": "amendment_activity",
                    "value": _unwrap(_as_dict(mon.get("opportunity")).get("amendments")),
                }
            )
        learn = build_operational_learning(r, limit=3)
        for e in learn.get("entries") or []:
            new_intelligence.append(
                {
                    "opportunity_id": cid,
                    "title": title,
                    "what": "learning",
                    "value": e.get("reusable_knowledge"),
                }
            )

    if period == "morning":
        out["economic_blockers"] = economic_blockers[:15]
        out["supplier_actions"] = supplier_actions[:15]
        out["expiring_quotes"] = expiring[:15]
        out["missing_evidence"] = missing_evidence[:15]
        out["pending_decisions"] = pending_decisions[:15]
    else:
        out["new_intelligence"] = new_intelligence[:15]
        out["unresolved_blockers"] = list(out.get("unresolved_blockers") or [])[:15] + economic_blockers[:10]
        out["next_priorities"] = list(out.get("next_day_priorities") or out.get("next_priorities") or [])[:15]
        out.setdefault("completed_actions", out.get("completed_actions") or [])
    return out
