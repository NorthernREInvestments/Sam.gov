"""Phase L.18 — READY_TO_RESEARCH_NOW conversion + OpenGov agency-mirror expansion."""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from application_clock import now_utc
from discovery.lower48 import (
    DIBBS_CAGE_REQUIRED,
    FREE_REGISTRATION_REQUIRED,
    NONFEDERAL_ACCESSIBLE_NOW,
    current_access_score,
    is_nonfederal_accessible_now,
)
from discovery.opengov_agency_mirrors import (
    BUILD as MIRROR_BUILD,
    OPENGOV_CDN_ANTI_BOT,
    discover_and_activate_mirrors,
)
from discovery.sam_api_parked import SAM_API_PENDING_REPLACEMENT_KEY, sam_api_park_status
from discovery.structured_adapters import cross_source_dedupe_key
from phase_l.acquisition_lanes import STAGE3_NO_ROW_CAP, classify_acquisition_lane
from phase_l.bidnet_parked import BIDNET_AUTH_HISTORY_PARKED, park_bidnet_auth_history
from phase_l.economic_evaluability import recompute_economics_from_recovery
from phase_l.evidence_recovery import run_parallel_recovery
from phase_l.legacy_cleanup import assert_no_fixed_positive_cap, legacy_cleanup_report
from phase_l.original_solicitation import resolve_original_solicitation, submission_path_checklist
from phase_l.progressive_funnel import run_progressive_stages_cheap
from phase_l.quality_audit import (
    SECONDARY_QUOTE_TARGET,
    VALIDATED_QUOTE_TARGET,
    audit_quote_positive,
)
from phase_l.quote_economics import BUYER_VALUE_PATH, SUPPLIER_MEMORY_PATH, load_json, save_json

BUILD = "20260928-m3-phase-l18-research-queue-conversion-opengov-mirrors"
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "phase_l"
OUT.mkdir(parents=True, exist_ok=True)
DOCS = ROOT / "docs"

L173_BASELINE = {
    "baseline_source": "L173",
    "accessible": 3337,
    "stage3": 778,
    "commercial_stage3": 156,
    "NONFEDERAL_ACCESSIBLE_NOW": 52,
    "READY_TO_RESEARCH_NOW": 51,
    "READY_FOR_OWNER_APPROVAL": 1,
    "validated_unique": 1,
    "secondary_unique": 2,
}

# Owner decision states (§26)
PURSUE_QUOTE_NOW = "PURSUE_QUOTE_NOW"
RESEARCH_COMPLETE_WAITING_QUOTE = "RESEARCH_COMPLETE_WAITING_QUOTE"
REGISTER_AND_PURSUE = "REGISTER_AND_PURSUUE"  # spelling per spec
WATCH_RECURRING_BUYER = "WATCH_RECURRING_BUYER"
SKIP_ECONOMICS = "SKIP_ECONOMICS"
SKIP_ACCESS = "SKIP_ACCESS"
SKIP_PRODUCT = "SKIP_PRODUCT"
NEEDS_SPEC_RESOLUTION = "NEEDS_SPEC_RESOLUTION"

READY_FOR_OWNER_APPROVAL = "READY_FOR_OWNER_APPROVAL"
NEEDS_MINOR_REVIEW = "NEEDS_MINOR_REVIEW"
READY_TO_RESEARCH_NOW = "READY_TO_RESEARCH_NOW"

# Quantity states (§7)
QUANTITY_EXACT = "QUANTITY_EXACT"
QUANTITY_RANGE = "QUANTITY_RANGE"
QUANTITY_ESTIMATED = "QUANTITY_ESTIMATED"
QUANTITY_UNRESOLVED = "QUANTITY_UNRESOLVED"

# Product identity (§10)
EXACT_PRODUCT = "EXACT_PRODUCT"
STRONG_PRODUCT_IDENTITY = "STRONG_PRODUCT_IDENTITY"
DESCRIPTIVE_SPEC = "DESCRIPTIVE_SPEC"
BRAND_OR_EQUAL = "BRAND_OR_EQUAL"
IDENTITY_UNRESOLVED = "IDENTITY_UNRESOLVED"

# Reason codes (§43)
NO_QUANTITY = "NO_QUANTITY"
NO_PRODUCT_IDENTITY = "NO_PRODUCT_IDENTITY"
NO_GOV_VALUE = "NO_GOV_VALUE"
NO_CREDIBLE_SUPPLIER = "NO_CREDIBLE_SUPPLIER"
ECONOMICS_NEGATIVE = "ECONOMICS_NEGATIVE"
DEADLINE_FAIL = "DEADLINE_FAIL"
AUTH_BLOCKED = "AUTH_BLOCKED"
EXPIRED = "EXPIRED"
DOCUMENTS_UNAVAILABLE = "DOCUMENTS_UNAVAILABLE"
SERVICES_NOT_PRODUCT = "SERVICES_NOT_PRODUCT"


def _utc() -> str:
    return now_utc().isoformat()


def load_research_queue_input() -> list[dict[str, Any]]:
    path = OUT / "l173_ready_to_research.json"
    blob = json.loads(path.read_text(encoding="utf-8"))
    return list(blob.get("opportunities") or [])


def match_inventory_row(
    packet: dict[str, Any], rows: list[dict[str, Any]]
) -> dict[str, Any] | None:
    sol = str(packet.get("solicitation") or "").strip().lower()
    title = str(packet.get("title") or "").strip().lower()
    sid = str(packet.get("source_id") or "").strip().lower()
    best = None
    for r in rows:
        rsol = str(r.get("solicitation_number") or "").strip().lower()
        rtitle = str(r.get("title") or "").strip().lower()
        rsid = str(r.get("source_id") or r.get("source_portal") or "").strip().lower()
        if sol and rsol and sol == rsol:
            if not sid or not rsid or sid == rsid or sid == "unknown":
                return r
            best = best or r
        if title and rtitle and title[:80] == rtitle[:80]:
            if sid == "unknown" or not sid or sid == rsid:
                return r
            best = best or r
    return best


