"""TDPUD water-materials package regression — exhaustive buyer history + public prices."""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urljoin

import httpx

from application_clock import now_utc
from evidence_breakthrough.acquisition_cost_resolver import resolve_public_acquisition_cost
from evidence_breakthrough.gov_value_resolver import resolve_government_value
from evidence_breakthrough.models import FOUND

log = logging.getLogger("govtracker.evidence_breakthrough.tdpud")

TDPUD_HOME = "https://www.tdpud.org/"
TDPUD_WATER_2027 = "https://www.tdpud.org/annual-water-materials-purchase-contract-2027"
TDPUD_BID_PDF = (
    "https://www.tdpud.org/files/f9a427b12/7.Material+Purchase+Contract+2027++Bid+Documents+.pdf"
)

IDENTITIES = [
    {
        "line_id": "TDPUD-Y502",
        "opportunity_id": "tdpud:water-materials-2027",
        "raw_description": 'METER YOKE, FORD Y502 - 3/4"',
        "manufacturer": "Ford Meter Box",
        "brand": "Ford",
        "model": "Y502",
        "part_number": "Y502",
        "quantity": 10.0,
        "uom": "EA",
        "uom_normalized": "EA",
        "confidence_grade": "A",
        "commercial_search_key": "Ford Meter Box Y502",
    },
    {
        "line_id": "TDPUD-SB317",
        "opportunity_id": "tdpud:water-materials-2027",
        "raw_description": 'SMITH-BLAIR 317-00069014-000 5.94-6.90 X 2" Tap Saddle',
        "manufacturer": "Smith-Blair",
        "brand": "Smith-Blair",
        "model": "317",
        "part_number": "317-00069014-000",
        "quantity": 1.0,
        "uom": "EA",
        "uom_normalized": "EA",
        "confidence_grade": "A",
        "commercial_search_key": "Smith-Blair 317-00069014-000",
    },
]

_AWARD_HINT = re.compile(
    r"(bid\s*tab|award|abstract\s*of\s*bids|notice\s*of\s*award|tabulation|"
    r"board\s*agenda|council\s*agenda|consent\s*agenda|resolution|"
    r"water\s*material|purchase\s*contract)",
    re.I,
)
_MONEY = re.compile(r"\$\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\d+\.\d{2})")


def _fetch(client: httpx.Client, url: str) -> dict[str, Any]:
    try:
        r = client.get(url, headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=True)
        return {
            "ok": r.status_code < 400,
            "status_code": r.status_code,
            "text": r.text or "",
            "url": str(r.url),
        }
    except Exception as exc:
        return {"ok": False, "status_code": None, "text": "", "url": url, "error": type(exc).__name__}


