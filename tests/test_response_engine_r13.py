"""R1.3 golden real-corpus regression — no live SAM API, no side effects."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts" / "response_engine"
CORPUS = ROOT / "data" / "response_corpus" / "real"


def test_r13_artifacts_exist_and_ready():
    summary = json.loads((ARTIFACTS / "r13_summary.json").read_text(encoding="utf-8"))
    assert summary["verdict"] == "PHASE_R13_RESPONSE_COMPILER_VALIDATED_READY"
    assert summary["r2_ready"] is True
    assert summary["real_projects"] >= 20
    assert summary["false_complete"] == 0
    assert summary["provenance_100"] is True
    assert summary["firewall_leaks"] == 0
    assert summary["unresolved_material_misses"] == 0
    assert summary["sam_api_calls"] == 0
    assert summary["ground_truth_captured"] == summary["ground_truth_total"]
    assert summary["ground_truth_total"] >= 100


def test_r13_coverage_matrix_met():
    matrix = json.loads((ARTIFACTS / "r13_coverage_matrix.json").read_text(encoding="utf-8"))
    gaps = [row for row in matrix["matrix"] if not row["met"]]
    assert gaps == [], gaps


def test_r13_golden_corpus_immutable_hashes():
    man = json.loads((CORPUS / "real_corpus_manifest.json").read_text(encoding="utf-8"))
    golden = json.loads((ARTIFACTS / "r13_golden_corpus_results.json").read_text(encoding="utf-8"))
    assert len(golden["golden"]) >= 15
    for pid in golden["golden"]:
        project = (man.get("projects") or {}).get(pid)
        assert project, pid
        assert "GOLDEN_CORPUS" in (project.get("tags") or []) or project.get("golden")
        docs = project.get("documents") or []
        assert docs, pid
        for d in docs:
            path = Path(d["path"])
            assert path.exists(), d["path"]
            import hashlib

            h = hashlib.sha256(path.read_bytes()).hexdigest()
            assert h == d["sha256"], f"hash drift {pid} {d['filename']}"


def test_r13_acknowledge_amendment_pattern():
    from response_engine.requirements import compile_requirements_from_text

    reqs = compile_requirements_from_text(
        response_project_id="t",
        text="3. Acknowledge the amendment. Then certify and resubmit.",
        source_document_id="d1",
    )
    cats = {r["requirement_category"] for r in reqs}
    assert "AMENDMENT_ACK" in cats
    assert all(r.get("source_document_id") or (r.get("provenance") or {}).get("document_id") for r in reqs)


def test_r13_exact_vs_brand_or_equal():
    from response_engine.requirements import compile_requirements_from_text

    exact = compile_requirements_from_text(
        response_project_id="t",
        text="Only approved sources. No substitutions. Exact OEM part required.",
        source_document_id="d1",
    )
    brand = compile_requirements_from_text(
        response_project_id="t",
        text="Brand Name or Equal: Acme Widget 100 or equal.",
        source_document_id="d2",
    )
    assert any(r["requirement_category"] == "EXACT_BRAND" for r in exact)
    assert any(r["requirement_category"] == "BRAND_OR_EQUAL" for r in brand)
    assert not any(r["requirement_category"] == "BRAND_OR_EQUAL" for r in exact)


def test_r13_corpus_store_blocks_sam_api():
    from response_engine.corpus_store import fetch_public_url

    r = fetch_public_url("https://api.sam.gov/opportunities/v2/search")
    assert r["ok"] is False
    assert r["status"] == "SAM_API_BLOCKED"


def test_r13_acquisition_log_present():
    log = json.loads((ARTIFACTS / "r13_corpus_acquisition_log.json").read_text(encoding="utf-8"))
    assert log["accepted"] >= 20
    assert log["sam_api_calls"] == 0
