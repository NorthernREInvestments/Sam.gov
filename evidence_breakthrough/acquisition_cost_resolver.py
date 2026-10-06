"""PublicAcquisitionCostResolver — real public prices only, no guessed wholesale."""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import quote_plus

import httpx

from evidence_breakthrough.models import (
    AMBIGUOUS_MATCH,
    FOUND,
    INSUFFICIENT_IDENTITY,
    NO_PUBLIC_PRICE,
    NO_PUBLIC_PRICE_FR,
    NOT_FOUND_AFTER_EXHAUSTIVE_SEARCH,
    PUBLIC_ALLOWED_EQUAL,
    PUBLIC_DISTRIBUTOR_EXACT,
    PUBLIC_RESELLER_EXACT,
    PUBLIC_RETAIL_EXACT,
    RETRYABLE_FAILURE,
    RETRYABLE_SOURCE_FAILURE,
    SUPPLIER_QUOTE,
    empty_resolver_result,
)

log = logging.getLogger("govtracker.evidence_breakthrough.acquisition")

_PRICE_RE = re.compile(
    r"(?:\$|USD\s*)(\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\d+\.\d{2})"
)
_DISTRIBUTOR_HOSTS = (
    "grainger.com",
    "zoro.com",
    "mscdirect.com",
    "globalindustrial.com",
    "fastenal.com",
    "mcmaster.com",
    "supplyhouse.com",
    "plumbingsupply.com",
    "ferguson.com",
    "wwd.com",
    "amazon.com",
    "ebay.com",
)


def _has_identity(identity: dict[str, Any]) -> bool:
    return bool(
        identity.get("part_number")
        or identity.get("catalog_number")
        or identity.get("model")
        or identity.get("sku")
        or identity.get("upc")
        or identity.get("nsn")
        or (identity.get("manufacturer") and identity.get("model"))
    )


def _query_variants(identity: dict[str, Any]) -> list[str]:
    mfr = (identity.get("manufacturer") or identity.get("brand") or "").strip()
    model = (identity.get("model") or "").strip()
    pn = (identity.get("part_number") or identity.get("catalog_number") or identity.get("sku") or "").strip()
    desc = (identity.get("raw_description") or "").strip()
    qs: list[str] = []
    if mfr and pn:
        qs.append(f"{mfr} {pn}")
    if mfr and model:
        qs.append(f"{mfr} {model}")
    if pn:
        qs.append(pn)
    if model and model != pn:
        qs.append(model)
    if mfr and model and pn and pn != model:
        qs.append(f"{mfr} {model} {pn}")
    # Short desc + model
    if model and desc:
        qs.append(f"{mfr} {model} {desc[:40]}".strip())
    # Dedupe
    out: list[str] = []
    seen: set[str] = set()
    for q in qs:
        qn = re.sub(r"\s+", " ", q).strip()
        if len(qn) < 3:
            continue
        key = qn.lower()
        if key not in seen:
            seen.add(key)
            out.append(qn)
    return out[:8]


def _basis_for_url(url: str, exact: bool) -> str:
    host = (url or "").lower()
    if any(h in host for h in ("grainger", "zoro", "mscdirect", "fastenal", "mcmaster", "globalindustrial", "ferguson", "supplyhouse")):
        return PUBLIC_DISTRIBUTOR_EXACT if exact else PUBLIC_ALLOWED_EQUAL
    if any(h in host for h in ("amazon", "ebay", "walmart", "homedepot", "lowes")):
        return PUBLIC_RESELLER_EXACT if exact else PUBLIC_ALLOWED_EQUAL
    return PUBLIC_RETAIL_EXACT if exact else PUBLIC_ALLOWED_EQUAL


def _direct_search_urls(query: str) -> list[str]:
    q = quote_plus(query)
    return [
        f"https://www.grainger.com/search?searchQuery={q}",
        f"https://www.zoro.com/search?q={q}",
        f"https://www.globalindustrial.com/search?q={q}",
        f"https://www.supplyhouse.com/search?q={q}",
        f"https://www.fastenal.com/product/search?term={q}",
        f"https://www.partsfish.com/oemparts/a/cum/search?q={q}",
        f"https://www.dieselpartsdirect.com/search?q={q}",
        f"https://www.fleetpride.com/search?q={q}",
    ]


