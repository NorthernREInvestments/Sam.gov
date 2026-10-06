"""Scale evidence → basket economics → lender pipeline runner."""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from typing import Any, Callable
from uuid import uuid4

from application_clock import now_utc
from evidence_breakthrough.corpus import load_identity_store, select_identities
from evidence_breakthrough.opengov_history import parse_opengov_opportunity_id
from m3_data_root import data_path
from scale_evidence_profit.bid_price_index import (
    buyers_from_identity_store,
    load_index,
    mine_missing_buyers,
    rebuild_index_from_buyer_caches,
)
from scale_evidence_profit.current_open_lines import harvest_open_project_identities
from scale_evidence_profit.line_resolver import resolve_line
from scale_evidence_profit.models import (
    BOTH_SIDES_READY,
    BUILD,
)
from scale_evidence_profit.opportunity import (
    aggregate_opportunity,
    build_lender_packet,
    classify_execution,
    classify_pipeline,
    compute_basket_economics,
    run_profit_first_handoff,
)
from scale_evidence_profit.report import build_completion_report

log = logging.getLogger("govtracker.scale_evidence_profit.batch")


def _paths():
    return (
        data_path("m3_scale_evidence_profit_checkpoint.json"),
        data_path("m3_scale_evidence_profit_store.json"),
        data_path("m3_scale_evidence_profit_last_report.json"),
    )


