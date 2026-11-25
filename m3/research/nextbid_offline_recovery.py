"""Offline recovery of previously paid NextBid responses (zero API calls).

Repairs the two confirmed defects against every retained body:

1. ``read_file`` Markdown stored as ``raw_text`` is extracted via ``nextbid_file_text``.
2. ``list_documents`` attachment metadata under ``source_documents`` / ``nextbid_documents``
   is recovered (not only the often-empty ``documents[]``).

Writes a durable per-NB recovery package under the data root and thickens
``solicitation_full_text`` when recovered notice text is more complete. Never contacts NextBid.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from m3.config import get_settings
from m3.research.full_text_store import load_full_text, store_full_text
from m3.research.nextbid_acquire import nextbid_file_text
from m3.research.nextbid_response_store import store_root

QTY_RE = re.compile(
    r"(?i)\b(?:qty|quantity|quantities)\b|"
    r"\b\d[\d,]*(?:\.\d+)?\s*(?:ea|each|pcs?|units?|cases?|boxes|lbs?|tons?|gallons?)\b"
)
SPEC_RE = re.compile(
    r"(?i)\b(?:specification|shall\s+meet|brand\s+name\s+or\s+equal|nsn|part\s+number|manufacturer|model)\b"
)
LINE_RE = re.compile(r"(?i)\b(?:line\s+item|clin|item\s+no\.?|bid\s+item|schedule\s+of\s+items)\b")
SUBMIT_RE = re.compile(r"(?i)\b(?:submit|submission|bid\s+form|proposal\s+due|electronic\s+bid)\b")


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def recovery_root(root: Path | None = None) -> Path:
    p = (root or get_settings().data_root()) / "nextbid_offline_recovery"
    p.mkdir(parents=True, exist_ok=True)
    (p / "by_nb_id").mkdir(parents=True, exist_ok=True)
    return p


def _atomic_write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


def iter_paid_request_rows(paid_root: Path | None = None) -> list[dict[str, Any]]:
    """Load every retained paid-response body from ``by_request`` (complete population)."""
    root = (paid_root or store_root()) / "by_request"
    rows: list[dict[str, Any]] = []
    if not root.exists():
        return rows
    for path in sorted(root.glob("*.json")):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(row, dict):
            continue
        row["_store_path"] = str(path)
        rows.append(row)
    return rows


def _thicker_str(a: str | None, b: str | None) -> str:
    aa, bb = a or "", b or ""
    return bb if len(bb) > len(aa) else aa


def _merge_attachment_lists(*lists: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for lst in lists:
        for item in lst or []:
            if not isinstance(item, dict):
                continue
            key = str(
                item.get("sha256")
                or item.get("path")
                or item.get("document_url")
                or item.get("url")
                or item.get("source_url")
                or item.get("name")
                or item.get("filename")
                or json.dumps(item, sort_keys=True, default=str)[:120]
            )
            if key in seen:
                # Prefer entries that mark stored=true / have more fields
                for i, prev in enumerate(out):
                    pk = str(
                        prev.get("sha256") or prev.get("path") or prev.get("name") or prev.get("filename") or ""
                    )
                    if pk == key or (
                        prev.get("sha256") and prev.get("sha256") == item.get("sha256")
                    ):
                        if len(json.dumps(item, default=str)) > len(json.dumps(prev, default=str)):
                            out[i] = {**prev, **item}
                        break
                continue
            seen.add(key)
            out.append(dict(item))
    return out


def recover_nb_package(nb_id: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge all retained endpoint bodies for one NB into a recovery package."""
    by_ep: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_ep[str(r.get("endpoint") or "unknown")].append(r)

    description = ""
    summary = ""
    ai_summary = ""
    title = None
    agency = None
    solicitation_number = None
    source_url = None
    procurement_kind = None
    response_deadline = None
    notice_fields: dict[str, Any] = {}
    source_documents: list[dict[str, Any]] = []
    nextbid_documents: list[dict[str, Any]] = []
    documents_legacy: list[dict[str, Any]] = []
    file_index: list[dict[str, Any]] = []
    recovered_texts: list[dict[str, Any]] = []
    markdown_recovered = 0
    markdown_chars = 0
    search_descriptions: list[str] = []

    for r in by_ep.get("search") or []:
        body = r.get("body") or {}
        for item in body.get("results") or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("nb_id") or "") != nb_id:
                continue
            title = title or item.get("title")
            agency = agency or item.get("agency")
            solicitation_number = solicitation_number or item.get("solicitation_number")
            source_url = source_url or item.get("source_url")
            d = str(item.get("description") or item.get("summary") or "")
            if d:
                search_descriptions.append(d)
                description = _thicker_str(description, d)

    for r in by_ep.get("get_opportunity") or []:
        body = r.get("body") or {}
        opp = body.get("opportunity") if isinstance(body.get("opportunity"), dict) else body
        if not isinstance(opp, dict):
            continue
        title = title or opp.get("title")
        agency = agency or opp.get("agency")
        solicitation_number = solicitation_number or opp.get("solicitation_number")
        source_url = source_url or opp.get("source_url")
        procurement_kind = procurement_kind or opp.get("procurement_kind")
        response_deadline = response_deadline or opp.get("response_deadline")
        description = _thicker_str(description, str(opp.get("description") or ""))
        summary = _thicker_str(summary, str(opp.get("summary") or ""))
        ai_summary = _thicker_str(ai_summary, str(opp.get("ai_summary") or ""))
        documents_legacy = _merge_attachment_lists(documents_legacy, opp.get("documents") or [])
        for k in (
            "set_aside", "notice_type", "lifecycle_status", "document_count", "attachment_count",
            "document_set_status", "tradelines", "classification", "locator", "location",
            "contacts", "amendments", "bonding_required", "contract_type", "posted_date",
            "last_updated", "source",
        ):
            if opp.get(k) not in (None, "", [], {}) and notice_fields.get(k) in (None, "", [], {}):
                notice_fields[k] = opp.get(k)

    docs_meta: dict[str, Any] = {}
    for r in by_ep.get("list_documents") or []:
        body = r.get("body") or {}
        if not isinstance(body, dict):
            continue
        source_documents = _merge_attachment_lists(source_documents, body.get("source_documents") or [])
        nextbid_documents = _merge_attachment_lists(nextbid_documents, body.get("nextbid_documents") or [])
        documents_legacy = _merge_attachment_lists(
            documents_legacy,
            body.get("documents") or [],
            body.get("filed") or [],
            body.get("attachment_index") or [],
        )
        for k in (
            "source_documents_count", "originals_stored", "originals_available",
            "pipeline_artifacts", "downloads", "notes",
        ):
            if body.get(k) not in (None, "", [], {}):
                docs_meta[k] = body.get(k)

    for r in by_ep.get("list_files") or []:
        body = r.get("body") or {}
        files = body.get("files") or body.get("artifacts") or body.get("results") or []
        if isinstance(files, list):
            file_index = _merge_attachment_lists(file_index, [f for f in files if isinstance(f, dict)])

    # Prefer markdown document0 from nextbid_documents into file index
    file_index = _merge_attachment_lists(file_index, nextbid_documents)

    for r in by_ep.get("read_file") or []:
        body = r.get("body")
        path = str(r.get("path") or "")
        text = nextbid_file_text(body)
        # Detect previously ignored raw_text recovery
        ignored_before = False
        if isinstance(body, dict):
            classic = str(body.get("text") or body.get("content") or body.get("markdown") or body.get("data") or "")
            raw = str(body.get("raw_text") or "")
            if len(raw.strip()) > 80 and len(classic.strip()) < 40:
                ignored_before = True
                markdown_recovered += 1
                markdown_chars += len(raw)
        if text and len(text.strip()) > 80:
            recovered_texts.append({
                "path": path,
                "chars": len(text),
                "sha256": hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest(),
                "previously_ignored_raw_text": ignored_before,
                "kind_hint": next(
                    (k for k in ("document0", "requirements", "evidence", "normalized", "case")
                     if k in path.lower()),
                    None,
                ),
                "text": text,
            })

    # Build combined document_text: prefer longer recovered file texts (md over json sidecars)
    recovered_texts.sort(key=lambda t: (0 if str(t.get("path") or "").endswith(".md") else 1, -int(t["chars"])))
    doc_parts: list[str] = []
    seen_sha: set[str] = set()
    for t in recovered_texts:
        if t["sha256"] in seen_sha:
            continue
        seen_sha.add(t["sha256"])
        # Skip tiny JSON classification sidecars when markdown exists
        if str(t.get("path") or "").endswith(".json") and any(
            str(x.get("path") or "").endswith(".md") for x in recovered_texts
        ):
            continue
        doc_parts.append(t["text"])
    document_text = "\n\n".join(doc_parts).strip()

    # Local original bytes: only recovered text bodies we actually have; stored=true PDFs are metadata-only
    local_text_bodies = len(doc_parts)
    stored_original_refs = sum(1 for d in source_documents if d.get("stored") is True)
    local_pdf_bytes = 0  # we do not have PDF bytes offline unless separately saved

    blob_for_signals = f"{description}\n{document_text}"
    pkg = {
        "nb_id": nb_id,
        "recovered_at": _utc(),
        "endpoints_present": sorted(by_ep.keys()),
        "response_rows": sum(len(v) for v in by_ep.values()),
        "title": title,
        "agency": agency,
        "solicitation_number": solicitation_number,
        "source_url": source_url,
        "procurement_kind": procurement_kind,
        "response_deadline": response_deadline,
        "description": description or None,
        "summary": summary or None,
        "ai_summary": ai_summary or None,
        "description_chars": len(description or ""),
        "document_text": document_text or None,
        "document_text_chars": len(document_text or ""),
        "search_description_variants": len(search_descriptions),
        "notice_fields": notice_fields,
        "source_documents": source_documents,
        "nextbid_documents": nextbid_documents,
        "documents_legacy": documents_legacy,
        "file_index": file_index,
        "documents_meta": docs_meta,
        "recovered_file_texts": [
            {k: v for k, v in t.items() if k != "text"} | {"text_sha256": t["sha256"], "chars": t["chars"]}
            for t in recovered_texts
        ],
        "markdown_raw_text_recovered_count": markdown_recovered,
        "markdown_raw_text_chars": markdown_chars,
        "attachment_references_recovered": len(source_documents) + len(nextbid_documents) + len(documents_legacy),
        "source_documents_count": len(source_documents),
        "stored_original_refs_metadata_only": stored_original_refs,
        "local_original_text_bodies": local_text_bodies,
        "local_original_pdf_bytes_available": local_pdf_bytes,
        "signals": {
            "has_quantity_language": bool(QTY_RE.search(blob_for_signals)),
            "has_specification_language": bool(SPEC_RE.search(blob_for_signals)),
            "has_line_item_language": bool(LINE_RE.search(blob_for_signals)),
            "has_submission_language": bool(SUBMIT_RE.search(blob_for_signals)),
        },
        "request_ids": [r.get("request_id") for r in rows if r.get("request_id")],
    }
    return pkg


