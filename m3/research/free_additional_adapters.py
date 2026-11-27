"""Additional free public portals (batch after municipals).

Providence RI, San Juan USD, Pocatello SD25, Danville VA, Papillion PLCS,
Ocean View SD, MSU Procurement, Indiana IDOA ZIP packages,
Pennsylvania eMarketplace, Louisiana LaPAC.

Delaware MMP already wired via adapters.search_delaware_mmp.
NC EVP held — PowerApps shell; no reliable public attachment enumeration.
"""
from __future__ import annotations

import hashlib
import io
import re
import zipfile
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

from m3.research.free_municipal_adapters import (
    _docs_from_urls,
    _guess_category,
    _is_doc,
    _item_base,
    _serviceish,
    _strip,
)
from m3.research.http_util import fetch_bytes, fetch_text


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _key(*parts: str) -> str:
    raw = "|".join(p.strip().lower() for p in parts if p)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


# --- Providence RI ------------------------------------------------------------------

PROVIDENCE_LIST_URL = "https://www.providenceri.gov/purchasing/openrfpsummary/"


def parse_providence_listing_html(
    html: str, *, list_url: str = PROVIDENCE_LIST_URL
) -> list[dict[str, Any]]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "html.parser")
    # Group by MT number when present, else by normalized title stem
    groups: dict[str, dict[str, Any]] = {}
    for a in soup.find_all("a", href=True):
        abs_u = urljoin(list_url, a["href"])
        if "wp-content/uploads" not in abs_u.lower() or not abs_u.lower().endswith(".pdf"):
            continue
        lab = _strip(a.get_text(" ", strip=True))
        name = unquote(urlparse(abs_u).path.split("/")[-1])
        mt = re.search(r"(?i)\bMT[-_ ]?(\d{4,6})\b", f"{lab} {name}")
        sol = f"MT-{mt.group(1)}" if mt else ""
        title = lab or name
        is_add = bool(re.search(r"(?i)addendum", f"{lab} {name}"))
        # Prefer MT id; else strip addendum suffix for grouping
        key = sol or re.sub(r"(?i)\s*[-–]?\s*addendum.*$", "", title).strip().lower()[:80]
        entry = groups.setdefault(
            key,
            {
                "solicitation_number": sol or key[:48],
                "title": re.sub(r"(?i)\s*addendum.*$", "", title).strip() or title,
                "document_urls": [],
                "detail_url": abs_u,
            },
        )
        if not is_add and len(title) > len(entry["title"]):
            entry["title"] = title
        entry["document_urls"].append(abs_u)
        if not is_add and not entry.get("_primary_set"):
            entry["detail_url"] = abs_u
            entry["_primary_set"] = True
    rows = []
    for entry in groups.values():
        entry.pop("_primary_set", None)
        entry["document_urls"] = list(dict.fromkeys(entry["document_urls"]))
        rows.append(entry)
    return rows


def search_providence_ri(
    source: dict, *, limit: int | None = None, fetch_detail_docs: bool = True
) -> dict[str, Any]:
    cfg = source.get("config") or {}
    list_url = str(cfg.get("list_url") or PROVIDENCE_LIST_URL)
    try:
        status, final, html = fetch_text(list_url, timeout=40)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "items": [], "source_key": source.get("source_key"), "errors": [str(exc)], "stats": {}}
    if status >= 400 or not html:
        return {"ok": False, "items": [], "source_key": source.get("source_key"), "errors": [f"status={status}"], "stats": {}}
    rows = parse_providence_listing_html(html, list_url=final or list_url)
    items: list[dict] = []
    skipped = 0
    for row in rows:
        if limit is not None and len(items) >= int(limit):
            break
        title = row["title"]
        if _serviceish(title) or re.search(
            r"(?i)\b(roof\s+replacement|general\s+contractor|technical\s+assistance|"
            r"data\s+integration|dental\s+lab)\b",
            title,
        ):
            skipped += 1
            continue
        items.append(
            _item_base(
                source_key=source["source_key"],
                title=title,
                sol=row["solicitation_number"],
                buyer="City of Providence Purchasing",
                state="RI",
                official_url=row["detail_url"],
                discovery_url=final or list_url,
                doc_urls=row["document_urls"],
                detail_url=row["detail_url"],
                tz="America/New_York",
            )
        )
    return {
        "ok": bool(items) or bool(rows),
        "items": items,
        "source_key": source.get("source_key"),
        "errors": [],
        "verified_at": _utc(),
        "pages_fetched": 1,
        "rows_examined": len(rows),
        "pagination_complete": True,
        "more_available": False,
        "stats": {"accepted": len(items), "skipped": skipped, "listing_rows": len(rows)},
    }


def discover_providence_ri_documents(
    detail_url: str, html: str = "", *, extra_urls: list[str] | None = None
) -> list[dict[str, Any]]:
    return _docs_from_urls([detail_url, *(extra_urls or [])], platform="providence_ri")


# --- San Juan USD CA ----------------------------------------------------------------

SANJUAN_LIST_URL = (
    "https://www.sanjuan.edu/our-district/contracts-and-bids/"
    "other-services-contracts-and-bids/bidrfp-files"
)


