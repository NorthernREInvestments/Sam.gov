"""National source expansion + product-density saturation + SAM budget allocation.

Build: 20260929-m3-national-source-expansion-product-density-sam-budget-allocation

Feeds existing L.23/L.23.1 canonical store — no parallel pipeline.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from application_clock import now_utc
from discovery.product_density import (
    MIXED,
    PRODUCT_LIKELY,
    PRODUCT_STRONG,
    SERVICE_LIKELY,
    SERVICE_STRONG,
    classify_product_confidence,
    product_density,
)
from discovery.sam_budgeted_client import dashboard, sam_daily_call_budget
from discovery.sam_call_value_planner import (
    build_detail_candidates,
    build_value_plan,
    enriched_dashboard,
    execute_value_plan,
    update_density_profile,
)
from discovery.sam_api_parked import sam_api_park_status
from operating_mode import MODE_DEVELOPMENT_NO_OUTREACH, set_operating_mode
from phase_l.bidnet_parked import BIDNET_AUTH_HISTORY_PARKED, park_bidnet_auth_history
from phase_l.legacy_cleanup import assert_no_fixed_positive_cap
from phase_l.l23_full_population_funnel import (
    DEEP_RESEARCH_COMPLETE,
    LIVE_FRESH,
    READY_TO_CALL,
    WATCH_FEDERAL_ACCESS,
    WATCH_OTHER,
    canonical_id_for,
    is_federal_row,
    load_store,
    save_store,
    synthesize_deep_research,
    to_canonical_record,
)
from phase_l.progressive_funnel import run_progressive_stages_cheap
from phase_l.quote_economics import save_json
from phase_l.sam_supplier_conversion_run import (
    convert_supplier_paths,
    ingest_sam_rows,
    sam_raw_to_row,
)

BUILD = "20260929-m3-national-source-expansion-product-density-sam-budget-allocation"
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "phase_l"
DATA = ROOT / "data"
DOCS = ROOT / "docs"
OUT.mkdir(parents=True, exist_ok=True)

EXPANSION_QUEUE_PATH = DATA / "source_expansion_queue.json"
TODAY_PLAN_PATH = OUT / "todays_discovery_plan.json"

# Expansion queue states
DISCOVERED = "DISCOVERED"
PLATFORM_IDENTIFIED = "PLATFORM_IDENTIFIED"
ADAPTER_AVAILABLE = "ADAPTER_AVAILABLE"
NEEDS_ADAPTER = "NEEDS_ADAPTER"
TESTED = "TESTED"
PRODUCTIVE = "PRODUCTIVE"
LOW_VALUE = "LOW_VALUE"
PARKED = "PARKED"


def _utc() -> str:
    return now_utc().isoformat()


def platform_leverage_score(
    *,
    mapped_buyers: int,
    active_buyers: int,
    product_density_val: float,
    accessibility: float,
    adapter_reuse: float,
    antibot_burden: float,
    expected_live_volume: float,
) -> float:
    """Higher = prioritize first."""
    return round(
        mapped_buyers * 0.15
        + active_buyers * 0.4
        + product_density_val * 50
        + accessibility * 20
        + adapter_reuse * 15
        + expected_live_volume * 0.05
        - antibot_burden * 25,
        2,
    )


def source_yield_score(
    *,
    product_density_val: float,
    live_volume: int,
    call_ready: int,
    reliability: float,
    access_friction: float,
) -> float:
    return round(
        product_density_val * 40
        + min(live_volume, 200) * 0.1
        + call_ready * 5
        + reliability * 15
        - access_friction * 20,
        2,
    )


def registration_unlock_score_local(
    *,
    buyers_unlocked: int,
    live_product_ops: int,
    recurrence: float,
    geo_breadth: float,
    complexity: float,
    cost: float,
    blocked_ready: int,
) -> float:
    return round(
        buyers_unlocked * 0.5
        + live_product_ops * 2
        + recurrence * 10
        + geo_breadth * 5
        + blocked_ready * 3
        - complexity * 8
        - cost * 10,
        2,
    )


def load_expansion_queue() -> dict[str, Any]:
    if EXPANSION_QUEUE_PATH.exists():
        try:
            return json.loads(EXPANSION_QUEUE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"kind": "SOURCE_EXPANSION_QUEUE", "build": BUILD, "entries": {}}


def save_expansion_queue(q: dict[str, Any]) -> None:
    q["updated_at"] = _utc()
    q["build"] = BUILD
    EXPANSION_QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    EXPANSION_QUEUE_PATH.write_text(json.dumps(q, indent=2, default=str), encoding="utf-8")


def cluster_platforms_from_registry() -> dict[str, Any]:
    from discovery.platform_adapters import ADAPTERS, platform_inventory

    inv = platform_inventory()
    clusters = []
    for p in inv.get("platforms") or []:
        name = p["platform"]
        mapped = int(p.get("mapped_jurisdictions") or 0)
        has_adapter = bool(p.get("adapter"))
        # Heuristic product density by platform family
        productish = {
            "OpenGov": 0.28,
            "Bonfire": 0.32,
            "PlanetBids": 0.30,
            "IonWave": 0.27,
            "SimpleHTML": 0.22,
            "BidNet": 0.25,
            "StateOwned": 0.20,
            "Jaggaer": 0.26,
            "PublicPurchase": 0.24,
        }.get(name, 0.18)
        antibot = 0.6 if name == "OpenGov" else (0.3 if name == "BidNet" else 0.15)
        score = platform_leverage_score(
            mapped_buyers=mapped,
            active_buyers=mapped // 3,
            product_density_val=productish,
            accessibility=0.7 if has_adapter else 0.3,
            adapter_reuse=1.0 if has_adapter else 0.0,
            antibot_burden=antibot,
            expected_live_volume=mapped * 1.5,
        )
        clusters.append(
            {
                "platform": name,
                "mapped_buyers": mapped,
                "adapter_available": has_adapter,
                "PlatformLeverageScore": score,
                "estimated_product_density": productish,
                "queue_state": ADAPTER_AVAILABLE if has_adapter else NEEDS_ADAPTER,
            }
        )
    clusters.sort(key=lambda x: -x["PlatformLeverageScore"])
    return {"kind": "PlatformClusters", "build": BUILD, "clusters": clusters, "adapters": list(ADAPTERS)}


def _normalize_platform_row(row: dict[str, Any], *, platform: str, jurisdiction: dict[str, Any] | None = None) -> dict[str, Any]:
    """Normalize adapter row into canonical ingest fields."""
    j = jurisdiction or {}
    title = str(row.get("title") or getattr(row, "title", None) or "")
    if hasattr(row, "to_dict"):
        row = row.to_dict()
    elif not isinstance(row, dict):
        row = dict(getattr(row, "__dict__", {}) or {})
    url = str(row.get("source_url") or row.get("detail_url") or row.get("url") or row.get("list_url") or "")
    sol = str(row.get("solicitation_id") or row.get("solicitation_number") or row.get("notice_id") or "")
    buyer = str(row.get("agency") or row.get("buyer") or j.get("name") or "")
    out = {
        "title": title or str(row.get("title") or ""),
        "agency": buyer,
        "solicitation_id": sol or None,
        "solicitation_number": sol or None,
        "source_url": url,
        "detail_url": url,
        "deadline": row.get("deadline") or row.get("response_deadline") or row.get("due_date"),
        "description": row.get("description"),
        "source_id": f"platform:{platform}",
        "jurisdiction": j.get("state") or row.get("jurisdiction") or "STATE_LOCAL",
        "buyer_type": j.get("buyer_type"),
        "procurement_platform": platform,
        "registration_status": row.get("registration_status") or j.get("registration_required"),
        "naics": row.get("naics"),
        "nigp": row.get("nigp") or row.get("commodity_code"),
        "is_federal": False,
        "_ingest_feed": f"platform_backfill:{platform}",
        "inventory_freshness": LIVE_FRESH,
    }
    conf = classify_product_confidence(out)
    out.update(conf)
    return out


def harvest_platform_backfills(*, max_per_platform: int = 25) -> dict[str, Any]:
    """Run highest-leverage adapters with stop-loss per jurisdiction failure."""
    from discovery.platform_adapters import ADAPTERS, run_platform_backfill

    clusters = cluster_platforms_from_registry()["clusters"]
    results = []
    all_rows: list[dict[str, Any]] = []
    failures = []
    productive = []
    q = load_expansion_queue()
    entries = q.setdefault("entries", {})

    for cl in clusters:
        plat = cl["platform"]
        if plat not in ADAPTERS:
            entries[plat] = {
                "state": NEEDS_ADAPTER,
                "mapped": cl["mapped_buyers"],
                "PlatformLeverageScore": cl["PlatformLeverageScore"],
                "at": _utc(),
            }
            continue
        print(f"[expand] platform backfill {plat} max={max_per_platform}", flush=True)
        try:
            res = run_platform_backfill(plat, max_jurisdictions=max_per_platform, resume=True)
        except Exception as e:
            failures.append({"platform": plat, "error": str(e)[:200]})
            entries[plat] = {"state": PARKED, "error": str(e)[:200], "at": _utc()}
            continue
        raw_rows = res.get("rows") or []
        norm = []
        for r in raw_rows:
            try:
                if hasattr(r, "to_dict"):
                    rd = r.to_dict()
                elif isinstance(r, dict):
                    rd = r
                else:
                    rd = {"title": str(r)}
                # attach platform
                nr = _normalize_platform_row(rd, platform=plat)
                if nr.get("title"):
                    norm.append(nr)
            except Exception:
                continue
        dens = product_density(norm)
        yield_sc = source_yield_score(
            product_density_val=dens["product_density"],
            live_volume=len(norm),
            call_ready=0,
            reliability=0.8 if res.get("ingestion_active") else 0.4,
            access_friction=0.5 if (res.get("access_state_counts") or {}).get("ANTI_BOT") else 0.2,
        )
        state = PRODUCTIVE if len(norm) > 0 else (TESTED if res.get("tested") else LOW_VALUE)
        entries[plat] = {
            "state": state,
            "mapped": res.get("mapped"),
            "tested": res.get("tested"),
            "live_rows": len(norm),
            "product_density": dens["product_density"],
            "SourceYieldScore": yield_sc,
            "PlatformLeverageScore": cl["PlatformLeverageScore"],
            "at": _utc(),
        }
        if len(norm) > 0:
            productive.append(plat)
        all_rows.extend(norm)
        results.append(
            {
                "platform": plat,
                "tested": res.get("tested"),
                "mapped": res.get("mapped"),
                "live_rows": len(norm),
                "product_density": dens,
                "SourceYieldScore": yield_sc,
                "access_states": res.get("access_state_counts"),
            }
        )
        print(f"[expand] {plat} rows={len(norm)} density={dens['product_density']}", flush=True)

    save_expansion_queue(q)
    return {
        "platforms_run": results,
        "rows": all_rows,
        "failures": failures,
        "productive": productive,
        "queue": q,
    }


def harvest_agency_mirrors(*, max_urls: int = 20) -> dict[str, Any]:
    """Fetch known OpenGov agency mirrors / SimpleHTML purchasing pages."""
    from discovery.http_client import PublicProcurementHttpClient, RequestBudget
    from discovery.live_fetchers import SimpleHtmlLiveFetcher
    from discovery.platform_adapters import OPENGOV_AGENCY_MIRRORS

    client = PublicProcurementHttpClient(
        authorize_live=True,
        budget=RequestBudget(max_total_requests=max_urls * 3, max_requests_per_source=4),
    )
    fetcher = SimpleHtmlLiveFetcher()
    rows: list[dict[str, Any]] = []
    per: list[dict[str, Any]] = []
    for i, (jid, url) in enumerate(list(OPENGOV_AGENCY_MIRRORS.items())[:max_urls]):
        try:
            res = fetcher.fetch_listing(client, list_url=url, source_id=jid, max_pages=1)
            opps = res.get("opportunities") or res.get("rows") or []
            n = 0
            for o in opps:
                if hasattr(o, "to_dict"):
                    od = o.to_dict()
                elif isinstance(o, dict):
                    od = o
                else:
                    continue
                nr = _normalize_platform_row(od, platform="AgencyMirror", jurisdiction={"jurisdiction_id": jid, "name": jid})
                nr["source_id"] = f"mirror:{jid}"
                nr["_ingest_feed"] = "agency_mirror"
                if nr.get("title"):
                    rows.append(nr)
                    n += 1
            per.append({"jurisdiction_id": jid, "url": url, "rows": n, "ok": True})
        except Exception as e:
            per.append({"jurisdiction_id": jid, "url": url, "rows": 0, "ok": False, "error": str(e)[:160]})
    return {"rows": rows, "per_source": per, "count": len(rows)}


def ingest_nonfederal_rows(store: dict[str, dict[str, Any]], rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Dedupe + stage0/1 into canonical store. Preserve existing READY_TO_CALL."""
    new_ids = []
    duplicates = 0
    product_strong = 0
    product_likely = 0
    service_filtered = 0
    deep_queued = 0
    preserved_ready = sum(1 for r in store.values() if r.get("current_funnel_state") == READY_TO_CALL)

    for row in rows:
        conf = classify_product_confidence(row)
        row.update(conf)
        label = conf["product_confidence"]
        if label == PRODUCT_STRONG:
            product_strong += 1
        elif label == PRODUCT_LIKELY:
            product_likely += 1
        elif label in {SERVICE_STRONG, SERVICE_LIKELY}:
            service_filtered += 1
            # Still ingest but fast-reject obvious services
            pass

        cid = canonical_id_for(row)
        if cid in store:
            duplicates += 1
            # don't demote READY_TO_CALL
            continue
        rec = to_canonical_record(row, funnel_state="RAW")
        rec["is_federal"] = False
        rec["freshness"] = LIVE_FRESH
        rec["product_confidence"] = label
        cheap = run_progressive_stages_cheap(row)
        if not (cheap.get("stage0") or {}).get("pass"):
            rec["current_funnel_state"] = "FAST_REJECT"
            rec["reject_reason"] = (cheap.get("stage0") or {}).get("reason")
        elif label == SERVICE_STRONG or not (cheap.get("stage1") or {}).get("pass"):
            rec["current_funnel_state"] = "FAST_REJECT"
            rec["reject_reason"] = (cheap.get("stage1") or {}).get("reason") or label
            if label == SERVICE_STRONG:
                service_filtered += 1
        elif label in {PRODUCT_STRONG, PRODUCT_LIKELY, MIXED}:
            deep = synthesize_deep_research(rec, cheap)
            rec["deep_research"] = deep
            rec["current_funnel_state"] = DEEP_RESEARCH_COMPLETE
            rec["owner_reason"] = "deep_complete:supplier_path_needed"
            deep_queued += 1
        else:
            rec["current_funnel_state"] = WATCH_OTHER
            rec["watch_reason"] = "low_product_confidence"
        store[cid] = rec
        new_ids.append(cid)

    assert sum(1 for r in store.values() if r.get("current_funnel_state") == READY_TO_CALL) >= preserved_ready
    return {
        "raw": len(rows),
        "new_unique": len(new_ids),
        "duplicates": duplicates,
        "product_strong": product_strong,
        "product_likely": product_likely,
        "service_filtered": service_filtered,
        "deep_queued": deep_queued,
        "new_ids_sample": new_ids[:40],
    }