def _search_tdpud_site(client: httpx.Client) -> dict[str, Any]:
    """Enumerate TDPUD procurement / board pages for prior water-material awards."""
    routes_attempted = [
        "tdpud_current_solicitation_page",
        "tdpud_home_links",
        "tdpud_bid_document_pdf",
        "site_search_water_materials",
        "board_agenda_hints",
    ]
    pages: list[dict[str, Any]] = []
    links_found: list[str] = []
    award_hints: list[dict[str, Any]] = []

    for url in [
        TDPUD_WATER_2027,
        TDPUD_HOME,
        "https://www.tdpud.org/district-information/bids-rfps",
        "https://www.tdpud.org/bids-rfps",
        "https://www.tdpud.org/about-us/board-of-directors",
        "https://www.tdpud.org/district-information/board-of-directors/agendas-minutes",
        "https://www.tdpud.org/search?q=water+materials",
        "https://www.tdpud.org/search?q=award+bid+tabulation",
    ]:
        fr = _fetch(client, url)
        pages.append({"url": url, "ok": fr.get("ok"), "status": fr.get("status_code")})
        if not fr.get("ok"):
            continue
        text = fr.get("text") or ""
        # collect same-site links
        for href in re.findall(r'href=["\']([^"\']+)["\']', text, re.I):
            full = urljoin(fr.get("url") or url, href)
            if "tdpud.org" not in full:
                continue
            if _AWARD_HINT.search(full) or _AWARD_HINT.search(href):
                if full not in links_found:
                    links_found.append(full)
        # money + product mentions on page
        if re.search(r"Y502|Smith-?Blair|317-00069014|water\s*material", text, re.I):
            moneys = [m.group(0) for m in _MONEY.finditer(text)][:10]
            award_hints.append(
                {
                    "url": fr.get("url"),
                    "product_mention": True,
                    "money_samples": moneys,
                    "has_award_language": bool(_AWARD_HINT.search(text)),
                }
            )

    # Follow top award-ish links (bounded)
    for link in links_found[:8]:
        fr = _fetch(client, link)
        pages.append({"url": link, "ok": fr.get("ok"), "status": fr.get("status_code"), "followed": True})
        if fr.get("ok") and _MONEY.search(fr.get("text") or ""):
            if re.search(r"Y502|Smith-?Blair|317-00069014|award|tabulation", fr.get("text") or "", re.I):
                award_hints.append(
                    {
                        "url": fr.get("url"),
                        "product_mention": bool(re.search(r"Y502|317-00069014|Smith-?Blair", fr.get("text") or "", re.I)),
                        "money_samples": [m.group(0) for m in _MONEY.finditer(fr.get("text") or "")][:8],
                        "has_award_language": True,
                    }
                )

    # PDF text for current package (estimate only — not historical paid)
    pdf_meta = {"url": TDPUD_BID_PDF, "ok": False}
    try:
        r = client.get(TDPUD_BID_PDF, headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=True)
        pdf_meta["ok"] = r.status_code < 400
        pdf_meta["status"] = r.status_code
        pdf_meta["bytes"] = len(r.content or b"")
        if r.status_code < 400 and r.content:
            try:
                from pdf_text import extract_pdf_text

                ptxt = extract_pdf_text(r.content) or ""
                pdf_meta["text_chars"] = len(ptxt)
                pdf_meta["has_y502"] = "Y502" in ptxt.upper()
                pdf_meta["has_smith"] = "SMITH" in ptxt.upper() and "BLAIR" in ptxt.upper()
                pdf_meta["stated_estimate"] = bool(re.search(r"estimated?\s*(value|cost|amount)|budget", ptxt, re.I))
            except Exception as exc:
                pdf_meta["text_error"] = type(exc).__name__
    except Exception as exc:
        pdf_meta["error"] = type(exc).__name__

    return {
        "routes_attempted": routes_attempted,
        "pages": pages,
        "award_candidate_links": links_found[:20],
        "award_hints": award_hints,
        "bid_pdf": pdf_meta,
        "history_found": bool(award_hints and any(h.get("money_samples") and h.get("product_mention") for h in award_hints)),
    }


