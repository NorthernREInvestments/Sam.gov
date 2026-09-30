"""Phase L.17.3 — Platform adapter expansion + NONFEDERAL_ACCESSIBLE_NOW growth."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from application_clock import now_utc
from discovery.coverage_saturation import run_saturation_pass
from discovery.lower48 import (
    DIBBS_CAGE_REQUIRED,
    FREE_REGISTRATION_REQUIRED,
    NONFEDERAL_ACCESSIBLE_NOW,
    REGISTER_BEFORE_BID,
    REGISTER_NOW_RECURRING_BUYER,
    classify_opportunity_access,
    current_access_score,
    is_nonfederal_accessible_now,
    registration_unlock_score,
)
from discovery.platform_adapters import (
    ADAPTERS,
    ANTI_BOT,
    AUTH_REQUIRED_TO_VIEW,
    BUILD,
    FREE_ACCOUNT_TO_BID,
    HIGH_LEVERAGE_PLATFORM_REGISTRATION,
    INGESTION_ACTIVE,
    PUBLIC_LISTING_AUTOMATABLE,
    platform_inventory,
    run_platform_backfill,
)
from discovery.sam_api_parked import SAM_API_PENDING_REPLACEMENT_KEY, sam_api_park_status
from discovery.structured_adapters import cross_source_dedupe_key
from phase_l.acquisition_lanes import STAGE3_NO_ROW_CAP, classify_acquisition_lane
from phase_l.bidnet_parked import BIDNET_AUTH_HISTORY_PARKED, park_bidnet_auth_history
from phase_l.economic_evaluability import recompute_economics_from_recovery
from phase_l.evidence_recovery import run_parallel_recovery
from phase_l.l141_repair import label_inventory_freshness
from phase_l.legacy_cleanup import assert_no_fixed_positive_cap, legacy_cleanup_report
from phase_l.original_solicitation import resolve_original_solicitation, submission_path_checklist
from phase_l.progressive_funnel import run_progressive_stages_cheap
from phase_l.quality_audit import (
    SECONDARY_QUOTE_TARGET,
    VALIDATED_QUOTE_TARGET,
    audit_quote_positive,
)
from phase_l.quote_economics import BUYER_VALUE_PATH, SUPPLIER_MEMORY_PATH, load_json, save_json
from phase_l.resilient_hunt import HUNT_COMPLETE_WITH_FAILURES, reset_checkpoint

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "phase_l"
OUT.mkdir(parents=True, exist_ok=True)
DOCS = ROOT / "docs"

L172_BASELINE = {
    "baseline_source": "L172",
    "unknown_jurisdictions": 22120,
    "counties_mapped": 157,
    "municipalities_mapped": 140,
    "mapped_total": 403,
    "live_unique": 243,
    "accessible": 2944,
    "stage3": 699,
    "commercial_stage3": 138,
    "NONFEDERAL_ACCESSIBLE_NOW": 48,
    "validated_unique": 1,
    "secondary_unique": 2,
    "READY_TO_RESEARCH_NOW": 0,
}

READY_FOR_OWNER_APPROVAL = "READY_FOR_OWNER_APPROVAL"
NEEDS_MINOR_REVIEW = "NEEDS_MINOR_REVIEW"
READY_TO_RESEARCH_NOW = "READY_TO_RESEARCH_NOW"
REGISTER_TO_UNLOCK = "REGISTER_TO_UNLOCK"


def _utc() -> str:
    return now_utc().isoformat()


def _uniq(packets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for p in packets:
        k = str(p.get("solicitation") or p.get("notice_id") or p.get("title") or "").lower()[:120]
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(p)
    return out


def diagnose_ready_to_research(accessible_now: list[dict[str, Any]], quotes: dict[str, Any]) -> dict[str, Any]:
    """§45 — diagnose why READY_TO_RESEARCH_NOW was 0 in L.17.2."""
    return {
        "prior_baseline": 0,
        "root_cause": (
            "L.17.2 routed ALL free-registration accessible-now rows exclusively into "
            "REGISTER_TO_UNLOCK, so READY_TO_RESEARCH_NOW stayed empty even though free/simple "
            "registration is an execution step, not a research blocker."
        ),
        "repair": (
            "L.17.3 places accessible-now tangible rows needing economics/supplier work into "
            "READY_TO_RESEARCH_NOW regardless of free-reg; REGISTER_TO_UNLOCK remains a parallel "
            "registration-action queue (platform-level unlocks ranked higher)."
        ),
        "accessible_now_input": len(accessible_now),
        "quote_ready_excluded": int(quotes.get("READY_FOR_OWNER_APPROVAL") or 0)
        + int(quotes.get("NEEDS_MINOR_REVIEW") or 0),
        "standards_unchanged": True,
    }


def harvest_bidnet_networks(*, max_states: int = 18) -> dict[str, Any]:
    from discovery.bidnet_network import all_bidnet_networks_enriched
    from discovery.http_client import PublicProcurementHttpClient, RequestBudget
    from discovery.live_fetchers import BidNetLiveFetcher
    from phase_l.hunt import screen_and_rank

    priority = {
        "TX", "GA", "VA", "IL", "FL", "CA", "NY", "OH", "NC", "MO", "KY", "IA", "KS",
        "WA", "OR", "MN", "WI", "MI", "AZ", "CO", "PA", "TN",
    }
    nets = [n for n in all_bidnet_networks_enriched() if n.get("state_code") in priority][:max_states]
    fetcher = BidNetLiveFetcher()
    client = PublicProcurementHttpClient(
        authorize_live=True,
        budget=RequestBudget(max_total_requests=400, max_requests_per_source=4),
    )
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for n in nets:
        url = n.get("list_url")
        sid = n.get("source_id")
        try:
            raw = fetcher.fetch_listing(client, list_url=url, source_id=sid, max_pages=1)
            opps = raw.get("opportunities") or []
            for o in opps:
                d = o.to_dict() if hasattr(o, "to_dict") else dict(o)
                d["source_id"] = sid
                d["source_portal"] = sid
                d["platform_family"] = "BidNet"
                d["state_code"] = n.get("state_code")
                d["jurisdiction"] = d.get("jurisdiction") or "LOCAL"
                d["registration_action"] = REGISTER_BEFORE_BID
                d["our_bid_access"] = "YES"
                d["document_access"] = "DOCS_FREE_REGISTRATION"
                d["l173_platform_harvest"] = True
                rows.append(d)
        except Exception as e:
            errors.append({"source_id": sid, "error": str(e)[:200]})
    ranked = screen_and_rank(rows) if rows else {"accessible": []}
    return {
        "rows": ranked.get("accessible") or rows,
        "raw_count": len(rows),
        "networks": len(nets),
        "errors": errors,
    }


def attach_chain(row: dict[str, Any]) -> dict[str, Any]:
    orig = resolve_original_solicitation(row)
    sub = submission_path_checklist(row, original=orig)
    row.setdefault(
        "discovery_provenance",
        {"source_id": row.get("source_id"), "source_url": row.get("source_url")},
    )
    row["authoritative_bid_location"] = {
        "detail_url": row.get("detail_url") or orig.get("detail_url"),
        "submission_path": sub,
        "solicitation_id": orig.get("solicitation_number") or row.get("solicitation_number"),
        "buyer": row.get("agency") or row.get("buyer"),
    }
    return row


def run_phase_l173(
    *,
    authorize_live: bool = True,
    refresh_hunt: bool = True,
    max_hunt_sources: int = 28,
    probe_counties: bool = True,
    probe_max_counties: int = 200,
) -> dict[str, Any]:
    assert_no_fixed_positive_cap()
    assert STAGE3_NO_ROW_CAP
    park_bidnet_auth_history()
    sam = sam_api_park_status()
    assert sam["calls_consumed"] == 0

    from operating_mode import MODE_DEVELOPMENT_NO_OUTREACH, set_operating_mode

    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)

    inventory = platform_inventory()
    print("[l173] platform inventory ready", flush=True)

    # Continue county enrichment (resume-safe)
    print("[l173] county enrichment pass...", flush=True)
    sat = run_saturation_pass(
        probe=probe_counties,
        probe_max_counties=probe_max_counties if probe_counties else 0,
    )
    reduction = sat["reduction"]
    print(
        f"[l173] unknown {reduction['unknown_before']} -> {reduction['unknown_after']} "
        f"counties_mapped={reduction['counties_mapped']}",
        flush=True,
    )

    # Platform backfills
    platform_results: dict[str, Any] = {}
    harvested_rows: list[dict[str, Any]] = []
    for plat in ("OpenGov", "Bonfire", "PlanetBids", "IonWave", "SimpleHTML"):
        print(f"[l173] backfill {plat}...", flush=True)
        # OpenGov: all mapped; others capped for stop-loss / DNS fragility
        cap = None if plat == "OpenGov" else (15 if plat != "SimpleHTML" else 22)
        try:
            res = run_platform_backfill(plat, max_jurisdictions=cap, resume=True)
        except Exception as e:
            res = {
                "platform": plat,
                "mapped": 0,
                "tested": 0,
                "error": str(e)[:240],
                "rows": [],
                "access_state_counts": {"ADAPTER_FAILED": 1},
                "activation_state_counts": {},
                "ingestion_active": 0,
                "live_rows": 0,
            }
        platform_results[plat] = {k: v for k, v in res.items() if k != "rows"}
        harvested_rows.extend(res.get("rows") or [])
        print(
            f"[l173] {plat}: tested={res.get('tested')} active={res.get('ingestion_active')} "
            f"rows={res.get('live_rows')} states={res.get('access_state_counts')}",
            flush=True,
        )

    # Structured + BidNet harvest
    from phase_l.hunt import screen_and_rank
    from phase_l.l15_structured_expansion import harvest_live_structured_sources

    live_harvest = harvest_live_structured_sources(max_per_source=250)
    print("[l173] BidNet public network harvest...", flush=True)
    bidnet_h = harvest_bidnet_networks(max_states=18)

    hunt_status = None
    if refresh_hunt and authorize_live:
        from phase_l.hunt import run_phase_l_hunt

        reset_checkpoint()
        print("[l173] fresh hunt profile=structured (no SAM Opportunities API)...", flush=True)
        hunt = run_phase_l_hunt(authorize_live=True, max_sources=max_hunt_sources, profile="structured")
        hunt_status = ((hunt.get("discovery_meta") or {}).get("live_runner") or {}).get("run_status")

    data = json.loads((OUT / "accessible_latest.json").read_text(encoding="utf-8"))
    rows = label_inventory_freshness(list(data.get("rows") or []))
    existing = {cross_source_dedupe_key(r) for r in rows}
    added = 0
    for batch in (
        harvested_rows,
        live_harvest.get("rows") or [],
        bidnet_h.get("rows") or [],
    ):
        ranked = screen_and_rank(batch) if batch else {"accessible": []}
        for r in ranked.get("accessible") or batch:
            attach_chain(r)
            k = cross_source_dedupe_key(r)
            if k in existing:
                continue
            existing.add(k)
            rows.append(r)
            added += 1
    for r in rows:
        attach_chain(r)
    data["rows"] = rows
    save_json(OUT / "accessible_latest.json", data)
    print(f"[l173] newly_merged={added} accessible={len(rows)}", flush=True)

    buyer_memory = load_json(BUYER_VALUE_PATH)
    supplier_memory = load_json(SUPPLIER_MEMORY_PATH)
    access_yes = [
        r
        for r in rows
        if str(r.get("our_bid_access") or "").upper() in {"YES", "CONDITIONAL", "REGISTER"}
    ]

    stage_counts: Counter = Counter()
    stage3: list[dict[str, Any]] = []
    print(f"[l173] scanning {len(access_yes)} access-eligible...", flush=True)
    for i, row in enumerate(access_yes):
        if i and i % 400 == 0:
            print(f"[l173] stage-scan {i}/{len(access_yes)} s3={len(stage3)}", flush=True)
        pipe = run_progressive_stages_cheap(row)
        if (pipe.get("stage1") or {}).get("pass"):
            stage_counts["stage1"] += 1
        if (pipe.get("stage2") or {}).get("pass"):
            stage_counts["stage2"] += 1
        if pipe.get("survives_to_stage3"):
            stage3.append({"row": row, "pipe": pipe})
    stage_counts["stage3"] = len(stage3)

    commercial_s3 = 0
    accessible_now: list[dict[str, Any]] = []
    ready_research: list[dict[str, Any]] = []
    register_unlock: list[dict[str, Any]] = []
    validated: list[dict[str, Any]] = []
    secondary: list[dict[str, Any]] = []
    ready_owner: list[dict[str, Any]] = []
    needs_review: list[dict[str, Any]] = []
    not_ready = 0
    source_prod: dict[str, Counter] = {}
    plat_prod: dict[str, Counter] = {}

    print(f"[l173] Stage3={len(stage3)} - process all...", flush=True)
    for item in stage3:
        row = item["row"]
        pipe = item["pipe"]
        screen = pipe.get("stage2_screen") or {}
        commercial = dict(
            screen.get("commercial_identity") or ((pipe.get("stage2") or {}).get("commercial") or {})
        )
        s3 = pipe.get("stage3") or {}
        lane = classify_acquisition_lane(row, commercial=commercial).get("acquisition_lane")
        is_commercial = "COMMERCIAL" in str(lane or "") or "OPEN" in str(lane or "")
        if is_commercial:
            commercial_s3 += 1

        access = current_access_score(row)
        opp_access = classify_opportunity_access(row)
        nf_ok = access["label"] == NONFEDERAL_ACCESSIBLE_NOW
        sid = str(row.get("source_id") or row.get("source_portal") or "unknown")
        plat = str(row.get("platform_family") or "unknown")
        source_prod.setdefault(sid, Counter())
        plat_prod.setdefault(plat, Counter())
        source_prod[sid]["stage3"] += 1
        plat_prod[plat]["stage3"] += 1
        if is_commercial:
            source_prod[sid]["commercial_stage3"] += 1
            plat_prod[plat]["commercial_stage3"] += 1

        original = resolve_original_solicitation(row)
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
            buyer_memory=buyer_memory,
            supplier_memory=supplier_memory,
            stage3=s3,
            attempt_upgrades=True,
        )
        qstate = str(audit.get("quality_state") or "")
        suppliers_block = econ.get("suppliers")
        if isinstance(suppliers_block, list):
            supplier_status = (suppliers_block[0] or {}).get("status") if suppliers_block else None
        elif isinstance(suppliers_block, dict):
            supplier_status = suppliers_block.get("status")
        else:
            supplier_status = None
        max_buy = econ.get("max_buy") if isinstance(econ.get("max_buy"), dict) else {}
        gov = econ.get("government_value") if isinstance(econ.get("government_value"), dict) else {}

        needs_reg = opp_access in {FREE_REGISTRATION_REQUIRED, "SIMPLE_VENDOR_SETUP"} or bool(
            row.get("registration_action")
        )
        packet = {
            "notice_id": row.get("notice_id") or row.get("external_id"),
            "title": row.get("title"),
            "buyer": row.get("agency") or row.get("buyer"),
            "state": row.get("state_code"),
            "solicitation": original.get("solicitation_number") or row.get("solicitation_number"),
            "product": row.get("title"),
            "deadline": row.get("response_deadline") or row.get("deadline"),
            "platform": plat,
            "authoritative_posting": (row.get("authoritative_bid_location") or {}).get("detail_url"),
            "submission_location": row.get("authoritative_bid_location"),
            "registration_required": needs_reg,
            "current_access_status": opp_access,
            "gov_evidence": audit.get("gov_grade"),
            "estimated_value": gov.get("value") or row.get("estimated_value"),
            "quantity_status": (econ.get("quantity") or {}).get("status")
            if isinstance(econ.get("quantity"), dict)
            else None,
            "supplier_status": supplier_status,
            "economics_status": max_buy.get("status") or qstate,
            "next_action": (
                "COMPLETE_FREE_REGISTRATION"
                if needs_reg
                else ("OWNER_APPROVAL" if qstate == VALIDATED_QUOTE_TARGET else "RESEARCH_ECONOMICS_SUPPLIER")
            ),
            "remaining_blocker": audit.get("primary_blocker") or audit.get("blocker"),
            "nonfederal_accessible_now": nf_ok,
            "CurrentAccessScore": access["CurrentAccessScore"],
            "source_id": sid,
            "quote_state": qstate,
            "jurisdiction_registry_id": row.get("jurisdiction_registry_id"),
            "document_access": row.get("document_access"),
            "discovery_provenance": row.get("discovery_provenance"),
        }

        tangible = bool((pipe.get("stage2") or {}).get("pass"))
        if nf_ok and tangible:
            accessible_now.append(packet)
            source_prod[sid]["accessible_now"] += 1
            plat_prod[plat]["accessible_now"] += 1
            # READY_TO_RESEARCH: accessible-now needing economics — free-reg does NOT exclude
            if qstate not in {VALIDATED_QUOTE_TARGET, SECONDARY_QUOTE_TARGET}:
                ready_research.append({**packet, "queue": READY_TO_RESEARCH_NOW})
            if needs_reg:
                register_unlock.append({**packet, "queue": REGISTER_TO_UNLOCK})

        if qstate == VALIDATED_QUOTE_TARGET:
            validated.append(packet)
            ready_owner.append({**packet, "pilot_state": READY_FOR_OWNER_APPROVAL})
        elif qstate == SECONDARY_QUOTE_TARGET:
            secondary.append(packet)
            needs_review.append({**packet, "pilot_state": NEEDS_MINOR_REVIEW})
        else:
            not_ready += 1

    validated_u = _uniq(validated)
    secondary_u = _uniq(secondary)
    ready_u = _uniq(ready_owner)
    review_u = _uniq(needs_review)
    accessible_now_u = _uniq(accessible_now)
    research_u = _uniq(ready_research)
    unlock_u = _uniq(register_unlock)

    structured = [
        r
        for r in rows
        if str(r.get("source_id") or r.get("source_portal") or "").startswith("structured_")
        or str(r.get("source_id") or "").startswith("platform_")
        or str(r.get("source_id") or "").startswith("network_bidnet")
        or bool(r.get("l173_platform_harvest"))
        or bool((r.get("raw_metadata") or {}).get("structured_adapter"))
        or bool((r.get("raw_metadata") or {}).get("l15_harvest"))
    ]
    live_unique = len({cross_source_dedupe_key(r) for r in structured})

    # Registration unlocks (platform-level ranked higher)
    reg_rows = []
    for plat, c in sorted(plat_prod.items(), key=lambda x: -int(x[1].get("accessible_now") or 0)):
        mapped = next(
            (p["mapped_jurisdictions"] for p in inventory["platforms"] if p["platform"] == plat),
            0,
        )
        score = registration_unlock_score(
            buyers_unlocked=max(mapped, 1),
            open_opportunities=int(c.get("stage3") or 0),
            product_opportunities=int(c.get("accessible_now") or 0),
            recurring=plat in {"BidNet", "OpenGov", "Bonfire", "PlanetBids"},
            cost=0.0,
            setup_burden="easy",
        )
        reg_rows.append(
            {
                "platform": plat,
                "flag": HIGH_LEVERAGE_PLATFORM_REGISTRATION if mapped >= 20 else None,
                "jurisdictions_unlocked": mapped,
                "live_stage3": int(c.get("stage3") or 0),
                "accessible_now": int(c.get("accessible_now") or 0),
                "commercial_stage3": int(c.get("commercial_stage3") or 0),
                "estimated_owner_effort": "15-30min free account" if plat == "BidNet" else "varies",
                "action": REGISTER_NOW_RECURRING_BUYER if mapped >= 20 else REGISTER_BEFORE_BID,
                **score,
            }
        )
    reg_rows.sort(key=lambda x: -float(x.get("RegistrationUnlockScore") or 0))

    diagnosis = diagnose_ready_to_research(
        accessible_now_u,
        {"READY_FOR_OWNER_APPROVAL": len(ready_u), "NEEDS_MINOR_REVIEW": len(review_u)},
    )

    # Verdict
    access_up = len(accessible_now_u) > L172_BASELINE["NONFEDERAL_ACCESSIBLE_NOW"]
    commercial_up = commercial_s3 >= L172_BASELINE["commercial_stage3"]
    og = platform_results.get("OpenGov") or {}
    og_active = int(og.get("ingestion_active") or 0) >= 1 or int(og.get("live_rows") or 0) > 0
    any_platform_active = any(int((platform_results.get(p) or {}).get("ingestion_active") or 0) > 0 for p in ADAPTERS)
    research_fixed = len(research_u) > 0
    unknown_down = reduction["unknown_after"] < L172_BASELINE["unknown_jurisdictions"]

    if (og_active or any_platform_active) and access_up and (commercial_up or research_fixed):
        verdict = "PHASE_L173_PLATFORM_EXPANSION_WORKING"
    elif access_up or og_active or any_platform_active or unknown_down:
        verdict = "PHASE_L173_PARTIAL_PLATFORM_EXPANSION"
    else:
        verdict = "PHASE_L173_PLATFORM_EXPANSION_FAILED"

    remaining = "expand productive agency-mirror discovery for OpenGov buyers behind Cloudflare CDN"
    if len(accessible_now_u) < 80:
        remaining = "quantity/config recovery on READY_TO_RESEARCH_NOW + more BidNet/state SimpleHTML harvest"
    if og_active and int(og.get("live_rows") or 0) < 10:
        remaining = "map more OpenGov agency alternate solicitation boards (Phoenix-style /Solicitations)"

    productivity = {
        "kind": "L173PlatformProductivity",
        "build": BUILD,
        "generated_at": _utc(),
        "by_platform": [
            {
                "platform": p,
                **(platform_results.get(p) or {}),
                "accessible_now_from_pipeline": int((plat_prod.get(p) or {}).get("accessible_now") or 0),
                "commercial_stage3_from_pipeline": int((plat_prod.get(p) or {}).get("commercial_stage3") or 0),
                "AdapterYieldScore": (platform_results.get(p) or {}).get("AdapterYieldScore"),
            }
            for p in ("OpenGov", "Bonfire", "PlanetBids", "IonWave", "SimpleHTML", "BidNet")
        ],
    }

    fresh = {
        "kind": "L173FreshHunt",
        "build": BUILD,
        "generated_at": _utc(),
        "terminal_status": hunt_status or HUNT_COMPLETE_WITH_FAILURES,
        "live_unique": live_unique,
        "accessible": len(rows),
        "stage3": stage_counts.get("stage3", 0),
        "commercial_stage3": commercial_s3,
        "NONFEDERAL_ACCESSIBLE_NOW_COUNT": len(accessible_now_u),
        "newly_merged": added,
        "platform_harvest_rows": len(harvested_rows),
        "bidnet_raw": bidnet_h.get("raw_count"),
        "sam_api_calls_consumed": 0,
        "baseline": L172_BASELINE,
        "delta": {
            "live_unique": live_unique - L172_BASELINE["live_unique"],
            "accessible": len(rows) - L172_BASELINE["accessible"],
            "stage3": stage_counts.get("stage3", 0) - L172_BASELINE["stage3"],
            "commercial_stage3": commercial_s3 - L172_BASELINE["commercial_stage3"],
            "NONFEDERAL_ACCESSIBLE_NOW": len(accessible_now_u)
            - L172_BASELINE["NONFEDERAL_ACCESSIBLE_NOW"],
            "validated_unique": len(validated_u) - L172_BASELINE["validated_unique"],
            "secondary_unique": len(secondary_u) - L172_BASELINE["secondary_unique"],
            "unknown": reduction["unknown_after"] - L172_BASELINE["unknown_jurisdictions"],
            "READY_TO_RESEARCH_NOW": len(research_u) - L172_BASELINE["READY_TO_RESEARCH_NOW"],
        },
    }

    quotes = {
        "kind": "L173QuoteTargets",
        "build": BUILD,
        "validated_unique": len(validated_u),
        "secondary_unique": len(secondary_u),
        "READY_FOR_OWNER_APPROVAL": len(ready_u),
        "NEEDS_MINOR_REVIEW": len(review_u),
        "READY_TO_RESEARCH_NOW": len(research_u),
        "REGISTER_TO_UNLOCK": len(unlock_u),
        "NOT_READY": not_ready,
        "validated": validated_u[:40],
        "secondary": secondary_u[:40],
        "pilot_gate_unchanged": True,
        "no_outreach": True,
    }

    summary = {
        "kind": "L173Summary",
        "build": BUILD,
        "generated_at": _utc(),
        "verdict": verdict,
        "baseline_used": L172_BASELINE,
        "platform_activation": {
            p: {
                "mapped": (platform_results.get(p) or {}).get("mapped"),
                "tested": (platform_results.get(p) or {}).get("tested"),
                "active": (platform_results.get(p) or {}).get("ingestion_active"),
                "access_states": (platform_results.get(p) or {}).get("access_state_counts"),
                "live_rows": (platform_results.get(p) or {}).get("live_rows"),
            }
            for p in ("OpenGov", "Bonfire", "PlanetBids", "IonWave", "SimpleHTML")
        },
        "coverage": {
            "unknown_before": L172_BASELINE["unknown_jurisdictions"],
            "unknown_after": reduction["unknown_after"],
            "delta_unknown": L172_BASELINE["unknown_jurisdictions"] - reduction["unknown_after"],
            "counties_mapped": reduction["counties_mapped"],
            "municipalities_mapped": reduction["municipalities_mapped"],
        },
        "after": {
            "live_unique": live_unique,
            "accessible": len(rows),
            "stage3": stage_counts.get("stage3", 0),
            "commercial_stage3": commercial_s3,
            "NONFEDERAL_ACCESSIBLE_NOW": len(accessible_now_u),
            "validated_unique": len(validated_u),
            "secondary_unique": len(secondary_u),
            "READY_TO_RESEARCH_NOW": len(research_u),
            "READY_FOR_OWNER_APPROVAL": len(ready_u),
            "REGISTER_TO_UNLOCK": len(unlock_u),
        },
        "owner_queues": {
            READY_TO_RESEARCH_NOW: len(research_u),
            READY_FOR_OWNER_APPROVAL: len(ready_u),
            REGISTER_TO_UNLOCK: len(unlock_u),
        },
        "ready_to_research_diagnosis": diagnosis,
        "registration_top": reg_rows[:12],
        "remaining_bottleneck": remaining,
        "sam_api": sam,
        "bidnet": BIDNET_AUTH_HISTORY_PARKED,
        "dibbs": DIBBS_CAGE_REQUIRED,
        "no_sam_api_calls": True,
        "no_outreach": True,
        "evidence_gate_unchanged": True,
        "legacy_cleanup": legacy_cleanup_report(),
    }

    artifacts = {
        "l173_platform_adapter_inventory.json": inventory,
        "l173_opengov_results.json": platform_results.get("OpenGov") or {},
        "l173_bonfire_results.json": platform_results.get("Bonfire") or {},
        "l173_planetbids_results.json": platform_results.get("PlanetBids") or {},
        "l173_ionwave_results.json": platform_results.get("IonWave") or {},
        "l173_simplehtml_results.json": platform_results.get("SimpleHTML") or {},
        "l173_platform_productivity.json": productivity,
        "l173_registration_unlocks.json": {
            "kind": "L173RegistrationUnlocks",
            "build": BUILD,
            "top": reg_rows[:40],
            "opportunity_specific_count": len(unlock_u),
        },
        "l173_accessible_now.json": {
            "kind": NONFEDERAL_ACCESSIBLE_NOW,
            "build": BUILD,
            "count": len(accessible_now_u),
            "opportunities": accessible_now_u[:400],
        },
        "l173_ready_to_research.json": {
            "kind": READY_TO_RESEARCH_NOW,
            "build": BUILD,
            "count": len(research_u),
            "diagnosis": diagnosis,
            "opportunities": research_u[:300],
        },
        "l173_fresh_hunt.json": fresh,
        "l173_quote_targets.json": quotes,
        "l173_summary.json": summary,
    }
    for name, payload in artifacts.items():
        save_json(OUT / name, payload)
        print(f"[l173] wrote {name}", flush=True)

    write_l173_docs(summary, platform_results, fresh, quotes, diagnosis)
    return summary


def write_l173_docs(
    summary: dict[str, Any],
    platform_results: dict[str, Any],
    fresh: dict[str, Any],
    quotes: dict[str, Any],
    diagnosis: dict[str, Any],
) -> None:
    DOCS.mkdir(parents=True, exist_ok=True)
    og = platform_results.get("OpenGov") or {}
    docs = {
        "phase_l173_platform_adapter_strategy.md": f"""# Phase L.17.3 — Platform Adapter Strategy

