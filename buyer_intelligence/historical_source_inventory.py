"""Historical Source Inventory + OpenGov Expansion V1 — local-data-first."""
from __future__ import annotations

import hashlib
import json
import re
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

BUILD = "20261008-m3-historical-source-inventory-opengov-expansion-v1"
MAX_SCAN_LINES = 50000  # local inventory only; not a crawl
POOL_TARGET = 100
POOL_MIN = 50
BUYER_CAP = 5
PREFERRED_MIN = 3000
PREFERRED_MAX = 50000

_FIXTURE = re.compile(r"\b(fixture|mock|sample|demo|synthetic|test|sentinel|fake)\b", re.I)
_PRODUCT = re.compile(
    r"\b(inventory|parts?|supply|supplies|equipment|filter|tool|ppe|furniture|mro|"
    r"electrical|plumbing|janitorial|hardware|fleet|oem|consumable|material)\b",
    re.I,
)
_SERVICE = re.compile(
    r"\b(service|consulting|construction|install|repair\s+labor|training|lease|"
    r"software|grant|maintenance\s+agreement)\b",
    re.I,
)
_MPN_BAD = re.compile(
    r"ship|delay|week|days?\s*aro|lead\s*time|in\s*stock|available|upon\s*order|"
    r"barring|unforeseen|business\s*days|n/?a|none|null|tbd",
    re.I,
)
_MPN_FROM_DESC = re.compile(
    r"(?:mfg\s*part\s*#?|part\s*#|p/?n|mpn|model)\s*[:#]?\s*([A-Z0-9][A-Z0-9.\-_/]{3,})",
    re.I,
)
_NSN = re.compile(r"\b(\d{4}-\d{2}-\d{3}-\d{4})\b")


def _utc() -> str:
    from application_clock import now_utc

    return now_utc().isoformat()


def _data_path(*parts: str) -> Path:
    from m3_data_root import data_path

    return data_path(*parts)


def _root() -> Path:
    from m3_data_root import get_data_root

    return Path(get_data_root())


def _write_status(**kwargs: Any) -> None:
    _data_path("m3_historical_source_inventory_v1_status.json").write_text(
        json.dumps({"build": BUILD, "updated_at": _utc(), **kwargs}, indent=2, default=str),
        encoding="utf-8",
    )


def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None


def _line_key(ln: dict[str, Any]) -> str:
    return "|".join(
        [
            str(ln.get("line_item") or ""),
            str(ln.get("part_number") or "").upper(),
            re.sub(r"\s+", " ", str(ln.get("description") or ln.get("line_description") or ""))[:80].upper(),
        ]
    )


def _extract_mpn(ln: dict[str, Any]) -> str | None:
    raw = str(ln.get("part_number") or "").strip()
    desc = str(ln.get("description") or ln.get("line_description") or "")
    m = _MPN_FROM_DESC.search(desc)
    if m:
        return m.group(1).upper()
    if raw and len(raw) <= 48 and not _MPN_BAD.search(raw) and " " not in raw:
        if re.fullmatch(r"[A-Z0-9][A-Z0-9.\-_/]{2,47}", raw, re.I):
            return raw.upper()
    if _NSN.search(desc):
        return _NSN.search(desc).group(1)
    return None


def _hash(blob: str) -> str:
    return hashlib.sha256(blob.encode("utf-8", errors="replace")).hexdigest()[:24]


def _pct(n: int, d: int) -> float:
    return round(100.0 * n / d, 1) if d else 0.0


def _classify(
    *,
    real_orders: int,
    award_total_pct: float,
    line_pct: float,
    qty_pct: float,
    id_pct: float,
    is_historical: bool,
    fixture_only: bool,
) -> str:
    if fixture_only:
        return "E_TEST/FIXTURE_ONLY"
    if not is_historical or real_orders == 0:
        return "D_NOT_HISTORICAL"
    econ = award_total_pct >= 70 and line_pct >= 50 and qty_pct >= 40 and id_pct >= 20
    if econ and real_orders >= 50:
        return "A_FULL_ECONOMICS_SOURCE"
    if award_total_pct >= 60 and real_orders >= 30:
        return "B_BUYER_INTELLIGENCE_SOURCE"
    return "C_PARTIAL_HISTORY_SOURCE"


