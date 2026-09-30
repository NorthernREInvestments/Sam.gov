"""Phase L.17.2 — National coverage saturation + NONFEDERAL_ACCESSIBLE_NOW expansion."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from application_clock import now_utc
from discovery.coverage_saturation import (
    build_registration_unlocks,
    load_persisted_registry,
    platform_leverage_report,
    run_saturation_pass,
    status_breakdown,
)
from discovery.jurisdiction_registry import coverage_percentages, platform_jurisdiction_map
from discovery.lower48 import (
    ACCESSIBLE_NOW,
    BUILD_L172,
    DIBBS_CAGE_REQUIRED,
    FREE_REGISTRATION_REQUIRED,
    NONFEDERAL_ACCESSIBLE_NOW,
    REGISTER_BEFORE_BID,
    REGISTER_NOW_RECURRING_BUYER,
    UNKNOWN_RESEARCH_PENDING,
    classify_opportunity_access,
    current_access_score,
    is_nonfederal_accessible_now,
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

# L.17 baseline (L.17.1 not completed)
L17_BASELINE = {
    "baseline_source": "L17",
    "live_unique": 244,
    "accessible": 2529,
    "stage3": 611,
    "commercial_stage3": 116,
    "NONFEDERAL_ACCESSIBLE_NOW": 35,
    "validated_unique": 1,
    "secondary_unique": 2,
    "unknown_jurisdictions": 22384,
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


def harvest_bidnet_public_networks(*, max_states: int = 12) -> dict[str, Any]:
    """Ingest public BidNet open-bids metadata for priority states (packages may need free reg)."""
    from discovery.bidnet_network import all_bidnet_networks_enriched
    from discovery.http_client import PublicProcurementHttpClient
    from discovery.live_fetchers import BidNetLiveFetcher
    from phase_l.hunt import screen_and_rank

    priority = {"TX", "GA", "VA", "IL", "FL", "CA", "NY", "OH", "NC", "MO", "KY", "IA", "KS", "WA"}
    nets = [n for n in all_bidnet_networks_enriched() if n.get("state_code") in priority][:max_states]
    fetcher = BidNetLiveFetcher()
    client = PublicProcurementHttpClient(authorize_live=True)
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for n in nets:
        url = n.get("list_url")
        sid = n.get("source_id")
        try:
            raw = fetcher.fetch_listing(client, list_url=url, source_id=sid, max_pages=1)
            opps = raw.get("opportunities") or []
            if not opps and (raw.get("body") or raw.get("text")):
                opps = fetcher.parse_listing(
                    raw.get("body") or raw.get("text") or "",
                    list_url=url,
                    meta={"source_id": sid},
                )
            for o in opps:
                d = o.to_dict() if hasattr(o, "to_dict") else dict(o)
                d["source_id"] = sid
                d["source_portal"] = sid
                d["platform_family"] = "BidNet"
                d["state_code"] = n.get("state_code")
                d["jurisdiction"] = d.get("jurisdiction") or "LOCAL"
                d["registration_action"] = REGISTER_BEFORE_BID
                d["our_bid_access"] = d.get("our_bid_access") or "YES"
                d["l172_harvest"] = True
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


def attach_authoritative_chain(row: dict[str, Any]) -> dict[str, Any]:
    orig = resolve_original_solicitation(row)
    sub = submission_path_checklist(row, original=orig)
    row["discovery_provenance"] = {
        "source_id": row.get("source_id") or row.get("source_portal"),
        "source_url": row.get("source_url") or row.get("list_url"),
    }
    row["authoritative_bid_location"] = {
        "detail_url": row.get("detail_url") or orig.get("detail_url"),
        "submission_path": sub,
        "solicitation_id": orig.get("solicitation_number") or row.get("solicitation_number"),
        "buyer": row.get("agency") or row.get("buyer"),
    }
    row["requirements_source"] = {
        "package_url": row.get("detail_url") or orig.get("detail_url"),
        "amendments_resolved": bool(sub.get("checks", {}).get("latest_amendment_reviewed")),
    }
    return row


def run_phase_l172(
    *,
    authorize_live: bool = True,
    refresh_hunt: bool = True,
    max_hunt_sources: int = 28,
    probe: bool = True,
    probe_max_counties: int | None = 500,
) -> dict[str, Any]:
    assert_no_fixed_positive_cap()
    assert STAGE3_NO_ROW_CAP
    park_bidnet_auth_history()
    sam = sam_api_park_status()
    assert sam["calls_consumed"] == 0

    from operating_mode import MODE_DEVELOPMENT_NO_OUTREACH, set_operating_mode

    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)

    # --- Coverage saturation (resume, no registry reset) ---
    print("[l172] saturation pass (resume from checkpoint)...", flush=True)
    sat = run_saturation_pass(probe=probe, probe_max_counties=probe_max_counties)
    reg = sat["registry"]
    reduction = sat["reduction"]
    print(
        f"[l172] unknown {reduction['unknown_before']} -> {reduction['unknown_after']} "
        f"(delta={reduction['delta_unknown']}); counties_mapped={reduction['counties_mapped']} "
        f"munis_mapped={reduction['municipalities_mapped']}",
        flush=True,
    )

    # --- Harvest structured + BidNet public networks ---
    from phase_l.hunt import screen_and_rank
    from phase_l.l15_structured_expansion import harvest_live_structured_sources

    live_harvest = harvest_live_structured_sources(max_per_source=250)
    print("[l172] harvesting BidNet public statewide listings...", flush=True)
    bidnet_h = harvest_bidnet_public_networks(max_states=14)

    hunt_meta: dict[str, Any] = {}
    hunt_status = None
    if refresh_hunt and authorize_live:
        from phase_l.hunt import run_phase_l_hunt

        reset_checkpoint()
        print("[l172] fresh hunt profile=structured (no SAM Opportunities API)...", flush=True)
        hunt = run_phase_l_hunt(
            authorize_live=True,
            max_sources=max_hunt_sources,
            profile="structured",
        )
        hunt_meta = hunt.get("discovery_meta") or {}
        hunt_status = (hunt_meta.get("live_runner") or {}).get("run_status")

    data = json.loads((OUT / "accessible_latest.json").read_text(encoding="utf-8"))
    rows = label_inventory_freshness(list(data.get("rows") or []))
    existing = {cross_source_dedupe_key(r) for r in rows}
    added = 0
    for batch in (live_harvest.get("rows") or [], bidnet_h.get("rows") or []):
        ranked = screen_and_rank(batch) if batch else {"accessible": []}
        for r in ranked.get("accessible") or batch:
            attach_authoritative_chain(r)
            k = cross_source_dedupe_key(r)
            if k in existing:
                continue
            existing.add(k)
            rows.append(r)
            added += 1
    for r in rows:
        attach_authoritative_chain(r)
    data["rows"] = rows
    save_json(OUT / "accessible_latest.json", data)
    print(f"[l172] newly_merged={added} accessible={len(rows)}", flush=True)

    buyer_memory = load_json(BUYER_VALUE_PATH)
    supplier_memory = load_json(SUPPLIER_MEMORY_PATH)
    access_yes = [
        r
        for r in rows
        if str(r.get("our_bid_access") or "").upper() in {"YES", "CONDITIONAL", "REGISTER"}
    ]

    stage_counts = Counter()
    stage3: list[dict[str, Any]] = []
    print(f"[l172] scanning {len(access_yes)} access-eligible...", flush=True)
    for i, row in enumerate(access_yes):
        if i and i % 400 == 0:
            print(f"[l172] stage-scan {i}/{len(access_yes)} s3={len(stage3)}", flush=True)
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
    platform_opp: dict[str, Counter] = {}

    print(f"[l172] Stage3={len(stage3)} - process all...", flush=True)
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
        plat = str(row.get("platform_family") or row.get("source_portal") or "unknown")
        source_prod.setdefault(sid, Counter())
        platform_opp.setdefault(plat, Counter())
        source_prod[sid]["stage3"] += 1
        platform_opp[plat]["stage3"] += 1
        if is_commercial:
            source_prod[sid]["commercial_stage3"] += 1
            platform_opp[plat]["commercial_stage3"] += 1

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
        max_buy_block = econ.get("max_buy")
        if not isinstance(max_buy_block, dict):
            max_buy_block = {}
        packet = {
            "notice_id": row.get("notice_id") or row.get("external_id"),
            "title": row.get("title"),
            "buyer": row.get("agency") or row.get("buyer"),
            "state": row.get("state_code"),
            "solicitation": original.get("solicitation_number") or row.get("solicitation_number"),
            "tangible_product": bool((pipe.get("stage2") or {}).get("pass")),
            "deadline": row.get("response_deadline") or row.get("deadline"),
            "current_access_status": opp_access,
            "registration_needed": opp_access
            in {FREE_REGISTRATION_REQUIRED, "SIMPLE_VENDOR_SETUP"},
            "authoritative_posting": (row.get("authoritative_bid_location") or {}).get("detail_url"),
            "submission_location": row.get("authoritative_bid_location"),
            "gov_evidence": audit.get("gov_grade"),
            "estimated_value": (econ.get("government_value") or {}).get("value")
            if isinstance(econ.get("government_value"), dict)
            else row.get("estimated_value"),
            "supplier_status": supplier_status,
            "economics_status": max_buy_block.get("status") or qstate,
            "remaining_blocker": audit.get("primary_blocker") or audit.get("blocker"),
            "nonfederal_accessible_now": nf_ok,
            "CurrentAccessScore": access["CurrentAccessScore"],
            "source_id": sid,
            "quote_state": qstate,
            "freshness": row.get("inventory_freshness") or row.get("freshness"),
            "discovery_provenance": row.get("discovery_provenance"),
            "win_probability_inputs": {
                "prior_bidder_count": row.get("offer_count"),
                "incumbent": row.get("incumbent"),
                "set_aside_access": row.get("set_aside"),
                "solicitation_type": row.get("notice_type"),
                "repeated_buyer": bool(row.get("recurring_buyer")),
            },
        }

        if nf_ok and packet["tangible_product"]:
            accessible_now.append(packet)
            source_prod[sid]["accessible_now"] += 1
            platform_opp[plat]["accessible_now"] += 1
            if opp_access in {FREE_REGISTRATION_REQUIRED, "SIMPLE_VENDOR_SETUP"}:
                register_unlock.append({**packet, "queue": REGISTER_TO_UNLOCK})
            elif qstate not in {VALIDATED_QUOTE_TARGET, SECONDARY_QUOTE_TARGET}:
                ready_research.append({**packet, "queue": READY_TO_RESEARCH_NOW})

        if qstate == VALIDATED_QUOTE_TARGET:
            validated.append(packet)
            ready_owner.append({**packet, "pilot_state": READY_FOR_OWNER_APPROVAL})
            source_prod[sid]["validated"] += 1
        elif qstate == SECONDARY_QUOTE_TARGET:
            secondary.append(packet)
            needs_review.append({**packet, "pilot_state": NEEDS_MINOR_REVIEW})
            source_prod[sid]["secondary"] += 1
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
        or bool((r.get("raw_metadata") or {}).get("structured_adapter"))
        or bool((r.get("raw_metadata") or {}).get("l15_harvest"))
        or bool(r.get("l172_harvest"))
        or str(r.get("source_id") or r.get("source_portal") or "").startswith("network_bidnet")
    ]
    live_unique = len({cross_source_dedupe_key(r) for r in structured})
    if live_unique == 0:
        # Fallback: nonfederal unique among accessible inventory
        live_unique = len(
            {
                cross_source_dedupe_key(r)
                for r in rows
                if is_nonfederal_accessible_now(r)
                or str(r.get("jurisdiction") or "").upper()
                not in {"FEDERAL", "FED", ""}
            }
        )

    # Platform leverage with opportunity stats
    plat_stats = {
        p: {
            "accessible_now": int(c.get("accessible_now") or 0),
            "commercial_stage3": int(c.get("commercial_stage3") or 0),
            "tangible_pct": 50.0,
            "reliability": 0.75,
        }
        for p, c in platform_opp.items()
    }
    leverage = platform_leverage_report(reg, opportunity_stats=plat_stats)
    reg_unlocks = build_registration_unlocks(reg)
    pct = coverage_percentages(reg)
    statuses = status_breakdown(reg)

    # Verdict — compare against L.17 baseline (within-run delta may be 0 on resume)
    unknown_reduced_vs_baseline = (
        L17_BASELINE["unknown_jurisdictions"] - reduction["unknown_after"]
    ) >= 100
    unknown_down = unknown_reduced_vs_baseline or reduction["delta_unknown"] >= 100
    counties_up = reduction["counties_mapped"] >= 80
    access_up = len(accessible_now_u) > L17_BASELINE["NONFEDERAL_ACCESSIBLE_NOW"]
    commercial_up = commercial_s3 >= L17_BASELINE["commercial_stage3"]
    if unknown_down and counties_up and (access_up or commercial_up):
        verdict = "PHASE_L172_NATIONAL_COVERAGE_SATURATION_WORKING"
    elif unknown_down or counties_up or access_up:
        verdict = "PHASE_L172_PARTIAL_NATIONAL_COVERAGE_SATURATION"
    else:
        verdict = "PHASE_L172_NATIONAL_COVERAGE_SATURATION_FAILED"

    remaining = "OpenGov/Bonfire/PlanetBids generic PlatformAdapter integration for mapped portals"
    if len(accessible_now_u) < L17_BASELINE["NONFEDERAL_ACCESSIBLE_NOW"] * 2:
        remaining = "quantity/config + BidNet free-reg unlock on NONFEDERAL_ACCESSIBLE_NOW queue"
    if reduction["unknown_after"] > 15000:
        remaining = "continue county portal probing + platform buyer directory enumeration"

    # Owner worklist
    nearing = sorted(
        [p for p in accessible_now_u if p.get("deadline")],
        key=lambda x: str(x.get("deadline")),
    )[:15]
    worklist = {
        "kind": "L172OwnerWorklist",
        "build": BUILD_L172,
        "generated_at": _utc(),
        "best_contracts_to_inspect": accessible_now_u[:15],
        "registrations_worth_completing": (reg_unlocks.get("top") or [])[:10],
        "quote_ready_targets": ready_u[:10],
        "opportunities_nearing_deadline": nearing,
        "no_automatic_actions": True,
    }

    fresh = {
        "kind": "L172FreshHunt",
        "build": BUILD_L172,
        "generated_at": _utc(),
        "terminal_status": hunt_status or HUNT_COMPLETE_WITH_FAILURES,
        "live_unique": live_unique,
        "accessible": len(rows),
        "stage1": stage_counts.get("stage1", 0),
        "stage2": stage_counts.get("stage2", 0),
        "stage3": stage_counts.get("stage3", 0),
        "commercial_stage3": commercial_s3,
        "NONFEDERAL_ACCESSIBLE_NOW_COUNT": len(accessible_now_u),
        "newly_merged": added,
        "bidnet_harvest_raw": bidnet_h.get("raw_count"),
        "sam_api_calls_consumed": 0,
        "baseline": L17_BASELINE,
        "delta": {
            "live_unique": live_unique - L17_BASELINE["live_unique"],
            "accessible": len(rows) - L17_BASELINE["accessible"],
            "stage3": stage_counts.get("stage3", 0) - L17_BASELINE["stage3"],
            "commercial_stage3": commercial_s3 - L17_BASELINE["commercial_stage3"],
            "NONFEDERAL_ACCESSIBLE_NOW": len(accessible_now_u) - L17_BASELINE["NONFEDERAL_ACCESSIBLE_NOW"],
            "validated_unique": len(validated_u) - L17_BASELINE["validated_unique"],
            "secondary_unique": len(secondary_u) - L17_BASELINE["secondary_unique"],
            "unknown": reduction["unknown_after"] - L17_BASELINE["unknown_jurisdictions"],
        },
    }

    quotes = {
        "kind": "L172QuoteTargets",
        "build": BUILD_L172,
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

    unknown_by_state = Counter(
        j["state"]
        for j in reg["jurisdictions"].values()
        if j["buyer_type"] == "COUNTY" and j.get("source_status") == UNKNOWN_RESEARCH_PENDING
    )

    summary = {
        "kind": "L172Summary",
        "build": BUILD_L172,
        "generated_at": _utc(),
        "verdict": verdict,
        "baseline_used": L17_BASELINE,
        "coverage": {
            "unknown_before": max(reduction["unknown_before"], L17_BASELINE["unknown_jurisdictions"]),
            "unknown_after": reduction["unknown_after"],
            "delta_unknown": L17_BASELINE["unknown_jurisdictions"] - reduction["unknown_after"],
            "delta_unknown_this_run": reduction["delta_unknown"],
            "counties_mapped": reduction["counties_mapped"],
            "municipalities_mapped": reduction["municipalities_mapped"],
            "coverage_percentages": pct,
            "status_breakdown": statuses,
        },
        "platform_leverage_top": (leverage.get("platforms") or [])[:12],
        "registration_top": (reg_unlocks.get("top") or [])[:12],
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
        "geographic_gaps": {
            "unknown_counties_by_state_top": unknown_by_state.most_common(15),
        },
        "saturation": {
            "catalog_stats": sat.get("catalog_stats"),
            "probe_stats": sat.get("probe_stats"),
            "bidnet_hints": sat.get("bidnet_hints"),
        },
        "sam_api": sam,
        "bidnet": BIDNET_AUTH_HISTORY_PARKED,
        "dibbs": DIBBS_CAGE_REQUIRED,
        "remaining_bottleneck": remaining,
        "no_sam_api_calls": True,
        "no_outreach": True,
        "evidence_gate_unchanged": True,
        "legacy_cleanup": legacy_cleanup_report(),
    }

    source_productivity = {
        "kind": "L172SourceProductivity",
        "build": BUILD_L172,
        "sources": sorted(
            [{"source_id": k, **dict(v)} for k, v in source_prod.items()],
            key=lambda x: (-int(x.get("accessible_now") or 0), -int(x.get("commercial_stage3") or 0)),
        )[:40],
        "refresh_priority_boost": [
            s["source_id"]
            for s in sorted(
                [{"source_id": k, **dict(v)} for k, v in source_prod.items()],
                key=lambda x: -int(x.get("accessible_now") or 0),
            )[:10]
            if int(s.get("accessible_now") or 0) > 0
        ],
        "refresh_priority_lower": [
            s["source_id"]
            for s in sorted(
                [{"source_id": k, **dict(v)} for k, v in source_prod.items()],
                key=lambda x: int(x.get("stage3") or 0),
            )
            if int(s.get("commercial_stage3") or 0) == 0 and int(s.get("accessible_now") or 0) == 0
        ][:10],
    }

    artifacts = {
        "l172_unknown_reduction.json": reduction,
        "l172_county_saturation.json": {
            "kind": "L172CountySaturation",
            "build": BUILD_L172,
            "counties_mapped": reduction["counties_mapped"],
            "pct": pct.get("county_source_mapped_pct"),
            "probe_stats": sat.get("probe_stats"),
            "unknown_by_state": dict(unknown_by_state),
            "priority_states": list(
                __import__(
                    "discovery.county_portal_catalog", fromlist=["PRIORITY_COUNTY_STATES"]
                ).PRIORITY_COUNTY_STATES
            ),
        },
        "l172_municipality_saturation.json": {
            "kind": "L172MunicipalitySaturation",
            "build": BUILD_L172,
            "municipalities_mapped": reduction["municipalities_mapped"],
            "pct": pct.get("municipality_source_mapped_pct"),
        },
        "l172_platform_enumeration.json": {
            "kind": "L172PlatformEnumeration",
            "build": BUILD_L172,
            "catalog_families": leverage.get("catalog_families"),
            "platforms": platform_jurisdiction_map(reg).get("platforms"),
        },
        "l172_platform_leverage.json": leverage,
        "l172_registration_unlocks.json": reg_unlocks,
        "l172_accessible_now.json": {
            "kind": NONFEDERAL_ACCESSIBLE_NOW,
            "build": BUILD_L172,
            "count": len(accessible_now_u),
            "opportunities": accessible_now_u[:400],
            "business_context": {
                "nebraska_llc": "not_yet_formed",
                "cage": "none",
                "dibbs": DIBBS_CAGE_REQUIRED,
                "sam_api": SAM_API_PENDING_REPLACEMENT_KEY,
            },
        },
        "l172_source_productivity.json": source_productivity,
        "l172_fresh_hunt.json": fresh,
        "l172_owner_worklist.json": worklist,
        "l172_quote_targets.json": quotes,
        "l172_summary.json": summary,
    }
    for name, payload in artifacts.items():
        save_json(OUT / name, payload)
        print(f"[l172] wrote {name}", flush=True)

    write_l172_docs(summary, reduction, leverage, fresh, quotes, worklist)
    return summary


def write_l172_docs(
    summary: dict[str, Any],
    reduction: dict[str, Any],
    leverage: dict[str, Any],
    fresh: dict[str, Any],
    quotes: dict[str, Any],
    worklist: dict[str, Any],
) -> None:
    DOCS.mkdir(parents=True, exist_ok=True)
    docs = {
        "phase_l172_coverage_saturation.md": f"""# Phase L.17.2 — Coverage Saturation

