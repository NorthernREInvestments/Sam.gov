"""BUILD 18 — Contract Lifecycle Intelligence (read models).

Connects opportunity discovery through award, execution, completion, and
institutional memory. Facts only — no invented statuses, payment timing,
performance scores, or future award predictions.

UNKNOWN remains UNKNOWN. Append-only history. Award ≠ execution success.
"""

from __future__ import annotations

import json
import re
from typing import Any

from application_clock import now_utc

BUILD_TAG = "20260919-m3-contract-lifecycle-1"

ST_UNKNOWN = "UNKNOWN"
ST_DETECTED = "DETECTED"

# Contract master statuses (factual only)
CTR_AWARDED = "AWARDED"
CTR_ACTIVE = "ACTIVE"
CTR_MODIFIED = "MODIFIED"
CTR_COMPLETED = "COMPLETED"
CTR_CLOSED = "CLOSED"

# Lifecycle stages
LIFECYCLE_STAGES = (
    "OPPORTUNITY",
    "AWARD",
    "KICKOFF",
    "SUPPLIER_PREPARATION",
    "PROCUREMENT",
    "DELIVERY",
    "ACCEPTANCE",
    "INVOICE",
    "PAYMENT",
    "CLOSEOUT",
)

STAGE_UNKNOWN = "UNKNOWN"
STAGE_PLANNED = "PLANNED"
STAGE_IN_PROGRESS = "IN_PROGRESS"
STAGE_COMPLETE = "COMPLETE"
STAGE_BLOCKED = "BLOCKED"

# Modification types
MOD_QTY = "Quantity change"
MOD_DATE = "Date change"
MOD_SPEC = "Specification change"
MOD_FUNDING = "Funding change"
MOD_ADMIN = "Administrative change"
MOD_UNKNOWN = "Unknown"

# Acceptance / payment
ACC_ACCEPTED = "ACCEPTED"
ACC_REJECTED = "REJECTED"
ACC_PENDING = "PENDING"
PAY_INVOICED = "INVOICED"
PAY_SUBMITTED = "SUBMITTED"
PAY_PAID = "PAID"
PAY_DELAYED = "DELAYED"

CONTRACT_INDEX_KEY = "m3_contract_master_v1"
MOD_INDEX_KEY = "m3_contract_modifications_v1"
DELIVERY_INDEX_KEY = "m3_delivery_records_v1"
ACCEPTANCE_INDEX_KEY = "m3_acceptance_records_v1"
PAYMENT_INDEX_KEY = "m3_payment_records_v1"
PERF_INDEX_KEY = "m3_contract_performance_v1"
CLOSEOUT_INDEX_KEY = "m3_closeout_records_v1"
FUTURE_DEMAND_KEY = "m3_future_demand_v1"
TIMELINE_INDEX_KEY = "m3_contract_timeline_v1"


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
    owner: str = "UNKNOWN",
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
        "owner": owner or "UNKNOWN",
    }


def _as_dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _unwrap(v: Any) -> Any:
    if isinstance(v, dict) and "value" in v:
        return v.get("value")
    return v


def _known(v: Any) -> bool:
    return _unwrap(v) not in (None, "", "UNKNOWN")


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


def _contract_id(row: dict[str, Any]) -> str:
    return str(
        row.get("contract_number")
        or row.get("award_number")
        or _as_dict(row.get("contract_record")).get("contract_number")
        or row.get("canonical_id")
        or "UNKNOWN"
    )


# ---------------------------------------------------------------------------
# BUILD 1 — Contract Master Record
# ---------------------------------------------------------------------------
def _infer_contract_status(row: dict[str, Any], stored: dict[str, Any]) -> str:
    """Factual status only — never invent COMPLETED/CLOSED without evidence."""
    st = str(stored.get("status") or "").upper()
    if st in {CTR_AWARDED, CTR_ACTIVE, CTR_MODIFIED, CTR_COMPLETED, CTR_CLOSED}:
        return st
    if _as_dict(row.get("closeout_record")).get("final_status") in {CTR_CLOSED, CTR_COMPLETED, "CLOSED", "COMPLETED"}:
        return CTR_CLOSED if "CLOSE" in str(_as_dict(row.get("closeout_record")).get("final_status")).upper() else CTR_COMPLETED
    mods = row.get("contract_modifications") or stored.get("modifications")
    if mods:
        return CTR_MODIFIED
    lc = str(row.get("lifecycle") or "").upper()
    if lc in {"CLOSEOUT", "CLOSED"}:
        # lifecycle label alone is not proof of closeout completion
        return CTR_ACTIVE if (row.get("award_number") or row.get("award_date")) else ST_UNKNOWN
    if row.get("award_number") or row.get("award_date") or lc in {"AWARDED", "WON", "EXECUTION", "FULFILLMENT", "PAYMENT"}:
        if lc in {"ACTIVE", "EXECUTION", "FULFILLMENT", "PAYMENT"}:
            return CTR_ACTIVE
        return CTR_AWARDED
    return ST_UNKNOWN


