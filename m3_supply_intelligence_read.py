"""BUILD 24 — Supply Intelligence Layer (read models).

Converts a government product opportunity into an evidence-backed supply pathway.

Opportunity → Product Identity → Manufacturer → Distributor/Channel → Supplier
  → Commercial Evidence → Supply Path → Human Decision

NOT a decision maker, ranking system, automated buyer, or Human OS replacement.
No scoring, rankings, AI-recommended vendors, auto-bid, auto-outreach, or scraping.

AI assists organization via existing staged routing (0–5); never silently spends budget.
UNKNOWN stays UNKNOWN. Claims require evidence. Existing layers remain source of truth.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from application_clock import now_utc

BUILD_TAG = "20260919-m3-supply-intelligence-1"

SUPPLIER_TYPES = (
    "Manufacturer",
    "Distributor",
    "Dealer",
    "Reseller",
    "Broker",
    "Unknown",
)

EVIDENCE_TYPES = (
    "Supplier Quote",
    "Distributor Listing",
    "Manufacturer Pricing",
    "Historical Government Purchase",
    "Catalog",
    "Unknown",
)

PRODUCT_INDEX = "m3_supply_products_v1"
MFG_INDEX = "m3_supply_manufacturers_v1"
SUPPLIER_INDEX = "m3_supply_suppliers_v1"
CHANNEL_INDEX = "m3_supply_channels_v1"
EVIDENCE_INDEX = "m3_supply_commercial_evidence_v1"
PATH_INDEX = "m3_supply_paths_v1"


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
    idx["entries"] = entries[-500:]
    if persist:
        _save_index(index_key, idx)
    return record


def _get(index_key: str, rid: str) -> dict[str, Any] | None:
    by = _as_dict(_load_index(index_key).get("by_key"))
    r = by.get(rid)
    return r if isinstance(r, dict) else None


def _list_entries(index_key: str, *, limit: int = 50) -> list[dict[str, Any]]:
    entries = [e for e in (_load_index(index_key).get("entries") or []) if isinstance(e, dict)]
    entries.sort(key=lambda e: str(e.get("updated_at") or e.get("created_at") or ""), reverse=True)
    return entries[:limit]


def _require_evidence(evidence: Any, *, claim: str = "claim") -> list[Any]:
    if isinstance(evidence, list) and evidence:
        return evidence
    if _known(evidence):
        return [evidence]
    raise ValueError(f"evidence required for {claim} — unsupported claims not stored")


# ---------------------------------------------------------------------------
# Product Intelligence
# ---------------------------------------------------------------------------
def create_product(payload: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
    name = str(payload.get("name") or "").strip()
    if not name:
        raise ValueError("name required")
    evidence = _require_evidence(
        payload.get("evidence_links") or payload.get("evidence"),
        claim="product",
    )
    now = _utc()
    pid = payload.get("product_id") or _sid("prod", f"{name}|{payload.get('part_numbers')}|{now}")
    record = {
        "kind": "M3SupplyProduct",
        "product_id": pid,
        "name": name,
        "category": payload.get("category") if _known(payload.get("category")) else "UNKNOWN",
        "manufacturer": payload.get("manufacturer") if _known(payload.get("manufacturer")) else "UNKNOWN",
        "model": payload.get("model") if _known(payload.get("model")) else "UNKNOWN",
        "part_numbers": payload.get("part_numbers") or [],
        "identifiers": payload.get("identifiers") or {},
        "specifications": payload.get("specifications") or {},
        "documents": payload.get("documents") or [],
        "evidence_links": evidence,
        "knowns": payload.get("knowns") or [f"name={name}"],
        "unknowns": payload.get("unknowns") if payload.get("unknowns") is not None else ["UNKNOWN"],
        "opportunity_id": payload.get("opportunity_id") or "UNKNOWN",
        "created_at": now,
        "updated_at": now,
        "numeric_score": None,
        "ranking": None,
        "build": BUILD_TAG,
    }
    if isinstance(record["part_numbers"], str):
        record["part_numbers"] = [record["part_numbers"]]
    out = _upsert(PRODUCT_INDEX, "product_id", record, persist=persist)
    _maybe_create_actions_for_unknowns(
        opportunity_id=record.get("opportunity_id"),
        unknowns=record["unknowns"],
        context=f"product:{pid}",
        persist=persist,
    )
    return out


def get_product(product_id: str) -> dict[str, Any] | None:
    return _get(PRODUCT_INDEX, product_id)


def create_manufacturer(payload: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
    company = str(payload.get("company_name") or payload.get("name") or "").strip()
    if not company:
        raise ValueError("company_name required")
    evidence = _require_evidence(
        payload.get("evidence_links") or payload.get("evidence"),
        claim="manufacturer",
    )
    now = _utc()
    mid = payload.get("manufacturer_id") or _sid("mfg", f"{company}|{now}")
    record = {
        "kind": "M3SupplyManufacturer",
        "manufacturer_id": mid,
        "company_name": company,
        "products": payload.get("products") or [],
        "categories": payload.get("categories") or [],
        "documents": payload.get("documents") or [],
        "channels": payload.get("channels") or [],
        "evidence_links": evidence,
        "knowns": payload.get("knowns") or [f"company_name={company}"],
        "unknowns": payload.get("unknowns") if payload.get("unknowns") is not None else ["UNKNOWN"],
        "created_at": now,
        "updated_at": now,
        "build": BUILD_TAG,
    }
    return _upsert(MFG_INDEX, "manufacturer_id", record, persist=persist)


def get_manufacturer(manufacturer_id: str) -> dict[str, Any] | None:
    return _get(MFG_INDEX, manufacturer_id)


def create_supplier(payload: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
    company = str(payload.get("company_name") or payload.get("name") or "").strip()
    if not company:
        raise ValueError("company_name required")
    evidence = _require_evidence(
        payload.get("evidence_links") or payload.get("evidence"),
        claim="supplier",
    )
    requested_type = payload.get("supplier_type") or "Unknown"
    if requested_type not in SUPPLIER_TYPES:
        requested_type = "Unknown"
    # Do not infer supplier type without evidence
    if requested_type != "Unknown" and not _known(payload.get("type_evidence")):
        stype = "Unknown"
        type_evidence = "UNKNOWN"
    else:
        stype = requested_type
        type_evidence = payload.get("type_evidence") if _known(payload.get("type_evidence")) else "UNKNOWN"

    now = _utc()
    sid = payload.get("supplier_id") or _sid("sup", f"{company}|{now}")
    record = {
        "kind": "M3SupplySupplier",
        "supplier_id": sid,
        "company_name": company,
        "supplier_type": stype,
        "products": payload.get("products") or [],
        "manufacturers": payload.get("manufacturers") or [],
        "contacts": payload.get("contacts") or [],
        "quotes": payload.get("quotes") or [],
        "terms": payload.get("terms") if _known(payload.get("terms")) else "UNKNOWN",
        "government_history": payload.get("government_history") or [],
        "evidence_links": evidence,
        "type_evidence": type_evidence,
        "knowns": payload.get("knowns") or [f"company_name={company}"],
        "unknowns": payload.get("unknowns") if payload.get("unknowns") is not None else ["UNKNOWN"],
        "opportunity_id": payload.get("opportunity_id") or "UNKNOWN",
        "created_at": now,
        "updated_at": now,
        "numeric_score": None,
        "ranking": None,
        "ai_recommended": False,
        "build": BUILD_TAG,
    }
    out = _upsert(SUPPLIER_INDEX, "supplier_id", record, persist=persist)
    _maybe_create_actions_for_unknowns(
        opportunity_id=record.get("opportunity_id"),
        unknowns=record["unknowns"],
        context=f"supplier:{sid}",
        persist=persist,
    )
    return out


def get_supplier(supplier_id: str) -> dict[str, Any] | None:
    return _get(SUPPLIER_INDEX, supplier_id)


def create_channel_relationship(payload: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
    a = payload.get("from_entity") or payload.get("entity_a")
    b = payload.get("to_entity") or payload.get("entity_b")
    rtype = payload.get("relationship_type") or payload.get("type")
    if not _known(a) or not _known(b):
        raise ValueError("from_entity and to_entity required")
    if not _known(rtype):
        raise ValueError("relationship_type required")
    evidence = _require_evidence(payload.get("evidence") or payload.get("evidence_links"), claim="channel relationship")
    now = _utc()
    rid = payload.get("relationship_id") or _sid("chan", f"{a}|{rtype}|{b}|{now}")
    record = {
        "kind": "M3SupplyChannelRelationship",
        "relationship_id": rid,
        "from_entity": a,
        "to_entity": b,
        "relationship_type": rtype,
        "evidence": evidence,
        "date_observed": payload.get("date_observed") or now,
        "knowns": payload.get("knowns") or [f"{a} —{rtype}→ {b}"],
        "unknowns": payload.get("unknowns") if payload.get("unknowns") is not None else ["UNKNOWN"],
        "created_at": now,
        "updated_at": now,
        "no_assumptions": True,
        "build": BUILD_TAG,
    }
    return _upsert(CHANNEL_INDEX, "relationship_id", record, persist=persist)


def create_commercial_evidence(payload: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
    etype = payload.get("evidence_type") or "Unknown"
    if etype not in EVIDENCE_TYPES:
        etype = "Unknown"
    source = payload.get("source")
    if etype != "Unknown" and not _known(source):
        raise ValueError("source required for commercial evidence")
    if not _known(source):
        source = "UNKNOWN"
    now = _utc()
    eid = payload.get("evidence_id") or _sid(
        "cev", f"{payload.get('product')}|{payload.get('supplier')}|{etype}|{now}"
    )
    record = {
        "kind": "M3SupplyCommercialEvidence",
        "evidence_id": eid,
        "product": payload.get("product") if _known(payload.get("product")) else "UNKNOWN",
        "supplier": payload.get("supplier") if _known(payload.get("supplier")) else "UNKNOWN",
        "evidence_type": etype,
        "price": payload.get("price") if payload.get("price") not in (None, "") else "UNKNOWN",
        "quantity": payload.get("quantity") if payload.get("quantity") not in (None, "") else "UNKNOWN",
        "date": payload.get("date") or now,
        "lead_time": payload.get("lead_time") if _known(payload.get("lead_time")) else "UNKNOWN",
        "terms": payload.get("terms") if _known(payload.get("terms")) else "UNKNOWN",
        "source": source,
        "notes": payload.get("notes") or "",
        "claim": payload.get("claim") or f"{etype} observed",
        "opportunity_id": payload.get("opportunity_id") or "UNKNOWN",
        "created_at": now,
        "updated_at": now,
        "fabricated": False,
        "build": BUILD_TAG,
    }
    # Provenance / commercial extensions — only persist when provided (never invent)
    for key in (
        "verification_status",
        "manufacturer",
        "model_sku",
        "unit_price",
        "total_quoted_price",
        "currency",
        "quote_expiration",
        "freight",
        "deposit_requirement",
        "availability",
        "quote_reference",
        "captured_by",
        "not_an_assumption",
    ):
        if key in payload and payload.get(key) is not None:
            record[key] = payload[key]
    return _upsert(EVIDENCE_INDEX, "evidence_id", record, persist=persist)


def create_supply_path(payload: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
    oid = payload.get("opportunity_id")
    if not _known(oid):
        raise ValueError("opportunity_id required")

    product = payload.get("product") if _known(payload.get("product")) else "UNKNOWN"
    manufacturer = payload.get("manufacturer") if _known(payload.get("manufacturer")) else "UNKNOWN"
    supplier = payload.get("supplier") if _known(payload.get("supplier")) else "UNKNOWN"
    channel_path = payload.get("channel_path") or []
    commercial = payload.get("commercial_evidence") or []

    knowns = list(payload.get("knowns") or [])
    unknowns = list(payload.get("unknowns") or [])

    if product == "UNKNOWN":
        unknowns.append("Product identity not evidenced")
    else:
        knowns.append(f"product={product}")
    if manufacturer == "UNKNOWN":
        unknowns.append("Manufacturer not evidenced")
    else:
        knowns.append(f"manufacturer={manufacturer}")
    if supplier == "UNKNOWN":
        unknowns.append("Supplier not evidenced")
    else:
        knowns.append(f"supplier={supplier}")
    if not commercial:
        unknowns.append("Need supplier pricing / commercial evidence")
    if not payload.get("execution_requirements"):
        unknowns.append("Need delivery confirmation / execution requirements")
    if not unknowns:
        unknowns = ["UNKNOWN"]

    now = _utc()
    path_id = payload.get("path_id") or payload.get("supply_path_id") or _sid("spath", f"{oid}|{product}|{now}")
    record = {
        "kind": "M3SupplyPath",
        "path_id": path_id,
        "opportunity_id": oid,
        "product": product,
        "manufacturer": manufacturer,
        "channel_path": channel_path if isinstance(channel_path, list) else [channel_path],
        "supplier": supplier,
        "commercial_evidence": commercial if isinstance(commercial, list) else [commercial],
        "transaction_requirements": payload.get("transaction_requirements") or ["UNKNOWN"],
        "execution_requirements": payload.get("execution_requirements") or ["UNKNOWN"],
        "knowns": knowns or ["UNKNOWN"],
        "unknowns": unknowns,
        "flow_example": [
            "Government Requirement",
            "Product",
            "Manufacturer",
            "Distributor",
            "Supplier",
            "Quote",
        ],
        "human_decision_required": True,
        "ai_decides": False,
        "no_scores": True,
        "no_rankings": True,
        "created_at": now,
        "updated_at": now,
        "build": BUILD_TAG,
    }
    out = _upsert(PATH_INDEX, "path_id", record, persist=persist)
    _maybe_create_actions_for_unknowns(
        opportunity_id=oid,
        unknowns=unknowns,
        context=f"supply_path:{path_id}",
        persist=persist,
    )
    if persist:
        try:
            from m3_research_execution_read import create_missions_from_supply_unknowns

            create_missions_from_supply_unknowns(
                opportunity_id=str(oid),
                unknowns=unknowns,
                product=product,
                supplier=supplier,
                persist=True,
            )
        except Exception:
            pass
    return out


def get_supply_path(path_id: str) -> dict[str, Any] | None:
    return _get(PATH_INDEX, path_id)


_UNKNOWN_ACTION_MAP = (
    ("pricing", {"title": "Request supplier quote", "why": "Commercial evidence missing", "evidence": "Quote", "type": "COMMUNICATION"}),
    ("quote", {"title": "Request supplier quote", "why": "Commercial evidence missing", "evidence": "Quote", "type": "COMMUNICATION"}),
    ("commercial", {"title": "Request supplier quote", "why": "Commercial evidence missing", "evidence": "Quote", "type": "COMMUNICATION"}),
    ("lead time", {"title": "Confirm lead time", "why": "Execution requirement unknown", "evidence": "Lead time confirmation", "type": "COMMUNICATION"}),
    ("delivery", {"title": "Confirm lead time", "why": "Execution requirement unknown", "evidence": "Delivery confirmation", "type": "COMMUNICATION"}),
    ("manufacturer", {"title": "Identify manufacturer", "why": "Product origin not evidenced", "evidence": "Manufacturer documentation", "type": "RESEARCH"}),
    ("supplier", {"title": "Identify supplier / channel", "why": "Supply path incomplete", "evidence": "Supplier catalog or quote", "type": "RESEARCH"}),
    ("product", {"title": "Confirm product identity", "why": "Product identity not evidenced", "evidence": "NSN/part/document", "type": "VALIDATION"}),
)


def _maybe_create_actions_for_unknowns(
    *,
    opportunity_id: Any,
    unknowns: list[Any],
    context: str,
    persist: bool,
) -> list[dict[str, Any]]:
    if not persist or not _known(opportunity_id):
        return []
    created: list[dict[str, Any]] = []
    try:
        from m3_action_orchestration_read import create_action
    except Exception:
        return []

    for u in unknowns or []:
        text = str(u).lower()
        if text in ("unknown", ""):
            continue
        matched = None
        for key, spec in _UNKNOWN_ACTION_MAP:
            if key in text:
                matched = spec
                break
        if not matched:
            matched = {
                "title": f"Resolve supply unknown: {str(u)[:80]}",
                "why": "Supply intelligence gap blocks path completion",
                "evidence": "Documented finding or explicit UNKNOWN",
                "type": "RESEARCH",
            }
        try:
            action = create_action(
                {
                    "action_type": matched["type"],
                    "title": matched["title"],
                    "description": matched["why"],
                    "trigger_source": "unknown_created",
                    "related_opportunity": opportunity_id,
                    "evidence_caused_by": context,
                    "evidence_requirements": [matched["evidence"]],
                    "completion_criteria": [matched["evidence"], "unknowns_recorded"],
                    "created_by": "supply_intelligence",
                },
                persist=True,
            )
            created.append(action)
        except Exception:
            continue
    return created


def derive_supply_from_row(row: dict[str, Any] | None) -> dict[str, Any]:
    row = row if isinstance(row, dict) else {}
    cid = row.get("canonical_id") or "UNKNOWN"
    nsn = _as_dict(_as_dict(_as_dict(row.get("dla_product_structure")).get("fields")).get("nsn")).get("value")
    product_name = row.get("title") or "UNKNOWN"
    manufacturer = "UNKNOWN"
    suppliers: list[str] = []
    for e in (_as_dict(row.get("supplier_product_graph")).get("edges") or [])[:8]:
        if not isinstance(e, dict):
            continue
        name = e.get("supplier_name") or e.get("company")
        role = str(e.get("role") or "").lower()
        if _known(name):
            if "manufacturer" in role:
                manufacturer = name
            else:
                suppliers.append(name)

    paths = [p for p in _list_entries(PATH_INDEX, limit=30) if p.get("opportunity_id") == cid]
    products = [p for p in _list_entries(PRODUCT_INDEX, limit=30) if p.get("opportunity_id") == cid]
    supplier_recs = [s for s in _list_entries(SUPPLIER_INDEX, limit=30) if s.get("opportunity_id") == cid]
    evidence = [e for e in _list_entries(EVIDENCE_INDEX, limit=30) if e.get("opportunity_id") == cid]

    product_identified = bool(_known(nsn) or products or (_known(product_name) and product_name != "UNKNOWN"))
    supplier_identified = bool(suppliers or supplier_recs or (paths and _known((paths[0] or {}).get("supplier"))))
    commercial_available = bool(evidence or (paths and (paths[0] or {}).get("commercial_evidence")))

    unknowns: list[str] = []
    if not product_identified:
        unknowns.append("Product identity not evidenced")
    if manufacturer == "UNKNOWN" and not any(_known(p.get("manufacturer")) for p in products):
        unknowns.append("Manufacturer not evidenced")
    if not supplier_identified:
        unknowns.append("Supplier not evidenced")
    if not commercial_available:
        unknowns.append("Need supplier pricing")

    return {
        "kind": "M3SupplyOpportunityView",
        "opportunity_id": cid,
        "derived_product": {"name": product_name, "nsn": nsn or "UNKNOWN", "manufacturer": manufacturer},
        "derived_suppliers": suppliers or ["UNKNOWN"],
        "stored_products": products[:10],
        "stored_suppliers": supplier_recs[:10],
        "stored_paths": paths[:5],
        "stored_evidence": evidence[:10],
        "supply_status": {
            "product_identified": product_identified,
            "supplier_identified": supplier_identified,
            "commercial_evidence_available": commercial_available,
            "unknowns_remaining": unknowns or ["None listed — still verify before deciding"],
        },
        "does_not_replace_existing_layers": True,
        "build": BUILD_TAG,
    }


def build_supply_intelligence_profile(row: dict[str, Any] | None) -> dict[str, Any]:
    row = row if isinstance(row, dict) else {}
    view = derive_supply_from_row(row)
    return {
        "kind": "M3SupplyIntelligenceProfile",
        "build": BUILD_TAG,
        "opportunity_id": row.get("canonical_id") or "UNKNOWN",
        "opportunity_view": view,
        "workflow": [
            "Opportunity",
            "Product Identity",
            "Manufacturer",
            "Distributor / Channel",
            "Supplier",
            "Commercial Evidence",
            "Supply Path",
            "Human Decision",
        ],
        "ai_budget_aware": True,
        "staged_ai_escalation_preserved": True,
        "no_automatic_expensive_ai": True,
        "no_scores": True,
        "no_rankings": True,
        "no_ai_recommended_vendors": True,
        "no_automatic_outreach": True,
        "not_a_decision_maker": True,
        "not_human_os_replacement": True,
        "external_sources_authoritative": True,
        "unknown_preserved": True,
        "engines_unchanged": True,
        "generated_at": _utc(),
        "read_only": True,
    }


def attach_supply_intelligence_to_deal_room(
    deal: dict[str, Any], *, row: dict[str, Any] | None = None
) -> dict[str, Any]:
    if not isinstance(deal, dict):
        return deal
    out = dict(deal)
    try:
        out["supply_intelligence"] = build_supply_intelligence_profile(
            row or {"canonical_id": deal.get("canonical_id")}
        )
    except Exception:
        out["supply_intelligence"] = {
            "kind": "M3SupplyIntelligenceProfile",
            "build": BUILD_TAG,
            "error": "supply_intelligence_unavailable",
            "read_only": True,
        }
    return out


def enrich_command_center_supply(
    sections: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    period: str,
) -> dict[str, Any]:
    out = dict(sections or {})
    research: list[dict[str, Any]] = []
    responses: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    products_id: list[dict[str, Any]] = []
    suppliers_added: list[dict[str, Any]] = []
    evidence_created: list[dict[str, Any]] = []
    learned: list[dict[str, Any]] = []

    for p in _list_entries(PATH_INDEX, limit=40):
        unk = p.get("unknowns") or []
        if any(
            "pricing" in str(u).lower() or "quote" in str(u).lower() or "supplier" in str(u).lower()
            for u in unk
        ):
            research.append(
                {
                    "opportunity_id": p.get("opportunity_id"),
                    "what": "Supply research needing attention",
                    "unknowns": unk[:3],
                    "path_id": p.get("path_id"),
                }
            )
        if p.get("supplier") == "UNKNOWN" or "Supplier not evidenced" in unk:
            blocked.append(
                {
                    "opportunity_id": p.get("opportunity_id"),
                    "what": "Blocked supply path",
                    "why": "Supplier or path incomplete",
                    "path_id": p.get("path_id"),
                }
            )
        if not (p.get("commercial_evidence") or []):
            missing.append(
                {
                    "opportunity_id": p.get("opportunity_id"),
                    "what": "Missing commercial evidence",
                    "path_id": p.get("path_id"),
                }
            )

    for e in _list_entries(EVIDENCE_INDEX, limit=20):
        if e.get("evidence_type") == "Supplier Quote":
            responses.append({"supplier": e.get("supplier"), "product": e.get("product"), "date": e.get("date")})
        evidence_created.append(
            {"type": e.get("evidence_type"), "supplier": e.get("supplier"), "product": e.get("product")}
        )

    for prod in _list_entries(PRODUCT_INDEX, limit=15):
        products_id.append({"product": prod.get("name"), "id": prod.get("product_id")})
    for s in _list_entries(SUPPLIER_INDEX, limit=15):
        suppliers_added.append({"supplier": s.get("company_name"), "type": s.get("supplier_type")})
        learned.append({"what": f"Supplier {s.get('company_name')} recorded", "type": s.get("supplier_type")})

    for r in (rows or [])[:15]:
        if not isinstance(r, dict):
            continue
        view = derive_supply_from_row(r)
        st = view.get("supply_status") or {}
        for u in st.get("unknowns_remaining") or []:
            if "None listed" in str(u):
                continue
            research.append({"opportunity_id": r.get("canonical_id"), "what": u})
            missing.append({"opportunity_id": r.get("canonical_id"), "what": u})

    if period == "morning":
        out["supply_research_needing_attention"] = research[:15]
        out["supplier_responses"] = responses[:15]
        out["missing_supply_evidence"] = missing[:15]
        out["blocked_supply_paths"] = blocked[:15]
    else:
        out["products_identified"] = products_id[:15]
        out["suppliers_added"] = suppliers_added[:15]
        out["evidence_created"] = evidence_created[:15]
        out["new_supply_intelligence_learned"] = learned[:15]
    return out


def supply_status_for_human_os(row: dict[str, Any] | None) -> dict[str, Any]:
    view = derive_supply_from_row(row)
    st = view.get("supply_status") or {}
    return {
        "product_identified": st.get("product_identified"),
        "supplier_identified": st.get("supplier_identified"),
        "commercial_evidence_available": st.get("commercial_evidence_available"),
        "unknowns_remaining": st.get("unknowns_remaining"),
        "plain": {
            "product": "Product identified" if st.get("product_identified") else "Product not yet identified",
            "supplier": "Supplier identified" if st.get("supplier_identified") else "Supplier not yet identified",
            "commercial": "Commercial evidence available"
            if st.get("commercial_evidence_available")
            else "Commercial evidence missing",
        },
    }


def supplier_workspace_supply_view(
    supplier_id: str | None = None, *, row: dict[str, Any] | None = None
) -> dict[str, Any]:
    rec = get_supplier(supplier_id) if supplier_id else None
    if not rec and row:
        view = derive_supply_from_row(row)
        name = (view.get("derived_suppliers") or ["UNKNOWN"])[0]
        return {
            "supplier": name,
            "known": {
                "products_carried": [view.get("derived_product")],
                "manufacturer_relationships": ["UNKNOWN"],
                "quotes": [],
                "evidence": ["supplier_product_graph"] if name != "UNKNOWN" else ["UNKNOWN"],
            },
            "unknown": {"terms": "UNKNOWN", "availability": "UNKNOWN", "government_capability": "UNKNOWN"},
        }
    if not rec:
        return {
            "supplier": "UNKNOWN",
            "known": {"products_carried": [], "manufacturer_relationships": [], "quotes": [], "evidence": ["UNKNOWN"]},
            "unknown": {"terms": "UNKNOWN", "availability": "UNKNOWN", "government_capability": "UNKNOWN"},
        }
    return {
        "supplier": rec.get("company_name"),
        "known": {
            "products_carried": rec.get("products") or [],
            "manufacturer_relationships": rec.get("manufacturers") or [],
            "quotes": rec.get("quotes") or [],
            "evidence": rec.get("evidence_links") or [],
        },
        "unknown": {
            "terms": rec.get("terms") if rec.get("terms") != "UNKNOWN" else "UNKNOWN",
            "availability": "UNKNOWN",
            "government_capability": "UNKNOWN" if not (rec.get("government_history") or []) else "history present",
        },
    }


def decision_workspace_supply_view(
    row: dict[str, Any] | None = None, *, path_id: str | None = None
) -> dict[str, Any]:
    path = get_supply_path(path_id) if path_id else None
    if not path and row:
        paths = [p for p in _list_entries(PATH_INDEX, limit=20) if p.get("opportunity_id") == row.get("canonical_id")]
        path = paths[0] if paths else None
    if not path:
        view = derive_supply_from_row(row)
        return {
            "supply_path": "UNKNOWN — not yet documented",
            "evidence": view.get("stored_evidence") or ["UNKNOWN"],
            "known": view.get("supply_status"),
            "unknown": (view.get("supply_status") or {}).get("unknowns_remaining"),
            "required_actions": ["Document supply path with evidence before deciding"],
        }
    return {
        "supply_path": {
            "product": path.get("product"),
            "manufacturer": path.get("manufacturer"),
            "channel_path": path.get("channel_path"),
            "supplier": path.get("supplier"),
        },
        "evidence": path.get("commercial_evidence") or ["UNKNOWN"],
        "known": path.get("knowns"),
        "unknown": path.get("unknowns"),
        "required_actions": [
            f"Resolve: {u}" for u in (path.get("unknowns") or []) if str(u).upper() != "UNKNOWN"
        ]
        or ["Human decision required — AI does not decide"],
    }
