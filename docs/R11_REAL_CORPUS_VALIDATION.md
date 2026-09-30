# R1.1 Real Corpus Validation

**Script:** `scripts/run_r11_corpus_validation.py`  
**Artifacts:** `artifacts/response_engine/r11_*.json`

## Method

- **0 SAM API calls**
- Prefer real PDFs under `artifacts/transactional_procurement_evidence/`
- Multi-format fixtures under `artifacts/response_engine/r11_fixtures/` for XLSX/DOCX/HTML/ZIP/CSV/Q&A/master

## Honest limitation

Fewer than 10 distinct live buyer packages may be present on disk after retention/fat-trim. Validation reports the **maximum available real PDFs** and supplements with fixtures, labeling each project `REAL_PDF` vs `SYNTHETIC:…`.

## Metrics recorded per project

docs expected/found/parsed/failed · amendments · templates · requirements · material · unknown/review · missing refs · parse confidence · response/evaluation classification · provenance coverage · package status

## Aggregate artifacts

| File | Content |
|------|---------|
| `r11_production_intake_summary.json` | Verdict candidate + aggregates |
| `r11_real_corpus_results.json` | Per-project rows |
| `r11_document_parse_results.json` | Parse methods / OCR / templates |
| `r11_legacy_cutover_report.json` | Legacy disposition |
| `r11_missing_document_analysis.json` | Missing-ref cases |