Build: `{BUILD_L172}`

## Verdict

`{summary.get('verdict')}`

## Unknown reduction

- Before: {reduction.get('unknown_before')}
- After: {reduction.get('unknown_after')}
- Delta: {reduction.get('delta_unknown')}
- Counties mapped: {reduction.get('counties_mapped')}
- Municipalities mapped: {reduction.get('municipalities_mapped')}

Resume-safe from `data/l17_coverage_checkpoint.json` + `data/l172_saturation_checkpoint.json`.
No national registry reset.
""",
        "phase_l172_platform_enumeration.md": f"""# L.17.2 Platform Enumeration

Catalog families: {json.dumps(leverage.get('catalog_families'), indent=2)}

Crosswalked onto JurisdictionProcurementRegistry. One adapter → many buyers.
""",
        "phase_l172_platform_leverage.md": f"""# L.17.2 Platform Leverage

Top platforms:

```json
{json.dumps((leverage.get('platforms') or [])[:8], indent=2)}
```

Metric: `PlatformLeverageScore` / `JurisdictionsUnlockedPerAdapter`
""",
        "phase_l172_registration_unlocks.md": f"""# L.17.2 Registration Unlocks

Top free/simple registrations (one account → many buyers). See `l172_registration_unlocks.json`.

Actions: `{REGISTER_BEFORE_BID}` / `{REGISTER_NOW_RECURRING_BUYER}` — never auto-register.
""",
        "phase_l172_accessible_now.md": f"""# L.17.2 Accessible-Now

