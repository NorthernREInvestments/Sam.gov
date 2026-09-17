"""Fixture-backed discovery adapters proving architecture (no live network)."""

from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from html.parser import HTMLParser
from typing import Any

from discovery.constants import (
    ADAPTER_FIXTURE_ONLY,
    ADAPTER_IMPLEMENTED,  # alias → FIXTURE_ONLY
    SOURCE_CITY,
    SOURCE_COOPERATIVE,
    SOURCE_FEDERAL_PUBLIC,
    SOURCE_STATE,
    TIER_1,
    TIER_2,
)
from discovery.deadline import normalize_deadline
from discovery.schema import CanonicalOpportunity


def _hash(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:40]


class _BidTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_td = False
        self.in_a = False
        self.current_row: list[str] = []
        self.rows: list[list[str]] = []
        self.current_href: str | None = None
        self.cell_text = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self.current_row = []
        elif tag == "td":
            self.in_td = True
            self.cell_text = ""
            self.current_href = None
        elif tag == "a" and self.in_td:
            self.in_a = True
            href = dict(attrs).get("href")
            if href:
                self.current_href = href

    def handle_endtag(self, tag: str) -> None:
        if tag == "td" and self.in_td:
            val = self.cell_text.strip()
            if self.current_href:
                val = f"{val}|{self.current_href}"
            self.current_row.append(val)
            self.in_td = False
        elif tag == "tr" and self.current_row:
            self.rows.append(self.current_row)

    def handle_data(self, data: str) -> None:
        if self.in_td:
            self.cell_text += data


class SimpleHtmlBidTableAdapter:
    """A. Official simple HTML bid table."""

    source_id = "fixture_html_city_bids"
    source_name = "Fixture City Bid Table"
    source_type = SOURCE_CITY
    jurisdiction = "LOCAL"
    discovery_method = "HTML_TABLE"
    adapter_status = ADAPTER_FIXTURE_ONLY
    trust_tier = TIER_1

    def preflight(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "would_call_live": False,
            "adapter_status": self.adapter_status,
            "live_capable": False,
            "LIVE_API_REQUESTS": 0,
        }

    def parse_fixture(self, payload: str | bytes | dict[str, Any], **kwargs: Any) -> list[CanonicalOpportunity]:
        html = payload.decode("utf-8") if isinstance(payload, bytes) else str(payload)
        parser = _BidTableParser()
        parser.feed(html)
        out: list[CanonicalOpportunity] = []
        for row in parser.rows:
            if len(row) < 3:
                continue
            if row[0].lower().startswith("solicitation") or row[0].lower() == "title":
                continue
            title_cell = row[0]
            title, href = (title_cell.split("|", 1) + [None])[:2] if "|" in title_cell else (title_cell, None)
            sol = row[1] if len(row) > 1 else None
            deadline_raw = row[2] if len(row) > 2 else None
            dl = normalize_deadline(deadline_raw)
            docs = []
            if len(row) > 3 and row[3].startswith("http"):
                docs.append({"url": row[3].split("|")[0], "kind": "solicitation"})
            out.append(
                CanonicalOpportunity(
                    external_id=str(sol or title)[:120],
                    source_id=self.source_id,
                    source_url=kwargs.get("list_url"),
                    detail_url=href,
                    title=title or "Untitled",
                    solicitation_number=sol,
                    agency=kwargs.get("agency") or "Fixture City Purchasing",
                    jurisdiction="CITY",
                    buyer_type=SOURCE_CITY,
                    state_code=kwargs.get("state_code") or "TX",
                    city=kwargs.get("city") or "Austin",
                    deadline_raw=dl["deadline_raw"],
                    deadline_timezone=dl["timezone"],
                    deadline_tz_confidence=dl["timezone_confidence"],
                    response_deadline=datetime.fromisoformat(dl["utc_deadline"]) if dl.get("utc_deadline") else None,
                    document_links=docs,
                    trust_tier=self.trust_tier,
                    content_hash=_hash(html),
                    raw_metadata={"row": row, "adapter": self.source_id},
                )
            )
        return out

    def fetch(self, *, authorize_live: bool = False, **kwargs: Any) -> list[CanonicalOpportunity]:
        if not authorize_live:
            raise PermissionError("live fetch requires authorize_live=True")
        raise NotImplementedError("Live HTML fetch not run in this build — use fixtures / smoke CLI")


