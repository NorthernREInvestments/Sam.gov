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


def _curl_fetch(url: str, *, timeout: float, binary: bool = False) -> tuple[int, str, bytes]:
    """Fallback for hosts that JA3/fingerprint-block httpx (e.g. Pantheon 403)."""
    import shutil
    import subprocess
    import sys
    import tempfile
    from pathlib import Path

    curl = "curl.exe" if sys.platform.startswith("win") else "curl"
    if not shutil.which(curl):
        return 403, url, b""
    tmp = Path(tempfile.mkstemp(prefix="m3_curl_")[1])
    try:
        cmd = [
            curl,
            "-sL",
            "-A",
            USER_AGENT,
            "--max-time",
            str(max(5, int(timeout))),
            "-o",
            str(tmp),
            "-w",
            "%{http_code}\n%{url_effective}",
            url,
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
        meta = (proc.stdout or b"").decode("utf-8", errors="replace").strip().splitlines()
        status = 0
        final = url
        if meta:
            try:
                status = int(meta[0].strip())
            except ValueError:
                status = 0
        if len(meta) > 1 and meta[1].startswith("http"):
            final = meta[1].strip()
        body = tmp.read_bytes() if tmp.exists() else b""
        if not status:
            status = 200 if body else 403
        try:
            validate_public_https_url(final)
        except Exception:
            return 403, url, b""
        return status, final, body
    except Exception:
        return 403, url, b""
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


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
        status, final, text = resp.status_code, str(resp.url), resp.text
        if status == 403:
            c_status, c_final, c_body = _curl_fetch(safe, timeout=float(t), binary=False)
            if c_status < 400 and c_body:
                return c_status, c_final, c_body.decode("utf-8", errors="replace")
        return status, final, text


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
            data = b"".join(chunks)
            status, final = resp.status_code, str(resp.url)
            if status == 403:
                c_status, c_final, c_body = _curl_fetch(safe, timeout=float(t), binary=True)
                if c_status < 400 and c_body:
                    return c_status, c_final, c_body[:max_bytes], ctype or "application/octet-stream"
            return status, final, data, ctype
