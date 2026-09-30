"""Phase I live deal hunt — broad scan → eligibility → score → deep research."""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any

from application_clock import now_utc
from eligibility_gate import (
    ELIGIBLE_CONDITIONAL,
    ELIGIBLE_CONFIRMED,
    ELIGIBILITY_NOT_APPLICABLE,
    NOT_CURRENTLY_ELIGIBLE,
    evaluate_eligibility_gate,
)
from phase_g.live_batch import fetch_live_sam_batch
from phase_g.provenance import PROVENANCE_LIVE_SOURCE, stamp_live_source
from phase_h.deep_research import (
    STATE_BID_DECISION,
    STATE_ELIGIBILITY_ACTION,
    STATE_QUOTE_OUTREACH,
    research_one_phase_h,
)
from phase_i.scoring import deal_hunt_score

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts" / "phase_i"
PHASE_G_RUN = ROOT / "artifacts" / "phase_g" / "live_run_latest.json"

_NSN_RE = re.compile(r"\b\d{4}-\d{2}-\d{3}-\d{4}\b")
_SERVICE_HARD = re.compile(
    r"\b(janitorial|consulting|staffing|construction|design[\-\s]?build|"
    r"rehabilitation|sewer\s+lines?|sources?\s+sought|rfi\b|"
    r"repair\s+of\b|overhaul\s+of\b)\b",
    re.I,
)
_REPAIR_TITLE = re.compile(r"^\s*Repair\s+of\b|\bRepair\s+of\s+[A-Z0-9]", re.I)
_FOOD = re.compile(r"\b(perishable|food\s+service|commissary|meals?\b|produce\b)\b", re.I)
_OEM_MFG = re.compile(r"\bNew\s+Manufactured\s+Material\b", re.I)

# SAM title/keyword pulls aimed at product-resale signals (existing API only)
_PRODUCT_KEYWORDS = [
    "NSN",
    "PARTS KIT",
    "TEST SET",
    "WHEEL ASSEMBLY",
    "CIRCUIT CARD",
    "Power Distribution",
    "BEARING",
    "VALVE",
    "PUMP",
    "FILTER",
    "AMPLIFIER",
    "DISPLAY",
    "TOOL KIT",
    "Hydraulic",
    "CUSHION",
    "TREADMILL",
    "Maintenance Stand",
    "Transmission Kit",
]

ELIGIBLE_PASS = {ELIGIBLE_CONFIRMED, ELIGIBILITY_NOT_APPLICABLE}
ELIGIBLE_MAYBE = {ELIGIBLE_CONDITIONAL}  # rare deep-research if high score


def _utc() -> str:
    return now_utc().isoformat()


def _write(name: str, payload: Any) -> Path:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    path = ARTIFACTS / name
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def _cid(row: dict[str, Any]) -> str:
    return str(
        row.get("canonical_id")
        or row.get("source_opportunity_id")
        or row.get("notice_id")
        or row.get("external_id")
        or ""
    )


def load_phase_g_seed() -> list[dict[str, Any]]:
    if not PHASE_G_RUN.exists():
        return []
    data = json.loads(PHASE_G_RUN.read_text(encoding="utf-8"))
    rows = [r for r in (data.get("all_results") or []) if isinstance(r, dict)]
    for r in rows:
        stamp_live_source(
            r,
            retrieved_at=str(r.get("listing_retrieval_timestamp") or _utc()),
            source_system=str(r.get("source_id") or r.get("source") or "sam.gov"),
        )
    return rows