def _serp_candidate_urls(query: str, *, limit: int = 6) -> list[str]:
    """Use existing Bing/DDG helpers when available."""
    urls: list[str] = []
    try:
        from phase_l.market_price import search_urls_with_fallback

        result = search_urls_with_fallback(query, limit=limit)
        for u in result.get("urls") or []:
            if u and u not in urls:
                urls.append(u)
    except Exception:
        pass
    if not urls:
        try:
            from phase_l.market_price import bing_search_urls

            bu, _ = bing_search_urls(query, limit=limit)
            for u in bu or []:
                if u and u not in urls:
                    urls.append(u)
        except Exception:
            pass
    return urls[:limit]


def _extract_prices(html: str) -> list[float]:
    vals: list[float] = []
    for m in _PRICE_RE.finditer(html or ""):
        try:
            v = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        if 0.5 <= v <= 500_000:
            vals.append(v)
    return vals


def _identity_on_page(html: str, identity: dict[str, Any]) -> bool:
    blob = (html or "").upper()
    tokens = []
    for k in ("part_number", "catalog_number", "model", "sku"):
        t = identity.get(k)
        if t and len(str(t)) >= 3:
            tokens.append(re.sub(r"[^A-Z0-9]", "", str(t).upper()))
    if not tokens:
        return False
    for t in tokens:
        if t and t in re.sub(r"[^A-Z0-9]", "", blob):
            return True
    return False


def _try_phase_l_market(identity: dict[str, Any], opportunity_id: str) -> dict[str, Any] | None:
    """Use existing L.2.2 market price research when available."""
    try:
        from phase_l.market_price import research_public_market_price
    except Exception:
        return None
    row = {
        "canonical_id": opportunity_id,
        "title": identity.get("raw_description"),
        "agency": None,
    }
    pn = identity.get("part_number") or identity.get("catalog_number") or identity.get("sku")
    ident = {
        "manufacturer": identity.get("manufacturer") or identity.get("brand"),
        "model": identity.get("model") or pn,
        "part_number": pn,
        "mpn": pn,
        "normalized_mpn": pn,
        "sku": identity.get("sku") or pn,
        "nsn": identity.get("nsn"),
        "upc": identity.get("upc"),
        "description": identity.get("raw_description"),
        "nomenclature": identity.get("raw_description"),
    }
    try:
        out = research_public_market_price(
            row,
            ident,
            budget={"market": 0, "market_max": 8},
            max_pages=6,
            authorize_live=True,
        )
    except Exception as exc:
        return {"error": type(exc).__name__, "detail": str(exc)[:200]}
    return out if isinstance(out, dict) else None


