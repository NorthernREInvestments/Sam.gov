"""Sourcewell open-solicitations HTML parser."""

from __future__ import annotations

import html as html_lib
import re
from urllib.parse import urljoin

from discovery.deadline import normalize_deadline
from discovery.opportunity_gate import is_structurally_valid_opportunity, sanitize_deadline_raw
from discovery.schema import CanonicalOpportunity


def parse_sourcewell_open_solicitations(
    body: str,
    *,
    list_url: str,
    source_id: str = "coop_sourcewell_live",
    trust_tier: int = 2,
) -> list[CanonicalOpportunity]:
    """
    Parse Sourcewell /solicitations HTML OpenSols section only.
    Excludes Pending and Recently awarded sections.
    """
    text = body or ""
    m = re.search(r'<section[^>]*id=["\']OpenSols["\'][^>]*>(.*?)</section>', text, re.I | re.S)
    if m:
        chunk = m.group(1)
    else:
        m2 = re.search(
            r'id=["\']OpenSols["\'](.*?)(?:id=["\']PendingSols["\']|id=["\']RecAwardSols["\']|Recently awarded)',
            text,
            re.I | re.S,
        )
        chunk = m2.group(1) if m2 else ""
        if not chunk:
            return []

    # Explicitly drop awarded section if nested somehow
    chunk = re.split(r'id=["\']RecAwardSols["\']|Recently awarded', chunk, maxsplit=1, flags=re.I)[0]

    found: list[CanonicalOpportunity] = []
    for lm in re.finditer(
        r'href=["\']((?:https://www\.sourcewell-mn\.gov)?/solicitations/(\d+)|https://www\.sourcewell-mn\.gov/solicitations/(\d+))["\'][^>]*>(.*?)</a>',
        chunk,
        re.I | re.S,
    ):
        href = lm.group(1)
        sid = lm.group(2) or lm.group(3)
        title = _clean(lm.group(4))
        if len(title) < 8:
            continue
        detail = href if href.startswith("http") else urljoin("https://www.sourcewell-mn.gov", href)
        window = chunk[lm.end() : lm.end() + 500]
        due = None
        dm = re.search(
            r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
            r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
            r"Dec(?:ember)?)\s+(\d{1,2}),?\s+(\d{4})",
            window,
            re.I,
        )
        if dm:
            due = sanitize_deadline_raw(dm.group(0))
        external_id = f"sourcewell:{sid}"
        gate = is_structurally_valid_opportunity(
            {
                "title": title,
                "solicitation_number": sid,
                "external_id": external_id,
                "deadline_raw": due,
                "detail_url": detail,
                "agency": "Sourcewell",
                "status": "OPEN",
            }
        )
        if not gate["valid"]:
            continue
        dl = normalize_deadline(due)
        found.append(
            CanonicalOpportunity(
                external_id=external_id,
                source_id=source_id,
                source_url=list_url,
                detail_url=detail,
                title=title,
                solicitation_number=sid,
                agency="Sourcewell",
                jurisdiction="COOPERATIVE",
                buyer_type="COOPERATIVE",
                status="OPEN",
                deadline_raw=gate["sanitized_deadline_raw"] or due,
                deadline_timezone=dl.get("timezone"),
                deadline_tz_confidence=dl.get("timezone_confidence"),
                trust_tier=trust_tier,
                raw_metadata={
                    "notice_type": "OPEN_SOLICITATION",
                    "platform": "Sourcewell",
                    "section": "OpenSols",
                    "structural_gate": gate,
                },
            )
        )

    seen: set[str] = set()
    out: list[CanonicalOpportunity] = []
    for o in found:
        if o.external_id in seen:
            continue
        seen.add(o.external_id)
        out.append(o)
    return out


def _clean(raw: str) -> str:
    t = html_lib.unescape(raw or "")
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", t).strip()[:300]


def sourcewell_has_open_structure(body: str) -> bool:
    return bool(
        re.search(r'id=["\']OpenSols["\']', body or "", re.I)
        or re.search(r"sourcewell-mn\.gov/solicitations/\d+", body or "", re.I)
    )