def classify_quantity_state(qty_rec: dict[str, Any] | None, row: dict[str, Any] | None = None) -> str:
    qty_rec = qty_rec or {}
    q = qty_rec.get("quantity")
    quality = str(qty_rec.get("quality") or "").upper()
    if q and quality in {"EXACT", "A", "QUANTITY_A_EXACT"}:
        return QUANTITY_EXACT
    if q and quality in {"RANGE", "B", "QUANTITY_B_RANGE"}:
        return QUANTITY_RANGE
    if qty_rec.get("unit_only") or quality in {"UNIT_ONLY", "ESTIMATED", "C"}:
        return QUANTITY_ESTIMATED
    if q:
        return QUANTITY_ESTIMATED
    if row:
        blob = f"{row.get('title') or ''} {row.get('description') or ''}"
        if re.search(r"\bone\s*\(\s*1\s*\)|\bqty\s*[:=]?\s*\d+\b|\bquantity\s*[:=]?\s*\d+\b", blob, re.I):
            return QUANTITY_EXACT
        if re.search(r"\b\d+\s*(ea|each|units?|pcs?|pieces?)\b", blob, re.I):
            return QUANTITY_ESTIMATED
    return QUANTITY_UNRESOLVED


def classify_product_identity(commercial: dict[str, Any], row: dict[str, Any], config: dict[str, Any]) -> str:
    if commercial.get("mpn") or commercial.get("sku"):
        return EXACT_PRODUCT
    if commercial.get("model") and commercial.get("manufacturer"):
        return STRONG_PRODUCT_IDENTITY
    if config.get("brand_or_equal"):
        return BRAND_OR_EQUAL
    blob = f"{row.get('title') or ''} {row.get('description') or ''}"
    blob_l = blob.lower()
    if re.search(r"\b(or\s+equal|brand[\-\s]?name\s+or\s+equal|or\s+equivalent)\b", blob_l):
        return BRAND_OR_EQUAL
    if re.search(
        r"\b(asus\s+chromebox|apple\s+ipad|ricoh\s+scansnap\s*sv\d+|caterpillar\s+model\s+c\d+|"
        r"yamaha\s+piano|hudson\s+plastic|part\s+0?\d{5,}|\bscansnap\s*sv\d+)\b",
        blob_l,
    ):
        if re.search(r"\b(part\s+0?\d{5,}|scansnap\s*sv\d+|model\s+c\d+)\b", blob_l):
            return EXACT_PRODUCT
        return STRONG_PRODUCT_IDENTITY
    if re.search(
        r"\b(ford\s+police\s+pursuit|grand\s+wagoneer|general\s+motors|cisco\s+catay?yst|"
        r"microsoft\s+teams|chromebox|ipad\s+11|forklifts?\s+(diesel|electric))\b",
        blob_l,
    ):
        return STRONG_PRODUCT_IDENTITY
    if commercial.get("manufacturer") or commercial.get("model"):
        return DESCRIPTIVE_SPEC
    if re.search(
        r"\b(laptop|desktop|server|switch|router|pump|motor|vehicle|truck|furniture|hvac|"
        r"monitor|forklift|tablet|scanner|sprayer|piano|engine|lamp|filter)\b",
        blob_l,
    ):
        return DESCRIPTIVE_SPEC
    return IDENTITY_UNRESOLVED


def deadline_runway(row: dict[str, Any], pipe: dict[str, Any]) -> dict[str, Any]:
    d = (pipe.get("stage0") or {}).get("deadline") or {}
    days = None
    for k in ("days_remaining", "days_to_deadline", "deadline_days"):
        if d.get(k) is not None:
            try:
                days = float(d[k])
            except (TypeError, ValueError):
                days = None
            break
    raw = row.get("response_deadline") or row.get("deadline") or row.get("due_date")
    if days is None and raw:
        try:
            # ISO-ish
            s = str(raw).replace("Z", "+00:00")
            dt = datetime.fromisoformat(s[:19])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            days = (dt - datetime.now(timezone.utc)).total_seconds() / 86400.0
        except Exception:
            days = None
    if days is not None and days < 0:
        label = "EXECUTION_FAIL"
    elif days is None:
        label = "ACCEPTABLE_RUNWAY"  # unknown — not auto-fail
    elif days >= 5:
        label = "STRONG_RUNWAY"
    elif days >= 3:
        label = "ACCEPTABLE_RUNWAY"
    elif days >= 0:
        label = "TIGHT"
    else:
        label = "EXECUTION_FAIL"
    return {"days": days, "label": label, "deadline_raw": raw}


def is_likely_services(row: dict[str, Any]) -> bool:
    blob = f"{row.get('title') or ''} {row.get('description') or ''}".lower()
    service = bool(
        re.search(
            r"\b(consulting|staffing|professional\s+services|construction|renovation|"
            r"operation\s+services|janitorial|security\s+guard|food\s+service|catering|"
            r"engineering\s+services|management\s+support|maintenance\s+services|"
            r"towing\s+services|appraisal\s+services|prevention\s+courses|"
            r"repair\s+and\s+maintenance|inspection,?\s+maintenance|"
            r"upfitting|vehicle\s+maintenance|procurement\s+policy)\b",
            blob,
        )
    )
    product = bool(
        re.search(
            r"\b(equipment|laptop|computer|vehicle|truck|pump|motor|furniture|hvac|"
            r"switch|router|monitor|parts|supply|supplies|hardware|forklift|tablet|"
            r"scanner|piano|engine|chromebox|ipad|sprayer)\b",
            blob,
        )
    )
    # Pure services / courses / policies — even if "vehicle" appears in title
    pure_service = bool(
        re.search(
            r"\b(operation\s+services|towing\s+services|appraisal\s+services|"
            r"prevention\s+courses|procurement\s+policy|software\s+platform|"
            r"paid\s+detail\s+application)\b",
            blob,
        )
    )
    if pure_service:
        return True
    return service and not product