def parse_sanjuan_listing_html(
    html: str, *, list_url: str = SANJUAN_LIST_URL
) -> list[dict[str, Any]]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "html.parser")
    groups: dict[str, dict[str, Any]] = {}
    for a in soup.find_all("a", href=True):
        abs_u = urljoin(list_url, a["href"])
        lab = _strip(a.get_text(" ", strip=True))
        if "resource-manager/view" not in abs_u and "finalsite.net" not in abs_u.lower():
            continue
        if not lab or lab.lower() in {"powered by finalsite"}:
            continue
        parent = a.find_parent(["li", "p", "div", "section", "article"])
        ctx = _strip(parent.get_text(" ", strip=True)) if parent else lab
        num_m = re.search(r"(?i)\b(\d{2}-\d{3})\b", f"{lab} {ctx}")
        if not num_m:
            continue
        num = num_m.group(1)
        prefix = "BID" if re.search(r"(?i)\bbid\b", f"{lab} {ctx}") and not re.search(r"(?i)rfp", f"{lab} {ctx}") else "RFP"
        sol = f"{prefix} {num}"
        key = num
        title_m = re.search(rf"(?i)(?:RFP|BID|RFQ)\s*#?\s*{re.escape(num)}\s*[-–:]?\s*(.+)", ctx)
        title = (title_m.group(1) if title_m else lab)[:300]
        title = re.split(
            r"(?i)\b(?:proposal|pricing\s+sheet|questions?\s+and\s+answers?|exhibit|erate)\b",
            title,
        )[0].strip(" -–") or lab
        entry = groups.setdefault(
            key,
            {
                "solicitation_number": sol,
                "title": title,
                "document_urls": [],
                "detail_url": abs_u,
                "erate": bool(re.search(r"(?i)e-?rate", ctx)),
            },
        )
        entry["document_urls"].append(abs_u)
        entry["erate"] = entry["erate"] or bool(re.search(r"(?i)e-?rate", f"{lab} {ctx}"))
        if re.search(r"(?i)(proposal|invitation|solicitation)", lab) and not re.search(
            r"(?i)(pricing|question|answer|exhibit)", lab
        ):
            entry["detail_url"] = abs_u
            if title:
                entry["title"] = title
    rows = []
    for e in groups.values():
        e["document_urls"] = list(dict.fromkeys(e["document_urls"]))
        rows.append(e)
    return rows


def search_sanjuan_usd(
    source: dict, *, limit: int | None = None, fetch_detail_docs: bool = True
) -> dict[str, Any]:
    cfg = source.get("config") or {}
    list_url = str(cfg.get("list_url") or SANJUAN_LIST_URL)
    try:
        status, final, html = fetch_text(list_url, timeout=45)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "items": [], "source_key": source.get("source_key"), "errors": [str(exc)], "stats": {}}
    if status >= 400 or not html:
        return {"ok": False, "items": [], "source_key": source.get("source_key"), "errors": [f"status={status}"], "stats": {}}
    rows = parse_sanjuan_listing_html(html, list_url=final or list_url)
    items: list[dict] = []
    skipped = 0
    for row in rows:
        if limit is not None and len(items) >= int(limit):
            break
        title = row["title"]
        if re.search(r"(?i)\b(internet\s+service|ethernet\s+service)\b", title):
            skipped += 1
            continue
        if _serviceish(title) and not re.search(
            r"(?i)\b(equipment|switch|firewall|vehicle|appliance|supplies)\b", title
        ):
            skipped += 1
            continue
        item = _item_base(
            source_key=source["source_key"],
            title=title,
            sol=row["solicitation_number"],
            buyer="San Juan Unified School District",
            state="CA",
            official_url=row["detail_url"],
            discovery_url=final or list_url,
            doc_urls=row["document_urls"],
            detail_url=row["detail_url"],
            tz="America/Los_Angeles",
        )
        item["evidence"] = {
            **(item.get("evidence") or {}),
            "erate_funded": bool(row.get("erate")),
            "verify_vendor_eligibility_if_erate": bool(row.get("erate")),
        }
        items.append(item)
    return {
        "ok": bool(items) or bool(rows),
        "items": items,
        "source_key": source.get("source_key"),
        "errors": [],
        "verified_at": _utc(),
        "pages_fetched": 1,
        "rows_examined": len(rows),
        "pagination_complete": True,
        "more_available": False,
        "stats": {"accepted": len(items), "skipped": skipped, "listing_rows": len(rows)},
    }


def discover_sanjuan_usd_documents(
    detail_url: str, html: str = "", *, extra_urls: list[str] | None = None
) -> list[dict[str, Any]]:
    return _docs_from_urls([detail_url, *(extra_urls or [])], platform="sanjuan_usd")


# --- Pocatello / Chubbuck SD25 ------------------------------------------------------

POCATELLO_LIST_URL = "https://www.sd25.us/departments/business-office"


def parse_pocatello_listing_html(
    html: str, *, list_url: str = POCATELLO_LIST_URL
) -> list[dict[str, Any]]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "html.parser")
    groups: dict[str, dict[str, Any]] = {}
    for a in soup.find_all("a", href=True):
        abs_u = urljoin(list_url, a["href"])
        lab = _strip(a.get_text(" ", strip=True))
        if "finalsite.net" not in abs_u.lower() and "resource-manager" not in abs_u:
            continue
        if not re.search(
            r"(?i)(invitation|addendum|request\s+for\s+proposal|rfp|rfq|custodial|food\s+items|"
            r"board\s+recommend|intercom|camera|supplies)",
            f"{lab} {abs_u}",
        ):
            continue
        if re.search(r"(?i)(budget|combined\s+rev|annual\s+budget)", lab):
            continue
        # Group key from title stem
        stem = re.sub(r"(?i)\b(addendum\s*\d+|board\s+recommend\s*-?\s*|re-?bid)\b", "", lab)
        stem = re.sub(r"\s+", " ", stem).strip(" ,-") or lab
        key = stem.lower()[:70]
        # Group by commodity + year when present (avoid merging 2017 with 2026)
        gkey = key
        year_m = re.search(r"(20\d{2})", f"{lab} {stem}")
        year = year_m.group(1) if year_m else ""
        for token in ("custodial supplies", "food items", "security cameras", "intercoms"):
            if token in f"{lab} {stem}".lower():
                gkey = f"{year}:{token}" if year else token
                break
        entry = groups.setdefault(
            gkey,
            {
                "solicitation_number": re.sub(r"[^\w\-]+", "_", stem)[:48],
                "title": stem,
                "document_urls": [],
                "detail_url": abs_u,
            },
        )
        entry["document_urls"].append(abs_u)
        if re.search(r"(?i)invitation|request\s+for\s+proposal|rfp", lab) and not re.search(
            r"(?i)addendum|board\s+recommend", lab
        ):
            entry["detail_url"] = abs_u
            entry["title"] = stem
            entry["solicitation_number"] = re.sub(r"[^\w\-]+", "_", stem)[:48]
    rows = []
    for e in groups.values():
        e["document_urls"] = list(dict.fromkeys(e["document_urls"]))
        rows.append(e)
    return rows


