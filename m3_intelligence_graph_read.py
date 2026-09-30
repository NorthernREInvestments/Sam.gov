"""BUILD 3 — Intelligence Graph read API.

Read-only assembler over existing M3 intelligence. Does not create engines,
change scoring, modify discovery/enrichment, or invent facts.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from application_clock import now_utc
from m3_opportunity_identity import (
    OpportunityIdentityResolver,
    looks_like_sam_notice_id,
    load_identity_resolver,
)
from m3_product_fact_projection import (
    INTEL_AVAILABLE,
    INTEL_EXECUTION_READY,
    INTEL_POSSIBLE,
    INTEL_RESEARCH_REQUIRED,
    INTEL_UNKNOWN,
    INTEL_VALIDATED,
    map_to_intelligence_state,
)

BUILD_TAG = "20260918-m3-intelligence-graph-read-1"

GRAPH_NODES = (
    "opportunity",
    "requirement",
    "product_identity",
    "supplier_intelligence",
    "historical_intelligence",
    "demand_signals",
    "procurement_path",
    "financing",
    "economics",
    "decision",
)


def _utc() -> str:
    return now_utc().isoformat()


def _clean(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _node(
    *,
    status: str,
    reason: str | None = None,
    confidence: Any = None,
    evidence: Any = None,
    facts: dict[str, Any] | None = None,
    source: str | None = None,
    timestamp: str | None = None,
    missing: list[str] | None = None,
    raw: Any = None,
) -> dict[str, Any]:
    return {
        "status": status or INTEL_UNKNOWN,
        "reason": reason,
        "confidence": confidence if confidence not in (None, "") else "UNKNOWN",
        "evidence": evidence if evidence is not None else [],
        "facts": facts or {},
        "source": source,
        "timestamp": timestamp,
        "missing": missing or [],
        "raw_present": bool(raw),
    }


def _as_dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _evidence_item(*, source: Any, snippet: Any = None, confidence: Any = None, field: str | None = None) -> dict[str, Any]:
    return {
        "field": field,
        "source": source or "UNKNOWN",
        "snippet": snippet,
        "confidence": confidence or "UNKNOWN",
    }


def resolve_pipeline_row(
    opportunity_id: str,
    *,
    store: Any | None = None,
    identity_resolver: OpportunityIdentityResolver | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Resolve any handle → pipeline row + identity. Read-only; may register identity aliases."""
    oid = _clean(opportunity_id)
    resolver = identity_resolver or load_identity_resolver()
    if not oid:
        return None, resolver.resolve(register=False)

    # Strip opp: prefixes for direct pipeline/notice handles
    raw = oid
    if raw.startswith("opp:pipeline:"):
        raw = raw[len("opp:pipeline:") :]
    elif raw.startswith("opp:notice:"):
        raw = raw[len("opp:notice:") :]
    elif raw.startswith("pipeline:"):
        raw = raw[len("pipeline:") :]
    elif raw.startswith("notice:"):
        raw = raw[len("notice:") :]

    pipeline_store = store
    if pipeline_store is None:
        from m3_pipeline_store import M3PipelineStore

        pipeline_store = M3PipelineStore()

    row: dict[str, Any] | None = None
    get = getattr(pipeline_store, "get", None)
    if callable(get):
        row = get(oid) or get(raw)

    # Identity map lookup
    ident = resolver.resolve(
        notice_id=raw if looks_like_sam_notice_id(raw) else None,
        canonical_id=raw if not looks_like_sam_notice_id(raw) else None,
        pipeline_id=raw if not looks_like_sam_notice_id(raw) else None,
        register=True,
    )
    # Also try full opportunity_uid
    if oid.startswith("opp:") and resolver.by_uid.get(oid):
        ident = dict(resolver.by_uid[oid])
        ident.setdefault("resolution_status", "RESOLVED")

    pipe_id = _clean(ident.get("pipeline_canonical_id"))
    if row is None and pipe_id and callable(get):
        row = get(pipe_id)

    notice = _clean(ident.get("notice_id")) or (raw if looks_like_sam_notice_id(raw) else None)
    if row is None and notice and hasattr(pipeline_store, "all"):
        for candidate in pipeline_store.all():
            if not isinstance(candidate, dict):
                continue
            if _clean(candidate.get("notice_id")) == notice or _clean(candidate.get("external_id")) == notice:
                row = candidate
                break
            if _clean(candidate.get("canonical_id")) == raw:
                row = candidate
                break

    if row is not None:
        ident = resolver.resolve_pipeline_row(row, register=True)
    return row, ident


