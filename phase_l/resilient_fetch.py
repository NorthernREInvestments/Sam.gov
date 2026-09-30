"""Phase L.2.7 — resilient HTTP fetch with timeouts, size caps, domain circuit breaker."""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

FETCH_OK = "FETCH_OK"
FETCH_TIMEOUT = "FETCH_TIMEOUT"
FETCH_403 = "FETCH_403"
FETCH_429 = "FETCH_429"
FETCH_BOT_BLOCKED = "FETCH_BOT_BLOCKED"
FETCH_JS_EMPTY = "FETCH_JS_EMPTY"
FETCH_TOO_LARGE = "FETCH_TOO_LARGE"
FETCH_INVALID = "FETCH_INVALID"
FETCH_ERROR = "FETCH_ERROR"
FETCH_SKIPPED_CIRCUIT = "FETCH_SKIPPED_CIRCUIT"

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

_BOT_MARKERS = (
    "captcha",
    "recaptcha",
    "hcaptcha",
    "cf-browser-verification",
    "attention required",
    "access denied",
    "bot detection",
    "please enable javascript",
    "enable cookies",
    "unusual traffic",
    "just a moment...",
    "checking your browser",
    "captcha page",
    "whoops, we couldn't find that",
    "request blocked",
    "perimeterx",
    "akamai bot",
)


def domain_of(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower().replace("www.", "")
        return host or "unknown"
    except Exception:
        return "unknown"


@dataclass
class DomainCircuitBreaker:
    """Skip domains that repeatedly fail within a run."""

    fail_threshold: int = 3
    failures: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    successes: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    degraded: set[str] = field(default_factory=set)
    skipped: set[str] = field(default_factory=set)
    events: list[dict[str, Any]] = field(default_factory=list)

    def allow(self, url: str) -> bool:
        d = domain_of(url)
        if d in self.skipped:
            return False
        return True

    def record(self, url: str, status: str) -> None:
        d = domain_of(url)
        bad = status in {
            FETCH_TIMEOUT,
            FETCH_403,
            FETCH_429,
            FETCH_BOT_BLOCKED,
            FETCH_JS_EMPTY,
            FETCH_ERROR,
        }
        if status == FETCH_OK:
            self.successes[d] += 1
            self.failures[d] = 0
            self.degraded.discard(d)
        elif bad:
            self.failures[d] += 1
            if self.failures[d] >= self.fail_threshold:
                self.degraded.add(d)
                self.skipped.add(d)
                self.events.append({"domain": d, "action": "SKIP_FOR_RUN", "status": status})
            elif self.failures[d] >= 2:
                self.degraded.add(d)
                self.events.append({"domain": d, "action": "DEGRADED", "status": status})

    def telemetry(self) -> dict[str, Any]:
        return {
            "degraded": sorted(self.degraded),
            "skipped": sorted(self.skipped),
            "failures": dict(self.failures),
            "successes": dict(self.successes),
            "events": self.events[-40:],
        }


@dataclass
class FetchResult:
    status: str
    url: str
    content: bytes = b""
    text: str = ""
    content_type: str = ""
    status_code: int = 0
    elapsed_ms: float = 0.0
    error: str | None = None
    domain: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "url": self.url,
            "content_type": self.content_type,
            "status_code": self.status_code,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "error": self.error,
            "domain": self.domain or domain_of(self.url),
            "bytes": len(self.content),
            "text_len": len(self.text),
        }


def _looks_bot_blocked(status_code: int, text: str) -> bool:
    low = (text or "")[:12000].lower()
    if status_code in {401, 403, 429, 503}:
        if status_code in {403, 429}:
            return True
        if any(m in low for m in _BOT_MARKERS):
            return True
    # Soft blocks served as HTTP 200 (captcha interstitial / soft 404 shell)
    strong = (
        "captcha page",
        "recaptcha",
        "hcaptcha",
        "just a moment...",
        "cf-browser-verification",
        "whoops, we couldn't find that",
        "access denied",
        "perimeterx",
    )
    if any(m in low for m in strong):
        return True
    return any(m in low for m in _BOT_MARKERS) and len(text or "") < 25000


def _looks_js_empty(text: str, content_type: str) -> bool:
    if "html" not in (content_type or "").lower() and text.startswith("<"):
        pass
    if not text or len(text.strip()) < 200:
        return True
    low = text.lower()
    # Shell with almost no product content
    if "application/json" in (content_type or "").lower():
        return False
    href_n = low.count("href=")
    if href_n <= 2 and ("<script" in low or "noscript" in low) and len(text) < 40000:
        return True
    visible = len(re_sub_tags(text))
    if visible < 120 and ("<script" in low or "noscript" in low):
        return True
    return False


def re_sub_tags(html: str) -> str:
    import re

    return re.sub(r"<[^>]+>", " ", html or "")


