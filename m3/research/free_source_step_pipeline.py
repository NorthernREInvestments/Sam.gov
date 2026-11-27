"""Stepped free-source pipeline — cheap filters first, spend effort only on fits.

Owner rule:
- OpenAI reads EVERYTHING that reaches a given step.
- Save what fits M3 (agency BUYING tangible goods). Trash the rest.
- Do not download docs or deep-read contracts that already failed an earlier step.

Steps (Nebraska SPB and other free portals):
  1) LIST          — public listing only
  2) REGEX_SCREEN  — drop obvious service / non-competitive titles (no AI, no docs)
  3) BODY_TRIAGE   — fetch detail HTML → OpenAI full-body checklist (agency buying? product?)
  4) DOCS          — only if PRODUCT + FETCH_DOCUMENTS → download every project document
  5) DOC_READ      — OpenAI every-word / facts from downloaded docs
  6) KEEP_OR_TRASH — persist keepers; delete/trash non-fits
"""
from __future__ import annotations

import io
import json
import re
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from m3.config import get_settings
from m3.research.adapters import (
    search_delaware_mmp,
    search_nebraska_spb,
    search_nj_purchase,
    search_tn_cpo,
    search_uw_procurement,
)
from m3.research.documents import extract_docx_text, extract_pdf_text
from m3.research.free_additional_adapters import (
    discover_danville_va_documents,
    discover_indiana_idoa_documents,
    discover_louisiana_lapac_documents,
    discover_msu_procurement_documents,
    discover_oceanview_ovsd_documents,
    discover_pa_emarketplace_documents,
    discover_papillion_plcs_documents,
    discover_pocatello_sd25_documents,
    discover_providence_ri_documents,
    discover_sanjuan_usd_documents,
    search_danville_va,
    search_indiana_idoa,
    search_louisiana_lapac,
    search_msu_procurement,
    search_oceanview_ovsd,
    search_pa_emarketplace,
    search_papillion_plcs,
    search_pocatello_sd25,
    search_providence_ri,
    search_sanjuan_usd,
)
from m3.research.free_municipal_adapters import (
    discover_alaska_opn_documents,
    discover_cleveland_oh_documents,
    discover_columbus_ga_documents,
    discover_erie_ny_documents,
    discover_jefferson_tx_documents,
    discover_kent_state_documents,
    discover_tntech_documents,
    discover_tulsa_ok_documents,
    discover_worcester_ma_documents,
    search_alaska_opn,
    search_cleveland_oh,
    search_columbus_ga,
    search_erie_ny,
    search_jefferson_tx,
    search_kent_state,
    search_tntech,
    search_tulsa_ok,
    search_worcester_ma,
)
from m3.research.http_util import fetch_bytes, fetch_text
from m3.research.nextbid_owner_filters import listing_skip_before_detail_reason
from m3.research.opportunity_checklist_triage import (
    authorize_document_fetch,
    body_agency_not_buying_reason,
    body_install_labor_reason,
    body_perishable_food_reason,
    body_service_contract_reason,
    body_surplus_sale_reason,
    perishable_food_reason,
    title_signal_decision,
    triage_opportunities_parallel,
)
from m3.research.package import (
    blob_path_for_hash,
    discover_delaware_mmp_documents,
    discover_nebraska_spb_documents,
    discover_nj_purchase_documents,
    discover_tn_cpo_documents,
    discover_uw_procurement_documents,
)
from m3.research.parallel_work import map_parallel

# Free portals with full stepped vet adapters (Nebraska pattern).
FREE_SOURCE_SPECS: dict[str, dict[str, Any]] = {
    "nebraska_spb": {
        "list_url": "https://das.nebraska.gov/materiel/bid-opportunities.html",
        "geo_state": "NE",
        "search": search_nebraska_spb,
        "discover_docs": discover_nebraska_spb_documents,
        "detail_mode": "html",
    },
    "nj_purchase": {
        "list_url": "https://www.nj.gov/treasury/purchase/",
        "geo_state": "NJ",
        "search": search_nj_purchase,
        "discover_docs": discover_nj_purchase_documents,
        "detail_mode": "pdf_notice",
    },
    "tn_cpo": {
        "list_url": (
            "https://www.tn.gov/generalservices/procurement/central-procurement-office--cpo-/"
            "supplier-information/request-for-proposals--rfp--opportunities.html"
        ),
        "geo_state": "TN",
        "search": search_tn_cpo,
        "discover_docs": discover_tn_cpo_documents,
        "detail_mode": "pdf_notice",
    },
    "uw_procurement": {
        "list_url": "https://finance.uw.edu/ps/suppliers/supplier-solicitation-notices",
        "geo_state": "WA",
        "search": search_uw_procurement,
        "discover_docs": discover_uw_procurement_documents,
        "detail_mode": "pdf_notice",
    },
    "delaware_open_bids": {
        "list_url": "https://data.delaware.gov/resource/2hnj-zwix.json",
        "geo_state": "DE",
        "search": search_delaware_mmp,
        "discover_docs": discover_delaware_mmp_documents,
        "detail_mode": "pdf_notice",
    },
    "erie_ny": {
        "list_url": "https://www3.erie.gov/purchasing/retrieve-bids",
        "geo_state": "NY",
        "search": search_erie_ny,
        "discover_docs": discover_erie_ny_documents,
        "detail_mode": "pdf_notice",
    },
    "worcester_ma": {
        "list_url": "https://www.worcesterma.gov/finance/purchasing-bids/bids/open-bids",
        "geo_state": "MA",
        "search": search_worcester_ma,
        "discover_docs": discover_worcester_ma_documents,
        "detail_mode": "pdf_notice",
    },
    "cleveland_oh": {
        "list_url": "https://www.clevelandohio.gov/city-hall/departments/finance/invitations-bid",
        "geo_state": "OH",
        "search": search_cleveland_oh,
        "discover_docs": discover_cleveland_oh_documents,
        "detail_mode": "pdf_notice",
    },
    "jefferson_tx": {
        "list_url": "https://jeffersoncountytx.gov/Purchasing/NoticesForBid",
        "geo_state": "TX",
        "search": search_jefferson_tx,
        "discover_docs": discover_jefferson_tx_documents,
        "detail_mode": "pdf_notice",
    },
    "alaska_opn": {
        "list_url": "https://aws.state.ak.us/OnlinePublicNotices/Notices/Search.aspx?status=1",
        "geo_state": "AK",
        "search": search_alaska_opn,
        "discover_docs": discover_alaska_opn_documents,
        "detail_mode": "pdf_notice",
    },
    "tntech": {
        "list_url": "https://www.tntech.edu/purchasing/bidopportunities.php",
        "geo_state": "TN",
        "search": search_tntech,
        "discover_docs": discover_tntech_documents,
        "detail_mode": "pdf_notice",
    },
    "kent_state": {
        "list_url": "https://www.kent.edu/procurement/bids",
        "geo_state": "OH",
        "search": search_kent_state,
        "discover_docs": discover_kent_state_documents,
        "detail_mode": "pdf_notice",
    },
    "tulsa_ok": {
        "list_url": (
            "https://www.cityoftulsa.org/government/departments/finance/selling-to-the-city/"
            "bid-opportunities-and-results/?t=current"
        ),
        "geo_state": "OK",
        "search": search_tulsa_ok,
        "discover_docs": discover_tulsa_ok_documents,
        "detail_mode": "pdf_notice",
    },
    "columbus_ga": {
        "list_url": "https://columbusga.gov/finance/bid-opportunities",
        "geo_state": "GA",
        "search": search_columbus_ga,
        "discover_docs": discover_columbus_ga_documents,
        "detail_mode": "pdf_notice",
    },
    "providence_ri": {
        "list_url": "https://www.providenceri.gov/purchasing/openrfpsummary/",
        "geo_state": "RI",
        "search": search_providence_ri,
        "discover_docs": discover_providence_ri_documents,
        "detail_mode": "pdf_notice",
    },
    "sanjuan_usd": {
        "list_url": (
            "https://www.sanjuan.edu/our-district/contracts-and-bids/"
            "other-services-contracts-and-bids/bidrfp-files"
        ),
        "geo_state": "CA",
        "search": search_sanjuan_usd,
        "discover_docs": discover_sanjuan_usd_documents,
        "detail_mode": "pdf_notice",
    },
    "pocatello_sd25": {
        "list_url": "https://www.sd25.us/departments/business-office",
        "geo_state": "ID",
        "search": search_pocatello_sd25,
        "discover_docs": discover_pocatello_sd25_documents,
        "detail_mode": "pdf_notice",
        "live_only": True,
        "archive_index": {
            "url": "https://www.sd25.us/departments/business-office",
            "section": "Historic Bid Information",
            "note": "Year folders on Business Office page — look up later, do not acquire.",
        },
    },
    "danville_va": {
        "list_url": "https://www.danvilleva.gov/Bids.aspx?CatID=16&Status=Open&txtSort=Date",
        "geo_state": "VA",
        "search": search_danville_va,
        "discover_docs": discover_danville_va_documents,
        "detail_mode": "pdf_notice",
    },
    "papillion_plcs": {
        "list_url": "https://www.plcschools.org/our-district/bids-rfps-and-rfis",
        "geo_state": "NE",
        "search": search_papillion_plcs,
        "discover_docs": discover_papillion_plcs_documents,
        "detail_mode": "pdf_notice",
    },
    "oceanview_ovsd": {
        "list_url": "https://www.ovsd.org/departments/purchasing",
        "geo_state": "CA",
        "search": search_oceanview_ovsd,
        "discover_docs": discover_oceanview_ovsd_documents,
        "detail_mode": "pdf_notice",
    },
    "msu_procurement": {
        "list_url": "https://procurement.msu.edu/for-suppliers/bid-opportunities",
        "geo_state": "MI",
        "search": search_msu_procurement,
        "discover_docs": discover_msu_procurement_documents,
        "detail_mode": "pdf_notice",
    },
    "indiana_idoa": {
        "list_url": "https://www.in.gov/idoa/procurement/current-business-opportunities/",
        "geo_state": "IN",
        "search": search_indiana_idoa,
        "discover_docs": discover_indiana_idoa_documents,
        "detail_mode": "pdf_notice",
    },
    "pa_emarketplace": {
        "list_url": "https://www.emarketplace.state.pa.us/Search.aspx",
        "geo_state": "PA",
        "search": search_pa_emarketplace,
        "discover_docs": discover_pa_emarketplace_documents,
        "detail_mode": "pdf_notice",
    },
    "louisiana_lapac": {
        "list_url": "https://wwwcfprd.doa.louisiana.gov/osp/lapac/catbids.cfm",
        "geo_state": "LA",
        "search": search_louisiana_lapac,
        "discover_docs": discover_louisiana_lapac_documents,
        "detail_mode": "pdf_notice",
        "live_only": True,
        "archive_index": {
            "url": "https://wwwcfprd.doa.louisiana.gov/osp/lapac/catbids.cfm",
            "note": "LaPAC category search; front-end keeps open Bid Open Date only.",
        },
    },
}