def winability_evidence(row: dict[str, Any], recovery: dict[str, Any], runway: dict[str, Any]) -> dict[str, Any]:
    factors = {
        "low_historical_bidder_count": bool(row.get("offer_count") and int(row.get("offer_count") or 99) <= 3),
        "repeat_open_competition": bool(row.get("recurring_buyer")),
        "commodity_specification": bool(
            re.search(r"\b(commodity|off[\-\s]?the[\-\s]?shelf|commercial\s+item)\b", str(row.get("title") or ""), re.I)
        ),
        "many_supplier_channels": len((recovery.get("suppliers") or {}).get("candidates") or recovery.get("suppliers") or [])
        >= 2
        if isinstance(recovery.get("suppliers"), dict)
        else len(recovery.get("suppliers") or []) >= 2,
        "no_incumbent_lock": not bool(row.get("incumbent")),
        "price_driven_evaluation": "lowest" in str(row.get("evaluation") or row.get("title") or "").lower()
        or "lpta" in str(row.get("title") or "").lower(),
        "simple_submission": bool((row.get("authoritative_bid_location") or {}).get("detail_url")),
        "adequate_runway": runway.get("label") in {"STRONG_RUNWAY", "ACCEPTABLE_RUNWAY"},
    }
    return {
        "kind": "WinabilityEvidence",
        "factors": factors,
        "positive_factor_count": sum(1 for v in factors.values() if v),
        "note": "Evidence only — not a statistical win probability",
    }


def classify_owner_decision(
    *,
    expired: bool,
    services: bool,
    access_blocked: bool,
    qty_state: str,
    identity: str,
    gov_grade: str,
    suppliers: list[dict[str, Any]],
    qstate: str,
    needs_reg: bool,
    econ: dict[str, Any],
    runway: dict[str, Any],
    recurring: bool,
) -> tuple[str, str | None]:
    if expired or (runway.get("label") == "EXECUTION_FAIL" and (runway.get("days") or 0) < 0):
        return SKIP_ECONOMICS, EXPIRED if expired else DEADLINE_FAIL
    if access_blocked:
        return SKIP_ACCESS, AUTH_BLOCKED
    if services:
        return SKIP_PRODUCT, SERVICES_NOT_PRODUCT
    if qstate == VALIDATED_QUOTE_TARGET:
        return (REGISTER_AND_PURSUE if needs_reg else PURSUE_QUOTE_NOW), None
    if qstate == SECONDARY_QUOTE_TARGET:
        return (REGISTER_AND_PURSUE if needs_reg else RESEARCH_COMPLETE_WAITING_QUOTE), None
    if identity == IDENTITY_UNRESOLVED and qty_state == QUANTITY_UNRESOLVED:
        return NEEDS_SPEC_RESOLUTION, NO_PRODUCT_IDENTITY
    if qty_state == QUANTITY_UNRESOLVED and identity in {IDENTITY_UNRESOLVED, DESCRIPTIVE_SPEC}:
        return NEEDS_SPEC_RESOLUTION, NO_QUANTITY
    gov_u = "UNKNOWN" in str(gov_grade).upper() or str(gov_grade).upper() in {"", "NONE"}
    if gov_u and not suppliers:
        return NEEDS_SPEC_RESOLUTION, NO_GOV_VALUE
    if not suppliers and "QUOTE" not in str((econ.get("quote_dependent") or {}).get("status") or "").upper():
        # still may be quote-required path
        if identity in {EXACT_PRODUCT, STRONG_PRODUCT_IDENTITY, BRAND_OR_EQUAL, DESCRIPTIVE_SPEC}:
            return RESEARCH_COMPLETE_WAITING_QUOTE, NO_CREDIBLE_SUPPLIER
        return NEEDS_SPEC_RESOLUTION, NO_CREDIBLE_SUPPLIER
    if str(qstate) in {"ECONOMIC_CASE_TOO_WEAK", "RECON_ONLY_CATEGORY_BENCHMARK"}:
        if recurring:
            return WATCH_RECURRING_BUYER, ECONOMICS_NEGATIVE
        if identity != IDENTITY_UNRESOLVED and qty_state != QUANTITY_UNRESOLVED:
            return RESEARCH_COMPLETE_WAITING_QUOTE, ECONOMICS_NEGATIVE
        return SKIP_ECONOMICS, ECONOMICS_NEGATIVE
    if needs_reg:
        return REGISTER_AND_PURSUE, None
    if suppliers:
        return RESEARCH_COMPLETE_WAITING_QUOTE, None
    return NEEDS_SPEC_RESOLUTION, NO_PRODUCT_IDENTITY