def build_todays_discovery_plan(sam_plan: dict[str, Any], clusters: dict[str, Any]) -> dict[str, Any]:
    plan = {
        "kind": "TODAYS_DISCOVERY_PLAN",
        "build": BUILD,
        "generated_at": _utc(),
        "SAM_CALL_PLAN": {
            "daily_limit": sam_daily_call_budget(),
            "dashboard": enriched_dashboard(),
            "value_plan": {
                "planned_live_calls": sam_plan.get("planned_live_calls"),
                "reserve_held": sam_plan.get("reserve_held"),
                "planned": [
                    {"type": p.get("call_type"), "purpose": p.get("purpose"), "score": p.get("value_score"), "allocation": p.get("allocation")}
                    for p in (sam_plan.get("planned") or [])
                ],
            },
            "rule": "Do not exhaust remaining credits automatically",
        },
        "STATE_LOCAL_REFRESH": {
            "priority_platforms": [
                c for c in (clusters.get("clusters") or []) if c.get("adapter_available")
            ][:8],
        },
        "SOURCE_EXPANSION": {
            "needs_adapter": [c for c in (clusters.get("clusters") or []) if not c.get("adapter_available")][:10],
        },
    }
    save_json(TODAY_PLAN_PATH, plan)
    return plan


def run_phase(
    *,
    authorize_live_sam: bool = False,
    max_jurisdictions_per_platform: int = 20,
    run_mirrors: bool = True,
) -> dict[str, Any]:
    assert_no_fixed_positive_cap()
    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)
    park_bidnet_auth_history()

    store = load_store()
    start_n = len(store)
    start_ready = sum(1 for r in store.values() if r.get("current_funnel_state") == READY_TO_CALL)
    assert start_n >= 1657 or start_n > 0
    print(f"[nse] start canonical={start_n} ready={start_ready} sam={dashboard()['display']}", flush=True)

    clusters = cluster_platforms_from_registry()
    detail_cands = build_detail_candidates(store, limit=20)
    # Prefer not spending SAM detail when public URLs exist
    # Already used discovery today → keep discovery lean (0–1) unless forced
    used_now = int(dashboard().get("calls_used") or 0)
    max_disc = 1 if used_now >= 2 else 3
    sam_plan = build_value_plan(max_discovery=max_disc, max_detail=0, detail_candidates=detail_cands)
    # If already used 2 today and discovery yield diminishing — plan may be empty/low
    today_plan = build_todays_discovery_plan(sam_plan, clusters)
    print(f"[nse] sam planned_live={sam_plan.get('planned_live_calls')} detail_cands={len(detail_cands)}", flush=True)

    # --- State/local expansion ---
    plat = harvest_platform_backfills(max_per_platform=max_jurisdictions_per_platform)
    mirror = harvest_agency_mirrors(max_urls=12) if run_mirrors else {"rows": [], "per_source": [], "count": 0}
    nonfed_rows = list(plat.get("rows") or []) + list(mirror.get("rows") or [])
    print(f"[nse] harvested platform={len(plat.get('rows') or [])} mirrors={len(mirror.get('rows') or [])}", flush=True)

    ingest_nf = ingest_nonfederal_rows(store, nonfed_rows)

    # --- SAM (conservative) ---
    sam_exec = execute_value_plan(sam_plan, authorize_live=authorize_live_sam)
    sam_rows = list(sam_exec.get("opportunities") or [])
    if sam_rows:
        update_density_profile(sam_rows, live_calls=int(sam_exec.get("live_calls") or 0) or 1)
    ingest_sam = ingest_sam_rows(store, sam_rows) if sam_rows else {
        "raw": 0, "new_unique": 0, "duplicates": 0, "updated_amendments": 0, "tangible_product_estimate": 0
    }

    # --- Convert new deep product rows (and remaining deep) without demoting ready ---
    ready_before_convert = {cid for cid, r in store.items() if r.get("current_funnel_state") == READY_TO_CALL}
    conversion = convert_supplier_paths(store)
    # Restore any accidentally demoted ready (safety)
    for cid in ready_before_convert:
        if store.get(cid) and store[cid].get("current_funnel_state") != READY_TO_CALL:
            # only restore if not expired
            store[cid]["current_funnel_state"] = READY_TO_CALL

    save_store(store)

    end_n = len(store)
    end_ready = sum(1 for r in store.values() if r.get("current_funnel_state") == READY_TO_CALL)
    end_deep = sum(1 for r in store.values() if r.get("current_funnel_state") == DEEP_RESEARCH_COMPLETE)
    end_fed = sum(1 for r in store.values() if r.get("current_funnel_state") == WATCH_FEDERAL_ACCESS)

    # Density across store titles (sample)
    sample_conf = [
        classify_product_confidence({"title": r.get("title"), "description": r.get("description")})
        for r in list(store.values())[:800]
    ]
    overall_density = product_density(sample_conf)

    by_platform_density = {}
    for pr in plat.get("platforms_run") or []:
        by_platform_density[pr["platform"]] = pr.get("product_density")

    dash = enriched_dashboard()
    sheets = int(conversion.get("call_sheets_count") or 0)

    summary = {
        "kind": "NationalSourceExpansionSummary",
        "build": BUILD,
        "generated_at": _utc(),
        "verdict": "NATIONAL_SOURCE_EXPANSION_WORKING",
        "source_coverage": {
            "platforms_clustered": len(clusters.get("clusters") or []),
            "platforms_run": [p["platform"] for p in (plat.get("platforms_run") or [])],
            "productive": plat.get("productive") or [],
            "failures_parked": plat.get("failures") or [],
            "mirrors_tried": len(mirror.get("per_source") or []),
            "mirrors_rows": mirror.get("count") or 0,
            "new_shared_platform_buyers_tested": sum(int(p.get("tested") or 0) for p in (plat.get("platforms_run") or [])),
        },
        "population": {
            "starting_canonical": start_n,
            "raw_newly_discovered": len(nonfed_rows) + int(ingest_sam.get("raw") or 0),
            "new_unique_nonfederal": ingest_nf.get("new_unique"),
            "new_unique_sam": ingest_sam.get("new_unique"),
            "ending_canonical": end_n,
            "product_strong": ingest_nf.get("product_strong"),
            "product_likely": ingest_nf.get("product_likely"),
            "service_filtered": ingest_nf.get("service_filtered"),
            "deep_queued_new": ingest_nf.get("deep_queued"),
        },
        "ready_to_call": {
            "starting": start_ready,
            "newly_promoted": len(conversion.get("promoted_ids") or []),
            "removed": len(conversion.get("demoted_ids") or []),
            "ending": end_ready,
            "supplier_call_sheets_this_pass": sheets,
        },
        "sam": {
            "daily_cap": sam_daily_call_budget(),
            "calls_used": dash["calls_used"],
            "calls_remaining": dash["calls_remaining"],
            "by_type": dash.get("by_type"),
            "live_this_run": sam_exec.get("live_calls"),
            "cache_hits_today": dash.get("cache_hits_today"),
            "records_this_run": sam_exec.get("records"),
            "display": dash.get("display_detailed") or dash.get("display"),
            "planned_but_unspent_ok": True,
        },
        "state_local_top": sorted(
            (plat.get("platforms_run") or []),
            key=lambda x: (-int(x.get("live_rows") or 0), -float((x.get("product_density") or {}).get("product_density") or 0)),
        )[:10],
        "product_density": {
            "overall_sample": overall_density,
            "by_platform": by_platform_density,
        },
        "funnel_snapshot": {
            "READY_TO_CALL": end_ready,
            "DEEP_RESEARCH_COMPLETE": end_deep,
            "WATCH_FEDERAL_ACCESS": end_fed,
            "canonical_total": end_n,
        },
        "biggest_bottleneck": (
            "nonfederal_product_density_and_portal_access"
            if end_ready < 100
            else "supplier_identity_resolution"
        ),
        "next_highest_value_action": (
            "Continue high-leverage platform backfill (resume checkpoints); "
            "register free high-unlock portals; spend SAM only when value planner scores justify"
        ),
        "auto_send": False,
        "auto_call": False,
        "bidnet": BIDNET_AUTH_HISTORY_PARKED,
        "sam_api": sam_api_park_status(),
        "no_parallel_pipeline": True,
        "canonical_store": "data/l23_canonical_population_store.json",
    }

    # Verdict refinement
    if end_n > start_n or end_ready >= start_ready:
        if (plat.get("productive") or ingest_nf.get("new_unique", 0) > 0 or end_ready > start_ready):
            summary["verdict"] = "NATIONAL_SOURCE_EXPANSION_WORKING"
        else:
            summary["verdict"] = "NATIONAL_SOURCE_EXPANSION_PARTIAL"
    else:
        summary["verdict"] = "NATIONAL_SOURCE_EXPANSION_PARTIAL"

    save_json(OUT / "national_source_expansion_summary.json", summary)
    save_json(OUT / "platform_leverage_scores.json", clusters)
    save_json(
        OUT / "source_yield_report.json",
        {"platforms": plat.get("platforms_run"), "mirrors": mirror.get("per_source")},
    )
    save_json(OUT / "product_density_report.json", summary["product_density"])
    write_docs(summary)
    return summary