def _assemble_opportunity(row: dict[str, Any], ident: dict[str, Any]) -> dict[str, Any]:
    ts = row.get("updated_at") or row.get("enriched_at") or row.get("discovered_at")
    facts = {
        "canonical_id": row.get("canonical_id"),
        "opportunity_uid": ident.get("opportunity_uid"),
        "notice_id": row.get("notice_id") or ident.get("notice_id"),
        "solicitation_number": row.get("solicitation_number") or ident.get("solicitation_number"),
        "title": row.get("title"),
        "agency": row.get("agency") or row.get("buyer"),
        "source_id": row.get("source_id") or ident.get("source_id"),
        "deadline": row.get("deadline"),
        "lifecycle": row.get("lifecycle"),
        "detail_url": row.get("detail_url"),
        "contract_id": row.get("contract_id") or ident.get("contract_id"),
    }
    status = INTEL_VALIDATED if row.get("canonical_id") else INTEL_UNKNOWN
    return _node(
        status=status,
        reason=None if status == INTEL_VALIDATED else "Opportunity not found in pipeline",
        confidence="HIGH" if status == INTEL_VALIDATED else "UNKNOWN",
        evidence=[_evidence_item(source="pipeline_store", field="canonical_id", confidence="HIGH")],
        facts=facts,
        source="pipeline_store+opportunity_identity",
        timestamp=str(ts) if ts else None,
        raw=row.get("canonical_id"),
    )


def _assemble_requirement(row: dict[str, Any]) -> dict[str, Any]:
    struct = _as_dict(row.get("dla_product_structure"))
    fields = _as_dict(struct.get("fields"))
    bom = row.get("line_items") or row.get("bom") or []
    readiness = str(row.get("readiness_state") or "")
    qty = struct.get("quantity")
    uoi = struct.get("unit_of_issue")
    if isinstance(fields.get("quantity"), dict):
        qty = qty if qty not in (None, "") else fields["quantity"].get("value")
    if isinstance(fields.get("unit_of_issue"), dict):
        uoi = uoi if uoi not in (None, "") else fields["unit_of_issue"].get("value")

    evidence = []
    for key in ("quantity", "unit_of_issue", "nsn", "part_number"):
        wrap = fields.get(key)
        if isinstance(wrap, dict) and wrap.get("value") not in (None, ""):
            evidence.append(
                _evidence_item(
                    field=key,
                    source=wrap.get("evidence_source") or "dla_product_structure",
                    snippet=wrap.get("evidence_snippet"),
                    confidence=wrap.get("confidence") or "HIGH",
                )
            )

    missing = []
    if qty in (None, "", "UNKNOWN"):
        missing.append("quantity")
    if uoi in (None, "", "UNKNOWN"):
        missing.append("unit_of_issue")
    if not evidence and not bom:
        missing.append("requirement_structure")

    if evidence or (isinstance(bom, list) and bom):
        status = map_to_intelligence_state(readiness) if readiness else INTEL_VALIDATED
        if status == INTEL_UNKNOWN:
            status = INTEL_VALIDATED
        reason = None
    else:
        status = INTEL_RESEARCH_REQUIRED
        reason = "Requirement structure not recovered"

    return _node(
        status=status,
        reason=reason,
        confidence="HIGH" if evidence else "UNKNOWN",
        evidence=evidence,
        facts={
            "quantity": qty if qty not in (None, "") else "UNKNOWN",
            "unit_of_issue": uoi if uoi not in (None, "") else "UNKNOWN",
            "nsn": struct.get("nsn") or "UNKNOWN",
            "part_number": struct.get("part_number") or "UNKNOWN",
            "bom_line_count": len(bom) if isinstance(bom, list) else 0,
            "package_access": row.get("package_access") or "UNKNOWN",
            "readiness_state": readiness or "UNKNOWN",
        },
        source="dla_product_structure|line_items",
        timestamp=_clean(row.get("enriched_at") or row.get("updated_at")),
        missing=missing,
        raw=struct or bom,
    )


def _assemble_product_identity(row: dict[str, Any]) -> dict[str, Any]:
    ident = _as_dict(row.get("product_identity"))
    struct = _as_dict(row.get("dla_product_structure"))
    fields = _as_dict(struct.get("fields"))
    projection = _as_dict(row.get("product_fact_projection"))
    state = str(ident.get("identity_state") or "")
    status = map_to_intelligence_state(state or INTEL_UNKNOWN)

    evidence = []
    facts: dict[str, Any] = {
        "identity_state": state or INTEL_UNKNOWN,
        "nsn": "UNKNOWN",
        "part_number": "UNKNOWN",
        "manufacturer": "UNKNOWN",
        "cage": "UNKNOWN",
        "knowledge_product_id": row.get("knowledge_product_id") or projection.get("knowledge_product_id"),
    }

    for key, fact_key in (
        ("nsn", "nsn"),
        ("part_number", "part_number"),
        ("cage", "cage"),
        ("manufacturer", "manufacturer"),
    ):
        wrap = fields.get(key)
        val = struct.get(key)
        if isinstance(wrap, dict) and wrap.get("value") not in (None, ""):
            val = wrap.get("value")
            evidence.append(
                _evidence_item(
                    field=fact_key,
                    source=wrap.get("evidence_source") or "product_identity",
                    snippet=wrap.get("evidence_snippet"),
                    confidence=wrap.get("confidence") or "HIGH",
                )
            )
        if val not in (None, ""):
            facts[fact_key] = val

    # Commercial match only if already on row (no matching engine call)
    match = _as_dict(row.get("commercial_product_matching") or row.get("commercial_match"))
    if match:
        facts["commercial_match"] = {
            "status": match.get("match_status") or match.get("status") or "UNKNOWN",
            "confidence": match.get("confidence") or "UNKNOWN",
            "sku": match.get("sku") or match.get("matched_sku"),
        }
        if str(match.get("confidence") or "").upper() in {"HIGH", "VALIDATED"}:
            status = INTEL_VALIDATED if status in {INTEL_UNKNOWN, INTEL_POSSIBLE} else status

    if status in {INTEL_UNKNOWN, INTEL_POSSIBLE} and not evidence:
        reason = "No validated product identity"
        if state in {"AMBIGUOUS", "CATEGORY_IDENTIFIED", "SPEC_IDENTIFIED"}:
            reason = f"Product identity remains {state}"
            status = INTEL_POSSIBLE if status == INTEL_UNKNOWN else status
    elif status == INTEL_UNKNOWN and evidence:
        status = INTEL_VALIDATED
        reason = None
    else:
        reason = None

    return _node(
        status=status,
        reason=reason,
        confidence=(evidence[0]["confidence"] if evidence else (ident.get("confidence") or "UNKNOWN")),
        evidence=evidence,
        facts=facts,
        source="product_identity|dla_product_structure|product_fact_projection",
        timestamp=_clean(projection.get("projected_at") or row.get("updated_at")),
        missing=[k for k in ("nsn", "part_number", "manufacturer", "cage") if facts.get(k) == "UNKNOWN"],
        raw=ident or struct,
    )


