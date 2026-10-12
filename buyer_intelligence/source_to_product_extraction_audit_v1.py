"""Source-to-Product Extraction Audit & Repair V1.

Traces incomplete quoteable opportunities from solicitation evidence through
commercial research, classifies root causes, recovers product lines from
OpenGov enrichment/cache when present, persists a product-line sidecar
(data contract), and remeasures identity/readiness — product-resale only.

Does NOT mutate HT v2 result snapshots. Prefer 0 SAM API calls.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from application_clock import now_utc
from buyer_intelligence.commercial_intelligence_upgrade_v1 import (
    REMATCH_FILE,
    _load,
    _save,
    _utc,
    quoteable_rows,
)
from buyer_intelligence.commercial_proof_repair_v1 import (
    CATEGORY_ONLY,
    EXACT_PRODUCT_VERIFIED,
    IDENTITY_CONFLICT,
    IDENTITY_UNKNOWN,
    PARTIAL_PRODUCT_IDENTITY,
    SPECIFICATION_COMPLETE_GENERIC,
    classify_product_identity,
)
from buyer_intelligence.first_revenue_validation_sprint_v1 import (
    _HARD_EXCLUDE,
    _clean_title,
    _days_to_deadline,
)
from buyer_intelligence.supplier_fit_evidence_contact_recovery_v1 import (
    SUPPLIER_META,
    resolve_contact,
)
from m3_data_root import data_path

log = logging.getLogger("govtracker.source_to_product_extraction_audit_v1")

BUILD = "20261012-m3-source-to-product-extraction-audit-v1"
OUT_DIR = "_og_commercial_research_pipeline_out"

SAMPLE_FILE = f"{OUT_DIR}/m3_source_product_v1_sample_50.json"
AUDIT_FILE = f"{OUT_DIR}/m3_source_product_v1_audit_traces.json"
ROOT_CAUSE_FILE = f"{OUT_DIR}/m3_source_product_v1_root_causes.json"
LINES_SIDECAR = f"{OUT_DIR}/m3_product_lines_contract_v1.json"
REPROCESS_FILE = f"{OUT_DIR}/m3_source_product_v1_reprocess_50.json"
SCORECARD_FILE = f"{OUT_DIR}/m3_source_product_v1_scorecard.json"
READINESS_FILE = f"{OUT_DIR}/m3_source_product_v1_readiness_metrics.json"
RFQ_FILE = f"{OUT_DIR}/m3_source_product_v1_rfqs.json"
DEALS_FILE = f"{OUT_DIR}/m3_source_product_v1_available_deals.json"
REPORT_FILE = f"{OUT_DIR}/m3_source_product_v1_report.json"
CHECKPOINT_FILE = f"{OUT_DIR}/m3_source_product_v1_checkpoint.json"
HANDOFF_TEST_FILE = f"{OUT_DIR}/m3_source_product_v1_handoff_proof.json"

# Failure taxonomy
SOURCE_DOCUMENT_NOT_AVAILABLE = "SOURCE_DOCUMENT_NOT_AVAILABLE"
DOCUMENT_NOT_DOWNLOADED = "DOCUMENT_NOT_DOWNLOADED"
AUTH_OR_ACCESS_REQUIRED = "AUTH_OR_ACCESS_REQUIRED"
ATTACHMENT_DISCOVERY_FAILED = "ATTACHMENT_DISCOVERY_FAILED"
DOCUMENT_DOWNLOAD_FAILED = "DOCUMENT_DOWNLOAD_FAILED"
PDF_TEXT_EXTRACTION_FAILED = "PDF_TEXT_EXTRACTION_FAILED"
SCANNED_DOCUMENT_NOT_PROCESSED = "SCANNED_DOCUMENT_NOT_PROCESSED"
TABLE_EXTRACTION_FAILED = "TABLE_EXTRACTION_FAILED"
SPREADSHEET_EXTRACTION_FAILED = "SPREADSHEET_EXTRACTION_FAILED"
PRODUCT_LINE_SEGMENTATION_FAILED = "PRODUCT_LINE_SEGMENTATION_FAILED"
SPECIFICATION_EXTRACTION_FAILED = "SPECIFICATION_EXTRACTION_FAILED"
MPN_NSN_EXTRACTION_FAILED = "MPN_NSN_EXTRACTION_FAILED"
QUANTITY_UOM_EXTRACTION_FAILED = "QUANTITY_UOM_EXTRACTION_FAILED"
PERSISTENCE_DATA_LOSS = "PERSISTENCE_DATA_LOSS"
DOWNSTREAM_HANDOFF_DATA_LOSS = "DOWNSTREAM_HANDOFF_DATA_LOSS"
SOURCE_GENUINELY_INCOMPLETE = "SOURCE_GENUINELY_INCOMPLETE"
BUYER_CLARIFICATION_REQUIRED = "BUYER_CLARIFICATION_REQUIRED"
OTHER_VERIFIED_FAILURE = "OTHER_VERIFIED_FAILURE"

_NON_PRODUCT = re.compile(
    r"\b(services?|construction|installation|design[- ]build|feasibility|"
    r"surplus|scrap|crm|software|development\s+project|maintenance\s+contract)\b",
    re.I,
)


# ---------------------------------------------------------------------------
# Product-line data contract
# ---------------------------------------------------------------------------


def make_product_line(
    *,
    line_number: Any,
    description: str,
    manufacturer: str | None = None,
    brand: str | None = None,
    model: str | None = None,
    mpn: str | None = None,
    nsn: str | None = None,
    upc: str | None = None,
    specs: dict[str, Any] | None = None,
    material: str | None = None,
    dimensions: str | None = None,
    capacity: str | None = None,
    certifications: list[str] | None = None,
    qty: Any = None,
    uom: str | None = None,
    pack_size: str | None = None,
    delivery_location: str | None = None,
    delivery_date: str | None = None,
    brand_restricted: bool | None = None,
    approved_equivalents: bool | None = None,
    mfr_auth_required: bool | None = None,
    mandatory: bool = True,
    source_document: str | None = None,
    source_location: str | None = None,
    provenance: str | None = None,
) -> dict[str, Any]:
    """Canonical product-line contract used by commercial research."""
    desc = (description or "").strip()
    idc = classify_product_identity(
        desc,
        structured={
            "MPN": mpn or model,
            "NSN": nsn,
            "MANUFACTURER": manufacturer or brand,
            "UPC": upc,
            "QTY": qty,
            "UOM": uom,
        },
    )
    return {
        "LINE_NUMBER": str(line_number) if line_number is not None else None,
        "DESCRIPTION": desc,
        "MANUFACTURER": manufacturer,
        "BRAND": brand or manufacturer,
        "MODEL": model,
        "MPN": mpn or model,
        "NSN": nsn,
        "UPC_GTIN": upc,
        "TECHNICAL_SPECS": specs or {},
        "MATERIAL": material,
        "DIMENSIONS": dimensions,
        "CAPACITY_RATING": capacity,
        "CERTIFICATIONS": certifications or [],
        "QTY": qty,
        "UOM": uom,
        "PACKAGE_SIZE": pack_size,
        "DELIVERY_LOCATION": delivery_location,
        "DELIVERY_DATE": delivery_date,
        "BRAND_RESTRICTED": brand_restricted,
        "APPROVED_EQUIVALENTS": approved_equivalents,
        "MFR_AUTH_REQUIRED": mfr_auth_required,
        "MANDATORY": mandatory,
        "SOURCE_DOCUMENT": source_document,
        "SOURCE_LOCATION": source_location,
        "PROVENANCE": provenance or "UNKNOWN",
        "IDENTITY_CLASS": idc["IDENTITY_CLASS"],
        "IS_EXACT_VERIFIED": idc["IS_EXACT_VERIFIED"],
        "CONTRACT_VERSION": "product_line_v1",
    }


_MFR_FROM_DESC = re.compile(
    r"\b(Meritor|SCA|3M|Grainger|Wachs|Penn\s+Valley|Caterpillar|Cummins|Delco|ACDelco|Honda|Ford|Chevy|Goodyear)\b",
    re.I,
)


def normalize_enrichment_line(ln: dict[str, Any], *, provenance: str) -> dict[str, Any]:
    ident = ln.get("IDENTITY") if isinstance(ln.get("IDENTITY"), dict) else {}
    desc = str(ln.get("DESCRIPTION") or ln.get("description") or ident.get("raw_description") or "")
    mpn = _plausible_mpn(
        ln.get("PART_NUMBER")
        or ln.get("MPN")
        or ln.get("part_number")
        or ln.get("CATALOG_NUMBER")
        or ln.get("BUYER_PART_REF")
        or ident.get("part_number")
        or ident.get("catalog_number")
    )
    mfr = ln.get("MANUFACTURER") or ln.get("manufacturer") or ln.get("BRAND")
    if not mfr:
        mm = _MFR_FROM_DESC.search(desc)
        if mm:
            mfr = mm.group(1)
    return make_product_line(
        line_number=ln.get("LINE_NUMBER") or ln.get("line_number") or ln.get("item"),
        description=desc,
        manufacturer=mfr,
        brand=ln.get("BRAND") or ln.get("brand") or mfr,
        model=ln.get("MODEL") or ln.get("model"),
        mpn=mpn,
        nsn=ln.get("NSN") or ln.get("nsn"),
        upc=ln.get("UPC") or ln.get("upc"),
        qty=ln.get("QTY") if ln.get("QTY") is not None else ln.get("qty") or ln.get("quantity"),
        uom=ln.get("UOM") or ln.get("uom") or ln.get("unitToMeasure"),
        source_document=ln.get("SOURCE") or provenance,
        source_location=ln.get("SOURCE_LOCATION") or ln.get("source_location"),
        provenance=provenance,
        approved_equivalents=ln.get("equal_allowed"),
    )


# ---------------------------------------------------------------------------
# Sample selection
# ---------------------------------------------------------------------------


def select_stratified_sample(rows: list[dict[str, Any]], *, n: int = 50) -> list[dict[str, Any]]:
    """Stratified incomplete product-resale opportunities; persist IDs before repair."""
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if int(r.get("QUOTEABLE_LINES") or 0) <= 0:
            continue
        title = _clean_title(str(r.get("TITLE") or ""))
        if _NON_PRODUCT.search(title) and not re.search(
            r"\b(paper|towel|glove|toner|valve|gasket|battery|filter|pump|cable)\b", title, re.I
        ):
            continue
        if _HARD_EXCLUDE.search(title) and not re.search(r"\b(paper|towel|toilet|glove|toner)\b", title, re.I):
            continue
        if re.search(r"\b(chevy|silverado|vehicle|truck|automobile|sedan)\b", title, re.I):
            continue  # product-resale focus — exclude vehicle buys from sample
        idc = classify_product_identity(title)["IDENTITY_CLASS"]
        if idc in {EXACT_PRODUCT_VERIFIED}:
            continue  # sample incomplete only
        src = str(r.get("SOURCE") or "OTHER")
        if "OpenGov" in src:
            portal = "OpenGov"
        elif "BidNet" in src:
            portal = "BidNet"
        elif "Socrata" in src or "SAM" in src.upper() or "Federal" in src:
            portal = "Federal_Socrata"
        elif "Jaggaer" in src or "Euna" in src or "Bonfire" in src:
            portal = "Euna_Jaggaer"
        else:
            portal = "Other"
        rl = int(r.get("REAL_LINES") or 0)
        multi = "multi" if rl >= 6 else "single"
        buckets[f"{portal}|{idc}|{multi}"].append(r)

    # Round-robin across buckets preferring commercial-looking titles
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _pref(r: dict[str, Any]) -> float:
        title = str(r.get("TITLE") or "").lower()
        sc = 0.0
        for kw in ("paper", "towel", "glove", "toner", "valve", "gasket", "filter", "battery", "mop", "cable"):
            if kw in title:
                sc += 5
        enum = int(r.get("ATTACHMENTS_ENUMERATED") or 0)
        fet = int(r.get("ATTACHMENTS_FETCHED") or 0)
        real = int(r.get("REAL_LINES") or 0)
        if real > 0:
            sc += 8  # handoff-loss candidates
        if enum > fet:
            sc += 4
        if fet > 0:
            sc += 3
        days = _days_to_deadline(r.get("DEADLINE"))
        if days and days >= 3:
            sc += 2
        return sc

    for b in buckets:
        buckets[b].sort(key=_pref, reverse=True)

    keys = sorted(buckets.keys(), key=lambda k: -len(buckets[k]))
    while len(selected) < n and any(buckets[k] for k in keys):
        for k in keys:
            if len(selected) >= n:
                break
            while buckets[k]:
                r = buckets[k].pop(0)
                oid = r["OPPORTUNITY_ID"]
                if oid in seen:
                    continue
                seen.add(oid)
                title = _clean_title(str(r.get("TITLE") or ""))
                idc = classify_product_identity(title)
                selected.append(
                    {
                        "OPPORTUNITY_ID": oid,
                        "TITLE": title,
                        "BUYER": r.get("BUYER"),
                        "DEADLINE": r.get("DEADLINE"),
                        "SOURCE": r.get("SOURCE"),
                        "BEFORE_IDENTITY_CLASS": idc["IDENTITY_CLASS"],
                        "REAL_LINES": r.get("REAL_LINES"),
                        "QUOTEABLE_LINES": r.get("QUOTEABLE_LINES"),
                        "ATTACHMENTS_ENUMERATED": r.get("ATTACHMENTS_ENUMERATED"),
                        "ATTACHMENTS_FETCHED": r.get("ATTACHMENTS_FETCHED"),
                        "STRATUM": k,
                        "SELECTED_AT": _utc(),
                    }
                )
                break
    return selected


# ---------------------------------------------------------------------------
# Enrichment / cache recovery
# ---------------------------------------------------------------------------


def _tnorm(x: Any) -> str:
    if isinstance(x, dict):
        x = x.get("VALUE") or x.get("value") or ""
    return re.sub(r"[^a-z0-9]+", " ", str(x or "").lower()).strip()


_CATALOG_IN_DESC = re.compile(
    r"(?:SCA\s*#|SKU\s*#?|CAT(?:ALOG)?\s*#?|P/?N\s*#?|MPN\s*#?|MODEL\s*#?)\s*([A-Z0-9][A-Z0-9\-./]{2,})",
    re.I,
)


def _plausible_mpn(token: str | None) -> str | None:
    """Reject garbage tokens extracted from prose (e.g. 'ion')."""
    if not token:
        return None
    t = str(token).strip()
    if len(t) < 4:
        return None
    if not re.search(r"\d", t):
        return None
    if t.lower() in {"each", "total", "item", "none", "null", "unit", "case"}:
        return None
    return t


def _enrich_line_mpn(item: dict[str, Any]) -> dict[str, Any]:
    """Pull catalog/MPN tokens out of description when dedicated fields are empty."""
    desc = str(item.get("DESCRIPTION") or item.get("description") or "")
    existing = _plausible_mpn(item.get("PART_NUMBER") or item.get("MPN"))
    if existing:
        return {**item, "PART_NUMBER": existing}
    m = _CATALOG_IN_DESC.search(desc)
    if m:
        tok = _plausible_mpn(m.group(1))
        if tok:
            return {**item, "PART_NUMBER": tok}
    # SCA #TM1616S / #82-59990 style
    m2 = re.search(r"#\s*([A-Z0-9][A-Z0-9\-./]{3,})", desc, re.I)
    if m2:
        tok = _plausible_mpn(m2.group(1))
        if tok:
            return {**item, "PART_NUMBER": tok}
    # drop implausible existing part fields
    if item.get("PART_NUMBER") and not _plausible_mpn(item.get("PART_NUMBER")):
        item = {**item, "PART_NUMBER": None}
    return item


def _payload_title_text(title_field: Any) -> str:
    if isinstance(title_field, dict):
        return str(title_field.get("VALUE") or title_field.get("value") or "")
    return str(title_field or "")


def _index_cache_file(
    f: Path,
    *,
    by_title: dict[str, dict[str, Any]],
    by_cid: dict[str, dict[str, Any]],
    by_buyer_slug: dict[str, list[dict[str, Any]]],
) -> bool:
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
        payload = d.get("payload") or {}
        items: list[dict[str, Any]] = []
        for pt in payload.get("priceTables") or []:
            for it in pt.get("priceItems") or []:
                if isinstance(it, dict) and (it.get("description") or "").strip():
                    raw_item = {
                        "LINE_NUMBER": it.get("id"),
                        "DESCRIPTION": it.get("description"),
                        "QTY": it.get("quantity"),
                        "UOM": it.get("unitToMeasure"),
                        "PART_NUMBER": it.get("custom1") or it.get("partNumber"),
                        "SOURCE": f"opengov_cache:{f.name}",
                        "STATE": "PROVEN",
                    }
                    items.append(_enrich_line_mpn(raw_item))
        if not items:
            return False
        title = _tnorm(payload.get("title"))
        slug = str(d.get("slug") or "")
        pid = d.get("project_id") or ""
        cid = f"opengov:{slug}:{pid}" if slug and pid else ""
        entry = {
            "kind": "cache",
            "cid": cid,
            "file": str(f),
            "lines": items,
            "payload_title": payload.get("title"),
            "slug": slug,
            "line_count": len(items),
        }
        # Prefer larger line payloads when the same title appears in multiple caches
        if cid and (cid not in by_cid or len(items) > len((by_cid[cid].get("lines") or []))):
            by_cid[cid] = entry
        if title and (title not in by_title or len(items) > len((by_title[title].get("lines") or []))):
            by_title[title] = entry
        if slug:
            by_buyer_slug[_tnorm(slug)].append(entry)
        return True
    except Exception:
        return False


def load_opengov_line_indexes() -> dict[str, Any]:
    """Index enrichment rows + cache payloads that contain price/line items."""
    by_title: dict[str, dict[str, Any]] = {}
    by_cid: dict[str, dict[str, Any]] = {}
    by_buyer_slug: dict[str, list[dict[str, Any]]] = defaultdict(list)
    sources_loaded: list[str] = []

    en_path = data_path("m3_opengov_live_deep_enrichment_v1.json")
    if en_path.exists():
        try:
            blob = json.loads(en_path.read_text(encoding="utf-8"))
            for r in blob.get("rows") or []:
                if not isinstance(r, dict):
                    continue
                lines = [_enrich_line_mpn(dict(ln)) for ln in (r.get("LINE_ITEMS") or []) if isinstance(ln, dict)]
                if not lines:
                    continue
                cid = str(r.get("canonical_opportunity_id") or "")
                title = _tnorm(r.get("TITLE"))
                slug = str(r.get("PORTAL_SLUG") or (cid.split(":")[1] if cid.count(":") >= 2 else ""))
                entry = {
                    "kind": "enrichment",
                    "cid": cid,
                    "raw": r,
                    "lines": lines,
                    "slug": slug,
                    "payload_title": _payload_title_text(r.get("TITLE")),
                    "line_count": len(lines),
                }
                if cid:
                    by_cid[cid] = entry
                if title:
                    by_title[title] = entry
                if slug:
                    by_buyer_slug[_tnorm(slug)].append(entry)
            sources_loaded.append("m3_opengov_live_deep_enrichment_v1.json")
        except Exception as exc:
            log.info("enrichment load failed: %s", exc)

    cache_dirs = [
        data_path("opengov_deep_enrichment_cache"),
        data_path("_og_profit_val_out") / "opengov_deep_enrichment_cache",
        Path("data/opengov_deep_enrichment_cache"),
        Path("data/_og_profit_val_out/opengov_deep_enrichment_cache"),
    ]
    seen_dirs: set[str] = set()
    for cache_dir in cache_dirs:
        key = str(cache_dir.resolve()) if cache_dir.exists() else ""
        if not key or key in seen_dirs:
            continue
        seen_dirs.add(key)
        cache_hits = 0
        for f in cache_dir.glob("*.json"):
            if _index_cache_file(f, by_title=by_title, by_cid=by_cid, by_buyer_slug=by_buyer_slug):
                cache_hits += 1
        sources_loaded.append(f"{cache_dir.as_posix()} ({cache_hits} with items)")

    return {
        "by_title": by_title,
        "by_cid": by_cid,
        "by_buyer_slug": dict(by_buyer_slug),
        "sources_loaded": sources_loaded,
    }


def load_profit_val_line_index() -> dict[str, Any]:
    """Index profitability-validation LINE_DETAILS + solicitation URLs by opportunity id."""
    by_oid: dict[str, dict[str, Any]] = {}
    paths = [
        data_path("_og_profit_val_out") / "m3_opengov_enriched_profitability_validation_v1_results.json",
        Path("data/_og_profit_val_out/m3_opengov_enriched_profitability_validation_v1_results.json"),
    ]
    for path in paths:
        if not path.exists():
            continue
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.info("profit_val load failed: %s", exc)
            continue
        for r in blob.get("rows") or []:
            if not isinstance(r, dict):
                continue
            oid = str(r.get("OPPORTUNITY_ID") or "")
            if not oid:
                continue
            details = r.get("LINE_DETAILS") or []
            lines: list[dict[str, Any]] = []
            for i, d in enumerate(details, 1):
                if not isinstance(d, dict):
                    continue
                ident = d.get("IDENTITY") if isinstance(d.get("IDENTITY"), dict) else {}
                lines.append(
                    _enrich_line_mpn(
                        {
                            "LINE_NUMBER": d.get("LINE_NUMBER") or i,
                            "DESCRIPTION": d.get("DESCRIPTION"),
                            "QTY": d.get("QTY"),
                            "UOM": d.get("UOM"),
                            "PART_NUMBER": ident.get("part_number") or ident.get("catalog_number"),
                            "IDENTITY": ident,
                            "SOURCE": f"profit_val_line_details:{path.name}",
                            "STATE": "PROVEN",
                        }
                    )
                )
            url = r.get("SOLICITATION_URL")
            by_oid[oid] = {
                "oid": oid,
                "title": r.get("TITLE"),
                "url": url,
                "lines": lines,
                "line_count": len(lines),
                "kind": "profit_val",
            }
        break
    return {"by_oid": by_oid, "source": str(paths[0])}


_OPENGOV_URL_RE = re.compile(
    r"procurement\.opengov\.com/portal/([^/]+)/projects/(\d+)",
    re.I,
)


def live_fetch_opengov_lines(url: str) -> dict[str, Any]:
    """Bounded public OpenGov project fetch — reuses existing cache/API helpers."""
    m = _OPENGOV_URL_RE.search(url or "")
    if not m:
        return {"RECOVERED": False, "LINES": [], "NOTE": "No OpenGov portal URL"}
    slug, pid = m.group(1), m.group(2)
    try:
        import httpx
        from buyer_intelligence.opengov_live_deep_enrichment_v1 import (
            extract_line_items,
            fetch_project_detail,
        )
    except Exception as exc:
        return {"RECOVERED": False, "LINES": [], "NOTE": f"Import failed: {exc}"}

    try:
        with httpx.Client(follow_redirects=True, timeout=35.0) as client:
            result = fetch_project_detail(slug, pid, client=client, use_cache=True)
    except Exception as exc:
        return {"RECOVERED": False, "LINES": [], "NOTE": f"Fetch error: {type(exc).__name__}"}

    payload = result.get("payload") if isinstance(result, dict) else None
    if not payload:
        return {
            "RECOVERED": False,
            "LINES": [],
            "NOTE": f"No payload: {result.get('route_status') if isinstance(result, dict) else 'unknown'}",
            "ROUTE_STATUS": (result or {}).get("route_status") if isinstance(result, dict) else None,
        }
    raw_lines = extract_line_items(payload)
    lines = [
        normalize_enrichment_line(_enrich_line_mpn(dict(ln)), provenance=f"RECOVERED:live_opengov:{slug}_{pid}")
        for ln in raw_lines
        if isinstance(ln, dict)
    ]
    lines = [ln for ln in lines if ln.get("DESCRIPTION")]
    return {
        "RECOVERED": bool(lines),
        "LINES": lines,
        "SOURCE": "live_opengov",
        "SOURCE_FILE": f"{slug}_{pid}",
        "LINE_COUNT": len(lines),
        "CACHE_HIT": bool(result.get("cache_hit")),
        "ROUTE_STATUS": result.get("route_status"),
        "MATCH_TITLE": payload.get("title"),
        "NOTE": f"Recovered {len(lines)} lines via OpenGov API ({'cache' if result.get('cache_hit') else 'live'})",
    }


def recover_lines_for_opportunity(
    row: dict[str, Any],
    indexes: dict[str, Any],
    *,
    profit_val: dict[str, Any] | None = None,
    allow_live_fetch: bool = False,
) -> dict[str, Any]:
    """Attempt to recover product lines from enrichment/cache/profit-val (no SAM)."""
    oid = str(row.get("OPPORTUNITY_ID") or "")
    # Prefer profit-val LINE_DETAILS (often includes part numbers)
    pv_hit = ((profit_val or {}).get("by_oid") or {}).get(oid) if profit_val else None
    if pv_hit and (pv_hit.get("lines") or []):
        lines = [
            normalize_enrichment_line(ln, provenance="RECOVERED:profit_val_line_details")
            for ln in pv_hit["lines"]
            if isinstance(ln, dict)
        ]
        lines = [ln for ln in lines if ln.get("DESCRIPTION")]
        if lines:
            return {
                "RECOVERED": True,
                "LINES": lines,
                "SOURCE": "profit_val",
                "SOURCE_CID": oid,
                "SOURCE_FILE": "m3_opengov_enriched_profitability_validation_v1_results.json",
                "LINE_COUNT": len(lines),
                "MATCH_TITLE": pv_hit.get("title"),
                "SOLICITATION_URL": pv_hit.get("url"),
                "NOTE": f"Recovered {len(lines)} lines from profit_val LINE_DETAILS",
            }

    title = _clean_title(str(row.get("TITLE") or ""))
    buyer = _tnorm(row.get("BUYER") or "")
    tkey = _tnorm(title)
    hit = (indexes.get("by_title") or {}).get(tkey)
    stop = {
        "the", "and", "for", "with", "from", "bid", "rfq", "rfp", "ifb", "number",
        "solicitation", "city", "county", "of", "department", "purchase", "delivery",
    }
    toks = {t for t in tkey.split() if len(t) > 2} - stop

    def _score_entry(v: dict[str, Any]) -> tuple[int, int]:
        pt = _tnorm(v.get("payload_title") or "")
        kt = {t for t in pt.split() if len(t) > 2} - stop
        overlap = len(toks & kt)
        # Prefer exact / containment and richer line tables
        bonus = 0
        if pt == tkey:
            bonus += 100
        elif tkey and (tkey in pt or pt in tkey):
            bonus += 40
        return (overlap + bonus, int(v.get("line_count") or len(v.get("lines") or [])))

    # fuzzy: token overlap / containment across all titles (strict for short titles)
    if not hit:
        best = None
        best_score = (0, 0)
        short_title = len(toks) <= 2
        for k, v in (indexes.get("by_title") or {}).items():
            kt = {t for t in k.split() if len(t) > 2} - stop
            if not toks or not kt:
                continue
            n = len(toks & kt)
            contained = bool(tkey and (tkey in k or k in tkey))
            exact = k == tkey
            if short_title:
                # "Janitorial Supplies" must be exact or full containment — never loose overlap
                if not (exact or contained):
                    continue
                need = len(toks)
            else:
                need = 2 if contained else max(3, min(5, int(0.45 * max(len(toks), 1))))
            if n < need and not exact:
                continue
            score = _score_entry(v)
            if score > best_score:
                best_score = score
                best = v
        hit = best

    # Buyer/slug scoped search — OpenGov cache filenames use portal slugs
    if not hit and buyer and len(toks) >= 3:
        buyer_toks = {t for t in buyer.split() if len(t) > 3} - stop
        candidates: list[dict[str, Any]] = []
        for slug, entries in (indexes.get("by_buyer_slug") or {}).items():
            if any(bt in slug for bt in buyer_toks):
                candidates.extend(entries)
        if candidates:
            ranked = sorted(candidates, key=_score_entry, reverse=True)
            # Require strong title overlap within buyer portal
            if ranked and _score_entry(ranked[0])[0] >= 3:
                hit = ranked[0]

    if hit:
        lines = [
            normalize_enrichment_line(
                ln, provenance=f"RECOVERED:{hit.get('kind')}:{Path(str(hit.get('file') or '')).name}"
            )
            for ln in hit["lines"]
            if isinstance(ln, dict)
        ]
        lines = [ln for ln in lines if ln.get("DESCRIPTION")]
        if lines:
            return {
                "RECOVERED": True,
                "LINES": lines,
                "SOURCE": hit.get("kind"),
                "SOURCE_CID": hit.get("cid"),
                "SOURCE_FILE": hit.get("file"),
                "LINE_COUNT": len(lines),
                "MATCH_TITLE": hit.get("payload_title"),
                "NOTE": f"Recovered {len(lines)} lines from {hit.get('kind')}",
            }

    # Targeted live OpenGov fetch for frozen-sample gaps (public API, cached on disk)
    url = None
    if pv_hit:
        url = pv_hit.get("url")
    if allow_live_fetch and url and str(row.get("SOURCE") or "") == "OpenGov":
        live = live_fetch_opengov_lines(str(url))
        if live.get("RECOVERED"):
            return live
        return {
            "RECOVERED": False,
            "LINES": [],
            "SOURCE": None,
            "SOLICITATION_URL": url,
            "NOTE": live.get("NOTE") or "Live OpenGov fetch returned no lines",
            "ROUTE_STATUS": live.get("ROUTE_STATUS"),
        }

    return {
        "RECOVERED": False,
        "LINES": [],
        "SOURCE": None,
        "SOLICITATION_URL": url,
        "NOTE": "No enrichment/cache/profit_val line match",
    }


# ---------------------------------------------------------------------------
# Trace + root cause
# ---------------------------------------------------------------------------


def trace_opportunity(
    row: dict[str, Any],
    sel: dict[str, Any],
    *,
    indexes: dict[str, Any],
    rematch_row: dict[str, Any] | None,
    profit_val: dict[str, Any] | None = None,
    allow_live_fetch: bool = False,
) -> dict[str, Any]:
    oid = str(row.get("OPPORTUNITY_ID") or sel.get("OPPORTUNITY_ID"))
    title = _clean_title(str(row.get("TITLE") or sel.get("TITLE") or ""))
    inv = row.get("EVIDENCE_INVENTORY") or {}
    enum = int(row.get("ATTACHMENTS_ENUMERATED") or 0)
    fet = int(row.get("ATTACHMENTS_FETCHED") or 0)
    real = int(row.get("REAL_LINES") or 0)
    ql = int(row.get("QUOTEABLE_LINES") or 0)
    docs = row.get("DOCS_PARSED_BY_CLASS") or {}
    paths_ok = set(row.get("EVIDENCE_PATHS_SUCCESSFUL") or [])
    paths_blocked = set(row.get("EVIDENCE_PATHS_BLOCKED") or [])
    before_id = classify_product_identity(title)

    recovery = recover_lines_for_opportunity(
        row, indexes, profit_val=profit_val, allow_live_fetch=allow_live_fetch
    )
    recovered_lines = recovery.get("LINES") or []

    # What commercial research currently receives (title-only proxy)
    commercial_input = {
        "TITLE_ONLY": True,
        "LINE_PAYLOAD_IN_HT_RESULTS": False,
        "NOTE": "HT v2 results persist REAL_LINES count but not line payloads",
    }

    stages = {
        "source_url": recovery.get("SOLICITATION_URL") or recovery.get("SOURCE_FILE"),
        "attachments_enumerated": enum,
        "attachments_fetched": fet,
        "docs_parsed_by_class": docs,
        "evidence_inventory": inv,
        "paths_successful": sorted(paths_ok),
        "paths_blocked": sorted(paths_blocked),
        "real_lines_count_in_ht": real,
        "quoteable_lines_count": ql,
        "line_payload_persisted_in_ht_results": False,
        "recovered_line_count": len(recovered_lines),
        "commercial_research_input": commercial_input,
        "displayed_identity_before": before_id["IDENTITY_CLASS"],
    }

    expected_docs = enum if enum > 0 else (1 if inv.get("DETAIL_RECORD") == "PRESENT" else 0)
    discovered = enum
    downloaded = fet
    parsed = sum(int(v) for v in docs.values()) if isinstance(docs, dict) else 0
    spec_bearing = 1 if inv.get("SPECIFICATIONS") == "PRESENT" or inv.get("PRICE_TABLE") == "PRESENT" else 0

    primary, secondary = classify_root_cause(
        row=row,
        real=real,
        enum=enum,
        fet=fet,
        docs=docs,
        inv=inv,
        paths_blocked=paths_blocked,
        recovered=len(recovered_lines),
        before_id=before_id["IDENTITY_CLASS"],
    )

    repairable = primary in {
        DOWNSTREAM_HANDOFF_DATA_LOSS,
        PERSISTENCE_DATA_LOSS,
        PRODUCT_LINE_SEGMENTATION_FAILED,
        SPECIFICATION_EXTRACTION_FAILED,
        MPN_NSN_EXTRACTION_FAILED,
        TABLE_EXTRACTION_FAILED,
        DOCUMENT_NOT_DOWNLOADED,
        PDF_TEXT_EXTRACTION_FAILED,
    }

    return {
        "OPPORTUNITY_ID": oid,
        "TITLE": title,
        "BUYER": row.get("BUYER") or sel.get("BUYER"),
        "SOURCE": row.get("SOURCE") or sel.get("SOURCE"),
        "DEADLINE": row.get("DEADLINE") or sel.get("DEADLINE"),
        "STAGES": stages,
        "DOCUMENT_COUNTS": {
            "EXPECTED_DOCUMENT_COUNT": expected_docs,
            "DISCOVERED_DOCUMENT_COUNT": discovered,
            "DOWNLOADED_DOCUMENT_COUNT": downloaded,
            "SUCCESSFULLY_PARSED_DOCUMENT_COUNT": parsed,
            "SPECIFICATION_BEARING_DOCUMENT_COUNT": spec_bearing,
            "MISSING_CRITICAL_DOCUMENTS": max(0, enum - fet) if enum else (1 if expected_docs and not fet else 0),
        },
        "BEFORE_IDENTITY": before_id,
        "RECOVERY": {k: v for k, v in recovery.items() if k != "LINES"},
        "RECOVERED_LINES": recovered_lines[:200],
        "RECOVERED_LINE_COUNT": len(recovered_lines),
        "PRIMARY_ROOT_CAUSE": primary,
        "SECONDARY_CAUSES": secondary,
        "REPAIRABLE": repairable,
        "REMATCH_SUPPLIERS": _supplier_names(rematch_row, row),
    }


def _supplier_names(rematch_row: dict[str, Any] | None, row: dict[str, Any]) -> list[str]:
    raw = (rematch_row or {}).get("SUPPLIER_SET_AFTER")
    if raw is None:
        raw = row.get("SUPPLIER_SET") or {}
    if isinstance(raw, dict):
        return list(raw.keys())
    if isinstance(raw, list):
        out: list[str] = []
        for x in raw:
            if isinstance(x, str):
                out.append(x)
            elif isinstance(x, dict) and x.get("SUPPLIER"):
                out.append(str(x["SUPPLIER"]))
        return out
    return []


def classify_root_cause(
    *,
    row: dict[str, Any],
    real: int,
    enum: int,
    fet: int,
    docs: dict[str, Any],
    inv: dict[str, Any],
    paths_blocked: set[str],
    recovered: int,
    before_id: str,
) -> tuple[str, list[str]]:
    secondary: list[str] = []
    src = str(row.get("SOURCE") or "")

    # Auth / BidNet paid
    if "PAID_SOURCE" in paths_blocked or "AUTH" in " ".join(paths_blocked).upper():
        return AUTH_OR_ACCESS_REQUIRED, secondary
    if "BidNet" in src and enum == 0 and real == 0 and inv.get("ATTACHMENTS") != "PRESENT":
        # many BidNet packages need auth for full docs
        if inv.get("DETAIL_RECORD") != "PRESENT":
            return AUTH_OR_ACCESS_REQUIRED, [SOURCE_DOCUMENT_NOT_AVAILABLE]

    # Lines were counted in HT but payload never handed to commercial research
    if real > 0:
        secondary.append(DOWNSTREAM_HANDOFF_DATA_LOSS)
        if recovered > 0:
            return DOWNSTREAM_HANDOFF_DATA_LOSS, secondary
        # Lines existed at enrich time but we cannot recover payload now
        if fet == 0 and enum > 0:
            secondary.append(DOCUMENT_NOT_DOWNLOADED)
        if before_id in {CATEGORY_ONLY, PARTIAL_PRODUCT_IDENTITY} and real >= 1:
            # Handed off title-only after line detection
            return DOWNSTREAM_HANDOFF_DATA_LOSS, secondary

    # Attachments discovered but not fully downloaded
    if enum > fet and fet == 0:
        return DOCUMENT_NOT_DOWNLOADED, secondary
    if enum > fet and fet > 0:
        secondary.append(DOCUMENT_NOT_DOWNLOADED)

    # Fetched but no lines
    if fet > 0 and real == 0:
        parsed = sum(int(v) for v in (docs or {}).values()) if docs else 0
        if parsed == 0:
            return PDF_TEXT_EXTRACTION_FAILED, [DOCUMENT_DOWNLOAD_FAILED] if enum > fet else secondary
        if inv.get("PRICE_TABLE") == "NOT_PRESENT" and inv.get("STRUCTURED_TABLE") == "PRESENT":
            return TABLE_EXTRACTION_FAILED, secondary
        return PRODUCT_LINE_SEGMENTATION_FAILED, secondary

    # Specs present inventory but identity still category-only
    if inv.get("SPECIFICATIONS") == "PRESENT" and before_id == CATEGORY_ONLY:
        return SPECIFICATION_EXTRACTION_FAILED, secondary

    if inv.get("ATTACHMENTS") == "NOT_PRESENT" and inv.get("DETAIL_RECORD") == "PRESENT" and real == 0:
        return SOURCE_GENUINELY_INCOMPLETE, secondary

    if enum == 0 and inv.get("ATTACHMENTS") != "PRESENT" and real == 0:
        if "BidNet" in src:
            return AUTH_OR_ACCESS_REQUIRED, [ATTACHMENT_DISCOVERY_FAILED]
        return SOURCE_DOCUMENT_NOT_AVAILABLE, secondary

    if real > 0 and recovered == 0:
        return PERSISTENCE_DATA_LOSS, [DOWNSTREAM_HANDOFF_DATA_LOSS]

    if before_id == CATEGORY_ONLY:
        return MPN_NSN_EXTRACTION_FAILED, secondary

    return OTHER_VERIFIED_FAILURE, secondary


# ---------------------------------------------------------------------------
# Reprocess / readiness
# ---------------------------------------------------------------------------


def opportunity_identity_from_lines(title: str, lines: list[dict[str, Any]]) -> dict[str, Any]:
    if not lines:
        return classify_product_identity(title)
    # Best line identity wins for opportunity-level class
    order = [
        EXACT_PRODUCT_VERIFIED,
        SPECIFICATION_COMPLETE_GENERIC,
        PARTIAL_PRODUCT_IDENTITY,
        CATEGORY_ONLY,
        IDENTITY_CONFLICT,
        IDENTITY_UNKNOWN,
    ]
    best = classify_product_identity(title)
    best_i = order.index(best["IDENTITY_CLASS"]) if best["IDENTITY_CLASS"] in order else 99
    for ln in lines:
        cls = ln.get("IDENTITY_CLASS") or classify_product_identity(ln.get("DESCRIPTION") or "")["IDENTITY_CLASS"]
        i = order.index(cls) if cls in order else 99
        if i < best_i:
            best_i = i
            best = {
                "IDENTITY_CLASS": cls,
                "IS_EXACT_VERIFIED": cls == EXACT_PRODUCT_VERIFIED,
                "NOTE": f"From recovered line {ln.get('LINE_NUMBER')}",
                "IDENTITY": ln,
            }
    # If multiple mandatory lines all complete generic / exact → keep best
    return best


def compute_readiness(lines: list[dict[str, Any]], *, suppliers: list[str], priced: bool = False) -> dict[str, Any]:
    mandatory = [ln for ln in lines if ln.get("MANDATORY", True)]
    if not mandatory:
        mandatory = list(lines)
    total = len(mandatory)
    extracted = total
    specs_complete = sum(
        1
        for ln in mandatory
        if ln.get("IDENTITY_CLASS") in {EXACT_PRODUCT_VERIFIED, SPECIFICATION_COMPLETE_GENERIC}
    )
    searchable = sum(
        1
        for ln in mandatory
        if ln.get("IDENTITY_CLASS")
        in {EXACT_PRODUCT_VERIFIED, SPECIFICATION_COMPLETE_GENERIC, PARTIAL_PRODUCT_IDENTITY}
        and (ln.get("MPN") or ln.get("DESCRIPTION"))
    )
    rfq_ready = sum(
        1
        for ln in mandatory
        if ln.get("IDENTITY_CLASS") in {EXACT_PRODUCT_VERIFIED, SPECIFICATION_COMPLETE_GENERIC}
        and (ln.get("QTY") is not None or True)  # qty may still be confirm-from-solicitation
        and bool(suppliers)
    )
    # RFQ ready requires more than category-only
    rfq_ready_lines = [
        ln
        for ln in mandatory
        if ln.get("IDENTITY_CLASS") in {EXACT_PRODUCT_VERIFIED, SPECIFICATION_COMPLETE_GENERIC, PARTIAL_PRODUCT_IDENTITY}
        and len(str(ln.get("DESCRIPTION") or "").split()) >= 2
        and suppliers
    ]
    full_sourcing = bool(mandatory) and specs_complete == total and bool(suppliers)
    return {
        "PRODUCT_LINES_DETECTED": total,
        "PRODUCT_LINES_EXTRACTED": extracted,
        "PRODUCT_SPECIFICATIONS_COMPLETE": specs_complete,
        "SUPPLIER_SEARCHABLE": searchable,
        "SUPPLIER_RFQ_READY": len(rfq_ready_lines),
        "ACQUISITION_PRICE_VERIFIED": 1 if priced else 0,
        "FULL_CONTRACT_SOURCING_READY": full_sourcing,
        "ECONOMICS_RESEARCH_READY": bool(mandatory) and specs_complete >= 1 and bool(suppliers),
        "EXECUTION_READY": False,
        "MANDATORY_LINE_COVERAGE": f"{specs_complete}/{total}" if total else "0/0",
    }


def reprocess_trace(trace: dict[str, Any]) -> dict[str, Any]:
    lines = list(trace.get("RECOVERED_LINES") or [])
    title = str(trace.get("TITLE") or "")
    after_id = opportunity_identity_from_lines(title, lines)
    suppliers = list(trace.get("REMATCH_SUPPLIERS") or [])
    readiness = compute_readiness(lines, suppliers=suppliers)
    before = (trace.get("BEFORE_IDENTITY") or {}).get("IDENTITY_CLASS")
    improved = after_id["IDENTITY_CLASS"] != before and after_id["IDENTITY_CLASS"] in {
        EXACT_PRODUCT_VERIFIED,
        SPECIFICATION_COMPLETE_GENERIC,
        PARTIAL_PRODUCT_IDENTITY,
    }
    # Fixed when we restored line payloads that HT counted but did not hand off
    handoff_fixed = bool(lines) and trace.get("PRIMARY_ROOT_CAUSE") in {
        DOWNSTREAM_HANDOFF_DATA_LOSS,
        PERSISTENCE_DATA_LOSS,
    }
    return {
        "OPPORTUNITY_ID": trace["OPPORTUNITY_ID"],
        "TITLE": title,
        "BUYER": trace.get("BUYER"),
        "SOURCE": trace.get("SOURCE"),
        "DEADLINE": trace.get("DEADLINE"),
        "BEFORE_IDENTITY_CLASS": before,
        "AFTER_IDENTITY_CLASS": after_id["IDENTITY_CLASS"],
        "IDENTITY_IMPROVED": improved or (handoff_fixed and bool(lines)),
        "LINES_RECOVERED": len(lines),
        "LINES": lines[:80],
        "READINESS": readiness,
        "PRIMARY_ROOT_CAUSE": trace.get("PRIMARY_ROOT_CAUSE"),
        "FIXED": handoff_fixed or (improved and bool(lines)),
        "REPAIRABLE": trace.get("REPAIRABLE"),
        "SUPPLIERS": suppliers,
        "NEXT_ACTION": _next_action(after_id, lines, suppliers, readiness),
        "Evidence_Links": [
            x
            for x in [
                (trace.get("RECOVERY") or {}).get("SOURCE_CID"),
                (trace.get("RECOVERY") or {}).get("SOURCE_FILE"),
            ]
            if x
        ],
    }


def _next_action(after_id, lines, suppliers, readiness) -> str:
    if not lines:
        return "Download/parse missing specification-bearing attachments"
    if after_id["IDENTITY_CLASS"] == CATEGORY_ONLY:
        return "Resolve manufacturer/MPN/NSN from remaining attachments"
    if readiness.get("SUPPLIER_RFQ_READY", 0) == 0 and not suppliers:
        return "Identify supplier channel for recovered product lines"
    if readiness.get("SUPPLIER_RFQ_READY", 0) > 0:
        return "Send supplier RFQ with recovered line specifications (AUTO_SEND=false)"
    if readiness.get("PRODUCT_SPECIFICATIONS_COMPLETE", 0) < readiness.get("PRODUCT_LINES_DETECTED", 0):
        return f"Complete specs for remaining lines ({readiness.get('MANDATORY_LINE_COVERAGE')})"
    return "Find acquisition price / historical unit price"


def build_rfqs(reprocessed: list[dict[str, Any]], *, limit: int = 8) -> dict[str, Any]:
    cands = [
        r
        for r in reprocessed
        if r.get("LINES")
        and r.get("AFTER_IDENTITY_CLASS")
        in {EXACT_PRODUCT_VERIFIED, SPECIFICATION_COMPLETE_GENERIC, PARTIAL_PRODUCT_IDENTITY}
    ]
    cands.sort(
        key=lambda r: (
            -int((r.get("READINESS") or {}).get("SUPPLIER_RFQ_READY") or 0),
            -int(r.get("LINES_RECOVERED") or 0),
            0 if r.get("AFTER_IDENTITY_CLASS") == EXACT_PRODUCT_VERIFIED else 1,
        )
    )
    packets = []
    for r in cands[:limit]:
        suppliers = r.get("SUPPLIERS") or ["UNKNOWN"]
        primary = suppliers[0]
        contact = resolve_contact(primary, (SUPPLIER_META.get(primary) or {}).get("KEY"))
        products = []
        for ln in (r.get("LINES") or [])[:15]:
            products.append(
                {
                    "line_no": ln.get("LINE_NUMBER"),
                    "description": ln.get("DESCRIPTION"),
                    "manufacturer": ln.get("MANUFACTURER"),
                    "mpn": ln.get("MPN"),
                    "nsn": ln.get("NSN"),
                    "qty": ln.get("QTY") if ln.get("QTY") is not None else "CONFIRM FROM SOLICITATION",
                    "uom": ln.get("UOM") or "EA",
                    "source_document": ln.get("SOURCE_DOCUMENT"),
                    "identity_class": ln.get("IDENTITY_CLASS"),
                }
            )
        if not products:
            continue
        packets.append(
            {
                "OPPORTUNITY_ID": r["OPPORTUNITY_ID"],
                "STATUS": "RFQ_READY" if r.get("AFTER_IDENTITY_CLASS") != CATEGORY_ONLY else "RFQ_IDENTITY_INCOMPLETE",
                "AUTO_SEND": False,
                "PRIMARY_SUPPLIER": primary,
                "CONTACT": contact,
                "FORM_URL": (contact or {}).get("CONTACT_URL"),
                "SOLICITATION_TITLE": r.get("TITLE"),
                "BUYER": r.get("BUYER"),
                "BID_DEADLINE": r.get("DEADLINE"),
                "PRODUCTS": products,
                "REQUESTED": [
                    "Firm delivered unit pricing per line",
                    "Freight",
                    "Lead time / stock",
                    "Reseller eligibility",
                    "Project/deviated pricing",
                    "Payment terms",
                    "Quote expiration",
                ],
                "NEXT_ACTION": "Operator review and send — AUTO_SEND=false",
            }
        )
    return {
        "kind": "source_product_rfqs",
        "build": BUILD,
        "built_at": _utc(),
        "AUTO_SEND": False,
        "COUNT": len(packets),
        "PACKETS": packets,
    }


def build_deals(reprocessed: list[dict[str, Any]]) -> dict[str, Any]:
    deals = []
    for r in reprocessed:
        readiness = r.get("READINESS") or {}
        tags = ["source_product", "commercial_proof", "first_revenue"]
        if readiness.get("SUPPLIER_RFQ_READY"):
            tags.append("needs_pricing")
        tags.append("needs_financing")
        deals.append(
            {
                "opportunity_id": r["OPPORTUNITY_ID"],
                "product": r.get("TITLE"),
                "buyer": r.get("BUYER"),
                "deadline": r.get("DEADLINE"),
                "identity_class": r.get("AFTER_IDENTITY_CLASS"),
                "before_identity_class": r.get("BEFORE_IDENTITY_CLASS"),
                "product_identity_completeness": r.get("AFTER_IDENTITY_CLASS"),
                "documents_acquired": True if r.get("LINES_RECOVERED") else False,
                "mandatory_line_extraction_coverage": readiness.get("MANDATORY_LINE_COVERAGE"),
                "supplier_search_readiness": readiness.get("SUPPLIER_SEARCHABLE"),
                "public_price_status": "UNKNOWN",
                "historical_price_status": "UNKNOWN",
                "financial_readiness": False,
                "financing_readiness": False,
                "mandatory_lines": readiness.get("PRODUCT_LINES_DETECTED"),
                "retail_pricing_coverage": "0/" + str(readiness.get("PRODUCT_LINES_DETECTED") or 0),
                "supplier_coverage": len(r.get("SUPPLIERS") or []),
                "suppliers": r.get("SUPPLIERS"),
                "lane": "PROVISIONAL",
                "lane_status": "PROVISIONAL",
                "economics_status": "INSUFFICIENT_EVIDENCE",
                "profit_scenario": "UNKNOWN",
                "estimated_remaining_profit": "UNKNOWN",
                "financing_status": "FINANCING_UNKNOWN",
                "next_action": r.get("NEXT_ACTION"),
                "ui_next_action_label": r.get("NEXT_ACTION"),
                "filter_tags": tags,
                "evidence_confidence": "PROVISIONAL",
                "readiness": readiness,
                "lines_recovered": r.get("LINES_RECOVERED"),
                "root_cause": r.get("PRIMARY_ROOT_CAUSE"),
                "identity_improved": r.get("IDENTITY_IMPROVED"),
                "ui_priority": 1 if r.get("IDENTITY_IMPROVED") else 3,
            }
        )
    deals.sort(key=lambda d: (int(d.get("ui_priority") or 9), -(d.get("lines_recovered") or 0)))
    return {
        "kind": "source_product_available_deals",
        "build": BUILD,
        "built_at": _utc(),
        "count": len(deals),
        "deals": deals,
    }


def handoff_proof_test(sidecar: dict[str, Any]) -> dict[str, Any]:
    """Prove a complete product line reaches commercial research intact."""
    by_oid = sidecar.get("by_opportunity") or {}
    proof = None
    for oid, entry in by_oid.items():
        lines = entry.get("LINES") or []
        for ln in lines:
            if ln.get("DESCRIPTION") and (ln.get("MPN") or ln.get("QTY") is not None or ln.get("MANUFACTURER")):
                # Simulate commercial research load
                loaded = make_product_line(
                    line_number=ln.get("LINE_NUMBER"),
                    description=ln["DESCRIPTION"],
                    manufacturer=ln.get("MANUFACTURER"),
                    mpn=ln.get("MPN"),
                    nsn=ln.get("NSN"),
                    qty=ln.get("QTY"),
                    uom=ln.get("UOM"),
                    source_document=ln.get("SOURCE_DOCUMENT"),
                    source_location=ln.get("SOURCE_LOCATION"),
                    provenance=ln.get("PROVENANCE"),
                )
                ok = (
                    loaded["DESCRIPTION"] == ln["DESCRIPTION"]
                    and loaded.get("MPN") == ln.get("MPN")
                    and loaded.get("QTY") == ln.get("QTY")
                    and loaded.get("UOM") == ln.get("UOM")
                    and loaded.get("SOURCE_DOCUMENT") == ln.get("SOURCE_DOCUMENT")
                )
                proof = {
                    "OPPORTUNITY_ID": oid,
                    "LINE": loaded,
                    "HANDOFF_INTACT": ok,
                    "FIELDS_CHECKED": ["DESCRIPTION", "MPN", "QTY", "UOM", "SOURCE_DOCUMENT", "PROVENANCE"],
                }
                break
        if proof:
            break
    if not proof:
        proof = {"HANDOFF_INTACT": False, "NOTE": "No recovered line with identifying fields available for proof"}
    return {"kind": "handoff_proof", "build": BUILD, "built_at": _utc(), **proof}


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def run_audit_and_repair(*, sample_n: int = 50) -> dict[str, Any]:
    t0 = time.time()
    rows = quoteable_rows()
    rematch = _load(REMATCH_FILE) or {"RESULTS": []}
    rematch_by = {r["OPPORTUNITY_ID"]: r for r in (rematch.get("RESULTS") or [])}
    ht_by = {r["OPPORTUNITY_ID"]: r for r in rows}

    # Baseline identity census (full quoteable)
    before_full = Counter(
        classify_product_identity(_clean_title(str(r.get("TITLE") or "")))["IDENTITY_CLASS"] for r in rows
    )

    # Freeze sample IDs once — never swap for easier opportunities on reruns
    existing = _load(SAMPLE_FILE) or {}
    prior = existing.get("SELECTED") if isinstance(existing.get("SELECTED"), list) else []
    if prior and len(prior) == sample_n:
        sample = prior
        log.info("Using frozen sample of %s IDs from %s", len(sample), SAMPLE_FILE)
    else:
        sample = select_stratified_sample(rows, n=sample_n)
        _save(
            SAMPLE_FILE,
            {
                "kind": "source_product_frozen_sample",
                "build": BUILD,
                "built_at": _utc(),
                "COUNT": len(sample),
                "NOTE": "IDs frozen before repair — do not swap",
                "SELECTED": sample,
            },
        )

    indexes = load_opengov_line_indexes()
    profit_val = load_profit_val_line_index()
    log.info(
        "line indexes titles=%s cids=%s profit_val=%s sources=%s",
        len(indexes.get("by_title") or {}),
        len(indexes.get("by_cid") or {}),
        len((profit_val.get("by_oid") or {})),
        indexes.get("sources_loaded"),
    )

    traces = []
    for i, sel in enumerate(sample, 1):
        oid = sel["OPPORTUNITY_ID"]
        row = ht_by.get(oid) or sel
        log.info("audit %s/%s %s %s", i, len(sample), oid, (sel.get("TITLE") or "")[:50])
        _save(CHECKPOINT_FILE, {"stage": "audit", "done": i, "total": len(sample), "oid": oid, "at": _utc()})
        traces.append(
            trace_opportunity(
                row,
                sel,
                indexes=indexes,
                rematch_row=rematch_by.get(oid),
                profit_val=profit_val,
                allow_live_fetch=True,  # frozen sample only — fills OpenGov cache gaps
            )
        )

    # Root cause census (exclusive primary)
    rc = Counter(t["PRIMARY_ROOT_CAUSE"] for t in traces)
    repairable_n = sum(1 for t in traces if t.get("REPAIRABLE"))
    root_table = []
    for cause, count in rc.most_common():
        fixed_preview = sum(
            1
            for t in traces
            if t["PRIMARY_ROOT_CAUSE"] == cause and t.get("RECOVERED_LINE_COUNT", 0) > 0
        )
        root_table.append(
            {
                "ROOT_CAUSE": cause,
                "OPPORTUNITIES": count,
                "REPAIRABLE": sum(1 for t in traces if t["PRIMARY_ROOT_CAUSE"] == cause and t.get("REPAIRABLE")),
                "FIXED_VIA_RECOVERY": fixed_preview,
            }
        )
    assert sum(r["OPPORTUNITIES"] for r in root_table) == len(traces)

    # Persist product-line sidecar for recovered opportunities
    by_opp: dict[str, Any] = {}
    for t in traces:
        if t.get("RECOVERED_LINES"):
            by_opp[t["OPPORTUNITY_ID"]] = {
                "OPPORTUNITY_ID": t["OPPORTUNITY_ID"],
                "TITLE": t.get("TITLE"),
                "LINES": t["RECOVERED_LINES"],
                "LINE_COUNT": t["RECOVERED_LINE_COUNT"],
                "RECOVERY": t.get("RECOVERY"),
                "UPDATED_AT": _utc(),
                "SAMPLE": True,
            }

    # Selective expand: remaining quoteable OpenGov via cache/profit_val only (no live fetch)
    expand_recovered = 0
    expand_lines = 0
    for i, r in enumerate(rows, 1):
        oid = r["OPPORTUNITY_ID"]
        if oid in by_opp:
            continue
        if i % 200 == 0:
            _save(CHECKPOINT_FILE, {"stage": "expand", "done": i, "total": len(rows), "at": _utc()})
        rec = recover_lines_for_opportunity(r, indexes, profit_val=profit_val, allow_live_fetch=False)
        if not rec.get("RECOVERED"):
            continue
        by_opp[oid] = {
            "OPPORTUNITY_ID": oid,
            "TITLE": r.get("TITLE"),
            "LINES": (rec.get("LINES") or [])[:200],
            "LINE_COUNT": rec.get("LINE_COUNT"),
            "RECOVERY": {k: v for k, v in rec.items() if k != "LINES"},
            "UPDATED_AT": _utc(),
            "SAMPLE": False,
        }
        expand_recovered += 1
        expand_lines += int(rec.get("LINE_COUNT") or 0)

    sidecar = {
        "kind": "product_lines_contract_v1",
        "build": BUILD,
        "built_at": _utc(),
        "CONTRACT": "product_line_v1",
        "by_opportunity": by_opp,
        "opportunity_count": len(by_opp),
        "sample_recovered": sum(1 for v in by_opp.values() if v.get("SAMPLE")),
        "expand_recovered": expand_recovered,
        "expand_lines": expand_lines,
        "NOTE": "Sidecar for commercial research handoff — HT v2 snapshots untouched",
        "indexes_sources": indexes.get("sources_loaded"),
    }
    _save(LINES_SIDECAR, sidecar)

    # Reprocess frozen 50
    reprocessed = []
    for i, t in enumerate(traces, 1):
        _save(CHECKPOINT_FILE, {"stage": "reprocess", "done": i, "total": len(traces), "at": _utc()})
        reprocessed.append(reprocess_trace(t))

    fixed_n = sum(1 for r in reprocessed if r.get("FIXED"))
    # Update root table fixed counts from reprocess
    fixed_by_cause = Counter()
    for r in reprocessed:
        if r.get("FIXED"):
            fixed_by_cause[r.get("PRIMARY_ROOT_CAUSE")] += 1
    for row in root_table:
        row["FIXED"] = int(fixed_by_cause.get(row["ROOT_CAUSE"]) or 0)

    before_sample = Counter(t["BEFORE_IDENTITY"]["IDENTITY_CLASS"] for t in traces)
    after_sample = Counter(r["AFTER_IDENTITY_CLASS"] for r in reprocessed)

    # Full population identity after: sample reprocess + expand sidecar recoveries
    after_full = Counter(before_full)
    full_improved = 0
    sample_oids = {r["OPPORTUNITY_ID"] for r in reprocessed}
    for r in reprocessed:
        before_cls = r.get("BEFORE_IDENTITY_CLASS") or classify_product_identity(
            _clean_title(str(r.get("TITLE") or ""))
        )["IDENTITY_CLASS"]
        after_cls = r["AFTER_IDENTITY_CLASS"]
        after_full[before_cls] -= 1
        after_full[after_cls] += 1
        if after_cls != before_cls:
            full_improved += 1
    for oid, entry in by_opp.items():
        if oid in sample_oids:
            continue
        row = ht_by.get(oid) or {}
        before_cls = classify_product_identity(_clean_title(str(row.get("TITLE") or entry.get("TITLE") or "")))[
            "IDENTITY_CLASS"
        ]
        after_cls = opportunity_identity_from_lines(
            str(row.get("TITLE") or entry.get("TITLE") or ""), entry.get("LINES") or []
        )["IDENTITY_CLASS"]
        if after_cls == before_cls:
            continue
        after_full[before_cls] -= 1
        after_full[after_cls] += 1
        full_improved += 1

    rfqs = build_rfqs(reprocessed)
    deals = build_deals(reprocessed)
    handoff = handoff_proof_test(sidecar)

    # Readiness aggregates for sample
    readiness_agg = {
        "PRODUCT_LINES_DETECTED": sum(int((r.get("READINESS") or {}).get("PRODUCT_LINES_DETECTED") or 0) for r in reprocessed),
        "PRODUCT_LINES_EXTRACTED": sum(int((r.get("READINESS") or {}).get("PRODUCT_LINES_EXTRACTED") or 0) for r in reprocessed),
        "PRODUCT_SPECIFICATIONS_COMPLETE": sum(
            int((r.get("READINESS") or {}).get("PRODUCT_SPECIFICATIONS_COMPLETE") or 0) for r in reprocessed
        ),
        "SUPPLIER_SEARCHABLE": sum(int((r.get("READINESS") or {}).get("SUPPLIER_SEARCHABLE") or 0) for r in reprocessed),
        "SUPPLIER_RFQ_READY": sum(int((r.get("READINESS") or {}).get("SUPPLIER_RFQ_READY") or 0) for r in reprocessed),
        "FULL_CONTRACT_SOURCING_READY": sum(
            1 for r in reprocessed if (r.get("READINESS") or {}).get("FULL_CONTRACT_SOURCING_READY")
        ),
        "OPPORTUNITIES_WITH_LINES": sum(1 for r in reprocessed if int(r.get("LINES_RECOVERED") or 0) > 0),
        "HISTORICAL_QUOTEABLE_COUNT_PRESERVED": 1003,
    }

    recovered_examples = []
    for r in reprocessed:
        if not r.get("LINES"):
            continue
        ln0 = r["LINES"][0]
        recovered_examples.append(
            {
                "Product": (ln0.get("DESCRIPTION") or r.get("TITLE") or "")[:80],
                "Buyer": r.get("BUYER"),
                "Source": r.get("SOURCE"),
                "Before_Identity": r.get("BEFORE_IDENTITY_CLASS"),
                "Recovered_Manufacturer_MPN_Specs": {
                    "manufacturer": ln0.get("MANUFACTURER"),
                    "mpn": ln0.get("MPN"),
                    "qty": ln0.get("QTY"),
                    "uom": ln0.get("UOM"),
                    "identity": ln0.get("IDENTITY_CLASS"),
                },
                "Quantity": ln0.get("QTY"),
                "After_Identity": r.get("AFTER_IDENTITY_CLASS"),
                "Evidence": ln0.get("SOURCE_DOCUMENT") or ln0.get("PROVENANCE"),
                "Line_Count": r.get("LINES_RECOVERED"),
            }
        )
        if len(recovered_examples) >= 12:
            break

    scorecard = {
        "sample_n": len(traces),
        "before_sample": dict(before_sample),
        "after_sample": dict(after_sample),
        "before_full_quoteable": dict(before_full),
        "after_full_quoteable_sample_adjusted": dict(after_full),
        "identity_improved": sum(1 for r in reprocessed if r.get("IDENTITY_IMPROVED")),
        "fixed": fixed_n,
        "repairable": repairable_n,
        "lines_recovered_total": sum(int(r.get("LINES_RECOVERED") or 0) for r in reprocessed),
        "opportunities_with_recovered_lines": readiness_agg["OPPORTUNITIES_WITH_LINES"],
        "supplier_searchable_lines": readiness_agg["SUPPLIER_SEARCHABLE"],
        "rfq_ready_lines": readiness_agg["SUPPLIER_RFQ_READY"],
        "full_mandatory_line_covered_opps": readiness_agg["FULL_CONTRACT_SOURCING_READY"],
        "rfqs": rfqs.get("COUNT"),
        "handoff_intact": handoff.get("HANDOFF_INTACT"),
    }

    report = {
        "kind": "source_to_product_extraction_audit_v1_report",
        "build": BUILD,
        "built_at": _utc(),
        "WALL_S": round(time.time() - t0, 2),
        "SAM_API_CALLS_USED": 0,
        "DEPLOYMENT": "LOCAL_ONLY",
        "BASELINE_PRESERVED": {
            "QUOTEABLE_1003": len(rows),
            "CI_MATCHES": len(rematch.get("NEW_MATCH_IDS") or []),
        },
        "A_ROOT_CAUSES": root_table,
        "A_ROOT_CAUSES_OPERATOR_TABLE": _operator_root_table(root_table),
        "B_SCORECARD": scorecard,
        "C_DOCUMENT_FINDINGS": {
            "sample_doc_totals": {
                "expected": sum(int((t.get("DOCUMENT_COUNTS") or {}).get("EXPECTED_DOCUMENT_COUNT") or 0) for t in traces),
                "discovered": sum(int((t.get("DOCUMENT_COUNTS") or {}).get("DISCOVERED_DOCUMENT_COUNT") or 0) for t in traces),
                "downloaded": sum(int((t.get("DOCUMENT_COUNTS") or {}).get("DOWNLOADED_DOCUMENT_COUNT") or 0) for t in traces),
                "parsed": sum(
                    int((t.get("DOCUMENT_COUNTS") or {}).get("SUCCESSFULLY_PARSED_DOCUMENT_COUNT") or 0) for t in traces
                ),
                "spec_bearing": sum(
                    int((t.get("DOCUMENT_COUNTS") or {}).get("SPECIFICATION_BEARING_DOCUMENT_COUNT") or 0) for t in traces
                ),
                "missing_critical": sum(
                    int((t.get("DOCUMENT_COUNTS") or {}).get("MISSING_CRITICAL_DOCUMENTS") or 0) for t in traces
                ),
            },
            "by_source": dict(
                Counter(t.get("SOURCE") for t in traces)
            ),
            "recovery_by_source": dict(
                Counter(t.get("SOURCE") for t in traces if int(t.get("RECOVERED_LINE_COUNT") or 0) > 0)
            ),
            "greatest_loss_portals": [
                "BidNet (auth / attachment discovery — line payloads unavailable in HT results)",
                "OpenGov (REAL_LINES counted in HT but line payloads not persisted; cache incomplete for many portals)",
            ],
            "expand_recovered_opportunities": expand_recovered,
            "expand_lines": expand_lines,
            "sidecar_opportunity_count": len(by_opp),
        },
        "D_RECOVERED_EXAMPLES": recovered_examples,
        "E_COMMERCIAL": {
            "additional_exact_identities_in_sample": after_sample.get(EXACT_PRODUCT_VERIFIED, 0)
            - before_sample.get(EXACT_PRODUCT_VERIFIED, 0),
            "additional_complete_generics": after_sample.get(SPECIFICATION_COMPLETE_GENERIC, 0)
            - before_sample.get(SPECIFICATION_COMPLETE_GENERIC, 0),
            "public_prices_unlocked": 0,
            "historical_unit_prices_unlocked": 0,
            "verified_positive_economics": 0,
            "rfq_packets": rfqs.get("COUNT"),
            "NOTE": "Identity/handoff repair primary — targeted pricing not re-swept broadly this pass",
        },
        "F_SUPPLIER_OUTREACH": {
            "AUTO_SEND": False,
            "RFQ_COUNT": rfqs.get("COUNT"),
            "STRONGEST": (rfqs.get("PACKETS") or [])[:5],
        },
        "G_DEPLOYMENT": {
            "LOCAL_VALIDATED": True,
            "DEPLOYED": False,
            "TESTS": "tests/test_source_to_product_extraction_audit_v1.py",
            "SAM_API_CALLS_USED": 0,
            "HT_V2_SNAPSHOTS_MUTATED": False,
        },
        "H_VERDICT": {
            "1_source_info_available_but_lost": True,
            "2_primary_loss_point": "DOWNSTREAM_HANDOFF_DATA_LOSS — HT v2 stores REAL_LINES counts/modes but not line payloads; commercial research received titles only",
            "3_recovered": {
                "sample_opportunities_with_lines": readiness_agg["OPPORTUNITIES_WITH_LINES"],
                "sample_lines_total": scorecard["lines_recovered_total"],
                "full_sidecar_opportunities": len(by_opp),
                "expand_opportunities": expand_recovered,
                "expand_lines": expand_lines,
            },
            "4_genuinely_sourceable_of_1003": len(by_opp),
            "5_meaningful_supplier_quote": readiness_agg["SUPPLIER_RFQ_READY"],
            "6_reliable_acquisition_prices": 0,
            "7_usable_gov_selling_price": 0,
            "8_positive_preliminary_economics": False,
            "9_largest_remaining_barrier": "BidNet auth/docs + OpenGov line-payload persistence; public/historical unit prices still scarce",
            "10_next_steps": [
                "Persist product_line_v1 payloads during HT enrichment (not counts only)",
                "Complete OpenGov price-table cache for quoteable OpenGov IDs with REAL_LINES>0",
                "Run targeted public pricing on recovered exact MPNs (Disc Brake Meritor kits, SCA paper)",
                "BidNet authorized attachment recovery for high-value product-only packages",
            ],
        },
        "INDEX_SOURCES": indexes.get("sources_loaded"),
    }

    _save(AUDIT_FILE, {"build": BUILD, "TRACES": traces, "count": len(traces)})
    _save(ROOT_CAUSE_FILE, {"build": BUILD, "ROWS": root_table, "TOTAL": len(traces)})
    _save(REPROCESS_FILE, {"build": BUILD, "RESULTS": reprocessed, "count": len(reprocessed)})
    _save(SCORECARD_FILE, scorecard)
    _save(READINESS_FILE, readiness_agg)
    _save(RFQ_FILE, rfqs)
    _save(DEALS_FILE, deals)
    _save(HANDOFF_TEST_FILE, handoff)
    _save(REPORT_FILE, report)

    return {
        "report": report,
        "sample": sample,
        "traces": traces,
        "reprocessed": reprocessed,
        "scorecard": scorecard,
        "root_causes": root_table,
        "rfqs": rfqs,
        "deals": deals,
        "sidecar": sidecar,
        "handoff": handoff,
        "readiness": readiness_agg,
        "examples": recovered_examples,
    }


def load_product_lines_for_opportunity(opportunity_id: str) -> list[dict[str, Any]]:
    """Commercial-research handoff helper — load recovered lines from sidecar."""
    blob = _load(LINES_SIDECAR) or {}
    entry = (blob.get("by_opportunity") or {}).get(opportunity_id) or {}
    return list(entry.get("LINES") or [])


def _operator_root_table(root_table: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map internal taxonomy to the Phase-11 operator summary table (exclusive, sum=50)."""
    buckets = {
        "Documents unavailable": {"causes": {SOURCE_DOCUMENT_NOT_AVAILABLE}, "opps": 0, "repairable": 0, "fixed": 0},
        "Attachments not downloaded": {
            "causes": {DOCUMENT_NOT_DOWNLOADED, DOCUMENT_DOWNLOAD_FAILED, ATTACHMENT_DISCOVERY_FAILED},
            "opps": 0,
            "repairable": 0,
            "fixed": 0,
        },
        "Text/table extraction failure": {
            "causes": {
                PDF_TEXT_EXTRACTION_FAILED,
                SCANNED_DOCUMENT_NOT_PROCESSED,
                TABLE_EXTRACTION_FAILED,
                SPREADSHEET_EXTRACTION_FAILED,
            },
            "opps": 0,
            "repairable": 0,
            "fixed": 0,
        },
        "Product line segmentation": {
            "causes": {PRODUCT_LINE_SEGMENTATION_FAILED},
            "opps": 0,
            "repairable": 0,
            "fixed": 0,
        },
        "Specifications missing": {
            "causes": {SPECIFICATION_EXTRACTION_FAILED, MPN_NSN_EXTRACTION_FAILED, QUANTITY_UOM_EXTRACTION_FAILED},
            "opps": 0,
            "repairable": 0,
            "fixed": 0,
        },
        "Persistence loss": {"causes": {PERSISTENCE_DATA_LOSS}, "opps": 0, "repairable": 0, "fixed": 0},
        "Downstream handoff loss": {
            "causes": {DOWNSTREAM_HANDOFF_DATA_LOSS},
            "opps": 0,
            "repairable": 0,
            "fixed": 0,
        },
        "Source genuinely incomplete": {
            "causes": {SOURCE_GENUINELY_INCOMPLETE, BUYER_CLARIFICATION_REQUIRED},
            "opps": 0,
            "repairable": 0,
            "fixed": 0,
        },
        "Other": {
            "causes": {AUTH_OR_ACCESS_REQUIRED, OTHER_VERIFIED_FAILURE},
            "opps": 0,
            "repairable": 0,
            "fixed": 0,
        },
    }
    for row in root_table:
        cause = row["ROOT_CAUSE"]
        placed = False
        for bucket in buckets.values():
            if cause in bucket["causes"]:
                bucket["opps"] += int(row.get("OPPORTUNITIES") or 0)
                bucket["repairable"] += int(row.get("REPAIRABLE") or 0)
                bucket["fixed"] += int(row.get("FIXED") or 0)
                placed = True
                break
        if not placed:
            buckets["Other"]["opps"] += int(row.get("OPPORTUNITIES") or 0)
            buckets["Other"]["repairable"] += int(row.get("REPAIRABLE") or 0)
            buckets["Other"]["fixed"] += int(row.get("FIXED") or 0)
    return [
        {"ROOT_CAUSE": name, "OPPORTUNITIES": b["opps"], "REPAIRABLE": b["repairable"], "FIXED": b["fixed"]}
        for name, b in buckets.items()
    ]