def run_offline_recovery(*, data_root: Path | None = None) -> dict[str, Any]:
    """Recover every retained paid NextBid response into per-NB packages + thicken full_text."""
    import os

    t0 = datetime.now(timezone.utc)
    data_root = data_root or get_settings().data_root()
    out_root = recovery_root(data_root)
    rows = iter_paid_request_rows()
    by_nb: dict[str, list[dict[str, Any]]] = defaultdict(list)
    no_nb = 0
    endpoint_counts = Counter()
    for r in rows:
        endpoint_counts[str(r.get("endpoint") or "unknown")] += 1
        nb = r.get("nb_id")
        if not nb and r.get("endpoint") == "search":
            # Search rows cover many NBs; attribute each result
            for item in ((r.get("body") or {}).get("results") or []):
                if isinstance(item, dict) and item.get("nb_id"):
                    # Lightweight pointer row for search membership
                    by_nb[str(item["nb_id"])].append({
                        **{k: r.get(k) for k in ("request_id", "endpoint", "path", "status_code", "retrieved_at")},
                        "nb_id": item["nb_id"],
                        "body": {"results": [item]},
                        "_from_search_slice": True,
                        "_store_path": r.get("_store_path"),
                    })
            continue
        if not nb:
            no_nb += 1
            continue
        by_nb[str(nb)].append(r)

    stats = Counter()
    stats["paid_request_files_inspected"] = len(rows)
    stats["unique_nb_ids"] = len(by_nb)
    stats["rows_without_nb_id"] = no_nb
    packages: list[dict[str, Any]] = []

    for nb_id, nb_rows in sorted(by_nb.items()):
        pkg = recover_nb_package(nb_id, nb_rows)
        packages.append(pkg)
        stats["packages_written"] += 1
        stats["markdown_bodies_recovered"] += int(pkg["markdown_raw_text_recovered_count"])
        stats["markdown_chars_recovered"] += int(pkg["markdown_raw_text_chars"])
        stats["attachment_refs_recovered"] += int(pkg["attachment_references_recovered"])
        stats["source_documents_refs"] += int(pkg["source_documents_count"])
        stats["local_text_bodies"] += int(pkg["local_original_text_bodies"])
        stats["description_chars_total"] += int(pkg["description_chars"])
        stats["document_text_chars_total"] += int(pkg["document_text_chars"])
        if pkg["description_chars"] > 0:
            stats["nbs_with_description"] += 1
        if pkg["document_text_chars"] > 0:
            stats["nbs_with_document_text"] += 1
        if pkg["signals"]["has_quantity_language"]:
            stats["nbs_with_qty_language"] += 1
        if pkg["signals"]["has_specification_language"]:
            stats["nbs_with_spec_language"] += 1
        if pkg["signals"]["has_line_item_language"]:
            stats["nbs_with_line_language"] += 1
        if pkg["signals"]["has_submission_language"]:
            stats["nbs_with_submission_language"] += 1
        if pkg["stored_original_refs_metadata_only"]:
            stats["nbs_with_stored_original_metadata"] += 1

        # Persist package (omit giant duplicate of file texts already hashed — keep text for replay)
        slim = dict(pkg)
        # Keep document_text in package for gather_original_evidence
        _atomic_write_json(out_root / "by_nb_id" / f"{nb_id}.json", slim)

        # Thicken solicitation_full_text when we recovered more complete notice text
        recovered = str(pkg.get("document_text") or "")
        if len(recovered) >= 200:
            prior = load_full_text(nb_id) or {}
            prior_text = str(prior.get("full_text") or "")
            if len(recovered) > len(prior_text):
                res = store_full_text(
                    nb_id,
                    recovered,
                    source="nextbid_offline_recovery_raw_text",
                    title=pkg.get("title"),
                )
                if not res.get("skipped_thinner"):
                    stats["full_text_store_updated"] += 1
                else:
                    stats["full_text_store_kept_thicker"] += 1

    # Manifest
    manifest = {
        "ran_at": _utc(),
        "elapsed_sec": round((datetime.now(timezone.utc) - t0).total_seconds(), 2),
        "external_calls": 0,
        "endpoint_counts": dict(endpoint_counts),
        "stats": dict(stats),
        "output_root": str(out_root),
    }
    _atomic_write_json(out_root / "RECOVERY_MANIFEST.json", manifest)
    # Index for quick load
    index = {
        nb: {
            "description_chars": p["description_chars"],
            "document_text_chars": p["document_text_chars"],
            "source_documents": p["source_documents_count"],
            "markdown_recovered": p["markdown_raw_text_recovered_count"],
            "endpoints": p["endpoints_present"],
        }
        for nb, p in ((x["nb_id"], x) for x in packages)
    }
    _atomic_write_json(out_root / "index.json", index)
    return manifest


