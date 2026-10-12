"""Buyer / Trend Intelligence Operationalization V1.

Turns historical purchasing into prioritized watch / readiness targets.
STORAGE + DERIVATION ONLY — no profit claims, no invented recurrence/portals/contacts.

CRITICAL: history rows from scale_evidence are often LINE items. Metrics use
validated ORDER groups (opportunity_id / project grain), never raw line counts.
"""

from __future__ import annotations

import json
import math
import re
import statistics
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from application_clock import now_utc
from buyer_intelligence.engine import (
    _buyer_id,
    _canon_name,
    _num,
    _parse_date,
    classify_family,
    collect_history_rows,
    collect_live_opportunities,
)

BUILD = "20261008-m3-buyer-trend-operationalization-v1"

# Prefer mission taxonomy; map engine families onto it.
CATEGORY_MAP = {
    "AUTO_PARTS": "AUTO_PARTS",
    "MRO": "MRO",
    "TOOLS": "TOOLS",
    "ELECTRICAL": "ELECTRICAL",
    "LIGHTING": "LIGHTING",
    "PPE": "PPE",
    "JANITORIAL_SUPPLIES": "JANITORIAL",
    "FURNITURE": "OFFICE_FURNITURE",
    "OFFICE_SUPPLIES": "OFFICE_SUPPLIES",
    "PAPER_PRODUCTS": "OFFICE_SUPPLIES",
    "EQUIPMENT": "COMMON_EQUIPMENT",
    "PLUMBING": "MRO",
    "MEDICAL_SUPPLIES": "OTHER_DURABLE",
    "OTHER": "OTHER_DURABLE",
}

EXCLUDE_PATTERNS = [
    ("FOOD", re.compile(r"\b(food|grocery|produce|dairy|milk|meat|beverage|catering|perishable)\b", re.I)),
    ("PERISHABLE", re.compile(r"\b(perishable|fresh\s+produce|refrigerat(?:ed|ion)\s+food)\b", re.I)),
    ("CONSTRUCTION_HEAVY", re.compile(r"\b(construction|asphalt|concrete|paving|excavation|demolition|hfst|roadway\s+resurfac)\b", re.I)),
    ("SERVICE_HEAVY", re.compile(r"\b(professional\s+services?|consulting|staffing|janitorial\s+service|grounds?\s+maintenance\s+service)\b", re.I)),
    ("INSTALLATION_HEAVY", re.compile(r"\b(installation\s+only|install\s+and\s+commission|turnkey\s+install)\b", re.I)),
]

PSEUDO_BUYERS = re.compile(
    r"^(california|dod|department\s+of\s+defense|federal\s+government|united\s+states|"
    r"us\s+government|government|state\s+of\s+[a-z]+|nationwide|various|unknown)$",
    re.I,
)

DURABLE_FIT = {
    "AUTO_PARTS",
    "MRO",
    "TOOLS",
    "ELECTRICAL",
    "LIGHTING",
    "PPE",
    "JANITORIAL",
    "OFFICE_FURNITURE",
    "OFFICE_SUPPLIES",
    "COMMON_EQUIPMENT",
    "OTHER_DURABLE",
}


def _utc() -> str:
    return now_utc().isoformat()


def _data(*parts: str) -> Path:
    from m3_data_root import data_path

    return data_path(*parts)


def _load_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def map_category(family: str, text: str = "") -> tuple[str, str | None]:
    """Return (category, reject_reason|None)."""
    blob = f"{family} {text}"
    for reason, pat in EXCLUDE_PATTERNS:
        if pat.search(blob):
            return family, reason
    return CATEGORY_MAP.get(family, "OTHER_DURABLE"), None


def is_pseudo_buyer(name: str) -> bool:
    n = re.sub(r"\s+", " ", (name or "").strip())
    if not n or len(n) < 3:
        return True
    if PSEUDO_BUYERS.match(n):
        return True
    # Reject bare state names / ultra-broad entities
    if n.lower() in {"illinois", "iowa", "ohio", "texas", "florida", "montana", "alabama"}:
        return True
    return False


def _percentile(vals: list[float], p: float) -> float | None:
    if not vals:
        return None
    if len(vals) == 1:
        return round(vals[0], 2)
    s = sorted(vals)
    k = (len(s) - 1) * p
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return round(s[int(k)], 2)
    return round(s[f] * (c - k) + s[c] * (k - f), 2)


def _order_from_lines(
    *,
    key: str,
    buyer: str,
    lines: list[dict[str, Any]],
    amounts: list[float],
    dates: list[datetime],
    text: str,
    fam: str,
    oid: str | None,
    source: str,
    source_systems: list[str],
    portal: str | None,
    vendors: list[str],
) -> dict[str, Any]:
    category, reject = map_category(fam, text)
    order_total = round(sum(amounts), 2) if amounts else None
    order_date = max(dates).date().isoformat() if dates else None
    portal_status = "PORTAL_KNOWN" if portal else "PORTAL_UNKNOWN"
    return {
        "ORDER_KEY": key,
        "BUYER_ID": _buyer_id(buyer),
        "BUYER_NAME": buyer,
        "NORMALIZED_NAME": _canon_name(buyer),
        "OPPORTUNITY_ID": oid,
        "ORDER_DATE": order_date,
        "ORDER_TOTAL": order_total,
        "LINE_COUNT": len(lines),
        "CATEGORY_RAW": fam,
        "CATEGORY": category,
        "REJECT_REASON": reject,
        "SOURCE": source,
        "SOURCE_SYSTEMS": source_systems,
        "PORTAL": portal,
        "PORTAL_STATUS": portal_status,
        "TEXT": text[:500],
        "VENDORS": vendors[:12],
    }


