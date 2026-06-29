"""Download solicitation documents from the public PIEE portal (no login required)."""

from __future__ import annotations

import io
import logging
import os
import re
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

logger = logging.getLogger("govtracker.piee")

PIEE_OPP_LINK = "https://piee.eb.mil/sol/xhtml/unauth/search/oppMgmtLink.xhtml"
MAX_PIE_PDFS = 12
MAX_PIE_PDF_BYTES = 40_000_000


def piee_fetch_enabled() -> bool:
    return os.getenv("PIEE_FETCH_ENABLED", "true").strip().lower() not in ("0", "false", "no")


def find_piee_notice_url(raw: dict[str, Any]) -> str | None:
    """Return the public PIEE notice URL from SAM attachment links or solicitation metadata."""
    for item in raw.get("opportunityAttachments") or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "")
        if "piee.eb.mil" in url.lower() and ("oppmgmtlink" in url.lower() or "viewpublicnotice" in url.lower()):
            return url

    sol_number = (
        raw.get("solicitationNumber")
        or (raw.get("documentAccess") or {}).get("solicitation_number")
        or ""
    )
    sol_number = str(sol_number).strip()
    if not sol_number:
        return None

    notice_type = _guess_notice_type(raw)
    params = urlencode({"noticeId": sol_number, "noticeType": notice_type})
    return f"{PIEE_OPP_LINK}?{params}"


def _guess_notice_type(raw: dict[str, Any]) -> str:
    blob = " ".join(
        str(raw.get(key) or "")
        for key in ("title", "type", "typeOfSetAsideDescription", "descriptionText")
    )
    if re.search(r"\bRFQ\b", blob, flags=re.I):
        return "RFQ"
    if re.search(r"\bRFP\b", blob, flags=re.I):
        return "RFP"
    return "CombinedSynopsisSolicitation"


def _priority_key(filename: str) -> tuple[int, str]:
    name = filename.lower()
    if "statement_of_work" in name or name.startswith("sow") or "_sow_" in name:
        return (0, name)
    if "pws" in name or "_prs" in name or "performance_requirement" in name:
        return (1, name)
    if "drawing" in name or "floor_plan" in name or "floor plan" in name:
        return (2, name)
    if "solicitation" in name and "instruction" not in name:
        return (1, name)
    if "price_breakout" in name or "pbs" in name:
        return (2, name)
    if "special_instruction" in name:
        return (3, name)
    if "wage_determination" in name:
        return (4, name)
    return (5, name)


def _extract_pdfs_from_zip(zip_bytes: bytes) -> list[tuple[str, bytes]]:
    pdfs: list[tuple[str, bytes]] = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            name = info.filename.split("/")[-1]
            if not name.lower().endswith(".pdf"):
                continue
            data = archive.read(info)
            if len(data) > MAX_PIE_PDF_BYTES:
                logger.warning("Skipping oversized PIEE PDF %s (%s bytes)", name, len(data))
                continue
            pdfs.append((name, data))
    pdfs.sort(key=lambda row: _priority_key(row[0]))
    return pdfs[:MAX_PIE_PDFS]


def download_piee_zip(notice_url: str) -> bytes | None:
    """Use headless Chromium to click PIEE 'Download All Attachments'."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            page = browser.new_page()
            page.goto(notice_url, wait_until="networkidle", timeout=120_000)
            if page.locator('a[accesskey="V"]').count() == 0:
                logger.info("PIEE notice has no public attachments: %s", notice_url)
                return None
            with page.expect_download(timeout=120_000) as download_info:
                page.get_by_role("link", name="Download All Attachments").click()
            download = download_info.value
            path = download.path()
            if not path:
                return None
            return Path(path).read_bytes()
        finally:
            browser.close()


def list_piee_attachment_names(notice_url: str) -> list[str]:
    """List PDF filenames on the public PIEE notice page (no download)."""
    import httpx

    try:
        response = httpx.get(
            notice_url,
            timeout=90.0,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        )
        response.raise_for_status()
    except Exception:
        return []

    names = re.findall(r'class="btn-link">\s*([^<\s][^<]*?\.pdf)\s*</a>', response.text, flags=re.I)
    cleaned = [re.sub(r"\s+", " ", name).strip() for name in names]
    return cleaned


def attach_piee_manifest(raw: dict[str, Any]) -> dict[str, Any]:
    """Add PIEE attachment filenames and notice URL to sam_raw for cards and screening."""
    notice_url = find_piee_notice_url(raw)
    if not notice_url:
        return raw

    updated = dict(raw)
    updated["pieeNoticeUrl"] = notice_url
    names = list_piee_attachment_names(notice_url)
    if names:
        updated["pieeAttachments"] = [
            {
                "type": "file",
                "description": name,
                "source": "piee",
                "url": notice_url,
            }
            for name in names
        ]
        access = dict(updated.get("documentAccess") or {})
        access["piee_attachment_count"] = len(names)
        access["piee_notice_url"] = notice_url
        access["requires_external_portal"] = False
        access["summary"] = (
            f"{len(names)} solicitation document(s) on PIEE "
            f"(Statement of Work, wage determinations, etc.)."
        )
        updated["documentAccess"] = access
    return updated


def fetch_piee_pdfs(raw: dict[str, Any]) -> tuple[list[tuple[str, bytes]], str | None]:
    """
    Download PDFs from the public PIEE solicitation page linked on SAM.gov.
    Returns (pdf_name_and_bytes, notice_url).
    """
    if not piee_fetch_enabled():
        return [], None

    notice_url = find_piee_notice_url(raw)
    if not notice_url:
        return [], None

    try:
        zip_bytes = download_piee_zip(notice_url)
    except Exception:
        logger.exception("PIEE download failed for %s", notice_url)
        return [], notice_url

    if not zip_bytes:
        return [], notice_url

    return _extract_pdfs_from_zip(zip_bytes), notice_url