def _try_opengov_vendor_public_prices(
    identity: dict[str, Any],
    opportunity_id: str,
    *,
    history_cache: dict[str, dict[str, Any]] | None = None,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    """Public vendor unit prices from unsealed OpenGov bid tabs (named commercial sellers).

    Used only when retail catalogs are blocked. Never invents — requires PN match + unitPrice > 0.
    Prefers prices from other projects (not the current solicitation alone).
    """
    from evidence_breakthrough.opengov_history import (
        build_or_load_buyer_history,
        parse_opengov_opportunity_id,
        search_buyer_history,
    )

    gov_code, project_id = parse_opengov_opportunity_id(opportunity_id)
    if not gov_code:
        return []
    if history_cache is not None and gov_code in history_cache:
        profile = history_cache[gov_code]
    else:
        profile = build_or_load_buyer_history(gov_code, client=client)
        if history_cache is not None:
            history_cache[gov_code] = profile

    pn = identity.get("part_number") or identity.get("catalog_number") or identity.get("sku")
    model = identity.get("model")
    ranked = search_buyer_history(
        profile,
        part_number=str(pn) if pn else None,
        model=str(model) if model else None,
        manufacturer=str(identity.get("manufacturer") or "") or None,
        description=str(identity.get("raw_description") or "") or None,
    )
    out: list[dict[str, Any]] = []
    for hit in ranked:
        if hit["match_grade"] not in {
            "EXACT_PN",
            "EXACT_PN_TOKEN",
            "EXACT_MODEL",
            "EXACT_MODEL_TOKEN",
            "MFR_MODEL_IN_DESC",
        }:
            continue
        if hit["score"] < 85:
            continue
        ln = hit["line"]
        # Prefer other projects when possible
        same_project = str(ln.get("project_id") or "") == str(project_id or "")
        for vr in ln.get("all_priced_vendors") or [{"unit_price": ln.get("unit_price"), "vendor": ln.get("winning_vendor")}]:
            up = vr.get("unit_price")
            try:
                upf = float(up) if up is not None else None
            except (TypeError, ValueError):
                upf = None
            if not upf or upf <= 0:
                continue
            vendor = vr.get("vendor") or ln.get("winning_vendor")
            out.append(
                {
                    "unit_price": upf,
                    "source_url": ln.get("source_url"),
                    "seller": vendor,
                    "basis": PUBLIC_DISTRIBUTOR_EXACT,
                    "exact_match": True,
                    "confidence": "B" if not same_project else "C",
                    "via": "opengov_public_vendor_bid",
                    "same_project": same_project,
                    "project_id": ln.get("project_id"),
                    "award_date": ln.get("closed_at"),
                    "note": "Public unsealed vendor unit price on OpenGov bid tabulation",
                }
            )
    # Prefer other-project, then lowest price
    out.sort(key=lambda c: (1 if c.get("same_project") else 0, float(c["unit_price"])))
    return out[:6]


def _try_openai_web_price(identity: dict[str, Any], opportunity_id: str) -> list[dict[str, Any]]:
    """Bounded OpenAI web-search fallback — real URLs/prices only, never invents."""
    try:
        from m3_supplier_intelligence import research_public_pricing_web
    except Exception:
        return []
    pn = identity.get("part_number") or identity.get("catalog_number") or identity.get("sku")
    product = {
        "Manufacturer": identity.get("manufacturer") or identity.get("brand"),
        "Model": identity.get("model") or pn,
        "Part_number": pn,
        "NSN": identity.get("nsn"),
        "Description": identity.get("raw_description"),
        "sufficient_for_pricing_research": True,
    }
    row = {
        "canonical_id": opportunity_id,
        "title": identity.get("raw_description"),
        "agency": None,
        "solicitation_number": None,
    }
    try:
        meta = research_public_pricing_web(row, product, allow_paid=True)
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for ev in meta.get("evidence") or []:
        if not isinstance(ev, dict):
            continue
        amt = ev.get("amount") or ev.get("Price") or ev.get("unit_price") or ev.get("price")
        try:
            up = float(amt) if amt is not None else None
        except (TypeError, ValueError):
            up = None
        if not up or up <= 0:
            continue
        url = ev.get("source_url") or ev.get("url")
        # Require a source URL for defensible public price
        if not url:
            continue
        conf = str(ev.get("match_confidence") or ev.get("confidence") or "MEDIUM").upper()
        if conf == "LOW":
            continue
        out.append(
            {
                "unit_price": up,
                "source_url": url,
                "seller": ev.get("source_name") or ev.get("seller"),
                "basis": _basis_for_url(url, exact=conf == "HIGH"),
                "exact_match": conf == "HIGH",
                "confidence": "A" if conf == "HIGH" else "B",
                "via": "openai_web_search",
            }
        )
    return out


def resolve_public_acquisition_cost(
    identity: dict[str, Any],
    *,
    opportunity_id: str | None = None,
    client: httpx.Client | None = None,
    max_pages: int = 8,
    use_phase_l: bool = True,
    use_openai_fallback: bool = True,
    allow_web_crawl: bool = True,
    history_cache: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    result = empty_resolver_result(side="public_acquisition_cost")
    oid = opportunity_id or identity.get("opportunity_id") or "unknown"

    if not _has_identity(identity):
        result["status"] = INSUFFICIENT_IDENTITY
        result["failure_reason"] = "INSUFFICIENT_IDENTITY"
        result["match_type"] = NO_PUBLIC_PRICE
        result["stop_reason"] = INSUFFICIENT_IDENTITY
        return result

    queries = _query_variants(identity)
    result["queries_attempted"] = list(queries)
    sources: list[str] = []
    candidates: list[dict[str, Any]] = []

    # 0) Human-like public price search (manufacturer → SERP → distributor → snippets)
    # Prefer this over OpenGov vendor-bid-as-cost when a real public retail/distributor
    # page is available — vendor bids alone previously caused false NO_PUBLIC_PRICE.
    if allow_web_crawl:
        try:
            from public_price_search.models import (
                PRICE_SEARCH_BUDGET_EXHAUSTED as _PPS_BUDGET,
                PUBLIC_PRICE_FOUND as _PPS_FOUND,
                PUBLIC_PRICE_PARTIAL as _PPS_PARTIAL,
            )
            from public_price_search.resolver import resolve_public_price as _pps_resolve

            sources.append("public_price_search_v2")
            pps = _pps_resolve(
                identity,
                opportunity_id=str(oid),
                client=client,
                use_budget=True,
            )
            result["price_search_trace"] = pps.get("search_trace")
            result["price_search_budget"] = pps.get("price_search_budget")
            if pps.get("status") in {_PPS_FOUND, _PPS_PARTIAL}:
                ev = pps.get("evidence") or {}
                candidates.append(
                    {
                        "unit_price": ev.get("unit_price"),
                        "source_url": ev.get("source_url"),
                        "seller": ev.get("seller"),
                        "basis": ev.get("basis") or PUBLIC_DISTRIBUTOR_EXACT,
                        "exact_match": True,
                        "confidence": "A" if pps.get("status") == _PPS_FOUND else "B",
                        "via": "public_price_search_v2",
                        "condition": ev.get("condition"),
                        "displayed_price": ev.get("displayed_price"),
                        "core_charge": ev.get("core_charge"),
                        "core_refundable": ev.get("core_refundable"),
                        "gross_cash_required": ev.get("gross_cash_required"),
                        "shipping": ev.get("shipping"),
                    }
                )
            elif pps.get("status") == _PPS_BUDGET:
                # Do not collapse budget exhaustion into NO_PUBLIC_PRICE
                result["status"] = RETRYABLE_FAILURE
                result["failure_reason"] = _PPS_BUDGET
                result["match_type"] = NO_PUBLIC_PRICE
                result["stop_reason"] = _PPS_BUDGET
                result["sources_attempted"] = sources
                return result
        except Exception as exc:
            log.debug("public_price_search_v2 failed: %s", type(exc).__name__)

    # 0b) Fast path: public OpenGov vendor bid unit prices when buyer history is available
    # (retail catalogs are often bot-blocked; unsealed bid tabs are structured + named sellers)
    sources.append("opengov_public_vendor_bid")
    for cand in _try_opengov_vendor_public_prices(
        identity, str(oid), history_cache=history_cache, client=client
    ):
        candidates.append(cand)

    # 1) Phase L verified market research (bounded)
    if not candidates and use_phase_l:
        sources.append("phase_l_market_price")
        pl = _try_phase_l_market(identity, str(oid))
        if pl and pl.get("error"):
            pass
        elif pl:
            price = pl.get("public_retail_unit_price")
            ev_list = pl.get("evidence") or []
            if price and float(price) > 0:
                url = None
                seller = None
                if ev_list and isinstance(ev_list[0], dict):
                    url = ev_list[0].get("source_url") or ev_list[0].get("url")
                    seller = ev_list[0].get("seller")
                candidates.append(
                    {
                        "unit_price": float(price),
                        "source_url": url,
                        "seller": seller,
                        "basis": _basis_for_url(url or "", exact=True),
                        "exact_match": True,
                        "confidence": pl.get("market_price_confidence") or "B",
                        "via": "phase_l",
                    }
                )
            for ev in ev_list[:5]:
                if not isinstance(ev, dict):
                    continue
                up = ev.get("unit_price") or ev.get("price")
                try:
                    upf = float(up) if up is not None else None
                except (TypeError, ValueError):
                    upf = None
                if upf and upf > 0:
                    url = ev.get("source_url") or ev.get("url")
                    candidates.append(
                        {
                            "unit_price": upf,
                            "source_url": url,
                            "seller": ev.get("seller"),
                            "basis": _basis_for_url(url or "", exact=bool(ev.get("economics_eligible"))),
                            "exact_match": bool(ev.get("economics_eligible")),
                            "confidence": ev.get("confidence") or "B",
                            "via": "phase_l_evidence",
                        }
                    )

    # 2) SERP → product pages + direct distributor searches (fail-closed identity check)
    # Skip slow web crawl when OpenGov already produced exact PN matches.
    own = client is None
    c = client or httpx.Client(timeout=25.0, follow_redirects=True)
    pages = 0
    retryable = False
    skip_web = bool(candidates) or (not allow_web_crawl)
    try:
        if not skip_web:
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml",
            }
            fetch_urls: list[str] = []
            for q in queries[:4]:
                for u in _serp_candidate_urls(q, limit=5):
                    if u not in fetch_urls:
                        fetch_urls.append(u)
                for u in _direct_search_urls(q)[:3]:
                    if u not in fetch_urls:
                        fetch_urls.append(u)

            product_queue: list[str] = []
            for url in fetch_urls:
                if pages >= max_pages:
                    break
                sources.append(url.split("/")[2] if "://" in url else url)
                pages += 1
                try:
                    r = c.get(url, headers=headers)
                except (httpx.TimeoutException, httpx.TransportError):
                    retryable = True
                    continue
                if r.status_code in {429, 503}:
                    retryable = True
                    continue
                if r.status_code >= 400:
                    continue
                html = r.text or ""
                pn = str(identity.get("part_number") or identity.get("model") or "")
                if pn and len(pn) >= 4:
                    for href in re.findall(r'href=["\']([^"\']+)["\']', html, re.I):
                        if pn.lower() not in href.lower() and re.sub(r"[^a-z0-9]", "", pn.lower()) not in re.sub(
                            r"[^a-z0-9]", "", href.lower()
                        ):
                            continue
                        from urllib.parse import urljoin

                        full = urljoin(str(r.url), href)
                        if full.startswith("http") and full not in product_queue and full not in fetch_urls:
                            product_queue.append(full)
                        if len(product_queue) >= 6:
                            break
                if not _identity_on_page(html, identity):
                    continue
                prices = _extract_prices(html)
                if not prices:
                    continue
                prices_sorted = sorted(prices)[:8]
                mid = prices_sorted[len(prices_sorted) // 2]
                host = url.split("/")[2]
                if mid < 1.0 and any(p >= 1.0 for p in prices_sorted):
                    mid = sorted(p for p in prices_sorted if p >= 1.0)[0]
                candidates.append(
                    {
                        "unit_price": mid,
                        "source_url": str(r.url),
                        "seller": host,
                        "basis": _basis_for_url(url, exact=True),
                        "exact_match": True,
                        "confidence": "B",
                        "via": "public_web_search",
                        "prices_seen": prices_sorted[:5],
                    }
                )

            for url in product_queue:
                if pages >= max_pages + 4:
                    break
                pages += 1
                sources.append(url.split("/")[2] if "://" in url else url)
                try:
                    r = c.get(url, headers=headers)
                except (httpx.TimeoutException, httpx.TransportError):
                    retryable = True
                    continue
                if r.status_code >= 400:
                    continue
                html = r.text or ""
                if not _identity_on_page(html, identity):
                    continue
                prices = _extract_prices(html)
                if not prices:
                    continue
                prices_sorted = sorted([p for p in prices if p >= 0.5])[:8]
                if not prices_sorted:
                    continue
                mid = prices_sorted[len(prices_sorted) // 2]
                candidates.append(
                    {
                        "unit_price": mid,
                        "source_url": str(r.url),
                        "seller": url.split("/")[2],
                        "basis": _basis_for_url(url, exact=True),
                        "exact_match": True,
                        "confidence": "A",
                        "via": "product_page",
                        "prices_seen": prices_sorted[:5],
                    }
                )

        quote = identity.get("supplier_quote") or identity.get("stored_quote")
        if isinstance(quote, dict) and quote.get("unit_price"):
            try:
                candidates.append(
                    {
                        "unit_price": float(quote["unit_price"]),
                        "source_url": quote.get("source_url"),
                        "seller": quote.get("seller"),
                        "basis": SUPPLIER_QUOTE,
                        "exact_match": True,
                        "confidence": "A",
                        "via": "stored_quote",
                    }
                )
            except (TypeError, ValueError):
                pass

        if not candidates and use_openai_fallback:
            sources.append("openai_web_search")
            for cand in _try_openai_web_price(identity, str(oid)):
                candidates.append(cand)
    finally:
        if own:
            c.close()

    result["sources_attempted"] = list(dict.fromkeys(sources))
    result["queries_attempted"] = queries

    if not candidates:
        if retryable:
            result["status"] = RETRYABLE_FAILURE
            result["failure_reason"] = RETRYABLE_SOURCE_FAILURE
            result["match_type"] = NO_PUBLIC_PRICE
            result["stop_reason"] = RETRYABLE_FAILURE
            return result
        result["status"] = NOT_FOUND_AFTER_EXHAUSTIVE_SEARCH
        result["failure_reason"] = NO_PUBLIC_PRICE_FR
        result["match_type"] = NO_PUBLIC_PRICE
        result["stop_reason"] = NOT_FOUND_AFTER_EXHAUSTIVE_SEARCH
        return result

    # Deterministic choice: exact > distributor > reseller; then lowest credible price
    def _rank(c: dict[str, Any]) -> tuple:
        basis = c.get("basis") or ""
        basis_rank = {
            PUBLIC_DISTRIBUTOR_EXACT: 0,
            PUBLIC_RETAIL_EXACT: 1,
            PUBLIC_RESELLER_EXACT: 2,
            SUPPLIER_QUOTE: 0,
            PUBLIC_ALLOWED_EQUAL: 3,
        }.get(basis, 4)
        exact = 0 if c.get("exact_match") else 1
        return (exact, basis_rank, float(c["unit_price"]))

    candidates.sort(key=_rank)
    # Ambiguity: many wildly different prices without exact
    prices = [float(c["unit_price"]) for c in candidates]
    if len(prices) >= 3 and (max(prices) / max(min(prices), 0.01)) > 8 and not any(c.get("exact_match") for c in candidates[:3]):
        result["status"] = AMBIGUOUS_MATCH
        result["failure_reason"] = AMBIGUOUS_MATCH
        result["match_type"] = NO_PUBLIC_PRICE
        result["stop_reason"] = AMBIGUOUS_MATCH
        result["best_match"] = candidates[0]
        return result

    best = candidates[0]
    result.update(
        {
            "status": FOUND,
            "match_type": best.get("basis") or PUBLIC_RETAIL_EXACT,
            "confidence": best.get("confidence") or "B",
            "source": best.get("seller") or best.get("via"),
            "evidence": {
                "unit_price": best["unit_price"],
                "uom": identity.get("uom_normalized") or identity.get("uom") or "EA",
                "seller": best.get("seller"),
                "source_url": best.get("source_url"),
                "basis": best.get("basis"),
                "exact_match": best.get("exact_match"),
                "pack_size": identity.get("pack_size"),
                "availability": best.get("availability"),
                "retrieved_via": best.get("via"),
                "alternate_prices": [
                    {"unit_price": c["unit_price"], "seller": c.get("seller"), "url": c.get("source_url")}
                    for c in candidates[1:4]
                ],
            },
            "provenance": [
                {"route": best.get("via"), "url": best.get("source_url"), "basis": best.get("basis")}
            ],
            "best_match": best,
            "stop_reason": "PUBLIC_PRICE_FOUND",
            "failure_reason": None,
        }
    )
    return result
