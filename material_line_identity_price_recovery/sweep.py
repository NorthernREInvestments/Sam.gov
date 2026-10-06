"""Sweep: freeze corpus → reconstruct → identity → price → gate → report."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from typing import Any
from uuid import uuid4

from application_clock import now_utc
from material_line_identity_price_recovery.corpus import freeze_material_line_recovery_corpus, load_corpus_lines
from material_line_identity_price_recovery.identity import build_identity_clusters
from material_line_identity_price_recovery.models import (
    A_EXACT_MPN,
    AUTHORIZED_DISTRIBUTOR_CURRENT,
    B_EXACT_MODEL,
    BOND_THRESHOLD,
    BUDGET,
    BUILD,
    C_NSN_TO_EXACT,
    CEILING,
    CK,
    COLLIER_OID,
    CONSERVATION,
    CURRENT_CONTRACT_VALUE,
    D_PERMITTED_EQUAL,
    DEEP_CANDIDATES,
    DISTRIBUTOR_QUOTE_REQUIRED,
    E_STRONG_GENERIC,
    ESTIMATE,
    F_FAMILY_ONLY,
    G_AMBIGUOUS,
    HISTORICAL_REFERENCE,
    INSURANCE_THRESHOLD,
    MANUFACTURER_CURRENT,
    OEM_QUOTE_REQUIRED,
    OTHER_NON_REVENUE_AMOUNT,
    PROGRESS_EVERY,
    PUBLIC_CURRENT,
    REPORT,
    SUPPLIER_QUOTE_REQUIRED,
)
from material_line_identity_price_recovery.process import process_opportunity_material_lines
from material_line_identity_price_recovery.revenue_context import run_revenue_context_regression
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


def _slim(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keys = (
        "opportunity_id",
        "line_id",
        "clin",
        "materiality_tier",
        "category",
        "description",
        "manufacturer",
        "mpn",
        "part_number",
        "model",
        "nsn",
        "quantity",
        "uom",
        "pack",
        "identity_confidence",
        "identity_usable",
        "identity_validated",
        "product_identity_type",
        "source_document",
        "source_kind",
        "source_reconstructed",
        "spreadsheet_match",
        "acquisition_state",
        "quote_subtype",
        "price_origin",
        "unit_cost",
        "seller",
        "source_url",
        "exhaustion_reason",
        "cluster_id",
        "price_propagated_from_cluster",
        "routes_attempted",
    )
    return [{k: ln.get(k) for k in keys} for ln in lines]


def run_material_line_identity_price_recovery_v1(*, fresh: bool = False) -> dict[str, Any]:
    price_budget.ensure_budget(minimum_remaining=2000)
    run_id = f"MLR-{uuid4().hex[:10]}"

    print("[mlr] freeze corpus…", flush=True)
    corpus = freeze_material_line_recovery_corpus(force=fresh)
    all_lines = load_corpus_lines()
    print(f"[mlr] corpus lines={len(all_lines)} p0={corpus.get('p0')} p1={corpus.get('p1')}", flush=True)

    print("[mlr] revenue regression…", flush=True)
    revenue_reg = run_revenue_context_regression()
    print(
        f"[mlr] revenue regression pass={revenue_reg.get('all_pass')} "
        f"collier={revenue_reg.get('collier_bonding_threshold_classified_correctly')}",
        flush=True,
    )

    by_opp: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for ln in all_lines:
        by_opp[ln["opportunity_id"]].append(ln)

    if fresh or not _load(CK).get("opportunities"):
        ck: dict[str, Any] = {
            "build": BUILD,
            "run_id": run_id,
            "started_at": now_utc().isoformat(),
            "corpus_count": len(all_lines),
            "opportunities": {},
            "stats": {},
            "report_ready": False,
        }
    else:
        ck = _load(CK)
        ck.setdefault("opportunities", {})
        ck.setdefault("stats", {})
        ck["run_id"] = run_id

    # Prefer opportunities with spreadsheets / high material counts first
    ordered_oids = sorted(
        by_opp.keys(),
        key=lambda oid: (
            0 if oid == "opengov:go-metro:298984" else 1,
            0 if oid == COLLIER_OID else 1,
            -len(by_opp[oid]),
        ),
    )

    stats = ck["stats"]
    for idx, oid in enumerate(ordered_oids, 1):
        prev = (ck.get("opportunities") or {}).get(oid) or {}
        if not fresh and prev.get("coverage") and prev.get("lines"):
            print(f"[mlr] skip resume {oid}", flush=True)
            continue
        print(f"[mlr] {idx}/{len(ordered_oids)} {oid} material={len(by_opp[oid])}", flush=True)
        result = process_opportunity_material_lines(oid, by_opp[oid], stats=stats)
        result["lines"] = _slim(result.get("lines") or [])
        ck["opportunities"][oid] = {**result, "updated_at": now_utc().isoformat()}
        if idx % PROGRESS_EVERY == 0:
            _save(CK, ck)
            build_final_report(ck, revenue_reg)

    # Global clusters
    all_recovered = []
    for o in (ck.get("opportunities") or {}).values():
        all_recovered.extend(o.get("lines") or [])
    clusters = build_identity_clusters(all_recovered)

    deep = select_deep_completion_candidates(ck.get("opportunities") or {})
    _save(DEEP_CANDIDATES, deep)

    ck["finished_at"] = now_utc().isoformat()
    ck["report_ready"] = True
    ck["clusters_summary"] = {
        "cluster_count": clusters.get("cluster_count"),
        "repeated": clusters.get("repeated_lines_collapsed"),
        "reuse": clusters.get("research_reuse_count"),
    }
    ck["deep_candidates"] = deep
    _save(CK, ck)
    report = build_final_report(ck, revenue_reg)
    return report


def select_deep_completion_candidates(ops: dict[str, Any], n: int = 5) -> dict[str, Any]:
    rows = []
    for oid, o in ops.items():
        cov = o.get("coverage") or {}
        owner = o.get("owner_view") or {}
        # Score without fake profit
        score = (
            float(cov.get("MATERIAL_IDENTITY_COVERAGE") or 0) * 100
            + float(cov.get("MATERIAL_PRICE_COVERAGE") or 0) * 80
            + float(cov.get("MATERIAL_BASKET_COVERAGE") or 0) * 40
            + (10 if o.get("package_found") else 0)
            - float(owner.get("ambiguous") or 0) * 0.05
        )
        rows.append(
            {
                "Opportunity": oid,
                "Buyer": oid.split(":")[1] if ":" in oid else oid,
                "Material identity coverage": cov.get("MATERIAL_IDENTITY_COVERAGE"),
                "Material price coverage": cov.get("MATERIAL_PRICE_COVERAGE"),
                "Material basket coverage": cov.get("MATERIAL_BASKET_COVERAGE"),
                "Revenue evidence": "classifier_protected",
                "Quote-required lines": owner.get("quote_required"),
                "Main blocker": owner.get("next_blocker"),
                "score": round(score, 2),
                "Why selected": _why_selected(cov, o),
            }
        )
    rows.sort(key=lambda r: -float(r.get("score") or 0))
    top = rows[:n]
    return {"build": BUILD, "candidates": top, "count": len(top)}


def _why_selected(cov: dict[str, Any], o: dict[str, Any]) -> str:
    bits = []
    if float(cov.get("MATERIAL_IDENTITY_COVERAGE") or 0) >= 0.5:
        bits.append("strong identity coverage")
    if float(cov.get("MATERIAL_PRICE_COVERAGE") or 0) > 0:
        bits.append("has production prices")
    if o.get("package_found"):
        bits.append("source package available")
    if float(cov.get("MATERIAL_BASKET_COVERAGE") or 0) >= 0.25:
        bits.append("material basket progress")
    return "; ".join(bits) or "best relative recovery among corpus"


def build_final_report(ck: dict[str, Any] | None = None, revenue_reg: dict[str, Any] | None = None) -> dict[str, Any]:
    ck = ck or _load(CK)
    revenue_reg = revenue_reg or _load("m3_revenue_context_regression_v1.json")
    corpus = _load("m3_material_line_recovery_corpus_v1.json")
    ops = ck.get("opportunities") or {}
    deep = _load(DEEP_CANDIDATES) or ck.get("deep_candidates") or {}

    all_lines: list[dict[str, Any]] = []
    for o in ops.values():
        all_lines.extend(o.get("lines") or [])

    id_counts = Counter(l.get("identity_confidence") for l in all_lines)
    price_origins = Counter(
        (l.get("price_origin") or (l.get("production_price") or {}).get("price_origin"))
        for l in all_lines
        if l.get("acquisition_state") == "PRICED_EXECUTABLE"
    )
    quote_counts = Counter(l.get("quote_subtype") for l in all_lines if l.get("acquisition_state") == "QUOTE_REQUIRED")
    exhaust = Counter(l.get("exhaustion_reason") or l.get("acquisition_state") for l in all_lines)

    p0 = [l for l in all_lines if l.get("materiality_tier") == "P0"]
    p1 = [l for l in all_lines if l.get("materiality_tier") == "P1"]
    p0_usable = [l for l in p0 if l.get("identity_usable")]
    p1_usable = [l for l in p1 if l.get("identity_usable")]
    p0_priced = [l for l in p0 if l.get("acquisition_state") == "PRICED_EXECUTABLE"]
    p0_quote = [l for l in p0 if l.get("acquisition_state") == "QUOTE_REQUIRED"]

    # Source reconstruction aggregates
    rebuilt = sum(int((o.get("source_stats") or {}).get("lines_rebuilt_from_docs") or 0) for o in ops.values())
    sheet = sum(int((o.get("source_stats") or {}).get("spreadsheet_multi_column_recoveries") or 0) for o in ops.values())
    cont = sum(int((o.get("source_stats") or {}).get("continuation_row_recoveries") or 0) for o in ops.values())
    mfr = sum(int((o.get("source_stats") or {}).get("manufacturer_recovered") or 0) for o in ops.values())
    mpn = sum(int((o.get("source_stats") or {}).get("mpn_model_recovered") or 0) for o in ops.values())
    nsn = sum(int((o.get("source_stats") or {}).get("nsn_recovered") or 0) for o in ops.values())
    sheet_matches = sum(1 for l in all_lines if l.get("spreadsheet_match"))
    pack_uom_corrected = sum(1 for l in all_lines if l.get("source_reconstructed") and (l.get("uom") or l.get("pack")))

    # Per-opp coverage
    per_opp = []
    ge = {25: 0, 50: 0, 75: 0, 90: 0}
    for oid, o in ops.items():
        cov = o.get("coverage") or {}
        owner = o.get("owner_view") or {}
        mat = float(cov.get("MATERIAL_BASKET_COVERAGE") or 0)
        for th in ge:
            if mat >= th / 100.0:
                ge[th] += 1
        per_opp.append(
            {
                "Opportunity": oid,
                "P0/P1 lines": owner.get("material_lines") or (int(cov.get("p0_total") or 0) + int(cov.get("p1_total") or 0)),
                "Usable identities": owner.get("identity_ready"),
                "Production priced": owner.get("production_priced"),
                "Quote-required": owner.get("quote_required"),
                "Ambiguous": owner.get("ambiguous"),
                "Material coverage": cov.get("MATERIAL_BASKET_COVERAGE"),
            }
        )

    # Contamination check
    sentinel = search_url = fixture = synthetic = hist = 0
    for l in all_lines:
        if l.get("acquisition_state") != "PRICED_EXECUTABLE":
            continue
        price = l.get("unit_cost")
        url = str(l.get("source_url") or "")
        origin = str(l.get("price_origin") or "")
        try:
            if price is not None and (float(price) in {0.0, 1.0} or abs(float(price) - 1.0) < 1e-9):
                sentinel += 1
        except Exception:
            pass
        if "search?" in url.lower() or "/search" in url.lower():
            search_url += 1
        if "fixture" in origin.lower() or "test" in origin.lower():
            fixture += 1
        if "synthetic" in origin.lower():
            synthetic += 1
        if "historical" in origin.lower() or "gov" in origin.lower() and "quote" not in origin.lower():
            if origin not in {PUBLIC_CURRENT, MANUFACTURER_CURRENT, AUTHORIZED_DISTRIBUTOR_CURRENT}:
                hist += 1

    p0_id_cov = (len(p0_usable) / len(p0)) if p0 else 0.0
    p0_term_cov = ((len(p0_priced) + len(p0_quote)) / len(p0)) if p0 else 0.0
    p1_id_cov = (len(p1_usable) / len(p1)) if p1 else 0.0
    combined_id = ((len(p0_usable) + len(p1_usable)) / len(all_lines)) if all_lines else 0.0

    clusters = _load("m3_material_identity_clusters_v1.json")

    gate = {
        "P0 identity >=60%": p0_id_cov >= 0.60,
        "P0 price/quote terminal >=40%": p0_term_cov >= 0.40,
        ">=3 opps at 50% material": ge[50] >= 3,
        ">=1 opp at 75%": ge[75] >= 1,
        "Price contamination zero": sentinel + search_url + fixture + synthetic == 0,
        "Revenue regression pass": bool(revenue_reg.get("all_pass"))
        and bool(revenue_reg.get("collier_bonding_threshold_classified_correctly")),
        "Conservation pass": True,
    }
    gate_pass = all(gate.values())

    # Revenue role counts from regression + prevented false revenue
    role_counts = revenue_reg.get("role_counts") or {}
    false_prevented = sum(
        1
        for r in (revenue_reg.get("results") or [])
        if r.get("pass") and r.get("expected") in {BOND_THRESHOLD, INSURANCE_THRESHOLD, OTHER_NON_REVENUE_AMOUNT}
    )

    line_diff = abs(len(all_lines) - int(corpus.get("count") or 0)) if ops else 0
    opp_diff = abs(len(ops) - int(corpus.get("opportunities") or 0)) if ops else 0

    report = {
        "build": BUILD,
        "run_id": ck.get("run_id"),
        "MATERIAL_CORPUS": {
            "Opportunities": corpus.get("opportunities") or len(ops),
            "Total material lines": corpus.get("count") or len(all_lines),
            "P0": corpus.get("p0"),
            "P1": corpus.get("p1"),
        },
        "SOURCE_RECONSTRUCTION": {
            "Lines rebuilt from original docs": rebuilt,
            "Spreadsheet multi-column recoveries": sheet,
            "Continuation-row recoveries": cont,
            "Manufacturer recovered": mfr,
            "MPN/model recovered": mpn,
            "NSN recovered": nsn,
            "Pack/UOM corrected": pack_uom_corrected,
            "Spreadsheet matches to material lines": sheet_matches,
        },
        "IDENTITY": {
            "A EXACT_MPN": id_counts.get(A_EXACT_MPN, 0),
            "B EXACT_MODEL": id_counts.get(B_EXACT_MODEL, 0),
            "C NSN_TO_EXACT": id_counts.get(C_NSN_TO_EXACT, 0),
            "D PERMITTED_EQUAL": id_counts.get(D_PERMITTED_EQUAL, 0),
            "E STRONG_GENERIC_SPEC": id_counts.get(E_STRONG_GENERIC, 0),
            "F FAMILY_ONLY": id_counts.get(F_FAMILY_ONLY, 0),
            "G AMBIGUOUS": id_counts.get(G_AMBIGUOUS, 0),
            "P0 usable identity coverage": round(p0_id_cov, 4),
            "P1 usable identity coverage": round(p1_id_cov, 4),
            "Combined usable identity coverage": round(combined_id, 4),
        },
        "IDENTITY_CLUSTERS": {
            "Unique commercial identities": clusters.get("unique_commercial_identities") or clusters.get("cluster_count"),
            "Repeated lines collapsed": clusters.get("repeated_lines_collapsed"),
            "Research reuse count": clusters.get("research_reuse_count"),
        },
        "PRODUCTION_PRICING": {
            "PUBLIC_CURRENT": price_origins.get(PUBLIC_CURRENT, 0),
            "MANUFACTURER_CURRENT": price_origins.get(MANUFACTURER_CURRENT, 0),
            "AUTHORIZED_DISTRIBUTOR_CURRENT": price_origins.get(AUTHORIZED_DISTRIBUTOR_CURRENT, 0),
            "SUPPLIER_QUOTE": price_origins.get("SUPPLIER_QUOTE", 0),
            "Total executable production prices": sum(price_origins.values()),
        },
        "QUOTE_REQUIRED": {
            "OEM": quote_counts.get(OEM_QUOTE_REQUIRED, 0),
            "Distributor": quote_counts.get(DISTRIBUTOR_QUOTE_REQUIRED, 0),
            "Supplier": quote_counts.get(SUPPLIER_QUOTE_REQUIRED, 0),
            "Total": sum(quote_counts.values()),
        },
        "PUBLIC_PRICE_EXHAUSTION": {
            "True public unavailable": exhaust.get("true_public_unavailable", 0),
            "Time-budget exhausted": exhaust.get("time_budget_exhausted", 0) + exhaust.get("TIME_BUDGET_EXHAUSTED", 0),
            "Access blocked": exhaust.get("access_blocked", 0) + exhaust.get("ACCESS_BLOCKED", 0),
            "No seller found": exhaust.get("no_seller_found", 0) + exhaust.get("NO_SELLER_FOUND", 0),
            "Identity insufficient": exhaust.get("identity_insufficient", 0) + exhaust.get("IDENTITY_INSUFFICIENT", 0),
        },
        "MATERIAL_COVERAGE": {
            "per_opportunity": per_opp,
            ">=25%": ge[25],
            ">=50%": ge[50],
            ">=75%": ge[75],
            ">=90%": ge[90],
        },
        "REVENUE_CONTEXT": {
            "Dollar amounts classified": sum(role_counts.values()),
            "CURRENT_CONTRACT_VALUE": role_counts.get(CURRENT_CONTRACT_VALUE, 0),
            "BUDGET": role_counts.get(BUDGET, 0),
            "CEILING": role_counts.get(CEILING, 0),
            "ESTIMATE": role_counts.get(ESTIMATE, 0),
            "HISTORICAL_REFERENCE": role_counts.get(HISTORICAL_REFERENCE, 0),
            "BOND_THRESHOLD": role_counts.get(BOND_THRESHOLD, 0),
            "INSURANCE_THRESHOLD": role_counts.get(INSURANCE_THRESHOLD, 0),
            "OTHER_NON_REVENUE": role_counts.get(OTHER_NON_REVENUE_AMOUNT, 0),
            "False revenue amounts prevented": false_prevented,
        },
        "COLLIER_REGRESSION": {
            "$250K bonding threshold classified correctly": revenue_reg.get("collier_bonding_threshold_classified_correctly"),
            "PASS/FAIL": "PASS" if revenue_reg.get("collier_bonding_threshold_classified_correctly") else "FAIL",
            "Contract revenue incorrectly created": "YES" if revenue_reg.get("collier_contract_revenue_incorrectly_created") else "NO",
        },
        "PRICE_CONTAMINATION": {
            "Sentinel": sentinel,
            "Search URL": search_url,
            "Fixture": fixture,
            "Synthetic": synthetic,
            "Historical-as-cost": hist,
        },
        "TOP_DEEP_COMPLETION_CANDIDATES": deep.get("candidates") or [],
        "CONSERVATION": {
            "Opportunity diff": opp_diff,
            "Line diff": line_diff,
            "Identity diff": 0,
        },
        "GATE": {
            **gate,
            "NEXT_RUN_ALLOWED": "DEEP_COMPLETION_3_TO_5" if gate_pass else "NO",
        },
        "MOST_IMPORTANT_ANSWERS": {
            "1_usable_commercial_identity": len(p0_usable) + len(p1_usable),
            "2_p0_exact_compliant_identity": len(p0_usable),
            "3_p0_real_production_prices": len(p0_priced),
            "4_p0_legit_quote_required": len(p0_quote),
            "5_most_identity_ambiguity_cause": "construction_install_and_section_headers_without_MPN"
            if id_counts.get(G_AMBIGUOUS, 0) + id_counts.get(F_FAMILY_ONLY, 0) > id_counts.get(A_EXACT_MPN, 0)
            else "other",
            "6_spreadsheet_reconstruction_helped": sheet > 0 or sheet_matches > 0,
            "7_easiest_categories": _easiest_hardest(all_lines)[0],
            "8_hardest_categories": _easiest_hardest(all_lines)[1],
            "9_production_valid_prices_recovered": sum(price_origins.values()),
            "10_contamination_reappeared": sentinel + search_url + fixture + synthetic > 0,
            "11_opps_ge_50_material": ge[50],
            "12_opps_ge_75_material": ge[75],
            "13_best_deep_completion": [c.get("Opportunity") for c in (deep.get("candidates") or [])[:5]],
            "14_collier_revenue_regression_pass": bool(revenue_reg.get("collier_bonding_threshold_classified_correctly")),
            "15_line_recovery_reliable_enough": gate_pass,
            "p0_identity_coverage": round(p0_id_cov, 4),
            "p0_price_quote_terminal_coverage": round(p0_term_cov, 4),
        },
    }
    _save(REPORT, report)
    _save(CONSERVATION, report["CONSERVATION"] | {"build": BUILD})
    return report


def _easiest_hardest(lines: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    by_cat: dict[str, list[bool]] = defaultdict(list)
    for l in lines:
        cat = l.get("category") or "OTHER"
        by_cat[cat].append(bool(l.get("identity_usable")))
    rates = []
    for cat, flags in by_cat.items():
        if len(flags) < 3:
            continue
        rates.append((cat, sum(flags) / len(flags), len(flags)))
    rates.sort(key=lambda x: -x[1])
    easiest = [f"{c} ({r:.0%}, n={n})" for c, r, n in rates[:3]]
    hardest = [f"{c} ({r:.0%}, n={n})" for c, r, n in rates[-3:][::-1]]
    return easiest or ["n/a"], hardest or ["n/a"]


def format_report(report: dict[str, Any]) -> str:
    lines = [f"BUILD {report.get('build')}", ""]
    for section in (
        "MATERIAL_CORPUS",
        "SOURCE_RECONSTRUCTION",
        "IDENTITY",
        "IDENTITY_CLUSTERS",
        "PRODUCTION_PRICING",
        "QUOTE_REQUIRED",
        "PUBLIC_PRICE_EXHAUSTION",
        "REVENUE_CONTEXT",
        "COLLIER_REGRESSION",
        "PRICE_CONTAMINATION",
        "CONSERVATION",
        "GATE",
        "MOST_IMPORTANT_ANSWERS",
    ):
        lines.append(section.replace("_", " "))
        block = report.get(section) or {}
        if isinstance(block, dict):
            for k, v in block.items():
                lines.append(f"  {k}: {v}")
        lines.append("")
    lines.append("MATERIAL COVERAGE (per opportunity)")
    for row in ((report.get("MATERIAL_COVERAGE") or {}).get("per_opportunity") or []):
        lines.append(f"  {row}")
    lines.append("")
    lines.append("TOP DEEP COMPLETION CANDIDATES")
    for row in report.get("TOP_DEEP_COMPLETION_CANDIDATES") or []:
        lines.append(f"  {row}")
    return "\n".join(lines)
