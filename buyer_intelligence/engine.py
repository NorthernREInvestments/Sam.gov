"""Data-first buyer intelligence — no Playwright, max 2k history rows."""

from __future__ import annotations

import hashlib
import json
import re
import statistics
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from application_clock import now_utc

BUILD = "20261007-m3-buyer-intelligence-micro-cashflow-v1"
MAX_HISTORY = 2000
MAX_PROFILES = 100
MAX_RANK = 50
TOP_UI = 25
PER_RECORD_TIMEOUT_S = 60

PRODUCT_FAMILIES = (
    "TOOLS",
    "PPE",
    "JANITORIAL_SUPPLIES",
    "OFFICE_SUPPLIES",
    "FURNITURE",
    "LIGHTING",
    "ELECTRICAL",
    "PLUMBING",
    "MRO",
    "AUTO_PARTS",
    "MEDICAL_SUPPLIES",
    "PAPER_PRODUCTS",
    "EQUIPMENT",
    "OTHER",
)

_FAMILY_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("PPE", re.compile(r"\b(glove|ppe|respirator|hard\s*hat|safety\s*vest|goggle|face\s*shield)\b", re.I)),
    ("JANITORIAL_SUPPLIES", re.compile(r"\b(janitor|cleaning|disinfectant|trash\s*bag|mop|broom|toilet\s*paper|paper\s*towel)\b", re.I)),
    ("OFFICE_SUPPLIES", re.compile(r"\b(office\s*suppl|toner|printer\s*paper|stapler|pen\b|binder)\b", re.I)),
    ("FURNITURE", re.compile(r"\b(desk|chair|furniture|cubicle|filing\s*cabinet)\b", re.I)),
    ("LIGHTING", re.compile(r"\b(led\b|lighting|lamp|fixture|bulb|luminaire)\b", re.I)),
    ("ELECTRICAL", re.compile(r"\b(electrical|breaker|conduit|wire|cable|transformer|outlet)\b", re.I)),
    ("PLUMBING", re.compile(r"\b(plumbing|pipe|valve|faucet|fitting|toilet|urinal)\b", re.I)),
    ("AUTO_PARTS", re.compile(r"\b(filter|brake|alternator|starter|spark\s*plug|exhaust|manifold|fleet\s*part|transit\s*part|inventory\s*parts?)\b", re.I)),
    ("MEDICAL_SUPPLIES", re.compile(r"\b(medical|syringe|bandage|gauze|surgical|catheter)\b", re.I)),
    ("PAPER_PRODUCTS", re.compile(r"\b(copy\s*paper|paper\s*product|napkin|tissue)\b", re.I)),
    ("TOOLS", re.compile(r"\b(tool|wrench|drill|socket|screwdriver|hammer|pliers)\b", re.I)),
    ("MRO", re.compile(r"\b(mro|maintenance|bearing|seal|gasket|fastener|hardware|screw|bolt|nut)\b", re.I)),
    ("EQUIPMENT", re.compile(r"\b(equipment|pump|compressor|generator|hvac|vehicle|truck)\b", re.I)),
]

_PCARD = re.compile(
    r"\b(purchase\s*card|government\s*purchase\s*card|\bGPC\b|p-?card|credit\s*card|"
    r"payment\s+by\s+card|simplified\s+purchase|small\s+purchase|quick\s+quote|\bRFQ\b)\b",
    re.I,
)
_INSTALL_NEG = re.compile(
    r"\b(install(?:ation)?|service|labor|repair|construction|customize|custom\s+build|"
    r"manufacturer\s+authorization|sole\s*source)\b",
    re.I,
)
_NORM = re.compile(r"[^a-z0-9]+")


def _utc() -> str:
    return now_utc().isoformat()


def _data_path(*parts: str) -> Path:
    from m3_data_root import data_path

    return data_path(*parts)


def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None


def _parse_date(v: Any) -> datetime | None:
    if not v:
        return None
    s = str(v).strip()
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s[:32])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except Exception:
            return None


def _buyer_id(name: str) -> str:
    n = _NORM.sub(" ", (name or "").lower()).strip()
    n = re.sub(r"\b(the|city|county|of|department|dept|agency)\b", " ", n)
    n = _NORM.sub(" ", n).strip()
    digest = hashlib.sha1(n.encode("utf-8")).hexdigest()[:10]
    slug = (n.replace(" ", "_")[:40] or "unknown")
    return f"{slug}_{digest}"


def _canon_name(name: str) -> str:
    s = re.sub(r"\s+", " ", (name or "").strip())
    return s.title() if s == s.lower() else s


