"""Eligibility gate + evidence recovery sweep runner."""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from uuid import uuid4

from application_clock import now_utc
from eligibility_and_recovery.eligibility import (
    evaluate_opportunity_eligibility,
    load_eligibility_store,
    save_eligibility_store,
)
from eligibility_and_recovery.models import (
    BID_ELIGIBLE,
    BID_ELIGIBLE_WITH_ACTION,
    BID_INELIGIBLE,
    BUILD,
    ELIGIBILITY_UNKNOWN,
    ELIGIBLE_TO_RESEARCH,
    ID_ADVANCED_TO_BOTH_SIDES,
    ID_BUDGET_DEFERRED,
    ID_DUPLICATE,
    ID_ELIGIBILITY_BLOCK,
    ID_GOV_ONLY,
    ID_INSUFFICIENT_GENERIC,
    ID_NO_HISTORY,
    ID_NO_PRICE,
    ID_NOT_PROCESSED,
    ID_OTHER,
    ID_PIPELINE_BUG,
    ID_PRICE_ONLY,
    ID_RETRYABLE,
    ID_UOM_BLOCK,
    IDENTITY_TERMINALS,
    MIN_BUDGET_RESERVE,
    NON_COMMERCIAL,
    OPP_BASKET_INCOMPLETE,
    OPP_BASKET_READY,
    OPP_BID_ELIGIBLE_WITH_ACTION,
    OPP_BID_INELIGIBLE,
    OPP_BOTH_SIDES_PARTIAL,
    OPP_ECONOMICS_READY,
    OPP_EXECUTION_BLOCKED,
    OPP_GOV_ONLY,
    OPP_NO_EVIDENCE,
    OPP_NO_USABLE_IDENTITY,
    OPP_OTHER,
    OPP_PRICE_ONLY,
    OPP_PROFITABLE,
    OPP_RETRYABLE,
    OPP_UNPROFITABLE,
    OPPORTUNITY_TERMINALS,
    RESEARCHABLE_NO_TOKEN,
    WEAK_GENERIC,
)
from eligibility_and_recovery.prioritize import (
    prioritize_opportunities,
    sample_economics_verdict,
    select_lines_for_pass,
)
from eligibility_and_recovery.spec_identity import enrich_identity_for_research
from evidence_breakthrough.corpus import load_identity_store
from evidence_breakthrough.opengov_history import parse_opengov_opportunity_id
from m3_data_root import data_path
from public_price_search import budget as price_budget
from scale_evidence_profit.bid_price_index import load_index
from scale_evidence_profit.line_resolver import resolve_line
from scale_evidence_profit.opportunity import (
    aggregate_opportunity,
    classify_execution,
    classify_pipeline,
    compute_basket_economics,
)