def fetch_keyword_sam_batch(
    *,
    keywords: list[str],
    days_back: int = 30,
    max_calls: int = 12,
) -> dict[str, Any]:
    """Targeted SAM searches for product-ish titles — read-only, authorized live."""
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:
        pass
    from datetime import timedelta

    import httpx

    from api_budget import can_spend_sam, record_sam_usage
    from discovery.federal_sam_ingest import normalize_sam_opportunity
    from operating_mode import MODE_DEVELOPMENT_NO_OUTREACH, set_operating_mode
    from sam_client import SAM_SEARCH_URL

    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)
    api_key = (os.getenv("SAM_GOV_API_KEY") or "").strip()
    if not api_key:
        return {"opportunities": [], "error": "SAM_GOV_API_KEY_missing", "LIVE_SAM_CALLS": 0}

    from application_clock import today_local

    posted_to = today_local()
    posted_from = posted_to - timedelta(days=days_back)
    collected: list[dict[str, Any]] = []
    seen: set[str] = set()
    calls = 0
    errors: list[str] = []

    with httpx.Client(timeout=60.0) as client:
        for kw in keywords:
            if calls >= max_calls or not can_spend_sam(1):
                break
            params = {
                "api_key": api_key,
                "postedFrom": posted_from.strftime("%m/%d/%Y"),
                "postedTo": posted_to.strftime("%m/%d/%Y"),
                "title": kw,
                "limit": 100,
                "offset": 0,
                "active": "yes",
                "ptype": "o,k",  # solicitation / combined synopsis
            }
            try:
                resp = client.get(SAM_SEARCH_URL, params=params)
                calls += 1
                record_sam_usage(1)
                if resp.status_code != 200:
                    errors.append(f"{kw}:HTTP_{resp.status_code}")
                    continue
                batch = (resp.json() or {}).get("opportunitiesData") or []
                for raw in batch:
                    if not isinstance(raw, dict):
                        continue
                    try:
                        opp = normalize_sam_opportunity(raw)
                    except Exception:
                        continue
                    nid = _cid(opp) or str(raw.get("noticeId") or "")
                    if not nid or nid in seen:
                        continue
                    seen.add(nid)
                    stamp_live_source(opp, retrieved_at=_utc(), source_system="sam.gov")
                    opp["phase_i_keyword"] = kw
                    opp["source_id"] = "fed_sam_keyword_hunt"
                    collected.append(opp)
            except Exception as e:
                errors.append(f"{kw}:{e}"[:120])
                calls += 1

    return {
        "opportunities": collected,
        "LIVE_SAM_CALLS": calls,
        "keywords_used": keywords[:calls],
        "errors": errors,
        "retrieved_at": _utc(),
    }