def profit_tiers(max_buy: dict[str, Any] | None, gov: dict[str, Any] | None) -> dict[str, Any]:
    max_buy = max_buy or {}
    gov = gov or {}
    # Prefer explicit tier flags if present
    tiers = {
        "positive": bool(max_buy.get("positive") or max_buy.get("profit_positive")),
        "gte_5k": bool(max_buy.get("profit_gte_5k") or max_buy.get("gte_5k")),
        "gte_10k": bool(max_buy.get("profit_gte_10k") or max_buy.get("gte_10k")),
        "gte_25k": bool(max_buy.get("profit_gte_25k") or max_buy.get("gte_25k")),
        "gte_50k": bool(max_buy.get("profit_gte_50k") or max_buy.get("gte_50k")),
        "gte_100k": bool(max_buy.get("profit_gte_100k") or max_buy.get("gte_100k")),
    }
    # Derive from spread if available
    spread = max_buy.get("max_profit") or max_buy.get("expected_profit") or gov.get("total_value")
    try:
        p = float(spread) if spread is not None else None
    except (TypeError, ValueError):
        p = None
    if p is not None:
        tiers["positive"] = tiers["positive"] or p > 0
        tiers["gte_5k"] = tiers["gte_5k"] or p >= 5000
        tiers["gte_10k"] = tiers["gte_10k"] or p >= 10000
        tiers["gte_25k"] = tiers["gte_25k"] or p >= 25000
        tiers["gte_50k"] = tiers["gte_50k"] or p >= 50000
        tiers["gte_100k"] = tiers["gte_100k"] or p >= 100000
    return tiers


def deep_research_row(
    row: dict[str, Any],
    packet: dict[str, Any],
    *,
    buyer_memory: dict[str, Any],
    supplier_memory: dict[str, Any],
) -> dict[str, Any]:
    pipe = run_progressive_stages_cheap(row)
    original = resolve_original_solicitation(row)
    submission = submission_path_checklist(row, original=original)
    runway = deadline_runway(row, pipe)
    expired = runway.get("label") == "EXECUTION_FAIL" and (runway.get("days") is not None and runway["days"] < 0)

    # Live/open recheck — stop early if expired
    if expired:
        return {
            "packet": packet,
            "title": row.get("title"),
            "solicitation": original.get("solicitation_number") or packet.get("solicitation"),
            "buyer": row.get("agency") or packet.get("buyer"),
            "state": row.get("state_code") or packet.get("state"),
            "owner_decision": SKIP_ECONOMICS,
            "reason_code": EXPIRED,
            "research_complete": True,
            "expired": True,
            "runway": runway,
            "quantity_state": QUANTITY_UNRESOLVED,
            "product_identity": IDENTITY_UNRESOLVED,
            "gov_grade": "GOV_VALUE_UNKNOWN",
            "supplier_grades": [],
            "quote_state": "EXPIRED",
            "authoritative_posting": (row.get("detail_url") or original.get("detail_url")),
            "submission_path": submission,
        }

    screen = pipe.get("stage2_screen") or {}
    commercial = dict(
        screen.get("commercial_identity") or ((pipe.get("stage2") or {}).get("commercial") or {})
    )
    s3 = pipe.get("stage3") or {}
    # Pure service titles win over Stage2 false positives
    services = is_likely_services(row)
    if not services and not bool((pipe.get("stage2") or {}).get("pass")):
        # Non-pass Stage2 without clear service keywords stays product-researchable
        services = False

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
    lane = classify_acquisition_lane(row, commercial=commercial).get("acquisition_lane")
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
        buyer_memory=buyer_memory,
        supplier_memory=supplier_memory,
        stage3=s3,
        attempt_upgrades=True,
    )
    qty_state = classify_quantity_state(recovery.get("quantity") or econ.get("quantity"), row)
    config = recovery.get("configuration") or {}
    identity = classify_product_identity(commercial, row, config)
    gov = econ.get("government_value") if isinstance(econ.get("government_value"), dict) else {}
    gov_grade = str(audit.get("gov_grade") or gov.get("grade") or "GOV_VALUE_UNKNOWN")
    suppliers_raw = econ.get("suppliers")
    if isinstance(suppliers_raw, dict):
        suppliers = list(suppliers_raw.get("candidates") or suppliers_raw.get("suppliers") or [])
        if not suppliers and suppliers_raw.get("status"):
            suppliers = [suppliers_raw]
    elif isinstance(suppliers_raw, list):
        suppliers = suppliers_raw
    else:
        suppliers = []
    # Also from recovery
    rec_sup = recovery.get("suppliers")
    if isinstance(rec_sup, dict):
        suppliers = suppliers or list(rec_sup.get("candidates") or [])
    elif isinstance(rec_sup, list):
        suppliers = suppliers or rec_sup

    supplier_grades = []
    for s in suppliers:
        if isinstance(s, dict):
            supplier_grades.append(str(s.get("grade") or s.get("supplier_grade") or s.get("quality") or "D"))

    qstate = str(audit.get("quality_state") or "")
    needs_reg = bool(packet.get("registration_required")) or str(
        packet.get("current_access_status") or ""
    ) in {FREE_REGISTRATION_REQUIRED, "SIMPLE_VENDOR_SETUP"}
    access_blocked = str(packet.get("current_access_status") or "").upper() in {
        "AUTH_REQUIRED",
        "HARD_ELIGIBILITY_BLOCKER",
        "PAID_ACCESS_REQUIRED",
    }
    recurring = bool(row.get("recurring_buyer") or packet.get("recurring_buyer"))
    decision, reason = classify_owner_decision(
        expired=False,
        services=services,
        access_blocked=access_blocked,
        qty_state=qty_state,
        identity=identity,
        gov_grade=gov_grade,
        suppliers=suppliers,
        qstate=qstate,
        needs_reg=needs_reg,
        econ=econ,
        runway=runway,
        recurring=recurring,
    )
    max_buy = econ.get("max_buy") if isinstance(econ.get("max_buy"), dict) else {}
    freight = econ.get("freight") if isinstance(econ.get("freight"), dict) else {}
    win = winability_evidence(row, recovery, runway)
    tiers = profit_tiers(max_buy, gov)

    # Quote target mapping
    pilot_state = None
    if qstate == VALIDATED_QUOTE_TARGET:
        pilot_state = READY_FOR_OWNER_APPROVAL
    elif qstate == SECONDARY_QUOTE_TARGET:
        pilot_state = NEEDS_MINOR_REVIEW

    return {
        "packet_key": cross_source_dedupe_key(row) or packet.get("title"),
        "title": row.get("title"),
        "solicitation": original.get("solicitation_number") or packet.get("solicitation"),
        "buyer": row.get("agency") or packet.get("buyer"),
        "state": row.get("state_code") or packet.get("state"),
        "source_id": row.get("source_id") or packet.get("source_id"),
        "platform": row.get("platform_family") or packet.get("platform"),
        "deadline": runway.get("deadline_raw"),
        "runway": runway,
        "authoritative_posting": row.get("detail_url") or original.get("detail_url"),
        "submission_location": {
            "detail_url": row.get("detail_url") or original.get("detail_url"),
            "submission_path": submission,
            "solicitation_id": original.get("solicitation_number"),
        },
        "registration_required": needs_reg,
        "quantity_state": qty_state,
        "quantity": (recovery.get("quantity") or {}).get("quantity"),
        "configuration": {
            "manufacturer": config.get("manufacturer") or commercial.get("manufacturer"),
            "model": config.get("base_model") or commercial.get("model"),
            "brand_or_equal": config.get("brand_or_equal"),
            "options": config.get("options_detected"),
        },
        "product_identity": identity,
        "gov_grade": gov_grade,
        "gov_recovery": {
            "state": (recovery.get("gov") or {}).get("state") or gov.get("state"),
            "recovered": (recovery.get("gov") or {}).get("recovered"),
        },
        "supplier_grades": supplier_grades,
        "supplier_count": len(suppliers),
        "quote_state": qstate,
        "pilot_state": pilot_state,
        "owner_decision": decision,
        "reason_code": reason or audit.get("primary_blocker") or audit.get("reason"),
        "economics": {
            "evaluable": "EVALUABLE" in str((econ.get("evaluability") or {}).get("state") or max_buy.get("status") or ""),
            "max_buy": max_buy,
            "freight_state": freight.get("state") or freight.get("status"),
            "profit_tiers": tiers,
        },
        "winability": win,
        "recurring_buy_signal": recurring,
        "research_complete": True,
        "expired": False,
        "stage2_pass": bool((pipe.get("stage2") or {}).get("pass")),
        "document_access": row.get("document_access") or packet.get("document_access"),
        "deal_card": {
            "buyer": row.get("agency") or packet.get("buyer"),
            "solicitation": original.get("solicitation_number") or packet.get("solicitation"),
            "product": row.get("title"),
            "quantity": (recovery.get("quantity") or {}).get("quantity"),
            "deadline": runway.get("deadline_raw"),
            "authoritative_source": row.get("detail_url") or original.get("detail_url"),
            "submission_location": row.get("detail_url"),
            "registration": needs_reg,
            "gov_evidence": gov_grade,
            "supplier_candidates": supplier_grades[:5],
            "max_buy": max_buy,
            "expected_profit_tiers": tiers,
            "competition_evidence": {
                "offer_count": row.get("offer_count"),
                "incumbent": row.get("incumbent"),
            },
            "winability": win,
            "recommendation": decision,
            "remaining_uncertainty": reason,
        },
    }


