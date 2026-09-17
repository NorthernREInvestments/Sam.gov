"""Live fetcher contract + platform-family production fetchers."""

from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from datetime import datetime
from html.parser import HTMLParser
from typing import Any

from discovery.classify import classify_discovery_opportunity
from discovery.constants import (
    ADAPTER_LIVE_CAPABLE,
    NOTICE_AWARDED_CONTRACT_CATALOG,
    TIER_1,
    TIER_2,
)
from discovery.deadline import normalize_deadline
from discovery.http_client import PublicProcurementHttpClient
from discovery.notice_types import classify_notice_type
from discovery.schema import CanonicalOpportunity
from discovery.validation import validate_listing_response


class LiveFetcher(ABC):
    """Production live fetcher — LIVE_CAPABLE only if subclass implements fetch_listing."""

    source_id: str = ""
    source_name: str = ""
    platform_family: str = ""
    adapter_status: str = ADAPTER_LIVE_CAPABLE
    trust_tier: int = TIER_1
    expected_kind: str = "html"

    @abstractmethod
    def parse_listing(self, body: str, *, list_url: str, meta: dict[str, Any] | None = None) -> list[CanonicalOpportunity]:
        ...

    def structure_recognized(self, body: str, *, list_url: str | None = None) -> bool:
        """Override in portal-specific fetchers."""
        return False

    def fetch_listing(
        self,
        client: PublicProcurementHttpClient,
        *,
        list_url: str,
        source_id: str | None = None,
        max_pages: int = 1,
        pagination_exhaust: bool = False,
        pagination_safety_max_pages: int | None = None,
    ) -> dict[str, Any]:
        """
        Fetch listing pages.

        When pagination_exhaust=True, continue until natural stop (empty/repeated/
        no-next) or defensive safety_max_pages → PAGINATION_INCOMPLETE (never silent SUCCESS).
        """
        sid = source_id or self.source_id
        from family_adapter_contract import (
            PAGINATION_NONE,
            PAGINATION_UNKNOWN,
            detect_pagination_model,
            next_page_url,
        )

        safety_cap = int(
            pagination_safety_max_pages
            if pagination_safety_max_pages is not None
            else max(1, int(max_pages))
        )
        # Soft request budget vs hard safety: exhaust uses safety_cap as emergency only
        soft_cap = max(1, int(max_pages))
        hard_cap = max(soft_cap, safety_cap) if pagination_exhaust else soft_cap

        all_opps: list[CanonicalOpportunity] = []
        malformed_total = 0
        pages_fetched = 0
        live_reqs = 0
        last_validation: dict[str, Any] = {}
        last_meta: dict[str, Any] = {}
        page_urls = [list_url]
        seen_page_urls: set[str] = {list_url}
        page_fingerprints: set[str] = set()
        pag_model = "UNKNOWN"
        source_reported_total: int | None = None
        pagination_complete = False
        pagination_stop_reason = "NOT_STARTED"
        beyond_page_1 = False

        for page_idx in range(hard_cap):
            page_url = page_urls[page_idx] if page_idx < len(page_urls) else None
            if page_url is None:
                pagination_stop_reason = "MISSING_NEXT_TOKEN"
                pagination_complete = pages_fetched > 0
                break

            resp = client.get(page_url, source_id=sid)
            live_reqs += 0 if not client.authorize_live else 1
            pages_fetched += 1
            last_meta = resp.meta.to_dict()
            body = resp.text or ""

            # Access blockers
            if resp.status_code in {401, 403} or _is_cloudflare_challenge(body):
                pagination_stop_reason = (
                    "BOT_PROTECTED" if _is_cloudflare_challenge(body) else "AUTH_REQUIRED"
                )
                pagination_complete = False
                last_validation = validate_listing_response(
                    status_code=resp.status_code,
                    content_type=resp.meta.content_type,
                    body=body,
                    records_found=0,
                    expected_kind=self.expected_kind,
                    structure_recognized=False,
                )
                last_validation["failure_type"] = pagination_stop_reason
                last_validation["valid"] = False
                break

            fp = hashlib.sha256(body.encode("utf-8", errors="ignore")).hexdigest()[:24]
            if fp in page_fingerprints:
                pagination_stop_reason = "REPEATED_PAGE"
                pagination_complete = True
                break
            page_fingerprints.add(fp)

            if page_idx == 0:
                pag = detect_pagination_model(body, page_url)
                pag_model = pag.get("model") or "UNKNOWN"
                # JSON total if present
                try:
                    if body.strip().startswith("{") or body.strip().startswith("["):
                        data = json.loads(body)
                        if isinstance(data, dict):
                            for k in ("total", "totalCount", "totalRecords", "recordCount", "count"):
                                if isinstance(data.get(k), int):
                                    source_reported_total = data[k]
                                    break
                except Exception:
                    pass
                if hard_cap > 1 and pag_model not in {PAGINATION_NONE, PAGINATION_UNKNOWN, "NO_PAGINATION"}:
                    for p in range(2, hard_cap + 1):
                        model = "PAGE_NUMBER" if pag_model in {"BOUNDED_WINDOW", "PAGE_NUMBER"} else pag_model
                        nxt = next_page_url(list_url, page=p, model=model)
                        if nxt and nxt not in seen_page_urls:
                            page_urls.append(nxt)
                            seen_page_urls.add(nxt)

            # Continuation / next link from HTML
            if pagination_exhaust and pages_fetched < hard_cap:
                next_href = None
                m = re.search(
                    r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>\s*(?:Next|»|›|Load\s*more)\s*<',
                    body,
                    re.I,
                )
                if m:
                    next_href = m.group(1)
                if next_href:
                    from urllib.parse import urljoin

                    abs_next = urljoin(page_url, next_href)
                    if abs_next not in seen_page_urls:
                        page_urls.append(abs_next)
                        seen_page_urls.add(abs_next)

            parsed = self.parse_listing(body, list_url=page_url, meta=resp.meta.to_dict())
            from discovery.opportunity_gate import is_structurally_valid_opportunity

            page_opps: list[CanonicalOpportunity] = []
            malformed = 0
            for o in parsed:
                gate = is_structurally_valid_opportunity(
                    {
                        "title": o.title,
                        "solicitation_number": o.solicitation_number,
                        "external_id": o.external_id,
                        "deadline_raw": o.deadline_raw,
                        "detail_url": o.detail_url,
                        "agency": o.agency,
                        "status": o.status,
                    }
                )
                if not gate["valid"]:
                    malformed += 1
                    continue
                if gate.get("sanitized_solicitation_number") is not None:
                    o.solicitation_number = gate["sanitized_solicitation_number"]
                elif o.solicitation_number and gate.get("sanitized_solicitation_number") is None:
                    o.solicitation_number = None
                if gate.get("sanitized_deadline_raw") is not None:
                    o.deadline_raw = gate["sanitized_deadline_raw"]
                elif o.deadline_raw and gate.get("sanitized_deadline_raw") is None:
                    o.deadline_raw = None
                o.raw_metadata = {**(o.raw_metadata or {}), "structural_gate": gate, "list_page": page_idx + 1}
                page_opps.append(o)

            malformed_total += malformed
            seen = {o.external_id for o in all_opps}
            new = [o for o in page_opps if o.external_id not in seen]
            all_opps.extend(new)
            if page_idx > 0 and len(new) > 0:
                beyond_page_1 = True

            last_validation = validate_listing_response(
                status_code=resp.status_code,
                content_type=resp.meta.content_type,
                body=body,
                records_found=len(page_opps),
                expected_kind=self.expected_kind,
                structure_recognized=self.structure_recognized(body, list_url=page_url),
            )

            if len(page_opps) == 0 and page_idx == 0:
                pagination_stop_reason = "EMPTY_PAGE"
                pagination_complete = True
                break
            if page_idx > 0 and len(new) == 0:
                pagination_stop_reason = "EMPTY_PAGE" if len(page_opps) == 0 else "REPEATED_RECORD_FINGERPRINT"
                pagination_complete = True
                break
            if source_reported_total is not None and len(all_opps) >= source_reported_total:
                pagination_stop_reason = "SOURCE_REPORTED_TOTAL_REACHED"
                pagination_complete = True
                break

            # No further URLs prepared and model says none
            if page_idx + 1 >= len(page_urls):
                if pag_model in {PAGINATION_NONE, "NO_PAGINATION"} or not pagination_exhaust:
                    pagination_stop_reason = "NO_PAGINATION" if pag_model in {PAGINATION_NONE, "NO_PAGINATION"} else "PAGE_BUDGET_SOFT"
                    pagination_complete = pag_model in {PAGINATION_NONE, "NO_PAGINATION"} or not pagination_exhaust
                    if pagination_exhaust and pag_model not in {PAGINATION_NONE, "NO_PAGINATION", PAGINATION_UNKNOWN}:
                        # Exhaust requested but could not discover next page
                        pagination_stop_reason = "MISSING_NEXT_TOKEN"
                        pagination_complete = True  # exhausted what is discoverable
                    break
                pagination_stop_reason = "MISSING_NEXT_TOKEN"
                pagination_complete = True
                break
        else:
            # Loop exhausted hard_cap without natural stop
            if pagination_exhaust and pages_fetched >= safety_cap:
                pagination_stop_reason = "PAGINATION_INCOMPLETE"
                pagination_complete = False
            else:
                pagination_stop_reason = "PAGE_BUDGET_SOFT"
                pagination_complete = not pagination_exhaust

        return {
            "opportunities": all_opps,
            "malformed_rejected": malformed_total,
            "request_meta": last_meta,
            "validation": last_validation,
            "pages_fetched": pages_fetched,
            "records_fetched": len(all_opps),
            "pagination_model": pag_model,
            "pagination_complete": pagination_complete,
            "pagination_stop_reason": pagination_stop_reason,
            "source_reported_total": source_reported_total,
            "beyond_page_1": beyond_page_1 or (pages_fetched > 1 and len(all_opps) > 0),
            "LIVE_API_REQUESTS": live_reqs,
        }

    def fetch_detail(
        self,
        client: PublicProcurementHttpClient,
        *,
        detail_url: str,
        source_id: str | None = None,
    ) -> dict[str, Any]:
        sid = source_id or self.source_id
        resp = client.get(detail_url, source_id=sid)
        docs = self.discover_documents(resp.text, detail_url=detail_url)
        return {
            "detail_url": detail_url,
            "request_meta": resp.meta.to_dict(),
            "documents": docs,
            "body_preview": (resp.text or "")[:500],
        }

    def discover_documents(self, body: str, *, detail_url: str | None = None) -> list[dict[str, Any]]:
        docs = []
        for m in re.finditer(r'href=["\']([^"\']+\.(?:pdf|docx?|xlsx?|zip))["\']', body or "", re.I):
            url = m.group(1)
            if url.startswith("/"):
                # relative — leave as-is; caller may resolve
                pass
            docs.append({"url": url, "kind": "attachment"})
        return docs

    def normalize(self, raw: dict[str, Any]) -> CanonicalOpportunity:
        raise NotImplementedError


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_td = False
        self.current_row: list[str] = []
        self.rows: list[list[str]] = []
        self.cell = ""
        self.href: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self.current_row = []
        elif tag in {"td", "th"}:
            self.in_td = True
            self.cell = ""
            self.href = None
        elif tag == "a" and self.in_td:
            self.href = dict(attrs).get("href")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self.in_td:
            val = self.cell.strip()
            if self.href:
                val = f"{val}|{self.href}"
            self.current_row.append(val)
            self.in_td = False
        elif tag == "tr" and len(self.current_row) >= 2:
            self.rows.append(self.current_row)

    def handle_data(self, data: str) -> None:
        if self.in_td:
            self.cell += data


