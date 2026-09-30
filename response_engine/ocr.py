"""R1.2 OCR fallback — local RapidOCR (or Tesseract if present). Native text first."""

from __future__ import annotations

import io
import re
from typing import Any

from application_clock import now_utc

OCR_ENGINE_VERSION = "r12-ocr-20260929-v1"
OCR_REVIEW_REQUIRED = "OCR_REVIEW_REQUIRED"

_MATERIAL_HINTS = re.compile(
    r"deadline|due\s+date|quantity|qty|signature|amendment|model|part\s*number|"
    r"delivery|submit|portal|piee|dibbs|certif|price|pricing|clin|sf\s*14|"
    r"shall\s+occur|must\s+be|required",
    re.I,
)

_ENGINE = None
_ENGINE_NAME: str | None = None
_ENGINE_ERROR: str | None = None


def _utc() -> str:
    return now_utc().isoformat()


def ocr_engine_status() -> dict[str, Any]:
    """Probe OCR availability without claiming success if unavailable."""
    global _ENGINE, _ENGINE_NAME, _ENGINE_ERROR
    if _ENGINE is not None:
        return {"available": True, "engine": _ENGINE_NAME, "version": OCR_ENGINE_VERSION}
    if _ENGINE_ERROR and _ENGINE is None and _ENGINE_NAME is None:
        # already probed and failed
        pass
    try:
        from rapidocr_onnxruntime import RapidOCR

        _ENGINE = RapidOCR()
        _ENGINE_NAME = "rapidocr_onnxruntime"
        _ENGINE_ERROR = None
        return {"available": True, "engine": _ENGINE_NAME, "version": OCR_ENGINE_VERSION}
    except Exception as exc:
        _ENGINE_ERROR = str(exc)[:300]
    # optional tesseract
    try:
        import shutil

        if shutil.which("tesseract"):
            import pytesseract  # type: ignore

            _ENGINE = ("tesseract", pytesseract)
            _ENGINE_NAME = "tesseract"
            _ENGINE_ERROR = None
            return {"available": True, "engine": _ENGINE_NAME, "version": OCR_ENGINE_VERSION}
    except Exception as exc:
        _ENGINE_ERROR = (_ENGINE_ERROR or "") + f"; tesseract:{exc}"[:200]
    return {
        "available": False,
        "engine": None,
        "version": OCR_ENGINE_VERSION,
        "error": _ENGINE_ERROR or "No local OCR engine available",
    }


def _ensure_engine():
    status = ocr_engine_status()
    if not status["available"]:
        raise RuntimeError(status.get("error") or "OCR unavailable")
    return _ENGINE, _ENGINE_NAME


def confidence_bucket(score: float | None) -> str:
    if score is None:
        return "MEDIUM"
    if score >= 0.85:
        return "HIGH"
    if score >= 0.55:
        return "MEDIUM"
    return "LOW"


def _ocr_image_array(arr) -> tuple[str, float | None, list[dict[str, Any]]]:
    engine, name = _ensure_engine()
    blocks: list[dict[str, Any]] = []
    if name == "rapidocr_onnxruntime":
        result, _ = engine(arr)
        texts = []
        scores = []
        for item in result or []:
            # [[box], text, score]
            box, text, score = item[0], item[1], float(item[2]) if len(item) > 2 else None
            texts.append(str(text))
            if score is not None:
                scores.append(score)
            blocks.append(
                {
                    "text": str(text),
                    "score": score,
                    "confidence": confidence_bucket(score),
                    "bbox": box,
                }
            )
        joined = "\n".join(texts).strip()
        avg = sum(scores) / len(scores) if scores else None
        return joined, avg, blocks
    if name == "tesseract":
        from PIL import Image
        import numpy as np

        _, tess = engine
        img = Image.fromarray(arr) if not hasattr(arr, "mode") else arr
        text = tess.image_to_string(img) or ""
        return text.strip(), 0.7, [{"text": text.strip(), "score": 0.7, "confidence": "MEDIUM", "bbox": None}]
    raise RuntimeError(f"Unknown OCR engine {name}")


