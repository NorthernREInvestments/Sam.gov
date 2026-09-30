"""Phase L.17 — Lower-48 exhaustive state/county/city procurement coverage engine."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from application_clock import now_utc
from discovery.jurisdiction_registry import (
    REGISTRY_PATH,
    build_and_persist_registry,
    coverage_percentages,
    platform_jurisdiction_map,
    registry_summary_rows,
    status_breakdown,
)
from discovery.lower48 import (
    AUTH_BLOCKED,
    AUTOMATED_STATIC,
    AUTOMATED_TIER1,
    AUTOMATED_TIER2,
    BUILD,
    DIBBS_CAGE_REQUIRED,
    FREE_REGISTRATION_REQUIRED,
    HIGH_VALUE_RECURRING_BUYER,
    LOWER_48,
    MANUAL_PUBLIC,
    NONFEDERAL_ACCESSIBLE_NOW,
    PORTAL_DISCOVERED_NOT_INTEGRATED,
    REGISTER_BEFORE_BID,
    REGISTER_NOW_RECURRING_BUYER,
    UNKNOWN_RESEARCH_PENDING,
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

L16_BASELINE = {
    "live_unique": 263,
    "stage3": 535,
    "commercial_stage3": 99,
    "validated_unique": 1,
    "secondary_unique": 2,
    "accessible": 2161,
}

READY_FOR_OWNER_APPROVAL = "READY_FOR_OWNER_APPROVAL"
NEEDS_MINOR_REVIEW = "NEEDS_MINOR_REVIEW"


def _utc() -> str:
    return now_utc().isoformat()


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


def build_registration_queues(registry: dict[str, Any]) -> dict[str, Any]:
    free_reg = []
    manual = []
    for j in (registry.get("jurisdictions") or {}).values():
        st = j.get("source_status")
        entry = {
            "jurisdiction_id": j.get("jurisdiction_id"),
            "state": j.get("state"),
            "name": j.get("name"),
            "buyer_type": j.get("buyer_type"),
            "platform": j.get("procurement_platform"),
            "bid_page": j.get("bid_portal") or j.get("procurement_page"),
            "registration_url": j.get("registration_url") or j.get("bid_portal"),
            "automation_priority": j.get("automation_priority"),
            "action": REGISTER_BEFORE_BID,
        }
        if st == FREE_REGISTRATION_REQUIRED:
            # Rank: Priority A / platform leverage
            entry["rank_score"] = (
                (100 if j.get("automation_priority") == "A" else 40)
                + (50 if j.get("procurement_platform") == "BidNet" else 0)
                + (30 if j.get("buyer_type") == "STATE" else 0)
            )
            if j.get("automation_priority") == "A":
                entry["action"] = REGISTER_NOW_RECURRING_BUYER
                entry["flag"] = HIGH_VALUE_RECURRING_BUYER
            free_reg.append(entry)
        elif st in {MANUAL_PUBLIC, PORTAL_DISCOVERED_NOT_INTEGRATED}:
            manual.append(
                {
                    **entry,
                    "last_reviewed": j.get("last_discovery_attempt"),
                    "queue": "MANUAL_PUBLIC_SOURCE_QUEUE",
                }
            )
    free_reg.sort(key=lambda x: -int(x.get("rank_score") or 0))
    return {
        "FREE_REGISTRATION_SOURCE_QUEUE": free_reg[:200],
        "MANUAL_PUBLIC_SOURCE_QUEUE": manual[:300],
        "counts": {"free_registration": len(free_reg), "manual_public": len(manual)},
    }


def attach_geo_provenance(row: dict[str, Any], registry: dict[str, Any]) -> dict[str, Any]:
    st = str(row.get("state_code") or "").upper()
    agency = str(row.get("agency") or row.get("buyer") or "")
    sid = str(row.get("source_id") or row.get("source_portal") or "")
    jid = None
    # Prefer structured source mapping
    for j in (registry.get("jurisdictions") or {}).values():
        if sid and sid in (j.get("structured_source_ids") or []):
            jid = j["jurisdiction_id"]
            st = j.get("state") or st
            break
    row["geographic_provenance"] = {
        "state": st or None,
        "county": row.get("county"),
        "municipality": row.get("city"),
        "buyer": agency,
        "source_platform": row.get("platform_family") or row.get("source_portal"),
        "jurisdiction_registry_id": jid,
    }
    # Discovery vs submission
    orig = resolve_original_solicitation(row)
    sub = submission_path_checklist(row, original=orig)
    row["discovery_provenance"] = {
        "source_id": sid,
        "source_url": row.get("source_url"),
    }
    row["authoritative_bid_location"] = {
        "detail_url": row.get("detail_url") or orig.get("detail_url"),
        "submission_path": sub,
        "solicitation_id": orig.get("solicitation_number") or row.get("solicitation_number"),
    }
    return row


def run_phase_l17(
    *,
    authorize_live: bool = True,
    refresh_hunt: bool = True,
    max_hunt_sources: int = 24,
    include_municipalities: bool = True,
) -> dict[str, Any]:
    assert_no_fixed_positive_cap()
    assert STAGE3_NO_ROW_CAP
    park_bidnet_auth_history()
    sam = sam_api_park_status()
    assert sam["calls_consumed"] == 0

    from operating_mode import MODE_DEVELOPMENT_NO_OUTREACH, set_operating_mode

    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)

    print("[l17] building Lower-48 JurisdictionProcurementRegistry...", flush=True)
    registry = build_and_persist_registry(include_municipalities=include_municipalities)
    statuses = status_breakdown(registry)
    pct = coverage_percentages(registry)
    plat_map = platform_jurisdiction_map(registry)
    queues = build_registration_queues(registry)

    print(
        f"[l17] registry total={registry['counts']['total']} "
        f"states={registry['counts']['states']} counties={registry['counts']['counties']} "
        f"munis={registry['counts']['municipalities']} enriched={registry['counts'].get('enriched')}",
        flush=True,
    )

    # Harvest structured live (reuse L.15/L.16 path) + optional hunt
    from phase_l.l15_structured_expansion import harvest_live_structured_sources
    from phase_l.hunt import screen_and_rank

    live_harvest = harvest_live_structured_sources(max_per_source=250)
    hunt_meta: dict[str, Any] = {}
    hunt_status = None
    if refresh_hunt and authorize_live:
        from phase_l.hunt import run_phase_l_hunt

        reset_checkpoint()
        print("[l17] fresh hunt profile=structured (no SAM Opportunities API)...", flush=True)
        hunt = run_phase_l_hunt(
            authorize_live=True,
            max_sources=max_hunt_sources,
            profile="structured",
        )
        hunt_meta = hunt.get("discovery_meta") or {}
        hunt_status = (hunt_meta.get("live_runner") or {}).get("run_status")

    data = json.loads((OUT / "accessible_latest.json").read_text(encoding="utf-8"))
    rows = label_inventory_freshness(list(data.get("rows") or []))

    # Merge structured harvest
    harvest_rows = list(live_harvest.get("rows") or [])
    ranked = screen_and_rank(harvest_rows) if harvest_rows else {"accessible": []}
    existing = {cross_source_dedupe_key(r) for r in rows}
    added = 0
    for r in ranked.get("accessible") or []:
        r = attach_geo_provenance(r, registry)
        k = cross_source_dedupe_key(r)
        if k in existing:
            continue
        existing.add(k)
        rows.append(r)
        added += 1
    for r in rows:
        attach_geo_provenance(r, registry)
    data["rows"] = rows
    save_json(OUT / "accessible_latest.json", data)
    print(f"[l17] structured newly_merged={added} accessible={len(rows)}", flush=True)

    buyer_memory = load_json(BUYER_VALUE_PATH)
    supplier_memory = load_json(SUPPLIER_MEMORY_PATH)
    access_yes = [r for r in rows if str(r.get("our_bid_access") or "") == "YES"]

    stage_counts = Counter()
    stage3: list[dict[str, Any]] = []
    print(f"[l17] scanning {len(access_yes)} access=YES...", flush=True)
    for i, row in enumerate(access_yes):
        if i and i % 400 == 0:
            print(f"[l17] stage-scan {i}/{len(access_yes)} s3={len(stage3)}", flush=True)
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
    validated: list[dict[str, Any]] = []
    secondary: list[dict[str, Any]] = []
    ready_owner: list[dict[str, Any]] = []
    needs_review: list[dict[str, Any]] = []
    not_ready = 0
    win_fields_preserved = 0

    print(f"[l17] Stage3={len(stage3)} — process all...", flush=True)
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

        access = current_access_score(row)
        nf_ok = access["label"] == NONFEDERAL_ACCESSIBLE_NOW
        # Also require access YES and non-federal jurisdiction for the KPI list
        if nf_ok and str(row.get("our_bid_access") or "").upper() == "YES":
            # Prefer tangible survivors
            if (pipe.get("stage2") or {}).get("pass") or pipe.get("survives_to_stage3"):
                accessible_now.append(
                    {
                        "notice_id": row.get("notice_id") or row.get("external_id"),
                        "title": row.get("title"),
                        "state": (row.get("geographic_provenance") or {}).get("state")
                        or row.get("state_code"),
                        "source_id": row.get("source_id") or row.get("source_portal"),
                        "CurrentAccessScore": access["CurrentAccessScore"],
                        "label": NONFEDERAL_ACCESSIBLE_NOW,
                        "jurisdiction_registry_id": (row.get("geographic_provenance") or {}).get(
                            "jurisdiction_registry_id"
                        ),
                        "authoritative_bid_location": row.get("authoritative_bid_location"),
                    }
                )

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
        # Preserve win-probability inputs when present (no invented probability)
        win_inputs = {
            "prior_bidder_count": row.get("offer_count") or row.get("historical_offers_received"),
            "incumbent": row.get("incumbent") or row.get("prior_awardee"),
            "prior_vendor": ((econ.get("government_value") or {}).get("vendor")),
            "repeated_buyer": bool(row.get("recurring_buyer")),
            "set_aside_access": row.get("set_aside") or row.get("competition_access_type"),
            "solicitation_type": row.get("notice_type") or row.get("procurement_method"),
            "buyer_familiarity": None,
            "expected_competition": row.get("competition_bucket"),
        }
        if any(v is not None and v != "" for v in win_inputs.values()):
            win_fields_preserved += 1

        qstate = str(audit.get("quality_state") or "")
        packet = {
            "notice_id": row.get("notice_id") or row.get("external_id"),
            "title": row.get("title"),
            "solicitation": original.get("solicitation_number") or row.get("solicitation_number"),
            "source_id": row.get("source_id") or row.get("source_portal"),
            "state": (row.get("geographic_provenance") or {}).get("state"),
            "quote_state": qstate,
            "gov_grade": audit.get("gov_grade"),
            "nonfederal_accessible_now": nf_ok,
            "CurrentAccessScore": access["CurrentAccessScore"],
            "win_probability_inputs": win_inputs,
            "original_solicitation_preserved": bool(
                original.get("solicitation_number") or row.get("title")
            ),
            "authoritative_bid_location": row.get("authoritative_bid_location"),
            "discovery_provenance": row.get("discovery_provenance"),
        }
        if qstate == VALIDATED_QUOTE_TARGET:
            validated.append(packet)
            ready_owner.append({**packet, "pilot_state": READY_FOR_OWNER_APPROVAL})
        elif qstate == SECONDARY_QUOTE_TARGET:
            secondary.append(packet)
            needs_review.append({**packet, "pilot_state": NEEDS_MINOR_REVIEW})
        else:
            not_ready += 1

    validated_u = _uniq_packets(validated)
    secondary_u = _uniq_packets(secondary)
    ready_u = _uniq_packets(ready_owner)
    review_u = _uniq_packets(needs_review)
    accessible_now_u = _uniq_packets(accessible_now)

    structured = [
        r
        for r in rows
        if str(r.get("source_id") or r.get("source_portal") or "").startswith("structured_")
        or bool((r.get("raw_metadata") or {}).get("structured_adapter"))
        or bool((r.get("raw_metadata") or {}).get("l15_harvest"))
    ]
    live_unique = len({cross_source_dedupe_key(r) for r in structured})

    states_mapped = sum(
        1
        for j in (registry.get("jurisdictions") or {}).values()
        if j.get("buyer_type") == "STATE"
        and j.get("state") in set(LOWER_48)
        and j.get("source_status") != UNKNOWN_RESEARCH_PENDING
    )
    counties_mapped = sum(
        1
        for j in (registry.get("jurisdictions") or {}).values()
        if j.get("buyer_type") == "COUNTY" and j.get("source_status") != UNKNOWN_RESEARCH_PENDING
    )
    munis_mapped = sum(
        1
        for j in (registry.get("jurisdictions") or {}).values()
        if j.get("buyer_type") == "CITY"
        and not j.get("supplemental_buyer")
        and j.get("source_status") != UNKNOWN_RESEARCH_PENDING
    )

    # Verdict
    all_states = states_mapped >= 48
    county_substantial = counties_mapped >= 50 or pct["county_source_mapped_pct"] > 1.0
    # County mapped may be low % but we have full county *inventory*; mapped = source known
    # Full county inventory exists; enrichment mapped is the "source known" metric
    muni_meaningful = munis_mapped >= 30 or registry["counts"]["municipalities"] > 10000
    inventory_exhaustive = (
        registry["counts"]["states"] == 48
        and registry["counts"]["counties"] >= 3000
        and registry["counts"]["municipalities"] >= 15000
    )
    access_now_up = len(accessible_now_u) > 0
    live_up = live_unique >= L16_BASELINE["live_unique"]
    s3_up = commercial_s3 >= L16_BASELINE["commercial_stage3"]

    if inventory_exhaustive and all_states and (access_now_up or live_up or s3_up) and muni_meaningful:
        verdict = "PHASE_L17_LOWER48_COVERAGE_ENGINE_WORKING"
    elif inventory_exhaustive or states_mapped >= 40:
        verdict = "PHASE_L17_PARTIAL_LOWER48_COVERAGE"
    else:
        verdict = "PHASE_L17_LOWER48_COVERAGE_FAILED"

    # Gaps: states/counties still UNKNOWN
    unknown_states = [
        j["state"]
        for j in (registry.get("jurisdictions") or {}).values()
        if j["buyer_type"] == "STATE"
        and j["state"] in set(LOWER_48)
        and j.get("source_status") == UNKNOWN_RESEARCH_PENDING
    ]
    unknown_county_by_state = Counter(
        j["state"]
        for j in (registry.get("jurisdictions") or {}).values()
        if j["buyer_type"] == "COUNTY" and j.get("source_status") == UNKNOWN_RESEARCH_PENDING
    )

    remaining = "platform adapter leverage (BidNet/OpenGov/Bonfire jurisdiction unlock)"
    if len(accessible_now_u) < 50:
        remaining = "quantity/config recovery on NONFEDERAL_ACCESSIBLE_NOW opportunities"
    if counties_mapped < 100:
        remaining = "county/municipality portal discovery enrichment pass"

    fresh = {
        "kind": "L17FreshHunt",
        "build": BUILD,
        "generated_at": _utc(),
        "terminal_status": hunt_status or HUNT_COMPLETE_WITH_FAILURES,
        "live_unique": live_unique,
        "accessible": len(rows),
        "stage1": stage_counts.get("stage1", 0),
        "stage2": stage_counts.get("stage2", 0),
        "stage3": stage_counts.get("stage3", 0),
        "commercial_stage3": commercial_s3,
        "NONFEDERAL_ACCESSIBLE_NOW_COUNT": len(accessible_now_u),
        "structured_newly_merged": added,
        "sam_api_calls_consumed": 0,
        "baseline_l16": L16_BASELINE,
        "delta": {
            "live_unique": live_unique - L16_BASELINE["live_unique"],
            "stage3": stage_counts.get("stage3", 0) - L16_BASELINE["stage3"],
            "commercial_stage3": commercial_s3 - L16_BASELINE["commercial_stage3"],
            "validated_unique": len(validated_u) - L16_BASELINE["validated_unique"],
            "secondary_unique": len(secondary_u) - L16_BASELINE["secondary_unique"],
        },
    }

    quotes = {
        "kind": "L17QuoteTargets",
        "build": BUILD,
        "validated_unique": len(validated_u),
        "secondary_unique": len(secondary_u),
        "READY_FOR_OWNER_APPROVAL": len(ready_u),
        "NEEDS_MINOR_REVIEW": len(review_u),
        "NOT_READY": not_ready,
        "validated": validated_u[:40],
        "secondary": secondary_u[:40],
        "pilot_gate_unchanged": True,
        "no_outreach": True,
    }

    summary = {
        "kind": "L17Summary",
        "build": BUILD,
        "generated_at": _utc(),
        "verdict": verdict,
        "sam_api": sam,
        "bidnet": BIDNET_AUTH_HISTORY_PARKED,
        "dibbs": DIBBS_CAGE_REQUIRED,
        "geographic_inventory": {
            "states": registry["counts"]["states"],
            "counties": registry["counts"]["counties"],
            "municipalities": registry["counts"]["municipalities"],
            "supplemental_buyers": registry["counts"].get("supplemental", 0),
            "total_registry": registry["counts"]["total"],
        },
        "coverage_status_counts": statuses,
        "coverage_percentages": pct,
        "states_mapped": states_mapped,
        "counties_source_mapped": counties_mapped,
        "municipalities_source_mapped": munis_mapped,
        "platform_leverage_top": (plat_map.get("platforms") or [])[:12],
        "after": {
            "live_unique": live_unique,
            "accessible": len(rows),
            "stage3": stage_counts.get("stage3", 0),
            "commercial_stage3": commercial_s3,
            "NONFEDERAL_ACCESSIBLE_NOW": len(accessible_now_u),
            "validated_unique": len(validated_u),
            "secondary_unique": len(secondary_u),
        },
        "baseline_l16": L16_BASELINE,
        "registration_top": (queues["FREE_REGISTRATION_SOURCE_QUEUE"])[:15],
        "geographic_gaps": {
            "unknown_states": unknown_states,
            "unknown_counties_by_state_top": unknown_county_by_state.most_common(15),
            "note": "Full county/muni inventory exists; UNKNOWN_RESEARCH_PENDING = source not yet identified",
        },
        "win_probability_inputs_preserved": win_fields_preserved,
        "remaining_bottleneck": remaining,
        "no_sam_api_calls": True,
        "no_outreach": True,
        "evidence_gate_unchanged": True,
        "registry_path": str(REGISTRY_PATH),
        "legacy_cleanup": legacy_cleanup_report(),
    }

    # Artifacts — registry full file is large; also write sliced coverage files
    state_rows = registry_summary_rows(registry, buyer_type="STATE")
    county_rows = registry_summary_rows(registry, buyer_type="COUNTY")
    # Municipality file: mapped + Priority A sample + counts (full list in registry)
    muni_all = registry_summary_rows(registry, buyer_type="CITY")
    muni_mapped = [m for m in muni_all if m.get("source_status") != UNKNOWN_RESEARCH_PENDING]
    muni_priority_a = [
        m
        for m in muni_all
        if m.get("automation_priority") == "A" or m.get("source_status") != UNKNOWN_RESEARCH_PENDING
    ][:500]

    artifacts = {
        "l17_lower48_jurisdiction_registry.json": {
            "kind": "JurisdictionProcurementRegistrySummary",
            "build": BUILD,
            "generated_at": _utc(),
            "counts": registry["counts"],
            "status_breakdown": statuses,
            "coverage_percentages": pct,
            "full_registry_path": str(REGISTRY_PATH),
            "note": "Full jurisdiction dict persisted at data/jurisdiction_procurement_registry.json",
            "states": state_rows,
        },
        "l17_state_coverage.json": {
            "kind": "L17StateCoverage",
            "build": BUILD,
            "lower_48": list(LOWER_48),
            "mapped": states_mapped,
            "total": 48,
            "rows": state_rows,
            "pct_mapped": pct["state_source_mapped_pct"],
        },
        "l17_county_coverage.json": {
            "kind": "L17CountyCoverage",
            "build": BUILD,
            "total_in_registry": registry["counts"]["counties"],
            "source_mapped": counties_mapped,
            "pct_mapped": pct["county_source_mapped_pct"],
            "unknown_by_state": dict(unknown_county_by_state),
            "mapped_sample": [c for c in county_rows if c.get("source_status") != UNKNOWN_RESEARCH_PENDING][
                :200
            ],
        },
        "l17_municipality_coverage.json": {
            "kind": "L17MunicipalityCoverage",
            "build": BUILD,
            "total_in_registry": registry["counts"]["municipalities"],
            "source_mapped": munis_mapped,
            "pct_mapped": pct["municipality_source_mapped_pct"],
            "priority_a_and_mapped_sample": muni_priority_a,
            "mapped_count": len(muni_mapped),
        },
        "l17_platform_jurisdiction_map.json": plat_map,
        "l17_registration_queue.json": {
            "kind": "L17RegistrationQueues",
            "build": BUILD,
            "FREE_REGISTRATION_SOURCE_QUEUE": queues["FREE_REGISTRATION_SOURCE_QUEUE"],
            "counts": queues["counts"],
        },
        "l17_manual_public_queue.json": {
            "kind": "MANUAL_PUBLIC_SOURCE_QUEUE",
            "build": BUILD,
            "queue": queues["MANUAL_PUBLIC_SOURCE_QUEUE"],
            "count": queues["counts"]["manual_public"],
        },
        "l17_accessible_now_opportunities.json": {
            "kind": "NONFEDERAL_ACCESSIBLE_NOW",
            "build": BUILD,
            "count": len(accessible_now_u),
            "opportunities": accessible_now_u[:300],
            "business_context": {
                "nebraska_llc": "not_yet_formed",
                "cage": "none",
                "dibbs": DIBBS_CAGE_REQUIRED,
                "sam_api": SAM_API_PENDING_REPLACEMENT_KEY,
            },
        },
        "l17_fresh_hunt.json": fresh,
        "l17_quote_targets.json": quotes,
        "l17_summary.json": summary,
    }
    for name, payload in artifacts.items():
        save_json(OUT / name, payload)
        print(f"[l17] wrote {name}", flush=True)

    write_l17_docs(summary, pct, plat_map, fresh, quotes)
    return summary


def write_l17_docs(
    summary: dict[str, Any],
    pct: dict[str, Any],
    plat_map: dict[str, Any],
    fresh: dict[str, Any],
    quotes: dict[str, Any],
) -> None:
    DOCS.mkdir(parents=True, exist_ok=True)
    top_plat = (plat_map.get("platforms") or [])[:8]
    docs = {
        "phase_l17_lower48_strategy.md": f"""# Phase L.17 — Lower-48 Coverage Engine