def _load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save_json(path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _population_stats() -> dict[str, Any]:
    store = load_identity_store()
    by = store.get("by_opportunity") or {}
    grades = {"A": 0, "B": 0, "C": 0}
    usable = 0
    opps = 0
    for pack in by.values():
        ids = [i for i in (pack.get("identities") or []) if i.get("confidence_grade") in grades]
        if ids:
            opps += 1
        for i in ids:
            grades[i["confidence_grade"]] += 1
            usable += 1
    return {"opportunities": len(by), "opportunities_with_usable": opps, "usable_identities": usable, "grades": grades}


def run_scale_evidence_to_profit(
    *,
    mine_buyers: int = 50,
    resume: bool = True,
    run_id: str | None = None,
    on_progress: Callable[..., None] | None = None,
    identity_limit: int | None = None,
) -> dict[str, Any]:
    """Phase A scale both-sides + Phase B basket/lender pipeline."""
    run_id = run_id or f"SEP-{uuid4().hex[:12]}"
    started = now_utc().isoformat()
    ck_path, store_path, report_path = _paths()

    def _progress(phase: str, pct: int, **extra: Any) -> None:
        if on_progress:
            on_progress(phase=phase, pct=pct, **extra)

    pop = _population_stats()
    _progress("POPULATION", 2, **pop)

    # --- Phase A0: deepen top histories + mine missing + harvest open lines ---
    _progress("BUILD_BID_INDEX", 5)
    idx = rebuild_index_from_buyer_caches(force=True)
    buyer_codes = buyers_from_identity_store()

    # Deepen history for buyers that already yield priced tabs
    # Skip deepen on resolve-only reruns when index is already large.
    deepen_codes = []
    if mine_buyers > 0 or len(idx.get("records") or []) < 5000:
        deepen_codes = [
            c
            for c in (idx.get("buyers_indexed") or [])
            if c in set(buyer_codes) or c in {"go-metro", "bridgeportct", "cambridgema", "collincountytx", "cvgairport"}
        ][:12]
    deepen_stats = {"deepened": 0}
    if deepen_codes:
        import httpx
        from evidence_breakthrough.opengov_history import build_or_load_buyer_history

        _progress("DEEPEN_HISTORY", 8, buyers=len(deepen_codes))
        with httpx.Client(timeout=40.0) as client:
            for di, code in enumerate(deepen_codes):
                try:
                    build_or_load_buyer_history(
                        code,
                        client=client,
                        max_projects=50,
                        max_awarded_detail=30,
                        force=True,
                    )
                    deepen_stats["deepened"] += 1
                except Exception:
                    pass
                _progress("DEEPEN_HISTORY", int(8 + 15 * (di + 1) / max(len(deepen_codes), 1)), buyer=code)
        idx = rebuild_index_from_buyer_caches(force=True)

    mine_stats = {"missing_requested": 0, "mined": 0, "with_prices": 0}
    if mine_buyers > 0:
        _progress("MINE_BUYER_HISTORY", 24, buyers=mine_buyers)
        mine_stats = mine_missing_buyers(
            buyer_codes,
            max_buyers=mine_buyers,
            max_awarded_detail=15,
            on_progress=on_progress,
        )
        idx = load_index()

    # Harvest current open project price-table lines (recurring inventory pattern)
    harvested: list[dict[str, Any]] = []
    harvest_n = 20 if mine_buyers > 0 else 12
    harvest_codes = list(
        dict.fromkeys(
            [
                "go-metro",
                "bridgeportct",
                "cityofshelton",
                "cambridgema",
                "collincountytx",
                "capecoralfl",
                "dekalbcountyga",
                *(idx.get("buyers_indexed") or [])[:harvest_n],
                *buyer_codes[:harvest_n],
            ]
        )
    )
    _progress("HARVEST_OPEN", 40, buyers=len(harvest_codes[:harvest_n]))
    harvested = harvest_open_project_identities(
        harvest_codes,
        max_buyers=harvest_n,
        max_open_per_buyer=8 if mine_buyers > 0 else 5,
        on_progress=on_progress,
    )

    _progress(
        "INDEX_READY",
        48,
        records=len(idx.get("records") or []),
        buyers=len(idx.get("buyers_indexed") or []),
        harvested=len(harvested),
    )

    # --- Phase A1: resolve store identities + harvested open lines ---
    limit = identity_limit or pop["usable_identities"] or 2000
    identities = select_identities(limit=limit, grades=("A", "B", "C"), full=True)
    # Merge harvested (prefer store identity first; add open harvest keys)
    seen_keys: set[str] = set()
    merged: list[dict[str, Any]] = []
    for ident in identities + harvested:
        oid = str(ident.get("opportunity_id") or "")
        key = f"{oid}::{ident.get('line_id') or ident.get('part_number') or ident.get('model')}"
        if key in seen_keys:
            continue
        seen_keys.add(key)
        merged.append(ident)
    identities = merged
    total = len(identities)

    ck = (
        _load_json(ck_path, {"kind": "ScaleEvidenceProfitCheckpoint", "done_keys": []})
        if resume
        else {"kind": "ScaleEvidenceProfitCheckpoint", "done_keys": []}
    )
    done = set(ck.get("done_keys") or []) if resume else set()
    if resume:
        store = _load_json(
            store_path,
            {"kind": "ScaleEvidenceProfitStore", "build": BUILD, "by_line": {}, "by_opportunity": {}},
        )
    else:
        store = {"kind": "ScaleEvidenceProfitStore", "build": BUILD, "by_line": {}, "by_opportunity": {}}

    by_line = store.setdefault("by_line", {})
    stats = {
        "identities_attempted": 0,
        "gov_found": 0,
        "cost_found": 0,
        "both_sides": 0,
        "insufficient": 0,
        "no_history": 0,
        "no_public_price": 0,
        "uom_blocked": 0,
    }

    for i, ident in enumerate(identities):
        oid = str(ident.get("opportunity_id") or "")
        key = f"{oid}::{ident.get('line_id') or ident.get('part_number') or ident.get('model') or i}"
        if key in done and resume:
            continue
        result = resolve_line(ident, idx)
        by_line[key] = result
        done.add(key)
        stats["identities_attempted"] += 1
        st = result.get("line_status")
        if st == BOTH_SIDES_READY:
            stats["both_sides"] += 1
        if (result.get("government_value") or {}).get("status") == "FOUND":
            stats["gov_found"] += 1
        if (result.get("public_cost") or {}).get("status") == "FOUND":
            stats["cost_found"] += 1
        if st == "INSUFFICIENT_IDENTITY":
            stats["insufficient"] += 1
        if st == "NO_HISTORY_AFTER_EXHAUSTIVE_SEARCH":
            stats["no_history"] += 1
        if (result.get("public_cost") or {}).get("stop_reason") == "NO_PUBLIC_PRICE_AFTER_EXHAUSTIVE_SEARCH":
            if (result.get("public_cost") or {}).get("status") != "FOUND":
                stats["no_public_price"] += 1
        if st == "UOM_BLOCKED":
            stats["uom_blocked"] += 1

        if stats["identities_attempted"] % 25 == 0:
            ck["done_keys"] = sorted(done)
            ck["stats"] = stats
            ck["run_id"] = run_id
            ck["updated_at"] = now_utc().isoformat()
            _save_json(ck_path, ck)
            store["stats"] = stats
            store["updated_at"] = now_utc().isoformat()
            _save_json(store_path, store)
            pct = min(75, int(45 + 30 * (i + 1) / max(total, 1)))
            _progress(
                "RESOLVE_LINES",
                pct,
                attempted=stats["identities_attempted"],
                both=stats["both_sides"],
                gov=stats["gov_found"],
                cost=stats["cost_found"],
            )

    # --- Phase A2 / B: opportunity aggregation + economics + pipeline ---
    _progress("OPPORTUNITY_AGGREGATION", 78)
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    # total purchasing lines per opp from identity store
    id_store = load_identity_store()
    total_lines_by_opp: dict[str, int] = {}
    for oid, pack in (id_store.get("by_opportunity") or {}).items():
        ids = pack.get("identities") if isinstance(pack.get("identities"), list) else []
        raw = pack.get("raw_rows") if isinstance(pack.get("raw_rows"), list) else []
        n = len(ids) or len(raw)
        if not n:
            n = sum(1 for x in identities if x.get("opportunity_id") == oid)
        total_lines_by_opp[oid] = n

    for key, result in by_line.items():
        oid = result.get("opportunity_id")
        if oid:
            groups[oid].append(result)

    by_opp = store.setdefault("by_opportunity", {})
    lender_packets = []
    near_ready = []
    for oid, lines in groups.items():
        # Prefer max(store package size, resolved lines) so harvested open RFQs aren't undercounted
        tot = max(int(total_lines_by_opp.get(oid) or 0), len(lines))
        agg = aggregate_opportunity(oid, lines, total_purchasing_lines=tot)
        econ = compute_basket_economics(lines)
        execution = classify_execution(agg, econ)
        pipeline = classify_pipeline(agg, econ, execution)
        pf = None
        if int(agg.get("both_sides_lines") or 0) >= 1 and econ.get("expected_revenue"):
            pf = run_profit_first_handoff(oid, econ)
        packet = None
        if pipeline.get("lender_ready") or pipeline.get("near_ready_24h"):
            packet = build_lender_packet(oid, agg, econ, pipeline, lines)
            if pipeline.get("lender_ready"):
                lender_packets.append(packet)
            elif pipeline.get("near_ready_24h"):
                near_ready.append(packet)

        # Source attribution
        code, _ = parse_opengov_opportunity_id(oid)
        source = "OpenGov" if code else ("Other" if not oid.startswith("opengov") else "OpenGov")

        by_opp[oid] = {
            **agg,
            "economics": econ,
            "execution": execution,
            "pipeline": pipeline,
            "profit_first": pf,
            "lender_packet": packet,
            "source_family": source,
        }

    lender_packets.sort(key=lambda p: -(p.get("expected_profit") or 0))
    near_ready.sort(key=lambda p: -(p.get("expected_profit") or 0))

    store["stats"] = stats
    store["population"] = pop
    store["mine_stats"] = {**mine_stats, **deepen_stats, "harvested_lines": len(harvested)}
    store["index_meta"] = {
        "records": len(idx.get("records") or []),
        "buyers_indexed": len(idx.get("buyers_indexed") or []),
    }
    store["lender_ready"] = lender_packets
    store["near_ready_24h"] = near_ready[:20]
    store["updated_at"] = now_utc().isoformat()
    store["run_id"] = run_id
    _save_json(store_path, store)

    ck["done_keys"] = sorted(done)
    ck["stats"] = stats
    ck["completed_at"] = now_utc().isoformat()
    ck["run_id"] = run_id
    _save_json(ck_path, ck)

    _progress("REPORT", 95)
    report = build_completion_report(
        store=store,
        population=pop,
        mine_stats=mine_stats,
        run_id=run_id,
        started=started,
    )
    _save_json(report_path, report)
    _progress("DONE", 100, both=stats["both_sides"], lender=len(lender_packets), near=len(near_ready))
    return report