Build: `{BUILD}`

## Verdict

`{summary.get('verdict')}`

Priority metric: jurisdictions × public_access × commercial_yield × reliability ÷ engineering_burden

OpenGov CDN (`procurement.opengov.com`) is Cloudflare-walled → `ANTI_BOT`.
Agency mirrors (Phoenix Solicitations, Boston bid-listings) remain publicly automatable.
""",
        "phase_l173_opengov.md": f"""# L.17.3 OpenGov

```json
{json.dumps(og, indent=2, default=str)[:4000]}
```

CDN portals classified `{ANTI_BOT}`. Agency mirrors → `{PUBLIC_LISTING_AUTOMATABLE}` / `{INGESTION_ACTIVE}` when parseable.
""",
        "phase_l173_bonfire.md": f"""# L.17.3 Bonfire

```json
{json.dumps(platform_results.get('Bonfire') or {}, indent=2, default=str)[:3000]}
```

Stop-loss applied on DNS/auth fragility. Mapped buyers retained.
""",
        "phase_l173_planetbids.md": f"""# L.17.3 PlanetBids

```json
{json.dumps(platform_results.get('PlanetBids') or {}, indent=2, default=str)[:3000]}
```
""",
        "phase_l173_ionwave.md": f"""# L.17.3 IonWave

