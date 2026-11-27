"""Every-word document reading via contiguous chunks + parallel OpenAI calls.

Coverage invariant: concatenated chunk texts == full extracted text.
Parallelism speeds IO/API only — no chunk is dropped.
"""
from __future__ import annotations

import hashlib
import io
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from m3.config import get_settings
from m3.research.budget import estimate_response_cost
from m3.research.openai_client import _extract_usage, _output_text, require_json_object
from m3.research.parallel_work import DEFAULT_OPENAI_WORKERS, map_parallel

CHUNK_CHARS = 50_000


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
    from pdfminer.high_level import extract_text
    from pdfminer.pdfpage import PDFPage
    from m3.research.extract_fields import ocr_pdf_bytes

    with io.BytesIO(data) as bio:
        page_count = sum(1 for _ in PDFPage.get_pages(bio))
    pages_out: list[dict[str, Any]] = []
    for i in range(page_count):
        text = extract_text(io.BytesIO(data), page_numbers=[i]) or ""
        pages_out.append({"page": i + 1, "text": text, "method": "pdfminer", "chars": len(text)})
    thin = sum(1 for p in pages_out if len(p["text"].strip()) < 40)
    if page_count and thin / page_count >= 0.5:
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
    return pages_out


def extract_file_units(path: Path) -> list[dict[str, Any]]:
    from m3.research.documents import extract_docx_text

    data = path.read_bytes()
    name = path.name
    if data[:4] == b"%PDF" or name.lower().endswith(".pdf"):
        pages = extract_pdf_pages(data)
        return [{"file": name, "unit": f"page_{p['page']}", **p} for p in pages]
    if name.lower().endswith(".docx"):
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
    t = data.decode("utf-8", errors="replace")
    return [{"file": name, "unit": "text_full", "page": 1, "text": t, "method": "utf8", "chars": len(t)}]


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
                }
            )
        full_text = "".join(parts)
        total_chars += len(full_text)
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
        "JSON keys: nb_id, title_verified, documents_belong_to_solicitation, "
        "want_type, decision, what_they_want, quantity, close_date, "
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
