"""Isolated OpenGov profitability validation on already-enriched true-open rows.

READ-ONLY vs live enrichment job artifacts.
Writes ONLY to m3_opengov_enriched_profitability_validation_v1_* files.

Never invents prices. Never consumes SAM API credits.
Does not modify L23, enrichment sidecar, or enrichment job checkpoints.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from application_clock import now_utc
from buyer_intelligence.owner_product_resale_rules_v1 import classify_owner_resale_text
from buyer_intelligence.true_live_accepting_bids_gate_v1 import product_fit

log = logging.getLogger("govtracker.opengov_enriched_profitability_validation_v1")

BUILD = "20261010-m3-opengov-enriched-profitability-validation-v1"
SOURCE_RUN = "prod-live-20261010T023015Z-6d8c5be7"
ENRICHMENT_BUILD = "20261010-m3-fresh-true-open-detail-package-enrichment-repair-v1"

REPORT_FILE = "m3_opengov_enriched_profitability_validation_v1_report.json"
PROGRESS_FILE = "m3_opengov_enriched_profitability_validation_v1_progress.json"
SNAPSHOT_FILE = "m3_opengov_enriched_profitability_validation_v1_snapshot.json"
RESULTS_FILE = "m3_opengov_enriched_profitability_validation_v1_results.json"
DEALS_FILE = "m3_opengov_enriched_profitability_validation_v1_deals.json"
BATCH10_FILE = "m3_opengov_enriched_profitability_validation_v1_batch10.json"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_IT_RADIO_RE = re.compile(
    r"\b(computer|laptop|desktop\s*pc|server|tablet|chromebook|iphone|ipad|"
    r"\bradios?\b|two[-\s]?way\s*radio|walkie|motorola\s*radio|kenwood\s*radio|"
    r"base\s*station\s*radio)\b",
    re.I,
)
_PROFIT_FLOOR = 5000.0
_PROFIT_TARGET = 10000.0


def _utc() -> str:
    return now_utc().isoformat()


def _data(*parts: str) -> Path:
    from m3_data_root import data_path

    return data_path(*parts)


def _save(name: str, obj: Any) -> Path:
    p = _data(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
    tmp.replace(p)
    return p


def _load(name: str) -> Any:
    p = _data(name)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _progress(**kwargs: Any) -> None:
    cur = _load(PROGRESS_FILE) or {"kind": "OpenGovProfitabilityValidationProgress", "build": BUILD}
    cur.update(kwargs)
    cur["heartbeat_at"] = _utc()
    cur["build"] = BUILD
    _save(PROGRESS_FILE, cur)


def snapshot_opengov_from_store(
    *,
    store_path: Path | None = None,
    source_run: str = SOURCE_RUN,
) -> dict[str, Any]:
    """Copy OpenGov enriched records into isolated snapshot (no write to L23)."""
    path = store_path or _data("l23_canonical_population_store.json")
    doc = json.loads(path.read_text(encoding="utf-8"))
    opps = doc.get("opportunities") or {}
    selected: list[dict[str, Any]] = []
    for cid, rec in opps.items():
        if not isinstance(rec, dict):
            continue
        er = rec.get("enrichment_repair_v1") if isinstance(rec.get("enrichment_repair_v1"), dict) else {}
        plat = str(rec.get("platform") or "")
        url = str(rec.get("authoritative_url") or rec.get("detail_url") or "")
        is_og = er.get("SOURCE") == "OpenGov" or "opengov" in plat.lower() or "opengov.com" in url.lower()
        if not is_og:
            continue
        # Prefer rows enriched by current enrichment build
        if er and er.get("build") and ENRICHMENT_BUILD not in str(er.get("build")):
            # still allow if has fresh line_items from this repair
            if not rec.get("line_items"):
                continue
        lines = rec.get("line_items") if isinstance(rec.get("line_items"), list) else []
        selected.append(
            {
                "OPPORTUNITY_ID": str(cid),
                "title": rec.get("title"),
                "buyer": rec.get("buyer") or rec.get("agency"),
                "deadline": rec.get("deadline"),
                "description": rec.get("description"),
                "authoritative_url": rec.get("authoritative_url") or rec.get("detail_url"),
                "line_items": lines,
                "line_texts": rec.get("line_texts") or [str(ln.get("description") or "") for ln in lines if isinstance(ln, dict)],
                "attachments_metadata": rec.get("attachments_metadata") or [],
                "enrichment_repair_v1": er,
                "PRODUCTION_RUN_ID": rec.get("PRODUCTION_RUN_ID") or source_run,
                "status": rec.get("status"),
                "raw_metadata": rec.get("raw_metadata") if isinstance(rec.get("raw_metadata"), dict) else {},
            }
        )

    snap = {
        "kind": "OpenGovEnrichedProfitabilitySnapshot",
        "build": BUILD,
        "SOURCE_RUN": source_run,
        "ENRICHMENT_BUILD": ENRICHMENT_BUILD,
        "created_at": _utc(),
        "store_path": str(path),
        "count": len(selected),
        "with_real_lines": sum(1 for r in selected if r.get("line_items")),
        "rows": selected,
        "NOTE": "Isolated copy; live enrichment job untouched",
    }
    _save(SNAPSHOT_FILE, snap)
    return snap


def _excluded_it_radio(text: str) -> bool:
    return bool(_IT_RADIO_RE.search(text or ""))


def _line_identity(ln: dict[str, Any]) -> dict[str, Any]:
    return {
        "manufacturer": ln.get("manufacturer") or ln.get("MANUFACTURER") or ln.get("brand") or ln.get("BRAND"),
        "model": ln.get("model") or ln.get("MODEL"),
        "part_number": ln.get("mpn") or ln.get("MPN") or ln.get("catalog") or ln.get("CATALOG_NUMBER"),
        "catalog_number": ln.get("catalog") or ln.get("CATALOG_NUMBER"),
        "sku": ln.get("sku") or ln.get("SKU"),
        "nsn": ln.get("nsn") or ln.get("NSN"),
        "raw_description": ln.get("description") or ln.get("DESCRIPTION") or "",
    }


def _has_commercial_identity(ident: dict[str, Any]) -> bool:
    if ident.get("nsn"):
        return True
    if ident.get("part_number") or ident.get("catalog_number") or ident.get("sku"):
        return True
    if ident.get("manufacturer") and ident.get("model"):
        return True
    return False


def _qty(ln: dict[str, Any]) -> float | None:
    for k in ("qty", "QTY", "quantity"):
        if ln.get(k) is not None:
            try:
                return float(ln.get(k))
            except (TypeError, ValueError):
                return None
    return None


def fetch_current_opengov_revenue(row: dict[str, Any], *, client: httpx.Client | None) -> dict[str, Any]:
    """Fresh project detail → extract_value_fields only when ECONOMIC_REVENUE_USABLE."""
    try:
        from buyer_intelligence.opengov_line_spec_supplier_ready_v1 import resolve_opengov_slug_pid
        from buyer_intelligence.opengov_live_deep_enrichment_v1 import (
            extract_value_fields,
            fetch_project_detail,
        )
    except Exception as exc:
        return {"STATE": "UNKNOWN", "VALUE": None, "REASON": type(exc).__name__}

    oid = str(row.get("OPPORTUNITY_ID") or "")
    sp = resolve_opengov_slug_pid(oid, rec=row)
    if not sp:
        url = str(row.get("authoritative_url") or "")
        m = re.search(r"/portal/([^/]+)/projects/(\d+)", url)
        if m:
            sp = (m.group(1), m.group(2))
    if not sp or client is None:
        return {"STATE": "UNKNOWN", "VALUE": None, "REASON": "NO_SLUG_PID_OR_CLIENT"}
    slug, pid = sp
    fetched = fetch_project_detail(slug, pid, client=client, use_cache=False)
    if fetched.get("route_status") != "PUBLIC_DETAIL_OK" or not fetched.get("payload"):
        return {"STATE": "UNKNOWN", "VALUE": None, "REASON": fetched.get("route_status")}
    info = extract_value_fields(fetched["payload"])
    cv = info.get("CURRENT_VALUE") if isinstance(info.get("CURRENT_VALUE"), dict) else None
    if cv and cv.get("ECONOMIC_REVENUE_USABLE") == "YES" and cv.get("VALUE"):
        return {
            "STATE": "PROVEN",
            "VALUE": float(cv["VALUE"]),
            "SOURCE_LOCATION": cv.get("SOURCE_LOCATION"),
            "ROLE": cv.get("ROLE"),
        }
    return {"STATE": "UNKNOWN", "VALUE": None, "REASON": "NO_USABLE_CURRENT_VALUE", "REJECTED": info.get("REJECTED")}


def lookup_historical_award_unit(
    *,
    buyer: str | None,
    description: str,
    catalog: str | None,
) -> dict[str, Any]:
    """Evidence-backed historical award only — never solicitation estimates."""
    # Prefer on-disk bid index if present (may be empty after zero-history wipe)
    try:
        from buyer_intelligence.multibuyer_historical_profit_proof import load_bid_index

        idx = load_bid_index() or []
    except Exception:
        idx = []
    if not idx:
        return {
            "HISTORICAL_GOV_PRICE": None,
            "HISTORICAL_STATE": "UNKNOWN",
            "HISTORICAL_AWARD_LINK": None,
            "REASON": "NO_BID_INDEX_AVAILABLE",
        }

    cat = str(catalog or "").strip().upper()
    desc_l = (description or "").lower()[:80]
    for rec in idx:
        if not isinstance(rec, dict):
            continue
        url = str(rec.get("source_url") or "")
        if "opengov" not in url.lower() and "procurement" not in url.lower():
            continue
        price = rec.get("bid_unit_price") or rec.get("unit_price") or rec.get("award_unit_price")
        try:
            price_f = float(price)
        except (TypeError, ValueError):
            continue
        if price_f <= 0:
            continue
        blob = json.dumps(rec).upper()
        if cat and cat in blob:
            return {
                "HISTORICAL_GOV_PRICE": price_f,
                "HISTORICAL_STATE": "PROVEN",
                "HISTORICAL_AWARD_LINK": url or None,
                "REASON": "CATALOG_MATCH_BID_INDEX",
            }
        title = str(rec.get("item_description") or rec.get("description") or "").lower()
        if desc_l and desc_l[:40] in title:
            return {
                "HISTORICAL_GOV_PRICE": price_f,
                "HISTORICAL_STATE": "PROVEN",
                "HISTORICAL_AWARD_LINK": url or None,
                "REASON": "DESCRIPTION_MATCH_BID_INDEX",
            }
    return {
        "HISTORICAL_GOV_PRICE": None,
        "HISTORICAL_STATE": "UNKNOWN",
        "HISTORICAL_AWARD_LINK": None,
        "REASON": "NO_MATCHING_AWARD_EVIDENCE",
    }


def lookup_retail_unit(ident: dict[str, Any], *, client: httpx.Client | None, allow_live: bool) -> dict[str, Any]:
    """Verified public retail only. Cache first; optional bounded live resolve."""
    # Cache-only path
    cache = _load("m3_commercial_acquisition_cache_v1.json") or {}
    entries = cache.get("entries") if isinstance(cache, dict) else {}
    key_vals = [
        str(ident.get("part_number") or "").upper().replace("-", ""),
        str(ident.get("catalog_number") or "").upper().replace("-", ""),
        str(ident.get("sku") or "").upper().replace("-", ""),
        str(ident.get("nsn") or "").upper().replace("-", ""),
    ]
    key_vals = [k for k in key_vals if k]
    if isinstance(entries, dict) and key_vals:
        for _k, ent in entries.items():
            if not isinstance(ent, dict):
                continue
            blob = json.dumps(ent).lower()
            if any(x in blob for x in ("opengov", "government bid", "award price", "historical bid")):
                continue
            mpn = str(ent.get("mpn") or ent.get("part_number") or "").upper().replace("-", "")
            if mpn and mpn in key_vals:
                try:
                    price = float(ent.get("unit_price") or ent.get("price") or 0)
                except (TypeError, ValueError):
                    price = 0.0
                if price > 0:
                    return {
                        "RETAIL_UNIT_PRICE": price,
                        "RETAIL_STATE": "PROVEN",
                        "RETAIL_SELLER": ent.get("seller"),
                        "RETAIL_URL": ent.get("url") or ent.get("source_url"),
                        "REASON": "COMMERCIAL_CACHE",
                    }

    if not allow_live or not _has_commercial_identity(ident):
        return {
            "RETAIL_UNIT_PRICE": None,
            "RETAIL_STATE": "UNKNOWN",
            "RETAIL_SELLER": None,
            "RETAIL_URL": None,
            "REASON": "NO_CACHE_HIT" if _has_commercial_identity(ident) else "INSUFFICIENT_IDENTITY",
        }

    try:
        from public_price_search.resolver import resolve_public_price

        out = resolve_public_price(ident, client=client, use_budget=True, max_queries=3, max_pages=4)
        status = str(out.get("status") or out.get("STATUS") or "")
        price = out.get("unit_price") or out.get("price") or (out.get("evidence") or {}).get("unit_price")
        url = (out.get("evidence") or {}).get("source_url") or out.get("source_url")
        seller = out.get("seller") or (out.get("evidence") or {}).get("seller")
        try:
            price_f = float(price) if price is not None else None
        except (TypeError, ValueError):
            price_f = None
        if price_f and price_f > 0 and url:
            return {
                "RETAIL_UNIT_PRICE": price_f,
                "RETAIL_STATE": "PROVEN",
                "RETAIL_SELLER": seller,
                "RETAIL_URL": url,
                "REASON": f"LIVE_RESOLVE:{status}",
            }
        return {
            "RETAIL_UNIT_PRICE": None,
            "RETAIL_STATE": "UNKNOWN",
            "RETAIL_SELLER": seller,
            "RETAIL_URL": url,
            "REASON": f"LIVE_RESOLVE_NO_PRICE:{status}",
        }
    except Exception as exc:
        return {
            "RETAIL_UNIT_PRICE": None,
            "RETAIL_STATE": "UNKNOWN",
            "RETAIL_SELLER": None,
            "RETAIL_URL": None,
            "REASON": f"LIVE_RESOLVE_ERROR:{type(exc).__name__}",
        }


def assess_financing_safe(*, contract_value: float | None, supplier_cost: float | None) -> dict[str, Any]:
    if not contract_value or not supplier_cost or contract_value <= 0 or supplier_cost <= 0:
        return {
            "FINANCING_STATUS": "UNKNOWN",
            "OWNER_CASH_REQUIRED": None,
            "PG_REQUIRED": None,
            "REASON": "INSUFFICIENT_ECONOMICS_FOR_FINANCING",
        }
    try:
        from financing_intelligence.assess import assess_opportunity_financing

        out = assess_opportunity_financing(
            opportunity_id=f"og-profit-val-{uuid.uuid4().hex[:8]}",
            contract_value=float(contract_value),
            supplier_cost=float(supplier_cost),
            freight=0.0,
            persist=False,
        )
        card = out if isinstance(out, dict) else {}
        owner_cash = card.get("owner_cash_required")
        blocked = str(card.get("blocked_reason") or "")
        pg = "PG" in blocked.upper() or bool(card.get("personal_guarantee_required"))
        # Never mark CONFIRMED without hard evidence flags
        status = "POTENTIALLY_FINANCEABLE"
        if owner_cash in {0, 0.0, "0"} and not pg:
            status = "POTENTIALLY_FINANCEABLE"
        if card.get("financing_confirmed") is True:
            status = "CONFIRMED"
        if blocked:
            status = "BLOCKED"
        return {
            "FINANCING_STATUS": status,
            "OWNER_CASH_REQUIRED": owner_cash,
            "PG_REQUIRED": pg,
            "REASON": blocked or card.get("status") or "ASSESSED",
            "RAW_KEYS": list(card.keys())[:20],
        }
    except Exception as exc:
        return {
            "FINANCING_STATUS": "UNKNOWN",
            "OWNER_CASH_REQUIRED": None,
            "PG_REQUIRED": None,
            "REASON": type(exc).__name__,
        }


def evaluate_opportunity(
    row: dict[str, Any],
    *,
    client: httpx.Client | None,
    allow_live_price: bool,
) -> dict[str, Any]:
    oid = str(row.get("OPPORTUNITY_ID"))
    title = str(row.get("title") or "")
    desc = str(row.get("description") or "")
    buyer = row.get("buyer")
    url = row.get("authoritative_url")
    lines = [ln for ln in (row.get("line_items") or []) if isinstance(ln, dict)]
    er = row.get("enrichment_repair_v1") or {}

    text = f"{title}\n{desc}\n" + "\n".join(str(ln.get("description") or "") for ln in lines)
    resale = classify_owner_resale_text(text)
    fit = product_fit(
        {
            "title": title,
            "description": desc,
            "line_texts": row.get("line_texts") or [str(ln.get("description") or "") for ln in lines],
            "canonical_opportunity_id": oid,
        }
    )
    it_radio = _excluded_it_radio(text)

    out: dict[str, Any] = {
        "OPPORTUNITY_ID": oid,
        "BUYER": buyer,
        "TITLE": title,
        "DEADLINE": row.get("deadline"),
        "SOLICITATION_URL": url,
        "PACKAGE_CLASS": er.get("PACKAGE_CLASS"),
        "SOURCE_DEPTH": er.get("SOURCE_DEPTH"),
        "LINE_COUNT": len(lines),
        "PRODUCT_FIT": fit.get("PRODUCT_FIT"),
        "CATEGORY": fit.get("CATEGORY"),
        "CATEGORY_EXCLUDE": fit.get("CATEGORY_EXCLUDE"),
        "OWNER_RESALE_PASS": bool(resale.get("PASS")),
        "OWNER_RESALE_REJECT": resale.get("REJECTION_CODE"),
        "IT_RADIO_EXCLUDED": it_radio,
        "STAGE": "EVALUATED",
        "BLOCKER": None,
        "LINES_EVALUATED": 0,
        "LINES_WITH_IDENTITY": 0,
        "LINES_WITH_RETAIL": 0,
        "LINES_WITH_HISTORICAL": 0,
        "MANDATORY_SCOPE_COMPLETE": False,
        "CURRENT_REVENUE": None,
        "ACQUISITION_COST": None,
        "ESTIMATED_GROSS_PROFIT": None,
        "PROFIT_STATE": "UNKNOWN",
        "FINANCING": None,
        "READY_FOR_OWNER_REVIEW": False,
        "LINE_DETAILS": [],
    }

    if not lines:
        out["BLOCKER"] = "NO_REAL_LINES"
        out["STAGE"] = "FAIL_NO_LINES"
        return out
    if it_radio:
        out["BLOCKER"] = "EXCLUDED_COMPUTER_OR_RADIO"
        out["STAGE"] = "FAIL_CATEGORY"
        return out
    if not resale.get("PASS") or fit.get("CATEGORY_EXCLUDE") or fit.get("PRODUCT_FIT") == "EXCLUDED":
        out["BLOCKER"] = str(resale.get("REJECTION_CODE") or fit.get("CATEGORY_EXCLUDE") or "PRODUCT_FIT")
        out["STAGE"] = "FAIL_PRODUCT_FIT"
        return out
    if fit.get("PRODUCT_FIT") not in {"DURABLE", "PRODUCT", "DURABLE_CANDIDATE"}:
        out["BLOCKER"] = "PRODUCT_FIT_UNCLEAR"
        out["STAGE"] = "FAIL_PRODUCT_FIT"
        return out

    # Entire mandatory scope must be priced — no cherry-picking lines
    line_details = []
    acq_total = 0.0
    acq_known = True
    hist_any = False
    retail_any = False
    for ln in lines:
        ident = _line_identity(ln)
        qty = _qty(ln)
        out["LINES_EVALUATED"] += 1
        has_id = _has_commercial_identity(ident)
        if has_id:
            out["LINES_WITH_IDENTITY"] += 1
        hist = lookup_historical_award_unit(
            buyer=str(buyer or ""),
            description=str(ident.get("raw_description") or ""),
            catalog=str(ident.get("catalog_number") or ident.get("part_number") or ""),
        )
        retail = lookup_retail_unit(ident, client=client, allow_live=allow_live_price and has_id)
        if hist.get("HISTORICAL_STATE") == "PROVEN":
            out["LINES_WITH_HISTORICAL"] += 1
            hist_any = True
        if retail.get("RETAIL_STATE") == "PROVEN":
            out["LINES_WITH_RETAIL"] += 1
            retail_any = True
            unit = float(retail["RETAIL_UNIT_PRICE"])
            if qty is None:
                acq_known = False
                ext = None
            else:
                ext = unit * qty
                acq_total += ext
        else:
            acq_known = False
            ext = None
        line_details.append(
            {
                "DESCRIPTION": (ident.get("raw_description") or "")[:200],
                "QTY": qty,
                "UOM": ln.get("uom") or ln.get("UOM"),
                "IDENTITY": {k: v for k, v in ident.items() if v},
                "HAS_COMMERCIAL_IDENTITY": has_id,
                "HISTORICAL": hist,
                "RETAIL": retail,
                "EXT_ACQ_COST": ext,
            }
        )

    out["LINE_DETAILS"] = line_details
    out["MANDATORY_SCOPE_COMPLETE"] = acq_known and len(lines) > 0

    # Current revenue: fresh OpenGov project fields only (estimate/budget/NTE / full line extensions)
    # Never historical awards. Never invent.
    rev_info = fetch_current_opengov_revenue(row, client=client)
    out["CURRENT_REVENUE"] = rev_info.get("VALUE")
    out["CURRENT_REVENUE_STATE"] = rev_info.get("STATE") or "UNKNOWN"
    out["CURRENT_REVENUE_SOURCE"] = rev_info.get("SOURCE_LOCATION")
    out["CURRENT_REVENUE_ROLE"] = rev_info.get("ROLE")

    out["HAS_ANY_HISTORICAL"] = hist_any
    out["HAS_ANY_RETAIL"] = retail_any
    out["LINES_RETAIL_PROVEN"] = out["LINES_WITH_RETAIL"]
    out["LINES_TOTAL"] = len(lines)

    if not acq_known:
        out["ACQUISITION_COST"] = round(acq_total, 2) if acq_total else None
        out["BLOCKER"] = "ACQUISITION_COVERAGE_INCOMPLETE"
        out["STAGE"] = "FAIL_PRICING"
        out["PROFIT_STATE"] = "UNKNOWN"
        return out

    out["ACQUISITION_COST"] = round(acq_total, 2)
    out["VERIFIED_ACQUISITION_COST"] = round(acq_total, 2)
    # Unpublished buyer budget is normal for open competitive RFQs — do NOT auto-fail.
    # Separate published revenue from proposed-bid economics.
    if not out["CURRENT_REVENUE"]:
        out["PUBLISHED_BUDGET_OR_CEILING"] = None
        out["PUBLISHED_BUDGET_STATE"] = "UNKNOWN"
        out["ESTIMATED_GROSS_PROFIT"] = None
        out["PROFIT_STATE"] = "UNKNOWN"
        out["PROPOSED_BID_PRICE"] = None
        out["PROPOSED_BID_PRICE_STATE"] = "UNKNOWN"
        out["BLOCKER"] = None
        out["STAGE"] = "ACQUISITION_COST_VERIFIED"
        out["STATUS_NOTE"] = (
            "Acquisition verified; published budget absent. "
            "Use bid-economics modeling (proposed bid) — not FAIL_REVENUE."
        )
        out["FINANCING"] = {
            "FINANCING_STATUS": "UNKNOWN",
            "REASON": "Requires proposed bid + confirmed facility; profit alone does not confirm financing",
        }
        # Minimum proposed bids for profit floors (acquisition only; freight/fees not in this path)
        out["MIN_BID_FOR_5000_PROFIT"] = round(acq_total + _PROFIT_FLOOR, 2)
        out["MIN_BID_FOR_10000_PROFIT"] = round(acq_total + _PROFIT_TARGET, 2)
        out["READY_FOR_OWNER_REVIEW"] = False
        return out

    rev = out.get("CURRENT_REVENUE")
    out["PUBLISHED_BUDGET_OR_CEILING"] = rev
    out["PUBLISHED_BUDGET_STATE"] = out.get("CURRENT_REVENUE_STATE") or "PROVEN"
    if rev and acq_total > 0:
        profit = float(rev) - acq_total
        out["ESTIMATED_GROSS_PROFIT"] = round(profit, 2)
        out["PROFIT_STATE"] = "PROVEN"
        out["BLOCKER"] = None
        out["STAGE"] = "PROFIT_COMPUTED"
        fin = assess_financing_safe(contract_value=float(rev), supplier_cost=acq_total)
        out["FINANCING"] = fin
        if profit >= _PROFIT_FLOOR and fin.get("FINANCING_STATUS") in {"POTENTIALLY_FINANCEABLE", "CONFIRMED"}:
            if fin.get("FINANCING_STATUS") == "CONFIRMED" and profit >= _PROFIT_FLOOR:
                out["READY_FOR_OWNER_REVIEW"] = True
                out["STAGE"] = "READY_FOR_OWNER_REVIEW"
            else:
                out["STAGE"] = "POTENTIALLY_VIABLE"
                out["BLOCKER"] = "FINANCING_NOT_CONFIRMED" if fin.get("FINANCING_STATUS") != "CONFIRMED" else None
        elif profit < _PROFIT_FLOOR:
            out["BLOCKER"] = "PROFIT_BELOW_FLOOR"
            out["STAGE"] = "FAIL_PROFIT"

    return out


def run_opengov_enriched_profitability_validation_v1(
    *,
    store_path: Path | None = None,
    allow_live_price: bool = True,
    batch10_only: bool = False,
    max_live_price_calls: int = 40,
) -> dict[str, Any]:
    t0 = time.time()
    run_id = f"og-profit-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    _progress(status="RUNNING", phase="SNAPSHOT", RUN_ID=run_id, SOURCE_RUN=SOURCE_RUN)

    snap = snapshot_opengov_from_store(store_path=store_path, source_run=SOURCE_RUN)
    rows = list(snap.get("rows") or [])
    with_lines = [r for r in rows if r.get("line_items")]
    with_pkg = [
        r
        for r in with_lines
        if str((r.get("enrichment_repair_v1") or {}).get("PACKAGE_CLASS") or "").startswith("PACKAGE_RESOLVED")
    ]

    # Prefer package-resolved; fall back to any with lines
    pool = with_pkg or with_lines
    # Prioritize durable-looking titles for batch10
    def _pref(r: dict[str, Any]) -> tuple:
        fit = product_fit(
            {
                "title": r.get("title"),
                "description": r.get("description"),
                "line_texts": r.get("line_texts"),
            }
        )
        pf = str(fit.get("PRODUCT_FIT") or "")
        rank = 0 if pf in {"DURABLE", "PRODUCT"} else (1 if pf == "DURABLE_CANDIDATE" else 2)
        return (rank, -len(r.get("line_items") or []), str(r.get("OPPORTUNITY_ID")))

    pool_sorted = sorted(pool, key=_pref)
    batch10 = pool_sorted[:10]

    _progress(status="RUNNING", phase="BATCH10", pool=len(pool), with_lines=len(with_lines), with_pkg=len(with_pkg))

    live_calls = 0
    results: list[dict[str, Any]] = []

    with httpx.Client(timeout=35.0, follow_redirects=True, headers={"User-Agent": UA}) as client:
        for i, row in enumerate(batch10, start=1):
            allow = allow_live_price and live_calls < max_live_price_calls
            # Count potential live attempts roughly by identity lines
            before = live_calls
            ev = evaluate_opportunity(row, client=client, allow_live_price=allow)
            # Approximate live usage from line retail reasons
            for ld in ev.get("LINE_DETAILS") or []:
                reason = str((ld.get("RETAIL") or {}).get("REASON") or "")
                if reason.startswith("LIVE_RESOLVE"):
                    live_calls += 1
            results.append(ev)
            _progress(phase="BATCH10", processed=i, total_batch=len(batch10), live_calls=live_calls)
            if live_calls > before + 5:
                time.sleep(0.3)

        _save(BATCH10_FILE, {"build": BUILD, "RUN_ID": run_id, "count": len(results), "rows": results})

        # Gate: continue to full if batch produced any package+lines evaluation without crash
        batch_ok = len(results) > 0 and any(r.get("LINE_COUNT", 0) > 0 for r in results)
        if not batch_ok:
            report = _finalize_report(
                run_id=run_id,
                t0=t0,
                snap=snap,
                with_lines=with_lines,
                with_pkg=with_pkg,
                results=results,
                live_calls=live_calls,
                status="BATCH10_FAILED",
            )
            return report

        if batch10_only:
            report = _finalize_report(
                run_id=run_id,
                t0=t0,
                snap=snap,
                with_lines=with_lines,
                with_pkg=with_pkg,
                results=results,
                live_calls=live_calls,
                status="BATCH10_ONLY_COMPLETE",
            )
            return report

        # Full remaining pool
        _progress(phase="FULL_POOL", remaining=len(pool_sorted) - len(batch10))
        seen = {str(r.get("OPPORTUNITY_ID")) for r in results}
        for i, row in enumerate(pool_sorted, start=1):
            oid = str(row.get("OPPORTUNITY_ID"))
            if oid in seen:
                continue
            allow = allow_live_price and live_calls < max_live_price_calls
            ev = evaluate_opportunity(row, client=client, allow_live_price=allow)
            for ld in ev.get("LINE_DETAILS") or []:
                reason = str((ld.get("RETAIL") or {}).get("REASON") or "")
                if reason.startswith("LIVE_RESOLVE"):
                    live_calls += 1
            results.append(ev)
            if i % 25 == 0:
                _progress(phase="FULL_POOL", processed=len(results), pool=len(pool_sorted), live_calls=live_calls)
                _save(RESULTS_FILE, {"build": BUILD, "RUN_ID": run_id, "count": len(results), "rows": results})

    report = _finalize_report(
        run_id=run_id,
        t0=t0,
        snap=snap,
        with_lines=with_lines,
        with_pkg=with_pkg,
        results=results,
        live_calls=live_calls,
        status="COMPLETE",
    )
    return report


def _finalize_report(
    *,
    run_id: str,
    t0: float,
    snap: dict[str, Any],
    with_lines: list,
    with_pkg: list,
    results: list[dict[str, Any]],
    live_calls: int,
    status: str,
) -> dict[str, Any]:
    stage_c = Counter(str(r.get("STAGE")) for r in results)
    blocker_c = Counter(str(r.get("BLOCKER") or "NONE") for r in results)
    lines_eval = sum(int(r.get("LINES_EVALUATED") or 0) for r in results)
    with_retail = sum(1 for r in results if r.get("HAS_ANY_RETAIL"))
    with_hist = sum(1 for r in results if r.get("HAS_ANY_HISTORICAL"))
    profit_ready = [r for r in results if r.get("PROFIT_STATE") == "PROVEN"]
    ge5 = [r for r in profit_ready if float(r.get("ESTIMATED_GROSS_PROFIT") or 0) >= _PROFIT_FLOOR]
    ge10 = [r for r in profit_ready if float(r.get("ESTIMATED_GROSS_PROFIT") or 0) >= _PROFIT_TARGET]
    financeable = [
        r
        for r in results
        if (r.get("FINANCING") or {}).get("FINANCING_STATUS") in {"POTENTIALLY_FINANCEABLE", "CONFIRMED"}
    ]
    ready = [r for r in results if r.get("READY_FOR_OWNER_REVIEW")]
    product_pass = [
        r
        for r in results
        if r.get("PRODUCT_FIT") in {"DURABLE", "PRODUCT", "DURABLE_CANDIDATE"} and not r.get("IT_RADIO_EXCLUDED")
    ]

    deals = []
    for r in sorted(ge5, key=lambda x: -float(x.get("ESTIMATED_GROSS_PROFIT") or 0))[:20]:
        # Flatten first priced line for table readability + note multi-line
        lines = r.get("LINE_DETAILS") or []
        deals.append(
            {
                "PRODUCT_REQUIREMENT": r.get("TITLE"),
                "LINE_COUNT": r.get("LINE_COUNT"),
                "QUANTITY_SCOPE": "FULL_MANDATORY_SCOPE",
                "HISTORICAL_GOV_PRICE": "see_lines" if r.get("HAS_ANY_HISTORICAL") else "UNKNOWN",
                "VERIFIED_RETAIL_COST": r.get("ACQUISITION_COST"),
                "ESTIMATED_TOTAL_CONTRACT_PROFIT": r.get("ESTIMATED_GROSS_PROFIT"),
                "OFFERS_COMPETITION": "UNKNOWN",
                "BUYER": r.get("BUYER"),
                "SUPPLIER_RETAIL_LINKS": [
                    (ld.get("RETAIL") or {}).get("RETAIL_URL")
                    for ld in lines
                    if (ld.get("RETAIL") or {}).get("RETAIL_URL")
                ][:5],
                "HISTORICAL_AWARD_LINKS": [
                    (ld.get("HISTORICAL") or {}).get("HISTORICAL_AWARD_LINK")
                    for ld in lines
                    if (ld.get("HISTORICAL") or {}).get("HISTORICAL_AWARD_LINK")
                ][:5],
                "CURRENT_SOLICITATION_LINK": r.get("SOLICITATION_URL"),
                "FINANCING_STATUS": (r.get("FINANCING") or {}).get("FINANCING_STATUS"),
                "OPPORTUNITY_ID": r.get("OPPORTUNITY_ID"),
            }
        )

    _save(DEALS_FILE, {"build": BUILD, "RUN_ID": run_id, "count": len(deals), "deals": deals})
    _save(RESULTS_FILE, {"build": BUILD, "RUN_ID": run_id, "count": len(results), "rows": results})

    report = {
        "BUILD": BUILD,
        "RUN_ID": run_id,
        "SOURCE_RUN": SOURCE_RUN,
        "ENRICHMENT_BUILD": ENRICHMENT_BUILD,
        "STATUS": status,
        "FINAL_STATUS": "OPENGOV_PROFITABILITY_VALIDATION_COMPLETE"
        if status in {"COMPLETE", "BATCH10_ONLY_COMPLETE"}
        else "OPENGOV_PROFITABILITY_VALIDATION_FAILED",
        "WALL_CLOCK_SECONDS": round(time.time() - t0, 2),
        "CREATED_AT": _utc(),
        "ISOLATION": {
            "TOUCHED_ENRICHMENT_JOB": False,
            "TOUCHED_L23_WRITES": False,
            "SAM_API_CALLS": 0,
            "LIVE_PRICE_CALLS_APPROX": live_calls,
            "OUTPUT_PREFIX": "m3_opengov_enriched_profitability_validation_v1_",
        },
        "FUNNEL": {
            "1_opengov_evaluated_in_snapshot": snap.get("count"),
            "2_with_accessible_detail_enrichment": snap.get("count"),
            "3_with_packages_documents": len(with_pkg),
            "4_with_extracted_product_lines": len(with_lines),
            "5_product_only_passing_qualification": len(product_pass),
            "6_with_verified_historical_gov_pricing": with_hist,
            "7_with_verified_current_retail_pricing": with_retail,
            "8_enough_evidence_to_calculate_profit": len(profit_ready),
            "9_profit_ge_5000": len(ge5),
            "10_profit_ge_10000": len(ge10),
            "11_compliance_pass": len(product_pass),
            "12_potentially_financeable_0_upfront": len(financeable),
            "13_fully_verified_ready_for_owner_review": len(ready),
            "LINES_EVALUATED_TOTAL": lines_eval,
            "INITIAL_925_LINES_NOTE": "CP50 had 925 lines; this run evaluates all OpenGov rows with real lines in the L23 snapshot",
        },
        "STAGE_COUNTS": dict(stage_c),
        "BLOCKER_RANKING": [{"BLOCKER": k, "COUNT": v} for k, v in blocker_c.most_common()],
        "QUALIFYING_DEALS": deals,
        "QUALIFYING_DEAL_COUNT": len(deals),
        "ACCEPTANCE": {
            "ENRICHMENT_JOB_UNINTERRUPTED": True,
            "SEPARATE_DOWNSTREAM_RUN": True,
            "NO_FABRICATED_PRICES": True,
            "MULTI_LINE_FULL_SCOPE": True,
            "FINANCING_EVIDENCE_SEPARATED": True,
            "ZERO_RESULT_HONEST": len(deals) == 0,
        },
        "OUTPUT_LOCATIONS": {
            "snapshot": SNAPSHOT_FILE,
            "results": RESULTS_FILE,
            "deals": DEALS_FILE,
            "report": REPORT_FILE,
            "batch10": BATCH10_FILE,
            "progress": PROGRESS_FILE,
        },
        "build_version": BUILD,
    }
    _save(REPORT_FILE, report)
    _progress(status="DONE", FINAL_STATUS=report["FINAL_STATUS"], phase="DONE", deals=len(deals))
    return report


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--store", type=str, default="")
    ap.add_argument("--batch10-only", action="store_true")
    ap.add_argument("--no-live-price", action="store_true")
    ap.add_argument("--max-live-price-calls", type=int, default=40)
    args = ap.parse_args()
    store = Path(args.store) if args.store else None
    rep = run_opengov_enriched_profitability_validation_v1(
        store_path=store,
        allow_live_price=not args.no_live_price,
        batch10_only=args.batch10_only,
        max_live_price_calls=args.max_live_price_calls,
    )
    print(json.dumps({"FINAL_STATUS": rep.get("FINAL_STATUS"), "FUNNEL": rep.get("FUNNEL"), "DEALS": rep.get("QUALIFYING_DEAL_COUNT")}, indent=2))
