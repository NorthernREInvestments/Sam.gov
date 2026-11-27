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


def step2_regex_screen(items: list[dict[str, Any]]) -> dict[str, Any]:
    keep: list[dict[str, Any]] = []
    trash: list[dict[str, Any]] = []
    for it in items:
        title = str(it.get("title") or "")
        reason = listing_skip_before_detail_reason(title)
        cat = str(it.get("nigp_category") or "")
        if reason:
            trash.append({**it, "trash_reason": reason, "trash_step": 2})
            continue
        if re.search(
            r"(?i)\b(case\s+management|consulting|staffing|construction|renovation)\b",
            f"{title} {cat}",
        ):
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
        url = str(it.get("official_url") or "")
        # Notice-file portals (NJ/TN/UW): the listing link IS the solicitation document.
        path_low = url.lower().split("?", 1)[0]
        if detail_mode == "pdf_notice" or path_low.endswith((".pdf", ".doc", ".docx")):
            import time

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
            if final_path.endswith(".docx") or data[:2] == b"PK":
                extracted = extract_docx_text(data)
            else:
                extracted = extract_pdf_text(data, allow_ocr=False)
            text = str(extracted.get("text") or "")
            if len(text) < 40:
                text = f"{it.get('title') or ''}\n{it.get('summary') or ''}".strip()
            existing = list(it.get("document_urls") or [])
            if final or url:
                existing = list(dict.fromkeys([*(existing or []), final or url]))
            if source_key in _PDF_NOTICE_SOURCES:
                docs = discover(final or url, "", extra_urls=existing)
                doc_urls = [d["source_url"] for d in docs]
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
    triage_inputs = [
        {"nb_id": str(p["item"].get("external_key") or p["item"].get("solicitation_number")), "opportunity": p["opportunity"]}
        for p in prepared
        if p.get("opportunity")
    ]
    triaged = (
        triage_opportunities_parallel(triage_inputs, max_workers=max_workers) if triage_inputs else []
    )
    by_id = {str(t.get("nb_id")): t for t in triaged}
    kept: list[dict[str, Any]] = []
    trashed: list[dict[str, Any]] = []
    for p in prepared:
        it = p["item"]
        eid = str(it.get("external_key") or it.get("solicitation_number"))
        triage = p.get("triage") or by_id.get(eid)
        if not triage:
            trashed.append({**it, "trash_reason": "missing_triage", "trash_step": 3})
            continue
        gate = authorize_document_fetch(triage, opportunity=p.get("opportunity"))
        row = {
            **it,
            "triage": triage,
            "docs_fetch_gate": gate,
            "detail_url": p.get("detail_url"),
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
                # Indiana-style ZIP packages: permanently keep archive + extract members.
                if (name.lower().endswith(".zip") or data[:2] == b"PK") and data[:2] == b"PK":
                    import zipfile

                    extract_dir = dest_dir / f"{Path(name).stem}_extracted"
                    extract_dir.mkdir(parents=True, exist_ok=True)
                    try:
                        with zipfile.ZipFile(io.BytesIO(data)) as zf:
                            for info in zf.infolist():
                                if info.is_dir():
                                    continue
                                member_name = Path(info.filename).name
                                if not member_name or member_name.startswith("."):
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
        return {str(d.get("sha256") or "") for d in (item.get("docs") or []) if d.get("sha256")}
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

    def _one(it: dict[str, Any]) -> dict[str, Any]:
        sol = str(it.get("solicitation_number") or it.get("external_key") or "unknown")
        nb_key = re.sub(r"[^\w\-]+", "_", sol)
        originals_parent = keep_root() / "by_source" / source_key
        current_hashes = {
            str(d.get("sha256") or "")
            for d in (it.get("saved_documents") or [])
            if d.get("sha256")
        }
        if skip_unchanged and current_hashes:
            prior = _prior_doc_fingerprint(source_key, sol)
            if prior and prior == current_hashes:
                ai_path = (
                    get_settings().data_root()
                    / "ai_free_source_doc_read"
                    / source_key
                    / f"{nb_key}.json"
                )
                cached = None
                if ai_path.exists():
                    try:
                        cached = json.loads(ai_path.read_text(encoding="utf-8"))
                    except Exception:
                        cached = None
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
            max_workers=max_workers,
        )
        ai_dir = get_settings().data_root() / "ai_free_source_doc_read" / source_key
        ai_dir.mkdir(parents=True, exist_ok=True)
        (ai_dir / f"{nb_key}.json").write_text(json.dumps(rec, indent=2, default=str), encoding="utf-8")
        return {**it, "doc_read": rec, "doc_read_reused": False}

    workers = min(3, max(1, len(items)))
    out = map_parallel(_one, items, max_workers=workers, ordered=True) if items else []
    return {
        "step": 5,
        "name": "DOC_READ",
        "items": out,
        "openai_spent_usd": sum(float((i.get("doc_read") or {}).get("openai_spent_usd") or 0) for i in out),
        "reused_unchanged": sum(1 for i in out if i.get("doc_read_reused")),
    }