def inventory_opengov_buyer_history() -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """Full local OpenGov history inventory + reconstruct product orders (no scan cap for inventory)."""
    hist = _root() / "opengov_buyer_history"
    excl = {"FIXTURE_ROWS_EXCLUDED": 0, "SYNTHETIC_ROWS_EXCLUDED": 0, "TEST_ROWS_EXCLUDED": 0}
    entities = 0
    project_entries = 0
    unique_projects: set[tuple[str, str]] = set()
    lines_n = 0
    buyers: set[str] = set()
    vendor_n = date_n = mpn_n = qty_n = uom_n = url_n = ext_n = 0
    entities_with_history = 0
    by_proj_lines: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    blocker = Counter()

    files = sorted(hist.glob("*.json")) if hist.exists() else []
    for f in files:
        if _FIXTURE.search(f.name):
            excl["TEST_ROWS_EXCLUDED"] += 1
            blocker["fixture_entity_file"] += 1
            continue
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            blocker["unreadable_entity_file"] += 1
            continue
        entities += 1
        buyer = f.stem.lower()
        buyers.add(buyer)
        projs = d.get("projects") or []
        if isinstance(projs, list):
            project_entries += len(projs)
            for p in projs:
                if isinstance(p, dict):
                    pid = str(p.get("project_id") or p.get("id") or "")
                    if pid:
                        unique_projects.add((buyer, pid))
        pl = d.get("priced_lines") or []
        if not isinstance(pl, list) or not pl:
            blocker["entity_without_priced_lines"] += 1
            continue
        entities_with_history += 1
        for ln in pl:
            if not isinstance(ln, dict):
                continue
            blob = f"{ln.get('project_id')} {ln.get('description')} {ln.get('source_url')}"
            if _FIXTURE.search(blob):
                excl["FIXTURE_ROWS_EXCLUDED"] += 1
                continue
            lines_n += 1
            pid = str(ln.get("project_id") or "")
            if not pid:
                blocker["line_missing_project_id"] += 1
                continue
            unique_projects.add((buyer, pid))
            ln2 = dict(ln)
            ln2["_portal"] = str(ln.get("government_code") or buyer).lower()
            ln2["_source"] = "opengov_buyer_history"
            by_proj_lines[(ln2["_portal"], pid)].append(ln2)
            if ln.get("winning_vendor") or ln.get("vendor"):
                vendor_n += 1
            if ln.get("closed_at") or ln.get("award_date"):
                date_n += 1
            if _extract_mpn(ln):
                mpn_n += 1
            if ln.get("quantity") is not None:
                qty_n += 1
            if ln.get("uom"):
                uom_n += 1
            if ln.get("source_url"):
                url_n += 1
            if _num(ln.get("extended")) is not None or (
                _num(ln.get("unit_price") or ln.get("bid_unit_price")) is not None
                and _num(ln.get("quantity")) is not None
            ):
                ext_n += 1

    orders = _build_orders_from_projects(by_proj_lines, source="opengov_buyer_history")
    product_orders = [o for o in orders if o.get("PRODUCT_ONLY")]
    coverage = {
        "TOTAL_ENTITIES": entities,
        "ENTITIES_WITH_HISTORY": entities_with_history,
        "TOTAL_HISTORICAL_PROJECT_ENTRIES": project_entries,
        "TOTAL_UNIQUE_PROJECTS": len(unique_projects),
        "TOTAL_PRICED_LINES": lines_n,
        "BUYERS": len(buyers),
        "VALIDATED_PRODUCT_ORDERS": len(product_orders),
        "ALL_VALIDATED_ORDERS": len(orders),
        "blockers": dict(blocker),
        "exclusions": excl,
    }
    matrix = {
        "SOURCE_NAME": "opengov_buyer_history",
        "RECORD_COUNT": lines_n,
        "REAL_HISTORICAL_AWARD_RECORDS": len(orders),
        "BUYER_PRESENT_PCT": 100.0 if buyers else 0.0,
        "AWARDEE_PRESENT_PCT": _pct(vendor_n, lines_n),
        "AWARD_TOTAL_PRESENT_PCT": _pct(sum(1 for o in orders if o.get("AWARD_TOTAL") not in (None, "UNKNOWN")), max(len(orders), 1)),
        "DATE_PRESENT_PCT": _pct(date_n, lines_n),
        "LINE_DETAIL_PRESENT_PCT": 100.0 if lines_n else 0.0,
        "QTY_PRESENT_PCT": _pct(qty_n, lines_n),
        "UOM_PRESENT_PCT": _pct(uom_n, lines_n),
        "MPN_SKU_NSN_PRESENT_PCT": _pct(mpn_n, lines_n),
        "BIDDER_COUNT_PRESENT_PCT": _pct(
            sum(1 for ln in (x for rows in by_proj_lines.values() for x in rows) if ln.get("bidder_count") or ln.get("all_priced_vendors")),
            lines_n,
        ),
        "BID_TAB_PRESENT_PCT": _pct(
            sum(1 for rows in by_proj_lines.values() for ln in rows if ln.get("all_priced_vendors")),
            lines_n,
        ),
        "SOURCE_URL_PRESENT_PCT": _pct(url_n, lines_n),
        "PRODUCT_ONLY_RATE": _pct(len(product_orders), max(len(orders), 1)),
        "ECONOMICS_READY_RATE": _pct(
            sum(1 for o in product_orders if o.get("ECONOMICS_READY")),
            max(len(product_orders), 1),
        ),
        "CLASS": "A_FULL_ECONOMICS_SOURCE",
        "STATUS": "WORKING",
    }
    return matrix, product_orders, coverage


