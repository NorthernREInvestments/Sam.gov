"""Controlled public procurement HTTP client — polite, accountable, mockable."""

from __future__ import annotations
from application_clock import now_utc

import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlparse

DEFAULT_UA = "GovTrackerDiscovery/1.0 (+operator-controlled; respectful; contact: operator)"


@dataclass
class RequestMeta:
    url: str
    http_status: int | None = None
    timestamp: str | None = None
    content_type: str | None = None
    bytes_len: int = 0
    cache_hit: bool = False
    etag: str | None = None
    last_modified: str | None = None
    error: str | None = None
    host: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "http_status": self.http_status,
            "timestamp": self.timestamp,
            "content_type": self.content_type,
            "bytes": self.bytes_len,
            "cache_hit": self.cache_hit,
            "etag": self.etag,
            "last_modified": self.last_modified,
            "error": self.error,
            "host": self.host,
        }


@dataclass
class HttpResponse:
    text: str
    content: bytes
    status_code: int
    headers: dict[str, str]
    meta: RequestMeta


@dataclass
class RequestBudget:
    max_total_requests: int = 100
    max_requests_per_source: int = 20
    max_pages_per_source: int = 1
    max_records_per_source: int = 100
    max_runtime_seconds: float = 120.0
    max_retries: int = 1
    min_interval_seconds: float = 2.0
    timeout_seconds: float = 20.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_total_requests": self.max_total_requests,
            "max_requests_per_source": self.max_requests_per_source,
            "max_pages_per_source": self.max_pages_per_source,
            "max_records_per_source": self.max_records_per_source,
            "max_runtime_seconds": self.max_runtime_seconds,
            "max_retries": self.max_retries,
            "min_interval_seconds": self.min_interval_seconds,
            "timeout_seconds": self.timeout_seconds,
        }


class BudgetExhausted(Exception):
    """Raised when a request budget limit is hit."""

    def __init__(self, message: str, *, kind: str = "global") -> None:
        super().__init__(message)
        self.kind = kind  # source | global | runtime


class SourceBudgetExhausted(BudgetExhausted):
    def __init__(self, source_id: str) -> None:
        super().__init__(f"per-source request budget exhausted for {source_id}", kind="source")
        self.source_id = source_id


class GlobalBudgetExhausted(BudgetExhausted):
    def __init__(self) -> None:
        super().__init__("global request budget exhausted", kind="global")


class RuntimeBudgetExhausted(BudgetExhausted):
    def __init__(self) -> None:
        super().__init__("max runtime exhausted", kind="runtime")