# Listing link IS the solicitation document (PDF/DOCX package).
_PDF_NOTICE_SOURCES = frozenset(
    {
        "nj_purchase",
        "tn_cpo",
        "uw_procurement",
        "delaware_open_bids",
        "erie_ny",
        "worcester_ma",
        "cleveland_oh",
        "jefferson_tx",
        "alaska_opn",
        "tntech",
        "kent_state",
        "tulsa_ok",
        "columbus_ga",
        "providence_ri",
        "sanjuan_usd",
        "pocatello_sd25",
        "danville_va",
        "papillion_plcs",
        "oceanview_ovsd",
        "msu_procurement",
        "indiana_idoa",
        "pa_emarketplace",
        "louisiana_lapac",
    }
)
_LIST_FETCH_DOCS = frozenset(
    {
        "delaware_open_bids",
        "worcester_ma",
        "alaska_opn",
        "tulsa_ok",
        "danville_va",
        "pa_emarketplace",
    }
)


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def keep_root() -> Path:
    p = get_settings().data_root() / "free_source_keep"
    p.mkdir(parents=True, exist_ok=True)
    (p / "by_source").mkdir(exist_ok=True)
    return p


def trash_root() -> Path:
    p = get_settings().data_root() / "free_source_trash"
    p.mkdir(parents=True, exist_ok=True)
    return p


def body_triage_root() -> Path:
    p = get_settings().data_root() / "ai_free_source_body_triage"
    p.mkdir(parents=True, exist_ok=True)
    return p


_REAL_ATTACHMENT_EXT = frozenset(
    {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".xlsm", ".csv", ".txt", ".rtf", ".zip", ".html", ".htm"}
)

_OFFICE_INTERNAL_NAME_RE = re.compile(
    r"(?i)^("
    r"\[content_types\]\.xml|"
    r".*\.rels|"
    r"app\.xml|core\.xml|custom\.xml|"
    r"document\.xml|settings\.xml|styles\.xml|fonttable\.xml|websettings\.xml|"
    r"sharedstrings\.xml|workbook\.xml|sheet\d+\.xml|theme\d+\.xml|"
    r"vbaproject\.bin|calcchain\.xml"
    r")$"
)


def is_real_attachment_doc(doc: dict[str, Any]) -> bool:
    """True for solicitation attachments; false for Office ZIP guts / junk members."""
    name = str(doc.get("name") or "").strip()
    if not name or name.startswith("."):
        return False
    base = Path(name).name
    if _OFFICE_INTERNAL_NAME_RE.match(base):
        return False
    path = str(doc.get("path") or doc.get("url") or "").replace("\\", "/").lower()
    low_name = name.replace("\\", "/").lower()
    combined = f"{low_name}|{path}"
    if any(
        seg in combined
        for seg in ("/_rels/", "/word/", "/xl/", "/ppt/", "/docprops/", "[content_types]")
    ):
        ext = Path(base).suffix.lower()
        if ext not in _REAL_ATTACHMENT_EXT:
            return False
    ext = Path(base).suffix.lower()
    if ext in _REAL_ATTACHMENT_EXT:
        return True
    # Direct download without extension can still be a notice blob.
    if not doc.get("from_zip") and "_extracted" not in combined:
        return ext in {"", ".bin"} or ext in _REAL_ATTACHMENT_EXT
    return False


