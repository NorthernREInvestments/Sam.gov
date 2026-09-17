"""Lightweight platform-family detection from URL/page metadata."""

from __future__ import annotations

import re
from typing import Any

from discovery.constants import (
    PLATFORM_BIDNET,
    PLATFORM_BONFIRE,
    PLATFORM_DEMANDSTAR,
    PLATFORM_IONWAVE,
    PLATFORM_JAGGAER,
    PLATFORM_JSON,
    PLATFORM_OPENGOV,
    PLATFORM_PERISCOPE,
    PLATFORM_PLANETBIDS,
    PLATFORM_PUBLIC_PURCHASE,
    PLATFORM_RSS,
    PLATFORM_SIMPLE_HTML,
    PLATFORM_UNKNOWN,
)

_PATTERNS: list[tuple[str, str]] = [
    (r"bonfirehub\.com|gobonfire", PLATFORM_BONFIRE),
    (r"opengov\.com|procurement\.opengov", PLATFORM_OPENGOV),
    (r"ionwave\.net", PLATFORM_IONWAVE),
    (r"planetbids\.com", PLATFORM_PLANETBIDS),
    (r"bidnet(direct)?\.com", PLATFORM_BIDNET),
    (r"demandstar\.com", PLATFORM_DEMANDSTAR),
    (r"jaggaer\.com|sciquest", PLATFORM_JAGGAER),
    (r"periscopeholdings|bidsync", PLATFORM_PERISCOPE),
    (r"publicpurchase\.com", PLATFORM_PUBLIC_PURCHASE),
]


def detect_platform(
    url: str | None = None,
    *,
    html_snippet: str | None = None,
    content_type: str | None = None,
) -> dict[str, Any]:
    """Classify likely platform. Does not bypass auth. No network."""
    blob = f"{url or ''} {html_snippet or ''}".lower()
    ct = (content_type or "").lower()

    if "json" in ct or blob.strip().startswith("{") or blob.strip().startswith("["):
        return {
            "platform": PLATFORM_JSON,
            "detection": "KNOWN_PLATFORM" if "json" in ct else "JSON",
            "confidence": "MEDIUM",
            "fingerprint_confidence": "POSSIBLE",
            "LIVE_API_REQUESTS": 0,
        }
    if "xml" in ct or "rss" in ct or "<rss" in blob or "<feed" in blob:
        return {
            "platform": PLATFORM_RSS,
            "detection": "RSS",
            "confidence": "HIGH",
            "fingerprint_confidence": "HIGH_CONFIDENCE",
            "LIVE_API_REQUESTS": 0,
        }

    for pattern, family in _PATTERNS:
        if re.search(pattern, blob, re.I):
            # URL host match → HIGH; HTML-only marker without URL → POSSIBLE
            url_hit = bool(url and re.search(pattern, url, re.I))
            return {
                "platform": family,
                "detection": "KNOWN_PLATFORM",
                "confidence": "HIGH" if url_hit else "MEDIUM",
                "fingerprint_confidence": "HIGH_CONFIDENCE" if url_hit else "POSSIBLE",
                "LIVE_API_REQUESTS": 0,
            }

    if "<table" in blob and ("bid" in blob or "solicitation" in blob or "rfp" in blob):
        return {
            "platform": PLATFORM_SIMPLE_HTML,
            "detection": "SIMPLE_HTML",
            "confidence": "MEDIUM",
            "fingerprint_confidence": "POSSIBLE",
            "LIVE_API_REQUESTS": 0,
        }

    return {
        "platform": PLATFORM_UNKNOWN,
        "detection": "UNKNOWN",
        "confidence": "LOW",
        "fingerprint_confidence": "UNKNOWN",
        "LIVE_API_REQUESTS": 0,
        "note": "Do not assume undocumented API",
    }