class JsonPublicEndpointAdapter:
    """B. Official JSON/public endpoint format."""

    source_id = "fixture_json_state_bids"
    source_name = "Fixture State JSON Portal"
    source_type = SOURCE_STATE
    jurisdiction = "STATE"
    discovery_method = "JSON_ENDPOINT"
    adapter_status = ADAPTER_FIXTURE_ONLY
    trust_tier = TIER_1

    def preflight(self, **kwargs: Any) -> dict[str, Any]:
        return {"source_id": self.source_id, "would_call_live": False, "LIVE_API_REQUESTS": 0}

    def parse_fixture(self, payload: str | bytes | dict[str, Any], **kwargs: Any) -> list[CanonicalOpportunity]:
        if isinstance(payload, dict):
            data = payload
        else:
            raw = payload.decode("utf-8") if isinstance(payload, bytes) else str(payload)
            data = json.loads(raw)
        items = data.get("opportunities") or data.get("results") or data.get("items") or []
        out = []
        for item in items:
            dl = normalize_deadline(
                item.get("response_deadline") or item.get("due_date"),
                timezone_hint=item.get("timezone"),
                timezone_explicit=bool(item.get("timezone")),
            )
            docs = item.get("documents") or []
            amends = item.get("amendments") or []
            out.append(
                CanonicalOpportunity(
                    external_id=str(item.get("id") or item.get("external_id") or ""),
                    source_id=self.source_id,
                    source_url=item.get("source_url"),
                    detail_url=item.get("detail_url"),
                    title=str(item.get("title") or "Untitled"),
                    solicitation_number=item.get("solicitation_number"),
                    agency=item.get("agency"),
                    subagency=item.get("department"),
                    jurisdiction="STATE",
                    buyer_type=SOURCE_STATE,
                    state_code=item.get("state") or kwargs.get("state_code") or "CA",
                    city=item.get("city"),
                    posted_date=None,
                    deadline_raw=dl["deadline_raw"],
                    deadline_timezone=dl["timezone"],
                    deadline_tz_confidence=dl["timezone_confidence"],
                    response_deadline=datetime.fromisoformat(dl["utc_deadline"]) if dl.get("utc_deadline") else None,
                    status=str(item.get("status") or "OPEN"),
                    procurement_method=item.get("procurement_method"),
                    set_aside=item.get("set_aside"),
                    naics=item.get("naics"),
                    description=item.get("description"),
                    estimated_value=item.get("estimated_value"),
                    estimated_value_status="KNOWN" if item.get("estimated_value") else "UNKNOWN",
                    document_links=[{"url": d, "kind": "document"} if isinstance(d, str) else d for d in docs],
                    amendment_links=[{"url": a, "kind": "amendment"} if isinstance(a, str) else a for a in amends],
                    qa_links=item.get("qa_links") or [],
                    submission_method=item.get("submission_method"),
                    contact=item.get("contact") or {},
                    trust_tier=self.trust_tier,
                    raw_metadata=dict(item),
                    content_hash=_hash(json.dumps(item, sort_keys=True, default=str)),
                )
            )
        return out

    def fetch(self, *, authorize_live: bool = False, **kwargs: Any) -> list[CanonicalOpportunity]:
        if not authorize_live:
            raise PermissionError("live fetch requires authorize_live=True")
        raise NotImplementedError("Live JSON fetch not run in this build")