def resilient_fetch(
    url: str,
    *,
    breaker: DomainCircuitBreaker | None = None,
    connect_timeout: float = 3.0,
    read_timeout: float = 6.0,
    max_bytes: int = 1_500_000,
    max_redirects: int = 4,
    retries: int = 0,
) -> FetchResult:
    """Hard-bounded fetch. Never hangs unbounded."""
    breaker = breaker or DomainCircuitBreaker()
    d = domain_of(url)
    if not breaker.allow(url):
        return FetchResult(status=FETCH_SKIPPED_CIRCUIT, url=url, domain=d)

    import httpx

    last_err: str | None = None
    for attempt in range(max(1, retries + 1)):
        t0 = time.monotonic()
        try:
            timeout = httpx.Timeout(connect_timeout, read=read_timeout, write=read_timeout, pool=connect_timeout)
            with httpx.Client(
                timeout=timeout,
                follow_redirects=True,
                max_redirects=max_redirects,
                headers=UA,
            ) as client:
                with client.stream("GET", url) as r:
                    ctype = (r.headers.get("content-type") or "").split(";")[0].strip()
                    chunks: list[bytes] = []
                    total = 0
                    too_large = False
                    for chunk in r.iter_bytes():
                        total += len(chunk)
                        if total > max_bytes:
                            too_large = True
                            break
                        chunks.append(chunk)
                    raw = b"".join(chunks)
                    elapsed = (time.monotonic() - t0) * 1000
                    if too_large:
                        fr = FetchResult(
                            status=FETCH_TOO_LARGE,
                            url=str(r.url),
                            content=raw[: max_bytes // 10],
                            content_type=ctype,
                            status_code=r.status_code,
                            elapsed_ms=elapsed,
                            domain=d,
                        )
                        breaker.record(url, fr.status)
                        return fr
                    text = ""
                    try:
                        text = raw.decode("utf-8", errors="ignore")
                    except Exception:
                        text = ""
                    if r.status_code == 403:
                        fr = FetchResult(
                            status=FETCH_403,
                            url=str(r.url),
                            content=raw,
                            text=text[:50000],
                            content_type=ctype,
                            status_code=r.status_code,
                            elapsed_ms=elapsed,
                            domain=d,
                        )
                        breaker.record(url, fr.status)
                        return fr
                    if r.status_code == 429:
                        fr = FetchResult(
                            status=FETCH_429,
                            url=str(r.url),
                            content=raw,
                            text=text[:50000],
                            content_type=ctype,
                            status_code=r.status_code,
                            elapsed_ms=elapsed,
                            domain=d,
                        )
                        breaker.record(url, fr.status)
                        return fr
                    if r.status_code >= 400:
                        fr = FetchResult(
                            status=FETCH_ERROR,
                            url=str(r.url),
                            content=raw,
                            text=text[:20000],
                            content_type=ctype,
                            status_code=r.status_code,
                            elapsed_ms=elapsed,
                            domain=d,
                            error=f"http_{r.status_code}",
                        )
                        breaker.record(url, fr.status)
                        return fr
                    if _looks_bot_blocked(r.status_code, text):
                        fr = FetchResult(
                            status=FETCH_BOT_BLOCKED,
                            url=str(r.url),
                            content=raw,
                            text=text[:50000],
                            content_type=ctype,
                            status_code=r.status_code,
                            elapsed_ms=elapsed,
                            domain=d,
                        )
                        breaker.record(url, fr.status)
                        return fr
                    if _looks_js_empty(text, ctype) and "pdf" not in ctype and "sheet" not in ctype and "csv" not in ctype:
                        # allow binary docs
                        if not (raw[:4] == b"%PDF" or raw[:2] == b"PK"):
                            fr = FetchResult(
                                status=FETCH_JS_EMPTY,
                                url=str(r.url),
                                content=raw,
                                text=text[:50000],
                                content_type=ctype,
                                status_code=r.status_code,
                                elapsed_ms=elapsed,
                                domain=d,
                            )
                            breaker.record(url, fr.status)
                            return fr
                    fr = FetchResult(
                        status=FETCH_OK,
                        url=str(r.url),
                        content=raw,
                        text=text[:250000],
                        content_type=ctype,
                        status_code=r.status_code,
                        elapsed_ms=elapsed,
                        domain=d,
                    )
                    breaker.record(url, fr.status)
                    return fr
        except httpx.TimeoutException as exc:
            last_err = str(exc)[:160]
            fr = FetchResult(
                status=FETCH_TIMEOUT,
                url=url,
                elapsed_ms=(time.monotonic() - t0) * 1000,
                domain=d,
                error=last_err,
            )
            breaker.record(url, fr.status)
            if attempt >= retries:
                return fr
            time.sleep(0.15)
        except Exception as exc:
            last_err = str(exc)[:160]
            fr = FetchResult(
                status=FETCH_ERROR,
                url=url,
                elapsed_ms=(time.monotonic() - t0) * 1000,
                domain=d,
                error=last_err,
            )
            breaker.record(url, fr.status)
            if attempt >= retries:
                return fr
            time.sleep(0.15)

    return FetchResult(status=FETCH_ERROR, url=url, domain=d, error=last_err or "unknown")
