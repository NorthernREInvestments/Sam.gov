"""Public commercial + government pricing evidence acquisition.

Retrieves real public seller/price and government award evidence.
No outreach. Explicit cost ledger. UNKNOWN != unavailable.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote_plus, unquote

from application_clock import now_utc
from m3_acquisition_target_engine import (
    CURRENT_ESTIMATED_REVENUE,
    CURRENT_VERIFIED_REVENUE,
    calculate_acquisition_target,
    target_profit_usd,
)
from m3_commercial_product_matching import (
    PRIORITY_TITLES,
    _eligible_profiles,
    _identity_bundle,
    extract_evidence_candidates,
)
from m3_deal_economics import (
    STATUS_BELOW,
    STATUS_UNKNOWN,
    STATUS_UNVIABLE,
    classify_profit_target_status,
    resolve_quantity,
)
from m3_product_identity_resolution import MATCH_HIGH, MATCH_LOW, MATCH_MEDIUM, MATCH_UNKNOWN

log = logging.getLogger("govtracker.m3_public_pricing_evidence")

EVIDENCE_INDEX_KEY = "m3_public_pricing_evidence_v1"
UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

ACCESS_OK = "OK"
AUTH_REQUIRED = "AUTH_REQUIRED"
BOT_BLOCKED = "BOT_BLOCKED"
REGISTRATION_REQUIRED = "REGISTRATION_REQUIRED"
RATE_LIMITED = "RATE_LIMITED"
ACCESS_BLOCKED = "ACCESS_BLOCKED"
FETCH_ERROR = "FETCH_ERROR"

CURRENT_GOVERNMENT_ESTIMATE = "CURRENT_GOVERNMENT_ESTIMATE"
HISTORICAL_EXACT_UNIT_PRICE = "HISTORICAL_EXACT_UNIT_PRICE"
HISTORICAL_EXACT_AWARD = "HISTORICAL_EXACT_AWARD"
HISTORICAL_SAME_ITEM_BENCHMARK = "HISTORICAL_SAME_ITEM_BENCHMARK"
HISTORICAL_COMPARABLE_BENCHMARK_LABEL = "HISTORICAL_COMPARABLE_BENCHMARK"
NO_USABLE_REVENUE_EVIDENCE = "NO_USABLE_REVENUE_EVIDENCE"
HISTORICAL_REVENUE_BENCHMARK_SCENARIO = "HISTORICAL_REVENUE_BENCHMARK_SCENARIO"

VOLUME_PRICING_EVIDENCED = "VOLUME_PRICING_EVIDENCED"
RESELLER_PROGRAM_EVIDENCED = "RESELLER_PROGRAM_EVIDENCED"
PROJECT_PRICING_EVIDENCED = "PROJECT_PRICING_EVIDENCED"
MANUFACTURER_DIRECT_EVIDENCED = "MANUFACTURER_DIRECT_EVIDENCED"
RFQ_PRICING_REQUIRED = "RFQ_PRICING_REQUIRED"
WHOLESALE_ACCESS_UNVERIFIED_SIGNAL = "WHOLESALE_ACCESS_UNVERIFIED"

Q_NEEDS_PUBLIC_PRODUCT_EVIDENCE = "NEEDS_PUBLIC_PRODUCT_EVIDENCE"
Q_NEEDS_PUBLIC_PRICE = "NEEDS_PUBLIC_PRICE"
Q_NEEDS_GOVERNMENT_REVENUE_EVIDENCE = "NEEDS_GOVERNMENT_REVENUE_EVIDENCE"
Q_RFQ_PRICE_REQUIRED_FUTURE = "RFQ_PRICE_REQUIRED_FUTURE"
Q_WHOLESALE_VERIFICATION_REQUIRED_FUTURE = "WHOLESALE_VERIFICATION_REQUIRED_FUTURE"
Q_READY_FOR_ECONOMICS = "READY_FOR_ECONOMICS"
Q_OWNER_REVIEW = "OWNER_REVIEW"

VA_ALLOWED = frozenset(
    {"SEARCH_PUBLIC_SOURCES", "ATTACH_EVIDENCE", "NORMALIZE_PRICING", "RESEARCH_AWARD_HISTORY", "ADD_NOTES", "NOTE"}
)
VA_FORBIDDEN = frozenset(
    {
        "CONTACT_SUPPLIER",
        "REQUEST_QUOTE",
        "REGISTER_SUPPLIER_ACCOUNT",
        "NEGOTIATE",
        "APPLY_FOR_FINANCING",
        "SUBMIT_BID",
        "APPROVE_PURSUIT",
    }
)

PRICE_RE = re.compile(
    r"(?P<pre>.{0,50})\$\s*(?P<amt>\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\d+(?:\.\d{2})?)(?P<post>.{0,50})",
    re.I | re.S,
)
LB_HINT = re.compile(r"\b(pls\s*)?(lb|lbs|pound|pounds)\b", re.I)
FT_HINT = re.compile(r"\b(ft|feet|foot)\b", re.I)
EA_HINT = re.compile(r"\b(each|ea|unit|pc|piece)\b", re.I)
BAG_HINT = re.compile(r"\b(\d+(?:\.\d+)?)\s*(lb|lbs)?\s*(bag|sack)\b", re.I)

BASELINE_SNAPSHOT = {
    "Products_analyzed": 32,
    "L1_L4_acquisition_prices": 0,
    "Government_revenue_evidence": 0,
    "Normalized_acquisition_costs": 0,
    "Target_acquisition_prices": 0,
    "Profit_scenarios": 0,
}


def _utc() -> str:
    return now_utc().isoformat()


def _known(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, (list, tuple, set, dict)):
        return len(v) > 0
    if isinstance(v, str):
        return v.strip() not in {"", "UNKNOWN", "unknown"}
    return True


def _num(v: Any) -> float | None:
    if v is None or v == "" or v == "UNKNOWN":
        return None
    try:
        return float(str(v).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None


@dataclass
class CostLedger:
    research_action_count: int = 0
    free_action_count: int = 0
    paid_action_count: int = 0
    reserved_cost: float = 0.0
    actual_cost: float = 0.0
    reconciled_cost: float = 0.0
    notes: list[str] = field(default_factory=list)

    def free(self, note: str = "") -> None:
        self.research_action_count += 1
        self.free_action_count += 1
        if note:
            self.notes.append(note)

    def paid_reserve(self, amount: float, note: str = "") -> None:
        self.research_action_count += 1
        self.paid_action_count += 1
        self.reserved_cost = round(self.reserved_cost + float(amount or 0), 4)
        if note:
            self.notes.append(note)

    def paid_reconcile(self, actual: float | None, reserved: float) -> None:
        if actual is None:
            actual = float(reserved or 0)
        self.actual_cost = round(self.actual_cost + float(actual), 4)
        self.reconciled_cost = round(self.reconciled_cost + float(actual), 4)
        self.reserved_cost = round(max(0.0, self.reserved_cost - float(reserved or 0)), 4)

    def as_dict(self) -> dict[str, Any]:
        return {
            "RESEARCH_ACTION_COUNT": self.research_action_count,
            "FREE_ACTION_COUNT": self.free_action_count,
            "PAID_ACTION_COUNT": self.paid_action_count,
            "RESERVED_COST": self.reserved_cost,
            "ACTUAL_COST": self.actual_cost,
            "RECONCILED_COST": self.reconciled_cost,
            "notes": self.notes[-20:],
        }


def _load_setting(key: str) -> dict[str, Any]:
    try:
        from database import SessionLocal
        from models import AppSetting

        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == key).one_or_none()
            if not row or not row.value:
                return {"by_id": {}, "updated_at": None}
            data = json.loads(row.value)
            return data if isinstance(data, dict) else {"by_id": {}, "updated_at": None}
        finally:
            db.close()
    except Exception:
        return {"by_id": {}, "updated_at": None}


def _save_setting(key: str, index: dict[str, Any]) -> None:
    index["updated_at"] = _utc()
    try:
        from database import SessionLocal
        from models import AppSetting

        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == key).one_or_none()
            payload = json.dumps(index, default=str)
            if row is None:
                db.add(AppSetting(key=key, value=payload))
            else:
                row.value = payload
            db.commit()
        finally:
            db.close()
    except Exception:
        log.debug("evidence index save failed", exc_info=True)


def load_evidence_index() -> dict[str, Any]:
    return _load_setting(EVIDENCE_INDEX_KEY)


def save_evidence_index(index: dict[str, Any]) -> None:
    _save_setting(EVIDENCE_INDEX_KEY, index)


def classify_access(status_code: int, body: str = "") -> str:
    blob = (body or "")[:2000].lower()
    if status_code in {401, 403}:
        if "captcha" in blob or "cloudflare" in blob:
            return BOT_BLOCKED
        if "register" in blob or "sign up" in blob:
            return REGISTRATION_REQUIRED
        return AUTH_REQUIRED
    if status_code == 429:
        return RATE_LIMITED
    if status_code >= 400:
        return ACCESS_BLOCKED
    return ACCESS_OK


def fetch_public_text(
    url: str,
    ledger: CostLedger,
    access_log: list[dict[str, Any]],
    *,
    seen: set[str],
) -> tuple[str, str]:
    if url in seen:
        ledger.free("reuse_url_skip")
        return "", "REUSED_SKIP"
    seen.add(url)
    try:
        import httpx

        ledger.free(f"http_get:{url[:80]}")
        r = httpx.get(url, timeout=25.0, follow_redirects=True, headers=UA)
        text = (r.text or "")[:250000]
        state = classify_access(r.status_code, text)
        access_log.append({"url": url, "status_code": r.status_code, "access_state": state, "at": _utc()})
        if state != ACCESS_OK:
            return "", state
        return text, ACCESS_OK
    except Exception as exc:
        access_log.append({"url": url, "access_state": FETCH_ERROR, "error": str(exc)[:160], "at": _utc()})
        ledger.free(f"http_error:{url[:60]}")
        return "", FETCH_ERROR


def duckduckgo_urls(query: str, ledger: CostLedger, access_log: list[dict[str, Any]], *, limit: int = 8) -> list[str]:
    url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
    text, state = fetch_public_text(url, ledger, access_log, seen=set())
    if state != ACCESS_OK:
        return []
    found: list[str] = []
    for m in re.finditer(r"uddg=([^&\"]+)", text):
        u = unquote(m.group(1))
        if u.startswith("http") and "duckduckgo.com" not in u and u not in found:
            found.append(u)
        if len(found) >= limit:
            break
    return found


def usaspending_awards(keyword: str, ledger: CostLedger, *, limit: int = 8) -> list[dict[str, Any]]:
    try:
        import httpx

        ledger.free(f"usaspending:{keyword[:60]}")
        r = httpx.post(
            "https://api.usaspending.gov/api/v2/search/spending_by_award/",
            json={
                "filters": {
                    "keywords": [keyword],
                    "award_type_codes": ["A", "B", "C", "D"],
                    "time_period": [{"start_date": "2019-01-01", "end_date": "2026-09-17"}],
                },
                "fields": [
                    "Award ID",
                    "Recipient Name",
                    "Award Amount",
                    "Start Date",
                    "Awarding Agency",
                    "Description",
                ],
                "limit": limit,
                "page": 1,
                "sort": "Start Date",
                "order": "desc",
            },
            timeout=30.0,
            headers=UA,
        )
        if r.status_code >= 400:
            return []
        return list((r.json() or {}).get("results") or [])
    except Exception:
        return []


def extract_price_observations(text: str, *, source_url: str, product_hint: str = "") -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not text:
        return out
    hint = (product_hint or "").lower()
    for m in re.finditer(r"\$\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\d+\.\d{2}|\d+)", text):
        amt = _num(m.group(1))
        if amt is None or amt <= 0 or amt > 1_000_000:
            continue
        start = max(0, m.start() - 50)
        end = min(len(text), m.end() + 50)
        ctx = text[start:end].lower()
        if re.search(r"\b(copyright|tel|fax|zip|isbn)\b", ctx):
            continue
        uom, basis, package_qty = "UNKNOWN", "UNKNOWN", None
        # Prefer UOM evidence immediately around this dollar sign only
        local = text[max(0, m.start() - 30) : min(len(text), m.end() + 30)].lower()
        if LB_HINT.search(local) or "pls" in local:
            uom, basis = "LB", "per_lb"
        elif FT_HINT.search(local):
            uom, basis = "FT", "per_ft"
        elif BAG_HINT.search(local):
            uom, basis = "BAG", "per_bag"
            bm = BAG_HINT.search(local)
            if bm:
                package_qty = _num(bm.group(1))
        elif EA_HINT.search(local):
            uom, basis = "EA", "per_each"
        relevance = MATCH_LOW
        if hint and any(tok in ctx for tok in re.findall(r"[a-z]{4,}", hint)[:6]):
            relevance = MATCH_MEDIUM
        if uom != "UNKNOWN":
            relevance = MATCH_HIGH if relevance == MATCH_MEDIUM else MATCH_MEDIUM
        if uom == "UNKNOWN" and relevance == MATCH_LOW:
            continue
        out.append(
            {
                "Observed_price": amt,
                "Observed_UOM": uom,
                "Package_quantity": package_qty if package_qty is not None else "UNKNOWN",
                "Quantity_tier": "UNKNOWN",
                "Minimum_order": "UNKNOWN",
                "Configuration": product_hint or "UNKNOWN",
                "Freight": "UNKNOWN",
                "Date": _utc(),
                "Source": source_url,
                "Price_basis": basis,
                "Confidence": relevance,
                "context_snippet": re.sub(r"\s+", " ", text[start:end])[:160],
            }
        )
        if len(out) >= 12:
            break
    return out


def detect_wholesale_signals(text: str, url: str) -> list[dict[str, Any]]:
    blob = f"{text[:8000]} {url}".lower()
    signals = []
    checks = [
        (VOLUME_PRICING_EVIDENCED, r"\b(volume\s+pric|bulk\s+pric|quantity\s+pric)\b"),
        (RESELLER_PROGRAM_EVIDENCED, r"\b(reseller\s+program|dealer\s+program)\b"),
        (PROJECT_PRICING_EVIDENCED, r"\b(project\s+pric|special\s+bid)\b"),
        (MANUFACTURER_DIRECT_EVIDENCED, r"\b(buy\s+direct|factory\s+direct)\b"),
        (RFQ_PRICING_REQUIRED, r"\b(request\s+(a\s+)?quote|call\s+for\s+(pric|quote)|rfq)\b"),
    ]
    for status, pat in checks:
        m = re.search(pat, blob, re.I)
        if m:
            signals.append(
                {"kind": "WHOLESALE_ACCESS_SIGNAL", "status": status, "Source": url, "Evidence": m.group(0), "Date": _utc()}
            )
    if not signals:
        signals.append(
            {
                "kind": "WHOLESALE_ACCESS_SIGNAL",
                "status": WHOLESALE_ACCESS_UNVERIFIED_SIGNAL,
                "Source": url or "UNKNOWN",
                "Date": _utc(),
            }
        )
    return signals


def seller_from_url(url: str) -> dict[str, Any]:
    host = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
    name = host.split(".")[0].replace("-", " ").title() if host else "UNKNOWN"
    stype = "MARKETPLACE_REFERENCE" if any(x in host for x in ("amazon.", "ebay.")) else "DISTRIBUTOR"
    return {"Seller_name": name, "Seller_type": stype, "authorized_status": "UNKNOWN"}


def normalize_purchase_quantity(
    required: float | None, *, package_size: float | None, observed_uom: str, gov_uom: str | None
) -> dict[str, Any]:
    if required is None or required <= 0:
        return {
            "kind": "QUANTITY_NORMALIZATION",
            "calculable": False,
            "REQUIRED_QUANTITY": "UNKNOWN",
            "PURCHASE_QUANTITY": "UNKNOWN",
            "PACKAGE_SIZE": "UNKNOWN",
            "PACKAGE_COUNT": "UNKNOWN",
            "EXCESS_QUANTITY": "UNKNOWN",
        }
    if package_size is None or package_size <= 0:
        return {
            "kind": "QUANTITY_NORMALIZATION",
            "calculable": True,
            "REQUIRED_QUANTITY": required,
            "PURCHASE_QUANTITY": required,
            "PACKAGE_SIZE": 1,
            "PACKAGE_COUNT": required,
            "EXCESS_QUANTITY": 0,
            "Observed_UOM": observed_uom,
            "Government_UOM": gov_uom or "UNKNOWN",
        }
    count = int(math.ceil(required / package_size))
    purchase = round(count * package_size, 4)
    return {
        "kind": "QUANTITY_NORMALIZATION",
        "calculable": True,
        "REQUIRED_QUANTITY": required,
        "PURCHASE_QUANTITY": purchase,
        "PACKAGE_SIZE": package_size,
        "PACKAGE_COUNT": count,
        "EXCESS_QUANTITY": round(purchase - required, 4),
        "Observed_UOM": observed_uom,
        "Government_UOM": gov_uom or "UNKNOWN",
    }


def pricing_level_for(obs: dict[str, Any]) -> str:
    conf = obs.get("Confidence")
    uom = obs.get("Observed_UOM")
    if conf == MATCH_HIGH and uom != "UNKNOWN":
        return "LEVEL_2"
    if uom != "UNKNOWN" and _known(obs.get("Source")):
        return "LEVEL_3"
    if _num(obs.get("Observed_price")) is not None:
        return "LEVEL_4"
    return "LEVEL_5"


def commercial_queries(row: dict[str, Any], profile: dict[str, Any]) -> list[str]:
    desc = str(profile.get("Original_description") or profile.get("Resolved_product_name") or "")
    title = str(row.get("title") or "")
    qs: list[str] = []
    for sp in re.findall(r"\(([A-Z][a-z]+\s+[a-z]+)\)", desc)[:2]:
        qs.extend([f"{sp} seed price per pound PLS", f"{sp} native seed buy lb"])
    if "blade" in desc.lower() or "carbide" in title.lower():
        qs.extend(["tungsten carbide snowplow blade price distributor", f"{desc} carbide blade buy"])
    if "badge" in desc.lower() or "badge" in title.lower():
        qs.extend(["law enforcement police badge manufacturer price", f"{desc} badge vendor catalog"])
    if "wheelchair" in title.lower() or "lift" in desc.lower():
        qs.extend(["wheelchair lift commercial price manufacturer", "vertical platform wheelchair lift catalog price"])
    qs.append(f"{desc} price buy")
    out: list[str] = []
    for q in qs:
        q = re.sub(r"\s+", " ", q).strip()
        if q and q not in out:
            out.append(q)
    return out[:6]


def government_queries(row: dict[str, Any], profile: dict[str, Any]) -> list[str]:
    title = str(row.get("title") or "")
    desc = str(profile.get("Original_description") or "")
    qs = [f"{title} award", f"{desc} government award unit price", f"Iowa DOT {title} award amount"]
    out: list[str] = []
    for q in qs:
        q = re.sub(r"\s+", " ", q).strip()
        if q and q not in out:
            out.append(q)
    return out[:4]


def retrieve_commercial_seller_evidence(
    row: dict[str, Any],
    profile: dict[str, Any],
    ledger: CostLedger,
    access_log: list[dict[str, Any]],
    *,
    max_pages: int = 6,
) -> dict[str, Any]:
    seen: set[str] = set()
    sellers: list[dict[str, Any]] = []
    prices: list[dict[str, Any]] = []
    wholesale: list[dict[str, Any]] = []
    named_products: list[dict[str, Any]] = []
    desc = str(profile.get("Original_description") or profile.get("Resolved_product_name") or "")
    pages = 0
    for q in commercial_queries(row, profile):
        if pages >= max_pages:
            break
        for u in duckduckgo_urls(q, ledger, access_log, limit=5):
            if pages >= max_pages:
                break
            if any(x in u.lower() for x in ("youtube.com", "facebook.com", "twitter.com", "linkedin.com")):
                continue
            text, state = fetch_public_text(u, ledger, access_log, seen=seen)
            if state != ACCESS_OK or not text:
                continue
            pages += 1
            seller = seller_from_url(u)
            obs = extract_price_observations(text, source_url=u, product_hint=desc)
            wholesale.extend(detect_wholesale_signals(text, u))
            tm = re.search(r"<title[^>]*>([^<]{5,120})</title>", text, re.I)
            pname = re.sub(r"\s+", " ", (tm.group(1) if tm else desc))[:120]
            cand = {
                "kind": "COMMERCIAL_PRODUCT_CANDIDATE",
                "Commercial_product_name": pname,
                "Manufacturer": "UNKNOWN",
                "Supplier": seller["Seller_name"],
                "Product_URL_reference": u,
                "Match_confidence": MATCH_MEDIUM if obs else MATCH_LOW,
                "Evidence_source": u,
                "candidate_origin": "public_page_retrieval",
                "Specifications": profile.get("Specifications") or {},
            }
            named_products.append(cand)
            for o in obs:
                level = pricing_level_for(o)
                if level == "LEVEL_5":
                    continue
                prices.append({**o, "Pricing_level": level, "Seller": seller["Seller_name"]})
            sellers.append(
                {
                    "kind": "COMMERCIAL_SELLER_EVIDENCE",
                    "Seller_name": seller["Seller_name"],
                    "Seller_type": seller["Seller_type"],
                    "Product_name": pname,
                    "Manufacturer": "UNKNOWN",
                    "Model": "UNKNOWN",
                    "Part_number": "UNKNOWN",
                    "Package_size": next(
                        (p.get("Package_quantity") for p in obs if _known(p.get("Package_quantity"))), "UNKNOWN"
                    ),
                    "Minimum_quantity": "UNKNOWN",
                    "Availability": "UNKNOWN",
                    "Observed_price": prices[-1]["Observed_price"] if prices else "UNKNOWN",
                    "Quantity_tier": "UNKNOWN",
                    "Price_basis": prices[-1]["Price_basis"] if prices else "UNKNOWN",
                    "Lead_time_evidence": "UNKNOWN",
                    "URL": u,
                    "Observation_datetime": _utc(),
                    "Evidence_confidence": MATCH_MEDIUM if obs else MATCH_LOW,
                    "authorized_status": "UNKNOWN",
                }
            )
    wh: list[dict[str, Any]] = []
    seen_wh: set[str] = set()
    for w in wholesale:
        st = str(w.get("status") or "")
        if st and st not in seen_wh:
            seen_wh.add(st)
            wh.append(w)
    if not wh:
        wh = [{"kind": "WHOLESALE_ACCESS_SIGNAL", "status": WHOLESALE_ACCESS_UNVERIFIED_SIGNAL, "Date": _utc()}]
    return {
        "COMMERCIAL_SELLER_EVIDENCE": sellers,
        "prices": prices,
        "named_products": named_products,
        "WHOLESALE_ACCESS_SIGNALS": wh,
        "pages_fetched": pages,
    }


def retrieve_government_revenue_evidence(
    row: dict[str, Any],
    profile: dict[str, Any],
    ledger: CostLedger,
    access_log: list[dict[str, Any]],
) -> dict[str, Any]:
    sources: list[dict[str, Any]] = []
    seen: set[str] = set()
    qty = _num(profile.get("Quantity")) or resolve_quantity(row)
    award = _num(row.get("award_amount"))
    est = _num(row.get("estimated_value") or row.get("government_revenue"))
    hist = _num(row.get("historical_award_amount"))
    if award is not None:
        sources.append(
            {
                "Revenue_value": award,
                "Unit_price": round(award / qty, 4) if qty else "UNKNOWN",
                "Quantity": qty if qty is not None else "UNKNOWN",
                "Evidence_type": CURRENT_VERIFIED_REVENUE,
                "Source": "row.award_amount",
                "Date": row.get("award_date") or "UNKNOWN",
                "Confidence": MATCH_HIGH,
            }
        )
    if est is not None:
        sources.append(
            {
                "Revenue_value": est,
                "Unit_price": round(est / qty, 4) if qty else "UNKNOWN",
                "Quantity": qty if qty is not None else "UNKNOWN",
                "Evidence_type": CURRENT_GOVERNMENT_ESTIMATE,
                "Source": "row.estimated_value",
                "Date": "UNKNOWN",
                "Confidence": MATCH_MEDIUM,
            }
        )
    if hist is not None:
        sources.append(
            {
                "Revenue_value": hist,
                "Unit_price": "UNKNOWN",
                "Quantity": qty if qty is not None else "UNKNOWN",
                "Evidence_type": HISTORICAL_EXACT_AWARD,
                "Source": "row.historical_award_amount",
                "Date": "UNKNOWN",
                "Confidence": MATCH_LOW,
            }
        )

    desc = str(profile.get("Original_description") or "")
    title = str(row.get("title") or "")
    keywords = []
    paren = re.findall(r"\(([A-Z][a-z]+\s+[a-z]+)\)", desc)
    if paren:
        keywords.append(paren[0] + " seed")
    if "carbide" in title.lower():
        keywords.append("carbide snow blade")
    if "wheelchair" in title.lower():
        keywords.append("wheelchair lift")
    if "badge" in title.lower():
        keywords.append("law enforcement badge")
    for kw in keywords[:3]:
        for a in usaspending_awards(kw, ledger, limit=5):
            amt = _num(a.get("Award Amount"))
            if amt is None:
                continue
            sources.append(
                {
                    "Revenue_value": amt,
                    "Unit_price": "UNKNOWN",
                    "Quantity": "UNKNOWN",
                    "Evidence_type": HISTORICAL_COMPARABLE_BENCHMARK_LABEL,
                    "Source": f"usaspending:{a.get('Award ID')}",
                    "Date": a.get("Start Date") or "UNKNOWN",
                    "Agency": a.get("Awarding Agency") or "UNKNOWN",
                    "Winner": a.get("Recipient Name") or "UNKNOWN",
                    "Confidence": MATCH_LOW,
                }
            )

    for q in government_queries(row, profile)[:3]:
        for u in duckduckgo_urls(q, ledger, access_log, limit=3):
            text, state = fetch_public_text(u, ledger, access_log, seen=seen)
            if state != ACCESS_OK:
                continue
            for m in re.finditer(
                r"(?:award(?:ed)?(?:\s+amount)?|total(?:\s+award)?|contract\s+value)\D{0,20}\$\s*([0-9,]+\.?\d*)",
                text,
                re.I,
            ):
                amt = _num(m.group(1))
                if amt is None or amt < 100:
                    continue
                sources.append(
                    {
                        "Revenue_value": amt,
                        "Unit_price": "UNKNOWN",
                        "Quantity": qty if qty is not None else "UNKNOWN",
                        "Evidence_type": HISTORICAL_SAME_ITEM_BENCHMARK,
                        "Source": u,
                        "Date": _utc(),
                        "Confidence": MATCH_LOW,
                        "context": m.group(0)[:120],
                    }
                )
                break
            for m in re.finditer(r"(?:unit\s+price|price\s+per)\D{0,12}\$\s*([0-9,]+\.?\d*)", text, re.I):
                up = _num(m.group(1))
                if up is None:
                    continue
                sources.append(
                    {
                        "Revenue_value": round(up * qty, 2) if qty else "UNKNOWN",
                        "Unit_price": up,
                        "Quantity": qty if qty is not None else "UNKNOWN",
                        "Evidence_type": HISTORICAL_EXACT_UNIT_PRICE,
                        "Source": u,
                        "Date": _utc(),
                        "Confidence": MATCH_MEDIUM,
                        "notes": ["historical_unit_price_not_current_guaranteed_revenue"],
                    }
                )
                break

    order = [
        CURRENT_VERIFIED_REVENUE,
        CURRENT_GOVERNMENT_ESTIMATE,
        HISTORICAL_EXACT_UNIT_PRICE,
        HISTORICAL_EXACT_AWARD,
        HISTORICAL_SAME_ITEM_BENCHMARK,
        HISTORICAL_COMPARABLE_BENCHMARK_LABEL,
    ]
    primary = None
    status = NO_USABLE_REVENUE_EVIDENCE
    for et in order:
        hit = next((s for s in sources if s.get("Evidence_type") == et), None)
        if hit:
            primary, status = hit, et
            break
    scenario = None
    unit_hist = next((s for s in sources if _num(s.get("Unit_price")) is not None), None)
    if unit_hist and qty:
        up = _num(unit_hist["Unit_price"])
        scenario = {
            "kind": HISTORICAL_REVENUE_BENCHMARK_SCENARIO,
            "unit_price_benchmark": up,
            "quantity": qty,
            "benchmark_revenue": round(up * qty, 2) if up is not None else "UNKNOWN",
            "Source": unit_hist.get("Source"),
            "label": "NOT_CURRENT_VERIFIED_REVENUE",
        }
    return {
        "kind": "GOVERNMENT_REVENUE_EVIDENCE",
        "status": status,
        "primary": primary,
        "sources": sources[:20],
        "HISTORICAL_REVENUE_BENCHMARK_SCENARIO": scenario,
        "SUPPLIER_PRICING_READY": qty is not None,
    }


def paid_commercial_research(row: dict[str, Any], profile: dict[str, Any], ledger: CostLedger) -> dict[str, Any]:
    est = 0.25
    auth = None
    try:
        from cost_governor import TIER_1_ACTIVE, get_cost_governor

        gov = get_cost_governor()
        oid = str(row.get("canonical_id") or "")
        fp = f"public_price_ev_v1|{oid}|{profile.get('Line_item')}|{str(profile.get('Original_description') or '')[:60]}"
        auth = gov.authorize(
            {
                "provider": "openai",
                "action_type": "AI_COMPLETION",
                "estimated_max_cost": est,
                "priority_tier": TIER_1_ACTIVE,
                "tracked": True,
                "question": f"Public catalog prices for {profile.get('Original_description')}",
                "could_change_decision": True,
                "deal_id": oid,
                "opportunity_id": oid,
                "idempotency_key": fp,
            }
        )
        if isinstance(auth, dict) and not auth.get("authorized", True):
            ledger.free(f"paid_blocked:{auth.get('reason')}")
            return {"executed": False, "reason": auth.get("reason"), "sellers": [], "prices": [], "government": []}
        ledger.paid_reserve(est, "openai_web_price_research")
    except Exception as exc:
        ledger.free(f"cost_governor_unavailable:{exc}")
        return {"executed": False, "reason": str(exc), "sellers": [], "prices": [], "government": []}

    try:
        from ai_model_router import FunnelStage
        from openai_runtime import create_response, extract_json_object, text_part

        raw = create_response(
            task="m3_public_pricing_evidence_research",
            instructions=(
                "Find PUBLIC catalog pages with real prices for this government requirement. "
                "Do NOT invent. Every product MUST include http URL. JSON only: "
                '{"sellers":[{"seller_name":str,"seller_type":str,"product_name":str,"manufacturer":str,'
                '"url":str,"price":number,"uom":str,"package_size":number|null,"price_basis":str}],'
                '"government_awards":[{"agency":str,"amount":number,"unit_price":number|null,'
                '"quantity":number|null,"date":str,"url":str,"winner":str}],'
                '"wholesale_signals":[str],"notes":str}'
            ),
            content=[
                text_part(
                    json.dumps(
                        {
                            "title": row.get("title"),
                            "requirement": profile.get("Original_description"),
                            "quantity": profile.get("Quantity"),
                            "specifications": profile.get("Specifications") or profile.get("SPECIFICATION_PROFILE"),
                        },
                        default=str,
                    )
                )
            ],
            max_output_tokens=1400,
            web_search=True,
            funnel_stage=FunnelStage.STAGE_3,
            automatic=True,
            notice_id=str(row.get("canonical_id") or "")[:80] or None,
        )
        parsed = extract_json_object(raw) if raw else {}
        if not isinstance(parsed, dict):
            parsed = {}
        try:
            from cost_governor import get_cost_governor

            rid = (auth or {}).get("reservation_id")
            if rid:
                get_cost_governor().reconcile(reservation_id=rid, success=True, actual_cost=est)
        except Exception:
            pass
        ledger.paid_reconcile(est, est)

        sellers, prices, gov = [], [], []
        for s in parsed.get("sellers") or []:
            if not isinstance(s, dict):
                continue
            url = s.get("url")
            if not (isinstance(url, str) and url.startswith("http")):
                continue
            amt = _num(s.get("price"))
            sellers.append(
                {
                    "kind": "COMMERCIAL_SELLER_EVIDENCE",
                    "Seller_name": s.get("seller_name") or "UNKNOWN",
                    "Seller_type": s.get("seller_type") or "DISTRIBUTOR",
                    "Product_name": s.get("product_name") or "UNKNOWN",
                    "Manufacturer": s.get("manufacturer") or "UNKNOWN",
                    "Model": "UNKNOWN",
                    "Part_number": "UNKNOWN",
                    "Package_size": s.get("package_size") if s.get("package_size") is not None else "UNKNOWN",
                    "Observed_price": amt if amt is not None else "UNKNOWN",
                    "Price_basis": s.get("price_basis") or "UNKNOWN",
                    "URL": url,
                    "Observation_datetime": _utc(),
                    "Evidence_confidence": MATCH_MEDIUM,
                    "authorized_status": "UNKNOWN",
                    "origin": "paid_web_research",
                }
            )
            if amt is not None:
                prices.append(
                    {
                        "Observed_price": amt,
                        "Observed_UOM": str(s.get("uom") or "UNKNOWN").upper(),
                        "Package_quantity": s.get("package_size") if s.get("package_size") is not None else "UNKNOWN",
                        "Source": url,
                        "Price_basis": s.get("price_basis") or "UNKNOWN",
                        "Confidence": MATCH_MEDIUM,
                        "Seller": s.get("seller_name"),
                        "Pricing_level": "LEVEL_2",
                        "Date": _utc(),
                        "Freight": "UNKNOWN",
                    }
                )
        for g in parsed.get("government_awards") or []:
            if not isinstance(g, dict):
                continue
            amt = _num(g.get("amount"))
            if amt is None:
                continue
            gov.append(
                {
                    "Revenue_value": amt,
                    "Unit_price": _num(g.get("unit_price")) if g.get("unit_price") is not None else "UNKNOWN",
                    "Quantity": _num(g.get("quantity")) if g.get("quantity") is not None else "UNKNOWN",
                    "Evidence_type": HISTORICAL_SAME_ITEM_BENCHMARK,
                    "Source": g.get("url") or "paid_web_research",
                    "Date": g.get("date") or _utc(),
                    "Winner": g.get("winner") or "UNKNOWN",
                    "Confidence": MATCH_LOW,
                }
            )
        return {
            "executed": True,
            "sellers": sellers,
            "prices": prices,
            "government": gov,
            "wholesale_signals": [
                {"kind": "WHOLESALE_ACCESS_SIGNAL", "status": s, "Source": "paid_web", "Date": _utc()}
                for s in (parsed.get("wholesale_signals") or [])
                if isinstance(s, str)
            ],
        }
    except Exception as exc:
        try:
            from cost_governor import get_cost_governor

            rid = (auth or {}).get("reservation_id")
            if rid:
                get_cost_governor().reconcile(reservation_id=rid, success=False, actual_cost=0.0)
        except Exception:
            pass
        ledger.paid_reconcile(0.0, est)
        return {"executed": False, "reason": str(exc), "sellers": [], "prices": [], "government": []}


def build_line_economics(
    row: dict[str, Any],
    profile: dict[str, Any],
    *,
    prices: list[dict[str, Any]],
    revenue: dict[str, Any],
) -> dict[str, Any]:
    qty = _num(profile.get("Quantity")) or resolve_quantity(row)
    op = row.get("operator_economics") if isinstance(row.get("operator_economics"), dict) else {}
    freight = _num(op.get("estimated_freight_usd"))
    financing = _num(op.get("estimated_financing_cost_usd"))
    expenses = _num(op.get("known_fees_usd"))
    freight_u, financing_u = freight is None, financing is None
    freight_used = freight if freight is not None else 0.0
    financing_used = financing if financing is not None else 0.0
    expenses_used = expenses if expenses is not None else 0.0

    usable = [p for p in prices if _num(p.get("Observed_price")) is not None and p.get("Pricing_level") != "LEVEL_5"]
    best = min(usable, key=lambda p: float(p["Observed_price"])) if usable else None
    package_size = _num((best or {}).get("Package_quantity")) if best else None
    if package_size is None and best and str(best.get("Observed_UOM") or "").upper() in {"LB", "FT", "EA"}:
        package_size = 1.0
    norm = normalize_purchase_quantity(
        qty,
        package_size=package_size,
        observed_uom=str((best or {}).get("Observed_UOM") or "UNKNOWN"),
        gov_uom=str(profile.get("Unit") or "UNKNOWN"),
    )

    normalized_cost: Any = "UNKNOWN"
    if best and norm.get("calculable") and _num(norm.get("PURCHASE_QUANTITY")) is not None:
        unit = _num(best["Observed_price"])
        purchase_q = _num(norm["PURCHASE_QUANTITY"])
        if unit is not None and purchase_q is not None:
            if str(best.get("Observed_UOM")) == "BAG":
                normalized_cost = round(unit * float(norm.get("PACKAGE_COUNT") or purchase_q), 2)
            else:
                normalized_cost = round(unit * purchase_q, 2)

    primary = revenue.get("primary") if isinstance(revenue.get("primary"), dict) else None
    scenario = revenue.get("HISTORICAL_REVENUE_BENCHMARK_SCENARIO")
    revenue_basis = None
    revenue_basis_type = revenue.get("status") or NO_USABLE_REVENUE_EVIDENCE
    if primary and _num(primary.get("Revenue_value")) is not None and revenue_basis_type in {
        CURRENT_VERIFIED_REVENUE,
        CURRENT_GOVERNMENT_ESTIMATE,
        CURRENT_ESTIMATED_REVENUE,
    }:
        revenue_basis = _num(primary.get("Revenue_value"))
    elif scenario and _num(scenario.get("benchmark_revenue")) is not None:
        revenue_basis = _num(scenario.get("benchmark_revenue"))
        revenue_basis_type = HISTORICAL_REVENUE_BENCHMARK_SCENARIO

    target: dict[str, Any] = {
        "calculable": False,
        "TOTAL_TARGET_ACQUISITION_COST": "UNKNOWN",
        "TARGET_UNIT_ACQUISITION_COST": "UNKNOWN",
        "Target_profit": target_profit_usd(row),
        "revenue_basis_type": revenue_basis_type,
    }
    if revenue_basis is not None:
        if revenue_basis_type == HISTORICAL_REVENUE_BENCHMARK_SCENARIO:
            tp = target_profit_usd(row)
            total = round(revenue_basis - tp - freight_used - financing_used - expenses_used, 2)
            target = {
                "calculable": True,
                "TOTAL_TARGET_ACQUISITION_COST": total,
                "TARGET_UNIT_ACQUISITION_COST": round(total / qty, 2) if qty else "UNKNOWN",
                "Target_profit": tp,
                "revenue_basis": revenue_basis,
                "revenue_basis_type": revenue_basis_type,
                "label": "SCENARIO_NOT_CURRENT_VERIFIED_REVENUE",
                "unknown_cost_components": [x for x, u in (("freight", freight_u), ("financing", financing_u)) if u],
            }
        else:
            target = calculate_acquisition_target(
                {"status": CURRENT_ESTIMATED_REVENUE, "primary_revenue": revenue_basis},
                row,
                quantity=qty,
            )
            target["revenue_basis_type"] = revenue_basis_type

    gap: dict[str, Any] = {
        "calculable": False,
        "Expected_profit_at_observed_price": "UNKNOWN",
        "Profit_target_status": STATUS_UNKNOWN,
        "status_annotation": None,
    }
    if revenue_basis is not None and _num(normalized_cost) is not None:
        expected_profit = round(revenue_basis - float(normalized_cost) - freight_used - financing_used - expenses_used, 2)
        status = classify_profit_target_status(
            projected_profit=expected_profit, target=target_profit_usd(row), has_cost=True
        )
        annotation = None
        if status in {STATUS_BELOW, STATUS_UNVIABLE}:
            status = STATUS_BELOW
            annotation = f"PUBLIC_ECONOMICS_BELOW_TARGET+{WHOLESALE_ACCESS_UNVERIFIED_SIGNAL}"
        obs_unit = _num(best.get("Observed_price")) if best else None
        tgt_unit = _num(target.get("TARGET_UNIT_ACQUISITION_COST"))
        unit_diff = round(obs_unit - tgt_unit, 2) if obs_unit is not None and tgt_unit is not None else "UNKNOWN"
        total_diff = round(float(unit_diff) * qty, 2) if isinstance(unit_diff, (int, float)) and qty else "UNKNOWN"
        gap = {
            "calculable": True,
            "Target_unit_cost": tgt_unit if tgt_unit is not None else "UNKNOWN",
            "Observed_unit_cost": obs_unit if obs_unit is not None else "UNKNOWN",
            "Unit_difference": unit_diff,
            "Quantity": qty if qty is not None else "UNKNOWN",
            "Total_difference": total_diff,
            "Target_profit": target_profit_usd(row),
            "Expected_profit_at_observed_price": expected_profit,
            "Margin_pct": round((expected_profit / revenue_basis) * 100, 2) if revenue_basis else "UNKNOWN",
            "Profit_target_status": status,
            "status_annotation": annotation,
            "Freight": freight if freight is not None else "UNKNOWN",
            "Financing": financing if financing is not None else "UNKNOWN",
            "Expenses": expenses_used,
            "unknown_cost_components": [x for x, u in (("freight", freight_u), ("financing", financing_u)) if u],
        }

    bid_scenarios = []
    if scenario and _num(scenario.get("unit_price_benchmark")) is not None and qty and _num(normalized_cost) is not None:
        base_u = float(scenario["unit_price_benchmark"])
        for bid_u in (round(base_u * 0.92, 2), round(base_u * 0.96, 2), base_u):
            rev = round(bid_u * qty, 2)
            bid_scenarios.append(
                {
                    "bid_unit_price": bid_u,
                    "scenario_revenue": rev,
                    "Expected_profit": round(rev - float(normalized_cost) - freight_used - financing_used - expenses_used, 2),
                    "label": "BID_PRICE_SCENARIO",
                    "not_current_verified_revenue": True,
                }
            )

    return {
        "QUANTITY_NORMALIZATION": norm,
        "NORMALIZED_ACQUISITION_COST": normalized_cost,
        "ACQUISITION_TARGET": target,
        "ACQUISITION_PRICE_GAP_PROFILE": gap,
        "BID_PRICE_SCENARIOS": bid_scenarios,
        "revenue_basis": revenue_basis if revenue_basis is not None else "UNKNOWN",
        "revenue_basis_type": revenue_basis_type,
    }


def assign_evidence_queue(line: dict[str, Any]) -> str:
    if not line.get("named_products_found"):
        return Q_NEEDS_PUBLIC_PRODUCT_EVIDENCE
    if not line.get("public_prices_found"):
        wh = [s.get("status") for s in (line.get("WHOLESALE_ACCESS_SIGNALS") or [])]
        if RFQ_PRICING_REQUIRED in wh:
            return Q_RFQ_PRICE_REQUIRED_FUTURE
        return Q_NEEDS_PUBLIC_PRICE
    if line.get("needs_government_revenue"):
        return Q_NEEDS_GOVERNMENT_REVENUE_EVIDENCE
    gap = line.get("ACQUISITION_PRICE_GAP_PROFILE") or {}
    if gap.get("status_annotation") and "WHOLESALE" in str(gap.get("status_annotation")):
        return Q_WHOLESALE_VERIFICATION_REQUIRED_FUTURE
    if gap.get("calculable"):
        return Q_READY_FOR_ECONOMICS
    return Q_OWNER_REVIEW


def analyze_line_public_evidence(
    row: dict[str, Any],
    profile: dict[str, Any],
    ledger: CostLedger,
    access_log: list[dict[str, Any]],
    *,
    allow_paid: bool = False,
) -> dict[str, Any]:
    existing = extract_evidence_candidates(row, profile)
    commercial = retrieve_commercial_seller_evidence(row, profile, ledger, access_log)
    revenue = retrieve_government_revenue_evidence(row, profile, ledger, access_log)

    paid = {"executed": False, "sellers": [], "prices": [], "government": []}
    if allow_paid and not commercial.get("prices"):
        paid = paid_commercial_research(row, profile, ledger)
        commercial["COMMERCIAL_SELLER_EVIDENCE"].extend(paid.get("sellers") or [])
        commercial["prices"].extend(paid.get("prices") or [])
        commercial["WHOLESALE_ACCESS_SIGNALS"].extend(paid.get("wholesale_signals") or [])
        for g in paid.get("government") or []:
            revenue.setdefault("sources", []).append(g)
            if revenue.get("status") == NO_USABLE_REVENUE_EVIDENCE:
                revenue["status"] = g.get("Evidence_type")
                revenue["primary"] = g

    named = list(commercial.get("named_products") or [])
    for c in existing:
        if c.get("candidate_origin") == "botanical_commercial_identity":
            named.append(c)
    prices = commercial.get("prices") or []
    econ = build_line_economics(row, profile, prices=prices, revenue=revenue)

    level_counts = {"LEVEL_1": 0, "LEVEL_2": 0, "LEVEL_3": 0, "LEVEL_4": 0, "LEVEL_5": 0}
    for p in prices:
        lvl = p.get("Pricing_level") or "LEVEL_5"
        if lvl in level_counts:
            level_counts[lvl] += 1
    if not prices:
        level_counts["LEVEL_5"] = 1

    if named and not prices:
        next_missing = "public acquisition price with UOM"
    elif not named:
        next_missing = "named commercial product with public URL"
    elif prices and revenue.get("status") == NO_USABLE_REVENUE_EVIDENCE:
        next_missing = "government revenue or historical unit-price benchmark"
    elif (econ.get("ACQUISITION_PRICE_GAP_PROFILE") or {}).get("calculable"):
        next_missing = "freight/financing verification and wholesale confirmation"
    else:
        next_missing = "compatible price + revenue basis"

    line = {
        "kind": "M3LinePublicPricingEvidence",
        "Opportunity": row.get("title"),
        "Opportunity_ID": row.get("canonical_id"),
        "Government_requirement": profile.get("Original_description") or profile.get("Resolved_product_name"),
        "Quantity": profile.get("Quantity") if _known(profile.get("Quantity")) else resolve_quantity(row),
        "COMMERCIAL_SELLER_EVIDENCE": commercial.get("COMMERCIAL_SELLER_EVIDENCE") or [],
        "named_products": named,
        "named_products_found": len(named) > 0,
        "public_prices_found": len(prices) > 0,
        "prices": prices,
        "PRICING_LEVEL_COUNTS": level_counts,
        "WHOLESALE_ACCESS_SIGNALS": commercial.get("WHOLESALE_ACCESS_SIGNALS") or [],
        "GOVERNMENT_REVENUE_EVIDENCE": revenue,
        "needs_government_revenue": revenue.get("status") == NO_USABLE_REVENUE_EVIDENCE,
        "QUANTITY_NORMALIZATION": econ.get("QUANTITY_NORMALIZATION"),
        "NORMALIZED_ACQUISITION_COST": econ.get("NORMALIZED_ACQUISITION_COST"),
        "ACQUISITION_TARGET": econ.get("ACQUISITION_TARGET"),
        "ACQUISITION_PRICE_GAP_PROFILE": econ.get("ACQUISITION_PRICE_GAP_PROFILE"),
        "BID_PRICE_SCENARIOS": econ.get("BID_PRICE_SCENARIOS"),
        "revenue_basis": econ.get("revenue_basis"),
        "revenue_basis_type": econ.get("revenue_basis_type"),
        "Next_missing_evidence": next_missing,
        "paid_research": {"executed": paid.get("executed"), "reason": paid.get("reason")},
        "DEVELOPMENT_NO_OUTREACH": True,
    }
    line["queue"] = assign_evidence_queue(line)
    return line


def _priority_rows(store: Any, *, limit: int) -> list[dict[str, Any]]:
    rows = [r for r in (store.all() if hasattr(store, "all") else []) if isinstance(r, dict)]

    def score(r: dict[str, Any]) -> tuple:
        title = str(r.get("title") or "")
        boost = 0
        for i, t in enumerate(PRIORITY_TITLES):
            if t.lower() in title.lower():
                boost = 300 - i * 20
                break
        ident = r.get("product_identity_resolution") or {}
        ready = int((ident.get("SUPPLIER_READINESS") or {}).get("Ready_with_specifications") or 0)
        return (-(boost + ready * 5), str(r.get("canonical_id") or ""))

    return sorted(rows, key=score)[: max(1, min(limit, 25))]


def analyze_public_pricing_evidence_top(
    store: Any,
    *,
    limit: int = 8,
    allow_paid_web: bool = False,
    paid_limit: int = 0,
    persist: bool = True,
    max_lines_per_opp: int = 2,
) -> dict[str, Any]:
    ranked = _priority_rows(store, limit=limit)
    ledger = CostLedger()
    access_log: list[dict[str, Any]] = []
    index = load_evidence_index() if persist else {"by_id": {}}
    by_id = index.setdefault("by_id", {})

    items = []
    paid_used = 0
    mfrs: set[str] = set()
    metrics = {
        "products_researched": 0,
        "named_products_found": 0,
        "sellers_found": 0,
        "public_prices_found": 0,
        "package_tiers_found": 0,
        "normalized_costs": 0,
        "levels": {"LEVEL_1": 0, "LEVEL_2": 0, "LEVEL_3": 0, "LEVEL_4": 0, "LEVEL_5": 0},
        "wholesale": {"volume": 0, "reseller": 0, "project": 0, "mfr_direct": 0, "rfq": 0, "unverified": 0},
        "gov": {
            CURRENT_VERIFIED_REVENUE: 0,
            CURRENT_GOVERNMENT_ESTIMATE: 0,
            HISTORICAL_EXACT_AWARD: 0,
            HISTORICAL_EXACT_UNIT_PRICE: 0,
            HISTORICAL_SAME_ITEM_BENCHMARK: 0,
            HISTORICAL_COMPARABLE_BENCHMARK_LABEL: 0,
            NO_USABLE_REVENUE_EVIDENCE: 0,
        },
        "econ": {
            "with_revenue_basis": 0,
            "with_acquisition_price": 0,
            "targets": 0,
            "profit_scenarios": 0,
            "above_10k": 0,
            "positive_below": 0,
            "negative_public": 0,
            "unknown": 0,
            "wholesale_verification_required": 0,
        },
    }
    real_results: dict[str, Any] = {}
    queues: dict[str, list] = defaultdict(list)

    for row in ranked:
        title = str(row.get("title") or "")
        is_priority = any(t.lower() in title.lower() for t in PRIORITY_TITLES)
        use_paid = allow_paid_web and paid_used < paid_limit and is_priority
        identity = _identity_bundle(row)
        profiles = _eligible_profiles(identity, row)[:max_lines_per_opp]
        if not profiles:
            continue

        lines = []
        for p in profiles:
            line = analyze_line_public_evidence(
                row, p, ledger, access_log, allow_paid=use_paid and paid_used < paid_limit
            )
            if use_paid and (line.get("paid_research") or {}).get("executed"):
                paid_used += 1
                use_paid = False
            lines.append(line)

            metrics["products_researched"] += 1
            if line.get("named_products_found"):
                metrics["named_products_found"] += 1
            metrics["sellers_found"] += len(line.get("COMMERCIAL_SELLER_EVIDENCE") or [])
            for s in line.get("COMMERCIAL_SELLER_EVIDENCE") or []:
                if _known(s.get("Manufacturer")) and s.get("Manufacturer") != "UNKNOWN":
                    mfrs.add(str(s["Manufacturer"]))
            if line.get("public_prices_found"):
                metrics["public_prices_found"] += 1
                metrics["econ"]["with_acquisition_price"] += 1
            for pobs in line.get("prices") or []:
                lvl = pobs.get("Pricing_level") or "LEVEL_5"
                if lvl in metrics["levels"]:
                    metrics["levels"][lvl] += 1
                if _known(pobs.get("Package_quantity")):
                    metrics["package_tiers_found"] += 1
            if not (line.get("prices") or []):
                metrics["levels"]["LEVEL_5"] += 1
            if _num(line.get("NORMALIZED_ACQUISITION_COST")) is not None:
                metrics["normalized_costs"] += 1
            for w in line.get("WHOLESALE_ACCESS_SIGNALS") or []:
                st = w.get("status")
                key = {
                    VOLUME_PRICING_EVIDENCED: "volume",
                    RESELLER_PROGRAM_EVIDENCED: "reseller",
                    PROJECT_PRICING_EVIDENCED: "project",
                    MANUFACTURER_DIRECT_EVIDENCED: "mfr_direct",
                    RFQ_PRICING_REQUIRED: "rfq",
                    WHOLESALE_ACCESS_UNVERIFIED_SIGNAL: "unverified",
                }.get(st)
                if key:
                    metrics["wholesale"][key] += 1
            gst = (line.get("GOVERNMENT_REVENUE_EVIDENCE") or {}).get("status")
            if gst in metrics["gov"]:
                metrics["gov"][gst] += 1
            else:
                metrics["gov"][NO_USABLE_REVENUE_EVIDENCE] += 1
            if _num(line.get("revenue_basis")) is not None:
                metrics["econ"]["with_revenue_basis"] += 1
            if (line.get("ACQUISITION_TARGET") or {}).get("calculable"):
                metrics["econ"]["targets"] += 1
            gap = line.get("ACQUISITION_PRICE_GAP_PROFILE") or {}
            if gap.get("calculable"):
                metrics["econ"]["profit_scenarios"] += 1
                ep = _num(gap.get("Expected_profit_at_observed_price"))
                if ep is None:
                    metrics["econ"]["unknown"] += 1
                elif ep >= target_profit_usd(row):
                    metrics["econ"]["above_10k"] += 1
                elif ep > 0:
                    metrics["econ"]["positive_below"] += 1
                else:
                    metrics["econ"]["negative_public"] += 1
                if gap.get("status_annotation") and "WHOLESALE" in str(gap.get("status_annotation")):
                    metrics["econ"]["wholesale_verification_required"] += 1
            else:
                metrics["econ"]["unknown"] += 1
            queues[line.get("queue") or Q_OWNER_REVIEW].append(
                {
                    "Opportunity": row.get("title"),
                    "canonical_id": row.get("canonical_id"),
                    "Requirement": line.get("Government_requirement"),
                    "Next_missing": line.get("Next_missing_evidence"),
                }
            )

        primary = lines[0]
        price0 = (primary.get("prices") or [{}])[0]
        seller0 = (primary.get("COMMERCIAL_SELLER_EVIDENCE") or [{}])[0]
        prod0 = (primary.get("named_products") or [{}])[0]
        gap = primary.get("ACQUISITION_PRICE_GAP_PROFILE") or {}
        card = {
            "Opportunity": row.get("title"),
            "Government_requirement": primary.get("Government_requirement"),
            "Quantity": primary.get("Quantity"),
            "Commercial_product": prod0.get("Commercial_product_name") or seller0.get("Product_name") or "UNKNOWN",
            "Manufacturer": seller0.get("Manufacturer") or "UNKNOWN",
            "Seller": seller0.get("Seller_name") or "UNKNOWN",
            "Compliance_confidence": prod0.get("Match_confidence") or MATCH_UNKNOWN,
            "Observed_price": price0.get("Observed_price") if price0 else "UNKNOWN",
            "Price_basis": price0.get("Price_basis") if price0 else "UNKNOWN",
            "Normalized_acquisition_cost": primary.get("NORMALIZED_ACQUISITION_COST"),
            "Government_revenue_evidence": (primary.get("GOVERNMENT_REVENUE_EVIDENCE") or {}).get("status"),
            "Revenue_evidence_type": (primary.get("GOVERNMENT_REVENUE_EVIDENCE") or {}).get("status"),
            "Target_acquisition_cost": (primary.get("ACQUISITION_TARGET") or {}).get("TOTAL_TARGET_ACQUISITION_COST"),
            "Observed_price_gap": gap.get("Total_difference"),
            "Expected_profit": gap.get("Expected_profit_at_observed_price"),
            "Financing_allowance_status": gap.get("Financing", "UNKNOWN"),
            "Wholesale_status": (primary.get("WHOLESALE_ACCESS_SIGNALS") or [{}])[0].get("status"),
            "Remaining_uncertainty": primary.get("Next_missing_evidence"),
            "Next_missing_evidence": primary.get("Next_missing_evidence"),
            "status_annotation": gap.get("status_annotation"),
        }
        for key in PRIORITY_TITLES:
            if key.lower() in title.lower() and key not in real_results:
                real_results[key] = card

        result = {
            "kind": "M3PublicPricingEvidence",
            "Opportunity_ID": row.get("canonical_id"),
            "Opportunity": row.get("title"),
            "LINE_EVIDENCE": lines,
            "DEVELOPMENT_NO_OUTREACH": True,
        }
        items.append(result)
        cid = row.get("canonical_id")
        if cid and hasattr(store, "_rows"):
            existing = store._rows.get(cid) or dict(row)
            existing["public_pricing_evidence_full"] = result
            existing["public_pricing_evidence"] = {
                "kind": "M3PublicPricingEvidenceSummary",
                "public_prices_found": any(li.get("public_prices_found") for li in lines),
                "normalized_cost": primary.get("NORMALIZED_ACQUISITION_COST"),
                "expected_profit": gap.get("Expected_profit_at_observed_price"),
                "Next_missing_evidence": primary.get("Next_missing_evidence"),
            }
            store._rows[cid] = existing
            by_id[str(cid)] = {"summary": existing["public_pricing_evidence"], "updated_at": _utc()}

    if persist:
        try:
            if hasattr(store, "save"):
                store.save()
        except Exception:
            pass
        save_evidence_index(index)

    access_counts: dict[str, int] = defaultdict(int)
    for a in access_log:
        access_counts[str(a.get("access_state") or "UNKNOWN")] += 1

    return {
        "kind": "M3PublicPricingEvidenceRun",
        "analyzed": len(items),
        "BASELINE": BASELINE_SNAPSHOT,
        "PUBLIC_COMMERCIAL_EVIDENCE": {
            "Products_researched": metrics["products_researched"],
            "Named_products_found": metrics["named_products_found"],
            "Manufacturers_found": len(mfrs),
            "Actual_sellers_found": metrics["sellers_found"],
            "Public_prices_found": metrics["public_prices_found"],
            "Package_quantity_tiers_found": metrics["package_tiers_found"],
            "Normalized_acquisition_costs": metrics["normalized_costs"],
        },
        "PRICING_LEVEL": metrics["levels"],
        "WHOLESALE_SIGNALS": {
            "Volume_pricing": metrics["wholesale"]["volume"],
            "Reseller_dealer_programs": metrics["wholesale"]["reseller"],
            "Project_special_pricing": metrics["wholesale"]["project"],
            "Manufacturer_direct": metrics["wholesale"]["mfr_direct"],
            "RFQ_required": metrics["wholesale"]["rfq"],
            "Wholesale_unverified": metrics["wholesale"]["unverified"],
        },
        "GOVERNMENT_PRICING_EVIDENCE": metrics["gov"],
        "ECONOMICS": metrics["econ"],
        "REAL_DEAL_RESULTS": real_results,
        "queues": {k: v[:20] for k, v in queues.items()},
        "COST": ledger.as_dict(),
        "ACCESS": dict(access_counts),
        "SAFETY": {"Outreach": 0, "Registrations": 0, "Quotes_requested": 0, "Bids_submitted": 0, "Purchases": 0},
        "items": items,
        "NEXT_STATE": "PUBLIC_PRICING_AND_REVENUE_EVIDENCE_OPERATIONAL",
        "DEVELOPMENT_NO_OUTREACH": True,
    }


def deal_room_public_pricing_section(row: dict[str, Any]) -> dict[str, Any]:
    full = row.get("public_pricing_evidence_full")
    if not isinstance(full, dict) or full.get("kind") != "M3PublicPricingEvidence":
        try:
            ledger = CostLedger()
            access: list[dict[str, Any]] = []
            profiles = _eligible_profiles(_identity_bundle(row), row)[:1]
            lines = [analyze_line_public_evidence(row, p, ledger, access, allow_paid=False) for p in profiles]
            full = {"kind": "M3PublicPricingEvidence", "LINE_EVIDENCE": lines}
        except Exception:
            full = {"LINE_EVIDENCE": []}
    primary = (full.get("LINE_EVIDENCE") or [{}])[0]
    gap = primary.get("ACQUISITION_PRICE_GAP_PROFILE") or {}
    rev = primary.get("GOVERNMENT_REVENUE_EVIDENCE") or {}
    seller = (primary.get("COMMERCIAL_SELLER_EVIDENCE") or [{}])[0]
    price = (primary.get("prices") or [{}])[0]
    prod = (primary.get("named_products") or [{}])[0]
    return {
        "kind": "M3DealRoomPublicPricingEvidence",
        "GOVERNMENT_SIDE": {
            "Requirement": primary.get("Government_requirement") or row.get("title"),
            "Quantity": primary.get("Quantity"),
            "Revenue_evidence": rev.get("status"),
            "Historical_unit_prices": [
                s.get("Unit_price") for s in (rev.get("sources") or []) if _num(s.get("Unit_price")) is not None
            ][:5],
            "Revenue_scenario": rev.get("HISTORICAL_REVENUE_BENCHMARK_SCENARIO"),
        },
        "COMMERCIAL_SIDE": {
            "Candidate_product": prod.get("Commercial_product_name") or seller.get("Product_name"),
            "Seller": seller.get("Seller_name"),
            "Package_size": seller.get("Package_size"),
            "Public_price": price.get("Observed_price") if price else "UNKNOWN",
            "Normalized_acquisition_cost": primary.get("NORMALIZED_ACQUISITION_COST"),
            "Pricing_level": price.get("Pricing_level") if price else "LEVEL_5",
            "Wholesale_signals": primary.get("WHOLESALE_ACCESS_SIGNALS"),
        },
        "ECONOMICS": {
            "Target_acquisition_price": (primary.get("ACQUISITION_TARGET") or {}).get("TOTAL_TARGET_ACQUISITION_COST"),
            "Observed_acquisition_price": primary.get("NORMALIZED_ACQUISITION_COST"),
            "Gap": gap.get("Total_difference"),
            "Expected_profit": gap.get("Expected_profit_at_observed_price"),
            "Profit_margin": gap.get("Margin_pct"),
            "Freight_uncertainty": gap.get("Freight") == "UNKNOWN"
            or "freight" in (gap.get("unknown_cost_components") or []),
            "Financing_uncertainty": gap.get("Financing") == "UNKNOWN"
            or "financing" in (gap.get("unknown_cost_components") or []),
            "status_annotation": gap.get("status_annotation"),
        },
        "NEXT_MISSING_EVIDENCE": primary.get("Next_missing_evidence"),
        "VA_allowed_actions": sorted(VA_ALLOWED),
        "VA_forbidden_actions": sorted(VA_FORBIDDEN),
        "DEVELOPMENT_NO_OUTREACH": True,
    }


def build_va_evidence_queues(limit: int = 25) -> dict[str, Any]:
    idx = load_evidence_index()
    return {
        "kind": "M3PublicPricingEvidenceQueues",
        "queues": {
            Q_NEEDS_PUBLIC_PRODUCT_EVIDENCE: [],
            Q_NEEDS_PUBLIC_PRICE: [],
            Q_NEEDS_GOVERNMENT_REVENUE_EVIDENCE: [],
            Q_RFQ_PRICE_REQUIRED_FUTURE: [],
            Q_WHOLESALE_VERIFICATION_REQUIRED_FUTURE: [],
            Q_READY_FOR_ECONOMICS: [],
            Q_OWNER_REVIEW: [],
        },
        "researched": len(idx.get("by_id") or {}),
        "VA_allowed_actions": sorted(VA_ALLOWED),
        "VA_forbidden_actions": sorted(VA_FORBIDDEN),
        "limit": limit,
        "DEVELOPMENT_NO_OUTREACH": True,
    }


def apply_va_evidence_update(
    store: Any,
    canonical_id: str,
    *,
    action: str,
    note: str | None = None,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    action_u = str(action or "").upper().strip()
    if action_u not in VA_ALLOWED:
        return {
            "ok": False,
            "error": "action_not_allowed",
            "VA_allowed_actions": sorted(VA_ALLOWED),
            "VA_forbidden_actions": sorted(VA_FORBIDDEN),
        }
    row = store._rows.get(canonical_id) if hasattr(store, "_rows") else None
    if not isinstance(row, dict):
        return {"ok": False, "error": "opportunity_not_found"}
    notes = list(row.get("public_pricing_va_notes") or [])
    if action_u in {"ADD_NOTES", "NOTE"} and note:
        notes.append({"at": _utc(), "note": str(note)[:2000], "action": action_u})
    if action_u == "ATTACH_EVIDENCE" and isinstance(evidence, dict):
        amt = _num(evidence.get("unit_price") or evidence.get("Observed_price"))
        if amt is not None:
            cp = dict(row.get("commercial_pricing") or {})
            cp["public_unit_price"] = amt
            cp["public_source"] = evidence.get("url") or evidence.get("Source")
            row["commercial_pricing"] = cp
        if _num(evidence.get("award_amount") or evidence.get("Revenue_value")) is not None:
            row["historical_award_amount"] = _num(evidence.get("award_amount") or evidence.get("Revenue_value"))
        notes.append({"at": _utc(), "note": note or "evidence_attached", "action": action_u})
    if action_u in {"SEARCH_PUBLIC_SOURCES", "NORMALIZE_PRICING", "RESEARCH_AWARD_HISTORY"}:
        notes.append({"at": _utc(), "note": note or action_u, "action": action_u})
    row["public_pricing_va_notes"] = notes[-30:]
    if hasattr(store, "_rows"):
        store._rows[canonical_id] = row
    if hasattr(store, "save"):
        try:
            store.save()
        except Exception:
            pass
    return {"ok": True, "canonical_id": canonical_id, "action": action_u}
