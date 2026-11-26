"""Canonical solicitation document inventory — inventory ≠ contents.

Parses NextBid list_documents shapes including source_documents / nextbid_documents.
Tracks per-document acquisition status. Never treats metadata as document text.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from m3.config import get_settings

# Acquisition / processing statuses (meanings fixed; names stable)
STATUS_DISCOVERED = "DISCOVERED"
STATUS_DOWNLOAD_PENDING = "DOWNLOAD_PENDING"
STATUS_DOWNLOADED = "DOWNLOADED"
STATUS_TEXT_EXTRACTED = "TEXT_EXTRACTED"
STATUS_ANALYZED = "ANALYZED"
STATUS_VERIFIED = "VERIFIED"
STATUS_SUPERSEDED = "SUPERSEDED"
STATUS_RESTRICTED = "RESTRICTED"
STATUS_FAILED = "FAILED"

DOC_STATUSES = frozenset(
    {
        STATUS_DISCOVERED,
        STATUS_DOWNLOAD_PENDING,
        STATUS_DOWNLOADED,
        STATUS_TEXT_EXTRACTED,
        STATUS_ANALYZED,
        STATUS_VERIFIED,
        STATUS_SUPERSEDED,
        STATUS_RESTRICTED,
        STATUS_FAILED,
    }
)

# Package completeness — evidence-based, not text-length
PKG_LISTING_ONLY = "listing_only"
PKG_SUBSTANTIVE_BODY = "substantive_solicitation_body"
PKG_PARTIAL_DOCUMENTS = "partial_document_package"
PKG_COMPLETE_RELEVANT = "complete_relevant_package"
PKG_COMPLETE_REVIEWED = "complete_package_reviewed"
PKG_COMPLETENESS_UNVERIFIED = "package_completeness_unverified"


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def inventory_root() -> Path:
    p = get_settings().data_root() / "solicitation_document_inventory"
    p.mkdir(parents=True, exist_ok=True)
    (p / "by_nb_id").mkdir(parents=True, exist_ok=True)
    return p


def inventory_path(nb_id: str) -> Path:
    return inventory_root() / "by_nb_id" / f"{nb_id}.json"


def originals_dir(nb_id: str) -> Path:
    return get_settings().data_root() / "nextbid_original_documents" / nb_id


def extract_document_entries(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Normalize NextBid list_documents / list_files response shapes.

    Recognizes: source_documents, nextbid_documents, documents, files, items,
    results, attachment_index, filed, data.
    """
    if not isinstance(payload, dict):
        return []
    files: list[dict[str, Any]] = []
    for key in (
        "source_documents",
        "nextbid_documents",
        "documents",
        "files",
        "items",
        "results",
        "attachment_index",
        "filed",
    ):
        val = payload.get(key)
        if isinstance(val, list):
            for x in val:
                if isinstance(x, dict):
                    row = dict(x)
                    row["_list_key"] = key
                    files.append(row)
    if not files and isinstance(payload.get("data"), list):
        for x in payload["data"]:
            if isinstance(x, dict):
                row = dict(x)
                row["_list_key"] = "data"
                files.append(row)
    return files


def _doc_identity(d: dict[str, Any]) -> str:
    sha = str(d.get("sha256") or d.get("hash") or d.get("content_hash") or "").strip().lower()
    if sha:
        return f"sha256:{sha}"
    name = str(d.get("name") or d.get("filename") or d.get("path") or d.get("id") or "unknown").strip()
    return f"name:{name.lower()[:120]}"


def classify_local_bytes_status(nb_id: str, entry: dict[str, Any]) -> str:
    """DISCOVERED vs DOWNLOADED based on local original bytes + hash."""
    sha = str(entry.get("sha256") or entry.get("hash") or "").strip().lower()
    odir = originals_dir(nb_id)
    if not odir.exists():
        return STATUS_DOWNLOAD_PENDING if (sha or entry.get("download") or entry.get("source_url")) else STATUS_DISCOVERED
    if sha:
        for p in odir.iterdir():
            if p.is_file() and sha[:16] in p.name.lower():
                return STATUS_DOWNLOADED
        # meta sidecar
        meta = odir / f"{sha}.meta.json"
        if meta.exists():
            return STATUS_DOWNLOADED
    # any file present with matching name
    name = str(entry.get("name") or entry.get("filename") or "")
    if name:
        safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in name)[:60]
        for p in odir.iterdir():
            if p.is_file() and safe and safe.lower() in p.name.lower():
                return STATUS_DOWNLOADED
    return STATUS_DOWNLOAD_PENDING


def signed_url_expired(download_meta: dict[str, Any] | None) -> bool | None:
    """True if expires_at is past; None if cannot establish."""
    if not download_meta:
        return None
    exp = str((download_meta.get("download") or download_meta).get("expires_at") or "")
    if not exp:
        return None
    try:
        dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt <= datetime.now(timezone.utc)
    except Exception:
        return None


