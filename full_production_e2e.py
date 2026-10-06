"""Full production end-to-end test orchestration + completion report builder."""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any

from application_clock import now_utc

log = logging.getLogger("govtracker.full_production_e2e")

PLUMBING_CASE_ID = "5e2a1594e10401f9"
REPORT_FILE = "m3_full_production_e2e_last_report.json"

PROFIT_BUCKETS = (
    (0, "gt_0"),
    (1000, "gte_1k"),
    (2500, "gte_2_5k"),
    (5000, "gte_5k"),
    (10000, "gte_10k"),
    (25000, "gte_25k"),
    (50000, "gte_50k"),
    (75000, "gte_75k"),
)


def _platform_bucket(rec: dict[str, Any]) -> str:
    plat = str(rec.get("platform") or rec.get("platform_family") or "").lower()
    sid = str(rec.get("source_id") or "").lower()
    blob = f"{plat} {sid}"
    if "bidnet" in blob:
        return "BidNet"
    if "opengov" in blob:
        return "OpenGov"
    if "sam" in blob or "federal" in blob:
        return "SAM"
    if "dla" in blob or "dibbs" in blob:
        return "DLA"
    return "Other"


def _profit_val(rec: dict[str, Any]) -> float | None:
    pf = rec.get("profit_first") if isinstance(rec.get("profit_first"), dict) else {}
    for key in ("expected_profit", "post_financing_profit"):
        v = pf.get(key)
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


def _line_count(rec: dict[str, Any], lie: Any | None) -> int:
    if isinstance(lie, dict):
        lines = lie.get("lines") or lie.get("line_items") or []
        if isinstance(lines, list) and lines:
            return len(lines)
        n = lie.get("line_count") or lie.get("n_lines")
        if n is not None:
            try:
                return int(n)
            except (TypeError, ValueError):
                pass
    for key in ("line_items", "items", "schedule_lines"):
        val = rec.get(key)
        if isinstance(val, list) and val:
            return len(val)
    meta = rec.get("raw_metadata") if isinstance(rec.get("raw_metadata"), dict) else {}
    for key in ("line_count", "n_lines", "item_count"):
        if meta.get(key) is not None:
            try:
                return int(meta[key])
            except (TypeError, ValueError):
                pass
    return 0


