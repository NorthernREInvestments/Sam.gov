# R1.1 File Parsing

**Module:** `response_engine/parsers.py`  
**Parser version:** `r11-20260929-v1`

## Reuse

| Capability | Source |
|------------|--------|
| PDF text | `pdf_text.extract_pdf_text` via `document_ingestion` |
| Tables / XLSX / DOCX / CSV | `table_extractors` |
| ZIP safety | `safe_inspect_zip` (path traversal bounds, size caps) |

## Pipeline

1. Content-type mismatch reject (e.g. HTML login page named `.pdf`)
2. Password-protected Office detect → `PASSWORD_PROTECTED_DOCUMENT`
3. Native extract by type
4. PDF scan detect → `OCR_REQUIRED` (flag only; no tesseract required in R1.1)
5. Cache by `sha256 + parser_version`

## Supported types

PDF, DOCX, XLSX/XLS/XLSM, CSV, HTML, TXT, ZIP (members exploded into graph).

## Spreadsheets

Buyer workbooks store **original bytes unchanged**. Structure includes sheet names, sample cell addresses, `modified: false`. Classified e.g. `PRICING_TEMPLATE`. No autofill in R1.1.

## Confidence

| Method | Default confidence |
|--------|-------------------|
| Native PDF / DOCX / clean XLSX | HIGH–MEDIUM |
| OCR / scan detected | LOW → material reqs `REVIEW_REQUIRED` |
| Mismatch / corrupt | UNUSABLE |

## Security

Read-only. No macro execution. Filenames sanitized. Archive extraction bounded.
