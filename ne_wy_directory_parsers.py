"""Parse Nebraska NDE and Wyoming WDE directories into entity records."""

from __future__ import annotations

import re
from typing import Any


def parse_nde_quickdisplay_markdown(text: str) -> list[dict[str, Any]]:
    """Parse NDE QuickDisplay markdown/HTML table of public districts (~244)."""
    if not text:
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    # Flexible row: admin | agencyid | NAME | ... | email |
    # ADDRESS2 cells are often omitted in markdown exports, so do not assume fixed columns.
    row_re = re.compile(
        r"\|\s*[^|\n]+\|\s*(\d{2}-\d{4}-\d{3})\s*\|\s*([^|\n]+?)\s*\|.*?\|\s*([\w.+-]+@[\w.-]+)\s*\|",
        re.I,
    )
    for m in row_re.finditer(text):
        agency_id, name, email = m.group(1).strip(), m.group(2).strip(), m.group(3).strip().lower()
        key = name.upper()
        if key in seen or len(name) < 4:
            continue
        seen.add(key)
        # Optional ESU / county from trailing numeric ESU column when present
        esu = None
        county = None
        tail = re.search(
            rf"{re.escape(agency_id)}\s*\|\s*{re.escape(name)}\s*\|.*?\|\s*(\d{{1,2}})\s*\|\s*([A-Z][^|]{{2,40}})\s*\|\s*{re.escape(email)}",
            text,
            re.I,
        )
        if tail:
            esu = f"ESU {int(tail.group(1))}"
            county = tail.group(2).strip().title() if tail.group(2).isupper() else tail.group(2).strip()
        website = website_from_email(email)
        out.append(
            {
                "name": name.title() if name.isupper() else name,
                "agency_id": agency_id,
                "state": "NE",
                "entity_type": "K12_SCHOOL_DISTRICT",
                "esu": esu,
                "county": county,
                "email": email,
                "website_guess": website,
                "provenance": "nde_quickdisplay_pda",
            }
        )

    # HTML fallback
    if len(out) < 100:
        html_re = re.compile(
            r"(\d{2}-\d{4}-\d{3})</t[dh]>\s*<t[dh][^>]*>\s*([^<]{4,90})\s*</t[dh]>",
            re.I,
        )
        for m in html_re.finditer(text):
            agency_id, name = m.group(1), re.sub(r"\s+", " ", m.group(2)).strip()
            key = name.upper()
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "name": name.title() if name.isupper() else name,
                    "agency_id": agency_id,
                    "state": "NE",
                    "entity_type": "K12_SCHOOL_DISTRICT",
                    "provenance": "nde_quickdisplay_html",
                }
            )
    return out


def parse_wde_directory_text(text: str) -> list[dict[str, Any]]:
    """Parse WDE education directory text for public school districts + websites."""
    if not text:
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    # ### Albany County School District #1 http://www.acsd1.org (0101000)
    pat = re.compile(
        r"(?:^|\n)(?:###\s*)?([A-Z][A-Za-z .'#\-]+School District\s*#?\s*\d+)\s+"
        r"(https?://[^\s\)]+)\s*\((\d{7})\)",
        re.M,
    )
    for m in pat.finditer(text):
        name, url, code = m.group(1).strip(), m.group(2).strip().rstrip("/"), m.group(3)
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        if not url.startswith("http"):
            url = "https://" + url
        out.append(
            {
                "name": name,
                "agency_id": code,
                "state": "WY",
                "entity_type": "K12_SCHOOL_DISTRICT",
                "website": url,
                "website_guess": url,
                "provenance": "wde_education_directory",
            }
        )
    # Fallback simpler pattern without ###
    if len(out) < 40:
        pat2 = re.compile(
            r"([A-Za-z .]+County School District\s*#?\s*\d+)\s+(https?://[^\s]+)\s*\((\d{7})\)",
            re.I,
        )
        for m in pat2.finditer(text):
            name, url, code = m.group(1).strip(), m.group(2).strip().rstrip("/."), m.group(3)
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "name": name,
                    "agency_id": code,
                    "state": "WY",
                    "entity_type": "K12_SCHOOL_DISTRICT",
                    "website": url if url.startswith("http") else "https://" + url,
                    "website_guess": url if url.startswith("http") else "https://" + url,
                    "provenance": "wde_education_directory",
                }
            )
    return out


def website_from_email(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    domain = email.split("@", 1)[-1].lower().strip()
    if not domain or domain.endswith("esu2.org") or re.match(r"esu\d+\.org$", domain):
        return None
    if domain in {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com"}:
        return None
    return f"https://www.{domain}"


PROCUREMENT_PATH_CANDIDATES = [
    "/purchasing",
    "/procurement",
    "/business",
    "/business-office",
    "/departments/business",
    "/departments/finance",
    "/about/business",
    "/bids",
    "/rfps",
    "/vendors",
]