```json
{json.dumps(platform_results.get('IonWave') or {}, indent=2, default=str)[:3000]}
```
""",
        "phase_l173_simplehtml.md": f"""# L.17.3 SimpleHTML

```json
{json.dumps(platform_results.get('SimpleHTML') or {}, indent=2, default=str)[:3000]}
```

Child bid-board link following enabled for purchasing landing pages.
""",
        "phase_l173_registration_unlocks.md": """# L.17.3 Registration Unlocks

Platform-level free accounts (`HIGH_LEVERAGE_PLATFORM_REGISTRATION`) outrank single-buyer registration.
See `l173_registration_unlocks.json`. BidNet auth history remains parked.
""",
        "phase_l173_accessible_now.md": f"""# L.17.3 Accessible-Now

Count: {summary.get('after', {}).get('NONFEDERAL_ACCESSIBLE_NOW')}

Baseline L.17.2: {L172_BASELINE['NONFEDERAL_ACCESSIBLE_NOW']}

Free/simple registration acceptable. No CAGE/SAM Opportunities API/DIBBS dependency.
""",
        "phase_l173_owner_queues.md": f"""# L.17.3 Owner Queues

- `{READY_TO_RESEARCH_NOW}`: {summary.get('owner_queues', {}).get(READY_TO_RESEARCH_NOW)}
- `{READY_FOR_OWNER_APPROVAL}`: {summary.get('owner_queues', {}).get(READY_FOR_OWNER_APPROVAL)}
- `{REGISTER_TO_UNLOCK}`: {summary.get('owner_queues', {}).get(REGISTER_TO_UNLOCK)}