class RssProcurementFeedAdapter:
    """C. RSS/XML procurement feed."""

    source_id = "fixture_rss_county_bids"
    source_name = "Fixture County RSS Feed"
    source_type = "COUNTY"
    jurisdiction = "COUNTY"
    discovery_method = "RSS"
    adapter_status = ADAPTER_FIXTURE_ONLY
    trust_tier = TIER_1

    def preflight(self, **kwargs: Any) -> dict[str, Any]:
        return {"source_id": self.source_id, "would_call_live": False, "LIVE_API_REQUESTS": 0}

    def parse_fixture(self, payload: str | bytes | dict[str, Any], **kwargs: Any) -> list[CanonicalOpportunity]:
        xml = payload.decode("utf-8") if isinstance(payload, bytes) else str(payload)
        root = ET.fromstring(xml)
        channel = root.find("channel")
        items = channel.findall("item") if channel is not None else root.findall("item")
        out = []
        for it in items:
            title = (it.findtext("title") or "Untitled").strip()
            link = (it.findtext("link") or "").strip()
            desc = (it.findtext("description") or "").strip()
            guid = (it.findtext("guid") or link or title).strip()
            # Optional custom fields
            sol = None
            for child in it:
                if child.tag.endswith("solicitationNumber") or child.tag == "solicitationNumber":
                    sol = child.text
            dl = normalize_deadline(None)
            # Look for deadline in description
            m = re.search(r"Deadline:\s*([0-9/\-]+)", desc, re.I)
            if m:
                dl = normalize_deadline(m.group(1))
            out.append(
                CanonicalOpportunity(
                    external_id=guid[:200],
                    source_id=self.source_id,
                    source_url=kwargs.get("list_url"),
                    detail_url=link or None,
                    title=title,
                    solicitation_number=sol,
                    agency=kwargs.get("agency") or "Fixture County",
                    jurisdiction="COUNTY",
                    buyer_type="COUNTY",
                    state_code=kwargs.get("state_code") or "FL",
                    description=desc,
                    deadline_raw=dl["deadline_raw"],
                    deadline_timezone=dl["timezone"],
                    deadline_tz_confidence=dl["timezone_confidence"],
                    document_links=[{"url": link, "kind": "detail"}] if link else [],
                    trust_tier=self.trust_tier,
                    raw_metadata={"guid": guid},
                    content_hash=_hash(xml),
                )
            )
        return out

    def fetch(self, *, authorize_live: bool = False, **kwargs: Any) -> list[CanonicalOpportunity]:
        if not authorize_live:
            raise PermissionError("live fetch requires authorize_live=True")
        raise NotImplementedError("Live RSS fetch not run in this build")


class CooperativeListingAdapter:
    """D. Cooperative opportunity listing."""

    source_id = "fixture_coop_sourcewell_style"
    source_name = "Fixture Cooperative Listing"
    source_type = SOURCE_COOPERATIVE
    jurisdiction = "NATIONAL"
    discovery_method = "JSON_ENDPOINT"
    adapter_status = ADAPTER_FIXTURE_ONLY
    trust_tier = TIER_2

    def preflight(self, **kwargs: Any) -> dict[str, Any]:
        return {"source_id": self.source_id, "would_call_live": False, "LIVE_API_REQUESTS": 0}

    def parse_fixture(self, payload: str | bytes | dict[str, Any], **kwargs: Any) -> list[CanonicalOpportunity]:
        data = payload if isinstance(payload, dict) else json.loads(
            payload.decode("utf-8") if isinstance(payload, bytes) else str(payload)
        )
        out = []
        for item in data.get("contracts") or data.get("opportunities") or []:
            out.append(
                CanonicalOpportunity(
                    external_id=str(item.get("contract_number") or item.get("id") or ""),
                    source_id=self.source_id,
                    source_url=item.get("url"),
                    detail_url=item.get("url"),
                    title=str(item.get("title") or "Untitled"),
                    solicitation_number=item.get("contract_number"),
                    agency=item.get("cooperative") or "Fixture Cooperative",
                    jurisdiction="COOPERATIVE",
                    buyer_type=SOURCE_COOPERATIVE,
                    description=item.get("description"),
                    status=str(item.get("status") or "OPEN"),
                    estimated_value=item.get("estimated_value"),
                    estimated_value_status="KNOWN" if item.get("estimated_value") else "UNKNOWN",
                    document_links=[{"url": u, "kind": "document"} for u in (item.get("documents") or [])],
                    trust_tier=self.trust_tier,
                    raw_metadata=dict(item),
                    content_hash=_hash(json.dumps(item, sort_keys=True, default=str)),
                )
            )
        return out

    def fetch(self, *, authorize_live: bool = False, **kwargs: Any) -> list[CanonicalOpportunity]:
        if not authorize_live:
            raise PermissionError("live fetch requires authorize_live=True")
        raise NotImplementedError("Live cooperative fetch not run in this build")