def _read_supplier_blob(row: dict[str, Any]) -> dict[str, Any]:
    """Prefer persisted / row supplier packages. Never call build_supplier_intelligence."""
    row_si = _as_dict(row.get("supplier_intelligence"))
    cid = _clean(row.get("canonical_id"))
    persisted: dict[str, Any] = {}
    if cid:
        try:
            from m3_supplier_intelligence import get_persisted_supplier_intelligence

            p = get_persisted_supplier_intelligence(cid)
            if isinstance(p, dict):
                persisted = p
        except Exception:
            persisted = {}
    # Prefer kind-tagged full packages
    for blob in (row_si, persisted):
        if blob.get("kind") == "M3SupplierIntelligence":
            return blob
    return row_si or persisted


def _assemble_supplier(row: dict[str, Any]) -> dict[str, Any]:
    si = _read_supplier_blob(row)
    graph_ann = _as_dict(row.get("supplier_product_graph"))
    graph_edges = list(graph_ann.get("edges") or [])

    if not si and not graph_edges:
        return _node(
            status=INTEL_UNKNOWN,
            reason="No validated supplier relationship",
            confidence="UNKNOWN",
            evidence=[],
            facts={},
            source=None,
            missing=["supplier_intelligence"],
        )

    supply = _as_dict(si.get("Supply_chain")) if si else {}
    pricing = _as_dict(si.get("Pricing_evidence")) if si else {}
    channels = supply.get("all_channels") or supply.get("channels") or []
    conf = (si or {}).get("ACQUISITION_COST_CONFIDENCE") or (si or {}).get("Cost_Confidence") or pricing.get("primary_level")
    status = map_to_intelligence_state(conf) if si else INTEL_UNKNOWN
    if status == INTEL_UNKNOWN and channels:
        status = INTEL_POSSIBLE
    if str(conf or "").upper() in {"HIGH", "VALIDATED", "LEVEL_1", "LEVEL_2"}:
        status = INTEL_VALIDATED
    if graph_edges and any(str(e.get("confidence") or "").upper() in {"VALIDATED", "HIGH"} for e in graph_edges if isinstance(e, dict)):
        status = INTEL_VALIDATED

    evidence = []
    for item in (pricing.get("items") or [])[:5]:
        if isinstance(item, dict):
            evidence.append(
                _evidence_item(
                    field="pricing",
                    source=item.get("source") or item.get("url") or "supplier_intelligence",
                    snippet=item.get("note") or item.get("price"),
                    confidence=item.get("confidence") or conf or "UNKNOWN",
                )
            )
    for e in graph_edges[:5]:
        if isinstance(e, dict):
            evidence.append(
                _evidence_item(
                    field=str(e.get("relationship_type") or "supplier_edge"),
                    source=e.get("source") or "supplier_product_graph",
                    snippet=e.get("supplier_name"),
                    confidence=e.get("confidence") or "UNKNOWN",
                )
            )

    validated = [
        c
        for c in channels
        if isinstance(c, dict) and str(c.get("validation_status") or c.get("status") or "").upper() in {"VALIDATED", "VERIFIED", "HIGH"}
    ]
    facts = {
        "supplier_count": len(channels) if isinstance(channels, list) else 0,
        "validated_supplier_count": len(validated),
        "graph_edge_count": len(graph_edges),
        "pricing_level": pricing.get("primary_level") or "UNKNOWN",
        "acquisition_cost_confidence": conf or "UNKNOWN",
        "margin_status": (_as_dict((si or {}).get("Margin")).get("margin_status") or (si or {}).get("Margin_Status") or "UNKNOWN"),
        "suppliers": [
            {"company": c.get("company"), "role": c.get("role"), "website": c.get("website")}
            for c in (channels[:8] if isinstance(channels, list) else [])
            if isinstance(c, dict)
        ],
        "supplier_product_edges": [
            {
                "supplier_name": e.get("supplier_name"),
                "relationship_type": e.get("relationship_type"),
                "confidence": e.get("confidence"),
                "source": e.get("source"),
            }
            for e in graph_edges
            if isinstance(e, dict)
        ][:12],
    }
    missing = []
    if not channels and not graph_edges:
        missing.append("supplier_channels")
    if not evidence:
        missing.append("pricing_evidence")

    if status == INTEL_UNKNOWN and not channels and not graph_edges:
        reason = "No validated supplier relationship"
    else:
        reason = None

    return _node(
        status=status,
        reason=reason,
        confidence=conf or ("HIGH" if graph_edges else "UNKNOWN"),
        evidence=evidence,
        facts=facts,
        source="supplier_intelligence|supplier_product_graph",
        timestamp=_clean(graph_ann.get("projected_at") or (si or {}).get("generated_at") or (si or {}).get("updated_at")),
        missing=missing,
        raw=si or graph_ann,
    )