def build_contract_record(row: dict[str, Any]) -> dict[str, Any]:
    stored = _as_dict(row.get("contract_record"))
    struct = _as_dict(row.get("dla_product_structure"))
    fields = _as_dict(struct.get("fields"))
    pi = _as_dict(row.get("product_identity"))
    spg = _as_dict(row.get("supplier_product_graph"))

    products = []
    nsn = _unwrap(fields.get("nsn") or pi.get("nsn") or struct.get("nsn"))
    pn = _unwrap(fields.get("part_number") or pi.get("part_number"))
    if _known(nsn) or _known(pn):
        products.append({"nsn": nsn or "UNKNOWN", "part_number": pn or "UNKNOWN", "evidence": "product_identity|dla"})
    for p in stored.get("products") or []:
        if isinstance(p, dict):
            products.append(p)

    suppliers = []
    for e in spg.get("edges") or []:
        if isinstance(e, dict) and e.get("supplier_name"):
            suppliers.append(
                {
                    "supplier_name": e.get("supplier_name"),
                    "relationship": e.get("relationship_type") or "UNKNOWN",
                    "evidence": e.get("source") or "supplier_product_graph",
                    "note": "Graph presence ≠ commitment",
                }
            )
    for s in stored.get("suppliers") or []:
        if isinstance(s, dict):
            suppliers.append(s)

    docs = []
    for d in row.get("documents") or []:
        if isinstance(d, dict):
            docs.append(
                {
                    "filename": d.get("filename") or "UNKNOWN",
                    "document_type": d.get("document_type") or "UNKNOWN",
                    "evidence": d.get("filename") or "documents",
                }
            )

    status = _infer_contract_status(row, stored)
    cid = stored.get("contract_number") or row.get("contract_number") or row.get("award_number")

    return {
        "kind": "M3ContractRecord",
        "contract_number": fact(cid, source="pipeline|contract_record", evidence="Contract/award number field", status=ST_DETECTED if _known(cid) else ST_UNKNOWN),
        "award_identifier": fact(
            stored.get("award_identifier") or row.get("award_number") or row.get("award_id"),
            source="pipeline",
            status=ST_DETECTED if (row.get("award_number") or row.get("award_id")) else ST_UNKNOWN,
        ),
        "related_opportunity": fact(row.get("canonical_id"), source="pipeline", status=ST_DETECTED if row.get("canonical_id") else ST_UNKNOWN),
        "agency": fact(stored.get("agency") or row.get("agency") or row.get("buyer")),
        "buyer": fact(stored.get("buyer") or row.get("buyer") or row.get("agency")),
        "contract_type": fact(stored.get("contract_type") or row.get("contract_type")),
        "award_date": fact(stored.get("award_date") or row.get("award_date")),
        "effective_date": fact(stored.get("effective_date") or row.get("effective_date")),
        "expiration_date": fact(stored.get("expiration_date") or row.get("expiration_date") or row.get("period_of_performance_end")),
        "value": fact(stored.get("value") or row.get("award_amount") or row.get("contract_value")),
        "products": products or [{"nsn": "UNKNOWN", "part_number": "UNKNOWN", "evidence": "UNKNOWN"}],
        "suppliers": suppliers or [{"supplier_name": "UNKNOWN", "evidence": "UNKNOWN", "note": "No suppliers evidenced"}],
        "documents": docs,
        "status": status,
        "evidence": fact(
            stored.get("evidence") or (row.get("award_number") if status != ST_UNKNOWN else "UNKNOWN"),
            source="contract_record|pipeline",
            evidence="Status derived only from evidenced fields — award ≠ execution success",
            status=ST_DETECTED if status != ST_UNKNOWN else ST_UNKNOWN,
        ),
        "owner": stored.get("owner") or "MANAGER",
        "note": "Award does not mean execution success",
        "fabricated": False,
    }


# ---------------------------------------------------------------------------
# BUILD 2 — Contract Lifecycle Model
# ---------------------------------------------------------------------------
def build_contract_lifecycle(row: dict[str, Any]) -> dict[str, Any]:
    stored = _as_dict(row.get("contract_lifecycle"))
    stages_in = _as_dict(stored.get("stages"))
    master = build_contract_record(row)
    awarded = master.get("status") not in {ST_UNKNOWN}

    # Reuse execution OS stage evidence when present
    exec_stages = {}
    try:
        from m3_execution_os_read import build_award_execution_control

        ctrl = build_award_execution_control(row)
        for s in ctrl.get("stages") or []:
            exec_stages[str(s.get("stage"))] = s
    except Exception:
        pass

    stage_map = {
        "OPPORTUNITY": None,
        "AWARD": "AWARD",
        "KICKOFF": None,
        "SUPPLIER_PREPARATION": "SUPPLIER_COMMITMENT",
        "PROCUREMENT": "PURCHASE_ORDER",
        "DELIVERY": "SHIPMENT",
        "ACCEPTANCE": "ACCEPTANCE",
        "INVOICE": "INVOICE",
        "PAYMENT": "PAYMENT",
        "CLOSEOUT": "CLOSEOUT",
    }

    stages = []
    for name in LIFECYCLE_STAGES:
        local = _as_dict(stages_in.get(name))
        mapped = stage_map.get(name)
        ex = _as_dict(exec_stages.get(mapped)) if mapped else {}
        status = str(local.get("status") or ex.get("status") or STAGE_UNKNOWN).upper()
        if status not in {STAGE_UNKNOWN, STAGE_PLANNED, STAGE_IN_PROGRESS, STAGE_COMPLETE, STAGE_BLOCKED}:
            status = STAGE_UNKNOWN
        if status == STAGE_UNKNOWN and name == "OPPORTUNITY" and row.get("canonical_id"):
            status = STAGE_COMPLETE
        if status == STAGE_UNKNOWN and name == "AWARD" and awarded:
            status = STAGE_COMPLETE if _known(row.get("award_date") or row.get("award_number")) else STAGE_PLANNED

        evidence = local.get("evidence") or ex.get("evidence") or fact(status="UNKNOWN")
        if not isinstance(evidence, dict):
            evidence = fact(evidence)
        open_actions = list(local.get("open_actions") or [])
        if not open_actions and status not in {STAGE_COMPLETE}:
            open_actions = [local.get("next_action") or ex.get("next_action") or f"Advance {name.replace('_', ' ').lower()} with evidence"]
        unknowns = list(local.get("unknowns") or [])
        if status == STAGE_UNKNOWN:
            unknowns = unknowns or [f"{name} not evidenced"]

        stages.append(
            {
                "stage": name,
                "status": status,
                "evidence": evidence,
                "owner": local.get("owner") or ex.get("owner") or "UNKNOWN",
                "open_actions": open_actions,
                "unknowns": unknowns,
            }
        )

    blocked = [s for s in stages if s["status"] == STAGE_BLOCKED]
    open_s = [s for s in stages if s["status"] != STAGE_COMPLETE]
    return {
        "kind": "M3ContractLifecycle",
        "contract_id": _contract_id(row),
        "opportunity_id": row.get("canonical_id") or "UNKNOWN",
        "stages": stages,
        "current_focus": (blocked[0] if blocked else open_s[0] if open_s else stages[-1])["stage"],
        "note": "Award ≠ execution success · quote ≠ commitment · payment expectation ≠ payment received",
        "fabricated": False,
    }


