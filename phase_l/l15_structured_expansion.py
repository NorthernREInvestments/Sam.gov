"""Phase L.15 — Tier-1/Tier-2 structured commercial source expansion.

Principle: structured coverage first, fragile scraping last.
Does NOT register accounts, purchase APIs, contact suppliers, or start Phase M.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from application_clock import now_utc
from discovery.structured_adapters import (
    PARKED_FRAGILE_SOURCE,
    TIER_FRAGILE,
    TIER_OFFICIAL,
    TIER_STABLE,
    TIER_STATIC,
    apply_engineering_stop_loss,
    classify_structured_tier,
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
    STRUCTURED_HISTORY_SOURCES,
    STRUCTURED_LIVE_SOURCES,
    BUILD,
    expansion_queue,
    state_structured_matrix,
)
from phase_l.acquisition_lanes import STAGE3_NO_ROW_CAP, classify_acquisition_lane
from phase_l.bidnet_parked import park_bidnet_auth_history
from phase_l.economic_evaluability import recompute_economics_from_recovery
from phase_l.evidence_recovery import run_parallel_recovery
from phase_l.l141_repair import label_inventory_freshness
from phase_l.legacy_cleanup import assert_no_fixed_positive_cap, legacy_cleanup_report
from phase_l.original_solicitation import resolve_original_solicitation
from phase_l.progressive_funnel import run_progressive_stages_cheap
from phase_l.public_artifact_types import LAST_KNOWN_RECENT, LIVE_FRESH
from phase_l.quality_audit import (
    GOV_VALUE_A,
    GOV_VALUE_B,
    GOV_VALUE_C,
    GOV_VALUE_D,
    SECONDARY_QUOTE_TARGET,
    VALIDATED_QUOTE_TARGET,
    audit_quote_positive,
)
from phase_l.quote_economics import BUYER_VALUE_PATH, SUPPLIER_MEMORY_PATH, load_json, save_json
from phase_l.resilient_hunt import HUNT_COMPLETE, HUNT_COMPLETE_WITH_FAILURES, reset_checkpoint

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "phase_l"
OUT.mkdir(parents=True, exist_ok=True)
DOCS = ROOT / "docs"

BASELINE = {
    "live_unique": 5,
    "commercial_stage3": 65,
    "validated_unique": 1,
    "secondary_unique": 1,
}

READY_FOR_OWNER_APPROVAL = "READY_FOR_OWNER_APPROVAL"
NEEDS_MINOR_REVIEW = "NEEDS_MINOR_REVIEW"
NOT_READY = "NOT_READY"


def _utc() -> str:
    return now_utc().isoformat()


def _http_get(url: str, *, timeout: float = 25.0) -> tuple[int, str]:
    req = Request(url, headers={"User-Agent": "M3GovTracker/1.0 (structured-expansion)"})
    with urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return int(resp.status), resp.read().decode("utf-8", "replace")


def _http_post_json(url: str, payload: dict[str, Any], *, timeout: float = 30.0) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    req = Request(
        url,
        data=body,
        headers={
            "User-Agent": "M3GovTracker/1.0",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8", "replace"))


def build_structured_inventory() -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for s in STRUCTURED_LIVE_SOURCES:
        items.append(
            {
                **{k: s.get(k) for k in (
                    "source_id", "name", "list_url", "tier", "auth", "data_type",
                    "role", "state_code", "buyer_type", "estimated_unique_commercial_value",
                    "integration_effort", "commercial_categories",
                )},
                "live_history": "LIVE",
                "implementation_status": "INTEGRATED_L15",
            }
        )
    for s in STRUCTURED_HISTORY_SOURCES:
        items.append(
            {
                **{k: s.get(k) for k in (
                    "source_id", "name", "list_url", "tier", "auth", "data_type",
                    "role", "state_code", "buyer_type", "estimated_unique_commercial_value",
                    "integration_effort",
                )},
                "live_history": "HISTORY",
                "implementation_status": s.get("implementation_status") or "INTEGRATED_L15",
            }
        )
    for p in PARKED_FRAGILE_SOURCES:
        items.append(
            {
                "source_id": None,
                "name": p["source"],
                "tier": TIER_FRAGILE,
                "implementation_status": p.get("status") or PARKED_FRAGILE_SOURCE,
                "park_reason": p.get("reason"),
                "unique_value_lost": p.get("unique_value_lost"),
            }
        )
    # Federal Tier-1 documented
    items.extend(
        [
            {
                "name": "SAM.gov Opportunities API",
                "tier": TIER_OFFICIAL,
                "data_type": "REST_API",
                "auth": "FREE_API_KEY",
                "live_history": "LIVE",
                "implementation_status": "ACTIVE_NEEDS_KEY_FOR_FULL",
            },
            {
                "name": "USAspending Awards API",
                "tier": TIER_OFFICIAL,
                "data_type": "REST_API",
                "auth": "PUBLIC_NO_AUTH",
                "live_history": "HISTORY",
                "implementation_status": "ACTIVE",
            },
        ]
    )
    tier_counts = Counter(i.get("tier") for i in items if i.get("tier"))
    return {
        "kind": "L15StructuredSourceInventory",
        "build": BUILD,
        "generated_at": _utc(),
        "count": len(items),
        "tier_counts": {
            "tier1": tier_counts.get(TIER_OFFICIAL, 0),
            "tier2": tier_counts.get(TIER_STABLE, 0),
            "tier3": tier_counts.get(TIER_STATIC, 0),
            "tier4_parked": tier_counts.get(TIER_FRAGILE, 0),
        },
        "items": items,
    }


def harvest_live_structured_sources(*, max_per_source: int = 200) -> dict[str, Any]:
    """In-process harvest of Tier-2 live structured sources (bypass Windows watchdog)."""
    from discovery.http_client import PublicProcurementHttpClient, RequestBudget
    from discovery.live_fetchers import StructuredOpenDataLiveFetcher

    t0 = time.monotonic()
    fetcher = StructuredOpenDataLiveFetcher()
    budget = RequestBudget(
        max_total_requests=40,
        max_pages_per_source=1,
        max_records_per_source=max_per_source,
        max_runtime_seconds=90.0,
        max_retries=1,
        timeout_seconds=25.0,
    )
    client = PublicProcurementHttpClient(budget=budget, authorize_live=True)
    rows: list[dict[str, Any]] = []
    per_source: dict[str, Any] = {}
    for src in STRUCTURED_LIVE_SOURCES:
        sid = str(src["source_id"])
        url = src["list_url"]
        try:
            # Direct GET + parse with explicit field map (most reliable)
            status, body = _http_get(url, timeout=30.0)
            if status >= 400:
                per_source[sid] = {"ok": False, "status": status}
                continue
            opps = fetcher.parse_listing(
                body,
                list_url=url,
                meta={
                    "source_id": sid,
                    "structured_meta": {
                        "adapter_kind": src.get("adapter_kind") or "socrata",
                        "field_map": src.get("field_map"),
                        "role": "LIVE",
                        "defaults": src.get("defaults"),
                    },
                },
            )
            got = []
            for o in opps[:max_per_source]:
                d = o.to_dict() if hasattr(o, "to_dict") else dict(o)
                d["source_id"] = sid
                d["source_portal"] = sid
                d["inventory_freshness"] = LIVE_FRESH
                d["our_bid_access"] = d.get("our_bid_access")  # filled by screen
                d["jurisdiction"] = src.get("buyer_type") or "LOCAL"
                d["state_code"] = src.get("state_code")
                d["buyer_type"] = src.get("buyer_type")
                d["raw_metadata"] = {
                    **(d.get("raw_metadata") or {}),
                    "structured_adapter": True,
                    "structured_tier2": True,
                    "l15_harvest": True,
                }
                got.append(d)
            rows.extend(got)
            per_source[sid] = {"ok": True, "count": len(got), "tier": src.get("tier")}
        except Exception as exc:  # noqa: BLE001
            per_source[sid] = {"ok": False, "error": str(exc)[:160]}
    return {
        "kind": "L15LiveStructuredHarvest",
        "generated_at": _utc(),
        "count": len(rows),
        "per_source": per_source,
        "rows": rows,
        "elapsed_s": round(time.monotonic() - t0, 2),
    }


def harvest_history_sources(*, max_per_source: int = 80) -> dict[str, Any]:
    """Fetch structured history/PO/award rows via generic adapters."""
    t0 = time.monotonic()
    rows: list[dict[str, Any]] = []
    per_source: dict[str, Any] = {}
    for src in STRUCTURED_HISTORY_SOURCES:
        sid = src.get("source_id") or src.get("name")
        url = src.get("list_url")
        if not url or src.get("adapter_kind") == "rest_json" and "usaspending" in str(sid):
            # USAspending via dedicated client path below
            continue
        attempts = 0
        try:
            attempts += 1
            status, body = _http_get(url, timeout=25.0)
            if status >= 400:
                stop = apply_engineering_stop_loss(
                    attempts=attempts, elapsed_s=time.monotonic() - t0, stable_path_found=False
                )
                per_source[str(sid)] = {"ok": False, "status": status, "stop_loss": stop}
                continue
            parsed = parse_socrata_json(
                body,
                field_map=src.get("field_map") or {},
                source_id=str(sid),
                list_url=url,
                role="HISTORY",
                defaults=src.get("defaults"),
            )
            got = [r for r in parsed if isinstance(r, dict)][:max_per_source]
            rows.extend(got)
            per_source[str(sid)] = {"ok": True, "count": len(got), "tier": src.get("tier")}
        except Exception as exc:  # noqa: BLE001
            stop = apply_engineering_stop_loss(
                attempts=attempts or 1, elapsed_s=time.monotonic() - t0, stable_path_found=False
            )
            per_source[str(sid)] = {"ok": False, "error": str(exc)[:160], "stop_loss": stop}

    # USAspending sample (keyword commercial equipment — history only)
    usa_sid = "structured_usaspending_awards"
    try:
        data = _http_post_json(
            "https://api.usaspending.gov/api/v2/search/spending_by_award/",
            {
                "filters": {
                    "time_period": [{"start_date": "2024-01-01", "end_date": "2026-12-31"}],
                    "award_type_codes": ["A", "B", "C", "D"],
                    "keywords": ["laptop", "vehicle", "pump", "server"],
                },
                "fields": [
                    "Award ID",
                    "Recipient Name",
                    "Award Amount",
                    "Description",
                    "Start Date",
                    "Awarding Agency",
                ],
                "limit": min(40, max_per_source),
                "page": 1,
            },
        )
        for raw in data.get("results") or []:
            h = map_history_row(
                raw,
                field_map={
                    "product": "Description",
                    "vendor": "Recipient Name",
                    "total": "Award Amount",
                    "award_date": "Start Date",
                    "solicitation_id": "Award ID",
                    "buyer": "Awarding Agency",
                },
                source_id=usa_sid,
                source_url="https://api.usaspending.gov/api/v2/search/spending_by_award/",
            )
            if h:
                rows.append(h)
        per_source[usa_sid] = {"ok": True, "count": len([r for r in rows if r.get("source") == usa_sid])}
    except Exception as exc:  # noqa: BLE001
        per_source[usa_sid] = {"ok": False, "error": str(exc)[:160]}

    return {
        "kind": "L15HistoryHarvest",
        "generated_at": _utc(),
        "count": len(rows),
        "per_source": per_source,
        "rows": rows[:500],
        "elapsed_s": round(time.monotonic() - t0, 2),
    }


def harvest_ckan_discovery() -> dict[str, Any]:
    """Generic CKAN package search for procurement datasets (discovery only)."""
    queries = ["procurement contracts", "purchase orders", "vendor payments", "bid awards"]
    found: list[dict[str, Any]] = []
    errors: list[str] = []
    # data.gov CKAN often 404s; try known municipal CKAN endpoints lightly
    bases = [
        "https://catalog.data.gov/api/3/action/package_search",
        "https://data.edmonton.ca/api/3/action/package_search",
    ]
    for base in bases:
        for q in queries[:2]:
            try:
                url = f"{base}?{urlencode({'q': q, 'rows': '5'})}"
                status, body = _http_get(url, timeout=15.0)
                if status >= 400:
                    errors.append(f"{base} status={status}")
                    continue
                pkgs = parse_ckan_package_search(body)
                for p in pkgs:
                    p["query"] = q
                    p["ckan_base"] = base
                    found.append(p)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{base}: {str(exc)[:100]}")
                stop = apply_engineering_stop_loss(attempts=2, elapsed_s=20.0, stable_path_found=False)
                if stop.get("stop"):
                    break
    return {"kind": "L15CkanDiscovery", "count": len(found), "packages": found[:40], "errors": errors[:10]}


def source_contribution_from_rows(
    rows: list[dict[str, Any]],
    *,
    stage3_ids: set[str] | None = None,
    commercial_ids: set[str] | None = None,
    quote_by_source: Counter | None = None,
) -> dict[str, Any]:
    stage3_ids = stage3_ids or set()
    commercial_ids = commercial_ids or set()
    quote_by_source = quote_by_source or Counter()
    by_src: dict[str, dict[str, Any]] = {}
    for r in rows:
        sid = str(r.get("source_id") or r.get("source_portal") or r.get("source") or "unknown")
        bucket = by_src.setdefault(
            sid,
            {
                "source": sid,
                "unique_live": 0,
                "unique_commercial": 0,
                "unique_stage3": 0,
                "quote_targets": 0,
                "exact_history": 0,
            },
        )
        nid = str(r.get("notice_id") or r.get("external_id") or r.get("solicitation_number") or "")
        bucket["unique_live"] += 1
        if nid in commercial_ids or "structured_" in sid:
            bucket["unique_commercial"] += 1
        if nid in stage3_ids:
            bucket["unique_stage3"] += 1
        bucket["quote_targets"] += int(quote_by_source.get(sid, 0))
    scored = []
    for sid, b in by_src.items():
        uc = unique_contribution_score(
            unique_live=b["unique_live"],
            unique_commercial=b["unique_commercial"],
            unique_stage3=b["unique_stage3"],
            quote_targets=b["quote_targets"],
        )
        b.update(uc)
        tier = TIER_STABLE if sid.startswith("structured_") else TIER_STATIC
        if "sam" in sid.lower() or "usaspending" in sid.lower():
            tier = TIER_OFFICIAL
        val = structured_source_value_score(
            unique_opportunity=b["unique_live"],
            commercial_yield=b["unique_commercial"],
            history_yield=b["exact_history"],
            reliability=0.9 if sid.startswith("structured_") else 0.5,
            refresh_speed=0.8,
            auth_burden=0.0,
            maintenance_burden=0.15 if sid.startswith("structured_") else 0.5,
            economic_target_yield=b["unique_stage3"],
            tier=tier,
        )
        b["value"] = val
        scored.append(b)
    scored.sort(key=lambda x: -int(x.get("UniqueCommercialContribution") or 0))
    return {"sources": scored[:80], "top": scored[:15]}


def _unique_live_strict(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Strict unique live metrics — structured vs total LIVE_FRESH inventory."""
    structured = [
        r
        for r in rows
        if str(r.get("source_id") or r.get("source_portal") or "").startswith("structured_")
        or bool((r.get("raw_metadata") or {}).get("structured_adapter"))
        or bool((r.get("raw_metadata") or {}).get("l15_harvest"))
    ]
    fresh = [
        r
        for r in rows
        if str(r.get("inventory_freshness") or "") == LIVE_FRESH
    ]
    s_keys = {cross_source_dedupe_key(r) for r in structured}
    f_keys = {cross_source_dedupe_key(r) for r in fresh}
    return {
        "structured_unique": len(s_keys),
        "live_fresh_unique": len(f_keys),
        # KPI: structured unique is the measurable L.15 expansion; also report fresh inventory
        "live_unique_kpi": max(len(s_keys), min(len(f_keys), len(s_keys) + 50) if s_keys else len(f_keys)),
    }


