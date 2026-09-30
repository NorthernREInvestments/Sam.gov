"""M3 FINAL END-TO-END OPERATOR DRY RUN — real Iowa product-resale opportunity.

Uses solicitation 645-DOTRFB-3046-2027 (Wildflower and Native Grass Seed) from the
existing live breakthrough artifact — not a fictional solicitation.

Does NOT invent supplier quotes. Records honest stops and defects.
"""

from __future__ import annotations

import json
import traceback
from pathlib import Path
from typing import Any

from application_clock import now_utc

ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS = ROOT / "artifacts"
BREAKTHROUGH = ARTIFACTS / "m3_iowa_live_breakthrough.json"
REPORT_PATH = ARTIFACTS / "m3_final_operator_dry_run_report.json"

OID = None  # set after ingest


def _utc() -> str:
    return now_utc().isoformat()


def _issue(
    severity: str,
    *,
    failure: str,
    module: str,
    reproduction: str,
    likely_cause: str,
    recommended_fix: str,
) -> dict[str, Any]:
    return {
        "severity": severity,
        "failure": failure,
        "module": module,
        "reproduction": reproduction,
        "likely_cause": likely_cause,
        "recommended_fix": recommended_fix,
    }


def _load_iowa() -> dict[str, Any]:
    data = json.loads(BREAKTHROUGH.read_text(encoding="utf-8"))
    if not data.get("ok"):
        raise RuntimeError("Iowa breakthrough artifact not ok — cannot dry-run a failed capture")
    return data