def _assemble_historical(row: dict[str, Any]) -> dict[str, Any]:
    hist = _as_dict(row.get("government_price_history") or row.get("historical_intelligence"))
    commercial = _as_dict(row.get("commercial_research"))
    award = row.get("historical_award_amount")
    unit = row.get("historical_unit_price")
    projection = _as_dict(row.get("award_product_projection") or (row.get("product_fact_projection") or {}).get("award"))
    # BUILD 5 annotation may sit at top-level
    if not projection and isinstance(row.get("award_product_projection"), dict):
        projection = row["award_product_projection"]

    evidence = []
    facts: dict[str, Any] = {
        "historical_award_amount": award if award not in (None, "") else "UNKNOWN",
        "historical_unit_price": unit if unit not in (None, "") else "UNKNOWN",
        "government_historical_price": commercial.get("government_historical_price") or "UNKNOWN",
        "demand_evidence_count": 0,
        "buying_agencies": [],
        "historical_suppliers": [],
        "knowledge_product_ids": [],
    }

    if projection:
        demands = projection.get("demand_evidence") or []
        rels = projection.get("supplier_relationships") or []
        facts["demand_evidence_count"] = len(demands) if isinstance(demands, list) else int(projection.get("demand_count") or 0)
        facts["buying_agencies"] = list(projection.get("buying_agencies") or [])
        facts["knowledge_product_ids"] = list(projection.get("knowledge_product_ids") or [])
        if isinstance(rels, list):
            facts["historical_suppliers"] = [
                r.get("supplier_name") for r in rels if isinstance(r, dict) and r.get("supplier_name")
            ][:8]
        for d in (demands[:5] if isinstance(demands, list) else []):
            if not isinstance(d, dict):
                continue
            evidence.append(
                _evidence_item(
                    field="product_demand",
                    source=d.get("award_source") or "award_product_projection",
                    snippet=f"agency={d.get('agency')} value={d.get('value')} date={d.get('date')}",
                    confidence=d.get("confidence") or "HIGH",
                )
            )

    if hist:
        facts["history_present"] = True
        facts["summary"] = {
            k: hist.get(k)
            for k in ("kind", "Status", "status", "Historical_exact_awards", "Revenue_basis", "confidence")
            if hist.get(k) is not None
        }
        conf = hist.get("confidence") or hist.get("Confidence") or hist.get("Status")
        status = map_to_intelligence_state(conf)
        if award not in (None, "") or unit not in (None, "") or facts["demand_evidence_count"]:
            status = INTEL_VALIDATED if status == INTEL_UNKNOWN else status
            if award not in (None, "") or unit not in (None, ""):
                evidence.append(
                    _evidence_item(
                        field="historical_price",
                        source="government_price_history",
                        snippet=str(award or unit),
                        confidence=conf or "HIGH",
                    )
                )
        reason = None if status != INTEL_UNKNOWN else "Historical package present but inconclusive"
    elif award not in (None, "") or unit not in (None, "") or facts["demand_evidence_count"]:
        status = INTEL_VALIDATED
        reason = None
        if award not in (None, "") or unit not in (None, ""):
            evidence.append(
                _evidence_item(
                    field="historical_award",
                    source="pipeline_row",
                    snippet=str(award or unit),
                    confidence="HIGH",
                )
            )
    else:
        status = INTEL_RESEARCH_REQUIRED
        reason = "No historical award or unit-price evidence"

    return _node(
        status=status,
        reason=reason,
        confidence=(evidence[0]["confidence"] if evidence else "UNKNOWN"),
        evidence=evidence,
        facts=facts,
        source="government_price_history|award_product_projection|historical_award_amount",
        timestamp=_clean(
            projection.get("projected_at")
            or hist.get("generated_at")
            or hist.get("updated_at")
            or row.get("updated_at")
        ),
        missing=[] if evidence else ["historical_award", "historical_unit_price"],
        raw=hist or projection or None,
    )