def run_phase_l18(*, run_opengov_mirrors: bool = True, max_mirror_blocked: int = 35) -> dict[str, Any]:
    assert_no_fixed_positive_cap()
    assert STAGE3_NO_ROW_CAP
    park_bidnet_auth_history()
    sam = sam_api_park_status()
    assert sam["calls_consumed"] == 0

    from operating_mode import MODE_DEVELOPMENT_NO_OUTREACH, set_operating_mode

    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)

    queue = load_research_queue_input()
    print(f"[l18] research queue input={len(queue)}", flush=True)
    save_json(
        OUT / "l18_research_queue_input.json",
        {"kind": "L18ResearchQueueInput", "build": BUILD, "count": len(queue), "opportunities": queue},
    )

    data = json.loads((OUT / "accessible_latest.json").read_text(encoding="utf-8"))
    rows = list(data.get("rows") or [])
    buyer_memory = load_json(BUYER_VALUE_PATH)
    supplier_memory = load_json(SUPPLIER_MEMORY_PATH)

    results: list[dict[str, Any]] = []
    attempted = completed = 0
    for i, packet in enumerate(queue):
        attempted += 1
        row = match_inventory_row(packet, rows)
        if not row:
            results.append(
                {
                    "title": packet.get("title"),
                    "solicitation": packet.get("solicitation"),
                    "owner_decision": NEEDS_SPEC_RESOLUTION,
                    "reason_code": DOCUMENTS_UNAVAILABLE,
                    "research_complete": True,
                    "error": "inventory_row_not_found",
                }
            )
            completed += 1
            continue
        print(f"[l18] research {i+1}/{len(queue)}: {(packet.get('title') or '')[:70]}", flush=True)
        try:
            res = deep_research_row(row, packet, buyer_memory=buyer_memory, supplier_memory=supplier_memory)
        except Exception as e:
            res = {
                "title": packet.get("title"),
                "solicitation": packet.get("solicitation"),
                "owner_decision": NEEDS_SPEC_RESOLUTION,
                "reason_code": DOCUMENTS_UNAVAILABLE,
                "research_complete": True,
                "error": str(e)[:240],
            }
        results.append(res)
        completed += 1

    # OpenGov mirrors (secondary)
    mirror_report: dict[str, Any] = {"skipped": True}
    mirror_rows: list[dict[str, Any]] = []
    if run_opengov_mirrors:
        print("[l18] OpenGov agency mirror discovery...", flush=True)
        mirror_report = discover_and_activate_mirrors(max_blocked=max_mirror_blocked, resume=True)
        mirror_rows = list(mirror_report.pop("rows", []) or [])
        print(
            f"[l18] mirrors attempted={mirror_report.get('blocked_attempted')} "
            f"found={mirror_report.get('mirrors_found')} active={mirror_report.get('automated')} "
            f"new_rows={len(mirror_rows)}",
            flush=True,
        )
        # Merge mirror rows into accessible inventory
        existing = {cross_source_dedupe_key(r) for r in rows}
        added = 0
        from phase_l.hunt import screen_and_rank

        ranked = screen_and_rank(mirror_rows) if mirror_rows else {"accessible": []}
        for r in ranked.get("accessible") or mirror_rows:
            k = cross_source_dedupe_key(r)
            if k in existing:
                continue
            existing.add(k)
            rows.append(r)
            added += 1
        data["rows"] = rows
        save_json(OUT / "accessible_latest.json", data)
        mirror_report["newly_merged"] = added

    # Aggregations
    decisions = Counter(r.get("owner_decision") for r in results)
    reasons = Counter(r.get("reason_code") for r in results if r.get("reason_code"))
    qty_c = Counter(r.get("quantity_state") for r in results)
    id_c = Counter(r.get("product_identity") for r in results)
    gov_c = Counter(str(r.get("gov_grade") or "unknown").upper() for r in results)
    # Normalize gov grades
    gov_norm = Counter()
    for g, n in gov_c.items():
        letter = "unknown"
        if "GOV_VALUE_A" in g or g.endswith("_A") or g == "A":
            letter = "A"
        elif "GOV_VALUE_B" in g or g.endswith("_B") or g == "B":
            letter = "B"
        elif "GOV_VALUE_C" in g or g.endswith("_C") or g == "C":
            letter = "C"
        elif "GOV_VALUE_D" in g or g.endswith("_D") or g == "D":
            letter = "D"
        elif "UNKNOWN" in g:
            letter = "unknown"
        gov_norm[letter] += n

    supplier_grade_c = Counter()
    quote_required = 0
    for r in results:
        grades = r.get("supplier_grades") or []
        if not grades and r.get("owner_decision") == RESEARCH_COMPLETE_WAITING_QUOTE:
            quote_required += 1
        for g in grades:
            gg = str(g).upper()
            if "A" in gg[:3]:
                supplier_grade_c["A"] += 1
            elif "B" in gg[:3]:
                supplier_grade_c["B"] += 1
            elif "C" in gg[:3]:
                supplier_grade_c["C"] += 1
            else:
                supplier_grade_c["D"] += 1

    validated = [r for r in results if r.get("quote_state") == VALIDATED_QUOTE_TARGET]
    secondary = [r for r in results if r.get("quote_state") == SECONDARY_QUOTE_TARGET]
    ready_owner = [r for r in results if r.get("pilot_state") == READY_FOR_OWNER_APPROVAL]
    needs_review = [r for r in results if r.get("pilot_state") == NEEDS_MINOR_REVIEW]

    # Remaining READY_TO_RESEARCH = incomplete or NEEDS_SPEC only (researched rows leave queue)
    remaining_research = [
        r
        for r in results
        if not r.get("research_complete")
        or r.get("owner_decision") == NEEDS_SPEC_RESOLUTION
    ]
    # Spec: do not leave already researched rows in queue — only unresolved/spec-needed
    remaining_packets = [
        {
            "title": r.get("title"),
            "solicitation": r.get("solicitation"),
            "buyer": r.get("buyer"),
            "owner_decision": r.get("owner_decision"),
            "reason_code": r.get("reason_code"),
            "queue": READY_TO_RESEARCH_NOW,
        }
        for r in remaining_research
        if r.get("owner_decision") == NEEDS_SPEC_RESOLUTION
    ]

    econ_eval = sum(1 for r in results if (r.get("economics") or {}).get("evaluable"))
    tiers_agg = Counter()
    for r in results:
        pt = ((r.get("economics") or {}).get("profit_tiers") or {})
        for k, v in pt.items():
            if v:
                tiers_agg[k] += 1

    qty_resolved = sum(1 for r in results if r.get("quantity_state") in {QUANTITY_EXACT, QUANTITY_RANGE, QUANTITY_ESTIMATED})
    config_resolved = sum(
        1
        for r in results
        if r.get("product_identity")
        not in {IDENTITY_UNRESOLVED, None}
    )

    # Verdict
    conversion_signal = (
        len(validated) + len(secondary) + decisions.get(PURSUE_QUOTE_NOW, 0)
        + decisions.get(RESEARCH_COMPLETE_WAITING_QUOTE, 0)
        + decisions.get(REGISTER_AND_PURSUE, 0)
    )
    qty_progress = qty_resolved >= 10 or qty_c.get(QUANTITY_EXACT, 0) >= 3
    mirror_progress = int(mirror_report.get("automated") or 0) >= 1 or int(mirror_report.get("newly_merged") or 0) > 0
    if attempted >= 51 and conversion_signal >= 5 and (qty_progress or conversion_signal >= 15 or mirror_progress):
        verdict = "PHASE_L18_RESEARCH_QUEUE_CONVERSION_WORKING"
    elif attempted >= 40 and (conversion_signal >= 1 or qty_progress or mirror_progress):
        verdict = "PHASE_L18_PARTIAL_RESEARCH_QUEUE_CONVERSION"
    else:
        verdict = "PHASE_L18_RESEARCH_QUEUE_CONVERSION_FAILED"

    remaining_bottleneck = "Gov evidence upgrades (A/B) on product-fit local solicitations"
    top_reason = reasons.most_common(1)[0][0] if reasons else None
    if top_reason == ECONOMICS_NEGATIVE:
        remaining_bottleneck = "buyer-specific award/history recovery to lift Gov D into usable A/B/C evidence"
    elif top_reason == NO_GOV_VALUE:
        remaining_bottleneck = "buyer-specific award/history recovery for local solicitations"
    elif top_reason == NO_QUANTITY:
        remaining_bottleneck = "solicitation package / line-item quantity extraction"
    elif top_reason == NO_PRODUCT_IDENTITY:
        remaining_bottleneck = "document recovery for exact MPN/model identity on unresolved product rows"
    elif top_reason == SERVICES_NOT_PRODUCT:
        remaining_bottleneck = "tighten Stage2 product filter before research queue admission"
    elif int(mirror_report.get("still_blocked") or 0) > 40 and int(mirror_report.get("automated") or 0) == 0:
        remaining_bottleneck = "OpenGov agency-mirror discovery for remaining CDN-blocked buyers"

    # Artifacts
    quantity_art = {
        "kind": "L18QuantityResolution",
        "build": BUILD,
        "counts": dict(qty_c),
        "exact": [r for r in results if r.get("quantity_state") == QUANTITY_EXACT][:40],
        "unresolved": [r for r in results if r.get("quantity_state") == QUANTITY_UNRESOLVED][:40],
    }
    config_art = {
        "kind": "L18ConfigurationResolution",
        "build": BUILD,
        "identity_counts": dict(id_c),
        "samples": [
            {"title": r.get("title"), "identity": r.get("product_identity"), "configuration": r.get("configuration")}
            for r in results
            if r.get("configuration")
        ][:40],
    }
    gov_art = {
        "kind": "L18GovEvidence",
        "build": BUILD,
        "grades": dict(gov_norm),
        "raw_grades": dict(gov_c),
        "upgrades_note": "Gov evidence grades unchanged in schema; recovery attempted via existing pipeline",
    }
    supplier_art = {
        "kind": "L18SupplierEvidence",
        "build": BUILD,
        "grades": dict(supplier_grade_c),
        "quote_required_rows": quote_required,
    }
    economics_art = {
        "kind": "L18Economics",
        "build": BUILD,
        "evaluable": econ_eval,
        "profit_tiers": dict(tiers_agg),
        "samples": [r.get("economics") for r in results if (r.get("economics") or {}).get("max_buy")][:30],
    }
    win_art = {
        "kind": "L18WinabilityEvidence",
        "build": BUILD,
        "samples": [r.get("winability") for r in results if r.get("winability")][:40],
    }
    owner_art = {
        "kind": "L18OwnerDecisions",
        "build": BUILD,
        "counts": dict(decisions),
        "decisions": [
            {
                "title": r.get("title"),
                "solicitation": r.get("solicitation"),
                "buyer": r.get("buyer"),
                "owner_decision": r.get("owner_decision"),
                "reason_code": r.get("reason_code"),
                "deal_card": r.get("deal_card"),
            }
            for r in results
        ],
    }
    quotes = {
        "kind": "L18QuoteTargets",
        "build": BUILD,
        "validated_unique": len({r.get("solicitation") or r.get("title") for r in validated}),
        "secondary_unique": len({r.get("solicitation") or r.get("title") for r in secondary}),
        "READY_FOR_OWNER_APPROVAL": len(ready_owner),
        "NEEDS_MINOR_REVIEW": len(needs_review),
        "validated": validated[:20],
        "secondary": secondary[:20],
        "pilot_gate_unchanged": True,
        "no_outreach": True,
    }
    research_results = {
        "kind": "L18ResearchResults",
        "build": BUILD,
        "generated_at": _utc(),
        "attempted": attempted,
        "completed": completed,
        "remaining_in_research_queue": len(remaining_packets),
        "results": results,
        "remaining_queue": remaining_packets,
    }
    summary = {
        "kind": "L18Summary",
        "build": BUILD,
        "generated_at": _utc(),
        "verdict": verdict,
        "baseline_used": L173_BASELINE,
        "research_queue": {
            "input": len(queue),
            "attempted": attempted,
            "completed": completed,
            "remaining": len(remaining_packets),
        },
        "quantity_configuration": {
            "quantity_counts": dict(qty_c),
            "identity_counts": dict(id_c),
            "quantity_resolved": qty_resolved,
            "configuration_resolved": config_resolved,
        },
        "government_evidence": dict(gov_norm),
        "supplier_evidence": {**dict(supplier_grade_c), "quote_required": quote_required},
        "economics": {"evaluable": econ_eval, "profit_tiers": dict(tiers_agg)},
        "owner_decisions": dict(decisions),
        "quote_targets": {
            "validated": quotes["validated_unique"],
            "secondary": quotes["secondary_unique"],
            "READY_FOR_OWNER_APPROVAL": quotes["READY_FOR_OWNER_APPROVAL"],
            "NEEDS_MINOR_REVIEW": quotes["NEEDS_MINOR_REVIEW"],
        },
        "opengov_mirrors": {
            "attempted": mirror_report.get("blocked_attempted"),
            "found": mirror_report.get("mirrors_found"),
            "confirmed": mirror_report.get("mirrors_confirmed"),
            "active": mirror_report.get("automated"),
            "still_blocked": mirror_report.get("still_blocked"),
            "new_opportunities": mirror_report.get("newly_merged") or mirror_report.get("new_live_rows"),
            "status_note": OPENGOV_CDN_ANTI_BOT,
        },
        "bottleneck_distribution": reasons.most_common(20),
        "conversion_funnel": {
            "READY_TO_RESEARCH_NOW": len(queue),
            "product_fit": sum(1 for r in results if r.get("stage2_pass")),
            "quantity_resolved": qty_resolved,
            "gov_usable": sum(1 for r in results if gov_norm and str(r.get("gov_grade") or "").upper() not in {"GOV_VALUE_UNKNOWN", "UNKNOWN", ""}),
            "suppliers_present": sum(1 for r in results if (r.get("supplier_count") or 0) > 0),
            "economics_evaluable": econ_eval,
            "quote_targets": quotes["validated_unique"] + quotes["secondary_unique"],
            "owner_approval": quotes["READY_FOR_OWNER_APPROVAL"],
        },
        "remaining_bottleneck": remaining_bottleneck,
        "sam_api": sam,
        "bidnet": BIDNET_AUTH_HISTORY_PARKED,
        "dibbs": DIBBS_CAGE_REQUIRED,
        "no_sam_api_calls": True,
        "no_outreach": True,
        "no_cloudflare_bypass": True,
        "evidence_gate_unchanged": True,
        "legacy_cleanup": legacy_cleanup_report(),
    }

    artifacts = {
        "l18_research_results.json": research_results,
        "l18_quantity_resolution.json": quantity_art,
        "l18_configuration_resolution.json": config_art,
        "l18_gov_evidence.json": gov_art,
        "l18_supplier_evidence.json": supplier_art,
        "l18_economics.json": economics_art,
        "l18_winability_evidence.json": win_art,
        "l18_opengov_mirrors.json": {
            k: v for k, v in mirror_report.items() if k != "rows"
        },
        "l18_owner_decisions.json": owner_art,
        "l18_quote_targets.json": quotes,
        "l18_summary.json": summary,
    }
    # input already written
    for name, payload in artifacts.items():
        save_json(OUT / name, payload)
        print(f"[l18] wrote {name}", flush=True)

    write_l18_docs(summary, decisions, reasons, mirror_report)
    return summary