def apply_quantity_amendment_overrides(
    *,
    item_number: str,
    original_qty: float,
    amendment_text: str,
) -> dict[str, Any]:
    """Accuracy helper: amended quantities override originals (Pocatello YY0001 case)."""
    item = (item_number or "").strip().upper()
    text = amendment_text or ""
    qty = None
    # Prefer explicit change language: "changed from 20,000 to 40" near item
    change = re.search(
        rf"(?is){re.escape(item)}.{{0,400}}?\b(?:changed|amended|revised|corrected)\b.{{0,80}}?\bto\s*([\d,]+)",
        text,
    )
    if change:
        try:
            qty = float(change.group(1).replace(",", ""))
        except ValueError:
            qty = None
    if qty is None:
        # "Quantity: 40" after the item id
        pat = re.compile(
            rf"(?is){re.escape(item)}.{{0,400}}?(?:quantity|qty)\s*[:=]\s*([\d,]+)",
            re.I,
        )
        m = pat.search(text)
        if m:
            try:
                qty = float(m.group(1).replace(",", ""))
            except ValueError:
                qty = None
    out = {
        "item_number": item,
        "original_quantity": original_qty,
        "amended_quantity": qty,
        "effective_quantity": qty if qty is not None else original_qty,
        "amendment_applied": qty is not None and qty != original_qty,
    }
    return out


def search_pocatello_sd25(
    source: dict, *, limit: int | None = None, fetch_detail_docs: bool = True
) -> dict[str, Any]:
    cfg = source.get("config") or {}
    list_url = str(cfg.get("list_url") or POCATELLO_LIST_URL)
    try:
        status, final, html = fetch_text(list_url, timeout=50)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "items": [], "source_key": source.get("source_key"), "errors": [str(exc)], "stats": {}}
    if status >= 400 or not html:
        return {"ok": False, "items": [], "source_key": source.get("source_key"), "errors": [f"status={status}"], "stats": {}}
    rows = parse_pocatello_listing_html(html, list_url=final or list_url)
    items: list[dict] = []
    skipped = 0
    for row in rows:
        if limit is not None and len(items) >= int(limit):
            break
        title = row["title"]
        if re.search(r"(?i)\b(parking\s+lot|tennis\s+court|rebuild|stairs|press\s+box)\b", title):
            skipped += 1
            continue
        if _serviceish(title) and not re.search(
            r"(?i)\b(supplies|food|equipment|camera|intercom)\b", title
        ):
            skipped += 1
            continue
        item = _item_base(
            source_key=source["source_key"],
            title=title,
            sol=row["solicitation_number"],
            buyer="Pocatello/Chubbuck School District 25",
            state="ID",
            official_url=row["detail_url"],
            discovery_url=final or list_url,
            doc_urls=row["document_urls"],
            detail_url=row["detail_url"],
            tz="America/Boise",
        )
        item["evidence"] = {
            **(item.get("evidence") or {}),
            "apply_amendments_before_qty": True,
            "quantity_amendment_accuracy_fixture": bool(
                re.search(r"(?i)custodial\s+supplies", title)
            ),
            "verify_deadlines_independently": True,
        }
        items.append(item)
    return {
        "ok": bool(items) or bool(rows),
        "items": items,
        "source_key": source.get("source_key"),
        "errors": [],
        "verified_at": _utc(),
        "pages_fetched": 1,
        "rows_examined": len(rows),
        "pagination_complete": True,
        "more_available": False,
        "stats": {"accepted": len(items), "skipped": skipped, "listing_rows": len(rows)},
    }


def discover_pocatello_sd25_documents(
    detail_url: str, html: str = "", *, extra_urls: list[str] | None = None
) -> list[dict[str, Any]]:
    return _docs_from_urls([detail_url, *(extra_urls or [])], platform="pocatello_sd25")


# --- Danville VA --------------------------------------------------------------------

DANVILLE_OPEN_URL = "https://www.danvilleva.gov/Bids.aspx?CatID=16&Status=Open&txtSort=Date"
DANVILLE_RFQ_URL = (
    "https://www.danvilleva.gov/Bids.aspx?CatID=26&Status=&showAllBids=on&txtSort=BidNumberDesc"
)


def parse_danville_listing_html(
    html: str, *, list_url: str
) -> list[dict[str, Any]]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "html.parser")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "bidID=" not in href and "bidId=" not in href:
            continue
        lab = _strip(a.get_text(" ", strip=True))
        if lab.lower().startswith("read on"):
            continue
        abs_u = urljoin(list_url, href)
        if abs_u in seen:
            continue
        seen.add(abs_u)
        sol_m = re.search(
            r"(?i)\b((?:IFB|RFP|RFQ|QB)[-_ ]?[A-Z0-9][A-Z0-9\-_/]*)", lab
        )
        sol = sol_m.group(1).replace(" ", "") if sol_m else lab.split()[0] if lab else ""
        rows.append(
            {
                "solicitation_number": sol[:64],
                "title": lab,
                "detail_url": abs_u,
                "document_urls": [],
            }
        )
    return rows


def _danville_detail_docs(detail_url: str) -> list[str]:
    from bs4 import BeautifulSoup

    try:
        st, fin, html = fetch_text(detail_url, timeout=35)
    except Exception:
        return []
    if st >= 400 or not html:
        return []
    docs = []
    soup = BeautifulSoup(html or "", "html.parser")
    for a in soup.find_all("a", href=True):
        abs_u = urljoin(fin or detail_url, a["href"])
        if "documentcenter/view" in abs_u.lower() or _is_doc(abs_u):
            docs.append(abs_u)
    return list(dict.fromkeys(docs))