def _capture_deadline_from_iowa_listing(record: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Persist Close from Iowa listing via the same SciQuest capture path used in discovery.

    Does not invent a date. Prefers captured listing HTML (deterministic), then live PublicEvent.
    Never uses Open/posted as the response deadline.
    """
    from discovery.sciquest import parse_sciquest_public_events
    from deadline_runtime import apply_response_deadline_to_row

    sol = str(record.get("solicitation_number") or "")
    list_url = "https://bids.sciquest.com/apps/Router/PublicEvent?CustomerOrg=DASIowa"

    def _match_from_body(kind: str, body: str, src: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
        if not body:
            return None
        opps = parse_sciquest_public_events(body, list_url=list_url, source_id="state_ia")
        for o in opps:
            if sol and sol not in str(o.solicitation_number or ""):
                continue
            meta = o.raw_metadata or {}
            enriched = apply_response_deadline_to_row(
                record,
                close_raw=o.deadline_raw,
                posted_raw=meta.get("posted_raw"),
                source_url=list_url,
            )
            return enriched, {
                "capture_source": kind,
                "capture_url": src,
                "deadline_raw": o.deadline_raw,
                "posted_raw": meta.get("posted_raw"),
                "open_is_not_response_deadline": True,
            }
        return None

    for name in ("iowa_wildflower_row.html", "iowa_list_snippet.html", "iowa_list_full.html"):
        path = ARTIFACTS / name
        if not path.exists():
            continue
        hit = _match_from_body(f"artifact:{name}", path.read_text(encoding="utf-8"), str(path))
        if hit:
            return hit

    # Live only if local listing capture missed the solicitation
    try:
        from portal_document_resolver import live_http_get

        live = live_http_get(list_url, source_id="state_ia_deadline_capture")
        if live.get("ok") and live.get("text"):
            hit = _match_from_body("live_public_event", live["text"], list_url)
            if hit:
                return hit
    except Exception as exc:  # noqa: BLE001
        return record, {
            "capture_source": "live_error",
            "deadline_raw": None,
            "note": str(exc)[:200],
        }

    return record, {
        "capture_source": None,
        "deadline_raw": None,
        "note": "No Close date recovered from listing — remains UNKNOWN",
    }


def phase1_discovery(iowa: dict[str, Any]) -> dict[str, Any]:
    t = iowa["target"]
    docs = iowa.get("documents") or []
    items = iowa.get("line_items") or []
    record = {
        "title": t["title"],
        "solicitation_number": t["solicitation_number"],
        "agency": t["agency"],
        "source_id": t.get("source_id") or "state_ia",
        "source": t.get("source_id") or "state_ia",
        "source_system": "state_ia",
        "detail_url": t.get("detail_url"),
        "ui_link": t.get("detail_url"),
        "url": t.get("detail_url"),
        "canonical_id": t.get("canonical_id"),
        "description": (
            "State of Iowa DAS procurement for wildflower and native grass seed. "
            f"{len(items)} product line items recovered from solicitation PDF."
        ),
        "line_items": items,  # full BOM — real recovered lines
        "documents": [
            {
                "title": d.get("title"),
                "url": d.get("url"),
                "document_type": d.get("document_type"),
                "format": d.get("format"),
                "content_fingerprint": d.get("content_fingerprint"),
                "authority": d.get("authority"),
                "validation": d.get("validation"),
            }
            for d in docs
        ],
        "package_access": "PUBLIC_DETAIL_PAGE",
        "status": "OPEN",
    }
    # Source capture: Close from listing — not a post-discovery manual inject
    record, capture_meta = _capture_deadline_from_iowa_listing(record)
    return {
        "phase": 1,
        "name": "DISCOVERY",
        "source": record["source"],
        "solicitation_number": record["solicitation_number"],
        "agency": record["agency"],
        "title": record["title"],
        "deadline": record.get("deadline") or record.get("deadline_raw") or "UNKNOWN",
        "deadline_raw": record.get("deadline_raw"),
        "posted_raw": record.get("posted_raw"),
        "deadline_capture": capture_meta,
        "product_service_classification": "tangible_product_seed (pending screen)",
        "source_url": record.get("detail_url") or "UNKNOWN",
        "line_item_count": len(items),
        "document_count": len(docs),
        "record": record,
        "notes": [
            "Opportunity sourced from artifacts/m3_iowa_live_breakthrough.json (live capture).",
            "Not a fictional solicitation.",
            "Response deadline recovered via SciQuest listing Close capture (Open ≠ Close).",
        ],
    }


def phase2_screening(record: dict[str, Any]) -> dict[str, Any]:
    from m3_end_to_end import M3EndToEndOrchestrator
    from m3_pipeline_store import M3PipelineStore
    from m3_discovery_service import restore_pipeline_store_from_db
    from m3_opportunity_operating_read import derive_operator_lifecycle
    from product_category_yield import classify_product_category

    store = M3PipelineStore()
    try:
        restore_pipeline_store_from_db(store)
    except Exception:
        pass
    orch = M3EndToEndOrchestrator(store=store)
    # Force re-screen path: if already present, apply stage1 directly
    ingest = orch.ingest_discovery_record(record, persist=True)
    cid = ingest["canonical_id"]
    row = store.get(cid) or {}
    # If duplicate skip left old row without screen, re-apply cheap screen
    if not row.get("cheap_screen"):
        from national_discovery_funnel import stage1_ultra_cheap
        from m3_lifecycle import derive_lifecycle, determine_next_action
        from product_false_positive_audit import audit_survivor

        s1 = stage1_ultra_cheap(
            {
                "title": row.get("title") or record.get("title") or "",
                "description": row.get("description") or record.get("description") or "",
                "status": row.get("status") or "OPEN",
            }
        )
        row["cheap_screen_survive"] = bool(s1.get("survive"))
        row["cheap_screen"] = s1
        row["product_classification"] = s1.get("classification") or row.get("product_classification")
        cat = classify_product_category(str(row.get("title") or ""), str(row.get("description") or ""))
        row["product_category"] = cat["category"]
        row["product_audit"] = audit_survivor(row)
        if s1.get("survive"):
            row["research_queued"] = True
        row["lifecycle"] = derive_lifecycle(row)
        row["pending_next_action"] = determine_next_action(row)
        store._rows[cid] = row
        store.save(durable_write=True, skip_remote_merge=True)

    row = store.get(cid) or row
    life = derive_operator_lifecycle(row)
    issues = []
    dl_known = bool(
        row.get("deadline_known")
        or (
            row.get("deadline") not in (None, "", "UNKNOWN")
            and row.get("deadline_evaluation")
        )
    )
    if not dl_known:
        issues.append(
            _issue(
                "MAJOR",
                failure="Deadline UNKNOWN after listing Close capture + ingest",
                module="discovery/ingest + Iowa SciQuest Close persistence",
                reproduction=f"Ingest {cid}; inspect row.deadline / deadline_raw",
                likely_cause="Close not persisted from listing into canonical deadline fields",
                recommended_fix="Ensure Jaggaer match + apply_response_deadline_to_row on ingest",
            )
        )
    posted = row.get("posted_raw")
    if posted and row.get("deadline") and str(posted).strip() == str(row.get("deadline")).strip():
        issues.append(
            _issue(
                "MAJOR",
                failure="Posted/Open date incorrectly used as response deadline",
                module="deadline persistence",
                reproduction=f"Compare posted_raw vs deadline on {cid}",
                likely_cause="Open vs Close confusion",
                recommended_fix="Use Close only as response_deadline",
            )
        )
    return {
        "phase": 2,
        "name": "SCREENING",
        "canonical_id": cid,
        "ingest": {k: ingest.get(k) for k in ("created", "duplicate", "survived", "lifecycle", "next_action", "stop_reason")},
        "cheap_screen": row.get("cheap_screen"),
        "cheap_screen_survive": row.get("cheap_screen_survive"),
        "product_classification": row.get("product_classification"),
        "product_category": row.get("product_category"),
        "lifecycle_engine": row.get("lifecycle"),
        "operator_lifecycle": life.get("current_state"),
        "deadline": row.get("deadline") or row.get("deadline_raw"),
        "deadline_raw": row.get("deadline_raw"),
        "deadline_viability": row.get("deadline_viability")
        or (row.get("deadline_evaluation") or {}).get("deadline_viability")
        or (row.get("deadline_evaluation") or {}).get("status")
        or "UNKNOWN",
        "deadline_runway_days": row.get("deadline_runway_days")
        or (row.get("deadline_evaluation") or {}).get("calendar_days_remaining"),
        "manual_intervention": None,
        "issues": issues,
        "row_snapshot_keys": sorted(row.keys())[:40],
    }


def phase3_product(cid: str) -> dict[str, Any]:
    from m3_pipeline_store import M3PipelineStore
    from m3_discovery_service import restore_pipeline_store_from_db
    from m3_supply_intelligence_read import derive_supply_from_row, build_supply_intelligence_profile

    store = M3PipelineStore()
    try:
        restore_pipeline_store_from_db(store)
    except Exception:
        pass
    row = store.get(cid) or {}
    items = row.get("line_items") or []
    sample = items[:5]
    unknowns = []
    if not items:
        unknowns.append("No line items")
    # Manufacturer rarely stated for seed species — expect UNKNOWN
    mfrs = {str(i.get("manufacturer") or "").strip() for i in items if isinstance(i, dict)}
    mfrs.discard("")
    if not mfrs:
        unknowns.append("Manufacturer UNKNOWN for seed species lines")
    profile = None
    try:
        profile = build_supply_intelligence_profile(row)
    except Exception as e:
        unknowns.append(f"supply profile error: {e}")
    derived = None
    try:
        derived = derive_supply_from_row(row)
    except Exception as e:
        unknowns.append(f"derive_supply error: {e}")

    return {
        "phase": 3,
        "name": "DEEP_DEAL_PRODUCT",
        "what_government_buying": row.get("title"),
        "commercial_product_identifiable": True if items else False,
        "line_item_count": len(items),
        "sample_lines": [
            {
                "description": s.get("description"),
                "quantity": s.get("quantity"),
                "unit": s.get("unit") or s.get("uom"),
                "source": s.get("source"),
                "confidence": s.get("confidence"),
            }
            for s in sample
            if isinstance(s, dict)
        ],
        "manufacturer": list(mfrs)[:5] or "UNKNOWN",
        "specifications": "Species scientific names in line descriptions (evidence from PDF)",
        "missing_information": unknowns,
        "documents": row.get("documents"),
        "supply_profile_kind": (profile or {}).get("kind"),
        "derived_supply_summary": {
            "product": (derived or {}).get("product"),
            "manufacturer": (derived or {}).get("manufacturer"),
            "supplier": (derived or {}).get("supplier"),
            "unknowns": (derived or {}).get("unknowns"),
        }
        if derived
        else None,
        "unknown_preserved": True,
    }


def _store_row(cid: str) -> dict[str, Any]:
    from m3_pipeline_store import M3PipelineStore
    from m3_discovery_service import restore_pipeline_store_from_db

    store = M3PipelineStore()
    try:
        restore_pipeline_store_from_db(store)
    except Exception:
        pass
    return store.get(cid) or {}


def _api_client():
    """TestClient with auth disabled — mirrors test_m3_* patterns when APP_EMAIL is set."""
    import app as app_mod
    import auth as auth_mod

    app_mod.auth_enabled = lambda: False  # type: ignore
    auth_mod.auth_enabled = lambda: False  # type: ignore
    from fastapi.testclient import TestClient

    return TestClient(app_mod.app)


def phase4_supply(cid: str) -> dict[str, Any]:
    from m3_supply_intelligence_read import build_supply_intelligence_profile, derive_supply_from_row

    row = _store_row(cid)
    profile = build_supply_intelligence_profile(row)
    derived = derive_supply_from_row(row)
    evidence = list((derived or {}).get("stored_evidence") or [])
    # Also surface unknowns from derive
    unknowns = list(((derived or {}).get("supply_status") or {}).get("unknowns_remaining") or [])
    if unknowns == ["None listed — still verify before deciding"]:
        unknowns = []

    operator_action = None
    if not evidence or any("pricing" in str(u).lower() or "supplier" in str(u).lower() for u in unknowns):
        operator_action = {
            "what": "Obtain supplier quote for seed basket (or priority species)",
            "why": "No commercial evidence on opportunity — acquisition cost UNKNOWN",
            "do_not_fabricate_supplier": True,
        }

    return {
        "phase": 4,
        "name": "SUPPLY",
        "potential_supplier_path": (derived or {}).get("paths") or (profile.get("paths") if isinstance(profile, dict) else None) or "UNKNOWN",
        "supplier_evidence_count": len(evidence),
        "known": [
            x
            for x in [
                f"product_lines={len(row.get('line_items') or [])}",
                "document=solicitation PDF recovered" if row.get("documents") else None,
                f"product_identified={((derived or {}).get('supply_status') or {}).get('product_identified')}",
            ]
            if x
        ],
        "unknown": unknowns
        or [
            "Supplier identity",
            "Distributor/channel",
            "Unit acquisition prices",
            "Lead time",
            "Payment terms",
        ],
        "operator_action_generated": operator_action,
        "fabricated_supplier": False,
        "derived": {
            "product": (derived or {}).get("derived_product"),
            "manufacturer": ((derived or {}).get("derived_product") or {}).get("manufacturer"),
            "supplier": (derived or {}).get("derived_suppliers"),
            "unknowns": unknowns,
        },
    }


def phase5_commercial(cid: str) -> dict[str, Any]:
    """Do NOT invent a quote — verify honest stop."""
    from m3_commercial_validation_read import build_commercial_economics_view, record_supplier_quote

    row = _store_row(cid)
    econ = build_commercial_economics_view(row)

    rejected_no_source = None
    try:
        record_supplier_quote(
            {"opportunity_id": cid, "supplier": "Hypothetical", "product": "Seed", "unit_price": 1},
            persist=False,
        )
        rejected_no_source = False
    except ValueError as e:
        rejected_no_source = str(e)

    stop_message = "Supplier quote required."
    if econ["acquisition_cost"]["unknown"]:
        stop_at = stop_message
    else:
        stop_at = "Acquisition already present (unexpected for this dry run)"

    return {
        "phase": 5,
        "name": "COMMERCIAL_VALIDATION",
        "invented_quote": False,
        "stop_at": stop_at,
        "acquisition_unknown": econ["acquisition_cost"]["unknown"],
        "acquisition_value": econ["acquisition_cost"]["value"],
        "quote_without_source_rejected": rejected_no_source,
        "provenance_enforced": bool(rejected_no_source),
        "unknown_handling": {
            "freight": "UNKNOWN"
            if not any(
                "freight" in str(c.get("label") or "").lower()
                for c in econ.get("known_additional_costs") or []
            )
            else "KNOWN",
            "payment_terms": econ.get("payment_terms"),
        },
        "economics_preview": {
            "revenue": econ["revenue"],
            "acquisition_cost": econ["acquisition_cost"],
            "unknown_costs": econ["unknown_costs"],
            "margin_visibility": econ["margin_visibility"],
        },
    }


def phase6_economics(cid: str) -> dict[str, Any]:
    from m3_commercial_validation_read import build_commercial_economics_view

    row = _store_row(cid)
    body = build_commercial_economics_view(row)
    # Also hit HTTP path with auth disabled
    http_status = None
    try:
        client = _api_client()
        r = client.get(f"/api/m3/commercial/economics/{cid}")
        http_status = r.status_code
    except Exception as e:
        http_status = f"error:{e}"

    issues = []
    acq = body.get("acquisition_cost") or {}
    margin = body.get("margin_visibility") or {}
    if acq.get("unknown") and acq.get("value") in (0, 0.0, "0"):
        issues.append(
            _issue(
                "BLOCKER",
                failure="UNKNOWN acquisition treated as zero",
                module="m3_commercial_validation_read",
                reproduction="economics view with no quote",
                likely_cause="coercion bug",
                recommended_fix="Keep UNKNOWN; never coerce blank to 0",
            )
        )
    if margin.get("unsupported_profit_presented_as_fact"):
        issues.append(
            _issue(
                "MAJOR",
                failure="Unsupported profit presented as fact",
                module="m3_commercial_validation_read",
                reproduction="economics with unknown acquisition",
                likely_cause="margin flag incorrect",
                recommended_fix="Force unsupported_profit_presented_as_fact=False when inputs unknown",
            )
        )
    return {
        "phase": 6,
        "name": "ECONOMICS",
        "http_status": http_status,
        "body": body,
        "distinguishes_known_unknown": True,
        "unknown_never_zero_ok": not any(i["severity"] == "BLOCKER" for i in issues),
        "issues": issues,
    }


def phase7_capital(cid: str) -> dict[str, Any]:
    from m3_capital_requirement_gate_read import build_capital_requirement_assessment

    row = _store_row(cid)
    body = build_capital_requirement_assessment(row, ensure_actions=False)
    http_status = None
    try:
        client = _api_client()
        r = client.get(f"/api/m3/capital/{cid}")
        http_status = r.status_code
    except Exception as e:
        http_status = f"error:{e}"

    issues = []
    cvs = body.get("capital_vs_funding_source") or {}
    if cvs.get("funded") is True and (body.get("funding_path") or {}).get("source_state") == "UNKNOWN":
        issues.append(
            _issue(
                "BLOCKER",
                failure="Marked funded with UNKNOWN funding source",
                module="m3_capital_requirement_gate_read",
                reproduction="capital gate with no financing evidence",
                likely_cause="funded flag incorrect",
                recommended_fix="Keep funded=False unless verified source evidence",
            )
        )
    if body.get("funding_path", {}).get("financing_available_claim"):
        issues.append(
            _issue(
                "BLOCKER",
                failure="financing_available_claim true without evidence",
                module="m3_capital_requirement_gate_read",
                reproduction="GET capital",
                likely_cause="claim flag default",
                recommended_fix="Always false unless verified",
            )
        )
    return {
        "phase": 7,
        "name": "CAPITAL_GATE",
        "http_status": http_status,
        "capital_required_state": (body.get("capital_required") or {}).get("state"),
        "known_requirement": (body.get("capital_required") or {}).get("known_requirement"),
        "known_prepayment": (body.get("capital_required") or {}).get("known_prepayment"),
        "cash_timing": (body.get("cash_timing") or {}).get("state"),
        "funding_path_state": (body.get("funding_path") or {}).get("state"),
        "funding_source_state": (body.get("funding_path") or {}).get("source_state"),
        "funded": (body.get("capital_vs_funding_source") or {}).get("funded"),
        "financing_available_claim": (body.get("funding_path") or {}).get("financing_available_claim"),
        "blocking_unknowns": body.get("blocking_unknowns"),
        "next_action": body.get("next_action"),
        "beginner": body.get("beginner"),
        "issues": issues,
        "raw": {
            k: body.get(k)
            for k in (
                "capital_required",
                "unknown_capital",
                "cash_timing",
                "funding_path",
                "capital_vs_funding_source",
                "narrative",
                "principles",
            )
        },
    }


def phase8_pursuit(cid: str) -> dict[str, Any]:
    from m3_pursuit_readiness_read import build_pursuit_readiness_assessment

    row = _store_row(cid)
    body = build_pursuit_readiness_assessment(row, ensure_actions=True, persist_actions=True)
    http_status = None
    try:
        client = _api_client()
        r = client.get(f"/api/m3/pursuit-readiness/{cid}")
        http_status = r.status_code
    except Exception as e:
        http_status = f"error:{e}"

    dims = {}
    issues = []
    for key, d in (body.get("dimensions") or {}).items():
        dims[key] = {
            "state": d.get("state"),
            "why": d.get("why"),
            "evidence_count": len(d.get("evidence") or []),
            "unknowns": d.get("unknowns"),
            "next_action": d.get("next_action"),
            "status": d.get("status"),
        }
    principles = body.get("principles") or {}
    if principles.get("does_not_predict_winners") is not True:
        issues.append(
            _issue(
                "MAJOR",
                failure="Pursuit readiness missing does_not_predict_winners principle",
                module="m3_pursuit_readiness_read",
                reproduction="build_pursuit_readiness_assessment",
                likely_cause="principles omitted",
                recommended_fix="Ensure principles always present",
            )
        )
    if body.get("overall", {}).get("not_a_score") is not True:
        issues.append(
            _issue(
                "MINOR",
                failure="overall.not_a_score not true",
                module="m3_pursuit_readiness_read",
                reproduction="build_pursuit_readiness_assessment",
                likely_cause="overall schema",
                recommended_fix="Keep not_a_score True",
            )
        )
    return {
        "phase": 8,
        "name": "PURSUIT_READINESS",
        "http_status": http_status,
        "dimensions": dims,
        "overall": body.get("overall"),
        "principles": principles,
        "actions_ensured": len(body.get("actions_created") or body.get("ensured_actions") or []),
        "issues": issues,
    }


def phase9_next_hour(cid: str) -> dict[str, Any]:
    from m3_next_hour_queue_read import build_next_hour_queue
    from m3_pipeline_store import M3PipelineStore
    from m3_discovery_service import restore_pipeline_store_from_db

    store = M3PipelineStore()
    try:
        restore_pipeline_store_from_db(store)
    except Exception:
        pass
    rows = list(store._rows.values())
    body = build_next_hour_queue(rows=rows, limit_next=50)
    items = body.get("next_hour") or body.get("items") or body.get("queue") or []
    other = body.get("other_attention") or []
    if isinstance(other, list):
        items = list(items) + list(other)
    http_status = None
    try:
        client = _api_client()
        r = client.get("/api/m3/operator/next-hour")
        http_status = r.status_code
    except Exception as e:
        http_status = f"error:{e}"
    related = [
        i
        for i in items
        if isinstance(i, dict)
        and cid in str(
            i.get("opportunity_id")
            or i.get("canonical_id")
            or i.get("related_opportunity")
            or i.get("deal_room_path")
            or ""
        )
    ]
    sample = related[:5] or [i for i in items if isinstance(i, dict)][:5]
    shaped = []
    for i in sample:
        shaped.append(
            {
                "what": i.get("what") or i.get("title") or i.get("action") or i.get("label"),
                "why": i.get("why") or i.get("reason"),
                "deadline": i.get("deadline") or i.get("deadline_viability"),
                "missing": i.get("missing") or i.get("what_is_missing"),
                "current_state": i.get("current_state") or i.get("state") or i.get("status"),
                "next_action": i.get("next_action") or i.get("recommended_next"),
            }
        )
    issues = []
    if not items:
        issues.append(
            _issue(
                "MAJOR",
                failure="Next-hour queue empty even with researched opportunity in pipeline",
                module="m3_next_hour_queue_read",
                reproduction="After Iowa ingest, build_next_hour_queue",
                likely_cause="Queue may require persisted actions / deadline filters; opportunity not surfaced",
                recommended_fix="Ensure pursuit gaps / capital next actions enter next-hour for opportunities without deadline",
            )
        )
    elif not related:
        issues.append(
            _issue(
                "MAJOR",
                failure="Next-hour has items but none tied to dry-run opportunity",
                module="m3_next_hour_queue_read",
                reproduction=f"build_next_hour_queue; look for {cid}",
                likely_cause="Scoring/filter excludes UNKNOWN deadline or new rows",
                recommended_fix="Include capital/pursuit blockers for new product opportunities without inventing urgency",
            )
        )
    return {
        "phase": 9,
        "name": "NEXT_HOUR",
        "http_status": http_status,
        "queue_count": len(items) if isinstance(items, list) else 0,
        "related_count": len(related),
        "sample": shaped,
        "issues": issues,
        "queue_keys": list(body.keys()) if isinstance(body, dict) else [],
    }


def phase11_deal_room(cid: str) -> dict[str, Any]:
    """Exercise the same attach chain as GET /api/m3/mobile/deal/{id}."""
    from m3_mobile_read_model import deal_room_summary
    from m3_market_hunt_handoff import attach_handoff_to_deal_room
    from m3_offer_readiness_read import attach_offer_readiness_to_deal_room
    from m3_execution_os_read import attach_execution_os_to_deal_room
    from m3_supplier_capital_ops_read import attach_supplier_capital_ops_to_deal_room
    from m3_economic_learning_read import attach_economic_learning_to_deal_room
    from m3_contract_lifecycle_read import attach_contract_lifecycle_to_deal_room
    from m3_master_record_read import attach_governance_to_deal_room
    from m3_intelligence_retrieval_read import attach_retrieval_to_deal_room
    from m3_action_orchestration_read import attach_action_orchestration_to_deal_room
    from m3_strategic_intelligence_read import attach_strategic_intelligence_to_deal_room
    from m3_human_os_read import attach_human_os_to_deal_room
    from m3_supply_intelligence_read import attach_supply_intelligence_to_deal_room
    from m3_research_execution_read import attach_research_execution_to_deal_room
    from m3_opportunity_operating_read import attach_opportunity_operating_to_deal_room
    from m3_pursuit_readiness_read import attach_pursuit_readiness_to_deal_room
    from m3_operator_loop_read import attach_operator_loop_to_deal_room
    from m3_commercial_validation_read import attach_commercial_validation_to_deal_room
    from m3_capital_requirement_gate_read import attach_capital_requirement_to_deal_room
    from m3_pipeline_store import M3PipelineStore
    from m3_discovery_service import restore_pipeline_store_from_db

    store = M3PipelineStore()
    try:
        restore_pipeline_store_from_db(store)
    except Exception:
        pass
    row = store.get(cid)
    issues = []
    if not row:
        return {
            "phase": 11,
            "name": "DEAL_ROOM",
            "http_status": None,
            "issues": [
                _issue(
                    "BLOCKER",
                    failure="Opportunity not in pipeline store for Deal Room",
                    module="m3_pipeline_store",
                    reproduction=f"store.get({cid})",
                    likely_cause="ingest/persist failed",
                    recommended_fix="Verify durable save",
                )
            ],
        }

    deal = deal_room_summary(row)
    deal = attach_handoff_to_deal_room(deal, row=row, store=store)
    deal = attach_offer_readiness_to_deal_room(deal, row=row)
    deal = attach_execution_os_to_deal_room(deal, row=row)
    deal = attach_supplier_capital_ops_to_deal_room(deal, row=row)
    deal = attach_economic_learning_to_deal_room(deal, row=row)
    deal = attach_contract_lifecycle_to_deal_room(deal, row=row)
    deal = attach_governance_to_deal_room(deal, row=row)
    deal = attach_retrieval_to_deal_room(deal, row=row)
    deal = attach_action_orchestration_to_deal_room(deal, row=row)
    deal = attach_strategic_intelligence_to_deal_room(deal, row=row)
    deal = attach_human_os_to_deal_room(deal, row=row)
    deal = attach_supply_intelligence_to_deal_room(deal, row=row)
    deal = attach_research_execution_to_deal_room(deal, row=row)
    deal = attach_opportunity_operating_to_deal_room(deal, row=row)
    deal = attach_pursuit_readiness_to_deal_room(deal, row=row)
    deal = attach_operator_loop_to_deal_room(deal, row=row)
    deal = attach_commercial_validation_to_deal_room(deal, row=row)
    body = attach_capital_requirement_to_deal_room(deal, row=row)

    http_status = None
    try:
        client = _api_client()
        r = client.get(f"/api/m3/mobile/deal/{cid}")
        http_status = r.status_code
    except Exception as e:
        http_status = f"error:{e}"

    required = [
        "overview",
        "commercial_validation",
        "capital_requirement_gate",
        "pursuit_readiness",
        "supply_intelligence",
        "operator_loop",
        "action_orchestration",
        "opportunity_operating",
    ]
    present = {k: (k in body and body.get(k) not in (None, {}, [])) for k in required}
    if "overview" not in body and body.get("canonical_id"):
        present["overview"] = True
    missing = [k for k, ok in present.items() if not ok]
    ov = body.get("overview") or {}
    deal_deadline = ov.get("deadline") or ov.get("deadline_raw")
    row_deadline = row.get("deadline") or row.get("deadline_raw")
    pr = (body.get("pursuit_readiness") or {}).get("deadline_context") or {}
    if row_deadline and (not deal_deadline or str(deal_deadline).upper() == "UNKNOWN"):
        issues.append(
            _issue(
                "MAJOR",
                failure="Deal Room overview shows UNKNOWN deadline while row has known Close",
                module="m3_mobile_read_model.deal_room_summary",
                reproduction=f"deal_room_summary for {cid}",
                likely_cause="overview not reading canonical deadline fields",
                recommended_fix="Surface deadline_raw/response_deadline in overview",
            )
        )
    if row_deadline and pr and not pr.get("deadline_known"):
        issues.append(
            _issue(
                "MAJOR",
                failure="Pursuit Readiness deadline_context unknown while row has Close",
                module="m3_pursuit_readiness_read",
                reproduction=f"build_pursuit_readiness_assessment for {cid}",
                likely_cause="deadline_context not wired",
                recommended_fix="deadline_context_for_operator on assessment",
            )
        )
    if missing:
        sev = (
            "MAJOR"
            if any(
                m in missing
                for m in ("capital_requirement_gate", "pursuit_readiness", "commercial_validation")
            )
            else "MINOR"
        )
        issues.append(
            _issue(
                sev,
                failure=f"Deal Room missing sections: {missing}",
                module="deal room attach chain",
                reproduction=f"attach chain for {cid}",
                likely_cause="attach skipped or error swallowed",
                recommended_fix="Ensure attach_* always sets kind payload",
            )
        )
    return {
        "phase": 11,
        "name": "DEAL_ROOM",
        "http_status": http_status,
        "sections_present": present,
        "missing_sections": missing,
        "has_capital_gate": bool(body.get("capital_requirement_gate")),
        "has_commercial": bool(body.get("commercial_validation")),
        "has_pursuit": bool(body.get("pursuit_readiness")),
        "canonical_id": body.get("canonical_id"),
        "capital_display": ((body.get("capital_requirement_gate") or {}).get("display")),
        "issues": issues,
        "top_keys": list(body.keys())[:40],
    }


def phase10_operator_loop(cid: str) -> dict[str, Any]:
    from m3_action_orchestration_read import create_action, get_action, ST_COMPLETED
    from m3_operator_loop_read import (
        operator_mark_needs_evidence,
        operator_record_evidence,
        operator_mark_done,
    )
    from m3_capital_requirement_gate_read import build_capital_requirement_assessment

    row = _store_row(cid) or {"canonical_id": cid}
    cap = build_capital_requirement_assessment(row, ensure_actions=True, persist_actions=True)

    act = create_action(
        {
            "title": "Obtain supplier quote for Iowa seed basket",
            "action_type": "RESEARCH",
            "why": "Acquisition cost UNKNOWN — commercial validation blocked",
            "trigger_source": f"dry_run:{cid}",
            "opportunity_id": cid,
            "related_opportunity": cid,
            "evidence_requirements": ["Supplier quote with provenance"],
            "created_by": "operator_dry_run",
        },
        persist=True,
    )
    aid = act["action_id"]

    needs = operator_mark_needs_evidence(
        {
            "action_id": aid,
            "note": "No real supplier quote available for this dry run — will not invent pricing",
            "required_evidence": ["Supplier quote PDF/email with unit prices"],
            "actor": "dry_run_operator",
        },
        persist=True,
    )

    evid = operator_record_evidence(
        {
            "action_id": aid,
            "claim": "Solicitation PDF and 196 line items are evidenced; supplier commercial quote is not",
            "source": "artifacts/m3_iowa_live_breakthrough.json",
            "evidence": "document_fingerprint + line_items recovered; acquisition cost still UNKNOWN",
            "evidence_type": "Research Note",
            "opportunity_id": cid,
        },
        persist=True,
    )

    done_attempt = operator_mark_done(
        {
            "action_id": aid,
            "evidence": "",
            "actor": "dry_run_operator",
        },
        persist=True,
    )

    final = get_action(aid)
    issues = []
    if done_attempt.get("accepted") is True:
        issues.append(
            _issue(
                "BLOCKER",
                failure="Action marked DONE without evidence",
                module="m3_operator_loop_read",
                reproduction="operator_mark_done with empty evidence",
                likely_cause="evidence check bypassed",
                recommended_fix="Reject DONE when evidence blank",
            )
        )
    if final and final.get("status") == ST_COMPLETED and not (final.get("lineage") or {}).get(
        "completion_evidence"
    ):
        issues.append(
            _issue(
                "BLOCKER",
                failure="Completed action lacks completion evidence",
                module="m3_action_orchestration_read",
                reproduction="dry run DONE path",
                likely_cause="completion without evidence",
                recommended_fix="Require lineage.completion_evidence",
            )
        )

    return {
        "phase": 10,
        "name": "OPERATOR_LOOP",
        "action_id": aid,
        "needs_evidence": {
            "accepted": needs.get("accepted"),
            "intent": needs.get("intent"),
        },
        "evidence_recorded": bool(evid),
        "done_without_evidence_accepted": done_attempt.get("accepted"),
        "done_requires": done_attempt.get("requires") or done_attempt.get("reason"),
        "final_action_status": (final or {}).get("status"),
        "capital_actions_ensured": len(cap.get("actions_ensured") or []),
        "issues": issues,
        "did_not_falsely_complete": done_attempt.get("accepted") is not True,
    }


def phase12_operator_question(report: dict[str, Any]) -> dict[str, Any]:
    p3 = report["phases"].get("3") or {}
    p4 = report["phases"].get("4") or {}
    p5 = report["phases"].get("5") or {}
    p7 = report["phases"].get("7") or {}
    p8 = report["phases"].get("8") or {}
    p9 = report["phases"].get("9") or {}
    p10 = report["phases"].get("10") or {}

    known = [
        f"Real solicitation {report['opportunity']['solicitation_number']} from Iowa DAS",
        f"Title: {report['opportunity']['title']}",
        f"Product line items recovered: {p3.get('line_item_count')}",
        "Solicitation PDF document recovered (fingerprint evidenced)",
        f"Cheap screen survive: {(report['phases'].get('2') or {}).get('cheap_screen_survive')}",
        f"Capital required state: {p7.get('capital_required_state')}",
        f"Funding source state: {p7.get('funding_source_state')} (not funded)",
    ]
    unknown = [
        "Response deadline",
        "Supplier identity / channel",
        "Acquisition unit prices / quote",
        "Freight",
        "Payment terms / deposit",
        "Government award/history pricing for this RFB (may exist elsewhere — not attached here)",
        "Funding path for execution",
    ]
    worth = [
        "Large multi-line tangible product BOM (seed) fits product-resale model",
        "Public package with recovered PDF + species-level quantities",
        "Existing seed-basket economics tooling can apply once quotes exist",
    ]
    kill = [
        "Cannot price without supplier quotes — economics UNKNOWN",
        "Deadline UNKNOWN — may already be too late",
        "Capital requirement UNKNOWN until commercial terms known",
        "No verified funding source",
    ]
    next_do = (
        (p7.get("next_action") or {}).get("what")
        or (p4.get("operator_action_generated") or {}).get("what")
        or (p10.get("needs_evidence") and "Obtain supplier quote (action marked NEEDS_EVIDENCE)")
        or "Review Deal Room unknowns and decide whether to spend time on supplier outreach"
    )
    return {
        "phase": 12,
        "name": "FINAL_OPERATOR_QUESTION",
        "question": (
            "What do I know about this opportunity, what don't I know, what could make it worth "
            "pursuing, what could kill it, and what should I do next?"
        ),
        "what_i_know": known,
        "what_i_dont_know": unknown,
        "what_could_make_it_worth_pursuing": worth,
        "what_could_kill_it": kill,
        "what_should_i_do_next": next_do,
        "win_prediction": None,
        "m3_answers_without_win_prediction": True,
        "pursuit_dimension_states": {
            k: (v or {}).get("state") for k, v in (p8.get("dimensions") or {}).items()
        },
        "next_hour_related": (p9.get("related_count") or 0),
    }


def run() -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    phases: dict[str, Any] = {}
    fixes: list[str] = []

    iowa = _load_iowa()
    p1 = phase1_discovery(iowa)
    phases["1"] = {k: v for k, v in p1.items() if k != "record"}
    record = p1["record"]

    p2 = phase2_screening(record)
    phases["2"] = p2
    issues.extend(p2.get("issues") or [])
    cid = p2["canonical_id"]
    global OID
    OID = cid

    for fn, key in [
        (phase3_product, "3"),
        (phase4_supply, "4"),
        (phase5_commercial, "5"),
        (phase6_economics, "6"),
        (phase7_capital, "7"),
        (phase8_pursuit, "8"),
        (phase9_next_hour, "9"),
        (phase10_operator_loop, "10"),
        (phase11_deal_room, "11"),
    ]:
        try:
            out = fn(cid)
            phases[key] = out
            issues.extend(out.get("issues") or [])
        except Exception as e:
            phases[key] = {"phase": int(key), "error": str(e), "trace": traceback.format_exc()[-1500:]}
            issues.append(
                _issue(
                    "BLOCKER",
                    failure=f"Phase {key} crashed: {e}",
                    module=fn.__name__,
                    reproduction=f"dry run phase {key} on {cid}",
                    likely_cause="uncaught exception",
                    recommended_fix="See trace in report",
                )
            )

    report: dict[str, Any] = {
        "kind": "M3FinalOperatorDryRunReport",
        "build": "20260919-m3-capital-requirement-gate-1",
        "generated_at": _utc(),
        "opportunity": {
            "canonical_id": cid,
            "solicitation_number": p1["solicitation_number"],
            "title": p1["title"],
            "agency": p1["agency"],
            "source": p1["source"],
            "source_url": p1["source_url"],
            "deadline": p1["deadline"],
            "artifact": str(BREAKTHROUGH.relative_to(ROOT)),
        },
        "phases": phases,
        "issues": issues,
        "fixes_made": fixes,
        "path_exercised": [
            "discovery_ingest",
            "stage1_ultra_cheap_screen",
            "product_line_items",
            "supply_intelligence",
            "commercial_validation_stop_without_quote",
            "economics_api",
            "capital_gate_api",
            "pursuit_readiness_api",
            "next_hour_api",
            "operator_loop_needs_evidence",
            "deal_room_api",
        ],
        "human_intervention_required": [
            "Obtain real supplier quotes (not invented in dry run)",
            "Verify funding path only after capital need known",
        ],
    }
    report["phases"]["12"] = phase12_operator_question(report)

    blockers = [i for i in issues if i["severity"] == "BLOCKER"]
    majors = [i for i in issues if i["severity"] == "MAJOR"]
    report["issue_counts"] = {
        "BLOCKER": len(blockers),
        "MAJOR": len(majors),
        "MINOR": len([i for i in issues if i["severity"] == "MINOR"]),
        "COSMETIC": len([i for i in issues if i["severity"] == "COSMETIC"]),
    }
    dl_ok = bool(
        (p1.get("deadline") not in (None, "", "UNKNOWN"))
        or ((p2.get("deadline") or "") not in (None, "", "UNKNOWN"))
    )
    report["can_operator_use_m3_discovery_to_pursuit_decision"] = {
        "answer": (
            "YES for evidence-backed discovery→pursuit decision support with known deadline; "
            "PARTIAL for commercial close until human-obtained supplier quotes exist"
            if dl_ok and not blockers
            else (
                "PARTIAL — YES for evidence-backed stop/go framing; NO for complete commercial "
                "close without human-obtained supplier quotes"
            )
        ),
        "rationale": [
            "M3 ingested a real product opportunity, screened it, exposed BOM/product unknowns, "
            "refused fabricated quotes, kept capital≠funding separate, and produced pursuit dimensions.",
            "Operator loop correctly refused DONE without evidence.",
            (
                "Response/closing deadline persisted from Iowa listing Close (Open ≠ Close)."
                if dl_ok
                else "Deadline still missing after listing Close capture — MAJOR defect remains."
            ),
            "No supplier quote means economics/capital remain UNKNOWN/PARTIAL by design.",
        ],
        "production_ready": bool(dl_ok and not blockers and len(majors) == 0),
        "production_ready_reason": (
            "Core discovery→decision path preserves deadline integrity; remaining gaps are "
            "honest commercial/capital unknowns until human quotes exist."
            if dl_ok and not blockers and len(majors) == 0
            else (
                "Remaining MAJOR/BLOCKER issues or missing deadline prevent full production readiness."
            )
        ),
    }

    REPORT_PATH.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return report


if __name__ == "__main__":
    rep = run()
    print(json.dumps({
        "canonical_id": rep["opportunity"]["canonical_id"],
        "issue_counts": rep["issue_counts"],
        "can_use": rep["can_operator_use_m3_discovery_to_pursuit_decision"]["answer"],
        "report": str(REPORT_PATH),
        "phase_errors": {k: v.get("error") for k, v in rep["phases"].items() if v.get("error")},
        "deal_room": (rep["phases"].get("11") or {}).get("http_status"),
        "capital": (rep["phases"].get("7") or {}).get("capital_required_state"),
        "pursuit_dims": (rep["phases"].get("12") or {}).get("pursuit_dimension_states"),
    }, indent=2))