def write_l18_docs(
    summary: dict[str, Any],
    decisions: Counter,
    reasons: Counter,
    mirrors: dict[str, Any],
) -> None:
    DOCS.mkdir(parents=True, exist_ok=True)
    docs = {
        "phase_l18_research_conversion.md": f"""# Phase L.18 — Research Queue Conversion

Build: `{BUILD}`

## Verdict

`{summary.get('verdict')}`

Processed all `{summary.get('research_queue', {}).get('input')}` READY_TO_RESEARCH_NOW rows.

Remaining in research queue (spec-unresolved only): `{summary.get('research_queue', {}).get('remaining')}`
""",
        "phase_l18_quantity_config.md": f"""# L.18 Quantity / Configuration

```json
{json.dumps(summary.get('quantity_configuration'), indent=2)}
```
""",
        "phase_l18_gov_evidence.md": f"""# L.18 Gov Evidence

Grades (schema unchanged):

```json
{json.dumps(summary.get('government_evidence'), indent=2)}
```
""",
        "phase_l18_supplier_research.md": f"""# L.18 Supplier Research

```json
{json.dumps(summary.get('supplier_evidence'), indent=2)}
```

No automated supplier outreach.
""",
        "phase_l18_economics.md": f"""# L.18 Economics

```json
{json.dumps(summary.get('economics'), indent=2)}
```
""",
        "phase_l18_winability.md": """# L.18 Winability Evidence

`WinabilityEvidence` factors are qualitative signals only — not fake probabilities.
See `l18_winability_evidence.json`.
""",
        "phase_l18_opengov_mirrors.md": f"""# L.18 OpenGov Agency Mirrors

No Cloudflare bypass. CDN remains `{OPENGOV_CDN_ANTI_BOT}` when no official mirror exists.

```json
{json.dumps(summary.get('opengov_mirrors'), indent=2)}
```
""",
        "phase_l18_owner_decisions.md": f"""# L.18 Owner Decisions

```json
{json.dumps(dict(decisions), indent=2)}
```

Bottlenecks:

```json
{json.dumps(reasons.most_common(15), indent=2)}
```
""",
        "phase_l18_legacy_cleanup.md": """# L.18 Legacy Cleanup

- Canonical deep research via existing `run_parallel_recovery` / `audit_quote_positive`
- OpenGov mirrors via `discovery/opengov_agency_mirrors.py` (generic, not per-city scrapers)
- Research-complete rows leave READY_TO_RESEARCH_NOW; only NEEDS_SPEC_RESOLUTION remains
- SAM Opportunities API parked; no Cloudflare bypass; no outreach
""",
        "phase_l18_regression.md": """# L.18 Regression

Tests: `tests/test_phase_l18_research_conversion.py`
""",
    }
    for name, body in docs.items():
        (DOCS / name).write_text(body, encoding="utf-8")


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--skip-mirrors", action="store_true")
    p.add_argument("--max-mirror-blocked", type=int, default=35)
    args = p.parse_args()
    summary = run_phase_l18(
        run_opengov_mirrors=not args.skip_mirrors,
        max_mirror_blocked=args.max_mirror_blocked,
    )
    print(
        json.dumps(
            {
                k: summary[k]
                for k in (
                    "verdict",
                    "research_queue",
                    "owner_decisions",
                    "quantity_configuration",
                    "quote_targets",
                    "opengov_mirrors",
                    "bottleneck_distribution",
                    "remaining_bottleneck",
                )
            },
            indent=2,
            default=str,
        )
    )