def search_danville_va(
    source: dict, *, limit: int | None = None, fetch_detail_docs: bool = True
) -> dict[str, Any]:
    cfg = source.get("config") or {}
    list_urls = list(
        cfg.get("list_urls")
        or [
            str(cfg.get("list_url") or DANVILLE_OPEN_URL),
            DANVILLE_RFQ_URL,
        ]
    )
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    pages = 0
    for list_url in list_urls:
        try:
            status, final, html = fetch_text(list_url, timeout=45)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{list_url}:{type(exc).__name__}")
            continue
        pages += 1
        if status >= 400 or not html:
            errors.append(f"{list_url}:status={status}")
            continue
        rows.extend(parse_danville_listing_html(html, list_url=final or list_url))
    # Dedupe by detail URL
    by_u: dict[str, dict] = {}
    for r in rows:
        by_u[r["detail_url"]] = r
    rows = list(by_u.values())
    items: list[dict] = []
    skipped = 0
    for row in rows:
        if limit is not None and len(items) >= int(limit):
            break
        title = row["title"]
        if _serviceish(title) or re.search(
            r"(?i)\b(resurfacing|rehabilitation|vegetation|pest\s+control|"
            r"enterprise\s+technology|management\s+solution|contractor)\b",
            title,
        ):
            skipped += 1
            continue
        # Prefer tangible utility/industrial product language; still allow unknowns for triage
        docs: list[str] = []
        if fetch_detail_docs:
            docs = _danville_detail_docs(row["detail_url"])
            if not docs:
                skipped += 1
                continue
        item = _item_base(
            source_key=source["source_key"],
            title=title,
            sol=row["solicitation_number"],
            buyer="City of Danville VA Purchasing",
            state="VA",
            official_url=(docs[0] if docs else row["detail_url"]),
            discovery_url=list_urls[0],
            doc_urls=docs,
            detail_url=row["detail_url"],
            tz="America/New_York",
        )
        item["evidence"] = {
            **(item.get("evidence") or {}),
            "ignore_open_label_if_deadline_passed": True,
            "distinguish_intent_to_award": True,
        }
        items.append(item)
    return {
        "ok": bool(items) or bool(rows),
        "items": items,
        "source_key": source.get("source_key"),
        "errors": errors,
        "verified_at": _utc(),
        "pages_fetched": pages,
        "rows_examined": len(rows),
        "pagination_complete": True,
        "more_available": False,
        "stats": {"accepted": len(items), "skipped": skipped, "listing_rows": len(rows)},
    }


def discover_danville_va_documents(
    detail_url: str, html: str = "", *, extra_urls: list[str] | None = None
) -> list[dict[str, Any]]:
    urls = list(extra_urls or [])
    if detail_url and ("bidID=" in detail_url or "bidId=" in detail_url):
        urls.extend(_danville_detail_docs(detail_url))
    elif detail_url:
        urls.append(detail_url)
    return _docs_from_urls(urls, platform="danville_va")


# --- Papillion La Vista Community Schools -------------------------------------------

PAPILLION_LIST_URL = "https://www.plcschools.org/our-district/bids-rfps-and-rfis"


def parse_papillion_listing_html(
    html: str, *, list_url: str = PAPILLION_LIST_URL
) -> list[dict[str, Any]]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "html.parser")
    groups: dict[str, dict[str, Any]] = {}
    for a in soup.find_all("a", href=True):
        abs_u = urljoin(list_url, a["href"])
        lab = _strip(a.get_text(" ", strip=True))
        if "finalsite.net" not in abs_u.lower():
            continue
        if not re.search(r"(?i)(invitation|bid|workbook|worksheet|rfp|rfi|equipment|grocery|paper|milk|produce|bread|beverage|truck)", f"{lab} {abs_u}"):
            continue
        stem = re.sub(
            r"(?i)\b(bid\s+price\s+(workbook|worksheet)|attachment|due\s+\d.*|sy\s*\d{4}.*)$",
            "",
            lab,
        )
        stem = re.sub(r"\s+", " ", stem).strip(" ,-") or lab
        # Normalize equipment / grocery / milk families
        key = stem.lower()[:70]
        entry = groups.setdefault(
            key,
            {
                "solicitation_number": re.sub(r"[^\w\-]+", "_", stem)[:48],
                "title": stem,
                "document_urls": [],
                "detail_url": abs_u,
            },
        )
        entry["document_urls"].append(abs_u)
        if re.search(r"(?i)invitation|request\s+for", lab):
            entry["detail_url"] = abs_u
            entry["title"] = stem
    rows = []
    for e in groups.values():
        e["document_urls"] = list(dict.fromkeys(e["document_urls"]))
        rows.append(e)
    return rows


def search_papillion_plcs(
    source: dict, *, limit: int | None = None, fetch_detail_docs: bool = True
) -> dict[str, Any]:
    cfg = source.get("config") or {}
    list_url = str(cfg.get("list_url") or PAPILLION_LIST_URL)
    try:
        status, final, html = fetch_text(list_url, timeout=40)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "items": [], "source_key": source.get("source_key"), "errors": [str(exc)], "stats": {}}
    if status >= 400 or not html:
        return {"ok": False, "items": [], "source_key": source.get("source_key"), "errors": [f"status={status}"], "stats": {}}
    rows = parse_papillion_listing_html(html, list_url=final or list_url)
    items: list[dict] = []
    skipped = 0
    for row in rows:
        if limit is not None and len(items) >= int(limit):
            break
        title = row["title"]
        if re.search(r"(?i)\b(software\s+solution|rfi)\b", title):
            skipped += 1
            continue
        item = _item_base(
            source_key=source["source_key"],
            title=title,
            sol=row["solicitation_number"],
            buyer="Papillion La Vista Community Schools",
            state="NE",
            official_url=row["detail_url"],
            discovery_url=final or list_url,
            doc_urls=row["document_urls"],
            detail_url=row["detail_url"],
            tz="America/Chicago",
        )
        item["evidence"] = {
            **(item.get("evidence") or {}),
            "verify_deadlines_even_if_listed_open": True,
            "preserve_pricing_workbook_structure": True,
            "monitoring_priority": "lower",
        }
        items.append(item)
    return {
        "ok": bool(items) or bool(rows),
        "items": items,
        "source_key": source.get("source_key"),
        "errors": [],
        "verified_at": _utc(),
        "pages_fetched": 1,
        "rows_examined": len(rows),
        "pagination_complete": True,
        "more_available": False,
        "stats": {"accepted": len(items), "skipped": skipped, "listing_rows": len(rows)},
    }


def discover_papillion_plcs_documents(
    detail_url: str, html: str = "", *, extra_urls: list[str] | None = None
) -> list[dict[str, Any]]:
    return _docs_from_urls([detail_url, *(extra_urls or [])], platform="papillion_plcs")


# --- Ocean View SD CA ---------------------------------------------------------------

OCEANVIEW_LIST_URL = "https://www.ovsd.org/departments/purchasing"


