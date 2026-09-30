# R1.2 OCR Pipeline

**Module:** `response_engine/ocr.py`  
**Engine:** `rapidocr_onnxruntime` (local ONNX; optional Tesseract if on PATH)

## Policy

1. Native PDF text first  
2. OCR only when page/document text sparse/missing  
3. Page-level provenance: document_id, page, method=OCR, score, bbox when available  
4. Material MEDIUM → `REVIEW_REQUIRED`; LOW → `OCR_REVIEW_REQUIRED`  
5. Owner Confirm / Correct / Mark unreadable with correction history  

## APIs

- `GET/POST /api/response-projects/{id}/ocr-review`