def _split_cell(cell: str) -> tuple[str, str | None]:
    if "|" in cell:
        a, b = cell.split("|", 1)
        return a.strip(), b.strip()
    return cell.strip(), None


class SimpleHtmlLiveFetcher(LiveFetcher):
    source_id = "live_simple_html"
    source_name = "Simple HTML Bid Table"
    platform_family = "SimpleHTML"
    expected_kind = "html"

    def structure_recognized(self, body: str, *, list_url: str | None = None) -> bool:
        text = (body or "").lower()
        if "<table" in text and re.search(r"\b(bid|rfp|rfq|solicitation|due|closing)\b", text, re.I):
            return True
        if re.search(r"views-row|field-event-project-number|bid-listings", text, re.I):
            return True
        return False

    def parse_listing(self, body: str, *, list_url: str, meta: dict[str, Any] | None = None) -> list[CanonicalOpportunity]:
        parser = _TableParser()
        parser.feed(body or "")
        out: list[CanonicalOpportunity] = []
        for row in parser.rows:
            if not row or row[0].lower() in {"title", "solicitation", "description", "event id", "event title"}:
                continue
            # Georgia GPR style: Event ID | Event Title | Entity | dates | status
            if len(row) >= 2 and re.match(r"^[A-Za-z0-9\-_./]+$", row[0].split("|")[0].strip()) and len(row[1]) > 10:
                sol, _ = _split_cell(row[0])
                title, href = _split_cell(row[1])
                deadline_raw = row[4] if len(row) > 4 else (row[3] if len(row) > 3 else None)
            else:
                title, href = _split_cell(row[0])
                if not title or len(title) < 3:
                    continue
                sol = row[1] if len(row) > 1 else None
                if sol and "|" in sol:
                    sol, _ = _split_cell(sol)
                deadline_raw = row[2] if len(row) > 2 else None
            if deadline_raw and "|" in str(deadline_raw):
                deadline_raw, _ = _split_cell(deadline_raw)
            if not title or len(title) < 3:
                continue
            # Skip header-ish
            if title.lower() in {"event title", "title", "description"}:
                continue
            dl = normalize_deadline(deadline_raw)
            nt = classify_notice_type(title=title)
            if nt["notice_type"] == NOTICE_AWARDED_CONTRACT_CATALOG:
                continue
            docs = []
            if len(row) > 3:
                _, doc_url = _split_cell(row[3])
                if doc_url and doc_url.startswith("http"):
                    docs.append({"url": doc_url, "kind": "document"})
            out.append(
                CanonicalOpportunity(
                    external_id=str(sol or title)[:160],
                    source_id=self.source_id,
                    source_url=list_url,
                    detail_url=href,
                    title=title,
                    solicitation_number=sol if sol and sol != title else None,
                    deadline_raw=dl.get("deadline_raw"),
                    deadline_timezone=dl.get("timezone"),
                    deadline_tz_confidence=dl.get("timezone_confidence"),
                    document_links=docs,
                    trust_tier=self.trust_tier,
                    raw_metadata={"notice_type": nt["notice_type"], "row": row},
                )
            )
        # Also extract solicitation links from non-table HTML (AR/Sourcewell style)
        if not out:
            for m in re.finditer(
                r'href=["\']([^"\']+)["\'][^>]*>([^<]{12,200}(?:RFP|RFQ|IFB|Bid|Solicitation)[^<]{0,80})<',
                body or "",
                re.I,
            ):
                href, title = m.group(1), re.sub(r"\s+", " ", m.group(2)).strip()
                out.append(
                    CanonicalOpportunity(
                        external_id=title[:160],
                        source_id=self.source_id,
                        source_url=list_url,
                        detail_url=href if href.startswith("http") else None,
                        title=title,
                        trust_tier=self.trust_tier,
                    )
                )
        # Boston.gov Drupal views-row bid listings
        if not out:
            from urllib.parse import urljoin

            for m in re.finditer(
                r'class="[^"]*views-row[^"]*"[\s\S]*?'
                r'field-event-project-number[^>]*>[\s\S]*?<div>\s*([A-Za-z0-9\-]+)\s*</div>[\s\S]*?'
                r'<a href="([^"]+)"[^>]*>([^<]{8,220})</a>[\s\S]*?'
                r'(?:Due:</span>\s*<span class="dl-d">([^<]+)</span>)?',
                body or "",
                re.I,
            ):
                sol, href, title, due = m.group(1), m.group(2), m.group(3).strip(), m.group(4)
                dl = normalize_deadline(due)
                detail = urljoin(list_url, href) if list_url and href.startswith("/") else (href if href.startswith("http") else None)
                out.append(
                    CanonicalOpportunity(
                        external_id=sol,
                        source_id=self.source_id,
                        source_url=list_url,
                        detail_url=detail,
                        title=title,
                        solicitation_number=sol,
                        status="OPEN",
                        deadline_raw=dl.get("deadline_raw"),
                        deadline_timezone=dl.get("timezone"),
                        deadline_tz_confidence=dl.get("timezone_confidence"),
                        document_links=[{"url": detail, "kind": "detail"}] if detail else [],
                        trust_tier=self.trust_tier,
                        raw_metadata={"discovery_route": "agency_bid_listings"},
                    )
                )
        return out


