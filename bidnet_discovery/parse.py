"""Parse BidNet authenticated/public solicitation search result HTML."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin

from discovery.deadline import normalize_deadline

SEARCH_RESULT_MARKERS = re.compile(
    r"mets-table-row|solicitation-link|simpleSolResults|Open\s+Solicitations",
    re.I,
)


def reported_total_from_html(html: str) -> int | None:
    m = re.search(
        r'class="simpleSolResultsNumResults"[^>]*>\s*([\d,]+)\s+results',
        html or "",
        re.I,
    )
    if m:
        try:
            return int(m.group(1).replace(",", ""))
        except ValueError:
            pass
    m = re.search(r"([\d,]+)\s+results", html or "", re.I)
    if m:
        try:
            return int(m.group(1).replace(",", ""))
        except ValueError:
            pass
    return None


def detect_category_restriction(html: str) -> bool:
    """True when UI clearly limits results to vendor profile categories."""
    blob = html or ""
    if re.search(
        r"(your\s+(selected\s+)?(naics|nigp|categor(?:y|ies))|"
        r"matching\s+your\s+(profile|preferences|categories)|"
        r"filtered\s+by\s+your\s+(naics|categories))",
        blob,
        re.I,
    ):
        return True
    return False


def parse_search_results_html(
    html: str,
    *,
    list_url: str,
    page_number: int = 1,
) -> list[dict[str, Any]]:
    """Parse current authenticated/public result rows into discovery records."""
    out: list[dict[str, Any]] = []
    rows = list(
        re.finditer(
            r'<tr[^>]*class="[^"]*mets-table-row[^"]*"[\s\S]*?</tr>',
            html or "",
            re.I,
        )
    )
    for idx, m in enumerate(rows):
        row = m.group(0)
        link = re.search(
            r'<a[^>]+href="([^"]+)"[^>]*class="[^"]*solicitation-link[^"]*"',
            row,
            re.I,
        ) or re.search(
            r'<a[^>]+class="[^"]*solicitation-link[^"]*"[^>]*href="([^"]+)"',
            row,
            re.I,
        )
        # Private supplier search often omits solicitation-link class
        if not link:
            link = re.search(
                r'<a[^>]+href="([^"]*solicitations/[^"]+)"[^>]*>',
                row,
                re.I,
            )
        if not link:
            link = re.search(r'<a[^>]+href="([^"]+)"[^>]*>', row, re.I)
        if not link:
            continue
        href = link.group(1).strip()
        if not href or href.lower().startswith("javascript:") or href.strip() == "#":
            continue
        if any(x in href.lower() for x in ("logout", "login", "javascript:")):
            continue

        title = None
        tm = re.search(r'class="rowTitle">\s*([^<]+?)\s*</span>', row, re.I)
        if tm:
            title = re.sub(r"\s+", " ", tm.group(1)).strip()
        if not title:
            tm2 = re.search(r'class="[^"]*solicitation-link[^"]*"[^>]*>([^<]{5,300})</a>', row, re.I)
            if tm2:
                title = re.sub(r"\s+", " ", tm2.group(1)).strip()
        if not title:
            for pat in (
                r'class="[^"]*title[^"]*"[^>]*>\s*([^<]{5,300})',
                r'class="[^"]*solicitationTitle[^"]*"[^>]*>\s*([^<]{5,300})',
                r'<a[^>]+href="[^"]*solicitations/[^"]+"[^>]*>\s*([^<]{5,300})\s*</a>',
                r"<td[^>]*>\s*<a[^>]+>\s*([^<]{5,300})\s*</a>",
                r"<td[^>]*>\s*([^<]{8,300})\s*</td>",
            ):
                tm3 = re.search(pat, row, re.I)
                if tm3:
                    cand = re.sub(r"\s+", " ", tm3.group(1)).strip()
                    if cand and cand.lower() not in {"view", "details", "open", "select"}:
                        title = cand
                        break
        if not title or title.lower() in {"open solicitations", "closed solicitations"}:
            continue

        location = None
        loc = re.search(r'class="location">\s*([^<]+?)\s*</span>', row, re.I)
        if loc:
            location = re.sub(r"\s+", " ", loc.group(1)).strip()

        published = None
        pm = re.search(
            r'class="publicationDate"[^>]*>[\s\S]*?class="dateValue">\s*([^<]+?)\s*</span>',
            row,
            re.I,
        )
        if pm:
            published = pm.group(1).strip()

        closing = None
        cm = re.search(
            r'class="closingDate[^"]*"[^>]*>[\s\S]*?class="dateValue">\s*([^<]+?)\s*</span>',
            row,
            re.I,
        )
        if cm:
            closing = cm.group(1).strip()

        member_agency = bool(
            re.search(r"Member Agency Bids|checkmarkCircle4|memberAgency", row, re.I)
        )
        purchasing_group = None
        pg = re.search(
            r'data-mets-tooltip="([^"]+)"',
            row,
            re.I,
        )
        if pg and "member" in pg.group(1).lower():
            purchasing_group = pg.group(1).strip()

        # Internal BidNet IDs from accessibility text / link id / URL tail
        internal_id = None
        hid = re.search(r'class="accessibility-hidden">\s*(\d{5,})\s*<', row, re.I)
        if hid:
            internal_id = hid.group(1)
        if not internal_id:
            idm = re.search(r'searchResultSol_solicitation_(\d+)', row, re.I)
            if idm:
                internal_id = idm.group(1)

        path_id = None
        pid = re.search(r"/(\d{7,})(?:\?|$)", href)
        if pid:
            path_id = pid.group(1)

        detail = urljoin(list_url, href)
        dl = normalize_deadline(closing) if closing else {}
        pub = normalize_deadline(published) if published else {}

        external_id = internal_id or path_id or title[:160]
        sol_num = None
        sn = re.search(
            r"\b((?:RFP|RFQ|IFB|ITB|BID)[\s#:]*[A-Z0-9][A-Z0-9\-_/]{2,})\b",
            title,
            re.I,
        )
        if sn:
            sol_num = sn.group(1)

        out.append(
            {
                "external_id": str(external_id)[:160],
                "source_id": "live_bidnet",
                "platform": "live_bidnet",
                "platform_family": "BidNet",
                "source_url": list_url,
                "detail_url": detail,
                "title": title,
                "agency": location,  # list shows location; issuing org comes from detail
                "location": location,
                "jurisdiction": location,
                "status": "OPEN",
                "solicitation_number": sol_num or (internal_id if internal_id else None),
                "solicitation_id": internal_id or path_id,
                "deadline_raw": closing,
                "deadline": dl.get("utc_deadline") or dl.get("parsed_local"),
                "deadline_timezone": dl.get("timezone"),
                "deadline_tz_confidence": dl.get("timezone_confidence"),
                "issue_date_raw": published,
                "issue_date": pub.get("utc_deadline") or pub.get("parsed_local"),
                "discovery_universe": "LIVE",
                "discovery_scope": "BROAD_PRODUCT_RESALE",
                "authoritative_url": detail,
                "raw_metadata": {
                    "platform": "BidNet",
                    "harvest_mode": "authenticated_search",
                    "member_agency": member_agency,
                    "purchasing_group": purchasing_group,
                    "bidnet_internal_id": internal_id,
                    "bidnet_path_id": path_id,
                    "result_href": href,
                    "result_page": page_number,
                    "result_position": idx,
                    "vendor_profile_codes_ignored": True,
                    "current_session_href": True,
                },
            }
        )

    # Fallback: solicitation-link anchors without table wrapper
    if not out:
        for idx, m in enumerate(
            re.finditer(
                r'<a[^>]+href="([^"]+)"[^>]*class="[^"]*solicitation-link[^"]*"[^>]*>'
                r'([\s\S]*?)</a>',
                html or "",
                re.I,
            )
        ):
            href = m.group(1).strip()
            inner = m.group(2)
            title_m = re.search(r'class="rowTitle">\s*([^<]+)', inner, re.I)
            title = re.sub(r"\s+", " ", (title_m.group(1) if title_m else re.sub(r"<[^>]+>", " ", inner))).strip()
            if len(title) < 5:
                continue
            detail = urljoin(list_url, href)
            pid = re.search(r"/(\d{7,})(?:\?|$)", href)
            out.append(
                {
                    "external_id": (pid.group(1) if pid else title)[:160],
                    "source_id": "live_bidnet",
                    "platform": "live_bidnet",
                    "platform_family": "BidNet",
                    "source_url": list_url,
                    "detail_url": detail,
                    "title": title[:300],
                    "status": "OPEN",
                    "discovery_universe": "LIVE",
                    "discovery_scope": "BROAD_PRODUCT_RESALE",
                    "authoritative_url": detail,
                    "raw_metadata": {
                        "platform": "BidNet",
                        "harvest_mode": "authenticated_search_fallback",
                        "result_href": href,
                        "result_page": page_number,
                        "result_position": idx,
                        "current_session_href": True,
                    },
                }
            )
    return out


def next_page_urls_from_html(html: str, *, list_url: str, current_page: int) -> list[str]:
    """Collect next-page candidate URLs from BidNet pagination controls."""
    candidates: list[str] = []
    # data-href on pagination options
    for m in re.finditer(
        r'data-page-number="(\d+)"[^>]*data-href="([^"]+)"',
        html or "",
        re.I,
    ):
        try:
            pnum = int(m.group(1))
        except ValueError:
            continue
        if pnum == current_page + 1:
            candidates.append(urljoin(list_url, m.group(2)))
    for m in re.finditer(
        r'data-href="([^"]+)"[^>]*data-page-number="(\d+)"',
        html or "",
        re.I,
    ):
        try:
            pnum = int(m.group(2))
        except ValueError:
            continue
        if pnum == current_page + 1:
            candidates.append(urljoin(list_url, m.group(1)))
    # link rel=next
    nm = re.search(r'rel="next"\s+href="([^"]+)"', html or "", re.I) or re.search(
        r'href="([^"]+)"\s+rel="next"', html or "", re.I
    )
    if nm:
        candidates.append(urljoin(list_url, nm.group(1)))
    # Synthetic /pageN for open-bids path pagination only.
    # Do NOT invent ?pageNumber= for private search — those URLs load without
    # changing result sets and falsely look like successful pagination.
    if "open-bids" in (list_url or "").lower():
        base = re.sub(r"/page\d+/?$", "", list_url.rstrip("/"), flags=re.I)
        candidates.append(f"{base}/page{current_page + 1}")
    # Dedup preserve order
    seen: set[str] = set()
    out: list[str] = []
    for u in candidates:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def parse_json_search_payload(data: Any, *, list_url: str) -> list[dict[str, Any]]:
    """Best-effort parse of BidNet XHR/JSON search payloads if present."""
    if data is None:
        return []
    items: list[Any] = []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        for key in ("results", "solicitations", "items", "content", "data", "hits"):
            if isinstance(data.get(key), list):
                items = data[key]
                break
        if not items and isinstance(data.get("response"), dict):
            return parse_json_search_payload(data["response"], list_url=list_url)
    out: list[dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        title = it.get("title") or it.get("noticeTitle") or it.get("name")
        href = (
            it.get("url")
            or it.get("href")
            or it.get("detailUrl")
            or it.get("solicitationUrl")
            or it.get("link")
        )
        if not title or not href:
            continue
        detail = urljoin(list_url, str(href))
        sid = it.get("id") or it.get("solicitationId") or it.get("noticeId")
        out.append(
            {
                "external_id": str(sid or title)[:160],
                "source_id": "live_bidnet",
                "platform": "live_bidnet",
                "platform_family": "BidNet",
                "source_url": list_url,
                "detail_url": detail,
                "title": str(title)[:300],
                "agency": it.get("buyer") or it.get("agency") or it.get("region"),
                "location": it.get("location") or it.get("region"),
                "status": "OPEN",
                "solicitation_number": it.get("solicitationNumber") or (str(sid) if sid else None),
                "solicitation_id": str(sid) if sid else None,
                "deadline_raw": it.get("closingDate") or it.get("closeDate"),
                "issue_date_raw": it.get("publicationDate") or it.get("publishDate"),
                "discovery_universe": "LIVE",
                "discovery_scope": "BROAD_PRODUCT_RESALE",
                "authoritative_url": detail,
                "raw_metadata": {
                    "platform": "BidNet",
                    "harvest_mode": "authenticated_json",
                    "current_session_href": True,
                    "vendor_profile_codes_ignored": True,
                },
            }
        )
    return out
