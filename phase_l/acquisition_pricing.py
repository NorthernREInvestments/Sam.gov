"""Phase L.2.7 — resilient acquisition pricing: discovery ≠ retrieval ≠ verification ≠ usability."""

from __future__ import annotations

import json
import re
import statistics
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from application_clock import now_utc
from phase_l.convergence import (
    DEALER_ADVERTISED,
    OEM_MSRP,
    PUBLIC_CATALOG,
    PUBLIC_RETAIL,
    classify_price_access,
)
from phase_l.market_price import DOMAIN_HINTS, _domain_search_url, extract_prices_from_page
from phase_l.pricing_sources import text_has_identity
from phase_l.product_page_resolution import (
    EXACT_VERIFIED,
    STRONG_VERIFIED,
    resolve_and_verify_market_price,
)
from phase_l.resilient_fetch import (
    FETCH_403,
    FETCH_429,
    FETCH_BOT_BLOCKED,
    FETCH_JS_EMPTY,
    FETCH_OK,
    FETCH_SKIPPED_CIRCUIT,
    FETCH_TIMEOUT,
    DomainCircuitBreaker,
    domain_of,
    resilient_fetch,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE_LEARNING_PATH = ROOT / "data" / "phase_l27_source_learning.json"
PRICE_MEMORY_PATH = ROOT / "data" / "phase_l27_price_memory.json"

# PriceLead states
UNVERIFIED_LEAD = "UNVERIFIED_LEAD"
VERIFIED = "VERIFIED"
REJECTED = "REJECTED"
FETCH_BLOCKED = "FETCH_BLOCKED"
NEEDS_MANUAL_OR_ALTERNATE = "NEEDS_MANUAL_OR_ALTERNATE"

# Failure taxonomy (not collapsed to CURRENT_PRICE_NOT_FOUND)
NO_PRICE_INFORMATION = "NO_PRICE_INFORMATION"
PRICE_LEAD_FETCH_BLOCKED = "PRICE_LEAD_FETCH_BLOCKED"
PAGE_IDENTITY_MISMATCH = "PAGE_IDENTITY_MISMATCH"
PAGE_FETCHED_NO_PRICE = "PAGE_FETCHED_NO_PRICE"
PRICE_VERIFIED_INACCESSIBLE = "PRICE_VERIFIED_INACCESSIBLE"
STRONG_PRICE_LEAD_REQUIRES_VERIFICATION = "STRONG_PRICE_LEAD_REQUIRES_VERIFICATION"

QUEUE_MANUAL_PRICE = "MANUAL_PRICE_VERIFICATION_PRIORITY"

_DOLLAR = re.compile(
    r"\$\s*("
    r"\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?"
    r"|\d{1,3}(?:,\d{3})*\.\d{2}"
    r"|\d{4,8}(?:\.\d{1,2})?"
    r")"
)


def _utc() -> str:
    return now_utc().isoformat()


def _f(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _num_dollar(s: str) -> float | None:
    m = _DOLLAR.search(s or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


@dataclass
class PriceLead:
    target_identity: str | None = None
    observed_identity: str | None = None
    seller: str | None = None
    apparent_price: float | None = None
    source_url: str | None = None
    source_type: str = "WEB"
    evidence_text: str | None = None
    discovered_at: str = field(default_factory=_utc)
    confidence: str = "MEDIUM"
    verification_status: str = UNVERIFIED_LEAD
    fetch_status: str | None = None
    price_type: str | None = None
    price_access: str | None = None
    economics_eligible: bool = False
    failure_class: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Category → direct sources
# ---------------------------------------------------------------------------
def infer_product_family(row: dict[str, Any], commercial: dict[str, Any] | None = None) -> str:
    commercial = commercial or {}
    blob = " ".join(
        str(x or "")
        for x in (
            row.get("title"),
            commercial.get("manufacturer"),
            commercial.get("model"),
            commercial.get("commercial_identity_state"),
        )
    ).lower()
    st = str(commercial.get("commercial_identity_state") or "")
    if any(x in blob for x in ("ford", "police", "f-150", "expedition", "vehicle", "suv", "truck")) or "VEHICLE" in st:
        return "VEHICLE"
    if any(x in blob for x in ("bobcat", "toolcat", "excavator", "caterpillar", "loader", "tractor")) or "EQUIPMENT" in st:
        return "EQUIPMENT"
    if any(x in blob for x in ("dell", "asus", "lenovo", "hp ", "server", "laptop", "poweredge", "latitude")):
        return "IT"
    if any(x in blob for x in ("fluke", "canon", "meter", "oscilloscope", "multimeter")):
        return "TEST_EQUIP"
    if any(x in blob for x in ("fisher", "lab", "pipette", "spectrophotometer")):
        return "LAB"
    if commercial.get("mpn") or commercial.get("nsn") or "nsn" in blob:
        return "MRO"
    return "MRO"


FAMILY_SOURCES: dict[str, list[tuple[str, str]]] = {
    "IT": [
        ("dell.com", "OEM"),
        ("cdw.com", "DISTRIBUTOR"),
        ("cdw-g.com", "DISTRIBUTOR"),
        ("newegg.com", "RESELLER"),
        ("bhphotovideo.com", "RESELLER"),
        ("insight.com", "DISTRIBUTOR"),
        ("connection.com", "DISTRIBUTOR"),
        ("shi.com", "DISTRIBUTOR"),
    ],
    "EQUIPMENT": [
        ("bobcat.com", "OEM"),
        ("machinerytrader.com", "DEALER"),
        ("sourcewell-mn.gov", "COOPERATIVE"),
        ("grainger.com", "DISTRIBUTOR"),
        ("fastenal.com", "DISTRIBUTOR"),
    ],
    "VEHICLE": [
        ("ford.com", "OEM"),
        ("sourcewell-mn.gov", "COOPERATIVE"),
        ("naspovaluepoint.org", "COOPERATIVE"),
    ],
    "TEST_EQUIP": [
        ("fluke.com", "OEM"),
        ("grainger.com", "DISTRIBUTOR"),
        ("zoro.com", "DISTRIBUTOR"),
        ("mscdirect.com", "DISTRIBUTOR"),
        ("tequipment.net", "DEALER"),
    ],
    "LAB": [
        ("fishersci.com", "DISTRIBUTOR"),
        ("coleparmer.com", "DISTRIBUTOR"),
        ("vwr.com", "DISTRIBUTOR"),
    ],
    "MRO": [
        ("grainger.com", "DISTRIBUTOR"),
        ("zoro.com", "DISTRIBUTOR"),
        ("mscdirect.com", "DISTRIBUTOR"),
        ("fastenal.com", "DISTRIBUTOR"),
        ("mcmaster.com", "DISTRIBUTOR"),
        ("digikey.com", "DISTRIBUTOR"),
        ("mouser.com", "DISTRIBUTOR"),
    ],
}


def direct_source_targets(
    *,
    search_id: dict[str, Any],
    family: str,
    learning: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Generate likely source URLs without search engines."""
    keys = []
    for k in (search_id.get("primary_mpn"), search_id.get("model"), search_id.get("sku")):
        if k and str(k).strip():
            keys.append(str(k).strip())
    for v in (search_id.get("mpn_variants") or [])[:2]:
        if v and str(v) not in keys:
            keys.append(str(v))
    if not keys:
        return []

    sources = list(FAMILY_SOURCES.get(family) or FAMILY_SOURCES["MRO"])
    # Prefer historically productive domains for this family
    if learning:
        ranked = learning.get("by_family", {}).get(family) or {}
        sources = sorted(
            sources,
            key=lambda t: -float((ranked.get(t[0]) or {}).get("usable_price_rate") or 0),
        )

    out: list[dict[str, Any]] = []
    for domain, stype in sources[:8]:
        for key in keys[:2]:
            u = _domain_search_url(domain, key)
            if not u:
                # generic search path
                u = f"https://www.{domain}/search?q={quote_plus(key)}"
            # Prefer static catalog hints
            prefer_static = []
            if stype in {"COOPERATIVE", "OEM"}:
                prefer_static.append(
                    {
                        "url": f"https://www.{domain}/search?q={quote_plus(key)}&filetype=pdf",
                        "source_type": stype,
                        "domain": domain,
                        "prefer_static": True,
                        "key": key,
                    }
                )
            out.append(
                {
                    "url": u,
                    "source_type": stype,
                    "domain": domain,
                    "prefer_static": False,
                    "key": key,
                }
            )
            out.extend(prefer_static)
    # de-dupe by url
    seen: set[str] = set()
    uniq = []
    for t in out:
        if t["url"] not in seen:
            seen.add(t["url"])
            uniq.append(t)
    return uniq[:16]


# ---------------------------------------------------------------------------
# Source learning + price memory
# ---------------------------------------------------------------------------
def load_json(path: Path) -> dict[str, Any]:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def record_source_outcome(
    learning: dict[str, Any],
    *,
    family: str,
    domain: str,
    verified: bool,
    usable: bool,
    blocked: bool,
    identity_hit: bool,
) -> None:
    by_f = learning.setdefault("by_family", {}).setdefault(family, {})
    rec = by_f.setdefault(
        domain,
        {
            "attempts": 0,
            "verified_prices": 0,
            "usable_prices": 0,
            "blocks": 0,
            "identity_hits": 0,
        },
    )
    rec["attempts"] += 1
    if verified:
        rec["verified_prices"] += 1
    if usable:
        rec["usable_prices"] += 1
    if blocked:
        rec["blocks"] += 1
    if identity_hit:
        rec["identity_hits"] += 1
    a = max(1, rec["attempts"])
    rec["usable_price_rate"] = round(rec["usable_prices"] / a, 4)
    rec["bot_block_rate"] = round(rec["blocks"] / a, 4)
    rec["identity_match_rate"] = round(rec["identity_hits"] / a, 4)


def memory_key(search_id: dict[str, Any]) -> str | None:
    parts = [
        search_id.get("manufacturer"),
        search_id.get("model") or search_id.get("primary_mpn"),
        search_id.get("primary_mpn"),
    ]
    key = "|".join(str(p).strip().upper() for p in parts if p)
    return key if len(key) >= 4 else None


def recall_price(mem: dict[str, Any], key: str | None, *, max_age_hours: float = 72.0) -> dict[str, Any] | None:
    if not key:
        return None
    hit = (mem.get("prices") or {}).get(key)
    if not hit:
        return None
    # freshness: simple string compare skip if missing timestamp
    return hit


def remember_price(mem: dict[str, Any], key: str | None, payload: dict[str, Any]) -> None:
    if not key:
        return
    prices = mem.setdefault("prices", {})
    prev = dict(prices.get(key) or {})
    prev.update({k: v for k, v in payload.items() if v is not None})
    prev["updated_at"] = _utc()
    prices[key] = prev


# ---------------------------------------------------------------------------
# Ranges + preliminary bid window
# ---------------------------------------------------------------------------
def acquisition_range(prices: list[float]) -> dict[str, Any] | None:
    vals = sorted(p for p in prices if isinstance(p, (int, float)) and p > 0)
    if not vals:
        return None
    return {
        "low": vals[0],
        "median": float(statistics.median(vals)),
        "high": vals[-1],
        "n": len(vals),
    }


def preliminary_bid_window(
    *,
    hist_low: float | None,
    hist_median: float | None,
    hist_recent: float | None,
    hist_high: float | None,
    acq_low: float | None,
    acq_median: float | None,
    acq_high: float | None,
) -> dict[str, Any] | None:
    """RECON_ONLY ballpark — not a bid recommendation."""
    if hist_median is None and hist_recent is None and hist_low is None:
        return None
    if acq_median is None and acq_low is None:
        return None
    h_lo = hist_low or hist_median or hist_recent
    h_hi = hist_high or hist_median or hist_recent
    h_med = hist_median or hist_recent or hist_low
    a_lo = acq_low or acq_median
    a_hi = acq_high or acq_median
    a_med = acq_median or acq_low
    if not all(isinstance(x, (int, float)) for x in (h_lo, h_hi, h_med, a_lo, a_hi, a_med)):
        return None
    # Competitive window near historical median, slightly under high
    bid_lo = round(min(h_med * 0.92, h_hi * 0.95), 2)
    bid_hi = round(max(h_med * 1.02, h_lo * 1.05), 2)
    if bid_lo > bid_hi:
        bid_lo, bid_hi = bid_hi, bid_lo
    gross_lo = round(bid_lo - a_hi, 2)
    gross_hi = round(bid_hi - a_lo, 2)
    return {
        "label": "RECON_ONLY",
        "kind": "PRELIMINARY_BID_WINDOW",
        "historical": {"low": h_lo, "median": h_med, "recent": hist_recent, "high": h_hi},
        "acquisition": {"low": a_lo, "median": a_med, "high": a_hi},
        "preliminary_competitive_window": {"low": bid_lo, "high": bid_hi},
        "potential_gross": {"low": min(gross_lo, gross_hi), "high": max(gross_lo, gross_hi)},
        "optimistic_spread": round(h_hi - a_lo, 2),
        "median_spread": round(h_med - a_med, 2),
        "conservative_spread": round(h_lo - a_hi, 2),
    }


# ---------------------------------------------------------------------------
# Core research
# ---------------------------------------------------------------------------
def _search_id_from(commercial: dict[str, Any], identity: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    return {
        "manufacturer": commercial.get("manufacturer") or identity.get("manufacturer"),
        "model": commercial.get("model") or identity.get("model"),
        "primary_mpn": commercial.get("mpn") or identity.get("mpn") or identity.get("primary_mpn"),
        "sku": identity.get("sku"),
        "mpn_variants": list(identity.get("mpn_variants") or [])[:4],
        "nsn": commercial.get("nsn") or identity.get("nsn"),
        "title": row.get("title"),
    }


def _lead_from_snippet(
    *,
    search_id: dict[str, Any],
    url: str,
    source_type: str,
    text: str,
    fetch_status: str | None = None,
) -> PriceLead | None:
    ok, matched = text_has_identity(text[:8000], search_id)
    if not ok:
        return None
    price = _num_dollar(text[:8000])
    if price is None or price < 5 or price > 5_000_000:
        return None
    if re.search(r"\b(used|refurbished|auction|salvage|/mo|monthly)\b", text[:4000], re.I):
        return None
    strong = bool(matched) and price is not None
    status = UNVERIFIED_LEAD
    failure = None
    if fetch_status in {FETCH_403, FETCH_429, FETCH_BOT_BLOCKED, FETCH_TIMEOUT, FETCH_JS_EMPTY}:
        status = FETCH_BLOCKED
        failure = PRICE_LEAD_FETCH_BLOCKED
    return PriceLead(
        target_identity=search_id.get("model") or search_id.get("primary_mpn"),
        observed_identity=matched,
        seller=domain_of(url),
        apparent_price=price,
        source_url=url,
        source_type=source_type,
        evidence_text=re.sub(r"\s+", " ", text[:240]),
        confidence="HIGH" if strong else "MEDIUM",
        verification_status=status,
        fetch_status=fetch_status,
        failure_class=failure or STRONG_PRICE_LEAD_REQUIRES_VERIFICATION,
        economics_eligible=False,
    )


def research_acquisition_price(
    *,
    row: dict[str, Any],
    commercial: dict[str, Any],
    identity: dict[str, Any],
    breaker: DomainCircuitBreaker,
    learning: dict[str, Any],
    memory: dict[str, Any],
    max_fetches: int = 8,
    allow_bing_fallback: bool = True,
) -> dict[str, Any]:
    """Multi-source discovery → safe fetch → verify → classify. Never hang."""
    search_id = _search_id_from(commercial, identity, row)
    family = infer_product_family(row, commercial)
    key = memory_key(search_id)
    tele = {
        "discovery_targets": 0,
        "fetches": 0,
        "fetch_ok": 0,
        "bot_blocks": 0,
        "timeouts": 0,
        "exact_product_pages": 0,
        "verified_prices": 0,
        "usable_prices": 0,
        "leads": 0,
        "strong_leads": 0,
    }

    cached = recall_price(memory, key)
    if cached and cached.get("verified_price"):
        tele["verified_prices"] = 1
        tele["usable_prices"] = 1 if cached.get("economics_eligible") else 0
        return {
            "kind": "PhaseL27AcquisitionResearch",
            "family": family,
            "cache_hit": True,
            "leads": [],
            "verified": [cached],
            "usable": [cached] if cached.get("economics_eligible") else [],
            "acquisition_range": acquisition_range([cached["verified_price"]]),
            "failure_class": None,
            "telemetry": tele,
            "source_url": cached.get("source_url"),
        }

    targets = direct_source_targets(search_id=search_id, family=family, learning=learning)
    # Optional Bing fallback (bounded, not backbone)
    if allow_bing_fallback and len(targets) < 4:
        try:
            from phase_l.market_price import bing_search_urls

            q = search_id.get("model") or search_id.get("primary_mpn")
            if q:
                urls, status = bing_search_urls(f'"{q}" price', limit=3)
                if status == "OK":
                    for u in urls:
                        targets.append(
                            {"url": u, "source_type": "SERP", "domain": domain_of(u), "prefer_static": False, "key": q}
                        )
        except Exception:
            pass

    tele["discovery_targets"] = len(targets)
    leads: list[PriceLead] = []
    verified: list[dict[str, Any]] = []
    usable: list[dict[str, Any]] = []
    fetch_log: list[dict[str, Any]] = []
    failure_classes: list[str] = []

    # Prefer static-ish extensions first
    targets = sorted(targets, key=lambda t: (0 if t.get("prefer_static") or str(t.get("url", "")).endswith((".pdf", ".xlsx", ".csv")) else 1))

    for t in targets:
        if tele["fetches"] >= max_fetches:
            break
        url = t["url"]
        if not breaker.allow(url):
            fetch_log.append({"url": url, "status": FETCH_SKIPPED_CIRCUIT})
            continue
        fr = resilient_fetch(url, breaker=breaker, retries=0)
        tele["fetches"] += 1
        fetch_log.append(fr.to_dict())
        blocked = fr.status in {FETCH_403, FETCH_429, FETCH_BOT_BLOCKED, FETCH_TIMEOUT, FETCH_JS_EMPTY}
        if fr.status == FETCH_TIMEOUT:
            tele["timeouts"] += 1
        if blocked:
            tele["bot_blocks"] += 1
            # Still try to pull a lead from any returned text
            lead = _lead_from_snippet(
                search_id=search_id,
                url=url,
                source_type=t.get("source_type") or "WEB",
                text=fr.text or "",
                fetch_status=fr.status,
            )
            if lead:
                leads.append(lead)
                tele["leads"] += 1
                if lead.confidence == "HIGH":
                    tele["strong_leads"] += 1
                    failure_classes.append(PRICE_LEAD_FETCH_BLOCKED)
            record_source_outcome(
                learning, family=family, domain=fr.domain, verified=False, usable=False, blocked=True, identity_hit=False
            )
            continue

        if fr.status != FETCH_OK:
            failure_classes.append(NO_PRICE_INFORMATION)
            record_source_outcome(
                learning, family=family, domain=fr.domain, verified=False, usable=False, blocked=False, identity_hit=False
            )
            continue

        tele["fetch_ok"] += 1
        # Prefer resolve_and_verify for HTML
        ctype = fr.content_type.lower()
        is_pdf = "pdf" in ctype or fr.content[:4] == b"%PDF" or url.lower().endswith(".pdf")
        identity_hit = False
        price_found = False

        if is_pdf:
            from phase_l.pricing_sources import extract_pdf_text, parse_pdf_price_rows

            pdf_text = extract_pdf_text(fr.content)
            rows = parse_pdf_price_rows(pdf_text, search_id=search_id, source_url=url)
            for rec in rows:
                identity_hit = True
                tele["exact_product_pages"] += 1
                if rec.price:
                    price_found = True
                    access = classify_price_access(
                        price_type=rec.acquisition_price_type or PUBLIC_CATALOG,
                        url=url,
                        evidence_text=rec.evidence_text,
                    )
                    item = {
                        "verified_price": rec.price,
                        "source_url": url,
                        "seller": domain_of(url),
                        "price_type": rec.acquisition_price_type or PUBLIC_CATALOG,
                        "confidence": EXACT_VERIFIED,
                        "economics_eligible": bool(access.get("economics_eligible")),
                        "price_access": access.get("price_access"),
                        "evidence_text": rec.evidence_text,
                    }
                    verified.append(item)
                    tele["verified_prices"] += 1
                    if item["economics_eligible"]:
                        usable.append(item)
                        tele["usable_prices"] += 1
                    else:
                        failure_classes.append(PRICE_VERIFIED_INACCESSIBLE)
            if not rows:
                ok, _ = text_has_identity(pdf_text[:20000], search_id)
                if ok:
                    identity_hit = True
                    failure_classes.append(PAGE_FETCHED_NO_PRICE)
                else:
                    failure_classes.append(PAGE_IDENTITY_MISMATCH)
        else:
            # HTML path — L.2.2 verification intact
            try:
                resolved = resolve_and_verify_market_price(
                    html=fr.text,
                    url=url,
                    search_id=search_id,
                    row=row,
                )
            except Exception:
                resolved = None

            if resolved is None or resolved.get("drop_reason") in {"SEARCH_PAGE_ONLY", "UNRELATED"}:
                # Search shells: try lead extraction only
                if resolved and resolved.get("drop_reason") == "SEARCH_PAGE_ONLY":
                    lead = _lead_from_snippet(
                        search_id=search_id,
                        url=url,
                        source_type=t.get("source_type") or "WEB",
                        text=fr.text,
                        fetch_status=FETCH_OK,
                    )
                    if lead:
                        leads.append(lead)
                        tele["leads"] += 1
                    failure_classes.append(PAGE_FETCHED_NO_PRICE)
                    record_source_outcome(
                        learning,
                        family=family,
                        domain=fr.domain,
                        verified=False,
                        usable=False,
                        blocked=False,
                        identity_hit=False,
                    )
                    continue
                ok, matched = text_has_identity(fr.text[:20000], search_id)
                if not ok:
                    failure_classes.append(PAGE_IDENTITY_MISMATCH)
                    record_source_outcome(
                        learning,
                        family=family,
                        domain=fr.domain,
                        verified=False,
                        usable=False,
                        blocked=False,
                        identity_hit=False,
                    )
                    continue
                identity_hit = True
                tele["exact_product_pages"] += 1
                prices = extract_prices_from_page(fr.text, source_url=url, product_hint=str(search_id.get("model") or ""))
                if not prices:
                    failure_classes.append(PAGE_FETCHED_NO_PRICE)
                    # lead from page text
                    lead = _lead_from_snippet(
                        search_id=search_id,
                        url=url,
                        source_type=t.get("source_type") or "WEB",
                        text=fr.text,
                        fetch_status=FETCH_OK,
                    )
                    if lead:
                        leads.append(lead)
                        tele["leads"] += 1
                else:
                    for p in prices[:3]:
                        price = _f(p.get("price") or p.get("unit_price"))
                        if not price:
                            continue
                        price_found = True
                        ptype = OEM_MSRP if t.get("source_type") == "OEM" else (
                            DEALER_ADVERTISED if t.get("source_type") == "DEALER" else PUBLIC_RETAIL
                        )
                        access = classify_price_access(price_type=ptype, url=url, evidence_text=p.get("evidence_text"))
                        conf = STRONG_VERIFIED
                        item = {
                            "verified_price": price,
                            "source_url": url,
                            "seller": domain_of(url),
                            "price_type": ptype,
                            "confidence": conf,
                            "economics_eligible": bool(access.get("economics_eligible")),
                            "price_access": access.get("price_access"),
                            "evidence_text": (p.get("evidence_text") or "")[:240],
                        }
                        # Fallback path is strong but not EXACT_VERIFIED unless page resolver agreed
                        verified.append(item)
                        tele["verified_prices"] += 1
                        if item["economics_eligible"] and conf in {EXACT_VERIFIED, STRONG_VERIFIED}:
                            usable.append(item)
                            tele["usable_prices"] += 1
            else:
                # Use resolver output
                if resolved.get("identity_match") and not resolved.get("drop_reason"):
                    identity_hit = True
                    tele["exact_product_pages"] += 1
                elif resolved.get("drop_reason") == "PRODUCT_IDENTITY_MISMATCH":
                    failure_classes.append(PAGE_IDENTITY_MISMATCH)
                elif resolved.get("drop_reason") == "NO_PRICE_ON_MATCHING_PAGE":
                    identity_hit = True
                    tele["exact_product_pages"] += 1
                    failure_classes.append(PAGE_FETCHED_NO_PRICE)
                for ev in resolved.get("evidence") or []:
                    price = _f(ev.get("price") or ev.get("unit_price"))
                    if not price:
                        continue
                    if ev.get("confidence") not in {EXACT_VERIFIED, STRONG_VERIFIED} and not ev.get("economics_eligible"):
                        # retain as lead only
                        leads.append(
                            PriceLead(
                                target_identity=search_id.get("model") or search_id.get("primary_mpn"),
                                observed_identity=str((resolved.get("identity_match") or {}).get("matched_value") or ""),
                                seller=domain_of(url),
                                apparent_price=price,
                                source_url=url,
                                source_type=t.get("source_type") or "WEB",
                                evidence_text=(ev.get("evidence_text") or "")[:240],
                                confidence="MEDIUM",
                                verification_status=UNVERIFIED_LEAD,
                                economics_eligible=False,
                            )
                        )
                        tele["leads"] += 1
                        continue
                    price_found = True
                    access = classify_price_access(
                        price_type=ev.get("acquisition_price_type") or PUBLIC_RETAIL,
                        url=url,
                        evidence_text=ev.get("evidence_text"),
                    )
                    item = {
                        "verified_price": price,
                        "source_url": url,
                        "seller": domain_of(url),
                        "price_type": ev.get("acquisition_price_type") or PUBLIC_RETAIL,
                        "confidence": ev.get("confidence") or STRONG_VERIFIED,
                        "economics_eligible": bool(ev.get("economics_eligible", True)) and access["economics_eligible"],
                        "price_access": access.get("price_access"),
                        "evidence_text": (ev.get("evidence_text") or "")[:240],
                    }
                    verified.append(item)
                    tele["verified_prices"] += 1
                    if item["economics_eligible"]:
                        usable.append(item)
                        tele["usable_prices"] += 1
                    else:
                        failure_classes.append(PRICE_VERIFIED_INACCESSIBLE)
                if identity_hit and not price_found:
                    failure_classes.append(PAGE_FETCHED_NO_PRICE)

        record_source_outcome(
            learning,
            family=family,
            domain=fr.domain,
            verified=price_found,
            usable=bool(usable),
            blocked=False,
            identity_hit=identity_hit,
        )
        if usable:
            break  # enough for this candidate

    # Strong blocked leads → manual queue signal
    strong_blocked = [
        L for L in leads if L.verification_status == FETCH_BLOCKED and L.confidence == "HIGH" and L.apparent_price
    ]
    for L in strong_blocked:
        if STRONG_PRICE_LEAD_REQUIRES_VERIFICATION not in failure_classes:
            failure_classes.append(STRONG_PRICE_LEAD_REQUIRES_VERIFICATION)

    prices = [v["verified_price"] for v in verified]
    # include strong leads in recon range only (not usable)
    lead_prices = [L.apparent_price for L in leads if L.apparent_price and L.confidence == "HIGH"]
    recon_prices = prices + lead_prices
    arange = acquisition_range(prices) if prices else acquisition_range(lead_prices)

    if usable:
        best = usable[0]
        remember_price(
            memory,
            key,
            {
                "verified_price": best["verified_price"],
                "source_url": best.get("source_url"),
                "economics_eligible": True,
                "price_type": best.get("price_type"),
                "confidence": best.get("confidence"),
            },
        )

    primary_failure = None
    if not verified and not leads:
        primary_failure = NO_PRICE_INFORMATION
    elif not verified and strong_blocked:
        primary_failure = PRICE_LEAD_FETCH_BLOCKED
    elif verified and not usable:
        primary_failure = PRICE_VERIFIED_INACCESSIBLE
    elif not prices and leads:
        primary_failure = STRONG_PRICE_LEAD_REQUIRES_VERIFICATION

    return {
        "kind": "PhaseL27AcquisitionResearch",
        "family": family,
        "cache_hit": False,
        "search_id": search_id,
        "targets": targets[:12],
        "leads": [L.to_dict() for L in leads],
        "verified": verified,
        "usable": usable,
        "acquisition_range": arange,
        "recon_range_includes_leads": bool(lead_prices and not prices),
        "failure_class": primary_failure,
        "failure_classes": list(dict.fromkeys(failure_classes))[:12],
        "fetch_log": fetch_log[-20:],
        "telemetry": tele,
        "manual_fallback": bool(strong_blocked),
        "strong_price_lead_requires_verification": bool(strong_blocked),
    }