def build_inventory_from_list_documents(
    nb_id: str,
    payload: dict[str, Any],
    *,
    source_request_id: str | None = None,
) -> dict[str, Any]:
    """Convert a list_documents body into durable inventory records (metadata only)."""
    entries = extract_document_entries(payload)
    docs: list[dict[str, Any]] = []
    for d in entries:
        status = classify_local_bytes_status(nb_id, d)
        docs.append(
            {
                "doc_key": _doc_identity(d),
                "nb_id": nb_id,
                "provider": "nextbid",
                "name": d.get("name") or d.get("filename"),
                "sha256": d.get("sha256") or d.get("hash"),
                "bytes": d.get("bytes") or d.get("size"),
                "source_url": d.get("source_url"),
                "download_path": d.get("download"),
                "store": d.get("store"),
                "store_key": d.get("store_key"),
                "list_key": d.get("_list_key"),
                "kind": d.get("kind") or "attachment",
                "acquisition_status": status,
                "extraction_status": None,
                "analysis_status": None,
                "is_inventory_metadata_only": True,
                "is_document_content": False,
                "discovered_at": _utc(),
                "source_request_id": source_request_id,
            }
        )
    return {
        "nb_id": nb_id,
        "updated_at": _utc(),
        "provider": "nextbid",
        "source_documents_count": len(
            [d for d in docs if d.get("list_key") == "source_documents"]
        ),
        "nextbid_documents_count": len(
            [d for d in docs if d.get("list_key") == "nextbid_documents"]
        ),
        "originals_available": payload.get("originals_available"),
        "originals_stored": payload.get("originals_stored"),
        "documents": docs,
        "response_keys": sorted(payload.keys()) if isinstance(payload, dict) else [],
        "note": "Inventory metadata only — not document contents",
    }


