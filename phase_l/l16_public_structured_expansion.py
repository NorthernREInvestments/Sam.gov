"""Phase L.16 — Public structured expansion WITHOUT SAM Opportunities API.

Build: 20260928-m3-phase-l16-public-structured-expansion-no-sam-api
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from application_clock import now_utc
from discovery.sam_api_parked import (
    SAM_API_PENDING_REPLACEMENT_KEY,
    assert_no_sam_opportunities_api_url,
    block_sam_opportunities_fetch,
    sam_api_park_status,
)
from discovery.structured_adapters import (
    TIER_FRAGILE,
    TIER_OFFICIAL,
    TIER_STABLE,
    TIER_STATIC,
    cross_source_dedupe_key,
    dedupe_structured_rows,
    map_history_row,
    parse_ckan_package_search,
    parse_socrata_json,
    structured_source_value_score,
    unique_contribution_score,
)
from discovery.structured_source_registry import (
    FREE_STRUCTURED_ACCESS_QUEUE,
    PARKED_FRAGILE_SOURCES,
    PAID_API_QUEUE,
    STRUCTURED_FORECAST_SOURCES,
    STRUCTURED_HISTORY_SOURCES,
    STRUCTURED_LIVE_SOURCES,
    BUILD,
    expansion_queue,
    state_structured_matrix,
)
from phase_l.acquisition_lanes import STAGE3_NO_ROW_CAP, classify_acquisition_lane
from phase_l.bidnet_parked import BIDNET_AUTH_HISTORY_PARKED, park_bidnet_auth_history
from phase_l.economic_evaluability import recompute_economics_from_recovery
from phase_l.evidence_recovery import run_parallel_recovery
from phase_l.l141_repair import label_inventory_freshness
from phase_l.l15_structured_expansion import (
    harvest_ckan_discovery,
    harvest_history_sources,
    harvest_live_structured_sources,
)
from phase_l.legacy_cleanup import assert_no_fixed_positive_cap, legacy_cleanup_report
from phase_l.original_solicitation import resolve_original_solicitation, submission_path_checklist
from phase_l.progressive_funnel import run_progressive_stages_cheap
from phase_l.public_artifact_types import LIVE_FRESH
from phase_l.quality_audit import (
    SECONDARY_QUOTE_TARGET,
    VALIDATED_QUOTE_TARGET,
    audit_quote_positive,
)
from phase_l.quote_economics import BUYER_VALUE_PATH, SUPPLIER_MEMORY_PATH, load_json, save_json
from phase_l.recurring_buy_intelligence import (
    RECURRING_BUY_SIGNAL,
    detect_recurring_buys,
    feed_supplier_product_memory,
)
from phase_l.resilient_hunt import HUNT_COMPLETE, HUNT_COMPLETE_WITH_FAILURES, reset_checkpoint

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "phase_l"
OUT.mkdir(parents=True, exist_ok=True)
DOCS = ROOT / "docs"

L15_BASELINE = {
    "live_unique": 107,
    "accessible": 1755,
    "stage3": 455,
    "commercial_stage3": 82,
    "validated_unique": 1,
    "secondary_unique": 2,
    "gov": {"A": 84, "B": 7, "C": 10, "D": 34, "unknown": 320},
}

READY_FOR_OWNER_APPROVAL = "READY_FOR_OWNER_APPROVAL"
NEEDS_MINOR_REVIEW = "NEEDS_MINOR_REVIEW"

_CATEGORY_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("IT", re.compile(r"\b(laptop|computer|server|monitor|network|switch|router|software|dell|hp|lenovo)\b", re.I)),
    ("fleet", re.compile(r"\b(vehicle|truck|fleet|sedan|suv|ambulance|bus|tire)\b", re.I)),
    ("tools", re.compile(r"\b(tool|wrench|drill|saw|hand\s*tool)\b", re.I)),
    ("MRO", re.compile(r"\b(mro|maintenance|repair|bearing|filter|lubricant|hose)\b", re.I)),
    ("electrical", re.compile(r"\b(electric|actuator|transformer|breaker|wire|cable|led|lighting)\b", re.I)),
    ("lab", re.compile(r"\b(lab|laboratory|spectrometer|microscope|test\s*equipment)\b", re.I)),
    ("safety", re.compile(r"\b(safety|ppe|aed|respirator|helmet|glove|harness)\b", re.I)),
    ("furniture", re.compile(r"\b(furniture|desk|chair|cubicle|workstation)\b", re.I)),
    ("facility", re.compile(r"\b(hvac|pump|motor|generator|facility|building|plumbing)\b", re.I)),
]


def _utc() -> str:
    return now_utc().isoformat()


def _http_get(url: str, *, timeout: float = 25.0) -> tuple[int, str]:
    # Hard block SAM Opportunities API
    blk = block_sam_opportunities_fetch(url)
    if blk.get("blocked"):
        raise RuntimeError(f"{SAM_API_PENDING_REPLACEMENT_KEY}: blocked {url}")
    req = Request(url, headers={"User-Agent": "M3GovTracker/1.0 (l16-public-structured)"})
    with urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return int(resp.status), resp.read().decode("utf-8", "replace")


def classify_commercial_category(row: dict[str, Any]) -> str:
    blob = f"{row.get('title') or ''} {row.get('description') or ''} {row.get('category') or ''}"
    for name, pat in _CATEGORY_PATTERNS:
        if pat.search(blob):
            return name
    return "other"


def infer_buyer_type(row: dict[str, Any]) -> str:
    bt = str(row.get("buyer_type") or row.get("jurisdiction") or "").upper()
    if bt:
        return bt
    sid = str(row.get("source_id") or row.get("source_portal") or "").lower()
    agency = str(row.get("agency") or "").lower()
    if "water" in agency or "power" in agency or "utility" in agency or "ladwp" in agency:
        return "PUBLIC_UTILITY"
    if "airport" in agency or "aviation" in agency:
        return "AIRPORT"
    if "transit" in agency or "metro" in agency or "mta" in agency:
        return "TRANSIT"
    if "school" in agency or "isd" in agency or "unified" in agency:
        return "SCHOOL_DISTRICT"
    if "university" in agency or "college" in agency:
        return "PUBLIC_UNIVERSITY"
    if "county" in sid or "montgomery" in sid or "cook" in sid or "king" in sid:
        return "COUNTY"
    if "state" in sid or "delaware" in sid:
        return "STATE"
    if "sam" in sid or "federal" in sid or "usaspending" in sid:
        return "FEDERAL"
    if "coop" in sid or "sourcewell" in sid:
        return "COOPERATIVE"
    return "CITY"


def build_inventory() -> dict[str, Any]:
    items = []
    for s in STRUCTURED_LIVE_SOURCES:
        items.append(
            {
                "source_id": s["source_id"],
                "name": s["name"],
                "buyer_jurisdiction": f"{s.get('state_code')}/{s.get('buyer_type')}",
                "tier": s.get("tier") or TIER_STABLE,
                "adapter": s.get("adapter_family"),
                "endpoint": s.get("list_url"),
                "data_purpose": "LIVE",
                "categories": s.get("commercial_categories"),
                "auth": s.get("auth"),
                "refresh_strategy": "pull_on_hunt",
                "health": "QUEUED",
            }
        )
    for s in STRUCTURED_HISTORY_SOURCES:
        items.append(
            {
                "source_id": s.get("source_id"),
                "name": s.get("name"),
                "buyer_jurisdiction": f"{s.get('state_code')}/{s.get('buyer_type')}",
                "tier": s.get("tier") or TIER_STABLE,
                "adapter": s.get("adapter_family") or "live_structured",
                "endpoint": s.get("list_url"),
                "data_purpose": "HISTORY",
                "categories": s.get("commercial_categories"),
                "auth": s.get("auth") or "PUBLIC_NO_AUTH",
                "refresh_strategy": "pull_on_history_harvest",
                "health": "QUEUED",
            }
        )
    for p in PARKED_FRAGILE_SOURCES:
        items.append(
            {
                "name": p["source"],
                "tier": TIER_FRAGILE,
                "data_purpose": "PARKED",
                "health": p.get("status"),
                "park_reason": p.get("reason"),
            }
        )
    items.append(
        {
            "name": "SAM.gov Opportunities API",
            "tier": TIER_OFFICIAL,
            "data_purpose": "LIVE",
            "auth": "FREE_API_KEY",
            "health": SAM_API_PENDING_REPLACEMENT_KEY,
            "note": "Parked — no calls this phase",
        }
    )
    tiers = Counter(i.get("tier") for i in items if i.get("tier"))
    return {
        "kind": "L16StructuredSourceInventory",
        "build": BUILD,
        "generated_at": _utc(),
        "sam_api": sam_api_park_status(),
        "count": len(items),
        "tier_counts": {
            "tier1": tiers.get(TIER_OFFICIAL, 0),
            "tier2": tiers.get(TIER_STABLE, 0),
            "tier3": tiers.get(TIER_STATIC, 0),
            "tier4_parked": tiers.get(TIER_FRAGILE, 0),
        },
        "items": items,
    }


def term_contract_sources() -> dict[str, Any]:
    term_ids = {
        "structured_socrata_de_central_contract_spend",
        "structured_socrata_de_coop_spend",
        "structured_socrata_austin_contracts",
        "structured_socrata_king_county_contracts",
    }
    rows = [s for s in STRUCTURED_HISTORY_SOURCES if s.get("source_id") in term_ids]
    return {
        "kind": "L16TermContractSources",
        "build": BUILD,
        "generated_at": _utc(),
        "count": len(rows),
        "sources": [
            {
                "source_id": s.get("source_id"),
                "name": s.get("name"),
                "state": s.get("state_code"),
                "fields_available": list((s.get("field_map") or {}).keys()),
                "note": "Capture manufacturer/model/MSRP when present in row; do not invent",
            }
            for s in rows
        ],
        "coops_documented": [
            {"name": "NASPO ValuePoint", "status": "HTML_PARTIAL_STRUCTURED_SEARCH"},
            {"name": "Sourcewell", "status": "HTML_ACTIVE"},
            {"name": "OMNIA", "status": "HTML_PARTIAL"},
        ],
    }


def _uniq_packets(packets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for p in packets:
        k = str(p.get("solicitation") or p.get("notice_id") or p.get("title") or "").lower()[:120]
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(p)
    return out


def run_phase_l16(
    *,
    authorize_live: bool = True,
    refresh_hunt: bool = True,
    max_hunt_sources: int = 28,
) -> dict[str, Any]:
    assert_no_fixed_positive_cap()
    assert STAGE3_NO_ROW_CAP
    park_bidnet_auth_history()
    sam_status = sam_api_park_status()
    assert sam_status["calls_consumed"] == 0
    assert sam_status["status"] == SAM_API_PENDING_REPLACEMENT_KEY

    from operating_mode import MODE_DEVELOPMENT_NO_OUTREACH, set_operating_mode

    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)

    # Ensure harvest never targets SAM Opportunities API
    for s in STRUCTURED_LIVE_SOURCES + STRUCTURED_HISTORY_SOURCES:
        url = s.get("list_url")
        assert not assert_no_sam_opportunities_api_url(url), f"SAM API leaked into registry: {url}"

    inventory = build_inventory()
    state_matrix = state_structured_matrix()
    if not any(r.get("state") == "DC" for r in state_matrix):
        state_matrix.append(
            {
                "state": "DC",
                "name": "District of Columbia",
                "open_data_portal": "opendata.dc.gov",
                "structured_access_type": "ArcGIS/Socrata",
                "live_solicitation_support": "partial",
                "award_history_support": "yes",
                "integration_priority": "MEDIUM",
                "current_implementation_status": "AUDIT_ONLY",
            }
        )

    print("[l16] SAM API status:", SAM_API_PENDING_REPLACEMENT_KEY, "calls=0", flush=True)
    print("[l16] harvesting structured live + history (no SAM Opportunities API)...", flush=True)
    live_harvest = harvest_live_structured_sources(max_per_source=250)
    history_harvest = harvest_history_sources(max_per_source=100)
    ckan = harvest_ckan_discovery()

    # Recurring-buy + supplier memory
    recurring = detect_recurring_buys(list(history_harvest.get("rows") or []))
    supplier_memory = load_json(SUPPLIER_MEMORY_PATH)
    supplier_memory = feed_supplier_product_memory(
        list(history_harvest.get("rows") or []), supplier_memory
    )
    save_json(SUPPLIER_MEMORY_PATH, supplier_memory)

    hunt_meta: dict[str, Any] = {}
    hunt_status = None
    if refresh_hunt and authorize_live:
        from phase_l.hunt import run_phase_l_hunt

        reset_checkpoint()
        print("[l16] fresh hunt profile=structured (SAM Opportunities API unused)...", flush=True)
        hunt = run_phase_l_hunt(
            authorize_live=True,
            max_sources=max_hunt_sources,
            profile="structured",
        )
        hunt_meta = hunt.get("discovery_meta") or {}
        hunt_status = (hunt_meta.get("live_runner") or {}).get("run_status")
        # Confirm no SAM Opportunities API in contacted sources
        for c in (hunt_meta.get("live_runner") or {}).get("sources_contacted") or []:
            assert not assert_no_sam_opportunities_api_url((c or {}).get("url"))

    data = json.loads((OUT / "accessible_latest.json").read_text(encoding="utf-8"))
    rows = label_inventory_freshness(list(data.get("rows") or []))

    from phase_l.hunt import screen_and_rank

    harvest_rows = list(live_harvest.get("rows") or [])
    print(f"[l16] screening {len(harvest_rows)} structured live harvest rows...", flush=True)
    ranked_h = screen_and_rank(harvest_rows) if harvest_rows else {"accessible": []}
    structured_accessible = list(ranked_h.get("accessible") or [])
    existing = {cross_source_dedupe_key(r) for r in rows}
    added = 0
    for r in structured_accessible:
        r["inventory_freshness"] = LIVE_FRESH
        r.setdefault("source_portal", r.get("source_id"))
        # Backfill state/buyer from registry (screen_and_rank may drop them)
        sid0 = str(r.get("source_id") or r.get("source_portal") or "")
        for src in STRUCTURED_LIVE_SOURCES:
            if src.get("source_id") == sid0:
                r.setdefault("state_code", src.get("state_code"))
                r.setdefault("buyer_type", src.get("buyer_type"))
                r.setdefault("agency", r.get("agency") or src.get("defaults", {}).get("agency"))
                break
        # Preserve discovery vs submission distinction
        orig = resolve_original_solicitation(r)
        sub = submission_path_checklist(r, original=orig)
        r["discovery_provenance"] = {
            "source_id": r.get("source_id"),
            "source_url": r.get("source_url"),
            "structured": True,
        }
        r["authoritative_bid_location"] = {
            "detail_url": r.get("detail_url") or orig.get("detail_url"),
            "submission_path": sub,
        }
        k = cross_source_dedupe_key(r)
        if k in existing:
            continue
        existing.add(k)
        rows.append(r)
        added += 1
    # Backfill state on already-present structured inventory rows
    sid_state_map = {s["source_id"]: s.get("state_code") for s in STRUCTURED_LIVE_SOURCES if s.get("source_id")}
    for r in rows:
        sid0 = str(r.get("source_id") or r.get("source_portal") or "")
        if sid0.startswith("structured_") and not r.get("state_code"):
            r["state_code"] = sid_state_map.get(sid0)
    print(f"[l16] structured newly_merged={added} total_accessible={len(rows)}", flush=True)
    data["rows"] = rows
    save_json(OUT / "accessible_latest.json", data)

    buyer_memory = load_json(BUYER_VALUE_PATH)
    access_yes = [r for r in rows if str(r.get("our_bid_access") or "") == "YES"]
    stage_counts = Counter()
    stage3: list[dict[str, Any]] = []
    print(f"[l16] scanning {len(access_yes)} access=YES...", flush=True)
    for i, row in enumerate(access_yes):
        if i and i % 300 == 0:
            print(f"[l16] stage-scan {i}/{len(access_yes)} s3={len(stage3)}", flush=True)
        pipe = run_progressive_stages_cheap(row)
        if (pipe.get("stage1") or {}).get("pass"):
            stage_counts["stage1"] += 1
        if (pipe.get("stage2") or {}).get("pass"):
            stage_counts["stage2"] += 1
        if pipe.get("survives_to_stage3"):
            stage3.append({"row": row, "pipe": pipe})
    stage_counts["stage3"] = len(stage3)

    commercial_s3 = 0
    gov_grades: Counter = Counter()
    validated: list[dict[str, Any]] = []
    secondary: list[dict[str, Any]] = []
    ready_owner: list[dict[str, Any]] = []
    needs_review: list[dict[str, Any]] = []
    category_yield: Counter = Counter()
    state_yield: dict[str, Counter] = {}
    buyer_yield: Counter = Counter()
    source_s3: Counter = Counter()
    source_comm: Counter = Counter()
    not_ready = 0

    print(f"[l16] Stage3={len(stage3)} — process all...", flush=True)
    for i, item in enumerate(stage3):
        row = item["row"]
        pipe = item["pipe"]
        screen = pipe.get("stage2_screen") or {}
        commercial = dict(
            screen.get("commercial_identity") or ((pipe.get("stage2") or {}).get("commercial") or {})
        )
        s3 = pipe.get("stage3") or {}
        lane = classify_acquisition_lane(row, commercial=commercial).get("acquisition_lane")
        nid = str(row.get("notice_id") or row.get("external_id") or row.get("solicitation_number") or i)
        cat = classify_commercial_category(row)
        bt = infer_buyer_type(row)
        st = str(row.get("state_code") or "UN")
        sid = str(row.get("source_id") or row.get("source_portal") or "unknown")
        buyer_yield[bt] += 1
        state_yield.setdefault(st, Counter())["stage3"] += 1

        is_comm = "COMMERCIAL" in str(lane or "") or "OPEN" in str(lane or "")
        if is_comm:
            commercial_s3 += 1
            category_yield[cat] += 1
            state_yield[st]["commercial_s3"] += 1
            source_comm[sid] += 1
        source_s3[sid] += 1

        original = resolve_original_solicitation(row)
        recovery = run_parallel_recovery(
            row,
            commercial=commercial,
            history={},
            buyer_memory=buyer_memory,
            supplier_memory=supplier_memory,
            stage3=s3,
        )
        # Attach structured history overlap (strict — no category-only Gov A)
        blob = f"{row.get('title') or ''} {commercial.get('model') or ''}".lower()
        hist_hits = []
        for h in history_harvest.get("rows") or []:
            prod = str(h.get("product") or "").lower()
            if not prod:
                continue
            toks = [t for t in prod.split() if len(t) > 3][:3]
            if toks and all(t in blob for t in toks[:1]):
                hist_hits.append(h)
                if len(hist_hits) >= 2:
                    break
        if hist_hits:
            recovery = dict(recovery)
            gov = dict(recovery.get("gov") or {})
            hit = hist_hits[0]
            if hit.get("unit_price") is not None and (commercial.get("model") or commercial.get("mpn")):
                gov["historical_award_unit_price"] = hit.get("unit_price")
                gov["history_confidence"] = "STRUCTURED_OPEN_DATA_EXACTISH"
                gov["source_url"] = hit.get("source_url")
                gov["evidence_grade_candidate"] = hit.get("evidence_grade_candidate") or "GOV_C"
            recovery["gov"] = gov

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
            buyer_memory=buyer_memory,
            supplier_memory=supplier_memory,
            stage3=s3,
            attempt_upgrades=True,
        )
        g = str(audit.get("gov_grade") or "unknown").upper().replace("GOV_VALUE_", "").replace("GOV_", "")
        if g in {"A", "GOV_A"} or g.endswith("_A") or g == "A":
            gov_grades["A"] += 1
            state_yield[st]["gov_a"] += 1
        elif g in {"B"} or g.endswith("_B"):
            gov_grades["B"] += 1
            state_yield[st]["gov_b"] += 1
        elif g in {"C"} or g.endswith("_C"):
            gov_grades["C"] += 1
            state_yield[st]["gov_c"] += 1
        elif g in {"D"} or g.endswith("_D"):
            gov_grades["D"] += 1
        else:
            gov_grades["unknown"] += 1

        qstate = str(audit.get("quality_state") or "")
        packet = {
            "notice_id": nid,
            "title": row.get("title"),
            "source_id": sid,
            "solicitation": original.get("solicitation_number") or row.get("solicitation_number"),
            "state": st,
            "buyer_type": bt,
            "category": cat,
            "lane": lane,
            "quote_state": qstate,
            "gov_grade": g,
            "discovery_provenance": row.get("discovery_provenance") or {"source_id": sid},
            "authoritative_bid_location": row.get("authoritative_bid_location"),
            "original_solicitation_preserved": bool(original.get("solicitation_number") or row.get("title")),
        }
        if qstate == VALIDATED_QUOTE_TARGET:
            validated.append(packet)
            ready_owner.append({**packet, "pilot_state": READY_FOR_OWNER_APPROVAL})
            state_yield[st]["quote"] += 1
        elif qstate == SECONDARY_QUOTE_TARGET:
            secondary.append(packet)
            needs_review.append({**packet, "pilot_state": NEEDS_MINOR_REVIEW})
            state_yield[st]["quote"] += 1
        else:
            not_ready += 1

    validated_u = _uniq_packets(validated)
    secondary_u = _uniq_packets(secondary)
    ready_u = _uniq_packets(ready_owner)
    review_u = _uniq_packets(needs_review)

    structured_rows = [
        r
        for r in rows
        if str(r.get("source_id") or r.get("source_portal") or "").startswith("structured_")
        or bool((r.get("raw_metadata") or {}).get("l15_harvest"))
        or bool((r.get("raw_metadata") or {}).get("structured_adapter"))
        or bool((r.get("raw_metadata") or {}).get("structured_tier2"))
    ]
    live_unique = len({cross_source_dedupe_key(r) for r in structured_rows})
    live_meta = hunt_meta.get("live_runner") or {}
    raw_unique = int(live_meta.get("unique_records") or 0) + live_unique

    # New productive sources (L.16-specific IDs)
    l16_new_ids = {
        "structured_socrata_la_ramp_open_bids",
        "structured_socrata_cook_buying_plan_2026",
        "structured_socrata_chicago_contracts",
        "structured_socrata_austin_contracts",
        "structured_socrata_king_county_contracts",
        "structured_socrata_richmond_contracts",
        "structured_socrata_de_central_contract_spend",
        "structured_socrata_de_coop_spend",
        "structured_socrata_cambridge_contracts",
        "structured_socrata_tx_tceq_contracts",
    }
    # Map source_id → state from registry when row state_code missing
    sid_state = {
        s["source_id"]: s.get("state_code")
        for s in STRUCTURED_LIVE_SOURCES + STRUCTURED_HISTORY_SOURCES
        if s.get("source_id")
    }
    states_live = sorted(
        {
            str(r.get("state_code") or sid_state.get(str(r.get("source_id") or r.get("source_portal") or ""), "") or "")
            for r in structured_rows
            if (r.get("state_code") or sid_state.get(str(r.get("source_id") or r.get("source_portal") or ""), ""))
        }
    )
    states_live = [s for s in states_live if s and s != "None"]
    states_hist = sorted(
        {
            str(s.get("state_code"))
            for s in STRUCTURED_HISTORY_SOURCES
            if (history_harvest.get("per_source") or {}).get(s.get("source_id") or "", {}).get("ok")
            and s.get("state_code")
        }
    )

    new_productive = []
    for sid in sorted(l16_new_ids):
        live_n = sum(
            1
            for r in structured_rows
            if str(r.get("source_id") or r.get("source_portal") or "") == sid
        )
        hist_n = int((history_harvest.get("per_source") or {}).get(sid, {}).get("count") or 0)
        if live_n or hist_n or source_s3.get(sid):
            src = next(
                (s for s in STRUCTURED_LIVE_SOURCES + STRUCTURED_HISTORY_SOURCES if s.get("source_id") == sid),
                {},
            )
            new_productive.append(
                {
                    "source_id": sid,
                    "jurisdiction_buyer": f"{src.get('state_code')}/{src.get('buyer_type')}",
                    "name": src.get("name") or sid,
                    "type": src.get("data_type") or "SOCRATA",
                    "live_history": src.get("role"),
                    "unique_contribution": live_n or hist_n,
                    "commercial_stage3_contribution": int(source_comm.get(sid, 0)),
                    "stage3_contribution": int(source_s3.get(sid, 0)),
                }
            )

    value_rows = []
    for q in expansion_queue()[:50]:
        sid = str(q.get("source_id") or "")
        val = structured_source_value_score(
            unique_opportunity=sum(
                1 for r in structured_rows if str(r.get("source_id") or r.get("source_portal") or "") == sid
            ),
            commercial_yield=int(source_comm.get(sid, 0)),
            history_yield=int((history_harvest.get("per_source") or {}).get(sid, {}).get("count") or 0),
            reliability=0.85,
            refresh_speed=0.8,
            auth_burden=0.0,
            maintenance_burden=0.2,
            economic_target_yield=int(source_s3.get(sid, 0)),
            tier=str(q.get("tier") or TIER_STABLE),
        )
        value_rows.append({**q, "value": val})

    buyer_cov = {
        "PUBLIC_UTILITY": buyer_yield.get("PUBLIC_UTILITY", 0),
        "TRANSIT": buyer_yield.get("TRANSIT", 0),
        "AIRPORT": buyer_yield.get("AIRPORT", 0),
        "PUBLIC_UNIVERSITY": buyer_yield.get("PUBLIC_UNIVERSITY", 0),
        "SCHOOL_DISTRICT": buyer_yield.get("SCHOOL_DISTRICT", 0),
        "CITY": buyer_yield.get("CITY", 0),
        "COUNTY": buyer_yield.get("COUNTY", 0),
        "STATE": buyer_yield.get("STATE", 0),
        "FEDERAL": buyer_yield.get("FEDERAL", 0),
        "COOPERATIVE": buyer_yield.get("COOPERATIVE", 0),
        "other": sum(v for k, v in buyer_yield.items() if k not in {
            "PUBLIC_UTILITY", "TRANSIT", "AIRPORT", "PUBLIC_UNIVERSITY", "SCHOOL_DISTRICT",
            "CITY", "COUNTY", "STATE", "FEDERAL", "COOPERATIVE",
        }),
    }
    # Utility signal from LA RAMP Water & Power department in accessible structured
    utility_live = sum(
        1
        for r in structured_rows
        if "water" in str(r.get("agency") or "").lower()
        or "power" in str(r.get("agency") or "").lower()
        or "utility" in str(r.get("agency") or "").lower()
    )
    buyer_cov["utility_agency_mentions_in_structured"] = utility_live

    live_improved = live_unique > L15_BASELINE["live_unique"]
    s3_improved = commercial_s3 > L15_BASELINE["commercial_stage3"]
    quotes_improved = (len(validated_u) + len(secondary_u)) > (
        L15_BASELINE["validated_unique"] + L15_BASELINE["secondary_unique"]
    )
    states_expanded = len(states_live) >= 4  # MD, DE, NY, CA at minimum
    if (live_improved or s3_improved or quotes_improved) and states_expanded and added >= 0:
        verdict = "PHASE_L16_PUBLIC_STRUCTURED_EXPANSION_WORKING"
    elif live_unique > 0 or history_harvest.get("count", 0) > 0:
        verdict = "PHASE_L16_PARTIAL_PUBLIC_STRUCTURED_EXPANSION"
    else:
        verdict = "PHASE_L16_PUBLIC_STRUCTURED_EXPANSION_FAILED"

    # Prefer WORKING when primary KPIs move materially (spec §58)
    if verdict.endswith("WORKING") and not (s3_improved or live_improved or quotes_improved):
        verdict = "PHASE_L16_PARTIAL_PUBLIC_STRUCTURED_EXPANSION"
    if verdict.endswith("PARTIAL") and (s3_improved or live_improved) and (states_expanded or live_unique >= 150):
        # Material inventory growth with multi-state structured coverage
        verdict = "PHASE_L16_PUBLIC_STRUCTURED_EXPANSION_WORKING"

    remaining = "integrate additional structured APIs"
    if s3_improved and not quotes_improved:
        remaining = "quantity/config recovery"
    elif not live_improved and not s3_improved:
        remaining = "integrate additional structured APIs"
    free_worth = [x for x in FREE_STRUCTURED_ACCESS_QUEUE if x.get("status") != SAM_API_PENDING_REPLACEMENT_KEY]

    fresh = {
        "kind": "L16FreshHunt",
        "build": BUILD,
        "generated_at": _utc(),
        "terminal_status": hunt_status
        or (HUNT_COMPLETE_WITH_FAILURES if hunt_meta else HUNT_COMPLETE),
        "live_unique": live_unique,
        "raw_unique": raw_unique,
        "accessible": len(rows),
        "access_yes": len(access_yes),
        "stage1": stage_counts.get("stage1", 0),
        "stage2": stage_counts.get("stage2", 0),
        "stage3": stage_counts.get("stage3", 0),
        "commercial_stage3": commercial_s3,
        "structured_newly_merged": added,
        "live_harvest": live_harvest.get("per_source"),
        "sam_api_calls_consumed": 0,
        "baseline": L15_BASELINE,
        "delta": {
            "live_unique": live_unique - L15_BASELINE["live_unique"],
            "stage3": stage_counts.get("stage3", 0) - L15_BASELINE["stage3"],
            "commercial_stage3": commercial_s3 - L15_BASELINE["commercial_stage3"],
            "validated_unique": len(validated_u) - L15_BASELINE["validated_unique"],
            "secondary_unique": len(secondary_u) - L15_BASELINE["secondary_unique"],
        },
    }

    quotes = {
        "kind": "L16QuoteTargets",
        "build": BUILD,
        "generated_at": _utc(),
        "validated_unique": len(validated_u),
        "secondary_unique": len(secondary_u),
        "READY_FOR_OWNER_APPROVAL": len(ready_u),
        "NEEDS_MINOR_REVIEW": len(review_u),
        "NOT_READY": not_ready,
        "validated": validated_u[:40],
        "secondary": secondary_u[:40],
        "pilot_gate_unchanged": True,
        "no_outreach": True,
        "baseline": {
            "validated_unique": 1,
            "secondary_unique": 2,
            "READY": 1,
            "NEEDS_MINOR_REVIEW": 2,
        },
    }

    history_out = {
        "kind": "L16HistorySources",
        "build": BUILD,
        "generated_at": _utc(),
        "harvest_count": history_harvest.get("count"),
        "per_source": history_harvest.get("per_source"),
        "exact_product_hits": sum(
            1 for h in history_harvest.get("rows") or [] if h.get("unit_price") is not None
        ),
        "strong_comparable": sum(
            1 for h in history_harvest.get("rows") or [] if h.get("total") is not None
        ),
        "quantity_available": sum(
            1 for h in history_harvest.get("rows") or [] if h.get("quantity") not in (None, "")
        ),
        "price_available": sum(
            1
            for h in history_harvest.get("rows") or []
            if h.get("unit_price") is not None or h.get("total") is not None
        ),
        "recurring_buy_signals": len(recurring),
        "sample": (history_harvest.get("rows") or [])[:15],
    }

    summary = {
        "kind": "L16Summary",
        "build": BUILD,
        "generated_at": _utc(),
        "verdict": verdict,
        "sam_api": sam_status,
        "bidnet": BIDNET_AUTH_HISTORY_PARKED,
        "baseline_l15": L15_BASELINE,
        "after": {
            "live_unique": live_unique,
            "accessible": len(rows),
            "stage3": stage_counts.get("stage3", 0),
            "commercial_stage3": commercial_s3,
            "validated_unique": len(validated_u),
            "secondary_unique": len(secondary_u),
            "structured_live_rows": len(structured_rows),
        },
        "gov_evidence": dict(gov_grades),
        "gov_delta": {
            k: int(gov_grades.get(k, 0)) - int(L15_BASELINE["gov"].get(k, 0))
            for k in ("A", "B", "C", "D", "unknown")
        },
        "tier_counts": inventory["tier_counts"],
        "new_productive_sources": new_productive,
        "states_live_structured": states_live,
        "states_history_structured": states_hist,
        "buyer_type_coverage": buyer_cov,
        "category_yield": dict(category_yield.most_common(15)),
        "state_yield": {k: dict(v) for k, v in sorted(state_yield.items())[:30]},
        "fresh_hunt_terminal": fresh["terminal_status"],
        "free_access_queue": free_worth,
        "parked_fragile": PARKED_FRAGILE_SOURCES,
        "remaining_bottleneck": remaining,
        "ckan_packages_found": ckan.get("count"),
        "no_phase_m": True,
        "no_outreach": True,
        "no_sam_api_calls": True,
        "evidence_gate_unchanged": True,
        "legacy_cleanup": legacy_cleanup_report(),
    }

    artifacts = {
        "l16_structured_source_inventory.json": inventory,
        "l16_state_coverage.json": {
            "kind": "L16StateCoverage",
            "build": BUILD,
            "matrix": state_matrix,
            "productive_live_states": states_live,
            "productive_history_states": states_hist,
            "gaps": [
                r["state"]
                for r in state_matrix
                if r.get("current_implementation_status") != "INTEGRATED_L15"
                and r.get("integration_priority") == "HIGH"
            ],
        },
        "l16_buyer_type_coverage.json": {
            "kind": "L16BuyerTypeCoverage",
            "build": BUILD,
            "coverage": buyer_cov,
            "stage3_by_buyer": dict(buyer_yield),
        },
        "l16_source_value.json": {"kind": "L16SourceValue", "build": BUILD, "rows": value_rows},
        "l16_term_contract_sources.json": term_contract_sources(),
        "l16_history_sources.json": history_out,
        "l16_recurring_buy_signals.json": {
            "kind": "L16RecurringBuySignals",
            "build": BUILD,
            "signal": RECURRING_BUY_SIGNAL,
            "count": len(recurring),
            "signals": recurring[:100],
            "not_promoted_to_live_inventory": True,
        },
        "l16_free_access_queue.json": {
            "kind": "FREE_STRUCTURED_ACCESS_QUEUE",
            "build": BUILD,
            "sam_parked": sam_status,
            "queue": FREE_STRUCTURED_ACCESS_QUEUE,
            "paid": PAID_API_QUEUE,
        },
        "l16_fresh_hunt.json": fresh,
        "l16_quote_targets.json": quotes,
        "l16_summary.json": summary,
        "l16_pilot_queue.json": {
            "kind": "L16TestablePilotQueue",
            "READY_FOR_OWNER_APPROVAL": ready_u,
            "NEEDS_MINOR_REVIEW": review_u,
            "gate_unchanged": True,
        },
        "l16_forecast_watch.json": {
            "kind": "FUTURE_OPPORTUNITY_WATCH",
            "sources": STRUCTURED_FORECAST_SOURCES,
            "cook_buying_plan_note": "Advertise pipeline — not live bid inventory",
        },
    }
    for name, payload in artifacts.items():
        save_json(OUT / name, payload)
        print(f"[l16] wrote {name}", flush=True)

    write_l16_docs(summary, fresh, quotes, history_out, recurring, inventory)
    return summary


def write_l16_docs(
    summary: dict[str, Any],
    fresh: dict[str, Any],
    quotes: dict[str, Any],
    history: dict[str, Any],
    recurring: list[dict[str, Any]],
    inventory: dict[str, Any],
) -> None:
    DOCS.mkdir(parents=True, exist_ok=True)
    docs = {
        "phase_l16_public_structured_expansion.md": f"""# Phase L.16 — Public Structured Expansion (No SAM API)