# ---------------------------------------------------------------------------
# BUILD 3 — Contract Modification Intelligence
# ---------------------------------------------------------------------------
_MOD_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (MOD_QTY, re.compile(r"\b(?:quantity|qty)\s+(?:change|increase|decrease|modif)", re.I)),
    (MOD_DATE, re.compile(r"\b(?:delivery\s+date|period\s+of\s+performance|POP|extend(?:sion)?)\b", re.I)),
    (MOD_SPEC, re.compile(r"\b(?:specification|spec\s+change|NSN\s+change|part\s+number\s+change)\b", re.I)),
    (MOD_FUNDING, re.compile(r"\b(?:funding|obligation|incremental\s+funding|de[\s-]?obligat)", re.I)),
    (MOD_ADMIN, re.compile(r"\b(?:administrative\s+mod|admin\s+change|novation|address\s+change)\b", re.I)),
]


def record_contract_modification(payload: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
    entry = {
        "contract_id": payload.get("contract_id") or "UNKNOWN",
        "opportunity_id": payload.get("opportunity_id") or "UNKNOWN",
        "modification_number": payload.get("modification_number") or "UNKNOWN",
        "date": payload.get("date") or _utc(),
        "change_type": payload.get("change_type") or MOD_UNKNOWN,
        "affected_requirements": payload.get("affected_requirements") or ["UNKNOWN"],
        "affected_products": payload.get("affected_products") or ["UNKNOWN"],
        "affected_quantities": payload.get("affected_quantities") or "UNKNOWN",
        "affected_timeline": payload.get("affected_timeline") or "UNKNOWN",
        "evidence": payload.get("evidence") if payload.get("evidence") not in (None, "") else "UNKNOWN",
        "owner": payload.get("owner") or "UNKNOWN",
        "recorded_at": _utc(),
    }
    if persist:
        _append(MOD_INDEX_KEY, entry, by=str(entry["contract_id"]))
    return entry


def build_contract_modifications(row: dict[str, Any]) -> dict[str, Any]:
    mods = []
    for m in _as_dict(row.get("contract_modifications")).get("entries") or row.get("contract_modifications") or []:
        if isinstance(m, dict):
            mods.append(m)
    # Detect from amendment documents (detection ≠ typed classification without evidence)
    for d in row.get("documents") or []:
        if not isinstance(d, dict):
            continue
        if str(d.get("document_type") or "").upper() != "AMENDMENT" and not d.get("amendment_number"):
            continue
        text = str(d.get("extracted_text") or d.get("filename") or "")
        change_type = MOD_UNKNOWN
        for label, pat in _MOD_PATTERNS:
            if pat.search(text):
                change_type = label
                break
        mods.append(
            {
                "kind": "M3ContractModification",
                "modification_number": d.get("amendment_number") or d.get("filename") or "UNKNOWN",
                "date": d.get("date") or "UNKNOWN",
                "change_type": change_type,
                "affected_requirements": ["UNKNOWN"],
                "affected_products": ["UNKNOWN"],
                "affected_quantities": "UNKNOWN",
                "affected_timeline": "UNKNOWN",
                "evidence": text[:200] if text else "amendment_document",
                "source": "documents",
                "note": "Detection of amendment ≠ full change classification without evidence",
            }
        )

    cid = _contract_id(row)
    for e in _load_index(MOD_INDEX_KEY).get("entries") or []:
        if isinstance(e, dict) and e.get("contract_id") == cid:
            mods.append(e)

    return {
        "kind": "M3ContractModificationSet",
        "contract_id": cid,
        "modifications": mods[:40],
        "change_types": [MOD_QTY, MOD_DATE, MOD_SPEC, MOD_FUNDING, MOD_ADMIN, MOD_UNKNOWN],
        "fabricated": False,
    }


# ---------------------------------------------------------------------------
# BUILD 4 — Delivery Intelligence
# ---------------------------------------------------------------------------
def record_delivery(payload: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
    entry = {
        "kind": "M3DeliveryRecord",
        "contract": payload.get("contract") or payload.get("contract_id") or "UNKNOWN",
        "opportunity_id": payload.get("opportunity_id") or "UNKNOWN",
        "product": payload.get("product") or "UNKNOWN",
        "supplier": payload.get("supplier") or "UNKNOWN",
        "quantity_expected": payload.get("quantity_expected") if payload.get("quantity_expected") is not None else "UNKNOWN",
        "quantity_delivered": payload.get("quantity_delivered") if payload.get("quantity_delivered") is not None else "UNKNOWN",
        "delivery_date_expected": payload.get("delivery_date_expected") or "UNKNOWN",
        "delivery_date_actual": payload.get("delivery_date_actual") or "UNKNOWN",
        "shipping_evidence": payload.get("shipping_evidence") if payload.get("shipping_evidence") not in (None, "") else "UNKNOWN",
        "acceptance_evidence": payload.get("acceptance_evidence") if payload.get("acceptance_evidence") not in (None, "") else "UNKNOWN",
        "issues": payload.get("issues") or ["UNKNOWN"],
        "recorded_at": _utc(),
        "assumes_complete": False,
    }
    if persist:
        _append(DELIVERY_INDEX_KEY, entry, by=str(entry["contract"]))
    return entry


def build_delivery_records(row: dict[str, Any]) -> dict[str, Any]:
    entries = []
    for e in _as_dict(row.get("delivery_records")).get("entries") or row.get("delivery_records") or []:
        if isinstance(e, dict):
            entries.append(e)
    pay = _as_dict(row.get("payment_readiness"))
    # Surface payment readiness shipment evidence as delivery signal — not completion
    if _known(pay.get("shipment_evidence")):
        entries.append(
            {
                "kind": "M3DeliveryRecord",
                "contract": _contract_id(row),
                "product": "UNKNOWN",
                "supplier": "UNKNOWN",
                "quantity_expected": "UNKNOWN",
                "quantity_delivered": "UNKNOWN",
                "delivery_date_expected": "UNKNOWN",
                "delivery_date_actual": "UNKNOWN",
                "shipping_evidence": pay.get("shipment_evidence"),
                "acceptance_evidence": pay.get("receiving_evidence") or "UNKNOWN",
                "issues": ["UNKNOWN"],
                "assumes_complete": False,
                "note": "Shipment evidence present — delivery completion not assumed",
            }
        )
    cid = _contract_id(row)
    for e in _load_index(DELIVERY_INDEX_KEY).get("entries") or []:
        if isinstance(e, dict) and e.get("contract") == cid:
            entries.append(e)
    return {
        "kind": "M3DeliveryRecordSet",
        "contract_id": cid,
        "records": entries[:40],
        "note": "Do not assume delivery completion",
        "fabricated": False,
    }


# ---------------------------------------------------------------------------
# BUILD 5 — Acceptance Intelligence
# ---------------------------------------------------------------------------
def record_acceptance(payload: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
    status = str(payload.get("status") or ST_UNKNOWN).upper()
    if status not in {ACC_ACCEPTED, ACC_REJECTED, ACC_PENDING, ST_UNKNOWN}:
        status = ST_UNKNOWN
    entry = {
        "kind": "M3AcceptanceRecord",
        "contract": payload.get("contract") or "UNKNOWN",
        "opportunity_id": payload.get("opportunity_id") or "UNKNOWN",
        "requirement": payload.get("requirement") or "UNKNOWN",
        "acceptance_criteria": payload.get("acceptance_criteria") or "UNKNOWN",
        "evidence_submitted": payload.get("evidence_submitted") if payload.get("evidence_submitted") not in (None, "") else "UNKNOWN",
        "accepted_by": payload.get("accepted_by") or "UNKNOWN",
        "acceptance_date": payload.get("acceptance_date") or "UNKNOWN",
        "issues": payload.get("issues") or ["UNKNOWN"],
        "status": status,
        "recorded_at": _utc(),
    }
    if persist:
        _append(ACCEPTANCE_INDEX_KEY, entry, by=str(entry["contract"]))
    return entry


def build_acceptance_records(row: dict[str, Any]) -> dict[str, Any]:
    entries = []
    for e in _as_dict(row.get("acceptance_records")).get("entries") or row.get("acceptance_records") or []:
        if isinstance(e, dict):
            entries.append(e)
    pay = _as_dict(row.get("payment_readiness"))
    if _known(pay.get("acceptance_evidence")):
        entries.append(
            {
                "kind": "M3AcceptanceRecord",
                "requirement": "UNKNOWN",
                "acceptance_criteria": "UNKNOWN",
                "evidence_submitted": pay.get("acceptance_evidence"),
                "accepted_by": "UNKNOWN",
                "acceptance_date": "UNKNOWN",
                "issues": ["UNKNOWN"],
                "status": ACC_PENDING,  # evidence present ≠ accepted without explicit status
                "note": "Acceptance evidence field present — status PENDING until documented ACCEPTED/REJECTED",
            }
        )
    cid = _contract_id(row)
    for e in _load_index(ACCEPTANCE_INDEX_KEY).get("entries") or []:
        if isinstance(e, dict) and e.get("contract") == cid:
            entries.append(e)
    return {
        "kind": "M3AcceptanceRecordSet",
        "contract_id": cid,
        "records": entries[:40],
        "states": [ACC_ACCEPTED, ACC_REJECTED, ACC_PENDING, ST_UNKNOWN],
        "fabricated": False,
    }


# ---------------------------------------------------------------------------
# BUILD 6 — Payment Lifecycle Memory
# ---------------------------------------------------------------------------
def record_payment(payload: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
    status = str(payload.get("payment_status") or payload.get("status") or ST_UNKNOWN).upper()
    if status not in {PAY_INVOICED, PAY_SUBMITTED, PAY_PAID, PAY_DELAYED, ST_UNKNOWN}:
        status = ST_UNKNOWN
    entry = {
        "kind": "M3PaymentRecord",
        "contract": payload.get("contract") or "UNKNOWN",
        "opportunity_id": payload.get("opportunity_id") or "UNKNOWN",
        "invoice": payload.get("invoice") or "UNKNOWN",
        "amount": payload.get("amount") if payload.get("amount") is not None else "UNKNOWN",
        "invoice_date": payload.get("invoice_date") or "UNKNOWN",
        "submitted_date": payload.get("submitted_date") or "UNKNOWN",
        "payment_date": payload.get("payment_date") or "UNKNOWN",
        "payment_status": status,
        "evidence": payload.get("evidence") if payload.get("evidence") not in (None, "") else "UNKNOWN",
        "recorded_at": _utc(),
        "timing_inferred": False,
    }
    if persist:
        _append(PAYMENT_INDEX_KEY, entry, by=str(entry["contract"]))
    return entry


def build_payment_records(row: dict[str, Any]) -> dict[str, Any]:
    entries = []
    for e in _as_dict(row.get("payment_records")).get("entries") or row.get("payment_records") or []:
        if isinstance(e, dict):
            entries.append(e)
    cid = _contract_id(row)
    for e in _load_index(PAYMENT_INDEX_KEY).get("entries") or []:
        if isinstance(e, dict) and e.get("contract") == cid:
            entries.append(e)
    return {
        "kind": "M3PaymentRecordSet",
        "contract_id": cid,
        "records": entries[:40],
        "states": [PAY_INVOICED, PAY_SUBMITTED, PAY_PAID, PAY_DELAYED, ST_UNKNOWN],
        "note": "Do not infer payment timing — expectation ≠ received",
        "fabricated": False,
    }


# ---------------------------------------------------------------------------
# BUILD 7 — Contract Performance Memory
# ---------------------------------------------------------------------------
def record_contract_performance(payload: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
    entry = {
        "kind": "M3ContractPerformance",
        "contract": payload.get("contract") or "UNKNOWN",
        "opportunity_id": payload.get("opportunity_id") or "UNKNOWN",
        "products": payload.get("products") or ["UNKNOWN"],
        "suppliers": payload.get("suppliers") or ["UNKNOWN"],
        "timeline_results": payload.get("timeline_results") or "UNKNOWN",
        "issues": payload.get("issues") or ["UNKNOWN"],
        "resolutions": payload.get("resolutions") or ["UNKNOWN"],
        "lessons": payload.get("lessons") or ["UNKNOWN"],
        "evidence": payload.get("evidence") if payload.get("evidence") not in (None, "") else "UNKNOWN",
        "recorded_at": _utc(),
        "score": None,
        "ranking": None,
    }
    if persist:
        _append(PERF_INDEX_KEY, entry, by=str(entry["contract"]))
    return entry


def build_contract_performance(row: dict[str, Any]) -> dict[str, Any]:
    entries = []
    for e in _as_dict(row.get("contract_performance")).get("entries") or []:
        if isinstance(e, dict):
            entries.append(e)
    cid = _contract_id(row)
    for e in _load_index(PERF_INDEX_KEY).get("entries") or []:
        if isinstance(e, dict) and e.get("contract") == cid:
            entries.append(e)
    # Pull economic learning lessons without inventing scores
    try:
        from m3_economic_learning_read import build_operational_learning

        for e in build_operational_learning(row, limit=10).get("entries") or []:
            entries.append(
                {
                    "kind": "M3ContractPerformance",
                    "contract": cid,
                    "products": [e.get("product") or "UNKNOWN"],
                    "suppliers": [e.get("supplier") or "UNKNOWN"],
                    "timeline_results": "UNKNOWN",
                    "issues": e.get("problems") or ["UNKNOWN"],
                    "resolutions": e.get("solutions") or ["UNKNOWN"],
                    "lessons": e.get("reusable_knowledge") or ["UNKNOWN"],
                    "evidence": e.get("evidence") or "operational_learning",
                    "score": None,
                    "ranking": None,
                    "source_layer": "operational_learning",
                }
            )
    except Exception:
        pass
    return {
        "kind": "M3ContractPerformanceSet",
        "contract_id": cid,
        "entries": entries[:40],
        "scores_forbidden": True,
        "rankings_forbidden": True,
        "note": "Factual history only — no performance scores or rankings",
    }


# ---------------------------------------------------------------------------
# BUILD 8 — Closeout Memory
# ---------------------------------------------------------------------------
def record_closeout(payload: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
    entry = {
        "kind": "M3CloseoutRecord",
        "contract": payload.get("contract") or "UNKNOWN",
        "opportunity_id": payload.get("opportunity_id") or "UNKNOWN",
        "final_status": payload.get("final_status") or ST_UNKNOWN,
        "final_documents": payload.get("final_documents") or ["UNKNOWN"],
        "final_economics": payload.get("final_economics") or "UNKNOWN",
        "final_lessons": payload.get("final_lessons") or ["UNKNOWN"],
        "outstanding_issues": payload.get("outstanding_issues") or ["UNKNOWN"],
        "date": payload.get("date") or _utc(),
        "evidence": payload.get("evidence") if payload.get("evidence") not in (None, "") else "UNKNOWN",
        "recorded_at": _utc(),
    }
    if persist:
        _append(CLOSEOUT_INDEX_KEY, entry, by=str(entry["contract"]))
    return entry


def build_closeout_records(row: dict[str, Any]) -> dict[str, Any]:
    entries = []
    local = row.get("closeout_record")
    if isinstance(local, dict) and local:
        entries.append(local)
    for e in _as_dict(row.get("closeout_records")).get("entries") or []:
        if isinstance(e, dict):
            entries.append(e)
    cid = _contract_id(row)
    for e in _load_index(CLOSEOUT_INDEX_KEY).get("entries") or []:
        if isinstance(e, dict) and e.get("contract") == cid:
            entries.append(e)
    return {
        "kind": "M3CloseoutRecordSet",
        "contract_id": cid,
        "records": entries[:20],
        "fabricated": False,
    }


# ---------------------------------------------------------------------------
# BUILD 9 — Recompete / Future Demand Memory
# ---------------------------------------------------------------------------
def record_future_demand(payload: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
    entry = {
        "kind": "M3FutureDemandRecord",
        "previous_contract": payload.get("previous_contract") or "UNKNOWN",
        "opportunity_id": payload.get("opportunity_id") or "UNKNOWN",
        "product_category": payload.get("product_category") or payload.get("product") or "UNKNOWN",
        "agency": payload.get("agency") or "UNKNOWN",
        "expiration_information": payload.get("expiration_information") or "UNKNOWN",
        "renewal_indicators": payload.get("renewal_indicators") or ["UNKNOWN"],
        "replacement_opportunities": payload.get("replacement_opportunities") or ["UNKNOWN"],
        "evidence": payload.get("evidence") if payload.get("evidence") not in (None, "") else "UNKNOWN",
        "recorded_at": _utc(),
        "predicts_future_awards": False,
    }
    if persist:
        _append(FUTURE_DEMAND_KEY, entry, by=str(entry["previous_contract"]))
    return entry


def build_future_demand(row: dict[str, Any]) -> dict[str, Any]:
    entries = []
    for e in _as_dict(row.get("future_demand")).get("entries") or []:
        if isinstance(e, dict):
            entries.append(e)
    # Evidence of recurring demand from demand signals — not predictions
    demand = _as_dict(row.get("demand_signal") or row.get("demand_signals"))
    awards = row.get("award_history") or demand.get("awards") or []
    if isinstance(awards, list) and len(awards) >= 2:
        entries.append(
            {
                "kind": "M3FutureDemandRecord",
                "previous_contract": _contract_id(row),
                "product_category": "UNKNOWN",
                "agency": row.get("agency") or "UNKNOWN",
                "expiration_information": row.get("expiration_date") or "UNKNOWN",
                "renewal_indicators": ["multiple_historical_awards_evidenced"],
                "replacement_opportunities": ["UNKNOWN"],
                "evidence": f"{len(awards)} historical award records",
                "predicts_future_awards": False,
                "note": "Recurring demand evidence only — does not predict future awards",
            }
        )
    cid = _contract_id(row)
    for e in _load_index(FUTURE_DEMAND_KEY).get("entries") or []:
        if isinstance(e, dict) and e.get("previous_contract") == cid:
            entries.append(e)
    return {
        "kind": "M3FutureDemandRecordSet",
        "contract_id": cid,
        "records": entries[:30],
        "note": "Do not predict future awards — track recurring demand evidence only",
        "predicts_future_awards": False,
    }


# ---------------------------------------------------------------------------
# BUILD 10 — Contract Knowledge Graph Connections
# ---------------------------------------------------------------------------
def build_contract_knowledge_graph(row: dict[str, Any]) -> dict[str, Any]:
    master = build_contract_record(row)
    cid = _contract_id(row)
    nodes = [{"id": f"CONTRACT:{cid}", "type": "CONTRACT", "label": cid}]
    edges = []

    def _link(to_id: str, to_type: str, label: str, evidence: Any, rel: str) -> None:
        if not any(n["id"] == to_id for n in nodes):
            nodes.append({"id": to_id, "type": to_type, "label": label})
        edges.append(
            {
                "from": f"CONTRACT:{cid}",
                "to": to_id,
                "relationship": rel,
                "evidence": evidence if evidence not in (None, "") else "UNKNOWN",
                "timestamp": _utc(),
            }
        )

    for p in master.get("products") or []:
        label = str(p.get("nsn") or p.get("part_number") or "UNKNOWN")
        if label != "UNKNOWN":
            _link(f"PRODUCT:{label}", "PRODUCT", label, p.get("evidence"), "COVERS_PRODUCT")
    for s in master.get("suppliers") or []:
        name = s.get("supplier_name") or "UNKNOWN"
        if name != "UNKNOWN":
            _link(f"SUPPLIER:{name}", "SUPPLIER", name, s.get("evidence"), "USES_SUPPLIER")
    agency = _unwrap(master.get("agency"))
    if _known(agency):
        _link(f"AGENCY:{agency}", "AGENCY", str(agency), "contract_record", "AWARDED_BY")
    buyer = _unwrap(master.get("buyer"))
    if _known(buyer) and buyer != agency:
        _link(f"BUYER:{buyer}", "BUYER", str(buyer), "contract_record", "BUYER")
    for d in master.get("documents") or []:
        fn = d.get("filename") or "UNKNOWN"
        if fn != "UNKNOWN":
            _link(f"DOC:{fn}", "DOCUMENT", fn, d.get("evidence"), "HAS_DOCUMENT")

    opp = row.get("canonical_id")
    if opp:
        _link(f"OPP:{opp}", "OPPORTUNITY", str(opp), "pipeline", "RELATED_OPPORTUNITY")

    # Economic / lessons / actions — evidence required
    try:
        from m3_economic_learning_read import build_economic_profile, build_operational_learning

        econ = build_economic_profile(row)
        _link("ECON:profile", "ECONOMIC_RECORD", f"status:{econ.get('status')}", "economic_profile", "HAS_ECONOMICS")
        for e in build_operational_learning(row, limit=5).get("entries") or []:
            _link(
                f"LESSON:{e.get('recorded_at') or e.get('outcome')}",
                "LESSON",
                str((e.get("reusable_knowledge") or ["UNKNOWN"])[0])[:60],
                e.get("evidence") or "operational_learning",
                "HAS_LESSON",
            )
    except Exception:
        pass

    return {
        "kind": "M3ContractKnowledgeGraph",
        "contract_id": cid,
        "nodes": nodes,
        "relationships": edges,
        "note": "Relationships require evidence — unknown links not promoted",
        "fabricated": False,
    }


# ---------------------------------------------------------------------------
# BUILD 11 — Contract Timeline View
# ---------------------------------------------------------------------------
def record_timeline_event(payload: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
    entry = {
        "event": payload.get("event") or "UNKNOWN",
        "date": payload.get("date") or _utc(),
        "source": payload.get("source") or "UNKNOWN",
        "evidence": payload.get("evidence") if payload.get("evidence") not in (None, "") else "UNKNOWN",
        "contract": payload.get("contract") or "UNKNOWN",
        "opportunity_id": payload.get("opportunity_id") or "UNKNOWN",
        "recorded_at": _utc(),
    }
    if persist:
        _append(TIMELINE_INDEX_KEY, entry, by=str(entry["contract"]))
    return entry


def build_contract_timeline(row: dict[str, Any]) -> dict[str, Any]:
    events = []
    master = build_contract_record(row)
    if master.get("status") != ST_UNKNOWN:
        events.append(
            {
                "event": "Award received" if master["status"] in {CTR_AWARDED, CTR_ACTIVE, CTR_MODIFIED} else f"Status: {master['status']}",
                "date": _unwrap(master.get("award_date")),
                "source": "contract_record",
                "evidence": _as_dict(master.get("evidence")).get("evidence") or "award fields",
            }
        )
    for m in build_contract_modifications(row).get("modifications") or []:
        events.append(
            {
                "event": f"Modification added ({m.get('change_type') or MOD_UNKNOWN})",
                "date": m.get("date") or "UNKNOWN",
                "source": m.get("source") or "contract_modifications",
                "evidence": m.get("evidence") or "UNKNOWN",
            }
        )
    for d in build_delivery_records(row).get("records") or []:
        if _known(d.get("shipping_evidence")) or _known(d.get("delivery_date_actual")):
            events.append(
                {
                    "event": "Delivery evidence recorded",
                    "date": d.get("delivery_date_actual") or d.get("recorded_at") or "UNKNOWN",
                    "source": "delivery_records",
                    "evidence": d.get("shipping_evidence") or "UNKNOWN",
                }
            )
    for a in build_acceptance_records(row).get("records") or []:
        if a.get("status") == ACC_ACCEPTED:
            events.append(
                {
                    "event": "Acceptance documented",
                    "date": a.get("acceptance_date") or "UNKNOWN",
                    "source": "acceptance_records",
                    "evidence": a.get("evidence_submitted") or "UNKNOWN",
                }
            )
    for p in build_payment_records(row).get("records") or []:
        if p.get("payment_status") == PAY_PAID:
            events.append(
                {
                    "event": "Payment received",
                    "date": p.get("payment_date") or "UNKNOWN",
                    "source": "payment_records",
                    "evidence": p.get("evidence") or "UNKNOWN",
                }
            )
    for c in build_closeout_records(row).get("records") or []:
        events.append(
            {
                "event": "Lessons / closeout captured",
                "date": c.get("date") or "UNKNOWN",
                "source": "closeout_records",
                "evidence": c.get("evidence") or "UNKNOWN",
            }
        )
    cid = _contract_id(row)
    for e in _load_index(TIMELINE_INDEX_KEY).get("entries") or []:
        if isinstance(e, dict) and e.get("contract") == cid:
            events.append(e)
    # Sort by date string when possible
    events.sort(key=lambda e: str(e.get("date") or "UNKNOWN"))
    return {
        "kind": "M3ContractTimeline",
        "contract_id": cid,
        "events": events[:60],
        "note": "Each event requires date, source, evidence",
        "fabricated": False,
    }


# ---------------------------------------------------------------------------
# BUILD 13 — Decision Trace Integration
# ---------------------------------------------------------------------------
def build_contract_decision_trace(
    *,
    question: str,
    row: dict[str, Any] | None = None,
    action_taken: str | None = None,
    persist: bool = False,
) -> dict[str, Any]:
    row = row if isinstance(row, dict) else {}
    lifecycle = build_contract_lifecycle(row)
    known = []
    unknowns = []
    for s in lifecycle.get("stages") or []:
        if s.get("status") == STAGE_COMPLETE:
            known.append({"fact": s["stage"], "status": s["status"], "evidence": s.get("evidence")})
        else:
            unknowns.extend(s.get("unknowns") or [s["stage"]])
    evidence_reviewed = [
        {"layer": "contract_record", "status": build_contract_record(row).get("status")},
        {"layer": "lifecycle", "focus": lifecycle.get("current_focus")},
        {"layer": "delivery", "count": len(build_delivery_records(row).get("records") or [])},
        {"layer": "payment", "count": len(build_payment_records(row).get("records") or [])},
    ]
    # Reuse economic decision trace when available
    try:
        from m3_economic_learning_read import build_decision_trace

        econ_trace = build_decision_trace(question=question or "What happened after the contract was won?", row=row, persist=False)
        evidence_reviewed.append({"layer": "economic_decision_trace", "action": econ_trace.get("generated_action")})
    except Exception:
        econ_trace = {}

    action = action_taken or f"Focus on {lifecycle.get('current_focus')} — resolve: {', '.join(str(u) for u in unknowns[:3]) or 'UNKNOWN'}"
    trace = {
        "kind": "M3ContractDecisionTrace",
        "opportunity_id": row.get("canonical_id") or "UNKNOWN",
        "contract_id": _contract_id(row),
        "question": question or "What evidence proves execution status?",
        "evidence_reviewed": evidence_reviewed,
        "known_facts": known,
        "unknowns": unknowns[:20],
        "action_taken": action,
        "date": _utc(),
        "numeric_score": None,
        "automatic_conclusion": False,
    }
    if persist:
        try:
            from m3_economic_learning_read import TRACE_INDEX_KEY as ECON_TRACE_KEY

            _append(ECON_TRACE_KEY, trace, by=str(trace["contract_id"]), cap=300)
        except Exception:
            _append(TIMELINE_INDEX_KEY, {"event": "decision_trace", **trace}, by=str(trace["contract_id"]))
    return trace


# ---------------------------------------------------------------------------
# Full profile + attach + command center
# ---------------------------------------------------------------------------
def build_contract_lifecycle_profile(row: dict[str, Any] | None) -> dict[str, Any]:
    row = row if isinstance(row, dict) else {}
    return {
        "kind": "M3ContractLifecycleProfile",
        "build": BUILD_TAG,
        "opportunity_id": row.get("canonical_id") or "UNKNOWN",
        "contract_record": build_contract_record(row),
        "lifecycle": build_contract_lifecycle(row),
        "modifications": build_contract_modifications(row),
        "delivery": build_delivery_records(row),
        "acceptance": build_acceptance_records(row),
        "payment": build_payment_records(row),
        "performance": build_contract_performance(row),
        "closeout": build_closeout_records(row),
        "future_demand": build_future_demand(row),
        "knowledge_graph": build_contract_knowledge_graph(row),
        "timeline": build_contract_timeline(row),
        "decision_trace": build_contract_decision_trace(
            question="What happened after the contract was won?",
            row=row,
            persist=False,
        ),
        "questions": [
            "What happened after the contract was won?",
            "What evidence proves execution status?",
            "What problems occurred?",
            "What should be remembered?",
            "What can improve future opportunities?",
        ],
        "facts_only": True,
        "unknown_preserved": True,
        "no_invented_performance": True,
        "no_invented_payment_status": True,
        "no_automatic_future_predictions": True,
        "no_numeric_scores": True,
        "engines_unchanged": True,
        "scoring_unchanged": True,
        "generated_at": _utc(),
        "read_only": True,
    }


def attach_contract_lifecycle_to_deal_room(deal: dict[str, Any], *, row: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(deal, dict):
        return deal
    out = dict(deal)
    try:
        out["contract_lifecycle"] = build_contract_lifecycle_profile(row or {"canonical_id": deal.get("canonical_id")})
    except Exception:
        out["contract_lifecycle"] = {
            "kind": "M3ContractLifecycleProfile",
            "build": BUILD_TAG,
            "error": "contract_lifecycle_unavailable",
            "read_only": True,
        }
    return out


def enrich_command_center_contract(
    sections: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    period: str,
) -> dict[str, Any]:
    """BUILD 12 — contract actions / delivery / payment signals."""
    out = dict(sections or {})
    contract_actions = []
    missing_exec = []
    delivery_blockers = []
    payment_unknowns = []
    expiring_reqs = []
    completed_milestones = []
    new_contract_intel = []
    unresolved = list(out.get("unresolved_blockers") or [])

    for r in rows[:50]:
        if not isinstance(r, dict):
            continue
        cid = r.get("canonical_id")
        title = (r.get("title") or "")[:80]
        life = build_contract_lifecycle(r)
        for s in life.get("stages") or []:
            if s.get("status") == STAGE_BLOCKED:
                contract_actions.append(
                    {
                        "opportunity_id": cid,
                        "title": title,
                        "stage": s.get("stage"),
                        "action": (s.get("open_actions") or ["Resolve blocker"])[0],
                        "owner": s.get("owner"),
                    }
                )
                unresolved.append(
                    {
                        "opportunity_id": cid,
                        "title": title,
                        "problem": f"Blocked: {s.get('stage')}",
                        "owner": s.get("owner"),
                        "action": (s.get("open_actions") or ["Resolve"])[0],
                    }
                )
            elif s.get("status") not in {STAGE_COMPLETE} and s.get("unknowns"):
                missing_exec.append(
                    {
                        "opportunity_id": cid,
                        "title": title,
                        "stage": s.get("stage"),
                        "unknowns": s.get("unknowns"),
                        "action": (s.get("open_actions") or ["Obtain evidence"])[0],
                    }
                )
            if s.get("status") == STAGE_COMPLETE:
                completed_milestones.append(
                    {"opportunity_id": cid, "title": title, "stage": s.get("stage")}
                )

        for d in build_delivery_records(r).get("records") or []:
            if d.get("assumes_complete") is False and not _known(d.get("delivery_date_actual")):
                if _known(d.get("shipping_evidence")) or d.get("quantity_expected") != "UNKNOWN":
                    delivery_blockers.append(
                        {
                            "opportunity_id": cid,
                            "title": title,
                            "issue": "Delivery not evidenced complete",
                            "action": "Obtain delivery/acceptance evidence",
                        }
                    )

        pays = build_payment_records(r).get("records") or []
        if not pays:
            master = build_contract_record(r)
            if master.get("status") in {CTR_AWARDED, CTR_ACTIVE, CTR_MODIFIED}:
                payment_unknowns.append(
                    {
                        "opportunity_id": cid,
                        "title": title,
                        "unknown": "payment_status",
                        "action": "Record invoice/payment evidence when available",
                    }
                )
        else:
            for p in pays:
                if p.get("payment_status") in {ST_UNKNOWN, PAY_DELAYED, PAY_SUBMITTED}:
                    payment_unknowns.append(
                        {
                            "opportunity_id": cid,
                            "title": title,
                            "unknown": p.get("payment_status"),
                            "invoice": p.get("invoice"),
                            "action": "Update payment evidence — do not infer timing",
                        }
                    )

        master = build_contract_record(r)
        exp = _unwrap(master.get("expiration_date"))
        if _known(exp):
            expiring_reqs.append(
                {
                    "opportunity_id": cid,
                    "title": title,
                    "what": "contract_expiration",
                    "value": exp,
                }
            )

        mods = build_contract_modifications(r).get("modifications") or []
        if mods:
            new_contract_intel.append(
                {
                    "opportunity_id": cid,
                    "title": title,
                    "what": "modifications",
                    "count": len(mods),
                }
            )

    if period == "morning":
        out["contract_actions_due"] = contract_actions[:15]
        out["missing_execution_evidence"] = missing_exec[:15]
        out["delivery_blockers"] = delivery_blockers[:15]
        out["payment_unknowns"] = payment_unknowns[:15]
        out["expiring_requirements"] = expiring_reqs[:15]
    else:
        out["completed_milestones"] = completed_milestones[:15]
        out["new_contract_intelligence"] = new_contract_intel[:15]
        out["unresolved_execution_issues"] = unresolved[:15]
        out["next_actions"] = (contract_actions + missing_exec)[:15]
        out.setdefault("completed_actions", out.get("completed_actions") or [])
    return out
