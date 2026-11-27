"""On-demand historical research source adapters (free).

Not a warehouse — targeted searches only when an active opportunity needs them.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

from m3.research.historical_match import (
    MATCH_MARKET,
    MATCH_NONE,
    PRICING_CONTRACT_TOTAL,
    PRICING_CATALOG_CONTRACT,
    PRICING_PO,
    RELEVANCE_CONTEXT,
    normalize_award_record,
)

UA = "M3SupplyHistoricalResearch/1.0 (+on-demand; not bulk archive)"


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "text/html,application/json"})
    return s


# --- Texas SmartBuy ESBD awards (no solicitation) ------------------------------------

TX_ESBD_AWARDS_URL = "https://www.txsmartbuy.gov/esbdawards"


def search_texas_esbd_awards(
    keyword: str, *, limit: int = 10, timeout: float = 40.0
) -> dict[str, Any]:
    """Public ESBD awards-without-solicitation search.

    Returns PO-level award amounts (not line-item unit prices). Use as leads /
    market context unless supporting line docs are retrieved separately.
    """
    kw = (keyword or "").strip()
    if not kw:
        return {"ok": False, "items": [], "errors": ["empty_keyword"], "source": "texas_smartbuy_esbd_awards"}
    s = _session()
    try:
        r = s.get(TX_ESBD_AWARDS_URL, params={"keyword": kw}, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "items": [],
            "errors": [f"{type(exc).__name__}:{exc}"],
            "source": "texas_smartbuy_esbd_awards",
        }
    if r.status_code >= 400:
        return {
            "ok": False,
            "items": [],
            "errors": [f"status={r.status_code}"],
            "source": "texas_smartbuy_esbd_awards",
        }
    plain = BeautifulSoup(r.text, "html.parser").get_text("\n", strip=True)
    parts = re.split(r"(?=PO Number:)", plain)
    items: list[dict[str, Any]] = []
    for part in parts[1:]:
        if limit is not None and len(items) >= int(limit):
            break

        def grab(pat: str) -> str | None:
            m = re.search(pat, part, flags=re.I)
            return m.group(1).strip() if m else None

        po = grab(r"PO Number:\s*(\S+)")
        if not po:
            continue
        amount = grab(r"PO Amount:\s*([\d,\.]+)")
        vendor = grab(r"Vendor Name:\s*(.+)")
        award_date = grab(r"Award Date:\s*(\S+)")
        agency = grab(r"Agency/Texas SmartBuy Member Number:\s*(.+)")
        raw = {
            "buyer": agency or "Texas SmartBuy / ESBD award",
            "product": f"TX PO {po} (keyword={kw})",
            "part_number": "",
            "match_class": MATCH_MARKET,
            "match_reason": (
                f"Texas ESBD award PO matched keyword '{kw}'. PO amount is a purchase-order "
                "total, not a verified product unit price."
            ),
            "pricing_type": PRICING_PO if amount else PRICING_CONTRACT_TOTAL,
            "award_date": award_date,
            "awardee": vendor,
            "quantity": "",
            "unit_price": None,
            "total_award_value": amount,
            "source_url": f"{TX_ESBD_AWARDS_URL}?{urlencode({'keyword': kw})}",
            "po_number": po,
            "notes": "PO-level evidence; retrieve line items before treating as unit pricing.",
            "relevance": RELEVANCE_CONTEXT,
        }
        # If only one product is implied by keyword research later, OpenAI can upgrade match.
        items.append(normalize_award_record(raw))
    return {
        "ok": True,
        "items": items,
        "errors": [],
        "source": "texas_smartbuy_esbd_awards",
        "endpoint": TX_ESBD_AWARDS_URL,
        "verified_at": _utc(),
        "pricing_note": "PO Amount is order total, not unit price.",
        "rows_returned": len(items),
    }


# --- USAC E-Rate FRN line items -------------------------------------------------------

USAC_FRN_LINE_ITEMS = "https://opendata.usac.org/resource/hbj5-2bpj.json"


def search_usac_erate_line_items(
    *,
    manufacturer: str | None = None,
    model: str | None = None,
    product: str | None = None,
    limit: int = 10,
    timeout: float = 40.0,
) -> dict[str, Any]:
    """SODA query against E-Rate Form 471 line items.

    Costs are pre-discount funding request amounts — NOT executed awarded unit prices.
    Useful for networking gear comps when labeled correctly.
    """
    clauses: list[str] = []
    if manufacturer:
        m = manufacturer.replace("'", "''").upper()
        clauses.append(f"upper(form_471_manufacturer_name) like '%{m}%'")
    if model:
        m = model.replace("'", "''").upper()
        clauses.append(f"upper(model_of_equipment) like '%{m}%'")
    if product and not manufacturer and not model:
        # Free-text fallback
        params: dict[str, Any] = {"$limit": max(1, min(int(limit), 25)), "$q": product}
    else:
        params = {"$limit": max(1, min(int(limit), 25))}
        if clauses:
            params["$where"] = " AND ".join(clauses)
        elif product:
            params["$q"] = product
        else:
            return {
                "ok": False,
                "items": [],
                "errors": ["need_manufacturer_model_or_product"],
                "source": "usac_erate_frn_line_items",
            }

    s = _session()
    try:
        r = s.get(USAC_FRN_LINE_ITEMS, params=params, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "items": [],
            "errors": [f"{type(exc).__name__}:{exc}"],
            "source": "usac_erate_frn_line_items",
        }
    if r.status_code >= 400:
        return {
            "ok": False,
            "items": [],
            "errors": [f"status={r.status_code}:{r.text[:200]}"],
            "source": "usac_erate_frn_line_items",
        }
    rows = r.json() if r.text else []
    items: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        mfr = row.get("form_471_manufacturer_name") or ""
        mdl = row.get("model_of_equipment") or ""
        pname = row.get("form_471_product_name") or ""
        one_time = row.get("one_time_eligible_costs") or row.get("total_one_time_cost")
        qty = row.get("one_time_quantity") or row.get("monthly_quantity")
        # Derive implied unit only when qty and one-time cost are present and numeric
        unit = None
        try:
            qf = float(str(qty).replace(",", "")) if qty not in (None, "", "0") else None
            cf = float(str(one_time).replace(",", "")) if one_time not in (None, "") else None
            if qf and cf is not None and qf > 0:
                unit = round(cf, 4)  # field is already unit-ish eligible cost in samples
                # In sample: one_time_eligible_costs=560.62 with qty=18 → that 560.62 is UNIT
        except Exception:
            unit = None
        match_class = MATCH_MARKET
        match_reason = (
            f"USAC E-Rate Form 471 line: {mfr} {mdl} ({pname}). "
            "This is requested/funding-line cost evidence, not a verified government awarded unit price."
        )
        if model and mdl and model.upper().replace(" ", "") in str(mdl).upper().replace(" ", ""):
            match_class = "EXACT_VERIFIED_PRODUCT_IDENTIFIER_MATCH"
            match_reason = (
                f"Model string match on E-Rate line item ({mdl}). Still funding-request pricing, "
                "not an executed award tabulation."
            )
        raw = {
            "buyer": row.get("organization_name") or "E-Rate applicant",
            "product": f"{mfr} {mdl} {pname}".strip(),
            "part_number": mdl,
            "match_class": match_class,
            "match_reason": match_reason,
            "pricing_type": "government_estimate" if unit is not None else PRICING_CATALOG_CONTRACT,
            "award_date": row.get("funding_year"),
            "awardee": row.get("spin_name") or "",
            "quantity": qty,
            "unit_of_measure": row.get("form_471_unit_name"),
            "unit_price": unit,
            "total_award_value": row.get("pre_discount_extended_eligible_line_item_costs")
            or row.get("total_eligible_one_time_costs"),
            "source_url": f"https://opendata.usac.org/d/hbj5-2bpj",
            "notes": (
                "E-Rate approved/requested costs ≠ paid award price. "
                f"state={row.get('state')} frn={row.get('funding_request_number')}"
            ),
            "relevance": RELEVANCE_CONTEXT,
        }
        item = normalize_award_record(raw, current_mpn=model)
        # Keep funding caveat even when deterministic model/MPN match upgrades the class.
        if "funding" not in (item.get("match_reason") or "").lower() and "e-rate" not in (
            item.get("match_reason") or ""
        ).lower():
            item["match_reason"] = (
                f"{item.get('match_reason') or ''} "
                "E-Rate Form 471 funding-line cost — not an executed awarded unit price."
            ).strip()
        item["relevance"] = RELEVANCE_CONTEXT
        items.append(item)
    return {
        "ok": True,
        "items": items,
        "errors": [],
        "source": "usac_erate_frn_line_items",
        "endpoint": USAC_FRN_LINE_ITEMS,
        "verified_at": _utc(),
        "pricing_note": "Form 471 line costs are funding requests, not awarded unit prices.",
        "rows_returned": len(items),
    }


# --- Louisiana LaPAC bid number → public PDFs ----------------------------------------

LA_BIDNO_URL = "https://wwwcfprd.doa.louisiana.gov/osp/lapac/dspBid.cfm"


def search_louisiana_lapac_by_bidno(
    bidno: str, *, timeout: float = 40.0
) -> dict[str, Any]:
    """Retrieve public LaPAC package docs for a known bid number (open or historical id)."""
    sol = (bidno or "").strip()
    if not sol:
        return {"ok": False, "items": [], "errors": ["empty_bidno"], "source": "louisiana_lapac_bidno"}
    s = _session()
    try:
        r = s.get(LA_BIDNO_URL, params={"search": "bidno", "term": sol}, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "items": [],
            "errors": [f"{type(exc).__name__}:{exc}"],
            "source": "louisiana_lapac_bidno",
        }
    if r.status_code >= 400:
        return {
            "ok": False,
            "items": [],
            "errors": [f"status={r.status_code}"],
            "source": "louisiana_lapac_bidno",
        }
    soup = BeautifulSoup(r.text, "html.parser")
    docs: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/agency/pdf/" in href.lower() or href.lower().endswith((".pdf", ".docx", ".doc")):
            from urllib.parse import urljoin

            docs.append(urljoin(r.url, href))
    docs = list(dict.fromkeys(docs))
    if not docs:
        return {
            "ok": True,
            "items": [],
            "errors": [],
            "source": "louisiana_lapac_bidno",
            "note": "No public PDFs for bid number",
            "rows_returned": 0,
        }
    raw = {
        "buyer": "Louisiana LaPAC",
        "product": f"LaPAC bid {sol}",
        "part_number": "",
        "match_class": MATCH_NONE,
        "match_reason": (
            "Retrieved solicitation/award package by bid number only; product match not established "
            "until documents are read."
        ),
        "pricing_type": "unknown_pricing_type",
        "award_date": "",
        "awardee": "",
        "quantity": "",
        "unit_price": None,
        "total_award_value": "",
        "source_url": f"{LA_BIDNO_URL}?search=bidno&term={sol}",
        "document_urls": docs,
        "notes": f"{len(docs)} public document URL(s); OpenAI should read for pricing/match.",
    }
    item = normalize_award_record(raw)
    item["document_urls"] = docs
    return {
        "ok": True,
        "items": [item],
        "errors": [],
        "source": "louisiana_lapac_bidno",
        "endpoint": LA_BIDNO_URL,
        "verified_at": _utc(),
        "rows_returned": 1,
        "document_urls": docs,
    }


# --- Roadmap stubs (methods known, automation incomplete) ----------------------------

BSO_STATUS_CLOSED = "2BCL"
BSO_STATUS_INTENT = "2BIA"
BSO_STATUS_BID_TO_PO = "2BPO"

BSO_PORTALS = {
    "oregonbuys": {
        "advanced_search": "https://oregonbuys.gov/bso/view/search/external/advancedSearchBid.xhtml",
        "doc_types": ["BID_SOLICITATIONS", "CONTRACT_BLANKETS"],
    },
    "illinois_bidbuy": {
        "advanced_search": "https://www.bidbuy.illinois.gov/bso/view/search/external/advancedSearchBid.xhtml",
        "doc_types": ["BID_SOLICITATIONS", "CONTRACT_BLANKETS", "VENDORS", "PURCHASE_ORDER"],
    },
    "commbuys": {
        "advanced_search": "https://www.commbuys.com/bso/view/search/external/advancedSearchBid.xhtml",
        "doc_types": ["BID_SOLICITATIONS", "CONTRACT_BLANKETS", "VENDORS"],
        "type_historical": "SH",
    },
}


def bso_automation_status() -> dict[str, Any]:
    """Document current BSO automation state for agents (no live scrape)."""
    return {
        "status": "partial",
        "proven": [
            "Public advancedSearchBid.xhtml loads without login",
            "Status option values shared: Closed=2BCL, Intent To Award=2BIA, Bid to PO=2BPO",
            "COMMBUYS type Historical Solicitation=SH",
            "Illinois document types include PURCHASE_ORDER and CONTRACT_BLANKETS",
        ],
        "blocked": [
            "PrimeFaces ajax search posts return the form panel; result table extraction not yet reliable",
            "Must use option values (2BCL) not labels (Closed) or validation fails",
            "Document-type switch requires Faces ajax change event before PO field set is trustworthy",
        ],
        "portals": BSO_PORTALS,
        "next_step": (
            "Capture one successful browser Network trace for Closed/PO search and mirror "
            "exact partial-render IDs; then share one adapter across OR/IL/MA."
        ),
    }


def missouri_awarded_status() -> dict[str, Any]:
    return {
        "status": "reachable_ui_automation_blocked",
        "url": "https://awardedsearch.oa.mo.gov/PublicAccess/index.html",
        "stack": "Angular web components (obpa-search-panel) behind Incapsula",
        "blocked": [
            "API endpoints not exposed as readable strings in bundled JS",
            "Incapsula bot management on PublicAccess",
        ],
        "next_step": "Owner-attended browser capture of search XHR, or official API if published",
    }


def pennsylvania_historical_status() -> dict[str, Any]:
    return {
        "status": "open_solicitations_only_in_current_adapter",
        "url": "https://www.emarketplace.state.pa.us/Search.aspx",
        "ad_types": {
            "1": "Service",
            "10": "Materials",
            "9": "Service & Materials",
            "3": "Agency Construction",
            "12": "PW Construction",
            "4": "Real Estate",
            "11": "All",
        },
        "note": (
            "Public search ad types are solicitation categories, not Awarded/Closed filters. "
            "Live Materials CSV path remains for active IFBs; historical award automation TBD."
        ),
    }