def run_tdpud_regression() -> dict[str, Any]:
    """Exhaustive TDPUD regression — never invent missing evidence."""
    out: dict[str, Any] = {
        "kind": "TDPUDRegression",
        "buyer": "Truckee Donner Public Utility District",
        "package": "Annual Water Material Purchase Contract 2027",
        "updated_at": now_utc().isoformat(),
        "history_found": False,
        "public_prices_found": 0,
        "both_sides": 0,
        "profit_result": None,
        "remaining_blocker": None,
        "lines": [],
        "site_search": None,
    }
    client = httpx.Client(timeout=30.0, follow_redirects=True)
    try:
        site = _search_tdpud_site(client)
        out["site_search"] = {
            "routes_attempted": site.get("routes_attempted"),
            "pages_ok": sum(1 for p in site.get("pages") or [] if p.get("ok")),
            "pages_tried": len(site.get("pages") or []),
            "award_candidate_links": site.get("award_candidate_links"),
            "award_hints": site.get("award_hints"),
            "bid_pdf": site.get("bid_pdf"),
        }
        out["history_found"] = bool(site.get("history_found"))

        # Also run gov resolver (will correctly report NO_BUYER_HISTORY for non-OpenGov)
        for ident in IDENTITIES:
            gov = resolve_government_value(ident, opportunity_id=ident["opportunity_id"], client=client)
            # Overlay TDPUD site evidence if we found product+money on an award-ish page
            if not out["history_found"]:
                # Check award_hints for this product
                needle = "Y502" if "Y502" in ident["model"] else "317"
                for hint in site.get("award_hints") or []:
                    if hint.get("product_mention") and hint.get("money_samples") and needle.lower() in str(hint).lower():
                        # Ambiguous money on page ≠ line unit price — do not claim gov value
                        gov = {
                            **gov,
                            "status": "NOT_FOUND_AFTER_EXHAUSTIVE_SEARCH",
                            "failure_reason": "NO_MATCHING_HISTORY",
                            "match_type": "GOV_NO_USABLE_HISTORY",
                            "stop_reason": "SITE_HINTS_WITHOUT_LINE_PRICE",
                            "provenance": [{"route": "tdpud_site", "url": hint.get("url"), "note": "page hint only"}],
                            "routes_attempted": (gov.get("routes_attempted") or []) + ["tdpud_official_site", "board_packet_hints"],
                        }
                        break
            else:
                gov["routes_attempted"] = (gov.get("routes_attempted") or []) + list(site.get("routes_attempted") or [])

            cost = resolve_public_acquisition_cost(
                ident,
                opportunity_id=ident["opportunity_id"],
                client=client,
                max_pages=10,
                use_phase_l=True,
            )
            both = gov.get("status") == FOUND and cost.get("status") == FOUND
            if cost.get("status") == FOUND:
                out["public_prices_found"] += 1
            if both:
                out["both_sides"] += 1
            out["lines"].append(
                {
                    "identity": {
                        "line_id": ident["line_id"],
                        "manufacturer": ident["manufacturer"],
                        "model": ident["model"],
                        "part_number": ident["part_number"],
                        "description": ident["raw_description"],
                    },
                    "government_value": {
                        "status": gov.get("status"),
                        "match_type": gov.get("match_type"),
                        "failure_reason": gov.get("failure_reason"),
                        "stop_reason": gov.get("stop_reason"),
                        "evidence": gov.get("evidence"),
                        "routes_attempted": gov.get("routes_attempted"),
                    },
                    "public_cost": {
                        "status": cost.get("status"),
                        "match_type": cost.get("match_type"),
                        "failure_reason": cost.get("failure_reason"),
                        "stop_reason": cost.get("stop_reason"),
                        "evidence": cost.get("evidence"),
                        "queries_attempted": cost.get("queries_attempted"),
                        "sources_attempted": cost.get("sources_attempted"),
                    },
                    "both_sides": both,
                }
            )
    finally:
        client.close()

    if out["both_sides"] > 0:
        out["profit_result"] = "HANDOFF_ELIGIBLE"
        out["remaining_blocker"] = None
    elif out["public_prices_found"] > 0 and not out["history_found"]:
        out["profit_result"] = None
        out["remaining_blocker"] = "NO_GOV_VALUE"
    elif out["history_found"] and out["public_prices_found"] == 0:
        out["profit_result"] = None
        out["remaining_blocker"] = "NO_PUBLIC_PRICE"
    else:
        out["profit_result"] = None
        out["remaining_blocker"] = "BOTH_SIDES_MISSING"

    # Honest: history_found only if we extracted a defensible line price (we didn't invent)
    # Site hints alone do not flip history_found to paid evidence
    if out["history_found"] and not any(
        (ln.get("government_value") or {}).get("status") == FOUND for ln in out["lines"]
    ):
        out["history_found"] = False
    if out["both_sides"] == 0:
        if not out["history_found"] and out["public_prices_found"] == 0:
            out["remaining_blocker"] = "BOTH_SIDES_MISSING"
        elif not out["history_found"]:
            out["remaining_blocker"] = "NO_GOV_VALUE"
        elif out["public_prices_found"] == 0:
            out["remaining_blocker"] = "NO_PUBLIC_PRICE"

    return out
