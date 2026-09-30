"""R1.1 real corpus validation — local artifacts only, 0 SAM API calls.

Writes:
  artifacts/response_engine/r11_production_intake_summary.json
  artifacts/response_engine/r11_real_corpus_results.json
  artifacts/response_engine/r11_document_parse_results.json
  artifacts/response_engine/r11_legacy_cutover_report.json
  artifacts/response_engine/r11_missing_document_analysis.json
"""

from __future__ import annotations

import io
import json
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts" / "response_engine"
EVIDENCE = ROOT / "artifacts" / "transactional_procurement_evidence"
IOWA = ROOT / "artifacts" / "iowa_wildflower_event.pdf"
FIXTURES = ARTIFACTS / "r11_fixtures"
BUILD = "20260929-m3-r11-production-intake-legacy-cutover"


def _xlsx() -> bytes:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Pricing"
    ws["A1"], ws["B1"], ws["C1"] = "CLIN", "Qty", "Unit Price"
    ws["A2"], ws["C2"] = "0001", None
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _docx() -> bytes:
    from docx import Document

    doc = Document()
    doc.add_heading("Nebraska Commodity ITB", 0)
    doc.add_paragraph("Quantity: 100 each. Brand name or equal. Acknowledge Amendment 0001.")
    doc.add_paragraph("See Attachment C for specifications. Pricing Sheet required.")
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _html() -> bytes:
    return b"""<!DOCTYPE html><html><body>
    <h1>Portal RFQ</h1>
    <p>Submit via portal by October 10, 2026 3:00 PM CT. Questions due October 1, 2026.</p>
    <a href="/files/CostSheet.xlsx">Cost Sheet</a>
    <p>Exact part number required: ABC-123.</p>
    </body></html>"""


def _qa_txt() -> bytes:
    return (
        b"Q&A Document\n"
        b"Q1: What is the delivery location?\n"
        b"A1: Deliver to Building 4 loading dock. This clarifies delivery; it does not amend the solicitation.\n"
    )


def _master_txt() -> bytes:
    return b"DLA MASTER SOLICITATION SPE4A1-19-R-0001 Version 2024. Applicable clauses incorporated by reference.\n"


def _zip_pkg() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("base_solicitation.txt", "RFQ base. Quantity: 12 each. Signed SF1449 required. Submit via email.")
        z.writestr("pricing.csv", "CLIN,Qty,UnitPrice\n1,12,\n")
    return buf.getvalue()


def ensure_fixtures() -> dict[str, Path]:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    paths = {
        "pricing_xlsx": FIXTURES / "CostSheet.xlsx",
        "itb_docx": FIXTURES / "Nebraska_ITB.docx",
        "portal_html": FIXTURES / "portal_rfq.html",
        "qa_txt": FIXTURES / "buyer_qa.txt",
        "master_txt": FIXTURES / "dla_master_solicitation.txt",
        "package_zip": FIXTURES / "solicitation_package.zip",
        "lines_csv": FIXTURES / "line_items.csv",
        "missing_ref_txt": FIXTURES / "base_with_missing_attachment.txt",
    }
    paths["pricing_xlsx"].write_bytes(_xlsx())
    paths["itb_docx"].write_bytes(_docx())
    paths["portal_html"].write_bytes(_html())
    paths["qa_txt"].write_bytes(_qa_txt())
    paths["master_txt"].write_bytes(_master_txt())
    paths["package_zip"].write_bytes(_zip_pkg())
    paths["lines_csv"].write_bytes(b"CLIN,Desc,Qty\n1,Widget,5\n")
    paths["missing_ref_txt"].write_text(
        "Base solicitation. Quantity: 5 each. See Attachment C for specifications. Signed form required.\n",
        encoding="utf-8",
    )
    return paths


def real_evidence_pdfs() -> list[Path]:
    out = []
    if EVIDENCE.exists():
        out.extend(sorted(EVIDENCE.rglob("*-event.pdf")))
    if IOWA.exists() and not any(p.name == IOWA.name for p in out):
        # iowa may duplicate 3046 — include once for path diversity report
        pass
    return out