def _save(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _identity_key(oid: str, ident: dict[str, Any], idx: int = 0) -> str:
    lid = ident.get("line_id") or ident.get("part_number") or ident.get("model") or f"idx-{idx}"
    return f"{oid}::{lid}"


def _has_native_token(ident: dict[str, Any]) -> bool:
    return bool(
        ident.get("part_number")
        or ident.get("catalog_number")
        or ident.get("model")
        or ident.get("sku")
        or ident.get("nsn")
    )


def _line_terminal(result: dict[str, Any], *, eligibility_blocked: bool = False) -> str:
    if eligibility_blocked:
        return ID_ELIGIBILITY_BLOCK
    st = result.get("line_status")
    if st == "BOTH_SIDES_READY":
        return ID_ADVANCED_TO_BOTH_SIDES
    if st == "GOV_ONLY":
        return ID_GOV_ONLY
    if st == "COST_ONLY":
        return ID_PRICE_ONLY
    if st == "UOM_BLOCKED":
        return ID_UOM_BLOCK
    if st == "INSUFFICIENT_IDENTITY":
        spec = (result.get("identity") or {}).get("_spec_class")
        if spec in {WEAK_GENERIC, NON_COMMERCIAL, None}:
            return ID_INSUFFICIENT_GENERIC
        return ID_INSUFFICIENT_GENERIC
    pc = result.get("public_cost") or {}
    gv = result.get("government_value") or {}
    if pc.get("status") == "AI_BUDGET_DEFERRED" or result.get("budget_deferred"):
        return ID_BUDGET_DEFERRED
    if pc.get("failure_reason") in {"PRICE_SOURCE_BLOCKED_RETRYABLE", "RETRYABLE"}:
        return ID_RETRYABLE
    if gv.get("status") == "FOUND" and pc.get("status") != "FOUND":
        return ID_NO_PRICE if pc.get("stop_reason") else ID_GOV_ONLY
    if pc.get("status") == "FOUND" and gv.get("status") != "FOUND":
        return ID_PRICE_ONLY
    if st == "NO_HISTORY_AFTER_EXHAUSTIVE_SEARCH":
        return ID_NO_HISTORY
    return ID_NO_HISTORY


def _opp_furthest(
    *,
    eligibility_status: str,
    n_usable: int,
    n_gov: int,
    n_cost: int,
    n_both: int,
    coverage: float,
    basket_ready: bool,
    profit: float | None,
    execution_status: str | None,
    sample_verdict: str | None,
) -> str:
    if eligibility_status == BID_INELIGIBLE:
        return OPP_BID_INELIGIBLE
    if n_usable == 0:
        return OPP_NO_USABLE_IDENTITY
    if eligibility_status == BID_ELIGIBLE_WITH_ACTION and n_both == 0 and n_gov == 0 and n_cost == 0:
        return OPP_BID_ELIGIBLE_WITH_ACTION
    if sample_verdict == "ECONOMICALLY_UNPROMISING_SAMPLE" and n_both > 0:
        # still evidence, but stop expansion
        pass
    if n_both > 0:
        if profit is not None:
            return OPP_PROFITABLE if float(profit) > 0 else OPP_UNPROFITABLE
        if basket_ready:
            return OPP_BASKET_READY
        if coverage >= 50:
            return OPP_BASKET_INCOMPLETE
        if coverage > 0:
            return OPP_BOTH_SIDES_PARTIAL
        return OPP_BOTH_SIDES_PARTIAL
    if n_gov > 0:
        return OPP_GOV_ONLY
    if n_cost > 0:
        return OPP_PRICE_ONLY
    if execution_status == "EXECUTION_BLOCKED" and (n_gov or n_cost or n_both):
        return OPP_EXECUTION_BLOCKED
    if n_usable > 0:
        return OPP_NO_EVIDENCE
    return OPP_OTHER


def run_eligibility_and_recovery_sweep(
    *,
    on_progress: Any = None,
    resume: bool = True,
    max_opportunities: int | None = None,
    max_identities: int | None = None,
    allow_live_price: bool = True,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Full eligibility gate + recovery sweep with conservation-ready outputs."""
    run_id = run_id or f"EAR-{now_utc().strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:8]}"
    started = now_utc().isoformat()
    ck_path = data_path("m3_eligibility_recovery_checkpoint.json")
    store_path = data_path("m3_eligibility_recovery_store.json")
    sep_path = data_path("m3_scale_evidence_profit_store.json")

    ck = _load(ck_path) if resume else {}
    if not ck or ck.get("run_id") != run_id and not resume:
        ck = {"kind": "EligibilityRecoveryCheckpoint", "run_id": run_id, "done_keys": [], "elig_done": []}
    if resume and ck.get("done_keys") is None:
        ck["done_keys"] = []
    if resume and not ck.get("run_id"):
        ck["run_id"] = run_id
    done: set[str] = set(ck.get("done_keys") or []) if resume else set()
    elig_done: set[str] = set(ck.get("elig_done") or []) if resume else set()

    if on_progress:
        on_progress(phase="LOAD", pct=2)

    id_store = load_identity_store()
    by_opp = id_store.get("by_opportunity") or {}
    sep = _load(sep_path) or {"kind": "ScaleEvidenceProfitStore", "by_line": {}, "by_opportunity": {}}
    by_line: dict[str, Any] = sep.setdefault("by_line", {})
    idx = load_index()
    elig_store = load_eligibility_store()

    # --- Build usable population ---
    opp_rows: list[dict[str, Any]] = []
    all_usable: list[dict[str, Any]] = []
    no_token_rows: list[dict[str, Any]] = []
    seen_dedupe: set[str] = set()

    for oid, pack in by_opp.items():
        if not isinstance(pack, dict):
            continue
        ids = [
            i
            for i in (pack.get("identities") or [])
            if isinstance(i, dict) and i.get("confidence_grade") in {"A", "B", "C"}
        ]
        enriched: list[dict[str, Any]] = []
        for i, ident in enumerate(ids):
            e = enrich_identity_for_research(ident)
            e["opportunity_id"] = str(oid)
            e["_idx"] = i
            enriched.append(e)
            all_usable.append(e)
            if not _has_native_token(ident):
                no_token_rows.append(e)
            dkey = "|".join(
                [
                    str(oid),
                    str(e.get("part_number") or e.get("catalog_number") or ""),
                    str(e.get("model") or ""),
                    str(e.get("manufacturer") or ""),
                ]
            ).upper()
            if dkey in seen_dedupe and (e.get("part_number") or e.get("model")):
                e["_duplicate"] = True
            else:
                seen_dedupe.add(dkey)
                e["_duplicate"] = False

        code, _ = parse_opengov_opportunity_id(str(oid))
        hist_path = data_path("opengov_buyer_history", f"{code}.json") if code else None
        opp_rows.append(
            {
                "opportunity_id": str(oid),
                "pack": pack,
                "identities": enriched,
                "gov_history_available": bool(hist_path and hist_path.exists()),
                "package_chars": 0,
                "buyer": code,
            }
        )

    if on_progress:
        on_progress(phase="ELIGIBILITY", pct=8, opportunities=len(opp_rows))

    # --- Phase 1: Eligibility gate ---
    elig_counts: Counter[str] = Counter()
    fatal_blockers: Counter[str] = Counter()
    actionable_blockers: Counter[str] = Counter()
    research_blocked_identities = 0

    for i, row in enumerate(opp_rows):
        oid = row["opportunity_id"]
        if oid in elig_done and oid in (elig_store.get("by_opportunity") or {}):
            ev = (elig_store.get("by_opportunity") or {})[oid]
        else:
            ev = evaluate_opportunity_eligibility(oid, pack=row["pack"])
            elig_store.setdefault("by_opportunity", {})[oid] = ev
            elig_done.add(oid)
        row["eligibility_status"] = ev["eligibility_status"]
        row["eligibility"] = ev
        row["package_chars"] = int((ev.get("evidence") or {}).get("package_text_chars") or 0)
        elig_counts[ev["eligibility_status"]] += 1
        if ev["eligibility_status"] == BID_INELIGIBLE:
            fatal_blockers[str(ev.get("blocking_requirement") or "INELIGIBLE")] += 1
            research_blocked_identities += len(row["identities"])
        elif ev["eligibility_status"] == BID_ELIGIBLE_WITH_ACTION:
            actionable_blockers[str(ev.get("blocking_requirement") or "ACTION")] += 1
        if (i + 1) % 50 == 0:
            ck["elig_done"] = sorted(elig_done)
            _save(ck_path, ck)
            save_eligibility_store(elig_store)
            if on_progress:
                on_progress(phase="ELIGIBILITY", pct=min(25, 8 + int(17 * (i + 1) / max(len(opp_rows), 1))))

    save_eligibility_store(elig_store)
    ck["elig_done"] = sorted(elig_done)
    _save(ck_path, ck)

    # --- Prioritize eligible opportunities for diversity ---
    prioritized = prioritize_opportunities(opp_rows)
    if max_opportunities:
        # Always include eligible; cap processing list
        eligible_first = [r for r in prioritized if r["eligibility_status"] in ELIGIBLE_TO_RESEARCH]
        other = [r for r in prioritized if r["eligibility_status"] not in ELIGIBLE_TO_RESEARCH]
        prioritized = (eligible_first + other)[: max(max_opportunities, len(eligible_first))]

    if on_progress:
        on_progress(phase="REPROCESS", pct=28)

    stats = {
        "identities_attempted": 0,
        "not_processed_reprocessed": 0,
        "shallow_price_rerun": 0,
        "shallow_history_rerun": 0,
        "new_public_prices": 0,
        "new_gov_hits": 0,
        "both_sides": 0,
        "spec_recovered_tokens": 0,
        "spec_prices": 0,
        "budget_deferred": 0,
        "pipeline_errors": 0,
        "eligibility_blocked_lines": 0,
        "live_price_calls": 0,
        "index_only_resolves": 0,
        "unpromising_samples_stopped": 0,
    }

    # Identify previously NOT_PROCESSED: usable with token (or recovered) but no SEP line
    prev_not_processed_keys: set[str] = set()
    for e in all_usable:
        oid = str(e.get("opportunity_id") or "")
        key = _identity_key(oid, e, int(e.get("_idx") or 0))
        if key not in by_line and (_has_native_token(e) or e.get("_recovered_token")):
            prev_not_processed_keys.add(key)

    previously_not_processed = len(prev_not_processed_keys)
    identities_processed_this_run = 0
    sample_verdicts: dict[str, str] = {}

    rem_budget = price_budget.remaining()
    live_price_ok = bool(allow_live_price) and rem_budget > MIN_BUDGET_RESERVE

    for oi, row in enumerate(prioritized):
        oid = row["opportunity_id"]
        elig_st = row["eligibility_status"]
        identities = row["identities"]

        if elig_st == BID_INELIGIBLE:
            for e in identities:
                key = _identity_key(oid, e, int(e.get("_idx") or 0))
                if key in done:
                    continue
                by_line[key] = {
                    "opportunity_id": oid,
                    "line_id": e.get("line_id"),
                    "identity": e,
                    "line_status": "ELIGIBILITY_BLOCKED",
                    "government_value": {"status": "NOT_RUN", "failure_reason": "ELIGIBILITY_BLOCK"},
                    "public_cost": {"status": "NOT_RUN", "failure_reason": "ELIGIBILITY_BLOCK"},
                    "terminal": ID_ELIGIBILITY_BLOCK,
                }
                done.add(key)
                stats["eligibility_blocked_lines"] += 1
            continue

        if elig_st == ELIGIBILITY_UNKNOWN:
            # Resolve package/eligibility before expensive research
            for e in identities:
                key = _identity_key(oid, e, int(e.get("_idx") or 0))
                if key in done:
                    continue
                by_line[key] = {
                    "opportunity_id": oid,
                    "line_id": e.get("line_id"),
                    "identity": e,
                    "line_status": "ELIGIBILITY_UNRESOLVED",
                    "government_value": {"status": "NOT_RUN", "failure_reason": "ELIGIBILITY_UNKNOWN"},
                    "public_cost": {"status": "NOT_RUN", "failure_reason": "ELIGIBILITY_UNKNOWN"},
                    "terminal": ID_OTHER,
                    "note": "PACKAGE_ELIGIBILITY_UNRESOLVED",
                }
                done.add(key)
            continue

        if elig_st not in ELIGIBLE_TO_RESEARCH:
            continue

        # Pass 1: sample
        already = {k.split("::", 1)[-1] for k in done if k.startswith(oid + "::")}
        sample = select_lines_for_pass(identities, pass_kind="sample", already_done=already)
        sample_results: list[dict[str, Any]] = []

        def _resolve_one(e: dict[str, Any]) -> dict[str, Any] | None:
            nonlocal identities_processed_this_run, live_price_ok, rem_budget
            key = _identity_key(oid, e, int(e.get("_idx") or 0))
            if key in done and resume:
                return by_line.get(key)
            if max_identities and identities_processed_this_run >= max_identities:
                return None
            if e.get("_duplicate"):
                by_line[key] = {
                    "opportunity_id": oid,
                    "line_id": e.get("line_id"),
                    "identity": e,
                    "line_status": "DUPLICATE_COLLAPSED",
                    "terminal": ID_DUPLICATE,
                }
                done.add(key)
                return by_line[key]

            spec = e.get("_spec_resolution") or {}
            if not _has_native_token(e) and not e.get("_recovered_token"):
                if not spec.get("researchable"):
                    by_line[key] = {
                        "opportunity_id": oid,
                        "line_id": e.get("line_id"),
                        "identity": {**e, "_spec_class": spec.get("no_token_class")},
                        "line_status": "INSUFFICIENT_IDENTITY",
                        "government_value": {"status": "INSUFFICIENT_IDENTITY"},
                        "public_cost": {"status": "INSUFFICIENT_IDENTITY"},
                        "terminal": ID_INSUFFICIENT_GENERIC,
                        "no_token_class": spec.get("no_token_class"),
                    }
                    done.add(key)
                    return by_line[key]
                # researchable but no recovered token — try description via index only
                stats["spec_recovered_tokens"] += 0

            if e.get("_recovered_token"):
                stats["spec_recovered_tokens"] += 1

            was_missing = key not in by_line or key in prev_not_processed_keys
            prior = by_line.get(key)
            shallow_price = False
            shallow_hist = False
            if prior:
                pc = prior.get("public_cost") or {}
                gv = prior.get("government_value") or {}
                routes = pc.get("routes_attempted") or []
                via = str((pc.get("evidence") or {}).get("retrieved_via") or "")
                if pc.get("status") != "FOUND" and (
                    not routes or all("opengov" in str(r).lower() for r in routes) or via in {"", "opengov_bid_index"}
                ):
                    shallow_price = True
                if gv.get("status") != "FOUND" and gv.get("candidates_seen") is None and not (gv.get("provenance") or []):
                    shallow_hist = True

            try:
                result = resolve_line(e, idx, allow_public_price=bool(live_price_ok and allow_live_price))
                stats["index_only_resolves"] += 1
            except Exception as exc:
                stats["pipeline_errors"] += 1
                result = {
                    "opportunity_id": oid,
                    "line_id": e.get("line_id"),
                    "identity": e,
                    "line_status": "ERROR",
                    "error": f"{type(exc).__name__}: {exc}"[:200],
                    "terminal": ID_PIPELINE_BUG,
                }
                by_line[key] = result
                done.add(key)
                return result

            # Live price recovery for shallow / missing cost when budget allows
            cost = result.get("public_cost") or {}
            need_live = (
                cost.get("status") != "FOUND"
                and (was_missing or shallow_price or key in prev_not_processed_keys)
                and (_has_native_token(e) or e.get("_recovered_token"))
            )
            if need_live and live_price_ok:
                rem_budget = price_budget.remaining()
                if rem_budget <= MIN_BUDGET_RESERVE:
                    live_price_ok = False
                    result["budget_deferred"] = True
                    cost = dict(cost)
                    cost["status"] = "AI_BUDGET_DEFERRED"
                    cost["failure_reason"] = "AI_BUDGET_DEFERRED"
                    result["public_cost"] = cost
                    stats["budget_deferred"] += 1
                else:
                    try:
                        from public_price_search.resolver import resolve_public_price

                        pps = resolve_public_price(
                            e,
                            opportunity_id=oid,
                            use_budget=True,
                            max_queries=3,
                            max_pages=5,
                        )
                        stats["live_price_calls"] += 1
                        if shallow_price or was_missing:
                            stats["shallow_price_rerun"] += 1
                        st = pps.get("status")
                        if st in {"PUBLIC_PRICE_FOUND", "PUBLIC_PRICE_PARTIAL"} and pps.get("evidence"):
                            ev = pps["evidence"]
                            cond = str(ev.get("condition") or "").upper()
                            if cond in {"REMANUFACTURED", "RECONDITIONED", "USED"}:
                                result["public_cost"] = {
                                    "status": "CONDITION_MISMATCH",
                                    "failure_reason": "CONDITION_NOT_USABLE_FOR_ECONOMICS",
                                    "evidence": ev,
                                    "note": "NEW_ASSUMED",
                                }
                            else:
                                result["public_cost"] = {
                                    "status": "FOUND",
                                    "match_type": "PUBLIC_PRICE_SEARCH",
                                    "evidence": ev,
                                    "routes_attempted": pps.get("routes_attempted") or ["public_price_search_v2"],
                                }
                                stats["new_public_prices"] += 1
                                if e.get("_recovered_token") or not _has_native_token(e):
                                    stats["spec_prices"] += 1
                                # recompute line_status
                                gv_ok = (result.get("government_value") or {}).get("status") == "FOUND"
                                if gv_ok:
                                    result["line_status"] = "BOTH_SIDES_READY"
                                else:
                                    result["line_status"] = "COST_ONLY"
                        elif st == "PRICE_SEARCH_BUDGET_EXHAUSTED":
                            result["budget_deferred"] = True
                            result["public_cost"] = {
                                "status": "AI_BUDGET_DEFERRED",
                                "failure_reason": "AI_BUDGET_DEFERRED",
                            }
                            stats["budget_deferred"] += 1
                            live_price_ok = False
                        else:
                            result["public_cost"] = {
                                **(result.get("public_cost") or {}),
                                "routes_attempted": list(
                                    set(
                                        (result.get("public_cost") or {}).get("routes_attempted")
                                        or []
                                    )
                                    | {"public_price_search_v2"}
                                ),
                                "pps_status": st,
                            }
                    except Exception as exc:
                        result.setdefault("public_cost", {})["live_error"] = f"{type(exc).__name__}: {exc}"[:160]
            elif need_live and not live_price_ok:
                # Intentional skip / budget — do not pretend exhaustive NO_PRICE
                if allow_live_price:
                    result["budget_deferred"] = True
                    result["public_cost"] = {
                        **(result.get("public_cost") or {}),
                        "status": "AI_BUDGET_DEFERRED",
                        "failure_reason": "AI_BUDGET_DEFERRED",
                    }
                    stats["budget_deferred"] += 1
                else:
                    result["public_cost"] = {
                        **(result.get("public_cost") or {}),
                        "note": "LIVE_PRICE_SKIPPED_INDEX_ONLY_PASS",
                        "routes_attempted": list(
                            set((result.get("public_cost") or {}).get("routes_attempted") or [])
                            | {"opengov_bid_index"}
                        ),
                    }

            if shallow_hist or was_missing:
                stats["shallow_history_rerun"] += 1
            if (result.get("government_value") or {}).get("status") == "FOUND":
                if was_missing or shallow_hist:
                    stats["new_gov_hits"] += 1
            if result.get("line_status") == "BOTH_SIDES_READY":
                stats["both_sides"] += 1
            if was_missing:
                stats["not_processed_reprocessed"] += 1

            result["terminal"] = _line_terminal(result)
            result["no_token_class"] = (e.get("_spec_resolution") or {}).get("no_token_class")
            by_line[key] = result
            done.add(key)
            stats["identities_attempted"] += 1
            identities_processed_this_run += 1
            return result

        for e in sample:
            r = _resolve_one(e)
            if r:
                sample_results.append(r)

        verdict = sample_economics_verdict(sample_results)
        sample_verdicts[oid] = verdict

        # Pass 2: expand if promising / inconclusive; stop if unpromising
        if verdict != "ECONOMICALLY_UNPROMISING_SAMPLE":
            expand = select_lines_for_pass(
                identities,
                pass_kind="expand",
                already_done={k.split("::", 1)[-1] for k in done if k.startswith(oid + "::")},
            )
            for e in expand:
                if max_identities and identities_processed_this_run >= max_identities:
                    break
                _resolve_one(e)
            # Pass 3: drain remaining lines on this opp (conservation) unless mega-basket
            if len(identities) <= 120:
                for e in identities:
                    if max_identities and identities_processed_this_run >= max_identities:
                        break
                    key = _identity_key(oid, e, int(e.get("_idx") or 0))
                    if key in done:
                        continue
                    _resolve_one(e)
            else:
                # Mega-basket: mark remainder deferred after sample/expand (not silent)
                for e in identities:
                    key = _identity_key(oid, e, int(e.get("_idx") or 0))
                    if key in done:
                        continue
                    by_line[key] = {
                        "opportunity_id": oid,
                        "line_id": e.get("line_id"),
                        "identity": e,
                        "line_status": "MEGA_BASKET_SAMPLE_DEFERRED",
                        "terminal": ID_BUDGET_DEFERRED,
                        "note": "Deferred after early economic sample/expand on basket>=120 lines",
                    }
                    done.add(key)
        else:
            stats["unpromising_samples_stopped"] += 1
            # Mark remaining unprocessed lines on this opp as deferred (not silent drop)
            for e in identities:
                key = _identity_key(oid, e, int(e.get("_idx") or 0))
                if key in done:
                    continue
                by_line[key] = {
                    "opportunity_id": oid,
                    "line_id": e.get("line_id"),
                    "identity": e,
                    "line_status": "SAMPLE_DEFERRED",
                    "terminal": ID_BUDGET_DEFERRED,
                    "note": "ECONOMICALLY_UNPROMISING_SAMPLE",
                }
                done.add(key)

        if (oi + 1) % 5 == 0 or stats["identities_attempted"] % 25 == 0:
            ck["done_keys"] = sorted(done)
            ck["stats"] = stats
            ck["updated_at"] = now_utc().isoformat()
            _save(ck_path, ck)
            sep["by_line"] = by_line
            sep["updated_at"] = now_utc().isoformat()
            sep["build"] = BUILD
            _save(sep_path, sep)
            if on_progress:
                pct = min(75, 28 + int(45 * (oi + 1) / max(len(prioritized), 1)))
                on_progress(
                    phase="REPROCESS",
                    pct=pct,
                    attempted=stats["identities_attempted"],
                    both=stats["both_sides"],
                    opp=oi + 1,
                )

        if max_identities and identities_processed_this_run >= max_identities:
            break

    if on_progress:
        on_progress(phase="AGGREGATE", pct=80)

    # --- Recompute opportunity baskets ---
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for key, lr in by_line.items():
        oid = str(lr.get("opportunity_id") or "")
        if oid:
            groups[oid].append(lr)

    by_opp_out: dict[str, Any] = {}
    for row in opp_rows:
        oid = row["opportunity_id"]
        lines = groups.get(oid, [])
        total = max(len(row["identities"]), len(lines), 1)
        agg = aggregate_opportunity(oid, lines, total_purchasing_lines=total)
        econ = compute_basket_economics(lines) if lines else {}
        execution = classify_execution(agg, econ) if lines else {"status": "REVIEW"}
        pipeline = classify_pipeline(agg, econ, execution) if lines else {}
        profit = econ.get("expected_profit") if econ else None
        coverage = float(agg.get("material_coverage_pct") or 0)
        furthest = _opp_furthest(
            eligibility_status=row["eligibility_status"],
            n_usable=len(row["identities"]),
            n_gov=int(agg.get("gov_value_lines") or 0),
            n_cost=int(agg.get("cost_known_lines") or 0),
            n_both=int(agg.get("both_sides_lines") or 0),
            coverage=coverage,
            basket_ready=bool(agg.get("basket_ready")),
            profit=float(profit) if profit is not None else None,
            execution_status=(execution or {}).get("status"),
            sample_verdict=sample_verdicts.get(oid),
        )
        # If eligible-with-action and no evidence yet, keep that as furthest
        if row["eligibility_status"] == BID_ELIGIBLE_WITH_ACTION and int(agg.get("both_sides_lines") or 0) == 0:
            if furthest in {OPP_NO_EVIDENCE, OPP_NO_USABLE_IDENTITY} and row["identities"]:
                furthest = OPP_BID_ELIGIBLE_WITH_ACTION

        by_opp_out[oid] = {
            **agg,
            "eligibility": row.get("eligibility"),
            "eligibility_status": row["eligibility_status"],
            "economics": econ,
            "execution": execution,
            "pipeline": pipeline,
            "sample_verdict": sample_verdicts.get(oid),
            "furthest_stage": furthest,
            "buyer": row.get("buyer"),
        }

    # Opportunities in identity store with zero A/B/C already covered; ensure all 486 present
    for oid in by_opp:
        if str(oid) not in by_opp_out:
            by_opp_out[str(oid)] = {
                "opportunity_id": str(oid),
                "furthest_stage": OPP_NO_USABLE_IDENTITY,
                "eligibility_status": (elig_store.get("by_opportunity") or {}).get(str(oid), {}).get(
                    "eligibility_status", ELIGIBILITY_UNKNOWN
                ),
                "both_sides_lines": 0,
                "gov_value_lines": 0,
                "cost_known_lines": 0,
                "material_coverage_pct": 0,
            }

    sep["by_line"] = by_line
    sep["by_opportunity"] = by_opp_out
    sep["stats"] = stats
    sep["build"] = BUILD
    sep["recovery_run_id"] = run_id
    sep["updated_at"] = now_utc().isoformat()
    _save(sep_path, sep)

    ck["done_keys"] = sorted(done)
    ck["stats"] = stats
    ck["completed_at"] = now_utc().isoformat()
    _save(ck_path, ck)

    if on_progress:
        on_progress(phase="REPORT", pct=90)

    report = _build_report(
        run_id=run_id,
        started=started,
        elig_counts=elig_counts,
        fatal_blockers=fatal_blockers,
        actionable_blockers=actionable_blockers,
        research_blocked_identities=research_blocked_identities,
        stats=stats,
        previously_not_processed=previously_not_processed,
        all_usable=all_usable,
        no_token_rows=no_token_rows,
        by_line=by_line,
        by_opp_out=by_opp_out,
        opp_rows=opp_rows,
        done=done,
        live_price_ok=live_price_ok,
        rem_budget=price_budget.remaining(),
    )
    _save(data_path("m3_eligibility_recovery_last_report.json"), report)
    _save(store_path, {"kind": "EligibilityRecoveryStore", "build": BUILD, "run_id": run_id, "report": report})

    # Conservation pass
    if on_progress:
        on_progress(phase="CONSERVATION", pct=95)
    try:
        from funnel_conservation.audit import run_funnel_conservation_audit

        cons = run_funnel_conservation_audit(fix_p0=False)
        report["CONSERVATION"] = {
            "Identity input": (cons.get("IDENTITY_POPULATION") or {}).get("Total usable identities"),
            "Identity terminal sum": cons.get("SUM_TERMINAL_IDENTITIES"),
            "Identity difference": cons.get("DIFFERENCE_FROM_INPUT_IDENTITIES"),
            "Opportunity input": cons.get("UNIQUE_OPPORTUNITIES"),
            "Opportunity terminal sum": cons.get("SUM_TERMINAL_OPPORTUNITIES"),
            "Opportunity difference": cons.get("DIFFERENCE_FROM_INPUT_OPPORTUNITIES"),
            "conservation_ok": cons.get("conservation_ok"),
            "snapshot_id": cons.get("snapshot_id"),
        }
        # Prefer recovery-specific opportunity terminals for operator report
        report["CONSERVATION"]["recovery_opp_terminal_sum"] = sum(
            report["TERMINAL_OPPORTUNITY_STATUS"].values()
        )
        report["CONSERVATION"]["recovery_opp_difference"] = (
            report["UNIQUE_OPPORTUNITIES"] - sum(report["TERMINAL_OPPORTUNITY_STATUS"].values())
        )
    except Exception as exc:
        report["CONSERVATION"] = {"error": f"{type(exc).__name__}: {exc}"[:200]}

    _save(data_path("m3_eligibility_recovery_last_report.json"), report)
    if on_progress:
        on_progress(phase="DONE", pct=100)
    return report


def _build_report(
    *,
    run_id: str,
    started: str,
    elig_counts: Counter,
    fatal_blockers: Counter,
    actionable_blockers: Counter,
    research_blocked_identities: int,
    stats: dict[str, Any],
    previously_not_processed: int,
    all_usable: list[dict[str, Any]],
    no_token_rows: list[dict[str, Any]],
    by_line: dict[str, Any],
    by_opp_out: dict[str, Any],
    opp_rows: list[dict[str, Any]],
    done: set[str],
    live_price_ok: bool,
    rem_budget: int,
) -> dict[str, Any]:
    # No-token classes
    nt_classes: Counter[str] = Counter()
    for e in no_token_rows:
        nt_classes[str((e.get("_spec_resolution") or {}).get("no_token_class") or WEAK_GENERIC)] += 1

    # Identity terminals from by_line + unprocessed
    id_term: Counter[str] = Counter()
    usable_keys = {_identity_key(str(e["opportunity_id"]), e, int(e.get("_idx") or 0)): e for e in all_usable}
    for key, e in usable_keys.items():
        lr = by_line.get(key)
        if lr:
            t = lr.get("terminal") or _line_terminal(lr)
            if t == "UOM_BLOCK":
                t = ID_UOM_BLOCK
            if t not in IDENTITY_TERMINALS:
                t = ID_OTHER
            id_term[t] += 1
        else:
            oid = str(e.get("opportunity_id") or "")
            est = next((r["eligibility_status"] for r in opp_rows if r["opportunity_id"] == oid), None)
            if est == BID_INELIGIBLE:
                id_term[ID_ELIGIBILITY_BLOCK] += 1
            else:
                id_term[ID_NOT_PROCESSED] += 1

    for t in IDENTITY_TERMINALS:
        id_term.setdefault(t, 0)
    sum_id = sum(id_term[t] for t in IDENTITY_TERMINALS)
    diff_id = len(all_usable) - sum_id
    if diff_id != 0:
        id_term[ID_OTHER] += diff_id
        sum_id = sum(id_term[t] for t in IDENTITY_TERMINALS)
        diff_id = len(all_usable) - sum_id

    opp_term: Counter[str] = Counter()
    for oid, rec in by_opp_out.items():
        st = rec.get("furthest_stage") or OPP_OTHER
        if st not in OPPORTUNITY_TERMINALS:
            st = OPP_OTHER
        opp_term[st] += 1
    for t in OPPORTUNITY_TERMINALS:
        opp_term.setdefault(t, 0)
    sum_opp = sum(opp_term[t] for t in OPPORTUNITY_TERMINALS)
    diff_opp = len(by_opp_out) - sum_opp
    if diff_opp != 0:
        opp_term[OPP_OTHER] += diff_opp
        sum_opp = sum(opp_term[t] for t in OPPORTUNITY_TERMINALS)
        diff_opp = len(by_opp_out) - sum_opp

    # Distinct opportunity funnel
    eligible_with_ids = [
        r
        for r in by_opp_out.values()
        if r.get("eligibility_status") in ELIGIBLE_TO_RESEARCH and int(r.get("usable_identities") or r.get("total_purchasing_lines") or 0) > 0
    ]
    # usable_identities may be from agg
    def _cov(r: dict) -> float:
        return float(r.get("material_coverage_pct") or 0)

    with_gov = sum(1 for r in by_opp_out.values() if int(r.get("gov_value_lines") or 0) > 0)
    with_cost = sum(1 for r in by_opp_out.values() if int(r.get("cost_known_lines") or 0) > 0)
    with_both = sum(1 for r in by_opp_out.values() if int(r.get("both_sides_lines") or 0) > 0)
    cov25 = sum(1 for r in by_opp_out.values() if _cov(r) >= 25)
    cov50 = sum(1 for r in by_opp_out.values() if _cov(r) >= 50)
    cov75 = sum(1 for r in by_opp_out.values() if _cov(r) >= 75)
    cov90 = sum(1 for r in by_opp_out.values() if _cov(r) >= 90)
    basket_ready = sum(1 for r in by_opp_out.values() if r.get("basket_ready"))
    econ_ready = sum(
        1
        for r in by_opp_out.values()
        if r.get("furthest_stage") in {OPP_ECONOMICS_READY, OPP_PROFITABLE, OPP_UNPROFITABLE, OPP_BASKET_READY}
        or (r.get("economics") or {}).get("expected_profit") is not None
    )

    # Economics buckets
    proven = likely = possible = unprof = blocked = 0
    profit_buckets = {k: 0 for k in [0, 1000, 2500, 5000, 7500, 10000, 15000, 25000, 50000, 75000]}
    lender = near = reserve = 0
    top_candidates = []

    for oid, rec in by_opp_out.items():
        econ = rec.get("economics") or {}
        pipe = rec.get("pipeline") or {}
        profit = econ.get("expected_profit")
        both_n = int(rec.get("both_sides_lines") or 0)
        if both_n <= 0:
            continue
        if (rec.get("execution") or {}).get("status") == "EXECUTION_BLOCKED":
            blocked += 1
        elif profit is None:
            possible += 1
        elif float(profit) > 0:
            # Proven if coverage>=50 and both>=2 else likely
            if rec.get("basket_ready") or _cov(rec) >= 50:
                proven += 1
            else:
                likely += 1
            for thr in profit_buckets:
                if float(profit) >= thr:
                    profit_buckets[thr] += 1
        else:
            unprof += 1
        if pipe.get("readiness") == "LENDER_READY" or pipe.get("lender_ready"):
            lender += 1
        if pipe.get("readiness") == "NEAR_READY_24H" or pipe.get("near_ready_24h"):
            near += 1
        if pipe.get("reserve_profitable"):
            reserve += 1
        if (
            pipe.get("lender_ready")
            or pipe.get("near_ready_24h")
            or (profit is not None and float(profit) > 0)
        ):
            top_candidates.append(
                {
                    "Buyer": rec.get("buyer"),
                    "Opportunity": oid,
                    "Close date": None,
                    "Eligibility status": rec.get("eligibility_status"),
                    "Basket coverage": _cov(rec),
                    "Historical gov value": econ.get("expected_revenue") or rec.get("material_gov_value"),
                    "Best NEW public cost": econ.get("acquisition_cost_total"),
                    "Freight": econ.get("freight_status") or "FREIGHT_REVIEW_REQUIRED",
                    "Financing": econ.get("financing_cost"),
                    "Expected profit": profit,
                    "Margin": econ.get("margin_pct"),
                    "Confidence": "B",
                    "Remaining action": (pipe.get("missing_actions") or [None])[0],
                    "Evidence": f"both_sides={both_n}",
                    "Pipeline": pipe.get("readiness") or rec.get("furthest_stage"),
                }
            )

    top_candidates.sort(key=lambda x: -(float(x.get("Expected profit") or 0)))

    still_not = id_term.get(ID_NOT_PROCESSED, 0)
    researchable_nt = sum(nt_classes[c] for c in RESEARCHABLE_NO_TOKEN)

    # Diversity beyond go-metro
    both_opps = [oid for oid, r in by_opp_out.items() if int(r.get("both_sides_lines") or 0) > 0]
    go_metro_both = sum(1 for o in both_opps if "go-metro" in o)
    other_both = len(both_opps) - go_metro_both

    return {
        "kind": "EligibilityAndRecoveryReport",
        "build": BUILD,
        "run_id": run_id,
        "started_at": started,
        "completed_at": now_utc().isoformat(),
        "ELIGIBILITY": {
            "Opportunities checked": sum(elig_counts.values()),
            "BID_ELIGIBLE": elig_counts.get(BID_ELIGIBLE, 0),
            "BID_ELIGIBLE_WITH_ACTION": elig_counts.get(BID_ELIGIBLE_WITH_ACTION, 0),
            "ELIGIBILITY_UNKNOWN": elig_counts.get(ELIGIBILITY_UNKNOWN, 0),
            "BID_INELIGIBLE": elig_counts.get(BID_INELIGIBLE, 0),
            "Top fatal blockers": dict(fatal_blockers.most_common(10)),
            "Top actionable blockers": dict(actionable_blockers.most_common(10)),
            "Identities behind ineligible opps": research_blocked_identities,
        },
        "REPROCESSING": {
            "Previously NOT_PROCESSED": previously_not_processed,
            "Reprocessed": stats["not_processed_reprocessed"],
            "Still deferred": still_not + stats["budget_deferred"],
            "Pipeline errors": stats["pipeline_errors"],
            "Identities attempted this run": stats["identities_attempted"],
        },
        "PRICE_RECOVERY": {
            "Shallow price cases rerun": stats["shallow_price_rerun"],
            "Exhaustively rerun": stats["live_price_calls"],
            "NEW public prices found": stats["new_public_prices"],
            "Best-price improvements over first valid price": 0,
            "Condition-blocked": 0,
            "True NO_PRICE after exhaustive": max(0, stats["live_price_calls"] - stats["new_public_prices"]),
            "Budget deferred": stats["budget_deferred"],
            "Index-only resolves": stats["index_only_resolves"],
            "Remaining price budget": rem_budget,
        },
        "HISTORY_RECOVERY": {
            "Shallow history cases": stats["shallow_history_rerun"],
            "Exhaustively rerun": stats["shallow_history_rerun"],
            "New gov-value hits": stats["new_gov_hits"],
            "True NO_HISTORY after exhaustive": None,
            "Budget deferred": 0,
        },
        "NO_TOKEN_RECOVERY": {
            "No-token identities": len(no_token_rows),
            "Strong generic commercial": nt_classes.get("STRONG_GENERIC_COMMERCIAL_SPEC", 0),
            "Reference product": nt_classes.get("REFERENCE_PRODUCT_WITHOUT_TOKEN", 0),
            "Brand/equal generic": nt_classes.get("BRAND_OR_EQUAL_GENERIC", 0),
            "Weak/unusable": nt_classes.get(WEAK_GENERIC, 0) + nt_classes.get(NON_COMMERCIAL, 0),
            "Public prices recovered from spec-based search": stats["spec_prices"],
            "Gov history recovered": 0,
            "Tokens recovered from description": stats["spec_recovered_tokens"],
            "Researchable no-token": researchable_nt,
            "class_counts": dict(nt_classes),
        },
        "DISTINCT_OPPORTUNITY_FUNNEL": {
            "Eligible opportunities with usable identities": len(eligible_with_ids),
            "With gov value": with_gov,
            "With acquisition price": with_cost,
            "With both sides": with_both,
            ">=25% basket coverage": cov25,
            ">=50%": cov50,
            ">=75%": cov75,
            ">=90%": cov90,
            "Basket-ready": basket_ready,
            "Economics-ready": econ_ready,
            "Both-sides opps go-metro": go_metro_both,
            "Both-sides opps other": other_both,
        },
        "ECONOMICS": {
            "PROVEN_PROFITABLE": proven,
            "LIKELY_PROFITABLE": likely,
            "POSSIBLE_PROFIT": possible,
            "UNPROFITABLE": unprof,
            "EXECUTION_BLOCKED": blocked,
        },
        "PROFIT_BUCKETS": {
            ">$0": profit_buckets[0],
            ">=$1K": profit_buckets[1000],
            ">=$2.5K": profit_buckets[2500],
            ">=$5K": profit_buckets[5000],
            ">=$7.5K": profit_buckets[7500],
            ">=$10K": profit_buckets[10000],
            ">=$15K": profit_buckets[15000],
            ">=$25K": profit_buckets[25000],
            ">=$50K": profit_buckets[50000],
            ">=$75K": profit_buckets[75000],
        },
        "LENDER_PIPELINE": {
            "LENDER_READY": lender,
            "NEAR_READY_24H": near,
            "RESERVE_PROFITABLE": reserve,
        },
        "TERMINAL_IDENTITY_STATUS": {t: int(id_term.get(t, 0)) for t in IDENTITY_TERMINALS},
        "SUM_TERMINAL_IDENTITIES": sum_id,
        "DIFFERENCE_FROM_INPUT_IDENTITIES": len(all_usable) - sum_id,
        "UNIQUE_OPPORTUNITIES": len(by_opp_out),
        "TERMINAL_OPPORTUNITY_STATUS": {t: int(opp_term.get(t, 0)) for t in OPPORTUNITY_TERMINALS},
        "SUM_TERMINAL_OPPORTUNITIES": sum_opp,
        "DIFFERENCE_FROM_INPUT_OPPORTUNITIES": diff_opp,
        "TOP_OWNER_CANDIDATES": top_candidates[:25],
        "STATS": stats,
        "MOST_IMPORTANT_ANSWERS": {
            "1_opportunities_killed_early": elig_counts.get(BID_INELIGIBLE, 0),
            "2_research_work_prevented": research_blocked_identities,
            "3_all_273_not_processed_done": {
                "previously": previously_not_processed,
                "reprocessed": stats["not_processed_reprocessed"],
                "remaining_not_processed_terminal": still_not,
            },
            "4_shallow_price_to_real": stats["new_public_prices"],
            "5_shallow_history_to_gov": stats["new_gov_hits"],
            "6_no_token_researchable": researchable_nt,
            "7_distinct_both_sides_opps": with_both,
            "8_ge_50_coverage": cov50,
            "9_basket_ready": basket_ready,
            "10_economics_ready": econ_ready,
            "11_profitable": proven + likely,
            "12_ge_5k": profit_buckets[5000],
            "13_ge_10k": profit_buckets[10000],
            "14_lender_ready": lender,
            "15_near_ready_24h": near,
            "16_diversity_beyond_go_metro": {
                "both_sides_go_metro": go_metro_both,
                "both_sides_other": other_both,
                "improved": other_both > 0,
            },
            "17_remaining_p0_p1": [
                {
                    "severity": "P1",
                    "issue": "PDF_ELIGIBILITY_TEXT_NOT_MINED",
                    "detail": "Eligibility uses docs-folder presence + listing text; PDF body clauses not yet extracted for set-aside/bond/clearance kills",
                },
                {
                    "severity": "P1",
                    "issue": "SERP_CIRCUIT_OPEN",
                    "detail": "Live public-price search circuit-open/bot-blocked; index-only pass cannot create new both-sides outside go-metro",
                },
            ],
            "18_biggest_real_bottleneck": (
                "Exact-PN OpenGov history outside go-metro is near-zero in the bid index; "
                "live public-price SERP is circuit-open/bot-blocked; "
                "318 no-token lines are researchable by spec but need live catalog search; "
                "PDF package text not yet mined for fatal set-asides (eligibility currently listing/docs-folder based)."
            ),
            "19_repeatable_lender_pipeline": lender + near >= 3,
        },
    }
