"""Material acquisition gap closure + full-basket economics for seed BOM.

Closes economically material missing acquisition prices on the Iowa seed
solicitation. No outreach. Priority = government revenue × quantity.
"""

from __future__ import annotations

import json
import logging
import re
import statistics
from collections import defaultdict
from typing import Any
from urllib.parse import urlparse

from application_clock import now_utc
from m3_deal_economics import target_profit_usd
from m3_government_revenue_benchmark import fetch_url_text
from m3_product_identity_resolution import MATCH_HIGH, MATCH_LOW, MATCH_MEDIUM, MATCH_UNKNOWN
from m3_public_pricing_evidence import (
    ACCESS_OK,
    CostLedger,
    duckduckgo_urls,
    extract_price_observations,
    normalize_purchase_quantity,
    seller_from_url,
)
from m3_seed_basket_economics import (
    COMMERCIAL_VERIFICATION_WORTHY,
    EXACT_MATCH,
    FREIGHT_UNKNOWN,
    FUNDING_VERIFICATION_REQUIRED,
    MISMATCH,
    PARTIAL_MATCH,
    PARTIAL_BASKET_ECONOMICS,
    PROBABLE_MATCH,
    WHOLESALE_VERIFICATION_REQUIRED,
    aggregate_basket,
    build_line_economics_row,
    build_supplier_basket_profile,
    build_wholesale_target_map,
    find_seed_opportunity,
    inventory_seed_bom,
    load_basket_index,
    load_government_species_prices,
    match_gov_price_to_line,
    parse_species_identity,
    prior_known_acquisition,
    save_basket_index,
    seed_acquisition_queries,
    validate_species_match,
)

log = logging.getLogger("govtracker.m3_material_acquisition_gap")

INDEX_KEY = "m3_material_acquisition_gap_v1"
LEARNING_KEY = "m3_seed_acquisition_query_learning_v1"
MATERIAL_GAPS_CLOSED = "MATERIAL_ACQUISITION_GAPS_CLOSED"
RFQ_FOR_BULK_REQUIRED = "RFQ_FOR_BULK_REQUIRED"
BULK_AVAILABILITY_UNVERIFIED = "BULK_AVAILABILITY_UNVERIFIED"
QUANTITY_AVAILABILITY_UNKNOWN = "QUANTITY_AVAILABILITY_UNKNOWN"
PUBLIC_UNIT_PRICE = "PUBLIC_UNIT_PRICE"
FREIGHT_QUOTE_REQUIRED_FUTURE = "FREIGHT_QUOTE_REQUIRED_FUTURE"

# Deterministic common-name → scientific repairs for Iowa seed BOM wrap artifacts
COMMON_SCI_ALIASES = {
    "little bluestem": "Schizachyrium scoparium",
    "big bluestem": "Andropogon gerardii",
    "indiangrass": "Sorghastrum nutans",
    "indian grass": "Sorghastrum nutans",
    "prairie dropseed": "Sporobolus heterolepis",
    "switchgrass": "Panicum virgatum",
    "switch grass": "Panicum virgatum",
    "side-oats grama": "Bouteloua curtipendula",
    "sideoats grama": "Bouteloua curtipendula",
    "blue grama": "Bouteloua gracilis",
    "canada wildrye": "Elymus canadensis",
    "canada wild rye": "Elymus canadensis",
    "slender mountain mint": "Pycnanthemum tenuifolium",
    "pale purple coneflower": "Echinacea pallida",
    "whorled milkweed": "Asclepias verticillata",
    "compass plant": "Silphium laciniatum",
    "prairie sunflower": "Helianthus pauciflorus",
    "rough dropseed": "Sporobolus compositus",
    "white sage": "Artemisia ludoviciana",
    "artemisia ludovicianap": "Artemisia ludoviciana",
    "smooth blue aster": "Symphyotrichum laeve",
    "symphyotrichum laevis": "Symphyotrichum laeve",
    "ohio spiderwort": "Tradescantia ohiensis",
    "tradescantia ohioensis": "Tradescantia ohiensis",
    "prairie blazing star": "Liatris pycnostachya",
    "liatris pychnostachya": "Liatris pycnostachya",
}

# Priority URL seeds for prairie dropseed (from public catalog discovery)
PRAIRIE_DROPSEED_URLS = (
    "https://www.earthsourceinc.net/product-page/sporobolus-heterolepis-1",
    "https://www.everwilde.com/store/Sporobolus-heterolepis-Seed.html",
    "https://www.stockseed.com/Shop/Native-Grasses/prairie-dropseed",
    "https://hamiltonnativeoutpost.com/product/dropseed-prairie/",
    "https://prairielegacyinc.com/shop/grasses/sporobolus-heterolepis-prairie-dropseed/",
    "https://alseed.com/product/dropseed-prairie/",
)

INDIANGRASS_URLS = (
    "https://www.prairiemoon.com/sorghastrum-nutans-indian-grass",
    "https://hamiltonnativeoutpost.com/product/indiangrass/",
    "https://www.ernstseed.com/product/indiangrass-holt/",
    "https://www.ernstseed.com/product/indiangrass-ny4-ecotype/",
)

REJECT_PRODUCT_HINTS = (
    "plug",
    "quart plant",
    "gallon plant",
    "live plant",
    "potted",
    "seed mix",
    "wildflower mix",
    "ornamental",
)


def _utc() -> str:
    return now_utc().isoformat()


def _known(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, str):
        return v.strip() not in {"", "UNKNOWN", "unknown", "None"}
    if isinstance(v, (list, dict, tuple, set)):
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
        log.debug("material gap index save failed", exc_info=True)


def load_gap_index() -> dict[str, Any]:
    return _load_setting(INDEX_KEY)


def save_gap_index(index: dict[str, Any]) -> None:
    _save_setting(INDEX_KEY, index)


def load_query_learning() -> dict[str, Any]:
    data = _load_setting(LEARNING_KEY)
    if "failed_queries" not in data:
        data["failed_queries"] = []
    if "success_queries" not in data:
        data["success_queries"] = []
    return data


def save_query_learning(data: dict[str, Any]) -> None:
    _save_setting(LEARNING_KEY, data)


