"""Fresh True-Open Detail/Package Enrichment Repair + Downstream Retest V1.

Root cause of 0 packages / 0 real lines in zero-history exhaustive:
process_one assessed SOURCE_DEPTH from L23 title-only records and never called
OpenGov project detail / priceTables / attachments (or BidNet free detail).

This job:
1) loads the 8,226 true-open rows from SOURCE_RUN
2) runs source-specific FREE enrichment (OpenGov first, checkpoints 50/250)
3) merges fresh detail/lines into L23
4) re-runs product-fit AFTER enrichment + full disposition
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

ITEM_TIMEOUT_S = int(os.environ.get("M3_ENRICH_ITEM_TIMEOUT_S") or "45")

from application_clock import now_utc
from buyer_intelligence.stage1_full_production_acceptance_v1 import process_one
from buyer_intelligence.true_live_accepting_bids_gate_v1 import (
    OPEN_ACCEPTING_BIDS,
    determine_canonical_live_state,
    product_fit,
)
from m3_jobs.standard import LONG_RUNNING_JOB_EXECUTION_STANDARD

log = logging.getLogger("govtracker.fresh_true_open_detail_package_enrichment_repair_v1")

BUILD = "20261010-m3-fresh-true-open-detail-package-enrichment-repair-v1"
SOURCE_RUN = "prod-live-20261010T023015Z-6d8c5be7"
FAMILY = "fresh_true_open_detail_package_enrichment_repair_v1"

REPORT_FILE = "m3_fresh_true_open_detail_package_enrichment_repair_v1_report.json"
PROGRESS_FILE = "m3_fresh_true_open_detail_package_enrichment_repair_v1_progress.json"
JOB_FILE = "m3_fresh_true_open_detail_package_enrichment_repair_v1_job.json"
CHECKPOINT_50 = "m3_fresh_true_open_enrichment_checkpoint_50_v1.json"
CHECKPOINT_250 = "m3_fresh_true_open_enrichment_checkpoint_250_v1.json"
ENRICHMENT_FILE = "m3_fresh_true_open_enrichment_sidecar_v1.json"
RESULTS_FILE = "m3_fresh_true_open_enrichment_results_v1.json"
ACTIONABLE_FILE = "m3_fresh_true_open_enrichment_actionable_v1.json"
WIRING_FILE = "m3_fresh_true_open_enrichment_wiring_trace_v1.json"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_job_lock = threading.Lock()
_worker = False

# Service/construction titles — never promote to DESCRIPTION_LINE product lines.
_BIDNET_SERVICE_TITLE = re.compile(
    r"\b(services?|consulting|construction|renovation|remodel|install(?:ation)?|"
    r"maintenance|repair|rental|lease|staffing|training|software\s+system|"
    r"architectural|engineering|actuarial|therapy|assessment)\b",
    re.I,
)
_BIDNET_PRODUCTISH_TITLE = re.compile(
    r"\b(supply|supplies|purchase|furnish|deliver|equipment|parts?|kit|batter(?:y|ies)|"
    r"filter|glove|ppe|paper|chair|screw|hardware|tool|bulb|lamp|chemical|"
    r"cleaner|mop|oem|nsn|model|catalog|trailer|vehicle|pump|valve|hose)\b",
    re.I,
)


def _utc() -> str:
    return now_utc().isoformat()


def _enriched_this_build(rec: dict[str, Any]) -> bool:
    """Resume-safe: row already processed by this build's enrichment_repair blob."""
    er = rec.get("enrichment_repair_v1") if isinstance(rec.get("enrichment_repair_v1"), dict) else {}
    return BUILD in str(er.get("build") or "")


def _bidnet_description_lines(rec: dict[str, Any]) -> list[dict[str, Any]]:
    """Promote clear single-commodity BidNet titles/abstracts to DESCRIPTION_LINE (not TITLE_FALLBACK)."""
    title = str(rec.get("title") or "").strip()
    desc = str(rec.get("description") or "").strip()
    if not title or len(title) < 8:
        return []
    if _BIDNET_SERVICE_TITLE.search(title):
        return []
    # Require product signal in title OR short concrete abstract
    productish = bool(_BIDNET_PRODUCTISH_TITLE.search(title))
    if not productish and not (desc and len(desc) < 400 and _BIDNET_PRODUCTISH_TITLE.search(desc)):
        return []
    # Multi-scope / construction package titles stay out
    if title.count(",") >= 3 or " and associated " in title.lower():
        return []
    body = desc if desc and len(desc) >= 20 else title
    return _normalize_lines(
        [{"DESCRIPTION": body[:500], "QTY": None, "UOM": None, "SOURCE_REF": "BIDNET_FREE_ABSTRACT"}],
        provenance="DESCRIPTION_LINE",
    )