def render_pdf_page_images(data: bytes, *, max_pages: int = 40, scale: float = 2.0) -> list[dict[str, Any]]:
    """Render PDF pages to RGB arrays for OCR. Prefers pypdfium2, then fitz."""
    pages: list[dict[str, Any]] = []
    try:
        import pypdfium2 as pdfium
        import numpy as np

        pdf = pdfium.PdfDocument(data)
        n = min(len(pdf), max_pages)
        for i in range(n):
            page = pdf[i]
            bitmap = page.render(scale=scale)
            pil = bitmap.to_pil()
            arr = __import__("numpy").array(pil.convert("RGB"))
            pages.append({"page": i + 1, "image": arr, "width": arr.shape[1], "height": arr.shape[0]})
        return pages
    except Exception:
        pass
    try:
        import fitz
        import numpy as np

        doc = fitz.open(stream=data, filetype="pdf")
        n = min(doc.page_count, max_pages)
        mat = fitz.Matrix(scale, scale)
        for i in range(n):
            page = doc.load_page(i)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
            pages.append({"page": i + 1, "image": arr, "width": pix.width, "height": pix.height})
        doc.close()
        return pages
    except Exception as exc:
        raise RuntimeError(f"PDF page render failed: {exc}") from exc


def page_needs_ocr(native_text: str, *, page_image_bytes_hint: int | None = None) -> bool:
    usable = len(re.sub(r"\s+", "", native_text or ""))
    if usable < 40:
        return True
    if usable < 80 and page_image_bytes_hint and page_image_bytes_hint > 50_000:
        return True
    return False


def ocr_pdf_bytes(
    data: bytes,
    *,
    document_id: str | None = None,
    filename: str | None = None,
    max_pages: int = 40,
    force_all_pages: bool = False,
    native_page_texts: list[str] | None = None,
) -> dict[str, Any]:
    """OCR scanned/sparse PDF pages. Native text first — OCR is fallback only."""
    status = ocr_engine_status()
    if not status["available"]:
        return {
            "ok": False,
            "parse_status": "OCR_ENGINE_UNAVAILABLE",
            "text": "",
            "pages": [],
            "engine": None,
            "error": status.get("error"),
            "ocr_engine_version": OCR_ENGINE_VERSION,
        }

    try:
        rendered = render_pdf_page_images(data, max_pages=max_pages)
    except Exception as exc:
        return {
            "ok": False,
            "parse_status": "OCR_RENDER_FAILED",
            "text": "",
            "pages": [],
            "engine": status["engine"],
            "error": str(exc)[:300],
            "ocr_engine_version": OCR_ENGINE_VERSION,
        }

    page_results: list[dict[str, Any]] = []
    texts: list[str] = []
    ocr_pages = 0
    native_pages = 0
    failures = 0
    high = med = low = 0

    for pr in rendered:
        page_no = pr["page"]
        native = ""
        if native_page_texts and page_no - 1 < len(native_page_texts):
            native = native_page_texts[page_no - 1] or ""
        need = force_all_pages or page_needs_ocr(native)
        if not need:
            native_pages += 1
            page_results.append(
                {
                    "page": page_no,
                    "method": "native",
                    "text": native,
                    "confidence": "HIGH",
                    "score": 1.0,
                    "blocks": [],
                    "ocr_used": False,
                }
            )
            if native:
                texts.append(native)
            continue
        try:
            text, avg, blocks = _ocr_image_array(pr["image"])
            bucket = confidence_bucket(avg)
            if bucket == "HIGH":
                high += 1
            elif bucket == "MEDIUM":
                med += 1
            else:
                low += 1
            ocr_pages += 1
            materialish = bool(_MATERIAL_HINTS.search(text or ""))
            review = bucket != "HIGH" and materialish
            page_results.append(
                {
                    "page": page_no,
                    "method": "OCR",
                    "text": text,
                    "confidence": bucket,
                    "score": avg,
                    "blocks": [
                        {
                            **b,
                            "document_id": document_id,
                            "page": page_no,
                            "extraction_method": "OCR",
                            "ocr_engine_version": OCR_ENGINE_VERSION,
                        }
                        for b in blocks
                    ],
                    "ocr_used": True,
                    "material_hints": materialish,
                    "review_required": review or bucket == "LOW",
                    "filename": filename,
                }
            )
            if text:
                texts.append(f"[OCR p.{page_no}] {text}")
        except Exception as exc:
            failures += 1
            page_results.append(
                {
                    "page": page_no,
                    "method": "OCR_FAILED",
                    "text": "",
                    "confidence": "UNUSABLE",
                    "error": str(exc)[:200],
                    "ocr_used": True,
                    "review_required": True,
                }
            )

    joined = "\n\n".join(texts).strip()
    overall = "HIGH"
    if low or failures:
        overall = "LOW"
    elif med:
        overall = "MEDIUM"
    elif not ocr_pages and not joined:
        overall = "UNUSABLE"

    return {
        "ok": bool(joined) or ocr_pages > 0,
        "parse_status": "OCR_FETCHED" if joined else ("OCR_EMPTY" if not failures else "OCR_FAILED"),
        "text": joined,
        "pages": page_results,
        "engine": status["engine"],
        "ocr_engine_version": OCR_ENGINE_VERSION,
        "stats": {
            "pages_rendered": len(rendered),
            "native_pages": native_pages,
            "ocr_pages": ocr_pages,
            "high": high,
            "medium": med,
            "low": low,
            "failures": failures,
            "review_gated_pages": sum(1 for p in page_results if p.get("review_required")),
        },
        "confidence": overall,
        "extracted_at": _utc(),
    }


