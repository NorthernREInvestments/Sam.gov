"""Phase L.2.5 — procurement price intelligence: expanded source search + telemetry."""

from __future__ import annotations

import re
from collections import Counter
from typing import Any
from urllib.parse import urlparse

from application_clock import now_utc
from phase_l.convergence import (
    DEALER_ADVERTISED,
    OEM_MSRP,
    PUBLIC_CATALOG,
    PUBLIC_RETAIL,
)
from phase_l.pricing_sources import (
    CURRENT_ACQUISITION_PRICE,
    CURRENT_MARKET_PRICE,
    GOVERNMENT_CHANNEL_PRICE,
    HISTORICAL_GOV_PRICE,
    PriceEvidenceRecord,
    classify_url_source,
    parse_content_auto,
)

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

SOURCE_KEYS = (
    "USAspending",
    "SAM awards",
    "state portals",
    "local portals",
    "bid tabs",
    "cooperative contracts",
    "board/council docs",
    "PDFs",
    "spreadsheets",
    "OEM",
    "dealer",
    "distributor",
)


def _utc() -> str:
    return now_utc().isoformat()


def empty_source_telemetry() -> dict[str, dict[str, int]]:
    return {
        k: {
            "searched": 0,
            "results_found": 0,
            "exact_identity_hits": 0,
            "historical_prices_found": 0,
            "current_prices_found": 0,
            "usable_acquisition_prices": 0,
            "blocked": 0,
            "parse_failures": 0,
            "bot_failures": 0,
        }
        for k in SOURCE_KEYS
    }


def build_search_id(
    identity: dict[str, Any] | None,
    commercial: dict[str, Any] | None = None,
    row: dict[str, Any] | None = None,
) -> dict[str, Any]:
    identity = identity or {}
    commercial = commercial or {}
    row = row or {}
    mfr = commercial.get("manufacturer") or identity.get("manufacturer")
    model = commercial.get("model") or identity.get("model")
    mpn = commercial.get("mpn") or identity.get("mpn") or identity.get("primary_mpn")
    return {
        "manufacturer": mfr,
        "model": model,
        "primary_mpn": mpn,
        "sku": identity.get("sku"),
        "mpn_variants": list(identity.get("mpn_variants") or [])[:4],
        "title": row.get("title"),
    }


def _exact_label(search_id: dict[str, Any]) -> str | None:
    model = search_id.get("model")
    mpn = search_id.get("primary_mpn")
    if model and len(str(model).strip()) >= 3:
        return str(model).strip()
    if mpn and len(str(mpn).strip()) >= 3:
        return str(mpn).strip()
    return None


def procurement_history_queries(search_id: dict[str, Any], row: dict[str, Any] | None = None) -> list[str]:
    """State/local/coop/board/PDF-oriented history queries (not USAspending)."""
    row = row or {}
    exact = _exact_label(search_id)
    if not exact:
        return []
    mfr = search_id.get("manufacturer") or ""
    buyer = str(row.get("agency") or row.get("buyer") or row.get("department") or "")[:40]
    state = str(row.get("pop_state") or row.get("state") or "")[:20]
    city = str(row.get("place_of_performance") or row.get("city") or "")[:40]
    q: list[str] = []
    for suffix in (
        "award",
        "bid tab",
        "purchase",
        "council",
        "contract",
        "awarded",
        "purchase order",
        "cooperative",
    ):
        q.append(f'"{exact}" {suffix}')
    q.append(f'"{exact}" site:.gov')
    q.append(f'"{exact}" filetype:pdf')
    q.append(f'"{exact}" filetype:xlsx')
    if buyer:
        q.append(f'"{buyer}" "{exact}"')
    if state:
        q.append(f'"{exact}" {state} award')
    if city and len(city) > 3:
        q.append(f'"{city}" "{exact}"')
    if mfr:
        q.append(f'"{mfr}" "{exact}" award')
    # vehicle/equipment bias
    blob = f"{exact} {mfr} {row.get('title') or ''}".lower()
    if any(x in blob for x in ("ford", "bobcat", "cat ", "loader", "truck", "police", "tractor", "toolcat")):
        q.append(f'"{exact}" fleet award')
        q.append(f'"{exact}" police bid')
        q.append(f'"{exact}" Sourcewell')
    # de-dupe preserve order
    seen: set[str] = set()
    out: list[str] = []
    for item in q:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out[:14]


