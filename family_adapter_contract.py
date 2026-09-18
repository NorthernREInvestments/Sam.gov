"""Reusable platform-family adapter contract — wraps existing LiveFetchers."""

from __future__ import annotations

import hashlib
import re
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from discovery.live_fetchers import LiveFetcher, get_fetcher_for_platform, get_live_fetcher
from discovery.platform_detect import detect_platform
from discovery.schema import CanonicalOpportunity

DOC_PUBLIC_DIRECT = "PUBLIC_DIRECT"
DOC_PUBLIC_DETAIL = "PUBLIC_DETAIL_PAGE"
DOC_AUTH_GATED = "AUTH_GATED"
DOC_REGISTRATION = "REGISTRATION_REQUIRED"
DOC_UNAVAILABLE = "UNAVAILABLE"
DOC_UNKNOWN = "UNKNOWN"

PAGINATION_PAGE = "PAGE_NUMBER"
PAGINATION_PATH_PAGE = "PATH_PAGE"  # e.g. BidNet /open-bids/page2
PAGINATION_OFFSET = "OFFSET"
PAGINATION_CURSOR = "CURSOR"
PAGINATION_LOAD_MORE = "LOAD_MORE"
PAGINATION_BOUNDED = "BOUNDED_WINDOW"
PAGINATION_NONE = "NO_PAGINATION"
PAGINATION_UNKNOWN = "UNKNOWN"


def fingerprint_platform(
    *,
    url: str | None = None,
    html: str | None = None,
    content_type: str | None = None,
    registry_family: str | None = None,
) -> dict[str, Any]:
    """
    Fingerprint confidence: VERIFIED | HIGH_CONFIDENCE | POSSIBLE | UNKNOWN
    Weak evidence must not become fact.
    """
    det = detect_platform(url, html_snippet=html, content_type=content_type)
    platform = det.get("platform")
    conf = str(det.get("confidence") or "LOW").upper()
    if registry_family and platform and str(registry_family).lower() == str(platform).lower():
        level = "VERIFIED"
    elif conf == "HIGH":
        level = "HIGH_CONFIDENCE"
    elif conf == "MEDIUM":
        level = "POSSIBLE"
    else:
        level = "UNKNOWN"
        if not platform or platform == "UNKNOWN":
            platform = registry_family or "UNKNOWN"
            if registry_family and level == "UNKNOWN":
                # Registry says family but page evidence weak — keep POSSIBLE not VERIFIED
                level = "POSSIBLE" if registry_family else "UNKNOWN"
    return {
        "platform_family": platform if level != "UNKNOWN" or registry_family else "UNKNOWN",
        "confidence": level,
        "detection": det.get("detection"),
        "registry_family": registry_family,
        "assigned_as_fact": level == "VERIFIED",
        "raw_detect": det,
    }


def detect_pagination_model(html: str | None, url: str | None = None) -> dict[str, Any]:
    text = (html or "").lower()
    qs = parse_qs(urlparse(url or "").query) if url else {}
    # BidNet Direct / similar: /solicitations/open-bids/page2
    path_pages = re.findall(
        r'href=["\']([^"\']*?/solicitations/open-bids/page\d+)["\']',
        html or "",
        re.I,
    )
    if path_pages or re.search(r"/solicitations/open-bids/page\d+", url or "", re.I):
        max_page = 1
        for href in path_pages:
            m = re.search(r"/page(\d+)\s*$", href, re.I)
            if m:
                max_page = max(max_page, int(m.group(1)))
        return {
            "model": PAGINATION_PATH_PAGE,
            "evidence": "bidnet_path_page",
            "max_page_hint": max_page,
        }
    if "page" in qs or "pagenumber" in {k.lower() for k in qs} or re.search(r"[?&]page=\d+", url or "", re.I):
        return {"model": PAGINATION_PAGE, "evidence": "url_query"}
    if "offset" in qs or "start" in qs:
        return {"model": PAGINATION_OFFSET, "evidence": "url_query"}
    if "cursor" in qs or "continuation" in qs:
        return {"model": PAGINATION_CURSOR, "evidence": "url_query"}
    if re.search(r"\b(next\s*page|pagination|pager|page\s*\d+\s*of)\b", text):
        return {"model": PAGINATION_PAGE, "evidence": "html_marker"}
    if re.search(r"\b(load\s*more|show\s*more)\b", text):
        return {"model": PAGINATION_LOAD_MORE, "evidence": "html_marker"}
    if re.search(r"\b(showing\s+\d+\s*[-–]\s*\d+\s*of\s+\d+)\b", text):
        return {"model": PAGINATION_BOUNDED, "evidence": "html_marker"}
    if html is not None and not re.search(r"page|pagination|next", text):
        return {"model": PAGINATION_NONE, "evidence": "no_markers"}
    return {"model": PAGINATION_UNKNOWN, "evidence": "insufficient"}


