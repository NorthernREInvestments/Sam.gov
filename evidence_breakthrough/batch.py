"""Staged evidence breakthrough runner: 50 → 200 → 852."""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable
from uuid import uuid4

import httpx

from application_clock import now_utc
from evidence_breakthrough.acquisition_cost_resolver import resolve_public_acquisition_cost
from evidence_breakthrough.corpus import load_identity_store, opportunity_groups, select_identities
from evidence_breakthrough.gov_value_resolver import resolve_government_value
from evidence_breakthrough.models import (
    BASKET_COMPLETE_COVERAGE,
    BASKET_READY_COVERAGE,
    BOTH_SIDES_BASKET_READY,
    BOTH_SIDES_LINE_READY,
    BUILD,
    FOUND,
    empty_evidence_map,
)
from evidence_breakthrough.opengov_history import parse_opengov_opportunity_id
from evidence_breakthrough.report import build_completion_report, summarize_funnel
from m3_data_root import data_path

log = logging.getLogger("govtracker.evidence_breakthrough.batch")


def _paths():
    return (
        data_path("m3_evidence_breakthrough_checkpoint.json"),
        data_path("m3_evidence_breakthrough_store.json"),
        data_path("m3_evidence_breakthrough_last_report.json"),
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


def _line_ready(gov: dict[str, Any], cost: dict[str, Any], uom_ok: bool) -> bool:
    return gov.get("status") == FOUND and cost.get("status") == FOUND and uom_ok


def _uom_ok(identity: dict[str, Any], gov: dict[str, Any], cost: dict[str, Any]) -> dict[str, Any]:
    buyer_uom = (identity.get("uom_normalized") or identity.get("uom") or "EA")
    try:
        from response_engine.uom import normalize_uom_code

        bu = normalize_uom_code(buyer_uom) or str(buyer_uom).upper()
    except Exception:
        bu = str(buyer_uom or "EA").upper()
    hist_uom = None
    if isinstance(gov.get("evidence"), dict):
        hist_uom = gov["evidence"].get("uom")
    cost_uom = None
    if isinstance(cost.get("evidence"), dict):
        cost_uom = cost["evidence"].get("uom")
    # OpenGov often uses numeric UOM ids — treat unknown hist uom as comparable when buyer EA
    try:
        from response_engine.uom import normalize_uom_code

        hu = normalize_uom_code(hist_uom) if hist_uom and not str(hist_uom).isdigit() else None
        cu = normalize_uom_code(cost_uom) if cost_uom else bu
    except Exception:
        hu, cu = None, bu
    ok = True
    reason = "OK"
    if hu and cu and hu != cu and {hu, cu} not in [{"EA", "EACH"}]:
        ok = False
        reason = "UOM_MISMATCH"
    if hu is None and hist_uom and str(hist_uom).isdigit() and bu not in {"EA", "EACH", ""}:
        # numeric portal UOM vs non-EA buyer — fail closed
        ok = False
        reason = "UOM_MISMATCH"
    return {"ok": ok, "buyer_uom": bu, "hist_uom": hu or hist_uom, "cost_uom": cu, "reason": reason}


def _handoff_profit(
    opportunity_id: str,
    line_results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Feed both-sides lines into existing line_item_economics (no profit math changes)."""
    try:
        from line_item_economics.engine import analyze_line_item_economics
        from line_item_economics.extract import line_from_row
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__}

    existing = []
    retail_by_line: dict[str, dict[str, Any]] = {}
    historical_by_line: dict[str, dict[str, Any]] = {}
    for i, lr in enumerate(line_results, start=1):
        ident = lr.get("identity") or {}
        lid = f"L{i:04d}"
        qty = ident.get("quantity")
        try:
            qty_f = float(qty) if qty is not None else 1.0
        except (TypeError, ValueError):
            qty_f = 1.0
        if qty_f <= 0:
            qty_f = 1.0
        existing.append(
            line_from_row(
                {
                    "clin": lid,
                    "description": ident.get("raw_description") or ident.get("description"),
                    "manufacturer": ident.get("manufacturer"),
                    "model": ident.get("model"),
                    "part_number": ident.get("part_number") or ident.get("catalog_number"),
                    "quantity": qty_f,
                    "uom": ident.get("uom_normalized") or ident.get("uom") or "EA",
                    "original_text": ident.get("commercial_search_key"),
                },
                index=i,
                provenance={"source": "evidence_breakthrough"},
            )
        )
        gov = lr.get("government_value") or {}
        cost = lr.get("public_cost") or {}
        if gov.get("status") == FOUND and isinstance(gov.get("evidence"), dict):
            historical_by_line[lid] = gov["evidence"]
        if cost.get("status") == FOUND and isinstance(cost.get("evidence"), dict):
            buyer_uom = ident.get("uom_normalized") or ident.get("uom") or "EA"
            # OpenGov numeric UOMs are not pack codes — treat as buyer UOM / EA for economics
            retail_uom = cost["evidence"].get("uom") or buyer_uom or "EA"
            if str(retail_uom).isdigit():
                retail_uom = buyer_uom or "EA"
            up = cost["evidence"].get("unit_price")
            retail_by_line[lid] = {
                "unit_price": up,
                "normalized_unit_price": up,
                "uom": retail_uom,
                "source_url": cost["evidence"].get("source_url"),
                "seller": cost["evidence"].get("seller"),
                "retailer": cost["evidence"].get("seller"),
                "confidence": cost.get("confidence"),
                "kind": "requested_brand",
            }
        if gov.get("status") == FOUND and isinstance(gov.get("evidence"), dict):
            # Ensure historical UOM does not look like a foreign pack code
            hu = gov["evidence"].get("uom")
            if hu is not None and str(hu).isdigit():
                gov["evidence"] = {
                    **gov["evidence"],
                    "uom": ident.get("uom_normalized") or ident.get("uom") or "EA",
                }
                historical_by_line[lid] = gov["evidence"]

    if not existing:
        return {"ok": False, "error": "no_lines"}
    try:
        lie = analyze_line_item_economics(
            opportunity_id=f"{opportunity_id}:ev",
            body_text=None,
            existing_lines=existing,
            retail_by_line=retail_by_line,
            historical_by_line=historical_by_line,
            persist=True,
        )
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:200]}

    roll = lie.get("rollup") or {}
    return {
        "ok": True,
        "profit_bucket": roll.get("profit_bucket"),
        "proof_label": roll.get("proof_label"),
        "post_financing_profit": roll.get("post_financing_profit"),
        "TOTAL_KNOWN_RETAIL_COST": roll.get("TOTAL_KNOWN_RETAIL_COST"),
        "TOTAL_KNOWN_HISTORICAL_VALUE": roll.get("TOTAL_KNOWN_HISTORICAL_VALUE"),
        "TOTAL_KNOWN_RETAIL_SPREAD": roll.get("TOTAL_KNOWN_RETAIL_SPREAD"),
        "estimated_freight": roll.get("estimated_freight"),
        "completeness_grade": roll.get("completeness_grade"),
        "lines_priced": roll.get("lines_priced"),
        "lines_historical_matched": roll.get("lines_historical_matched"),
        "owner_summary": {
            "status": roll.get("profit_bucket"),
            "expected_profit": roll.get("post_financing_profit"),
            "retail_cost": roll.get("TOTAL_KNOWN_RETAIL_COST"),
            "gov_value": roll.get("TOTAL_KNOWN_HISTORICAL_VALUE"),
        },
    }


def run_evidence_breakthrough_stage(
    *,
    stage_limit: int = 50,
    resume: bool = True,
    run_id: str | None = None,
    on_progress: Callable[..., None] | None = None,
    skip_acquisition: bool = False,
    max_buyers_history: int | None = None,
) -> dict[str, Any]:
    """Run gov-value + public-cost resolvers on prioritized corpus slice."""
    run_id = run_id or f"EV-{uuid4().hex[:12]}"
    started = now_utc().isoformat()
    ck_path, store_path, report_path = _paths()
    ck = (
        _load_json(ck_path, {"kind": "EvidenceBreakthroughCheckpoint", "done_keys": [], "stats": {}})
        if resume
        else {"kind": "EvidenceBreakthroughCheckpoint", "done_keys": [], "stats": {}}
    )
    done = set(ck.get("done_keys") or []) if resume else set()
    if resume:
        store = _load_json(
            store_path,
            {"kind": "EvidenceBreakthroughStore", "build": BUILD, "by_line": {}, "by_opportunity": {}},
        )
    else:
        store = {"kind": "EvidenceBreakthroughStore", "build": BUILD, "by_line": {}, "by_opportunity": {}}
    by_line = store.setdefault("by_line", {})
    by_opp = store.setdefault("by_opportunity", {})

    identities = select_identities(limit=stage_limit)
    total = len(identities)
    history_cache: dict[str, dict[str, Any]] = {}
    buyers_loaded = 0

    stats = {
        "identities_attempted": 0,
        "gov_found": 0,
        "cost_found": 0,
        "both_sides_lines": 0,
        "retryable_gov": 0,
        "retryable_cost": 0,
        "no_gov": 0,
        "no_cost": 0,
    }

    def _progress(phase: str, pct: int, **extra: Any) -> None:
        if on_progress:
            on_progress(phase=phase, pct=pct, **extra)

    _progress("SELECT_CORPUS", 2, total=total, stage_limit=stage_limit)

    client = httpx.Client(timeout=40.0, follow_redirects=True)
    try:
        for idx, ident in enumerate(identities):
            oid = str(ident.get("opportunity_id") or "")
            line_key = f"{oid}::{ident.get('line_id') or ident.get('part_number') or ident.get('model') or idx}"
            if line_key in done and resume:
                continue

            stats["identities_attempted"] += 1
            gov_code, _ = parse_opengov_opportunity_id(oid)
            if gov_code and gov_code not in history_cache:
                if max_buyers_history is not None and buyers_loaded >= max_buyers_history:
                    pass
                else:
                    from evidence_breakthrough.opengov_history import build_or_load_buyer_history

                    history_cache[gov_code] = build_or_load_buyer_history(
                        gov_code,
                        client=client,
                        max_projects=30,
                        max_awarded_detail=12,
                    )
                    buyers_loaded += 1

            gov = resolve_government_value(
                ident,
                opportunity_id=oid,
                client=client,
                history_cache=history_cache,
            )
            if skip_acquisition:
                from evidence_breakthrough.models import empty_resolver_result

                cost = empty_resolver_result(side="public_acquisition_cost")
                cost["status"] = "SKIPPED"
                cost["stop_reason"] = "SKIPPED"
            else:
                cost = resolve_public_acquisition_cost(
                    ident,
                    opportunity_id=oid,
                    client=client,
                    max_pages=4,
                    use_phase_l=False,
                    use_openai_fallback=False,
                    # Human-like public_price_search_v2 runs inside resolver when True
                    allow_web_crawl=True,
                    history_cache=history_cache,
                )

            uom = _uom_ok(ident, gov, cost)
            line_ready = _line_ready(gov, cost, uom["ok"])

            if gov.get("status") == FOUND:
                stats["gov_found"] += 1
            elif gov.get("status") == "RETRYABLE_FAILURE":
                stats["retryable_gov"] += 1
            else:
                stats["no_gov"] += 1

            if cost.get("status") == FOUND:
                stats["cost_found"] += 1
            elif cost.get("status") == "RETRYABLE_FAILURE":
                stats["retryable_cost"] += 1
            elif cost.get("status") != "SKIPPED":
                stats["no_cost"] += 1

            if line_ready:
                stats["both_sides_lines"] += 1

            emap = empty_evidence_map()
            emap.update(
                {
                    "opportunity_id": oid,
                    "buyer": gov_code,
                    "current_solicitation": oid,
                    "line": {
                        "line_id": ident.get("line_id"),
                        "description": ident.get("raw_description"),
                        "quantity": ident.get("quantity"),
                        "uom": ident.get("uom_normalized") or ident.get("uom"),
                    },
                    "identity": {
                        "grade": ident.get("confidence_grade"),
                        "manufacturer": ident.get("manufacturer"),
                        "model": ident.get("model"),
                        "part_number": ident.get("part_number") or ident.get("catalog_number"),
                        "commercial_search_key": ident.get("commercial_search_key"),
                    },
                    "government_value": gov,
                    "public_cost": cost,
                    "uom_normalization": uom,
                    "confidence": {
                        "gov": gov.get("confidence"),
                        "cost": cost.get("confidence"),
                        "line_ready": line_ready,
                    },
                    "source_links": [
                        *( [p.get("url") for p in (gov.get("provenance") or []) if p.get("url")] ),
                        *( [p.get("url") for p in (cost.get("provenance") or []) if p.get("url")] ),
                    ],
                    "missing_facts": [
                        *([] if gov.get("status") == FOUND else ["GOVERNMENT_VALUE"]),
                        *([] if cost.get("status") == FOUND else ["ACQUISITION_COST"]),
                        *([] if uom["ok"] else ["UOM_NORMALIZATION"]),
                    ],
                    "both_sides_status": BOTH_SIDES_LINE_READY if line_ready else None,
                    "updated_at": now_utc().isoformat(),
                }
            )
            by_line[line_key] = emap
            done.add(line_key)

            # Checkpoint every 5
            if stats["identities_attempted"] % 5 == 0:
                ck["done_keys"] = sorted(done)
                ck["stats"] = stats
                ck["updated_at"] = now_utc().isoformat()
                ck["run_id"] = run_id
                _save_json(ck_path, ck)
                store["updated_at"] = now_utc().isoformat()
                store["stats"] = stats
                _save_json(store_path, store)
                pct = min(90, int(5 + 85 * (idx + 1) / max(total, 1)))
                _progress(
                    "RESOLVING",
                    pct,
                    attempted=stats["identities_attempted"],
                    gov=stats["gov_found"],
                    cost=stats["cost_found"],
                    both=stats["both_sides_lines"],
                )
            time.sleep(0.05)
    finally:
        client.close()

    # Opportunity rollups + profit handoff
    _progress("BASKET_ROLLUP", 92)
    groups = opportunity_groups(identities)
    opp_summaries = []
    for oid, idents in groups.items():
        line_maps = []
        for ident in idents:
            for k, em in by_line.items():
                if em.get("opportunity_id") == oid and (
                    em.get("identity", {}).get("part_number")
                    == (ident.get("part_number") or ident.get("catalog_number"))
                    or em.get("identity", {}).get("model") == ident.get("model")
                ):
                    line_maps.append(em)
                    break
        n = len(line_maps) or len(idents)
        gov_n = sum(1 for em in line_maps if (em.get("government_value") or {}).get("status") == FOUND)
        cost_n = sum(1 for em in line_maps if (em.get("public_cost") or {}).get("status") == FOUND)
        both_n = sum(1 for em in line_maps if (em.get("confidence") or {}).get("line_ready"))
        coverage = (both_n / n) if n else 0.0
        basket_status = None
        if coverage >= BASKET_COMPLETE_COVERAGE:
            basket_status = "COMPLETE"
        elif coverage >= BASKET_READY_COVERAGE:
            basket_status = BOTH_SIDES_BASKET_READY
        profit = None
        if both_n >= 1:
            profit = _handoff_profit(oid, line_maps)
        summary = {
            "opportunity_id": oid,
            "total_lines_in_stage": n,
            "lines_with_gov_value": gov_n,
            "lines_with_acquisition_cost": cost_n,
            "lines_with_both_sides": both_n,
            "coverage_pct": round(coverage * 100, 1),
            "basket_status": basket_status,
            "profit": profit,
        }
        by_opp[oid] = summary
        opp_summaries.append(summary)

    # TDPUD regression (reuse prior result when present to avoid repeat long crawl)
    _progress("TDPUD_REGRESSION", 96)
    tdpud = store.get("tdpud") if isinstance(store.get("tdpud"), dict) else None
    if not tdpud or not tdpud.get("lines"):
        from evidence_breakthrough.tdpud_regression import run_tdpud_regression

        tdpud = run_tdpud_regression()
    else:
        tdpud = {**tdpud, "reused": True}

    ck["done_keys"] = sorted(done)
    ck["stats"] = stats
    ck["completed_at"] = now_utc().isoformat()
    ck["run_id"] = run_id
    _save_json(ck_path, ck)
    store["stats"] = stats
    store["updated_at"] = now_utc().isoformat()
    store["tdpud"] = tdpud
    _save_json(store_path, store)

    funnel = summarize_funnel(store, identities)
    report = build_completion_report(
        stats=stats,
        funnel=funnel,
        opp_summaries=opp_summaries,
        tdpud=tdpud,
        stage_limit=stage_limit,
        run_id=run_id,
        started=started,
    )
    _save_json(report_path, report)
    _progress("DONE", 100, gov=stats["gov_found"], cost=stats["cost_found"], both=stats["both_sides_lines"])
    return report