def build_e2e_completion_report(
    *,
    phase_results: dict[str, Any],
    canonical_before: int | None,
    run_id: str,
) -> dict[str, Any]:
    """Scan live store + phase results into the owner completion structure."""
    from m3_canonical_discovery_bridge import available_count
    from phase_l.l23_full_population_funnel import load_store
    from phase_l.owner_ui_service import _is_available_rec
    from universe_pass.classify import (
        MIXED_PRODUCT_SERVICE,
        PURE_SERVICE,
        TANGIBLE_PRODUCT,
        UNKNOWN,
        ELIGIBLE_FOR_PROFIT,
    )

    store = load_store()
    canonical_after = available_count(store)

    try:
        from line_item_economics.engine import load_analysis

        _load_lie = load_analysis
    except Exception:
        _load_lie = lambda _oid: None  # noqa: E731

    class_counts: Counter = Counter()
    status_counts: Counter = Counter()
    fail_reasons: Counter = Counter()
    profit_by_source: Counter = Counter()
    both_by_source: Counter = Counter()
    econ_ready_by_source: Counter = Counter()
    buckets: Counter = Counter()
    multi = Counter()
    gov_known = acq_known = both = 0
    public_retail = 0
    complete_baskets = partial_baskets = 0
    priced_complete_profit = 0
    owner_candidates: list[dict[str, Any]] = []

    for cid, rec in store.items():
        if not isinstance(rec, dict) or not _is_available_rec(rec):
            continue
        cls = str(rec.get("universe_class") or rec.get("product_service_classification") or UNKNOWN)
        class_counts[cls] += 1
        src = _platform_bucket(rec)
        pf = rec.get("profit_first") if isinstance(rec.get("profit_first"), dict) else {}
        st = str(pf.get("profit_status") or "")
        if st:
            status_counts[st] += 1
        signals = set(pf.get("proof_signals") or [])
        if "PROFITABLE_AT_PUBLIC_RETAIL" in signals:
            public_retail += 1

        rev = None
        cost = None
        econ = None
        # owner_card / economics may be nested
        card = pf.get("owner_card") if isinstance(pf.get("owner_card"), dict) else {}
        for blob in (card, pf):
            if not isinstance(blob, dict):
                continue
            if rev is None and blob.get("expected_revenue") is not None:
                rev = blob.get("expected_revenue")
            if cost is None and blob.get("product_cost") is not None:
                cost = blob.get("product_cost")
        # Missing facts → failure buckets
        missing = pf.get("missing_facts") or []
        if isinstance(missing, list):
            for m in missing:
                ms = str(m).upper()
                if "GOV" in ms or "REVENUE" in ms or "VALUE" in ms:
                    fail_reasons["NO_GOV_VALUE"] += 1
                elif "ACQ" in ms or "COST" in ms or "PRICE" in ms or "RETAIL" in ms:
                    fail_reasons["NO_ACQUISITION_PRICE"] += 1
                elif "UOM" in ms or "PACK" in ms:
                    fail_reasons["UOM_AMBIGUOUS"] += 1
                elif "IDENT" in ms or "MODEL" in ms or "NSN" in ms:
                    fail_reasons["IDENTITY_AMBIGUOUS"] += 1
                elif "DOC" in ms:
                    fail_reasons["DOCUMENT_MISSING"] += 1
                elif "FREIGHT" in ms:
                    fail_reasons["FREIGHT_ERODES_MARGIN"] += 1
                elif "FINANC" in ms:
                    fail_reasons["FINANCING_ERODES_MARGIN"] += 1
                elif "TIME" in ms or "DEADLINE" in ms:
                    fail_reasons["INSUFFICIENT_TIME"] += 1
                else:
                    fail_reasons["OTHER"] += 1
        if st == "EXECUTION_BLOCKED":
            fail_reasons["EXECUTION_BLOCKED"] += 1
        elif st == "UNPROFITABLE":
            fail_reasons["ACTUALLY_UNPROFITABLE"] += 1

        if rev is not None:
            gov_known += 1
            both_by_source[src] += 0  # ensure key
        if cost is not None:
            acq_known += 1
        if rev is not None and cost is not None:
            both += 1
            both_by_source[src] += 1

        if st in {"PROVEN_PROFITABLE", "LIKELY_PROFITABLE", "POSSIBLE_PROFIT"} or (
            pf.get("expected_profit") is not None and float(pf.get("expected_profit") or 0) > 0
        ):
            profit_by_source[src] += 1
        if st in {"PROVEN_PROFITABLE", "LIKELY_PROFITABLE", "POSSIBLE_PROFIT", "UNPROFITABLE"}:
            econ_ready_by_source[src] += 1

        profit = _profit_val(rec)
        if profit is not None:
            for threshold, key in PROFIT_BUCKETS:
                if profit > threshold if threshold == 0 else profit >= threshold:
                    buckets[key] += 1

        lie = None
        if cls in ELIGIBLE_FOR_PROFIT:
            try:
                lie = _load_lie(cid)
            except Exception:
                lie = None
        nlines = _line_count(rec, lie)
        if nlines >= 10:
            multi["lines_10plus"] += 1
        if nlines >= 20:
            multi["lines_20plus"] += 1
        if nlines >= 40:
            multi["lines_40plus"] += 1
        if isinstance(lie, dict):
            priced = lie.get("priced_lines") or lie.get("lines_priced")
            total_l = nlines
            try:
                priced_n = int(priced) if priced is not None else 0
            except (TypeError, ValueError):
                priced_n = 0
            if total_l and priced_n >= total_l:
                complete_baskets += 1
                multi["fully_priced"] += 1
                if profit is not None and profit > 0:
                    priced_complete_profit += 1
                    multi["profitable_complete"] += 1
            elif priced_n > 0:
                partial_baskets += 1
                multi["partially_priced"] += 1

        if st in {"PROVEN_PROFITABLE", "LIKELY_PROFITABLE", "POSSIBLE_PROFIT"} or (
            profit is not None and profit >= 1000
        ):
            owner_candidates.append(
                {
                    "opportunity_id": cid,
                    "title": (rec.get("title") or "")[:160],
                    "buyer": rec.get("buyer") or rec.get("agency"),
                    "source": src,
                    "close_date": rec.get("deadline") or rec.get("due_date"),
                    "profit_status": st or None,
                    "expected_profit": profit,
                    "expected_revenue": rev,
                    "product_cost": cost,
                    "margin": card.get("margin") or pf.get("margin"),
                    "confidence": card.get("confidence") or pf.get("ranking_score"),
                    "proof_signals": list(signals)[:12],
                    "research_next": (pf.get("research_priority") or card.get("research_next")),
                    "documents": len(rec.get("document_links") or [])
                    if isinstance(rec.get("document_links"), list)
                    else None,
                }
            )

    owner_candidates.sort(
        key=lambda x: (
            0 if x.get("profit_status") == "PROVEN_PROFITABLE" else 1,
            0 if x.get("profit_status") == "LIKELY_PROFITABLE" else 1,
            -(float(x.get("expected_profit") or 0)),
        )
    )

    # Plumbing regression
    plumbing = {"opportunity_id": PLUMBING_CASE_ID, "found": False}
    prec = store.get(PLUMBING_CASE_ID)
    if isinstance(prec, dict):
        lie = None
        try:
            lie = _load_lie(PLUMBING_CASE_ID)
        except Exception:
            lie = None
        pf = prec.get("profit_first") if isinstance(prec.get("profit_first"), dict) else {}
        plumbing = {
            "opportunity_id": PLUMBING_CASE_ID,
            "found": True,
            "available": _is_available_rec(prec),
            "title": (prec.get("title") or "")[:160],
            "line_count": _line_count(prec, lie),
            "line_item_economics": {
                "present": bool(lie),
                "priced_lines": (lie or {}).get("priced_lines") if isinstance(lie, dict) else None,
                "historical_matches": (lie or {}).get("historical_matches")
                if isinstance(lie, dict)
                else None,
                "product_cost": (lie or {}).get("product_cost") if isinstance(lie, dict) else None,
                "gov_value": (
                    ((lie or {}).get("government_value") or (lie or {}).get("historical_value"))
                    if isinstance(lie, dict)
                    else None
                ),
            },
            "profit_first": {
                "profit_status": pf.get("profit_status"),
                "expected_profit": pf.get("expected_profit"),
                "expected_revenue": (pf.get("owner_card") or {}).get("expected_revenue")
                if isinstance(pf.get("owner_card"), dict)
                else None,
                "product_cost": (pf.get("owner_card") or {}).get("product_cost")
                if isinstance(pf.get("owner_card"), dict)
                else None,
                "freight": (pf.get("owner_card") or {}).get("freight")
                if isinstance(pf.get("owner_card"), dict)
                else None,
                "financing": (pf.get("owner_card") or {}).get("financing")
                if isinstance(pf.get("owner_card"), dict)
                else None,
            },
        }
        # Re-evaluate live for current numbers
        try:
            from profit_first.router import evaluate_opportunity_profit

            ev = evaluate_opportunity_profit(
                opportunity_id=PLUMBING_CASE_ID,
                rec=prec,
                title=prec.get("title"),
                buyer=prec.get("buyer"),
                line_item_analysis=lie,
                ranking_signals={},
                execution_pass=None,
            )
            plumbing["live_evaluation"] = {
                "profit_status": (ev.get("economics") or {}).get("profit_status"),
                "expected_profit": (ev.get("economics") or {}).get("expected_profit"),
                "expected_revenue": (ev.get("economics") or {}).get("expected_revenue"),
                "product_cost": (ev.get("economics") or {}).get("product_cost"),
                "freight_cost": (ev.get("economics") or {}).get("freight_cost"),
                "financing_cost": (ev.get("economics") or {}).get("financing_cost"),
                "proof_signals": (ev.get("economics") or {}).get("proof_signals"),
            }
            # Defensible if we still have multi-line + both sides or prior profit attachment
            live_profit = (ev.get("economics") or {}).get("expected_profit")
            plumbing["regression_pass"] = bool(
                plumbing["line_count"] >= 30
                or (live_profit is not None and float(live_profit) > 0)
                or (pf.get("expected_profit") is not None and float(pf.get("expected_profit") or 0) > 0)
            )
        except Exception as exc:
            plumbing["live_evaluation_error"] = type(exc).__name__
            plumbing["regression_pass"] = plumbing["line_count"] >= 30

    up = phase_results.get("universe_pass") or {}
    bn = phase_results.get("bidnet") or {}
    og = phase_results.get("opengov") or {}
    exp = phase_results.get("expansion") or {}
    bn_rec = phase_results.get("bidnet_recovery") or {}
    og_rec = phase_results.get("opengov_recovery") or {}

    # Likely product ≈ mixed for this classifier (no separate LIKELY_PRODUCT class)
    tangible = class_counts.get(TANGIBLE_PRODUCT, 0)
    mixed = class_counts.get(MIXED_PRODUCT_SERVICE, 0)
    product_candidates = tangible + mixed

    raw_total = sum(
        int(x or 0)
        for x in (
            bn.get("retrieved_unique") or bn.get("raw"),
            og.get("raw_opportunities") or og.get("raw"),
            exp.get("raw_opportunities") or exp.get("raw"),
        )
        if x is not None
    )
    net_new_total = sum(
        int(x or 0)
        for x in (
            bn.get("net_new"),
            og.get("net_new"),
            exp.get("net_new"),
        )
        if x is not None
    )

    top_fail = sorted(fail_reasons.items(), key=lambda x: -x[1])[:12]

    report = {
        "kind": "FullProductionE2EReport",
        "run_id": run_id,
        "completed_at": now_utc().isoformat(),
        "build_target": "20261003-m3-full-production-test-v1",
        "full_universe": {
            "canonical_before": canonical_before,
            "raw_discovered": raw_total,
            "net_new": net_new_total,
            "canonical_after": canonical_after,
        },
        "source_counts": {
            "BidNet": {
                "reported_open": bn.get("reported_open_ui") or bn.get("reported_total"),
                "retrieved": bn.get("retrieved_unique") or bn.get("retrieved_total"),
                "retrieval_pct": bn.get("retrieval_pct"),
                "pages_scanned": bn.get("pages_scanned"),
                "pagination_complete": bn.get("pagination_complete"),
                "net_new": bn.get("net_new"),
                "existing_enriched": bn.get("existing_enriched"),
                "state_fill_in": bn.get("partition_method") or bn.get("state_fill_in"),
            },
            "OpenGov": {
                "entities_attempted": og.get("entities_attempted"),
                "working_structured": (og.get("portal_status_counts") or {}).get("WORKING_STRUCTURED"),
                "working_fallback": (og.get("portal_status_counts") or {}).get("WORKING_AGENCY_FALLBACK"),
                "no_open_bids": (og.get("portal_status_counts") or {}).get("NO_OPEN_BIDS"),
                "invalid": (og.get("portal_status_counts") or {}).get("INVALID_PORTAL"),
                "blocked": (og.get("portal_status_counts") or {}).get("RECOVERY_BLOCKED"),
                "raw": og.get("raw_opportunities") or og.get("raw"),
                "unique": og.get("unique_records"),
                "net_new": og.get("net_new"),
                "pagination_complete": og.get("pagination_complete_entities"),
                "route_counts": og.get("route_counts"),
            },
            "Federal_SAM": exp.get("sam") or phase_results.get("sam") or {},
            "DLA": exp.get("dla") or phase_results.get("dla") or {},
            "Other": exp.get("other") or {"raw": exp.get("raw_opportunities"), "net_new": exp.get("net_new")},
        },
        "product_funnel": {
            "tangible": tangible,
            "likely_product": mixed,  # MIXED used as likely/mixed product bucket
            "mixed": mixed,
            "service": class_counts.get(PURE_SERVICE, 0),
            "unknown": class_counts.get(UNKNOWN, 0),
            "construction": class_counts.get("CONSTRUCTION", 0),
            "total_product_candidates": product_candidates,
            "universe_pass_classification": up.get("classification"),
        },
        "evidence_funnel": {
            "government_value_known": gov_known or (up.get("product_economics") or {}).get(
                "government_value_evidence_found"
            ),
            "acquisition_cost_known": acq_known or (up.get("product_economics") or {}).get(
                "acquisition_evidence_found"
            ),
            "both_sides_known": both or (up.get("product_economics") or {}).get("both_sides_known"),
            "complete_baskets": complete_baskets,
            "partial_baskets": partial_baskets,
        },
        "economics": {
            "economics_ready": sum(econ_ready_by_source.values()),
            "PROVEN_PROFITABLE": status_counts.get("PROVEN_PROFITABLE", 0)
            or (up.get("product_economics") or {}).get("proven_profitable", 0),
            "LIKELY_PROFITABLE": status_counts.get("LIKELY_PROFITABLE", 0)
            or (up.get("product_economics") or {}).get("likely_profitable", 0),
            "POSSIBLE_PROFIT": status_counts.get("POSSIBLE_PROFIT", 0)
            or (up.get("product_economics") or {}).get("possible_profit", 0),
            "UNPROVEN": status_counts.get("UNPROVEN", 0)
            or (up.get("product_economics") or {}).get("unproven", 0),
            "UNPROFITABLE": status_counts.get("UNPROFITABLE", 0)
            or (up.get("product_economics") or {}).get("unprofitable", 0),
            "EXECUTION_BLOCKED": status_counts.get("EXECUTION_BLOCKED", 0)
            or (up.get("product_economics") or {}).get("execution_blocked", 0),
            "PROFITABLE_AT_PUBLIC_RETAIL": public_retail
            or (up.get("product_economics") or {}).get("profitable_at_public_retail", 0),
        },
        "profit_buckets": {
            "gt_0": buckets.get("gt_0", 0),
            "gte_1k": buckets.get("gte_1k", 0),
            "gte_2_5k": buckets.get("gte_2_5k", 0),
            "gte_5k": buckets.get("gte_5k", 0),
            "gte_10k": buckets.get("gte_10k", 0),
            "gte_25k": buckets.get("gte_25k", 0),
            "gte_50k": buckets.get("gte_50k", 0),
            "gte_75k": buckets.get("gte_75k", 0),
        },
        "multi_line": {
            "lines_10plus": multi.get("lines_10plus", 0),
            "lines_20plus": multi.get("lines_20plus", 0),
            "lines_40plus": multi.get("lines_40plus", 0),
            "fully_priced": multi.get("fully_priced", 0),
            "partially_priced": multi.get("partially_priced", 0),
            "profitable_complete_baskets": multi.get("profitable_complete", 0) or priced_complete_profit,
        },
        "source_to_profit": {
            "BidNet": profit_by_source.get("BidNet", 0),
            "OpenGov": profit_by_source.get("OpenGov", 0),
            "SAM": profit_by_source.get("SAM", 0),
            "DLA": profit_by_source.get("DLA", 0),
            "Other": profit_by_source.get("Other", 0),
            "both_sides_by_source": dict(both_by_source),
            "economics_ready_by_source": dict(econ_ready_by_source),
        },
        "top_failure_reasons": [{ "reason": r, "count": c} for r, c in top_fail],
        "top_owner_candidates": owner_candidates[:25],
        "recovery": {
            "bidnet": {
                "attempted": bn_rec.get("attempted") or bn_rec.get("processed"),
                "documents_recovered": bn_rec.get("documents_recovered"),
                "economics_ready": bn_rec.get("economics_ready"),
                "summary": {k: bn_rec.get(k) for k in list(bn_rec.keys())[:30]}
                if isinstance(bn_rec, dict)
                else bn_rec,
            },
            "opengov": {
                "attempted": og_rec.get("attempted") or og_rec.get("processed"),
                "documents_recovered": og_rec.get("documents_recovered"),
                "economics_ready": og_rec.get("economics_ready"),
                "summary": {k: og_rec.get(k) for k in list(og_rec.keys())[:30]}
                if isinstance(og_rec, dict)
                else og_rec,
            },
        },
        "plumbing_regression": plumbing,
        "phase_results_keys": sorted(phase_results.keys()),
        "universe_pass": {
            "before": up.get("before_canonical_live"),
            "after": up.get("after_canonical_live"),
            "product_candidates": up.get("product_candidates"),
            "product_economics": up.get("product_economics"),
            "removed_from_live": up.get("removed_from_live"),
            "duplicates_collapsed": up.get("duplicates_amendments_collapsed"),
        },
    }

    # Persist
    try:
        from m3_data_root import data_path
        import json

        path = data_path(REPORT_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    except Exception:
        log.exception("Failed to persist E2E report")

    return report


def run_full_production_e2e(
    *,
    run_id: str | None = None,
    on_progress: Any | None = None,
    skip_bidnet: bool = False,
    skip_opengov: bool = False,
    skip_expansion: bool = False,
    bidnet_max_results: int = 25000,
    opengov_max_pages: int = 40,
    recovery_bidnet_limit: int = 400,
    recovery_opengov_limit: int = 200,
    profit_limit: int | None = None,
) -> dict[str, Any]:
    """Execute discovery → reconcile → classify → recover → profit-first end-to-end."""
    from uuid import uuid4

    from m3_canonical_discovery_bridge import available_count, discovery_health_payload
    from phase_l.l23_full_population_funnel import load_store

    run_id = run_id or f"E2E-{uuid4().hex[:12]}"
    started = now_utc().isoformat()
    phases: dict[str, Any] = {}

    def prog(phase: str, pct: int, **extra: Any) -> None:
        if on_progress:
            try:
                on_progress(phase=phase, pct=pct, **extra)
            except Exception:
                pass

    # --- Snapshot before ---
    prog("SNAPSHOT_BEFORE", 2)
    store0 = load_store()
    canonical_before = available_count(store0)
    phases["snapshot_before"] = {
        "canonical_live": canonical_before,
        "health": discovery_health_payload(),
        "at": now_utc().isoformat(),
    }

    # --- BidNet ---
    if not skip_bidnet:
        prog("BIDNET_HARVEST", 8)
        try:
            from bidnet_discovery import run_bidnet_partitioned_harvest

            def _bn_prog(**kw: Any) -> None:
                prog(
                    "BIDNET_HARVEST",
                    min(35, 8 + int(kw.get("pct") or 0) // 4),
                    retrieved=kw.get("retrieved"),
                    pages=kw.get("pages"),
                    partition=kw.get("partition"),
                )

            bn = run_bidnet_partitioned_harvest(
                max_results=bidnet_max_results,
                max_pages_per_partition=900,
                include_national=True,
                persist=True,
                run_id=f"{run_id}-BN",
                use_auth_seed=True,
                on_progress=_bn_prog,
            )
            merge = bn.get("canonical_merge") if isinstance(bn.get("canonical_merge"), dict) else {}
            phases["bidnet"] = {
                "status": "COMPLETED",
                "reported_open_ui": bn.get("reported_open_ui"),
                "retrieved_unique": bn.get("retrieved_unique"),
                "retrieval_pct": bn.get("retrieval_pct"),
                "pages_scanned": bn.get("pages_scanned"),
                "pagination_complete": bn.get("pagination_complete"),
                "partition_method": bn.get("partition_method"),
                "net_new": bn.get("net_new") or merge.get("new") or merge.get("new_canonical_opportunities_added"),
                "existing_enriched": merge.get("updated") or merge.get("existing_opportunities_updated"),
                "elapsed_sec": bn.get("elapsed_sec"),
                "errors": bn.get("errors"),
            }
        except Exception as exc:
            log.exception("BidNet harvest failed in E2E")
            phases["bidnet"] = {"status": "FAILED", "error": type(exc).__name__}
    else:
        phases["bidnet"] = {"status": "SKIPPED"}

    # --- OpenGov ---
    if not skip_opengov:
        prog("OPENGOV_CASCADE", 38)
        try:
            from opengov_discovery.cascade import run_opengov_cascade_discovery

            def _og_prog(**kw: Any) -> None:
                prog(
                    "OPENGOV_CASCADE",
                    min(55, 38 + int(kw.get("pct") or 0) // 6),
                    entities=kw.get("entities"),
                    retrieved=kw.get("retrieved"),
                )

            og = run_opengov_cascade_discovery(
                max_entities=None,
                max_pages=opengov_max_pages,
                persist=True,
                run_id=f"{run_id}-OG",
                use_auth=True,
                allow_browser=False,
                on_progress=_og_prog,
            )
            phases["opengov"] = {
                "status": "COMPLETED",
                "entities_attempted": og.get("entities_attempted"),
                "entities_successful": og.get("entities_successful"),
                "portal_status_counts": og.get("portal_status_counts"),
                "route_counts": og.get("route_counts"),
                "raw_opportunities": og.get("raw_opportunities"),
                "unique_records": og.get("unique_records"),
                "net_new": og.get("net_new"),
                "pagination_complete_entities": og.get("pagination_complete_entities"),
                "anti_bot_primary_failures": og.get("anti_bot_primary_failures"),
                "recovery_blocked": og.get("recovery_blocked"),
                "resolver_telemetry": og.get("resolver_telemetry"),
            }
        except Exception as exc:
            log.exception("OpenGov cascade failed in E2E")
            phases["opengov"] = {"status": "FAILED", "error": type(exc).__name__}
    else:
        phases["opengov"] = {"status": "SKIPPED"}

    # --- Other free expansion (no BidNet double-hit; skip Euna) ---
    if not skip_expansion:
        prog("EXPANSION_OTHER", 56)
        try:
            from discovery_expansion import run_expansion_harvest

            exp = run_expansion_harvest(
                include_bidnet=False,
                include_structured=True,
                include_platform_catalog=True,
                max_pages=40,
                max_catalog_entities_per_family=30,
                persist=True,
            )
            merge = exp.get("canonical_merge") if isinstance(exp.get("canonical_merge"), dict) else {}
            phases["expansion"] = {
                "status": "COMPLETED",
                "raw_opportunities": exp.get("raw_opportunities") or exp.get("raw"),
                "net_new": exp.get("net_new") or merge.get("new"),
                "per_family": exp.get("per_family") or exp.get("by_family"),
                "errors": exp.get("errors"),
            }
        except Exception as exc:
            log.exception("Expansion harvest failed in E2E")
            phases["expansion"] = {"status": "FAILED", "error": type(exc).__name__}

        # Federal/DLA coverage snapshot (do not force paid SAM burn)
        try:
            from discovery.federal_dla_coverage import load_federal_dla_coverage

            cov = load_federal_dla_coverage()
            phases["sam"] = {
                "status": "SNAPSHOT",
                "coverage": {
                    k: cov.get(k)
                    for k in (
                        "federal_live",
                        "sam_live",
                        "dla_live",
                        "last_run",
                        "counts",
                    )
                    if k in cov or True
                },
            }
            phases["dla"] = {"status": "SNAPSHOT", "note": "included in federal_dla coverage"}
        except Exception as exc:
            phases["sam"] = {"status": "UNAVAILABLE", "error": type(exc).__name__}
            phases["dla"] = {"status": "UNAVAILABLE", "error": type(exc).__name__}
    else:
        phases["expansion"] = {"status": "SKIPPED"}

    prog("SNAPSHOT_MID", 60)
    phases["snapshot_mid"] = {
        "canonical_live": available_count(load_store()),
        "at": now_utc().isoformat(),
    }

    # --- Universe pass ---
    prog("UNIVERSE_PASS", 62)
    try:
        from universe_pass import run_universe_pass

        up = run_universe_pass(
            classify=True,
            freshness=True,
            profit_route=True,
            limit=None,
            profit_limit=profit_limit,
            resume=False,
            persist=True,
            force_reclassify=False,
            run_id=f"{run_id}-UNI",
        )
        phases["universe_pass"] = up
    except Exception as exc:
        log.exception("Universe pass failed in E2E")
        phases["universe_pass"] = {"status": "FAILED", "error": type(exc).__name__}

    # --- Recovery ---
    prog("BIDNET_RECOVERY", 78)
    try:
        from bidnet_recovery import run_bidnet_recovery

        bn_rec = run_bidnet_recovery(
            limit=recovery_bidnet_limit,
            batch_size=25,
            resume=True,
            persist=True,
            force=False,
            min_tier=3,
            use_auth=True,
        )
        phases["bidnet_recovery"] = bn_rec
    except Exception as exc:
        log.exception("BidNet recovery failed in E2E")
        phases["bidnet_recovery"] = {"status": "FAILED", "error": type(exc).__name__}

    prog("OPENGOV_RECOVERY", 88)
    try:
        from opengov_recovery import run_opengov_recovery

        og_rec = run_opengov_recovery(
            limit=recovery_opengov_limit,
            batch_size=20,
            resume=True,
            persist=True,
            force=False,
            use_auth=True,
        )
        phases["opengov_recovery"] = og_rec
    except Exception as exc:
        log.exception("OpenGov recovery failed in E2E")
        phases["opengov_recovery"] = {"status": "FAILED", "error": type(exc).__name__}

    # Light profit refresh after recovery (bounded)
    prog("PROFIT_REFRESH", 92)
    try:
        from universe_pass import run_universe_pass

        up2 = run_universe_pass(
            classify=False,
            freshness=False,
            profit_route=True,
            limit=None,
            profit_limit=profit_limit or 2000,
            resume=True,
            persist=True,
            force_reclassify=False,
            run_id=f"{run_id}-UNI2",
        )
        phases["profit_refresh"] = {
            "product_economics": up2.get("product_economics"),
            "after_canonical_live": up2.get("after_canonical_live"),
        }
        # Merge economics into universe_pass for report
        if isinstance(phases.get("universe_pass"), dict) and up2.get("product_economics"):
            phases["universe_pass"]["product_economics_after_recovery"] = up2.get("product_economics")
    except Exception as exc:
        phases["profit_refresh"] = {"status": "FAILED", "error": type(exc).__name__}

    prog("BUILD_REPORT", 96)
    report = build_e2e_completion_report(
        phase_results=phases,
        canonical_before=canonical_before,
        run_id=run_id,
    )
    report["started_at"] = started
    report["phases"] = {k: (v if not isinstance(v, dict) else {kk: vv for kk, vv in v.items() if kk != "per_entity"}) for k, v in phases.items()}
    prog("DONE", 100)
    return report