def _assemble_demand_signals(row: dict[str, Any]) -> dict[str, Any]:
    """BUILD 6 — demand signals attached to product / opportunity (read-only assemble)."""
    try:
        from m3_demand_signal import (
            DETECTED,
            VALIDATED,
            ACTIVE,
            UNKNOWN as DS_UNKNOWN,
            collect_demand_signals_for_row,
        )

        annotated = row.get("demand_signals") if isinstance(row.get("demand_signals"), dict) else {}
        signals = list(annotated.get("signals") or [])
        if not signals:
            signals = collect_demand_signals_for_row(row)
    except Exception:
        signals = []
        annotated = {}

    if not signals:
        return _node(
            status=INTEL_UNKNOWN,
            reason="No demand signals detected",
            confidence="UNKNOWN",
            evidence=[],
            facts={"signal_count": 0, "types": [], "products_to_monitor": []},
            source="demand_signals",
            missing=["demand_signal"],
        )

    statuses = {str(s.get("status") or "").upper() for s in signals if isinstance(s, dict)}
    confs = [str(s.get("confidence") or "UNKNOWN") for s in signals if isinstance(s, dict)]
    if VALIDATED in statuses or ACTIVE in statuses or "HIGH" in {c.upper() for c in confs}:
        status = INTEL_VALIDATED
    elif DETECTED in statuses:
        status = INTEL_POSSIBLE
    else:
        status = INTEL_UNKNOWN

    evidence = []
    for s in signals[:8]:
        if not isinstance(s, dict):
            continue
        ev0 = (s.get("evidence") or [{}])[0] if s.get("evidence") else {}
        evidence.append(
            _evidence_item(
                field=str(s.get("signal_type") or "demand"),
                source=s.get("source") or ev0.get("source") or "demand_signal",
                snippet=ev0.get("snippet") or s.get("expected_timing"),
                confidence=s.get("confidence") or ev0.get("confidence") or "UNKNOWN",
            )
        )

    types = sorted({str(s.get("signal_type")) for s in signals if isinstance(s, dict)})
    return _node(
        status=status,
        reason=None if status != INTEL_UNKNOWN else "Demand signals weak or unknown",
        confidence="HIGH" if status == INTEL_VALIDATED else ("MEDIUM" if status == INTEL_POSSIBLE else "UNKNOWN"),
        evidence=evidence,
        facts={
            "signal_count": len(signals),
            "types": types,
            "signals": [
                {
                    "signal_id": s.get("signal_id"),
                    "signal_type": s.get("signal_type"),
                    "status": s.get("status"),
                    "confidence": s.get("confidence"),
                    "agency": s.get("related_agency"),
                    "expected_timing": s.get("expected_timing"),
                    "monitoring_recipe": s.get("monitoring_recipe"),
                }
                for s in signals
                if isinstance(s, dict)
            ][:12],
        },
        source="m3_demand_signal",
        timestamp=_clean(annotated.get("updated_at") or row.get("updated_at")),
        missing=[],
        raw=signals,
    )


def _assemble_procurement(row: dict[str, Any]) -> dict[str, Any]:
    pkg = _as_dict(row.get("procurement_package") or row.get("product_transaction_readiness"))
    path = _as_dict(row.get("procurement_path") or row.get("selected_price_path") or row.get("price_path"))
    resale = _as_dict(row.get("product_resale_source_intelligence") or row.get("source_intelligence"))

    facts = {
        "package_access": row.get("package_access") or "UNKNOWN",
        "readiness_state": row.get("readiness_state") or pkg.get("readiness_state") or "UNKNOWN",
        "selected_path_id": path.get("selected_path_id") or path.get("path_id") or resale.get("selected_path_id") or "UNKNOWN",
        "configuration_completeness": pkg.get("Configuration_completeness") or pkg.get("completeness") or "UNKNOWN",
    }
    evidence = []
    if facts["selected_path_id"] != "UNKNOWN":
        evidence.append(
            _evidence_item(
                field="procurement_path",
                source="product_resale_source_intelligence|procurement_path",
                snippet=facts["selected_path_id"],
                confidence="HIGH",
            )
        )
    if pkg:
        evidence.append(
            _evidence_item(
                field="procurement_package",
                source="procurement_package",
                snippet=facts["configuration_completeness"],
                confidence=pkg.get("Identity_confidence") or "UNKNOWN",
            )
        )

    if evidence:
        status = INTEL_AVAILABLE if facts["selected_path_id"] != "UNKNOWN" else INTEL_VALIDATED
        reason = None
    elif str(row.get("package_access") or "").upper() in {"AUTH_REQUIRED", "REGISTRATION_REQUIRED", "BOT_PROTECTED"}:
        status = INTEL_RESEARCH_REQUIRED
        reason = f"Package access blocked: {row.get('package_access')}"
    else:
        status = INTEL_UNKNOWN
        reason = "Procurement path not established"

    return _node(
        status=status,
        reason=reason,
        confidence="HIGH" if facts["selected_path_id"] != "UNKNOWN" else "UNKNOWN",
        evidence=evidence,
        facts=facts,
        source="procurement_package|price_path|package_access",
        timestamp=_clean(pkg.get("generated_at") or row.get("updated_at")),
        missing=[] if evidence else ["procurement_path"],
        raw=pkg or path or None,
    )


