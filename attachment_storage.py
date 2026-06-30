"""Persist and load solicitation attachment file bytes in PostgreSQL."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from models import Contract, ContractAttachment


def _filename_key(name: str) -> str:
    return (name or "document").strip().lower()


def _sanitize_pdf_text(text: str) -> str:
    return (text or "").replace("\x00", "")


def list_stored_attachments(session: Session, contract_id: int) -> list[ContractAttachment]:
    return (
        session.query(ContractAttachment)
        .filter(ContractAttachment.contract_id == contract_id)
        .order_by(ContractAttachment.id)
        .all()
    )


def stored_pdf_items(session: Session, contract_id: int) -> list[tuple[str, bytes]]:
    rows = list_stored_attachments(session, contract_id)
    items: list[tuple[str, bytes]] = []
    for row in rows:
        if row.file_bytes and row.file_bytes.startswith(b"%PDF"):
            items.append((row.filename, bytes(row.file_bytes)))
    return items


def attachment_storage_summary(session: Session, contract_id: int) -> dict[str, Any]:
    rows = list_stored_attachments(session, contract_id)
    total_bytes = sum(r.file_size_bytes or 0 for r in rows)
    return {
        "count": len(rows),
        "pdf_count": sum(1 for r in rows if (r.file_bytes or b"").startswith(b"%PDF")),
        "total_bytes": total_bytes,
        "files": [
            {
                "filename": r.filename,
                "source": r.source,
                "content_type": r.content_type,
                "file_size_bytes": r.file_size_bytes,
                "extracted_text_chars": len(r.extracted_text or ""),
            }
            for r in rows
        ],
    }


def persist_attachment_files(
    session: Session,
    contract: Contract,
    files: list[tuple[str, bytes, str, str | None]],
    *,
    extracted_by_name: dict[str, str] | None = None,
) -> int:
    """
    Save downloaded files to contract_attachments.
    files: list of (filename, bytes, source, source_url)
    Returns number of rows written/updated.
    """
    extracted_by_name = extracted_by_name or {}
    written = 0
    for filename, data, source, source_url in files:
        if not data:
            continue
        key = _filename_key(filename)
        content_type = "application/pdf" if data.startswith(b"%PDF") else "application/octet-stream"
        per_file_text = _sanitize_pdf_text(extracted_by_name.get(filename) or "")

        existing = (
            session.query(ContractAttachment)
            .filter(
                ContractAttachment.contract_id == contract.id,
                ContractAttachment.filename_key == key,
            )
            .first()
        )
        if existing:
            existing.filename = filename
            existing.source = source
            existing.source_url = source_url
            existing.content_type = content_type
            existing.file_bytes = data
            existing.file_size_bytes = len(data)
            if per_file_text:
                existing.extracted_text = per_file_text
            existing.downloaded_at = datetime.now(timezone.utc)
        else:
            session.add(
                ContractAttachment(
                    contract_id=contract.id,
                    filename=filename,
                    filename_key=key,
                    source=source,
                    source_url=source_url,
                    content_type=content_type,
                    file_bytes=data,
                    file_size_bytes=len(data),
                    extracted_text=per_file_text or None,
                    downloaded_at=datetime.now(timezone.utc),
                )
            )
        written += 1
    return written


def download_and_persist_attachments(
    session: Session,
    contract: Contract,
    *,
    max_pdfs: int = 12,
) -> list[tuple[str, bytes]]:
    """Download from SAM/PIEE URLs, persist bytes to DB, return PDF list."""
    from claude_client import _attachment_label, _collect_attachment_urls
    from piee_client import fetch_piee_pdfs

    raw = contract.sam_raw if isinstance(contract.sam_raw, dict) else {}
    to_persist: list[tuple[str, bytes, str, str | None]] = []
    pdfs: list[tuple[str, bytes]] = []
    seen: set[str] = set()

    try:
        piee_pdfs, _ = fetch_piee_pdfs(raw)
        for name, data in piee_pdfs:
            if not data.startswith(b"%PDF"):
                continue
            key = _filename_key(name)
            if key in seen:
                continue
            seen.add(key)
            to_persist.append((name, data, "piee", None))
            pdfs.append((name, data))
            if len(pdfs) >= max_pdfs:
                break
    except Exception:
        pass

    if len(pdfs) < max_pdfs:
        import httpx

        for url in _collect_attachment_urls(raw):
            if len(pdfs) >= max_pdfs:
                break
            try:
                with httpx.Client(timeout=180.0, follow_redirects=True) as client:
                    resp = client.get(url)
                    resp.raise_for_status()
            except Exception:
                continue
            data = resp.content
            if not data.startswith(b"%PDF"):
                continue
            label = _attachment_label(url, resp)
            key = _filename_key(label)
            if key in seen:
                continue
            seen.add(key)
            to_persist.append((label, data, "sam", url))
            pdfs.append((label, data))

    if to_persist:
        persist_attachment_files(session, contract, to_persist)
    return pdfs


def get_contract_pdf_bytes(session: Session, contract: Contract, *, max_pdfs: int = 12) -> list[tuple[str, bytes]]:
    """PDF bytes from DB when stored; otherwise download, persist, and return."""
    if contract.id:
        stored = stored_pdf_items(session, contract.id)
        if stored:
            return stored[:max_pdfs]
    downloaded = download_and_persist_attachments(session, contract, max_pdfs=max_pdfs)
    if contract.id and downloaded:
        session.flush()
    return downloaded
