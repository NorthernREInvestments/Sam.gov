"""Compare M3 actuals against curated expected truth."""

from __future__ import annotations

from typing import Any

from validation_harness.gap_classifier import classify_gap, likely_module_for
from validation_harness.models import empty_gap
from validation_harness.severity import consequence_for, severity_for


def _norm(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        if s.upper() in {"", "NONE", "NULL"}:
            return None
        return s
    if isinstance(v, (int, float, bool)):
        return v
    if isinstance(v, list):
        return [_norm(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _norm(val) for k, val in v.items()}
    return v


def _as_bool(v: Any) -> bool | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    s = str(v).strip().upper()
    if s in {"TRUE", "YES", "1", "Y"}:
        return True
    if s in {"FALSE", "NO", "0", "N"}:
        return False
    return None


def _contains_req(actual_reqs: list[dict[str, Any]], *, category: str | None = None, subtype: str | None = None, text_any: list[str] | None = None) -> bool:
    for r in actual_reqs or []:
        if category and str(r.get("category") or "").upper() != category.upper():
            continue
        if subtype and str(r.get("subtype") or "").upper() != subtype.upper():
            # also search normalized/raw
            blob = f"{r.get('normalized_requirement')} {r.get('raw_text')} {r.get('subtype')}".upper()
            if subtype.upper() not in blob:
                continue
        if text_any:
            blob = f"{r.get('normalized_requirement')} {r.get('raw_text')} {r.get('captured_value')}".upper()
            if not any(t.upper() in blob for t in text_any):
                continue
        return True
    return False


def extract_actuals(profile: dict[str, Any], workflow: dict[str, Any] | None = None, row: dict[str, Any] | None = None) -> dict[str, Any]:
    """Project M3 outputs into a comparable actuals dict. Does not invent facts."""
    profile = profile or {}
    workflow = workflow or {}
    row = row or {}
    reqs = list(profile.get("requirements") or [])
    gate = profile.get("owner_approval_gate") or {}
    invoice = profile.get("invoice_checklist") or {}
    cash = profile.get("cash_cycle") or {}
    post = profile.get("post_award_checklist") or {}
    submission = profile.get("submission_checklist") or {}
    supplier = profile.get("supplier_confirmation_checklist") or {}
    amd = profile.get("amendment") or {}

    def qty_vals() -> list[Any]:
        out = []
        for r in reqs:
            if r.get("category") != "QUANTITY_UOM":
                continue
            cv = r.get("captured_value")
            if isinstance(cv, dict) and cv.get("quantity") is not None:
                out.append(cv.get("quantity"))
            elif cv is not None and not isinstance(cv, dict):
                out.append(cv)
        return out

    supplier_state = gate.get("supplier_execution_state") or profile.get("supplier_execution_state") or {}
    sat = gate.get("satisfaction_raw") or gate.get("satisfaction") or {}

    return {
        "classification": {
            "product_classification": row.get("product_classification") or row.get("product_category") or "UNKNOWN",
        },
        "product_identity": {
            "part_number_detected": _contains_req(reqs, category="PRODUCT", subtype="PART")
            or _contains_req(reqs, category="PRODUCT", text_any=["PART"]),
            "nsn_detected": _contains_req(reqs, category="PRODUCT", subtype="NSN")
            or _contains_req(reqs, category="PRODUCT", text_any=["NSN", "NIIN"]),
            "brand_or_equal_detected": _contains_req(reqs, category="PRODUCT", subtype="BRAND_OR_EQUAL")
            or _contains_req(reqs, category="PRODUCT", text_any=["OR EQUAL"]),
            "brand_only_detected": _contains_req(reqs, category="PRODUCT", subtype="BRAND_ONLY")
            or _contains_req(reqs, category="PRODUCT", text_any=["BRAND NAME ONLY", "NO SUBSTITUT"]),
        },
        "quantity_uom": {
            "quantities_found": qty_vals(),
            "has_quantity": bool(qty_vals()) or _contains_req(reqs, category="QUANTITY_UOM"),
            "hd_hundred_detected": _contains_req(reqs, category="QUANTITY_UOM", subtype="HD_HUNDRED")
            or _contains_req(reqs, category="QUANTITY_UOM", text_any=[" HD", "HUNDRED"]),
            "total_pieces_noted": _contains_req(reqs, category="QUANTITY_UOM", subtype="TOTAL_PIECES")
            or _contains_req(reqs, category="QUANTITY_UOM", text_any=["TOTAL PIECES"]),
            "estimate_flagged_not_guaranteed": any(
                r.get("subtype") == "ESTIMATE"
                or "not a guaranteed" in str(r.get("notes") or "").lower()
                or "estimated" in str(r.get("normalized_requirement") or "").lower()
                for r in reqs
                if r.get("category") == "QUANTITY_UOM"
            ),
            "multi_clin_destinations": len(
                {r.get("captured_value") for r in reqs if r.get("subtype") == "CLIN_DESTINATION" and r.get("captured_value")}
            ),
        },
        "packaging": {
            "mil_std_2073_detected": _contains_req(reqs, category="PACKAGING", text_any=["2073", "MIL-STD-2073", "MIL STD 2073"]),
            "commercial_packaging_detected": _contains_req(reqs, category="PACKAGING", text_any=["COMMERCIAL", "ASTM"]),
            "specialist_packaging_flagged": _contains_req(reqs, category="PACKAGING", subtype="SPECIALIST")
            or _contains_req(reqs, category="PACKAGING", text_any=["PACKAGING HOUSE", "SPECIALIST"]),
        },
        "shipping_delivery": {
            "fob_destination": _contains_req(reqs, category="FOB", text_any=["DESTINATION"]),
            "fob_origin": _contains_req(reqs, category="FOB", text_any=["ORIGIN"]),
            "delivery_aro_detected": _contains_req(reqs, category="DELIVERY", subtype="ARO")
            or _contains_req(reqs, category="DELIVERY", text_any=["DAYS"]),
            "dodaac_detected": _contains_req(reqs, category="DELIVERY", subtype="DODAAC")
            or _contains_req(reqs, category="DELIVERY", text_any=["DODAAC"]),
        },
        "inspection_acceptance": {
            "source_inspection": _contains_req(reqs, category="INSPECTION", subtype="SOURCE")
            or _contains_req(reqs, category="INSPECTION", text_any=["SOURCE INSPECTION"]),
            "destination_acceptance": _contains_req(reqs, category="ACCEPTANCE", text_any=["DESTINATION"]),
            "certificate_of_conformance": _contains_req(reqs, category="ACCEPTANCE", subtype="COC")
            or _contains_req(reqs, category="ACCEPTANCE", text_any=["CONFORMANCE", "COC"]),
            "first_article": _contains_req(reqs, category="INSPECTION", text_any=["FIRST ARTICLE", "FAT"])
            or _contains_req(reqs, text_any=["FIRST ARTICLE", "FAT REQUIRED"]),
        },
        "submission": {
            "has_submission_requirements": bool(submission.get("items")),
            "amendment_ack_required": _contains_req(reqs, category="SUBMISSION", text_any=["AMENDMENT"]),
        },
        "invoice_payment": {
            "wawf_required": bool(invoice.get("wawf_required")),
            "payment_states_distinct": (
                list(post.get("payment_states_distinct") or [])
                if isinstance(post.get("payment_states_distinct"), list)
                else list((invoice.get("states") or {}).values())
                if isinstance(invoice.get("states"), dict)
                else []
            ),
        },
        "financing": {
            "financing_assumed": bool(cash.get("financing_assumed")),
            "cash_unknowns_preserved": sum(1 for v in cash.values() if v == "UNKNOWN") > 0,
            "financing_compatible": sat.get("financing_compatible") is True,
        },
        "economics": {
            "economics_complete": sat.get("economics_complete") is True,
        },
        "supplier": {
            "supplier_confirmation_incomplete": bool(supplier.get("blocks_owner_approval")),
            "supplier_checklist_present": bool(supplier.get("items")),
            "state": supplier_state.get("state"),
            "supplier_validated": supplier_state.get("supplier_validated") is True,
            "quote_executable": supplier_state.get("quote_executable") is True,
            "has_public_market_price": bool(supplier_state.get("has_public_market_price")),
        },
        "amendment": {
            "amendment_applied": bool(amd.get("applied")),
            "changed_categories": list(amd.get("changed_categories") or []),
        },
        "owner_readiness": {
            "ready_for_owner_approval": gate.get("ready_for_owner_approval") is True,
            "blockers": list(profile.get("execution_critical_blockers") or gate.get("blockers") or []),
            "provenance": gate.get("provenance") or {},
        },
        "operator_workflow": {
            "operator_workflow_state": workflow.get("operator_workflow_state") or row.get("operator_workflow_state"),
            "operator_blockers": list(workflow.get("operator_blockers") or []),
        },
        "provenance": {
            "critical_with_evidence": sum(
                1
                for r in reqs
                if r.get("mandatory") and r.get("evidence") and r.get("category") in {"PRODUCT", "PACKAGING", "QUANTITY_UOM", "FOB", "SUBMISSION"}
            ),
            "critical_without_evidence": sum(
                1
                for r in reqs
                if r.get("mandatory")
                and r.get("category") in {"PRODUCT", "PACKAGING", "QUANTITY_UOM", "FOB"}
                and not r.get("evidence")
                and r.get("raw_text")
                and r.get("confidence") == "EXTRACTED"
            ),
            "owner_gate_provenance": gate.get("provenance") or {},
        },
        "post_award": {
            "step_labels": [s.get("label") for s in (post.get("steps") or [])],
            "states_note_distinct": "collapse" in str(post.get("note") or "").lower() or "≠" in str(post.get("note") or ""),
        },
        "_meta": {
            "requirement_count": len(reqs),
            "requirement_categories": sorted({str(r.get("category")) for r in reqs}),
            "validation_depth": row.get("validation_depth"),
        },
    }


def compare_case(
    case: dict[str, Any],
    actuals: dict[str, Any],
    *,
    run_id: str | None = None,
) -> dict[str, Any]:
    """
    Compare expected vs actuals.
    UNKNOWN in expected means the field must remain unknown / not falsely asserted.
    Missing expected domains are skipped (not every case needs every field).
    """
    expected = case.get("expected") or {}
    allowed_unknowns = set(case.get("allowed_unknowns") or [])
    gaps: list[dict[str, Any]] = []
    matches: list[str] = []

    def add_gap(domain: str, field: str, exp: Any, act: Any, *, false_readiness: bool = False, false_rejection: bool = False):
        path = f"{domain}.{field}"
        if path in allowed_unknowns or field in allowed_unknowns:
            return
        cat = classify_gap(domain, path, false_readiness=false_readiness, false_rejection=false_rejection)
        sev = severity_for(path, cat, false_readiness=false_readiness)
        gaps.append(
            empty_gap(
                case_id=str(case.get("case_id")),
                category=cat,
                severity=sev,
                field_path=path,
                expected=exp,
                actual=act,
                likely_module=likely_module_for(cat),
                consequence=consequence_for(cat, path),
                run_id=run_id,
            )
        )

    def check(domain: str, field: str, exp: Any, act: Any, **kwargs: Any):
        if exp is None:
            return
        # Special token: expect UNKNOWN / not ready etc.
        if isinstance(exp, str) and exp.upper() == "UNKNOWN":
            if act not in (None, "UNKNOWN", [], {}) and act is not False:
                # actual claimed a concrete value when expected UNKNOWN
                add_gap(domain, field, "UNKNOWN", act, **kwargs)
            else:
                matches.append(f"{domain}.{field}")
            return
        ne, na = _norm(exp), _norm(act)
        if isinstance(exp, bool) or isinstance(act, bool):
            eb, ab = _as_bool(exp), _as_bool(act)
            if eb is not None and ab is not None:
                if eb != ab:
                    add_gap(domain, field, exp, act, **kwargs)
                else:
                    matches.append(f"{domain}.{field}")
                return
        if isinstance(exp, list) and not isinstance(act, list):
            # expect list of tokens present in actual list/string
            blob = json_like(act)
            missing = [x for x in exp if str(x).upper() not in blob.upper()]
            if missing:
                add_gap(domain, field, exp, act, **kwargs)
            else:
                matches.append(f"{domain}.{field}")
            return
        if ne != na:
            # soft numeric compare
            try:
                if float(ne) == float(na):  # type: ignore[arg-type]
                    matches.append(f"{domain}.{field}")
                    return
            except (TypeError, ValueError):
                pass
            add_gap(domain, field, exp, act, **kwargs)
        else:
            matches.append(f"{domain}.{field}")

    def json_like(v: Any) -> str:
        return str(v)

    # Domain walks
    for domain, fields in (expected or {}).items():
        if not isinstance(fields, dict):
            continue
        act_domain = actuals.get(domain) or {}
        for field, exp in fields.items():
            check(domain, field, exp, act_domain.get(field))

    # Top-level readiness expectations
    if case.get("expected_owner_readiness") is not None:
        act_ready = bool((actuals.get("owner_readiness") or {}).get("ready_for_owner_approval"))
        exp_ready = bool(case.get("expected_owner_readiness"))
        if exp_ready != act_ready:
            add_gap(
                "owner_readiness",
                "ready_for_owner_approval",
                exp_ready,
                act_ready,
                false_readiness=act_ready and not exp_ready,
                false_rejection=(not act_ready and exp_ready and case.get("adversarial") is False),
            )
        else:
            matches.append("owner_readiness.ready_for_owner_approval")

    if case.get("expected_blockers"):
        act_blockers = [str(b).upper() for b in ((actuals.get("owner_readiness") or {}).get("blockers") or [])]
        for b in case.get("expected_blockers") or []:
            bu = str(b).upper()
            # substring match — blocker taxonomies differ slightly
            if not any(bu in a or a in bu for a in act_blockers):
                # also check workflow blockers
                wf = [str(x).upper() for x in ((actuals.get("operator_workflow") or {}).get("operator_blockers") or [])]
                if not any(bu in a or a in bu for a in wf):
                    add_gap("owner_readiness", f"blocker:{b}", b, act_blockers)

    # Broader-pipeline: at least one of these blockers must appear
    any_blockers = (expected or {}).get("execution_blockers_include_any")
    if isinstance(any_blockers, list) and any_blockers:
        act_blockers = [str(b).upper() for b in ((actuals.get("owner_readiness") or {}).get("blockers") or [])]
        ok = any(
            any(str(want).upper() in a or a in str(want).upper() for a in act_blockers)
            for want in any_blockers
        )
        if not ok:
            add_gap(
                "owner_readiness",
                "execution_blockers_include_any",
                any_blockers,
                act_blockers,
                false_readiness=False,
            )
        else:
            matches.append("owner_readiness.execution_blockers_include_any")

    if case.get("expected_operator_state"):
        act_st = (actuals.get("operator_workflow") or {}).get("operator_workflow_state")
        check("operator_workflow", "operator_workflow_state", case.get("expected_operator_state"), act_st)

    # Outcome
    critical = [g for g in gaps if g.get("severity") == "CRITICAL"]
    high = [g for g in gaps if g.get("severity") == "HIGH"]
    if critical:
        status = "FAILED"
    elif high:
        status = "PARTIAL"
    elif gaps:
        status = "PARTIAL"
    else:
        status = "PASSED"

    return {
        "case_id": case.get("case_id"),
        "case_name": case.get("case_name"),
        "status": status,
        "matches": matches,
        "gaps": gaps,
        "match_count": len(matches),
        "gap_count": len(gaps),
        "critical_count": len(critical),
        "adversarial": bool(case.get("adversarial")),
        "tags": list(case.get("tags") or []),
    }
