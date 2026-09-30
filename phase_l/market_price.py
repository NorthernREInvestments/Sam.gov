"""Phase L.2.1 market-price rescue — MPN expansion, search fallback, extraction.

Reuses m3_public_pricing_evidence.fetch_public_text; does not invent prices.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote_plus, urlparse

from application_clock import now_utc

# Confidence states
EXACT_PUBLIC_PRICE_HIGH = "EXACT_PUBLIC_PRICE_HIGH"
EXACT_PUBLIC_PRICE_MEDIUM = "EXACT_PUBLIC_PRICE_MEDIUM"
EXACT_PUBLIC_PRICE_LOW = "EXACT_PUBLIC_PRICE_LOW"
STRONG_COMMERCIAL_COMPARABLE = "STRONG_COMMERCIAL_COMPARABLE"
WEAK_COMPARABLE = "WEAK_COMPARABLE"
RFQ_ONLY = "RFQ_ONLY"
NO_PUBLIC_PRICE_FOUND = "NO_PUBLIC_PRICE_FOUND"
SEARCH_BLOCKED = "SEARCH_BLOCKED"
SEARCH_PROVIDER_FAILED = "SEARCH_PROVIDER_FAILED"
IDENTITY_INSUFFICIENT = "IDENTITY_INSUFFICIENT"

SELLER_MANUFACTURER = "MANUFACTURER"
SELLER_AUTHORIZED_DISTRIBUTOR = "AUTHORIZED_DISTRIBUTOR"
SELLER_ESTABLISHED_DISTRIBUTOR = "ESTABLISHED_DISTRIBUTOR"
SELLER_EQUIPMENT_DEALER = "EQUIPMENT_DEALER"
SELLER_RETAILER = "RETAILER"
SELLER_GOV_COOP = "GOVERNMENT_COOPERATIVE_CATALOG"
SELLER_PUBLIC_CONTRACT = "PUBLIC_CONTRACT_CATALOG"
SELLER_SURPLUS = "SURPLUS_DEALER"
SELLER_MARKETPLACE = "MARKETPLACE"
SELLER_UNKNOWN = "UNKNOWN"

CONDITION_NEW = "NEW"
CONDITION_NOS = "NEW_OLD_STOCK"
CONDITION_REFURB = "REFURBISHED"
CONDITION_USED = "USED"
CONDITION_UNKNOWN = "UNKNOWN"

_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

# Configurable domain hints by product family (not economics hard-codes)
DOMAIN_HINTS: dict[str, list[str]] = {
    "IT": [
        "cdw.com",
        "newegg.com",
        "bhphotovideo.com",
        "bestbuy.com",
        "dell.com",
        "asus.com",
        "lenovo.com",
        "hp.com",
        "cdw-g.com",
    ],
    "MRO": [
        "grainger.com",
        "zoro.com",
        "mscdirect.com",
        "globalindustrial.com",
        "fastenal.com",
        "mcmaster.com",
        "digikey.com",
        "mouser.com",
    ],
    "EQUIPMENT": [
        "bobcat.com",
        "caterpillar.com",
        "genielift.com",
        "machinerytrader.com",
        "ironplanet.com",
        "fastenal.com",
        "grainger.com",
        "ford.com",
        "sourcewell-mn.gov",
    ],
    "LAB": [
        "fishersci.com",
        "vwr.com",
        "avantorsciences.com",
        "coleparmer.com",
        "thomassci.com",
        "flir.com",
        "teledyneflir.com",
    ],
    "OFFICE": ["cdw.com", "staples.com", "officedepot.com", "canon.com", "usa.canon.com"],
}

_COMMERCIAL_POS = re.compile(
    r"\b(dell|asus|lenovo|hp\b|canon|flir|bobcat|toolcat|genie|caterpillar|\bcat\b|"
    r"ford|printer|monitor|laptop|server|switch|router|generator|pump|tractor|"
    r"forklift|camera|tool|mro|grainger|commercial|brand\s+name)\b",
    re.I,
)
_MIL_ONLY_NEG = re.compile(
    r"\b(source\s+approval|drawing[\-\s]?controlled|depot\s+repair|"
    r"sustaining\s+engineering|BOA\s+holders?\s+only|overhaul)\b",
    re.I,
)
_RFQ_ONLY_RE = re.compile(
    r"\b(request\s+(a\s+)?quote|call\s+for\s+(price|quote)|rfq\s+only|"
    r"submit\s+rfq|no\s+list\s+price|price\s+on\s+request)\b",
    re.I,
)
_USED_RE = re.compile(r"\b(used|refurbished|refurb|pre[\-\s]?owned|surplus)\b", re.I)
_DOLLAR_RE = re.compile(
    r"(?:USD\s*)?\$\s*("
    r"\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?"  # $1,450.00
    r"|\d{1,3}(?:,\d{3})*\.\d{2}"  # $12.50 or $1,299.99
    r"|\d{4,7}(?:\.\d{1,2})?"  # $1299 or $53000
    r")"
    r"|\bPrice\s*[:#]?\s*\$?\s*("
    r"\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?"
    r"|\d{1,3}(?:,\d{3})*\.\d{2}"
    r"|\d{4,7}(?:\.\d{1,2})?"
    r")"
    r"|\bPurchase\s*\$?\s*("
    r"\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?"
    r"|\d{1,3}(?:,\d{3})*\.\d{2}"
    r"|\d{4,7}(?:\.\d{1,2})?"
    r")",
    re.I,
)


def _utc() -> str:
    return now_utc().isoformat()


def _num(raw: Any) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        return float(str(raw).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None


def normalize_mpn_variants(mpn: str | None) -> list[str]:
    if not mpn:
        return []
    raw = str(mpn).strip()
    if not raw or raw.upper() in {"UNKNOWN", "NONE"}:
        return []
    variants = [raw]
    compact = re.sub(r"[^A-Za-z0-9]", "", raw)
    if compact and compact not in variants:
        variants.append(compact)
    spaced = re.sub(r"([A-Za-z]+)(\d)", r"\1 \2", raw)
    spaced = re.sub(r"(\d)([A-Za-z]+)", r"\1 \2", spaced)
    if spaced != raw and spaced not in variants:
        variants.append(spaced)
    # strip trailing rev markers lightly
    no_rev = re.sub(r"[-_/]R\d+$", "", raw, flags=re.I)
    if no_rev and no_rev not in variants:
        variants.append(no_rev)
    return variants


def build_search_identity(identity: dict[str, Any], row: dict[str, Any] | None = None) -> dict[str, Any]:
    """Canonical search identity from Phase J (+ row hints)."""
    row = row or {}
    nsn = identity.get("nsn") or identity.get("normalized_nsn") or row.get("nsn")
    mpn = identity.get("mpn") or identity.get("normalized_mpn") or row.get("mpn")
    title = row.get("title") or identity.get("nomenclature") or ""
    # Recover MPN from common solicitation title patterns when Phase J left it empty
    if not mpn:
        m = re.search(
            r"(?:P/?N|PN|Part\s*(?:Number|No\.?)|MPN)\s*[:_\-]?\s*([A-Z0-9][A-Z0-9\-_/]{3,})",
            title,
            re.I,
        )
        if m:
            mpn = m.group(1).strip().rstrip(".,;")
    alts = list(identity.get("mpns") or identity.get("alternate_part_numbers") or [])
    if mpn and mpn not in alts:
        alts = [mpn] + [a for a in alts if a != mpn]
    variants: list[str] = []
    for p in alts:
        for v in normalize_mpn_variants(p):
            if v not in variants:
                variants.append(v)
    model = identity.get("model") or row.get("model")
    mfr = identity.get("manufacturer") or row.get("manufacturer") or identity.get("brand") or row.get("brand")
    # title already set above
    # Recover commercial model from title when identity is partial (e.g. ToolCat UW56)
    # Do NOT invent short engine/NSN tokens as models when an MPN already exists.
    if not model and not mpn:
        m = re.search(
            r"\b((?:ToolCat\s+)?UW\d{2}|PowerEdge\s+R\d{3,4}|ImageRUNNER\s+\w+)\b",
            title,
            re.I,
        )
        if m:
            model = m.group(1).strip()
    if not mfr:
        m = re.search(
            r"\b(Dell|ASUS|Lenovo|HP|Canon|FLIR|Bobcat|Genie|Caterpillar|Eaton|Cisco)\b",
            title,
            re.I,
        )
        if m:
            mfr = m.group(1)
    return {
        "nsn": nsn,
        "niin": str(nsn).replace("-", "")[-9:] if nsn else None,
        "manufacturer": mfr,
        "cage": identity.get("cage") or row.get("cage"),
        "primary_mpn": mpn,
        "alternate_mpns": alts[1:] if len(alts) > 1 else [],
        "mpn_variants": variants,
        "sku": identity.get("sku") or row.get("sku"),
        "model": model,
        "brand": identity.get("brand") or row.get("brand") or mfr,
        "product_title": title,
        "approved_sources": identity.get("cages") or [],
        "cross_references": [],
    }


def generate_market_queries(search_id: dict[str, Any], *, max_queries: int = 24) -> list[str]:
    """Deterministic query chain — commercial model-first when no MPN; else MPN-first."""
    qs: list[str] = []
    mfr = search_id.get("manufacturer")
    model = search_id.get("model")
    sku = search_id.get("sku")
    title = (search_id.get("product_title") or "")[:80]
    nsn = search_id.get("nsn")
    has_mpn = bool(search_id.get("mpn_variants") or search_id.get("primary_mpn"))

    def add(q: str) -> None:
        q = re.sub(r"\s+", " ", q).strip()
        if q and q not in qs:
            qs.append(q)

    # Commercial model-first (L.2.3) when manufacturer+model exist
    if mfr and model:
        add(f'"{mfr}" "{model}"')
        add(f'"{model}"')
        add(f"{mfr} {model} price")
        add(f"{mfr} {model} dealer")
        add(f"{mfr} {model} distributor")
        add(f"{mfr} {model} MSRP")
        add(f"{mfr} {model} buy")
        add(f'"{model}" {mfr} buy')

    for v in search_id.get("mpn_variants") or []:
        add(f'"{v}"')
        add(f'"{v}" price')
        add(f'"{v}" distributor')
        add(f'"{v}" supplier')
        add(f'"{v}" buy')
        if mfr:
            add(f'"{v}" {mfr}')
        add(v)
        if nsn:
            add(f"{nsn} {v}")
        if v == (search_id.get("mpn_variants") or [None])[0]:
            for site in ("grainger.com", "zoro.com", "mouser.com"):
                add(f'site:{site} "{v}"')

    if sku and str(sku).upper() not in {"UNKNOWN", ""}:
        add(f'"{sku}" price')
        add(f"{sku} buy")

    if model and not mfr:
        add(f'"{model}" price')
        add(f'"{model}" buy')

    if title and (search_id.get("primary_mpn") or model):
        key = search_id.get("primary_mpn") or model
        add(f'"{key}" {title[:40]}')

    if title and mfr and not model:
        add(f"{mfr} {title} price")

    # NSN alone only after commercial keys exhausted
    if nsn and not has_mpn and not model:
        add(f"NSN {nsn} price distributor")
        add(f"{nsn} buy")

    return qs[:max_queries]


def commercial_researchability_score(row: dict[str, Any], identity: dict[str, Any] | None = None) -> dict[str, Any]:
    """Prefer publicly priceable commercial items for market-rescue validation."""
    identity = identity or {}
    blob = f"{row.get('title') or ''} {row.get('description') or ''}"
    score = 0
    reasons: list[str] = []

    if identity.get("mpn") or identity.get("normalized_mpn"):
        score += 35
        reasons.append("has_mpn")
    if identity.get("nsn") and identity.get("mpn"):
        score += 10
        reasons.append("nsn_plus_mpn")
    if identity.get("manufacturer"):
        score += 10
        reasons.append("manufacturer")
    if _COMMERCIAL_POS.search(blob):
        score += 25
        reasons.append("commercial_signals")
    if row.get("or_equal_allowed") or re.search(r"or\s+equal|brand\s+name", blob, re.I):
        score += 8
        reasons.append("or_equal")
    if row.get("quantity") is not None:
        score += 10
        reasons.append("qty_known")
    if row.get("historical_award_unit_price") is not None:
        score += 12
        reasons.append("history_available")
    if _MIL_ONLY_NEG.search(blob) and not _COMMERCIAL_POS.search(blob):
        score -= 25
        reasons.append("military_only_penalty")
    if identity.get("nsn") and not identity.get("mpn") and not _COMMERCIAL_POS.search(blob):
        score -= 10
        reasons.append("nsn_without_mpn_obscure")

    return {"researchability_score": score, "reasons": reasons}


def infer_product_family(row: dict[str, Any], search_id: dict[str, Any]) -> str:
    blob = f"{row.get('title') or ''} {search_id.get('product_title') or ''}".lower()
    if any(x in blob for x in ("dell", "asus", "laptop", "monitor", "server", "switch", "printer", "router")):
        return "IT"
    if any(x in blob for x in ("flir", "lab", "spectrometer", "microscope", "pipette")):
        return "LAB"
    if any(x in blob for x in ("bobcat", "toolcat", "genie", "caterpillar", "tractor", "forklift", "excavator")):
        return "EQUIPMENT"
    if any(x in blob for x in ("grainger", "bearing", "gasket", "valve", "nsn", "mro")):
        return "MRO"
    if any(x in blob for x in ("canon", "copier", "imagerunner", "office")):
        return "OFFICE"
    return "MRO"


def classify_seller_type(url: str, seller: str | None = None) -> str:
    host = (url or "").lower()
    name = (seller or "").lower()
    if any(x in host for x in ("dell.com", "asus.com", "canon.com", "flir.com", "bobcat.com", "genielift.com")):
        return SELLER_MANUFACTURER
    if any(x in host for x in ("grainger.com", "mscdirect.com", "digikey.com", "mouser.com", "cdw.com", "zoro.com")):
        return SELLER_ESTABLISHED_DISTRIBUTOR
    if any(x in host for x in ("bestbuy.com", "newegg.com", "bhphoto", "amazon.com")):
        return SELLER_MARKETPLACE if "amazon." in host else SELLER_RETAILER
    if any(x in host for x in ("sourcewell", "omnia", "naspo", "buyboard")):
        return SELLER_GOV_COOP
    if "ebay." in host or "craigslist" in host:
        return SELLER_MARKETPLACE
    if "surplus" in host or "surplus" in name:
        return SELLER_SURPLUS
    if "dealer" in host or "dealer" in name:
        return SELLER_EQUIPMENT_DEALER
    return SELLER_UNKNOWN


CONDITION_OPEN_BOX = "OPEN_BOX"


def classify_condition(text: str) -> str:
    t = (text or "").lower()
    if re.search(r"new[\-\s]?old[\-\s]?stock|\bnos\b", t):
        return CONDITION_NOS
    if re.search(r"\bopen[\-\s]?box\b", t):
        return CONDITION_OPEN_BOX
    if _USED_RE.search(t):
        if "refurb" in t:
            return CONDITION_REFURB
        return CONDITION_USED
    if re.search(r"\bnew\b", t):
        return CONDITION_NEW
    return CONDITION_UNKNOWN


def extract_json_ld_prices(text: str, *, source_url: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        text,
        re.I | re.S,
    ):
        raw = m.group(1).strip()
        try:
            data = json.loads(raw)
        except Exception:
            continue
        nodes = data if isinstance(data, list) else [data]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            offers = node.get("offers") or node.get("Offers")
            if isinstance(offers, dict):
                offers = [offers]
            if not isinstance(offers, list):
                continue
            for off in offers:
                if not isinstance(off, dict):
                    continue
                price = _num(off.get("price") or off.get("lowPrice"))
                if price is None or price <= 0:
                    continue
                out.append(
                    {
                        "unit_price": price,
                        "seller": off.get("seller", {}).get("name")
                        if isinstance(off.get("seller"), dict)
                        else None,
                        "url": source_url,
                        "condition": CONDITION_NEW,
                        "confidence": "HIGH",
                        "exact_match": "JSON_LD",
                        "price_type": "RETAIL",
                        "source_type": "json_ld",
                        "evidence_type": "STRUCTURED_OFFER",
                        "seller_type": classify_seller_type(source_url),
                        "checked_at": _utc(),
                    }
                )
    return out


def extract_html_prices(text: str, *, source_url: str, product_hint: str = "") -> list[dict[str, Any]]:
    """Deterministic $ / Price: extraction — does not require UOM to accept a hit."""
    out: list[dict[str, Any]] = []
    hint_tokens = [t for t in re.findall(r"[A-Za-z0-9]{3,}", (product_hint or "").lower())[:8]]
    # meta product:price
    for m in re.finditer(
        r'property=["\']og:price:amount["\']\s+content=["\']([^"\']+)["\']|'
        r'content=["\']([^"\']+)["\']\s+property=["\']og:price:amount["\']|'
        r'itemprop=["\']price["\']\s+content=["\']([^"\']+)["\']',
        text,
        re.I,
    ):
        price = _num(m.group(1) or m.group(2) or m.group(3))
        if price and 0 < price < 5_000_000:
            out.append(
                {
                    "unit_price": price,
                    "url": source_url,
                    "confidence": "HIGH",
                    "exact_match": "META",
                    "condition": CONDITION_NEW,
                    "price_type": "RETAIL",
                    "source_type": "meta_price",
                    "seller_type": classify_seller_type(source_url),
                    "checked_at": _utc(),
                }
            )
    for m in _DOLLAR_RE.finditer(text):
        raw = m.group(1) or m.group(2) or m.group(3)
        price = _num(raw)
        if price is None or price <= 0 or price > 5_000_000:
            continue
        start = max(0, m.start() - 60)
        end = min(len(text), m.end() + 60)
        ctx = text[start:end]
        ctx_l = ctx.lower()
        if re.search(r"\b(copyright|tel|fax|zip|isbn|year\s+founded)\b", ctx_l):
            continue
        if re.search(r"(function\s*\(|getTime|replace\(/|\\\\\$1)", ctx_l):
            continue
        if "application/ld+json" in ctx_l:
            continue
        if price > 100_000 and "award" in ctx_l:
            continue
        if price < 5.0:
            continue
        condition = classify_condition(ctx)
        conf = "MEDIUM"
        if hint_tokens and any(t in ctx_l for t in hint_tokens):
            conf = "HIGH"
        if _RFQ_ONLY_RE.search(ctx_l):
            continue
        out.append(
            {
                "unit_price": price,
                "url": source_url,
                "confidence": conf,
                "exact_match": "TEXT" if conf == "HIGH" else "TEXT_WEAK",
                "condition": condition,
                "price_type": "RETAIL",
                "source_type": "html_text",
                "seller_type": classify_seller_type(source_url),
                "context_snippet": re.sub(r"\s+", " ", ctx)[:160],
                "checked_at": _utc(),
            }
        )
        if len(out) >= 20:
            break
    return out


def extract_prices_from_page(text: str, *, source_url: str, product_hint: str = "") -> list[dict[str, Any]]:
    obs = extract_json_ld_prices(text, source_url=source_url)
    obs.extend(extract_html_prices(text, source_url=source_url, product_hint=product_hint))
    # Also reuse existing extractor when it yields
    try:
        from m3_public_pricing_evidence import extract_price_observations

        for o in extract_price_observations(text, source_url=source_url, product_hint=product_hint):
            price = _num(o.get("Observed_price"))
            if price is None:
                continue
            obs.append(
                {
                    "unit_price": price,
                    "url": source_url,
                    "confidence": o.get("Confidence") or "MEDIUM",
                    "exact_match": "LEGACY_EXTRACT",
                    "condition": classify_condition(str(o.get("context_snippet") or "")),
                    "price_type": "RETAIL",
                    "source_type": "legacy_extract_price_observations",
                    "seller_type": classify_seller_type(source_url),
                    "context_snippet": o.get("context_snippet"),
                    "checked_at": _utc(),
                }
            )
    except Exception:
        pass
    return obs


def bing_search_urls(query: str, *, limit: int = 8) -> tuple[list[str], str]:
    """Returns (urls, status). status=OK|SEARCH_PROVIDER_FAILED."""
    try:
        import httpx

        url = f"https://www.bing.com/search?q={quote_plus(query)}"
        r = httpx.get(url, timeout=8.0, follow_redirects=True, headers=_UA)
        if r.status_code >= 400:
            return [], SEARCH_PROVIDER_FAILED
        text = r.text or ""
        found: list[str] = []
        allowed_hosts = {d for domains in DOMAIN_HINTS.values() for d in domains}
        # Prefer full non-bing https hrefs first
        for m in re.finditer(r'href="(https?://(?!www\.bing)[^"]+)"', text):
            u = m.group(1)
            if any(x in u for x in ("bing.com", "microsoft.com", "msn.com", "facebook.com", "youtube.com")):
                continue
            if u not in found:
                found.append(u)
            if len(found) >= limit * 2:
                break
        # cite tags (often truncated roots)
        for m in re.finditer(r"<cite[^>]*>(.*?)</cite>", text, re.I | re.S):
            raw = re.sub(r"<[^>]+>", "", m.group(1))
            raw = re.sub(r"\s+", "", raw).replace("›", "/").strip()
            raw = raw.split("…")[0].split("...")[0]
            if not raw or any(x in raw for x in ("bing.", "microsoft.", "msn.")):
                continue
            if not raw.startswith("http"):
                raw = "https://" + raw.lstrip("/")
            host_m = re.match(r"(https?://[^/]+)", raw)
            if host_m:
                root = host_m.group(1) + "/"
                host = host_m.group(1).lower()
                # Keep roots only when they are known commercial catalogs
                if any(h in host for h in allowed_hosts) and root not in found:
                    found.append(root)
            if len(found) >= limit * 2:
                break

        # Rank: product-ish paths and allowlisted hosts first; drop random hotels/blogs
        def _rank(u: str) -> tuple[int, int]:
            low = u.lower()
            host_ok = any(h in low for h in allowed_hosts)
            productish = 1 if re.search(r"/(product|products|p/|dp/|item|sku|part)/", low) else 0
            root_only = 1 if re.match(r"https?://[^/]+/?$", low) else 0
            return (0 if host_ok else 1, 0 if productish else (2 if root_only else 1))

        found = sorted(dict.fromkeys(found), key=_rank)
        # If we have any allowlisted hits, prefer them exclusively
        preferred = [u for u in found if any(h in u.lower() for h in allowed_hosts)]
        if preferred:
            found = preferred + [u for u in found if u not in preferred]
        return found[:limit], "OK" if found else SEARCH_PROVIDER_FAILED
    except Exception:
        return [], SEARCH_PROVIDER_FAILED


def duckduckgo_search_urls(query: str, *, limit: int = 8) -> tuple[list[str], str]:
    try:
        from m3_public_pricing_evidence import CostLedger, duckduckgo_urls

        ledger = CostLedger()
        log: list[dict[str, Any]] = []
        urls = duckduckgo_urls(query, ledger, log, limit=limit)
        if urls:
            return urls, "OK"
        if any(str(x.get("access_state") or "") in {"AUTH_REQUIRED", "BOT_BLOCKED"} for x in log):
            return [], SEARCH_PROVIDER_FAILED
        return [], SEARCH_PROVIDER_FAILED
    except Exception:
        return [], SEARCH_PROVIDER_FAILED


def search_urls_with_fallback(query: str, *, limit: int = 8) -> dict[str, Any]:
    """Ordered provider fallback. Failed provider ≠ market price unknown yet."""
    attempts: list[dict[str, Any]] = []
    # Prefer Bing; only try DDG if Bing returns nothing usable
    for name, fn in (("bing", bing_search_urls),):
        urls, status = fn(query, limit=limit)
        attempts.append({"provider": name, "status": status, "url_count": len(urls)})
        if status == "OK" and urls:
            return {"urls": urls, "provider": name, "attempts": attempts, "ok": True}
    # DDG fallback once
    urls, status = duckduckgo_search_urls(query, limit=limit)
    attempts.append({"provider": "duckduckgo", "status": status, "url_count": len(urls)})
    if status == "OK" and urls:
        return {"urls": urls, "provider": "duckduckgo", "attempts": attempts, "ok": True}
    return {
        "urls": [],
        "provider": None,
        "attempts": attempts,
        "ok": False,
        "status": SEARCH_PROVIDER_FAILED,
    }


def _domain_search_url(domain: str, key: str) -> str | None:
    q = quote_plus(key)
    d = domain.lower().replace("www.", "")
    if "grainger.com" in d:
        return f"https://www.grainger.com/search?searchQuery={q}"
    if "zoro.com" in d:
        return f"https://www.zoro.com/search?q={q}"
    if "mscdirect.com" in d:
        return f"https://www.mscdirect.com/browse/tn/?searchterm={q}"
    if "digikey.com" in d:
        return f"https://www.digikey.com/en/products?keywords={q}"
    if "mouser.com" in d:
        return f"https://www.mouser.com/c/?q={q}"
    if "newegg.com" in d:
        return f"https://www.newegg.com/p/pl?d={q}"
    if "bhphotovideo.com" in d:
        return f"https://www.bhphotovideo.com/c/search?Ntt={q}"
    if "cdw.com" in d:
        return f"https://www.cdw.com/search/?key={q}"
    if "dell.com" in d:
        return f"https://www.dell.com/en-us/search/{q}"
    if "fishersci.com" in d:
        return f"https://www.fishersci.com/us/en/catalog/search/products.html?keyword={q}"
    if "coleparmer.com" in d:
        return f"https://www.coleparmer.com/search?searchterm={q}"
    if "globalindustrial.com" in d:
        return f"https://www.globalindustrial.com/search?q={q}"
    if "fastenal.com" in d:
        return f"https://www.fastenal.com/product/search?term={q}"
    if "mcmaster.com" in d:
        return f"https://www.mcmaster.com/search/{q}"
    if "bobcat.com" in d:
        return f"https://www.bobcat.com/search?q={q}"
    if "machinerytrader.com" in d:
        return f"https://www.machinerytrader.com/list/search?keywords={q}"
    if "sourcewell-mn.gov" in d or "sourcewell" in d:
        return f"https://www.sourcewell-mn.gov/search?search={q}"
    if "naspovaluepoint.org" in d:
        return f"https://www.naspovaluepoint.org/?s={q}"
    if "ford.com" in d:
        return f"https://www.ford.com/search/?searchTerm={q}"
    return None


def direct_domain_urls(search_id: dict[str, Any], family: str) -> list[str]:
    """site-style paths / search URLs for known public catalogs."""
    domains = DOMAIN_HINTS.get(family) or DOMAIN_HINTS["MRO"]
    # Always include MRO/IT distributors for MPN lookups
    extra = DOMAIN_HINTS["MRO"][:4] + DOMAIN_HINTS["IT"][:3]
    ordered = list(dict.fromkeys([*domains, *extra]))
    keys = (search_id.get("mpn_variants") or [])[:2]
    if search_id.get("sku"):
        keys.append(str(search_id["sku"]))
    if search_id.get("model"):
        keys.append(str(search_id["model"]))
    urls: list[str] = []
    for domain in ordered[:8]:
        for key in keys[:2]:
            if not key:
                continue
            u = _domain_search_url(domain, key)
            if u and u not in urls:
                urls.append(u)
    return urls[:14]


def expand_root_to_catalog_search(urls: list[str], search_id: dict[str, Any]) -> list[str]:
    """Convert homepage/root SERP citations into distributor search URLs."""
    key = (search_id.get("primary_mpn") or search_id.get("model") or search_id.get("sku") or "")
    if not key:
        return []
    out: list[str] = []
    for u in urls:
        try:
            host = urlparse(u).netloc.lower().replace("www.", "")
            path = urlparse(u).path or "/"
        except Exception:
            continue
        if path not in {"", "/"} and "/search" not in path:
            continue
        mapped = _domain_search_url(host, str(key))
        if mapped and mapped not in out:
            out.append(mapped)
    return out


def select_acquisition_baseline(
    observations: list[dict[str, Any]],
    *,
    require_new: bool = True,
    product_hint: str = "",
) -> dict[str, Any] | None:
    if not observations:
        return None
    allowed_hosts = {
        d
        for domains in DOMAIN_HINTS.values()
        for d in domains
    }
    scored: list[tuple[float, dict[str, Any]]] = []
    hint_toks = [t for t in re.findall(r"[a-z0-9]{3,}", (product_hint or "").lower())[:8]]
    for o in observations:
        price = _num(o.get("unit_price"))
        if price is None or price <= 0:
            continue
        url = str(o.get("url") or "").lower()
        host_ok = any(h in url for h in allowed_hosts)
        hint_ok = bool(hint_toks) and any(t in url or t in str(o.get("context_snippet") or "").lower() for t in hint_toks)
        if not host_ok and not hint_ok and o.get("exact_match") not in {"JSON_LD", "META"}:
            # Reject off-topic SERP landing pages (news/blogs) without product signal
            continue
        cond = str(o.get("condition") or CONDITION_UNKNOWN).upper()
        if require_new and cond in {CONDITION_USED, CONDITION_REFURB}:
            score = -100.0
        else:
            score = 0.0
        st = str(o.get("seller_type") or "")
        if st == SELLER_MANUFACTURER:
            score += 40
        elif st in {SELLER_AUTHORIZED_DISTRIBUTOR, SELLER_ESTABLISHED_DISTRIBUTOR}:
            score += 35
        elif st == SELLER_RETAILER:
            score += 25
        elif st == SELLER_MARKETPLACE:
            score += 5
        elif st == SELLER_SURPLUS:
            score -= 20
        conf = str(o.get("confidence") or "").upper()
        if conf in {"HIGH", "EXACT"}:
            score += 20
        elif conf in {"MEDIUM"}:
            score += 10
        if o.get("exact_match") in {"JSON_LD", "META", "EXACT"}:
            score += 15
        if o.get("listing_page"):
            score -= 30
        if str(o.get("confidence") or "").upper() == "LOW":
            score -= 15
        if host_ok:
            score += 20
        scored.append((score, {**o, "unit_price": price}))
    if not scored:
        return None
    scored.sort(key=lambda x: (x[0], -abs(x[1]["unit_price"])), reverse=True)
    best = scored[0]
    if best[0] < 0:
        return {**best[1], "selection_warning": "only_used_or_refurb_found"}
    return {
        **best[1],
        "selection_reason": "prefer_new_reputable_over_lowest",
    }


def map_confidence(observations: list[dict[str, Any]], selected: dict[str, Any] | None, *, rfq_only: bool) -> str:
    if rfq_only and not observations:
        return RFQ_ONLY
    if not selected:
        return NO_PUBLIC_PRICE_FOUND
    conf = str(selected.get("confidence") or "").upper()
    st = selected.get("seller_type")
    if selected.get("exact_match") in {"JSON_LD", "META"} and st in {
        SELLER_MANUFACTURER,
        SELLER_ESTABLISHED_DISTRIBUTOR,
        SELLER_RETAILER,
    }:
        return EXACT_PUBLIC_PRICE_HIGH
    if conf == "HIGH":
        return EXACT_PUBLIC_PRICE_MEDIUM
    if conf == "MEDIUM":
        return EXACT_PUBLIC_PRICE_LOW
    if selected.get("selection_warning"):
        return WEAK_COMPARABLE
    return STRONG_COMMERCIAL_COMPARABLE


def select_verified_acquisition_benchmark(
    evidence: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Median of economics-eligible exact/strong verified prices — never lowest blindly."""
    from phase_l.product_page_resolution import EXACT_VERIFIED, STRONG_VERIFIED

    eligible = [
        e
        for e in evidence
        if e.get("economics_eligible")
        and e.get("confidence") in {EXACT_VERIFIED, STRONG_VERIFIED}
        and _num(e.get("price")) is not None
        and float(e["price"]) > 0
    ]
    if not eligible:
        return None
    prices = sorted(float(e["price"]) for e in eligible)
    median = prices[len(prices) // 2]
    # Prefer evidence closest to median with highest confidence
    exact = [e for e in eligible if e.get("confidence") == EXACT_VERIFIED]
    pool = exact or eligible
    chosen = min(pool, key=lambda e: abs(float(e["price"]) - median))
    return {
        **chosen,
        "unit_price": float(chosen["price"]),
        "url": chosen.get("source_url"),
        "selection_reason": (
            f"{len(eligible)} verified prices {[round(p, 2) for p in prices]}; "
            f"selected median-credible {median}"
        ),
        "verified_price_count": len(eligible),
        "verified_prices": prices,
    }


def research_public_market_price(
    row: dict[str, Any],
    identity: dict[str, Any],
    *,
    budget: dict[str, int] | None = None,
    max_pages: int = 10,
    authorize_live: bool = True,
) -> dict[str, Any]:
    """L.2.2: search → product URL resolve → identity verify → verified price only."""
    from phase_l.product_page_resolution import (
        APPROXIMATE,
        EXACT_VERIFIED,
        REJECTED,
        STRONG_VERIFIED,
        WEAK,
        resolve_and_verify_market_price,
    )

    budget = budget or {}
    search_id = build_search_identity(identity, row)
    has_key = bool(
        search_id.get("primary_mpn")
        or search_id.get("sku")
        or search_id.get("nsn")
        or search_id.get("model")
    )
    commercial_key = bool(
        search_id.get("primary_mpn")
        or search_id.get("sku")
        or (search_id.get("model") and search_id.get("manufacturer"))
    )
    # L.2.2: NSN-only military lookups rarely yield public product pages; skip to save budget
    if has_key and not commercial_key:
        return {
            "kind": "PhaseL22MarketPrice",
            "phase": "L.2.2",
            "market_price_confidence": NO_PUBLIC_PRICE_FOUND,
            "attempted": True,
            "observations": [],
            "evidence": [],
            "public_retail_unit_price": None,
            "drop_reason": "NO_EXACT_IDENTITY",
            "pages_fetched": 0,
            "product_pages_fetched": 0,
            "search_results_found": 0,
            "candidate_links_extracted": 0,
            "note": "nsn_only_skipped_for_l22_commercial_validation",
        }
    if not has_key:
        return {
            "kind": "PhaseL22MarketPrice",
            "market_price_confidence": IDENTITY_INSUFFICIENT,
            "attempted": False,
            "observations": [],
            "evidence": [],
            "public_retail_unit_price": None,
            "drop_reason": "NO_EXACT_IDENTITY",
        }

    if not authorize_live:
        return {
            "kind": "PhaseL22MarketPrice",
            "market_price_confidence": NO_PUBLIC_PRICE_FOUND,
            "attempted": False,
            "skipped": "live_disabled",
            "observations": [],
            "evidence": [],
        }

    if budget.get("market", 0) >= budget.get("market_max", 100):
        return {
            "kind": "PhaseL22MarketPrice",
            "market_price_confidence": SEARCH_BLOCKED,
            "attempted": False,
            "skipped": "budget",
            "observations": [],
            "evidence": [],
        }

    family = infer_product_family(row, search_id)
    queries = generate_market_queries(search_id, max_queries=16)
    observations: list[dict[str, Any]] = []
    evidence_all: list[dict[str, Any]] = []
    provider_attempts: list[dict[str, Any]] = []
    pages_fetched = 0
    product_pages_fetched = 0
    search_pages_seen = 0
    candidate_links_extracted = 0
    exact_identity_pages = 0
    strong_identity_pages = 0
    rejected_mismatches = 0
    fetch_failed = 0
    rfq_only_hits = 0
    drop_reasons: list[str] = []
    mpn_query_attempted = bool(search_id.get("mpn_variants"))
    search_results_found = 0

    from m3_public_pricing_evidence import ACCESS_OK, CostLedger, fetch_public_text

    ledger = CostLedger()
    access_log: list[dict[str, Any]] = []
    seen: set[str] = set()

    candidate_urls: list[str] = []
    for u in direct_domain_urls(search_id, family):
        if u not in candidate_urls:
            candidate_urls.append(u)

    for q in queries[:3]:
        result = search_urls_with_fallback(q, limit=4)
        provider_attempts.extend(result.get("attempts") or [])
        urls = result.get("urls") or []
        if urls:
            search_results_found += len(urls)
        for u in urls:
            if u not in candidate_urls:
                candidate_urls.append(u)
        for u in expand_root_to_catalog_search(urls, search_id):
            if u not in candidate_urls:
                candidate_urls.append(u)

    product_queue: list[str] = []
    listing_budget = max(3, max_pages // 2)

    def _fetch(url: str) -> tuple[str | None, str]:
        nonlocal pages_fetched, fetch_failed
        if pages_fetched >= max_pages:
            return None, "BUDGET"
        host = (url or "").lower()
        if any(x in host for x in ("bing.com/search", "duckduckgo.com", "google.com/search", "yahoo.com/search")):
            return None, "SERP_SKIP"
        text, state = fetch_public_text(url, ledger, access_log, seen=seen)
        if state != ACCESS_OK or not text:
            fetch_failed += 1
            drop_reasons.append("FETCH_FAILED")
            return None, state or "FETCH_FAILED"
        pages_fetched += 1
        budget["market"] = budget.get("market", 0) + 1
        return text, ACCESS_OK

    # Pass 1: discovery pages → extract product links (do not accept listing prices)
    for u in candidate_urls:
        if pages_fetched >= listing_budget and product_queue:
            break
        if pages_fetched >= max_pages:
            break
        text, state = _fetch(u)
        if not text:
            continue
        resolved = resolve_and_verify_market_price(
            html=text, url=u, search_id=search_id, row=row, from_listing=True
        )
        page_type = resolved.get("page_type")
        links = resolved.get("candidate_links") or []
        if links:
            search_pages_seen += 1
            candidate_links_extracted += len(links)
            for link in links:
                pu = link.get("url")
                if pu and pu not in product_queue and pu not in seen:
                    product_queue.append(pu)
            drop_reasons.append(resolved.get("drop_reason") or "SEARCH_PAGE_ONLY")
            continue

        # Direct product / PDF / catalog page
        if page_type in {
            "SEARCH_RESULTS_PAGE",
            "CATEGORY_PAGE",
            "HOMEPAGE",
            "UNRELATED",
        }:
            search_pages_seen += 1
            drop_reasons.append(resolved.get("drop_reason") or "SEARCH_PAGE_ONLY")
            continue

        product_pages_fetched += 1
        if _RFQ_ONLY_RE.search(text[:8000]) and not resolved.get("evidence"):
            rfq_only_hits += 1
            drop_reasons.append("RFQ_ONLY")
            continue

        idm = resolved.get("identity_match") or {}
        ml = idm.get("match_level")
        if ml == "EXACT_MPN":
            exact_identity_pages += 1
        elif ml in {"EXACT_ALT_MPN", "EXACT_MODEL", "EXACT_SKU", "EXACT_NSN_PRODUCT", "STRONG_TITLE_MATCH"}:
            strong_identity_pages += 1
        elif ml in {"NO_MATCH", "CONFLICT", "PARTIAL_MATCH"}:
            rejected_mismatches += 1
            drop_reasons.append("PRODUCT_IDENTITY_MISMATCH")

        for ev in resolved.get("evidence") or []:
            evidence_all.append(ev)
            if ev.get("economics_eligible"):
                observations.append(
                    {
                        "unit_price": ev["price"],
                        "url": ev.get("source_url"),
                        "confidence": "HIGH" if ev.get("confidence") == EXACT_VERIFIED else "MEDIUM",
                        "exact_match": "L22_VERIFIED",
                        "condition": ev.get("condition"),
                        "price_type": "RETAIL",
                        "source_type": ev.get("extraction_method"),
                        "seller_type": ev.get("seller_type"),
                        "context_snippet": ev.get("evidence_text"),
                        "checked_at": ev.get("fetched_at"),
                        "l22_confidence": ev.get("confidence"),
                        "economics_eligible": True,
                        "product_match_level": ev.get("product_match_level"),
                        "page_type": ev.get("page_type"),
                    }
                )
            elif ev.get("confidence") in {APPROXIMATE, WEAK}:
                observations.append(
                    {
                        "unit_price": ev["price"],
                        "url": ev.get("source_url"),
                        "confidence": "LOW",
                        "exact_match": "L22_RESEARCH_ONLY",
                        "listing_page": False,
                        "l22_confidence": ev.get("confidence"),
                        "economics_eligible": False,
                        "rejection_reason": ev.get("rejection_reason"),
                    }
                )
            else:
                drop_reasons.append(str(ev.get("rejection_reason") or REJECTED))

        if resolved.get("drop_reason") and not resolved.get("evidence"):
            drop_reasons.append(str(resolved["drop_reason"]))

    # Pass 2: fetch ranked product URLs from listings
    for pu in product_queue:
        if pages_fetched >= max_pages:
            break
        if any(e.get("economics_eligible") for e in evidence_all) and product_pages_fetched >= 4:
            break
        text, state = _fetch(pu)
        if not text:
            continue
        product_pages_fetched += 1
        resolved = resolve_and_verify_market_price(
            html=text, url=pu, search_id=search_id, row=row
        )
        # If we accidentally queued another listing, extract deeper once
        if resolved.get("candidate_links") and resolved.get("page_type") in {
            "SEARCH_RESULTS_PAGE",
            "CATEGORY_PAGE",
        }:
            drop_reasons.append("SEARCH_PAGE_ONLY")
            continue

        idm = resolved.get("identity_match") or {}
        ml = idm.get("match_level")
        if ml == "EXACT_MPN":
            exact_identity_pages += 1
        elif ml in {"EXACT_ALT_MPN", "EXACT_MODEL", "EXACT_SKU", "EXACT_NSN_PRODUCT", "STRONG_TITLE_MATCH"}:
            strong_identity_pages += 1
        elif ml in {"NO_MATCH", "CONFLICT", "PARTIAL_MATCH"}:
            rejected_mismatches += 1
            drop_reasons.append("PRODUCT_IDENTITY_MISMATCH")

        for ev in resolved.get("evidence") or []:
            evidence_all.append(ev)
            if ev.get("economics_eligible"):
                observations.append(
                    {
                        "unit_price": ev["price"],
                        "url": ev.get("source_url"),
                        "confidence": "HIGH" if ev.get("confidence") == EXACT_VERIFIED else "MEDIUM",
                        "exact_match": "L22_VERIFIED",
                        "condition": ev.get("condition"),
                        "price_type": "RETAIL",
                        "source_type": ev.get("extraction_method"),
                        "seller_type": ev.get("seller_type"),
                        "context_snippet": ev.get("evidence_text"),
                        "checked_at": ev.get("fetched_at"),
                        "l22_confidence": ev.get("confidence"),
                        "economics_eligible": True,
                        "product_match_level": ev.get("product_match_level"),
                        "page_type": ev.get("page_type"),
                    }
                )
            elif ev.get("confidence") in {APPROXIMATE, WEAK, REJECTED}:
                drop_reasons.append(str(ev.get("rejection_reason") or ev.get("confidence")))

        if resolved.get("drop_reason") and not any(
            e.get("source_url") == pu and e.get("economics_eligible") for e in evidence_all
        ):
            drop_reasons.append(str(resolved["drop_reason"]))

    selected = select_verified_acquisition_benchmark(evidence_all)
    # Research-only observations (non-eligible) never set public_retail
    verified_obs = [o for o in observations if o.get("economics_eligible")]
    prices = [float(o["unit_price"]) for o in verified_obs if _num(o.get("unit_price"))]
    all_evidence_prices = [
        float(e["price"]) for e in evidence_all if _num(e.get("price")) and e.get("confidence") != REJECTED
    ]

    all_providers_failed = (
        provider_attempts
        and all(a.get("status") == SEARCH_PROVIDER_FAILED for a in provider_attempts)
        and pages_fetched == 0
    )

    if selected:
        conf_l22 = selected.get("confidence") or STRONG_VERIFIED
        if conf_l22 == EXACT_VERIFIED:
            confidence = EXACT_PUBLIC_PRICE_HIGH
        else:
            confidence = EXACT_PUBLIC_PRICE_MEDIUM
    elif rfq_only_hits > 0 and not verified_obs:
        confidence = RFQ_ONLY
    elif all_providers_failed:
        confidence = SEARCH_PROVIDER_FAILED
    else:
        confidence = NO_PUBLIC_PRICE_FOUND

    # Primary drop reason for telemetry
    from collections import Counter

    reason_counts = Counter(drop_reasons)
    primary_drop = reason_counts.most_common(1)[0][0] if reason_counts else None
    if not selected and not primary_drop:
        if candidate_links_extracted == 0 and search_results_found == 0:
            primary_drop = "NO_SEARCH_RESULTS"
        elif product_pages_fetched == 0:
            primary_drop = "NO_PRODUCT_URL"
        else:
            primary_drop = "NO_PRICE_ON_MATCHING_PAGE"

    out = {
        "kind": "PhaseL22MarketPrice",
        "phase": "L.2.2",
        "attempted": True,
        "mpn_query_attempted": mpn_query_attempted,
        "search_identity": search_id,
        "queries": queries[:12],
        "product_family": family,
        "provider_attempts": provider_attempts[-12:],
        "pages_fetched": pages_fetched,
        "product_pages_fetched": product_pages_fetched,
        "search_pages_seen": search_pages_seen,
        "search_results_found": search_results_found,
        "candidate_links_extracted": candidate_links_extracted,
        "exact_identity_pages": exact_identity_pages,
        "strong_identity_pages": strong_identity_pages,
        "rejected_mismatches": rejected_mismatches,
        "fetch_failed": fetch_failed,
        "rfq_only_pages": rfq_only_hits,
        "observations": observations[:30],
        "evidence": evidence_all[:40],
        "verified_evidence": [e for e in evidence_all if e.get("economics_eligible")][:20],
        "market_price_low": min(prices) if prices else None,
        "market_price_median": sorted(prices)[len(prices) // 2] if prices else None,
        "market_price_high": max(prices) if prices else None,
        "market_price_selected": selected.get("unit_price") if selected else None,
        "public_retail_unit_price": selected.get("unit_price") if selected else None,
        "public_retail_source": (selected or {}).get("url") or (selected or {}).get("source_url"),
        "selected_observation": selected,
        "selection_reason": (selected or {}).get("selection_reason"),
        "market_price_confidence": confidence,
        "l22_confidence": (selected or {}).get("confidence"),
        "economics_eligible": bool(selected),
        "drop_reason": None if selected else primary_drop,
        "drop_reason_counts": dict(reason_counts.most_common(12)),
        "research_prices_non_economic": all_evidence_prices[:10] if not selected else None,
        "market_research_state": (
            "MARKET_PRICE_FOUND"
            if selected and confidence not in {RFQ_ONLY, NO_PUBLIC_PRICE_FOUND, SEARCH_PROVIDER_FAILED, SEARCH_BLOCKED}
            else "MARKET_PRICE_PENDING"
        ),
        "retrieved_at": _utc(),
    }
    return out