def next_page_url(list_url: str, *, page: int, model: str = PAGINATION_PAGE) -> str | None:
    """Build next listing URL for page-number / offset / path pagination. page is 1-indexed."""
    if page <= 1:
        return list_url
    parsed = urlparse(list_url)
    if model == PAGINATION_PATH_PAGE:
        path = re.sub(r"/page\d+/?$", "", parsed.path or "", flags=re.I).rstrip("/")
        if "bidnetdirect.com" in (parsed.netloc or "").lower() and "/solicitations/" not in path.lower():
            path = f"{path}/solicitations/open-bids"
        new_path = f"{path}/page{page}"
        return urlunparse((parsed.scheme, parsed.netloc, new_path, "", "", ""))
    qs = parse_qs(parsed.query, keep_blank_values=True)
    if model == PAGINATION_OFFSET:
        page_size = int((qs.get("limit") or qs.get("pageSize") or ["25"])[0] or 25)
        qs["offset"] = [str((page - 1) * page_size)]
    else:
        if "pageNumber" in qs or "PageNumber" in qs:
            key = "pageNumber" if "pageNumber" in qs else "PageNumber"
            qs[key] = [str(page)]
        elif "page" in qs or not qs:
            qs["page"] = [str(page)]
        else:
            qs["page"] = [str(page)]
    new_q = urlencode({k: v[0] if len(v) == 1 else v for k, v in qs.items()}, doseq=True)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_q, parsed.fragment))


def normalize_bidnet_open_bids_url(url: str | None) -> str | None:
    """Map BidNet Direct landing URLs to public open-bids listing."""
    if not url:
        return url
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    if "bidnetdirect.com" not in host:
        return url
    path = (parsed.path or "").rstrip("/")
    if re.search(r"/solicitations/open-bids(?:/page\d+)?$", path, re.I):
        base_path = re.sub(r"/page\d+$", "", path, flags=re.I)
        return urlunparse((parsed.scheme or "https", parsed.netloc, base_path, "", "", ""))
    m = re.match(r"^/([a-z0-9\-]+)$", path, re.I)
    if m:
        slug = m.group(1).lower()
        if slug not in {"public", "cms", "jawr", "login", "register"}:
            return urlunparse(
                (parsed.scheme or "https", parsed.netloc, f"/{slug}/solicitations/open-bids", "", "", "")
            )
    return url


def classify_document_access(
    *,
    doc_urls: list[str] | None = None,
    detail_requires_login: bool = False,
    listing_mentions_login_for_docs: bool = False,
    registration_required: bool = False,
) -> dict[str, Any]:
    if registration_required:
        state = DOC_REGISTRATION
    elif detail_requires_login or listing_mentions_login_for_docs:
        state = DOC_AUTH_GATED
    elif doc_urls:
        state = DOC_PUBLIC_DIRECT
    elif not doc_urls and not detail_requires_login:
        state = DOC_PUBLIC_DETAIL
    else:
        state = DOC_UNKNOWN
    return {
        "document_access": state,
        "retain_opportunity": True,  # never discard for auth-gated packages
        "document_urls": doc_urls or [],
    }