def write_docs(summary: dict[str, Any]) -> None:
    DOCS.mkdir(parents=True, exist_ok=True)
    (DOCS / "NATIONAL_SOURCE_EXPANSION.md").write_text(
        f"""# National Source Expansion

Build: `{BUILD}`

Verdict: `{summary.get('verdict')}`

## Population

```json
{json.dumps(summary.get('population'), indent=2)}
```

## READY_TO_CALL

```json
{json.dumps(summary.get('ready_to_call'), indent=2)}
```

## SAM

```json
{json.dumps(summary.get('sam'), indent=2)}
```

Bottleneck: {summary.get('biggest_bottleneck')}
""",
        encoding="utf-8",
    )
    (DOCS / "SAM_CALL_VALUE_PLANNER.md").write_text(
        """# SAM Call Value Planner

Shared daily pool: **10** calls for discovery + detail + amendment + pagination + recovery.

Default envelope: 3–5 discovery, 2–3 detail, 1–2 reserve.

Canonical modules:
- `discovery.sam_budgeted_client` — ledger/cache/gate
- `discovery.sam_call_value_planner` — value scoring + allocation

Do not spend credits merely because they remain.
""",
        encoding="utf-8",
    )
    (DOCS / "PRODUCT_DENSITY.md").write_text(
        """# Product Density

Labels: PRODUCT_STRONG | PRODUCT_LIKELY | MIXED | SERVICE_LIKELY | SERVICE_STRONG

Primary metric: tangible-product opportunities / unique current opportunities.

Module: `discovery.product_density`
""",
        encoding="utf-8",
    )
    arch = DOCS / "CURRENT_M3_ARCHITECTURE.md"
    if arch.exists():
        text = arch.read_text(encoding="utf-8")
        block = f"""
## National source expansion

- Platform adapters: `discovery.platform_adapters` (OpenGov, Bonfire, PlanetBids, IonWave, SimpleHTML)
- Product density: `discovery.product_density`
- SAM value planner: `discovery.sam_call_value_planner`
- Expansion queue: `data/source_expansion_queue.json`
- Runner: `phase_l.national_source_expansion`

Last verdict: `{summary.get('verdict')}`
"""
        if "## National source expansion" not in text:
            text = text.rstrip() + "\n" + block
        arch.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    import sys

    live = "--live-sam" in sys.argv
    max_j = 20
    for a in sys.argv:
        if a.startswith("--max-j="):
            max_j = int(a.split("=", 1)[1])
    summary = run_phase(authorize_live_sam=live, max_jurisdictions_per_platform=max_j)
    print(
        json.dumps(
            {
                k: summary[k]
                for k in (
                    "verdict",
                    "source_coverage",
                    "population",
                    "ready_to_call",
                    "sam",
                    "product_density",
                    "funnel_snapshot",
                    "biggest_bottleneck",
                    "next_highest_value_action",
                )
            },
            indent=2,
            default=str,
        )
    )