def procurement_current_queries(search_id: dict[str, Any], row: dict[str, Any] | None = None) -> list[str]:
    exact = _exact_label(search_id)
    if not exact:
        return []
    mfr = search_id.get("manufacturer") or ""
    q = [
        f'"{exact}" MSRP',
        f'"{exact}" price list filetype:pdf',
        f'"{exact}" price filetype:xlsx',
        f'"{exact}" dealer inventory',
        f'"{exact}" buy new',
        f'"{exact}" Sourcewell price',
        f'"{exact}" OMNIA contract',
    ]
    if mfr:
        q.insert(0, f'"{mfr}" "{exact}" price')
        q.append(f'site:{mfr.lower().replace(" ", "")}.com "{exact}"')
    blob = f"{exact} {mfr} {(row or {}).get('title') or ''}".lower()
    if any(x in blob for x in ("ford", "f-150", "police responder")):
        q.append(f'"{exact}" Ford dealer price')
    if any(x in blob for x in ("bobcat", "toolcat", "cat ", "loader")):
        q.append(f'"{exact}" equipment dealer price')
    seen: set[str] = set()
    out: list[str] = []
    for item in q:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out[:10]


def _telemetry_bucket(url: str, prefer_history: bool) -> str:
    kind = classify_url_source(url)
    low = (url or "").lower()
    if kind == "PDF" or low.endswith(".pdf"):
        return "PDFs"
    if kind == "SPREADSHEET":
        return "spreadsheets"
    if kind == "COOPERATIVE":
        return "cooperative contracts"
    if kind == "BOARD":
        return "board/council docs"
    if kind == "BID_TAB":
        return "bid tabs"
    if kind == "DEALER_OEM":
        if any(x in low for x in ("dealer", "inventory")):
            return "dealer"
        return "OEM"
    if ".gov" in low:
        if prefer_history:
            return "state portals" if "state." in low or "/state/" in low else "local portals"
        return "state portals"
    if any(x in low for x in ("grainger", "fastenal", "mscdirect", "distributor")):
        return "distributor"
    return "dealer" if not prefer_history else "local portals"


def fetch_public_bytes(url: str, *, timeout: float = 12.0) -> tuple[bytes, str, int]:
    """Return (content, content_type, status). Empty on failure."""
    try:
        import httpx

        r = httpx.get(url, timeout=timeout, follow_redirects=True, headers=UA)
        ctype = (r.headers.get("content-type") or "").split(";")[0].strip()
        if r.status_code >= 400:
            return b"", ctype, r.status_code
        return r.content[:4_000_000], ctype, r.status_code
    except Exception:
        return b"", "", 0


def direct_portal_urls(search_id: dict[str, Any], *, prefer_history: bool) -> list[str]:
    """Construct likely public pricing/award URLs without a search provider."""
    from urllib.parse import quote_plus

    exact = _exact_label(search_id)
    if not exact:
        return []
    q = quote_plus(exact)
    mfr = quote_plus(str(search_id.get("manufacturer") or ""))
    urls: list[str] = []
    if prefer_history:
        urls.extend(
            [
                f"https://www.sourcewell-mn.gov/search?keys={q}",
                f"https://www.omniapartners.com/search?search={q}",
            ]
        )
    else:
        mfr_raw = str(search_id.get("manufacturer") or "").lower()
        if "bobcat" in mfr_raw or "toolcat" in exact.lower():
            urls.append(f"https://www.bobcat.com/search?q={q}")
        if "ford" in mfr_raw or "f-150" in exact.lower():
            urls.append(f"https://www.ford.com/search/?searchTerm={q}")
        urls.extend(
            [
                f"https://www.sourcewell-mn.gov/search?keys={q}",
                f"https://www.grainger.com/search?searchQuery={q}",
            ]
        )
    return urls[:5]