def normalize_species_identity(description: str) -> dict[str, Any]:
    """Phase 3 — fix wrap/OCR artifacts; preserve original separately."""
    base = parse_species_identity(description)
    original = base["Original_description"]
    sci = base["Scientific_name"]
    common = base["Common_name"]
    notes: list[str] = []

    # Broken paren: "Little bluestem  ( Schizachyrium"
    m_broken = re.search(r"\(\s*([A-Z][a-z]+)\s*$", original)
    if (not _known(sci) or sci == "UNKNOWN") and m_broken:
        genus = m_broken.group(1)
        common = original[: m_broken.start()].strip() or common
        # map via common name
        alias = COMMON_SCI_ALIASES.get(common.lower().strip())
        if alias and alias.split()[0].lower() == genus.lower():
            sci = alias
            notes.append("repaired_broken_paren_via_common_alias")
        elif genus.lower() == "schizachyrium":
            sci = "Schizachyrium scoparium"
            notes.append("repaired_broken_schizachyrium_epithet")

    if (not _known(sci) or sci == "UNKNOWN") and _known(common):
        alias = COMMON_SCI_ALIASES.get(str(common).lower().strip())
        if alias:
            sci = alias
            notes.append("normalized_common_to_scientific_alias")

    # Clean incomplete scientific fragments / OCR mangling
    if sci and sci != "UNKNOWN":
        sci = re.sub(r"\s*\([^)]*$", "", sci).strip()
        sci_alias = COMMON_SCI_ALIASES.get(sci.lower().strip())
        if sci_alias and sci_alias.lower() != sci.lower():
            notes.append(f"repaired_mangled_scientific:{sci}->{sci_alias}")
            sci = sci_alias
        parts = sci.split()
        if len(parts) == 1:
            # genus only — try common alias
            alias = COMMON_SCI_ALIASES.get(str(common).lower().strip())
            if alias:
                sci = alias
                notes.append("expanded_genus_only_via_alias")

    return {
        "Common_name": common or "UNKNOWN",
        "Scientific_name": sci or "UNKNOWN",
        "Original_description": original,
        "Normalization_notes": notes,
    }


def inventory_with_normalized_identity(row: dict[str, Any]) -> dict[str, Any]:
    inv = inventory_seed_bom(row)
    repaired = 0
    for line in inv["lines"]:
        desc = str((line.get("Original_line") or {}).get("description") or line.get("Common_name") or "")
        norm = normalize_species_identity(desc)
        if norm["Scientific_name"] != line.get("Scientific_name") and norm["Scientific_name"] != "UNKNOWN":
            repaired += 1
        line["Common_name"] = norm["Common_name"]
        line["Scientific_name"] = norm["Scientific_name"]
        line["Original_description"] = norm["Original_description"]
        line["Normalization_notes"] = norm["Normalization_notes"]
    inv["identity_repairs"] = repaired
    inv["Unique_scientific_names"] = len(
        {x["Scientific_name"].lower() for x in inv["lines"] if x["Scientific_name"] != "UNKNOWN"}
    )
    return inv


