"""Fetch full SAM.gov opportunity details: description, attachments, document access."""

from __future__ import annotations

import os
import re
from html import unescape
from typing import Any

import httpx

from api_budget import can_spend_sam, record_sam_usage
from usaspending_client import STATE_NAME_TO_CODE, normalize_state

NOTICE_DESC_URL = "https://api.sam.gov/prod/opportunities/v1/noticedesc"
SAM_SEARCH_URL = "https://api.sam.gov/opportunities/v2/search"
SAM_RESOURCES_URL = "https://sam.gov/api/prod/opps/v3/opportunities/{notice_id}/resources"
SAM_FILE_DOWNLOAD_URL = "https://sam.gov/api/prod/opps/v3/opportunities/resources/files/{resource_id}/download"

EXTERNAL_PORTAL_PATTERNS: list[tuple[str, str]] = [
    ("PIEE", r"\bPIEE\b|piee\.eb\.mil"),
    ("FedConnect", r"FedConnect|fedconnect\.net"),
    ("NECO", r"\bNECO\b"),
    ("DIBBS", r"\bDIBBS\b"),
]

STATE_NAME_PATTERN = "|".join(
    sorted((re.escape(name.title()) for name in STATE_NAME_TO_CODE if len(name) > 2), key=len, reverse=True)
)


def _api_key() -> str:
    return os.getenv("SAM_GOV_API_KEY", "").strip()


def _sam_api_get(url: str, *, params: dict[str, Any] | None = None, timeout: float = 45.0) -> httpx.Response | None:
    """Perform one SAM.gov API GET and count it against the daily budget."""
    if not can_spend_sam(1):
        return None
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(url, params=params)
            response.raise_for_status()
            record_sam_usage(1)
            return response
    except Exception:
        return None


