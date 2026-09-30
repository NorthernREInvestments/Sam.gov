"""Phase L.2.8 — product-detail resolution from search/category shells + bot-block fallbacks.

Legal public-data recovery only. No CAPTCHA bypass, credential theft, or anti-bot evasion.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote_plus

from application_clock import now_utc
from phase_l.acquisition_pricing import (
    FAMILY_SOURCES,
    FETCH_BLOCKED,
    PriceLead,
    UNVERIFIED_LEAD,
    _lead_from_snippet,
    acquisition_range,
    direct_source_targets,
    infer_product_family,
    memory_key,
    recall_price,
    record_source_outcome,
    remember_price,
    _search_id_from,
)
from phase_l.convergence import PUBLIC_CATALOG, PUBLIC_RETAIL, classify_price_access
from phase_l.market_price import _domain_search_url
from phase_l.pricing_sources import extract_pdf_text, parse_pdf_price_rows, parse_spreadsheet_prices
from phase_l.product_page_resolution import (
    CATEGORY_PAGE,
    EXACT_PRODUCT_PAGE,
    EXACT_VERIFIED,
    HOMEPAGE,
    LIKELY_PRODUCT_PAGE,
    PDF_PRICE_LIST,
    SEARCH_RESULTS_PAGE,
    STRONG_VERIFIED,
    UNRELATED,
    classify_page_type,
    extract_product_candidate_links,
    normalize_url,
    resolve_and_verify_market_price,
)
from phase_l.resilient_fetch import (
    FETCH_403,
    FETCH_429,
    FETCH_BOT_BLOCKED,
    FETCH_JS_EMPTY,
    FETCH_OK,
    FETCH_TIMEOUT,
    DomainCircuitBreaker,
    domain_of,
    resilient_fetch,
)

MAX_PRODUCT_RESOLUTION_DEPTH = 3

# L.2.8 page classes
EXACT_PRODUCT_DETAIL = "EXACT_PRODUCT_DETAIL"
STRONG_PRODUCT_DETAIL = "STRONG_PRODUCT_DETAIL"
PRODUCT_FAMILY_PAGE = "PRODUCT_FAMILY_PAGE"
SEARCH_SHELL = "SEARCH_SHELL"
CATEGORY_SHELL = "CATEGORY_SHELL"
STATIC_PRICE_ARTIFACT = "STATIC_PRICE_ARTIFACT"
PAGE_BLOCKED = "BLOCKED"
PAGE_JS_EMPTY = "JS_EMPTY"
PAGE_UNRELATED = "UNRELATED"

# Freshness
CURRENT_30D = "CURRENT_30D"
CURRENT_90D = "CURRENT_90D"
STALE_1Y = "STALE_1Y"
HISTORICAL_ONLY = "HISTORICAL_ONLY"
UNKNOWN_DATE = "UNKNOWN_DATE"

PROMISING_REQUIRES_PRICE_VERIFICATION = "PROMISING_REQUIRES_PRICE_VERIFICATION"

_REJECT_LINK = re.compile(
    r"/(cart|checkout|login|signin|account|help|support|wishlist|compare|blog|news|"
    r"privacy|terms|about|contact)(/|$|\?)",
    re.I,
)
_ACCESSORY = re.compile(
    r"\b(accessor(y|ies)|compatible\s+with|replacement\s+only|warranty|cable|case|"
    r"mount|adapter\s+kit|refurb|used|open[\-\s]?box)\b",
    re.I,
)
_STATIC_EXT = re.compile(r"\.(pdf|xlsx|xls|csv)(\?|$)", re.I)
_STATIC_HINT = re.compile(
    r"(price[\s_\-]?list|price[\s_\-]?book|catalog|schedule|bid[\s_\-]?tab|"
    r"contract[\s_\-]?price|msrp|inventory)",
    re.I,
)

# Domain-specific alternate sellers when primary blocks (public sources only)
DOMAIN_FALLBACKS: dict[str, list[str]] = {
    "ford.com": ["sourcewell-mn.gov", "naspovaluepoint.org"],
    "bobcat.com": ["sourcewell-mn.gov", "machinerytrader.com", "grainger.com"],
    "grainger.com": ["zoro.com", "mscdirect.com", "fastenal.com"],
    "zoro.com": ["grainger.com", "mscdirect.com"],
    "fastenal.com": ["grainger.com", "mscdirect.com", "mouser.com"],
    "digikey.com": ["mouser.com", "newark.com", "alliedelec.com"],
    "mouser.com": ["digikey.com", "newark.com"],
    "dell.com": ["cdw.com", "shi.com", "insight.com", "connection.com"],
    "cdw.com": ["shi.com", "insight.com", "newegg.com"],
}


def _utc() -> str:
    return now_utc().isoformat()


def _norm(s: str | None) -> str:
    return re.sub(r"[^A-Z0-9]+", "", str(s or "").upper())


def classify_detail_page(url: str, html: str | None = None, *, fetch_status: str | None = None) -> str:
    if fetch_status in {FETCH_403, FETCH_429, FETCH_BOT_BLOCKED}:
        return PAGE_BLOCKED
    if fetch_status == FETCH_JS_EMPTY:
        return PAGE_JS_EMPTY
    if _STATIC_EXT.search(url or "") or (html and html[:4] == "%PDF"):
        return STATIC_PRICE_ARTIFACT
    pt = classify_page_type(url, html)
    if pt == EXACT_PRODUCT_PAGE:
        return EXACT_PRODUCT_DETAIL
    if pt == LIKELY_PRODUCT_PAGE:
        return STRONG_PRODUCT_DETAIL
    if pt == SEARCH_RESULTS_PAGE:
        return SEARCH_SHELL
    if pt == CATEGORY_PAGE:
        return CATEGORY_SHELL
    if pt == HOMEPAGE:
        return SEARCH_SHELL
    if pt in {PDF_PRICE_LIST}:
        return STATIC_PRICE_ARTIFACT
    if pt == UNRELATED:
        return PAGE_UNRELATED
    return STRONG_PRODUCT_DETAIL if pt == LIKELY_PRODUCT_PAGE else PAGE_UNRELATED


def classify_price_freshness(evidence_text: str | None = None, *, page_html: str | None = None) -> str:
    blob = f"{evidence_text or ''} {(page_html or '')[:5000]}"
    if re.search(r"\b(today|in\s+stock|updated\s+today|as\s+of\s+today)\b", blob, re.I):
        return CURRENT_30D
    if re.search(r"\b(20(2[4-6]))[-/](0?[1-9]|1[0-2])\b", blob):
        return CURRENT_90D
    if re.search(r"\b(20(1[9-9]|2[0-2]))\b", blob):
        return STALE_1Y
    if re.search(r"\b(historical|archive|discontinued)\b", blob, re.I):
        return HISTORICAL_ONLY
    return UNKNOWN_DATE


def extract_static_artifact_links(html: str, *, base_url: str, search_id: dict[str, Any], limit: int = 8) -> list[dict[str, Any]]:
    if not html:
        return []
    keys = [_norm(k) for k in (search_id.get("primary_mpn"), search_id.get("model"), search_id.get("sku")) if k]
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for m in re.finditer(r'href=["\']([^"\']+\.(?:pdf|xlsx|xls|csv)[^"\']*)["\']', html, re.I):
        u = normalize_url(m.group(1), base=base_url)
        if not u or u in seen:
            continue
        seen.add(u)
        score = 10
        low = u.lower()
        if _STATIC_HINT.search(low):
            score += 30
        ctx_start = max(0, m.start() - 80)
        ctx = html[ctx_start : m.end() + 80]
        if keys and any(k and k in _norm(ctx + low) for k in keys):
            score += 40
        out.append({"url": u, "score": score, "kind": "static_artifact"})
    out.sort(key=lambda x: -x["score"])
    return out[:limit]


_HARD_ACCESSORY = re.compile(
    r"\b(compatible[\-\s]?with|replacement[\-\s]?only|accessor(y|ies)|refurb|used|open[\-\s]?box)\b",
    re.I,
)
_SHELL_URL = re.compile(
    r"(/search\b|/browse\b|/p/pl\b|/c/search|filetype=pdf|/rss|/Product/RSS)",
    re.I,
)
_DETAIL_URL = re.compile(
    r"/(product|products|dp|ip|item|sku|part)/|/p/(?!pl\b)[A-Z0-9][A-Z0-9\-_]{4,}",
    re.I,
)


def rank_product_links(
    links: list[dict[str, Any]],
    *,
    search_id: dict[str, Any],
) -> list[dict[str, Any]]:
    """Re-rank / filter accessory and weak links."""
    keys = []
    for k in (search_id.get("primary_mpn"), search_id.get("model"), search_id.get("sku"), *(search_id.get("mpn_variants") or [])[:3]):
        n = _norm(k)
        if n and n not in keys:
            keys.append(n)
    ranked = []
    for link in links:
        u = link.get("url") or ""
        low = u.lower()
        if _REJECT_LINK.search(low):
            continue
        if _HARD_ACCESSORY.search(low):
            continue
        if "rss" in low or "><title>" in low or "javascript:" in low:
            continue
        score = float(link.get("score") or 0)
        if _SHELL_URL.search(low) and not _DETAIL_URL.search(low):
            score -= 80
            # Search/category shells should not outrank product SKUs
            if "/p/pl" in low or "/search" in low or "/browse" in low:
                continue
        if _DETAIL_URL.search(low):
            score += 35
        if _ACCESSORY.search(low):
            # soft accessory path words — keep only with exact key in URL
            if not keys or not any(k in _norm(low) for k in keys):
                continue
            score -= 40
        for k in keys:
            if k and k in _norm(low):
                score += 40
                break
        if score <= 0:
            continue
        ranked.append({**link, "score": score})
    ranked.sort(key=lambda x: -x["score"])
    return ranked


def synthesize_product_urls(search_id: dict[str, Any], *, family: str) -> list[dict[str, Any]]:
    """Identity-guided direct product URL candidates when shells are sterile."""
    key = search_id.get("primary_mpn") or search_id.get("sku") or search_id.get("model")
    if not key:
        return []
    q = quote_plus(str(key))
    raw = str(key).strip()
    slug = re.sub(r"[^A-Za-z0-9\-]+", "-", raw).strip("-").lower()
    candidates: list[tuple[str, str, float]] = []
    # Public catalog patterns (no auth bypass)
    candidates.append((f"https://www.mcmaster.com/{quote_plus(raw)}/", "mcmaster.com", 20))
    candidates.append((f"https://www.digikey.com/en/products/result?keywords={q}", "digikey.com", 15))
    candidates.append((f"https://www.mouser.com/c/?q={q}", "mouser.com", 15))
    candidates.append((f"https://www.grainger.com/search?searchQuery={q}", "grainger.com", 8))
    candidates.append((f"https://www.newegg.com/p/pl?d={q}", "newegg.com", 12))
    if family == "IT":
        candidates.append((f"https://www.cdw.com/search/?key={q}", "cdw.com", 12))
        candidates.append((f"https://www.bhphotovideo.com/c/search?Ntt={q}", "bhphotovideo.com", 12))
    if family in {"EQUIPMENT", "VEHICLE"}:
        candidates.append((f"https://www.machinerytrader.com/list/search?keywords={q}", "machinerytrader.com", 10))
    if family == "EQUIPMENT" and slug:
        candidates.append((f"https://www.bobcat.com/na/en/equipment/{slug}", "bobcat.com", 18))
    out = []
    seen: set[str] = set()
    for url, domain, score in sorted(candidates, key=lambda x: -x[2]):
        if url in seen:
            continue
        seen.add(url)
        out.append({"url": url, "domain": domain, "score": score, "source": "synthesized", "key": raw})
    return out[:8]


def discover_indexed_price_leads(
    search_id: dict[str, Any],
    *,
    limit: int = 4,
) -> list[PriceLead]:
    """Public indexed/snippet evidence only — UNVERIFIED / PRICE_LEAD_ONLY."""
    from phase_l.market_price import search_urls_with_fallback

    key = search_id.get("primary_mpn") or search_id.get("model") or search_id.get("sku")
    if not key:
        return []
    mfr = search_id.get("manufacturer") or ""
    query = f"{mfr} {key} price".strip()
    try:
        ser = search_urls_with_fallback(query, limit=limit)
    except Exception:
        return []
    leads: list[PriceLead] = []
    for u in (ser.get("urls") or [])[:limit]:
        # snippet may be absent; still record URL as discovery if identity in path
        blob = f"{u} {query}"
        lead = _lead_from_snippet(
            search_id=search_id,
            url=u,
            source_type="INDEXED_SNIPPET",
            text=blob,
            fetch_status=FETCH_OK,
        )
        if lead:
            lead.confidence = "MEDIUM"
            lead.verification_status = UNVERIFIED_LEAD
            lead.economics_eligible = False
            leads.append(lead)
    # Attempt DDG HTML snippets when available (public index text)
    try:
        import httpx

        ddg = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
        resp = httpx.get(
            ddg,
            timeout=8.0,
            headers={"User-Agent": "Mozilla/5.0 (compatible; GovTrackerResearch/1.0)"},
            follow_redirects=True,
        )
        if resp.status_code < 400 and resp.text:
            for m in re.finditer(
                r"(?:result__snippet|result__a)[^>]*>(.*?)</(?:a|td|div)",
                resp.text,
                re.I | re.S,
            ):
                snip = re.sub(r"<[^>]+>", " ", m.group(1))
                snip = re.sub(r"\s+", " ", snip).strip()
                if len(snip) < 20:
                    continue
                lead = _lead_from_snippet(
                    search_id=search_id,
                    url=ddg,
                    source_type="INDEXED_SNIPPET",
                    text=snip,
                    fetch_status=FETCH_OK,
                )
                if lead:
                    lead.confidence = "HIGH" if lead.apparent_price and lead.observed_identity else "MEDIUM"
                    lead.verification_status = UNVERIFIED_LEAD
                    lead.economics_eligible = False
                    leads.append(lead)
                if len(leads) >= limit:
                    break
    except Exception:
        pass
    return leads[:limit]


def is_sterile_shell(html: str, *, search_id: dict[str, Any]) -> bool:
    """True when fetch succeeded but page has no usable product/identity signal."""
    if not html or len(html) < 400:
        return True
    href_n = html.lower().count("href=")
    if href_n <= 3:
        return True
    keys = [_norm(k) for k in (search_id.get("primary_mpn"), search_id.get("model"), search_id.get("sku")) if k]
    blob = _norm(html[:60000])
    if keys and not any(k and k in blob for k in keys):
        # No identity tokens and few product paths → sterile for our purposes
        if not re.search(r"/(product|products|/p/[A-Z0-9])", html, re.I):
            return True
    return False


def _fallback_key(domain: str) -> str:
    d = (domain or "").lower().replace("www.", "")
    for key in DOMAIN_FALLBACKS:
        if d == key or d.endswith("." + key):
            return key
    return d


def alternate_seller_targets(
    *,
    blocked_domain: str,
    search_id: dict[str, Any],
    family: str,
) -> list[dict[str, Any]]:
    """Next public sellers when a domain is blocked — no aggressive retries."""
    blocked = _fallback_key(blocked_domain)
    alts = list(DOMAIN_FALLBACKS.get(blocked, []) or [])
    for domain, _stype in FAMILY_SOURCES.get(family) or FAMILY_SOURCES["MRO"]:
        d = domain.replace("www.", "")
        if d == blocked or d.endswith("." + blocked) or blocked.endswith("." + d):
            continue
        if domain not in alts:
            alts.append(domain)
    key = search_id.get("primary_mpn") or search_id.get("model") or search_id.get("sku")
    if not key:
        return []
    out = []
    for domain in alts[:6]:
        u = _domain_search_url(domain, str(key)) or f"https://www.{domain}/search?q={quote_plus(str(key))}"
        out.append({"url": u, "domain": domain, "source_type": "FALLBACK_SELLER", "key": key})
    return out


def resolve_product_detail(
    *,
    start_url: str,
    html: str,
    search_id: dict[str, Any],
    row: dict[str, Any],
    breaker: DomainCircuitBreaker,
    max_depth: int = MAX_PRODUCT_RESOLUTION_DEPTH,
    max_detail_fetches: int = 4,
) -> dict[str, Any]:
    """
    Shell → candidate links → fetch top details (depth-limited).
    Returns verified evidence / leads / classification.
    """
    tele = {
        "shells": 0,
        "candidate_links": 0,
        "detail_fetches": 0,
        "detail_ok": 0,
        "static_artifacts": 0,
        "exact_details": 0,
        "blocked": 0,
        "depth_used": 0,
    }
    page_class = classify_detail_page(start_url, html)
    leads: list[PriceLead] = []
    verified: list[dict[str, Any]] = []
    usable: list[dict[str, Any]] = []
    visited: set[str] = {start_url}
    queue: list[tuple[str, str, int]] = []  # url, html, depth
    detail_fetches = 0

    # Immediate snippet lead from shell
    shell_lead = _lead_from_snippet(
        search_id=search_id,
        url=start_url,
        source_type="SEARCH_SHELL",
        text=html or "",
        fetch_status=FETCH_OK,
    )
    if shell_lead:
        leads.append(shell_lead)

    # Detail page: process immediately. Shell/sterile: extract or synthesize then descend.
    if page_class in {EXACT_PRODUCT_DETAIL, STRONG_PRODUCT_DETAIL, STATIC_PRICE_ARTIFACT}:
        queue.append((start_url, html, 0))
    else:
        tele["shells"] += 1
        links0 = extract_product_candidate_links(html or "", base_url=start_url, search_id=search_id, limit=10)
        links0 = rank_product_links(links0, search_id=search_id)
        tele["candidate_links"] += len(links0)
        seed_urls: list[tuple[str, int]] = [(L["url"], 1) for L in links0[:4]]
        if not seed_urls or is_sterile_shell(html, search_id=search_id):
            family = infer_product_family(row, None)
            for syn in synthesize_product_urls(search_id, family=family)[:3]:
                seed_urls.append((syn["url"], 1))
                tele["candidate_links"] += 1
            for lead in discover_indexed_price_leads(search_id, limit=3):
                leads.append(lead)
        # Also enqueue start page for static-artifact discovery
        queue.append((start_url, html, 0))
        for nu, depth in seed_urls:
            if nu in visited or detail_fetches >= max_detail_fetches:
                continue
            visited.add(nu)
            if not breaker.allow(nu):
                continue
            fr = resilient_fetch(nu, breaker=breaker, retries=0)
            detail_fetches += 1
            tele["detail_fetches"] += 1
            if fr.status in {FETCH_403, FETCH_429, FETCH_BOT_BLOCKED, FETCH_JS_EMPTY, FETCH_TIMEOUT}:
                tele["blocked"] += 1
                lead = _lead_from_snippet(
                    search_id=search_id,
                    url=nu,
                    source_type="PRODUCT_DETAIL",
                    text=fr.text or nu,
                    fetch_status=fr.status,
                )
                if lead:
                    leads.append(lead)
                continue
            if fr.status != FETCH_OK:
                continue
            tele["detail_ok"] += 1
            queue.append((nu, fr.text, depth))

    while queue and detail_fetches < max_detail_fetches:
        url, page_html, depth = queue.pop(0)
        tele["depth_used"] = max(tele["depth_used"], depth)
        pclass = classify_detail_page(url, page_html)
        if pclass in {SEARCH_SHELL, CATEGORY_SHELL, PRODUCT_FAMILY_PAGE}:
            tele["shells"] += 1

        # Static artifacts first
        statics = extract_static_artifact_links(page_html, base_url=url, search_id=search_id)
        tele["static_artifacts"] += len(statics)
        for art in statics[:2]:
            if art["url"] in visited or detail_fetches >= max_detail_fetches:
                continue
            visited.add(art["url"])
            if not breaker.allow(art["url"]):
                continue
            fr = resilient_fetch(art["url"], breaker=breaker, retries=0)
            detail_fetches += 1
            tele["detail_fetches"] += 1
            if fr.status != FETCH_OK:
                if fr.status in {FETCH_403, FETCH_429, FETCH_BOT_BLOCKED}:
                    tele["blocked"] += 1
                continue
            tele["detail_ok"] += 1
            if fr.content[:4] == b"%PDF" or art["url"].lower().endswith(".pdf"):
                text = extract_pdf_text(fr.content)
                for rec in parse_pdf_price_rows(text, search_id=search_id, source_url=art["url"]):
                    if not rec.price:
                        continue
                    tele["exact_details"] += 1
                    access = classify_price_access(
                        price_type=rec.acquisition_price_type or PUBLIC_CATALOG,
                        url=art["url"],
                        evidence_text=rec.evidence_text,
                    )
                    fresh = classify_price_freshness(rec.evidence_text, page_html=text[:3000])
                    item = {
                        "verified_price": rec.price,
                        "source_url": art["url"],
                        "seller": domain_of(art["url"]),
                        "price_type": rec.acquisition_price_type or PUBLIC_CATALOG,
                        "confidence": EXACT_VERIFIED,
                        "economics_eligible": bool(access.get("economics_eligible")) and fresh in {CURRENT_30D, CURRENT_90D, UNKNOWN_DATE},
                        "price_access": access.get("price_access"),
                        "evidence_text": rec.evidence_text,
                        "page_class": STATIC_PRICE_ARTIFACT,
                        "freshness": fresh,
                    }
                    verified.append(item)
                    if item["economics_eligible"]:
                        usable.append(item)
            elif art["url"].lower().endswith((".xlsx", ".csv", ".xls")):
                for rec in parse_spreadsheet_prices(fr.content, search_id=search_id, source_url=art["url"], filename_hint=art["url"]):
                    if not rec.price:
                        continue
                    tele["exact_details"] += 1
                    access = classify_price_access(
                        price_type=rec.acquisition_price_type or PUBLIC_CATALOG,
                        url=art["url"],
                        evidence_text=rec.evidence_text,
                    )
                    item = {
                        "verified_price": rec.price,
                        "source_url": art["url"],
                        "seller": domain_of(art["url"]),
                        "price_type": rec.acquisition_price_type or PUBLIC_CATALOG,
                        "confidence": EXACT_VERIFIED,
                        "economics_eligible": bool(access.get("economics_eligible")),
                        "price_access": access.get("price_access"),
                        "evidence_text": rec.evidence_text,
                        "page_class": STATIC_PRICE_ARTIFACT,
                        "freshness": UNKNOWN_DATE,
                    }
                    verified.append(item)
                    if item["economics_eligible"]:
                        usable.append(item)
            if usable:
                break
        if usable:
            break

        # If already a detail page, verify
        if pclass in {EXACT_PRODUCT_DETAIL, STRONG_PRODUCT_DETAIL} or (
            depth > 0 and classify_page_type(url, page_html) in {EXACT_PRODUCT_PAGE, LIKELY_PRODUCT_PAGE}
        ):
            resolved = resolve_and_verify_market_price(html=page_html, url=url, search_id=search_id, row=row)
            if not resolved.get("drop_reason") or resolved.get("evidence"):
                for ev in resolved.get("evidence") or []:
                    price = ev.get("price") or ev.get("unit_price")
                    if not price:
                        continue
                    if ev.get("confidence") not in {EXACT_VERIFIED, STRONG_VERIFIED} and not ev.get("economics_eligible"):
                        leads.append(
                            PriceLead(
                                target_identity=search_id.get("model") or search_id.get("primary_mpn"),
                                apparent_price=float(price),
                                source_url=url,
                                source_type="PRODUCT_DETAIL",
                                evidence_text=(ev.get("evidence_text") or "")[:240],
                                confidence="MEDIUM",
                                verification_status=UNVERIFIED_LEAD,
                                economics_eligible=False,
                            )
                        )
                        continue
                    tele["exact_details"] += 1
                    fresh = classify_price_freshness(ev.get("evidence_text"), page_html=page_html)
                    access = classify_price_access(
                        price_type=ev.get("acquisition_price_type") or PUBLIC_RETAIL,
                        url=url,
                        evidence_text=ev.get("evidence_text"),
                    )
                    item = {
                        "verified_price": float(price),
                        "source_url": url,
                        "seller": domain_of(url),
                        "price_type": ev.get("acquisition_price_type") or PUBLIC_RETAIL,
                        "confidence": ev.get("confidence") or STRONG_VERIFIED,
                        "economics_eligible": bool(ev.get("economics_eligible", True))
                        and access["economics_eligible"]
                        and fresh not in {HISTORICAL_ONLY, STALE_1Y},
                        "price_access": access.get("price_access"),
                        "evidence_text": (ev.get("evidence_text") or "")[:240],
                        "page_class": EXACT_PRODUCT_DETAIL if ev.get("confidence") == EXACT_VERIFIED else STRONG_PRODUCT_DETAIL,
                        "freshness": fresh,
                    }
                    verified.append(item)
                    if item["economics_eligible"]:
                        usable.append(item)
                if usable:
                    break
            elif resolved.get("drop_reason") == "NO_PRICE_ON_MATCHING_PAGE":
                tele["exact_details"] += 1  # identity matched but no price

        # Extract product links and descend
        if depth >= max_depth:
            continue
        links = extract_product_candidate_links(page_html, base_url=url, search_id=search_id, limit=10)
        links = rank_product_links(links, search_id=search_id)
        tele["candidate_links"] += len(links)
        for link in links[:4]:
            nu = link["url"]
            if nu in visited or detail_fetches >= max_detail_fetches:
                continue
            visited.add(nu)
            if not breaker.allow(nu):
                continue
            fr = resilient_fetch(nu, breaker=breaker, retries=0)
            detail_fetches += 1
            tele["detail_fetches"] += 1
            if fr.status in {FETCH_403, FETCH_429, FETCH_BOT_BLOCKED, FETCH_JS_EMPTY, FETCH_TIMEOUT}:
                tele["blocked"] += 1
                lead = _lead_from_snippet(
                    search_id=search_id,
                    url=nu,
                    source_type="PRODUCT_DETAIL",
                    text=fr.text or link.get("url", ""),
                    fetch_status=fr.status,
                )
                if lead:
                    # Also try URL+title as lead if price in nearby from shell
                    leads.append(lead)
                continue
            if fr.status != FETCH_OK:
                continue
            tele["detail_ok"] += 1
            queue.append((nu, fr.text, depth + 1))

    return {
        "kind": "PhaseL28ProductDetailResolution",
        "start_url": start_url,
        "start_page_class": page_class,
        "leads": [L.to_dict() if hasattr(L, "to_dict") else L for L in leads],
        "verified": verified,
        "usable": usable,
        "telemetry": tele,
        "visited_n": len(visited),
    }


def research_with_detail_resolution(
    *,
    row: dict[str, Any],
    commercial: dict[str, Any],
    identity: dict[str, Any],
    breaker: DomainCircuitBreaker,
    learning: dict[str, Any],
    memory: dict[str, Any],
    max_shell_fetches: int = 4,
    max_detail_fetches: int = 4,
) -> dict[str, Any]:
    """L.2.8 entry: direct sources → shell follow-through → bot-block alternates."""
    from phase_l.acquisition_pricing import (
        PRICE_LEAD_FETCH_BLOCKED,
        STRONG_PRICE_LEAD_REQUIRES_VERIFICATION,
    )

    search_id = _search_id_from(commercial, identity, row)
    family = infer_product_family(row, commercial)
    key = memory_key(search_id)
    tele_tot = {
        "shells": 0,
        "candidate_links": 0,
        "product_detail_pages": 0,
        "static_artifacts": 0,
        "blocked_domains": 0,
        "alternate_attempts": 0,
        "fetches": 0,
        "fetch_ok": 0,
        "leads": 0,
        "strong_leads": 0,
        "verified_prices": 0,
        "usable_prices": 0,
        "exact_product_pages": 0,
    }

    cached = recall_price(memory, key)
    if cached and cached.get("verified_price"):
        return {
            "kind": "PhaseL28AcquisitionResearch",
            "family": family,
            "cache_hit": True,
            "leads": [],
            "verified": [cached],
            "usable": [cached] if cached.get("economics_eligible") else [],
            "acquisition_range": acquisition_range([cached["verified_price"]]),
            "telemetry": tele_tot,
            "manual_fallback": False,
            "detail_resolutions": 1,
        }

    targets = direct_source_targets(search_id=search_id, family=family, learning=learning)[:max_shell_fetches]
    all_leads: list[dict[str, Any]] = []
    all_verified: list[dict[str, Any]] = []
    all_usable: list[dict[str, Any]] = []
    blocked_domains: set[str] = set()
    detail_resolutions = 0
    fetch_log: list[dict[str, Any]] = []

    def _consume_resolution(res: dict[str, Any]) -> None:
        nonlocal detail_resolutions
        detail_resolutions += 1
        t = res.get("telemetry") or {}
        tele_tot["shells"] += int(t.get("shells") or 0)
        tele_tot["candidate_links"] += int(t.get("candidate_links") or 0)
        tele_tot["static_artifacts"] += int(t.get("static_artifacts") or 0)
        tele_tot["fetches"] += int(t.get("detail_fetches") or 0)
        tele_tot["fetch_ok"] += int(t.get("detail_ok") or 0)
        tele_tot["exact_product_pages"] += int(t.get("exact_details") or 0)
        tele_tot["product_detail_pages"] += int(t.get("exact_details") or 0)
        tele_tot["blocked_domains"] += int(t.get("blocked") or 0)
        for L in res.get("leads") or []:
            ld = L if isinstance(L, dict) else L.to_dict()
            all_leads.append(ld)
            tele_tot["leads"] += 1
            if ld.get("confidence") == "HIGH":
                tele_tot["strong_leads"] += 1
        for v in res.get("verified") or []:
            all_verified.append(v)
            tele_tot["verified_prices"] += 1
        for u in res.get("usable") or []:
            all_usable.append(u)
            tele_tot["usable_prices"] += 1

    for t in targets:
        url = t["url"]
        if not breaker.allow(url):
            continue
        fr = resilient_fetch(url, breaker=breaker, retries=0)
        tele_tot["fetches"] += 1
        fetch_log.append(fr.to_dict())
        if fr.status in {FETCH_403, FETCH_429, FETCH_BOT_BLOCKED, FETCH_JS_EMPTY}:
            blocked_domains.add(fr.domain)
            tele_tot["blocked_domains"] += 1
            lead = _lead_from_snippet(
                search_id=search_id,
                url=url,
                source_type=t.get("source_type") or "WEB",
                text=fr.text or "",
                fetch_status=fr.status,
            )
            if lead:
                all_leads.append(lead.to_dict())
                tele_tot["leads"] += 1
                if lead.confidence == "HIGH":
                    tele_tot["strong_leads"] += 1
            for alt in alternate_seller_targets(blocked_domain=fr.domain, search_id=search_id, family=family)[:3]:
                tele_tot["alternate_attempts"] += 1
                if not breaker.allow(alt["url"]):
                    continue
                afr = resilient_fetch(alt["url"], breaker=breaker, retries=0)
                tele_tot["fetches"] += 1
                fetch_log.append(afr.to_dict())
                if afr.status != FETCH_OK:
                    continue
                tele_tot["fetch_ok"] += 1
                res = resolve_product_detail(
                    start_url=alt["url"],
                    html=afr.text,
                    search_id=search_id,
                    row=row,
                    breaker=breaker,
                    max_detail_fetches=max_detail_fetches,
                )
                _consume_resolution(res)
                if all_usable:
                    break
            record_source_outcome(
                learning, family=family, domain=fr.domain, verified=False, usable=False, blocked=True, identity_hit=False
            )
            if all_usable:
                break
            continue

        if fr.status != FETCH_OK:
            continue
        # Soft-block / sterile shell → alternate recovery without hammering
        if is_sterile_shell(fr.text, search_id=search_id) or classify_detail_page(
            url, fr.text, fetch_status=fr.status
        ) in {PAGE_BLOCKED, PAGE_JS_EMPTY}:
            blocked_domains.add(fr.domain)
            tele_tot["blocked_domains"] += 1
            for lead in discover_indexed_price_leads(search_id, limit=2):
                all_leads.append(lead.to_dict())
                tele_tot["leads"] += 1
                if lead.confidence == "HIGH":
                    tele_tot["strong_leads"] += 1
            for alt in alternate_seller_targets(blocked_domain=fr.domain, search_id=search_id, family=family)[:3]:
                tele_tot["alternate_attempts"] += 1
                if not breaker.allow(alt["url"]):
                    continue
                afr = resilient_fetch(alt["url"], breaker=breaker, retries=0)
                tele_tot["fetches"] += 1
                fetch_log.append(afr.to_dict())
                if afr.status != FETCH_OK:
                    continue
                tele_tot["fetch_ok"] += 1
                res = resolve_product_detail(
                    start_url=alt["url"],
                    html=afr.text,
                    search_id=search_id,
                    row=row,
                    breaker=breaker,
                    max_detail_fetches=max_detail_fetches,
                )
                _consume_resolution(res)
                if all_usable:
                    break
            record_source_outcome(
                learning, family=family, domain=fr.domain, verified=False, usable=False, blocked=True, identity_hit=False
            )
            if all_usable:
                break
            # Still attempt resolution on the sterile page (static/synth path)
            tele_tot["fetch_ok"] += 1
            res = resolve_product_detail(
                start_url=url,
                html=fr.text,
                search_id=search_id,
                row=row,
                breaker=breaker,
                max_detail_fetches=max_detail_fetches,
            )
            _consume_resolution(res)
            if all_usable:
                break
            continue

        tele_tot["fetch_ok"] += 1
        res = resolve_product_detail(
            start_url=url,
            html=fr.text,
            search_id=search_id,
            row=row,
            breaker=breaker,
            max_detail_fetches=max_detail_fetches,
        )
        _consume_resolution(res)
        record_source_outcome(
            learning,
            family=family,
            domain=fr.domain,
            verified=bool(all_verified),
            usable=bool(all_usable),
            blocked=False,
            identity_hit=bool(tele_tot["exact_product_pages"]),
        )
        if all_usable:
            break

    if not all_leads and not all_verified:
        for lead in discover_indexed_price_leads(search_id, limit=3):
            all_leads.append(lead.to_dict())
            tele_tot["leads"] += 1
            if lead.confidence == "HIGH":
                tele_tot["strong_leads"] += 1
            tele_tot["alternate_attempts"] += 1

    strong_blocked = [
        L for L in all_leads if L.get("verification_status") == FETCH_BLOCKED and L.get("confidence") == "HIGH"
    ]
    prices = [v["verified_price"] for v in all_verified]
    lead_prices = [L["apparent_price"] for L in all_leads if L.get("apparent_price") and L.get("confidence") == "HIGH"]
    arange = acquisition_range(prices) if prices else acquisition_range(lead_prices)

    if all_usable:
        best = all_usable[0]
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
    if not all_verified and not all_leads:
        primary_failure = "NO_PRICE_INFORMATION"
    elif not all_verified and strong_blocked:
        primary_failure = PRICE_LEAD_FETCH_BLOCKED
    elif all_leads and not all_verified:
        primary_failure = STRONG_PRICE_LEAD_REQUIRES_VERIFICATION

    return {
        "kind": "PhaseL28AcquisitionResearch",
        "family": family,
        "cache_hit": False,
        "leads": all_leads,
        "verified": all_verified,
        "usable": all_usable,
        "acquisition_range": arange,
        "recon_range_includes_leads": bool(lead_prices and not prices),
        "failure_class": primary_failure,
        "telemetry": tele_tot,
        "fetch_log": fetch_log[-30:],
        "manual_fallback": bool(strong_blocked),
        "strong_price_lead_requires_verification": bool(strong_blocked or (all_leads and not all_usable)),
        "detail_resolutions": detail_resolutions,
        "blocked_domains": sorted(blocked_domains),
        "promising_requires_verification": bool(all_leads and not all_usable),
    }