class JsonLiveFetcher(LiveFetcher):
    source_id = "live_json"
    source_name = "Public JSON Listing"
    platform_family = "JSON"
    expected_kind = "json"

    def parse_listing(self, body: str, *, list_url: str, meta: dict[str, Any] | None = None) -> list[CanonicalOpportunity]:
        data = json.loads(body)
        items = data if isinstance(data, list) else (
            data.get("opportunities")
            or data.get("results")
            or data.get("items")
            or data.get("bids")
            or data.get("projects")
            or []
        )
        out = []
        for item in items:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or item.get("projectName") or item.get("name") or "Untitled")
            nt = classify_notice_type(title=title, description=str(item.get("description") or ""), status=str(item.get("status") or ""))
            if nt["is_open_solicitation"] is False and nt["notice_type"] in {
                NOTICE_AWARDED_CONTRACT_CATALOG,
                "AWARD_NOTICE",
            }:
                continue
            dl = normalize_deadline(
                item.get("response_deadline") or item.get("due_date") or item.get("closeDate") or item.get("dueDate"),
                timezone_hint=item.get("timezone"),
                timezone_explicit=bool(item.get("timezone")),
            )
            ext = str(item.get("id") or item.get("external_id") or item.get("projectId") or item.get("solicitation_number") or title)[:160]
            docs = item.get("documents") or item.get("attachments") or []
            doc_links = []
            for d in docs:
                if isinstance(d, str):
                    doc_links.append({"url": d, "kind": "document"})
                elif isinstance(d, dict) and d.get("url"):
                    doc_links.append({"url": d["url"], "kind": d.get("type") or "document", "filename": d.get("name")})
            out.append(
                CanonicalOpportunity(
                    external_id=ext,
                    source_id=self.source_id,
                    source_url=list_url,
                    detail_url=item.get("detail_url") or item.get("publicUrl") or item.get("url"),
                    title=title,
                    solicitation_number=item.get("solicitation_number") or item.get("referenceNumber"),
                    agency=item.get("agency") or item.get("organizationName"),
                    state_code=item.get("state"),
                    city=item.get("city"),
                    description=item.get("description"),
                    status=str(item.get("status") or "OPEN"),
                    deadline_raw=dl.get("deadline_raw"),
                    deadline_timezone=dl.get("timezone"),
                    deadline_tz_confidence=dl.get("timezone_confidence"),
                    document_links=doc_links,
                    amendment_links=[
                        {"url": a.get("url") if isinstance(a, dict) else a, "kind": "amendment"}
                        for a in (item.get("amendments") or [])
                        if (isinstance(a, dict) and a.get("url")) or isinstance(a, str)
                    ],
                    trust_tier=self.trust_tier,
                    raw_metadata={**item, "notice_type": nt["notice_type"]},
                )
            )
        return out