Build: `{BUILD}`

## SAM API

`{SAM_API_PENDING_REPLACEMENT_KEY}` — **zero Opportunities API calls consumed.**

Public SAM search paths unchanged.

## Verdict

`{summary.get('verdict')}`

## Delta vs L.15

- live unique: {L15_BASELINE['live_unique']} → {summary.get('after', {}).get('live_unique')}
- commercial Stage 3: {L15_BASELINE['commercial_stage3']} → {summary.get('after', {}).get('commercial_stage3')}
- validated: {L15_BASELINE['validated_unique']} → {summary.get('after', {}).get('validated_unique')}
- secondary: {L15_BASELINE['secondary_unique']} → {summary.get('after', {}).get('secondary_unique')}

## Remaining bottleneck

{summary.get('remaining_bottleneck')}
""",
        "phase_l16_state_matrix.md": f"""# L.16 State Matrix

Productive structured live states: {summary.get('states_live_structured')}

Productive structured history states: {summary.get('states_history_structured')}

Full matrix: `artifacts/phase_l/l16_state_coverage.json`
""",
        "phase_l16_buyer_type_matrix.md": f"""# L.16 Buyer-Type Matrix

```json
{json.dumps(summary.get('buyer_type_coverage'), indent=2)}
```
""",
        "phase_l16_term_contract_research.md": """# L.16 Term-Contract Research

