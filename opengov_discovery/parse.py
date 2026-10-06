"""Parse OpenGov portal HTML / JSON into opportunity records."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urljoin

from discovery.deadline import normalize_deadline


def _items_from_json(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if not isinstance(data, dict):
        return []
    for key in (
        "rows",  # government/{code}/project/public
        "projects",
        "opportunities",
        "results",
        "items",
        "bids",
        "data",
        "content",
        "records",
    ):
        val = data.get(key)
        if isinstance(val, list):
            return [x for x in val if isinstance(x, dict)]
        if isinstance(val, dict):
            nested = _items_from_json(val)
            if nested:
                return nested
    return []


def _gov_code_from_item(item: dict[str, Any]) -> str | None:
    gov = item.get("government")
    if isinstance(gov, dict):
        code = gov.get("code")
        if code:
            return str(code).strip().lower()
        org = gov.get("organization") if isinstance(gov.get("organization"), dict) else {}
        # fall through — organization rarely has code
        _ = org
    return None


def _detail_url_for_item(item: dict[str, Any], list_url: str) -> str | None:
    detail = (
        item.get("publicUrl")
        or item.get("detail_url")
        or item.get("url")
        or item.get("projectUrl")
        or item.get("href")
    )
    if detail:
        if not str(detail).startswith("http") and list_url:
            return urljoin(list_url, str(detail))
        return str(detail)
    pid = item.get("id")
    code = _gov_code_from_item(item)
    if pid and code:
        return f"https://procurement.opengov.com/portal/{code}/projects/{pid}"
    if pid:
        return f"https://api.procurement.opengov.com/api/v1/project/{pid}"
    return None


def parse_opengov_json_payload(data: Any, *, list_url: str, agency: str | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in _items_from_json(data):
        title = str(
            item.get("title")
            or item.get("projectTitle")
            or item.get("projectName")
            or item.get("name")
            or ""
        ).strip()
        if not title or len(title) < 4:
            continue
        detail = _detail_url_for_item(item, list_url)
        sol = (
            item.get("solicitation_number")
            or item.get("referenceNumber")
            or item.get("projectNumber")
            or item.get("financialId")
            or item.get("number")
            or item.get("id")
        )
        deadline_raw = (
            item.get("proposalDeadline")
            or item.get("closeDate")
            or item.get("dueDate")
            or item.get("response_deadline")
            or item.get("due_date")
        )
        release_raw = item.get("releaseProjectDate") or item.get("releaseDate") or item.get("created_at")
        dl = normalize_deadline(deadline_raw) if deadline_raw else {}
        docs = []
        for d in item.get("documents") or item.get("attachments") or []:
            if isinstance(d, str) and d.startswith("http"):
                docs.append({"document_url": d, "document_name": d.rsplit("/", 1)[-1], "document_type": "attachment"})
            elif isinstance(d, dict) and d.get("url"):
                docs.append(
                    {
                        "document_url": d["url"],
                        "document_name": d.get("name") or d.get("filename") or "document",
                        "document_type": d.get("type") or "attachment",
                    }
                )
        addenda = item.get("addendums") or item.get("addenda") or []
        gov = item.get("government") if isinstance(item.get("government"), dict) else {}
        org = gov.get("organization") if isinstance(gov.get("organization"), dict) else {}
        agency_name = (
            item.get("agency")
            or item.get("organizationName")
            or org.get("name")
            or agency
        )
        dept = item.get("department")
        if isinstance(dept, dict):
            dept_name = dept.get("name")
        else:
            dept_name = dept
        out.append(
            {
                "external_id": str(sol or title)[:160],
                "title": title,
                "solicitation_number": str(sol) if sol else None,
                "agency": agency_name,
                "description": item.get("description") or item.get("summary"),
                "status": str(item.get("status") or "OPEN"),
                "detail_url": detail,
                "source_url": list_url,
                "deadline": dl.get("utc_deadline") or dl.get("parsed_local"),
                "deadline_raw": deadline_raw,
                "release_date": release_raw,
                "timezone": dl.get("timezone") or org.get("timezone"),
                "timezone_confidence": dl.get("timezone_confidence"),
                "location": item.get("location") or item.get("city") or org.get("city"),
                "categories": item.get("categories") or item.get("commodities") or item.get("nigp"),
                "buyer_contact": item.get("contact") or item.get("buyer"),
                "estimated_value": item.get("budget") or item.get("estimatedValue"),
                "document_links": docs,
                "raw_metadata": {
                    "platform": "OpenGov",
                    "discovery_scope": "BROAD_PRODUCT_RESALE",
                    "opengov_item_keys": list(item.keys())[:40],
                    "opengov_project_id": item.get("id"),
                    "opengov_government_code": _gov_code_from_item(item),
                    "department": dept_name,
                    "addenda_count": len(addenda) if isinstance(addenda, list) else 0,
                    "has_addenda": bool(addenda),
                },
            }
        )
    return out


def parse_opengov_portal_html(html: str, *, list_url: str, agency: str | None = None) -> list[dict[str, Any]]:
    """Extract project cards / links from rendered OpenGov portal HTML."""
    body = html or ""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    # Embedded JSON blobs (greedy to first closing script — nested objects)
    for m in re.finditer(
        r'<script[^>]*type=["\']application/json["\'][^>]*>([\s\S]*?)</script>',
        body,
        re.I,
    ):
        raw = (m.group(1) or "").strip()
        if not raw.startswith("{") and not raw.startswith("["):
            continue
        try:
            data = json.loads(raw)
            for rec in parse_opengov_json_payload(data, list_url=list_url, agency=agency):
                key = str(rec.get("detail_url") or rec.get("external_id"))
                if key and key not in seen:
                    seen.add(key)
                    out.append(rec)
        except Exception:
            pass

    # __NEXT_DATA__ style
    m_next = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>([\s\S]*?)</script>',
        body,
        re.I,
    )
    if m_next:
        try:
            data = json.loads((m_next.group(1) or "").strip())
            for rec in parse_opengov_json_payload(data, list_url=list_url, agency=agency):
                key = str(rec.get("detail_url") or rec.get("external_id"))
                if key and key not in seen:
                    seen.add(key)
                    out.append(rec)
        except Exception:
            pass

    # Project links
    for m in re.finditer(
        r'href=["\']([^"\']+/projects?/[^"\']+)["\'][^>]*>([^<]{6,240})<',
        body,
        re.I,
    ):
        href, title = m.group(1), re.sub(r"\s+", " ", m.group(2)).strip()
        if title.lower() in {"view", "details", "more", "apply"}:
            continue
        detail = urljoin(list_url, href)
        if detail in seen:
            continue
        seen.add(detail)
        out.append(
            {
                "external_id": detail.rsplit("/", 1)[-1][:160],
                "title": title,
                "agency": agency,
                "status": "OPEN",
                "detail_url": detail,
                "source_url": list_url,
                "document_links": [],
                "raw_metadata": {"platform": "OpenGov", "parse_route": "project_href"},
            }
        )

    # proposalDeadline / projectTitle text patterns
    if not out:
        for m in re.finditer(
            r'projectTitle["\']?\s*[:=]\s*["\']([^"\']{8,200})["\']',
            body,
            re.I,
        ):
            title = m.group(1).strip()
            key = title.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "external_id": title[:160],
                    "title": title,
                    "agency": agency,
                    "status": "OPEN",
                    "detail_url": list_url,
                    "source_url": list_url,
                    "document_links": [],
                    "raw_metadata": {"platform": "OpenGov", "parse_route": "projectTitle_field"},
                }
            )

    # Fallback to existing OpenGovLiveFetcher HTML/JSON parser
    if not out:
        try:
            from discovery.live_fetchers import OpenGovLiveFetcher

            opps = OpenGovLiveFetcher().parse_listing(body, list_url=list_url)
            for o in opps:
                d = o.to_dict() if hasattr(o, "to_dict") else {}
                if not d and hasattr(o, "__dict__"):
                    d = {
                        "external_id": getattr(o, "external_id", None),
                        "title": getattr(o, "title", None),
                        "detail_url": getattr(o, "detail_url", None),
                        "solicitation_number": getattr(o, "solicitation_number", None),
                        "agency": getattr(o, "agency", None) or agency,
                        "deadline_raw": getattr(o, "deadline_raw", None),
                        "status": getattr(o, "status", None) or "OPEN",
                        "source_url": list_url,
                        "document_links": getattr(o, "document_links", None) or [],
                    }
                d["agency"] = d.get("agency") or agency
                d.setdefault("raw_metadata", {})["parse_route"] = "OpenGovLiveFetcher"
                key = str(d.get("detail_url") or d.get("external_id") or d.get("title"))
                if key and key not in seen:
                    seen.add(key)
                    out.append(d)
        except Exception:
            pass

    return out


def parse_detail_html(html: str, *, detail_url: str | None = None) -> dict[str, Any]:
    """Parse authenticated OpenGov project detail for recovery."""
    body = html or ""
    title = None
    hm = re.search(r"<h1[^>]*>([^<]{5,300})</h1>", body, re.I)
    if hm:
        title = re.sub(r"\s+", " ", hm.group(1)).strip()
    tm = re.search(r"<title>([^<]+)</title>", body, re.I)
    if tm and not title:
        title = re.sub(r"\s*[-|].*$", "", tm.group(1)).strip()

    def _label(lab: str) -> str | None:
        pat = re.escape(lab) + r"\s*[:</][^<]*</[^>]+>\s*([^<]{2,400})"
        m = re.search(pat, body, re.I)
        if m:
            return re.sub(r"\s+", " ", m.group(1)).strip()
        return None

    closing = _label("Proposal Deadline") or _label("Close Date") or _label("Due Date")
    pub = _label("Published") or _label("Release Date") or _label("Issue Date")
    sol = _label("Solicitation Number") or _label("Project Number") or _label("Reference")
    org = _label("Organization") or _label("Agency") or _label("Buyer")
    loc = _label("Location")
    desc = None
    dm = re.search(r"(?:Description|Overview)[^<]{0,40}</[^>]+>\s*<[^>]+>([\s\S]{40,4000}?)</(?:div|p|section)", body, re.I)
    if dm:
        desc = re.sub(r"<[^>]+>", " ", dm.group(1))
        desc = re.sub(r"\s+", " ", desc).strip()

    docs: list[dict[str, Any]] = []
    for m in re.finditer(
        r'href="([^"]+\.(?:pdf|docx?|xlsx?|csv|zip)(?:\?[^"]*)?)"[^>]*>([^<]{0,160})<',
        body,
        re.I,
    ):
        href, name = m.group(1), re.sub(r"\s+", " ", m.group(2) or "").strip()
        url = urljoin(detail_url or "", href)
        docs.append(
            {
                "document_type": "attachment",
                "document_name": name or href.rsplit("/", 1)[-1][:160],
                "document_url": url,
                "retrieval_status": "URL_DISCOVERED",
            }
        )
    for m in re.finditer(
        r'href="([^"]+)"[^>]*>([^<]{0,120}(?:Addendum|Amendment|Attachment|Specification|Bid\s*Sheet|Pricing)[^<]{0,40})</a>',
        body,
        re.I,
    ):
        href, name = m.group(1), re.sub(r"\s+", " ", m.group(2)).strip()
        if "login" in href.lower():
            continue
        url = urljoin(detail_url or "", href)
        if not any(d.get("document_url") == url for d in docs):
            docs.append(
                {
                    "document_type": "linked",
                    "document_name": name[:160],
                    "document_url": url,
                    "retrieval_status": "URL_DISCOVERED",
                }
            )

    auth_wall = bool(
        re.search(r"sign\s*in\s*to\s*(view|download)|login\s*required|members?\s*only", body, re.I)
    )
    dl = normalize_deadline(closing) if closing else {}
    return {
        "title": title,
        "description": desc,
        "agency": org,
        "solicitation_number": sol,
        "location": loc,
        "issue_date": pub,
        "close_date_raw": closing,
        "deadline": dl.get("utc_deadline") or dl.get("parsed_local"),
        "timezone": dl.get("timezone"),
        "timezone_confidence": dl.get("timezone_confidence"),
        "documents": docs,
        "auth_wall": auth_wall,
        "material_improvement": bool(closing or desc or org or sol or docs),
        "parse_ok": bool(title or closing or docs or desc),
    }


def detect_category_restriction(html: str) -> bool:
    """True if UI suggests results are limited to vendor category preferences."""
    return bool(
        re.search(
            r"(showing\s+bids?\s+matching\s+your\s+(categories|preferences|naics|nigp)|"
            r"based\s+on\s+your\s+(commodity|category)\s+preferences|"
            r"update\s+your\s+(categories|commodity\s+codes)\s+to\s+see\s+more)",
            html or "",
            re.I,
        )
    )
