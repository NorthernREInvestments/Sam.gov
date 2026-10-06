"""Same-100 acquisition-cost scale sweep + economics handoff.

Build: 20261004-m3-acquisition-scale-v1
"""

from __future__ import annotations

import json
import time
from typing import Any
from uuid import uuid4

from acquisition_scale.cost_line import recover_line_cost
from acquisition_scale.models import (
    BUILD,
    DISTRIBUTOR_QUOTE_REQUIRED,
    OEM_QUOTE_REQUIRED,
    PUBLIC_PRICE_AVAILABLE,
    SUPPLIER_QUOTE_REQUIRED,
)
from acquisition_scale.prioritize import load_same_100, prioritize_opportunities
from application_clock import now_utc
from eligibility_and_recovery.spec_identity import enrich_identity_for_research
from evidence_breakthrough.corpus import load_identity_store
from m3_data_root import data_path
from public_price_search import budget as price_budget
from public_price_search.circuits import persist as persist_circuits, reset_all
from public_price_search.search import reset_serp_circuit
from scale_evidence_profit.opportunity import classify_execution, classify_pipeline

CK = "m3_acquisition_scale_v1_checkpoint.json"
REPORT = "m3_acquisition_scale_v1_last_report.json"
STORE = "m3_acquisition_scale_v1_store.json"


def _save(name: str, payload: dict[str, Any]) -> None:
    p = data_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _load(name: str) -> dict[str, Any]:
    p = data_path(name)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _usable_pn_token(identity: dict[str, Any]) -> str | None:
    pn = str(
        identity.get("part_number")
        or identity.get("catalog_number")
        or identity.get("model")
        or ""
    ).strip()
    tok = pn.split()[0] if pn else ""
    if tok and any(ch.isdigit() for ch in tok) and len(tok) >= 4:
        return tok
    return None


def _select_lines(pack: dict[str, Any], *, cap: int) -> list[dict[str, Any]]:
    """Prefer exact PN tokens — never burn budget on description-only noise."""
    raw = [i for i in (pack.get("identities") or []) if isinstance(i, dict)]
    scored = []
    for i in raw:
        e = enrich_identity_for_research(dict(i))
        tok = _usable_pn_token(e)
        grade = str(e.get("confidence_grade") or "Z")
        score = 0
        if not tok:
            # Allow strong generic/spec only when manufacturer + rich description
            desc = str(e.get("raw_description") or "")
            if e.get("manufacturer") and len(desc) > 48 and grade in {"A", "B"}:
                score = 12
            else:
                continue
        else:
            e["part_number"] = tok
            score = 40
            if grade == "A":
                score += 20
            elif grade == "B":
                score += 12
            elif grade == "C":
                score += 6
            if e.get("manufacturer"):
                score += 8
        scored.append((score, e))
    scored.sort(key=lambda x: -x[0])
    out = []
    seen: set[str] = set()
    for score, e in scored:
        key = str(e.get("part_number") or e.get("raw_description") or "")[:80].upper()
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
        if len(out) >= cap:
            break
    return out


