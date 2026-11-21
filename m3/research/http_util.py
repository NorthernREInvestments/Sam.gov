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


def fetch_text(url: str, *, timeout: float | None = None) -> tuple[int, str, str]:
    """Returns status, final_url, text."""
    settings = get_settings()
    safe = validate_public_https_url(url)
    t = timeout or settings.research_timeout_seconds
    with httpx.Client(
        timeout=t,
        follow_redirects=True,
        headers=DEFAULT_HEADERS,
    ) as client:
        # Re-validate redirect targets
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