class RssLiveFetcher(LiveFetcher):
    source_id = "live_rss"
    source_name = "Public RSS/XML Procurement Feed"
    platform_family = "RSS"
    expected_kind = "rss"

    def parse_listing(self, body: str, *, list_url: str, meta: dict[str, Any] | None = None) -> list[CanonicalOpportunity]:
        root = ET.fromstring(body)
        channel = root.find("channel")
        items = channel.findall("item") if channel is not None else root.findall("{http://www.w3.org/2005/Atom}entry")
        if not items and channel is None:
            items = root.findall("item")
        out = []
        for it in items:
            title = (it.findtext("title") or it.findtext("{http://www.w3.org/2005/Atom}title") or "Untitled").strip()
            link = (it.findtext("link") or "")
            if not link:
                link_el = it.find("{http://www.w3.org/2005/Atom}link")
                if link_el is not None:
                    link = link_el.attrib.get("href") or ""
            desc = (it.findtext("description") or it.findtext("{http://www.w3.org/2005/Atom}summary") or "").strip()
            guid = (it.findtext("guid") or link or title).strip()
            nt = classify_notice_type(title=title, description=desc)
            if nt["is_open_solicitation"] is False:
                continue
            dl = normalize_deadline(None)
            m = re.search(r"Deadline:\s*([0-9/\-]+)", desc, re.I)
            if m:
                dl = normalize_deadline(m.group(1))
            out.append(
                CanonicalOpportunity(
                    external_id=guid[:200],
                    source_id=self.source_id,
                    source_url=list_url,
                    detail_url=link or None,
                    title=title,
                    description=desc,
                    deadline_raw=dl.get("deadline_raw"),
                    deadline_timezone=dl.get("timezone"),
                    deadline_tz_confidence=dl.get("timezone_confidence"),
                    document_links=[{"url": link, "kind": "detail"}] if link else [],
                    trust_tier=self.trust_tier,
                    raw_metadata={"notice_type": nt["notice_type"]},
                )
            )
        return out


def _is_cloudflare_challenge(body: str | None) -> bool:
    text = (body or "").lower()
    return "just a moment" in text and ("cloudflare" in text or "cf-ray" in text or "challenge-platform" in text)


class OpenGovLiveFetcher(JsonLiveFetcher):
    """OpenGov public procurement — portal JSON/HTML or agency alternate listings."""

    source_id = "live_opengov"
    source_name = "OpenGov Procurement Family"
    platform_family = "OpenGov"

    def structure_recognized(self, body: str, *, list_url: str | None = None) -> bool:
        if _is_cloudflare_challenge(body):
            return False
        text = (body or "").lower()
        if "procurement.opengov.com" in (list_url or "").lower() or "opengov" in text:
            if re.search(r"proposalDeadline|projectTitle|portal/|/projects/", body or "", re.I):
                return True
            if text.strip().startswith("{") or text.strip().startswith("["):
                return True
        # Agency alternate: Boston-style Drupal bid listings
        if re.search(r"field-event-project-number|views-row|bid-listings", text, re.I):
            return True
        return bool(re.search(r"open\s+solicitation|current\s+bid|proposal\s+deadline", text, re.I))

    def parse_listing(self, body: str, *, list_url: str, meta: dict[str, Any] | None = None) -> list[CanonicalOpportunity]:
        if _is_cloudflare_challenge(body):
            return []
        text = (body or "").strip()
        opps: list[CanonicalOpportunity] = []
        if text.startswith("{") or text.startswith("["):
            opps = super().parse_listing(body, list_url=list_url, meta=meta)
        else:
            # Boston.gov / Drupal views-row bid listings (authoritative agency alternate)
            for m in re.finditer(
                r'class="[^"]*views-row[^"]*"[\s\S]*?'
                r'field-event-project-number[^>]*>[\s\S]*?<div>\s*([A-Za-z0-9\-]+)\s*</div>[\s\S]*?'
                r'<a href="([^"]+)"[^>]*>([^<]{8,220})</a>[\s\S]*?'
                r'(?:Due:</span>\s*<span class="dl-d">([^<]+)</span>)?',
                body or "",
                re.I,
            ):
                sol, href, title, due = m.group(1), m.group(2), m.group(3).strip(), m.group(4)
                dl = normalize_deadline(due)
                detail = href if href.startswith("http") else None
                if href.startswith("/") and list_url:
                    from urllib.parse import urljoin

                    detail = urljoin(list_url, href)
                opps.append(
                    CanonicalOpportunity(
                        external_id=sol,
                        source_id=self.source_id,
                        source_url=list_url,
                        detail_url=detail,
                        title=title,
                        solicitation_number=sol,
                        agency="City of Boston",
                        status="OPEN",
                        deadline_raw=dl.get("deadline_raw"),
                        deadline_timezone=dl.get("timezone"),
                        deadline_tz_confidence=dl.get("timezone_confidence"),
                        document_links=[{"url": detail, "kind": "detail"}] if detail else [],
                        trust_tier=self.trust_tier,
                        raw_metadata={
                            "platform": "OpenGov_or_agency_alternate",
                            "document_access": "PUBLIC_DETAIL_PAGE",
                            "discovery_route": "agency_bid_listings",
                        },
                    )
                )
            if not opps:
                opps = SimpleHtmlLiveFetcher().parse_listing(body, list_url=list_url, meta=meta)
        for o in opps:
            o.source_id = self.source_id
            meta_rm = dict(o.raw_metadata or {})
            meta_rm.setdefault("document_access", "PUBLIC_DETAIL_PAGE")
            if "opengov.com" in (list_url or "").lower():
                meta_rm["downstream_platform"] = "OpenGov"
            o.raw_metadata = meta_rm
        return opps