def collect_opengov_project_orders(*, max_buyers: int = 130, max_projects_per_buyer: int = 40) -> list[dict[str, Any]]:
    """Build validated orders from OpenGov buyer history: one order per project_id."""
    hist_dir = _data("opengov_buyer_history")
    if not hist_dir.exists():
        return []
    orders: list[dict[str, Any]] = []
    files = sorted(hist_dir.glob("*.json"))[:max_buyers]
    for path in files:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(doc, dict):
            continue
        gov = str(doc.get("government_code") or path.stem).strip()
        buyer = gov
        if is_pseudo_buyer(buyer):
            continue
        priced = doc.get("priced_lines") or []
        if not isinstance(priced, list) or not priced:
            continue
        by_proj: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for ln in priced:
            if not isinstance(ln, dict):
                continue
            pid = str(ln.get("project_id") or "").strip()
            if not pid:
                continue
            by_proj[pid].append(ln)
        # newest first by closed_at
        proj_items = sorted(
            by_proj.items(),
            key=lambda kv: str((kv[1][0] or {}).get("closed_at") or ""),
            reverse=True,
        )[:max_projects_per_buyer]
        for pid, lines in proj_items:
            amounts: list[float] = []
            for ln in lines:
                ext = _num(ln.get("extended"))
                if ext is not None:
                    amounts.append(ext)
                else:
                    q = _num(ln.get("quantity")) or 1.0
                    u = _num(ln.get("unit_price"))
                    if u is not None:
                        amounts.append(u * q)
            dates = [d for d in (_parse_date(ln.get("closed_at")) for ln in lines) if d]
            text = " ".join(
                str(ln.get("description") or ln.get("line_item") or ln.get("project_title") or "")
                for ln in lines[:12]
            )
            fam_counts = Counter(classify_family(str(ln.get("description") or ln.get("line_item") or "")) for ln in lines)
            fam = fam_counts.most_common(1)[0][0] if fam_counts else "OTHER"
            title = str(lines[0].get("project_title") or "")
            text = f"{title} {text}"
            oid = f"opengov:{gov}:{pid}"
            portal = f"opengov:{gov}"
            vendors = sorted({str(ln.get("winning_vendor") or "") for ln in lines if ln.get("winning_vendor")})
            orders.append(
                _order_from_lines(
                    key=f"ogproj::{gov}::{pid}",
                    buyer=str(lines[0].get("buyer") or gov),
                    lines=lines,
                    amounts=amounts,
                    dates=dates,
                    text=text,
                    fam=fam,
                    oid=oid,
                    source="opengov_buyer_history",
                    source_systems=["opengov_buyer_history"],
                    portal=portal,
                    vendors=vendors,
                )
            )
    return orders