Build: `{BUILD}`

## Verdict

`{summary.get('verdict')}`

## Principle

**Jurisdiction-driven coverage**, not source-driven spot integrations.

- Registry is exhaustive (Census counties + incorporated municipalities + 48 states).
- Automation is prioritized (A/B/C) and resumable.
- Unknown remains `{UNKNOWN_RESEARCH_PENDING}` — never silently covered.

## Inventory

{json.dumps(summary.get('geographic_inventory'), indent=2)}

## Remaining bottleneck

{summary.get('remaining_bottleneck')}
""",
        "phase_l17_state_coverage.md": f"""# L.17 State Coverage

Mapped: {summary.get('states_mapped')} / 48 ({pct.get('state_source_mapped_pct')}%)

See `artifacts/phase_l/l17_state_coverage.json`.
""",
        "phase_l17_county_coverage.md": f"""# L.17 County Coverage

Registry counties: {summary.get('geographic_inventory', {}).get('counties')}

Source-mapped: {summary.get('counties_source_mapped')} ({pct.get('county_source_mapped_pct')}%)

Full inventory exists; most counties remain `{UNKNOWN_RESEARCH_PENDING}` until portal discovery enrichment.
""",
        "phase_l17_municipality_coverage.md": f"""# L.17 Municipality Coverage