class BonfireLiveFetcher(JsonLiveFetcher):
    source_id = "live_bonfire"
    source_name = "Bonfire Platform Family"
    platform_family = "Bonfire"

    def parse_listing(self, body: str, *, list_url: str, meta: dict[str, Any] | None = None) -> list[CanonicalOpportunity]:
        text = (body or "").strip()
        if text.startswith("{") or text.startswith("["):
            opps = super().parse_listing(body, list_url=list_url, meta=meta)
        else:
            # Bonfire public portal often embeds project cards
            opps = SimpleHtmlLiveFetcher().parse_listing(body, list_url=list_url, meta=meta)
            if not opps:
                # Card-like titles
                for m in re.finditer(
                    r'data-project-id=["\']([^"\']+)["\'][^>]*>.*?project-title[^>]*>([^<]+)',
                    body or "",
                    re.I | re.S,
                ):
                    opps.append(
                        CanonicalOpportunity(
                            external_id=m.group(1),
                            source_id=self.source_id,
                            source_url=list_url,
                            title=m.group(2).strip(),
                            trust_tier=self.trust_tier,
                        )
                    )
            if not opps:
                # Bonfire public ProjectPublic / opportunity cards
                for m in re.finditer(
                    r'href="([^"]*(?:ProjectPublic|projects/public|PublicPortal)[^"]*)"[^>]*>([^<]{8,220})</a>',
                    body or "",
                    re.I,
                ):
                    href, title = m.group(1), re.sub(r"\s+", " ", m.group(2)).strip()
                    if title.lower() in {"login", "register", "home", "about"}:
                        continue
                    from urllib.parse import urljoin

                    detail = urljoin(list_url, href) if list_url else href
                    opps.append(
                        CanonicalOpportunity(
                            external_id=title[:160],
                            source_id=self.source_id,
                            source_url=list_url,
                            detail_url=detail,
                            title=title,
                            trust_tier=self.trust_tier,
                            raw_metadata={"platform": "Bonfire"},
                        )
                    )
        for o in opps:
            o.source_id = self.source_id
        return opps


class PlanetBidsLiveFetcher(SimpleHtmlLiveFetcher):
    source_id = "live_planetbids"
    source_name = "PlanetBids Platform Family"
    platform_family = "PlanetBids"

    def structure_recognized(self, body: str, *, list_url: str | None = None) -> bool:
        text = (body or "").lower()
        if "planetbids" in text or "pbsystem.planetbids" in (list_url or "").lower():
            return bool(re.search(r"bid\s*title|invitation|open\s+bids|solicitation", text, re.I))
        return SimpleHtmlLiveFetcher.structure_recognized(self, body, list_url=list_url)

    def parse_listing(self, body: str, *, list_url: str, meta: dict[str, Any] | None = None) -> list[CanonicalOpportunity]:
        from urllib.parse import urljoin

        opps = super().parse_listing(body, list_url=list_url, meta=meta)
        # PlanetBids portal rows / bid title links
        if not opps:
            for m in re.finditer(
                r'<a[^>]+href="([^"]+)"[^>]*>((?:[^<]{0,200})(?:RFP|RFQ|IFB|Bid|Invitation)(?:[^<]{0,120}))</a>',
                body or "",
                re.I,
            ):
                href, title = m.group(1), re.sub(r"\s+", " ", m.group(2)).strip()
                if len(title) < 8:
                    continue
                detail = urljoin(list_url, href) if list_url else href
                opps.append(
                    CanonicalOpportunity(
                        external_id=title[:160],
                        source_id=self.source_id,
                        source_url=list_url,
                        detail_url=detail,
                        title=title,
                        trust_tier=self.trust_tier,
                        raw_metadata={"platform": "PlanetBids", "document_access": "UNKNOWN"},
                    )
                )
        for o in opps:
            o.source_id = self.source_id
            o.raw_metadata = {**(o.raw_metadata or {}), "downstream_platform": "PlanetBids"}
        return opps


class PublicPurchaseLiveFetcher(SimpleHtmlLiveFetcher):
    source_id = "live_public_purchase"
    source_name = "Public Purchase Platform Family"
    platform_family = "PublicPurchase"

    def structure_recognized(self, body: str, *, list_url: str | None = None) -> bool:
        text = (body or "").lower()
        if "best deal" in text and "free registration" in text:
            return False
        if "start browsing now" in text and "publicpurchase" in text:
            return False
        if re.search(r"current\s+bids|open\s+bids|bid\s+list|solicitation", text, re.I) and "<table" in text:
            return True
        return False

    def parse_listing(self, body: str, *, list_url: str, meta: dict[str, Any] | None = None) -> list[CanonicalOpportunity]:
        text = (body or "").lower()
        if "best deal" in text or "free registration" in text or "start browsing now" in text:
            return []
        if list_url and re.search(r"/buyer/public/home/?$", list_url, re.I):
            return []
        opps = super().parse_listing(body, list_url=list_url, meta=meta)
        cleaned: list[CanonicalOpportunity] = []
        for o in opps:
            title_l = (o.title or "").lower()
            sol_l = (o.solicitation_number or "").lower()
            if any(
                x in title_l or x in sol_l
                for x in ("best deal", "free registration", "start browsing", "select region", "vendor registration")
            ):
                continue
            o.source_id = self.source_id
            o.raw_metadata = {
                **(o.raw_metadata or {}),
                "document_access": "REGISTRATION_REQUIRED",
                "downstream_platform": "PublicPurchase",
            }
            cleaned.append(o)
        return cleaned