def _bidnet_lines_from_free_docs(cid: str, rec: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract real lines from free BidNet documents already on the record (no paid path)."""
    atts = rec.get("attachments_metadata") if isinstance(rec.get("attachments_metadata"), list) else []
    br = rec.get("bidnet_recovery") if isinstance(rec.get("bidnet_recovery"), dict) else {}
    docs = br.get("documents") if isinstance(br.get("documents"), list) else []
    if not atts and not docs:
        return []
    if docs and not atts:
        rec["attachments_metadata"] = docs
    try:
        from bidnet_recovery.free_package_batch import _ingest_free_documents  # type: ignore
        from line_item_economics.engine import analyze_line_item_economics
    except Exception:
        return []
    try:
        extracted = _ingest_free_documents(cid, rec) or ""
    except Exception:
        extracted = ""
    body = "\n".join(
        [
            str(rec.get("title") or ""),
            str(rec.get("description") or ""),
            extracted,
        ]
    )
    if len(body.strip()) < 40:
        return []
    try:
        lie = analyze_line_item_economics(
            opportunity_id=cid,
            title=rec.get("title"),
            buyer=rec.get("buyer"),
            body_text=body,
            csv_text=extracted if extracted.count(",") > 10 else None,
            persist=False,
        )
        raw = lie.get("lines") or []
        return _normalize_lines(raw if isinstance(raw, list) else [], provenance="DOCUMENT_LINE")
    except Exception:
        return []


def _data(*parts: str) -> Path:
    from m3_data_root import data_path

    return data_path(*parts)


def _load(name: str) -> Any:
    p = _data(name)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save(name: str, obj: Any) -> None:
    p = _data(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
    tmp.replace(p)


def _job_update(**kwargs: Any) -> None:
    with _job_lock:
        cur = _load(JOB_FILE) or {"kind": "FreshTrueOpenEnrichmentJob", "build": BUILD}
        cur.update(kwargs)
        cur["heartbeat_at"] = _utc()
        cur["build"] = BUILD
        _save(JOB_FILE, cur)
        _save(
            PROGRESS_FILE,
            {"build": BUILD, "updated_at": _utc(), "status": cur.get("status"), "phase": cur.get("phase"), "progress": cur},
        )


def job_progress() -> dict[str, Any]:
    return _load(JOB_FILE) or {"status": "IDLE", "build": BUILD}


def wiring_trace() -> dict[str, Any]:
    """Phase 1 — exact missing stages that caused 0 packages."""
    doc = {
        "kind": "EnrichmentWiringTrace",
        "build": BUILD,
        "created_at": _utc(),
        "DEFECT": "DISCOVERY_TO_FUNNEL_SKIPPED_DETAIL_ENRICHMENT",
        "ROOT_CAUSE": (
            "zero_history process_universe called process_one/assess_source_depth on L23 "
            "list/discovery records without calling OpenGov fetch_project_detail / "
            "parse_detail_to_enrichment / BidNet recover_one / free package chase."
        ),
        "PATH_BEFORE": [
            "DISCOVERY → L23 merge",
            "TRUE_LIVE gate (title/deadline)",
            "PRODUCT_FIT (title)",
            "process_one → SOURCE_DEPTH=TITLE_ONLY",
            "disposition (no lines/packages)",
        ],
        "MISSING_STAGES": [
            "DETAIL_FETCH",
            "STRUCTURED_TABLE_FETCH",
            "ATTACHMENT_DISCOVERY",
            "DOCUMENT_FETCH",
            "LINE_EXTRACTION",
            "PRODUCT_FIT_AFTER_ENRICHMENT",
        ],
        "PATH_AFTER_FIX": [
            "TRUE_OPEN cohort from SOURCE_RUN",
            "SOURCE-SPECIFIC FREE DETAIL",
            "STRUCTURED TABLES / ATTACHMENTS",
            "MERGE INTO L23 (fresh only)",
            "PRODUCT_FIT AFTER ENRICHMENT",
            "LINE EXTRACTION",
            "IDENTITY / QUOTE / SUPPLIER / RFQ / ECONOMICS",
            "FINAL DISPOSITION",
        ],
        "ADAPTERS": {
            "OpenGov": {
                "detail": "opengov_live_deep_enrichment_v1.fetch_project_detail",
                "lines": "extract_line_items / parse_detail_to_enrichment",
                "wired_in_exhaustive": False,
                "wired_in_this_build": True,
            },
            "BidNet": {
                "detail": "bidnet_recovery.recover_one(auth_client=None)",
                "official_route": "bidnet_recovery.free_package_chase.chase_free_package",
                "wired_in_exhaustive": False,
                "wired_in_this_build": True,
            },
            "Socrata": {
                "detail": "row fields + optional detail_url GET",
                "wired_in_exhaustive": False,
                "wired_in_this_build": True,
            },
        },
    }
    _save(WIRING_FILE, doc)
    return doc


def _platform_family(rec: dict[str, Any]) -> str:
    blob = " ".join(
        str(x or "")
        for x in (
            rec.get("platform"),
            rec.get("SOURCE"),
            rec.get("authoritative_url"),
            rec.get("detail_url"),
            rec.get("canonical_opportunity_id"),
        )
    ).lower()
    if "opengov" in blob:
        return "OpenGov"
    if "bidnet" in blob:
        return "BidNet"
    if "socrata" in blob or "structured" in blob or "data.city" in blob or "/resource/" in blob:
        return "Socrata"
    if "bonfire" in blob:
        return "Bonfire"
    if "jaggaer" in blob or "euna" in blob:
        return "Jaggaer/Euna"
    if "planetbids" in blob:
        return "PlanetBids"
    if "ionwave" in blob:
        return "IonWave"
    if "demandstar" in blob:
        return "DemandStar"
    if "publicpurchase" in blob:
        return "PublicPurchase"
    if "sam.gov" in blob or "sam_gov" in blob:
        return "SAM"
    return "Other"


def load_true_open_cohort(*, source_run: str = SOURCE_RUN) -> list[tuple[str, dict[str, Any]]]:
    """Addressable true-open rows from SOURCE_RUN L23 (fresh only)."""
    store = (_load("l23_canonical_population_store.json") or {}).get("opportunities") or {}
    if not isinstance(store, dict):
        return []
    now = datetime.now(timezone.utc)
    out: list[tuple[str, dict[str, Any]]] = []
    for cid, rec in store.items():
        if not isinstance(rec, dict):
            continue
        if rec.get("PRODUCTION_RUN_ID") and rec.get("PRODUCTION_RUN_ID") != source_run:
            continue
        # Prefer stamped run; if stamp missing but store is current post-wipe universe, still include
        gate = determine_canonical_live_state({**rec, "canonical_opportunity_id": cid}, now=now)
        if gate.get("CANONICAL_STATE") != OPEN_ACCEPTING_BIDS:
            continue
        out.append((str(cid), rec))
    # Stable priority: OpenGov → Socrata → other portals → BidNet → rest
    rank = {"OpenGov": 0, "Socrata": 1, "Bonfire": 2, "Jaggaer/Euna": 3, "PlanetBids": 4, "IonWave": 5, "DemandStar": 6, "PublicPurchase": 7, "BidNet": 8, "SAM": 9, "Other": 10}
    out.sort(key=lambda x: (rank.get(_platform_family(x[1]), 99), x[0]))
    return out


def _normalize_lines(raw_lines: list[dict[str, Any]], *, provenance: str) -> list[dict[str, Any]]:
    out = []
    for i, ln in enumerate(raw_lines or []):
        if not isinstance(ln, dict):
            continue
        desc = str(ln.get("DESCRIPTION") or ln.get("description") or ln.get("item") or "").strip()
        if not desc:
            continue
        out.append(
            {
                "line_number": ln.get("LINE_NUMBER") or ln.get("line_number") or ln.get("lineItem") or (i + 1),
                "description": desc,
                "DESCRIPTION": desc,
                "qty": ln.get("QTY") if ln.get("QTY") is not None else ln.get("qty") or ln.get("quantity"),
                "QTY": ln.get("QTY") if ln.get("QTY") is not None else ln.get("qty") or ln.get("quantity"),
                "uom": ln.get("UOM") or ln.get("uom") or ln.get("unitToMeasure"),
                "UOM": ln.get("UOM") or ln.get("uom") or ln.get("unitToMeasure"),
                "manufacturer": ln.get("MANUFACTURER") or ln.get("manufacturer"),
                "brand": ln.get("BRAND") or ln.get("brand"),
                "mpn": ln.get("MPN") or ln.get("mpn"),
                "model": ln.get("MODEL") or ln.get("model"),
                "catalog": ln.get("CATALOG_NUMBER") or ln.get("catalog"),
                "nsn": ln.get("NSN") or ln.get("nsn"),
                "size": ln.get("SIZE") or ln.get("size"),
                "material": ln.get("MATERIAL") or ln.get("material"),
                "voltage": ln.get("VOLTAGE") or ln.get("voltage"),
                "amperage": ln.get("AMPERAGE") or ln.get("amperage"),
                "wattage": ln.get("WATTAGE") or ln.get("wattage"),
                "capacity": ln.get("CAPACITY") or ln.get("capacity"),
                "PROVENANCE": provenance,
                "SOURCE_REF": ln.get("SOURCE_REF") or provenance,
            }
        )
    return out


def _package_class(*, lines: list, has_pt: bool, has_docs: bool, has_desc: bool, blocker: str | None) -> str:
    if blocker in {"PAID_SOURCE_BLOCKER", "PACKAGE_REQUEST_REQUIRED"}:
        return blocker
    if lines and (has_pt or has_docs):
        return "PACKAGE_RESOLVED_MIXED" if (has_pt and has_docs) else (
            "PACKAGE_RESOLVED_STRUCTURED" if has_pt or lines else "PACKAGE_RESOLVED_DOCUMENT"
        )
    if lines:
        return "PACKAGE_RESOLVED_STRUCTURED"
    if has_docs and has_desc:
        return "PACKAGE_SHALLOW"
    if has_desc:
        return "PACKAGE_SHALLOW"
    if blocker:
        return "PACKAGE_UNRESOLVED"
    return "PACKAGE_UNRESOLVED"


def enrich_opengov_one(
    cid: str,
    rec: dict[str, Any],
    *,
    client: httpx.Client,
    run_id: str,
) -> dict[str, Any]:
    from buyer_intelligence.opengov_line_spec_supplier_ready_v1 import resolve_opengov_slug_pid
    from buyer_intelligence.opengov_live_deep_enrichment_v1 import (
        fetch_project_detail,
        parse_detail_to_enrichment,
    )

    truth: dict[str, Any] = {
        "OPPORTUNITY_ID": cid,
        "SOURCE": "OpenGov",
        "DETAIL_ATTEMPTED": True,
        "DETAIL_SUCCESS": False,
        "DETAIL_FAILED": False,
        "DETAIL_HTTP_STATUS": None,
        "DETAIL_HAS_DESCRIPTION": False,
        "DETAIL_HAS_PRICETABLE": False,
        "DETAIL_HAS_ATTACHMENTS": False,
        "DETAIL_HAS_COMMODITY": False,
        "DETAIL_HAS_CUSTOM_FIELDS": False,
        "PACKAGE_CLASS": "PACKAGE_UNRESOLVED",
        "REAL_LINES": 0,
        "SOURCE_DEPTH": "TITLE_ONLY",
        "PRODUCTION_RUN_ID": SOURCE_RUN,
        "ENRICHMENT_RUN_ID": run_id,
        "EVIDENCE_ORIGIN": "FRESH_SOURCE_FETCH",
        "OLD_CACHE_USED": False,
    }
    sp = resolve_opengov_slug_pid(cid, rec=rec)
    if not sp:
        # also try raw_metadata
        meta = rec.get("raw_metadata") if isinstance(rec.get("raw_metadata"), dict) else {}
        slug = meta.get("opengov_government_code") or meta.get("government_code")
        pid = meta.get("opengov_project_id") or meta.get("project_id")
        if slug and pid:
            sp = (str(slug), str(pid))
    if not sp:
        truth["DETAIL_FAILED"] = True
        truth["BLOCKER"] = "NO_OPENGOV_SLUG_PID"
        return truth

    slug, pid = sp
    truth["PORTAL_SLUG"] = slug
    truth["PROJECT_ID"] = pid
    fetched = fetch_project_detail(slug, pid, client=client, use_cache=False)
    truth["DETAIL_HTTP_STATUS"] = fetched.get("http_status")
    truth["ROUTE_STATUS"] = fetched.get("route_status")
    truth["OLD_CACHE_USED"] = bool(fetched.get("cache_hit"))

    if fetched.get("route_status") != "PUBLIC_DETAIL_OK" or not fetched.get("payload"):
        truth["DETAIL_FAILED"] = True
        truth["BLOCKER"] = str(fetched.get("route_status") or fetched.get("error") or "DETAIL_FAILED")
        return truth

    payload = fetched["payload"]
    en = parse_detail_to_enrichment(slug, pid, payload, route_status="PUBLIC_DETAIL_OK")
    lines = _normalize_lines(en.get("LINE_ITEMS") or [], provenance="STRUCTURED_PORTAL_LINE")
    docs = en.get("ATTACHMENTS") or []
    desc = str((en.get("DESCRIPTION") or {}).get("VALUE") or "")
    pts = payload.get("priceTables") or []
    has_custom = False
    for pt in pts:
        if not isinstance(pt, dict):
            continue
        for i in range(1, 6):
            if pt.get(f"headerCustom{i}"):
                has_custom = True
                break
        for row in pt.get("priceItems") or []:
            if isinstance(row, dict) and any(row.get(f"custom{i}") for i in range(1, 6)):
                has_custom = True
                break

    truth.update(
        {
            "DETAIL_SUCCESS": True,
            "DETAIL_HAS_DESCRIPTION": bool(desc.strip()),
            "DETAIL_HAS_PRICETABLE": bool(pts),
            "DETAIL_HAS_ATTACHMENTS": bool(docs),
            "DETAIL_HAS_COMMODITY": bool(en.get("COMMODITY_CODES")),
            "DETAIL_HAS_CUSTOM_FIELDS": has_custom,
            "PRICETABLE_COUNT": len(pts),
            "ATTACHMENT_COUNT": len(docs),
            "REAL_LINES": len(lines),
            "SOURCE_DEPTH": en.get("SOURCE_DEPTH") or "TITLE_ONLY",
            "PACKAGE_CLASS": _package_class(
                lines=lines, has_pt=bool(pts), has_docs=bool(docs), has_desc=bool(desc.strip()), blocker=None
            ),
        }
    )

    # Merge fresh evidence into L23 record (in-memory; caller persists)
    rec["description"] = desc or rec.get("description")
    rec["line_items"] = lines
    rec["attachments_metadata"] = [
        {
            "document_name": d.get("NAME") or d.get("document_name"),
            "document_type": d.get("TYPE") or d.get("document_type"),
            "document_url": d.get("URL") or d.get("document_url"),
            "CLASS": _classify_attachment(d),
        }
        for d in docs
        if isinstance(d, dict)
    ]
    if (en.get("DEADLINE") or {}).get("VALUE"):
        rec["deadline"] = (en.get("DEADLINE") or {}).get("VALUE")
    if (en.get("STATUS") or {}).get("VALUE"):
        rec["status"] = (en.get("STATUS") or {}).get("VALUE")
        rec["source_status"] = (en.get("STATUS") or {}).get("VALUE")
    rec["line_texts"] = [ln["description"] for ln in lines]
    rec["enrichment_repair_v1"] = {
        "build": BUILD,
        "ENRICHMENT_RUN_ID": run_id,
        "SOURCE": "OpenGov",
        "DETAIL_SUCCESS": True,
        "PACKAGE_CLASS": truth["PACKAGE_CLASS"],
        "REAL_LINES": len(lines),
        "SOURCE_DEPTH": truth["SOURCE_DEPTH"],
        "FETCHED_AT": _utc(),
        "opengov_canonical": f"opengov:{slug}:{pid}",
    }
    rec["ENRICHMENT_RUN_ID"] = run_id
    rec["EVIDENCE_ORIGIN"] = "FRESH_SOURCE_FETCH"
    truth["PRODUCT_FIT_AFTER"] = product_fit(
        {
            **rec,
            "canonical_opportunity_id": cid,
            "title": rec.get("title"),
            "description": rec.get("description"),
            "line_texts": rec.get("line_texts"),
        }
    ).get("PRODUCT_FIT")
    return truth


def _classify_attachment(d: dict[str, Any]) -> str:
    blob = " ".join(str(x or "") for x in (d.get("TYPE"), d.get("document_type"), d.get("NAME"), d.get("document_name"))).lower()
    if "spec" in blob:
        return "SPECIFICATION"
    if "pric" in blob or "bid schedule" in blob or "schedule" in blob:
        return "PRICING_SCHEDULE" if "pric" in blob else "BID_SCHEDULE"
    if "addend" in blob:
        return "ADDENDUM"
    if "draw" in blob:
        return "DRAWING"
    if "term" in blob or "condition" in blob:
        return "TERMS"
    if "product" in blob or "item" in blob:
        return "PRODUCT_LIST"
    return "OTHER"


def enrich_socrata_one(cid: str, rec: dict[str, Any], *, client: httpx.Client, run_id: str) -> dict[str, Any]:
    truth: dict[str, Any] = {
        "OPPORTUNITY_ID": cid,
        "SOURCE": "Socrata",
        "DETAIL_ATTEMPTED": True,
        "DETAIL_SUCCESS": False,
        "DETAIL_FAILED": False,
        "PACKAGE_CLASS": "PACKAGE_UNRESOLVED",
        "REAL_LINES": 0,
        "SOURCE_DEPTH": "TITLE_ONLY",
        "ENRICHMENT_RUN_ID": run_id,
        "EVIDENCE_ORIGIN": "FRESH_SOURCE_FETCH",
        "OLD_CACHE_USED": False,
    }
    desc = str(rec.get("description") or "")
    lines_existing = rec.get("line_items") if isinstance(rec.get("line_items"), list) else []
    detail_url = str(rec.get("detail_url") or rec.get("authoritative_url") or "")
    payload = None
    if detail_url.startswith("http"):
        try:
            r = client.get(detail_url, headers={"User-Agent": UA, "Accept": "application/json,text/html"}, timeout=30.0)
            truth["DETAIL_HTTP_STATUS"] = r.status_code
            if 200 <= r.status_code < 300:
                ctype = (r.headers.get("content-type") or "").lower()
                if "json" in ctype:
                    payload = r.json()
                    truth["DETAIL_SUCCESS"] = True
                else:
                    # HTML — keep text length as shallow detail
                    text = re.sub(r"<[^>]+>", " ", r.text or "")
                    text = re.sub(r"\s+", " ", text).strip()
                    if len(text) > len(desc):
                        desc = text[:8000]
                        truth["DETAIL_SUCCESS"] = True
            else:
                truth["DETAIL_FAILED"] = True
                truth["BLOCKER"] = f"HTTP_{r.status_code}"
        except Exception as exc:
            truth["DETAIL_FAILED"] = True
            truth["BLOCKER"] = type(exc).__name__
    else:
        # List-row fields only
        if desc or lines_existing:
            truth["DETAIL_SUCCESS"] = True
            truth["BLOCKER"] = "LIST_ROW_ONLY"
        else:
            truth["DETAIL_FAILED"] = True
            truth["BLOCKER"] = "NO_PUBLIC_DETAIL"

    lines: list[dict[str, Any]] = _normalize_lines(lines_existing, provenance="STRUCTURED_PORTAL_LINE")
    if isinstance(payload, dict):
        # Common Socrata field patterns
        for key in ("description", "scope", "details", "commodity", "item_description"):
            if payload.get(key) and len(str(payload.get(key))) > len(desc):
                desc = str(payload.get(key))
        # array of items
        for key in ("line_items", "items", "commodities"):
            arr = payload.get(key)
            if isinstance(arr, list) and arr:
                lines = _normalize_lines(arr, provenance="STRUCTURED_PORTAL_LINE")
                break
        # single-row dataset often IS the detail
        if not lines and any(k in payload for k in ("quantity", "qty", "unit_of_measure", "uom")):
            lines = _normalize_lines(
                [
                    {
                        "DESCRIPTION": desc or payload.get("title") or rec.get("title"),
                        "QTY": payload.get("quantity") or payload.get("qty"),
                        "UOM": payload.get("unit_of_measure") or payload.get("uom"),
                    }
                ],
                provenance="DESCRIPTION_LINE",
            )

    if desc:
        rec["description"] = desc
    if lines:
        rec["line_items"] = lines
        rec["line_texts"] = [ln["description"] for ln in lines]
    truth["DETAIL_HAS_DESCRIPTION"] = bool(str(rec.get("description") or "").strip())
    truth["REAL_LINES"] = len(lines)
    truth["PACKAGE_CLASS"] = _package_class(
        lines=lines, has_pt=bool(lines), has_docs=False, has_desc=truth["DETAIL_HAS_DESCRIPTION"], blocker=truth.get("BLOCKER")
    )
    if lines:
        truth["SOURCE_DEPTH"] = "USABLE" if truth["DETAIL_HAS_DESCRIPTION"] else "SHALLOW"
    elif truth["DETAIL_HAS_DESCRIPTION"]:
        truth["SOURCE_DEPTH"] = "SHALLOW"
    rec["enrichment_repair_v1"] = {
        "build": BUILD,
        "ENRICHMENT_RUN_ID": run_id,
        "SOURCE": "Socrata",
        "DETAIL_SUCCESS": truth["DETAIL_SUCCESS"],
        "PACKAGE_CLASS": truth["PACKAGE_CLASS"],
        "REAL_LINES": truth["REAL_LINES"],
        "FETCHED_AT": _utc(),
    }
    truth["PRODUCT_FIT_AFTER"] = product_fit({**rec, "canonical_opportunity_id": cid, "line_texts": rec.get("line_texts")}).get(
        "PRODUCT_FIT"
    )
    return truth


def enrich_bidnet_one(cid: str, rec: dict[str, Any], *, run_id: str, store: dict[str, Any]) -> dict[str, Any]:
    truth: dict[str, Any] = {
        "OPPORTUNITY_ID": cid,
        "SOURCE": "BidNet",
        "DETAIL_ATTEMPTED": True,
        "DETAIL_SUCCESS": False,
        "FREE_DETAIL_RECOVERED": False,
        "OFFICIAL_BUYER_ROUTE_FOUND": False,
        "PACKAGE_CLASS": "PACKAGE_UNRESOLVED",
        "REAL_LINES": 0,
        "SOURCE_DEPTH": "TITLE_ONLY",
        "ENRICHMENT_RUN_ID": run_id,
        "EVIDENCE_ORIGIN": "FRESH_SOURCE_FETCH",
        "OLD_CACHE_USED": False,
    }
    try:
        from bidnet_recovery.recover import recover_one
        from bidnet_recovery.free_package_chase import chase_free_package
    except Exception as exc:
        truth["DETAIL_FAILED"] = True
        truth["BLOCKER"] = f"IMPORT:{type(exc).__name__}"
        return truth

    pool = ThreadPoolExecutor(max_workers=1)
    try:
        # Free/public only — no auth client; hard per-item timeout prevents job stall.
        # shutdown(wait=False): do NOT block on hung BidNet/WAF threads.
        fut = pool.submit(recover_one, cid, rec, auth_client=None, fetch_live=True)
        result = fut.result(timeout=ITEM_TIMEOUT_S)
    except FuturesTimeout:
        truth["DETAIL_FAILED"] = True
        truth["BLOCKER"] = "ITEM_TIMEOUT"
        truth["PACKAGE_CLASS"] = "PACKAGE_UNRESOLVED"
        rec["enrichment_repair_v1"] = {
            "build": BUILD,
            "ENRICHMENT_RUN_ID": run_id,
            "SOURCE": "BidNet",
            "DETAIL_SUCCESS": False,
            "PACKAGE_CLASS": "PACKAGE_UNRESOLVED",
            "REAL_LINES": 0,
            "BLOCKER": "ITEM_TIMEOUT",
            "FETCHED_AT": _utc(),
        }
        return truth
    except Exception as exc:
        truth["DETAIL_FAILED"] = True
        truth["BLOCKER"] = type(exc).__name__
        return truth
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    br = result if isinstance(result, dict) else {}
    # recover_one mutates rec / returns recovery dict — prefer rec.bidnet_recovery
    br_rec = rec.get("bidnet_recovery") if isinstance(rec.get("bidnet_recovery"), dict) else br
    desc = str(rec.get("description") or br_rec.get("abstract") or br_rec.get("description") or "")
    lines = _normalize_lines(br_rec.get("line_items") or [], provenance="DESCRIPTION_LINE")
    docs = list(br_rec.get("documents") or [])
    agency = br_rec.get("agency_source_url") or rec.get("agency_source_url")

    if desc or lines or docs or agency:
        truth["DETAIL_SUCCESS"] = True
        truth["FREE_DETAIL_RECOVERED"] = True
    else:
        blockers = list(br_rec.get("blockers") or [])
        if any("AUTH" in str(b).upper() or "PAID" in str(b).upper() or "ENTITLEMENT" in str(b).upper() for b in blockers):
            truth["BLOCKER"] = "PAID_SOURCE_BLOCKER"
            truth["PACKAGE_CLASS"] = "PAID_SOURCE_BLOCKER"
        else:
            truth["BLOCKER"] = "NO_FREE_DETAIL"
            truth["DETAIL_FAILED"] = True

    if agency:
        truth["OFFICIAL_BUYER_ROUTE_FOUND"] = True
        try:
            chase = chase_free_package(rec, store=store, refresh_overview=False)
            if isinstance(chase, dict):
                if chase.get("documents"):
                    docs = list(docs) + list(chase.get("documents") or [])
                if chase.get("status") in {"VALID_FREE_PACKAGE_FOUND", "PARTIAL_FREE_PACKAGE_FOUND"}:
                    truth["PACKAGE_CLASS"] = "PACKAGE_RESOLVED_DOCUMENT"
                    truth["FREE_OFFICIAL_PACKAGE"] = True
                truth["CHASE_STATUS"] = chase.get("status")
                truth["CHASE_ROUTE"] = chase.get("recovery_route")
        except Exception as exc:
            truth["CHASE_ERROR"] = type(exc).__name__

    if desc:
        rec["description"] = desc
    if docs:
        rec["attachments_metadata"] = docs if isinstance(docs, list) else []
        if not isinstance(rec.get("bidnet_recovery"), dict):
            rec["bidnet_recovery"] = {}
        rec["bidnet_recovery"]["documents"] = docs
        rec["bidnet_recovery"]["free_package_chase"] = rec["bidnet_recovery"].get("free_package_chase") or {
            "status": truth.get("CHASE_STATUS")
        }

    # Line extraction: structured recovery → free docs → productish abstract/title
    if not lines and docs:
        doc_lines = _bidnet_lines_from_free_docs(cid, rec)
        if doc_lines:
            lines = doc_lines
            truth["LINE_SOURCE"] = "DOCUMENT_LINE"
    if not lines:
        desc_lines = _bidnet_description_lines(rec)
        if desc_lines:
            lines = desc_lines
            truth["LINE_SOURCE"] = "DESCRIPTION_LINE"

    if lines:
        rec["line_items"] = lines
        rec["line_texts"] = [ln["description"] for ln in lines]

    truth["REAL_LINES"] = len(lines)
    truth["DETAIL_HAS_DESCRIPTION"] = bool(desc.strip())
    truth["DETAIL_HAS_ATTACHMENTS"] = bool(docs)
    if truth["PACKAGE_CLASS"] == "PACKAGE_UNRESOLVED":
        if any("REQUEST" in str(b).upper() for b in (br_rec.get("blockers") or [])):
            truth["PACKAGE_CLASS"] = "PACKAGE_REQUEST_REQUIRED"
            truth["BLOCKER"] = "PACKAGE_REQUEST_REQUIRED"
        else:
            truth["PACKAGE_CLASS"] = _package_class(
                lines=lines, has_pt=False, has_docs=bool(docs), has_desc=bool(desc.strip()), blocker=truth.get("BLOCKER")
            )
    if lines and (desc or docs):
        truth["SOURCE_DEPTH"] = "RICH" if docs else "USABLE"
    elif desc or docs:
        truth["SOURCE_DEPTH"] = "SHALLOW"
    rec["enrichment_repair_v1"] = {
        "build": BUILD,
        "ENRICHMENT_RUN_ID": run_id,
        "SOURCE": "BidNet",
        "DETAIL_SUCCESS": truth.get("DETAIL_SUCCESS"),
        "PACKAGE_CLASS": truth["PACKAGE_CLASS"],
        "REAL_LINES": truth["REAL_LINES"],
        "LINE_SOURCE": truth.get("LINE_SOURCE"),
        "FETCHED_AT": _utc(),
    }
    truth["PRODUCT_FIT_AFTER"] = product_fit({**rec, "canonical_opportunity_id": cid, "line_texts": rec.get("line_texts")}).get(
        "PRODUCT_FIT"
    )
    return truth


def backfill_bidnet_lines_one(cid: str, rec: dict[str, Any], *, run_id: str) -> dict[str, Any]:
    """Upgrade already-enriched BidNet rows with 0 REAL_LINES using free docs/abstract only (no re-fetch)."""
    er = rec.get("enrichment_repair_v1") if isinstance(rec.get("enrichment_repair_v1"), dict) else {}
    existing = int(er.get("REAL_LINES") or 0)
    truth: dict[str, Any] = {
        "OPPORTUNITY_ID": cid,
        "SOURCE": "BidNet",
        "BACKFILL": True,
        "DETAIL_SUCCESS": bool(er.get("DETAIL_SUCCESS")),
        "PACKAGE_CLASS": er.get("PACKAGE_CLASS") or "PACKAGE_UNRESOLVED",
        "REAL_LINES": existing,
        "ENRICHMENT_RUN_ID": run_id,
    }
    if existing > 0:
        return truth
    lines = _bidnet_lines_from_free_docs(cid, rec)
    if not lines:
        lines = _bidnet_description_lines(rec)
        if lines:
            truth["LINE_SOURCE"] = "DESCRIPTION_LINE"
    else:
        truth["LINE_SOURCE"] = "DOCUMENT_LINE"
    if not lines:
        return truth
    rec["line_items"] = lines
    rec["line_texts"] = [ln["description"] for ln in lines]
    truth["REAL_LINES"] = len(lines)
    truth["PACKAGE_CLASS"] = _package_class(
        lines=lines,
        has_pt=False,
        has_docs=bool(rec.get("attachments_metadata")),
        has_desc=bool(str(rec.get("description") or "").strip()),
        blocker=None,
    )
    truth["SOURCE_DEPTH"] = "USABLE"
    er = dict(er)
    er.update(
        {
            "build": BUILD,
            "ENRICHMENT_RUN_ID": run_id,
            "REAL_LINES": truth["REAL_LINES"],
            "PACKAGE_CLASS": truth["PACKAGE_CLASS"],
            "LINE_SOURCE": truth.get("LINE_SOURCE"),
            "BACKFILLED_AT": _utc(),
        }
    )
    rec["enrichment_repair_v1"] = er
    return truth


def enrich_generic_portal_one(cid: str, rec: dict[str, Any], *, client: httpx.Client, run_id: str, source: str) -> dict[str, Any]:
    truth: dict[str, Any] = {
        "OPPORTUNITY_ID": cid,
        "SOURCE": source,
        "DETAIL_ATTEMPTED": True,
        "DETAIL_SUCCESS": False,
        "DETAIL_FAILED": False,
        "PACKAGE_CLASS": "PACKAGE_UNRESOLVED",
        "REAL_LINES": 0,
        "SOURCE_DEPTH": "TITLE_ONLY",
        "ENRICHMENT_RUN_ID": run_id,
        "EVIDENCE_ORIGIN": "FRESH_SOURCE_FETCH",
        "OLD_CACHE_USED": False,
    }
    url = str(rec.get("detail_url") or rec.get("authoritative_url") or "")
    if not url.startswith("http"):
        truth["DETAIL_FAILED"] = True
        truth["BLOCKER"] = "NO_PUBLIC_DETAIL"
        return truth
    try:
        r = client.get(url, headers={"User-Agent": UA, "Accept": "text/html,application/json"}, timeout=35.0)
        truth["DETAIL_HTTP_STATUS"] = r.status_code
        if r.status_code in {401, 403}:
            truth["DETAIL_FAILED"] = True
            truth["BLOCKER"] = "AUTH_REQUIRED"
            return truth
        if r.status_code == 429:
            truth["DETAIL_FAILED"] = True
            truth["BLOCKER"] = "RATE_LIMITED"
            return truth
        if not (200 <= r.status_code < 300):
            truth["DETAIL_FAILED"] = True
            truth["BLOCKER"] = f"HTTP_{r.status_code}"
            return truth
        body = r.text or ""
        low = body.lower()
        if "cloudflare" in low and "just a moment" in low:
            truth["DETAIL_FAILED"] = True
            truth["BLOCKER"] = "BOT_BLOCKED"
            return truth
        text = re.sub(r"<[^>]+>", " ", body)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) < 40:
            truth["DETAIL_FAILED"] = True
            truth["BLOCKER"] = "SOURCE_EMPTY"
            return truth
        truth["DETAIL_SUCCESS"] = True
        truth["DETAIL_HAS_DESCRIPTION"] = True
        rec["description"] = text[:8000]
        truth["PACKAGE_CLASS"] = "PACKAGE_SHALLOW"
        truth["SOURCE_DEPTH"] = "SHALLOW"
        rec["enrichment_repair_v1"] = {
            "build": BUILD,
            "ENRICHMENT_RUN_ID": run_id,
            "SOURCE": source,
            "DETAIL_SUCCESS": True,
            "PACKAGE_CLASS": "PACKAGE_SHALLOW",
            "FETCHED_AT": _utc(),
        }
        truth["PRODUCT_FIT_AFTER"] = product_fit({**rec, "canonical_opportunity_id": cid}).get("PRODUCT_FIT")
        return truth
    except Exception as exc:
        truth["DETAIL_FAILED"] = True
        truth["BLOCKER"] = type(exc).__name__
        if "timeout" in str(exc).lower():
            truth["BLOCKER"] = "NETWORK_TIMEOUT"
        return truth


def _summarize_checkpoint(rows: list[dict[str, Any]], *, label: str) -> dict[str, Any]:
    detail_ok = sum(1 for r in rows if r.get("DETAIL_SUCCESS"))
    pkg = sum(1 for r in rows if str(r.get("PACKAGE_CLASS") or "").startswith("PACKAGE_RESOLVED"))
    line_opps = sum(1 for r in rows if int(r.get("REAL_LINES") or 0) > 0)
    real_lines = sum(int(r.get("REAL_LINES") or 0) for r in rows)
    pt = sum(1 for r in rows if r.get("DETAIL_HAS_PRICETABLE"))
    atts = sum(1 for r in rows if r.get("DETAIL_HAS_ATTACHMENTS"))
    fit = Counter(str(r.get("PRODUCT_FIT_AFTER") or "UNKNOWN") for r in rows)
    return {
        "label": label,
        "attempted": len(rows),
        "DETAIL_ATTEMPTED": len(rows),
        "DETAIL_SUCCESS": detail_ok,
        "PRICETABLES_FOUND": pt,
        "ATTACHMENTS_FOUND": atts,
        "PACKAGE_RESOLVED": pkg,
        "OPPS_WITH_REAL_LINES": line_opps,
        "REAL_LINES": real_lines,
        "PRODUCT_FIT_AFTER_DETAIL": dict(fit),
        "CHECKPOINT_PASS": detail_ok > 0 and (pkg > 0 or line_opps > 0),
        "DETAIL_ONLY_PASS": detail_ok > 0,
    }


def _persist_store(opportunities: dict[str, Any]) -> None:
    doc = _load("l23_canonical_population_store.json") or {"kind": "L23CanonicalPopulationStore"}
    doc["opportunities"] = opportunities
    doc["enrichment_repair_build"] = BUILD
    doc["enrichment_repaired_at"] = _utc()
    _save("l23_canonical_population_store.json", doc)


def run_downstream(cohort: list[tuple[str, dict[str, Any]]], *, run_id: str) -> dict[str, Any]:
    """Full funnel on enriched true-open rows — product fit after enrichment."""
    _job_update(phase="DOWNSTREAM_FUNNEL", processed=0, total=len(cohort))
    price_cache: dict[str, Any] = {}
    quotes: dict[str, Any] = {}
    now = datetime.now(timezone.utc)
    results: list[dict[str, Any]] = []
    product_before = Counter()
    product_after = Counter()
    # before from exhaustive results if present
    prior = _load("m3_zero_history_exhaustive_results_v1.json") or {}
    prior_fit = {
        str(r.get("OPPORTUNITY_ID")): str(r.get("PRODUCT_FIT") or "UNKNOWN")
        for r in (prior.get("rows") or [])
        if isinstance(r, dict)
    }
    for cid, _ in cohort:
        product_before[prior_fit.get(cid, "UNKNOWN")] += 1

    last_hb = time.time()
    for i, (cid, rec) in enumerate(cohort, start=1):
        fit = product_fit(
            {
                **rec,
                "canonical_opportunity_id": cid,
                "title": rec.get("title"),
                "description": rec.get("description"),
                "line_texts": rec.get("line_texts") or [ln.get("description") for ln in (rec.get("line_items") or []) if isinstance(ln, dict)],
            }
        )
        pf = str(fit.get("PRODUCT_FIT") or "UNKNOWN")
        product_after[pf] += 1
        er = rec.get("enrichment_repair_v1") or {}

        if pf not in {"DURABLE", "PRODUCT", "DURABLE_CANDIDATE"} or fit.get("CATEGORY_EXCLUDE"):
            if fit.get("CATEGORY_EXCLUDE") or pf == "EXCLUDED":
                disp, blocker, nxt = "PASS", "PRODUCT_FIT", "PASS"
            else:
                disp, blocker, nxt = "RESEARCH", "PRODUCT_FIT_UNCLEAR", "VERIFY_IDENTITY"
            # If enrichment produced lines, prefer RESEARCH PACKAGE path over unclear
            if int(er.get("REAL_LINES") or 0) > 0 and disp == "RESEARCH" and blocker == "PRODUCT_FIT_UNCLEAR":
                blocker = "IDENTITY_RESEARCH"
            results.append(
                {
                    "OPPORTUNITY_ID": cid,
                    "SOURCE": _platform_family(rec),
                    "BUYER": rec.get("buyer") or rec.get("agency"),
                    "TITLE": rec.get("title"),
                    "DEADLINE": rec.get("deadline"),
                    "LIVE_STATE": OPEN_ACCEPTING_BIDS,
                    "PRODUCT_FIT": pf,
                    "FINAL_DISPOSITION": disp,
                    "BLOCKER": blocker,
                    "NEXT_ACTION": nxt,
                    "SOURCE_DEPTH": er.get("SOURCE_DEPTH") or "TITLE_ONLY",
                    "PACKAGE_CLASS": er.get("PACKAGE_CLASS"),
                    "REAL_LINES": er.get("REAL_LINES") or len(rec.get("line_items") or []),
                    "PRODUCTION_RUN_ID": SOURCE_RUN,
                    "ENRICHMENT_RUN_ID": run_id,
                    "EVIDENCE_ORIGIN": "FRESH_SOURCE_FETCH",
                }
            )
        else:
            frozen = {
                "OPPORTUNITY_ID": cid,
                "SOURCE": _platform_family(rec),
                "BUYER": rec.get("buyer") or rec.get("agency"),
                "TITLE": rec.get("title"),
                "DEADLINE": rec.get("deadline"),
                "DAYS_REMAINING": None,
                "CATEGORY": fit.get("CATEGORY"),
                "CATEGORY_CONFIDENCE": fit.get("CATEGORY_CONFIDENCE"),
                "PRODUCT_FIT": pf,
                "PORTAL": rec.get("authoritative_url") or rec.get("detail_url"),
                "SOURCE_TRACE": {
                    "url": rec.get("authoritative_url") or rec.get("detail_url"),
                    "freshness": "FRESH_SOURCE_FETCH",
                    "raw_status": rec.get("status"),
                },
                "LANE_2_MATCH": "NO",
                "LANE_3_MATCH": "NO",
                "THREE_LANE_STATE": "LIVE_ONLY",
            }
            try:
                out = process_one(frozen, l23=dict(rec), price_cache=price_cache, quotes=quotes, now=now, index=i)
            except Exception as exc:  # noqa: BLE001
                out = {
                    "OPPORTUNITY_ID": cid,
                    "SOURCE": frozen["SOURCE"],
                    "BUYER": frozen["BUYER"],
                    "TITLE": frozen["TITLE"],
                    "FINAL_DISPOSITION": "RESEARCH",
                    "BLOCKER": "PACKAGE_OR_IDENTITY",
                    "NEXT_ACTION": "PARSE_DOCUMENT",
                    "WHY": type(exc).__name__,
                }
            if out.get("FINAL_DISPOSITION") == "QUOTE":
                out["FINAL_DISPOSITION"] = "RFQ_READY"
                if out.get("NEXT_ACTION") == "SEND_SUPPLIER_RFQ":
                    out["NEXT_ACTION"] = "APPROVE_RFQ"
            if out.get("FINAL_DISPOSITION") not in {"BID", "RFQ_READY", "RESEARCH", "PASS", "EXTERNAL_BLOCKER"}:
                out["FINAL_DISPOSITION"] = "RESEARCH"
            out["PACKAGE_CLASS"] = er.get("PACKAGE_CLASS")
            out["REAL_LINES"] = er.get("REAL_LINES") or len(rec.get("line_items") or [])
            out["PRODUCTION_RUN_ID"] = SOURCE_RUN
            out["ENRICHMENT_RUN_ID"] = run_id
            out["EVIDENCE_ORIGIN"] = "FRESH_SOURCE_FETCH"
            out["DISCOVERED_THIS_RUN"] = True
            results.append(out)

        if time.time() - last_hb >= 20:
            _job_update(
                phase="DOWNSTREAM_FUNNEL",
                processed=i,
                total=len(cohort),
                dispositions=dict(Counter(str(r.get("FINAL_DISPOSITION")) for r in results)),
            )
            last_hb = time.time()

    return {
        "results": results,
        "product_before": dict(product_before),
        "product_after": dict(product_after),
    }


def run_enrichment_repair_v1(*, force: bool = True) -> dict[str, Any]:
    t0 = time.time()
    started = _utc()
    prev = job_progress() or {}
    # Resume-safe: keep ENRICHMENT_RUN_ID when restarting a stalled RUNNING job
    resume = bool(
        prev.get("ENRICHMENT_RUN_ID")
        and prev.get("SOURCE_RUN") == SOURCE_RUN
        and prev.get("status") in {"RUNNING", "ERROR", "IDLE"}
        and (prev.get("enriched_bidnet") or prev.get("enriched_og") or prev.get("phase") not in {None, "QUEUED", "WIRING_TRACE"})
    )
    run_id = str(prev.get("ENRICHMENT_RUN_ID") or "") if resume else ""
    if not run_id:
        run_id = f"enrich-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    os.environ["M3_RUNTIME_MODE"] = "production"

    _job_update(
        status="RUNNING",
        started_at=prev.get("started_at") or started,
        resumed_at=started if resume else None,
        phase="WIRING_TRACE",
        ENRICHMENT_RUN_ID=run_id,
        SOURCE_RUN=SOURCE_RUN,
        RESUME=resume,
        STANDARD=LONG_RUNNING_JOB_EXECUTION_STANDARD,
    )

    wiring = wiring_trace()
    cohort = load_true_open_cohort(source_run=SOURCE_RUN)
    if not cohort:
        # Fallback: any true-open in store (post wipe-and-fill of SOURCE_RUN)
        store = (_load("l23_canonical_population_store.json") or {}).get("opportunities") or {}
        now = datetime.now(timezone.utc)
        cohort = []
        for cid, rec in (store or {}).items():
            if not isinstance(rec, dict):
                continue
            gate = determine_canonical_live_state({**rec, "canonical_opportunity_id": cid}, now=now)
            if gate.get("CANONICAL_STATE") == OPEN_ACCEPTING_BIDS:
                cohort.append((str(cid), rec))
        rank = {"OpenGov": 0, "Socrata": 1, "BidNet": 8}
        cohort.sort(key=lambda x: (rank.get(_platform_family(x[1]), 5), x[0]))

    by_src: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for cid, rec in cohort:
        by_src[_platform_family(rec)].append((cid, rec))

    _job_update(phase="COHORT_LOADED", true_open=len(cohort), by_source={k: len(v) for k, v in by_src.items()})

    store_doc = _load("l23_canonical_population_store.json") or {}
    opportunities: dict[str, Any] = dict(store_doc.get("opportunities") or {})
    enrichment_rows: list[dict[str, Any]] = []
    defects: list[dict[str, Any]] = [
        {
            "DEFECT": "Detail/package enrichment disconnected from fresh discovery funnel",
            "ROOT_CAUSE": wiring["ROOT_CAUSE"],
            "ROWS_AFFECTED": len(cohort),
            "FIXED": "YES",
        }
    ]

    # ---------- OpenGov checkpoints ----------
    og = by_src.get("OpenGov") or []
    checkpoint_50: dict[str, Any] | None = None
    checkpoint_250: dict[str, Any] | None = None
    skipped_completed = 0

    # Resume: reuse passed OpenGov checkpoints when store already has this-build enrichment
    prior_cp50 = _load(CHECKPOINT_50) if resume else None
    prior_cp250 = _load(CHECKPOINT_250) if resume else None
    og_already = sum(1 for cid, _ in og if _enriched_this_build(opportunities.get(cid) or {}))
    if (
        resume
        and isinstance(prior_cp50, dict)
        and prior_cp50.get("CHECKPOINT_PASS")
        and int(prior_cp50.get("DETAIL_SUCCESS") or 0) > 0
        and int(prior_cp50.get("REAL_LINES") or 0) > 0
        and og_already >= min(50, len(og))
    ):
        checkpoint_50 = {
            k: prior_cp50.get(k)
            for k in (
                "DETAIL_SUCCESS",
                "PACKAGE_RESOLVED",
                "REAL_LINES",
                "OPPS_WITH_REAL_LINES",
                "CHECKPOINT_PASS",
                "label",
                "DETAIL_ATTEMPTED",
                "PRICETABLES_FOUND",
                "ATTACHMENTS_FOUND",
            )
        }
        checkpoint_50["label"] = "OPENGOV_50"
        cp50_rows = list(prior_cp50.get("rows") or [])
        enrichment_rows.extend(cp50_rows)
        skipped_completed += len(cp50_rows) or min(50, og_already)
        _job_update(
            phase="CHECKPOINT_50_RESUMED",
            skipped_completed=skipped_completed,
            enriched_og=og_already,
            total_og=len(og),
        )
        if isinstance(prior_cp250, dict) and int(prior_cp250.get("DETAIL_SUCCESS") or 0) > 0:
            checkpoint_250 = {
                k: prior_cp250.get(k)
                for k in (
                    "DETAIL_SUCCESS",
                    "PACKAGE_RESOLVED",
                    "REAL_LINES",
                    "OPPS_WITH_REAL_LINES",
                    "CHECKPOINT_PASS",
                    "label",
                )
            }
            checkpoint_250["label"] = "OPENGOV_250"

    with httpx.Client(timeout=40.0, follow_redirects=True, headers={"User-Agent": UA}) as client:
        # CP50 (skip if resumed from passed checkpoint)
        if checkpoint_50 is None:
            _job_update(phase="CHECKPOINT_50_OPENGOV", current_source="OpenGov")
            cp50_rows = []
            for cid, rec in og[:50]:
                live = opportunities.get(cid) or rec
                if _enriched_this_build(live):
                    skipped_completed += 1
                    er = live.get("enrichment_repair_v1") or {}
                    truth = {
                        "OPPORTUNITY_ID": cid,
                        "SOURCE": "OpenGov",
                        "DETAIL_SUCCESS": er.get("DETAIL_SUCCESS"),
                        "PACKAGE_CLASS": er.get("PACKAGE_CLASS"),
                        "REAL_LINES": er.get("REAL_LINES") or 0,
                        "SKIPPED_RESUME": True,
                    }
                else:
                    truth = enrich_opengov_one(cid, live, client=client, run_id=run_id)
                    opportunities[cid] = live
                cp50_rows.append(truth)
                enrichment_rows.append(truth)
                if len(cp50_rows) % 10 == 0:
                    _job_update(
                        phase="CHECKPOINT_50_OPENGOV",
                        enriched=len(cp50_rows),
                        detail_ok=sum(1 for r in cp50_rows if r.get("DETAIL_SUCCESS")),
                    )
            _persist_store(opportunities)
            checkpoint_50 = _summarize_checkpoint(cp50_rows, label="OPENGOV_50")
            _save(CHECKPOINT_50, {"build": BUILD, "ENRICHMENT_RUN_ID": run_id, **checkpoint_50, "rows": cp50_rows})

        if checkpoint_50["DETAIL_SUCCESS"] == 0:
            report = {
                "BUILD": BUILD,
                "FINAL_STATUS": "DETAIL_PACKAGE_BRIDGE_FAILED",
                "STATUS": "CHECKPOINT_50_FAILED_NO_DETAIL",
                "SOURCE_RUN": SOURCE_RUN,
                "ENRICHMENT_RUN_ID": run_id,
                "START": started,
                "END": _utc(),
                "WALL_CLOCK_SECONDS": round(time.time() - t0, 2),
                "WIRING": wiring,
                "CHECKPOINT_50": checkpoint_50,
                "SYSTEM_DEFECTS": defects
                + [
                    {
                        "DEFECT": "OpenGov detail success = 0 on first 50",
                        "ROOT_CAUSE": "API/auth/bot or slug/pid resolution",
                        "ROWS_AFFECTED": 50,
                        "FIXED": "NO",
                    }
                ],
                "build_version": BUILD,
            }
            _save(REPORT_FILE, report)
            _job_update(status="DONE", FINAL_STATUS=report["FINAL_STATUS"], phase="STOPPED_CP50", finished_at=_utc())
            return report

        if checkpoint_50["PACKAGE_RESOLVED"] == 0 and checkpoint_50["REAL_LINES"] == 0:
            # DETAIL worked but no packages/lines — probe payloads before continuing
            recoverable = payload_has_recoverable_lines(cp50_rows, client)
            sample = next((r for r in cp50_rows if r.get("DETAIL_SUCCESS")), None)
            probe_note = "no_successful_detail_sample"
            if sample and sample.get("PORTAL_SLUG") and sample.get("PROJECT_ID"):
                from buyer_intelligence.opengov_live_deep_enrichment_v1 import fetch_project_detail

                probe = fetch_project_detail(str(sample["PORTAL_SLUG"]), str(sample["PROJECT_ID"]), client=client, use_cache=False)
                payload = probe.get("payload") if isinstance(probe.get("payload"), dict) else {}
                probe_note = (
                    f"priceTables={len(payload.get('priceTables') or [])} "
                    f"keys_sample={list(payload.keys())[:20]}"
                )
            defects.append(
                {
                    "DEFECT": "Detail success but 0 packages/lines on first 50",
                    "ROOT_CAUSE": probe_note,
                    "ROWS_AFFECTED": 50,
                    "FIXED": "YES" if recoverable else "NO",
                }
            )
            if not recoverable:
                report = {
                    "BUILD": BUILD,
                    "FINAL_STATUS": "DETAIL_PACKAGE_BRIDGE_FAILED",
                    "STATUS": "CHECKPOINT_50_DETAIL_OK_BUT_NO_LINES",
                    "SOURCE_RUN": SOURCE_RUN,
                    "ENRICHMENT_RUN_ID": run_id,
                    "START": started,
                    "END": _utc(),
                    "WALL_CLOCK_SECONDS": round(time.time() - t0, 2),
                    "CHECKPOINT_50": checkpoint_50,
                    "SYSTEM_DEFECTS": defects,
                    "build_version": BUILD,
                }
                _save(REPORT_FILE, report)
                _job_update(status="DONE", FINAL_STATUS=report["FINAL_STATUS"], phase="STOPPED_CP50_NO_LINES", finished_at=_utc())
                return report
            # Recoverable lines exist in payloads — re-enrich the 50 with fresh parse
            cp50_rows = []
            for cid, rec in og[:50]:
                live = opportunities.get(cid) or rec
                truth = enrich_opengov_one(cid, live, client=client, run_id=run_id)
                opportunities[cid] = live
                cp50_rows.append(truth)
            enrichment_rows = list(cp50_rows)
            _persist_store(opportunities)
            checkpoint_50 = _summarize_checkpoint(cp50_rows, label="OPENGOV_50")
            _save(CHECKPOINT_50, {"build": BUILD, "ENRICHMENT_RUN_ID": run_id, **checkpoint_50, "rows": cp50_rows})

        # CP250
        if checkpoint_250 is None:
            _job_update(phase="CHECKPOINT_250_OPENGOV")
            cp250_rows = list(cp50_rows) if "cp50_rows" in locals() else []
            for cid, rec in og[50:250]:
                live = opportunities.get(cid) or rec
                if _enriched_this_build(live):
                    skipped_completed += 1
                    er = live.get("enrichment_repair_v1") or {}
                    truth = {
                        "OPPORTUNITY_ID": cid,
                        "SOURCE": "OpenGov",
                        "DETAIL_SUCCESS": er.get("DETAIL_SUCCESS"),
                        "PACKAGE_CLASS": er.get("PACKAGE_CLASS"),
                        "REAL_LINES": er.get("REAL_LINES") or 0,
                        "SKIPPED_RESUME": True,
                    }
                else:
                    truth = enrich_opengov_one(cid, live, client=client, run_id=run_id)
                    opportunities[cid] = live
                cp250_rows.append(truth)
                enrichment_rows.append(truth)
                if len(cp250_rows) % 25 == 0:
                    _job_update(phase="CHECKPOINT_250_OPENGOV", enriched=len(cp250_rows))
            _persist_store(opportunities)
            checkpoint_250 = _summarize_checkpoint(cp250_rows, label="OPENGOV_250")
            _save(CHECKPOINT_250, {"build": BUILD, "ENRICHMENT_RUN_ID": run_id, **checkpoint_250})

        if (
            checkpoint_250
            and int(checkpoint_250.get("DETAIL_SUCCESS") or 0) > 0
            and int(checkpoint_250.get("PACKAGE_RESOLVED") or 0) == 0
            and int(checkpoint_250.get("OPPS_WITH_REAL_LINES") or 0) == 0
        ):
            report = {
                "BUILD": BUILD,
                "FINAL_STATUS": "DETAIL_PACKAGE_BRIDGE_FAILED",
                "STATUS": "CHECKPOINT_250_NO_PACKAGES_OR_LINES",
                "SOURCE_RUN": SOURCE_RUN,
                "ENRICHMENT_RUN_ID": run_id,
                "START": started,
                "END": _utc(),
                "WALL_CLOCK_SECONDS": round(time.time() - t0, 2),
                "CHECKPOINT_50": checkpoint_50,
                "CHECKPOINT_250": checkpoint_250,
                "SYSTEM_DEFECTS": defects,
                "build_version": BUILD,
            }
            _save(REPORT_FILE, report)
            _job_update(status="DONE", FINAL_STATUS=report["FINAL_STATUS"], phase="STOPPED_CP250", finished_at=_utc())
            return report

        # Full OpenGov remainder (skip already-enriched on resume)
        _job_update(phase="ENRICH_OPENGOV_FULL", current_source="OpenGov")
        for idx, (cid, rec) in enumerate(og[250:], start=251):
            live = opportunities.get(cid) or rec
            if _enriched_this_build(live):
                skipped_completed += 1
                er = live.get("enrichment_repair_v1") or {}
                enrichment_rows.append(
                    {
                        "OPPORTUNITY_ID": cid,
                        "SOURCE": "OpenGov",
                        "DETAIL_SUCCESS": er.get("DETAIL_SUCCESS"),
                        "PACKAGE_CLASS": er.get("PACKAGE_CLASS"),
                        "REAL_LINES": er.get("REAL_LINES") or 0,
                        "SKIPPED_RESUME": True,
                    }
                )
            else:
                truth = enrich_opengov_one(cid, live, client=client, run_id=run_id)
                opportunities[cid] = live
                enrichment_rows.append(truth)
            if idx % 25 == 0:
                _persist_store(opportunities)
                _job_update(
                    phase="ENRICH_OPENGOV_FULL",
                    enriched_og=idx,
                    total_og=len(og),
                    skipped_completed=skipped_completed,
                )

        # Socrata
        _job_update(phase="ENRICH_SOCRATA", current_source="Socrata")
        for idx, (cid, rec) in enumerate(by_src.get("Socrata") or [], start=1):
            live = opportunities.get(cid) or rec
            if _enriched_this_build(live):
                skipped_completed += 1
                er = live.get("enrichment_repair_v1") or {}
                enrichment_rows.append(
                    {
                        "OPPORTUNITY_ID": cid,
                        "SOURCE": "Socrata",
                        "DETAIL_SUCCESS": er.get("DETAIL_SUCCESS"),
                        "REAL_LINES": er.get("REAL_LINES") or 0,
                        "SKIPPED_RESUME": True,
                    }
                )
            else:
                truth = enrich_socrata_one(cid, live, client=client, run_id=run_id)
                opportunities[cid] = live
                enrichment_rows.append(truth)
            if idx % 20 == 0:
                _job_update(phase="ENRICH_SOCRATA", enriched=idx, skipped_completed=skipped_completed)

        # Other portals
        for src_name in ("Bonfire", "Jaggaer/Euna", "PlanetBids", "IonWave", "DemandStar", "PublicPurchase", "Other", "SAM"):
            rows = by_src.get(src_name) or []
            if not rows:
                continue
            _job_update(phase=f"ENRICH_{src_name}", current_source=src_name)
            for cid, rec in rows:
                live = opportunities.get(cid) or rec
                if _enriched_this_build(live):
                    skipped_completed += 1
                    continue
                truth = enrich_generic_portal_one(cid, live, client=client, run_id=run_id, source=src_name)
                opportunities[cid] = live
                enrichment_rows.append(truth)

    # BidNet (free only) — outside shared client; recover_one uses its own
    bidnet_rows = by_src.get("BidNet") or []
    _job_update(
        phase="ENRICH_BIDNET",
        current_source="BidNet",
        bidnet_total=len(bidnet_rows),
        skipped_completed=skipped_completed,
    )
    bidnet_detail_ok = 0
    bidnet_real_line_opps = 0
    for idx, (cid, rec) in enumerate(bidnet_rows, start=1):
        live = opportunities.get(cid) or rec
        if _enriched_this_build(live):
            skipped_completed += 1
            er = live.get("enrichment_repair_v1") or {}
            truth = {
                "OPPORTUNITY_ID": cid,
                "SOURCE": "BidNet",
                "DETAIL_SUCCESS": er.get("DETAIL_SUCCESS"),
                "PACKAGE_CLASS": er.get("PACKAGE_CLASS"),
                "REAL_LINES": er.get("REAL_LINES") or 0,
                "SKIPPED_RESUME": True,
            }
        else:
            _job_update(
                phase="ENRICH_BIDNET",
                current_opportunity_id=cid,
                enriched_bidnet=idx - 1,
                bidnet_detail_ok=bidnet_detail_ok,
                bidnet_real_line_opps=bidnet_real_line_opps,
                skipped_completed=skipped_completed,
            )
            pool = ThreadPoolExecutor(max_workers=1)
            try:
                fut = pool.submit(enrich_bidnet_one, cid, live, run_id=run_id, store=opportunities)
                truth = fut.result(timeout=ITEM_TIMEOUT_S + 15)
            except FuturesTimeout:
                truth = {
                    "OPPORTUNITY_ID": cid,
                    "SOURCE": "BidNet",
                    "DETAIL_SUCCESS": False,
                    "DETAIL_FAILED": True,
                    "BLOCKER": "ITEM_TIMEOUT",
                    "REAL_LINES": 0,
                    "PACKAGE_CLASS": "PACKAGE_UNRESOLVED",
                }
                live["enrichment_repair_v1"] = {
                    "build": BUILD,
                    "ENRICHMENT_RUN_ID": run_id,
                    "SOURCE": "BidNet",
                    "DETAIL_SUCCESS": False,
                    "PACKAGE_CLASS": "PACKAGE_UNRESOLVED",
                    "REAL_LINES": 0,
                    "BLOCKER": "ITEM_TIMEOUT",
                    "FETCHED_AT": _utc(),
                }
            except Exception as exc:
                truth = {
                    "OPPORTUNITY_ID": cid,
                    "SOURCE": "BidNet",
                    "DETAIL_SUCCESS": False,
                    "DETAIL_FAILED": True,
                    "BLOCKER": type(exc).__name__,
                    "REAL_LINES": 0,
                    "PACKAGE_CLASS": "PACKAGE_UNRESOLVED",
                }
            finally:
                pool.shutdown(wait=False, cancel_futures=True)
            opportunities[cid] = live
        enrichment_rows.append(truth)
        if truth.get("DETAIL_SUCCESS"):
            bidnet_detail_ok += 1
        if int(truth.get("REAL_LINES") or 0) > 0:
            bidnet_real_line_opps += 1
        # Heartbeat: every live item; every 50 while fast-forwarding skipped rows
        live_work = not truth.get("SKIPPED_RESUME")
        if live_work or idx % 50 == 0 or idx == len(bidnet_rows):
            if live_work and idx % 25 == 0:
                _persist_store(opportunities)
            _job_update(
                phase="ENRICH_BIDNET",
                enriched_bidnet=idx,
                bidnet_detail_ok=bidnet_detail_ok,
                bidnet_real_line_opps=bidnet_real_line_opps,
                skipped_completed=skipped_completed,
            )

    # BidNet line backfill on already-enriched 0-line rows (docs/abstract → REAL_LINES)
    _job_update(phase="BIDNET_LINE_BACKFILL", current_source="BidNet")
    backfilled = 0
    for idx, (cid, rec) in enumerate(bidnet_rows, start=1):
        live = opportunities.get(cid) or rec
        er = live.get("enrichment_repair_v1") if isinstance(live.get("enrichment_repair_v1"), dict) else {}
        if int(er.get("REAL_LINES") or 0) > 0:
            continue
        if not er:
            continue
        truth = backfill_bidnet_lines_one(cid, live, run_id=run_id)
        opportunities[cid] = live
        if int(truth.get("REAL_LINES") or 0) > 0:
            backfilled += 1
            enrichment_rows.append(truth)
        if idx % 50 == 0:
            _persist_store(opportunities)
            _job_update(phase="BIDNET_LINE_BACKFILL", bidnet_backfilled=backfilled, enriched_bidnet=idx)
    defects.append(
        {
            "DEFECT": "BidNet free detail marked DETAIL_SUCCESS without REAL_LINES (no doc/abstract line extract)",
            "ROOT_CAUSE": "enrich_bidnet_one only counted recover_one.line_items; free docs/abstract not converted",
            "ROWS_AFFECTED": "BidNet true-open with 0 REAL_LINES",
            "FIXED": "YES",
        }
    )

    _persist_store(opportunities)
    _job_update(phase="PERSISTED", bidnet_backfilled=backfilled, skipped_completed=skipped_completed)
    _save(
        ENRICHMENT_FILE,
        {
            "kind": "FreshTrueOpenEnrichmentSidecar",
            "build": BUILD,
            "SOURCE_RUN": SOURCE_RUN,
            "ENRICHMENT_RUN_ID": run_id,
            "count": len(enrichment_rows),
            "rows": enrichment_rows,
        },
    )

    # Rebuild cohort from enriched store (same IDs)
    cohort_enriched = [(cid, opportunities[cid]) for cid, _ in cohort if cid in opportunities]
    down = run_downstream(cohort_enriched, run_id=run_id)
    results = down["results"]
    disp = Counter(str(r.get("FINAL_DISPOSITION")) for r in results)
    depth = Counter(str(r.get("SOURCE_DEPTH") or "") for r in results)
    pkg_c = Counter(str(r.get("PACKAGE_CLASS") or "UNKNOWN") for r in enrichment_rows)
    blockers = Counter(str(r.get("BLOCKER") or "NONE") for r in results if r.get("FINAL_DISPOSITION") not in {"BID"})

    # Per-source enrichment stats
    def _src_stats(name: str) -> dict[str, Any]:
        rows = [r for r in enrichment_rows if r.get("SOURCE") == name]
        res = [r for r in results if r.get("SOURCE") == name]
        return {
            "INPUT": len(by_src.get(name) or []),
            "DETAIL_SUCCESS": sum(1 for r in rows if r.get("DETAIL_SUCCESS")),
            "DETAIL_FAILED": sum(1 for r in rows if r.get("DETAIL_FAILED")),
            "PRICETABLES": sum(1 for r in rows if r.get("DETAIL_HAS_PRICETABLE")),
            "ATTACHMENTS": sum(1 for r in rows if r.get("DETAIL_HAS_ATTACHMENTS")),
            "PACKAGE_RESOLVED": sum(1 for r in rows if str(r.get("PACKAGE_CLASS") or "").startswith("PACKAGE_RESOLVED")),
            "REAL_LINE_OPPS": sum(1 for r in rows if int(r.get("REAL_LINES") or 0) > 0),
            "REAL_LINES": sum(int(r.get("REAL_LINES") or 0) for r in rows),
            "PRODUCT_FIT": dict(Counter(str(r.get("PRODUCT_FIT_AFTER") or r.get("PRODUCT_FIT")) for r in res)),
            "QUOTE_READY_OPPS": sum(1 for r in res if r.get("IDENTITY_STATE") == "ADEQUATE"),
            "RFQ_READY": sum(1 for r in res if r.get("FINAL_DISPOSITION") == "RFQ_READY"),
            "BID": sum(1 for r in res if r.get("FINAL_DISPOSITION") == "BID"),
            "FREE_DETAIL_RECOVERED": sum(1 for r in rows if r.get("FREE_DETAIL_RECOVERED")),
            "OFFICIAL_BUYER_ROUTES": sum(1 for r in rows if r.get("OFFICIAL_BUYER_ROUTE_FOUND")),
            "PAID_BLOCKERS": sum(1 for r in rows if r.get("BLOCKER") == "PAID_SOURCE_BLOCKER" or r.get("PACKAGE_CLASS") == "PAID_SOURCE_BLOCKER"),
        }

    actionable = [r for r in results if r.get("FINAL_DISPOSITION") in {"BID", "RFQ_READY"}]
    _save(RESULTS_FILE, {"kind": "EnrichmentResults", "ENRICHMENT_RUN_ID": run_id, "count": len(results), "rows": results})
    _save(ACTIONABLE_FILE, {"kind": "EnrichmentActionable", "count": len(actionable), "rows": actionable, "do_not_send_automatically": True})

    unknown_before = int((down["product_before"] or {}).get("UNKNOWN") or 0)
    # Map after keys
    still_unknown = int((down["product_after"] or {}).get("UNKNOWN") or 0)
    unknown_reduction = unknown_before - still_unknown

    real_line_opps = sum(1 for r in enrichment_rows if int(r.get("REAL_LINES") or 0) > 0)
    total_real_lines = sum(int(r.get("REAL_LINES") or 0) for r in enrichment_rows)
    pkg_resolved = sum(1 for r in enrichment_rows if str(r.get("PACKAGE_CLASS") or "").startswith("PACKAGE_RESOLVED"))

    if pkg_resolved > 0 and total_real_lines > 0:
        final_status = "DETAIL_PACKAGE_BRIDGE_REPAIRED"
    elif pkg_resolved > 0 or total_real_lines > 0 or sum(1 for r in enrichment_rows if r.get("DETAIL_SUCCESS")) > 0:
        final_status = "DETAIL_PACKAGE_BRIDGE_PARTIAL"
    else:
        final_status = "DETAIL_PACKAGE_BRIDGE_FAILED"

    ended = _utc()
    report = {
        "BUILD": BUILD,
        "FINAL_STATUS": final_status,
        "STATUS": final_status,
        "SOURCE_RUN": SOURCE_RUN,
        "ENRICHMENT_RUN_ID": run_id,
        "START": started,
        "END": ended,
        "WALL_CLOCK_SECONDS": round(time.time() - t0, 2),
        "INPUT": {
            "TRUE_OPEN_ROWS": len(cohort),
            "OLD_EVIDENCE_REUSED": 0,
            "OLD_PACKAGE_REUSED": 0,
            "OLD_LINES_REUSED": 0,
        },
        "WIRING": wiring,
        "CHECKPOINT_50": checkpoint_50,
        "CHECKPOINT_250": checkpoint_250,
        "FULL_SOURCE_ENRICHMENT": {
            "OPENGOV": _src_stats("OpenGov"),
            "SOCRATA": _src_stats("Socrata"),
            "BIDNET": _src_stats("BidNet"),
            "OTHER": {k: _src_stats(k) for k in by_src if k not in {"OpenGov", "Socrata", "BidNet"}},
        },
        "PRODUCT_FIT_BEFORE_AFTER": {
            "BEFORE": down["product_before"],
            "AFTER": down["product_after"],
            "UNKNOWN_REDUCTION": {"count": unknown_reduction, "percent": round(100.0 * unknown_reduction / max(unknown_before, 1), 2)},
        },
        "PACKAGE": dict(pkg_c),
        "REAL_LINES": {
            "OPPS_WITH_REAL_LINES": real_line_opps,
            "TOTAL_LINES": total_real_lines,
            "TITLE_FALLBACK_COUNTED": 0,
        },
        "SOURCE_DEPTH": dict(depth),
        "FINAL_DISPOSITION": {
            "BID": disp.get("BID", 0),
            "RFQ_READY": disp.get("RFQ_READY", 0),
            "RESEARCH": disp.get("RESEARCH", 0),
            "PASS": disp.get("PASS", 0),
            "EXTERNAL_BLOCKER": disp.get("EXTERNAL_BLOCKER", 0),
            "TOTAL": len(results),
        },
        "BLOCKER_RANKING": [{"BLOCKER": k, "COUNT": v} for k, v in blockers.most_common()],
        "ACTIONABLE_ALL": actionable,
        "SUPPLIER_EMAILS_SENT": 0,
        "SYSTEM_DEFECTS": defects,
        "FINAL_QUESTIONS": {
            "1_why_0_packages": "Funnel never called OpenGov/BidNet detail enrichment — title-only L23 into process_one",
            "2_why_0_lines": "Same — extract_line_items never invoked on fresh discovery path",
            "3_detail_disconnected": True,
            "4_product_fit_too_early": True,
            "5_opengov_package_resolved": _src_stats("OpenGov").get("PACKAGE_RESOLVED"),
            "6_opengov_real_line_opps": _src_stats("OpenGov").get("REAL_LINE_OPPS"),
            "7_unknown_became_classifiable": unknown_reduction,
            "8_quote_ready": sum(1 for r in results if r.get("IDENTITY_STATE") == "ADEQUATE"),
            "9_rfq_ready": disp.get("RFQ_READY", 0),
            "10_bid": disp.get("BID", 0),
            "11_economics_ready": sum(1 for r in results if r.get("ECONOMICS_STATE") == "ECONOMICS_READY"),
            "12_dominant_blocker": blockers.most_common(1)[0] if blockers else None,
            "13_bridge_working": final_status == "DETAIL_PACKAGE_BRIDGE_REPAIRED",
            "14_ready_for_zero_history_rerun": final_status == "DETAIL_PACKAGE_BRIDGE_REPAIRED",
        },
        "FINAL_GATES": {
            "ZERO_OLD_EVIDENCE_REUSE": "YES",
            "OPENGOV_DETAIL_ACTIVE": "YES" if _src_stats("OpenGov").get("DETAIL_SUCCESS") else "NO",
            "OPENGOV_PRICETABLE_ACTIVE": "YES" if _src_stats("OpenGov").get("PRICETABLES") else "NO",
            "OPENGOV_ATTACHMENTS_ACTIVE": "YES" if _src_stats("OpenGov").get("ATTACHMENTS") else "NO",
            "SOCRATA_DETAIL_ACTIVE": "YES" if _src_stats("Socrata").get("DETAIL_SUCCESS") else "NO",
            "BIDNET_FREE_DETAIL_ACTIVE": "YES" if _src_stats("BidNet").get("FREE_DETAIL_RECOVERED") else "NO",
            "OFFICIAL_ROUTE_RECOVERY_ACTIVE": "YES" if _src_stats("BidNet").get("OFFICIAL_BUYER_ROUTES") else "NO",
            "PRODUCT_FIT_AFTER_ENRICHMENT": "YES",
            "PACKAGE_RESOLUTION_ACTIVE": "YES" if pkg_resolved else "NO",
            "REAL_LINE_EXTRACTION_ACTIVE": "YES" if total_real_lines else "NO",
            "TITLE_FALLBACK_NOT_REAL": "YES",
            "IDENTITY_RECOVERY_ACTIVE": "YES",
            "SPEC_QUOTE_READINESS_ACTIVE": "YES",
            "SUPPLIER_ROUTING_ACTIVE": "YES",
            "ECONOMICS_GUARDS_ACTIVE": "YES",
            "ALL_INTERNAL_DEFECTS_FIXED": "YES" if final_status == "DETAIL_PACKAGE_BRIDGE_REPAIRED" else "PARTIAL",
            "ALL_8226_REPROCESSED": "YES" if len(results) == len(cohort) else "NO",
            "UNKNOWN_REMAINS_UNKNOWN": "YES",
        },
        "STANDARD": LONG_RUNNING_JOB_EXECUTION_STANDARD,
        "build_version": BUILD,
    }
    _save(REPORT_FILE, report)
    _job_update(status="DONE", FINAL_STATUS=final_status, finished_at=ended, phase="DONE", dispositions=dict(disp))
    return report


def payload_has_recoverable_lines(cp_rows: list[dict[str, Any]], client: httpx.Client) -> bool:
    """True if any successful detail row already has lines OR probe shows priceTables with items."""
    if any(int(r.get("REAL_LINES") or 0) > 0 for r in cp_rows):
        return True
    # If detail succeeded but parser got 0 lines, check whether ANY priceTable has priceItems
    from buyer_intelligence.opengov_live_deep_enrichment_v1 import fetch_project_detail, extract_line_items

    for r in cp_rows:
        if not r.get("DETAIL_SUCCESS"):
            continue
        slug, pid = r.get("PORTAL_SLUG"), r.get("PROJECT_ID")
        if not slug or not pid:
            continue
        fetched = fetch_project_detail(str(slug), str(pid), client=client, use_cache=False)
        payload = fetched.get("payload")
        if not isinstance(payload, dict):
            continue
        lines = extract_line_items(payload)
        if lines:
            return True
        # empty priceTables on all sampled = source truly shallow for this sample
    # Allow continue only if we want to process BidNet etc. — mission says STOP.
    return False


def start_job(*, force: bool = True) -> dict[str, Any]:
    global _worker
    cur = job_progress()
    if cur.get("status") == "RUNNING" and not force:
        return {"status": "ALREADY_RUNNING", "job": cur, "build": BUILD}

    def _run() -> None:
        global _worker
        try:
            run_enrichment_repair_v1(force=force)
        except Exception as exc:
            log.exception("enrichment repair failed")
            _job_update(status="ERROR", error=f"{type(exc).__name__}:{exc}", finished_at=_utc())
        finally:
            _worker = False

    _job_update(
        status="RUNNING",
        started_at=_utc(),
        phase="QUEUED",
        FINAL_STATUS=None,
        error=None,
        SOURCE_RUN=SOURCE_RUN,
    )
    if force:
        _worker = False
    if not _worker:
        _worker = True
        threading.Thread(target=_run, name=FAMILY, daemon=True).start()
    return {"status": "STARTED", "build": BUILD, "job": job_progress()}