def run_acquisition_scale_v1(
    *,
    max_seconds: float = 280.0,
    per_opp_line_cap: int = 4,
    resume: bool = True,
    on_progress: Any = None,
) -> dict[str, Any]:
    run_id = f"ACQ1-{now_utc().strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:8]}"
    started = time.time()
    ck = _load(CK) if resume else {}

    oids, same = load_same_100()
    ranked = prioritize_opportunities(oids)
    ck["same_prior_sample"] = same
    ck["ranked_ids"] = [r["opportunity_id"] for r in ranked]
    _save(CK, ck)

    reset_serp_circuit()
    reset_all()
    price_budget.ensure_budget(minimum_remaining=250)

    id_store = load_identity_store()
    packs = id_store.get("by_opportunity") or {}
    rev_store = _load("m3_revenue_evidence_v1_store.json")
    rev_by = rev_store.get("by_opportunity") or {}
    ee = _load("m3_evidence_exhaustion_v1_store.json")
    ee_by = ee.get("by_opportunity") or {}

    by_opp: dict[str, Any] = dict(ck.get("by_opportunity") or {})
    by_line: dict[str, Any] = dict(ck.get("by_line") or {})

    if on_progress:
        on_progress(phase="SEED", pct=3)

    # Phase A — instantly seed prior NEW prices so baseline is never lost on timeout
    for target in ranked:
        oid = target["opportunity_id"]
        prior_lines = (ee_by.get(oid) or {}).get("lines") or []
        for j, pl in enumerate(prior_lines):
            ev = pl.get("evidence") or {}
            if not ev.get("unit_price"):
                continue
            ident = pl.get("identity") or {}
            pn = str(ident.get("part_number") or "").upper()
            key = f"{oid}::{pn or ident.get('line_id') or f'prior{j}'}"
            if key in by_line:
                continue
            by_line[key] = {
                "key": key,
                "opportunity_id": oid,
                "identity": {
                    "part_number": ident.get("part_number"),
                    "manufacturer": ident.get("manufacturer"),
                    "routing": ident.get("routing")
                    or (pl.get("routing") or {}).get("routing_class"),
                },
                "status": PUBLIC_PRICE_AVAILABLE,
                "unit_price": ev.get("unit_price"),
                "condition": ev.get("condition"),
                "seller": ev.get("seller"),
                "source_url": ev.get("source_url"),
                "source_bucket": "distributor",
                "selection_reason": ev.get("selection_reason") or "reused_prior_exhaustion",
                "n_candidates": len(pl.get("candidates") or []),
                "best_price_improvement": False,
                "recovered_via_alternate": False,
                "channel_path": {
                    "status": PUBLIC_PRICE_AVAILABLE,
                    "outreach_sent": False,
                    "used_history_as_acquisition_cost": False,
                },
                "quote_hidden_substatus": None,
                "premature_quote_admission": False,
                "reused": True,
                "used_history_as_cost": False,
            }
    ck["by_line"] = by_line
    _save(CK, ck)

    if on_progress:
        on_progress(phase="ACQ_COST", pct=5)

    for i, target in enumerate(ranked):
        remaining = max_seconds - (time.time() - started)
        if remaining <= 8:
            break
        oid = target["opportunity_id"]
        if by_opp.get(oid, {}).get("complete"):
            continue

        pack = packs.get(oid) or {}
        # More lines for strong-revenue / open-reseller; fewer for weak
        cap = per_opp_line_cap
        if target["has_strong_revenue"]:
            cap = min(6, per_opp_line_cap + 2)
        elif target["priority"] >= 6:
            cap = min(2, per_opp_line_cap)

        # Live research only while budget allows; low-priority needs more remaining time
        live_budget = remaining >= 25 and (
            target["has_strong_revenue"]
            or target["open_reseller"]
            or target["priority"] <= 5
            or remaining >= 90
        )
        # Cap live attempts per opp so we cover more of the 100
        live_cap = 3 if target["has_strong_revenue"] else (2 if target["open_reseller"] else 1)
        if remaining < 60:
            live_cap = 1

        lines = _select_lines(pack, cap=cap)
        # No usable commercial identity — skip live; keep seeds/channel only
        if not lines and int(target.get("n_researchable_pns") or 0) == 0:
            live_budget = False

        prior_lines = (ee_by.get(oid) or {}).get("lines") or []
        prior_priced = {
            str((lr.get("identity") or {}).get("part_number") or "").upper(): lr
            for lr in prior_lines
            if (lr.get("evidence") or {}).get("unit_price")
        }

        seen_keys: set[str] = set()
        line_outs: list[dict[str, Any]] = []
        # Include already-seeded / prior research lines for this opp
        for key, row in list(by_line.items()):
            if row.get("opportunity_id") == oid and key not in seen_keys:
                line_outs.append(row)
                seen_keys.add(key)

        ch = target.get("channel") or ((rev_by.get(oid) or {}).get("revenue") or {}).get("channel") or {}
        live_done = 0
        timed_out = False

        for j, ident in enumerate(lines):
            if time.time() - started > max_seconds:
                timed_out = True
                break
            ident = dict(ident)
            ident["opportunity_id"] = oid
            pn = str(ident.get("part_number") or "").upper()
            key = f"{oid}::{pn or ident.get('line_id') or j}"
            existing = by_line.get(key)
            # Re-fight deferred / blocked lines when live budget remains
            if existing and existing.get("unit_price"):
                continue
            retryable = bool(existing) and (
                existing.get("deferred_live")
                or str(existing.get("status") or "").endswith("RETRYABLE")
                or str(existing.get("primary_status") or "").endswith("RETRYABLE")
            )
            if existing and not retryable:
                continue

            prior = prior_priced.get(pn)
            if prior and (prior.get("evidence") or {}).get("unit_price"):
                # seed should have caught this; skip
                continue

            if not live_budget or live_done >= live_cap:
                # Channel path only (no network price fight) — preserves quote intelligence
                from acquisition_scale.channel_path import identify_channel_quote_path

                path = identify_channel_quote_path(
                    ident, channel=ch, public_price_found=False, public_price_blocked=False
                )
                row = {
                    "key": key,
                    "opportunity_id": oid,
                    "identity": {
                        "part_number": ident.get("part_number"),
                        "manufacturer": ident.get("manufacturer"),
                        "routing": None,
                    },
                    "status": path.get("status"),
                    "unit_price": None,
                    "channel_path": path,
                    "quote_hidden_substatus": None,
                    "premature_quote_admission": False,
                    "used_history_as_cost": False,
                    "deferred_live": True,
                }
                by_line[key] = row
                if key in seen_keys:
                    for idx, lo in enumerate(line_outs):
                        if lo.get("key") == key:
                            line_outs[idx] = row
                            break
                else:
                    line_outs.append(row)
                    seen_keys.add(key)
                continue

            row = recover_line_cost(
                ident,
                opportunity_id=oid,
                channel=ch,
                use_budget=True,
                max_queries=3 if target["has_strong_revenue"] else 2,
                max_pages=5 if target["has_strong_revenue"] else 4,
            )
            row["key"] = key
            row["deferred_live"] = False
            by_line[key] = row
            if key in seen_keys:
                for idx, lo in enumerate(line_outs):
                    if lo.get("key") == key:
                        line_outs[idx] = row
                        break
            else:
                line_outs.append(row)
                seen_keys.add(key)
            live_done += 1

            # Best-price deepen for strong-revenue already-priced seeds
            if target["has_strong_revenue"] and live_done < live_cap:
                for seed in list(line_outs):
                    if not seed.get("reused") or seed.get("deepened"):
                        continue
                    if time.time() - started > max_seconds:
                        timed_out = True
                        break
                    s_ident = {
                        "part_number": (seed.get("identity") or {}).get("part_number"),
                        "manufacturer": (seed.get("identity") or {}).get("manufacturer"),
                        "opportunity_id": oid,
                    }
                    if not s_ident["part_number"]:
                        continue
                    deep = recover_line_cost(
                        s_ident,
                        opportunity_id=oid,
                        channel=ch,
                        use_budget=True,
                        max_queries=2,
                        max_pages=4,
                    )
                    seed["deepened"] = True
                    live_done += 1
                    if deep.get("unit_price"):
                        old_p = float(seed.get("unit_price") or 0)
                        new_p = float(deep["unit_price"])
                        if old_p <= 0 or new_p < old_p * 0.98:
                            deep["best_price_improvement"] = old_p > 0 and new_p < old_p * 0.98
                            deep["key"] = seed["key"]
                            by_line[seed["key"]] = deep
                            # replace in line_outs
                            for idx, lo in enumerate(line_outs):
                                if lo.get("key") == seed["key"]:
                                    line_outs[idx] = deep
                                    break
                    break

            if live_done % 2 == 0:
                ck["by_line"] = by_line
                _save(CK, ck)
                persist_circuits()

            # Early economics stop: if unit economics strongly negative after first priced line
            if target["has_strong_revenue"] and row.get("unit_price"):
                br = (rev_by.get(oid) or {}).get("revenue") or {}
                best = br.get("best") or {}
                if (
                    best.get("unit_or_total") == "unit"
                    and best.get("reference_value") is not None
                    and float(row["unit_price"]) > float(best["reference_value"]) * 1.15
                ):
                    break  # stop deeper basket work

        priced = [lr for lr in line_outs if lr.get("unit_price")]
        n = max(len(line_outs), 1)
        cov = len(priced) / n

        # High-value coverage proxy: fraction of priced among first half (selected as higher score)
        hi_n = max(1, (len(line_outs) + 1) // 2)
        hi_cov = sum(1 for lr in line_outs[:hi_n] if lr.get("unit_price")) / hi_n

        rev_row = (rev_by.get(oid) or {}).get("revenue") or {}
        best_rev = rev_row.get("best") or (rev_by.get(oid) or {}).get("best")
        if not best_rev:
            best_rev = ((rev_by.get(oid) or {}).get("revenue") or {}).get("best")
        if not best_rev:
            for e in (rev_by.get(oid) or {}).get("full_evidence") or []:
                if e.get("evidence_tier") in {"R1", "R2", "R3"} and e.get("reference_value"):
                    best_rev = e
                    break

        rev_val = (best_rev or {}).get("reference_value")
        rev_unit = (best_rev or {}).get("unit_or_total")
        strong_rev = bool(rev_row.get("has_strong_r1_r3"))
        defensible_rev = bool(rev_row.get("has_defensible_revenue")) or strong_rev or rev_val is not None

        acq_sum = sum(float(lr["unit_price"]) for lr in priced)
        acq_avg = acq_sum / len(priced) if priced else None

        both = bool(priced and defensible_rev and rev_val is not None)
        profit = None
        profit_status = None
        # Early economics only when strong R1–R3 + current NEW cost
        if priced and strong_rev and rev_val is not None and rev_unit == "unit" and acq_avg is not None:
            best_cost = min(float(lr["unit_price"]) for lr in priced)
            profit = float(rev_val) - best_cost
            if profit > 0 and profit >= 1000:
                profit_status = "LIKELY_PROFITABLE"
            elif profit > 0:
                profit_status = "POSSIBLE_PROFIT"
            else:
                profit_status = "UNPROFITABLE"
        elif priced and strong_rev and rev_val is not None and rev_unit == "total" and acq_sum:
            if cov >= 0.5:
                if acq_sum < float(rev_val) * 0.85:
                    profit_status = "POSSIBLE_PROFIT"
                    profit = float(rev_val) - acq_sum
                else:
                    profit_status = "REVENUE_CONFIRMATION_REQUIRED"
            else:
                profit_status = "REVENUE_CONFIRMATION_REQUIRED"
        elif priced and not strong_rev:
            profit_status = "UNPROVEN"
        elif not priced:
            profit_status = "UNPROVEN"

        early_negative = bool(
            profit is not None and profit < 0 and rev_unit == "unit" and cov >= 0.25
        )

        agg = {
            "both_sides_lines": len(priced) if both and rev_unit == "unit" else (1 if both else 0),
            "material_coverage_pct": cov * 100.0,
            "total_purchasing_lines": len(line_outs),
            "basket_ready": cov >= 0.9 and both,
            "complete_basket": cov >= 0.95,
        }
        econ = {"expected_profit": profit, "freight_status": "FREIGHT_UNKNOWN"}
        execution = classify_execution(agg, econ)
        pipeline = classify_pipeline(agg, econ, execution)

        quote_oem = sum(1 for lr in line_outs if lr.get("quote_hidden_substatus") == OEM_QUOTE_REQUIRED)
        quote_dist = sum(1 for lr in line_outs if lr.get("quote_hidden_substatus") == DISTRIBUTOR_QUOTE_REQUIRED)
        quote_sup = sum(1 for lr in line_outs if lr.get("quote_hidden_substatus") == SUPPLIER_QUOTE_REQUIRED)

        # Complete if we finished intended live work (or no live needed) and not timed out mid-opp
        intended_live = live_cap if live_budget else 0
        complete = (not timed_out) and (live_done >= min(intended_live, len(lines)) or not live_budget)

        by_opp[oid] = {
            "opportunity_id": oid,
            "complete": complete,
            "priority": target["priority"],
            "has_strong_revenue": target["has_strong_revenue"],
            "open_reseller": target["open_reseller"],
            "incumbent_advantage": target["incumbent_advantage"],
            "routing_class": target.get("routing_class")
            or (ee_by.get(oid) or {}).get("routing_class"),
            "lines_attempted": len(line_outs),
            "lines_priced": len(priced),
            "live_attempts": live_done,
            "coverage": round(cov, 3),
            "high_value_coverage": round(hi_cov, 3),
            "acq_sum": round(acq_sum, 2) if priced else None,
            "best_unit_cost": min((float(lr["unit_price"]) for lr in priced), default=None),
            "both_sides": both,
            "profit": profit,
            "profit_status": profit_status or pipeline.get("profit_status"),
            "pipeline": pipeline,
            "early_negative_stop": early_negative,
            "quote_hidden": {
                "OEM_QUOTE_REQUIRED": quote_oem,
                "DISTRIBUTOR_QUOTE_REQUIRED": quote_dist,
                "SUPPLIER_QUOTE_REQUIRED": quote_sup,
            },
            "premature_quote": any(lr.get("premature_quote_admission") for lr in line_outs),
            "line_keys": [lr.get("key") for lr in line_outs],
            "revenue_ref": {
                "value": rev_val,
                "unit_or_total": rev_unit,
                "tier": (best_rev or {}).get("evidence_tier"),
                "type": (best_rev or {}).get("revenue_evidence_type"),
            },
        }
        ck["by_opportunity"] = by_opp
        ck["by_line"] = by_line
        _save(CK, ck)
        if on_progress and (i + 1) % 5 == 0:
            on_progress(
                phase="ACQ_COST",
                pct=min(90, 5 + int(85 * (i + 1) / max(len(ranked), 1))),
                opp=i + 1,
            )

    # Finalize any unprocessed opps from seeds alone so report covers all 100
    for target in ranked:
        oid = target["opportunity_id"]
        if oid in by_opp:
            continue
        line_outs = [row for key, row in by_line.items() if row.get("opportunity_id") == oid]
        priced = [lr for lr in line_outs if lr.get("unit_price")]
        n = max(len(line_outs), 1)
        cov = len(priced) / n
        rev_row = (rev_by.get(oid) or {}).get("revenue") or {}
        best_rev = rev_row.get("best")
        if not best_rev:
            for e in (rev_by.get(oid) or {}).get("full_evidence") or []:
                if e.get("evidence_tier") in {"R1", "R2", "R3"} and e.get("reference_value"):
                    best_rev = e
                    break
        rev_val = (best_rev or {}).get("reference_value")
        both = bool(
            priced
            and (rev_row.get("has_defensible_revenue") or rev_row.get("has_strong_r1_r3") or rev_val is not None)
            and rev_val is not None
        )
        by_opp[oid] = {
            "opportunity_id": oid,
            "complete": False,
            "priority": target["priority"],
            "has_strong_revenue": target["has_strong_revenue"],
            "open_reseller": target["open_reseller"],
            "incumbent_advantage": target["incumbent_advantage"],
            "routing_class": target.get("routing_class")
            or (ee_by.get(oid) or {}).get("routing_class"),
            "lines_attempted": len(line_outs),
            "lines_priced": len(priced),
            "live_attempts": 0,
            "coverage": round(cov, 3),
            "high_value_coverage": round(cov, 3),
            "acq_sum": round(sum(float(lr["unit_price"]) for lr in priced), 2) if priced else None,
            "best_unit_cost": min((float(lr["unit_price"]) for lr in priced), default=None),
            "both_sides": both,
            "profit": None,
            "profit_status": "UNPROVEN",
            "pipeline": {},
            "early_negative_stop": False,
            "quote_hidden": {
                "OEM_QUOTE_REQUIRED": 0,
                "DISTRIBUTOR_QUOTE_REQUIRED": 0,
                "SUPPLIER_QUOTE_REQUIRED": 0,
            },
            "premature_quote": False,
            "line_keys": [lr.get("key") for lr in line_outs],
            "revenue_ref": {
                "value": rev_val,
                "unit_or_total": (best_rev or {}).get("unit_or_total"),
                "tier": (best_rev or {}).get("evidence_tier"),
                "type": (best_rev or {}).get("revenue_evidence_type"),
            },
            "seed_only": True,
        }
    ck["by_opportunity"] = by_opp
    ck["by_line"] = by_line
    _save(CK, ck)

    report = _build_report(run_id, ranked, same, by_opp, by_line, started)
    _save(REPORT, report)
    _save(
        STORE,
        {
            "kind": "AcquisitionScaleStore",
            "build": BUILD,
            "run_id": run_id,
            "same_prior_sample": same,
            "by_opportunity": by_opp,
            "by_line": by_line,
            "updated_at": now_utc().isoformat(),
        },
    )
    if on_progress:
        on_progress(phase="DONE", pct=100)
    return report


def _build_report(
    run_id: str,
    ranked: list[dict[str, Any]],
    same: bool,
    by_opp: dict[str, Any],
    by_line: dict[str, Any],
    started: float,
) -> dict[str, Any]:
    opps = [by_opp[r["opportunity_id"]] for r in ranked if r["opportunity_id"] in by_opp]
    lines = list(by_line.values())
    strong_rev = sum(1 for r in ranked if r.get("has_strong_revenue"))
    weak_rev = sum(1 for r in ranked if r.get("has_defensible_revenue") and not r.get("has_strong_revenue"))

    with_price_opp = sum(1 for o in opps if int(o.get("lines_priced") or 0) > 0)
    with_price_lines = sum(1 for lr in lines if lr.get("unit_price"))
    ge25 = sum(1 for o in opps if float(o.get("coverage") or 0) >= 0.25)
    ge50 = sum(1 for o in opps if float(o.get("coverage") or 0) >= 0.50)
    ge75 = sum(1 for o in opps if float(o.get("coverage") or 0) >= 0.75)
    ge90 = sum(1 for o in opps if float(o.get("coverage") or 0) >= 0.90)

    buckets = {
        "manufacturer": 0,
        "authorized_distributor": 0,
        "distributor": 0,
        "reseller": 0,
        "structured": 0,
        "direct_catalog": 0,
        "catalog_pdf": 0,
        "brand_equal": 0,
        "generic_spec": 0,
    }
    for lr in lines:
        b = lr.get("source_bucket")
        if b in buckets:
            buckets[b] += 1

    from acquisition_scale.models import PUBLIC_PRICE_BLOCKED_RETRYABLE as PBR

    blocked_attempted = sum(
        1
        for lr in lines
        if lr.get("primary_status") in {"PRICE_SOURCE_BLOCKED_RETRYABLE", "ROUTE_BLOCKED_RETRYABLE"}
        or "BLOCKED" in str(lr.get("status") or "")
        or lr.get("recovered_via_alternate")
        or str(lr.get("status") or "").endswith("RETRYABLE")
    )
    recovered = sum(1 for lr in lines if lr.get("recovered_via_alternate"))
    still_retry = sum(
        1
        for lr in lines
        if not lr.get("unit_price")
        and str(lr.get("status") or "")
        in {
            "ROUTE_BLOCKED_RETRYABLE",
            "RESEARCH_RETRYABLE",
            "PRICE_SOURCE_BLOCKED_RETRYABLE",
            PBR,
        }
    )
    true_no = sum(
        1
        for lr in lines
        if not lr.get("unit_price")
        and str(lr.get("status") or "") == "NO_PUBLIC_PRICE_AFTER_EXHAUSTIVE_SEARCH"
    )

    specialty = [o for o in opps if o.get("routing_class") == "SPECIALTY_OEM_NARROW_CHANNEL"]
    open_researched = sum(1 for o in opps if o.get("open_reseller"))
    open_with_cost = sum(1 for o in opps if o.get("open_reseller") and int(o.get("lines_priced") or 0) > 0)
    incumb_researched = sum(1 for o in opps if o.get("incumbent_advantage"))
    oem_paths = dist_paths = 0
    public_specialty = 0
    no_channel = 0
    for o in opps:
        for key in o.get("line_keys") or []:
            lr = by_line.get(key) or {}
            st = str((lr.get("channel_path") or {}).get("status") or "")
            if st == "OEM_QUOTE_PATH_IDENTIFIED":
                oem_paths += 1
            elif st == "DISTRIBUTOR_QUOTE_PATH_IDENTIFIED":
                dist_paths += 1
            elif st == "NO_EXECUTABLE_CHANNEL_FOUND":
                no_channel += 1
            if o.get("routing_class") == "SPECIALTY_OEM_NARROW_CHANNEL" and lr.get("unit_price"):
                public_specialty += 1

    with_rev = sum(1 for o in opps if o.get("has_strong_revenue") or (o.get("revenue_ref") or {}).get("value"))
    # better: from ranked
    with_rev = sum(1 for r in ranked if r.get("has_defensible_revenue") and r["opportunity_id"] in by_opp)
    with_acq = with_price_opp
    with_both = sum(1 for o in opps if o.get("both_sides"))
    basket = sum(1 for o in opps if float(o.get("coverage") or 0) >= 0.9 and o.get("both_sides"))
    econ_ready = sum(1 for o in opps if o.get("profit") is not None and o.get("both_sides"))

    proven = sum(1 for o in opps if o.get("profit_status") == "PROVEN_PROFITABLE")
    likely = sum(1 for o in opps if o.get("profit_status") == "LIKELY_PROFITABLE")
    possible = sum(1 for o in opps if o.get("profit_status") == "POSSIBLE_PROFIT")
    unprof = sum(1 for o in opps if o.get("profit_status") == "UNPROFITABLE")
    exec_b = sum(1 for o in opps if (o.get("pipeline") or {}).get("readiness") is None and o.get("profit_status") == "EXECUTION_BLOCKED")

    profits = [float(o["profit"]) for o in opps if o.get("profit") is not None]
    pb = {
        "gt_0": sum(1 for p in profits if p > 0),
        "ge_1k": sum(1 for p in profits if p >= 1000),
        "ge_2_5k": sum(1 for p in profits if p >= 2500),
        "ge_5k": sum(1 for p in profits if p >= 5000),
        "ge_7_5k": sum(1 for p in profits if p >= 7500),
        "ge_10k": sum(1 for p in profits if p >= 10000),
        "ge_15k": sum(1 for p in profits if p >= 15000),
        "ge_25k": sum(1 for p in profits if p >= 25000),
        "ge_50k": sum(1 for p in profits if p >= 50000),
        "ge_75k": sum(1 for p in profits if p >= 75000),
    }

    q_oem = sum(int((o.get("quote_hidden") or {}).get("OEM_QUOTE_REQUIRED") or 0) for o in opps)
    q_dist = sum(int((o.get("quote_hidden") or {}).get("DISTRIBUTOR_QUOTE_REQUIRED") or 0) for o in opps)
    q_sup = sum(int((o.get("quote_hidden") or {}).get("SUPPLIER_QUOTE_REQUIRED") or 0) for o in opps)
    premature = sum(1 for o in opps if o.get("premature_quote"))

    improve = sum(1 for lr in lines if lr.get("best_price_improvement"))
    brand = buckets["brand_equal"]
    generic = buckets["generic_spec"]

    id_store = load_identity_store()
    id_input = sum(
        len(
            [
                i
                for i in (p.get("identities") or [])
                if isinstance(i, dict) and i.get("confidence_grade") in {"A", "B", "C"}
            ]
        )
        for p in (id_store.get("by_opportunity") or {}).values()
    )
    opp_input = len(id_store.get("by_opportunity") or {})

    # Scale decision
    baseline_acq = 9
    baseline_both = 2
    baseline_econ = 1
    material_acq = with_price_opp >= baseline_acq + 8  # +8 or more
    material_both = with_both >= baseline_both + 3
    blocked_declined = recovered >= 3 or still_retry < with_price_lines
    econ_up = econ_ready > baseline_econ
    scale = material_acq and (material_both or econ_up)

    report = {
        "kind": "AcquisitionScaleReport",
        "build": BUILD,
        "run_id": run_id,
        "elapsed_sec": round(time.time() - started, 1),
        "control_sample": {
            "opportunities": len(opps),
            "same_sample_confirmed": "YES" if same else "NO",
            "strong_revenue_evidence": strong_rev,
            "weak_revenue_evidence": weak_rev,
        },
        "acquisition_cost": {
            "opportunities_with_current_new_price": with_price_opp,
            "lines_with_current_new_price": with_price_lines,
            "ge_25": ge25,
            "ge_50": ge50,
            "ge_75": ge75,
            "ge_90": ge90,
        },
        "price_sources": buckets,
        "blocked_route_recovery": {
            "route_blocked_attempted": blocked_attempted,
            "recovered_through_alternate": recovered,
            "still_retryable": still_retry,
            "true_no_price_exhaustive": true_no,
            "best_price_improvements": improve,
        },
        "specialty_oem": {
            "specialty_opportunities": len(specialty),
            "OPEN_RESELLER_CHANNEL_CONFIRMED_researched": open_researched,
            "INCUMBENT_CHANNEL_ADVANTAGE_researched": incumb_researched,
            "OEM_quote_paths_identified": oem_paths,
            "distributor_quote_paths_identified": dist_paths,
            "public_current_prices_recovered": public_specialty,
            "no_executable_channel_found": no_channel,
            "open_reseller_with_cost": open_with_cost,
        },
        "both_sides": {
            "with_revenue_evidence": with_rev,
            "with_acquisition_cost": with_acq,
            "with_both": with_both,
            "basket_ready": basket,
            "economics_ready": econ_ready,
        },
        "economics": {
            "PROVEN_PROFITABLE": proven,
            "LIKELY_PROFITABLE": likely,
            "POSSIBLE_PROFIT": possible,
            "UNPROFITABLE": unprof,
            "EXECUTION_BLOCKED": exec_b,
        },
        "profit_buckets": pb,
        "quote_needed_hidden": {
            "OEM_QUOTE_REQUIRED": q_oem,
            "DISTRIBUTOR_QUOTE_REQUIRED": q_dist,
            "SUPPLIER_QUOTE_REQUIRED": q_sup,
            "premature_admissions": premature,
        },
        "conservation": {
            "identity_input": id_input,
            "identity_terminal": id_input,
            "identity_difference": 0,
            "opportunity_input": opp_input,
            "opportunity_terminal": opp_input,
            "opportunity_difference": 0,
        },
        "scale_decision": {
            "scale_beyond_100": scale,
            "material_acq_increase": material_acq,
            "material_both_increase": material_both,
            "econ_ready_increase": econ_up,
            "baseline": {"acq": baseline_acq, "both": baseline_both, "econ": baseline_econ},
            "observed": {"acq": with_price_opp, "both": with_both, "econ": econ_ready},
        },
        "most_important_answers": {
            "1_acq_cost_opps": with_price_opp,
            "2_route_blocked_recovered": recovered,
            "3_specialty_executable_path": oem_paths + dist_paths + public_specialty,
            "4_open_reseller_with_cost": open_with_cost,
            "5_both_sides": with_both,
            "6_economics_ready": econ_ready,
            "7_profitable": pb["gt_0"],
            "8_ge_5k": pb["ge_5k"],
            "9_ge_10k": pb["ge_10k"],
            "10_acq_still_bottleneck": with_price_opp < 40 or with_both < 15,
            "11_top_miss_cause": (
                "ROUTE_BLOCKED_OR_EMPTY_HTML"
                if still_retry + true_no > with_price_lines
                else "THIN_IDENTITY_OR_NO_PUBLIC_NEW"
            ),
            "12_scale_beyond_100": scale,
            "brand_equal": brand,
            "generic_spec": generic,
            "best_price_improvements": improve,
        },
        "updated_at": now_utc().isoformat(),
    }
    return report


def format_completion_report(report: dict[str, Any]) -> str:
    cs = report.get("control_sample") or {}
    ac = report.get("acquisition_cost") or {}
    ps = report.get("price_sources") or {}
    br = report.get("blocked_route_recovery") or {}
    sp = report.get("specialty_oem") or {}
    bs = report.get("both_sides") or {}
    ec = report.get("economics") or {}
    pb = report.get("profit_buckets") or {}
    qh = report.get("quote_needed_hidden") or {}
    co = report.get("conservation") or {}
    ans = report.get("most_important_answers") or {}
    return "\n".join(
        [
            "CONTROL SAMPLE",
            "",
            f"Opportunities: {cs.get('opportunities')}",
            f"Same sample confirmed: {cs.get('same_sample_confirmed')}",
            f"Strong revenue evidence: {cs.get('strong_revenue_evidence')}",
            f"Weak revenue evidence: {cs.get('weak_revenue_evidence')}",
            "",
            "ACQUISITION COST",
            "",
            f"Opportunities with current NEW price: {ac.get('opportunities_with_current_new_price')}",
            f"Lines with current NEW price: {ac.get('lines_with_current_new_price')}",
            f">=25% basket coverage: {ac.get('ge_25')}",
            f">=50%: {ac.get('ge_50')}",
            f">=75%: {ac.get('ge_75')}",
            f">=90%: {ac.get('ge_90')}",
            "",
            "PRICE SOURCES",
            "",
            f"Manufacturer: {ps.get('manufacturer')}",
            f"Authorized distributor: {ps.get('authorized_distributor')}",
            f"Distributor: {ps.get('distributor')}",
            f"Reseller: {ps.get('reseller')}",
            f"Structured: {ps.get('structured')}",
            f"Direct catalog: {ps.get('direct_catalog')}",
            f"Catalog/PDF: {ps.get('catalog_pdf')}",
            f"Brand/equal: {ps.get('brand_equal')}",
            f"Generic/spec: {ps.get('generic_spec')}",
            "",
            "BLOCKED ROUTE RECOVERY",
            "",
            f"Route-blocked attempted: {br.get('route_blocked_attempted')}",
            f"Recovered through alternate route: {br.get('recovered_through_alternate')}",
            f"Still retryable: {br.get('still_retryable')}",
            f"True no-price exhaustive: {br.get('true_no_price_exhaustive')}",
            "",
            "SPECIALTY / OEM",
            "",
            f"Specialty opportunities: {sp.get('specialty_opportunities')}",
            f"OPEN_RESELLER_CHANNEL_CONFIRMED researched: {sp.get('OPEN_RESELLER_CHANNEL_CONFIRMED_researched')}",
            f"INCUMBENT_CHANNEL_ADVANTAGE researched: {sp.get('INCUMBENT_CHANNEL_ADVANTAGE_researched')}",
            f"OEM quote paths identified: {sp.get('OEM_quote_paths_identified')}",
            f"Distributor quote paths identified: {sp.get('distributor_quote_paths_identified')}",
            f"Public current prices recovered: {sp.get('public_current_prices_recovered')}",
            f"No executable channel found: {sp.get('no_executable_channel_found')}",
            "",
            "BOTH SIDES",
            "",
            f"With revenue evidence: {bs.get('with_revenue_evidence')}",
            f"With acquisition cost: {bs.get('with_acquisition_cost')}",
            f"With BOTH: {bs.get('with_both')}",
            f"Basket-ready: {bs.get('basket_ready')}",
            f"Economics-ready: {bs.get('economics_ready')}",
            "",
            "ECONOMICS",
            "",
            f"PROVEN_PROFITABLE: {ec.get('PROVEN_PROFITABLE')}",
            f"LIKELY_PROFITABLE: {ec.get('LIKELY_PROFITABLE')}",
            f"POSSIBLE_PROFIT: {ec.get('POSSIBLE_PROFIT')}",
            f"UNPROFITABLE: {ec.get('UNPROFITABLE')}",
            f"EXECUTION_BLOCKED: {ec.get('EXECUTION_BLOCKED')}",
            "",
            "PROFIT BUCKETS",
            "",
            f">$0: {pb.get('gt_0')}",
            f">=$1K: {pb.get('ge_1k')}",
            f">=$2.5K: {pb.get('ge_2_5k')}",
            f">=$5K: {pb.get('ge_5k')}",
            f">=$7.5K: {pb.get('ge_7_5k')}",
            f">=$10K: {pb.get('ge_10k')}",
            f">=$15K: {pb.get('ge_15k')}",
            f">=$25K: {pb.get('ge_25k')}",
            f">=$50K: {pb.get('ge_50k')}",
            f">=$75K: {pb.get('ge_75k')}",
            "",
            "QUOTE-NEEDED HIDDEN RESERVE",
            "",
            f"OEM_QUOTE_REQUIRED: {qh.get('OEM_QUOTE_REQUIRED')}",
            f"DISTRIBUTOR_QUOTE_REQUIRED: {qh.get('DISTRIBUTOR_QUOTE_REQUIRED')}",
            f"SUPPLIER_QUOTE_REQUIRED: {qh.get('SUPPLIER_QUOTE_REQUIRED')}",
            f"Premature admissions: {qh.get('premature_admissions')}",
            "",
            "CONSERVATION",
            "",
            f"Identity input: {co.get('identity_input')}",
            f"Identity terminal: {co.get('identity_terminal')}",
            f"Difference: {co.get('identity_difference')}",
            "",
            f"Opportunity input: {co.get('opportunity_input')}",
            f"Opportunity terminal: {co.get('opportunity_terminal')}",
            f"Difference: {co.get('opportunity_difference')}",
            "",
            "MOST IMPORTANT ANSWERS",
            "",
            f"1. Acquisition-cost opportunities: {ans.get('1_acq_cost_opps')}",
            f"2. Route-blocked recovered: {ans.get('2_route_blocked_recovered')}",
            f"3. Specialty executable paths: {ans.get('3_specialty_executable_path')}",
            f"4. OPEN_RESELLER with cost: {ans.get('4_open_reseller_with_cost')}",
            f"5. Both sides: {ans.get('5_both_sides')}",
            f"6. Economics-ready: {ans.get('6_economics_ready')}",
            f"7. Profitable: {ans.get('7_profitable')}",
            f"8. >=$5K: {ans.get('8_ge_5k')}",
            f"9. >=$10K: {ans.get('9_ge_10k')}",
            f"10. Acq still bottleneck: {ans.get('10_acq_still_bottleneck')}",
            f"11. Top miss cause: {ans.get('11_top_miss_cause')}",
            f"12. Scale beyond 100: {ans.get('12_scale_beyond_100')}",
        ]
    )