def apply_ocr_human_correction(
    project: dict[str, Any],
    *,
    document_id: str,
    page: int,
    corrected_text: str,
    user: str = "owner",
    notes: str | None = None,
    action: str = "CORRECT",  # CONFIRM | CORRECT | MARK_UNREADABLE
) -> dict[str, Any]:
    """Store OCR correction history without overwriting audit trail."""
    docs = project.get("documents") or []
    doc = next((d for d in docs if d.get("document_id") == document_id), None)
    if not doc:
        return {"ok": False, "error": "document_not_found"}
    ocr_meta = doc.setdefault("ocr", {})
    pages = ocr_meta.setdefault("pages", [])
    page_rec = next((p for p in pages if int(p.get("page") or 0) == int(page)), None)
    if not page_rec:
        page_rec = {"page": int(page), "method": "OCR", "text": ""}
        pages.append(page_rec)
    history = page_rec.setdefault("correction_history", [])
    original = page_rec.get("corrected_text") or page_rec.get("text") or ""
    entry = {
        "at": _utc(),
        "user": user,
        "action": action,
        "original_ocr": page_rec.get("text"),
        "previous_text": original,
        "corrected_text": corrected_text if action != "MARK_UNREADABLE" else "",
        "notes": notes,
    }
    history.append(entry)
    if action == "MARK_UNREADABLE":
        page_rec["corrected_text"] = ""
        page_rec["confidence"] = "UNUSABLE"
        page_rec["review_required"] = True
        page_rec["unreadable"] = True
    elif action == "CONFIRM":
        page_rec["corrected_text"] = page_rec.get("text") or corrected_text
        page_rec["review_required"] = False
        page_rec["owner_confirmed"] = True
    else:
        page_rec["corrected_text"] = corrected_text
        page_rec["review_required"] = False
        page_rec["owner_corrected"] = True
    # Rebuild document text preferring corrections
    rebuilt = []
    for p in sorted(pages, key=lambda x: int(x.get("page") or 0)):
        t = p.get("corrected_text") if p.get("corrected_text") is not None and p.get("owner_corrected") or p.get("owner_confirmed") else None
        if t is None:
            t = p.get("corrected_text") or p.get("text") or ""
        if t:
            rebuilt.append(f"[OCR p.{p.get('page')}] {t}" if p.get("method") == "OCR" else t)
    if rebuilt:
        doc["text"] = "\n\n".join(rebuilt)
    ocr_meta["last_correction_at"] = _utc()
    project.setdefault("ocr_review_queue", [])
    # refresh queue entry
    project["ocr_review_queue"] = [
        q for q in project["ocr_review_queue"] if not (q.get("document_id") == document_id and int(q.get("page") or 0) == int(page))
    ]
    if page_rec.get("review_required"):
        project["ocr_review_queue"].append(
            {
                "document_id": document_id,
                "page": int(page),
                "filename": doc.get("filename"),
                "confidence": page_rec.get("confidence"),
                "excerpt": (page_rec.get("text") or "")[:200],
            }
        )
    return {"ok": True, "document_id": document_id, "page": page, "entry": entry}


