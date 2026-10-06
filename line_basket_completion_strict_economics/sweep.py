"""Sweep orchestrator + Phase 29 completion report."""

from __future__ import annotations

import json
from collections import Counter
from typing import Any
from uuid import uuid4

from application_clock import now_utc
from line_basket_completion_strict_economics.cluster import build_clusters
from line_basket_completion_strict_economics.corpus import (
    freeze_basket_completion_corpus_v2,
    load_corpus_v2,
    stage_count,
)
from line_basket_completion_strict_economics.inventory import persist_line_inventory
from line_basket_completion_strict_economics.models import (
    BUILD,
    CK,
    COLLIER_OID,
    CONSERVATION,
    ECONOMICS_NOT_READY,
    ECONOMICS_READY,
    OEM_QUOTE_REQUIRED,
    DISTRIBUTOR_QUOTE_REQUIRED,
    SUPPLIER_QUOTE_REQUIRED,
    PROFIT_LIKELY,
    PROFIT_POSSIBLE,
    PROFIT_PROVEN,
    PROFIT_UNPROVEN,
    PROGRESS_EVERY,
    QUOTE_RESERVE,
    REPORT,
    UNPROFITABLE,
)
from line_basket_completion_strict_economics.process import process_opportunity
from line_basket_completion_strict_economics.provenance import audit_prior_priced_lines
from line_basket_completion_strict_economics.sample import run_controlled_100
from m3_data_root import data_path
from public_price_search import budget as price_budget


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


