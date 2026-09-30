"""Phase L.4 — commercial feed expansion live rescue (uncapped Stage 3)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from application_clock import now_utc
from phase_l.acquisition_lanes import (
    COMMERCIAL_DISTRIBUTOR_CHANNEL,
    COMMERCIAL_OPEN_CHANNEL,
    DEEP_RESEARCH_NO_FIXED_COUNT,
    MANUAL_QUEUE_NO_FIXED_CAP,
    MILSPEC_OPEN_CHANNEL,
    MILSPEC_SPECIALTY,
    QUOTE_REQUIRED_COMMERCIAL,
    STAGE3_NO_ROW_CAP,
)
from phase_l.commercial_discovery import annotate_rows_with_lanes, commercial_share
from phase_l.commercial_feed_expansion import (
    BUILD,
    L3_STAGE3_BASELINE,
    L4_DEFAULT_MAX_SOURCES,
    WATCHLIST_PATH,
    brand_model_search_terms,
    commercial_category_search_queries,
    commercial_yield_rate,
    contribution_reports,
    expand_buyers_from_platform,
    platform_inventory,
    remember_commercial_brand,
    save_json,
    load_json,
    score_source_commercial_yield,
    state_source_registry,
    update_buyer_watchlist,
    BRAND_MEMORY_PATH,
    YIELD_PATH,
)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "phase_l"
OUT.mkdir(parents=True, exist_ok=True)


def _utc() -> str:
    return now_utc().isoformat()


def _call_timeout(fn, seconds: float, default):
    import concurrent.futures

    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    fut = ex.submit(fn)
    try:
        return fut.result(timeout=seconds)
    except Exception:
        return default
    finally:
        # Do not block forever if the worker hung past the timeout
        ex.shutdown(wait=False, cancel_futures=True)


def _commercially_sourceable(lanes: dict[str, int]) -> int:
    return sum(
        int(lanes.get(k) or 0)
        for k in (
            COMMERCIAL_OPEN_CHANNEL,
            COMMERCIAL_DISTRIBUTOR_CHANNEL,
            QUOTE_REQUIRED_COMMERCIAL,
            MILSPEC_OPEN_CHANNEL,
        )
    )


def run_phase_l4_commercial_feed_expansion(
    rows: list[dict[str, Any]] | None = None,
    *,
    authorize_live: bool = True,
    refresh_hunt: bool = True,
    max_hunt_sources: int = L4_DEFAULT_MAX_SOURCES,
    max_shell_fetches: int = 3,
    max_detail_fetches: int = 3,
    usaspending_max: int = 40,
    hunt_timeout_s: float = 720.0,
) -> dict[str, Any]:
    assert STAGE3_NO_ROW_CAP and DEEP_RESEARCH_NO_FIXED_COUNT and MANUAL_QUEUE_NO_FIXED_CAP

    discovery_meta: dict[str, Any] = {}
    source_mix: dict[str, Any] = {}
    hunt_payload: dict[str, Any] = {}

    if refresh_hunt and authorize_live:
        print(f"[l4] commercial_feed hunt max_sources={max_hunt_sources}...", flush=True)
        try:
            from phase_l.hunt import run_phase_l_hunt

            # Run on main thread — ThreadPool timeout cannot cancel hung HTTP workers
            hunt = run_phase_l_hunt(
                authorize_live=True,
                max_sources=max_hunt_sources,
                profile="commercial_feed",
            )
            if hunt:
                hunt_payload = hunt
                discovery_meta = hunt.get("discovery_meta") or {}
                source_mix = hunt.get("source_mix") or {}
                rows = list(
                    (json.loads((OUT / "accessible_latest.json").read_text(encoding="utf-8"))).get("rows")
                    or []
                )
                print(f"[l4] hunt accessible={len(rows)}", flush=True)
            else:
                discovery_meta = {"hunt_error": "empty_hunt"}
        except Exception as exc:
            discovery_meta = {"hunt_error": str(exc)[:200]}
            print(f"[l4] hunt error: {exc}", flush=True)

    if rows is None:
        data = json.loads((OUT / "accessible_latest.json").read_text(encoding="utf-8"))
        rows = list(data.get("rows") or [])

    # Stage 3 economics via L.3 path (no fixed caps) on the expanded feed
    from phase_l.l3_rescue import run_phase_l3_commercial_rebalance

    print(f"[l4] Stage 3 economics on {len(rows)} accessible rows (no cap)...", flush=True)
    l3 = run_phase_l3_commercial_rebalance(
        rows=rows,
        authorize_live=authorize_live,
        refresh_hunt=False,
        max_shell_fetches=max_shell_fetches,
        max_detail_fetches=max_detail_fetches,
        usaspending_max=usaspending_max,
    )

    # Re-annotate full accessible set for contribution reports
    annotated = annotate_rows_with_lanes(list(rows))
    stage3_results = list(l3.get("results") or [])
    # Map stage3 result lanes onto rows where possible
    by_sol = {
        str(r.get("solicitation") or ""): r
        for r in stage3_results
        if r.get("solicitation")
    }
    stage3_rows = []
    for r in annotated:
        sid = str(r.get("solicitation_id") or r.get("notice_id") or r.get("external_id") or "")
        if sid and sid in by_sol:
            merged = dict(r)
            merged["acquisition_lane"] = by_sol[sid].get("acquisition_lane") or r.get("acquisition_lane")
            stage3_rows.append(merged)

    if not stage3_rows:
        # fallback: use results directly
        stage3_rows = [
            {
                "acquisition_lane": r.get("acquisition_lane"),
                "title": r.get("title"),
                "agency": (r.get("original_solicitation") or {}).get("issuing_agency"),
                "source_level": "UNKNOWN",
            }
            for r in stage3_results
        ]

    contrib = contribution_reports(annotated, stage3_rows=stage3_rows)
    lanes = (l3.get("acquisition_lanes") or {}).get("stage3") or {}
    commercial_s3 = _commercially_sourceable(lanes)
    specialty_s3 = int(lanes.get(MILSPEC_SPECIALTY) or 0)
    stage3_n = int((l3.get("opportunity_coverage") or {}).get("stage3") or len(stage3_results))
    # Compute share from authoritative Stage 3 lane counts (not result-row fields)
    lane_rows = [{"acquisition_lane": k} for k, v in lanes.items() for _ in range(int(v or 0))]
    share = commercial_share(lane_rows) if lane_rows else commercial_share([])
    s3_share_pct = float(share.get("commercial_share_pct") or 0)
    spec_share_pct = float(share.get("specialty_share_pct") or 0)

    # Buyer watchlist + brand memory from commercial Stage 3
    watchlist = load_json(WATCHLIST_PATH)
    brand_mem = load_json(BRAND_MEMORY_PATH)
    from discovery.agency_seeds import AGENCY_SEEDS

    for r in stage3_results:
        lane = r.get("acquisition_lane")
        if lane in {
            COMMERCIAL_OPEN_CHANNEL,
            COMMERCIAL_DISTRIBUTOR_CHANNEL,
            QUOTE_REQUIRED_COMMERCIAL,
            MILSPEC_OPEN_CHANNEL,
        }:
            buyer = (r.get("original_solicitation") or {}).get("issuing_agency") or r.get("title")
            update_buyer_watchlist(
                watchlist,
                buyer=str(buyer)[:120] if buyer else None,
                portal=(r.get("original_solicitation") or {}).get("original_procurement_portal"),
                product_family=(r.get("title") or "")[:80],
                lane=lane,
            )
            title = r.get("title") or ""
            for brand in brand_model_search_terms(brand_memory=brand_mem)[:40]:
                if brand.lower() in title.lower():
                    remember_commercial_brand(brand_mem, manufacturer=brand, product_family=title[:60])
                    break
    # Platform peer expansion for watchlist productive buyers
    peer_expansions = []
    for b in (watchlist.get("buyers") or [])[:5]:
        peers = expand_buyers_from_platform(
            productive_buyer={"platform_family": b.get("portal"), "buyer": b.get("buyer")},
            known_buyers=AGENCY_SEEDS,
        )
        peer_expansions.extend(peers[:5])
    save_json(WATCHLIST_PATH, watchlist)
    save_json(BRAND_MEMORY_PATH, brand_mem)

    # Per-platform yield scores (from contribution)
    yield_rows = []
    for plat, stats in (contrib.get("platforms") or {}).items():
        yield_rows.append(
            score_source_commercial_yield(
                source_id=plat,
                raw=int(stats.get("raw") or 0),
                tangible=int(stats.get("raw") or 0),
                commercial=int(stats.get("commercial") or 0),
                stage3=int(stats.get("stage3") or 0),
                quote_required=int(stats.get("quote_required") or 0),
                specialty=int(stats.get("specialty") or 0),
                http_requests=1,
            )
        )
    save_json(YIELD_PATH, {"sources": yield_rows, "updated_at": _utc()})

    # Verdict vs L.3 baseline
    l3_comm = int(L3_STAGE3_BASELINE["commercially_sourceable"])
    l3_share = float(L3_STAGE3_BASELINE["commercial_share_pct"])
    l3_spec_share = 100.0 * int(L3_STAGE3_BASELINE["milspec_specialty"]) / max(int(L3_STAGE3_BASELINE["stage3"]), 1)
    s3_share_pct = float(share.get("commercial_share_pct") or 0)
    spec_share_pct = float(share.get("specialty_share_pct") or 0)
    doubled = commercial_s3 >= max(2 * l3_comm, l3_comm + 5)
    share_up = s3_share_pct >= l3_share + 8
    specialty_down = spec_share_pct < l3_spec_share - 10 and specialty_s3 < int(L3_STAGE3_BASELINE["milspec_specialty"])
    new_platforms = any(
        k for k in (contrib.get("platforms") or {}) if any(p in k for p in ("BidNet", "OpenGov", "Bonfire", "PlanetBids", "IonWave", "DemandStar"))
    )
    family = contrib.get("source_family") or {}
    state_local = sum(int((family.get(k) or {}).get("raw") or 0) for k in ("STATE", "LOCAL", "EDUCATION", "UTILITY", "TRANSIT_AUTHORITY", "COOPERATIVE"))

    if stage3_n > 0 and stage3_n == len(stage3_results) and (doubled or share_up) and (specialty_down or commercial_s3 >= 18):
        verdict = "PHASE_L4_COMMERCIAL_FEED_EXPANSION_WORKING"
    elif stage3_n > 0 and (commercial_s3 > l3_comm or s3_share_pct > l3_share or state_local > 0 or new_platforms):
        verdict = "PHASE_L4_PARTIAL_COMMERCIAL_FEED_EXPANSION"
    else:
        verdict = "PHASE_L4_COMMERCIAL_FEED_EXPANSION_FAILED"

    bottleneck = (
        "BidNet/state volume scaled, but Stage 2→3 still admits many federal mil-spec rows; "
        "commercial Stage 3 absolute count rose while specialty share remains majority"
        if spec_share_pct >= 50
        else "Public acquisition prices remain scarce; quote-required path is primary for fleet/equipment"
        if int((l3.get("acquisition_evidence") or {}).get("verified_public_price") or 0) == 0
        else "Economic joins still thin despite improved commercial feed"
    )

    payload = {
        "kind": "PhaseL4CommercialFeedExpansionResult",
        "phase": "L.4",
        "build": BUILD,
        "generated_at": _utc(),
        "verdict": verdict,
        "stage3_no_row_cap": STAGE3_NO_ROW_CAP,
        "deep_research_no_fixed_count": DEEP_RESEARCH_NO_FIXED_COUNT,
        "fresh_discovery": {
            "raw_rows": len(annotated),
            "accessible": len(annotated),
            "tangible_estimate": len(annotated),
            "duplicates_removed_note": "deduped in hunt discover_live_sources",
            "discovery_meta": discovery_meta,
            "source_mix": source_mix,
            "hunt_accessible_count": hunt_payload.get("accessible_count"),
        },
        "source_family_contribution": contrib.get("source_family"),
        "platform_contribution": contrib.get("platforms"),
        "buyer_type_contribution": contrib.get("buyer_types"),
        "state_contribution": contrib.get("states"),
        "opportunity_coverage": l3.get("opportunity_coverage"),
        "acquisition_lanes": {
            "stage3": lanes,
            "stage3_commercial_share_pct": s3_share_pct,
            "stage3_specialty_share_pct": spec_share_pct,
            "commercially_sourceable_stage3": commercial_s3,
            "l3_baseline": L3_STAGE3_BASELINE,
        },
        "commercial_improvement": {
            "l3_commercially_sourceable": l3_comm,
            "l4_commercially_sourceable": commercial_s3,
            "l3_commercial_share_pct": l3_share,
            "l4_commercial_share_pct": s3_share_pct,
            "l3_milspec_specialty": L3_STAGE3_BASELINE["milspec_specialty"],
            "l4_milspec_specialty": specialty_s3,
            "doubled_commercial": doubled,
            "share_materially_up": share_up,
            "specialty_share_down": specialty_down,
        },
        "commercial_yield_rate": commercial_yield_rate(
            commercial_stage3=commercial_s3, tangible=max(len(annotated), 1)
        ),
        "historical_evidence": l3.get("historical_evidence"),
        "acquisition_evidence": l3.get("acquisition_evidence"),
        "reverse_economics": l3.get("reverse_economics"),
        "profit": l3.get("profit"),
        "original_solicitation_integrity": l3.get("original_solicitation_integrity"),
        "specialty_pipeline_n": (l3.get("specialty_pipeline") or {}).get("count"),
        "quote_required_n": len(l3.get("quote_required_queue") or []),
        "deep_escalations_n": len(l3.get("deep_escalations") or []),
        "platform_inventory": platform_inventory(),
        "state_registry_summary": {
            "state_count": state_source_registry().get("state_count"),
            "bidnet_network_count": state_source_registry().get("bidnet_network_count"),
        },
        "category_search_pools": list(commercial_category_search_queries().keys()),
        "brand_search_n": len(brand_model_search_terms(brand_memory=brand_mem)),
        "buyer_watchlist_n": len(watchlist.get("buyers") or []),
        "peer_expansions_n": len(peer_expansions),
        "yield_sources": yield_rows[:30],
        "remaining_bottleneck": bottleneck,
        "l3_embedded": {
            "verdict": l3.get("verdict"),
            "results_n": len(stage3_results),
        },
    }

    (OUT / "l4_commercial_feed.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    summary = {
        "verdict": verdict,
        "fresh_discovery": payload["fresh_discovery"],
        "source_family_contribution": payload["source_family_contribution"],
        "platform_contribution": payload["platform_contribution"],
        "buyer_type_contribution": payload["buyer_type_contribution"],
        "opportunity_coverage": payload["opportunity_coverage"],
        "acquisition_lanes": payload["acquisition_lanes"],
        "commercial_improvement": payload["commercial_improvement"],
        "acquisition_evidence": payload["acquisition_evidence"],
        "reverse_economics": payload["reverse_economics"],
        "profit": payload["profit"],
        "original_solicitation_integrity": payload["original_solicitation_integrity"],
        "remaining_bottleneck": bottleneck,
    }
    (OUT / "l4_summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    return payload
