"""Bounded HTTP client with SSRF protections."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

import httpx

from m3.config import get_settings

# Browser-like UA: many distributor CDNs block non-browser research agents with 403.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
}


def _host_is_public(hostname: str) -> bool:
    if not hostname or hostname.lower() in {"localhost", "metadata.google.internal"}:
        return False
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = info[4][0]
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_reserved
            or addr.is_multicast
        ):
            return False
    return True


def validate_public_https_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("only_http_https")
    if parsed.scheme != "https" and not parsed.hostname.endswith(".gov"):
        # Prefer https; allow http only for .gov legacy boards
        if not (parsed.hostname or "").endswith((".gov", ".us")):
            raise ValueError("https_required")
    host = parsed.hostname or ""
    if not _host_is_public(host):
        raise ValueError("ssrf_blocked_host")
    return url


import threading

_thread_client: threading.local = threading.local()


def _shared_client(timeout: float) -> httpx.Client:
    """Reuse HTTP connections per thread (Build 002F)."""
    client = getattr(_thread_client, "client", None)
    t = getattr(_thread_client, "timeout", None)
    if client is None or t != timeout or client.is_closed:
        if client is not None and not client.is_closed:
            try:
                client.close()
            except Exception:
                pass
        client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers=DEFAULT_HEADERS,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
        _thread_client.client = client
        _thread_client.timeout = timeout
    return client


def fetch_text(url: str, *, timeout: float | None = None) -> tuple[int, str, str]:
    """Returns status, final_url, text."""
    from m3.research.perf import timed

    settings = get_settings()
    safe = validate_public_https_url(url)
    t = timeout or settings.research_timeout_seconds
    with timed("http_fetch_text"):
        client = _shared_client(t)
        resp = client.get(safe)
        validate_public_https_url(str(resp.url))
        return resp.status_code, str(resp.url), resp.text


def fetch_json(url: str, *, timeout: float | None = None) -> tuple[int, str, object]:
    status, final, text = fetch_text(url, timeout=timeout)
    import json

    try:
        return status, final, json.loads(text)
    except json.JSONDecodeError:
        return status, final, None


def fetch_bytes(
    url: str, *, timeout: float | None = None, max_bytes: int = 25_000_000
) -> tuple[int, str, bytes, str]:
    """Returns status, final_url, body bytes, content_type. Bounded size."""
    from m3.research.perf import timed

    settings = get_settings()
    safe = validate_public_https_url(url)
    t = timeout or settings.research_timeout_seconds
    with timed("http_fetch_bytes"):
        client = _shared_client(t)
        with client.stream("GET", safe) as resp:
            validate_public_https_url(str(resp.url))
            ctype = resp.headers.get("content-type", "") or ""
            chunks: list[bytes] = []
            total = 0
            for chunk in resp.iter_bytes():
                chunks.append(chunk)
                total += len(chunk)
                if total > max_bytes:
                    break
            return resp.status_code, str(resp.url), b"".join(chunks), ctype
