"""Commercial Gap Closure V1 — completeness lots, live acquisition prices, selling prices.

Implements repairs then executes real free/cached research.
No fabricated prices. AUTO_SEND remains false. Respects commercial budget gates.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections import Counter
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import httpx

from application_clock import now_utc
from buyer_intelligence.commercial_budget_control_v1 import (
    dashboard_payload,
    is_paid_research_paused,
)
from buyer_intelligence.commercial_intelligence_upgrade_v1 import _load, _save, _utc
from buyer_intelligence.commercial_proof_repair_v1 import EXACT_PRODUCT_VERIFIED
from buyer_intelligence.first_revenue_validation_sprint_v1 import _days_to_deadline
from buyer_intelligence.product_line_store_v1 import get_entry, get_lines, load_store
from buyer_intelligence.research_novelty_engine_v1 import (
    HEAVILY_RESEARCHED,
    evidence_version_hash,
    record_research_attempt,
    should_revisit,
)
from buyer_intelligence.revenue_strategy_engine_v1 import (
    assign_lane_from_contribution,
    calculate_remaining_contribution,
)
from buyer_intelligence.supplier_outreach_queue_v1 import sync_from_rfqs
from m3_data_root import data_path

log = logging.getLogger("govtracker.commercial_gap_closure_v1")

BUILD = "20261012-m3-commercial-gap-closure-v1"
OUT = "_og_commercial_research_pipeline_out"

CHECKPOINT = f"{OUT}/m3_cgc_v1_checkpoint.json"
RESULTS = f"{OUT}/m3_cgc_v1_results.json"
REPORT = f"{OUT}/m3_cgc_v1_report.json"
PRICE_FAIL = f"{OUT}/m3_cgc_v1_price_failures.json"
HIST_REPORT = f"{OUT}/m3_cgc_v1_historical_report.json"
RFQS = f"{OUT}/m3_cgc_v1_rfqs.json"
FINANCE = f"{OUT}/m3_cgc_v1_financing.json"
DEALS = f"{OUT}/m3_cgc_v1_available_deals.json"
COMPLETENESS = f"{OUT}/m3_cgc_v1_completeness.json"

# Sources with known login/JS walls — do not burn retries
CIRCUIT_BLOCKED = {
    "grainger.com",
    "mscdirect.com",
    "ferguson.com",
    "mcmaster.com",
    "supplyhouse.com",
}

_DOLLAR = re.compile(r"\$\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\d+\.\d{2})")
_UA = {
    "User-Agent": "Mozilla/5.0 (compatible; M3CommercialResearch/1.0; +https://northernreinvestments.example)",
    "Accept": "text/html,application/xhtml+xml",
}


def _ckpt(**kwargs: Any) -> None:
    cur = _load(CHECKPOINT) or {"kind": "cgc_checkpoint", "build": BUILD}
    cur.update(kwargs)
    cur["at"] = _utc()
    _save(CHECKPOINT, cur)


# ─── PHASE 1: Completeness / bidding lots ───────────────────────────────────


def _line_is_mandatory(ln: dict[str, Any]) -> bool:
    if ln.get("MANDATORY") is False or ln.get("OPTIONAL") is True:
        return False
    desc = str(ln.get("DESCRIPTION") or "").lower()
    if any(x in desc for x in ("optional", "if needed", "as needed", "alternate", "or equal may")):
        # soft — still count as mandatory unless explicitly OPTIONAL flag
        if "optional" in desc and "mandatory" not in desc:
            return False
    return True


def _line_complete_identity(ln: dict[str, Any]) -> bool:
    if ln.get("MPN") or ln.get("IDENTITY_CLASS") == EXACT_PRODUCT_VERIFIED:
        return True
    # Complete generic: description + qty + uom + enough detail
    desc = str(ln.get("DESCRIPTION") or "")
    if len(desc) >= 40 and ln.get("QTY") not in (None, "", 0, "0") and ln.get("UOM"):
        specs = ln.get("TECHNICAL_SPECS") or {}
        if specs or re.search(r"\d+\s*(in|inch|awg|psi|gal|qt|lb|mm|cm|vac|amp)", desc, re.I):
            return True
    return False


def detect_bidding_structure(entry: dict[str, Any], lines: list[dict[str, Any]]) -> dict[str, Any]:
    """Infer whether partial/lot bids appear permitted from text — never assume."""
    blob = " ".join(
        [
            str(entry.get("TITLE") or ""),
            str(entry.get("DESCRIPTION") or ""),
            " ".join(str(ln.get("DESCRIPTION") or "") for ln in lines[:30]),
        ]
    ).lower()
    partial_allowed = None
    evidence = []
    if any(x in blob for x in ("partial bid", "bids may be submitted for one or more", "award by line", "line item award", "may award by lot")):
        partial_allowed = True
        evidence.append("TEXT_ALLOWS_PARTIAL_OR_LINE_AWARD")
    if any(x in blob for x in ("all or none", "all-or-none", "must bid all items", "complete bid required", "no partial")):
        partial_allowed = False
        evidence.append("TEXT_REQUIRES_ALL_OR_NONE")
    if partial_allowed is None:
        evidence.append("UNKNOWN_DO_NOT_ASSUME_PARTIAL")
    # Lot grouping: same manufacturer family / section headers
    lots: list[dict[str, Any]] = []
    if lines:
        # Default: one primary lot = all mandatory lines
        mand = [ln for ln in lines if _line_is_mandatory(ln)]
        lots.append(
            {
                "lot_id": "LOT_PRIMARY",
                "line_count": len(mand) or len(lines),
                "lines": mand or lines,
                "basis": "ALL_MANDATORY_OR_ALL_LINES",
            }
        )
        # Secondary: split by manufacturer when ≥2 distinct and each has ≥1 MPN
        by_mfr: dict[str, list] = {}
        for ln in mand or lines:
            m = str(ln.get("MANUFACTURER") or ln.get("BRAND") or "").strip().upper() or "UNKNOWN"
            by_mfr.setdefault(m, []).append(ln)
        if len(by_mfr) >= 2 and all(any(x.get("MPN") for x in v) for v in by_mfr.values()):
            for i, (mfr, lns) in enumerate(sorted(by_mfr.items()), 1):
                if mfr == "UNKNOWN" and len(lns) > 8:
                    continue
                lots.append(
                    {
                        "lot_id": f"LOT_MFR_{i}_{mfr[:20]}",
                        "manufacturer": mfr,
                        "line_count": len(lns),
                        "lines": lns,
                        "basis": "MANUFACTURER_GROUP_CANDIDATE",
                        "note": "Only bid as separate lot if solicitation permits partial/lot awards",
                    }
                )
    return {
        "partial_bids_allowed": partial_allowed,
        "evidence": evidence,
        "lots": [
            {
                "lot_id": lt["lot_id"],
                "line_count": lt["line_count"],
                "basis": lt["basis"],
                "manufacturer": lt.get("manufacturer"),
                "complete": all(_line_complete_identity(ln) and ln.get("QTY") not in (None, "", 0, "0") for ln in lt["lines"]),
                "exact_mpn_lines": sum(1 for ln in lt["lines"] if ln.get("MPN")),
                "mpns": [ln.get("MPN") for ln in lt["lines"] if ln.get("MPN")][:12],
            }
            for lt in lots
        ],
    }


def classify_opportunity_completeness(oid: str) -> dict[str, Any]:
    e = get_entry(oid) or {}
    lines = get_lines(oid)
    mand = [ln for ln in lines if _line_is_mandatory(ln)] or list(lines)
    structure = detect_bidding_structure(e, lines)
    complete_mand = [ln for ln in mand if _line_complete_identity(ln) and ln.get("QTY") not in (None, "", 0, "0")]
    blockers = []
    if not lines:
        blockers.append("NO_PRODUCT_LINES")
    if any(not _line_complete_identity(ln) for ln in mand):
        blockers.append("MISSING_PRODUCT_IDENTITY")
    if any(ln.get("QTY") in (None, "", 0, "0") for ln in mand):
        blockers.append("MISSING_QUANTITY")
    if e.get("RECOVERY_STATUS") in {"ACCOUNT_ENTITLEMENT_REQUIRED", "EXTRACTION_FAILED"}:
        blockers.append(str(e.get("RECOVERY_STATUS")))
    full_contract = bool(mand) and len(complete_mand) == len(mand)
    complete_lots = [lt for lt in structure["lots"] if lt.get("complete") and lt["line_count"] <= 12]
    return {
        "opportunity_id": oid,
        "buyer": e.get("BUYER"),
        "title": e.get("TITLE"),
        "source": e.get("SOURCE"),
        "deadline": e.get("DEADLINE"),
        "line_count": len(lines),
        "mandatory_count": len(mand),
        "mandatory_complete": len(complete_mand),
        "full_mandatory_coverage": full_contract,
        "complete_bidding_lots": complete_lots,
        "complete_lot_count": len(complete_lots),
        "bidding_structure": structure,
        "blockers": blockers,
        "classification_note": (
            "FULL_CONTRACT"
            if full_contract
            else ("COMPLETE_LOT_CANDIDATE" if complete_lots else "INCOMPLETE")
        ),
    }


def run_completeness_audit() -> dict[str, Any]:
    store = load_store(force=True)
    rows = []
    for oid in (store.get("by_opportunity") or {}):
        if str(oid).startswith("__"):
            continue
        rows.append(classify_opportunity_completeness(oid))
    full = sum(1 for r in rows if r["full_mandatory_coverage"])
    lots = sum(r["complete_lot_count"] for r in rows)
    opps_with_lot = sum(1 for r in rows if r["complete_lot_count"] > 0)
    blocker_c = Counter(b for r in rows for b in r["blockers"])
    out = {
        "kind": "cgc_completeness_audit",
        "build": BUILD,
        "at": _utc(),
        "opportunity_count": len(rows),
        "full_mandatory_coverage_opps": full,
        "complete_bidding_lot_instances": lots,
        "opps_with_at_least_one_complete_lot": opps_with_lot,
        "blocker_counts": dict(blocker_c),
        "top_complete": sorted(
            [r for r in rows if r["full_mandatory_coverage"] or r["complete_lot_count"]],
            key=lambda x: (-int(x["full_mandatory_coverage"]), -x["complete_lot_count"], x["mandatory_count"]),
        )[:40],
        "NOTE": "Complete lots are candidates only when partial/lot bidding is permitted or unknown; UNKNOWN ≠ allowed",
    }
    _save(COMPLETENESS, out)
    return out


# ─── PHASE 2: Acquisition price routes ──────────────────────────────────────


def _failure_class(status: str | None, trace: dict | None = None) -> str:
    s = str(status or "")
    if s in {"INSUFFICIENT_IDENTITY"}:
        return "PRODUCT_IDENTITY_INSUFFICIENT"
    if s in {"PRICE_SOURCE_BLOCKED_RETRYABLE"}:
        return "SOURCE_BLOCKED_OR_LOGIN"
    if s in {"NO_PUBLIC_PRICE_AFTER_EXHAUSTIVE_SEARCH"}:
        return "PUBLIC_PRICE_UNAVAILABLE"
    if s in {"UOM_AMBIGUOUS"}:
        return "UNIT_PACKAGING_AMBIGUITY"
    if s in {"CONDITION_MISMATCH"}:
        return "PRODUCT_CONDITION_MISMATCH"
    if s in {"PRICE_SEARCH_BUDGET_EXHAUSTED"}:
        return "SEARCH_BUDGET"
    blocked = (trace or {}).get("blocked_sources") or []
    if blocked:
        return "SOURCE_BLOCKED_OR_LOGIN"
    return "PUBLIC_PRICE_UNAVAILABLE_OR_NOT_FOUND"


_CATEGORY_TOKENS = {
    "electrical_hv": ("circuit breaker", "vacuum interrupter", "kv ", "15kv", "69kv", "switchgear"),
    "medical_autopsy": ("autopsy", "cadaver", "mopec", "carrier chassis", "bariatric carrier"),
    "software_license": ("license", "subscription", "ekahau", "renewal"),
    "janitorial_paper": ("tissue", "towel", "tork", "essity", "multifold", "bath tissue"),
    "automotive_hd": ("brake", "meritor", "axle", "drum", "hub"),
    "ammunition": ("hornady", "ammunition", "luger", "critical duty", "grain", "fmj", "+p"),
}


def _expected_category(description: str | None, manufacturer: str | None = None) -> str | None:
    blob = f"{description or ''} {manufacturer or ''}".lower()
    for cat, tokens in _CATEGORY_TOKENS.items():
        if any(t in blob for t in tokens):
            return cat
    return None


def _price_evidence_compatible(
    *,
    mpn: str,
    description: str | None,
    manufacturer: str | None,
    url: str | None,
    supplier: str | None,
    page_hint: str | None = None,
) -> tuple[bool, str | None]:
    """Reject MPN-collision prices from unrelated product categories."""
    url_l = (url or "").lower()
    hint = f"{url_l} {supplier or ''} {page_hint or ''}".lower()
    cat = _expected_category(description, manufacturer)
    # Hard reject known mismatch hosts for HV electrical / medical
    if cat == "electrical_hv":
        if any(x in hint for x in ("revzilla", "motorcycle", "knife", "knifecountry", "ebay.com/sch", "webstaurant")):
            return False, "CATEGORY_MISMATCH_CONSUMER_RETAIL"
        if "circuit" not in hint and "breaker" not in hint and "interrupter" not in hint and "switchgear" not in hint:
            # Allow OEM/distributor hosts without category word if MPN in path
            if mpn.lower() not in url_l.replace("-", ""):
                return False, "CATEGORY_MISMATCH_OR_WEAK_IDENTITY"
    if cat == "medical_autopsy":
        if any(
            x in hint
            for x in (
                "summitracing",
                "truckac",
                "racing",
                "oil filter",
                "e-z-clip",
                "velvac",
                "fuel-cap",
                "fuel cap",
            )
        ):
            return False, "CATEGORY_MISMATCH_AUTOMOTIVE"
        if "mopec" not in hint and "autopsy" not in hint and "cadaver" not in hint:
            return False, "CATEGORY_MISMATCH_OR_WEAK_IDENTITY"
    if cat == "software_license":
        if "ekahau" not in hint and "subscription" not in hint and "license" not in hint:
            return False, "CATEGORY_MISMATCH_SOFTWARE"
    if cat == "ammunition":
        if any(x in hint for x in ("lee-loader", "lee loader", "reloading press", "drarmory.com/lee")):
            return False, "CATEGORY_MISMATCH_RELOADING_TOOL"
        if "hornady" not in hint and "critical" not in hint and "ammo" not in hint and "ammunition" not in hint:
            if "90225" not in hint and "90235" not in hint and "90238" not in hint:
                return False, "CATEGORY_MISMATCH_OR_WEAK_IDENTITY"
    # HTML search-result / SERP pages are weak evidence — need a product page
    if (
        "/search" in url_l
        or "search?" in url_l
        or "/sch/" in url_l
        or "duckduckgo.com" in url_l
        or "html.duckduckgo.com" in url_l
        or "/html/?q=" in url_l
    ):
        return False, "SEARCH_PAGE_NOT_PRODUCT_PAGE"
    # Non-commerce / wrong vertical hosts
    if any(
        x in url_l
        for x in (
            "realtor.com",
            "zillow.com",
            "highergov.com",
            "sam.gov",
            "wikipedia.org",
            "facebook.com",
            "linkedin.com",
        )
    ):
        return False, "NON_COMMERCE_OR_WRONG_VERTICAL_HOST"
    return True, None


def research_mpn_public_price(
    *,
    mpn: str,
    manufacturer: str | None = None,
    description: str | None = None,
    opportunity_id: str | None = None,
    qty: Any = None,
    uom: str | None = None,
    deadline_s: float = 45,
) -> dict[str, Any]:
    """Route A/B/C via public_price_search.resolve_public_price + direct catalog probes."""
    t0 = time.time()
    mpn_u = str(mpn or "").strip().upper()
    if not mpn_u or len(mpn_u) < 3:
        return {
            "status": "NO_NEW_EVIDENCE",
            "failure_class": "PRODUCT_IDENTITY_INSUFFICIENT",
            "mpn": mpn,
            "newly_acquired": False,
        }

    # Known exact catalog (prior verified — mark reused not new)
    known = {
        "TM1616S": {
            "unit_price": "47.44",
            "uom": "CASE",
            "pack_qty": 96,
            "url": "https://www.hecdistribution.com/shop/housekeeping-and-janitorial-products/paper-products/bathroom-tissue/essity-tork-bath-tissue-tm1616s/",
            "supplier": "HEC",
            "class": "EXACT_PUBLIC_ACQUISITION_PRICE",
            "reused": True,
        },
        "MK520A": {
            "unit_price": "34.99",
            "uom": "CASE",
            "pack_qty": 4000,
            "url": "https://www.webstaurantstore.com/tork-universal-natural-kraft-multifold-paper-towel-h2-case/500MFTMK520A.html",
            "supplier": "WebstaurantStore",
            "class": "EXACT_PUBLIC_ACQUISITION_PRICE",
            "reused": True,
        },
        "D420": {
            "unit_price": "1592.00",
            "uom": "EA",
            "url": "https://www.gofoodservice.com/p/manitowoc-rfp0620a-161",
            "supplier": "GoFoodservice",
            "class": "EXACT_PUBLIC_ACQUISITION_PRICE",
            "reused": True,
        },
        # Re-verified 2026-10-16 via live public_price_search (Elliott Electric).
        "150010": {
            "unit_price": "360.79",
            "uom": "CARTON",
            "url": "https://www.elliottelectric.com/P/Item/WIR/150010/",
            "supplier": "Elliott Electric",
            "class": "EXACT_PUBLIC_ACQUISITION_PRICE",
            "reused": True,
        },
    }
    if mpn_u in known:
        hit = known[mpn_u]
        return {
            "status": "COMPLETED",
            "newly_acquired": False,
            "failure_class": None,
            "mpn": mpn_u,
            "unit_price": hit["unit_price"],
            "uom": hit.get("uom"),
            "pack_qty": hit.get("pack_qty"),
            "url": hit["url"],
            "supplier": hit["supplier"],
            "class": hit["class"],
            "qty": qty,
            "timestamp": _utc(),
            "route": "KNOWN_CATALOG_REUSED",
            "elapsed_s": round(time.time() - t0, 3),
        }

    identity = {
        "part_number": mpn_u,
        "manufacturer": manufacturer,
        "raw_description": description,
        "model": mpn_u,
    }
    try:
        from public_price_search.models import PUBLIC_PRICE_FOUND, PUBLIC_PRICE_PARTIAL
        from public_price_search.resolver import resolve_public_price

        with httpx.Client(timeout=12.0, follow_redirects=True, headers=_UA) as client:
            # Cap queries/pages tightly for free research
            result = resolve_public_price(
                identity,
                opportunity_id=opportunity_id,
                client=client,
                max_queries=3,
                max_pages=4,
                use_budget=True,
            )
        status = result.get("status")
        ev = result.get("evidence") or {}
        if status in {PUBLIC_PRICE_FOUND, PUBLIC_PRICE_PARTIAL} and ev.get("unit_price"):
            url = ev.get("url") or ev.get("source_url")
            supplier = ev.get("seller") or ev.get("seller_class")
            ok, reason = _price_evidence_compatible(
                mpn=mpn_u,
                description=description,
                manufacturer=manufacturer,
                url=url,
                supplier=str(supplier) if supplier else None,
            )
            if not ok:
                # Fall through to probe / failure — do not accept collision
                status = "REJECTED_CATEGORY_MISMATCH"
            else:
                return {
                    "status": "COMPLETED",
                    "newly_acquired": True,
                    "failure_class": None,
                    "mpn": mpn_u,
                    "unit_price": str(ev.get("unit_price")),
                    "uom": ev.get("uom") or uom,
                    "pack_qty": ev.get("pack_qty"),
                    "url": url,
                    "supplier": supplier,
                    "class": "EXACT_PUBLIC_ACQUISITION_PRICE"
                    if status == PUBLIC_PRICE_FOUND
                    else "COMPLIANT_EQUIVALENT_PRICE",
                    "qty": qty,
                    "timestamp": _utc(),
                    "route": "PUBLIC_PRICE_SEARCH_RESOLVER",
                    "match_type": result.get("match_type"),
                    "condition": ev.get("condition"),
                    "elapsed_s": round(time.time() - t0, 3),
                    "openai_usd": "0",
                    "sam_calls": 0,
                }
        # Direct catalog probe (Route A) — free HTML, no paid APIs
        if time.time() - t0 < deadline_s:
            probe = _direct_catalog_probe(mpn_u, manufacturer, description=description)
            if probe:
                probe.update(
                    {
                        "newly_acquired": True,
                        "qty": qty,
                        "timestamp": _utc(),
                        "elapsed_s": round(time.time() - t0, 3),
                        "openai_usd": "0",
                        "sam_calls": 0,
                    }
                )
                return probe
        fail_class = (
            "INCORRECT_PRODUCT_MATCH_REJECTED"
            if status == "REJECTED_CATEGORY_MISMATCH"
            else _failure_class(status, result.get("search_trace"))
        )
        return {
            "status": "NO_NEW_EVIDENCE",
            "newly_acquired": False,
            "failure_class": fail_class,
            "mpn": mpn_u,
            "resolver_status": status,
            "stop_reason": result.get("stop_reason") or result.get("failure_reason"),
            "blocked_sources": (result.get("search_trace") or {}).get("blocked_sources"),
            "elapsed_s": round(time.time() - t0, 3),
            "route": "PUBLIC_PRICE_SEARCH_RESOLVER",
        }
    except Exception as exc:
        return {
            "status": "FAILED",
            "newly_acquired": False,
            "failure_class": "SOFTWARE_OR_NETWORK_ERROR",
            "mpn": mpn_u,
            "error": f"{type(exc).__name__}:{exc}"[:200],
            "elapsed_s": round(time.time() - t0, 3),
        }


def _direct_catalog_probe(
    mpn: str, manufacturer: str | None, *, description: str | None = None
) -> dict[str, Any] | None:
    """Lightweight free probes against open catalogs (errors still classified)."""
    cat = _expected_category(description, manufacturer)
    # Skip consumer retail catalogs for industrial/medical categories
    if cat in {"electrical_hv", "medical_autopsy"}:
        queries = []
        if manufacturer:
            queries.append(
                f"https://duckduckgo.com/html/?q={quote_plus(str(manufacturer) + ' ' + mpn + ' price')}"
            )
        if cat == "medical_autopsy":
            queries.append(f"https://www.mopec.com/search?q={quote_plus(mpn)}")
        if not queries:
            return None
    else:
        queries = [
            f"https://www.zoro.com/search?q={quote_plus(mpn)}",
            f"https://www.finditparts.com/search?q={quote_plus(mpn)}",
        ]
        if manufacturer:
            queries.insert(0, f"https://duckduckgo.com/html/?q={quote_plus(manufacturer + ' ' + mpn + ' price')}")
    try:
        with httpx.Client(timeout=10.0, follow_redirects=True, headers=_UA) as client:
            for url in queries[:3]:
                host = re.sub(r"^https?://(www\.)?", "", url).split("/")[0].lower()
                if any(b in host for b in CIRCUIT_BLOCKED):
                    continue
                try:
                    resp = client.get(url)
                except Exception:
                    continue
                if resp.status_code in {401, 403}:
                    continue
                if resp.status_code >= 400:
                    continue
                final_url = str(resp.url)
                text = resp.text or ""
                # Require MPN presence
                if mpn.upper() not in text.upper() and mpn.replace("-", "") not in text.upper().replace("-", ""):
                    continue
                ok, reason = _price_evidence_compatible(
                    mpn=mpn,
                    description=description,
                    manufacturer=manufacturer,
                    url=final_url,
                    supplier=host,
                    page_hint=text[:2000],
                )
                if not ok:
                    continue
                # Prefer product pages over search listings
                if "/search" in final_url.lower() or "search?" in final_url.lower():
                    continue
                prices = []
                for m in _DOLLAR.finditer(text[:200000]):
                    try:
                        prices.append(Decimal(m.group(1).replace(",", "")))
                    except Exception:
                        pass
                # Filter noise — keep plausible unit prices
                prices = [p for p in prices if Decimal("0.50") <= p <= Decimal("50000")]
                if not prices:
                    continue
                # Prefer median-ish lower quartile for list noise
                prices.sort()
                pick = prices[min(len(prices) // 4, len(prices) - 1)]
                return {
                    "status": "COMPLETED",
                    "mpn": mpn,
                    "unit_price": str(pick),
                    "uom": "EA",
                    "url": final_url,
                    "supplier": host,
                    "class": "EXACT_PUBLIC_ACQUISITION_PRICE",
                    "route": "DIRECT_CATALOG_HTML_PROBE",
                    "note": "Extracted from public HTML; verify pack/UOM before bidding",
                    "prices_seen_count": len(prices),
                }
    except Exception:
        return None
    return None


# ─── PHASE 3: Historical selling prices ─────────────────────────────────────


def _scan_cached_historical(mpn: str, buyer: str | None = None) -> list[dict[str, Any]]:
    """Search persisted commercial/HT artifacts for unit prices — no SAM spend."""
    hits: list[dict[str, Any]] = []
    mpn_u = mpn.upper()
    roots = [
        Path(data_path(OUT)),
        Path(data_path(".")),
    ]
    patterns = [
        "*historical*",
        "*fpd*",
        "*award*",
        "*profit*",
        "*bid*tab*",
        "*gometro*",
        "*hcso*",
    ]
    files: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for pat in patterns:
            files.extend(root.glob(pat))
            files.extend(root.glob(f"**/{pat}"))
    seen = set()
    for path in files[:80]:
        if not path.is_file() or path.suffix.lower() not in {".json"}:
            continue
        if path.stat().st_size > 40_000_000:
            continue
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        try:
            blob = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue
        raw = json.dumps(blob)[:500000].upper()
        if mpn_u not in raw and mpn_u.replace("-", "") not in raw.replace("-", ""):
            continue
        # Pull dollar amounts near MPN occurrences in string form
        text = json.dumps(blob)
        for m in re.finditer(re.escape(mpn), text, re.I):
            window = text[max(0, m.start() - 200) : m.end() + 200]
            for dm in _DOLLAR.finditer(window):
                try:
                    price = Decimal(dm.group(1).replace(",", ""))
                except Exception:
                    continue
                if price < 1 or price > 100000:
                    continue
                match_level = "EXACT_PRODUCT_OTHER_BUYER"
                if buyer and buyer.lower() in window.lower():
                    match_level = "EXACT_PRODUCT_SAME_BUYER"
                hits.append(
                    {
                        "mpn": mpn_u,
                        "unit_price": str(price),
                        "match_level": match_level,
                        "source_file": path.name,
                        "snippet": window[:180],
                        "buyer_hint": buyer,
                        "class": match_level,
                    }
                )
                if len(hits) >= 5:
                    return hits
    return hits


def research_historical_unit_price(oid: str, *, max_mpns: int = 5) -> dict[str, Any]:
    e = get_entry(oid) or {}
    lines = get_lines(oid)
    buyer = str(e.get("BUYER") or "")
    findings = []
    failures = []
    # Prior FPD reuse (not newly acquired)
    if oid in HEAVILY_RESEARCHED or "Tampa" in buyer or "Dayton" in buyer:
        fpd = _load(f"{OUT}/m3_fpd_bidnet_v1_commercial.json") or {}
        key = "tampa" if ("Tampa" in buyer or oid == "8e7ae0c1316f0d7f") else "dayton" if ("Dayton" in buyer or oid == "777499f8df1dcb7f") else None
        if key and (fpd.get(key) or {}).get("HISTORICAL_GOV_PRICING"):
            findings.append(
                {
                    "newly_acquired": False,
                    "class": (fpd[key]["HISTORICAL_GOV_PRICING"] or {}).get("CLASS"),
                    "evidence": fpd[key]["HISTORICAL_GOV_PRICING"],
                    "route": "FPD_CACHE_REUSED",
                }
            )
    for ln in lines[:max_mpns]:
        mpn = str(ln.get("MPN") or "").strip()
        if not mpn:
            failures.append({"mpn": None, "failure": "NO_MPN", "match_level": "NO_USABLE_PRICE"})
            continue
        hits = _scan_cached_historical(mpn, buyer)
        if hits:
            # Prefer same-buyer
            hits.sort(key=lambda h: 0 if "SAME_BUYER" in h["match_level"] else 1)
            best = hits[0]
            best["newly_acquired"] = True  # newly found in this scan of caches
            best["qty"] = ln.get("QTY")
            findings.append(best)
        else:
            failures.append(
                {
                    "mpn": mpn,
                    "failure": "NO_CACHED_UNIT_PRICE",
                    "match_level": "NO_USABLE_PRICE",
                    "note": "SAM not consumed; free/cache only",
                }
            )
    usable = [
        f
        for f in findings
        if f.get("match_level")
        in {
            "EXACT_PRODUCT_SAME_BUYER",
            "EXACT_PRODUCT_OTHER_BUYER",
            "SPEC_MATCH_SAME_BUYER",
            "SPEC_MATCH_OTHER_BUYER",
            "SPECIFICATION_MATCHED_UNIT_PRICE",
        }
        or f.get("class")
        in {
            "EXACT_PRODUCT_SAME_BUYER",
            "EXACT_PRODUCT_OTHER_BUYER",
            "SPECIFICATION_MATCHED_UNIT_PRICE",
            "SPEC_MATCH_SAME_BUYER",
        }
    ]
    return {
        "task": "RESEARCH_HISTORICAL_BUYER_PRICE",
        "status": "COMPLETED" if usable else "SELLING_PRICE_UNKNOWN",
        "class": (usable[0].get("match_level") or usable[0].get("class")) if usable else "SELLING_PRICE_UNKNOWN",
        "findings": findings,
        "usable": usable,
        "failures": failures,
        "sam_calls": 0,
        "newly_acquired_count": sum(1 for f in findings if f.get("newly_acquired")),
        "reused_count": sum(1 for f in findings if f.get("newly_acquired") is False),
    }


# ─── Selection + execution ───────────────────────────────────────────────────


def select_research_cohort(*, limit: int = 50, min_fresh_mpn: int = 30) -> list[dict[str, Any]]:
    store = load_store(force=True)
    scored = []
    for oid, e in (store.get("by_opportunity") or {}).items():
        if str(oid).startswith("__"):
            continue
        lines = e.get("LINES") or []
        if not lines:
            continue
        days = _days_to_deadline(e.get("DEADLINE"))
        if days is not None and days < 1:
            continue
        mpns = [str(ln.get("MPN")).strip() for ln in lines if ln.get("MPN")]
        n = len(lines)
        exact = len(mpns)
        score = 0
        if 1 <= n <= 5:
            score += 50
        elif n <= 12:
            score += 25
        if exact:
            score += 40 + min(20, exact * 3)
        if oid not in HEAVILY_RESEARCHED:
            score += 35
        else:
            score -= 50
        if e.get("SOURCE") == "OpenGov":
            score += 10
        if days and days >= 5:
            score += 10
        # Prefer product-like titles
        title = str(e.get("TITLE") or "").lower()
        if any(x in title for x in ("service", "construction", "install", "renovation", "janitorial service")):
            score -= 40
        scored.append(
            {
                "opportunity_id": oid,
                "title": e.get("TITLE"),
                "buyer": e.get("BUYER"),
                "source": e.get("SOURCE"),
                "deadline": e.get("DEADLINE"),
                "line_count": n,
                "exact_lines": exact,
                "mpns": mpns[:8],
                "days_left": days,
                "score": score,
                "previously_researched": oid in HEAVILY_RESEARCHED,
            }
        )
    scored.sort(key=lambda x: -x["score"])
    fresh = [x for x in scored if not x["previously_researched"] and x["exact_lines"] > 0]
    out = []
    seen = set()
    for x in fresh:
        if x["opportunity_id"] in seen:
            continue
        out.append(x)
        seen.add(x["opportunity_id"])
        if len(out) >= max(min_fresh_mpn, int(limit * 0.6)):
            break
    for x in scored:
        if x["opportunity_id"] in seen:
            continue
        out.append(x)
        seen.add(x["opportunity_id"])
        if len(out) >= limit:
            break
    _save(
        f"{OUT}/m3_cgc_v1_selection.json",
        {"build": BUILD, "at": _utc(), "count": len(out), "ids": [x["opportunity_id"] for x in out], "opportunities": out},
    )
    return out[:limit]


def execute_opportunity_research(opp: dict[str, Any], *, price_budget_s: float = 50) -> dict[str, Any]:
    oid = opp["opportunity_id"]
    t0 = time.time()
    completeness = classify_opportunity_completeness(oid)
    entry = get_entry(oid) or {}
    eh = evidence_version_hash(entry)
    prices = []
    price_failures = []
    gate = should_revisit(
        oid,
        task="RESEARCH_PUBLIC_ACQUISITION_PRICE",
        method="public_price_search_v1",
        sources=["public_html", "ddg", "zoro", "webstaurant", "finditparts"],
        current_evidence_hash=eh,
    )
    lines = get_lines(oid)
    researched_mpns = 0
    if gate.get("allowed"):
        for ln in lines:
            if time.time() - t0 > price_budget_s:
                break
            mpn = ln.get("MPN")
            if not mpn:
                continue
            researched_mpns += 1
            pr = research_mpn_public_price(
                mpn=str(mpn),
                manufacturer=ln.get("MANUFACTURER") or ln.get("BRAND"),
                description=ln.get("DESCRIPTION"),
                opportunity_id=oid,
                qty=ln.get("QTY"),
                uom=ln.get("UOM"),
                deadline_s=min(40, price_budget_s - (time.time() - t0)),
            )
            pr["line"] = ln.get("LINE_NUMBER")
            if pr.get("unit_price") and pr.get("newly_acquired"):
                prices.append(pr)
            elif pr.get("unit_price") and not pr.get("newly_acquired"):
                prices.append({**pr, "reused": True})
            else:
                price_failures.append(pr)
            record_research_attempt(
                oid,
                task="RESEARCH_PUBLIC_ACQUISITION_PRICE",
                method="public_price_search_v1",
                sources=[pr.get("route") or "none"],
                status=str(pr.get("status") or "NO_NEW_EVIDENCE"),
                prices_recovered=1 if pr.get("unit_price") and pr.get("newly_acquired") else 0,
                evidence_hash=eh,
            )
    else:
        price_failures.append({"failure_class": "NOVELTY_SUPPRESSED", "detail": gate})

    hist = research_historical_unit_price(oid)
    record_research_attempt(
        oid,
        task="RESEARCH_HISTORICAL_BUYER_PRICE",
        method="cached_artifact_scan",
        sources=["local_artifacts"],
        status=str(hist.get("status")),
        historical_recovered=hist.get("newly_acquired_count") or 0,
        evidence_hash=eh,
    )

    # Economics: only when acquisition known; selling may be unknown → CONDITIONAL/UNKNOWN
    acq_total = Decimal("0")
    have_acq = False
    for p in prices:
        try:
            unit = Decimal(str(p.get("unit_price")))
            qty = p.get("qty")
            if qty not in (None, "", 0, "0"):
                acq_total += (unit * Decimal(str(qty))).quantize(Decimal("0.01"))
                have_acq = True
            else:
                acq_total += unit
                have_acq = True
        except Exception:
            continue
    sell = None
    if hist.get("usable"):
        try:
            sell = Decimal(str(hist["usable"][0]["unit_price"]))
            # rough: unit * qty of first priced line if available
            q0 = hist["usable"][0].get("qty")
            if q0 not in (None, "", 0, "0"):
                sell = (sell * Decimal(str(q0))).quantize(Decimal("0.01"))
        except Exception:
            sell = None
    contrib = calculate_remaining_contribution(
        selling_price=sell,
        acquisition_cost=acq_total if have_acq else None,
        freight=None,
        financing_fees=None,
    )
    lane = assign_lane_from_contribution(contrib)

    # Suppliers / RFQ readiness
    from buyer_intelligence.commercial_execution_queue_v1 import task_find_suppliers, task_prepare_rfq, task_financing_gap

    suppliers = task_find_suppliers(oid)
    rfq = task_prepare_rfq(oid, suppliers.get("suppliers"))
    econ = {"acquisition_cost": str(acq_total) if have_acq else None, "contribution": contrib, "lane": lane}
    fin = task_financing_gap(oid, econ)

    return {
        "opportunity_id": oid,
        "title": opp.get("title"),
        "buyer": opp.get("buyer"),
        "deadline": opp.get("deadline"),
        "previously_researched": opp.get("previously_researched"),
        "completeness": completeness,
        "novelty_gate": gate,
        "mpns_researched": researched_mpns,
        "prices": prices,
        "price_failures": price_failures,
        "new_prices": sum(1 for p in prices if p.get("newly_acquired")),
        "reused_prices": sum(1 for p in prices if p.get("reused") or p.get("newly_acquired") is False),
        "historical": hist,
        "economics": econ,
        "suppliers": suppliers,
        "rfq": rfq,
        "financing": fin,
        "wall_s": round(time.time() - t0, 3),
        "openai_usd": "0.0000",
        "sam_calls": 0,
    }


def build_deals(results: list[dict[str, Any]]) -> dict[str, Any]:
    deals = []
    for r in results:
        econ = r.get("economics") or {}
        contrib = econ.get("contribution") or {}
        lane = econ.get("lane") or {}
        prices = r.get("prices") or []
        hist = r.get("historical") or {}
        deals.append(
            {
                "opportunity_id": r.get("opportunity_id"),
                "product": r.get("title"),
                "buyer": r.get("buyer"),
                "deadline": r.get("deadline"),
                "mandatory_coverage": (r.get("completeness") or {}).get("mandatory_complete"),
                "mandatory_total": (r.get("completeness") or {}).get("mandatory_count"),
                "full_mandatory": (r.get("completeness") or {}).get("full_mandatory_coverage"),
                "complete_lots": (r.get("completeness") or {}).get("complete_lot_count"),
                "acquisition_prices": [
                    {"mpn": p.get("mpn"), "unit_price": p.get("unit_price"), "url": p.get("url"), "new": p.get("newly_acquired")}
                    for p in prices
                ],
                "public_acquisition_cost": econ.get("acquisition_cost"),
                "historical_class": hist.get("class"),
                "historical_unit": (hist.get("usable") or [{}])[0].get("unit_price") if hist.get("usable") else None,
                "contribution_state": contrib.get("CONTRIBUTION_STATE"),
                "conditional_remaining_contribution": contrib.get("REMAINING_CONTRIBUTION")
                if contrib.get("CONTRIBUTION_STATE") == "CONDITIONAL_CONTRIBUTION"
                else None,
                "verified_remaining_contribution": contrib.get("REMAINING_CONTRIBUTION")
                if contrib.get("CONTRIBUTION_STATE") == "VERIFIED_CONTRIBUTION"
                else None,
                "lane": lane.get("LANE") or "PROVISIONAL",
                "financing_status": (r.get("financing") or {}).get("financing_status"),
                "next_action": (
                    "Recover buyer unit prices"
                    if prices and not (hist.get("usable"))
                    else (
                        "Request supplier quote"
                        if (r.get("rfq") or {}).get("packet") and not prices
                        else (
                            "Finish product identification"
                            if not (r.get("completeness") or {}).get("full_mandatory")
                            else "Operator review RFQ — AUTO_SEND=false"
                        )
                    )
                ),
                "new_prices": r.get("new_prices"),
                "filter_tags": ["revenue_strategy", "cash_flow" if (lane.get("LANE") == "CASH_FLOW") else "needs_pricing"],
            }
        )
    deals.sort(
        key=lambda d: (
            0 if d.get("verified_remaining_contribution") else 1,
            0 if d.get("conditional_remaining_contribution") else 1,
            0 if d.get("public_acquisition_cost") else 1,
            0 if d.get("full_mandatory") else 1,
        )
    )
    out = {"kind": "cgc_available_deals", "build": BUILD, "built_at": _utc(), "count": len(deals), "deals": deals, "budget": dashboard_payload()}
    _save(DEALS, out)
    _save(f"{OUT}/m3_cex_v1_available_deals.json", {**out, "kind": "cex_available_deals", "NOTE": "Synced from commercial_gap_closure_v1"})
    _save(f"{OUT}/m3_rse_v1_available_deals.json", out)
    _save(f"{OUT}/m3_source_product_v1_available_deals.json", out)
    return out


def prepare_top_rfqs(results: list[dict[str, Any]], *, limit: int = 10) -> dict[str, Any]:
    ranked = sorted(
        results,
        key=lambda r: (
            -int((r.get("completeness") or {}).get("full_mandatory_coverage") or 0),
            -int(r.get("new_prices") or 0),
            -int((r.get("completeness") or {}).get("complete_lot_count") or 0),
            -int(r.get("mpns_researched") or 0),
        ),
    )
    packets = []
    for r in ranked:
        pkt = (r.get("rfq") or {}).get("packet")
        if not pkt:
            continue
        # Prefer supplier-specific
        if pkt.get("PRIMARY_SUPPLIER") in {None, "UNKNOWN", "Category distributor TBD"}:
            # still include if we have MPNs
            if not any(x.get("mpn") for x in (pkt.get("PRODUCTS") or [])):
                continue
        pkt = dict(pkt)
        pkt["RESEARCH_META"] = {
            "new_prices": r.get("new_prices"),
            "acquisition_cost": (r.get("economics") or {}).get("acquisition_cost"),
            "historical_class": (r.get("historical") or {}).get("class"),
        }
        packets.append(pkt)
        if len(packets) >= limit:
            break
    out = {"kind": "cgc_rfqs", "build": BUILD, "AUTO_SEND": False, "COUNT": len(packets), "PACKETS": packets}
    _save(RFQS, out)
    _save(f"{OUT}/m3_cex_v1_rfqs.json", {**out, "kind": "cex_rfqs"})
    sync_from_rfqs()
    return out


def prepare_financing_packets(results: list[dict[str, Any]], *, limit: int = 10) -> dict[str, Any]:
    packets = []
    for r in results:
        fin = r.get("financing") or {}
        pkt = fin.get("packet")
        if not pkt:
            continue
        # Prefer those with acquisition cost
        if not (r.get("economics") or {}).get("acquisition_cost") and len(packets) >= 3:
            continue
        packets.append(pkt)
        if len(packets) >= limit:
            break
    providers = [
        {
            "name": "SouthStar Capital",
            "public_claims_only": True,
            "confirmed": False,
            "barriers_unknown": [
                "new LLC",
                "personal guarantee",
                "personal credit",
                "minimum deal size",
                "prior contract history",
            ],
        },
        {"name": "TradeCap Partners", "confirmed": False, "public_claims_only": True},
        {"name": "King Trade Capital", "confirmed": False, "public_claims_only": True},
        {"name": "SMB Compass", "confirmed": False, "public_claims_only": True},
        {"name": "Setscale", "confirmed": False, "public_claims_only": True},
    ]
    out = {
        "kind": "cgc_financing",
        "build": BUILD,
        "AUTO_SUBMIT": False,
        "CONFIRMED": 0,
        "providers": providers,
        "packets": packets,
        "NOTE": "Advertisements are not confirmation",
    }
    _save(FINANCE, out)
    _save(f"{OUT}/m3_financing_qualification_packets_v1.json", out)
    return out


def run_gap_closure(
    *,
    max_opps: int = 50,
    price_target: int = 30,
    per_opp_price_s: float = 55,
) -> dict[str, Any]:
    t0 = time.time()
    _ckpt(stage="START", max_opps=max_opps)
    completeness = run_completeness_audit()
    cohort = select_research_cohort(limit=max_opps, min_fresh_mpn=min(30, max_opps))
    # Persist IDs before research
    results = []
    price_fail_log = []
    new_prices = 0
    reused_prices = 0
    new_hist = 0
    priced_opps = 0

    for i, opp in enumerate(cohort, 1):
        _ckpt(stage="EXECUTE", done=i, total=len(cohort), oid=opp["opportunity_id"])
        # Focus live price research on first price_target fresh MPN opps
        budget_s = per_opp_price_s if (i <= price_target and opp.get("exact_lines")) else 15
        try:
            r = execute_opportunity_research(opp, price_budget_s=budget_s)
        except Exception as exc:
            r = {"opportunity_id": opp["opportunity_id"], "status": "ERROR", "error": type(exc).__name__}
        results.append(r)
        new_prices += int(r.get("new_prices") or 0)
        reused_prices += int(r.get("reused_prices") or 0)
        new_hist += int((r.get("historical") or {}).get("newly_acquired_count") or 0)
        if r.get("new_prices"):
            priced_opps += 1
        for f in r.get("price_failures") or []:
            price_fail_log.append({"opportunity_id": opp["opportunity_id"], **f})
        log.info(
            "CGC %s/%s %s new_prices=%s hist=%s full=%s",
            i,
            len(cohort),
            opp["opportunity_id"],
            r.get("new_prices"),
            (r.get("historical") or {}).get("class"),
            (r.get("completeness") or {}).get("full_mandatory_coverage"),
        )

    deals = build_deals(results)
    rfqs = prepare_top_rfqs(results, limit=10)
    finance = prepare_financing_packets(results, limit=10)
    _save(RESULTS, {"build": BUILD, "at": _utc(), "results": results})
    _save(PRICE_FAIL, {"build": BUILD, "count": len(price_fail_log), "rows": price_fail_log, "by_class": dict(Counter(x.get("failure_class") for x in price_fail_log))})
    _save(
        HIST_REPORT,
        {
            "build": BUILD,
            "newly_acquired_unit_prices": new_hist,
            "rows": [
                {
                    "oid": r.get("opportunity_id"),
                    "class": (r.get("historical") or {}).get("class"),
                    "usable": (r.get("historical") or {}).get("usable"),
                    "failures": (r.get("historical") or {}).get("failures"),
                }
                for r in results
            ],
        },
    )

    full_after = completeness["full_mandatory_coverage_opps"]
    report = {
        "kind": "m3_commercial_gap_closure_v1_report",
        "build": BUILD,
        "built_at": _utc(),
        "elapsed_s": round(time.time() - t0, 2),
        "budget": dashboard_payload(),
        "paid_research_paused": is_paid_research_paused(),
        "A_SCORECARD": {
            "full_mandatory_before_declared": 4,
            "full_mandatory_after_audit": full_after,
            "complete_bidding_lots": completeness["complete_bidding_lot_instances"],
            "opps_with_complete_lot": completeness["opps_with_at_least_one_complete_lot"],
            "exact_acquisition_prices_before": 3,
            "new_acquisition_prices": new_prices,
            "reused_acquisition_prices": reused_prices,
            "opps_with_new_prices": priced_opps,
            "new_historical_unit_prices": new_hist,
            "complete_rfqs": rfqs.get("COUNT"),
            "verified_profitable": sum(1 for d in deals.get("deals") or [] if d.get("verified_remaining_contribution")),
            "conditional_positive": sum(
                1
                for d in deals.get("deals") or []
                if d.get("conditional_remaining_contribution")
                and float(str(d.get("conditional_remaining_contribution") or 0) or 0) > 0
            ),
            "confirmed_financeable": 0,
            "execution_ready": 0,
        },
        "completeness": {
            "full_mandatory_coverage_opps": full_after,
            "complete_bidding_lot_instances": completeness["complete_bidding_lot_instances"],
            "blocker_counts": completeness["blocker_counts"],
        },
        "price_failure_classes": dict(Counter(x.get("failure_class") for x in price_fail_log)),
        "cohort_size": len(cohort),
        "rfqs": rfqs.get("COUNT"),
        "finance_packets": len(finance.get("packets") or []),
        "top_deals": (deals.get("deals") or [])[:20],
        "NOTE": "No OpenAI/SAM spent by default; free/cached public research only",
    }
    _save(REPORT, report)
    _ckpt(stage="DONE", report_summary={k: report["A_SCORECARD"] for k in ("A_SCORECARD",)})
    return report