class BidNetLiveFetcher(LiveFetcher):
    """BidNet Direct public open-bids listings — metadata is public; packages often gated."""

    source_id = "live_bidnet"
    source_name = "BidNet Direct Platform Family"
    platform_family = "BidNet"
    expected_kind = "html"

    def structure_recognized(self, body: str, *, list_url: str | None = None) -> bool:
        text = (body or "").lower()
        return bool(
            re.search(r"solicitation-link|mets-table-row|open\s+solicitations|sol-closing-date", text, re.I)
        )

    def parse_listing(self, body: str, *, list_url: str, meta: dict[str, Any] | None = None) -> list[CanonicalOpportunity]:
        from urllib.parse import urljoin, urlparse

        out: list[CanonicalOpportunity] = []
        # Primary: BidNet Direct mets-table-row cards
        for m in re.finditer(
            r'<tr[^>]*class="[^"]*mets-table-row[^"]*"[\s\S]*?</tr>',
            body or "",
            re.I,
        ):
            row = m.group(0)
            link = re.search(
                r'<a[^>]+href="([^"]+)"[^>]*class="[^"]*solicitation-link[^"]*"[^>]*>([^<]+)</a>',
                row,
                re.I,
            ) or re.search(
                r'<a[^>]+class="[^"]*solicitation-link[^"]*"[^>]*href="([^"]+)"[^>]*>([^<]+)</a>',
                row,
                re.I,
            )
            if not link:
                continue
            href, title = link.group(1), re.sub(r"\s+", " ", link.group(2)).strip()
            if title.lower() in {"open solicitations", "closed solicitations"}:
                continue
            closing = re.search(
                r'class="[^"]*sol-closing-date[^"]*"[\s\S]*?class="date-value">([^<]+)<',
                row,
                re.I,
            )
            published = re.search(
                r'class="[^"]*sol-publication-date[^"]*"[\s\S]*?class="date-value">([^<]+)<',
                row,
                re.I,
            )
            region = re.search(r'class="sol-region-item">([^<]+)<', row, re.I)
            # ID from statewide/{id}/abstract or path slug
            sid_m = re.search(r"/solicitations/(?:statewide/)?(\d+)/abstract", href, re.I)
            if not sid_m:
                sid_m = re.search(r"/solicitations/open-bids/([^/?#]+)", href, re.I)
            external_id = sid_m.group(1) if sid_m else title[:160]
            detail = urljoin(list_url, href) if list_url else href
            dl = normalize_deadline(closing.group(1).strip() if closing else None)
            # RFP number sometimes in title
            sol_num = None
            sn = re.search(r"\b((?:RFP|RFQ|IFB|ITB|BID)[\s#:]*[A-Z0-9][A-Z0-9\-_/]{2,})\b", title, re.I)
            if sn:
                sol_num = sn.group(1)
            host = urlparse(list_url or "").netloc
            out.append(
                CanonicalOpportunity(
                    external_id=str(external_id)[:160],
                    source_id=self.source_id,
                    source_url=list_url,
                    detail_url=detail,
                    title=title,
                    solicitation_number=sol_num or (str(external_id) if sid_m else None),
                    agency=region.group(1).strip() if region else None,
                    status="OPEN",
                    deadline_raw=dl.get("deadline_raw"),
                    deadline_timezone=dl.get("timezone"),
                    deadline_tz_confidence=dl.get("timezone_confidence"),
                    document_links=[{"url": detail, "kind": "public_abstract"}],
                    trust_tier=self.trust_tier,
                    raw_metadata={
                        "platform": "BidNet",
                        "document_access": "AUTH_GATED",
                        "public_metadata_only": True,
                        "host": host,
                        "published_raw": published.group(1).strip() if published else None,
                        "notice_type": classify_notice_type(title=title)["notice_type"],
                    },
                )
            )
        if not out:
            # Florida-style title links without mets-table-row wrapper
            for m in re.finditer(
                r'href="([^"]*solicitations/(?:statewide/)?(?:open-bids/)?[^"]+)"[^>]*>([^<]{8,200})</a>',
                body or "",
                re.I,
            ):
                href, title = m.group(1), re.sub(r"\s+", " ", m.group(2)).strip()
                if title.lower() in {"open solicitations", "closed solicitations", "login", "register"}:
                    continue
                if "javascript:" in href.lower():
                    continue
                detail = urljoin(list_url, href) if list_url else href
                sid_m = re.search(r"/solicitations/(?:statewide/)?(\d+)", href, re.I)
                out.append(
                    CanonicalOpportunity(
                        external_id=(sid_m.group(1) if sid_m else title)[:160],
                        source_id=self.source_id,
                        source_url=list_url,
                        detail_url=detail,
                        title=title,
                        status="OPEN",
                        trust_tier=self.trust_tier,
                        raw_metadata={
                            "platform": "BidNet",
                            "document_access": "AUTH_GATED",
                            "public_metadata_only": True,
                        },
                    )
                )
        # Deduplicate by external_id
        seen_ids: set[str] = set()
        deduped: list[CanonicalOpportunity] = []
        for o in out:
            key = str(o.external_id or o.title or "")[:160]
            if not key or key in seen_ids:
                continue
            seen_ids.add(key)
            deduped.append(o)
        return deduped


class JaggaerPublicLiveFetcher(LiveFetcher):
    """Jaggaer/SciQuest public event portals (Iowa IMPACS, Montana eMACS, etc.)."""

    source_id = "live_jaggaer"
    source_name = "Jaggaer/SciQuest Public Portal Family"
    platform_family = "Jaggaer"
    expected_kind = "html"

    def structure_recognized(self, body: str, *, list_url: str | None = None) -> bool:
        from discovery.sciquest import sciquest_has_public_event_structure

        return sciquest_has_public_event_structure(body or "")

    def parse_listing(self, body: str, *, list_url: str, meta: dict[str, Any] | None = None) -> list[CanonicalOpportunity]:
        from discovery.sciquest import parse_sciquest_public_events

        opps = parse_sciquest_public_events(
            body,
            list_url=list_url,
            source_id=self.source_id,
            trust_tier=self.trust_tier,
        )
        for o in opps:
            o.source_id = self.source_id
        return opps


class StateOwnedHtmlLiveFetcher(SimpleHtmlLiveFetcher):
    source_id = "live_state_owned_html"
    source_name = "State-Owned HTML Portal"
    platform_family = "StateOwned"


class CooperativeOpenSolicitationFetcher(JsonLiveFetcher):
    """Cooperative OPEN solicitations only — skips awarded contract catalogs."""

    source_id = "live_cooperative"
    source_name = "Cooperative Open Solicitation"
    platform_family = "JSON"
    trust_tier = TIER_2
    expected_kind = "html"

    def structure_recognized(self, body: str, *, list_url: str | None = None) -> bool:
        from discovery.sourcewell import sourcewell_has_open_structure

        if list_url and "sourcewell" in (list_url or "").lower():
            return sourcewell_has_open_structure(body or "")
        text = (body or "").lower()
        return "solicitation" in text or text.strip().startswith("{") or text.strip().startswith("[")

    def parse_listing(self, body: str, *, list_url: str, meta: dict[str, Any] | None = None) -> list[CanonicalOpportunity]:
        text = (body or "").strip()
        if list_url and "sourcewell" in list_url.lower():
            from discovery.sourcewell import parse_sourcewell_open_solicitations

            return parse_sourcewell_open_solicitations(
                body,
                list_url=list_url,
                source_id=self.source_id,
                trust_tier=self.trust_tier,
            )
        if text.startswith("{") or text.startswith("["):
            data = json.loads(body)
            if isinstance(data, dict) and "contracts" in data and "opportunities" not in data and "solicitations" not in data:
                return []
            if isinstance(data, dict) and "solicitations" in data:
                body = json.dumps({"opportunities": data["solicitations"]})
            opps = JsonLiveFetcher.parse_listing(self, body, list_url=list_url, meta=meta)
        else:
            opps = SimpleHtmlLiveFetcher().parse_listing(body, list_url=list_url, meta=meta)
        filtered = []
        for o in opps:
            o.source_id = self.source_id
            o.buyer_type = "COOPERATIVE"
            o.jurisdiction = "COOPERATIVE"
            o.trust_tier = TIER_2
            nt = classify_notice_type(title=o.title, description=o.description)
            if nt["notice_type"] == NOTICE_AWARDED_CONTRACT_CATALOG:
                continue
            if nt.get("is_open_solicitation") is False:
                continue
            filtered.append(o)
        return filtered