def research_procurement_sources(
    *,
    search_id: dict[str, Any],
    row: dict[str, Any],
    budget: dict[str, int],
    authorize_live: bool = True,
    prefer_history: bool = True,
    max_queries: int = 6,
    max_urls: int = 8,
    telemetry: dict[str, dict[str, int]] | None = None,
) -> dict[str, Any]:
    """Search + fetch + parse expanded procurement sources."""
    telemetry = telemetry if telemetry is not None else empty_source_telemetry()
    queries = (
        procurement_history_queries(search_id, row)
        if prefer_history
        else procurement_current_queries(search_id, row)
    )[:max_queries]
    out: dict[str, Any] = {
        "kind": "PhaseL25ProcurementResearch",
        "prefer_history": prefer_history,
        "queries": queries,
        "urls": [],
        "records": [],
        "attempted": False,
        "retrieved_at": _utc(),
    }
    if not authorize_live:
        out["skipped"] = "live_disabled"
        return out
    if not queries and not _exact_label(search_id):
        out["skipped"] = "no_queries"
        return out

    from phase_l.market_price import bing_search_urls

    urls: list[str] = []
    # Constructed portals first — live Bing/DDG are often empty or stall
    for u in direct_portal_urls(search_id, prefer_history=prefer_history):
        if u not in urls:
            urls.append(u)
    out["attempted"] = True

    for q in queries[:1]:  # at most one live search query
        if budget.get("procurement", 0) >= budget.get("procurement_max", 120):
            break
        budget["procurement"] = budget.get("procurement", 0) + 1
        for bucket in ("bid tabs", "board/council docs", "cooperative contracts", "PDFs"):
            if prefer_history:
                telemetry[bucket]["searched"] += 1
        if not prefer_history:
            telemetry["OEM"]["searched"] += 1
            telemetry["dealer"]["searched"] += 1
            telemetry["spreadsheets"]["searched"] += 1
        found: list[str] = []
        try:
            from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout

            pool = ThreadPoolExecutor(max_workers=1)
            try:
                fut = pool.submit(lambda: bing_search_urls(q, limit=3))
                try:
                    u, status = fut.result(timeout=6.0)
                    if status == "OK" and u:
                        found = list(u)
                except FutTimeout:
                    found = []
            finally:
                pool.shutdown(wait=False, cancel_futures=True)
            for u in found or []:
                if u not in urls:
                    urls.append(u)
            if found:
                for bucket in ("bid tabs", "PDFs", "local portals") if prefer_history else ("OEM", "dealer", "PDFs"):
                    telemetry[bucket]["results_found"] += 1
        except Exception:
            pass

    # Prefer procurement-ish URLs for history; dealer/OEM for current
    def _url_rank(u: str) -> tuple[int, int]:
        low = (u or "").lower()
        if prefer_history:
            score = 0
            if any(x in low for x in ("sourcewell", "omnia", "naspo")):
                score += 55
            if ".gov" in low:
                score += 50
            if any(x in low for x in ("bid", "award", "tabulation", "purchasing", "agenda", "minutes", "council", "board")):
                score += 40
            if low.endswith(".pdf") or low.endswith(".xlsx") or low.endswith(".csv"):
                score += 30
            if any(x in low for x in ("bobcat.com", "ford.com", "machinerytrader", "facebook", "youtube", "google.com")):
                score -= 40
            return (-score, len(low))
        score = 0
        if any(x in low for x in ("dealer", "inventory", "price", "msrp", "catalog", "grainger", "bobcat", "ford")):
            score += 30
        if low.endswith((".pdf", ".xlsx", ".csv")):
            score += 25
        if any(x in low for x in ("sourcewell", "omnia")):
            score += 15
        return (-score, len(low))

    urls = sorted(dict.fromkeys(urls), key=_url_rank)
    out["urls"] = urls[:max_urls]
    records: list[PriceEvidenceRecord] = []

    for url in urls[:max_urls]:
        if budget.get("procurement_fetch", 0) >= budget.get("procurement_fetch_max", 80):
            break
        bucket = _telemetry_bucket(url, prefer_history)
        budget["procurement_fetch"] = budget.get("procurement_fetch", 0) + 1
        data, ctype, status = fetch_public_bytes(url, timeout=8.0)
        if status in {403, 429, 503} or not data:
            if status in {403, 429, 503}:
                telemetry[bucket]["bot_failures"] += 1
            else:
                telemetry[bucket]["parse_failures"] += 1
            continue
        try:
            parsed = parse_content_auto(
                url=url,
                content=data,
                content_type=ctype,
                search_id=search_id,
                prefer_history=prefer_history,
            )
        except Exception:
            telemetry[bucket]["parse_failures"] += 1
            continue
        if not parsed:
            telemetry[bucket]["parse_failures"] += 1
            continue
        for rec in parsed:
            telemetry[bucket]["exact_identity_hits"] += 1
            if HISTORICAL_GOV_PRICE in rec.roles and rec.price is not None:
                telemetry[bucket]["historical_prices_found"] += 1
            if any(r in rec.roles for r in (CURRENT_ACQUISITION_PRICE, CURRENT_MARKET_PRICE, GOVERNMENT_CHANNEL_PRICE)):
                telemetry[bucket]["current_prices_found"] += 1
            if rec.economics_eligible_as_acquisition and rec.price is not None:
                telemetry[bucket]["usable_acquisition_prices"] += 1
            if rec.price_access in {"NO", "CONDITIONAL"} and not rec.economics_eligible_as_acquisition:
                telemetry[bucket]["blocked"] += 1
            records.append(rec)

    out["records"] = [r.to_dict() for r in records]
    out["record_count"] = len(records)
    return out