def load_recovery_package(nb_id: str, *, data_root: Path | None = None) -> dict[str, Any] | None:
    path = recovery_root(data_root) / "by_nb_id" / f"{nb_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def apply_recovery_to_evidence(
    nb_id: str,
    *,
    original: dict[str, Any],
    doc_text: str,
    sources_used: list[str],
    cache_refs: list[str],
    data_root: Path | None = None,
) -> tuple[dict[str, Any], str]:
    """Merge offline recovery package into gather_original_evidence fields (thicker wins)."""
    pkg = load_recovery_package(nb_id, data_root=data_root)
    if not pkg:
        return original, doc_text

    sources_used.append("nextbid_offline_recovery")
    cache_refs.append(str(recovery_root(data_root) / "by_nb_id" / f"{nb_id}.json"))

    for k in (
        "title", "agency", "solicitation_number", "source_url", "procurement_kind",
        "response_deadline", "description", "summary", "ai_summary",
    ):
        v = pkg.get(k)
        if isinstance(v, str) and len(v) > len(str(original.get(k) or "")):
            original[k] = v
        elif v not in (None, "", [], {}) and original.get(k) in (None, "", [], {}):
            original[k] = v

    for k, v in (pkg.get("notice_fields") or {}).items():
        if v not in (None, "", [], {}) and original.get(k) in (None, "", [], {}):
            original[k] = v

    # Prefer recovered document_text when thicker than current store
    recovered_doc = str(pkg.get("document_text") or "")
    if len(recovered_doc) > len(doc_text or ""):
        doc_text = recovered_doc

    # Merge attachment metadata — never drop source_documents
    att = []
    for key in ("attachments", "documents", "document_list", "source_documents", "nextbid_documents"):
        cur = original.get(key)
        if isinstance(cur, list):
            att.extend(cur)
    att = _merge_attachment_lists(
        att,
        pkg.get("source_documents") or [],
        pkg.get("nextbid_documents") or [],
        pkg.get("documents_legacy") or [],
        pkg.get("file_index") or [],
    )
    if att:
        original["attachments"] = att
        original["documents"] = att
        original["source_documents"] = pkg.get("source_documents") or original.get("source_documents") or []
        original["nextbid_documents"] = pkg.get("nextbid_documents") or original.get("nextbid_documents") or []
    original["offline_recovery"] = {
        "markdown_raw_text_recovered_count": pkg.get("markdown_raw_text_recovered_count"),
        "document_text_chars": pkg.get("document_text_chars"),
        "source_documents_count": pkg.get("source_documents_count"),
        "endpoints_present": pkg.get("endpoints_present"),
        "local_original_text_bodies": pkg.get("local_original_text_bodies"),
        "stored_original_refs_metadata_only": pkg.get("stored_original_refs_metadata_only"),
        "local_original_pdf_bytes_available": pkg.get("local_original_pdf_bytes_available"),
    }
    return original, doc_text