def step6_keep_or_trash(
    *,
    kept_items: list[dict[str, Any]],
    trashed_items: list[dict[str, Any]],
    source_key: str = "nebraska_spb",
) -> dict[str, Any]:
    """Persist keep index; write trash ledger (no further work on trash)."""
    keep_idx = keep_root() / "by_source" / source_key / "KEEP_INDEX.json"
    trash_idx = trash_root() / f"{source_key}_TRASH.json"
    keep_payload = {
        "updated_at": _utc(),
        "count": len(kept_items),
        "items": [
            {
                "external_key": i.get("external_key"),
                "solicitation_number": i.get("solicitation_number"),
                "title": i.get("title"),
                "deadline_at": i.get("deadline_at"),
                "official_url": i.get("official_url") or i.get("detail_url"),
                "want_type": (i.get("triage") or {}).get("want_type"),
                "what_they_want": (i.get("triage") or {}).get("what_they_want"),
                "docs": [
                    {"name": d.get("name"), "sha256": d.get("sha256"), "path": d.get("path")}
                    for d in (i.get("saved_documents") or [])
                ],
                "doc_read_ok": (i.get("doc_read") or {}).get("ok"),
                "supplier_quote_ready": ((i.get("doc_read") or {}).get("final") or {}).get(
                    "supplier_quote_ready"
                ),
            }
            for i in kept_items
        ],
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
    return {
        "step": 6,
        "name": "KEEP_OR_TRASH",
        "keep_index": str(keep_idx),
        "trash_index": str(trash_idx),
        "kept_count": len(kept_items),
        "trashed_count": len(trashed_items),
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
    read_docs: bool = True,
    sources: list[str] | None = None,
) -> dict[str, Any]:
    """Once-a-day full pull: every wired free portal, entire board, no listing cap.

    HTTP/docs are unlimited on these sites. OpenAI every-word runs for PRODUCT
    fits, but skips packages whose doc hashes already match a prior successful read.
    """
    os_env_setup()
    keys = list(sources or FREE_SOURCE_SPECS.keys())
    summary: dict[str, Any] = {
        "started_at": _utc(),
        "mode": "daily_full_board",
        "limit": None,
        "sources": keys,
        "results": [],
    }
    print(
        json.dumps({"phase": "DAILY_START", "sources": keys, "full_board": True}),
        flush=True,
    )
    for key in keys:
        if key not in FREE_SOURCE_SPECS:
            summary["results"].append({"source": key, "ok": False, "error": "unsupported"})
            continue
        print(json.dumps({"phase": "DAILY_SOURCE", "source": key}), flush=True)
        try:
            result = run_free_source_steps(
                key,
                limit=None,
                max_workers=max_workers,
                read_docs=read_docs,
                skip_unchanged_doc_read=True,
            )
            summary["results"].append(
                {
                    "source": key,
                    "ok": True,
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
                }
            )
        except Exception as exc:  # noqa: BLE001
            summary["results"].append(
                {"source": key, "ok": False, "error": f"{type(exc).__name__}:{exc}"[:240]}
            )
    summary["finished_at"] = _utc()
    summary["kept_total"] = sum(int(r.get("kept_count") or 0) for r in summary["results"] if r.get("ok"))
    summary["trashed_total"] = sum(
        int(r.get("trashed_count") or 0) for r in summary["results"] if r.get("ok")
    )
    out = keep_root() / "DAILY_FULL_BOARD_LAST.json"
    out.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    summary["report"] = str(out)
    print(
        json.dumps(
            {
                "phase": "DAILY_DONE",
                "kept_total": summary["kept_total"],
                "trashed_total": summary["trashed_total"],
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