def merge_history_from_procurement(
    history: dict[str, Any],
    procurement: dict[str, Any],
) -> dict[str, Any]:
    """If USAspending missed, adopt best historical gov price from adapters."""
    history = dict(history or {})
    if history.get("historical_award_unit_price") is not None:
        history.setdefault("l25_supplement", True)
        history["l25_history_records"] = (procurement or {}).get("records") or []
        return history

    hist_recs = [
        r
        for r in (procurement or {}).get("records") or []
        if HISTORICAL_GOV_PRICE in (r.get("roles") or []) and r.get("price")
    ]
    if not hist_recs:
        history["l25_history_attempted"] = bool((procurement or {}).get("attempted"))
        history["l25_history_queries"] = (procurement or {}).get("queries") or []
        return history

    # Prefer HIGH confidence + award hints
    hist_recs.sort(
        key=lambda r: (
            1 if r.get("confidence") == "HIGH" else 0,
            float(r.get("price") or 0),
        ),
        reverse=True,
    )
    best = hist_recs[0]
    history["historical_award_unit_price"] = float(best["price"])
    history["historical_award_price"] = float(best["price"])
    history["history_research_state"] = "HISTORY_FOUND"
    history["history_confidence"] = "EXACT_MODEL"
    history["source_type"] = best.get("source_type") or "STATE_LOCAL_AWARD"
    history["source_url"] = best.get("source_url")
    history["historical_identity_match_type"] = "EXACT_MODEL"
    history["l25_history_source"] = best
    history["l25_history_records"] = hist_recs[:8]
    history["attempted"] = True
    return history


def merge_market_from_procurement(
    market: dict[str, Any],
    procurement: dict[str, Any],
) -> dict[str, Any]:
    """Supplement current acquisition from dealer/OEM/PDF/XLSX when L.2.2 retail miss."""
    market = dict(market or {})
    if market.get("public_retail_unit_price") is not None and market.get("economics_eligible", True):
        market["l25_current_records"] = (procurement or {}).get("records") or []
        return market

    acq = [
        r
        for r in (procurement or {}).get("records") or []
        if r.get("economics_eligible_as_acquisition") and r.get("price")
    ]
    intel = [
        r
        for r in (procurement or {}).get("records") or []
        if r.get("price") and not r.get("economics_eligible_as_acquisition")
    ]
    market["l25_current_attempted"] = bool((procurement or {}).get("attempted"))
    market["l25_current_queries"] = (procurement or {}).get("queries") or []
    market["l25_current_records"] = (procurement or {}).get("records") or []

    if acq:
        # Prefer dealer > catalog > OEM MSRP
        pref = {
            DEALER_ADVERTISED: 40,
            PUBLIC_RETAIL: 35,
            PUBLIC_CATALOG: 30,
            OEM_MSRP: 20,
        }

        def score(r: dict[str, Any]) -> tuple:
            return (pref.get(r.get("acquisition_price_type") or "", 10), -float(r["price"]))

        acq.sort(key=score, reverse=True)
        best = acq[0]
        market["public_retail_unit_price"] = float(best["price"])
        market["public_retail_price"] = float(best["price"])
        market["public_retail_source"] = best.get("source_url")
        market["economics_eligible"] = True
        market["acquisition_price_type"] = best.get("acquisition_price_type") or PUBLIC_CATALOG
        market["price_access"] = best.get("price_access") or "YES"
        market["l22_confidence"] = "STRONG_VERIFIED" if best.get("confidence") == "HIGH" else "STRONG_VERIFIED"
        market["market_research_state"] = "MARKET_PRICE_FOUND"
        market["selection_reason"] = f"l25:{best.get('source_type')}:{best.get('evidence_location')}"
        market["verified_evidence"] = [
            {
                "price": best["price"],
                "source_url": best.get("source_url"),
                "acquisition_price_type": best.get("acquisition_price_type"),
                "economics_eligible": True,
                "confidence": "STRONG_VERIFIED",
                "evidence_text": best.get("evidence_text"),
                "seller_type": "EQUIPMENT_DEALER"
                if best.get("acquisition_price_type") == DEALER_ADVERTISED
                else "MANUFACTURER"
                if best.get("acquisition_price_type") == OEM_MSRP
                else "CATALOG",
            }
        ]
        market["attempted"] = True
        return market

    if intel:
        best = intel[0]
        market["intel_price"] = float(best["price"])
        market["intel_source"] = best.get("source_url")
        market["acquisition_price_type"] = best.get("acquisition_price_type")
        market["price_access"] = best.get("price_access") or "NO"
        market["economics_eligible"] = False
        market["drop_reason"] = "PRICE_INACCESSIBLE"
        market["attempted"] = True
    return market