class FederalPublicPageLiveFetcher(JsonLiveFetcher):
    source_id = "live_federal_public"
    source_name = "Federal Public Notice Pages"
    platform_family = "JSON"

    def parse_listing(self, body: str, *, list_url: str, meta: dict[str, Any] | None = None) -> list[CanonicalOpportunity]:
        text = (body or "").strip()
        if text.startswith("{") or text.startswith("["):
            opps = super().parse_listing(body, list_url=list_url, meta=meta)
        else:
            opps = SimpleHtmlLiveFetcher().parse_listing(body, list_url=list_url, meta=meta)
        out = []
        for o in opps:
            o.source_id = self.source_id
            o.jurisdiction = "FEDERAL"
            o.buyer_type = "FEDERAL_PUBLIC"
            nt = classify_notice_type(title=o.title, description=o.description)
            o.raw_metadata = {**(o.raw_metadata or {}), "notice_type": nt["notice_type"]}
            if nt["notice_type"] in {"AWARD_NOTICE", "FORECAST", "CANCELLED"}:
                continue
            out.append(o)
        return out


# SPE* / NSN patterns used by DLA DIBBS public RFQ listings (parser only — not injected IDs)
_SPE_SOL_RE = re.compile(
    r"\b(SPE[0-9A-Z]{2,4}[-]?[0-9]{2,3}[-]?[A-Z]?[-]?[0-9A-Z]{3,6})\b",
    re.I,
)
_NSN_RE = re.compile(r"\b(\d{4}-\d{2}-\d{3}-\d{4})\b")


class DibbsLiveFetcher(LiveFetcher):
    """DLA Internet Bid Board System (DIBBS) — public RFQ search/list pages."""

    source_id = "live_dibbs"
    source_name = "DLA DIBBS Public RFQs"
    platform_family = "DIBBS"
    expected_kind = "html"

    def structure_recognized(self, body: str, *, list_url: str | None = None) -> bool:
        text = (body or "").lower()
        if "dod warning" in text and "consent" in text and "rfq" not in text:
            return False
        return bool(
            re.search(r"\b(rfq|solicitation|nsn|dibbs|return\s*by|issue\s*date)\b", text, re.I)
            and (_SPE_SOL_RE.search(body or "") or _NSN_RE.search(body or "") or "<table" in text)
        )

    def parse_listing(self, body: str, *, list_url: str, meta: dict[str, Any] | None = None) -> list[CanonicalOpportunity]:
        from urllib.parse import urljoin

        text = body or ""
        out: list[CanonicalOpportunity] = []
        seen: set[str] = set()

        # Table / link rows containing SPE* solicitation numbers
        for m in re.finditer(
            r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>\s*(' + _SPE_SOL_RE.pattern + r')\s*</a>',
            text,
            re.I,
        ):
            href, sol = m.group(1), m.group(2).upper().replace(" ", "")
            if sol in seen:
                continue
            seen.add(sol)
            # Nearby NSN in a window after the match
            window = text[m.end() : m.end() + 400]
            nsn_m = _NSN_RE.search(window)
            title_m = re.search(r">([A-Za-z][^<]{8,120})<", window)
            title = (title_m.group(1).strip() if title_m else f"DLA RFQ {sol}")
            if nsn_m:
                title = f"{title} NSN {nsn_m.group(1)}"
            detail = urljoin(list_url, href) if list_url else href
            out.append(
                CanonicalOpportunity(
                    external_id=sol,
                    source_id=self.source_id,
                    source_url=list_url,
                    detail_url=detail,
                    title=title[:500],
                    solicitation_number=sol,
                    agency="Defense Logistics Agency",
                    status="OPEN",
                    jurisdiction="FEDERAL",
                    buyer_type="FEDERAL_PUBLIC",
                    trust_tier=self.trust_tier,
                    raw_metadata={
                        "platform": "DIBBS",
                        "nsn": nsn_m.group(1) if nsn_m else None,
                        "notice_type": "RFQ",
                        "document_access": "PUBLIC_OR_VENDOR_PORTAL",
                    },
                )
            )

        # Fallback: bare SPE tokens near RFQ chrome
        if not out:
            for m in _SPE_SOL_RE.finditer(text):
                sol = m.group(1).upper().replace(" ", "")
                if sol in seen:
                    continue
                seen.add(sol)
                window = text[max(0, m.start() - 80) : m.end() + 200]
                nsn_m = _NSN_RE.search(window)
                title = f"DLA RFQ {sol}"
                if nsn_m:
                    title = f"{title} NSN {nsn_m.group(1)}"
                out.append(
                    CanonicalOpportunity(
                        external_id=sol,
                        source_id=self.source_id,
                        source_url=list_url,
                        title=title,
                        solicitation_number=sol,
                        agency="Defense Logistics Agency",
                        status="OPEN",
                        jurisdiction="FEDERAL",
                        buyer_type="FEDERAL_PUBLIC",
                        trust_tier=self.trust_tier,
                        raw_metadata={
                            "platform": "DIBBS",
                            "nsn": nsn_m.group(1) if nsn_m else None,
                            "notice_type": "RFQ",
                        },
                    )
                )
        return out