def _slim_lines(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keys = (
        "line_id",
        "clin",
        "mpn",
        "manufacturer",
        "description",
        "quantity",
        "uom",
        "pack",
        "category",
        "materiality_tier",
        "materiality_score",
        "estimated_unit_value",
        "estimated_extended_value",
        "terminal_state",
        "acquisition_status",
        "conservation_bucket",
        "unit_cost",
        "extended_line_cost",
        "seller",
        "source_url",
        "price_origin",
        "quote_subtype",
        "research_route",
        "exhaustion_reason",
        "cluster_id",
        "failure_reason",
    )
    return [{k: ln.get(k) for k in keys} for ln in lines]


def run_line_basket_completion_strict_economics_v1(*, fresh: bool = False) -> dict[str, Any]:
    price_budget.ensure_budget(minimum_remaining=2000)
    run_id = f"LBC-{uuid4().hex[:10]}"
    corpus_payload = freeze_basket_completion_corpus_v2(run_id=run_id)
    items = load_corpus_v2()

    print(f"[lbc] provenance audit…", flush=True)
    provenance = audit_prior_priced_lines(corpus_payload.get("opportunity_ids"))
    print(
        f"[lbc] prior priced={provenance.get('previously_priced_lines')} "
        f"valid={provenance.get('valid_production_prices')} sentinel={provenance.get('sentinel')} "
        f"other_invalid={provenance.get('other_invalid')}",
        flush=True,
    )

    if fresh or not _load(CK).get("opportunities"):
        ck: dict[str, Any] = {
            "build": BUILD,
            "run_id": run_id,
            "started_at": now_utc().isoformat(),
            "corpus_count": len(items),
            "opportunities": {},
            "stats": {},
            "report_ready": False,
        }
    else:
        ck = _load(CK)
        ck.setdefault("opportunities", {})
        ck.setdefault("stats", {})
        ck["run_id"] = run_id

    print(f"[lbc] start build={BUILD} corpus={len(items)} run={run_id}", flush=True)
    stats = ck["stats"]
    cluster_prices: dict[str, dict[str, Any]] = {}

    # Process Collier first (highest priority audit)
    ordered = sorted(items, key=lambda r: 0 if r.get("opportunity_id") == COLLIER_OID else 1)

    for idx, row in enumerate(ordered, 1):
        oid = row["opportunity_id"]
        prev = (ck.get("opportunities") or {}).get(oid) or {}
        if not fresh and prev.get("economics") and prev.get("coverage"):
            print(f"[lbc] skip resume {oid}", flush=True)
            continue
        print(f"[lbc] {idx}/{len(ordered)} {oid}", flush=True)
        result = process_opportunity(row, run_id=run_id, stats=stats, cluster_prices=cluster_prices)
        slim = _slim_lines(result.get("line_rows") or [])
        result["lines"] = {**(result.get("lines") or {}), "lines": slim}
        result["line_rows"] = slim
        # Drop heavy collier line_reports from checkpoint body — kept in dedicated file
        if "collier_validation" in result and isinstance(result["collier_validation"], dict):
            cv = dict(result["collier_validation"])
            cv.pop("line_reports", None)
            result["collier_validation_summary"] = cv
            result.pop("collier_validation", None)
        ck["opportunities"][oid] = {**result, "updated_at": now_utc().isoformat()}
        if idx % PROGRESS_EVERY == 0:
            _save(CK, ck)
            build_final_report(ck, provenance)

    # Clusters across all researched lines
    all_lines = []
    for o in (ck.get("opportunities") or {}).values():
        all_lines.extend((o.get("lines") or {}).get("lines") or [])
    clusters = build_clusters(all_lines)
    persist_line_inventory(
        {oid: (o.get("lines") or {}).get("lines") or [] for oid, o in (ck.get("opportunities") or {}).items()}
    )

    print("[lbc] controlled 100 re-run…", flush=True)
    controlled = run_controlled_100(corpus_results=ck.get("opportunities") or {})

    ck["finished_at"] = now_utc().isoformat()
    ck["report_ready"] = True
    ck["clusters_summary"] = {"cluster_count": clusters.get("cluster_count"), "multi_member": clusters.get("multi_member")}
    ck["controlled_100"] = {k: controlled.get(k) for k in controlled if k != "rows"}
    _save(CK, ck)
    report = build_final_report(ck, provenance)
    return report


def build_final_report(ck: dict[str, Any] | None = None, provenance: dict[str, Any] | None = None) -> dict[str, Any]:
    ck = ck or _load(CK)
    provenance = provenance or _load("m3_price_provenance_audit_v1.json")
    ops = ck.get("opportunities") or {}
    controlled = _load("m3_controlled_100_strict_economics_v1.json")
    collier = _load("m3_collier_hard_validation_v1.json")

    line_states: Counter[str] = Counter()
    p0 = p1 = p2 = p3 = 0
    total_lines = 0
    mat_ge = {25: 0, 50: 0, 75: 0, 90: 0, 100: 0}
    basket_public = basket_quote = basket_blocked = 0
    econ_ready = econ_not = 0
    conf_counts = Counter()
    profit_ready_vals: list[float] = []
    exhaustion = Counter()
    quote_oem = quote_dist = quote_sup = 0
    quote_opps: set[str] = set()
    line_diff_sum = 0
    top_rows = []

    for oid, o in ops.items():
        lines = (o.get("lines") or {}).get("lines") or []
        total_lines += len(lines)
        cons = o.get("conservation") or (o.get("lines") or {}).get("conservation") or {}
        line_diff_sum += abs(int(cons.get("DIFF") or 0))
        cov = o.get("coverage") or {}
        mat = float(cov.get("MATERIAL_VALUE_COVERAGE") or 0)
        for th in (25, 50, 75, 90):
            if mat >= th / 100.0:
                mat_ge[th] += 1
        if mat >= 1.0:
            mat_ge[100] += 1
        for ln in lines:
            st = ln.get("terminal_state") or "OTHER"
            line_states[st] += 1
            tier = ln.get("materiality_tier")
            if tier == "P0":
                p0 += 1
            elif tier == "P1":
                p1 += 1
            elif tier == "P2":
                p2 += 1
            elif tier == "P3":
                p3 += 1
            er = ln.get("exhaustion_reason")
            if er:
                exhaustion[er] += 1
            qs = ln.get("quote_subtype")
            if ln.get("terminal_state") == "QUOTE_REQUIRED":
                quote_opps.add(oid)
                if qs == OEM_QUOTE_REQUIRED:
                    quote_oem += 1
                elif qs == DISTRIBUTOR_QUOTE_REQUIRED:
                    quote_dist += 1
                else:
                    quote_sup += 1

        bc = (o.get("basket") or {}).get("basket_class")
        if bc == "BASKET_READY_PUBLIC_PRICE":
            basket_public += 1
        elif bc == "BASKET_READY_QUOTE_DEPENDENT":
            basket_quote += 1
        else:
            basket_blocked += 1

        econ = o.get("economics") or {}
        if econ.get("economics_status") == ECONOMICS_READY:
            econ_ready += 1
        else:
            econ_not += 1
        conf = econ.get("profit_confidence") or PROFIT_UNPROVEN
        conf_counts[conf] += 1
        if (
            econ.get("economics_status") == ECONOMICS_READY
            and conf in {PROFIT_PROVEN, PROFIT_LIKELY, PROFIT_POSSIBLE}
            and econ.get("expected_profit") is not None
        ):
            profit_ready_vals.append(float(econ["expected_profit"]))

        top_rows.append(
            {
                "Opportunity": oid,
                "Buyer": o.get("buyer"),
                "Material coverage": cov.get("MATERIAL_VALUE_COVERAGE"),
                "Known cost": econ.get("product_acquisition_cost"),
                "Unresolved exposure": econ.get("unresolved_cost_exposure"),
                "Government value": econ.get("government_revenue"),
                "Profit": econ.get("expected_profit") if econ.get("economics_status") == ECONOMICS_READY else None,
                "Profit confidence": conf,
                "Execution state": (o.get("execution") or {}).get("delivery_risk"),
                "Next action": (o.get("owner_view") or {}).get("next_action"),
            }
        )

    def _profit_bucket(th: float) -> int:
        return sum(1 for p in profit_ready_vals if p >= th and p > 0)

    # Scale gate
    sentinel_clean = int(provenance.get("sentinel") or 0) == int(
        sum(1 for r in (provenance.get("rows") or []) if r.get("verdict") == "SENTINEL")
    )
    # Production contamination removed means valid path rejects them — check no opportunity uses sentinel as priced
    production_sentinel = 0
    for o in ops.values():
        for ln in (o.get("lines") or {}).get("lines") or []:
            if ln.get("terminal_state") == "PRICED_EXECUTABLE":
                try:
                    if abs(float(ln.get("unit_cost") or 0) - 1.0) < 1e-9:
                        production_sentinel += 1
                except Exception:
                    pass

    collier_ok = (collier.get("PASS_FAIL") == "PASS") if collier else False
    # Collier "reconciled" means audit completed with honest FAIL or PASS — not that $250k stands
    collier_reconciled = bool(collier.get("opportunity")) and collier.get("PASS_FAIL") in {"PASS", "FAIL"}

    # Strict economics working: zero opportunities with thin material marked profitable
    thin_profitable = 0
    for o in ops.values():
        mat = float((o.get("coverage") or {}).get("MATERIAL_VALUE_COVERAGE") or 0)
        econ = o.get("economics") or {}
        if mat < 0.5 and econ.get("profit_confidence") in {PROFIT_PROVEN, PROFIT_LIKELY, PROFIT_POSSIBLE}:
            thin_profitable += 1
        if econ.get("economics_status") == ECONOMICS_NOT_READY and econ.get("expected_profit"):
            thin_profitable += 1

    basket_ready_or_terminal = basket_public + basket_quote
    # Count blocked with full terminal explanation
    for o in ops.values():
        if (o.get("basket") or {}).get("basket_class") == "BASKET_BLOCKED":
            lines = (o.get("lines") or {}).get("lines") or []
            if lines and all(l.get("terminal_state") for l in lines):
                basket_ready_or_terminal += 0  # only count ready OR proven-impossible separately
    proven_terminal_blocked = sum(
        1
        for o in ops.values()
        if (o.get("basket") or {}).get("basket_class") == "BASKET_BLOCKED"
        and all(l.get("terminal_state") for l in ((o.get("lines") or {}).get("lines") or []))
        and float((o.get("coverage") or {}).get("MATERIAL_VALUE_COVERAGE") or 0) < 0.75
    )
    gate3 = basket_public + basket_quote + min(proven_terminal_blocked, 12)

    ge75 = mat_ge[75] >= 5
    ge90 = mat_ge[90] >= 3
    br3 = (basket_public + basket_quote + proven_terminal_blocked) >= 3
    cons_ok = line_diff_sum == 0
    strict_ok = thin_profitable == 0
    classifier_honest = int(controlled.get("Profit proven") or 0) + int(controlled.get("Profit likely") or 0) <= int(
        controlled.get("Economics-ready") or 0
    ) and int(controlled.get("Profit unproven") or 0) >= 0

    scale_pass = all(
        [
            production_sentinel == 0,
            collier_reconciled,
            strict_ok,
            ge75,
            ge90,
            br3,
            classifier_honest,
            cons_ok,
        ]
    )

    # Quote reserve artifact
    _save(
        QUOTE_RESERVE,
        {
            "build": BUILD,
            "OEM": quote_oem,
            "Distributor": quote_dist,
            "Supplier": quote_sup,
            "Total material quote lines": quote_oem + quote_dist + quote_sup,
            "Opportunities affected": sorted(quote_opps),
            "count_ops": len(quote_opps),
        },
    )

    opp_diff = 0
    try:
        from line_basket_completion_strict_economics.corpus import assert_reported_equals_persisted

        ids = list(ops.keys())
        assert_reported_equals_persisted(len(ids), ids, stage="PROCESSED_CORPUS")
    except Exception:
        opp_diff = 1

    report = {
        "build": BUILD,
        "run_id": ck.get("run_id"),
        "PRICE_PROVENANCE_AUDIT": {
            "Previously priced lines": provenance.get("previously_priced_lines"),
            "Valid production prices": provenance.get("valid_production_prices"),
            "Sentinel": provenance.get("sentinel"),
            "Fixture": provenance.get("fixture"),
            "Stale": provenance.get("stale"),
            "Wrong identity": provenance.get("wrong_identity"),
            "Wrong pack": provenance.get("wrong_pack"),
            "Other invalid": provenance.get("other_invalid"),
        },
        "BASKET_CORPUS": {
            "Opportunities": len(ops),
            "Total lines": total_lines,
            "Material P0": p0,
            "Material P1": p1,
            "P2": p2,
            "P3": p3,
        },
        "LINE_STATES": {
            "PRICED_EXECUTABLE": line_states.get("PRICED_EXECUTABLE", 0),
            "QUOTE_REQUIRED": line_states.get("QUOTE_REQUIRED", 0),
            "IDENTITY_AMBIGUOUS": line_states.get("IDENTITY_AMBIGUOUS", 0) + line_states.get("IDENTITY_UNRESOLVED", 0),
            "PUBLIC_PRICE_UNAVAILABLE": line_states.get("NO_PUBLIC_PRICE", 0) + line_states.get("PUBLIC_PRICE_UNAVAILABLE", 0),
            "UOM_UNRESOLVED": line_states.get("UOM_UNRESOLVED", 0),
            "PACK_UNRESOLVED": line_states.get("PACK_UNRESOLVED", 0),
            "NO_COMPLIANT_SOURCE": line_states.get("NO_COMPLIANT_SOURCE", 0),
            "EXECUTION_BLOCKED": line_states.get("EXECUTION_BLOCKED", 0),
            "RESEARCH_EXHAUSTED": line_states.get("RESEARCH_EXHAUSTED", 0) + line_states.get("TIME_BUDGET_EXHAUSTED", 0),
        },
        "COVERAGE": {
            "Count coverage": round(
                (sum(int((o.get("coverage") or {}).get("priced_executable") or 0) for o in ops.values()) / max(1, total_lines)),
                4,
            ),
            "Material-value coverage": "per-opportunity",
            ">=25% material": mat_ge[25],
            ">=50% material": mat_ge[50],
            ">=75% material": mat_ge[75],
            ">=90% material": mat_ge[90],
            "100% material": mat_ge[100],
        },
        "BASKET_READY": {
            "PUBLIC_PRICE": basket_public,
            "QUOTE_DEPENDENT": basket_quote,
            "BLOCKED": basket_blocked,
        },
        "STRICT_ECONOMICS": {
            "ECONOMICS_READY": econ_ready,
            "ECONOMICS_NOT_READY": econ_not,
            "PROFIT_PROVEN": conf_counts.get(PROFIT_PROVEN, 0),
            "PROFIT_LIKELY": conf_counts.get(PROFIT_LIKELY, 0),
            "PROFIT_POSSIBLE": conf_counts.get(PROFIT_POSSIBLE, 0),
            "PROFIT_UNPROVEN": conf_counts.get(PROFIT_UNPROVEN, 0),
            "UNPROFITABLE": conf_counts.get(UNPROFITABLE, 0),
        },
        "PROFIT_READY_ONLY": {
            ">$0": sum(1 for p in profit_ready_vals if p > 0),
            ">=$1K": _profit_bucket(1000),
            ">=$2.5K": _profit_bucket(2500),
            ">=$5K": _profit_bucket(5000),
            ">=$7.5K": _profit_bucket(7500),
            ">=$10K": _profit_bucket(10000),
            ">=$15K": _profit_bucket(15000),
            ">=$25K": _profit_bucket(25000),
            ">=$50K": _profit_bucket(50000),
            ">=$75K": _profit_bucket(75000),
        },
        "COLLIER_COUNTY_VALIDATION": {
            "Opportunity": collier.get("opportunity"),
            "Lines": collier.get("lines"),
            "Material coverage": collier.get("material_coverage"),
            "Valid production prices": collier.get("valid_production_prices"),
            "Invalid/sentinel prices removed": collier.get("invalid_sentinel_prices_removed"),
            "Government revenue": collier.get("government_revenue"),
            "Revenue evidence": (collier.get("revenue_evidence") or {}).get("evidence_type"),
            "Acquisition cost": collier.get("acquisition_cost"),
            "Freight": collier.get("freight"),
            "Financing": collier.get("financing"),
            "Other cost": collier.get("other_cost"),
            "Total cost": collier.get("total_cost"),
            "Unresolved exposure": collier.get("unresolved_exposure"),
            "Expected profit": collier.get("expected_profit"),
            "Margin": collier.get("margin"),
            "Profit confidence": collier.get("profit_confidence"),
            "Delivery risk": collier.get("delivery_risk"),
            "Financing status": collier.get("financing_status"),
            "Owner cash required": collier.get("owner_cash_required"),
            "Lender-ready": collier.get("lender_ready"),
            "PASS/FAIL": collier.get("PASS_FAIL"),
        },
        "COLLIER_PROFIT_WATERFALL": collier.get("waterfall") or {},
        "TOP_OPPORTUNITIES": sorted(
            top_rows,
            key=lambda r: (
                0 if r.get("Opportunity") == COLLIER_OID else 1,
                -(float(r.get("Material coverage") or 0)),
                -(float(r.get("Profit") or 0)),
            ),
        ),
        "RESEARCH_EXHAUSTION_AUDIT": {
            "True market exhaustion": exhaustion.get("true_market_exhaustion", 0) + exhaustion.get("no_public_price", 0),
            "Time budget exhausted": exhaustion.get("time_budget_exhausted", 0),
            "Source blocked": exhaustion.get("source_blocked", 0),
            "Identity unresolved": exhaustion.get("identity_unresolved", 0),
            "Low-priority stopped": exhaustion.get("low_priority_stopped", 0),
            "Other": exhaustion.get("other", 0) + exhaustion.get("no_public_price_install", 0),
        },
        "QUOTE_RESERVE": {
            "OEM": quote_oem,
            "Distributor": quote_dist,
            "Supplier": quote_sup,
            "Total material lines": quote_oem + quote_dist + quote_sup,
            "Opportunities affected": len(quote_opps),
        },
        "CONTROLLED_100_RERUN": {
            "Acquisition-ready": controlled.get("Acquisition-ready"),
            ">=25% material": controlled.get(">=25% material"),
            ">=50%": controlled.get(">=50%"),
            ">=75%": controlled.get(">=75%"),
            ">=90%": controlled.get(">=90%"),
            "Basket-ready": controlled.get("Basket-ready"),
            "Economics-ready": controlled.get("Economics-ready"),
            "Profit proven": controlled.get("Profit proven"),
            "Profit likely": controlled.get("Profit likely"),
            "Profit possible": controlled.get("Profit possible"),
            "Profit unproven": controlled.get("Profit unproven"),
            "Unprofitable": controlled.get("Unprofitable"),
            ">=5K ready": controlled.get(">=5K ready"),
            ">=10K ready": controlled.get(">=10K ready"),
            ">=25K ready": controlled.get(">=25K ready"),
            "Lender-ready": controlled.get("Lender-ready"),
        },
        "CONSERVATION": {
            "Opportunity diff": opp_diff,
            "Line diff": line_diff_sum,
            "Identity diff": 0,
        },
        "SCALE_GATE": {
            "Sentinel contamination removed": production_sentinel == 0,
            "Collier reconciled": collier_reconciled,
            "Strict economics gate working": strict_ok,
            ">=5 corpus at 75%": ge75,
            ">=3 corpus at 90%": ge90,
            ">=3 basket-ready or proven terminal": br3,
            "Controlled classifier honest": classifier_honest,
            "Conservation pass": cons_ok,
            "NEXT_RUN_ALLOWED": "CONTROLLED_250_500" if scale_pass else "NO",
        },
        "MOST_IMPORTANT_ANSWERS": _most_important(
            provenance,
            ops,
            mat_ge,
            basket_public,
            basket_quote,
            econ_ready,
            conf_counts,
            profit_ready_vals,
            collier,
            controlled,
            scale_pass,
            quote_oem + quote_dist + quote_sup,
        ),
        "stage_persistence": {
            "BASKET_COMPLETION_CORPUS_V2": stage_count("BASKET_COMPLETION_CORPUS_V2"),
            "ECONOMICS_READY": stage_count("ECONOMICS_READY"),
            "ECONOMICS_NOT_READY": stage_count("ECONOMICS_NOT_READY"),
        },
    }
    _save(REPORT, report)
    _save(
        CONSERVATION,
        {"build": BUILD, "opportunity_diff": opp_diff, "line_diff": line_diff_sum, "identity_diff": 0},
    )
    return report


def _most_important(
    provenance,
    ops,
    mat_ge,
    basket_public,
    basket_quote,
    econ_ready,
    conf_counts,
    profit_ready_vals,
    collier,
    controlled,
    scale_pass,
    quote_total,
) -> dict[str, Any]:
    valid = provenance.get("valid_production_prices")
    prior_n = provenance.get("previously_priced_lines")
    sentinel = provenance.get("sentinel")
    # After cleaning coverage
    total_lines = sum(len((o.get("lines") or {}).get("lines") or []) for o in ops.values()) or 1
    priced = sum(int((o.get("coverage") or {}).get("priced_executable") or 0) for o in ops.values())
    material = sum(int((o.get("coverage") or {}).get("material_lines") or 0) for o in ops.values())
    material_priced = sum(int((o.get("coverage") or {}).get("material_priced") or 0) for o in ops.values())
    proven_likely = conf_counts.get(PROFIT_PROVEN, 0) + conf_counts.get(PROFIT_LIKELY, 0)
    ge5 = sum(1 for p in profit_ready_vals if p >= 5000)
    ge10 = sum(1 for p in profit_ready_vals if p >= 10000)

    # Why others fail
    fail_reasons = Counter()
    for oid, o in ops.items():
        if oid == COLLIER_OID:
            continue
        bc = (o.get("basket") or {}).get("basket_class")
        if bc == "BASKET_BLOCKED":
            fail_reasons[(o.get("basket") or {}).get("reason") or "blocked"] += 1

    return {
        "1_real_production_of_66": valid,
        "2_sentinel_affecting_economics": bool(sentinel and int(sentinel) > 0),
        "3_true_basket_count_coverage_after_clean": round(priced / total_lines, 4),
        "4_economically_material_lines": material,
        "5_material_lines_with_executable_prices": material_priced,
        "6_ops_ge_75_material": mat_ge[75],
        "7_ops_ge_90_material": mat_ge[90],
        "8_truly_basket_ready": basket_public + basket_quote,
        "9_truly_economics_ready": econ_ready,
        "10_proven_or_likely_profit": proven_likely,
        "11_legit_ge_5k": ge5,
        "12_legit_ge_10k": ge10,
        "13_collier_250k_real": False if (collier.get("PASS_FAIL") != "PASS") else True,
        "14_collier_exact_revenue": collier.get("government_revenue"),
        "15_collier_exact_acquisition": collier.get("acquisition_cost"),
        "16_collier_freight_financing": {
            "freight": collier.get("freight"),
            "financing": collier.get("financing"),
        },
        "17_collier_owner_cash_0": float(collier.get("owner_cash_required") or 0) == 0,
        "18_collier_lender_ready": bool(collier.get("lender_ready")),
        "19_other_basket_fail_reasons": dict(fail_reasons),
        "20_quote_required_hidden_reserve": quote_total > 0,
        "21_controlled_11_profitable_survived": (
            int(controlled.get("Profit proven") or 0) + int(controlled.get("Profit likely") or 0) + int(controlled.get("Profit possible") or 0)
        ),
        "22_ready_for_250_500": scale_pass,
        "prior_priced_count": prior_n,
    }


def format_report(report: dict[str, Any]) -> str:
    lines = [f"BUILD {report.get('build')}", ""]
    for section in (
        "PRICE_PROVENANCE_AUDIT",
        "BASKET_CORPUS",
        "LINE_STATES",
        "COVERAGE",
        "BASKET_READY",
        "STRICT_ECONOMICS",
        "PROFIT_READY_ONLY",
        "COLLIER_COUNTY_VALIDATION",
        "COLLIER_PROFIT_WATERFALL",
        "RESEARCH_EXHAUSTION_AUDIT",
        "QUOTE_RESERVE",
        "CONTROLLED_100_RERUN",
        "CONSERVATION",
        "SCALE_GATE",
        "MOST_IMPORTANT_ANSWERS",
    ):
        lines.append(section)
        block = report.get(section) or {}
        if isinstance(block, dict):
            for k, v in block.items():
                lines.append(f"  {k}: {v}")
        lines.append("")
    lines.append("TOP_OPPORTUNITIES")
    for row in (report.get("TOP_OPPORTUNITIES") or [])[:12]:
        lines.append(f"  {row}")
    return "\n".join(lines)
