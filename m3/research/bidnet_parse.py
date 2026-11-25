"""BidNet Direct HTML parsers (offline) and the fail-closed acquisition gate.

Parsers restored from the retired application (``bidnet_discovery/parse.py`` and
``bidnet_recovery/parse_abstract.py`` at b579812^). They only read HTML that was already
retrieved; nothing here performs network I/O. Live BidNet acquisition has no client in M3 and
``bidnet_acquisition_allowed()`` stays False until the owner enables every control.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin

from m3.research.staged_inventory import parse_deadline, state_code

PROVIDER = "bidnet"


def bidnet_acquisition_allowed() -> dict[str, Any]:
    """All controls must pass: config flag, owner allowlist, and the global acquisition lock released."""
    from m3.config import get_settings
    from m3.research.nextbid_controller import acquisition_locked
    from m3.research.source_allowlist import can_execute_live_acquisition

    checks = {
        "bidnet_enabled": bool(get_settings().bidnet_enabled),
        "owner_allowlisted": bool(can_execute_live_acquisition(PROVIDER)),
        "acquisition_unlocked": not acquisition_locked(),
        "live_client_present": False,
    }
    return {"allowed": all(checks.values()), "checks": checks}


def _clean(s: str | None) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


def _deadline(raw: str | None, location: str | None) -> dict[str, Any]:
    if not raw:
        return {"deadline_at": None, "deadline_basis": "missing"}
    st = state_code(*(re.findall(r"\b([A-Z]{2})\b", location or "") or [location]))
    d = parse_deadline(raw, tz_name=None, state=st)
    return {**d, "deadline_at": d["deadline_at"].isoformat() if d.get("deadline_at") else None}


def reported_total_from_html(html: str) -> int | None:
    m = (re.search(r'class="simpleSolResultsNumResults"[^>]*>\s*([\d,]+)\s+results', html or "", re.I)
         or re.search(r"([\d,]+)\s+results", html or "", re.I)
         or re.search(r"\b([\d,]+)\s+Open\s+Solicitations\b", html or "", re.I))
    return int(m.group(1).replace(",", "")) if m else None


def detect_category_restriction(html: str) -> bool:
    """True when the result list is limited to the vendor profile's categories."""
    return bool(re.search(
        r"your\s+(?:selected\s+)?(?:naics|nigp|categor(?:y|ies))|matching\s+your\s+(?:profile|preferences|categories)"
        r"|filtered\s+by\s+your\s+(?:naics|categories)", html or "", re.I))


def _row_field(row: str, cls: str, alt_cls: str | None = None) -> str | None:
    m = re.search(rf'class="{cls}[^"]*"[^>]*>[\s\S]*?class="dateValue">\s*([^<]+?)\s*</span>', row, re.I)
    if not m and alt_cls:
        # State listing layout: <span class="sol-closing-date open"> … <span class="date-value">12/16/2026</span>
        m = re.search(rf'class="{alt_cls}[^"]*"[^>]*>[\s\S]*?class="date-value">\s*([^<]+?)\s*</span>', row, re.I)
    return m.group(1).strip() if m else None


def parse_search_results_html(html: str, *, list_url: str, page_number: int = 1) -> list[dict[str, Any]]:
    """Result rows from a BidNet solicitation list page."""
    out: list[dict[str, Any]] = []
    for idx, m in enumerate(re.finditer(r'<tr[^>]*class="[^"]*mets-table-row[^"]*"[\s\S]*?</tr>', html or "", re.I)):
        row = m.group(0)
        link = (re.search(r'<a[^>]+href="([^"]+)"[^>]*class="[^"]*solicitation-link', row, re.I)
                or re.search(r'<a[^>]+class="[^"]*solicitation-link[^"]*"[^>]*href="([^"]+)"', row, re.I)
                or re.search(r'<a[^>]+href="([^"]*solicitations/[^"]+)"', row, re.I))
        if not link:
            continue
        href = link.group(1).strip()
        if not href or href == "#" or any(x in href.lower() for x in ("javascript:", "logout", "login")):
            continue
        tm = re.search(r'class="rowTitle">\s*([^<]+?)\s*</span>', row, re.I) or re.search(
            r'class="[^"]*solicitation-link[^"]*"[^>]*>\s*([^<]{2,300}?)\s*</a>', row, re.I)
        title = _clean(tm.group(1)) if tm else None
        if not title or title.lower() in {"open solicitations", "closed solicitations"}:
            continue
        loc = re.search(r'class="location">\s*([^<]+?)\s*</span>', row, re.I) or re.search(
            r'class="sol-region-item">\s*([^<]+?)\s*</span>', row, re.I)
        location = _clean(loc.group(1)) if loc else None
        closing = _row_field(row, "closingDate", "sol-closing-date")
        published = _row_field(row, "publicationDate", "sol-publication-date")
        hid = re.search(r'class="accessibility-hidden">\s*(\d{5,})\s*<', row, re.I) or re.search(
            r"searchResultSol_solicitation_(\d+)", row, re.I)
        pid = re.search(r"/(\d{7,})(?:/abstract)?(?:[?/]|$)", href)
        event_id = (hid.group(1) if hid else None) or (pid.group(1) if pid else None)
        pg = re.search(r'data-mets-tooltip="([^"]+)"', row, re.I)
        out.append({
            "provider": PROVIDER,
            "provider_record_id": event_id,
            "title": title,
            "location": location,
            "official_url": urljoin(list_url, href),
            "deadline_raw": closing,
            **_deadline(closing, location),
            "posted_raw": published,
            "member_agency": bool(re.search(r"Member Agency Bids|checkmarkCircle4|memberAgency", row, re.I)),
            "purchasing_group": pg.group(1).strip() if pg and "member" in pg.group(1).lower() else None,
            "result_page": page_number,
            "result_position": idx,
        })
    return out


