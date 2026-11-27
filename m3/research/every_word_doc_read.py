"""Every-word document reading via contiguous chunks + parallel OpenAI calls.

Coverage invariant: concatenated chunk texts == full extracted text.
Parallelism speeds IO/API only — no chunk is dropped.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

from m3.config import get_settings
from m3.research.budget import estimate_response_cost
from m3.research.openai_client import _extract_usage, _output_text, require_json_object
from m3.research.parallel_work import DEFAULT_OPENAI_WORKERS, map_parallel

# Larger chunks → fewer OpenAI round-trips per file (still full coverage).
CHUNK_CHARS = 80_000

# When a package has many files, prefer primary solicitation docs first.
_PRIMARY_DOC_NAME_RE = re.compile(
    r"(?i)("
    r"\brfp\b|\bifb\b|\bitb\b|\brfq\b|\brfi\b|"
    r"solicitation|invitation|bid\s*docs?|specifications?|"
    r"cost\s*proposal|pricing|quote\s*sheet|bid\s*form|"
    r"scope|requirements|addendum|amendment"
    r")"
)
_LOW_VALUE_DOC_NAME_RE = re.compile(
    r"(?i)("
    r"questions?\s*and\s*responses?|q\s*&\s*a|pre[\s-]?bid\s*slide|"
    r"board\s*recommend|bid\s*tab(?:ulation)?|tab\s*sheet|"
    r"attendance|sign[\s-]?in|agenda"
    r")"
)

# Claims/HIPAA/PHI packets — never send to OpenAI (cost + sensitivity; not product specs).
_SENSITIVE_DOC_NAME_RE = re.compile(
    r"(?i)("
    r"claims?\s*data|"
    r"rx\s*claims|"
    r"claims?\s+history|"
    r"claims?\s+summary|"
    r"hipaa|"
    r"\bphi\b|"
    r"business\s+associate|"
    r"member\s+amount|"
    r"enrollment\s+\d{4}|"
    r"network\s+disruption|"
    r"formulary\s+disruption|"
    r"plan\s+exclusions|"
    r"834\s+guidance"
    r")"
)


def is_sensitive_non_product_doc(name: str) -> bool:
    """True for claims/HIPAA/PHI-style attachments that must not go to OpenAI."""
    return bool(_SENSITIVE_DOC_NAME_RE.search(name or ""))


def contiguous_chunks(text: str, size: int = CHUNK_CHARS) -> list[tuple[int, int, str]]:
    if not text:
        return [(0, 0, "")]
    out: list[tuple[int, int, str]] = []
    i = 0
    n = len(text)
    while i < n:
        j = min(n, i + size)
        out.append((i, j, text[i:j]))
        i = j
    if "".join(c[2] for c in out) != text:
        raise RuntimeError("chunk_coverage_mismatch")
    return out


def extract_pdf_pages(data: bytes) -> list[dict[str, Any]]:
    """Page-level PDF text. Never raise PSEOF/corrupt-PDF into the source runner."""
    from pdfminer.high_level import extract_text
    from pdfminer.pdfpage import PDFPage
    from m3.research.extract_fields import ocr_pdf_bytes

    pages_out: list[dict[str, Any]] = []
    page_count = 0
    try:
        with io.BytesIO(data) as bio:
            page_count = sum(1 for _ in PDFPage.get_pages(bio))
        for i in range(page_count):
            try:
                text = extract_text(io.BytesIO(data), page_numbers=[i]) or ""
            except Exception as page_exc:  # noqa: BLE001
                pages_out.append(
                    {
                        "page": i + 1,
                        "text": "",
                        "method": "pdfminer_page_error",
                        "chars": 0,
                        "error": f"{type(page_exc).__name__}",
                    }
                )
                continue
            pages_out.append({"page": i + 1, "text": text, "method": "pdfminer", "chars": len(text)})
    except Exception as exc:  # noqa: BLE001
        # Truncated/corrupt PDFs (PSEOF:Unexpected EOF) — try OCR whole file, else empty.
        try:
            ocr = ocr_pdf_bytes(data, max_pages=8)
            if ocr.get("ok") and ocr.get("text"):
                t = str(ocr.get("text") or "")
                return [
                    {
                        "page": 1,
                        "text": t,
                        "method": "ocr_after_pdfminer_error",
                        "chars": len(t),
                        "pages_ocrd": ocr.get("pages_ocrd"),
                        "pdfminer_error": f"{type(exc).__name__}:{exc}"[:160],
                    }
                ]
        except Exception:
            pass
        return [
            {
                "page": 1,
                "text": "",
                "method": "pdf_unreadable",
                "chars": 0,
                "error": f"{type(exc).__name__}:{exc}"[:160],
            }
        ]

    thin = sum(1 for p in pages_out if len(p["text"].strip()) < 40)
    if page_count and thin / page_count >= 0.5:
        try:
            ocr = ocr_pdf_bytes(data, max_pages=page_count)
            if ocr.get("ok") and ocr.get("text"):
                t = str(ocr.get("text") or "")
                return [
                    {
                        "page": 1,
                        "text": t,
                        "method": "ocr_all_pages",
                        "chars": len(t),
                        "pages_ocrd": ocr.get("pages_ocrd") or page_count,
                        "page_count": page_count,
                    }
                ]
        except Exception:
            pass
    return pages_out


def extract_file_units(path: Path) -> list[dict[str, Any]]:
    from m3.research.documents import extract_docx_text, extract_xls_text, extract_xlsx_text

    data = path.read_bytes()
    name = path.name
    low = name.lower()
    if is_sensitive_non_product_doc(name):
        return [
            {
                "file": name,
                "unit": "skipped_sensitive",
                "page": 1,
                "text": "",
                "method": "skipped_sensitive_not_sent_to_openai",
                "chars": 0,
                "extract_ok": False,
                "skipped_sensitive": True,
                "skip_reason": "claims_hipaa_phi_or_member_data",
            }
        ]
    if data[:4] == b"%PDF" or low.endswith(".pdf"):
        pages = extract_pdf_pages(data)
        return [{"file": name, "unit": f"page_{p['page']}", **p} for p in pages]
    if low.endswith(".docx"):
        ex = extract_docx_text(data)
        t = str(ex.get("text") or "")
        return [
            {
                "file": name,
                "unit": "docx_full",
                "page": 1,
                "text": t,
                "method": "python-docx",
                "chars": len(t),
                "extract_ok": bool(ex.get("ok")),
            }
        ]
    if low.endswith((".xlsx", ".xlsm")) or data[:2] == b"PK" and low.endswith(".xlsx"):
        ex = extract_xlsx_text(data)
        t = str(ex.get("text") or "")
        return [
            {
                "file": name,
                "unit": "xlsx_full",
                "page": 1,
                "text": t,
                "method": "openpyxl",
                "chars": len(t),
                "extract_ok": bool(ex.get("ok")),
                "extract_reason": ex.get("reason"),
            }
        ]
    if low.endswith(".xls") or data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        ex = extract_xls_text(data)
        t = str(ex.get("text") or "")
        return [
            {
                "file": name,
                "unit": "xls_full",
                "page": 1,
                "text": t,
                "method": "xlrd",
                "chars": len(t),
                "extract_ok": bool(ex.get("ok")),
                "extract_reason": ex.get("reason"),
            }
        ]
    if low.endswith((".csv", ".txt", ".md", ".json", ".html", ".htm")):
        t = data.decode("utf-8", errors="replace")
        return [
            {
                "file": name,
                "unit": "text_full",
                "page": 1,
                "text": t,
                "method": "utf8",
                "chars": len(t),
            }
        ]
    # Binary / unknown — never utf8-decode Office/ZIP guts into OpenAI.
    return [
        {
            "file": name,
            "unit": "skipped_binary",
            "page": 1,
            "text": "",
            "method": "skipped_unsupported_binary",
            "chars": 0,
            "extract_ok": False,
            "skip_reason": f"unsupported_extension:{Path(name).suffix or 'none'}",
        }
    ]


def _ask_json(client: Any, model: str, prompt: str) -> tuple[dict[str, Any], Decimal]:
    last = ""
    spent = Decimal("0")
    err = "invalid_or_non_json"
    for attempt in range(1, 3):
        user = prompt if attempt == 1 else (
            "INVALID JSON before. Return one valid JSON object only.\n"
            f"Bad:\n{last[:2000]}\n\n{prompt[:90000]}"
        )
        try:
            resp = client.responses.create(
                model=model,
                input=[
                    {
                        "role": "system",
                        "content": (
                            "You read solicitation document text verbatim. "
                            "Never invent. Never skip. Return one JSON object only."
                        ),
                    },
                    {"role": "user", "content": user},
                ],
                text={"format": {"type": "json_object"}},
            )
        except Exception:
            resp = client.responses.create(
                model=model,
                input=[
                    {"role": "system", "content": "Return one JSON object only."},
                    {"role": "user", "content": user},
                ],
            )
        usage = _extract_usage(resp)
        spent += estimate_response_cost(
            model=model,
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            web_search_calls=0,
        )
        last = _output_text(resp)
        ok, parsed, err = require_json_object(last)
        if ok and parsed is not None:
            return parsed, spent
    raise RuntimeError(f"json_parse_failed_after_retry:{err}:{last[:300]}")


def read_every_word_for_nb(
    *,
    nb_id: str,
    title: str,
    originals_dir: Path,
    max_workers: int = DEFAULT_OPENAI_WORKERS,
    chunk_chars: int = CHUNK_CHARS,
) -> dict[str, Any]:
    """Read every character of every local original under originals_dir/nb_id."""
    from openai import OpenAI

    settings = get_settings()
    client = OpenAI(api_key=(settings.openai_api_key or "").strip())
    model = settings.openai_default_model or "gpt-4o-mini"

    orig = Path(originals_dir) / nb_id
    if not orig.exists():
        return {"ok": False, "nb_id": nb_id, "status": "NO_LOCAL_ORIGINALS", "title": title}
    files = [p for p in sorted(orig.iterdir()) if p.is_file() and not p.name.endswith(".meta.json")]
    if not files:
        return {"ok": False, "nb_id": nb_id, "status": "NO_LOCAL_ORIGINALS", "title": title}
    # Speed: if package is large, read primary solicitation/cost docs first; skip
    # low-value Q&A/tab sheets unless they are the only files.
    if len(files) > 6:
        primary = [p for p in files if _PRIMARY_DOC_NAME_RE.search(p.name)]
        if primary:
            files = [
                p
                for p in primary
                if not _LOW_VALUE_DOC_NAME_RE.search(p.name)
            ] or primary
        else:
            files = [p for p in files if not _LOW_VALUE_DOC_NAME_RE.search(p.name)] or files
        # Cap extreme packages (still every word of each selected file).
        files = sorted(files, key=lambda p: (0 if _PRIMARY_DOC_NAME_RE.search(p.name) else 1, p.stat().st_size))[:12]
    for p in files:
        if nb_id not in str(p.resolve()):
            raise RuntimeError(f"file_not_under_nb_id:{p}")

    spent = Decimal("0")
    file_reports: list[dict[str, Any]] = []
    all_chunk_findings: list[dict[str, Any]] = []
    total_chars = 0
    total_chunks = 0

    # Build every file's chunks first, then run ALL chunks in one parallel pool
    # (up to max_workers) so multi-file packages don't serialize file-by-file.
    pending_jobs: list[dict[str, Any]] = []
    file_meta: list[dict[str, Any]] = []

    for path in files:
        units = extract_file_units(path)
        parts: list[str] = []
        page_map: list[dict[str, Any]] = []
        skipped = bool(units and (units[0].get("skipped_sensitive") or units[0].get("method", "").startswith("skipped_")))
        for u in units:
            t = u.get("text") or ""
            start = sum(len(x) for x in parts)
            parts.append(t)
            page_map.append(
                {
                    "unit": u.get("unit"),
                    "page": u.get("page"),
                    "method": u.get("method"),
                    "chars": len(t),
                    "char_start": start,
                    "char_end": start + len(t),
                    "skipped_sensitive": u.get("skipped_sensitive"),
                    "skip_reason": u.get("skip_reason"),
                }
            )
        full_text = "".join(parts)
        total_chars += len(full_text)
        # Sensitive / unsupported binary: record the file, do NOT buy OpenAI chunks.
        if skipped or not full_text.strip():
            file_meta.append(
                {
                    "file": path.name,
                    "path": path,
                    "bytes": path.stat().st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "units": page_map,
                    "full_text_chars": len(full_text),
                    "chunks": 0,
                    "coverage_sha256": hashlib.sha256(full_text.encode("utf-8", errors="replace")).hexdigest(),
                    "coverage_ok": True,
                    "chunk_findings": [],
                    "openai_skipped": True,
                    "skip_reason": (units[0].get("skip_reason") if units else None)
                    or ("empty_extract" if not full_text.strip() else "skipped"),
                }
            )
            continue

        chunks = contiguous_chunks(full_text, chunk_chars)
        total_chunks += len(chunks)
        if "".join(c[2] for c in chunks) != full_text:
            raise RuntimeError(f"coverage_break:{nb_id}:{path.name}")

        file_idx = len(file_meta)
        file_meta.append(
            {
                "file": path.name,
                "path": path,
                "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "units": page_map,
                "full_text_chars": len(full_text),
                "chunks": len(chunks),
                "coverage_sha256": hashlib.sha256(full_text.encode("utf-8", errors="replace")).hexdigest(),
                "coverage_ok": True,
                "chunk_findings": [],
            }
        )
        for idx, (start, end, piece) in enumerate(chunks, 1):
            pending_jobs.append(
                {
                    "file_idx": file_idx,
                    "file": path.name,
                    "idx": idx,
                    "chunks_total": len(chunks),
                    "start": start,
                    "end": end,
                    "full_len": len(full_text),
                    "piece": piece,
                }
            )

    def _one(job: dict[str, Any]) -> dict[str, Any]:
        parsed, cost = _ask_json(
            client,
            model,
            f"NB_ID: {nb_id}\nEXPECTED_TITLE: {title}\n"
            f"FILE: {job['file']}\nCHUNK: {job['idx']} of {job['chunks_total']}\n"
            f"CHAR_RANGE: [{job['start']}, {job['end']}) of {job['full_len']}\n"
            "You must read EVERY WORD in CHUNK_TEXT below. "
            "Confirm documents_belong_to_solicitation for this solicitation. "
            "Extract facts FROM THIS CHUNK ONLY.\n"
            "JSON keys: nb_id, file, chunk_index, chunks_total, char_start, char_end, "
            "read_complete (must be true), documents_belong_to_solicitation, "
            "facts_from_this_chunk, quantities, dates, requirements, concerns.\n\n"
            f"CHUNK_TEXT:\n{job['piece']}",
        )
        parsed["_cost_usd"] = float(cost)
        parsed["_file_idx"] = job["file_idx"]
        if not parsed.get("read_complete"):
            raise RuntimeError(
                f"model_did_not_confirm_read_complete:{nb_id}:{job['file']}:{job['idx']}"
            )
        return parsed

    # No readable non-sensitive text → do not buy a final OpenAI judgment.
    if not pending_jobs:
        for meta in file_meta:
            meta.pop("path", None)
            file_reports.append(meta)
        return {
            "ok": True,
            "nb_id": nb_id,
            "title": title,
            "status": "NO_READABLE_NON_SENSITIVE_TEXT",
            "total_chars_read": total_chars,
            "total_chunks_read": 0,
            "files": file_reports,
            "final": {
                "nb_id": nb_id,
                "want_type": "UNCLEAR",
                "decision": "UNCLEAR",
                "what_they_want": None,
                "supplier_quote_ready": False,
                "why": "no_readable_non_sensitive_document_text",
                "next_step": "SKIP",
            },
            "openai_spent_usd": 0.0,
            "max_workers": max_workers,
        }

    chunk_findings = (
        map_parallel(_one, pending_jobs, max_workers=max_workers, ordered=True)
        if pending_jobs
        else []
    )
    for cf in chunk_findings:
        spent += Decimal(str(cf.pop("_cost_usd", 0)))
        fidx = int(cf.pop("_file_idx"))
        file_meta[fidx]["chunk_findings"].append(cf)
        all_chunk_findings.append({**cf, "file": file_meta[fidx]["file"]})

    for meta in file_meta:
        meta.pop("path", None)
        file_reports.append(meta)

    final, cost = _ask_json(
        client,
        model,
        f"NB_ID: {nb_id}\nEXPECTED_TITLE: {title}\n"
        "All document chunks were read (every character covered). "
        "Using ONLY chunk findings below, produce final judgment.\n"
        "M3 scope: tangible non-perishable goods only. "
        "If this solicitation is perishable food / grocery / milk / bread / produce / "
        "meat / dairy / frozen food commodities, set want_type=SERVICE (out of scope), "
        "supplier_quote_ready=false, next_step=SKIP. "
        "Food-service equipment or non-food supplies may still be PRODUCT.\n"
        "what_they_want must name the product(s) being purchased in plain English "
        "(e.g. '15 3-inch ball valves' or 'Multiple parts listing — filters and hoses'). "
        "product_category must be exactly one of: furniture, janitorial, it_hardware, "
        "electrical, automotive, medical, safety, apparel, appliances, fuel, "
        "building_materials, plumbing, fleet_equipment, industrial_equipment, "
        "chemicals, office_supplies, general_products.\n"
        "JSON keys: nb_id, title_verified, documents_belong_to_solicitation, "
        "want_type, decision, what_they_want, quantity, close_date, product_category, "
        "key_requirements, missing_info, supplier_quote_ready, why, next_step, "
        "files_read, total_chunks_read.\n\n"
        f"CHUNK_FINDINGS:\n{json.dumps(all_chunk_findings, indent=2)[:200000]}",
    )
    spent += cost

    return {
        "ok": True,
        "nb_id": nb_id,
        "title": title,
        "status": "EVERY_WORD_READ",
        "total_chars_read": total_chars,
        "total_chunks_read": total_chunks,
        "files": file_reports,
        "final": final,
        "openai_spent_usd": float(spent),
        "max_workers": max_workers,
    }