def load_inventory(nb_id: str) -> dict[str, Any] | None:
    path = inventory_path(nb_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_inventory(inv: dict[str, Any]) -> Path:
    nb = str(inv.get("nb_id") or "UNKNOWN")
    path = inventory_path(nb)
    path.parent.mkdir(parents=True, exist_ok=True)
    # merge with existing by doc_key
    prior = load_inventory(nb) or {}
    by_key = {d.get("doc_key"): d for d in (prior.get("documents") or []) if isinstance(d, dict)}
    for d in inv.get("documents") or []:
        if not isinstance(d, dict):
            continue
        k = d.get("doc_key")
        if k and k in by_key:
            merged = {**by_key[k], **{x: y for x, y in d.items() if y is not None}}
            by_key[k] = merged
        elif k:
            by_key[k] = d
    inv = {
        **prior,
        **inv,
        "documents": list(by_key.values()),
        "updated_at": _utc(),
        "document_count": len(by_key),
    }
    path.write_text(json.dumps(inv, indent=2, default=str), encoding="utf-8")
    return path


def load_list_documents_from_store(nb_id: str) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Return (meta, body) for every stored list_documents response for nb_id."""
    from m3.research.nextbid_response_store import load_by_request_id, store_root

    idx = store_root() / "by_nb_id" / f"{nb_id}.jsonl"
    out: list[tuple[dict[str, Any], dict[str, Any]]] = []
    if not idx.exists():
        return out
    for line in idx.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            meta = json.loads(line)
        except Exception:
            continue
        if meta.get("endpoint") != "list_documents":
            continue
        row = load_by_request_id(str(meta.get("request_id") or ""))
        if not row:
            continue
        body = row.get("body")
        if isinstance(body, dict):
            out.append((meta, body))
    return out


def reprocess_stored_manifests(nb_id: str) -> dict[str, Any]:
    """Offline: parse all paid list_documents for nb into durable inventory. Zero network."""
    pairs = load_list_documents_from_store(nb_id)
    if not pairs:
        return {"nb_id": nb_id, "ok": True, "manifests": 0, "documents": 0, "reason": "no_stored_list_documents"}
    combined_docs: list[dict[str, Any]] = []
    keys: list[str] = []
    for meta, body in pairs:
        built = build_inventory_from_list_documents(
            nb_id, body, source_request_id=str(meta.get("request_id") or "")[:24]
        )
        combined_docs.extend(built.get("documents") or [])
        keys = built.get("response_keys") or keys
    inv = {
        "nb_id": nb_id,
        "provider": "nextbid",
        "documents": combined_docs,
        "response_keys": keys,
        "reprocessed_from_store": True,
        "manifests_processed": len(pairs),
        "note": "Inventory from stored list_documents — contents not implied",
    }
    path = save_inventory(inv)
    return {
        "ok": True,
        "nb_id": nb_id,
        "manifests": len(pairs),
        "documents": len(load_inventory(nb_id).get("documents") or []) if load_inventory(nb_id) else 0,
        "path": str(path),
        "downloaded": sum(
            1
            for d in (load_inventory(nb_id) or {}).get("documents") or []
            if d.get("acquisition_status") == STATUS_DOWNLOADED
        ),
        "download_pending": sum(
            1
            for d in (load_inventory(nb_id) or {}).get("documents") or []
            if d.get("acquisition_status") == STATUS_DOWNLOAD_PENDING
        ),
    }


def package_completeness(
    *,
    opportunity_document_count: int | None,
    inventory: dict[str, Any] | None,
    evidence_package: dict[str, Any] | None,
    required_docs_reviewed: bool = False,
) -> dict[str, Any]:
    """Evidence-based package completeness — NEVER uses text length alone as proof."""
    cov = (evidence_package or {}).get("coverage") or {}
    inv_docs = (inventory or {}).get("documents") or []
    pending = [d for d in inv_docs if d.get("acquisition_status") in {STATUS_DOWNLOAD_PENDING, STATUS_DISCOVERED}]
    downloaded = [d for d in inv_docs if d.get("acquisition_status") in {STATUS_DOWNLOADED, STATUS_TEXT_EXTRACTED, STATUS_ANALYZED, STATUS_VERIFIED}]
    reported = opportunity_document_count
    has_body = bool(cov.get("has_substantive_document_content"))
    listing_only = int(cov.get("listing_preview_count") or 0) > 0 and not has_body

    label = PKG_COMPLETENESS_UNVERIFIED
    reasons: list[str] = []

    if reported is not None and int(reported) > 0 and not inv_docs:
        label = PKG_PARTIAL_DOCUMENTS
        reasons.append(f"opportunity_reports_document_count={reported}_but_no_inventory_or_bodies")
    elif reported is not None and int(reported) > 0 and pending:
        label = PKG_PARTIAL_DOCUMENTS
        reasons.append(f"discovered_docs_not_downloaded:{len(pending)}")
    elif inv_docs and pending:
        label = PKG_PARTIAL_DOCUMENTS
        reasons.append(f"download_pending:{len(pending)}")
    elif inv_docs and downloaded and not pending and required_docs_reviewed:
        label = PKG_COMPLETE_REVIEWED
        reasons.append("all_inventoried_docs_downloaded_and_marked_reviewed")
    elif inv_docs and downloaded and not pending:
        label = PKG_COMPLETE_RELEVANT
        reasons.append("all_inventoried_docs_downloaded_review_flag_not_set")
    elif has_body and (reported in (None, 0)) and not inv_docs:
        # Body-only sources: may be complete IF provider reports zero docs — still unverified
        label = PKG_SUBSTANTIVE_BODY
        reasons.append("substantive_local_body_without_attachment_inventory_completeness_unverified")
    elif listing_only:
        label = PKG_LISTING_ONLY
        reasons.append("listing_preview_or_thin_description_only")
    else:
        label = PKG_COMPLETENESS_UNVERIFIED
        reasons.append("insufficient_evidence_to_classify_package")

    return {
        "package_completeness": label,
        "reasons": reasons,
        "opportunity_document_count": reported,
        "inventoried": len(inv_docs),
        "downloaded": len(downloaded),
        "download_pending": len(pending),
        "text_length_not_used_as_completeness_proof": True,
        "complete_solicitation_reviewed": label == PKG_COMPLETE_REVIEWED,
    }


def select_best_source_document(
    docs: list[dict[str, Any]],
    *,
    prefer_name_substrings: list[str] | None = None,
) -> dict[str, Any] | None:
    """Pick the most likely requirements/schedule PDF — avoid specimen contracts by default."""
    if not docs:
        return None
    prefer = [s.lower() for s in (prefer_name_substrings or [])]
    avoid = ("specimen", "sample contract", "ccna", "professional services agreement")

    def score(d: dict[str, Any]) -> int:
        name = str(d.get("name") or d.get("filename") or "").lower()
        s = 0
        for w, pts in (
            ("schedule", 50),
            ("pricing", 45),
            ("bid", 35),
            ("spec", 40),
            ("solicitation", 40),
            ("ifb", 35),
            ("rfb", 35),
            ("rfq", 30),
            ("rfp", 25),
            ("propane", 60),
            ("fuel", 40),
            ("supply", 25),
            ("pdf", 5),
        ):
            if w in name:
                s += pts
        for p in prefer:
            if p and p in name:
                s += 80
        for a in avoid:
            if a in name:
                s -= 100
        try:
            s += min(int(d.get("bytes") or 0) // 50000, 20)
        except Exception:
            pass
        return s

    ranked = sorted(docs, key=score, reverse=True)
    return ranked[0] if ranked else None