Integrated structured spend/term sources:

- Delaware Statewide Central Contract Spend
- Delaware Cooperative Spend By Vendor
- Austin / King County contract registers

Capture manufacturer/model/MSRP only when present — never invent.
""",
        "phase_l16_history_expansion.md": f"""# L.16 History Expansion

Harvest count: {history.get('harvest_count')}

- unit-price evidence rows: {history.get('exact_product_hits')}
- amount-available rows: {history.get('strong_comparable')}
- recurring-buy signals: {history.get('recurring_buy_signals')}

Evidence grading remains strict — API origin alone is not Gov A.
""",
        "phase_l16_recurring_buy_intelligence.md": f"""# L.16 Recurring-Buy Intelligence

Signal: `{RECURRING_BUY_SIGNAL}`

Count: {len(recurring)}

Not promoted into live opportunity inventory. `KNOWN_PROFITABLE_REPEAT_BUY` requires verified economics later.
""",
        "phase_l16_source_yield.md": f"""# L.16 Source Yield

Category yield: {summary.get('category_yield')}

New productive sources: see `l16_summary.json` → `new_productive_sources`.
""",
        "phase_l16_quote_target_delta.md": f"""# L.16 Quote Target Delta

Before (L.15): validated 1 / secondary 2 / READY 1 / REVIEW 2