class FederalPublicNoticeAdapter:
    """E. Federal public notice/page (non-SAM API)."""

    source_id = "fixture_federal_public_notice"
    source_name = "Fixture Federal Public Notice Page"
    source_type = SOURCE_FEDERAL_PUBLIC
    jurisdiction = "FEDERAL"
    discovery_method = "PUBLIC_HTML"
    adapter_status = ADAPTER_FIXTURE_ONLY
    trust_tier = TIER_1

    def preflight(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "would_call_live": False,
            "sam_api": 0,
            "note": "Non-SAM federal public page — SAM API not used",
            "LIVE_API_REQUESTS": 0,
        }

    def parse_fixture(self, payload: str | bytes | dict[str, Any], **kwargs: Any) -> list[CanonicalOpportunity]:
        # Accept JSON representing a scraped public notice (fixture)
        data = payload if isinstance(payload, dict) else json.loads(
            payload.decode("utf-8") if isinstance(payload, bytes) else str(payload)
        )
        notices = data if isinstance(data, list) else data.get("notices") or [data]
        out = []
        for n in notices:
            dl = normalize_deadline(
                n.get("response_deadline"),
                timezone_hint=n.get("timezone"),
                timezone_explicit=bool(n.get("timezone")),
            )
            out.append(
                CanonicalOpportunity(
                    external_id=str(n.get("notice_id") or n.get("id") or ""),
                    source_id=self.source_id,
                    source_url=n.get("source_url"),
                    detail_url=n.get("detail_url"),
                    title=str(n.get("title") or "Untitled"),
                    solicitation_number=n.get("solicitation_number"),
                    agency=n.get("agency"),
                    subagency=n.get("office"),
                    jurisdiction="FEDERAL",
                    buyer_type=SOURCE_FEDERAL_PUBLIC,
                    state_code=None,  # do not fabricate
                    set_aside=n.get("set_aside"),
                    naics=n.get("naics"),
                    psc=n.get("psc"),
                    description=n.get("description"),
                    deadline_raw=dl["deadline_raw"],
                    deadline_timezone=dl["timezone"],
                    deadline_tz_confidence=dl["timezone_confidence"],
                    response_deadline=datetime.fromisoformat(dl["utc_deadline"]) if dl.get("utc_deadline") else None,
                    document_links=n.get("document_links") or [],
                    amendment_links=n.get("amendment_links") or [],
                    submission_method=n.get("submission_method"),
                    trust_tier=self.trust_tier,
                    raw_metadata=dict(n),
                    content_hash=_hash(json.dumps(n, sort_keys=True, default=str)),
                )
            )
        return out

    def fetch(self, *, authorize_live: bool = False, **kwargs: Any) -> list[CanonicalOpportunity]:
        if not authorize_live:
            raise PermissionError("live fetch requires authorize_live=True")
        raise NotImplementedError("Live federal public fetch not run — SAM API still disabled")