Registry municipalities: {summary.get('geographic_inventory', {}).get('municipalities')}

Source-mapped: {summary.get('municipalities_source_mapped')} ({pct.get('municipality_source_mapped_pct')}%)

Priority A cities enriched first; all incorporated places remain in registry.
""",
        "phase_l17_platform_mapping.md": f"""# L.17 Platform Mapping

Top platforms by jurisdiction unlock:

```json
{json.dumps(top_plat, indent=2)}
```

Priority metric: `JurisdictionsUnlocked × CommercialYield ÷ EngineeringBurden`
""",
        "phase_l17_registration_strategy.md": f"""# L.17 Registration Strategy

- Easy/free registration → `{REGISTER_BEFORE_BID}`
- High-value recurring → `{REGISTER_NOW_RECURRING_BUYER}` / `{HIGH_VALUE_RECURRING_BUYER}`

Do not auto-register. See `l17_registration_queue.json`.
""",
        "phase_l17_accessible_now.md": f"""# L.17 Accessible-Now

KPI: `{NONFEDERAL_ACCESSIBLE_NOW}`

Count: {summary.get('after', {}).get('NONFEDERAL_ACCESSIBLE_NOW')}

Favors state/county/city/education/utility without CAGE/DIBBS/SAM Opportunities API.
Evidence gates unchanged.
""",
        "phase_l17_source_gaps.md": f"""# L.17 Source Gaps