## READY_TO_RESEARCH diagnosis

```json
{json.dumps(diagnosis, indent=2)}
```
""",
        "phase_l173_legacy_cleanup.md": """# L.17.3 Legacy Cleanup

- Shared adapters in `discovery/platform_adapters.py` (config-driven; not per-city parsers)
- OpenGov CDN vs agency-mirror distinction explicit
- READY_TO_RESEARCH no longer starved by free-reg exclusive routing
- SAM Opportunities API parked; BidNet auth history parked; DIBBS = CAGE required
""",
        "phase_l173_regression.md": """# L.17.3 Regression

Tests: `tests/test_phase_l173_platform_adapters.py`

Covers adapters, crosswalk, access states, READY_TO_RESEARCH behavior, no SAM/DIBBS/outreach.
""",
    }
    for name, body in docs.items():
        (DOCS / name).write_text(body, encoding="utf-8")


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--no-refresh-hunt", action="store_true")
    p.add_argument("--no-probe", action="store_true")
    p.add_argument("--probe-max-counties", type=int, default=200)
    p.add_argument("--max-hunt-sources", type=int, default=28)
    args = p.parse_args()
    summary = run_phase_l173(
        authorize_live=True,
        refresh_hunt=not args.no_refresh_hunt,
        max_hunt_sources=args.max_hunt_sources,
        probe_counties=not args.no_probe,
        probe_max_counties=args.probe_max_counties,
    )
    print(
        json.dumps(
            {
                k: summary[k]
                for k in (
                    "verdict",
                    "platform_activation",
                    "after",
                    "owner_queues",
                    "coverage",
                    "remaining_bottleneck",
                )
            },
            indent=2,
            default=str,
        )
    )