def merge_inventories(*batches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for batch in batches:
        for r in batch:
            if not isinstance(r, dict):
                continue
            cid = _cid(r)
            if not cid or cid in seen:
                continue
            # Dedupe amendments: prefer later title containing AMENDMENT only if same sol
            seen.add(cid)
            out.append(r)
    return out


def early_hard_reject(row: dict[str, Any]) -> str | None:
    title = str(row.get("title") or "")
    if not title.strip():
        return "no_title"
    if _REPAIR_TITLE.search(title) or _SERVICE_HARD.search(title):
        return "service_construction_or_market_research"
    if _FOOD.search(title):
        return "food_perishable"
    if str(row.get("notice_semantic_class") or "").upper() in {
        "NOTICE_AWARD_OR_HISTORY",
        "NOTICE_MARKET_RESEARCH",
    }:
        return "not_bid_quote_notice"
    blob = title.lower()
    if "award notice" in blob or "justification and approval" in blob:
        return "award_or_ja"
    return None


def cheap_product_screen(row: dict[str, Any]) -> dict[str, Any]:
    """Ultra-cheap product screen only — no owner-gate enrich in Stage B."""
    from discovery.dla_product_extract import classify_federal_product_cheap
    from national_discovery_funnel import stage1_ultra_cheap
    from phase_g.live_batch import PRODUCTISH

    try:
        cheap = stage1_ultra_cheap(row)
        fed = classify_federal_product_cheap(row)
        product_class = str(
            fed.get("cheap_screen_class")
            or fed.get("product_classification")
            or cheap.get("classification")
            or "UNKNOWN"
        )
        survive = bool(cheap.get("survive"))
        is_product = survive and (
            product_class in PRODUCTISH
            or fed.get("federal_product_class") in PRODUCTISH
            or str(fed.get("product_classification") or "") in {"CORE_PRODUCT", "PRODUCT_PLUS_SERVICE"}
            or bool(_NSN_RE.search(str(row.get("title") or "")))
        )
        rejected = (not survive) or (
            str(fed.get("federal_product_class") or "") in {"FEDERAL_SERVICE", "FEDERAL_CONSTRUCTION"}
            and not is_product
        )
        out = dict(row)
        out["product_class"] = product_class
        out["is_product"] = is_product
        out["has_nsn"] = bool(_NSN_RE.search(str(row.get("title") or "")) or row.get("has_nsn"))
        out["is_dla"] = bool(row.get("is_dla") or fed.get("is_dla"))
        return {
            "is_product": is_product and not rejected,
            "rejected": rejected or not is_product,
            "product_class": product_class,
            "row": out,
            "screen": "stage1_ultra_cheap",
        }
    except Exception as e:
        title = str(row.get("title") or "")
        has_nsn = bool(_NSN_RE.search(title))
        is_product = has_nsn or bool(
            re.search(r"\b(PARTS?\s+KIT|TEST\s+SET|ASSEMBLY|VALVE|PUMP|BEARING)\b", title, re.I)
        )
        return {
            "is_product": is_product and not early_hard_reject(row),
            "rejected": not is_product,
            "product_class": "HEURISTIC_PRODUCT" if is_product else "UNKNOWN",
            "row": row,
            "screen": f"heuristic:{e}"[:80],
        }


def eligibility_screen(row: dict[str, Any]) -> dict[str, Any]:
    text = "\n".join(
        str(x)
        for x in (
            row.get("title"),
            row.get("description"),
            row.get("solicitation_text"),
            row.get("typeOfSetAsideDescription"),
        )
        if x
    )
    gate = evaluate_eligibility_gate(row, text=text)
    status = gate.get("overall_status")
    passable = status in ELIGIBLE_PASS
    conditional_ok = status in ELIGIBLE_MAYBE and (deal_hunt_score(row, eligibility_status=status)["deal_hunt_score"] >= 50)
    return {
        "gate": gate,
        "status": status,
        "deep_research_allowed": passable or conditional_ok,
        "blocked": status == NOT_CURRENTLY_ELIGIBLE or (
            gate.get("actionable_for_quote_or_bid") is not True and status not in ELIGIBLE_MAYBE
            and status not in ELIGIBLE_PASS
        ),
    }


def build_owner_packet(result: dict[str, Any]) -> dict[str, Any]:
    op = result.get("phase_h_operator_packet") or {}
    gate = result.get("eligibility_gate") or {}
    hist = result.get("phase_h_history") or {}
    awards = hist.get("awards") or []
    prior = awards[0] if awards else {}
    max_cost = result.get("phase_h_max_supplier_cost") or {}
    max_v = max_cost.get("maximum_allowable_supplier_cost")
    return {
        "kind": "PhaseIOwnerPacket",
        "opportunity": op.get("opportunity") or {},
        "eligibility": {
            "overall_status": gate.get("overall_status"),
            "plain": gate.get("plain"),
            "relevant": (op.get("eligibility") or {}).get("relevant") or [],
        },
        "history": {
            "class": result.get("phase_h_history_class"),
            "prior_award": prior.get("Award ID") or prior.get("award_id"),
            "prior_value": prior.get("Amount") or prior.get("total_obligation_amount"),
            "awardee": prior.get("Recipient Name") or prior.get("recipient_name"),
            "offer_count": prior.get("number_of_offers") or prior.get("Number of Offers") or "UNKNOWN",
            "count": len(awards),
        },
        "market": op.get("market") or {},
        "economics": {
            **(op.get("economics") or {}),
            "target_profit_floor": 10000,
            "max_supplier_cost": max_v,
            "quote_target": (
                f"Delivered supplier cost must be ≤ ${max_v:,.2f} to preserve ~$10,000 target profit."
                if max_v is not None
                else "UNKNOWN — need revenue basis"
            ),
        },
        "compliance": op.get("compliance") or {},
        "financing": op.get("funding") or {},
        "readiness": result.get("phase_h_readiness"),
        "next_action": result.get("phase_h_next_action") or op.get("next_action"),
        "provenance": PROVENANCE_LIVE_SOURCE,
        "generated_at": _utc(),
    }


def run_phase_i_hunt(
    *,
    expand_sam: bool = True,
    keyword_hunt: bool = True,
    sam_expand_calls: int = 20,
    keyword_calls: int = 12,
    days_back: int = 30,
    stage_b_cap: int = 200,
    stage_c_cap: int = 50,
    deep_max: int = 50,
    quote_ready_target: int = 3,
    authorize_live: bool = True,
) -> dict[str, Any]:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:
        pass
    from deep_deal_research import ResearchBudget
    from operating_mode import MODE_DEVELOPMENT_NO_OUTREACH, set_operating_mode

    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)
    costs = {
        "sam_expand_calls": 0,
        "sam_keyword_calls": 0,
        "public_http": 0,
        "usaspending": 0,
        "openai": 0,
        "external_actions": 0,
    }

    # --- Stage A: inventory ---
    seed = load_phase_g_seed()
    expand_rows: list[dict[str, Any]] = []
    keyword_rows: list[dict[str, Any]] = []
    expand_meta: dict[str, Any] = {}
    keyword_meta: dict[str, Any] = {}

    if expand_sam and authorize_live:
        print(f"[phase_i] Stage A expand SAM days_back={days_back} calls<={sam_expand_calls}", flush=True)
        expand_meta = fetch_live_sam_batch(
            target=1500, max_api_calls=sam_expand_calls, days_back=days_back, page_limit=100
        )
        expand_rows = list(expand_meta.get("opportunities") or [])
        costs["sam_expand_calls"] = int(expand_meta.get("LIVE_SAM_CALLS") or 0)
        print(f"  expand got {len(expand_rows)} calls={costs['sam_expand_calls']}", flush=True)

    if keyword_hunt and authorize_live:
        print(f"[phase_i] Stage A keyword hunt calls<={keyword_calls}", flush=True)
        keyword_meta = fetch_keyword_sam_batch(
            keywords=_PRODUCT_KEYWORDS, days_back=days_back, max_calls=keyword_calls
        )
        keyword_rows = list(keyword_meta.get("opportunities") or [])
        costs["sam_keyword_calls"] = int(keyword_meta.get("LIVE_SAM_CALLS") or 0)
        print(f"  keyword got {len(keyword_rows)} calls={costs['sam_keyword_calls']}", flush=True)

    inventory = merge_inventories(seed, expand_rows, keyword_rows)
    print(f"[phase_i] Stage A unique inventory={len(inventory)} (seed={len(seed)})", flush=True)

    # --- Stage B: cheap product + eligibility ---
    stage_b: list[dict[str, Any]] = []
    reject_counts: Counter[str] = Counter()
    elig_counts: Counter[str] = Counter()
    product_counts: Counter[str] = Counter()

    for row in inventory:
        hard = early_hard_reject(row)
        if hard:
            reject_counts[hard] += 1
            continue
        screen = cheap_product_screen(row)
        product_counts[str(screen.get("product_class") or "UNKNOWN")] += 1
        if not screen.get("is_product") or screen.get("rejected"):
            reject_counts["not_product"] += 1
            continue
        merged = screen.get("row") or row
        elig = eligibility_screen(merged)
        elig_counts[str(elig.get("status") or "UNKNOWN")] += 1
        if not elig.get("deep_research_allowed"):
            reject_counts[f"eligibility:{elig.get('status')}"] += 1
            continue
        scored = deal_hunt_score(merged, eligibility_status=elig.get("status"))
        stage_b.append(
            {
                **merged,
                "phase_i_eligibility": elig.get("gate"),
                "phase_i_eligibility_status": elig.get("status"),
                **scored,
                "phase_i_stage": "B",
            }
        )

    stage_b.sort(key=lambda r: (-int(r.get("deal_hunt_score") or 0), _cid(r)))
    stage_b = stage_b[:stage_b_cap]
    print(f"[phase_i] Stage B survivors={len(stage_b)}", flush=True)

    # --- Stage C: top scored ---
    stage_c = stage_b[:stage_c_cap]
    for r in stage_c:
        r["phase_i_stage"] = "C"
    print(f"[phase_i] Stage C research candidates={len(stage_c)}", flush=True)
    _write(
        "stage_c_candidates.json",
        {
            "count": len(stage_c),
            "candidates": [
                {
                    "canonical_id": _cid(r),
                    "title": r.get("title"),
                    "score": r.get("deal_hunt_score"),
                    "reasons": r.get("deal_hunt_reasons"),
                    "eligibility": r.get("phase_i_eligibility_status"),
                    "nsn": bool(r.get("has_nsn")),
                }
                for r in stage_c
            ],
        },
    )

    # --- Stage D: deep research ---
    budget = ResearchBudget(
        public_http_hard_max=120,
        public_http_target=100,
        usaspending_hard_max=60,
        sam_hard_max=0,
    )
    counter = {"usaspending": 0, "usaspending_max": 60}
    deep_results: list[dict[str, Any]] = []
    quote_ready: list[dict[str, Any]] = []
    bid_ready: list[dict[str, Any]] = []
    stop_reason = None

    for i, row in enumerate(stage_c, 1):
        if len(quote_ready) >= quote_ready_target:
            stop_reason = f"quote_ready_target_{quote_ready_target}_met"
            break
        if i > deep_max:
            stop_reason = "deep_max_reached"
            break
        if not budget.can_http() and counter["usaspending"] >= counter["usaspending_max"]:
            stop_reason = "research_budget_exhausted"
            break
        title = (str(row.get("title") or ""))[:70]
        print(f"[phase_i] Stage D {i}/{min(len(stage_c), deep_max)} {title}", flush=True)
        try:
            packet = research_one_phase_h(
                row, budget=budget, budget_counter=counter, authorize_live=authorize_live
            )
        except Exception as e:
            print(f"  -> ERROR {e}", flush=True)
            packet = {
                "phase_h_readiness": "BLOCKED",
                "error": str(e)[:300],
                "phase_h_source_row": {"canonical_id": _cid(row), "title": row.get("title")},
                "phase_h_history_class": "NO_HISTORY_FOUND",
                "phase_h_identity_confidence": "UNKNOWN",
                "eligibility_gate": row.get("phase_i_eligibility"),
            }
        packet["phase_i_deal_hunt_score"] = row.get("deal_hunt_score")
        packet["phase_i_stage"] = "D"
        readiness = packet.get("phase_h_readiness")
        print(
            f"  -> {readiness} hist={packet.get('phase_h_history_class')} "
            f"id={packet.get('phase_h_identity_confidence')} "
            f"elig={(packet.get('eligibility_gate') or {}).get('overall_status')}",
            flush=True,
        )
        if readiness == STATE_QUOTE_OUTREACH:
            packet["phase_i_owner_packet"] = build_owner_packet(packet)
            quote_ready.append(packet)
        elif readiness == STATE_BID_DECISION:
            packet["phase_i_owner_packet"] = build_owner_packet(packet)
            bid_ready.append(packet)
        deep_results.append(packet)

    costs["public_http"] = int((budget.to_dict() or {}).get("public_http") or 0)
    costs["usaspending"] = int(counter.get("usaspending") or 0)
    if stop_reason is None:
        if quote_ready:
            stop_reason = "deep_queue_exhausted_with_quote_ready"
        else:
            stop_reason = "deep_queue_exhausted_no_quote_ready"

    readiness_counts = Counter(r.get("phase_h_readiness") for r in deep_results)
    history_counts = Counter(r.get("phase_h_history_class") for r in deep_results)
    identity_counts = Counter(r.get("phase_h_identity_confidence") for r in deep_results)
    economics_counts = Counter(r.get("phase_h_economics_state") for r in deep_results)
    funding_counts = Counter(r.get("phase_h_funding_state") for r in deep_results)
    deep_elig = Counter((r.get("eligibility_gate") or {}).get("overall_status") for r in deep_results)

    # Best rejected / near misses for report
    near = sorted(
        [
            r
            for r in deep_results
            if r.get("phase_h_readiness")
            not in {STATE_QUOTE_OUTREACH, STATE_BID_DECISION}
        ],
        key=lambda r: (
            0 if r.get("phase_h_history_class") in {"STRONG_HISTORY", "MODERATE_HISTORY"} else 1,
            0 if r.get("phase_h_identity_confidence") in {"EXACT_CONFIRMED", "STRONG_MATCH"} else 1,
            str((r.get("phase_h_source_row") or {}).get("title") or ""),
        ),
    )[:10]

    classification = "NO EXECUTABLE LIVE DEAL FOUND"
    if quote_ready or bid_ready:
        classification = "EXECUTABLE LIVE DEAL FOUND"
    elif any(
        (
            r.get("phase_h_identity_confidence") in {"EXACT_CONFIRMED", "STRONG_MATCH"}
            and r.get("phase_h_history_class") in {"STRONG_HISTORY", "MODERATE_HISTORY"}
        )
        or (
            r.get("phase_h_identity_confidence") in {"EXACT_CONFIRMED", "STRONG_MATCH"}
            and r.get("phase_h_economics_state") == "ECONOMICS_PROMISING_QUOTE_REQUIRED"
        )
        or (
            r.get("phase_h_history_class") in {"STRONG_HISTORY", "MODERATE_HISTORY"}
            and r.get("phase_h_identity_confidence") in {"UNKNOWN", "PARTIAL"}
        )
        for r in deep_results
    ):
        classification = "PROMISING LIVE DEALS FOUND — MORE EVIDENCE REQUIRED"

    scorecard = {
        "kind": "PhaseIScorecard",
        "classification": classification,
        "stage_a_scanned": len(inventory),
        "stage_a_seed": len(seed),
        "stage_a_expand": len(expand_rows),
        "stage_a_keyword": len(keyword_rows),
        "stage_b_survivors": len(stage_b),
        "stage_c_candidates": len(stage_c),
        "stage_d_deep_researched": len(deep_results),
        "READY_FOR_QUOTE_OUTREACH": len(quote_ready),
        "READY_FOR_BID_DECISION": len(bid_ready),
        "ELIGIBILITY_ACTION_REQUIRED": readiness_counts.get(STATE_ELIGIBILITY_ACTION, 0),
        "eligibility_funnel": dict(elig_counts),
        "product_class_counts": dict(product_counts),
        "early_reject_counts": dict(reject_counts),
        "deep_eligibility_counts": dict(deep_elig),
        "readiness_counts": dict(readiness_counts),
        "identity_counts": dict(identity_counts),
        "history_counts": dict(history_counts),
        "economics_counts": dict(economics_counts),
        "funding_counts": dict(funding_counts),
        "costs": costs,
        "stop_reason": stop_reason,
        "DEVELOPMENT_NO_OUTREACH": True,
        "generated_at": _utc(),
    }

    payload = {
        "kind": "PhaseIHuntRun",
        "scorecard": scorecard,
        "quote_ready": [
            {
                "title": (r.get("phase_h_source_row") or {}).get("title"),
                "canonical_id": (r.get("phase_h_source_row") or {}).get("canonical_id"),
                "nsn": r.get("phase_h_nsn"),
                "readiness": r.get("phase_h_readiness"),
                "eligibility": (r.get("eligibility_gate") or {}).get("overall_status"),
                "max_supplier_cost": (r.get("phase_h_max_supplier_cost") or {}).get(
                    "maximum_allowable_supplier_cost"
                ),
                "next_action": r.get("phase_h_next_action"),
                "owner_packet": r.get("phase_i_owner_packet"),
            }
            for r in quote_ready
        ],
        "bid_ready": [
            {
                "title": (r.get("phase_h_source_row") or {}).get("title"),
                "canonical_id": (r.get("phase_h_source_row") or {}).get("canonical_id"),
                "owner_packet": r.get("phase_i_owner_packet"),
            }
            for r in bid_ready
        ],
        "near_misses": [
            {
                "title": (r.get("phase_h_source_row") or {}).get("title"),
                "readiness": r.get("phase_h_readiness"),
                "identity": r.get("phase_h_identity_confidence"),
                "history": r.get("phase_h_history_class"),
                "economics": r.get("phase_h_economics_state"),
                "eligibility": (r.get("eligibility_gate") or {}).get("overall_status"),
                "next_action": r.get("phase_h_next_action")
                or (r.get("phase_h_operator_packet") or {}).get("next_action"),
            }
            for r in near
        ],
        "deep_results": deep_results,
        "expand_meta": {k: v for k, v in expand_meta.items() if k != "opportunities"},
        "keyword_meta": {k: v for k, v in keyword_meta.items() if k != "opportunities"},
    }

    _write("hunt_latest.json", payload)
    _write("scorecard_latest.json", scorecard)
    _write(
        "quote_ready_latest.json",
        {"count": len(quote_ready), "cases": payload["quote_ready"]},
    )
    _write(
        "owner_packets.json",
        {
            "count": len(quote_ready) + len(bid_ready),
            "packets": [r.get("phase_i_owner_packet") for r in quote_ready + bid_ready],
        },
    )
    print(
        f"[phase_i] DONE classification={classification} quote={len(quote_ready)} "
        f"bid={len(bid_ready)} stop={stop_reason}",
        flush=True,
    )
    return payload