Unknown states: {summary.get('geographic_gaps', {}).get('unknown_states')}

Unknown counties by state (top): {summary.get('geographic_gaps', {}).get('unknown_counties_by_state_top')}

Status breakdown: {json.dumps(summary.get('coverage_status_counts'), indent=2)}
""",
        "phase_l17_legacy_cleanup.md": """# L.17 Legacy Cleanup

- Canonical registry: `data/jurisdiction_procurement_registry.json`
- Census inventories: `data/lower48_counties.json`, `data/lower48_municipalities.json`
- Status taxonomy: `discovery/lower48.py`
- Platform map derived from registry — not a second hard-coded state list
- SAM Opportunities API remains parked; BidNet auth history parked; DIBBS = CAGE required
""",
        "phase_l17_regression.md": """# L.17 Regression

Tests: `tests/test_phase_l17_lower48_coverage.py`

Covers Lower-48 completeness, status taxonomy, accessible-now, platform map, queues, no SAM/outreach.
""",
    }
    for name, body in docs.items():
        (DOCS / name).write_text(body, encoding="utf-8")


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--no-refresh-hunt", action="store_true")
    p.add_argument("--max-hunt-sources", type=int, default=24)
    p.add_argument("--skip-municipalities", action="store_true", help="states+counties only (faster)")
    args = p.parse_args()
    summary = run_phase_l17(
        authorize_live=True,
        refresh_hunt=not args.no_refresh_hunt,
        max_hunt_sources=args.max_hunt_sources,
        include_municipalities=not args.skip_municipalities,
    )
    print(
        json.dumps(
            {
                k: summary[k]
                for k in (
                    "verdict",
                    "geographic_inventory",
                    "after",
                    "coverage_percentages",
                    "remaining_bottleneck",
                    "states_mapped",
                )
            },
            indent=2,
            default=str,
        )
    )