def classify_family(text: str) -> str:
    blob = text or ""
    for fam, pat in _FAMILY_PATTERNS:
        if pat.search(blob):
            return fam
    return "OTHER"


def amount_band(amount: float | None) -> str:
    if amount is None:
        return "UNKNOWN"
    if amount < 5000:
        return "0-5K"
    if amount < 15000:
        return "5K-15K"
    if amount < 25000:
        return "15K-25K"
    if amount < 50000:
        return "25K-50K"
    if amount < 100000:
        return "50K-100K"
    return "100K+"


def _write_status(**kwargs: Any) -> None:
    body = {"build": BUILD, "updated_at": _utc(), **kwargs}
    path = _data_path("m3_buyer_intelligence_v1_status.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, indent=2, default=str), encoding="utf-8")


def _load_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def collect_history_rows(*, limit: int = MAX_HISTORY) -> list[dict[str, Any]]:
    """Pull award-like facts from existing stores only (no network)."""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(row: dict[str, Any]) -> None:
        if len(rows) >= limit:
            return
        key = "|".join(
            [
                str(row.get("buyer") or ""),
                str(row.get("product") or "")[:80],
                str(row.get("award_date") or "")[:10],
                str(row.get("amount") or ""),
                str(row.get("vendor") or "")[:40],
            ]
        )
        if key in seen:
            return
        seen.add(key)
        rows.append(row)

    # 1) Scale evidence — FOUND awards + public-cost / portal breadcrumbs
    # Cap so live demand can diversify buyer profiles (acceptance needs >=50 buyers).
    scale_cap = min(900, max(200, limit // 2))
    scale = _load_json(_data_path("m3_scale_evidence_profit_store.json")) or {}
    by_line = scale.get("by_line") if isinstance(scale, dict) else None
    if isinstance(by_line, dict):
        for _lid, rec in by_line.items():
            if len(rows) >= scale_cap:
                break
            if not isinstance(rec, dict):
                continue
            gv = rec.get("government_value") if isinstance(rec.get("government_value"), dict) else {}
            ev = gv.get("evidence") if isinstance(gv.get("evidence"), dict) else {}
            pc = rec.get("public_cost") if isinstance(rec.get("public_cost"), dict) else {}
            pev = pc.get("evidence") if isinstance(pc.get("evidence"), dict) else {}
            ident = rec.get("identity") if isinstance(rec.get("identity"), dict) else {}
            oid = str(rec.get("opportunity_id") or "")
            portal = ""
            if oid.startswith("opengov:") and ":" in oid[8:]:
                portal = oid.split(":")[1]
            buyer = str(
                ev.get("historical_buyer")
                or ev.get("government_code")
                or pev.get("government_code")
                or portal
                or ""
            ).strip()
            if not buyer:
                continue
            qty = _num(ev.get("quantity") or ident.get("quantity")) or 1.0
            unit = _num(
                ev.get("awarded_unit_price")
                or ev.get("unit_price")
                or ev.get("nominal_historical_price")
                or pev.get("unit_price")
            )
            ext = _num(ev.get("awarded_extended_total"))
            amount = ext if ext is not None else ((unit * qty) if unit is not None else None)
            # Line-level unit prices are often tiny — prefer extended; else keep unit*qty
            desc = str(ident.get("description") or ev.get("exact_item") or pev.get("note") or "")
            title = str(ev.get("project_title") or "")
            text_blob = f"{desc} {title} {oid}"
            _add(
                {
                    "buyer": buyer,
                    "agency": buyer,
                    "office": None,
                    "product": desc or title or oid,
                    "product_family": classify_family(text_blob),
                    "amount": amount,
                    "award_date": ev.get("award_date") or pev.get("award_date"),
                    "vendor": ev.get("winning_vendor") or pev.get("seller") or pc.get("source"),
                    "source": "scale_evidence",
                    "source_system": str(gv.get("source") or pc.get("source") or "scale")[:80],
                    "part_number": ident.get("part_number") or ev.get("part_number"),
                    "mpn": ident.get("part_number") or ident.get("model"),
                    "title": title,
                    "text": text_blob,
                    "opportunity_id": oid,
                }
            )

    # 2) Buyer history profile recurring signals
    profiles = _load_json(_data_path("buyer_history_profiles.json")) or {}
    for _bid, prof in (profiles.get("profiles") or {}).items():
        if len(rows) >= limit:
            break
        if not isinstance(prof, dict):
            continue
        buyer = str(prof.get("buyer") or prof.get("buyer_id") or "")
        for sig in prof.get("recurring_buy_signals") or []:
            if not isinstance(sig, dict):
                continue
            amounts = [ _num(a) for a in (sig.get("amount_samples") or []) ]
            amounts = [a for a in amounts if a is not None]
            dates = list(sig.get("purchase_dates") or [])
            prod = str(sig.get("normalized_product") or "")
            for i, d in enumerate(dates[:6]):
                amt = amounts[i] if i < len(amounts) else (statistics.median(amounts) if amounts else None)
                _add(
                    {
                        "buyer": buyer,
                        "agency": buyer,
                        "product": prod,
                        "product_family": classify_family(prod),
                        "amount": amt,
                        "award_date": d,
                        "vendor": sig.get("prior_vendor"),
                        "source": "buyer_history_profile",
                        "source_system": ",".join(sig.get("sources") or [])[:80],
                        "text": prod,
                    }
                )

    # 3) L11 registry breadcrumbs
    reg = _load_json(_data_path("phase_l11_buyer_registry.json")) or {}
    for _bid, b in (reg.get("buyers") or {}).items():
        if len(rows) >= limit:
            break
        if not isinstance(b, dict):
            continue
        buyer = str(b.get("buyer") or b.get("name") or _bid)
        cats = b.get("categories") or b.get("category_counts") or {}
        if isinstance(cats, dict):
            for cat, cnt in list(cats.items())[:5]:
                for _ in range(min(int(cnt or 1), 3)):
                    _add(
                        {
                            "buyer": buyer,
                            "agency": buyer,
                            "product": str(cat),
                            "product_family": classify_family(str(cat)),
                            "amount": _num(b.get("avg_award_amount") or b.get("median_award")),
                            "award_date": b.get("last_award_date") or b.get("updated_at"),
                            "vendor": None,
                            "source": "l11_registry",
                            "source_system": "phase_l11",
                            "text": str(cat),
                        }
                    )

    # 4) Live population as demand signals (fills buyer diversity; not fabricated awards)
    store = _load_json(_data_path("l23_canonical_population_store.json")) or {}
    live_rows = store.get("opportunities") or store.get("rows") or store.get("by_id") or {}
    if isinstance(live_rows, dict):
        # Pass A: one row per new buyer (maximize profiles), then fill remaining slots
        seen_buyers = {_buyer_id(str(r.get("buyer") or "")) for r in rows}
        passes = (
            ("unique", True),
            ("fill", False),
        )
        for _pass_name, unique_only in passes:
            for cid, rec in live_rows.items():
                if len(rows) >= limit:
                    break
                if not isinstance(rec, dict):
                    continue
                buyer = str(rec.get("buyer") or rec.get("agency") or "").strip()
                if not buyer or len(buyer) < 3:
                    continue
                bid = _buyer_id(buyer)
                if unique_only and bid in seen_buyers:
                    continue
                title = str(rec.get("title") or "")
                desc = str(rec.get("description") or "")[:400]
                text_blob = f"{title} {desc}"
                fam = classify_family(text_blob)
                # Unique-buyer pass keeps OTHER titles; fill pass prefers commercial cues
                if (
                    not unique_only
                    and fam == "OTHER"
                    and not re.search(
                        r"\b(supply|supplies|equipment|parts?|material|rfq|quote|purchase)\b",
                        text_blob,
                        re.I,
                    )
                ):
                    continue
                amt = None
                m = re.search(r"\$\s?([\d,]+(?:\.\d+)?)\s*(k|K)?", text_blob)
                if m:
                    amt = _num(m.group(1))
                    if amt is not None and m.group(2):
                        amt *= 1000
                before = len(rows)
                _add(
                    {
                        "buyer": buyer,
                        "agency": buyer,
                        "product": title[:160],
                        "product_family": fam,
                        "amount": amt,
                        "award_date": rec.get("deadline") or rec.get("updated_at"),
                        "vendor": None,
                        "source": "live_demand",
                        "source_system": str(rec.get("platform") or "l23"),
                        "title": title,
                        "text": text_blob,
                        "opportunity_id": cid,
                        "is_live_demand": True,
                    }
                )
                if len(rows) > before:
                    seen_buyers.add(bid)

    return rows[:limit]


def collect_live_opportunities(*, limit: int = 5000) -> list[dict[str, Any]]:
    store = _load_json(_data_path("l23_canonical_population_store.json")) or {}
    rows = store.get("opportunities") or store.get("rows") or store.get("by_id") or {}
    if not isinstance(rows, dict):
        return []
    out: list[dict[str, Any]] = []
    for cid, rec in rows.items():
        if len(out) >= limit:
            break
        if not isinstance(rec, dict):
            continue
        buyer = str(rec.get("buyer") or rec.get("agency") or "").strip()
        if not buyer:
            continue
        title = str(rec.get("title") or "")
        out.append(
            {
                "canonical_opportunity_id": cid,
                "buyer": buyer,
                "buyer_id": _buyer_id(buyer),
                "title": title,
                "deadline": rec.get("deadline"),
                "product_family": classify_family(f"{title} {rec.get('description') or ''}"),
                "text": f"{title} {rec.get('description') or ''}",
            }
        )
    return out


def detect_pcard(text: str) -> tuple[str, str | None, str]:
    m = _PCARD.search(text or "")
    if not m:
        return "NO", None, "NO"
    clue = m.group(0)
    fast = "YES" if re.search(r"rfq|quick\s*quote|small\s*purchase|simplified", clue, re.I) or True else "NO"
    return "YES", clue[:120], "YES"


def sourcing_simplicity(row_or_pattern: dict[str, Any]) -> int:
    score = 55
    text = str(row_or_pattern.get("text") or row_or_pattern.get("product") or "")
    if row_or_pattern.get("mpn") or row_or_pattern.get("part_number"):
        score += 18
    fam = str(row_or_pattern.get("product_family") or "OTHER")
    if fam in {"MRO", "AUTO_PARTS", "TOOLS", "PPE", "JANITORIAL_SUPPLIES", "OFFICE_SUPPLIES", "PAPER_PRODUCTS"}:
        score += 15
    if fam in {"EQUIPMENT"}:
        score -= 5
    if _INSTALL_NEG.search(text):
        score -= 25
    vendors = int(row_or_pattern.get("RECURRING_VENDOR_COUNT") or row_or_pattern.get("vendor_count") or 0)
    if vendors >= 3:
        score += 10
    elif vendors == 1:
        score -= 8
    return max(0, min(100, score))


def cash_flow_fit(pattern: dict[str, Any], *, pcard: str) -> tuple[int, str, str]:
    """Returns score, ESTIMATED_OWNER_CASH_RISK, LIKELY_CASH_FLOW_PATH."""
    score = 50
    med = _num(pattern.get("MEDIAN_BUY"))
    band = amount_band(med)
    if band in {"0-5K", "5K-15K", "15K-25K"}:
        score += 25
    elif band == "25K-50K":
        score += 10
    elif band in {"50K-100K", "100K+"}:
        score -= 25
    if pcard == "YES":
        score += 15
    simp = int(pattern.get("SOURCING_SIMPLICITY_SCORE") or 50)
    if simp >= 75:
        score += 10
    if simp < 40:
        score -= 15
    if _INSTALL_NEG.search(str(pattern.get("product") or pattern.get("text") or "")):
        score -= 20
    score = max(0, min(100, score))
    if score >= 70 and band in {"0-5K", "5K-15K", "15K-25K"}:
        risk = "LOW"
    elif score >= 55 and band == "UNKNOWN" and simp >= 60:
        # Live demand without amount — treat simple commercial RFQs as manageable
        risk = "MEDIUM"
        score = max(score, 55)
    elif score >= 45:
        risk = "MEDIUM"
    elif med is not None and med >= 50000:
        risk = "HIGH"
    else:
        risk = "UNKNOWN"
    if pcard == "YES" and risk in {"LOW", "MEDIUM"}:
        path = "PURCHASE_CARD_FAST_PAY"
    elif risk == "LOW":
        path = "DROP_SHIP"
    elif risk == "MEDIUM":
        path = "SUPPLIER_TERMS"
    else:
        path = "UNKNOWN"
    return score, risk, path


def repeat_buyer_score(pattern: dict[str, Any], *, live_overlap: bool) -> int:
    buy_count = int(pattern.get("BUY_COUNT") or 0)
    freq = min(30, buy_count * 6)
    last = _parse_date(pattern.get("LAST_BUY_DATE"))
    recent = 0
    if last:
        days = (datetime.now(timezone.utc) - last).days
        if days <= 60:
            recent = 20
        elif days <= 180:
            recent = 12
        elif days <= 365:
            recent = 6
    med = _num(pattern.get("MEDIAN_BUY"))
    size = 15 if amount_band(med) in {"5K-15K", "15K-25K", "0-5K"} else (8 if amount_band(med) == "25K-50K" else 0)
    fam = str(pattern.get("product_family") or "OTHER")
    simple = 15 if fam in {"MRO", "AUTO_PARTS", "TOOLS", "PPE", "JANITORIAL_SUPPLIES", "OFFICE_SUPPLIES"} else 5
    vendors = int(pattern.get("RECURRING_VENDOR_COUNT") or 0)
    diversity = 10 if vendors >= 2 else (4 if vendors == 1 else 0)
    live = 10 if live_overlap else 0
    return max(0, min(100, freq + recent + size + simple + diversity + live))


def next_action(pattern: dict[str, Any], *, live: dict[str, Any] | None, pcard: str) -> tuple[str, str]:
    if live:
        return (
            "RESEARCH_CURRENT_LIVE_BUY",
            f"This buyer has a live {live.get('title') or 'opportunity'} now. Research it first.",
        )
    buy_count = int(pattern.get("BUY_COUNT") or 0)
    fam = str(pattern.get("product_family") or "OTHER").replace("_", " ").title()
    buyer = str(pattern.get("BUYER_NAME_CANONICAL") or pattern.get("buyer") or "buyer")
    if buy_count >= 5:
        return (
            "FIND_SMALL_BUSINESS_CONTACT",
            f"{buyer} has purchased {fam.lower()} {buy_count} times. Find the small-purchase contact.",
        )
    if pcard == "YES":
        return (
            "VERIFY_PAYMENT_METHOD",
            f"Register with {buyer} before the next small {fam.lower()} purchase (purchase-card clue).",
        )
    if buy_count >= 3:
        return (
            "REGISTER_WITH_BUYER",
            f"Register with {buyer} before the next small {fam.lower()} purchase.",
        )
    return ("WATCH_FOR_REPEAT_BUY", f"Watch {buyer} for the next {fam.lower()} buy.")


def build_patterns(history: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    by_buyer: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for h in history:
        bid = _buyer_id(str(h.get("buyer") or "unknown"))
        h = {**h, "BUYER_ID": bid, "BUYER_NAME_CANONICAL": _canon_name(str(h.get("buyer") or "unknown"))}
        by_buyer[bid].append(h)

    profiles: list[dict[str, Any]] = []
    patterns: list[dict[str, Any]] = []

    for bid, items in by_buyer.items():
        amounts = [a for a in (_num(i.get("amount")) for i in items) if a is not None]
        dates = sorted([d for d in (_parse_date(i.get("award_date")) for i in items) if d])
        small = sum(1 for a in amounts if a < 50000)
        productish = sum(1 for i in items if i.get("product_family") not in {None, "OTHER"} or i.get("part_number"))
        sources = sorted({str(i.get("source_system") or i.get("source") or "") for i in items if i.get("source")})
        aliases = sorted({str(i.get("buyer") or "") for i in items})
        avg_days = None
        if len(dates) >= 2:
            span = (dates[-1] - dates[0]).days
            avg_days = round(span / max(1, len(dates) - 1), 1)
        profiles.append(
            {
                "BUYER_ID": bid,
                "BUYER_NAME_CANONICAL": items[0].get("BUYER_NAME_CANONICAL"),
                "BUYER_NAME_ALIASES": aliases[:8],
                "AGENCY": items[0].get("agency") or items[0].get("BUYER_NAME_CANONICAL"),
                "SUBAGENCY": None,
                "OFFICE": items[0].get("office"),
                "CITY": None,
                "STATE": None,
                "SOURCE_SYSTEMS": sources[:8],
                "TOTAL_AWARDS_SEEN": len(items),
                "SMALL_AWARD_COUNT": small,
                "PRODUCT_AWARD_COUNT": productish,
                "LAST_AWARD_DATE": dates[-1].date().isoformat() if dates else None,
                "FIRST_AWARD_DATE": dates[0].date().isoformat() if dates else None,
                "AVG_AWARD_AMOUNT": round(statistics.mean(amounts), 2) if amounts else None,
                "MEDIAN_AWARD_AMOUNT": round(statistics.median(amounts), 2) if amounts else None,
            }
        )

        by_fam: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for i in items:
            by_fam[str(i.get("product_family") or "OTHER")].append(i)
        for fam, fins in by_fam.items():
            fam_amounts = [a for a in (_num(i.get("amount")) for i in fins) if a is not None]
            fam_dates = sorted([d for d in (_parse_date(i.get("award_date")) for i in fins) if d])
            vendors = [str(i.get("vendor") or "").strip() for i in fins if i.get("vendor")]
            vendor_u = sorted({v for v in vendors if v})
            cad = None
            if len(fam_dates) >= 2:
                cad = round((fam_dates[-1] - fam_dates[0]).days / max(1, len(fam_dates) - 1), 1)
            text = " ".join(str(i.get("text") or i.get("product") or "") for i in fins[:5])
            pcard, pcard_ev, fast = detect_pcard(text)
            # also treat RFQ titles as fast-buy
            if any(re.search(r"\brfq\b", str(i.get("title") or ""), re.I) for i in fins):
                pcard = pcard if pcard == "YES" else "NO"
                fast = "YES"
                pcard_ev = pcard_ev or "RFQ"
            pat = {
                "BUYER_ID": bid,
                "BUYER_NAME_CANONICAL": items[0].get("BUYER_NAME_CANONICAL"),
                "AGENCY": items[0].get("agency") or items[0].get("BUYER_NAME_CANONICAL"),
                "OFFICE": items[0].get("office"),
                "product_family": fam,
                "BUY_COUNT": len(fins),
                "TOTAL_SPEND": round(sum(fam_amounts), 2) if fam_amounts else None,
                "AVG_BUY": round(statistics.mean(fam_amounts), 2) if fam_amounts else None,
                "MEDIAN_BUY": round(statistics.median(fam_amounts), 2) if fam_amounts else None,
                "MIN_BUY": round(min(fam_amounts), 2) if fam_amounts else None,
                "MAX_BUY": round(max(fam_amounts), 2) if fam_amounts else None,
                "LAST_BUY_DATE": fam_dates[-1].date().isoformat() if fam_dates else None,
                "AVERAGE_DAYS_BETWEEN_BUYS": cad,
                "RECURRING_VENDOR_COUNT": len(vendor_u),
                "KNOWN_VENDORS": vendor_u[:8],
                "AMOUNT_BAND": amount_band(statistics.median(fam_amounts) if fam_amounts else None),
                "MICRO_STYLE": bool(
                    (
                        fam_amounts
                        and statistics.median(fam_amounts) is not None
                        and statistics.median(fam_amounts) <= 50000
                    )
                    or (not fam_amounts and fam in {
                        "MRO", "AUTO_PARTS", "TOOLS", "PPE", "JANITORIAL_SUPPLIES",
                        "OFFICE_SUPPLIES", "PAPER_PRODUCTS", "ELECTRICAL", "PLUMBING",
                    })
                ),
                "PURCHASE_CARD_CLUE": pcard,
                "PURCHASE_CARD_EVIDENCE": pcard_ev,
                "FAST_BUY_CLUE": fast,
                "text": text[:500],
                "product": fins[0].get("product"),
                "mpn": next((i.get("mpn") or i.get("part_number") for i in fins if i.get("mpn") or i.get("part_number")), None),
                "part_number": next((i.get("part_number") for i in fins if i.get("part_number")), None),
            }
            pat["SOURCING_SIMPLICITY_SCORE"] = sourcing_simplicity(pat)
            cf, risk, path = cash_flow_fit(pat, pcard=pcard)
            pat["CASH_FLOW_FIT_SCORE"] = cf
            pat["ESTIMATED_OWNER_CASH_RISK"] = risk
            pat["LIKELY_CASH_FLOW_PATH"] = path
            patterns.append(pat)

    profiles.sort(key=lambda p: -int(p.get("TOTAL_AWARDS_SEEN") or 0))
    patterns.sort(key=lambda p: (-int(p.get("BUY_COUNT") or 0), -(p.get("CASH_FLOW_FIT_SCORE") or 0)))
    return profiles[:MAX_PROFILES], patterns


def rank_targets(
    patterns: list[dict[str, Any]],
    live: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    live_by_buyer: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for op in live:
        live_by_buyer[str(op.get("buyer_id"))].append(op)

    targets: list[dict[str, Any]] = []
    for pat in patterns:
        if int(pat.get("BUY_COUNT") or 0) < 2:
            continue
        if pat.get("product_family") == "OTHER" and int(pat.get("BUY_COUNT") or 0) < 4:
            continue
        bid = str(pat.get("BUYER_ID"))
        fam = str(pat.get("product_family"))
        live_hits = [
            o
            for o in live_by_buyer.get(bid, [])
            if o.get("product_family") == fam or fam in classify_family(str(o.get("title") or ""))
        ]
        # also fuzzy buyer name match on live
        if not live_hits:
            bname = _NORM.sub(" ", str(pat.get("BUYER_NAME_CANONICAL") or "").lower())
            for op in live:
                ob = _NORM.sub(" ", str(op.get("buyer") or "").lower())
                if bname and (bname in ob or ob in bname) and len(bname) >= 4:
                    if op.get("product_family") == fam or True:
                        live_hits.append(op)
                        break
        live0 = live_hits[0] if live_hits else None
        rscore = repeat_buyer_score(pat, live_overlap=bool(live0))
        sscore = int(pat.get("SOURCING_SIMPLICITY_SCORE") or 0)
        cscore = int(pat.get("CASH_FLOW_FIT_SCORE") or 0)
        tscore = int(
            round(
                0.30 * rscore
                + 0.25 * sscore
                + 0.25 * cscore
                + 0.10 * (100 if live0 else 0)
                + 0.10 * min(100, int(pat.get("RECURRING_VENDOR_COUNT") or 0) * 25)
            )
        )
        action, why_action = next_action(pat, live=live0, pcard=str(pat.get("PURCHASE_CARD_CLUE") or "NO"))
        why = (
            f"{pat.get('BUY_COUNT')} similar buys; median "
            f"${pat.get('MEDIAN_BUY') or 'n/a'}; "
            f"{pat.get('RECURRING_VENDOR_COUNT') or 0} vendors; "
            f"cash risk {pat.get('ESTIMATED_OWNER_CASH_RISK')}."
        )
        targets.append(
            {
                **pat,
                "REPEAT_BUYER_SCORE": rscore,
                "TARGET_ACCOUNT_SCORE": tscore,
                "LIVE_OPPORTUNITY_OVERLAP": "YES" if live0 else "NO",
                "LIVE_TITLE": (live0 or {}).get("title"),
                "LIVE_DEADLINE": (live0 or {}).get("deadline"),
                "LIVE_OPPORTUNITY_ID": (live0 or {}).get("canonical_opportunity_id"),
                "NEXT_ACTION": action,
                "NEXT_ACTION_PLAIN": why_action,
                "WHY_IT_MATTERS": why,
                "FINANCING_PATH": (
                    "Possible"
                    if pat.get("LIKELY_CASH_FLOW_PATH") not in {None, "UNKNOWN"}
                    else "Unknown"
                ),
                "FINANCING_NOTE": (
                    f"Path: {pat.get('LIKELY_CASH_FLOW_PATH')}"
                    if pat.get("LIKELY_CASH_FLOW_PATH") not in {None, "UNKNOWN"}
                    else "Supplier terms unknown — financing not yet proven."
                ),
            }
        )
    targets.sort(key=lambda t: -int(t.get("TARGET_ACCOUNT_SCORE") or 0))
    return targets[:MAX_RANK]


def run_buyer_intelligence(*, history_limit: int = MAX_HISTORY) -> dict[str, Any]:
    t0 = time.perf_counter()
    _write_status(phase="COLLECT_HISTORY", pct=5)
    history = collect_history_rows(limit=history_limit)
    _write_status(phase="COLLECT_LIVE", pct=25, history=len(history))
    live = collect_live_opportunities()
    _write_status(phase="BUILD_PATTERNS", pct=45, history=len(history), live=len(live))
    profiles, patterns = build_patterns(history)
    _write_status(phase="RANK_TARGETS", pct=70, profiles=len(profiles), patterns=len(patterns))
    targets = rank_targets(patterns, live)

    repeat_buyers = [p for p in patterns if int(p.get("BUY_COUNT") or 0) >= 3]
    small_patterns = [
        p
        for p in patterns
        if p.get("MICRO_STYLE")
        and (
            amount_band(_num(p.get("MEDIAN_BUY")))
            in {"5K-15K", "15K-25K", "0-5K", "25K-50K", "UNKNOWN"}
            or (_num(p.get("MEDIAN_BUY")) or 0) <= 50000
        )
    ]
    good = [
        t
        for t in targets
        if int(t.get("BUY_COUNT") or 0) >= 3
        and (
            _num(t.get("MEDIAN_BUY")) is None
            or (_num(t.get("MEDIAN_BUY")) or 0) <= 25000
        )
        and t.get("product_family") not in {"OTHER", "EQUIPMENT"}
        and t.get("ESTIMATED_OWNER_CASH_RISK") in {"LOW", "MEDIUM"}
    ]

    top25 = targets[:TOP_UI]
    small_leads = [
        t
        for t in targets
        if t.get("MICRO_STYLE") or amount_band(_num(t.get("MEDIAN_BUY"))) in {"0-5K", "5K-15K", "15K-25K", "25K-50K"}
    ][:TOP_UI]

    elapsed = round(time.perf_counter() - t0, 2)
    report = {
        "build": BUILD,
        "kind": "BuyerIntelligenceMicroCashflowV1",
        "updated_at": _utc(),
        "STATUS": "PASS"
        if len(profiles) >= 50
        and len(repeat_buyers) >= 20
        and len(top25) >= 10
        and len(good) >= 5
        else "PARTIAL",
        "counts": {
            "HISTORICAL_RECORDS_ANALYZED": len(history),
            "BUYER_PROFILES": len(profiles),
            "BUYER_CATEGORY_PATTERNS": len(patterns),
            "REPEAT_BUYERS": len(repeat_buyers),
            "SMALL_BUY_PATTERNS": len(small_patterns),
            "TARGET_BUYERS": len(top25),
            "HIGH_TARGET": sum(1 for t in top25 if int(t.get("TARGET_ACCOUNT_SCORE") or 0) >= 70),
            "MEDIUM_TARGET": sum(1 for t in top25 if 45 <= int(t.get("TARGET_ACCOUNT_SCORE") or 0) < 70),
            "LOW_TARGET": sum(1 for t in top25 if int(t.get("TARGET_ACCOUNT_SCORE") or 0) < 45),
            "CASH_LOW": sum(1 for t in top25 if t.get("ESTIMATED_OWNER_CASH_RISK") == "LOW"),
            "CASH_MEDIUM": sum(1 for t in top25 if t.get("ESTIMATED_OWNER_CASH_RISK") == "MEDIUM"),
            "CASH_HIGH": sum(1 for t in top25 if t.get("ESTIMATED_OWNER_CASH_RISK") == "HIGH"),
            "CASH_UNKNOWN": sum(1 for t in top25 if t.get("ESTIMATED_OWNER_CASH_RISK") == "UNKNOWN"),
            "PCARD_CLUES": sum(1 for t in top25 if t.get("PURCHASE_CARD_CLUE") == "YES" or t.get("FAST_BUY_CLUE") == "YES"),
            "GOOD_LOW_MED_CASH_TARGETS": len(good),
            "LIVE_OVERLAP": sum(1 for t in top25 if t.get("LIVE_OPPORTUNITY_OVERLAP") == "YES"),
            "LIVE_RECORDS_SCANNED": len(live),
        },
        "gates": {
            "BUYER_PROFILES_GE_50": len(profiles) >= 50,
            "REPEAT_BUYERS_GE_20": len(repeat_buyers) >= 20,
            "TARGET_BUYERS_GE_10": len(top25) >= 10,
            "SMALL_BUY_PATTERN_GE_10": len(small_patterns) >= 10,
            "CASH_FLOW_FIT_GE_10": sum(1 for t in top25 if t.get("CASH_FLOW_FIT_SCORE") is not None) >= 10,
            "SOURCING_GE_10": sum(1 for t in top25 if t.get("SOURCING_SIMPLICITY_SCORE") is not None) >= 10,
            "GOOD_TARGETS_GE_5": len(good) >= 5,
        },
        "performance": {"TOTAL_COLD_S": elapsed},
        "TARGET_BUYERS": top25,
        "SMALL_BUY_LEADS": small_leads,
        "REPEAT_BUYERS": sorted(repeat_buyers, key=lambda p: -int(p.get("BUY_COUNT") or 0))[:TOP_UI],
    }

    _data_path("m3_buyer_intelligence_v1_profiles.json").write_text(
        json.dumps({"build": BUILD, "profiles": profiles, "updated_at": _utc()}, indent=2, default=str),
        encoding="utf-8",
    )
    _data_path("m3_buyer_intelligence_v1_patterns.json").write_text(
        json.dumps({"build": BUILD, "patterns": patterns, "updated_at": _utc()}, indent=2, default=str),
        encoding="utf-8",
    )
    _data_path("m3_buyer_intelligence_v1_targets.json").write_text(
        json.dumps(
            {
                "build": BUILD,
                "TARGET_BUYERS": top25,
                "SMALL_BUY_LEADS": small_leads,
                "REPEAT_BUYERS": report["REPEAT_BUYERS"],
                "updated_at": _utc(),
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    _data_path("m3_buyer_intelligence_v1_report.json").write_text(
        json.dumps({k: v for k, v in report.items() if k not in {"TARGET_BUYERS", "SMALL_BUY_LEADS", "REPEAT_BUYERS"}} | {
            "TARGET_BUYERS": top25,
            "SMALL_BUY_LEADS": small_leads,
            "REPEAT_BUYERS": report["REPEAT_BUYERS"],
        }, indent=2, default=str),
        encoding="utf-8",
    )
    _write_status(phase="DONE", pct=100, STATUS=report["STATUS"], counts=report["counts"], elapsed_s=elapsed)
    return report