def schema_fingerprint(body: str | None, *, expected_markers: list[str] | None = None) -> str:
    text = body or ""
    markers = expected_markers or ["table", "solicitation", "bid", "rfp", "deadline", "due"]
    present = [m for m in markers if m.lower() in text.lower()]
    raw = "|".join(present) + f"|len={len(text)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def evaluate_zero_result(
    *,
    status_code: int | None,
    body: str | None,
    records_found: int,
    structure_recognized: bool,
    prior_avg_records: float | None = None,
    prior_schema_fp: str | None = None,
) -> dict[str, Any]:
    """Distinguish VALID_ZERO_RESULTS from POSSIBLE_PARSER_FAILURE."""
    if records_found > 0:
        return {"state": "NON_ZERO", "healthy_eligible": True}
    text = (body or "").lower()
    empty_markers = bool(re.search(r"(no\s+(open\s+)?(bids?|solicitations?|results)|0\s+results|nothing\s+found)", text))
    schema_fp = schema_fingerprint(body)
    schema_changed = bool(prior_schema_fp and prior_schema_fp != schema_fp and not empty_markers)
    if status_code and status_code >= 400:
        return {
            "state": "POSSIBLE_PARSER_FAILURE",
            "reason": f"http_{status_code}",
            "healthy_eligible": False,
            "schema_fingerprint": schema_fp,
        }
    if schema_changed:
        return {
            "state": "POSSIBLE_PARSER_FAILURE",
            "reason": "schema_fingerprint_changed",
            "healthy_eligible": False,
            "schema_fingerprint": schema_fp,
            "prior_schema_fp": prior_schema_fp,
        }
    if not structure_recognized and not empty_markers:
        return {
            "state": "POSSIBLE_PARSER_FAILURE",
            "reason": "structure_not_recognized",
            "healthy_eligible": False,
            "schema_fingerprint": schema_fp,
        }
    if empty_markers or (structure_recognized and prior_avg_records is not None and prior_avg_records < 1):
        return {
            "state": "VALID_ZERO_RESULTS",
            "reason": "empty_listing_or_historically_empty",
            "healthy_eligible": True,
            "schema_fingerprint": schema_fp,
        }
    if structure_recognized and (prior_avg_records is None or prior_avg_records >= 1):
        return {
            "state": "POSSIBLE_PARSER_FAILURE",
            "reason": "zero_against_historical_volume",
            "healthy_eligible": False,
            "schema_fingerprint": schema_fp,
            "prior_avg_records": prior_avg_records,
        }
    return {
        "state": "POSSIBLE_PARSER_FAILURE",
        "reason": "zero_without_empty_marker",
        "healthy_eligible": False,
        "schema_fingerprint": schema_fp,
    }


def evaluate_schema_change(
    *,
    body: str | None,
    prior_schema_fp: str | None,
    expected_fields_present: list[str] | None = None,
) -> dict[str, Any]:
    fp = schema_fingerprint(body)
    text = body or ""
    missing = [f for f in (expected_fields_present or []) if f.lower() not in text.lower()]
    changed = bool(prior_schema_fp and prior_schema_fp != fp)
    material = changed or (len(missing) >= 2)
    return {
        "schema_fingerprint": fp,
        "changed": changed,
        "material_change": material,
        "missing_expected_markers": missing,
        "health_if_material": "DEGRADED" if material else "OK",
        "silent_zero_success_forbidden": True,
    }