def next_page_urls_from_html(html: str, *, list_url: str, current_page: int) -> list[str]:
    cands: list[str] = []
    for pat, num_g, href_g in ((r'data-page-number="(\d+)"[^>]*data-href="([^"]+)"', 1, 2),
                               (r'data-href="([^"]+)"[^>]*data-page-number="(\d+)"', 2, 1)):
        for m in re.finditer(pat, html or "", re.I):
            if int(m.group(num_g)) == current_page + 1:
                cands.append(urljoin(list_url, m.group(href_g)))
    nm = re.search(r'rel="next"\s+href="([^"]+)"', html or "", re.I) or re.search(r'href="([^"]+)"\s+rel="next"', html or "", re.I)
    if nm:
        cands.append(urljoin(list_url, nm.group(1)))
    # Only the open-bids path paginates by /pageN; query-string paging silently repeats page 1
    if "open-bids" in (list_url or "").lower():
        cands.append(f"{re.sub(r'/page\d+/?$', '', list_url.rstrip('/'), flags=re.I)}/page{current_page + 1}")
    return list(dict.fromkeys(u for u in cands if u))


def _field_body(html: str, label: str) -> str | None:
    m = re.search(re.escape(label) + r'</span>\s*<div class="mets-field-body[^"]*">([\s\S]*?)</div>', html or "", re.I)
    if not m or re.search(r"member-only-info|registered members only", m.group(1), re.I):
        return None
    return _clean(re.sub(r"<br\s*/?>", "\n", m.group(1), flags=re.I)) or None


def _locked(html: str, label: str) -> bool:
    if re.search(r'class="[^"]*locked[^"]*mets-field[^"]*"[\s\S]{0,200}?' + re.escape(label) + r"[\s\S]{0,400}?member-only-info",
                 html or "", re.I):
        return True
    m = re.search(re.escape(label) + r'</span>\s*<div class="mets-field-body[^"]*">([\s\S]{0,800}?)</div>', html or "", re.I)
    return bool(m and re.search(r"member-only-info|registered members only", m.group(1), re.I))


_DOC_PATTERNS = (
    ("attachment", r'href="([^"]+\.(?:pdf|docx?|xlsx?|csv|zip)(?:\?[^"]*)?)"'),
    ("download_endpoint", r'href=["\']([^"\']*(?:downloadDocument|getDocument|getFile|documentId|fileId|SolicitationDocument|/download|/attachment)[^"\']*)["\']'),
    ("data_attr", r'data-(?:download-url|file-url|document-url)=["\']([^"\']+)["\']'),
)


def parse_bidnet_abstract(html: str, *, detail_url: str | None = None) -> dict[str, Any]:
    """Public abstract page: title, location, dates and overview; org, number, description and
    documents are usually member-gated and reported in ``locked_fields``."""
    body = html or ""
    hm = re.search(r"<h1[^>]*>([^<]{5,300})</h1>", body, re.I)
    tm = re.search(r"<title>([^<]+)</title>", body, re.I)
    title = _clean(hm.group(1)) if hm else (re.sub(r"\s*[-|].*$", "", tm.group(1)).strip() if tm else None)
    om = re.search(r'id="ai-public-overview-content"[^>]*>\s*([^<]{20,2500})\s*</div>', body, re.I)
    overview = _clean(om.group(1)) if om else None
    location = _field_body(body, "Location")
    closing = _field_body(body, "Closing Date")
    docs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for kind, pat in _DOC_PATTERNS:
        for m in re.finditer(pat, body, re.I):
            href = m.group(1)
            if href.startswith("#") or re.search(r"javascript:|login|logout|register|captcha", href, re.I):
                continue
            url = urljoin(detail_url or "", href)
            if url.startswith("http") and url not in seen:
                seen.add(url)
                docs.append({"document_type": kind, "document_name": href.rsplit("/", 1)[-1][:160], "document_url": url,
                             "retrieval_status": "URL_DISCOVERED"})
    locked = [lab for lab in ("Issuing Organization", "Solicitation Number", "Description", "Source") if _locked(body, lab)]
    return {
        "provider": PROVIDER,
        "title": title,
        "description": _field_body(body, "Description") or overview,
        "overview": overview,
        "location": location,
        "posted_raw": _field_body(body, "Publication Date"),
        "deadline_raw": closing,
        **_deadline(closing, location),
        "buyer": _field_body(body, "Issuing Organization"),
        "solicitation_number": _field_body(body, "Solicitation Number"),
        "documents": docs,
        "locked_fields": locked,
        "auth_wall": bool(locked) or bool(re.search(r"Registered members only|Get instant access|abstractRegisterNowButton", body, re.I)),
        "parse_ok": bool(title or closing or overview),
    }
