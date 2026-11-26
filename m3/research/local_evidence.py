"""Assemble ALL locally retained solicitation evidence before analysis.

Never treat solicitation_full_text.full_text alone as the complete package.
Never uses network / API. Distinguishes document content vs attachment metadata.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from m3.config import get_settings
from m3.research.full_text_store import load_full_text

_LISTING_MAX = 500


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _add_doc(
    docs: list[dict[str, Any]],
    *,
    doc_id: str,
    kind: str,
    source_path: str,
    text: str,
    is_metadata_only: bool = False,
    page_refs: list[str] | None = None,
    content_kind: str = "text",
) -> None:
    body = (text or "").replace("\x00", "")
    docs.append(
        {
            "doc_id": doc_id,
            "kind": kind,
            "source_path": source_path,
            "chars": len(body),
            "is_metadata_only": bool(is_metadata_only),
            "is_listing_preview": (not is_metadata_only) and 0 < len(body.strip()) <= _LISTING_MAX and kind in {
                "listing_summary",
                "full_text_store_thin",
            },
            "page_refs": page_refs or [],
            "content_kind": content_kind,
            "text": body,
        }
    )


def _load_paid_texts(nb_id: str) -> list[dict[str, Any]]:
    from m3.research.nextbid_response_store import load_by_request_id, store_root

    out: list[dict[str, Any]] = []
    idx = store_root() / "by_nb_id" / f"{nb_id}.jsonl"
    # legacy wrong extension still checked
    legacy = store_root() / "by_nb_id" / f"{nb_id}.json"
    request_ids: list[str] = []
    if idx.exists():
        for line in idx.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            rid = row.get("request_id")
            if rid:
                request_ids.append(str(rid))
    if legacy.exists() and not request_ids:
        try:
            blob = json.loads(legacy.read_text(encoding="utf-8"))
            if isinstance(blob, dict) and blob.get("request_id"):
                request_ids.append(str(blob["request_id"]))
            elif isinstance(blob, dict):
                # entire body may be inline
                body = blob.get("body") if isinstance(blob.get("body"), dict) else blob
                text = ""
                if isinstance(body, dict):
                    text = str(
                        body.get("raw_text")
                        or body.get("description")
                        or (body.get("opportunity") or {}).get("description")
                        or ""
                    )
                if text:
                    out.append(
                        {
                            "doc_id": f"paid_legacy:{nb_id}",
                            "kind": "paid_response_body",
                            "source_path": str(legacy),
                            "text": text,
                            "endpoint": blob.get("endpoint"),
                        }
                    )
        except Exception:
            pass
    for rid in request_ids:
        row = load_by_request_id(rid)
        if not row:
            continue
        body = row.get("body")
        text = ""
        endpoint = str(row.get("endpoint") or "")
        if isinstance(body, dict):
            # list_documents / list_files → inventory metadata ONLY (never as solicitation body)
            if endpoint in {"list_documents", "list_files"}:
                from m3.research.document_inventory import extract_document_entries

                entries = extract_document_entries(body)
                out.append(
                    {
                        "doc_id": f"paid_inventory:{endpoint}:{rid[:12]}",
                        "kind": "attachment_metadata",
                        "source_path": str(store_root() / "by_request" / f"{rid}.json"),
                        "text": json.dumps(
                            {
                                "endpoint": endpoint,
                                "source_documents_count": len(
                                    [e for e in entries if e.get("_list_key") == "source_documents"]
                                ),
                                "nextbid_documents_count": len(
                                    [e for e in entries if e.get("_list_key") == "nextbid_documents"]
                                ),
                                "entries": [
                                    {
                                        "name": e.get("name") or e.get("filename"),
                                        "sha256": e.get("sha256") or e.get("hash"),
                                        "bytes": e.get("bytes"),
                                        "list_key": e.get("_list_key"),
                                        "has_source_url": bool(e.get("source_url")),
                                        "has_download": bool(e.get("download")),
                                    }
                                    for e in entries[:40]
                                ],
                                "note": "document_inventory_metadata_not_contents",
                            },
                            default=str,
                        )[:8000],
                        "is_metadata_only": True,
                        "endpoint": endpoint,
                        "document_entries": entries,
                    }
                )
                continue
            if endpoint in {"read_file", "get_file"} or body.get("raw_text"):
                text = str(body.get("raw_text") or body.get("text") or "")
            opp = body.get("opportunity") if isinstance(body.get("opportunity"), dict) else body
            if not text and isinstance(opp, dict) and endpoint in {"get_opportunity", ""}:
                text = str(opp.get("description") or opp.get("summary") or opp.get("ai_summary") or "")
            # attachment metadata on opportunity detail
            atts = None
            if isinstance(opp, dict):
                atts = opp.get("attachments") or opp.get("documents") or body.get("files")
            # Also surface source_documents if somehow present on opportunity body
            if isinstance(body, dict) and body.get("source_documents") and endpoint == "get_opportunity":
                from m3.research.document_inventory import extract_document_entries

                entries = extract_document_entries(body)
                if entries:
                    out.append(
                        {
                            "doc_id": f"paid_meta:source_documents:{rid[:12]}",
                            "kind": "attachment_metadata",
                            "source_path": str(store_root() / "by_request" / f"{rid}.json"),
                            "text": json.dumps({"source_documents": entries[:40]}, default=str)[:4000],
                            "is_metadata_only": True,
                            "endpoint": endpoint,
                            "document_entries": entries,
                        }
                    )
            if atts and not text:
                out.append(
                    {
                        "doc_id": f"paid_meta:{rid[:12]}",
                        "kind": "attachment_metadata",
                        "source_path": str(store_root() / "by_request" / f"{rid}.json"),
                        "text": json.dumps(atts, default=str)[:4000],
                        "is_metadata_only": True,
                        "endpoint": endpoint,
                    }
                )
                continue
        elif isinstance(body, str):
            text = body
        if text:
            out.append(
                {
                    "doc_id": f"paid:{rid[:16]}",
                    "kind": "paid_response_body" if len(text) > _LISTING_MAX else "listing_summary",
                    "source_path": str(store_root() / "by_request" / f"{rid}.json"),
                    "text": text,
                    "endpoint": endpoint,
                    "is_metadata_only": False,
                }
            )
    return out


def _load_checkpoint_text(nb_id: str) -> list[dict[str, Any]]:
    from m3.research.nextbid_client import load_checkpoint

    out: list[dict[str, Any]] = []
    try:
        ck = load_checkpoint("nextbid_acquire_v1") or {}
    except Exception:
        return out
    state = ck.get("state") if isinstance(ck, dict) else None
    if not isinstance(state, dict):
        return out
    root = get_settings().data_root() / "nextbid_checkpoints" / "nextbid_acquire_v1.json"
    for bucket in ("enriched", "listings"):
        rows = state.get(bucket) or []
        if isinstance(rows, dict):
            rows = list(rows.values())
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("nb_id") or "") != nb_id:
                continue
            text = str(row.get("document_text") or row.get("description") or row.get("summary") or "")
            if text:
                out.append(
                    {
                        "doc_id": f"checkpoint:{bucket}:{nb_id}",
                        "kind": "listing_summary" if len(text) <= _LISTING_MAX else "checkpoint_enriched",
                        "source_path": str(root),
                        "text": text,
                    }
                )
    return out


def _load_offline_recovery(nb_id: str) -> list[dict[str, Any]]:
    root = get_settings().data_root() / "nextbid_offline_recovery"
    out: list[dict[str, Any]] = []
    enrich = root / "enrichment.jsonl"
    if not enrich.exists():
        return out
    for line in enrich.read_text(encoding="utf-8").splitlines():
        if nb_id not in line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if str(row.get("nb_id") or "") != nb_id:
            continue
        text = str(row.get("description") or row.get("document_text") or "")
        if text:
            out.append(
                {
                    "doc_id": f"offline_recovery:{nb_id}",
                    "kind": "offline_recovery",
                    "source_path": str(enrich),
                    "text": text,
                }
            )
        for ref in row.get("recovered_file_texts") or []:
            if isinstance(ref, dict):
                t = str(ref.get("text") or "")
                if t:
                    out.append(
                        {
                            "doc_id": f"offline_recovery_file:{ref.get('sha') or ref.get('path')}",
                            "kind": "recovered_attachment_text",
                            "source_path": str(ref.get("path") or enrich),
                            "text": t,
                        }
                    )
    return out


def _load_manifest(nb_id: str) -> list[dict[str, Any]]:
    path = get_settings().data_root() / "solicitation_document_manifests" / f"{nb_id}.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    out: list[dict[str, Any]] = [
        {
            "doc_id": f"manifest:{nb_id}",
            "kind": "attachment_metadata",
            "source_path": str(path),
            "text": json.dumps(data, default=str)[:8000],
            "is_metadata_only": True,
        }
    ]
    # Follow local extracted text refs into full_text store when present
    for doc in data.get("documents") or data.get("files") or []:
        if not isinstance(doc, dict):
            continue
        ref = doc.get("local_extracted_text_ref") or doc.get("record_key")
        if ref:
            ft = load_full_text(str(ref))
            if ft and ft.get("full_text"):
                out.append(
                    {
                        "doc_id": f"manifest_text:{ref}",
                        "kind": "manifest_linked_full_text",
                        "source_path": str(path),
                        "text": str(ft.get("full_text") or ""),
                    }
                )
    return out


def _load_package_docs(db: Session | None, nb_id: str) -> list[dict[str, Any]]:
    if db is None:
        return []
    try:
        from m3.models import SolicitationPackage
        from m3.research.package_association import recover_text_from_package
        from sqlalchemy.orm import joinedload
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    try:
        q = (
            db.query(SolicitationPackage)
            .options(joinedload(SolicitationPackage.documents))
            .filter(
                (SolicitationPackage.external_key == nb_id)
                | (SolicitationPackage.external_key == f"nextbid:{nb_id}")
                | (SolicitationPackage.solicitation_number == nb_id)
            )
        )
        pkgs = q.all()
    except Exception:
        return out
    for pkg in pkgs:
        recovered = recover_text_from_package(pkg)
        text = str(recovered.get("text") or "")
        if text:
            out.append(
                {
                    "doc_id": f"package:{pkg.external_key}",
                    "kind": "package_document_text",
                    "source_path": f"db:solicitation_packages:{pkg.external_key}",
                    "text": text,
                    "text_source": recovered.get("text_source"),
                }
            )
        for d in pkg.documents or []:
            meta = {
                "filename": getattr(d, "filename", None),
                "document_type": getattr(d, "document_type", None),
                "storage_path": getattr(d, "storage_path", None),
                "content_hash": getattr(d, "content_hash", None),
            }
            excerpt = getattr(d, "extracted_excerpt", None) or ""
            if excerpt:
                out.append(
                    {
                        "doc_id": f"package_doc:{getattr(d, 'id', meta.get('filename'))}",
                        "kind": "package_excerpt",
                        "source_path": str(meta.get("storage_path") or "db:package_documents"),
                        "text": str(excerpt),
                    }
                )
            else:
                out.append(
                    {
                        "doc_id": f"package_meta:{meta.get('filename')}",
                        "kind": "attachment_metadata",
                        "source_path": str(meta.get("storage_path") or "db:package_documents"),
                        "text": json.dumps(meta, default=str),
                        "is_metadata_only": True,
                    }
                )
    return out


def _load_http_cache_snippets(nb_id: str) -> list[dict[str, Any]]:
    """Best-effort scan of retained HTTP cache JSON for this nb_id (local only)."""
    root = get_settings().data_root() / "nextbid_cache" / "http"
    if not root.exists():
        return []
    out: list[dict[str, Any]] = []
    # Bounded scan — stop after a few hits to avoid multi-second walks in hot paths
    hits = 0
    for path in root.rglob("*.json"):
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if nb_id not in raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        payload = data.get("payload") if isinstance(data, dict) else None
        text = ""
        if isinstance(payload, dict):
            # detail
            if str(payload.get("nb_id") or "") == nb_id or nb_id in str(payload.get("id") or ""):
                text = str(payload.get("description") or payload.get("summary") or "")
            # search results
            for row in payload.get("results") or []:
                if isinstance(row, dict) and str(row.get("nb_id") or "") == nb_id:
                    text = str(row.get("description") or row.get("summary") or text)
        if text:
            out.append(
                {
                    "doc_id": f"http_cache:{path.name}",
                    "kind": "listing_summary" if len(text) <= _LISTING_MAX else "http_cache_detail",
                    "source_path": str(path),
                    "text": text,
                }
            )
            hits += 1
            if hits >= 3:
                break
    return out


def assemble_local_evidence_package(
    nb_id: str,
    *,
    db: Session | None = None,
    title: str | None = None,
    include_http_cache: bool = True,
    include_package_db: bool = True,
) -> dict[str, Any]:
    """Build one assembled evidence package from every relevant local store.

    Does not claim complete solicitation review unless substantive non-preview
    document content is present. Distinguishes metadata-only attachment listings.

    include_http_cache: when False, skips the O(cache) HTTP JSON walk (safe for
    bulk inventory / analysis — paid bodies, full_text, packages, checkpoints remain).
    include_package_db: when False, skips DB package/PDF re-extract (fast inventory).
    """
    nb_id = str(nb_id or "").strip()
    documents: list[dict[str, Any]] = []
    sources_inspected: list[str] = []

    ft = load_full_text(nb_id)
    sources_inspected.append("solicitation_full_text")
    if ft and str(ft.get("full_text") or "").strip():
        text = str(ft.get("full_text") or "")
        thin = bool(ft.get("is_listing_preview_only")) or len(text.strip()) <= _LISTING_MAX
        _add_doc(
            documents,
            doc_id=f"full_text:{nb_id}",
            kind="full_text_store_thin" if thin else "full_text_store",
            source_path=str(
                get_settings().data_root() / "solicitation_full_text" / f"{nb_id}.json"
            ),
            text=text,
            is_metadata_only=False,
        )

    for row in _load_paid_texts(nb_id):
        sources_inspected.append("nextbid_paid_responses")
        _add_doc(
            documents,
            doc_id=str(row["doc_id"]),
            kind=str(row.get("kind") or "paid_response_body"),
            source_path=str(row.get("source_path") or ""),
            text=str(row.get("text") or ""),
            is_metadata_only=bool(row.get("is_metadata_only")),
        )

    for row in _load_checkpoint_text(nb_id):
        sources_inspected.append("nextbid_checkpoints")
        _add_doc(
            documents,
            doc_id=str(row["doc_id"]),
            kind=str(row.get("kind") or "checkpoint"),
            source_path=str(row.get("source_path") or ""),
            text=str(row.get("text") or ""),
        )

    for row in _load_offline_recovery(nb_id):
        sources_inspected.append("nextbid_offline_recovery")
        _add_doc(
            documents,
            doc_id=str(row["doc_id"]),
            kind=str(row.get("kind") or "offline_recovery"),
            source_path=str(row.get("source_path") or ""),
            text=str(row.get("text") or ""),
            is_metadata_only=bool(row.get("is_metadata_only")),
        )

    for row in _load_manifest(nb_id):
        sources_inspected.append("solicitation_document_manifests")
        _add_doc(
            documents,
            doc_id=str(row["doc_id"]),
            kind=str(row.get("kind") or "manifest"),
            source_path=str(row.get("source_path") or ""),
            text=str(row.get("text") or ""),
            is_metadata_only=bool(row.get("is_metadata_only")),
        )

    if include_package_db:
        for row in _load_package_docs(db, nb_id):
            sources_inspected.append("db_solicitation_packages")
            _add_doc(
                documents,
                doc_id=str(row["doc_id"]),
                kind=str(row.get("kind") or "package"),
                source_path=str(row.get("source_path") or ""),
                text=str(row.get("text") or ""),
                is_metadata_only=bool(row.get("is_metadata_only")),
            )
    else:
        sources_inspected.append("db_solicitation_packages_skipped_fast_inventory")

    if include_http_cache:
        for row in _load_http_cache_snippets(nb_id):
            sources_inspected.append("nextbid_http_cache")
            _add_doc(
                documents,
                doc_id=str(row["doc_id"]),
                kind=str(row.get("kind") or "http_cache"),
                source_path=str(row.get("source_path") or ""),
                text=str(row.get("text") or ""),
            )
    else:
        sources_inspected.append("nextbid_http_cache_skipped_bulk")

    # Inventory metadata (never treated as solicitation body)
    inv_path = get_settings().data_root() / "nextbid_inventory" / "opportunities.jsonl"
    sources_inspected.append("nextbid_inventory")
    inv_row: dict[str, Any] = {}
    if inv_path.exists():
        with inv_path.open(encoding="utf-8") as fh:
            for line in fh:
                if nb_id not in line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if str(row.get("nb_id") or "") == nb_id:
                    inv_row = row
                    break

    content_docs = [
        d
        for d in documents
        if not d.get("is_metadata_only") and not d.get("is_listing_preview") and int(d.get("chars") or 0) > _LISTING_MAX
    ]
    listing_only_docs = [d for d in documents if d.get("is_listing_preview") or d.get("kind") == "listing_summary"]
    meta_only = [d for d in documents if d.get("is_metadata_only")]

    # Prefer longest substantive body for primary assembly text
    content_docs_sorted = sorted(content_docs, key=lambda d: int(d.get("chars") or 0), reverse=True)
    primary = content_docs_sorted[0] if content_docs_sorted else None
    assembled_parts: list[str] = []
    source_refs: list[dict[str, Any]] = []
    for d in content_docs_sorted:
        assembled_parts.append(f"=== SOURCE {d['doc_id']} ({d['kind']}) path={d['source_path']} ===\n{d['text']}")
        source_refs.append(
            {
                "doc_id": d["doc_id"],
                "kind": d["kind"],
                "source_path": d["source_path"],
                "chars": d["chars"],
                "page_refs": d.get("page_refs") or [],
            }
        )
    assembled_text = "\n\n".join(assembled_parts)
    total_content_chars = sum(int(d.get("chars") or 0) for d in content_docs)

    # Substantive body ≠ complete package. Text length never proves package completeness.
    complete_enough = total_content_chars >= 800 and bool(content_docs)

    from m3.research.document_inventory import (
        load_inventory,
        package_completeness,
        reprocess_stored_manifests,
    )
    from m3.research.nextbid_response_store import load_by_request_id, store_root as _paid_root

    # Ensure stored list_documents manifests are inventoried (offline, idempotent)
    try:
        reprocess_stored_manifests(nb_id)
    except Exception:
        pass
    doc_inv = load_inventory(nb_id)

    # Pull document_count from latest get_opportunity body
    reported_doc_count = None
    idx = _paid_root() / "by_nb_id" / f"{nb_id}.jsonl"
    if idx.exists():
        for line in reversed(idx.read_text(encoding="utf-8").splitlines()):
            if not line.strip():
                continue
            try:
                meta = json.loads(line)
            except Exception:
                continue
            if meta.get("endpoint") != "get_opportunity":
                continue
            body = (load_by_request_id(str(meta.get("request_id") or "")) or {}).get("body")
            if isinstance(body, dict):
                opp = body.get("opportunity") if isinstance(body.get("opportunity"), dict) else body
                if isinstance(opp, dict):
                    reported_doc_count = opp.get("document_count")
                    if reported_doc_count is None:
                        reported_doc_count = opp.get("attachment_count")
            break

    pkg_comp = package_completeness(
        opportunity_document_count=int(reported_doc_count) if reported_doc_count is not None else None,
        inventory=doc_inv,
        evidence_package={
            "coverage": {
                "has_substantive_document_content": complete_enough,
                "listing_preview_count": len(listing_only_docs),
            }
        },
        required_docs_reviewed=False,
    )

    coverage = {
        "has_substantive_document_content": complete_enough,
        "substantive_doc_count": len(content_docs),
        "listing_preview_count": len(listing_only_docs),
        "metadata_only_count": len(meta_only),
        "total_content_chars": total_content_chars,
        "primary_source": (primary or {}).get("doc_id"),
        "opportunity_document_count": reported_doc_count,
        "document_inventory": {
            "inventoried": pkg_comp.get("inventoried"),
            "downloaded": pkg_comp.get("downloaded"),
            "download_pending": pkg_comp.get("download_pending"),
        },
        "package_completeness": pkg_comp.get("package_completeness"),
        "package_completeness_reasons": pkg_comp.get("reasons"),
        "complete_solicitation_reviewed_claim_allowed": bool(pkg_comp.get("complete_solicitation_reviewed")),
        "text_length_not_proof_of_completeness": True,
        "note": (
            f"Substantive local content assembled; package_completeness={pkg_comp.get('package_completeness')}"
            if complete_enough
            else "Insufficient non-preview document content; listing/metadata only — not a complete solicitation review"
        ),
    }

    return {
        "nb_id": nb_id,
        "title": title or inv_row.get("title") or (ft or {}).get("title"),
        "assembled_at": _utc_now_iso(),
        "inventory": {
            k: inv_row.get(k)
            for k in (
                "nb_id",
                "title",
                "solicitation_number",
                "deadline_at",
                "response_deadline",
                "solicitation_status",
                "lifecycle_status",
                "official_url",
                "source",
            )
            if inv_row
        },
        "sources_inspected": sorted(set(sources_inspected)),
        "documents": [
            {k: v for k, v in d.items() if k not in {"text", "document_entries"}}
            | {"text_preview": (d.get("text") or "")[:240], "has_text": bool((d.get("text") or "").strip())}
            for d in documents
        ],
        "document_texts": documents,  # full texts for analysis (caller responsibility)
        "document_inventory": doc_inv,
        "assembled_text": assembled_text,
        "assembled_chars": len(assembled_text),
        "source_refs": source_refs,
        "coverage": coverage,
        "package_completeness": pkg_comp,
        "network_used": False,
        "api_calls": 0,
    }


def cheap_prefilter_from_evidence(package: dict[str, Any]) -> dict[str, Any]:
    """Inexpensive rules before any AI spend. Never the final decision-maker."""
    title = str(package.get("title") or "")
    text = str(package.get("assembled_text") or "")[:4000]
    blob = f"{title}\n{text}"
    reject = None
    if re.search(
        r"(?i)\b(janitorial\s+services|consulting\s+services|construction\s+of|"
        r"roadway\s+improvements|maintenance\s+services|installation\s+services|"
        r"professional\s+services|staffing)\b",
        blob,
    ) and not re.search(r"(?i)\b(purchase\s+of|furnish|supply\s+of|equipment|supplies)\b", blob):
        reject = "obvious_non_product"
    coverage = package.get("coverage") or {}
    if not coverage.get("has_substantive_document_content"):
        return {
            "ai_eligible": False,
            "reason": "insufficient_substantive_local_content",
            "reject": reject,
        }
    if reject:
        return {"ai_eligible": False, "reason": reject, "reject": reject}
    return {"ai_eligible": True, "reason": "substantive_local_content", "reject": None}