def real_attachment_docs(docs: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [d for d in (docs or []) if is_real_attachment_doc(d)]


def real_attachment_hashes(docs: list[dict[str, Any]] | None) -> set[str]:
    return {
        str(d.get("sha256") or "")
        for d in real_attachment_docs(docs)
        if d.get("sha256")
    }


def opportunity_body_sha256(opportunity: dict[str, Any] | None) -> str:
    opp = opportunity or {}
    blob = json.dumps(
        {
            "title": opp.get("title"),
            "description": opp.get("description"),
            "summary": opp.get("summary"),
            "notice_type": opp.get("notice_type"),
            "ai_summary": opp.get("ai_summary"),
        },
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return sha256(blob.encode("utf-8")).hexdigest()


def persist_body_triage(
    *,
    source_key: str,
    nb_key: str,
    opportunity: dict[str, Any] | None,
    triage: dict[str, Any],
    reused: bool = False,
) -> Path:
    """Durable full OpenAI body-triage JSON BEFORE keep/trash acts on it."""
    dest = body_triage_root() / source_key
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / f"{nb_key}.json"
    payload = {
        "persisted_at": _utc(),
        "source_key": source_key,
        "nb_id": nb_key,
        "body_sha256": opportunity_body_sha256(opportunity) if opportunity else None,
        "reused_unchanged_body": reused,
        "opportunity_snapshot": {
            "title": (opportunity or {}).get("title"),
            "description": str((opportunity or {}).get("description") or "")[:12000],
            "summary": (opportunity or {}).get("summary"),
            "notice_type": (opportunity or {}).get("notice_type"),
        },
        "triage": triage,
    }
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def load_reusable_body_triage(
    *,
    source_key: str,
    nb_key: str,
    body_sha: str,
) -> dict[str, Any] | None:
    path = body_triage_root() / source_key / f"{nb_key}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if data.get("body_sha256") != body_sha:
        return None
    triage = data.get("triage")
    if not isinstance(triage, dict) or not triage.get("ok"):
        return None
    # Replayed triage is free — zero out spend attribution for this run.
    out = dict(triage)
    out["openai_spent_usd"] = 0.0
    out["reused_unchanged_body"] = True
    out["live_ai_invoked"] = False
    return out


def apply_deadline_from_evidence(item: dict[str, Any]) -> dict[str, Any]:
    """Copy listing/triage/doc-read close into deadline_at when listing omitted it."""
    from m3.research.opportunity_ui_ready import enrich_deadline_from_evidence

    enriched = enrich_deadline_from_evidence(
        {
            **item,
            "triage": item.get("triage") or {},
            "doc_read": item.get("doc_read") or {},
            "deadline_at": item.get("deadline_at"),
        }
    )
    out = dict(item)
    if enriched.get("deadline_at"):
        out["deadline_at"] = enriched["deadline_at"]
    return out


def past_deadline_scrap_reason(item: dict[str, Any]) -> str | None:
    """Once we know the close date and it has passed — scrap, do not keep/hold."""
    from datetime import datetime, timezone

    from m3.research.opportunity_ui_ready import parse_deadline_value

    due = parse_deadline_value(item.get("deadline_at"))
    if due is None:
        doc_final = ((item.get("doc_read") or {}).get("final") or {})
        triage = item.get("triage") or {}
        due = parse_deadline_value(
            doc_final.get("close_date") or triage.get("close_date")
        )
    if due is not None and due < datetime.now(timezone.utc):
        return f"past_deadline:{due.date().isoformat()}"
    return None


def doc_read_not_product_reason(item: dict[str, Any]) -> str | None:
    """After DOC_READ: demote false PRODUCT keeps when docs prove services/PBM/food."""
    from m3.research.opportunity_checklist_triage import (
        triage_product_service_contradiction_reason,
    )

    doc_read = item.get("doc_read") or {}
    final = doc_read.get("final") if isinstance(doc_read.get("final"), dict) else {}
    title = str(item.get("title") or "")
    what = str(final.get("what_they_want") or (item.get("triage") or {}).get("what_they_want") or "")
    food = perishable_food_reason(title, what, final.get("want_type"))
    if food:
        return f"doc_read_{food}"
    if not final:
        return None
    reason = triage_product_service_contradiction_reason(
        {
            "want_type": final.get("want_type") or "PRODUCT",
            "decision": "PRODUCT",
            "what_they_want": final.get("what_they_want"),
            "parsed": final,
        }
    )
    if reason:
        return f"doc_read_{reason}"
    want = str(final.get("want_type") or "").upper()
    if want in {"SERVICE", "MIX", "SERVICE_OR_MIX"}:
        return f"doc_read_want_type_not_product:{want}"
    return None


def _zip_member_is_real_solicitation_file(member_name: str, info_filename: str) -> bool:
    name = Path(member_name).name
    if not name or name.startswith(".") or name.startswith("~"):
        return False
    if _OFFICE_INTERNAL_NAME_RE.match(name):
        return False
    low = (info_filename or "").replace("\\", "/").lower()
    ext = Path(name).suffix.lower()
    if ext in _REAL_ATTACHMENT_EXT:
        return True
    if any(seg in low for seg in ("/_rels/", "/word/", "/xl/", "/ppt/", "/docprops/")):
        return False
    return False


def _strip_html(html: str) -> str:
    t = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html or "")
    t = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", t)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _nebraska_spb_main_text(html: str, *, title: str = "", solicitation_number: str = "") -> str:
    """Drop DAS Materiel chrome; keep solicitation body OpenAI actually needs."""
    text = _strip_html(html)
    # Nav link "Surplus Property Auction" sits BEFORE ITB bodies — scrub, don't split-left.
    text = re.sub(
        r"(?i)\b(?:Links of Interest|Surplus Property Auction|Search State Contract Database|"
        r"Toggle navigation|Forms\s*&\s*Docs|Office Depot Contract|Purchasing Public Records Request|"
        r"Purchasing Contracts)\b",
        " ",
        text,
    )
    sol = (solicitation_number or "").strip()
    cut_patterns = []
    if sol:
        cut_patterns.append(rf"(?i)\b(?:ITB|RFQ|RFP|RFI)\s*{re.escape(sol)}\b")
        cut_patterns.append(rf"(?i)\b{re.escape(sol)}\b")
    cut_patterns.append(r"(?i)\bAGENCY\s*:")
    if title.strip():
        cut_patterns.append(rf"(?i)\b{re.escape(title[:48].strip())}\b")
    for pat in cut_patterns:
        m = re.search(pat, text)
        if m:
            text = text[m.start() :]
            break
    return re.sub(r"\s+", " ", text).strip()


def detail_text_to_opportunity(item: dict[str, Any], text: str, final_url: str) -> dict[str, Any]:
    """Build a triage-ready opportunity from extracted detail text (HTML or PDF)."""
    title = str(item.get("title") or "")
    body = (text or "").strip()
    return {
        "nb_id": item.get("external_key") or item.get("solicitation_number"),
        "title": title,
        "agency": item.get("buyer_name") or item.get("agency"),
        "solicitation_number": item.get("solicitation_number"),
        "notice_type": item.get("procurement_type"),
        "procurement_kind": "goods",  # label only — triage must use body
        "response_deadline": item.get("deadline_at"),
        "posted_date": item.get("posted_at"),
        "description": body[:50000],
        "summary": (item.get("summary") or body[:2000] or title),
        "source_url": final_url,
        "document_count": len(item.get("document_urls") or []),
        "attachment_count": len(item.get("document_urls") or []),
        "documents": [{"url": u} for u in (item.get("document_urls") or [])],
        "states": [item.get("geo_state")] if item.get("geo_state") else [],
        "source": item.get("source_key"),
        "nigp_category": item.get("nigp_category"),
    }


def detail_html_to_opportunity(item: dict[str, Any], html: str, final_url: str) -> dict[str, Any]:
    """Build a triage-ready opportunity object from a free-portal detail page."""
    title = str(item.get("title") or "")
    sol = str(item.get("solicitation_number") or "")
    if str(item.get("source_key") or "") == "nebraska_spb" or "nebraska.gov" in (final_url or ""):
        text = _nebraska_spb_main_text(html, title=title, solicitation_number=sol)
    else:
        text = _strip_html(html)
    return detail_text_to_opportunity(item, text, final_url)


def step1_list_source(source_key: str, *, limit: int | None = None) -> dict[str, Any]:
    spec = FREE_SOURCE_SPECS[source_key]
    source = {
        "source_key": source_key,
        "config": {
            "list_url": spec["list_url"],
            "geo_state": spec["geo_state"],
        },
    }
    # Some portals need detail/doc resolution during list (DE/Worcester/Alaska).
    fetch_docs = source_key in _LIST_FETCH_DOCS
    result = spec["search"](source, limit=limit, fetch_detail_docs=fetch_docs)
    return {
        "step": 1,
        "name": "LIST",
        "ok": bool(result.get("ok")),
        "source_key": source_key,
        "rows_examined": result.get("rows_examined"),
        "items": result.get("items") or [],
        "stats": result.get("stats") or {},
        "errors": result.get("errors") or [],
    }


def step1_list_nebraska(*, limit: int | None = None) -> dict[str, Any]:
    return step1_list_source("nebraska_spb", limit=limit)


# Cheap title/category kills — never spend OpenAI body triage on these.
_REGEX_SERVICEISH_RE = re.compile(
    r"(?i)\b("
    r"case\s+management|consulting|staffing|construction|renovation|remodel(?:ing)?|"
    r"janitorial|custodial|professional\s+services|management\s+services|"
    r"engineering\s+services|architectural\s+services|legal\s+services|"
    r"audit(?:ing|or)?\s+services|snow\s+removal|mowing\b|pest\s+control|"
    r"asphalt\s+(?:paving|overlay)|road\s+resurfacing|pavement\s+(?:repair|marking)|"
    r"landscap(?:e|ing)\s+services|grounds?\s+maintenance|"
    r"pharmacy\s+benefits?|benefits?\s+management|\bPBM\b|"
    r"board\s+recommend|bid\s+tabulation|tab\s+sheet|"
    r"questions?\s+and\s+responses?|pre[\s-]?bid\s+slide|"
    r"tree\s+trimming\s+services|hauling\s+of|drayage|"
    r"waste\s+pickup|biohazard\s+waste|"
    r"software\s+(?:renewal|maintenance|subscription)|"
    r"(?:annual|license)\s+renewal|"
    r"therapy\s+training|commute\s+solutions?|"
    r"equipment\s+rental|uniform\s+rental|tractor\s+rental|"
    r"rental\s+and\s+launder|job\s+walk|"
    r"roof\s+replacement|fire\s+alarm\s+system\s+replacement|"
    r"road\s+overlay|bid\s+pack\b|"
    # Perishable food commodities — never spend triage/docs on these.
    r"food\s+items|perishable(?:s)?|grocer(?:y|ies)|"
    r"(?:fs\s+)?(?:milk|bread)\s+supply|milk\s+supply|bread\s+supply|"
    r"fresh\s+(?:fruit|vegetable|produce)|dairy\s+products?|"
    r"frozen\s+(?:foods?|meats?|turkey)|cafeteria\s+food"
    r")\b"
)

_CLOSED_LISTING_STATUS_RE = re.compile(
    r"(?i)^(closed|awarded|cancelled|canceled|expired|historical|historic|"
    r"archive|archived|tabulated|complete[d]?|inactive)\b"
)


def listing_not_open_live_reason(item: dict[str, Any]) -> str | None:
    """Front-end gate: only open/live solicitations enter body triage."""
    status = str(
        item.get("listing_status")
        or item.get("status")
        or (item.get("evidence") or {}).get("listing_status")
        or ""
    ).strip()
    if status and _CLOSED_LISTING_STATUS_RE.search(status):
        return f"listing_not_open:{status}"
    from m3.research.opportunity_ui_ready import parse_deadline_value
    from datetime import datetime, timezone

    due = parse_deadline_value(item.get("deadline_at") or item.get("response_deadline"))
    if due is not None and due < datetime.now(timezone.utc):
        return "listing_past_deadline"
    return None


def step2_regex_screen(items: list[dict[str, Any]]) -> dict[str, Any]:
    keep: list[dict[str, Any]] = []
    trash: list[dict[str, Any]] = []
    for it in items:
        title = str(it.get("title") or "")
        live_reason = listing_not_open_live_reason(it)
        if live_reason:
            trash.append({**it, "trash_reason": live_reason, "trash_step": 2})
            continue
        reason = listing_skip_before_detail_reason(title)
        cat = str(it.get("nigp_category") or "")
        if reason:
            trash.append({**it, "trash_reason": reason, "trash_step": 2})
            continue
        if _REGEX_SERVICEISH_RE.search(f"{title} {cat}"):
            trash.append(
                {
                    **it,
                    "trash_reason": "regex_serviceish_title_or_category",
                    "trash_step": 2,
                }
            )
            continue
        keep.append(it)
    return {
        "step": 2,
        "name": "REGEX_SCREEN",
        "kept": keep,
        "trashed": trash,
        "kept_count": len(keep),
        "trashed_count": len(trash),
    }


def step3_body_triage(
    items: list[dict[str, Any]],
    *,
    source_key: str = "nebraska_spb",
    max_workers: int = 10,
) -> dict[str, Any]:
    """Fetch detail body + OpenAI checklist. No durable doc store yet."""
    spec = FREE_SOURCE_SPECS.get(source_key) or FREE_SOURCE_SPECS["nebraska_spb"]
    discover = spec["discover_docs"]
    detail_mode = str(spec.get("detail_mode") or "html")

    def _prep(it: dict[str, Any]) -> dict[str, Any]:
        # Never raise — one corrupt notice must not kill the whole portal.
        try:
            return _prep_inner(it)
        except Exception as exc:  # noqa: BLE001
            return {
                "item": it,
                "triage": {
                    "ok": False,
                    "nb_id": it.get("external_key"),
                    "want_type": "UNCLEAR",
                    "decision": "UNCLEAR",
                    "next_step": "SKIP",
                    "why": f"prep_error:{type(exc).__name__}:{exc}"[:240],
                },
                "opportunity": None,
                "detail_url": str(it.get("official_url") or ""),
            }

    def _prep_inner(it: dict[str, Any]) -> dict[str, Any]:
        url = str(it.get("official_url") or "")
        # Notice-file portals (NJ/TN/UW): the listing link IS the solicitation document.
        path_low = url.lower().split("?", 1)[0]
        if detail_mode == "pdf_notice" or path_low.endswith((".pdf", ".doc", ".docx")):
            import time

            from m3.research.documents import extract_xlsx_text

            status, final, data = 0, url, b""
            last_why = "notice_fetch_failed"
            for attempt in range(4):
                try:
                    status, final, data, _ctype = fetch_bytes(url, timeout=90)
                    if status < 400 and data:
                        break
                    last_why = f"detail_fetch_failed:{status}"
                except Exception as exc:  # noqa: BLE001
                    last_why = f"notice_fetch_failed:{type(exc).__name__}"
                    status, final, data = 0, url, b""
                time.sleep(0.8 * (attempt + 1))
            if status >= 400 or not data:
                return {
                    "item": it,
                    "triage": {
                        "ok": False,
                        "nb_id": it.get("external_key"),
                        "want_type": "UNCLEAR",
                        "decision": "UNCLEAR",
                        "next_step": "SKIP",
                        "why": last_why,
                    },
                    "opportunity": None,
                    "detail_url": url,
                }
            final_path = (final or url).lower().split("?", 1)[0]
            # Choose extractor by real type — never utf8-decode binaries / Office ZIP guts.
            try:
                if final_path.endswith(".docx") or (
                    data[:2] == b"PK" and final_path.endswith((".docx",))
                ):
                    extracted = extract_docx_text(data)
                elif final_path.endswith((".xlsx", ".xlsm", ".xls")) or (
                    data[:2] == b"PK" and "sheet" in final_path
                ):
                    extracted = extract_xlsx_text(data)
                elif data[:4] == b"%PDF" or final_path.endswith(".pdf") or "documentcenter/view" in final_path:
                    extracted = extract_pdf_text(data, allow_ocr=False)
                elif data[:2] == b"PK":
                    # Unknown ZIP/Office container — try docx then pdf, never raw decode.
                    extracted = extract_docx_text(data)
                    if not (extracted.get("text") or "").strip():
                        extracted = extract_pdf_text(data, allow_ocr=False)
                else:
                    extracted = extract_pdf_text(data, allow_ocr=False)
            except Exception as exc:  # noqa: BLE001
                extracted = {"ok": False, "text": "", "reason": f"extract_error:{type(exc).__name__}"}
            text = str(extracted.get("text") or "")
            if len(text) < 40:
                text = f"{it.get('title') or ''}\n{it.get('summary') or ''}".strip()
            existing = list(it.get("document_urls") or [])
            if final or url:
                existing = list(dict.fromkeys([*(existing or []), final or url]))
            if source_key in _PDF_NOTICE_SOURCES:
                try:
                    docs = discover(final or url, "", extra_urls=existing)
                    doc_urls = [d["source_url"] for d in docs]
                except TypeError:
                    docs = discover(final or url, "")
                    doc_urls = [d["source_url"] for d in docs]
                except Exception:
                    doc_urls = existing
            else:
                doc_urls = existing
            it = {
                **it,
                "document_urls": doc_urls,
                "documents_available": "yes" if doc_urls else "unknown",
            }
            opp = detail_text_to_opportunity(it, text, final or url)
            return {"item": it, "opportunity": opp, "detail_url": final or url, "triage": None}

        status, final, html = fetch_text(url, timeout=25)
        if status >= 400 or not html:
            return {
                "item": it,
                "triage": {
                    "ok": False,
                    "nb_id": it.get("external_key"),
                    "want_type": "UNCLEAR",
                    "decision": "UNCLEAR",
                    "next_step": "SKIP",
                    "why": f"detail_fetch_failed:{status}",
                },
                "opportunity": None,
                "detail_url": url,
            }
        docs = discover(final or url, html)
        it = {
            **it,
            "document_urls": [d["source_url"] for d in docs],
            "documents_available": "yes" if docs else "unknown",
        }
        opp = detail_html_to_opportunity(it, html, final or url)
        return {"item": it, "opportunity": opp, "detail_url": final or url, "triage": None}

    prepared = map_parallel(_prep, items, max_workers=min(max_workers, max(1, len(items))), ordered=True)

    # Reuse prior full triage when the detail body hash is unchanged — skip OpenAI.
    # Also skip OpenAI when deterministic body/title rules already prove non-product.
    triage_inputs: list[dict[str, Any]] = []
    reused_by_id: dict[str, dict[str, Any]] = {}
    deterministic_by_id: dict[str, dict[str, Any]] = {}
    for p in prepared:
        if p.get("triage") and not p.get("opportunity"):
            continue
        if not p.get("opportunity"):
            continue
        it = p["item"]
        eid = str(it.get("external_key") or it.get("solicitation_number") or "")
        nb_key = re.sub(r"[^\w\-]+", "_", eid)
        opp = p["opportunity"]
        body_sha = opportunity_body_sha256(opp)
        prior = load_reusable_body_triage(source_key=source_key, nb_key=nb_key, body_sha=body_sha)
        if prior is not None:
            reused_by_id[eid] = prior
            continue
        det_reason = (
            body_surplus_sale_reason(opp)
            or body_agency_not_buying_reason(opp)
            or body_install_labor_reason(opp)
            or body_service_contract_reason(opp)
            or body_perishable_food_reason(opp)
            or perishable_food_reason(it.get("title"), opp.get("title"))
        )
        if det_reason:
            deterministic_by_id[eid] = {
                "ok": True,
                "nb_id": eid,
                "want_type": "SERVICE",
                "decision": "SERVICE_OR_MIX",
                "next_step": "SKIP",
                "why": f"deterministic_pre_openai:{det_reason}",
                "what_they_want": None,
                "openai_spent_usd": 0.0,
                "live_ai_invoked": False,
                "parsed": {
                    "agency_is_buying": True,
                    "is_product_only": False,
                    "why": det_reason,
                },
            }
            continue
        title_dec = title_signal_decision(str(opp.get("title") or it.get("title") or ""))
        if title_dec and title_dec.get("next_step") == "SKIP":
            deterministic_by_id[eid] = {
                "ok": True,
                "nb_id": eid,
                "want_type": title_dec.get("want_type") or "SERVICE",
                "decision": title_dec.get("decision") or "SERVICE_OR_MIX",
                "next_step": "SKIP",
                "why": f"deterministic_title:{title_dec.get('why')}",
                "what_they_want": None,
                "openai_spent_usd": 0.0,
                "live_ai_invoked": False,
                "parsed": {
                    "agency_is_buying": title_dec.get("agency_is_buying", True),
                    "is_product_only": False,
                    "why": title_dec.get("why"),
                },
            }
            continue
        triage_inputs.append({"nb_id": eid, "opportunity": opp})

    triaged = (
        triage_opportunities_parallel(triage_inputs, max_workers=max_workers) if triage_inputs else []
    )
    by_id = {str(t.get("nb_id")): t for t in triaged}
    by_id.update(reused_by_id)
    by_id.update(deterministic_by_id)
    kept: list[dict[str, Any]] = []
    trashed: list[dict[str, Any]] = []
    reused_count = 0
    deterministic_skip_count = 0
    for p in prepared:
        it = p["item"]
        eid = str(it.get("external_key") or it.get("solicitation_number"))
        nb_key = re.sub(r"[^\w\-]+", "_", eid)
        triage = p.get("triage") or by_id.get(eid)
        if not triage:
            trashed.append({**it, "trash_reason": "missing_triage", "trash_step": 3})
            continue
        reused = bool(triage.get("reused_unchanged_body")) or eid in reused_by_id
        if reused:
            reused_count += 1
        if eid in deterministic_by_id:
            deterministic_skip_count += 1
        # Persist FULL triage JSON before any keep/trash act (paid=kept for OpenAI body).
        persist_body_triage(
            source_key=source_key,
            nb_key=nb_key,
            opportunity=p.get("opportunity"),
            triage=triage,
            reused=reused,
        )
        gate = authorize_document_fetch(triage, opportunity=p.get("opportunity"))
        row = {
            **it,
            "triage": triage,
            "docs_fetch_gate": gate,
            "detail_url": p.get("detail_url"),
            "body_triage_reused": reused,
        }
        if (
            triage.get("ok")
            and str(triage.get("want_type") or "").upper() == "PRODUCT"
            and str(triage.get("decision") or "").upper() == "PRODUCT"
            and str(triage.get("next_step") or "").upper() == "FETCH_DOCUMENTS"
            and gate.get("authorized")
        ):
            kept.append(row)
        else:
            row["trash_reason"] = gate.get("reason") or triage.get("why") or "body_triage_not_product"
            row["trash_step"] = 3
            trashed.append(row)
    return {
        "step": 3,
        "name": "BODY_TRIAGE",
        "kept": kept,
        "trashed": trashed,
        "kept_count": len(kept),
        "trashed_count": len(trashed),
        "reused_unchanged_body": reused_count,
        "deterministic_skip_no_openai": deterministic_skip_count,
        "openai_calls": len(triage_inputs),
        "openai_spent_usd": sum(float((k.get("triage") or {}).get("openai_spent_usd") or 0) for k in kept)
        + sum(float((t.get("triage") or {}).get("openai_spent_usd") or 0) for t in trashed),
    }


def step4_download_docs(
    items: list[dict[str, Any]],
    *,
    source_key: str = "nebraska_spb",
    max_workers: int = 10,
) -> dict[str, Any]:
    """Download every project document for PRODUCT fits only."""
    spec = FREE_SOURCE_SPECS.get(source_key) or FREE_SOURCE_SPECS["nebraska_spb"]
    discover = spec["discover_docs"]

    def _one(it: dict[str, Any]) -> dict[str, Any]:
        urls = list(it.get("document_urls") or [])
        detail = str(it.get("detail_url") or it.get("official_url") or "")
        if not urls and detail:
            if source_key in _PDF_NOTICE_SOURCES:
                urls = [d["source_url"] for d in discover(detail, "", extra_urls=[detail])]
            else:
                urls = [d["source_url"] for d in discover(detail)]
        saved: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        sol = str(it.get("solicitation_number") or it.get("external_key") or "unknown")
        dest_dir = keep_root() / "by_source" / source_key / re.sub(r"[^\w\-]+", "_", sol)
        dest_dir.mkdir(parents=True, exist_ok=True)
        for url in urls:
            try:
                status, final, data, ctype = fetch_bytes(url, timeout=60)
                if status >= 400 or not data:
                    failed.append({"url": url, "status": status})
                    continue
                h = sha256(data).hexdigest()
                name = unquote(urlparse(final or url).path.split("/")[-1]) or f"{h[:12]}.bin"
                ext = Path(name).suffix.lstrip(".") or "bin"
                blob = blob_path_for_hash(h, ext=ext)
                if not blob.exists():
                    blob.write_bytes(data)
                local = dest_dir / name
                local.write_bytes(data)
                saved.append(
                    {
                        "name": name,
                        "bytes": len(data),
                        "sha256": h,
                        "blob": str(blob),
                        "path": str(local),
                        "url": final or url,
                        "content_type": ctype,
                    }
                )
                # Only explode true .zip solicitation packages (Indiana IDOA, etc.).
                # DOCX/XLSX are also PK zip containers — extracting them floods the
                # package with Office XML guts and wastes every-word OpenAI reads.
                if name.lower().endswith(".zip") and data[:2] == b"PK":
                    import zipfile

                    extract_dir = dest_dir / f"{Path(name).stem}_extracted"
                    extract_dir.mkdir(parents=True, exist_ok=True)
                    try:
                        with zipfile.ZipFile(io.BytesIO(data)) as zf:
                            for info in zf.infolist():
                                if info.is_dir():
                                    continue
                                member_name = Path(info.filename).name
                                if not _zip_member_is_real_solicitation_file(
                                    member_name, info.filename
                                ):
                                    continue
                                raw = zf.read(info)
                                mh = sha256(raw).hexdigest()
                                mext = Path(member_name).suffix.lstrip(".") or "bin"
                                mblob = blob_path_for_hash(mh, ext=mext)
                                if not mblob.exists():
                                    mblob.write_bytes(raw)
                                mlocal = extract_dir / member_name
                                mlocal.write_bytes(raw)
                                saved.append(
                                    {
                                        "name": member_name,
                                        "bytes": len(raw),
                                        "sha256": mh,
                                        "blob": str(mblob),
                                        "path": str(mlocal),
                                        "url": f"{final or url}#{info.filename}",
                                        "content_type": "application/octet-stream",
                                        "from_zip": name,
                                    }
                                )
                    except Exception as zexc:  # noqa: BLE001
                        failed.append({"url": url, "error": f"zip_extract:{type(zexc).__name__}:{zexc}"})
            except Exception as exc:  # noqa: BLE001
                failed.append({"url": url, "error": f"{type(exc).__name__}:{exc}"})
        # Fingerprint / KEEP index only care about real attachments — drop Office guts.
        saved = real_attachment_docs(saved)
        return {**it, "saved_documents": saved, "failed_documents": failed, "keep_dir": str(dest_dir)}

    out = map_parallel(_one, items, max_workers=min(max_workers, max(1, len(items))), ordered=True) if items else []
    return {
        "step": 4,
        "name": "DOCS",
        "items": out,
        "doc_count": sum(len(i.get("saved_documents") or []) for i in out),
        "failed_count": sum(len(i.get("failed_documents") or []) for i in out),
    }


def _load_keep_index(source_key: str) -> dict[str, Any]:
    path = keep_root() / "by_source" / source_key / "KEEP_INDEX.json"
    if not path.exists():
        return {"items": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"items": []}


def _prior_doc_fingerprint(source_key: str, solicitation_number: str) -> set[str]:
    idx = _load_keep_index(source_key)
    want = str(solicitation_number or "")
    for item in idx.get("items") or []:
        if str(item.get("solicitation_number") or "") != want:
            continue
        if not item.get("doc_read_ok"):
            return set()
        # Prefer explicit real-attachment fingerprint; fall back to filtered docs list.
        stored = item.get("attachment_fingerprint")
        if isinstance(stored, list) and stored:
            return {str(h) for h in stored if h}
        return real_attachment_hashes(item.get("docs") or [])
    return set()


def step5_doc_read(
    items: list[dict[str, Any]],
    *,
    source_key: str = "nebraska_spb",
    max_workers: int = 10,
    skip_unchanged: bool = True,
) -> dict[str, Any]:
    """OpenAI reads downloaded document text for PRODUCT fits (every local file).

    Daily mode: skip solicitations whose saved doc hashes already match a prior
    successful every-word read — free sites re-list everything, but we don't
    re-buy OpenAI for unchanged packages.
    """
    from m3.research.every_word_doc_read import read_every_word_for_nb

    # Cost is per token, not per worker — run packages wide (up to 80).
    package_workers = min(80, max(1, len(items)))
    # Bound total in-flight chunk calls so OpenAI rate limits don't erase the win.
    chunk_workers = max(2, min(20, max(2, 160 // package_workers)))

    def _one(it: dict[str, Any]) -> dict[str, Any]:
        sol = str(it.get("solicitation_number") or it.get("external_key") or "unknown")
        nb_key = re.sub(r"[^\w\-]+", "_", sol)
        originals_parent = keep_root() / "by_source" / source_key
        try:
            # Stable fingerprint = real attachments only (never Office ZIP internals).
            current_hashes = real_attachment_hashes(it.get("saved_documents") or [])
            ai_path = (
                get_settings().data_root()
                / "ai_free_source_doc_read"
                / source_key
                / f"{nb_key}.json"
            )
            if skip_unchanged and current_hashes:
                prior = _prior_doc_fingerprint(source_key, sol)
                cached = None
                if ai_path.exists():
                    try:
                        cached = json.loads(ai_path.read_text(encoding="utf-8"))
                    except Exception:
                        cached = None
                # Reuse prior successful every-word if KEEP fingerprint matches, OR
                # the saved AI report's file sha256 set matches current attachments
                # (avoids re-buying OpenAI after a mid-run restart).
                cached_hashes: set[str] = set()
                if isinstance(cached, dict) and cached.get("ok"):
                    cached_hashes = {
                        str(f.get("sha256") or "")
                        for f in (cached.get("files") or [])
                        if f.get("sha256")
                    }
                if (prior and prior == current_hashes) or (
                    cached_hashes and cached_hashes == current_hashes
                ):
                    return {
                        **it,
                        "doc_read": cached
                        or {
                            "ok": True,
                            "nb_id": nb_key,
                            "status": "REUSED_UNCHANGED_PACKAGE",
                            "openai_spent_usd": 0.0,
                        },
                        "doc_read_reused": True,
                    }
            rec = read_every_word_for_nb(
                nb_id=nb_key,
                title=str(it.get("title") or ""),
                originals_dir=originals_parent,
                max_workers=chunk_workers,
            )
            ai_dir = get_settings().data_root() / "ai_free_source_doc_read" / source_key
            ai_dir.mkdir(parents=True, exist_ok=True)
            (ai_dir / f"{nb_key}.json").write_text(json.dumps(rec, indent=2, default=str), encoding="utf-8")
            return {**it, "doc_read": rec, "doc_read_reused": False}
        except Exception as exc:  # noqa: BLE001
            # One corrupt PDF / bad JSON must NOT kill the whole portal.
            err = {
                "ok": False,
                "nb_id": nb_key,
                "status": "DOC_READ_ERROR",
                "error": f"{type(exc).__name__}:{exc}"[:400],
                "openai_spent_usd": 0.0,
            }
            ai_dir = get_settings().data_root() / "ai_free_source_doc_read" / source_key
            ai_dir.mkdir(parents=True, exist_ok=True)
            (ai_dir / f"{nb_key}.json").write_text(json.dumps(err, indent=2, default=str), encoding="utf-8")
            return {**it, "doc_read": err, "doc_read_reused": False, "doc_read_error": err["error"]}

    out = map_parallel(_one, items, max_workers=package_workers, ordered=True) if items else []
    return {
        "step": 5,
        "name": "DOC_READ",
        "items": out,
        "openai_spent_usd": sum(float((i.get("doc_read") or {}).get("openai_spent_usd") or 0) for i in out),
        "reused_unchanged": sum(1 for i in out if i.get("doc_read_reused")),
        "package_workers": package_workers,
        "chunk_workers": chunk_workers,
    }


def _parse_posted_date(raw: Any) -> Any:
    from datetime import date, datetime

    if raw is None or raw == "":
        return None
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    s = str(raw).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s[:19]).date()
    except Exception:
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").date()
        except Exception:
            return None


def promote_kept_to_opportunities(
    kept_items: list[dict[str, Any]],
    *,
    source_key: str,
) -> dict[str, Any]:
    """Upsert UI-ready PRODUCT keepers into Opportunity.

    Unknown Closes = FAIL — incomplete packages stay in keep index / disk only
    and are never shown as Active/Saved rows.
    """
    from m3.db import SessionLocal
    from m3.models import Opportunity, User
    from m3.research.opportunity_ui_ready import (
        enrich_deadline_from_evidence,
        opportunity_ui_ready,
        parse_deadline_value,
    )
    from m3.services import save_opportunity

    if not kept_items:
        return {"promoted": 0, "created": 0, "updated": 0, "skipped_incomplete": 0, "source": source_key}

    db = SessionLocal()
    created = updated = skipped = 0
    errors: list[str] = []
    skip_reasons: list[str] = []
    try:
        owner = (
            db.query(User)
            .filter(User.is_primary_owner.is_(True), User.is_active.is_(True))
            .order_by(User.created_at.asc())
            .first()
        )
        if owner is None:
            owner = db.query(User).filter(User.is_active.is_(True)).order_by(User.created_at.asc()).first()
        if owner is None:
            return {"promoted": 0, "error": "no_active_user", "source": source_key}

        for it in kept_items:
            try:
                key = str(it.get("external_key") or "").strip()
                if not key:
                    sol = str(it.get("solicitation_number") or it.get("title") or "").strip()
                    key = f"{source_key}:{sol}" if sol else ""
                if not key:
                    skipped += 1
                    continue
                triage = it.get("triage") or {}
                doc_final = ((it.get("doc_read") or {}).get("final") or {})
                enriched = enrich_deadline_from_evidence(
                    {
                        **it,
                        "triage": triage,
                        "doc_read": it.get("doc_read") or {},
                        "deadline_at": it.get("deadline_at"),
                    }
                )
                # Past close = scrap. Never promote expired packets as Active.
                past = past_deadline_scrap_reason(enriched)
                food = perishable_food_reason(
                    it.get("title"),
                    triage.get("what_they_want"),
                    doc_final.get("what_they_want"),
                )
                if past or food:
                    skipped += 1
                    skip_reasons.append(
                        f"{it.get('solicitation_number') or key}:{past or food}"
                    )
                    existed = db.query(Opportunity).filter_by(external_key=key).one_or_none()
                    if existed is not None and existed.status in {"saved", "pursuing"}:
                        existed.status = "archived"
                        note = (existed.notes or "").strip()
                        tag = f"SCRAPPED:{past or food}"
                        if tag not in note:
                            existed.notes = (note + "\n" + tag).strip() if note else tag
                        existed.updated_at = datetime.now(timezone.utc)
                        db.add(existed)
                        db.commit()
                    continue
                from m3.research.documents import (
                    prefer_schedule_quantity,
                    quantity_is_unspecified,
                    schedule_quantity_from_paths,
                )
                from m3.research.product_category import (
                    guess_product_category,
                    normalize_product_category,
                )
                from m3.web.titles import build_product_list_title, format_quantity_storage

                what = (
                    triage.get("what_they_want")
                    or doc_final.get("what_they_want")
                    or None
                )
                raw_qty = triage.get("quantity") or doc_final.get("quantity") or it.get("quantity")
                # Fail-closed: bid-sheet Estimated Usage / Quantity wins over AI "Not specified".
                schedule = (it.get("doc_read") or {}).get("schedule_quantity") or {}
                if quantity_is_unspecified(raw_qty) or not schedule.get("title_quantity"):
                    keep_paths = []
                    for d in it.get("saved_documents") or []:
                        p = d.get("path") or d.get("local_path")
                        if p:
                            keep_paths.append(p)
                    if not keep_paths:
                        sol = str(it.get("solicitation_number") or key or "")
                        keep_dir = (
                            keep_root()
                            / "by_source"
                            / source_key
                            / re.sub(r"[^\w\-]+", "_", sol)
                        )
                        kd = it.get("keep_dir")
                        if kd:
                            keep_dir = Path(kd)
                        if keep_dir.is_dir():
                            keep_paths = [
                                p
                                for p in keep_dir.iterdir()
                                if p.is_file()
                                and p.suffix.lower() in {".xlsx", ".xlsm", ".xls"}
                            ]
                    if keep_paths:
                        scanned = schedule_quantity_from_paths(keep_paths)
                        if scanned.get("title_quantity"):
                            schedule = scanned
                raw_qty = prefer_schedule_quantity(raw_qty, schedule)
                qty = format_quantity_storage(raw_qty)
                category = normalize_product_category(
                    doc_final.get("product_category")
                    or it.get("product_category")
                    or triage.get("product_category")
                ) or guess_product_category(it.get("title"), what, qty)
                # Never promote without a category — last-chance guess from list title text.
                list_title = build_product_list_title(
                    what_they_want=what,
                    quantity=raw_qty,
                    official_title=str(it.get("title") or ""),
                    solicitation_number=str(it.get("solicitation_number") or ""),
                )
                if not category:
                    category = guess_product_category(list_title, it.get("title"), what, qty)
                if not category:
                    category = "general_products"
                item = {
                    "external_key": key,
                    "title": it.get("title"),
                    "display_title": list_title,
                    "solicitation_number": it.get("solicitation_number"),
                    "buyer_name": it.get("buyer_name") or it.get("agency"),
                    "geo_state": it.get("geo_state") or (FREE_SOURCE_SPECS.get(source_key) or {}).get("geo_state"),
                    "deadline_at": enriched.get("deadline_at"),
                    "official_url": it.get("official_url") or it.get("detail_url"),
                    "source_key": source_key,
                    "requirement_class": "product_supply",
                    "procurement_type": it.get("procurement_type"),
                    "product_category": category,
                    "quantity": qty,
                    "what_they_want": what if isinstance(what, str) else format_quantity_storage(what),
                    "documents_available": "yes" if (it.get("saved_documents") or []) else "unknown",
                    "estimated_value_unknown": True,
                    "posted_at": it.get("posted_at") or it.get("posted_date"),
                    "want_type": triage.get("want_type") or doc_final.get("want_type"),
                    "evidence": {
                        "free_source_keep": True,
                        "want_type": triage.get("want_type"),
                        "what_they_want": what,
                        "match_reason": triage.get("match_reason") or doc_final.get("match_reason"),
                        "supplier_quote_ready": doc_final.get("supplier_quote_ready"),
                        "source_name": source_key,
                        "close_date_source": "listing"
                        if parse_deadline_value(it.get("deadline_at"))
                        else ("triage_or_doc_read" if parse_deadline_value(enriched.get("deadline_at")) else None),
                        "schedule_quantity": schedule.get("title_quantity") if schedule else None,
                        "quantity_source": (
                            "bid_sheet_schedule"
                            if schedule.get("title_quantity")
                            and str(raw_qty) == str(schedule.get("title_quantity"))
                            else None
                        ),
                    },
                }
                ok, reasons = opportunity_ui_ready(item)
                # Incomplete = FAIL. Gov postings have closes; if we don't have them,
                # we failed extraction — do NOT show as Active/Saved. Keep on disk only.
                item["evidence"] = {
                    **(item.get("evidence") or {}),
                    "needs_openai_field_repair": (not ok),
                    "ui_ready_fail": reasons if not ok else [],
                    "blocked_from_ui_incomplete": (not ok),
                }
                if not ok:
                    skipped += 1
                    skip_reasons.append(
                        f"{it.get('solicitation_number') or key}:{','.join(reasons)}"
                    )
                    # If a prior incomplete row is already in Active, pull it out.
                    existed = db.query(Opportunity).filter_by(external_key=key).one_or_none()
                    if existed is not None and existed.status in {"saved", "pursuing"}:
                        ev = dict(existed.raw_evidence or {}) if isinstance(existed.raw_evidence, dict) else {}
                        ev.update(item["evidence"])
                        existed.raw_evidence = ev
                        existed.status = "archived"
                        note = (existed.notes or "").strip()
                        tag = "INCOMPLETE_FAIL: missing closes/id — held until field repair"
                        if tag not in note:
                            existed.notes = (note + "\n" + tag).strip() if note else tag
                        existed.updated_at = datetime.now(timezone.utc)
                        db.add(existed)
                        db.commit()
                    continue

                existed = db.query(Opportunity).filter_by(external_key=key).one_or_none()
                opp = save_opportunity(db, item, owner, data_origin="operator")
                posted = _parse_posted_date(item.get("posted_at"))
                if posted is not None:
                    opp.posted_at = posted
                if category:
                    opp.product_category = category
                if list_title:
                    opp.display_title = list_title[:512]
                # Keep what_they_want on evidence for Active title rendering.
                ev = dict(opp.raw_evidence or {}) if isinstance(opp.raw_evidence, dict) else {}
                if what:
                    ev["what_they_want"] = what
                if category:
                    ev["product_category"] = category
                opp.raw_evidence = ev
                # Surface freshly kept contracts at the top of "newest" sorts
                opp.updated_at = datetime.now(timezone.utc)
                if existed is None:
                    created += 1
                else:
                    updated += 1
                db.add(opp)
                db.commit()
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                msg = str(exc).replace("\n", " ").strip()
                if len(msg) > 180:
                    msg = msg[:177] + "..."
                errors.append(
                    f"{it.get('solicitation_number') or it.get('external_key')}:{type(exc).__name__}:{msg}"
                )
        return {
            "promoted": created + updated,
            "created": created,
            "updated": updated,
            "skipped_incomplete": skipped,
            "skip_reasons": skip_reasons[:30],
            "source": source_key,
            "errors": errors[:20],
        }
    finally:
        db.close()


def step6_keep_or_trash(
    *,
    kept_items: list[dict[str, Any]],
    trashed_items: list[dict[str, Any]],
    source_key: str = "nebraska_spb",
) -> dict[str, Any]:
    """Persist keep index; write trash ledger (no further work on trash)."""
    keep_idx = keep_root() / "by_source" / source_key / "KEEP_INDEX.json"
    trash_idx = trash_root() / f"{source_key}_TRASH.json"
    keep_rows: list[dict[str, Any]] = []
    for i in kept_items:
        enriched = apply_deadline_from_evidence(i)
        keep_rows.append(
            {
                "external_key": enriched.get("external_key"),
                "solicitation_number": enriched.get("solicitation_number"),
                "title": enriched.get("title"),
                "deadline_at": enriched.get("deadline_at"),
                "close_date": ((enriched.get("doc_read") or {}).get("final") or {}).get(
                    "close_date"
                )
                or (enriched.get("triage") or {}).get("close_date"),
                "posted_at": enriched.get("posted_at") or enriched.get("posted_date"),
                "official_url": enriched.get("official_url") or enriched.get("detail_url"),
                "want_type": (enriched.get("triage") or {}).get("want_type"),
                "what_they_want": (enriched.get("triage") or {}).get("what_they_want")
                or ((enriched.get("doc_read") or {}).get("final") or {}).get("what_they_want"),
                "docs": [
                    {"name": d.get("name"), "sha256": d.get("sha256"), "path": d.get("path")}
                    for d in real_attachment_docs(enriched.get("saved_documents") or [])
                ],
                "attachment_fingerprint": sorted(
                    real_attachment_hashes(enriched.get("saved_documents") or [])
                ),
                "doc_read_ok": (enriched.get("doc_read") or {}).get("ok"),
                "supplier_quote_ready": ((enriched.get("doc_read") or {}).get("final") or {}).get(
                    "supplier_quote_ready"
                ),
            }
        )
    keep_payload = {
        "updated_at": _utc(),
        "count": len(keep_rows),
        "items": keep_rows,
    }
    trash_payload = {
        "updated_at": _utc(),
        "count": len(trashed_items),
        "items": [
            {
                "external_key": t.get("external_key"),
                "solicitation_number": t.get("solicitation_number"),
                "title": t.get("title"),
                "trash_step": t.get("trash_step"),
                "trash_reason": t.get("trash_reason"),
            }
            for t in trashed_items
        ],
    }
    keep_idx.parent.mkdir(parents=True, exist_ok=True)
    keep_idx.write_text(json.dumps(keep_payload, indent=2, default=str), encoding="utf-8")
    trash_idx.write_text(json.dumps(trash_payload, indent=2, default=str), encoding="utf-8")
    ui = promote_kept_to_opportunities(kept_items, source_key=source_key)
    return {
        "step": 6,
        "name": "KEEP_OR_TRASH",
        "keep_index": str(keep_idx),
        "trash_index": str(trash_idx),
        "kept_count": len(kept_items),
        "trashed_count": len(trashed_items),
        "ui_promote": ui,
    }


def run_free_source_steps(
    source_key: str,
    *,
    limit: int | None = None,
    max_workers: int = 10,
    read_docs: bool = True,
    skip_unchanged_doc_read: bool = True,
) -> dict[str, Any]:
    """Full stepped run for a free portal — stop investing when it doesn't fit.

    Free sites have no pull credit limits: pass limit=None to take the entire
    public board. --limit is smoke-test only.
    """
    if source_key not in FREE_SOURCE_SPECS:
        raise ValueError(f"unsupported_free_source:{source_key}")
    os_env_setup()
    summary: dict[str, Any] = {
        "started_at": _utc(),
        "source": source_key,
        "limit": limit,
        "full_board": limit is None,
        "steps": [],
    }

    s1 = step1_list_source(source_key, limit=limit)
    summary["steps"].append({k: s1[k] for k in s1 if k != "items"})
    print(
        json.dumps(
            {
                "phase": "LIST",
                "source": source_key,
                "rows": s1.get("rows_examined"),
                "listed": len(s1.get("items") or []),
            }
        ),
        flush=True,
    )

    s2 = step2_regex_screen(s1.get("items") or [])
    summary["steps"].append({k: s2[k] for k in s2 if k not in {"kept", "trashed"}})
    print(json.dumps({"phase": "REGEX_SCREEN", "kept": s2["kept_count"], "trash": s2["trashed_count"]}), flush=True)

    all_trash: list[dict[str, Any]] = list(s2.get("trashed") or [])
    if not s2.get("kept"):
        s6 = step6_keep_or_trash(kept_items=[], trashed_items=all_trash, source_key=source_key)
        summary["steps"].append(s6)
        summary["finished_at"] = _utc()
        summary["kept_count"] = 0
        summary["trashed_count"] = len(all_trash)
        _write_summary(summary, source_key=source_key)
        return summary

    s3 = step3_body_triage(s2["kept"], source_key=source_key, max_workers=max_workers)
    summary["steps"].append({k: s3[k] for k in s3 if k not in {"kept", "trashed"}})
    all_trash.extend(s3.get("trashed") or [])
    print(
        json.dumps(
            {
                "phase": "BODY_TRIAGE",
                "kept": s3["kept_count"],
                "trash": s3["trashed_count"],
                "openai_usd": s3.get("openai_spent_usd"),
            }
        ),
        flush=True,
    )

    kept = s3.get("kept") or []
    if not kept:
        s6 = step6_keep_or_trash(kept_items=[], trashed_items=all_trash, source_key=source_key)
        summary["steps"].append(s6)
        summary["finished_at"] = _utc()
        summary["kept_count"] = 0
        summary["trashed_count"] = len(all_trash)
        _write_summary(summary, source_key=source_key)
        return summary

    s4 = step4_download_docs(kept, source_key=source_key, max_workers=max_workers)
    summary["steps"].append({k: s4[k] for k in s4 if k != "items"})
    print(json.dumps({"phase": "DOCS", "files": s4.get("doc_count"), "failed": s4.get("failed_count")}), flush=True)

    docs_items = s4.get("items") or []
    if read_docs and docs_items:
        s5 = step5_doc_read(
            docs_items,
            source_key=source_key,
            max_workers=max_workers,
            skip_unchanged=skip_unchanged_doc_read,
        )
        summary["steps"].append({k: s5[k] for k in s5 if k != "items"})
        docs_items = s5.get("items") or docs_items
        print(
            json.dumps(
                {
                    "phase": "DOC_READ",
                    "openai_usd": s5.get("openai_spent_usd"),
                    "reused_unchanged": s5.get("reused_unchanged"),
                }
            ),
            flush=True,
        )

    # Persist AI/listing close into deadline_at, then scrap past-deadline + non-product.
    still_kept: list[dict[str, Any]] = []
    demoted_after_docs = 0
    scrapped_past = 0
    for it in docs_items:
        it = apply_deadline_from_evidence(it)
        past = past_deadline_scrap_reason(it)
        if past:
            scrapped_past += 1
            all_trash.append({**it, "trash_reason": past, "trash_step": 5})
            continue
        reason = doc_read_not_product_reason(it)
        if reason:
            demoted_after_docs += 1
            all_trash.append({**it, "trash_reason": reason, "trash_step": 5})
        else:
            still_kept.append(it)
    docs_items = still_kept
    if demoted_after_docs or scrapped_past:
        print(
            json.dumps(
                {
                    "phase": "DOC_READ_DEMOTE_NON_PRODUCT",
                    "demoted": demoted_after_docs,
                    "scrapped_past_deadline": scrapped_past,
                    "still_kept": len(docs_items),
                }
            ),
            flush=True,
        )

    s6 = step6_keep_or_trash(kept_items=docs_items, trashed_items=all_trash, source_key=source_key)
    summary["steps"].append(s6)
    summary["kept"] = [
        {
            "solicitation_number": i.get("solicitation_number"),
            "title": i.get("title"),
            "what": (i.get("triage") or {}).get("what_they_want"),
            "docs": len(i.get("saved_documents") or []),
            "doc_read_ok": (i.get("doc_read") or {}).get("ok"),
        }
        for i in docs_items
    ]
    summary["trashed_sample"] = [
        {"title": t.get("title"), "step": t.get("trash_step"), "reason": t.get("trash_reason")}
        for t in all_trash[:20]
    ]
    summary["kept_count"] = len(docs_items)
    summary["trashed_count"] = len(all_trash)
    summary["finished_at"] = _utc()
    _write_summary(summary, source_key=source_key)
    return summary


def run_nebraska_spb_steps(
    *,
    limit: int | None = None,
    max_workers: int = 10,
    read_docs: bool = True,
) -> dict[str, Any]:
    return run_free_source_steps(
        "nebraska_spb",
        limit=limit,
        max_workers=max_workers,
        read_docs=read_docs,
    )


def run_nj_purchase_steps(
    *,
    limit: int | None = None,
    max_workers: int = 10,
    read_docs: bool = True,
) -> dict[str, Any]:
    return run_free_source_steps(
        "nj_purchase",
        limit=limit,
        max_workers=max_workers,
        read_docs=read_docs,
    )


def run_tn_cpo_steps(
    *,
    limit: int | None = None,
    max_workers: int = 10,
    read_docs: bool = True,
) -> dict[str, Any]:
    return run_free_source_steps(
        "tn_cpo",
        limit=limit,
        max_workers=max_workers,
        read_docs=read_docs,
    )


def run_uw_procurement_steps(
    *,
    limit: int | None = None,
    max_workers: int = 10,
    read_docs: bool = True,
) -> dict[str, Any]:
    return run_free_source_steps(
        "uw_procurement",
        limit=limit,
        max_workers=max_workers,
        read_docs=read_docs,
    )


def run_delaware_open_bids_steps(
    *,
    limit: int | None = None,
    max_workers: int = 10,
    read_docs: bool = True,
) -> dict[str, Any]:
    return run_free_source_steps(
        "delaware_open_bids",
        limit=limit,
        max_workers=max_workers,
        read_docs=read_docs,
    )


def run_all_free_sources_daily(
    *,
    max_workers: int = 10,
    source_workers: int = 5,
    read_docs: bool = True,
    sources: list[str] | None = None,
    max_source_attempts: int = 3,
    resume_failed_only: bool = False,
) -> dict[str, Any]:
    """Once-a-day full pull: every wired free portal, entire board, no listing cap.

    HTTP/docs are unlimited on these sites. OpenAI every-word runs for PRODUCT
    fits, but skips packages whose doc hashes already match a prior successful read.

    Portals run concurrently up to ``source_workers`` (default 5). Within each
    portal, triage/docs still use ``max_workers``.

    If a portal dies, it is retried automatically (up to ``max_source_attempts``).
    ``resume_failed_only`` re-runs only sources that failed in DAILY_FULL_BOARD_LAST.
    """
    os_env_setup()
    last_path = keep_root() / "DAILY_FULL_BOARD_LAST.json"
    if resume_failed_only and sources is None and last_path.exists():
        try:
            prior = json.loads(last_path.read_text(encoding="utf-8"))
            sources = [
                str(r.get("source"))
                for r in (prior.get("results") or [])
                if not r.get("ok") and r.get("source")
            ]
        except Exception:
            sources = None
    keys = list(sources or FREE_SOURCE_SPECS.keys())
    attempts = max(1, int(max_source_attempts))
    summary: dict[str, Any] = {
        "started_at": _utc(),
        "mode": "daily_full_board_resume" if resume_failed_only else "daily_full_board",
        "limit": None,
        "sources": keys,
        "source_workers": max(1, int(source_workers)),
        "max_source_attempts": attempts,
        "results": [],
        "retries": [],
    }
    print(
        json.dumps(
            {
                "phase": "DAILY_START",
                "sources": keys,
                "full_board": True,
                "source_workers": summary["source_workers"],
                "max_source_attempts": attempts,
                "resume_failed_only": bool(resume_failed_only),
            }
        ),
        flush=True,
    )

    def _one_source(key: str) -> dict[str, Any]:
        if key not in FREE_SOURCE_SPECS:
            return {"source": key, "ok": False, "error": "unsupported"}
        last_err = ""
        for attempt in range(1, attempts + 1):
            print(
                json.dumps(
                    {
                        "phase": "DAILY_SOURCE",
                        "source": key,
                        "attempt": attempt,
                        "max_attempts": attempts,
                    }
                ),
                flush=True,
            )
            try:
                result = run_free_source_steps(
                    key,
                    limit=None,
                    max_workers=max_workers,
                    read_docs=read_docs,
                    skip_unchanged_doc_read=True,
                )
                return {
                    "source": key,
                    "ok": True,
                    "attempt": attempt,
                    "listed": next(
                        (
                            s.get("rows_examined")
                            for s in (result.get("steps") or [])
                            if s.get("name") == "LIST"
                        ),
                        None,
                    ),
                    "kept_count": result.get("kept_count"),
                    "trashed_count": result.get("trashed_count"),
                    "kept": result.get("kept") or [],
                    "ui_promote": next(
                        (
                            s.get("ui_promote")
                            for s in (result.get("steps") or [])
                            if s.get("name") == "KEEP_OR_TRASH"
                        ),
                        None,
                    ),
                }
            except Exception as exc:  # noqa: BLE001
                last_err = f"{type(exc).__name__}:{exc}"[:240]
                print(
                    json.dumps(
                        {
                            "phase": "DAILY_SOURCE_RETRY",
                            "source": key,
                            "attempt": attempt,
                            "error": last_err,
                            "will_retry": attempt < attempts,
                        }
                    ),
                    flush=True,
                )
                summary["retries"].append(
                    {"source": key, "attempt": attempt, "error": last_err}
                )
        return {"source": key, "ok": False, "error": last_err, "attempts": attempts}

    # Preserve source order in the summary for readable reports.
    summary["results"] = map_parallel(
        _one_source,
        keys,
        max_workers=max(1, int(source_workers)),
        ordered=True,
    )

    # Final automatic resume pass for anything still failed after per-source retries.
    still_failed = [str(r.get("source")) for r in summary["results"] if not r.get("ok") and r.get("source")]
    if still_failed and not resume_failed_only:
        print(
            json.dumps({"phase": "DAILY_AUTO_RESUME", "sources": still_failed}),
            flush=True,
        )
        resume_results = map_parallel(
            _one_source,
            still_failed,
            max_workers=min(2, max(1, int(source_workers))),
            ordered=True,
        )
        by_key = {str(r.get("source")): r for r in resume_results}
        summary["results"] = [
            by_key.get(str(r.get("source")), r) if not r.get("ok") else r
            for r in summary["results"]
        ]
        summary["auto_resume"] = {
            "sources": still_failed,
            "recovered": [k for k, v in by_key.items() if v.get("ok")],
            "still_failed": [k for k, v in by_key.items() if not v.get("ok")],
        }

    summary["finished_at"] = _utc()
    summary["kept_total"] = sum(int(r.get("kept_count") or 0) for r in summary["results"] if r.get("ok"))
    summary["trashed_total"] = sum(
        int(r.get("trashed_count") or 0) for r in summary["results"] if r.get("ok")
    )
    summary["failed_sources"] = [
        {"source": r.get("source"), "error": r.get("error")}
        for r in summary["results"]
        if not r.get("ok")
    ]
    out = keep_root() / "DAILY_FULL_BOARD_LAST.json"
    out.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    summary["report"] = str(out)
    print(
        json.dumps(
            {
                "phase": "DAILY_DONE",
                "kept_total": summary["kept_total"],
                "trashed_total": summary["trashed_total"],
                "failed_sources": [f.get("source") for f in summary["failed_sources"]],
                "report": summary["report"],
            }
        ),
        flush=True,
    )
    return summary


def os_env_setup() -> None:
    import os

    os.environ.setdefault("OPENAI_ENABLED", "true")
    os.environ.setdefault("PAID_RESEARCH_ENABLED", "true")
    # m3/research/*.py → parents[2] = govtracker
    env = Path(__file__).resolve().parents[2] / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    get_settings.cache_clear()


def _write_summary(summary: dict[str, Any], *, source_key: str = "nebraska_spb") -> None:
    out = keep_root() / "by_source" / source_key / "LAST_STEP_RUN.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    print(
        json.dumps(
            {k: summary[k] for k in summary if k not in {"kept", "trashed_sample", "steps"}},
            indent=2,
        ),
        flush=True,
    )