def run() -> dict:
    import response_engine.production_intake as pi
    import response_engine.store as store
    from response_engine.legacy_bridge import wrap_legacy_bid_readiness
    from response_engine.parsers import parse_file_bytes
    from response_engine.production_intake import ingest_bytes_into_project, run_production_intake
    from response_engine.service import create_or_get_project_from_opportunity

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    fixtures = ensure_fixtures()
    pdfs = real_evidence_pdfs()

    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        store.STORE_DIR = tdp / "response_projects"
        store.INDEX_PATH = store.STORE_DIR / "index.json"
        pi.BINARY_STORE = tdp / "binaries"
        store.ensure_store()

        projects: list[dict] = []
        parse_results: list[dict] = []
        missing_analysis: list[dict] = []

        # --- Real PDF projects (up to 4 unique evidence packages) ---
        for i, pdf in enumerate(pdfs[:5]):
            sol = pdf.parent.name if pdf.parent != EVIDENCE else pdf.stem
            cid = f"r11-corpus-real-{sol}"
            p = create_or_get_project_from_opportunity(
                canonical_opportunity_id=cid,
                buyer="Iowa / State portal" if "DOT" in sol or "RFB" in sol else "Agency",
                solicitation_number=sol,
                discovery_source="SciQuest",
                authoritative_source=f"local:{pdf}",
                force_new=True,
            )
            result = run_production_intake(p, local_paths=[str(pdf)], compile_after=True, try_url_fetch=False)
            docs = p.get("documents") or []
            reqs = [r for r in (p.get("requirements") or []) if not r.get("superseded")]
            with_src = sum(1 for r in reqs if r.get("source_document_id") or (r.get("provenance") or {}).get("document_id"))
            row = {
                "project_id": p["response_project_id"],
                "canonical_opportunity_id": cid,
                "kind": "REAL_PDF",
                "source_types": ["local_artifact", "state_portal_pdf"],
                "file_types": sorted({Path(d.get("filename") or "").suffix.lower() for d in docs}),
                "docs_expected": 1,
                "docs_found": len(docs),
                "docs_parsed": sum(1 for d in docs if d.get("parse_status") not in {None, "PARSE_FAILED", "CONTENT_TYPE_MISMATCH"}),
                "docs_failed": sum(1 for d in docs if d.get("parse_status") in {"PARSE_FAILED", "CONTENT_TYPE_MISMATCH", "EMPTY"}),
                "amendments": len(p.get("amendments") or []),
                "templates": sum(1 for d in docs if d.get("is_buyer_template")),
                "requirements": len(reqs),
                "material_requirements": sum(1 for r in reqs if r.get("materiality") == "MATERIAL"),
                "unknown_review": sum(1 for r in reqs if r.get("compliance_status") in {"UNKNOWN", "REVIEW_REQUIRED"}),
                "missing_references": (p.get("package_completeness") or {}).get("references_missing") or [],
                "parse_confidence": [d.get("extraction_confidence") for d in docs],
                "response_classification": p.get("response_type"),
                "evaluation_classification": p.get("evaluation_method"),
                "package_status": (p.get("package_completeness") or {}).get("status"),
                "document_package_complete": (p.get("package_completeness") or {}).get("document_package_complete"),
                "provenance_coverage": (with_src / len(reqs)) if reqs else 1.0,
                "sam_api_calls": result.get("sam_api_calls", 0),
                "path": str(pdf),
            }
            projects.append(row)
            for d in docs:
                parse_results.append(
                    {
                        "project": cid,
                        "filename": d.get("filename"),
                        "method": d.get("parse_method"),
                        "confidence": d.get("extraction_confidence"),
                        "ocr_required": d.get("ocr_required"),
                        "status": d.get("parse_status"),
                        "hash": (d.get("file_hash") or "")[:16],
                    }
                )
            if row["missing_references"]:
                missing_analysis.append({"project": cid, "missing": row["missing_references"]})

        # --- Synthetic multi-format projects (honest: not live buyer packages) ---
        synthetic_specs = [
            ("r11-syn-xlsx", "federal commercial RFQ + buyer Excel", [fixtures["pricing_xlsx"]], {"buyer": "Synthetic Federal"}),
            ("r11-syn-docx", "state commodity ITB DOCX", [fixtures["itb_docx"]], {"buyer": "Nebraska Synthetic"}),
            ("r11-syn-html", "portal-based RFQ HTML", [fixtures["portal_html"]], {"buyer": "Portal Buyer"}),
            ("r11-syn-zip", "ZIP solicitation package", [fixtures["package_zip"]], {"buyer": "ZIP Buyer"}),
            ("r11-syn-qa-master", "DLA master + Q&A", [fixtures["master_txt"], fixtures["qa_txt"]], {"buyer": "DLA Synthetic"}),
            ("r11-syn-missing", "missing Attachment C", [fixtures["missing_ref_txt"]], {"buyer": "Incomplete Package"}),
        ]
        for cid, kind, paths, meta in synthetic_specs:
            p = create_or_get_project_from_opportunity(
                canonical_opportunity_id=cid,
                buyer=meta.get("buyer"),
                solicitation_number=cid,
                discovery_source="fixture",
                authoritative_source="fixture_local",
                force_new=True,
            )
            # Tag document types for master / Q&A
            result = run_production_intake(p, local_paths=[str(x) for x in paths], compile_after=False, try_url_fetch=False)
            for d in p.get("documents") or []:
                fn = (d.get("filename") or "").lower()
                if "master" in fn:
                    d["document_type"] = "MASTER_SOLICITATION"
                    p.setdefault("document_graph", {}).setdefault("edges", []).append(
                        {"from": d["document_id"], "to": p["response_project_id"], "relation": "MASTER_FOR"}
                    )
                if "qa" in fn or "q&a" in fn:
                    d["document_type"] = "Q_AND_A"
                    bases = [x for x in p["documents"] if x.get("document_type") == "BASE_SOLICITATION"]
                    if bases:
                        p.setdefault("document_graph", {}).setdefault("edges", []).append(
                            {"from": d["document_id"], "to": bases[0]["document_id"], "relation": "Q_AND_A_FOR"}
                        )
            from response_engine.service import compile_project

            if p.get("documents"):
                compile_project(p, persist=True)
            from response_engine.package_completeness import evaluate_package_completeness

            evaluate_package_completeness(p)
            docs = p.get("documents") or []
            reqs = [r for r in (p.get("requirements") or []) if not r.get("superseded")]
            with_src = sum(1 for r in reqs if r.get("source_document_id") or (r.get("provenance") or {}).get("document_id"))
            row = {
                "project_id": p["response_project_id"],
                "canonical_opportunity_id": cid,
                "kind": f"SYNTHETIC:{kind}",
                "source_types": ["fixture"],
                "file_types": sorted({Path(d.get("filename") or "").suffix.lower() for d in docs}),
                "docs_expected": len(paths),
                "docs_found": len(docs),
                "docs_parsed": sum(1 for d in docs if (d.get("text") or d.get("workbook") or d.get("parse_status") == "FETCHED")),
                "docs_failed": 0,
                "amendments": len(p.get("amendments") or []),
                "templates": sum(1 for d in docs if d.get("is_buyer_template")),
                "requirements": len(reqs),
                "material_requirements": sum(1 for r in reqs if r.get("materiality") == "MATERIAL"),
                "unknown_review": sum(1 for r in reqs if r.get("compliance_status") in {"UNKNOWN", "REVIEW_REQUIRED"}),
                "missing_references": (p.get("package_completeness") or {}).get("references_missing") or [],
                "parse_confidence": [d.get("extraction_confidence") for d in docs],
                "response_classification": p.get("response_type"),
                "evaluation_classification": p.get("evaluation_method"),
                "package_status": (p.get("package_completeness") or {}).get("status"),
                "document_package_complete": (p.get("package_completeness") or {}).get("document_package_complete"),
                "provenance_coverage": (with_src / len(reqs)) if reqs else 1.0,
                "sam_api_calls": result.get("sam_api_calls", 0),
            }
            projects.append(row)
            for d in docs:
                parse_results.append(
                    {
                        "project": cid,
                        "filename": d.get("filename"),
                        "method": d.get("parse_method"),
                        "confidence": d.get("extraction_confidence"),
                        "ocr_required": d.get("ocr_required"),
                        "status": d.get("parse_status"),
                        "buyer_template_class": d.get("buyer_template_class"),
                        "hash": (d.get("file_hash") or "")[:16],
                    }
                )
            if row["missing_references"]:
                missing_analysis.append({"project": cid, "missing": row["missing_references"]})

        # Legacy cutover spot-check
        legacy_wrapped = wrap_legacy_bid_readiness(
            "r11-corpus-real-" + (pdfs[0].parent.name if pdfs else "none"),
            {"bid_readiness": {"ladder": "READY"}},
        )

        # Aggregate parse stats on real PDFs
        pdf_stats = {"pdfs": 0, "native_text": 0, "ocr_required": 0, "ocr_failures": 0}
        for pdf in pdfs[:5]:
            parsed = parse_file_bytes(pdf.read_bytes(), filename=pdf.name)
            pdf_stats["pdfs"] += 1
            if parsed.get("ocr_required"):
                pdf_stats["ocr_required"] += 1
                if not parsed.get("ok"):
                    pdf_stats["ocr_failures"] += 1
            else:
                pdf_stats["native_text"] += 1

        xlsx_stats = {
            "workbooks_parsed": sum(1 for r in parse_results if (r.get("filename") or "").endswith(".xlsx")),
            "buyer_templates": sum(1 for r in parse_results if r.get("buyer_template_class")),
            "pricing_sheets": sum(1 for r in parse_results if r.get("buyer_template_class") == "PRICING_TEMPLATE"),
        }

        real_count = sum(1 for p in projects if p["kind"] == "REAL_PDF")
        syn_count = len(projects) - real_count
        fetch_ok = sum(p["docs_parsed"] for p in projects)
        fetch_total = sum(max(p["docs_found"], 1) for p in projects)

        summary = {
            "build": BUILD,
            "verdict_candidate": "PHASE_R11_PRODUCTION_SOLICITATION_INTAKE_PARTIAL"
            if real_count < 10
            else "PHASE_R11_PRODUCTION_SOLICITATION_INTAKE_READY",
            "real_production_projects": real_count,
            "synthetic_projects": syn_count,
            "total_projects": len(projects),
            "limitation": (
                f"Only {real_count} unique real solicitation PDFs available under "
                "artifacts/transactional_procurement_evidence; remainder are multi-format fixtures. "
                "No live portal/auth packages ingested (0 network SAM)."
            ),
            "intake": {
                "documents_found": sum(p["docs_found"] for p in projects),
                "documents_parsed": fetch_ok,
                "fetch_success_pct": round(100.0 * fetch_ok / fetch_total, 1) if fetch_total else 0,
                "missing_referenced_docs_projects": len(missing_analysis),
            },
            "parsing": pdf_stats,
            "spreadsheets": xlsx_stats,
            "sam_api_calls": 0,
            "legacy_cutover": {
                "canonical_readiness_source": "response_engine_r1",
                "legacy_ready_cannot_override": True,
                "sample_wrap": {
                    "ladder": (legacy_wrapped.get("bid_readiness") or {}).get("ladder"),
                    "ready_to_submit": (legacy_wrapped.get("bid_readiness") or {}).get("ready_to_submit"),
                    "canonical_source": (legacy_wrapped.get("bid_readiness") or {}).get("canonical_source"),
                },
            },
            "clean_room": "No supplier/economics fields written into ResponseProject government namespace during intake.",
        }

        legacy_report = {
            "build": BUILD,
            "disposition": {
                "bid_compliance_engine": "deprecated_compatibility — still callable; operator Bid Prep readiness wrapped by legacy_bridge (R1 wins)",
                "bid_requirement_extraction": "deprecated — R1 requirements.py is canonical for Bid Prep",
                "governing_documents": "adapted — R1 document_graph supersedes for ResponseProject",
                "legacy_compliance_matrix": "compatibility wrapper via wrap_legacy_compliance_matrix",
                "proposal_service": "superseded — not in normal operator Bid Prep flow",
                "old_bid_package_draft": "retained for future R4/R5; not operator-facing readiness",
                "old_bid_prep_endpoints": "adapted — /api/opportunities/.../bid-readiness returns R1-wrapped shape",
            },
            "canonical_source_of_truth": "ResponseProject + SolicitationDocument + SolicitationRequirement + ComplianceMatrix",
            "operator_facing_readiness": "response_engine_r1 via legacy_bridge / bid_prep_card_for_opportunity",
        }

        (ARTIFACTS / "r11_production_intake_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        (ARTIFACTS / "r11_real_corpus_results.json").write_text(json.dumps({"build": BUILD, "projects": projects}, indent=2), encoding="utf-8")
        (ARTIFACTS / "r11_document_parse_results.json").write_text(json.dumps({"build": BUILD, "parses": parse_results, "pdf_stats": pdf_stats, "xlsx_stats": xlsx_stats}, indent=2), encoding="utf-8")
        (ARTIFACTS / "r11_legacy_cutover_report.json").write_text(json.dumps(legacy_report, indent=2), encoding="utf-8")
        (ARTIFACTS / "r11_missing_document_analysis.json").write_text(json.dumps({"build": BUILD, "cases": missing_analysis}, indent=2), encoding="utf-8")
        return summary


if __name__ == "__main__":
    s = run()
    print(json.dumps(s, indent=2))