def parse_oceanview_listing_html(
    html: str, *, list_url: str = OCEANVIEW_LIST_URL
) -> list[dict[str, Any]]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "html.parser")
    rows: list[dict[str, Any]] = []
    for a in soup.find_all("a", href=True):
        abs_u = urljoin(list_url, a["href"])
        lab = _strip(a.get_text(" ", strip=True))
        if "resource-manager/view" not in abs_u and "finalsite.net" not in abs_u.lower():
            continue
        if re.search(r"(?i)(w-9|vendor\s+information|tobacco|drug-free|terms\s+and\s+conditions|purchasing\s+and\s+contracts)", lab):
            continue
        if not re.search(r"(?i)(bid|rfp|rfq|vehicle|food\s+service|truck|equipment)", f"{lab} {abs_u}"):
            continue
        sol_m = re.search(r"(?i)\b((?:\d{4}-\d{2}-BID-\d{2})|(?:RFP\s*\d{4}-\d{2})|(?:\d{4}/\d{2}[^\s]*))", lab)
        sol = sol_m.group(1) if sol_m else re.sub(r"[^\w\-]+", "_", lab)[:48]
        rows.append(
            {
                "solicitation_number": sol,
                "title": lab,
                "detail_url": abs_u,
                "document_urls": [abs_u],
            }
        )
    # Also attach general terms URL when present
    return rows


def search_oceanview_ovsd(
    source: dict, *, limit: int | None = None, fetch_detail_docs: bool = True
) -> dict[str, Any]:
    cfg = source.get("config") or {}
    list_url = str(cfg.get("list_url") or OCEANVIEW_LIST_URL)
    try:
        status, final, html = fetch_text(list_url, timeout=40)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "items": [], "source_key": source.get("source_key"), "errors": [str(exc)], "stats": {}}
    if status >= 400 or not html:
        return {"ok": False, "items": [], "source_key": source.get("source_key"), "errors": [f"status={status}"], "stats": {}}
    rows = parse_oceanview_listing_html(html, list_url=final or list_url)
    items: list[dict] = []
    skipped = 0
    for row in rows:
        if limit is not None and len(items) >= int(limit):
            break
        title = row["title"]
        if re.search(r"(?i)(repairs?|mainten|portables?|prequalified|construction)", title):
            skipped += 1
            continue
        item = _item_base(
            source_key=source["source_key"],
            title=title,
            sol=row["solicitation_number"],
            buyer="Ocean View School District Purchasing",
            state="CA",
            official_url=row["detail_url"],
            discovery_url=final or list_url,
            doc_urls=row["document_urls"],
            detail_url=row["detail_url"],
            tz="America/Los_Angeles",
        )
        item["evidence"] = {
            **(item.get("evidence") or {}),
            "payment_terms_net45": True,
            "use_contractual_payment_terms_for_financing": True,
            "monitoring_priority": "lower",
        }
        items.append(item)
    return {
        "ok": bool(items) or bool(rows),
        "items": items,
        "source_key": source.get("source_key"),
        "errors": [],
        "verified_at": _utc(),
        "pages_fetched": 1,
        "rows_examined": len(rows),
        "pagination_complete": True,
        "more_available": False,
        "stats": {"accepted": len(items), "skipped": skipped, "listing_rows": len(rows)},
    }


def discover_oceanview_ovsd_documents(
    detail_url: str, html: str = "", *, extra_urls: list[str] | None = None
) -> list[dict[str, Any]]:
    return _docs_from_urls([detail_url, *(extra_urls or [])], platform="oceanview_ovsd")


# --- Michigan State University ------------------------------------------------------

MSU_LIST_URL = "https://procurement.msu.edu/for-suppliers/bid-opportunities"


def parse_msu_listing_html(html: str, *, list_url: str = MSU_LIST_URL) -> list[dict[str, Any]]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "html.parser")
    groups: dict[str, dict[str, Any]] = {}
    # Filename-based grouping is reliable on MSU Sitecore pages.
    for a in soup.find_all("a", href=True):
        abs_u = urljoin(list_url, a["href"])
        if "bidopps" not in abs_u.lower():
            continue
        name = unquote(urlparse(abs_u).path.split("/")[-1])
        m = re.search(r"(?i)(?:rfp|rfx|rxf)[-_]?(\d{5,7})", name)
        if not m:
            continue
        num = m.group(1)
        sol = f"RFP{num}"
        lab = _strip(a.get_text(" ", strip=True))
        entry = groups.setdefault(
            num,
            {"solicitation_number": sol, "title": sol, "document_urls": [], "detail_url": abs_u},
        )
        entry["document_urls"].append(abs_u)
        if re.search(r"(?i)^rfp\b", lab) and "qa" not in name.lower() and "announcement" not in name.lower():
            entry["detail_url"] = abs_u
        # Prefer nearby heading text for title
        heading = a.find_previous(["h2", "h3", "h4", "strong"])
        if heading:
            ht = _strip(heading.get_text(" ", strip=True))
            if num in ht.replace(" ", "") or num in ht:
                entry["title"] = ht[:300]
    rows = []
    for e in groups.values():
        e["document_urls"] = list(dict.fromkeys(e["document_urls"]))
        rows.append(e)
    return rows


def search_msu_procurement(
    source: dict, *, limit: int | None = None, fetch_detail_docs: bool = True
) -> dict[str, Any]:
    cfg = source.get("config") or {}
    list_url = str(cfg.get("list_url") or MSU_LIST_URL)
    try:
        status, final, html = fetch_text(list_url, timeout=45)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "items": [], "source_key": source.get("source_key"), "errors": [str(exc)], "stats": {}}
    if status >= 400 or not html:
        return {"ok": False, "items": [], "source_key": source.get("source_key"), "errors": [f"status={status}"], "stats": {}}
    rows = parse_msu_listing_html(html, list_url=final or list_url)
    items: list[dict] = []
    skipped = 0
    for row in rows:
        if limit is not None and len(items) >= int(limit):
            break
        title = row["title"]
        if re.search(
            r"(?i)\b(software|analytics|platform|saas|consulting|services|administrator)\b",
            title,
        ):
            skipped += 1
            continue
        item = _item_base(
            source_key=source["source_key"],
            title=title,
            sol=row["solicitation_number"],
            buyer="Michigan State University Procurement",
            state="MI",
            official_url=row["detail_url"],
            discovery_url=final or list_url,
            doc_urls=row["document_urls"],
            detail_url=row["detail_url"],
            tz="America/Detroit",
        )
        item["evidence"] = {
            **(item.get("evidence") or {}),
            "reconcile_deadline_extensions": True,
            "monitoring_priority": "lower",
        }
        items.append(item)
    return {
        "ok": bool(items) or bool(rows),
        "items": items,
        "source_key": source.get("source_key"),
        "errors": [],
        "verified_at": _utc(),
        "pages_fetched": 1,
        "rows_examined": len(rows),
        "pagination_complete": True,
        "more_available": False,
        "stats": {"accepted": len(items), "skipped": skipped, "listing_rows": len(rows)},
    }


