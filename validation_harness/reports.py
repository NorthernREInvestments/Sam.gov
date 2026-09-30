"""Validation run artifacts + human-readable report."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from application_clock import now_utc
from validation_harness.gap_register import GapRegister

COVERAGE_DOMAINS = (
    "Discovery",
    "Product Identity",
    "Quantity/UOM",
    "History",
    "Supplier",
    "Economics",
    "Packaging",
    "Delivery",
    "Inspection",
    "Acceptance",
    "Technical Data",
    "Submission",
    "Financing",
    "Post Award",
    "Invoice",
    "Payment",
    "Workflow",
)

_TAG_TO_COVERAGE = {
    "product": "Product Identity",
    "identity": "Product Identity",
    "quantity": "Quantity/UOM",
    "uom": "Quantity/UOM",
    "clin": "Quantity/UOM",
    "idiq": "Quantity/UOM",
    "packaging": "Packaging",
    "delivery": "Delivery",
    "fob": "Delivery",
    "inspection": "Inspection",
    "acceptance": "Acceptance",
    "submission": "Submission",
    "amendment": "Submission",
    "financing": "Financing",
    "supplier": "Supplier",
    "wawf": "Invoice",
    "invoice": "Invoice",
    "payment": "Payment",
    "post_award": "Post Award",
    "workflow": "Workflow",
    "readiness": "Workflow",
    "adversarial": "Workflow",
}


def _artifacts_root() -> Path:
    return Path(__file__).resolve().parents[1] / "artifacts" / "validation"


def coverage_report(results: list[dict[str, Any]]) -> dict[str, Any]:
    covered: set[str] = set()
    for r in results:
        for tag in r.get("tags") or []:
            for key, domain in _TAG_TO_COVERAGE.items():
                if key in str(tag).lower():
                    covered.add(domain)
        # infer from expected keys via case tags only; also from actuals meta
        actuals = r.get("actuals") or {}
        if actuals.get("product_identity"):
            covered.add("Product Identity")
        if actuals.get("quantity_uom"):
            covered.add("Quantity/UOM")
        if actuals.get("packaging"):
            covered.add("Packaging")
        if actuals.get("shipping_delivery"):
            covered.add("Delivery")
        if actuals.get("inspection_acceptance"):
            covered.add("Inspection")
            covered.add("Acceptance")
        if actuals.get("submission"):
            covered.add("Submission")
        if actuals.get("financing"):
            covered.add("Financing")
        if actuals.get("invoice_payment"):
            covered.add("Invoice")
            covered.add("Payment")
        if actuals.get("post_award"):
            covered.add("Post Award")
        if actuals.get("operator_workflow") or actuals.get("owner_readiness"):
            covered.add("Workflow")
        if actuals.get("supplier"):
            covered.add("Supplier")
    missing = [d for d in COVERAGE_DOMAINS if d not in covered]
    return {
        "domains": list(COVERAGE_DOMAINS),
        "covered": sorted(covered),
        "missing_or_thin": missing,
        "note": "Discovery/History/Technical Data/Economics may remain thin until live golden cases are curated.",
    }


def render_markdown(run: dict[str, Any], coverage: dict[str, Any]) -> str:
    depth = run.get("depth_breakdown") or {}
    lines = [
        "# M3 VALIDATION REPORT",
        "",
        f"Run ID: `{run.get('run_id')}`",
        f"Build: `{run.get('build')}`",
        "",
        "## Coverage depth (do not collapse)",
        "",
        f"Execution-path golden cases: **{depth.get('execution_path_golden_cases', 0)}**",
        "",
        f"Broader-pipeline golden cases: **{depth.get('broader_pipeline_golden_cases', 0)}**",
        "",
        f"Adversarial readiness cases: **{depth.get('adversarial_readiness_cases', 0)}**",
        "",
        f"Positive-ready cases: **{depth.get('positive_ready_cases', 0)}**",
        "",
        f"Reality cases (Phase F): **{depth.get('reality_cases', 0)}**",
        "",
        f"_Note: {run.get('started_note') or 'Depths are not interchangeable.'}_",
        "",
        f"Cases (total): **{run.get('case_count')}**",
        f"Passed: **{run.get('passed')}**",
        f"Partial: **{run.get('partial')}**",
        f"Failed: **{run.get('failed')}**",
        f"Gaps: **{run.get('gap_count')}** (critical: {run.get('critical_gaps')})",
        "",
        "## CRITICAL GAPS",
        "",
    ]
    critical = [g for g in (run.get("gaps") or []) if g.get("severity") == "CRITICAL"]
    if not critical:
        lines.append("_None._")
    else:
        for g in critical:
            lines.extend(
                [
                    f"### {g.get('case_id')} — {g.get('field_path')}",
                    "",
                    f"Expected: `{g.get('expected')}`",
                    "",
                    f"Actual: `{g.get('actual')}`",
                    "",
                    f"Classification: **{g.get('category')}**",
                    "",
                    f"Likely component: `{g.get('likely_module')}`",
                    "",
                    f"Potential consequence: {g.get('consequence')}",
                    "",
                ]
            )
    lines.extend(["## Coverage", ""])
    lines.append("Covered: " + ", ".join(coverage.get("covered") or []) or "_none_")
    lines.append("")
    lines.append("Blind spots: " + ", ".join(coverage.get("missing_or_thin") or []) or "_none_")
    lines.append("")
    lines.append("## Case results")
    lines.append("")
    for r in run.get("results") or []:
        lines.append(f"- **{r.get('case_id')}** `{r.get('status')}` — {r.get('case_name')} (gaps={len(r.get('gaps') or [])})")
    lines.append("")
    lines.append("_Golden expected truth is curated. M3 never authors its own gold standard._")
    return "\n".join(lines)


def write_run_artifacts(run: dict[str, Any], *, root: Path | None = None) -> dict[str, Any]:
    base = root or _artifacts_root()
    run_id = str(run.get("run_id") or now_utc().strftime("%Y%m%dT%H%M%SZ"))
    out_dir = base / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    coverage = coverage_report(list(run.get("results") or []))
    summary = {
        "run_id": run_id,
        "build": run.get("build"),
        "case_count": run.get("case_count"),
        "passed": run.get("passed"),
        "partial": run.get("partial"),
        "failed": run.get("failed"),
        "gap_count": run.get("gap_count"),
        "critical_gaps": run.get("critical_gaps"),
        "depth_breakdown": run.get("depth_breakdown") or {},
        "coverage": coverage,
        "generated_at": now_utc().isoformat(),
        "note": run.get("started_note"),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (out_dir / "gaps.json").write_text(json.dumps(run.get("gaps") or [], indent=2), encoding="utf-8")
    (out_dir / "case_results.json").write_text(json.dumps(run.get("results") or [], indent=2, default=str), encoding="utf-8")
    (out_dir / "report.md").write_text(render_markdown(run, coverage), encoding="utf-8")

    # Persist gap register history
    reg = GapRegister(base / "gap_register.json")
    reg.ingest(list(run.get("gaps") or []), run_id=run_id)
    reg_path = reg.save()

    return {
        "output_dir": str(out_dir),
        "summary": str(out_dir / "summary.json"),
        "gaps": str(out_dir / "gaps.json"),
        "case_results": str(out_dir / "case_results.json"),
        "report": str(out_dir / "report.md"),
        "gap_register": str(reg_path),
    }