def _assemble_financing(row: dict[str, Any]) -> dict[str, Any]:
    funding = _as_dict(row.get("funding_requirement") or row.get("financing"))
    econ = _as_dict(row.get("transaction_economics") or row.get("economics") or row.get("deal_economics"))
    status_raw = (
        row.get("funding_status")
        or funding.get("status")
        or funding.get("funding_status")
        or "UNKNOWN"
    )
    capital = funding.get("capital_amount") or row.get("working_capital_required")
    fin_cost = row.get("financing_cost") or row.get("financing_cost_amount") or funding.get("financing_cost")

    facts = {
        "funding_status": status_raw,
        "capital_requirement": capital if capital not in (None, "") else "UNKNOWN",
        "financing_cost": fin_cost if fin_cost not in (None, "") else "UNKNOWN",
        "unknown_financing_is_not_rejection": True,
    }

    status = map_to_intelligence_state(status_raw)
    if str(status_raw).upper() in {"UNKNOWN", "FUNDING_VERIFICATION_REQUIRED", "VERIFICATION_REQUIRED", ""}:
        status = INTEL_UNKNOWN
        reason = "Financing not verified"
    elif capital not in (None, "") or fin_cost not in (None, ""):
        status = INTEL_POSSIBLE if status == INTEL_UNKNOWN else status
        reason = None
    else:
        reason = "Financing package incomplete"

    evidence = []
    if capital not in (None, ""):
        evidence.append(
            _evidence_item(field="capital_requirement", source="funding_requirement", snippet=str(capital), confidence="MEDIUM")
        )
    if fin_cost not in (None, ""):
        evidence.append(
            _evidence_item(field="financing_cost", source="pipeline_row", snippet=str(fin_cost), confidence="MEDIUM")
        )

    return _node(
        status=status,
        reason=reason,
        confidence=funding.get("confidence") or ("MEDIUM" if evidence else "UNKNOWN"),
        evidence=evidence,
        facts=facts,
        source="funding_requirement|financing",
        timestamp=_clean(funding.get("updated_at") or row.get("updated_at")),
        missing=[] if evidence else ["capital_requirement", "financing_cost", "eligibility"],
        raw=funding or None,
    )


def _read_economics_blob(row: dict[str, Any]) -> dict[str, Any]:
    de = _as_dict(row.get("deal_economics"))
    if de.get("kind") == "M3DealEconomics":
        return de
    te = _as_dict(row.get("transaction_economics") or row.get("economics"))
    cid = _clean(row.get("canonical_id"))
    if cid:
        try:
            from m3_deal_economics import get_persisted_economics

            p = get_persisted_economics(cid)
            if isinstance(p, dict) and p.get("kind") == "M3DealEconomics":
                return p
        except Exception:
            pass
    return de or te


def _assemble_economics(row: dict[str, Any]) -> dict[str, Any]:
    de = _read_economics_blob(row)
    if not de:
        return _node(
            status=INTEL_UNKNOWN,
            reason="Economic analysis not available",
            confidence="UNKNOWN",
            evidence=[],
            facts={},
            source=None,
            missing=["deal_economics", "transaction_economics"],
        )

    prof = _as_dict(de.get("DEAL_ECONOMICS_PROFILE") or de)
    revenue = prof.get("Revenue") or de.get("revenue") or de.get("government_revenue")
    profit = (
        prof.get("Projected_profit")
        or de.get("expected_actual_profit")
        or de.get("projected_profit")
        or de.get("expected_profit")
    )
    acq = prof.get("Current_acquisition_cost") or de.get("acquisition") or de.get("acquisition_cost")
    conf = de.get("PRICE_CONFIDENCE") or de.get("confidence") or prof.get("Pricing_evidence_level")
    target_status = de.get("PROFIT_TARGET_STATUS") or prof.get("Profit_Target_Status")

    facts = {
        "revenue": revenue if revenue not in (None, "") else "UNKNOWN",
        "acquisition_cost": acq if acq not in (None, "") else "UNKNOWN",
        "projected_profit": profit if profit not in (None, "") else "UNKNOWN",
        "profit_target_status": target_status or "UNKNOWN",
        "pricing_confidence": conf or "UNKNOWN",
        "next_action": de.get("Next_Action") or "UNKNOWN",
    }

    evidence = []
    if revenue not in (None, "", "UNKNOWN"):
        evidence.append(
            _evidence_item(
                field="revenue",
                source=prof.get("pricing_source") or "deal_economics",
                snippet=str(revenue),
                confidence=conf or "UNKNOWN",
            )
        )
    if acq not in (None, "", "UNKNOWN"):
        evidence.append(
            _evidence_item(
                field="acquisition_cost",
                source=prof.get("pricing_source") or "deal_economics",
                snippet=str(acq),
                confidence=conf or "UNKNOWN",
            )
        )

    status = map_to_intelligence_state(conf or target_status)
    if facts["revenue"] != "UNKNOWN" or facts["projected_profit"] != "UNKNOWN":
        if status == INTEL_UNKNOWN:
            status = INTEL_POSSIBLE
    if str(conf or "").upper() in {"HIGH", "VALIDATED"} and facts["revenue"] != "UNKNOWN":
        status = INTEL_VALIDATED

    return _node(
        status=status,
        reason=None if evidence else "Economic figures incomplete",
        confidence=conf or "UNKNOWN",
        evidence=evidence,
        facts=facts,
        source="deal_economics|transaction_economics",
        timestamp=_clean(de.get("generated_at") or de.get("updated_at") or row.get("updated_at")),
        missing=[k for k in ("revenue", "acquisition_cost", "projected_profit") if facts.get(k) == "UNKNOWN"],
        raw=de,
    )