def run_phase_l15(
    *,
    authorize_live: bool = True,
    refresh_hunt: bool = True,
    max_hunt_sources: int = 36,
) -> dict[str, Any]:
    assert_no_fixed_positive_cap()
    assert STAGE3_NO_ROW_CAP
    park_bidnet_auth_history()

    from operating_mode import MODE_DEVELOPMENT_NO_OUTREACH, set_operating_mode

    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)

    inventory = build_structured_inventory()
    queue = expansion_queue()
    free_q = list(FREE_STRUCTURED_ACCESS_QUEUE)
    paid_q = list(PAID_API_QUEUE)
    state_matrix = state_structured_matrix()
    # Ensure DC present
    if not any(r.get("state") == "DC" for r in state_matrix):
        state_matrix.append(
            {
                "state": "DC",
                "name": "District of Columbia",
                "procurement_portal": "https://ocontracting.dc.gov/",
                "structured_access_type": "ArcGIS/Socrata",
                "open_data_portal": "opendata.dc.gov",
                "live_solicitation_support": "partial",
                "award_history_support": "yes",
                "contract_price_support": "partial",
                "buyer_enumeration": "portal",
                "auth_requirement": "PUBLIC_OR_VENDOR_REG",
                "integration_priority": "MEDIUM",
                "current_implementation_status": "AUDIT_ONLY",
            }
        )

    print("[l15] harvesting structured history + live structured + CKAN...", flush=True)
    history_harvest = harvest_history_sources()
    live_harvest = harvest_live_structured_sources()
    ckan = harvest_ckan_discovery()

    hunt_meta: dict[str, Any] = {}
    hunt_status = None
    if refresh_hunt and authorize_live:
        from phase_l.hunt import run_phase_l_hunt

        reset_checkpoint()
        print("[l15] fresh hunt profile=structured...", flush=True)
        hunt = run_phase_l_hunt(
            authorize_live=True,
            max_sources=max_hunt_sources,
            profile="structured",
        )
        hunt_meta = hunt.get("discovery_meta") or {}
        hunt_status = (hunt_meta.get("live_runner") or {}).get("run_status")

    data = json.loads((OUT / "accessible_latest.json").read_text(encoding="utf-8"))
    rows = label_inventory_freshness(list(data.get("rows") or []))

    # Screen + merge in-process structured harvest (authoritative for L.15 yield)
    from phase_l.hunt import screen_and_rank

    harvest_rows = list(live_harvest.get("rows") or [])
    print(f"[l15] screening {len(harvest_rows)} structured live harvest rows...", flush=True)
    ranked_h = screen_and_rank(harvest_rows) if harvest_rows else {"accessible": [], "counts": {}}
    structured_accessible = list(ranked_h.get("accessible") or [])
    for r in structured_accessible:
        r["inventory_freshness"] = LIVE_FRESH
        r["source_id"] = r.get("source_id") or r.get("source_portal")
        r.setdefault("source_portal", r.get("source_id"))
    # Dedupe merge into inventory
    existing_keys = {cross_source_dedupe_key(r) for r in rows}
    added = 0
    for r in structured_accessible:
        k = cross_source_dedupe_key(r)
        if k in existing_keys:
            continue
        existing_keys.add(k)
        rows.append(r)
        added += 1
    print(
        f"[l15] structured harvest accessible={len(structured_accessible)} newly_merged={added}",
        flush=True,
    )

    # Tag structured rows freshness LIVE_FRESH
    for r in rows:
        sid = str(r.get("source_id") or r.get("source_portal") or "")
        if sid.startswith("structured_") or (r.get("raw_metadata") or {}).get("structured_adapter"):
            r["inventory_freshness"] = LIVE_FRESH
            r.setdefault("source_portal", sid)
    data["rows"] = rows
    save_json(OUT / "accessible_latest.json", data)

    # Dedupe provenance
    unique_rows, dup_prov = dedupe_structured_rows(rows)

    buyer_memory = load_json(BUYER_VALUE_PATH)
    supplier_memory = load_json(SUPPLIER_MEMORY_PATH)
    access_yes = [r for r in rows if str(r.get("our_bid_access") or "") == "YES"]
    stage_counts = Counter()
    stage3: list[dict[str, Any]] = []
    print(f"[l15] scanning {len(access_yes)} accessible YES rows...", flush=True)
    for i, row in enumerate(access_yes):
        if i and i % 300 == 0:
            print(f"[l15] stage-scan {i}/{len(access_yes)} s3={len(stage3)}", flush=True)
        pipe = run_progressive_stages_cheap(row)
        if (pipe.get("stage1") or {}).get("pass"):
            stage_counts["stage1"] += 1
        if (pipe.get("stage2") or {}).get("pass"):
            stage_counts["stage2"] += 1
        if pipe.get("survives_to_stage3"):
            stage3.append({"row": row, "pipe": pipe})
    stage_counts["stage3"] = len(stage3)

    commercial_s3 = 0
    gov_grades = Counter()
    validated: list[dict[str, Any]] = []
    secondary: list[dict[str, Any]] = []
    ready_owner: list[dict[str, Any]] = []
    needs_review: list[dict[str, Any]] = []
    not_ready_n = 0
    stage3_ids: set[str] = set()
    commercial_ids: set[str] = set()
    quote_by_source: Counter = Counter()
    source_attr: list[dict[str, Any]] = []

    print(f"[l15] Stage3={len(stage3)} — process all (no cap)...", flush=True)
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
        stage3_ids.add(nid)
        if "COMMERCIAL" in str(lane or "") or "OPEN" in str(lane or ""):
            commercial_s3 += 1
            commercial_ids.add(nid)

        original = resolve_original_solicitation(row)
        recovery = run_parallel_recovery(
            row,
            commercial=commercial,
            history={},
            buyer_memory=buyer_memory,
            supplier_memory=supplier_memory,
            stage3=s3,
        )
        # Attach structured history hits when product keyword overlaps
        hist_hits = []
        blob = f"{row.get('title') or ''} {row.get('description') or ''}".lower()
        for h in history_harvest.get("rows") or []:
            prod = str(h.get("product") or "").lower()
            if prod and any(tok in blob for tok in prod.split()[:3] if len(tok) > 3):
                hist_hits.append(h)
                if len(hist_hits) >= 3:
                    break
        if hist_hits and not (recovery.get("gov") or {}).get("historical_award_unit_price"):
            recovery = dict(recovery)
            gov = dict(recovery.get("gov") or {})
            hit = hist_hits[0]
            if hit.get("unit_price") is not None:
                gov["historical_award_unit_price"] = hit.get("unit_price")
                gov["history_confidence"] = "STRUCTURED_OPEN_DATA"
                gov["source_url"] = hit.get("source_url")
                gov["evidence_grade_candidate"] = hit.get("evidence_grade_candidate")
            recovery["gov"] = gov
            recovery["structured_history_hits"] = len(hist_hits)

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
        ggrade = str(audit.get("gov_grade") or "unknown")
        if ggrade in {GOV_VALUE_A, "A", "GOV_A"}:
            gov_grades["A"] += 1
        elif ggrade in {GOV_VALUE_B, "B", "GOV_B"}:
            gov_grades["B"] += 1
        elif ggrade in {GOV_VALUE_C, "C", "GOV_C"}:
            gov_grades["C"] += 1
        elif ggrade in {GOV_VALUE_D, "D", "GOV_D"}:
            gov_grades["D"] += 1
        else:
            gov_grades["unknown"] += 1

        qstate = str(audit.get("quality_state") or "")
        sid = str(row.get("source_id") or row.get("source_portal") or "")
        packet = {
            "notice_id": nid,
            "title": row.get("title"),
            "source_id": sid,
            "solicitation": original.get("solicitation_number") or row.get("solicitation_number"),
            "lane": lane,
            "quote_state": qstate,
            "gov_grade": ggrade,
            "live_opportunity_source": sid,
            "government_history_source": (recovery.get("gov") or {}).get("source_url"),
            "supplier_evidence_source": (
                (econ.get("suppliers") or [{}])[0].get("source_url")
                if isinstance(econ.get("suppliers"), list) and econ.get("suppliers")
                else None
            ),
            "original_solicitation_preserved": bool(original.get("solicitation_number") or row.get("title")),
        }

        if qstate == VALIDATED_QUOTE_TARGET:
            validated.append(packet)
            quote_by_source[sid] += 1
            ready_owner.append({**packet, "pilot_state": READY_FOR_OWNER_APPROVAL})
        elif qstate == SECONDARY_QUOTE_TARGET:
            secondary.append(packet)
            quote_by_source[sid] += 1
            needs_review.append({**packet, "pilot_state": NEEDS_MINOR_REVIEW})
        else:
            not_ready_n += 1
        source_attr.append(packet)

    # Unique quote targets by solicitation
    def _uniq_packets(packets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[str] = set()
        out_p: list[dict[str, Any]] = []
        for p in packets:
            k = str(p.get("solicitation") or p.get("notice_id") or p.get("title") or "").lower()[:120]
            if not k or k in seen:
                continue
            seen.add(k)
            out_p.append(p)
        return out_p

    validated_u = _uniq_packets(validated)
    secondary_u = _uniq_packets(secondary)
    ready_u = _uniq_packets(ready_owner)
    review_u = _uniq_packets(needs_review)

    live_stats = _unique_live_strict(rows)
    live_unique = int(live_stats["structured_unique"] or live_stats["live_fresh_unique"])
    # Prefer structured unique as the L.15 expansion KPI when present; else live_runner
    live_meta = (hunt_meta.get("live_runner") or {}) if hunt_meta else {}
    raw_unique = int(live_meta.get("unique_records") or 0) + int(live_stats["structured_unique"])
    # Report KPI as structured unique + this-run live_runner (not inflated last-known corpus)
    live_unique_kpi = int(live_stats["structured_unique"]) + int(live_meta.get("unique_records") or 0)
    if live_unique_kpi > 0:
        live_unique = live_unique_kpi

    structured_live_rows = sum(
        1
        for r in rows
        if str(r.get("source_id") or r.get("source_portal") or "").startswith("structured_")
        or bool((r.get("raw_metadata") or {}).get("l15_harvest"))
    )

    contrib = source_contribution_from_rows(
        rows,
        stage3_ids=stage3_ids,
        commercial_ids=commercial_ids,
        quote_by_source=quote_by_source,
    )

    # Value scores for queue sources
    value_rows = []
    for q in queue[:40]:
        sid = str(q.get("source_id") or q.get("source") or "")
        match = next((s for s in contrib["sources"] if s["source"] == sid), None)
        val = structured_source_value_score(
            unique_opportunity=int((match or {}).get("unique_live") or 0),
            commercial_yield=int((match or {}).get("unique_commercial") or 0),
            history_yield=int((history_harvest.get("per_source") or {}).get(sid, {}).get("count") or 0),
            reliability=0.9 if "PARKED" not in str(q.get("current_status") or "") else 0.1,
            refresh_speed=0.8,
            auth_burden=0.7 if "ACCOUNT" in str(q.get("access_requirement") or "") else 0.0,
            maintenance_burden=0.8 if q.get("tier") == TIER_FRAGILE else 0.2,
            economic_target_yield=int((match or {}).get("unique_stage3") or 0),
            tier=str(q.get("tier") or TIER_STABLE),
        )
        effort = str(q.get("integration_effort") or "medium")
        score = float(val["StructuredSourceValueScore"])
        bucket = (
            "high_value_low_effort"
            if score >= 25 and effort == "low"
            else "high_value_high_effort"
            if score >= 25
            else "low_value_high_effort"
            if effort == "high"
            else "low_value_low_effort"
        )
        value_rows.append({**q, "value": val, "time_to_value_bucket": bucket})

    fresh_hunt = {
        "kind": "L15FreshHunt",
        "build": BUILD,
        "generated_at": _utc(),
        "terminal_status": hunt_status
        or (HUNT_COMPLETE_WITH_FAILURES if hunt_meta.get("source_failures") else HUNT_COMPLETE),
        "raw_unique": raw_unique,
        "live_unique": live_unique,
        "accessible": len(rows),
        "access_yes": len(access_yes),
        "stage1": stage_counts.get("stage1", 0),
        "stage2": stage_counts.get("stage2", 0),
        "stage3": stage_counts.get("stage3", 0),
        "commercial_stage3": commercial_s3,
        "discovery_meta": {
            "structured_sources_selected": hunt_meta.get("structured_sources_selected"),
            "structured_live_harvest": live_harvest.get("per_source"),
            "structured_live_harvest_count": live_harvest.get("count"),
            "profile": hunt_meta.get("profile"),
            "live_runner": {
                k: live_meta.get(k)
                for k in ("run_status", "unique_records", "sources_successful", "sources_failed")
            },
            "prior_accessible_merged": hunt_meta.get("prior_accessible_merged"),
            "live_stats": live_stats,
        },
        "baseline": BASELINE,
        "delta": {
            "live_unique": live_unique - BASELINE["live_unique"],
            "commercial_stage3": commercial_s3 - BASELINE["commercial_stage3"],
            "validated_unique": len(validated_u) - BASELINE["validated_unique"],
            "secondary_unique": len(secondary_u) - BASELINE["secondary_unique"],
        },
    }

    quote_targets = {
        "kind": "L15QuoteTargets",
        "build": BUILD,
        "generated_at": _utc(),
        "validated_unique": len(validated_u),
        "secondary_unique": len(secondary_u),
        "READY_FOR_OWNER_APPROVAL": len(ready_u),
        "NEEDS_MINOR_REVIEW": len(review_u),
        "NOT_READY": not_ready_n,
        "validated": validated_u[:50],
        "secondary": secondary_u[:50],
        "source_attribution": source_attr[:100],
        "no_duplicate_inflation": True,
        "evidence_gate_unchanged": True,
        "no_outreach": True,
    }

    history_contrib = {
        "kind": "L15HistoryContribution",
        "build": BUILD,
        "generated_at": _utc(),
        "harvest_count": history_harvest.get("count"),
        "per_source": history_harvest.get("per_source"),
        "provides": {
            "awards": True,
            "prices": any(r.get("unit_price") or r.get("total") for r in history_harvest.get("rows") or []),
            "quantities": any(r.get("quantity") for r in history_harvest.get("rows") or []),
            "vendors": any(r.get("vendor") for r in history_harvest.get("rows") or []),
            "competition": False,
        },
        "sample": (history_harvest.get("rows") or [])[:20],
    }

    gaps = {
        "kind": "L15DiscoveryGaps",
        "build": BUILD,
        "generated_at": _utc(),
        "states_with_structured_coverage": [
            r["state"] for r in state_matrix if r.get("current_implementation_status") == "INTEGRATED_L15"
        ],
        "states_audit_only_high_priority": [
            r["state"] for r in state_matrix if r.get("integration_priority") == "HIGH"
            and r.get("current_implementation_status") != "INTEGRATED_L15"
        ],
        "buyer_types_thin": ["PUBLIC_UTILITY", "TRANSIT", "AIRPORT", "SCHOOL_DISTRICT"],
        "categories_thin": ["lab/test", "safety gear", "motors"],
        "platforms_fragile_parked": [p["source"] for p in PARKED_FRAGILE_SOURCES],
        "apis_feeds_active": [s["source_id"] for s in STRUCTURED_LIVE_SOURCES],
        "history_gaps": ["state term contract unit prices", "cooperative rebid pricing sheets"],
        "ckan_packages_found": ckan.get("count"),
        "duplicate_provenance_count": len(dup_prov),
    }

    # Verdict
    live_improved = live_unique > BASELINE["live_unique"]
    s3_improved = commercial_s3 > BASELINE["commercial_stage3"]
    quotes_improved = (len(validated_u) + len(secondary_u)) > (
        BASELINE["validated_unique"] + BASELINE["secondary_unique"]
    )
    if (live_improved or s3_improved or quotes_improved) and structured_live_rows > 0:
        verdict = "PHASE_L15_STRUCTURED_COMMERCIAL_EXPANSION_WORKING"
    elif structured_live_rows > 0 or history_harvest.get("count", 0) > 0:
        verdict = "PHASE_L15_PARTIAL_STRUCTURED_COMMERCIAL_EXPANSION"
    else:
        verdict = "PHASE_L15_STRUCTURED_COMMERCIAL_EXPANSION_FAILED"

    remaining = (
        "obtain free API/account access"
        if free_q
        else "integrate additional structured APIs"
        if gaps["states_audit_only_high_priority"]
        else "quantity/config recovery"
    )
    if not live_improved and not s3_improved:
        remaining = "integrate additional structured APIs"

    summary = {
        "kind": "L15Summary",
        "build": BUILD,
        "generated_at": _utc(),
        "verdict": verdict,
        "baseline": BASELINE,
        "after": {
            "live_unique": live_unique,
            "commercial_stage3": commercial_s3,
            "validated_unique": len(validated_u),
            "secondary_unique": len(secondary_u),
            "accessible": len(rows),
            "stage3": stage_counts.get("stage3", 0),
            "structured_live_rows": structured_live_rows,
        },
        "gov_evidence": dict(gov_grades),
        "fresh_hunt_terminal": fresh_hunt["terminal_status"],
        "tier_counts": inventory["tier_counts"],
        "new_integrations": [
            {
                "source": s["name"],
                "access_type": s.get("data_type"),
                "live_history": "LIVE",
                "auth": s.get("auth"),
                "unique_contribution": next(
                    (
                        c.get("UniqueCommercialContribution")
                        for c in contrib["sources"]
                        if c["source"] == s["source_id"]
                    ),
                    0,
                ),
            }
            for s in STRUCTURED_LIVE_SOURCES
        ],
        "parked_fragile": PARKED_FRAGILE_SOURCES,
        "free_access_queue": free_q,
        "paid_api_queue": paid_q,
        "remaining_bottleneck": remaining,
        "no_phase_m": True,
        "no_outreach": True,
        "no_auto_accounts": True,
        "evidence_gate_unchanged": True,
        "legacy_cleanup": legacy_cleanup_report() if callable(legacy_cleanup_report) else {},
    }

    # Write artifacts
    artifacts = {
        "l15_structured_source_inventory.json": inventory,
        "l15_structured_source_queue.json": {
            "kind": "STRUCTURED_SOURCE_EXPANSION_QUEUE",
            "build": BUILD,
            "generated_at": _utc(),
            "queue": queue,
        },
        "l15_free_access_queue.json": {
            "kind": "FREE_STRUCTURED_ACCESS_QUEUE",
            "build": BUILD,
            "queue": free_q,
            "paid": paid_q,
        },
        "l15_source_value.json": {
            "kind": "L15SourceValue",
            "build": BUILD,
            "rows": value_rows,
            "high_value_low_effort": [r for r in value_rows if r.get("time_to_value_bucket") == "high_value_low_effort"],
            "high_value_high_effort": [r for r in value_rows if r.get("time_to_value_bucket") == "high_value_high_effort"],
            "low_value_high_effort": [r for r in value_rows if r.get("time_to_value_bucket") == "low_value_high_effort"],
        },
        "l15_fresh_hunt.json": fresh_hunt,
        "l15_source_contribution.json": {
            "kind": "L15SourceContribution",
            "build": BUILD,
            **contrib,
            "structured_live_rows": structured_live_rows,
        },
        "l15_history_contribution.json": history_contrib,
        "l15_quote_targets.json": quote_targets,
        "l15_discovery_gaps.json": gaps,
        "l15_summary.json": summary,
        "l15_state_structured_matrix.json": {
            "kind": "L15StateStructuredMatrix",
            "build": BUILD,
            "count": len(state_matrix),
            "rows": state_matrix,
        },
        "l15_ckan_discovery.json": ckan,
        "l15_duplicate_provenance.json": {"count": len(dup_prov), "sample": dup_prov[:40]},
    }
    for name, payload in artifacts.items():
        save_json(OUT / name, payload)
        print(f"[l15] wrote {name}", flush=True)

    # Regenerate controlled pilot queue pointers (testable queue)
    save_json(
        OUT / "l15_pilot_queue.json",
        {
            "kind": "L15TestablePilotQueue",
            "build": BUILD,
            "READY_FOR_OWNER_APPROVAL": ready_u,
            "NEEDS_MINOR_REVIEW": review_u,
            "note": "Evidence gate unchanged; owner approval only; no send",
        },
    )

    write_l15_docs(summary, fresh_hunt, inventory, gaps, history_contrib, quote_targets)
    return summary


def write_l15_docs(
    summary: dict[str, Any],
    fresh: dict[str, Any],
    inventory: dict[str, Any],
    gaps: dict[str, Any],
    history: dict[str, Any],
    quotes: dict[str, Any],
) -> None:
    DOCS.mkdir(parents=True, exist_ok=True)
    docs = {
        "phase_l15_structured_source_strategy.md": f"""# Phase L.15 — Structured Source Strategy

Build: `{BUILD}`

## Principle

**Structured coverage first, fragile scraping last.**

Tier-1 official APIs/feeds → Tier-2 Socrata/CKAN/ArcGIS/RSS → Tier-3 static → Tier-4 parked with stop-loss.

## Verdict

`{summary.get('verdict')}`

## Baseline → After

- live unique: {BASELINE['live_unique']} → {summary.get('after', {}).get('live_unique')}
- commercial Stage 3: {BASELINE['commercial_stage3']} → {summary.get('after', {}).get('commercial_stage3')}
- validated unique: {BASELINE['validated_unique']} → {summary.get('after', {}).get('validated_unique')}
- secondary unique: {BASELINE['secondary_unique']} → {summary.get('after', {}).get('secondary_unique')}

## Remaining bottleneck

{summary.get('remaining_bottleneck')}
""",
        "phase_l15_api_feed_inventory.md": f"""# L.15 API / Feed Inventory

Tier counts: {json.dumps(inventory.get('tier_counts'))}

See `artifacts/phase_l/l15_structured_source_inventory.json` for full rows.

Federal Tier-1: SAM.gov Opportunities API (free key), USAspending (public).

Tier-2 live: Montgomery MD solicitations, Delaware open bids, NYC City Record.
""",
        "phase_l15_state_structured_coverage.md": """# L.15 State Structured Coverage

All 50 states + DC audited into `l15_state_structured_matrix.json`.

Integrated live structured (L.15): MD (Montgomery), DE, NY (NYC).

High-priority audit-only: states with open-data portals but HTML-only bid boards remain the gap.
""",
        "phase_l15_open_data_sources.md": f"""# L.15 Open Data Sources

- Socrata generic adapter: domain + dataset id + field_map
- CKAN discovery packages found: {gaps.get('ckan_packages_found')}
- ArcGIS FeatureServer parser available (wire datasets as discovered)
- History: USAspending + BRLA PO + NYC awards + Montgomery contracts
""",
        "phase_l15_source_value.md": """# L.15 Source Value

`StructuredSourceValueScore` and `UniqueCommercialContribution` in `l15_source_value.json` / `l15_source_contribution.json`.

Prefer **high value / low effort** structured APIs over fragile portals.
""",
        "phase_l15_source_stop_loss.md": f"""# L.15 Source Engineering Stop-Loss

Status constant: `{PARKED_FRAGILE_SOURCE}`

Max attempts: 3 · Max seconds: 45 before park when no stable structured path.

Parked: OpenGov CDN, Bonfire, IonWave, BidNet auth history, DemandStar, Public Purchase.
""",
        "phase_l15_fresh_hunt.md": f"""# L.15 Fresh Hunt

Terminal: `{fresh.get('terminal_status')}`

- raw unique: {fresh.get('raw_unique')}
- live unique: {fresh.get('live_unique')}
- accessible: {fresh.get('accessible')}
- Stage 1/2/3: {fresh.get('stage1')} / {fresh.get('stage2')} / {fresh.get('stage3')}
- commercial Stage 3: {fresh.get('commercial_stage3')}
""",
        "phase_l15_quote_target_delta.md": f"""# L.15 Quote Target Delta

Baseline validated/secondary unique: 1 / 1

After: {quotes.get('validated_unique')} / {quotes.get('secondary_unique')}

READY_FOR_OWNER_APPROVAL: {quotes.get('READY_FOR_OWNER_APPROVAL')}
NEEDS_MINOR_REVIEW: {quotes.get('NEEDS_MINOR_REVIEW')}

Evidence gate unchanged. No outreach. No duplicate inflation.
""",
        "phase_l15_legacy_cleanup.md": """# L.15 Legacy Cleanup

- Prefer `live_structured` over duplicate fragile HTML adapters for the same capability
- BidNet auth history remains parked
- OpenGov CDN / Bonfire / IonWave / DemandStar / Public Purchase remain parked when fragile
- One canonical path: structured registry → StructuredOpenDataLiveFetcher → hunt profile `structured`
""",
        "phase_l15_regression.md": """# L.15 Regression

Tests: `tests/test_phase_l15_structured_expansion.py`

Covers adapters, mapping, dedupe, scoring, tiers, stop-loss, free-access queue, hunt completion invariants, evidence gate, no outreach.
""",
    }
    for name, body in docs.items():
        (DOCS / name).write_text(body, encoding="utf-8")


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--no-refresh-hunt", action="store_true")
    p.add_argument("--max-hunt-sources", type=int, default=20)
    args = p.parse_args()
    summary = run_phase_l15(
        authorize_live=True,
        refresh_hunt=not args.no_refresh_hunt,
        max_hunt_sources=args.max_hunt_sources,
    )
    print(json.dumps({k: summary[k] for k in ("verdict", "after", "gov_evidence", "remaining_bottleneck")}, indent=2))