def discover_msu_procurement_documents(
    detail_url: str, html: str = "", *, extra_urls: list[str] | None = None
) -> list[dict[str, Any]]:
    return _docs_from_urls([detail_url, *(extra_urls or [])], platform="msu_procurement")


# --- Indiana IDOA ZIP packages ------------------------------------------------------

INDIANA_LIST_URL = "https://www.in.gov/idoa/procurement/current-business-opportunities/"


def parse_indiana_listing_html(
    html: str, *, list_url: str = INDIANA_LIST_URL
) -> list[dict[str, Any]]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "html.parser")
    rows: list[dict[str, Any]] = []
    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            cells = tr.find_all(["td", "th"])
            if len(cells) < 5:
                continue
            texts = [_strip(c.get_text(" ", strip=True)) for c in cells]
            if texts[0].lower().startswith("event name") or "response due" in texts[4].lower():
                continue
            event_name = texts[0]
            agency = texts[1]
            event_id = texts[2]
            desc = texts[3]
            deadline = texts[4]
            zips = []
            for a in tr.find_all("a", href=True):
                abs_u = urljoin(list_url, a["href"])
                if abs_u.lower().endswith(".zip"):
                    zips.append(abs_u)
            if not event_id or not zips:
                continue
            title = re.sub(r"(?i)\s*bid\s+documents\s*$", "", event_name).strip() or desc
            rows.append(
                {
                    "solicitation_number": event_id,
                    "title": title[:500],
                    "agency": agency,
                    "deadline_at": deadline,
                    "detail_url": zips[0],
                    "document_urls": list(dict.fromkeys(zips)),
                    "description": desc,
                }
            )
    return rows


def inventory_indiana_zip_bytes(data: bytes) -> list[dict[str, Any]]:
    """Validate ZIP and list members (required before Indiana connector use)."""
    out: list[dict[str, Any]] = []
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            out.append(
                {
                    "name": info.filename,
                    "bytes": info.file_size,
                    "compress_size": info.compress_size,
                }
            )
    return out


def search_indiana_idoa(
    source: dict, *, limit: int | None = None, fetch_detail_docs: bool = True
) -> dict[str, Any]:
    cfg = source.get("config") or {}
    list_url = str(cfg.get("list_url") or INDIANA_LIST_URL)
    try:
        status, final, html = fetch_text(list_url, timeout=45)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "items": [], "source_key": source.get("source_key"), "errors": [str(exc)], "stats": {}}
    if status >= 400 or not html:
        return {"ok": False, "items": [], "source_key": source.get("source_key"), "errors": [f"status={status}"], "stats": {}}
    rows = parse_indiana_listing_html(html, list_url=final or list_url)
    items: list[dict] = []
    skipped = 0
    for row in rows:
        if limit is not None and len(items) >= int(limit):
            break
        title = f"{row['title']} {row.get('description') or ''}"
        if _serviceish(title) or re.search(
            r"(?i)\b(trash\s+services|administrator|toxicology|adoption\s+services|"
            r"consulting|management\s+services|health\s+plan)\b",
            title,
        ):
            skipped += 1
            continue
        item = _item_base(
            source_key=source["source_key"],
            title=row["title"],
            sol=row["solicitation_number"],
            buyer=str(row.get("agency") or "State of Indiana")[:255],
            state="IN",
            official_url=row["detail_url"],
            discovery_url=final or list_url,
            doc_urls=row["document_urls"],
            deadline=row.get("deadline_at"),
            detail_url=row["detail_url"],
            tz="America/Indiana/Indianapolis",
        )
        item["evidence"] = {
            **(item.get("evidence") or {}),
            "package_format": "zip_archive",
            "validate_zip_before_use": True,
            "monitor_zip_hash_for_revisions": True,
            "description": (row.get("description") or "")[:500],
        }
        items.append(item)
    return {
        "ok": bool(items) or bool(rows),
        "items": items,
        "source_key": source.get("source_key"),
        "errors": [],
        "verified_at": _utc(),
        "pages_fetched": 1,
        "rows_examined": len(rows),
        "pagination_complete": True,
        "more_available": False,
        "stats": {"accepted": len(items), "skipped": skipped, "listing_rows": len(rows)},
    }


def discover_indiana_idoa_documents(
    detail_url: str, html: str = "", *, extra_urls: list[str] | None = None
) -> list[dict[str, Any]]:
    docs = _docs_from_urls([detail_url, *(extra_urls or [])], platform="indiana_idoa")
    for d in docs:
        if str(d.get("source_url") or "").lower().endswith(".zip"):
            d["document_type"] = "solicitation_zip_package"
    return docs


# --- Pennsylvania eMarketplace ------------------------------------------------------

PA_SEARCH_URL = "https://www.emarketplace.state.pa.us/Search.aspx"
PA_SOL_URL = "https://www.emarketplace.state.pa.us/Solicitations.aspx?SID={sid}"
# Advertisement type: 10 = Materials (product-first public CSV export).
_PA_AD_MATERIALS = "10"


def _pa_search_csv(*, ad_type: str = _PA_AD_MATERIALS) -> tuple[str, list[dict[str, str]]]:
    """POST Materials/All search; server returns public Solicitations-*.csv."""
    import csv
    import io

    import httpx
    from bs4 import BeautifulSoup

    from m3.research.http_util import USER_AGENT

    with httpx.Client(follow_redirects=True, timeout=70, headers={"User-Agent": USER_AGENT}) as client:
        r = client.get(PA_SEARCH_URL)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        payload: dict[str, str] = {}
        for inp in soup.find_all("input"):
            name = inp.get("name")
            if not name:
                continue
            typ = (inp.get("type") or "").lower()
            if typ in {"radio", "checkbox"} and not inp.has_attr("checked"):
                continue
            payload[name] = inp.get("value") or ""
        for sel in soup.find_all("select"):
            name = sel.get("name")
            if not name:
                continue
            opt = sel.find("option", selected=True) or sel.find("option")
            payload[name] = (opt.get("value") if opt else "") or ""
        payload["ctl00$MainBody$rdolAdTypes"] = ad_type
        payload["ctl00$MainBody$ddlRows"] = "32767"
        payload["ctl00$MainBody$btnSearch"] = "Search"
        r2 = client.post(PA_SEARCH_URL, data=payload)
        r2.raise_for_status()
        text = r2.text or ""
        if "Bid No,Bid Type,Title" not in text:
            raise RuntimeError("pa_emarketplace_csv_not_returned")
        reader = csv.DictReader(io.StringIO(text))
        rows = [dict(row) for row in reader if row.get("Bid No")]
        return str(r2.url), rows


