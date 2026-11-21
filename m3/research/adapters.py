"""Free-source adapters. Documented vs experimentally verified tracked in registry."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlparse

from m3.research.classify import PRODUCT_HINTS, classify_requirement
from m3.research.http_util import fetch_json, fetch_text
from m3.research.schema_map import (
    AGENCY_CANDIDATES,
    CATEGORY_CANDIDATES,
    DEADLINE_CANDIDATES,
    NUMBER_CANDIDATES,
    POSTED_CANDIDATES,
    STATUS_CANDIDATES,
    TITLE_CANDIDATES,
    URL_CANDIDATES,
    VALUE_CANDIDATES,
    pick_first_key,
)

PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[\s\-.]?)?\(?([2-9]\d{2})\)?[\s\-.]?([2-9]\d{2})[\s\-.]?(\d{4})(?!\d)")
VALUE_RANGE_RE = re.compile(
    r"\$?\s*([\d,]+(?:\.\d+)?)\s*(?:-|–|to)\s*\$?\s*([\d,]+(?:\.\d+)?)",
    re.I,
)
VALUE_SINGLE_RE = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)")


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _key(*parts: str) -> str:
    raw = "|".join(p.strip().lower() for p in parts if p)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _guess_category(title: str) -> str | None:
    t = title.lower()
    mapping = [
        ("furniture", r"furniture|desk|chair|cubicle|seating"),
        ("janitorial", r"janitorial|custodial|cleaning suppl|paper towel|toilet paper"),
        ("it_hardware", r"laptop|computer|chromebook|server|networking|printer|monitor"),
        ("appliances", r"appliance|refrigerat|washer|dryer|freezer|ice machine"),
        ("electrical", r"electrical|breaker|conduit|lighting|wire|cable"),
        ("plumbing", r"plumbing|faucet|valve|pipe"),
        ("tools_mro", r"\btools?\b|mro|fastener|industrial suppl|lubricant|oil"),
        ("safety_ppe", r"ppe|safety equipment|respirator|glove|hard hat"),
        ("office_supplies", r"office suppl|toner|paper cup|stationery"),
        ("vehicles_parts", r"vehicle|tire|automotive|parts"),
    ]
    for cat, pat in mapping:
        if re.search(pat, t):
            return cat
    return "general_products" if PRODUCT_HINTS.search(t) else None


def _parse_money(val: Any) -> float | None:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val)
    m = VALUE_RANGE_RE.search(s)
    if m:
        try:
            return (float(m.group(1).replace(",", "")) + float(m.group(2).replace(",", ""))) / 2.0
        except ValueError:
            pass
    m = re.search(r"([\d,]+(?:\.\d+)?)", s.replace("$", ""))
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _parse_value_band(val: Any) -> tuple[float | None, float | None]:
    """Parse published ranges like '$50,001 - $100,000' into min/max."""
    if val is None:
        return None, None
    if isinstance(val, (int, float)):
        f = float(val)
        return f, f
    s = str(val)
    m = VALUE_RANGE_RE.search(s)
    if m:
        try:
            lo = float(m.group(1).replace(",", ""))
            hi = float(m.group(2).replace(",", ""))
            return lo, hi
        except ValueError:
            return None, None
    single = _parse_money(s)
    return single, single


def _extract_url(val: Any, fallback: str | None = None) -> str | None:
    if isinstance(val, dict):
        u = val.get("url") or val.get("href")
        if u:
            return str(u)
    if isinstance(val, str) and val.startswith("http"):
        return val
    return fallback


def _row_get(row: dict, *keys: str) -> Any:
    for k in keys:
        if k and k in row and row[k] not in (None, ""):
            return row[k]
    return None


def _is_productish(title: str, category_raw: str | None, cfg: dict) -> bool:
    prefer_cats = [c.lower() for c in (cfg.get("prefer_categories") or [])]
    exclude_cats = [c.lower() for c in (cfg.get("exclude_categories") or [])]
    cat = (category_raw or "").lower()
    if exclude_cats and any(x in cat for x in exclude_cats):
        return False
    clf = classify_requirement(title, category_raw=category_raw)
    if prefer_cats and any(x in cat for x in prefer_cats):
        return clf["requirement_class"] not in {"services", "construction"}
    if cfg.get("is_forecast"):
        if re.search(r"commodit|supply|equipment|furniture|vehicle|goods|material", cat, re.I):
            return True
        return bool(clf["include_in_product_resale"] and PRODUCT_HINTS.search(f"{title} {cat}"))
    return bool(clf["include_in_product_resale"])


def search_socrata(
    source: dict,
    *,
    keywords: list[str] | None = None,
    limit: int | None = None,
    offset: int = 0,
    page_size: int = 500,
    max_pages: int | None = None,
    apply_adapter_keywords: bool = False,
) -> dict[str, Any]:
    """Retrieve Socrata rows with offset pagination — no artificial commercial ceiling.

    Keyword matching belongs in the authoritative filter layer unless
    ``apply_adapter_keywords`` is explicitly enabled for a verified source-native search.
    """
    from m3.config import get_settings

    settings = get_settings()
    page_size = min(500, max(50, page_size or settings.research_socrata_page_size))
    # 0 / None → use configured safeguard (default high); never a silent 3-page cap
    configured = int(max_pages if max_pages is not None else settings.research_max_pages_per_source)
    max_pages = max(1, configured if configured > 0 else 500)
    soft_limit = int(limit) if limit else None
    cfg = dict(source.get("config") or {})
    urls = [cfg.get("dataset_url") or source.get("base_url")]
    urls += list(cfg.get("fallback_urls") or [])
    items: list[dict] = []
    errors: list[str] = []
    used_url = None
    pages_fetched = 0
    total_rows_seen = 0
    mapping_failures = 0
    closed_excluded = 0
    non_product_excluded = 0
    keyword_excluded = 0
    truncated = False
    more_available = False
    pagination_complete = False
    page_offset = max(0, int(offset or 0))
    prefer_statuses = [s.lower() for s in (cfg.get("prefer_statuses") or [])]
    keywords = list(keywords or [])
    # Runtime field resolution from config + universal candidates
    title_keys = tuple(
        k
        for k in (cfg.get("title_field"), *TITLE_CANDIDATES)
        if k
    )
    status_keys = tuple(k for k in (cfg.get("status_field"), *STATUS_CANDIDATES) if k)
    agency_keys = tuple(k for k in (cfg.get("agency_field"), *AGENCY_CANDIDATES) if k)
    number_keys = tuple(k for k in (cfg.get("number_field"), *NUMBER_CANDIDATES) if k)
    deadline_keys = tuple(k for k in (cfg.get("deadline_field"), *DEADLINE_CANDIDATES) if k)
    posted_keys = tuple(k for k in (cfg.get("posted_field"), *POSTED_CANDIDATES) if k)
    value_keys = tuple(k for k in (cfg.get("value_field"), *VALUE_CANDIDATES) if k)
    category_keys = tuple(k for k in (cfg.get("category_field"), *CATEGORY_CANDIDATES) if k)
    url_keys = tuple(k for k in (cfg.get("url_field"), *URL_CANDIDATES) if k)

    def _append_row(row: dict, final: str) -> None:
        nonlocal items, mapping_failures, closed_excluded, non_product_excluded, keyword_excluded
        if not isinstance(row, dict):
            return
        title_key = pick_first_key(row, title_keys, cfg.get("title_field"))
        title = str((row.get(title_key) if title_key else "") or "").strip()
        if not title:
            # Last resort: longest string field that looks like a title (not an id/url)
            best = ""
            for k, v in row.items():
                if not isinstance(v, str):
                    continue
                kl = str(k).lower()
                if kl.startswith(":") or "url" in kl or "id" in kl or "date" in kl:
                    continue
                if 12 <= len(v.strip()) <= 400 and len(v.strip()) > len(best):
                    best = v.strip()
            title = best
        if not title or len(title) < 5:
            mapping_failures += 1
            return
        status_raw = str(_row_get(row, *status_keys) or "")
        if prefer_statuses:
            if status_raw and status_raw.lower() not in prefer_statuses:
                if re.search(r"cancel|closed|award|inactive|expired", status_raw, re.I):
                    closed_excluded += 1
                    return
                if status_raw.strip():
                    closed_excluded += 1
                    return
        elif status_raw and re.search(r"^(closed|award|awarded|cancelled|canceled|inactive|expired)$", status_raw, re.I):
            closed_excluded += 1
            return
        if re.search(r"cancel|indefinitely postponed", str(row), re.I) and "cancelledpostponed" in row:
            if row.get("cancelledpostponed"):
                closed_excluded += 1
                return
        category_raw = str(_row_get(row, *category_keys) or "")
        if not _is_productish(title, category_raw, cfg):
            non_product_excluded += 1
            return
        # Adapter keyword gate is OFF by default (Build 002A) — filter layer is authoritative
        if apply_adapter_keywords and keywords:
            blob = f"{title} {category_raw}".lower()
            prefer_cats = [c.lower() for c in (cfg.get("prefer_categories") or [])]
            is_prefer = prefer_cats and any(x in category_raw.lower() for x in prefer_cats)
            if not is_prefer and not any(k.lower() in blob for k in keywords if k):
                if not PRODUCT_HINTS.search(title) and not PRODUCT_HINTS.search(category_raw):
                    keyword_excluded += 1
                    return
        agency = str(
            _row_get(row, *agency_keys) or cfg.get("buyer_type") or "Unknown"
        )
        sol = str(_row_get(row, *number_keys) or "")
        deadline_raw = _row_get(row, *deadline_keys)
        posted_raw = _row_get(row, *posted_keys)
        # Drop clearly past deadlines when no prefer_statuses list (active discovery)
        if deadline_raw and not prefer_statuses:
            try:
                raw_d = str(deadline_raw).replace("Z", "+00:00")
                dt = datetime.fromisoformat(raw_d[:19])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt.year >= 2000 and dt < datetime.now(timezone.utc):
                    closed_excluded += 1
                    return
            except Exception:
                pass
        value_raw = _row_get(row, *value_keys)
        vmin, vmax = _parse_value_band(value_raw)
        official = _extract_url(
            _row_get(row, *url_keys),
            cfg.get("official_listing_url") or final,
        )
        set_aside_raw = _row_get(row, cfg.get("set_aside_field") or "set_aside", "mwbe", "lsbrpindicator")
        set_aside = "unknown"
        set_aside_verified = False
        if set_aside_raw is not None:
            smap = cfg.get("set_aside_map") or {}
            mapped = smap.get(str(set_aside_raw))
            if mapped:
                set_aside = mapped
                set_aside_verified = True
            else:
                set_aside = str(set_aside_raw)
                set_aside_verified = False
        cat = _guess_category(f"{title} {category_raw}")
        proc_type = str(
            _row_get(row, cfg.get("type_field") or "procurement_type", "type")
            or cfg.get("procurement_type_default")
            or "unknown"
        )
        clf = classify_requirement(title, category_raw=category_raw, procurement_type=proc_type)
        qty = _row_get(row, "quantity", "qty", "estimated_quantity")
        items.append(
            {
                "external_key": _key(source["source_key"], sol or title, official or ""),
                "title": title[:500],
                "solicitation_number": sol or None,
                "buyer_name": agency[:255],
                "geo_state": cfg.get("geo_state"),
                "geo_city": None,
                "geo_location": agency[:255],
                "deadline_at": str(deadline_raw) if deadline_raw else None,
                "deadline_timezone": "UTC" if deadline_raw else None,
                "posted_at": str(posted_raw)[:10] if posted_raw else None,
                "procurement_type": proc_type[:120],
                "product_category": cat or ("unknown" if not category_raw else category_raw[:80].lower()),
                "set_aside": set_aside if isinstance(set_aside, str) else "unknown",
                "set_aside_verified": set_aside_verified,
                "estimated_value_min": vmin,
                "estimated_value_max": vmax,
                "estimated_value_unknown": vmin is None and vmax is None,
                "value_estimate_method": (
                    "published_estimate_band"
                    if value_raw and vmin is not None
                    else ("published_amount" if vmin is not None else None)
                ),
                "value_evidence": str(value_raw)[:200] if value_raw else None,
                "value_evidence_class": ("published_estimate" if value_raw else "not_yet_researched"),
                "official_url": (official or final)[:2048],
                "documents_available": "yes" if official and "http" in (official or "") else "unknown",
                "quantity": str(qty) if qty not in (None, "") else None,
                "requirement_class": clf["requirement_class"],
                "classification_evidence": clf["classification_evidence"],
                "source_key": source["source_key"],
                "source_verified_at": _utc(),
                "is_forecast": bool(cfg.get("is_forecast")),
                "title_field_used": title_key,
                "evidence": {
                    "row_keys": list(row.keys())[:20],
                    "source_url": final,
                    "status": status_raw or None,
                    "category_raw": category_raw or None,
                    "classification": clf,
                    "title_field_used": title_key,
                },
            }
        )

    for url in urls:
        if not url:
            continue
        try:
            page_offset = max(0, int(offset or 0))
            hit_empty = False
            for _page in range(max_pages):
                if soft_limit is not None and len(items) >= soft_limit:
                    truncated = True
                    more_available = True
                    break
                q_url = f"{url}?$limit={page_size}&$offset={page_offset}"
                order_field = cfg.get("posted_field") or cfg.get("deadline_field")
                if order_field:
                    q_url += f"&$order={order_field} DESC"
                status, final, data = fetch_json(q_url)
                pages_fetched += 1
                if status >= 400 or not isinstance(data, list):
                    errors.append(f"{url} status={status} offset={page_offset}")
                    break
                used_url = final
                if not data:
                    hit_empty = True
                    pagination_complete = True
                    more_available = False
                    break
                total_rows_seen += len(data)
                for row in data:
                    if soft_limit is not None and len(items) >= soft_limit:
                        truncated = True
                        more_available = True
                        break
                    _append_row(row, final)
                if len(data) < page_size:
                    pagination_complete = True
                    more_available = False
                    break
                page_offset += page_size
                more_available = True
            else:
                # Exhausted max_pages without empty page → incomplete unless soft-limited
                if not hit_empty and not truncated:
                    more_available = True
                    pagination_complete = False
            if items or pagination_complete:
                break
        except Exception as exc:
            errors.append(f"{url}:{type(exc).__name__}:{exc}"[:200])

    out_items = items if soft_limit is None else items[:soft_limit]
    return {
        "ok": bool(out_items) or (pagination_complete and total_rows_seen > 0 and not errors),
        "items": out_items,
        "source_key": source["source_key"],
        "used_url": used_url,
        "errors": errors,
        "verified_at": _utc(),
        "pages_fetched": pages_fetched,
        "rows_examined": total_rows_seen,
        "truncated": truncated,
        "more_available": more_available,
        "pagination_complete": pagination_complete and not more_available,
        "next_offset": page_offset if more_available else None,
        "stats": {
            "mapping_failures": mapping_failures,
            "closed_excluded": closed_excluded,
            "non_product_excluded": non_product_excluded,
            "keyword_excluded": keyword_excluded,
            "accepted": len(out_items),
        },
    }



def search_html_links(
    source: dict,
    *,
    keywords: list[str] | None = None,
    limit: int = 30,
    apply_adapter_keywords: bool = False,
) -> dict[str, Any]:
    cfg = source.get("config") or {}
    urls = [cfg.get("list_url") or source.get("base_url")]
    urls += list(cfg.get("extra_urls") or [])
    hints = [h.lower() for h in (cfg.get("link_hints") or ["bid", "rfp"])]
    reject_pats = [re.compile(p, re.I) for p in (cfg.get("reject_title_patterns") or [])]
    items: list[dict] = []
    errors: list[str] = []
    seen: set[str] = set()
    keywords = list(keywords or [])

    for url in urls:
        if not url or len(items) >= limit:
            continue
        try:
            status, final, html = fetch_text(url)
            if status >= 400:
                errors.append(f"{url} status={status}")
                continue
            hrefs = re.findall(r'href=["\']([^"\']+)["\']', html, flags=re.I)
            texts = re.findall(r"<a[^>]*>(.*?)</a>", html, flags=re.I | re.S)
            for i, href in enumerate(hrefs):
                text = texts[i] if i < len(texts) else ""
                label = re.sub(r"<[^>]+>", "", text or "").strip()
                abs_url = urljoin(final, href)
                if abs_url in seen:
                    continue
                blob = f"{label} {abs_url}".lower()
                if not any(h in blob for h in hints):
                    continue
                # Skip pure templates / forms
                if any(p.search(label) or p.search(abs_url) for p in reject_pats):
                    continue
                # Prefer document/pdf/html solicitation pages over generic site chrome
                path = urlparse(abs_url).path.lower()
                if path.endswith((".css", ".js", ".png", ".jpg", ".gif", ".svg")):
                    continue
                # Keyword gate only when explicitly opted into source-native search
                if apply_adapter_keywords and keywords and not any(k.lower() in blob for k in keywords):
                    if not PRODUCT_HINTS.search(label):
                        continue
                title = label or abs_url.rsplit("/", 1)[-1]
                title = re.sub(r"\s+", " ", title).strip()
                if len(title) < 6:
                    continue
                seen.add(abs_url)
                clf = classify_requirement(title)
                if not clf["include_in_product_resale"] and clf["requirement_class"] in {"services", "construction"}:
                    continue
                items.append(
                    {
                        "external_key": _key(source["source_key"], abs_url),
                        "title": title[:500],
                        "solicitation_number": None,
                        "buyer_name": source.get("name"),
                        "geo_state": cfg.get("geo_state"),
                        "geo_city": None,
                        "geo_location": cfg.get("geo_state"),
                        "deadline_at": None,
                        "deadline_timezone": None,
                        "posted_at": None,
                        "procurement_type": "unknown",
                        "product_category": _guess_category(title) or "unknown",
                        "set_aside": "unknown",
                        "set_aside_verified": False,
                        "estimated_value_min": None,
                        "estimated_value_max": None,
                        "estimated_value_unknown": True,
                        "value_estimate_method": None,
                        "value_evidence": None,
                        "value_evidence_class": "not_yet_researched",
                        "official_url": abs_url[:2048],
                        "documents_available": "yes" if path.endswith((".pdf", ".doc", ".docx", ".xlsx")) else "unknown",
                        "quantity": None,
                        "requirement_class": clf["requirement_class"],
                        "classification_evidence": clf["classification_evidence"],
                        "source_key": source["source_key"],
                        "source_verified_at": _utc(),
                        "evidence": {"list_url": final, "classification": clf},
                    }
                )
                if len(items) >= limit:
                    break
        except Exception as exc:
            errors.append(f"{url}:{type(exc).__name__}:{exc}"[:200])

    return {
        "ok": bool(items),
        "items": items,
        "source_key": source["source_key"],
        "errors": errors,
        "verified_at": _utc(),
        "pages_fetched": len([u for u in urls if u]),
        "rows_examined": len(seen) or len(items),
        "pagination_complete": True,
        "more_available": False,
        "stats": {"accepted": len(items), "mapping_failures": 0},
    }


def discover_socrata_catalog(source: dict) -> dict[str, Any]:
    """Discover free public bid datasets via the Socrata catalog API (no paid key)."""
    cfg = source.get("config") or {}
    queries = list(cfg.get("queries") or ["open bid opportunities", "solicitations"])
    found: list[dict] = []
    errors: list[str] = []
    seen: set[str] = set()
    for q in queries:
        try:
            url = f"https://api.us.socrata.com/api/catalog/v1?q={q.replace(' ', '%20')}&only=datasets&limit=15"
            status, final, data = fetch_json(url)
            if status >= 400 or not isinstance(data, dict):
                errors.append(f"{q} status={status}")
                continue
            for it in data.get("results") or []:
                res = it.get("resource") or {}
                rid = res.get("id")
                domain = (it.get("metadata") or {}).get("domain")
                name = res.get("name") or ""
                if not rid or not domain or rid in seen:
                    continue
                if re.search(r"charit|campaign|political|broadband|test\b", name, re.I):
                    continue
                seen.add(rid)
                found.append(
                    {
                        "dataset_id": rid,
                        "name": name,
                        "domain": domain,
                        "dataset_url": f"https://{domain}/resource/{rid}.json",
                        "link": it.get("link"),
                        "discovered_via": q,
                    }
                )
        except Exception as exc:
            errors.append(f"{q}:{type(exc).__name__}:{exc}"[:200])
    return {
        "ok": bool(found),
        "items": found,
        "source_key": source["source_key"],
        "errors": errors,
        "verified_at": _utc(),
    }


def _valid_phone(match: re.Match[str]) -> str | None:
    a, b, c = match.group(1), match.group(2), match.group(3)
    # Reject obvious non-phones (repeating, 000, etc.)
    if a[0] in "01" or b[0] in "01":
        return None
    if len(set(a + b + c)) <= 2:
        return None
    return f"({a}) {b}-{c}"


def _extract_phone(html: str) -> str | None:
    # Prefer explicit tel: links
    for href in re.findall(r'href=["\']tel:([^"\']+)["\']', html, flags=re.I):
        digits = re.sub(r"\D", "", href)
        if digits.startswith("1") and len(digits) == 11:
            digits = digits[1:]
        if len(digits) == 10:
            m = PHONE_RE.search(f"({digits[:3]}) {digits[3:6]}-{digits[6:]}")
            if m:
                p = _valid_phone(m)
                if p:
                    return p
    # Prefer numbers near contact language (windowed scan)
    for m in re.finditer(r"(?:phone|tel\.?|call us|contact|toll[- ]free)(.{0,60})", html, flags=re.I):
        inner = PHONE_RE.search(m.group(0))
        if inner:
            p = _valid_phone(inner)
            if p:
                return p
    for m in PHONE_RE.finditer(html):
        p = _valid_phone(m)
        if not p:
            continue
        start = max(0, m.start() - 12)
        ctx = html[start : m.end() + 12]
        if re.search(r"(rgba?|width|height|px|em|rem|version|sku|isbn)", ctx, re.I):
            continue
        return p
    return None


def probe_supplier_page(source: dict) -> dict[str, Any]:
    cfg = source.get("config") or {}
    urls = [source.get("base_url")] + list(cfg.get("extra_pages") or [])
    errors: list[str] = []
    phone = None
    email = None
    app_links: list[str] = []
    final_ok = None
    status_ok = None

    for url in urls:
        if not url:
            continue
        try:
            status, final, html = fetch_text(url)
            if status >= 400:
                errors.append(f"{url} status={status}")
                continue
            final_ok = final
            status_ok = status
            phone = phone or _extract_phone(html)
            emails = re.findall(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", html, flags=re.I)
            for e in emails:
                if not re.search(r"(example\.com|sentry|wixpress|schema|webpack|cloudflare|noreply)", e, re.I):
                    email = email or e
                    break
            hrefs = re.findall(r'href=["\']([^"\']+)["\']', html, flags=re.I)
            texts = re.findall(r"<a[^>]*>(.*?)</a>", html, flags=re.I | re.S)
            for i, href in enumerate(hrefs):
                label = re.sub(r"<[^>]+>", "", texts[i] if i < len(texts) else "").lower()
                h = href.lower()
                if any(
                    x in label or x in h
                    for x in (
                        "reseller",
                        "become a partner",
                        "become a dealer",
                        "apply",
                        "credit application",
                        "open an account",
                        "new account",
                        "government",
                        "trade account",
                    )
                ):
                    abs_u = urljoin(final, href)
                    if not abs_u.startswith("http"):
                        continue
                    if re.search(
                        r"\.(css|js|png|jpg|gif|svg|woff2?)(\?|$)|datadome|adobecqms|_next/static|images-na\.ssl-images",
                        abs_u,
                        re.I,
                    ):
                        continue
                    if abs_u not in app_links:
                        app_links.append(abs_u)
            if final_ok:
                break
        except Exception as exc:
            errors.append(f"{url}:{type(exc).__name__}:{exc}"[:200])

    if not final_ok:
        return {"ok": False, "item": None, "errors": errors or ["unreachable"]}

    reseller = next(
        (a for a in app_links if re.search(r"reseller|partner|dealer|account|apply|government", a, re.I)),
        None,
    )
    item = {
        "name": source.get("name"),
        "website": final_ok,
        "supplier_type": cfg.get("supplier_type") or "unknown",
        "product_categories": list(cfg.get("categories") or []),
        "brands": list(cfg.get("brands") or []),
        "reseller_application_url": reseller,
        "credit_application_url": next((a for a in app_links if "credit" in a.lower()), None),
        "phone": phone,
        "email": email,
        "catalog_available": "unknown",
        "geo_coverage": "nationwide",
        "verification_status": "website_retrieved",
        "source_urls": [final_ok] + ([reseller] if reseller else [])[:3],
        "next_action": "Confirm reseller/credit path and record contacts",
        "source_key": source["source_key"],
        "evidence": {"http_status": status_ok, "app_links_found": len(app_links)},
    }
    return {"ok": True, "item": item, "errors": errors, "verified_at": _utc()}