def _assemble_decision(row: dict[str, Any], nodes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    try:
        from m3_lifecycle import derive_lifecycle, determine_next_action, readiness_summary

        lifecycle = row.get("lifecycle") or derive_lifecycle(row)
        nxt = row.get("pending_next_action") or determine_next_action(row)
        readiness = row.get("readiness_summary") or readiness_summary(row)
    except Exception:
        lifecycle = row.get("lifecycle") or "UNKNOWN"
        nxt = row.get("pending_next_action") or {}
        readiness = row.get("readiness_summary") or {}

    next_action = nxt.get("next_action") if isinstance(nxt, dict) else nxt
    unknowns = [
        name
        for name, node in nodes.items()
        if name != "decision" and str(node.get("status") or "").upper() in {INTEL_UNKNOWN, INTEL_RESEARCH_REQUIRED}
    ]

    # Execution readiness only when core chain is strong
    core_ok = all(
        str(nodes.get(n, {}).get("status") or "").upper()
        in {INTEL_VALIDATED, INTEL_AVAILABLE, INTEL_EXECUTION_READY}
        for n in ("opportunity", "product_identity")
    )
    if str(lifecycle or "").upper() in {"READY_FOR_BID", "OPERATOR_READY", "EXECUTION_READY"}:
        status = INTEL_EXECUTION_READY
    elif core_ok and str(nodes.get("economics", {}).get("status") or "").upper() in {
        INTEL_VALIDATED,
        INTEL_AVAILABLE,
        INTEL_POSSIBLE,
    }:
        status = INTEL_AVAILABLE
    elif unknowns:
        status = INTEL_RESEARCH_REQUIRED
    else:
        status = INTEL_POSSIBLE

    facts = {
        "lifecycle": lifecycle or "UNKNOWN",
        "next_action": next_action or "UNKNOWN",
        "next_action_reason": (nxt or {}).get("reason") if isinstance(nxt, dict) else None,
        "why_not_ready": (readiness or {}).get("why_not_ready") or row.get("stop_reason") or "UNKNOWN",
        "unknown_nodes": unknowns,
        "known_nodes": [
            name
            for name, node in nodes.items()
            if name != "decision"
            and str(node.get("status") or "").upper()
            in {INTEL_VALIDATED, INTEL_AVAILABLE, INTEL_EXECUTION_READY, INTEL_POSSIBLE}
        ],
    }

    return _node(
        status=status,
        reason=None if not unknowns else f"Missing intelligence: {', '.join(unknowns)}",
        confidence="HIGH" if status == INTEL_EXECUTION_READY else ("MEDIUM" if core_ok else "UNKNOWN"),
        evidence=[
            _evidence_item(field="lifecycle", source="m3_lifecycle", snippet=str(lifecycle), confidence="HIGH"),
        ],
        facts=facts,
        source="lifecycle|readiness|graph_rollup",
        timestamp=_clean(row.get("updated_at")),
        missing=unknowns,
        raw={"lifecycle": lifecycle, "next_action": nxt, "readiness": readiness},
    )


def assemble_intelligence_graph(
    row: dict[str, Any] | None,
    *,
    identity: dict[str, Any] | None = None,
    opportunity_id: str | None = None,
) -> dict[str, Any]:
    """Assemble unified read-only intelligence graph for one opportunity."""
    ident = identity or {}
    assembled_at = _utc()

    if row is None:
        empty_nodes = {
            "opportunity": _node(
                status=INTEL_UNKNOWN,
                reason="Opportunity not found",
                facts={"requested_id": opportunity_id},
            ),
            "requirement": _node(status=INTEL_UNKNOWN, reason="No pipeline row"),
            "product_identity": _node(status=INTEL_UNKNOWN, reason="No pipeline row"),
            "supplier_intelligence": _node(
                status=INTEL_UNKNOWN, reason="No validated supplier relationship"
            ),
            "historical_intelligence": _node(status=INTEL_RESEARCH_REQUIRED, reason="No pipeline row"),
            "demand_signals": _node(status=INTEL_UNKNOWN, reason="No pipeline row"),
            "procurement_path": _node(status=INTEL_UNKNOWN, reason="No pipeline row"),
            "financing": _node(status=INTEL_UNKNOWN, reason="Financing not verified"),
            "economics": _node(status=INTEL_UNKNOWN, reason="Economic analysis not available"),
        }
        empty_nodes["decision"] = _node(
            status=INTEL_UNKNOWN,
            reason="Cannot decide without opportunity",
            facts={"unknown_nodes": list(empty_nodes.keys())},
        )
        return {
            "kind": "M3IntelligenceGraph",
            "build": BUILD_TAG,
            "found": False,
            "opportunity_id": opportunity_id,
            "opportunity_uid": ident.get("opportunity_uid"),
            "identity": ident,
            "assembled_at": assembled_at,
            "graph": empty_nodes,
            "opportunity": empty_nodes["opportunity"],
            "requirement": empty_nodes["requirement"],
            "product_identity": empty_nodes["product_identity"],
            "supplier_intelligence": empty_nodes["supplier_intelligence"],
            "historical_intelligence": empty_nodes["historical_intelligence"],
            "demand_signals": empty_nodes["demand_signals"],
            "procurement_path": empty_nodes["procurement_path"],
            "financing": empty_nodes["financing"],
            "economics": empty_nodes["economics"],
            "decision": empty_nodes["decision"],
            "summary": {
                "known": [],
                "unknown": list(GRAPH_NODES),
                "research_required": ["historical_intelligence"],
            },
            "pipeline_json_preserved": True,
            "read_only": True,
        }

    nodes: dict[str, dict[str, Any]] = {
        "opportunity": _assemble_opportunity(row, ident),
        "requirement": _assemble_requirement(row),
        "product_identity": _assemble_product_identity(row),
        "supplier_intelligence": _assemble_supplier(row),
        "historical_intelligence": _assemble_historical(row),
        "demand_signals": _assemble_demand_signals(row),
        "procurement_path": _assemble_procurement(row),
        "financing": _assemble_financing(row),
        "economics": _assemble_economics(row),
    }
    nodes["decision"] = _assemble_decision(row, nodes)

    known = [
        k
        for k, n in nodes.items()
        if str(n.get("status") or "").upper()
        in {INTEL_VALIDATED, INTEL_AVAILABLE, INTEL_EXECUTION_READY, INTEL_POSSIBLE}
    ]
    unknown = [k for k, n in nodes.items() if str(n.get("status") or "").upper() == INTEL_UNKNOWN]
    research = [k for k, n in nodes.items() if str(n.get("status") or "").upper() == INTEL_RESEARCH_REQUIRED]

    return {
        "kind": "M3IntelligenceGraph",
        "build": BUILD_TAG,
        "found": True,
        "opportunity_id": row.get("canonical_id") or opportunity_id,
        "opportunity_uid": ident.get("opportunity_uid") or nodes["opportunity"]["facts"].get("opportunity_uid"),
        "identity": ident,
        "assembled_at": assembled_at,
        "graph": nodes,
        # Flat convenience aliases matching the target view
        "opportunity": nodes["opportunity"],
        "requirement": nodes["requirement"],
        "product_identity": nodes["product_identity"],
        "supplier_intelligence": nodes["supplier_intelligence"],
        "historical_intelligence": nodes["historical_intelligence"],
        "demand_signals": nodes["demand_signals"],
        "procurement_path": nodes["procurement_path"],
        "financing": nodes["financing"],
        "economics": nodes["economics"],
        "decision": nodes["decision"],
        "summary": {
            "known": known,
            "unknown": unknown,
            "research_required": research,
        },
        "pipeline_json_preserved": True,
        "read_only": True,
    }


def get_intelligence_graph(
    opportunity_id: str,
    *,
    store: Any | None = None,
    identity_resolver: OpportunityIdentityResolver | None = None,
    restore_from_db: bool = False,
) -> dict[str, Any]:
    """Public entry: resolve handle → assemble graph."""
    resolver = identity_resolver or load_identity_resolver()
    pipeline_store = store
    if pipeline_store is None:
        from m3_pipeline_store import M3PipelineStore

        pipeline_store = M3PipelineStore()
        if restore_from_db:
            try:
                from m3_discovery_service import restore_pipeline_store_from_db

                restore_pipeline_store_from_db(pipeline_store)
            except Exception:
                pass

    row, ident = resolve_pipeline_row(
        opportunity_id, store=pipeline_store, identity_resolver=resolver
    )
    # Persist newly registered aliases when resolver mutated (best-effort)
    try:
        from m3_opportunity_identity import save_identity_resolver

        if ident.get("opportunity_uid"):
            save_identity_resolver(resolver)
    except Exception:
        pass

    return assemble_intelligence_graph(row, identity=ident, opportunity_id=opportunity_id)


def graph_snapshot_for_tests(row: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Test helper — assemble from an in-memory row without pipeline store."""
    from m3_opportunity_identity import OpportunityIdentityResolver

    resolver = kwargs.get("identity_resolver") or OpportunityIdentityResolver()
    ident = resolver.resolve_pipeline_row(row, register=True)
    return assemble_intelligence_graph(deepcopy(row), identity=ident, opportunity_id=row.get("canonical_id"))