def _pa_detail_docs(sid: str) -> tuple[list[str], dict[str, Any]]:
    from bs4 import BeautifulSoup

    meta: dict[str, Any] = {}
    url = PA_SOL_URL.format(sid=sid)
    try:
        st, fin, html = fetch_text(url, timeout=40)
    except Exception as exc:  # noqa: BLE001
        meta["detail_error"] = f"{type(exc).__name__}"
        return [], meta
    if st >= 400 or not html:
        meta["detail_status"] = st
        return [], meta
    soup = BeautifulSoup(html or "", "html.parser")
    docs: list[str] = []
    login_refs = 0
    for a in soup.find_all("a", href=True):
        href_raw = (a.get("href") or "").replace("&amp;", "&")
        abs_u = urljoin(fin or url, href_raw)
        lab = _strip(a.get_text(" ", strip=True))
        low = abs_u.lower()
        if "filedownload.aspx" in low:
            docs.append(abs_u)
            continue
        if re.search(
            r"(?i)(login|sign\s*in|supplier\s+portal|registration\s+required)",
            f"{lab} {abs_u}",
        ):
            login_refs += 1
    # Regex fallback only when soup found no FileDownload (avoid truncating
    # OriginalFileName values that contain spaces).
    if not docs:
        for href in re.findall(
            r'FileDownload\.aspx\?file=[^"\'>\s&]+(?:&amp;|&)?(?:OriginalFileName=[^"\'>]*)?',
            html or "",
            flags=re.I,
        ):
            docs.append(urljoin(fin or url, href.replace("&amp;", "&")))
    # Dedupe by file= path so OriginalFileName variants collapse
    deduped: list[str] = []
    seen_file: set[str] = set()
    for u in docs:
        m = re.search(r"(?i)[?&]file=([^&]+)", u)
        key = unquote(m.group(1)).lower() if m else u.lower()
        if key in seen_file:
            continue
        seen_file.add(key)
        deduped.append(u)
    docs = deduped
    meta["public_filedownload_count"] = len(docs)
    meta["login_referenced_links"] = login_refs
    meta["package_incomplete"] = bool(login_refs) or len(docs) == 0
    meta["verify_package_completeness"] = True
    return docs, meta


def search_pa_emarketplace(
    source: dict, *, limit: int | None = None, fetch_detail_docs: bool = True
) -> dict[str, Any]:
    cfg = source.get("config") or {}
    ad_type = str(cfg.get("ad_type") or _PA_AD_MATERIALS)
    try:
        final, rows = _pa_search_csv(ad_type=ad_type)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "items": [],
            "source_key": source.get("source_key"),
            "errors": [f"{type(exc).__name__}:{exc}"],
            "stats": {},
        }
    items: list[dict] = []
    skipped = 0
    for row in rows:
        if limit is not None and len(items) >= int(limit):
            break
        sol = str(row.get("Bid No") or "").strip()
        title = str(row.get("Title") or "").strip()
        status = str(row.get("Status") or "").strip()
        bid_type = str(row.get("Bid Type") or "").strip()
        if not sol or not title:
            skipped += 1
            continue
        if status and status.lower() not in {"open", "extended", "amended", ""}:
            skipped += 1
            continue
        if _serviceish(title) or re.search(
            r"(?i)\b(rfi|consulting|management\s+services|construction)\b", f"{title} {bid_type}"
        ):
            # Keep IFB materials; drop pure RFI service shells
            if bid_type.upper() == "RFI" or re.search(r"(?i)\b(consulting|management\s+services)\b", title):
                skipped += 1
                continue
        # COSTARS / non-numeric shells often lack public FileDownload packages
        if not re.fullmatch(r"\d{7,}", sol):
            skipped += 1
            continue
        detail = PA_SOL_URL.format(sid=sol)
        docs: list[str] = []
        detail_meta: dict[str, Any] = {}
        if fetch_detail_docs:
            docs, detail_meta = _pa_detail_docs(sol)
            if not docs:
                skipped += 1
                continue
        item = _item_base(
            source_key=source["source_key"],
            title=title,
            sol=sol,
            buyer=str(row.get("Agency") or "Commonwealth of Pennsylvania")[:255],
            state="PA",
            official_url=(docs[0] if docs else detail),
            discovery_url=final or PA_SEARCH_URL,
            doc_urls=docs,
            deadline=str(row.get("Bid Open Date") or "") or None,
            detail_url=detail,
            tz="America/New_York",
        )
        item["summary"] = str(row.get("Description") or title)[:1000]
        item["evidence"] = {
            **(item.get("evidence") or {}),
            "bid_type": bid_type,
            "listing_status": status,
            "ad_type_filter": "materials",
            "some_refs_may_require_supplier_login": True,
            **detail_meta,
        }
        if detail_meta.get("package_incomplete"):
            item["package_status_hint"] = "INCOMPLETE_OR_LOGIN_REFS"
        items.append(item)
    return {
        "ok": bool(items) or bool(rows),
        "items": items,
        "source_key": source.get("source_key"),
        "errors": [],
        "verified_at": _utc(),
        "pages_fetched": 1,
        "rows_examined": len(rows),
        "pagination_complete": True,
        "more_available": False,
        "stats": {"accepted": len(items), "skipped": skipped, "listing_rows": len(rows)},
    }


def discover_pa_emarketplace_documents(
    detail_url: str, html: str = "", *, extra_urls: list[str] | None = None
) -> list[dict[str, Any]]:
    urls = list(extra_urls or [])
    sid_m = re.search(r"(?i)[?&]SID=([^&]+)", detail_url or "")
    if sid_m:
        docs, _meta = _pa_detail_docs(sid_m.group(1))
        urls.extend(docs)
    elif detail_url:
        urls.append(detail_url)
    return _docs_from_urls(urls, platform="pa_emarketplace")


