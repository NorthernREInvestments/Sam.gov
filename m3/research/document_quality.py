"""Document content quality gates for solicitation packages (Build 002Q).

Generic portal / employee / login / navigation pages are never successful
solicitation document acquisitions.
"""

from __future__ import annotations

import re
from typing import Any

# Strong junk signals — any 2+ (or 1 critical) ⇒ reject as solicitation body
_CRITICAL_JUNK = re.compile(
    r"(?i)("
    r"employee\s+data\s+portal|"
    r"sign\s+in\s+to\s+(?:your\s+)?(?:account|portal)|"
    r"invalid\s+username\s+or\s+password|"
    r"session\s+has\s+expired|"
    r"access\s+denied|"
    r"403\s+forbidden|"
    r"404\s+not\s+found|"
    r"page\s+not\s+found|"
    r"enable\s+javascript\s+to\s+continue|"
    r"cloudflare\s+ray\s+id|"
    r"just\s+a\s+moment(?:\.\.\.|…)|"
    r"checking\s+your\s+browser\s+before\s+accessing"
    r")"
)

_SOFT_JUNK = re.compile(
    r"(?i)("
    r"skip\s+to\s+main\s+content|"
    r"cookie\s+consent|"
    r"accept\s+(?:all\s+)?cookies|"
    r"privacy\s+policy|"
    r"terms\s+of\s+use|"
    r"main\s+navigation|"
    r"site\s+map|"
    r"follow\s+us\s+on|"
    r"subscribe\s+to\s+(?:our\s+)?newsletter|"
    r"business\s+services\s*-\s*purchasing\s*-\s*solicitations|"
    r"city\s+of\s+mesa\s+employee|"
    r"schedule\s+tour|"
    r"vendor\s+registration|"
    r"official\s+website\s+of\s+the\s+state|"
    r"here's\s+how\s+you\s+know|"
    r"translate\s+this\s+page|"
    r"back\s+to\s+top"
    r")"
)

# Strong solicitation-body markers only — bare "quantity"/"specifications" appear on portals
_SOLICITATION_BODY = re.compile(
    r"(?i)("
    r"solicitation\s+(?:number|no\.?|#)|"
    r"invitation\s+to\s+bid|"
    r"request\s+for\s+(?:proposal|quote|bid|tender)|"
    r"notice\s+of\s+bid\s+solicitation|"
    r"bid\s+opening\s+date|"
    r"submit\s+(?:bids?|proposals?|quotes?)\s+(?:by|to|via|before)|"
    r"scope\s+of\s+(?:work|services|supply)|"
    r"line\s+item\s+(?:no|number|#)|"
    r"unit\s+price|"
    r"ordered\s+quantity|"
    r"delivery\s+(?:location|date|schedule)|"
    r"amendment\s+(?:no|number|#)\s*\.?\s*\d|"
    r"technical\s+specifications|"
    r"attachment\s+[a-z0-9]"
    r")"
)


def title_body_overlap(title: str, body: str) -> dict[str, Any]:
    """Measure whether stored body likely matches the solicitation title.

    Low overlap often means portal search-page pollution or wrong document.
    """
    stop = {
        "the",
        "and",
        "for",
        "with",
        "from",
        "that",
        "this",
        "into",
        "onto",
        "a",
        "an",
        "of",
        "to",
        "in",
        "on",
        "or",
        "by",
        "at",
        "as",
        "is",
        "are",
        "be",
        "new",
        "one",
        "two",
        "bid",
        "rfp",
        "rfq",
        "ifb",
        "rfb",
    }
    tokens = [
        w
        for w in re.findall(r"[a-z0-9]{3,}", (title or "").lower())
        if w not in stop and not w.isdigit()
    ]
    # Prefer distinctive tokens (skip ultra-generic)
    distinctive = [w for w in tokens if len(w) >= 4][:12]
    if not distinctive:
        return {"overlap_ratio": 1.0, "matched": 0, "total": 0, "ok": True, "reason": "no_title_tokens"}
    body_l = (body or "").lower()
    matched = sum(1 for w in distinctive if w in body_l)
    ratio = matched / max(1, len(distinctive))
    ok = ratio >= 0.25 or matched >= 2
    return {
        "overlap_ratio": round(ratio, 3),
        "matched": matched,
        "total": len(distinctive),
        "ok": ok,
        "reason": "ok" if ok else "title_body_mismatch",
    }


def classify_document_text(text: str, *, fmt: str | None = None) -> dict[str, Any]:
    """Return quality verdict for extracted document text."""
    t = (text or "").strip()
    if not t:
        return {
            "ok": False,
            "reason": "empty_text",
            "is_junk": True,
            "is_solicitation_body": False,
        }
    if len(t) < 80:
        return {
            "ok": False,
            "reason": "thin_text",
            "is_junk": True,
            "is_solicitation_body": False,
        }

    critical = bool(_CRITICAL_JUNK.search(t))
    soft_hits = len(_SOFT_JUNK.findall(t))
    body_hits = len(_SOLICITATION_BODY.findall(t))

    if critical and body_hits < 2:
        return {
            "ok": False,
            "reason": "critical_portal_or_error_page",
            "is_junk": True,
            "is_solicitation_body": False,
            "soft_hits": soft_hits,
            "body_hits": body_hits,
        }
    if soft_hits >= 2 and body_hits < 2:
        return {
            "ok": False,
            "reason": "generic_portal_chrome",
            "is_junk": True,
            "is_solicitation_body": False,
            "soft_hits": soft_hits,
            "body_hits": body_hits,
        }
    if body_hits < 1 and soft_hits >= 1:
        return {
            "ok": False,
            "reason": "navigation_without_solicitation_body",
            "is_junk": True,
            "is_solicitation_body": False,
            "soft_hits": soft_hits,
            "body_hits": body_hits,
        }
    if body_hits < 1 and len(t) >= 400:
        # Long chrome / listing pages with no solicitation markers
        navish = len(
            re.findall(
                r"(?i)\b(home|menu|login|sign\s+in|contact\s+us|departments?|programs?)\b",
                t,
            )
        )
        if navish >= 4:
            return {
                "ok": False,
                "reason": "nav_heavy_without_solicitation_body",
                "is_junk": True,
                "is_solicitation_body": False,
                "soft_hits": soft_hits,
                "body_hits": body_hits,
                "navish": navish,
            }

    return {
        "ok": True,
        "reason": None,
        "is_junk": False,
        "is_solicitation_body": body_hits >= 1,
        "soft_hits": soft_hits,
        "body_hits": body_hits,
    }


def classify_raw_bytes(data: bytes, *, ctype: str = "", fmt: str = "") -> dict[str, Any]:
    """Quick pre-extract check for HTML portal/login pages."""
    if not data:
        return {"ok": False, "reason": "empty_file", "is_junk": True}
    if data.startswith(b"%PDF") or data[:2] == b"PK":
        return {"ok": True, "reason": None, "is_junk": False, "binary_ok": True}
    head = data[:12000].decode("utf-8", errors="ignore")
    low = head.lower()
    if "html" in (ctype or "").lower() or low.lstrip().startswith(("<!doctype", "<html")):
        return classify_document_text(head, fmt="html")
    return {"ok": True, "reason": None, "is_junk": False}


def is_junk_package_excerpt(text: str) -> bool:
    return bool(classify_document_text(text).get("is_junk"))
