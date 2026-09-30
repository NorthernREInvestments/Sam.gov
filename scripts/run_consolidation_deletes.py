"""Execute proven-safe M3 repository consolidation deletes.

Deletes only allowlisted proven-dead paths. Does not touch data/ business stores,
response_engine, secrets, or production phase_l core modules.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "repository_cleanup"
OUT.mkdir(parents=True, exist_ok=True)
BUILD = "20260930-m3-production-repository-consolidation"

DEAD_TOP = [
    "check_status.py",
    "claude_export.py",
    "clear_db.py",
    "refresh_workflow.py",
]

RESCUE_MODULES = [
    f"phase_l/l{n}_rescue.py"
    for n in (3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 21, 22, 23, 24, 25, 26, 27, 28, 29)
]

RESCUE_SCRIPTS = [
    "scripts/run_phase_l3_commercial_rebalance.py",
    "scripts/run_phase_l4_commercial_feed.py",
    "scripts/run_phase_l5_retention_repair.py",
    "scripts/run_phase_l6_quote_economics.py",
    "scripts/run_phase_l7_quote_readiness.py",
    "scripts/run_phase_l8_population_recovery.py",
    "scripts/run_phase_l9_quality_audit.py",
    "scripts/run_phase_l21_market_rescue.py",
    "scripts/run_phase_l22_product_resolution.py",
    "scripts/run_phase_l23_commercial_identity.py",
    "scripts/run_phase_l24_convergence.py",
    "scripts/run_phase_l25_source_expansion.py",
    "scripts/run_phase_l26_progressive_funnel.py",
    "scripts/run_phase_l27_resilient_pricing.py",
    "scripts/run_phase_l28_product_detail_resolution.py",
    "scripts/run_phase_l29_maximum_source_coverage.py",
]

RESCUE_TESTS = [
    "tests/test_phase_l3_commercial_rebalance.py",
    "tests/test_phase_l4_commercial_feed.py",
    "tests/test_phase_l5_retention.py",
    "tests/test_phase_l6_quote_economics.py",
    "tests/test_phase_l7_quote_readiness.py",
    "tests/test_phase_l8_population_recovery.py",
    "tests/test_phase_l9_quality_audit.py",
    "tests/test_phase_l21_market_rescue.py",
    "tests/test_phase_l22_product_resolution.py",
    "tests/test_phase_l23_commercial_identity.py",
    "tests/test_phase_l24_convergence.py",
    "tests/test_phase_l25_source_expansion.py",
    "tests/test_phase_l26_progressive_funnel.py",
    "tests/test_phase_l27_resilient_pricing.py",
    "tests/test_phase_l28_product_detail_resolution.py",
    "tests/test_phase_l29_maximum_source_coverage.py",
]

EARLY_PHASE_SCRIPTS = [
    "scripts/run_phase_l2_enrichment.py",
    "scripts/run_phase_l11_exact_history.py",
    "scripts/run_phase_l12_auth_history.py",
    "scripts/run_phase_l13_public_artifacts.py",
    "scripts/run_phase_l14_nonbidnet.py",
    "scripts/run_phase_l141_repair.py",
    "scripts/run_phase_l15_structured_expansion.py",
    "scripts/run_phase_l16_public_structured.py",
]

OBSOLETE_PHASE_TESTS = [
    "tests/test_phase_f_reality.py",
    "tests/test_phase_f_ui_torture.py",
    "tests/test_phase_e1_integrity.py",
]

KEEP_DOCS = {
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
    # Live operational phase docs required by tests / operators
    "phase_l21_quote_prep_strategy.md",
    "phase_l22_supplier_call_desk.md",
    "phase_l23_full_population_funnel.md",
    "phase_l231_population_audit.md",
}

OBSOLETE_DOC_PREFIXES = (
    "phase_l",
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


def _rm(path: Path, deleted: list, missing: list) -> None:
    if not path.exists():
        missing.append(str(path.relative_to(ROOT)).replace("\\", "/"))
        return
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
            if path.exists():
                # Windows lock — skip without failing the pass
                missing.append(str(path.relative_to(ROOT)).replace("\\", "/") + " (locked)")
                return
        else:
            path.unlink()
    except OSError as exc:
        missing.append(str(path.relative_to(ROOT)).replace("\\", "/") + f" ({exc})")
        return
    deleted.append(str(path.relative_to(ROOT)).replace("\\", "/"))


def main() -> dict:
    deleted: list[str] = []
    missing: list[str] = []

    for rel in (
        DEAD_TOP
        + RESCUE_MODULES
        + RESCUE_SCRIPTS
        + RESCUE_TESTS
        + EARLY_PHASE_SCRIPTS
        + OBSOLETE_PHASE_TESTS
    ):
        _rm(ROOT / rel, deleted, missing)

    for p in list((ROOT / "docs").glob("*.md")):
        if p.name in KEEP_DOCS:
            continue
        if p.name.startswith(("R1", "R2", "R3", "R4", "R5")):
            continue
        # Preserve live L.21–L.23.1 operator docs required by production tests
        if p.name.startswith(("phase_l21_", "phase_l22_", "phase_l23_", "phase_l231_")):
            continue
        if any(p.name.startswith(pref) for pref in OBSOLETE_DOC_PREFIXES):
            _rm(p, deleted, missing)

    for p in ROOT.rglob("__pycache__"):
        if p.is_dir() and ".git" not in p.parts:
            _rm(p, deleted, missing)
    for name in (".pytest_cache", ".mypy_cache", ".ruff_cache"):
        _rm(ROOT / name, deleted, missing)

    for p in list(ROOT.glob("_full_pytest*.txt")) + list(ROOT.glob("scripts/_full_pytest*.txt")):
        _rm(p, deleted, missing)

    manifest = {
        "build": BUILD,
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "deleted_count": len(deleted),
        "missing_count": len(missing),
        "deleted": sorted(deleted),
        "missing_already_absent": sorted(missing),
        "r5_known_good_commit": "2fe3e1629e4844846057ddcf564e73b83b421791",
    }
    (OUT / "deletion_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps({"deleted": len(deleted), "missing": len(missing)}, indent=2))
    return manifest


if __name__ == "__main__":
    main()