# --- Louisiana LaPAC ----------------------------------------------------------------

LA_CAT_URL = "https://wwwcfprd.doa.louisiana.gov/osp/lapac/catbids.cfm"
LA_SEGMENT_URL = "https://wwwcfprd.doa.louisiana.gov/osp/lapac/dspBid.cfm?search=segment&term={term}"


def _la_category_terms(html: str, *, list_url: str = LA_CAT_URL) -> list[tuple[str, str]]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "html.parser")
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        abs_u = urljoin(list_url, a["href"])
        m = re.search(r"(?i)[?&]term=(\d+)", abs_u)
        if not m:
            continue
        term = m.group(1)
        if term in seen:
            continue
        seen.add(term)
        out.append((_strip(a.get_text(" ", strip=True)), term))
    return out


def parse_lapac_segment_html(
    html: str, *, list_url: str, category: str = ""
) -> list[dict[str, Any]]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "html.parser")
    table = soup.find("table")
    if not table:
        return []
    rows_out: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for tr in table.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue
        texts = [_strip(c.get_text(" ", strip=True)) for c in cells]
        if texts and texts[0].lower() in {"bid number", "bid no"}:
            continue
        # New solicitation rows usually have 4+ cells with open date
        pdfs = []
        for a in tr.find_all("a", href=True):
            abs_u = urljoin(list_url, a["href"])
            if "/agency/pdf/" in abs_u.lower() or _is_doc(abs_u):
                pdfs.append(abs_u)
        first = texts[0] if texts else ""
        looks_new = len(cells) >= 4 and first and not re.search(r"(?i)^addendum", first)
        if looks_new and pdfs:
            if current:
                rows_out.append(current)
            current = {
                "solicitation_number": first.split()[0][:64],
                "title": texts[1] if len(texts) > 1 else first,
                "deadline_at": texts[3] if len(texts) > 3 else None,
                "detail_url": pdfs[0],
                "document_urls": list(dict.fromkeys(pdfs)),
                "category": category,
            }
        elif current and pdfs:
            current["document_urls"] = list(dict.fromkeys([*current["document_urls"], *pdfs]))
            if re.search(r"(?i)addendum|amend", " ".join(texts)):
                current["has_amendment"] = True
    if current:
        rows_out.append(current)
    return rows_out


def search_louisiana_lapac(
    source: dict, *, limit: int | None = None, fetch_detail_docs: bool = True
) -> dict[str, Any]:
    cfg = source.get("config") or {}
    cat_url = str(cfg.get("list_url") or LA_CAT_URL)
    try:
        status, final, html = fetch_text(cat_url, timeout=45)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "items": [], "source_key": source.get("source_key"), "errors": [str(exc)], "stats": {}}
    if status >= 400 or not html:
        return {"ok": False, "items": [], "source_key": source.get("source_key"), "errors": [f"status={status}"], "stats": {}}
    terms = _la_category_terms(html, list_url=final or cat_url)
    # Prefer goods-like UNSPSC segments first, then the rest
    product_terms = {
        "12000000",  # chemicals
        "47000000",  # cleaning
        "25000000",  # vehicles
        "52000000",  # appliances
        "53000000",  # apparel
        "46000000",  # defense/safety equip
        "40000000",  # distribution systems/equip
        "22000000",  # construction machinery/accessories
    }
    ordered = [t for t in terms if t[1] in product_terms] + [t for t in terms if t[1] not in product_terms]
    items: list[dict] = []
    skipped = 0
    pages = 1
    errors: list[str] = []
    rows_examined = 0
    seen_sol: set[str] = set()
    for cat_name, term in ordered:
        if limit is not None and len(items) >= int(limit):
            break
        seg = LA_SEGMENT_URL.format(term=term)
        try:
            st, fin, seg_html = fetch_text(seg, timeout=40)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"segment:{term}:{type(exc).__name__}")
            continue
        pages += 1
        if st >= 400 or not seg_html:
            errors.append(f"segment:{term}:status={st}")
            continue
        rows = parse_lapac_segment_html(seg_html, list_url=fin or seg, category=cat_name)
        rows_examined += len(rows)
        for row in rows:
            if limit is not None and len(items) >= int(limit):
                break
            sol = row["solicitation_number"]
            if sol in seen_sol:
                continue
            title = row["title"]
            if _serviceish(title) or re.search(
                r"(?i)\b(waste\s+disposal|transport\s+and\s+disposal|generator|"
                r"lift\s+station|services\b|consulting)\b",
                title,
            ):
                # Keep product-ish chemical/equip titles even if service words appear later
                if not re.search(r"(?i)\b(salt|paper|supply|supplies|equipment|chemical|food|hygiene)\b", title):
                    skipped += 1
                    continue
            seen_sol.add(sol)
            item = _item_base(
                source_key=source["source_key"],
                title=title,
                sol=sol,
                buyer="Louisiana public entity (LaPAC)",
                state="LA",
                official_url=row["detail_url"],
                discovery_url=final or cat_url,
                doc_urls=row["document_urls"],
                deadline=row.get("deadline_at"),
                detail_url=row["detail_url"],
                tz="America/Chicago",
            )
            item["evidence"] = {
                **(item.get("evidence") or {}),
                "lapac_category": cat_name,
                "lapac_segment": term,
                "has_amendment": bool(row.get("has_amendment")),
            }
            items.append(item)
    return {
        "ok": bool(items) or rows_examined > 0,
        "items": items,
        "source_key": source.get("source_key"),
        "errors": errors[:20],
        "verified_at": _utc(),
        "pages_fetched": pages,
        "rows_examined": rows_examined,
        "pagination_complete": True,
        "more_available": False,
        "stats": {
            "accepted": len(items),
            "skipped": skipped,
            "listing_rows": rows_examined,
            "categories": len(terms),
        },
    }


def discover_louisiana_lapac_documents(
    detail_url: str, html: str = "", *, extra_urls: list[str] | None = None
) -> list[dict[str, Any]]:
    return _docs_from_urls([detail_url, *(extra_urls or [])], platform="louisiana_lapac")
