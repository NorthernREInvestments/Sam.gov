"""Inventory for M3 production consolidation — no deletes."""
from __future__ import annotations

import collections
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "repository_cleanup"
OUT.mkdir(parents=True, exist_ok=True)

SKIP_PARTS = {".venv", "venv", "__pycache__", ".git", ".pytest_cache"}


def iter_py():
    for p in ROOT.rglob("*.py"):
        if SKIP_PARTS.intersection(p.parts):
            continue
        yield p


def main() -> dict:
    all_py = list(iter_py())
    top_py = sorted(ROOT.glob("*.py"))

    # External import refs for top-level modules
    rows = []
    for p in top_py:
        name = p.stem
        refs = []
        pat = re.compile(rf"(?:from|import)\s+{re.escape(name)}(?:\.|\s|,|$)")
        for q in all_py:
            if q == p:
                continue
            try:
                t = q.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if pat.search(t):
                refs.append(str(q.relative_to(ROOT)).replace("\\", "/"))
        rows.append(
            {
                "module": name,
                "external_importers": len(refs),
                "importers_sample": refs[:12],
                "bytes": p.stat().st_size,
                "zero_external": len(refs) == 0,
            }
        )
    rows.sort(key=lambda r: (r["external_importers"], r["module"]))

    # phase_l modules with zero importers outside package tests/scripts
    phase_l_dead = []
    for p in sorted((ROOT / "phase_l").rglob("*.py")):
        if p.name == "__init__.py":
            continue
        mod = "phase_l." + p.relative_to(ROOT / "phase_l").with_suffix("").as_posix().replace("/", ".")
        short = p.stem
        importers = []
        for q in all_py:
            if q == p or "phase_l" in q.parts and q.parent == p.parent:
                # still count other files
                pass
            try:
                t = q.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if q == p:
                continue
            if re.search(rf"(?:from|import)\s+phase_l(?:\.{re.escape(short)}|\s+import\s+[^\n]*\b{re.escape(short)}\b)", t) or (
                f"phase_l.{short}" in t or f"from phase_l import {short}" in t or f"import phase_l.{short}" in t
            ):
                # more precise
                if re.search(
                    rf"(from\s+phase_l(?:\.[a-zA-Z0-9_\.]+)?\s+import\s+[^\n]*\b{re.escape(short)}\b)|"
                    rf"(import\s+phase_l\.{re.escape(short)}\b)|"
                    rf"(from\s+phase_l\.{re.escape(short)}\s+import)",
                    t,
                ):
                    importers.append(str(q.relative_to(ROOT)).replace("\\", "/"))
        # also check app dynamic
        if not importers:
            phase_l_dead.append({"module": f"phase_l/{p.relative_to(ROOT/'phase_l').as_posix()}", "stem": short})

    docs = list((ROOT / "docs").glob("*.md"))
    obsolete_doc_prefixes = (
        "phase_l3_",
        "phase_l4_",
        "phase_l5_",
        "phase_l6_",
        "phase_l7_",
        "phase_l8_",
        "phase_l9_",
        "phase_l10_",
        "phase_l11_",
        "phase_l12_",
        "phase_l13_",
        "phase_l14_",
        "phase_l141_",
        "phase_l15_",
        "phase_l16_",
        "phase_l17_",
        "phase_l172_",
        "phase_l173_",
        "phase_l18_",
        "phase_l19_",
        "phase_l20_",
        "phase_l24_",
        "phase_l25_",
        "phase_l26_",
        "phase_l27_",
        "phase_l28_",
        "phase_l29_",
        "phase_f_",
        "phase_g_",
        "phase_h_",
        "phase_i_",
        "phase_j_",
        "phase_k_",
        "phase_e1_",
        "pilot_",
        "eligibility_gate_audit",
        "eligibility_gate_completion",
        "eligibility_gate_design",
        "eligibility_gate_regression",
        "end_to_end_validation_audit",
        "execution_compliance_audit",
        "operator_ui_audit",
        "operator_workflow_mapping",
        "sam_live_fallback_design",
        "sam_live_fallback_phase_k",
        "sam_live_fallback_regression",
        "repository_cleanup_audit",
    )
    # Keep CURRENT architecture, R*, M3 operator, SAM policy, PHASE_HISTORY, ARTIFACT, NATIONAL, PRODUCT
    keep_docs = {
        "CURRENT_M3_ARCHITECTURE.md",
        "CURRENT_M3_UI_ARCHITECTURE.md",
        "CURRENT_RUNTIME_FILE_MAP.md",
        "PHASE_HISTORY_CHANGELOG.md",
        "ARTIFACT_RETENTION_POLICY.md",
        "RESPONSE_ENGINE_ARCHITECTURE.md",
        "REPOSITORY_CLEANUP_RESULTS.md",
        "M3_OPERATOR_QUICKSTART.md",
        "M3_OWNER_APPROVAL_QUICKSTART.md",
        "M3_OPERATOR_CHEATSHEET.md",
        "M3_OPERATOR_TRAINING_TEST.md",
        "NATIONAL_SOURCE_EXPANSION.md",
        "PRODUCT_DENSITY.md",
        "SAM_API_BUDGET_POLICY.md",
        "SAM_CACHE_AND_LEDGER.md",
        "SAM_CALL_VALUE_PLANNER.md",
        "SAM_QUERY_PLANNER.md",
        "SUPPLIER_PATH_CONVERSION.md",
        "eligibility_gate_design.md",  # maybe keep design? mark obsolete if superseded
    }
    obsolete_docs = []
    for d in docs:
        if d.name.startswith("R1") or d.name.startswith("R2") or d.name.startswith("R3") or d.name.startswith("R4") or d.name.startswith("R5"):
            continue
        if d.name in keep_docs:
            continue
        if any(d.name.startswith(pref) for pref in obsolete_doc_prefixes) or d.name.startswith("phase_l_"):
            obsolete_docs.append(d.name)

    tests = list((ROOT / "tests").glob("test_*.py"))
    test_groups = {
        "phase_l_early": [t.name for t in tests if re.match(r"test_phase_l([3-9]|1[0-9]|2[4-9]|141|172|173)_", t.name)],
        "phase_g_k": [t.name for t in tests if re.match(r"test_phase_[fghijke]", t.name)],
        "m3_read_models": [t.name for t in tests if t.name.startswith("test_m3_") and t.name not in {
            "test_m3_primary_application.py",
            "test_m3_final_operator_dry_run.py",
            "test_m3_live_opportunity_novelty.py",
            "test_m3_pipeline_empty_save_guard.py",
        }],
    }

    report = {
        "top_level_zero_external": [r for r in rows if r["zero_external"]],
        "top_level_low_external": [r for r in rows if 0 < r["external_importers"] <= 2],
        "top_level_all": rows,
        "phase_l_zero_importers_candidate": phase_l_dead[:80],
        "docs_total": len(docs),
        "obsolete_docs_candidate": sorted(obsolete_docs),
        "obsolete_docs_count": len(obsolete_docs),
        "tests_total": len(tests),
        "test_groups": {k: {"count": len(v), "files": sorted(v)} for k, v in test_groups.items()},
        "artifact_dirs": {},
    }
    art = ROOT / "artifacts"
    if art.exists():
        for d in sorted([x for x in art.iterdir() if x.is_dir()]):
            files = [f for f in d.rglob("*") if f.is_file()]
            report["artifact_dirs"][d.name] = {
                "files": len(files),
                "bytes": sum(f.stat().st_size for f in files),
            }

    (OUT / "inventory.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({
        "zero_external_top": len(report["top_level_zero_external"]),
        "obsolete_docs": report["obsolete_docs_count"],
        "phase_l_zero_candidates": len(report["phase_l_zero_importers_candidate"]),
        "tests": report["tests_total"],
        "out": str(OUT / "inventory.json"),
    }, indent=2))
    print("\nZERO EXTERNAL TOP-LEVEL:")
    for r in report["top_level_zero_external"]:
        print(" ", r["module"], r["bytes"])
    return report


if __name__ == "__main__":
    main()
