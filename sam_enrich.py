"""Fetch full SAM.gov opportunity details: description, links, document access."""

from __future__ import annotations

import os
import re
from html import unescape
from typing import Any

import httpx

from usaspending_client import STATE_NAME_TO_CODE, normalize_state

NOTICE_DESC_URL = "https://api.sam.gov/prod/opportunities/v1/noticedesc"
SAM_SEARCH_URL = "https://api.sam.gov/opportunities/v2/search"
SAM_LINKS_URL = "https://sam.gov/api/prod/file-services/v1/opps/{notice_id}/links"

EXTERNAL_PORTAL_PATTERNS: list[tuple[str, str]] = [
    ("PIEE", r"\bPIEE\b"),
    ("FedConnect", r"FedConnect|fedconnect\.net"),
    ("NECO", r"\bNECO\b"),
    ("DIBBS", r"\bDIBBS\b"),
    ("beta.sam.gov", r"beta\.sam\.gov"),
]

STATE_NAME_PATTERN = "|".join(
    sorted((re.escape(name.title()) for name in STATE_NAME_TO_CODE if len(name) > 2), key=len, reverse=True)
)


def _api_key() -> str:
    return os.getenv("SAM_GOV_API_KEY", "").strip()


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


def fetch_notice_description(notice_id: str, api_key: str | None = None) -> str | None:
    api_key = api_key or _api_key()
    if not notice_id or not api_key:
        return None
    try:
        with httpx.Client(timeout=45.0) as client:
            response = client.get(
                NOTICE_DESC_URL,
                params={"noticeid": notice_id, "api_key": api_key},
            )
            response.raise_for_status()
            payload = response.json()
            description = payload.get("description")
            return str(description) if description else None
    except Exception:
        return None


def fetch_opportunity_links(notice_id: str, api_key: str | None = None) -> list[dict[str, Any]]:
    """Best-effort fetch of SAM.gov link resources (PIEE, etc.)."""
    api_key = api_key or _api_key()
    if not notice_id:
        return []
    url = SAM_LINKS_URL.format(notice_id=notice_id)
    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.get(url, params={"api_key": api_key} if api_key else None)
            if response.status_code != 200 or not response.text.strip():
                return []
            data = response.json()
    except Exception:
        return []

    links: list[dict[str, Any]] = []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("links") or data.get("opportunityLinks") or data.get("_embedded", {}).get("links") or []
        if isinstance(items, dict):
            items = items.get("links") or []
    else:
        items = []

    for item in items:
        if isinstance(item, str):
            links.append({"url": item, "label": item})
        elif isinstance(item, dict):
            url = item.get("url") or item.get("href") or item.get("link")
            if url:
                links.append(
                    {
                        "url": url,
                        "label": item.get("description") or item.get("name") or item.get("title") or url,
                    }
                )
    return links


def fetch_opportunity_raw(notice_id: str, api_key: str | None = None) -> dict[str, Any] | None:
    api_key = api_key or _api_key()
    if not notice_id or not api_key:
        return None
    try:
        with httpx.Client(timeout=60.0) as client:
            response = client.get(
                SAM_SEARCH_URL,
                params={"api_key": api_key, "noticeid": notice_id, "limit": 1},
            )
            response.raise_for_status()
            rows = response.json().get("opportunitiesData") or []
            return rows[0] if rows else None
    except Exception:
        return None


def build_document_access(
    raw: dict[str, Any],
    *,
    description_text: str,
    opportunity_links: list[dict[str, Any]],
) -> dict[str, Any]:
    resource_links = raw.get("resourceLinks") or []
    pdf_count = len(resource_links) if isinstance(resource_links, list) else 0
    link_count = len(opportunity_links)
    external_portals = detect_external_portals(
        description_text,
        raw.get("title"),
        " ".join(str(link.get("label") or "") for link in opportunity_links),
    )
    requires_external = bool(external_portals) or (pdf_count == 0 and link_count > 0)

    if pdf_count:
        status = "pdf_attachments"
        summary = f"{pdf_count} PDF attachment(s) available on SAM.gov."
    elif link_count:
        status = "external_links"
        summary = (
            f"No direct PDF attachments on SAM.gov — {link_count} external link(s) listed "
            f"(often PIEE or another portal where the SOW lives)."
        )
    elif external_portals:
        status = "external_portal"
        portal_text = ", ".join(external_portals)
        summary = (
            f"No downloadable PDFs on SAM.gov. Solicitation documents are on {portal_text} - "
            "open the SAM.gov posting and use the Attachments/Links section."
        )
    else:
        status = "none_found"
        summary = "No PDF attachments or external links found on SAM.gov yet."

    return {
        "status": status,
        "summary": summary,
        "pdf_attachment_count": pdf_count,
        "external_link_count": link_count,
        "external_portals": external_portals,
        "requires_external_portal": requires_external,
        "sam_gov_link": raw.get("uiLink"),
        "solicitation_number": raw.get("solicitationNumber"),
    }


def enrich_opportunity(raw: dict[str, Any] | None, api_key: str | None = None) -> dict[str, Any]:
    """Merge search metadata with full description text and link/document access info."""
    if not raw:
        return {}
    notice_id = str(raw.get("noticeId") or "")
    description_field = raw.get("description")
    description_html: str | None = None

    if isinstance(description_field, str) and description_field.startswith("http"):
        description_html = fetch_notice_description(notice_id, api_key)
    elif isinstance(description_field, str) and description_field.strip():
        description_html = description_field

    description_text = html_to_text(description_html)
    opportunity_links = fetch_opportunity_links(notice_id, api_key)
    work_states = extract_states_from_text(
        raw.get("title"),
        _place_of_performance_text(raw),
        description_text,
    )
    document_access = build_document_access(
        raw,
        description_text=description_text,
        opportunity_links=opportunity_links,
    )

    enriched = dict(raw)
    enriched["descriptionHtml"] = description_html
    enriched["descriptionText"] = description_text
    enriched["opportunityLinks"] = opportunity_links
    enriched["workStates"] = work_states
    enriched["documentAccess"] = document_access
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


def needs_enrichment(sam_raw: dict[str, Any] | None) -> bool:
    if not isinstance(sam_raw, dict) or not sam_raw:
        return True
    if not sam_raw.get("descriptionText"):
        return True
    if "documentAccess" not in sam_raw:
        return True
    return False


def ensure_enriched_sam_raw(
    contract: Any,
    *,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Return enriched sam_raw, refreshing from SAM.gov when needed."""
    raw = contract.sam_raw if isinstance(getattr(contract, "sam_raw", None), dict) else {}
    if needs_enrichment(raw):
        notice_id = getattr(contract, "notice_id", None)
        fresh = fetch_opportunity_raw(str(notice_id or ""), api_key) if notice_id else None
        raw = fresh or raw
    if raw:
        raw = enrich_opportunity(raw, api_key)
        contract.sam_raw = raw
    return raw