class SharedPlatformStyleAdapter:
    """F. Shared-platform style listing (Bonfire/OpenGov-like JSON shape)."""

    source_id = "fixture_shared_platform_listing"
    source_name = "Fixture Shared Platform Style"
    source_type = SOURCE_CITY
    jurisdiction = "LOCAL"
    discovery_method = "PLATFORM_FAMILY"
    adapter_status = ADAPTER_FIXTURE_ONLY
    trust_tier = TIER_1

    def preflight(self, **kwargs: Any) -> dict[str, Any]:
        return {"source_id": self.source_id, "platform_family": "Bonfire-like", "LIVE_API_REQUESTS": 0}

    def parse_fixture(self, payload: str | bytes | dict[str, Any], **kwargs: Any) -> list[CanonicalOpportunity]:
        data = payload if isinstance(payload, dict) else json.loads(
            payload.decode("utf-8") if isinstance(payload, bytes) else str(payload)
        )
        projects = data.get("projects") or data.get("bids") or []
        out = []
        for p in projects:
            dl = normalize_deadline(p.get("closeDate") or p.get("dueDate"))
            out.append(
                CanonicalOpportunity(
                    external_id=str(p.get("projectId") or p.get("id") or ""),
                    source_id=self.source_id,
                    source_url=p.get("publicUrl"),
                    detail_url=p.get("publicUrl"),
                    title=str(p.get("projectName") or p.get("title") or "Untitled"),
                    solicitation_number=p.get("referenceNumber") or p.get("solicitation_number"),
                    agency=p.get("organizationName") or kwargs.get("agency") or "Fixture Agency",
                    jurisdiction="CITY",
                    buyer_type=SOURCE_CITY,
                    state_code=p.get("state") or "WA",
                    city=p.get("city"),
                    description=p.get("description"),
                    deadline_raw=dl["deadline_raw"],
                    deadline_timezone=dl["timezone"],
                    deadline_tz_confidence=dl["timezone_confidence"],
                    document_links=[
                        {"url": a.get("url"), "kind": a.get("type") or "attachment", "filename": a.get("name")}
                        for a in (p.get("attachments") or [])
                        if a.get("url")
                    ],
                    amendment_links=[
                        {"url": a.get("url"), "kind": "amendment"}
                        for a in (p.get("amendments") or [])
                        if a.get("url")
                    ],
                    trust_tier=self.trust_tier,
                    raw_metadata=dict(p),
                    content_hash=_hash(json.dumps(p, sort_keys=True, default=str)),
                )
            )
        return out

    def fetch(self, *, authorize_live: bool = False, **kwargs: Any) -> list[CanonicalOpportunity]:
        if not authorize_live:
            raise PermissionError("live fetch requires authorize_live=True")
        raise NotImplementedError("Live platform fetch not run in this build")


IMPLEMENTED_ADAPTERS = {
    SimpleHtmlBidTableAdapter.source_id: SimpleHtmlBidTableAdapter(),
    JsonPublicEndpointAdapter.source_id: JsonPublicEndpointAdapter(),
    RssProcurementFeedAdapter.source_id: RssProcurementFeedAdapter(),
    CooperativeListingAdapter.source_id: CooperativeListingAdapter(),
    FederalPublicNoticeAdapter.source_id: FederalPublicNoticeAdapter(),
    SharedPlatformStyleAdapter.source_id: SharedPlatformStyleAdapter(),
}


def get_implemented_adapter(source_id: str):
    return IMPLEMENTED_ADAPTERS.get(source_id)


def list_implemented_adapters() -> list[dict[str, str]]:
    return [
        {
            "source_id": a.source_id,
            "source_name": a.source_name,
            "source_type": a.source_type,
            "adapter_status": a.adapter_status,
        }
        for a in IMPLEMENTED_ADAPTERS.values()
    ]
