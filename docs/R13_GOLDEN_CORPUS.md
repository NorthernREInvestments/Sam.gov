# R1.3 — Golden Corpus

## Selection

Up to 20 diverse non-support projects with documents are marked `GOLDEN_CORPUS` on the real corpus manifest. Prefer multi-tag packages covering federal/DLA/Nebraska/SF1449/XLSX/Q&A/ZIP/OCR.

## Immutability

Golden source files are hash-locked. `tests/test_response_engine_r13.py` verifies SHA-256 of stored files against the manifest. Replace only by versioning a new file and updating the manifest deliberately — never silent overwrite.

## Expectations stored

Per golden project (in baseline/golden artifacts):

- document count / file types
- response type / evaluation / product mode (as classified)
- material requirement counts
- package completeness / false-complete flag
- provenance coverage
- firewall cleanliness

## Regression command

```bash
python -m pytest tests/test_response_engine_r13.py -q
python scripts/run_r13_corpus_acquisition_validation.py
```

Full re-acquire+validate rewrites `artifacts/response_engine/r13_*.json` and re-marks golden tags.