def html_to_text(html: str | None) -> str:
    if not html:
        return ""
    text = re.sub(r"(?i)<br\s*/?>", "\n", html)
    text = re.sub(r"(?i)</p>", "\n\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(re.sub(r"\s+", " ", text)).strip()
    text = re.sub(r"\n\s+\n", "\n\n", text)
    return text.strip()


def extract_states_from_text(*parts: str | None) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    blob = " ".join(p for p in parts if p)
    if not blob:
        return found

    for match in re.finditer(rf"\b({STATE_NAME_PATTERN})\b", blob, flags=re.IGNORECASE):
        code = normalize_state(match.group(1))
        if code and code not in seen:
            seen.add(code)
            found.append(code)

    for match in re.finditer(r"\b([A-Z]{2})\b", blob):
        code = normalize_state(match.group(1))
        if code and code not in seen:
            seen.add(code)
            found.append(code)

    return found


def detect_external_portals(*texts: str | None) -> list[str]:
    blob = " ".join(t for t in texts if t)
    portals: list[str] = []
    for label, pattern in EXTERNAL_PORTAL_PATTERNS:
        if re.search(pattern, blob, flags=re.IGNORECASE):
            portals.append(label)
    return portals


def build_file_download_url(resource_id: str, api_key: str | None = None) -> str:
    api_key = api_key or _api_key()
    base = SAM_FILE_DOWNLOAD_URL.format(resource_id=resource_id)
    return f"{base}?api_key={api_key}" if api_key else base


def fetch_notice_description(notice_id: str, api_key: str | None = None) -> str | None:
    api_key = api_key or _api_key()
    if not notice_id or not api_key:
        return None
    response = _sam_api_get(
        NOTICE_DESC_URL,
        params={"noticeid": notice_id, "api_key": api_key},
    )
    if not response:
        return None
    try:
        payload = response.json()
        description = payload.get("description")
        return str(description) if description else None
    except Exception:
        return None


def fetch_opportunity_attachments(notice_id: str, api_key: str | None = None) -> list[dict[str, Any]] | None:
    """Fetch attachments and links posted on the SAM.gov opportunity. None if the API call failed."""
    api_key = api_key or _api_key()
    if not notice_id or not api_key:
        return None

    url = SAM_RESOURCES_URL.format(notice_id=notice_id)
    response = _sam_api_get(url, params={"api_key": api_key})
    if not response or not response.text.strip():
        return None
    try:
        data = response.json()
    except Exception:
        return None

    embedded = data.get("_embedded") if isinstance(data, dict) else None
    if not isinstance(embedded, dict):
        return []

    attachment_lists = embedded.get("opportunityAttachmentList") or []
    raw_items: list[dict[str, Any]] = []
    for block in attachment_lists:
        if isinstance(block, dict):
            raw_items.extend(block.get("attachments") or [])

    attachments: list[dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        att_type = str(item.get("type") or "file").lower()
        description = (
            item.get("description")
            or item.get("name")
            or item.get("resourceName")
            or "Attachment"
        )
        resource_id = item.get("resourceId")
        uri = item.get("uri")

        entry: dict[str, Any] = {
            "type": att_type,
            "description": str(description).strip(),
            "resource_id": resource_id,
            "attachment_id": item.get("attachmentId"),
            "mime_type": item.get("mimeType"),
            "size": item.get("size"),
            "posted_date": item.get("postedDate"),
        }

        if att_type == "link" and uri:
            entry["url"] = str(uri).strip()
            entry["is_pdf_link"] = _looks_like_pdf_url(entry["url"])
        elif att_type == "file" and resource_id:
            entry["download_url"] = build_file_download_url(str(resource_id), api_key)
            entry["is_pdf_link"] = True

        attachments.append(entry)

    attachments.sort(key=lambda row: row.get("posted_date") or "", reverse=True)
    return attachments


def _looks_like_pdf_url(url: str) -> bool:
    cleaned = url.lower().split("?")[0]
    return cleaned.endswith(".pdf")


def attachments_to_links(attachments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    links: list[dict[str, Any]] = []
    for item in attachments:
        if item.get("type") == "link" and item.get("url"):
            links.append({"url": item["url"], "label": item.get("description") or item["url"]})
    return links


def attachments_to_download_urls(attachments: list[dict[str, Any]]) -> list[str]:
    urls: list[str] = []
    for item in attachments:
        if item.get("download_url"):
            urls.append(item["download_url"])
        elif item.get("is_pdf_link") and item.get("url"):
            urls.append(item["url"])
    return urls


def build_document_access(
    raw: dict[str, Any],
    *,
    description_text: str,
    attachments: list[dict[str, Any]],
) -> dict[str, Any]:
    file_count = sum(1 for item in attachments if item.get("type") == "file")
    link_count = sum(1 for item in attachments if item.get("type") == "link")
    pdf_link_count = sum(1 for item in attachments if item.get("is_pdf_link"))
    total = len(attachments)

    link_labels = [str(item.get("description") or "Link") for item in attachments if item.get("type") == "link"]
    file_labels = [str(item.get("description") or "File") for item in attachments if item.get("type") == "file"]

    external_portals = detect_external_portals(
        description_text,
        raw.get("title"),
        " ".join(link_labels),
        " ".join(item.get("url") or "" for item in attachments),
    )

    if total == 0:
        legacy_links = raw.get("resourceLinks") or []
        if isinstance(legacy_links, list) and legacy_links:
            total = len(legacy_links)
            file_count = total
            status = "pdf_attachments"
            summary = f"{total} PDF attachment(s) listed on SAM.gov."
        elif external_portals:
            status = "external_portal"
            summary = (
                f"Documents referenced on {', '.join(external_portals)} - "
                "check SAM.gov Attachments/Links (API returned no attachment list)."
            )
        else:
            status = "none_found"
            summary = "No attachments or links found on SAM.gov yet."
    elif file_count and link_count:
        status = "mixed_attachments"
        summary = (
            f"{total} item(s) on SAM.gov: {file_count} file(s) "
            f"({', '.join(file_labels[:2])}{'…' if len(file_labels) > 2 else ''}) "
            f"and {link_count} link(s)."
        )
    elif file_count:
        status = "pdf_attachments"
        summary = f"{file_count} file attachment(s) on SAM.gov."
    else:
        status = "sam_links"
        summary = f"{link_count} link(s) on SAM.gov."
        if pdf_link_count:
            summary += f" Includes {pdf_link_count} direct PDF link(s) we can read."

    requires_external = bool(external_portals) and file_count == 0

    return {
        "status": status,
        "summary": summary,
        "total_count": total,
        "file_attachment_count": file_count,
        "link_count": link_count,
        "pdf_link_count": pdf_link_count,
        "pdf_attachment_count": file_count + pdf_link_count,
        "external_link_count": link_count,
        "external_portals": external_portals,
        "requires_external_portal": requires_external,
        "sam_gov_link": raw.get("uiLink"),
        "solicitation_number": raw.get("solicitationNumber"),
        "attachment_labels": [item.get("description") for item in attachments if item.get("description")],
    }


def _apply_attachment_fields(
    raw: dict[str, Any],
    *,
    attachments: list[dict[str, Any]],
    description_text: str,
) -> dict[str, Any]:
    opportunity_links = attachments_to_links(attachments)
    download_urls = attachments_to_download_urls(attachments)
    document_access = build_document_access(
        raw,
        description_text=description_text,
        attachments=attachments,
    )

    enriched = dict(raw)
    enriched["opportunityAttachments"] = attachments
    enriched["opportunityLinks"] = opportunity_links
    enriched["attachmentDownloadUrls"] = download_urls
    if download_urls:
        enriched["resourceLinks"] = download_urls
    enriched["documentAccess"] = document_access
    return enriched


def refresh_opportunity_attachments(raw: dict[str, Any], api_key: str | None = None) -> dict[str, Any]:
    """Fast path: fetch SAM.gov attachments/links only (skips description + search refetch)."""
    notice_id = str(raw.get("noticeId") or "")
    description_text = str(raw.get("descriptionText") or html_to_text(raw.get("descriptionHtml")))
    attachments = fetch_opportunity_attachments(notice_id, api_key)
    if attachments is None:
        failed = dict(raw)
        failed["scrapeStatus"] = "incomplete"
        failed["scrapeError"] = "sam_attachments_failed"
        return failed
    return _apply_attachment_fields(raw, attachments=attachments, description_text=description_text)


def enrich_description_only(raw: dict[str, Any] | None, api_key: str | None = None) -> dict[str, Any]:
    """Light enrich for text screening — description text only, no attachment API calls."""
    from datetime import datetime, timezone

    if not raw:
        return {}
    notice_id = str(raw.get("noticeId") or "")
    if raw.get("descriptionText"):
        return dict(raw)

    description_field = raw.get("description")
    description_html: str | None = raw.get("descriptionHtml")

    if not description_html:
        if isinstance(description_field, str) and description_field.startswith("http"):
            description_html = fetch_notice_description(notice_id, api_key)
        elif isinstance(description_field, str) and description_field.strip():
            description_html = description_field

    description_text = html_to_text(description_html) or str(raw.get("descriptionText") or "")
    enriched = dict(raw)
    if description_html:
        enriched["descriptionHtml"] = description_html
    if description_text:
        enriched["descriptionText"] = description_text
    enriched["textEnrichedAt"] = datetime.now(timezone.utc).isoformat() if description_text else None
    return enriched


def enrich_opportunity(raw: dict[str, Any] | None, api_key: str | None = None) -> dict[str, Any]:
    """Merge search metadata with full description text and SAM.gov attachments."""
    if not raw:
        return {}
    notice_id = str(raw.get("noticeId") or "")
    description_field = raw.get("description")
    description_html: str | None = raw.get("descriptionHtml")

    if not description_html:
        if isinstance(description_field, str) and description_field.startswith("http"):
            description_html = fetch_notice_description(notice_id, api_key)
        elif isinstance(description_field, str) and description_field.strip():
            description_html = description_field

    description_text = html_to_text(description_html) or str(raw.get("descriptionText") or "")
    attachments = fetch_opportunity_attachments(notice_id, api_key)
    if attachments is None:
        failed = dict(raw)
        failed["scrapeStatus"] = "incomplete"
        failed["scrapeError"] = "sam_attachments_failed"
        return failed

    work_states = extract_states_from_text(
        raw.get("title"),
        _place_of_performance_text(raw),
        description_text,
    )

    enriched = _apply_attachment_fields(raw, attachments=attachments, description_text=description_text)
    enriched["descriptionHtml"] = description_html
    enriched["descriptionText"] = description_text
    enriched["workStates"] = work_states
    return enriched


def _place_of_performance_text(raw: dict[str, Any]) -> str | None:
    place = raw.get("placeOfPerformance") or raw.get("placeOfPerformanceLocation")
    if not isinstance(place, dict):
        return None
    parts = [place.get("streetAddress"), place.get("streetAddress2")]
    city = place.get("city")
    if isinstance(city, dict):
        city = city.get("name")
    state = place.get("state")
    if isinstance(state, dict):
        state = state.get("code") or state.get("name")
    parts.extend([city, state, place.get("zip")])
    return ", ".join(str(p) for p in parts if p) or None


def needs_attachment_refresh(sam_raw: dict[str, Any] | None) -> bool:
    return not is_sam_metadata_ready(sam_raw)


def is_sam_metadata_ready(sam_raw: dict[str, Any] | None) -> bool:
    """True when SAM attachments + PIEE manifest metadata were fetched (before PDF text extraction)."""
    if not isinstance(sam_raw, dict) or not sam_raw:
        return False
    status = sam_raw.get("scrapeStatus")
    if status not in ("metadata_ready", "complete"):
        return False
    if "opportunityAttachments" not in sam_raw:
        return False
    doc_access = sam_raw.get("documentAccess")
    if not isinstance(doc_access, dict):
        return False
    if "attachment_labels" not in doc_access:
        return False
    return True


def is_scrape_complete(sam_raw: dict[str, Any] | None) -> bool:
    """True when SAM metadata is ready AND attachment text extraction finished (see attachmentExtraction)."""
    if not is_sam_metadata_ready(sam_raw):
        return False
    if sam_raw.get("scrapeStatus") != "complete":
        return False
    extraction = sam_raw.get("attachmentExtraction")
    if not isinstance(extraction, dict):
        return False
    return extraction.get("method") in ("text", "ocr_needed", "no_pdfs_expected")


def needs_enrichment(sam_raw: dict[str, Any] | None) -> bool:
    return not is_sam_metadata_ready(sam_raw)


def scrape_opportunity_complete(raw: dict[str, Any], api_key: str | None = None) -> tuple[dict[str, Any], bool]:
    """
    Full scrape for one opportunity: SAM description + all attachments/links + PIEE manifest.
    Returns (sam_raw, success). Incomplete records must not be treated as ready contracts.
    """
    from datetime import datetime, timezone

    from piee_client import attach_piee_manifest

    if is_sam_metadata_ready(raw):
        refreshed = attach_piee_manifest(dict(raw))
        if is_scrape_complete(refreshed):
            return refreshed, True
        return refreshed, True

    notice_id = str(raw.get("noticeId") or "")
    if not notice_id:
        failed = dict(raw)
        failed["scrapeStatus"] = "incomplete"
        failed["scrapeError"] = "missing_notice_id"
        return failed, False

    if not can_spend_sam(1):
        failed = dict(raw)
        failed["scrapeStatus"] = "incomplete"
        failed["scrapeError"] = "sam_budget_exhausted"
        return failed, False

    enriched = enrich_opportunity(raw, api_key)
    if enriched.get("scrapeStatus") == "incomplete":
        return enriched, False

    enriched = attach_piee_manifest(enriched)
    enriched["scrapeStatus"] = "metadata_ready"
    enriched["scrapedAt"] = datetime.now(timezone.utc).isoformat()
    enriched.pop("scrapeError", None)
    return enriched, True


def scrape_opportunities_batch(opportunities: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Scrape every contract in a SAM search batch before database upsert.
    Skips opportunities that cannot be fully scraped (e.g. SAM budget exhausted).
    """
    from api_budget import scrape_max_per_sync
    from sam_client import normalize_opportunity

    complete_rows: list[dict[str, Any]] = []
    skipped: list[str] = []
    budget_stopped = False
    max_scrape = scrape_max_per_sync()

    for opp in opportunities:
        raw = opp.get("sam_raw") if isinstance(opp.get("sam_raw"), dict) else {}
        notice_id = str(opp.get("notice_id") or raw.get("noticeId") or "")

        if budget_stopped:
            if notice_id:
                skipped.append(notice_id)
            continue

        if max_scrape > 0 and len(complete_rows) >= max_scrape:
            if notice_id:
                skipped.append(notice_id)
            continue

        enriched_raw, ok = scrape_opportunity_complete(raw)
        if not ok:
            if enriched_raw.get("scrapeError") == "sam_budget_exhausted":
                budget_stopped = True
            if notice_id:
                skipped.append(notice_id)
            continue

        row = dict(opp)
        row["sam_raw"] = enriched_raw
        if enriched_raw.get("descriptionText"):
            row["description"] = enriched_raw["descriptionText"][:8000]
        normalized = normalize_opportunity(enriched_raw)
        if normalized.get("location"):
            row["location"] = normalized["location"]
        complete_rows.append(row)

    return {
        "opportunities": complete_rows,
        "scraped_complete": len(complete_rows),
        "scraped_skipped": len(skipped),
        "skipped_notice_ids": skipped,
        "budget_stopped": budget_stopped,
    }


def ensure_enriched_sam_raw(
    contract: Any,
    *,
    api_key: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Return enriched sam_raw, refreshing from SAM.gov when needed."""
    raw = contract.sam_raw if isinstance(getattr(contract, "sam_raw", None), dict) else {}
    notice_id = str(getattr(contract, "notice_id", None) or raw.get("noticeId") or "")

    if force or needs_enrichment(raw):
        if not raw or not raw.get("noticeId"):
            fresh = fetch_opportunity_raw(notice_id, api_key) if notice_id else None
            raw = fresh or raw
        if raw:
            enriched, ok = scrape_opportunity_complete(raw, api_key)
            if ok:
                contract.sam_raw = enriched
                if enriched.get("descriptionText"):
                    contract.description = enriched["descriptionText"][:8000]
                raw = enriched
    return raw


def fetch_opportunity_raw(notice_id: str, api_key: str | None = None) -> dict[str, Any] | None:
    api_key = api_key or _api_key()
    if not notice_id or not api_key:
        return None
    response = _sam_api_get(
        SAM_SEARCH_URL,
        params={"api_key": api_key, "noticeid": notice_id, "limit": 1},
        timeout=60.0,
    )
    if not response:
        return None
    try:
        rows = response.json().get("opportunitiesData") or []
        return rows[0] if rows else None
    except Exception:
        return None