def gate_ocr_requirements(project: dict[str, Any]) -> int:
    """Apply OCR confidence policy to material requirements. Returns gated count."""
    gated = 0
    docs = {d["document_id"]: d for d in project.get("documents") or []}
    for req in project.get("requirements") or []:
        if req.get("superseded"):
            continue
        src = docs.get(req.get("source_document_id") or "")
        if not src:
            continue
        ocr = src.get("ocr") or {}
        if not (src.get("ocr_required") or ocr.get("pages") or "OCR_REQUIRED" in (src.get("flags") or [])):
            continue
        # provenance confidence
        prov = req.get("provenance") or {}
        page = prov.get("source_page")
        page_conf = None
        for p in ocr.get("pages") or []:
            if page is not None and int(p.get("page") or 0) == int(page):
                page_conf = p.get("confidence")
                break
        conf = page_conf or src.get("extraction_confidence") or ocr.get("confidence") or "MEDIUM"
        material = req.get("materiality") == "MATERIAL" or req.get("mandatory")
        if not material:
            continue
        if conf == "HIGH":
            prov["extractor"] = prov.get("extractor") or "r12_ocr"
            prov["confidence"] = "HIGH"
            req["provenance"] = prov
            continue
        if conf == "MEDIUM":
            req["compliance_status"] = "REVIEW_REQUIRED"
            req["confidence"] = "MEDIUM"
            req["notes"] = (req.get("notes") or "") + " OCR MEDIUM — review required."
            prov["extractor"] = "r12_ocr"
            prov["confidence"] = "MEDIUM"
            req["provenance"] = prov
            gated += 1
        else:
            req["compliance_status"] = OCR_REVIEW_REQUIRED
            req["confidence"] = "LOW"
            req["notes"] = (req.get("notes") or "") + " OCR LOW — do not canonicalize material fact."
            prov["extractor"] = "r12_ocr"
            prov["confidence"] = "LOW"
            req["provenance"] = prov
            gated += 1
    return gated


def build_ocr_review_queue(project: dict[str, Any]) -> list[dict[str, Any]]:
    queue: list[dict[str, Any]] = []
    for d in project.get("documents") or []:
        for p in (d.get("ocr") or {}).get("pages") or []:
            if p.get("review_required") and not p.get("owner_confirmed") and not p.get("owner_corrected"):
                queue.append(
                    {
                        "document_id": d["document_id"],
                        "filename": d.get("filename") or d.get("original_filename"),
                        "page": p.get("page"),
                        "confidence": p.get("confidence"),
                        "excerpt": (p.get("text") or "")[:240],
                        "material_hints": p.get("material_hints"),
                    }
                )
    project["ocr_review_queue"] = queue
    return queue