class PublicProcurementHttpClient:
    """
    Single controlled client for discovery.
    Inject transport for tests — no live network unless authorize_live=True.
    """

    def __init__(
        self,
        *,
        budget: RequestBudget | None = None,
        authorize_live: bool = False,
        transport: Callable[..., HttpResponse] | None = None,
        user_agent: str = DEFAULT_UA,
    ) -> None:
        self.budget = budget or RequestBudget()
        self.authorize_live = authorize_live
        self.transport = transport
        self.user_agent = user_agent
        self.request_count = 0
        self.requests_by_source: dict[str, int] = {}
        self.requests_by_host: dict[str, int] = {}
        self.request_log: list[dict[str, Any]] = []
        self._cache: dict[str, HttpResponse] = {}
        self._etag_map: dict[str, str] = {}
        self._last_modified_map: dict[str, str] = {}
        self._last_host_at: dict[str, float] = {}
        self._started_at = time.monotonic()
        self.cache_hits = 0

    def accounting(self) -> dict[str, Any]:
        return {
            "request_count": self.request_count,
            "cache_hits": self.cache_hits,
            "requests_by_source": dict(self.requests_by_source),
            "requests_by_host": dict(self.requests_by_host),
            "request_log": list(self.request_log),
            "budget": self.budget.to_dict(),
            "elapsed_seconds": round(time.monotonic() - self._started_at, 3),
            "SAM": 0,
            "OpenAI": 0,
            "USAspending": 0,
            "paid": 0,
            "public_procurement_live_requests": self.request_count if self.authorize_live else 0,
        }

    def _check_budget(self, source_id: str) -> None:
        if self.request_count >= self.budget.max_total_requests:
            raise GlobalBudgetExhausted()
        if self.requests_by_source.get(source_id, 0) >= self.budget.max_requests_per_source:
            raise SourceBudgetExhausted(source_id)
        if (time.monotonic() - self._started_at) >= self.budget.max_runtime_seconds:
            raise RuntimeBudgetExhausted()

    def get(
        self,
        url: str,
        *,
        source_id: str = "unknown",
        headers: dict[str, str] | None = None,
        use_cache: bool = True,
        conditional: bool = True,
    ) -> HttpResponse:
        if not self.authorize_live and self.transport is None:
            raise PermissionError(
                "Live HTTP requires authorize_live=True or an injected test transport"
            )

        cache_key = url
        if use_cache and cache_key in self._cache:
            self.cache_hits += 1
            cached = self._cache[cache_key]
            meta = RequestMeta(
                url=url,
                http_status=cached.status_code,
                timestamp=now_utc().isoformat(),
                content_type=cached.headers.get("content-type"),
                bytes_len=len(cached.content),
                cache_hit=True,
                etag=cached.headers.get("etag"),
                last_modified=cached.headers.get("last-modified"),
                host=urlparse(url).netloc,
            )
            self.request_log.append(meta.to_dict())
            return HttpResponse(
                text=cached.text,
                content=cached.content,
                status_code=cached.status_code,
                headers=cached.headers,
                meta=meta,
            )

        self._check_budget(source_id)
        host = urlparse(url).netloc.lower()
        # Per-host polite interval
        last = self._last_host_at.get(host)
        if last is not None:
            wait = self.budget.min_interval_seconds - (time.monotonic() - last)
            if wait > 0 and self.authorize_live and self.transport is None:
                time.sleep(wait)

        req_headers = {"User-Agent": self.user_agent, "Accept": "*/*"}
        if headers:
            req_headers.update(headers)
        if conditional:
            if url in self._etag_map:
                req_headers["If-None-Match"] = self._etag_map[url]
            if url in self._last_modified_map:
                req_headers["If-Modified-Since"] = self._last_modified_map[url]

        attempts = 0
        last_exc: Exception | None = None
        while attempts <= self.budget.max_retries:
            attempts += 1
            try:
                if self.transport is not None:
                    resp = self.transport(url, headers=req_headers, timeout=self.budget.timeout_seconds)
                else:
                    resp = self._live_httpx(url, req_headers)
                break
            except Exception as exc:
                last_exc = exc
                if attempts > self.budget.max_retries:
                    meta = RequestMeta(
                        url=url,
                        timestamp=now_utc().isoformat(),
                        error=str(exc),
                        host=host,
                    )
                    self.request_count += 1
                    self.requests_by_source[source_id] = self.requests_by_source.get(source_id, 0) + 1
                    self.requests_by_host[host] = self.requests_by_host.get(host, 0) + 1
                    self.request_log.append(meta.to_dict())
                    raise
                time.sleep(min(2.0 * attempts, self.budget.min_interval_seconds))
        else:
            raise last_exc or RuntimeError("HTTP failed")

        self.request_count += 1
        self.requests_by_source[source_id] = self.requests_by_source.get(source_id, 0) + 1
        self.requests_by_host[host] = self.requests_by_host.get(host, 0) + 1
        self._last_host_at[host] = time.monotonic()

        etag = resp.headers.get("etag") or resp.headers.get("ETag")
        lm = resp.headers.get("last-modified") or resp.headers.get("Last-Modified")
        if etag:
            self._etag_map[url] = etag
        if lm:
            self._last_modified_map[url] = lm

        meta = RequestMeta(
            url=url,
            http_status=resp.status_code,
            timestamp=now_utc().isoformat(),
            content_type=resp.headers.get("content-type"),
            bytes_len=len(resp.content),
            cache_hit=False,
            etag=etag,
            last_modified=lm,
            host=host,
        )
        self.request_log.append(meta.to_dict())
        out = HttpResponse(
            text=resp.text,
            content=resp.content,
            status_code=resp.status_code,
            headers=dict(resp.headers),
            meta=meta,
        )
        if use_cache and resp.status_code == 200:
            self._cache[cache_key] = out
        return out

    def _live_httpx(self, url: str, headers: dict[str, str]) -> HttpResponse:
        import httpx

        with httpx.Client(timeout=self.budget.timeout_seconds, follow_redirects=True) as client:
            r = client.get(url, headers=headers)
            return HttpResponse(
                text=r.text,
                content=r.content,
                status_code=r.status_code,
                headers={k.lower(): v for k, v in r.headers.items()},
                meta=RequestMeta(url=url),
            )


def content_hash(data: str | bytes) -> str:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()
