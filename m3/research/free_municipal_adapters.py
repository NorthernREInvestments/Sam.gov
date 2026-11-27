"""Free municipal/state portals with public solicitation packages (no login).

Erie NY, Worcester MA, Cleveland OH, Jefferson TX, Alaska OPN.
Stepped pipeline adapters — list + public docs only; no portal fighting.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

from m3.research.http_util import fetch_text

DOC_EXTS = (".pdf", ".doc", ".docx", ".xlsx", ".xls", ".zip", ".xlsm")


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _key(*parts: str) -> str:
    raw = "|".join(p.strip().lower() for p in parts if p)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _guess_category(title: str) -> str | None:
    t = (title or "").lower()
    mapping = [
        ("furniture", r"furniture|desk|chair"),
        ("janitorial", r"janitorial|custodial|cleaning"),
        ("it_hardware", r"laptop|computer|server|printer"),
        ("electrical", r"electrical|transformer|conduit|lighting"),
        ("automotive", r"batter(?:y|ies)|automotive|vehicle|tire"),
        ("medical", r"medical|glove|pharmaceutical|ultrasonic"),
        ("safety", r"cone|safety|ppe|guardrail"),
        ("apparel", r"shirt|pant|uniform|polo"),
        ("appliances", r"microwave|washer|dryer|appliance"),
        ("fuel", r"fuel|gasoline|diesel"),
        ("building_materials", r"lumber|roofing|insulation|siding|aggregate"),
    ]
    for name, pat in mapping:
        if re.search(pat, t, re.I):
            return name
    return None


def _is_doc(url: str) -> bool:
    low = (url or "").lower()
    path = low.split("?", 1)[0]
    return (
        any(path.endswith(ext) for ext in DOC_EXTS)
        or "attachment.aspx" in low
        or "filedownload.aspx" in low
        or "/resource-manager/view/" in low
        or "/documentcenter/view/" in low
        or "finalsite.net/images/" in low
        or "finalsite.net/files/" in low
        or "/osp/lapac/agency/pdf/" in low
    )


def _strip(lab: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", lab or "")).strip()


def _item_base(
    *,
    source_key: str,
    title: str,
    sol: str,
    buyer: str,
    state: str,
    official_url: str,
    discovery_url: str,
    doc_urls: list[str],
    deadline: str | None = None,
    tz: str = "America/New_York",
    detail_url: str | None = None,
) -> dict[str, Any]:
    primary = official_url
    skip_name = re.compile(
        r"(?i)(addend|amend|tabulat|sign[-_]?in|instruction|w-?9|"
        r"cori|compliance|affidavit|insurance|bidder.?info|check.?list|checklist|"
        r"cover.?sheet|acknowledg|mwbe|wage.?rate|prevailing.?wage|osha|equal.?opp|"
        r"non[-_]?collusion|reference.?form|vendor.?reg|reap.?program|wage.?theft|"
        r"std[_-]?terms|terms[_-]?and[_-]?conditions|po[_-]?terms)"
    )
    prefer_name = re.compile(
        r"(?i)(solicitation|invitation|itb|rfb|rfp|rfq|ifb|specs?|specification|package|"
        r"bid[-_ ]?doc|sealed.?bid|form[-_]?30b|gloves|supplies|materials|"
        r"bid_opportunities|electrochemical|laundry|leather|electrical)"
    )
    ranked: list[str] = []
    for u in doc_urls:
        path = unquote(urlparse(u).path).lower()
        name = path.split("/")[-1]
        if not (name.endswith((".pdf", ".docx", ".doc")) or "attachment.aspx" in u.lower()):
            continue
        # Worcester /bids/ FORM-30B sealed packages are the solicitation itself.
        sealed_pkg = bool(re.search(r"(?i)form[-_]?30b", name) and "/bids/" in path)
        if "/document-center/" in path:
            continue
        if skip_name.search(name) and not sealed_pkg:
            continue
        if re.search(r"(?i)addend|amend", name):
            continue
        ranked.append(u)

    def _rank(u: str) -> tuple:
        path = unquote(urlparse(u).path).lower()
        in_bids = 0 if any(
            m in path
            for m in (
                "/bids/",
                "/nfb/",
                "/fin/dps/bid/",
                "/bid_opportunities/",
                "/media/",
                "/solicitationdocument/",
            )
        ) else 1
        preferred = 0 if prefer_name.search(path) else 1
        return (in_bids, preferred, u)

    ranked.sort(key=_rank)
    if ranked:
        primary = ranked[0]
    elif doc_urls:
        primary = doc_urls[0]
    return {
        "external_key": _key(source_key, sol or primary, title),
        "title": title[:500],
        "solicitation_number": sol or None,
        "buyer_name": buyer[:255],
        "geo_state": state,
        "geo_city": None,
        "geo_location": state,
        "deadline_at": deadline,
        "deadline_timezone": tz,
        "posted_at": None,
        "procurement_type": "open_bid",
        "product_category": _guess_category(title) or "unknown",
        "nigp_category": None,
        "set_aside": "unknown",
        "set_aside_verified": False,
        "estimated_value_min": None,
        "estimated_value_max": None,
        "estimated_value_unknown": True,
        "value_estimate_method": None,
        "value_evidence": None,
        "value_evidence_class": "not_yet_researched",
        "official_url": primary[:2048],
        "discovery_url": discovery_url[:2048],
        "detail_url": (detail_url or official_url)[:2048],
        "documents_available": "yes" if doc_urls else "unknown",
        "document_urls": doc_urls,
        "quantity": None,
        "requirement_class": "pending_classification",
        "classification_evidence": ["deferred_to_central_classifier"],
        "solicitation_status": "open",
        "source_key": source_key,
        "source_verified_at": _utc(),
        "source_purpose": "individual_solicitation_candidate",
        "discovery_stage": "unrestricted",
        "verified_individual_solicitation": True,
        "record_type": "INDIVIDUAL_SOLICITATION",
        "summary": title,
        "evidence": {
            "detail_url": detail_url or official_url,
            "live_listing": True,
            "documents_linked": len(doc_urls),
        },
    }


def _serviceish(title: str) -> bool:
    from m3.research.nextbid_owner_filters import listing_skip_before_detail_reason

    if listing_skip_before_detail_reason(title):
        return True
    return bool(
        re.search(
            r"(?i)\b("
            r"construction|reconstruction|renovation|abatement|remediation|"
            r"paving(?!\s+mix)|asphalt\s+repair|guardrail\s+repair|"
            r"maintenance\s+&\s+repair|elevator|travel\s+agency|"
            r"consulting|staffing|management\s+services|engineering\s+services|"
            r"camera\s+installation|cabling\s+retrofit|ahu\s+replacement|"
            r"towing\s+service|it\s+maintenance"
            r")\b",
            title or "",
        )
    )


def _docs_from_urls(urls: list[str], *, platform: str) -> list[dict[str, Any]]:
    out = []
    seen: set[str] = set()
    for u in urls:
        if not u or u in seen or not _is_doc(u):
            continue
        seen.add(u)
        name = unquote(urlparse(u).path.split("/")[-1]) or "document.bin"
        if "attachment.aspx" in u.lower():
            name = f"attachment_{urlparse(u).query.split('id=')[-1][:12]}.pdf"
        if "filedownload.aspx" in u.lower():
            m_orig = re.search(r"(?i)(?:\?|&)OriginalFileName=([^&]+)", u)
            m_file = re.search(r"(?i)(?:\?|&)file=([^&]+)", u)
            if m_orig and m_orig.group(1).strip():
                name = unquote(m_orig.group(1).replace("+", " ")).strip()
            elif m_file:
                name = unquote(m_file.group(1)).rsplit("/", 1)[-1]
        is_am = bool(re.search(r"(?i)addend|amend", f"{name} {u}"))
        out.append(
            {
                "source_url": u,
                "original_filename": name[:500],
                "document_title": name[:500],
                "document_type": "amendment" if is_am else "solicitation_notice",
                "is_amendment": is_am,
                "meta": {"platform": platform},
            }
        )
    return out


# --- Erie County NY -----------------------------------------------------------------

ERIE_LIST_URL = "https://www3.erie.gov/purchasing/retrieve-bids"


def parse_erie_ny_listing_html(html: str, *, list_url: str = ERIE_LIST_URL) -> list[dict[str, Any]]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "html.parser")
    table = soup.find("table")
    if not table:
        return []
    rows_out: list[dict[str, Any]] = []
    for tr in table.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if len(cells) < 7:
            continue
        texts = [_strip(c.get_text(" ", strip=True)) for c in cells]
        if texts[0].lower() == "type" or "bid number" in texts[1].lower():
            continue
        sol = texts[1]
        title = texts[3] or texts[4]
        deadline = texts[5]
        docs: list[str] = []
        for a in tr.find_all("a", href=True):
            abs_u = urljoin(list_url, a["href"])
            if _is_doc(abs_u):
                docs.append(abs_u)
        if not sol or not docs:
            continue
        rows_out.append(
            {
                "solicitation_number": sol,
                "title": title,
                "deadline_at": deadline,
                "detail_url": docs[0],
                "document_urls": list(dict.fromkeys(docs)),
            }
        )
    return rows_out


def search_erie_ny(source: dict, *, limit: int | None = None, fetch_detail_docs: bool = True) -> dict[str, Any]:
    cfg = source.get("config") or {}
    list_url = str(cfg.get("list_url") or ERIE_LIST_URL)
    try:
        status, final, html = fetch_text(list_url, timeout=35)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "items": [], "source_key": source.get("source_key"), "errors": [str(exc)], "stats": {}}
    if status >= 400 or not html:
        return {"ok": False, "items": [], "source_key": source.get("source_key"), "errors": [f"status={status}"], "stats": {}}
    rows = parse_erie_ny_listing_html(html, list_url=final or list_url)
    items: list[dict] = []
    skipped = 0
    for row in rows:
        if limit is not None and len(items) >= int(limit):
            break
        title = row["title"]
        if _serviceish(title):
            skipped += 1
            continue
        items.append(
            _item_base(
                source_key=source["source_key"],
                title=title,
                sol=row["solicitation_number"],
                buyer="Erie County NY Purchasing Division",
                state="NY",
                official_url=row["detail_url"],
                discovery_url=final or list_url,
                doc_urls=row["document_urls"],
                deadline=row.get("deadline_at"),
                detail_url=row["detail_url"],
            )
        )
    return {
        "ok": bool(items) or bool(rows),
        "items": items,
        "source_key": source.get("source_key"),
        "errors": [],
        "verified_at": _utc(),
        "pages_fetched": 1,
        "rows_examined": len(rows),
        "pagination_complete": True,
        "more_available": False,
        "stats": {"accepted": len(items), "skipped": skipped, "listing_rows": len(rows)},
    }


def discover_erie_ny_documents(detail_url: str, html: str = "", *, extra_urls: list[str] | None = None) -> list[dict[str, Any]]:
    return _docs_from_urls([detail_url, *(extra_urls or [])], platform="erie_ny")


# --- Worcester MA -------------------------------------------------------------------

WORCESTER_LIST_URL = "https://www.worcesterma.gov/finance/purchasing-bids/bids/open-bids"


def parse_worcester_listing_html(html: str, *, list_url: str = WORCESTER_LIST_URL) -> list[dict[str, Any]]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "html.parser")
    rows_out: list[dict[str, Any]] = []
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if len(cells) < 3:
            continue
        texts = [_strip(c.get_text(" ", strip=True)) for c in cells]
        if texts[0].lower() in {"bid number", "bid #"}:
            continue
        sol = texts[0]
        title = texts[1]
        deadline = texts[2] if len(texts) > 2 else None
        if not sol or not title or len(sol) < 3:
            continue
        # Detail slug from link if present
        detail = None
        for a in tr.find_all("a", href=True):
            href = a["href"]
            if "/bids/" in href and "open-bids" not in href:
                detail = urljoin(list_url, href)
                break
        if not detail:
            slug = sol.lower().replace(" ", "")
            detail = urljoin(list_url, f"/finance/purchasing-bids/bids/{slug}")
        rows_out.append(
            {
                "solicitation_number": sol,
                "title": title,
                "deadline_at": deadline,
                "detail_url": detail,
                "document_urls": [],
            }
        )
    return rows_out


def _worcester_detail_docs(detail_url: str) -> list[str]:
    try:
        st, fin, html = fetch_text(detail_url, timeout=30)
    except Exception:
        return []
    if st >= 400 or not html:
        return []
    docs = []
    for href, _lab in re.findall(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</a>', html or "", flags=re.I):
        abs_u = urljoin(fin or detail_url, href)
        if _is_doc(abs_u) and "worcesterma.gov" in abs_u:
            docs.append(abs_u)
    return list(dict.fromkeys(docs))


def search_worcester_ma(source: dict, *, limit: int | None = None, fetch_detail_docs: bool = True) -> dict[str, Any]:
    cfg = source.get("config") or {}
    list_url = str(cfg.get("list_url") or WORCESTER_LIST_URL)
    try:
        status, final, html = fetch_text(list_url, timeout=35)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "items": [], "source_key": source.get("source_key"), "errors": [str(exc)], "stats": {}}
    if status >= 400 or not html:
        return {"ok": False, "items": [], "source_key": source.get("source_key"), "errors": [f"status={status}"], "stats": {}}
    rows = parse_worcester_listing_html(html, list_url=final or list_url)
    items: list[dict] = []
    skipped = 0
    for row in rows:
        if limit is not None and len(items) >= int(limit):
            break
        title = row["title"]
        if _serviceish(title):
            skipped += 1
            continue
        docs = _worcester_detail_docs(row["detail_url"]) if fetch_detail_docs else []
        if fetch_detail_docs and not docs:
            skipped += 1
            continue
        items.append(
            _item_base(
                source_key=source["source_key"],
                title=title,
                sol=row["solicitation_number"],
                buyer="City of Worcester Purchasing",
                state="MA",
                official_url=(docs[0] if docs else row["detail_url"]),
                discovery_url=final or list_url,
                doc_urls=docs,
                deadline=row.get("deadline_at"),
                detail_url=row["detail_url"],
            )
        )
    return {
        "ok": bool(items) or bool(rows),
        "items": items,
        "source_key": source.get("source_key"),
        "errors": [],
        "verified_at": _utc(),
        "pages_fetched": 1,
        "rows_examined": len(rows),
        "pagination_complete": True,
        "more_available": False,
        "stats": {"accepted": len(items), "skipped": skipped, "listing_rows": len(rows)},
    }


def discover_worcester_ma_documents(detail_url: str, html: str = "", *, extra_urls: list[str] | None = None) -> list[dict[str, Any]]:
    urls = list(extra_urls or [])
    if detail_url and not _is_doc(detail_url):
        urls.extend(_worcester_detail_docs(detail_url))
    elif detail_url:
        urls.append(detail_url)
    return _docs_from_urls(urls, platform="worcester_ma")


# --- Cleveland OH -------------------------------------------------------------------

CLEVELAND_LIST_URL = "https://www.clevelandohio.gov/city-hall/departments/finance/invitations-bid"


def parse_cleveland_listing_html(html: str, *, list_url: str = CLEVELAND_LIST_URL) -> list[dict[str, Any]]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "html.parser")
    by_sol: dict[str, dict[str, Any]] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        abs_u = urljoin(list_url, href)
        if "/fin/dps/bid/" not in abs_u.lower() or not abs_u.lower().endswith(".pdf"):
            continue
        parent = a.find_parent(["article", "div", "section", "li", "td"])
        ctx = _strip(parent.get_text(" ", strip=True)) if parent else ""
        m = re.search(r"(?i)File\s+Number:\s*([0-9A-Z\-]+)", ctx)
        sol = m.group(1) if m else None
        if not sol:
            # ITB_132-26.pdf / 656RFB2026.pdf / 129-26_Addendum1.pdf
            name = unquote(urlparse(abs_u).path.split("/")[-1])
            m2 = re.search(r"(?i)(?:ITB_|RFB)?(\d{2,4}(?:-\d{2})?)", name)
            sol = m2.group(1) if m2 else name
        title_m = re.search(r"(?i)Description:\s*(.+?)(?:Closing|Deadline|Bid Document|Addendum|$)", ctx)
        title = title_m.group(1).strip() if title_m else _strip(a.get_text(" ", strip=True))
        if title.lower() in {"bid document", "addendum", "addendum 1"}:
            title = ctx[:200] if ctx else sol
        deadline_m = re.search(r"(?i)(?:Closing|Deadline|Due)[:\s]+([A-Za-z0-9,\s/\-]+?\d{4})", ctx)
        entry = by_sol.setdefault(
            sol,
            {"solicitation_number": sol, "title": title[:500], "deadline_at": None, "document_urls": [], "detail_url": abs_u},
        )
        if title and len(title) > len(entry["title"]):
            entry["title"] = title[:500]
        if deadline_m:
            entry["deadline_at"] = deadline_m.group(1).strip()
        entry["document_urls"].append(abs_u)
    rows = []
    for sol, entry in by_sol.items():
        entry["document_urls"] = list(dict.fromkeys(entry["document_urls"]))
        # Prefer non-addendum as detail
        for u in entry["document_urls"]:
            if "addendum" not in u.lower():
                entry["detail_url"] = u
                break
        rows.append(entry)
    return rows


def search_cleveland_oh(source: dict, *, limit: int | None = None, fetch_detail_docs: bool = True) -> dict[str, Any]:
    cfg = source.get("config") or {}
    list_url = str(cfg.get("list_url") or CLEVELAND_LIST_URL)
    try:
        status, final, html = fetch_text(list_url, timeout=40)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "items": [], "source_key": source.get("source_key"), "errors": [str(exc)], "stats": {}}
    if status >= 400 or not html:
        return {"ok": False, "items": [], "source_key": source.get("source_key"), "errors": [f"status={status}"], "stats": {}}
    rows = parse_cleveland_listing_html(html, list_url=final or list_url)
    items: list[dict] = []
    skipped = 0
    for row in rows:
        if limit is not None and len(items) >= int(limit):
            break
        title = row["title"]
        if _serviceish(title) or re.search(r"(?i)\b(forestry|park|construction|concrete)\b", title):
            skipped += 1
            continue
        items.append(
            _item_base(
                source_key=source["source_key"],
                title=title,
                sol=row["solicitation_number"],
                buyer="City of Cleveland Department of Finance",
                state="OH",
                official_url=row["detail_url"],
                discovery_url=final or list_url,
                doc_urls=row["document_urls"],
                deadline=row.get("deadline_at"),
                tz="America/New_York",
                detail_url=row["detail_url"],
            )
        )
    return {
        "ok": bool(items) or bool(rows),
        "items": items,
        "source_key": source.get("source_key"),
        "errors": [],
        "verified_at": _utc(),
        "pages_fetched": 1,
        "rows_examined": len(rows),
        "pagination_complete": True,
        "more_available": False,
        "stats": {"accepted": len(items), "skipped": skipped, "listing_rows": len(rows)},
    }


def discover_cleveland_oh_documents(detail_url: str, html: str = "", *, extra_urls: list[str] | None = None) -> list[dict[str, Any]]:
    return _docs_from_urls([detail_url, *(extra_urls or [])], platform="cleveland_oh")


# --- Jefferson County TX ------------------------------------------------------------

JEFFERSON_LIST_URL = "https://jeffersoncountytx.gov/Purchasing/NoticesForBid"


def parse_jefferson_tx_listing_html(html: str, *, list_url: str = JEFFERSON_LIST_URL) -> list[dict[str, Any]]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "html.parser")
    table = soup.find("table")
    if not table:
        return []
    rows_out: list[dict[str, Any]] = []
    for tr in table.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 3:
            continue
        texts = [_strip(c.get_text(" ", strip=True)) for c in cells]
        if texts[0].lower().startswith("number") or "notices for bid" in texts[0].lower():
            continue
        # First cell embeds IFB number + links (e.g. IFB 26-079/MR or bare 26-079/MR)
        sol_m = re.search(
            r"(?i)\b((?:IFB|RFP|RFQ|BID)\s*[\d\-]+(?:/[A-Z]{1,3})?|\d{2}-\d{3}/[A-Z]{1,3})",
            texts[0],
        )
        sol = sol_m.group(1).replace(" ", "") if sol_m else None
        title = texts[1] if len(texts) > 1 else texts[0]
        status = texts[-1] if texts else ""
        docs: list[str] = []
        tabs: list[str] = []
        for a in tr.find_all("a", href=True):
            abs_u = urljoin(list_url, a["href"])
            if not _is_doc(abs_u):
                continue
            if "/nfb/TAB/" in abs_u:
                tabs.append(abs_u)
            else:
                docs.append(abs_u)
        if not sol or not docs:
            continue
        # Prefer open/due bids; still list pending with docs for triage
        rows_out.append(
            {
                "solicitation_number": sol,
                "title": title,
                "deadline_at": status if re.search(r"(?i)due|deadline", status) else None,
                "status": status,
                "detail_url": docs[0],
                "document_urls": list(dict.fromkeys([*docs, *tabs])),
            }
        )
    return rows_out


def search_jefferson_tx(source: dict, *, limit: int | None = None, fetch_detail_docs: bool = True) -> dict[str, Any]:
    cfg = source.get("config") or {}
    list_url = str(cfg.get("list_url") or JEFFERSON_LIST_URL)
    try:
        status, final, html = fetch_text(list_url, timeout=45)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "items": [], "source_key": source.get("source_key"), "errors": [str(exc)], "stats": {}}
    if status >= 400 or not html:
        return {"ok": False, "items": [], "source_key": source.get("source_key"), "errors": [f"status={status}"], "stats": {}}
    rows = parse_jefferson_tx_listing_html(html, list_url=final or list_url)
    items: list[dict] = []
    skipped = 0
    for row in rows:
        if limit is not None and len(items) >= int(limit):
            break
        title = row["title"]
        st = str(row.get("status") or "")
        if re.search(r"(?i)\b(awarded|cancelled|closed|tabulation\s+only)\b", st) and "due" not in st.lower():
            # keep awarded tabs out of open acquisition unless still due
            if not re.search(r"(?i)bids?\s+due", st):
                skipped += 1
                continue
        if _serviceish(title) or re.search(
            r"(?i)\b(reconstruction|flood\s+mitigation|mitigation\s+plan|management\s+services|"
            r"engineering\s+services|consulting\s+services)\b",
            title,
        ):
            skipped += 1
            continue
        # Keep outboard motors in list — accuracy test; triage must catch install/warranty services
        items.append(
            _item_base(
                source_key=source["source_key"],
                title=title,
                sol=row["solicitation_number"],
                buyer="Jefferson County TX Purchasing",
                state="TX",
                official_url=row["detail_url"],
                discovery_url=final or list_url,
                doc_urls=row["document_urls"],
                deadline=row.get("deadline_at"),
                tz="America/Chicago",
                detail_url=row["detail_url"],
            )
        )
    return {
        "ok": bool(items) or bool(rows),
        "items": items,
        "source_key": source.get("source_key"),
        "errors": [],
        "verified_at": _utc(),
        "pages_fetched": 1,
        "rows_examined": len(rows),
        "pagination_complete": True,
        "more_available": False,
        "stats": {"accepted": len(items), "skipped": skipped, "listing_rows": len(rows)},
    }


def discover_jefferson_tx_documents(detail_url: str, html: str = "", *, extra_urls: list[str] | None = None) -> list[dict[str, Any]]:
    return _docs_from_urls([detail_url, *(extra_urls or [])], platform="jefferson_tx")


# --- Alaska Online Public Notices ---------------------------------------------------

ALASKA_LIST_URL = "https://aws.state.ak.us/OnlinePublicNotices/Notices/Search.aspx?status=1"
_AK_PROCURE_RE = re.compile(r"(?i)\b(ITB|RFP|RFQ|IFB|RFA|Invitation\s+to\s+Bid|Request\s+for\s+Proposal)\b")


def parse_alaska_opn_listing_html(html: str, *, list_url: str = ALASKA_LIST_URL) -> list[dict[str, Any]]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "html.parser")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "View.aspx" not in href:
            continue
        title = _strip(a.get_text(" ", strip=True))
        if not title or not _AK_PROCURE_RE.search(title):
            continue
        abs_u = urljoin(list_url, href)
        if abs_u in seen:
            continue
        seen.add(abs_u)
        sol_m = re.search(
            r"(?i)\b((?:ITB|RFP|RFQ|IFB|RFA)\s*#?\s*[A-Z0-9][A-Z0-9\-_/]*)",
            title,
        )
        sol = ""
        if sol_m:
            sol = re.sub(r"\s+", "-", sol_m.group(1).replace("#", "").strip())
            sol = re.sub(r"-{2,}", "-", sol)
        rows.append({"solicitation_number": sol, "title": title, "detail_url": abs_u, "document_urls": []})
    return rows


def _alaska_detail_docs(detail_url: str) -> tuple[str, list[str]]:
    try:
        st, fin, html = fetch_text(detail_url, timeout=30)
    except Exception:
        return "", []
    if st >= 400 or not html:
        return "", []
    docs = []
    for href, lab in re.findall(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</a>', html or "", flags=re.I):
        abs_u = urljoin(fin or detail_url, href)
        if "Attachment.aspx" in abs_u or _is_doc(abs_u):
            docs.append(abs_u)
    title = ""
    hm = re.search(r"(?is)<h1[^>]*>([\s\S]*?)</h1>", html or "")
    if hm:
        title = _strip(hm.group(1))
    return title, list(dict.fromkeys(docs))


def search_alaska_opn(source: dict, *, limit: int | None = None, fetch_detail_docs: bool = True) -> dict[str, Any]:
    cfg = source.get("config") or {}
    list_url = str(cfg.get("list_url") or ALASKA_LIST_URL)
    try:
        status, final, html = fetch_text(list_url, timeout=40)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "items": [], "source_key": source.get("source_key"), "errors": [str(exc)], "stats": {}}
    if status >= 400 or not html:
        return {"ok": False, "items": [], "source_key": source.get("source_key"), "errors": [f"status={status}"], "stats": {}}
    rows = parse_alaska_opn_listing_html(html, list_url=final or list_url)
    items: list[dict] = []
    skipped = 0
    for row in rows:
        if limit is not None and len(items) >= int(limit):
            break
        title = row["title"]
        # Do NOT auto-skip "construction"/"repairs" titles — Alaska materials-only accuracy case.
        if re.search(
            r"(?i)\b(meeting|board\s+meeting|public\s+hearing|regulation|administrative\s+order|"
            r"management\s+and\s+engineering\s+services|construction\s+management)\b",
            title,
        ):
            skipped += 1
            continue
        docs: list[str] = []
        detail_title = ""
        if fetch_detail_docs:
            detail_title, docs = _alaska_detail_docs(row["detail_url"])
            if not docs:
                skipped += 1
                continue
        use_title = detail_title if detail_title and detail_title.lower() != "notice" else title
        items.append(
            _item_base(
                source_key=source["source_key"],
                title=use_title,
                sol=row["solicitation_number"] or "",
                buyer="State of Alaska",
                state="AK",
                official_url=(docs[0] if docs else row["detail_url"]),
                discovery_url=final or list_url,
                doc_urls=docs,
                detail_url=row["detail_url"],
                tz="America/Anchorage",
            )
        )
    return {
        "ok": bool(items) or bool(rows),
        "items": items,
        "source_key": source.get("source_key"),
        "errors": [],
        "verified_at": _utc(),
        "pages_fetched": 1,
        "rows_examined": len(rows),
        "pagination_complete": True,
        "more_available": False,
        "stats": {"accepted": len(items), "skipped": skipped, "listing_rows": len(rows)},
    }


def discover_alaska_opn_documents(detail_url: str, html: str = "", *, extra_urls: list[str] | None = None) -> list[dict[str, Any]]:
    urls = list(extra_urls or [])
    if detail_url and "View.aspx" in detail_url:
        _t, docs = _alaska_detail_docs(detail_url)
        urls.extend(docs)
    elif detail_url:
        urls.append(detail_url)
    return _docs_from_urls(urls, platform="alaska_opn")


# --- Tennessee Technological University ---------------------------------------------

TNTECH_LIST_URL = "https://www.tntech.edu/purchasing/bidopportunities.php"
TNTECH_STD_TERMS = (
    "https://www.tntech.edu/purchasing/pdf/RFQ_Std_Terms_and_Conditions_rev_6-26-24.pdf"
)


def parse_tntech_listing_html(html: str, *, list_url: str = TNTECH_LIST_URL) -> list[dict[str, Any]]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "html.parser")
    table = soup.find("table")
    if not table:
        return []
    rows_out: list[dict[str, Any]] = []
    for tr in table.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if len(cells) < 2:
            continue
        texts = [_strip(c.get_text(" ", strip=True)) for c in cells]
        if texts[0].lower() in {"due date", "date"} or "description" in texts[1].lower():
            continue
        listing_deadline = texts[0]
        title = texts[1]
        docs: list[str] = []
        for a in tr.find_all("a", href=True):
            abs_u = urljoin(list_url, a["href"])
            if _is_doc(abs_u):
                docs.append(abs_u)
        if not title or not docs:
            continue
        sol_m = re.search(r"(?i)\b((?:RFQ|RFP|ITB|IFB)[-_\s]?[A-Z0-9][A-Z0-9\-_/]*)", title)
        if not sol_m:
            # Filename often carries the RFQ label
            name = unquote(urlparse(docs[0]).path.split("/")[-1])
            sol_m = re.search(r"(?i)\b((?:RFQ|RFP|ITB|IFB)[-_\s]?[A-Z0-9][A-Z0-9\-_/]*)", name)
        sol = sol_m.group(1).replace(" ", "_") if sol_m else re.sub(r"[^\w\-]+", "_", title)[:48]
        rows_out.append(
            {
                "solicitation_number": sol,
                "title": title,
                "deadline_at": listing_deadline,
                "listing_deadline": listing_deadline,
                "detail_url": docs[0],
                "document_urls": list(dict.fromkeys([*docs, TNTECH_STD_TERMS])),
            }
        )
    return rows_out


def search_tntech(
    source: dict, *, limit: int | None = None, fetch_detail_docs: bool = True
) -> dict[str, Any]:
    cfg = source.get("config") or {}
    list_url = str(cfg.get("list_url") or TNTECH_LIST_URL)
    try:
        status, final, html = fetch_text(list_url, timeout=35)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "items": [], "source_key": source.get("source_key"), "errors": [str(exc)], "stats": {}}
    if status >= 400 or not html:
        return {"ok": False, "items": [], "source_key": source.get("source_key"), "errors": [f"status={status}"], "stats": {}}
    rows = parse_tntech_listing_html(html, list_url=final or list_url)
    items: list[dict] = []
    skipped = 0
    for row in rows:
        if limit is not None and len(items) >= int(limit):
            break
        title = row["title"]
        if _serviceish(title):
            skipped += 1
            continue
        item = _item_base(
            source_key=source["source_key"],
            title=title,
            sol=row["solicitation_number"],
            buyer="Tennessee Technological University Purchasing",
            state="TN",
            official_url=row["detail_url"],
            discovery_url=final or list_url,
            doc_urls=row["document_urls"],
            deadline=row.get("deadline_at"),
            tz="America/Chicago",
            detail_url=row["detail_url"],
        )
        # Owner accuracy: listing due date may disagree with RFQ body — flag for confirmation.
        item["listing_deadline"] = row.get("listing_deadline")
        item["deadline_source"] = "listing_unconfirmed"
        item["deadline_requires_document_confirmation"] = True
        item["evidence"] = {
            **(item.get("evidence") or {}),
            "listing_deadline": row.get("listing_deadline"),
            "deadline_conflict_policy": "prefer_document_deadline_seek_confirmation",
        }
        items.append(item)
    return {
        "ok": bool(items) or bool(rows),
        "items": items,
        "source_key": source.get("source_key"),
        "errors": [],
        "verified_at": _utc(),
        "pages_fetched": 1,
        "rows_examined": len(rows),
        "pagination_complete": True,
        "more_available": False,
        "stats": {"accepted": len(items), "skipped": skipped, "listing_rows": len(rows)},
    }


def discover_tntech_documents(
    detail_url: str, html: str = "", *, extra_urls: list[str] | None = None
) -> list[dict[str, Any]]:
    urls = [detail_url, *(extra_urls or []), TNTECH_STD_TERMS]
    return _docs_from_urls(urls, platform="tntech")


# --- Kent State University ----------------------------------------------------------

KENT_LIST_URL = "https://www.kent.edu/procurement/bids"


def parse_kent_listing_html(html: str, *, list_url: str = KENT_LIST_URL) -> list[dict[str, Any]]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "html.parser")
    table = soup.find("table")
    if not table:
        return []
    rows_out: list[dict[str, Any]] = []
    for tr in table.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if len(cells) < 4:
            continue
        texts = [_strip(c.get_text(" ", strip=True)) for c in cells]
        if texts[0].lower().startswith("document") or "due date" in texts[3].lower():
            continue
        doc_cell = texts[0]
        title = texts[1]
        posted = texts[2] if len(texts) > 2 else None
        deadline = texts[3] if len(texts) > 3 else None
        sol_m = re.search(r"(?i)\b((?:RFP|RFQ|ITB|IFB)\s*#?\s*\d{3,5})", doc_cell) or re.search(
            r"(?i)\b((?:RFP|RFQ|ITB|IFB)\s*#?\s*\d{3,5})", title
        )
        sol = ""
        if sol_m:
            sol = re.sub(r"\s+", "", sol_m.group(1).replace("#", ""))
        pdfs: list[str] = []
        qa_urls: list[str] = []
        for a in tr.find_all("a", href=True):
            abs_u = urljoin(list_url, a["href"])
            low = abs_u.lower()
            if "dynamicforms" in low or low.startswith("mailto:"):
                continue
            if _is_doc(abs_u) or "s3fs" in low or "s3-live.kent.edu" in low:
                pdfs.append(abs_u)
            elif "kent.edu" in low and ("/node/" in low or "/procurement/" in low or "question" in low):
                qa_urls.append(abs_u)
        if not title or not pdfs:
            continue
        rows_out.append(
            {
                "solicitation_number": sol or re.sub(r"[^\w\-]+", "_", title)[:40],
                "title": title,
                "deadline_at": deadline,
                "posted_at": posted,
                "detail_url": pdfs[0],
                "document_urls": list(dict.fromkeys([*pdfs, *qa_urls])),
                "qa_urls": qa_urls,
            }
        )
    return rows_out


def search_kent_state(
    source: dict, *, limit: int | None = None, fetch_detail_docs: bool = True
) -> dict[str, Any]:
    cfg = source.get("config") or {}
    list_url = str(cfg.get("list_url") or KENT_LIST_URL)
    try:
        status, final, html = fetch_text(list_url, timeout=40)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "items": [], "source_key": source.get("source_key"), "errors": [str(exc)], "stats": {}}
    if status >= 400 or not html:
        return {"ok": False, "items": [], "source_key": source.get("source_key"), "errors": [f"status={status}"], "stats": {}}
    rows = parse_kent_listing_html(html, list_url=final or list_url)
    items: list[dict] = []
    skipped = 0
    for row in rows:
        if limit is not None and len(items) >= int(limit):
            break
        title = row["title"]
        if _serviceish(title) and not re.search(r"(?i)\b(kit|equipment|technology|supplies|materials)\b", title):
            skipped += 1
            continue
        item = _item_base(
            source_key=source["source_key"],
            title=title,
            sol=row["solicitation_number"],
            buyer="Kent State University Procurement",
            state="OH",
            official_url=row["detail_url"],
            discovery_url=final or list_url,
            doc_urls=row["document_urls"],
            deadline=row.get("deadline_at"),
            tz="America/New_York",
            detail_url=row["detail_url"],
        )
        item["posted_at"] = row.get("posted_at")
        item["evidence"] = {
            **(item.get("evidence") or {}),
            "qa_urls": row.get("qa_urls") or [],
            "preserve_version_ids": True,
            "submission_portal": "dynamicforms_separate_from_acquisition",
        }
        items.append(item)
    return {
        "ok": bool(items) or bool(rows),
        "items": items,
        "source_key": source.get("source_key"),
        "errors": [],
        "verified_at": _utc(),
        "pages_fetched": 1,
        "rows_examined": len(rows),
        "pagination_complete": True,
        "more_available": False,
        "stats": {"accepted": len(items), "skipped": skipped, "listing_rows": len(rows)},
    }


def discover_kent_state_documents(
    detail_url: str, html: str = "", *, extra_urls: list[str] | None = None
) -> list[dict[str, Any]]:
    urls = [detail_url, *(extra_urls or [])]
    docs = _docs_from_urls(urls, platform="kent_state")
    # Preserve VersionId query on Kent S3 objects
    for d in docs:
        src = str(d.get("source_url") or "")
        if "VersionId=" in src:
            d["meta"] = {**(d.get("meta") or {}), "version_id": src.split("VersionId=", 1)[-1][:80]}
            if re.search(r"(?i)rev\.?pdf|revised", src):
                d["document_type"] = "revised_solicitation"
                d["is_amendment"] = True
    # Q&A HTML pages (not file extensions)
    seen = {d["source_url"] for d in docs}
    for u in urls:
        if u in seen:
            continue
        low = u.lower()
        if "dynamicforms" in low or not ("kent.edu" in low):
            continue
        if "/node/" in low or "question" in low or "/procurement/" in low:
            docs.append(
                {
                    "source_url": u,
                    "original_filename": "questions_and_answers.html",
                    "document_title": "Questions and Answers",
                    "document_type": "qa",
                    "is_amendment": False,
                    "meta": {"platform": "kent_state"},
                }
            )
            seen.add(u)
    return docs


# --- City of Tulsa ------------------------------------------------------------------

TULSA_LIST_URL = (
    "https://www.cityoftulsa.org/government/departments/finance/selling-to-the-city/"
    "bid-opportunities-and-results/?t=current"
)


def parse_tulsa_listing_html(html: str, *, list_url: str = TULSA_LIST_URL) -> list[dict[str, Any]]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "html.parser")
    table = soup.find("table")
    if not table:
        return []
    rows_out: list[dict[str, Any]] = []
    for tr in table.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if len(cells) < 3:
            continue
        texts = [_strip(c.get_text(" ", strip=True)) for c in cells]
        if "bid #" in texts[0].lower() or texts[1].lower().startswith("response"):
            continue
        bid_cell = texts[0]
        deadline = texts[1]
        title = texts[2]
        sol_m = re.search(r"(?i)\b((?:IFB|RFP|RFQ|CSP|TAC)\s*[\w\-]+)", bid_cell)
        sol = sol_m.group(1).replace(" ", " ") if sol_m else bid_cell.split("(")[0].strip()
        addendum_n = None
        am = re.search(r"\((\d+)\)", bid_cell)
        if am:
            addendum_n = int(am.group(1))
        detail = None
        for a in tr.find_all("a", href=True):
            abs_u = urljoin(list_url, a["href"])
            if "?t=" in abs_u or abs_u.rstrip("/").endswith("bid-opportunities-and-results"):
                continue
            if "/bid-opportunities-and-results/" in abs_u:
                detail = abs_u
                break
        if not sol or not detail:
            continue
        rows_out.append(
            {
                "solicitation_number": sol.strip(),
                "title": title,
                "deadline_at": deadline,
                "addendum_count": addendum_n,
                "detail_url": detail,
                "document_urls": [],
            }
        )
    return rows_out


def _tulsa_detail_docs(detail_url: str) -> tuple[list[str], dict[str, Any]]:
    meta: dict[str, Any] = {}
    try:
        st, fin, html = fetch_text(detail_url, timeout=35)
    except Exception:
        return [], meta
    if st >= 400 or not html:
        return [], meta
    docs: list[str] = []
    for href, lab in re.findall(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</a>', html or "", flags=re.I):
        abs_u = urljoin(fin or detail_url, href)
        if _is_doc(abs_u) and "cityoftulsa.org" in abs_u:
            docs.append(abs_u)
    text = _strip(re.sub(r"<[^>]+>", " ", html or ""))
    if re.search(r"(?i)mandatory\s+(pre[- ]?bid|pre[- ]?proposal|site\s+visit|conference)", text):
        meta["mandatory_prebid"] = True
        meta["eligibility_restriction"] = "mandatory_prebid_or_site_visit"
    return list(dict.fromkeys(docs)), meta


def search_tulsa_ok(
    source: dict, *, limit: int | None = None, fetch_detail_docs: bool = True
) -> dict[str, Any]:
    cfg = source.get("config") or {}
    list_url = str(cfg.get("list_url") or TULSA_LIST_URL)
    try:
        status, final, html = fetch_text(list_url, timeout=40)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "items": [], "source_key": source.get("source_key"), "errors": [str(exc)], "stats": {}}
    if status >= 400 or not html:
        return {"ok": False, "items": [], "source_key": source.get("source_key"), "errors": [f"status={status}"], "stats": {}}
    rows = parse_tulsa_listing_html(html, list_url=final or list_url)
    items: list[dict] = []
    skipped = 0
    for row in rows:
        if limit is not None and len(items) >= int(limit):
            break
        title = row["title"]
        if _serviceish(title) or re.search(
            r"(?i)\b(rental\s+services|consulting|management\s+services)\b", title
        ):
            skipped += 1
            continue
        docs: list[str] = []
        detail_meta: dict[str, Any] = {}
        if fetch_detail_docs:
            docs, detail_meta = _tulsa_detail_docs(row["detail_url"])
            if not docs:
                skipped += 1
                continue
        item = _item_base(
            source_key=source["source_key"],
            title=title,
            sol=row["solicitation_number"],
            buyer="City of Tulsa Finance / Purchasing",
            state="OK",
            official_url=(docs[0] if docs else row["detail_url"]),
            discovery_url=final or list_url,
            doc_urls=docs,
            deadline=row.get("deadline_at"),
            tz="America/Chicago",
            detail_url=row["detail_url"],
        )
        item["evidence"] = {
            **(item.get("evidence") or {}),
            "listing_addendum_count": row.get("addendum_count"),
            "scan_docs_for_mandatory_prebid": True,
            **detail_meta,
        }
        if detail_meta.get("mandatory_prebid"):
            item["eligibility_restriction"] = "mandatory_prebid_or_site_visit"
            item["bid_eligibility_blocked_reason"] = "mandatory_prebid_attendance_required"
        items.append(item)
    return {
        "ok": bool(items) or bool(rows),
        "items": items,
        "source_key": source.get("source_key"),
        "errors": [],
        "verified_at": _utc(),
        "pages_fetched": 1,
        "rows_examined": len(rows),
        "pagination_complete": True,
        "more_available": False,
        "stats": {"accepted": len(items), "skipped": skipped, "listing_rows": len(rows)},
    }


def discover_tulsa_ok_documents(
    detail_url: str, html: str = "", *, extra_urls: list[str] | None = None
) -> list[dict[str, Any]]:
    urls = list(extra_urls or [])
    if detail_url and not _is_doc(detail_url):
        docs, _meta = _tulsa_detail_docs(detail_url)
        urls.extend(docs)
    elif detail_url:
        urls.append(detail_url)
    return _docs_from_urls(urls, platform="tulsa_ok")


# --- Columbus Consolidated Government (GA) ------------------------------------------

COLUMBUS_LIST_URL = "https://columbusga.gov/finance/bid-opportunities"


def parse_columbus_ga_listing_html(
    html: str, *, list_url: str = COLUMBUS_LIST_URL
) -> list[dict[str, Any]]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "html.parser")
    table = soup.find("table")
    if not table:
        return []
    rows_out: list[dict[str, Any]] = []
    for tr in table.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if len(cells) < 3:
            continue
        texts = [_strip(c.get_text(" ", strip=True)) for c in cells]
        if texts[0].lower().startswith("solicitation") or "due date" in texts[-1].lower():
            continue
        sol = texts[0]
        title = texts[1]
        deadline = texts[2] if len(texts) > 2 else None
        docs: list[str] = []
        broken: list[str] = []
        for a in tr.find_all("a", href=True):
            href = a["href"]
            abs_u = urljoin(list_url, href)
            lab = _strip(a.get_text(" ", strip=True))
            # Flag non-document / broken CMS edit links named as addenda
            if re.search(r"(?i)addendum", lab) and (
                "tosic_sexycontent" in abs_u or "ng-edit" in abs_u or abs_u.rstrip("/").endswith("Addendum")
            ):
                broken.append(abs_u)
                continue
            if _is_doc(abs_u) or "/Portals/finance/" in abs_u:
                if abs_u.lower().split("?", 1)[0].endswith((".pdf", ".xlsx", ".xls", ".doc", ".docx", ".zip")):
                    docs.append(abs_u)
        if not sol or not docs:
            continue
        rows_out.append(
            {
                "solicitation_number": sol,
                "title": title,
                "deadline_at": deadline,
                "detail_url": docs[0],
                "document_urls": list(dict.fromkeys(docs)),
                "broken_document_links": broken,
                "mandatory_site_visit": bool(
                    re.search(r"(?i)mandatory\s+(site\s+visit|pre[- ]?bid|conference)", title)
                ),
            }
        )
    return rows_out


def search_columbus_ga(
    source: dict, *, limit: int | None = None, fetch_detail_docs: bool = True
) -> dict[str, Any]:
    cfg = source.get("config") or {}
    list_url = str(cfg.get("list_url") or COLUMBUS_LIST_URL)
    try:
        status, final, html = fetch_text(list_url, timeout=40)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "items": [], "source_key": source.get("source_key"), "errors": [str(exc)], "stats": {}}
    if status >= 400 or not html:
        return {"ok": False, "items": [], "source_key": source.get("source_key"), "errors": [f"status={status}"], "stats": {}}
    rows = parse_columbus_ga_listing_html(html, list_url=final or list_url)
    items: list[dict] = []
    skipped = 0
    for row in rows:
        if limit is not None and len(items) >= int(limit):
            break
        title = row["title"]
        if _serviceish(title) or re.search(
            r"(?i)\b(roofing|consultant|consulting|irrigation\s+installation|"
            r"body\s+repair|paint\s+services|pension|investment\s+consultant|"
            r"design\s+guidelines|resurfacing|installation\s+and\s+maintenance|"
            r"phase\s*\d|wall\s+project|site\s+visit)\b",
            title,
        ):
            skipped += 1
            continue
        item = _item_base(
            source_key=source["source_key"],
            title=title,
            sol=row["solicitation_number"],
            buyer="Columbus Consolidated Government Finance",
            state="GA",
            official_url=row["detail_url"],
            discovery_url=final or list_url,
            doc_urls=row["document_urls"],
            deadline=row.get("deadline_at"),
            tz="America/New_York",
            detail_url=row["detail_url"],
        )
        broken = row.get("broken_document_links") or []
        item["evidence"] = {
            **(item.get("evidence") or {}),
            "broken_document_links": broken,
            "package_incomplete": bool(broken),
            "mandatory_site_visit": row.get("mandatory_site_visit"),
            "deadline_may_be_amended": any(
                re.search(r"(?i)addend|amend", u) for u in row["document_urls"]
            ),
        }
        if broken:
            item["package_status_hint"] = "INCOMPLETE_MISSING_ADDENDUM"
        if row.get("mandatory_site_visit"):
            item["eligibility_restriction"] = "mandatory_prebid_or_site_visit"
        items.append(item)
    return {
        "ok": bool(items) or bool(rows),
        "items": items,
        "source_key": source.get("source_key"),
        "errors": [],
        "verified_at": _utc(),
        "pages_fetched": 1,
        "rows_examined": len(rows),
        "pagination_complete": True,
        "more_available": False,
        "stats": {"accepted": len(items), "skipped": skipped, "listing_rows": len(rows)},
    }


def discover_columbus_ga_documents(
    detail_url: str, html: str = "", *, extra_urls: list[str] | None = None
) -> list[dict[str, Any]]:
    return _docs_from_urls([detail_url, *(extra_urls or [])], platform="columbus_ga")