class PieePublicLiveFetcher(SimpleHtmlLiveFetcher):
    """PIEE public (unauthenticated) solicitation index — DoD product/service notices."""

    source_id = "live_piee_public"
    source_name = "PIEE Public Solicitation Search"
    platform_family = "PIEE"
    expected_kind = "html"

    def structure_recognized(self, body: str, *, list_url: str | None = None) -> bool:
        text = (body or "").lower()
        return bool(
            re.search(r"\b(solicitation|piee|opportunity|rfq|rfp|ifb)\b", text, re.I)
            and ("piee" in text or "eb.mil" in (list_url or "").lower() or "<table" in text)
        )

    def parse_listing(self, body: str, *, list_url: str, meta: dict[str, Any] | None = None) -> list[CanonicalOpportunity]:
        opps = super().parse_listing(body, list_url=list_url, meta=meta)
        out: list[CanonicalOpportunity] = []
        for o in opps:
            o.source_id = self.source_id
            o.jurisdiction = "FEDERAL"
            o.buyer_type = "FEDERAL_PUBLIC"
            o.raw_metadata = {**(o.raw_metadata or {}), "platform": "PIEE", "document_access": "PUBLIC_INDEX"}
            out.append(o)
        # Also capture W91*/SPE*/N00* style DoD solicitation numbers in links
        from urllib.parse import urljoin

        seen = {o.external_id for o in out}
        for m in re.finditer(
            r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>\s*([A-Z0-9]{2,6}[-]?[A-Z0-9]{2,14})\s*</a>',
            body or "",
            re.I,
        ):
            href, sol = m.group(1), m.group(2).upper()
            if sol in seen or len(sol) < 6:
                continue
            if not re.match(r"^(SPE|W91|W91[A-Z]|N00|FA|SPR)", sol, re.I):
                continue
            seen.add(sol)
            detail = urljoin(list_url, href) if list_url else href
            out.append(
                CanonicalOpportunity(
                    external_id=sol,
                    source_id=self.source_id,
                    source_url=list_url,
                    detail_url=detail,
                    title=f"DoD solicitation {sol}",
                    solicitation_number=sol,
                    agency="Department of Defense",
                    status="OPEN",
                    jurisdiction="FEDERAL",
                    buyer_type="FEDERAL_PUBLIC",
                    trust_tier=self.trust_tier,
                    raw_metadata={"platform": "PIEE", "notice_type": "SOLICITATION"},
                )
            )
        return out


LIVE_FETCHERS: dict[str, LiveFetcher] = {
    SimpleHtmlLiveFetcher.source_id: SimpleHtmlLiveFetcher(),
    JsonLiveFetcher.source_id: JsonLiveFetcher(),
    RssLiveFetcher.source_id: RssLiveFetcher(),
    OpenGovLiveFetcher.source_id: OpenGovLiveFetcher(),
    BonfireLiveFetcher.source_id: BonfireLiveFetcher(),
    PlanetBidsLiveFetcher.source_id: PlanetBidsLiveFetcher(),
    PublicPurchaseLiveFetcher.source_id: PublicPurchaseLiveFetcher(),
    BidNetLiveFetcher.source_id: BidNetLiveFetcher(),
    JaggaerPublicLiveFetcher.source_id: JaggaerPublicLiveFetcher(),
    StateOwnedHtmlLiveFetcher.source_id: StateOwnedHtmlLiveFetcher(),
    CooperativeOpenSolicitationFetcher.source_id: CooperativeOpenSolicitationFetcher(),
    FederalPublicPageLiveFetcher.source_id: FederalPublicPageLiveFetcher(),
    DibbsLiveFetcher.source_id: DibbsLiveFetcher(),
    PieePublicLiveFetcher.source_id: PieePublicLiveFetcher(),
}

PLATFORM_TO_FETCHER = {
    "SimpleHTML": "live_simple_html",
    "JSON": "live_json",
    "RSS": "live_rss",
    "OpenGov": "live_opengov",
    "Bonfire": "live_bonfire",
    "PlanetBids": "live_planetbids",
    "PublicPurchase": "live_public_purchase",
    "BidNet": "live_bidnet",
    "Jaggaer": "live_jaggaer",
    "StateOwned": "live_state_owned_html",
    "DIBBS": "live_dibbs",
    "PIEE": "live_piee_public",
    "FederalPublic": "live_federal_public",
}


def get_live_fetcher(fetcher_id: str) -> LiveFetcher | None:
    return LIVE_FETCHERS.get(fetcher_id)


def get_fetcher_for_platform(platform_family: str) -> LiveFetcher | None:
    fid = PLATFORM_TO_FETCHER.get(platform_family)
    return LIVE_FETCHERS.get(fid) if fid else None


def list_live_capable_fetchers() -> list[dict[str, str]]:
    return [
        {
            "source_id": f.source_id,
            "source_name": f.source_name,
            "platform_family": f.platform_family,
            "adapter_status": f.adapter_status,
        }
        for f in LIVE_FETCHERS.values()
    ]


def should_fetch_detail(
    opp: CanonicalOpportunity,
    *,
    classification: str | None = None,
    fetch_details_enabled: bool = True,
) -> dict[str, Any]:
    """
    Deterministic detail-fetch gate.
    UNKNOWN alone does NOT earn a PDF/detail download.
    Listing-only profiles pass fetch_details_enabled=False.
    """
    if not fetch_details_enabled:
        return {"fetch": False, "reason": "profile_listing_only", "earned": False}

    cls = classification or classify_discovery_opportunity(
        title=opp.title, description=opp.description, status=opp.status
    )["classification"]

    st = str(opp.status or "").lower()
    if st in {"closed", "cancelled", "canceled", "awarded"}:
        return {"fetch": False, "reason": "not_open", "earned": False, "classification": cls}
    if cls == "SERVICE":
        return {"fetch": False, "reason": "service_heavy", "earned": False, "classification": cls}
    if cls == "CLEARLY_IRRELEVANT":
        return {"fetch": False, "reason": "irrelevant", "earned": False, "classification": cls}
    if cls == "UNKNOWN":
        return {"fetch": False, "reason": "unknown_does_not_earn_detail", "earned": False, "classification": cls}

    # CORE_PRODUCT / PRODUCT_PLUS_SERVICE earn detail when profile allows
    if cls in {"CORE_PRODUCT", "PRODUCT_PLUS_SERVICE"}:
        if not (opp.detail_url or (opp.document_links or [])):
            return {"fetch": False, "reason": "no_detail_url", "earned": True, "classification": cls}
        return {"fetch": True, "reason": "product_signal", "earned": True, "classification": cls}

    return {"fetch": False, "reason": "not_eligible", "earned": False, "classification": cls}
