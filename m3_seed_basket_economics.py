"""Multi-line BOM acquisition pricing + basket economics for seed procurements.

Transaction unit = awardable basket, not individual line $10k profit.
Public evidence only. No outreach. Partial coverage labeled honestly.
"""

from __future__ import annotations

import json
import logging
import math
import re
import statistics
from collections import defaultdict
from typing import Any
from urllib.parse import quote_plus, urlparse

from application_clock import now_utc
from m3_deal_economics import classify_profit_target_status, target_profit_usd
from m3_government_revenue_benchmark import (
    SEED_PRIORITY_URLS,
    WHOLESALE_VERIFICATION_REQUIRED,
    fetch_url_text,
    parse_species_unit_prices,
)
from m3_product_identity_resolution import MATCH_HIGH, MATCH_LOW, MATCH_MEDIUM, MATCH_UNKNOWN
from m3_public_pricing_evidence import (
    ACCESS_OK,
    CostLedger,
    duckduckgo_urls,
    extract_price_observations,
    normalize_purchase_quantity,
    seller_from_url,
)
from pdf_text import extract_pdf_text

log = logging.getLogger("govtracker.m3_seed_basket_economics")

INDEX_KEY = "m3_seed_basket_economics_v1"
SEED_SOL_MARKERS = ("645-DOTRFB-3046-2027", "645DOTRFB30462027", "Wildflower and Native Grass Seed")
UA = {"User-Agent": "M3SeedBasket/1.0 (public research; no login)"}

# Match / status
EXACT_HISTORICAL = "EXACT_HISTORICAL"
SAME_SPECIES = "SAME_SPECIES"
COMPARABLE = "COMPARABLE"
GOV_UNKNOWN = "UNKNOWN"

EXACT_MATCH = "EXACT_MATCH"
PROBABLE_MATCH = "PROBABLE_MATCH"
PARTIAL_MATCH = "PARTIAL_MATCH"
MISMATCH = "MISMATCH"
MATCH_UNK = "UNKNOWN"

POSITIVE_PUBLIC_SPREAD = "POSITIVE_PUBLIC_SPREAD"
NEGATIVE_PUBLIC_SPREAD = "NEGATIVE_PUBLIC_SPREAD"
NEAR_BREAK_EVEN = "NEAR_BREAK_EVEN"
SPREAD_UNKNOWN = "UNKNOWN"

PARTIAL_BASKET_ECONOMICS = "PARTIAL_BASKET_ECONOMICS"
COMPLETE_BASKET_ECONOMICS = "COMPLETE_BASKET_ECONOMICS"
COMMERCIAL_VERIFICATION_WORTHY = "COMMERCIAL_VERIFICATION_WORTHY"
FREIGHT_UNKNOWN = "FREIGHT_UNKNOWN"
FUNDING_VERIFICATION_REQUIRED = "FUNDING_VERIFICATION_REQUIRED"

Q_NEEDS_SEED_ACQUISITION_PRICE = "NEEDS_SEED_ACQUISITION_PRICE"
Q_NEEDS_PACKAGE_NORMALIZATION = "NEEDS_PACKAGE_NORMALIZATION"
Q_NEEDS_SPECIES_MATCH_VALIDATION = "NEEDS_SPECIES_MATCH_VALIDATION"
Q_NEEDS_FREIGHT_EVIDENCE = "NEEDS_FREIGHT_EVIDENCE"
Q_WHOLESALE_VERIFICATION_REQUIRED_FUTURE = "WHOLESALE_VERIFICATION_REQUIRED_FUTURE"
Q_OWNER_REVIEW = "OWNER_REVIEW"

VA_ALLOWED = frozenset(
    {
        "RESEARCH_PUBLIC_SEED_PRICES",
        "ATTACH_PRICE_SHEET",
        "NORMALIZE_PACKAGE",
        "VALIDATE_SPECIES",
        "ADD_NOTES",
        "NOTE",
        "ENTER_KNOWN_SUPPLIER_PRICE",
    }
)
VA_FORBIDDEN = frozenset(
    {
        "CONTACT_SUPPLIER",
        "REQUEST_QUOTE",
        "REGISTER_ACCOUNT",
        "NEGOTIATE",
        "SUBMIT_BID",
        "APPROVE_PURSUIT",
    }
)

BASELINE = {
    "BOM_lines": 196,
    "Total_quantity_lb": 7892.9,
    "Government_revenue_coverage_pct": 90.61,
    "Acquisition_coverage_pct": 13.86,
    "Two_sided_economics_coverage_pct": 13.86,
}

# Known public native-seed catalog hosts (research hints only)
SEED_SELLER_HINTS = (
    "agrecol.com",
    "prairiemoon.com",
    "ernstseed.com",
    "ionxchange.com",
    "shootingstarnativeseed.com",
    "allendanseed.com",
    "nativeseed.com",
    "everwilde.com",
)

SCI_RE = re.compile(
    r"\(\s*([A-Z][a-z]+(?:\s+(?:×|x)\s+[A-Z]?[a-z]+)?(?:\s+[a-z]+){0,3}"
    r"(?:\s+\([^)]+\))?)\s*\)"
)
SCI_FALLBACK = re.compile(r"\b([A-Z][a-z]+\s+[a-z]{3,})\b")


def _utc() -> str:
    return now_utc().isoformat()


def _known(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, str):
        return v.strip() not in {"", "UNKNOWN", "unknown", "None"}
    if isinstance(v, (list, dict, set, tuple)):
        return len(v) > 0
    return True