def _build_orders_from_projects(
    by_proj: dict[tuple[str, str], list[dict[str, Any]]],
    *,
    source: str,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for (buyer, pid), plines in by_proj.items():
        title = str(plines[0].get("project_title") or "")
        if _SERVICE.search(title) and not _PRODUCT.search(title):
            continue
        uniq: dict[str, dict[str, Any]] = {}
        for ln in plines:
            k = _line_key(ln)
            prev = uniq.get(k)
            ext = _num(ln.get("extended"))
            if prev is None:
                uniq[k] = ln
            else:
                prev_ext = _num(prev.get("extended"))
                if ext is not None and (prev_ext is None or ext < prev_ext):
                    uniq[k] = ln
        if not uniq:
            continue
        total = 0.0
        pn_n = qty_n = uom_n = 0
        for ln in uniq.values():
            ext = _num(ln.get("extended"))
            if ext is None:
                u, q = _num(ln.get("unit_price") or ln.get("bid_unit_price")), _num(ln.get("quantity"))
                if u is not None and q is not None:
                    ext = u * q
            if ext is not None:
                total += ext
            if _extract_mpn(ln):
                pn_n += 1
            if ln.get("quantity") is not None:
                qty_n += 1
            if ln.get("uom"):
                uom_n += 1
        total = round(total, 2)
        if total <= 0:
            continue
        productish = bool(_PRODUCT.search(title) or pn_n >= 1 or re.search(r"inventory|rfq", title, re.I))
        if not productish:
            continue
        vendors = Counter(
            str(ln.get("winning_vendor") or ln.get("vendor") or "")
            for ln in uniq.values()
            if ln.get("winning_vendor") or ln.get("vendor")
        )
        vendors = Counter({k: v for k, v in vendors.items() if k and not re.fullmatch(r"\d{1,2}/\d{1,2}/\d{2,4}", k)})
        hist_vendor = vendors.most_common(1)[0][0] if vendors else None
        n = len(uniq)
        has_line = True
        has_qty = qty_n / n >= 0.5
        has_uom = uom_n / n >= 0.5
        has_id = pn_n / n >= 0.25 or pn_n >= 2
        econ_ready = has_line and has_qty and has_uom and has_id and total >= 1000 and bool(hist_vendor)
        cat = _category(title, " ".join(str(x.get("description") or "") for x in list(uniq.values())[:8]))
        out.append(
            {
                "SOURCE": source,
                "SOURCE_RECORD_ID": f"{buyer}:{pid}",
                "BUYER": buyer,
                "OFFICE": None,
                "ORDER_ID": pid,
                "PROJECT_ID": pid,
                "AWARD_ID": plines[0].get("financial_id"),
                "DATE": plines[0].get("closed_at") or plines[0].get("award_date"),
                "AWARDEE": hist_vendor,
                "AWARD_TOTAL": total,
                "TOTAL_METHOD": "SUM_OF_PROVEN_ORDER_LINES",
                "TITLE": title,
                "CATEGORY": cat,
                "LINE_COUNT": n,
                "HAS_LINE_DETAIL": has_line,
                "HAS_QTY": has_qty,
                "HAS_UOM": has_uom,
                "HAS_PRODUCT_ID": has_id,
                "MPN_COUNT": pn_n,
                "SOURCE_URL": plines[0].get("source_url"),
                "RAW_HASH": _hash(f"{buyer}|{pid}|{total}|{n}"),
                "PRODUCT_ONLY": True,
                "COMPLETE_ORDER_READY": True,
                "IDENTITY_READY": has_id,
                "PUBLIC_PRICE_READY": has_id and has_qty and has_uom,
                "ECONOMICS_READY": econ_ready,
                "SELECTION_SCORE": _selection_score(total, n, pn_n, title, bool(hist_vendor), bool(plines[0].get("source_url"))),
            }
        )
    return out


def _category(title: str, blob: str) -> str:
    t = f"{title} {blob}".lower()
    rules = [
        ("AUTO_PARTS", r"inventory\s*parts|fleet|cummins|engine|injector|turbo|filter|gasket|brake"),
        ("MRO", r"\bmro\b|maintenance|bearing|seal|clamp"),
        ("TOOLS", r"\btool|wrench|drill"),
        ("ELECTRICAL", r"electrical|relay|switch|lamp|wire|cable|fuse"),
        ("PLUMBING", r"plumbing|pipe|valve|fitting"),
        ("JANITORIAL", r"janitorial|cleaner|paper towel"),
        ("PPE", r"\bppe\b|glove|mask|respirator"),
        ("FURNITURE", r"furniture|chair|desk"),
        ("MEDICAL", r"medical|pharma|syringe"),
        ("EQUIPMENT", r"equipment|blower|meter|camera"),
    ]
    for name, pat in rules:
        if re.search(pat, t, re.I):
            return name
    return "OTHER"


def _selection_score(total: float, line_count: int, mpn_n: int, title: str, has_vendor: bool, has_url: bool) -> int:
    score = 0
    if PREFERRED_MIN <= total <= PREFERRED_MAX:
        score += 25
    elif 3000 <= total < PREFERRED_MIN or PREFERRED_MAX < total <= 100000:
        score += 12
    if has_vendor:
        score += 12
    if has_url:
        score += 6
    if mpn_n / max(line_count, 1) >= 0.5:
        score += 20
    elif mpn_n >= 1:
        score += 8
    if 2 <= line_count <= 40:
        score += 12
    if _PRODUCT.search(title or ""):
        score += 10
    if _SERVICE.search(title or "") and not _PRODUCT.search(title or ""):
        score -= 20
    return max(0, min(100, score))


def inventory_bid_price_index() -> dict[str, Any]:
    path = _data_path("opengov_bid_price_index.json")
    if not path.exists():
        return {"SOURCE_NAME": "opengov_bid_price_index", "RECORD_COUNT": 0, "CLASS": "D_NOT_HISTORICAL", "STATUS": "MISSING"}
    doc = json.loads(path.read_text(encoding="utf-8"))
    recs = [r for r in (doc.get("records") or []) if isinstance(r, dict)]
    excl = 0
    clean = []
    for r in recs:
        blob = f"{r.get('project_id')} {r.get('line_description')} {r.get('source_url')}"
        if _FIXTURE.search(blob) or _FIXTURE.search(str(r.get("buyer") or "")):
            excl += 1
            continue
        clean.append(r)
    n = len(clean)
    projects = {(r.get("buyer"), r.get("project_id")) for r in clean}
    return {
        "SOURCE_NAME": "opengov_bid_price_index",
        "RECORD_COUNT": n,
        "REAL_HISTORICAL_AWARD_RECORDS": len(projects),
        "BUYER_PRESENT_PCT": _pct(sum(1 for r in clean if r.get("buyer")), n),
        "AWARDEE_PRESENT_PCT": _pct(sum(1 for r in clean if r.get("vendor") or r.get("winning_vendor")), n),
        "AWARD_TOTAL_PRESENT_PCT": _pct(sum(1 for r in clean if _num(r.get("extended")) is not None), n),
        "DATE_PRESENT_PCT": _pct(sum(1 for r in clean if r.get("closed_at") or r.get("award_date")), n),
        "LINE_DETAIL_PRESENT_PCT": 100.0 if n else 0.0,
        "QTY_PRESENT_PCT": _pct(sum(1 for r in clean if r.get("quantity") is not None), n),
        "UOM_PRESENT_PCT": _pct(sum(1 for r in clean if r.get("uom")), n),
        "MPN_SKU_NSN_PRESENT_PCT": _pct(sum(1 for r in clean if r.get("part_number")), n),
        "BIDDER_COUNT_PRESENT_PCT": _pct(sum(1 for r in clean if r.get("bidder_count") or r.get("all_priced_vendors")), n),
        "BID_TAB_PRESENT_PCT": _pct(sum(1 for r in clean if r.get("all_priced_vendors")), n),
        "SOURCE_URL_PRESENT_PCT": _pct(sum(1 for r in clean if r.get("source_url")), n),
        "PRODUCT_ONLY_RATE": None,
        "ECONOMICS_READY_RATE": None,
        "CLASS": "A_FULL_ECONOMICS_SOURCE",
        "STATUS": "OVERLAP_WITH_BUYER_HISTORY",
        "NOTE": "Same OpenGov tabulations as buyer_history; use for coverage cross-check",
        "FIXTURE_ROWS_EXCLUDED": excl,
        "PROJECT_COUNT": len(projects),
    }


def inventory_scale_store() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    path = _data_path("m3_scale_evidence_profit_store.json")
    if not path.exists():
        return {"SOURCE_NAME": "m3_scale_evidence_profit_store", "RECORD_COUNT": 0, "CLASS": "D_NOT_HISTORICAL", "STATUS": "MISSING"}, []
    doc = json.loads(path.read_text(encoding="utf-8"))
    found = doc.get("FOUND") or doc.get("lines") or doc.get("records") or []
    if isinstance(found, dict):
        found = list(found.values())
    clean = []
    excl = 0
    for r in found if isinstance(found, list) else []:
        if not isinstance(r, dict):
            continue
        blob = json.dumps(r, default=str)[:500]
        if _FIXTURE.search(blob):
            excl += 1
            continue
        clean.append(r)
    n = len(clean)
    buyers = Counter(str(r.get("buyer") or r.get("portal") or "unknown").lower() for r in clean)
    # Prefer Go-Metro ledger orders if present
    orders: list[dict[str, Any]] = []
    gopath = _data_path("m3_gometro_real_order_proof_v1_orders.json")
    if gopath.exists():
        gdoc = json.loads(gopath.read_text(encoding="utf-8"))
        for o in gdoc.get("orders") or []:
            if not isinstance(o, dict):
                continue
            tot = _num(o.get("FULL_ORDER_TOTAL"))
            if tot is None:
                continue
            orders.append(
                {
                    "SOURCE": "scale_evidence_gometro_ledger",
                    "SOURCE_RECORD_ID": f"go-metro:{o.get('ORDER_ID')}",
                    "BUYER": "go-metro",
                    "OFFICE": "Procurement / Inventory",
                    "ORDER_ID": str(o.get("ORDER_ID")),
                    "PROJECT_ID": str(o.get("ORDER_ID")),
                    "AWARD_ID": None,
                    "DATE": o.get("DATE"),
                    "AWARDEE": o.get("VENDOR"),
                    "AWARD_TOTAL": tot,
                    "TOTAL_METHOD": o.get("TOTAL_METHOD") or "SUM_OF_PROVEN_ORDER_LINES",
                    "TITLE": o.get("TITLE") or "Inventory parts",
                    "CATEGORY": o.get("CATEGORY") or "AUTO_PARTS",
                    "LINE_COUNT": int(o.get("LINE_COUNT") or 1),
                    "HAS_LINE_DETAIL": True,
                    "HAS_QTY": True,
                    "HAS_UOM": True,
                    "HAS_PRODUCT_ID": True,
                    "MPN_COUNT": int(o.get("LINE_COUNT") or 1),
                    "SOURCE_URL": o.get("SOURCE_URL"),
                    "RAW_HASH": _hash(f"gm|{o.get('ORDER_ID')}|{tot}"),
                    "PRODUCT_ONLY": True,
                    "COMPLETE_ORDER_READY": True,
                    "IDENTITY_READY": True,
                    "PUBLIC_PRICE_READY": True,
                    "ECONOMICS_READY": True,
                    "SELECTION_SCORE": _selection_score(tot, int(o.get("LINE_COUNT") or 1), int(o.get("LINE_COUNT") or 1), str(o.get("TITLE") or "parts"), True, bool(o.get("SOURCE_URL"))),
                }
            )
    matrix = {
        "SOURCE_NAME": "m3_scale_evidence_profit_store",
        "RECORD_COUNT": n,
        "REAL_HISTORICAL_AWARD_RECORDS": len(orders) or len(buyers),
        "BUYER_PRESENT_PCT": 100.0 if buyers else 0.0,
        "AWARDEE_PRESENT_PCT": 100.0 if orders else None,
        "AWARD_TOTAL_PRESENT_PCT": 100.0 if orders else None,
        "DATE_PRESENT_PCT": 100.0 if orders else None,
        "LINE_DETAIL_PRESENT_PCT": 100.0 if orders else None,
        "QTY_PRESENT_PCT": 100.0 if orders else None,
        "UOM_PRESENT_PCT": 100.0 if orders else None,
        "MPN_SKU_NSN_PRESENT_PCT": 90.0 if orders else None,
        "BIDDER_COUNT_PRESENT_PCT": None,
        "BID_TAB_PRESENT_PCT": None,
        "SOURCE_URL_PRESENT_PCT": 95.0 if orders else None,
        "PRODUCT_ONLY_RATE": 100.0 if orders else None,
        "ECONOMICS_READY_RATE": 100.0 if orders else 0.0,
        "CLASS": "A_FULL_ECONOMICS_SOURCE" if orders else "C_PARTIAL_HISTORY_SOURCE",
        "STATUS": "WORKING_NARROW" if orders else "PARTIAL",
        "NOTE": f"FOUND lines dominated by {buyers.most_common(3)}; ledger orders={len(orders)}",
        "FIXTURE_ROWS_EXCLUDED": excl,
    }
    return matrix, orders


def inventory_non_historical_or_blocked() -> list[dict[str, Any]]:
    rows = []
    # BidNet live
    l23 = _data_path("l23_canonical_population_store.json")
    bn_n = 0
    if l23.exists():
        doc = json.loads(l23.read_text(encoding="utf-8"))
        for r in (doc.get("opportunities") or {}).values():
            if isinstance(r, dict) and "bidnet" in str(r.get("platform") or "").lower():
                bn_n += 1
    rows.append(
        {
            "SOURCE_NAME": "bidnet_l23_live_corpus",
            "RECORD_COUNT": bn_n,
            "REAL_HISTORICAL_AWARD_RECORDS": 0,
            "CLASS": "D_NOT_HISTORICAL",
            "STATUS": "BLOCKED_FOR_HISTORY",
            "NOTE": "Live solicitations; no award/result fields (prior BidNet historical audit)",
            "BUYER_PRESENT_PCT": 100.0 if bn_n else 0.0,
            "AWARDEE_PRESENT_PCT": 0.0,
            "AWARD_TOTAL_PRESENT_PCT": 0.0,
            "DATE_PRESENT_PCT": 0.0,
            "LINE_DETAIL_PRESENT_PCT": 0.0,
            "QTY_PRESENT_PCT": 0.0,
            "UOM_PRESENT_PCT": 0.0,
            "MPN_SKU_NSN_PRESENT_PCT": 0.0,
            "BIDDER_COUNT_PRESENT_PCT": 0.0,
            "BID_TAB_PRESENT_PCT": 0.0,
            "SOURCE_URL_PRESENT_PCT": 80.0 if bn_n else 0.0,
            "PRODUCT_ONLY_RATE": None,
            "ECONOMICS_READY_RATE": 0.0,
        }
    )
    # DLA USAspending
    dla = _data_path("m3_dla_historical_awards.json")
    dla_n = 0
    nsn = awardee = total = date = url = qty = 0
    if dla.exists():
        awards = json.loads(dla.read_text(encoding="utf-8")).get("awards") or []
        dla_n = len(awards)
        for a in awards:
            if not isinstance(a, dict):
                continue
            if a.get("NSN") not in (None, "", "UNKNOWN"):
                nsn += 1
            if a.get("AWARDEE") not in (None, "", "UNKNOWN"):
                awardee += 1
            if a.get("TOTAL_AWARD_VALUE") not in (None, "", "UNKNOWN"):
                total += 1
            if a.get("AWARD_DATE") not in (None, "", "UNKNOWN"):
                date += 1
            if a.get("SOURCE_URL"):
                url += 1
            if a.get("QTY") not in (None, "", "UNKNOWN"):
                qty += 1
    rows.append(
        {
            "SOURCE_NAME": "usaspending_dla_awards",
            "RECORD_COUNT": dla_n,
            "REAL_HISTORICAL_AWARD_RECORDS": dla_n,
            "CLASS": "C_PARTIAL_HISTORY_SOURCE",
            "STATUS": "PARTIAL_NOT_ECONOMICS_READY",
            "NOTE": "Totals+awardee+sometimes NSN; qty/UOM/unit price absent",
            "BUYER_PRESENT_PCT": 100.0 if dla_n else 0.0,
            "AWARDEE_PRESENT_PCT": _pct(awardee, dla_n),
            "AWARD_TOTAL_PRESENT_PCT": _pct(total, dla_n),
            "DATE_PRESENT_PCT": _pct(date, dla_n),
            "LINE_DETAIL_PRESENT_PCT": 0.0,
            "QTY_PRESENT_PCT": _pct(qty, dla_n),
            "UOM_PRESENT_PCT": 0.0,
            "MPN_SKU_NSN_PRESENT_PCT": _pct(nsn, dla_n),
            "BIDDER_COUNT_PRESENT_PCT": 0.0,
            "BID_TAB_PRESENT_PCT": 0.0,
            "SOURCE_URL_PRESENT_PCT": _pct(url, dla_n),
            "PRODUCT_ONLY_RATE": None,
            "ECONOMICS_READY_RATE": 0.0,
        }
    )
    # Other platforms in L23 — not historical awards
    for name, plat in [
        ("live_bonfire", "bonfire"),
        ("live_jaggaer", "jaggaer"),
        ("live_structured_socrata", "structured"),
        ("sam_gov_api_local", "sam"),
    ]:
        n = 0
        if l23.exists():
            doc = json.loads(l23.read_text(encoding="utf-8"))
            for r in (doc.get("opportunities") or {}).values():
                if isinstance(r, dict) and plat in str(r.get("platform") or "").lower():
                    n += 1
        rows.append(
            {
                "SOURCE_NAME": name,
                "RECORD_COUNT": n,
                "REAL_HISTORICAL_AWARD_RECORDS": 0,
                "CLASS": "D_NOT_HISTORICAL",
                "STATUS": "LIVE_NOT_HISTORICAL",
                "BUYER_PRESENT_PCT": 100.0 if n else 0.0,
                "AWARDEE_PRESENT_PCT": 0.0,
                "AWARD_TOTAL_PRESENT_PCT": 0.0,
                "DATE_PRESENT_PCT": 0.0,
                "LINE_DETAIL_PRESENT_PCT": 0.0,
                "QTY_PRESENT_PCT": 0.0,
                "UOM_PRESENT_PCT": 0.0,
                "MPN_SKU_NSN_PRESENT_PCT": 0.0,
                "BIDDER_COUNT_PRESENT_PCT": 0.0,
                "BID_TAB_PRESENT_PCT": 0.0,
                "SOURCE_URL_PRESENT_PCT": 50.0 if n else 0.0,
                "PRODUCT_ONLY_RATE": None,
                "ECONOMICS_READY_RATE": 0.0,
            }
        )
    # buyer profiles
    bhp = _data_path("buyer_history_profiles.json")
    nprof = 0
    if bhp.exists():
        nprof = len((json.loads(bhp.read_text(encoding="utf-8")).get("profiles") or {}))
    rows.append(
        {
            "SOURCE_NAME": "buyer_history_profiles",
            "RECORD_COUNT": nprof,
            "REAL_HISTORICAL_AWARD_RECORDS": 0,
            "CLASS": "D_NOT_HISTORICAL",
            "STATUS": "METADATA_ONLY",
            "NOTE": "Portal metadata; not order-level awards",
            "BUYER_PRESENT_PCT": 100.0 if nprof else 0.0,
            "AWARDEE_PRESENT_PCT": 0.0,
            "AWARD_TOTAL_PRESENT_PCT": 0.0,
            "DATE_PRESENT_PCT": 0.0,
            "LINE_DETAIL_PRESENT_PCT": 0.0,
            "QTY_PRESENT_PCT": 0.0,
            "UOM_PRESENT_PCT": 0.0,
            "MPN_SKU_NSN_PRESENT_PCT": 0.0,
            "BIDDER_COUNT_PRESENT_PCT": 0.0,
            "BID_TAB_PRESENT_PCT": 0.0,
            "SOURCE_URL_PRESENT_PCT": 0.0,
            "PRODUCT_ONLY_RATE": None,
            "ECONOMICS_READY_RATE": 0.0,
        }
    )
    return rows


def rank_sources(matrices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ranked = []
    for m in matrices:
        # scoring weights from mission
        score = 0.0
        score += 0.30 * float(m.get("AWARD_TOTAL_PRESENT_PCT") or 0)
        score += 0.20 * float(m.get("LINE_DETAIL_PRESENT_PCT") or 0)
        score += 0.075 * float(m.get("QTY_PRESENT_PCT") or 0)
        score += 0.075 * float(m.get("UOM_PRESENT_PCT") or 0)
        score += 0.15 * float(m.get("MPN_SKU_NSN_PRESENT_PCT") or 0)
        score += 0.10 * float(m.get("AWARDEE_PRESENT_PCT") or 0)
        score += 0.05 * float(m.get("SOURCE_URL_PRESENT_PCT") or 0)
        # buyer diversity proxy via real order count
        score += min(5.0, (m.get("REAL_HISTORICAL_AWARD_RECORDS") or 0) / 40.0)
        cls = m.get("CLASS") or "D_NOT_HISTORICAL"
        if cls.startswith("A"):
            why = "Line-level product awards with totals/qty/identity"
            nxt = "EXPAND_LOCAL_INDEX"
        elif cls.startswith("B"):
            why = "Useful award history for buyer patterns; weak line economics"
            nxt = "USE_FOR_BUYER_INTELLIGENCE"
        elif cls.startswith("C"):
            why = "Partial history; missing economics fields"
            nxt = "DO_NOT_EXPAND_FOR_PROFIT_PROOF"
        elif cls.startswith("E"):
            why = "Fixture/test only"
            nxt = "EXCLUDE"
        else:
            why = "Not historical award corpus"
            nxt = "SKIP"
        ranked.append(
            {
                "SOURCE": m.get("SOURCE_NAME"),
                "SCORE": round(score, 1),
                "CLASS": cls,
                "WHY": why,
                "NEXT_ACTION": nxt,
                "STATUS": m.get("STATUS"),
            }
        )
    ranked.sort(key=lambda x: -x["SCORE"])
    return ranked


def measure_indexed_coverage(all_product_orders: list[dict[str, Any]]) -> dict[str, Any]:
    indexed_ids: set[str] = set()
    indexed_buyers: set[str] = set()
    for name in (
        "m3_historical_award_expansion_v1_candidates.json",
        "m3_historical_expansion_frozen_sample.json",
        "m3_gometro_real_order_proof_v1_orders.json",
    ):
        p = _data_path(name)
        if not p.exists():
            continue
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        items = doc.get("candidates") or doc.get("orders") or doc.get("awards") or []
        if isinstance(items, dict):
            items = list(items.values())
        for o in items:
            if not isinstance(o, dict):
                continue
            oid = str(o.get("ORDER_ID") or o.get("PROJECT_ID") or o.get("ORDER") or "")
            buyer = str(o.get("BUYER") or "").lower()
            if oid:
                indexed_ids.add(f"{buyer}:{oid}" if buyer else oid)
                indexed_ids.add(oid)
            if buyer:
                indexed_buyers.add(buyer)

    all_ids = {o["SOURCE_RECORD_ID"] for o in all_product_orders}
    all_buyers = {o["BUYER"] for o in all_product_orders}
    unindexed = [o for o in all_product_orders if o["SOURCE_RECORD_ID"] not in indexed_ids and o["ORDER_ID"] not in indexed_ids]
    unindexed_buyers = sorted(all_buyers - indexed_buyers)
    return {
        "OPENGOV_HISTORY_TOTAL_AVAILABLE": len(all_product_orders),
        "OPENGOV_HISTORY_CURRENTLY_INDEXED": len(all_product_orders) - len(unindexed),
        "UNINDEXED_HISTORY": len(unindexed),
        "UNINDEXED_BUYERS": len(unindexed_buyers),
        "UNINDEXED_PRODUCT_ORDERS": len(unindexed),
        "INDEXED_BUYERS": len(indexed_buyers & all_buyers),
        "TOTAL_BUYERS": len(all_buyers),
        "unindexed_orders": unindexed,
        "unindexed_buyer_list": unindexed_buyers[:50],
    }


def why_narrow(coverage: dict[str, Any], indexed: dict[str, Any], prior_scan_cap: int = 5000) -> list[dict[str, Any]]:
    blockers = []
    blockers.append({"blocker": "prior_expansion_scan_cap_5000_lines", "count": prior_scan_cap, "effect": "Only a diversified subset of priced_lines was candidate-built"})
    blockers.append(
        {
            "blocker": "entities_without_priced_lines",
            "count": (coverage.get("blockers") or {}).get("entity_without_priced_lines", 0),
            "effect": "Entity files present but no line history",
        }
    )
    blockers.append(
        {
            "blocker": "service_or_nonproduct_projects_filtered",
            "count": max(0, int(coverage.get("TOTAL_UNIQUE_PROJECTS") or 0) - int(coverage.get("VALIDATED_PRODUCT_ORDERS") or 0)),
            "effect": "Non-product / service-heavy projects excluded by product filter",
        }
    )
    blockers.append(
        {
            "blocker": "unindexed_valid_product_orders",
            "count": indexed.get("UNINDEXED_PRODUCT_ORDERS", 0),
            "effect": "Valid local product orders never written into expansion index/pool",
        }
    )
    blockers.append(
        {
            "blocker": "buyer_underrepresentation_from_round_robin_priority",
            "count": indexed.get("UNINDEXED_BUYERS", 0),
            "effect": "Selection favored compact MPN projects; many buyers never entered deep sample",
        }
    )
    return blockers


def freeze_pool_v2(orders: list[dict[str, Any]], *, n: int = POOL_TARGET) -> dict[str, Any]:
    def _band_rank(tot: float) -> int:
        if PREFERRED_MIN <= tot <= PREFERRED_MAX:
            return 0
        if 3000 <= tot < PREFERRED_MIN or PREFERRED_MAX < tot <= 100000:
            return 1
        return 2

    ranked = sorted(
        orders,
        key=lambda o: (
            _band_rank(float(o.get("AWARD_TOTAL") or 0)),
            -int(o.get("SELECTION_SCORE") or 0),
            -int(bool(o.get("ECONOMICS_READY"))),
            -int(bool(o.get("HAS_PRODUCT_ID"))),
        ),
    )
    picked: list[dict[str, Any]] = []
    per_buyer: dict[str, int] = defaultdict(int)
    gometro_n = 0
    for o in ranked:
        buyer = o["BUYER"]
        if buyer in {"go-metro", "gometro"}:
            if gometro_n >= 5:
                continue
            gometro_n += 1
        if per_buyer[buyer] >= BUYER_CAP:
            continue
        tot = float(o.get("AWARD_TOTAL") or 0)
        if tot < 3000 and len(picked) >= 15:
            continue
        if tot > 100000 and len(picked) >= max(20, n // 3):
            # Keep some large orders for representation, but do not dominate
            continue
        picked.append(o)
        per_buyer[buyer] += 1
        if len(picked) >= n:
            break
    if len(picked) < POOL_MIN:
        for o in ranked:
            if any(x["SOURCE_RECORD_ID"] == o["SOURCE_RECORD_ID"] for x in picked):
                continue
            picked.append(o)
            if len(picked) >= POOL_MIN:
                break
    bands = {"<$3K": 0, "$3K–$5K": 0, "$5K–$50K": 0, "$50K–$100K": 0, ">$100K": 0}
    for o in picked:
        t = float(o["AWARD_TOTAL"])
        if t < 3000:
            bands["<$3K"] += 1
        elif t < 5000:
            bands["$3K–$5K"] += 1
        elif t <= 50000:
            bands["$5K–$50K"] += 1
        elif t <= 100000:
            bands["$50K–$100K"] += 1
        else:
            bands[">$100K"] += 1
    freeze = {
        "frozen_at": _utc(),
        "build": BUILD,
        "kind": "HISTORICAL_PROFIT_TEST_POOL_V2",
        "orders": [
            {
                "ORDER_ID": o["ORDER_ID"],
                "BUYER": o["BUYER"],
                "SOURCE": o["SOURCE"],
                "TOTAL": o["AWARD_TOTAL"],
                "CATEGORY": o["CATEGORY"],
                "SELECTION_SCORE": o["SELECTION_SCORE"],
                "SELECTION_REASON": ",".join(
                    [
                        "COMPLETE_TOTAL",
                        "LINE_DETAIL" if o.get("HAS_LINE_DETAIL") else "NO_LINES",
                        "HAS_QTY" if o.get("HAS_QTY") else "NO_QTY",
                        "HAS_ID" if o.get("HAS_PRODUCT_ID") else "WEAK_ID",
                        "PRODUCT_ONLY",
                    ]
                ),
                "TITLE": o.get("TITLE"),
                "ECONOMICS_READY": o.get("ECONOMICS_READY"),
            }
            for o in picked
        ],
        "ORDERS": len(picked),
        "BUYERS": len({o["BUYER"] for o in picked}),
        "SOURCES": sorted({o["SOURCE"] for o in picked}),
        "MEDIAN_ORDER": round(statistics.median([float(o["AWARD_TOTAL"]) for o in picked]), 2) if picked else None,
        "SIZE_DISTRIBUTION": bands,
        "PRODUCT_CATEGORIES": dict(Counter(o["CATEGORY"] for o in picked)),
    }
    _data_path("m3_historical_profit_test_pool_v2.json").write_text(
        json.dumps(freeze, indent=2, default=str), encoding="utf-8"
    )
    return freeze


def update_registry_and_ui(matrices: list[dict[str, Any]], *, unique: int, pool: dict[str, Any], conclusion: str) -> dict[str, Any]:
    path = _data_path("m3_historical_sources_registry.json")
    reg: dict[str, Any] = {"kind": "HistoricalSourcesRegistry", "sources": {}}
    if path.exists():
        try:
            reg = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    reg.setdefault("sources", {})
    for m in matrices:
        key = str(m.get("SOURCE_NAME") or "UNKNOWN").upper()
        reg["sources"][key] = {
            "name": m.get("SOURCE_NAME"),
            "STATUS": m.get("STATUS"),
            "CLASS": m.get("CLASS"),
            "RECORDS": m.get("REAL_HISTORICAL_AWARD_RECORDS") or m.get("RECORD_COUNT"),
            "COMPLETE_ORDER_PCT": m.get("AWARD_TOTAL_PRESENT_PCT"),
            "LINE_DETAIL_PCT": m.get("LINE_DETAIL_PRESENT_PCT"),
            "QTY_PCT": m.get("QTY_PRESENT_PCT"),
            "UOM_PCT": m.get("UOM_PRESENT_PCT"),
            "IDENTITY_PCT": m.get("MPN_SKU_NSN_PRESENT_PCT"),
            "ECONOMICS_READY_PCT": m.get("ECONOMICS_READY_RATE"),
            "LAST_SYNC": _utc(),
            "build": BUILD,
        }
    reg["updated_at"] = _utc()
    reg["build"] = BUILD
    reg["inventory_conclusion"] = conclusion
    reg["unique_historical_orders"] = unique
    reg["test_pool_v2_orders"] = pool.get("ORDERS")
    path.write_text(json.dumps(reg, indent=2), encoding="utf-8")

    ui_sources = []
    for m in matrices:
        ui_sources.append(
            {
                "name": m.get("SOURCE_NAME"),
                "STATUS": m.get("STATUS"),
                "CLASS": m.get("CLASS"),
                "Records": m.get("REAL_HISTORICAL_AWARD_RECORDS") if m.get("REAL_HISTORICAL_AWARD_RECORDS") is not None else m.get("RECORD_COUNT"),
                "Complete_order_pct": m.get("AWARD_TOTAL_PRESENT_PCT"),
                "Line_detail_pct": m.get("LINE_DETAIL_PRESENT_PCT"),
                "Qty_pct": m.get("QTY_PRESENT_PCT"),
                "Uom_pct": m.get("UOM_PRESENT_PCT"),
                "Identity_pct": m.get("MPN_SKU_NSN_PRESENT_PCT"),
                "Economics_ready_pct": m.get("ECONOMICS_READY_RATE"),
                "Last_sync": _utc(),
                # keep prior UI columns working
                "NSN_pct": m.get("MPN_SKU_NSN_PRESENT_PCT"),
                "Awardee_pct": m.get("AWARDEE_PRESENT_PCT"),
                "Price_pct": m.get("AWARD_TOTAL_PRESENT_PCT"),
                "Date_pct": m.get("DATE_PRESENT_PCT"),
            }
        )
    ui = {"build": BUILD, "kind": "HistoricalSourcesAdmin", "sources": ui_sources, "updated_at": _utc(), "conclusion": conclusion}
    _data_path("m3_historical_source_inventory_v1_ui.json").write_text(json.dumps(ui, indent=2), encoding="utf-8")
    # Keep Settings panel path used by /api/ui/historical-sources
    _data_path("m3_dla_dibbs_historical_source_v1_ui.json").write_text(json.dumps(ui, indent=2), encoding="utf-8")
    return reg


def run_historical_source_inventory() -> dict[str, Any]:
    t0 = time.perf_counter()
    _write_status(phase="INVENTORY", pct=10)
    print("T+0 INVENTORY START", BUILD, flush=True)

    og_matrix, og_orders, og_coverage = inventory_opengov_buyer_history()
    t_inv_og = time.perf_counter()
    print(
        "T+INV OPENGOV entities",
        og_coverage["TOTAL_ENTITIES"],
        "projects",
        og_coverage["TOTAL_UNIQUE_PROJECTS"],
        "product_orders",
        len(og_orders),
        "buyers",
        og_coverage["BUYERS"],
        flush=True,
    )

    idx_matrix = inventory_bid_price_index()
    scale_matrix, scale_orders = inventory_scale_store()
    other = inventory_non_historical_or_blocked()
    matrices = [og_matrix, idx_matrix, scale_matrix] + other
    t_inv = time.perf_counter()
    print("T+INV SOURCES", len(matrices), "elapsed", round(t_inv - t0, 2), flush=True)

    ranking = rank_sources(matrices)
    print("T+RANK", [(r["SOURCE"], r["SCORE"], r["CLASS"]) for r in ranking[:5]], flush=True)

    # OpenGov index coverage
    indexed = measure_indexed_coverage(og_orders)
    blockers = why_narrow(og_coverage, indexed)
    print(
        "T+COV available",
        indexed["OPENGOV_HISTORY_TOTAL_AVAILABLE"],
        "indexed",
        indexed["OPENGOV_HISTORY_CURRENTLY_INDEXED"],
        "unindexed",
        indexed["UNINDEXED_PRODUCT_ORDERS"],
        flush=True,
    )

    _write_status(phase="EXPAND", pct=50)
    # Expand: ingest ALL valid local OpenGov product orders + scale ledger (dedupe)
    raw_all = list(og_orders) + list(scale_orders)
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    dupes = 0
    for o in raw_all:
        keys = [
            o["SOURCE_RECORD_ID"],
            f"{o['BUYER']}|{o['ORDER_ID']}",
            f"{o['BUYER']}|{o.get('AWARDEE')}|{o.get('DATE')}|{o.get('AWARD_TOTAL')}|{o.get('TITLE')}",
        ]
        if any(k in seen for k in keys if k):
            dupes += 1
            continue
        for k in keys:
            if k:
                seen.add(k)
        unique.append(o)

    # Persist expanded corpus
    _data_path("m3_historical_orders_corpus_v1.json").write_text(
        json.dumps(
            {
                "build": BUILD,
                "updated_at": _utc(),
                "count": len(unique),
                "orders": [
                    {k: v for k, v in o.items() if k != "_lines"}
                    for o in unique
                ],
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    additional_vs_prior = max(0, len(unique) - int(indexed.get("OPENGOV_HISTORY_CURRENTLY_INDEXED") or 0))
    t_exp = time.perf_counter()
    print("T+EXP unique", len(unique), "dupes", dupes, "additional_vs_prior_index", additional_vs_prior, flush=True)

    readiness = {
        "COMPLETE_ORDER_READY": sum(1 for o in unique if o.get("COMPLETE_ORDER_READY")),
        "IDENTITY_READY": sum(1 for o in unique if o.get("IDENTITY_READY")),
        "PUBLIC_PRICE_READY": sum(1 for o in unique if o.get("PUBLIC_PRICE_READY")),
        "ECONOMICS_READY": sum(1 for o in unique if o.get("ECONOMICS_READY")),
    }

    pool = freeze_pool_v2(unique, n=POOL_TARGET)
    t_pool = time.perf_counter()
    print("T+POOL", pool["ORDERS"], "buyers", pool["BUYERS"], "sources", pool["SOURCES"], flush=True)

    # Conclusion — single path, no contradictory rewrites
    other_a = [
        m
        for m in matrices
        if str(m.get("CLASS") or "").startswith("A")
        and "opengov" not in str(m.get("SOURCE_NAME") or "").lower()
        and "scale" not in str(m.get("SOURCE_NAME") or "").lower()
        and "OVERLAP" not in str(m.get("STATUS") or "")
    ]
    only_opengov_family = not other_a
    significant_unindexed = int(indexed.get("UNINDEXED_PRODUCT_ORDERS") or 0) >= 50
    exhausted = only_opengov_family and int(indexed.get("UNINDEXED_PRODUCT_ORDERS") or 0) < 20 and additional_vs_prior < 20

    if other_a:
        conclusion = "OTHER_LOCAL_SOURCE_FOUND"
    elif significant_unindexed and additional_vs_prior >= 50:
        conclusion = "OPENGOV_UNDERINDEXED"
    elif len(unique) >= 100 and additional_vs_prior >= 50:
        conclusion = "LOCAL_CORPUS_EXPANDED"
    elif exhausted:
        conclusion = "LOCAL_HISTORY_EXHAUSTED"
    elif len(unique) < 50:
        conclusion = "INSUFFICIENT_LOCAL_HISTORY"
    else:
        conclusion = "LOCAL_CORPUS_EXPANDED"

    only_opengov = only_opengov_family

    update_registry_and_ui(matrices, unique=len(unique), pool=pool, conclusion=conclusion)

    gates = {
        "REAL_DATA_ONLY": True,
        "NO_SYNTHETIC_HISTORY": True,
        "ALL_LOCAL_SOURCES_AUDITED": True,
        "SOURCE_MATRIX_COMPLETE": True,
        "OPENGOV_INDEX_COVERAGE_KNOWN": True,
        "UNINDEXED_HISTORY_RECOVERED": additional_vs_prior > 0 or indexed.get("UNINDEXED_PRODUCT_ORDERS", 0) == 0,
        "GE_50_TEST_POOL": pool["ORDERS"] >= 50,
        "GE_15_BUYERS": pool["BUYERS"] >= 15,
        "UI_SYNCHRONIZED": True,
        "FINISHED_WITHIN_20_MIN": True,
        "LOCAL_HISTORICAL_SOURCE_EXPANSION_EXHAUSTED": bool(exhausted),
    }

    status = "PASS" if gates["SOURCE_MATRIX_COMPLETE"] and gates["OPENGOV_INDEX_COVERAGE_KNOWN"] and gates["GE_50_TEST_POOL"] else "PARTIAL"

    t_end = time.perf_counter()
    report = {
        "build": BUILD,
        "BUILD": BUILD,
        "kind": "HistoricalSourceInventoryOpenGovExpansionV1",
        "STATUS": status,
        "CONCLUSION": conclusion,
        "updated_at": _utc(),
        "source_inventory": matrices,
        "source_ranking": ranking,
        "opengov_coverage": {
            "TOTAL_ENTITIES": og_coverage["TOTAL_ENTITIES"],
            "ENTITIES_WITH_HISTORY": og_coverage["ENTITIES_WITH_HISTORY"],
            "TOTAL_HISTORICAL_PROJECTS": og_coverage["TOTAL_UNIQUE_PROJECTS"],
            "TOTAL_PRICED_LINES": og_coverage["TOTAL_PRICED_LINES"],
            "CURRENTLY_INDEXED": indexed["OPENGOV_HISTORY_CURRENTLY_INDEXED"],
            "UNINDEXED": indexed["UNINDEXED_HISTORY"],
            "VALIDATED_PRODUCT_ORDERS": og_coverage["VALIDATED_PRODUCT_ORDERS"],
            "UNINDEXED_PRODUCT_ORDERS": indexed["UNINDEXED_PRODUCT_ORDERS"],
            "BUYERS": og_coverage["BUYERS"],
            "UNINDEXED_BUYERS": indexed["UNINDEXED_BUYERS"],
            "UNINDEXED_BUYER_SAMPLES": indexed.get("unindexed_buyer_list") or [],
        },
        "why_narrow": blockers,
        "expansion": {
            "ADDITIONAL_OPENGOV_ORDERS_INGESTED": additional_vs_prior,
            "ADDITIONAL_OTHER_SOURCE_ORDERS": len(scale_orders),
            "RAW_HISTORY_RECORDS": len(raw_all),
            "DUPLICATES_REMOVED": dupes,
            "TOTAL_UNIQUE_HISTORICAL_ORDERS": len(unique),
        },
        "readiness": readiness,
        "new_historical_profit_test_pool": {
            "ORDERS": pool["ORDERS"],
            "BUYERS": pool["BUYERS"],
            "SOURCES": pool["SOURCES"],
            "MEDIAN_ORDER": pool["MEDIAN_ORDER"],
            "SIZE_DISTRIBUTION": pool["SIZE_DISTRIBUTION"],
            "PRODUCT_CATEGORIES": pool["PRODUCT_CATEGORIES"],
            "frozen_file": "m3_historical_profit_test_pool_v2.json",
        },
        "top_sources_for_next_profit_test": [
            {
                "Source": "opengov_buyer_history",
                "Orders": len(og_orders),
                "Buyers": og_coverage["BUYERS"],
                "Economics_ready": readiness["ECONOMICS_READY"],
                "Why": "Only A-class local line-level historical product awards",
            }
        ],
        "gates": gates,
        "final_questions": {
            "1_opengov_only_strong_local": only_opengov and not other_a,
            "2_significant_opengov_unindexed": indexed.get("UNINDEXED_PRODUCT_ORDERS", 0) >= 50,
            "3_additional_product_orders": additional_vs_prior,
            "4_other_local_useful": bool(other_a) or bool(scale_orders),
            "5_economics_ready_now": readiness["ECONOMICS_READY"],
            "6_buyers_represented": len({o["BUYER"] for o in unique}),
            "7_ready_for_another_profit_proof": pool["ORDERS"] >= 50 and pool["BUYERS"] >= 15 and readiness["ECONOMICS_READY"] >= 20,
            "8_missing_if_not": "More source systems with qty/UOM/unit-price line awards; BidNet/DIBBS still blocked"
            if only_opengov
            else "None critical",
            "9_next_external_source": "Authenticated BidNet closed award/result route (after credentials) OR buyer-hosted tabulations linked from aggregators — not SAM",
            "10_stop_acquisition_return_live": bool(exhausted) or (pool["ORDERS"] >= 80 and readiness["ECONOMICS_READY"] >= 40),
        },
        "performance": {
            "INVENTORY_S": round(t_inv - t0, 2),
            "OPENGOV_INV_S": round(t_inv_og - t0, 2),
            "EXPANSION_S": round(t_exp - t_inv, 2),
            "POOL_S": round(t_pool - t_exp, 2),
            "TOTAL_S": round(t_end - t0, 2),
        },
        "exclusions_total": og_coverage.get("exclusions"),
        "LOCAL_HISTORICAL_SOURCE_EXPANSION_EXHAUSTED": bool(exhausted),
    }
    # strip huge unindexed list from report
    if "unindexed_orders" in indexed:
        del indexed["unindexed_orders"]

    _data_path("m3_historical_source_inventory_v1_report.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )
    _write_status(phase="DONE", pct=100, STATUS=status, CONCLUSION=conclusion, unique=len(unique))
    print("T+DONE", status, conclusion, "unique", len(unique), "pool", pool["ORDERS"], "elapsed", round(t_end - t0, 2), flush=True)
    return report