class FamilyAdapter:
    """
    Common family-adapter interface over existing LiveFetcher implementations.
    Missing fields stay UNKNOWN — do not force unsupported capabilities.
    """

    def __init__(self, fetcher: LiveFetcher, *, config: dict[str, Any] | None = None) -> None:
        self.fetcher = fetcher
        self.config = config or {}

    @property
    def platform_family(self) -> str:
        return self.fetcher.platform_family

    @property
    def adapter_id(self) -> str:
        return self.fetcher.source_id

    def discover_listing_pages(self, list_url: str | None = None, *, max_pages: int = 3) -> list[str]:
        base = list_url or self.config.get("listing_path") or self.config.get("base_url") or ""
        if not base:
            return []
        model = self.config.get("pagination_model") or PAGINATION_PAGE
        pages = [base]
        for p in range(2, max_pages + 1):
            nxt = next_page_url(base, page=p, model=model)
            if nxt and nxt not in pages:
                pages.append(nxt)
        return pages

    def parse_listing(self, body: str, *, list_url: str) -> list[CanonicalOpportunity]:
        return self.fetcher.parse_listing(body, list_url=list_url, meta=None)

    def normalize_opportunity(self, opp: CanonicalOpportunity) -> dict[str, Any]:
        return {
            "external_id": opp.external_id,
            "title": opp.title,
            "solicitation_number": opp.solicitation_number or "UNKNOWN",
            "buyer": opp.agency or "UNKNOWN",
            "status": opp.status or "UNKNOWN",
            "deadline": opp.deadline_raw or "UNKNOWN",
            "posted_date": (opp.raw_metadata or {}).get("posted_date") or "UNKNOWN",
            "modified_date": (opp.raw_metadata or {}).get("modified_date") or "UNKNOWN",
            "procurement_type": (opp.raw_metadata or {}).get("notice_type") or "UNKNOWN",
            "public_detail_url": opp.detail_url or "UNKNOWN",
            "source_id": opp.source_id,
            "platform_family": self.platform_family,
        }

    def get_detail_metadata(self, body: str, *, detail_url: str) -> dict[str, Any]:
        docs = self.fetcher.discover_documents(body, detail_url=detail_url)
        return {"detail_url": detail_url, "documents": docs, "fields": "UNKNOWN"}

    def discover_document_links(self, body: str, *, detail_url: str | None = None) -> list[dict[str, Any]]:
        return self.fetcher.discover_documents(body, detail_url=detail_url)

    def detect_pagination(self, body: str | None, url: str | None = None) -> dict[str, Any]:
        return detect_pagination_model(body, url)

    def compute_source_fingerprint(self, body: str | None) -> str:
        return schema_fingerprint(body)

    def checkpoint_cursor(self, *, last_successful: str | None, overlap_hours: int = 24) -> dict[str, Any]:
        from discovery_checkpoint import incremental_window

        return incremental_window(
            {"last_successful_checkpoint": last_successful, "overlap_hours": overlap_hours}
        )

    def detect_changes(self, prior_fp: str | None, body: str | None) -> dict[str, Any]:
        return evaluate_schema_change(body=body, prior_schema_fp=prior_fp)

    def fetch_paginated(
        self,
        client: Any,
        *,
        list_url: str,
        source_id: str,
        max_pages: int = 3,
    ) -> dict[str, Any]:
        """Fetch beyond page 1 when pagination exists — no silent first-page-only coverage."""
        pages = self.discover_listing_pages(list_url, max_pages=max_pages)
        all_opps: list[CanonicalOpportunity] = []
        page_results = []
        requests = 0
        for i, page_url in enumerate(pages):
            if i == 0:
                result = self.fetcher.fetch_listing(
                    client, list_url=page_url, source_id=source_id, max_pages=1
                )
            else:
                # Subsequent pages — still use fetcher parse path
                resp = client.get(page_url, source_id=source_id)
                requests += 1 if getattr(client, "authorize_live", False) else 0
                parsed = self.parse_listing(resp.text, list_url=page_url)
                result = {
                    "opportunities": parsed,
                    "request_meta": resp.meta.to_dict(),
                    "validation": {"health": "UNKNOWN"},
                    "pages_fetched": 1,
                    "LIVE_API_REQUESTS": 1 if getattr(client, "authorize_live", False) else 0,
                }
            opps = result.get("opportunities") or []
            # Dedup within source by external_id
            seen = {o.external_id for o in all_opps}
            new = [o for o in opps if o.external_id not in seen]
            all_opps.extend(new)
            page_results.append(
                {
                    "page": i + 1,
                    "url": page_url,
                    "records": len(opps),
                    "new_records": len(new),
                }
            )
            requests += int(result.get("LIVE_API_REQUESTS") or 0)
            if i > 0 and len(new) == 0:
                break  # no further pages with content
        pag = self.detect_pagination(
            None,  # body optional
            list_url,
        )
        return {
            "opportunities": all_opps,
            "pages_fetched": len(page_results),
            "page_results": page_results,
            "pagination_model": pag.get("model"),
            "beyond_page_1": any(p["page"] > 1 and p["records"] > 0 for p in page_results),
            "LIVE_API_REQUESTS": requests,
            "validation": (page_results[0] if page_results else {}),
        }


def get_family_adapter(platform_family: str, *, config: dict[str, Any] | None = None) -> FamilyAdapter | None:
    fetcher = get_fetcher_for_platform(platform_family)
    if not fetcher:
        return None
    return FamilyAdapter(fetcher, config=config)


def get_family_adapter_by_id(adapter_id: str, *, config: dict[str, Any] | None = None) -> FamilyAdapter | None:
    fetcher = get_live_fetcher(adapter_id)
    if not fetcher:
        return None
    return FamilyAdapter(fetcher, config=config)


# Recognized alternative families without fabricating availability
IONWAVE_NOTE = {
    "platform_family": "IonWave",
    "adapter_present": False,
    "typical_access": "AUTH_GATED_OR_VARIANT",
    "public_discovery": "UNKNOWN",
}