KPI: `{NONFEDERAL_ACCESSIBLE_NOW}`

Count: {summary.get('after', {}).get('NONFEDERAL_ACCESSIBLE_NOW')}

Baseline (L.17): {L17_BASELINE['NONFEDERAL_ACCESSIBLE_NOW']}

Free/simple registration is allowed. CAGE/DIBBS/SAM Opportunities API not required.
""",
        "phase_l172_source_productivity.md": """# L.17.2 Source Productivity

See `artifacts/phase_l/l172_source_productivity.json` for live/commercial/accessible-now by source
and refresh priority boost/lower lists.
""",
        "phase_l172_owner_workflow.md": f"""# L.17.2 Owner Workflow

Three queues:

1. `{READY_TO_RESEARCH_NOW}`
2. `{READY_FOR_OWNER_APPROVAL}`
3. `{REGISTER_TO_UNLOCK}`

Worklist: `artifacts/phase_l/l172_owner_worklist.json`

No automatic actions. No outreach. No bid submission.
""",
        "phase_l172_legacy_cleanup.md": """# L.17.2 Legacy Cleanup

- Canonical registry remains `data/jurisdiction_procurement_registry.json`
- Saturation resumes via checkpoints — never rebuilds inventory from zero
- Platform catalogs centralize buyer→platform mappings
- SAM Opportunities API parked; BidNet auth history parked; DIBBS = CAGE required
""",
        "phase_l172_regression.md": """# L.17.2 Regression

Tests: `tests/test_phase_l172_coverage_saturation.py`

Covers resume, no registry reset, platform enumeration, accessible-now, owner queues,
no SAM/DIBBS/outreach, Stage 3 uncapped, pilot gate unchanged.
""",
    }
    for name, body in docs.items():
        (DOCS / name).write_text(body, encoding="utf-8")


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--no-refresh-hunt", action="store_true")
    p.add_argument("--no-probe", action="store_true")
    p.add_argument("--probe-max-counties", type=int, default=500)
    p.add_argument("--max-hunt-sources", type=int, default=28)
    args = p.parse_args()
    summary = run_phase_l172(
        authorize_live=True,
        refresh_hunt=not args.no_refresh_hunt,
        max_hunt_sources=args.max_hunt_sources,
        probe=not args.no_probe,
        probe_max_counties=args.probe_max_counties,
    )
    print(
        json.dumps(
            {
                k: summary[k]
                for k in ("verdict", "coverage", "after", "owner_queues", "remaining_bottleneck")
            },
            indent=2,
            default=str,
        )
    )
