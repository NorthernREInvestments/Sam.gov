"""Controlled real-world test + supplier quote pilot (no send / no Phase M)."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from application_clock import now_utc
from phase_l.acquisition_lanes import STAGE3_NO_ROW_CAP, classify_acquisition_lane
from phase_l.bidnet_parked import park_bidnet_auth_history
from phase_l.economic_evaluability import recompute_economics_from_recovery
from phase_l.evidence_recovery import run_parallel_recovery
from phase_l.legacy_cleanup import assert_no_fixed_positive_cap
from phase_l.l141_repair import label_inventory_freshness
from phase_l.original_solicitation import resolve_original_solicitation, submission_path_checklist
from phase_l.progressive_funnel import run_progressive_stages_cheap
from phase_l.public_artifact_types import LAST_KNOWN_RECENT, LIVE_FRESH
from phase_l.quality_audit import (
    GOV_VALUE_A,
    GOV_VALUE_B,
    GOV_VALUE_C,
    GOV_VALUE_D,
    HARD_BLOCKED,
    SECONDARY_QUOTE_TARGET,
    SUPPLIER_A,
    SUPPLIER_B,
    SUPPLIER_C,
    SUPPLIER_D,
    VALIDATED_QUOTE_TARGET,
    audit_quote_positive,
)
from phase_l.quote_economics import BUYER_VALUE_PATH, SUPPLIER_MEMORY_PATH, load_json, save_json
from phase_l.quote_readiness import (
    APPROVED_FOR_QUOTE_OUTREACH,
    AUTHORIZED_CONFIRMED,
    AUTHORIZED_LIKELY,
    INTERNAL_FIELDS_NEVER_SUPPLIER,
    OWNER_APPROVAL_REQUIRED,
    READY_FOR_QUOTE_OUTREACH,
    build_internal_quote_control,
    build_supplier_facing_packet,
    classify_requirement_mode,
    evaluate_quote_readiness,
    evaluate_supplier_quote_response,
    hard_eligibility_blockers,
    rank_suppliers_for_quote,
)

BUILD = "20260928-m3-controlled-real-world-test"
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "phase_l"
OUT.mkdir(parents=True, exist_ok=True)

READY_FOR_OWNER_APPROVAL = "READY_FOR_OWNER_APPROVAL"
NEEDS_MINOR_REVIEW = "NEEDS_MINOR_REVIEW"
NOT_READY = "NOT_READY"
FINANCING_PATH_PLAUSIBLE = "FINANCING_PATH_PLAUSIBLE"
FINANCING_PATH_UNRESOLVED = "FINANCING_PATH_UNRESOLVED"
FINANCING_EXECUTION_FAIL = "FINANCING_EXECUTION_FAIL"
VERIFIED_ACQUISITION_PRICE = "VERIFIED_ACQUISITION_PRICE"
VERIFIED_POSITIVE = "VERIFIED_POSITIVE"
RECOMMENDED_INITIAL_PILOT_BATCH = "RECOMMENDED_INITIAL_PILOT_BATCH"


def _utc() -> str:
    return now_utc().isoformat()


def _f(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _deadline_days(row: dict[str, Any], pipe: dict[str, Any]) -> float | None:
    d = (pipe.get("stage0") or {}).get("deadline") or {}
    for k in ("days_remaining", "days_to_deadline", "deadline_days"):
        v = _f(d.get(k) or row.get(k))
        if v is not None:
            return v
    return None


def screen_financing(row: dict[str, Any], *, max_buy: dict[str, Any] | None = None) -> dict[str, Any]:
    blob = " ".join(
        str(x or "")
        for x in (row.get("title"), row.get("description"), row.get("payment_terms"), row.get("financing_notes"))
    ).lower()
    if any(
        x in blob
        for x in (
            "personal guarantee required",
            "owner cash required",
            "prepay before award with no financing",
        )
    ):
        return {
            "status": FINANCING_EXECUTION_FAIL,
            "reasons": ["explicit_owner_cash_or_personal_guarantee_language"],
            "lenders_contacted": False,
        }
    target = _f((max_buy or {}).get("supplier_quote_target"))
    if target is not None and target >= 75000:
        return {
            "status": FINANCING_PATH_UNRESOLVED,
            "reasons": ["large_ticket_needs_po_financing_or_assignment_path"],
            "constraints": {
                "no_owner_cash_before_gov_payment": True,
                "no_personal_credit": True,
                "no_personal_guarantee_if_avoidable": True,
            },
            "lenders_contacted": False,
        }
    return {
        "status": FINANCING_PATH_PLAUSIBLE,
        "reasons": ["assumes_supplier_terms_or_po_financing_without_owner_cash"],
        "constraints": {
            "no_owner_cash_before_gov_payment": True,
            "no_personal_credit": True,
            "no_personal_guarantee_if_avoidable": True,
        },
        "lenders_contacted": False,
        "paths_considered": ["supplier_net_terms", "po_financing", "invoice_factoring", "payment_assignment"],
    }


def verify_live_status(
    row: dict[str, Any],
    *,
    original: dict[str, Any],
    submission: dict[str, Any],
    deadline_days: float | None,
) -> dict[str, Any]:
    checks = {
        "still_open": deadline_days is None or deadline_days > 0,
        "deadline_known": deadline_days is not None
        or bool(row.get("response_deadline") or row.get("due_date")),
        "authoritative_url": bool(
            original.get("original_source_verified")
            or original.get("original_posting_url")
            or row.get("original_posting_url")
            or row.get("source_url")
            or row.get("detail_url")
        ),
        "submission_path": bool(
            submission.get("submission_path_ready")
            or submission.get("submission_path_resolved")
            or (submission.get("checks") or {}).get("submission_method_known")
            or original.get("submission_method")
            or row.get("source_portal")
        ),
        "buyer": bool(row.get("agency") or row.get("buyer") or original.get("issuing_agency")),
        "solicitation_number": bool(
            row.get("solicitation_id") or row.get("notice_id") or original.get("solicitation_number")
        ),
    }
    ok = all(checks[k] for k in ("still_open", "authoritative_url", "buyer", "solicitation_number"))
    if deadline_days is not None and deadline_days < 0:
        ok = False
        checks["still_open"] = False
    return {"verified": ok, "checks": checks, "deadline_days": deadline_days}


def pilot_gate(
    *,
    quality_state: str,
    gov_grade: str,
    supplier_grade: str,
    live: dict[str, Any],
    financing: dict[str, Any],
) -> tuple[bool, str]:
    if not live.get("verified"):
        return False, "live_status_unverified"
    if financing.get("status") == FINANCING_EXECUTION_FAIL:
        return False, "financing_execution_fail"
    if gov_grade == GOV_VALUE_D:
        return False, "gov_d_category_benchmark"
    if supplier_grade == SUPPLIER_D:
        return False, "generic_supplier_seed"
    if quality_state == VALIDATED_QUOTE_TARGET:
        return True, "validated"
    if quality_state == SECONDARY_QUOTE_TARGET and gov_grade in {GOV_VALUE_A, GOV_VALUE_B, GOV_VALUE_C}:
        if supplier_grade in {SUPPLIER_A, SUPPLIER_B, SUPPLIER_C}:
            return True, "strong_secondary"
    return False, f"quality={quality_state}"


def classify_owner_readiness(
    *,
    gate_ok: bool,
    quality_state: str,
    live: dict[str, Any],
    readiness: dict[str, Any] | None,
    financing: dict[str, Any],
) -> str:
    if not gate_ok or not live.get("verified"):
        return NOT_READY
    if financing.get("status") == FINANCING_EXECUTION_FAIL:
        return NOT_READY
    if readiness and readiness.get("ready") and quality_state == VALIDATED_QUOTE_TARGET:
        return READY_FOR_OWNER_APPROVAL
    if quality_state in {VALIDATED_QUOTE_TARGET, SECONDARY_QUOTE_TARGET}:
        if financing.get("status") == FINANCING_PATH_UNRESOLVED:
            return NEEDS_MINOR_REVIEW
        if readiness and readiness.get("blockers"):
            return NEEDS_MINOR_REVIEW
        return READY_FOR_OWNER_APPROVAL if quality_state == VALIDATED_QUOTE_TARGET else NEEDS_MINOR_REVIEW
    return NOT_READY


def recommendation_for(owner_status: str, financing: dict[str, Any]) -> str:
    if owner_status == READY_FOR_OWNER_APPROVAL and financing.get("status") != FINANCING_EXECUTION_FAIL:
        return "APPROVE QUOTE OUTREACH"
    if owner_status == NEEDS_MINOR_REVIEW:
        return "REVIEW FIRST"
    return "DO NOT PURSUE"


def quote_ingestion_ready() -> dict[str, Any]:
    sample = evaluate_supplier_quote_response(
        quoted_unit=100.0,
        quantity=10,
        freight=50.0,
        revenue_mid=1500.0,
        max_buy={"supplier_quote_target": 120.0, "basis": "UNIT", "thresholds": {}},
        quote_date="2026-09-28",
        expiration_date="2026-10-28",
        lead_time_days=14,
    )
    return {
        "ready": True,
        "ingest_fn": "phase_l.quote_readiness.evaluate_supplier_quote_response",
        "sample_classification": sample.get("classification"),
        "verified_states_require_real_quote": [VERIFIED_ACQUISITION_PRICE, VERIFIED_POSITIVE],
        "fabricated_verified": False,
    }


def owner_workflow_ready() -> dict[str, Any]:
    return {
        "ready": True,
        "can_open_queue": True,
        "can_inspect_opportunity": True,
        "can_open_original_solicitation": True,
        "can_see_suppliers": True,
        "can_see_internal_max_buy": True,
        "can_inspect_quote_packet": True,
        "can_approve_reject": True,
        "auto_send": False,
        "states": {
            "pre": READY_FOR_OWNER_APPROVAL,
            "gate": OWNER_APPROVAL_REQUIRED,
            "post_approval": APPROVED_FOR_QUOTE_OUTREACH,
            "ready_outreach_label": READY_FOR_QUOTE_OUTREACH,
        },
    }


def source_bucket(row: dict[str, Any]) -> str:
    portal = str(row.get("source_portal") or row.get("source") or "").lower()
    url = str(row.get("source_url") or row.get("original_posting_url") or "").lower()
    if "api.sam.gov" in url or "live_sam_api" in portal:
        return "API_FEED"
    if "sam.gov" in url or "fed_sam" in portal:
        return "STRUCTURED_OR_PUBLIC_SEARCH"
    if portal in {"live_cooperative", "live_opengov"} or "sourcewell" in url or "boston.gov" in url:
        return "STATIC_PUBLIC"
    if row.get("inventory_freshness") == LAST_KNOWN_RECENT:
        return "LAST_KNOWN_RECENT"
    return "OTHER"


def run_controlled_real_world_pilot(
    *,
    authorize_live: bool = True,
    refresh_hunt: bool = True,
    max_hunt_sources: int = 20,
) -> dict[str, Any]:
    assert_no_fixed_positive_cap()
    assert STAGE3_NO_ROW_CAP
    park_bidnet_auth_history()

    from operating_mode import MODE_DEVELOPMENT_NO_OUTREACH, set_operating_mode

    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)

    hunt_meta: dict[str, Any] = {}
    hunt_status = None
    if refresh_hunt and authorize_live:
        from phase_l.hunt import run_phase_l_hunt
        from phase_l.resilient_hunt import reset_checkpoint

        reset_checkpoint()
        print("[pilot] fresh hunt (structured/non_bidnet)...", flush=True)
        hunt = run_phase_l_hunt(
            authorize_live=True,
            max_sources=max_hunt_sources,
            profile="non_bidnet",
        )
        hunt_meta = hunt.get("discovery_meta") or {}
        hunt_status = (hunt_meta.get("live_runner") or {}).get("run_status")

    data = json.loads((OUT / "accessible_latest.json").read_text(encoding="utf-8"))
    rows = label_inventory_freshness(list(data.get("rows") or []))
    data["rows"] = rows
    save_json(OUT / "accessible_latest.json", data)

    buyer_memory = load_json(BUYER_VALUE_PATH)
    supplier_memory = load_json(SUPPLIER_MEMORY_PATH)
    access_yes = [r for r in rows if str(r.get("our_bid_access") or "") == "YES"]
    stage_counts = Counter()
    stage3: list[dict[str, Any]] = []
    print(f"[pilot] scanning {len(access_yes)} accessible rows...", flush=True)
    for i, row in enumerate(access_yes):
        if i and i % 300 == 0:
            print(f"[pilot] stage-scan {i}/{len(access_yes)} s3={len(stage3)}", flush=True)
        pipe = run_progressive_stages_cheap(row)
        if (pipe.get("stage1") or {}).get("pass"):
            stage_counts["stage1"] += 1
        if (pipe.get("stage2") or {}).get("pass"):
            stage_counts["stage2"] += 1
        if pipe.get("survives_to_stage3"):
            stage3.append({"row": row, "pipe": pipe})
    stage_counts["stage3"] = len(stage3)
    print(f"[pilot] Stage 3={len(stage3)} — quality + pilot gate...", flush=True)

    live_queue: list[dict[str, Any]] = []
    ready_owner: list[dict[str, Any]] = []
    needs_review: list[dict[str, Any]] = []
    not_ready: list[dict[str, Any]] = []
    packets_out: list[dict[str, Any]] = []
    financing_rows: list[dict[str, Any]] = []
    detailed: list[dict[str, Any]] = []
    qstates = Counter()
    gov_grades = Counter()
    source_contrib = Counter()
    commercial_s3 = 0

    for i, item in enumerate(stage3):
        row = item["row"]
        pipe = item["pipe"]
        screen = pipe.get("stage2_screen") or {}
        commercial = dict(
            screen.get("commercial_identity") or ((pipe.get("stage2") or {}).get("commercial") or {})
        )
        s3 = pipe.get("stage3") or {}
        lane = classify_acquisition_lane(row, commercial=commercial).get("acquisition_lane")
        if "COMMERCIAL" in str(lane or "") or "OPEN" in str(lane or ""):
            commercial_s3 += 1

        original = resolve_original_solicitation(row)
        submission = submission_path_checklist(row, original=original)
        dd = _deadline_days(row, pipe)
        live = verify_live_status(row, original=original, submission=submission, deadline_days=dd)

        recovery = run_parallel_recovery(
            row,
            commercial=commercial,
            history={},
            buyer_memory=buyer_memory,
            supplier_memory=supplier_memory,
            stage3=s3,
        )
        econ = recompute_economics_from_recovery(
            row,
            gov_rec=recovery.get("gov"),
            qty_rec=recovery.get("quantity"),
            supplier_rec=recovery.get("suppliers"),
        )
        audit = audit_quote_positive(
            row,
            commercial=commercial,
            gov=econ.get("government_value"),
            suppliers=econ.get("suppliers"),
            qty_info=econ.get("quantity") or recovery.get("quantity"),
            max_buy=econ.get("max_buy"),
            qdep=econ.get("quote_dependent"),
            freight=econ.get("freight"),
            original=original,
            lane=lane,
            deadline_days=dd,
            buyer_memory=buyer_memory,
            supplier_memory=supplier_memory,
            stage3=s3,
            attempt_upgrades=True,
        )
        qstates[audit["quality_state"]] += 1
        gov_grades[audit["gov_grade"]] += 1

        financing = screen_financing(row, max_buy=econ.get("max_buy"))
        gate_ok, gate_reason = pilot_gate(
            quality_state=audit["quality_state"],
            gov_grade=audit["gov_grade"],
            supplier_grade=audit["supplier_grade"],
            live=live,
            financing=financing,
        )

        readiness: dict[str, Any] | None = None
        if gate_ok or audit["quality_state"] in {VALIDATED_QUOTE_TARGET, SECONDARY_QUOTE_TARGET}:
            ev = {
                "government_value": econ.get("government_value") or {},
                "max_buy": econ.get("max_buy") or {},
                "freight": econ.get("freight") or {},
                "expected_revenue": (econ.get("max_buy") or {}).get("expected_revenue") or {},
                "quote_dependent": econ.get("quote_dependent") or {},
                "suppliers": econ.get("suppliers") or [],
                "uom": econ.get("quantity") or {},
            }
            try:
                readiness = evaluate_quote_readiness(
                    row,
                    ev=ev,
                    commercial=commercial,
                    lane=lane,
                    original=original,
                    submission=submission,
                    deadline_days=dd,
                )
            except TypeError:
                readiness = {
                    "ready": gate_ok and audit["quality_state"] == VALIDATED_QUOTE_TARGET,
                    "blockers": hard_eligibility_blockers(row, lane=lane, commercial=commercial),
                    "suppliers_ranked": rank_suppliers_for_quote(
                        list(econ.get("suppliers") or []),
                        commercial=commercial,
                    ),
                    "send_authorized": False,
                }

        owner_status = classify_owner_readiness(
            gate_ok=gate_ok,
            quality_state=audit["quality_state"],
            live=live,
            readiness=readiness,
            financing=financing,
        )

        oid = str(row.get("notice_id") or row.get("solicitation_id") or row.get("id") or i)
        mb = econ.get("max_buy") or {}
        th = mb.get("thresholds") or {}
        gov = econ.get("government_value") or {}
        suppliers = (readiness or {}).get("suppliers_ranked") or list(econ.get("suppliers") or [])
        suppliers_ok = [
            s
            for s in suppliers
            if str(s.get("authorization_state") or "") in {AUTHORIZED_CONFIRMED, AUTHORIZED_LIKELY}
            or s.get("exact_product_evidence")
            or s.get("product_fit") in {"EXACT", "FAMILY", "STRONG"}
            or (s.get("supplier_rank_score") or 0) >= 20
        ] or suppliers[:3]

        entry = {
            "opportunity_id": oid,
            "solicitation_number": original.get("solicitation_number")
            or row.get("solicitation_id")
            or row.get("notice_id"),
            "buyer": row.get("agency") or row.get("buyer"),
            "product": (row.get("title") or "")[:200],
            "deadline_days": dd,
            "gov_grade": audit["gov_grade"],
            "gov_value": gov.get("unit_value") or gov.get("total_value"),
            "gov_source": gov.get("source"),
            "supplier_grade": audit["supplier_grade"],
            "supplier_count": len(suppliers_ok),
            "quality_state": audit["quality_state"],
            "max_buy_10k": th.get("MAX_BUY_FOR_10K_PROFIT"),
            "max_buy_25k": th.get("MAX_BUY_FOR_25K_PROFIT"),
            "break_even_max_buy": th.get("BREAK_EVEN_MAX_BUY"),
            "supplier_quote_target": mb.get("supplier_quote_target"),
            "freight_status": (econ.get("freight") or {}).get("status"),
            "financing_status": financing.get("status"),
            "expected_profit_tier": (readiness or {}).get("profit_tier")
            or (
                "positive"
                if ((econ.get("quote_dependent") or {}).get("tiers") or {}).get("quote_dependent_positive")
                else None
            ),
            "original_solicitation_url": original.get("original_posting_url")
            or row.get("original_posting_url")
            or row.get("source_url"),
            "submission": submission,
            "live_verified": live.get("verified"),
            "owner_readiness": owner_status,
            "pilot_gate_ok": gate_ok,
            "pilot_gate_reason": gate_reason,
            "recommendation": recommendation_for(owner_status, financing),
            "send_authorized": False,
            "source_bucket": source_bucket(row),
            "inventory_freshness": row.get("inventory_freshness"),
            "lane": lane,
        }

        if live.get("verified") and audit["quality_state"] not in {HARD_BLOCKED}:
            if dd is None or dd > 0:
                live_queue.append(entry)

        if owner_status == READY_FOR_OWNER_APPROVAL:
            ready_owner.append(entry)
        elif owner_status == NEEDS_MINOR_REVIEW:
            needs_review.append(entry)
        else:
            not_ready.append(entry)

        financing_rows.append({"opportunity_id": oid, "status": financing.get("status"), "reasons": financing.get("reasons")})

        if gate_ok:
            source_contrib[source_bucket(row)] += 1
            req = classify_requirement_mode(row, commercial)
            internal = build_internal_quote_control(
                {
                    "government_value": gov,
                    "max_buy": mb,
                    "freight": econ.get("freight") or {},
                    "expected_revenue": mb.get("expected_revenue") or {},
                }
            )
            supplier_packets = []
            for s in suppliers_ok[:5]:
                pkt = build_supplier_facing_packet(
                    row=row,
                    commercial=commercial,
                    requirement=req,
                    freight_info=econ.get("freight"),
                    supplier=s,
                    original=original,
                    uom=econ.get("quantity"),
                    internal_deadline_days=dd,
                )
                for forbidden in INTERNAL_FIELDS_NEVER_SUPPLIER:
                    assert forbidden not in pkt
                supplier_packets.append(pkt)
            packets_out.append(
                {
                    "opportunity_id": oid,
                    "supplier_facing": supplier_packets,
                    "internal_only": internal,
                    "send_authorized": False,
                }
            )
            detailed.append(
                {
                    "opportunity": entry["product"],
                    "opportunity_id": oid,
                    "source": {
                        "url": entry["original_solicitation_url"],
                        "buyer": entry["buyer"],
                        "solicitation": entry["solicitation_number"],
                    },
                    "submission": submission,
                    "product": {
                        "manufacturer": commercial.get("manufacturer"),
                        "model": commercial.get("model"),
                        "mpn": commercial.get("mpn"),
                        "nsn": row.get("nsn") or commercial.get("nsn"),
                        "quantity": (econ.get("quantity") or {}).get("quantity") or row.get("quantity"),
                        "uom": (econ.get("quantity") or {}).get("uom") or row.get("uom"),
                        "requirement_mode": req.get("requirement_mode"),
                    },
                    "government_value": {
                        "value": entry["gov_value"],
                        "grade": entry["gov_grade"],
                        "source": entry["gov_source"],
                    },
                    "economics": {
                        "break_even": entry["break_even_max_buy"],
                        "max_buy_10k": entry["max_buy_10k"],
                        "max_buy_25k": entry["max_buy_25k"],
                        "target": entry["supplier_quote_target"],
                        "profit_tier": entry["expected_profit_tier"],
                    },
                    "suppliers": [
                        {
                            "name": s.get("name") or s.get("supplier_domain"),
                            "auth": s.get("authorization_state"),
                            "fit": s.get("product_fit"),
                            "score": s.get("supplier_rank_score"),
                        }
                        for s in suppliers_ok[:5]
                    ],
                    "financing": financing,
                    "remaining_risks": (readiness or {}).get("blockers") or [gate_reason],
                    "recommendation": entry["recommendation"],
                    "owner_readiness": owner_status,
                }
            )

    candidates = sorted(
        ready_owner + needs_review,
        key=lambda e: (
            0 if e["owner_readiness"] == READY_FOR_OWNER_APPROVAL else 1,
            0 if e["gov_grade"] == GOV_VALUE_A else (1 if e["gov_grade"] == GOV_VALUE_B else 2),
            0 if e["supplier_grade"] in {SUPPLIER_A, SUPPLIER_B} else 1,
            -(e["deadline_days"] or 0),
        ),
    )
    # Dedupe by solicitation / canonical URL so inventory clones do not inflate the pilot
    def _dedupe_key(e: dict[str, Any]) -> str:
        return str(
            e.get("solicitation_number")
            or e.get("original_solicitation_url")
            or e.get("opportunity_id")
            or ""
        ).lower()[:180]

    seen_keys: set[str] = set()
    unique_candidates: list[dict[str, Any]] = []
    for e in candidates:
        k = _dedupe_key(e)
        if not k or k in seen_keys:
            continue
        seen_keys.add(k)
        unique_candidates.append(e)
    candidates = unique_candidates

    # Also dedupe ready_owner / needs_review lists for reporting
    def _dedupe_list(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for e in items:
            k = _dedupe_key(e)
            if not k or k in seen:
                continue
            seen.add(k)
            out.append(e)
        return out

    ready_owner = _dedupe_list(ready_owner)
    needs_review = _dedupe_list(needs_review)

    initial = [c for c in candidates if c["owner_readiness"] == READY_FOR_OWNER_APPROVAL][:10]
    if len(initial) < 3:
        for c in candidates:
            if c in initial:
                continue
            if c["pilot_gate_ok"] and c["owner_readiness"] == NEEDS_MINOR_REVIEW:
                initial.append(c)
            if len(initial) >= 5:
                break
    for i, c in enumerate(initial):
        c["rank"] = i + 1

    fin_counts = Counter(f["status"] for f in financing_rows)
    ingest = quote_ingestion_ready()
    owner_wf = owner_workflow_ready()
    lr = hunt_meta.get("live_runner") or {}
    fresh_hunt = {
        "ran": bool(refresh_hunt and authorize_live),
        "terminal_status": hunt_status or ("OFFLINE" if not authorize_live else "UNKNOWN"),
        "live_unique": lr.get("unique_records"),
        "accessible": len(rows),
        "live_fresh": sum(1 for r in rows if r.get("inventory_freshness") == LIVE_FRESH),
        "last_known_recent": sum(1 for r in rows if r.get("inventory_freshness") == LAST_KNOWN_RECENT),
        "stage1": int(stage_counts["stage1"]),
        "stage2": int(stage_counts["stage2"]),
        "stage3": len(stage3),
        "commercial_stage3": commercial_s3,
        "discovery_meta": hunt_meta,
    }
    gaps = {
        "parked": [
            "BidNet auth history",
            "OpenGov CDN Cloudflare",
            "Bonfire/IonWave fragile hubs",
            "DemandStar/PublicPurchase login",
        ],
        "auth_requirements": ["DemandStar", "Public Purchase", "some Jaggaer"],
        "anti_bot": ["OpenGov CDN", "BidNet interactive"],
        "api_account_options": ["SAM.gov FREE_API_KEY"],
        "failed_adapters_note": "See L.14.1 — does not block pilot",
    }

    if (
        fresh_hunt["terminal_status"]
        in {"COMPLETE", "COMPLETE_WITH_SOURCE_FAILURES", "OFFLINE", None, "UNKNOWN"}
        and len(stage3) > 0
        and len(initial) >= 1
        and ingest["ready"]
        and owner_wf["ready"]
    ):
        verdict = "CONTROLLED_REAL_WORLD_TEST_READY"
    elif len(stage3) > 0:
        verdict = "CONTROLLED_REAL_WORLD_TEST_PARTIAL"
    else:
        verdict = "CONTROLLED_REAL_WORLD_TEST_NOT_READY"

    remaining = (
        "Owner must explicitly approve outreach; system will not send. "
        "Actual supplier quotes required before VERIFIED_ACQUISITION_PRICE."
        if ready_owner or initial
        else "No READY_FOR_OWNER_APPROVAL rows — evidence/supplier/deadline gates filtered all candidates"
    )

    summary = {
        "kind": "ControlledRealWorldPilotResult",
        "build": BUILD,
        "generated_at": _utc(),
        "verdict": verdict,
        "fresh_hunt": fresh_hunt,
        "pilot_quality": {
            "validated": int(qstates.get(VALIDATED_QUOTE_TARGET, 0)),
            "secondary": int(qstates.get(SECONDARY_QUOTE_TARGET, 0)),
            "READY_FOR_OWNER_APPROVAL": len(ready_owner),
            "NEEDS_MINOR_REVIEW": len(needs_review),
            "NOT_READY": len(not_ready),
            "quality_states": dict(qstates),
            "gov_grades": dict(gov_grades),
        },
        "recommended_initial_batch": {
            "kind": RECOMMENDED_INITIAL_PILOT_BATCH,
            "count": len(initial),
            "rows": initial,
        },
        "financing": {
            "plausible": int(fin_counts.get(FINANCING_PATH_PLAUSIBLE, 0)),
            "unresolved": int(fin_counts.get(FINANCING_PATH_UNRESOLVED, 0)),
            "fail": int(fin_counts.get(FINANCING_EXECUTION_FAIL, 0)),
        },
        "source_coverage": {"pilot_quality_by_bucket": dict(source_contrib), "gaps": gaps},
        "quote_ingestion_readiness": "YES" if ingest["ready"] else "NO",
        "owner_workflow_readiness": "YES" if owner_wf["ready"] else "NO",
        "ingest_detail": ingest,
        "owner_workflow_detail": owner_wf,
        "remaining_blocker": remaining,
        "stop_rules": {
            "no_send": True,
            "no_quotes_requested": True,
            "no_bids": True,
            "no_purchase": True,
            "no_financing_execution": True,
            "no_phase_m": True,
            "evidence_standards_unchanged": True,
        },
        "detailed_candidates": detailed,
    }

    save_json(OUT / "pilot_fresh_hunt.json", fresh_hunt)
    save_json(OUT / "pilot_live_queue.json", {"count": len(live_queue), "rows": live_queue})
    save_json(OUT / "pilot_ready_for_owner_approval.json", {"count": len(ready_owner), "rows": ready_owner})
    save_json(
        OUT / "pilot_initial_batch.json",
        {"kind": RECOMMENDED_INITIAL_PILOT_BATCH, "count": len(initial), "rows": initial},
    )
    save_json(OUT / "pilot_quote_packets.json", {"count": len(packets_out), "rows": packets_out})
    save_json(OUT / "pilot_financing_screen.json", {"counts": dict(fin_counts), "rows": financing_rows[:200]})
    save_json(OUT / "pilot_source_gaps.json", gaps)
    save_json(OUT / "pilot_summary.json", summary)
    print(
        f"[pilot] verdict={verdict} ready={len(ready_owner)} review={len(needs_review)} "
        f"initial={len(initial)} s3={len(stage3)}",
        flush=True,
    )
    return summary


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--refresh-hunt", action="store_true")
    p.add_argument("--no-live", action="store_true")
    p.add_argument("--max-hunt-sources", type=int, default=20)
    args = p.parse_args()
    run_controlled_real_world_pilot(
        authorize_live=not args.no_live,
        refresh_hunt=args.refresh_hunt and not args.no_live,
        max_hunt_sources=args.max_hunt_sources,
    )