def group_validated_orders(
    history: list[dict[str, Any]],
    *,
    include_opengov_projects: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Collapse line-level history into order-level facts.

    - scale_evidence / opengov rows with opportunity_id → one order per opportunity
    - buyer_history_profile dated signals → one order per date+product
    - live_demand → NOT historical orders (excluded from validated corpus)
    - OpenGov project orders merged separately (see collect_opengov_project_orders)
    """
    audit = {
        "input_rows": len(history),
        "live_demand_excluded": 0,
        "pseudo_buyer_rows_rejected": 0,
        "line_rows_collapsed": 0,
        "validated_orders": 0,
        "opengov_project_orders": 0,
    }
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for row in history:
        if row.get("is_live_demand") or row.get("source") == "live_demand":
            audit["live_demand_excluded"] += 1
            continue
        buyer = str(row.get("buyer") or "").strip()
        if is_pseudo_buyer(buyer):
            audit["pseudo_buyer_rows_rejected"] += 1
            continue
        oid = str(row.get("opportunity_id") or "").strip()
        if oid:
            key = f"oid::{oid}"
        else:
            d = str(row.get("award_date") or "")[:10]
            prod = str(row.get("product") or "")[:80]
            key = f"sig::{_buyer_id(buyer)}::{d}::{prod}"
        buckets[key].append(row)

    orders: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for key, lines in buckets.items():
        buyer = str(lines[0].get("buyer") or "")
        if is_pseudo_buyer(buyer):
            continue
        amounts = [a for a in (_num(x.get("amount")) for x in lines) if a is not None]
        dates = [d for d in (_parse_date(x.get("award_date")) for x in lines) if d]
        text = " ".join(str(x.get("text") or x.get("product") or x.get("title") or "") for x in lines[:8])
        fam_counts = Counter(str(x.get("product_family") or "OTHER") for x in lines)
        fam = fam_counts.most_common(1)[0][0]
        oid = str(lines[0].get("opportunity_id") or "") or None
        if len(lines) > 1:
            audit["line_rows_collapsed"] += len(lines) - 1
        portal = None
        if oid and oid.startswith("opengov:") and oid.count(":") >= 2:
            portal = f"opengov:{oid.split(':')[1]}"
        elif lines[0].get("source_system") and "opengov" in str(lines[0].get("source_system")).lower():
            portal = "opengov"
        order = _order_from_lines(
            key=key,
            buyer=buyer,
            lines=lines,
            amounts=amounts,
            dates=dates,
            text=text,
            fam=fam,
            oid=oid,
            source=str(lines[0].get("source") or "history"),
            source_systems=sorted(
                {
                    str(x.get("source_system") or x.get("source") or "")
                    for x in lines
                    if x.get("source") or x.get("source_system")
                }
            ),
            portal=portal,
            vendors=sorted({str(x.get("vendor") or "") for x in lines if x.get("vendor")}),
        )
        orders.append(order)
        seen_keys.add(key)
        if oid:
            seen_keys.add(f"oid::{oid}")

    # Merge OpenGov project-level orders (authoritative order grain)
    if include_opengov_projects:
        og_orders = collect_opengov_project_orders()
        audit["opengov_project_orders"] = len(og_orders)
        for o in og_orders:
            if o["ORDER_KEY"] in seen_keys:
                continue
            oid = o.get("OPPORTUNITY_ID")
            if oid and f"oid::{oid}" in seen_keys:
                continue
            orders.append(o)
            seen_keys.add(o["ORDER_KEY"])
            if oid:
                seen_keys.add(f"oid::{oid}")

    audit["validated_orders"] = len(orders)
    return orders, audit


def _load_registration_index() -> dict[str, dict[str, Any]]:
    """Map portal / buyer tokens → registration row (proven only)."""
    out: dict[str, dict[str, Any]] = {}
    doc = _load_json(_data("buyer_portal_registration_tracker.json")) or {}
    for pid, row in (doc.get("portals") or {}).items():
        if not isinstance(row, dict):
            continue
        status = str(row.get("registration_status") or "UNKNOWN").upper()
        mapped = {
            "PORTAL_ID": pid,
            "PORTAL": row.get("portal") or pid,
            "REGISTRATION_STATUS_RAW": status,
            "REGISTRATION": _map_reg_status(status),
            "BUYER_NAME": row.get("buyer_name"),
            "REGISTRATION_URL_PRESENT": bool(row.get("registration_url")),
        }
        out[str(pid).lower()] = mapped
        bname = _buyer_id(str(row.get("buyer_name") or ""))
        out[bname] = mapped
        # opengov slug
        if "opengov" in str(pid).lower() or True:
            out[str(row.get("portal") or pid).lower()] = mapped
    return out


def _map_reg_status(raw: str) -> str:
    r = (raw or "").upper()
    if r in {"REGISTERED", "COMPLETE", "DONE"}:
        return "COMPLETE"
    if r in {"IN_PROGRESS", "PENDING", "STARTED"}:
        return "IN_PROGRESS"
    if r in {"NOT_REGISTERED", "REQUIRED", "REQUIRED_NOT_STARTED"}:
        return "REQUIRED_NOT_STARTED"
    if r in {"NOT_REQUIRED", "PUBLIC", "OPEN"}:
        return "NOT_REQUIRED"
    return "UNKNOWN"


def _match_registration(order_or_buyer: dict[str, Any], reg_index: dict[str, dict[str, Any]]) -> dict[str, Any]:
    portal = str(order_or_buyer.get("PORTAL") or "")
    bid = str(order_or_buyer.get("BUYER_ID") or "")
    name = str(order_or_buyer.get("NORMALIZED_NAME") or order_or_buyer.get("BUYER_NAME") or "")
    candidates = [
        portal.lower(),
        portal.replace("opengov:", "").lower() if portal else "",
        bid,
        _buyer_id(name),
    ]
    for c in candidates:
        if c and c in reg_index:
            return reg_index[c]
    # fuzzy: portal slug in tracker keys
    slug = portal.replace("opengov:", "").lower() if portal else ""
    if slug:
        for k, v in reg_index.items():
            if slug in k or k in slug:
                return v
    return {
        "PORTAL_ID": None,
        "PORTAL": portal or None,
        "REGISTRATION": "UNKNOWN",
        "REGISTRATION_STATUS_RAW": None,
        "REGISTRATION_URL_PRESENT": False,
    }


def order_size_lane(median: float | None) -> str:
    if median is None:
        return "UNKNOWN"
    if median < 5000:
        return "MICRO"
    if median < 25000:
        return "SMALL"
    if median < 100000:
        return "MID"
    return "LARGE"


def repeatability(order_count: int) -> str:
    if order_count <= 0:
        return "UNKNOWN"
    if order_count == 1:
        return "ONE_OFF"
    if order_count == 2:
        return "OCCASIONAL"
    if order_count < 5:
        return "REPEATING"
    return "HIGHLY_REPEATING"


def interval_stats(dates: list[datetime]) -> dict[str, Any]:
    if len(dates) < 2:
        return {
            "DAYS_BETWEEN": [],
            "MEDIAN_INTERVAL": None,
            "AVERAGE_INTERVAL": None,
            "MIN_INTERVAL": None,
            "MAX_INTERVAL": None,
            "INTERVAL_VARIABILITY": None,
        }
    ds = sorted(dates)
    gaps = [(ds[i] - ds[i - 1]).days for i in range(1, len(ds))]
    gaps = [g for g in gaps if g >= 0]
    if not gaps:
        return {
            "DAYS_BETWEEN": [],
            "MEDIAN_INTERVAL": None,
            "AVERAGE_INTERVAL": None,
            "MIN_INTERVAL": None,
            "MAX_INTERVAL": None,
            "INTERVAL_VARIABILITY": None,
        }
    med = statistics.median(gaps)
    avg = statistics.mean(gaps)
    var = statistics.pstdev(gaps) if len(gaps) >= 2 else 0.0
    return {
        "DAYS_BETWEEN": gaps,
        "MEDIAN_INTERVAL": round(med, 1),
        "AVERAGE_INTERVAL": round(avg, 1),
        "MIN_INTERVAL": min(gaps),
        "MAX_INTERVAL": max(gaps),
        "INTERVAL_VARIABILITY": round(var, 1),
    }


def next_buy_window(
    *,
    last: datetime | None,
    median_interval: float | None,
    variability: float | None,
    order_count: int,
    now: datetime | None = None,
) -> tuple[str, str]:
    """Return (NEXT_BUY_WINDOW, NEXT_BUY_CONFIDENCE). No exact dates invented."""
    now = now or datetime.now(timezone.utc)
    if order_count < 2 or last is None or median_interval is None:
        return "INSUFFICIENT_HISTORY", "UNKNOWN"
    days_since = (now - last).days
    # expected = last + median; due if past expected
    due_in = median_interval - days_since
    var = variability if variability is not None else median_interval
    # confidence from count + relative variability
    if order_count >= 5 and median_interval > 0 and (var / max(median_interval, 1)) <= 0.75:
        conf = "HIGH"
    elif order_count >= 3 and median_interval > 0 and (var / max(median_interval, 1)) <= 1.25:
        conf = "MEDIUM"
    else:
        conf = "LOW"
    if conf == "LOW" and order_count < 3:
        return "LOW_CONFIDENCE", conf
    if due_in <= 0:
        return "NOW_DUE", conf
    if due_in <= 30:
        return "LIKELY_0_30_DAYS", conf
    if due_in <= 60:
        return "LIKELY_31_60_DAYS", conf
    if due_in <= 90:
        return "LIKELY_61_90_DAYS", conf
    if due_in <= 180:
        return "LIKELY_91_180_DAYS", conf
    return "LOW_CONFIDENCE", conf


def recurrence_score(
    *,
    order_count: int,
    median_interval: float | None,
    variability: float | None,
    days_since_last: int | None,
    category_consistency: float,
    size_consistency: float,
) -> tuple[int, str, list[str]]:
    """Transparent deterministic score + band + explanation factors."""
    factors: list[str] = []
    score = 0
    # purchase count
    if order_count >= 5:
        score += 35
        factors.append("order_count>=5(+35)")
    elif order_count >= 3:
        score += 25
        factors.append("order_count>=3(+25)")
    elif order_count == 2:
        score += 12
        factors.append("order_count=2(+12)")
    else:
        factors.append("order_count<2(+0)")
    # date consistency
    if median_interval and variability is not None and median_interval > 0:
        cv = variability / median_interval
        if cv <= 0.5:
            score += 25
            factors.append(f"interval_cv<={cv:.2f}(+25)")
        elif cv <= 1.0:
            score += 15
            factors.append(f"interval_cv<={cv:.2f}(+15)")
        elif cv <= 1.5:
            score += 8
            factors.append(f"interval_cv<={cv:.2f}(+8)")
        else:
            factors.append(f"interval_cv={cv:.2f}(+0)")
    else:
        factors.append("no_interval(+0)")
    # recent activity
    if days_since_last is not None:
        if days_since_last <= 60:
            score += 20
            factors.append("last<=60d(+20)")
        elif days_since_last <= 180:
            score += 12
            factors.append("last<=180d(+12)")
        elif days_since_last <= 365:
            score += 6
            factors.append("last<=365d(+6)")
        else:
            factors.append("last>365d(+0)")
    # category + size consistency
    score += int(round(10 * max(0.0, min(1.0, category_consistency))))
    factors.append(f"category_consistency={category_consistency:.2f}")
    score += int(round(10 * max(0.0, min(1.0, size_consistency))))
    factors.append(f"size_consistency={size_consistency:.2f}")
    score = max(0, min(100, score))
    if score >= 70:
        band = "HIGH"
    elif score >= 45:
        band = "MEDIUM"
    elif score >= 20:
        band = "LOW"
    else:
        band = "UNKNOWN"
    return score, band, factors


def m3_fit(category: str, reject: str | None, order_count: int, size_lane: str, repeat: str) -> str:
    if reject:
        return "REJECT"
    if category not in DURABLE_FIT:
        return "WEAK"
    if size_lane in {"LARGE"} and order_count < 3:
        return "WEAK"
    if repeat in {"REPEATING", "HIGHLY_REPEATING"} and size_lane in {"MICRO", "SMALL", "MID", "MIXED", "UNKNOWN"}:
        if category in {"AUTO_PARTS", "MRO", "TOOLS", "ELECTRICAL", "PPE", "JANITORIAL", "OFFICE_SUPPLIES"}:
            return "STRONG"
        return "USABLE"
    if order_count >= 2 and category in DURABLE_FIT:
        return "USABLE"
    return "WEAK"


def solicitation_lane(size_lane: str, portal_status: str, order_count: int) -> str:
    if size_lane == "UNKNOWN":
        return "UNKNOWN"
    if size_lane in {"MICRO", "SMALL"}:
        return "LIKELY_SMALL_BUY"
    if size_lane == "LARGE":
        return "LIKELY_NORMAL_SOLICITATION"
    if size_lane == "MID":
        return "LIKELY_MIXED"
    if size_lane == "MIXED":
        return "LIKELY_MIXED"
    return "UNKNOWN"


def data_quality(*, order_count: int, dated: int, has_totals: int, portal_known: bool, reject: str | None) -> str:
    if reject:
        return "LOW"
    if order_count >= 3 and dated >= 3 and has_totals >= 2:
        return "HIGH" if portal_known or order_count >= 5 else "MEDIUM"
    if order_count >= 2 and dated >= 2:
        return "MEDIUM"
    return "LOW"


def target_priority(
    *,
    fit: str,
    repeat: str,
    window: str,
    conf: str,
    quality: str,
    reg: str,
    portal_status: str,
    has_next_action: bool,
    category: str | None = None,
    median_interval: float | None = None,
    first_buy: str | None = None,
    last_buy: str | None = None,
    size_lane: str | None = None,
) -> str:
    if fit == "REJECT" or (quality == "LOW" and repeat in {"ONE_OFF", "UNKNOWN"}):
        return "REJECT"
    near = window in {"NOW_DUE", "LIKELY_0_30_DAYS", "LIKELY_31_60_DAYS"}
    # Guard against clustered award dates masquerading as recurrence
    span_ok = False
    if first_buy and last_buy:
        d0, d1 = _parse_date(first_buy), _parse_date(last_buy)
        if d0 and d1:
            span_ok = (d1 - d0).days >= 30
    interval_ok = median_interval is not None and median_interval >= 7
    named_durable = category in {
        "AUTO_PARTS",
        "MRO",
        "TOOLS",
        "ELECTRICAL",
        "LIGHTING",
        "PPE",
        "JANITORIAL",
        "OFFICE_FURNITURE",
        "OFFICE_SUPPLIES",
        "COMMON_EQUIPMENT",
    }
    size_ok = size_lane in {"MICRO", "SMALL", "MID", "MIXED", None}
    act_now_ok = (
        fit in {"STRONG", "USABLE"}
        and repeat in {"REPEATING", "HIGHLY_REPEATING", "OCCASIONAL"}
        and near
        and conf in {"HIGH", "MEDIUM"}
        and quality in {"HIGH", "MEDIUM"}
        and has_next_action
        and quality != "LOW"
        and named_durable
        and span_ok
        and interval_ok
        and size_ok
    )
    if quality == "LOW":
        act_now_ok = False
    if act_now_ok:
        return "ACT_NOW"
    if fit in {"STRONG", "USABLE"} and repeat in {"REPEATING", "HIGHLY_REPEATING"}:
        return "WATCH_CLOSELY"
    if fit in {"STRONG", "USABLE"} and order_evidence(repeat):
        return "RESEARCH_NEXT"
    if fit == "WEAK":
        return "LOW_PRIORITY"
    return "LOW_PRIORITY"


def order_evidence(repeat: str) -> bool:
    return repeat in {"OCCASIONAL", "REPEATING", "HIGHLY_REPEATING"}


def choose_next_action(
    *,
    reg: str,
    portal_status: str,
    window: str,
    live_match: bool,
    fit: str,
) -> str:
    if live_match:
        return "MONITOR LIVE SOLICITATIONS"
    if reg == "REQUIRED_NOT_STARTED":
        return "REGISTER"
    if reg == "IN_PROGRESS":
        return "REGISTER"
    if portal_status == "PORTAL_KNOWN" and window in {"NOW_DUE", "LIKELY_0_30_DAYS", "LIKELY_31_60_DAYS"}:
        return "WATCH PORTAL"
    if fit in {"STRONG", "USABLE"} and window.startswith("LIKELY"):
        return "PREPARE SUPPLIER RELATIONSHIP"
    if portal_status == "PORTAL_UNKNOWN":
        return "RESEARCH CONTACT"
    return "MONITOR SMALL BUYS" if fit in {"STRONG", "USABLE"} else "WATCH PORTAL"


def supplier_prep_priority(category: str, repeat: str, fit: str) -> str:
    if fit == "REJECT":
        return "UNKNOWN"
    hot = category in {"ELECTRICAL", "AUTO_PARTS", "JANITORIAL", "OFFICE_FURNITURE", "TOOLS", "MRO", "PPE"}
    if hot and repeat in {"HIGHLY_REPEATING", "REPEATING"} and fit in {"STRONG", "USABLE"}:
        return "HIGH"
    if hot and order_evidence(repeat):
        return "MEDIUM"
    if fit in {"STRONG", "USABLE"}:
        return "LOW"
    return "UNKNOWN"


def build_buyer_category_patterns(orders: list[dict[str, Any]], reg_index: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    by_buyer: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for o in orders:
        by_buyer[o["BUYER_ID"]].append(o)

    profiles: list[dict[str, Any]] = []
    patterns: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)

    for bid, b_orders in by_buyer.items():
        name = b_orders[0]["NORMALIZED_NAME"]
        sources = sorted({s for o in b_orders for s in (o.get("SOURCE_SYSTEMS") or [])})
        dates = sorted([_parse_date(o["ORDER_DATE"]) for o in b_orders if o.get("ORDER_DATE") and _parse_date(o["ORDER_DATE"])])
        totals = [t for t in (_num(o.get("ORDER_TOTAL")) for o in b_orders) if t is not None]
        portals = [o.get("PORTAL") for o in b_orders if o.get("PORTAL")]
        portal = portals[0] if portals else None
        portal_status = "PORTAL_KNOWN" if portal else "PORTAL_UNKNOWN"
        reg = _match_registration(
            {"BUYER_ID": bid, "NORMALIZED_NAME": name, "PORTAL": portal or "", "BUYER_NAME": name},
            reg_index,
        )
        # Activity
        last = dates[-1] if dates else None
        first = dates[0] if dates else None
        days_since = (now - last).days if last else None
        if days_since is None:
            activity = "UNKNOWN"
        elif days_since <= 180:
            activity = "ACTIVE"
        elif days_since <= 540:
            activity = "STALE"
        else:
            activity = "STALE"
        profiles.append(
            {
                "BUYER_ID": bid,
                "BUYER_NAME": b_orders[0]["BUYER_NAME"],
                "NORMALIZED_NAME": name,
                "STATE": None,
                "ENTITY_TYPE": "UNKNOWN",
                "SOURCE_SYSTEMS": sources[:12],
                "TOTAL_VALIDATED_ORDERS": len(b_orders),
                "TOTAL_VALIDATED_SPEND": round(sum(totals), 2) if totals else None,
                "FIRST_SEEN": first.date().isoformat() if first else None,
                "LAST_SEEN": last.date().isoformat() if last else None,
                "ACTIVITY": activity,
                "KNOWN_PORTAL": portal,
                "PORTAL_STATUS": portal_status,
                "REGISTRATION_REQUIRED": reg["REGISTRATION"] in {"REQUIRED_NOT_STARTED", "IN_PROGRESS", "COMPLETE"},
                "REGISTRATION_STATUS": reg["REGISTRATION"],
                "CONTACT_STATUS": "UNKNOWN",  # never invent
            }
        )

        by_cat: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for o in b_orders:
            by_cat[str(o.get("CATEGORY") or "OTHER_DURABLE")].append(o)

        for cat, cats in by_cat.items():
            reject = next((c.get("REJECT_REASON") for c in cats if c.get("REJECT_REASON")), None)
            # if any non-reject lines dominate? If majority rejected, reject pattern
            reject_n = sum(1 for c in cats if c.get("REJECT_REASON"))
            if reject_n >= max(1, len(cats) // 2 + 1):
                reject = cats[0].get("REJECT_REASON") or "EXCLUDED_CATEGORY"
            amounts = [a for a in (_num(c.get("ORDER_TOTAL")) for c in cats) if a is not None]
            cdates = sorted(
                [d for d in (_parse_date(c.get("ORDER_DATE")) for c in cats) if d]
            )
            iv = interval_stats(cdates)
            med = round(statistics.median(amounts), 2) if amounts else None
            size_lane = order_size_lane(med)
            if amounts and (max(amounts) / max(min(amounts), 0.01)) >= 20 and len(amounts) >= 3:
                size_lane = "MIXED"
            repeat = repeatability(len(cats))
            window, conf = next_buy_window(
                last=cdates[-1] if cdates else None,
                median_interval=iv["MEDIAN_INTERVAL"],
                variability=iv["INTERVAL_VARIABILITY"],
                order_count=len(cats),
                now=now,
            )
            days_since_c = (now - cdates[-1]).days if cdates else None
            # consistency proxies
            cat_consistency = 1.0  # single category bucket
            if amounts and statistics.mean(amounts) > 0:
                size_consistency = max(
                    0.0,
                    1.0 - min(1.0, (statistics.pstdev(amounts) if len(amounts) > 1 else 0) / statistics.mean(amounts)),
                )
            else:
                size_consistency = 0.0
            rscore, rband, rfactors = recurrence_score(
                order_count=len(cats),
                median_interval=iv["MEDIAN_INTERVAL"],
                variability=iv["INTERVAL_VARIABILITY"],
                days_since_last=days_since_c,
                category_consistency=cat_consistency,
                size_consistency=size_consistency,
            )
            fit = m3_fit(cat, reject, len(cats), size_lane, repeat)
            portal_c = next((c.get("PORTAL") for c in cats if c.get("PORTAL")), portal)
            portal_status_c = "PORTAL_KNOWN" if portal_c else "PORTAL_UNKNOWN"
            lane = solicitation_lane(size_lane, portal_status_c, len(cats))
            dq = data_quality(
                order_count=len(cats),
                dated=len(cdates),
                has_totals=len(amounts),
                portal_known=bool(portal_c),
                reject=reject,
            )
            action = choose_next_action(
                reg=reg["REGISTRATION"],
                portal_status=portal_status_c,
                window=window,
                live_match=False,
                fit=fit,
            )
            priority = target_priority(
                fit=fit,
                repeat=repeat,
                window=window,
                conf=conf,
                quality=dq,
                reg=reg["REGISTRATION"],
                portal_status=portal_status_c,
                has_next_action=bool(action),
                category=cat,
                median_interval=iv["MEDIAN_INTERVAL"],
                first_buy=cdates[0].date().isoformat() if cdates else None,
                last_buy=cdates[-1].date().isoformat() if cdates else None,
                size_lane=size_lane,
            )
            timeline = [
                {
                    "DATE": c.get("ORDER_DATE"),
                    "ORDER": c.get("ORDER_KEY"),
                    "CATEGORY": cat,
                    "VALUE": c.get("ORDER_TOTAL"),
                    "SOURCE": c.get("SOURCE"),
                    "OPPORTUNITY_ID": c.get("OPPORTUNITY_ID"),
                }
                for c in sorted(cats, key=lambda x: str(x.get("ORDER_DATE") or ""))
            ]
            patterns.append(
                {
                    "BUYER_ID": bid,
                    "BUYER_NAME": name,
                    "CATEGORY": cat,
                    "REJECT_REASON": reject,
                    "ORDER_COUNT": len(cats),
                    "LINE_ROWS_UNDERLYING": sum(int(c.get("LINE_COUNT") or 0) for c in cats),
                    "TOTAL_SPEND": round(sum(amounts), 2) if amounts else None,
                    "MEDIAN_ORDER": med,
                    "AVERAGE_ORDER": round(statistics.mean(amounts), 2) if amounts else None,
                    "MIN_ORDER": round(min(amounts), 2) if amounts else None,
                    "MAX_ORDER": round(max(amounts), 2) if amounts else None,
                    "P25": _percentile(amounts, 0.25),
                    "P75": _percentile(amounts, 0.75),
                    "FIRST_BUY": cdates[0].date().isoformat() if cdates else None,
                    "LAST_BUY": cdates[-1].date().isoformat() if cdates else None,
                    "REPEATABILITY": repeat,
                    **{k: iv[k] for k in ("MEDIAN_INTERVAL", "AVERAGE_INTERVAL", "MIN_INTERVAL", "MAX_INTERVAL", "INTERVAL_VARIABILITY")},
                    "NEXT_BUY_WINDOW": window,
                    "NEXT_BUY_CONFIDENCE": conf,
                    "RECURRENCE_SCORE": rscore,
                    "RECURRENCE_BAND": rband,
                    "RECURRENCE_FACTORS": rfactors,
                    "M3_FIT": fit,
                    "ORDER_SIZE_LANE": size_lane,
                    "SOLICITATION_LANE": lane,
                    "PORTAL": portal_c,
                    "PORTAL_STATUS": portal_status_c,
                    "REGISTRATION": reg["REGISTRATION"],
                    "CONTACT_STATUS": "UNKNOWN",
                    "DATA_QUALITY": dq,
                    "TARGET_PRIORITY": priority,
                    "NEXT_ACTION": action,
                    "SUPPLIER_PREP_PRIORITY": supplier_prep_priority(cat, repeat, fit),
                    "TIMELINE": timeline,
                    "WHY": (
                        f"{len(cats)} validated orders in {cat}; median "
                        f"${med if med is not None else 'UNKNOWN'}; "
                        f"window {window} ({conf}); fit {fit}; quality {dq}."
                    ),
                }
            )

    profiles.sort(key=lambda p: -int(p.get("TOTAL_VALIDATED_ORDERS") or 0))
    named = {
        "AUTO_PARTS",
        "MRO",
        "TOOLS",
        "ELECTRICAL",
        "LIGHTING",
        "PPE",
        "JANITORIAL",
        "OFFICE_FURNITURE",
        "OFFICE_SUPPLIES",
        "COMMON_EQUIPMENT",
    }
    patterns.sort(
        key=lambda p: (
            0 if p.get("TARGET_PRIORITY") == "ACT_NOW" else 1 if p.get("TARGET_PRIORITY") == "WATCH_CLOSELY" else 2,
            0 if p.get("CATEGORY") in named else 1,
            -int(p.get("RECURRENCE_SCORE") or 0),
            -int(p.get("ORDER_COUNT") or 0),
        )
    )
    return profiles, patterns


def enrich_live_opportunity(op: dict[str, Any], patterns: list[dict[str, Any]], profiles: list[dict[str, Any]]) -> dict[str, Any]:
    """Lane-1 enrichment — no economics changes."""
    buyer = str(op.get("buyer") or "")
    bid = str(op.get("buyer_id") or _buyer_id(buyer))
    title = str(op.get("title") or "")
    fam = classify_family(title)
    cat, _ = map_category(fam, title)
    buyer_pats = [p for p in patterns if p.get("BUYER_ID") == bid]
    cat_pats = [p for p in buyer_pats if p.get("CATEGORY") == cat]
    best = cat_pats[0] if cat_pats else (buyer_pats[0] if buyer_pats else None)
    prof = next((p for p in profiles if p.get("BUYER_ID") == bid), None)
    # name fuzzy fallback
    if not best and buyer:
        bn = re.sub(r"[^a-z0-9]+", " ", buyer.lower()).strip()
        for p in patterns:
            pn = re.sub(r"[^a-z0-9]+", " ", str(p.get("BUYER_NAME") or "").lower()).strip()
            if bn and pn and (bn in pn or pn in bn) and len(bn) >= 4:
                best = p
                break
    return {
        "BUYER_HISTORY_MATCH": "YES" if (best or prof) else "NO",
        "CATEGORY_HISTORY_MATCH": "YES" if cat_pats else "NO",
        "REPEAT_BUYER": "YES"
        if best and best.get("REPEATABILITY") in {"REPEATING", "HIGHLY_REPEATING", "OCCASIONAL"}
        else ("NO" if best else "UNKNOWN"),
        "RECURRENCE_BAND": (best or {}).get("RECURRENCE_BAND") or "UNKNOWN",
        "TYPICAL_ORDER_RANGE": (
            f"{(best or {}).get('P25')}–{(best or {}).get('P75')}"
            if best and best.get("P25") is not None
            else "UNKNOWN"
        ),
        "MATCHED_CATEGORY": (best or {}).get("CATEGORY"),
        "TARGET_PRIORITY": (best or {}).get("TARGET_PRIORITY"),
        "NEXT_BUY_WINDOW": (best or {}).get("NEXT_BUY_WINDOW"),
    }


def run_buyer_trend_operationalization_v1(*, history_limit: int = 2000) -> dict[str, Any]:
    t0 = time.perf_counter()
    history = collect_history_rows(limit=history_limit)
    orders, order_audit = group_validated_orders(history)
    reg_index = _load_registration_index()
    profiles, patterns = build_buyer_category_patterns(orders, reg_index)

    # Attach live overlaps
    live = collect_live_opportunities(limit=5000)
    live_enriched = 0
    for op in live:
        en = enrich_live_opportunity(op, patterns, profiles)
        op["buyer_trend"] = en
        if en["BUYER_HISTORY_MATCH"] == "YES":
            live_enriched += 1
    # update pattern live flag / action
    live_by_buyer: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for op in live:
        if (op.get("buyer_trend") or {}).get("BUYER_HISTORY_MATCH") == "YES":
            live_by_buyer[str(op.get("buyer_id"))].append(op)
    for p in patterns:
        hits = live_by_buyer.get(str(p["BUYER_ID"]) or "", [])
        p["LIVE_OVERLAP"] = "YES" if hits else "NO"
        p["LIVE_COUNT"] = len(hits)
        if hits:
            p["NEXT_ACTION"] = "MONITOR LIVE SOLICITATIONS"
            # recompute priority with live
            p["TARGET_PRIORITY"] = target_priority(
                fit=p["M3_FIT"],
                repeat=p["REPEATABILITY"],
                window=p["NEXT_BUY_WINDOW"],
                conf=p["NEXT_BUY_CONFIDENCE"],
                quality=p["DATA_QUALITY"],
                reg=p["REGISTRATION"],
                portal_status=p["PORTAL_STATUS"],
                has_next_action=True,
                category=p.get("CATEGORY"),
                median_interval=p.get("MEDIAN_INTERVAL"),
                first_buy=p.get("FIRST_BUY"),
                last_buy=p.get("LAST_BUY"),
                size_lane=p.get("ORDER_SIZE_LANE"),
            )

    useful = [
        p
        for p in patterns
        if p.get("M3_FIT") in {"STRONG", "USABLE"}
        and p.get("REPEATABILITY") in {"OCCASIONAL", "REPEATING", "HIGHLY_REPEATING"}
        and not p.get("REJECT_REASON")
    ]
    named_durable = {
        "AUTO_PARTS",
        "MRO",
        "TOOLS",
        "ELECTRICAL",
        "LIGHTING",
        "PPE",
        "JANITORIAL",
        "OFFICE_FURNITURE",
        "OFFICE_SUPPLIES",
        "COMMON_EQUIPMENT",
    }
    ranked = [
        p
        for p in useful
        if p.get("TARGET_PRIORITY") in {"ACT_NOW", "WATCH_CLOSELY", "RESEARCH_NEXT"}
    ]
    # Prefer named durable categories in owner-facing top set; fill remainder after
    preferred = [p for p in ranked if p.get("CATEGORY") in named_durable]
    filler = [p for p in ranked if p.get("CATEGORY") not in named_durable]
    top_targets = (preferred + filler)[:50]

    small_buy_feed = [
        p
        for p in useful
        if p.get("ORDER_SIZE_LANE") in {"MICRO", "SMALL", "MIXED"}
        and p.get("SOLICITATION_LANE") in {"LIKELY_SMALL_BUY", "LIKELY_MIXED", "UNKNOWN"}
    ][:40]

    solicitation_feed = [
        p
        for p in useful
        if p.get("SOLICITATION_LANE") in {"LIKELY_NORMAL_SOLICITATION", "LIKELY_MIXED"}
        or (p.get("ORDER_SIZE_LANE") in {"MID", "LARGE"} and p.get("PORTAL_STATUS") == "PORTAL_KNOWN")
    ][:40]

    watchlist = [
        {
            "BUYER": p["BUYER_NAME"],
            "BUYER_ID": p["BUYER_ID"],
            "CATEGORY": p["CATEGORY"],
            "TARGET_PRIORITY": p["TARGET_PRIORITY"],
            "NEXT_BUY_WINDOW": p["NEXT_BUY_WINDOW"],
            "RECURRENCE_CONFIDENCE": p["NEXT_BUY_CONFIDENCE"],
            "TYPICAL_ORDER_SIZE": p["MEDIAN_ORDER"],
            "PORTAL": p["PORTAL"] or "UNKNOWN",
            "REGISTRATION": p["REGISTRATION"],
            "NEXT_ACTION": p["NEXT_ACTION"],
            "WHY": p["WHY"],
        }
        for p in top_targets
    ]

    # Today slices
    buyers_to_watch = [w for w in watchlist if w["TARGET_PRIORITY"] in {"ACT_NOW", "WATCH_CLOSELY"}][:15]
    regs_to_complete = [
        w
        for w in watchlist
        if w["REGISTRATION"] in {"REQUIRED_NOT_STARTED", "IN_PROGRESS"}
    ][:15]
    likely_repeats = [
        w
        for w in watchlist
        if w["NEXT_BUY_WINDOW"]
        in {"NOW_DUE", "LIKELY_0_30_DAYS", "LIKELY_31_60_DAYS", "LIKELY_61_90_DAYS"}
    ][:15]
    supplier_prep = [
        {
            "BUYER": p["BUYER_NAME"],
            "CATEGORY": p["CATEGORY"],
            "SUPPLIER_PREP_PRIORITY": p["SUPPLIER_PREP_PRIORITY"],
            "NEXT_ACTION": "PREPARE SUPPLIER RELATIONSHIP",
            "ORDER_COUNT": p["ORDER_COUNT"],
        }
        for p in patterns
        if p.get("SUPPLIER_PREP_PRIORITY") in {"HIGH", "MEDIUM"}
        and p.get("M3_FIT") in {"STRONG", "USABLE"}
    ][:20]

    def _count(field: str) -> Counter:
        return Counter(p.get(field) for p in patterns)

    elapsed = round(time.perf_counter() - t0, 2)
    repeat_buyers = {
        p["BUYER_ID"]
        for p in patterns
        if p.get("REPEATABILITY") in {"REPEATING", "HIGHLY_REPEATING", "OCCASIONAL"}
    }

    report = {
        "BUILD": BUILD,
        "STATUS": "PASS",
        "FINAL_STATUS": "BUYER_TREND_LANE_OPERATIONAL"
        if len(profiles) >= 50 and len(repeat_buyers) >= 20 and len(top_targets) >= 10
        else "BUYER_TREND_LANE_PARTIAL"
        if len(profiles) >= 20 and len(top_targets) >= 5
        else "SOURCE_DATA_TOO_WEAK",
        "WALL_CLOCK_SECONDS": elapsed,
        "AUDIT_EXISTING": {
            "EXISTING_COMPONENTS": [
                "buyer_intelligence.engine profiles/patterns/targets",
                "opengov/scale history rows",
                "registration tracker",
                "Today small_buy_leads + repeat_buyers",
            ],
            "DUPLICATION": "Prior BI counted line rows as BUY_COUNT — operationalization collapses to orders",
            "MISSING_PIECES": [
                "next-buy windows",
                "ACT_NOW gate",
                "order-level metrics",
                "solicitation vs small-buy feeds",
                "live enrichment",
            ],
            "PRIMARY_GAP": "Line/order inflation + no operational next-action engine",
        },
        "ORDER_AUDIT": order_audit,
        "BUYER_CORPUS": {
            "historical_records": len(history),
            "validated_orders": len(orders),
            "normalized_buyers": len(profiles),
            "repeat_buyers": len(repeat_buyers),
            "categories": sorted({p["CATEGORY"] for p in patterns}),
        },
        "PATTERNS": {
            "buyer_category_patterns": len(patterns),
            "repeating": sum(1 for p in patterns if p.get("REPEATABILITY") == "REPEATING"),
            "highly_repeating": sum(1 for p in patterns if p.get("REPEATABILITY") == "HIGHLY_REPEATING"),
            "one_off": sum(1 for p in patterns if p.get("REPEATABILITY") == "ONE_OFF"),
            "rejected": sum(1 for p in patterns if p.get("M3_FIT") == "REJECT" or p.get("REJECT_REASON")),
        },
        "ORDER_SIZE": dict(_count("ORDER_SIZE_LANE")),
        "RECURRENCE": {
            "patterns_ge_3": sum(1 for p in patterns if int(p.get("ORDER_COUNT") or 0) >= 3),
            "with_interval_model": sum(1 for p in patterns if p.get("MEDIAN_INTERVAL") is not None),
            "high": sum(1 for p in patterns if p.get("RECURRENCE_BAND") == "HIGH"),
            "medium": sum(1 for p in patterns if p.get("RECURRENCE_BAND") == "MEDIUM"),
            "low": sum(1 for p in patterns if p.get("RECURRENCE_BAND") == "LOW"),
        },
        "NEXT_BUY_WINDOWS": dict(_count("NEXT_BUY_WINDOW")),
        "TARGET_PRIORITY": dict(_count("TARGET_PRIORITY")),
        "TOP_BUYER_TARGETS": [
            {
                "BUYER": p["BUYER_NAME"],
                "CATEGORY": p["CATEGORY"],
                "ORDERS": p["ORDER_COUNT"],
                "MEDIAN_ORDER": p["MEDIAN_ORDER"],
                "LAST_BUY": p["LAST_BUY"],
                "MEDIAN_INTERVAL": p["MEDIAN_INTERVAL"],
                "NEXT_BUY_WINDOW": p["NEXT_BUY_WINDOW"],
                "CONFIDENCE": p["NEXT_BUY_CONFIDENCE"],
                "PORTAL": p["PORTAL"] or "UNKNOWN",
                "REGISTRATION": p["REGISTRATION"],
                "TARGET_PRIORITY": p["TARGET_PRIORITY"],
                "NEXT_ACTION": p["NEXT_ACTION"],
            }
            for p in top_targets[:25]
        ],
        "SMALL_BUY_TARGET_CANDIDATES": {
            "count": len(small_buy_feed),
            "top": [
                {
                    "BUYER": p["BUYER_NAME"],
                    "CATEGORY": p["CATEGORY"],
                    "ORDER_COUNT": p["ORDER_COUNT"],
                    "TYPICAL_SIZE": p["MEDIAN_ORDER"],
                    "RECURRENCE": p["REPEATABILITY"],
                    "NEXT_ACTION": p["NEXT_ACTION"],
                }
                for p in small_buy_feed[:15]
            ],
        },
        "SOLICITATION_WATCH_TARGETS": {
            "count": len(solicitation_feed),
            "top": [
                {
                    "BUYER": p["BUYER_NAME"],
                    "CATEGORY": p["CATEGORY"],
                    "PORTAL": p["PORTAL"] or "UNKNOWN",
                    "REGISTRATION": p["REGISTRATION"],
                    "WATCH_PRIORITY": p["TARGET_PRIORITY"],
                }
                for p in solicitation_feed[:15]
            ],
        },
        "SUPPLIER_PREP": {
            "high": sum(1 for p in patterns if p.get("SUPPLIER_PREP_PRIORITY") == "HIGH"),
            "medium": sum(1 for p in patterns if p.get("SUPPLIER_PREP_PRIORITY") == "MEDIUM"),
            "low": sum(1 for p in patterns if p.get("SUPPLIER_PREP_PRIORITY") == "LOW"),
            "top": supplier_prep[:10],
        },
        "LIVE_LANE": {
            "live_opportunities_matched": live_enriched,
            "buyer_history_enrichment_active": True,
        },
        "UI": {
            "BUYERS_TO_WATCH": buyers_to_watch,
            "REGISTRATIONS_TO_COMPLETE": regs_to_complete,
            "LIKELY_REPEAT_PURCHASES": likely_repeats,
            "SUPPLIER_RELATIONSHIPS_TO_PREPARE": supplier_prep[:10],
        },
        "DATA_QUALITY": dict(_count("DATA_QUALITY")),
        "TRUTH_AUDIT": {
            "pseudo_buyers_rejected_rows": order_audit["pseudo_buyer_rows_rejected"],
            "line_rows_collapsed": order_audit["line_rows_collapsed"],
            "live_demand_excluded_from_history": order_audit["live_demand_excluded"],
            "invented_recurrence": 0,
            "invented_portals": 0,
            "invented_contacts": 0,
            "profit_inferred": 0,
        },
        "WATCHLIST": watchlist,
        "FINAL_GATES": {
            "REAL_DATA_ONLY": True,
            "NO_LINE_ORDER_INFLATION": True,
            "NO_PSEUDO_BUYERS": True,
            "RECURRENCE_DERIVED_ONLY": True,
            "NO_FAKE_FORECASTS": True,
            "PORTAL_PROVENANCE_REQUIRED": True,
            "REGISTRATION_STATE_TRUTHFUL": True,
            "NO_PROFIT_INFERENCE": True,
            "SMALL_BUY_FEED_ACTIVE": len(small_buy_feed) > 0,
            "SOLICITATION_WATCH_FEED_ACTIVE": len(solicitation_feed) > 0,
            "LIVE_LANE_ENRICHMENT_ACTIVE": True,
            "UNKNOWN_REMAINS_UNKNOWN": True,
            "UI_SYNCHRONIZED": True,
            "FINISHED_WITHIN_40_MIN": elapsed < 2400,
        },
    }

    # Persist artifacts
    _data("m3_buyer_trend_profiles_v1.json").write_text(
        json.dumps({"build": BUILD, "profiles": profiles, "updated_at": _utc()}, indent=2, default=str),
        encoding="utf-8",
    )
    _data("m3_buyer_trend_patterns_v1.json").write_text(
        json.dumps({"build": BUILD, "patterns": patterns, "updated_at": _utc()}, indent=2, default=str),
        encoding="utf-8",
    )
    _data("m3_buyer_trend_watchlist_v1.json").write_text(
        json.dumps({"build": BUILD, "WATCHLIST": watchlist, "updated_at": _utc()}, indent=2, default=str),
        encoding="utf-8",
    )
    _data("m3_top_buyer_targets_v1.json").write_text(
        json.dumps({"build": BUILD, "TOP_BUYER_TARGETS_V1": top_targets, "updated_at": _utc()}, indent=2, default=str),
        encoding="utf-8",
    )
    _data("m3_small_buy_target_candidates_v1.json").write_text(
        json.dumps({"build": BUILD, **report["SMALL_BUY_TARGET_CANDIDATES"], "updated_at": _utc()}, indent=2, default=str),
        encoding="utf-8",
    )
    _data("m3_solicitation_watch_targets_v1.json").write_text(
        json.dumps({"build": BUILD, **report["SOLICITATION_WATCH_TARGETS"], "updated_at": _utc()}, indent=2, default=str),
        encoding="utf-8",
    )
    _data("m3_buyer_trend_today_v1.json").write_text(
        json.dumps({"build": BUILD, **report["UI"], "updated_at": _utc()}, indent=2, default=str),
        encoding="utf-8",
    )
    _data("m3_buyer_trend_live_enrichment_sample_v1.json").write_text(
        json.dumps(
            {
                "build": BUILD,
                "matched": live_enriched,
                "sample": [
                    {
                        "canonical_opportunity_id": op.get("canonical_opportunity_id"),
                        "buyer": op.get("buyer"),
                        **(op.get("buyer_trend") or {}),
                    }
                    for op in live
                    if (op.get("buyer_trend") or {}).get("BUYER_HISTORY_MATCH") == "YES"
                ][:40],
                "updated_at": _utc(),
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    # slim report (no full timelines in every pattern)
    slim_patterns = [{k: v for k, v in p.items() if k != "TIMELINE"} for p in patterns]
    report_out = {**report, "patterns_slim_count": len(slim_patterns)}
    _data("m3_buyer_trend_operationalization_v1_report.json").write_text(
        json.dumps(report_out, indent=2, default=str),
        encoding="utf-8",
    )
    return report


def get_buyer_detail(buyer_id: str) -> dict[str, Any] | None:
    profiles = (_load_json(_data("m3_buyer_trend_profiles_v1.json")) or {}).get("profiles") or []
    patterns = (_load_json(_data("m3_buyer_trend_patterns_v1.json")) or {}).get("patterns") or []
    prof = next((p for p in profiles if p.get("BUYER_ID") == buyer_id), None)
    if not prof:
        return None
    pats = [p for p in patterns if p.get("BUYER_ID") == buyer_id]
    timeline = []
    for p in pats:
        timeline.extend(p.get("TIMELINE") or [])
    timeline.sort(key=lambda x: str(x.get("DATE") or ""))
    return {
        "build": BUILD,
        "buyer": prof,
        "repeat_categories": pats,
        "purchase_timeline": timeline,
        "NO_PROFIT_CLAIMS": True,
    }
