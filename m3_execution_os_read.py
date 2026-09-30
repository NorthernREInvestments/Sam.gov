"""BUILD 15 — M3 Execution Intelligence Layer (read models).

Extends opportunity intelligence into an operating system for small
product-resale contractors. Read models first; reuses existing graph,
supplier, economics, submission, and offer-readiness patterns.

Does NOT rebuild engines, invent prices/suppliers/financing, or convert
UNKNOWN into confidence.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from application_clock import now_utc

BUILD_TAG = "20260919-m3-execution-os-1"

# Stage / lifecycle states
ST_UNKNOWN = "UNKNOWN"
ST_PLANNED = "PLANNED"
ST_IN_PROGRESS = "IN_PROGRESS"
ST_COMPLETE = "COMPLETE"
ST_BLOCKED = "BLOCKED"

# Payment
PAY_READY = "PAYMENT_READY"
PAY_BLOCKED = "PAYMENT_BLOCKED"
PAY_UNKNOWN = "UNKNOWN"

# Roles (permissions structure — no silent critical edits)
ROLE_OWNER = "OWNER"
ROLE_MANAGER = "MANAGER"
ROLE_RESEARCHER = "RESEARCHER"
ROLE_VIEWER = "VIEWER"

AWARD_STAGES = (
    "AWARD",
    "SUPPLIER_COMMITMENT",
    "PURCHASE_ORDER",
    "PRODUCT_ACQUISITION",
    "SHIPMENT",
    "GOVERNMENT_RECEIPT",
    "INSPECTION",
    "ACCEPTANCE",
    "INVOICE",
    "PAYMENT",
    "CLOSEOUT",
)

OWNERSHIP_CHAIN = (
    "SUPPLIER_OWNERSHIP",
    "COMPANY_OWNERSHIP",
    "CARRIER_RESPONSIBILITY",
    "GOVERNMENT_RECEIPT",
    "GOVERNMENT_ACCEPTANCE",
)

MEMORY_INDEX_KEY = "m3_contract_memory_v1"
PERF_INDEX_KEY = "m3_performance_learning_v1"
AUDIT_INDEX_KEY = "m3_execution_audit_v1"


def _utc() -> str:
    return now_utc().isoformat()


def _clean(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def fact(
    value: Any = None,
    *,
    source: str = "UNKNOWN",
    date: str | None = None,
    confidence: str = "UNKNOWN",
    evidence: Any = "UNKNOWN",
    status: str = ST_UNKNOWN,
) -> dict[str, Any]:
    """Canonical fact envelope — UNKNOWN stays UNKNOWN."""
    if value is None or value == "" or str(value).upper() in {"NONE", "NULL"}:
        value = "UNKNOWN"
    if evidence is None or evidence == "":
        evidence = "UNKNOWN"
    return {
        "value": value,
        "source": source or "UNKNOWN",
        "date": date or "UNKNOWN",
        "confidence": confidence or "UNKNOWN",
        "evidence": evidence,
        "status": status or ST_UNKNOWN,
    }


def _as_dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _num(v: Any) -> float | None:
    if v is None or v == "" or str(v).upper() == "UNKNOWN":
        return None
    try:
        return float(str(v).replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return None


def _load_index(key: str) -> dict[str, Any]:
    """AppSetting JSON index — same pattern as research queue / graph."""
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


def _unwrap_value(v: Any) -> Any:
    """Flatten nested evidence envelopes without inventing values."""
    if isinstance(v, dict):
        if "value" in v:
            return v.get("value")
        return "UNKNOWN"
    return v


# ---------------------------------------------------------------------------
# 1. Award execution control
# ---------------------------------------------------------------------------
def build_award_execution_control(row: dict[str, Any]) -> dict[str, Any]:
    """Post-award lifecycle tracking — status/evidence/owner/next/blockers per stage."""
    ann = _as_dict(row.get("award_execution"))
    stages_in = _as_dict(ann.get("stages"))
    lifecycle = str(row.get("lifecycle") or "").upper()
    awarded = lifecycle in {"AWARDED", "WON", "EXECUTION", "FULFILLMENT", "PAYMENT", "CLOSEOUT"} or bool(
        row.get("award_date") or row.get("award_number")
    )

    stages: list[dict[str, Any]] = []
    for name in AWARD_STAGES:
        stored = _as_dict(stages_in.get(name))
        status = str(stored.get("status") or ST_UNKNOWN).upper()
        if status not in {ST_UNKNOWN, ST_PLANNED, ST_IN_PROGRESS, ST_COMPLETE, ST_BLOCKED}:
            status = ST_UNKNOWN
        # Soft planning defaults only — never invent completion
        if status == ST_UNKNOWN and awarded and name == "AWARD":
            status = ST_PLANNED if not row.get("award_date") else ST_IN_PROGRESS
            if row.get("award_date") or row.get("award_number"):
                status = ST_COMPLETE if stored.get("status") in (None, "", ST_UNKNOWN) else status
                if not stored.get("status"):
                    status = ST_COMPLETE

        evidence = stored.get("evidence")
        if not evidence:
            if name == "AWARD" and (row.get("award_number") or row.get("award_date")):
                evidence = fact(
                    row.get("award_number") or row.get("award_date"),
                    source="pipeline.award",
                    date=str(row.get("award_date") or "UNKNOWN"),
                    confidence="MEDIUM",
                    evidence="Award fields present on pipeline row",
                    status=ST_COMPLETE if status == ST_COMPLETE else ST_PLANNED,
                )
            else:
                evidence = fact(status="UNKNOWN")

        blockers = list(stored.get("blockers") or [])
        if status == ST_UNKNOWN and awarded and name != "AWARD":
            blockers = blockers or ["Stage not evidenced yet"]

        stages.append(
            {
                "stage": name,
                "status": status,
                "evidence": evidence if isinstance(evidence, dict) else fact(evidence),
                "owner": stored.get("owner") or "UNKNOWN",
                "next_action": stored.get("next_action")
                or (
                    "Record award evidence"
                    if name == "AWARD" and status != ST_COMPLETE
                    else f"Advance {name.replace('_', ' ').lower()} with evidence"
                ),
                "blockers": blockers if blockers else ["UNKNOWN"] if status == ST_UNKNOWN else [],
            }
        )

    blocked = [s for s in stages if s["status"] == ST_BLOCKED]
    open_stages = [s for s in stages if s["status"] not in {ST_COMPLETE}]
    return {
        "kind": "M3AwardExecutionControl",
        "opportunity_id": row.get("canonical_id"),
        "awarded_signal": awarded,
        "stages": stages,
        "current_focus": (blocked[0] if blocked else open_stages[0] if open_stages else stages[-1])["stage"],
        "question": "Where are we in post-award execution?",
        "fabricated": False,
    }


# ---------------------------------------------------------------------------
# 2. Supplier commitment intelligence
# ---------------------------------------------------------------------------
def build_supplier_commitment(row: dict[str, Any]) -> dict[str, Any]:
    """Supplier exists ≠ execution ready. Expand commitment facts only."""
    si = _as_dict(row.get("supplier_intelligence"))
    spg = _as_dict(row.get("supplier_product_graph"))
    commit = _as_dict(row.get("supplier_commitment"))
    edges = [e for e in (spg.get("edges") or []) if isinstance(e, dict)]

    suppliers = []
    for e in edges[:12]:
        name = e.get("supplier_name") or "UNKNOWN"
        stored = _as_dict((_as_dict(commit.get("by_supplier")).get(str(name))))
        suppliers.append(
            {
                "supplier_name": name,
                "relationship": fact(
                    e.get("relationship_type"),
                    source="supplier_product_graph",
                    confidence=str(e.get("confidence") or "UNKNOWN"),
                    evidence="Graph edge — not commitment proof",
                    status=ST_UNKNOWN,
                ),
                "product_capability": fact(
                    stored.get("product_capability"),
                    source=stored.get("product_capability_source") or "UNKNOWN",
                    confidence=stored.get("product_capability_confidence") or "UNKNOWN",
                    evidence=stored.get("product_capability_evidence") or "UNKNOWN",
                ),
                "quote_received": fact(stored.get("quote_received")),
                "quote_date": fact(stored.get("quote_date")),
                "quote_expiration": fact(stored.get("quote_expiration")),
                "lead_time": fact(stored.get("lead_time")),
                "payment_terms": fact(stored.get("payment_terms")),
                "po_acceptance": fact(stored.get("po_acceptance")),
                "direct_shipment_capability": fact(stored.get("direct_shipment_capability")),
                "traceability_capability": fact(stored.get("traceability_capability")),
                "warranty_process": fact(stored.get("warranty_process")),
                "replacement_process": fact(stored.get("replacement_process")),
                "execution_ready": bool(
                    stored.get("quote_received") not in (None, "", "UNKNOWN", False)
                    and stored.get("po_acceptance") not in (None, "", "UNKNOWN", False)
                ),
                "note": "Supplier present on graph is not execution-ready without quote + PO acceptance evidence",
            }
        )

    # Surface SI possible suppliers without marking ready
    for s in (si.get("Possible_Suppliers") or [])[:6]:
        if not isinstance(s, dict):
            continue
        name = s.get("company") or "UNKNOWN"
        if any(x["supplier_name"] == name for x in suppliers):
            continue
        suppliers.append(
            {
                "supplier_name": name,
                "relationship": fact(s.get("role"), source="supplier_intelligence", status=ST_UNKNOWN),
                "product_capability": fact(),
                "quote_received": fact(),
                "quote_date": fact(),
                "quote_expiration": fact(),
                "lead_time": fact(),
                "payment_terms": fact(),
                "po_acceptance": fact(),
                "direct_shipment_capability": fact(),
                "traceability_capability": fact(),
                "warranty_process": fact(),
                "replacement_process": fact(),
                "execution_ready": False,
                "note": "Listed possible supplier — commitment UNKNOWN",
            }
        )

    return {
        "kind": "M3SupplierCommitmentIntelligence",
        "opportunity_id": row.get("canonical_id"),
        "suppliers": suppliers or [
            {
                "supplier_name": "UNKNOWN",
                "execution_ready": False,
                "quote_received": fact(),
                "note": "No suppliers evidenced",
            }
        ],
        "execution_ready_count": sum(1 for s in suppliers if s.get("execution_ready")),
        "question": "Which suppliers have evidenced commitment for execution?",
        "assumes_capability": False,
    }


# ---------------------------------------------------------------------------
# 3. Cash survival model
# ---------------------------------------------------------------------------
def build_cash_survival_model(row: dict[str, Any]) -> dict[str, Any]:
    """Factual cash exposure — never says 'this is financeable'."""
    de = _as_dict(row.get("deal_economics"))
    profile = _as_dict(de.get("DEAL_ECONOMICS_PROFILE") or de)
    exec_i = _as_dict(row.get("execution_intelligence"))
    cash = _as_dict(row.get("cash_survival"))
    funding = _as_dict(row.get("funding_requirement"))

    def money_fact(key: str, *sources: Any) -> dict[str, Any]:
        for src_name, bag in sources:
            if not isinstance(bag, dict):
                continue
            if key in bag and bag[key] not in (None, "", "UNKNOWN"):
                return fact(
                    bag[key],
                    source=src_name,
                    confidence=str(bag.get(f"{key}_confidence") or bag.get("PRICE_CONFIDENCE") or "UNKNOWN"),
                    evidence=f"{key} from {src_name}",
                    status=ST_UNKNOWN if _num(bag[key]) is None else ST_PLANNED,
                )
        return fact()

    supplier_pay = money_fact(
        "supplier_payment_required",
        ("cash_survival", cash),
        ("deal_economics", profile),
        ("execution_intelligence", exec_i),
    )
    if supplier_pay["value"] == "UNKNOWN":
        # Map known fields without inventing
        for k in ("Required_Acquisition_Cost", "acquisition_cost", "Estimated_Capital_Needed"):
            v = profile.get(k) or de.get(k) or exec_i.get(k) or funding.get("capital_amount")
            if v not in (None, "", "UNKNOWN"):
                supplier_pay = fact(
                    v,
                    source="deal_economics|execution_intelligence",
                    confidence=str(de.get("PRICE_CONFIDENCE") or "UNKNOWN"),
                    evidence=f"Mapped from {k} — not a financing approval",
                    status=ST_PLANNED,
                )
                break

    paths = []
    for p in cash.get("financing_paths") or exec_i.get("financing_paths") or []:
        if isinstance(p, dict):
            paths.append(
                {
                    "path": p.get("path") or p.get("name") or "UNKNOWN",
                    "requirements": p.get("requirements") or ["UNKNOWN"],
                    "missing_information": p.get("missing_information") or ["UNKNOWN"],
                    "status": p.get("status") or ST_UNKNOWN,
                    "evidence": p.get("evidence") or fact(),
                }
            )
    if not paths:
        paths = [
            {
                "path": "UNKNOWN",
                "requirements": ["UNKNOWN"],
                "missing_information": ["financing_path_not_evidenced"],
                "status": ST_UNKNOWN,
                "evidence": fact(evidence="No financing path evidenced — do not assume financeable"),
            }
        ]

    return {
        "kind": "M3CashSurvivalModel",
        "opportunity_id": row.get("canonical_id"),
        "question": "What cash exposure is evidenced, and what financing facts are missing?",
        "supplier_payment_requirements": supplier_pay,
        "deposits": money_fact("deposit", ("cash_survival", cash)),
        "shipping_costs": money_fact("shipping_cost", ("cash_survival", cash), ("deal_economics", profile)),
        "government_payment_trigger": fact(
            cash.get("government_payment_trigger") or "UNKNOWN",
            source="cash_survival" if cash.get("government_payment_trigger") else "UNKNOWN",
            evidence=cash.get("government_payment_trigger_evidence") or "UNKNOWN",
        ),
        "acceptance_timing": fact(cash.get("acceptance_timing")),
        "invoice_timing": fact(cash.get("invoice_timing")),
        "cash_exposure": money_fact("cash_exposure", ("cash_survival", cash)),
        "financing_requirements": fact(
            cash.get("financing_requirements") or exec_i.get("Financing_Fit") or "UNKNOWN",
            source="cash_survival|execution_intelligence",
            evidence="Fit/status label only — not an approval",
        ),
        "identified_financing_paths": paths,
        "is_financeable_claim": False,
        "note": "Never claims financeability — shows paths, requirements, and gaps only",
    }


# ---------------------------------------------------------------------------
# 4. Ownership / risk timeline
# ---------------------------------------------------------------------------
def build_ownership_risk_timeline(row: dict[str, Any]) -> dict[str, Any]:
    struct = _as_dict(row.get("dla_product_structure"))
    fields = _as_dict(struct.get("fields"))
    fob_field = fields.get("fob")
    fob_val = fob_field.get("value") if isinstance(fob_field, dict) else struct.get("fob")
    fob_ev = fob_field if isinstance(fob_field, dict) else None
    own = _as_dict(row.get("ownership_risk"))

    fob = fact(
        fob_val or own.get("fob") or "UNKNOWN",
        source=(fob_ev or {}).get("evidence_source") or "dla_product_structure",
        confidence=(fob_ev or {}).get("confidence") or "UNKNOWN",
        evidence=(fob_ev or {}).get("evidence_snippet") or own.get("fob_evidence") or "UNKNOWN",
        status=ST_PLANNED if fob_val else ST_UNKNOWN,
    )

    chain = []
    for step in OWNERSHIP_CHAIN:
        stored = _as_dict(_as_dict(own.get("chain")).get(step))
        chain.append(
            {
                "stage": step,
                "status": stored.get("status") or ST_UNKNOWN,
                "risk_transfer": fact(stored.get("risk_transfer")),
                "insurance_requirements": fact(stored.get("insurance_requirements")),
                "ownership_evidence": fact(stored.get("ownership_evidence")),
                "owner": stored.get("owner") or "UNKNOWN",
            }
        )

    return {
        "kind": "M3OwnershipRiskTimeline",
        "opportunity_id": row.get("canonical_id"),
        "fob_terms": fob,
        "chain": chain,
        "question": "Where does ownership and risk sit, with evidence?",
    }


# ---------------------------------------------------------------------------
# 5. Product change control
# ---------------------------------------------------------------------------
def build_product_change_control(row: dict[str, Any]) -> dict[str, Any]:
    struct = _as_dict(row.get("dla_product_structure"))
    fields = _as_dict(struct.get("fields"))
    pi = _as_dict(row.get("product_identity"))
    proposed = _as_dict(row.get("supplier_proposed_product") or row.get("product_change_control"))

    def _id(key: str, *bags: dict[str, Any]) -> dict[str, Any]:
        for b in bags:
            f = b.get(key) if isinstance(b, dict) else None
            if isinstance(f, dict) and f.get("value") not in (None, "", "UNKNOWN"):
                return fact(
                    f.get("value"),
                    source=f.get("evidence_source") or "product_identity",
                    confidence=f.get("confidence") or "UNKNOWN",
                    evidence=f.get("evidence_snippet") or "UNKNOWN",
                    status=ST_PLANNED,
                )
            if b.get(key) not in (None, "", "UNKNOWN") and not isinstance(b.get(key), dict):
                return fact(b.get(key), source="pipeline", status=ST_PLANNED)
        return fact()

    requested = {
        "nsn": _id("nsn", fields, struct, pi),
        "part_number": _id("part_number", fields, struct, pi),
        "manufacturer": _id("oem", fields, struct, pi) if fields else _id("manufacturer", pi, struct),
        "cage": _id("cage", fields, struct, pi),
        "specifications": fact(proposed.get("requested_specifications") or "UNKNOWN"),
    }
    offered = {
        "nsn": fact(proposed.get("nsn")),
        "part_number": fact(proposed.get("part_number")),
        "manufacturer": fact(proposed.get("manufacturer")),
        "cage": fact(proposed.get("cage")),
        "specifications": fact(proposed.get("specifications")),
    }

    diffs = []
    for k in ("nsn", "part_number", "manufacturer", "cage", "specifications"):
        rv, ov = requested[k]["value"], offered[k]["value"]
        if ov != "UNKNOWN" and rv != "UNKNOWN" and str(rv).upper() != str(ov).upper():
            diffs.append(
                {
                    "field": k,
                    "requested": rv,
                    "proposed": ov,
                    "approval_required": True,
                    "status": ST_BLOCKED,
                    "note": "Equivalent does not mean compliant",
                    "evidence": fact(
                        f"{rv} → {ov}",
                        evidence="Requested vs supplier-proposed mismatch",
                        status=ST_BLOCKED,
                    ),
                }
            )
        elif ov == "UNKNOWN" and rv == "UNKNOWN":
            diffs.append(
                {
                    "field": k,
                    "requested": rv,
                    "proposed": ov,
                    "approval_required": False,
                    "status": ST_UNKNOWN,
                    "note": "Both UNKNOWN",
                    "evidence": fact(),
                }
            )

    return {
        "kind": "M3ProductChangeControl",
        "opportunity_id": row.get("canonical_id"),
        "requested": requested,
        "supplier_proposed": offered,
        "differences": diffs,
        "has_substitution_risk": any(d.get("status") == ST_BLOCKED for d in diffs),
        "note": "Equivalent does not mean compliant — silent substitutions forbidden",
    }


# ---------------------------------------------------------------------------
# 6. Payment readiness
# ---------------------------------------------------------------------------
def build_payment_readiness(row: dict[str, Any]) -> dict[str, Any]:
    pay = _as_dict(row.get("payment_readiness"))
    dla = _as_dict(row.get("dla_product_structure"))
    dla_fields = _as_dict(dla.get("fields"))
    pi = _as_dict(row.get("product_identity"))

    def _pay_val(*candidates: Any) -> Any:
        for c in candidates:
            v = _unwrap_value(c)
            if v not in (None, "", "UNKNOWN"):
                return v
        return None

    checks = {
        "contract_number": fact(
            _pay_val(pay.get("contract_number"), row.get("contract_number"), row.get("award_number"))
        ),
        "clin": fact(_pay_val(pay.get("clin"), row.get("clin"))),
        "part_number": fact(
            _pay_val(
                pay.get("part_number"),
                dla_fields.get("part_number"),
                dla.get("part_number"),
                pi.get("part_number"),
            )
        ),
        "quantity": fact(_pay_val(pay.get("quantity"), dla_fields.get("quantity"), dla.get("quantity"))),
        "unit_of_measure": fact(
            _pay_val(pay.get("unit_of_measure"), dla_fields.get("unit_of_issue"), dla.get("unit_of_issue"))
        ),
        "price": fact(_pay_val(pay.get("price"), row.get("unit_price"), row.get("award_amount"))),
        "shipment_evidence": fact(_pay_val(pay.get("shipment_evidence"))),
        "receiving_evidence": fact(_pay_val(pay.get("receiving_evidence"))),
        "acceptance_evidence": fact(_pay_val(pay.get("acceptance_evidence"))),
        "required_documents": fact(_pay_val(pay.get("required_documents"))),
    }

    missing = [k for k, v in checks.items() if v.get("value") == "UNKNOWN"]
    if missing:
        # If nothing evidenced at all beyond unknowns
        if len(missing) == len(checks):
            state = PAY_UNKNOWN
        else:
            state = PAY_BLOCKED
    else:
        state = PAY_READY

    return {
        "kind": "M3PaymentReadiness",
        "opportunity_id": row.get("canonical_id"),
        "status": state,
        "checks": checks,
        "missing": missing or [],
        "blockers": [f"Missing evidenced {m}" for m in missing] if state == PAY_BLOCKED else [],
        "question": "Can we submit an invoice with evidenced completeness?",
        "note": "UNKNOWN fields block PAYMENT_READY — never invent invoice facts",
    }


# ---------------------------------------------------------------------------
# 7. Contract memory
# ---------------------------------------------------------------------------
def record_contract_memory(
    *,
    opportunity_id: str,
    field: str,
    previous_value: Any,
    new_value: Any,
    reason: str,
    evidence: Any,
    user: str = "system",
    persist: bool = True,
) -> dict[str, Any]:
    """Append-only history — never overwrite."""
    entry = {
        "opportunity_id": opportunity_id,
        "field": field,
        "previous_value": previous_value if previous_value is not None else "UNKNOWN",
        "new_value": new_value if new_value is not None else "UNKNOWN",
        "change_date": _utc(),
        "reason": reason or "UNKNOWN",
        "evidence": evidence if evidence not in (None, "") else "UNKNOWN",
        "user": user,
    }
    idx = _load_index(MEMORY_INDEX_KEY)
    entries = list(idx.get("entries") or [])
    entries.append(entry)
    idx["entries"] = entries[-500:]
    by = _as_dict(idx.get("by_key"))
    key = f"{opportunity_id}|{field}"
    by.setdefault(key, []).append(entry)
    by[key] = by[key][-50:]
    idx["by_key"] = by
    if persist:
        _save_index(MEMORY_INDEX_KEY, idx)
    return entry


def build_contract_memory(row: dict[str, Any], *, limit: int = 40) -> dict[str, Any]:
    cid = str(row.get("canonical_id") or "")
    idx = _load_index(MEMORY_INDEX_KEY)
    local = [e for e in (_as_dict(row.get("contract_memory")).get("entries") or []) if isinstance(e, dict)]
    global_e = [e for e in (idx.get("entries") or []) if isinstance(e, dict) and e.get("opportunity_id") == cid]
    merged = local + global_e
    # newest first
    merged.sort(key=lambda e: str(e.get("change_date") or ""), reverse=True)
    return {
        "kind": "M3ContractMemory",
        "opportunity_id": cid or "UNKNOWN",
        "entries": merged[:limit],
        "overwrite_forbidden": True,
        "question": "What changed, with previous values preserved?",
    }


# ---------------------------------------------------------------------------
# 8. Performance learning loop
# ---------------------------------------------------------------------------
def record_performance_outcome(payload: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
    """Capture factual post-completion results — no fake scores."""
    entry = {
        "opportunity_id": payload.get("opportunity_id") or "UNKNOWN",
        "product": payload.get("product") or "UNKNOWN",
        "supplier": payload.get("supplier") or "UNKNOWN",
        "agency": payload.get("agency") or "UNKNOWN",
        "contract_type": payload.get("contract_type") or "UNKNOWN",
        "delivery_performance": payload.get("delivery_performance") or "UNKNOWN",
        "acceptance_result": payload.get("acceptance_result") or "UNKNOWN",
        "problems": payload.get("problems") or ["UNKNOWN"],
        "resolutions": payload.get("resolutions") or ["UNKNOWN"],
        "lessons_learned": payload.get("lessons_learned") or ["UNKNOWN"],
        "recorded_at": _utc(),
        "fabricated_score": False,
    }
    idx = _load_index(PERF_INDEX_KEY)
    entries = list(idx.get("entries") or [])
    entries.append(entry)
    idx["entries"] = entries[-300:]
    if persist:
        _save_index(PERF_INDEX_KEY, idx)
    return entry


def build_performance_learning(*, opportunity_id: str | None = None, limit: int = 25) -> dict[str, Any]:
    idx = _load_index(PERF_INDEX_KEY)
    entries = [e for e in (idx.get("entries") or []) if isinstance(e, dict)]
    if opportunity_id:
        entries = [e for e in entries if e.get("opportunity_id") == opportunity_id]
    return {
        "kind": "M3PerformanceLearningLoop",
        "entries": entries[:limit],
        "feeds": ["Market Hunt", "Supplier Graph", "Discovery Planner"],
        "fake_scores_forbidden": True,
        "note": "Factual outcomes only — consumers must not invent rankings from empty lessons",
    }


# ---------------------------------------------------------------------------
# 10. Action ownership
# ---------------------------------------------------------------------------
def unknown_to_action(
    *,
    problem: str,
    owner: str = "UNKNOWN",
    action: str = "UNKNOWN",
    due_date: str = "UNKNOWN",
    evidence_required: str = "UNKNOWN",
    status: str = ST_UNKNOWN,
) -> dict[str, Any]:
    return {
        "problem": problem or "UNKNOWN",
        "owner": owner or "UNKNOWN",
        "action": action or "UNKNOWN",
        "due_date": due_date or "UNKNOWN",
        "evidence_required": evidence_required or "UNKNOWN",
        "status": status if status in {ST_UNKNOWN, ST_PLANNED, ST_IN_PROGRESS, ST_COMPLETE, ST_BLOCKED} else ST_UNKNOWN,
        "complete": status == ST_COMPLETE,
    }


def build_action_ownership(row: dict[str, Any], *, research_items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    actions = []
    for item in research_items or []:
        if not isinstance(item, dict):
            continue
        actions.append(
            unknown_to_action(
                problem=str(item.get("why_this_matters") or item.get("research_type") or "UNKNOWN"),
                owner=str(item.get("owner") or "RESEARCHER"),
                action=str(item.get("recommended_action") or "Research"),
                due_date=str(item.get("due_date") or row.get("deadline") or "UNKNOWN"),
                evidence_required=", ".join(item.get("missing_information") or []) or "UNKNOWN",
                status=ST_PLANNED if str(item.get("status") or "").upper() != "COMPLETE" else ST_COMPLETE,
            )
        )
    # Offer readiness soft/hard blockers as actions
    try:
        from m3_offer_readiness_read import build_offer_readiness_profile

        offer = build_offer_readiness_profile(row, research_items=research_items or [], include_category_scaffold=False)
        for b in (offer.get("blockers") or {}).get("top") or []:
            actions.append(
                unknown_to_action(
                    problem=str(b.get("requirement") or b.get("category") or "Blocker"),
                    owner="MANAGER",
                    action=str(b.get("action") or "Resolve blocker"),
                    due_date=str(row.get("deadline") or "UNKNOWN"),
                    evidence_required=str(b.get("evidence") or "UNKNOWN"),
                    status=ST_BLOCKED if b.get("blocker_type") == "HARD_BLOCKER" else ST_PLANNED,
                )
            )
    except Exception:
        pass

    # Dedupe by problem+action
    seen: set[str] = set()
    uniq = []
    for a in actions:
        key = f"{a['problem']}|{a['action']}"
        if key in seen:
            continue
        seen.add(key)
        uniq.append(a)

    return {
        "kind": "M3ActionOwnership",
        "opportunity_id": row.get("canonical_id"),
        "actions": uniq[:30],
        "pattern": "Problem → Owner → Action → Due date → Evidence required → Complete",
    }


# ---------------------------------------------------------------------------
# 11. Permissions / audit
# ---------------------------------------------------------------------------
def record_audit_change(
    *,
    field: str,
    old_value: Any,
    new_value: Any,
    reason: str,
    user: str,
    role: str,
    evidence: Any,
    opportunity_id: str = "UNKNOWN",
    persist: bool = True,
) -> dict[str, Any]:
    entry = {
        "opportunity_id": opportunity_id,
        "field": field,
        "old_value": old_value if old_value is not None else "UNKNOWN",
        "new_value": new_value if new_value is not None else "UNKNOWN",
        "reason": reason or "UNKNOWN",
        "user": user or "UNKNOWN",
        "role": role if role in {ROLE_OWNER, ROLE_MANAGER, ROLE_RESEARCHER, ROLE_VIEWER} else ROLE_VIEWER,
        "date": _utc(),
        "evidence": evidence if evidence not in (None, "") else "UNKNOWN",
    }
    idx = _load_index(AUDIT_INDEX_KEY)
    entries = list(idx.get("entries") or [])
    entries.append(entry)
    idx["entries"] = entries[-500:]
    if persist:
        _save_index(AUDIT_INDEX_KEY, idx)
    return entry


def build_permissions_audit(row: dict[str, Any] | None = None) -> dict[str, Any]:
    idx = _load_index(AUDIT_INDEX_KEY)
    cid = (row or {}).get("canonical_id")
    entries = [e for e in (idx.get("entries") or []) if isinstance(e, dict)]
    if cid:
        entries = [e for e in entries if e.get("opportunity_id") == cid]
    return {
        "kind": "M3PermissionsAudit",
        "roles": [ROLE_OWNER, ROLE_MANAGER, ROLE_RESEARCHER, ROLE_VIEWER],
        "critical_change_requires": ["old_value", "new_value", "reason", "user", "date", "evidence"],
        "entries": entries[-40:],
        "silent_change_forbidden": True,
    }


# ---------------------------------------------------------------------------
# 12. Submission control center
# ---------------------------------------------------------------------------
def build_submission_control(row: dict[str, Any]) -> dict[str, Any]:
    bc = _as_dict(row.get("bid_compliance"))
    sub = _as_dict(bc.get("submission_instructions") or row.get("bid_submission_intelligence"))
    snap = _as_dict(row.get("submission_snapshot"))
    docs = row.get("documents") if isinstance(row.get("documents"), list) else []
    amendments = [
        d
        for d in docs
        if isinstance(d, dict)
        and (
            str(d.get("document_type") or "").upper() == "AMENDMENT"
            or d.get("amendment_number")
        )
    ]

    checks = {
        "current_solicitation_version": fact(
            snap.get("solicitation_version") or row.get("solicitation_version") or "UNKNOWN"
        ),
        "amendments": fact(
            len(amendments) if amendments else snap.get("amendment_count") or "UNKNOWN",
            evidence=f"{len(amendments)} amendment doc(s)" if amendments else "UNKNOWN",
        ),
        "required_attachments": fact(sub.get("required_attachments") or snap.get("required_attachments")),
        "required_forms": fact(
            [f.get("form_name") for f in (_as_dict(bc.get("forms")).get("forms") or []) if isinstance(f, dict)]
            or snap.get("required_forms")
            or "UNKNOWN"
        ),
        "pricing_consistency": fact(snap.get("pricing_consistency")),
        "compliance_matrix": fact(
            "PRESENT" if _as_dict(bc.get("compliance_matrix")).get("rows") else "UNKNOWN",
            source="bid_compliance",
        ),
        "company_information": fact(snap.get("company_information")),
        "final_files": fact(snap.get("final_files")),
    }
    missing = [k for k, v in checks.items() if v.get("value") == "UNKNOWN"]
    snapshot = {
        "files_submitted": snap.get("files_submitted") or "UNKNOWN",
        "version": snap.get("version") or checks["current_solicitation_version"]["value"],
        "date": snap.get("date") or "UNKNOWN",
        "confirmation_evidence": snap.get("confirmation_evidence") or "UNKNOWN",
    }

    return {
        "kind": "M3SubmissionControlCenter",
        "opportunity_id": row.get("canonical_id"),
        "checks": checks,
        "missing": missing,
        "ready": len(missing) == 0,
        "submission_snapshot": snapshot,
        "question": "Is the submission package complete and version-correct?",
        "automates_submission": False,
    }


# ---------------------------------------------------------------------------
# 9 + 13. Operator command center / twice-daily follow-up
# ---------------------------------------------------------------------------
def build_operator_command_center(
    rows: list[dict[str, Any]] | None = None,
    *,
    store: Any | None = None,
    period: str = "morning",
) -> dict[str, Any]:
    """Daily operating view — morning (what changed / act) or evening (moved / blocked / next)."""
    if rows is None:
        if store is None:
            from m3_pipeline_store import M3PipelineStore

            store = M3PipelineStore()
        rows = list(store.all()) if hasattr(store, "all") else []

    period = "evening" if str(period).lower().startswith("eve") else "morning"
    active = []
    for r in rows:
        if not isinstance(r, dict) or not r.get("canonical_id"):
            continue
        lc = str(r.get("lifecycle") or "").upper()
        if lc in {"REJECTED", "REJECTED_CHEAP_SCREEN", "CANCELLED", "CLOSED", "ARCHIVED", "LOST"}:
            continue
        active.append(r)

    deadlines = []
    blockers = []
    actions = []
    changes = []
    completed = []
    expiring = []

    for r in active[:80]:
        cid = r.get("canonical_id")
        if r.get("deadline"):
            deadlines.append({"opportunity_id": cid, "deadline": r.get("deadline"), "title": r.get("title")})
        mem = build_contract_memory(r, limit=5)
        for e in mem.get("entries") or []:
            changes.append({**e, "title": r.get("title")})
        own = build_action_ownership(r)
        for a in own.get("actions") or []:
            item = {**a, "opportunity_id": cid, "title": (r.get("title") or "")[:80]}
            if a.get("status") == ST_COMPLETE:
                completed.append(item)
            elif a.get("status") == ST_BLOCKED:
                blockers.append(item)
            else:
                actions.append(item)
        # Quote expiration / evidence expiry signals
        commit = build_supplier_commitment(r)
        for s in commit.get("suppliers") or []:
            qe = _as_dict(s.get("quote_expiration"))
            if qe.get("value") not in (None, "", "UNKNOWN"):
                expiring.append(
                    {
                        "opportunity_id": cid,
                        "what": "quote_expiration",
                        "value": qe.get("value"),
                        "supplier": s.get("supplier_name"),
                    }
                )

    changes.sort(key=lambda e: str(e.get("change_date") or ""), reverse=True)

    if period == "morning":
        sections = {
            "new_changes": changes[:15],
            "deadlines": deadlines[:15],
            "amendments": [
                {"opportunity_id": r.get("canonical_id"), "title": r.get("title")}
                for r in active
                if any(
                    isinstance(d, dict)
                    and (
                        str(d.get("document_type") or "").upper() == "AMENDMENT"
                        or d.get("amendment_number")
                    )
                    for d in (r.get("documents") or [])
                )
            ][:10],
            "supplier_responses": [
                e
                for e in changes
                if "supplier" in str(e.get("field") or "").lower() or "quote" in str(e.get("field") or "").lower()
            ][:10],
            "blockers": blockers[:15],
            "required_actions": actions[:20],
        }
        questions = ["What changed?", "What requires action?"]
    else:
        sections = {
            "completed_actions": completed[:15],
            "unresolved_blockers": blockers[:15],
            "expiring_information": expiring[:15],
            "next_priorities": actions[:15],
            "what_moved": changes[:15],
            "deadlines": deadlines[:15],
        }
        questions = ["What moved?", "What is blocked?", "What is next?"]

    # BUILD 16 — expand morning/evening with supplier/capital/ops signals
    try:
        from m3_supplier_capital_ops_read import enrich_command_center_sections

        sections = enrich_command_center_sections(sections, active, period=period)
    except Exception:
        pass

    # BUILD 17 — economic blockers / missing evidence / learning signals
    try:
        from m3_economic_learning_read import enrich_command_center_economic

        sections = enrich_command_center_economic(sections, active, period=period)
    except Exception:
        pass

    # BUILD 18 — contract lifecycle actions / delivery / payment
    try:
        from m3_contract_lifecycle_read import enrich_command_center_contract

        sections = enrich_command_center_contract(sections, active, period=period)
    except Exception:
        pass

    # BUILD 19 — data governance / master record
    try:
        from m3_master_record_read import enrich_command_center_governance

        sections = enrich_command_center_governance(sections, active, period=period)
    except Exception:
        pass

    # BUILD 20 — intelligence retrieval navigator
    try:
        from m3_intelligence_retrieval_read import enrich_command_center_retrieval

        sections = enrich_command_center_retrieval(sections, active, period=period)
    except Exception:
        pass

    # BUILD 21 — action orchestration
    try:
        from m3_action_orchestration_read import enrich_command_center_actions

        sections = enrich_command_center_actions(sections, active, period=period)
    except Exception:
        pass

    # BUILD 22 — strategic intelligence
    try:
        from m3_strategic_intelligence_read import enrich_command_center_strategic

        sections = enrich_command_center_strategic(sections, active, period=period)
    except Exception:
        pass

    # BUILD 23 — human operating system
    try:
        from m3_human_os_read import enrich_command_center_human_os

        sections = enrich_command_center_human_os(sections, active, period=period)
    except Exception:
        pass

    # BUILD 24 — supply intelligence
    try:
        from m3_supply_intelligence_read import enrich_command_center_supply

        sections = enrich_command_center_supply(sections, active, period=period)
    except Exception:
        pass

    # BUILD 25 — research mission execution
    try:
        from m3_research_execution_read import enrich_command_center_research_execution

        sections = enrich_command_center_research_execution(sections, active, period=period)
    except Exception:
        pass

    # BUILD 26 — opportunity operating integration
    try:
        from m3_opportunity_operating_read import enrich_command_center_operating

        sections = enrich_command_center_operating(sections, active, period=period)
    except Exception:
        pass

    # BUILD 27 — pursuit readiness
    try:
        from m3_pursuit_readiness_read import enrich_command_center_pursuit_readiness

        sections = enrich_command_center_pursuit_readiness(sections, active, period=period)
    except Exception:
        pass

    # BUILD 28 — next-hour operator queue
    try:
        from m3_next_hour_queue_read import enrich_command_center_next_hour

        sections = enrich_command_center_next_hour(sections, active, period=period)
    except Exception:
        pass

    # BUILD 29 — operator loop
    try:
        from m3_operator_loop_read import enrich_command_center_operator_loop

        sections = enrich_command_center_operator_loop(sections, active, period=period)
    except Exception:
        pass

    return {
        "kind": "M3OperatorCommandCenter",
        "build": BUILD_TAG,
        "period": period,
        "questions": questions,
        "active_opportunities": len(active),
        "sections": sections,
        "goal": "A new employee can learn the process in 15–30 minutes",
        "meaningful_changes_only": True,
        "generated_at": _utc(),
        "read_only": True,
        "engines_unchanged": True,
    }


# ---------------------------------------------------------------------------
# Full execution OS profile (per opportunity)
# ---------------------------------------------------------------------------
def build_execution_os_profile(
    row: dict[str, Any] | None,
    *,
    research_items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    row = row if isinstance(row, dict) else {}
    profile = {
        "kind": "M3ExecutionOSProfile",
        "build": BUILD_TAG,
        "opportunity_id": row.get("canonical_id") or "UNKNOWN",
        "questions": [
            "Should we pursue this?",
            "Can we actually execute it?",
            "What can stop us?",
            "What evidence proves each decision?",
            "What action happens next?",
        ],
        "award_execution": build_award_execution_control(row),
        "supplier_commitment": build_supplier_commitment(row),
        "cash_survival": build_cash_survival_model(row),
        "ownership_risk": build_ownership_risk_timeline(row),
        "product_change_control": build_product_change_control(row),
        "payment_readiness": build_payment_readiness(row),
        "contract_memory": build_contract_memory(row),
        "performance_learning": build_performance_learning(opportunity_id=str(row.get("canonical_id") or "")),
        "action_ownership": build_action_ownership(row, research_items=research_items),
        "permissions_audit": build_permissions_audit(row),
        "submission_control": build_submission_control(row),
        "facts_only": True,
        "unknown_preserved": True,
        "no_fabricated_prices": True,
        "no_assumed_financing": True,
        "engines_unchanged": True,
        "scoring_unchanged": True,
        "generated_at": _utc(),
        "read_only": True,
    }
    # BUILD 16 — attach readiness gate without rebuilding engines
    try:
        from m3_supplier_capital_ops_read import build_execution_readiness_gate

        profile["execution_readiness_gate"] = build_execution_readiness_gate(row)
    except Exception:
        profile["execution_readiness_gate"] = {
            "kind": "M3ExecutionReadinessGate",
            "decision": "EXECUTION_UNKNOWN",
            "numeric_score": None,
        }
    return profile


def attach_execution_os_to_deal_room(deal: dict[str, Any], *, row: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(deal, dict):
        return deal
    out = dict(deal)
    try:
        out["execution_os"] = build_execution_os_profile(row or {"canonical_id": deal.get("canonical_id")})
    except Exception:
        out["execution_os"] = {
            "kind": "M3ExecutionOSProfile",
            "build": BUILD_TAG,
            "error": "execution_os_unavailable",
            "read_only": True,
        }
    return out