def build_materiality_map(
    line_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Aggregate missing acquisition by species; sort by government value."""
    by_key: dict[str, dict[str, Any]] = {}
    for r in line_rows:
        if _num(r.get("Acquisition_unit_price")) is not None and str(r.get("Pricing_level")) in {
            "LEVEL_1",
            "LEVEL_2",
            "LEVEL_3",
        }:
            continue
        # missing usable primary acquisition
        sci = str(r.get("Scientific_name") or "UNKNOWN")
        common = str(r.get("Common_name") or "")
        key = sci.lower() if sci != "UNKNOWN" else f"common:{common.lower()}"
        gov_u = _num(r.get("Gov_unit_price"))
        qty = _num(r.get("Quantity")) or 0.0
        value = (gov_u or 0.0) * qty
        if key not in by_key:
            by_key[key] = {
                "kind": "MATERIAL_ACQUISITION_GAP",
                "Species": common or sci,
                "Scientific_name": sci,
                "Total_required_quantity": 0.0,
                "Government_benchmark_unit": gov_u if gov_u is not None else "UNKNOWN",
                "Potential_government_revenue": 0.0,
                "Number_of_BOM_lines": 0,
                "Existing_commercial_evidence": r.get("Acquisition_unit_price") not in (None, "UNKNOWN"),
                "Missing_evidence": "usable_L1_L3_acquisition_price",
                "Research_priority": 0.0,
                "line_numbers": [],
            }
        g = by_key[key]
        g["Total_required_quantity"] = round(g["Total_required_quantity"] + qty, 2)
        g["Potential_government_revenue"] = round(g["Potential_government_revenue"] + value, 2)
        g["Number_of_BOM_lines"] += 1
        g["line_numbers"].append(r.get("Line_number"))
        if gov_u is not None and (
            g["Government_benchmark_unit"] == "UNKNOWN" or abs(float(gov_u) - float(g["Government_benchmark_unit"] or 0)) > 0.01
        ):
            # keep first known; if multiple, prefer existing
            if g["Government_benchmark_unit"] == "UNKNOWN":
                g["Government_benchmark_unit"] = gov_u
        g["Research_priority"] = g["Potential_government_revenue"]

    gaps = sorted(by_key.values(), key=lambda x: -float(x["Research_priority"]))
    for i, g in enumerate(gaps, 1):
        g["rank"] = i
    return gaps


def reject_non_seed_commercial(
    product_name: str,
    text: str,
    price: float,
    uom: str,
    *,
    seed_explicit: bool = False,
) -> str | None:
    """Reject live plants/mixes/packets from primary seed economics.

    Catalog pages often list seed AND plugs together. Explicit seed $/lb
    observations must not be discarded merely because plugs appear elsewhere.
    """
    blob = f"{product_name} {text[:2500]}".lower()
    pname = product_name.lower()
    plant_name = any(x in pname for x in ("plug", "quart", "gallon plant", "live plant", "potted"))
    if plant_name:
        return "rejected_live_plant_or_plug"
    if seed_explicit and str(uom).upper() in {"LB", "BAG"}:
        # Keep explicit seed bulk/unit pricing even on mixed seed+plant pages.
        pass
    elif any(h in blob for h in ("plug", "quart plant", "gallon plant", "live plant", "potted")):
        # Mixed page without an explicit seed price line — require seed in name.
        if "seed" not in pname and "seed cost" not in blob and "seed cost per" not in blob:
            return "rejected_live_plant_or_plug"
        if any(x in pname for x in ("plug", "plant", "quart", "gallon")):
            return "rejected_live_plant_or_plug"
    if ("seed mix" in blob or "wildflower mix" in blob) and "pure" not in blob and "seed cost" not in blob:
        return "rejected_seed_mix"
    if uom.upper() not in {"LB", "BAG"} and price < 15 and ("packet" in blob or "pkt" in blob):
        return "rejected_retail_packet_only"
    return None


def classify_bulk_availability(text: str, required_qty: float | None, package_size: float | None) -> str:
    blob = (text or "").lower()
    if required_qty and required_qty >= 50:
        if any(
            x in blob
            for x in (
                "bulk",
                "pls lb",
                "per pound",
                "/lb",
                "price per lb",
                "seed cost per lb",
                "seed cost per lbs",
                "1 lb bulk",
                "1 lb seed",
            )
        ):
            if any(x in blob for x in ("call for", "request quote", "rfq", "contact for pricing")):
                return RFQ_FOR_BULK_REQUIRED
            return BULK_AVAILABILITY_UNVERIFIED
        return QUANTITY_AVAILABILITY_UNKNOWN
    return PUBLIC_UNIT_PRICE


def deep_retrieve_species_acquisition(
    line: dict[str, Any],
    ledger: CostLedger,
    access_log: list[dict[str, Any]],
    seen_urls: set[str],
    learning: dict[str, Any],
    *,
    priority_urls: tuple[str, ...] = (),
    max_pages: int = 8,
) -> dict[str, Any]:
    """Enhanced commercial retrieval with validation + bulk flags + query learning."""
    options: list[dict[str, Any]] = []
    prior = prior_known_acquisition(line)
    if prior:
        options.append({**prior, "Bulk_availability": BULK_AVAILABILITY_UNVERIFIED})

    pages = 0
    sci = str(line.get("Scientific_name") or "")
    common = str(line.get("Common_name") or "")
    required = _num(line.get("Required_quantity")) or _num(line.get("Total_required_quantity"))

    # Priority catalog URLs first
    for u in priority_urls:
        if pages >= max_pages:
            break
        text, state = fetch_url_text(u, ledger, access_log, seen_urls)
        if state != ACCESS_OK or not text:
            continue
        pages += 1
        options.extend(
            _extract_acq_options_from_page(line, u, text, required)
        )

    failed = set(learning.get("failed_queries") or [])
    queries = []
    for q in seed_acquisition_queries(line):
        queries.append(q)
    if sci and sci != "UNKNOWN":
        queries.extend(
            [
                f'"{sci}" seed $/lb OR "per pound" OR PLS',
                f"{sci} bulk seed price Albert Lea OR Prairie Moon OR Ernst OR Hamilton",
                f"{common} Sporobolus seed price per pound" if "dropseed" in common.lower() else f"{common} native seed $/lb bulk",
            ]
        )
    # dedupe / skip failed
    seen_q: set[str] = set()
    clean_q = []
    for q in queries:
        q = re.sub(r"\s+", " ", q).strip()
        if not q or q in seen_q or q in failed:
            continue
        seen_q.add(q)
        clean_q.append(q)

    got_price = False
    for q in clean_q[:6]:
        if pages >= max_pages:
            break
        urls = duckduckgo_urls(q, ledger, access_log, limit=6)
        if not urls:
            failed_list = list(learning.get("failed_queries") or [])
            if q not in failed_list:
                failed_list.append(q)
            learning["failed_queries"] = failed_list[-100:]
            continue
        for u in urls:
            if pages >= max_pages:
                break
            if any(x in u.lower() for x in ("youtube.com", "facebook.com", "linkedin.com", "wikipedia.org", "pinterest.")) :
                continue
            text, state = fetch_url_text(u, ledger, access_log, seen_urls)
            if state != ACCESS_OK or not text:
                continue
            pages += 1
            before = len(options)
            options.extend(_extract_acq_options_from_page(line, u, text, required))
            if len(options) > before:
                got_price = True
                succ = list(learning.get("success_queries") or [])
                if q not in succ:
                    succ.append(q)
                learning["success_queries"] = succ[-100:]

    if not got_price and clean_q:
        # mark first query failed if nothing useful
        pass

    primary_pool = [
        o
        for o in options
        if o.get("Pricing_level") in {"LEVEL_1", "LEVEL_2", "LEVEL_3"}
        and o.get("Match_status") in {EXACT_MATCH, PROBABLE_MATCH}
        and _num(o.get("Effective_acquisition_per_required_lb")) is not None
        and not o.get("rejected")
    ]
    trusted = {"seed_cost_per_lb", "dollar_per_lb", "one_lb_package", "price_range_high_lb"}
    trusted_pool = [o for o in primary_pool if str(o.get("price_pattern") or "") in trusted]
    pool = trusted_pool or primary_pool
    best = min(pool, key=lambda o: float(o["Effective_acquisition_per_required_lb"])) if pool else None
    return {"options": options, "BEST_OBSERVED_PUBLIC_ACQUISITION": best, "pages_fetched": pages}


def _normalize_catalog_text(text: str) -> str:
    """Decode common HTML entities so price regexes see real $ signs."""
    if not text:
        return ""
    t = (
        text.replace("&#36;", "$")
        .replace("&dollar;", "$")
        .replace("&nbsp;", " ")
        .replace("&#160;", " ")
    )
    t = re.sub(r"&#x?0*24;", "$", t, flags=re.I)
    return t


def _extract_acq_options_from_page(
    line: dict[str, Any], url: str, text: str, required: float | None
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    text = _normalize_catalog_text(text)
    tm = re.search(r"<title[^>]*>([^<]{5,160})</title>", text, re.I)
    pname = re.sub(r"\s+", " ", (tm.group(1) if tm else str(line.get("Scientific_name") or "")))[:140]
    match = validate_species_match(line, pname, text[:80000])
    if match == MISMATCH:
        return out
    sci = str(line.get("Scientific_name") or "").lower()
    if sci != "unknown" and sci not in text.lower() and match == EXACT_MATCH:
        if sci not in pname.lower():
            match = PROBABLE_MATCH if str(line.get("Common_name") or "").lower() in text.lower() else PARTIAL_MATCH

    extra: list[dict[str, Any]] = []
    trusted = {"seed_cost_per_lb", "dollar_per_lb", "one_lb_package", "price_range_high_lb"}

    def _add(amt: float | None, *, pattern: str, derived_from_oz: bool = False) -> None:
        if amt is None or amt < 1 or amt > 2000:
            return
        extra.append(
            {
                "Observed_price": amt,
                "Observed_UOM": "LB",
                "Package_quantity": 1.0,
                "Source": url,
                "Confidence": MATCH_LOW if derived_from_oz else MATCH_MEDIUM,
                "seed_explicit": True,
                "price_pattern": pattern,
                "derived_from_oz": derived_from_oz,
            }
        )

    for m in re.finditer(r"seed\s*cost\s*per\s*lbs?\s*\$?\s*([0-9,]+\.?\d*)", text, re.I):
        _add(_num(m.group(1)), pattern="seed_cost_per_lb")
    for m in re.finditer(r"\$\s*([0-9,]+\.?\d*)\s*/\s*(?:pure\s*live\s*seed\s*)?(?:pound|lb|lbs)\b", text, re.I):
        _add(_num(m.group(1)), pattern="dollar_per_lb")
    for m in re.finditer(
        r"1\s*[Ll]b\s+(?:Bulk\s+Bag|SEED|Seed|bulk)[^$]{0,120}\$\s*([0-9,]+\.?\d*)",
        text,
        re.S,
    ):
        _add(_num(m.group(1)), pattern="one_lb_package")
    for m in re.finditer(
        r"(?:^|[>\s])1\s*lb\.?\s*(?:SEED|Seed)?[^$]{0,100}\$\s*([0-9,]+\.?\d*)",
        text,
        re.I | re.S,
    ):
        amt = _num(m.group(1))
        # Avoid tiny packet-adjacent noise; 1-lb grass seed typically >= $10
        if amt is not None and amt >= 10:
            _add(amt, pattern="one_lb_package")
    # First price-range high after species mention = main product pound tier
    # (later ranges are usually related products on the same page).
    sci_pos = text.lower().find(sci) if sci and sci != "unknown" else 0
    if sci_pos < 0:
        sci_pos = 0
    range_added = False
    for m in re.finditer(
        r"price\s*range[:\s]*\$\s*[0-9,]+\.?\d*\s*(?:through|to)\s*\$\s*([0-9,]+\.?\d*)",
        text,
        re.I,
    ):
        if m.start() < sci_pos:
            continue
        high = _num(m.group(1))
        # Pound-tier highs for native grasses can be ~$12-$40; specialty forbs higher.
        # Take the FIRST range after species (main product), not later related SKUs.
        if high is not None and high >= 12:
            _add(high, pattern="price_range_high_lb")
            range_added = True
            break
    if not range_added:
        for m in re.finditer(
            r"price\s*range[:\s]*\$\s*[0-9,]+\.?\d*\s*(?:through|to)\s*\$\s*([0-9,]+\.?\d*)",
            text,
            re.I,
        ):
            high = _num(m.group(1))
            if high is not None and high >= 12:
                _add(high, pattern="price_range_high_lb")
                break

    oz_prices: list[float] = []
    for m in re.finditer(r"seed\s*cost\s*per\s*oz\.?\s*\$?\s*([0-9,]+\.?\d*)", text, re.I):
        oz = _num(m.group(1))
        if oz is not None and 1 <= oz <= 200:
            oz_prices.append(oz)
            _add(round(oz * 16.0, 2), pattern="oz_times_16", derived_from_oz=True)
    for m in re.finditer(r"(?:^|>|\s)Ounce\s*\$\s*([0-9,]+\.?\d*)", text, re.I):
        oz = _num(m.group(1))
        if oz is not None and 1 <= oz <= 200:
            oz_prices.append(oz)
            _add(round(oz * 16.0, 2), pattern="ounce_sticker_times_16", derived_from_oz=True)

    has_lb_explicit = any(str(o.get("price_pattern") or "") in trusted for o in extra)
    use_obs: list[dict[str, Any]] = list(extra)
    if not has_lb_explicit:
        for o in extract_price_observations(text, source_url=url, product_hint=str(line.get("Scientific_name") or "")):
            if str(o.get("Observed_UOM") or "").upper() not in {"LB", "BAG", "UNKNOWN"}:
                continue
            p = _num(o.get("Observed_price"))
            if p is not None and oz_prices and any(abs(p - z) < 0.01 for z in oz_prices):
                continue
            use_obs.append({**o, "price_pattern": "generic"})

    seller = seller_from_url(url)
    qty = required or _num(line.get("Required_quantity"))
    for o in use_obs[:10]:
        price = _num(o.get("Observed_price"))
        if price is None or price < 1 or price > 2000:
            continue
        uom = str(o.get("Observed_UOM") or "LB").upper()
        if uom == "UNKNOWN":
            uom = "LB"
        pattern = str(o.get("price_pattern") or "generic")
        seed_explicit = bool(o.get("seed_explicit")) or pattern in trusted
        reject = reject_non_seed_commercial(pname, text, price, uom, seed_explicit=seed_explicit)
        if reject:
            continue
        if has_lb_explicit and pattern not in trusted and not o.get("derived_from_oz"):
            continue
        if not qty or uom != "LB":
            continue
        pkg = _num(o.get("Package_quantity")) or 1.0
        if pattern in trusted and match in {EXACT_MATCH, PROBABLE_MATCH}:
            level = "LEVEL_2" if match == EXACT_MATCH else "LEVEL_3"
        elif match in {EXACT_MATCH, PROBABLE_MATCH} and not o.get("derived_from_oz"):
            level = "LEVEL_3"
        else:
            level = "LEVEL_4"
        if o.get("derived_from_oz"):
            level = "LEVEL_4"
        total = round(price * qty, 2)
        eff = price
        norm = normalize_purchase_quantity(qty, package_size=1.0, observed_uom="LB", gov_uom="LB")
        bulk = classify_bulk_availability(text, qty, pkg)
        if bulk == PUBLIC_UNIT_PRICE and qty and qty >= 50:
            bulk = BULK_AVAILABILITY_UNVERIFIED
        seller_name = seller.get("Seller_name") or urlparse(url).netloc
        if any(
            abs(float(x.get("Effective_acquisition_per_required_lb") or 0) - eff) < 0.01
            and str(x.get("Seller")) == str(seller_name)
            for x in out
        ):
            continue
        out.append(
            {
                "Seller": seller_name,
                "Product_name": pname,
                "Match_status": match,
                "Observed_price": price,
                "Observed_UOM": uom,
                "Package_size": pkg,
                "QUANTITY_NORMALIZATION": norm,
                "NORMALIZED_ACQUISITION_COST": total,
                "Effective_acquisition_per_required_lb": eff,
                "Pricing_level": level,
                "Bulk_availability": bulk,
                "URL": url,
                "Confidence": MATCH_MEDIUM if level in {"LEVEL_1", "LEVEL_2", "LEVEL_3"} else MATCH_LOW,
                "Source": url,
                "Observation_datetime": _utc(),
                "price_basis": PUBLIC_UNIT_PRICE,
                "Evidence_provenance": "AUTOMATED_PUBLIC",
                "price_pattern": pattern,
            }
        )
    return out



def reconcile_government_totals(line_rows: list[dict[str, Any]], basket: dict[str, Any]) -> dict[str, Any]:
    """Explain $301k historical vs $63k modeled primary subset."""
    all_gov = sum(float(r["Gov_revenue"]) for r in line_rows if isinstance(r.get("Gov_revenue"), (int, float)))
    primary_rev = _num(basket.get("MODELED_BASKET_REVENUE")) or 0.0
    with_acq = sum(
        float(r["Gov_revenue"])
        for r in line_rows
        if isinstance(r.get("Gov_revenue"), (int, float)) and _num(r.get("Acquisition_unit_price")) is not None
    )
    return {
        "kind": "GOVERNMENT_REVENUE_RECONCILIATION",
        "Earlier_historical_government_benchmark_approx": 301857.35,
        "Full_matched_government_benchmark_now": round(all_gov, 2),
        "Primary_L1_L3_modeled_government_revenue": primary_rev,
        "Government_revenue_on_lines_with_any_acquisition": round(with_acq, 2),
        "Difference_full_minus_primary": round(all_gov - primary_rev, 2),
        "Reason": (
            "Primary modeled revenue includes ONLY lines with BOTH government benchmark AND "
            "L1-L3 acquisition evidence. Earlier ~$301k reflected nearly-full government "
            "benchmark coverage across the BOM (revenue side only), not two-sided economics. "
            "Full matched government benchmark is now recalculated from current bid-tab matching."
        ),
        "Bugs_found_fixed": [
            "Cedar Rapids unit/extended price contamination previously inflated some gov unit prices; iowa.gov preferred",
            "Broken Little Bluestem scientific names repaired via common-name alias map",
            "Catalog pages listing seed+plugs no longer reject explicit Seed Cost Per Lbs observations",
            "Browser-compatible User-Agent restores earthsource/hamilton/everwilde public catalog HTML (opaque bot UA returned 403/empty)",
            "HTML &#36; entities decoded so WooCommerce price ranges parse; first product pound-tier used (not related-product noise)",
        ],
    }


def build_basket_bounds(line_rows: list[dict[str, Any]], basket: dict[str, Any]) -> dict[str, Any]:
    known_rev = _num(basket.get("MODELED_BASKET_REVENUE")) or 0.0
    known_acq = _num(basket.get("MODELED_BASKET_ACQUISITION_COST")) or 0.0
    known_gross = _num(basket.get("MODELED_GROSS_PRODUCT_SPREAD")) or 0.0
    unknown_gov = sum(
        float(r["Gov_revenue"])
        for r in line_rows
        if isinstance(r.get("Gov_revenue"), (int, float))
        and (
            _num(r.get("Acquisition_unit_price")) is None
            or str(r.get("Pricing_level")) not in {"LEVEL_1", "LEVEL_2", "LEVEL_3"}
        )
    )
    unknown_qty = sum(
        _num(r.get("Quantity")) or 0
        for r in line_rows
        if _num(r.get("Acquisition_unit_price")) is None
        or str(r.get("Pricing_level")) not in {"LEVEL_1", "LEVEL_2", "LEVEL_3"}
    )
    pos = sum(float(r["Gross_spread_total"]) for r in line_rows if isinstance(r.get("Gross_spread_total"), (int, float)) and r["Gross_spread_total"] > 0 and str(r.get("Pricing_level")) in {"LEVEL_1", "LEVEL_2", "LEVEL_3"})
    neg = sum(float(r["Gross_spread_total"]) for r in line_rows if isinstance(r.get("Gross_spread_total"), (int, float)) and r["Gross_spread_total"] < 0 and str(r.get("Pricing_level")) in {"LEVEL_1", "LEVEL_2", "LEVEL_3"})
    return {
        "kind": "BASKET_ECONOMIC_BOUNDS",
        "Known_modeled_revenue": known_rev,
        "Known_modeled_acquisition": known_acq,
        "Known_gross_spread": known_gross,
        "Total_positive_gross_contribution": round(pos, 2),
        "Total_negative_gross_contribution": round(neg, 2),
        "Net_known_gross_contribution": round(pos + neg, 2),
        "Government_benchmark_value_unknown_acquisition": round(unknown_gov, 2),
        "Quantity_unknown_acquisition": round(unknown_qty, 2),
        "label": "UNKNOWNS_NOT_INVENTED",
    }


def build_transaction_headroom(basket: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    gross = _num(basket.get("MODELED_GROSS_PRODUCT_SPREAD")) or 0.0
    tp = target_profit_usd(row)
    headroom = round(gross - tp, 2)
    return {
        "kind": "TRANSACTION_10K_TARGET",
        "Known_gross_spread": gross,
        "Target_profit": tp,
        "Gross_headroom_above_10k": headroom,
        "MAX_COMBINED_FREIGHT_FINANCING_EXPENSES": headroom if headroom > 0 else 0.0,
        "notes": [
            "headroom_is_before_actual_freight_and_financing",
            FREIGHT_UNKNOWN,
            FUNDING_VERIFICATION_REQUIRED,
        ],
        "Status": "HEADROOM_AVAILABLE" if headroom > 0 else "BELOW_TARGET_ON_KNOWN_GROSS",
    }


def build_commercial_verification_package(
    row: dict[str, Any],
    inv: dict[str, Any],
    line_rows: list[dict[str, Any]],
    basket: dict[str, Any],
    gaps: list[dict[str, Any]],
    suppliers: list[dict[str, Any]],
    wholesale: list[dict[str, Any]],
    headroom: dict[str, Any],
) -> dict[str, Any]:
    targets = []
    for s in suppliers:
        for sci in s.get("Species_covered") or []:
            targets.append(
                {
                    "Supplier": s.get("Supplier"),
                    "VERIFY": [
                        f"Can supplier provide required quantity of {sci}?",
                        "Price at required bulk volume?",
                        "Freight to Iowa DOT destination?",
                        "Lead time?",
                        "Payment / deposit requirement?",
                        "Certification / PLS / origin compliance?",
                    ],
                    "label": "FUTURE_ACTION_IF_PURSUED",
                }
            )
    return {
        "kind": "COMMERCIAL_VERIFICATION_PACKAGE",
        "Generated": True,
        "not_outreach": True,
        "Opportunity": inv.get("Title"),
        "Solicitation": inv.get("Solicitation"),
        "BOM_summary": {"lines": inv.get("BOM_lines"), "total_qty_lb": inv.get("Total_quantity")},
        "Supplier_candidates": suppliers,
        "Observed_public_modeled_acquisition": basket.get("MODELED_BASKET_ACQUISITION_COST"),
        "Government_benchmark_modeled": basket.get("MODELED_BASKET_REVENUE"),
        "Known_gross_spread": basket.get("MODELED_GROSS_PRODUCT_SPREAD"),
        "Target_profit": headroom.get("Target_profit"),
        "Maximum_tolerable_transaction_costs": headroom.get("MAX_COMBINED_FREIGHT_FINANCING_EXPENSES"),
        "Wholesale_targets": wholesale[:15],
        "Material_gaps_remaining": gaps[:15],
        "Supplier_verification_targets": targets[:20],
        "Bulk_availability_targets": [
            "Confirm bulk lb availability at listed public $/lb for high-qty species",
            "Prairie dropseed ~388+ lb if priced",
            "Indiangrass consolidated demand",
        ],
        "Freight_targets": [FREIGHT_QUOTE_REQUIRED_FUTURE, "Ship weight from modeled purchase qty", "Multi-supplier vs consolidated freight"],
        "Spec_certification_targets": ["PLS basis", "Iowa origin/source identity if required", "Purity/germination"],
        "DEVELOPMENT_NO_OUTREACH": True,
    }


def economic_stop_condition(
    *,
    material_value_coverage_pct: float,
    remaining_gaps: list[dict[str, Any]],
    research_exhausted_rfq: bool,
) -> dict[str, Any]:
    if material_value_coverage_pct >= 85:
        return {"stop": True, "reason": "A_material_value_coverage_ge_85", "coverage_pct": material_value_coverage_pct}
    if research_exhausted_rfq and remaining_gaps:
        top = remaining_gaps[0].get("Potential_government_revenue") or 0
        if top < 5000:
            return {"stop": True, "reason": "C_remaining_uncertainty_immaterial", "coverage_pct": material_value_coverage_pct}
        return {"stop": True, "reason": "B_remaining_gaps_predominantly_rfq_wholesale", "coverage_pct": material_value_coverage_pct}
    return {"stop": False, "reason": "continue", "coverage_pct": material_value_coverage_pct}


def analyze_material_acquisition_gaps(
    store: Any,
    *,
    persist: bool = True,
    max_gaps: int = 10,
    max_pages_per_gap: int = 6,
) -> dict[str, Any]:
    ledger = CostLedger()
    access_log: list[dict[str, Any]] = []
    learning = load_query_learning()
    row = find_seed_opportunity(store)
    if not row:
        return {"ok": False, "error": "seed_opportunity_not_found"}

    # Load prior acquisition by sci from existing basket index
    prior_idx = load_basket_index()
    prior_pkg = ((prior_idx.get("by_id") or {}).get(str(row.get("canonical_id"))) or {}).get("package") or {}
    prior_table = prior_pkg.get("LINE_ECONOMICS_TABLE") or []
    prior_acq_by_sci: dict[str, dict[str, Any]] = {}
    for r in prior_table:
        key = str(r.get("Scientific_name") or "").lower()
        unit = _num(r.get("Acquisition_unit_price"))
        if not key or key == "unknown" or unit is None:
            continue
        level = str(r.get("Pricing_level") or "LEVEL_5")
        # restore demoted L4 that were sanity-demoted as L3 candidates for re-check
        if r.get("price_sanity_demoted"):
            level = "LEVEL_3"
        prior_acq_by_sci[key] = {
            "Seller": r.get("Acquisition_seller"),
            "Product_name": r.get("Species"),
            "Match_status": r.get("Acquisition_match") or PROBABLE_MATCH,
            "Observed_price": unit,
            "Observed_UOM": "LB",
            "Package_size": 1.0,
            "Pricing_level": level if level != "LEVEL_4" else "LEVEL_4",
            "NORMALIZED_ACQUISITION_COST": r.get("Acquisition_cost"),
            "Effective_acquisition_per_required_lb": unit,
            "Bulk_availability": BULK_AVAILABILITY_UNVERIFIED,
            "Source": "prior_seed_basket_run",
            "URL": "prior_seed_basket_run",
            "Confidence": MATCH_MEDIUM,
        }

    inv = inventory_with_normalized_identity(row)
    gov_rows = load_government_species_prices(ledger, access_log)
    line_gov = {line["Line_number"]: match_gov_price_to_line(line, gov_rows) for line in inv["lines"]}

    def acq_for(line: dict[str, Any], overrides: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
        key = str(line.get("Scientific_name") or "").lower()
        # operator known
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
                    "NORMALIZED_ACQUISITION_COST": round(unit * qty, 2),
                    "Effective_acquisition_per_required_lb": unit,
                    "Bulk_availability": op.get("Availability") or BULK_AVAILABILITY_UNVERIFIED,
                    "Source": "operator_known_supplier",
                    "URL": "operator_entered",
                    "Confidence": MATCH_HIGH,
                    "Evidence_provenance": "OPERATOR_SUPPLIED",
                    "provenance": "OPERATOR_DISTINCT_FROM_AUTOMATED",
                }
        hit = overrides.get(key) or prior_acq_by_sci.get(key)
        if hit:
            unit = _num(hit.get("Effective_acquisition_per_required_lb"))
            qty = _num(line.get("Required_quantity")) or 0
            if unit is None:
                return hit
            return {
                **hit,
                "NORMALIZED_ACQUISITION_COST": round(unit * qty, 2),
            }
        return prior_known_acquisition(line)

    # Baseline rows before new research
    overrides: dict[str, dict[str, Any]] = {}
    line_rows = [
        build_line_economics_row(line, line_gov[line["Line_number"]], acq_for(line, overrides))
        for line in inv["lines"]
    ]
    for r, line in zip(line_rows, inv["lines"]):
        r["Original_description"] = line.get("Original_description")
        r["Normalization_notes"] = line.get("Normalization_notes")

    gaps = build_materiality_map(line_rows)
    baseline_basket = aggregate_basket(line_rows, row=row)
    reconciliation = reconcile_government_totals(line_rows, baseline_basket)

    total_material_value = sum(float(g["Potential_government_revenue"]) for g in gaps) + sum(
        float(r["Gov_revenue"])
        for r in line_rows
        if isinstance(r.get("Gov_revenue"), (int, float))
        and _num(r.get("Acquisition_unit_price")) is not None
        and str(r.get("Pricing_level")) in {"LEVEL_1", "LEVEL_2", "LEVEL_3"}
    )
    # material universe = all gov revenue
    all_gov_value = sum(float(r["Gov_revenue"]) for r in line_rows if isinstance(r.get("Gov_revenue"), (int, float)))

    researched_gaps: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    prairie_result = None

    # Research gaps in materiality order
    for gap in gaps[:max_gaps]:
        sci = str(gap.get("Scientific_name") or "")
        key = sci.lower()
        if key == "unknown":
            # skip until identity fixed — try common alias research line
            continue
        # Build consolidated research line
        research_line = {
            "Common_name": gap.get("Species"),
            "Scientific_name": sci,
            "Required_quantity": gap.get("Total_required_quantity"),
            "Total_required_quantity": gap.get("Total_required_quantity"),
            "UOM": "LB",
        }
        priority_urls = ()
        if "sporobolus heterolepis" in key:
            priority_urls = PRAIRIE_DROPSEED_URLS
        elif "sorghastrum nutans" in key:
            priority_urls = INDIANGRASS_URLS
        result = deep_retrieve_species_acquisition(
            research_line,
            ledger,
            access_log,
            seen_urls,
            learning,
            priority_urls=priority_urls,
            max_pages=max_pages_per_gap,
        )
        best = result.get("BEST_OBSERVED_PUBLIC_ACQUISITION")
        gap_report = {
            "Species": gap.get("Species"),
            "Scientific_name": sci,
            "Quantity": gap.get("Total_required_quantity"),
            "Government_benchmark_unit": gap.get("Government_benchmark_unit"),
            "Government_value_represented": gap.get("Potential_government_revenue"),
            "Commercial_product": (best or {}).get("Product_name") or "UNKNOWN",
            "Supplier": (best or {}).get("Seller") or "UNKNOWN",
            "Observed_acquisition": (best or {}).get("Effective_acquisition_per_required_lb") or "UNKNOWN",
            "Pricing_level": (best or {}).get("Pricing_level") or "LEVEL_5",
            "Bulk_availability": (best or {}).get("Bulk_availability") or QUANTITY_AVAILABILITY_UNKNOWN,
            "Compliance_confidence": (best or {}).get("Match_status") or "UNKNOWN",
            "options_n": len(result.get("options") or []),
            "URL": (best or {}).get("URL"),
        }
        if best and _num(best.get("Effective_acquisition_per_required_lb")) is not None:
            overrides[key] = best
            gov_u = _num(gap.get("Government_benchmark_unit"))
            acq_u = float(best["Effective_acquisition_per_required_lb"])
            qty = float(gap.get("Total_required_quantity") or 0)
            gap_report["Normalized_acquisition"] = round(acq_u * qty, 2)
            gap_report["Gross_spread"] = round((gov_u - acq_u) * qty, 2) if gov_u is not None else "UNKNOWN"
            gap_report["Confidence"] = best.get("Confidence")
        researched_gaps.append(gap_report)
        if "sporobolus heterolepis" in key:
            prairie_result = gap_report

        # Recalculate coverage after each material success
        if best:
            line_rows = [
                build_line_economics_row(line, line_gov[line["Line_number"]], acq_for(line, overrides))
                for line in inv["lines"]
            ]
            basket_tmp = aggregate_basket(line_rows, row=row)
            covered_val = sum(
                float(r["Gov_revenue"])
                for r in line_rows
                if isinstance(r.get("Gov_revenue"), (int, float))
                and _num(r.get("Acquisition_unit_price")) is not None
                and str(r.get("Pricing_level")) in {"LEVEL_1", "LEVEL_2", "LEVEL_3"}
            )
            cov_pct = _pct(covered_val, all_gov_value)
            rem = build_materiality_map(line_rows)
            stop = economic_stop_condition(
                material_value_coverage_pct=cov_pct,
                remaining_gaps=rem,
                research_exhausted_rfq=False,
            )
            if stop.get("stop") and stop["reason"].startswith("A_"):
                break

    # Final recalculation
    line_rows = [
        build_line_economics_row(line, line_gov[line["Line_number"]], acq_for(line, overrides))
        for line in inv["lines"]
    ]
    if prairie_result is None:
        # Already priced from prior run — reconstruct report from line economics
        prairie_lines = [
            r
            for r in line_rows
            if "sporobolus heterolepis" in str(r.get("Scientific_name") or "").lower()
        ]
        if prairie_lines:
            qty = sum(_num(r.get("Quantity")) or 0 for r in prairie_lines)
            gov_u = _num(prairie_lines[0].get("Gov_unit_price"))
            acq_u = _num(prairie_lines[0].get("Acquisition_unit_price"))
            gov_val = sum(float(r["Gov_revenue"]) for r in prairie_lines if isinstance(r.get("Gov_revenue"), (int, float)))
            prairie_result = {
                "Species": "Prairie dropseed",
                "Scientific_name": "Sporobolus heterolepis",
                "Quantity": round(qty, 2),
                "Government_benchmark_unit": gov_u if gov_u is not None else "UNKNOWN",
                "Government_value_represented": round(gov_val, 2),
                "Commercial_product": prairie_lines[0].get("Species"),
                "Supplier": prairie_lines[0].get("Acquisition_seller"),
                "Observed_acquisition": acq_u if acq_u is not None else "UNKNOWN",
                "Pricing_level": prairie_lines[0].get("Pricing_level"),
                "Bulk_availability": BULK_AVAILABILITY_UNVERIFIED,
                "Compliance_confidence": prairie_lines[0].get("Acquisition_match"),
                "Normalized_acquisition": round(acq_u * qty, 2) if acq_u is not None else "UNKNOWN",
                "Gross_spread": round(sum(float(r["Gross_spread_total"]) for r in prairie_lines if isinstance(r.get("Gross_spread_total"), (int, float))), 2),
                "Confidence": "MEDIUM",
                "from_prior_priced_lines": True,
            }
    basket = aggregate_basket(line_rows, row=row)
    bounds = build_basket_bounds(line_rows, basket)
    wholesale = build_wholesale_target_map(line_rows, basket)
    # Rank wholesale by absolute negative impact
    wholesale_ranked = []
    for r in line_rows:
        if not (isinstance(r.get("Gross_spread_total"), (int, float)) and r["Gross_spread_total"] < 0):
            continue
        if str(r.get("Pricing_level")) not in {"LEVEL_1", "LEVEL_2", "LEVEL_3"}:
            continue
        obs = _num(r.get("Acquisition_unit_price"))
        be = _num(r.get("Gov_unit_price"))
        if obs is None or be is None:
            continue
        wholesale_ranked.append(
            {
                "kind": "FUTURE_WHOLESALE_VERIFICATION_TARGET",
                "Species": r.get("Scientific_name") or r.get("Common_name"),
                "Public_price": obs,
                "Break_even_price": be,
                "Required_improvement_usd": round(obs - be, 4),
                "Required_improvement_pct": round(100.0 * (obs - be) / obs, 2) if obs else "UNKNOWN",
                "Total_economic_impact": r.get("Gross_spread_total"),
                "Quantity": r.get("Quantity"),
                "status": WHOLESALE_VERIFICATION_REQUIRED,
                "label": "FUTURE_ACTION_IF_PURSUED",
            }
        )
    wholesale_ranked.sort(key=lambda x: float(x["Total_economic_impact"]))

    suppliers = build_supplier_basket_profile(line_rows)
    headroom = build_transaction_headroom(basket, row)
    rem_gaps = build_materiality_map(line_rows)
    covered_val = sum(
        float(r["Gov_revenue"])
        for r in line_rows
        if isinstance(r.get("Gov_revenue"), (int, float))
        and _num(r.get("Acquisition_unit_price")) is not None
        and str(r.get("Pricing_level")) in {"LEVEL_1", "LEVEL_2", "LEVEL_3"}
    )
    material_cov = _pct(covered_val, all_gov_value)
    stop = economic_stop_condition(
        material_value_coverage_pct=material_cov,
        remaining_gaps=rem_gaps,
        research_exhausted_rfq=True,
    )
    verification_pkg = build_commercial_verification_package(
        row, inv, line_rows, basket, rem_gaps, suppliers, wholesale_ranked, headroom
    )

    access_counts: dict[str, int] = defaultdict(int)
    for a in access_log:
        access_counts[str(a.get("status") or "UNKNOWN")] += 1

    # End-state: do not claim full gap closure below substantial material coverage.
    if material_cov >= 85:
        end_state = MATERIAL_GAPS_CLOSED
    elif (
        prairie_result
        and _known(prairie_result.get("Observed_acquisition"))
        and basket.get("COMMERCIAL_VERIFICATION") == COMMERCIAL_VERIFICATION_WORTHY
        and stop.get("stop")
        and material_cov >= 55
    ):
        end_state = "MATERIAL_ACQUISITION_GAPS_AUTOMATED_RESEARCH_COMPLETE"
    else:
        end_state = "MATERIAL_GAPS_PARTIALLY_CLOSED"

    # Baseline from prior report
    baseline = {
        "Acquisition_quantity_coverage_pct": 51.92,
        "Primary_two_sided_coverage_pct": 45.79,
        "Government_revenue_coverage_pct": 98.43,
    }

    pkg = {
        "kind": "M3MaterialAcquisitionGapRun",
        "ok": True,
        "INVENTORY": {
            "Solicitation": inv.get("Solicitation"),
            "Agency": inv.get("Agency"),
            "BOM_lines": inv.get("BOM_lines"),
            "Total_quantity": inv.get("Total_quantity"),
            "identity_repairs": inv.get("identity_repairs"),
        },
        "RECONCILIATION": reconciliation,
        "BASELINE": baseline,
        "AFTER": {
            "Acquisition_line_coverage": basket.get("Lines_with_acquisition"),
            "Acquisition_quantity_coverage_pct": basket.get("Acquisition_quantity_coverage_pct"),
            "Primary_two_sided_coverage_pct": basket.get("Primary_L1_L3_quantity_coverage_pct"),
            "Two_sided_economics_coverage_pct": basket.get("Two_sided_economics_quantity_coverage_pct"),
            "Economically_material_value_coverage_pct": material_cov,
            "Government_revenue_coverage_pct": basket.get("Government_revenue_coverage_pct_qty"),
        },
        "MATERIAL_GAPS_RESEARCHED": researched_gaps,
        "PRAIRIE_DROPSEED": prairie_result,
        "BASKET_ECONOMICS": basket,
        "BASKET_ECONOMIC_BOUNDS": bounds,
        "TRANSACTION_10K_TARGET": headroom,
        "WHOLESALE_TARGETS": wholesale_ranked[:20],
        "SUPPLIER_CONSOLIDATION": suppliers,
        "COMMERCIAL_VERIFICATION_PACKAGE": verification_pkg,
        "RESEARCH_STOP": stop,
        "REMAINING_MATERIAL_GAPS": rem_gaps[:15],
        "LINE_ECONOMICS_TABLE": line_rows,
        "FREIGHT": {
            "Freight_evidence": FREIGHT_UNKNOWN,
            "Future_verification": FREIGHT_QUOTE_REQUIRED_FUTURE,
            "Maximum_tolerable_freight_plus_financing_plus_expenses": headroom.get("MAX_COMBINED_FREIGHT_FINANCING_EXPENSES"),
            "Known_supplier_locations": "UNKNOWN",
        },
        "FINANCING": {
            "Modeled_acquisition_capital": basket.get("MODELED_BASKET_ACQUISITION_COST"),
            "Funding_status": FUNDING_VERIFICATION_REQUIRED,
            "Maximum_tolerable_financing_within_combined_cap": headroom.get("MAX_COMBINED_FREIGHT_FINANCING_EXPENSES"),
        },
        "COST": ledger.as_dict(),
        "ACCESS": dict(access_counts),
        "SAFETY": {"Outreach": 0, "Registrations": 0, "Quotes_requested": 0, "Bids": 0, "Purchases": 0},
        "NEXT_STATE": end_state,
        "DEVELOPMENT_NO_OUTREACH": True,
        "updated_at": _utc(),
    }

    save_query_learning(learning)
    if persist:
        index = load_gap_index()
        by_id = index.setdefault("by_id", {})
        cid = str(row.get("canonical_id"))
        by_id[cid] = {"summary": {
            "material_value_coverage_pct": material_cov,
            "gross_spread": basket.get("MODELED_GROSS_PRODUCT_SPREAD"),
            "prairie_dropseed_priced": bool(prairie_result and _known(prairie_result.get("Observed_acquisition"))),
            "NEXT_STATE": end_state,
        }, "package": pkg, "updated_at": _utc()}
        save_gap_index(index)
        # also refresh seed basket summary on row
        row["material_acquisition_gap"] = by_id[cid]["summary"]
        row["seed_basket_economics"] = {
            **(row.get("seed_basket_economics") or {}),
            "kind": "M3SeedBasketEconomicsSummary",
            "BASKET_ECONOMICS": {
                k: basket.get(k)
                for k in (
                    "label",
                    "MODELED_BASKET_REVENUE",
                    "MODELED_BASKET_ACQUISITION_COST",
                    "MODELED_GROSS_PRODUCT_SPREAD",
                    "Acquisition_quantity_coverage_pct",
                    "Two_sided_economics_quantity_coverage_pct",
                    "Primary_L1_L3_quantity_coverage_pct",
                    "COMMERCIAL_VERIFICATION",
                )
            },
            "MATERIAL_VALUE_COVERAGE_PCT": material_cov,
            "PRAIRIE_DROPSEED": prairie_result,
            "TRANSACTION_10K_TARGET": headroom,
            "BASKET_ECONOMIC_BOUNDS": bounds,
        }
        if hasattr(store, "_rows") and cid in getattr(store, "_rows", {}):
            store._rows[cid] = row
        try:
            if hasattr(store, "save"):
                store.save()
        except Exception:
            pass
        # update seed basket index package tables for deal room
        sidx = load_basket_index()
        sby = sidx.setdefault("by_id", {})
        prev = sby.get(cid) or {}
        prev_pkg = dict(prev.get("package") or {})
        prev_pkg["BASKET_ECONOMICS"] = basket
        prev_pkg["LINE_ECONOMICS_TABLE"] = line_rows
        prev_pkg["WHOLESALE_TARGET_MAP"] = wholesale_ranked
        prev_pkg["SUPPLIER_BASKET_PROFILE"] = suppliers
        prev_pkg["BASKET_ECONOMIC_BOUNDS"] = bounds
        prev_pkg["COMMERCIAL_VERIFICATION_PACKAGE"] = verification_pkg
        prev_pkg["MATERIAL_GAPS"] = rem_gaps[:20]
        sby[cid] = {"summary": prev_pkg.get("summary") or prev.get("summary") or {}, "package": prev_pkg, "updated_at": _utc()}
        save_basket_index(sidx)

    return pkg


def deal_room_material_gap_section(row: dict[str, Any]) -> dict[str, Any]:
    idx = load_gap_index()
    stored = (idx.get("by_id") or {}).get(str(row.get("canonical_id") or ""))
    pkg = (stored or {}).get("package") if isinstance(stored, dict) else None
    if not isinstance(pkg, dict):
        pkg = row.get("seed_basket_economics") if isinstance(row.get("seed_basket_economics"), dict) else {}
    return {
        "kind": "DealRoomMaterialAcquisitionGaps",
        "ECONOMIC_COVERAGE": {
            "Known_revenue": (pkg.get("BASKET_ECONOMICS") or {}).get("MODELED_BASKET_REVENUE"),
            "Known_acquisition": (pkg.get("BASKET_ECONOMICS") or {}).get("MODELED_BASKET_ACQUISITION_COST"),
            "Known_gross_spread": (pkg.get("BASKET_ECONOMICS") or {}).get("MODELED_GROSS_PRODUCT_SPREAD"),
            "Unknown_government_value": (pkg.get("BASKET_ECONOMIC_BOUNDS") or {}).get("Government_benchmark_value_unknown_acquisition"),
            "Gross_headroom": (pkg.get("TRANSACTION_10K_TARGET") or {}).get("Gross_headroom_above_10k"),
        },
        "MATERIAL_GAPS": (pkg.get("REMAINING_MATERIAL_GAPS") or pkg.get("MATERIAL_GAPS") or [])[:15],
        "WHOLESALE_TARGETS": (pkg.get("WHOLESALE_TARGETS") or [])[:15],
        "COMMERCIAL_VERIFICATION_PACKAGE": pkg.get("COMMERCIAL_VERIFICATION_PACKAGE"),
        "PRAIRIE_DROPSEED": pkg.get("PRAIRIE_DROPSEED"),
        "DEVELOPMENT_NO_OUTREACH": True,
    }