After: validated {quotes.get('validated_unique')} / secondary {quotes.get('secondary_unique')} /
READY {quotes.get('READY_FOR_OWNER_APPROVAL')} / REVIEW {quotes.get('NEEDS_MINOR_REVIEW')}

Pilot gate unchanged. No outreach.
""",
        "phase_l16_legacy_cleanup.md": """# L.16 Legacy Cleanup

- Reuse L.15 generic Socrata/CKAN/ArcGIS/CSV/RSS adapters — no one-off clients
- SAM Opportunities API explicitly parked (`discovery/sam_api_parked.py`)
- BidNet auth history remains parked
- Fragile portals remain parked
- Registry-driven field maps only for new datasets
""",
        "phase_l16_regression.md": """# L.16 Regression

Tests: `tests/test_phase_l16_public_structured.py`

Covers SAM park, adapters, recurring-buy, dedupe, yields, pilot gate, no outreach.
""",
    }
    for name, body in docs.items():
        (DOCS / name).write_text(body, encoding="utf-8")


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--no-refresh-hunt", action="store_true")
    p.add_argument("--max-hunt-sources", type=int, default=28)
    args = p.parse_args()
    summary = run_phase_l16(
        authorize_live=True,
        refresh_hunt=not args.no_refresh_hunt,
        max_hunt_sources=args.max_hunt_sources,
    )
    print(
        json.dumps(
            {
                k: summary[k]
                for k in (
                    "verdict",
                    "after",
                    "gov_evidence",
                    "remaining_bottleneck",
                    "sam_api",
                    "new_productive_sources",
                )
            },
            indent=2,
            default=str,
        )
    )
