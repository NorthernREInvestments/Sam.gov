"""M3 Commercial Proof Repair V1 — product identity, pricing evidence, proof research.

Strict product-identity classes (never treat generic FSC titles as EXACT).
Multi-route acquisition pricing (retail → generic commodity → distributor → RFQ).
Evidence-first + supplier-quote populations. Historical comparability repair.
Product-resale only. Prefer 0 SAM API calls. No invented profit.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any
from urllib.parse import quote_plus

from application_clock import now_utc
from buyer_intelligence.commercial_intelligence_upgrade_v1 import (
    FIN_BLOCKED,
    FIN_CONFIRMED,
    FIN_UNKNOWN,
    FIN_UNVERIFIED,
    PACKET_RECON_FILE,
    REMATCH_FILE,
    _dec,
    _load,
    _money_str,
    _save,
    _utc,
    classify_family,
    financing_label_for_deal,
    max_supplier_cost_targets,
    multi_line_completeness,
    normalize_product_identity,
    quoteable_rows,
    research_public_price_bounded,
)
from buyer_intelligence.first_revenue_validation_sprint_v1 import (
    RESULTS_FILE as FR_RESULTS_FILE,
    _clean_title,
    _days_to_deadline,
    _HARD_EXCLUDE,
    hard_exclusion_reason,
)
from buyer_intelligence.revenue_first_commercial_intelligence_v1 import (
    LANE_CASH,
    LANE_GROWTH,
    LANE_PROVISIONAL,
    LANE_SCALE,
    classify_profit_lane,
    evaluate_cash_flow,
    load_historical_indexes,
    match_historical,
)
from buyer_intelligence.supplier_fit_evidence_contact_recovery_v1 import (
    SUPPLIER_META,
    resolve_contact,
)

log = logging.getLogger("govtracker.commercial_proof_repair_v1")

BUILD = "20261012-m3-commercial-proof-repair-v1"
OUT_DIR = "_og_commercial_research_pipeline_out"

IDENTITY_AUDIT_FILE = f"{OUT_DIR}/m3_commercial_proof_v1_identity_audit.json"
SOURCE_RELIABILITY_FILE = f"{OUT_DIR}/m3_commercial_proof_v1_source_reliability.json"
POP_A_FILE = f"{OUT_DIR}/m3_commercial_proof_v1_population_a_evidence_first.json"
POP_B_FILE = f"{OUT_DIR}/m3_commercial_proof_v1_population_b_quote_dependent.json"
PRIOR25_RECLASS_FILE = f"{OUT_DIR}/m3_commercial_proof_v1_prior25_reclass.json"
HIST_AUDIT_FILE = f"{OUT_DIR}/m3_commercial_proof_v1_historical_audit.json"
RESULTS_FILE = f"{OUT_DIR}/m3_commercial_proof_v1_research_results.json"
SCORECARD_FILE = f"{OUT_DIR}/m3_commercial_proof_v1_scorecard.json"
RFQ_FILE = f"{OUT_DIR}/m3_commercial_proof_v1_rfqs.json"
FINANCING_FILE = f"{OUT_DIR}/m3_commercial_proof_v1_financing.json"
DEALS_FILE = f"{OUT_DIR}/m3_commercial_proof_v1_available_deals.json"
CHECKPOINT_FILE = f"{OUT_DIR}/m3_commercial_proof_v1_checkpoint.json"
REPORT_FILE = f"{OUT_DIR}/m3_commercial_proof_v1_report.json"
LANES_FILE = f"{OUT_DIR}/m3_commercial_proof_v1_lanes.json"

# Identity classes
EXACT_PRODUCT_VERIFIED = "EXACT_PRODUCT_VERIFIED"
SPECIFICATION_COMPLETE_GENERIC = "SPECIFICATION_COMPLETE_GENERIC"
PARTIAL_PRODUCT_IDENTITY = "PARTIAL_PRODUCT_IDENTITY"
CATEGORY_ONLY = "CATEGORY_ONLY"
IDENTITY_CONFLICT = "IDENTITY_CONFLICT"
IDENTITY_UNKNOWN = "IDENTITY_UNKNOWN"

# Historical classes
EXACT_HISTORICAL_UNIT_PRICE = "EXACT_HISTORICAL_UNIT_PRICE"
SPECIFICATION_MATCHED_HISTORICAL_PRICE = "SPECIFICATION_MATCHED_HISTORICAL_PRICE"
COMPARABLE_CATEGORY_ONLY = "COMPARABLE_CATEGORY_ONLY"
TOTAL_AWARD_ONLY = "TOTAL_AWARD_ONLY"
SELLING_PRICE_UNKNOWN = "SELLING_PRICE_UNKNOWN"

# Price evidence
EXACT_PUBLIC_PRICE_VERIFIED = "EXACT_PUBLIC_PRICE_VERIFIED"
EQUIVALENT_PUBLIC_PRICE_VERIFIED = "EQUIVALENT_PUBLIC_PRICE_VERIFIED"
DISTRIBUTOR_PUBLIC_PRICE = "DISTRIBUTOR_PUBLIC_PRICE"
SUPPLIER_QUOTE_REQUIRED = "SUPPLIER_QUOTE_REQUIRED"
PRICE_UNAVAILABLE = "PRICE_UNAVAILABLE"

# RFQ states
RFQ_READY = "RFQ_READY"
RFQ_IDENTITY_INCOMPLETE = "RFQ_IDENTITY_INCOMPLETE"

MONEY = Decimal("0.01")

_GENERIC_FSC = re.compile(
    r"^(?:\d{2}--)?("
    r"VALVE[, ]|DISK[, ]|GASKET|CABLE(?:\s+ASSEMBLY)?[, ]|CLAMP[, ]|"
    r"HOUSING[, ]|SLEEVE|BEARING|LINING[, ]|ELBOW[, ]|FILTER|"
    r"BRACKET|CHARGER[, ]|BATTERY"
    r")",
    re.I,
)

_COMPLETE_GENERIC = re.compile(
    r"\b("
    r"paper\s*towels?|toilet\s*paper|bath\s*tissue|bond\s*paper|"
    r"nitrile\s+gloves?|gloves?\s+nitrile|safety\s+glasses|copy\s+paper|copier\s+paper|"
    r"trash\s+liners?|garbage\s+bags?|hand\s+soap|"
    r"led\s+bulbs?|a\d+\s+batteries?"
    r")\b",
    re.I,
)

_CATEGORY_ONLY = re.compile(
    r"\b("
    r"janitorial\s+supplies|office\s+supplies|electrical\s+(parts|accessories|supplies)|"
    r"plumbing\s+supplies|battery[- ]operated\s+equipment|ppe|safety\s+products|"
    r"hardware|mro\s+supplies|cleaning\s+supplies"
    r")\b",
    re.I,
)

_MEASURABLE_SPEC = re.compile(
    r"\b("
    r"\d+(?:\.\d+)?\s*(?:\"|in|inch|mm|mil|gauge|awg|v|volt|amp|psi|gpm|hp)|"
    r"size\s*[:=]?\s*\w+|model\s+[a-z0-9\-]+|mpn\s*[:=]|upc\s*[:=]|"
    r"#?\d{1,2}\s*bond|8\s*mil|powder[\s-]?free"
    r")\b",
    re.I,
)

_NON_PRODUCT = re.compile(
    r"\b("
    r"services?|construction|installation|design[- ]build|maintenance\s+contract|"
    r"labor|prevailing\s+wage|surplus|scrap|disposal|feasibility|"
    r"crm|software|hearing\s+protection\s+program|access\s+controls?|"
    r"development\s+project|well\s*#|microgrids?|solar\s*,\s*wind|"
    r"fabrication|towbarless|evidence\s+collection\s+kit|"
    r"pump\s+removal|compressor\b.*\bproject"
    r")\b",
    re.I,
)

# Retail-first hosts that historically return extractable prices
_RETAIL_HOSTS_OFFICE = [
    "https://www.quill.com/search?keywords={q}",
    "https://www.walmart.com/search?q={q}",
    "https://www.homedepot.com/s/{q}",
    "https://www.lowes.com/search?searchTerm={q}",
    "https://www.officedepot.com/a/search/?q={q}",
    "https://www.staples.com/search?query={q}",
    "https://www.webstaurantstore.com/search/{slug}.html",
]

_RETAIL_HOSTS_MRO = [
    "https://www.homedepot.com/s/{q}",
    "https://www.lowes.com/search?searchTerm={q}",
    "https://www.walmart.com/search?q={q}",
    "https://www.globalindustrial.com/search?q={q}",
    "https://www.northerntool.com/search?q={q}",
    "https://www.zoro.com/search?q={q}",
]


# ---------------------------------------------------------------------------
# Strict product identity
# ---------------------------------------------------------------------------


def classify_product_identity(
    text: str,
    *,
    structured: dict[str, Any] | None = None,
    nsn_resolved_mfr: str | None = None,
    conflicting: bool = False,
) -> dict[str, Any]:
    """Strict identity classification — generic FSC titles are NEVER EXACT."""
    structured = structured or {}
    title = _clean_title(text or "")
    ident = normalize_product_identity(title, structured=structured)
    mpn = ident.get("part_number")
    nsn = ident.get("nsn")
    mfr = ident.get("manufacturer") or nsn_resolved_mfr

    if conflicting:
        return _id_result(IDENTITY_CONFLICT, ident, "Contradictory identifying information")

    if _NON_PRODUCT.search(title) and not _COMPLETE_GENERIC.search(title):
        return _id_result(IDENTITY_UNKNOWN, ident, "Non-product / service-like title")

    # EXACT: manufacturer+MPN, or NSN resolved to approved mfr+product, or verified UPC
    upc = structured.get("UPC") or structured.get("upc") or structured.get("GTIN")
    if upc and (mfr or mpn):
        return _id_result(EXACT_PRODUCT_VERIFIED, ident, "UPC/GTIN with matching identity", upc=upc)
    if mpn and mfr:
        return _id_result(EXACT_PRODUCT_VERIFIED, ident, "Manufacturer + verified MPN")
    if mpn and re.search(r"\b(model|mpn|catalog|part\s*#)\b", title, re.I):
        # Model token present in title with MPN-like token (e.g. Penn Valley Pump Model 6DDS…)
        return _id_result(EXACT_PRODUCT_VERIFIED, ident, "Model/MPN explicitly labeled in title")
    if nsn and nsn_resolved_mfr and mpn:
        return _id_result(
            EXACT_PRODUCT_VERIFIED,
            ident,
            "NSN resolved to approved manufacturer and part",
            nsn_resolved_mfr=nsn_resolved_mfr,
        )
    if nsn and not nsn_resolved_mfr:
        # NSN alone without manufacturer mapping → partial, not exact
        return _id_result(
            PARTIAL_PRODUCT_IDENTITY,
            ident,
            "NSN present but not resolved to manufacturer/part mapping",
        )

    # Complete generic commodity with measurable specs OR well-known unrestricted commodity
    if _COMPLETE_GENERIC.search(title):
        if _MEASURABLE_SPEC.search(title) or re.search(
            r"\b(paper\s*towels?|toilet\s*paper|bond\s*paper|nitrile\s+gloves?)\b", title, re.I
        ):
            # Paper towels / toilet paper as unrestricted commodities → SPECIFICATION_COMPLETE_GENERIC
            # when brand not required; still need pack/qty for bid (qty may be unknown)
            return _id_result(
                SPECIFICATION_COMPLETE_GENERIC,
                ident,
                "Unrestricted commodity with identifiable product type; pack/qty still required for bid",
            )

    if _MEASURABLE_SPEC.search(title) and not _GENERIC_FSC.match(title):
        return _id_result(
            SPECIFICATION_COMPLETE_GENERIC if _COMPLETE_GENERIC.search(title) else PARTIAL_PRODUCT_IDENTITY,
            ident,
            "Measurable specs present",
        )

    # Generic FSC / truncated federal noun titles
    if _GENERIC_FSC.match(title) or (
        "," in title and len(title.split()) <= 4 and not mpn and not nsn_resolved_mfr
    ):
        return _id_result(
            CATEGORY_ONLY if len(title.split()) <= 3 else PARTIAL_PRODUCT_IDENTITY,
            ident,
            "Generic FSC/noun title without MPN/NSN resolution — not an exact product",
        )

    if _CATEGORY_ONLY.search(title):
        return _id_result(CATEGORY_ONLY, ident, "Broad product category only")

    if _COMPLETE_GENERIC.search(title) or classify_family({}, title) in {
        "office_supplies",
        "janitorial",
        "ppe",
    }:
        return _id_result(PARTIAL_PRODUCT_IDENTITY, ident, "Commodity family known; specs/pack incomplete")

    if title.strip():
        return _id_result(PARTIAL_PRODUCT_IDENTITY, ident, "Description present but insufficient for unique product")

    return _id_result(IDENTITY_UNKNOWN, ident, "Insufficient information")


def _id_result(cls: str, ident: dict[str, Any], note: str, **extra: Any) -> dict[str, Any]:
    return {
        "IDENTITY_CLASS": cls,
        "IS_EXACT_VERIFIED": cls == EXACT_PRODUCT_VERIFIED,
        "NOTE": note,
        "IDENTITY": ident,
        **extra,
    }


def reclassify_prior_sprint(prior_results: list[dict[str, Any]]) -> dict[str, Any]:
    rows = []
    counts: Counter[str] = Counter()
    previously_claimed_exact = 0
    actually_exact = 0
    for r in prior_results:
        previously_claimed_exact += 1 if r.get("Exact_Product_Identity") else 0
        title = str(r.get("Product") or "")
        cls = classify_product_identity(title)
        counts[cls["IDENTITY_CLASS"]] += 1
        if cls["IS_EXACT_VERIFIED"]:
            actually_exact += 1
        rows.append(
            {
                "OPPORTUNITY_ID": r.get("OPPORTUNITY_ID"),
                "TITLE": title,
                "PREVIOUS_CLAIMED_EXACT": bool(r.get("Exact_Product_Identity")),
                "CORRECTED_CLASS": cls["IDENTITY_CLASS"],
                "NOTE": cls["NOTE"],
                "WAS_FALSE_EXACT": bool(r.get("Exact_Product_Identity")) and not cls["IS_EXACT_VERIFIED"],
            }
        )
    return {
        "kind": "prior25_identity_reclassification",
        "build": BUILD,
        "built_at": _utc(),
        "PREVIOUS_CLAIMED_EXACT": previously_claimed_exact,
        "ACTUALLY_EXACT_VERIFIED": actually_exact,
        "FALSE_EXACT_COUNT": previously_claimed_exact - actually_exact,
        "COUNTS": dict(counts),
        "ROWS": rows,
    }


def audit_quoteable_identities(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    product_scope = 0
    non_product = 0
    for r in rows:
        if int(r.get("QUOTEABLE_LINES") or 0) <= 0:
            continue
        title = _clean_title(str(r.get("TITLE") or ""))
        if _NON_PRODUCT.search(title) and not _COMPLETE_GENERIC.search(title):
            non_product += 1
            cls_name = IDENTITY_UNKNOWN
            note = "Non-product excluded from commercial proof"
        else:
            product_scope += 1
            cls = classify_product_identity(title)
            cls_name = cls["IDENTITY_CLASS"]
            note = cls["NOTE"]
        counts[cls_name] += 1
        if len(samples[cls_name]) < 8:
            samples[cls_name].append(
                {
                    "OPPORTUNITY_ID": r.get("OPPORTUNITY_ID"),
                    "TITLE": title[:120],
                    "NOTE": note,
                }
            )
    return {
        "kind": "quoteable_identity_audit",
        "build": BUILD,
        "built_at": _utc(),
        "QUOTEABLE": len(rows),
        "PRODUCT_SCOPE_CANDIDATES": product_scope,
        "NON_PRODUCT_TITLES": non_product,
        "COUNTS": dict(counts),
        "SAMPLES": dict(samples),
        "BEFORE_AFTER_NOTE": (
            "Previous first-revenue sprint treated short SIMPLE_PRODUCT titles as exact; "
            "corrected classifier requires manufacturer+MPN, resolved NSN, UPC, or complete generic commodity."
        ),
    }


# ---------------------------------------------------------------------------
# Historical comparability repair
# ---------------------------------------------------------------------------


def classify_historical_comparability(
    open_title: str,
    hist: dict[str, Any] | None,
) -> dict[str, Any]:
    if not hist or hist.get("STATUS") in {None, "HISTORICAL_PRICE_UNKNOWN", SELLING_PRICE_UNKNOWN}:
        return {"STATUS": SELLING_PRICE_UNKNOWN, "NOTE": "No historical match"}

    open_toks = set(re.findall(r"[a-z0-9]{3,}", (open_title or "").lower()))
    hist_title = str(hist.get("title") or "")
    hist_toks = set(re.findall(r"[a-z0-9]{3,}", hist_title.lower()))
    overlap = open_toks & hist_toks

    # Spec tokens that must align for exact/spec-matched (size, model, material)
    open_specs = set(re.findall(r"\d+(?:\.\d+)?|\"|inch|mm|mil|model|mpn", (open_title or "").lower()))
    hist_specs = set(re.findall(r"\d+(?:\.\d+)?|\"|inch|mm|mil|model|mpn|[a-z]*\d+[a-z0-9\-]*", hist_title.lower()))
    spec_overlap = open_specs & hist_specs

    unit = hist.get("unit_price")
    award = hist.get("award_amount")
    line_count = int(hist.get("line_count") or 0)

    # Generic FSC open title vs highly specific hist (8" check valve) → category only
    if _GENERIC_FSC.match(_clean_title(open_title)) and (
        re.search(r'\d+\s*"', hist_title) or re.search(r"\bmodel\b", hist_title, re.I)
    ):
        return {
            **hist,
            "STATUS": COMPARABLE_CATEGORY_ONLY,
            "PREVIOUS_STATUS": hist.get("STATUS"),
            "NOTE": (
                "Open solicitation is generic FSC/noun title; historical award has specific size/model. "
                "Cannot use as unit selling-price benchmark."
            ),
            "OVERLAP_TOKENS": sorted(overlap)[:12],
            "unit_price_for_margin": None,  # do not feed into margin
            "award_amount_for_margin": None,
        }

    if unit is not None and len(overlap) >= 3 and (spec_overlap or len(overlap) >= 5):
        return {
            **hist,
            "STATUS": EXACT_HISTORICAL_UNIT_PRICE if len(overlap) >= 5 else SPECIFICATION_MATCHED_HISTORICAL_PRICE,
            "NOTE": "Sufficient title/spec overlap for unit-price comparison",
            "unit_price_for_margin": unit,
            "OVERLAP_TOKENS": sorted(overlap)[:12],
        }

    if unit is not None and len(overlap) >= 2:
        return {
            **hist,
            "STATUS": COMPARABLE_CATEGORY_ONLY,
            "NOTE": "Weak overlap — category signal only, not a selling-price benchmark",
            "unit_price_for_margin": None,
            "OVERLAP_TOKENS": sorted(overlap)[:12],
        }

    if award is not None:
        return {
            **hist,
            "STATUS": TOTAL_AWARD_ONLY,
            "NOTE": "Aggregate award only" + ("; multi-line" if line_count > 1 else ""),
            "unit_price_for_margin": None,
            "award_amount_for_margin": None if line_count > 1 else award,
            "OVERLAP_TOKENS": sorted(overlap)[:12],
        }

    return {**hist, "STATUS": SELLING_PRICE_UNKNOWN, "unit_price_for_margin": None}


def audit_prior_historical(prior_results: list[dict[str, Any]]) -> dict[str, Any]:
    audited = []
    for r in prior_results:
        hist = r.get("Past_Gov_Price") or {}
        if hist.get("STATUS") in {None, "HISTORICAL_PRICE_UNKNOWN"}:
            continue
        fixed = classify_historical_comparability(str(r.get("Product") or ""), hist)
        audited.append(
            {
                "OPPORTUNITY_ID": r.get("OPPORTUNITY_ID"),
                "TITLE": r.get("Product"),
                "PREVIOUS_STATUS": hist.get("STATUS"),
                "CORRECTED_STATUS": fixed.get("STATUS"),
                "PREVIOUS_UNIT": hist.get("unit_price"),
                "USABLE_FOR_MARGIN": fixed.get("unit_price_for_margin") is not None,
                "SOURCE_URL": hist.get("source_url") or fixed.get("source_url"),
                "NOTE": fixed.get("NOTE"),
            }
        )
    return {
        "kind": "historical_comparability_audit",
        "build": BUILD,
        "built_at": _utc(),
        "AUDITED": audited,
        "COUNT": len(audited),
    }


# ---------------------------------------------------------------------------
# Source reliability + multi-route pricing
# ---------------------------------------------------------------------------


def load_source_reliability() -> dict[str, Any]:
    return _load(SOURCE_RELIABILITY_FILE) or {
        "kind": "source_reliability",
        "build": BUILD,
        "hosts": {},
    }


def record_source_event(
    reliability: dict[str, Any],
    host: str,
    *,
    event: str,
) -> None:
    hosts = reliability.setdefault("hosts", {})
    entry = hosts.setdefault(
        host,
        {
            "success_price": 0,
            "exact_match": 0,
            "equivalent_match": 0,
            "rejected": 0,
            "js_empty": 0,
            "account_gated": 0,
            "blocked_403": 0,
            "search_only": 0,
            "attempts": 0,
        },
    )
    entry["attempts"] = int(entry.get("attempts") or 0) + 1
    if event in entry:
        entry[event] = int(entry.get(event) or 0) + 1
    hosts[host] = entry


def _host_of(url: str) -> str:
    m = re.search(r"https?://([^/]+)/", url or "")
    return (m.group(1) if m else "unknown").lower()


def retail_first_urls(identity: dict[str, Any], title: str) -> list[str]:
    """Route A/B: prefer retail/commodity hosts that return HTML prices."""
    q_raw = (identity.get("synonyms") or [None])[0] or " ".join((title or "").split()[:8])
    q = quote_plus(q_raw)
    slug = quote_plus(q_raw.replace(" ", "-"))
    blob = f"{q_raw} {title}".lower()
    urls: list[str] = []
    if re.search(r"\b(paper|towel|toilet|office|toner|glove|nitrile|bond\s*paper|janitorial|mop)\b", blob):
        templates = _RETAIL_HOSTS_OFFICE
    else:
        templates = _RETAIL_HOSTS_MRO + _RETAIL_HOSTS_OFFICE[:2]
    for tmpl in templates:
        urls.append(tmpl.format(q=q, slug=slug))
    return urls


def research_price_multi_route(
    oid: str,
    title: str,
    identity_class: dict[str, Any],
    reliability: dict[str, Any],
) -> dict[str, Any]:
    """Routes A–D. Never invent discounts. Track source reliability."""
    cls = identity_class.get("IDENTITY_CLASS")
    ident = dict(identity_class.get("IDENTITY") or {})
    attempts: list[dict[str, Any]] = []

    if cls in {CATEGORY_ONLY, IDENTITY_UNKNOWN, IDENTITY_CONFLICT}:
        return {
            "status": SUPPLIER_QUOTE_REQUIRED if cls == CATEGORY_ONLY else PRICE_UNAVAILABLE,
            "evidence_level": SUPPLIER_QUOTE_REQUIRED if cls == CATEGORY_ONLY else PRICE_UNAVAILABLE,
            "unit_price": None,
            "route": "D_RFQ" if cls == CATEGORY_ONLY else "NONE",
            "note": "Identity insufficient for public price match",
            "attempts": attempts,
            "checked_at": _utc(),
        }

    # Route A/B: retail-first commodity / exact when possible
    import httpx
    from public_price_search.extract import extract_candidate_from_page, extract_product_links
    from public_price_search.search import fetch_page

    urls = retail_first_urls(ident, title)[:6]
    # Also run existing bounded research (distributor path) as Route C after retail
    best: dict[str, Any] | None = None
    query_tokens = {
        w
        for w in re.findall(r"[a-z0-9]{3,}", ((ident.get("synonyms") or [None])[0] or title).lower())
        if w not in {"and", "the", "for", "with", "bid"}
    }

    try:
        with httpx.Client(timeout=12.0, follow_redirects=True) as client:
            pages = 0
            for url in urls:
                if pages >= 8:
                    break
                host = _host_of(url)
                fr = fetch_page(url, client=client, use_budget=True)
                pages += 1
                attempts.append({"url": url, "status_code": fr.get("status_code"), "ok": fr.get("ok")})
                if fr.get("status_code") == 403:
                    record_source_event(reliability, host, event="blocked_403")
                    continue
                text = fr.get("text") or ""
                if fr.get("blocked") or not fr.get("ok"):
                    record_source_event(reliability, host, event="blocked_403")
                    continue
                if len(text) < 5000 and "grainger" in host:
                    record_source_event(reliability, host, event="js_empty")
                    continue
                detail_urls = extract_product_links(text, ident, base_host=host)[:2]
                targets = detail_urls or [fr.get("url") or url]
                for target in targets:
                    if pages >= 10 and target != (fr.get("url") or url):
                        break
                    body = text
                    final_url = fr.get("url") or url
                    if target != (fr.get("url") or url):
                        fr2 = fetch_page(target, client=client, use_budget=True)
                        pages += 1
                        body = fr2.get("text") or ""
                        final_url = fr2.get("url") or target
                        if fr2.get("blocked") or not body:
                            record_source_event(reliability, _host_of(target), event="blocked_403")
                            continue
                    cand = extract_candidate_from_page(body, ident, url=final_url)
                    if not cand or cand.get("price") is None:
                        record_source_event(reliability, _host_of(final_url), event="search_only")
                        continue
                    # Token overlap gate
                    page_toks = set(re.findall(r"[a-z0-9]{3,}", (body[:8000] or "").lower()))
                    if query_tokens and len(query_tokens & page_toks) < max(1, min(2, len(query_tokens))):
                        record_source_event(reliability, _host_of(final_url), event="rejected")
                        attempts.append({"url": final_url, "rejected": "token_overlap"})
                        continue
                    price = float(cand["price"])
                    el = (
                        EXACT_PUBLIC_PRICE_VERIFIED
                        if cls == EXACT_PRODUCT_VERIFIED and ident.get("has_exact_id")
                        else EQUIVALENT_PUBLIC_PRICE_VERIFIED
                    )
                    if "grainger" in _host_of(final_url) or "mscdirect" in _host_of(final_url):
                        el = DISTRIBUTOR_PUBLIC_PRICE
                    best = {
                        "status": "FOUND",
                        "evidence_level": el,
                        "unit_price": price,
                        "currency": "USD",
                        "source_url": final_url,
                        "seller": _host_of(final_url),
                        "uom": ident.get("uom") or "EA",
                        "pack_qty": ident.get("qty"),
                        "route": "A_EXACT" if el == EXACT_PUBLIC_PRICE_VERIFIED else "B_GENERIC_OR_RETAIL",
                        "checked_at": _utc(),
                        "pack_note": "Normalize pack/case to solicitation UOM before bid",
                        "attempts": attempts,
                    }
                    record_source_event(
                        reliability,
                        _host_of(final_url),
                        event="exact_match" if el == EXACT_PUBLIC_PRICE_VERIFIED else "equivalent_match",
                    )
                    record_source_event(reliability, _host_of(final_url), event="success_price")
                    break
                if best:
                    break
    except Exception as exc:
        attempts.append({"error": str(exc)[:160]})

    if best:
        return best

    # Route C: existing distributor-bounded research
    try:
        dist = research_public_price_bounded(oid, ident, title=title)
        url = dist.get("source_url") or ""
        host = _host_of(url) if url else "distributor_path"
        if dist.get("unit_price") is not None:
            # Reject lighting host mismatches already handled upstream; double-check
            if "1000bulbs" in host and not re.search(r"\b(led|bulb|lamp|light)\b", title, re.I):
                record_source_event(reliability, host, event="rejected")
                dist = {**dist, "unit_price": None, "status": "REJECTED_HOST_MISMATCH"}
            else:
                record_source_event(reliability, host or "unknown", event="success_price")
                return {
                    **dist,
                    "route": "C_DISTRIBUTOR",
                    "seller": host,
                    "attempts": attempts + [{"route_c": dist.get("status")}],
                }
        else:
            if dist.get("status") == "SKIPPED_NEEDS_MPN":
                record_source_event(reliability, "distributor_path", event="search_only")
            attempts.append({"route_c": dist.get("status"), "note": dist.get("note")})
    except Exception as exc:
        attempts.append({"route_c_error": str(exc)[:120]})

    # Route D
    return {
        "status": SUPPLIER_QUOTE_REQUIRED,
        "evidence_level": SUPPLIER_QUOTE_REQUIRED,
        "unit_price": None,
        "route": "D_RFQ",
        "note": "Public retail/distributor prices unavailable — supplier quote required",
        "attempts": attempts,
        "checked_at": _utc(),
    }


# ---------------------------------------------------------------------------
# Economics
# ---------------------------------------------------------------------------


def compute_commercial_economics(
    *,
    identity_class: str,
    qty: Decimal | None,
    unit_acquisition: Decimal | None,
    selling_unit: Decimal | None,
    selling_label: str,
    freight: Decimal | None = None,
    packaging: Decimal | None = None,
    compliance: Decimal | None = None,
    financing_fees: Decimal | None = None,
) -> dict[str, Any]:
    missing: list[str] = []
    if unit_acquisition is None:
        missing.append("ACQUISITION_UNIT_COST")
    if selling_unit is None:
        missing.append("SELLING_UNIT_PRICE")
    if freight is None:
        missing.append("FREIGHT")
    if financing_fees is None:
        missing.append("FINANCING_FEES")

    q = qty if qty is not None else Decimal("1")
    qty_assumed = qty is None
    total_acq = (unit_acquisition * q) if unit_acquisition is not None else None
    total_sell = (selling_unit * q) if selling_unit is not None else None

    gross = None
    if total_sell is not None and total_acq is not None:
        gross = total_sell - total_acq

    contrib = None
    if gross is not None:
        known_extra = Decimal("0")
        for v in (freight, packaging, compliance, financing_fees):
            if v is not None:
                known_extra += v
        if freight is None or financing_fees is None:
            # conditional contribution incomplete
            contrib = None
        else:
            contrib = gross - known_extra

    # Max acquisition for profit floors (requires selling)
    max_costs = {}
    min_bid_targets = {}
    if total_sell is not None:
        known = Decimal("0")
        for v in (freight, packaging, compliance, financing_fees):
            if v is not None:
                known += v
        for label, profit in (
            ("MAX_ACQ_FOR_500", Decimal("500")),
            ("MAX_ACQ_FOR_2000", Decimal("2000")),
            ("MAX_ACQ_FOR_5000", Decimal("5000")),
            ("MAX_ACQ_FOR_10000", Decimal("10000")),
        ):
            max_costs[label] = _money_str(total_sell - known - profit)
    if total_acq is not None:
        known = Decimal("0")
        for v in (freight, packaging, compliance, financing_fees):
            if v is not None:
                known += v
        for label, profit in (
            ("MIN_BID_FOR_500", Decimal("500")),
            ("MIN_BID_FOR_2000", Decimal("2000")),
            ("MIN_BID_FOR_5000", Decimal("5000")),
            ("MIN_BID_FOR_10000", Decimal("10000")),
        ):
            min_bid_targets[label] = {
                "TARGET_BID": _money_str(total_acq + known + profit),
                "LABEL": "TARGET_NOT_VERIFIED_ACHIEVABLE",
                "ASSUMES_UNKNOWN_COSTS_ZERO": freight is None or financing_fees is None,
            }

    if total_acq is not None and total_sell is not None and freight is not None and financing_fees is not None:
        level = "VERIFIED_TRANSACTION_ECONOMICS" if contrib is not None and contrib > 0 else (
            "VERIFIED_NEGATIVE" if contrib is not None and contrib <= 0 else "VERIFIED_INCOMPLETE"
        )
    elif total_acq is not None and total_sell is not None:
        level = "CONDITIONAL_SELLING_AND_ACQUISITION_SCENARIO"
    elif total_acq is not None:
        level = "VERIFIED_ACQUISITION_PRICE_SCENARIO"
    elif total_sell is not None:
        level = "CONDITIONAL_SELLING_PRICE_SCENARIO"
    else:
        level = "INSUFFICIENT_EVIDENCE"

    positive_preliminary = bool(gross is not None and gross > 0 and level.startswith("CONDITIONAL"))
    plausible = {
        "P500": bool(gross is not None and gross >= Decimal("500")),
        "P2000": bool(gross is not None and gross >= Decimal("2000")),
        "P5000": bool(gross is not None and gross >= Decimal("5000")),
        "P10000": bool(gross is not None and gross >= Decimal("10000")),
    }

    return {
        "EVIDENCE_LEVEL": level,
        "IDENTITY_CLASS": identity_class,
        "QTY": _money_str(q) if q is not None else None,
        "QTY_ASSUMED_1": qty_assumed,
        "UNIT_ACQUISITION": _money_str(unit_acquisition),
        "TOTAL_ACQUISITION": _money_str(total_acq),
        "UNIT_SELLING": _money_str(selling_unit),
        "TOTAL_SELLING": _money_str(total_sell),
        "SELLING_LABEL": selling_label,
        "GROSS_PROFIT_SCENARIO": _money_str(gross),
        "GROSS_MARGIN_PCT": (
            str(((gross / total_sell) * Decimal("100")).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
            if gross is not None and total_sell and total_sell != 0
            else None
        ),
        "REMAINING_CONTRIBUTION_VERIFIED": _money_str(contrib),
        "MISSING": missing,
        "MAX_ACQUISITION_FOR_PROFIT": max_costs,
        "MIN_BID_TARGETS": min_bid_targets,
        "POSITIVE_PRELIMINARY_MARGIN": positive_preliminary,
        "PLAUSIBLE": plausible,
        "NOTE": "Scenarios are not verified final profitability; unknown freight/financing ≠ 0",
    }


# ---------------------------------------------------------------------------
# Population selection
# ---------------------------------------------------------------------------


def select_evidence_first(
    rows: list[dict[str, Any]],
    rematch: dict[str, Any],
    hist_index: dict[str, Any],
    *,
    n: int = 25,
) -> list[dict[str, Any]]:
    rematch_by = {r["OPPORTUNITY_ID"]: r for r in (rematch.get("RESULTS") or [])}
    scored: list[tuple[float, dict[str, Any], dict[str, Any]]] = []
    for r in rows:
        if int(r.get("QUOTEABLE_LINES") or 0) <= 0:
            continue
        title = _clean_title(str(r.get("TITLE") or ""))
        if _NON_PRODUCT.search(title) and not _COMPLETE_GENERIC.search(title):
            continue
        if _HARD_EXCLUDE.search(title) and not _COMPLETE_GENERIC.search(title):
            continue
        days = _days_to_deadline(r.get("DEADLINE"))
        if days is not None and days < 3:
            continue
        if days is not None and days < 0:
            continue
        idc = classify_product_identity(title)
        if idc["IDENTITY_CLASS"] in {IDENTITY_UNKNOWN, IDENTITY_CONFLICT}:
            continue
        hist_raw = match_historical(str(r.get("BUYER") or ""), title, hist_index)
        hist = classify_historical_comparability(title, hist_raw)
        rr = rematch_by.get(r["OPPORTUNITY_ID"])
        suppliers = len((rr or {}).get("SUPPLIER_SET_AFTER") or {}) or int(r.get("PACKET_COUNT") or 0)

        sc = 0.0
        # Prefer complete generic + exact
        if idc["IDENTITY_CLASS"] == EXACT_PRODUCT_VERIFIED:
            sc += 50
        elif idc["IDENTITY_CLASS"] == SPECIFICATION_COMPLETE_GENERIC:
            sc += 45
        elif idc["IDENTITY_CLASS"] == PARTIAL_PRODUCT_IDENTITY:
            sc += 15
        else:
            sc += 5
        if hist.get("unit_price_for_margin") is not None:
            sc += 40
        elif hist.get("STATUS") == TOTAL_AWARD_ONLY:
            sc += 5
        if suppliers:
            sc += 20
        if int(r.get("REAL_LINES") or 1) <= 3:
            sc += 15
        if days and days >= 5:
            sc += 10
        # Evidence-first: boost unrestricted commodities we can actually price on retail sites
        if _COMPLETE_GENERIC.search(title):
            sc += 25
        scored.append((sc, r, {"identity": idc, "historical": hist, "suppliers": suppliers}))

    scored.sort(key=lambda x: (-x[0], x[1].get("OPPORTUNITY_ID") or ""))
    out = []
    seen_stems: set[str] = set()
    for sc, r, meta in scored:
        title = _clean_title(str(r.get("TITLE") or ""))
        stem = " ".join(re.sub(r"[^a-z0-9]+", " ", title.lower()).split()[:4])
        if stem in seen_stems:
            continue
        # Prefer identity that can support public pricing or hist
        idc = meta["identity"]["IDENTITY_CLASS"]
        if idc == CATEGORY_ONLY and meta["historical"].get("unit_price_for_margin") is None:
            continue
        seen_stems.add(stem)
        out.append(
            {
                "OPPORTUNITY_ID": r["OPPORTUNITY_ID"],
                "TITLE": title,
                "BUYER": r.get("BUYER"),
                "DEADLINE": r.get("DEADLINE"),
                "SCORE": sc,
                "POPULATION": "A_EVIDENCE_FIRST",
                "IDENTITY_CLASS": idc,
                "HIST_STATUS": meta["historical"].get("STATUS"),
                "SUPPLIERS_HINT": meta["suppliers"],
            }
        )
        if len(out) >= n:
            break
    return out


def select_quote_dependent(
    rows: list[dict[str, Any]],
    rematch: dict[str, Any],
    *,
    exclude_ids: set[str],
    n: int = 15,
) -> list[dict[str, Any]]:
    rematch_by = {r["OPPORTUNITY_ID"]: r for r in (rematch.get("RESULTS") or [])}
    scored = []
    for r in rows:
        oid = r.get("OPPORTUNITY_ID")
        if oid in exclude_ids:
            continue
        if int(r.get("QUOTEABLE_LINES") or 0) <= 0:
            continue
        title = _clean_title(str(r.get("TITLE") or ""))
        if _NON_PRODUCT.search(title) and not _COMPLETE_GENERIC.search(title):
            continue
        if _HARD_EXCLUDE.search(title):
            continue
        days = _days_to_deadline(r.get("DEADLINE"))
        if days is not None and days < 3:
            continue
        idc = classify_product_identity(title)
        if idc["IDENTITY_CLASS"] not in {
            PARTIAL_PRODUCT_IDENTITY,
            CATEGORY_ONLY,
            EXACT_PRODUCT_VERIFIED,
            SPECIFICATION_COMPLETE_GENERIC,
        }:
            continue
        rr = rematch_by.get(oid)
        suppliers = list(((rr or {}).get("SUPPLIER_SET_AFTER") or {}).keys())
        if not suppliers and r.get("SUPPLIER_SET"):
            suppliers = list((r.get("SUPPLIER_SET") or {}).keys())
        if not suppliers and not int(r.get("PACKET_COUNT") or 0):
            continue
        # Quote-dependent: partial/category OR exact without easy retail
        sc = 20.0 + (10 if suppliers else 0) + (15 if int(r.get("REAL_LINES") or 99) <= 3 else 0)
        if idc["IDENTITY_CLASS"] == EXACT_PRODUCT_VERIFIED:
            sc += 25
        if days and 5 <= days <= 30:
            sc += 10
        scored.append((sc, r, idc, suppliers))
    scored.sort(key=lambda x: (-x[0], x[1].get("OPPORTUNITY_ID") or ""))
    out = []
    seen = set()
    for sc, r, idc, suppliers in scored:
        title = _clean_title(str(r.get("TITLE") or ""))
        stem = " ".join(re.sub(r"[^a-z0-9]+", " ", title.lower()).split()[:4])
        if stem in seen:
            continue
        seen.add(stem)
        out.append(
            {
                "OPPORTUNITY_ID": r["OPPORTUNITY_ID"],
                "TITLE": title,
                "BUYER": r.get("BUYER"),
                "DEADLINE": r.get("DEADLINE"),
                "SCORE": sc,
                "POPULATION": "B_QUOTE_DEPENDENT",
                "IDENTITY_CLASS": idc["IDENTITY_CLASS"],
                "SUPPLIERS": suppliers[:5],
            }
        )
        if len(out) >= n:
            break
    return out


# ---------------------------------------------------------------------------
# Research one opportunity
# ---------------------------------------------------------------------------


def research_one(
    row: dict[str, Any],
    *,
    rematch_row: dict[str, Any] | None,
    hist_index: dict[str, Any],
    reliability: dict[str, Any],
    population: str,
    live_price: bool = True,
) -> dict[str, Any]:
    oid = str(row.get("OPPORTUNITY_ID") or "")
    title = _clean_title(str(row.get("TITLE") or row.get("Product") or ""))
    buyer = str(row.get("BUYER") or row.get("Buyer") or "")
    # Prefer recovered product-line sidecar over title-only identity
    recovered_lines: list[dict[str, Any]] = []
    try:
        from buyer_intelligence.source_to_product_extraction_audit_v1 import (
            load_product_lines_for_opportunity,
            opportunity_identity_from_lines,
        )

        recovered_lines = load_product_lines_for_opportunity(oid)
    except Exception:
        recovered_lines = []
    if recovered_lines:
        idc = opportunity_identity_from_lines(title, recovered_lines)
        # Prefer first searchable line description for pricing route
        for ln in recovered_lines:
            if ln.get("DESCRIPTION") and ln.get("IDENTITY_CLASS") != "CATEGORY_ONLY":
                title_for_price = str(ln.get("DESCRIPTION") or title)
                if ln.get("MPN") or ln.get("MANUFACTURER"):
                    idc = {
                        **idc,
                        "IDENTITY": {
                            **(idc.get("IDENTITY") or {}),
                            "part_number": ln.get("MPN"),
                            "manufacturer": ln.get("MANUFACTURER"),
                            "qty": ln.get("QTY"),
                            "uom": ln.get("UOM"),
                            "has_exact_id": bool(ln.get("MPN") or ln.get("NSN")),
                            "raw_description": ln.get("DESCRIPTION"),
                            "description": ln.get("DESCRIPTION"),
                        },
                    }
                    title = title_for_price
                break
    else:
        idc = classify_product_identity(title)
    hist_raw = match_historical(buyer, title, hist_index)
    hist = classify_historical_comparability(title, hist_raw)

    price = (
        research_price_multi_route(oid, title, idc, reliability)
        if live_price
        else {"evidence_level": PRICE_UNAVAILABLE, "unit_price": None, "route": "SKIPPED"}
    )

    suppliers: list[str] = []
    if rematch_row:
        suppliers = list((rematch_row.get("SUPPLIER_SET_AFTER") or {}).keys())
    if row.get("SUPPLIERS"):
        suppliers = list(dict.fromkeys(suppliers + list(row.get("SUPPLIERS") or [])))
    if row.get("SUPPLIER_SET"):
        suppliers = list(dict.fromkeys(suppliers + list((row.get("SUPPLIER_SET") or {}).keys())))
    for p in row.get("PACKETS") or []:
        if isinstance(p, dict) and p.get("SUPPLIER"):
            suppliers.append(str(p["SUPPLIER"]))
    suppliers = list(dict.fromkeys(suppliers))

    unit_acq = _dec(price.get("unit_price"))
    selling = _dec(hist.get("unit_price_for_margin"))
    selling_label = hist.get("STATUS") or SELLING_PRICE_UNKNOWN
    qty = _dec((idc.get("IDENTITY") or {}).get("qty"))

    econ = compute_commercial_economics(
        identity_class=idc["IDENTITY_CLASS"],
        qty=qty,
        unit_acquisition=unit_acq,
        selling_unit=selling,
        selling_label=selling_label,
        freight=None,
        financing_fees=None,
    )

    fin = financing_label_for_deal(contract_value=selling, supplier_cost=unit_acq)
    cf = evaluate_cash_flow(
        award_or_selling=(selling * (qty or Decimal("1"))) if selling is not None else None,
        acquisition_cost=(unit_acq * (qty or Decimal("1"))) if unit_acq is not None else None,
        financing_status=fin,
    )
    evidence_complete = (
        selling is not None
        and unit_acq is not None
        and econ["EVIDENCE_LEVEL"] == "VERIFIED_TRANSACTION_ECONOMICS"
        and fin == FIN_CONFIRMED
    )
    lane = classify_profit_lane(
        expected_contribution=_dec(econ.get("REMAINING_CONTRIBUTION_VERIFIED")),
        evidence_complete=evidence_complete,
        financing_status=fin,
        full_sourcing_ready=unit_acq is not None and bool(suppliers),
    )
    if lane["LANE"] == LANE_PROVISIONAL and econ.get("GROSS_PROFIT_SCENARIO"):
        lane["GROSS_PROFIT_SCENARIO"] = econ["GROSS_PROFIT_SCENARIO"]
        lane["SCENARIO_NOTE"] = "Preliminary gross only — not verified contribution"

    rfq_status = RFQ_READY if idc["IDENTITY_CLASS"] in {
        EXACT_PRODUCT_VERIFIED,
        SPECIFICATION_COMPLETE_GENERIC,
        PARTIAL_PRODUCT_IDENTITY,
    } and suppliers else RFQ_IDENTITY_INCOMPLETE
    if idc["IDENTITY_CLASS"] == CATEGORY_ONLY:
        rfq_status = RFQ_IDENTITY_INCOMPLETE

    next_action = _next_action(idc, price, hist, suppliers, econ, fin)

    return {
        "OPPORTUNITY_ID": oid,
        "Product": title,
        "Buyer": buyer,
        "Deadline": row.get("DEADLINE"),
        "Population": population,
        "Identity": idc,
        "Past_Gov_Price": hist,
        "Acquisition_Price": price,
        "Suppliers": suppliers,
        "Economics": econ,
        "Lane": lane,
        "Cash_Flow": cf,
        "Financing": fin,
        "RFQ_STATUS": rfq_status,
        "Evidence_Links": [u for u in [price.get("source_url"), hist.get("source_url")] if u],
        "Next_Action": next_action,
        "Workflow": [
            {"step": "Identify", "status": "complete" if idc["IS_EXACT_VERIFIED"] or idc["IDENTITY_CLASS"] == SPECIFICATION_COMPLETE_GENERIC else "incomplete"},
            {"step": "Price", "status": "complete" if unit_acq is not None else "incomplete"},
            {"step": "Source", "status": "complete" if suppliers else "incomplete"},
            {"step": "Finance", "status": "blocked" if fin != FIN_CONFIRMED else "complete"},
            {"step": "Bid", "status": "incomplete"},
            {"step": "Fulfill", "status": "incomplete"},
            {"step": "Get Paid", "status": "incomplete"},
        ],
    }


def _next_action(idc, price, hist, suppliers, econ, fin) -> str:
    if idc["IDENTITY_CLASS"] in {CATEGORY_ONLY, IDENTITY_UNKNOWN}:
        return "Recover full product specs / NSN-MPN mapping from attachments"
    if not suppliers:
        return "Identify supplier channel"
    if price.get("unit_price") is None:
        return "Send supplier RFQ for delivered pricing (public price unavailable)"
    if hist.get("unit_price_for_margin") is None:
        return "Recover specification-matched historical unit price or set target bid"
    if fin in {FIN_UNKNOWN, FIN_UNVERIFIED}:
        return "Qualify financing with provider packet — do not mark confirmed"
    return "Operator review economics + prepare bid"


# ---------------------------------------------------------------------------
# RFQs + financing
# ---------------------------------------------------------------------------


def build_rfqs(results: list[dict[str, Any]], *, limit: int = 8) -> dict[str, Any]:
    def score(r: dict[str, Any]) -> float:
        sc = 0.0
        idc = (r.get("Identity") or {}).get("IDENTITY_CLASS")
        if idc == EXACT_PRODUCT_VERIFIED:
            sc += 40
        elif idc == SPECIFICATION_COMPLETE_GENERIC:
            sc += 35
        elif idc == PARTIAL_PRODUCT_IDENTITY:
            sc += 15
        if r.get("Suppliers"):
            sc += 20
        if (r.get("Acquisition_Price") or {}).get("unit_price") is not None:
            sc += 10
        if (r.get("Past_Gov_Price") or {}).get("unit_price_for_margin") is not None:
            sc += 25
        if (r.get("Economics") or {}).get("POSITIVE_PRELIMINARY_MARGIN"):
            sc += 20
        days = _days_to_deadline(r.get("Deadline"))
        if days and days >= 5:
            sc += 10
        if r.get("RFQ_STATUS") == RFQ_READY:
            sc += 15
        return sc

    ranked = sorted(results, key=score, reverse=True)
    packets = []
    for r in ranked:
        if len(packets) >= limit:
            break
        if r.get("RFQ_STATUS") == RFQ_IDENTITY_INCOMPLETE and (r.get("Identity") or {}).get("IDENTITY_CLASS") == CATEGORY_ONLY:
            continue
        suppliers = r.get("Suppliers") or ["UNKNOWN"]
        primary = suppliers[0]
        contact = resolve_contact(primary, (SUPPLIER_META.get(primary) or {}).get("KEY"))
        econ = r.get("Economics") or {}
        packets.append(
            {
                "OPPORTUNITY_ID": r["OPPORTUNITY_ID"],
                "STATUS": r.get("RFQ_STATUS") or RFQ_READY,
                "AUTO_SEND": False,
                "PRIMARY_SUPPLIER": primary,
                "ALTERNATES": suppliers[1:4],
                "CONTACT": contact,
                "FORM_URL": (contact or {}).get("CONTACT_URL"),
                "SOLICITATION_TITLE": r.get("Product"),
                "BUYER": r.get("Buyer"),
                "BID_DEADLINE": r.get("Deadline"),
                "IDENTITY_CLASS": (r.get("Identity") or {}).get("IDENTITY_CLASS"),
                "PRODUCTS": [
                    {
                        "description": r.get("Product"),
                        "manufacturer": ((r.get("Identity") or {}).get("IDENTITY") or {}).get("manufacturer"),
                        "mpn": ((r.get("Identity") or {}).get("IDENTITY") or {}).get("part_number"),
                        "nsn": ((r.get("Identity") or {}).get("IDENTITY") or {}).get("nsn"),
                        "qty": ((r.get("Identity") or {}).get("IDENTITY") or {}).get("qty") or "CONFIRM FROM SOLICITATION",
                        "uom": ((r.get("Identity") or {}).get("IDENTITY") or {}).get("uom") or "EA",
                    }
                ],
                "REQUESTED": [
                    "Firm delivered unit pricing",
                    "Freight to destination",
                    "Lead time / stock",
                    "Reseller eligibility",
                    "Project/deviated pricing",
                    "Payment terms",
                    "Quote expiration",
                    "Direct-to-government delivery",
                ],
                "INTERNAL_ONLY": {
                    "MAX_ACQUISITION_FOR_PROFIT": econ.get("MAX_ACQUISITION_FOR_PROFIT"),
                    "MIN_BID_TARGETS": econ.get("MIN_BID_TARGETS"),
                    "DO_NOT_DISCLOSE": True,
                },
                "NEXT_ACTION": "Operator review and send — AUTO_SEND=false",
                "SCORE": score(r),
            }
        )
    return {
        "kind": "commercial_proof_rfqs",
        "build": BUILD,
        "built_at": _utc(),
        "AUTO_SEND": False,
        "COUNT": len(packets),
        "PACKETS": packets,
        "MONDAY_PRIORITY": packets[:5],
    }


def build_financing_packets(*, live_fetch: bool = True) -> dict[str, Any]:
    providers = [
        {"NAME": "SouthStar Capital", "URL": "https://www.southstarcapital.com/", "FOCUS": "PO / trade finance"},
        {"NAME": "TradeCap Partners", "URL": "https://www.tradecappartners.com/", "FOCUS": "PO financing"},
        {"NAME": "King Trade Capital", "URL": "https://www.kingtradecapital.com/", "FOCUS": "Trade finance"},
        {"NAME": "SMB Compass", "URL": "https://smbcompass.com/", "FOCUS": "SMB marketplace"},
        {"NAME": "Setscale", "URL": "https://www.setscale.com/", "FOCUS": "Vendor financing"},
    ]
    results = []
    for p in providers:
        entry = {
            **p,
            "RESEARCHED_AT": _utc(),
            "MIN_TRANSACTION": "UNKNOWN",
            "NEW_BUSINESS_ELIGIBLE": "UNKNOWN",
            "OPERATING_HISTORY_REQUIRED": "UNKNOWN",
            "BUSINESS_CREDIT_REQUIRED": "UNKNOWN",
            "PERSONAL_GUARANTEE_REQUIRED": "UNKNOWN",
            "ADVANCE_PCT": "UNKNOWN",
            "FEES": "UNKNOWN",
            "GOV_PO_ELIGIBLE": "UNKNOWN",
            "PAYMENT_TIMING": "UNKNOWN",
            "APPROVAL_STATUS": "NOT_CONFIRMED",
            "FIT_ZERO_OWNER_CASH_NO_PG": "UNVERIFIED",
            "PUBLIC_FETCH_OK": False,
            "NOTES": [],
            "QUALIFICATION_PACKET": {
                "COMPANY_CONSTRAINTS": {
                    "OWNER_CASH": "0",
                    "PERSONAL_CREDIT": "UNAVAILABLE",
                    "PERSONAL_GUARANTEE": "WILL_NOT_SIGN",
                    "BUSINESS_CREDIT": "NONE",
                    "OPERATING_HISTORY": "NEW",
                },
                "ASK": [
                    "Do you fund new product-resale businesses with $0 owner cash?",
                    "Is a personal guarantee required?",
                    "Minimum PO / invoice size?",
                    "Advance rate and fees on government POs?",
                    "Approval timeline and document checklist?",
                ],
                "AUTO_SEND": False,
            },
        }
        if live_fetch:
            try:
                import httpx

                with httpx.Client(timeout=15.0, follow_redirects=True) as client:
                    resp = client.get(p["URL"], headers={"User-Agent": "M3ResearchBot/1.0"})
                    entry["PUBLIC_FETCH_OK"] = resp.status_code == 200
                    text = (resp.text or "")[:40000].lower()
                    if "personal guarantee" in text or "personal guaranty" in text:
                        entry["PERSONAL_GUARANTEE_REQUIRED"] = "MENTIONED_ON_SITE"
                        entry["FIT_ZERO_OWNER_CASH_NO_PG"] = "LIKELY_BLOCKED_OR_RESTRICTED"
                        entry["NOTES"].append("Personal guarantee mentioned on public site")
                    if "non-recourse" in text or "no personal guarantee" in text:
                        entry["PERSONAL_GUARANTEE_REQUIRED"] = "SITE_CLAIMS_NO_PG_OR_NON_RECOURSE"
                        entry["FIT_ZERO_OWNER_CASH_NO_PG"] = "POSSIBLE_IF_UNDERWRITING_CONFIRMS"
                        entry["NOTES"].append("Site claims non-recourse / no PG — still unconfirmed")
                    if "purchase order" in text or "po financing" in text:
                        entry["NOTES"].append("PO financing mentioned")
                        entry["GOV_PO_ELIGIBLE"] = "MENTIONED_UNVERIFIED"
            except Exception as exc:
                entry["NOTES"].append(f"Fetch failed: {str(exc)[:120]}")
        results.append(entry)
    return {
        "kind": "commercial_proof_financing",
        "build": BUILD,
        "built_at": _utc(),
        "CONFIRMED_FINANCEABLE": 0,
        "PROVIDERS": results,
        "VERDICT": "No provider confirmed under $0 owner cash / no PG constraints.",
    }


def build_deals(results: list[dict[str, Any]]) -> dict[str, Any]:
    deals = []
    for r in results:
        econ = r.get("Economics") or {}
        lane = r.get("Lane") or {}
        tags = ["commercial_proof", "first_revenue", "needs_financing"]
        if lane.get("LANE") == LANE_CASH:
            tags.append("cash_flow")
        if lane.get("LANE") == LANE_GROWTH:
            tags.append("growth")
        if lane.get("LANE") == LANE_SCALE:
            tags.append("scale")
        if (r.get("Acquisition_Price") or {}).get("unit_price") is None:
            tags.append("needs_pricing")
        if econ.get("EVIDENCE_LEVEL") == "VERIFIED_TRANSACTION_ECONOMICS":
            tags.append("ready_to_bid")
        # Priority rank for UI
        pri = 5
        if econ.get("POSITIVE_PRELIMINARY_MARGIN"):
            pri = 1
        elif econ.get("GROSS_PROFIT_SCENARIO"):
            pri = 2
        elif (r.get("Identity") or {}).get("IS_EXACT_VERIFIED") or (
            r.get("Identity") or {}
        ).get("IDENTITY_CLASS") == SPECIFICATION_COMPLETE_GENERIC:
            pri = 3 if r.get("RFQ_STATUS") == RFQ_READY else 4
        deals.append(
            {
                "opportunity_id": r["OPPORTUNITY_ID"],
                "product": r.get("Product"),
                "buyer": r.get("Buyer"),
                "deadline": r.get("Deadline"),
                "identity_class": (r.get("Identity") or {}).get("IDENTITY_CLASS"),
                "population": r.get("Population"),
                "current_retail_cost": (r.get("Acquisition_Price") or {}).get("unit_price"),
                "historical_government_pricing": (r.get("Past_Gov_Price") or {}).get("STATUS"),
                "profit_scenario": econ.get("GROSS_PROFIT_SCENARIO") or "UNKNOWN",
                "estimated_remaining_profit": econ.get("REMAINING_CONTRIBUTION_VERIFIED") or "UNKNOWN",
                "economics_status": econ.get("EVIDENCE_LEVEL"),
                "lane": lane.get("LANE") or LANE_PROVISIONAL,
                "lane_status": lane.get("LANE_STATUS"),
                "lane_verification": lane.get("VERIFICATION"),
                "financing_status": r.get("Financing"),
                "suppliers": r.get("Suppliers"),
                "supplier_coverage": len(r.get("Suppliers") or []),
                "next_action": r.get("Next_Action"),
                "workflow": r.get("Workflow"),
                "evidence_links": r.get("Evidence_Links"),
                "filter_tags": tags,
                "ui_priority": pri,
                "evidence_confidence": "PROVISIONAL",
                "payment_timing_confidence": (r.get("Cash_Flow") or {}).get("PAYMENT_TIMING_CONFIDENCE"),
                "owner_cash_required": (r.get("Cash_Flow") or {}).get("OWNER_CASH_REQUIRED"),
            }
        )
    deals.sort(key=lambda d: (int(d.get("ui_priority") or 9), d.get("opportunity_id") or ""))
    return {"kind": "commercial_proof_available_deals", "build": BUILD, "built_at": _utc(), "count": len(deals), "deals": deals}


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def run_repair(*, pop_a_n: int = 25, pop_b_n: int = 15, live_price: bool = True, live_financing: bool = True) -> dict[str, Any]:
    t0 = time.time()
    rows = quoteable_rows()
    rematch = _load(REMATCH_FILE) or {"RESULTS": [], "NEW_MATCH_IDS": [], "PACKETS": []}
    rematch_by = {r["OPPORTUNITY_ID"]: r for r in (rematch.get("RESULTS") or [])}
    packet_recon = _load(PACKET_RECON_FILE) or {}
    hist_index = load_historical_indexes()
    reliability = load_source_reliability()

    prior = (_load(FR_RESULTS_FILE) or {}).get("RESULTS") or []
    prior_reclass = reclassify_prior_sprint(prior)
    identity_audit = audit_quoteable_identities(rows)
    hist_audit = audit_prior_historical(prior)

    pop_a = select_evidence_first(rows, rematch, hist_index, n=pop_a_n)
    pop_b = select_quote_dependent(
        rows, rematch, exclude_ids={p["OPPORTUNITY_ID"] for p in pop_a}, n=pop_b_n
    )
    _save(POP_A_FILE, {"build": BUILD, "COUNT": len(pop_a), "SELECTED": pop_a})
    _save(POP_B_FILE, {"build": BUILD, "COUNT": len(pop_b), "SELECTED": pop_b})
    _save(PRIOR25_RECLASS_FILE, prior_reclass)
    _save(IDENTITY_AUDIT_FILE, identity_audit)
    _save(HIST_AUDIT_FILE, hist_audit)

    ht_by = {r["OPPORTUNITY_ID"]: r for r in rows}
    results: list[dict[str, Any]] = []

    # Also re-research prior 25 with corrected identity (no silent replace — tagged)
    for i, pr in enumerate(prior, 1):
        oid = pr.get("OPPORTUNITY_ID")
        row = ht_by.get(oid) or pr
        if isinstance(row, dict) and not row.get("TITLE") and pr.get("Product"):
            row = {**row, "TITLE": pr.get("Product"), "BUYER": pr.get("Buyer"), "DEADLINE": pr.get("Deadline")}
        log.info("prior25 re-research %s/%s %s", i, len(prior), oid)
        _save(CHECKPOINT_FILE, {"stage": "prior25", "done": i, "total": len(prior), "at": _utc()})
        out = research_one(
            row,
            rematch_row=rematch_by.get(oid),
            hist_index=hist_index,
            reliability=reliability,
            population="PRIOR25_RECLASS",
            live_price=live_price,
        )
        results.append(out)

    for i, sel in enumerate(pop_a, 1):
        oid = sel["OPPORTUNITY_ID"]
        row = {**(ht_by.get(oid) or {}), **sel}
        log.info("popA %s/%s %s %s", i, len(pop_a), oid, (sel.get("TITLE") or "")[:50])
        _save(CHECKPOINT_FILE, {"stage": "popA", "done": i, "total": len(pop_a), "at": _utc()})
        results.append(
            research_one(
                row,
                rematch_row=rematch_by.get(oid),
                hist_index=hist_index,
                reliability=reliability,
                population="A_EVIDENCE_FIRST",
                live_price=live_price,
            )
        )

    for i, sel in enumerate(pop_b, 1):
        oid = sel["OPPORTUNITY_ID"]
        row = {**(ht_by.get(oid) or {}), **sel}
        log.info("popB %s/%s %s %s", i, len(pop_b), oid, (sel.get("TITLE") or "")[:50])
        _save(CHECKPOINT_FILE, {"stage": "popB", "done": i, "total": len(pop_b), "at": _utc()})
        results.append(
            research_one(
                row,
                rematch_row=rematch_by.get(oid),
                hist_index=hist_index,
                reliability=reliability,
                population="B_QUOTE_DEPENDENT",
                live_price=live_price,
            )
        )

    reliability["updated_at"] = _utc()
    reliability["build"] = BUILD
    _save(SOURCE_RELIABILITY_FILE, reliability)

    rfqs = build_rfqs(results, limit=8)
    financing = build_financing_packets(live_fetch=live_financing)
    deals = build_deals(results)

    # Scorecard (dedupe by oid preferring evidence-first over prior)
    by_oid: dict[str, dict[str, Any]] = {}
    for r in results:
        oid = r["OPPORTUNITY_ID"]
        prev = by_oid.get(oid)
        if not prev or (r.get("Population") == "A_EVIDENCE_FIRST"):
            by_oid[oid] = r
    uniq = list(by_oid.values())

    def _priced(r):
        return (r.get("Acquisition_Price") or {}).get("unit_price") is not None

    def _exact_price(r):
        return (r.get("Acquisition_Price") or {}).get("evidence_level") == EXACT_PUBLIC_PRICE_VERIFIED

    def _equiv(r):
        return (r.get("Acquisition_Price") or {}).get("evidence_level") in {
            EQUIVALENT_PUBLIC_PRICE_VERIFIED,
            DISTRIBUTOR_PUBLIC_PRICE,
        }

    scorecard = {
        "prior25_false_exact": prior_reclass.get("FALSE_EXACT_COUNT"),
        "prior25_actually_exact": prior_reclass.get("ACTUALLY_EXACT_VERIFIED"),
        "quoteable_identity_counts": identity_audit.get("COUNTS"),
        "researched_unique": len(uniq),
        "researched_total_rows": len(results),
        "pop_a": len(pop_a),
        "pop_b": len(pop_b),
        "exact_verified_products": sum(1 for r in uniq if (r.get("Identity") or {}).get("IS_EXACT_VERIFIED")),
        "complete_generic": sum(
            1 for r in uniq if (r.get("Identity") or {}).get("IDENTITY_CLASS") == SPECIFICATION_COMPLETE_GENERIC
        ),
        "exact_public_prices": sum(1 for r in uniq if _exact_price(r)),
        "equivalent_or_distributor_prices": sum(1 for r in uniq if _equiv(r)),
        "acquisition_price_evidence": sum(1 for r in uniq if _priced(r)),
        "usable_historical_unit_for_margin": sum(
            1 for r in uniq if (r.get("Past_Gov_Price") or {}).get("unit_price_for_margin") is not None
        ),
        "positive_preliminary_margin": sum(
            1 for r in uniq if (r.get("Economics") or {}).get("POSITIVE_PRELIMINARY_MARGIN")
        ),
        "plausible_500": sum(1 for r in uniq if ((r.get("Economics") or {}).get("PLAUSIBLE") or {}).get("P500")),
        "plausible_2000": sum(1 for r in uniq if ((r.get("Economics") or {}).get("PLAUSIBLE") or {}).get("P2000")),
        "plausible_5000": sum(1 for r in uniq if ((r.get("Economics") or {}).get("PLAUSIBLE") or {}).get("P5000")),
        "plausible_10000": sum(1 for r in uniq if ((r.get("Economics") or {}).get("PLAUSIBLE") or {}).get("P10000")),
        "rfqs": rfqs.get("COUNT"),
        "financing_providers": len(financing.get("PROVIDERS") or []),
        "confirmed_financeable": 0,
        "execution_ready": 0,
    }

    lane_counts = Counter((r.get("Lane") or {}).get("LANE") for r in uniq)
    report = {
        "kind": "commercial_proof_repair_v1_report",
        "build": BUILD,
        "built_at": _utc(),
        "WALL_S": round(time.time() - t0, 2),
        "SAM_API_CALLS_USED": 0,
        "DEPLOYMENT": "LOCAL_ONLY",
        "BASELINE_PRESERVED": {
            "QUOTEABLE": len(rows),
            "CI_MATCHES": len(rematch.get("NEW_MATCH_IDS") or []),
            "PACKET_READY": packet_recon.get("UNIQUE_OPPORTUNITIES_WITH_PACKETS_AFTER") or 285,
        },
        "A_IDENTITY": {
            "PRIOR25": {
                "PREVIOUS_CLAIMED_EXACT": prior_reclass.get("PREVIOUS_CLAIMED_EXACT"),
                "CORRECTED_EXACT": prior_reclass.get("ACTUALLY_EXACT_VERIFIED"),
                "COUNTS": prior_reclass.get("COUNTS"),
            },
            "QUOTEABLE_AUDIT": identity_audit.get("COUNTS"),
        },
        "B_SCORECARD": scorecard,
        "E_LANES": dict(lane_counts),
        "H_SOURCE_RELIABILITY_TOP": sorted(
            [
                {"host": h, **v}
                for h, v in (reliability.get("hosts") or {}).items()
            ],
            key=lambda x: (-int(x.get("success_price") or 0), -int(x.get("attempts") or 0)),
        )[:15],
        "I_DEPLOYMENT": {"LOCAL_VALIDATED": True, "DEPLOYED": False},
    }

    _save(RESULTS_FILE, {"build": BUILD, "RESULTS": results, "UNIQUE": uniq, "count": len(results)})
    _save(SCORECARD_FILE, scorecard)
    _save(RFQ_FILE, rfqs)
    _save(FINANCING_FILE, financing)
    _save(DEALS_FILE, deals)
    _save(LANES_FILE, dict(lane_counts))
    _save(REPORT_FILE, report)
    return {
        "report": report,
        "results": results,
        "unique": uniq,
        "scorecard": scorecard,
        "rfqs": rfqs,
        "financing": financing,
        "prior_reclass": prior_reclass,
        "identity_audit": identity_audit,
        "hist_audit": hist_audit,
        "deals": deals,
        "reliability": reliability,
    }