def _num(v: Any) -> float | None:
    if v is None or v == "" or v == "UNKNOWN":
        return None
    try:
        return float(str(v).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None


def _pct(part: float, whole: float) -> float:
    if whole <= 0:
        return 0.0
    return round(100.0 * part / whole, 2)


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
        log.debug("seed basket index save failed", exc_info=True)


def load_basket_index() -> dict[str, Any]:
    return _load_setting(INDEX_KEY)


def save_basket_index(index: dict[str, Any]) -> None:
    _save_setting(INDEX_KEY, index)


def find_seed_opportunity(store: Any) -> dict[str, Any] | None:
    rows = store.all() if hasattr(store, "all") else []
    for r in rows:
        blob = f"{r.get('canonical_id')} {r.get('solicitation_number')} {r.get('title')}"
        if any(m.lower().replace("-", "") in blob.lower().replace("-", "") for m in SEED_SOL_MARKERS):
            if "Wildflower" in str(r.get("title") or "") or "3046" in blob:
                return r
    return None


def parse_species_identity(description: str) -> dict[str, Any]:
    desc = re.sub(r"\s+", " ", str(description or "")).strip()
    sci = None
    common = desc
    m = SCI_RE.search(desc)
    if m:
        sci = re.sub(r"\s+", " ", m.group(1)).strip()
        # strip trailing incomplete paren fragments like "Poir."
        sci = re.sub(r"\s*\([^)]*$", "", sci).strip()
        common = desc[: m.start()].strip(" -–—") or desc
    else:
        # broken paren forms: "Little bluestem  ( Schizachyrium"
        m2 = re.search(r"\(\s*([A-Z][a-z]+)\s*$", desc)
        if m2:
            common = desc[: m2.start()].strip()
            # cannot complete species epithet — leave UNKNOWN scientific
            sci = None
        else:
            m3 = SCI_FALLBACK.search(desc)
            if m3 and m3.group(1).split()[0].lower() not in {"iowa", "state", "pounds"}:
                sci = m3.group(1)
    return {
        "Common_name": common or "UNKNOWN",
        "Scientific_name": sci or "UNKNOWN",
        "Original_description": desc,
    }


def inventory_seed_bom(row: dict[str, Any]) -> dict[str, Any]:
    lines_out: list[dict[str, Any]] = []
    total_qty = 0.0
    for i, raw in enumerate([x for x in (row.get("line_items") or []) if isinstance(x, dict)]):
        ident = parse_species_identity(str(raw.get("description") or raw.get("item") or ""))
        qty = _num(raw.get("quantity")) or 0.0
        total_qty += qty
        uom = str(raw.get("uom") or raw.get("unit") or "LB").upper()
        lines_out.append(
            {
                "Line_number": i + 1,
                "Line_id": raw.get("line_id") or raw.get("id") or f"L{i+1}",
                "Common_name": ident["Common_name"],
                "Scientific_name": ident["Scientific_name"],
                "Required_quantity": qty,
                "UOM": uom,
                "Purity_germination_spec": raw.get("specifications") or raw.get("specs") or "UNKNOWN",
                "Seed_type": "UNKNOWN",
                "Origin_requirements": "UNKNOWN",
                "Packaging_requirements": "UNKNOWN",
                "Original_line": raw,
            }
        )
    return {
        "kind": "SEED_BOM_INVENTORY",
        "Solicitation": row.get("solicitation_number") or "645-DOTRFB-3046-2027",
        "Agency": row.get("agency") or "State of Iowa Department of Administrative Services",
        "Title": row.get("title"),
        "canonical_id": row.get("canonical_id"),
        "BOM_lines": len(lines_out),
        "Total_quantity": round(total_qty, 2),
        "Unique_scientific_names": len(
            {x["Scientific_name"].lower() for x in lines_out if x["Scientific_name"] != "UNKNOWN"}
        ),
        "lines": lines_out,
    }


def load_government_species_prices(
    ledger: CostLedger,
    access_log: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Reuse known Iowa seed bid-tab sources — do not rediscover the solicitation."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for url in SEED_PRIORITY_URLS:
        text, state = fetch_url_text(url, ledger, access_log, seen)
        if state != ACCESS_OK or not text:
            # fallback direct PDF extract
            try:
                import httpx

                ledger.free(f"pdf_direct:{url[:60]}")
                with httpx.Client(timeout=40, follow_redirects=True, headers=UA) as client:
                    r = client.get(url)
                if r.content[:4] == b"%PDF":
                    text = extract_pdf_text(r.content)
            except Exception:
                text = ""
        if not text:
            continue
        agency = "Iowa" if "iowa" in url.lower() else "Cedar Rapids"
        out.extend(parse_species_unit_prices(text, source_url=url, agency_hint=agency))
    # dedupe by sci + bid set
    deduped: list[dict[str, Any]] = []
    keys: set[str] = set()
    for e in out:
        k = f"{str(e.get('Scientific_name')).lower()}|{tuple(e.get('Bid_prices') or [])}|{e.get('Source_URL')}"
        if k in keys:
            continue
        keys.add(k)
        deduped.append(e)
    return deduped


def _prefer_unit_bid_prices(prices: list[float]) -> list[float]:
    """Drop extended totals when bid tabs alternate unit/extended columns."""
    prices = [float(p) for p in prices if _num(p) is not None and float(p) > 0]
    if len(prices) < 4:
        return prices
    even = prices[0::2]
    odd = prices[1::2]
    if even and odd and statistics.median(odd) > 2.5 * max(statistics.median(even), 0.01):
        return even
    med = statistics.median(prices)
    filtered = [p for p in prices if p <= 2.5 * med]
    return filtered if len(filtered) >= 2 else prices


def match_gov_price_to_line(line: dict[str, Any], gov_rows: list[dict[str, Any]]) -> dict[str, Any]:
    sci = str(line.get("Scientific_name") or "").lower()
    common = str(line.get("Common_name") or "").lower()
    hits = []
    for g in gov_rows:
        gs = str(g.get("Scientific_name") or "").lower()
        gc = str(g.get("Common_name") or "").lower()
        status = None
        if sci != "unknown" and gs == sci:
            status = EXACT_HISTORICAL if (g.get("Bid_count") or 0) >= 2 else SAME_SPECIES
        elif sci != "unknown" and gs and sci.split()[0] == gs.split()[0] and len(sci.split()) > 1 and sci != gs:
            status = COMPARABLE
        elif common != "unknown" and gc and (gc in common or common in gc) and len(common) > 4:
            status = SAME_SPECIES
        if not status:
            continue
        raw_prices = [float(p) for p in (g.get("Bid_prices") or []) if _num(p) is not None]
        unit_prices = _prefer_unit_bid_prices(raw_prices)
        hits.append({**g, "GOV_PRICE_STATUS": status, "_unit_prices": unit_prices})
    if not hits:
        return {
            "GOV_PRICE_STATUS": GOV_UNKNOWN,
            "Unit_price_median": "UNKNOWN",
            "Unit_price_low": "UNKNOWN",
            "Unit_price_high": "UNKNOWN",
            "Bid_prices": [],
            "Source_URL": "UNKNOWN",
            "Confidence": MATCH_UNKNOWN,
        }
    def _rank(h: dict[str, Any]) -> tuple:
        url = str(h.get("Source_URL") or "").lower()
        tier = 0 if "iowa.gov" in url or "bidopportunities" in url else 1 if "revize.com" in url else 2
        status_rank = 0 if h.get("GOV_PRICE_STATUS") == EXACT_HISTORICAL else 1 if h.get("GOV_PRICE_STATUS") == SAME_SPECIES else 2
        return (tier, status_rank, -(len(h.get("_unit_prices") or [])))

    hits.sort(key=_rank)
    best = hits[0]
    prices = list(best.get("_unit_prices") or [])
    median = statistics.median(prices) if prices else _num(best.get("Unit_price_median")) or _num(best.get("Unit_price"))
    return {
        "GOV_PRICE_STATUS": best.get("GOV_PRICE_STATUS") or SAME_SPECIES,
        "Unit_price_median": median if median is not None else "UNKNOWN",
        "Unit_price_low": min(prices) if prices else best.get("Unit_price_low", "UNKNOWN"),
        "Unit_price_high": max(prices) if prices else best.get("Unit_price_high", "UNKNOWN"),
        "Bid_prices": prices,
        "Winning_price": min(prices) if prices else "UNKNOWN",
        "Quantity": best.get("Quantity"),
        "Date": best.get("Evidence_date") or "UNKNOWN",
        "Source_URL": best.get("Source_URL"),
        "Confidence": MATCH_MEDIUM if prices else MATCH_LOW,
        "Evidence": best,
    }


def validate_species_match(line: dict[str, Any], product_name: str, page_text: str = "") -> str:
    sci = str(line.get("Scientific_name") or "").lower()
    common = str(line.get("Common_name") or "").lower()
    # Large window: SPA/catalog pages often put latin names below the fold / in JSON.
    blob = f"{product_name} {page_text[:80000]}".lower()
    if sci != "unknown" and sci in blob:
        return EXACT_MATCH
    if sci != "unknown" and sci.split()[0] in blob and (sci.split()[-1] in blob if len(sci.split()) > 1 else False):
        return EXACT_MATCH
    if common != "unknown" and len(common) > 4 and common in blob:
        return PROBABLE_MATCH
    # reject obvious wrong species tokens
    wrong = ("soybean", "corn hybrid", "tomato", "lawn fertilizer")
    if any(w in blob for w in wrong):
        return MISMATCH
    if "seed" in blob or "pls" in blob:
        return PARTIAL_MATCH
    return MATCH_UNK


def seed_acquisition_queries(line: dict[str, Any]) -> list[str]:
    sci = line.get("Scientific_name")
    common = line.get("Common_name")
    qs = []
    if _known(sci) and sci != "UNKNOWN":
        qs.extend(
            [
                f"{sci} native seed price per pound PLS",
                f"{sci} seed buy lb Agrecol OR Prairie Moon OR Ernst",
                f'"{sci}" $/lb seed',
            ]
        )
    if _known(common) and common != "UNKNOWN":
        qs.append(f"{common} native grass seed price per pound")
    out = []
    for q in qs:
        q = re.sub(r"\s+", " ", q).strip()
        if q and q not in out:
            out.append(q)
    return out[:4]


def prior_known_acquisition(line: dict[str, Any]) -> dict[str, Any] | None:
    """Reuse prior Big Bluestem Agrecol evidence — do not re-pay for it."""
    sci = str(line.get("Scientific_name") or "").lower()
    if "andropogon gerardii" in sci:
        qty = _num(line.get("Required_quantity")) or 0.0
        unit = 13.75
        return {
            "Seller": "Agrecol",
            "Product_name": "Big Bluestem (Andropogon gerardii)",
            "Match_status": EXACT_MATCH,
            "Observed_price": unit,
            "Observed_UOM": "LB",
            "Package_size": 1.0,
            "Pricing_level": "LEVEL_2",
            "URL": "prior_public_pricing_evidence",
            "Confidence": MATCH_MEDIUM,
            "Source": "prior_public_pricing_evidence",
            "NORMALIZED_ACQUISITION_COST": round(unit * qty, 2) if qty else "UNKNOWN",
            "Effective_acquisition_per_required_lb": unit,
        }
    return None


def retrieve_species_acquisition(
    line: dict[str, Any],
    ledger: CostLedger,
    access_log: list[dict[str, Any]],
    seen_urls: set[str],
    *,
    max_pages: int = 4,
) -> dict[str, Any]:
    prior = prior_known_acquisition(line)
    options: list[dict[str, Any]] = []
    if prior:
        options.append(prior)

    pages = 0
    sci = str(line.get("Scientific_name") or "")
    for q in seed_acquisition_queries(line):
        if pages >= max_pages:
            break
        for u in duckduckgo_urls(q, ledger, access_log, limit=5):
            if pages >= max_pages:
                break
            if any(x in u.lower() for x in ("youtube.com", "facebook.com", "linkedin.com", "wikipedia.org")):
                continue
            # prefer known seed sellers when present in results
            text, state = fetch_url_text(u, ledger, access_log, seen_urls)
            if state != ACCESS_OK or not text:
                continue
            pages += 1
            # title
            tm = re.search(r"<title[^>]*>([^<]{5,160})</title>", text, re.I)
            pname = re.sub(r"\s+", " ", (tm.group(1) if tm else sci))[:140]
            match = validate_species_match(line, pname, text)
            if match in {MISMATCH}:
                continue
            obs = extract_price_observations(text, source_url=u, product_hint=sci or str(line.get("Common_name")))
            # keep lb prices preferentially
            lb_obs = [o for o in obs if str(o.get("Observed_UOM") or "").upper() in {"LB", "BAG"}]
            use_obs = lb_obs or obs
            seller = seller_from_url(u)
            for o in use_obs[:3]:
                price = _num(o.get("Observed_price"))
                if price is None or price < 0.5 or price > 5000:
                    continue
                uom = str(o.get("Observed_UOM") or "UNKNOWN").upper()
                pkg = _num(o.get("Package_quantity"))
                if uom == "LB":
                    pkg = pkg or 1.0
                level = "LEVEL_2" if match == EXACT_MATCH and uom == "LB" else "LEVEL_3" if uom != "UNKNOWN" else "LEVEL_4"
                if match == PARTIAL_MATCH:
                    level = "LEVEL_4"
                qty = _num(line.get("Required_quantity"))
                norm = normalize_purchase_quantity(qty, package_size=pkg if uom == "BAG" else 1.0, observed_uom=uom, gov_uom="LB")
                purchase_q = _num(norm.get("PURCHASE_QUANTITY"))
                if uom == "BAG" and pkg and purchase_q:
                    # package count * bag price; purchase qty in lb = count * pkg
                    count = _num(norm.get("PACKAGE_COUNT")) or 0
                    total = round(price * count, 2)
                    eff = round(total / qty, 4) if qty else "UNKNOWN"
                    purchased_lb = round(count * pkg, 4)
                elif uom == "LB" and qty:
                    total = round(price * qty, 2)
                    eff = price
                    purchased_lb = qty
                    norm = normalize_purchase_quantity(qty, package_size=1.0, observed_uom="LB", gov_uom="LB")
                else:
                    continue
                # only primary economics from L1-L3 / exact-probable
                options.append(
                    {
                        "Seller": seller.get("Seller_name") or urlparse(u).netloc,
                        "Seller_type": seller.get("Seller_type"),
                        "Product_name": pname,
                        "Match_status": match,
                        "Observed_price": price,
                        "Observed_UOM": uom,
                        "Package_size": pkg if pkg is not None else "UNKNOWN",
                        "QUANTITY_NORMALIZATION": norm,
                        "Purchased_quantity_lb": purchased_lb,
                        "NORMALIZED_ACQUISITION_COST": total,
                        "Effective_acquisition_per_required_lb": eff,
                        "Pricing_level": level,
                        "URL": u,
                        "Confidence": MATCH_MEDIUM if match in {EXACT_MATCH, PROBABLE_MATCH} else MATCH_LOW,
                        "Source": u,
                        "Observation_datetime": _utc(),
                    }
                )

    # best observed among L2/L3 exact/probable
    primary_pool = [
        o
        for o in options
        if o.get("Pricing_level") in {"LEVEL_1", "LEVEL_2", "LEVEL_3"}
        and o.get("Match_status") in {EXACT_MATCH, PROBABLE_MATCH}
        and _num(o.get("Effective_acquisition_per_required_lb")) is not None
    ]
    if not primary_pool:
        primary_pool = [o for o in options if _num(o.get("Effective_acquisition_per_required_lb")) is not None]
    best = min(primary_pool, key=lambda o: float(o["Effective_acquisition_per_required_lb"])) if primary_pool else None
    return {
        "options": options,
        "BEST_OBSERVED_PUBLIC_ACQUISITION": best,
        "pages_fetched": pages,
    }


def materiality_score(line: dict[str, Any], gov: dict[str, Any]) -> float:
    qty = _num(line.get("Required_quantity")) or 0.0
    gov_u = _num(gov.get("Unit_price_median"))
    if gov_u is not None:
        return qty * gov_u
    return qty * 10.0  # unknown gov — still prioritize quantity


def classify_spread(gov_u: float | None, acq_u: float | None) -> str:
    if gov_u is None or acq_u is None:
        return SPREAD_UNKNOWN
    spread = gov_u - acq_u
    if abs(spread) <= max(0.25, 0.03 * gov_u):
        return NEAR_BREAK_EVEN
    if spread > 0:
        return POSITIVE_PUBLIC_SPREAD
    return NEGATIVE_PUBLIC_SPREAD


def build_line_economics_row(line: dict[str, Any], gov: dict[str, Any], acq: dict[str, Any] | None) -> dict[str, Any]:
    qty = _num(line.get("Required_quantity")) or 0.0
    gov_u = _num(gov.get("Unit_price_median"))
    gov_rev = round(gov_u * qty, 2) if gov_u is not None else "UNKNOWN"
    acq_u = _num((acq or {}).get("Effective_acquisition_per_required_lb"))
    acq_cost = _num((acq or {}).get("NORMALIZED_ACQUISITION_COST"))
    if acq_cost is None and acq_u is not None and qty:
        acq_cost = round(acq_u * qty, 2)
    spread_u = round(gov_u - acq_u, 4) if gov_u is not None and acq_u is not None else "UNKNOWN"
    spread_t = round(float(spread_u) * qty, 2) if isinstance(spread_u, (int, float)) else "UNKNOWN"
    margin = "UNKNOWN"
    if isinstance(gov_rev, (int, float)) and gov_rev and isinstance(spread_t, (int, float)):
        margin = round(100.0 * spread_t / gov_rev, 2)
    be = round(gov_u, 4) if gov_u is not None else "UNKNOWN"
    level = (acq or {}).get("Pricing_level") or "LEVEL_5"
    # Sanity: packet/retail outliers vs historical gov unit prices → demote from primary basket
    gov_high = _num(gov.get("Unit_price_high")) or gov_u
    if acq_u is not None and gov_high is not None and gov_high > 0 and acq_u > 5.0 * gov_high:
        level = "LEVEL_4"
        if isinstance(spread_t, (int, float)):
            # keep numbers but mark not for primary L1-L3 basket
            pass
    return {
        "Line_number": line.get("Line_number"),
        "Species": line.get("Original_description") or f"{line.get('Common_name')} ({line.get('Scientific_name')})",
        "Common_name": line.get("Common_name"),
        "Scientific_name": line.get("Scientific_name"),
        "Quantity": qty,
        "UOM": line.get("UOM"),
        "Gov_price_status": gov.get("GOV_PRICE_STATUS"),
        "Gov_unit_price": gov_u if gov_u is not None else "UNKNOWN",
        "Gov_low": gov.get("Unit_price_low"),
        "Gov_high": gov.get("Unit_price_high"),
        "Gov_revenue": gov_rev,
        "Gov_source": gov.get("Source_URL"),
        "Acquisition_unit_price": acq_u if acq_u is not None else "UNKNOWN",
        "Acquisition_cost": acq_cost if acq_cost is not None else "UNKNOWN",
        "Acquisition_seller": (acq or {}).get("Seller") or "UNKNOWN",
        "Acquisition_match": (acq or {}).get("Match_status") or MATCH_UNK,
        "Pricing_level": level,
        "Gross_spread_unit": spread_u,
        "Gross_spread_total": spread_t,
        "Gross_margin_pct": margin,
        "Spread_class": classify_spread(gov_u, acq_u),
        "Break_even_acquisition": be,
        "Wholesale_status": WHOLESALE_VERIFICATION_REQUIRED
        if isinstance(spread_t, (int, float)) and spread_t <= 0
        else ("WHOLESALE_ACCESS_UNVERIFIED" if acq_u is not None else "UNKNOWN"),
        "Materiality": materiality_score(line, gov),
        "Alternatives": [],
        "price_sanity_demoted": level == "LEVEL_4" and (acq or {}).get("Pricing_level") in {"LEVEL_1", "LEVEL_2", "LEVEL_3"},
    }


def aggregate_basket(line_rows: list[dict[str, Any]], *, row: dict[str, Any]) -> dict[str, Any]:
    total_qty = sum(_num(r.get("Quantity")) or 0 for r in line_rows)
    qty_gov = sum(_num(r.get("Quantity")) or 0 for r in line_rows if _num(r.get("Gov_unit_price")) is not None)
    qty_acq = sum(_num(r.get("Quantity")) or 0 for r in line_rows if _num(r.get("Acquisition_unit_price")) is not None)
    qty_both = sum(
        _num(r.get("Quantity")) or 0
        for r in line_rows
        if _num(r.get("Gov_unit_price")) is not None and _num(r.get("Acquisition_unit_price")) is not None
    )
    lines_gov = sum(1 for r in line_rows if _num(r.get("Gov_unit_price")) is not None)
    lines_acq = sum(1 for r in line_rows if _num(r.get("Acquisition_unit_price")) is not None)
    lines_both = sum(
        1
        for r in line_rows
        if _num(r.get("Gov_unit_price")) is not None and _num(r.get("Acquisition_unit_price")) is not None
    )

    # primary basket uses L1-L3 both-sided rows
    primary = [
        r
        for r in line_rows
        if _num(r.get("Gov_unit_price")) is not None
        and _num(r.get("Acquisition_cost")) is not None
        and str(r.get("Pricing_level")) in {"LEVEL_1", "LEVEL_2", "LEVEL_3"}
        and r.get("Acquisition_match") in {EXACT_MATCH, PROBABLE_MATCH}
    ]
    # if too thin, include LEVEL_4 probable for sensitivity only
    l4 = [
        r
        for r in line_rows
        if _num(r.get("Gov_unit_price")) is not None
        and _num(r.get("Acquisition_cost")) is not None
        and str(r.get("Pricing_level")) == "LEVEL_4"
    ]

    modeled_rev = round(sum(float(r["Gov_revenue"]) for r in primary if isinstance(r.get("Gov_revenue"), (int, float))), 2)
    modeled_acq = round(sum(float(r["Acquisition_cost"]) for r in primary), 2)
    gross = round(modeled_rev - modeled_acq, 2)
    qty_primary = sum(_num(r.get("Quantity")) or 0 for r in primary)

    op = row.get("operator_economics") if isinstance(row.get("operator_economics"), dict) else {}
    freight = _num(op.get("estimated_freight_usd"))
    financing = _num(op.get("estimated_financing_cost_usd"))
    expenses = _num(op.get("known_fees_usd")) or 0.0
    freight_used = freight if freight is not None else 0.0
    financing_used = financing if financing is not None else 0.0
    expected = round(gross - freight_used - financing_used - expenses, 2)

    # value-weighted: gov revenue of all gov-priced lines vs modeled both-sided
    all_gov_rev = sum(float(r["Gov_revenue"]) for r in line_rows if isinstance(r.get("Gov_revenue"), (int, float)))
    coverage_value = _pct(modeled_rev, all_gov_rev) if all_gov_rev else 0.0

    label = COMPLETE_BASKET_ECONOMICS if _pct(qty_both, total_qty) >= 95 and lines_both == len(line_rows) else PARTIAL_BASKET_ECONOMICS

    tp = target_profit_usd(row)
    max_acq = round(modeled_rev - tp - freight_used - financing_used - expenses, 2) if modeled_rev else "UNKNOWN"
    gap = round(float(modeled_acq) - float(max_acq), 2) if isinstance(max_acq, (int, float)) else "UNKNOWN"
    improvement = "UNKNOWN"
    if isinstance(gap, (int, float)) and modeled_acq > 0:
        improvement = {
            "Absolute_usd": gap,
            "Pct_of_observed_acquisition": round(100.0 * gap / modeled_acq, 2),
            "notes": ["required_improvement_for_10k_on_MODELED_partial_basket_not_complete_contract"],
        }

    # wholesale sensitivities on modeled acquisition
    sensitivities = []
    for pct_cut in (5, 10, 15, 20, 25):
        acq2 = round(modeled_acq * (1 - pct_cut / 100.0), 2)
        profit2 = round(modeled_rev - acq2 - freight_used - financing_used - expenses, 2)
        sensitivities.append(
            {
                "kind": "WHOLESALE_IMPROVEMENT_SCENARIO",
                "label": "HYPOTHETICAL_SENSITIVITY_NOT_OBSERVED_PRICE",
                "acquisition_improvement_pct": pct_cut,
                "Modeled_acquisition": acq2,
                "Expected_profit": profit2,
                "Meets_10k": profit2 >= tp,
            }
        )

    positive = sorted(
        [r for r in primary if isinstance(r.get("Gross_spread_total"), (int, float)) and r["Gross_spread_total"] > 0],
        key=lambda r: -float(r["Gross_spread_total"]),
    )
    negative = sorted(
        [r for r in primary if isinstance(r.get("Gross_spread_total"), (int, float)) and r["Gross_spread_total"] < 0],
        key=lambda r: float(r["Gross_spread_total"]),
    )
    unknown_material = sorted(
        [
            r
            for r in line_rows
            if _num(r.get("Acquisition_unit_price")) is None and (_num(r.get("Gov_unit_price")) is not None or (_num(r.get("Quantity")) or 0) > 50)
        ],
        key=lambda r: -float(r.get("Materiality") or 0),
    )

    verification = None
    if label == PARTIAL_BASKET_ECONOMICS and _pct(qty_both, total_qty) >= 25 and (positive or negative):
        verification = COMMERCIAL_VERIFICATION_WORTHY
    elif _pct(qty_acq, total_qty) >= 40 and all_gov_rev > 0:
        verification = COMMERCIAL_VERIFICATION_WORTHY

    return {
        "kind": "BASKET_ECONOMICS",
        "label": label,
        "BOM_lines": len(line_rows),
        "Total_quantity": round(total_qty, 2),
        "Lines_with_government_revenue": lines_gov,
        "Lines_with_acquisition": lines_acq,
        "Lines_with_both_sides": lines_both,
        "Government_revenue_coverage_pct_qty": _pct(qty_gov, total_qty),
        "Acquisition_quantity_coverage_pct": _pct(qty_acq, total_qty),
        "Two_sided_economics_quantity_coverage_pct": _pct(qty_both, total_qty),
        "Primary_L1_L3_quantity_coverage_pct": _pct(qty_primary, total_qty),
        "Value_weighted_modeled_coverage_pct": coverage_value,
        "MODELED_BASKET_REVENUE": modeled_rev,
        "MODELED_BASKET_ACQUISITION_COST": modeled_acq,
        "MODELED_GROSS_PRODUCT_SPREAD": gross,
        "Freight": freight if freight is not None else FREIGHT_UNKNOWN,
        "Financing": financing if financing is not None else FUNDING_VERIFICATION_REQUIRED,
        "Transaction_expenses": expenses,
        "EXPECTED_BASKET_PROFIT_BEFORE_FREIGHT": gross,
        "EXPECTED_BASKET_PROFIT": expected if freight is not None or financing is not None else gross,
        "profit_notes": [
            "freight_and_financing_treated_as_0_in_numeric_profit_when_UNKNOWN_but_flagged",
            FREIGHT_UNKNOWN if freight is None else "freight_modeled",
            FUNDING_VERIFICATION_REQUIRED if financing is None else "financing_modeled",
        ],
        "Unmodeled_quantity": round(total_qty - qty_both, 2),
        "Unmodeled_government_revenue_estimate": round(all_gov_rev - modeled_rev, 2) if all_gov_rev else "UNKNOWN",
        "Target_profit": tp,
        "MAX_ACQUISITION_COST_FOR_10K": max_acq,
        "Observed_modeled_acquisition": modeled_acq,
        "Acquisition_gap_vs_10k": gap,
        "Improvement_required": improvement,
        "WHOLESALE_IMPROVEMENT_SCENARIOS": sensitivities,
        "L4_scenario_line_count": len(l4),
        "TOP_POSITIVE_CONTRIBUTORS": positive[:8],
        "TOP_NEGATIVE_CONTRIBUTORS": negative[:8],
        "TOP_UNKNOWN_ECONOMIC_CONTRIBUTORS": unknown_material[:12],
        "COMMERCIAL_VERIFICATION": verification,
        "Confidence": MATCH_MEDIUM if _pct(qty_both, total_qty) >= 30 else MATCH_LOW,
    }


def build_wholesale_target_map(line_rows: list[dict[str, Any]], basket: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for r in line_rows:
        if r.get("Spread_class") not in {NEGATIVE_PUBLIC_SPREAD, NEAR_BREAK_EVEN}:
            continue
        if _num(r.get("Acquisition_unit_price")) is None or _num(r.get("Gov_unit_price")) is None:
            continue
        obs = float(r["Acquisition_unit_price"])
        be = float(r["Gov_unit_price"])
        # desired: 15% under gov median as illustrative target — labeled not observed
        desired = round(be * 0.85, 4)
        out.append(
            {
                "kind": "WHOLESALE_TARGET_MAP",
                "Species": r.get("Scientific_name") or r.get("Common_name"),
                "Observed": obs,
                "Break_even": be,
                "Desired_acquisition_illustrative": desired,
                "Improvement_required_to_breakeven": round(obs - be, 4),
                "Improvement_pct_to_breakeven": round(100.0 * (obs - be) / obs, 2) if obs else "UNKNOWN",
                "status": WHOLESALE_VERIFICATION_REQUIRED,
                "notes": ["desired_is_illustrative_not_observed_obtainable_price"],
            }
        )
    return out[:20]


def build_supplier_basket_profile(line_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_seller: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in line_rows:
        seller = str(r.get("Acquisition_seller") or "")
        if not seller or seller == "UNKNOWN":
            continue
        if _num(r.get("Acquisition_cost")) is None:
            continue
        by_seller[seller].append(r)
    profiles = []
    for seller, rows in by_seller.items():
        qty = sum(_num(r.get("Quantity")) or 0 for r in rows)
        total = sum(float(r["Acquisition_cost"]) for r in rows)
        profiles.append(
            {
                "kind": "SUPPLIER_BASKET_PROFILE",
                "Supplier": seller,
                "Species_covered": sorted({str(r.get("Scientific_name") or r.get("Common_name")) for r in rows}),
                "BOM_lines_covered": len(rows),
                "Quantity_covered": round(qty, 2),
                "Public_acquisition_total": round(total, 2),
                "Potential_order_size": round(total, 2),
                "Pricing_evidence_quality": "MIXED",
                "Wholesale_status": "WHOLESALE_ACCESS_UNVERIFIED",
                "notes": ["no_consolidated_discount_assumed"],
            }
        )
    profiles.sort(key=lambda p: -p["Public_acquisition_total"])
    return profiles


def analyze_seed_basket(
    store: Any,
    *,
    persist: bool = True,
    max_species_research: int = 18,
    max_pages_per_species: int = 3,
    second_pass_species: int = 8,
    reuse_prior_acquisition: bool = False,
) -> dict[str, Any]:
    ledger = CostLedger()
    access_log: list[dict[str, Any]] = []
    row = find_seed_opportunity(store)
    if not row:
        return {"ok": False, "error": "seed_opportunity_not_found", "DEVELOPMENT_NO_OUTREACH": True}

    inv = inventory_seed_bom(row)
    gov_rows = load_government_species_prices(ledger, access_log)

    # Match gov to every line
    line_gov: dict[int, dict[str, Any]] = {}
    for line in inv["lines"]:
        line_gov[line["Line_number"]] = match_gov_price_to_line(line, gov_rows)

    # Unique species research priority by materiality (sum across lines)
    by_sci: dict[str, dict[str, Any]] = {}
    for line in inv["lines"]:
        sci = str(line.get("Scientific_name") or "UNKNOWN")
        key = sci.lower()
        gov = line_gov[line["Line_number"]]
        mat = materiality_score(line, gov)
        if key not in by_sci:
            by_sci[key] = {"line": line, "materiality": mat, "qty": _num(line.get("Required_quantity")) or 0.0}
        else:
            by_sci[key]["materiality"] += mat
            by_sci[key]["qty"] += _num(line.get("Required_quantity")) or 0.0
            if (_num(line.get("Required_quantity")) or 0) > (_num(by_sci[key]["line"].get("Required_quantity")) or 0):
                by_sci[key]["line"] = line

    ranked = sorted(by_sci.values(), key=lambda x: -x["materiality"])
    research_targets = [x for x in ranked if x["line"].get("Scientific_name") != "UNKNOWN"]

    acq_by_sci: dict[str, dict[str, Any]] = {}
    seen_urls: set[str] = set()
    researched = 0

    # Optionally reuse prior acquisition options from last persisted run (recalc economics only)
    if reuse_prior_acquisition:
        prev = (load_basket_index().get("by_id") or {}).get(str(row.get("canonical_id"))) or {}
        prev_pkg = prev.get("package") if isinstance(prev, dict) else {}
        for r in (prev_pkg or {}).get("LINE_ECONOMICS_TABLE") or []:
            key = str(r.get("Scientific_name") or "").lower()
            if not key or key == "unknown":
                continue
            unit = _num(r.get("Acquisition_unit_price"))
            if unit is None:
                continue
            if key in acq_by_sci:
                continue
            acq_by_sci[key] = {
                "options": [],
                "BEST_OBSERVED_PUBLIC_ACQUISITION": {
                    "Seller": r.get("Acquisition_seller"),
                    "Product_name": r.get("Species"),
                    "Match_status": r.get("Acquisition_match") or PROBABLE_MATCH,
                    "Observed_price": unit,
                    "Observed_UOM": "LB",
                    "Package_size": 1.0,
                    "Pricing_level": r.get("Pricing_level") if str(r.get("Pricing_level")) != "LEVEL_4" or not r.get("price_sanity_demoted") else "LEVEL_3",
                    # restore pre-demotion level when possible
                    "URL": "reused_prior_run",
                    "Confidence": MATCH_MEDIUM,
                    "NORMALIZED_ACQUISITION_COST": r.get("Acquisition_cost"),
                    "Effective_acquisition_per_required_lb": unit,
                    "Source": "reused_prior_run",
                },
                "pages_fetched": 0,
            }
            # if previously demoted, keep original observed level as LEVEL_3 for re-eval
            best = acq_by_sci[key]["BEST_OBSERVED_PUBLIC_ACQUISITION"]
            if r.get("price_sanity_demoted") or str(r.get("Pricing_level")) == "LEVEL_4":
                best["Pricing_level"] = "LEVEL_3"
            researched += 1

    if not reuse_prior_acquisition:
        for item in research_targets[:max_species_research]:
            line = item["line"]
            key = str(line.get("Scientific_name")).lower()
            result = retrieve_species_acquisition(
                line, ledger, access_log, seen_urls, max_pages=max_pages_per_species
            )
            acq_by_sci[key] = result
            researched += 1

    # Build preliminary rows to find high-materiality unknowns
    def acq_for_line(line: dict[str, Any]) -> dict[str, Any] | None:
        key = str(line.get("Scientific_name") or "").lower()
        # operator-entered known supplier prices
        for op in row.get("operator_known_supplier_prices") or []:
            if not isinstance(op, dict):
                continue
            if str(op.get("Scientific_name") or "").lower() == key and _num(op.get("unit_price")) is not None:
                qty = _num(line.get("Required_quantity")) or 0
                unit = float(op["unit_price"])
                return {
                    "Seller": op.get("Supplier") or "OPERATOR_KNOWN",
                    "Product_name": op.get("Product") or line.get("Common_name"),
                    "Match_status": EXACT_MATCH,
                    "Observed_price": unit,
                    "Observed_UOM": op.get("UOM") or "LB",
                    "Package_size": _num(op.get("Package_size")) or 1.0,
                    "Pricing_level": "LEVEL_1",
                    "URL": "operator_entered",
                    "Confidence": MATCH_HIGH,
                    "NORMALIZED_ACQUISITION_COST": round(unit * qty, 2),
                    "Effective_acquisition_per_required_lb": unit,
                    "Source": "operator_known_supplier",
                }
        best = (acq_by_sci.get(key) or {}).get("BEST_OBSERVED_PUBLIC_ACQUISITION")
        if best:
            # rescale cost to this line's quantity
            unit = _num(best.get("Effective_acquisition_per_required_lb"))
            qty = _num(line.get("Required_quantity")) or 0
            if unit is None:
                return best
            scaled = {
                **best,
                "NORMALIZED_ACQUISITION_COST": round(unit * qty, 2),
                "QUANTITY_NORMALIZATION": normalize_purchase_quantity(
                    qty,
                    package_size=1.0 if str(best.get("Observed_UOM")) == "LB" else _num(best.get("Package_size")),
                    observed_uom=str(best.get("Observed_UOM") or "LB"),
                    gov_uom="LB",
                ),
            }
            return scaled
        return prior_known_acquisition(line)

    prelim_rows = [
        build_line_economics_row(line, line_gov[line["Line_number"]], acq_for_line(line)) for line in inv["lines"]
    ]
    unknown_material = [
        r
        for r in prelim_rows
        if _num(r.get("Acquisition_unit_price")) is None and r.get("Scientific_name") != "UNKNOWN"
    ]
    unknown_material.sort(key=lambda r: -float(r.get("Materiality") or 0))

    # Pass 2 — economically material unknowns not yet researched
    researched_keys = set(acq_by_sci.keys())
    extra = 0
    if not reuse_prior_acquisition:
        for r in unknown_material:
            if extra >= second_pass_species:
                break
            key = str(r.get("Scientific_name")).lower()
            if key in researched_keys:
                continue
            line = next((L for L in inv["lines"] if str(L.get("Scientific_name")).lower() == key), None)
            if not line:
                continue
            acq_by_sci[key] = retrieve_species_acquisition(
                line, ledger, access_log, seen_urls, max_pages=max_pages_per_species
            )
            researched_keys.add(key)
            extra += 1
            researched += 1

    line_rows = [
        build_line_economics_row(line, line_gov[line["Line_number"]], acq_for_line(line)) for line in inv["lines"]
    ]
    # attach alternatives count
    for r in line_rows:
        key = str(r.get("Scientific_name") or "").lower()
        opts = (acq_by_sci.get(key) or {}).get("options") or []
        r["Alternatives"] = [
            {"Seller": o.get("Seller"), "Effective_per_lb": o.get("Effective_acquisition_per_required_lb"), "Level": o.get("Pricing_level")}
            for o in opts[:5]
        ]

    basket = aggregate_basket(line_rows, row=row)
    wholesale_map = build_wholesale_target_map(line_rows, basket)
    suppliers = build_supplier_basket_profile(line_rows)

    # Pricing level counts (species researched)
    level_counts = {"LEVEL_1": 0, "LEVEL_2": 0, "LEVEL_3": 0, "LEVEL_4": 0, "LEVEL_5": 0}
    priced_species = 0
    sellers: set[str] = set()
    for key, res in acq_by_sci.items():
        best = res.get("BEST_OBSERVED_PUBLIC_ACQUISITION")
        if best and _num(best.get("Effective_acquisition_per_required_lb")) is not None:
            priced_species += 1
            level_counts[str(best.get("Pricing_level") or "LEVEL_5")] = level_counts.get(str(best.get("Pricing_level") or "LEVEL_5"), 0) + 1
            if best.get("Seller"):
                sellers.add(str(best["Seller"]))
        else:
            level_counts["LEVEL_5"] += 1
        for o in res.get("options") or []:
            if o.get("Seller"):
                sellers.add(str(o["Seller"]))

    # also count prior-only species applied to lines
    for r in line_rows:
        if r.get("Acquisition_seller") and r["Acquisition_seller"] != "UNKNOWN":
            sellers.add(str(r["Acquisition_seller"]))

    access_counts: dict[str, int] = defaultdict(int)
    for a in access_log:
        access_counts[str(a.get("status") or "UNKNOWN")] += 1

    pkg = {
        "kind": "M3SeedBasketEconomics",
        "ok": True,
        "BASELINE": BASELINE,
        "INVENTORY": {
            "Solicitation": inv["Solicitation"],
            "Agency": inv["Agency"],
            "BOM_lines": inv["BOM_lines"],
            "Total_quantity": inv["Total_quantity"],
            "Unique_scientific_names": inv["Unique_scientific_names"],
        },
        "AFTER": {
            "Government_revenue_coverage_pct": basket["Government_revenue_coverage_pct_qty"],
            "Acquisition_line_coverage": basket["Lines_with_acquisition"],
            "Acquisition_quantity_coverage_pct": basket["Acquisition_quantity_coverage_pct"],
            "Two_sided_economics_coverage_pct": basket["Two_sided_economics_quantity_coverage_pct"],
        },
        "PUBLIC_ACQUISITION_RESEARCH": {
            "Species_researched": researched,
            "Species_with_usable_prices": priced_species,
            "Sellers_found": len(sellers),
            "seller_list": sorted(sellers)[:30],
            **level_counts,
        },
        "BASKET_ECONOMICS": basket,
        "LINE_ECONOMICS_TABLE": line_rows,
        "WHOLESALE_TARGET_MAP": wholesale_map,
        "SUPPLIER_BASKET_PROFILE": suppliers,
        "GOVERNMENT_PRICE_ROWS": len(gov_rows),
        "COST": ledger.as_dict(),
        "ACCESS": dict(access_counts),
        "SAFETY": {"Outreach": 0, "Registrations": 0, "Quotes": 0, "Bids": 0, "Purchases": 0},
        "NEXT_STATE": "MULTI_LINE_BASKET_ECONOMICS_OPERATIONAL",
        "DEVELOPMENT_NO_OUTREACH": True,
        "updated_at": _utc(),
    }

    if persist:
        index = load_basket_index()
        by_id = index.setdefault("by_id", {})
        cid = str(row.get("canonical_id"))
        # persist compact summary + full package
        by_id[cid] = {
            "summary": {
                "label": basket.get("label"),
                "Acquisition_quantity_coverage_pct": basket.get("Acquisition_quantity_coverage_pct"),
                "Two_sided_economics_quantity_coverage_pct": basket.get("Two_sided_economics_quantity_coverage_pct"),
                "MODELED_GROSS_PRODUCT_SPREAD": basket.get("MODELED_GROSS_PRODUCT_SPREAD"),
                "EXPECTED_BASKET_PROFIT": basket.get("EXPECTED_BASKET_PROFIT"),
                "COMMERCIAL_VERIFICATION": basket.get("COMMERCIAL_VERIFICATION"),
            },
            "package": pkg,
            "updated_at": _utc(),
        }
        save_basket_index(index)
        row["seed_basket_economics"] = {
            "kind": "M3SeedBasketEconomicsSummary",
            **by_id[cid]["summary"],
            "BASKET_ECONOMICS": {
                k: basket.get(k)
                for k in (
                    "label",
                    "MODELED_BASKET_REVENUE",
                    "MODELED_BASKET_ACQUISITION_COST",
                    "MODELED_GROSS_PRODUCT_SPREAD",
                    "EXPECTED_BASKET_PROFIT",
                    "Acquisition_quantity_coverage_pct",
                    "Two_sided_economics_quantity_coverage_pct",
                    "Government_revenue_coverage_pct_qty",
                    "MAX_ACQUISITION_COST_FOR_10K",
                    "Acquisition_gap_vs_10k",
                    "COMMERCIAL_VERIFICATION",
                    "Freight",
                    "Financing",
                )
            },
        }
        if hasattr(store, "_rows") and cid in getattr(store, "_rows", {}):
            store._rows[cid] = row
        try:
            if hasattr(store, "save"):
                store.save()
        except Exception:
            pass

    return pkg


def deal_room_seed_basket_section(row: dict[str, Any]) -> dict[str, Any]:
    pkg = row.get("seed_basket_economics")
    full = None
    if not isinstance(pkg, dict) or "LINE_ECONOMICS_TABLE" not in pkg:
        idx = load_basket_index()
        stored = (idx.get("by_id") or {}).get(str(row.get("canonical_id") or ""))
        if isinstance(stored, dict):
            full = stored.get("package")
            pkg = stored.get("summary") or pkg or {}
    else:
        full = pkg
    basket = (full or {}).get("BASKET_ECONOMICS") or (pkg or {}).get("BASKET_ECONOMICS") or {}
    table = (full or {}).get("LINE_ECONOMICS_TABLE") or []
    return {
        "kind": "DealRoomSeedBasketEconomics",
        "BASKET_ECONOMICS": basket,
        "LINE_ECONOMICS_TABLE": table[:50],
        "TOP_POSITIVE": (basket.get("TOP_POSITIVE_CONTRIBUTORS") or [])[:5],
        "TOP_NEGATIVE": (basket.get("TOP_NEGATIVE_CONTRIBUTORS") or [])[:5],
        "TOP_UNKNOWN": (basket.get("TOP_UNKNOWN_ECONOMIC_CONTRIBUTORS") or [])[:5],
        "SUPPLIER_BASKET_PROFILE": (full or {}).get("SUPPLIER_BASKET_PROFILE") or [],
        "WHOLESALE_TARGET_MAP": (full or {}).get("WHOLESALE_TARGET_MAP") or [],
        "sort_hints": ["Largest_positive", "Largest_negative", "Largest_unknown_materiality"],
        "DEVELOPMENT_NO_OUTREACH": True,
    }


def build_va_basket_queues(limit: int = 25) -> dict[str, Any]:
    idx = load_basket_index()
    by = idx.get("by_id") or {}
    queues = {
        Q_NEEDS_SEED_ACQUISITION_PRICE: [],
        Q_NEEDS_PACKAGE_NORMALIZATION: [],
        Q_NEEDS_SPECIES_MATCH_VALIDATION: [],
        Q_NEEDS_FREIGHT_EVIDENCE: [],
        Q_WHOLESALE_VERIFICATION_REQUIRED_FUTURE: [],
        Q_OWNER_REVIEW: [],
    }
    for cid, data in list(by.items())[:limit]:
        s = (data or {}).get("summary") or {}
        pkg = (data or {}).get("package") or {}
        basket = pkg.get("BASKET_ECONOMICS") or {}
        if basket.get("COMMERCIAL_VERIFICATION") == COMMERCIAL_VERIFICATION_WORTHY:
            queues[Q_WHOLESALE_VERIFICATION_REQUIRED_FUTURE].append({"canonical_id": cid, **s})
        elif (basket.get("Acquisition_quantity_coverage_pct") or 0) < 50:
            queues[Q_NEEDS_SEED_ACQUISITION_PRICE].append({"canonical_id": cid, **s})
        elif basket.get("Freight") == FREIGHT_UNKNOWN:
            queues[Q_NEEDS_FREIGHT_EVIDENCE].append({"canonical_id": cid, **s})
        else:
            queues[Q_OWNER_REVIEW].append({"canonical_id": cid, **s})
    return {"kind": "M3SeedBasketQueues", "queues": queues, "DEVELOPMENT_NO_OUTREACH": True}


def apply_va_basket_update(
    store: Any,
    canonical_id: str,
    *,
    action: str,
    note: str | None = None,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    action_u = str(action or "").upper().strip()
    if action_u in VA_FORBIDDEN:
        return {"ok": False, "error": "forbidden_action", "action": action_u, "DEVELOPMENT_NO_OUTREACH": True}
    if action_u not in VA_ALLOWED:
        return {"ok": False, "error": "unknown_or_disallowed_action", "action": action_u}
    row = None
    if hasattr(store, "get"):
        row = store.get(canonical_id)
    if row is None and hasattr(store, "_rows"):
        row = (store._rows or {}).get(canonical_id)
    if not isinstance(row, dict):
        return {"ok": False, "error": "opportunity_not_found"}
    notes = list(row.get("VA_basket_notes") or [])
    if note:
        notes.append({"at": _utc(), "action": action_u, "note": note})
    row["VA_basket_notes"] = notes[-20:]
    if evidence and action_u == "ENTER_KNOWN_SUPPLIER_PRICE":
        # Minimal operator path for future known supplier — compare vs public later
        prices = list(row.get("operator_known_supplier_prices") or [])
        prices.append(
            {
                "Supplier": evidence.get("Supplier") or evidence.get("seller"),
                "Scientific_name": evidence.get("Scientific_name") or evidence.get("species"),
                "Product": evidence.get("Product"),
                "unit_price": _num(evidence.get("unit_price") or evidence.get("price")),
                "UOM": evidence.get("UOM") or "LB",
                "Package_size": evidence.get("Package_size"),
                "Availability": evidence.get("Availability"),
                "Freight": evidence.get("Freight"),
                "Terms": evidence.get("Terms"),
                "notes": evidence.get("notes"),
                "entered_at": _utc(),
                "label": "OPERATOR_KNOWN_NOT_PUBLIC_AUTOMATED",
            }
        )
        row["operator_known_supplier_prices"] = prices[-50:]
    if evidence and action_u in {"ATTACH_PRICE_SHEET", "NORMALIZE_PACKAGE", "VALIDATE_SPECIES"}:
        attached = list(row.get("VA_basket_evidence") or [])
        attached.append({**evidence, "action": action_u, "at": _utc()})
        row["VA_basket_evidence"] = attached[-40:]
    if hasattr(store, "_rows"):
        store._rows[canonical_id] = row
    try:
        if hasattr(store, "save"):
            store.save()
    except Exception:
        pass
    return {"ok": True, "canonical_id": canonical_id, "action": action_u, "DEVELOPMENT_NO_OUTREACH": True}
